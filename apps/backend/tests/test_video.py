import copy,hashlib,os,time,unittest
from dataclasses import replace
from archive_backend.contracts import BackendError
from archive_backend.video_service import VideoService
from archive_backend.video_contracts import canonical_url
from archive_backend.mcp_adapter import tool_list,dispatch
from support import Fixture

URL='https://www.youtube.com/watch?v=BaW_jenozKc'
PLAN={'title':'A / safe video','uploader':'Fixture','duration':10.,'source':'youtube:BaW_jenozKc','choices':[{'id':'video-720','label':'720p · MP4','kind':'video','height':720,'estimatedBytes':None}]}
class Worker:
    def __init__(self,root):self.root=root;self.tasks={};self.starts=0;self.running=False
    def call(self,**args):
        op=args['op'];id=args.get('taskId')
        if op=='status':return {'available':True}
        if op=='start':
            self.starts+=1
            if args['action']=='resolve':result=copy.deepcopy(PLAN)
            else:
                d=self.root/id;d.mkdir(exist_ok=True);p=d/'media.mp4';p.write_bytes(b'fixture video');p.chmod(0o640)
                result={'filename':'media.mp4','sizeBytes':'13','sha256':hashlib.sha256(b'fixture video').hexdigest()}
            self.tasks[id]={'state':'running'} if self.running else {'state':'completed','result':result}
        if op=='cancel':self.tasks[id]={'state':'canceled'}
        if op=='cleanup':self.tasks.pop(id,None)
        return self.tasks.get(id,{'state':'absent'})
