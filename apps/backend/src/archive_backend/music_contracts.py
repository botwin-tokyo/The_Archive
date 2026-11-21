"""Music DTO validation and deterministic logical names, without provider imports."""
import hashlib
import re
import unicodedata
from .contracts import BackendError, identifier, object_fields, path

SPOTIFY_ID = re.compile(r'^[A-Za-z0-9]{22}$')
SUCCESS = frozenset(('completed','already-present'))
TERMINAL = SUCCESS | {'failed','conflict','blocked-auth','canceled'}


def selection(value):
    object_fields(value, ('kind','id'))
    if value['kind'] not in ('track','album','artist') or not isinstance(value['id'],str) or not SPOTIFY_ID.fullmatch(value['id']):
        raise BackendError('INVALID_ARGUMENT','A Spotify track, album or artist identity is required')
    return value


def item(value):
    fields=('kind','id','title','artist','year','trackCount','durationMs','disc','number','available')
    object_fields(value, fields); selection({k:value[k] for k in ('kind','id')})
    if any(not isinstance(value[k],str) or len(value[k])>500 for k in ('title','artist','year')):
        raise BackendError('INVALID_RESPONSE','Invalid music metadata')
    for k,low,high in (('trackCount',0,500),('durationMs',0,86400000),('disc',1,500),('number',1,500)):
        if type(value[k]) is not int or not low<=value[k]<=high: raise BackendError('INVALID_RESPONSE','Invalid track numbering')
    if type(value['available']) is not bool: raise BackendError('INVALID_RESPONSE','Invalid track availability')
    return value


def label(value, maximum=120):
    value=unicodedata.normalize('NFC',value)
    value=''.join('_' if c in '/\\\0<>:"|?*' or unicodedata.category(c).startswith('C') else c for c in value)
    value=value.strip(' .') or 'Unknown'
    if value.lower().startswith('.archive-'): value='_'+value
    while len(value.encode())>maximum: value=value[:-1]
    return value


def target(plan,track,destination):
    artist_id=plan.get('_artistId') or hashlib.sha256(plan['_albumArtist'].encode()).hexdigest()[:16]
    parts=[destination['path'],f"{label(plan['_albumArtist'])} [{label(artist_id,24)}]",
           f"{label(plan['year'] or 'Unknown',10)} - {label(plan['_albumTitle'])} [{plan['_albumId']}]",
           f"Disc {track['disc']:02d}",f"{track['number']:03d} - {label(track['title'])} [{track['id']}].mp3"]
    result='/'.join(p for p in parts if p)
    if len(result.encode())>1024: raise BackendError('INVALID_PATH','Music destination is too deep')
    return path(result)


def public(value):
    if isinstance(value,list): return [public(v) for v in value]
    if isinstance(value,dict): return {k:public(v) for k,v in value.items() if not k.startswith('_')}
    return value


def tool_schema(capability):
    """Discoverable nested input schemas for generic MCP clients."""
    string={'type':'string','minLength':1,'maxLength':256}
    selection_schema={'type':'object','properties':{'kind':{'enum':['track','album','artist']},'id':{'type':'string','pattern':'^[A-Za-z0-9]{22}$'}},'required':['kind','id'],'additionalProperties':False}
    location_schema={'type':'object','properties':{'driveId':string,'path':{'type':'string','maxLength':1024}},'required':['driveId','path'],'additionalProperties':False}
    types={'id':string,'requestId':string,'planId':string,'planRevision':{'type':'integer','minimum':1},'revision':{'type':'integer','minimum':1},
           'query':{'type':'string','minLength':1,'maxLength':300},'kind':{'enum':['track','album','artist']},'selection':selection_schema,
           'destination':location_schema,'cursor':string,'limit':{'type':'integer','minimum':1,'maximum':100},'action':{'enum':['pause','resume','cancel','retry-failed']}}
    return {'type':'object','properties':{key:types[key] for key in capability.required+capability.optional},'required':list(capability.required),'additionalProperties':False}


def artist_plan(data):
    releases=data.get('releases')
    if not isinstance(releases,list) or not 1<=len(releases)<=500:
        raise BackendError('INVALID_RESPONSE','Discographies must contain 1–500 releases')
    seen=set()
    for release in releases:
        object_fields(release,('id','title','year','trackCount','releaseType'))
        selection({'kind':'album','id':release['id']})
        if release['id'] in seen: raise BackendError('INVALID_RESPONSE','Repeated discography release')
        seen.add(release['id'])
        if any(not isinstance(release[k],str) or len(release[k])>500 for k in ('title','year','releaseType')) or type(release['trackCount']) is not int or not 1<=release['trackCount']<=500:
            raise BackendError('INVALID_RESPONSE','Invalid discography release')
    if data.get('tracks')!=[] or type(data.get('expectedTracks')) is not int or data['expectedTracks']!=sum(r['trackCount'] for r in releases):
        raise BackendError('INVALID_RESPONSE','Incomplete discography count')
    for field in ('title','artist','year'):
        if not isinstance(data.get(field),str) or len(data[field])>500: raise BackendError('INVALID_RESPONSE','Invalid artist metadata')
    return releases
