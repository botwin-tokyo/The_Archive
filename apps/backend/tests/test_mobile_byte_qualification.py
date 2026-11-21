"""Small deterministic checks for the opt-in >4 GiB qualification harness."""
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[3]
GENERATOR=runpy.run_path(str(ROOT/'scripts/create-mobile-byte-fixture.py'))


class MobileByteQualificationTests(unittest.TestCase):
    def test_small_fixture_digest_allocation_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'new.bin'; result=GENERATOR['create'](path,16391)
            self.assertEqual(result['logicalBytesRead'],'16391')
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),result['sha256'])
            self.assertEqual(path.stat().st_mode & 0o777,0o400)
            with self.assertRaises(FileExistsError): GENERATOR['create'](path,1)
            self.assertEqual(path.stat().st_size,16391)
    def test_large_markers_cover_integer_boundaries_without_allocating(self):
        positions=[offset for offset,_ in GENERATOR['markers'](2**32+123)]
        self.assertIn(2**31-2048,positions); self.assertIn(2**31+17,positions)
        self.assertIn(2**32-2048,positions); self.assertIn(2**32+17,positions)
    def test_small_real_http_harness_does_not_claim_large_or_native_qualification(self):
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory); output=work/'result.json'; env=dict(os.environ,TMPDIR=str(work))
            result=subprocess.run([sys.executable,str(ROOT/'scripts/qualify-mobile-large-bytes.py'),'--work-dir',str(work),'--evidence',str(output),'--bytes',str(8*1024**2+123),'--reserve-bytes',str(64*1024**2)],env=env,capture_output=True,text=True,timeout=60)
            self.assertEqual(result.returncode,0,result.stdout[-1000:]+result.stderr[-1000:])
            evidence=json.loads(output.read_text()); self.assertTrue(evidence['success'])
            self.assertFalse(evidence['greaterThan4GiB']); self.assertFalse(evidence['tlsQualified']); self.assertFalse(evidence['nativeBackgroundQualified'])
            self.assertEqual([step['sha256'] for step in evidence['steps']],[evidence['input']['sha256']]*2)
            self.assertTrue(all(step['cleanupState']=='confirmed' for step in evidence['steps']))
            self.assertEqual([p.name for p in work.iterdir()],['result.json'])


if __name__=='__main__': unittest.main()
