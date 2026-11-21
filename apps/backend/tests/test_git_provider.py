import io,json,unittest,urllib.error
from archive_backend.git_provider import GitHub,repo_record
from archive_backend.contracts import BackendError

class Response(io.BytesIO):
    def __init__(self,data,link=''):super().__init__(json.dumps(data).encode());self.headers={'Link':link}

def repo(i,name='example'):
    return {'id':i,'owner':{'login':'owner'},'name':name,'default_branch':'feature/current','private':True}

class DiscoveryTests(unittest.TestCase):
    def test_pages_identity_dedup_and_branch(self):
        responses=[Response({'login':'owner'}),Response([repo(1)],'<https://api.github.com/user/repos?page=2>; rel="next"'),Response([repo(1),repo(2,'new')])]
        result=GitHub('secret',request=lambda *a,**k:responses.pop(0)).discover()
        self.assertEqual(len(result['repositories']),2)
        self.assertEqual(result['repositories'][0]['branch'],'feature/current')
    def test_untrusted_next_page_rejected(self):
        responses=[Response({'login':'owner'}),Response([repo(1)],'<https://evil.invalid/>; rel="next"')]
        with self.assertRaises(BackendError):GitHub('secret',request=lambda *a,**k:responses.pop(0)).discover()
    def test_invalid_identity(self):
        with self.assertRaises(BackendError):repo_record(repo(1,'../outside'))
    def test_partial_listing_never_returns_inventory(self):
        responses=[Response({'login':'owner'}),Response([repo(1)],'<https://api.github.com/user/repos?page=2>; rel="next"')]
        def request(*a,**k):
            if responses:return responses.pop(0)
            raise OSError('network unavailable')
        with self.assertRaises(BackendError):GitHub('secret',request=request,sleep=lambda _:None).discover()
    def test_controlled_excludes_read_only_collaborations(self):
        writable=repo(2);writable['owner']['login']='org';writable['permissions']={'push':True}
        readonly=repo(3);readonly['owner']['login']='other'
        responses=[Response({'login':'owner'}),Response([repo(1),writable,readonly])]
        result=GitHub('secret',request=lambda *a,**k:responses.pop(0)).discover('controlled')
        self.assertEqual({r['id'] for r in result['repositories']},{'1','2'})
