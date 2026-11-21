import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from archive_backend.persistence import State
from archive_backend.security import Auth
from archive_backend.filesystem import ConfinedFilesystem,Entry
from archive_backend.volumes import VolumeRegistry
from archive_backend.catalog import Catalog
from archive_backend.contracts import BackendError

class ListingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='archive-listing-test-'); self.root=Path(self.temp.name); (self.root/'disk').mkdir()
        self.state=State(self.root/'state.db'); self.auth=Auth(self.state); self.owner=self.auth.bootstrap_owner('fixture-owner-'+('x'*40))
        self.registry=VolumeRegistry.fixtures({'files':self.root/'disk'}); self.fs=ConfinedFilesystem(self.registry.get('files'))
        self.catalog=Catalog(self.state,self.auth,{'files':self.fs}); self.addCleanup(self.temp.cleanup); self.addCleanup(self.state.close); self.addCleanup(self.registry.close)
    def query(self,**extra): return {'location':{'driveId':'files','path':''},'requestId':'q',**extra}
    def test_real_directory_unicode_stable_cursor(self):
        for name in ('猫','a','b'): self.fs.create_dir(name)
        first=self.catalog.list(self.owner,self.query(limit=1)); self.fs.create_dir('aa')
        second=self.catalog.list(self.owner,self.query(limit=1,cursor=first['nextCursor']))
        self.assertEqual(first['total'],3); self.assertEqual(second['entries'][0]['name'],'b')
        with self.assertRaises(BackendError): self.catalog.list(self.owner,self.query(limit=1,cursor=first['nextCursor'],sort='size'))
    def test_expiry_revocation_and_missing_volume(self):
        self.fs.create_dir('a'); self.fs.create_dir('b')
        first=self.catalog.list(self.owner,self.query(limit=1)); self.catalog.clock=lambda:10**20
        with self.assertRaises(BackendError): self.catalog.list(self.owner,self.query(cursor=first['nextCursor']))
        with self.assertRaises(BackendError): self.catalog.filesystem('missing')
    def test_100000_synthetic_entries_disk_backed_bounded_page(self):
        # Generated metadata is synthetic; real boundary tested separately in phase4.
        def generate(_):
            for i in range(100000): yield Entry(f'file{i:06}',f'file{i:06}','file',i,1000000000,i,1,i+1,1,0o600)
        with patch.object(self.fs,'iter_entries',generate):
            page=self.catalog.list(self.owner,self.query(limit=100))
        self.assertEqual(page['total'],100000); self.assertEqual(len(page['entries']),100)
        page2=self.catalog.list(self.owner,self.query(limit=100,cursor=page['nextCursor']))
        self.assertEqual(page2['entries'][0]['name'],'file000100')
