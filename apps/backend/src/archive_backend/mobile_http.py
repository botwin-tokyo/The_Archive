"""Dedicated loopback mobile adapter. TLS is terminated by the trusted local proxy.

No legacy commands, MCP, owner settings, proxy URLs, redirects or credentials in
query strings. Request framing stays explicit; large bytes are streamed separately.
"""
import http.server
import ipaddress
import json
import socket
import socketserver
import threading
import re
from urllib.parse import urlsplit, parse_qs

from .contracts import BackendError, translate_error
from .http_adapter import HeaderReader, decode_json
from .mobile_contracts import mobile_error, error_status

CONTROL_LIMIT=256*1024
OUTPUT_LIMIT=2*1024*1024


class MobileServer(socketserver.ThreadingMixIn,http.server.HTTPServer):
    daemon_threads=True
    block_on_close=False
    request_queue_size=16
    def __init__(self,boundary,host,port):
        if host not in ('127.0.0.1','::1') or type(port) is not int or not 0<=port<=65535:
            raise BackendError('INVALID_ARGUMENT','Explicit loopback mobile listener required')
        self.address_family=socket.AF_INET6 if host=='::1' else socket.AF_INET
        self.boundary=boundary; self.slots=threading.BoundedSemaphore(16)
        super().__init__((host,port),MobileHandler)
    def get_request(self):
        conn,addr=super().get_request(); conn.settimeout(30); return conn,addr
    def process_request(self,request,address):
        if not self.slots.acquire(blocking=False): self.shutdown_request(request); return
        try: super().process_request(request,address)
        except BaseException: self.slots.release(); raise
    def process_request_thread(self,request,address):
        try: super().process_request_thread(request,address)
        finally: self.slots.release()
    def handle_error(self,request,address): pass


