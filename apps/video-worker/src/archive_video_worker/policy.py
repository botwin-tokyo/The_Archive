"""Fixed supported source identities; shared by admission and provider."""
import re
from urllib.parse import urlsplit,parse_qs
MAX_BYTES=8*1024**3

def canonical_url(value):
    if not isinstance(value,str) or not 1<=len(value)<=2048 or any(c.isspace() for c in value): raise ValueError('INVALID_URL')
    try: u=urlsplit(value); port=u.port
    except ValueError: raise ValueError('INVALID_URL') from None
    if u.scheme!='https' or u.username or u.password or port not in (None,443) or u.fragment: raise ValueError('INVALID_URL')
    host=u.hostname; query=parse_qs(u.query)
    if 'list' in query: raise ValueError('PLAYLIST_UNSUPPORTED')
    if host in ('youtube.com','www.youtube.com','m.youtube.com','youtu.be'):
        if host=='youtu.be': identity=u.path.strip('/')
        elif u.path=='/watch' and len(query.get('v',[]))==1: identity=query['v'][0]
        elif u.path.startswith('/shorts/'): identity=u.path[len('/shorts/'):].rstrip('/')
        else: raise ValueError('INVALID_URL')
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}',identity): raise ValueError('INVALID_URL')
        return 'https://www.youtube.com/watch?v='+identity
    if host in ('x.com','www.x.com','twitter.com','www.twitter.com'):
        m=re.fullmatch(r'/[A-Za-z0-9_]{1,30}/status/([0-9]{1,25})/?',u.path)
        if m:return 'https://x.com/i/status/'+m[1]
    raise ValueError('UNSUPPORTED_URL')
