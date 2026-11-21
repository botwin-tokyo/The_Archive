import unittest
from archive_backend.config import parse_config
from archive_backend.git_config import settings, DEFAULTS
from archive_backend.contracts import BackendError

class GitConfigurationTests(unittest.TestCase):
    def test_optional_and_strict(self):
        self.assertIsNone(parse_config({}).git_backup)
        cfg={'socket':'/run/archive-git/worker.sock','workerUid':999,'spool':'/mnt/git/spool','controlSocket':'/run/archive-console/git.sock','volumeId':'git'}
        self.assertEqual(parse_config({'gitBackup':cfg}).git_backup,cfg)
        for patch in ({'workerUid':0},{'spool':'/mnt/../etc'},{'token':'secret'}):
            with self.assertRaises(BackendError):parse_config({'gitBackup':dict(cfg,**patch)})
    def test_settings(self):
        self.assertFalse(settings(DEFAULTS)['enabled'])
        for patch in ({'scope':'everything'},{'retention':0},{'branchOverrides':{'1':'-bad'}},{'timezone':'Invalid/Zone'}):
            with self.assertRaises(BackendError):settings(dict(DEFAULTS,**patch))
