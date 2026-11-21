"""Bounded JSON control and streaming binary transport for Phone Transfer Hold."""
import errno
import json
import os
import re
import sqlite3
from urllib.parse import parse_qsl
from .contracts import BackendError
from .phone_transfer_contracts import PhoneError, LIMITS, fail

PREFIX='/phone-transfer/v1'
ROUTES=[
 ('GET',r'/capabilities','capabilities'),('POST',r'/deliveries/prepare','prepare'),
 ('GET',r'/operations/(?P<requestId>[\w-]+)','operation'),
 ('GET',r'/deliveries/(?P<deliveryId>[\w-]+)/uploads','uploads'),
 ('POST',r'/deliveries/(?P<deliveryId>[\w-]+)/commit','commit'),
 ('GET',r'/inboxes/(?P<inboxId>[\w-]+)/deliveries','list'),
 ('GET',r'/inboxes/(?P<inboxId>[\w-]+)/changes','changes'),
 ('GET',r'/deliveries/(?P<deliveryId>[\w-]+)','get'),
 ('PUT',r'/inboxes/(?P<inboxId>[\w-]+)/devices/(?P<deviceId>[\w-]+)','device'),
 ('PUT',r'/deliveries/(?P<deliveryId>[\w-]+)/receipts/(?P<deviceId>[\w-]+)','receipt'),
 ('POST',r'/deliveries/(?P<deliveryId>[\w-]+)/delete','delete')]

def body(h,binary=False):
    if h.headers.get('Content-Type','').split(';')[0].strip().lower()!=('application/octet-stream' if binary else 'application/json'):fail('UNSUPPORTED_MEDIA_TYPE',415)
    length=h.headers.get('Content-Length','')
    if not re.fullmatch('[0-9]{1,12}',length):fail('INVALID_CONTENT_LENGTH')
    length=int(length)
    if length>(LIMITS['chunkBytes'] if binary else LIMITS['jsonBytes']):fail('REQUEST_TOO_LARGE',413)
    data=h.rfile.read(length)
    if len(data)!=length:fail('INCOMPLETE_BODY')
    from .http_adapter import decode_json
    return data if binary else decode_json(data)

def reply(h,status,value,limit=LIMITS['jsonBytes']):
    data=json.dumps(value,ensure_ascii=False,separators=(',',':')).encode('utf-8')
    if len(data)>limit:fail('RESPONSE_TOO_LARGE',503)
    h.send_response(status);h.send_header('Content-Type','application/json; charset=utf-8')
    h.send_header('Content-Length',str(len(data)));h.send_header('Cache-Control','no-store');h.send_header('X-Content-Type-Options','nosniff')
    if status==429:h.send_header('Retry-After','60')
    h.end_headers();h.wfile.write(data)

def handle(h,parts):
    streaming=False
    try:
        try:principal=h.server.service.authenticate(h.token())
        except BackendError:fail('UNAUTHENTICATED',401)
        service=h.server.service.phone_transfer
        pairs=parse_qsl(parts.query,keep_blank_values=True,strict_parsing=True)
        if len({k for k,v in pairs})!=len(pairs):fail('INVALID_QUERY')
        query=dict(pairs);path=parts.path[len(PREFIX):]
        if h.command=='GET' and path=='/openapi.json' and not query:
            service.admit(principal,'openapi')
            from .phone_transfer_openapi import document
            return reply(h,200,document(),LIMITS['pageBytes'])
        binary=re.fullmatch(r'/deliveries/([A-Za-z0-9_-]{1,128})/attachments/([A-Za-z0-9_-]{1,128})/(chunks|content)',path)
        if binary:
            if query:fail('INVALID_QUERY')
            delivery,attachment,kind=binary.groups()
            for header in ('Content-Range','X-Chunk-SHA256','Range','If-Range'):
                if len(h.headers.get_all(header,[]))>1:fail('DUPLICATE_HEADER')
            if kind=='chunks' and h.command=='PUT':
                # Authorize before accepting potentially large payloads.
                service.authorize(principal,'chunk');service.delivery_access(principal,'chunk',delivery)
                result=service.chunk(principal,delivery,attachment,h.headers.get('Content-Range'),h.headers.get('X-Chunk-SHA256'),body(h,True))
                return reply(h,200,{'ok':True,'result':result})
            if kind=='content' and h.command=='GET':
                status,length,headers,fd=service.content(principal,delivery,attachment,h.headers.get('Range'),h.headers.get('If-Range'))
                try:
                    streaming=True;h.send_response(status)
                    for name,value in headers.items():h.send_header(name,value)
                    h.send_header('Content-Length',str(length));h.send_header('Cache-Control','no-store');h.end_headers()
                    while length:
                        chunk=os.read(fd,min(65536,length))
                        if not chunk:raise OSError('Incomplete stored content')
                        h.wfile.write(chunk);length-=len(chunk)
                finally:os.close(fd)
                return
            fail('METHOD_NOT_ALLOWED',405)
        for method,pattern,op in ROUTES:
            match=re.fullmatch(pattern,path)
            if not match or h.command!=method:continue
            args=match.groupdict()
            if method=='GET':
                if query and (op not in ('list','changes') or not set(query)<={'limit','cursor'}):fail('INVALID_QUERY')
                if 'limit' in query:
                    if not re.fullmatch('[0-9]{1,3}',query['limit']):fail('INVALID_QUERY')
                    query['limit']=int(query['limit'])
                args.update(query)
            else:
                if query:fail('INVALID_QUERY')
                value=body(h)
                if not isinstance(value,dict) or set(value)&set(args):fail('INVALID_INPUT')
                args.update(value)
            result=service.call(principal,'phone_transfer.'+op,args,http=True);status=200
            if op=='prepare':result,status=result
            return reply(h,status,{'ok':True,'result':result},LIMITS['pageBytes'] if op in ('list','changes') else LIMITS['jsonBytes'])
        fail('NOT_FOUND',404)
    except (BackendError,OSError,sqlite3.Error,ValueError) as error:
        if streaming:h.close_connection=True;return
        if isinstance(error,PhoneError):safe=error
        elif isinstance(error,BackendError):safe=PhoneError(error.code,403 if error.code in ('PERMISSION_DENIED','PERMISSION_REVOKED') else 400)
        elif isinstance(error,OSError) and error.errno in (errno.ENOSPC,errno.EDQUOT):safe=PhoneError('STORAGE_FULL',507)
        elif isinstance(error,ValueError):safe=PhoneError('INVALID_INPUT',400)
        else:safe=PhoneError('STORAGE_UNAVAILABLE',503)
        h.close_connection=True
        return reply(h,safe.status,{'ok':False,'error':safe.envelope()})
