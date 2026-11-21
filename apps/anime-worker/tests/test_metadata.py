import json,os,tempfile,time,unittest
from unittest import mock
from archive_anime_worker import anilist,anipm,hls,metadata,net

CATALOG={'anilistId':154587,'malId':52991,'title':'Frieren','altTitles':['Sousou no Frieren'],'year':2023,'format':'TV','status':'FINISHED','episodes':28,'hasCover':True}
def item(route,anilist_id,mal,source='settlar'):
    return {'source':source,'routeId':route,'title':route,'anilistId':str(anilist_id) if anilist_id else None,'malId':mal,'type':'TV','year':2023,'episodeCount':28}

class Network:
    """Route exact URLs to canned bodies; anything else is a test failure."""
    def __init__(self,routes):self.routes=routes;self.seen=[]
    def __call__(self,url,*,limit,data=None,headers=None,timeout=30,opener=None):
        net.check(url);self.seen.append(url)
        for prefix,value in self.routes.items():
            if url.startswith(prefix):
                if isinstance(value,Exception):raise value
                body=value if isinstance(value,bytes) else json.dumps(value).encode()
                if len(body)>limit:raise net.Failure('RESPONSE_TOO_LARGE')
                return 200,{},body
        raise AssertionError('unexpected URL '+url)

class NetTests(unittest.TestCase):
    def test_allowlist_and_url_shape(self):
        for url in ['https://ani.pm/api','https://media.settlar.io/v1/object/v3.x','https://graphql.anilist.co']:net.check(url)
        for url in ['http://ani.pm/','https://ani.pm.evil.com/','https://user:pw@ani.pm/','https://ani.pm:8443/','file:///etc/passwd','https://127.0.0.1/','https://ani.pm/#frag','//ani.pm/x',None]:
            with self.subTest(url=url),self.assertRaises(net.Failure):net.check(url)
    def test_private_resolution_refused(self):
        with mock.patch('socket.getaddrinfo',return_value=[(2,1,6,'',('10.0.0.5',443))]),self.assertRaises(net.Failure):net.public_address('ani.pm')
        with mock.patch('socket.getaddrinfo',return_value=[(10,1,6,'',('::1',443,0,0))]),self.assertRaises(net.Failure):net.public_address('ani.pm')
        # Mocked public-address classification only; no connection is made.
        with mock.patch('socket.getaddrinfo',return_value=[(2,1,6,'',('104.21.1.1',443))]):net.public_address('ani.pm')
    def test_opener_has_no_plain_http_file_or_proxy_handlers(self):
        names={type(h).__name__ for h in net._opener.handlers}
        self.assertNotIn('HTTPHandler',names);self.assertNotIn('FileHandler',names);self.assertNotIn('ProxyHandler',names);self.assertIn('HTTPSHandler',names)

class FetchTests(unittest.TestCase):
    """net.fetch with a fake opener: status mapping, body bounds and redirect validation."""
    class Response:
        def __init__(self,body):self.body=body;self.status=200;self.headers={}
        def read(self,n):return self.body[:n]
        def __enter__(self):return self
        def __exit__(self,*a):return False
    class Opener:
        def __init__(self,result):self.result=result
        def open(self,request,timeout):
            if isinstance(self.result,Exception):raise self.result
            return FetchTests.Response(self.result)
    def setUp(self):
        p=mock.patch.object(net,'public_address',lambda host:None);p.start();self.addCleanup(p.stop)
    def test_body_limit_and_status_mapping(self):
        import urllib.error,email.message
        self.assertEqual(net.fetch('https://ani.pm/x',limit=4,opener=self.Opener(b'1234'))[2],b'1234')
        with self.assertRaises(net.Failure) as caught:net.fetch('https://ani.pm/x',limit=4,opener=self.Opener(b'12345'))
        self.assertEqual(str(caught.exception),'RESPONSE_TOO_LARGE')
        headers=email.message.Message();headers['Retry-After']='42'
        with self.assertRaises(net.RateLimited) as limited:net.fetch('https://ani.pm/x',limit=4,opener=self.Opener(urllib.error.HTTPError('https://ani.pm/x',429,'',headers,None)))
        self.assertEqual(limited.exception.retry_after,42)
        for code,expected in [(404,'NOT_FOUND'),(410,'NOT_FOUND'),(500,'SOURCE_UNAVAILABLE'),(403,'SOURCE_UNAVAILABLE')]:
            with self.subTest(code=code),self.assertRaises(net.Failure) as caught:net.fetch('https://ani.pm/x',limit=4,opener=self.Opener(urllib.error.HTTPError('https://ani.pm/x',code,'',email.message.Message(),None)))
            self.assertEqual(str(caught.exception),expected)
        with self.assertRaises(net.Failure):net.fetch('https://ani.pm/x',limit=4,opener=self.Opener(OSError('reset')))
    def test_redirects_are_validated(self):
        import urllib.request
        handler=net.Redirects();request=urllib.request.Request('https://ani.pm/x')
        for target in ['https://evil.example/x','http://ani.pm/x','https://127.0.0.1/x','file:///etc/passwd']:
            with self.subTest(target=target),self.assertRaises(net.Failure):handler.redirect_request(request,None,302,'',{},target)
        self.assertIsNotNone(handler.redirect_request(request,None,302,'',{},'https://media.settlar.io/v1/object/x'))

