"""UID-authenticated Unix socket; bounded task slots, leases and process groups."""
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
import uuid

MAX_JSON = 2*1024*1024
UUID = re.compile(r'^[a-f0-9]{32}$')
SPOTIFY = re.compile(r'^[A-Za-z0-9]{22}$')


def disk_used(root):
    total=0
    for p in root.rglob('*'):
        try:
            if p.is_file() and not p.is_symlink():total+=p.stat().st_size
        except FileNotFoundError:pass  # ffmpeg may remove a completed intermediate concurrently
    return total


class Worker:
    def __init__(self, config):
        self.config = config; self.root = Path(config['spool']); self.tasks = {}; self.lock = threading.RLock()
        self.epoch = uuid.uuid4().hex

    def stop(self, task):
        if task.get('reaped'): return
        process = task['process']
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        process.wait(timeout=10)
        task['reaped'] = True
        task['state'] = 'canceled'

    def reap(self):
        with self.lock:
            for task in self.tasks.values():
                p = task['process']
                if task['state'] != 'running': continue
                used=disk_used(self.root)
                if used>24*1024**3 or shutil.disk_usage(self.root).free<10*1024**3:
                    self.stop(task); task['state']='failed'; task['error']='DISK_BUDGET'
                elif time.monotonic() > task['lease'] or time.monotonic() > task['deadline']:
                    self.stop(task); task['state'] = 'failed'; task['error'] = 'LEASE_EXPIRED'
                elif p.poll() is not None:
                    # Remove any descendants before a result can be imported.
                    self.stop(task)
                    try:
                        file = task['directory']/'result.json'
                        if file.stat().st_size > MAX_JSON: raise ValueError('Result too large')
                        result = json.loads(file.read_text())
                        task.update(result); task['state'] = 'completed' if result.get('ok') else 'failed'
                    except (OSError, ValueError):
                        task['state'] = 'failed'; task['error'] = 'SOURCE_UNAVAILABLE'

    def call(self, args):
        with self.lock:
            self.reap(); op = args.get('op')
            if op == 'status': return {'epoch': self.epoch, 'version': 'yt-dlp 2026.8.19', 'available': True}
            task_id = args.get('taskId', '')
            if not isinstance(task_id, str) or not UUID.fullmatch(task_id): raise ValueError('Task ID')
            task = self.tasks.get(task_id)
            if op == 'start':
                if task:
                    if task['request'] != args: raise ValueError('Task identity collision')
                    task['lease'] = time.monotonic()+60
                    return self.present(task)
                action = args.get('action'); payload = args.get('payload', {})
                if set(args) != {'op','taskId','action','payload'}: raise ValueError('Fields')
                from .policy import canonical_url
                if action not in ('resolve','download'): raise ValueError('Action')
                if set(payload)!=({'url'} if action=='resolve' else {'url','choiceId','source','duration'}):raise ValueError('Fields')
                canonical_url(payload.get('url'))
                if action=='download' and (not re.fullmatch(r'audio|video-[0-9]{1,4}',str(payload['choiceId'])) or not isinstance(payload['source'],str) or len(payload['source'])>200 or type(payload['duration']) not in (int,float) or not 0<payload['duration']<=86400):raise ValueError('Choice')
                active = [t for t in self.tasks.values() if t['state']=='running']
                if sum(t['action']=='download' for t in active) >= 1 and action=='download' or any(t['action']!='download' for t in active) and action!='download': raise ValueError('BUSY')
                if len(self.tasks) >= 128 or shutil.disk_usage(self.root).free < 34*1024**3: raise ValueError('QUOTA_EXCEEDED')
                used = disk_used(self.root)
                if used >= 24*1024**3: raise ValueError('QUOTA_EXCEEDED')
                directory = self.root/task_id; directory.mkdir(mode=0o750)
                task_home=directory/'.home'; task_home.mkdir(mode=0o700)
                environment=dict(os.environ,HOME=str(task_home))
                p = subprocess.Popen([sys.executable,'-m','archive_video_worker.task'], cwd=directory,
                                     stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True, close_fds=True, env=environment)
                p.stdin.write(json.dumps({'action':action,'payload':payload}).encode()); p.stdin.close()
                task = {'id':task_id,'request':args,'action':action,'state':'running','process':p,'directory':directory,
                        'lease':time.monotonic()+60,'deadline':time.monotonic()+(7200 if action=='download' else 120)}
                self.tasks[task_id] = task; return self.present(task)
            if op == 'cleanup' and task is None:
                # After restart, only core-generated task IDs can be removed. No process survives systemd KillMode=control-group.
                shutil.rmtree(self.root/task_id, ignore_errors=True); return {'state':'absent','epoch':self.epoch}
            if not task: return {'state':'absent','epoch':self.epoch}
            if op == 'poll': task['lease'] = time.monotonic()+60; return self.present(task)
            if op == 'cancel': self.stop(task); return self.present(task)
            if op == 'cleanup':
                self.stop(task); shutil.rmtree(task['directory']); del self.tasks[task_id]
                return {'state':'absent','epoch':self.epoch}
            raise ValueError('Operation')

    def present(self, task):
        value = {'epoch':self.epoch,'state':task['state'],'taskId':task['id']}
        if task['state']=='completed': value['result'] = task['result']
        if task['state']=='failed': value['error'] = task.get('error','SOURCE_UNAVAILABLE')
        if task['state']=='running':
            try:
                data=json.loads((task['directory']/'progress.json').read_text()); stage=data['stage']
                if stage in ('downloading','validating'): value['stage']=stage
                fraction=data.get('progress')
                if type(fraction) in (float,int) and 0<=fraction<=1:value['progress']=fraction
            except (OSError, ValueError, KeyError): pass
        return value


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        _, uid, _ = struct.unpack('3i', self.request.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
        if uid != self.server.worker.config['coreUid']: return
        self.request.settimeout(5)
        try:
            raw = self.rfile.readline(65537)
            if len(raw)>65536: raise ValueError('Request size')
            result = self.server.worker.call(json.loads(raw))
            self.wfile.write(json.dumps({'ok':True,'result':result}).encode()+b'\n')
        except Exception:
            self.wfile.write(b'{"ok":false,"error":"WORKER_UNAVAILABLE"}\n')


def main(config):
    worker = Worker(config); path = config['socket']
    try: os.unlink(path)
    except FileNotFoundError: pass
    server = Server(path, Handler); server.worker = worker; os.chmod(path,0o660)
    def monitor():
        while True: time.sleep(1); worker.reap()
    threading.Thread(target=monitor, daemon=True).start()
    server.serve_forever()
