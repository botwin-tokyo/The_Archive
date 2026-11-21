import copy
import hashlib
import os
from pathlib import Path
import unittest
from archive_backend.contracts import BackendError
from archive_backend.music_service import MusicService
from archive_backend.music_contracts import label, target
from support import Fixture

TRACK={'kind':'track','id':'a'*22,'title':'A song','artist':'An artist','year':'2026','trackCount':1,'durationMs':1000,'disc':1,'number':1,'available':True}
ARTIST={'kind':'artist','title':'An artist · Discography','artist':'An artist','year':'','tracks':[],'expectedTracks':4,'releases':[{'id':identity*22,'title':'Album '+identity,'year':'2026','trackCount':2,'releaseType':'album'} for identity in ('c','e')]}
PLAN={'title':'An album','artist':'An artist','year':'2026','kind':'album','tracks':[TRACK,dict(TRACK,id='b'*22,number=2,disc=2)],'expectedTracks':2,'albumId':'c'*22,'albumTitle':'An album','albumArtist':'An artist','artistId':'d'*22}

class FakeWorker:
    def __init__(self,root): self.root=root;self.tasks={};self.starts=0;self.epoch='test-worker'
    def call(self,**args):
        op=args['op'];id=args.get('taskId')
        if op=='status': return {'available':True,'epoch':self.epoch}
        if op=='start':
            if id not in self.tasks:
                self.starts+=1
                if args['action']=='resolve':
                    chosen=args['payload']['selection']
                    result=copy.deepcopy(ARTIST if chosen['kind']=='artist' else PLAN)
                    if chosen['kind']!='artist': result['albumId']=chosen['id']
                elif args['action']=='lyrics': result={'status':'missing','plain':None,'synced':None,'sourceId':None,'embedded':False}
                elif args['action']=='search': result={'items':[TRACK],'nextOffset':None}
                else:
                    directory=self.root/id;directory.mkdir();file=directory/'audio.mp3';file.write_bytes(b'fixture audio');file.chmod(0o640)
                    result={'sha256':hashlib.sha256(b'fixture audio').hexdigest(),'sizeBytes':'13','durationMs':1000}
                self.tasks[id]={'state':'completed','epoch':self.epoch,'result':result}
            return self.tasks[id]
        if op=='poll': return self.tasks.get(id,{'state':'absent','epoch':self.epoch})
        if op=='cancel': self.tasks[id]={'state':'canceled','epoch':self.epoch};return self.tasks[id]
        if op=='cleanup': self.tasks.pop(id,None);return {'state':'absent','epoch':self.epoch}

