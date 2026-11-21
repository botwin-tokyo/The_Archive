import json
import os
import queue
import tempfile
import time
import unittest
from unittest.mock import patch
from archive_backend.telemetry import Publisher, PublishError, payload, put_latest, worker, read_secret, send_websocket

class Stop:
    def __init__(self): self.stopped=False; self.delays=[]
    def is_set(self): return self.stopped
    def wait(self, delay): self.delays.append(delay); self.stopped=True

class Telemetry(unittest.TestCase):
    def test_payload_allowlist(self):
        raw=payload({'filename':'SECRET','token':'SECRET','apiState':'SECRET','activeJobs':3,'drives':[{'label':'SECRET','path':'/private','freeBytes':10,'online':True}]})
        self.assertNotIn(b'SECRET',raw); self.assertNotIn(b'/private',raw)
        value=json.loads(raw); self.assertEqual(value['activeJobs'],3); self.assertEqual(value['apiState'],'Unknown')
        self.assertIsNone(value['drives'][0]['temperatureCelsius'])
        self.assertEqual(json.loads(payload({'apiState':[]}))['apiState'],'Unknown')
    def test_disabled_has_no_process_or_network(self):
        with patch('archive_backend.telemetry.multiprocessing.get_context') as context:
            for config in ({'enabled':False},{'enabled':True,'endpoint':''}):
                p=Publisher(config); self.assertFalse(p.submit({})); p.close()
            context.assert_not_called()
    def test_queue_bounded_coalesces(self):
        channel=queue.Queue(maxsize=2)
        for i in range(1000): put_latest(channel,i)
        self.assertEqual(channel.qsize(),2); self.assertEqual(channel.get(),998); self.assertEqual(channel.get(),999)
    def run_worker(self,sender):
        samples=queue.Queue(2); statuses=queue.Queue(2); stop=Stop(); samples.put(b'{}')
        worker({'intervalSeconds':5},samples,statuses,stop,sender)
        outputs=[]
        while not statuses.empty(): outputs.append(statuses.get())
        return outputs,stop
    def test_success_and_outage_safe_status(self):
        output,stop=self.run_worker(lambda c,d:None)
        self.assertEqual(output[-1]['state'],'Connected'); self.assertIsNotNone(output[-1]['lastSuccessfulSend'])
        def failed(c,d): raise OSError('SECRET endpoint /private')
        output,stop=self.run_worker(failed)
        self.assertEqual(output[-1]['state'],'Reconnecting'); self.assertNotIn('SECRET',json.dumps(output))
        self.assertLess(stop.delays[0],5)
    def test_auth_circuit(self):
        def rejected(c,d): raise PublishError('AUTH_REJECTED')
        output,stop=self.run_worker(rejected)
        self.assertEqual(output[-1]['state'],'Error'); self.assertEqual(stop.delays,[300])
    def test_secret_file_permission_and_symlink(self):
        with tempfile.TemporaryDirectory() as root:
            name=root+'/key'; link=root+'/link'
            with open(name,'w') as handle: handle.write('test-not-real')
            os.chmod(name,0o600); self.assertEqual(read_secret(name),'test-not-real')
            os.symlink(name,link)
            with self.assertRaises(PublishError): read_secret(link)
            os.chmod(name,0o644)
            with self.assertRaises(PublishError): read_secret(name)
    def test_wss_required_without_network(self):
        with patch('archive_backend.telemetry.socket.create_connection') as connect:
            with self.assertRaises(PublishError): send_websocket({'endpoint':'ws://localhost'},b'{}')
            connect.assert_not_called()
    def test_stalled_worker_isolated(self):
        p=Publisher({'enabled':False})
        p.statuses=queue.Queue(2); p.samples=queue.Queue(2)
        class Process:
            terminated=False
            def is_alive(self): return True
            def terminate(self): self.terminated=True
            def join(self,timeout): pass
        process=Process(); p.process=process
        p.current={'state':'Connecting','attemptAt':time.monotonic()-100,'lastSuccessfulSend':None}
        with patch.object(p,'_dispose_queues'):
            start=time.monotonic(); result=p.status()
        self.assertLess(time.monotonic()-start,0.1); self.assertTrue(process.terminated)
        self.assertEqual(result['errorCode'],'WORKER_STALLED')
        self.assertIs(p.process,process)
        self.assertTrue(p._terminating)
        with patch.object(p,'_start') as start:
            self.assertFalse(p.submit({})); start.assert_not_called()

    def test_websocket_handshake_and_masking(self):
        import base64, hashlib, struct
        class Socket:
            def __init__(self): self.sent=[]; self.response=b''
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def settimeout(self,value): pass
            def sendall(self,data):
                self.sent.append(data)
                if data.startswith(b'GET '):
                    key=data.split(b'Sec-WebSocket-Key: ')[1].split(b'\r\n')[0]
                    accept=base64.b64encode(hashlib.sha1(key+b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
                    self.response=b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: '+accept+b'\r\n\r\n'
            def recv(self,n):
                data=self.response[:n]; self.response=self.response[n:]; return data
        conn=Socket()
        context=type('Context',(),{'wrap_socket':lambda self,raw,server_hostname:conn})()
        with patch('archive_backend.telemetry.read_secret',return_value='test-not-real'), patch('archive_backend.telemetry.socket.create_connection',return_value=conn), patch('archive_backend.telemetry.ssl.create_default_context',return_value=context):
            data=b'x'*200
            send_websocket({'endpoint':'wss://health.invalid/test'},data)
        frame=conn.sent[1]; self.assertEqual(frame[:2],b'\x81\xfe')
        self.assertEqual(struct.unpack('!H',frame[2:4])[0],200)
        mask=frame[4:8]; self.assertEqual(bytes(c^mask[i%4] for i,c in enumerate(frame[8:])),data)
        self.assertEqual(conn.sent[2][:2],b'\x88\x80')
    def test_connection_timeout_is_categorical(self):
        with patch('archive_backend.telemetry.read_secret',return_value='test-not-real'), patch('archive_backend.telemetry.socket.create_connection',side_effect=TimeoutError('SECRET')):
            with self.assertRaises(PublishError) as caught: send_websocket({'endpoint':'wss://health.invalid'},b'{}')
        self.assertEqual(caught.exception.code,'CONNECTION_FAILED')

    def test_process_start_failure_does_not_escape(self):
        with patch('archive_backend.telemetry.multiprocessing.get_context',side_effect=OSError('SECRET')):
            p=Publisher({'enabled':True,'endpoint':'wss://health.invalid'})
            self.assertEqual(p.status()['errorCode'],'WORKER_UNAVAILABLE')
            self.assertFalse(p.submit({})); p.close()
