import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch,MagicMock
from archive_music_worker import lyrics

PLAIN='First invented line\nSecond invented line'
SYNC='[00:00.00] First invented line\n[00:02.50] Second invented line'

class LyricsTests(unittest.TestCase):
    def lookup(self,**overrides):
        value={'id':1,'trackName':'Fixture','artistName':'Artist','albumName':'Album','duration':10,'instrumental':False,'plainLyrics':PLAIN,'syncedLyrics':SYNC}
        value.update(overrides)
        opener=MagicMock();opener.open.return_value.__enter__.return_value.read.return_value=json.dumps(value).encode()
        with patch.object(lyrics.urllib.request,'build_opener',return_value=opener):
            result=lyrics.lookup('Fixture',['Artist'],'Album',10)
        self.assertTrue(opener.open.call_args.args[0].full_url.startswith('https://lrclib.net/api/get?'))
        return result
    def test_exact_match_returns_plain_and_synced(self):
        result=self.lookup();self.assertEqual(result['status'],'found');self.assertEqual(result['synced'],SYNC)
    def test_wrong_recording_and_instrumental_are_not_guessed(self):
        for change in ({'duration':20},{'artistName':'Someone else'},{'albumName':'Live album'},{'trackName':'Different song'}):
            self.assertEqual(self.lookup(**change)['status'],'missing')
        self.assertEqual(self.lookup(instrumental=True)['status'],'instrumental')
    def test_bad_timestamps_fall_back_to_plain(self):
        self.assertIsNone(self.lookup(syncedLyrics='[99:00.00] Outside recording')['synced'])
        self.assertEqual(self.lookup(syncedLyrics='bad time')['plain'],PLAIN)
    def test_oversized_response_and_network_errors_are_optional(self):
        opener=MagicMock();opener.open.side_effect=TimeoutError()
        with patch.object(lyrics.urllib.request,'build_opener',return_value=opener):
            self.assertEqual(lyrics.lookup('Fixture',['Artist'],'Album',10)['status'],'failed')
        self.assertEqual(self.lookup(plainLyrics='x'*65537)['status'],'failed')
    def test_embedding_preserves_existing_art_and_adds_lyrics(self):
        from mutagen.id3 import ID3,APIC
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'audio.mp3'
            import subprocess
            subprocess.run(['/usr/bin/ffmpeg','-v','error','-f','lavfi','-i','anullsrc=r=44100:cl=stereo','-t','10','-q:a','9',str(path)],check=True)
            tags=ID3(path);tags.add(APIC(encoding=3,mime='image/jpeg',type=3,data=b'fixture cover'));tags.save(path)
            result=lyrics.embed(path,self.lookup(),10)
            saved=ID3(path)
            self.assertTrue(result['embedded']);self.assertEqual(saved.getall('USLT')[0].text,PLAIN)
            self.assertEqual(saved.getall('SYLT')[0].text[1][1],2500);self.assertEqual(saved.getall('APIC')[0].data,b'fixture cover')
    def test_failed_embedding_retains_original_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'audio.mp3';path.write_bytes(b'untagged fixture')
            result=lyrics.embed(path,self.lookup(),10)
            self.assertFalse(result['embedded']);self.assertEqual(path.read_bytes(),b'untagged fixture')
            self.assertFalse((path.parent/'lyrics-tagged.mp3').exists())
    def test_optional_lookup_failure_does_not_fail_audio_download(self):
        from archive_music_worker import provider
        song=MagicMock();song.name='Fixture';song.artists=['Artist'];song.album_name='Album';song.duration=10
        artifact={'sha256':'a'*64,'sizeBytes':'100','durationMs':10000}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(provider,'client'),patch('spotdl.types.song.Song.from_url',return_value=song),patch('spotdl.download.downloader.Downloader') as downloader,patch('archive_music_worker.media.verify',return_value=artifact),patch.object(lyrics,'lookup',side_effect=RuntimeError('provider unavailable')):
                downloader.return_value.download_song.return_value=(song,Path(directory)/'audio.mp3')
                result=provider.download({'trackId':'a'*22,'durationMs':10000},directory,lambda stage:None)
                self.assertEqual(result['sha256'],'a'*64);self.assertEqual(result['lyrics']['status'],'failed')
