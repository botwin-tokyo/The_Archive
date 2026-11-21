import tempfile
import unittest
from pathlib import Path

from archive_backend.contracts import BackendError
from archive_backend.persistence import State
from archive_backend.security import Auth
from archive_backend.jobs import Jobs


class JobsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state.sqlite'
        self.state = State(self.path)
        self.auth = Auth(self.state)
        self.owner = self.auth.bootstrap_owner('fixture-owner-job-secret-not-production-01')
        self.agent = self.auth.register(self.owner, 'Agent', 'fixture-agent-job-secret-not-production-02', ['read', 'write'], [{'driveId': 'files', 'path': 'allowed'}])
        self.other = self.auth.register(self.owner, 'Other', 'fixture-other-job-secret-not-production-03', ['read', 'write'], [{'driveId': 'files', 'path': ''}])
        self.now = [100.0]
        self.jobs = Jobs(self.state, self.auth, clock=lambda: self.now[0])
        self.payload = {'targets': [{'driveId': 'files', 'path': 'allowed/item', 'id': 'entry', 'revision': 1}], 'destination': {'driveId': 'files', 'path': 'allowed/copies'}, 'bytesTotal': '100'}

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def accept(self, who=None, key='request'):
        return self.jobs.accept(who or self.agent, 'copy', self.payload, key)['jobIds'][0]

    def test_scheduler_and_snapshot_do_not_scan_completed_history(self):
        import json
        completed = dict(self.state.get('jobs', self.accept()), state='completed', _lease=None)
        with self.state.transaction() as c:
            c.execute("DELETE FROM records WHERE namespace='jobs'")
            c.executemany("INSERT INTO records(namespace,key,body,updated) VALUES('jobs',?,?,?)",
                          ((f'history-{i}', json.dumps(dict(completed, id=f'history-{i}')), i) for i in range(5000)))
        statements = []
        self.state.connection.set_trace_callback(statements.append)
        try:
            self.accept(key='indexed-admission')
            query = next(q for q in statements if q.startswith('SELECT COUNT(*) FROM records'))
            plan = ' '.join(str(tuple(row)) for row in self.state.connection.execute('EXPLAIN QUERY PLAN ' + query))
            self.assertIn('active_upload_jobs', plan)
            self.assertIn('active_upload_jobs', query)
            admitted = self.state.get('jobs', self.accept(key='indexed-admission'))
            admitted['state'] = 'completed'
            self.state.put('jobs', admitted['id'], admitted)
            self.assertIsNone(self.jobs.claim('idle-worker'))
            self.assertEqual(len(self.jobs.snapshot(self.owner, limit=5)['jobs']), 5)
        finally:
            self.state.connection.set_trace_callback(None)
        for marker, index in [('ORDER BY updated,key', 'schedulable_jobs'), ('ORDER BY CASE', 'job_snapshot_order')]:
            query = next(q for q in statements if q.startswith('SELECT body FROM records') and marker in q)
            plan = ' '.join(str(tuple(row)) for row in self.state.connection.execute('EXPLAIN QUERY PLAN ' + query))
            self.assertIn(index, plan)
            self.assertNotIn('TEMP B-TREE', plan)
        # A terminal job still holding a live lease consumes worker capacity.
        live = dict(completed, id='terminal-lease', _lease={'worker':'old', 'expires':200, 'fence':1})
        self.state.put('jobs', live['id'], live)
        self.jobs.max_active = 1
        self.accept(key='pending')
        self.assertIsNone(self.jobs.claim('new-worker'))

    def test_accept_reconcile_filter_and_no_internal_data(self):
        job_id = self.accept()
        self.assertEqual(self.accept(), job_id)
        with self.assertRaises(BackendError):
            self.jobs.accept(self.agent, 'copy', {**self.payload, 'bytesTotal': '200'}, 'request')
        value = self.jobs.get(self.agent, job_id)
        self.assertFalse(value['simulated'])
        self.assertFalse(any(key.startswith('_') for key in value))
        self.assertEqual(self.jobs.snapshot(self.other)['jobs'], [])
        with self.assertRaises(BackendError):
            self.jobs.get(self.other, job_id)
        self.assertEqual(len(self.jobs.snapshot(self.owner)['jobs']), 1)
        self.assertEqual(self.jobs.events(self.other)['events'], [])
        self.assertEqual(len(self.jobs.events(self.agent)['events']), 1)

    def test_fenced_state_progress_and_pause_ack(self):
        job_id = self.accept()
        lease = self.jobs.claim('worker')
        self.assertEqual(lease['job']['id'], job_id)
        fence = lease['fence']
        self.jobs.transition(job_id, 'running', 'worker', fence)
        self.jobs.update(job_id, 'worker', fence, {'bytesDone': '100'})
        self.assertEqual(self.jobs.get(self.agent, job_id)['state'], 'running')
        self.assertEqual(self.jobs.control(self.agent, job_id, 'pause')['state'], 'pausing')
        self.jobs.transition(job_id, 'paused', 'worker', fence)
        self.assertEqual(self.jobs.control(self.agent, job_id, 'resume')['state'], 'queued')
        with self.assertRaises(BackendError):
            self.jobs.transition(job_id, 'running', 'worker', fence)
        next_lease = self.jobs.claim('new-worker')
        for state in ['running', 'verifying', 'committing', 'completed']:
            self.jobs.transition(job_id, state, 'new-worker', next_lease['fence'])
        self.assertEqual(self.jobs.get(self.agent, job_id)['state'], 'completed')
        with self.assertRaises(BackendError):
            self.jobs.control(self.agent, job_id, 'retry')

    def test_lease_expiry_and_restart_do_not_replay_effects(self):
        job_id = self.accept()
        lease = self.jobs.claim('worker', lease_seconds=1)
        self.now[0] += 2
        stopped = self.state.get('jobs', job_id)
        stopped['_workerStopped'] = True  # The test's modeled worker has exited.
        self.state.put('jobs', job_id, stopped)
        self.assertEqual(self.jobs.recover(), 1)
        value = self.jobs.get(self.agent, job_id)
        self.assertEqual(value['error']['code'], 'INTERRUPTED')
        self.assertFalse(value['capabilities']['retry'])
        with self.assertRaises(BackendError):
            self.jobs.transition(job_id, 'running', 'worker', lease['fence'])
        self.state.close()
        self.state = State(self.path)
        self.auth = Auth(self.state)
        self.jobs = Jobs(self.state, self.auth, clock=lambda: self.now[0])
        self.assertEqual(self.jobs.get(self.agent, job_id)['state'], 'failed')
        self.jobs.authorize_retry(job_id, {'reconciled': True, 'destinationCommitted': False})
        retry = self.jobs.control(self.agent, job_id, 'retry')
        self.assertEqual(retry['previousAttemptId'], job_id)
        self.assertNotEqual(retry['id'], job_id)
        self.assertTrue(self.state.get('jobs', retry['id'])['_checkpoint']['reconciled'])

    def test_revocation_and_scope_change_stop_status_and_resume(self):
        job_id = self.accept()
        lease = self.jobs.claim('worker')
        self.jobs.transition(job_id, 'running', 'worker', lease['fence'])
        self.auth.revoke(self.owner, self.agent.id)
        with self.assertRaises(BackendError):
            self.jobs.heartbeat(job_id, 'worker', lease['fence'])
        with self.assertRaises(BackendError):
            self.jobs.get(self.agent, job_id)
        self.assertFalse(self.jobs.get(self.owner, job_id)['capabilities']['pause'])
        self.assertFalse(self.jobs.get(self.owner, job_id)['capabilities']['resume'])
        self.accept(self.other, 'other')
        self.assertTrue(self.jobs.snapshot(self.other)['jobs'])

    def test_bounded_fair_claim_and_client_lease(self):
        self.jobs = Jobs(self.state, self.auth, clock=lambda: self.now[0], max_active=2, per_principal=1)
        first, second = self.accept(key='a'), self.accept(key='b')
        third = self.accept(self.other, 'c')
        lease1, lease2 = self.jobs.claim('one'), self.jobs.claim('two')
        self.assertNotEqual(lease1['job']['actor']['id'], lease2['job']['actor']['id'])
        self.assertIsNone(self.jobs.claim('three'))
        client = self.jobs.accept(self.owner, 'upload', {'destination': {'driveId': 'files', 'path': ''}}, 'upload', initial_state='waiting-for-client')['jobIds'][0]
        with self.assertRaises(BackendError):
            self.jobs.claim_id(self.owner, client, 'client')
        self.assertEqual(self.jobs.get(self.owner, client)['state'], 'waiting-for-client')
        for lease in (lease1, lease2):
            self.jobs.transition(lease['job']['id'], 'failed', lease['workerId'], lease['fence'], {'error': {'code': 'INTERRUPTED', 'retryable': False}})
        client_lease = self.jobs.claim_id(self.owner, client, 'client')
        self.jobs.transition(client, 'running', 'client', client_lease['fence'])
        self.jobs.transition(client, 'waiting-for-client', 'client', client_lease['fence'])
        with self.assertRaises(BackendError):
            self.jobs.update(client, 'client', client_lease['fence'], {'bytesDone': '1'})
        renewed = self.jobs.claim_id(self.owner, client, 'client-2')
        self.assertGreater(renewed['fence'], client_lease['fence'])

    def test_read_only_downgrade_can_inspect_but_cannot_resume(self):
        job_id = self.accept()
        lease = self.jobs.claim('worker')
        self.jobs.transition(job_id, 'running', 'worker', lease['fence'])
        self.jobs.control(self.agent, job_id, 'pause')
        self.jobs.transition(job_id, 'paused', 'worker', lease['fence'])
        self.auth.update_scopes(self.owner, self.agent.id, ['read'], [{'driveId': 'files', 'path': 'allowed'}])
        self.assertEqual(self.jobs.get(self.agent, job_id)['state'], 'paused')
        self.assertFalse(self.jobs.get(self.agent, job_id)['capabilities']['resume'])
        with self.assertRaises(BackendError):
            self.jobs.control(self.agent, job_id, 'resume')
        self.accept(self.other, 'new-other')
        self.assertEqual(self.jobs.snapshot(self.agent, limit=1)['jobs'][0]['id'], job_id)

    def test_client_cancel_ack_and_queued_scheduler_cancel(self):
        client = self.jobs.accept(self.owner, 'upload', {'destination': {'driveId': 'files', 'path': ''}}, 'cancel-upload', initial_state='waiting-for-client')['jobIds'][0]
        self.assertEqual(self.jobs.control(self.owner, client, 'cancel')['state'], 'canceling')
        self.assertEqual(self.jobs.acknowledge_cancel(self.owner, client)['state'], 'canceled')
        queued = self.accept()
        self.jobs.control(self.agent, queued, 'cancel')
        self.assertIsNone(self.jobs.claim('worker'))
        self.assertEqual(self.jobs.get(self.agent, queued)['state'], 'canceled')

    def test_expired_live_client_lease_is_not_reclaimed(self):
        client = self.jobs.accept(self.owner, 'upload', {'destination': {'driveId': 'files', 'path': ''}}, 'live-upload', initial_state='waiting-for-client')['jobIds'][0]
        lease = self.jobs.claim_id(self.owner, client, 'client', lease_seconds=1)
        self.now[0] += 2
        self.jobs.recover([client])
        self.assertIsNotNone(self.state.get('jobs', client)['_lease'])
        self.assertFalse(self.state.get('jobs', client)['_workerStopped'])
        with self.assertRaises(BackendError):
            self.jobs.claim_id(self.owner, client, 'other-client')
        with self.assertRaises(BackendError):
            self.jobs.authorize_retry(client, {'reconciled': True})
        stopped = self.state.get('jobs', client)
        stopped['_workerStopped'] = True
        self.state.put('jobs', client, stopped)
        self.jobs.recover([client])
        self.assertIsNone(self.state.get('jobs', client)['_lease'])

    def test_retained_outbox_gap_requests_snapshot(self):
        self.accept()
        with self.state.transaction():
            for index in range(10005):
                self.state.emit('diagnostic', self.owner.id, {'index': index})
        self.assertEqual(self.state.connection.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 10000)
        events = self.jobs.events(self.agent, 0)
        self.assertTrue(events['gap'])
        self.assertEqual(len(self.jobs.snapshot(self.agent)['jobs']), 1)

    def test_quota_and_invalid_transition_leave_no_false_receipt(self):
        self.jobs = Jobs(self.state, self.auth, max_queued=1)
        job_id = self.accept()
        with self.assertRaises(BackendError):
            self.accept(key='second')
        self.assertIsNone(self.state.reconcile(self.agent.id, 'copy', 'second'))
        with self.assertRaises(BackendError):
            self.jobs.transition(job_id, 'completed')
        self.assertEqual(self.jobs.get(self.agent, job_id)['state'], 'queued')


if __name__ == '__main__':
    unittest.main()
