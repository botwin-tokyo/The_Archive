"""Bounded immutable representations, standard single ranges and directory manifests."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import os
import re
import secrets
import threading
import time

from .catalog import present_entry
from .contracts import BackendError, CHUNK_BYTES, bytes_value, canonical, object_fields, translate_error
from .mobile_auth import request_id, stamp
from .mobile_metadata import legacy_ref
from .mobile_spool import SnapshotSpool

TTL=86400


def write_snapshot(fd,data): return os.write(fd,data)


def byte_range(size,header=None,if_range=None,etag=None,method='GET'):
    """RFC 9110: Range applies only to GET; unknown units are ignored."""
    if method!='GET' or not header or not header.startswith('bytes=') or (if_range is not None and if_range!=etag):
        return 200,0,size
    match=re.fullmatch(r'bytes=([0-9]{0,19})-([0-9]{0,19})',header)
    if not match or not any(match.groups()) or size==0: return 416,0,0
    first,last=match.groups()
    if not first:
        suffix=int(last)
        if suffix==0: return 416,0,0
        start=max(0,size-suffix); return 206,start,size-start
    start=int(first); end=int(last) if last else size-1
    if start>=size or end<start: return 416,0,0
    end=min(end,size-1); return 206,start,end-start+1


class MobileDownloads:
    def __init__(self,transfers):
        self.transfers=transfers; self.boundary=transfers.boundary; self.service=transfers.service; self.state=transfers.state
        self.spool=SnapshotSpool(self.state.path,self.service.config.min_free_bytes)
        self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='archive-mobile-snapshot')
        self._futures={}; self._closing=False; self._manifest_slot=threading.BoundedSemaphore(1)
    def close(self):
        self._closing=True; self.executor.shutdown(wait=True,cancel_futures=True); self.spool.close()
    def _context(self,record):
        return self.boundary.auth._context(self.state.get('mobile_sessions',record['sessionId']))
    def _get(self,context,rid,*,source_check=True,allow_expired=False):
        record=self.state.get('mobile_downloads',rid)
        if not record or record['actor']!=context.principal.id or record['sessionId']!=context.session_id:
            raise BackendError('PERMISSION_DENIED','Representation is not available to this device')
        self.service.auth.authorize(context.principal,'read',record['source']['driveId'],record['source']['path'])
        if source_check:
            fs=self.service.catalog.filesystem(record['source']['driveId'])
            if str(fs.volume.generation)!=record['source']['generation']: raise BackendError('VOLUME_CHANGED','Representation source volume changed')
        if not allow_expired and record['expires']<=time.time(): raise BackendError('REPRESENTATION_EXPIRED','Representation expired; request a new captured version')
        return record
    def _check_prepare(self,context,rid):
        if self._closing or self.transfers._closing: raise BackendError('TRANSFER_INTERRUPTED','Snapshot preparation interrupted',True)
        record=self._get(context,rid)
        if record['state']=='canceled': raise BackendError('CANCELED','Representation canceled')
        self.boundary.metadata.validate_ref(context,record['source'])
        return record
    def initiate(self,context,args):
        object_fields(args,('requestId','source')); request=request_id(args['requestId']); legacy_ref(args['source'])
        key=context.principal.id+':'+request; digest=hashlib.sha256(canonical(args).encode()).hexdigest()
        with self.transfers._condition:
            if self._closing or self.transfers._closing: raise BackendError('UNAVAILABLE','Snapshot service is stopping',True)
            with self.state.transaction() as c:
                prior=self.state.get('mobile_download_requests',key)
                if prior:
                    if prior['digest']!=digest: raise BackendError('REQUEST_MISMATCH','Download request is bound to another source')
                    rid=prior['representationId']; self._get(context,rid,source_check=False)
                else:
                    from .mobile_limits import reserve_request
                    reserve_request(self.state)
                    source=self.boundary.metadata.validate_ref(context,args['source'])
                    entry=self.service.catalog.stat(context.principal,source)
                    if entry['kind']!='file': raise BackendError('UNSUPPORTED_ENTRY','Use an explicit directory manifest; folders have no single file checksum')
                    size=bytes_value(entry['sizeBytes'])
                    if size>int(self.boundary.limits['maxAcceptedFileBytes']): raise BackendError('QUOTA_EXCEEDED','File exceeds mobile representation limit')
                    pending=c.execute("SELECT COUNT(*) FROM records WHERE namespace='mobile_downloads' AND json_extract(body,'$.state')='preparing'").fetchone()[0]
                    if pending>=8: raise BackendError('BUSY','Snapshot preparation queue is full',True)
                    self.transfers._reserve(size)
                    reserved=c.execute("SELECT COALESCE(SUM(CAST(json_extract(body,'$.size') AS INTEGER)-CAST(COALESCE(json_extract(body,'$.capturedBytes'),'0') AS INTEGER)),0) FROM records WHERE namespace='mobile_downloads' AND json_extract(body,'$.state')='preparing' AND COALESCE(json_extract(body,'$.released'),0)=0").fetchone()[0]
                    self.spool.ensure_space(size+reserved)
                    payload={'kind':'download','source':{k:source[k] for k in ('driveId','path')},'targets':[source],'bytesTotal':entry['sizeBytes']}
                    receipt=self.service.jobs.accept(context.principal,'download',payload,request,initial_state='waiting-for-client')
                    job_id=receipt['jobIds'][0]; rid=hashlib.sha256(('mobile-snapshot:'+job_id).encode()).hexdigest(); now=time.time()
                    record={'id':rid,'requestId':request,'jobId':job_id,'sessionId':context.session_id,'actor':context.principal.id,'source':args['source'],
                            'size':entry['sizeBytes'],'state':'preparing','created':now,'expires':now+TTL,'released':False,'cleanupState':'pending',
                            'sha256':None,'etag':'"'+secrets.token_hex(24)+'"','fileIdentity':None,'error':None,'complete':None,'capturedBytes':'0'}
                    self.state.put('mobile_downloads',rid,record)
                    self.state.put('mobile_download_requests',key,{'digest':digest,'representationId':rid})
            if not prior:
                future=self.executor.submit(self._prepare,rid); self._futures[rid]=future
                future.add_done_callback(lambda _:self._forget_future(rid))
        # Admission never blocks waiting for preparation and never implicitly
        # re-mints authority on query. Ready clients call /authorize explicitly.
        return self.snapshot(context,rid)
    def _forget_future(self,rid):
        with self.transfers._condition: self._futures.pop(rid,None)
    def _prepare(self,rid):
        context=None; worker=None; lease=None; fd=None; acquired=False
        try:
            while not acquired:
                if self._closing or self.transfers._closing: raise BackendError('TRANSFER_INTERRUPTED','Snapshot preparation interrupted',True)
                acquired=self.transfers._hash_slot.acquire(timeout=.25)
            record=self.state.get('mobile_downloads',rid); context=self._context(record)
            with self.transfers._writer(context,rid,'download','prepare') as worker:
                record=self._check_prepare(context,rid)
                lease=self.service.jobs.claim_id(context.principal,record['jobId'],worker)
                fs=self.service.catalog.filesystem(record['source']['driveId'])
                fd=self.spool.create(rid); total=0; digest=hashlib.sha256()
                with fs.open_read(record['source']['path'],int(record['source']['revision'])) as source:
                    while True:
                        self._check_prepare(context,rid)
                        self.service.jobs.heartbeat(record['jobId'],worker,lease['fence'])
                        data=source.read(CHUNK_BYTES)
                        if not data: break
                        self.spool.ensure_space(len(data))
                        view=memoryview(data)
                        while view:
                            written=write_snapshot(fd,view)
                            if written<=0: raise BackendError('TRANSFER_INTERRUPTED','Snapshot write stopped safely',True)
                            view=view[written:]
                        total+=len(data); digest.update(data)
                        if total>int(record['size']): raise BackendError('SOURCE_CHANGED','Source grew while snapshotting')
                        with self.state.transaction():
                            current=self.state.get('mobile_downloads',rid); current['capturedBytes']=str(total); self.state.put('mobile_downloads',rid,current)
                self._check_prepare(context,rid)
                if total!=int(record['size']): raise BackendError('SOURCE_CHANGED','Source size changed while snapshotting')
                os.fsync(fd)
                with self.state.transaction():
                    current=self._get(context,rid)
                    if current['state']=='canceled': raise BackendError('CANCELED','Representation canceled')
                    current['capturedDigest']=digest.hexdigest(); self.state.put('mobile_downloads',rid,current)
                file_identity=self.spool.promote(rid,fd)
                with self.state.transaction():
                    current=self._get(context,rid)
                    if current['state']=='canceled': raise BackendError('CANCELED','Representation canceled')
                    current.update(state='ready',sha256=digest.hexdigest(),fileIdentity=file_identity,error=None)
                    self.state.put('mobile_downloads',rid,current)
                    self.service.jobs.transition(record['jobId'],'waiting-for-client',worker,lease['fence'])
        except Exception as exc:
            error=translate_error(exc)
            with self.state.transaction():
                record=self.state.get('mobile_downloads',rid)
                if record:
                    canceled=record['state']=='canceled' or error.code=='CANCELED'
                    record.update(state='canceled' if canceled else 'failed',error=None if canceled else error.envelope())
                    self.state.put('mobile_downloads',rid,record)
                    job=self.state.get('jobs',record['jobId'])
                    if job and job['state'] not in ('completed','failed','canceled'):
                        # The sole preparation callback owns the work; do not
                        # accidentally release an unrelated worker's lease.
                        own=job.get('_lease')
                        if not own or (worker and own['worker']==worker):
                            job.update(state='canceled' if canceled else 'failed',_lease=None,_workerStopped=True,error=None if canceled else error.envelope(job['id']))
                            job['_fence']+=1; self.service.jobs._save(job,'mobile-snapshot-stopped')
        finally:
            if fd is not None: os.close(fd)
            if acquired: self.transfers._hash_slot.release()
            # _writer has finished and no descriptor can still write the spool.
            with self.transfers._condition:
                record=self.state.get('mobile_downloads',rid)
                if record and record['state'] in ('failed','canceled'): self._cleanup(record)
                self._futures.pop(rid,None)
    def snapshot(self,context,rid,*,mint=False):
        with self.transfers._condition:
            with self.state.transaction():
                record=self._get(context,rid,source_check=False,allow_expired=True)
                base={'schemaVersion':1,'requestId':record['requestId'],'representationId':rid,'serverJobId':record['jobId'],'cleanupState':record['cleanupState']}
                if record['state']=='canceled':
                    from .mobile_contracts import mobile_error, stored_error
                    error=mobile_error(BackendError('CLEANUP_PENDING','Representation cleanup requires retry'))['error'] if record['cleanupState']=='failed' else None
                    if error: error.update(code='cleanup_pending',recovery='retry',retryable=False)
                    return dict(base,state='canceled',error=error)
                if record['expires']<=time.time(): raise BackendError('REPRESENTATION_EXPIRED','Representation expired; request a new captured version')
                if record['state']=='failed':
                    from .mobile_contracts import stored_error
                    raise stored_error(record['error'])
                if record['state']=='preparing': return dict(base,state='preparing',pollAfterMs=1000)
                self._get(context,rid)
                # Verify metadata before reporting a content endpoint as usable.
                with self.spool.open(record): pass
                grant={'transferToken':None,'transferExpiresAt':None}
                if mint:
                    issued=self.boundary.auth.issue_transfer(context,'download',rid,0,int(record['size']),expires_at=record['expires'])
                    grant={'transferToken':issued['transferToken'],'transferExpiresAt':issued['expiresAt']}
                return dict(base,state='ready',source=record['source'],bytes=record['size'],strongETag=record['etag'],sha256=record['sha256'],
                            mediaTypeHint=None,expiresAt=stamp(record['expires']),contentPath='/mobile/v1/downloads/'+rid+'/content',
                            **grant,immutableSnapshot=True,redirectFree=True)
    def _cleanup(self,record):
        rid=record['id']
        if rid in self.transfers._active: return
        try:
            self.spool.remove(rid)
            record.update(released=True,cleanupState='confirmed')
        except Exception as exc:
            record.update(cleanupState='failed',cleanupError=translate_error(exc).envelope())
        self.state.put('mobile_downloads',rid,record)
    def cancel(self,context,rid):
        with self.transfers._condition:
            with self.state.transaction():
                record=self._get(context,rid,source_check=False,allow_expired=True)
                record.update(state='canceled',cleanupState='pending' if not record['released'] else 'confirmed')
                self.state.put('mobile_downloads',rid,record)
                future=self._futures.get(rid)
                if future and future.cancel(): self._futures.pop(rid,None)
                if rid not in self.transfers._active:
                    job=self.state.get('jobs',record['jobId'])
                    if job and job['state'] not in ('completed','failed','canceled') and not job.get('_lease'):
                        job.update(state='canceled',_fence=job.get('_fence',0)+1); self.service.jobs._save(job,'mobile-download-cancel')
                    self._cleanup(record)
                return self.snapshot(context,rid)
    @contextmanager
    def content(self,token,rid,range_header=None,if_range=None,method='GET'):
        context=self.boundary.auth.authenticate_transfer(token,'download',rid)
        record=self._get(context,rid)
        if record['state']!='ready': raise BackendError('CONFLICT','Representation is not readable')
        status,start,length=byte_range(int(record['size']),range_header,if_range,record['etag'],method)
        self.boundary.auth.authenticate_transfer(token,'download',rid,start,length)
        headers={'ETag':record['etag'],'Accept-Ranges':'bytes'}
        if status==206: headers['Content-Range']=f"bytes {start}-{start+length-1}/{record['size']}"
        elif status==416: headers['Content-Range']='bytes */'+record['size']
        try:
            with self.transfers._writer(context,rid,'download','content'):
                try:
                    with self.spool.open(record) as fd:
                        os.lseek(fd,start,os.SEEK_SET)
                        def chunks():
                            remaining=length; position=start
                            while remaining:
                                if self.transfers._closing: raise BackendError('TRANSFER_INTERRUPTED','Service stopping',True)
                                self.boundary.auth.authenticate_transfer(token,'download',rid,position,remaining)
                                current=self._get(context,rid)
                                if current['state']!='ready': raise BackendError('CANCELED','Representation canceled')
                                job=self.service.jobs.get(context.principal,current['jobId'])
                                if job['state'] in ('paused','pausing','canceling','canceled','failed'):
                                    raise BackendError('CONFLICT','Download job stopped; reconcile before retrying')
                                self.spool.check(fd,record)
                                data=os.read(fd,min(CHUNK_BYTES,remaining))
                                if not data: raise BackendError('INTEGRITY_FAILED','Snapshot content ended unexpectedly')
                                self.spool.check(fd,record)
                                position+=len(data); remaining-=len(data)
                                yield data
                        yield status,length,headers,chunks()
                finally: pass
        finally:
            with self.transfers._condition:
                current=self.state.get('mobile_downloads',rid)
                if current and current['state']=='canceled': self._cleanup(current)
    def complete(self,context,rid,args):
        object_fields(args,('requestId','bytes','sha256')); request_id(args['requestId']); bytes_value(args['bytes'])
        with self.transfers._condition:
            with self.state.transaction():
                record=self._get(context,rid,source_check=False,allow_expired=True)
                digest=hashlib.sha256(canonical(args).encode()).hexdigest()
                if record.get('complete'):
                    if record['complete']['digest']!=digest: raise BackendError('REQUEST_MISMATCH','Completion is already bound to another payload')
                    return record['complete']['receipt']
                if record['state']!='ready' or record['expires']<=time.time(): raise BackendError('REPRESENTATION_EXPIRED','Prepared representation is no longer available')
                if args['bytes']!=record['size'] or args['sha256']!=record['sha256']:
                    raise BackendError('INTEGRITY_FAILED','Client completion does not match representation size/checksum')
                with self.spool.open(record): pass
                self._get(context,rid)
                lease=self.service.jobs.claim_id(context.principal,record['jobId'],'mobile-download-ack-'+secrets.token_hex(16))
                worker,fence=lease['workerId'],lease['fence']
                for state in ('running','verifying','committing','completed'):
                    self.service.jobs.transition(record['jobId'],state,worker,fence,{'bytesDone':record['size'],'outcome':'complete'} if state=='completed' else None)
                receipt={'schemaVersion':1,'requestId':args['requestId'],'representationId':rid,'acknowledged':True}
                record['complete']={'digest':digest,'receipt':receipt}; self.state.put('mobile_downloads',rid,record)
                return receipt
    def _prior_manifest(self,context,key,digest):
        prior=self.state.get('mobile_manifests',key)
        if prior:
            if prior['digest']!=digest: raise BackendError('REQUEST_MISMATCH','Manifest request is bound to another source')
            if prior['expires']<=time.time(): raise BackendError('REPRESENTATION_EXPIRED','Manifest expired; select a new source snapshot')
            generations={}
            for item in prior['result']['entries']:
                self.service.auth.authorize(context.principal,'read',item['source']['driveId'],item['source']['path'])
                drive=item['source']['driveId']; generation=item['source']['generation']
                if drive in generations and generations[drive]!=generation: raise BackendError('INTEGRITY_FAILED','Manifest contains inconsistent volume generations')
                generations[drive]=generation
            for drive,generation in generations.items():
                if str(self.service.catalog.filesystem(drive).volume.generation)!=generation:
                    raise BackendError('VOLUME_CHANGED','Manifest source volume changed; select a new captured tree')
            return prior['result']
        return None
    def manifest(self,context,args):
        object_fields(args,('requestId','source')); request=request_id(args['requestId']); legacy_ref(args['source'])
        key=context.principal.id+':'+request; digest=hashlib.sha256(canonical(args).encode()).hexdigest()
        prior=self._prior_manifest(context,key,digest)
        if prior is not None: return prior
        if not self._manifest_slot.acquire(blocking=False): raise BackendError('BUSY','A bounded manifest scan is already active',True)
        try: return self._capture_manifest(context,args,key,digest,request)
        finally: self._manifest_slot.release()
    def _capture_manifest(self,context,args,key,digest,request):
        # Filesystem traversal is outside an outer SQLite writer transaction.
        prior=self._prior_manifest(context,key,digest)
        if prior is not None: return prior
        source=self.boundary.metadata.validate_ref(context,args['source']); fs=self.service.catalog.filesystem(source['driveId'])
        root=fs.stat(source['path'])
        if root.kind!='directory': raise BackendError('INVALID_ARGUMENT','Directory manifest requires a folder')
        entries=[{'relativePath':'','kind':'directory','source':args['source'],'bytes':None}]
        queue=[(root,'',0)]; visited=[]; total=0; files=0; directories=1
        while queue:
            folder,relative,depth=queue.pop()
            if depth>64: raise BackendError('SCAN_LIMIT','Directory manifest depth exceeds 64')
            self.service.auth.authorize(context.principal,'read',source['driveId'],folder.path)
            for item in fs.iter_entries(folder.path):
                if depth+1>64: raise BackendError('SCAN_LIMIT','Directory manifest depth exceeds 64')
                if len(entries)>=1000: raise BackendError('SCAN_LIMIT','Directory manifest exceeds 1000 entries')
                self.service.auth.authorize(context.principal,'read',source['driveId'],item.path)
                if item.kind not in ('file','directory'): raise BackendError('UNSUPPORTED_ENTRY','Directory manifest contains an unsupported entry')
                rel=relative+'/'+item.name if relative else item.name
                if not rel or rel.startswith('/') or re.match(r'^[A-Za-z]:',rel) or any(p in ('','.','..') for p in rel.split('/')) or re.search(r'[\\\x00-\x1f]',rel):
                    raise BackendError('UNSUPPORTED_ENTRY','Entry name cannot be represented safely by mobile v1')
                raw=present_entry(item,source['driveId'],self.service.auth,context.principal); ref=self.boundary.metadata.ref(raw)
                entries.append({'relativePath':rel,'kind':'directory' if item.kind=='directory' else 'file','source':ref,'bytes':str(item.size) if item.kind=='file' else None})
                if item.kind=='directory': directories+=1; queue.append((item,rel,depth+1))
                else:
                    files+=1; total+=item.size
                    if total>2**63-1: raise BackendError('SCAN_LIMIT','Directory manifest byte count exceeds the contract')
            if fs.stat(folder.path).revision!=folder.revision: raise BackendError('SOURCE_CHANGED','Directory changed during manifest capture')
            visited.append(folder)
        for folder in visited:
            if fs.stat(folder.path).revision!=folder.revision: raise BackendError('SOURCE_CHANGED','Directory changed during manifest capture')
        for item in entries: self.boundary.metadata.validate_ref(context,item['source'])
        expires=time.time()+TTL
        result={'schemaVersion':1,'requestId':request,'manifestId':hashlib.sha256(key.encode()).hexdigest(),'source':args['source'],
                'entries':entries,'totalBytes':str(total),'fileCount':files,'directoryCount':directories,'expiresAt':stamp(expires),'consistency':'captured_entry_refs'}
        if len(canonical(result).encode())>2*1024*1024: raise BackendError('SCAN_LIMIT','Directory manifest exceeds mobile response budget')
        with self.state.transaction():
            prior=self._prior_manifest(context,key,digest)
            if prior is not None: return prior
            self.service.auth.refresh(context.principal)
            from .mobile_limits import reserve_request
            reserve_request(self.state)
            self.state.put('mobile_manifests',key,{'actor':context.principal.id,'digest':digest,'expires':expires,'result':result})
        return result
