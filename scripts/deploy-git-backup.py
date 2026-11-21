#!/usr/bin/python3
"""Install the verified GitHub worker/core integration without formatting disks."""
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

REPO=Path(__file__).resolve().parents[1]
INSTALLED=Path(configured('@RUNTIME_ROOT@/archive-console-service'))
CONFIG=Path('/etc/archive-console/config.json')
UUID=configured('@GIT_UUID@')
ROOT=Path(configured('@GIT_MOUNT@/github-backup'))
WORK=Path(configured('@RUNTIME_ROOT@/archive-console-git'))

def run(*args,**kw):return subprocess.run(args,check=True,text=True,**kw)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true');parser.add_argument('--credential-source');args=parser.parse_args()
    config=json.loads(CONFIG.read_text())
    mount=json.loads(run('findmnt','--json','--mountpoint',configured('@GIT_MOUNT@'),'-o','UUID,SOURCE,FSTYPE,MAJ:MIN',capture_output=True).stdout)['filesystems'][0]
    assert mount['uuid']==UUID and mount['fstype']=='ext4','Git mount identity mismatch'
    assert any(v['id']=='git' and v['filesystemUuid']==UUID and v['rootPath']==configured('@GIT_MOUNT@/archive') for v in config['volumes'])
    if args.check:
        print('Verified Git UUID, configured volume and installation paths. No disk formatting. Worker + core + timer templates ready.');return
    if os.geteuid()!=0:sys.exit('Administrator access required')
    if not args.credential_source:sys.exit('A private credential source file is required')
    src=Path(args.credential_source);info=src.lstat()
    import stat
    assert stat.S_ISREG(info.st_mode) and not info.st_mode&0o077,'Credential source must be private and regular'
    credential=src.read_text().strip();assert 20<=len(credential)<=2048 and not any(c.isspace() for c in credential)
    try:worker=pwd.getpwnam('archive-git')
    except KeyError:
        run('useradd','--system','--user-group','--home-dir',str(WORK),'--shell','/usr/sbin/nologin','archive-git');worker=pwd.getpwnam('archive-git')
    core=pwd.getpwnam('archive-console')
    for path in (ROOT,ROOT/'repositories',ROOT/'generations',WORK):
        if path.exists():
            st=path.lstat();assert stat.S_ISDIR(st.st_mode) and st.st_uid==worker.pw_uid and not st.st_mode&0o022,'Unsafe existing worker directory'
        else:path.mkdir(mode=0o750);os.chown(path,worker.pw_uid,worker.pw_gid)
    backup=Path('/var/backups/archive-console')/('git-backup-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    backup.mkdir(mode=0o700)
    targets=[CONFIG,Path('/etc/archive-console/git-worker.json'),Path('/etc/systemd/system/archive-console.service.d/git-backup.conf')]
    targets += [Path('/etc/systemd/system')/name for name in ('archive-console-git-worker.service','archive-console-git-backup.service','archive-console-git-backup.timer')]
    originals={}
    for i,path in enumerate(targets):
        if path.exists():saved=backup/str(i);shutil.copy2(path,saved);originals[path]=saved
        else:originals[path]=None
    shutil.copytree(INSTALLED/'backend/archive_backend',backup/'archive_backend')
    secret=WORK/'github.token'
    # Do not print or persist the provisioning source in repository configuration.
    fd=os.open(secret,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:f.write(credential+'\n');f.flush();os.fsync(f.fileno())
    os.chown(secret,worker.pw_uid,worker.pw_gid);os.chmod(secret,0o600)
    worker_config={'root':str(ROOT),'state':str(WORK/'state.json'),'socket':'/run/archive-git/worker.sock','secret':str(secret),'ownerUid':core.pw_uid,
                   'reserveBytes':10*1024**3,'volume':{'id':'git-worker','mountPoint':configured('@GIT_MOUNT@'),'filesystemUuid':UUID,'expectedDevice':[int(n) for n in mount['maj:min'].split(':')],'expectedMountSource':mount['source']}}
    config['gitBackup']={'socket':'/run/archive-git/worker.sock','workerUid':worker.pw_uid,'spool':str(ROOT/'generations'),'controlSocket':'/run/archive-console/git-control.sock','volumeId':'git'}
    sys.path.insert(0,str(REPO/'apps/backend/src'))
    from archive_backend.config import parse_config
    parse_config(config)
    try:
        for file in (REPO/'apps/backend/src/archive_backend').glob('*.py'):
            dest=INSTALLED/'backend/archive_backend'/file.name;shutil.copy2(file,dest);os.chmod(dest,0o755 if file.name=='git_askpass.py' else 0o644)
        shutil.copy2(REPO/'scripts/trigger-git-backup.py',INSTALLED/'trigger-git-backup.py')
        Path('/etc/archive-console/git-worker.json').write_text(json.dumps(worker_config,indent=2)+'\n')
        CONFIG.write_text(json.dumps(config,indent=2)+'\n')
        drop=Path('/etc/systemd/system/archive-console.service.d/git-backup.conf');drop.parent.mkdir(exist_ok=True)
        drop.write_text('[Unit]\nWants=archive-console-git-worker.service\nAfter=archive-console-git-worker.service\n[Service]\nSupplementaryGroups=archive-git\nRuntimeDirectory=archive-console\nRuntimeDirectoryMode=0750\n')
        for path in targets[3:]:install_template(REPO/'deploy/systemd'/path.name,path)
        run('systemctl','daemon-reload');run('systemctl','enable','--now','archive-console-git-worker.service');run('systemctl','restart','archive-console.service')
        for attempt in range(30):
            try:
                with urllib.request.urlopen('http://127.0.0.1:'+str(config['listen']['port'])+'/ready',timeout=2) as r:
                    if json.load(r).get('ready'):break
            except OSError:pass
            time.sleep(1)
        else:raise RuntimeError('Backend readiness failed')
        run('systemctl','is-active','--quiet','archive-console-git-worker.service')
    except BaseException:
        run('systemctl','stop','archive-console-git-worker.service')
        for path,saved in originals.items():
            if saved:shutil.copy2(saved,path)
            else:path.unlink(missing_ok=True)
        shutil.rmtree(INSTALLED/'backend/archive_backend');shutil.copytree(backup/'archive_backend',INSTALLED/'backend/archive_backend')
        run('systemctl','daemon-reload');run('systemctl','restart','archive-console.service');raise
    print('Git worker and coordinator installed; timer not enabled until initial backup qualification. Rollback:',backup)

if __name__=='__main__':main()
