"""Resolved destination scope regressions on disposable managed volumes."""
import unittest

from archive_backend.contracts import BackendError
from archive_backend.mcp_adapter import dispatch
from archive_backend.recovery import reconcile
from support import Fixture


class OperationAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.service = self.f.service
        self.token = 'fixture-destination-scope-' + 'x' * 40
        self.service.filesystems['files'].create_dir('allowed')
        self.source = self.f.put('allowed/source.bin', b'source bytes')
        self.agent = self.service.auth.register(self.f.owner, 'Scoped fixture', self.token,
            ['read', 'write', 'trash'], [{'driveId': 'files', 'path': 'allowed'}])

    def request(self, request_id, policy, ref=None, **extra):
        return dict(requestId=request_id, kind='rename', targets=[ref or self.f.ref(self.source)],
                    name='renamed.bin', conflictPolicy=policy, **extra)

    def narrow_to_source(self):
        self.service.auth.update_scopes(self.f.owner, self.agent.id, ['read', 'write', 'trash'],
            [{'driveId': 'files', 'path': self.source['path']}])

    def assert_denied(self, operation):
        with self.assertRaises(BackendError) as error:
            operation()
        self.assertEqual(error.exception.code, 'PERMISSION_DENIED')

    def test_implicit_parent_denied_for_each_policy_at_application_and_mcp(self):
        self.narrow_to_source()
        # A collision must not be inspected or replaced before scope rejection.
        self.f.put('allowed/renamed.bin', b'existing bytes')
        for policy in ('ask', 'skip', 'keep-both', 'replace'):
            with self.subTest(policy=policy):
                request = self.request('deny-' + policy, policy)
                self.assert_denied(lambda: self.f.call('operations.submit', {'request': request}, self.token))
                reply = dispatch(self.service, self.token, {'jsonrpc': '2.0', 'id': 1,
                    'method': 'tools/call', 'params': {'name': 'operations.submit', 'arguments': {'request': request}}})
                self.assertTrue(reply['result']['isError'])
                self.assertEqual(reply['result']['structuredContent']['error']['code'], 'PERMISSION_DENIED')
        self.assertEqual(self.f.state.list('jobs'), [])
        self.assertEqual(self.f.state.list('intents'), [])
        self.assertEqual(self.f.state.list('trash'), [])
        self.assertEqual((self.f.root / 'files/allowed/source.bin').read_bytes(), b'source bytes')
        self.assertEqual((self.f.root / 'files/allowed/renamed.bin').read_bytes(), b'existing bytes')

    def test_allowed_root_directory_cannot_be_renamed_to_unauthorized_sibling(self):
        entry = next(e for e in self.f.call('files.list', {'query': {'requestId': 'root',
            'location': {'driveId': 'files', 'path': ''}}})['entries'] if e['name'] == 'allowed')
        for policy in ('ask', 'skip', 'keep-both', 'replace'):
            self.assert_denied(lambda: self.service.operations.submit(self.agent,
                self.request('root-' + policy, policy, self.f.ref(entry))))
        self.assertTrue((self.f.root / 'files/allowed/source.bin').is_file())
        self.assertFalse((self.f.root / 'files/renamed.bin').exists())

    def test_authorized_rename_policies_keep_expected_collision_semantics(self):
        for policy in ('ask', 'skip', 'keep-both', 'replace'):
            with self.subTest(policy=policy):
                source = self.f.put('allowed/' + policy + '-source', b'source bytes')
                target = policy + '-target'
                self.f.put('allowed/' + target, b'existing bytes')
                request = self.request('allow-' + policy, policy, self.f.ref(source))
                request['name'] = target
                receipt = self.service.operations.submit(self.agent, request)
                if policy == 'ask':
                    self.assertEqual(receipt['status'], 'needs-decision')
                    self.assertTrue((self.f.root / 'files' / source['path']).exists())
                    continue
                self.service.run_one()
                job = self.service.jobs.get(self.agent, receipt['jobIds'][0])
                self.assertEqual(job['state'], 'completed')
                if policy == 'skip':
                    self.assertEqual(job['outcome'], 'skipped')
                    self.assertTrue((self.f.root / 'files' / source['path']).exists())
                else:
                    name = target + ' (1)' if policy == 'keep-both' else target
                    self.assertEqual((self.f.root / 'files/allowed' / name).read_bytes(), b'source bytes')
                expected = b'source bytes' if policy == 'replace' else b'existing bytes'
                self.assertEqual((self.f.root / 'files/allowed' / target).read_bytes(), expected)

    def test_queued_rename_rechecks_implicit_destination_after_scope_change(self):
        for policy in ('ask', 'skip', 'keep-both', 'replace'):
            with self.subTest(policy=policy):
                self.service.auth.update_scopes(self.f.owner, self.agent.id, ['read', 'write', 'trash'],
                    [{'driveId': 'files', 'path': 'allowed'}])
                receipt = self.service.operations.submit(self.agent, self.request('queued-' + policy, policy))
                self.narrow_to_source()
                self.assert_denied(self.service.run_one)
                self.assertEqual(self.f.state.get('jobs', receipt['jobIds'][0])['state'], 'failed')
                self.assertTrue((self.f.root / 'files/allowed/source.bin').exists())
                self.assertFalse((self.f.root / 'files/allowed/renamed.bin').exists())

    def test_scope_change_after_intent_blocks_rename_commit(self):
        self.service.operations.fault = lambda point: self.narrow_to_source() if point == 'after-intent' else None
        receipt = self.service.operations.submit(self.agent, self.request('commit-check', 'keep-both'))
        self.assert_denied(self.service.run_one)
        self.assertEqual(self.f.state.get('jobs', receipt['jobIds'][0])['state'], 'failed')
        self.assertTrue((self.f.root / 'files/allowed/source.bin').exists())
        self.assertFalse((self.f.root / 'files/allowed/renamed.bin').exists())

    def test_cross_drive_rename_rejected_instead_of_using_source_drive(self):
        request = self.request('wrong-drive', 'skip', destination={'driveId': 'backups', 'path': ''})
        with self.assertRaises(BackendError) as error:
            self.service.operations.submit(self.f.owner, request)
        self.assertEqual(error.exception.code, 'INVALID_ARGUMENT')
        self.assertTrue((self.f.root / 'files/allowed/source.bin').exists())

    def test_stale_source_and_volume_escape_remain_rejected(self):
        stale = dict(self.f.ref(self.source), revision=0)
        receipt = self.service.operations.submit(self.agent, self.request('stale', 'skip', stale))
        with self.assertRaises(BackendError) as error:
            self.service.run_one()
        self.assertEqual(error.exception.code, 'STALE_REVISION')
        self.assertEqual(self.f.state.get('jobs', receipt['jobIds'][0])['state'], 'failed')
        request = self.request('escape', 'replace')
        request['name'] = '../../outside'
        with self.assertRaises(BackendError) as error:
            self.service.operations.submit(self.f.owner, request)
        self.assertEqual(error.exception.code, 'INVALID_PATH')

    def test_recovery_does_not_bless_destination_after_scope_change(self):
        def fault(point):
            if point == 'after-rename':
                raise RuntimeError('stopped rename fixture')
        self.service.operations.fault = fault
        receipt = self.service.operations.submit(self.agent, self.request('recover-check', 'keep-both'))
        with self.assertRaises(RuntimeError):
            self.service.run_one()
        job_id = receipt['jobIds'][0]
        job = self.f.state.get('jobs', job_id)
        job['_workerStopped'] = True
        job['_lease']['expires'] = 0
        self.f.state.put('jobs', job_id, job)
        self.service.jobs.recover([job_id])
        self.narrow_to_source()
        result = reconcile(self.service)
        self.assertGreaterEqual(result['errors'], 1)
        self.assertEqual(self.f.state.get('intents', job_id + ':0')['status'], 'intent')
        self.assertEqual(self.f.state.get('jobs', job_id)['state'], 'failed')
        self.assertTrue((self.f.root / 'files/allowed/renamed.bin').exists())
        self.assertFalse((self.f.root / 'files/allowed/source.bin').exists())

    def test_legacy_queued_rename_is_revalidated_before_any_effect(self):
        self.narrow_to_source()
        for policy in ('ask', 'skip', 'keep-both', 'replace'):
            with self.subTest(policy=policy):
                request = self.request('legacy-' + policy, policy)
                # Simulate a job accepted by the old source-only validator.
                receipt = self.service.jobs.accept(self.agent, 'rename', request, request['requestId'])
                self.assert_denied(self.service.run_one)
                self.assertEqual(self.f.state.get('jobs', receipt['jobIds'][0])['state'], 'failed')
        self.assertEqual(self.f.state.list('intents'), [])
        self.assertTrue((self.f.root / 'files/allowed/source.bin').exists())
        self.assertFalse((self.f.root / 'files/allowed/renamed.bin').exists())

    def test_authorized_rename_without_collision_for_each_policy(self):
        for policy in ('ask', 'skip', 'keep-both', 'replace'):
            with self.subTest(policy=policy):
                source = self.f.put('allowed/no-collision-' + policy)
                request = self.request('no-collision-' + policy, policy, self.f.ref(source))
                request['name'] = 'done-' + policy
                receipt = self.service.operations.submit(self.agent, request)
                self.service.run_one()
                self.assertEqual(self.service.jobs.get(self.agent, receipt['jobIds'][0])['state'], 'completed')
                self.assertFalse((self.f.root / 'files' / source['path']).exists())
                self.assertTrue((self.f.root / 'files/allowed' / request['name']).exists())

    def test_authorized_rename_recovery_marks_proven_intent_without_replaying(self):
        def fault(point):
            if point == 'after-rename':
                raise RuntimeError('stopped rename fixture')
        self.service.operations.fault = fault
        receipt = self.service.operations.submit(self.agent, self.request('recover-authorized', 'keep-both'))
        with self.assertRaises(RuntimeError):
            self.service.run_one()
        job_id = receipt['jobIds'][0]
        job = self.f.state.get('jobs', job_id)
        job['_workerStopped'] = True
        job['_lease']['expires'] = 0
        self.f.state.put('jobs', job_id, job)
        self.service.jobs.recover([job_id])
        result = reconcile(self.service)
        self.assertEqual(result['repaired'], 1)
        self.assertEqual(self.f.state.get('intents', job_id + ':0')['status'], 'done')
        self.assertEqual(self.f.state.get('jobs', job_id)['state'], 'failed')
        self.assertTrue((self.f.root / 'files/allowed/renamed.bin').exists())
        self.assertFalse((self.f.root / 'files/allowed/renamed.bin (1)').exists())
        self.assertEqual(reconcile(self.service)['repaired'], 0)
