"""Fleet protocol v1: durable health events, live challenges, bounded reconnects.

Wire behavior follows websocket-server-v2's Python SDK and get_design contract.
Runs only in the disposable telemetry process; no storage request waits on WSS.
"""
import datetime
import fcntl
import json
import os
from pathlib import Path
import queue
import random
import sqlite3
import time
import uuid
from .telemetry import PublishError, put_latest, read_secret


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


class Spool:
    def __init__(self, path, node, source):
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = open(str(path)+'.lock', 'a')
        try: fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close(); raise
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, created REAL, body TEXT); CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);')
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('boot',?)", (uuid.uuid4().hex,))
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('seq','0')")
        self.boot = self.db.execute("SELECT value FROM meta WHERE key='boot'").fetchone()[0]
        self.node, self.source = node, source

    def event(self, kind, body):
        with self.db:
            seq = int(self.db.execute("SELECT value FROM meta WHERE key='seq'").fetchone()[0])+1
            self.db.execute("UPDATE meta SET value=? WHERE key='seq'", (str(seq),))
        return dict(protocol_version=1,event_id=uuid.uuid4().hex,node_id=self.node,source_id=self.source,
                    boot_id=self.boot,seq=seq,kind=kind,schema_id=f'{self.node}/{self.source}.{kind}',
                    schema_version='1.0.0',observed_at=utcnow(),payload=body)

    def add(self, kind, body):
        if kind == 'heartbeat': raise ValueError('Heartbeats cannot be replayed')
        event = self.event(kind, body)
        with self.db:
            self.db.execute('INSERT INTO events VALUES(?,?,?)', (event['event_id'],time.time(),json.dumps(event)))
        self.prune()
        return event

    def prune(self):
        # Aggregate events are small; both age and row count bound disk use (~10 MiB).
        with self.db:
            self.db.execute('DELETE FROM events WHERE created<?', (time.time()-86300,))
            self.db.execute('DELETE FROM events WHERE id IN (SELECT id FROM events ORDER BY created DESC LIMIT -1 OFFSET 10000)')

    def pending(self):
        self.prune()
        row = self.db.execute('SELECT body FROM events ORDER BY created LIMIT 1').fetchone()
        return json.loads(row[0]) if row else None

    def ack(self, batch, reply):
        ids = {e['event_id'] for e in batch['events']}
        if reply.get('type') != 'ack' or reply.get('batch_id') != batch['batch_id'] or not isinstance(reply.get('challenge'),str):
            raise PublishError('INVALID_ACK')
        accepted = reply.get('accepted',[]) + reply.get('duplicates',[])
        if not set(accepted) <= ids: raise PublishError('INVALID_ACK')
        with self.db:
            self.db.executemany('DELETE FROM events WHERE id=?', [(i,) for i in accepted])
        if reply.get('rejected'): raise PublishError('EVENT_REJECTED')
        if not accepted and not reply.get('retryable'): raise PublishError('INVALID_ACK')
        return bool(accepted)

    def close(self):
        self.db.close(); self.lock.close()


def health_events(snapshot):
    checks = []
    for key, name in [('apiState','api'),('mcpState','mcp')]:
        checks.append({'name':name,'status':'healthy' if snapshot.get(key)=='Ready' else 'unhealthy'})
    drives = snapshot.get('drives',[])
    online = bool(drives) and all(d.get('online') is True for d in drives)
    space = online and all(isinstance(d.get('freeBytes'),int) and d['freeBytes'] >= 10*1024**3 for d in drives)
    checks.extend([{'name':'storage','status':'healthy' if online else 'unhealthy'},
                   {'name':'free-space','status':'healthy' if space else 'degraded'}])
    status = 'unhealthy' if any(c['status']=='unhealthy' for c in checks) else 'degraded' if not space else 'healthy'
    yield 'health', {'status':status,'summary':'Archive service and configured storage checks','checks':checks}
    for key,name,unit in [('uptimeSeconds','uptime','seconds'),('activeJobs','active-jobs','jobs'),('queuedJobs','queued-jobs','jobs')]:
        if type(snapshot.get(key)) is int:
            yield 'metric', {'name':name,'value':snapshot[key],'unit':unit,'labels':{}}
    for drive in drives:
        for key,name in [('capacityBytes','storage-capacity'),('freeBytes','storage-free')]:
            if type(drive.get(key)) is int:
                yield 'metric', {'name':name,'value':drive[key],'unit':'bytes','labels':{'slot':str(drive['slot'])}}


