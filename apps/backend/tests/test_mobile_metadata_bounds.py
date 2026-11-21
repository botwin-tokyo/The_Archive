import json
import time
import unittest
import uuid
from unittest.mock import patch
from cryptography.fernet import Fernet
from archive_backend.contracts import BackendError
from archive_backend.mobile_boundary import MobileBoundary
from archive_backend.mobile_contracts import stored_error,mobile_error
from support import Fixture


class MobileMetadataBoundsTests(unittest.TestCase):
    def setUp(self):
        self.f=Fixture(); self.boundary=MobileBoundary(self.f.service,{'publicOrigin':'https://fixture.invalid','replayKeyFile':'unused'},replay_key=Fernet.generate_key())
        self.boundary.auth.enroll(self.f.owner,'test','synthetic metadata password',['read','write','trash'],[{'driveId':'files','path':''}])
        self.session=self.boundary.auth.login({'username':'test','password':'synthetic metadata password','deviceLabel':'test','requestId':str(uuid.uuid4())})
        self.context=self.boundary.auth.authenticate(self.session['accessToken']); self.metadata=self.boundary.metadata
    def tearDown(self): self.boundary.close(); self.f.close()
    def query(self):
        root=self.boundary.profile(self.context)['roots'][0]
        return {'queryId':str(uuid.uuid4()),'partition':{'serverId':self.boundary.auth.server_id,'accountId':self.context.account_id,'rootId':root['id']},'folder':root['ref'],'cursor':None,'pageSize':100,'search':'','sort':{'field':'name','direction':'asc','foldersFirst':True}}
    def test_folder_scan_and_adapter_outside_outer_sql_transaction(self):
        self.f.put('a',b'a'); query=self.query(); fs=self.f.service.filesystems['files']; original=fs.iter_entries; scans=[]
        def scan(path):
            self.assertFalse(self.f.state.connection.in_transaction); scans.append(path)
            yield from original(path)
        with patch.object(fs,'iter_entries',side_effect=scan): result=self.metadata.folder(self.context,query)
        self.assertTrue(scans); self.assertEqual(result['entries'][0]['name'],'a')
        with self.f.state.transaction() as c:
            row=c.execute('SELECT * FROM listing_snapshots WHERE id=?',(result['snapshotId'],)).fetchone()
            self.assertEqual(row['revision'],int(result['cache']['revision']))
    def test_cursor_cannot_rebind_old_snapshot_to_a_new_generation(self):
        self.f.put('a'); self.f.put('b'); query=self.query(); query['pageSize']=1
        first=self.metadata.folder(self.context,query); query['cursor']=first['nextCursor']
        volume=self.f.service.filesystems['files'].volume; volume._generation+=1
        query['folder']['generation']=str(volume.generation)
        with self.assertRaises(BackendError) as error: self.metadata.folder(self.context,query)
        self.assertEqual(error.exception.code,'CURSOR_EXPIRED')
    def test_manifest_scan_outside_outer_sql_transaction(self):
        fs=self.f.service.filesystems['files']; fs.create_dir('folder'); self.f.put('folder/file')
        source=self.metadata.ref(self.metadata.current(self.context,'files','folder')); original=fs.iter_entries
        def scan(path):
            self.assertFalse(self.f.state.connection.in_transaction); yield from original(path)
        with patch.object(fs,'iter_entries',side_effect=scan): result=self.boundary.transfers.downloads.manifest(self.context,{'requestId':str(uuid.uuid4()),'source':source})
        self.assertEqual(result['fileCount'],1)
    def test_trash_over_500_explicitly_fails_not_truncated(self):
        entry=self.f.put('a'); root=self.boundary.profile(self.context)['roots'][0]['ref']
        with self.f.state.transaction():
            for i in range(501):
                item={'token':str(i),'drive':'files','source':'a','entry':entry}
                self.f.state.put('trash',str(i),item)
        with patch.object(self.metadata,'_trash_generation',return_value=int(root['generation'])):
            with self.assertRaises(BackendError) as error: self.metadata.trash(self.context,root)
        self.assertEqual(error.exception.code,'SCAN_LIMIT')
    def test_trash_query_not_limited_by_first_10000_foreign_drive_rows(self):
        entry=self.f.put('a'); root=self.boundary.profile(self.context)['roots'][0]['ref']
        with self.f.state.transaction() as c:
            body=json.dumps({'token':'other','drive':'backups','source':'a','entry':entry})
            c.executemany('INSERT INTO records(namespace,key,body,updated) VALUES(?,?,?,?)',[('trash','a'+str(i).zfill(5),body,time.time()) for i in range(10001)])
            self.f.state.put('trash','z-last',{'token':'z-last','drive':'files','source':'a','entry':entry})
        with patch.object(self.metadata,'_trash_generation',return_value=int(root['generation'])): result=self.metadata.trash(self.context,root)
        self.assertEqual(len(result),1); self.assertEqual(result[0]['ref']['id'],'z-last')
    def _create_trash(self):
        from archive_backend.mobile_auth import stamp
        source=self.metadata.ref(self.f.put('trashed'))
        intent={'requestId':str(uuid.uuid4()),'linkedRequestId':None,'kind':'trash','sources':[source],'destination':None,'name':None,'collisionPolicy':'ask','conditionsRefreshedAt':stamp(time.time())}
        self.metadata.execute(self.context,intent)
        while self.f.service.run_one(): pass
        return self.boundary.profile(self.context)['roots'][0]['ref']
    def test_trash_generation_proof_is_not_rebound_after_remount(self):
        root=self._create_trash(); before=self.metadata.trash(self.context,root); self.assertEqual(len(before),1)
        self.f.service.filesystems['files'].volume._generation+=1
        fresh=self.boundary.profile(self.context)['roots'][0]['ref']
        stale=self.metadata.trash(self.context,fresh)[0]
        self.assertEqual(stale['ref']['generation'],before[0]['ref']['generation'])
        self.assertEqual(stale['permissions'],[])
        self.assertIsNone(stale['parentRef'])
        forged=dict(before[0]['ref'],generation=fresh['generation'])
        with self.assertRaises(BackendError): self.metadata._validate_trash_source(self.context,forged)
    def test_trash_proof_uses_bounded_index_and_ignores_unrelated_malformed_intent(self):
        root=self._create_trash()
        with self.f.state.transaction() as c:
            c.execute("INSERT INTO records(namespace,key,body,updated) VALUES('intents','malformed-unrelated','{invalid',?)",(time.time(),))
            plan=c.execute("EXPLAIN QUERY PLAN SELECT key FROM records INDEXED BY mobile_intent_token WHERE namespace='intents' AND json_valid(body) AND json_extract(body,'$.token')=?",('example',)).fetchall()
        self.assertTrue(any('mobile_intent_token' in str(tuple(row)) for row in plan))
        self.assertEqual(len(self.metadata.trash(self.context,root)),1)
    def test_profile_negotiates_collision_precondition_semantics(self):
        self.assertEqual(self.boundary.profile(self.context)['capabilitiesVersion'],'mobile-v1.1')
    def test_missing_or_changed_trash_intent_is_explicitly_unavailable(self):
        root=self._create_trash(); item=self.f.state.list('trash')[0]
        with self.f.state.transaction() as c:
            row=c.execute("SELECT key,body FROM records WHERE namespace='intents' AND json_extract(body,'$.token')=?",(item['token'],)).fetchone()
        original=json.loads(row['body']); changed=dict(original,actor='different'); self.f.state.put('intents',row['key'],changed)
        unavailable=self.metadata.trash(self.context,root)[0]
        self.assertEqual(unavailable['ref']['generation'],'0')
        self.assertEqual(unavailable['permissions'],[])
        with self.assertRaises(BackendError): self.metadata._validate_trash_source(self.context,unavailable['ref'])
        self.f.state.delete('intents',row['key'])
        self.assertEqual(self.metadata.trash(self.context,root)[0]['permissions'],[])
    def test_unsupported_filename_fails_explicitly_instead_of_invalid_dto(self):
        (self.f.root/'files'/'bad\nname').write_bytes(b'bytes')
        with self.assertRaises(BackendError) as error: self.metadata.folder(self.context,self.query())
        self.assertEqual(error.exception.code,'UNSUPPORTED_ENTRY')
    def test_stored_error_has_stable_uuid_correlation(self):
        raw={'code':'INTEGRITY_FAILED','message':'Synthetic','correlationId':'legacy-job','retryable':False}
        a=mobile_error(stored_error(raw))['error']; b=mobile_error(stored_error(raw))['error']
        self.assertEqual(a,b); self.assertEqual(str(uuid.UUID(a['correlationId'])),a['correlationId'])


if __name__=='__main__': unittest.main()
