import os,subprocess,tempfile,unittest,tarfile
from pathlib import Path
from archive_backend.git_engine import GitEngine,safe_path
from archive_backend.contracts import BackendError

class GitEngineTests(unittest.TestCase):
    def test_full_history_export_and_force_push_retention(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'source';source.mkdir()
            def git(*args):return subprocess.check_output(['git','-C',str(source),*args],stderr=subprocess.DEVNULL).decode().strip()
            git('init','-b','custom');git('config','user.email','test@example.invalid');git('config','user.name','Test')
            (source/'hello.txt').write_text('first');git('add','.');git('commit','-m','first');old=git('rev-parse','HEAD')
            git('tag','v1')
            engine=GitEngine(root/'backup',allow_local=True)
            repo={'id':'123','url':str(source),'branch':'custom','owner':'test','name':'source'}
            first=engine.acquire(repo)
            with tarfile.open(root/'backup/generations'/first['artifact']/'files.tar') as t:self.assertEqual(t.extractfile('hello.txt').read(),b'first')
            (source/'hello.txt').write_text('replacement');git('add','.');git('commit','--amend','-m','replacement');git('tag','-d','v1')
            second=engine.acquire(repo)
            self.assertNotEqual(first['commit'],second['commit'])
            mirror=root/'backup/repositories/123/mirror.git'
            refs=engine.command(mirror,'for-each-ref','--format=%(objectname)','refs/backup/').decode()
            self.assertIn(old,refs)
            restore=root/'restore';subprocess.run(['git','clone','--no-hardlinks',str(mirror),str(restore)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            self.assertEqual((restore/'hello.txt').read_text(),'replacement')
    def test_paths(self):
        for p in ('../bad','/etc/passwd','a/../../b','.archive-console/evil','a\\b'):
            with self.assertRaises(BackendError):safe_path(p)
    def test_pinned_github_submodule_is_exported(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'source';sub=root/'sub';source.mkdir();sub.mkdir()
            def git(p,*args):return subprocess.check_output(['git','-C',str(p),*args],stderr=subprocess.DEVNULL).decode().strip()
            for p in (source,sub):
                git(p,'init','-b','main');git(p,'config','user.email','test@example.invalid');git(p,'config','user.name','Test')
            (sub/'child.txt').write_text('pinned child');git(sub,'add','.');git(sub,'commit','-m','child');oid=git(sub,'rev-parse','HEAD')
            (source/'.gitmodules').write_text('[submodule "child"]\n path = child\n url = https://github.com/test/child.git\n')
            git(source,'add','.gitmodules');git(source,'update-index','--add','--cacheinfo','160000,'+oid+',child');git(source,'commit','-m','parent')
            class Engine(GitEngine):
                def command(self,repo,*args,**kwargs):
                    if args[:3]==('remote','add','origin') and args[-1]=='https://github.com/test/child.git':args=(*args[:-1],str(sub))
                    return super().command(repo,*args,**kwargs)
            engine=Engine(root/'backup',allow_local=True)
            result=engine.acquire({'id':'1','url':str(source),'branch':'main','owner':'test','name':'source'})
            self.assertEqual(result['coverage'],[])
            with tarfile.open(root/'backup/generations'/result['artifact']/'files.tar') as t:
                self.assertEqual(t.extractfile('child/child.txt').read(),b'pinned child')
