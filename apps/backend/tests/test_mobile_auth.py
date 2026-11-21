import json
import tempfile
import time
import unittest
import uuid
import concurrent.futures
from pathlib import Path
from cryptography.fernet import Fernet
from archive_backend.contracts import BackendError
from archive_backend.mobile_auth import MobileAuth, ACCESS_SECONDS, IDLE_SECONDS
from archive_backend.mobile_enrollment import load_replay_key
from support import Fixture


class MobileAuthTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture(); self.now = [time.time()]
        self.f.service.auth.clock = lambda: self.now[0]
        self.key = Fernet.generate_key()
        self.mobile = MobileAuth(self.f.state, self.f.service.auth, self.key, clock=lambda: self.now[0])
        self.password = 'synthetic mobile test password'
        self.mobile.enroll(self.f.owner, 'Alice', self.password, ['read','write','trash'], [{'driveId':'files','path':''}])

    def tearDown(self): self.f.close()

    def args(self, **changes):
        return dict({'username':'Alice','password':self.password,'deviceLabel':'Synthetic iPhone','requestId':str(uuid.uuid4())}, **changes)

    def login(self): return self.mobile.login(self.args())

    def test_verifiers_only_and_legacy_unchanged(self):
        response = self.login(); context = self.mobile.authenticate(response['accessToken'])
        self.assertEqual(context.principal.kind, 'agent')
        self.assertNotIn('settings', context.principal.scopes)
        self.assertNotIn('purge', context.principal.scopes)
        dump = '\n'.join(self.f.state.connection.iterdump())
        for secret in (self.password,response['accessToken'],response['refreshToken'],self.key.decode()): self.assertNotIn(secret,dump)
        self.assertEqual(self.f.service.auth.authenticate(self.f.token).id, self.f.owner.id)
        with self.assertRaises(BackendError): self.f.service.auth.authenticate(response['accessToken'])

    def test_generic_rejection_and_rate_limit(self):
        errors=[]
        for username in ('Alice','Unknown'):
            with self.assertRaises(BackendError) as err: self.mobile.login(self.args(username=username,password='bad'))
            errors.append((err.exception.code,str(err.exception)))
        self.assertEqual(errors[0],errors[1])
        for _ in range(9):
            with self.assertRaises(BackendError): self.mobile.login(self.args(password='bad'))
        with self.assertRaises(BackendError) as err: self.login()
        self.assertEqual(err.exception.code,'RATE_LIMITED')

    def test_login_idempotent_and_request_mismatch(self):
        args=self.args(); one=self.mobile.login(args); two=self.mobile.login(args)
        self.assertEqual(one,two)
        with self.assertRaises(BackendError): self.mobile.login(dict(args,deviceLabel='changed'))
        self.now[0]+=61
        with self.assertRaises(BackendError): self.mobile.login(args)

    def test_refresh_rotation_idempotent_across_restart(self):
        first=self.login(); context=self.mobile.authenticate(first['accessToken'])
        args={'refreshToken':first['refreshToken'],'requestId':str(uuid.uuid4())}
        second=self.mobile.refresh(args)
        restarted=MobileAuth(self.f.state,self.f.service.auth,self.key,clock=lambda:self.now[0])
        self.assertEqual(second,restarted.refresh(args))
        other=restarted.authenticate(second['accessToken'])
        self.assertEqual(context.session_id,other.session_id)
        self.assertEqual(context.principal.credential_id,other.principal.credential_id)
        self.assertNotEqual(first['refreshToken'],second['refreshToken'])

    def test_refresh_reuse_revocation_commits(self):
        first=self.login()
        second=self.mobile.refresh({'refreshToken':first['refreshToken'],'requestId':str(uuid.uuid4())})
        with self.assertRaises(BackendError): self.mobile.refresh({'refreshToken':first['refreshToken'],'requestId':str(uuid.uuid4())})
        self.assertTrue(self.f.state.get('mobile_sessions',first['deviceSessionId'])['revoked'])
        self.assertEqual(self.f.state.list('mobile_replays'),[])
        with self.assertRaises(BackendError): self.mobile.authenticate(second['accessToken'])

    def test_expired_replay_revokes_family(self):
        first=self.login(); args={'refreshToken':first['refreshToken'],'requestId':str(uuid.uuid4())}
        self.mobile.refresh(args); self.now[0]+=61
        with self.assertRaises(BackendError): self.mobile.refresh(args)
        self.assertTrue(self.f.state.get('mobile_sessions',first['deviceSessionId'])['revoked'])

    def test_old_replay_does_not_resurrect_superseded_tokens(self):
        first=self.login(); args={'refreshToken':first['refreshToken'],'requestId':str(uuid.uuid4())}
        second=self.mobile.refresh(args)
        self.mobile.refresh({'refreshToken':second['refreshToken'],'requestId':str(uuid.uuid4())})
        with self.assertRaises(BackendError): self.mobile.refresh(args)

    def test_access_expiry_and_idle_expiry(self):
        first=self.login(); self.now[0]+=ACCESS_SECONDS+1
        with self.assertRaises(BackendError) as err: self.mobile.authenticate(first['accessToken'])
        self.assertEqual(err.exception.code,'AUTH_EXPIRED')
        second=self.mobile.refresh({'refreshToken':first['refreshToken'],'requestId':str(uuid.uuid4())})
        self.mobile.authenticate(second['accessToken'])
        self.now[0]+=IDLE_SECONDS+1
        with self.assertRaises(BackendError): self.mobile.refresh({'refreshToken':second['refreshToken'],'requestId':str(uuid.uuid4())})

    def test_logout_reaches_existing_domain_principal(self):
        first=self.login(); context=self.mobile.authenticate(first['accessToken'])
        self.mobile.logout(first['accessToken'])
        with self.assertRaises(BackendError): self.f.service.auth.authorize(context.principal,'read','files','')
        with self.assertRaises(BackendError): self.mobile.refresh({'refreshToken':first['refreshToken'],'requestId':str(uuid.uuid4())})

    def test_scope_narrowing_reaches_existing_principal(self):
        context=self.mobile.authenticate(self.login()['accessToken'])
        self.mobile.assign(self.f.owner,['read'],[{'driveId':'files','path':'restricted'}])
        with self.assertRaises(BackendError): self.f.service.auth.authorize(context.principal,'read','files','')
        with self.assertRaises(BackendError): self.f.service.auth.authorize(context.principal,'write','files','restricted')
        self.f.service.auth.authorize(context.principal,'read','files','restricted')

    def test_device_isolation_and_recovery(self):
        first=self.login(); second=self.login()
        a=self.mobile.authenticate(first['accessToken']); b=self.mobile.authenticate(second['accessToken'])
        self.assertNotEqual(a.principal.id,b.principal.id)
        self.mobile.revoke_device(self.f.owner,first['deviceSessionId'])
        self.mobile.authenticate(second['accessToken'])
        self.mobile.recover_password(self.f.owner,'new synthetic password only')
        with self.assertRaises(BackendError): self.mobile.authenticate(second['accessToken'])
        with self.assertRaises(BackendError): self.login()

    def test_private_enrollment_and_session_limit(self):
        first=self.login(); context=self.mobile.authenticate(first['accessToken'])
        with self.assertRaises(BackendError): self.mobile.enroll(context.principal,'bob',self.password,['read'],[{'driveId':'files','path':''}])
        with self.assertRaises(BackendError): self.mobile.assign(self.f.owner,['read','settings'],[{'driveId':'files','path':''}])
        self.mobile.max_sessions=1
        with self.assertRaises(BackendError): self.login()

    def test_private_key_permissions(self):
        path=self.f.root/'key'; path.write_bytes(self.key); path.chmod(0o600)
        self.assertEqual(load_replay_key(str(path)),self.key)
        path.chmod(0o644)
        with self.assertRaises(BackendError): load_replay_key(str(path))

    def test_absolute_expiry_survives_refresh(self):
        first=self.login(); session=self.f.state.get('mobile_sessions',first['deviceSessionId'])
        session['absoluteExpires']=self.now[0]+2; self.f.state.put('mobile_sessions',session['id'],session)
        second=self.mobile.refresh({'refreshToken':first['refreshToken'],'requestId':str(uuid.uuid4())})
        self.now[0]+=3
        with self.assertRaises(BackendError): self.mobile.authenticate(second['accessToken'])

    def test_concurrent_exact_refresh_serializes(self):
        first=self.login(); args={'refreshToken':first['refreshToken'],'requestId':str(uuid.uuid4())}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            responses=list(pool.map(lambda _:self.mobile.refresh(args),range(2)))
        self.assertEqual(responses[0],responses[1])

    def test_expired_session_stops_domain_authorization(self):
        context=self.mobile.authenticate(self.login()['accessToken'])
        self.now[0]+=IDLE_SECONDS+1
        with self.assertRaises(BackendError): self.f.service.auth.authorize(context.principal,'read','files','')

    def test_account_assignment_is_a_ceiling_for_device_principal(self):
        context=self.mobile.authenticate(self.login()['accessToken'])
        self.f.service.auth.update_scopes(self.f.owner,context.principal.id,['read','write','trash','purge'],[{'driveId':'files','path':''}])
        with self.assertRaises(BackendError): self.f.service.auth.authorize(context.principal,'read','files','')


if __name__ == '__main__': unittest.main()
