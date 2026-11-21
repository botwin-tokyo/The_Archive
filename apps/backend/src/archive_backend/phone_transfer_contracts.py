"""Strict Phone Transfer Hold contracts. No credentials or external push provider."""
import json
import os
import re
import uuid
from urllib.parse import urlsplit
from .contracts import BackendError, object_fields

INBOX = 'phone-primary'
LIMITS = {'maxAttachmentBytes': 268435456, 'maxDeliveryBytes': 536870912, 'maxAttachments': 10,
          'maxLinks': 20, 'chunkBytes': 4194304, 'maxPageSize': 100, 'maxRetentionDays': 90,
          'defaultRetentionDays': 30, 'quotaBytes': 10737418240, 'draftSeconds': 86400,
          'feedRetentionDays': 90, 'maxDrafts': 16, 'maxDevices': 64, 'maxOperations': 100000,
          'metadataBytes': 268435456, 'minFreeBytes': 67108864, 'requestsPerMinute': 1200,
          'maxPrepareMetadataBytes':196608, 'jsonBytes': 262144, 'pageBytes': 1048576, 'snapshotSeconds': 900}

class PhoneError(BackendError):
    def __init__(self, code, status=400, message=None, **extra):
        super().__init__(code, message or code.replace('_', ' ').capitalize(), status in (429,503,507))
        self.status = status; self.extra = extra
    def envelope(self, correlation_id=None):
        return {**super().envelope(correlation_id), **self.extra}

def fail(code, status=400, message=None, **extra):
    raise PhoneError(code, status, message, **extra)

def string(maximum, minimum=0, pattern=None, byte_limit=None):
    value = {'type':'string','minLength':minimum,'maxLength':maximum}
    if pattern: value['pattern'] = pattern
    if byte_limit is not None: value['x-maxUtf8Bytes'] = byte_limit
    return value

def obj(properties, required=()):
    return {'type':'object','properties':properties,'required':list(required),'additionalProperties':False}

