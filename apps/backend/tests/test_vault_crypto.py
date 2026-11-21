import unittest,os
from archive_backend.vault_crypto import seal,unseal
from archive_backend.contracts import BackendError
class CryptoTests(unittest.TestCase):
 def test_exact_and_tamper(self):
  k=os.urandom(32);v={'password':'  A_\\\n\x00é👋  '};a=seal(k,v,'vault:1');b=seal(k,v,'vault:1')
  self.assertNotEqual(a,b);self.assertEqual(unseal(k,a,'vault:1'),v)
  for key,blob,context in [(os.urandom(32),a,'vault:1'),(k,a,'other'),(k,a[:-4]+'AAAA','vault:1')]:
   with self.assertRaises(BackendError):unseal(key,blob,context)
