#!/usr/bin/env python3
"""Offline isolated install smoke from an explicit official-wheel fixture cache.

Does not enroll an account, create a key file, open a listener, or change live config.
The fresh virtual environment and extracted package exist only in a disposable tempdir.
"""
import argparse
import hashlib
import json
import os
import platform
from pathlib import Path,PurePosixPath
import subprocess
import sys
import tarfile
import tempfile
import venv

ROOT=Path(__file__).resolve().parents[1]


def run(command,env,cwd):
    result=subprocess.run(command,env=env,cwd=cwd,capture_output=True,text=True,timeout=120)
    if result.returncode: raise RuntimeError('Package smoke command failed: '+result.stderr[-1500:])
    return result.stdout


def verify(archive,wheelhouse):
    if sys.version_info[:2]!=(3,12) or sys.platform!='linux' or platform.machine() not in ('x86_64','AMD64'):
        raise ValueError('The bundled hash-locked smoke profile requires CPython 3.12 Linux x86_64')
    wheelhouse=wheelhouse.resolve(); archive=archive.resolve()
    if not wheelhouse.is_dir(): raise ValueError('Explicit official wheel cache required')
    with tempfile.TemporaryDirectory(prefix='archive-package-smoke-') as directory:
        root=Path(directory); extracted=root/'source'; extracted.mkdir()
        with tarfile.open(archive) as source:
            members=source.getmembers(); names={m.name for m in members}
            if len(names)!=len(members) or len(members)>10000 or sum(m.size for m in members)>32*1024**2: raise ValueError('Archive exceeds source-package bounds')
            for member in members:
                path=PurePosixPath(member.name)
                if path.is_absolute() or '..' in path.parts or not member.isfile(): raise ValueError('Unexpected archive entry')
                target=extracted.joinpath(*path.parts); target.parent.mkdir(parents=True,exist_ok=True)
                with target.open('xb') as output: output.write(source.extractfile(member).read())
        manifest=json.loads((extracted/'package-manifest.json').read_text())
        if {item['path'] for item in manifest['files']}!=names-{'package-manifest.json'}: raise ValueError('Manifest does not cover exact archive contents')
        for item in manifest['files']:
            data=(extracted/item['path']).read_bytes()
            if len(data)!=item['bytes'] or hashlib.sha256(data).hexdigest()!=item['sha256']: raise ValueError('Package manifest mismatch')
        venv.EnvBuilder(with_pip=True).create(root/'venv'); python=str(root/'venv/bin/python')
        env=dict(os.environ,PIP_DISABLE_PIP_VERSION_CHECK='1'); env.pop('PYTHONPATH',None)
        base=[python,'-m','pip','install','--no-index','--find-links',str(wheelhouse)]
        run([*base,'-r',str(extracted/'backend/requirements-build.txt')],env,root)
        run([*base,'--no-build-isolation','--no-deps',str(extracted/'backend')],env,root)
        run([python,'-I','-c',"import archive_backend.application, importlib.util; assert importlib.util.find_spec('argon2') is None; assert importlib.util.find_spec('cryptography') is None"],env,root)
        entry=str(root/'venv/bin/archive-console-service')
        run([entry,'--help'],env,root)
        run([entry,'--config',str(extracted/'config.example.json'),'--check-config'],env,root)
        run([*base,'--require-hashes','-r',str(extracted/'backend/requirements-mobile-linux-x86_64-py312.lock')],env,root)
        run([*base,'--no-build-isolation',str(extracted/'backend')+'[mobile]'],env,root)
        run([entry,'--config',str(extracted/'deploy/mobile/config.mobile.example.json'),'--check-config'],env,root)
        code="""import importlib.metadata as m,json
from argon2 import PasswordHasher
from cryptography.fernet import Fernet
from cryptography.hazmat.backends.openssl.backend import backend
import archive_backend.mobile_boundary,archive_backend.mobile_http,archive_backend.mobile_enrollment
h=PasswordHasher(); v=h.hash('synthetic package verification only'); assert h.verify(v,'synthetic package verification only')
f=Fernet(Fernet.generate_key()); assert f.decrypt(f.encrypt(b'synthetic package verification'))==b'synthetic package verification'
print(json.dumps({'versions':{n:m.version(n) for n in ['argon2-cffi','argon2-cffi-bindings','cryptography','cffi','pycparser']},'openssl':backend.openssl_version_text()}))
"""
        details=json.loads(run([python,'-I','-c',code],env,root))
        if list(root.rglob('*.sqlite')) or list(root.rglob('*.key')): raise ValueError('Smoke unexpectedly created persistent service state or key')
        return {'schemaVersion':1,'success':True,'isolatedVenv':True,'offlineWheelCache':True,'legacyWithoutMobileDependencies':True,
                'optionalMobileInstall':True,'syntheticArgon2AndFernet':True,'help':True,'unconfiguredCheck':True,'placeholderMobileCheck':True,
                'enrollmentPerformed':False,'listenerStarted':False,'sourceArchiveSha256':hashlib.sha256(archive.read_bytes()).hexdigest(),**details}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',type=Path,default=ROOT/'release/archive-console-backend-source.tar.gz')
    parser.add_argument('--wheelhouse',type=Path,required=True)
    args=parser.parse_args(); print(json.dumps(verify(args.archive,args.wheelhouse),sort_keys=True))


if __name__=='__main__': main()
