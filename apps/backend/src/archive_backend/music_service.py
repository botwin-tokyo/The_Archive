"""Durable music plans and jobs. Only the main service starts the coordinator."""
import copy
import hashlib
import json
import re
import threading
import time
import uuid
from .contracts import BackendError, digest, identifier, location, object_fields
from .music_contracts import item, selection, target, public, artist_plan, SUCCESS, TERMINAL
from . import music_discography
from .music_lyrics import MusicLyrics, validate_result
from .music_worker_client import MusicWorkerClient
from .music_imports import MusicImports
from . import acquisition_auth


HISTORY = 20000
# Keys are random, so a capped list() would silently drop jobs; readers use these indexed predicates instead.
UNFINISHED_ALBUMS = "namespace='music_jobs' AND json_extract(body,'$.kind') IS NOT 'artist' AND json_extract(body,'$.state') NOT IN ('completed','canceled','failed','partial')"
ARTIST_JOBS = "namespace='music_jobs' AND json_extract(body,'$.kind')='artist'"
TOP_LEVEL_JOBS = "namespace='music_jobs' AND json_extract(body,'$._parentId') IS NULL"


class MusicService:
    def __init__(self, service, config, worker=None):
        self.service,self.state,self.config=service,service.state,config
        self.worker=worker or (MusicWorkerClient(config) if config else None)
        self.imports=MusicImports(service,config) if config else None
        self.lock=threading.RLock(); self.stop=threading.Event(); self.thread=None
        self.health={'available':False,'at':0}; self.last_error=None
        self.lyrics=MusicLyrics(self)
        if self.state is not None:
            with self.state.transaction() as connection:
                connection.execute("CREATE INDEX IF NOT EXISTS music_unfinished_albums ON records(namespace,key) WHERE "+UNFINISHED_ALBUMS)
                connection.execute("CREATE INDEX IF NOT EXISTS music_artist_jobs ON records(namespace,key) WHERE "+ARTIST_JOBS)
                connection.execute("CREATE INDEX IF NOT EXISTS music_top_level_jobs ON records(namespace,key) WHERE "+TOP_LEVEL_JOBS)

    def rows(self, where):
        return [json.loads(r['body']) for r in self.state.select("SELECT body FROM records WHERE "+where)]

    def unfinished_albums(self): return self.rows(UNFINISHED_ALBUMS)
    def artist_jobs(self): return self.rows(ARTIST_JOBS)

    def job_count(self):
        return self.state.select("SELECT COUNT(*) AS total FROM records WHERE namespace='music_jobs'")[0]['total']

    def enabled(self):
        if not self.config: raise BackendError('UNAVAILABLE','SpotifyDL worker is not configured',True)

    def start(self):
        if self.config and not self.thread:
            self.stop.clear(); self.thread=threading.Thread(target=self.run,name='archive-music',daemon=True); self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread: self.thread.join(timeout=45)
        if self.thread and self.thread.is_alive(): raise BackendError('BUSY','Music importer is still stopping')

    def principal(self, value):
        return acquisition_auth.principal(self.service.auth, value)

    def get(self, namespace, id):
        identifier(id); row=self.state.get('music_'+namespace,id)
        if not row: raise BackendError('NOT_FOUND','Music request was not found')
        return row

    def accept(self, principal, action, args, create):
        identifier(args['requestId']); key=digest([principal.id,action,args['requestId']]); fingerprint=digest(args)
        with self.state.transaction():
            prior=self.state.get('music_requests',key)
            if prior:
                if prior['digest']!=fingerprint: raise BackendError('CONFLICT','This music request ID was used with different arguments')
                return acquisition_auth.visible(self.service.auth,principal,self.get(prior['namespace'],prior['id']))
            namespace,row=create()
            self.state.put('music_'+namespace,row['id'],row)
            self.state.put('music_requests',key,{'id':row['id'],'namespace':namespace,'digest':fingerprint,'createdAt':time.time()})
            self.state.audit(principal.id,'music.'+action,'accepted',row['id'])
            return row

    def call(self, principal, capability, args, _parent=None):
        principal=self.service.auth.authorize(principal,'acquire')
        if capability=='music.status':
            enabled=bool(self.config)
            return {'enabled':enabled,'available':enabled and self.health['available'],
                    'message':'Ready · source availability varies by recording' if self.health['available'] else 'The music worker is starting or unavailable. Existing files are safe.',
                    'provider':'spotDL 4.5.2','defaultDestination':{'driveId':'files','path':'Music'},'mcp':True}
        with self.lock:
            if capability=='music.lyrics.status': return self.lyrics.status()
            if capability=='music.lyrics.backfill':
                self.enabled(); return self.lyrics.backfill(principal,args['requestId'])
            if capability=='music.artwork.get': return {'dataUrl':acquisition_auth.visible(self.service.auth,principal,self.get('plans',args['id'])).get('_artwork')}
            if capability=='music.search.get': return public(acquisition_auth.visible(self.service.auth,principal,self.get('searches',args['id'])))
            if capability=='music.plan.get': return public(acquisition_auth.visible(self.service.auth,principal,self.get('plans',args['id'])))
            if capability=='music.jobs.get': return public(acquisition_auth.visible(self.service.auth,principal,self.get('jobs',args['id'])))
            if capability=='music.jobs.list':
                limit=args.get('limit',100); cursor=args.get('cursor','')
                if type(limit) is not int or not 1<=limit<=100: raise BackendError('INVALID_ARGUMENT','Music job page limit is 1–100')
                if cursor: identifier(cursor)
                rows=sorted((r for r in self.rows(TOP_LEVEL_JOBS) if acquisition_auth.accessible(self.service.auth,principal,r)),key=lambda r:(r['createdAt'],r['id']),reverse=True)
                if cursor:
                    index=next((i for i,r in enumerate(rows) if r['id']==cursor),None)
                    if index is None: raise BackendError('INVALID_ARGUMENT','Unknown job cursor')
                    rows=rows[index+1:]
                items=[]; size=0
                for row in rows[:limit]:
                    value=public(row); length=len(json.dumps(value,ensure_ascii=False).encode())
                    if items and size+length>2*1024*1024: break
                    items.append(value); size+=length
                return {'items':items,'cursor':items[-1]['id'] if len(rows)>len(items) else None}
            self.enabled()
            if capability in ('music.search','music.resolve'):
                if capability=='music.search':
                    if not isinstance(args['query'],str) or not 1<=len(args['query'].strip())<=300 or args['kind'] not in ('track','album','artist'): raise BackendError('INVALID_ARGUMENT','A search query and track/album/artist type are required')
                    offset=0
                    if args.get('cursor'):
                        c=self.get('cursors',args['cursor'])
                        if c['query']!=args['query'] or c['kind']!=args['kind'] or c['expires']<time.time(): raise BackendError('INVALID_ARGUMENT','Search cursor expired or belongs to another query')
                        offset=c['offset']
                    payload={'query':args['query'],'kind':args['kind'],'offset':offset}; namespace='searches'
                else: payload={'selection':selection(args['selection'])}; namespace='plans'
                def create():
                    if len(self.state.list('music_'+namespace,1000))>=1000: raise BackendError('QUOTA_EXCEEDED','Metadata history is full; wait for expired previews to clear')
                    active=sum(r['state'] in ('queued','running') for ns in ('searches','plans') for r in self.state.list('music_'+ns,1000))
                    if active>=20: raise BackendError('BUSY','The metadata queue is full',True)
                    id=uuid.uuid4().hex
                    base={'id':id,'state':'queued','error':None,'_payload':payload,'_actor':principal.as_dict(),'_created':time.time(),'_task':None}
                    if namespace=='searches': base.update(items=[],cursor=None)
                    else: base.update(revision=1,title='Loading release',artist='',year='',kind=payload['selection']['kind'],tracks=[],expectedTracks=0,expiresAt=time.time()+900)
                    return namespace,base
                return public(self.accept(principal,capability.split('.')[1],args,create))
            if capability=='music.enqueue':
                def create():
                    plan=acquisition_auth.visible(self.service.auth,principal,self.get('plans',args['planId']))
                    if type(args['planRevision']) is not int or plan['revision']!=args['planRevision']: raise BackendError('STALE_REVISION','Reload the album preview')
                    if plan['state']!='completed' or plan['expiresAt']<time.time(): raise BackendError('INVALID_ARGUMENT','Resolve this release again before downloading')
                    destination=location(args['destination']); self.service.auth.authorize(principal,'write',destination['driveId'],destination['path']); drive=next((d for d in self.service.drives(principal) if d['id']==destination['driveId']),None)
                    if not drive or not drive['capabilities']['write']: raise BackendError('PERMISSION_DENIED','Select a writable storage folder')
                    if self.service.catalog.filesystem(destination['driveId']).stat(destination['path']).kind!='directory': raise BackendError('INVALID_PATH','Destination must be an existing directory')
                    active=self.unfinished_albums(); artists=self.artist_jobs()
                    reserved=sum(1 for r in artists if r['state'] not in ('completed','canceled') for release in r['releases'] if not release['jobId'])
                    required=1+(len(plan['releases']) if plan['kind']=='artist' else 0)
                    if self.job_count()+reserved+required-(1 if _parent else 0)>HISTORY:
                        raise BackendError('QUOTA_EXCEEDED','Music history capacity is insufficient for every release in this request')
                    if len(active)>=100 or sum(len(r['tracks']) for r in active)+len(plan['tracks'])>5000: raise BackendError('QUOTA_EXCEEDED','Music queue limit reached')
                    if plan['kind']=='artist' and sum(r['state'] not in ('completed','canceled','failed','partial') for r in artists)>=100:
                        raise BackendError('QUOTA_EXCEEDED','The artist queue is full')
                    if plan['kind']=='artist':
                        return 'jobs',{'id':uuid.uuid4().hex,'revision':1,'kind':'artist','title':plan['title'],'artist':plan['artist'],'state':'queued','destination':destination,'createdAt':time.time(),'tracks':[],'releases':music_discography.create_releases(plan),'_actor':principal.as_dict()}
                    tracks=[]
                    for i,t in enumerate(plan['tracks']):
                        tracks.append({k:t[k] for k in ('id','title','artist','disc','number')})
                        tracks[-1].update(state='queued' if t['available'] else 'failed',attempt=0,error=None if t['available'] else 'Unavailable Spotify track',entry=None,
                            _position=i,_path=target(plan,t,destination),_durationMs=t['durationMs'],_task=None,_transfer=None,_available=t['available'])
                    row={'id':uuid.uuid4().hex,'revision':1,'title':plan['title'],'artist':plan['artist'],'state':'queued','destination':destination,'createdAt':time.time(),'tracks':tracks,'_actor':principal.as_dict()}
                    if _parent: row['_parentId']=_parent
                    return 'jobs',row
                return public(self.accept(principal,'enqueue',args,create))
            if capability=='music.jobs.control':
                job=acquisition_auth.visible(self.service.auth,principal,self.get('jobs',args['id']))
                if type(args['revision']) is not int or job['revision']!=args['revision']: raise BackendError('STALE_REVISION','The music job changed; refresh and try again')
                action=args['action']
                if action not in ('pause','resume','cancel','retry-failed'): raise BackendError('INVALID_ARGUMENT','Unknown music job action')
                if job['state'] in ('completed','canceled'): raise BackendError('CONFLICT','This music job is terminal')
                if action=='resume' and job['state']!='paused': raise BackendError('CONFLICT','Only paused jobs can resume')
                if job.get('kind')=='artist':
                    music_discography.control(self,principal,job,action)
                    self.state.audit(principal.id,capability,action,job['id']); return public(job)
                for track in job['tracks']:
                    if track['state'] in SUCCESS or action=='pause' and track['state'] in TERMINAL: continue
                    if action in ('cancel','pause'):
                        if track.get('_task'): self.worker.call(op='cancel',taskId=track['_task'])
                        if track.get('_transfer'):
                            result=self.service.transfers.query(principal,track['_transfer'])
                            if result['state']=='completed': track['entry']=self.imports.record(track,job['destination']['driveId'],result['entry'],principal);track['state']='completed';continue
                            if action=='cancel': self.service.transfers.cancel(principal,track['_transfer'],discard=True)
                        track['state']='canceled' if action=='cancel' else 'paused'
                        if action=='cancel' and track.get('_task'):
                            self.worker.call(op='cleanup',taskId=track['_task']);track['_task']=None
                    elif track['_available'] and (action=='resume' or track['state'] in ('failed','conflict','blocked-auth','waiting-for-drive')):
                        if track.get('_task'): self.worker.call(op='cleanup',taskId=track['_task'])
                        if track.get('_transfer'): self.service.transfers.cancel(principal,track['_transfer'],discard=True)
                        track.pop('_artifact',None)
                        track.update(state='queued',error=None,_task=None,_transfer=None,_retryAt=0)
                        # User retries reset the automatic retry budget, not the unique attempt counter.
                        track['_automatic']=0
                job['state']='canceled' if action=='cancel' else 'paused' if action=='pause' else 'queued'; self.save(job)
                self.state.audit(principal.id,capability,action,job['id']); return public(job)
        raise BackendError('INVALID_ARGUMENT','Unknown music capability')

    def save(self,job):
        job['revision']+=1; self.state.put('music_jobs',job['id'],job)

    def run(self):
        while not self.stop.is_set():
            try:
                with self.lock: self.tick()
                self.last_error=None
            except Exception as error:
                self.last_error=error.code if isinstance(error,BackendError) else 'INTERNAL_ERROR'
            self.stop.wait(1)

    def metadata(self):
        active=False
        for ns in ('searches','plans'):
            for row in self.state.list('music_'+ns,1000):
                if row['state'] not in ('queued','running'): continue
                try:
                    self.principal(row['_actor'])
                    if not row['_task']:
                        if active: continue
                        row['_task']=uuid.uuid4().hex; row['state']='running'
                        self.state.put('music_'+ns,row['id'],row)
                        result=self.worker.call(op='start',taskId=row['_task'],action='search' if ns=='searches' else 'resolve',payload=row['_payload'])
                    else:
                        result=self.worker.call(op='poll',taskId=row['_task'])
                        if result['state']=='absent':
                            self.worker.call(op='cleanup',taskId=row['_task']);row['_task']=None;row['state']='queued';self.state.put('music_'+ns,row['id'],row);continue
                    active=True
                    if result['state']=='completed':
                        data=result['result']
                        if ns=='searches':
                            if not isinstance(data['items'],list) or len(data['items'])>20: raise BackendError('INVALID_RESPONSE','Too many search results')
                            row['items']=[item(v) for v in data['items']]
                            if data.get('nextOffset') is not None:
                                if type(data['nextOffset']) is not int or not 0<data['nextOffset']<=1000: raise BackendError('INVALID_RESPONSE','Invalid search page')
                                cursor=uuid.uuid4().hex;row['cursor']=cursor;self.state.put('music_cursors',cursor,dict(row['_payload'],offset=data['nextOffset'],expires=time.time()+600))
                        elif row['kind']=='artist':
                            releases=artist_plan(data)
                            row.update(title=data['title'],artist=data['artist'],year='',tracks=[],expectedTracks=data['expectedTracks'],releases=releases,expiresAt=time.time()+900)
                        else:
                            if not isinstance(data['tracks'],list) or not 1<=len(data['tracks'])<=500 or type(data['expectedTracks']) is not int or len(data['tracks'])!=data['expectedTracks']: raise BackendError('INVALID_RESPONSE','The full album could not be resolved')
                            tracks=[item(t) for t in data['tracks']]
                            if any(t['kind']!='track' for t in tracks): raise BackendError('INVALID_RESPONSE','Invalid album tracks')
                            for field in ('title','artist','year','albumTitle','albumArtist'):
                                if not isinstance(data[field],str) or len(data[field])>500: raise BackendError('INVALID_RESPONSE','Invalid release metadata')
                            selection({'kind':'album','id':data['albumId']})
                            if row['_payload']['selection']['kind']=='album' and data['albumId']!=row['_payload']['selection']['id']: raise BackendError('INVALID_RESPONSE','Album identity did not match the requested release')
                            artist_id=data.get('artistId')
                            if artist_id is not None and (not isinstance(artist_id,str) or not re.fullmatch('[A-Za-z0-9]{22}',artist_id)): raise BackendError('INVALID_RESPONSE','Invalid artist identity')
                            artwork=data.get('artwork')
                            if artwork is not None and (not isinstance(artwork,str) or len(artwork)>128*1024 or not re.fullmatch(r'data:image/jpeg;base64,[A-Za-z0-9+/=]+',artwork)): raise BackendError('INVALID_RESPONSE','Invalid cover preview')
                            row['_artwork']=artwork
                            row.update(title=data['title'],artist=data['artist'],year=data['year'],tracks=tracks,expectedTracks=len(tracks),expiresAt=time.time()+900,
                                _albumId=data['albumId'],_albumTitle=data['albumTitle'],_albumArtist=data['albumArtist'],_artistId=artist_id)
                        row['state']='completed';row['error']=None;self.state.put('music_'+ns,row['id'],row);self.worker.call(op='cleanup',taskId=row['_task']);active=False
                    elif result['state'] in ('failed','canceled'):
                        row['state']='failed';row['error']='Spotify metadata is unavailable. Try again shortly or use a direct Spotify link.'
                        self.state.put('music_'+ns,row['id'],row);self.worker.call(op='cleanup',taskId=row['_task']);active=False
                except (KeyError,TypeError,ValueError):
                    row['state']='failed';row['error']='The provider returned incompatible metadata';self.state.put('music_'+ns,row['id'],row)
                except BackendError as error:
                    if error.code=='UNAVAILABLE': continue
                    row['state']='failed';row['error']=error.message;self.state.put('music_'+ns,row['id'],row)

    def maintenance(self):
        now=time.time()
        if now-getattr(self,'_maintained',0)<60: return
        self._maintained=now
        retained={r.get('_planId') for j in self.artist_jobs() if j['state'] not in ('completed','canceled') for r in j['releases']}
        for ns in ('searches','plans'):
            for row in self.state.list('music_'+ns,1000):
                if row['state'] in ('completed','failed') and row.get('_task'):
                    self.worker.call(op='cleanup',taskId=row['_task']);row['_task']=None;self.state.put('music_'+ns,row['id'],row)
                if row['state'] in ('completed','failed') and now-row['_created']>86400 and row['id'] not in retained: self.state.delete('music_'+ns,row['id'])
        for row in self.state.list('music_cursors',1000):
            if row['expires']<now:
                # Cursor values originally omitted IDs; remove expired keys directly.
                with self.state.transaction() as connection:
                    connection.execute("DELETE FROM records WHERE namespace='music_cursors' AND json_extract(body,'$.expires')<?",(now,))
                break
        # Only jobs still holding a worker task need cleanup; canonical JSON makes this a cheap SQL filter.
        for job in self.rows("namespace='music_jobs' AND instr(body,'\"_task\":\"')>0"):
            changed=False
            for track in job['tracks']:
                if track['state'] in TERMINAL and track.get('_task'):
                    self.worker.call(op='cleanup',taskId=track['_task']);self.state.delete('music_lyric_results',track['_task']);track['_task']=None;changed=True
            if changed: self.save(job)

    def tick(self):
        try:
            self.worker.call(op='status');self.health={'available':True,'at':time.time()}
        except BackendError:
            self.health={'available':False,'at':time.time()};return
        self.maintenance()
        self.metadata(); music_discography.tick(self)
        try: self.lyrics.tick()
        except Exception: pass # Optional lyrics never stop the audio coordinator.
        jobs=self.unfinished_albums()
        slots=sum(t['state'] in ('resolving-source','downloading','converting','verifying') for j in jobs for t in j['tracks']); imported=False
        for job in sorted(jobs,key=lambda j:j['createdAt']):
            if self.stop.is_set(): break
            if job.get('kind')=='artist' or job['state'] in ('paused','completed','canceled','partial','failed'): continue
            before=copy.deepcopy(job)
            try: principal=self.principal(job['_actor'])
            except BackendError:
                for track in job['tracks']:
                    if track['state'] not in TERMINAL:
                        if track.get('_task'): self.worker.call(op='cancel',taskId=track['_task'])
                        track.update(state='blocked-auth',error='The requesting credential was revoked')
                job['state']='failed';self.save(job);continue
            for track in job['tracks']:
                if self.stop.is_set(): break
                if track['state'] in TERMINAL: continue
                try:
                    if track['state'] in ('queued','waiting-for-drive'):
                        self.service.catalog.filesystem(job['destination']['driveId'])
                        if track.get('_artifact') and track.get('_task') and track['state']=='waiting-for-drive': track['state']='ready-to-import'
                        else:
                            if track.get('_retryAt',0)>time.time(): continue
                            existing=self.imports.present(principal,track,job['destination'])
                            if existing:
                                track.update(state='already-present',entry=existing,error=None)
                                try: self.lyrics.schedule(principal,track['id'],existing)
                                except BackendError: pass
                                continue
                            if slots>=2: continue
                            # Persist the task identity before sending it; retrying start is idempotent.
                            if track.get('_task') and self.worker.call(op='poll',taskId=track['_task'])['state']=='absent':
                                self.worker.call(op='cleanup',taskId=track['_task']);track['_task']=None
                            if not track.get('_task'):
                                track['_task']=uuid.uuid4().hex;track['attempt']+=1;self.save(job)
                            result=self.worker.call(op='start',taskId=track['_task'],action='download',payload={'trackId':track['id'],'durationMs':track['_durationMs']})
                            track.update(state='resolving-source',error=None,_epoch=result['epoch']); slots+=1
                    if track['state'] in ('resolving-source','downloading','converting','verifying'):
                        result=self.worker.call(op='poll',taskId=track['_task'])
                        if result['state']=='absent' or result['epoch']!=track.get('_epoch'):
                            self.worker.call(op='cleanup',taskId=track['_task']);track.update(state='queued',_task=None);slots-=1;continue
                        if result['state']=='completed':
                            artifact=dict(result['result']);lyrics=artifact.pop('lyrics',None)
                            if lyrics:
                                try: self.state.put('music_lyric_results',track['_task'],validate_result(lyrics))
                                except Exception: pass
                            object_fields(artifact,('sha256','sizeBytes','durationMs'))
                            if not isinstance(artifact['sha256'],str) or not re.fullmatch('[a-f0-9]{64}',artifact['sha256']) or not isinstance(artifact['sizeBytes'],str) or not artifact['sizeBytes'].isdigit() or not 0<int(artifact['sizeBytes'])<=512*1024**2: raise BackendError('INVALID_RESPONSE','Invalid audio artifact')
                            track.update(state='ready-to-import',_artifact=artifact);self.save(job);slots-=1
                        elif result['state'] in ('failed','canceled'):
                            self.worker.call(op='cleanup',taskId=track['_task']);track['_task']=None;slots-=1
                            automatic=track.get('_automatic',0)+1;track['_automatic']=automatic
                            if automatic<3: track.update(state='queued',_retryAt=time.time()+(5 if automatic==1 else 30),error='Source unavailable · retry scheduled')
                            else: track.update(state='failed',error='No verified source was available after three attempts')
                        else: track['state']=result.get('stage','downloading')
                    if track['state'] in ('ready-to-import','importing') and not imported:
                        existing=self.imports.present(principal,track,job['destination'])
                        if existing and not track.get('_transfer'):
                            track.update(state='already-present',entry=existing,error=None);self.save(job)
                            if track.get('_task'):
                                self.worker.call(op='cleanup',taskId=track['_task'])
                                self.state.delete('music_lyric_results',track['_task'])
                            continue
                        track['state']='importing';self.save(job)
                        track['entry']=self.imports.publish(principal,job,track,lambda:self.save(job))
                        track.update(state='completed',error=None);self.save(job);imported=True
                        self.worker.call(op='cleanup',taskId=track['_task'])
                except BackendError as error:
                    if error.code in ('BUSY','UNAVAILABLE'): continue
                    if error.code in ('DRIVE_UNAVAILABLE','VOLUME_MISMATCH','INSUFFICIENT_SPACE','READ_ONLY'): track.update(state='waiting-for-drive',error='Waiting for the selected drive or free space')
                    elif error.code in ('PERMISSION_DENIED','PERMISSION_REVOKED'): track.update(state='blocked-auth',error='Permission is no longer available')
                    else: track.update(state='conflict' if error.code=='CONFLICT' else 'failed',error=error.message[:500])
            states=[t['state'] for t in job['tracks']]
            if all(s in SUCCESS for s in states): job['state']='completed'
            elif all(s in TERMINAL for s in states): job['state']='partial' if any(s in SUCCESS for s in states) else 'failed'
            elif 'waiting-for-drive' in states: job['state']='waiting-for-drive'
            else: job['state']='running'
            if job!=before: self.save(job)
