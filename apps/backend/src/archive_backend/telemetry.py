"""Bounded aggregate-only health publishing in a disposable process.

Storage calls submit() without waiting on DNS, TLS, socket writes, or a receiver.
The parent supervises a stuck child on submit/status and preserves storage state.
"""
import base64
import hashlib
import json
import multiprocessing
import os
import queue
import random
import socket
import ssl
import stat
import struct
import time
from urllib.parse import urlsplit

MAX_PAYLOAD = 16 * 1024
IO_TIMEOUT = 5
STALL_LIMIT = 20
STATES = {'Ready', 'Starting', 'Disabled', 'Error', 'Unavailable', 'Unknown'}


class PublishError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def payload(snapshot):
    """Copy only validated aggregate fields, never arbitrary nested data/text."""
    if not isinstance(snapshot, dict):
        snapshot = {}
    def number(key):
        value = snapshot.get(key)
        return value if type(value) is int and 0 <= value <= 2**63 - 1 else None
    result = {'schemaVersion': 1, 'timestamp': int(time.time()),
              'uptimeSeconds': number('uptimeSeconds'), 'activeJobs': number('activeJobs'),
              'queuedJobs': number('queuedJobs'), 'errorCount': number('errorCount')}
    for key in ('apiState', 'mcpState'):
        value = snapshot.get(key)
        result[key] = value if isinstance(value, str) and value in STATES else 'Unknown'
    drives = snapshot.get('drives', [])
    result['drives'] = []
    if isinstance(drives, list):
        for index, drive in enumerate(drives[:32]):
            if not isinstance(drive, dict):
                continue
            safe = {'slot': index, 'online': drive.get('online') if type(drive.get('online')) is bool else None}
            for key in ('capacityBytes', 'freeBytes', 'temperatureCelsius'):
                value = drive.get(key)
                safe[key] = value if type(value) is int and 0 <= value <= 2**63 - 1 else None
            result['drives'].append(safe)
    data = json.dumps(result, separators=(',', ':'), allow_nan=False).encode()
    if len(data) > MAX_PAYLOAD:
        raise PublishError('PAYLOAD_LIMIT')
    return data


def read_secret(path):
    if not isinstance(path, str) or not os.path.isabs(path):
        raise PublishError('AUTH_NOT_CONFIGURED')
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.geteuid() or info.st_size > 4096:
                raise PublishError('AUTH_CONFIGURATION')
            data = os.read(fd, 4097).strip()
        finally:
            os.close(fd)
        if not data or len(data) > 4096 or any(c <= 32 or c >= 127 for c in data):
            raise PublishError('AUTH_CONFIGURATION')
        return data.decode('ascii')
    except (OSError, UnicodeError):
        raise PublishError('AUTH_CONFIGURATION') from None


def send_websocket(config, data):
    """One encrypted authenticated connection/sample, bounded handshake and send.

    Last success means bytes sent through TLS, not receiver persistence. DNS may
    outlive socket timeout; the parent watchdog kills the isolated process.
    """
    endpoint = urlsplit(config.get('endpoint', ''))
    if endpoint.scheme != 'wss' or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise PublishError('INVALID_CONFIGURATION')
    secret = read_secret(config.get('secretFile'))
    key = base64.b64encode(os.urandom(16)).decode('ascii')
    host = endpoint.hostname
    port = endpoint.port or 443
    authority = ('[' + host + ']' if ':' in host else host) + (':' + str(port) if port != 443 else '')
    target = endpoint.path or '/'
    if any(ord(c) <= 32 or ord(c) >= 127 for c in target + authority):
        raise PublishError('INVALID_CONFIGURATION')
    request = ('GET ' + target + ' HTTP/1.1\r\nHost: ' + authority + '\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: ' + key + '\r\nSec-WebSocket-Version: 13\r\nAuthorization: Bearer ' + secret + '\r\n\r\n').encode('ascii')
    try:
        with socket.create_connection((host, port), timeout=IO_TIMEOUT) as raw:
            with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as conn:
                conn.settimeout(IO_TIMEOUT)
                conn.sendall(request)
                response = bytearray()
                deadline = time.monotonic() + IO_TIMEOUT
                while not response.endswith(b'\r\n\r\n'):
                    if len(response) >= 8192 or time.monotonic() >= deadline:
                        raise PublishError('HANDSHAKE_LIMIT')
                    part = conn.recv(1)
                    if not part:
                        raise PublishError('HANDSHAKE_FAILED')
                    response.extend(part)
                lines = bytes(response).decode('iso-8859-1').split('\r\n')
                status = lines[0].split(' ')
                if len(status) < 2:
                    raise PublishError('HANDSHAKE_FAILED')
                if status[1] in ('401', '403'):
                    raise PublishError('AUTH_REJECTED')
                headers = {}
                for line in lines[1:]:
                    if line:
                        k, v = line.split(':', 1)
                        k = k.lower().strip()
                        if k in headers:
                            raise PublishError('HANDSHAKE_FAILED')
                        headers[k] = v.strip()
                expected = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
                if status[1] != '101' or headers.get('sec-websocket-accept') != expected or headers.get('upgrade', '').lower() != 'websocket' or 'upgrade' not in [x.strip().lower() for x in headers.get('connection', '').split(',')]:
                    raise PublishError('HANDSHAKE_FAILED')
                if 'sec-websocket-extensions' in headers or 'sec-websocket-protocol' in headers:
                    raise PublishError('HANDSHAKE_FAILED')
                if len(data) > MAX_PAYLOAD:
                    raise PublishError('PAYLOAD_LIMIT')
                mask = os.urandom(4)
                size = len(data)
                prefix = bytes([0x81, 0x80 | size]) if size < 126 else b'\x81\xfe' + struct.pack('!H', size)
                conn.sendall(prefix + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(data)))
                close_mask = os.urandom(4)
                conn.sendall(b'\x88\x80' + close_mask)
    except PublishError:
        raise
    except (OSError, ValueError, UnicodeError):
        raise PublishError('CONNECTION_FAILED') from None


