"""Durable agent-owned radio batches. Audio and HTTP never run in DB transactions."""
import json
import time
import uuid
from .contracts import BackendError, CAPABILITIES, digest, entry_ref
from .radio_contracts import LIMITS, tool_schema, validate
from .security import Principal

TERMINAL = {'completed','completed-with-errors','canceled'}
ACTIVE = {'queued','preparing','sending','pausing','canceling'}


def public(row):
    return {k:v for k,v in row.items() if not k.startswith('_')}


class RadioService:
    def __init__(self, service, config):
        self.service=service; self.state=service.state; self.auth=service.auth; self.config=config
        self.worker=None
        with self.state.transaction() as c:
            c.execute('CREATE TABLE IF NOT EXISTS radio_batches(id TEXT PRIMARY KEY,actor TEXT NOT NULL,station TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,body TEXT NOT NULL)')
            c.execute('CREATE INDEX IF NOT EXISTS radio_batch_actor ON radio_batches(actor,id)')
            c.execute('CREATE INDEX IF NOT EXISTS radio_batch_schedule ON radio_batches(station,state,created)')
            c.execute("CREATE INDEX IF NOT EXISTS radio_pending_station ON radio_batches(station,created,id) WHERE state IN ('queued','preparing','sending','pausing','canceling')")
            c.execute("CREATE INDEX IF NOT EXISTS radio_active_count ON radio_batches(state) WHERE state NOT IN ('completed','completed-with-errors','canceled')")
            c.execute("CREATE INDEX IF NOT EXISTS radio_cleanup ON radio_batches(id) WHERE state IN ('completed','completed-with-errors','canceled') AND COALESCE(json_extract(body,'$._spoolCleaned'),0)=0")
            c.execute('CREATE TABLE IF NOT EXISTS radio_items(batch TEXT NOT NULL,id TEXT NOT NULL,position INTEGER NOT NULL,state TEXT NOT NULL,body TEXT NOT NULL,PRIMARY KEY(batch,id))')
            c.execute('CREATE INDEX IF NOT EXISTS radio_item_order ON radio_items(batch,position)')
            c.execute("CREATE INDEX IF NOT EXISTS radio_spool_refs ON radio_items(json_extract(body,'$._manifest.spoolName'))")
            c.execute('CREATE TABLE IF NOT EXISTS radio_requests(actor TEXT,operation TEXT,request TEXT,digest TEXT,receipt TEXT,PRIMARY KEY(actor,operation,request))')
            c.execute('CREATE TABLE IF NOT EXISTS radio_leases(station TEXT PRIMARY KEY,owner TEXT,fence INTEGER NOT NULL,expires REAL NOT NULL)')

    def start(self):
        if self.config and self.config['enabled']:
            from .radio_sources import cleanup_orphans
            cleanup_orphans(self)
            from .radio_delivery import RadioDispatcher
            self.worker=RadioDispatcher(self);self.worker.start()

    def close(self):
        if self.worker:self.worker.close();self.worker=None

    def station(self,principal,station_id):
        fresh=self.auth.authorize(principal,'radio');self.auth.authorize(fresh,'read')
        station=next((s for s in (self.config or {}).get('stations',[]) if s['id']==station_id),None)
        if station is None or (fresh.kind!='owner' and fresh.id not in station['agentIds']):
            raise BackendError('PERMISSION_DENIED','Radio station is not granted to this identity')
        return station

    def source(self,principal,ref,*,check=True):
        entry_ref(ref)
        if not any(ref['driveId']==r['driveId'] and (not r['path'] or ref['path']==r['path'] or ref['path'].startswith(r['path']+'/')) for r in (self.config or {}).get('roots',[])):
            raise BackendError('PERMISSION_DENIED','Track is outside configured Music roots')
        self.auth.authorize(principal,'read',ref['driveId'],ref['path'])
        if not ref['path'].lower().endswith('.mp3'):raise BackendError('UNSUPPORTED_ENTRY','Only MP3 files can be delivered')
        if check:
            e=self.service.catalog.stat(principal,ref)
            if e['kind']!='file' or int(e['sizeBytes'])>int(LIMITS['maxFileBytes']):
                raise BackendError('UNSUPPORTED_ENTRY','Track must be a regular MP3 within the size limit')
            return e

    def principal(self,batch):
        p=batch['_principal']
        return self.auth.refresh(Principal(p['id'],p['label'],p['kind'],p['credentialId'],p['scopeVersion'],tuple(p['scopes']),tuple((r['driveId'],r['path']) for r in p['roots'])))

    def batch(self,bid):
        with self.state._lock:
            row=self.state.connection.execute('SELECT body FROM radio_batches WHERE id=?',(bid,)).fetchone()
            if row is None:raise BackendError('NOT_FOUND','Radio batch not found')
            return json.loads(row[0])

    def items(self,bid):
        with self.state._lock:
            return [json.loads(r[0]) for r in self.state.connection.execute('SELECT body FROM radio_items WHERE batch=? ORDER BY position',(bid,))]

    def save(self,b):
        b['updatedAt']=time.time();b['revision']+=1
        self.state.connection.execute('INSERT INTO radio_batches VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,created=excluded.created,body=excluded.body',
                                      (b['id'],b['actorId'],b['stationId'],b['state'],b.get('queuedAt',b['createdAt']),json.dumps(b)))

    def save_item(self,bid,i):
        self.state.connection.execute('INSERT INTO radio_items VALUES(?,?,?,?,?) ON CONFLICT(batch,id) DO UPDATE SET position=excluded.position,state=excluded.state,body=excluded.body',
                                      (bid,i['id'],i['position'],i['state'],json.dumps(i)))

    def access(self,p,b):
        self.station(p,b['stationId'])
        if p.kind!='owner' and b['actorId']!=p.id:raise BackendError('PERMISSION_DENIED','Batch belongs to another identity')
        for i in self.items(b['id']):self.source(p,i['source'],check=False)

    def event(self,b,action,actor=None):
        self.state.audit(actor or b['actorId'],'radio.'+action,b['state'],b['id'])
        self.state.emit('radio.batch',b['actorId'],{'batchId':b['id'],'revision':b['revision'],'state':b['state']})

    def call(self,p,cap,args):
        validate(args,tool_schema(CAPABILITIES[cap]));p=self.auth.authorize(p,'radio');self.auth.authorize(p,'read')
        op=cap.removeprefix('radio.')
        if op=='status':
            stations=[]
            for station in (self.config or {}).get('stations',[]):
                try:self.station(p,station['id'])
                except BackendError:continue
                stations.append({'id':station['id'],'receiverId':station['receiverId'],'configured':bool(station['endpoint']), 'enabled':bool(self.config['enabled'])})
            roots=[]
            for root in (self.config or {}).get('roots',[]):
                # Advertise intersections, including agents granted a narrower subtree.
                candidates=[root] if p.kind=='owner' else [dict(driveId=d,path=path if path.startswith(root['path']+'/') or not root['path'] else root['path']) for d,path in p.roots if d==root['driveId'] and (not path or not root['path'] or path==root['path'] or path.startswith(root['path']+'/') or root['path'].startswith(path+'/'))]
                for candidate in candidates:
                    if candidate not in roots:roots.append(candidate)
            return {'enabled':bool(self.config and self.config['enabled']),'stations':stations,'roots':roots,'limits':LIMITS}
        if op=='tracks.list':
            from .radio_sources import list_tracks
            return list_tracks(self,p,args)
        if op=='requests.reconcile':
            with self.state._lock:
                row=self.state.connection.execute('SELECT receipt FROM radio_requests WHERE actor=? AND operation=? AND request=?',(p.id,args['operation'],args['requestId'])).fetchone()
                if not row:return None
                receipt=json.loads(row[0]);self.access(p,self.batch(receipt['id']));return receipt
        if op=='batches.list':
            after=args.get('cursor','');limit=args.get('limit',100);result=[];cursor=after
            # Page the authorized actor's indexed rows, then apply station/root grants.
            with self.state._lock:
                rows=self.state.connection.execute('SELECT id,body FROM radio_batches WHERE id>?'+(' AND actor=?' if p.kind!='owner' else '')+' ORDER BY id LIMIT ?', (after,p.id,limit+1) if p.kind!='owner' else (after,limit+1)).fetchall()
            for row in rows[:limit]:
                cursor=row[0];b=json.loads(row[1])
                try:self.access(p,b)
                except BackendError:continue
                result.append(public(b))
            return {'batches':result,'nextCursor':cursor if len(rows)>limit else None}
        if op=='batches.get':
            b=self.batch(args['batchId']);self.access(p,b);items=self.items(b['id'])
            try:offset=int(args.get('cursor','0'))
            except ValueError:raise BackendError('INVALID_ARGUMENT','Invalid item cursor') from None
            if offset<0 or offset>1024:raise BackendError('INVALID_ARGUMENT','Invalid item cursor')
            limit=args.get('limit',100);end=offset+limit
            return {'batch':public(b),'items':[public(i) for i in items[offset:end]],'nextCursor':str(end) if end<len(items) else None}
        action=op.removeprefix('batches.')
        with self.state._lock:
            prior=self.state.connection.execute('SELECT digest,receipt FROM radio_requests WHERE actor=? AND operation=? AND request=?',(p.id,action,args['requestId'])).fetchone()
        if prior:
            if prior[0]!=digest(args):raise BackendError('CONFLICT','Request ID already used with different arguments')
            receipt=json.loads(prior[1]);self.access(p,self.batch(receipt['id']));return receipt
        # Filesystem validation outside SQLite writer locks.
        validated={}
        if action=='add':
            b=self.batch(args['batchId']);self.access(p,b)
            for ref in args['tracks']:
                e=self.source(p,ref);validated[digest(ref)]=(e['sizeBytes'],self.service.catalog.filesystem(ref['driveId']).volume.generation)
        with self.state.transaction() as c:
            prior=c.execute('SELECT digest,receipt FROM radio_requests WHERE actor=? AND operation=? AND request=?',(p.id,action,args['requestId'])).fetchone()
            if prior:
                if prior[0]!=digest(args):raise BackendError('CONFLICT','Request ID was already used with different arguments')
                receipt=json.loads(prior[1]);self.access(p,self.batch(receipt['id']));return receipt
            if action=='create':
                self.station(p,args['stationId'])
                if c.execute("SELECT COUNT(*) FROM radio_batches INDEXED BY radio_active_count WHERE state NOT IN ('completed','completed-with-errors','canceled')").fetchone()[0]>=64:
                    raise BackendError('QUOTA_EXCEEDED','Radio active batch limit reached')
                b={'id':str(uuid.uuid4()),'actorId':p.id,'stationId':args['stationId'],'name':args['name'],'revision':0,'state':'draft','createdAt':time.time(),'itemCount':0,'totalBytes':'0','deliveredCount':0,'confirmedBytes':'0','error':None,'_principal':p.as_dict()}
            else:
                b=self.batch(args['batchId']);self.access(p,b)
                if b['revision']!=args['revision']:raise BackendError('STALE_REVISION','Radio batch changed; refresh it')
                items=self.items(b['id'])
                if action in ('add','remove','reorder'):
                    if b['state']!='draft':raise BackendError('CONFLICT','Pushed batch membership is frozen')
                    if action=='add':
                        existing={digest(i['source']) for i in items}
                        for ref in args['tracks']:
                            if digest(ref) in existing:continue
                            if len(items)>=1024:raise BackendError('QUOTA_EXCEEDED','Track limit reached')
                            item={'id':str(uuid.uuid4()),'position':len(items),'source':ref,'state':'pending','deliveryId':str(uuid.uuid4()),'attempts':0,'error':None,'receipt':None,'uploadedBytes':'0','sizeBytes':validated[digest(ref)][0],'_sourceGeneration':validated[digest(ref)][1]}
                            existing.add(digest(ref));items.append(item);self.save_item(b['id'],item)
                    else:
                        by_id={i['id']:i for i in items}
                        if not set(args['itemIds'])<=set(by_id):raise BackendError('INVALID_ARGUMENT','Unknown item')
                        if action=='remove':
                            for iid in args['itemIds']:c.execute('DELETE FROM radio_items WHERE batch=? AND id=?',(b['id'],iid))
                            items=[i for i in items if i['id'] not in args['itemIds']]
                        else:
                            if set(args['itemIds'])!=set(by_id):raise BackendError('INVALID_ARGUMENT','Reorder requires every item exactly once')
                            items=[by_id[iid] for iid in args['itemIds']]
                        for index,item in enumerate(items):item['position']=index;self.save_item(b['id'],item)
                    b['itemCount']=len(items);b['totalBytes']=str(sum(int(i['sizeBytes']) for i in items))
                elif action=='push':
                    if not self.config or not self.config['enabled'] or not self.station(p,b['stationId'])['endpoint']:raise BackendError('UNAVAILABLE','Radio sending is not configured and enabled')
                    if b['state']!='draft' or not items:raise BackendError('CONFLICT','A nonempty draft is required')
                    b['state']='queued';b['_principal']=p.as_dict();b['queuedAt']=time.time();b['_receiverId']=self.station(p,b['stationId'])['receiverId']
                elif action=='control':self.control(b,items,args['action'],p)
                else:raise BackendError('INVALID_ARGUMENT','Unknown radio action')
            self.save(b);self.event(b,action,p.id);receipt=public(b)
            c.execute('INSERT INTO radio_requests VALUES(?,?,?,?,?)',(p.id,action,args['requestId'],digest(args),json.dumps(receipt)))
            return receipt

    def control(self,b,items,action,p):
        live=any(i.get('_uncertain') or i['state'] in ('preparing','sending','reconciling') for i in items)
        if action=='cancel':
            if b['state'] in TERMINAL:return
            b['state']='canceling' if live else 'canceled'
            if not live:
                for i in items:
                    if i['state']!='delivered':i['state']='canceled';self.save_item(b['id'],i)
        elif action=='pause':
            if b['state'] not in ACTIVE:raise BackendError('CONFLICT','Batch is not active')
            b['state']='pausing' if live else 'paused'
        elif action in ('resume','retry-failed'):
            if b['state'] not in ('paused','blocked'):raise BackendError('CONFLICT','Batch must be paused or blocked')
            if not self.config['enabled']:raise BackendError('UNAVAILABLE','Radio sending disabled')
            b['_principal']=p.as_dict();b['state']='queued';b['error']=None
            for i in items:
                if i['state']=='failed':i['state']='reconciling' if i.get('_manifest') else 'pending';i['attempts']=0;i['firstAttemptAt']=time.time();i['retryAt']=0;i['error']=None;self.save_item(b['id'],i)
        elif action=='skip-failed':
            if b['state']!='blocked':raise BackendError('CONFLICT','Batch is not blocked')
            for i in items:
                if i['state']=='failed':
                    if i.get('_uncertain'):raise BackendError('CONFLICT','Reconcile uncertain delivery before skipping')
                    i['state']='canceled';self.save_item(b['id'],i)
            b['state']='queued';b['error']=None
