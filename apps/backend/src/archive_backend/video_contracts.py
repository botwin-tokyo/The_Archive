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

from .contracts import BackendError,object_fields
from .music_contracts import tool_schema as music_schema

def tool_schema(capability):
    # Reuse shared scalar/location types while supplying video-specific fields.
    from dataclasses import replace
    base=music_schema(replace(capability,required=tuple(k for k in capability.required if k not in ('url','choiceId'))))
    if 'url' in capability.required:base['properties']['url']={'type':'string','minLength':1,'maxLength':2048,'description':'Public single YouTube or X/Twitter video HTTPS URL'}
    if 'choiceId' in capability.required:base['properties']['choiceId']={'type':'string','pattern':'^(audio|video-[0-9]{1,4})$','description':'Exact choice ID returned by the completed preview'}
    base['required']=list(capability.required)
    return base

def metadata(data):
    import math
    object_fields(data,('title','uploader','duration','source','choices'))
    if any(not isinstance(data[k],str) or len(data[k])>500 for k in ('title','uploader','source')) or not re.fullmatch(r'(youtube|twitter):[A-Za-z0-9_-]{1,100}',data['source']):raise BackendError('INVALID_RESPONSE','Invalid video metadata')
    if type(data['duration']) not in (int,float) or not math.isfinite(data['duration']) or not 0<data['duration']<=86400:raise BackendError('INVALID_RESPONSE','Invalid video duration')
    if not isinstance(data['choices'],list) or not 1<=len(data['choices'])<=20:raise BackendError('INVALID_RESPONSE','Invalid video choices')
    seen=set()
    for c in data['choices']:
        object_fields(c,('id','label','kind','height','estimatedBytes'))
        if not isinstance(c['id'],str) or c['id'] in seen or not isinstance(c['label'],str) or len(c['label'])>500 or type(c['height']) is not int or not 0<=c['height']<=4320:raise BackendError('INVALID_RESPONSE','Invalid video choice')
        if (c['kind']=='audio' and (c['id']!='audio' or c['height']!=0)) or (c['kind']=='video' and (c['height']==0 or c['id']!='video-'+str(c['height']))) or c['kind'] not in ('video','audio'):raise BackendError('INVALID_RESPONSE','Invalid video choice')
        if c['estimatedBytes'] is not None and (type(c['estimatedBytes']) not in (int,float) or not math.isfinite(c['estimatedBytes']) or c['estimatedBytes']<0):raise BackendError('INVALID_RESPONSE','Invalid estimate')
        seen.add(c['id'])
    return data
