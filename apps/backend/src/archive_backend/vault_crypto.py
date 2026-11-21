"""Versioned authenticated envelopes; no plaintext user data is persisted."""
import base64,json,os,stat,uuid
from .contracts import BackendError,canonical

def seal(key,value,context):
 from cryptography.hazmat.primitives.ciphers.aead import AESGCM
 nonce=os.urandom(12)
 return base64.b64encode(nonce+AESGCM(key).encrypt(nonce,canonical(value).encode(),context.encode())).decode()
def unseal(key,value,context):
 from cryptography.hazmat.primitives.ciphers.aead import AESGCM
 try:
  raw=base64.b64decode(value,validate=True)
  return json.loads(AESGCM(key).decrypt(raw[:12],raw[12:],context.encode()))
 except Exception:raise BackendError('VAULT_INTEGRITY','Vault authentication failed') from None

def private_read(path):
 fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
 with os.fdopen(fd,'rb') as f:
  st=os.fstat(f.fileno())
  if not stat.S_ISREG(st.st_mode) or st.st_uid not in (0,os.geteuid()) or stat.S_IMODE(st.st_mode)&0o077:raise BackendError('PERMISSION_DENIED','Vault file must be private')
  return f.read(96*1024*1024+1)

def new_key_record():
 root=os.urandom(32);identity=str(uuid.uuid4());key=os.urandom(32)
 return {'version':1,'vaultId':identity,'rootKey':base64.b64encode(root).decode(),'wrappedKey':seal(root,base64.b64encode(key).decode(),identity+':key:1')}

def load_keys(path):
 r=json.loads(private_read(path));identity=r['vaultId']
 root=base64.b64decode(r['rootKey'],validate=True)
 key=base64.b64decode(unseal(root,r['wrappedKey'],identity+':key:1'),validate=True)
 if r['version']!=1 or len(root)!=32 or len(key)!=32:raise BackendError('VAULT_INTEGRITY','Invalid vault key material')
 return identity,key
