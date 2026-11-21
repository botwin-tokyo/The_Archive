import io,json,subprocess,threading,time,unittest
from pathlib import Path
import test_radio_service as fixtures
import test_radio_receiver as receivers
from archive_backend.radio_delivery import RadioDispatcher
from archive_backend.radio_receiver_client import ReceiverClient
from archive_backend.contracts import BackendError

class DeliveryTests(unittest.TestCase):
    def setUp(self):
        fixtures.RadioServiceTests.setUp(self)
        key=self.f.root/'key';key.write_text('fixture-radio-key-'+'x'*40);key.chmod(0o600)
        self.store=receivers.mock.Store(self.f.root/'receiver',key.read_text(),receiver_id='station')
        self.http=receivers.mock.server(self.store);self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
        self.config['enabled']=True;self.config['stations'][0]['endpoint']=f'http://127.0.0.1:{self.http.server_port}'
        self.path=self.f.root/'files'/'Music'/'real.mp3'
        subprocess.run(['/usr/bin/ffmpeg','-v','error','-f','lavfi','-i','sine=duration=0.05','-c:a','libmp3lame',str(self.path)],check=True)
        entries=self.f.call('files.list',{'query':{'requestId':'real','location':{'driveId':'files','path':'Music'}}})['entries']
        self.ref=self.f.ref(next(e for e in entries if e['name']=='real.mp3'))
        self.now=[time.time()]
        self.factory=lambda station:ReceiverClient(station,allow_loopback_http=True)
        self.worker=RadioDispatcher(self.f.service.radio,client_factory=self.factory,clock=lambda:self.now[0])
    def tearDown(self):
        self.worker.close();self.http.shutdown();self.http.server_close();self.thread.join();self.store.close();fixtures.RadioServiceTests.tearDown(self)
    def batch(self,refs=None):
        b=self.f.call('radio.batches.create',{'requestId':'create','stationId':'radio','name':'Test'})
        b=self.f.call('radio.batches.add',{'requestId':'add','batchId':b['id'],'revision':b['revision'],'tracks':refs or [self.ref]})
        return self.f.call('radio.batches.push',{'requestId':'push','batchId':b['id'],'revision':b['revision']})
    def test_lost_ack_restart_reconciles_without_duplicate(self):
        b=self.batch();before=self.path.read_bytes()
        def fault(point):
            if point=='committed':raise RuntimeError('lost response')
        self.store.fault=fault;self.worker.tick()
        row=self.f.service.radio.items(b['id'])[0];self.assertTrue(row['_uncertain']);self.assertEqual(row['state'],'retry-wait')
        self.now[0]+=400;self.worker=RadioDispatcher(self.f.service.radio,client_factory=self.factory,clock=lambda:self.now[0]);self.worker.tick();self.worker.tick()
        self.assertEqual(self.f.service.radio.batch(b['id'])['state'],'completed')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM deliveries WHERE receipt IS NOT NULL').fetchone()[0],1)
        self.assertEqual(self.path.read_bytes(),before)
    def test_pause_during_upload_acknowledges_then_resume(self):
        b=self.batch()
        def fault(point):
            if point=='committed':
                fresh=self.f.service.radio.batch(b['id'])
                self.f.call('radio.batches.control',{'requestId':'pause','batchId':b['id'],'revision':fresh['revision'],'action':'pause'})
        self.store.fault=fault;self.worker.tick()
        fresh=self.f.service.radio.batch(b['id']);self.assertEqual(fresh['state'],'paused');self.assertEqual(fresh['deliveredCount'],1)
        self.f.call('radio.batches.control',{'requestId':'resume','batchId':b['id'],'revision':fresh['revision'],'action':'resume'});self.worker.tick()
        self.assertEqual(self.f.service.radio.batch(b['id'])['state'],'completed')
    def test_cancel_uncertain_reconciles_real_receipt(self):
        b=self.batch()
        def fault(point):
            if point=='committed':raise RuntimeError('lost')
        self.store.fault=fault;self.worker.tick();fresh=self.f.service.radio.batch(b['id'])
        result=self.f.call('radio.batches.control',{'requestId':'cancel','batchId':b['id'],'revision':fresh['revision'],'action':'cancel'})
        self.assertEqual(result['state'],'canceling');self.now[0]+=400;self.worker.tick()
        final=self.f.service.radio.batch(b['id']);self.assertEqual(final['state'],'canceled');self.assertEqual(final['deliveredCount'],1)
    def test_station_lease_prevents_second_dispatcher(self):
        self.batch();claim=self.worker.claim('radio');other=RadioDispatcher(self.f.service.radio,client_factory=self.factory,clock=lambda:self.now[0]);self.assertIsNone(other.claim('radio'))
        self.now[0]+=46;self.assertIsNotNone(other.claim('radio'))
        with self.f.state.transaction():
            with self.assertRaises(BackendError):self.worker.fenced('radio',claim[1])
    def test_128_real_mp3_deliveries_order_and_source_preservation(self):
        data=self.path.read_bytes();refs=[]
        from archive_backend.catalog import present_entry
        for i in range(128):
            name=f'Music/{i:03}.mp3';(self.f.root/'files'/name).write_bytes(data)
            refs.append(self.f.ref(present_entry(self.f.service.filesystems['files'].stat(name),'files')))
        # Exercise the real HTTP MCP adapter with a scoped radio agent.
        import urllib.request
        from archive_backend.http_adapter import create_server
        token='radio-mcp-fixture-'+('z'*40)
        agent=self.f.service.auth.register(self.f.owner,'MCP Radio',token,['read','radio'],[{'driveId':'files','path':'Music'}]);self.config['stations'][0]['agentIds']=[agent.id]
        http=create_server(self.f.service,'127.0.0.1',0);thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        self.addCleanup(thread.join);self.addCleanup(http.server_close);self.addCleanup(http.shutdown)
        def rpc(method,params):
            request=urllib.request.Request(f'http://127.0.0.1:{http.server_port}/mcp',data=json.dumps({'jsonrpc':'2.0','id':1,'method':method,'params':params}).encode(),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json','Accept':'application/json, text/event-stream','MCP-Protocol-Version':'2025-11-25'})
            with urllib.request.urlopen(request) as response:result=json.load(response)['result']
            if result.get('isError'):raise AssertionError(result)
            return result
        rpc('initialize',{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'radio-test','version':'1'}})
        def call(capability,arguments=None):return rpc('tools/call',{'name':capability,'arguments':arguments or {}})['structuredContent']
        self.f.call=call
        self.assertEqual(len(call('radio.tracks.list',{'location':{'driveId':'files','path':'Music'},'limit':100})['tracks']),100)
        b=self.batch(refs);order=[]
        def fault(point):
            if point=='committed':
                order.append(self.store.db.execute('SELECT id FROM deliveries ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        self.store.fault=fault
        expected=[i['deliveryId'] for i in self.f.service.radio.items(b['id'])]
        for _ in range(129):self.worker.tick()
        page=self.f.call('radio.batches.get',{'batchId':b['id'],'limit':100});self.assertEqual(len(page['items']),100)
        result=self.f.service.radio.batch(b['id']);self.assertEqual(result['state'],'completed');self.assertEqual(result['deliveredCount'],128);self.assertEqual(order,expected)
        self.assertEqual(len({i['receipt']['trackId'] for i in self.f.service.radio.items(b['id'])}),128)
        for i in range(128):self.assertEqual((self.f.root/'files'/'Music'/f'{i:03}.mp3').read_bytes(),data)
    def test_source_changed_blocks_without_upload(self):
        b=self.batch();self.path.write_bytes(b'changed');self.worker.tick()
        self.assertEqual(self.f.service.radio.batch(b['id'])['state'],'blocked')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0],0)
    def test_cancel_before_send_preserves_source(self):
        b=self.batch();self.f.call('radio.batches.control',{'requestId':'cancel','batchId':b['id'],'revision':b['revision'],'action':'cancel'});self.worker.tick()
        self.assertEqual(self.f.service.radio.batch(b['id'])['state'],'canceled');self.assertTrue(self.path.exists())
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0],0)
    def test_missing_station_grant_blocks_accepted_agent_work(self):
        token='radio-agent-'+('x'*40)
        agent=self.f.service.auth.register(self.f.owner,'Radio',token,['read','radio'],[{'driveId':'files','path':'Music'}]);self.config['stations'][0]['agentIds']=[agent.id]
        b=self.f.call('radio.batches.create',{'requestId':'a','stationId':'radio','name':'agent'},token)
        b=self.f.call('radio.batches.add',{'requestId':'b','batchId':b['id'],'revision':b['revision'],'tracks':[self.ref]},token)
        b=self.f.call('radio.batches.push',{'requestId':'c','batchId':b['id'],'revision':b['revision']},token)
        self.config['stations'][0]['agentIds']=[];self.worker.tick()
        self.assertEqual(self.f.service.radio.batch(b['id'])['state'],'blocked')
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0],0)
