"""Bounded disk-backed immutable listing snapshots, authorized on every page."""
import base64
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from .contracts import BackendError, canonical, digest, location, entry_ref, object_fields


def timestamp(seconds=None):
    return datetime.fromtimestamp(time.time() if seconds is None else seconds,timezone.utc).isoformat().replace('+00:00','Z')


def present_entry(entry,drive_id,auth=None,principal=None):
    supported=entry.kind in ('file','directory')
    read=write=trash=False
    if auth is not None:
        for scope in ('read','write','trash'):
            try: auth.authorize(principal,scope,drive_id,entry.path)
            except BackendError: allowed=False
            else: allowed=True
            if scope=='read': read=allowed
            elif scope=='write': write=allowed
            else: trash=allowed
    else: read=write=trash=True
    ref_id=hashlib.sha256(f'{drive_id}:{entry.device}:{entry.inode}:{entry.path}'.encode()).hexdigest()[:32]
    return {'id':ref_id,'driveId':drive_id,'path':entry.path,'revision':entry.revision,
            'name':entry.name or drive_id,'parentPath':entry.path.rpartition('/')[0],
            'kind':entry.kind,'type':'Folder' if entry.kind=='directory' else ('Symbolic link' if entry.kind=='symlink' else 'File'),
            'sizeBytes':str(entry.size) if entry.kind=='file' else None,'createdAt':None,
            'modifiedAt':timestamp(entry.mtime_ns/1e9),'capabilities':{
                'read':read and supported,'download':read and supported,'copy':read and supported,
                'move':write and supported,'rename':write and supported,'trash':trash and supported,
                **({'reason':'Unsupported entry type'} if not supported else {})}}


