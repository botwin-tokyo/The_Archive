import unittest
from archive_backend.phone_transfer_contracts import prepare, parse_config, PhoneError
from archive_backend.contracts import BackendError
from support import Fixture

class ContractTests(unittest.TestCase):
 def test_defaults_exact_text_and_bounds(self):
  raw={'requestId':'11111111-1111-4111-8111-111111111111','inboxId':'phone-primary','title':' Test ','message':'  Ω\n '}
  result=prepare(raw);self.assertEqual(result['message'],raw['message']);self.assertEqual(result['retentionDays'],30)
  for changes in ({'title':' '},{'message':'x'*32769},{'links':[{'label':'x','url':'file:///etc/passwd'}]},{'links':[{'label':'x','url':'https://user:pass@example.com'}]}):
   with self.assertRaises(BackendError):prepare(dict(raw,**changes))
 def test_independent_root_and_explicit_grants(self):
  volumes=[{'id':'files','rootPath':'/mnt/data/archive','mountPoint':'/mnt/data'}]
  cfg={'enabled':True,'volumeId':'files','root':'/mnt/data/PhoneTransferHold','grants':[{'principalId':'agent','inboxId':'phone-primary'}]}
  self.assertEqual(parse_config(cfg,volumes)['quotaBytes'],10737418240)
  for root in ('/tmp/hold','/mnt/data','/mnt/data/archive/hold'):
   with self.assertRaises(PhoneError):parse_config(dict(cfg,root=root),volumes)
 def test_phone_scope_does_not_grant_vault_or_files(self):
  f=Fixture()
  try:
   p=f.service.auth.register(f.owner,'phone','phone-test-'+('x'*40),['phone_transfer.send'],[])
   f.service.auth.authorize(p,'phone_transfer.send')
   for action in ('vault.read','read','phone_transfer.read'):
    with self.assertRaises(BackendError):f.service.auth.authorize(p,action)
  finally:f.close()
