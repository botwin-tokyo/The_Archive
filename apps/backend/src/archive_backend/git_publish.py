"""Core-only bounded archive publication from an immutable worker artifact."""
import ctypes
import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
from .contracts import BackendError
from .filesystem import openat2, RESOLVE_BENEATH, RESOLVE_NO_SYMLINKS, RESOLVE_NO_XDEV
from .git_engine import safe_path

FLAGS=os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC
RESOLVE=RESOLVE_BENEATH|RESOLVE_NO_SYMLINKS|RESOLVE_NO_XDEV


def directory(root, path):
    fd=os.dup(root)
    try:
        for part in path.split('/'):
            safe_path(part)
            try:os.mkdir(part,0o700,dir_fd=fd)
            except FileExistsError:pass
            child=openat2(fd,part,FLAGS,resolve=RESOLVE);os.close(fd);fd=child
        return fd
    except BaseException:os.close(fd);raise


def exchange(fd, a, b):
    libc=ctypes.CDLL(None,use_errno=True)
    result=libc.renameat2(ctypes.c_int(fd),ctypes.c_char_p(a.encode()),ctypes.c_int(fd),ctypes.c_char_p(b.encode()),ctypes.c_uint(2))
    if result:raise OSError(ctypes.get_errno(),'Atomic directory exchange failed')


class Publisher:
    def __init__(self,service,config):self.service=service;self.config=config
    def publish(self,repo):
        artifact=repo['artifact']
        if not re.fullmatch('[a-f0-9]{32}',artifact):raise BackendError('INVALID_RESPONSE','Invalid Git artifact ID')
        volume=self.service.registry.get(self.config['volumeId']);volume.check()
        prior=self.service.state.get('git_publications',repo['id'])
        logical='GitHub/'+repo['owner']+'/'+repo['name']+'--'+repo['id']
        safe_path(logical)
        if prior and prior['artifact']==artifact:return prior
        root=volume.open_root();parent=None;stage=None
        try:
            parent=directory(root,logical)
            try:
                current=openat2(parent,'current/.archive-git-info.json',os.O_RDONLY,resolve=RESOLVE)
                with os.fdopen(current) as f:info=json.load(f)
                if info.get('artifact')==artifact:
                    saved={'artifact':artifact,'path':logical+'/current','commit':repo['commit']}
                    self.service.state.put('git_publications',repo['id'],saved)
                    try:shutil.rmtree('.archive-git-'+artifact,dir_fd=parent)
                    except FileNotFoundError:pass
                    self.service.state.put('git_publish_intents',repo['id'],dict(saved,state='completed'))
                    return saved
            except FileNotFoundError:pass
            except BackendError as error:
                if error.code!='NOT_FOUND':raise
            try:
                current_stat=os.stat('current',dir_fd=parent,follow_symlinks=False)
            except FileNotFoundError:current_stat=None
            if current_stat is not None and (not stat.S_ISDIR(current_stat.st_mode) or not prior):
                raise BackendError('CONFLICT','Existing current folder is not a recognized generated backup')
            top=os.open('/',FLAGS)
            try:spool=openat2(top,self.config['spool'].lstrip('/'),FLAGS,resolve=RESOLVE_BENEATH|RESOLVE_NO_SYMLINKS)
            finally:os.close(top)
            try:source=openat2(spool,artifact+'/files.tar',os.O_RDONLY|os.O_CLOEXEC,resolve=RESOLVE)
            finally:os.close(spool)
            info=os.fstat(source)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=self.config['workerUid'] or info.st_nlink!=1 or info.st_mode&0o022:
                os.close(source);raise BackendError('PERMISSION_DENIED','Git artifact ownership is invalid')
            if volume.capacity()['available']<info.st_size+10*1024**3:
                os.close(source);raise BackendError('FULL_DISK','Not enough space to publish Git backup safely')
            stage='.archive-git-'+artifact
            try:os.mkdir(stage,0o700,dir_fd=parent)
            except FileExistsError:
                shutil.rmtree(stage,dir_fd=parent);os.mkdir(stage,0o700,dir_fd=parent)
            staged=openat2(parent,stage,FLAGS,resolve=RESOLVE)
            try:
                with os.fdopen(source,'rb') as stream:
                    digest=hashlib.sha256()
                    for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
                    if digest.hexdigest()!=repo['sha256']:raise BackendError('INTEGRITY_FAILED','Git export checksum mismatch')
                    stream.seek(0);count=total=0
                    with tarfile.open(fileobj=stream,mode='r:') as archive:
                        for entry in archive:
                            name=safe_path(entry.name)
                            if not entry.isfile():raise BackendError('UNSUPPORTED_ENTRY','Git export contains a nonregular entry')
                            count+=1;total+=entry.size
                            if count>500000 or total>200*1024**3:raise BackendError('QUOTA_EXCEEDED','Git export budget exceeded')
                            parts=name.rsplit('/',1);outdir=directory(staged,parts[0]) if len(parts)==2 else os.dup(staged)
                            try:
                                fd=os.open(parts[-1],os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o700 if entry.mode&0o111 else 0o600,dir_fd=outdir)
                                with os.fdopen(fd,'wb') as out,archive.extractfile(entry) as inp:
                                    shutil.copyfileobj(inp,out,1024*1024);out.flush()
                            finally:os.close(outdir)
                            if count%100==0:volume.check()
                    if count!=repo['files'] or total!=repo['bytes']:raise BackendError('INTEGRITY_FAILED','Git export manifest totals differ')
                    after=os.fstat(stream.fileno())
                    if (after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns)!=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns):raise BackendError('SOURCE_CHANGED','Git export changed during import')
                fd=os.open('.archive-git-info.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600,dir_fd=staged)
                with os.fdopen(fd,'w') as out:json.dump(repo,out);out.flush();os.fsync(out.fileno())
                # Flush this filesystem before publishing; avoids one HDD barrier per tiny file.
                libc=ctypes.CDLL(None,use_errno=True)
                if libc.syncfs(ctypes.c_int(staged)) != 0:raise OSError(ctypes.get_errno(),'Git staging flush failed')
                os.fsync(staged)
            finally:os.close(staged)
            self.service.state.put('git_publish_intents',repo['id'],{'artifact':artifact,'path':logical,'stage':stage})
            volume.check()
            try:os.stat('current',dir_fd=parent,follow_symlinks=False)
            except FileNotFoundError:os.rename(stage,'current',src_dir_fd=parent,dst_dir_fd=parent)
            else:exchange(parent,stage,'current')
            os.fsync(parent);volume.check()
            saved={'artifact':artifact,'path':logical+'/current','commit':repo['commit']}
            self.service.state.put('git_publications',repo['id'],saved)
            self.service.state.put('git_publish_intents',repo['id'],dict(saved,state='completed'))
            # An exchanged stage contains the previous complete export; history lives in Git refs.
            try:shutil.rmtree(stage,dir_fd=parent)
            except FileNotFoundError:pass
            return saved
        finally:
            if parent is not None:os.close(parent)
            os.close(root)
