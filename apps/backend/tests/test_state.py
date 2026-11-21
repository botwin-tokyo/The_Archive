import concurrent.futures
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from archive_backend.contracts import BackendError
from archive_backend.persistence import State, digest


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state.sqlite'
        self.state = State(self.path)

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def test_worker_opens_validated_database_without_history_scan_or_migration(self):
        info = self.path.stat()
        statements = []
        connect = sqlite3.connect
        def traced(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection
        with patch('archive_backend.persistence.sqlite3.connect', traced):
            worker = State(self.path, validated_identity=(info.st_dev, info.st_ino))
            worker.close()
        sql = ' '.join(statements).upper()
        self.assertNotIn('QUICK_CHECK', sql)
        # The receipt guard still verifies under its short writer lock.
        self.assertEqual(sql.count('BEGIN IMMEDIATE'), 1)
        self.assertNotIn('CREATE TABLE', sql)

    def test_worker_rejects_database_replacement_and_schema_change(self):
        info = self.path.stat()
        with self.assertRaises(BackendError):
            State(self.path, validated_identity=(info.st_dev, info.st_ino + 1))
        self.state.connection.execute("UPDATE migrations SET checksum='changed'")
        with self.assertRaises(BackendError):
            State(self.path, validated_identity=(info.st_dev, info.st_ino))

    def test_restart_backup_and_modes(self):
        self.assertEqual(self.state.connection.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
        self.assertEqual(self.state.connection.execute('PRAGMA synchronous').fetchone()[0], 2)
        self.state.put('transfers', 'one', {'offset': '123'})
        self.state.backup(Path(self.tmp.name) / 'backup.sqlite')
        self.state.close()
        self.state = State(self.path)
        self.assertEqual(self.state.get('transfers', 'one')['offset'], '123')
        backup = State(Path(self.tmp.name) / 'backup.sqlite')
        self.assertEqual(backup.get('transfers', 'one'), {'offset': '123'})
        backup.close()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_nested_rollback_and_bounded_keyset(self):
        with self.state.transaction():
            self.state.put('jobs', 'a', {'value': 1})
            with self.assertRaises(RuntimeError):
                with self.state.transaction():
                    self.state.put('jobs', 'b', {'value': 2})
                    raise RuntimeError('rollback savepoint')
            self.state.put('jobs', 'c', {'value': 3})
        self.assertIsNone(self.state.get('jobs', 'b'))
        self.assertEqual([row['key'] for row in self.state.scan('jobs', limit=1)], ['a'])
        self.assertEqual([row['key'] for row in self.state.scan('jobs', after='a')], ['c'])
        with self.assertRaises(BackendError):
            self.state.list('jobs', limit=10001)

    def test_duplicate_accept_race_is_atomic(self):
        def submit(_):
            return self.state.atomic_accept('actor', 'copy', 'key', digest({'target': 1}), {'id': 'job-one', 'state': 'queued'})
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            receipts = list(pool.map(submit, range(16)))
        self.assertTrue(all(receipt == receipts[0] for receipt in receipts))
        self.assertEqual(len(self.state.list('jobs')), 1)
        self.assertEqual(len(self.state.select('SELECT * FROM audit')), 1)
        self.assertEqual(len(self.state.events(0)), 1)
        with self.assertRaises(BackendError) as error:
            self.state.atomic_accept('actor', 'copy', 'key', digest({'target': 2}), {'id': 'other'})
        self.assertEqual(error.exception.code, 'CONFLICT')

    def test_failed_accept_rolls_back_everything(self):
        self.state.connection.execute("CREATE TRIGGER reject_audit BEFORE INSERT ON audit BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with self.assertRaises(BackendError):
            self.state.atomic_accept('actor', 'copy', 'key', 'digest', {'id': 'one'})
        self.assertEqual(self.state.list('jobs'), [])
        self.assertIsNone(self.state.reconcile('actor', 'copy', 'key'))
        self.assertEqual(self.state.events(0), [])

    def test_read_only_and_full_fail_without_acceptance(self):
        self.state.connection.execute('PRAGMA query_only=ON')
        with self.assertRaises(BackendError) as error:
            self.state.put('intents', 'read-only', {'value': 1})
        self.assertEqual(error.exception.code, 'READ_ONLY')
        self.state.connection.execute('PRAGMA query_only=OFF')
        pages = self.state.connection.execute('PRAGMA page_count').fetchone()[0]
        self.state.connection.execute(f'PRAGMA max_page_count={pages}')
        with self.assertRaises(BackendError) as error:
            self.state.put('intents', 'full', {'value': 'x' * 500000})
        self.assertEqual(error.exception.code, 'INSUFFICIENT_SPACE')
        self.assertIsNone(self.state.get('intents', 'full'))

    def test_migration_checksum_and_newer_version_rejected(self):
        self.state.connection.execute("UPDATE migrations SET checksum='changed'")
        with self.assertRaises(BackendError) as error:
            State(self.path)
        self.assertEqual(error.exception.code, 'INTEGRITY_FAILED')
        self.state.connection.execute('INSERT INTO migrations VALUES(99,?,0)', ('future',))
        with self.assertRaises(BackendError) as error:
            State(self.path)
        self.assertEqual(error.exception.code, 'UNSUPPORTED')

    def test_exclusive_service_lock_and_release(self):
        second = State(self.path)
        try:
            self.state.acquire_service_lock()
            with self.assertRaises(BackendError) as error:
                second.acquire_service_lock()
            self.assertEqual(error.exception.code, 'BUSY')
            self.state.release_service_lock()
            second.acquire_service_lock()
            self.assertEqual(Path(str(self.path) + '.lock').stat().st_mode & 0o777, 0o600)
        finally:
            second.close()

    def test_corruption_and_malformed_document(self):
        corrupt = Path(self.tmp.name) / 'corrupt.sqlite'
        corrupt.write_bytes(b'not sqlite'); corrupt.chmod(0o600)
        with self.assertRaises(BackendError) as error:
            State(corrupt)
        self.assertEqual(error.exception.code, 'INTEGRITY_FAILED')
        with self.assertRaises(BackendError):
            self.state.put('jobs', 'nan', {'value': float('nan')})
        self.assertIsNone(self.state.get('jobs', 'nan'))


    def test_existing_exposed_symlink_and_hardlink_state_are_denied(self):
        import os
        exposed=Path(self.tmp.name)/'exposed.sqlite'; exposed.write_bytes(b''); exposed.chmod(0o644)
        with self.assertRaises(BackendError) as error: State(exposed)
        self.assertEqual(error.exception.code,'PERMISSION_DENIED')
        link=Path(self.tmp.name)/'linked.sqlite'; link.symlink_to(self.path)
        with self.assertRaises(BackendError): State(link)
        hard=Path(self.tmp.name)/'hard.sqlite'; os.link(self.path,hard)
        with self.assertRaises(BackendError): State(hard)


if __name__ == '__main__':
    unittest.main()