class AniListTests(unittest.TestCase):
    def test_description_is_plain_text(self):
        self.assertEqual(anilist.plain_text('A <i>mage</i>&amp;friend.<br><br>Second <script>x</script>line'),'A mage&friend.\nSecond xline')
        self.assertIsNone(anilist.plain_text(None));self.assertLessEqual(len(anilist.plain_text('x'*9000)),6000)
    def test_catalog_normalization_prefers_english_and_bounds_fields(self):
        value=anilist.catalog({'id':1,'idMal':2,'title':{'romaji':'Romaji','english':'English','native':'ネイティブ','userPreferred':'Romaji'},'synonyms':['A']*20,'seasonYear':None,'startDate':{'year':2001},'format':'WEIRD','status':'FINISHED','episodes':-1},True)
        self.assertEqual((value['title'],value['year'],value['format'],value['episodes']),('English',2001,None,None));self.assertEqual(value['altTitles'],['Romaji','ネイティブ','A'])
    def test_cover_requires_raster_signature(self):
        with mock.patch.object(net,'fetch',return_value=(200,{},b'<svg onload=alert(1)>')):self.assertIsNone(anilist.cover('https://s4.anilist.co/x.jpg',1000))
        with mock.patch.object(net,'fetch',return_value=(200,{},b'\xff\xd8\xff\xe0jpeg')):self.assertTrue(anilist.cover('https://s4.anilist.co/x.jpg',1000).startswith('data:image/jpeg;base64,'))
        self.assertIsNone(anilist.cover('https://evil.example/x.jpg',1000))
    def test_rate_limit_gate_is_shared_and_honors_retry_after(self):
        with tempfile.TemporaryDirectory() as d,mock.patch.dict(os.environ,{'ARCHIVE_ANIME_SPOOL':d}):
            with mock.patch.object(net,'fetch',side_effect=net.RateLimited(30)),self.assertRaises(net.RateLimited):anilist.query('q',{})
            with mock.patch.object(net,'fetch') as fetch,self.assertRaises(net.RateLimited):anilist.query('q',{})
            fetch.assert_not_called()
            anilist.gate_set(-1)
            with mock.patch.object(net,'fetch',return_value=(200,{'X-RateLimit-Remaining':'50'},b'{"data":{"Page":{}}}')):self.assertEqual(anilist.query('q',{}),{'Page':{}})
    def test_graphql_errors_are_unavailable(self):
        with tempfile.TemporaryDirectory() as d,mock.patch.dict(os.environ,{'ARCHIVE_ANIME_SPOOL':d}),mock.patch.object(net,'fetch',return_value=(200,{},b'{"errors":[{"message":"x"}],"data":null}')):
            with self.assertRaises(net.Failure):anilist.query('q',{})

