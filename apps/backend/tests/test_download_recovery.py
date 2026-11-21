"""Startup recovery of durable downloads, using only disposable service state."""
import unittest
from unittest.mock import patch

from archive_backend.application import Service
from archive_backend.contracts import BackendError
from archive_backend.persistence import State
from archive_backend.recovery import reconcile
from archive_backend.volumes import VolumeRegistry
from support import Fixture


class DownloadRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(self.fixture.close)

    @property
    def service(self):
        return self.fixture.service

    def download(self, name='source.bin', principal=None):
        entry = self.fixture.put(name, b'abcdefgh')
        principal = principal or self.fixture.owner
        session = self.service.transfers.download(principal, self.fixture.ref(entry), 'download-' + name)
        self.service.transfers.download_progress(principal, session['transferId'], '3', False)
        return session

    def interrupt(self, session, principal=None, *, stopped=True):
        principal = principal or self.fixture.owner
        lease = self.service.jobs.claim_id(principal, session['jobId'], 'stopped-download-fixture')
        self.service.jobs.transition(session['jobId'], 'running', 'stopped-download-fixture', lease['fence'])
        # The synchronous test call has returned and has no byte writer. Persist
        # that proof exactly as the supervisor does after joining a worker.
        job = self.service.state.get('jobs', session['jobId'])
        job['_workerStopped'] = stopped
        if stopped:
            job['_lease']['expires'] = 0
        self.service.state.put('jobs', job['id'], job)

    def restart(self):
        config = self.service.config
        self.service.close()
        self.fixture.state = State(self.fixture.root / 'state.sqlite')
        self.fixture.registry = VolumeRegistry.fixtures({name: self.fixture.root / name for name in ('files', 'backups')})
        self.fixture.service = Service(config, self.fixture.state, self.fixture.registry)
        self.assertFalse(self.service.ready)
        self.service.start()
        self.assertTrue(self.service.ready)

    def record(self, session):
        return self.service.state.get('transfers', session['transferId'])

    def save(self, session, record):
        self.service.state.put('transfers', session['transferId'], record)

    def assert_failed(self, session):
        job = self.service.jobs.get(self.fixture.owner, session['jobId'])
        self.assertEqual(job['state'], 'failed')
        self.assertEqual(job['error']['code'], 'INTERRUPTED')
        self.assertFalse(job['capabilities']['retry'])
        self.assertEqual(self.record(session)['offset'], '3')

    def test_download_persists_source_provenance_and_acceptance_is_atomic(self):
        session = self.download()
        record = self.record(session)
        self.assertEqual(record['generation'], self.service.filesystems['files'].volume.generation)
        self.assertEqual(record['revision'], record['ref']['revision'])
        original_put = self.service.state.put
        def fail_session(namespace, key, value):
            if namespace == 'transfers':
                raise BackendError('STATE_UNAVAILABLE', 'Controlled metadata write failure')
            return original_put(namespace, key, value)
        with patch.object(self.service.state, 'put', side_effect=fail_session):
            with self.assertRaises(BackendError):
                self.service.transfers.download(self.fixture.owner, record['ref'], 'failed-acceptance')
        self.assertIsNone(self.service.state.reconcile(self.fixture.owner.id, 'download', 'failed-acceptance'))
        self.assertEqual(len(self.service.state.list('jobs')), 1)

    def test_service_start_recovers_same_download_and_durable_offset(self):
        session = self.download()
        generation = self.record(session)['generation']
        self.interrupt(session)
        self.restart()
        record = self.record(session)
        self.assertEqual(record['generation'], generation)
        self.assertEqual(record['state'], 'waiting-for-client')
        self.assertEqual(record['offset'], '3')
        job = self.service.jobs.get(self.fixture.owner, session['jobId'])
        self.assertEqual((job['state'], job['bytesDone']), ('waiting-for-client', '3'))
        remaining = self.service.transfers.read(self.fixture.owner, record['drive'], record['path'], record['revision'], 3, 5)
        self.assertEqual(remaining, b'defgh')
        self.service.transfers.download_progress(self.fixture.owner, session['transferId'], '8', True)
        self.assertEqual(self.service.jobs.get(self.fixture.owner, session['jobId'])['state'], 'completed')
        self.assertEqual(len(self.service.state.list('jobs')), 1)

    def test_legacy_record_stays_failed_until_explicit_zero_restart(self):
        session = self.download()
        record = self.record(session)
        del record['generation']
        del record['revision']
        self.save(session, record)
        self.interrupt(session)
        self.restart()
        self.assert_failed(session)
        self.assertNotIn('generation', self.record(session))
        result = self.service.transfers.restart_download(self.fixture.owner, session['transferId'])
        self.assertEqual((result['jobId'], result['offset']), (session['jobId'], '0'))
        self.assertEqual(self.record(session)['generation'], self.service.filesystems['files'].volume.generation)
        self.interrupt(session)
        self.restart()
        self.assertEqual(self.record(session)['state'], 'waiting-for-client')
        self.assertEqual(self.record(session)['offset'], '0')

    def test_saved_generation_can_use_revision_from_exact_ref(self):
        session = self.download()
        record = self.record(session)
        del record['revision']
        self.save(session, record)
        self.interrupt(session)
        self.restart()
        self.assertEqual(self.record(session)['state'], 'waiting-for-client')
        self.assertEqual(self.record(session)['revision'], record['ref']['revision'])

    def test_changed_source_stays_failed_without_blocking_startup(self):
        session = self.download()
        self.interrupt(session)
        (self.fixture.root / 'files/source.bin').write_bytes(b'changed bytes')
        self.restart()
        self.assert_failed(session)

    def test_changed_generation_stays_failed_without_blocking_startup(self):
        session = self.download()
        record = self.record(session)
        record['generation'] = record['generation'] % (2**53 - 1) + 1
        self.save(session, record)
        self.interrupt(session)
        self.restart()
        self.assert_failed(session)

    def test_conflicting_source_metadata_stays_failed(self):
        session = self.download()
        record = self.record(session)
        record['revision'] = record['revision'] ^ 1
        self.save(session, record)
        self.interrupt(session)
        self.restart()
        self.assert_failed(session)

    def test_revoked_principal_stays_failed_without_blocking_startup(self):
        principal = self.service.auth.register(self.fixture.owner, 'Download fixture', 'download-fixture-' + 'z' * 40, ['read'], [{'driveId': 'files', 'path': ''}])
        session = self.download(principal=principal)
        self.interrupt(session, principal)
        self.service.auth.revoke(self.fixture.owner, principal.id)
        self.restart()
        self.assert_failed(session)

    def test_live_lease_is_not_recovered(self):
        session = self.download()
        self.interrupt(session, stopped=False)
        self.restart()
        job = self.service.state.get('jobs', session['jobId'])
        self.assertEqual(job['state'], 'running')
        self.assertIsNotNone(job['_lease'])

    def test_full_offset_does_not_infer_completion(self):
        session = self.download()
        self.service.transfers.download_progress(self.fixture.owner, session['transferId'], '8', False)
        self.interrupt(session)
        self.restart()
        job = self.service.jobs.get(self.fixture.owner, session['jobId'])
        self.assertEqual((job['state'], job['bytesDone']), ('waiting-for-client', '8'))

    def test_terminal_download_is_unchanged(self):
        session = self.download()
        self.service.transfers.download_progress(self.fixture.owner, session['transferId'], '8', True)
        record = self.record(session)
        self.restart()
        self.assertEqual(self.record(session), record)
        self.assertEqual(self.service.jobs.get(self.fixture.owner, session['jobId'])['state'], 'completed')

    def test_malformed_records_are_isolated_and_healthy_download_recovers(self):
        healthy = self.download('healthy.bin')
        invalid = self.download('invalid.bin')
        record = self.record(invalid)
        record['offset'] = 'not-a-byte-count'
        self.save(invalid, record)
        self.interrupt(healthy)
        self.interrupt(invalid)
        malformed = [None, [], {}, {'direction': 'download'}]
        for index, value in enumerate(malformed):
            self.service.state.put('transfers', '!malformed-' + str(index), value)
        for namespace in ('intents', 'locks', 'reservations'):
            self.service.state.put(namespace, '!malformed', None)
        self.restart()
        self.assertEqual(self.record(healthy)['state'], 'waiting-for-client')
        self.assertEqual(self.service.jobs.get(self.fixture.owner, invalid['jobId'])['state'], 'failed')
        self.assertEqual(self.record(invalid), record)
        result = reconcile(self.service, allow_client_recovery=True)
        self.assertGreaterEqual(result['errors'], len(malformed) + 4)
        self.assertEqual(result['transfers'], 0)

    def test_unexpected_runtime_failure_is_not_hidden_as_record_damage(self):
        session = self.download()
        self.interrupt(session)
        self.service.jobs.recover()
        with patch.object(self.service.catalog, 'filesystem', side_effect=RuntimeError('controlled runtime fault')):
            with self.assertRaises(RuntimeError):
                reconcile(self.service, allow_client_recovery=True)


if __name__ == '__main__':
    unittest.main()
