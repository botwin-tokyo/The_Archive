"""Large mobile HTTP bodies over existing durable bounded transfer checkpoints."""
from contextlib import contextmanager
import hashlib
import os
import threading
import time
import uuid

from .catalog import present_entry
from .contracts import BackendError, CHUNK_BYTES, bytes_value, canonical, name, object_fields, translate_error
from .jobs import _owner_stopped, _process_stamp
from .mobile_auth import request_id, stamp
from .mobile_metadata import legacy_ref
from .mobile_contracts import mobile_name


class MobileTransfers:
    supports_upload=True
    supports_download=True
    def __init__(self,boundary):
        self.boundary=boundary; self.service=boundary.service; self.state=self.service.state
        self._condition=threading.Condition(threading.RLock()); self._active={}; self._closing=False
        self._hash_slot=threading.BoundedSemaphore(1); self.runtime=str(uuid.uuid4())
        from .mobile_downloads import MobileDownloads
        self.downloads=MobileDownloads(self)
        from .mobile_recovery import MobileMaintenance
        try: self.maintenance=MobileMaintenance(self)
        except BaseException:
            self.downloads.close(); raise
    def close(self):
        self.maintenance.close()
        with self._condition:
            self._closing=True
            while self._active: self._condition.wait(1)
        self.downloads.close()
    def _upload(self,context,tid):
        record=self.state.get('mobile_uploads',tid)
        if not record or record['actor']!=context.principal.id or record['sessionId']!=context.session_id:
            raise BackendError('PERMISSION_DENIED','Upload is not available to this device')
        self.service.transfers._session(context.principal,tid)
        return record
    def _reserve(self,size):
        total=self.state.connection.execute("SELECT COALESCE(SUM(CAST(json_extract(body,'$.size') AS INTEGER)),0) FROM records WHERE namespace IN ('mobile_uploads','mobile_downloads') AND COALESCE(json_extract(body,'$.released'),0)=0").fetchone()[0]
        if total+size>int(self.boundary.limits['stagingBudgetBytes']):
            raise BackendError('QUOTA_EXCEEDED','Mobile staging reservation budget reached')
    @contextmanager
    def _writer(self,context,resource,direction,phase='content'):
        worker='mobile-'+direction+'-'+str(uuid.uuid4())
        record={'owner':self.runtime,'pid':os.getpid(),'processStart':_process_stamp(os.getpid()),
                'worker':worker,'session':context.session_id,'direction':direction,'phase':phase}
        with self._condition:
            if self._closing: raise BackendError('UNAVAILABLE','Mobile service is stopping',True)
            if resource in self._active or len(self._active)>=2 or any(r['session']==context.session_id and r['direction']==direction for r in self._active.values()):
                raise BackendError('BUSY','A bounded transfer writer is already active',True)
            with self.state.transaction():
                prior=self.state.get('mobile_writers',resource)
                if prior:
                    if not _owner_stopped(prior): raise BackendError('BUSY','Previous writer termination is not proven',True)
                    self.state.delete('mobile_writers',resource)
                self.state.put('mobile_writers',resource,record)
            self._active[resource]=record
        try: yield worker
        finally:
            with self._condition:
                try:
                    with self.state.transaction():
                        prior=self.state.get('mobile_writers',resource)
                        if prior and prior.get('worker')==worker: self.state.delete('mobile_writers',resource)
                        upload=self.state.get('mobile_uploads',resource)
                        if upload:
                            job=self.state.get('jobs',upload['jobId'])
                            lease=job.get('_lease') if job else None
                            # This method has returned from its only writer call.
                            # Never fence another caller's lease on failed acquisition.
                            if job and ((lease and lease.get('worker')==worker) or (job['state']=='failed' and not lease)):
                                if lease:
                                    job['_lease']=None; job['_fence']+=1; job['state']='failed'
                                    job['error']=BackendError('TRANSFER_INTERRUPTED','Writer stopped; reconcile retained bytes',True).envelope(job['id'])
                                job['_workerStopped']=True; self.service.jobs._save(job,'mobile-writer-stopped')
                            # Close the final-byte/control race under the same
                            # coordinator lock used by pause/cancel submission.
                            if phase=='content' and upload.get('desired') and job and not job.get('_lease') and job['state'] in ('queued','waiting-for-client'):
                                if upload['desired']=='cancel': self.service.transfers.cancel(context.principal,resource)
                                else: self.service.jobs.control(context.principal,upload['jobId'],'pause')
                                upload['desired']=None; self.state.put('mobile_uploads',resource,upload)
                            transfer=self.state.get('transfers',resource)
                            if transfer and transfer['state'] in ('canceled','discarded'):
                                self._cleanup_cancel(context,resource)
                finally:
                    self._active.pop(resource,None); self._condition.notify_all()
    def initiate_upload(self,context,args):
        object_fields(args,('requestId','destination','name','size','sha256','collisionPolicy'),('expectedCollision',))
        rid=request_id(args['requestId']); size=bytes_value(args['size']); mobile_name(args['name'])
        if size>int(self.boundary.limits['maxAcceptedFileBytes']): raise BackendError('QUOTA_EXCEEDED','File exceeds mobile size limit')
        if not isinstance(args['sha256'],str) or len(args['sha256'])!=64 or any(x not in '0123456789abcdef' for x in args['sha256']):
            raise BackendError('INVALID_ARGUMENT','A frozen full-file SHA-256 is required')
        if args['collisionPolicy'] not in ('ask','skip','keep_both','replace'): raise BackendError('INVALID_ARGUMENT','Invalid collision policy')
        digest=hashlib.sha256(canonical(args).encode()).hexdigest(); key=context.principal.id+':'+rid
        with self.state.transaction():
            prior=self.state.get('mobile_upload_requests',key)
            if prior:
                if prior['digest']!=digest: raise BackendError('REQUEST_MISMATCH','Upload request ID is bound to another payload')
                tid=prior['transferId']; self._upload(context,tid)
            else:
                from .mobile_limits import reserve_request
                reserve_request(self.state)
                active=self.state.connection.execute("SELECT COUNT(*) FROM records WHERE namespace='mobile_uploads' AND json_extract(body,'$.actor')=? AND COALESCE(json_extract(body,'$.released'),0)=0",(context.principal.id,)).fetchone()[0]
                if active>=128: raise BackendError('QUOTA_EXCEEDED','Device has too many retained uploads')
                destination=self.boundary.metadata.validate_ref(context,args['destination'],stat=False)
                current=self.boundary.metadata.current(context,destination['driveId'],destination['path'])
                if current['id']!=destination['id'] or current['kind']!='directory': raise BackendError('SOURCE_CHANGED','Upload destination identity changed')
                self.service.auth.authorize(context.principal,'write',destination['driveId'],destination['path'])
                fs=self.service.catalog.filesystem(destination['driveId'])
                target=(destination['path']+'/' if destination['path'] else '')+args['name']
                existing=self.service.operations._existing(fs,target)
                accepted_collision=self.boundary.metadata.ref(present_entry(existing,destination['driveId'])) if existing else None
                if 'expectedCollision' in args:
                    expected=args['expectedCollision']
                    if expected is not None: legacy_ref(expected)
                    if expected!=accepted_collision:
                        raise BackendError('CONFLICT','Reviewed collision target changed; review the current target before admission')
                self._reserve(size)
                admission=self.service.transfers.initiate(context.principal,{'requestId':rid,'destination':{k:destination[k] for k in ('driveId','path')},'name':args['name'],'sizeBytes':args['size'],'sha256':args['sha256'],'conflictPolicy':args['collisionPolicy'].replace('_','-')})
                tid=admission['transferId']; now=time.time()
                record={'id':tid,'actor':context.principal.id,'sessionId':context.session_id,'requestId':rid,'jobId':admission['jobId'],
                        'size':args['size'],'sha256':args['sha256'],'destination':args['destination'],'name':args['name'],
                        'created':now,'updated':now,'released':False,'desired':None,'lastError':None,'cleanupState':'not_needed'}
                self.state.put('mobile_uploads',tid,record)
                self.state.put('mobile_upload_requests',key,{'digest':digest,'transferId':tid})
                if args['collisionPolicy']=='replace':
                    transfer=self.state.get('transfers',tid)
                    expected=legacy_ref(accepted_collision) if accepted_collision is not None else None
                    self.state.put('mobile_collision_guards','transfer:'+tid,{'actor':context.principal.id,'drive':transfer['drive'],'target':transfer['path'],'generation':fs.volume.generation,'expected':expected})
        return self.upload_snapshot(context,tid,mint=True)
    def upload_snapshot(self,context,tid,*,mint=False):
        # Same lock order as writer/control: coordinator, then SQLite.
        with self._condition:
            active=dict(self._active.get(tid) or {})
            with self.state.transaction():
                return self._snapshot_locked(context,tid,active,mint=mint)
    def _snapshot_locked(self,context,tid,active,*,mint=False):
        record=self._upload(context,tid); transfer=self.service.transfers.query(context.principal,tid); job=self.service.jobs.get(context.principal,record['jobId'])
        if transfer['state'] in ('canceled','discarded') and self.state.get('intents','transfer:'+tid):
            raise BackendError('STATE_UNAVAILABLE','Publication history must be reconciled before declaring cancellation')
        state=job['state']; outcome=None
        if record.get('expired'): state='failed'
        elif state=='completed': outcome='published' if transfer['entry'] is not None else 'skipped' if job['outcome']=='skipped' else None
        elif transfer['state'] in ('canceled','discarded') or state=='canceled': state='canceled'; outcome='canceled'
        elif record.get('desired'): state={'pause':'pausing','cancel':'canceling'}[record['desired']]
        elif active and active['phase']=='content': state={'pause':'pausing','cancel':'canceling'}.get(record.get('desired'),'running')
        error=None; raw=record.get('lastError') if record.get('expired') else job.get('error') or record.get('lastError')
        cleanup=record.get('cleanupState','pending' if not record['released'] else 'confirmed')
        if raw and (outcome is None or cleanup=='failed'):
            from .mobile_contracts import mobile_error, stored_error
            error=mobile_error(stored_error(raw))['error']
            if raw['code']=='INTEGRITY_FAILED': error.update(code='integrity_failed',recovery='choose_source',retryable=False)
            if cleanup=='failed': error.update(code='cleanup_pending',recovery='retry',retryable=False)
        controls=[k for k,v in job['capabilities'].items() if v and k in ('pause','resume','cancel','retry')]
        if active and active['phase']=='content': controls=['pause','cancel']
        if active and active['phase']=='finalize': controls=[]
        recoverable={'TRANSFER_INTERRUPTED','INTERRUPTED','INSUFFICIENT_SPACE','READ_ONLY','DRIVE_UNAVAILABLE','UNAVAILABLE','BUSY'}
        if state=='failed' and raw and (raw.get('retryable') or raw['code'] in recoverable) and not job.get('_lease'):
            stored=self.state.get('jobs',job['id'])
            if stored.get('_workerStopped') and raw['code'] not in ('INTEGRITY_FAILED','SOURCE_CHANGED','STALE_REVISION'): controls=['retry']
        if record.get('expired'): controls=[]
        if outcome: controls=['cancel'] if outcome=='canceled' and cleanup!='confirmed' else []
        grant={'transferToken':None,'expiresAt':None}
        if mint and outcome is None and not record.get('desired') and not record.get('expired'):
            grant=self.boundary.auth.issue_transfer(context,'upload',tid,int(transfer['offset']),int(record['size']))
        entry=None
        if transfer['entry']:
            saved=self.state.get('transfers',tid)
            entry={k:transfer['entry'][k] for k in ('driveId','path','id')}
            entry.update(revision=str(transfer['entry']['revision']),generation=str(saved['generation']))
        return {'schemaVersion':1,'requestId':record['requestId'],'receiptId':record['jobId'],'serverJobId':record['jobId'],'transferId':tid,
                'acceptedSize':record['size'],'acceptedSha256':record['sha256'],'durableOffset':transfer['offset'],
                'contentPath':'/mobile/v1/uploads/'+tid+'/content',**grant,'protocol':'archive_mobile_file_v1','state':state,
                'outcome':outcome,'entry':entry,'verification':transfer['verification'],'error':error,'controls':controls,'cleanupState':cleanup}
    def _desired(self,context,tid):
        record=self._upload(context,tid)
        if self._closing: raise BackendError('TRANSFER_INTERRUPTED','Service stopping; retained offset is authoritative',True)
        action=record.get('desired')
        if not action: return
        if action=='cancel':
            if self.state.get('intents','transfer:'+tid): raise BackendError('STATE_UNAVAILABLE','Publication intent requires reconciliation before cancellation')
            self.service.transfers.cancel(context.principal,tid)
        else: self.service.jobs.control(context.principal,record['jobId'],'pause')
        record['desired']=None
        if action=='cancel': record['cleanupState']='pending'
        self.state.put('mobile_uploads',tid,record)
        raise BackendError('CONFLICT','Transfer '+('canceled' if action=='cancel' else 'paused')+' at a durable boundary')
    def write_upload(self,token,tid,offset,length,reader):
        context=self.boundary.auth.authenticate_transfer(token,'upload',tid,offset,length)
        self._upload(context,tid)
        with self._writer(context,tid,'upload') as worker:
            transfer=self.service.transfers.query(context.principal,tid)
            if offset!=int(transfer['offset']): raise BackendError('CONFLICT','Query authoritative durable offset before retrying this body')
            if self.service.jobs.get(context.principal,transfer['jobId'])['state'] not in ('queued','waiting-for-client'):
                raise BackendError('CONFLICT','Upload is not currently writable')
            remaining=length; cursor=offset
            try:
                while remaining:
                    self._desired(context,tid)
                    self.boundary.auth.authenticate_transfer(token,'upload',tid,cursor,remaining)
                    data=reader.read(min(CHUNK_BYTES,remaining))
                    if not isinstance(data,bytes) or not data or len(data)>min(CHUNK_BYTES,remaining):
                        raise BackendError('TRANSFER_INTERRUPTED','Body ended before its declared length; query retained offset',True)
                    self._desired(context,tid)
                    self.boundary.auth.authenticate_transfer(token,'upload',tid,cursor,len(data))
                    ack=self.service.transfers.write(context.principal,tid,cursor,data,worker_id=worker)
                    cursor=int(ack['offset']); remaining-=len(data)
                    with self.state.transaction():
                        record=self.state.get('mobile_uploads',tid); record['cleanupState']='pending'; record['updated']=time.time(); self.state.put('mobile_uploads',tid,record)
                self._desired(context,tid)
                record=self._upload(context,tid); record.update(updated=time.time(),lastError=None); self.state.put('mobile_uploads',tid,record)
                return {'schemaVersion':1,'transferId':tid,'durableOffset':str(cursor)}
            except Exception as exc:
                error=translate_error(exc)
                with self.state.transaction():
                    record=self.state.get('mobile_uploads',tid)
                    job=self.state.get('jobs',record['jobId'])
                    stopped=job and job['state'] in ('paused','pausing','canceling','canceled')
                    record['lastError']=None if stopped else error.envelope()
                    record['updated']=time.time(); self.state.put('mobile_uploads',tid,record)
                raise
    def finalize_upload(self,context,tid):
        snapshot=self.upload_snapshot(context,tid)
        if snapshot['outcome'] is not None:
            if snapshot['outcome'] in ('published','skipped'): self._release_completed(context,tid)
            return snapshot
        if not self._hash_slot.acquire(blocking=False): raise BackendError('BUSY','A bounded verification worker is busy',True)
        try:
            with self._writer(context,tid,'upload','finalize') as worker:
                self._desired(context,tid)
                self.service.transfers.finalize(context.principal,tid,worker_id=worker)
                self._release_completed(context,tid)
        finally: self._hash_slot.release()
        return self.upload_snapshot(context,tid)
    def _release_completed(self,context,tid):
        with self.state.transaction():
            record=self._upload(context,tid); transfer=self.state.get('transfers',tid)
            if record['released'] or transfer['state']!='completed': return
            try:
                if transfer.get('entry') is None:
                    fs=self.service.catalog.filesystem(transfer['drive']); fs.volume.check(transfer['generation'])
                    try: fs.discard_staging(tid)
                    except BackendError as e:
                        if e.code!='NOT_FOUND': raise
                record['released']=True; record['cleanupState']='confirmed'; record['lastError']=None
            except BackendError as e:
                record['cleanupState']='failed'; record['lastError']=e.envelope()
            record['updated']=time.time(); self.state.put('mobile_uploads',tid,record)
    def _cleanup_cancel(self,context,tid):
        """Called under coordinator ownership only, after file writers have returned."""
        record=self._upload(context,tid); transfer=self.state.get('transfers',tid)
        if record['released'] or transfer['state'] not in ('canceled','discarded'): return
        owner=self.state.get('mobile_writers',tid)
        if owner and owner['owner']!=self.runtime and not _owner_stopped(owner):
            record['cleanupState']='pending'; self.state.put('mobile_uploads',tid,record); return
        if self.state.get('intents','transfer:'+tid): raise BackendError('STATE_UNAVAILABLE','Publication intent requires reconciliation before cleanup')
        record['cleanupState']='pending'; self.state.put('mobile_uploads',tid,record)
        try:
            fs=self.service.catalog.filesystem(transfer['drive']); fs.volume.check(transfer['generation'])
            self.service.transfers.cancel(context.principal,tid,discard=True)
            record.update(released=True,cleanupState='confirmed',lastError=None,desired=None,updated=time.time())
        except BackendError as e:
            record.update(cleanupState='failed',lastError=e.envelope(),updated=time.time())
        self.state.put('mobile_uploads',tid,record)
    def _reconcile_cancel_publication(self,context,tid):
        if not self._hash_slot.acquire(blocking=False): raise BackendError('BUSY','A bounded verification worker is busy',True)
        try:
            with self._writer(context,tid,'upload','reconcile'):
                record=self._upload(context,tid); job=self.state.get('jobs',record['jobId'])
                if job.get('_lease') and not _owner_stopped(job['_lease']):
                    raise BackendError('BUSY','Publication writer termination is not proven',True)
                from .recovery import reconcile_transfer
                if not reconcile_transfer(self.service,tid):
                    raise BackendError('STATE_UNAVAILABLE','Publication result is uncertain; reconcile before a new admission')
                self._release_completed(context,tid)
        finally: self._hash_slot.release()
        return self.upload_snapshot(context,tid)
    def control_upload(self,context,tid,action):
        if action not in ('pause','resume','retry','cancel'): raise BackendError('INVALID_ARGUMENT','Unknown upload control')
        with self._condition:
            snapshot=self.upload_snapshot(context,tid)
            reconcile=action=='cancel' and snapshot['outcome'] is None and tid not in self._active and self.state.get('intents','transfer:'+tid) is not None
        if reconcile: return self._reconcile_cancel_publication(context,tid)
        with self._condition:
            record=self._upload(context,tid); snapshot=self.upload_snapshot(context,tid)
            active=self._active.get(tid)
            if snapshot['outcome'] is not None:
                if snapshot['outcome']=='canceled' and action=='cancel' and not active:
                    self._cleanup_cancel(context,tid)
                return self.upload_snapshot(context,tid)
            if active:
                if active['phase']!='content' or action not in ('pause','cancel'):
                    raise BackendError('BUSY','Wait for the active transfer boundary',True)
                record['desired']=action
                if action=='cancel': record['cleanupState']='pending'
                self.state.put('mobile_uploads',tid,record)
                return self.upload_snapshot(context,tid)
            if action=='cancel':
                if self.state.get('intents','transfer:'+tid): raise BackendError('STATE_UNAVAILABLE','Publication result changed; reconcile before cancellation')
                self.service.transfers.cancel(context.principal,tid)
            elif action=='retry':
                if 'retry' not in snapshot['controls']: raise BackendError('UNSUPPORTED_ENTRY','Upload retry requires reconciled writer termination')
                self.service.retry(context.principal,record['jobId'])
            else: self.service.jobs.control(context.principal,record['jobId'],action)
            record['desired']=None; record['updated']=time.time(); self.state.put('mobile_uploads',tid,record)
            if action=='cancel': self._cleanup_cancel(context,tid)
        return self.upload_snapshot(context,tid)
