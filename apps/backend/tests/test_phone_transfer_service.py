import hashlib
import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
import uuid
from support import Fixture
from archive_backend.phone_transfer_store import PhoneStore
from archive_backend.phone_transfer_contracts import INBOX,LIMITS
from archive_backend.http_adapter import create_server

class PhoneServiceTests(unittest.TestCase):
    def setUp(self):
        self.f=Fixture();self.tmp=tempfile.TemporaryDirectory();self.s=self.f.service.phone_transfer
        self.s.store=PhoneStore(self.tmp.name)
        self.token='phone-full-'+('x'*40)
        self.agent=self.f.service.auth.register(self.f.owner,'phone',self.token,['phone_transfer.send','phone_transfer.read','phone_transfer.delete'],[])
        self.s.config={'enabled':True,'quotaBytes':LIMITS['quotaBytes'],'grants':[{'principalId':self.agent.id,'inboxId':INBOX}]}
        self.http=create_server(self.f.service,'127.0.0.1',0);self.thread=threading.Thread(target=self.http.serve_forever);self.thread.start()
        self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def tearDown(self):
        self.http.shutdown();self.http.server_close();self.thread.join();self.f.close();self.tmp.cleanup()
    def request(self,method,path,value=None,raw=None,headers=None,token=None):
        hs={'Authorization':'Bearer '+(token or self.token),'Content-Type':'application/octet-stream' if raw is not None else 'application/json',**(headers or {})}
        req=urllib.request.Request('http://127.0.0.1:'+str(self.http.server_port)+path,data=raw if raw is not None else json.dumps(value).encode() if value is not None else None,headers=hs,method=method)
        try:r=self.opener.open(req)
        except urllib.error.HTTPError as e:r=e
        with r:
            data=r.read();result=json.loads(data) if r.headers.get_content_type()=='application/json' else data
            return r.status,result,r.headers
    def api(self,method,path,value=None,**kw):return self.request(method,'/phone-transfer/v1'+path,value,**kw)
    def prep(self,data=b''):
        args={'requestId':str(uuid.uuid4()),'inboxId':INBOX,'title':'Unicode Ω','attachments':[{'clientAttachmentId':'a','filename':'../file.txt','mediaType':'text/plain','byteLength':len(data),'sha256':hashlib.sha256(data).hexdigest()}]}
        status,value,_=self.api('POST','/deliveries/prepare',args);self.assertEqual(status,201,value)
        return args,value['result']
    def test_http_binary_retries_and_receipts(self):
        data=b'abcd'*(1024*1024)+b'tail';args,d=self.prep(data);did=d['deliveryId'];aid=d['attachments'][0]['attachmentId'];base='/deliveries/'+did
        self.assertEqual(self.api('POST','/deliveries/prepare',args)[0],200)
        self.assertEqual(self.api('GET',base)[0],404)
        self.assertEqual(self.api('GET','/inboxes/'+INBOX+'/deliveries')[1]['result']['items'],[])
        for start in range(0,len(data),LIMITS['chunkBytes']):
            chunk=data[start:start+LIMITS['chunkBytes']];hs={'Content-Range':f'bytes {start}-{start+len(chunk)-1}/{len(data)}','X-Chunk-SHA256':hashlib.sha256(chunk).hexdigest()}
            for _ in range(2):self.assertEqual(self.api('PUT',base+'/attachments/'+aid+'/chunks',raw=chunk,headers=hs)[0],200)
        for _ in range(2):self.assertEqual(self.api('POST',base+'/commit',{'requestId':args['requestId']})[0],200)
        status,got,headers=self.api('GET',base+'/attachments/'+aid+'/content');self.assertEqual((status,got),(200,data));self.assertEqual(headers['X-Content-Type-Options'],'nosniff')
        self.assertEqual(self.api('GET',base+'/attachments/'+aid+'/content',headers={'Range':'bytes=-4'})[:2],(206,b'tail'))
        self.assertEqual(self.api('GET',base+'/attachments/'+aid+'/content',headers={'Range':'bytes=0-1','If-Range':'different'})[:2],(200,data))
        device=str(uuid.uuid4());device_body={'platform':'android','displayName':'Phone','pushToken':None,'notificationsEnabled':True}
        self.assertEqual(self.api('PUT','/inboxes/'+INBOX+'/devices/'+device,device_body)[0],200)
        self.assertEqual(self.api('GET',base)[1]['result']['receipts'],[])
        result=self.api('PUT',base+'/receipts/'+device,{'downloadedAttachmentIds':[aid],'opened':True})[1]['result'];self.assertTrue(result['downloadedAt']);self.assertTrue(result['openedAt'])
        snap=self.api('GET','/inboxes/'+INBOX+'/deliveries')[1]['result']
        delete={'requestId':str(uuid.uuid4())}
        for _ in range(2):self.assertEqual(self.api('POST',base+'/delete',delete)[0],200)
        self.assertEqual(self.api('GET',base)[0],410)
        feed=self.api('GET','/inboxes/'+INBOX+'/changes?cursor='+snap['syncCursor'])[1]['result'];self.assertEqual(len(feed['events']),1);self.assertEqual(feed['events'][0]['kind'],'deleted')
        self.assertEqual(self.api('POST',base+'/commit',{'requestId':args['requestId']})[0],410)
    def test_scope_grant_sender_and_revocation(self):
        args,d=self.prep();other='phone-send-'+('z'*40)
        agent=self.f.service.auth.register(self.f.owner,'sender',other,['phone_transfer.send'],[])
        self.assertEqual(self.api('GET','/capabilities',token=other)[0],403)
        self.s.config['grants'].append({'principalId':agent.id,'inboxId':INBOX})
        self.assertEqual(self.api('GET','/capabilities',token=other)[0],200)
        self.assertEqual(self.api('GET','/inboxes/'+INBOX+'/deliveries',token=other)[0],403)
        self.assertEqual(self.api('GET','/operations/'+args['requestId'],token=other)[0],404)
        self.assertEqual(self.api('GET','/deliveries/'+d['deliveryId']+'/uploads',token=other)[0],404)
        bad=dict(args,inboxId='other',requestId=str(uuid.uuid4()))
        self.assertEqual(self.api('POST','/deliveries/prepare',bad,token=other)[0],403)
        self.f.service.auth.revoke(self.f.owner,agent.id)
        self.assertEqual(self.api('GET','/capabilities',token=other)[0],401)
    def test_discovery_and_invalid_requests(self):
        hs={'Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2025-11-25'}
        for method,params in [('resources/read',{'uri':'archive://skills/phone-transfer-hold'}),('tools/call',{'name':'phone_transfer.capabilities','arguments':{}})]:
            status,result,_=self.request('POST','/mcp',{'jsonrpc':'2.0','id':1,'method':method,'params':params},headers=hs)
            self.assertEqual(status,200);self.assertNotIn('error',result)
        self.assertFalse(self.api('GET','/capabilities')[1]['result']['pushEnabled'])
        self.assertEqual(self.api('POST','/deliveries/prepare',raw=b'{}')[0],415)
        self.assertEqual(self.api('GET','/inboxes/'+INBOX+'/deliveries?limit=1&limit=2')[0],400)
        dev={'platform':'android','displayName':'Phone','pushToken':'not-supported','notificationsEnabled':True}
        self.assertEqual(self.api('PUT','/inboxes/'+INBOX+'/devices/'+str(uuid.uuid4()),dev)[0],400)
        self.assertEqual(self.api('GET','/openapi.json')[0],200)
