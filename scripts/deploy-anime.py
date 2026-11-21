"""Install the reviewed local anime worker and core. Run as root with --install-local.

Standard library only: no dependency
download or wheel cache is used. FFmpeg/ffprobe must already be installed from the OS.
Never overwrites the live SQLite database; core code/config are backed up first.
"""
from deployment_settings import configured, install_template, validate_deployment
validate_deployment('BACKUPS_MOUNT', 'FILES_MOUNT', 'RUNTIME_ROOT')
import argparse,filecmp,grp,json,os,pathlib,pwd,shutil,sqlite3,subprocess,sys,time

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--install-local',action='store_true',required=True)
parser.add_argument('--default-destination',metavar='DRIVE:PATH',help='Existing File Storage folder the Anime page offers by default, e.g. data:Anime')
parser.add_argument('--confirm-core-restart',action='store_true',help='Proceed even though other jobs are active; the core resumes durable work after restart')
args=parser.parse_args()
default=None
if args.default_destination:
    drive,_,folder=args.default_destination.partition(':')
    if not drive or folder.startswith('/') or '..' in folder.split('/'): raise SystemExit('Use DRIVE:PATH with a logical path, e.g. data:Anime')
    default={'driveId':drive,'path':folder.strip('/')}
if os.geteuid()!=0: raise SystemExit('Run as root: sudo python3 scripts/deploy-anime.py --install-local')
os.umask(0o022)
repo=pathlib.Path(__file__).resolve().parents[1]
root=pathlib.Path(configured('@RUNTIME_ROOT@/archive-console-anime'))
service_root=pathlib.Path(configured('@RUNTIME_ROOT@/archive-console-service'))
core_config=pathlib.Path('/etc/archive-console/config.json')
installed=service_root/'backend/archive_backend'
database=service_root/'state/archive.sqlite3'
def run(*command): subprocess.run(command,check=True)

# Preflight ---------------------------------------------------------------------
for binary in ('/usr/bin/ffmpeg','/usr/bin/ffprobe','/usr/bin/python3','/usr/sbin/nft'):
    if not os.access(binary,os.X_OK): raise SystemExit(f'Missing required executable {binary}')
source=repo/'apps/backend/src/archive_backend'
changed=sorted(n for n in os.listdir(source) if n.endswith('.py') and (not (installed/n).exists() or not filecmp.cmp(source/n,installed/n,shallow=False)))
unrelated=[n for n in changed if not n.startswith('anime_') and n not in ('application.py','config.py','contracts.py','mcp_adapter.py')]
print('Core modules to install:',', '.join(changed) or 'none')
if unrelated: print('NOTE: non-anime modules differ from the installed core:',', '.join(unrelated))
connection=sqlite3.connect(f'file:{database}?mode=ro',uri=True)
try:
    active={ns:connection.execute("SELECT COUNT(*) FROM records WHERE namespace=? AND json_extract(body,'$.state') NOT IN ('completed','partial','failed','canceled','paused')",(ns,)).fetchone()[0] for ns in ('jobs','music_jobs','video_jobs')}
finally: connection.close()
print('Active jobs before restart:',active)
if any(active.values()) and not args.confirm_core_restart:
    raise SystemExit('Other work is active. Durable jobs resume after the core restarts; rerun with --confirm-core-restart to proceed.')

# Worker identity, directories and release ---------------------------------------
try: grp.getgrnam('archive-anime-read')
except KeyError: run('groupadd','--system','archive-anime-read')
try: pwd.getpwnam('archive-anime')
except KeyError: run('useradd','--system','--gid','archive-anime-read','--home-dir',str(root/'home'),'--shell','/usr/sbin/nologin','archive-anime')
run('usermod','-a','-G','archive-anime-read','archive-console')
uid=pwd.getpwnam('archive-anime').pw_uid; gid=grp.getgrnam('archive-anime-read').gr_gid; core=pwd.getpwnam('archive-console').pw_uid
root.mkdir(mode=0o755,exist_ok=True)
for name,mode in (('spool',0o750),('home',0o700)):
    path=root/name; path.mkdir(exist_ok=True); os.chown(path,uid,gid); path.chmod(mode)
