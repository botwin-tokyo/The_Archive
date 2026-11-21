"""Strict vault contracts. Secret strings are never normalized."""
import os
from .contracts import BackendError, object_fields
S={'type':'string','maxLength':16384}
ID={'type':'string','minLength':1,'maxLength':256}
REV={'type':'integer','minimum':1,'maximum':2**53-1}
FIELDS={'displayName':{'type':'string','minLength':1,'maxLength':512},'url':{'type':'string','maxLength':4096},'login':S,'password':S,'notes':{'type':'string','maxLength':65536}}
TYPES=dict(FIELDS,id=ID,requestId=ID,revision=REV,version=REV,query={'type':'string','maxLength':256},cursor={'type':'string','maxLength':256},limit={'type':'integer','minimum':1,'maximum':100},trashed={'type':'boolean'},length={'type':'integer','minimum':12,'maximum':256},alphabet={'type':'string','minLength':2,'maxLength':256},operation=ID,archive={'type':'string','maxLength':1000000})
from .contracts import VAULT_OPS as OPS

def schema(name):
 scope,required,optional=OPS[name.removeprefix('vault.')]
 return {'type':'object','properties':{k:TYPES[k] for k in required+optional},'required':list(required),'additionalProperties':False}
def validate(value,s):
 def bad():raise BackendError('INVALID_ARGUMENT','Invalid vault arguments or size limit exceeded')
 kind=s['type']
 if kind=='object':
  object_fields(value,s.get('required',()),tuple(s['properties']))
  for k,v in value.items():validate(v,s['properties'][k])
 elif kind=='string':
  if not isinstance(value,str):bad()
  try:n=len(value.encode('utf-8'))
  except UnicodeError:bad()
  if not s.get('minLength',0)<=n<=s['maxLength']:bad()
 elif kind=='integer':
  if type(value) is not int or not s['minimum']<=value<=s['maximum']:bad()
 elif kind=='boolean' and type(value) is not bool:bad()
def parse_config(v,volumes):
 if v is None:return None
 object_fields(v,('enabled','keyFile','anchor','replicas'))
 if type(v['enabled']) is not bool or not isinstance(v['replicas'],list) or len(v['replicas'])!=2:raise BackendError('INVALID_ARGUMENT','Two vault replicas required')
 paths=[v['keyFile'],v['anchor']]
 for r in v['replicas']:
  object_fields(r,('path','mountPoint','filesystemUuid'))
  paths.extend([r['path'],r['mountPoint']])
  if not isinstance(r['filesystemUuid'],str) or not r['filesystemUuid']:raise BackendError('INVALID_ARGUMENT','Vault mount identity required')
  if not r['path'].startswith(r['mountPoint'].rstrip('/')+'/'):raise BackendError('INVALID_ARGUMENT','Vault must be under its declared mount')
 if len({r['filesystemUuid'] for r in v['replicas']})!=2:raise BackendError('INVALID_ARGUMENT','Independent vault filesystems required')
 for p in paths:
  if not isinstance(p,str) or not os.path.isabs(p) or os.path.normpath(p)!=p or p=='/' or '\0' in p:raise BackendError('INVALID_ARGUMENT','Explicit vault paths required')
 for p in [v['keyFile'],v['anchor']]+[r['path'] for r in v['replicas']]:
  for root in [x['rootPath'] for x in volumes]:
   if p==root or p.startswith(root.rstrip('/')+'/'):raise BackendError('INVALID_ARGUMENT','Vault must be outside managed roots')
 private=[v['keyFile'],v['anchor']]+[r['path'] for r in v['replicas']]
 if any(a==b or a.startswith(b.rstrip('/')+'/') or b.startswith(a.rstrip('/')+'/') for i,a in enumerate(private) for b in private[i+1:]):raise BackendError('INVALID_ARGUMENT','Vault key, coordinator and replica paths must be independent')
 return v
