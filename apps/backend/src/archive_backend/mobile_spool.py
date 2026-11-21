"""Private descriptor-relative immutable download cache, outside managed volumes."""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import stat
import hashlib
import json
from .contracts import BackendError


def identity(info):
    return [info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns,info.st_mode]


class SnapshotSpool:
    def __init__(self,state_path,min_free_bytes):
        if not os.path.isabs(state_path): raise BackendError('INVALID_ARGUMENT','Mobile snapshots require private disk-backed service state')
        self.reserve=min_free_bytes
        owner_hash=hashlib.sha256(Path(state_path).name.encode('utf-8')).hexdigest()
        self.directory_name='.archive-mobile-spool-'+owner_hash[:24]
        parent=os.open(str(Path(state_path).parent),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
        try:
            info=os.fstat(parent)
            if info.st_uid!=os.geteuid() or info.st_mode & 0o022: raise BackendError('PERMISSION_DENIED','Unsafe snapshot parent')
            try: os.mkdir(self.directory_name,0o700,dir_fd=parent)
            except FileExistsError: pass
            self.fd=os.open(self.directory_name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=parent)
            info=os.fstat(self.fd)
            if info.st_uid!=os.geteuid() or info.st_mode & 0o077:
                os.close(self.fd); raise BackendError('PERMISSION_DENIED','Snapshot directory must be private and owned')
            try: self._owner_marker(owner_hash)
            except BaseException:
                os.close(self.fd); raise
            os.fsync(parent)
        finally: os.close(parent)
    def _owner_marker(self,owner_hash):
        expected={'schemaVersion':1,'stateNameSha256':owner_hash}
        try:
            marker=os.open('.owner.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=self.fd)
        except FileExistsError:
            marker=os.open('.owner.json',os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=self.fd)
            try:
                info=os.fstat(marker)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid() or info.st_mode & 0o077:
                    raise BackendError('PERMISSION_DENIED','Unsafe snapshot ownership marker')
                try: actual=json.loads(os.read(marker,1024))
                except (ValueError,UnicodeError): raise BackendError('INTEGRITY_FAILED','Malformed snapshot ownership marker') from None
                if actual!=expected: raise BackendError('PERMISSION_DENIED','Snapshot cache belongs to another service state')
            finally: os.close(marker)
        else:
            try:
                data=json.dumps(expected,separators=(',',':')).encode()
                if os.write(marker,data)!=len(data): raise BackendError('STATE_UNAVAILABLE','Snapshot marker write failed')
                os.fsync(marker)
            finally: os.close(marker)
            os.fsync(self.fd)
    @staticmethod
    def name(resource,suffix):
        if not isinstance(resource,str) or not re.fullmatch(r'[a-f0-9]{64}',resource): raise BackendError('INVALID_ARGUMENT','Invalid representation identity')
        return resource+suffix
    def capacity(self):
        info=os.fstatvfs(self.fd); return info.f_bavail*info.f_frsize
    def ensure_space(self,size):
        if size+self.reserve>self.capacity(): raise BackendError('INSUFFICIENT_SPACE','Snapshot cache filesystem has insufficient space')
    def create(self,resource):
        fd=os.open(self.name(resource,'.part'),os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=self.fd)
        os.fsync(self.fd)
        return fd
    def promote(self,resource,fd):
        os.fchmod(fd,0o400); os.fsync(fd)
        os.link(self.name(resource,'.part'),self.name(resource,'.blob'),src_dir_fd=self.fd,dst_dir_fd=self.fd,follow_symlinks=False)
        os.unlink(self.name(resource,'.part'),dir_fd=self.fd); os.fsync(self.fd)
        return identity(os.fstat(fd))
    @contextmanager
    def open(self,record):
        try: fd=os.open(self.name(record['id'],'.blob'),os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=self.fd)
        except FileNotFoundError: raise BackendError('INTEGRITY_FAILED','Prepared snapshot file is unavailable') from None
        try:
            self.check(fd,record); yield fd
        finally: os.close(fd)
    @staticmethod
    def check(fd,record):
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o400 or identity(info)!=record.get('fileIdentity'):
            raise BackendError('INTEGRITY_FAILED','Prepared snapshot identity or content metadata changed')
    def remove(self,resource):
        for suffix in ('.part','.blob'):
            target=self.name(resource,suffix)
            try: info=os.stat(target,dir_fd=self.fd,follow_symlinks=False)
            except FileNotFoundError: continue
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_nlink not in (1,2):
                raise BackendError('INTEGRITY_FAILED','Unexpected snapshot cleanup entry; operator inspection required')
            os.unlink(target,dir_fd=self.fd)
        os.fsync(self.fd)
    def close(self): os.close(self.fd)
