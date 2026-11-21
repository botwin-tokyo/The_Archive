"""Git acquisition without checkout hooks, shell execution, or remote writes."""
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import tarfile
import time
import uuid
from .contracts import BackendError


def atomic_json(path, value):
    path=Path(path); tmp=path.with_name(path.name+'.tmp-'+uuid.uuid4().hex)
    with tmp.open('x') as f:json.dump(value,f);f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)
    fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def safe_path(name):
    p=PurePosixPath(name)
    if p.is_absolute() or not name or '\\' in name or '\0' in name or any(x in ('','.','..') or x.startswith('.archive-') or len(x.encode())>255 for x in name.split('/')) or len(name.encode())>4095:
        raise BackendError('INVALID_PATH','Repository contains a path unsupported by archive storage')
    return name


class GitEngine:
    def __init__(self, root, *, secret=None, cancel=None, verify=lambda:None, allow_local=False):
        self.root=Path(root);self.secret=secret;self.cancel=cancel;self.verify=verify;self.allow_local=allow_local
        self.env={'PATH':'/usr/bin:/bin','HOME':str(self.root),'LANG':'C.UTF-8','GIT_TERMINAL_PROMPT':'0',
                  'PYTHONPATH':str(Path(__file__).resolve().parents[1]),'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null','GIT_LFS_SKIP_SMUDGE':'1'}
        if secret:
            self.env.update(GIT_ASKPASS=str(Path(__file__).with_name('git_askpass.py')),ARCHIVE_GIT_SECRET=str(secret))

    def command(self, repo, *args, output=None, timeout=900):
        self.verify()
        if self.cancel and self.cancel.is_set():raise BackendError('ABORTED','Git backup canceled')
        command=['/usr/bin/git','-c','core.hooksPath=/dev/null','-c','credential.helper=',
                 '-c','protocol.file.allow='+('always' if self.allow_local else 'never'),
                 '-c','protocol.ext.allow=never','-c','http.followRedirects=false','-c','gc.auto=0']
        if repo:command+=['--git-dir='+str(repo)]
        command+=list(args)
        import tempfile
        with tempfile.TemporaryFile() as errors, tempfile.TemporaryFile() as result:
            process=subprocess.Popen(command,env=self.env,stdin=subprocess.DEVNULL,stdout=output or result,stderr=errors,start_new_session=True)
            deadline=time.monotonic()+timeout
            try:
                while process.poll() is None:
                    if time.monotonic()>deadline or (self.cancel and self.cancel.is_set()):
                        raise BackendError('INTERRUPTED','Git command canceled or deadline reached')
                    time.sleep(.05)
                if process.returncode:raise BackendError('UNAVAILABLE','Git command failed: '+args[0]+'. Check repository access and available storage.')
                self.verify()
                if output:return b''
                result.seek(0);data=result.read(16*1024*1024+1)
                if len(data)>16*1024*1024:raise BackendError('QUOTA_EXCEEDED','Git metadata exceeded its bound')
                return data
            finally:
                if process.poll() is None:os.killpg(process.pid,signal.SIGKILL)
                process.wait()

    def acquire(self, repo, retention=30, branch=None):
        self.verify();directory=self.root/'repositories'/repo['id'];directory.mkdir(parents=True,exist_ok=True)
        mirror=directory/'mirror.git'
        if not mirror.exists():
            stage=directory/('clone-'+uuid.uuid4().hex)
            self.command(None,'init','--bare',str(stage))
            self.command(stage,'remote','add','origin',repo['url'])
            self.command(stage,'fetch','--atomic','origin','+refs/heads/*:refs/heads/*','+refs/tags/*:refs/tags/*')
            self.command(stage,'fsck','--full','--no-dangling')
            os.rename(stage,mirror)
        else:
            old=self.command(mirror,'for-each-ref','--format=%(objectname) %(refname)','refs/heads/','refs/tags/').decode().splitlines()
            stamp=time.strftime('%Y%m%dT%H%M%S',time.gmtime())+'-'+uuid.uuid4().hex[:8]
            for row in old:
                oid,ref=row.split(' ',1);self.command(mirror,'update-ref','refs/backup/'+stamp+'/'+ref[5:],oid)
            self.command(mirror,'remote','set-url','origin',repo['url'])
            self.command(mirror,'fetch','--atomic','--prune','origin','+refs/heads/*:refs/heads/*','+refs/tags/*:refs/tags/*')
        lfs_complete=True
        try:self.command(mirror,'-c','lfs.url='+repo['url'].removesuffix('.git')+'.git/info/lfs','lfs','fetch','--all','origin',timeout=1800)
        except BackendError:lfs_complete=False
        selected=branch or repo['branch']
        self.command(None,'check-ref-format','refs/heads/'+selected)
        heads=self.command(mirror,'for-each-ref','--format=%(refname)','refs/heads/').decode().splitlines()
        if not heads:return dict(repo,status='empty',commit=None,selectedBranch=selected,coverage=[],artifact=None)
        commit=self.command(mirror,'rev-parse','--verify','refs/heads/'+selected+'^{commit}').decode().strip()
        self.command(mirror,'symbolic-ref','HEAD','refs/heads/'+selected)
        refs=self.command(mirror,'for-each-ref','--format=%(refname)','refs/backup/').decode().splitlines()
        stamps=sorted({r.split('/')[2] for r in refs})
        for stamp in stamps[:-retention]:
            for ref in refs:
                if ref.startswith('refs/backup/'+stamp+'/'):self.command(mirror,'update-ref','-d',ref)
        cached=directory/'latest.json'
        if cached.exists():
            previous=json.loads(cached.read_text())
            if lfs_complete and previous.get('commit')==commit and previous.get('selectedBranch')==selected and not previous.get('coverage') and (self.root/'generations'/previous['artifact']/'files.tar').is_file():
                return dict(previous,**repo,selectedBranch=selected,completedAt=time.time())
        result=self.export(repo,mirror,commit,selected)
        if not lfs_complete and not self.allow_local:result['coverage'].append({'kind':'lfs-history','path':'','message':'Full-history LFS verification did not complete'})
        atomic_json(cached,result)
        return result

    def export(self, repo, mirror, commit, branch, ancestors=()):
        generation=uuid.uuid4().hex
        directory=self.root/'generations'/generation;directory.mkdir(parents=True,mode=0o750)
        raw=directory/'source.tar';final=directory/'files.tar';coverage=[]
        with raw.open('xb') as f:self.command(mirror,'archive','--format=tar',commit,output=f)
        tree=self.command(mirror,'ls-tree','-r','-z',commit)
        submodules=[row for row in tree.split(b'\0') if row.startswith(b'160000 ')]
        lfs_attempted=False;lfs_ok=False;count=total=0
        with tarfile.open(raw,'r:') as source,tarfile.open(final,'w') as dest:
            for entry in source:
                name=safe_path(entry.name.rstrip('/'))
                if entry.isdir():continue
                if not entry.isfile():
                    coverage.append({'kind':'symlink' if entry.issym() else 'unsupported','path':name,'message':'Preserved in Git history; source export does not follow links'});continue
                if entry.size>64*1024**3:raise BackendError('QUOTA_EXCEEDED','Repository file exceeds export budget')
                stream=source.extractfile(entry);prefix=stream.read(min(entry.size,1024));stream.seek(0)
                lfs=re.fullmatch(rb'version https://git-lfs.github.com/spec/v1\noid sha256:([a-f0-9]{64})\nsize ([0-9]+)\n?',prefix)
                if lfs:
                    if not lfs_attempted:
                        lfs_attempted=True
                        try:self.command(mirror,'-c','lfs.url='+repo['url'].removesuffix('.git')+'.git/info/lfs','lfs','fetch','--all','origin',timeout=1800);lfs_ok=True
                        except BackendError:coverage.append({'kind':'lfs','path':name,'message':'LFS download unavailable; pointers retained'})
                    oid=lfs[1].decode();obj=mirror/'lfs/objects'/oid[:2]/oid[2:4]/oid
                    if lfs_ok and obj.is_file():
                        digest=hashlib.sha256()
                        with obj.open('rb') as lf:
                            for chunk in iter(lambda:lf.read(1024*1024),b''):digest.update(chunk)
                        if digest.hexdigest()!=oid or obj.stat().st_size!=int(lfs[2]):raise BackendError('INTEGRITY_FAILED','LFS object checksum failed')
                        stream.close();stream=obj.open('rb');entry.size=obj.stat().st_size
                    else:coverage.append({'kind':'lfs','path':name,'message':'LFS object missing; pointer retained'})
                entry.name=name;entry.uid=entry.gid=0;entry.uname=entry.gname='';entry.mode=0o755 if entry.mode&0o111 else 0o644
                dest.addfile(entry,stream);stream.close();count+=1;total+=entry.size
                if count>500000 or total>200*1024**3:raise BackendError('QUOTA_EXCEEDED','Repository export exceeds configured bounds')
        if submodules:
            with tarfile.open(final,'a') as dest:
                for row in submodules:
                    meta,path=row.split(b'\t',1);path=safe_path(path.decode('utf-8'));pinned=meta.split()[2].decode()
                    try:
                        if len(ancestors)>=5:raise BackendError('QUOTA_EXCEEDED','Submodule recursion limit')
                        configured=self.command(mirror,'config','--null','--blob',commit+':.gitmodules','--get-regexp',r'^submodule\..*\.path$')
                        key=next(part.split(b'\n',1)[0].decode()[:-5]+'.url' for part in configured.split(b'\0') if b'\n' in part and part.split(b'\n',1)[1].decode()==path)
                        url=self.command(mirror,'config','--blob',commit+':.gitmodules','--get',key).decode().strip()
                        from urllib.parse import urljoin
                        if url.startswith(('./','../')):url=urljoin(repo['url']+'/',url)
                        if url.startswith('git@github.com:'):url='https://github.com/'+url[len('git@github.com:'):]
                        if not re.fullmatch(r'https://github.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?',url):
                            raise BackendError('PERMISSION_DENIED','Submodule host is outside GitHub policy')
                        identity=url+'@'+pinned
                        if identity in ancestors:raise BackendError('INVALID_ARGUMENT','Submodule cycle')
                        sub=self.root/'submodules'/hashlib.sha256(url.encode()).hexdigest()
                        sub.parent.mkdir(exist_ok=True)
                        if not sub.exists():
                            self.command(None,'init','--bare',str(sub));self.command(sub,'remote','add','origin',url)
                        self.command(sub,'fetch','--atomic','origin','+refs/heads/*:refs/heads/*','+refs/tags/*:refs/tags/*')
                        self.command(sub,'cat-file','-e',pinned+'^{commit}')
                        # Pinned commits remain reachable even if the remote rewrites history.
                        self.command(sub,'update-ref','refs/backup/pinned/'+pinned,pinned)
                        exported=self.export(dict(repo,url=url),sub,pinned,pinned,ancestors+(identity,))
                        with tarfile.open(self.root/'generations'/exported['artifact']/'files.tar','r:') as child:
                            for item in child:
                                item.name=safe_path(path+'/'+item.name)
                                with child.extractfile(item) as stream:dest.addfile(item,stream)
                        count+=exported['files'];total+=exported['bytes']
                        coverage.extend(dict(c,path=path+'/'+c['path']) for c in exported['coverage'])
                        shutil.rmtree(self.root/'generations'/exported['artifact'])
                    except Exception as error:
                        coverage.append({'kind':'submodule','path':path,'message':error.message if isinstance(error,BackendError) else 'Pinned submodule could not be acquired'})
        raw.unlink();os.chmod(final,0o640)
        digest=hashlib.sha256()
        with final.open('rb') as f:
            for chunk in iter(lambda:f.read(1024*1024),b''):digest.update(chunk)
            os.fsync(f.fileno())
        result=dict(repo,status='acquired',commit=commit,selectedBranch=branch,coverage=coverage,
                    artifact=generation,sha256=digest.hexdigest(),files=count,bytes=total,completedAt=time.time())
        atomic_json(directory/'manifest.json',result);os.chmod(directory/'manifest.json',0o640)
        return result
