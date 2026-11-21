import http.client
import json
import threading
import unittest
import uuid
import hashlib
import time
from pathlib import Path
from dataclasses import replace
from cryptography.fernet import Fernet
from archive_backend.config import parse_config
from archive_backend.contracts import BackendError
from archive_backend.mobile_boundary import MobileBoundary
from archive_backend.mobile_http import create_server
from support import Fixture


class MobileHttpTests(unittest.TestCase):
    def setUp(self):
        self.f=Fixture()
        self.config={'listen':{'host':'127.0.0.1','port':8124},'publicOrigin':'https://archive.example.invalid','replayKeyFile':'/private/key'}
        self.boundary=MobileBoundary(self.f.service,self.config,replay_key=Fernet.generate_key())
        self.boundary.auth.enroll(self.f.owner,'alice','synthetic password only',['read','write','trash'],[{'driveId':'files','path':''}])
        self.server=create_server(self.boundary,'127.0.0.1',0)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.boundary.close(); self.f.close()
    def request(self,method,path,body=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_address[1],timeout=5)
        default={'Host':'archive.example.invalid','X-Forwarded-Proto':'https','X-Forwarded-For':'192.0.2.1'}
        default.update(headers or {})
        if body is not None: body=json.dumps(body); default['Content-Type']='application/json'
        conn.request(method,path,body,default); response=conn.getresponse(); data=response.read(); result=(response.status,dict(response.headers),json.loads(data) if data else None); conn.close(); return result
    def login(self):
        status,_,data=self.request('POST','/mobile/v1/sessions',{'username':'alice','password':'synthetic password only','deviceLabel':'test','requestId':str(uuid.uuid4())})
        self.assertEqual(status,200); return data
    def test_discovery_profile_and_logout(self):
        status,headers,data=self.request('GET','/mobile/v1/discovery')
        self.assertEqual(status,200); self.assertEqual(data['protocol'],'archive_mobile_file_v1'); self.assertNotIn('Location',headers)
        schema=json.loads((Path(__file__).resolve().parents[3]/'contracts/mobile/mobile-api.schema.json').read_text())
        self.assertEqual(set(data),set(schema['$defs']['Discovery']['required']))
        token=self.login()['accessToken']; bearer={'Authorization':'Bearer '+token}
        status,_,profile=self.request('GET','/mobile/v1/profile',headers=bearer)
        self.assertEqual(status,200); self.assertEqual(len(profile['roots']),1); self.assertEqual(profile['roots'][0]['ref']['driveId'],'files')
        self.assertIsInstance(profile['roots'][0]['ref']['revision'],str); self.assertEqual(profile['route'],'unknown')
        self.assertEqual(set(profile),set(schema['$defs']['Profile']['required']))
        self.assertFalse(next(c for c in profile['capabilities'] if c['name']=='purge')['supported'])
        self.assertEqual(self.request('DELETE','/mobile/v1/sessions/current',headers=bearer)[0],200)
        self.assertEqual(self.request('GET','/mobile/v1/profile',headers=bearer)[0],403)
    def test_no_admin_routes_or_redirects(self):
        for path in ['/v1/call','/mcp','/mobile/v1/profile/','/mobile/v1/sessions/','/mobile/v1/settings']:
            status,headers,_=self.request('GET',path,headers={'Authorization':'Bearer '+self.f.token})
            self.assertEqual(status,404); self.assertNotIn('Location',headers)
    def test_auth_and_forwarding_fail_closed(self):
        self.assertEqual(self.request('GET','/mobile/v1/profile')[0],401)
        for headers in [{'Host':'evil.test'},{'Origin':'https://evil.test'},{'X-Forwarded-Proto':'http'},{'X-Forwarded-For':'192.0.2.1, 127.0.0.1'},{'Forwarded':'proto=https'},{'X-Real-IP':'192.0.2.1'}]:
            self.assertIn(self.request('GET','/mobile/v1/discovery',headers=headers)[0],(400,403))
        self.assertEqual(self.request('GET','/mobile/v1/profile?token=not-allowed')[0],400)
    def test_request_bounds_and_error_shape(self):
        status,_,data=self.request('POST','/mobile/v1/sessions',{'huge':'x'*(256*1024)})
        self.assertEqual(status,400); self.assertEqual(set(data['error']),{'code','message','recovery','retryable','safeServerCode','correlationId'})
    def test_scope_narrowed_profile_and_unavailable_root(self):
        token=self.login()['accessToken']
        self.boundary.auth.assign(self.f.owner,['read'],[{'driveId':'files','path':'missing'}])
        status,_,profile=self.request('GET','/mobile/v1/profile',headers={'Authorization':'Bearer '+token})
        self.assertEqual(status,200); self.assertFalse(profile['roots'][0]['available'])
        self.assertIsNone(profile['capacity'])
        self.assertFalse(next(c for c in profile['capabilities'] if c['name']=='write')['allowed'])
    def test_configuration_no_public_listener_or_plaintext(self):
        parsed=parse_config({'mobile':self.config}); self.assertIsNone(parsed.listen)
        for update in [{'publicOrigin':'http://archive.example.invalid'},{'publicOrigin':'https://archive.example.invalid/'},{'listen':{'host':'0.0.0.0','port':8124}},{'maxAcceptedFileBytes':'-1'}]:
            with self.assertRaises(BackendError): parse_config({'mobile':dict(self.config,**update)})
        with self.assertRaises(BackendError): parse_config({'mobile':self.config,'listen':self.config['listen']})
    def test_duplicate_host(self):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_address[1],timeout=5)
        conn.putrequest('GET','/mobile/v1/discovery',skip_host=True)
        for key,value in [('Host','archive.example.invalid'),('Host','archive.example.invalid'),('X-Forwarded-Proto','https'),('X-Forwarded-For','192.0.2.1')]: conn.putheader(key,value)
        conn.endheaders(); response=conn.getresponse(); self.assertEqual(response.status,400); response.read(); conn.close()

    def test_same_service_owns_both_listeners_and_one_worker_pool(self):
        key=self.f.root/'mobile.key'; key.write_bytes(Fernet.generate_key()); key.chmod(0o600)
        config=dict(self.config,replayKeyFile=str(key),listen={'host':'127.0.0.1','port':0})
        self.f.service.config=replace(self.f.service.config,listen={'host':'127.0.0.1','port':0},mobile=config)
        self.f.service.start()
        self.assertIs(self.f.service.mobile_boundary.service,self.f.service)
        self.assertIs(self.f.service.mobile_boundary.auth.state,self.f.state)
        self.assertIsNotNone(self.f.service.server); self.assertIsNotNone(self.f.service.mobile_server)
        self.assertIsNotNone(self.f.service.pool)

    def test_http_status_taxonomy(self):
        token=self.login()['accessToken']
        for code,status in [('CONFLICT',409),('STALE_REVISION',409),('REQUEST_MISMATCH',409),('CURSOR_EXPIRED',409),('NOT_FOUND',404),('UNSUPPORTED_ENTRY',501),('INSUFFICIENT_SPACE',507),('QUOTA_EXCEEDED',507),('INTERNAL_ERROR',503),('STATE_UNAVAILABLE',503),('INVALID_ARGUMENT',400)]:
            def fail(*args): raise BackendError(code)
            self.boundary.call=fail
            actual,_,data=self.request('POST','/mobile/v1/call',{'method':'metadata.home','params':{}},headers={'Authorization':'Bearer '+token})
            self.assertEqual(actual,status,code)
            if code=='CURSOR_EXPIRED': self.assertEqual(data['error']['code'],'cursor_expired')

    def test_actual_large_upload_route_authorize_and_finalize(self):
        token=self.login()['accessToken']; bearer={'Authorization':'Bearer '+token}
        _,_,profile=self.request('GET','/mobile/v1/profile',headers=bearer)
        data=b'opaque\0'*(400000)
        args={'requestId':str(uuid.uuid4()),'destination':profile['roots'][0]['ref'],'name':'over-1m.bin','size':str(len(data)),'sha256':hashlib.sha256(data).hexdigest(),'collisionPolicy':'ask'}
        status,_,admission=self.request('POST','/mobile/v1/uploads',args,bearer); self.assertEqual(status,200)
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_address[1],timeout=10)
        headers={'Host':'archive.example.invalid','X-Forwarded-Proto':'https','X-Forwarded-For':'192.0.2.1','Authorization':'Bearer '+admission['transferToken'],'Content-Type':'application/octet-stream'}
        conn.request('PUT',admission['contentPath']+'?offset=0',data,headers); response=conn.getresponse(); ack=json.loads(response.read()); conn.close()
        self.assertEqual(response.status,200); self.assertEqual(ack['durableOffset'],str(len(data)))
        path='/mobile/v1/uploads/'+admission['transferId']
        status,_,queried=self.request('GET',path,headers=bearer); self.assertIsNone(queried['transferToken'])
        status,_,authorized=self.request('POST',path+'/authorize',{},bearer); self.assertIsNotNone(authorized['transferToken'])
        status,_,done=self.request('POST',path+'/finalize',{},bearer); self.assertEqual(status,200); self.assertEqual(done['outcome'],'published')
        self.assertEqual((self.f.root/'files/over-1m.bin').read_bytes(),data)

    def test_download_http_range_etag_head_and_accounting(self):
        token=self.login()['accessToken']; bearer={'Authorization':'Bearer '+token}
        raw=self.f.put('range.bin',b'0123456789'); source=self.boundary.metadata.ref(raw)
        status,_,first=self.request('POST','/mobile/v1/downloads',{'requestId':str(uuid.uuid4()),'source':source},bearer)
        self.assertIn(status,(200,202)); path='/mobile/v1/downloads/'+first['representationId']
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            status,_,rep=self.request('GET',path,headers=bearer)
            if rep['state']=='ready': break
            time.sleep(.01)
        self.assertEqual(rep['state'],'ready'); self.assertIsNone(rep['transferToken'])
        status,_,rep=self.request('POST',path+'/authorize',{},bearer)
        def content(method,extra=None,authenticated=True):
            conn=http.client.HTTPConnection('127.0.0.1',self.server.server_address[1],timeout=5)
            headers={'Host':'archive.example.invalid','X-Forwarded-Proto':'https','X-Forwarded-For':'192.0.2.1'}
            if authenticated: headers['Authorization']='Bearer '+rep['transferToken']
            headers.update(extra or {})
            conn.request(method,path+'/content',headers=headers); response=conn.getresponse(); result=(response.status,dict(response.headers),response.read()); conn.close(); return result
        status,headers,data=content('GET',{'Range':'bytes=2-5','If-Range':rep['strongETag']})
        self.assertEqual((status,data),(206,b'2345')); self.assertEqual(headers['Content-Range'],'bytes 2-5/10'); self.assertEqual(headers['ETag'],rep['strongETag'])
        self.assertEqual(content('GET',{'Range':'bytes=2-','If-Range':'"different"'})[0],200)
        status,headers,data=content('HEAD',{'Range':'bytes=2-'})
        self.assertEqual((status,headers['Content-Length'],data),(200,'10',b''))
        status,headers,data=content('GET',{'Range':'bytes=99-'})
        self.assertEqual((status,headers['Content-Range'],data),(416,'bytes */10',b''))
        self.assertEqual(content('GET',{'Range':'bytes=0-'},authenticated=False)[0],401)
        body={'requestId':str(uuid.uuid4()),'bytes':'10','sha256':rep['sha256']}
        status,_,receipt=self.request('POST',path+'/complete',body,bearer)
        self.assertEqual(status,200); self.assertTrue(receipt['acknowledged'])
        self.assertEqual(self.request('POST',path+'/complete',body,bearer)[2],receipt)


    def test_acquisition_discovery_denial_and_storage(self):
        self.assertEqual(self.request('GET','/mobile/v1/acquisitions')[0],401)
        bearer={'Authorization':'Bearer '+self.login()['accessToken']}
        status,_,data=self.request('GET','/mobile/v1/acquisitions',headers=bearer)
        self.assertEqual(status,200);self.assertFalse(data['integrations'][0]['allowed'])
        self.assertEqual(data['protocol'],'archive_mobile_acquisition_v1')
        self.assertEqual(self.request('POST','/mobile/v1/acquisitions/call',{'method':'music.search','params':{'requestId':'x','query':'test','kind':'track'}},bearer)[0],403)
        status,_,data=self.request('GET','/mobile/v1/storage',headers=bearer)
        self.assertEqual(status,200);self.assertEqual(data['maxRoots'],128)
        self.assertEqual([d['id'] for d in data['drives']],['files'])

    def test_acquisition_http_search_replay_control_and_unknown_fields(self):
        import os
        from archive_backend.music_service import MusicService
        from test_music import FakeWorker
        spool=self.f.root/'spool';spool.mkdir()
        self.f.service.music=MusicService(self.f.service,{'socket':'/unused','spool':str(spool),'workerUid':os.getuid()},FakeWorker(spool))
        self.boundary.auth.assign(self.f.owner,['read','write','acquire'],[{'driveId':'files','path':''}])
        bearer={'Authorization':'Bearer '+self.login()['accessToken']}
        def call(method,params):
            return self.request('POST','/mobile/v1/acquisitions/call',{'method':method,'params':params},bearer)
        args={'requestId':'resolve-mobile','selection':{'kind':'album','id':'c'*22}}
        status,_,plan=call('music.resolve',args);self.assertEqual(status,200)
        self.f.service.music.tick()
        status,_,plan=call('music.plan.get',{'id':plan['id']});self.assertEqual(plan['state'],'completed')
        status,_,receipt=call('acquisitions.reconcile',{'method':'music.resolve','requestId':'resolve-mobile'})
        self.assertEqual(status,200);self.assertEqual(receipt['result']['id'],plan['id'])
        status,_,job=call('music.enqueue',{'requestId':'enqueue-mobile','planId':plan['id'],'planRevision':plan['revision'],'destination':{'driveId':'files','path':''}})
        self.assertEqual(status,200)
        status,_,canceled=call('music.jobs.control',{'id':job['id'],'revision':job['revision'],'action':'cancel'})
        self.assertEqual(status,200);self.assertEqual(canceled['state'],'canceled')
        self.assertEqual(call('music.search',{'requestId':'x','query':'x','kind':'playlist'})[0],400)
        self.assertEqual(call('music.jobs.control',{'id':job['id'],'revision':True,'action':'cancel'})[0],400)
        self.assertEqual(call('music.status',{'unexpected':True})[0],400)
        self.assertEqual(call('agents.register',{})[0],501)
        self.assertEqual(call('acquisitions.reconcile',{'method':'music.enqueue','requestId':'absent'})[0],404)
        self.assertNotIn('_actor',json.dumps(receipt))


if __name__=='__main__': unittest.main()
