"""Core-only publication through the existing verified resumable transfer path."""
import hashlib
import os
import stat
from .contracts import BackendError, CHUNK_BYTES
from .filesystem import openat2
from .music_contracts import SUCCESS


class MusicImports:
    def __init__(self, service, config): self.service,self.config=service,config

    def present(self, principal, track, destination):
        key=hashlib.sha256((destination['driveId']+':'+track['_path']).encode()).hexdigest()
        record=self.service.state.get('music_library',key)
        if record and record.get('trackId')==track['id']:
            try:
                self.service.catalog.stat(principal,record['entry'])
                return record['entry']
            except BackendError as error:
                if error.code not in ('NOT_FOUND','STALE_REVISION'): raise
        return None

    def directories(self, principal, drive, directory):
        fs=self.service.catalog.filesystem(drive); parent=''
        for segment in directory.split('/'):
            current='/'.join(p for p in (parent,segment) if p)
            try:
                if fs.stat(current).kind!='directory': raise BackendError('CONFLICT','Music path contains a file')
            except BackendError as error:
                if error.code!='NOT_FOUND': raise
                request='music-folder-'+hashlib.sha256((drive+':'+current).encode()).hexdigest()
                receipt=self.service.operations.submit(principal,{'requestId':request,'kind':'new-folder','destination':{'driveId':drive,'path':parent},'name':segment})
                job=self.service.jobs.get(principal,receipt['jobIds'][0])
                # The operation worker can finish between submit and get; recheck the actual directory.
                try:
                    if fs.stat(current).kind=='directory':
                        parent=current;continue
                except BackendError as current_error:
                    if current_error.code!='NOT_FOUND':raise
                if job['state'] in ('failed','canceled','completed'):
                    if job['state'] == 'failed' and (job.get('error') or {}).get('code') in {'INTERRUPTED', 'BUSY'}:
                        from .recovery import reconcile_job
                        if reconcile_job(self.service, job['id']):
                            raise BackendError('BUSY','Preparing album folders',True)
                    raise BackendError('CONFLICT','A music folder changed or could not be created; choose another destination')
                raise BackendError('BUSY','Preparing album folders',True)
            parent=current

    def publish(self, principal, job, track, save):
        service=self.service; drive=job['destination']['driveId']
        service.auth.authorize(principal,'acquire'); service.auth.authorize(principal,'write',drive,track['_path'])
        # Resume a completed transfer before consulting a possibly removed spool artifact.
        if track.get('_transfer'):
            receipt=service.transfers.query(principal,track['_transfer'])
            if receipt['state']=='completed': return self.record(track,drive,receipt['entry'],principal)
        directory,_,name=track['_path'].rpartition('/')
        self.directories(principal,drive,directory)
        if not track.get('_transfer'):
            try: service.catalog.filesystem(drive).stat(track['_path'])
            except BackendError as error:
                if error.code!='NOT_FOUND':raise
            else: raise BackendError('CONFLICT','A file already exists at this destination; it was not overwritten')
        root=os.open(self.config['spool'],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try: fd=openat2(root,track['_task']+'/audio.mp3',os.O_RDONLY|os.O_NOFOLLOW)
        finally: os.close(root)
        with os.fdopen(fd,'rb') as source:
            before=os.fstat(source.fileno()); artifact=track['_artifact']
            if not stat.S_ISREG(before.st_mode) or before.st_uid!=self.config['workerUid'] or before.st_nlink!=1 or before.st_mode & 0o027 or not 0<before.st_size<=512*1024*1024 or str(before.st_size)!=artifact['sizeBytes']:
                raise BackendError('INTEGRITY_FAILED','The music artifact is not a protected regular file')
            checksum=hashlib.sha256()
            while chunk:=source.read(CHUNK_BYTES): checksum.update(chunk)
            if checksum.hexdigest()!=artifact['sha256']: raise BackendError('INTEGRITY_FAILED','Music source verification failed')
            if not track.get('_transfer'):
                receipt=service.transfers.initiate(principal,{'requestId':'music-import-'+job['id']+'-'+str(track['_position'])+'-'+str(track['attempt']),
                    'destination':{'driveId':drive,'path':directory},'name':name,'sizeBytes':artifact['sizeBytes'],
                    'sha256':artifact['sha256'],'conflictPolicy':'ask','parentRequestId':job['id']})
                track['_transfer']=receipt['transferId']; save()
            else: receipt=service.transfers.query(principal,track['_transfer'])
            offset=int(receipt['offset']); source.seek(offset)
            while chunk:=source.read(CHUNK_BYTES):
                service.auth.authorize(principal,'acquire')
                service.transfers.write(principal,track['_transfer'],offset,chunk); offset+=len(chunk)
            after=os.fstat(source.fileno())
            if (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                raise BackendError('INTEGRITY_FAILED','Music artifact changed while importing')
            result=service.transfers.finalize(principal,track['_transfer'])
            return self.record(track,drive,result['entry'],principal)

    def record(self, track, drive, entry, principal):
        if not entry: raise BackendError('CONFLICT','Music import did not publish a file')
        ref={k:entry[k] for k in ('id','driveId','path','revision')}
        key=hashlib.sha256((drive+':'+track['_path']).encode()).hexdigest()
        self.service.state.put('music_library',key,{'entry':ref,'sha256':track['_artifact']['sha256'],'trackId':track['id'],'durationMs':track['_artifact'].get('durationMs',0)})
        try:
            result=self.service.state.get('music_lyric_results',track['_task']) if track.get('_task') else None
            self.service.music.lyrics.schedule(principal,track['id'],ref,result=result,duration_ms=track['_artifact'].get('durationMs',0))
            if track.get('_task'): self.service.state.delete('music_lyric_results',track['_task'])
        except BackendError: pass
        return ref
