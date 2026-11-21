"""Durable owner-only anime discovery, plans and episode jobs.

The isolated worker performs all network access. The core owns accepted
requests, destinations, publication, library receipts and final outcomes.
"""
import copy
import json
import threading
import time
import uuid
from .contracts import BackendError, digest, identifier, location
from .anime_contracts import (ACTIVE, AUDIO, MAX_EPISODES_PER_JOB, QUALITY, SUBTITLE_LANGUAGES, SUCCESS, TERMINAL, EPISODE_ID,
                              anilist_id, artifact, folder, lookup_result, provider_series, public, search_result, target)
from .anime_imports import AnimeImports, library_key, occupied
from .anime_worker_client import AnimeWorkerClient
from .security import Principal

JOB_TERMINAL = ('completed', 'partial', 'failed', 'canceled')
ACTIVE_JOBS = "namespace='anime_jobs' AND json_extract(body,'$.state') NOT IN ('completed','partial','failed','canceled')"
LOOKUP_SECONDS = 1800
PLAN_SECONDS = 900
SEARCH_CACHE_SECONDS = 600
HISTORY = 1000
ACTIVE_JOB_LIMIT = 50
QUEUED_EPISODE_LIMIT = 2000
UNAVAILABLE = {'VARIANT_UNAVAILABLE': 'This language is not available for this episode',
               'QUALITY_UNAVAILABLE': 'The selected quality is not available for this episode',
               'SUBTITLE_UNAVAILABLE': 'A selected subtitle language is not available for this episode',
               'EPISODE_UNAVAILABLE': 'The provider no longer lists this episode',
               'INCOMPLETE_SOURCE': "The provider's copy of this episode is far shorter than its listed runtime; it was not saved. Retry later"}
FAILURES = {'UNSUPPORTED_STREAM': 'The provider offered an unsupported stream format', 'UNSUPPORTED_PLAYLIST': 'The provider playlist uses an unsupported format',
            'INVALID_MEDIA': 'Downloaded media failed verification', 'DISK_BUDGET': 'Insufficient download staging space',
            'SOURCE_CHANGED': 'The provider changed unexpectedly; try again later', 'RATE_LIMITED': 'The provider is rate limiting requests; retry later'}
RETRYABLE = ('SOURCE_UNAVAILABLE', 'LEASE_EXPIRED', 'RATE_LIMITED', 'WORKER_RESTARTED')


def new_id():
    """Time-ordered keys let history pages and the active-job index avoid sorting every record."""
    return '%013x%s' % (int(time.time()*1000), uuid.uuid4().hex[:19])


