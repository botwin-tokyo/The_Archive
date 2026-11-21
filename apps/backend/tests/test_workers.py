"""Phase6 process primitive/supervision tests; real file-operation integration is separate."""
import multiprocessing
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from archive_backend.config import parse_config
from archive_backend.contracts import BackendError
from archive_backend.persistence import State
from archive_backend.security import Auth
from archive_backend.jobs import Jobs
from archive_backend.workers import WorkerPool


def _crash_probe():
    os._exit(23)


class FakeProcess:
    def __init__(self, events):
        self.events, self.alive, self.pid = events, False, 321
    def start(self):
        self.alive = True
        self.events.append('started')
    def is_alive(self):
        return self.alive
    def terminate(self):
        self.events.append('terminated')
        self.alive = False
    def kill(self):
        self.events.append('killed')
        self.alive = False
    def join(self, timeout=None):
        self.events.append('joined')
    def close(self):
        self.events.append('closed')


class WorkerPrimitiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / 'state.sqlite')
        self.auth = Auth(self.state)
        self.owner = self.auth.bootstrap_owner('fixture-owner-worker-secret-not-production')
        self.jobs = Jobs(self.state, self.auth)
        self.events = []
        self.service = SimpleNamespace(state=self.state, auth=self.auth, jobs=self.jobs, config=parse_config({}), registry=SimpleNamespace(list=lambda: []))
        self.pool = WorkerPool(self.service, _reconcile_hook=lambda _: self.events.append('reconciled'))
        self.pool._context = SimpleNamespace(Process=lambda **kwargs: FakeProcess(self.events))

    def tearDown(self):
        self.pool.close(graceful_timeout=0)
        self.state.close()
        self.tmp.cleanup()

    def accept(self, key):
        return self.jobs.accept(self.owner, 'new-folder', {'destination': {'driveId': 'files', 'path': ''}, 'name': key}, key)['jobIds'][0]

    def test_join_precedes_fencing_and_recovery(self):
        job_id = self.accept('one')
        self.pool.run_once()
        fence = self.pool.active[job_id]['fence']
        self.assertEqual(self.jobs.get(self.owner, job_id)['state'], 'preparing')
        self.assertTrue(self.pool.terminate_worker(job_id))
        self.assertLess(self.events.index('joined'), self.events.index('reconciled'))
        self.assertEqual(self.jobs.get(self.owner, job_id)['state'], 'failed')
        self.assertEqual(self.jobs.get(self.owner, job_id)['error']['code'], 'INTERRUPTED')
        self.assertGreater(self.state.get('jobs', job_id)['_fence'], fence)
        self.assertEqual(self.pool.active, {})

    def test_two_process_bound_and_shutdown_fences(self):
        ids = [self.accept(str(index)) for index in range(3)]
        self.pool.run_once()
        self.assertEqual(len(self.pool.active), 2)
        self.assertEqual(sum(self.jobs.get(self.owner, job_id)['state'] == 'queued' for job_id in ids), 1)
        self.pool.close(graceful_timeout=0)
        self.assertEqual(self.pool.active, {})
        self.assertEqual(self.events.count('reconciled'), 2)

    def test_settled_worker_only_cleans_its_own_resources(self):
        job_id = self.accept('settled')
        self.pool.run_once()
        item = self.pool._workers[job_id]
        job = self.state.get('jobs', job_id)
        job.update(state='completed', _lease=None)
        self.state.put('jobs', job_id, job)
        self.state.put('locks', job_id + ':lock', {'job': job_id})
        self.state.put('locks', 'other:lock', {'job': 'other'})
        item['process'].alive = False
        self.pool._finish(job_id, item)
        self.assertNotIn('reconciled', self.events)
        self.assertIsNone(self.state.get('locks', job_id + ':lock'))
        self.assertIsNotNone(self.state.get('locks', 'other:lock'))

    def test_fault_injection_not_available_for_production_registry(self):
        with self.assertRaises(BackendError):
            WorkerPool(self.service, fixture_fault='after-publish-rename')

    def test_actual_spawned_process_crash_is_observed_and_joined(self):
        process = multiprocessing.get_context('spawn').Process(target=_crash_probe)
        process.start()
        process.join(timeout=5)
        if process.is_alive():
            process.kill()
            process.join(timeout=2)
        self.assertFalse(process.is_alive())
        self.assertEqual(process.exitcode, 23)
        process.close()


if __name__ == '__main__':
    unittest.main()
