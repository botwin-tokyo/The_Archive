import unittest
from support import Fixture
from archive_backend.contracts import BackendError
from archive_backend.radio_service import RadioService

class RadioServiceTests(unittest.TestCase):
    def setUp(self):
        self.f=Fixture();(self.f.root/'files'/'Music').mkdir()
        self.config={'enabled':False,'spool':str(self.f.root/'spool'),'spoolBudgetBytes':str(2*1024**3),'roots':[{'driveId':'files','path':'Music'}],
                     'stations':[{'id':'radio','receiverId':'station','endpoint':'','secretFile':str(self.f.root/'key'),'agentIds':[]}]}
        self.f.service.radio=RadioService(self.f.service,self.config)
        self.ref=self.f.ref(self.f.put('Music/test.mp3',b'test'))
    def tearDown(self):self.f.close()
    def create(self):return self.f.call('radio.batches.create',{'requestId':'create','stationId':'radio','name':'Test'})
    def add(self,b):return self.f.call('radio.batches.add',{'batchId':b['id'],'requestId':'add','revision':b['revision'],'tracks':[self.ref,self.ref]})
    def test_draft_idempotency_order_and_restart(self):
        b=self.create();self.assertEqual(self.create(),b)
        b=self.add(b);self.assertEqual(b['itemCount'],1)
        r=self.f.call('radio.batches.get',{'batchId':b['id']});self.assertEqual(len(r['items']),1)
        self.f.service.radio=RadioService(self.f.service,self.config)
        self.assertEqual(self.f.call('radio.batches.get',{'batchId':b['id']})['batch'],b)
        with self.assertRaises(BackendError):self.f.call('radio.batches.create',{'requestId':'create','stationId':'radio','name':'Different'})
        with self.assertRaises(BackendError):self.f.call('radio.batches.push',{'requestId':'push','batchId':b['id'],'revision':b['revision']})
        self.config['enabled']=True;self.config['stations'][0]['endpoint']='https://radio.invalid'
        b=self.f.call('radio.batches.push',{'requestId':'push','batchId':b['id'],'revision':b['revision']})
        self.assertEqual(b['state'],'queued')
        with self.assertRaises(BackendError):self.f.call('radio.batches.remove',{'requestId':'remove','batchId':b['id'],'revision':b['revision'],'itemIds':[r['items'][0]['id']]})
    def test_agent_grants_ownership_roots_and_revocation(self):
        token='radio-agent-test-'+'x'*40
        a=self.f.service.auth.register(self.f.owner,'Radio',token,['read','radio'],[{'driveId':'files','path':'Music'}])
        with self.assertRaises(BackendError):self.f.call('radio.batches.create',{'requestId':'x','stationId':'radio','name':'A'},token)
        self.config['stations'][0]['agentIds']=[a.id]
        b=self.f.call('radio.batches.create',{'requestId':'x','stationId':'radio','name':'A'},token)
        with self.assertRaises(BackendError):self.f.call('radio.batches.get',{'batchId':self.create()['id']},token)
        self.f.service.auth.revoke(self.f.owner,a.id)
        with self.assertRaises(BackendError):self.f.call('radio.batches.get',{'batchId':b['id']},token)
    def test_128_tracks_and_stale_revision(self):
        refs=[self.f.ref(self.f.put(f'Music/{i}.mp3',b'mp3')) for i in range(128)]
        b=self.create();args={'batchId':b['id'],'revision':b['revision'],'requestId':'128','tracks':refs}
        b=self.f.call('radio.batches.add',args);self.assertEqual(b['itemCount'],128)
        self.assertEqual(self.f.call('radio.batches.add',args),b)
        with self.assertRaises(BackendError):self.f.call('radio.batches.add',dict(args,requestId='stale'))
        page=self.f.call('radio.batches.get',{'batchId':b['id'],'limit':100});self.assertEqual(len(page['items']),100)
        page=self.f.call('radio.batches.get',{'batchId':b['id'],'cursor':page['nextCursor']});self.assertEqual(len(page['items']),28)
