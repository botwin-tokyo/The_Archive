import hashlib
import unittest
from support import Fixture
from archive_backend.contracts import BackendError

class FolderUploads(unittest.TestCase):
    def setUp(self):self.f=Fixture();self.addCleanup(self.f.close)
    def begin(self,count=1,size=3):
        return self.f.call('transfers.folderBegin',dict(requestId='folder-request',destination={'driveId':'files','path':''},name='External',sizeBytes=str(size),fileCount=count,manifestDigest='a'*64))
    def child(self,folder,path='External',name='card.json'):
        return self.f.call('transfers.initiate',dict(requestId='folder-file-0',destination={'driveId':'files','path':path},name=name,sizeBytes='3',sha256=hashlib.sha256(b'abc').hexdigest(),folderId=folder))
    def test_accept_verify_progress_finish_and_reconcile(self):
        receipt=self.begin();folder=receipt['folderId']
        self.assertEqual(self.begin(),receipt)
        self.f.service.filesystems['files'].create_dir('External')
        child=self.child(folder);tid=child['transferId']
        with self.assertRaises(BackendError):self.f.call('transfers.folderFinish',{'folderId':folder})
        self.f.service.write_chunk(self.f.token,tid,0,b'abc');self.f.call('transfers.finalize',{'transferId':tid})
        for _ in range(2):status=self.f.call('transfers.folderAck',{'folderId':folder,'transferId':tid})
        self.assertEqual(status['completedFiles'],1);self.assertEqual(status['completedBytes'],'3')
        self.assertEqual(self.f.call('transfers.folderFinish',{'folderId':folder})['state'],'completed')
        self.assertEqual(self.f.call('operations.reconcile',{'requestId':'folder-request'})['jobIds'],[folder])
    def test_outside_folder_rejected_and_pause_stops_writes(self):
        folder=self.begin()['folderId']
        with self.assertRaises(BackendError):self.child(folder,path='')
        self.f.service.filesystems['files'].create_dir('External');child=self.child(folder)
        self.f.call('jobs.control',{'id':folder,'action':'pause'})
        with self.assertRaises(BackendError) as caught:self.f.service.write_chunk(self.f.token,child['transferId'],0,b'abc')
        self.assertEqual(caught.exception.code,'PAUSED')
        self.f.call('jobs.control',{'id':folder,'action':'resume'})
        resumed=self.f.call('jobs.get',{'id':folder})
        self.assertEqual(resumed['state'],'waiting-for-client')
        self.assertTrue(resumed['capabilities']['pause'])
        self.f.service.write_chunk(self.f.token,child['transferId'],0,b'abc')
    def test_large_manifest_is_one_durable_parent_and_can_reselect(self):
        folder=self.begin(count=112251,size=19840185358)['folderId']
        self.assertEqual(len(self.f.call('jobs.snapshot')['jobs']),1)
        info=self.f.call('transfers.resumeInfo',{'jobId':folder});self.assertEqual(info['direction'],'folder');self.assertEqual(info['manifestDigest'],'a'*64)
        self.f.call('transfers.folderError',{'folderId':folder,'code':'INTERRUPTED'})
        info=self.f.call('transfers.folderStatus',{'folderId':folder});self.assertEqual(info['state'],'waiting-for-client');self.assertTrue(info['error'])
    def test_existing_folder_not_overwritten(self):
        self.f.service.filesystems['files'].create_dir('External')
        with self.assertRaises(BackendError) as caught:self.begin()
        self.assertEqual(caught.exception.code,'CONFLICT');self.assertFalse(self.f.call('jobs.snapshot')['jobs'])
    def test_unverified_or_foreign_child_cannot_advance(self):
        folder=self.begin()['folderId'];self.f.service.filesystems['files'].create_dir('External');child=self.child(folder)
        with self.assertRaises(BackendError):self.f.call('transfers.folderAck',{'folderId':folder,'transferId':child['transferId']})
        self.assertEqual(self.f.call('transfers.folderStatus',{'folderId':folder})['completedFiles'],0)

    def test_parent_shows_durable_bytes_before_file_verification(self):
        folder=self.begin()['folderId'];self.f.service.filesystems['files'].create_dir('External');child=self.child(folder)
        tid=child['transferId']
        for _ in range(2):self.f.service.write_chunk(self.f.token,tid,0,b'a')
        self.assertEqual(self.f.call('jobs.get',{'id':folder})['bytesDone'],'1')
        self.assertEqual(self.f.call('transfers.folderStatus',{'folderId':folder})['completedFiles'],0)
        self.f.service.write_chunk(self.f.token,tid,1,b'bc')
        self.f.call('transfers.finalize',{'transferId':tid})
        self.f.call('transfers.folderAck',{'folderId':folder,'transferId':tid})
        self.assertEqual(self.f.call('jobs.get',{'id':folder})['bytesDone'],'3')

    def test_legacy_queued_folder_can_resume_without_new_job(self):
        folder=self.begin()['folderId']
        row=self.f.service.state.get('jobs',folder);row['state']='queued'
        self.f.service.state.put('jobs',folder,row)
        self.assertTrue(self.f.call('jobs.get',{'id':folder})['capabilities']['resume'])
        resumed=self.f.call('jobs.control',{'id':folder,'action':'resume'})
        self.assertEqual(resumed['id'],folder)
        self.assertEqual(resumed['state'],'waiting-for-client')
        self.assertTrue(resumed['capabilities']['pause'])

    def test_active_session_queries_use_partial_indexes(self):
        queries=[("SELECT COUNT(*) FROM records WHERE namespace='transfers' AND json_extract(body,'$.actor')=? AND json_extract(body,'$.state') NOT IN ('completed','discarded')",('owner',),'active_transfer_actor'),("SELECT COUNT(*) FROM records INDEXED BY active_upload_jobs WHERE namespace='jobs' AND json_extract(body,'$.state') NOT IN ('completed','failed','canceled')",(),'active_upload_jobs')]
        for query,args,index in queries:
            plan=' '.join(str(tuple(row)) for row in self.f.service.state.connection.execute('EXPLAIN QUERY PLAN '+query,args))
            self.assertIn(index,plan)
