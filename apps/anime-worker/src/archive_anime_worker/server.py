"""UID-authenticated Unix socket; bounded task slots, leases, deadlines and process groups.

A download task directory survives worker restarts, pauses and retryable failures,
so starting the same task identity again resumes from its verified checkpoint.
"""
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

MAX_RESULT = 3 * 1024 * 1024
TASK_ID = re.compile(r'^[a-f0-9]{32}$')
SERIES = re.compile(r'^[a-z]{2,20}/[a-z0-9]+(?:-[a-z0-9]+)*-[0-9]{1,12}$')
EPISODE_ID = re.compile(r'^[A-Za-z0-9._-]{1,200}$')
NUMBER = re.compile(r'^(0|[1-9][0-9]{0,4})(\.[0-9]{1,2})?$')
QUALITY = re.compile(r'^(best|[1-9][0-9]{2,3}p)$')
LANGUAGES = ('en', 'es', 'pt', 'fr', 'de', 'it', 'ru', 'ar', 'ja', 'zh', 'ko', 'id', 'th', 'vi', 'pl', 'tr', 'hi', 'ms')
SPOOL_MAX = 24 * 1024**3
FREE_MIN = 8 * 1024**3
ADMIT_FREE = 16 * 1024**3
LEASE = 60
DEADLINES = {'search': 120, 'lookup': 180, 'download': 3 * 3600}


def disk_used(root):
    total = 0
    for base, _, files in os.walk(root):
        for name in files:
            try: total += os.lstat(os.path.join(base, name)).st_size
            except FileNotFoundError: pass  # FFmpeg and checkpoint pruning remove files concurrently
    return total


def validate(action, payload):
    """Only stable identities and fixed choices cross the boundary; never URLs or paths."""
    if not isinstance(payload, dict): raise ValueError('Payload')
    if action == 'search':
        if set(payload) != {'query', 'page'} or not isinstance(payload['query'], str) or not 1 <= len(payload['query'].strip()) <= 200 or '\0' in payload['query'] or type(payload['page']) is not int or not 1 <= payload['page'] <= 50: raise ValueError('Search')
    elif action == 'lookup':
        if set(payload) != {'anilistId', 'providerSeriesId'} or type(payload['anilistId']) is not int or not 1 <= payload['anilistId'] <= 10**9: raise ValueError('Lookup')
        if payload['providerSeriesId'] is not None and (not isinstance(payload['providerSeriesId'], str) or not SERIES.fullmatch(payload['providerSeriesId'])): raise ValueError('Lookup')
    elif action == 'download':
        if set(payload) != {'providerSeriesId', 'episodeId', 'number', 'audio', 'quality', 'subtitles'}: raise ValueError('Download')
        if not isinstance(payload['providerSeriesId'], str) or not SERIES.fullmatch(payload['providerSeriesId']) or not isinstance(payload['episodeId'], str) or not EPISODE_ID.fullmatch(payload['episodeId']): raise ValueError('Download')
        if not isinstance(payload['number'], str) or not NUMBER.fullmatch(payload['number']) or payload['audio'] not in ('sub', 'dub') or not isinstance(payload['quality'], str) or not QUALITY.fullmatch(payload['quality']): raise ValueError('Download')
        subtitles = payload['subtitles']
        if not isinstance(subtitles, list) or len(set(subtitles)) != len(subtitles) or any(s not in LANGUAGES for s in subtitles): raise ValueError('Download')
    else: raise ValueError('Action')


