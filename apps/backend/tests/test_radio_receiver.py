import importlib.util,hashlib,io,json,subprocess,tempfile,threading,unittest,uuid
from pathlib import Path
from archive_backend.radio_receiver_client import ReceiverClient,ReceiverError
spec=importlib.util.spec_from_file_location('radio_mock',Path(__file__).resolve().parents[3]/'tools/radio/receiver_mock.py');mock=importlib.util.module_from_spec(spec);spec.loader.exec_module(mock)

class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);key=self.root/'key';key.write_text('fixture-radio-key-'+'x'*40);key.chmod(0o600)
        self.store=mock.Store(self.root/'receiver',key.read_text());self.http=mock.server(self.store);self.thread=threading.Thread(target=self.http.serve_forever,daemon=True);self.thread.start()
        self.client=ReceiverClient({'endpoint':f'http://127.0.0.1:{self.http.server_port}','secretFile':str(key),'receiverId':'archive-radio-station'},allow_loopback_http=True)
        p=self.root/'test.mp3';subprocess.run(['/usr/bin/ffmpeg','-v','error','-f','lavfi','-i','sine=duration=0.1','-c:a','libmp3lame',str(p)],check=True);self.data=p.read_bytes()
        self.item={'deliveryId':str(uuid.uuid4()),'_manifest':{'sha256':hashlib.sha256(self.data).hexdigest(),'sizeBytes':str(len(self.data))}}
    def tearDown(self):self.http.shutdown();self.http.server_close();self.thread.join();self.store.close();self.temp.cleanup()
    def test_upload_replay_conflict_and_unknown(self):
        self.assertEqual(self.client.capabilities()['version'],1);self.assertIsNone(self.client.lookup(self.item))
        receipt=self.client.upload(self.item,io.BytesIO(self.data),lambda n:None)
        self.assertEqual(self.client.lookup(self.item),receipt)
        self.assertEqual(self.client.upload(self.item,io.BytesIO(self.data),lambda n:None),receipt)
        self.item['_manifest']['sha256']='0'*64
        with self.assertRaises(ReceiverError):self.client.upload(self.item,io.BytesIO(self.data),lambda n:None)
        self.assertEqual(self.store.db.execute('select count(*) from deliveries where receipt is not null').fetchone()[0],1)
    def test_lost_ack_then_reconcile(self):
        def fault(point):
            if point=='committed':raise RuntimeError('lost response')
        self.store.fault=fault
        with self.assertRaises(ReceiverError):self.client.upload(self.item,io.BytesIO(self.data),lambda n:None)
        receipt=self.client.lookup(self.item);self.assertEqual(receipt['deliveryId'],self.item['deliveryId'])
    def test_interrupted_body_retries_same_identity(self):
        import socket
        did=self.item['deliveryId'];manifest=self.item['_manifest']
        headers=(f'PUT /archive-radio/v1/deliveries/{did}/content HTTP/1.1\r\n'
                 f'Host: 127.0.0.1\r\nAuthorization: Bearer {self.store.token}\r\n'
                 f'Content-Type: audio/mpeg\r\nContent-Length: {len(self.data)}\r\n'
                 f'X-Archive-SHA256: {manifest["sha256"]}\r\n\r\n')
        with socket.create_connection(('127.0.0.1',self.http.server_port),timeout=5) as sock:
            sock.sendall(headers.encode()+self.data[:len(self.data)//2])
            sock.shutdown(socket.SHUT_WR)
            self.assertEqual(sock.recv(1024),b'')
        self.assertIsNone(self.client.lookup(self.item))
        self.assertFalse((self.root/'receiver'/(did+'.mp3')).exists())
        receipt=self.client.upload(self.item,io.BytesIO(self.data),lambda n:None)
        self.assertEqual(receipt['deliveryId'],did)
        self.assertEqual(self.client.lookup(self.item),receipt)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0],1)
    def test_recover_publication_before_receipt(self):
        def fault(point):
            if point=='published':raise RuntimeError('crash')
        self.store.fault=fault
        with self.assertRaises(ReceiverError):self.client.upload(self.item,io.BytesIO(self.data),lambda n:None)
        self.store.close();recovered=mock.Store(self.root/'receiver','fixture-radio-key-'+'x'*40)
        self.store.db=recovered.db
        self.assertEqual(self.client.lookup(self.item)['deliveryId'],self.item['deliveryId'])
