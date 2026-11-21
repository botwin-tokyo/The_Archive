"""Core coordinator and owner API for Git backup acquisition/publication."""
import datetime
import json
import os
from pathlib import Path
import socket
import socketserver
import struct
import threading
import time
import uuid
from zoneinfo import ZoneInfo
from .contracts import BackendError, identifier, digest
from .git_config import DEFAULTS, settings
from .git_publish import Publisher


def rpc(path,uid,request,timeout=10):
    try:
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout);connection.connect(path)
            _,peer,_=struct.unpack('3i',connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
            if peer!=uid:raise BackendError('PERMISSION_DENIED','Git socket peer identity differs')
            connection.sendall(json.dumps(request).encode()+b'\n')
            with connection.makefile('rb') as f:raw=f.readline(8*1024*1024+1)
            if len(raw)>8*1024*1024:raise ValueError()
            response=json.loads(raw)
            if not response.get('ok'):raise BackendError('UNAVAILABLE',response.get('message','Git worker unavailable'))
            return response['result']
    except BackendError:raise
    except (OSError,ValueError):raise BackendError('UNAVAILABLE','Git backup worker is unavailable; saved files are unchanged') from None


class GitService:
    def __init__(self,service,config):
        self.service=service;self.config=config;self.lock=threading.RLock();self.stop=threading.Event();self.thread=None;self.control=None
        self.publisher=Publisher(service,config) if config else None
        self.last_error=None;self.preview=None;self.previewing=False
    def worker(self,**request):
        if not self.config:raise BackendError('UNAVAILABLE','Git backup is not configured')
        return rpc(self.config['socket'],self.config['workerUid'],request,timeout=150 if request.get('action')=='preview' else 10)
    def policy(self):return self.service.state.get('git_settings','current',dict(DEFAULTS))
    def trigger(self,request_id,scheduled=False,retry_failed=False):
        identifier(request_id)
        with self.lock:
            policy=self.policy()
            if scheduled and not policy['enabled']:return {'state':'disabled'}
            if not self.config:raise BackendError('UNAVAILABLE','Git backup is not configured')
            self.service.registry.get('git').check()
            active=self.service.state.get('git_run','active')
            prior=self.service.state.get('git_requests',digest(request_id))
            if prior:return prior
            if active and active['state'] in ('queued','running','publishing'):return active
            retry_ids=None
            if retry_failed:
                inventory=self.service.state.get('git_inventory','current',{}).get('repositories',{})
                retry_ids=[key for key,r in inventory.items() if r.get('availability')=='failed' or r.get('coverage') or self.service.state.get('git_repo_status',key,{}).get('status')=='publication-failed']
                if not retry_ids:raise BackendError('INVALID_ARGUMENT','No failed or partial repositories to retry')
            row={'id':uuid.uuid4().hex,'requestId':request_id,'state':'queued','startedAt':time.time(),'settings':policy,'onlyIds':retry_ids}
            self.service.state.put('git_requests',digest(request_id),row)
            self.service.state.put('git_run','active',row)
            return row
    def status(self):
        policy=self.policy();now=datetime.datetime.now(ZoneInfo(policy['timezone']))
        tomorrow=(now+datetime.timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
        inventory=self.service.state.get('git_inventory','current',{'repositories':{},'history':[]})
        publications={p['id']:p for p in self.service.state.list('git_repo_status',limit=10000)}
        repos=[]
        for key,row in inventory.get('repositories',{}).items():
            # Never expose filesystem paths or provider credential configuration.
            public={k:row.get(k) for k in ('id','fullName','selectedBranch','commit','status','availability','coverage','completedAt','lastError','files','bytes')}
            public.update(publications.get(key,{}))
            if row.get('availability')=='failed':public.update(status='acquisition-failed',lastError=row.get('lastError'))
            repos.append(public)
        return {'configured':bool(self.config),'settings':policy,'identity':inventory.get('identity'),
                'run':self.service.state.get('git_run','active'),'repositories':repos,
                'history':self.service.state.list('git_history',limit=30),'lastSuccess':self.service.state.get('git_success','last'),
                'nextRun':tomorrow.isoformat() if policy['enabled'] else None,'error':self.last_error,
                'preview':self.preview,'previewing':self.previewing,
                'coverageNotice':'Full Git branches/tags plus selected source view. Issues, releases, Actions and account settings are not included. Review per-repository coverage exceptions.'}
    def call(self,principal,capability,args):
        self.service.auth.owner(principal)
        if capability=='gitBackup.status':return self.status()
        if capability=='gitBackup.configure':
            policy=settings(args['settings'])
            with self.lock:
                self.service.state.put('git_settings','current',policy)
                self.service.state.audit(principal.id,'gitBackup.configure','saved')
            return self.status()
        if capability=='gitBackup.run':return self.trigger(args['requestId'])
        if capability=='gitBackup.retry':return self.trigger(args['requestId'],retry_failed=True)
        if capability=='gitBackup.reconcile':return self.service.state.get('git_requests',digest(args['requestId']))
        if capability=='gitBackup.cancel':
            with self.lock:
                row=self.service.state.get('git_run','active')
                if row and row['state']=='queued':row['state']='canceled';self.service.state.put('git_run','active',row)
                else:self.worker(action='cancel')
            return self.status()
        if capability=='gitBackup.preview':
            with self.lock:
                if not self.previewing:
                    self.previewing=True
                    def preview():
                        try:self.preview=self.worker(action='preview',scope=self.policy()['scope']);self.last_error=None
                        except BackendError as e:self.last_error=e.message
                        finally:self.previewing=False
                    threading.Thread(target=preview,daemon=True).start()
            return self.status()
        raise BackendError('INVALID_ARGUMENT','Unknown Git backup capability')
    def tick(self):
        run=self.service.state.get('git_run','active')
        if run and run['state']=='queued':
            remote=self.worker(action='run',id=run['id'],settings=run['settings'],onlyIds=run.get('onlyIds'))
            if remote['id']!=run['id']:raise BackendError('BUSY','A different Git worker run is still active')
            run['state']='running';self.service.state.put('git_run','active',run)
        inventory=self.worker(action='status');self.service.state.put('git_inventory','current',inventory)
        remote=inventory.get('run')
        if not run or not remote or run['id']!=remote['id']:return
        run.update({k:remote[k] for k in ('completed','failed','total','current','error') if k in remote})
        self.service.state.put('git_run','active',run)
        failed=0
        for key,repo in inventory.get('repositories',{}).items():
            if self.stop.is_set():return
            if repo.get('runId')!=run['id'] or repo.get('availability')!='available':continue
            previous=self.service.state.get('git_repo_status',key,{})
            if previous.get('artifact')==repo.get('artifact') and previous.get('published'):
                if remote['state']!='running':self.worker(action='ack',id=key,artifact=repo['artifact'])
                continue
            if repo.get('status')=='empty':
                self.service.state.put('git_repo_status',key,{'id':key,'status':'empty','published':False});continue
            if not repo.get('artifact'):continue
            try:
                published=self.publisher.publish(repo)
                self.service.state.put('git_repo_status',key,dict(published,id=key,published=True,status='partial' if repo['coverage'] else 'complete',lastError=None))
                if remote['state']!='running':self.worker(action='ack',id=key,artifact=repo['artifact'])
            except BackendError as error:
                failed+=1;self.service.state.put('git_repo_status',key,dict(previous,id=key,status='publication-failed',lastError=error.message))
            except OSError:
                failed+=1;self.service.state.put('git_repo_status',key,dict(previous,id=key,status='publication-failed',lastError='Publication failed; previous files retained'))
        run.update({k:remote[k] for k in ('completed','failed','total','current','error') if k in remote})
        if remote['state']!='running':
            partial=any(r.get('coverage') for r in inventory['repositories'].values() if r.get('runId')==run['id'])
            run['state']='partial' if failed or partial or remote['state']=='partial' else remote['state']
            run['finishedAt']=time.time();run['publicationFailures']=failed
            self.service.state.put('git_history',run['id'],run)
            history=sorted(self.service.state.list('git_history',limit=10000),key=lambda r:r['startedAt'])
            for old in history[:-30]:self.service.state.delete('git_history',old['id'])
            if run['state']=='completed' and not run.get('onlyIds'):self.service.state.put('git_success','last',{'id':run['id'],'at':run['finishedAt'],'repositories':run.get('total',0)})
        self.service.state.put('git_run','active',run)
        self.service.state.put('git_requests',digest(run['requestId']),run)
    def start(self):
        if not self.config:return
        self.service.state.put('git_protection','active',{'enabled':True})
        def loop():
            while not self.stop.is_set():
                try:
                    run=self.service.state.get('git_run','active')
                    if run and run['state'] in ('queued','running','publishing'):self.tick()
                    self.last_error=None
                except Exception as error:self.last_error=error.message if isinstance(error,BackendError) else 'Git coordinator failed; inspect service status'
                self.stop.wait(2)
        self.thread=threading.Thread(target=loop,daemon=True,name='git-coordinator');self.thread.start()
        parent=self
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                _,uid,_=struct.unpack('3i',self.request.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
                if uid not in (0,os.geteuid()):return
                self.request.settimeout(5)
                try:
                    raw=self.rfile.readline(1025)
                    if len(raw)>1024:raise ValueError()
                    request=json.loads(raw)
                    if request.get('action')!='trigger':raise ValueError()
                    result=parent.trigger(request['requestId'],scheduled=True)
                    out={'ok':True,'result':result}
                except Exception:out={'ok':False,'message':'Scheduled Git backup could not start'}
                self.wfile.write(json.dumps(out).encode()+b'\n')
        path=Path(self.config['controlSocket']);path.unlink(missing_ok=True)
        class Server(socketserver.ThreadingUnixStreamServer):daemon_threads=True
        self.control=Server(str(path),Handler);os.chmod(path,0o600)
        threading.Thread(target=self.control.serve_forever,daemon=True).start()
    def close(self):
        self.stop.set()
        if self.control:self.control.shutdown();self.control.server_close()
        if self.thread:self.thread.join(timeout=60)
        if self.thread and self.thread.is_alive():raise BackendError('BUSY','Git publisher is still stopping')
