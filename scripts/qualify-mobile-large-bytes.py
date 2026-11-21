#!/usr/bin/env python3
"""Disposable real loopback byte qualification, not NAS/TLS/native certification.

Keeps only one materialized large file at a time. --work-dir must be an existing
scratch directory with at least payload-size + reserve free. --bytes may be reduced
for harness checks; only a measured size greater than 4 GiB qualifies the large gate.
No original user data, live service configuration, or credentials are used.
"""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import resource
import runpy
import secrets
import socket
import sys
import tempfile
import threading
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'apps/backend/src'))
from cryptography.fernet import Fernet
from archive_backend.application import Service
from archive_backend.catalog import present_entry
from archive_backend.config import parse_config
from archive_backend.mobile_boundary import MobileBoundary
from archive_backend.mobile_http import create_server
from archive_backend.persistence import State
from archive_backend.volumes import VolumeRegistry

BLOCK=1024*1024


def require(value,message):
    if not value: raise RuntimeError(message)


def free(path):
    value=os.statvfs(path); return value.f_bavail*value.f_frsize


def sha_file(path):
    digest=hashlib.sha256(); count=0
    with open(path,'rb') as stream:
        for block in iter(lambda:stream.read(BLOCK),b''): digest.update(block); count+=len(block)
    return count,digest.hexdigest()


class Client:
    def __init__(self,port): self.port=port; self.access=None
    def connect(self): return http.client.HTTPConnection('127.0.0.1',self.port,timeout=300)
    def headers(self,token=None):
        values={'Host':'qualification.invalid','X-Forwarded-Proto':'https','X-Forwarded-For':'192.0.2.10'}
        if token: values['Authorization']='Bearer '+token
        return values
    def control(self,method,path,body=None):
        connection=self.connect()
        try:
            headers=self.headers(self.access)
            if body is not None: body=json.dumps(body).encode(); headers['Content-Type']='application/json'
            connection.request(method,path,body,headers); response=connection.getresponse()
            require('Location' not in response.headers,'Unexpected redirect')
            length=int(response.getheader('Content-Length','0')); require(0<length<=2*1024*1024,'Invalid control response length')
            value=json.loads(response.read(length))
            if response.status not in (200,202): raise RuntimeError('Control HTTP '+str(response.status)+' '+str(value.get('error',{}).get('safeServerCode','rejected')))
            return response.status,value
        finally: connection.close()
    def upload(self,admission,source,offset,total,stop_at=None):
        connection=self.connect(); path=admission['contentPath']+'?offset='+str(offset)
        try:
            connection.putrequest('PUT',path,skip_host=True,skip_accept_encoding=True)
            headers=self.headers(admission['transferToken']); headers.update({'Content-Type':'application/octet-stream','Content-Length':str(total-offset)})
            for name,value in headers.items(): connection.putheader(name,value)
            connection.endheaders(); sent=offset
            with open(source,'rb') as stream:
                stream.seek(offset)
                while sent<(stop_at if stop_at is not None else total):
                    size=min(BLOCK,(stop_at if stop_at is not None else total)-sent)
                    data=stream.read(size); require(len(data)==size,'Fixture source changed while uploading')
                    connection.send(data); sent+=len(data)
            if stop_at is not None:
                connection.sock.shutdown(socket.SHUT_WR)
                return sent
            response=connection.getresponse(); require(response.status==200,'Upload did not acknowledge the complete request')
            body=response.read(4096); value=json.loads(body)
            require(value['durableOffset']==str(total),'Upload acknowledgment was not the full durable size')
            return value
        finally: connection.close()
    def binary(self,path,token,*,start=None,etag=None,method='GET'):
        deadline=time.monotonic()+60
        while True:
            connection=self.connect(); headers=self.headers(token)
            if start is not None: headers.update({'Range':'bytes='+str(start)+'-','If-Range':etag})
            connection.request(method,path,headers=headers); response=connection.getresponse()
            if response.status==503:
                value=json.loads(response.read(4096)); connection.close()
                if value.get('error',{}).get('safeServerCode')=='BUSY' and time.monotonic()<deadline:
                    time.sleep(.1); continue
                raise RuntimeError('Content authority unavailable')
            require('Location' not in response.headers,'Unexpected content redirect')
            require(response.getheader('Content-Encoding') in (None,'identity'),'Representation was transformed')
            require(response.getheader('Content-Type')=='application/octet-stream','Expected byte representation')
            return connection,response