class AnimeService:
    def __init__(self, service, config, worker=None):
        self.service,self.state,self.config=service,service.state,config
        self.worker=worker or (AnimeWorkerClient(config) if config else None)
        self.lock=threading.RLock(); self.stop=threading.Event(); self.thread=None; self.import_thread=None
        self.imports=AnimeImports(service,config,self.stop) if config else None
        self.health={'available':False,'at':0}; self.last_error=None
        if self.state is not None:
            with self.state.transaction() as connection:
                connection.execute("CREATE INDEX IF NOT EXISTS anime_active_jobs ON records(namespace,key) WHERE "+ACTIVE_JOBS)

    # Lifecycle -------------------------------------------------------------
    def start(self):
        if self.config and not self.thread:
            self.stop.clear(); self.thread=threading.Thread(target=self.run,name='archive-anime',daemon=True); self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread: self.thread.join(timeout=15)
        if self.import_thread: self.import_thread.join(timeout=45)
        if self.import_thread and self.import_thread.is_alive(): raise BackendError('BUSY','Anime importer is still stopping')

    def run(self):
        while not self.stop.is_set():
            try:
                with self.lock: self.tick()
                self.last_error=None
            except Exception as error:
                self.last_error=error.code if isinstance(error,BackendError) else 'INTERNAL_ERROR'
            self.stop.wait(1)

    # Records ---------------------------------------------------------------
    def principal(self, value):
        return self.service.auth.owner(Principal(value['id'],value['label'],value['kind'],value['credentialId'],value['scopeVersion'],tuple(value['scopes']),tuple((r['driveId'],r['path']) for r in value['roots'])))

    def get(self, namespace, id):
        identifier(id); row=self.state.get('anime_'+namespace,id)
        if row is None: raise BackendError('NOT_FOUND','Anime request was not found')
        return row

    def put(self, namespace, row):
        if 'revision' in row: row['revision']+=1
        self.state.put('anime_'+namespace,row['id'],row)

    def accept(self, principal, action, args, create):
        identifier(args['requestId']); key=digest([principal.id,'anime.'+action,args['requestId']]); fingerprint=digest(args)
        with self.state.transaction():
            prior=self.state.get('anime_requests',key)
            if prior:
                if prior['digest']!=fingerprint: raise BackendError('CONFLICT','This anime request ID was used with different arguments')
                return self.get(prior['namespace'],prior['id'])
            namespace,row=create()
            row['_requestKey']=key
            self.state.put('anime_'+namespace,row['id'],row)
            self.state.put('anime_requests',key,{'id':row['id'],'namespace':namespace,'digest':fingerprint,'action':action,'createdAt':time.time()})
            self.state.audit(principal.id,'anime.'+action,'accepted',row['id'])
            return row

    def active_jobs(self):
        return [json.loads(r['body']) for r in self.state.select("SELECT body FROM records WHERE "+ACTIVE_JOBS+" ORDER BY key LIMIT ?",(ACTIVE_JOB_LIMIT+1,))]

    def destination(self, principal, value):
        location(value)
        if not any(v['id']==value['driveId'] and v.get('role')=='files' for v in self.service.config.volumes): raise BackendError('PERMISSION_DENIED','Select a File Storage folder')
        self.service.auth.authorize(principal,'write',value['driveId'],value['path'])
        if self.service.catalog.filesystem(value['driveId']).stat(value['path']).kind!='directory': raise BackendError('INVALID_PATH','Select an existing folder')
        return {'driveId':value['driveId'],'path':value['path']}

    def default_destination(self, principal):
        configured=(self.config or {}).get('defaultDestination')
        if configured:
            # An operator-chosen default is never silently replaced by another drive.
            try: return self.destination(principal,configured)
            except BackendError: return None
        for candidate in ({'driveId':'files','path':'Anime'},{'driveId':'files','path':'Videos'}):
            try: return self.destination(principal,candidate)
            except BackendError: continue
        return None

    def enabled(self):
        if not self.config: raise BackendError('UNAVAILABLE','The anime worker is not configured',True)

    # Capabilities ----------------------------------------------------------
    def call(self, principal, capability, args):
        self.service.auth.owner(principal)
        with self.lock:
            if capability=='anime.status':
                available=bool(self.config) and self.health['available']
                return {'enabled':bool(self.config),'available':available,'provider':'ani.pm','catalog':'AniList',
                        'message':'Ready · availability varies by series and episode' if available else 'The anime worker is starting or unavailable. Existing files are safe.',
                        'defaultDestination':self.default_destination(principal),'maxEpisodesPerJob':MAX_EPISODES_PER_JOB,'audio':list(AUDIO),'mcp':True}
            if capability=='anime.search.get': return public(self.get('searches',args['id']))
            if capability=='anime.series.get': return public(self.get('series',args['id']))
            if capability=='anime.plan.get': return public(self.get('plans',args['id']))
            if capability=='anime.jobs.get': return public(self.get('jobs',args['id']))
            if capability=='anime.cover.get':
                cover=self.state.get('anime_covers',str(anilist_id(args['anilistId'])))
                return {'dataUrl':cover['dataUrl'] if cover else None}
            if capability=='anime.jobs.list': return self.list_jobs(args)
            if capability=='anime.reconcile': return self.reconcile(principal,args['requestId'])
            if capability=='anime.search': self.enabled(); return public(self.accept(principal,'search',args,lambda:self.create_search(principal,args)))
            if capability=='anime.series.lookup': self.enabled(); return public(self.accept(principal,'lookup',args,lambda:self.create_lookup(principal,args)))
            if capability=='anime.resolve': return public(self.accept(principal,'resolve',args,lambda:self.create_plan(principal,args)))
            if capability=='anime.enqueue': self.enabled(); return public(self.accept(principal,'enqueue',args,lambda:self.create_job(principal,args)))
            if capability=='anime.jobs.control': return self.control(principal,args)
        raise BackendError('INVALID_ARGUMENT','Unknown anime capability')

    def reconcile(self, principal, request_id):
        identifier(request_id)
        for action in ('enqueue','resolve','lookup','search'):
            prior=self.state.get('anime_requests',digest([principal.id,'anime.'+action,request_id]))
            if prior:
                row=self.state.get('anime_'+prior['namespace'],prior['id'])
                return {'action':action,'id':prior['id'],'state':row['state'] if row else 'expired'}
        return None

    def list_jobs(self, args):
        limit=args.get('limit',50); cursor=args.get('cursor','')
        if type(limit) is not int or not 1<=limit<=100: raise BackendError('INVALID_ARGUMENT','Anime job page limit is 1–100')
        if cursor: identifier(cursor)
        rows=[json.loads(r['body']) for r in self.state.select("SELECT body FROM records WHERE namespace='anime_jobs' AND (?='' OR key<?) ORDER BY key DESC LIMIT ?",(cursor,cursor,limit+1))]
        items=[]; size=0
        for row in rows[:limit]:
            value=public(row); length=len(json.dumps(value,ensure_ascii=False).encode())
            if items and size+length>3*1024*1024: break
            items.append(value); size+=length
        return {'items':items,'cursor':items[-1]['id'] if len(rows)>len(items) else None}

    def metadata_capacity(self):
        rows=[r for ns in ('searches','series') for r in self.state.list('anime_'+ns,1000)]
        if len(rows)>=1900: raise BackendError('QUOTA_EXCEEDED','Anime metadata history is full; wait for expired lookups to clear')
        if sum(r['state'] in ('queued','running') for r in rows)>=20: raise BackendError('BUSY','The anime metadata queue is full',True)

    def create_search(self, principal, args):
        query=args['query']; page=args.get('page',1)
        if not isinstance(query,str) or not 1<=len(query.strip())<=200: raise BackendError('INVALID_ARGUMENT','Enter a title of 1–200 characters')
        if type(page) is not int or not 1<=page<=50: raise BackendError('INVALID_ARGUMENT','Search page must be 1–50')
        self.metadata_capacity()
        normalized=' '.join(query.split()).casefold()
        row={'id':new_id(),'state':'queued','query':query.strip(),'page':page,'items':[],'hasNextPage':False,'error':None,
             '_normalized':normalized,'_actor':principal.as_dict(),'_created':time.time(),'_task':None}
        # On-demand cache: an identical recent page is copied instead of asking AniList again.
        for prior in self.state.list('anime_searches',1000):
            if prior['state']=='completed' and prior.get('_normalized')==normalized and prior['page']==page and time.time()-prior['_created']<SEARCH_CACHE_SECONDS:
                row.update(state='completed',items=copy.deepcopy(prior['items']),hasNextPage=prior['hasNextPage'],cached=True)
                break
        return 'searches',row

    def create_lookup(self, principal, args):
        anilist=anilist_id(args['anilistId']); override=args.get('providerSeriesId')
        if override is not None: provider_series(override)
        self.metadata_capacity()
        mapping=self.state.get('anime_mappings',str(anilist))
        return 'series',{'id':new_id(),'revision':1,'state':'queued','anilistId':anilist,'requestedProvider':override,'catalog':None,'description':None,'genres':[],'relations':[],
                         'candidates':[],'match':{'state':'pending','providerSeriesId':None,'evidence':None},'episodes':[],'samples':{'sub':None,'dub':None},'expiresAt':time.time()+LOOKUP_SECONDS,'error':None,
                         '_provider':override or (mapping or {}).get('providerSeriesId'),'_override':override is not None,'_actor':principal.as_dict(),'_created':time.time(),'_task':None}

    def create_plan(self, principal, args):
        series=self.get('series',args['seriesId'])
        if type(args['seriesRevision']) is not int or series['revision']!=args['seriesRevision']: raise BackendError('STALE_REVISION','The series details changed; reload them')
        if series['state']!='completed' or series['expiresAt']<time.time(): raise BackendError('CONFLICT','Look the series up again before downloading')
        provider=series['match']['providerSeriesId']
        if not provider: raise BackendError('CONFLICT','Choose the matching provider series first')
        audio=args['audio']
        if audio not in AUDIO: raise BackendError('INVALID_ARGUMENT','Audio must be sub or dub')
        sample=series['samples'].get(audio)
        if not sample: raise BackendError('INVALID_ARGUMENT','This audio language is not available for this series')
        quality=args['quality']
        if not isinstance(quality,str) or not QUALITY.fullmatch(quality) or quality!='best' and int(quality[:-1]) not in sample['heights']: raise BackendError('INVALID_ARGUMENT','Choose an available quality')
        subtitles=args['subtitles']
        if not isinstance(subtitles,list) or len(set(subtitles))!=len(subtitles) or any(s not in sample['subtitles'] for s in subtitles): raise BackendError('INVALID_ARGUMENT','Choose available subtitle languages')
        subtitles=sorted(subtitles,key=SUBTITLE_LANGUAGES.index)
        wanted=args['episodeIds']
        if not isinstance(wanted,list) or not 1<=len(wanted)<=MAX_EPISODES_PER_JOB or len(set(wanted))!=len(wanted) or any(not isinstance(e,str) or not EPISODE_ID.fullmatch(e) for e in wanted):
            raise BackendError('INVALID_ARGUMENT',f'Select 1–{MAX_EPISODES_PER_JOB} distinct episodes; split longer series into batches')
        inventory={e['id']:e for e in series['episodes']}
        if any(e not in inventory for e in wanted): raise BackendError('INVALID_ARGUMENT','An episode is not in the current provider inventory')
        destination=self.destination(principal,args['destination'])
        title=series['catalog']['title']; anilist=series['anilistId']
        series_folder=folder(destination,title,anilist)
        selected=set(wanted); episodes=[]
        for source in series['episodes']:
            if source['id'] not in selected: continue
            item={'id':source['id'],'number':source['number'],'title':source['title'],'path':target(series_folder,source['number'],audio),'status':'new','reason':None,'entry':None}
            item['name']=item['path'].rpartition('/')[2]
            if not source[audio]: item.update(status='unavailable',reason=f'The provider does not list a {audio} release')
            else: item.update(self.classify(principal,destination,anilist,source['id'],audio,item['path']))
            episodes.append(item)
        counts={s:sum(e['status']==s for e in episodes) for s in ('new','already-present','conflict','unavailable')}
        return 'plans',{'id':new_id(),'revision':1,'state':'ready','seriesId':series['id'],'anilistId':anilist,'title':title,'providerSeriesId':provider,'audio':audio,'quality':quality,
                        'subtitles':subtitles,'destination':destination,'folder':series_folder,'episodes':episodes,'counts':dict(counts,total=len(episodes)),
                        'estimate':'Sizes are unknown until download; full episodes are commonly 300 MB–1.5 GB.','expiresAt':time.time()+PLAN_SECONDS,'_actor':principal.as_dict(),'_created':time.time()}

    def classify(self, principal, destination, anilist, episode_id, audio, logical_path):
        record=self.state.get('anime_library',library_key(destination['driveId'],logical_path))
        if record and (record.get('anilistId'),record.get('episodeId'),record.get('audio'))==(anilist,episode_id,audio):
            try:
                self.service.catalog.stat(principal,record['entry'])
                return {'status':'already-present','reason':None,'entry':record['entry']}
            except BackendError as error:
                if error.code not in ('NOT_FOUND','STALE_REVISION'): raise
        if occupied(self.service,destination['driveId'],logical_path):
            return {'status':'conflict','reason':'An unknown file already exists at this name; it will not be overwritten'}
        return {'status':'new','reason':None}

    def create_job(self, principal, args):
        plan=self.get('plans',args['planId'])
        if type(args['planRevision']) is not int or plan['revision']!=args['planRevision']: raise BackendError('STALE_REVISION','Review the download plan again')
        if plan['state']!='ready' or plan['expiresAt']<time.time(): raise BackendError('CONFLICT','This review expired; review the selection again')
        destination=self.destination(principal,plan['destination'])
        rows=self.state.select("SELECT COUNT(*) AS total FROM records WHERE namespace='anime_jobs'")[0]['total']
        if rows>=HISTORY: raise BackendError('QUOTA_EXCEEDED','Anime download history is full; admission is paused so accepted requests stay replay-safe')
        active=self.active_jobs()
        pending=sum(1 for j in active for e in j['episodes'] if e['state'] not in TERMINAL)
        if len(active)>=ACTIVE_JOB_LIMIT or pending+plan['counts']['new']>QUEUED_EPISODE_LIMIT: raise BackendError('QUOTA_EXCEEDED','The anime queue is full; wait for current downloads to finish')
        episodes=[]
        for item in plan['episodes']:
            state={'new':'queued','already-present':'already-present','conflict':'conflict','unavailable':'unavailable'}[item['status']]
            episodes.append({'id':item['id'],'number':item['number'],'title':item['title'],'name':item['name'],'state':state,'stage':None,'progress':None,'attempt':0,
                             'error':item['reason'],'entry':item.get('entry'),'height':None,'sizeBytes':None,'_path':item['path'],'_task':None,'_transfer':None})
        job={'id':new_id(),'revision':1,'state':'queued','title':plan['title'],'anilistId':plan['anilistId'],'providerSeriesId':plan['providerSeriesId'],'audio':plan['audio'],
             'quality':plan['quality'],'subtitles':plan['subtitles'],'destination':destination,'folder':plan['folder'],'createdAt':time.time(),'episodes':episodes,'summary':None,
             '_actor':principal.as_dict(),'_planId':plan['id']}
        self.aggregate(job)
        return 'jobs',job

    # Controls --------------------------------------------------------------
    def control(self, principal, args):
        self.enabled(); job=self.get('jobs',args['id'])
        if type(args['revision']) is not int or job['revision']!=args['revision']: raise BackendError('STALE_REVISION','The anime job changed; refresh and try again')
        action=args['action']
        if action not in ('pause','resume','cancel','retry-failed'): raise BackendError('INVALID_ARGUMENT','Unknown anime job action')
        if job['state'] in ('completed','canceled'): raise BackendError('CONFLICT','This anime job has finished')
        if action=='resume' and job['state']!='paused': raise BackendError('CONFLICT','Only paused jobs can resume')
        if action=='retry-failed' and not any(e['state'] in ('failed','conflict','unavailable','blocked-auth') for e in job['episodes']): raise BackendError('CONFLICT','No failed episodes to retry')
        for episode in job['episodes']:
            if episode['state'] in SUCCESS or episode['state']=='importing': continue
            # A finalized transfer wins over a late control, including after a crash.
            if episode.get('_transfer'):
                receipt=self.service.transfers.query(principal,episode['_transfer'])
                if receipt['state']=='completed':
                    episode.update(state='completed',stage=None,progress=1,error=None,entry=self.imports.record(job,episode,receipt['entry'])); continue
            if action in ('pause','cancel'):
                if episode['state'] in TERMINAL: continue
                if episode.get('_task'): self.worker_call(op='cancel',taskId=episode['_task'])
                if action=='cancel':
                    if episode.get('_transfer'): self.service.transfers.cancel(principal,episode['_transfer'],discard=True); episode['_transfer']=None
                    if episode.get('_task'): self.worker_call(op='cleanup',taskId=episode['_task']); episode['_task']=None
                    episode.pop('_artifact',None); episode.update(state='canceled',stage=None,error=None)
                elif not episode.get('_artifact'):
                    # Paused episodes keep their checkpoint; resume continues from verified segments.
                    # A verified artifact stays ready and is not published while the job is paused.
                    episode.update(state='paused',stage=None)
            elif action=='resume':
                if episode['state']=='paused': episode.update(state='queued',error=None,_retryAt=0)
            elif episode['state'] in ('failed','conflict','unavailable','blocked-auth','waiting-for-drive'):
                if episode.get('_task'): self.worker_call(op='cleanup',taskId=episode['_task'])
                if episode.get('_transfer'): self.service.transfers.cancel(principal,episode['_transfer'],discard=True)
                episode.pop('_artifact',None)
                episode.update(state='queued',stage=None,progress=None,error=None,_task=None,_transfer=None,_retryAt=0,_automatic=0)
        # Retrying inside a paused job requeues failures but leaves the job paused until Resume.
        job['state']={'pause':'paused','cancel':'canceled'}.get(action,'paused' if action=='retry-failed' and job['state']=='paused' else 'queued')
        if action!='pause': self.aggregate(job)
        self.put('jobs',job); self.state.audit(principal.id,'anime.jobs.control',action,job['id'])
        return public(job)

    def worker_call(self, **args):
        try: return self.worker.call(**args)
        except BackendError as error:
            # Cancellation of an unreachable worker is completed by its lease and process-group cleanup.
            if args.get('op') in ('cancel','cleanup') and error.code=='UNAVAILABLE': return {'state':'absent'}
            raise

    @staticmethod
    def aggregate(job):
        states=[e['state'] for e in job['episodes']]
        summary={s:states.count(s) for s in set(states)}
        job['summary']={'total':len(states),'completed':summary.get('completed',0)+summary.get('already-present',0),'failed':sum(summary.get(s,0) for s in ('failed','conflict','blocked-auth')),
                        'unavailable':summary.get('unavailable',0),'canceled':summary.get('canceled',0),'pending':sum(1 for s in states if s not in TERMINAL)}
        # A job is successful only when every episode has a successful terminal outcome.
        if all(s in SUCCESS for s in states): job['state']='completed'
        elif all(s in TERMINAL for s in states):
            job['state']='partial' if any(s in SUCCESS for s in states) else 'canceled' if all(s=='canceled' for s in states) else 'failed'
        elif job['state']=='paused': pass
        elif any(s in ACTIVE+('ready','importing') for s in states): job['state']='running'
        elif 'waiting-for-drive' in states: job['state']='waiting-for-drive'
        else: job['state']='queued'

    # Coordinator -----------------------------------------------------------
    def tick(self):
        try:
            self.worker.call(op='status'); self.health={'available':True,'at':time.time()}
        except BackendError:
            self.health={'available':False,'at':time.time()}; return
        self.maintenance()
        self.metadata()
        self.schedule()

    def metadata(self):
        """One worker metadata task at a time, oldest first."""
        rows=sorted((r for ns in ('searches','series') for r in self.state.list('anime_'+ns,1000) if r['state'] in ('queued','running')),key=lambda r:r['id'])
        for row in rows:
            namespace='searches' if 'query' in row else 'series'
            try:
                self.principal(row['_actor'])
                if not row['_task']:
                    row['_task']=uuid.uuid4().hex; row['state']='running'; self.state.put('anime_'+namespace,row['id'],row)
                    payload={'query':row['query'],'page':row['page']} if namespace=='searches' else {'anilistId':row['anilistId'],'providerSeriesId':row['_provider']}
                    result=self.worker.call(op='start',taskId=row['_task'],action='search' if namespace=='searches' else 'lookup',payload=payload)
                else:
                    result=self.worker.call(op='poll',taskId=row['_task'])
                    if result['state']=='absent':
                        row.update(_task=None,state='queued'); self.state.put('anime_'+namespace,row['id'],row); return
                if result['state']=='running': return
                if result['state']=='completed':
                    if namespace=='searches': self.finish_search(row,result['result'])
                    else: self.finish_lookup(row,result['result'])
                else:
                    code=result.get('error','SOURCE_UNAVAILABLE')
                    row.update(state='failed',error='AniList is rate limiting searches; try again in a minute' if code=='RATE_LIMITED' else 'Anime metadata is unavailable right now. Try again shortly.')
                    self.state.put('anime_'+namespace,row['id'],row)
                self.worker_call(op='cleanup',taskId=row['_task'])
                return
            except (KeyError,TypeError,ValueError):
                row.update(state='failed',error='The anime provider returned incompatible metadata'); self.state.put('anime_'+namespace,row['id'],row)
            except BackendError as error:
                if error.code in ('UNAVAILABLE','BUSY'): return
                row.update(state='failed',error=error.message[:500]); self.state.put('anime_'+namespace,row['id'],row)

    def finish_search(self, row, data):
        items,more,covers=search_result(data)
        for anilist,data_url in covers.items(): self.state.put('anime_covers',str(anilist),{'dataUrl':data_url,'at':time.time()})
        row.update(state='completed',items=items,hasNextPage=more,error=None); self.state.put('anime_searches',row['id'],row)

    def finish_lookup(self, row, data):
        value,cover=lookup_result(data,row['anilistId'])
        if cover: self.state.put('anime_covers',str(row['anilistId']),{'dataUrl':cover,'at':time.time()})
        match=value['match']
        if row['_override']:
            if match!=row['_provider']: raise BackendError('INVALID_RESPONSE','The selected provider series could not be loaded')
            self.state.put('anime_mappings',str(row['anilistId']),{'providerSeriesId':match,'at':time.time(),'evidence':'owner'})
            state='selected'
        elif row['_provider'] and match==row['_provider']: state='selected'
        else:
            # A remembered choice the provider no longer serves falls back to automatic identity matching.
            if row['_provider']: self.state.delete('anime_mappings',str(row['anilistId']))
            state='matched' if match else 'ambiguous' if value['candidates'] else 'none'
        evidence=next((c['evidence'] for c in value['candidates'] if c['providerSeriesId']==match),None)
        row.update(value,state='completed',error=None,match={'state':state,'providerSeriesId':match,'evidence':evidence},expiresAt=time.time()+LOOKUP_SECONDS)
        self.put('series',row)

    def schedule(self):
        jobs=self.active_jobs()
        busy=any(e['state'] in ACTIVE for j in jobs for e in j['episodes'])
        claimed={e['_path']:j['id'] for j in jobs for e in j['episodes'] if e['state'] in ACTIVE+('ready','importing')}
        importing=bool(self.import_thread and self.import_thread.is_alive())
        for job in jobs:
            if self.stop.is_set(): break
            if job['state']=='paused':
                self.settle_paused(job); continue
            before=copy.deepcopy(job)
            try: principal=self.principal(job['_actor'])
            except BackendError:
                for episode in job['episodes']:
                    if episode['state'] not in TERMINAL and episode['state']!='importing':
                        if episode.get('_task'): self.worker_call(op='cancel',taskId=episode['_task'])
                        episode.update(state='blocked-auth',error='The requesting credential was revoked')
                self.aggregate(job); self.put('jobs',job); continue
            for episode in job['episodes']:
                if self.stop.is_set(): break
                try:
                    busy=self.advance(principal,job,episode,busy,claimed)
                    if episode['state']=='ready' and not importing and not self.stop.is_set():
                        episode['state']='importing'; importing=True
                        self.put('jobs',job); before=copy.deepcopy(job)
                        self.import_thread=threading.Thread(target=self.publish,args=(copy.deepcopy(job),copy.deepcopy(episode)),name='archive-anime-import',daemon=True)
                        self.import_thread.start()
                    elif episode['state']=='importing' and not importing:
                        # The previous import thread stopped (shutdown or crash); publication resumes from its receipt.
                        episode['state']='ready'
                except BackendError as error:
                    if error.code in ('BUSY','UNAVAILABLE'): continue
                    if error.code in ('DRIVE_UNAVAILABLE','VOLUME_MISMATCH','INSUFFICIENT_SPACE','READ_ONLY'): episode.update(state='waiting-for-drive',error='Waiting for the selected drive or free space')
                    elif error.code in ('PERMISSION_DENIED','PERMISSION_REVOKED'): episode.update(state='blocked-auth',error='Permission is no longer available')
                    else: episode.update(state='conflict' if error.code=='CONFLICT' else 'failed',stage=None,error=error.message[:500])
            self.aggregate(job)
            if job!=before: self.put('jobs',job)

    def settle_paused(self, job):
        changed=False
        for episode in job['episodes']:
            if episode['state'] in ACTIVE and episode.get('_task'):
                self.worker_call(op='cancel',taskId=episode['_task']); episode.update(state='paused',stage=None); changed=True
        if changed: self.put('jobs',job)

    def advance(self, principal, job, episode, busy, claimed):
        state=episode['state']
        if state in ('queued','waiting-for-drive'):
            drive=job['destination']['driveId']; self.service.catalog.filesystem(drive)
            if episode.get('_artifact'): episode.update(state='ready',error=None); return busy
            existing=self.imports.present(principal,job,episode)
            if existing:
                if episode.get('_task'): self.worker_call(op='cleanup',taskId=episode['_task']); episode['_task']=None
                episode.update(state='already-present',entry=existing,error=None,stage=None,progress=1); return busy
            if claimed.get(episode['_path'],job['id'])!=job['id'] or busy or episode.get('_retryAt',0)>time.time(): return busy
            if not episode.get('_task'):
                episode['_task']=uuid.uuid4().hex; episode['attempt']+=1; self.put('jobs',job)
            result=self.worker.call(op='start',taskId=episode['_task'],action='download',payload=self.download_payload(job,episode))
            episode.update(state='resolving',stage='resolving',error=None,_epoch=result.get('epoch'))
            claimed[episode['_path']]=job['id']; busy=True
            state='resolving'
        if state in ACTIVE:
            result=self.worker.call(op='poll',taskId=episode['_task'])
            if result['state']=='absent' or result.get('epoch')!=episode.get('_epoch'):
                # The worker restarted. The same task identity resumes its retained checkpoint.
                episode.update(state='queued',stage=None); return busy
            if result['state']=='completed':
                episode['_artifact']=artifact(result['result'],job['audio'],job['quality'],job['subtitles'])
                episode.update(state='ready',stage=None,progress=1,height=episode['_artifact']['height'],sizeBytes=episode['_artifact']['sizeBytes'],error=None)
                return False
            if result['state'] in ('failed','canceled'):
                code=result.get('error','SOURCE_UNAVAILABLE')
                if code in UNAVAILABLE:
                    self.worker_call(op='cleanup',taskId=episode['_task']); episode.update(state='unavailable',stage=None,_task=None,error=UNAVAILABLE[code]); return False
                automatic=episode.get('_automatic',0)+1; episode['_automatic']=automatic
                if code in RETRYABLE and automatic<3:
                    # Keep the task and its checkpoint; the next start resumes verified segments.
                    episode.update(state='queued',stage=None,_retryAt=time.time()+(10 if automatic==1 else 60),error='Source unavailable · retry scheduled')
                else:
                    self.worker_call(op='cleanup',taskId=episode['_task'])
                    episode.update(state='failed',stage=None,_task=None,error=FAILURES.get(code,'The episode could not be downloaded after three attempts' if code in RETRYABLE else 'Source unavailable or media verification failed ('+str(code)[:40]+')'))
                return False
            stage=result.get('stage') if result.get('stage') in ACTIVE else 'downloading'; fraction=result.get('progress')
            previous=episode.get('progress')
            # Bounded progress cadence: persist stage changes and whole-percent movement only.
            if episode['state']!=stage or fraction is None or previous is None or abs(fraction-previous)>=0.01:
                episode.update(state=stage,stage=stage,progress=fraction)
        return busy

    @staticmethod
    def download_payload(job, episode):
        return {'providerSeriesId':job['providerSeriesId'],'episodeId':episode['id'],'number':episode['number'],'audio':job['audio'],'quality':job['quality'],'subtitles':job['subtitles']}

    def publish(self, job, episode):
        def save(fields):
            with self.lock:
                self.update_episode(job['id'],episode['id'],fields)
        try:
            principal=self.principal(job['_actor'])
            entry=self.imports.publish(principal,job,episode,save)
            save({'state':'completed','entry':entry,'progress':1,'error':None,'stage':None})
            try: self.worker_call(op='cleanup',taskId=episode['_task'])
            except BackendError: pass
        except BackendError as error:
            if error.code=='BUSY': save({'state':'ready'})
            elif error.code in ('DRIVE_UNAVAILABLE','VOLUME_MISMATCH','INSUFFICIENT_SPACE','READ_ONLY'): save({'state':'waiting-for-drive','error':'Waiting for the selected drive or free space'})
            elif error.code in ('PERMISSION_DENIED','PERMISSION_REVOKED'): save({'state':'blocked-auth','error':'Permission is no longer available'})
            else: save({'state':'conflict' if error.code=='CONFLICT' else 'failed','error':error.message[:500]})
        except Exception:
            save({'state':'failed','error':'Verified publication failed; retry after checking the destination'})

    def update_episode(self, job_id, episode_id, fields):
        """Merge one episode's fields into the current durable job; the coordinator may have changed others."""
        job=self.get('jobs',job_id)
        next(e for e in job['episodes'] if e['id']==episode_id).update(fields)
        self.aggregate(job); self.put('jobs',job)

    def maintenance(self):
        now=time.time()
        if now-getattr(self,'_maintained',0)<60: return
        self._maintained=now
        referenced={j['_planId'] for j in self.active_jobs()}
        for namespace,age in (('searches',86400),('series',86400),('plans',86400)):
            for row in self.state.list('anime_'+namespace,1000):
                if row.get('state') in ('queued','running'): continue
                if row.get('_task'):
                    self.worker_call(op='cleanup',taskId=row['_task']); row['_task']=None; self.state.put('anime_'+namespace,row['id'],row)
                if now-row['_created']>age and row['id'] not in referenced:
                    with self.state.transaction():
                        self.state.delete('anime_'+namespace,row['id'])
                        if row.get('_requestKey'): self.state.delete('anime_requests',row['_requestKey'])
        with self.state.transaction() as connection:
            connection.execute("DELETE FROM records WHERE namespace='anime_covers' AND json_extract(body,'$.at')<?",(now-7*86400,))
