"""Authenticated machine-readable HTTP contract."""
import copy
import re
from .phone_transfer_contracts import SCHEMAS, LIMITS
from .phone_transfer_http import ROUTES, PREFIX

def document():
    paths={}
    error={'description':'Error envelope; 429 includes Retry-After; retry only retryable errors.','content':{'application/json':{'schema':{'type':'object','required':['ok','error'],'properties':{'ok':{'const':False},'error':{'type':'object','required':['code','message','retryable'],'properties':{'code':{'type':'string'},'message':{'type':'string'},'retryable':{'type':'boolean'},'expectedOffset':{'type':'integer'}}}}}}}}
    for method,pattern,op in ROUTES:
        path=re.sub(r'\(\?P<(\w+)>\[\\w-\]\+\)',lambda m:'{'+m[1]+'}',pattern)
        names=re.findall(r'{(\w+)}',path);spec=copy.deepcopy(SCHEMAS[op]);params=[]
        for name in names:
            params.append({'in':'path','name':name,'required':True,'schema':spec['properties'].pop(name)})
            spec['required'].remove(name)
        item={'operationId':'phone_transfer.'+op,'parameters':params,'responses':{'200':{'description':'Success envelope {ok:true,result:...}.'},**{str(n):copy.deepcopy(error) for n in (400,401,403,404,409,410,413,415,429,503,507)}}}
        if op=='prepare':item['responses']['201']={'description':'New durable draft; result has requestId, deliveryId, state, uploadExpiresAt, attachments [{clientAttachmentId,attachmentId,receivedBytes,byteLength,complete}].'}
        if method=='GET':
            for name,schema in spec['properties'].items():params.append({'in':'query','name':name,'required':name in spec['required'],'schema':schema})
        else:item['requestBody']={'required':True,'content':{'application/json':{'schema':spec}}}
        paths[PREFIX+path]={method.lower():item}
    base=PREFIX+'/deliveries/{deliveryId}/attachments/{attachmentId}/'
    params=[{'in':'path','name':n,'required':True,'schema':{'type':'string'}} for n in ('deliveryId','attachmentId')]
    paths[base+'chunks']={'put':{'operationId':'phone_transfer.chunk','parameters':params+[{'in':'header','name':n,'required':True,'schema':{'type':'string'}} for n in ('Content-Range','X-Chunk-SHA256','Content-Length')],'requestBody':{'required':True,'content':{'application/octet-stream':{'schema':{'type':'string','format':'binary','maxLength':LIMITS['chunkBytes']}}}},'responses':{'200':{'description':'Acknowledged attachmentId, receivedBytes, byteLength, complete in success envelope'},'default':error}}}
    paths[base+'content']={'get':{'operationId':'phone_transfer.content','parameters':params+[{'in':'header','name':n,'schema':{'type':'string'}} for n in ('Range','If-Range')],'responses':{str(n):{'description':'Original bytes; SHA256 ETag, Accept-Ranges, safe attachment disposition; 206 has Content-Range','content':{'application/octet-stream':{'schema':{'type':'string','format':'binary'}}}} for n in (200,206)}}}
    paths[base+'content']['get']['responses'].update({'416':error,'default':error})
    return {'openapi':'3.1.0','info':{'title':'Archive Phone Transfer Hold','version':'1','description':'Existing bearer + explicit inbox grant. No Firebase or push. Limits in capabilities.'},'servers':[{'url':'https://archive.example.com'}],'security':[{'ArchiveBearer':[]}],'components':{'securitySchemes':{'ArchiveBearer':{'type':'http','scheme':'bearer'}}},'paths':paths}
