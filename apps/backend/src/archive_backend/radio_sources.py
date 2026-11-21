"""Authorized MP3 discovery and immutable, quota-bounded private preparation."""
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import stat
import subprocess
import time
import uuid
from .contracts import BackendError, digest, location
from .radio_contracts import LIMITS


def list_tracks(radio,p,args):
    loc=location(args['location']);radio.auth.authorize(p,'read',loc['driveId'],loc['path'])
    if not any(loc['driveId']==r['driveId'] and (not r['path'] or loc['path']==r['path'] or loc['path'].startswith(r['path']+'/')) for r in (radio.config or {}).get('roots',[])):
        raise BackendError('PERMISSION_DENIED','Discovery must begin within an authorized Music root')
    binding=digest({'location':loc,'query':args.get('query',''),'scope':p.scope_version,'roots':radio.config['roots']})
    cursor=args.get('cursor');now=time.time()
    if cursor:
        scan=radio.state.get('radio_scans',cursor)
        if not scan or scan['actor']!=p.id or scan['binding']!=binding or scan['expires']<now:
            raise BackendError('STALE_REVISION','Radio discovery cursor expired or authorization changed')
    else:
        cursor=uuid.uuid4().hex
        scan={'actor':p.id,'binding':binding,'expires':now+300,'pending':[loc['path']],'current':None,'cursor':None,'buffer':[],'scanned':0}
        with radio.state.transaction() as c:
            c.execute("DELETE FROM records WHERE namespace='radio_scans' AND (json_extract(body,'$.expires')<? OR json_extract(body,'$.actor')=?)",(now,p.id))
            if c.execute("SELECT COUNT(*) FROM records WHERE namespace='radio_scans'").fetchone()[0]>=32:
                raise BackendError('QUOTA_EXCEEDED','Discovery cursor limit reached')
    result=[];limit=args.get('limit',100)
    for _ in range(4):
        while scan['buffer'] and len(result)<limit:result.append(scan['buffer'].pop(0))
        if len(result)>=limit:break
        if scan['current'] is None:
            if not scan['pending']:break
            scan['current']=scan['pending'].pop();scan['cursor']=None
        query={'location':{'driveId':loc['driveId'],'path':scan['current']},'requestId':'radio-discovery','limit':100}
        if scan['cursor']:query['cursor']=scan['cursor']
        page=radio.service.catalog.list(p,query)
        for e in page['entries']:
            scan['scanned']+=1
            if scan['scanned']>200000 or len(scan['pending'])>=4096:raise BackendError('QUOTA_EXCEEDED','Search a smaller Music subtree')
            if e['kind']=='directory':scan['pending'].append(e['path'])
            elif e['kind']=='file' and e['name'].lower().endswith('.mp3') and args.get('query','').casefold() in e['name'].casefold():
                scan['buffer'].append(e)
        scan['cursor']=page['nextCursor']
        if not scan['cursor']:scan['current']=None
    while scan['buffer'] and len(result)<limit:result.append(scan['buffer'].pop(0))
    more=bool(scan['buffer'] or scan['pending'] or scan['current'] is not None)
    if more:radio.state.put('radio_scans',cursor,scan)
    else:radio.state.delete('radio_scans',cursor)
    return {'tracks':result,'nextCursor':cursor if more else None,'scannedEntries':scan['scanned'],'snapshot':False}


def spool_root(config):
    root=Path(config['spool'])
    if str(root.resolve())!=str(root):raise BackendError('PERMISSION_DENIED','Radio spool must not contain symlinks')
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    st=root.stat()
    if st.st_uid!=os.geteuid() or stat.S_IMODE(st.st_mode)&0o077:raise BackendError('PERMISSION_DENIED','Radio spool must be private and service-owned')
    return root


def spool_path(config,delivery_id,spool_name=None):
    try:
        if str(uuid.UUID(delivery_id))!=delivery_id:raise ValueError()
    except (ValueError,AttributeError):raise BackendError('INVALID_ARGUMENT','Invalid delivery ID') from None
    if spool_name is None:spool_name=delivery_id+'.'+uuid.uuid4().hex+'.mp3'
    if not re.fullmatch(re.escape(delivery_id)+r'\.[0-9a-f]{32}\.mp3',spool_name):raise BackendError('INTEGRITY_FAILED','Invalid prepared spool identity')
    return spool_root(config)/spool_name


