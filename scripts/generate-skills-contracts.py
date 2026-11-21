"""Generate Skill Library MCP and OpenAPI contracts from backend schemas."""
import json
import pathlib
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]/'apps/backend/src'))
from archive_backend.skills_contracts import OPS, schema
out = pathlib.Path(__file__).resolve().parents[1]/'contracts/skill-library'
out.mkdir(exist_ok=True)
(out/'mcp-input-schemas.json').write_text(json.dumps({'skills.'+op: schema(op) for op in OPS}, indent=2)+'\n')
paths = {}
routes = [('get','/status','status'),('get','/help','help'),('get','/skills','list'),('post','/skills/put','put'),('get','/skills/{id}','get'),('get','/skills/{id}/document','document'),('post','/skills/{id}/trash','trash'),('post','/skills/{id}/restore','restore'),('post','/uploads','uploadBegin'),('get','/uploads/{uploadId}','uploadStatus'),('post','/uploads/{uploadId}/publish','uploadPublish'),('delete','/uploads/{uploadId}','uploadCancel')]
for method, path, op in routes:
    s = schema(op); params = []; body = dict(s, properties=dict(s['properties']), required=list(s['required']))
    for k in s['properties']:
        if '{'+k+'}' in path or method == 'get':
            params.append({'name':k,'in':'path' if '{'+k+'}' in path else 'query','required':k in s['required'],'schema':s['properties'][k]})
            body['properties'].pop(k); body['required'] = [x for x in body['required'] if x != k]
    operation = {'operationId':'skills.'+op,'description':'Requires '+OPS[op][0]+'. Returns {ok:true,result:...}; errors return {ok:false,error:...}.','parameters':params,'responses':{'200':{'description':'Operation result'}}}
    if body['properties']: operation['requestBody'] = {'required':True,'content':{'application/json':{'schema':body}}}
    paths.setdefault(path,{})[method] = operation
paths['/uploads/{uploadId}/files'] = {'put':{'operationId':'skills.uploadChunk','description':'skills.write; retry identical bytes at the same offset; maximum chunk 1 MiB.','parameters':[{'name':n,'in':i,'required':True,'schema':{'type':t}} for n,i,t in [('uploadId','path','string'),('path','query','string'),('offset','query','integer')]],'requestBody':{'required':True,'content':{'application/octet-stream':{'schema':{'type':'string','format':'binary'}}}},'responses':{'200':{'description':'Upload state and durable acknowledged offsets'}}}}
paths['/skills/{id}/files'] = {'get':{'operationId':'skills.download','description':'skills.read; verified file bytes, attachment disposition, no-store. Maximum 16 MiB.','parameters':[{'name':n,'in':i,'required':n!='revision','schema':{'type':t}} for n,i,t in [('id','path','string'),('path','query','string'),('revision','query','integer')]],'responses':{'200':{'description':'File bytes','content':{'application/octet-stream':{'schema':{'type':'string','format':'binary'}}}}}}}
(out/'openapi.json').write_text(json.dumps({'openapi':'3.1.0','info':{'title':'Archive Skill Library','version':'1.0.0'},'servers':[{'url':'https://archive.example.com/skills/v1'}],'security':[{'bearer':[]}],'components':{'securitySchemes':{'bearer':{'type':'http','scheme':'bearer'}}},'paths':paths},indent=2)+'\n')
