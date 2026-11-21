import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
import zipfile

ROOT=Path(__file__).resolve().parents[3]
PACKAGER=runpy.run_path(str(ROOT/'scripts/package-backend.py'))


class BackendPackageTests(unittest.TestCase):
    def test_reproducible_source_assets_and_legacy_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); first=PACKAGER['build'](root/'one'); second=PACKAGER['build'](root/'two')
            self.assertEqual(first['sha256'],second['sha256'])
            with tarfile.open(root/'one/archive-console-backend-source.tar.gz') as archive:
                names=archive.getnames()
                for name in ('backend/archive_backend/mobile_auth.py','backend/pyproject.toml','backend/requirements-mobile.txt','deploy/mobile/config.mobile.example.json','deploy/mobile/nginx.conf.example','contracts/mobile/mobile-api.schema.json','deploy/local.example.json','scripts/deployment_settings.py','package-manifest.json'):
                    self.assertIn(name,names)
                self.assertFalse(any(name.endswith(('.key','.pem','.sqlite','.whl','.pyc')) or '__pycache__' in name for name in names))
                metadata=tomllib.loads(archive.extractfile('backend/pyproject.toml').read().decode())
                self.assertEqual(metadata['project']['dependencies'],[]); self.assertEqual(metadata['tool']['setuptools']['packages']['find']['where'],['.'])
                original=tomllib.loads((ROOT/'apps/backend/pyproject.toml').read_text())
                self.assertEqual(metadata['project'],original['project'])
                manifest=json.load(archive.extractfile('package-manifest.json'))
                for item in manifest['files']:
                    data=archive.extractfile(item['path']).read(); self.assertEqual(len(data),item['bytes']); self.assertEqual(hashlib.sha256(data).hexdigest(),item['sha256'])
                config=json.load(archive.extractfile('config.example.json')); self.assertIsNone(config['statePath']); self.assertIsNone(config['listen']); self.assertEqual(config['volumes'],[])
            with zipfile.ZipFile(root/'one/archive-console-backend.pyz') as archive:
                self.assertTrue(all(name.startswith('archive_backend/') or name=='__main__.py' for name in archive.namelist()))
                self.assertFalse(any('cryptography/' in name or 'argon2/' in name for name in archive.namelist()))
    def test_zipapp_help_and_unconfigured_check_need_no_optional_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); PACKAGER['build'](root)
            config=root/'empty.json'; config.write_text((ROOT/'apps/backend/config.example.json').read_text())
            env=dict(os.environ); env.pop('PYTHONPATH',None)
            for args in (['--help'],['--config',str(config),'--check-config']):
                result=subprocess.run([sys.executable,'-S',str(root/'archive-console-backend.pyz'),*args],env=env,capture_output=True,text=True,timeout=20)
                self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(sorted(p.name for p in root.iterdir()),['archive-console-backend-source.tar.gz','archive-console-backend.pyz','backend-checksums.sha256','empty.json'])


if __name__=='__main__': unittest.main()
