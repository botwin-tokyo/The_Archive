import json
import tempfile
import unittest
from pathlib import Path
from dataclasses import FrozenInstanceError

from archive_backend.contracts import BackendError
from archive_backend.persistence import State
from archive_backend.security import Auth, Confirmations

OWNER = 'fixture-owner-secret-not-production-000001'
AGENT = 'fixture-agent-secret-not-production-000002'


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / 'state.sqlite')
        self.auth = Auth(self.state)
        self.owner = self.auth.bootstrap_owner(OWNER)
        self.agent = self.auth.register(self.owner, 'Fixture agent', AGENT, ['read', 'write', 'trash', 'purge'], [{'driveId': 'files', 'path': 'allowed'}])

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def test_verifiers_and_immutable_principal(self):
        self.assertEqual(self.auth.authenticate(AGENT).id, self.agent.id)
        records = json.dumps(self.state.list('credentials'))
        self.assertNotIn(AGENT, records)
        self.assertNotIn(OWNER, records)
        self.assertIn('verifier', records)
        self.assertNotIn('verifier', json.dumps(self.auth.list(self.owner)))
        with self.assertRaises(FrozenInstanceError):
            self.agent.label = 'changed'
        with self.assertRaises(BackendError):
            self.auth.authenticate('invalid-credential-that-is-long-enough')
        with self.assertRaises(BackendError):
            self.auth.bootstrap_owner(OWNER)

    def test_verification_cache_rechecks_permissions_revocation_and_proof(self):
        from unittest.mock import patch
        import hashlib
        with patch('archive_backend.security.hashlib.pbkdf2_hmac', wraps=hashlib.pbkdf2_hmac) as derive:
            self.auth.authenticate(AGENT)
            self.auth.authenticate(AGENT)
            self.assertEqual(derive.call_count, 1)
            self.auth.update_scopes(self.owner, self.agent.id, ['read'], [{'driveId':'files','path':'new'}])
            fresh = self.auth.authenticate(AGENT)
            self.assertEqual(fresh.scopes, ('read',))
            self.assertEqual(fresh.roots, (('files', 'new'),))
            self.assertEqual(derive.call_count, 1)
            selector = self.auth._selector(AGENT)
            record = self.state.get('credentials', selector)
            record['verifier'] = '00' * 32
            self.state.put('credentials', selector, record)
            with self.assertRaises(BackendError):
                self.auth.authenticate(AGENT)
            self.assertEqual(derive.call_count, 2)
        self.auth.revoke(self.owner, self.agent.id)
        with self.assertRaises(BackendError):
            self.auth.authenticate(AGENT)

    def test_verification_cache_expires_and_is_bounded_without_raw_secrets(self):
        from unittest.mock import patch
        import hashlib
        with patch('archive_backend.security.time.monotonic', return_value=100):
            self.auth.authenticate(AGENT)
        with patch('archive_backend.security.time.monotonic', return_value=401), patch('archive_backend.security.hashlib.pbkdf2_hmac', wraps=hashlib.pbkdf2_hmac) as derive:
            self.auth.authenticate(AGENT)
            self.assertEqual(derive.call_count, 1)
        for i in range(256):
            self.auth._verified[('fixture', i)] = 1000
        self.auth.authenticate(OWNER)
        self.assertEqual(len(self.auth._verified), 256)
        self.assertNotIn(AGENT, repr(self.auth._verified))
        self.assertNotIn(OWNER, repr(self.auth._verified))

    def test_prefix_scope_and_owner_administration(self):
        self.auth.authorize(self.agent, 'read', 'files', 'allowed/child')
        for drive, path in [('files', 'allowed-other'), ('backup', 'allowed'), ('files', 'allowed/../outside')]:
            with self.assertRaises(BackendError):
                self.auth.authorize(self.agent, 'read', drive, path)
        with self.assertRaises(BackendError):
            self.auth.list(self.agent)
        with self.assertRaises(BackendError):
            self.auth.register(self.agent, 'Other', 'fixture-other-secret-not-production-003', ['read'], [{'driveId': 'files', 'path': ''}])

    def test_scope_change_revocation_and_rotation_rechecked(self):
        self.auth.authenticate(AGENT)  # Populate verification cache before rotation.
        self.auth.update_scopes(self.owner, self.agent.id, ['read'], [{'driveId': 'files', 'path': 'new'}])
        with self.assertRaises(BackendError):
            self.auth.authorize(self.agent, 'read', 'files', 'allowed')
        fresh = self.auth.authorize(self.agent, 'read', 'files', 'new')
        self.assertEqual(fresh.scope_version, 2)
        self.auth.rotate(self.owner, self.agent.id, 'fixture-rotated-secret-not-production-0004')
        with self.assertRaises(BackendError):
            self.auth.refresh(self.agent)
        rotated = self.auth.authenticate('fixture-rotated-secret-not-production-0004')
        self.auth.revoke(self.owner, self.agent.id)
        with self.assertRaises(BackendError):
            self.auth.authenticate('fixture-rotated-secret-not-production-0004')
        with self.assertRaises(BackendError) as error:
            self.auth.authorize(rotated, 'read', 'files', 'new')
        self.assertEqual(error.exception.code, 'PERMISSION_REVOKED')
        self.auth.authorize(self.owner, 'read', 'files', '')

    def test_confirmation_exact_bound_expiring_single_use_and_rollback(self):
        now = [100.0]
        confirmations = Confirmations(self.state, self.auth, lambda: now[0])
        targets = [{'driveId': 'files', 'path': 'allowed/item', 'id': 'one', 'revision': 1}]
        grant = confirmations.issue(self.agent, 'purge', targets, {'files': 1})
        with self.assertRaises(BackendError):
            confirmations.consume(self.agent, 'purge', [{**targets[0], 'revision': 2}], {'files': 1}, grant['id'])
        with self.assertRaises(BackendError):
            confirmations.consume(self.agent, 'purge', targets, {'files': 2}, grant['id'])
        with self.assertRaises(RuntimeError):
            with self.state.transaction():
                confirmations.consume(self.agent, 'purge', targets, {'files': 1}, grant['id'])
                raise RuntimeError('intent failed')
        confirmations.consume(self.agent, 'purge', targets, {'files': 1}, grant['id'])
        with self.assertRaises(BackendError):
            confirmations.consume(self.agent, 'purge', targets, {'files': 1}, grant['id'])
        second = confirmations.issue(self.agent, 'purge', targets, {'files': 1}, ttl=1)
        now[0] += 2
        with self.assertRaises(BackendError):
            confirmations.consume(self.agent, 'purge', targets, {'files': 1}, second['id'])
        with self.assertRaises(BackendError):
            confirmations.issue(self.agent, 'purge', [{'id': 'missing'}], {})

    def test_confirmation_invalidated_by_scope_change_and_other_actor(self):
        targets = [{'driveId': 'files', 'path': 'allowed/item', 'id': 'one', 'revision': 1}]
        confirmations = Confirmations(self.state, self.auth)
        grant = confirmations.issue(self.agent, 'purge', targets, {'files': 1})
        with self.assertRaises(BackendError):
            confirmations.consume(self.owner, 'purge', targets, {'files': 1}, grant['id'])
        self.auth.update_scopes(self.owner, self.agent.id, ['read', 'purge'], [{'driveId': 'files', 'path': 'allowed'}])
        with self.assertRaises(BackendError):
            confirmations.consume(self.agent, 'purge', targets, {'files': 1}, grant['id'])


if __name__ == '__main__':
    unittest.main()
