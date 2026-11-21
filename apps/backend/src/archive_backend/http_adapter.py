"""Bounded loopback-only HTTP adapter; application owns all authorization/I/O."""
import http.server
import http.client
import json
import re
import socket
import socketserver
import threading
from urllib.parse import parse_qs, urlsplit
from .contracts import BackendError, CHUNK_BYTES, bytes_value, translate_error
from .mcp_adapter import PROTOCOL_VERSION, dispatch, rpc_error

CONTROL_LIMIT = 256 * 1024
OUTPUT_LIMIT = 2 * 1024 * 1024
HEADER_LIMIT = 16 * 1024
TIMEOUT_SECONDS = 10


def decode_json(data):
    def reject_constant(value):
        raise ValueError('Nonfinite number')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate key')
            result[key] = value
        return result
    try:
        return json.loads(data.decode('utf-8'), parse_constant=reject_constant, object_pairs_hook=unique)
    except (ValueError, UnicodeError, RecursionError):
        raise BackendError('INVALID_ARGUMENT', 'Invalid JSON body') from None


class BoundedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16
    def __init__(self, service, host, port, max_connections=16):
        if host not in ('127.0.0.1', '::1') or type(port) is not int or not 0 <= port <= 65535:
            raise BackendError('INVALID_ARGUMENT', 'Explicit loopback listener required')
        if type(max_connections) is not int or not 1 <= max_connections <= 64:
            raise BackendError('INVALID_ARGUMENT', 'Invalid connection budget')
        self.address_family = socket.AF_INET6 if host == '::1' else socket.AF_INET
        self.service = service
        self.slots = threading.BoundedSemaphore(max_connections)
        super().__init__((host, port), Handler)
        actual_port = self.server_address[1]
        self.authority = ('[::1]' if host == '::1' else host) + ':' + str(actual_port)
    def get_request(self):
        conn, addr = super().get_request()
        conn.settimeout(TIMEOUT_SECONDS)
        return conn, addr
    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise
    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()
    def handle_error(self, request, client_address):
        # Do not emit raw headers, paths, tokens or exception repr to stderr.
        pass


