"""Strict mobile DTO adapters; all effects use the existing authorized domain."""
import json
import hashlib
import time
import re
from datetime import datetime
from .catalog import present_entry
from .contracts import BackendError, bytes_value, canonical, entry_ref, identifier, name, object_fields
from .mobile_auth import request_id, stamp
from .mobile_contracts import mobile_path,mobile_name

class TrashProofUnavailable(BackendError):
    """One historical item's proof is missing; not a storage/database failure."""


METHODS={'metadata.home','metadata.listFolder','metadata.stat','metadata.listTrash','operations.execute','operations.reconcile','operations.listReceipts'}


def legacy_ref(value):
    object_fields(value,('driveId','path','id','revision','generation'))
    mobile_path(value['path'])
    revision=bytes_value(value['revision']); bytes_value(value['generation'])
    result={k:value[k] for k in ('driveId','path','id')}; result['revision']=revision
    entry_ref(result)
    return result


class MobileMetadata:
    def __init__(self,boundary):
        self.boundary=boundary; self.service=boundary.service; self.state=self.service.state
    def validate_ref(self,context,value,*,stat=True):
        ref=legacy_ref(value)
        self.service.auth.authorize(context.principal,'read',ref['driveId'],ref['path'])
        fs=self.service.catalog.filesystem(ref['driveId'])
        if str(fs.volume.generation)!=value['generation']:
            raise BackendError('VOLUME_CHANGED','Source volume changed; refresh before acting')
        if stat: self.service.catalog.stat(context.principal,ref)
        return ref
    def ref(self,entry):
        mobile_path(entry['path'])
        fs=self.service.catalog.filesystem(entry['driveId'])
        return {'driveId':entry['driveId'],'path':entry['path'],'id':entry['id'],
                'revision':str(entry['revision']),'generation':str(fs.volume.generation)}
    def current(self,context,drive,path):
        self.service.auth.authorize(context.principal,'read',drive,path)
        fs=self.service.catalog.filesystem(drive)
        return present_entry(fs.stat(path),drive,self.service.auth,context.principal)
    def entry(self,context,raw):
        if raw['kind'] not in ('file','directory'):
            raise BackendError('UNSUPPORTED_ENTRY','Only regular files and folders are supported')
        parent=None
        if raw['path']:
            try: parent=self.ref(self.current(context,raw['driveId'],raw['path'].rpartition('/')[0]))
            except BackendError as e:
                if e.code not in ('PERMISSION_DENIED','NOT_FOUND'): raise
        permissions=[]
        caps=raw['capabilities']
        if caps.get('read'): permissions.extend(['read','copy'])
        if raw['kind']=='directory' and caps.get('read'): permissions.extend(['list','folderSearch'])
        for key in ('move','rename','trash'):
            if caps.get(key): permissions.append(key)
        try: self.service.auth.authorize(context.principal,'write',raw['driveId'],raw['path']); writable=True
        except BackendError: writable=False
        if writable:
            permissions.extend(['write','atomicKeepBoth'])
            if raw['kind']=='directory': permissions.append('mkdir')
            if caps.get('trash'): permissions.extend(['replaceViaTrash','restore'])
        at=datetime.fromisoformat(raw['modifiedAt'].replace('Z','+00:00')).timestamp()
        return {'ref':self.ref(raw),'parentRef':parent,'name':raw['name'],'kind':'folder' if raw['kind']=='directory' else 'file',
                'bytes':raw['sizeBytes'],'modifiedAt':stamp(at),'mediaTypeHint':None,'permissions':permissions}
    def _partition(self,context,partition,folder):
        object_fields(partition,('serverId','accountId','rootId'))
        if partition['serverId']!=self.boundary.auth.server_id or partition['accountId']!=context.account_id:
            raise BackendError('PERMISSION_DENIED','Cache partition belongs to another account')
        principal=self.service.auth.refresh(context.principal)
        for drive,path in principal.roots:
            rid=hashlib.sha256((drive+'\0'+path).encode()).hexdigest()[:32]
            if rid==partition['rootId'] and folder['driveId']==drive and (path=='' or folder['path']==path or folder['path'].startswith(path+'/')): return
        raise BackendError('PERMISSION_DENIED','Folder is outside the selected root')
    def folder(self,context,query):
        object_fields(query,('queryId','partition','folder','cursor','pageSize','search','sort'))
        identifier(query['queryId']); legacy_ref(query['folder']); self._partition(context,query['partition'],query['folder'])
        ref=self.validate_ref(context,query['folder'],stat=False)
        current_folder=self.current(context,ref['driveId'],ref['path'])
        if current_folder['id']!=ref['id'] or current_folder['kind']!='directory':
            raise BackendError('SOURCE_CHANGED','Selected folder identity changed')
        object_fields(query['sort'],('field','direction','foldersFirst'))
        if query['sort']['field'] not in ('name','modified','size') or type(query['sort']['foldersFirst']) is not bool:
            raise BackendError('INVALID_ARGUMENT','Invalid mobile sort')
        if query['cursor'] is not None and (not isinstance(query['cursor'],str) or len(query['cursor'])>512):
            raise BackendError('INVALID_ARGUMENT','Invalid directory cursor')
        legacy={'location':{'driveId':ref['driveId'],'path':ref['path']},'requestId':query['queryId'],
                'limit':query['pageSize'],'search':query['search'],'sort':query['sort']['field'],
                'direction':query['sort']['direction'],'foldersFirst':query['sort']['foldersFirst'],'scope':'directory'}
        if query['cursor']: legacy['cursor']=query['cursor']
        try: page,snapshot=self.service.catalog.list(context.principal,legacy,snapshot_details=True,snapshot_generation=query['folder']['generation'])
        except BackendError as e:
            if e.code=='STALE_REVISION' and query['cursor']:
                raise BackendError('CURSOR_EXPIRED','Folder page expired; refresh listing') from None
            if e.code=='QUOTA_EXCEEDED': raise BackendError('SCAN_LIMIT','Directory scan budget exceeded') from None
            raise
        # The catalog inserts bounded batches. Never hold an outer SQL writer
        # transaction while walking the directory or adapting filesystem metadata.
        entries=[self.entry(context,row) for row in page['entries'] if row['kind'] in ('file','directory')]
        self.validate_ref(context,query['folder'],stat=False)
        count=snapshot['count']
        folder_ref=self.ref(current_folder); folder_ref['revision']=str(page['revision'])
        return {'queryId':query['queryId'],'folder':folder_ref,'entries':entries,'nextCursor':page['nextCursor'],
                'snapshotId':snapshot['id'],'expiresAt':stamp(snapshot['expires']),
                'cache':{'partition':query['partition'],'fetchedAt':stamp(time.time()),'revision':str(page['revision']),'freshness':'fresh'},
                'searchScope':'current_folder','totalMatched':str(count),'scanLimitReached':False}
    def home(self,context):
        profile=self.boundary.profile(context)
        connection={'state':'connected' if any(r['available'] for r in profile['roots']) else 'permission_limited','profile':None,
                    'roots':profile['roots'],'capabilities':profile['capabilities'],'route':'unknown','capacity':profile['capacity'],'error':None,'demo':False}
        return {'connection':connection,'inbox':[],'inboxRef':None,'cache':None,
                'shortcuts':[{'id':r['id'],'label':r['label'],'ref':r['ref'],'status':'available' if r['available'] else 'missing','order':i} for i,r in enumerate(profile['roots'])],
                'transfers':[],'demo':False}
    def _trash_generation(self,item):
        # Legacy Trash rows may lack generation; their exact durable mutation
        # intent carries it. Never substitute whichever mount is current today.
        with self.state.transaction() as c:
            rows=c.execute("SELECT key,body FROM records INDEXED BY mobile_intent_token WHERE namespace='intents' AND json_valid(body) AND json_extract(body,'$.token')=? AND json_extract(body,'$.kind')='trash' LIMIT 2",(item['token'],)).fetchall()
        try:
            if len(rows)!=1: raise ValueError()
            intent=json.loads(rows[0]['body']); generation=intent['generation']
            if type(generation) is not int or not 1<=generation<=2**53-1 or intent['status']!='done': raise ValueError()
            if hashlib.sha256(rows[0]['key'].encode()).hexdigest()!=item['token']: raise ValueError()
            for key in ('token','drive','source','revision','device','inode','entry','deletedBy'):
                if intent[key]!=item[key]: raise ValueError()
            if intent['actor']!=item['deletedBy']['id']: raise ValueError()
            if item.get('generation',generation)!=generation: raise ValueError()
            return generation
        except (KeyError,TypeError,ValueError,AttributeError):
            raise TrashProofUnavailable('STATE_UNAVAILABLE','Trash generation proof requires operator reconciliation') from None
    def _validate_trash_source(self,context,source):
        item=self.state.get('trash',source['id'])
        if not isinstance(item,dict) or item.get('drive')!=source['driveId'] or item.get('source')!=source['path'] or str(item.get('revision'))!=source['revision']:
            raise BackendError('SOURCE_CHANGED','Selected Trash identity changed')
        generation=self._trash_generation(item)
        if str(generation)!=source['generation']: raise BackendError('VOLUME_CHANGED','Trash belongs to a different captured generation')
        self.service.catalog.filesystem(source['driveId']).volume.check(generation)
        self.service.auth.authorize(context.principal,'trash',source['driveId'],source['path'])
    def trash(self,context,root):
        ref=self.validate_ref(context,root,stat=False)
        current=self.current(context,ref['driveId'],ref['path'])
        if current['id']!=ref['id'] or current['kind']!='directory':
            raise BackendError('SOURCE_CHANGED','Selected Trash root identity changed')
        self.service.auth.authorize(context.principal,'trash')
        result=[]; after=''; scanned=0
        while True:
            with self.state.transaction() as c:
                rows=c.execute("SELECT key,body FROM records WHERE namespace='trash' AND key>? AND json_extract(body,'$.drive')=? ORDER BY key LIMIT 250",(after,ref['driveId'])).fetchall()
            if not rows: break
            for row in rows:
                after=row['key']; scanned+=1
                if scanned>10000: raise BackendError('SCAN_LIMIT','Trash scan exceeds the bounded mobile listing; narrow the selected root')
                item=json.loads(row['body']); path=item['source']
                if ref['path'] and not path.startswith(ref['path']+'/'): continue
                try: self.service.auth.authorize(context.principal,'trash',ref['driveId'],path)
                except BackendError as e:
                    if e.code=='PERMISSION_DENIED': continue
                    raise
                if item['entry']['kind'] not in ('file','directory'): continue
                if len(result)>=500: raise BackendError('SCAN_LIMIT','Trash exceeds 500 visible entries; narrow the selected root')
                try: generation=self._trash_generation(item)
                except TrashProofUnavailable: generation=0  # Explicit unavailable-ref sentinel, never current generation.
                fs=self.service.catalog.filesystem(ref['driveId'])
                fs.volume.check()
                entry=dict(item['entry']); entry['id']=item['token']
                adapted=self.entry(context,entry); adapted['ref']['generation']=str(generation)
                if generation==0 or fs.volume.generation!=generation:
                    # Preserve historical identity but offer no action. One stale
                    # item must not hide current, independently restorable items.
                    adapted['permissions']=[]; adapted['parentRef']=None
                result.append(adapted)
        self.validate_ref(context,root,stat=False)
        return result
    def call(self,context,request):
        object_fields(request,('method','params'))
        method=request['method']; params=request['params']
        if not isinstance(method,str) or method not in METHODS: raise BackendError('UNSUPPORTED_ENTRY','Mobile method unavailable')
        self.service.auth.refresh(context.principal)
        if method=='metadata.home': object_fields(params); return self.home(context)
        if method=='metadata.listFolder': return self.folder(context,params)
        if method=='metadata.stat':
            ref=self.validate_ref(context,params)
            return self.entry(context,self.service.catalog.stat(context.principal,ref))
        if method=='metadata.listTrash': return self.trash(context,params)
        if method=='operations.execute': return self.execute(context,params)
        if method=='operations.reconcile':
            object_fields(params,('requestId',)); return self.reconcile(context,request_id(params['requestId']))
        object_fields(params)
        result=[]
        with self.state.transaction() as c:
            ids=[r[0] for r in c.execute("SELECT key FROM records WHERE namespace='mobile_operations' AND json_extract(body,'$.actor')=? ORDER BY updated DESC LIMIT 100",(context.principal.id,))]
        for key in ids:
            record=self.state.get('mobile_operations',key)
            try: result.append(self._receipt(context,record))
            except BackendError as e:
                if e.code not in ('PERMISSION_DENIED','PERMISSION_REVOKED'): raise
        return result
    def _key(self,context,rid): return context.principal.id+':'+rid
    def _visible(self,context,record):
        if record is None: raise BackendError('NOT_FOUND','Operation receipt not found')
        if record['actor']!=context.principal.id: raise BackendError('PERMISSION_DENIED','Operation belongs to another device')
        intent=record['intent']
        for ref in [*intent['sources'],*([intent['destination']] if intent['destination'] else [])]:
            self.service.auth.authorize(context.principal,'read',ref['driveId'],ref['path'])
    def reconcile(self,context,rid):
        record=self.state.get('mobile_operations',self._key(context,rid))
        if record is None: raise BackendError('REQUEST_UNKNOWN','No durable operation receipt exists for this device/request')
        return self._receipt(context,record)
    def _outcome(self,source,label,outcome,error=None):
        return {'source':source,'name':label,'outcome':outcome,'resultRef':None,'error':error}
    def _receipt(self,context,record):
        self._visible(context,record); outcomes=[]; pending=False
        for row in record['items']:
            if row.get('outcome'):
                outcomes.append(row['outcome']); continue
            try: job=self.service.jobs.get(context.principal,row['jobId'])
            except BackendError as e:
                if e.code=='NOT_FOUND': raise BackendError('STATE_UNAVAILABLE','An admitted operation child is missing; operator reconciliation required') from None
                raise
            if job['state']=='needs-decision':
                outcomes.append(self._outcome(row['source'],row['name'],'needs_decision'))
                continue
            if job['state'] not in ('completed','failed','canceled'):
                pending=True; continue
            error=None; outcome='succeeded'
            if job['outcome']=='copied-source-remains': outcome='copied_source_remains'
            elif job['state'] in ('failed','canceled'):
                outcome='failed'; raw=job.get('error') or {'code':'INTERRUPTED','message':'Operation stopped','retryable':False}
                from .mobile_contracts import mobile_error, stored_error
                error=mobile_error(stored_error(raw))['error']
                if raw['code']=='CONFLICT': outcome='needs_decision'
            elif job['outcome']=='skipped': outcome='skipped'
            label=(job.get('resultingNames') or [row['name']])[0]
            outcomes.append(self._outcome(row['source'],label,outcome,error))
        kinds={x['outcome'] for x in outcomes}
        status='accepted' if pending else 'needs_decision' if 'needs_decision' in kinds else 'partial' if 'copied_source_remains' in kinds or ('failed' in kinds and len(kinds)>1) else 'failed' if 'failed' in kinds else 'completed'
        return {'requestId':record['intent']['requestId'],'jobId':record['id'],'status':status,'outcomes':outcomes,
                'decisionDraft':record['intent'] if 'needs_decision' in kinds else None,'demo':False}
    def _validate_intent(self,intent):
        object_fields(intent,('requestId','linkedRequestId','kind','sources','destination','name','collisionPolicy','conditionsRefreshedAt'))
        request_id(intent['requestId'])
        if intent['linkedRequestId'] is not None: request_id(intent['linkedRequestId'])
        if intent['kind'] not in ('mkdir','rename','move','copy','trash','restore') or intent['collisionPolicy'] not in ('ask','skip','keep_both','replace'):
            raise BackendError('INVALID_ARGUMENT','Invalid mobile operation')
        if not isinstance(intent['sources'],list) or len(intent['sources'])>500 or (intent['kind']!='mkdir' and not intent['sources']):
            raise BackendError('INVALID_ARGUMENT','Select a bounded set of exact sources')
        if intent['kind']=='mkdir' and intent['sources']: raise BackendError('INVALID_ARGUMENT','Folder creation has no source')
        if intent['kind']=='rename' and len(intent['sources'])!=1: raise BackendError('INVALID_ARGUMENT','Rename requires exactly one source')
        for ref in intent['sources']: legacy_ref(ref)
        identities=[(r['driveId'],r['id']) for r in intent['sources']]
        if len(set(identities))!=len(identities): raise BackendError('INVALID_ARGUMENT','Duplicate selected source')
        if intent['destination'] is not None: legacy_ref(intent['destination'])
        if intent['kind'] in ('mkdir','copy','move') and intent['destination'] is None: raise BackendError('INVALID_ARGUMENT','Destination required')
        if intent['kind'] in ('mkdir','rename'): mobile_name(intent['name'])
        elif intent['name'] is not None: raise BackendError('INVALID_ARGUMENT','This operation does not accept a new name')
        try:
            if not isinstance(intent['conditionsRefreshedAt'],str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?(?:Z|[+-]\d{2}:\d{2})',intent['conditionsRefreshedAt']): raise ValueError()
            at=datetime.fromisoformat(intent['conditionsRefreshedAt'].replace('Z','+00:00'))
            if at.tzinfo is None: raise ValueError()
        except (ValueError,AttributeError,TypeError): raise BackendError('INVALID_ARGUMENT','Timezone-aware condition timestamp required') from None
    def execute(self,context,intent):
        self._validate_intent(intent); key=self._key(context,intent['requestId']); digest=hashlib.sha256(canonical(intent).encode()).hexdigest()
        with self.state.transaction():
            prior=self.state.get('mobile_operations',key)
            if prior:
                if prior['digest']!=digest: raise BackendError('REQUEST_MISMATCH','Request ID is already bound to another operation')
                return self._receipt(context,prior)
            from .mobile_limits import reserve_request
            reserve_request(self.state)
            linked=intent['linkedRequestId']
            if linked:
                linked_record=self.state.get('mobile_operations',self._key(context,linked))
                if linked_record and linked_record['intent']['kind']!=intent['kind']:
                    raise BackendError('REQUEST_MISMATCH','Linked collision decisions must keep the operation kind')
                previous=self.reconcile(context,linked)
                if previous['status']=='accepted': raise BackendError('CONFLICT','Previous operation is still running; reconcile first')
                unresolved={(r['source']['driveId'],r['source']['id']) for r in previous['outcomes'] if r['source'] and r['outcome'] in ('failed','needs_decision')}
                if intent['kind']=='mkdir':
                    if previous['status'] not in ('needs_decision','failed'): raise BackendError('CONFLICT','Previous folder operation has already completed')
                elif any((r['driveId'],r['id']) not in unresolved for r in intent['sources']):
                    raise BackendError('CONFLICT','A linked decision cannot replay successful sources')
            record={'schemaVersion':1,'id':'mobile-op-'+intent['requestId'],'actor':context.principal.id,'sessionId':context.session_id,
                    'digest':digest,'intent':intent,'items':[],'createdAt':stamp(time.time())}
            self.state.put('mobile_operations',key,record)
            sources=intent['sources'] or [None]
            for i,source in enumerate(sources):
                label=intent['name'] or (source['path'].rsplit('/',1)[-1] if source else 'Folder')
                row={'source':source,'name':label,'requestId':intent['requestId']+':'+str(i)}
                try:
                    if source:
                        self.validate_ref(context,source,stat=intent['kind']!='restore')
                        if intent['kind']=='restore': self._validate_trash_source(context,source)
                    if intent['destination']:
                        self.validate_ref(context,intent['destination'])
                    request={'requestId':row['requestId'],'kind':'new-folder' if intent['kind']=='mkdir' else intent['kind'],
                             'parentRequestId':intent['requestId'],'conflictPolicy':intent['collisionPolicy'].replace('_','-')}
                    if source: request['targets']=[legacy_ref(source)]
                    if intent['destination']: request['destination']={k:intent['destination'][k] for k in ('driveId','path')}
                    if intent['name']: request['name']=intent['name']
                    receipt=self.service.operations.submit(context.principal,request)
                    if receipt['status']=='needs-decision': row['outcome']=self._outcome(source,label,'needs_decision')
                    else:
                        row['jobId']=receipt['jobIds'][0]
                        if intent['collisionPolicy']=='replace':
                            destination,target=self.service.operations._effective_destination(request,legacy_ref(source) if source else None)
                            fs=self.service.catalog.filesystem(destination['driveId'])
                            existing=self.service.operations._existing(fs,target)
                            expected=self.ref(present_entry(existing,destination['driveId'])) if existing else None
                            if expected: expected=legacy_ref(expected)
                            guard={'actor':context.principal.id,'drive':destination['driveId'],'target':target,'expected':expected,'generation':fs.volume.generation}
                            suffix=':mkdir' if intent['kind']=='mkdir' else ':0:publish' if intent['kind']=='copy' or (intent['kind']=='move' and source['driveId']!=destination['driveId']) else ':0'
                            self.state.put('mobile_collision_guards',row['jobId']+suffix,guard)
                except BackendError as error:
                    # Persistence failures must roll back aggregate + all admissions.
                    if error.code in ('STATE_UNAVAILABLE','INTERNAL_ERROR','INTEGRITY_FAILED'): raise
                    from .mobile_contracts import mobile_error, stored_error
                    row['outcome']=self._outcome(source,label,'needs_decision' if error.code=='CONFLICT' else 'failed',mobile_error(error)['error'])
                record['items'].append(row)
            self.state.put('mobile_operations',key,record)
        return self._receipt(context,record)
