import hashlib
import unittest
from support import Fixture
from archive_backend.contracts import BackendError

class NativeResumeTests(unittest.TestCase):
    def setUp(self): self.f=Fixture(); self.addCleanup(self.f.close)
    def test_lookup_binds_exact_job_identity_size_and_expected_hash(self):
        data=b'original bytes'; expected=hashlib.sha256(data).hexdigest()
        session=self.f.call('transfers.initiate',{'requestId':'source','destination':{'driveId':'files','path':''},'name':'source','sizeBytes':str(len(data)),'sha256':expected})
        self.f.service.write_chunk(self.f.token,session['transferId'],0,data[:3])
        info=self.f.call('transfers.resumeInfo',{'jobId':session['jobId']})
        self.assertEqual(info['transferId'],session['transferId']); self.assertEqual(info['offset'],'3'); self.assertEqual(info['sha256'],expected)
        self.assertEqual(info['name'],'source'); self.assertEqual(info['sizeBytes'],str(len(data)))
        token='fixture-other-resume-'+('z'*40); self.f.service.auth.register(self.f.owner,'Other',token,['read','write'],[{'driveId':'files','path':''}])
        with self.assertRaises(BackendError): self.f.call('transfers.resumeInfo',{'jobId':session['jobId']},token)
    def test_explicit_download_restart_preserves_id_and_source_revision(self):
        entry=self.f.put('source',b'123456'); session=self.f.call('transfers.download',{'ref':self.f.ref(entry),'requestId':'download'})
        self.f.call('transfers.downloadProgress',{'transferId':session['transferId'],'offset':'3','complete':False})
        reset=self.f.call('transfers.restartDownload',{'transferId':session['transferId']})
        self.assertEqual(reset['offset'],'0'); self.assertEqual(reset['jobId'],session['jobId'])
        info=self.f.call('transfers.resumeInfo',{'jobId':session['jobId']}); self.assertEqual(info['ref'],self.f.ref(entry))
        (self.f.root/'files/source').write_bytes(b'changed')
        with self.assertRaises(BackendError): self.f.call('transfers.restartDownload',{'transferId':session['transferId']})
