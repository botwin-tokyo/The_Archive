import hashlib
import unittest
from support import Fixture
from archive_backend.contracts import BackendError,CHUNK_BYTES

class TransferTests(unittest.TestCase):
    def setUp(self): self.f=Fixture(); self.addCleanup(self.f.close)
    def begin(self,data=b'',name='out.bin'):
        return self.f.call('transfers.initiate',{'requestId':'u-'+name,'destination':{'driveId':'files','path':''},'name':name,'sizeBytes':str(len(data)),'sha256':hashlib.sha256(data).hexdigest()})
    def test_binary_stream_replay_finalize_download(self):
        data=b'\x00\xffhello'*(CHUNK_BYTES//7+10); session=self.begin(data)
        tid=session['transferId']
        for offset in range(0,len(data),CHUNK_BYTES):
            chunk=data[offset:offset+CHUNK_BYTES]; response=self.f.service.write_chunk(self.f.token,tid,offset,chunk)
            self.assertEqual(int(response['offset']),offset+len(chunk))
            self.assertEqual(self.f.service.write_chunk(self.f.token,tid,offset,chunk),response)
        result=self.f.call('transfers.finalize',{'transferId':tid})
        self.assertEqual(result['verification']['digest'],hashlib.sha256(data).hexdigest())
        self.assertEqual(self.f.call('transfers.finalize',{'transferId':tid}),result)
        ref=self.f.ref(result['entry']); start=self.f.call('transfers.download',{'ref':ref,'requestId':'d'})
        got=b''.join(self.f.service.download_chunk(self.f.token,'files','out.bin',ref['revision'],o,CHUNK_BYTES) for o in range(0,len(data),CHUNK_BYTES))
        self.assertEqual(got,data)
        self.f.call('transfers.downloadProgress',{'transferId':start['transferId'],'offset':str(len(data)),'complete':True})
        self.assertTrue(all(j['state']=='completed' for j in self.f.call('jobs.snapshot')['jobs']))
    def test_stale_volume_does_not_strand_a_writer_lease(self):
        session=self.begin(b'abc');tid=session['transferId']
        record=self.f.service.state.get('transfers',tid);original=record['generation']
        record['generation']=original+1;self.f.service.state.put('transfers',tid,record)
        with self.assertRaises(BackendError):self.f.service.write_chunk(self.f.token,tid,0,b'abc')
        job=self.f.service.state.get('jobs',session['jobId'])
        self.assertEqual(job['state'],'waiting-for-client');self.assertIsNone(job['_lease'])
        record['generation']=original;self.f.service.state.put('transfers',tid,record)
        self.f.service.write_chunk(self.f.token,tid,0,b'abc')
        record=self.f.service.state.get('transfers',tid);record['generation']=original+1
        self.f.service.state.put('transfers',tid,record)
        with self.assertRaises(BackendError):self.f.call('transfers.finalize',{'transferId':tid})
        job=self.f.service.state.get('jobs',session['jobId'])
        self.assertEqual(job['state'],'waiting-for-client');self.assertIsNone(job['_lease'])
    def test_empty_and_bad_range_hash(self):
        session=self.begin(); self.f.call('transfers.finalize',{'transferId':session['transferId']})
        session=self.begin(b'abc','bad')
        with self.assertRaises(BackendError): self.f.service.write_chunk(self.f.token,session['transferId'],1,b'a')
        self.f.service.write_chunk(self.f.token,session['transferId'],0,b'xyz')
        with self.assertRaises(BackendError): self.f.call('transfers.finalize',{'transferId':session['transferId']})
        self.assertFalse((self.f.root/'files'/'bad').exists())
    def test_cancel_discard_retains_final_files(self):
        session=self.begin(b'abc'); self.f.service.write_chunk(self.f.token,session['transferId'],0,b'a')
        self.f.call('transfers.discard',{'transferId':session['transferId']})
        self.assertFalse((self.f.root/'files'/'out.bin').exists())
    def test_chunk_bound_and_quota(self):
        session=self.begin(b'a')
        with self.assertRaises(BackendError): self.f.service.write_chunk(self.f.token,session['transferId'],0,b'a'*(CHUNK_BYTES+1))
        with self.assertRaises(BackendError): self.f.service.write_chunk(self.f.token,session['transferId'],0,b'ab')
