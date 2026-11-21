import tempfile
import unittest
from pathlib import Path
from archive_music_worker.server import Worker

class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.worker=Worker({'spool':self.temp.name})
    def test_invalid_task_and_arbitrary_download_url_rejected(self):
        for args in ({'op':'start','taskId':'../escape','action':'download','payload':{}},{'op':'start','taskId':'a'*32,'action':'download','payload':{'trackId':'http://127.0.0.1/secrets'}}):
            with self.assertRaises(ValueError):self.worker.call(args)
        self.assertEqual(list(Path(self.temp.name).iterdir()),[])
    def test_cleanup_cannot_escape_spool(self):
        with self.assertRaises(ValueError):self.worker.call({'op':'cleanup','taskId':'../../elsewhere'})
    def test_missing_task_is_explicit(self):
        self.assertEqual(self.worker.call({'op':'poll','taskId':'b'*32})['state'],'absent')