class MusicTests(unittest.TestCase):
    def setUp(self):
        self.f=Fixture();self.addCleanup(self.f.close);self.spool=self.f.root/'spool';self.spool.mkdir()
        self.worker=FakeWorker(self.spool)
        self.music=MusicService(self.f.service,{'socket':'/unused','spool':str(self.spool),'workerUid':os.getuid()},self.worker)
        self.f.service.music=self.music
    def plan(self):
        receipt=self.f.call('music.resolve',{'requestId':'resolve','selection':{'kind':'album','id':'c'*22}})
        self.music.tick();return self.f.call('music.plan.get',{'id':receipt['id']})
    def enqueue(self):
        p=self.plan();return self.f.call('music.enqueue',{'requestId':'enqueue','planId':p['id'],'planRevision':p['revision'],'destination':{'driveId':'files','path':''}})
    def settle(self):
        for _ in range(30):
            self.music.tick()
            while lease:=self.f.service.jobs.claim('fixture-music'):
                self.f.service.operations.execute(lease)
    def test_full_album_import_and_verified_duplicate(self):
        job=self.enqueue();self.assertEqual(len(job['tracks']),2)
        self.settle();done=self.f.call('music.jobs.get',{'id':job['id']})
        self.assertEqual(done['state'],'completed',done)
        self.assertEqual([t['disc'] for t in done['tracks']],[1,2])
        for t in done['tracks']: self.assertEqual((self.f.root/'files'/t['entry']['path']).read_bytes(),b'fixture audio')
        p=self.f.call('music.plan.get',{'id':self.f.state.list('music_plans')[0]['id']})
        another=self.f.call('music.enqueue',{'requestId':'repeat','planId':p['id'],'planRevision':p['revision'],'destination':{'driveId':'files','path':''}})
        self.settle();self.assertTrue(all(t['state']=='already-present' for t in self.f.call('music.jobs.get',{'id':another['id']})['tracks']))
    def test_idempotency_and_conflicting_reuse(self):
        job=self.enqueue();p=self.f.state.list('music_plans')[0]
        args={'requestId':'enqueue','planId':p['id'],'planRevision':p['revision'],'destination':{'driveId':'files','path':''}}
        self.assertEqual(self.f.call('music.enqueue',args)['id'],job['id'])
        args['destination']['path']='different'
        with self.assertRaises(BackendError) as error:self.f.call('music.enqueue',args)
        self.assertEqual(error.exception.code,'CONFLICT')
    def test_owner_only_even_for_existing_write_agent(self):
        secret='test-agent-'+'y'*40
        self.f.service.auth.register(self.f.owner,'agent',secret,['read','write'],[('files','')])
        with self.assertRaises(BackendError):self.f.call('music.status',token=secret)
    def test_unknown_destination_and_host_paths_rejected(self):
        p=self.plan()
        for location in ({'driveId':'unknown','path':''},{'driveId':'files','path':'../../etc'}):
            with self.assertRaises(BackendError):self.f.call('music.enqueue',{'requestId':'unsafe'+location['driveId'],'planId':p['id'],'planRevision':1,'destination':location})
    def test_pause_revision_cancel_preserves_completed(self):
        j=self.enqueue();paused=self.f.call('music.jobs.control',{'id':j['id'],'revision':j['revision'],'action':'pause'})
        self.music.tick();self.assertEqual(self.f.call('music.jobs.get',{'id':j['id']})['state'],'paused')
        with self.assertRaises(BackendError):self.f.call('music.jobs.control',{'id':j['id'],'revision':j['revision'],'action':'resume'})
        self.f.call('music.jobs.control',{'id':j['id'],'revision':paused['revision'],'action':'resume'});self.settle()
        self.assertEqual(self.f.call('music.jobs.get',{'id':j['id']})['state'],'completed')
    def test_incomplete_album_cannot_be_queued(self):
        original=PLAN['expectedTracks'];PLAN['expectedTracks']=3
        try:
            p=self.plan();self.assertEqual(p['state'],'failed')
        finally:PLAN['expectedTracks']=original
    def test_private_fields_never_leave_boundary(self):
        j=self.enqueue()
        self.assertNotIn('_actor',str(j));self.assertNotIn('_path',str(j));self.assertNotIn('_task',str(j))
    def test_changed_library_file_is_not_silently_skipped(self):
        j=self.enqueue();self.settle();p=self.f.state.list('music_plans')[0]
        first=self.f.call('music.jobs.get',{'id':j['id']})['tracks'][0]['entry'];(self.f.root/'files'/first['path']).write_bytes(b'changed')
        next_job=self.f.call('music.enqueue',{'requestId':'changed','planId':p['id'],'planRevision':1,'destination':{'driveId':'files','path':''}})
        self.settle();t=self.f.call('music.jobs.get',{'id':next_job['id']})['tracks'][0];self.assertEqual(t['state'],'conflict');self.assertEqual((self.f.root/'files'/first['path']).read_bytes(),b'changed')
    def test_names_are_bounded_and_confined(self):
        self.assertNotIn('/',label('../a/b\\c'))
        self.assertLessEqual(len(label('é'*500).encode()),120)
    def test_coordinator_restart_reuses_committed_import_receipt(self):
        j=self.enqueue()
        for _ in range(10):
            self.music.tick()
            while lease:=self.f.service.jobs.claim('fixture-music'):self.f.service.operations.execute(lease)
            row=self.f.state.get('music_jobs',j['id'])
            if row['tracks'][0]['state']=='completed':break
        row=self.f.state.get('music_jobs',j['id']);t=row['tracks'][0]
        self.assertEqual(t['state'],'completed')
        t['state']='importing';t['entry']=None;row['state']='running';self.f.state.put('music_jobs',j['id'],row)
        self.music=MusicService(self.f.service,self.music.config,self.worker);self.f.service.music=self.music
        self.settle();done=self.f.call('music.jobs.get',{'id':j['id']});self.assertEqual(done['state'],'completed')
        self.assertEqual(len(list((self.f.root/'files').rglob('*.mp3'))),2)
    def test_pause_resume_unavailable_album_track_finishes_partial(self):
        original=PLAN['tracks'][1]['available'];PLAN['tracks'][1]['available']=False
        try:j=self.enqueue()
        finally:PLAN['tracks'][1]['available']=original
        j=self.f.call('music.jobs.control',{'id':j['id'],'revision':j['revision'],'action':'pause'})
        self.f.call('music.jobs.control',{'id':j['id'],'revision':j['revision'],'action':'resume'});self.settle()
        done=self.f.call('music.jobs.get',{'id':j['id']});self.assertEqual(done['state'],'partial');self.assertEqual(done['tracks'][1]['state'],'failed')
    def test_worker_restart_with_persisted_unsent_task_does_not_stall(self):
        j=self.enqueue();row=self.f.state.get('music_jobs',j['id']);row['tracks'][0]['_task']='f'*32;self.f.state.put('music_jobs',j['id'],row)
        self.settle();self.assertEqual(self.f.call('music.jobs.get',{'id':j['id']})['state'],'completed')
    def test_symlink_spool_artifact_cannot_be_published(self):
        j=self.enqueue();self.music.tick();row=self.f.state.get('music_jobs',j['id']);t=row['tracks'][0]
        file=self.spool/t['_task']/'audio.mp3';file.unlink();file.symlink_to(self.f.root/'state.sqlite')
        self.settle();self.assertEqual(self.f.call('music.jobs.get',{'id':j['id']})['tracks'][0]['state'],'failed')

    def artist_job(self):
        receipt=self.f.call('music.resolve',{'requestId':'artist','selection':{'kind':'artist','id':'d'*22}})
        self.music.tick();p=self.f.call('music.plan.get',{'id':receipt['id']})
        self.assertEqual(p['state'],'completed',p)
        return self.f.call('music.enqueue',{'requestId':'artist-download','planId':p['id'],'planRevision':p['revision'],'destination':{'driveId':'files','path':''}})
    def test_artist_download_retains_album_paths_and_skips_existing_album(self):
        self.enqueue();self.settle()
        artist=self.artist_job();self.settle();self.settle()
        result=self.f.call('music.jobs.get',{'id':artist['id']})
        self.assertEqual(result['state'],'completed',result)
        self.assertEqual(len(result['releases']),2)
        first=self.f.call('music.jobs.get',{'id':result['releases'][0]['jobId']})
        self.assertTrue(all(t['state']=='already-present' for t in first['tracks']))
        self.assertEqual(len(list((self.f.root/'files').rglob('*.mp3'))),4)
        listed=self.f.call('music.jobs.list',{})['items']
        self.assertEqual(len(listed),2) # original album + artist, children stay nested
        self.assertNotIn('_planId',str(result))

    def test_artist_status_follows_independently_retried_release(self):
        from archive_backend import music_discography
        child = self.enqueue()
        artist = self.artist_job()
        parent = self.music.get('jobs', artist['id'])
        row = self.music.get('jobs', child['id'])
        row['state'] = 'failed'
        self.music.save(row)
        parent['releases'] = [parent['releases'][0]]
        parent['releases'][0]['jobId'] = row['id']
        music_discography.update_release(parent['releases'][0], row)
        parent['state'] = 'partial'
        self.music.save(parent)
        starts = self.worker.starts
        music_discography.tick(self.music)
        self.assertEqual(self.music.get('jobs', parent['id'])['state'], 'partial')
        self.assertEqual(self.worker.starts, starts)
        row['state'] = 'queued'
        self.music.save(row)
        music_discography.tick(self.music)
        self.assertEqual(self.music.get('jobs', parent['id'])['state'], 'running')
        parent = self.music.get('jobs', parent['id'])
        parent['state'] = 'paused'
        self.music.save(parent)
        music_discography.tick(self.music)
        self.assertEqual(self.music.get('jobs', parent['id'])['state'], 'paused')
    def test_artist_pause_restart_resume_and_idempotent_children(self):
        job=self.artist_job();self.music.tick();self.music.tick()
        job=self.f.call('music.jobs.get',{'id':job['id']})
        paused=self.f.call('music.jobs.control',{'id':job['id'],'revision':job['revision'],'action':'pause'})
        self.music=MusicService(self.f.service,self.music.config,self.worker);self.f.service.music=self.music
        self.music.tick();self.assertEqual(self.f.call('music.jobs.get',{'id':job['id']})['state'],'paused')
        self.f.call('music.jobs.control',{'id':job['id'],'revision':paused['revision'],'action':'resume'})
        self.settle();self.settle()
        self.assertEqual(self.f.call('music.jobs.get',{'id':job['id']})['state'],'completed')
        self.assertEqual(len(self.f.state.list('music_jobs')),3)
    def test_artist_cancel_before_expansion_starts_no_albums(self):
        job=self.artist_job()
        self.f.call('music.jobs.control',{'id':job['id'],'revision':job['revision'],'action':'cancel'})
        self.settle()
        self.assertEqual(len(self.f.state.list('music_jobs')),1)
        self.assertEqual(list((self.f.root/'files').rglob('*.mp3')),[])
    def test_artist_incomplete_or_repeated_releases_are_rejected(self):
        from archive_backend.music_contracts import artist_plan
        for change in ({'expectedTracks':5},{'releases':[ARTIST['releases'][0]]*2}):
            with self.assertRaises(BackendError):artist_plan(dict(ARTIST,**change))
    def test_artist_metadata_failure_retry_resolves_only_failed_release(self):
        job=self.artist_job();self.music.tick()
        row=self.f.state.get('music_jobs',job['id']);release=row['releases'][0]
        plan=self.f.state.get('music_plans',release['_planId']);plan.update(state='failed',error='Fixture metadata failure')
        self.f.state.put('music_plans',plan['id'],plan)
        self.settle();self.settle()
        result=self.f.call('music.jobs.get',{'id':job['id']});self.assertEqual(result['state'],'partial',result)
        self.f.call('music.jobs.control',{'id':job['id'],'revision':result['revision'],'action':'retry-failed'})
        self.settle();self.settle()
        result=self.f.call('music.jobs.get',{'id':job['id']});self.assertEqual(result['state'],'completed',result)
        self.assertEqual(len(self.f.state.list('music_jobs')),3)

    def test_artist_child_association_rolls_back_without_an_orphan(self):
        from unittest.mock import patch
        job=self.artist_job();self.music.tick()
        self.music.metadata()
        original=self.f.state.put
        failed=False
        def put(namespace,identity,value):
            nonlocal failed
            if namespace=='music_jobs' and identity==job['id'] and any(r['jobId'] for r in value['releases']) and not failed:
                failed=True
                raise BackendError('STATE_UNAVAILABLE','Injected atomic association failure')
            return original(namespace,identity,value)
        with patch.object(self.f.state,'put',side_effect=put): self.music.tick()
        row=self.f.state.get('music_jobs',job['id'])
        self.assertTrue(failed)
        self.assertIsNone(row['releases'][0]['jobId'])
        self.assertEqual(len(self.f.state.list('music_jobs')),1)
    def test_discography_reserves_history_capacity_before_acceptance(self):
        from unittest.mock import patch
        import archive_backend.music_service as module
        p=self.plan()
        root=dict(self.artist_job(),releases=[dict(ARTIST['releases'][0],jobId=None)]*4)
        self.f.state.put('music_jobs',root['id'],root)
        # 1 artist row + 4 reserved releases + 1 new album exceeds a history of 5.
        with patch.object(module,'HISTORY',5),self.assertRaises(BackendError) as caught:
            self.f.call('music.enqueue',{'requestId':'capacity','planId':p['id'],'planRevision':1,'destination':{'driveId':'files','path':''}})
        self.assertEqual(caught.exception.code,'QUOTA_EXCEEDED')
        with patch.object(module,'HISTORY',6):
            self.f.call('music.enqueue',{'requestId':'capacity-fits','planId':p['id'],'planRevision':1,'destination':{'driveId':'files','path':''}})
    def test_history_beyond_one_thousand_never_hides_unfinished_work(self):
        """Keys are random; old capped list() reads dropped arbitrary jobs once history passed 1,000."""
        base={'revision':1,'title':'Old','artist':'A','state':'completed','destination':{'driveId':'files','path':''},'tracks':[]}
        with self.f.state.transaction():
            for i in range(1200):
                self.f.state.put('music_jobs','%032x'%(i*7919),dict(base,id='%032x'%(i*7919),createdAt=i,_parentId='parent'))
        j=self.enqueue();self.assertEqual(self.music.job_count(),1201)
        self.assertIn(j['id'],[r['id'] for r in self.music.unfinished_albums()]);self.settle()
        self.assertEqual(self.f.call('music.jobs.get',{'id':j['id']})['state'],'completed')
        self.assertEqual([r['id'] for r in self.f.call('music.jobs.list',{})['items']],[j['id']],'children of artist jobs stay out of the top-level history')
    def test_music_scheduler_queries_use_partial_indexes(self):
        from archive_backend.music_service import UNFINISHED_ALBUMS,ARTIST_JOBS,TOP_LEVEL_JOBS
        for where,index in ((UNFINISHED_ALBUMS,'music_unfinished_albums'),(ARTIST_JOBS,'music_artist_jobs'),(TOP_LEVEL_JOBS,'music_top_level_jobs')):
            with self.f.state.transaction() as connection:
                plan=' '.join(str(tuple(r)) for r in connection.execute("EXPLAIN QUERY PLAN SELECT body FROM records WHERE "+where))
            self.assertIn(index,plan)
    def test_artist_job_history_is_bounded_and_pageable(self):
        release=dict(ARTIST['releases'][0],title='x'*500,state='queued',jobId=None,completedTracks=0,totalTracks=2,error=None)
        for i in range(8):
            row={'id':'history-'+str(i),'kind':'artist','revision':1,'title':'Artist','artist':'Artist','state':'paused','destination':{'driveId':'files','path':''},'createdAt':i,'tracks':[],'releases':[release]*500}
            self.f.state.put('music_jobs',row['id'],row)
        first=self.f.call('music.jobs.list',{})
        self.assertLess(len(first['items']),8);self.assertIsNotNone(first['cursor'])
        ids=[r['id'] for r in first['items']];cursor=first['cursor']
        while cursor:
            next_page=self.f.call('music.jobs.list',{'cursor':cursor});ids.extend(r['id'] for r in next_page['items']);cursor=next_page['cursor']
        self.assertEqual(len(ids),8);self.assertEqual(len(set(ids)),8)

    def test_parallel_release_rechecks_verified_track_before_import(self):
        first=self.enqueue();p=self.f.state.list('music_plans')[0]
        second=self.f.call('music.enqueue',{'requestId':'parallel','planId':p['id'],'planRevision':p['revision'],'destination':{'driveId':'files','path':''}})
        self.settle()
        jobs=[self.f.call('music.jobs.get',{'id':j['id']}) for j in (first,second)]
        self.assertTrue(all(j['state']=='completed' for j in jobs),jobs)
        self.assertFalse(any(j['state']=='failed' for j in self.f.service.jobs.snapshot(self.f.owner)['jobs']))
        self.assertEqual(len(self.f.state.list('transfers')),2)
        self.assertEqual(sum(t['state']=='already-present' for j in jobs for t in j['tracks']),2)

    def test_directory_operation_finishing_before_status_read_is_success(self):
        original=self.f.service.operations.submit
        def immediate(principal,args):
            receipt=original(principal,args)
            while lease:=self.f.service.jobs.claim('fast-mkdir'):
                self.f.service.operations.execute(lease)
            return receipt
        self.f.service.operations.submit=immediate
        self.music.imports.directories(self.f.owner,'files','Fast/Disc 01')
        self.assertTrue((self.f.root/'files/Fast/Disc 01').is_dir())

    def test_album_directory_recovers_interrupted_startup_without_losing_artifact(self):
        with self.assertRaises(BackendError):
            self.music.imports.directories(self.f.owner, 'files', 'Recover')
        lease = self.f.service.jobs.claim('startup-failure')
        job_id = lease['job']['id']
        job = self.f.state.get('jobs', job_id)
        job['_workerStopped'] = True
        self.f.state.put('jobs', job_id, job)
        self.f.service.jobs.recover([job_id])
        with self.assertRaises(BackendError) as error:
            self.music.imports.directories(self.f.owner, 'files', 'Recover')
        self.assertEqual(error.exception.code, 'BUSY')
        self.assertEqual(self.f.service.jobs.get(self.f.owner, job_id)['state'], 'queued')
        self.f.service.run_one()
        self.music.imports.directories(self.f.owner, 'files', 'Recover')
        self.assertEqual(len(self.f.state.list('jobs')), 1)

    def test_unknown_existing_file_is_not_staged_or_overwritten(self):
        j=self.enqueue();row=self.f.state.get('music_jobs',j['id']);track=row['tracks'][0]
        file=self.f.root/'files'/track['_path'];file.parent.mkdir(parents=True);file.write_bytes(b'keep this file')
        self.settle();job=self.f.call('music.jobs.get',{'id':j['id']})
        self.assertEqual(job['tracks'][0]['state'],'conflict');self.assertEqual(file.read_bytes(),b'keep this file')
        self.assertFalse(any(t['path']==track['_path'] for t in self.f.state.list('transfers')))
