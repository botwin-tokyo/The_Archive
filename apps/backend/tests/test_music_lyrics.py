import hashlib
import unittest
from unittest.mock import patch
from archive_backend.contracts import BackendError
from archive_backend.music_lyrics import validate_result,MusicLyrics
import test_music

RESULT={'status':'found','plain':'Invented fixture line','synced':'[00:00.00] Invented fixture line','sourceId':1,'embedded':True}

class LyricsTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_music.MusicTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.f=self.fixture.f;self.music=self.fixture.music
    def completed_track(self):
        job=self.fixture.enqueue();self.fixture.settle()
        track=self.f.call('music.jobs.get',{'id':job['id']})['tracks'][0]
        identity=hashlib.sha256(('files:'+track['entry']['path']).encode()).hexdigest()
        self.f.state.delete('music_lyrics',identity)
        self.music.lyrics.schedule(self.f.owner,track['id'],track['entry'],RESULT,force=True)
        return track
    def test_sidecar_is_verified_and_audio_is_unchanged(self):
        track=self.completed_track();path=self.f.root/'files'/track['entry']['path'];before=path.read_bytes()
        self.music.lyrics.tick()
        self.assertEqual(path.read_bytes(),before);self.assertEqual(path.with_suffix('.lrc').read_text(),RESULT['synced'])
        self.assertEqual(self.music.lyrics.status()['saved'],1)
    def test_plain_text_fallback_and_restart_reuse_transfer(self):
        track=self.completed_track();identity=hashlib.sha256(('files:'+track['entry']['path']).encode()).hexdigest()
        row=self.f.state.get('music_lyrics',identity);row['_result']=dict(RESULT,synced=None);self.f.state.put('music_lyrics',identity,row)
        self.music.lyrics.tick();saved=self.f.state.get('music_lyrics',identity)
        saved.update(state='publishing',_result=dict(RESULT,synced=None));self.f.state.put('music_lyrics',identity,saved)
        self.music.lyrics=MusicLyrics(self.music);self.music.lyrics.tick()
        path=self.f.root/'files'/track['entry']['path'];self.assertEqual(path.with_suffix('.txt').read_text(),RESULT['plain'])
        self.assertEqual(self.music.lyrics.status()['saved'],1)
    def test_conflict_does_not_overwrite_user_lyrics_or_audio(self):
        track=self.completed_track();path=self.f.root/'files'/track['entry']['path'];path.with_suffix('.lrc').write_text('My own lyrics')
        self.music.lyrics.tick()
        self.assertEqual(path.with_suffix('.lrc').read_text(),'My own lyrics');self.assertEqual(path.read_bytes(),b'fixture audio')
        self.assertEqual(self.music.lyrics.status()['failed'],1)
    def test_changed_audio_prevents_sidecar_publication(self):
        track=self.completed_track();path=self.f.root/'files'/track['entry']['path'];path.write_bytes(b'replaced')
        self.music.lyrics.tick();self.assertFalse(path.with_suffix('.lrc').exists())
        self.assertEqual(self.music.lyrics.status()['failed'],1)
    def test_backfill_is_idempotent_and_never_starts_audio_downloads(self):
        self.fixture.enqueue();self.fixture.settle();starts=self.fixture.worker.starts
        args={'requestId':'backfill'}
        self.f.call('music.lyrics.backfill',args);run=self.f.state.get('music_lyrics_runs','backfill')
        self.f.call('music.lyrics.backfill',args);self.assertEqual(self.f.state.get('music_lyrics_runs','backfill')['id'],run['id'])
        self.music.lyrics.tick();self.assertFalse(self.music.lyrics.status()['scanning'])
        self.assertLessEqual(self.fixture.worker.starts-starts,1)
        self.assertEqual(len(list((self.f.root/'files').rglob('*.mp3'))),2)
    def test_invalid_payload_is_rejected(self):
        for change in ({'plain':'x'*65537},{'plain':'bad\0text'},{'status':'made-up'}):
            with self.assertRaises(BackendError):validate_result(dict(RESULT,**change))
    def test_lyrics_tools_are_owner_only(self):
        secret='lyrics-test-'+'x'*40
        self.f.service.auth.register(self.f.owner,'agent',secret,['read','write'],[('files','')])
        for capability,args in (('music.lyrics.status',{}),('music.lyrics.backfill',{'requestId':'denied'})):
            with self.assertRaises(BackendError):self.f.call(capability,args,token=secret)
    def test_new_download_result_automatically_publishes_sidecars(self):
        original=self.fixture.worker.call
        def worker(**args):
            result=original(**args)
            if args.get('action')=='download': result['result']['lyrics']=dict(RESULT)
            return result
        with patch.object(self.fixture.worker,'call',side_effect=worker):
            job=self.fixture.enqueue();self.fixture.settle()
        self.assertEqual(self.f.call('music.jobs.get',{'id':job['id']})['state'],'completed')
        self.assertEqual(len(list((self.f.root/'files').rglob('*.lrc'))),2)
        self.assertEqual(self.music.lyrics.status()['saved'],2)
        self.assertEqual(self.f.state.list('music_lyric_results'),[])
