#!/usr/bin/env python3
"""Activate an operator-configured Git disk; verify deployment settings before use."""
from deployment_settings import configured, install_template, validate_deployment
validate_deployment('GIT_MOUNT', 'GIT_UUID', 'RUNTIME_ROOT')
import argparse
import datetime
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import time
import urllib.request

REPO = Path(__file__).resolve().parents[1]
CONFIG = Path('/etc/archive-console/config.json')
BACKEND = Path(configured('@RUNTIME_ROOT@/archive-console-service/backend'))
DROPIN = Path('/etc/systemd/system/archive-console.service.d/git-volume.conf')
MOUNT = configured('@GIT_MOUNT@')
UUID = configured('@GIT_UUID@')


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Verify mounted identities and show the plan without changes')
    args = parser.parse_args()
    if not args.check and os.geteuid() != 0:
        sys.exit('Run with sudo: administrator access is required for service installation.')
    account = pwd.getpwnam('archive-console')
    config = json.loads(CONFIG.read_text())
    new = {'id':'git', 'label':'Git', 'role':'files',
           'rootPath':MOUNT+'/archive', 'mountPoint':MOUNT, 'filesystemUuid':UUID,
           'expectedFilesystem':'ext4', 'expectedDevice':[], 'expectedMountSource':''}
    existing = next((v for v in config['volumes'] if v['id'] == new['id']), None)
    if existing is None:
        config['volumes'].append(new)
    elif any(existing[k] != new[k] for k in ('rootPath','mountPoint','filesystemUuid')):
        sys.exit('Existing git-volume configuration conflicts; no changes made.')
    # Never trust changing /dev/sdX letters: verify the mounted device by UUID.
    for volume in config['volumes']:
        mounted = json.loads(run('findmnt','--json','--mountpoint',volume['mountPoint'],
                                '-o','TARGET,SOURCE,FSTYPE,UUID,MAJ:MIN',
                                capture_output=True).stdout)['filesystems'][0]
        if (mounted['uuid'] != volume['filesystemUuid'] or
                mounted['fstype'] != volume['expectedFilesystem']):
            sys.exit('Mounted identity mismatch: '+volume['id'])
        volume['expectedDevice'] = [int(n) for n in mounted['maj:min'].split(':')]
        volume['expectedMountSource'] = mounted['source']
    sys.path.insert(0, str(REPO/'apps/backend/src'))
    from archive_backend.config import parse_config
    parse_config(config)
    if args.check:
        print(json.dumps({'volumes':config['volumes'], 'restartRequired':True}, indent=2))
        return
    root = Path(new['rootPath'])
    if root.exists() or root.is_symlink():
        info = root.lstat()
        if root.is_symlink() or not root.is_dir() or info.st_uid != account.pw_uid or info.st_mode & 0o022:
            sys.exit('Existing managed root has unsafe ownership or permissions; no changes made.')
    else:
        root.mkdir(mode=0o700)
        os.chown(root, account.pw_uid, account.pw_gid)
    # Verify mounts and service permissions before touching the running service.
    probe = '''import json,sys,tempfile,os
from archive_backend.volumes import VolumeConfig,VolumeRegistry
for v in json.load(sys.stdin)['volumes']:
 c=VolumeConfig(v['id'],v['label'],v['rootPath'],v['mountPoint'],v['filesystemUuid'],v['expectedFilesystem'],tuple(v['expectedDevice']),v['expectedMountSource'],v.get('expectedMountId'),v.get('expectedMountRoot','/'))
 with VolumeRegistry([c]) as registry:
  capacity=registry.get(v['id']).capacity()
  assert not capacity['read_only'], v['id']+' is read-only'
  if v['id'] == 'git':
   with tempfile.TemporaryFile(dir=v['rootPath']) as test:
    test.write(b'Git volume check'); test.flush(); os.fsync(test.fileno()); test.seek(0)
    assert test.read() == b'Git volume check'
  print(v['id'],capacity['total'],'bytes: verified')
'''
    def verify(namespace=False):
        command = []
        if namespace:
            pid = run('systemctl','show','-p','MainPID','--value','archive-console.service',capture_output=True).stdout.strip()
            command = ['nsenter','--target',pid,'--mount','--']
        run(*command,'runuser','-u','archive-console','--','env',f'PYTHONPATH={BACKEND}',
            '/usr/bin/python3','-c',probe,input=json.dumps(config))
    verify()
    backup = Path('/var/backups/archive-console')/datetime.datetime.now().strftime('git-volume-%Y%m%d-%H%M%S-%f')
    backup.mkdir(parents=True,mode=0o700)
    targets = [(CONFIG,'config.json'),(DROPIN,'git-volume.conf')]
    for path,name in targets:
        if path.exists(): shutil.copy2(path,backup/name)
    print('Rollback files:',backup,flush=True)
    try:
        CONFIG.write_text(json.dumps(config,indent=2)+'\n')
        DROPIN.parent.mkdir(parents=True,exist_ok=True)
        DROPIN.write_text(configured('[Unit]\nRequiresMountsFor=@GIT_MOUNT@\n\n[Service]\nReadWritePaths=@GIT_MOUNT@\n'))
        run('systemctl','daemon-reload')
        run('systemctl','restart','archive-console.service')
        run('systemctl','is-active','--quiet','archive-console.service')
        verify(namespace=True)
        host = config['listen']['host']
        authority = f"[{host}]" if ':' in host else host
        endpoint = f"http://{authority}:{config['listen']['port']}/ready"
        deadline = time.monotonic() + 20
        while True:
            try:
                with urllib.request.urlopen(endpoint, timeout=2) as response:
                    if json.load(response).get('ready') is not True:
                        raise RuntimeError('Backend is not ready')
                break
            except (OSError, ValueError, RuntimeError):
                if time.monotonic() >= deadline:
                    raise RuntimeError('Backend readiness check failed')
                time.sleep(0.5)
    except BaseException:
        for path,name in targets:
            if (backup/name).exists(): shutil.copy2(backup/name,path)
            elif path == DROPIN: path.unlink(missing_ok=True)
        run('systemctl','daemon-reload')
        run('systemctl','restart','archive-console.service')
        raise
    print('Git activated. All configured volumes verified inside the service namespace.')


if __name__ == '__main__':
    main()
