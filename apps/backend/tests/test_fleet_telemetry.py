import json
import os
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from archive_backend.fleet_telemetry import Spool, health_events, worker
from archive_backend.telemetry import PublishError, payload
from archive_backend.config import parse_config
from archive_backend.contracts import BackendError

class FleetTelemetry(unittest.TestCase):
    def setUp(self): self.previous_umask=os.umask(0o022)
    def tearDown(self): os.umask(self.previous_umask)
    def test_spool_reopen_identity_and_ack(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'spool.db'; spool=Spool(path,'test','archive')
            first=spool.add('health',{'status':'healthy'});spool.close()
            spool=Spool(path,'test','archive')
            self.assertEqual(spool.pending(),first)
            second=spool.event('heartbeat',{})
            self.assertEqual(first['boot_id'],second['boot_id']);self.assertGreater(second['seq'],first['seq'])
            batch={'batch_id':'batch','events':[first]}
            reply={'type':'ack','batch_id':'batch','challenge':'next','accepted':[],'duplicates':[first['event_id']],'retryable':[],'rejected':[]}
            self.assertTrue(spool.ack(batch,reply));self.assertIsNone(spool.pending());spool.close()
    def test_lost_bad_and_retryable_ack_preserve_event(self):
        with tempfile.TemporaryDirectory() as root:
            spool=Spool(Path(root)/'s','test','archive');event=spool.add('health',{})
            batch={'batch_id':'batch','events':[event]}
            reply={'type':'ack','batch_id':'wrong','challenge':'x','accepted':[event['event_id']]}
            with self.assertRaises(PublishError):spool.ack(batch,reply)
            reply.update(batch_id='batch',accepted=[],retryable=[{'event_id':event['event_id']}])
            self.assertFalse(spool.ack(batch,reply));self.assertEqual(spool.pending(),event)
            with self.assertRaises(ValueError):spool.add('heartbeat',{})
            spool.close()
    def test_spool_single_writer(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'s';spool=Spool(path,'test','archive')
            with self.assertRaises(BlockingIOError):Spool(path,'test','archive')
            spool.close()
    def test_all_drives_and_no_sensitive_fields(self):
        snapshot=json.loads(payload({'apiState':'Ready','mcpState':'Ready','token':'SECRET','drives':[{'online':True,'capacityBytes':100*1024**3,'freeBytes':20*1024**3,'path':'SECRET'}]*5}))
        events=list(health_events(snapshot))
        self.assertEqual(events[0][1]['status'],'healthy');self.assertEqual(len(snapshot['drives']),5)
        self.assertNotIn('SECRET',json.dumps(events))
        snapshot['drives'][-1]['online']=False
        self.assertEqual(list(health_events(snapshot))[0][1]['status'],'unhealthy')
    def test_startup_missing_volume_remains_in_health(self):
        import time
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from archive_backend.application import Service
        state=MagicMock();state.transaction.return_value.__enter__.return_value.execute.return_value.fetchone.return_value=[0]
        volume=SimpleNamespace(capacity=lambda:{'total':100*1024**3,'available':20*1024**3})
        service=SimpleNamespace(_started=time.monotonic(),server=True,state=state,config=SimpleNamespace(volumes=[{'id':'missing'},{'id':'online'}]),filesystems={'online':SimpleNamespace(volume=volume)})
        snapshot=Service._health_snapshot(service)
        self.assertFalse(snapshot['drives'][0]['online']);self.assertTrue(snapshot['drives'][1]['online'])
        self.assertEqual(list(health_events(json.loads(payload(snapshot))))[0][1]['status'],'unhealthy')
    def test_low_space_and_missing_status(self):
        self.assertEqual(list(health_events({}))[0][1]['status'],'unhealthy')
        snapshot={'apiState':'Ready','mcpState':'Ready','drives':[{'online':True,'freeBytes':0,'slot':0}]}
        self.assertEqual(list(health_events(snapshot))[0][1]['status'],'degraded')
    def test_fleet_config_rejects_incomplete(self):
        with self.assertRaises(BackendError):parse_config({'telemetry':{'enabled':True,'endpoint':'wss://test','intervalSeconds':30,'protocol':'fleet-v1'}})
    def test_connection_test_reuses_live_publisher(self):
        import time
        from types import SimpleNamespace
        from unittest.mock import Mock
        from archive_backend.application import Service
        publisher=Mock()
        publisher.status.return_value={'state':'Connected','lastSuccessfulSend':time.time()}
        service=SimpleNamespace(config=SimpleNamespace(telemetry={'protocol':'fleet-v1','endpoint':'wss://test','secretFile':'/private/key','intervalSeconds':30}),_settings=lambda:{'health':{}},publisher=publisher,_health_snapshot=lambda:{})
        with patch('archive_backend.telemetry.Publisher') as create:
            self.assertTrue(Service.test_health(service)['success']);create.assert_not_called()
        publisher.submit.assert_called_once()
    def test_spool_expires_old_health(self):
        with tempfile.TemporaryDirectory() as root:
            spool=Spool(Path(root)/'s','test','archive');spool.add('health',{})
            with spool.db:spool.db.execute('UPDATE events SET created=0')
            self.assertIsNone(spool.pending());spool.close()
    def test_live_hello_challenge_health_ack(self):
        with tempfile.TemporaryDirectory() as root:
            secret=Path(root)/'token';secret.write_text('test-only');secret.chmod(0o600)
            samples=queue.Queue();statuses=queue.Queue(100);stop=threading.Event();frames=[]
            samples.put(payload({'apiState':'Ready','mcpState':'Ready','drives':[{'online':True,'freeBytes':20*1024**3}]}))
            class Socket:
                def __enter__(self):return self
                def __exit__(self,*args):pass
                def send(self,value):frames.append(json.loads(value))
                def recv(self,timeout):
                    sent=frames[-1]
                    if sent['type']=='hello':return json.dumps({'type':'welcome','heartbeat_s':15,'challenge':'first'})
                    event=sent['events'][0]
                    if event['kind']=='heartbeat':assert sent['challenge']=='first' and sent['replay'] is False
                    if event['kind']=='health':stop.set()
                    return json.dumps({'type':'ack','batch_id':sent['batch_id'],'accepted':[event['event_id']],'duplicates':[],'rejected':[],'retryable':[],'challenge':'next'})
            config={'nodeId':'test','sourceId':'archive','manifestRevision':1,'spoolPath':str(Path(root)/'spool'),'secretFile':str(secret),'endpoint':'wss://test','intervalSeconds':30}
            with patch('websockets.sync.client.connect',return_value=Socket()):worker(config,samples,statuses,stop)
            states=[]
            while not statuses.empty():states.append(statuses.get())
            self.assertEqual(frames[0]['type'],'hello');self.assertEqual(frames[1]['events'][0]['kind'],'heartbeat')
            self.assertEqual(states[-1]['state'],'Connected');self.assertIsNotNone(states[-1]['lastSuccessfulSend'])