def flush_dir(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(fd)
    finally:os.close(fd)


def probe_mp3(path):
    try:
        result=subprocess.run(['/usr/bin/ffprobe','-v','error','-probesize','1048576','-analyzeduration','5000000','-show_entries','stream=codec_name,codec_type','-of','json',str(path)],capture_output=True,timeout=20,check=True)
        if len(result.stdout)>65536:raise ValueError()
        streams=json.loads(result.stdout)['streams'];audio=[s for s in streams if s.get('codec_type')=='audio']
        if not audio or any(s.get('codec_name')!='mp3' for s in audio):raise ValueError()
    except (OSError,ValueError,KeyError,subprocess.SubprocessError):
        raise BackendError('UNSUPPORTED_ENTRY','File does not contain supported MP3 audio') from None


def prepare(radio,p,item,heartbeat=lambda:None):
    radio.source(p,item['source'])
    target=spool_path(radio.config,item['deliveryId']);temp=target.with_suffix('.part')
    root=target.parent
    e=radio.service.catalog.stat(p,item['source']);size=int(e['sizeBytes'])
    used=sum(x.stat(follow_symlinks=False).st_size for x in root.iterdir())
    if used+size>int(radio.config['spoolBudgetBytes']) or shutil.disk_usage(root).free<size+64*1024**2:
        raise BackendError('INSUFFICIENT_SPACE','Radio spool reservation cannot be satisfied')
    fs=radio.service.catalog.filesystem(item['source']['driveId']);generation=fs.volume.generation
    if '_sourceGeneration' in item:fs.volume.check(item['_sourceGeneration'])
    hashed=hashlib.sha256();count=0
    try:
        with fs.open_read(item['source']['path'],expected_revision=item['source']['revision']) as source, temp.open('xb') as output:
            os.chmod(temp,0o600)
            while data:=source.read(1024*1024):
                heartbeat();count+=len(data)
                if count>int(LIMITS['maxFileBytes']):raise BackendError('QUOTA_EXCEEDED','MP3 exceeds size limit')
                output.write(data);hashed.update(data)
            source.check();output.flush();os.fsync(output.fileno())
        radio.source(p,item['source']);fs.volume.check(generation)
        if count!=size:raise BackendError('SOURCE_CHANGED','MP3 changed during preparation')
        heartbeat();probe_mp3(temp);heartbeat()
        os.chmod(temp,0o400);os.replace(temp,target);flush_dir(root)
        return {'spoolName':target.name,'sha256':hashed.hexdigest(),'sizeBytes':str(size),'filename':e['name'],'volumeGeneration':generation}
    except BaseException:
        if temp.exists():temp.unlink()
        raise


def open_payload(config,item,heartbeat=lambda:None):
    path=spool_path(config,item['deliveryId'],item['_manifest']['spoolName'])
    try:f=os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW),'rb')
    except OSError:raise BackendError('INTEGRITY_FAILED','Prepared MP3 is unavailable; receipt reconciliation required') from None
    try:
        s=os.fstat(f.fileno());manifest=item['_manifest']
        if not stat.S_ISREG(s.st_mode) or str(s.st_size)!=manifest['sizeBytes']:raise BackendError('INTEGRITY_FAILED','Prepared MP3 size changed')
        checksum=hashlib.sha256()
        while data:=f.read(1024*1024):heartbeat();checksum.update(data)
        if checksum.hexdigest()!=manifest['sha256']:raise BackendError('INTEGRITY_FAILED','Prepared MP3 checksum changed')
        f.seek(0);return f
    except BaseException:f.close();raise


def cleanup(config,item):
    path=spool_path(config,item['deliveryId'],item['_manifest']['spoolName'])
    for p in (path,path.with_suffix('.part')):
        if p.exists():p.unlink()
    flush_dir(path.parent)


def cleanup_orphans(radio):
    """Called at service startup under the singleton service lock, before workers."""
    root=spool_root(radio.config)
    for path in root.iterdir():
        if not re.fullmatch(r'[0-9a-f-]{36}\.[0-9a-f]{32}\.(mp3|part)',path.name):continue
        name=path.with_suffix('.mp3').name
        with radio.state._lock:
            row=radio.state.connection.execute("SELECT 1 FROM radio_items WHERE json_extract(body,'$._manifest.spoolName')=? LIMIT 1",(name,)).fetchone()
        if not row:path.unlink()
    flush_dir(root)
