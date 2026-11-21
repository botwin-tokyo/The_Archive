import unittest
from support import Fixture
from archive_backend.contracts import BackendError


class AdditionalStorageTests(unittest.TestCase):
    def test_third_drive_listing_write_and_existing_agent_scope(self):
        f = Fixture(drive_ids=('files', 'backups', 'data'))
        self.addCleanup(f.close)
        self.assertEqual({d['id'] for d in f.call('drives.list')},
                         {'files', 'backups', 'data'})
        entry = f.put('third-drive.txt', b'third drive content', drive='data')
        self.assertEqual(entry['driveId'], 'data')
        for drive in ('files', 'backups'):
            page = f.call('files.list', {'query': {'requestId':drive,
                          'location': {'driveId':drive, 'path':''}}})
            self.assertEqual(page['total'], 0)
        token = 'existing-agent-' + 's'*40
        f.call('agents.register', {'label':'Existing scoped agent', 'secret':token,
               'scopes':['read'], 'allowedRoots':[{'driveId':'files', 'path':''}]})
        with self.assertRaises(BackendError):
            f.call('files.list', {'query': {'requestId':'denied',
                   'location': {'driveId':'data', 'path':''}}}, token=token)
