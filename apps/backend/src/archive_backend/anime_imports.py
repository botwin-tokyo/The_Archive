"""Core-only episode publication through the existing verified resumable transfer path."""
import hashlib
import os
import stat
from .contracts import BackendError, CHUNK_BYTES
from .filesystem import openat2
from .anime_contracts import MAX_EPISODE_BYTES


def library_key(drive, logical_path):
    return hashlib.sha256((drive+':'+logical_path).encode()).hexdigest()


def occupied(service, drive, logical_path):
    try: service.catalog.filesystem(drive).stat(logical_path)
    except BackendError as error:
        if error.code=='NOT_FOUND': return False
        raise
    return True


class AnimeImports:
    def __init__(self, service, config, stop): self.service,self.config,self.stop=service,config,stop

    def present(self, principal, job, episode):
        """A library receipt plus an unchanged exact entry proves the requested artifact; a pathname alone does not."""
        record=self.service.state.get('anime_library',library_key(job['destination']['driveId'],episode['_path']))
        if record and (record.get('anilistId'),record.get('episodeId'),record.get('audio'))==(job['anilistId'],episode['id'],job['audio']):
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
                if fs.stat(current).kind!='directory': raise BackendError('CONFLICT','The anime folder path contains a file')
            except BackendError as error:
                if error.code!='NOT_FOUND': raise
                request='anime-folder-'+hashlib.sha256((drive+':'+current).encode()).hexdigest()
                receipt=self.service.operations.submit(principal,{'requestId':request,'kind':'new-folder','destination':{'driveId':drive,'path':parent},'name':segment})
                job=self.service.jobs.get(principal,receipt['jobIds'][0])
                # The operation worker can finish between submit and get; recheck the actual directory.
                try:
                    if fs.stat(current).kind=='directory':
                        parent=current; continue
                except BackendError as current_error:
                    if current_error.code!='NOT_FOUND': raise
                if job['state'] in ('failed','canceled','completed'):
                    raise BackendError('CONFLICT','The series folder changed or could not be created; choose another destination')
                raise BackendError('BUSY','Preparing the series folder',True)
            parent=current

    def publish(self, principal, job, episode, save):
        """Publish one verified spool artifact. `save(fields)` durably merges episode fields."""
        service=self.service; drive=job['destination']['driveId']; target=episode['_path']
        service.auth.owner(principal); service.auth.authorize(principal,'write',drive,target)
        # Resume a completed transfer before consulting a possibly removed spool artifact.
        if episode.get('_transfer'):
            receipt=service.transfers.query(principal,episode['_transfer'])
            if receipt['state']=='completed': return self.record(job,episode,receipt['entry'])
        directory,_,name=target.rpartition('/')
        self.directories(principal,drive,directory)
        if not episode.get('_transfer') and occupied(service,drive,target):
            raise BackendError('CONFLICT','A file already exists at this destination; it was not overwritten')
        artifact=episode['_artifact']
        root=os.open(self.config['spool'],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try: fd=openat2(root,episode['_task']+'/'+artifact['filename'],os.O_RDONLY|os.O_NOFOLLOW)
        finally: os.close(root)
        with os.fdopen(fd,'rb') as source:
            before=os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_uid!=self.config['workerUid'] or before.st_nlink!=1 or before.st_mode & 0o027 or not 0<before.st_size<=MAX_EPISODE_BYTES or str(before.st_size)!=artifact['sizeBytes']:
                raise BackendError('INTEGRITY_FAILED','The episode artifact is not a protected regular file')
            checksum=hashlib.sha256()
            while chunk:=source.read(CHUNK_BYTES):
                if self.stop.is_set(): raise BackendError('BUSY','Anime importer is stopping',True)
                checksum.update(chunk)
            if checksum.hexdigest()!=artifact['sha256']: raise BackendError('INTEGRITY_FAILED','Episode verification failed')
            if not episode.get('_transfer'):
                receipt=service.transfers.initiate(principal,{'requestId':'anime-import-'+job['id']+'-'+episode['id']+'-'+str(episode['attempt']),
                    'destination':{'driveId':drive,'path':directory},'name':name,'sizeBytes':artifact['sizeBytes'],
                    'sha256':artifact['sha256'],'conflictPolicy':'ask','parentRequestId':job['id']})
                episode['_transfer']=receipt['transferId']; save({'_transfer':receipt['transferId']})
            else: receipt=service.transfers.query(principal,episode['_transfer'])
            offset=int(receipt['offset']); source.seek(offset)
            while chunk:=source.read(CHUNK_BYTES):
                if self.stop.is_set(): raise BackendError('BUSY','Anime importer is stopping',True)
                service.auth.owner(principal)
                service.transfers.write(principal,episode['_transfer'],offset,chunk); offset+=len(chunk)
            after=os.fstat(source.fileno())
            if (before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                raise BackendError('INTEGRITY_FAILED','Episode artifact changed while importing')
            result=service.transfers.finalize(principal,episode['_transfer'])
            return self.record(job,episode,result['entry'])

    def record(self, job, episode, entry):
        if not entry: raise BackendError('CONFLICT','Episode import did not publish a file')
        ref={k:entry[k] for k in ('id','driveId','path','revision')}
        artifact=episode.get('_artifact') or {}
        self.service.state.put('anime_library',library_key(job['destination']['driveId'],episode['_path']),{
            'entry':ref,'sha256':artifact.get('sha256'),'sizeBytes':artifact.get('sizeBytes'),'anilistId':job['anilistId'],'providerSeriesId':job['providerSeriesId'],
            'episodeId':episode['id'],'number':episode['number'],'audio':job['audio'],'height':artifact.get('height'),'subtitles':artifact.get('subtitles',[])})
        return ref
