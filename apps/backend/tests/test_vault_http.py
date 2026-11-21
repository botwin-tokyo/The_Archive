import json,threading,urllib.request,unittest
import test_vault_service as fixture
from archive_backend.http_adapter import create_server
class HttpTests(unittest.TestCase):
 def setUp(self):
  fixture.ServiceTests.setUp(self);self.token='vault-full-test-'+('k'*40)
  self.f.service.auth.register(self.f.owner,'vault',self.token,['vault.read','vault.reveal','vault.write','vault.export'],[])
  self.http=create_server(self.f.service,'127.0.0.1',0);self.thread=threading.Thread(target=self.http.serve_forever);self.thread.start()
 def open(self):return fixture.ServiceTests.open(self)
 def tearDown(self):self.http.shutdown();self.http.server_close();self.thread.join();fixture.ServiceTests.tearDown(self)
 def request(self,path,value):
  r=urllib.request.Request(f'http://127.0.0.1:{self.http.server_port}'+path,data=json.dumps(value).encode(),headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json','Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2025-11-25'})
  with urllib.request.urlopen(r) as response:self.assertEqual(response.headers['Cache-Control'],'no-store');return json.load(response)
 def test_api_and_mcp_parity(self):
  e=self.request('/vault/v1/entries',{'requestId':'http','displayName':'HTTP','login':' Name ','password':'  A_\n👋 '})['result']
  r=self.request('/mcp',{'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'vault.entries.reveal','arguments':{'id':e['id']}}})
  self.assertEqual(r['result']['structuredContent']['password'],'  A_\n👋 ')
  r=self.request('/mcp',{'jsonrpc':'2.0','id':2,'method':'resources/read','params':{'uri':'archive://skills/password-manager'}})
  self.assertIn('requestId',r['result']['contents'][0]['text'])
