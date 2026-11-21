import hashlib,json,os,shutil,signal,subprocess,tempfile,time,unittest
from pathlib import Path
from unittest import mock
from archive_anime_worker import download,media,net,server,task

SERIES='settlar/frieren-6351'
MEDIA='https://media.settlar.io/v1/object/'
VTT='WEBVTT\n\n00:00.500 --> 00:02.000\nHello\n'

def build(directory):
    """Real 4-second H.264/AAC HLS VOD at 360p in 1-second MPEG-TS segments."""
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','testsrc=size=640x360:rate=24:duration=4','-f','lavfi','-i','sine=duration=4',
                    '-c:v','libx264','-g','24','-c:a','aac','-f','hls','-hls_time','1','-hls_playlist_type','vod','-hls_segment_filename',str(directory/'seg%d.ts'),str(directory/'index.m3u8')],check=True)
    playlist=(directory/'index.m3u8').read_text().replace('seg',MEDIA+'seg')
    return playlist,{MEDIA+p.name:p.read_bytes() for p in directory.glob('seg*.ts')}

class Provider:
    """Fake allowlisted network for one series; records every fetched URL."""
    def __init__(self,playlist,segments):
        self.playlist,self.segments=playlist,dict(segments);self.fetched=[];self.fail_after=None;self.subtitles=['en']
        self.episodes=[{'routeId':'e1.aaa','number':1,'title':'One','sub':True,'dub':False,'runtime':0}]
    def __call__(self,url,*,limit,data=None,headers=None,timeout=30,opener=None):
        net.check(url);self.fetched.append(url)
        if url.startswith('https://ani.pm/api/anime/series/6351'):body={'id':6351,'source':'settlar','episodes':self.episodes}
        elif url.startswith('https://ani.pm/api/anime/playback-bootstrap/'):body={'effectiveLanguage':'sub','settlarSelection':'2.s'}
        elif url.startswith('https://ani.pm/api/anime/settlar/preview-session'):body={'embedUrl':'https://embed.settlar.io/e?t=tok'}
        elif url.startswith('https://embed.settlar.io/api/embed/session'):body={'kind':'hls','source':MEDIA+'master','audioLang':'ja','subtitles':[{'srclang':c,'url':MEDIA+'sub-'+c} for c in self.subtitles]}
        elif url==MEDIA+'master':return 200,{},('#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=640x360,CODECS="avc1.64001e,mp4a.40.2"\n'+MEDIA+'index.m3u8\n').encode()
        elif url==MEDIA+'index.m3u8':return 200,{},self.playlist.encode()
        elif url.startswith(MEDIA+'sub-'):return 200,{},VTT.encode()
        elif url in self.segments:
            if self.fail_after is not None and sum(u in self.segments for u in self.fetched)>self.fail_after:raise net.Failure('SOURCE_UNAVAILABLE')
            return 200,{},self.segments[url]
        else:raise AssertionError('unexpected '+url)
        return 200,{},json.dumps(body).encode()

PAYLOAD={'providerSeriesId':SERIES,'episodeId':'e1.aaa','number':'1','audio':'sub','quality':'360p','subtitles':['en']}

class DownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.playlist,cls.segments=build(Path(cls.temp.name))
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def setUp(self):
        self.dir=Path(tempfile.mkdtemp());self.addCleanup(shutil.rmtree,self.dir,True);self.provider=Provider(self.playlist,self.segments)
        patches=[mock.patch.object(net,'fetch',self.provider),mock.patch.dict(os.environ,{'ARCHIVE_ANIME_SPOOL':str(self.dir)}),mock.patch.object(download,'FREE_MIN',0)]
        for p in patches:p.start();self.addCleanup(p.stop)
        self.stages=[]
    def acquire(self,**change):return download.acquire(dict(PAYLOAD,**change),self.dir,lambda s,f=None:self.stages.append(s))

    def test_verified_mp4_with_tagged_audio_and_subtitles(self):
        result=self.acquire();final=self.dir/'media.mp4'
        self.assertEqual((result['filename'],result['height'],result['audioLanguage'],result['subtitles']),('media.mp4',360,'jpn',['en']))
        self.assertEqual(result['sha256'],hashlib.sha256(final.read_bytes()).hexdigest());self.assertEqual(int(result['sizeBytes']),final.stat().st_size)
        self.assertAlmostEqual(result['durationSeconds'],4,delta=0.5);self.assertEqual(oct(final.stat().st_mode&0o777),'0o640')
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),['media.mp4'])
        self.assertEqual(self.stages[0],'resolving');self.assertIn('verifying',self.stages)

    def test_interrupted_download_resumes_and_revalidates_segments(self):
        self.provider.fail_after=2
        with self.assertRaises(net.Failure):self.acquire()
        state=json.loads((self.dir/'checkpoint.json').read_text());kept=len(state['done']);self.assertGreaterEqual(kept,1)
        first=sorted(state['done'])[0];(self.dir/'segments'/('%05d.ts'%int(first))).write_bytes(b'\x47'+b'\x00'*187)
        self.provider.fail_after=None;self.provider.fetched=[];self.acquire()
        fetched=[u for u in self.provider.fetched if u in self.segments]
        self.assertEqual(len(fetched),len(self.segments)-kept+1,'only missing and corrupted segments are refetched')

    def test_changed_manifest_discards_checkpoint(self):
        self.provider.fail_after=2
        with self.assertRaises(net.Failure):self.acquire()
        self.provider.playlist=self.playlist.replace('seg0.ts','seg0.ts?v=2');self.provider.segments[MEDIA+'seg0.ts?v=2']=self.segments[MEDIA+'seg0.ts']
        self.provider.fail_after=None;self.provider.fetched=[];self.acquire()
        self.assertEqual(len([u for u in self.provider.fetched if u in self.provider.segments]),len(self.segments))

    def test_html_masquerading_as_segment_is_rejected(self):
        self.provider.segments[MEDIA+'seg1.ts']=b'<html>not media</html>'
        with self.assertRaises(net.Failure) as caught:self.acquire()
        self.assertEqual(str(caught.exception),'INVALID_MEDIA');self.assertFalse((self.dir/'media.mp4').exists())

    def test_unavailable_choices_are_explicit(self):
        for change,expected in [({'quality':'1080p'},'QUALITY_UNAVAILABLE'),({'subtitles':['fr']},'SUBTITLE_UNAVAILABLE'),({'audio':'dub'},'VARIANT_UNAVAILABLE'),
                                ({'episodeId':'e1.gone'},'EPISODE_UNAVAILABLE'),({'number':'2'},'EPISODE_UNAVAILABLE')]:
            with self.subTest(change=change),self.assertRaises(net.Failure) as caught:self.acquire(**change)
            self.assertEqual(str(caught.exception),expected)

    def test_quality_best_and_no_subtitles(self):
        result=self.acquire(quality='best',subtitles=[]);self.assertEqual((result['height'],result['subtitles']),(360,[]))

    def test_spool_budget_stops_acquisition(self):
        with mock.patch.object(download,'SPOOL_MAX',10),self.assertRaises(net.Failure) as caught:self.acquire()
        self.assertEqual(str(caught.exception),'DISK_BUDGET')

    def test_verify_rejects_wrong_height_language_and_tracks(self):
        result=self.acquire();path=self.dir/'media.mp4'
        media.verify(path,360,result['durationSeconds'],['en'],'sub')
        for args in [(720,result['durationSeconds'],['en'],'sub'),(360,result['durationSeconds'],['en'],'dub'),(360,result['durationSeconds'],[],'sub'),(360,60,['en'],'sub')]:
            with self.subTest(args=args),self.assertRaises(net.Failure):media.verify(path,*args)
        bad=self.dir/'bad.mp4';bad.write_bytes(b'<html>');
        with self.assertRaises(net.Failure):media.verify(bad,360,4,[],'sub')

    def test_transport_stream_accepts_partial_packets_and_rejects_non_media(self):
        packet=b'\x47'+b'\x00'*187
        self.assertTrue(download.transport_stream(packet*40));self.assertTrue(download.transport_stream(packet*40+b'\x47'+b'\x00'*181),'trailing partial packet (len %% 188 = 182)')
        self.assertTrue(download.transport_stream(b'\x00'*6+packet*40),'leading partial packet')
        for body in [b'<html>'+b'x'*20000,b'\x89PNG\r\n\x1a\n'+b'\x00'*20000,packet*4,packet*20+b'\x00'*188*80]:
            with self.subTest(body=body[:8]):self.assertFalse(download.transport_stream(body))

    def test_anamorphic_coded_height_is_plausible_for_its_label(self):
        """A 720x576 anamorphic master labeled 480p (seen on Trigun) is accepted; a mislabeled 1080p is not."""
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'pal.mp4'
            subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','testsrc=size=720x576:rate=25:duration=2','-f','lavfi','-i','sine=duration=2','-c:v','libx264','-vf','setsar=64/45','-c:a','aac','-metadata:s:a:0','language=eng','-shortest',str(p)],check=True)
            media.verify(p,480,2,[],'dub')
            with self.assertRaises(net.Failure):media.verify(p,1080,2,[],'dub')
            with self.assertRaises(net.Failure):media.verify(p,360,2,[],'dub')

    def test_source_far_shorter_than_listed_runtime_is_not_saved(self):
        self.provider.episodes[0]['runtime']=24
        with self.assertRaises(net.Failure) as caught:self.acquire()
        self.assertEqual(str(caught.exception),'INCOMPLETE_SOURCE');self.assertFalse((self.dir/'media.mp4').exists())
        self.assertFalse(any(u in self.segments for u in self.provider.fetched),'rejected before downloading segments')
        self.provider.episodes[0]['runtime']=0.1;self.assertEqual(self.acquire()['filename'],'media.mp4')

    def test_task_codes_never_leak_upstream_text(self):
        self.assertEqual(task.code(net.Failure('NOT_FOUND'),'download'),'SOURCE_UNAVAILABLE');self.assertEqual(task.code(net.Failure('NOT_FOUND'),'lookup'),'NOT_FOUND')
        self.assertEqual(task.code(net.Failure('https://media.settlar.io/secret?sig=1'),'download'),'SOURCE_UNAVAILABLE');self.assertEqual(task.code(KeyError('x'),'search'),'SOURCE_UNAVAILABLE')
        self.assertEqual(task.code(net.RateLimited(10),'search'),'RATE_LIMITED');self.assertEqual(task.code(net.Failure('SEGMENT_LIMIT'),'download'),'UNSUPPORTED_PLAYLIST');self.assertEqual(task.code(net.Failure('INCOMPLETE_SOURCE'),'download'),'INCOMPLETE_SOURCE')

