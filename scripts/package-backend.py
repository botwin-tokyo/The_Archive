"""Deterministic architecture-neutral source archives; never install or start them."""
from pathlib import Path
import argparse
import gzip
import hashlib
import io
import json
import os
import tarfile
import time
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def source_files():
    package=ROOT/'apps/backend/src/archive_backend'
    modules={p.relative_to(package).as_posix():p.read_bytes() for p in sorted(package.rglob('*.py')) if not p.is_symlink()}
    files={'backend/archive_backend/'+name:data for name,data in modules.items()}
    project=(ROOT/'apps/backend/pyproject.toml').read_text()
    if project.count('where = ["src"]')!=1: raise ValueError('Review source-package discovery layout before packaging')
    files['backend/pyproject.toml']=project.replace('where = ["src"]','where = ["."]\ninclude = ["archive_backend*"]').encode()
    for name in ('requirements-mobile.txt','requirements-build.txt','requirements-mobile-linux-x86_64-py312.lock'):
        files['backend/'+name]=(ROOT/'apps/backend'/name).read_bytes()
    files['config.example.json']=(ROOT/'apps/backend/config.example.json').read_bytes()
    for name in ('deploy/local.example.json', 'scripts/deployment_settings.py'):
        files[name]=(ROOT/name).read_bytes()
    for directory in ('deploy/systemd','deploy/mobile','contracts/mobile'):
        for path in sorted((ROOT/directory).rglob('*')):
            if path.is_symlink(): raise ValueError('Package assets must not be symlinks')
            if path.is_file() and path.suffix in ('.md','.json','.example','.service'):
                files[path.relative_to(ROOT).as_posix()]=path.read_bytes()
    return modules,files


def build(output,epoch=0):
    if not 0<=epoch<=2**31-1: raise ValueError('SOURCE_DATE_EPOCH must fit the supported archive range')
    output=Path(output); output.mkdir(parents=True,exist_ok=True)
    modules,files=source_files()
    manifest={'schemaVersion':1,'sourceDateEpoch':epoch,'runtimeDependenciesBundled':False,
              'files':[{'path':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()} for name,data in sorted(files.items())]}
    files['package-manifest.json']=(json.dumps(manifest,sort_keys=True,indent=2)+'\n').encode()
    pyz=output/'archive-console-backend.pyz'
    with pyz.open('wb') as target:
        target.write(b'#!/usr/bin/env python3\n')
        with zipfile.ZipFile(target,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
            values={'archive_backend/'+name:data for name,data in modules.items()}
            values['__main__.py']=b'from archive_backend.bootstrap import main\nmain()\n'
            for name,data in sorted(values.items()):
                info=zipfile.ZipInfo(name,time.gmtime(max(epoch,315532800))[:6]); info.create_system=3
                info.external_attr=0o100644<<16; info.compress_type=zipfile.ZIP_DEFLATED
                archive.writestr(info,data,compress_type=zipfile.ZIP_DEFLATED,compresslevel=9)
    pyz.chmod(0o755)
    source=output/'archive-console-backend-source.tar.gz'
    with source.open('wb') as target:
        with gzip.GzipFile(filename='',mode='wb',fileobj=target,mtime=epoch,compresslevel=9) as compressed:
            with tarfile.open(fileobj=compressed,mode='w',format=tarfile.PAX_FORMAT) as archive:
                for name,data in sorted(files.items()):
                    info=tarfile.TarInfo(name); info.size=len(data); info.mode=0o644; info.mtime=epoch
                    info.uid=info.gid=0; info.uname=info.gname=''
                    archive.addfile(info,io.BytesIO(data))
    checksums={path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in (pyz,source)}
    (output/'backend-checksums.sha256').write_text(''.join(f'{digest}  {name}\n' for name,digest in sorted(checksums.items())))
    return {'schemaVersion':1,'outputDirectory':str(output),'files':len(files),'sourceDateEpoch':epoch,'sha256':checksums}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,default=ROOT/'release')
    args=parser.parse_args(); print(json.dumps(build(args.output_dir,int(os.environ.get('SOURCE_DATE_EPOCH','0'))),sort_keys=True))


if __name__=='__main__': main()
