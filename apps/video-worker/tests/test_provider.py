import tempfile,unittest,subprocess
from pathlib import Path
from archive_video_worker.provider import choices,validate_info,verify
from archive_video_worker.policy import canonical_url
class ProviderTests(unittest.TestCase):
    def info(self):return {'id':'BaW_jenozKc','title':'fixture','extractor_key':'Youtube','duration':1,'formats':[{'vcodec':'avc1','acodec':'mp4a','height':720}]}
    def test_compatible_choices_only(self):
        i=self.info();i['formats']+=[{'vcodec':'vp9','acodec':'opus','height':1080}];self.assertEqual([c['id'] for c in choices(i)],['video-720','audio'])
    def test_twitter_hls_unspecified_audio_codec_is_verified_after_merge(self):
        i=self.info();i['formats']=[{'format_id':'hls-893','vcodec':'avc1.64001F','acodec':'none','height':720,'ext':'mp4'},{'format_id':'hls-audio','vcodec':'none','acodec':None,'ext':'mp4'}]
        self.assertEqual([c['id'] for c in choices(i)],['video-720','audio'])
    def test_multi_live_drm_rejected(self):
        for delta in [{'_type':'playlist','entries':[]},{'is_live':True},{'has_drm':True},{'duration':float('nan')},{'extractor_key':'Generic'}]:
            with self.subTest(delta=delta),self.assertRaises(ValueError):validate_info(dict(self.info(),**delta),'https://www.youtube.com/watch?v=BaW_jenozKc')
    def test_source_identity_change_rejected(self):
        with self.assertRaises(ValueError):validate_info(self.info(),'https://www.youtube.com/watch?v=aaaaaaaaaaa')
    def test_real_media_validation(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'sample.mp4'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=size=1280x720:rate=1:duration=1','-f','lavfi','-i','sine=duration=1','-c:v','libx264','-c:a','aac','-shortest',str(p)],check=True)
            verify(p,'video',720,1)
            with self.assertRaises(ValueError):verify(p,'video',1080,1)
            with self.assertRaises(ValueError):verify(p,'audio',0,1)
    def test_url_consistent(self):self.assertEqual(canonical_url('https://x.com/user/status/123'),'https://x.com/i/status/123')

class WorkerTests(unittest.TestCase):
    def test_deadline_kills_and_reaps_process_group(self):
        import os,time,subprocess
        from archive_video_worker.server import Worker
        with tempfile.TemporaryDirectory() as d:
            w=Worker({'spool':d});p=subprocess.Popen(['sleep','30'],start_new_session=True)
            try:
                task={'process':p,'state':'running','lease':time.monotonic()-1,'deadline':time.monotonic()+60,'directory':Path(d)}
                w.tasks['a'*32]=task;w.reap();self.assertIsNotNone(p.poll());self.assertEqual(task['state'],'failed');self.assertTrue(task['reaped'])
            finally:
                if p.poll() is None:os.killpg(p.pid,9);p.wait()
    def test_protocol_rejects_arbitrary_source_and_quality(self):
        from archive_video_worker.server import Worker
        with tempfile.TemporaryDirectory() as d:
            w=Worker({'spool':d})
            for payload in [{'url':'https://127.0.0.1/test'},{'url':'https://youtu.be/BaW_jenozKc','extra':'--exec'}]:
                with self.assertRaises(ValueError):w.call({'op':'start','taskId':'b'*32,'action':'resolve','payload':payload})
