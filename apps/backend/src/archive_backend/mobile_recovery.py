"""Bounded maintenance of generated mobile bytes; durable receipts never expire.

No original file or Trash entry is removed. Ambiguous publication intents, live
processes and changed volumes require reconciliation instead of automatic cleanup.
"""
import json
import math
import os
import re
import threading
import time
from .contracts import BackendError,translate_error,bytes_value,path
from .mobile_metadata import legacy_ref
from .jobs import _owner_stopped

IDLE_SECONDS=86400
BATCH=64


class MobileMaintenance:
    def __init__(self,transfers):
        self.transfers=transfers; self.downloads=transfers.downloads; self.state=transfers.state; self.service=transfers.service
        self._stop=threading.Event(); self._cursors={}; self._scan=None
        self.tick()
        self.thread=threading.Thread(target=self._run,name='archive-mobile-maintenance',daemon=True); self.thread.start()
    def close(self):
        self._stop.set(); self.thread.join()
        if self._scan is not None: self._scan.close(); self._scan=None
    def _run(self):
        while not self._stop.wait(60):
            try: self.tick()
            except Exception as exc:
                error=translate_error(exc); self.service.observability.record(error.code,level='error',correlation_id=error.correlation_id)
    def _rows(self,namespace):
        after=self._cursors.get(namespace,'')
        with self.state.transaction() as c:
            rows=c.execute("SELECT key,body FROM records WHERE namespace=? AND key>? AND CASE WHEN json_valid(body) THEN COALESCE(json_extract(body,'$.released'),0)=0 ELSE 1 END ORDER BY key LIMIT ?",(namespace,after,BATCH)).fetchall()
        self._cursors[namespace]=rows[-1]['key'] if len(rows)==BATCH else ''
        return rows
    def _invalid(self): raise BackendError('INTEGRITY_FAILED','Stored mobile lifecycle metadata requires inspection')
    def _validate(self,namespace,key,record):
        if not isinstance(record,dict) or record.get('id')!=key or not re.fullmatch(r'[a-f0-9]{64}',key): self._invalid()
        for field in ('actor','jobId','sessionId'):
            if not isinstance(record.get(field),str) or not record[field]: self._invalid()
        if type(record.get('released')) is not bool: self._invalid()
        bytes_value(record['size'])
        for field in ('created','expires') if namespace=='mobile_downloads' else ('created','updated'):
            value=record.get(field)
            if type(value) not in (int,float) or not math.isfinite(value) or value<0: self._invalid()
        legacy_ref(record['source' if namespace=='mobile_downloads' else 'destination'])
        if namespace=='mobile_downloads' and record.get('state') not in ('preparing','ready','failed','canceled'): self._invalid()
        job=self.state.get('jobs',record['jobId'])
        if not isinstance(job,dict) or job.get('id')!=record['jobId'] or job.get('actor',{}).get('id')!=record['actor'] or job.get('operation')!=('download' if namespace=='mobile_downloads' else 'upload'):
            self._invalid()
        if job.get('_lease') is not None and not isinstance(job['_lease'],dict): self._invalid()
        return job
    def _report(self,error):
        self.service.observability.record(error.code,level='warning',correlation_id=error.correlation_id)
    def _isolate(self,callback):
        try: callback()
        except BackendError as error:
            if error.code=='STATE_UNAVAILABLE': raise
            self._report(error)
        except (ValueError,TypeError,KeyError,AttributeError,IndexError):
            self._report(BackendError('INTEGRITY_FAILED','Stored mobile lifecycle metadata requires inspection'))
    def _stopped(self,rid,job):
        if rid in self.transfers._active or rid in self.downloads._futures: return False
        owner=self.state.get('mobile_writers',rid)
        if owner:
            if not _owner_stopped(owner): return False
            self.state.delete('mobile_writers',rid)
        lease=job.get('_lease') if job else None
        if lease:
            if not _owner_stopped(lease): return False
            job.update(_lease=None,_fence=job.get('_fence',0)+1,_workerStopped=True)
            self.service.jobs._save(job,'mobile-recovered-stopped-writer')
        return True
    def _session_valid(self,record,scope,drive,path):
        try:
            context=self.transfers.boundary.auth._context(self.state.get('mobile_sessions',record['sessionId']))
            self.service.auth.authorize(context.principal,scope,drive,path); return True
        except BackendError as error:
            if error.code in ('AUTH_EXPIRED','UNAUTHENTICATED','PERMISSION_REVOKED','REVOKED','PERMISSION_DENIED'): return False
            raise
    def _stop_job(self,job,error):
        if job and job['state'] not in ('completed','failed','canceled'):
            job.update(state='failed',error=error.envelope(),_lease=None,_workerStopped=True,_fence=job.get('_fence',0)+1)
            self.service.jobs._save(job,'mobile-retention-stopped')
    def _download(self,saved,now):
        with self.transfers._condition:
            with self.state.transaction():
                record=self.state.get('mobile_downloads',saved['id']); job=self._validate('mobile_downloads',saved['id'],record); rid=record['id']
                if record['released'] or not self._stopped(rid,job): return
                error=None
                if record['expires']<=now: error=BackendError('REPRESENTATION_EXPIRED','Generated snapshot expired; request a new captured version')
                elif not self._session_valid(record,'read',record['source']['driveId'],record['source']['path']):
                    error=BackendError('PERMISSION_REVOKED','Snapshot authority is no longer available')
                elif record['state']=='preparing':
                    error=BackendError('SNAPSHOT_INTERRUPTED','Snapshot preparation was interrupted; request a new captured version')
                elif record['state']=='ready':
                    try:
                        with self.downloads.spool.open(record): pass
                    except Exception as exc: error=translate_error(exc)
                if error:
                    record.update(state='failed',error=error.envelope(),cleanupState='pending'); self.state.put('mobile_downloads',rid,record)
                    self._stop_job(job,error)
                if record['state'] in ('failed','canceled'): self.downloads._cleanup(record)
    def _upload(self,saved,now):
        with self.transfers._condition:
            with self.state.transaction():
                record=self.state.get('mobile_uploads',saved['id']); job=self._validate('mobile_uploads',saved['id'],record); rid=record['id']
                if record['released'] or not self._stopped(rid,job): return
                transfer=self.state.get('transfers',rid)
                if not isinstance(transfer,dict) or transfer.get('id')!=rid or transfer.get('jobId')!=record['jobId'] or transfer.get('actor')!=record['actor'] or transfer.get('direction','upload')!='upload' or transfer.get('size')!=record['size'] or str(transfer.get('generation'))!=record['destination']['generation'] or transfer.get('drive')!=record['destination']['driveId']:
                    raise BackendError('INTEGRITY_FAILED','Upload retention record is inconsistent')
                path(transfer['path']); offset=bytes_value(transfer['offset'])
                if offset>bytes_value(transfer['size']): self._invalid()
                completed=transfer['state']=='completed'
                published=completed and transfer.get('entry') is not None
                inactive=now-record.get('updated',record['created'])>=IDLE_SECONDS
                canceled=transfer['state'] in ('canceled','discarded') or record.get('desired')=='cancel' or bool(job and job['state'] in ('canceling','canceled'))
                invalid=not self._session_valid(record,'write',transfer['drive'],transfer['path'])
                if not (completed or inactive or canceled or invalid): return
                if self.state.get('intents','transfer:'+rid) and not completed:
                    # Publication may already have moved original bytes. Never
                    # discard or reset that authority based on elapsed time.
                    record.update(cleanupState='failed',lastError=BackendError('STATE_UNAVAILABLE','Publication intent requires reconciliation before staging cleanup').envelope())
                    self.state.put('mobile_uploads',rid,record); return
                try:
                    if not published:
                        fs=self.service.catalog.filesystem(transfer['drive']); fs.volume.check(transfer['generation'])
                        try:
                            held=fs.internal_stat('staging',rid)
                            if held.kind!='file': raise BackendError('INTEGRITY_FAILED','Unexpected staging entry type requires inspection')
                            fs.discard_staging(rid)
                        except BackendError as error:
                            if error.code!='NOT_FOUND': raise
                    record.update(released=True,cleanupState='confirmed',lastError=None,desired=None)
                    if not completed:
                        transfer['state']='discarded'; self.state.put('transfers',rid,transfer)
                        if canceled and job and job['state'] not in ('completed','failed','canceled'):
                            job.update(state='canceled',error=None,_lease=None,_fence=job.get('_fence',0)+1,_workerStopped=True)
                            self.service.jobs._save(job,'mobile-retention-canceled')
                        if not canceled:
                            error=BackendError('PERMISSION_REVOKED' if invalid else 'UPLOAD_EXPIRED','Upload authority ended' if invalid else 'Upload staging expired; select a source for a new request')
                            record.update(expired=True,lastError=error.envelope())
                            if job and job['state']=='failed':
                                job['error']=error.envelope(); self.service.jobs._save(job,'mobile-expired-staging')
                            else: self._stop_job(job,error)
                    self.state.put('mobile_uploads',rid,record)
                except Exception as exc:
                    record.update(cleanupState='failed',lastError=translate_error(exc).envelope()); self.state.put('mobile_uploads',rid,record)
    def _tokens(self,now):
        # Raw bounded rows are decoded separately. One malformed record cannot
        # hide other generated bytes, nor is malformed authority auto-deleted.
        after=self._cursors.get('sessions','')
        with self.state.transaction() as c:
            rows=c.execute("SELECT key,body FROM records WHERE namespace='mobile_sessions' AND key>? AND CASE WHEN json_valid(body) THEN COALESCE(json_extract(body,'$.revoked'),0)=0 ELSE 1 END ORDER BY key LIMIT ?",(after,BATCH)).fetchall()
        self._cursors['sessions']=rows[-1]['key'] if len(rows)==BATCH else ''
        def session_row(row):
            session=json.loads(row['body'])
            if not isinstance(session,dict) or session.get('id')!=row['key'] or type(session.get('revoked')) is not bool: self._invalid()
            for field in ('idleExpires','absoluteExpires'):
                if type(session.get(field)) not in (int,float) or not math.isfinite(session[field]): self._invalid()
            if min(session['idleExpires'],session['absoluteExpires'])<=now:
                with self.state.transaction(): self.transfers.boundary.auth._revoke(session,'expired-retention')
        for row in rows: self._isolate(lambda:session_row(row))
        ns,key=self._cursors.get('tokens',('',''))
        with self.state.transaction() as c:
            rows=c.execute("SELECT namespace,key,body FROM records WHERE namespace IN ('mobile_tokens','mobile_transfer_tokens','mobile_replays') AND (namespace>? OR (namespace=? AND key>?)) AND CASE WHEN json_valid(body) THEN COALESCE(json_extract(body,'$.expires'),0)<=? ELSE 1 END ORDER BY namespace,key LIMIT 256",(ns,ns,key,now)).fetchall()
        self._cursors['tokens']=(rows[-1]['namespace'],rows[-1]['key']) if len(rows)==256 else ('','')
        def token_row(row):
            record=json.loads(row['body'])
            if not isinstance(record,dict) or type(record.get('expires')) not in (int,float) or not math.isfinite(record['expires']): self._invalid()
            if not isinstance(record.get('session'),str) or not record['session']: self._invalid()
            if row['namespace']=='mobile_tokens' and record.get('kind') not in ('access','refresh'): self._invalid()
            # Used refresh verifiers survive through the live family's deadline,
            # including older individual token expiry, preserving reuse detection.
            if row['namespace']=='mobile_tokens' and record.get('kind')=='refresh' and record.get('used'):
                session=self.state.get('mobile_sessions',record['session'])
                if not isinstance(session,dict): self._invalid()
                if not session['revoked'] and min(session['idleExpires'],session['absoluteExpires'])>now: return
            with self.state.transaction(): self.state.delete(row['namespace'],row['key'])
        for row in rows: self._isolate(lambda:token_row(row))
    def _orphans(self,now):
        if self._scan is None: self._scan=os.scandir(self.downloads.spool.fd)
        for _ in range(256):
            try: entry=next(self._scan)
            except StopIteration:
                self._scan.close(); self._scan=None; break
            match=re.fullmatch(r'([a-f0-9]{64})\.(part|blob)',entry.name)
            if not match: continue
            rid=match[1]
            try: info=entry.stat(follow_symlinks=False)
            except FileNotFoundError: continue
            if now-info.st_mtime<3600: continue
            def inspect():
                with self.transfers._condition:
                    with self.state.transaction():
                        if self.state.get('mobile_downloads',rid) or not self._stopped(rid,None): return
                        # No inference from malformed/missing lifecycle authority.
                        # remove() rejects foreign owner, symlink and unsafe links.
                        self.downloads.spool.remove(rid)
            self._isolate(inspect)
    def tick(self):
        now=time.time(); self.state.verify_guard(); self._tokens(now)
        for namespace,handler in (('mobile_uploads',self._upload),('mobile_downloads',self._download)):
            for row in self._rows(namespace):
                def process():
                    record=json.loads(row['body']); self._validate(namespace,row['key'],record)
                    if not record['released']: handler(record,now)
                self._isolate(process)
        self._orphans(now)