(root/'releases').mkdir(exist_ok=True)
release=root/'releases'/time.strftime('%Y%m%d-%H%M%S'); release.mkdir()
shutil.copytree(repo/'apps/anime-worker/src',release/'src',ignore=shutil.ignore_patterns('__pycache__'))
for path in (release/'src').rglob('*'): path.chmod(0o755 if path.is_dir() else 0o644)
link=root/'current.new'; link.unlink(missing_ok=True); link.symlink_to(release); link.replace(root/'current')
worker_config=pathlib.Path('/etc/archive-console/anime-worker.json')
worker_config.write_text(json.dumps({'socket':'/run/archive-console-anime/worker.sock','spool':str(root/'spool'),'coreUid':core})+'\n'); os.chown(worker_config,0,gid); worker_config.chmod(0o640)

# Dedicated egress table; never flush or alter other tables -----------------------
firewall=pathlib.Path('/etc/archive-console/anime-egress.nft')
firewall.write_text((repo/'deploy/anime/egress.nft').read_text().replace('ANIME_UID',str(uid))); firewall.chmod(0o644)
helper=pathlib.Path('/usr/local/libexec/archive-anime-egress'); helper.parent.mkdir(exist_ok=True)
helper.write_text('#!/bin/sh\nset -eu\nif /usr/sbin/nft list table inet archive_anime_egress >/dev/null 2>&1; then /usr/sbin/nft delete table inet archive_anime_egress; fi\n/usr/sbin/nft -f /etc/archive-console/anime-egress.nft\n'); helper.chmod(0o755)

# Units: hide every configured archive volume from the worker ---------------------
mounts=sorted({v['mountPoint'] for v in json.loads(core_config.read_text()).get('volumes',[])} | {configured('@FILES_MOUNT@'),configured('@BACKUPS_MOUNT@')})
unit=configured((repo/'deploy/systemd/archive-console-anime.service').read_text()).replace('ANIME_INACCESSIBLE',' '.join('-'+m for m in mounts))
pathlib.Path('/etc/systemd/system/archive-console-anime.service').write_text(unit)
shutil.copy2(repo/'deploy/systemd/archive-anime-egress.service','/etc/systemd/system/archive-anime-egress.service')
run('systemctl','daemon-reload'); run('systemctl','restart','archive-anime-egress.service'); run('systemctl','enable','--now','archive-console-anime.service')
run('systemctl','restart','archive-console-anime.service')
for _ in range(20):
    if pathlib.Path('/run/archive-console-anime/worker.sock').exists(): break
    time.sleep(0.5)
else: raise SystemExit('Anime worker socket did not appear; core was not changed. Inspect: journalctl -u archive-console-anime')

# Core: private backup, then additive code/config/drop-in --------------------------
backup=service_root/('pre-anime-'+time.strftime('%Y%m%d-%H%M%S')); backup.mkdir(mode=0o700)
shutil.copy2(core_config,backup/'config.json'); shutil.copytree(service_root/'backend',backup/'backend')
src=sqlite3.connect(str(database)); dst=sqlite3.connect(str(backup/'archive.sqlite3')); src.backup(dst); dst.close(); src.close(); (backup/'archive.sqlite3').chmod(0o600)
try:
    run('systemctl','stop','archive-console.service')
    shutil.copytree(source,installed,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__'))
    data=json.loads(core_config.read_text()); previous=data.get('anime') or {}
    # Keep operator choices such as defaultDestination across reinstalls.
    data['anime']={**{k:v for k,v in previous.items() if k=='defaultDestination'},'socket':'/run/archive-console-anime/worker.sock','spool':str(root/'spool'),'workerUid':uid}
    if default: data['anime']['defaultDestination']=default
    core_config.write_text(json.dumps(data,indent=2)+'\n')
    dropin=pathlib.Path('/etc/systemd/system/archive-console.service.d/anime.conf'); dropin.parent.mkdir(exist_ok=True)
    dropin.write_text(configured('[Unit]\nWants=archive-console-anime.service\nAfter=archive-console-anime.service\n[Service]\nSupplementaryGroups=archive-anime-read\nReadOnlyPaths=@RUNTIME_ROOT@/archive-console-anime/spool\n'))
    run('systemctl','daemon-reload'); run('systemctl','start','archive-console.service')
except Exception:
    # Keep the current database; namespaces are additive. Restore runnable code and configuration.
    shutil.copy2(backup/'config.json',core_config)
    shutil.copytree(backup/'backend',service_root/'backend',dirs_exist_ok=True)
    pathlib.Path('/etc/systemd/system/archive-console.service.d/anime.conf').unlink(missing_ok=True)
    subprocess.run(['systemctl','daemon-reload'],check=False); subprocess.run(['systemctl','start','archive-console.service'],check=False)
    raise
print(json.dumps({'workerRelease':release.name,'coreBackup':backup.name,'workerUid':uid,'inaccessibleMounts':mounts,'activeJobsBefore':active}))
