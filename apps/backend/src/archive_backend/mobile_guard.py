"""Independent critical-transaction watermark; ambiguous restore fails closed.

This detects database-only rollback/missing sidecars, not coordinated rollback of
both files. Every full-backup restore still requires explicit operator reconciliation
and identity reset. No production recovery is performed automatically.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import time
import uuid
from .contracts import BackendError

CRITICAL_NAMESPACES=frozenset({'principals','credentials','credential_ids','configuration','intents','trash',
    'mobile_accounts','mobile_sessions','mobile_tokens','mobile_transfer_tokens','mobile_replays',
    'mobile_operations','mobile_upload_requests','mobile_download_requests','mobile_manifests'})


class ReceiptGuard:
    def __init__(self,state):
        self.state=state; self.blocked=False
        self.identity=hashlib.sha256(Path(state.path).name.encode()).hexdigest()
        self.name='.archive-mobile-guard-'+self.identity[:24]+'.json'
        self.fd=os.open(str(Path(state.path).parent),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
        info=os.fstat(self.fd)
        if info.st_uid!=os.geteuid() or info.st_mode & 0o022:
            os.close(self.fd); raise BackendError('PERMISSION_DENIED','Unsafe watermark parent')
    def close(self): os.close(self.fd)
    def assert_available(self):
        if self.blocked: raise BackendError('STATE_UNAVAILABLE','Mobile state requires private restore reconciliation')
    def _record(self):
        row=self.state._connection.execute("SELECT body FROM records WHERE namespace='mobile_guard' AND key='current'").fetchone()
        try: return json.loads(row[0]) if row else None
        except (ValueError,TypeError): raise BackendError('INTEGRITY_FAILED','Malformed database receipt watermark') from None
    def _read(self):
        try: fd=os.open(self.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=self.fd)
        except FileNotFoundError: return None
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid() or info.st_nlink!=1 or info.st_mode & 0o077 or info.st_size>4096:
                raise BackendError('INTEGRITY_FAILED','Unsafe independent receipt watermark')
            try:
                value=json.loads(os.read(fd,4096))
                if not isinstance(value,dict): raise ValueError()
                return value
            except (ValueError,UnicodeError): raise BackendError('INTEGRITY_FAILED','Malformed independent receipt watermark') from None
        finally: os.close(fd)
    def _write(self,value):
        # The marker is written before the SQL commit. A crash in between makes
        # the service unavailable rather than falsely claiming a request is new.
        old=self._read()
        if old is not None and old.get('stateNameSha256')!=self.identity:
            raise BackendError('INTEGRITY_FAILED','Independent watermark belongs to another state file')
        name=self.name+'.tmp-'+uuid.uuid4().hex
        try:
            fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=self.fd)
            try:
                data=json.dumps(value,sort_keys=True,separators=(',',':')).encode()
                if os.write(fd,data)!=len(data): raise BackendError('STATE_UNAVAILABLE','Independent watermark write failed')
                os.fsync(fd)
            finally: os.close(fd)
            os.replace(name,self.name,src_dir_fd=self.fd,dst_dir_fd=self.fd); os.fsync(self.fd)
        finally:
            try: os.unlink(name,dir_fd=self.fd)
            except FileNotFoundError: pass
    def verify(self):
        self.assert_available()
        database=self._record(); marker=self._read()
        valid=bool(isinstance(database,dict) and set(database)=={'schemaVersion','serverId','sequence','stateNameSha256'}
                   and database.get('schemaVersion')==1 and type(database.get('sequence')) is int and database['sequence']>=0
                   and database.get('stateNameSha256')==self.identity and database==marker)
        server=self.state._connection.execute("SELECT body FROM records WHERE namespace='mobile_meta' AND key='server'").fetchone()
        try: server_id=json.loads(server[0]).get('id') if server else None
        except (ValueError,TypeError,AttributeError): server_id=None
        if not valid or server_id!=database['serverId']:
            self.blocked=True
            raise BackendError('STATE_UNAVAILABLE','Independent receipt watermark mismatch; private restore reconciliation required')
    def advance(self):
        self.verify(); value=self._record(); value['sequence']+=1
        self.blocked=True
        self.state._connection.execute("UPDATE records SET body=?,revision=revision+1,updated=? WHERE namespace='mobile_guard' AND key='current'",(json.dumps(value,sort_keys=True,separators=(',',':')),time.time()))
        self._write(value)
    @classmethod
    def attach_existing(cls,state):
        if state.path==':memory:' or state.path.startswith('file:') or state._mobile_recovery: return None
        guard=cls(state)
        try:
            with state.transaction():
                record=guard._record(); marker=guard._read()
                if record is None and marker is None: guard.close(); return None
                guard.verify()
            return guard
        except BaseException:
            guard.close(); raise
    @classmethod
    def enable(cls,state,server_id):
        if state._mobile_recovery:
            raise BackendError('STATE_UNAVAILABLE','Complete explicit restore recovery before enabling mobile authentication')
        if state._receipt_guard:
            state.verify_guard(); return
        if state.path==':memory:' or state.path.startswith('file:'):
            raise BackendError('INVALID_ARGUMENT','Mobile requests require a durable independent watermark')
        guard=cls(state)
        try:
            with state.transaction() as c:
                if guard._read() is not None or guard._record() is not None:
                    raise BackendError('STATE_UNAVAILABLE','Existing watermark requires private reconciliation')
                count=c.execute("SELECT COUNT(*) FROM records WHERE namespace IN ('mobile_accounts','mobile_sessions','mobile_operations','mobile_upload_requests','mobile_download_requests')").fetchone()[0]
                if count: raise BackendError('STATE_UNAVAILABLE','Pre-watermark mobile state requires explicit private migration/reconciliation')
                value={'schemaVersion':1,'serverId':server_id,'sequence':0,'stateNameSha256':guard.identity}
                c.execute("INSERT INTO records(namespace,key,body,updated) VALUES('mobile_guard','current',?,?)",(json.dumps(value,sort_keys=True,separators=(',',':')),time.time()))
                guard._write(value)
            state._receipt_guard=guard
        except BaseException:
            guard.close(); raise


def recover_mobile_identity(state,auth,owner,new_password,scopes,roots,*,acknowledged=False):
    """Private stopped-service operator workflow; never mounted on HTTP/MCP."""
    from argon2 import PasswordHasher
    from .mobile_auth import MobileAuth
    if not state._mobile_recovery or state._service_lock_fd is None or not acknowledged:
        raise BackendError('PERMISSION_DENIED','Stopped-service owner recovery and reconciliation acknowledgement are required')
    auth.owner(owner); scopes,roots=MobileAuth._assignment(scopes,roots)
    encoded=PasswordHasher(time_cost=3,memory_cost=65536,parallelism=4).hash(MobileAuth._password(new_password))
    guard=ReceiptGuard(state); server_id=str(uuid.uuid4())
    try:
        with state.transaction() as c:
            from .jobs import _owner_stopped
            # A child can outlive the main service lock. Inspect every live lease
            # under the SQL writer lock; unknown owner identity is not termination.
            rows=c.execute("SELECT key,body FROM records WHERE namespace='jobs' AND json_type(body,'$._lease') NOT IN ('null') ORDER BY key")
            stopped=[]
            for index,row in enumerate(rows):
                if index>=10000: raise BackendError('STATE_UNAVAILABLE','Active lease inspection exceeds recovery bound')
                job=json.loads(row['body']); lease=job.get('_lease')
                if not isinstance(lease,dict) or not _owner_stopped(lease):
                    raise BackendError('BUSY','A surviving or unproven job worker prevents restore recovery')
                stopped.append(job)
            rows=c.execute("SELECT key,body FROM records WHERE namespace='mobile_writers' ORDER BY key")
            dead_writers=[]
            for index,row in enumerate(rows):
                if index>=10000: raise BackendError('STATE_UNAVAILABLE','Mobile writer inspection exceeds recovery bound')
                if not _owner_stopped(json.loads(row['body'])):
                    raise BackendError('BUSY','A surviving or unproven mobile writer prevents restore recovery')
                dead_writers.append(row['key'])
            # Fence a child that was spawned but had not registered its own PID:
            # it must validate the original lease before any filesystem effect.
            for job in stopped:
                job.update(_lease=None,_fence=job.get('_fence',0)+1,_workerStopped=True)
                if job['state'] not in ('completed','failed','canceled'):
                    job.update(state='failed',error=BackendError('STATE_UNAVAILABLE','Restore recovery fenced interrupted work; inspect before retry').envelope())
                state.put('jobs',job['id'],job)
            for key in dead_writers: state.delete('mobile_writers',key)
            account=state.get('mobile_accounts','current')
            if not account: raise BackendError('NOT_FOUND','Restore an enrolled account before private recovery')
            rows=c.execute("SELECT key,body FROM records WHERE namespace='mobile_sessions'").fetchall()
            for row in rows:
                session=json.loads(row['body']); session['revoked']=True; state.put('mobile_sessions',row['key'],session)
                principal=state.get('principals',session['principal']['id'])
                if principal:
                    principal['revoked']=True; principal['scopeVersion']+=1; state.put('principals',principal['id'],principal)
            c.execute("DELETE FROM records WHERE namespace IN ('mobile_tokens','mobile_transfer_tokens','mobile_replays')")
            account.update(passwordHash=encoded,revision=account['revision']+1,scopes=scopes,roots=roots); state.put('mobile_accounts','current',account)
            state.put('mobile_meta','server',{'id':server_id,'schemaVersion':1})
            state.audit(owner.id,'mobile.restore_identity','reset',server_id)
            value={'schemaVersion':1,'serverId':server_id,'sequence':0,'stateNameSha256':guard.identity}
            c.execute("INSERT INTO records(namespace,key,body,updated) VALUES('mobile_guard','current',?,?) ON CONFLICT(namespace,key) DO UPDATE SET body=excluded.body,updated=excluded.updated",(json.dumps(value,sort_keys=True,separators=(',',':')),time.time()))
            guard._write(value)
        state._mobile_recovery=False; state._receipt_guard=guard
        return {'mobileIdentityReset':True,'serverId':server_id,'oldDevicesRevoked':True,'operatorReconciliationAcknowledged':True}
    except BaseException:
        guard.close(); raise