def worker(config, samples, statuses, stop):
    from websockets.sync.client import connect
    os.umask(0o077)
    spool = Spool(config['spoolPath'],config['nodeId'],config['sourceId'])
    current = {'state':'Connecting','lastSuccessfulSend':None}
    started = time.monotonic(); last_sample = None; failures = 0
    def pulse(**updates):
        current.update(updates)
        put_latest(statuses,dict(current,pulseAt=time.monotonic()))
    def collect():
        nonlocal last_sample
        data = None
        try:
            while True: data = samples.get_nowait()
        except queue.Empty: pass
        if data is not None:
            last_sample=time.monotonic()
            for kind,body in health_events(json.loads(data)): spool.add(kind,body)
    try:
        while not stop.is_set():
            collect(); pulse(state='Connecting')
            try:
                secret = read_secret(config['secretFile'])
                with connect(config['endpoint'],additional_headers={'Authorization':'Bearer '+secret},
                             open_timeout=5,close_timeout=2,ping_interval=20,ping_timeout=10,
                             max_size=262144,max_queue=4,compression=None,proxy=None) as ws:
                    ws.send(json.dumps(dict(type='hello',protocol_version=1,node_id=config['nodeId'],source_id=config['sourceId'],manifest_revision=config['manifestRevision'],boot_id=spool.boot)))
                    welcome=json.loads(ws.recv(timeout=5))
                    if welcome.get('type')!='welcome' or not 5 <= welcome['heartbeat_s'] <= 3600:
                        raise PublishError('INVALID_WELCOME')
                    challenge=welcome['challenge']; heartbeat_at=0; live_since=utcnow()
                    while not stop.is_set():
                        collect(); pulse()
                        # A stuck collector must not keep this application falsely fresh.
                        if last_sample is None or time.monotonic()-last_sample > max(60,config['intervalSeconds']*3):
                            raise PublishError('SAMPLE_STALE')
                        heartbeat=time.monotonic()-heartbeat_at >= welcome['heartbeat_s']
                        event=spool.event('heartbeat',{'uptime_s':time.monotonic()-started,'publisher_version':'archive-console-0.1.0','spool_depth':spool.db.execute('SELECT count(*) FROM events').fetchone()[0]}) if heartbeat else spool.pending()
                        if event is None: stop.wait(.25); continue
                        batch={'type':'events','batch_id':uuid.uuid4().hex,'replay':False if heartbeat else event['observed_at']<live_since,'events':[event]}
                        if heartbeat: batch['challenge']=challenge
                        ws.send(json.dumps(batch)); reply=json.loads(ws.recv(timeout=8))
                        accepted=spool.ack(batch,reply); challenge=reply['challenge']
                        if accepted:
                            failures=0
                            if heartbeat: heartbeat_at=time.monotonic(); pulse(lastHeartbeatAck=time.time())
                            if event['kind']=='health': pulse(state='Connected',lastSuccessfulSend=time.time(),errorCode=None)
                        stop.wait(1 if reply.get('retryable') else .05)
            except Exception as exc:
                failures+=1
                code=exc.code if isinstance(exc,PublishError) else 'CONNECTION_FAILED'
                circuit=code in ('AUTH_CONFIGURATION','AUTH_NOT_CONFIGURED','EVENT_REJECTED','INVALID_WELCOME')
                pulse(state='Error' if circuit else 'Reconnecting',errorCode=code)
                delay=300 if circuit else random.uniform(.8,1.2)*min(60,2**min(failures,6))
                until=time.monotonic()+delay
                while not stop.is_set() and time.monotonic()<until:
                    collect(); pulse(); stop.wait(.5)
    finally: spool.close()
