import json,tempfile,unittest
from pathlib import Path
from archive_backend.vault_crypto import new_key_record
from archive_backend.vault_store import VaultStore
from archive_backend.contracts import BackendError
class StoreTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
  key=self.root/'keys';key.write_text(json.dumps(new_key_record()));key.chmod(0o600)
  replicas=[]
  for n in ('a','b'):
   p=self.root/n;p.mkdir(mode=0o700);replicas.append({'path':str(p),'mountPoint':str(p),'filesystemUuid':n})
  self.config={'keyFile':str(key),'anchor':str(self.root/'anchor'),'replicas':replicas}
  self.missing=set();self.store=self.open();self.store.provision()
 def open(self):
  def check(r):
   if r['filesystemUuid'] in self.missing:raise OSError('missing')
   return r['filesystemUuid']
  return VaultStore(self.config,mount_check=check)
 def tearDown(self):self.store.close();self.temp.cleanup()
 def test_mirrored_and_private(self):
  secret='CANARY  secret_é\n'
  self.store.mutate(lambda v:(v['entries'].update({'x':secret}) or True))
  self.assertEqual(self.store.read()[0]['entries']['x'],secret)
  self.assertEqual((self.root/'a/1.snapshot').read_bytes(),(self.root/'b/1.snapshot').read_bytes())
  for p in self.root.rglob('*'):
   if p.is_file():self.assertNotIn(secret.encode(),p.read_bytes())
 def test_degraded_rejects_write(self):
  self.missing.add('a');self.assertEqual(self.store.read()[1]['state'],'degraded')
  with self.assertRaises(BackendError):self.store.mutate(lambda v:True)
 def test_fault_boundaries(self):
  for point in ('prepare0','prepare1','commit0','commit1','anchor'):
   before=self.store.read()[0]['sequence']
   def fault(p):
    if p==point:raise OSError('crash')
   self.store.fault=fault
   with self.assertRaises(BackendError):self.store.mutate(lambda v:(v['entries'].update({point:'secret'}) or True))
   self.store.close();self.store=self.open();value,health=self.store.read()
   self.assertEqual(value['sequence'],before+(point!='prepare0'))
   self.assertEqual(health['state'],'healthy')
 def test_single_writer_and_tamper(self):
  with self.assertRaises(BackendError):self.open()
  (self.root/'a/0.snapshot').write_bytes(b'bad')
  with self.assertRaises(BackendError):self.store.read()
 def test_disk_budget_rejects_before_commit(self):
  from unittest.mock import patch
  with patch('archive_backend.vault_store.BUDGET',1):
   with self.assertRaises(BackendError):self.store.mutate(lambda v:True)
  self.assertEqual(self.store.read()[0]['sequence'],0)
 def test_stale_copy_not_silently_overwritten(self):
  self.store.mutate(lambda v:True)
  (self.root/'b/1.snapshot').unlink()
  with self.assertRaises(OSError):self.store.read()
 def test_isolated_export_restore(self):
  from archive_backend.vault_crypto import seal,unseal
  self.store.mutate(lambda v:(v['entries'].update({'test':{'password':'restore-canary'}}) or True))
  data,_=self.store.read();archive=seal(self.store.key,data,self.store.context('export'))
  # Independent empty vault with original recovery key but distinct paths/anchor.
  other=Path(self.temp.name)/'isolated';other.mkdir(mode=0o700)
  config=dict(self.config,anchor=str(other/'anchor'),replicas=[])
  for n in ('c','d'):
   path=other/n;path.mkdir(mode=0o700);config['replicas'].append({'path':str(path),'filesystemUuid':n})
  restored=VaultStore(config,mount_check=lambda r:r['filesystemUuid'])
  try:
   restored.provision();decoded=unseal(restored.key,archive,restored.context('export'))
   restored.mutate(lambda v:(v.update(entries=decoded['entries']) or True))
   self.assertEqual(restored.read()[0]['entries'],data['entries'])
  finally:restored.close()
