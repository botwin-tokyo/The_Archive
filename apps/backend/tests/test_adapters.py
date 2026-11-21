import io
import json
import unittest
from types import SimpleNamespace
from archive_backend.contracts import CAPABILITIES, BackendError
from archive_backend.http_adapter import Handler, decode_json, CONTROL_LIMIT
from archive_backend.mcp_adapter import dispatch, tool_list, PROTOCOL_VERSION

class FakeService:
    ready = True
    def __init__(self): self.calls=[]
    def authenticate(self, token):
        if token != 'test-token': raise BackendError('UNAUTHENTICATED')
        return {'id':'test'}
    def call(self, token, request):
        self.authenticate(token); self.calls.append(request)
        if request['arguments'].get('fail'): raise ValueError('SECRET path /private')
        return {'capability':request['capability']}
    def write_chunk(self, token, transfer_id, offset, data):
        self.authenticate(token); return {'offset':str(offset+len(data))}
    def download_chunk(self, token, drive_id, path, revision, offset, length):
        self.authenticate(token); return b'x'*length

class FakeSocket:
    def __init__(self, request): self.input=io.BytesIO(request); self.output=io.BytesIO()
    def makefile(self, *args): return self.input
    def sendall(self, value): self.output.write(value)

class Adapters(unittest.TestCase):
    def setUp(self): self.service=FakeService()
    def request(self, method, target, body=b'', headers=None):
        values={'Host':'127.0.0.1:8000','Authorization':'Bearer test-token','Content-Length':str(len(body)),'Content-Type':'application/json'}
        values.update(headers or {})
        raw=(method+' '+target+' HTTP/1.1\r\n'+''.join(k+': '+v+'\r\n' for k,v in values.items())+'\r\n').encode()+body
        conn=FakeSocket(raw)
        Handler(conn, ('127.0.0.1',1234), SimpleNamespace(authority='127.0.0.1:8000',service=self.service))
        head, result=conn.output.getvalue().split(b'\r\n\r\n',1)
        self.last_headers = head
        return int(head.split(b' ')[1]), result
    def test_api(self):
        status,data=self.request('POST','/v1/call',json.dumps({'version':1,'capability':'drives.list','arguments':{}}).encode())
        self.assertEqual(status,200); self.assertTrue(json.loads(data)['ok'])
    def test_host_origin_auth(self):
        for headers,expected in [({'Host':'evil.example'},403),({'Origin':'https://evil.example'},403),({'Origin':'null'},403),({'Authorization':'Bearer wrong'},401)]:
            status,_=self.request('POST','/v1/call',b'{}',headers)
            self.assertEqual(status,expected)
    def test_body_limits_and_safe_errors(self):
        status,_=self.request('POST','/v1/call',b'',{'Content-Length':str(CONTROL_LIMIT+1)})
        self.assertEqual(status,400)
        _,data=self.request('POST','/v1/call',json.dumps({'version':1,'capability':'x','arguments':{'fail':True}}).encode())
        self.assertNotIn(b'SECRET',data); self.assertNotIn(b'/private',data)
        for data in (b'{"a":1,"a":2}', b'{"a":NaN}', b'\xff'):
            with self.assertRaises(BackendError): decode_json(data)
    def test_transfer_routes(self):
        status,data=self.request('PUT','/v1/transfers/session?offset=2',b'ab',{'Content-Type':'application/octet-stream'})
        self.assertEqual(status,200); self.assertEqual(json.loads(data)['result']['offset'],'4')
        status,data=self.request('GET','/v1/download?driveId=d&path=a&revision=1&offset=0&length=3')
        self.assertEqual((status,data),(200,b'xxx'))
        self.assertIn(b'X-Archive-Revision: 1', self.last_headers)
        status,_=self.request('GET','/v1/download?driveId=d&path=a&revision=1&offset=0&length=1048577')
        self.assertEqual(status,400)
    def test_registry_parity(self):
        self.assertEqual({t['name'] for t in tool_list()},set(CAPABILITIES))
        for name in CAPABILITIES:
            result=dispatch(self.service,'test-token',{'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':{}}})
            self.assertEqual(result['result']['structuredContent']['capability'],name)
            self.assertEqual(self.service.calls[-1]['capability'],name)
    def test_mcp_transport(self):
        headers={'Accept':'application/json, text/event-stream','MCP-Protocol-Version':PROTOCOL_VERSION}
        status,data=self.request('POST','/mcp',b'{"jsonrpc":"2.0","id":1,"method":"ping"}',headers)
        self.assertEqual(status,200); self.assertEqual(json.loads(data)['result'],{})
        status,data=self.request('POST','/mcp',b'{"jsonrpc":"2.0","method":"notifications/initialized"}',headers)
        self.assertEqual((status,data),(202,b''))
        self.assertEqual(self.request('GET','/mcp')[0],405)
        self.assertEqual(self.request('POST','/mcp',b'{}')[0],406)
        headers['MCP-Protocol-Version']='bad'
        self.assertEqual(self.request('POST','/mcp',b'{}',headers)[0],400)
    def test_mcp_protocol_errors(self):
        value=dispatch(self.service,'test-token',{'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'bad'}})
        self.assertEqual(value['error']['code'],-32602)
        value=dispatch(self.service,'test-token',{'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'drives.list','arguments':{'fail':True}}})
        self.assertTrue(value['result']['isError']); self.assertNotIn('SECRET',json.dumps(value))
        with self.assertRaises(BackendError): dispatch(self.service,'revoked',{'jsonrpc':'2.0','id':1,'method':'ping'})
    def test_ready_minimal(self):
        status,data=self.request('GET','/ready',headers={'Authorization':''})
        self.assertEqual(status,200); self.assertEqual(json.loads(data),{'ready':True})

    def test_initialize_and_list(self):
        result=dispatch(self.service,'test-token',{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':PROTOCOL_VERSION,'capabilities':{},'clientInfo':{'name':'test','version':'1'}}})
        self.assertEqual(result['result']['protocolVersion'],PROTOCOL_VERSION)
        result=dispatch(self.service,'test-token',{'jsonrpc':'2.0','id':2,'method':'tools/list'})
        self.assertEqual(len(result['result']['tools']),len(CAPABILITIES))
    def test_hard_header_limit(self):
        status,_=self.request('GET','/ready',headers={'X-Padding':'a'*17000})
        self.assertEqual(status,431)
    def test_duplicate_query_and_chunk_encoding(self):
        self.assertEqual(self.request('PUT','/v1/transfers/a?offset=1&offset=2',b'a',{'Content-Type':'application/octet-stream'})[0],400)
        self.assertEqual(self.request('POST','/v1/call',b'{}',{'Transfer-Encoding':'chunked'})[0],400)
