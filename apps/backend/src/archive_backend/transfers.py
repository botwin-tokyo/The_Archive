"""Sequential resumable byte sessions. Acknowledgments follow data and metadata flush."""
import hashlib
import time
import uuid
from .contracts import BackendError, CHUNK_BYTES, bytes_value, location, name, entry_ref
from .operations import join
from .catalog import present_entry


class Transfers:
    def __init__(self,state,auth,jobs,catalog,operations,*,fault=lambda _:None,max_session_bytes=2**40,max_sessions=1024,min_free_bytes=67108864):
        self.state,self.auth,self.jobs,self.catalog,self.operations=state,auth,jobs,catalog,operations
        self.fault,self.max_session_bytes,self.max_sessions,self.min_free_bytes=fault,max_session_bytes,max_sessions,min_free_bytes
        # Completed per-file history must not slow every subsequent folder file.
        with state.transaction() as connection:
            connection.execute("CREATE INDEX IF NOT EXISTS active_transfer_actor ON records(json_extract(body,'$.actor')) WHERE namespace='transfers' AND json_extract(body,'$.state') NOT IN ('completed','discarded')")
            connection.execute("CREATE INDEX IF NOT EXISTS active_transfer_drive ON records(json_extract(body,'$.drive')) WHERE namespace='transfers' AND json_extract(body,'$.state') NOT IN ('completed','discarded')")
            connection.execute("CREATE INDEX IF NOT EXISTS active_upload_jobs ON records(namespace,key) WHERE namespace='jobs' AND json_extract(body,'$.state') NOT IN ('completed','failed','canceled')")

    def _session(self,principal,transfer_id,scope='write'):
        record=self.state.get('transfers',transfer_id)
        fresh=self.auth.refresh(principal)
        if record is None or (fresh.kind!='owner' and record['actor']!=fresh.id): raise BackendError('PERMISSION_DENIED','Transfer is not available to this identity')
        self.auth.authorize(fresh,scope,record['drive'],record['path'])
        self.jobs.get(fresh,record['jobId'])
        if record.get('folderId') and record['state']!='completed':
            from .folder_uploads import writable
            writable(self,fresh,record['folderId'])
        return record

    def initiate(self,principal,args):
        location(args['destination']); name(args['name']); total=bytes_value(args['sizeBytes'])
        if total>self.max_session_bytes: raise BackendError('QUOTA_EXCEEDED','Declared transfer exceeds session budget')
        policy=args.get('conflictPolicy','ask')
        if policy not in ('ask','skip','keep-both','replace'): raise BackendError('INVALID_ARGUMENT','Invalid conflict policy')
        expected=args.get('sha256')
        if expected is not None and (not isinstance(expected,str) or len(expected)!=64 or any(c not in '0123456789abcdef' for c in expected)): raise BackendError('INVALID_ARGUMENT','SHA-256 must be lowercase hexadecimal')
        drive=args['destination']['driveId']; target=join(args['destination']['path'],args['name'])
        fresh=self.auth.authorize(principal,'write',drive,target); fs=self.catalog.filesystem(drive)
        folder_id=args.get('folderId')
        if folder_id:
            from .folder_uploads import writable
            parent=writable(self,principal,folder_id)
            if drive!=parent['drive'] or not target.startswith(parent['path']+'/'):
                raise BackendError('PERMISSION_DENIED','File is outside its selected folder destination')
        payload={'kind':'upload','destination':args['destination'],'name':args['name'],'bytesTotal':args['sizeBytes'],'sha256':expected,'conflictPolicy':policy,'parentRequestId':args.get('parentRequestId')}
        if folder_id:payload.update(folderId=folder_id,parentId=folder_id)
        with self.state.transaction():
            receipt=self.jobs.accept(fresh,'upload',payload,args['requestId'],initial_state='waiting-for-client')
            job_id=receipt['jobIds'][0]; transfer_id=hashlib.sha256(('upload:'+job_id).encode()).hexdigest()
            existing=self.state.get('transfers',transfer_id)
            if existing: return self.query(fresh,transfer_id)
            active=self.state.connection.execute("SELECT COUNT(*) FROM records WHERE namespace='transfers' AND json_extract(body,'$.actor')=? AND json_extract(body,'$.state') NOT IN ('completed','discarded')",(fresh.id,)).fetchone()[0]
            if active>=self.max_sessions: raise BackendError('QUOTA_EXCEEDED','Too many retained transfer sessions')
            available=fs.volume.capacity().get('availableBytes',fs.volume.capacity().get('available'))
            reserved=self.state.connection.execute("SELECT COALESCE(SUM(CAST(json_extract(body,'$.size') AS INTEGER)-CAST(json_extract(body,'$.offset') AS INTEGER)),0) FROM records WHERE namespace='transfers' AND json_extract(body,'$.drive')=? AND json_extract(body,'$.state') NOT IN ('completed','discarded')",(drive,)).fetchone()[0]
            reserved+=self.state.connection.execute("SELECT COALESCE(SUM(CAST(json_extract(body,'$.bytes') AS INTEGER)),0) FROM records WHERE namespace='reservations' AND json_extract(body,'$.drive')=?",(drive,)).fetchone()[0]
            if available is not None and int(available)<total+reserved+self.min_free_bytes: raise BackendError('INSUFFICIENT_SPACE','Insufficient reserved destination space')
            record={'id':transfer_id,'actor':fresh.id,'credential':fresh.credential_id,'drive':drive,'path':target,'jobId':job_id,'size':str(total),'offset':'0','generation':fs.volume.generation,'expected':expected,'state':'waiting-for-client','direction':'upload','policy':policy,'requestId':args['requestId'],'parentRequestId':args.get('parentRequestId'),'lastRange':None,'entry':None}
            if folder_id:record['folderId']=folder_id
            self.state.put('transfers',transfer_id,record)
        return self.query(fresh,transfer_id)

    def resume_info(self,principal,job_id):
        import json
        job=self.jobs.get(principal,job_id)
        if self.state.get('folder_uploads',job_id):
            from .folder_uploads import status
            return dict(status(self,principal,job_id),direction='folder',jobId=job_id)
        if job['operation'] not in ('upload','download'): raise BackendError('UNSUPPORTED_ENTRY','Native byte transfer job required')
        with self.state.transaction() as c:
            row=c.execute("SELECT key FROM records WHERE namespace='transfers' AND json_extract(body,'$.jobId')=?",(job_id,)).fetchone()
        if row is None: raise BackendError('NOT_FOUND','Transfer session is unavailable')
        record=self._session(principal,row[0],'read' if job['operation']=='download' else 'write')
        return {'transferId':record['id'],'jobId':record['jobId'],'direction':record.get('direction','upload'),
                'name':record['path'].rsplit('/',1)[-1],'sizeBytes':record['size'],'offset':record['offset'],
                'sha256':record.get('expected'),'ref':record.get('ref'),'state':record['state']}

    def restart_download(self,principal,transfer_id):
        record=self._session(principal,transfer_id,'read')
        if record.get('direction')!='download': raise BackendError('INVALID_ARGUMENT','Download session required')
        self.catalog.stat(principal,record['ref'])
        fs=self.catalog.filesystem(record['drive']); generation=fs.volume.generation
        with self.state.transaction():
            job=self.state.get('jobs',record['jobId'])
            if job.get('_lease') or job['state'] not in ('paused','waiting-for-client','failed','queued'):
                raise BackendError('BUSY','Download cannot restart while a writer is active or after completion',True)
            # Explicitly restarting a selected local destination is a new recorded
            # attempt on the same source/session, never a claim to retain lost bytes.
            fs.volume.check(generation)
            record.update(offset='0',state='waiting-for-client',generation=generation,revision=record['ref']['revision'])
            self.state.put('transfers',transfer_id,record)
            job.update(bytesDone='0',state='waiting-for-client',error=None,_lease=None,_fence=job.get('_fence',0)+1)
            job['_attempt']=job.get('_attempt',0)+1
            self.state.put('attempts',job['id']+':'+str(job['_attempt']),{'jobId':job['id'],'attempt':job['_attempt'],'reason':'explicit-native-download-restart'})
            self.jobs._save(job,'download-restart')
        return self.query(principal,transfer_id)

    def query(self,principal,transfer_id):
        record=self.state.get('transfers',transfer_id)
        scope='read' if record and record.get('direction')=='download' else 'write'
        record=self._session(principal,transfer_id,scope)
        return {'transferId':record['id'],'jobId':record['jobId'],'offset':record['offset'],'sizeBytes':record['size'],'chunkBytes':CHUNK_BYTES,'state':record['state'],'entry':record.get('entry'),'verification':record.get('verification')}

    def write(self,principal,transfer_id,offset,data,*,worker_id=None):
        if type(offset) is not int or offset<0 or not isinstance(data,bytes) or len(data)>CHUNK_BYTES: raise BackendError('INVALID_ARGUMENT','Invalid bounded chunk')
        record=self._session(principal,transfer_id)
        if record.get('direction','upload')!='upload': raise BackendError('INVALID_ARGUMENT','Upload session required')
        if record['state'] in ('completed','discarded','canceled'): raise BackendError('CONFLICT','Transfer no longer accepts bytes')
        expected=int(record['offset']); checksum=hashlib.sha256(data).hexdigest()
        # Reject a stale volume before claiming a writer. A failed validation
        # must not strand the job with a live-process lease.
        fs=self.catalog.filesystem(record['drive']); fs.volume.check(record['generation'])
        if offset!=expected:
            previous=record['lastRange']
            if previous and previous=={'offset':offset,'size':len(data),'sha256':checksum}: return {'transferId':transfer_id,'offset':record['offset']}
            raise BackendError('CONFLICT','Chunk offset differs from authoritative durable offset')
        if expected+len(data)>int(record['size']): raise BackendError('QUOTA_EXCEEDED','Chunk exceeds declared transfer size')
        worker=worker_id or 'upload-'+uuid.uuid4().hex
        with self.state.transaction():
            record=self._session(principal,transfer_id)
            expected=int(record['offset'])
            if offset!=expected:
                if record['lastRange']=={'offset':offset,'size':len(data),'sha256':checksum}: return {'transferId':transfer_id,'offset':record['offset']}
                raise BackendError('CONFLICT','Concurrent chunk changed the durable offset')
            lease=self.jobs.claim_id(principal,record['jobId'],worker)
            record=self._session(principal,transfer_id)
            if offset!=int(record['offset']): raise BackendError('CONFLICT','A concurrent range was acknowledged before lease acquisition')
            expected=int(record['offset'])
        fence=lease['fence']; self.jobs.transition(record['jobId'],'running',worker,fence)
        try:
            fs.volume.check(record['generation'])
            available=fs.volume.capacity().get('availableBytes',fs.volume.capacity().get('available'))
            if available is not None and int(available)<len(data)+self.min_free_bytes: raise BackendError('INSUFFICIENT_SPACE','Destination minimum free-space reserve reached')
            try: staged_file=fs.open_staging(transfer_id)
            except BackendError as e:
                if e.code!='NOT_FOUND' or expected!=0: raise
                staged_file=fs.create_staging(transfer_id)
            with staged_file as staged:
                staged.truncate(expected); staged.seek(expected)
                written=staged.write(data)
                if written!=len(data): raise BackendError('TRANSFER_INTERRUPTED','Short write; offset remains unchanged',True)
                staged.flush()
            self.fault('after-chunk-flush')
            self.auth.authorize(principal,'write',record['drive'],record['path']); fs.volume.check(record['generation'])
            with self.state.transaction():
                self.jobs._lease(self.state.get('jobs',record['jobId']),worker,fence)
                record['offset']=str(expected+len(data)); record['lastRange']={'offset':offset,'size':len(data),'sha256':checksum}; record['state']='waiting-for-client'
                self.state.put('transfers',transfer_id,record)
                self.jobs.transition(record['jobId'],'waiting-for-client',worker,fence,{'bytesDone':record['offset']})
                if record.get('folderId'):
                    parent=self.state.get('folder_uploads',record['folderId'])
                    job=self.state.get('jobs',record['folderId'])
                    # Folder uploads stream one child at a time. Show durable
                    # in-flight bytes as well as completely verified files.
                    job['bytesDone']=str(int(parent['completedBytes'])+int(record['offset']))
                    self.jobs._save(job,'folder-byte-progress')
            return {'transferId':transfer_id,'offset':record['offset']}
        except Exception as error:
            if isinstance(error,BackendError):
                try: self.jobs.transition(record['jobId'],'failed',worker,fence,{'error':error.envelope(record['jobId'])})
                except BackendError: pass
            raise

    def finalize(self,principal,transfer_id,*,worker_id=None):
        if self.state.get('intents','transfer:'+transfer_id):
            from .recovery import reconcile_transfer
            # Recovery verifies authorization, exact inode and durable checksum first.
            self._session(principal,transfer_id)
            from types import SimpleNamespace
            reconcile_transfer(SimpleNamespace(state=self.state,auth=self.auth,jobs=self.jobs,catalog=self.catalog),transfer_id)
        record=self._session(principal,transfer_id)
        if record.get('direction','upload')!='upload': raise BackendError('INVALID_ARGUMENT','Upload session required')
        if record['state']=='completed': return {'entry':record['entry'],'jobId':record['jobId'],'verification':record['verification']}
        if int(record['offset'])!=int(record['size']): raise BackendError('TRANSFER_INTERRUPTED','Upload is incomplete; query durable offset',True)
        fs=self.catalog.filesystem(record['drive']); fs.volume.check(record['generation'])
        worker=worker_id or 'finalize-'+uuid.uuid4().hex; lease=self.jobs.claim_id(principal,record['jobId'],worker); fence=lease['fence']
        self.jobs.transition(record['jobId'],'running',worker,fence)
        digest=hashlib.sha256(); count=0
        if int(record['size'])==0:
            try:
                with fs.create_staging(transfer_id) as empty: empty.flush()
            except BackendError as e:
                if e.code!='CONFLICT': raise
        with fs.read_internal('staging',transfer_id) as staged:
            while True:
                self.jobs.heartbeat(record['jobId'],worker,fence)
                self.auth.authorize(principal,'write',record['drive'],record['path'])
                chunk=staged.read(CHUNK_BYTES)
                if not chunk: break
                count+=len(chunk); digest.update(chunk)
        actual=digest.hexdigest()
        if count!=int(record['size']) or (record['expected'] is not None and actual!=record['expected']):
            error=BackendError('INTEGRITY_FAILED','Upload failed declared size or SHA-256 verification')
            self.jobs.transition(record['jobId'],'failed',worker,fence,{'error':error.envelope(record['jobId'])})
            raise error
        record['verification']={'algorithm':'sha256','digest':actual,'bytes':str(count),'level':'destination-sha256'}
        self.state.put('transfers',transfer_id,record)
        self.jobs.transition(record['jobId'],'verifying',worker,fence)
        self.jobs.transition(record['jobId'],'committing',worker,fence)
        try:
            with self.operations._locks(record['jobId'],worker,fence,[{'driveId':record['drive'],'path':record['path']}]):
                published=self.operations._publish(principal,fs,transfer_id,record['path'],record['policy'],'transfer:'+transfer_id,record['jobId'],worker,fence)
        except BackendError as error:
            self.jobs.transition(record['jobId'],'failed',worker,fence,{'error':error.envelope(record['jobId'])})
            raise
        if published is None:
            self.jobs.transition(record['jobId'],'completed',worker,fence,{'outcome':'skipped'})
            record['state']='completed'; record['entry']=None
        else:
            record['entry']=present_entry(published,record['drive'],self.auth,principal); record['state']='completed'
        record['verification']={'algorithm':'sha256','digest':actual,'bytes':str(count),'level':'destination-sha256'}
        self.fault('before-transfer-terminal')
        with self.state.transaction():
            self.state.put('transfers',transfer_id,record)
            if published is not None: self.jobs.transition(record['jobId'],'completed',worker,fence,{'outcome':'complete','resultingNames':[published.name]})
            self.state.audit(principal.id,'upload.finalize','completed',transfer_id)
        return {'entry':record['entry'],'jobId':record['jobId'],'verification':record['verification']}

    def cancel(self,principal,transfer_id,discard=False):
        record=self._session(principal,transfer_id)
        if record['state']=='completed' and record.get('entry') is not None: raise BackendError('CONFLICT','Completed files cannot be discarded through transfer controls')
        job=self.jobs.get(principal,record['jobId'])
        if job['state'] not in ('completed','canceled','failed'):
            self.jobs.control(principal,record['jobId'],'cancel')
        record['state']='canceled'
        if discard:
            self.state.audit(principal.id,'transfer.discard','intent',transfer_id)
            try: self.catalog.filesystem(record['drive']).discard_staging(transfer_id)
            except BackendError as e:
                if e.code!='NOT_FOUND': raise
            record['state']='discarded'
        self.state.put('transfers',transfer_id,record)
        if job['state'] not in ('completed','canceled','failed'): self.jobs.acknowledge_cancel(principal,record['jobId'])
        return self.query(principal,transfer_id)

    def batch_initiate(self,principal,request_id,files):
        if not isinstance(files,list) or not 1<=len(files)<=1000: raise BackendError('INVALID_ARGUMENT','A bounded file manifest is required')
        sessions=[]
        with self.state.transaction():
            for index,item in enumerate(files):
                from .contracts import object_fields
                object_fields(item,('destination','name','sizeBytes'),('sha256','conflictPolicy','relativePath'))
                args={k:v for k,v in item.items() if k!='relativePath'}
                args.update(requestId=f'{request_id}:{index}',parentRequestId=request_id)
                sessions.append(self.initiate(principal,args))
        return {'requestId':request_id,'sessions':sessions,'jobIds':[s['jobId'] for s in sessions]}

    def download(self,principal,ref,request_id,parent_request_id=None):
        entry_ref(ref)
        fs=self.catalog.filesystem(ref['driveId']); generation=fs.volume.generation
        entry=self.catalog.stat(principal,ref)
        if entry['kind'] not in ('file','directory'): raise BackendError('UNSUPPORTED_ENTRY','Download regular files or directory metadata through bounded sessions')
        size=entry['sizeBytes'] if entry['kind']=='file' else '0'
        payload={'kind':'download','source':{'driveId':ref['driveId'],'path':ref['path']},'targets':[ref],'bytesTotal':size,'parentRequestId':parent_request_id}
        with self.state.transaction():
            fs.volume.check(generation)
            receipt=self.jobs.accept(principal,'download',payload,request_id,initial_state='waiting-for-client')
            job_id=receipt['jobIds'][0]; tid=hashlib.sha256(('download:'+job_id).encode()).hexdigest()
            if self.state.get('transfers',tid) is None:
                self.state.put('transfers',tid,{'id':tid,'actor':principal.id,'drive':ref['driveId'],'path':ref['path'],'jobId':job_id,'size':size,'offset':'0','generation':generation,'revision':ref['revision'],'state':'waiting-for-client','direction':'download','ref':ref,'requestId':request_id,'parentRequestId':parent_request_id})
        return {'jobId':job_id,'transferId':tid,'sizeBytes':size,'revision':ref['revision']}

    def batch_download(self,principal,request_id,refs):
        if not isinstance(refs,list) or not 1<=len(refs)<=1000: raise BackendError('INVALID_ARGUMENT','Bounded download manifest required')
        with self.state.transaction():
            sessions=[self.download(principal,ref,f'{request_id}:{i}',request_id) for i,ref in enumerate(refs)]
        return {'sessions':sessions,'jobIds':[s['jobId'] for s in sessions]}

    def read(self,principal,drive,logical_path,revision,offset,length):
        if type(offset) is not int or offset<0 or type(length) is not int or not 0<=length<=CHUNK_BYTES: raise BackendError('INVALID_ARGUMENT','Invalid download range')
        self.auth.authorize(principal,'read',drive,logical_path); fs=self.catalog.filesystem(drive)
        with fs.open_read(logical_path,revision) as source:
            source.seek(offset); data=source.read(length)
            self.auth.authorize(principal,'read',drive,logical_path)
            return data

    def download_progress(self,principal,transfer_id,offset,complete):
        record=self._session(principal,transfer_id,'read')
        if record.get('direction')!='download': raise BackendError('INVALID_ARGUMENT','Download session required')
        amount=bytes_value(offset)
        if not int(record['offset'])<=amount<=int(record['size']) or (complete and amount!=int(record['size'])): raise BackendError('INVALID_ARGUMENT','Invalid acknowledged download progress')
        if record['state']=='completed': return self.query(principal,transfer_id)
        self.catalog.stat(principal,record['ref'])
        worker='download-'+uuid.uuid4().hex; lease=self.jobs.claim_id(principal,record['jobId'],worker); fence=lease['fence']
        self.jobs.transition(record['jobId'],'running',worker,fence)
        record['offset']=offset; record['state']='completed' if complete else 'waiting-for-client'
        with self.state.transaction():
            self.state.put('transfers',transfer_id,record)
            if complete:
                self.jobs.transition(record['jobId'],'verifying',worker,fence,{'bytesDone':offset})
                self.jobs.transition(record['jobId'],'committing',worker,fence)
                self.jobs.transition(record['jobId'],'completed',worker,fence,{'outcome':'complete'})
            else: self.jobs.transition(record['jobId'],'waiting-for-client',worker,fence,{'bytesDone':offset})
        return self.query(principal,transfer_id)
