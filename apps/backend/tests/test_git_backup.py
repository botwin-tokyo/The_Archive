import hashlib,io,json,os,tarfile,tempfile,unittest,uuid
from pathlib import Path
from unittest.mock import patch
from support import Fixture
from archive_backend.git_publish import Publisher
from archive_backend.contracts import BackendError
from archive_backend.git_config import DEFAULTS

class BackupIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.umask=os.umask(0o077);self.f=Fixture(drive_ids=('files','git'));self.addCleanup(self.f.close);self.addCleanup(os.umask,self.umask)
        self.spool=self.f.root/'spool';self.spool.mkdir()
        self.config={'volumeId':'git','spool':str(self.spool),'workerUid':os.geteuid()}
        self.publisher=Publisher(self.f.service,self.config)
    def artifact(self,content=b'hello',count=1):
        key=uuid.uuid4().hex;folder=self.spool/key;folder.mkdir();dest=folder/'files.tar'
        with tarfile.open(dest,'w') as archive:
            for i in range(count):
                info=tarfile.TarInfo('nested/file'+str(i));info.size=len(content);archive.addfile(info,io.BytesIO(content))
        return {'id':'123','owner':'owner','name':'repo','artifact':key,'commit':'a'*40,'sha256':hashlib.sha256(dest.read_bytes()).hexdigest(),'files':count,'bytes':len(content)*count}
    def test_large_publication_and_atomic_update(self):
        first=self.artifact(count=1001);result=self.publisher.publish(first)
        current=self.f.root/'git'/result['path']
        self.assertEqual(len(list((current/'nested').iterdir())),1001)
        second=self.artifact(b'updated');self.publisher.publish(second)
        self.assertEqual((current/'nested/file0').read_bytes(),b'updated')
        self.assertEqual(len(list((current/'nested').iterdir())),1)
    def test_bad_checksum_retains_previous(self):
        first=self.artifact();result=self.publisher.publish(first)
        second=self.artifact(b'bad');second['sha256']='0'*64
        with self.assertRaises(BackendError):self.publisher.publish(second)
        self.assertEqual((self.f.root/'git'/result['path']/'nested/file0').read_bytes(),b'hello')
    def test_reconcile_after_exchange_before_receipt(self):
        first=self.artifact();self.publisher.publish(first);second=self.artifact(b'new')
        original=self.f.state.put
        def failing(namespace,*args,**kwargs):
            if namespace=='git_publications':raise RuntimeError('injected power interruption')
            return original(namespace,*args,**kwargs)
        with patch.object(self.f.state,'put',side_effect=failing):
            with self.assertRaises(RuntimeError):self.publisher.publish(second)
        result=self.publisher.publish(second)
        self.assertEqual(result['artifact'],second['artifact'])
    def test_generated_path_guard_and_other_folders(self):
        self.f.state.put('git_protection','active',{'enabled':True})
        for action in ('write','trash','purge'):
            with self.assertRaises(BackendError):self.f.service.auth.authorize(self.f.owner,action,'git','GitHub/owner/repo')
        self.f.service.auth.authorize(self.f.owner,'read','git','GitHub/owner/repo')
        self.f.service.auth.authorize(self.f.owner,'write','git','MyProjects/new')
        receipt=self.f.call('operations.submit',{'request':{'requestId':'outside-generated','kind':'new-folder','destination':{'driveId':'git','path':''},'name':'MyProjects','conflictPolicy':'ask'}})
        self.assertEqual(receipt['status'],'accepted')
    def test_owner_configuration(self):
        result=self.f.call('gitBackup.configure',{'settings':dict(DEFAULTS,scope='controlled')})
        self.assertEqual(result['settings']['scope'],'controlled')
        self.assertFalse(result['configured'])
    def test_agent_cannot_control_or_inspect_backup_configuration(self):
        token='read-agent-'+('z'*40)
        self.f.call('agents.register',{'label':'Reader','secret':token,'scopes':['read'],'allowedRoots':[{'driveId':'git','path':''}]})
        with self.assertRaises(BackendError):self.f.call('gitBackup.status',token=token)
    def test_missing_mount_does_not_accept_run(self):
        coordinator=self.f.service.git_backup;coordinator.config=self.config
        self.f.registry.get('git').close()
        with self.assertRaises(BackendError):coordinator.trigger('missing-disk')
        self.assertIsNone(self.f.state.get('git_run','active'))
    def test_run_idempotency_and_singleton(self):
        coordinator=self.f.service.git_backup;coordinator.config=self.config
        first=coordinator.trigger('first')
        self.assertEqual(coordinator.trigger('first')['id'],first['id'])
        self.assertEqual(coordinator.trigger('second')['id'],first['id'])
