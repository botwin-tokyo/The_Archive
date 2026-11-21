import unittest
from support import Fixture
from archive_backend.contracts import BackendError

class CatalogTests(unittest.TestCase):
    def setUp(self): self.f=Fixture(); self.addCleanup(self.f.close)
    def test_real_drive_listing_stat_unicode_and_snapshots(self):
        one=self.f.put('猫.bin'); self.f.put('z.bin'); self.f.put('a.bin')
        q={'requestId':'q','location':{'driveId':'files','path':''},'limit':1}
        page=self.f.call('files.list',{'query':q}); self.assertEqual(page['total'],3); self.assertEqual(page['entries'][0]['name'],'a.bin')
        self.f.put('b.bin'); q['cursor']=page['nextCursor']; page2=self.f.call('files.list',{'query':q}); self.assertEqual(page2['entries'][0]['name'],'z.bin')
        self.assertEqual(self.f.call('files.stat',{'ref':self.f.ref(one)})['name'],'猫.bin')
        self.assertFalse(self.f.call('drives.list')[0]['simulated'])
    def test_cursor_binding_and_expiry(self):
        self.f.put('one'); self.f.put('two')
        q={'requestId':'q','location':{'driveId':'files','path':''},'limit':1}; page=self.f.call('files.list',{'query':q})
        q.update(cursor=page['nextCursor'],sort='size')
        with self.assertRaises(BackendError): self.f.call('files.list',{'query':q})
    def test_scopes_and_revocation(self):
        self.f.service.filesystems['files'].create_dir('allowed'); self.f.put('allowed/a')
        agent=self.f.service.auth.register(self.f.owner,'Fixture','test-agent-'+('y'*40),['read'],[{'driveId':'files','path':'allowed'}])
        with self.assertRaises(BackendError): self.f.call('files.list',{'query':{'requestId':'q','location':{'driveId':'files','path':''}}},'test-agent-'+('y'*40))
        self.f.service.auth.revoke(self.f.owner,agent.id)
        with self.assertRaises(BackendError): self.f.call('status.get',token='test-agent-'+('y'*40))
