"""Optional lyrics queue, isolated lookups, and verified sidecar publication."""
import hashlib
import json
import time
import uuid
from .contracts import BackendError, digest, identifier, object_fields

FINAL = frozenset(('saved','missing','instrumental','failed'))


def validate_result(value):
    object_fields(value,('status','plain','synced','sourceId','embedded'))
    if value['status'] not in ('found','missing','instrumental','failed') or type(value['embedded']) is not bool:
        raise BackendError('INVALID_RESPONSE','Invalid lyrics result')
    if value['sourceId'] is not None and (type(value['sourceId']) is not int or value['sourceId']<0):
        raise BackendError('INVALID_RESPONSE','Invalid lyrics source')
    for key in ('plain','synced'):
        text=value[key]
        try:
            invalid=text is not None and (not isinstance(text,str) or not text.strip() or len(text.encode('utf-8'))>65536 or any(ord(c)<32 and c not in '\r\n\t' for c in text))
        except UnicodeError: invalid=True
        if invalid: raise BackendError('INVALID_RESPONSE','Invalid lyrics text')
    if value['status']=='found' and not (value['plain'] or value['synced']): raise BackendError('INVALID_RESPONSE','Empty lyrics result')
    if value['status']!='found' and (value['plain'] or value['synced']): raise BackendError('INVALID_RESPONSE','Unexpected lyrics text')
    return value


