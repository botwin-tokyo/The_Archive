"""Install reviewed core backend modules and restart archive-console.service once.

Run as root: sudo python3 scripts/deploy-core.py --install-local [--confirm-core-restart]
Workers (music, video, anime, Git) are not restarted; durable core jobs resume after the
restart. The live SQLite database is backed up with the SQLite backup API and never restored.
"""
from deployment_settings import configured, install_template, validate_deployment
validate_deployment('RUNTIME_ROOT')
import argparse,filecmp,json,os,pathlib,shutil,sqlite3,subprocess,time,urllib.request

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--install-local',action='store_true',required=True)
parser.add_argument('--confirm-core-restart',action='store_true',help='Proceed even though jobs are active; they resume after the restart')
args=parser.parse_args()
if os.geteuid()!=0: raise SystemExit('Run as root: sudo python3 scripts/deploy-core.py --install-local')
repo=pathlib.Path(__file__).resolve().parents[1]
service_root=pathlib.Path(configured('@RUNTIME_ROOT@/archive-console-service'))
source=repo/'apps/backend/src/archive_backend'; installed=service_root/'backend/archive_backend'
database=service_root/'state/archive.sqlite3'; config=pathlib.Path('/etc/archive-console/config.json')

changed=sorted(n for n in os.listdir(source) if n.endswith('.py') and (not (installed/n).exists() or not filecmp.cmp(source/n,installed/n,shallow=False)))
print('Core modules to install:',', '.join(changed) or 'none')
if not changed: raise SystemExit('Installed core already matches the repository; nothing to do.')
connection=sqlite3.connect(f'file:{database}?mode=ro',uri=True)
try:
    active={ns:connection.execute("SELECT COUNT(*) FROM records WHERE namespace=? AND json_extract(body,'$.state') NOT IN ('completed','partial','failed','canceled','paused')",(ns,)).fetchone()[0]
            for ns in ('jobs','music_jobs','video_jobs','anime_jobs')}
finally: connection.close()
print('Active jobs before restart:',active)
if any(active.values()) and not args.confirm_core_restart:
    raise SystemExit('Jobs are active. They resume after the core restarts; rerun with --confirm-core-restart to proceed.')

backup=service_root/('pre-core-'+time.strftime('%Y%m%d-%H%M%S')); backup.mkdir(mode=0o700)
shutil.copy2(config,backup/'config.json'); shutil.copytree(service_root/'backend',backup/'backend')
src=sqlite3.connect(str(database)); dst=sqlite3.connect(str(backup/'archive.sqlite3')); src.backup(dst); dst.close(); src.close(); (backup/'archive.sqlite3').chmod(0o600)
try:
    subprocess.run(['systemctl','stop','archive-console.service'],check=True)
    shutil.copytree(source,installed,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__'))
    subprocess.run(['systemctl','start','archive-console.service'],check=True)
    listen=json.loads(config.read_text()).get('listen') or {}
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"http://{listen.get('host','127.0.0.1')}:{listen.get('port',18743)}/ready",timeout=2) as response:
                if json.loads(response.read()).get('ready'): break
        except OSError: pass
        time.sleep(1)
    else: raise RuntimeError('Core did not report ready')
except Exception:
    # Keep the current database. Restore runnable code only.
    shutil.copytree(backup/'backend',service_root/'backend',dirs_exist_ok=True)
    subprocess.run(['systemctl','restart','archive-console.service'],check=False)
    raise
print(json.dumps({'installed':changed,'coreBackup':backup.name,'activeJobsBefore':active}))
