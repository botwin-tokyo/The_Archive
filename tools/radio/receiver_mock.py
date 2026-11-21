"""Local protocol reference receiver; test-only loopback HTTP, not production hosting.
Run with --root DIR --key-file FILE --port 18800. Never prints credentials.
"""
import argparse
from datetime import datetime,timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import uuid

PREFIX='/archive-radio/v1'


def sync_dir(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


class Store:
    def __init__(self,root,token,receiver_id='archive-radio-station',fault=lambda point:None):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.token=token;self.receiver_id=receiver_id;self.fault=fault;self.lock=threading.Lock()
        self.db=sqlite3.connect(self.root/'receiver.sqlite',check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY,sha TEXT,size TEXT,receipt TEXT)');self.db.commit()
        # A manifest is committed BEFORE publication. Reconcile a published file
        # with an uncommitted receipt after a crash, without a second registration.
        for did,sha,size,receipt in self.db.execute('SELECT * FROM deliveries').fetchall():
            p=self.root/(did+'.mp3')
            if not receipt and p.exists() and p.stat().st_size==int(size):
                with p.open('rb') as f:actual=hashlib.file_digest(f,'sha256').hexdigest()
                if actual==sha:self.commit(did,sha,size)
    def commit(self,did,sha,size):
        receipt={'version':1,'receiverId':self.receiver_id,'deliveryId':did,'trackId':'track-'+did,
                 'sha256':sha,'sizeBytes':size,'receivedAt':datetime.now(timezone.utc).isoformat()}
        self.db.execute('UPDATE deliveries SET receipt=? WHERE id=?',(json.dumps(receipt),did));self.db.commit()
        return receipt
    def close(self):self.db.close()


def server(store,port=0):
    class Handler(BaseHTTPRequestHandler):
        protocol_version='HTTP/1.1'
        def log_message(self,*args):pass
        def reply(self,status,value):
            data=json.dumps(value).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.send_header('Connection','close');self.end_headers();self.wfile.write(data);self.close_connection=True
        def auth(self):
            if not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+store.token):
                self.reply(401,{'error':'unauthorized'});return False
            return True
        def do_GET(self):
            if not self.auth():return
            if self.path==PREFIX+'/capabilities':return self.reply(200,{'version':1,'receiverId':store.receiver_id,'contentTypes':['audio/mpeg'],'maxUploadBytes':str(1024**3)})
            match=re.fullmatch(PREFIX+r'/deliveries/([0-9a-f-]{36})',self.path)
            if not match:return self.reply(404,{'error':'not_found'})
            did=match[1]
            with store.lock:
                row=store.db.execute('SELECT receipt FROM deliveries WHERE id=?',(did,)).fetchone()
            if row and row[0]:return self.reply(200,json.loads(row[0]))
            # No concurrent upload is in progress once the lock is acquired.
            return self.reply(404,{'error':'not_found'})
        def do_PUT(self):
            if not self.auth():return
            match=re.fullmatch(PREFIX+r'/deliveries/([0-9a-f-]{36})/content',self.path)
            if not match:return self.reply(404,{'error':'not_found'})
            did=match[1]
            try:
                if str(uuid.UUID(did))!=did:raise ValueError()
                size=self.headers.get('Content-Length','');length=int(size);sha=self.headers.get('X-Archive-SHA256','')
                if str(length)!=size or not 0<length<=1024**3 or not re.fullmatch('[0-9a-f]{64}',sha) or self.headers.get('Content-Type')!='audio/mpeg' or self.headers.get('Transfer-Encoding'):raise ValueError()
            except ValueError:return self.reply(422,{'error':'invalid_upload'})
            with store.lock:
                row=store.db.execute('SELECT sha,size,receipt FROM deliveries WHERE id=?',(did,)).fetchone()
                if row:
                    if row[0]!=sha or row[1]!=size:return self.reply(409,{'error':'delivery_conflict'})
                    if row[2]:return self.reply(200,json.loads(row[2]))
                store.db.execute('INSERT OR IGNORE INTO deliveries VALUES(?,?,?,NULL)',(did,sha,size));store.db.commit()
                temp=store.root/(did+'.part');target=store.root/(did+'.mp3');hashed=hashlib.sha256();remaining=length
                self.connection.settimeout(5)
                try:
                    with temp.open('wb') as f:
                        while remaining:
                            data=self.rfile.read(min(65536,remaining))
                            if not data:raise ConnectionError('incomplete')
                            f.write(data);hashed.update(data);remaining-=len(data)
                        f.flush();os.fsync(f.fileno())
                    if hashed.hexdigest()!=sha:return self.reply(422,{'error':'checksum_mismatch'})
                    from archive_backend.radio_sources import probe_mp3
                    try:probe_mp3(temp)
                    except Exception:return self.reply(422,{'error':'invalid_mp3'})
                    os.replace(temp,target);sync_dir(store.root);store.fault('published')
                    receipt=store.commit(did,sha,size);store.fault('committed')
                    return self.reply(201,receipt)
                except (OSError,RuntimeError):
                    self.close_connection=True
                    return
    return ThreadingHTTPServer(('127.0.0.1',port),Handler)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);parser.add_argument('--key-file',required=True);parser.add_argument('--port',type=int,default=18800);parser.add_argument('--receiver-id',default='archive-radio-station');args=parser.parse_args()
    from archive_backend.radio_receiver_client import read_secret
    store=Store(args.root,read_secret(args.key_file),args.receiver_id);http=server(store,args.port)
    try:http.serve_forever()
    finally:http.server_close();store.close()
