"""Single-writer mirrored encrypted snapshots with durable acknowledgment anchor.
No operation is acknowledged until both immutable snapshots/markers and anchor fsync.
"""
import copy,fcntl,hashlib,json,os,stat,threading
from pathlib import Path
from .contracts import BackendError,canonical
from .vault_crypto import load_keys,private_read,seal,unseal

MAX_SNAPSHOT=64*1024*1024
BUDGET=1024**3

def atomic(directory,name,data):
 # directory is an already verified open fd; a disappearing mount cannot redirect writes.
 tmp=name+'.tmp'
 fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600,dir_fd=directory)
 try:
  with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
  os.rename(tmp,name,src_dir_fd=directory,dst_dir_fd=directory);os.fsync(directory)
 finally:
  try:os.unlink(tmp,dir_fd=directory)
  except FileNotFoundError:pass

def read_at(fd,name):
 f=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd)
 with os.fdopen(f,'rb') as stream:
  st=os.fstat(stream.fileno())
  if not stat.S_ISREG(st.st_mode) or st.st_mode&0o077 or st.st_uid!=os.geteuid():raise BackendError('VAULT_INTEGRITY','Unsafe vault file')
  if st.st_size>MAX_SNAPSHOT*2:raise BackendError('VAULT_INTEGRITY','Vault file exceeds limit')
  return stream.read()

def exists(fd,name):
 try:os.stat(name,dir_fd=fd,follow_symlinks=False);return True
 except FileNotFoundError:return False

