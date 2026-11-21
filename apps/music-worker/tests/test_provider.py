import unittest
from unittest.mock import patch
from archive_music_worker import provider

class AlbumTests(unittest.TestCase):
    def test_multi_page_album_keeps_disc_order_and_unavailable_slots(self):
        class Spotify:
            def album(self,id):return {'id':id,'name':'Album','total_tracks':3,'artists':[{'id':'a'*22,'name':'Artist'}]}
            def album_tracks(self,id,limit):return {'items':[{'id':'b'*22,'name':'One','disc_number':1,'track_number':1},None],'next':'page2'}
            def next(self,page):return {'items':[{'id':'c'*22,'name':'Three','disc_number':2,'track_number':1}],'next':None}
        with patch.object(provider,'client',return_value=Spotify()):r=provider.resolve({'selection':{'kind':'album','id':'d'*22}})
        self.assertEqual(r['expectedTracks'],3);self.assertFalse(r['tracks'][1]['available']);self.assertEqual(r['tracks'][2]['disc'],2)
    def test_incomplete_album_is_rejected(self):
        class Spotify:
            def album(self,id):return {'id':id,'name':'Album','total_tracks':2}
            def album_tracks(self,id,limit):return {'items':[{'id':'b'*22}],'next':None}
        with patch.object(provider,'client',return_value=Spotify()),self.assertRaises(ValueError):provider.resolve({'selection':{'kind':'album','id':'d'*22}})
    def test_album_search_uses_album_results_and_real_offset(self):
        response={'data':{'searchV2':{'albumsV2':{'items':[{'data':{'__typename':'Album','uri':'spotify:album:'+'a'*22,'name':'Album','artists':{'items':[{'profile':{'name':'Artist'}}]},'date':{'year':2020}}}],'totalCount':50}}}}
        with patch('spotapi.song.Song.query_songs',return_value=response) as query:r=provider.search({'query':'Album','kind':'album','offset':20})
        query.assert_called_once_with('Album',limit=20,offset=20);self.assertEqual(r['nextOffset'],40);self.assertEqual(r['items'][0]['kind'],'album')
    def test_fixed_spool_template_is_a_file_not_a_spotdl_directory(self):
        from unittest.mock import MagicMock
        from spotdl.utils.formatter import create_file_name
        from pathlib import Path
        song=MagicMock();song.artists=['Fixture Artist'];song.name='Fixture Song';song.title='Fixture Song';song.album_name='Fixture Album'
        # This regression caused successful downloads to be rejected: literal
        # audio.mp3 is interpreted by spotDL as a directory, not a filename.
        self.assertEqual(create_file_name(song,'/spool/attempt/audio.{output-ext}','mp3'),Path('/spool/attempt/audio.mp3'))

class ArtistTests(unittest.TestCase):
    def group(self, identity):
        return {'releases':{'items':[{'id':identity,'name':'Album','date':{'year':2020},'tracks':{'totalCount':2},'type':'ALBUM'}]}}
    def test_artist_uses_every_page_and_primary_editions(self):
        from archive_music_worker.discography import resolve_artist
        groups=[self.group(str(i).zfill(22)) for i in range(51)]
        groups[0]['releases']['items'].append(dict(groups[0]['releases']['items'][0],id='z'*22))
        def page(identity, **args):return {'data':{'artistUnion':{'discography':{'all':{'items':groups[args['offset']:args['offset']+50],'totalCount':51}}}}}
        with patch('archive_music_worker.discography.Artist') as cls:
            cls.return_value.get_artist.return_value={'data':{'artistUnion':{'id':'a'*22,'profile':{'name':'Artist'}}}}
            cls.return_value.get_artist_discography.side_effect=page
            result=resolve_artist('a'*22)
        self.assertEqual(len(result['releases']),51);self.assertEqual(result['expectedTracks'],102)
        self.assertNotIn('z'*22,[r['id'] for r in result['releases']])
        self.assertEqual(cls.return_value.get_artist_discography.call_args_list[1].kwargs['offset'],50)
    def test_missing_and_repeated_artist_pages_fail_closed(self):
        from archive_music_worker.discography import resolve_artist
        for groups in ([],[self.group('b'*22)]*2):
            with patch('archive_music_worker.discography.Artist') as cls:
                cls.return_value.get_artist.return_value={'data':{'artistUnion':{'id':'a'*22,'profile':{'name':'Artist'}}}}
                cls.return_value.get_artist_discography.return_value={'data':{'artistUnion':{'discography':{'all':{'items':groups,'totalCount':2}}}}}
                with self.assertRaises(ValueError):resolve_artist('a'*22)
    def test_artist_search_uses_artist_endpoint(self):
        from archive_music_worker.discography import search_artists
        with patch('archive_music_worker.discography.Artist') as cls:
            cls.return_value.query_artists.return_value={'data':{'searchV2':{'artists':{'items':[{'data':{'__typename':'Artist','uri':'spotify:artist:'+'a'*22,'profile':{'name':'Artist'}}}],'totalCount':60}}}}
            result=search_artists('Artist',20)
        cls.return_value.query_artists.assert_called_once_with('Artist',limit=20,offset=20)
        self.assertEqual(result['items'][0]['kind'],'artist');self.assertEqual(result['nextOffset'],40)
