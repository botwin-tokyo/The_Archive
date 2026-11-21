"""Strict radio control schemas and owner-managed station configuration."""
import os
import re
from urllib.parse import urlsplit
from .contracts import BackendError, entry_ref, location, object_fields

LIMITS = {'tracksPerBatch': 1024, 'tracksPerCall': 128, 'pageSize': 100,
          'maxFileBytes': str(1024**3), 'activeBatches': 64, 'stations': 8}
ID = {'type': 'string', 'minLength': 1, 'maxLength': 256}
PATH = {'type': 'string', 'maxLength': 4096}
REF = {'type': 'object', 'properties': {'id': ID, 'driveId': ID, 'path': PATH,
       'revision': {'type': 'integer', 'minimum': 0, 'maximum': 2**53-1}},
       'required': ['id', 'driveId', 'path', 'revision'], 'additionalProperties': False}
LOC = {'type': 'object', 'properties': {'driveId': ID, 'path': PATH},
       'required': ['driveId', 'path'], 'additionalProperties': False}
TYPES = {'batchId': ID, 'stationId': ID, 'name': {'type':'string','minLength':1,'maxLength':120},
         'requestId': ID, 'revision': {'type':'integer','minimum':1},
         'tracks': {'type':'array','items':REF,'minItems':1,'maxItems':128},
         'itemIds': {'type':'array','items':ID,'minItems':1,'maxItems':1024,'uniqueItems':True},
         'limit': {'type':'integer','minimum':1,'maximum':100},
         'cursor': {'type':'string','minLength':1,'maxLength':512},
         'location': LOC, 'query': {'type':'string','maxLength':255},
         'action': {'enum':['pause','resume','cancel','retry-failed','skip-failed']},
         'operation': {'enum':['create','add','remove','reorder','push','control']}}


def tool_schema(cap):
    return {'type':'object','properties':{k:TYPES[k] for k in cap.required+cap.optional},
            'required':list(cap.required),'additionalProperties':False}


def validate(value, schema):
    def bad(): raise BackendError('INVALID_ARGUMENT','Invalid radio arguments')
    if 'enum' in schema and value not in schema['enum']: bad()
    kind=schema.get('type')
    if kind=='object':
        object_fields(value,schema.get('required',()),tuple(schema['properties']))
        for k,v in value.items(): validate(v,schema['properties'][k])
    elif kind=='string':
        if not isinstance(value,str) or not schema.get('minLength',0)<=len(value)<=schema.get('maxLength',4096) or '\0' in value: bad()
    elif kind=='integer':
        if type(value) is not int or not schema.get('minimum',0)<=value<=schema.get('maximum',2**53-1): bad()
    elif kind=='array':
        if not isinstance(value,list) or not schema.get('minItems',0)<=len(value)<=schema.get('maxItems',1024): bad()
        for v in value: validate(v,schema['items'])
        if schema.get('uniqueItems') and len(set(value))!=len(value): bad()


def parse_config(value):
    if value is None: return None
    object_fields(value,('enabled','spool','roots','stations'),('spoolBudgetBytes',))
    if type(value['enabled']) is not bool: raise BackendError('INVALID_ARGUMENT','Invalid radio enabled flag')
    def absolute(p):
        if not isinstance(p,str) or not os.path.isabs(p) or '..' in p.split('/') or '\0' in p or p=='/':
            raise BackendError('INVALID_ARGUMENT','Radio paths must be explicit absolute private paths')
    absolute(value['spool'])
    roots=value['roots'];stations=value['stations']
    if not isinstance(roots,list) or not 1<=len(roots)<=128 or not isinstance(stations,list) or not 1<=len(stations)<=8:
        raise BackendError('INVALID_ARGUMENT','Bounded radio roots and stations required')
    for root in roots: location(root)
    seen=set()
    for station in stations:
        object_fields(station,('id','receiverId','endpoint','secretFile','agentIds'))
        for key in ('id','receiverId'):
            if not isinstance(station[key],str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}',station[key]):
                raise BackendError('INVALID_ARGUMENT','Invalid station identity')
        if station['id'] in seen: raise BackendError('INVALID_ARGUMENT','Duplicate station')
        seen.add(station['id']);absolute(station['secretFile'])
        if not isinstance(station['agentIds'],list) or len(station['agentIds'])>128 or any(not isinstance(x,str) or not 1<=len(x)<=256 for x in station['agentIds']):
            raise BackendError('INVALID_ARGUMENT','Explicit station agent grants required')
        endpoint=station['endpoint']
        if endpoint=='' and not value['enabled']: continue
        try:
            u=urlsplit(endpoint)
            valid=u.scheme=='https' and u.hostname and not u.username and not u.password and not u.query and not u.fragment and u.path in ('','/') and u.port!=0 and not any(c.isspace() for c in endpoint)
        except (ValueError,TypeError): valid=False
        if not valid: raise BackendError('INVALID_ARGUMENT','Station requires an HTTPS origin without credentials or path')
    budget=value.get('spoolBudgetBytes',str(2*1024**3))
    from .contracts import bytes_value
    if not 1024**3<=bytes_value(budget)<=64*1024**3: raise BackendError('INVALID_ARGUMENT','Invalid radio spool budget')
    return dict(value,spoolBudgetBytes=budget)
