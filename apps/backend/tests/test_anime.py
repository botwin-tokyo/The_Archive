import copy,hashlib,os,time,unittest,unittest.mock
from dataclasses import replace
from archive_backend.contracts import BackendError
from archive_backend.anime_service import AnimeService
from archive_backend.anime_contracts import episode_number,target,folder,MAX_EPISODES_PER_JOB
from archive_backend.mcp_adapter import tool_list,dispatch
from support import Fixture

PROVIDER='settlar/frieren-beyond-journeys-end-6351'
OTHER='settlar/frieren-movie-77'
CATALOG={'anilistId':154587,'malId':52991,'title':"Frieren: Beyond Journey's End",'altTitles':['Sousou no Frieren'],'year':2023,'format':'TV','status':'FINISHED','episodes':28,'hasCover':True}
SEARCH={'items':[CATALOG],'hasNextPage':False,'covers':{'154587':'data:image/jpeg;base64,/9j/AA=='}}
def candidate(provider=PROVIDER,evidence='anilist-id'):
    return {'providerSeriesId':provider,'title':CATALOG['title'],'year':2023,'type':'TV','episodeCount':28,'anilistId':154587 if evidence=='anilist-id' else None,'malId':52991,'evidence':evidence}
LOOKUP={'catalog':CATALOG,'description':'An elf mage reflects on a finished journey.','genres':['Adventure'],'relations':[{'anilistId':182255,'title':'Frieren Season 2','relation':'SEQUEL','format':'TV','year':2026}],
        'candidates':[candidate()],'match':PROVIDER,
        'episodes':[{'id':'e1.aaa','number':1,'title':'The End of the Journey','sub':True,'dub':True},{'id':'e1.bbb','number':2,'title':None,'sub':True,'dub':False},{'id':'e1.ccc','number':'12.5','title':'Recap','sub':True,'dub':False}],
        'samples':{'sub':{'heights':[1080],'subtitles':['en','es']},'dub':{'heights':[1080],'subtitles':['en']}},'cover':'data:image/jpeg;base64,/9j/AA=='}
MEDIA=b'\x00\x00\x00\x18ftypisom fixture episode'

class Worker:
    """Deterministic stand-in for the isolated worker protocol, including restarts and failures."""
    def __init__(self,root):
        self.root=root;self.tasks={};self.starts=[];self.running=False;self.lookup=copy.deepcopy(LOOKUP);self.epoch='epoch-1';self.failures={}
    def call(self,**args):
        op=args['op'];id=args.get('taskId')
        if op=='status':return {'available':True,'epoch':self.epoch}
        if op=='start':
            self.starts.append((id,args['action'],copy.deepcopy(args['payload'])))
            if args['action']=='search':result=copy.deepcopy(SEARCH)
            elif args['action']=='lookup':result=copy.deepcopy(self.lookup)
            else:
                d=self.root/id;d.mkdir(exist_ok=True);p=d/'media.mp4';p.write_bytes(MEDIA+args['payload']['episodeId'].encode());p.chmod(0o640)
                data=p.read_bytes()
                result={'filename':'media.mp4','sizeBytes':str(len(data)),'sha256':hashlib.sha256(data).hexdigest(),'height':1080,'durationSeconds':1440.0,
                        'audioLanguage':'jpn' if args['payload']['audio']=='sub' else 'eng','subtitles':args['payload']['subtitles'],'manifest':'a'*64}
                failure=self.failures.get(args['payload']['episodeId'])
                if failure:self.tasks[id]={'state':'failed','error':failure,'epoch':self.epoch};return self.tasks[id]
            self.tasks[id]={'state':'running','epoch':self.epoch} if self.running and args['action']=='download' else {'state':'completed','result':result,'epoch':self.epoch}
        if op=='cancel' and id in self.tasks:self.tasks[id]={'state':'canceled','epoch':self.epoch}
        if op=='cleanup':self.tasks.pop(id,None)
        return self.tasks.get(id,{'state':'absent','epoch':self.epoch})
    def restart(self):
        self.tasks={};self.epoch='epoch-2'