class ServerTests(unittest.TestCase):
    def setUp(self):
        self.dir=tempfile.mkdtemp();self.addCleanup(shutil.rmtree,self.dir,True);self.worker=server.Worker({'spool':self.dir,'coreUid':os.getuid()})
        patches=[mock.patch.object(server,'ADMIT_FREE',0),mock.patch.object(server,'FREE_MIN',0)]
        for p in patches:p.start();self.addCleanup(p.stop)
        # Replace the task process with a controllable sleeper; the protocol logic is what is under test.
        self.spawned=[]
        def spawn(task):
            for name in ('result.json','progress.json'):(task['directory']/name).unlink(missing_ok=True)
            p=subprocess.Popen(['sleep','30'],start_new_session=True);self.spawned.append(p);self.addCleanup(self.kill,p)
            task.update(process=p,state='running',reaped=False,lease=time.monotonic()+60,deadline=time.monotonic()+60);task.pop('error',None)
        self.worker.spawn=spawn
    def kill(self,p):
        if p.poll() is None:os.killpg(p.pid,signal.SIGKILL);p.wait()
    def start(self,id='a'*32,action='download',payload=None):
        return self.worker.call({'op':'start','taskId':id,'action':action,'payload':payload or dict(PAYLOAD)})
    def test_protocol_rejects_urls_paths_and_unknown_fields(self):
        bad=[dict(PAYLOAD,providerSeriesId='https://evil/x-1'),dict(PAYLOAD,episodeId='../../etc'),dict(PAYLOAD,number='1e3'),dict(PAYLOAD,quality='9999999p'),dict(PAYLOAD,subtitles=['xx']),dict(PAYLOAD,url='https://x')]
        for payload in bad:
            with self.subTest(payload=payload),self.assertRaises(ValueError):self.start(payload=payload)
        for args in [{'op':'start','taskId':'../x','action':'search','payload':{'query':'a','page':1}},{'op':'start','taskId':'b'*32,'action':'shell','payload':{}},{'op':'start','taskId':'b'*32,'action':'search','payload':{'query':'a','page':1},'extra':1}]:
            with self.subTest(args=args),self.assertRaises(ValueError):self.worker.call(args)
        self.assertEqual(self.worker.call({'op':'poll','taskId':'c'*32})['state'],'absent')
    def test_one_download_and_one_metadata_slot(self):
        self.start();self.start(id='e'*32,action='search',payload={'query':'Frieren','page':1})
        with self.assertRaises(ValueError):self.start(id='b'*32)
        with self.assertRaises(ValueError):self.start(id='f'*32,action='lookup',payload={'anilistId':1,'providerSeriesId':None})
    def test_same_identity_resumes_in_retained_directory_after_cancel_or_restart(self):
        self.start();marker=Path(self.dir)/('a'*32)/'checkpoint.json';marker.write_text('{}')
        self.assertEqual(self.worker.call({'op':'cancel','taskId':'a'*32})['state'],'canceled');self.assertTrue(marker.exists())
        self.assertEqual(self.start()['state'],'running');self.assertEqual(len(self.spawned),2)
        with self.assertRaises(ValueError):self.start(payload=dict(PAYLOAD,quality='best'))
        # A restarted worker has no memory of the task, but the checkpoint directory is reused.
        restarted=server.Worker({'spool':self.dir,'coreUid':os.getuid()});restarted.spawn=self.worker.spawn;self.worker.call({'op':'cancel','taskId':'a'*32})
        restarted.call({'op':'start','taskId':'a'*32,'action':'download','payload':dict(PAYLOAD)});self.assertTrue(marker.exists())
        restarted.call({'op':'cleanup','taskId':'a'*32});self.assertFalse((Path(self.dir)/('a'*32)).exists())
    def test_lease_expiry_and_deadline_kill_process_group(self):
        self.start();t=self.worker.tasks['a'*32];t['lease']=time.monotonic()-1;self.worker.reap()
        self.assertEqual((t['state'],t['error']),('failed','LEASE_EXPIRED'));self.assertIsNotNone(t['process'].poll())
        self.start();t['deadline']=time.monotonic()-1;self.worker.reap();self.assertEqual(t['state'],'failed')
    def test_completed_result_is_read_after_reaping(self):
        self.start();t=self.worker.tasks['a'*32];(t['directory']/'result.json').write_text(json.dumps({'ok':True,'result':{'x':1}}));self.kill(t['process'])
        self.worker.reap();self.assertEqual(self.worker.call({'op':'poll','taskId':'a'*32})['result'],{'x':1})
        self.assertEqual(self.start()['state'],'completed','a completed task is not restarted')
    def test_disk_budget_fails_running_download(self):
        self.start();t=self.worker.tasks['a'*32]
        with mock.patch.object(server,'SPOOL_MAX',-1):self.worker.reap()
        self.assertEqual((t['state'],t['error']),('failed','DISK_BUDGET'))

if __name__=='__main__':unittest.main()
