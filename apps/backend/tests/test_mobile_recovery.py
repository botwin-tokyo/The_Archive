import hashlib
import io
import json
import os
import time
import unittest
import uuid
from unittest.mock import patch
from cryptography.fernet import Fernet
from archive_backend.contracts import BackendError
from archive_backend.mobile_boundary import MobileBoundary
from archive_backend.mobile_contracts import error_status,mobile_error
from archive_backend.mobile_auth import token_digest
from support import Fixture


class MobileRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f=Fixture(); self.key=Fernet.generate_key(); self.config={'publicOrigin':'https://fixture.invalid','replayKeyFile':'unused'}
        self.boundary=MobileBoundary(self.f.service,self.config,replay_key=self.key)
        self.boundary.auth.enroll(self.f.owner,'test','synthetic recovery password',['read','write','trash'],[{'driveId':'files','path':''}])
        self.session=self.boundary.auth.login({'username':'test','password':'synthetic recovery password','deviceLabel':'test','requestId':str(uuid.uuid4())})
        self.context=self.boundary.auth.authenticate(self.session['accessToken'])
    def tearDown(self): self.boundary.close(); self.f.close()
    def upload(self):
        args={'requestId':str(uuid.uuid4()),'destination':self.boundary.profile(self.context)['roots'][0]['ref'],'name':'upload.bin','size':'3','sha256':hashlib.sha256(b'abc').hexdigest(),'collisionPolicy':'ask'}
        value=self.boundary.transfers.initiate_upload(self.context,args)
        self.boundary.transfers.write_upload(value['transferToken'],value['transferId'],0,3,io.BytesIO(b'abc'))
        return args,value
    def ready(self):
        source=self.f.put('download.bin',b'original')
        args={'requestId':str(uuid.uuid4()),'source':self.boundary.metadata.ref(source)}
        value=self.boundary.transfers.downloads.initiate(self.context,args); rid=value['representationId']
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            value=self.boundary.transfers.downloads.snapshot(self.context,rid)
            if value['state']=='ready': return args,value
            time.sleep(.01)
        self.fail('Snapshot did not prepare')
    def age_upload(self,rid):
        row=self.f.state.get('mobile_uploads',rid); row['updated']=0; self.f.state.put('mobile_uploads',rid,row)
    def test_expired_upload_truthful_tombstone_and_no_new_grant(self):
        args,value=self.upload(); rid=value['transferId']; self.age_upload(rid)
        self.boundary.transfers.maintenance.tick()
        result=self.boundary.transfers.upload_snapshot(self.context,rid,mint=True)
        self.assertEqual((result['state'],result['outcome'],result['controls'],result['transferToken']),('failed',None,[],None))
        self.assertEqual(result['error']['code'],'transfer_expired'); self.assertEqual(result['error']['recovery'],'choose_source'); self.assertFalse(result['error']['retryable'])
        self.assertEqual(result['cleanupState'],'confirmed'); self.assertFalse((self.f.root/'files/upload.bin').exists())
        repeated=self.boundary.transfers.initiate_upload(self.context,args)
        self.assertEqual(repeated['transferId'],rid); self.assertIsNone(repeated['transferToken'])
        self.assertEqual(result['error']['correlationId'],repeated['error']['correlationId'])
        with self.assertRaises(BackendError): self.boundary.transfers.finalize_upload(self.context,rid)
    def test_expiry_preserves_published_original(self):
        _,value=self.upload(); rid=value['transferId']; self.boundary.transfers.finalize_upload(self.context,rid)
        self.age_upload(rid); self.boundary.transfers.maintenance.tick()
        self.assertEqual((self.f.root/'files/upload.bin').read_bytes(),b'abc')
    def test_generation_change_prevents_staging_cleanup(self):
        _,value=self.upload(); rid=value['transferId']; self.age_upload(rid)
        fs=self.f.service.filesystems['files']; fs.volume._generation+=1
        self.boundary.transfers.maintenance.tick()
        record=self.f.state.get('mobile_uploads',rid); self.assertFalse(record['released']); self.assertEqual(record['cleanupState'],'failed')
    def test_ambiguous_publication_never_discarded_by_age(self):
        _,value=self.upload(); rid=value['transferId']; self.age_upload(rid)
        self.f.state.put('intents','transfer:'+rid,{'status':'intent'})
        self.boundary.transfers.maintenance.tick()
        record=self.f.state.get('mobile_uploads',rid); self.assertFalse(record['released']); self.assertEqual(record['lastError']['code'],'STATE_UNAVAILABLE')
    def test_revoked_session_generated_staging_reaped(self):
        _,value=self.upload(); self.boundary.auth.logout(self.session['accessToken'])
        self.boundary.transfers.maintenance.tick()
        self.assertTrue(self.f.state.get('mobile_uploads',value['transferId'])['released'])
    def test_ready_representation_survives_boundary_restart(self):
        args,value=self.ready(); rid=value['representationId']; self.boundary.close()
        (self.f.root/'files/download.bin').write_bytes(b'changed')
        self.boundary=MobileBoundary(self.f.service,self.config,replay_key=self.key)
        self.context=self.boundary.auth.authenticate(self.session['accessToken'])
        resumed=self.boundary.transfers.downloads.snapshot(self.context,rid,mint=True)
        self.assertEqual(resumed['sha256'],hashlib.sha256(b'original').hexdigest()); self.assertEqual(resumed['strongETag'],value['strongETag'])
        with self.boundary.transfers.downloads.content(resumed['transferToken'],rid) as (_,_,_,chunks): self.assertEqual(b''.join(chunks),b'original')
    def test_interrupted_prepare_fails_without_implicit_recapture(self):
        args,value=self.ready(); rid=value['representationId']; self.boundary.close()
        record=self.f.state.get('mobile_downloads',rid); record['state']='preparing'; self.f.state.put('mobile_downloads',rid,record)
        self.boundary=MobileBoundary(self.f.service,self.config,replay_key=self.key)
        self.context=self.boundary.auth.authenticate(self.session['accessToken'])
        with self.assertRaises(BackendError) as error: self.boundary.transfers.downloads.initiate(self.context,args)
        self.assertEqual(error.exception.code,'SNAPSHOT_INTERRUPTED'); self.assertEqual(error_status(error.exception),409)
        self.assertEqual(mobile_error(error.exception)['error']['recovery'],'restart_download')
        self.assertTrue(self.f.state.get('mobile_downloads',rid)['released'])
        self.assertEqual((self.f.root/'files/download.bin').read_bytes(),b'original')
    def test_expired_snapshot_releases_cache_not_source(self):
        _,value=self.ready(); rid=value['representationId']; row=self.f.state.get('mobile_downloads',rid); row['expires']=0; self.f.state.put('mobile_downloads',rid,row)
        self.boundary.transfers.maintenance.tick()
        self.assertTrue(self.f.state.get('mobile_downloads',rid)['released'])
        self.assertEqual((self.f.root/'files/download.bin').read_bytes(),b'original')
        with self.assertRaises(BackendError) as error: self.boundary.transfers.downloads.snapshot(self.context,rid)
        self.assertEqual(error.exception.code,'REPRESENTATION_EXPIRED')
    def test_maintenance_waits_for_reader_termination(self):
        _,value=self.ready(); rid=value['representationId']; rep=self.boundary.transfers.downloads.snapshot(self.context,rid,mint=True)
        with self.boundary.transfers.downloads.content(rep['transferToken'],rid) as (_,_,_,chunks):
            row=self.f.state.get('mobile_downloads',rid); row['expires']=0; self.f.state.put('mobile_downloads',rid,row)
            self.boundary.transfers.maintenance.tick(); self.assertFalse(self.f.state.get('mobile_downloads',rid)['released'])
        self.boundary.transfers.maintenance.tick(); self.assertTrue(self.f.state.get('mobile_downloads',rid)['released'])
    def test_unproven_writer_never_reaped_due_to_age(self):
        _,value=self.upload(); rid=value['transferId']; self.age_upload(rid)
        self.f.state.put('mobile_writers',rid,{'pid':os.getpid(),'processStart':'unknown','owner':'foreign'})
        with patch('archive_backend.mobile_recovery._owner_stopped',return_value=False): self.boundary.transfers.maintenance.tick()
        self.assertFalse(self.f.state.get('mobile_uploads',rid)['released'])
        with patch('archive_backend.mobile_recovery._owner_stopped',return_value=True): self.boundary.transfers.maintenance.tick()
        self.assertTrue(self.f.state.get('mobile_uploads',rid)['released'])
    def test_orphan_spool_grace_and_namespace(self):
        spool=self.boundary.transfers.downloads.spool; rid='f'*64
        fd=spool.create(rid); os.write(fd,b'generated'); os.close(fd)
        path=self.f.root/spool.directory_name/(rid+'.part')
        self.boundary.transfers.maintenance.tick(); self.assertTrue(path.exists())
        os.utime(path,(0,0)); self.boundary.transfers.maintenance.tick(); self.assertFalse(path.exists())
        self.assertTrue((self.f.root/spool.directory_name/'.owner.json').exists())
    def test_token_reaper_preserves_unexpired_refresh_reuse_verifier(self):
        old=self.session['refreshToken']; self.boundary.auth.refresh({'refreshToken':old,'requestId':str(uuid.uuid4())})
        self.f.state.put('mobile_transfer_tokens','expired',{'expires':0,'session':self.context.session_id})
        self.boundary.transfers.maintenance.tick()
        self.assertIsNone(self.f.state.get('mobile_transfer_tokens','expired'))
        self.assertTrue(self.f.state.get('mobile_tokens',token_digest(old))['used'])
    def test_malformed_rows_are_isolated_and_do_not_authorize_cleanup(self):
        _,value=self.upload(); rid=value['transferId']; self.age_upload(rid)
        with self.f.state.transaction() as c:
            for key,body in [('0'*64,'{broken'),('1'*64,'[]'),('2'*64,json.dumps({'id':'2'*64,'released':False}))]:
                c.execute("INSERT INTO records(namespace,key,body,updated) VALUES('mobile_uploads',?,?,?)",(key,body,time.time()))
        self.boundary.transfers.maintenance.tick()
        self.assertTrue(self.f.state.get('mobile_uploads',rid)['released'])
        with self.f.state.transaction() as c:
            self.assertEqual(c.execute("SELECT body FROM records WHERE namespace='mobile_uploads' AND key=?",('0'*64,)).fetchone()[0],'{broken')
        self.assertEqual(self.f.state.get('mobile_uploads','1'*64),[])
    def test_missing_job_or_transfer_never_infers_cleanup_ownership(self):
        _,value=self.upload(); rid=value['transferId']; self.age_upload(rid)
        jobid=value['serverJobId']; original=self.f.state.get('jobs',jobid); self.f.state.delete('jobs',jobid)
        self.boundary.transfers.maintenance.tick(); self.assertFalse(self.f.state.get('mobile_uploads',rid)['released'])
        self.f.state.put('jobs',jobid,original); self.f.state.delete('transfers',rid)
        self.boundary.transfers.maintenance.tick(); self.assertFalse(self.f.state.get('mobile_uploads',rid)['released'])
    def test_database_failure_is_not_treated_as_one_bad_record(self):
        with patch.object(self.f.state,'verify_guard',side_effect=BackendError('STATE_UNAVAILABLE','synthetic persistence failure')):
            with self.assertRaises(BackendError): self.boundary.transfers.maintenance.tick()
    def test_expired_old_refresh_still_revokes_live_extended_family(self):
        old=self.session['refreshToken']; self.boundary.auth.refresh({'refreshToken':old,'requestId':str(uuid.uuid4())})
        key=token_digest(old); row=self.f.state.get('mobile_tokens',key); row['expires']=0; self.f.state.put('mobile_tokens',key,row)
        self.boundary.transfers.maintenance.tick(); self.assertIsNotNone(self.f.state.get('mobile_tokens',key))
        with self.assertRaises(BackendError) as error: self.boundary.auth.refresh({'refreshToken':old,'requestId':str(uuid.uuid4())})
        self.assertEqual(error.exception.code,'PERMISSION_REVOKED')
    def test_request_capacity_rejects_new_without_erasing_prior_receipts(self):
        args,value=self.upload()
        with patch('archive_backend.mobile_limits.REQUEST_LIMIT',1):
            self.assertEqual(self.boundary.transfers.initiate_upload(self.context,args)['transferId'],value['transferId'])
            with self.assertRaises(BackendError) as error: self.boundary.transfers.initiate_upload(self.context,dict(args,requestId=str(uuid.uuid4())))
            self.assertEqual(error.exception.code,'QUOTA_EXCEEDED')


if __name__=='__main__': unittest.main()
