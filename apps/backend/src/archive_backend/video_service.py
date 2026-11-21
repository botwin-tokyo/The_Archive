"""Durable owner-only video queue; isolated worker, core-owned publication."""
import copy,hashlib,re,threading,time,uuid
from .contracts import BackendError,digest,identifier,location,object_fields,path
from .music_contracts import public,label
from .video_contracts import canonical_url,metadata,MAX_BYTES
from .video_worker_client import VideoWorkerClient
from .video_imports import VideoImports
from . import acquisition_auth

TERMINAL={'completed','failed','canceled','paused'}

class VideoService:
    def __init__(self,service,config,worker=None):
        self.service,self.state,self.config=service,service.state,config
        self.worker=worker or (VideoWorkerClient(config) if config else None)
        self.imports=VideoImports(service,config) if config else None
        self.lock=threading.RLock();self.stop=threading.Event();self.thread=None;self.import_thread=None;self.available=False

    def start(self):
        if self.config and not self.thread:
            self.thread=threading.Thread(target=self.run,name='archive-video',daemon=True);self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=10)
        if self.import_thread:self.import_thread.join(timeout=45)
        if self.import_thread and self.import_thread.is_alive():raise BackendError('BUSY','Video importer still stopping')

    def actor(self,row):
        return acquisition_auth.principal(self.service.auth, row['_actor'])

    def get(self,ns,id):
        identifier(id);row=self.state.get('video_'+ns,id)
        if row is None:raise BackendError('NOT_FOUND','Video request not found')
        return row

    def save(self,ns,row):
        row['revision']+=1;self.state.put('video_'+ns,row['id'],row)

    def destination(self,principal,value):
        location(value)
        self.service.auth.authorize(principal,'write',value['driveId'],value['path'])
        if self.service.catalog.filesystem(value['driveId']).stat(value['path']).kind!='directory':raise BackendError('INVALID_PATH','Select an existing folder')
        return value

    def accept(self,principal,cap,args,create):
        identifier(args['requestId']);key=digest([principal.id,cap,args['requestId']]);fingerprint=digest(args)
        with self.state.transaction():
            old=self.state.get('video_requests',key)
            if old:
                if old['digest']!=fingerprint:raise BackendError('CONFLICT','Request ID was used with different arguments')
                return public(acquisition_auth.visible(self.service.auth,principal,self.get(old['namespace'],old['id'])))
            ns,row=create();self.state.put('video_'+ns,row['id'],row)
            self.state.put('video_requests',key,{'digest':fingerprint,'namespace':ns,'id':row['id']})
            self.state.audit(principal.id,cap,'accepted',row['id']);return public(row)

    def call(self,principal,cap,args):
        principal=self.service.auth.authorize(principal,'acquire')
        with self.lock:
            if cap=='video.status':
                default=None
                try:default=self.destination(principal,{'driveId':'files','path':'Videos'})
                except BackendError:pass
                return {'enabled':bool(self.config),'available':self.available,'defaultDestination':default,'supportedSites':['YouTube','X / Twitter'],'maxBytes':str(MAX_BYTES),'mcp':True}
            if cap=='video.plan.get':return public(acquisition_auth.visible(self.service.auth,principal,self.get('plans',args['id'])))
            if cap=='video.jobs.get':return public(acquisition_auth.visible(self.service.auth,principal,self.get('jobs',args['id'])))
            if cap=='video.jobs.list':
                limit=args.get('limit',100)
                if type(limit) is not int or not 1<=limit<=100:raise BackendError('INVALID_ARGUMENT','Page size must be 1–100')
                rows=sorted((r for r in self.state.list('video_jobs',1000) if acquisition_auth.accessible(self.service.auth,principal,r)),key=lambda r:(r['createdAt'],r['id']),reverse=True)
                if args.get('cursor'):
                    identifier(args['cursor']);i=next((i for i,r in enumerate(rows) if r['id']==args['cursor']),None)
                    if i is None:raise BackendError('INVALID_ARGUMENT','Invalid video cursor')
                    rows=rows[i+1:]
                return {'items':public(rows[:limit]),'cursor':rows[limit-1]['id'] if len(rows)>limit else None}
            if not self.config:raise BackendError('UNAVAILABLE','Video worker is not configured',True)
            if cap=='video.resolve':
                try:url=canonical_url(args['url'])
                except ValueError:raise BackendError('INVALID_ARGUMENT','Use a single public YouTube or X/Twitter video link; playlists and other sites are unsupported') from None
                def create():
                    rows=self.state.list('video_plans',1000)
                    if len(rows)>=1000 or sum(r['state'] in ('queued','running') for r in rows)>=20:raise BackendError('QUOTA_EXCEEDED','Video preview queue is full')
                    return 'plans',{'id':uuid.uuid4().hex,'revision':1,'state':'queued','title':'Inspecting video','uploader':'','duration':0,'source':'','choices':[],'expiresAt':time.time()+900,'error':None,'_url':url,'_actor':principal.as_dict(),'_task':None}
                return self.accept(principal,cap,args,create)
            if cap=='video.enqueue':
                def create():
                    p=acquisition_auth.visible(self.service.auth,principal,self.get('plans',args['planId']))
                    if type(args['planRevision']) is not int or p['revision']!=args['planRevision'] or p['state']!='completed' or p['expiresAt']<time.time():raise BackendError('CONFLICT','Preview changed or expired; inspect the link again')
                    choice=next((c for c in p['choices'] if c['id']==args['choiceId']),None)
                    if choice is None:raise BackendError('INVALID_ARGUMENT','Choose an available format')
                    dest=self.destination(principal,args['destination']);rows=self.state.list('video_jobs',1000)
                    if len(rows)>=1000 or sum(r['state'] not in ('completed','failed','canceled') for r in rows)>=100:raise BackendError('QUOTA_EXCEEDED','Video queue/history is full')
                    suffix=hashlib.sha256((p['source']+choice['id']).encode()).hexdigest()[:12]
                    name=label(p['title'],150)+' [video-'+suffix+'].'+('mp3' if choice['kind']=='audio' else 'mp4')
                    target=path('/'.join(x for x in (dest['path'],name) if x))
                    return 'jobs',{'id':uuid.uuid4().hex,'revision':1,'state':'queued','title':p['title'],'destination':copy.deepcopy(dest),'createdAt':time.time(),'choice':choice,'progress':None,'error':None,'entry':None,'_actor':principal.as_dict(),'_url':p['_url'],'_source':p['source'],'_duration':p['duration'],'_path':target,'_task':None,'_position':0,'_attempt':0}
                # attempt is private in the public contract; use _attempt internally.
                return self.accept(principal,cap,args,create)
            if cap=='video.jobs.control':
                row=acquisition_auth.visible(self.service.auth,principal,self.get('jobs',args['id']))
                if type(args['revision']) is not int or row['revision']!=args['revision']:raise BackendError('STALE_REVISION','Refresh the video job first')
                action=args['action']
                if action not in ('pause','resume','cancel','retry-failed'):raise BackendError('INVALID_ARGUMENT','Unknown control')
                if row['state']=='importing':raise BackendError('BUSY','Verified publication is finishing; refresh shortly',True)
                if row['state'] in ('completed','canceled'):return public(row)
                if action=='resume' and row['state']!='paused' or action=='retry-failed' and row['state']!='failed':raise BackendError('CONFLICT','Control does not apply to this job')
                # A finalized transfer wins over a late control, including recovery after a crash.
                if row.get('_transfer'):
                    receipt=self.service.transfers.query(principal,row['_transfer'])
                    if receipt['state']=='completed':
                        row.update(state='completed',entry=self.imports.record(row,row['destination']['driveId'],receipt['entry'],principal),progress=1,error=None)
                        self.save('jobs',row);return public(row)
                    if action in ('cancel','retry-failed'):
                        self.service.transfers.cancel(principal,row['_transfer'],discard=True);row.pop('_transfer',None)

                if row.get('_task') and action in ('pause','cancel'):
                    self.worker.call(op='cancel',taskId=row['_task'])
                    if not row.get('_artifact'):self.worker.call(op='cleanup',taskId=row['_task']);row['_task']=None
                row['state']={'pause':'paused','resume':'queued','cancel':'canceled','retry-failed':'queued'}[action];row['error']=None
                if action=='retry-failed':
                    row['_automatic']=0;row.pop('_artifact',None);row['progress']=None
                    if row.get('_task'):self.worker.call(op='cleanup',taskId=row['_task']);row['_task']=None
                self.save('jobs',row);return public(row)
            raise BackendError('INVALID_ARGUMENT','Unknown video capability')

    def run(self):
        while not self.stop.is_set():
            try:
                with self.lock:self.tick()
            except Exception:self.available=False
            self.stop.wait(1)

    def tick(self):
        self.available=bool(self.worker.call(op='status')['available'])
        plans=self.state.list('video_plans',1000)
        for p in plans:
            if p['expiresAt']<time.time() and p['state'] in ('completed','failed'):
                # Keep request receipts valid; bounded history avoids silent idempotency loss.
                continue
            if p['state'] not in ('queued','running'):continue
            try:
                self.actor(p)
                result=self.task(p,'plans','resolve',{'url':p['_url']})
                if result['state']=='completed':
                    p.update(metadata(result['result']),state='completed',expiresAt=time.time()+900);self.save('plans',p);self.cleanup(p,'plans')
                elif result['state'] in ('failed','canceled'):
                    p.update(state='failed',error=self.error(result));self.save('plans',p);self.cleanup(p,'plans')
            except BackendError as e:
                if e.code not in ('BUSY','UNAVAILABLE'):p.update(state='failed',error=e.message);self.save('plans',p)
            break
        rows=self.state.list('video_jobs',1000)
        # Always renew the existing acquisition before admitting another.
        rows.sort(key=lambda r:(not bool(r.get('_task')),r['createdAt']))
        for j in rows:
            if j['state'] in TERMINAL:
                if j['state'] in ('completed','canceled') and j.get('_task'):self.cleanup(j,'jobs')
                continue
            if j['state']=='importing' and self.import_thread and self.import_thread.is_alive():continue
            try:
                principal=self.actor(j);self.destination(principal,j['destination'])
                if j.get('_artifact'):
                    if not self.import_thread or not self.import_thread.is_alive():
                        j['state']='importing';self.save('jobs',j)
                        self.import_thread=threading.Thread(target=self.publish,args=(j,),name='archive-video-import',daemon=True);self.import_thread.start()
                    continue
                present=self.imports.present(principal,j,j['destination'])
                if present:j.update(state='completed',entry=present,progress=1,error=None);self.save('jobs',j);continue
                if time.time()<j.get('_retryAt',0):continue
                result=self.task(j,'jobs','download',{'url':j['_url'],'choiceId':j['choice']['id'],'source':j['_source'],'duration':j['_duration']})
                if result['state']=='completed':
                    a=result['result'];object_fields(a,('filename','sizeBytes','sha256'))
                    expected='media.'+('mp3' if j['choice']['kind']=='audio' else 'mp4')
                    if a['filename']!=expected or not isinstance(a['sha256'],str) or not re.fullmatch('[a-f0-9]{64}',a['sha256']) or not isinstance(a['sizeBytes'],str) or not a['sizeBytes'].isdigit() or not 0<int(a['sizeBytes'])<=MAX_BYTES:raise BackendError('INVALID_RESPONSE','Invalid video artifact')
                    j.update(_artifact=a,state='validating',progress=None,error=None);self.save('jobs',j)
                elif result['state'] in ('failed','canceled'):
                    self.cleanup(j,'jobs');n=j.get('_automatic',0)+1;j['_automatic']=n
                    retry=result.get('error') in ('SOURCE_UNAVAILABLE','LEASE_EXPIRED') and n<3
                    j.update(state='queued' if retry else 'failed',error=self.error(result),_retryAt=time.time()+(5 if n==1 else 30));self.save('jobs',j)
                else:
                    state=result.get('stage','downloading');fraction=result.get('progress')
                    if j['state']!=state or j['progress']!=fraction:j.update(state=state,progress=fraction,error=None);self.save('jobs',j)
            except BackendError as e:
                if e.code in ('BUSY','UNAVAILABLE'):continue
                j.update(state='waiting-for-drive' if e.code in ('DRIVE_UNAVAILABLE','VOLUME_MISMATCH','INSUFFICIENT_SPACE','READ_ONLY') else 'failed',error=e.message[:500]);self.save('jobs',j)

    def task(self,row,ns,action,payload):
        if row.get('_task'):
            result=self.worker.call(op='poll',taskId=row['_task'])
            if result['state']!='absent':return result
            self.worker.call(op='cleanup',taskId=row['_task']);row['_task']=None
        row['_task']=uuid.uuid4().hex;row['_attempt']=row.get('_attempt',0)+1
        self.save(ns,row)
        result=self.worker.call(op='start',taskId=row['_task'],action=action,payload=payload)
        if ns=='plans':row['state']='running';self.save(ns,row)
        return result

    def cleanup(self,row,ns):
        if row.get('_task'):
            self.worker.call(op='cleanup',taskId=row['_task']);row['_task']=None;self.save(ns,row)

    def publish(self,row):
        try:
            principal=self.actor(row)
            def save():
                with self.lock:self.save('jobs',row)
            entry=self.imports.publish(principal,row,row,save)
            with self.lock:row.update(state='completed',entry=entry,progress=1,error=None);self.save('jobs',row)
        except BackendError as e:
            with self.lock:
                row.update(state='waiting-for-drive' if e.code in ('DRIVE_UNAVAILABLE','VOLUME_MISMATCH','INSUFFICIENT_SPACE','READ_ONLY','BUSY') else 'failed',error=e.message[:500]);self.save('jobs',row)
        except Exception:
            with self.lock:row.update(state='failed',error='Verified publication failed; retry after checking the destination');self.save('jobs',row)

    @staticmethod
    def error(result):
        code=result.get('error','SOURCE_UNAVAILABLE')
        return {'MULTI_VIDEO_UNSUPPORTED':'This link contains multiple videos; only single videos are supported','LIVE_OR_DRM_UNSUPPORTED':'Live or protected video is unsupported','QUALITY_UNAVAILABLE':'Selected quality is no longer available; inspect again','SOURCE_CHANGED':'Source changed since preview; inspect again','DISK_BUDGET':'Insufficient download staging space','NO_COMPATIBLE_FORMAT':'No compatible MP4 or audio format is available'}.get(code,'Source unavailable or media validation failed ('+str(code)[:60]+')')
