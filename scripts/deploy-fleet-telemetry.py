#!/usr/bin/env python3
"""Install an operator-registered Archive fleet source using deployment settings.

Requires root and a private raw publisher credential file; never prints credentials.
Backs up changed config, modules, and persisted UI health settings before restarting.
"""
from deployment_settings import configured, install_template, validate_deployment
validate_deployment('FLEET_ENDPOINT', 'FLEET_NODE_ID', 'RUNTIME_ROOT')
import argparse
import json
import os
from pathlib import Path
import pwd
import shutil
import sqlite3
import subprocess
import time

parser=argparse.ArgumentParser()
parser.add_argument('--credential-source',required=True,type=Path)
args=parser.parse_args()
if os.geteuid()!=0:raise SystemExit('Run this deployment as root')
repo=Path(__file__).resolve().parents[1]
config_path=Path('/etc/archive-console/config.json')
base=Path(configured('@RUNTIME_ROOT@/archive-console-service'))
python=base/'mobile-venv/bin/python'
account=pwd.getpwnam('archive-console')
secret=args.credential_source.read_text().strip()
if not secret or len(secret)>4096 or any(ord(c)<=32 or ord(c)>=127 for c in secret):raise SystemExit('Invalid publisher credential source')
subprocess.run([str(python),'-m','pip','install','websockets==16.1.1'],check=True,stdout=subprocess.DEVNULL)
backup=Path('/var/backups/archive-console/fleet-'+time.strftime('%Y%m%d-%H%M%S'))
backup.mkdir(parents=True,mode=0o700)
config=json.loads(config_path.read_text())
shutil.copy2(config_path,backup/'config.json')
files=['telemetry.py','fleet_telemetry.py','application.py','config.py']
for name in files:
    old=base/'backend/archive_backend'/name
    if old.exists():shutil.copy2(old,backup/name)
secret_path=base/'state/fleet-publisher.token'
if secret_path.exists():shutil.copy2(secret_path,backup/'fleet-publisher.token')
subprocess.run(['systemctl','stop','archive-console.service'],check=True)
original_settings=None
try:
    with sqlite3.connect(config['statePath']) as db:
        row=db.execute("SELECT body FROM records WHERE namespace='configuration' AND key='current'").fetchone()
        if row:
            original_settings=row[0]
            (backup/'settings.json').write_text(original_settings)
            settings=json.loads(original_settings)
            settings['health']={'enabled':True,'endpoint':configured('@FLEET_ENDPOINT@'),'intervalSeconds':30}
            settings['revision']+=1;settings['restartRequired']=False
            db.execute("UPDATE records SET body=?,revision=revision+1,updated=? WHERE namespace='configuration' AND key='current'",(json.dumps(settings),time.time()))
    fd=os.open(secret_path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as f:f.write(secret+'\n');f.flush();os.fsync(f.fileno())
    os.chown(secret_path,account.pw_uid,account.pw_gid);os.chmod(secret_path,0o600)
    config['telemetry']={'enabled':True,'endpoint':configured('@FLEET_ENDPOINT@'),'intervalSeconds':30,'protocol':'fleet-v1','nodeId':configured('@FLEET_NODE_ID@'),'sourceId':'archive-console','manifestRevision':1,'spoolPath':str(base/'state/fleet/spool.sqlite3'),'secretFile':str(secret_path)}
    for name in files:shutil.copy2(repo/'apps/backend/src/archive_backend'/name,base/'backend/archive_backend'/name)
    temporary=config_path.with_suffix('.fleet.tmp');temporary.write_text(json.dumps(config,indent=2)+'\n');os.chmod(temporary,0o644);os.replace(temporary,config_path)
    subprocess.run(['systemctl','start','archive-console.service'],check=True)
except Exception:
    subprocess.run(['systemctl','stop','archive-console.service'],check=False)
    shutil.copy2(backup/'config.json',config_path)
    for name in files:
        if (backup/name).exists():shutil.copy2(backup/name,base/'backend/archive_backend'/name)
    if original_settings:
        with sqlite3.connect(config['statePath']) as db:db.execute("UPDATE records SET body=? WHERE namespace='configuration' AND key='current'",(original_settings,))
    if (backup/'fleet-publisher.token').exists():shutil.copy2(backup/'fleet-publisher.token',secret_path)
    subprocess.run(['systemctl','start','archive-console.service'],check=False)
    raise SystemExit('Deployment failed; previous core configuration restored') from None
print('Fleet telemetry deployed. Rollback files:',backup)