class MatchingTests(unittest.TestCase):
    def test_identity_evidence_and_ambiguity(self):
        found=[anipm.candidate(item('frieren-6351',154587,52991),CATALOG),anipm.candidate(item('frieren-s2-8384',182255,59978),CATALOG)]
        self.assertEqual([c['evidence'] for c in found],['anilist-id','title']);self.assertEqual(anipm.automatic(found),'settlar/frieren-6351')
        # A synthetic provider AniList ID still matches through MAL identity.
        synthetic=anipm.candidate(item('last-attack-7624',900059571,52991),CATALOG);self.assertEqual(synthetic['evidence'],'mal-id');self.assertEqual(anipm.automatic([synthetic]),'settlar/last-attack-7624')
        twice=[anipm.candidate(item('a-1',154587,None),CATALOG),anipm.candidate(item('b-2',154587,None),CATALOG)];self.assertIsNone(anipm.automatic(twice))
        self.assertIsNone(anipm.automatic([found[1]]))
    def test_invalid_routes_are_dropped(self):
        for route in ['../x-1','Frieren-1','frieren','frieren-1/../2','']:self.assertIsNone(anipm.candidate(item(route,1,1),CATALOG))
        self.assertIsNone(anipm.candidate(item('frieren-1',1,1,source='Bad/Src'),CATALOG))
    def test_episode_inventory_filters_bad_and_repeated_entries(self):
        data={'episodes':[{'routeId':'e1.a','number':1,'title':' ','sub':True,'dub':False},{'routeId':'e1.b','number':1.0,'title':'dup','sub':True,'dub':True},
                          {'routeId':'e1.c','number':12.5,'title':'Recap','sub':True,'dub':'yes'},{'routeId':'bad/../id','number':3,'sub':True,'dub':True},{'routeId':'e1.d','number':-1,'sub':True,'dub':True}]}
        self.assertEqual(anipm.episodes(data),[{'id':'e1.a','number':1,'title':None,'sub':True,'dub':False},{'id':'e1.c','number':12.5,'title':'Recap','sub':True,'dub':False}])
        self.assertEqual([anipm.number_text(n) for n in (1,12.5,3.0,12.25)],['1','12.5','3','12.25'])
    def test_session_rejects_other_language_and_foreign_hosts(self):
        boot={'effectiveLanguage':'sub','settlarSelection':'2.sel'}
        routes={'https://ani.pm/api/anime/playback-bootstrap/settlar/frieren-6351?ep=1&lang=dub':boot}
        with mock.patch.object(net,'fetch',Network(routes)),self.assertRaises(net.Failure) as caught:anipm.session('settlar/frieren-6351',1,'dub')
        self.assertEqual(str(caught.exception),'VARIANT_UNAVAILABLE')
        routes={'https://ani.pm/api/anime/playback-bootstrap/':boot,'https://ani.pm/api/anime/settlar/preview-session':{'embedUrl':'https://evil.example/e?t=abc'}}
        with mock.patch.object(net,'fetch',Network(routes)),self.assertRaises(net.Failure):anipm.session('settlar/frieren-6351',1,'sub')
        routes={'https://ani.pm/api/anime/playback-bootstrap/':boot,'https://ani.pm/api/anime/settlar/preview-session':{'embedUrl':'https://embed.settlar.io/e?t=abc'},
                'https://embed.settlar.io/api/embed/session':{'kind':'hls','source':'https://media.settlar.io/v1/object/m','audioLang':'en','subtitles':[{'srclang':'en','url':'https://media.settlar.io/s'},{'srclang':'xx','url':'https://media.settlar.io/x'},{'srclang':'fr','url':'https://evil.example/f'}]}}
        with mock.patch.object(net,'fetch',Network(routes)),self.assertRaises(net.Failure):anipm.session('settlar/frieren-6351',1,'sub')
        routes['https://ani.pm/api/anime/playback-bootstrap/']={'effectiveLanguage':'dub','settlarSelection':'2.sel'}
        with mock.patch.object(net,'fetch',Network(routes)):value=anipm.session('settlar/frieren-6351',1,'dub')
        self.assertEqual(anipm.session_subtitles(value),{'en':'https://media.settlar.io/s'})
    def test_explicit_series_must_match_source_and_id(self):
        with mock.patch.object(net,'fetch',Network({'https://ani.pm/api/anime/series/6351':{'id':6351,'source':'anilist','title':'x','episodes':[]}})),self.assertRaises(net.Failure):anipm.series('settlar/frieren-6351')
        with mock.patch.object(net,'fetch',Network({'https://ani.pm/api/anime/series/6351':{'id':6351,'source':'settlar','title':'Frieren','anilistId':'154587','malId':52991,'episodes':[]}})):
            chosen,_=anipm.explicit('settlar/anything-6351',CATALOG)
        self.assertEqual((chosen['providerSeriesId'],chosen['evidence']),('settlar/anything-6351','anilist-id'))