class MobileHandler(http.server.BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'; server_version='ArchiveMobile'; sys_version=''
    def log_message(self,*args): pass
    def parse_request(self):
        original=self.rfile; self.rfile=HeaderReader(original)
        try: return super().parse_request()
        finally: self.rfile=original
    def handle_expect_100(self): self.send_error(417); return False
    def send_error(self,code,message=None,explain=None):
        self.reply(code,mobile_error(BackendError('INVALID_ARGUMENT','Request rejected')))
    def headers_out(self,status,length,media_type='application/json',extra=None):
        self.response_started=True
        self.send_response(status)
        self.send_header('Content-Type',media_type); self.send_header('Content-Length',str(length))
        self.send_header('Cache-Control','no-store, no-transform'); self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Connection','close')
        for key,value in (extra or {}).items(): self.send_header(key,str(value))
        self.end_headers(); self.close_connection=True
    def reply(self,status,value):
        payload=json.dumps(value,allow_nan=False,ensure_ascii=False,separators=(',',':')).encode()
        if len(payload)>OUTPUT_LIMIT:
            status=503; payload=json.dumps(mobile_error(BackendError('RESOURCE_EXHAUSTED','Mobile response budget exceeded'))).encode()
        self.headers_out(status,len(payload))
        if self.command!='HEAD': self.wfile.write(payload)
    def policy(self):
        if len(self.path)>8192: raise BackendError('INVALID_ARGUMENT','Request target too long')
        for name in ('Host','Origin','Authorization','Content-Length','Content-Type','X-Forwarded-Proto','X-Forwarded-For','Range','If-Range'):
            if len(self.headers.get_all(name,[]))>1: raise BackendError('INVALID_ARGUMENT','Duplicate singleton header')
        for name in ('Transfer-Encoding','Forwarded','X-Forwarded-Host','X-Real-IP','Expect'):
            if self.headers.get(name) is not None: raise BackendError('INVALID_ARGUMENT','Unsupported forwarding or request framing')
        origin=self.server.boundary.config['publicOrigin']
        if self.headers.get('Host')!=urlsplit(origin).netloc or self.headers.get('X-Forwarded-Proto')!='https':
            raise BackendError('PERMISSION_DENIED','Trusted HTTPS proxy authority required')
        if self.headers.get('Origin') is not None and self.headers['Origin']!=origin:
            raise BackendError('PERMISSION_DENIED','Origin rejected')
        try:
            if not ipaddress.ip_address(self.client_address[0]).is_loopback: raise ValueError()
            self.peer=str(ipaddress.ip_address(self.headers.get('X-Forwarded-For','')))
        except ValueError: raise BackendError('PERMISSION_DENIED','Trusted proxy peer required') from None
        parts=urlsplit(self.path)
        if parts.scheme or parts.netloc or parts.fragment or not self.path.startswith('/'):
            raise BackendError('INVALID_ARGUMENT','Relative request target required')
        return parts
    def token(self):
        value=self.headers.get('Authorization','')
        if not value.startswith('Bearer ') or not 32<=len(value[7:])<=512 or not value[7:].isascii() or any(c.isspace() for c in value[7:]):
            raise BackendError('UNAUTHENTICATED','Bearer authentication required')
        return value[7:]
    def content_length(self):
        raw=self.headers.get('Content-Length','')
        if not raw.isascii() or not raw.isdecimal() or len(raw)>19:
            raise BackendError('INVALID_ARGUMENT','Known Content-Length required')
        return int(raw)
    def body(self):
        if self.headers.get('Content-Type','').split(';')[0].strip().lower()!='application/json':
            raise BackendError('INVALID_ARGUMENT','JSON body required')
        length=self.content_length()
        if length>CONTROL_LIMIT: raise BackendError('INVALID_ARGUMENT','Control body too large')
        raw=self.rfile.read(length)
        if len(raw)!=length: raise BackendError('INVALID_ARGUMENT','Incomplete request body')
        return decode_json(raw)
    def run_request(self):
        self.response_started=False
        try:
            parts=self.policy(); boundary=self.server.boundary
            boundary.service.state.verify_guard()
            download=re.fullmatch(r'/mobile/v1/downloads/([a-f0-9]{64})(?:/(content|authorize|cancel|complete))?',parts.path)
            if download and self.command in ('GET','HEAD') and download[2]=='content':
                if parts.query: raise BackendError('INVALID_ARGUMENT','Content query fields are not accepted')
                with boundary.transfers.downloads.content(self.token(),download[1],self.headers.get('Range'),self.headers.get('If-Range'),self.command) as (status,length,headers,chunks):
                    self.headers_out(status,length,'application/octet-stream',headers)
                    if self.command!='HEAD':
                        for chunk in chunks: self.wfile.write(chunk)
                return
            upload=re.fullmatch(r'/mobile/v1/uploads/([a-f0-9]{64})(?:/(content|finalize|cancel|authorize|control))?',parts.path)
            if upload and self.command=='PUT' and upload[2]=='content':
                try: query=parse_qs(parts.query,strict_parsing=True,keep_blank_values=True,max_num_fields=2)
                except ValueError: raise BackendError('INVALID_ARGUMENT','Invalid upload offset query') from None
                if set(query)!={'offset'} or len(query['offset'])!=1: raise BackendError('INVALID_ARGUMENT','Exact upload offset query required')
                from .contracts import bytes_value
                offset=bytes_value(query['offset'][0])
                if self.headers.get('Content-Type','').split(';')[0].strip().lower()!='application/octet-stream':
                    raise BackendError('INVALID_ARGUMENT','File body requires application/octet-stream')
                result=boundary.transfers.write_upload(self.token(),upload[1],offset,self.content_length(),self.rfile)
                return self.reply(200,result)
            if parts.query: raise BackendError('INVALID_ARGUMENT','Query fields not accepted for this route')
            if self.command=='POST' and parts.path in ('/mobile/v1/downloads','/mobile/v1/download-manifests'):
                context=boundary.auth.authenticate(self.token())
                result=boundary.transfers.downloads.initiate(context,self.body()) if parts.path=='/mobile/v1/downloads' else boundary.transfers.downloads.manifest(context,self.body())
                return self.reply(202 if result.get('state')=='preparing' else 200,result)
            if download and ((self.command=='GET' and download[2] is None) or (self.command=='POST' and download[2] in ('authorize','cancel','complete'))):
                context=boundary.auth.authenticate(self.token()); rid,action=download[1],download[2]
                if self.command=='GET': result=boundary.transfers.downloads.snapshot(context,rid)
                else:
                    from .contracts import object_fields
                    body=self.body()
                    if action=='complete': result=boundary.transfers.downloads.complete(context,rid,body)
                    else:
                        object_fields(body)
                        result=boundary.transfers.downloads.snapshot(context,rid,mint=True) if action=='authorize' else boundary.transfers.downloads.cancel(context,rid)
                return self.reply(202 if result.get('state')=='preparing' else 200,result)
            if self.command=='POST' and parts.path=='/mobile/v1/uploads':
                context=boundary.auth.authenticate(self.token())
                return self.reply(200,boundary.transfers.initiate_upload(context,self.body()))
            if upload and ((self.command=='GET' and upload[2] is None) or (self.command=='POST' and upload[2] in ('finalize','cancel','authorize','control'))):
                context=boundary.auth.authenticate(self.token()); tid,action=upload[1],upload[2]
                if self.command=='GET': return self.reply(200,boundary.transfers.upload_snapshot(context,tid))
                from .contracts import object_fields
                body=self.body(); object_fields(body,('action',) if action=='control' else ())
                if action=='finalize': result=boundary.transfers.finalize_upload(context,tid)
                elif action=='authorize': result=boundary.transfers.upload_snapshot(context,tid,mint=True)
                else:
                    control='cancel' if action=='cancel' else body['action']
                    if action=='control' and control not in ('pause','resume','retry'): raise BackendError('INVALID_ARGUMENT','Invalid upload control')
                    result=boundary.transfers.control_upload(context,tid,control)
                return self.reply(200,result)
            if self.command=='GET' and parts.path=='/mobile/v1/discovery':
                return self.reply(200,boundary.discovery())
            if self.command=='POST' and parts.path=='/mobile/v1/sessions':
                return self.reply(200,boundary.auth.login(self.body(),self.peer))
            if self.command=='POST' and parts.path=='/mobile/v1/sessions/refresh':
                return self.reply(200,boundary.auth.refresh(self.body()))
            if (self.command,parts.path) not in {('DELETE','/mobile/v1/sessions/current'),('GET','/mobile/v1/profile'),('POST','/mobile/v1/call'),('GET','/mobile/v1/acquisitions'),('POST','/mobile/v1/acquisitions/call'),('GET','/mobile/v1/storage')}:
                return self.reply(404,mobile_error(BackendError('NOT_FOUND','Mobile route not found')))
            token=self.token(); context=boundary.auth.authenticate(token)
            if self.command=='DELETE': return self.reply(200,boundary.auth.logout(token))
            if parts.path=='/mobile/v1/profile': return self.reply(200,boundary.profile(context))
            if parts.path=='/mobile/v1/acquisitions': return self.reply(200,boundary.acquisitions.discovery(context))
            if parts.path=='/mobile/v1/storage': return self.reply(200,boundary.acquisitions.storage(context))
            if parts.path=='/mobile/v1/acquisitions/call': return self.reply(200,boundary.acquisitions.call(context,self.body()))
            return self.reply(200,boundary.call(context,self.body()))
        except (BrokenPipeError,ConnectionResetError,TimeoutError): self.close_connection=True
        except Exception as exc:
            error=translate_error(exc)
            self.server.boundary.service.observability.record(error.code,level='warning',correlation_id=error.correlation_id)
            if self.response_started:
                # A partial binary stream must end, never append a JSON error or
                # second HTTP status line to representation bytes.
                self.close_connection=True
                return
            value=mobile_error(error)
            if error.code=='INTEGRITY_FAILED' and self.path.startswith('/mobile/v1/uploads'):
                value['error']['recovery']='choose_source'
            if error.code=='INTEGRITY_FAILED' and self.path.startswith('/mobile/v1/downloads'):
                value['error']['recovery']='restart_download'
            self.reply(error_status(error),value)
    do_GET=run_request
    do_HEAD=run_request
    do_POST=run_request
    do_PUT=run_request
    do_DELETE=run_request
    do_OPTIONS=run_request


def create_server(boundary,host,port): return MobileServer(boundary,host,port)
