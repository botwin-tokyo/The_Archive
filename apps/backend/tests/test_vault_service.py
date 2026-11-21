import unittest
import test_vault_store as stores
from support import Fixture
from archive_backend.vault_service import VaultService
from archive_backend.contracts import BackendError
class ServiceTests(unittest.TestCase):
 def setUp(self):
  stores.StoreTests.setUp(self);self.f=Fixture();v=VaultService(self.f.service,None);v.store=self.store;v.config={'enabled':True};self.f.service.vault=v
 def open(self):return stores.StoreTests.open(self)
 def tearDown(self):self.f.service.vault.store=None;self.f.close();stores.StoreTests.tearDown(self)
 def call(self,op,a=None,t=None):return self.f.call('vault.'+op,a or {},t) if t else self.f.call('vault.'+op,a or {})
 def create(self):return self.call('entries.create',{'requestId':'create','displayName':'App','login':' login ','password':'  \nÉ_\\\x00 secret👋 '})
 def test_roundtrip_history_idempotency(self):
  e=self.create();self.assertEqual(e,self.create());self.assertNotIn('password',e)
  r=self.call('entries.reveal',{'id':e['id']});self.assertEqual(r['password'],'  \nÉ_\\\x00 secret👋 ')
  self.call('entries.update',{'requestId':'edit','id':e['id'],'revision':1,'password':'changed'})
  self.assertEqual(self.call('entries.reveal',{'id':e['id'],'version':1})['password'],r['password'])
  with self.assertRaises(BackendError):self.call('entries.update',{'requestId':'stale','id':e['id'],'revision':1,'password':'oops'})
  self.assertNotIn('password',str(self.call('entries.history',{'id':e['id']})))
 def test_scope_and_revocation(self):
  e=self.create();token='vault-test-'+('x'*40)
  a=self.f.service.auth.register(self.f.owner,'list only',token,['vault.read'],[])
  self.assertEqual(len(self.call('entries.list',t=token)['items']),1)
  with self.assertRaises(BackendError):self.call('entries.reveal',{'id':e['id']},token)
  self.f.service.auth.revoke(self.f.owner,a.id)
  with self.assertRaises(BackendError):self.call('entries.list',t=token)
 def test_export_restore_no_overwrite(self):
  e=self.create();b=self.call('backup.export');r=self.call('backup.import',{'requestId':'restore','archive':b['archive']})
  self.assertEqual(r['importedCount'],1);self.assertNotEqual(r['ids'][0],e['id'])
  self.assertEqual(self.call('entries.reveal',{'id':e['id']})['password'],self.call('entries.reveal',{'id':r['ids'][0]})['password'])
 def test_trash_restore_and_no_plaintext_audit(self):
  e=self.create();self.call('entries.trash',{'requestId':'trash','id':e['id'],'revision':1});self.assertEqual(self.call('entries.list')['items'],[])
  self.call('entries.restore',{'requestId':'restore','id':e['id'],'revision':2});self.assertEqual(len(self.call('entries.list')['items']),1)
  self.assertNotIn('secret👋',str(self.f.state.connection.execute('SELECT * FROM audit').fetchall()))