MASTER=b'#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="s",NAME="English",LANGUAGE="eng",URI="https://media.settlar.io/s"\n#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=1920x1080,CODECS="avc1.640032,mp4a.40.2",SUBTITLES="s"\nhttps://media.settlar.io/v1/object/1080\n#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=1280x720,CODECS="avc1.64001f,mp4a.40.2"\n720.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=1,RESOLUTION=1280x720,CODECS="hvc1.1,mp4a.40.2"\nhevc\n'
MEDIA=b'#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXTINF:10,\nhttps://media.settlar.io/v1/object/s1\n#EXTINF:4.5,\ns2\n#EXT-X-ENDLIST\n'
class PlaylistTests(unittest.TestCase):
    def fetch(self,body):return mock.patch.object(net,'fetch',return_value=(200,{},body))
    def test_variants_and_selection(self):
        with self.fetch(MASTER):found=hls.variants('https://media.settlar.io/v1/object/master')
        self.assertEqual([(v['height'],v['url']) for v in found],[(1080,'https://media.settlar.io/v1/object/1080'),(720,'https://media.settlar.io/v1/object/720.m3u8')])
        self.assertEqual(hls.select(found,'best')['height'],1080);self.assertEqual(hls.select(found,'720p')['height'],720)
        with self.assertRaises(net.Failure):hls.select(found,'480p')
    def test_unsupported_master_features_rejected(self):
        for extra in [b'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",NAME="en",URI="a"\n',b'#EXT-X-SESSION-KEY:METHOD=AES-128,URI="k"\n',b'#EXT-X-NEW-THING:1\n']:
            with self.subTest(extra=extra),self.fetch(MASTER.replace(b'#EXT-X-VERSION:3\n',b'#EXT-X-VERSION:3\n'+extra)),self.assertRaises(net.Failure):hls.variants('https://media.settlar.io/m')
        with self.fetch(MASTER.replace(b'https://media.settlar.io/v1/object/1080',b'https://evil.example/1080')),self.assertRaises(net.Failure):hls.variants('https://media.settlar.io/m')
        with self.fetch(b'not a playlist'),self.assertRaises(net.Failure):hls.variants('https://media.settlar.io/m')
    def test_media_identity_and_rejections(self):
        with self.fetch(MEDIA):segments,duration,identity=hls.media('https://media.settlar.io/v1/object/media')
        self.assertEqual(segments,['https://media.settlar.io/v1/object/s1','https://media.settlar.io/v1/object/s2']);self.assertEqual(duration,14.5);self.assertEqual(len(identity),64)
        for change in [(b'#EXT-X-ENDLIST\n',b''),(b'#EXTINF:4.5,\n',b'#EXT-X-KEY:METHOD=AES-128,URI="k"\n#EXTINF:4.5,\n'),(b'#EXTINF:4.5,\n',b'#EXT-X-BYTERANGE:100@0\n#EXTINF:4.5,\n'),
                       (b'#EXTINF:4.5,\n',b'#EXT-X-MAP:URI="init.mp4"\n#EXTINF:4.5,\n'),(b'#EXTINF:4.5,\n',b'#EXT-X-DISCONTINUITY\n#EXTINF:4.5,\n'),(b's2',b'file:///etc/passwd'),(b'#EXTINF:10,',b'#EXTINF:nan,')]:
            with self.subTest(change=change),self.fetch(MEDIA.replace(*change)),self.assertRaises(net.Failure):hls.media('https://media.settlar.io/v1/object/media')

class LookupTests(unittest.TestCase):
    def test_lookup_without_identity_match_returns_candidates_only(self):
        details=(dict(CATALOG),'desc',[],[],None)
        with mock.patch.object(anilist,'details',return_value=details),mock.patch.object(anipm,'search',return_value=[item('frieren-s2-8384',182255,59978)]):
            result=metadata.lookup({'anilistId':154587,'providerSeriesId':None})
        self.assertIsNone(result['match']);self.assertEqual(result['episodes'],[]);self.assertEqual(len(result['candidates']),1);self.assertEqual(result['samples'],{'sub':None,'dub':None})
    def test_failed_explicit_choice_falls_back_to_identity_match(self):
        details=(dict(CATALOG),'desc',[],[],None)
        with mock.patch.object(anilist,'details',return_value=details),mock.patch.object(anipm,'search',return_value=[item('frieren-6351',154587,52991)]),\
             mock.patch.object(anipm,'explicit',side_effect=net.Failure('NOT_FOUND')),mock.patch.object(anipm,'series',return_value={'episodes':[]}):
            result=metadata.lookup({'anilistId':154587,'providerSeriesId':'settlar/gone-99'})
        self.assertEqual(result['match'],'settlar/frieren-6351')

if __name__=='__main__':unittest.main()
