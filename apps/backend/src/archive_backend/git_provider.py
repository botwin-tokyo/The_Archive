"""Bounded, authenticated GitHub discovery. Never logs response bodies or secrets."""
import json
import os
import re
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from .contracts import BackendError


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_secret(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096 or info.st_mode & 0o077 or info.st_uid != os.geteuid():
            raise BackendError('PERMISSION_DENIED', 'GitHub credential must be private and worker-owned')
        value = os.read(fd, 4096).decode().strip()
        if not 20 <= len(value) <= 2048 or any(c.isspace() for c in value):
            raise BackendError('INVALID_ARGUMENT', 'Invalid GitHub credential')
        return value
    finally: os.close(fd)


def repo_record(raw):
    owner, name = raw.get('owner', {}).get('login'), raw.get('name')
    if type(raw.get('id')) is not int or raw['id'] <= 0 or not all(isinstance(s,str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',s) and s not in ('.','..') for s in (owner,name)):
        raise BackendError('INVALID_RESPONSE', 'Invalid GitHub repository identity')
    return {'id':str(raw['id']), 'owner':owner, 'name':name, 'fullName':owner+'/'+name,
            'url':'https://github.com/'+owner+'/'+name+'.git',
            'branch':raw.get('default_branch') or 'main', 'private':bool(raw.get('private')),
            'archived':bool(raw.get('archived')), 'fork':bool(raw.get('fork')),
            'sizeKiB':max(0,int(raw.get('size') or 0)), 'permissions':{k:bool(raw.get('permissions',{}).get(k)) for k in ('admin','maintain','push')}}


class GitHub:
    def __init__(self, token, *, request=None, sleep=time.sleep):
        self.token=token; self.request=request or urllib.request.build_opener(NoRedirect()).open; self.sleep=sleep

    def get(self, url):
        parsed=urllib.parse.urlsplit(url)
        if parsed.scheme!='https' or parsed.netloc!='api.github.com' or parsed.username or parsed.fragment:
            raise BackendError('INVALID_RESPONSE', 'GitHub pagination left the trusted API')
        request=urllib.request.Request(url,headers={'Authorization':'Bearer '+self.token,
            'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28','User-Agent':'Archive-Console-Git-Backup'})
        for attempt in range(3):
            try:
                with self.request(request,timeout=30) as response:
                    data=response.read(8*1024*1024+1)
                    if len(data)>8*1024*1024:raise BackendError('INVALID_RESPONSE','GitHub page too large')
                    return json.loads(data), response.headers.get('Link','')
            except urllib.error.HTTPError as exc:
                retry=exc.code in (429,500,502,503,504) or (exc.code==403 and (exc.headers.get('Retry-After') or exc.headers.get('X-RateLimit-Remaining')=='0'))
                if retry and attempt<2:
                    try: delay=min(60,max(1,int(exc.headers.get('Retry-After','5'))))
                    except ValueError:delay=5
                    self.sleep(delay);continue
                if exc.code in (401,403):raise BackendError('PERMISSION_DENIED','GitHub authentication, scope, SSO, or rate limit requires attention') from None
                raise BackendError('UNAVAILABLE','GitHub discovery failed (HTTP '+str(exc.code)+')') from None
            except (OSError,ValueError):
                if attempt<2:self.sleep(2**attempt);continue
                raise BackendError('UNAVAILABLE','GitHub discovery could not finish') from None

    def discover(self, scope='owned'):
        identity,_=self.get('https://api.github.com/user')
        affiliation='owner' if scope=='owned' else 'owner,collaborator,organization_member'
        url='https://api.github.com/user/repos?per_page=100&affiliation='+affiliation
        repos={}; seen=set()
        while url:
            if url in seen or len(seen)>=100:raise BackendError('INVALID_RESPONSE','GitHub pagination exceeded its bound')
            seen.add(url); page,links=self.get(url)
            if not isinstance(page,list):raise BackendError('INVALID_RESPONSE','GitHub repository page malformed')
            for raw in page:
                item=repo_record(raw)
                owned=item['owner'].lower()==identity['login'].lower()
                if scope=='owned' and not owned:continue
                if scope=='controlled' and not owned and not any(item['permissions'].values()):continue
                repos[item['id']]=item
            matches=re.findall(r'<([^>]+)>;\s*rel="next"',links)
            url=matches[0] if matches else None
        return {'login':identity['login'],'scope':scope,'repositories':sorted(repos.values(),key=lambda r:r['fullName'].lower()),
                'discoveredAt':time.time(),'visibilityNotice':'Inventory includes only repositories visible to this credential. Git data excludes issues, releases and account settings.'}
