"""UID-authenticated Git acquisition worker; never writes Archive-managed roots."""
import argparse
import copy
import fcntl
import json
import os
from pathlib import Path
import shutil
import socket
import socketserver
import struct
import threading
import time
from .contracts import BackendError
from .git_config import settings
from .git_provider import GitHub, read_secret
from .git_engine import GitEngine, atomic_json
from .volumes import VolumeConfig, VolumeRegistry


class Worker:
    def __init__(self, config):
        self.config=config;self.root=Path(config['root']);self.state_path=Path(config['state'])
        self.lock=threading.RLock();self.cancel=threading.Event();self.thread=None
        self.state=json.loads(self.state_path.read_text()) if self.state_path.exists() else {'repositories':{},'run':None,'history':[]}
        if self.state.get('run',{} ) and self.state['run']['state']=='running':
            self.state['run']['state']='interrupted';self.save()
        self.registry=None;self.deadline=None
    def verify(self):
        if self.deadline and time.monotonic()>self.deadline:raise BackendError('INTERRUPTED','Git backup exceeded its twelve-hour deadline')
        if self.registry is None:
            v=self.config['volume']; self.registry=VolumeRegistry([VolumeConfig(v['id'],'Git backup',str(self.root),v['mountPoint'],v['filesystemUuid'],'ext4',tuple(v['expectedDevice']),v['expectedMountSource'])])
        capacity=self.registry.get('git-worker').capacity()
        if capacity['read_only'] or capacity['available']<self.config.get('reserveBytes',10*1024**3):
            raise BackendError('FULL_DISK','Git volume is read-only or below its free-space reserve')
    def save(self):atomic_json(self.state_path,self.state)
    def status(self):
        with self.lock:return copy.deepcopy(self.state)
    def dispatch(self, request):
        action=request.get('action')
        if action=='status':return self.status()
        if action=='ack':
            with self.lock:
                repo=self.state['repositories'].get(request.get('id'),{})
                artifact=request.get('artifact')
                if repo.get('artifact')!=artifact: return {'accepted':False}
                if self.thread and self.thread.is_alive():return {'accepted':False}
                for folder in (self.root/'generations').iterdir():
                    manifest=folder/'manifest.json'
                    if folder.name==artifact or not manifest.is_file():continue
                    old=json.loads(manifest.read_text())
                    if old.get('id')==request['id']:shutil.rmtree(folder)
                return {'accepted':True}
        if action=='cancel':self.cancel.set();return {'accepted':True}
        if action=='preview':
            self.verify();return GitHub(read_secret(self.config['secret'])).discover(request.get('scope','owned'))
        if action=='run':
            policy=settings(request['settings']);run_id=request['id']
            if not isinstance(run_id,str) or not 1<=len(run_id)<=128:raise BackendError('INVALID_ARGUMENT','Invalid run ID')
            with self.lock:
                if self.thread and self.thread.is_alive():return self.state['run']
                if any(r['id']==run_id for r in self.state['history']) or (self.state['run'] and self.state['run']['id']==run_id):return self.state['run']
                self.cancel.clear();self.state['run']={'id':run_id,'state':'running','startedAt':time.time(),'completed':0,'failed':0,'total':0}
                self.save();self.thread=threading.Thread(target=self.run,args=(policy,request.get('onlyIds')),daemon=True);self.thread.start()
                return copy.deepcopy(self.state['run'])
        raise BackendError('INVALID_ARGUMENT','Unknown Git worker action')
    def run(self, policy, only_ids=None):
        self.deadline=time.monotonic()+43200
        try:
            self.verify();inventory=GitHub(read_secret(self.config['secret'])).discover(policy['scope'])
            with self.lock:
                self.state['identity']=inventory['login'];self.state['run']['total']=len(inventory['repositories']);self.state['discoveredAt']=inventory['discoveredAt']
                found={r['id'] for r in inventory['repositories']}
                for key,old in self.state['repositories'].items():
                    if key not in found:old['availability']='not-discovered'
                self.save()
            if only_ids is not None:
                inventory['repositories']=[r for r in inventory['repositories'] if r['id'] in only_ids]
                with self.lock:self.state['run']['total']=len(inventory['repositories']);self.save()
            engine=GitEngine(self.root,secret=self.config['secret'],cancel=self.cancel,verify=self.verify)
            for repo in inventory['repositories']:
                if self.cancel.is_set():break
                with self.lock:self.state['run']['current']=repo['fullName'];self.save()
                try:
                    result=engine.acquire(repo,policy['retention'],policy['branchOverrides'].get(repo['id']))
                    with self.lock:
                        self.state['repositories'][repo['id']]=dict(result,availability='available',runId=self.state['run']['id'])
                        self.state['run']['completed']+=1;self.save()
                except Exception as error:
                    with self.lock:
                        old=self.state['repositories'].get(repo['id'],repo)
                        self.state['repositories'][repo['id']]=dict(old,lastError=error.message if isinstance(error,BackendError) else 'Repository acquisition failed',availability='failed',runId=self.state['run']['id'])
                        self.state['run']['failed']+=1;self.save()
            with self.lock:
                self.state['run']['state']='canceled' if self.cancel.is_set() else 'partial' if self.state['run']['failed'] else 'completed'
        except Exception as error:
            with self.lock:
                self.state['run']['state']='failed';self.state['run']['error']=error.message if isinstance(error,BackendError) else 'Git backup worker failed'
        finally:
            with self.lock:
                self.deadline=None
                self.state['run']['finishedAt']=time.time();self.state['history']=(self.state['history']+[copy.deepcopy(self.state['run'])])[-30:];self.save()


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        _,uid,_=struct.unpack('3i',self.request.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
        if uid not in (0,self.server.owner_uid):return
        self.request.settimeout(180)
        try:
            raw=self.rfile.readline(128*1024+1)
            if len(raw)>128*1024:raise ValueError()
            result=self.server.worker.dispatch(json.loads(raw));response={'ok':True,'result':result}
        except Exception as e:response={'ok':False,'message':e.message if isinstance(e,BackendError) else 'Worker request failed'}
        self.wfile.write(json.dumps(response).encode()+b'\n')


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads=True


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);args=parser.parse_args()
    config=json.loads(Path(args.config).read_text());os.umask(0o027)
    lock=open(config['state']+'.lock','a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    worker=Worker(config);path=Path(config['socket']);path.unlink(missing_ok=True)
    server=Server(str(path),Handler);server.worker=worker;server.owner_uid=config['ownerUid'];os.chmod(path,0o660)
    server.serve_forever()

if __name__=='__main__':main()
