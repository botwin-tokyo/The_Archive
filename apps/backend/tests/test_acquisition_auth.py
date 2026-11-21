import os
import unittest
import uuid
from cryptography.fernet import Fernet
from archive_backend.contracts import BackendError
from archive_backend.mobile_boundary import MobileBoundary
from archive_backend.music_service import MusicService
from archive_backend.video_service import VideoService
from support import Fixture
from test_music import FakeWorker
from test_video import Worker, URL


class AcquisitionAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.f=Fixture(); self.addCleanup(self.f.close)
        self.boundary=MobileBoundary(self.f.service,{'publicOrigin':'https://archive.invalid'},replay_key=Fernet.generate_key())
        self.addCleanup(self.boundary.close)
        self.boundary.auth.enroll(self.f.owner,'test','test acquisition password',['read','write','acquire'],[{'driveId':'backups','path':''}])
        login=self.boundary.auth.login({'username':'test','password':'test acquisition password','deviceLabel':'test','requestId':str(uuid.uuid4())})
        self.token=login['accessToken']; self.context=self.boundary.auth.authenticate(self.token)
        self.actor=self.boundary.auth.acquisition_principal(self.context)
        spool=self.f.root/'spool';spool.mkdir()
        config={'socket':'/unused','spool':str(spool),'workerUid':os.getuid()}
        self.music=MusicService(self.f.service,config,FakeWorker(spool));self.f.service.music=self.music
        self.video=VideoService(self.f.service,config,Worker(spool));self.f.service.video=self.video

    def test_logout_does_not_cancel_account_work_but_scope_revocation_does(self):
        result=self.music.call(self.actor,'music.resolve',{'requestId':'resolve','selection':{'kind':'album','id':'c'*22}})
        self.boundary.auth.logout(self.token)
        self.music.tick()
        plan=self.music.call(self.actor,'music.plan.get',{'id':result['id']})
        self.assertEqual(plan['state'],'completed')
        self.boundary.auth.assign(self.f.owner,['read'],[{'driveId':'backups','path':''}])
        with self.assertRaises(BackendError):self.music.call(self.actor,'music.status',{})

    def test_root_scope_and_existing_backup_destination(self):
        result=self.music.call(self.actor,'music.resolve',{'requestId':'resolve','selection':{'kind':'album','id':'c'*22}})
        self.music.tick();plan=self.music.call(self.actor,'music.plan.get',{'id':result['id']})
        args={'requestId':'enqueue','planId':plan['id'],'planRevision':plan['revision'],'destination':{'driveId':'files','path':''}}
        with self.assertRaises(BackendError):self.music.call(self.actor,'music.enqueue',args)
        args['destination']['driveId']='backups'
        job=self.music.call(self.actor,'music.enqueue',args)
        self.assertEqual(job['id'],self.music.call(self.actor,'music.enqueue',args)['id'])
        for _ in range(20):
            self.music.tick()
            while self.f.service.run_one():pass
        self.assertEqual(self.music.call(self.actor,'music.jobs.get',{'id':job['id']})['state'],'completed')

    def test_owner_plans_and_history_are_not_exposed_to_mobile_actor(self):
        for service,prefix,args in [(self.music,'music',{'selection':{'kind':'album','id':'c'*22}}),(self.video,'video',{'url':URL})]:
            p=service.call(self.f.owner,prefix+'.resolve',{'requestId':'owner',**args})
            with self.assertRaises(BackendError):service.call(self.actor,prefix+'.plan.get',{'id':p['id']})
            self.assertEqual(service.call(self.actor,prefix+'.jobs.list',{})['items'],[])

    def test_video_scoped_publication_and_request_replay(self):
        p=self.video.call(self.actor,'video.resolve',{'requestId':'resolve','url':URL});self.video.tick()
        p=self.video.call(self.actor,'video.plan.get',{'id':p['id']})
        args={'requestId':'enqueue','planId':p['id'],'planRevision':p['revision'],'choiceId':'video-720','destination':{'driveId':'backups','path':''}}
        j=self.video.call(self.actor,'video.enqueue',args)
        self.assertEqual(j['id'],self.video.call(self.actor,'video.enqueue',args)['id'])
        for _ in range(6):
            self.video.tick()
            if self.video.import_thread:self.video.import_thread.join(5)
        self.assertEqual(self.video.call(self.actor,'video.jobs.get',{'id':j['id']})['state'],'completed')
        self.assertEqual(self.video.call(self.f.owner,'video.jobs.get',{'id':j['id']})['state'],'completed')

    def test_new_device_reuses_account_actor_and_replay_checks_narrowed_roots(self):
        p=self.video.call(self.actor,'video.resolve',{'requestId':'resolve','url':URL});self.video.tick()
        p=self.video.call(self.actor,'video.plan.get',{'id':p['id']})
        args={'requestId':'enqueue','planId':p['id'],'planRevision':p['revision'],'choiceId':'video-720','destination':{'driveId':'backups','path':''}}
        j=self.video.call(self.actor,'video.enqueue',args)
        login=self.boundary.auth.login({'username':'test','password':'test acquisition password','deviceLabel':'other','requestId':str(uuid.uuid4())})
        other=self.boundary.auth.acquisition_principal(self.boundary.auth.authenticate(login['accessToken']))
        self.assertEqual(self.actor.id,other.id)
        self.assertEqual(self.video.call(other,'video.enqueue',args)['id'],j['id'])
        self.boundary.auth.assign(self.f.owner,['read','write','acquire'],[{'driveId':'files','path':''}])
        with self.assertRaises(BackendError):self.video.call(other,'video.enqueue',args)
        self.assertEqual(self.video.call(other,'video.jobs.list',{})['items'],[])