class MusicLyrics:
    def __init__(self,music): self.music=music;self.service=music.service;self.state=music.state;self.next_lookup=0

    def status(self):
        with self.state.transaction() as connection:
            counts={row[0]:row[1] for row in connection.execute("SELECT json_extract(body,'$.state'),COUNT(*) FROM records WHERE namespace='music_lyrics' GROUP BY 1")}
        run=self.state.get('music_lyrics_runs','backfill')
        return {'provider':'LRCLIB','saved':counts.get('saved',0),'missing':counts.get('missing',0),
                'instrumental':counts.get('instrumental',0),'failed':counts.get('failed',0),
                'pending':sum(count for state,count in counts.items() if state not in FINAL),'scanning':bool(run and run['state']=='scanning')}

    def backfill(self,principal,request_id):
        identifier(request_id);key=digest([principal.id,request_id])
        with self.state.transaction():
            if self.state.get('music_lyrics_requests',key): return self.status()
            run=self.state.get('music_lyrics_runs','backfill')
            if not run or run['state']!='scanning':
                self.state.put('music_lyrics_runs','backfill',{'id':uuid.uuid4().hex,'state':'scanning','cursor':'','_actor':principal.as_dict()})
            self.state.put('music_lyrics_requests',key,{'createdAt':time.time()})
            self.state.audit(principal.id,'music.lyrics.backfill','accepted','SpotifyDL library')
        return self.status()

    def schedule(self,principal,track_id,entry,result=None,force=False,duration_ms=0):
        identity=hashlib.sha256((entry['driveId']+':'+entry['path']).encode()).hexdigest()
        old=self.state.get('music_lyrics',identity)
        if old and old['entry']==entry:
            if old['state'] not in FINAL or old['state'] in ('saved','instrumental') or not force: return False
        row={'id':identity,'trackId':track_id,'entry':entry,'state':'queued','attempt':0,'error':None,'source':'LRCLIB',
             'sourceId':None,'embedded':False,'files':[], '_actor':principal.as_dict(),'_task':None,'_result':None,
             '_transfers':{},'_nextAt':0,'_durationMs':duration_ms,'_generation':uuid.uuid4().hex}
        if result:
            try:
                row['_result']=validate_result(result)
                row['embedded']=result['embedded'];row['sourceId']=result['sourceId']
                row['state']='publishing' if result['status']=='found' else result['status'] if result['status']!='failed' else 'queued'
            except BackendError: pass # Optional metadata cannot fail an audio import.
        self.state.put('music_lyrics',identity,row)
        return True

    def scan(self):
        run=self.state.get('music_lyrics_runs','backfill')
        if not run or run['state']!='scanning': return
        try:
            principal=self.music.principal(run['_actor'])
            rows=self.state.list('music_library',100,after=run['cursor'])
            for record in rows:
                entry=record['entry']
                self.schedule(principal,record['trackId'],entry,force=True,duration_ms=record.get('durationMs',0))
                run['cursor']=hashlib.sha256((entry['driveId']+':'+entry['path']).encode()).hexdigest()
            if len(rows)<100: run['state']='completed'
        except BackendError:
            run['state']='failed'
        self.state.put('music_lyrics_runs','backfill',run)

    def publish(self,principal,row):
        # The audio reference must still be the exact imported file. No rewriting
        # or following moved/replaced files during a lyrics-only backfill.
        self.service.catalog.stat(principal,row['entry'])
        data=row['_result']; extension='lrc' if data['synced'] else 'txt'
        content=(data['synced'] or data['plain']).encode('utf-8')
        directory,_,filename=row['entry']['path'].rpartition('/')
        if not filename.lower().endswith('.mp3'): raise BackendError('INVALID_PATH','Lyrics require a SpotifyDL MP3')
        name=filename[:-4]+'.'+extension
        checksum=hashlib.sha256(content).hexdigest()
        transfer=row['_transfers'].get(extension)
        if not transfer:
            receipt=self.service.transfers.initiate(principal,{'requestId':'lyrics-'+row['_generation']+'-'+extension,
                'destination':{'driveId':row['entry']['driveId'],'path':directory},'name':name,
                'sizeBytes':str(len(content)),'sha256':checksum,'conflictPolicy':'ask'})
            transfer=receipt['transferId'];row['_transfers'][extension]=transfer
            self.state.put('music_lyrics',row['id'],row)
        receipt=self.service.transfers.query(principal,transfer)
        if receipt['state']!='completed':
            offset=int(receipt['offset'])
            if offset<len(content): self.service.transfers.write(principal,transfer,offset,content[offset:])
            receipt=self.service.transfers.finalize(principal,transfer)
        ref=receipt['entry']
        row['files']=[{k:ref[k] for k in ('id','driveId','path','revision')}]
        row['state']='saved';row['error']=None;row['_result']=None

    def tick(self):
        self.scan()
        with self.state.transaction() as connection:
            value=connection.execute("SELECT body FROM records WHERE namespace='music_lyrics' AND (json_extract(body,'$.state') NOT IN ('saved','missing','instrumental','failed') OR json_extract(body,'$._task') IS NOT NULL) AND COALESCE(json_extract(body,'$._nextAt'),0)<=? ORDER BY CASE json_extract(body,'$.state') WHEN 'fetching' THEN 0 WHEN 'publishing' THEN 1 ELSE 2 END,updated LIMIT 1",(time.time(),)).fetchone()
        if not value: return
        row=json.loads(value[0])
        try:
            principal=self.music.principal(row['_actor'])
            self.service.catalog.stat(principal,row['entry'])
            if row['state']=='queued':
                if time.monotonic()<self.next_lookup: return
                self.next_lookup=time.monotonic()+2
                if not row['_task']:
                    row['_task']=uuid.uuid4().hex
                    self.state.put('music_lyrics',row['id'],row)
                result=self.music.worker.call(op='start',taskId=row['_task'],action='lyrics',payload={'trackId':row['trackId'],'durationMs':row['_durationMs']})
                row['state']='fetching';row['attempt']+=1
            else: result=None
            if row['state']=='fetching':
                result=result or self.music.worker.call(op='poll',taskId=row['_task'])
                if result['state']=='absent':
                    self.music.worker.call(op='cleanup',taskId=row['_task']);row.update(state='queued',_task=None)
                elif result['state'] in ('completed','failed','canceled'):
                    data=validate_result(result['result']) if result['state']=='completed' else {'status':'failed','plain':None,'synced':None,'sourceId':None,'embedded':False}
                    self.music.worker.call(op='cleanup',taskId=row['_task']);row['_task']=None
                    row.update(_result=data,sourceId=data['sourceId'])
                    if data['status']=='found': row['state']='publishing'
                    elif data['status']=='failed' and row['attempt']<3: row.update(state='queued',_nextAt=time.time()+(30 if row['attempt']==1 else 300))
                    else: row.update(state=data['status'],_result=None,error='Lyrics provider unavailable' if data['status']=='failed' else None)
            if row['state']=='publishing': self.publish(principal,row)
        except BackendError as error:
            if error.code in ('UNAVAILABLE','BUSY','INSUFFICIENT_SPACE','DRIVE_UNAVAILABLE','VOLUME_MISMATCH','READ_ONLY','QUOTA_EXCEEDED'):
                row['_nextAt']=time.time()+10;row['error']='Waiting for the lyrics service or destination drive'
            else: row.update(state='failed',error=error.message[:500],_result=None)
        except (KeyError,ValueError,TypeError): row.update(state='failed',error='Incompatible lyrics response',_result=None)
        if row['state']=='failed':
            for transfer in row['_transfers'].values():
                try: self.service.transfers.cancel(self.music.principal(row['_actor']),transfer,discard=True)
                except BackendError: pass
        if row['state'] in FINAL and row.get('_task'):
            try: self.music.worker.call(op='cleanup',taskId=row['_task']);row['_task']=None
            except BackendError: pass
        self.state.put('music_lyrics',row['id'],row)
