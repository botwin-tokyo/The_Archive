"""Bounded raw-MP3 HTTPS receiver protocol. No redirects or environment proxies."""
import http.client
import json
import os
import re
import ssl
import stat
import time
from urllib.parse import urlsplit
from .contracts import BackendError

PREFIX='/archive-radio/v1'


class ReceiverError(BackendError):
    def __init__(self,code,message,retryable=False,retry_after=0):
        super().__init__(code,message,retryable);self.retry_after=min(60,max(0,retry_after))


def read_secret(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as f:
        s=os.fstat(f.fileno())
        if not stat.S_ISREG(s.st_mode) or stat.S_IMODE(s.st_mode)&0o077 or s.st_uid not in (0,os.geteuid()):
            raise BackendError('PERMISSION_DENIED','Receiver credential file must be private')
        key=f.read(514).strip()
    if not 32<=len(key)<=512 or not key.isascii() or any(chr(c).isspace() for c in key):
        raise BackendError('PERMISSION_DENIED','Invalid receiver credential configuration')
    return key.decode('ascii')


class ReceiverClient:
    def __init__(self,station,*,allow_loopback_http=False):
        self.station=station;self.url=urlsplit(station['endpoint']);self.test=allow_loopback_http
        if (self.url.scheme!='https' and not (allow_loopback_http and self.url.scheme=='http' and self.url.hostname in ('127.0.0.1','::1'))) or self.url.path not in ('','/') or self.url.username or self.url.password or self.url.query or self.url.fragment:
            raise BackendError('INVALID_ARGUMENT','Invalid receiver origin')

    def request(self,method,path,*,item=None,payload=None,checkpoint=lambda n:None):
        connection=None
        try:
            token=read_secret(self.station['secretFile'])
            cls=http.client.HTTPSConnection if self.url.scheme=='https' else http.client.HTTPConnection
            connection=cls(self.url.hostname,self.url.port,timeout=10,**({'context':ssl.create_default_context()} if self.url.scheme=='https' else {}))
            headers={'Authorization':'Bearer '+token,'Accept':'application/json','User-Agent':'ArchiveRadio/1.0','Connection':'close'}
            if payload is not None:
                headers.update({'Content-Type':'audio/mpeg','Content-Length':item['_manifest']['sizeBytes'],'X-Archive-SHA256':item['_manifest']['sha256']})
            connection.putrequest(method,PREFIX+path)
            for k,v in headers.items():connection.putheader(k,v)
            connection.endheaders();sent=0;deadline=time.monotonic()+3600
            if payload is not None:
                while data:=payload.read(1024*1024):
                    if time.monotonic()>deadline:raise TimeoutError()
                    checkpoint(sent);connection.send(data);sent+=len(data)
                checkpoint(sent)
            response=connection.getresponse();raw=response.read(65537)
            if len(raw)>65536:raise ReceiverError('INVALID_RESPONSE','Receiver response too large')
            if response.status==404 and method=='GET':return None
            if response.status in (401,403):raise ReceiverError('PERMISSION_DENIED','Radio receiver rejected its configured credential')
            if response.status==429 or 500<=response.status<=599:
                retry=response.getheader('Retry-After','0')
                raise ReceiverError('UNAVAILABLE','Receiver temporarily unavailable',True,int(retry) if retry.isdigit() else 0)
            if response.status not in (200,201,202):raise ReceiverError('INVALID_RESPONSE','Receiver rejected delivery (HTTP '+str(response.status)+')')
            try:value=json.loads(raw)
            except (ValueError,UnicodeError):raise ReceiverError('INVALID_RESPONSE','Invalid receiver JSON') from None
            if not isinstance(value,dict):raise ReceiverError('INVALID_RESPONSE','Invalid receiver response')
            if response.status==202:
                if value.get('state')!='receiving':raise ReceiverError('INVALID_RESPONSE','Invalid receiving status')
                raise ReceiverError('UNAVAILABLE','Receiver is still reconciling this delivery',True,5)
            return value
        except (TimeoutError,OSError,http.client.HTTPException):
            raise ReceiverError('UNAVAILABLE','Radio connection interrupted; reconcile receipt before retry',True) from None
        finally:
            if connection:connection.close()

    def capabilities(self):
        value=self.request('GET','/capabilities')
        if not value or value.get('version')!=1 or value.get('receiverId')!=self.station['receiverId'] or 'audio/mpeg' not in value.get('contentTypes',[]) or not isinstance(value.get('maxUploadBytes'),str) or not value['maxUploadBytes'].isdigit():
            raise ReceiverError('INVALID_RESPONSE','Receiver identity or protocol does not match configuration')
        return value

    def receipt(self,item,value):
        m=item['_manifest']
        if not value or set(value)!={'version','receiverId','deliveryId','trackId','sha256','sizeBytes','receivedAt'} or value['version']!=1 or value['receiverId']!=self.station['receiverId'] or value['deliveryId']!=item['deliveryId'] or value['sha256']!=m['sha256'] or value['sizeBytes']!=m['sizeBytes'] or not isinstance(value['trackId'],str) or not 1<=len(value['trackId'])<=256 or not isinstance(value['receivedAt'],str) or not 1<=len(value['receivedAt'])<=64:
            raise ReceiverError('INVALID_RESPONSE','Receiver receipt does not match the prepared MP3')
        return value

    def lookup(self,item):
        value=self.request('GET','/deliveries/'+item['deliveryId'])
        return self.receipt(item,value) if value is not None else None

    def upload(self,item,payload,checkpoint):
        value=self.request('PUT','/deliveries/'+item['deliveryId']+'/content',item=item,payload=payload,checkpoint=checkpoint)
        return self.receipt(item,value)
