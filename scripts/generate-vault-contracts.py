"""Generate portable API/MCP documentation from the deployed input contract."""
import copy,json,sys
from pathlib import Path
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root/'apps/backend/src'))
from archive_backend.vault_contracts import OPS,schema
out=root/'contracts/password-manager';out.mkdir(exist_ok=True)
(out/'mcp-input-schemas.json').write_text(json.dumps({'methods':{'vault.'+op:schema(op) for op in OPS}},indent=2)+'\n')
routes=[('get','/status','status'),('post','/entries/search','entries.list'),('get','/entries/{id}','entries.get'),('post','/entries/{id}/reveal','entries.reveal'),('post','/entries','entries.create'),('patch','/entries/{id}','entries.update'),('post','/entries/{id}/trash','entries.trash'),('post','/entries/{id}/restore','entries.restore'),('post','/entries/{id}/history','entries.history'),('post','/entries/{id}/restore-version','entries.restoreVersion'),('post','/passwords/generate','passwords.generate'),('post','/requests/reconcile','requests.reconcile'),('post','/backup/export','backup.export'),('post','/backup/import','backup.import')]
paths={}
for method,path,op in routes:
 s=copy.deepcopy(schema(op));params=[]
 if '{id}' in path:
  params=[{'in':'path','name':'id','required':True,'schema':s['properties'].pop('id')}];s['required'].remove('id')
 operation={'operationId':'vault.'+op,'security':[{'vaultBearer':[]}],'parameters':params,'responses':{'200':{'description':'No-store result envelope.'},'403':{'description':'Credential lacks required vault permission'},'409':{'description':'Revision or request identity conflict'},'503':{'description':'Vault unavailable; writes require both replicas'}}}
 if method!='get':operation['requestBody']={'required':True,'content':{'application/json':{'schema':s}}}
 paths.setdefault(path,{})[method]=operation
(out/'openapi.json').write_text(json.dumps({'openapi':'3.1.0','info':{'title':'Archive Password Manager','version':'1.0.0'},'servers':[{'url':'https://archive.example.com/vault/v1'}],'components':{'securitySchemes':{'vaultBearer':{'type':'http','scheme':'bearer'}}},'paths':paths},indent=2)+'\n')