class VaultStore:
 def __init__(self,config,*,mount_check=None,fault=lambda point:None):
  self.config=config;self.identity,self.key=load_keys(config['keyFile']);self.lock=threading.RLock();self.fault=fault
  self.mount_check=mount_check or self.verify_mount
  ap=Path(config['anchor']);self.anchor_name=ap.name
  self.anchor_dir=os.open(ap.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
  st=os.fstat(self.anchor_dir)
  if st.st_uid!=os.geteuid() or st.st_mode&0o077:os.close(self.anchor_dir);raise BackendError('PERMISSION_DENIED','Private anchor directory required')
  self.lockfd=os.open(ap.name+'.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=self.anchor_dir)
  try:fcntl.flock(self.lockfd,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except OSError:os.close(self.lockfd);os.close(self.anchor_dir);raise BackendError('UNAVAILABLE','Vault already has a writer') from None
 def close(self):os.close(self.lockfd);os.close(self.anchor_dir)
 @staticmethod
 def verify_mount(replica):
  mount=Path(replica['mountPoint']);path=Path(replica['path'])
  if path.resolve()!=path or mount.resolve()!=mount:raise BackendError('VAULT_INTEGRITY','Vault symlink rejected')
  expected=Path('/dev/disk/by-uuid')/replica['filesystemUuid']
  if not expected.exists() or not os.path.ismount(mount):raise BackendError('UNAVAILABLE','Vault mount is absent')
  device=os.stat(expected).st_rdev
  if mount.stat().st_dev!=device or path.stat().st_dev!=device:raise BackendError('VAULT_INTEGRITY','Unexpected vault device')
  # Resolve partition's parent physical block device; rejects two partitions on one disk.
  sys=Path('/sys/dev/block')/f'{os.major(device)}:{os.minor(device)}';real=sys.resolve()
  return str(real.parent if (real/'partition').exists() else real)
 def open_replicas(self):
  opened=[];identities=[]
  for r in self.config['replicas']:
   try:
    identity=self.mount_check(r)
    fd=os.open(r['path'],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    st=os.fstat(fd)
    if st.st_uid!=os.geteuid() or st.st_mode&0o077:os.close(fd);raise BackendError('VAULT_INTEGRITY','Private replica directory required')
    identities.append(identity);opened.append(fd)
   except (OSError,BackendError):opened.append(None)
  if len(identities)==2 and identities[0]==identities[1]:
   for fd in opened:
    if fd is not None:os.close(fd)
   raise BackendError('VAULT_INTEGRITY','Replicas must be on separate physical devices')
  return opened
 def context(self,kind):return self.identity+':vault-v1:'+kind
 def anchor(self):
  if not exists(self.anchor_dir,self.anchor_name):raise BackendError('VAULT_UNPROVISIONED','Vault requires explicit provisioning')
  return unseal(self.key,read_at(self.anchor_dir,self.anchor_name).decode(),self.context('anchor'))
 def set_anchor(self,a):atomic(self.anchor_dir,self.anchor_name,seal(self.key,a,self.context('anchor')).encode())
 def marker(self,fd,n,hashed):atomic(fd,f'{n}.commit',seal(self.key,{'sequence':n,'hash':hashed},self.context('commit')).encode())
 def verify_snapshot(self,raw,n,hashed):
  if hashlib.sha256(raw).hexdigest()!=hashed:raise BackendError('VAULT_INTEGRITY','Snapshot digest mismatch')
  value=unseal(self.key,raw.decode(),self.context('snapshot'))
  if value['sequence']!=n or value['vaultId']!=self.identity:raise BackendError('VAULT_INTEGRITY','Snapshot identity mismatch')
  return value
 def provision(self):
  with self.lock:
   fds=self.open_replicas()
   try:
    if None in fds or exists(self.anchor_dir,self.anchor_name):raise BackendError('CONFLICT','Provision requires empty healthy replicas')
    if any(os.listdir(fd) for fd in fds):raise BackendError('CONFLICT','Replica is not empty')
    value={'vaultId':self.identity,'sequence':0,'entries':{},'requests':{}}
    raw=seal(self.key,value,self.context('snapshot')).encode();h=hashlib.sha256(raw).hexdigest()
    for fd in fds:atomic(fd,'0.snapshot',raw);self.marker(fd,0,h)
    self.set_anchor({'sequence':0,'hash':h})
   finally:
    for fd in fds:
     if fd is not None:os.close(fd)
 def load(self,fds):
  a=self.anchor();n=a['sequence'];valid=[]
  for fd in fds:
   if fd is None:continue
   raw=read_at(fd,f'{n}.snapshot');value=self.verify_snapshot(raw,n,a['hash'])
   marker=unseal(self.key,read_at(fd,f'{n}.commit').decode(),self.context('commit'))
   if marker!={'sequence':n,'hash':a['hash']}:raise BackendError('VAULT_INTEGRITY','Commit marker mismatch')
   valid.append((raw,value))
  if not valid:raise BackendError('UNAVAILABLE','No verified vault replica available')
  if None not in fds:
   pending=[exists(fd,f'{n+1}.snapshot') for fd in fds]
   if all(pending):
    raws=[read_at(fd,f'{n+1}.snapshot') for fd in fds]
    if raws[0]!=raws[1]:raise BackendError('VAULT_INTEGRITY','Divergent pending replicas')
    h=hashlib.sha256(raws[0]).hexdigest();v=self.verify_snapshot(raws[0],n+1,h)
    if v.get('previous')!=a['hash']:raise BackendError('VAULT_INTEGRITY','Broken vault history')
    for fd in fds:self.marker(fd,n+1,h)
    self.set_anchor({'sequence':n+1,'hash':h});return v,{'sequence':n+1,'hash':h}
   if any(pending):
    # Single prepare without any commit was never acknowledged. Safe to abort.
    if any(exists(fd,f'{n+1}.commit') for fd in fds):raise BackendError('VAULT_INTEGRITY','Incomplete committed replica')
    for fd,present in zip(fds,pending):
     if present:os.unlink(f'{n+1}.snapshot',dir_fd=fd);os.fsync(fd)
  return valid[0][1],a
 def read(self):
  with self.lock:
   fds=self.open_replicas()
   try:
    value,a=self.load(fds);return copy.deepcopy(value),{'state':'healthy' if None not in fds else 'degraded','writable':None not in fds,'replicasAvailable':sum(fd is not None for fd in fds),'sequence':a['sequence']}
   finally:
    for fd in fds:
     if fd is not None:os.close(fd)
 def mutate(self,fn):
  with self.lock:
   fds=self.open_replicas()
   try:
    if None in fds:raise BackendError('UNAVAILABLE','Both vault replicas are required for writes',True)
    value,a=self.load(fds);result=fn(value)
    if result is None:return None
    value['sequence']=a['sequence']+1;value['previous']=a['hash']
    raw=seal(self.key,value,self.context('snapshot')).encode()
    if len(raw)>MAX_SNAPSHOT:raise BackendError('QUOTA_EXCEEDED','Vault snapshot budget exceeded')
    for fd in fds:
     used=sum(os.stat(name,dir_fd=fd,follow_symlinks=False).st_size for name in os.listdir(fd))
     free=os.fstatvfs(fd)
     if used+len(raw)>BUDGET or free.f_bavail*free.f_frsize<len(raw)+64*1024**2:raise BackendError('QUOTA_EXCEEDED','Vault replica free-space reserve reached')
    n=value['sequence'];h=hashlib.sha256(raw).hexdigest()
    for i,fd in enumerate(fds):atomic(fd,f'{n}.snapshot',raw);self.fault('prepare'+str(i))
    for i,fd in enumerate(fds):self.marker(fd,n,h);self.fault('commit'+str(i))
    self.set_anchor({'sequence':n,'hash':h});self.fault('anchor')
    return result
   except OSError:raise BackendError('UNAVAILABLE','Vault write interrupted; reconcile request before retry',True) from None
   finally:
    for fd in fds:
     if fd is not None:os.close(fd)