class Worker:
    def __init__(self, config):
        self.config = config; self.root = Path(config['spool']); self.tasks = {}; self.lock = threading.RLock(); self.epoch = uuid.uuid4().hex

    def stop(self, task, state='canceled'):
        if not task.get('reaped'):
            try: os.killpg(task['process'].pid, signal.SIGKILL)
            except ProcessLookupError: pass
            task['process'].wait(timeout=10); task['reaped'] = True
        task['state'] = state

    def reap(self):
        with self.lock:
            running = [t for t in self.tasks.values() if t['state'] == 'running']
            over = bool(running) and (disk_used(self.root) > SPOOL_MAX or shutil.disk_usage(self.root).free < FREE_MIN)
            for task in running:
                now = time.monotonic()
                if over and task['action'] == 'download': self.stop(task, 'failed'); task['error'] = 'DISK_BUDGET'
                elif now > task['lease']: self.stop(task, 'failed'); task['error'] = 'LEASE_EXPIRED'
                elif now > task['deadline']: self.stop(task, 'failed'); task['error'] = 'SOURCE_UNAVAILABLE'
                elif task['process'].poll() is not None:
                    # Kill any descendants (FFmpeg) before a result can be imported.
                    self.stop(task, 'failed')
                    try:
                        file = task['directory'] / 'result.json'
                        if file.stat().st_size > MAX_RESULT: raise ValueError('Result size')
                        result = json.loads(file.read_text())
                        if result.get('ok'): task.update(state='completed', result=result['result'])
                        else: task['error'] = str(result.get('error', 'SOURCE_UNAVAILABLE'))[:40]
                    except (OSError, ValueError, KeyError): task['error'] = 'SOURCE_UNAVAILABLE'

    def spawn(self, task):
        directory = task['directory']
        for name in ('result.json', 'progress.json'):
            (directory / name).unlink(missing_ok=True)
        home = directory / '.home'; home.mkdir(mode=0o700, exist_ok=True)
        environment = {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'PYTHONPATH': os.environ.get('PYTHONPATH', ''), 'PYTHONDONTWRITEBYTECODE': '1', 'ARCHIVE_ANIME_SPOOL': str(self.root), 'LANG': 'C.UTF-8'}
        process = subprocess.Popen([sys.executable, '-m', 'archive_anime_worker.task'], cwd=directory, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True, close_fds=True, env=environment)
        process.stdin.write(json.dumps({'action': task['action'], 'payload': task['request']['payload']}).encode()); process.stdin.close()
        task.update(process=process, state='running', reaped=False, lease=time.monotonic() + LEASE, deadline=time.monotonic() + DEADLINES[task['action']])
        task.pop('error', None); task.pop('result', None)

    def admit(self, action):
        active = [t for t in self.tasks.values() if t['state'] == 'running']
        if any((t['action'] == 'download') == (action == 'download') for t in active): raise ValueError('BUSY')
        if len(self.tasks) >= 256: raise ValueError('QUOTA_EXCEEDED')
        if action == 'download' and (shutil.disk_usage(self.root).free < ADMIT_FREE or disk_used(self.root) >= SPOOL_MAX): raise ValueError('QUOTA_EXCEEDED')

    def call(self, args):
        with self.lock:
            self.reap(); op = args.get('op')
            if op == 'status': return {'epoch': self.epoch, 'version': 'archive-anime-worker 0.1.0', 'available': True}
            task_id = args.get('taskId', '')
            if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id): raise ValueError('Task ID')
            task = self.tasks.get(task_id)
            if op == 'start':
                if set(args) != {'op', 'taskId', 'action', 'payload'}: raise ValueError('Fields')
                validate(args['action'], args['payload'])
                if task:
                    if task['request'] != args: raise ValueError('Task identity collision')
                    if task['state'] == 'running': task['lease'] = time.monotonic() + LEASE
                    elif task['state'] != 'completed' and task['action'] == 'download':
                        # Pause, lease loss or a retryable failure: resume from the retained checkpoint.
                        self.admit('download'); self.spawn(task)
                    return self.present(task)
                self.admit(args['action'])
                directory = self.root / task_id
                if directory.is_symlink() or (directory.exists() and not directory.is_dir()): raise ValueError('Directory')
                if args['action'] != 'download' and directory.exists(): shutil.rmtree(directory)
                directory.mkdir(mode=0o750, exist_ok=True)
                task = {'id': task_id, 'request': args, 'action': args['action'], 'directory': directory}
                self.spawn(task); self.tasks[task_id] = task
                return self.present(task)
            if op == 'cleanup':
                if task: self.stop(task); del self.tasks[task_id]
                # Only core-generated task IDs reach here; systemd's control-group kill removes orphans on restart.
                shutil.rmtree(self.root / task_id, ignore_errors=True)
                return {'state': 'absent', 'epoch': self.epoch}
            if not task: return {'state': 'absent', 'epoch': self.epoch}
            if op == 'poll': task['lease'] = time.monotonic() + LEASE if task['state'] == 'running' else task.get('lease', 0); return self.present(task)
            if op == 'cancel':
                if task['state'] == 'running': self.stop(task)
                return self.present(task)
            raise ValueError('Operation')

    def present(self, task):
        value = {'epoch': self.epoch, 'state': task['state'], 'taskId': task['id']}
        if task['state'] == 'completed': value['result'] = task['result']
        if task['state'] == 'failed': value['error'] = task.get('error', 'SOURCE_UNAVAILABLE')
        if task['state'] == 'running':
            try:
                data = json.loads((task['directory'] / 'progress.json').read_text())
                if data.get('stage') in ('resolving', 'downloading', 'verifying'): value['stage'] = data['stage']
                fraction = data.get('progress')
                if type(fraction) in (int, float) and 0 <= fraction <= 1: value['progress'] = fraction
            except (OSError, ValueError, AttributeError): pass
        return value


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        _, uid, _ = struct.unpack('3i', self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != self.server.worker.config['coreUid']: return
        self.request.settimeout(5)
        try:
            raw = self.rfile.readline(65537)
            if len(raw) > 65536: raise ValueError('Request size')
            result = self.server.worker.call(json.loads(raw))
            self.wfile.write(json.dumps({'ok': True, 'result': result}).encode() + b'\n')
        except Exception:
            self.wfile.write(b'{"ok":false,"error":"WORKER_UNAVAILABLE"}\n')


def main(config):
    worker = Worker(config); path = config['socket']
    try: os.unlink(path)
    except FileNotFoundError: pass
    server = Server(path, Handler); server.worker = worker; os.chmod(path, 0o660)

    def monitor():
        while True:
            time.sleep(1)
            try: worker.reap()
            except Exception: pass
    threading.Thread(target=monitor, daemon=True).start()
    server.serve_forever()