def qualify(args,evidence):
    require(args.bytes>=4096,'Qualification input must contain at least 4096 bytes')
    require(args.work_dir.is_dir(),'Scratch work directory does not exist')
    require(Path(tempfile.gettempdir()).resolve()==args.work_dir.resolve(),'Set process TMPDIR to the same explicit scratch work directory')
    require(args.reserve_bytes>=64*1024**2,'At least 64 MiB scratch reserve is required')
    require(free(args.work_dir)>=args.bytes+args.reserve_bytes,'Insufficient space for one materialized payload plus reserve')
    evidence.update(schemaVersion=1,synthetic=True,transport='loopback_http_trusted_proxy_headers',tlsQualified=False,
                    nativeBackgroundQualified=False,bytes=str(args.bytes),greaterThan4GiB=args.bytes>2**32,bufferBytes=BLOCK,
                    reserveBytes=str(args.reserve_bytes),freeBeforeBytes=str(free(args.work_dir)),steps=[])
    with tempfile.TemporaryDirectory(prefix='archive-mobile-large-',dir=args.work_dir) as directory:
        root=Path(directory); files=root/'files'; backups=root/'backups'; files.mkdir(); backups.mkdir()
        source=files/'boundary-source.bin'
        generator=runpy.run_path(str(Path(__file__).with_name('create-mobile-byte-fixture.py')))
        fixture=generator['create'](source,args.bytes); evidence['input']=fixture
        state=State(root/'state.sqlite'); service=Service(parse_config({'minFreeBytes':args.reserve_bytes}),state,VolumeRegistry.fixtures({'files':files,'backups':backups}))
        boundary=None; server=None; thread=None
        try:
            owner=service.auth.bootstrap_owner('synthetic-'+secrets.token_urlsafe(48)); service.ready=True
            boundary=MobileBoundary(service,{'publicOrigin':'https://qualification.invalid','replayKeyFile':'unused'},replay_key=Fernet.generate_key())
            password='synthetic-'+secrets.token_urlsafe(32)
            boundary.auth.enroll(owner,'qualification',password,['read','write','trash'],[{'driveId':'files','path':''}])
            server=create_server(boundary,'127.0.0.1',0); thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
            client=Client(server.server_address[1])
            _,session=client.control('POST','/mobile/v1/sessions',{'username':'qualification','password':password,'deviceLabel':'qualification','requestId':str(uuid.uuid4())})
            client.access=session['accessToken']; _,profile=client.control('GET','/mobile/v1/profile')
            args_upload={'requestId':str(uuid.uuid4()),'destination':profile['roots'][0]['ref'],'name':'published-fixture.bin','size':fixture['bytes'],'sha256':fixture['sha256'],'collisionPolicy':'ask'}
            _,admission=client.control('POST','/mobile/v1/uploads',args_upload); upload_path='/mobile/v1/uploads/'+admission['transferId']
            interrupted=min(args.bytes//2+17,2**31+111)
            sent=client.upload(admission,source,0,args.bytes,stop_at=interrupted); deadline=time.monotonic()+60
            while True:
                _,snapshot=client.control('GET',upload_path)
                if snapshot['state'] not in ('running','preparing','verifying','committing'): break
                require(time.monotonic()<deadline,'Interrupted writer did not terminate'); time.sleep(.1)
            offset=int(snapshot['durableOffset']); require(0<offset<=sent,'Invalid authoritative interrupted upload offset')
            require(free(root)>=args.reserve_bytes,'Scratch reserve reached during interrupted upload')
            _,authority=client.control('POST',upload_path+'/authorize',{})
            client.upload(authority,source,offset,args.bytes)
            _,complete=client.control('POST',upload_path+'/finalize',{})
            require(complete['outcome']=='published','Finalize did not confirm publication')
            require(complete['verification']['digest']==fixture['sha256'] and complete['verification']['bytes']==fixture['bytes'],'Server verification differs from frozen input')
            count,digest=sha_file(files/'published-fixture.bin'); require(count==args.bytes and digest==fixture['sha256'],'Published bytes differ from input')
            _,replay=client.control('POST','/mobile/v1/uploads',args_upload); require(replay['transferId']==admission['transferId'] and replay['outcome']=='published','Lost-response replay changed identity')
            evidence['steps'].append({'kind':'upload','interruptedSentBytes':str(sent),'durableResumeOffset':str(offset),'publishedBytes':str(count),'sha256':digest,'admissionReplaySameId':True,'cleanupState':complete['cleanupState']})
            print(json.dumps({'checkpoint':'large_upload_verified','bytes':str(count),'resumeOffset':str(offset)}),flush=True)
            # Only this generated fixture output is removed, after independent
            # verification, before allocating the download snapshot of the sparse input.
            (files/'published-fixture.bin').unlink()
            require(free(root)>=args.bytes+args.reserve_bytes,'Space not reclaimed for sequential download snapshot')
            raw=present_entry(service.filesystems['files'].stat('boundary-source.bin'),'files',service.auth,owner)
            source_ref=boundary.metadata.ref(raw)
            admission_status,preparing=client.control('POST','/mobile/v1/downloads',{'requestId':str(uuid.uuid4()),'source':source_ref})
            download_path='/mobile/v1/downloads/'+preparing['representationId']; deadline=time.monotonic()+900
            while True:
                _,representation=client.control('GET',download_path)
                if representation['state']=='ready': break
                require(time.monotonic()<deadline,'Snapshot preparation timed out'); require(free(root)>=args.reserve_bytes,'Scratch reserve reached during preparation'); time.sleep(.25)
            _,representation=client.control('POST',download_path+'/authorize',{})
            require(representation['bytes']==fixture['bytes'] and representation['sha256']==fixture['sha256'],'Snapshot differs from admitted source')
            connection,response=client.binary(representation['contentPath'],representation['transferToken'],method='HEAD')
            require(response.status==200 and int(response.getheader('Content-Length'))==args.bytes,'HEAD length mismatch'); response.read(); connection.close()
            digest=hashlib.sha256(); received=0; stop=min(args.bytes//2+31,2**31+333)
            connection,response=client.binary(representation['contentPath'],representation['transferToken'])
            require(response.status==200 and response.getheader('ETag')==representation['strongETag'],'Initial download validator mismatch')
            try:
                while received<stop:
                    data=response.read(min(BLOCK,stop-received)); require(bool(data),'Download ended before interruption point')
                    digest.update(data); received+=len(data)
            finally: response.close(); connection.close()
            connection,response=client.binary(representation['contentPath'],representation['transferToken'],start=received,etag=representation['strongETag'])
            expected_range=f'bytes {received}-{args.bytes-1}/{args.bytes}'
            require(response.status==206 and response.getheader('Content-Range')==expected_range and response.getheader('ETag')==representation['strongETag'],'Resume status/range/validator mismatch')
            require(int(response.getheader('Content-Length'))==args.bytes-received,'Resume length mismatch')
            resumed_at=received
            try:
                while True:
                    data=response.read(BLOCK)
                    if not data: break
                    digest.update(data); received+=len(data)
            finally: response.close(); connection.close()
            require(received==args.bytes and digest.hexdigest()==fixture['sha256'],'Resumed download checksum/length mismatch')
            probe=2**32+17 if args.bytes>2**32+17 else args.bytes-17
            connection,response=client.binary(representation['contentPath'],representation['transferToken'],start=probe,etag=representation['strongETag'])
            try: tail=response.read(4096)
            finally: response.close(); connection.close()
            with source.open('rb') as stream: stream.seek(probe); expected=stream.read(4096)
            require(response.status==206 and tail==expected,'Large decimal offset marker mismatch')
            acknowledgment={'requestId':str(uuid.uuid4()),'bytes':fixture['bytes'],'sha256':fixture['sha256']}
            _,receipt=client.control('POST',download_path+'/complete',acknowledgment)
            _,replayed=client.control('POST',download_path+'/complete',acknowledgment); require(receipt==replayed,'Completion replay differs')
            _,canceled=client.control('POST',download_path+'/cancel',{}); require(canceled['cleanupState']=='confirmed','Snapshot cleanup was not confirmed')
            evidence['steps'].append({'kind':'download','admissionHttpStatus':admission_status,'initialStatus':200,'resumeStatus':206,'resumeOffset':str(resumed_at),'receivedBytes':str(received),'sha256':digest.hexdigest(),'probeOffset':str(probe),'probeBytes':len(tail),'completionReplaySame':True,'cleanupState':canceled['cleanupState']})
            evidence['capabilitiesVersion']=profile['capabilitiesVersion']; evidence['freeAfterCleanupBytes']=str(free(root))
            print(json.dumps({'checkpoint':'large_download_verified','bytes':str(received),'resumeOffset':str(resumed_at),'probeOffset':str(probe)}),flush=True)
        finally:
            if server: server.shutdown(); server.server_close()
            if thread: thread.join()
            if boundary: boundary.close()
            service.close()
    evidence.update(success=True,peakRssKiB=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    require(evidence['peakRssKiB']<512*1024,'Peak process RSS exceeded 512 MiB qualification bound')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir',required=True,type=Path); parser.add_argument('--evidence',required=True,type=Path)
    parser.add_argument('--bytes',type=int,default=2**32+123); parser.add_argument('--reserve-bytes',type=int,default=512*1024**2)
    args=parser.parse_args(); evidence={}; started=time.monotonic(); failed=None
    fd=os.open(args.evidence,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        try: qualify(args,evidence)
        except Exception as error:
            evidence.update(success=False,errorType=type(error).__name__,error=str(error)[:500]); failed=error
        evidence['elapsedSeconds']=round(time.monotonic()-started,3)
        with os.fdopen(fd,'w') as target: json.dump(evidence,target,indent=2); target.write('\n')
        print(json.dumps({'success':evidence['success'],'evidence':str(args.evidence),'elapsedSeconds':evidence['elapsedSeconds']}),flush=True)
    except BaseException:
        try: os.close(fd)
        except OSError: pass
        raise
    if failed: raise SystemExit(1)


if __name__=='__main__': main()
