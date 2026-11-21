"""Independent durable phone inbox; no use of Archive transfer jobs or vault data."""
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
import uuid
from .contracts import BackendError
from .filesystem import openat2
from .phone_transfer_contracts import LIMITS, INBOX, fail, prepare as validate_prepare


def stamp(value):
    return datetime.fromtimestamp(value,timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z') if value is not None else None

def encode(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
def ident():return str(uuid.uuid4())

SCHEMA = '''
CREATE TABLE meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE deliveries (id TEXT PRIMARY KEY,actor TEXT NOT NULL,request TEXT NOT NULL,inbox TEXT NOT NULL,
 state TEXT NOT NULL,created REAL NOT NULL,draft_expires REAL NOT NULL,published REAL,expires REAL,
 bytes INTEGER NOT NULL,body TEXT NOT NULL,fingerprint TEXT NOT NULL,cleanup INTEGER NOT NULL DEFAULT 0,
 UNIQUE(actor,request));
CREATE INDEX inbox_ready ON deliveries(inbox,state,published DESC,id DESC);
CREATE INDEX delivery_expiry ON deliveries(state,expires,draft_expires);
CREATE TABLE attachments (id TEXT PRIMARY KEY,delivery TEXT NOT NULL,size INTEGER NOT NULL,hash TEXT NOT NULL,
 received INTEGER NOT NULL DEFAULT 0,body TEXT NOT NULL,signature TEXT);
CREATE INDEX attachment_delivery ON attachments(delivery,id);
CREATE TABLE chunks (attachment TEXT,start INTEGER,length INTEGER,hash TEXT,PRIMARY KEY(attachment,start));
CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,inbox TEXT,kind TEXT,delivery TEXT,at REAL,summary TEXT);
CREATE INDEX inbox_events ON events(inbox,seq);
CREATE INDEX event_expiry ON events(at,seq);
CREATE TABLE devices (id TEXT PRIMARY KEY,actor TEXT,inbox TEXT,body TEXT);
CREATE TABLE receipts (delivery TEXT,device TEXT,body TEXT,PRIMARY KEY(delivery,device));
CREATE TABLE deletes (actor TEXT,request TEXT,delivery TEXT,PRIMARY KEY(actor,request));
'''

class PhoneStore:
    def __init__(self,root,check=lambda:None,*,quota=None,clock=time.time,fault=lambda point:None):
        self.root=root;self.check=check;self.clock=clock;self.fault=fault
        self.quota=quota if quota is not None else LIMITS['quotaBytes'];self.lock=threading.RLock()
        self.fd=None;self.lockfd=None;self.db=None;self.key=None

    def close(self):
        with self.lock:
            for scan in getattr(self,'scans',{}).values():scan.close()
            self.scans={}
            if self.db:self.db.close();self.db=None
            if self.lockfd is not None:os.close(self.lockfd);self.lockfd=None
            if self.fd is not None:os.close(self.fd);self.fd=None

    def ready(self):
        self.check()
        if self.fd is not None:
            info=os.stat(self.root,follow_symlinks=False);prior=os.fstat(self.fd)
            if (info.st_dev,info.st_ino)!=(prior.st_dev,prior.st_ino):fail('STORAGE_UNAVAILABLE',503)
            return
        parent=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
        try:self.fd=openat2(parent,self.root.lstrip('/'),os.O_RDONLY|os.O_DIRECTORY,resolve=0x04|0x08)
        finally:os.close(parent)
        try:
            rootinfo=os.fstat(self.fd)
            if rootinfo.st_uid!=os.geteuid() or rootinfo.st_mode&0o077:fail('STORAGE_UNAVAILABLE',503)
            self.lockfd=openat2(self.fd,'writer.lock',os.O_RDWR|os.O_CREAT,0o600);fcntl.flock(self.lockfd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            names=set(os.listdir(self.fd));existing='catalog.sqlite3' in names
            if not existing and names-{'writer.lock'}:fail('CATALOG_MISSING',503)
            for name in ('catalog.sqlite3','catalog.sqlite3-journal','catalog.sqlite3-wal','catalog.sqlite3-shm'):
                if name in names:
                    info=os.stat(name,dir_fd=self.fd,follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:fail('STORAGE_UNAVAILABLE',503)
            self.db=sqlite3.connect(f'/proc/self/fd/{self.fd}/catalog.sqlite3',check_same_thread=False)
            self.db.row_factory=sqlite3.Row;self.db.execute('PRAGMA synchronous=FULL');self.db.execute('PRAGMA journal_mode=DELETE')
            if not existing:
                self.db.executescript('BEGIN IMMEDIATE;'+SCHEMA)
                self.db.executemany('INSERT INTO meta VALUES(?,?)',[('schema','1'),('key',secrets.token_hex(32)),('feedFloor','0')]);self.db.commit()
            if self.db.execute('SELECT value FROM meta WHERE key=?',('schema',)).fetchone()[0]!='1':fail('UNSUPPORTED_SCHEMA',503)
            self.key=bytes.fromhex(self.db.execute('SELECT value FROM meta WHERE key=?',('key',)).fetchone()[0])
            for area in ('staging','blobs'):
                try:os.mkdir(area,0o700,dir_fd=self.fd)
                except FileExistsError:pass
                fd=openat2(self.fd,area,os.O_RDONLY|os.O_DIRECTORY);os.close(fd)
            os.chmod('catalog.sqlite3',0o600,dir_fd=self.fd,follow_symlinks=False);os.fsync(self.fd)
        except Exception:self.close();raise

    @contextmanager
    def transaction(self):
        with self.lock:
            self.ready()
            with self.db:
                yield
                allocated=self.db.execute('PRAGMA page_count').fetchone()[0]*self.db.execute('PRAGMA page_size').fetchone()[0]
                if allocated>LIMITS['metadataBytes']:fail('METADATA_QUOTA',507)

    def row(self,sql,args=()):
        value=self.db.execute(sql,args).fetchone();return dict(value) if value else None
    def rows(self,sql,args=()):return [dict(v) for v in self.db.execute(sql,args)]
    def attachments(self,delivery):return self.rows('SELECT * FROM attachments WHERE delivery=? ORDER BY id',(delivery,))
    def delivery(self,delivery,actor=None):
        value=self.row('SELECT * FROM deliveries WHERE id=?',(delivery,))
        if not value or (actor is not None and value['actor']!=actor):fail('NOT_FOUND',404)
        return value
    def visible(self,d):
        if d['state'] in ('deleted','expired'):fail('DELIVERY_GONE',410)
        if d['state']!='ready':fail('NOT_FOUND',404)
        if d['expires']<=self.clock():fail('DELIVERY_EXPIRED',410)
    def draft(self,d):
        if d['state'] in ('deleted','expired') or (d['state']=='uploading' and d['draft_expires']<=self.clock()):fail('DELIVERY_GONE',410)
        if d['state']!='uploading':fail('UPLOAD_CONFLICT',409)
    def fingerprint(self,value):return hmac.new(self.key,encode(value).encode(),hashlib.sha256).hexdigest()
    def usage(self):return self.db.execute("SELECT COALESCE(SUM(bytes),0) FROM deliveries WHERE state IN ('uploading','ready') OR cleanup=1").fetchone()[0]
    def free_check(self,extra=0):
        capacity=os.fstatvfs(self.fd)
        if capacity.f_bavail*capacity.f_frsize<extra+LIMITS['minFreeBytes']:fail('STORAGE_QUOTA',507)
    def sync_area(self,area):
        fd=openat2(self.fd,area,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    @staticmethod
    def upload_attachment(a):return {'attachmentId':a['id'],'receivedBytes':a['received'],'byteLength':a['size'],'complete':a['received']==a['size']}
    def upload_result(self,d,prepared=False):
        attachments=[]
        for a in self.attachments(d['id']):
            row=self.upload_attachment(a)
            if prepared:row['clientAttachmentId']=json.loads(a['body'])['clientAttachmentId']
            attachments.append(row)
        result={'deliveryId':d['id'],'state':d['state'],'attachments':attachments}
        if prepared:result.update(requestId=d['request'],uploadExpiresAt=stamp(d['draft_expires']) if d['state']=='uploading' else None)
        return result

    def prepare(self,actor,raw):
        args=validate_prepare(raw)
        with self.transaction():
            fp=self.fingerprint(args);prior=self.row('SELECT * FROM deliveries WHERE actor=? AND request=?',(actor,args['requestId']))
            if prior:
                if not hmac.compare_digest(prior['fingerprint'],fp):fail('IDEMPOTENCY_CONFLICT',409)
                if prior['state'] in ('expired','deleted'):fail('DELIVERY_GONE',410)
                return self.upload_result(prior,True),False
            if self.db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0]>=LIMITS['maxOperations']:fail('OPERATION_QUOTA',507)
            if self.db.execute("SELECT COUNT(*) FROM deliveries WHERE state='uploading'").fetchone()[0]>=LIMITS['maxDrafts']:fail('UPLOAD_QUOTA',429)
            size=sum(a['byteLength'] for a in args['attachments'])
            if self.usage()+size>self.quota:fail('INBOX_QUOTA',507)
            self.free_check(size);now=self.clock();delivery=ident()
            self.db.execute('INSERT INTO deliveries (id,actor,request,inbox,state,created,draft_expires,bytes,body,fingerprint) VALUES(?,?,?,?,?,?,?,?,?,?)',(delivery,actor,args['requestId'],args['inboxId'],'uploading',now,now+LIMITS['draftSeconds'],size,encode(args),fp))
            for a in args['attachments']:
                aid=ident();fd=openat2(self.fd,'staging/'+aid,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                try:os.fsync(fd)
                finally:os.close(fd)
                self.db.execute('INSERT INTO attachments(id,delivery,size,hash,body) VALUES(?,?,?,?,?)',(aid,delivery,a['byteLength'],a['sha256'],encode(a)))
            self.sync_area('staging');self.fault('prepare-before-commit')
            return self.upload_result(self.delivery(delivery),True),True

    def uploads(self,actor,delivery):
        with self.transaction():
            d=self.delivery(delivery,actor)
            if d['state']=='uploading':self.draft(d)
            return self.upload_result(d)

    def operation(self,actor,request):
        with self.transaction():
            d=self.row('SELECT * FROM deliveries WHERE actor=? AND request=?',(actor,str(uuid.UUID(request))))
            if not d:fail('NOT_FOUND',404)
            receipts=[json.loads(r['body']) for r in self.rows('SELECT body FROM receipts WHERE delivery=? ORDER BY device',(d['id'],))]
            return {'requestId':d['request'],'deliveryId':d['id'],'state':d['state'],'uploadExpiresAt':stamp(d['draft_expires']) if d['state']=='uploading' else None,'publishedAt':stamp(d['published']),'expiresAt':stamp(d['expires']),'attachments':[self.upload_attachment(a) for a in self.attachments(d['id'])],'receipts':receipts}

    def chunk(self,actor,delivery,attachment,range_header,chunk_hash,data,guard=lambda:None):
        match=re.fullmatch(r'bytes ([0-9]{1,16})-([0-9]{1,16})/([0-9]{1,16})',range_header or '')
        if not match or not re.fullmatch('[0-9a-f]{64}',chunk_hash or '') or not isinstance(data,bytes):fail('INVALID_CHUNK')
        start,end,total=map(int,match.groups());length=end-start+1
        if not 1<=length<=LIMITS['chunkBytes'] or length!=len(data) or end>=total or start%LIMITS['chunkBytes'] or (length!=LIMITS['chunkBytes'] and end+1!=total):fail('INVALID_CHUNK')
        if hashlib.sha256(data).hexdigest()!=chunk_hash:fail('CHUNK_HASH_MISMATCH',409)
        with self.transaction():
            d=self.delivery(delivery,actor);self.draft(d)
            a=self.row('SELECT * FROM attachments WHERE id=? AND delivery=?',(attachment,delivery))
            if not a:fail('NOT_FOUND',404)
            if total!=a['size']:fail('UPLOAD_CONFLICT',409)
            old=self.row('SELECT * FROM chunks WHERE attachment=? AND start=?',(attachment,start))
            if old:
                if old['length']!=length or old['hash']!=chunk_hash:fail('UPLOAD_CONFLICT',409)
                return self.upload_attachment(a)
            if start!=a['received']:fail('UPLOAD_CONFLICT',409,expectedOffset=a['received'])
            self.free_check(length)
            fd=openat2(self.fd,'staging/'+attachment,os.O_RDWR)
            try:
                info=os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:fail('STORAGE_UNAVAILABLE',503)
                os.ftruncate(fd,start);view=memoryview(data);position=start
                while view:
                    written=os.pwrite(fd,view,position)
                    if written<=0:fail('STORAGE_UNAVAILABLE',503)
                    position+=written;view=view[written:]
                os.fsync(fd);self.fault('chunk-before-offset')
            finally:os.close(fd)
            guard()
            self.db.execute('UPDATE attachments SET received=? WHERE id=?',(end+1,attachment))
            self.db.execute('INSERT INTO chunks VALUES(?,?,?,?)',(attachment,start,length,chunk_hash));a['received']=end+1
            return self.upload_attachment(a)

    @staticmethod
    def signature(info):return encode([info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns])

    def verify_file(self,area,a):
        fd=openat2(self.fd,area+'/'+a['id'],os.O_RDONLY|os.O_NONBLOCK)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size!=a['size']:fail('CONTENT_HASH_MISMATCH',409)
            digest=hashlib.sha256()
            while True:
                data=os.read(fd,1048576)
                if not data:break
                digest.update(data)
            if digest.hexdigest()!=a['hash'] or self.signature(info)!=self.signature(os.fstat(fd)):fail('CONTENT_HASH_MISMATCH',409)
            os.fsync(fd)
            return self.signature(info)
        finally:os.close(fd)

    @staticmethod
    def publication(d):return {'deliveryId':d['id'],'requestId':d['request'],'state':d['state'],'publishedAt':stamp(d['published']),'expiresAt':stamp(d['expires'])}

    def summary(self,d):
        body=json.loads(d['body'])
        return {'id':d['id'],'inboxId':d['inbox'],'senderLabel':body['senderLabel'],'title':body['title'],'publishedAt':stamp(d['published']),'expiresAt':stamp(d['expires']),'attachmentCount':len(body['attachments']),'linkCount':len(body['links']),'totalBytes':d['bytes']}

    def event(self,d,kind):
        self.db.execute('INSERT INTO events(id,inbox,kind,delivery,at,summary) VALUES(?,?,?,?,?,?)',(ident(),d['inbox'],kind,d['id'],self.clock(),encode(self.summary(d)) if kind=='created' else None))

    def commit(self,actor,delivery,request,guard=lambda:None):
        with self.transaction():
            d=self.delivery(delivery,actor)
            if d['request']!=str(uuid.UUID(request)):fail('IDEMPOTENCY_CONFLICT',409)
            if d['state']=='ready':
                self.visible(d);return self.publication(d)
            self.draft(d);attachments=self.attachments(delivery)
            if any(a['received']!=a['size'] for a in attachments):fail('UPLOAD_INCOMPLETE',409)
            for a in attachments:
                try:fd=openat2(self.fd,'blobs/'+a['id'],os.O_RDONLY|os.O_NONBLOCK);os.close(fd);area='blobs'
                except BackendError as exc:
                    if exc.code!='NOT_FOUND':raise
                    area='staging'
                signature=self.verify_file(area,a)
                if area=='staging':os.rename('staging/'+a['id'],'blobs/'+a['id'],src_dir_fd=self.fd,dst_dir_fd=self.fd)
                # Rename can change ctime; bind the final identity after finalization.
                fd=openat2(self.fd,'blobs/'+a['id'],os.O_RDONLY)
                try:signature=self.signature(os.fstat(fd))
                finally:os.close(fd)
                self.db.execute('UPDATE attachments SET signature=? WHERE id=?',(signature,a['id']))
                self.fault('commit-after-file')
            self.sync_area('staging');self.sync_area('blobs');self.fault('commit-after-files')
            guard();self.check();now=self.clock();body=json.loads(d['body'])
            d.update(state='ready',published=now,expires=now+body['retentionDays']*86400)
            self.db.execute("UPDATE deliveries SET state='ready',published=?,expires=? WHERE id=?",(d['published'],d['expires'],delivery))
            self.event(d,'created');self.fault('commit-before-catalog')
            return self.publication(d)

    def terminal(self,d,state):
        was_ready=d['state']=='ready'
        self.db.execute('UPDATE deliveries SET state=?,body=?,cleanup=1 WHERE id=?',(state,'{}',d['id']))
        self.db.execute('UPDATE attachments SET body=? WHERE delivery=?',('{}',d['id']))
        self.db.execute('DELETE FROM chunks WHERE attachment IN (SELECT id FROM attachments WHERE delivery=?)',(d['id'],))
        if was_ready:self.event(d,state)

    def delete(self,actor,delivery,request):
        request=str(uuid.UUID(request))
        with self.transaction():
            prior=self.row('SELECT delivery FROM deletes WHERE actor=? AND request=?',(actor,request))
            if prior:
                if prior['delivery']!=delivery:fail('IDEMPOTENCY_CONFLICT',409)
                return {'deliveryId':delivery,'state':'deleted'}
            d=self.delivery(delivery)
            if d['state']=='expired':fail('DELIVERY_GONE',410)
            if self.db.execute('SELECT COUNT(*) FROM deletes').fetchone()[0]>=LIMITS['maxOperations']:fail('OPERATION_QUOTA',507)
            if d['state']!='deleted':self.terminal(d,'deleted')
            self.db.execute('INSERT INTO deletes VALUES(?,?,?)',(actor,request,delivery));self.fault('delete-before-commit')
            return {'deliveryId':delivery,'state':'deleted'}

    def maintain(self):
        with self.transaction():
            now=self.clock()
            rows=self.rows("SELECT * FROM deliveries WHERE (state='uploading' AND draft_expires<=?) OR (state='ready' AND expires<=?) LIMIT 100",(now,now))
            for d in rows:self.terminal(d,'expired')
            cutoff=now-LIMITS['feedRetentionDays']*86400
            first_live=self.db.execute('SELECT MIN(seq) FROM events WHERE at>=?',(cutoff,)).fetchone()[0]
            expired=self.row('SELECT MAX(seq) AS seq FROM events WHERE at<? AND seq<?',(cutoff,first_live if first_live is not None else 2**63-1))
            if expired['seq'] is not None:
                self.db.execute('UPDATE meta SET value=? WHERE key=?',(str(expired['seq']),'feedFloor'))
                self.db.execute('DELETE FROM events WHERE seq<=?',(expired['seq'],))
        with self.transaction():
            for d in self.rows('SELECT id FROM deliveries WHERE cleanup=1 LIMIT 100'):
                for a in self.attachments(d['id']):
                    for area in ('staging','blobs'):
                        try:os.unlink(area+'/'+a['id'],dir_fd=self.fd)
                        except FileNotFoundError:pass
                self.sync_area('staging');self.sync_area('blobs');self.fault('cleanup-before-catalog')
                self.db.execute('UPDATE deliveries SET cleanup=0 WHERE id=?',(d['id'],))
            # Recover files created before a failed prepare transaction. Bounded scan
            # iterators progress between maintenance passes instead of rescanning a prefix.
            for area in ('staging','blobs'):
                scans=getattr(self,'scans',{});self.scans=scans
                if area not in scans:
                    fd=openat2(self.fd,area,os.O_RDONLY|os.O_DIRECTORY)
                    try:scans[area]=os.scandir(fd)
                    finally:os.close(fd)
                for _ in range(256):
                    item=next(scans[area],None)
                    if item is None:scans.pop(area).close();break
                    if not re.fullmatch(r'[0-9a-f-]{36}',item.name):continue
                    if not self.row('SELECT id FROM attachments WHERE id=?',(item.name,)):
                        os.unlink(area+'/'+item.name,dir_fd=self.fd)
                self.sync_area(area)

    def checkpoint(self):
        row=self.row("SELECT seq FROM sqlite_sequence WHERE name='events'")
        return row['seq'] if row else 0

    def cursor(self,payload):
        raw=encode(payload).encode();body=base64.urlsafe_b64encode(raw).decode().rstrip('=')
        return body+'.'+hmac.new(self.key,body.encode(),hashlib.sha256).hexdigest()

    def uncursor(self,token,actor,inbox,kind):
        try:
            if len(token)>2048:raise ValueError()
            body,signature=token.split('.')
            if not hmac.compare_digest(signature,hmac.new(self.key,body.encode(),hashlib.sha256).hexdigest()):raise ValueError()
            value=json.loads(base64.urlsafe_b64decode(body+'='*(-len(body)%4)))
            if value['actor']!=actor or value['inbox']!=inbox or value['type']!=kind:raise ValueError()
        except (ValueError,KeyError,TypeError,UnicodeError):fail('INVALID_CURSOR',409)
        if value['expires']<=self.clock():fail('CURSOR_EXPIRED',410)
        return value

    def feed_cursor(self,actor,inbox,seq):
        return self.cursor({'type':'feed','actor':actor,'inbox':inbox,'seq':seq,'expires':self.clock()+90*86400})

    def listing(self,actor,inbox,limit=50,cursor=None):
        with self.transaction():
            generation=self.checkpoint();now=self.clock()
            if cursor:
                state=self.uncursor(cursor,actor,inbox,'snapshot')
                if state['limit']!=limit:fail('SNAPSHOT_CHANGED',409)
                if state['generation']!=generation or (state['minExpiry'] is not None and state['minExpiry']<=now):fail('SNAPSHOT_CHANGED',409)
            else:
                first_expiry=self.db.execute("SELECT MIN(expires) FROM deliveries WHERE inbox=? AND state='ready' AND expires>?",(inbox,now)).fetchone()[0]
                state={'type':'snapshot','actor':actor,'inbox':inbox,'generation':generation,'limit':limit,'expires':now+LIMITS['snapshotSeconds'],'last':None,'minExpiry':first_expiry,'sync':self.feed_cursor(actor,inbox,generation)}
            sql="SELECT * FROM deliveries WHERE inbox=? AND state='ready' AND expires>?";params=[inbox,now]
            if state['last']:
                published,delivery=state['last'];sql+=' AND (published<? OR (published=? AND id<?))';params.extend([published,published,delivery])
            rows=self.rows(sql+' ORDER BY published DESC,id DESC LIMIT ?',tuple(params+[limit+1]));page=rows[:limit]
            next_cursor=None
            if len(rows)>limit:
                state['last']=[page[-1]['published'],page[-1]['id']];next_cursor=self.cursor(state)
            return {'items':[self.summary(d) for d in page],'nextCursor':next_cursor,'syncCursor':state['sync']}

    def changes(self,actor,inbox,cursor,limit=100):
        with self.transaction():
            state=self.uncursor(cursor,actor,inbox,'feed');floor=int(self.db.execute("SELECT value FROM meta WHERE key='feedFloor'").fetchone()[0])
            if state['seq']<floor:fail('CURSOR_EXPIRED',410)
            rows=self.rows('SELECT * FROM events WHERE inbox=? AND seq>? ORDER BY seq LIMIT ?',(inbox,state['seq'],limit+1));page=rows[:limit]
            seq=page[-1]['seq'] if page else state['seq']
            return {'events':[{'eventId':r['id'],'kind':r['kind'],'deliveryId':r['delivery'],'at':stamp(r['at']),'summary':json.loads(r['summary']) if r['summary'] else None} for r in page], 'nextCursor':self.feed_cursor(actor,inbox,seq),'hasMore':len(rows)>limit}

    def metadata(self,delivery):
        with self.transaction():
            d=self.delivery(delivery);self.visible(d);body=json.loads(d['body'])
            attachments=[]
            for a in self.attachments(delivery):
                meta=json.loads(a['body']);attachments.append({'id':a['id'],'filename':meta['filename'],'mediaType':meta['mediaType'],'byteLength':a['size'],'sha256':a['hash']})
            receipts=[json.loads(r['body']) for r in self.rows('SELECT body FROM receipts WHERE delivery=? ORDER BY device',(delivery,))]
            return {'id':delivery,'inboxId':d['inbox'],'state':'ready','senderLabel':body['senderLabel'],'title':body['title'],'message':body['message'],'publishedAt':stamp(d['published']),'expiresAt':stamp(d['expires']),'links':body['links'],'attachments':attachments,'receipts':receipts}

    def device(self,actor,inbox,device,args):
        device=str(uuid.UUID(device))
        if args['pushToken'] is not None:fail('PUSH_UNSUPPORTED',400,'Server push is disabled; use inbox synchronization')
        with self.transaction():
            prior=self.row('SELECT * FROM devices WHERE id=?',(device,))
            if prior and (prior['actor']!=actor or prior['inbox']!=inbox):fail('NOT_FOUND',404)
            if not prior and self.db.execute('SELECT COUNT(*) FROM devices').fetchone()[0]>=LIMITS['maxDevices']:fail('DEVICE_QUOTA',507)
            self.db.execute('INSERT OR REPLACE INTO devices VALUES(?,?,?,?)',(device,actor,inbox,encode({'platform':'android','displayName':args['displayName'],'notificationsEnabled':args['notificationsEnabled']})))
            return {'deviceId':device,'inboxId':inbox,'registered':True,'pushEnabled':False}

    def receipt(self,actor,delivery,device,args):
        device=str(uuid.UUID(device))
        with self.transaction():
            d=self.delivery(delivery);self.visible(d);registered=self.row('SELECT * FROM devices WHERE id=?',(device,))
            if not registered or registered['actor']!=actor or registered['inbox']!=d['inbox']:fail('DEVICE_NOT_REGISTERED',404)
            attachments={a['id'] for a in self.attachments(delivery)};ack=set(args.get('downloadedAttachmentIds',[]))
            if not ack<=attachments:fail('INVALID_ATTACHMENT')
            prior=self.row('SELECT body FROM receipts WHERE delivery=? AND device=?',(delivery,device))
            value=json.loads(prior['body']) if prior else {'deviceId':device,'downloadedAttachmentIds':[],'downloadedAt':None,'openedAt':None}
            value['downloadedAttachmentIds']=sorted(set(value['downloadedAttachmentIds'])|ack);now=stamp(self.clock())
            if set(value['downloadedAttachmentIds'])==attachments and value['downloadedAt'] is None:value['downloadedAt']=now
            if args.get('opened',False) and value['openedAt'] is None:value['openedAt']=now
            self.db.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?)',(delivery,device,encode(value)))
            return value

    def content(self,delivery,attachment,range_header=None,if_range=None):
        """Return a pinned fd. Caller must close it even on client disconnect."""
        with self.transaction():
            d=self.delivery(delivery);self.visible(d)
            a=self.row('SELECT * FROM attachments WHERE id=? AND delivery=?',(attachment,delivery))
            if not a:fail('NOT_FOUND',404)
            etag='"'+a['hash']+'"';size=a['size'];start=0;length=size;status=200
            if range_header:
                match=re.fullmatch(r'bytes=([0-9]{0,16})-([0-9]{0,16})',range_header)
                if not match or not any(match.groups()):fail('INVALID_RANGE',416)
                if if_range is None or if_range==etag:
                    first,last=match.groups()
                    if size==0:fail('INVALID_RANGE',416)
                    if first:
                        start=int(first);end=int(last) if last else size-1
                        if start>=size or end<start:fail('INVALID_RANGE',416)
                        end=min(end,size-1)
                    else:
                        suffix=int(last)
                        if suffix==0:fail('INVALID_RANGE',416)
                        start=max(0,size-suffix);end=size-1
                    length=end-start+1;status=206
            fd=openat2(self.fd,'blobs/'+attachment,os.O_RDONLY|os.O_NONBLOCK)
            try:
                info=os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or self.signature(info)!=a['signature']:fail('CONTENT_UNAVAILABLE',503)
                os.lseek(fd,start,os.SEEK_SET)
                meta=json.loads(a['body'])
                headers={'X-Content-Type-Options':'nosniff','ETag':etag,'Accept-Ranges':'bytes','Content-Type':'application/octet-stream'}
                # Only syntactically valid MIME labels are emitted as headers.
                if re.fullmatch(r'[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+',meta['mediaType']):headers['Content-Type']=meta['mediaType']
                from urllib.parse import quote
                clean=''.join(c for c in meta['filename'] if ord(c)>=32 and ord(c)!=127).replace('\\','_').replace('/','_') or 'attachment'
                headers['Content-Disposition']='attachment; filename="attachment"; filename*=UTF-8\'\''+quote(clean,safe='')
                if status==206:headers['Content-Range']=f'bytes {start}-{start+length-1}/{size}'
                return status,length,headers,fd
            except Exception:os.close(fd);raise