class HeaderReader:
    def __init__(self, reader):
        self.reader = reader
        self.remaining = HEADER_LIMIT
    def readline(self, limit=-1):
        data = self.reader.readline(min(limit, self.remaining + 1) if limit >= 0 else self.remaining + 1)
        self.remaining -= len(data)
        if self.remaining < 0:
            raise http.client.LineTooLong('headers')
        return data


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'ArchiveConsole'
    sys_version = ''
    def parse_request(self):
        original = self.rfile
        self.rfile = HeaderReader(original)
        try:
            return super().parse_request()
        finally:
            self.rfile = original
    def log_message(self, format, *args):
        pass
    def handle_expect_100(self):
        self.send_error(417)
        return False
    def send_error(self, code, message=None, explain=None):
        self.reply(code, {'ok': False, 'error': BackendError('INVALID_ARGUMENT', 'Request rejected').envelope()})
    def reply(self, status, value=None, *, binary=False, revision=None, attachment=False, output_limit=OUTPUT_LIMIT):
        payload = value if binary else (b'' if value is None else json.dumps(value, ensure_ascii=False, allow_nan=False).encode())
        if len(payload) > output_limit:
            status = 503
            payload = b'{"ok":false,"error":{"code":"RESOURCE_EXHAUSTED","message":"Response exceeds bounded transport budget"}}'
            binary = False
        self.send_response(status)
        self.send_header('Content-Type', 'application/octet-stream' if binary else 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        if attachment and binary: self.send_header('Content-Disposition', 'attachment')
        if binary and revision is not None:
            self.send_header('X-Archive-Revision', str(revision))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        if payload:
            self.wfile.write(payload)
    def policy(self):
        if len(self.path) > 8192 or sum(len(k) + len(v) + 4 for k, v in self.headers.items()) > HEADER_LIMIT:
            raise BackendError('INVALID_ARGUMENT', 'Request headers exceed limit')
        for header in ('Host', 'Origin', 'Authorization', 'Content-Length', 'Content-Type', 'MCP-Protocol-Version'):
            if len(self.headers.get_all(header, [])) > 1:
                raise BackendError('INVALID_ARGUMENT', 'Duplicate singleton header')
        if self.headers.get('Host') != self.server.authority:
            raise BackendError('PERMISSION_DENIED', 'Host rejected')
        origin = self.headers.get('Origin')
        if origin is not None and origin != 'http://' + self.server.authority:
            raise BackendError('PERMISSION_DENIED', 'Origin rejected')
        if self.headers.get('Transfer-Encoding') is not None:
            raise BackendError('INVALID_ARGUMENT', 'Chunked request encoding unsupported')
    def token(self):
        value = self.headers.get('Authorization', '')
        if not value.startswith('Bearer ') or not 1 <= len(value[7:]) <= 4096 or any(ord(c) <= 32 or ord(c) >= 127 for c in value[7:]):
            raise BackendError('UNAUTHENTICATED', 'Bearer authentication required')
        return value[7:]
    def body(self, limit, media_type):
        if self.headers.get('Content-Type', '').split(';')[0].strip().lower() != media_type:
            raise BackendError('INVALID_ARGUMENT', 'Unsupported content type')
        length = self.headers.get('Content-Length', '')
        if not length.isascii() or not length.isdecimal() or len(length) > 10 or int(length) > limit:
            raise BackendError('INVALID_ARGUMENT', 'A bounded Content-Length is required')
        data = self.rfile.read(int(length))
        if len(data) != int(length):
            raise BackendError('INVALID_ARGUMENT', 'Incomplete request body')
        return data
    def run_request(self):
        try:
            self.policy()
            parts = urlsplit(self.path)
            if parts.scheme or parts.netloc or parts.fragment:
                raise BackendError('INVALID_ARGUMENT', 'Relative request target required')
            if self.command == 'GET' and parts.path == '/ready' and not parts.query:
                return self.reply(200 if self.server.service.ready else 503, {'ready': bool(self.server.service.ready)})
            if parts.path.startswith('/phone-transfer/v1/'):
                from .phone_transfer_http import handle
                return handle(self,parts)
            token = self.token()
            if parts.path == '/mcp' and not parts.query:
                self.server.service.authenticate(token)
                if self.command != 'POST':
                    return self.reply(405)
                accepted = {part.split(';')[0].strip().lower() for part in self.headers.get('Accept', '').split(',')}
                if 'application/json' not in accepted or 'text/event-stream' not in accepted:
                    return self.reply(406, rpc_error(None, -32600, 'Accept both JSON and event-stream'))
                try:
                    message = decode_json(self.body(CONTROL_LIMIT, 'application/json'))
                except BackendError:
                    return self.reply(400, rpc_error(None, -32700, 'Parse error'))
                version = self.headers.get('MCP-Protocol-Version')
                initialize = isinstance(message, dict) and message.get('method') == 'initialize'
                if (version is not None and version != PROTOCOL_VERSION) or (not initialize and version != PROTOCOL_VERSION):
                    return self.reply(400, rpc_error(None, -32600, 'Unsupported or missing protocol version'))
                result = dispatch(self.server.service, token, message)
                return self.reply(202 if result is None else 200, result, output_limit=16777216 if isinstance(message.get('params'),dict) and message['params'].get('name')=='skills.document' else OUTPUT_LIMIT)
            if parts.path.startswith('/skills/v1/'):
                from .skills_http import route as skills_route, query as skills_query
                params=skills_query(parts.query)
                match=re.fullmatch(r'/skills/v1/(uploads|skills)/([a-zA-Z0-9-]{1,256})/files',parts.path)
                if match:
                    area,ident=match.groups(); principal=self.server.service.authenticate(token)
                    if area=='uploads' and self.command=='PUT' and set(params)=={'path','offset'}:
                        result=self.server.service.skills.chunk(principal,ident,params['path'],params['offset'],self.body(1048576,'application/octet-stream'))
                    elif area=='skills' and self.command=='GET' and 'path' in params and set(params)<={'path','revision'}:
                        data,revision=self.server.service.skills.download(principal,ident,params['path'],params.get('revision'))
                        return self.reply(200,data,binary=True,revision=revision,attachment=True,output_limit=16777216)
                    else: raise BackendError('INVALID_ARGUMENT','Invalid skill file request')
                else:
                    body={} if self.command=='GET' or (self.command=='DELETE' and self.headers.get('Content-Length','0')=='0') else decode_json(self.body(CONTROL_LIMIT,'application/json'))
                    if not isinstance(body,dict):raise BackendError('INVALID_ARGUMENT','Object body required')
                    result=self.server.service.call(token,skills_route(self.command,parts.path,params,body))
                return self.reply(200,{'ok':True,'result':result},output_limit=8388608)
            elif parts.path.startswith('/vault/v1/') and not parts.query:
                from .vault_http import route
                body={} if self.command=='GET' else decode_json(self.body(CONTROL_LIMIT,'application/json'))
                if not isinstance(body,dict):raise BackendError('INVALID_ARGUMENT','Object body required')
                result=self.server.service.call(token,route(self.command,parts.path,body))
            elif self.command == 'POST' and parts.path == '/v1/call' and not parts.query:
                request = decode_json(self.body(CONTROL_LIMIT, 'application/json'))
                result = self.server.service.call(token, request)
                if request.get('capability')=='skills.document': return self.reply(200,{'ok':True,'result':result},output_limit=8388608)
            elif self.command == 'PUT' and parts.path.startswith('/v1/transfers/'):
                transfer_id = parts.path[len('/v1/transfers/'):]
                if not transfer_id or '/' in transfer_id or len(transfer_id) > 256:
                    raise BackendError('INVALID_ARGUMENT', 'Invalid transfer ID')
                query = self.query(parts.query, {'offset'})
                result = self.server.service.write_chunk(token, transfer_id, bytes_value(query['offset']), self.body(CHUNK_BYTES, 'application/octet-stream'))
            elif self.command == 'GET' and parts.path == '/v1/download':
                query = self.query(parts.query, {'driveId', 'path', 'revision', 'offset', 'length'})
                offset = bytes_value(query['offset']); length = bytes_value(query['length'])
                if not 1 <= length <= CHUNK_BYTES:
                    raise BackendError('INVALID_ARGUMENT', 'Invalid download length')
                revision = bytes_value(query['revision'])
                data = self.server.service.download_chunk(token, query['driveId'], query['path'], revision, offset, length)
                if not isinstance(data, bytes) or len(data) > length:
                    raise BackendError('INTERNAL_ERROR', 'Invalid transfer response')
                return self.reply(200, data, binary=True, revision=revision)
            else:
                return self.reply(404, {'ok': False, 'error': BackendError('NOT_FOUND').envelope()})
            self.reply(200, {'ok': True, 'result': result})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception as exc:
            error = translate_error(exc)
            status = 401 if error.code == 'UNAUTHENTICATED' else 403 if error.code in ('PERMISSION_DENIED', 'REVOKED', 'PERMISSION_REVOKED') else 400
            if error.code in ('UNAVAILABLE','VAULT_INTEGRITY','VAULT_UNPROVISIONED','INTEGRITY_ERROR'):status=503
            elif error.code=='CONFLICT':status=409
            elif error.code=='NOT_FOUND':status=404
            elif error.code=='RESOURCE_EXHAUSTED':status=429
            elif error.code=='QUOTA_EXCEEDED':status=507
            self.reply(status, {'ok': False, 'error': error.envelope()})
    @staticmethod
    def query(value, keys):
        try:
            params = parse_qs(value, keep_blank_values=True, strict_parsing=True, max_num_fields=10)
        except ValueError:
            raise BackendError('INVALID_ARGUMENT', 'Invalid query') from None
        if set(params) != keys or any(len(v) != 1 for v in params.values()):
            raise BackendError('INVALID_ARGUMENT', 'Invalid query fields')
        return {k: v[0] for k, v in params.items()}
    do_GET = run_request
    do_POST = run_request
    do_PATCH = run_request
    do_PUT = run_request
    do_DELETE = run_request
    do_OPTIONS = run_request


def create_server(service, host, port, max_connections=16):
    return BoundedServer(service, host, port, max_connections)


def start_server(service, host, port, max_connections=16):
    server = create_server(service, host, port, max_connections)
    thread = threading.Thread(target=server.serve_forever, name='archive-http', daemon=True)
    thread.start()
    return server