class VideoTests(unittest.TestCase):
    def setUp(self):
        self.f=Fixture();self.addCleanup(self.f.close);self.spool=self.f.root/'spool';self.spool.mkdir()
        self.f.service.config=replace(self.f.service.config,volumes=({'id':'files','role':'files'},))
        self.worker=Worker(self.spool);self.config={'socket':'/unused','spool':str(self.spool),'workerUid':os.getuid()}
        self.video=VideoService(self.f.service,self.config,self.worker);self.f.service.video=self.video
    def plan(self):
        p=self.f.call('video.resolve',{'requestId':'resolve','url':URL});self.video.tick();return self.f.call('video.plan.get',{'id':p['id']})
    def enqueue(self,key='enqueue'):
        p=self.plan();return self.f.call('video.enqueue',{'requestId':key,'planId':p['id'],'planRevision':p['revision'],'choiceId':'video-720','destination':{'driveId':'files','path':''}})
    def settle(self):
        for _ in range(6):
            self.video.tick()
            if self.video.import_thread:self.video.import_thread.join(5)
    def test_verified_publication_and_dedup(self):
        j=self.enqueue();self.settle();done=self.f.call('video.jobs.get',{'id':j['id']});self.assertEqual(done['state'],'completed',done)
        self.assertEqual((self.f.root/'files'/done['entry']['path']).read_bytes(),b'fixture video')
        self.assertNotIn('attempt',done)
        count=self.worker.starts;j2=self.enqueue('second');self.settle();self.assertEqual(self.worker.starts,count);self.assertEqual(self.f.call('video.jobs.get',{'id':j2['id']})['state'],'completed')
    def test_idempotent_enqueue_after_expiry(self):
        j=self.enqueue();p=self.f.state.list('video_plans')[0];args={'requestId':'enqueue','planId':p['id'],'planRevision':p['revision'],'choiceId':'video-720','destination':{'driveId':'files','path':''}}
        p['expiresAt']=0;self.f.state.put('video_plans',p['id'],p)
        self.assertEqual(self.f.call('video.enqueue',args)['id'],j['id']);args['choiceId']='audio'
        with self.assertRaises(BackendError):self.f.call('video.enqueue',args)
    def test_owner_only_mcp_and_nested_schema(self):
        token='agent-'+40*'y';self.f.service.auth.register(self.f.owner,'agent',token,['read','write'],[('files','')])
        for cap in ['video.status','video.jobs.list']:
            with self.assertRaises(BackendError):self.f.call(cap,token=token)
        schema=next(t for t in tool_list() if t['name']=='video.enqueue')['inputSchema'];self.assertEqual(schema['properties']['destination']['required'],['driveId','path'])
        result=dispatch(self.f.service,self.f.token,{'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'video.status','arguments':{}}});self.assertFalse(result['result']['isError'])
    def test_controls_and_stale_revision(self):
        j=self.enqueue();args={'id':j['id'],'revision':j['revision'],'action':'pause'};paused=self.f.call('video.jobs.control',args);self.assertEqual(paused['state'],'paused')
        with self.assertRaises(BackendError):self.f.call('video.jobs.control',args)
        resumed=self.f.call('video.jobs.control',dict(args,revision=paused['revision'],action='resume'));self.assertEqual(resumed['state'],'queued')
        canceled=self.f.call('video.jobs.control',dict(args,revision=resumed['revision'],action='cancel'));self.assertEqual(canceled['state'],'canceled');self.settle();self.assertFalse(list((self.f.root/'files').glob('*.mp4')))
    def test_restart_after_artifact_ready(self):
        j=self.enqueue();self.video.tick();self.assertEqual(self.f.call('video.jobs.get',{'id':j['id']})['state'],'validating')
        self.video=VideoService(self.f.service,self.config,self.worker);self.f.service.video=self.video;self.settle();self.assertEqual(self.f.call('video.jobs.get',{'id':j['id']})['state'],'completed')
    def test_lost_dispatch_response_reuses_task(self):
        j=self.enqueue();original=self.worker.call;failed=[False]
        def call(**args):
            result=original(**args)
            if args['op']=='start' and args['action']=='download' and not failed[0]:failed[0]=True;raise BackendError('UNAVAILABLE','Lost response')
            return result
        self.worker.call=call;self.settle();self.assertEqual(self.f.call('video.jobs.get',{'id':j['id']})['state'],'completed');self.assertEqual(self.worker.starts,2)
    def test_unknown_collision_preserved(self):
        j=self.enqueue();row=self.f.state.get('video_jobs',j['id']);self.f.put(row['_path'],b'existing');self.settle()
        self.assertEqual((self.f.root/'files'/row['_path']).read_bytes(),b'existing');self.assertEqual(self.f.call('video.jobs.get',{'id':j['id']})['state'],'failed')
    def test_corrupt_artifact_fails(self):
        j=self.enqueue();self.video.tick();row=self.f.state.get('video_jobs',j['id']);(self.spool/row['_task']/'media.mp4').write_bytes(b'bad');self.settle();self.assertEqual(self.f.call('video.jobs.get',{'id':j['id']})['state'],'failed')
    def test_retry_corrupt_artifact_reacquires(self):
        j=self.enqueue();self.video.tick();row=self.f.state.get('video_jobs',j['id']);(self.spool/row['_task']/'media.mp4').write_bytes(b'bad');self.settle();failed=self.f.call('video.jobs.get',{'id':j['id']})
        self.f.call('video.jobs.control',{'id':j['id'],'revision':failed['revision'],'action':'retry-failed'});self.settle();self.assertEqual(self.f.call('video.jobs.get',{'id':j['id']})['state'],'completed')
    def test_unknown_target_denied(self):
        p=self.plan()
        with self.assertRaises(BackendError):self.f.call('video.enqueue',{'requestId':'bad','planId':p['id'],'planRevision':p['revision'],'choiceId':'video-720','destination':{'driveId':'unknown','path':''}})
    def test_publication_receipt_recovers_after_record_failure(self):
        j=self.enqueue();original=self.video.imports.record;called=[False]
        def fail(*args):
            if not called[0]:called[0]=True;raise BackendError('BUSY','Simulated interrupted final record')
            return original(*args)
        self.video.imports.record=fail;self.settle()
        done=self.f.call('video.jobs.get',{'id':j['id']});self.assertEqual(done['state'],'completed',done)
        self.assertEqual(len(list((self.f.root/'files').glob('*.mp4'))),1)
    def test_symlink_artifact_rejected(self):
        j=self.enqueue();self.video.tick();row=self.f.state.get('video_jobs',j['id']);artifact=self.spool/row['_task']/'media.mp4';other=self.spool/'other';other.write_bytes(b'fixture video');other.chmod(0o640);artifact.unlink();artifact.symlink_to(other)
        self.settle();self.assertEqual(self.f.call('video.jobs.get',{'id':j['id']})['state'],'failed');self.assertFalse(list((self.f.root/'files').glob('*.mp4')))
    def test_url_admission(self):
        self.assertEqual(canonical_url('https://youtu.be/BaW_jenozKc?si=abc'),URL)
        self.assertEqual(canonical_url('https://twitter.com/user/status/123'),'https://x.com/i/status/123')
        for url in ['http://youtube.com/watch?v=BaW_jenozKc','https://youtube.com.evil/watch?v=BaW_jenozKc','https://127.0.0.1/a','https://u:p@youtube.com/watch?v=BaW_jenozKc',URL+'&list=123','https://youtube.com/playlist?list=123']:
            with self.subTest(url=url),self.assertRaises(ValueError):canonical_url(url)