class AnimeTests(unittest.TestCase):
    def setUp(self):
        self.f=Fixture();self.addCleanup(self.f.close);self.spool=self.f.root/'spool';self.spool.mkdir()
        self.f.service.config=replace(self.f.service.config,volumes=({'id':'files','role':'files'},))
        self.worker=Worker(self.spool);self.config={'socket':'/unused','spool':str(self.spool),'workerUid':os.getuid()}
        self.anime=AnimeService(self.f.service,self.config,self.worker);self.f.service.anime=self.anime
    def settle(self,rounds=12):
        for _ in range(rounds):
            self.anime.tick()
            if self.anime.import_thread:self.anime.import_thread.join(10)
            while lease:=self.f.service.jobs.claim('fixture-anime'):self.f.service.operations.execute(lease)
    def series(self,key='lookup',**extra):
        s=self.f.call('anime.series.lookup',dict({'requestId':key,'anilistId':154587},**extra));self.settle(2);return self.f.call('anime.series.get',{'id':s['id']})
    def plan(self,episodes=('e1.aaa','e1.bbb'),key='plan',**changes):
        s=self.series()
        args={'requestId':key,'seriesId':s['id'],'seriesRevision':s['revision'],'episodeIds':list(episodes),'audio':'sub','quality':'1080p','subtitles':['en'],'destination':{'driveId':'files','path':''}}
        args.update(changes);return self.f.call('anime.resolve',args)
    def enqueue(self,key='enqueue',**changes):
        p=self.plan(key='plan-'+key,**changes);return self.f.call('anime.enqueue',{'requestId':key,'planId':p['id'],'planRevision':p['revision']})
    def job(self,j):return self.f.call('anime.jobs.get',{'id':j['id']})
    def published(self):return sorted(p.relative_to(self.f.root/'files').as_posix() for p in (self.f.root/'files').rglob('*.mp4'))

    def test_search_lookup_plan_enqueue_and_verified_publication(self):
        search=self.f.call('anime.search',{'requestId':'search','query':'  Frieren '});self.settle(1)
        done=self.f.call('anime.search.get',{'id':search['id']});self.assertEqual(done['state'],'completed');self.assertEqual(done['items'][0]['anilistId'],154587)
        self.assertTrue(self.f.call('anime.cover.get',{'anilistId':154587})['dataUrl'].startswith('data:image/jpeg'))
        s=self.series();self.assertEqual(s['match'],{'state':'matched','providerSeriesId':PROVIDER,'evidence':'anilist-id'});self.assertEqual(s['episodes'][2]['number'],'12.5')
        p=self.plan(episodes=('e1.aaa','e1.bbb','e1.ccc'));self.assertEqual(p['counts'],{'total':3,'new':3,'already-present':0,'conflict':0,'unavailable':0})
        self.assertEqual(p['episodes'][2]['name'],'Episode 012.5 [sub].mp4')
        j=self.f.call('anime.enqueue',{'requestId':'enqueue','planId':p['id'],'planRevision':p['revision']});self.settle()
        done=self.job(j);self.assertEqual(done['state'],'completed',done);self.assertEqual(done['summary']['completed'],3)
        folder_name="Frieren_ Beyond Journey's End [AL-154587]"
        self.assertEqual(self.published(),[f'{folder_name}/Episode 001 [sub].mp4',f'{folder_name}/Episode 002 [sub].mp4',f'{folder_name}/Episode 012.5 [sub].mp4'])
        self.assertEqual((self.f.root/'files'/folder_name/'Episode 001 [sub].mp4').read_bytes(),MEDIA+b'e1.aaa')
        self.assertTrue(all(e['entry'] and e['height']==1080 for e in done['episodes']))
        self.assertFalse(any(k.startswith('_') for e in done['episodes'] for k in e))
        payload=next(p for _,a,p in self.worker.starts if a=='download');self.assertEqual(payload,{'providerSeriesId':PROVIDER,'episodeId':'e1.aaa','number':'1','audio':'sub','quality':'1080p','subtitles':['en']})

    def test_receipt_deduplicates_and_unknown_file_is_preserved(self):
        j=self.enqueue();self.settle();self.assertEqual(self.job(j)['state'],'completed')
        downloads=sum(a=='download' for _,a,_ in self.worker.starts)
        p=self.plan(key='again');self.assertEqual(p['counts']['already-present'],2)
        j2=self.f.call('anime.enqueue',{'requestId':'again-enqueue','planId':p['id'],'planRevision':p['revision']});self.settle()
        self.assertEqual(self.job(j2)['state'],'completed');self.assertEqual(sum(a=='download' for _,a,_ in self.worker.starts),downloads)
        # An unknown file at the generated name is never overwritten.
        name=target(folder({'driveId':'files','path':''},CATALOG['title'],154587),'12.5','sub')
        p=self.plan(episodes=('e1.ccc',),key='collide');self.assertEqual(p['counts']['new'],1)
        (self.f.root/'files'/name).write_bytes(b'owner file');p=self.plan(episodes=('e1.ccc',),key='collide-2')
        self.assertEqual(p['counts']['conflict'],1);j3=self.f.call('anime.enqueue',{'requestId':'collide','planId':p['id'],'planRevision':p['revision']});self.settle()
        self.assertEqual(self.job(j3)['episodes'][0]['state'],'conflict');self.assertEqual((self.f.root/'files'/name).read_bytes(),b'owner file')

    def test_conflict_created_after_review_is_preserved(self):
        j=self.enqueue(episodes=('e1.aaa',));row=self.f.state.get('anime_jobs',j['id']);path=row['episodes'][0]['_path']
        (self.f.root/'files'/path.rpartition('/')[0]).mkdir();(self.f.root/'files'/path).write_bytes(b'arrived later');self.settle()
        done=self.job(j);self.assertEqual(done['episodes'][0]['state'],'conflict',done);self.assertEqual((self.f.root/'files'/path).read_bytes(),b'arrived later')

    def test_idempotency_and_reconcile(self):
        a=self.f.call('anime.search',{'requestId':'same','query':'Frieren'});b=self.f.call('anime.search',{'requestId':'same','query':'Frieren'});self.assertEqual(a['id'],b['id'])
        with self.assertRaises(BackendError) as caught:self.f.call('anime.search',{'requestId':'same','query':'Other'})
        self.assertEqual(caught.exception.code,'CONFLICT')
        j=self.enqueue();again=self.f.call('anime.enqueue',{'requestId':'enqueue','planId':self.f.state.get('anime_jobs',j['id'])['_planId'],'planRevision':1});self.assertEqual(again['id'],j['id'])
        self.assertEqual(self.f.call('anime.reconcile',{'requestId':'enqueue'})['id'],j['id']);self.assertIsNone(self.f.call('anime.reconcile',{'requestId':'never-sent'}))

    def test_search_cache_reuses_recent_identical_page(self):
        self.f.call('anime.search',{'requestId':'one','query':'Frieren'});self.settle(1)
        second=self.f.call('anime.search',{'requestId':'two','query':'  frieren'});self.assertEqual(second['state'],'completed');self.assertTrue(second['cached'])
        self.assertEqual(sum(a=='search' for _,a,_ in self.worker.starts),1)

    def test_invalid_and_stale_plans(self):
        s=self.series();base={'requestId':'x','seriesId':s['id'],'seriesRevision':s['revision'],'episodeIds':['e1.aaa'],'audio':'sub','quality':'1080p','subtitles':['en'],'destination':{'driveId':'files','path':''}}
        cases={'stale':{'seriesRevision':s['revision']+1},'unknown episode':{'episodeIds':['e1.zzz']},'quality':{'quality':'720p'},'subtitle':{'subtitles':['fr']},'audio':{'audio':'raw'},
               'duplicate':{'episodeIds':['e1.aaa','e1.aaa']},'empty':{'episodeIds':[]},'too many':{'episodeIds':['e%d'%i for i in range(MAX_EPISODES_PER_JOB+1)]},'backups':{'destination':{'driveId':'backups','path':''}},'missing folder':{'destination':{'driveId':'files','path':'absent'}}}
        for name,change in cases.items():
            with self.subTest(name),self.assertRaises(BackendError):self.f.call('anime.resolve',dict(base,requestId=name,**change))
        p=self.plan(episodes=('e1.aaa','e1.bbb'),key='dub',audio='dub');self.assertEqual([e['status'] for e in p['episodes']],['new','unavailable'])
        with self.assertRaises(BackendError) as caught:self.f.call('anime.enqueue',{'requestId':'stale-plan','planId':p['id'],'planRevision':p['revision']+1})
        self.assertEqual(caught.exception.code,'STALE_REVISION')
        row=self.f.state.get('anime_plans',p['id']);row['expiresAt']=0;self.f.state.put('anime_plans',p['id'],row)
        with self.assertRaises(BackendError):self.f.call('anime.enqueue',{'requestId':'expired','planId':p['id'],'planRevision':p['revision']})

    def test_unavailable_dub_and_partial_batch(self):
        self.worker.failures['e1.bbb']='INCOMPLETE_SOURCE';j=self.enqueue();self.settle();done=self.job(j)
        self.assertIn('far shorter than its listed runtime',done['episodes'][1]['error'])
        self.assertEqual(done['state'],'partial',done);self.assertEqual([e['state'] for e in done['episodes']],['completed','unavailable'])
        self.assertEqual(done['summary'],{'total':2,'completed':1,'failed':0,'unavailable':1,'canceled':0,'pending':0})
        del self.worker.failures['e1.bbb'];done=self.f.call('anime.jobs.control',{'id':j['id'],'revision':done['revision'],'action':'retry-failed'});self.settle()
        self.assertEqual(self.job(j)['state'],'completed');self.assertEqual(len(self.published()),2)
        self.assertEqual(sum(1 for _,a,p in self.worker.starts if a=='download' and p['episodeId']=='e1.aaa'),1,'retry never re-downloads a successful episode')

    def test_retryable_failure_keeps_task_then_gives_up(self):
        self.worker.failures['e1.aaa']='SOURCE_UNAVAILABLE';j=self.enqueue(episodes=('e1.aaa',));self.anime.tick();self.anime.tick()
        row=self.f.state.get('anime_jobs',j['id'])['episodes'][0];self.assertEqual(row['state'],'queued');task=row['_task'];self.assertTrue(task)
        for _ in range(3):
            row=self.f.state.get('anime_jobs',j['id']);row['episodes'][0]['_retryAt']=0;self.f.state.put('anime_jobs',j['id'],row);self.anime.tick();self.anime.tick()
        done=self.job(j);self.assertEqual(done['episodes'][0]['state'],'failed',done);self.assertEqual({t for t,a,_ in self.worker.starts if a=='download'},{task})

    def test_worker_restart_resumes_same_task(self):
        self.worker.running=True;j=self.enqueue(episodes=('e1.aaa',));self.anime.tick()
        task=self.f.state.get('anime_jobs',j['id'])['episodes'][0]['_task'];self.assertIn(self.job(j)['episodes'][0]['state'],('resolving','downloading'))
        self.worker.restart();self.worker.running=False;self.settle()
        self.assertEqual(self.job(j)['state'],'completed');self.assertEqual([t for t,a,_ in self.worker.starts if a=='download'],[task,task])

    def test_core_restart_after_artifact_ready(self):
        j=self.enqueue(episodes=('e1.aaa',));self.anime.tick();self.assertEqual(self.f.state.get('anime_jobs',j['id'])['episodes'][0]['state'],'importing')
        self.anime.import_thread.join(10)
        # Simulate a crash before the import thread recorded its outcome.
        row=self.f.state.get('anime_jobs',j['id']);row['episodes'][0].update(state='importing',entry=None);self.f.state.put('anime_jobs',j['id'],row)
        self.anime=AnimeService(self.f.service,self.config,self.worker);self.f.service.anime=self.anime;self.settle()
        self.assertEqual(self.job(j)['state'],'completed');self.assertEqual(len(self.published()),1)

    def test_controls_and_stale_revision(self):
        self.worker.running=True;j=self.enqueue();self.anime.tick();args={'id':j['id'],'revision':self.job(j)['revision'],'action':'pause'}
        paused=self.f.call('anime.jobs.control',args);self.assertEqual(paused['state'],'paused');self.assertEqual(paused['episodes'][0]['state'],'paused')
        with self.assertRaises(BackendError):self.f.call('anime.jobs.control',args)
        task=self.f.state.get('anime_jobs',j['id'])['episodes'][0]['_task'];self.settle(2);self.assertEqual(self.job(j)['state'],'paused')
        resumed=self.f.call('anime.jobs.control',dict(args,revision=self.job(j)['revision'],action='resume'));self.assertEqual(resumed['state'],'queued')
        self.worker.running=False;self.settle();self.assertEqual(self.job(j)['state'],'completed');self.assertIn(task,[t for t,a,_ in self.worker.starts if a=='download'])
        with self.assertRaises(BackendError):self.f.call('anime.jobs.control',dict(args,revision=self.job(j)['revision'],action='cancel'))

    def test_cancel_discards_unpublished_work_and_keeps_completed(self):
        j=self.enqueue();self.settle(1);self.anime.import_thread.join(10);self.worker.running=True;self.settle(1)
        current=self.job(j);self.assertEqual(current['episodes'][0]['state'],'completed',current)
        canceled=self.f.call('anime.jobs.control',{'id':j['id'],'revision':current['revision'],'action':'cancel'});self.settle()
        done=self.job(j);self.assertEqual([e['state'] for e in done['episodes']],['completed','canceled']);self.assertEqual(done['state'],'partial');self.assertEqual(len(self.published()),1)
        self.assertNotIn(self.f.state.get('anime_jobs',j['id'])['episodes'][1]['_task'],self.worker.tasks)

    def test_publication_receipt_wins_over_late_cancel(self):
        j=self.enqueue(episodes=('e1.aaa',));original=self.anime.imports.record;failed=[False]
        (self.f.root/'files'/folder({'driveId':'files','path':''},CATALOG['title'],154587)).mkdir()
        def fail(*args):
            if not failed[0]:failed[0]=True;raise BackendError('BUSY','Simulated interruption after finalize')
            return original(*args)
        self.anime.imports.record=fail;self.settle(1);self.anime.import_thread.join(10)
        current=self.job(j);self.f.call('anime.jobs.control',{'id':j['id'],'revision':current['revision'],'action':'cancel'})
        done=self.job(j);self.assertEqual(done['episodes'][0]['state'],'completed',done);self.assertEqual(len(self.published()),1)

    def test_corrupt_or_linked_artifact_never_published(self):
        self.worker.running=True;j=self.enqueue(episodes=('e1.aaa',));self.anime.tick()
        task=self.f.state.get('anime_jobs',j['id'])['episodes'][0]['_task'];self.worker.running=False;self.worker.tasks.pop(task)
        original=self.worker.call
        def corrupt(**args):
            result=original(**args)
            if args['op']=='start' and args['action']=='download':(self.spool/args['taskId']/'media.mp4').write_bytes(b'tampered')
            return result
        self.worker.call=corrupt;self.settle();done=self.job(j);self.assertEqual(done['episodes'][0]['state'],'failed',done);self.assertEqual(self.published(),[])
        self.worker.call=original;self.f.call('anime.jobs.control',{'id':j['id'],'revision':done['revision'],'action':'retry-failed'});self.settle()
        self.assertEqual(self.job(j)['state'],'completed')

    def test_ambiguous_match_requires_owner_selection_and_is_remembered(self):
        self.worker.lookup.update(match=None,candidates=[candidate(evidence='title'),candidate(OTHER,'title')],episodes=[])
        s=self.series();self.assertEqual(s['match']['state'],'ambiguous')
        with self.assertRaises(BackendError):self.f.call('anime.resolve',{'requestId':'r','seriesId':s['id'],'seriesRevision':s['revision'],'episodeIds':['e1.aaa'],'audio':'sub','quality':'best','subtitles':[],'destination':{'driveId':'files','path':''}})
        self.worker.lookup=copy.deepcopy(LOOKUP);self.worker.lookup['candidates']=[candidate(evidence='title')]
        chosen=self.series('pick',providerSeriesId=PROVIDER);self.assertEqual(chosen['match']['state'],'selected')
        self.assertEqual(self.f.state.get('anime_mappings','154587')['providerSeriesId'],PROVIDER)
        self.series('later');self.assertEqual(self.worker.starts[-1][2],{'anilistId':154587,'providerSeriesId':PROVIDER})

    def test_untrusted_worker_metadata_is_rejected(self):
        for name,change in {'wrong series':{'catalog':dict(CATALOG,anilistId=1)},'bad number':{'episodes':[{'id':'e1','number':-1,'title':None,'sub':True,'dub':False}]},
                            'repeated':{'episodes':[LOOKUP['episodes'][0],LOOKUP['episodes'][0]]},'foreign match':{'match':OTHER},'bad cover':{'cover':'javascript:alert(1)'}}.items():
            with self.subTest(name):
                self.worker.lookup=dict(copy.deepcopy(LOOKUP),**change);s=self.series('bad-'+name);self.assertEqual(s['state'],'failed',s)

    def test_owner_only_and_mcp_schema(self):
        token='agent-'+40*'y';self.f.service.auth.register(self.f.owner,'agent',token,['read','write'],[('files','')])
        for cap in ['anime.status','anime.jobs.list']:
            with self.assertRaises(BackendError):self.f.call(cap,token=token)
        tools={t['name']:t for t in tool_list()}
        schema=tools['anime.resolve']['inputSchema'];self.assertEqual(schema['properties']['destination']['required'],['driveId','path']);self.assertEqual(schema['properties']['audio'],{'enum':['sub','dub'],'description':'sub = Japanese audio, dub = English audio'})
        self.assertTrue(tools['anime.status']['annotations']['readOnlyHint']);self.assertFalse(tools['anime.enqueue']['annotations']['readOnlyHint'])
        result=dispatch(self.f.service,self.f.token,{'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'anime.status','arguments':{}}});self.assertFalse(result['result']['isError'])

    def test_job_history_pages_newest_first(self):
        ids=[self.enqueue('job-%d'%i,episodes=('e1.aaa',))['id'] for i in range(3)]
        first=self.f.call('anime.jobs.list',{'limit':2});self.assertEqual([j['id'] for j in first['items']],ids[::-1][:2])
        second=self.f.call('anime.jobs.list',{'limit':2,'cursor':first['cursor']});self.assertEqual([j['id'] for j in second['items']],[ids[0]]);self.assertIsNone(second['cursor'])

    def test_unconfigured_service_reports_unavailable(self):
        anime=AnimeService(self.f.service,None);self.f.service.anime=anime
        status=self.f.call('anime.status');self.assertFalse(status['enabled'])
        with self.assertRaises(BackendError) as caught:self.f.call('anime.search',{'requestId':'s','query':'Frieren'})
        self.assertEqual(caught.exception.code,'UNAVAILABLE')

    # Phase 5: crash boundaries and coordination -----------------------------------
    def test_lost_dispatch_response_reuses_persisted_task(self):
        j=self.enqueue(episodes=('e1.aaa',));original=self.worker.call;lost=[False]
        def call(**args):
            result=original(**args)
            if args['op']=='start' and args['action']=='download' and not lost[0]:lost[0]=True;raise BackendError('UNAVAILABLE','Lost response')
            return result
        self.worker.call=call;self.settle()
        self.assertEqual(self.job(j)['state'],'completed');tasks=[t for t,a,_ in self.worker.starts if a=='download'];self.assertEqual(len(set(tasks)),1);self.assertEqual(len(tasks),2)

    def test_import_resumes_from_durable_transfer_offset(self):
        big=b'\x00'*(3*1024*1024+17);original=self.worker.call
        def call(**args):
            result=original(**args)
            if args['op']=='start' and args['action']=='download':
                p=self.spool/args['taskId']/'media.mp4';p.write_bytes(big);p.chmod(0o640)
                self.worker.tasks[args['taskId']]['result'].update(sizeBytes=str(len(big)),sha256=hashlib.sha256(big).hexdigest())
            return result
        self.worker.call=call;transfers=self.f.service.transfers;write=transfers.write;offsets=[];failed=[False]
        def interrupted(principal,transfer,offset,chunk):
            if offset>0 and not failed[0]:failed[0]=True;raise BackendError('BUSY','Simulated interruption mid-import')
            offsets.append(offset);return write(principal,transfer,offset,chunk)
        with unittest.mock.patch.object(transfers,'write',interrupted):
            j=self.enqueue(episodes=('e1.aaa',));self.settle()
        self.assertEqual(self.job(j)['state'],'completed');self.assertEqual(offsets,[0,1024*1024,2*1024*1024,3*1024*1024],'resumed at the acknowledged offset, not byte zero')
        self.assertEqual(hashlib.sha256((self.f.root/'files'/self.published()[0]).read_bytes()).hexdigest(),hashlib.sha256(big).hexdigest())

    def test_same_episode_in_two_jobs_downloads_once(self):
        self.worker.running=True;a=self.enqueue('first',episodes=('e1.aaa',));b=self.enqueue('second',episodes=('e1.aaa',));self.settle(3)
        self.assertEqual(sum(a_=='download' for _,a_,_ in self.worker.starts),1);self.assertEqual(self.job(b)['episodes'][0]['state'],'queued')
        self.worker.running=False;task=self.f.state.get('anime_jobs',a['id'])['episodes'][0]['_task'];self.worker.tasks.pop(task);self.settle()
        self.assertEqual([self.job(x)['state'] for x in (a,b)],['completed','completed']);self.assertEqual(self.job(b)['episodes'][0]['state'],'already-present')
        self.assertEqual(len(self.published()),1)

    def test_offline_drive_waits_then_continues(self):
        j=self.enqueue(episodes=('e1.aaa',));drive=self.f.service.catalog.filesystems.pop('files')
        self.settle(2);waiting=self.job(j);self.assertEqual((waiting['state'],waiting['episodes'][0]['state']),('waiting-for-drive','waiting-for-drive'))
        self.f.service.catalog.filesystems['files']=drive;self.settle();self.assertEqual(self.job(j)['state'],'completed')

    def test_revoked_requester_blocks_remaining_episodes(self):
        self.worker.running=True;j=self.enqueue();self.anime.tick()
        with unittest.mock.patch.object(self.anime,'principal',side_effect=BackendError('PERMISSION_REVOKED','revoked')):self.anime.tick()
        done=self.job(j);self.assertEqual(done['state'],'failed');self.assertEqual({e['state'] for e in done['episodes']},{'blocked-auth'})
        self.assertTrue(all(v['state']=='canceled' for k,v in self.worker.tasks.items()))

    def test_active_job_scheduler_uses_partial_index(self):
        from archive_backend.anime_service import ACTIVE_JOBS
        with self.f.state.transaction() as connection:
            plan=' '.join(str(tuple(r)) for r in connection.execute("EXPLAIN QUERY PLAN SELECT body FROM records WHERE "+ACTIVE_JOBS+" ORDER BY key LIMIT 51"))
        self.assertIn('anime_active_jobs',plan)
        done=self.enqueue(episodes=('e1.aaa',));self.settle();self.assertEqual(self.anime.active_jobs(),[])

    def test_terminal_history_does_not_count_against_active_queue(self):
        for i in range(3):self.enqueue('done-%d'%i,episodes=('e1.aaa',));self.settle()
        self.assertEqual(len(self.anime.active_jobs()),0);self.assertEqual(len(self.f.call('anime.jobs.list')['items']),3)

    # Phase 8 matrix additions ------------------------------------------------------
    def test_destination_removed_between_review_and_enqueue(self):
        (self.f.root/'files'/'Incoming').mkdir();p=self.plan(key='dest',destination={'driveId':'files','path':'Incoming'});(self.f.root/'files'/'Incoming').rmdir()
        with self.assertRaises(BackendError):self.f.call('anime.enqueue',{'requestId':'dest-enqueue','planId':p['id'],'planRevision':p['revision']})
        self.assertIsNone(self.f.call('anime.reconcile',{'requestId':'dest-enqueue'}),'a refused admission leaves no accepted request')

    def test_artifact_must_match_the_requested_variant(self):
        from archive_backend.anime_contracts import artifact
        good={'filename':'media.mp4','sizeBytes':'10','sha256':'a'*64,'height':1080,'durationSeconds':1440.0,'audioLanguage':'jpn','subtitles':['en'],'manifest':'b'*64}
        self.assertEqual(artifact(dict(good),'sub','1080p',['en'])['height'],1080);artifact(dict(good,height=720),'sub','best',['en'])
        for change,args in [({'audioLanguage':'eng'},('sub','1080p',['en'])),({'subtitles':[]},('sub','1080p',['en'])),({'height':720},('sub','1080p',['en'])),
                            ({'sizeBytes':str(7*1024**3)},('sub','best',['en'])),({'sha256':'x'},('sub','best',['en'])),({'filename':'../x.mp4'},('sub','best',['en'])),({'durationSeconds':float('inf')},('sub','best',['en']))]:
            with self.subTest(change=change),self.assertRaises(BackendError):artifact(dict(good,**change),*args)

    def test_rate_limited_metadata_is_reported_clearly(self):
        original=self.worker.call
        def limited(**args):
            if args['op']=='start' and args['action']=='search':self.worker.tasks[args['taskId']]={'state':'failed','error':'RATE_LIMITED','epoch':self.worker.epoch};return self.worker.tasks[args['taskId']]
            return original(**args)
        self.worker.call=limited;s=self.f.call('anime.search',{'requestId':'limited','query':'Frieren'});self.settle(1)
        done=self.f.call('anime.search.get',{'id':s['id']});self.assertEqual(done['state'],'failed');self.assertIn('rate limiting',done['error'])

    def test_unknown_catalog_episode_count_and_provider_inventory_are_independent(self):
        self.worker.lookup['catalog']=dict(CATALOG,episodes=None);s=self.series('unknown-count')
        self.assertIsNone(s['catalog']['episodes']);self.assertEqual(len(s['episodes']),3)

    def test_retry_failed_inside_paused_job_keeps_it_resumable(self):
        self.worker.failures['e1.aaa']='EPISODE_UNAVAILABLE';self.worker.running=True;j=self.enqueue();self.settle(2)
        current=self.job(j);self.assertEqual(current['episodes'][0]['state'],'unavailable')
        paused=self.f.call('anime.jobs.control',{'id':j['id'],'revision':current['revision'],'action':'pause'});self.assertEqual(paused['episodes'][1]['state'],'paused')
        del self.worker.failures['e1.aaa'];self.worker.running=False
        retried=self.f.call('anime.jobs.control',{'id':j['id'],'revision':paused['revision'],'action':'retry-failed'})
        self.assertEqual(retried['state'],'paused','retrying inside a paused job keeps the job paused');self.settle(2);self.assertEqual(self.job(j)['state'],'paused')
        self.f.call('anime.jobs.control',{'id':j['id'],'revision':self.job(j)['revision'],'action':'resume'});self.settle()
        self.assertEqual(self.job(j)['state'],'completed')

    def test_configured_default_destination_is_used_and_never_silently_replaced(self):
        from archive_backend.config import parse_config
        self.f.service.config=replace(self.f.service.config,volumes=({'id':'files','role':'files'},{'id':'backups','role':'harness-backups'}))
        (self.f.root/'files'/'Anime').mkdir();(self.f.root/'files'/'Library').mkdir()
        self.anime.config=dict(self.config,defaultDestination={'driveId':'files','path':'Library'})
        self.assertEqual(self.f.call('anime.status')['defaultDestination'],{'driveId':'files','path':'Library'})
        (self.f.root/'files'/'Library').rmdir()
        self.assertIsNone(self.f.call('anime.status')['defaultDestination'],'a missing configured folder is not replaced by files/Anime')
        parse_config({'anime':dict(self.config,defaultDestination={'driveId':'data','path':'Anime'})})
        for bad in ({'driveId':'x','path':'/abs'},{'driveId':'x','path':'../up'},{'driveId':'x'},'data:Anime'):
            with self.subTest(bad=bad),self.assertRaises(BackendError):parse_config({'anime':dict(self.config,defaultDestination=bad)})

    def test_episode_numbers_are_canonical_strings(self):
        self.assertEqual([episode_number(v) for v in (1,'12.5',12.5,0)],['1','12.5','12.5','0'])
        for bad in (-1,'01','1.234',float('nan'),True,'1e3'):
            with self.subTest(bad=bad),self.assertRaises(BackendError):episode_number(bad)

if __name__=='__main__':unittest.main()
