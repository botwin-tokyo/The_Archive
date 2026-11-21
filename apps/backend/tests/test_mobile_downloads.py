import hashlib
import json
import os
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from cryptography.fernet import Fernet
from archive_backend.contracts import BackendError,CHUNK_BYTES
from archive_backend.mobile_boundary import MobileBoundary
from archive_backend.mobile_downloads import byte_range
from archive_backend.mobile_spool import SnapshotSpool
from support import Fixture


class MobileDownloadTests(unittest.TestCase):
    def setUp(self):
        self.f=Fixture(); self.boundary=MobileBoundary(self.f.service,{'publicOrigin':'https://fixture.invalid','replayKeyFile':'unused'},replay_key=Fernet.generate_key())
        self.boundary.auth.enroll(self.f.owner,'test','synthetic download password',['read','write','trash'],[{'driveId':'files','path':''}])
        session=self.boundary.auth.login({'username':'test','password':'synthetic download password','deviceLabel':'test','requestId':str(uuid.uuid4())})
        self.context=self.boundary.auth.authenticate(session['accessToken']); self.session=session
        self.downloads=self.boundary.transfers.downloads
    def tearDown(self): self.boundary.close(); self.f.close()
    def args(self,data=b'0123456789',name='source.bin'):
        source=self.f.put(name,data)
        return {'requestId':str(uuid.uuid4()),'source':self.boundary.metadata.ref(source)}
    def ready(self,args=None,data=b'0123456789'):
        args=args or self.args(data)
        first=self.downloads.initiate(self.context,args); rid=first['representationId']
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            value=self.downloads.snapshot(self.context,rid)
            if value['state']=='ready': return self.downloads.snapshot(self.context,rid,mint=True)
            time.sleep(.01)
        self.fail('Synthetic snapshot did not complete')
    def read(self,rep,range_header=None,if_range=None,method='GET'):
        with self.downloads.content(rep['transferToken'],rep['representationId'],range_header,if_range,method) as (status,length,headers,chunks):
            return status,length,headers,b''.join(chunks) if method=='GET' else b''
    def test_whole_range_suffix_if_range_and_head(self):
        rep=self.ready(); status,length,headers,data=self.read(rep)
        self.assertEqual((status,length,data),(200,10,b'0123456789')); self.assertEqual(headers['ETag'],rep['strongETag'])
        self.assertEqual(self.read(rep,'bytes=3-6',rep['strongETag'])[:2],(206,4))
        self.assertEqual(self.read(rep,'bytes=3-6',rep['strongETag'])[3],b'3456')
        self.assertEqual(self.read(rep,'bytes=-3')[3],b'789')
        self.assertEqual(self.read(rep,'bytes=7-')[3],b'789')
        self.assertEqual(self.read(rep,'bytes=3-','"different"')[0],200)
        self.assertEqual(self.read(rep,'bytes=3-6',method='HEAD')[:2],(200,10))
        status,length,headers,data=self.read(rep,'bytes=100-')
        self.assertEqual((status,length,data),(416,0,b'')); self.assertEqual(headers['Content-Range'],'bytes */10')
    def test_snapshot_is_immutable_after_original_changes(self):
        rep=self.ready(); (self.f.root/'files/source.bin').write_bytes(b'CHANGED')
        self.assertEqual(self.read(rep)[3],b'0123456789')
        self.assertEqual(rep['sha256'],hashlib.sha256(b'0123456789').hexdigest())
    def test_capture_changed_source_fails_not_misleading_validator(self):
        args=self.args(b'a'*(CHUNK_BYTES+1)); original=self.downloads.spool.ensure_space; calls=[]
        def mutate(size):
            calls.append(size)
            if len(calls)==2: (self.f.root/'files/source.bin').write_bytes(b'b'*(CHUNK_BYTES+1))
            return original(size)
        with patch.object(self.downloads.spool,'ensure_space',side_effect=mutate):
            first=self.downloads.initiate(self.context,args); rid=first['representationId']
            deadline=time.monotonic()+5
            while self.f.state.get('mobile_downloads',rid)['state']=='preparing' and time.monotonic()<deadline: time.sleep(.01)
        with self.assertRaises(BackendError): self.downloads.snapshot(self.context,rid)
        self.assertNotEqual(self.f.state.get('mobile_downloads',rid)['state'],'ready')
    def test_corrupt_snapshot_rejected_and_never_rebound(self):
        rep=self.ready(); rid=rep['representationId']
        path=self.f.root/self.downloads.spool.directory_name/(rid+'.blob'); path.chmod(0o600); path.write_bytes(b'bad')
        with self.assertRaises(BackendError) as error: self.read(rep)
        self.assertEqual(error.exception.code,'INTEGRITY_FAILED')
    def test_complete_is_idempotent_accounting_after_local_claim(self):
        rep=self.ready(); args={'requestId':str(uuid.uuid4()),'bytes':rep['bytes'],'sha256':rep['sha256']}
        receipt=self.downloads.complete(self.context,rep['representationId'],args)
        self.assertTrue(receipt['acknowledged']); self.assertEqual(receipt,self.downloads.complete(self.context,rep['representationId'],args))
        with self.assertRaises(BackendError): self.downloads.complete(self.context,rep['representationId'],dict(args,sha256='0'*64))
    def test_bad_completion_does_not_complete(self):
        rep=self.ready()
        with self.assertRaises(BackendError): self.downloads.complete(self.context,rep['representationId'],{'requestId':str(uuid.uuid4()),'bytes':'10','sha256':'0'*64})
        self.assertIsNone(self.f.state.get('mobile_downloads',rep['representationId'])['complete'])
    def test_cancel_while_reading_retains_file_until_reader_exit(self):
        rep=self.ready(data=b'a'*(CHUNK_BYTES+1)); rid=rep['representationId']; path=self.f.root/self.downloads.spool.directory_name/(rid+'.blob')
        with self.assertRaises(BackendError):
            with self.downloads.content(rep['transferToken'],rid) as (_,_,_,chunks):
                next(chunks); result=self.downloads.cancel(self.context,rid)
                self.assertEqual(result['cleanupState'],'pending'); self.assertTrue(path.exists())
                next(chunks)
        self.assertFalse(path.exists()); self.assertEqual(self.downloads.snapshot(self.context,rid)['cleanupState'],'confirmed')
    def test_cancel_cleanup_failure_and_explicit_retry(self):
        rep=self.ready(); rid=rep['representationId']
        with patch.object(self.downloads.spool,'remove',side_effect=OSError('synthetic cleanup')):
            canceled=self.downloads.cancel(self.context,rid)
        self.assertEqual(canceled['cleanupState'],'failed'); self.assertFalse(self.f.state.get('mobile_downloads',rid)['released'])
        self.assertEqual(self.downloads.cancel(self.context,rid)['cleanupState'],'confirmed')
    def test_expiry_authorization_and_generation_bound(self):
        rep=self.ready(); rid=rep['representationId']
        record=self.f.state.get('mobile_downloads',rid); record['expires']=0; self.f.state.put('mobile_downloads',rid,record)
        with self.assertRaises(BackendError) as error: self.downloads.snapshot(self.context,rid)
        self.assertEqual(error.exception.code,'REPRESENTATION_EXPIRED')
        self.assertEqual(self.downloads.cancel(self.context,rid)['cleanupState'],'confirmed')
        other=self.ready(self.args(b'other',name='other.bin')); self.boundary.auth.logout(self.session['accessToken'])
        with self.assertRaises(BackendError): self.read(other)
    def test_snapshot_read_only_root_uses_private_state_spool(self):
        args=self.args(); self.boundary.auth.assign(self.f.owner,['read'],[{'driveId':'files','path':''}])
        with patch.object(self.f.service.filesystems['files'],'create_staging',side_effect=AssertionError('Must not write a read-only source volume')):
            rep=self.ready(args)
        self.assertEqual(self.read(rep)[3],b'0123456789')
        self.assertTrue((self.f.root/self.downloads.spool.directory_name/(rep['representationId']+'.blob')).exists())
    def test_admission_idempotent_and_get_does_not_mint(self):
        args=self.args(); rep=self.ready(args); again=self.downloads.initiate(self.context,args)
        self.assertEqual(again['representationId'],rep['representationId']); self.assertIsNone(again['transferToken'])
        queried=self.downloads.snapshot(self.context,rep['representationId']); self.assertIsNone(queried['transferToken']); self.assertIsNone(queried['transferExpiresAt'])
        self.assertLessEqual(rep['transferExpiresAt'],rep['expiresAt'])
    def test_native_shapes_match_schema(self):
        schema=json.loads((Path(__file__).resolve().parents[3]/'contracts/mobile/mobile-api.schema.json').read_text())
        rep=self.ready(); self.assertEqual(set(rep),set(schema['$defs']['DownloadRepresentation']['required']))
        canceled=self.downloads.cancel(self.context,rep['representationId']); self.assertEqual(set(canceled),set(schema['$defs']['DownloadCanceled']['required']))
    def test_empty_file_standard_read(self):
        rep=self.ready(data=b''); self.assertEqual(self.read(rep)[3],b''); self.assertEqual(self.read(rep,'bytes=0-')[0],416)
    def test_manifest_includes_empty_root_and_nested_empty_directory(self):
        fs=self.f.service.filesystems['files']; fs.create_dir('folder'); fs.create_dir('folder/empty'); self.f.put('folder/file',b'abc')
        raw=self.boundary.metadata.current(self.context,'files','folder'); args={'requestId':str(uuid.uuid4()),'source':self.boundary.metadata.ref(raw)}
        manifest=self.downloads.manifest(self.context,args)
        self.assertEqual(manifest['entries'][0]['relativePath'],''); self.assertEqual(manifest['entries'][0]['kind'],'directory')
        self.assertEqual(manifest['fileCount'],1); self.assertEqual(manifest['directoryCount'],2); self.assertEqual(manifest['totalBytes'],'3')
        (self.f.root/'files/folder/file').write_bytes(b'changed')
        self.assertEqual(self.downloads.manifest(self.context,args),manifest)
    def test_manifest_rejects_symlinks_and_over_limit(self):
        fs=self.f.service.filesystems['files']; fs.create_dir('folder'); os.symlink('missing',self.f.root/'files/folder/link')
        raw=self.boundary.metadata.current(self.context,'files','folder')
        with self.assertRaises(BackendError): self.downloads.manifest(self.context,{'requestId':str(uuid.uuid4()),'source':self.boundary.metadata.ref(raw)})
        (self.f.root/'files/folder/link').unlink()
        for i in range(1000): (self.f.root/f'files/folder/f{i}').write_bytes(b'')
        raw=self.boundary.metadata.current(self.context,'files','folder')
        with self.assertRaises(BackendError) as error: self.downloads.manifest(self.context,{'requestId':str(uuid.uuid4()),'source':self.boundary.metadata.ref(raw)})
        self.assertEqual(error.exception.code,'SCAN_LIMIT')

    def test_authorization_narrows_between_content_buffers(self):
        rep=self.ready(data=b'a'*(CHUNK_BYTES+1))
        with self.assertRaises(BackendError):
            with self.downloads.content(rep['transferToken'],rep['representationId']) as (_,_,_,chunks):
                next(chunks)
                self.boundary.auth.assign(self.f.owner,['read'],[{'driveId':'backups','path':''}])
                next(chunks)

    def test_source_generation_change_rejects_existing_representation(self):
        rep=self.ready(); volume=self.f.service.filesystems['files'].volume
        volume._generation+=1
        with self.assertRaises(BackendError): self.read(rep)

    def test_snapshot_write_failure_cleans_partial(self):
        args=self.args()
        with patch('archive_backend.mobile_downloads.write_snapshot',return_value=0):
            first=self.downloads.initiate(self.context,args); rid=first['representationId']
            deadline=time.monotonic()+5
            while self.f.state.get('mobile_downloads',rid)['state']=='preparing' and time.monotonic()<deadline: time.sleep(.01)
        with self.assertRaises(BackendError): self.downloads.snapshot(self.context,rid)
        record=self.f.state.get('mobile_downloads',rid)
        self.assertTrue(record['released']); self.assertEqual(record['cleanupState'],'confirmed')

    def test_two_state_files_in_one_parent_own_disjoint_spools(self):
        other=SnapshotSpool(str(self.f.root/'another-state.sqlite'),0)
        try:
            self.assertNotEqual(other.directory_name,self.downloads.spool.directory_name)
            rid='a'*64
            for spool,data in ((self.downloads.spool,b'one'),(other,b'two')):
                fd=spool.create(rid)
                try: os.write(fd,data); spool.promote(rid,fd)
                finally: os.close(fd)
            self.downloads.spool.remove(rid)
            self.assertEqual((self.f.root/other.directory_name/(rid+'.blob')).read_bytes(),b'two')
        finally: other.close()

    def test_spool_marker_mismatch_fails_closed(self):
        marker=self.f.root/self.downloads.spool.directory_name/'.owner.json'
        marker.write_text(json.dumps({'schemaVersion':1,'stateNameSha256':'wrong'}))
        with self.assertRaises(BackendError): SnapshotSpool(self.f.state.path,0)

    def test_manifest_replay_checks_captured_volume_generation(self):
        fs=self.f.service.filesystems['files']; fs.create_dir('empty')
        source=self.boundary.metadata.ref(self.boundary.metadata.current(self.context,'files','empty'))
        args={'requestId':str(uuid.uuid4()),'source':source}; manifest=self.downloads.manifest(self.context,args)
        self.assertEqual(len(manifest['entries']),1)
        fs.volume._generation+=1
        with self.assertRaises(BackendError) as error: self.downloads.manifest(self.context,args)
        self.assertEqual(error.exception.code,'VOLUME_CHANGED')


if __name__=='__main__': unittest.main()
