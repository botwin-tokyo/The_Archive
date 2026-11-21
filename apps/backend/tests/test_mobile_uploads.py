import hashlib
import io
import json
import threading
import unittest
import uuid
from unittest.mock import patch
from cryptography.fernet import Fernet
from archive_backend.contracts import BackendError,CHUNK_BYTES
from archive_backend.mobile_boundary import MobileBoundary
from support import Fixture


class MobileUploadTests(unittest.TestCase):
    def setUp(self):
        self.f=Fixture(); self.boundary=MobileBoundary(self.f.service,{'publicOrigin':'https://fixture.invalid','replayKeyFile':'unused'},replay_key=Fernet.generate_key())
        self.boundary.auth.enroll(self.f.owner,'test','synthetic upload password',['read','write','trash'],[{'driveId':'files','path':''}])
        session=self.boundary.auth.login({'username':'test','password':'synthetic upload password','deviceLabel':'test','requestId':str(uuid.uuid4())})
        self.context=self.boundary.auth.authenticate(session['accessToken']); self.session=session
        self.transfer=self.boundary.transfers
    def tearDown(self): self.boundary.close(); self.f.close()
    def args(self,data=b'opaque bytes',**changes):
        args={'requestId':str(uuid.uuid4()),'destination':self.boundary.profile(self.context)['roots'][0]['ref'],'name':'uploaded.bin','size':str(len(data)),'sha256':hashlib.sha256(data).hexdigest(),'collisionPolicy':'ask'}
        args.update(changes); return args
    def start(self,data=b'opaque bytes',**changes): return self.transfer.initiate_upload(self.context,self.args(data,**changes))
    def write(self,admission,data,offset=0,length=None):
        return self.transfer.write_upload(admission['transferToken'],admission['transferId'],offset,len(data) if length is None else length,io.BytesIO(data))
    def test_large_single_body_checkpointed_and_byte_equal(self):
        data=b'opaque\0\xff'*(CHUNK_BYTES//3); args=self.args(data); admission=self.transfer.initiate_upload(self.context,args)
        self.assertTrue(len(data)>CHUNK_BYTES)
        class BoundedReader(io.BytesIO):
            def read(self,size=-1):
                if size<0 or size>CHUNK_BYTES: raise AssertionError('Unbounded body read')
                return super().read(size)
        ack=self.transfer.write_upload(admission['transferToken'],admission['transferId'],0,len(data),BoundedReader(data))
        self.assertEqual(ack['durableOffset'],str(len(data)))
        queried=self.transfer.upload_snapshot(self.context,admission['transferId']); self.assertIsNone(queried['transferToken']); self.assertIsNone(queried['outcome'])
        done=self.transfer.finalize_upload(self.context,admission['transferId'])
        self.assertEqual(done['outcome'],'published'); self.assertEqual(done['verification']['digest'],args['sha256'])
        self.assertEqual((self.f.root/'files/uploaded.bin').read_bytes(),data)
        self.assertEqual(done,self.transfer.finalize_upload(self.context,admission['transferId']))
        repeated=self.transfer.initiate_upload(self.context,args); self.assertEqual(repeated['transferId'],admission['transferId']); self.assertEqual(repeated['outcome'],'published')
    def test_zero_byte_finalize_and_query(self):
        admission=self.start(b''); done=self.transfer.finalize_upload(self.context,admission['transferId'])
        self.assertEqual(done['durableOffset'],'0'); self.assertEqual(done['outcome'],'published')
    def test_interrupted_body_retains_non_aligned_prefix_then_resumes(self):
        data=b'a'*(CHUNK_BYTES+777); admission=self.start(data); prefix=data[:12345]
        with self.assertRaises(BackendError): self.write(admission,prefix,length=len(data))
        snapshot=self.transfer.upload_snapshot(self.context,admission['transferId']); self.assertEqual(snapshot['durableOffset'],str(len(prefix)))
        authority=self.transfer.upload_snapshot(self.context,admission['transferId'],mint=True)
        self.write(authority,data[len(prefix):],offset=len(prefix))
        self.assertEqual(self.transfer.finalize_upload(self.context,admission['transferId'])['outcome'],'published')
    def test_duplicate_body_and_changed_payload_cannot_duplicate_publication(self):
        data=b'abc'; args=self.args(data); admission=self.transfer.initiate_upload(self.context,args); self.write(admission,data)
        with self.assertRaises(BackendError): self.write(admission,data)
        with self.assertRaises(BackendError): self.transfer.initiate_upload(self.context,dict(args,name='changed'))
        self.assertEqual(self.transfer.finalize_upload(self.context,admission['transferId'])['outcome'],'published')
    def test_corrupt_hash_never_publishes(self):
        admission=self.start(b'right'); self.write(admission,b'wrong')
        with self.assertRaises(BackendError) as error: self.transfer.finalize_upload(self.context,admission['transferId'])
        self.assertEqual(error.exception.code,'INTEGRITY_FAILED'); self.assertFalse((self.f.root/'files/uploaded.bin').exists())
        snapshot=self.transfer.upload_snapshot(self.context,admission['transferId']); self.assertEqual(snapshot['error']['code'],'integrity_failed'); self.assertNotIn('retry',snapshot['controls'])
    def test_skip_is_not_published_and_old_file_remains(self):
        self.f.put('uploaded.bin',b'old'); admission=self.start(b'new',collisionPolicy='skip'); self.write(admission,b'new')
        done=self.transfer.finalize_upload(self.context,admission['transferId'])
        self.assertEqual(done['outcome'],'skipped'); self.assertIsNone(done['entry']); self.assertEqual((self.f.root/'files/uploaded.bin').read_bytes(),b'old')
        self.assertTrue(self.f.state.get('mobile_uploads',admission['transferId'])['released'])
    def test_replace_changed_target_preserved(self):
        self.f.put('uploaded.bin',b'old'); admission=self.start(b'new',collisionPolicy='replace'); self.write(admission,b'new')
        (self.f.root/'files/uploaded.bin').write_bytes(b'external replacement')
        with self.assertRaises(BackendError): self.transfer.finalize_upload(self.context,admission['transferId'])
        self.assertEqual((self.f.root/'files/uploaded.bin').read_bytes(),b'external replacement')
    def test_sibling_publication_does_not_invalidate_folder_identity(self):
        destination=self.boundary.profile(self.context)['roots'][0]['ref']
        a=self.start(b'a',name='a',destination=destination); self.write(a,b'a'); self.transfer.finalize_upload(self.context,a['transferId'])
        b=self.start(b'b',name='b',destination=destination); self.write(b,b'b'); self.transfer.finalize_upload(self.context,b['transferId'])
        self.assertEqual((self.f.root/'files/b').read_bytes(),b'b')
    def test_transfer_authority_scope_expiry_and_device_isolation(self):
        a=self.start(b'abc'); b=self.start(b'x',name='b')
        with self.assertRaises(BackendError): self.transfer.write_upload(a['transferToken'],b['transferId'],0,1,io.BytesIO(b'x'))
        with self.assertRaises(BackendError): self.write(a,b'oversize')
        with self.assertRaises(BackendError): self.boundary.auth.authenticate(a['transferToken'])
        self.boundary.auth.logout(self.session['accessToken'])
        with self.assertRaises(BackendError): self.write(a,b'abc')
    def test_pause_resume_cancel_and_finalize_race(self):
        admission=self.start(b'abc'); tid=admission['transferId']
        paused=self.transfer.control_upload(self.context,tid,'pause'); self.assertEqual(paused['state'],'paused')
        with self.assertRaises(BackendError): self.write(admission,b'abc')
        self.transfer.control_upload(self.context,tid,'resume'); self.write(admission,b'abc')
        done=self.transfer.finalize_upload(self.context,tid)
        self.assertEqual(self.transfer.control_upload(self.context,tid,'cancel')['outcome'],'published')
        other=self.start(b'abc',name='other'); canceled=self.transfer.control_upload(self.context,other['transferId'],'cancel')
        self.assertEqual(canceled['outcome'],'canceled')
        self.assertEqual(canceled['cleanupState'],'confirmed')
    def test_concurrent_writer_denied_and_active_cancel_is_boundary_safe(self):
        admission=self.start(b'abcdef'); tid=admission['transferId']; entered=threading.Event(); release=threading.Event(); errors=[]
        class Reader(io.BytesIO):
            def read(self,size=-1): entered.set(); release.wait(5); return super().read(size)
        def write():
            try: self.transfer.write_upload(admission['transferToken'],tid,0,6,Reader(b'abcdef'))
            except BackendError as e: errors.append(e.code)
        thread=threading.Thread(target=write); thread.start(); self.assertTrue(entered.wait(2))
        try:
            with self.assertRaises(BackendError): self.write(admission,b'abcdef')
            result=self.transfer.control_upload(self.context,tid,'cancel'); self.assertEqual(result['state'],'canceling')
        finally: release.set(); thread.join(5)
        self.assertFalse(thread.is_alive()); self.assertTrue(errors)
        self.assertEqual(self.transfer.upload_snapshot(self.context,tid)['outcome'],'canceled')
        self.assertEqual(self.transfer.upload_snapshot(self.context,tid)['cleanupState'],'confirmed')
        self.assertFalse((self.f.root/'files/uploaded.bin').exists())
    def test_stage_budget_and_admission_rollback(self):
        self.boundary.limits['stagingBudgetBytes']='2'
        with self.assertRaises(BackendError): self.start(b'123')
        self.assertEqual(self.f.state.list('transfers'),[]); self.assertEqual(self.f.state.list('mobile_uploads'),[])
    def test_snapshot_fields_match_native_schema(self):
        from pathlib import Path
        schema=json.loads((Path(__file__).resolve().parents[3]/'contracts/mobile/mobile-api.schema.json').read_text())
        admission=self.start()
        self.assertEqual(set(admission),set(schema['$defs']['UploadAdmission']['required']))

    def test_lost_finalize_publication_reconciles_same_file(self):
        admission=self.start(b'abc'); tid=admission['transferId']; self.write(admission,b'abc')
        def fault(point):
            if point=='before-transfer-terminal': raise RuntimeError('synthetic lost completion')
        self.f.service.transfers.fault=fault
        with self.assertRaises(RuntimeError): self.transfer.finalize_upload(self.context,tid)
        inode=(self.f.root/'files/uploaded.bin').stat().st_ino
        self.f.service.transfers.fault=lambda _:None
        result=self.transfer.finalize_upload(self.context,tid)
        self.assertEqual(result['outcome'],'published'); self.assertEqual((self.f.root/'files/uploaded.bin').stat().st_ino,inode)
        self.assertTrue(self.f.state.get('mobile_uploads',tid)['released'])

    def test_cancel_reconciles_lost_publication_instead_of_claiming_canceled(self):
        admission=self.start(b'abc'); tid=admission['transferId']; self.write(admission,b'abc')
        def fault(point):
            if point=='before-transfer-terminal': raise RuntimeError('synthetic lost completion')
        self.f.service.transfers.fault=fault
        with self.assertRaises(RuntimeError): self.transfer.finalize_upload(self.context,tid)
        inode=(self.f.root/'files/uploaded.bin').stat().st_ino
        self.f.service.transfers.fault=lambda _:None
        result=self.transfer.control_upload(self.context,tid,'cancel')
        self.assertEqual(result['outcome'],'published'); self.assertEqual((self.f.root/'files/uploaded.bin').stat().st_ino,inode)
    def test_cancel_ambiguous_publication_never_reports_canceled(self):
        admission=self.start(b'abc'); tid=admission['transferId']; self.write(admission,b'abc')
        self.f.state.put('intents','transfer:'+tid,{'kind':'publish','drive':'files','destination':'unknown','token':tid,'device':0,'inode':0})
        with self.assertRaises(BackendError) as error: self.transfer.control_upload(self.context,tid,'cancel')
        self.assertEqual(error.exception.code,'STATE_UNAVAILABLE'); self.assertEqual(self.f.state.get('transfers',tid)['state'],'waiting-for-client')
        self.assertFalse(self.f.state.get('mobile_uploads',tid)['released'])
    def test_cancel_after_ask_collision_preserves_target_and_confirms_cleanup(self):
        self.f.put('uploaded.bin',b'original'); admission=self.start(b'new'); self.write(admission,b'new')
        with self.assertRaises(BackendError): self.transfer.finalize_upload(self.context,admission['transferId'])
        self.assertIsNone(self.f.state.get('intents','transfer:'+admission['transferId']))
        result=self.transfer.control_upload(self.context,admission['transferId'],'cancel')
        self.assertEqual(result['outcome'],'canceled'); self.assertEqual(result['cleanupState'],'confirmed')
        self.assertEqual((self.f.root/'files/uploaded.bin').read_bytes(),b'original')
    def test_expected_collision_changed_target_rejects_before_admission(self):
        entry=self.f.put('uploaded.bin',b'original'); expected=self.boundary.metadata.ref(entry)
        args=self.args(b'new',collisionPolicy='replace',expectedCollision=expected)
        (self.f.root/'files/uploaded.bin').write_bytes(b'changed')
        with self.assertRaises(BackendError) as error: self.transfer.initiate_upload(self.context,args)
        self.assertEqual(error.exception.code,'CONFLICT')
        self.assertIsNone(self.f.state.get('mobile_upload_requests',self.context.principal.id+':'+args['requestId']))
        self.assertEqual((self.f.root/'files/uploaded.bin').read_bytes(),b'changed')
    def test_expected_absence_rejects_new_collision_and_exact_replay_stays_bound(self):
        args=self.args(b'new',collisionPolicy='replace',expectedCollision=None)
        self.f.put('uploaded.bin',b'unreviewed')
        with self.assertRaises(BackendError): self.transfer.initiate_upload(self.context,args)
        fresh=self.boundary.metadata.ref(self.boundary.metadata.current(self.context,'files','uploaded.bin'))
        args=dict(args,expectedCollision=fresh)
        admission=self.transfer.initiate_upload(self.context,args); self.write(admission,b'new')
        self.assertEqual(self.transfer.finalize_upload(self.context,admission['transferId'])['outcome'],'published')
        self.assertEqual(self.transfer.initiate_upload(self.context,args)['outcome'],'published')
        with self.assertRaises(BackendError): self.transfer.initiate_upload(self.context,dict(args,expectedCollision=None))

    def test_revoked_write_scope_stops_at_next_buffer(self):
        data=b'a'*(CHUNK_BYTES+1); admission=self.start(data); owner=self
        class Reader(io.BytesIO):
            count=0
            def read(self,size=-1):
                self.count+=1
                if self.count==2: owner.boundary.auth.assign(owner.f.owner,['read'],[{'driveId':'files','path':''}])
                return super().read(size)
        with self.assertRaises(BackendError): self.transfer.write_upload(admission['transferToken'],admission['transferId'],0,len(data),Reader(data))
        self.assertEqual(self.f.state.get('transfers',admission['transferId'])['offset'],str(CHUNK_BYTES))
        self.assertFalse((self.f.root/'files/uploaded.bin').exists())

    def test_large_decimal_admission_without_allocating_large_memory(self):
        volume=self.f.service.filesystems['files'].volume
        with patch.object(volume,'capacity',return_value={'total':2**40,'available':2**40}):
            admission=self.start(b'',size=str(2**32+1))
        self.assertEqual(admission['acceptedSize'],'4294967297'); self.assertEqual(admission['durableOffset'],'0')

    def test_transfer_token_expiry_requires_explicit_renewal(self):
        admission=self.start(b'abc'); original=self.boundary.auth.clock
        self.boundary.auth.clock=lambda:original()+86401
        with self.assertRaises(BackendError) as error: self.write(admission,b'abc')
        self.assertEqual(error.exception.code,'AUTH_EXPIRED')
        self.boundary.auth.clock=original

    def test_persisted_cancel_intent_cannot_be_bypassed_by_finalize(self):
        admission=self.start(b'abc'); tid=admission['transferId']; self.write(admission,b'abc')
        record=self.f.state.get('mobile_uploads',tid); record['desired']='cancel'; self.f.state.put('mobile_uploads',tid,record)
        self.assertEqual(self.transfer.upload_snapshot(self.context,tid)['state'],'canceling')
        self.assertIsNone(self.transfer.upload_snapshot(self.context,tid,mint=True)['transferToken'])
        with self.assertRaises(BackendError): self.transfer.finalize_upload(self.context,tid)
        self.assertFalse((self.f.root/'files/uploaded.bin').exists())
        self.assertEqual(self.transfer.upload_snapshot(self.context,tid)['outcome'],'canceled')

    def test_full_disk_can_retry_explicitly_after_space_is_freed(self):
        admission=self.start(b'abc'); tid=admission['transferId']; volume=self.f.service.filesystems['files'].volume
        with patch.object(volume,'capacity',return_value={'total':10,'available':0}):
            with self.assertRaises(BackendError): self.write(admission,b'abc')
        failed=self.transfer.upload_snapshot(self.context,tid)
        self.assertEqual(failed['state'],'failed'); self.assertFalse(failed['error']['retryable']); self.assertIn('retry',failed['controls'])
        self.transfer.control_upload(self.context,tid,'retry'); self.write(admission,b'abc')
        self.assertEqual(self.transfer.finalize_upload(self.context,tid)['outcome'],'published')

    def test_cancel_cleanup_failure_is_visible_and_retryable_by_cancel(self):
        admission=self.start(b'abc'); tid=admission['transferId']; self.write(admission,b'abc')
        fs=self.f.service.filesystems['files']
        with patch.object(fs,'discard_staging',side_effect=BackendError('DRIVE_UNAVAILABLE','Synthetic cleanup failure')):
            canceled=self.transfer.control_upload(self.context,tid,'cancel')
        self.assertEqual(canceled['outcome'],'canceled'); self.assertEqual(canceled['cleanupState'],'failed')
        self.assertEqual(canceled['error']['code'],'cleanup_pending'); self.assertFalse(self.f.state.get('mobile_uploads',tid)['released'])
        cleaned=self.transfer.control_upload(self.context,tid,'cancel')
        self.assertEqual(cleaned['cleanupState'],'confirmed'); self.assertTrue(self.f.state.get('mobile_uploads',tid)['released'])
        with self.assertRaises(BackendError): fs.internal_stat('staging',tid)


if __name__=='__main__': unittest.main()
