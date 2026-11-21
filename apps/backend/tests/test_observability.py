import json
import threading
import time
import unittest
from archive_backend.observability import Observability, aggregate_health, redact


class ObservabilityTests(unittest.TestCase):
    def test_allowlist_drops_secret_paths_and_headers(self):
        value = redact({'code': 'CONFLICT', 'message': 'token=SECRET /private/file', 'authorization': 'Bearer SECRET', 'path': '/private/file', 'correlationId': 'SECRET'})
        self.assertNotIn('SECRET', json.dumps(value))
        self.assertNotIn('/private', json.dumps(value))
        report = aggregate_health({'api': 'ready', 'mcp': 'ready', 'token': 'SECRET'}, [{'role': 'files', 'state': 'online', 'label': 'private name', 'path': '/secret', 'capacityBytes': None, 'freeBytes': None}], [{'state': 'running', 'label': 'secret file'}])
        self.assertNotIn('secret', json.dumps(report))
        self.assertIsNone(report['drives'][0]['capacityBytes'])
        self.assertEqual(report['jobs'], {'running': 1})

    def test_stalled_sink_is_nonblocking_and_bounded(self):
        block = threading.Event()
        entered = threading.Event()
        def stalled(_):
            entered.set()
            block.wait(2)
        diagnostics = Observability(limit=3, sink=stalled)
        diagnostics.record('OK')
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        for _ in range(30):
            diagnostics.record('UNAVAILABLE', password='SECRET')
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertLessEqual(len(diagnostics._records), 3)
        self.assertGreater(diagnostics.counters()['sink_dropped'], 0)
        block.set()
        diagnostics.close()

    def test_failing_sink_does_not_recurse(self):
        def failed(_):
            raise OSError('secret path full disk')
        diagnostics = Observability(sink=failed)
        diagnostics.record('FULL_DISK')
        deadline = time.monotonic() + 1
        while 'sink_errors' not in diagnostics.counters() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(diagnostics.counters()['sink_errors'], 1)
        self.assertEqual(len(diagnostics._records), 1)
        diagnostics.close()


if __name__ == '__main__':
    unittest.main()