def put_latest(channel, value):
    """Never wait for a consumer; permit dropped status/health samples."""
    try:
        channel.put_nowait(value)
        return 0
    except queue.Full:
        try:
            channel.get_nowait()
        except queue.Empty:
            return 1
        try:
            channel.put_nowait(value)
        except queue.Full:
            pass
        return 1


def worker(config, samples, statuses, stop, sender=send_websocket):
    if config.get('protocol') == 'fleet-v1':
        from .fleet_telemetry import worker as fleet_worker
        return fleet_worker(config, samples, statuses, stop)
    failures = 0
    coalesced = 0
    last_success = None
    interval = max(5, min(3600, config.get('intervalSeconds', 30)))
    while not stop.is_set():
        try:
            data = samples.get(timeout=0.25)
        except queue.Empty:
            continue
        # Drain stale routine samples before spending network work.
        try:
            while True:
                data = samples.get_nowait()
                coalesced += 1
        except queue.Empty:
            pass
        put_latest(statuses, {'state': 'Connecting', 'attemptAt': time.monotonic(), 'lastSuccessfulSend': last_success, 'coalescedSamples': coalesced})
        try:
            sender(config, data)
            failures = 0
            last_success = time.time()
            put_latest(statuses, {'state': 'Connected', 'lastSuccessfulSend': last_success, 'coalescedSamples': coalesced})
            delay = interval
        except Exception as exc:
            failures += 1
            code = exc.code if isinstance(exc, PublishError) else 'CONNECTION_FAILED'
            circuit = code in ('AUTH_REJECTED', 'AUTH_NOT_CONFIGURED', 'AUTH_CONFIGURATION', 'INVALID_CONFIGURATION') or failures >= 8
            put_latest(statuses, {'state': 'Error' if circuit else 'Reconnecting', 'errorCode': code,
                                 'lastSuccessfulSend': last_success, 'coalescedSamples': coalesced})
            delay = 300 if circuit else min(60, 2 ** min(failures, 6)) * random.uniform(0.8, 1.2)
        stop.wait(delay)


class Publisher:
    def __init__(self, config):
        self.config = dict(config)
        self.dropped = 0
        self.process = None
        self.samples = None
        self.statuses = None
        self.stop = None
        self.retry_at = 0
        self.closed = False
        self._terminating = False
        self.current = {'state': 'Disabled' if not config.get('enabled') else 'Not configured', 'lastSuccessfulSend': None}
        if config.get('enabled') and config.get('endpoint'):
            self._start()
    def _start(self):
        try:
            context = multiprocessing.get_context('spawn')
            self.samples = context.Queue(maxsize=2)
            self.statuses = context.Queue(maxsize=2)
            self.stop = context.Event()
            self.process = context.Process(target=worker, args=(self.config, self.samples, self.statuses, self.stop), daemon=True)
            self.process.start()
        except Exception:
            self._dispose_queues()
            self.process = None
            self.retry_at = time.monotonic() + 30
            self.current = {'state': 'Error', 'errorCode': 'WORKER_UNAVAILABLE', 'lastSuccessfulSend': self.current.get('lastSuccessfulSend')}
            return
        self.current = {'state': 'Connecting', 'lastSuccessfulSend': self.current.get('lastSuccessfulSend')}
    def _refresh(self):
        if self.process is None:
            return
        if self._terminating:
            self.process.join(timeout=0)
            if self.process.is_alive(): return
            self._dispose_queues(); self.process=None; self._terminating=False
            return
        try:
            while True:
                update = self.statuses.get_nowait()
                self.current = update
        except queue.Empty:
            pass
        attempt = self.current.get('pulseAt', self.current.get('attemptAt'))
        stalled = attempt is not None and time.monotonic() - attempt > STALL_LIMIT
        if not self.process.is_alive() or stalled:
            self.process.terminate()
            self.process.join(timeout=0)  # Never wait for network recovery.
            self.current = {'state': 'Error', 'errorCode': 'WORKER_STALLED' if stalled else 'WORKER_EXITED',
                            'lastSuccessfulSend': self.current.get('lastSuccessfulSend')}
            self.retry_at = time.monotonic() + 30
            if self.process.is_alive():
                self._terminating=True
                return
            self._dispose_queues()
            self.process = None
    def _dispose_queues(self):
        for channel in (self.samples, self.statuses):
            if channel is not None:
                try:
                    channel.cancel_join_thread()
                    channel.close()
                except (OSError, ValueError):
                    pass
    def submit(self, snapshot):
        if self.closed:
            return False
        self._refresh()
        if self.process is None and self.config.get('enabled') and self.config.get('endpoint') and time.monotonic() >= self.retry_at:
            self._start()
        if self.process is None:
            return False
        if self._terminating:
            self.dropped+=1; return False
        self.dropped += put_latest(self.samples, payload(snapshot))
        return True
    def status(self):
        self._refresh()
        return {k: v for k, v in self.current.items() if k not in ('attemptAt', 'pulseAt')} | {'droppedSamples': self.dropped}
    def close(self):
        self.closed = True
        self.current = {'state': 'Disabled', 'lastSuccessfulSend': self.current.get('lastSuccessfulSend')}
        if self.process is not None:
            self.stop.set()
            self.process.join(timeout=0.2)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join(timeout=0.2)
            self._dispose_queues()
            self.process = None
