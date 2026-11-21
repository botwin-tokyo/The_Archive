import unittest
from unittest.mock import patch
from support import Fixture
from archive_backend.contracts import BackendError

class CopyMoveTests(unittest.TestCase):
    def setUp(self): self.f=Fixture(); self.addCleanup(self.f.close)
    def submit(self,kind,entry,key='move'):
        return self.f.call('operations.submit',{'request':{'requestId':key,'kind':kind,'targets':[self.f.ref(entry)],'destination':{'driveId':'backups','path':''}}})
    def test_cleanup_only_retry_keeps_verified_destination_identity(self):
        source=self.f.put('file.bin',b'only copy'*1000); receipt=self.submit('move',source)
        def fail(point):
            if point=='before-source-cleanup': raise OSError('injected source disconnect')
        self.f.service.operations.fault=fail
        with self.assertRaises(BackendError): self.f.service.run_one()
        job=self.f.service.jobs.get(self.f.owner,receipt['jobIds'][0]); self.assertEqual(job['outcome'],'copied-source-remains')
        destination=self.f.service.filesystems['backups'].stat('file.bin'); self.assertTrue((self.f.root/'files/file.bin').exists())
        self.f.service.operations.fault=lambda _:None
        self.f.call('jobs.control',{'id':job['id'],'action':'retry'})
        with patch.object(self.f.service.operations,'_copy',side_effect=AssertionError('must not recopy')): self.f.service.run_one()
        self.assertEqual(self.f.service.filesystems['backups'].stat('file.bin').inode,destination.inode)
        self.assertFalse((self.f.root/'files/file.bin').exists())
        self.assertEqual(self.f.service.jobs.get(self.f.owner,job['id'])['state'],'completed')
    def test_recursive_copy_keeps_empty_directories_and_opaque_bytes(self):
        fs=self.f.service.filesystems['files']; fs.create_dir('folder'); fs.create_dir('folder/empty'); self.f.put('folder/data',b'\x00\xff')
        source=self.f.call('files.list',{'query':{'requestId':'q','location':{'driveId':'files','path':''}}})['entries'][0]
        self.submit('copy',source,'copy'); self.f.service.run_one()
        self.assertTrue((self.f.root/'backups/folder/empty').is_dir()); self.assertEqual((self.f.root/'backups/folder/data').read_bytes(),b'\x00\xff')
    def test_target_volume_loss_preserves_source_and_other_drive(self):
        source=self.f.put(); receipt=self.submit('move',source)
        self.f.service.registry.get('backups').close()
        with self.assertRaises(BackendError): self.f.service.run_one()
        self.assertEqual((self.f.root/'files/a.bin').read_bytes(),b'opaque\0data')
        self.assertEqual(self.f.call('files.list',{'query':{'requestId':'q','location':{'driveId':'files','path':''}}})['total'],1)
        self.assertNotEqual(self.f.service.jobs.get(self.f.owner,receipt['jobIds'][0])['state'],'completed')