class Catalog:
    def __init__(self,state,auth,filesystems,*,clock=time.time,max_entries=200000,ttl=300):
        self.state,self.auth,self.filesystems=state,auth,filesystems
        self.clock,self.max_entries,self.ttl=clock,max_entries,ttl
        state.connection.execute('PRAGMA temp_store=FILE')
        with state.transaction() as c:
            c.execute('CREATE TABLE IF NOT EXISTS listing_snapshots(id TEXT PRIMARY KEY,actor TEXT,binding TEXT,revision INTEGER,expires REAL,count INTEGER,scope_version INTEGER)')
            c.execute('CREATE TABLE IF NOT EXISTS listing_rows(snapshot TEXT,ordinal INTEGER,folder INTEGER,name TEXT,modified INTEGER,kind TEXT,size INTEGER,body TEXT,PRIMARY KEY(snapshot,ordinal))')
            c.execute('CREATE INDEX IF NOT EXISTS listing_sort ON listing_rows(snapshot,folder,name,ordinal)')

    def filesystem(self,drive_id):
        try: fs=self.filesystems[drive_id]
        except KeyError: raise BackendError('DRIVE_UNAVAILABLE','Configured drive is unavailable') from None
        fs.volume.check()
        return fs

    def stat(self,principal,ref):
        entry_ref(ref)
        self.auth.authorize(principal,'read',ref['driveId'],ref['path'])
        entry=self.filesystem(ref['driveId']).stat(ref['path'])
        if entry.revision != ref['revision']:
            raise BackendError('STALE_REVISION','Entry changed; refresh before acting')
        value=present_entry(entry,ref['driveId'],self.auth,principal)
        if value['id'] != ref['id']:
            raise BackendError('STALE_REVISION','Entry identity changed')
        return value

    def list(self,principal,query,*,snapshot_details=False,snapshot_generation=None):
        object_fields(query,('location','requestId'),('cursor','limit','sort','direction','foldersFirst','search','scope'))
        loc=location(query['location']); fresh=self.auth.authorize(principal,'read',loc['driveId'],loc['path'])
        limit=query.get('limit',100)
        if type(limit) is not int or not 1<=limit<=500:
            raise BackendError('INVALID_ARGUMENT','Listing page limit out of range')
        if query.get('scope','directory') != 'directory':
            raise BackendError('UNSUPPORTED_ENTRY','Recursive search is not an implicit directory scan')
        sort=query.get('sort','name'); direction=query.get('direction','asc')
        if sort not in ('name','modified','type','size') or direction not in ('asc','desc'):
            raise BackendError('INVALID_ARGUMENT','Invalid listing sort')
        search=query.get('search','')
        if not isinstance(search,str) or len(search)>255: raise BackendError('INVALID_ARGUMENT','Invalid name query')
        binding=digest({k:query.get(k,d) for k,d in [('location',None),('sort','name'),('direction','asc'),('foldersFirst',True),('search',''),('scope','directory')]})
        if snapshot_generation is not None: binding=digest({'query':binding,'generation':snapshot_generation})
        cursor=query.get('cursor'); offset=0
        if cursor:
            try:
                if len(cursor)>512: raise ValueError()
                decoded=json.loads(base64.urlsafe_b64decode(cursor+'='*((-len(cursor))%4)))
                sid,offset=decoded
                if not isinstance(sid,str) or len(sid)!=32 or type(offset) is not int or offset<0: raise ValueError()
            except (ValueError,TypeError): raise BackendError('INVALID_ARGUMENT','Invalid listing cursor') from None
        else:
            sid=uuid.uuid4().hex
            fs=self.filesystem(loc['driveId']); before=fs.stat(loc['path'])
            if before.kind != 'directory': raise BackendError('INVALID_ARGUMENT','Listing target is not a directory')
            with self.state.transaction() as c:
                stale=[r[0] for r in c.execute('SELECT id FROM listing_snapshots WHERE expires<? OR actor=?',(self.clock(),fresh.id))]
                # Keep at most two prior active snapshots per actor. Expired snapshots are metadata only.
                active=[r[0] for r in c.execute('SELECT id FROM listing_snapshots WHERE actor=? AND expires>=? ORDER BY expires DESC',(fresh.id,self.clock()))]
                remove=set(stale)-set(active[:2])
                for old in remove:
                    c.execute('DELETE FROM listing_rows WHERE snapshot=?',(old,)); c.execute('DELETE FROM listing_snapshots WHERE id=?',(old,))
                all_snapshots=[r[0] for r in c.execute('SELECT id FROM listing_snapshots ORDER BY expires DESC LIMIT -1 OFFSET 31')]
                for old in all_snapshots:
                    c.execute('DELETE FROM listing_rows WHERE snapshot=?',(old,)); c.execute('DELETE FROM listing_snapshots WHERE id=?',(old,))
                c.execute('INSERT INTO listing_snapshots VALUES(?,?,?,?,?,?,?)',(sid,fresh.id,binding,before.revision,self.clock()+self.ttl,0,fresh.scope_version))
            count=0
            try:
                rows=[]; scanned=0
                for entry in fs.iter_entries(loc['path']):
                    scanned+=1
                    if scanned>self.max_entries: raise BackendError('QUOTA_EXCEEDED','Directory scan budget exhausted')
                    if search.casefold() not in entry.name.casefold(): continue
                    self.auth.authorize(fresh,'read',loc['driveId'],entry.path)
                    count+=1
                    if count>self.max_entries: raise BackendError('QUOTA_EXCEEDED','Directory exceeds configured snapshot budget')
                    value=present_entry(entry,loc['driveId'],self.auth,fresh)
                    rows.append((sid,count,0 if entry.kind=='directory' else 1,entry.name,entry.mtime_ns,entry.kind,entry.size,canonical(value)))
                    if len(rows)>=250:
                        with self.state.transaction() as c: c.executemany('INSERT INTO listing_rows VALUES(?,?,?,?,?,?,?,?)',rows)
                        rows=[]
                with self.state.transaction() as c:
                    c.executemany('INSERT INTO listing_rows VALUES(?,?,?,?,?,?,?,?)',rows)
                    if fs.stat(loc['path']).revision != before.revision: raise BackendError('SOURCE_CHANGED','Directory changed during snapshot; refresh')
                    c.execute('UPDATE listing_snapshots SET count=? WHERE id=?',(count,sid))
            except BaseException:
                with self.state.transaction() as c:
                    c.execute('DELETE FROM listing_rows WHERE snapshot=?',(sid,)); c.execute('DELETE FROM listing_snapshots WHERE id=?',(sid,))
                raise
        with self.state.transaction() as c:
            snapshot=c.execute('SELECT * FROM listing_snapshots WHERE id=?',(sid,)).fetchone()
            if snapshot is None or snapshot['actor']!=fresh.id or snapshot['binding']!=binding or snapshot['expires']<self.clock() or snapshot['scope_version']!=fresh.scope_version:
                raise BackendError('STALE_REVISION','Listing snapshot expired or query changed; refresh')
            self.filesystem(loc['driveId'])
            column={'name':'name','modified':'modified','type':'kind','size':'size'}[sort]
            order=('folder ASC,' if query.get('foldersFirst',True) else '')+column+' '+direction.upper()+',name ASC,ordinal ASC'
            entries=[json.loads(r[0]) for r in c.execute('SELECT body FROM listing_rows WHERE snapshot=? ORDER BY '+order+' LIMIT ? OFFSET ?',(sid,limit,offset))]
            next_offset=offset+len(entries)
            next_cursor=base64.urlsafe_b64encode(canonical([sid,next_offset]).encode()).decode().rstrip('=') if next_offset<snapshot['count'] else None
            result={'schemaVersion':1,'requestId':query['requestId'],'location':loc,'revision':snapshot['revision'],'entries':entries,'nextCursor':next_cursor,'total':snapshot['count'],'stale':False}
            if snapshot_details:
                count=c.execute("SELECT COUNT(*) FROM listing_rows WHERE snapshot=? AND kind IN ('file','directory')",(sid,)).fetchone()[0]
                return result,{'id':sid,'expires':snapshot['expires'],'count':count}
            return result
