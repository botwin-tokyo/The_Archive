import unittest
from support import Fixture
from archive_backend.contracts import BackendError

class OperationTests(unittest.TestCase):
    def setUp(self): self.f=Fixture(); self.addCleanup(self.f.close)
    def submit(self,request):
        receipt=self.f.call('operations.submit',{'request':request}); self.f.service.run_one(); return self.f.service.jobs.get(self.f.owner,receipt['jobIds'][0])
    def test_create_rename_trash_restore(self):
        self.submit({'requestId':'folder','kind':'new-folder','name':'hello','destination':{'driveId':'files','path':''}})
        source=self.f.put('hello/a')
        job=self.submit({'requestId':'rename','kind':'rename','targets':[self.f.ref(source)],'name':'b'})
        self.assertEqual(job['state'],'completed'); self.assertTrue((self.f.root/'files/hello/b').exists())
        source=self.f.call('files.list',{'query':{'requestId':'l','location':{'driveId':'files','path':'hello'}}})['entries'][0]
        self.submit({'requestId':'trash','kind':'trash','targets':[self.f.ref(source)]})
        item=self.f.call('trash.list',{'driveId':'files'})[0]
        self.submit({'requestId':'restore','kind':'restore','targets':[self.f.ref(item['entry'])]})
        self.assertTrue((self.f.root/'files/hello/b').exists())
    def test_copy_and_crossdrive_move(self):
        source=self.f.put(data=b'opaque'*200000)
        job=self.submit({'requestId':'copy','kind':'copy','targets':[self.f.ref(source)],'destination':{'driveId':'backups','path':''}})
        self.assertEqual(job['state'],'completed'); self.assertEqual((self.f.root/'files/a.bin').read_bytes(),(self.f.root/'backups/a.bin').read_bytes())
        job=self.submit({'requestId':'move','kind':'move','targets':[self.f.ref(source)],'destination':{'driveId':'backups','path':''},'conflictPolicy':'keep-both'})
        self.assertEqual(job['state'],'completed'); self.assertFalse((self.f.root/'files/a.bin').exists()); self.assertEqual(len(self.f.call('trash.list',{'driveId':'files'})),1)
    def test_no_silent_overwrite_and_idempotency(self):
        source=self.f.put(); self.f.put(drive='backups')
        request={'requestId':'copy','kind':'copy','targets':[self.f.ref(source)],'destination':{'driveId':'backups','path':''}}
        receipt=self.f.call('operations.submit',{'request':request})
        self.assertEqual(receipt['status'],'needs-decision')
        self.assertTrue((self.f.root/'files/a.bin').exists())
        request['conflictPolicy']='replace'
        accepted=self.f.call('operations.submit',{'request':request})
        self.assertEqual(accepted,self.f.call('operations.submit',{'request':request}))
        request['conflictPolicy']='skip'
        with self.assertRaises(BackendError): self.f.call('operations.submit',{'request':request})
    def test_purge_requires_bound_confirmation(self):
        source=self.f.put(); self.submit({'requestId':'trash','kind':'trash','targets':[self.f.ref(source)]})
        ref=self.f.ref(self.f.call('trash.list',{'driveId':'files'})[0]['entry'])
        request={'requestId':'purge','kind':'purge','targets':[ref]}
        with self.assertRaises(BackendError): self.f.call('operations.submit',{'request':request})
        grant=self.f.call('trash.confirm',{'driveId':'files','targets':[ref]})
        request['confirmation']={'grantId':grant['id']}
        self.submit(request); self.assertEqual(self.f.call('trash.list',{'driveId':'files'}),[])

    def test_exact_retry_after_commit_precedes_new_collision_preflight(self):
        request={'requestId':'durable-mkdir','kind':'new-folder','name':'committed','destination':{'driveId':'files','path':''}}
        receipt=self.f.call('operations.submit',{'request':request}); self.f.service.run_one()
        self.assertEqual(self.f.call('operations.submit',{'request':request}),receipt)
        changed=dict(request,name='different')
        with self.assertRaises(BackendError) as error: self.f.call('operations.submit',{'request':changed})
        self.assertEqual(error.exception.code,'CONFLICT')

    def test_large_folder_move_bounds_queue_rows_not_verified_files(self):
        import json
        folder=self.f.root/'files/large';folder.mkdir()
        for i in range(1003): (folder/f'{i:04}.bin').write_bytes(f'file-{i}'.encode())
        source=next(e for e in self.f.call('files.list',{'query':{'requestId':'large','location':{'driveId':'files','path':''}}})['entries'] if e['name']=='large')
        job=self.submit({'requestId':'large-move','kind':'move','targets':[self.f.ref(source)],'destination':{'driveId':'backups','path':''}})
        self.assertEqual(job['state'],'completed',job.get('error'))
        self.assertEqual(len(job['childIds']),1000)
        self.assertFalse(folder.exists())
        total=0
        for i in range(1003):
            expected=f'file-{i}'.encode();total+=len(expected)
            self.assertEqual((self.f.root/'backups/large'/f'{i:04}.bin').read_bytes(),expected)
        self.assertEqual(int(job['bytesDone']),total)
        rows=self.f.state.select("SELECT body FROM records WHERE namespace='children' AND key LIKE ?",(job['id']+':%',))
        files=[json.loads(r['body']) for r in rows if json.loads(r['body']).get('kind')!='directory']
        self.assertEqual(len(files),1003)
        self.assertTrue(all(r['status']=='verified' and r['sha256'] for r in files))

    def test_copy_progress_rehydrates_verified_checkpoints_on_retry(self):
        folder=self.f.root/'files/retry';folder.mkdir()
        for i in range(3): (folder/str(i)).write_bytes(b'checkpoint'*17)
        source=next(e for e in self.f.call('files.list',{'query':{'requestId':'retry-list','location':{'driveId':'files','path':''}}})['entries'] if e['name']=='retry')
        request={'requestId':'retry-copy','kind':'copy','targets':[self.f.ref(source)],'destination':{'driveId':'backups','path':''}}
        receipt=self.f.call('operations.submit',{'request':request});jid=receipt['jobIds'][0]
        def fault(point):
            if point=='after-copy-verify': raise RuntimeError('stopped before publish')
        self.f.service.operations.fault=fault
        with self.assertRaises(RuntimeError): self.f.service.run_one()
        record=self.f.state.get('jobs',jid);record['_lease']['expires']=0;record['_workerStopped']=True
        self.f.state.put('jobs',jid,record);self.f.service.jobs.recover([jid])
        self.f.service.retry(self.f.owner,jid)
        self.f.service.operations.fault=lambda _:None
        self.f.service.run_one()
        job=self.f.service.jobs.get(self.f.owner,jid)
        self.assertEqual(job['state'],'completed',job.get('error'))
        self.assertEqual(int(job['bytesDone']),510)
        self.assertEqual(len(list((self.f.root/'backups/retry').iterdir())),3)

    def test_tree_verification_pages_release_writer_lock_and_keep_checkpointing(self):
        from types import SimpleNamespace
        for i in range(260):
            self.f.state.put('children',f'parent:0:{i:04}',{'path':str(i),'revision':7,'bytes':'1'})
        checked=[];pulses=[]
        def pulse():
            self.assertEqual(getattr(self.f.state._local,'depth',0),0)
            pulses.append(True)
        def stat(path):
            self.assertEqual(getattr(self.f.state._local,'depth',0),0)
            checked.append(path);return SimpleNamespace(revision=7)
        self.f.service.operations._verify_children(SimpleNamespace(stat=stat),'parent:0',pulse)
        self.assertEqual(checked,[str(i) for i in range(260)])
        self.assertEqual(len(pulses),4)
