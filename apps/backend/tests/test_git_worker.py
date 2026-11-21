import json,os,tempfile,unittest
from pathlib import Path
from archive_backend.git_worker import Worker
from archive_backend.git_provider import read_secret
from archive_backend.contracts import BackendError

class WorkerTests(unittest.TestCase):
    def test_restart_marks_unfinished_run_interrupted(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'state.json';p.write_text(json.dumps({'repositories':{},'run':{'id':'old','state':'running'},'history':[]}))
            w=Worker({'root':temp,'state':str(p)})
            self.assertEqual(w.status()['run']['state'],'interrupted')
            self.assertEqual(json.loads(p.read_text())['run']['state'],'interrupted')
    def test_credential_requires_private_regular_file(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/'token';p.write_text('fixture-'+('x'*40));p.chmod(0o600)
            self.assertEqual(len(read_secret(p)),48)
            p.chmod(0o644)
            with self.assertRaises(BackendError):read_secret(p)
            link=Path(temp)/'link';link.symlink_to(p)
            with self.assertRaises(OSError):read_secret(link)
