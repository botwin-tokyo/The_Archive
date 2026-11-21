"""Core-only publication through the existing verified resumable transfer path."""
import hashlib
import os
import stat
from .contracts import BackendError, CHUNK_BYTES
from .filesystem import openat2


class VideoImports:
    def __init__(self, service, config): self.service,self.config=service,config

    def present(self, principal, track, destination):
        key=hashlib.sha256((destination['driveId']+':'+track['_path']).encode()).hexdigest()
        record=self.service.state.get('video_library',key)
        if record and record.get('source')==track['_source'] and record.get('choiceId')==track['choice']['id']:
            try:
                self.service.catalog.stat(principal,record['entry'])
                return record['entry']
            except BackendError as error:
                if error.code not in ('NOT_FOUND','STALE_REVISION'): raise
        return None

    def publish(self, principal, job, track, save):
        service=self.service; drive=job['destination']['driveId']
        service.auth.authorize(principal,'acquire'); service.auth.authorize(principal,'write',drive,track['_path'])
        # Resume a completed transfer before consulting a possibly removed spool artifact.
        if track.get('_transfer'):
            receipt=service.transfers.query(principal,track['_transfer'])
            if receipt['state']=='completed': return self.record(track,drive,receipt['entry'],principal)
        directory,_,name=track['_path'].rpartition('/')
        if service.catalog.filesystem(drive).stat(directory).kind!='directory': raise BackendError('CONFLICT','Destination is not a folder')
        root=os.open(self.config['spool'],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try: fd=openat2(root,track['_task']+'/'+track['_artifact']['filename'],os.O_RDONLY|os.O_NOFOLLOW)
        finally: os.close(root)
        with os.fdopen(fd,'rb') as source:
            before=os.fstat(source.fileno()); artifact=track['_artifact']
            if not stat.S_ISREG(before.st_mode) or before.st_uid!=self.config['workerUid'] or before.st_nlink!=1 or before.st_mode & 0o027 or not 0<before.st_size<=8*1024**3 or str(before.st_size)!=artifact['sizeBytes']:
                raise BackendError('INTEGRITY_FAILED','The video artifact is not a protected regular file')
            checksum=hashlib.sha256()
            while chunk:=source.read(CHUNK_BYTES):
                if service.video.stop.is_set():raise BackendError('BUSY','Video importer stopping',True)
                checksum.update(chunk)
            if checksum.hexdigest()!=artifact['sha256']: raise BackendError('INTEGRITY_FAILED','Video source verification failed')
            if not track.get('_transfer'):
                receipt=service.transfers.initiate(principal,{'requestId':'video-import-'+job['id']+'-'+str(track['_position'])+'-'+str(track['_attempt']),
                    'destination':{'driveId':drive,'path':directory},'name':name,'sizeBytes':artifact['sizeBytes'],
                    'sha256':artifact['sha256'],'conflictPolicy':'ask','parentRequestId':job['id']})
                track['_transfer']=receipt['transferId']; save()
            else: receipt=service.transfers.query(principal,track['_transfer'])
            offset=int(receipt['offset']); source.seek(offset)
            while chunk:=source.read(CHUNK_BYTES):
                if service.video.stop.is_set(): raise BackendError('BUSY','Video import is stopping',True)
                service.auth.authorize(principal,'acquire')
                service.transfers.write(principal,track['_transfer'],offset,chunk); offset+=len(chunk)
            after=os.fstat(source.fileno())
            if (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                raise BackendError('INTEGRITY_FAILED','Video artifact changed while importing')
            result=service.transfers.finalize(principal,track['_transfer'])
            return self.record(track,drive,result['entry'],principal)

    def record(self, track, drive, entry, principal):
        if not entry: raise BackendError('CONFLICT','Video import did not publish a file')
        ref={k:entry[k] for k in ('id','driveId','path','revision')}
        key=hashlib.sha256((drive+':'+track['_path']).encode()).hexdigest()
        self.service.state.put('video_library',key,{'entry':ref,'sha256':track['_artifact']['sha256'],'source':track['_source'],'choiceId':track['choice']['id']})
        return ref
