"""Optional, bounded LRCLIB lookup. Never search arbitrary URLs or guess lyrics."""
import json
import math
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

MAX_TEXT = 65536
MAX_RESPONSE = 256 * 1024
TIMESTAMP = re.compile(r'^\[(\d{1,3}):([0-5]\d)(?:\.(\d{1,3}))?\](.*)$')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl): return None


def normalized(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def clean(value):
    if value is None: return None
    if not isinstance(value, str) or len(value.encode('utf-8')) > MAX_TEXT: raise ValueError('Lyrics size')
    if any(ord(c) < 32 and c not in '\n\r\t' for c in value): raise ValueError('Lyrics controls')
    return value.replace('\r\n','\n').strip() or None


def parse_lrc(text, duration):
    lines=[]
    for line in text.splitlines():
        match=TIMESTAMP.fullmatch(line)
        if not match:
            if line.strip(): raise ValueError('Unsupported timestamp line')
            continue
        minutes,seconds,fraction,words=match.groups()
        milliseconds=(int(minutes)*60+int(seconds))*1000+int((fraction or '').ljust(3,'0'))
        if milliseconds > (duration+3)*1000 or lines and milliseconds < lines[-1][1]: raise ValueError('Lyrics timing')
        lines.append((words.strip(),milliseconds))
    if not lines: raise ValueError('No timestamped lyrics')
    return lines


def lookup(title, artists, album, duration, actual_duration=None):
    empty={'status':'missing','plain':None,'synced':None,'sourceId':None,'embedded':False}
    try:
        if not title or not artists or not math.isfinite(duration) or duration<=0: return empty
        params=urllib.parse.urlencode({'track_name':title,'artist_name':artists[0],'album_name':album,'duration':round(duration)})
        request=urllib.request.Request('https://lrclib.net/api/get?'+params,headers={'User-Agent':'ArchiveConsole/0.1.0 (local music archive)','Accept':'application/json'})
        with urllib.request.build_opener(NoRedirect).open(request,timeout=8) as response:
            raw=response.read(MAX_RESPONSE+1)
        if len(raw)>MAX_RESPONSE: raise ValueError('Lyrics response too large')
        result=json.loads(raw)
        matched=float(result['duration'])
        if not math.isfinite(matched) or abs(matched-duration)>2 or normalized(result['trackName'])!=normalized(title): return empty
        if normalized(result['artistName']) not in {normalized(a) for a in artists}|{normalized(', '.join(artists))}: return empty
        if album and normalized(result['albumName'])!=normalized(album): return empty
        source_id=result.get('id')
        if type(source_id) is not int or source_id<0: raise ValueError('Lyrics identity')
        if result.get('instrumental') is True: return dict(empty,status='instrumental',sourceId=source_id)
        plain=clean(result.get('plainLyrics'));synced=clean(result.get('syncedLyrics'))
        if synced:
            try:
                parsed=parse_lrc(synced,duration)
                plain=plain or clean('\n'.join(words for words,_ in parsed))
                if actual_duration is not None and abs(actual_duration-matched)>2: synced=None
            except ValueError: synced=None
        return dict(empty,status='found' if plain or synced else 'missing',plain=plain,synced=synced,sourceId=source_id)
    except urllib.error.HTTPError as error:
        return dict(empty,status='missing' if error.code==404 else 'failed')
    except Exception:
        return dict(empty,status='failed')


def for_track(args):
    from .provider import client
    track=client().track(args['trackId'])
    if track.get('id')!=args['trackId']: raise ValueError('Track identity mismatch')
    return lookup(track['name'],[a['name'] for a in track['artists']],track['album']['name'],track['duration_ms']/1000,
                  args.get('durationMs',0)/1000 or None)


def embed(path, result, duration):
    """Edit a disposable copy so optional tagging failures cannot damage audio."""
    if result['status']!='found': return result
    import os
    import shutil
    from mutagen.id3 import ID3, USLT, SYLT
    pending=path.with_name('lyrics-tagged.mp3')
    try:
        shutil.copyfile(path,pending)
        tags=ID3(pending)
        if result['plain']: tags.add(USLT(encoding=3,lang='und',desc='',text=result['plain']))
        if result['synced']: tags.add(SYLT(encoding=3,lang='und',format=2,type=1,desc='',text=parse_lrc(result['synced'],duration)))
        tags.save(pending)
        ID3(pending)
        from .media import verify
        verify(pending,int(duration*1000))
        os.replace(pending,path)
        return dict(result,embedded=True)
    except Exception:
        pending.unlink(missing_ok=True)
        return result
