"""Shared authenticated application boundary used by desktop, API and MCP."""
import threading
import time
import uuid
from .contracts import BackendError, CAPABILITIES, validate_call, translate_error, object_fields, entry_ref
from .config import parse_config
from .persistence import State
from .security import Auth, Confirmations
from .jobs import Jobs
from .volumes import VolumeConfig, VolumeRegistry
from .filesystem import ConfinedFilesystem
from .catalog import Catalog, timestamp
from .operations import Operations
from .transfers import Transfers
from .observability import Observability


class Service:
    def __init__(self,config,state=None,registry=None,*,fault=lambda _:None):
        self.phone_transfer=None; self.skills=None; self.vault=None; self.radio=None; self.music=None; self.video=None; self.anime=None; self.git_backup=None
        self.config=config; self.ready=False; self.server=None; self.mobile_server=None; self.mobile_boundary=None; self.publisher=None; self.pool=None
        self.state=state; self.registry=registry or VolumeRegistry(); self.volume_errors={}; self.children=[]
        import json,sys
        self.observability=Observability(sink=lambda row:sys.stderr.write(json.dumps(dict(row,component='control',version='0.2.0',at=timestamp(row['at'])))+'\n'))
        self._close_lock=threading.Lock()
        self._telemetry_stop=threading.Event(); self._telemetry_thread=None; self._health_status={'state':'not-configured'}; self._started=time.monotonic()
        if state is None:
            if config.state_path is None:
                self.auth=self.jobs=self.catalog=self.operations=self.transfers=None
                return
            self.state=State(config.state_path)
        self.auth=Auth(self.state); self.confirmations=Confirmations(self.state,self.auth)
        self.jobs=Jobs(self.state,self.auth,max_active=config.max_workers,max_queued=config.max_pending_jobs)
        self.filesystems={v.volume_id:ConfinedFilesystem(v) for v in self.registry.list()}
        self.catalog=Catalog(self.state,self.auth,self.filesystems)
        self.operations=Operations(self.state,self.auth,self.jobs,self.catalog,self.confirmations,fault=fault,min_free_bytes=config.min_free_bytes)
        self.transfers=Transfers(self.state,self.auth,self.jobs,self.catalog,self.operations,fault=fault,min_free_bytes=config.min_free_bytes)
        from .radio_service import RadioService
        self.radio=RadioService(self,config.radio)
        from .phone_transfer_service import PhoneService
        self.phone_transfer=PhoneService(self,config.phone_transfer)
        from .skills_service import SkillsService
        self.skills=SkillsService(self,config.skills)
        from .vault_service import VaultService
        self.vault=VaultService(self,config.vault)
        from .music_service import MusicService
        self.music=MusicService(self,config.music)
        from .video_service import VideoService
        self.video=VideoService(self,config.video)
        from .anime_service import AnimeService
        self.anime=AnimeService(self,config.anime)
        from .git_service import GitService
        self.git_backup=GitService(self,config.git_backup)

    @classmethod
    def from_config(cls,config,*,state=None):
        registry=VolumeRegistry(); errors={}; children=[]
        # One missing data disk must not prevent the other verified disk's startup.
        for value in config.volumes:
            try:
                child=VolumeRegistry([VolumeConfig(value['id'],value['label'],value['rootPath'],value['mountPoint'],value['filesystemUuid'],value['expectedFilesystem'],tuple(value['expectedDevice']),value['expectedMountSource'],value.get('expectedMountId'),value.get('expectedMountRoot','/'))])
                children.append(child); registry._volumes[value['id']]=child.get(value['id'])
            except BackendError as error: errors[value['id']]=error.code
        service=cls(config,state=state,registry=registry); service.volume_errors=errors; service.children=children
        return service

    def authenticate(self,token):
        if self.auth is None: raise BackendError('AUTH_REQUIRED','Owner enrollment and state configuration are required')
        principal=self.auth.authenticate(token)
        session=self.state.get('sessions',principal.credential_id)
        if session is None or time.time()-session['lastSeen']>=5:
            self.state.put('sessions',principal.credential_id,{'id':principal.credential_id,'actorId':principal.id,'lastSeen':time.time(),'expiresAt':time.time()+300})
        return principal

    def start(self):
        if self.state is not None:
            self.state.acquire_service_lock()
            self.jobs.recover()
            from .recovery import reconcile
            reconcile(self,allow_client_recovery=True)
            if self.config.mobile:
                from .mobile_boundary import MobileBoundary
                from .mobile_http import create_server as create_mobile_server
                self.mobile_boundary=MobileBoundary(self,self.config.mobile)
                self.mobile_server=create_mobile_server(self.mobile_boundary,self.config.mobile['listen']['host'],self.config.mobile['listen']['port'])
                threading.Thread(target=self.mobile_server.serve_forever,daemon=True,name='archive-mobile-http').start()
            if self.config.listen:
                from .http_adapter import create_server
                self.server=create_server(self,self.config.listen['host'],self.config.listen['port'])
                threading.Thread(target=self.server.serve_forever,daemon=True,name='archive-http').start()
            from .workers import WorkerPool
            self.pool=WorkerPool(self); self.pool.start()
            self.phone_transfer.start()
            self.radio.start()
            self.music.start()
            self.video.start()
            self.anime.start()
            self.git_backup.start()
            from .telemetry import Publisher
            health=dict(self.config.telemetry,**self._settings()['health'])
            self.publisher=Publisher(health)
            self._telemetry_thread=threading.Thread(target=self._collect_health,daemon=True,name='archive-health-collector'); self._telemetry_thread.start()
        elif self.config.listen or self.config.mobile or self.config.volumes:
            raise BackendError('STATE_UNAVAILABLE','Explicit service-state path and enrollment are required')
        self.ready=True

    def close(self):
        with self._close_lock:
            self.ready=False
            if self.server: self.server.shutdown(); self.server.server_close(); self.server=None
            if self.mobile_server: self.mobile_server.shutdown(); self.mobile_server.server_close(); self.mobile_server=None
            if self.mobile_boundary: self.mobile_boundary.close(); self.mobile_boundary=None
            if self.phone_transfer: self.phone_transfer.close()
            if self.skills: self.skills.close()
            if self.vault: self.vault.close()
            if self.radio: self.radio.close()
            if self.git_backup: self.git_backup.close()
            if self.anime: self.anime.close()
            if self.video: self.video.close()
            if self.music: self.music.close()
            if self.pool: self.pool.close(); self.pool=None
            self._telemetry_stop.set()
            if self._telemetry_thread: self._telemetry_thread.join(timeout=6)
            if self.publisher: self.publisher.close(); self.publisher=None
            self.observability.close()
            self.registry.close()
            if self.state: self.state.close(); self.state=None

    def _settings(self):
        return self.state.get('configuration','current',{'revision':0,'driveLabels':{v.volume_id:v.label for v in self.registry.list()} or {'files':'File Storage','backups':'Memory Harness Backups'},'transferConcurrency':self.config.max_workers,'health':{k:self.config.telemetry[k] for k in ('enabled','endpoint','intervalSeconds')},'appearance':{'reducedMotion':False,'compactRows':False},'restartRequired':False,'simulated':False})

    def _save_settings(self,principal,draft):
        self.auth.owner(principal)
        object_fields(draft,('revision','driveLabels','transferConcurrency','health','appearance','restartRequired','simulated'))
        if draft['simulated'] is not False or type(draft['transferConcurrency']) is not int or not 1<=draft['transferConcurrency']<=2: raise BackendError('INVALID_ARGUMENT','Invalid live transfer settings')
        object_fields(draft['health'],('enabled','endpoint','intervalSeconds'))
        parse_config({'telemetry':draft['health']})
        object_fields(draft['appearance'],('reducedMotion','compactRows'))
        if any(type(v) is not bool for v in draft['appearance'].values()) or not isinstance(draft['driveLabels'],dict) or len(draft['driveLabels'])>2 or any(not isinstance(v,str) or not 1<=len(v)<=120 for v in draft['driveLabels'].values()): raise BackendError('INVALID_ARGUMENT','Invalid appearance or drive labels')
        with self.state.transaction():
            old=self._settings()
            if draft['revision']!=old['revision']: raise BackendError('STALE_REVISION','Settings changed; reload saved state')
            saved=dict(draft,revision=old['revision']+1,restartRequired=draft['transferConcurrency']!=self.config.max_workers or draft['health']!=old['health'])
            self.state.put('configuration','current',saved); self.state.audit(principal.id,'configuration.save','saved')
            return saved

    def drives(self,principal):
        self.auth.refresh(principal); settings=self._settings(); result=[]
        configured={v['id']:v for v in self.config.volumes}
        for volume in self.registry.list(): configured.setdefault(volume.volume_id,{'id':volume.volume_id,'label':volume.label,'role':'harness-backups' if 'backup' in volume.volume_id else 'files'})
        if not configured: configured={'files':{'id':'files','label':'File Storage','role':'files'},'backups':{'id':'backups','label':'Memory Harness Backups','role':'harness-backups'}}
        for id,cfg in configured.items():
            if principal.kind!='owner' and not any(d==id for d,_ in principal.roots): continue
            state='unconfigured' if id not in self.filesystems and id not in self.volume_errors else 'offline'; total=free=None; readable=writable=trash=purge=False
            try:
                fs=self.catalog.filesystem(id); capacity=fs.volume.capacity(); total=str(capacity.get('totalBytes',capacity.get('total'))); free=str(capacity.get('availableBytes',capacity.get('available')))
                state='read-only' if capacity.get('read_only',capacity.get('readOnly',False)) else 'online'
                readable=principal.kind=='owner' or 'read' in principal.scopes
                writable=state=='online' and (principal.kind=='owner' or 'write' in principal.scopes)
                trash=writable and (principal.kind=='owner' or 'trash' in principal.scopes); purge=writable and (principal.kind=='owner' or 'purge' in principal.scopes)
            except BackendError: pass
            result.append({'id':id,'role':cfg['role'],'label':settings['driveLabels'].get(id,cfg['label']),'state':state,'configured':id in self.filesystems or id in self.volume_errors,'capacityBytes':total,'freeBytes':free,'temperatureC':None,'capabilities':{'browse':readable,'write':writable,'trash':trash,'purge':purge},'revision':self.filesystems[id].volume.generation if id in self.filesystems else 0,'simulated':False})
        return result

    def status(self):
        health=self._health_status
        return {'schemaVersion':1,'backend':'connected' if self.ready else 'unavailable','freshnessAt':timestamp(),'api':'ready' if self.server else 'not-configured','mcp':'ready' if self.server else 'not-configured','health':str(health.get('state','not-configured')).lower().replace(' ','-'),'authenticatedSessions':sum(1 for s in self.state.list('sessions',limit=10000) if s['expiresAt']>time.time()) if self.state else 0,'stale':False,'simulated':False}

    def _agents(self,principal):
        return [{'id':a['id'],'label':a['label'],'scopes':a['scopes'],'allowedRoots':a['roots'],'createdAt':timestamp(a['createdAt']),'lastUsedAt':None,'revoked':a['revoked'],'revision':a['scopeVersion'],'credentialDisplay':'Stored securely outside renderer'} for a in self.auth.list(principal) if a['kind']=='agent']

    def call(self,token,request):
        try: return self._call(token,request)
        except Exception as exc:
            error=translate_error(exc)
            self.observability.record(error.code,level='warning',correlation_id=error.correlation_id)
            if error.code=='STATE_UNAVAILABLE': self.ready=False
            if error.code in ('PERMISSION_DENIED','PERMISSION_REVOKED','AUTH_REQUIRED') and self.state:
                actor=error.details.get('actor','unresolved')
                try:
                    with self.state.transaction():
                        denied=self.state.get('denials',actor,{'last':0,'count':0}); denied['count']+=1
                        if time.time()-denied['last']>=60:
                            self.state.audit(actor,'authorization.denied',error.code,correlation_id=error.correlation_id); denied['last']=time.time()
                        self.state.put('denials',actor,denied)
                except BackendError: self.ready=False
            raise error from None

    def _call(self,token,request):
        principal=self.authenticate(token); cap,args=validate_call(request)
        if cap.owner_only: self.auth.owner(principal)
        capability=cap.name
        if capability.startswith('phone_transfer.'): return self.phone_transfer.call(principal,capability,args)
        if capability.startswith('skills.'): return self.skills.call(principal,capability,args)
        if capability.startswith('vault.'): return self.vault.call(principal,capability,args)
        if capability.startswith('radio.'): return self.radio.call(principal,capability,args)
        if capability.startswith('gitBackup.'): return self.git_backup.call(principal,capability,args)
        if capability.startswith('anime.'): return self.anime.call(principal,capability,args)
        if capability.startswith('video.'): return self.video.call(principal,capability,args)
        if capability.startswith('music.'): return self.music.call(principal,capability,args)
        if capability=='drives.list': return self.drives(principal)
        if capability=='status.get': self.auth.refresh(principal); return self.status()
        if capability=='files.list': return self.catalog.list(principal,args['query'])
        if capability=='files.stat': return self.catalog.stat(principal,args['ref'])
        if capability=='operations.submit': return self.operations.submit(principal,args['request'])
        if capability=='operations.reconcile':
            prior=self.state.reconcile(principal.id,'upload',args['requestId'])
            if prior:
                parent=self.state.get('jobs',prior['jobIds'][0])
                if parent and parent['_payload'].get('folderUpload'):
                    self.jobs.get(principal,parent['id']); return prior
            rows=[]
            for record in self.state.list('jobs',limit=10000):
                if record['actor']['id']==principal.id and (record['requestId']==args['requestId'] or record['_payload'].get('parentRequestId')==args['requestId']):
                    self.jobs.get(principal,record['id']); rows.append(record['id'])
            return {'status':'accepted','requestId':args['requestId'],'jobIds':rows,'simulated':False} if rows else None
        if capability=='jobs.snapshot':
            result=self.jobs.snapshot(principal)
            for job in result['jobs']:
                if job['state']=='failed' and job['operation']!='purge' and (job.get('error') or {}).get('retryable'): job['capabilities']['retry']=True
            return result
        if capability=='jobs.get': return self.jobs.get(principal,args['id'])
        if capability=='jobs.events': return self.jobs.events(principal,args['after'],args.get('limit',200))
        if capability=='jobs.control':
            if args['action']=='retry': return self.retry(principal,args['id'])
            result=self.jobs.control(principal,args['id'],args['action'])
            return result
        if capability=='jobs.hide':
            if not isinstance(args['ids'],list) or len(args['ids'])>1000: raise BackendError('INVALID_ARGUMENT','Invalid visible-history selection')
            for id in args['ids']: self.jobs.get(principal,id)
            self.state.put('hidden',principal.id,args['ids']); return {'hidden':args['ids']}
        if capability=='trash.list': return self.operations.trash_list(principal,args['driveId'])
        if capability=='trash.confirm':
            targets=args['targets']; self.auth.authorize(principal,'purge')
            if args.get('empty'): targets=[t['entry'] for t in self.operations.trash_list(principal,args['driveId'])]
            refs=[{k:t[k] for k in ('id','driveId','path','revision')} for t in targets]
            if not refs: raise BackendError('INVALID_ARGUMENT','No exact Trash targets selected')
            for ref in refs:
                entry_ref(ref); self.auth.authorize(principal,'purge',ref['driveId'],ref['path'])
                item=self.state.get('trash',ref['id'])
                if not item or item['revision']!=ref['revision'] or item['drive']!=args['driveId']: raise BackendError('STALE_REVISION','Trash targets changed')
            generation={args['driveId']:self.catalog.filesystem(args['driveId']).volume.generation}
            grant=self.confirmations.issue(principal,'purge',refs,generation)
            return {**grant,'targets':refs,'driveId':args['driveId']}
        if capability=='configuration.get': return self._settings()
        if capability=='configuration.save': return self._save_settings(principal,args['draft'])
        if capability=='configuration.testHealth': return self.test_health()
        if capability=='agents.list': return self._agents(principal)
        if capability=='agents.register':
            p=self.auth.register(principal,args['label'],args['secret'],args['scopes'],args['allowedRoots']); return {'id':p.id,'credentialId':p.credential_id}
        if capability in ('agents.revoke','agents.updateScopes','agents.rotate'):
            current=next((a for a in self._agents(principal) if a['id']==args['id']),None)
            if current is None: raise BackendError('NOT_FOUND','Agent not found')
            if 'revision' in args and args['revision']!=current['revision']: raise BackendError('STALE_REVISION','Agent configuration changed')
            if capability=='agents.revoke': self.auth.revoke(principal,args['id'])
            elif capability=='agents.updateScopes': self.auth.update_scopes(principal,args['id'],args['scopes'],args['allowedRoots'])
            else: self.auth.rotate(principal,args['id'],args['secret'])
            return next(a for a in self._agents(principal) if a['id']==args['id'])
        if capability=='sessions.list': return [{'id':s['id'],'actorId':s['actorId'],'lastSeenAt':timestamp(s['lastSeen']),'expiresAt':timestamp(s['expiresAt'])} for s in self.state.list('sessions',limit=10000) if s['expiresAt']>time.time()]
        if capability in ('audit.list','diagnostics.list'):
            fresh=self.auth.refresh(principal)
            with self.state.transaction() as c:
                rows=c.execute('SELECT * FROM audit WHERE sequence>? '+('' if fresh.kind=='owner' else 'AND actor=? ')+'ORDER BY sequence DESC LIMIT ?',((args.get('after',0),) if fresh.kind=='owner' else (args.get('after',0),fresh.id))+(min(args.get('limit',200),500),)).fetchall()
            if capability=='audit.list': return [dict(r) for r in rows]
            return [{'id':str(r['sequence']),'at':timestamp(r['at']),'level':'info','code':'SERVICE_EVENT','message':r['operation']+': '+r['outcome'],'correlationId':r['correlation_id'] or str(r['sequence']),'simulated':False} for r in rows]
        if capability.startswith('transfers.folder'):
            from . import folder_uploads
            if capability=='transfers.folderBegin':return folder_uploads.begin(self.transfers,principal,args)
            if capability=='transfers.folderStatus':return folder_uploads.status(self.transfers,principal,args['folderId'])
            if capability=='transfers.folderAck':return folder_uploads.acknowledge(self.transfers,principal,args['folderId'],args['transferId'])
            if capability=='transfers.folderFinish':return folder_uploads.finish(self.transfers,principal,args['folderId'])
            if capability=='transfers.folderError':return folder_uploads.error(self.transfers,principal,args['folderId'],args['code'])
        if capability=='transfers.initiate': return self.transfers.initiate(principal,args)
        if capability=='transfers.batchInitiate': return self.transfers.batch_initiate(principal,args['requestId'],args['files'])
        if capability=='transfers.resumeInfo': return self.transfers.resume_info(principal,args['jobId'])
        if capability=='transfers.restartDownload': return self.transfers.restart_download(principal,args['transferId'])
        if capability=='transfers.query': return self.transfers.query(principal,args['transferId'])
        if capability=='transfers.finalize': return self.transfers.finalize(principal,args['transferId'])
        if capability=='transfers.cancel': return self.transfers.cancel(principal,args['transferId'])
        if capability=='transfers.discard': return self.transfers.cancel(principal,args['transferId'],True)
        if capability=='transfers.download': return self.transfers.download(principal,args['ref'],args['requestId'],args.get('parentRequestId'))
        if capability=='transfers.batchDownload': return self.transfers.batch_download(principal,args['requestId'],args['refs'])
        if capability=='transfers.downloadProgress': return self.transfers.download_progress(principal,args['transferId'],args['offset'],args['complete'])
        if capability=='transfers.clientError':
            record=self.transfers._session(principal,args['transferId'],'read' if self.state.get('transfers',args['transferId'],{}).get('direction')=='download' else 'write')
            if args['code'] not in ('SOURCE_CHANGED','INTEGRITY_FAILED','FULL_DISK','CONFLICT','INTERRUPTED'): raise BackendError('INVALID_ARGUMENT','Invalid client error code')
            with self.state.transaction():
                self.state.audit(principal.id,'transfer.client_error',args['code'],record['jobId'])
                job=self.state.get('jobs',record['jobId'])
                job['error']=BackendError(args['code'],'Native transfer stopped; partial bytes are retained',args['code'] not in ('SOURCE_CHANGED','INTEGRITY_FAILED')).envelope(record['jobId'])
                self.jobs._save(job,'client-error')
            return {'reported':True,'state':job['state']}
        raise BackendError('INVALID_ARGUMENT','Capability handler unavailable')

    def _health_snapshot(self):
        snapshot={'uptimeSeconds':int(time.monotonic()-self._started),'apiState':'Ready' if self.server else 'Disabled','mcpState':'Ready' if self.server else 'Disabled','drives':[]}
        with self.state.transaction() as c:
            snapshot['activeJobs']=c.execute("SELECT COUNT(*) FROM records WHERE namespace='jobs' AND json_extract(body,'$.state') IN ('running','verifying','committing')").fetchone()[0]
            snapshot['queuedJobs']=c.execute("SELECT COUNT(*) FROM records WHERE namespace='jobs' AND json_extract(body,'$.state')='queued'").fetchone()[0]
        volume_ids=list(dict.fromkeys([v['id'] for v in self.config.volumes]+list(self.filesystems)))
        for volume_id in volume_ids:
            fs=self.filesystems.get(volume_id)
            if fs is None:
                snapshot['drives'].append({'online':False}); continue
            try:
                cap=fs.volume.capacity(); snapshot['drives'].append({'online':True,'capacityBytes':cap['total'],'freeBytes':cap['available'],'temperatureCelsius':None})
            except BackendError: snapshot['drives'].append({'online':False})
        return snapshot

    def _collect_health(self):
        next_send=0
        while not self._telemetry_stop.wait(.25):
            try:
                if time.monotonic()>=next_send:
                    self.publisher.submit(self._health_snapshot()); next_send=time.monotonic()+self.publisher.config.get('intervalSeconds',30)
                self._health_status=self.publisher.status()
                if not self.publisher.config.get('endpoint'): self._health_status['state']='not-configured'
            except Exception: self._health_status={'state':'error'}

    def test_health(self):
        from .telemetry import Publisher
        config=dict(self.config.telemetry,**self._settings()['health'])
        if not config.get('endpoint') or not config.get('secretFile'):
            return {'simulated':False,'success':False,'message':'Endpoint and secure authentication reference must be configured first'}
        if config.get('protocol') == 'fleet-v1':
            # A second publisher would fence the production source's live lease.
            self.publisher.submit(self._health_snapshot())
            result=self.publisher.status()
            at=result.get('lastSuccessfulSend')
            success=result.get('state')=='Connected' and at is not None and time.time()-at < max(60,config['intervalSeconds']*3)
            return {'simulated':False,'success':success,'message':'Server acknowledged recent health on the active fleet connection' if success else 'No recent acknowledged health; inspect publisher status'}
        test=Publisher(dict(config,enabled=True)); deadline=time.monotonic()+10
        try:
            test.submit(self._health_snapshot())
            while time.monotonic()<deadline:
                result=test.status()
                if result.get('lastSuccessfulSend') is not None: return {'simulated':False,'success':True,'message':'Observed encrypted authenticated health sample delivery'}
                if result.get('state')=='Error': return {'simulated':False,'success':False,'message':'Health connection failed; inspect safe diagnostic code'}
                time.sleep(.05)
            return {'simulated':False,'success':False,'message':'Health test timed out; storage remains independent'}
        finally: test.close()

    def retry(self,principal,job_id):
        import time
        current=self.jobs.get(principal,job_id)
        if current['state']!='failed': raise BackendError('CONFLICT','Only failed jobs can be retried')
        if self.pool and job_id in self.pool.active: raise BackendError('BUSY','Previous worker is still exiting',True)
        with self.state.transaction():
            record=self.state.get('jobs',job_id)
            self.jobs._authorize_payload(self.jobs._principal(record),record['operation'],record['_payload'])
            lease=record.get('_lease')
            if lease or not record.get('_workerStopped',False): raise BackendError('BUSY','Previous writer termination is not yet proven',True)
            if record['operation']=='purge': raise BackendError('CONFIRMATION_REQUIRED','Review remaining Trash targets for a new exact confirmation')
            if record['operation']=='new-folder' and self.state.get('intents',job_id+':mkdir',{}).get('status')=='intent': raise BackendError('STATE_UNAVAILABLE','Interrupted folder creation needs owner inspection before retry')
            record['_fence']+=1; record['_lease']=None; record['error']=None
            record['_attempt']=record.get('_attempt',0)+1
            record['state']='waiting-for-client' if record['operation'] in ('upload','download') else 'queued'
            self.state.put('attempts',job_id+':'+str(record['_attempt']),{'jobId':job_id,'attempt':record['_attempt'],'at':timestamp(),'actor':principal.id})
            self.jobs._save(record,'retry-reconciled')
            for transfer in self.state.list('transfers',limit=10000):
                if transfer['jobId']==job_id and transfer['state'] not in ('completed','discarded'):
                    transfer['state']='waiting-for-client'; self.state.put('transfers',transfer['id'],transfer)
        return self.jobs.get(principal,job_id)

    def write_chunk(self,token,transfer_id,offset,data): return self.transfers.write(self.authenticate(token),transfer_id,offset,data)
    def download_chunk(self,token,drive_id,path,revision,offset,length): return self.transfers.read(self.authenticate(token),drive_id,path,revision,offset,length)

    def run_one(self):
        lease=self.jobs.claim('inline-'+uuid.uuid4().hex)
        if lease: self.operations.execute(lease)
        return lease
