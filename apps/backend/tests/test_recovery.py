"""Disposable real-filesystem recovery and independent-process integration tests."""
import hashlib
import threading
import time
import unittest
from unittest.mock import patch

from archive_backend.contracts import BackendError
from archive_backend.recovery import reconcile, reconcile_transfer, reconcile_job
from archive_backend.workers import WorkerPool
from support import Fixture


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.point = None
        def fault(point):
            if point == self.point:
                self.point = None
                raise RuntimeError('controlled stopped-call fixture')
        self.fixture = Fixture(fault=fault)
        self.service = self.fixture.service
        self.pools = []

    def tearDown(self):
        for pool in self.pools:
            pool.close(graceful_timeout=0)
        self.fixture.close()

    def stopped_call(self, job_id):
        # The faulting synchronous test call has returned; there is no old writer.
        with self.service.state.transaction():
            record = self.service.state.get('jobs', job_id)
            if record.get('_lease'):
                record['_lease']['expires'] = 0
            record['_workerStopped'] = True
            self.service.state.put('jobs', job_id, record)
        self.service.jobs.recover([job_id])

    def upload(self, data=b'abcdefgh', name='upload.bin', expected=True):
        args = {'requestId': 'upload-' + name, 'destination': {'driveId': 'files', 'path': ''}, 'name': name, 'sizeBytes': str(len(data))}
        if expected:
            args['sha256'] = hashlib.sha256(data).hexdigest()
        return self.service.transfers.initiate(self.fixture.owner, args)

    def test_after_chunk_flush_resumes_only_from_durable_offset(self):
        data = b'abcdefgh'
        session = self.upload(data)
        self.point = 'after-chunk-flush'
        with self.assertRaises(RuntimeError):
            self.service.transfers.write(self.fixture.owner, session['transferId'], 0, data[:4])
        self.assertEqual(self.service.transfers.query(self.fixture.owner, session['transferId'])['offset'], '0')
        self.stopped_call(session['jobId'])
        reconcile(self.service)
        self.assertEqual(self.service.jobs.get(self.fixture.owner, session['jobId'])['state'], 'failed')
        reconcile(self.service, allow_client_recovery=True)
        self.assertEqual(self.service.jobs.get(self.fixture.owner, session['jobId'])['state'], 'waiting-for-client')
        self.service.transfers.write(self.fixture.owner, session['transferId'], 0, data)
        self.service.transfers.finalize(self.fixture.owner, session['transferId'])
        with self.service.filesystems['files'].open_read('upload.bin') as output:
            self.assertEqual(output.read(), data)

    def test_published_upload_converges_before_terminal_without_duplicate_name(self):
        data = b'opaque\x00bytes'
        session = self.upload(data, expected=False)
        self.service.transfers.write(self.fixture.owner, session['transferId'], 0, data)
        self.point = 'before-transfer-terminal'
        with self.assertRaises(RuntimeError):
            self.service.transfers.finalize(self.fixture.owner, session['transferId'])
        self.assertTrue(reconcile_transfer(self.service, session['transferId']))
        result = self.service.transfers.finalize(self.fixture.owner, session['transferId'])
        self.assertEqual(result['verification']['digest'], hashlib.sha256(data).hexdigest())
        self.assertEqual(self.service.jobs.get(self.fixture.owner, session['jobId'])['state'], 'completed')
        entries = list(self.service.filesystems['files'].iter_entries(''))
        self.assertEqual([entry.name for entry in entries], ['upload.bin'])
        self.assertTrue(reconcile_transfer(self.service, session['transferId']))

    def test_after_publish_rename_recovery_checks_identity_and_hash(self):
        data = b'published-once'
        session = self.upload(data)
        self.service.transfers.write(self.fixture.owner, session['transferId'], 0, data)
        self.point = 'after-publish-rename'
        with self.assertRaises(RuntimeError):
            self.service.transfers.finalize(self.fixture.owner, session['transferId'])
        self.assertTrue(reconcile_transfer(self.service, session['transferId']))
        self.assertEqual(self.service.transfers.query(self.fixture.owner, session['transferId'])['state'], 'completed')

    def run_faulting(self, request, point):
        receipt = self.service.operations.submit(self.fixture.owner, request)
        self.point = point
        with self.assertRaises(RuntimeError):
            self.service.run_one()
        self.stopped_call(receipt['jobIds'][0])
        return receipt['jobIds'][0]

    def test_trash_and_restore_metadata_repaired_without_replaying_io(self):
        entry = self.fixture.put('trash-me.bin', b'trash bytes')
        job = self.run_faulting({'requestId': 'trash-one', 'kind': 'trash', 'targets': [self.fixture.ref(entry)]}, 'after-trash-rename')
        self.assertEqual(self.service.state.list('trash'), [])
        result = reconcile(self.service)
        self.assertGreaterEqual(result['repaired'], 1)
        trash = self.service.operations.trash_list(self.fixture.owner, 'files')
        self.assertEqual(len(trash), 1)
        self.assertEqual(self.service.jobs.get(self.fixture.owner, job)['state'], 'failed')
        self.run_faulting({'requestId': 'restore-one', 'kind': 'restore', 'targets': [self.fixture.ref(trash[0]['entry'])]}, 'after-restore')
        reconcile(self.service)
        self.assertEqual(self.service.state.list('trash'), [])
        with self.service.filesystems['files'].open_read('trash-me.bin') as restored:
            self.assertEqual(restored.read(), b'trash bytes')

    def test_purge_metadata_and_reservation_repair_never_replays_purge(self):
        entry = self.fixture.put('purge-me.bin')
        receipt = self.service.operations.submit(self.fixture.owner, {'requestId': 'trash-purge', 'kind': 'trash', 'targets': [self.fixture.ref(entry)]})
        self.service.run_one()
        item = self.service.operations.trash_list(self.fixture.owner, 'files')[0]['entry']
        ref = self.fixture.ref(item)
        grant = self.service.confirmations.issue(self.fixture.owner, 'purge', [ref], {'files': self.service.filesystems['files'].volume.generation})
        job_id = self.run_faulting({'requestId': 'purge-one', 'kind': 'purge', 'targets': [ref], 'confirmation': {'grantId': grant['id']}}, 'after-purge')
        self.service.state.put('reservations', 'reserved', {'id': 'reserved', 'job': job_id, 'drive': 'files', 'bytes': '4096'})
        reconcile(self.service)
        self.assertEqual(self.service.state.list('trash'), [])
        self.assertEqual(self.service.state.list('reservations'), [])
        cursor = self.service.state.cursor()
        reconcile(self.service)
        self.assertEqual(self.service.state.cursor(), cursor)

    def test_uncertain_mkdir_is_not_declared_completed_or_replayed(self):
        job_id = self.run_faulting({'requestId': 'mkdir-one', 'kind': 'new-folder', 'destination': {'driveId': 'files', 'path': ''}, 'name': 'created'}, 'after-mkdir')
        result = reconcile(self.service)
        self.assertGreaterEqual(result['uncertain'], 1)
        self.assertEqual(self.service.jobs.get(self.fixture.owner, job_id)['state'], 'failed')
        self.assertFalse(self.service.jobs.get(self.fixture.owner, job_id)['capabilities']['retry'])
        self.assertEqual(self.service.filesystems['files'].stat('created').kind, 'directory')
        self.assertFalse(reconcile_job(self.service, job_id))
        self.assertEqual(self.service.jobs.get(self.fixture.owner, job_id)['state'], 'failed')

    def test_unstarted_folder_recovery_is_scoped_and_reuses_same_job(self):
        job_id = self.service.operations.submit(self.fixture.owner, {'requestId': 'unstarted', 'kind': 'new-folder', 'destination': {'driveId': 'files', 'path': ''}, 'name': 'safe'})['jobIds'][0]
        self.service.jobs.claim('stopped-startup')
        self.stopped_call(job_id)
        with patch.object(self.service.state, 'scan', side_effect=AssertionError('Unrelated history scanned')):
            self.assertTrue(reconcile_job(self.service, job_id))
        self.assertEqual(self.service.jobs.get(self.fixture.owner, job_id)['state'], 'queued')
        self.service.run_one()
        self.assertEqual(self.service.jobs.get(self.fixture.owner, job_id)['state'], 'completed')
        self.assertEqual(self.service.filesystems['files'].stat('safe').kind, 'directory')

    def test_legacy_unstarted_folder_and_repeated_startup_failure(self):
        job_id = self.service.operations.submit(self.fixture.owner, {'requestId': 'legacy', 'kind': 'new-folder', 'destination': {'driveId': 'files', 'path': ''}, 'name': 'legacy'})['jobIds'][0]
        self.service.jobs.claim('legacy-worker')
        self.stopped_call(job_id)
        job = self.service.state.get('jobs', job_id)
        del job['_interruptedState']
        self.service.state.put('jobs', job_id, job)
        self.assertEqual(job['revision'], 3)
        self.assertTrue(reconcile_job(self.service, job_id))
        for attempt in range(3):
            self.service.jobs.claim('startup-' + str(attempt))
            self.stopped_call(job_id)
            self.assertEqual(reconcile_job(self.service, job_id), attempt < 2)
        self.assertEqual(self.service.jobs.get(self.fixture.owner, job_id)['state'], 'failed')

    def test_folder_waits_for_conflicting_work_and_can_be_paused(self):
        self.service.state.put('locks', 'other:lock', {'job':'other','drive':'files','path':''})
        for pause in (False, True):
            name = 'wait-' + str(pause)
            job_id = self.service.operations.submit(self.fixture.owner, {'requestId':name,'kind':'new-folder','destination':{'driveId':'files','path':''},'name':name})['jobIds'][0]
            lease = self.service.jobs.claim('waiting-worker')
            self.service.state.put('locks', 'other:lock', {'job':'other','drive':'files','path':''})
            errors = []
            def run():
                try: self.service.operations.execute(lease)
                except BaseException as error: errors.append(error)
            thread = threading.Thread(target=run)
            thread.start()
            deadline = time.monotonic() + 3
            while self.service.jobs.get(self.fixture.owner, job_id)['state'] != 'running' and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(self.service.jobs.get(self.fixture.owner, job_id)['state'], 'running')
            if pause:
                self.service.jobs.control(self.fixture.owner, job_id, 'pause')
            else:
                self.service.state.delete('locks', 'other:lock')
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(self.service.jobs.get(self.fixture.owner, job_id)['state'], 'paused' if pause else 'completed')

    def test_real_spawned_worker_finishes_independent_job(self):
        receipt = self.service.operations.submit(self.fixture.owner, {'requestId': 'process-one', 'kind': 'new-folder', 'destination': {'driveId': 'files', 'path': ''}, 'name': 'worker-created'})
        pool = WorkerPool(self.service, poll_interval=0.02)
        self.pools.append(pool)
        pool.start()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            status = self.service.jobs.get(self.fixture.owner, receipt['jobIds'][0])['state']
            if status in {'completed', 'failed'}:
                break
            time.sleep(0.02)
        self.assertEqual(status, 'completed', pool.last_error)
        self.assertEqual(self.service.filesystems['files'].stat('worker-created').kind, 'directory')

    def test_killed_real_worker_fences_and_other_work_continues(self):
        first = self.service.operations.submit(self.fixture.owner, {'requestId': 'kill-one', 'kind': 'new-folder', 'destination': {'driveId': 'files', 'path': ''}, 'name': 'not-created'})['jobIds'][0]
        pool = WorkerPool(self.service, fixture_delay=30)
        self.pools.append(pool)
        pool.run_once()
        self.assertIn(first, pool.active)
        self.assertTrue(pool.terminate_worker(first))
        self.assertEqual(self.service.jobs.get(self.fixture.owner, first)['state'], 'queued')
        with self.assertRaises(BackendError):
            self.service.filesystems['files'].stat('not-created')
        pool.fixture_delay = 0
        second = self.service.operations.submit(self.fixture.owner, {'requestId': 'after-kill', 'kind': 'new-folder', 'destination': {'driveId': 'backups', 'path': ''}, 'name': 'other-work'})['jobIds'][0]
        pool.start()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            status = self.service.jobs.get(self.fixture.owner, second)['state']
            if status in {'completed', 'failed'}:
                break
            time.sleep(0.02)
        self.assertEqual(status, 'completed', pool.last_error)


if __name__ == '__main__':
    unittest.main()