ID = string(128,1,r'^[A-Za-z0-9_-]{1,128}$')
UUID = string(36,36,r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')
HASH = string(64,64,r'^[0-9a-f]{64}$')
ATTACHMENT = obj({'clientAttachmentId':ID,'filename':string(255,1,byte_limit=255),'mediaType':string(128,1),'byteLength':{'type':'integer','minimum':0,'maximum':LIMITS['maxAttachmentBytes']},'sha256':HASH},('clientAttachmentId','filename','mediaType','byteLength','sha256'))
LINK = obj({'label':string(200),'url':string(8192,1,byte_limit=8192)},('label','url'))
SCHEMAS = {
 'capabilities':obj({}), 'help':obj({}),
 'prepare':obj({'requestId':UUID,'inboxId':ID,'title':string(200,1),'senderLabel':string(120),'message':string(32768,byte_limit=32768),'links':{'type':'array','maxItems':20,'items':LINK},'attachments':{'type':'array','maxItems':10,'items':ATTACHMENT},'retentionDays':{'type':'integer','minimum':1,'maximum':90}},('requestId','inboxId','title')),
 'operation':obj({'requestId':UUID},('requestId',)),
 'uploads':obj({'deliveryId':ID},('deliveryId',)),
 'commit':obj({'deliveryId':ID,'requestId':UUID},('deliveryId','requestId')),
 'list':obj({'inboxId':ID,'limit':{'type':'integer','minimum':1,'maximum':100},'cursor':string(2048,1,r'^[\x21-\x7e]+$')},('inboxId',)),
 'changes':obj({'inboxId':ID,'limit':{'type':'integer','minimum':1,'maximum':100},'cursor':string(2048,1,r'^[\x21-\x7e]+$')},('inboxId','cursor')),
 'get':obj({'deliveryId':ID},('deliveryId',)),
 'device':obj({'inboxId':ID,'deviceId':UUID,'platform':{'type':'string','enum':['android']},'displayName':string(120),'pushToken':{'type':['null','string'],'maxLength':4096,'x-maxUtf8Bytes':4096},'notificationsEnabled':{'type':'boolean'}},('inboxId','deviceId','platform','displayName','pushToken','notificationsEnabled')),
 'receipt':obj({'deliveryId':ID,'deviceId':UUID,'downloadedAttachmentIds':{'type':'array','maxItems':10,'items':ID},'opened':{'type':'boolean'}},('deliveryId','deviceId')),
 'delete':obj({'deliveryId':ID,'requestId':UUID},('deliveryId','requestId')),
}

def validate(value, spec):
    kind=spec['type']
    if isinstance(kind,list):
        if value is None and 'null' in kind:return
        kind='string'
    valid=True
    if kind=='object':
        object_fields(value,spec.get('required',()),tuple(spec['properties']))
        for k,v in value.items():validate(v,spec['properties'][k])
    elif kind=='array':
        valid=isinstance(value,list) and len(value)<=spec['maxItems']
        if valid:
            for v in value:validate(v,spec['items'])
    elif kind=='string':
        try:valid=isinstance(value,str) and spec.get('minLength',0)<=len(value)<=spec.get('maxLength',4096) and len(value.encode('utf-8'))<=spec.get('x-maxUtf8Bytes',16384) and ('pattern' not in spec or re.fullmatch(spec['pattern'],value) is not None)
        except UnicodeError:valid=False
        if 'enum' in spec:valid=valid and value in spec['enum']
    elif kind=='integer':valid=type(value) is int and spec['minimum']<=value<=spec['maximum']
    elif kind=='boolean':valid=type(value) is bool
    if not valid:fail('INVALID_INPUT')

def prepare(value):
    validate(value,SCHEMAS['prepare'])
    result={'senderLabel':'Agent','message':'','links':[],'attachments':[],'retentionDays':30,**value}
    result['requestId']=str(uuid.UUID(result['requestId']))
    if not result['title'].strip() or not (result['message'] or result['links'] or result['attachments']):fail('EMPTY_DELIVERY')
    if len({a['clientAttachmentId'] for a in result['attachments']})!=len(result['attachments']):fail('DUPLICATE_ATTACHMENT')
    if sum(a['byteLength'] for a in result['attachments'])>LIMITS['maxDeliveryBytes']:fail('DELIVERY_TOO_LARGE',413)
    for link in result['links']:
        try:
            url=urlsplit(link['url'])
            if url.scheme not in ('http','https') or not url.hostname or url.username is not None or url.password is not None or any(ord(c)<33 for c in link['url']):fail('INVALID_LINK')
            url.port
        except ValueError:fail('INVALID_LINK')
    for a in result['attachments']:
        if a['byteLength']==0 and a['sha256']!='e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855':fail('CONTENT_HASH_MISMATCH',409)
        if any(ord(c)<32 or ord(c)==127 for c in a['mediaType']):fail('INVALID_MEDIA_LABEL')
    if len(json.dumps(result,ensure_ascii=False,separators=(',',':')).encode('utf-8'))>LIMITS['maxPrepareMetadataBytes']:fail('METADATA_TOO_LARGE',413)
    return result

def parse_config(value, volumes, vault=None, skills=None):
    if value is None:return None
    object_fields(value,('enabled','volumeId','root','grants'),('quotaBytes',))
    if type(value['enabled']) is not bool or not isinstance(value['root'],str):fail('INVALID_CONFIGURATION')
    root=value['root'];volume=next((v for v in volumes if v['id']==value['volumeId']),None)
    if not volume or not os.path.isabs(root) or os.path.normpath(root)!=root or '\0' in root or not root.startswith(volume['mountPoint'].rstrip('/')+'/'):fail('INVALID_CONFIGURATION')
    protected=[v['rootPath'] for v in volumes]
    if skills:protected.append(skills['root'])
    if vault:protected.extend([vault['keyFile'],vault['anchor']]+[r['path'] for r in vault['replicas']])
    if any(root==p or root.startswith(p.rstrip('/')+'/') or p.startswith(root.rstrip('/')+'/') for p in protected):fail('INVALID_CONFIGURATION')
    if not isinstance(value['grants'],list) or len(value['grants'])>128:fail('INVALID_CONFIGURATION')
    for g in value['grants']:
        object_fields(g,('principalId','inboxId'));validate(g['principalId'],ID)
        if g['inboxId']!=INBOX:fail('INVALID_CONFIGURATION')
    quota=value.get('quotaBytes',LIMITS['quotaBytes'])
    if type(quota) is not int or not 1<=quota<=2**53-1:fail('INVALID_CONFIGURATION')
    return dict(value,quotaBytes=quota)
