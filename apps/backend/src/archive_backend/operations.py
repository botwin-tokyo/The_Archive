"""Recoverable filesystem mutations. Disk intent and outcome are distinct records."""
import contextlib
import hashlib
import time
import uuid
from .contracts import BackendError, CHUNK_BYTES, canonical, entry_ref, location, name, object_fields, path
from .catalog import present_entry, timestamp


def join(parent, child):
    return parent+'/'+child if parent else child


class Operations:
    def __init__(self,state,auth,jobs,catalog,confirmations,*,fault=lambda _:None,min_free_bytes=67108864):
        self.state,self.auth,self.jobs,self.catalog,self.confirmations=state,auth,jobs,catalog,confirmations
        self.fault,self.min_free_bytes=fault,min_free_bytes
        self._progress_at={}
        self._copy_totals={}

    @staticmethod
    def _effective_destination(request,ref=None):
        # A rename's omitted destination is still a write to the source parent.
        # Resolve it once so policy, worker and recovery checks use the same path.
        dest=request.get('destination')
        if dest is None:
            dest={'driveId':ref['driveId'],'path':ref['path'].rpartition('/')[0]}
        child=request['name'] if ref is None else (ref['path'].rsplit('/',1)[-1] if request['kind']=='restore' else request.get('name',ref['path'].rsplit('/',1)[-1]))
        return dest,join(dest['path'],child)

    def _authorize_destination(self,principal,dest,target):
        self.auth.authorize(principal,'write',dest['driveId'],dest['path'])
        self.auth.authorize(principal,'write',dest['driveId'],target)

    def _destinations(self,request):
        kind=request.get('kind')
        if kind not in ('new-folder','rename','copy','move','restore'): return []
        result=[]
        for ref in ([None] if kind=='new-folder' else request.get('targets',[])):
            dest,target=self._effective_destination(request,ref)
            if ref is not None and kind in ('rename','restore') and dest['driveId']!=ref['driveId']:
                raise BackendError('INVALID_ARGUMENT','Rename and restore remain on the original drive')
            result.append((dest,target))
        return result

    def _authorize_destinations(self,principal,request):
        for dest,target in self._destinations(request):
            self._authorize_destination(principal,dest,target)

    def validate(self,principal,request):
        object_fields(request,('requestId','kind'),('targets','destination','name','conflictPolicy','confirmation','recreateParents','parentRequestId'))
        if not isinstance(request['requestId'],str) or not 1<=len(request['requestId'])<=256: raise BackendError('INVALID_ARGUMENT','Request ID required')
        kind=request['kind']
        if kind not in ('new-folder','rename','copy','move','trash','restore','purge'): raise BackendError('INVALID_ARGUMENT','Use the byte transfer API for upload/download')
        targets=request.get('targets',[])
        if not isinstance(targets,list) or len(targets)>1000 or (kind!='new-folder' and not targets): raise BackendError('INVALID_ARGUMENT','Bounded exact targets required')
        if kind=='rename' and len(targets)!=1: raise BackendError('INVALID_ARGUMENT','Rename requires one target')
        for ref in targets:
            entry_ref(ref); path(ref['path'],allow_root=False)
            self.auth.authorize(principal,'read' if kind=='copy' else ('trash' if kind in ('trash','restore') else 'purge' if kind=='purge' else 'write'),ref['driveId'],ref['path'])
        if 'destination' in request:
            location(request['destination']); self.auth.authorize(principal,'write',request['destination']['driveId'],request['destination']['path'])
        if kind in ('new-folder','copy','move') and 'destination' not in request: raise BackendError('INVALID_ARGUMENT','Destination required')
        if kind in ('new-folder','rename'): name(request.get('name'))
        if request.get('conflictPolicy','ask') not in ('ask','skip','keep-both','replace'): raise BackendError('INVALID_ARGUMENT','Invalid collision policy')
        self._authorize_destinations(principal,request)
        for ref in targets:
            dest=request.get('destination')
            if dest and dest['driveId']==ref['driveId'] and (dest['path']==ref['path'] or dest['path'].startswith(ref['path']+'/')):
                raise BackendError('INVALID_PATH','Cannot copy or move into the source subtree')
        return request

    def submit(self,principal,request):
        self.validate(principal,request)
        # Reconcile durable identity before inspecting today's destination; an
        # already committed target is not a new conflict on an exact retry.
        if self.state.reconcile(principal.id,request['kind'],request['requestId']):
            return self.jobs.accept(principal,request['kind'],request,request['requestId'])
        if request.get('conflictPolicy','ask')=='ask' and request['kind'] in ('new-folder','rename','copy','move','restore'):
            conflicts=[]
            for dest,target in self._destinations(request):
                source_name=target.rsplit('/',1)[-1]
                self.auth.authorize(principal,'write',dest['driveId'],target)
                fs=self.catalog.filesystem(dest['driveId']); existing=self._existing(fs,target)
                if existing:
                    value=present_entry(existing,dest['driveId'],self.auth,principal)
                    conflicts.append({'sourceName':source_name,'destination':dest,'existing':{k:value[k] for k in ('id','driveId','path','revision')},'kind':'name','allowedPolicies':['ask','skip','keep-both','replace']})
            if conflicts: return {'status':'needs-decision','requestId':request['requestId'],'conflicts':conflicts}
        # Required irreversible consent is consumed with acceptance, before any worker can act.
        with self.state.transaction():
            prior=self.state.reconcile(principal.id,request['kind'],request['requestId'])
            if request['kind']=='purge' and not prior:
                conf=request.get('confirmation',{})
                if not conf.get('grantId'): raise BackendError('CONFIRMATION_REQUIRED','Review exact permanent-deletion targets first')
                generations={r['driveId']:self.catalog.filesystem(r['driveId']).volume.generation for r in request['targets']}
                self.confirmations.consume(principal,'purge',request['targets'],generations,conf['grantId'])
            return self.jobs.accept(principal,request['kind'],request,request['requestId'])

    def _checkpoint(self,job_id,worker,fence):
        self.jobs.heartbeat(job_id,worker,fence)
        record=self.state.get('jobs',job_id)
        if record['state']=='canceling':
            self.jobs.transition(job_id,'canceled',worker,fence,{'outcome':'partial'})
            raise BackendError('CANCELED','Work stopped; retained partials remain recoverable')
        if record['state']=='pausing':
            self.jobs.transition(job_id,'paused',worker,fence)
            raise BackendError('PAUSED','Work paused at a safe checkpoint')
        principal=self.jobs._principal(record)
        self._authorize_destinations(principal,record['_payload'])
        return principal

    @contextlib.contextmanager
    def _locks(self,job_id,worker,fence,locations):
        keys=sorted(set((x['driveId'],x['path']) for x in locations if x))
        while True:
            with self.state.transaction():
                record=self.state.get('jobs',job_id); self.jobs._lease(record,worker,fence)
                busy=any(lock['job']!=job_id and d==lock['drive'] and
                         (p==lock['path'] or p.startswith(lock['path']+'/') or lock['path'].startswith(p+'/') or not p or not lock['path'])
                         for lock in self.state.list('locks',limit=10000) for d,p in keys)
                if not busy:
                    for d,p in keys: self.state.put('locks',job_id+':'+hashlib.sha256((d+':'+p).encode()).hexdigest(),{'job':job_id,'drive':d,'path':p,'fence':fence})
                    break
            # Ordinary concurrent work is a wait, not a terminal download error.
            # Sleep without the database lock; keep authorization, cancellation,
            # pause and lease heartbeats active while the other worker finishes.
            self._checkpoint(job_id,worker,fence)
            time.sleep(0.1)
        try: yield
        finally:
            with self.state.transaction() as c:
                c.execute("DELETE FROM records WHERE namespace='locks' AND json_extract(body,'$.job')=?",(job_id,))

    def _existing(self,fs,target):
        try: return fs.stat(target)
        except BackendError as e:
            if e.code=='NOT_FOUND': return None
            raise

    def _intent(self,key,record,principal):
        if 'drive' in record: record=dict(record,generation=self.catalog.filesystem(record['drive']).volume.generation,actor=principal.id)
        with self.state.transaction():
            self.state.put('intents',key,record)
            self.state.audit(principal.id,record['kind'],'intent',key)
        self.fault('after-intent')

    def _done(self,key,record,principal):
        record=dict(self.state.get('intents',key,{}) or {},**record); record['status']='done'
        with self.state.transaction():
            self.state.put('intents',key,record)
            self.state.audit(principal.id,record['kind'],'committed',key)
        self.fault('after-outcome')
        return record

    def _trash(self,principal,drive,source,revision,key):
        fs=self.catalog.filesystem(drive); token=hashlib.sha256(key.encode()).hexdigest()
        previous=self.state.get('intents',key)
        if previous:
            try:
                held=fs.internal_stat('trash',token)
                if (held.device,held.inode)!=(previous['device'],previous['inode']): raise BackendError('STATE_UNAVAILABLE','Trash identity does not match durable intent')
            except BackendError as e:
                if e.code!='NOT_FOUND': raise
            else:
                with self.state.transaction():
                    self.state.put('trash',token,previous)
                    self._done(key,previous,principal)
                return token
        entry=fs.stat(source)
        if entry.revision!=revision: raise BackendError('STALE_REVISION','Source changed before Trash')
        self.auth.authorize(principal,'trash',drive,source)
        record={'kind':'trash','status':'intent','drive':drive,'source':source,'revision':revision,'device':entry.device,'inode':entry.inode,'token':token,
                'entry':present_entry(entry,drive,self.auth,principal),'deletedAt':timestamp(),'deletedBy':{'id':principal.id,'label':principal.label,'kind':principal.kind}}
        self._intent(key,record,principal)
        fs.move_to_internal(source,'trash',token,expected_revision=revision)
        self.fault('after-trash-rename')
        with self.state.transaction():
            self.state.put('trash',token,record)
            self._done(key,record,principal)
        return token

    def _destination(self,principal,fs,target,policy,key):
        self.auth.authorize(principal,'write',fs.volume.volume_id,target)
        existing=self._existing(fs,target)
        # Mobile admission may bind a Replace decision to an exact current target.
        # Legacy requests without this internal guard retain their existing contract.
        guard=self.state.get('mobile_collision_guards',key)
        if guard:
            if guard['actor']!=principal.id or guard['drive']!=fs.volume.volume_id or guard['target']!=target:
                raise BackendError('PERMISSION_DENIED','Collision decision does not match this operation')
            fs.volume.check(guard['generation'])
            expected=guard['expected']
            prior=self.state.get('intents',key+':replace')
            own_trash=bool(prior and prior.get('kind')=='trash' and prior.get('source')==target
                           and prior.get('entry',{}).get('id')==(expected or {}).get('id')
                           and prior.get('revision')==(expected or {}).get('revision'))
            if own_trash:
                try:
                    held=fs.internal_stat('trash',prior['token'])
                    own_trash=(held.device,held.inode)==(prior['device'],prior['inode'])
                except BackendError as error:
                    if error.code!='NOT_FOUND': raise
                    own_trash=False
            if existing:
                current=present_entry(existing,fs.volume.volume_id)
                if not expected or current['id']!=expected['id'] or current['revision']!=expected['revision']:
                    raise BackendError('STALE_REVISION','Replacement target changed; review the collision again')
            elif expected and not own_trash:
                raise BackendError('STALE_REVISION','Replacement target disappeared; review the collision again')
        if not existing: return target
        if policy=='skip': return None
        if policy=='ask': raise BackendError('CONFLICT','Destination exists; choose a collision policy')
        if policy=='replace':
            self._trash(principal,fs.volume.volume_id,target,existing.revision,key+':replace')
            return target
        base=target
        for number in range(1,10001):
            candidate=base+f' ({number})'
            self.auth.authorize(principal,'write',fs.volume.volume_id,candidate)
            if not self._existing(fs,candidate): return candidate
        raise BackendError('QUOTA_EXCEEDED','Unique-name search budget exhausted')

    def _publish(self,principal,fs,token,target,policy,key,job_id,worker,fence):
        previous=self.state.get('intents',key)
        if previous:
            self.auth.authorize(principal,'write',fs.volume.volume_id,previous['destination'])
            published=self._existing(fs,previous['destination'])
            if published and (published.device,published.inode)==(previous['device'],previous['inode']):
                self._done(key,previous,principal); return published
        stage=fs.internal_stat('staging',token)
        for _ in range(100):
            actual=self._destination(principal,fs,target,policy,key)
            if actual is None: return None
            record={'kind':'publish','status':'intent','drive':fs.volume.volume_id,'token':token,'destination':actual,'device':stage.device,'inode':stage.inode}
            self._intent(key,record,principal)
            self._checkpoint(job_id,worker,fence)
            @contextlib.contextmanager
            def publication():
                with self.state.transaction():
                    self.jobs._lease(self.state.get('jobs',job_id),worker,fence)
                    self.auth.authorize(principal,'write',fs.volume.volume_id,actual)
                    yield
            try:
                published=fs.commit_staging(token,actual,checkpoint=lambda:self._checkpoint(job_id,worker,fence),publication=publication)
            except BackendError as e:
                if e.code=='CONFLICT' and policy=='keep-both': continue
                raise
            self.fault('after-publish-rename')
            self._done(key,record,principal)
            return published
        raise BackendError('BUSY','Destination changed too often',True)

    def _copy_file(self,principal,src,dst,source,token,relative,job_id,worker,fence,key):
        entry=src.stat(source)
        if entry.kind!='file': raise BackendError('UNSUPPORTED_ENTRY','Only regular files can be copied')
        checkpoint=self.state.get('children',key)
        if checkpoint and checkpoint.get('status')=='verified':
            if checkpoint['revision']!=entry.revision: raise BackendError('SOURCE_CHANGED','Copy source changed')
            dest_entry=dst.internal_stat('staging',token,relative)
            if dest_entry.size!=entry.size: raise BackendError('INTEGRITY_FAILED','Staged child changed')
            digest=hashlib.sha256()
            with dst.read_internal('staging',token,relative) as staged:
                while True:
                    self._checkpoint(job_id,worker,fence); data=staged.read(CHUNK_BYTES)
                    if not data: break
                    digest.update(data)
            if digest.hexdigest()!=checkpoint['sha256']: raise BackendError('INTEGRITY_FAILED','Staged child checksum changed')
            return entry.size
        with self.state.transaction():
            reserved=int(self.state.connection.execute("SELECT COALESCE(SUM(MAX(0,CAST(json_extract(body,'$.size') AS INTEGER)-CAST(json_extract(body,'$.offset') AS INTEGER))),0) FROM records WHERE namespace='transfers' AND json_extract(body,'$.drive')=? AND COALESCE(json_extract(body,'$.direction'),'upload')!='download' AND json_extract(body,'$.state') NOT IN ('completed','discarded')",(dst.volume.volume_id,)).fetchone()[0])
            reserved+=int(self.state.connection.execute("SELECT COALESCE(SUM(CAST(json_extract(body,'$.bytes') AS INTEGER)),0) FROM records WHERE namespace='reservations' AND json_extract(body,'$.drive')=? AND key!=?",(dst.volume.volume_id,key)).fetchone()[0])
            available=dst.volume.capacity()['available']
            if available<entry.size+reserved+self.min_free_bytes: raise BackendError('INSUFFICIENT_SPACE','Destination capacity is reserved for other work')
            self.state.put('reservations',key,{'id':key,'drive':dst.volume.volume_id,'bytes':str(entry.size),'job':job_id})
        child_id=str(uuid.uuid5(uuid.NAMESPACE_URL,key))
        with self.state.transaction():
            parent=self.state.get('jobs',job_id)
            # Detailed queue rows are a bounded preview, not a limit on folder size.
            # Every file still has its own durable verification checkpoint.
            if child_id not in parent['childIds'] and len(parent['childIds'])<1000:
                child=dict(parent,id=child_id,requestId=parent['requestId']+':'+child_id,revision=1,parentId=job_id,childIds=[],label=entry.name,source={'driveId':src.volume.volume_id,'path':source},state='running',bytesDone='0',bytesTotal=str(entry.size),_lease=None,_fence=0,_workPlanChild=True,_planKey=key)
                self.state.put('jobs',child_id,child); parent['childIds'].append(child_id); self.jobs._save(parent,'work-plan-child')
            if job_id not in self._copy_totals:
                self._copy_totals[job_id]=int(self.state.connection.execute("SELECT COALESCE(SUM(CAST(json_extract(body,'$.bytes') AS INTEGER)),0) FROM records WHERE namespace='children' AND key LIKE ?",(job_id+':%',)).fetchone()[0])
            completed=self._copy_totals[job_id]
        hasher=hashlib.sha256(); copied=0
        try: output=dst.create_internal_file('staging',token,relative)
        except BackendError as e:
            if e.code!='CONFLICT': raise
            output=dst.open_internal_file('staging',token,relative,writable=True)
            output.truncate(0)
        with src.open_read(source,entry.revision) as incoming,output:
            while True:
                self._checkpoint(job_id,worker,fence)
                self.auth.authorize(principal,'read',src.volume.volume_id,source)
                self.auth.authorize(principal,'write',dst.volume.volume_id,self.jobs.payload(job_id)['destination']['path'])
                data=incoming.read(CHUNK_BYTES)
                if not data: break
                if dst.volume.capacity()['available']<len(data)+self.min_free_bytes: raise BackendError('INSUFFICIENT_SPACE','Free-space reserve exhausted while copying')
                hasher.update(data); output.write(data); copied+=len(data)
                self.state.put('reservations',key,{'id':key,'drive':dst.volume.volume_id,'bytes':str(entry.size-copied),'job':job_id})
                now=time.monotonic()
                if now-self._progress_at.get(job_id,0)>=.25:
                    current=self.state.get('jobs',job_id)
                    self.jobs.update(job_id,worker,fence,{'bytesDone':str(max(int(current['bytesDone']),completed+copied))})
                    self._progress_at[job_id]=now
            output.flush()
        if copied!=entry.size or src.stat(source).revision!=entry.revision: raise BackendError('SOURCE_CHANGED','Copy source changed during transfer')
        verify=hashlib.sha256()
        with dst.read_internal('staging',token,relative) as incoming:
            while True:
                self._checkpoint(job_id,worker,fence); data=incoming.read(CHUNK_BYTES)
                if not data: break
                verify.update(data)
        if verify.digest()!=hasher.digest(): raise BackendError('INTEGRITY_FAILED','Destination bytes failed SHA-256 verification')
        self.state.put('children',key,{'id':key,'path':source,'relative':relative,'revision':entry.revision,'bytes':str(copied),'sha256':hasher.hexdigest(),'status':'verified'})
        with self.state.transaction():
            self.state.delete('reservations',key)
            current=self.state.get('jobs',job_id)
            self.jobs.update(job_id,worker,fence,{'bytesDone':str(max(int(current['bytesDone']),completed+copied))})
            self._copy_totals[job_id]=completed+copied
            child=self.state.get('jobs',child_id)
            if child is not None:
                child.update(state='verifying',bytesDone=str(copied)); self.jobs._save(child,'staged-verified')
        return copied

    def _checkpoint_children(self,key,checkpoint):
        # Never hold the SQLite writer lock while walking a large filesystem tree.
        # Keyset pages keep both memory and cancellation latency bounded.
        import json
        after=''
        while True:
            checkpoint()
            rows=self.state.select("SELECT key,body FROM records WHERE namespace='children' AND key LIKE ? AND key>? ORDER BY key LIMIT 128",(key+':%',after))
            if not rows: return
            for row in rows:
                yield json.loads(row['body'])
            after=rows[-1]['key']

    def _verify_children(self,source_fs,key,checkpoint=lambda:None):
        for child in self._checkpoint_children(key,checkpoint):
            if source_fs.stat(child['path']).revision!=child['revision']: raise BackendError('SOURCE_CHANGED','A copied child changed before source cleanup')

    def _copy(self,principal,ref,destination,target,policy,job_id,worker,fence,index):
        src=self.catalog.filesystem(ref['driveId']); dst=self.catalog.filesystem(destination['driveId'])
        key=f'{job_id}:{index}'; token=hashlib.sha256((key+':stage').encode()).hexdigest()
        prior=self.state.get('intents',key+':publish')
        if prior:
            self.auth.authorize(principal,'write',dst.volume.volume_id,prior['destination'])
            published=self._existing(dst,prior['destination'])
            if published and (published.device,published.inode)==(prior['device'],prior['inode']): return published
        if destination['driveId']==ref['driveId'] and target==ref['path'] and policy!='keep-both': raise BackendError('INVALID_ARGUMENT','Source and destination are identical')
        if policy=='skip' and self._existing(dst,target) is not None: return None
        source=src.stat(ref['path'])
        if source.revision!=ref['revision']: raise BackendError('STALE_REVISION','Source changed before copy')
        total=0
        if source.kind=='file': total=self._copy_file(principal,src,dst,ref['path'],token,'',job_id,worker,fence,key+':file')
        elif source.kind=='directory':
            try: dst.create_internal_dir('staging',token)
            except BackendError as e:
                if e.code!='CONFLICT': raise
            # Depth-first bounded recursion uses the filesystem iterator, not an in-memory work tree.
            def recurse(source_path,relative,depth):
                if depth>64: raise BackendError('QUOTA_EXCEEDED','Folder depth budget exhausted')
                before=src.stat(source_path); amount=0
                for child in src.iter_entries(source_path):
                    self._checkpoint(job_id,worker,fence)
                    sub=join(relative,child.name)
                    if child.kind=='directory':
                        try: dst.create_internal_dir('staging',token,sub)
                        except BackendError as e:
                            if e.code!='CONFLICT': raise
                        amount+=recurse(child.path,sub,depth+1)
                    elif child.kind=='file': amount+=self._copy_file(principal,src,dst,child.path,token,sub,job_id,worker,fence,key+':'+hashlib.sha256(child.path.encode()).hexdigest())
                    else: raise BackendError('UNSUPPORTED_ENTRY','Folder contains unsupported links or entries')
                if src.stat(source_path).revision!=before.revision: raise BackendError('SOURCE_CHANGED','Folder changed while copying')
                child_key=key+':dir:'+hashlib.sha256(source_path.encode()).hexdigest()
                self.state.put('children',child_key,{'id':child_key,'path':source_path,'relative':relative,'revision':before.revision,'bytes':'0','kind':'directory','status':'verified'})
                return amount
            total=recurse(ref['path'],'',0)
        else: raise BackendError('UNSUPPORTED_ENTRY','Unsupported copy source')
        if src.stat(ref['path']).revision!=ref['revision']: raise BackendError('SOURCE_CHANGED','Source changed before publication')
        # Validate every planned file once more after the complete tree was copied.
        for child in self._checkpoint_children(key,lambda:self._checkpoint(job_id,worker,fence)):
            if src.stat(child['path']).revision!=child['revision']: raise BackendError('SOURCE_CHANGED','A copied child changed before publication')
        self.fault('after-copy-verify')
        self._verify_children(src,key,lambda:self._checkpoint(job_id,worker,fence))
        published=self._publish(principal,dst,token,target,policy,key+':publish',job_id,worker,fence)
        for child_id in self.state.get('jobs',job_id)['childIds']:
            with self.state.transaction():
                self.jobs.heartbeat(job_id,worker,fence)
                child=self.state.get('jobs',child_id)
                if child.get('_planKey','').startswith(key+':'):
                    child.update(state='completed',outcome='complete' if published else 'skipped'); self.jobs._save(child,'publication-outcome')
        return published

    def execute(self,lease):
        job_id=lease['job']['id']; worker=lease['workerId']; fence=lease['fence']; request=lease['payload']; kind=request['kind']
        self._copy_totals.pop(job_id,None)
        names=[]; outcome='complete'
        try:
            principal=self._checkpoint(job_id,worker,fence)
            self.jobs.transition(job_id,'running',worker,fence)
            # Persisted jobs can predate these checks; revalidate before any effect.
            self.validate(principal,{k:v for k,v in request.items() if k!='previousAttemptId'})
            locations=[*request.get('targets',[]),*(dest for dest,_ in self._destinations(request))]
            with self._locks(job_id,worker,fence,locations):
                cleanup=self.state.get('cleanup',job_id)
                if cleanup:
                    destination=cleanup['destination']
                    self.catalog.stat(principal,{k:destination[k] for k in ('id','driveId','path','revision')})
                    ref=cleanup['ref']; fs=self.catalog.filesystem(ref['driveId'])
                    self._verify_children(fs,f"{job_id}:{cleanup['index']}",lambda:self._checkpoint(job_id,worker,fence))
                    self._trash(principal,ref['driveId'],ref['path'],ref['revision'],f"{job_id}:{cleanup['index']}:source-cleanup")
                    self.state.delete('cleanup',job_id)
                    self.jobs.transition(job_id,'verifying',worker,fence)
                    self.jobs.transition(job_id,'committing',worker,fence)
                    self.jobs.transition(job_id,'completed',worker,fence,{'outcome':'complete','resultingNames':[destination['name']]})
                    return
                if kind=='new-folder':
                    dest=request['destination']; fs=self.catalog.filesystem(dest['driveId']); target=join(dest['path'],request['name']); key=job_id+':mkdir'
                    old=self.state.get('intents',key)
                    if old and old['status']=='done': names=[target.rsplit('/',1)[-1]]
                    else:
                        self._intent(key,{'kind':'mkdir','status':'intent','drive':dest['driveId'],'destination':target},principal)
                        self._checkpoint(job_id,worker,fence)
                        actual=self._destination(principal,fs,target,request.get('conflictPolicy','ask'),key)
                        if actual is None:
                            self.jobs.transition(job_id,'verifying',worker,fence); self.jobs.transition(job_id,'committing',worker,fence); self.jobs.transition(job_id,'completed',worker,fence,{'outcome':'skipped'}); return
                        self._authorize_destination(principal,dest,actual)
                        entry=fs.create_dir(actual); target=actual; self.fault('after-mkdir')
                        self._done(key,{'kind':'mkdir','drive':dest['driveId'],'destination':target,'device':entry.device,'inode':entry.inode},principal); names=[entry.name]
                for index,ref in enumerate(request.get('targets',[])):
                    self._checkpoint(job_id,worker,fence)
                    fs=self.catalog.filesystem(ref['driveId']); key=f'{job_id}:{index}'; policy=request.get('conflictPolicy','ask')
                    if kind=='trash': self._trash(principal,ref['driveId'],ref['path'],ref['revision'],key); names.append(ref['path'].rsplit('/',1)[-1]); continue
                    if kind in ('restore','purge'):
                        self._restore_or_purge(principal,ref,request,key,checkpoint=lambda:self._checkpoint(job_id,worker,fence)); names.append(ref['path'].rsplit('/',1)[-1]); continue
                    dest,target=self._effective_destination(request,ref)
                    self._authorize_destination(principal,dest,target)
                    if kind=='copy' or (kind=='move' and dest['driveId']!=ref['driveId']):
                        published=self._copy(principal,ref,dest,target,policy,job_id,worker,fence,index)
                        if published is None: outcome='skipped'; continue
                        names.append(published.name)
                        if kind=='move':
                            try:
                                self.fault('before-source-cleanup'); self._verify_children(fs,key,lambda:self._checkpoint(job_id,worker,fence)); self._trash(principal,ref['driveId'],ref['path'],ref['revision'],key+':source-cleanup')
                            except Exception:
                                self.state.put('cleanup',job_id,{'ref':ref,'destination':present_entry(published,dest['driveId']),'index':index})
                                raise BackendError('PARTIAL_OUTCOME','Copied; source remains. Retry source cleanup only',True)
                    else:
                        old=self.state.get('intents',key)
                        if old:
                            self._authorize_destination(principal,dest,old['destination'])
                        if old and self._existing(fs,old['destination']) and (fs.stat(old['destination']).device,fs.stat(old['destination']).inode)==(old.get('device'),old.get('inode')):
                            self._done(key,old,principal); names.append(old['destination'].rsplit('/',1)[-1]); continue
                        source=fs.stat(ref['path'])
                        if source.revision!=ref['revision']: raise BackendError('STALE_REVISION','Source changed before rename')
                        if target==ref['path']: raise BackendError('INVALID_ARGUMENT','Source and destination are identical')
                        actual=self._destination(principal,fs,target,policy,key)
                        if actual is None: outcome='skipped'; continue
                        intent={'kind':'rename','status':'intent','drive':ref['driveId'],'source':ref['path'],'destination':actual,'device':source.device,'inode':source.inode}
                        self._intent(key,intent,principal)
                        self._checkpoint(job_id,worker,fence)
                        with self.state.transaction():
                            self.jobs._lease(self.state.get('jobs',job_id),worker,fence)
                            self.auth.authorize(principal,'write',ref['driveId'],ref['path'])
                            self._authorize_destination(principal,dest,actual)
                            renamed=fs.rename_noreplace(ref['path'],actual,ref['revision'])
                            self.fault('after-rename'); self._done(key,intent,principal)
                        names.append(renamed.name)
                self.jobs.transition(job_id,'verifying',worker,fence)
                self.jobs.transition(job_id,'committing',worker,fence)
                current=self.state.get('jobs',job_id)
                for child_id in current['childIds']:
                    with self.state.transaction():
                        self.jobs.heartbeat(job_id,worker,fence)
                        child=self.state.get('jobs',child_id)
                        if child['state']!='completed':
                            child.update(state='completed',outcome='complete'); self.jobs._save(child,'published')
                self.jobs.transition(job_id,'completed',worker,fence,{'resultingNames':names,'outcome':outcome,'bytesTotal':current['bytesDone']})
        except BackendError as exc:
            with self.state.transaction() as c:
                c.execute("DELETE FROM records WHERE namespace='reservations' AND json_extract(body,'$.job')=?",(job_id,))
            if exc.code in ('PAUSED','CANCELED'): return
            current=self.state.get('jobs',job_id)
            if current and current['state'] not in ('completed','failed','canceled'):
                self.jobs.transition(job_id,'failed',worker,fence,{'error':exc.envelope(job_id),'outcome':'copied-source-remains' if exc.code=='PARTIAL_OUTCOME' else 'partial' if names else None,'resultingNames':names})
            raise

    def trash_list(self,principal,drive):
        self.auth.authorize(principal,'trash')
        self.catalog.filesystem(drive)
        result=[]
        for item in self.state.list('trash',limit=10000):
            if item['drive']!=drive: continue
            try: self.auth.authorize(principal,'trash',drive,item['source'])
            except BackendError: continue
            entry=dict(item['entry']); entry['id']=item['token']
            result.append({'entry':entry,'originalPath':item['source'],'deletedAt':item['deletedAt'],'deletedBy':item['deletedBy'],'retainedBytes':entry['sizeBytes']})
        return result

    def _restore_or_purge(self,principal,ref,request,key,checkpoint=None):
        item=self.state.get('trash',ref['id'])
        if item is None or item['drive']!=ref['driveId'] or item['revision']!=ref['revision'] or item['source']!=ref['path']:
            raise BackendError('STALE_REVISION','Trash item changed; refresh')
        fs=self.catalog.filesystem(item['drive']); held=fs.internal_stat('trash',item['token'])
        if (held.device,held.inode)!=(item['device'],item['inode']): raise BackendError('STALE_REVISION','Trash inode changed')
        if request['kind']=='purge':
            self.auth.authorize(principal,'purge',item['drive'],item['source'])
            intent={'kind':'purge','status':'intent','drive':item['drive'],'token':item['token']}
            self._intent(key,intent,principal)
            fs.remove_internal('trash',item['token'],checkpoint=checkpoint); self.fault('after-purge')
        else:
            dest,_=self._effective_destination(request,ref)
            if dest['driveId']!=item['drive']: raise BackendError('INVALID_ARGUMENT','Restore remains on its original drive')
            self.auth.authorize(principal,'write',dest['driveId'],dest['path'])
            if request.get('recreateParents'):
                current=''
                for segment in dest['path'].split('/') if dest['path'] else []:
                    current=join(current,segment)
                    if self._existing(fs,current) is None: fs.create_dir(current)
            target=self._destination(principal,fs,join(dest['path'],item['source'].rsplit('/',1)[-1]),request.get('conflictPolicy','ask'),key)
            if target is None: return
            intent={'kind':'restore','status':'intent','drive':item['drive'],'token':item['token'],'destination':target,'device':held.device,'inode':held.inode}
            self._intent(key,intent,principal); fs.restore_internal('trash',item['token'],target); self.fault('after-restore')
        with self.state.transaction():
            self.state.delete('trash',ref['id']); self._done(key,intent,principal)
