"""Install the reviewed local video deployment. Run as root with --install-local.

Requires a prepared, hash-matching .runtime/music-wheels directory and verified
.runtime/music-bin/deno. Does not fetch dependencies or read interactive secrets.
"""
from deployment_settings import configured, install_template, validate_deployment
validate_deployment('BACKUPS_MOUNT', 'FILES_MOUNT', 'RUNTIME_ROOT')
import sys
import hashlib,grp,json,os,pathlib,pwd,shutil,sqlite3,subprocess,time
if sys.argv[1:] != ['--install-local'] or os.geteuid()!=0:
    raise SystemExit('Usage: sudo python3 scripts/deploy-video.py --install-local')
os.umask(0o022)
r=pathlib.Path(__file__).resolve().parents[1];root=pathlib.Path(configured('@RUNTIME_ROOT@/archive-console-video'))
def run(*args):subprocess.run(args,check=True)
if hashlib.sha256((r/'.runtime/music-bin/deno').read_bytes()).hexdigest()!=json.loads((r/'deploy/music/deno-release.json').read_text())['binarySha256']:
    raise SystemExit('Prepared Deno binary does not match the reviewed manifest')
try:grp.getgrnam('archive-video-read')
except KeyError:run('groupadd','--system','archive-video-read')
try:pwd.getpwnam('archive-video')
except KeyError:run('useradd','--system','--gid','archive-video-read','--home-dir',str(root/'home'),'--shell','/usr/sbin/nologin','archive-video')
run('usermod','-a','-G','archive-video-read','archive-console')
uid=pwd.getpwnam('archive-video').pw_uid;gid=grp.getgrnam('archive-video-read').gr_gid;core=pwd.getpwnam('archive-console').pw_uid
root.mkdir(mode=0o755,exist_ok=True)
for name,mode in [('spool',0o750),('home',0o700)]:
 p=root/name;p.mkdir(exist_ok=True);os.chown(p,uid,gid);p.chmod(mode)
(root/'releases').mkdir(exist_ok=True);release=root/'releases'/time.strftime('%Y%m%d-%H%M%S');release.mkdir()
shutil.copytree(r/'apps/video-worker/src',release/'src',ignore=shutil.ignore_patterns('__pycache__'))
for p in (release/'src').rglob('*'):p.chmod(0o755 if p.is_dir() else 0o644)
run('/usr/bin/python3','-m','venv',str(release/'venv'))
run(str(release/'venv/bin/pip'),'install','--no-index','--find-links',str(r/'.runtime/music-wheels'),'--require-hashes','-r',str(r/'apps/video-worker/requirements-linux-x86_64-py312.lock'))
(root/'bin').mkdir(exist_ok=True);shutil.copy2(r/'.runtime/music-bin/deno',root/'bin/deno')
link=root/'current.new';link.unlink(missing_ok=True);link.symlink_to(release);link.replace(root/'current')
config=pathlib.Path('/etc/archive-console/video-worker.json');config.write_text(json.dumps({'socket':'/run/archive-console-video/worker.sock','spool':str(root/'spool'),'coreUid':core})+'\n');os.chown(config,0,gid);config.chmod(0o640)
# Install a dedicated UID firewall; never flush or alter existing tables.
firewall=pathlib.Path('/etc/archive-console/video-egress.nft');firewall.write_text((r/'deploy/video/egress.nft').read_text().replace('VIDEO_UID',str(uid)));firewall.chmod(0o644)
helper=pathlib.Path('/usr/local/libexec/archive-video-egress');helper.parent.mkdir(exist_ok=True)
helper.write_text('#!/bin/sh\nset -eu\nif /usr/sbin/nft list table inet archive_video_egress >/dev/null 2>&1; then /usr/sbin/nft delete table inet archive_video_egress; fi\n/usr/sbin/nft -f /etc/archive-console/video-egress.nft\n');helper.chmod(0o755)
for unit in ['archive-console-video.service','archive-video-egress.service']:install_template(r/'deploy/systemd'/unit,pathlib.Path('/etc/systemd/system')/unit)
run('systemctl','daemon-reload');run('systemctl','stop','archive-console-video.service');run('systemctl','restart','archive-video-egress.service');run('systemctl','enable','--now','archive-console-video.service')
# Backup live core configuration, installed code and SQLite using SQLite backup API.
backup=pathlib.Path(configured('@RUNTIME_ROOT@/archive-console-service'))/('pre-yoinks-'+time.strftime('%Y%m%d-%H%M%S'));backup.mkdir(mode=0o700)
shutil.copy2('/etc/archive-console/config.json',backup/'config.json');shutil.copytree(configured('@RUNTIME_ROOT@/archive-console-service/backend'),backup/'backend')
source=sqlite3.connect(configured('@RUNTIME_ROOT@/archive-console-service/state/archive.sqlite3'));dest=sqlite3.connect(backup/'archive.sqlite3');source.backup(dest);dest.close();source.close();(backup/'archive.sqlite3').chmod(0o600)
try:
    run('systemctl','stop','archive-console.service')
    shutil.copytree(r/'apps/backend/src/archive_backend',configured('@RUNTIME_ROOT@/archive-console-service/backend/archive_backend'),dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__'))
    config=pathlib.Path('/etc/archive-console/config.json');data=json.loads(config.read_text());data['video']={'socket':'/run/archive-console-video/worker.sock','spool':str(root/'spool'),'workerUid':uid};config.write_text(json.dumps(data,indent=2)+'\n')
    dropin=pathlib.Path('/etc/systemd/system/archive-console.service.d/video.conf');dropin.parent.mkdir(exist_ok=True)
    dropin.write_text(configured('[Unit]\nWants=archive-console-video.service\nAfter=archive-console-video.service\n[Service]\nSupplementaryGroups=archive-video-read\nReadOnlyPaths=@RUNTIME_ROOT@/archive-console-video/spool\n'))
    run('systemctl','daemon-reload');run('systemctl','start','archive-console.service')
except Exception:
    # Keep the current database; namespaces are additive. Restore runnable code
    # and configuration, never overwrite a live database with a stale snapshot.
    shutil.copy2(backup/'config.json','/etc/archive-console/config.json')
    shutil.copytree(backup/'backend',configured('@RUNTIME_ROOT@/archive-console-service/backend'),dirs_exist_ok=True)
    subprocess.run(['systemctl','start','archive-console.service'],check=False)
    raise
print('Installed video release',release.name,'core backup',backup.name)
