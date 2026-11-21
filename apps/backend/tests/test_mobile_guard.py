import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid
from cryptography.fernet import Fernet
from archive_backend.contracts import BackendError
from archive_backend.mobile_auth import MobileAuth
from archive_backend.mobile_guard import recover_mobile_identity
from archive_backend.persistence import State,_SCHEMA,_MIGRATIONS
from archive_backend.security import Auth


class MobileGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.path=Path(self.temp.name)/'state.sqlite'
        self.state=State(self.path); self.auth=Auth(self.state)
        self.owner_token='test-only-owner-' + 'x' * 32; self.owner=self.auth.bootstrap_owner(self.owner_token)
        self.key=Fernet.generate_key(); self.mobile=MobileAuth(self.state,self.auth,self.key)
        self.mobile.enroll(self.owner,'test','synthetic guard password',['read'],[{'driveId':'files','path':''}])
    def tearDown(self): self.state.close(); self.temp.cleanup()
    def login(self): return self.mobile.login({'username':'test','password':'synthetic guard password','deviceLabel':'test','requestId':str(uuid.uuid4())})
    def test_schema_one_checksum_preserved_and_old_binary_refuses_v2(self):
        self.assertEqual(_MIGRATIONS[0],(1,_SCHEMA))
        stored=self.state.connection.execute('SELECT checksum FROM migrations WHERE version=1').fetchone()[0]
        self.assertEqual(stored,hashlib.sha256(_SCHEMA.encode()).hexdigest())
        with patch.object(State,'schema_version',1):
            with self.assertRaises(BackendError) as error: State(self.path)
        self.assertEqual(error.exception.code,'UNSUPPORTED')
    def test_critical_updates_advance_but_progress_does_not(self):
        before=self.state.get('mobile_guard','current')['sequence']
        self.state.put('transfers','fixture-progress',{'offset':'7'})
        self.assertEqual(self.state.get('mobile_guard','current')['sequence'],before)
        self.login()
        self.assertGreater(self.state.get('mobile_guard','current')['sequence'],before)
        self.state.verify_guard()
    def test_db_only_rollback_fails_before_unknown_request_replay(self):
        backup=Path(self.temp.name)/'backup.sqlite'; self.state.backup(backup)
        self.state.put('mobile_operations','synthetic-request',{'admitted':True})
        self.state.close()
        for suffix in ('-wal','-shm'):
            path=Path(str(self.path)+suffix)
            if path.exists(): path.unlink()
        shutil.copyfile(backup,self.path); self.path.chmod(0o600)
        with self.assertRaises(BackendError) as error: State(self.path)
        self.assertEqual(error.exception.code,'STATE_UNAVAILABLE')
        self.state=State(self.path,mobile_recovery=True)
    def test_missing_marker_fails_closed(self):
        guard=self.state._receipt_guard
        os.unlink(guard.name,dir_fd=guard.fd)
        with self.assertRaises(BackendError): self.state.verify_guard()
        with self.assertRaises(BackendError): self.state.get('mobile_operations','unknown')
    def test_failure_after_independent_advance_is_blocked_and_owner_recoverable(self):
        session=self.login(); previous=self.mobile.server_id; guard=self.state._receipt_guard; original=guard._write
        def fail_after_marker(value): original(value); raise RuntimeError('synthetic crash after watermark')
        with patch.object(guard,'_write',side_effect=fail_after_marker):
            with self.assertRaises(RuntimeError): self.state.put('mobile_operations','uncertain',{'value':1})
        with self.assertRaises(BackendError): self.state.get('mobile_operations','uncertain')
        self.state.close(); self.state=State(self.path,mobile_recovery=True); self.state.acquire_service_lock(); self.auth=Auth(self.state)
        owner=self.auth.authenticate(self.owner_token)
        result=recover_mobile_identity(self.state,self.auth,owner,'new synthetic recovered password',['read'],[{'driveId':'files','path':''}],acknowledged=True)
        self.assertNotEqual(result['serverId'],previous); self.assertTrue(result['oldDevicesRevoked'])
        renewed=MobileAuth(self.state,self.auth,self.key)
        with self.assertRaises(BackendError): renewed.authenticate(session['accessToken'])
        self.assertIsNone(self.state.get('mobile_operations','uncertain')); self.state.verify_guard()
    def test_recovery_requires_owner_stopped_service_and_acknowledgment(self):
        with self.assertRaises(BackendError): recover_mobile_identity(self.state,self.auth,self.owner,'synthetic new password',['read'],[{'driveId':'files','path':''}],acknowledged=True)
        self.state.close(); self.state=State(self.path,mobile_recovery=True); self.auth=Auth(self.state); owner=self.auth.authenticate(self.owner_token)
        with self.assertRaises(BackendError): recover_mobile_identity(self.state,self.auth,owner,'synthetic new password',['read'],[{'driveId':'files','path':''}],acknowledged=True)
        self.state.acquire_service_lock()
        with self.assertRaises(BackendError): recover_mobile_identity(self.state,self.auth,owner,'synthetic new password',['read'],[{'driveId':'files','path':''}],acknowledged=False)
    def test_recovery_denies_surviving_legacy_worker_and_fences_dead_launch(self):
        from archive_backend.jobs import _process_stamp
        self.state.put('jobs','live-job',{'id':'live-job','state':'running','_fence':1,'_lease':{'pid':os.getpid(),'processStart':_process_stamp(os.getpid())}})
        self.state.close(); self.state=State(self.path,mobile_recovery=True); self.state.acquire_service_lock(); self.auth=Auth(self.state)
        owner=self.auth.authenticate(self.owner_token)
        with self.assertRaises(BackendError) as error:
            recover_mobile_identity(self.state,self.auth,owner,'new synthetic recovered password',['read'],[{'driveId':'files','path':''}],acknowledged=True)
        self.assertEqual(error.exception.code,'BUSY')
        with patch('archive_backend.jobs._owner_stopped',return_value=True):
            recover_mobile_identity(self.state,self.auth,owner,'new synthetic recovered password',['read'],[{'driveId':'files','path':''}],acknowledged=True)
        row=self.state.get('jobs','live-job'); self.assertIsNone(row['_lease']); self.assertEqual(row['_fence'],2)
        self.assertEqual(row['state'],'failed')
    def test_second_connection_tracks_committed_watermark(self):
        other=State(self.path)
        try:
            self.login(); other.verify_guard()
            other.put('mobile_operations','other-request',{'accepted':True})
            self.state.verify_guard()
        finally: other.close()
    def test_rollback_before_advance_leaves_existing_guard_valid(self):
        before=self.state.get('mobile_guard','current')
        with self.assertRaises(RuntimeError):
            with self.state.transaction():
                self.state.put('mobile_operations','rolled-back',{'value':1}); raise RuntimeError('before commit')
        self.assertEqual(self.state.get('mobile_guard','current'),before); self.state.verify_guard()


if __name__=='__main__': unittest.main()
