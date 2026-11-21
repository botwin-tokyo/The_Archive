import unittest
from archive_backend.contracts import CAPABILITIES,BackendError
from archive_backend.vault_contracts import schema,validate,parse_config
class VaultContracts(unittest.TestCase):
 def test_preserves_all_characters(self):
  for secret in ('  A_!\\\né👋  ','', '\u0000'):
   v={'requestId':'x','displayName':'app','login':' login ','password':secret};validate(v,schema('entries.create'));self.assertEqual(v['password'],secret)
 def test_bounds_and_unknowns(self):
  for value in ({'id':'x','oops':True},{'id':1}):
   with self.assertRaises(BackendError):validate(value,schema('entries.reveal'))
 def test_scope_is_explicit(self):self.assertEqual(CAPABILITIES['vault.entries.reveal'].scope,'vault.reveal')
