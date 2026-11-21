import copy
import unittest
import uuid
from cryptography.fernet import Fernet
from archive_backend.contracts import BackendError
from archive_backend.mobile_auth import stamp
from archive_backend.mobile_boundary import MobileBoundary
from support import Fixture
import time


class MobileMetadataTests(unittest.TestCase):
    def setUp(self):
        self.f=Fixture(); self.boundary=MobileBoundary(self.f.service,{'publicOrigin':'https://archive.invalid','replayKeyFile':'unused'},replay_key=Fernet.generate_key())
        self.boundary.auth.enroll(self.f.owner,'test','synthetic metadata password',['read','write','trash'],[{'driveId':'files','path':''},{'driveId':'backups','path':''}])
        response=self.boundary.auth.login({'username':'test','password':'synthetic metadata password','deviceLabel':'test','requestId':str(uuid.uuid4())})
        self.context=self.boundary.auth.authenticate(response['accessToken']); self.metadata=self.boundary.metadata
    def tearDown(self): self.boundary.close(); self.f.close()
    def roots(self): return self.boundary.profile(self.context)['roots']
    def root(self,drive='files'): return next(r for r in self.roots() if r['ref']['driveId']==drive)
    def ref(self,entry): return self.metadata.ref(entry)
    def query(self,**changes):
        root=self.root(); value={'queryId':str(uuid.uuid4()),'partition':{'serverId':self.boundary.auth.server_id,'accountId':self.context.account_id,'rootId':root['id']},'folder':root['ref'],'cursor':None,'pageSize':100,'search':'','sort':{'field':'name','direction':'asc','foldersFirst':True}}
        value.update(changes); return value
    def intent(self,kind,sources=None,**changes):
        value={'requestId':str(uuid.uuid4()),'linkedRequestId':None,'kind':kind,'sources':sources or [],'destination':None,'name':None,'collisionPolicy':'ask','conditionsRefreshedAt':stamp(time.time())}
        value.update(changes); return value
    def call(self,method,params): return self.boundary.call(self.context,{'method':method,'params':params})
    def finish(self):
        while self.f.service.run_one(): pass
    def test_three_roots_include_unavailable_drive_without_hiding_available_roots(self):
        roots=[{'driveId':d,'path':''} for d in ('files','backups','offline')]
        self.boundary.auth.assign(self.f.owner,['read','write','trash'],roots)
        profile=self.boundary.profile(self.context)
        self.assertEqual(len(profile['roots']),3)
        self.assertEqual({r['ref']['driveId'] for r in profile['roots']},{'files','backups','offline'})
        self.assertFalse(next(r for r in profile['roots'] if r['ref']['driveId']=='offline')['available'])
        self.assertEqual(len(profile['capacity']['rootIds']),2)

    def test_direct_dto_listing_paging_search_and_stale_folder_refresh(self):
        query=self.query(pageSize=1)
        self.f.put('a',b'a'); self.f.put('b',b'b')
        first=self.call('metadata.listFolder',query)
        self.assertEqual(first['entries'][0]['name'],'a'); self.assertEqual(first['totalMatched'],'2')
        self.assertNotEqual(first['folder']['revision'],query['folder']['revision'])
        query['cursor']=first['nextCursor']; second=self.call('metadata.listFolder',query)
        self.assertEqual(second['entries'][0]['name'],'b'); self.assertIsNone(second['nextCursor'])
        self.assertIsInstance(second['entries'][0]['ref']['generation'],str)
        self.assertRegex(second['entries'][0]['modifiedAt'],r'\.\d{3}Z$')
        search=self.call('metadata.listFolder',self.query(search='B'))
        self.assertEqual(search['totalMatched'],'1')
    def test_cursor_expiry_and_partition_or_generation_denial(self):
        self.f.put('a'); self.f.put('b'); query=self.query(pageSize=1)
        first=self.call('metadata.listFolder',query); query['cursor']=first['nextCursor']
        self.f.state.connection.execute('UPDATE listing_snapshots SET expires=0')
        with self.assertRaises(BackendError) as err: self.call('metadata.listFolder',query)
        self.assertEqual(err.exception.code,'CURSOR_EXPIRED')
        bad=self.query(); bad['partition']['accountId']='another'
        with self.assertRaises(BackendError): self.call('metadata.listFolder',bad)
        bad=self.query(); bad['folder']['generation']='0'
        with self.assertRaises(BackendError) as err: self.call('metadata.listFolder',bad)
        self.assertEqual(err.exception.code,'VOLUME_CHANGED')
    def test_no_admin_capability_forwarding(self):
        with self.assertRaises(BackendError): self.call('agents.register',{})
        with self.assertRaises(BackendError): self.call('metadata.home',{'unexpected':True})
    def test_home_uses_real_roots_and_no_fabricated_inbox(self):
        home=self.call('metadata.home',{})
        self.assertFalse(home['demo']); self.assertEqual(home['inbox'],[]); self.assertIsNone(home['inboxRef'])
        self.assertIsNone(home['connection']['profile']); self.assertEqual(len(home['connection']['roots']),2)
    def test_mkdir_reconcile_idempotent_after_committed_parent_revision_changes(self):
        intent=self.intent('mkdir',destination=self.root()['ref'],name='new')
        first=self.call('operations.execute',intent); self.assertEqual(first['status'],'accepted')
        self.finish(); again=self.call('operations.execute',intent)
        self.assertEqual(first['jobId'],again['jobId']); self.assertEqual(again['status'],'completed')
        self.assertEqual(again['outcomes'][0]['outcome'],'succeeded')
        changed=dict(intent,name='different')
        with self.assertRaises(BackendError) as err: self.call('operations.execute',changed)
        self.assertEqual(err.exception.code,'REQUEST_MISMATCH')
        self.assertEqual(len(self.call('operations.listReceipts',{})),1)
    def test_batch_conflict_and_linked_resolution_cannot_repeat_success(self):
        a=self.ref(self.f.put('a')); b=self.ref(self.f.put('b')); self.f.put('b',drive='backups')
        intent=self.intent('copy',[a,b],destination=self.root('backups')['ref'])
        self.call('operations.execute',intent); self.finish()
        result=self.call('operations.reconcile',{'requestId':intent['requestId']})
        self.assertEqual(result['status'],'needs_decision'); self.assertEqual({r['outcome'] for r in result['outcomes']},{'succeeded','needs_decision'})
        bad=self.intent('copy',[a],destination=self.root('backups')['ref'],collisionPolicy='keep_both',linkedRequestId=intent['requestId'])
        with self.assertRaises(BackendError): self.call('operations.execute',bad)
        good=self.intent('copy',[b],destination=self.root('backups')['ref'],collisionPolicy='keep_both',linkedRequestId=intent['requestId'])
        self.call('operations.execute',good); self.finish()
        result=self.call('operations.reconcile',{'requestId':good['requestId']})
        self.assertEqual(result['status'],'completed'); self.assertTrue((self.f.root/'backups/b (1)').exists())
    def test_skipped_source_cannot_be_replayed_by_linked_decision(self):
        a=self.ref(self.f.put('a')); self.f.put('a',drive='backups')
        intent=self.intent('copy',[a],destination=self.root('backups')['ref'],collisionPolicy='skip')
        self.call('operations.execute',intent); self.finish()
        result=self.call('operations.reconcile',{'requestId':intent['requestId']}); self.assertEqual(result['outcomes'][0]['outcome'],'skipped')
        with self.assertRaises(BackendError): self.call('operations.execute',self.intent('copy',[a],destination=self.root('backups')['ref'],linkedRequestId=intent['requestId']))
    def test_trash_restore_uses_existing_safe_jobs(self):
        previously_observed_root=self.root()['ref']
        source=self.ref(self.f.put('a'))
        self.call('operations.execute',self.intent('trash',[source])); self.finish()
        trash=self.call('metadata.listTrash',previously_observed_root); self.assertEqual(len(trash),1)
        with self.assertRaises(BackendError): self.call('metadata.listTrash',dict(previously_observed_root,id='replaced-root'))
        with self.assertRaises(BackendError): self.call('metadata.listTrash',dict(previously_observed_root,generation='0'))
        self.call('operations.execute',self.intent('restore',[trash[0]['ref']])); self.finish()
        self.assertTrue((self.f.root/'files/a').exists())
    def test_stale_trash_does_not_hide_fresh_restore_round_trip(self):
        old=self.ref(self.f.put('old-trash'))
        self.call('operations.execute',self.intent('trash',[old]));self.finish()
        self.f.service.filesystems['files'].volume._generation+=1
        fresh=self.ref(self.f.put('fresh-trash'))
        self.call('operations.execute',self.intent('trash',[fresh]));self.finish()
        rows=self.call('metadata.listTrash',self.root()['ref'])
        stale=next(r for r in rows if r['name']=='old-trash')
        current=next(r for r in rows if r['name']=='fresh-trash')
        self.assertEqual(stale['permissions'],[])
        self.assertIn('restore',current['permissions'])
        result=self.call('operations.execute',self.intent('restore',[current['ref']],destination=self.root()['ref']))
        self.finish()
        receipt=self.call('operations.reconcile',{'requestId':result['requestId']})
        self.assertEqual(receipt['status'],'completed')
        self.assertTrue((self.f.root/'files/fresh-trash').exists())
        self.assertEqual([r['name'] for r in self.call('metadata.listTrash',self.root()['ref'])],['old-trash'])
        forged=dict(stale['ref'],generation=self.root()['ref']['generation'])
        with self.assertRaises(BackendError):self.metadata._validate_trash_source(self.context,forged)

    def test_stale_source_per_item_and_scope_revocation(self):
        old=self.ref(self.f.put('a')); (self.f.root/'files/a').write_bytes(b'changed')
        result=self.call('operations.execute',self.intent('trash',[old]))
        self.assertEqual(result['status'],'failed'); self.assertEqual(result['outcomes'][0]['error']['code'],'source_changed')
        self.boundary.auth.assign(self.f.owner,['read'],[{'driveId':'backups','path':''}])
        self.assertEqual(self.call('operations.listReceipts',{}),[])
    def test_aggregate_failure_rolls_back_all_child_admissions(self):
        a=self.ref(self.f.put('a')); b=self.ref(self.f.put('b'))
        original=self.f.service.operations.submit; calls=[]
        def failing(principal,request):
            calls.append(request)
            if len(calls)==2: raise BackendError('STATE_UNAVAILABLE')
            return original(principal,request)
        self.f.service.operations.submit=failing
        intent=self.intent('trash',[a,b])
        with self.assertRaises(BackendError): self.call('operations.execute',intent)
        self.assertEqual(self.f.state.list('mobile_operations'),[])
        self.assertEqual(self.f.state.list('jobs'),[])
    def test_cross_device_receipts_are_not_visible(self):
        intent=self.intent('mkdir',destination=self.root()['ref'],name='new'); self.call('operations.execute',intent)
        response=self.boundary.auth.login({'username':'test','password':'synthetic metadata password','deviceLabel':'another','requestId':str(uuid.uuid4())})
        other=self.boundary.auth.authenticate(response['accessToken'])
        with self.assertRaises(BackendError): self.boundary.call(other,{'method':'operations.reconcile','params':{'requestId':intent['requestId']}})

    def test_replace_target_changed_after_admission_is_preserved(self):
        source=self.ref(self.f.put('a',b'source')); self.f.put('a',b'old',drive='backups')
        intent=self.intent('copy',[source],destination=self.root('backups')['ref'],collisionPolicy='replace')
        self.call('operations.execute',intent)
        (self.f.root/'backups/a').write_bytes(b'new external content')
        with self.assertRaises(BackendError): self.finish()
        self.assertEqual((self.f.root/'backups/a').read_bytes(),b'new external content')
        result=self.call('operations.reconcile',{'requestId':intent['requestId']})
        self.assertEqual(result['outcomes'][0]['error']['code'],'source_changed')

    def test_replace_preserves_old_target_in_trash(self):
        source=self.ref(self.f.put('a',b'source')); self.f.put('a',b'old',drive='backups')
        intent=self.intent('copy',[source],destination=self.root('backups')['ref'],collisionPolicy='replace')
        self.call('operations.execute',intent); self.finish()
        self.assertEqual((self.f.root/'backups/a').read_bytes(),b'source')
        self.assertEqual(len(self.f.service.operations.trash_list(self.context.principal,'backups')),1)

    def test_batch_same_destination_survives_own_directory_changes(self):
        sources=[self.ref(self.f.put(x,x.encode())) for x in ('a','b')]
        for x in ('a','b'): self.f.put(x,b'old',drive='backups')
        intent=self.intent('copy',sources,destination=self.root('backups')['ref'],collisionPolicy='replace')
        self.call('operations.execute',intent); self.finish()
        result=self.call('operations.reconcile',{'requestId':intent['requestId']})
        self.assertEqual(result['status'],'completed'); self.assertEqual(len(result['outcomes']),2)
        self.assertEqual((self.f.root/'backups/b').read_bytes(),b'b')

    def test_worker_time_collision_is_an_actionable_decision(self):
        source=self.ref(self.f.put('a'))
        intent=self.intent('copy',[source],destination=self.root('backups')['ref'])
        self.call('operations.execute',intent); self.f.put('a',b'later collision',drive='backups')
        with self.assertRaises(BackendError): self.finish()
        result=self.call('operations.reconcile',{'requestId':intent['requestId']})
        self.assertEqual(result['status'],'needs_decision'); self.assertEqual(result['decisionDraft'],intent)

    def test_explicit_job_needs_decision_is_not_pending_forever(self):
        intent=self.intent('mkdir',destination=self.root()['ref'],name='new')
        self.call('operations.execute',intent)
        record=self.f.state.list('mobile_operations')[0]; job=self.f.state.get('jobs',record['items'][0]['jobId'])
        job['state']='needs-decision'; self.f.state.put('jobs',job['id'],job)
        result=self.call('operations.reconcile',{'requestId':intent['requestId']})
        self.assertEqual(result['status'],'needs_decision'); self.assertEqual(result['outcomes'][0]['outcome'],'needs_decision')

    def test_unknown_receipt_differs_from_corrupt_admitted_children(self):
        with self.assertRaises(BackendError) as error: self.call('operations.reconcile',{'requestId':str(uuid.uuid4())})
        self.assertEqual(error.exception.code,'REQUEST_UNKNOWN')
        intent=self.intent('mkdir',destination=self.root()['ref'],name='new'); self.call('operations.execute',intent)
        record=self.f.state.list('mobile_operations')[0]; self.f.state.delete('jobs',record['items'][0]['jobId'])
        with self.assertRaises(BackendError) as error: self.call('operations.reconcile',{'requestId':intent['requestId']})
        self.assertEqual(error.exception.code,'STATE_UNAVAILABLE')


if __name__=='__main__': unittest.main()
