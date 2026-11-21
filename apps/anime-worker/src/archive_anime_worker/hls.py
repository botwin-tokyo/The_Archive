"""Strict HLS playlist parsing: plain MPEG-TS VOD only; everything else is rejected explicitly."""
import hashlib
import re
import urllib.parse
from . import net

MAX_PLAYLIST = 2 * 1024 * 1024
MAX_SEGMENTS = 5000
# Tags this downloader understands. A tag outside this set is a format we have not proven safe.
MASTER_TAGS = ('#EXTM3U', '#EXT-X-VERSION', '#EXT-X-STREAM-INF', '#EXT-X-MEDIA', '#EXT-X-INDEPENDENT-SEGMENTS')
MEDIA_TAGS = ('#EXTM3U', '#EXT-X-VERSION', '#EXT-X-TARGETDURATION', '#EXT-X-MEDIA-SEQUENCE', '#EXT-X-PLAYLIST-TYPE', '#EXT-X-ALLOW-CACHE', '#EXTINF', '#EXT-X-ENDLIST', '#EXT-X-INDEPENDENT-SEGMENTS')
UNSUPPORTED = ('#EXT-X-KEY', '#EXT-X-SESSION-KEY', '#EXT-X-MAP', '#EXT-X-BYTERANGE', '#EXT-X-DISCONTINUITY', '#EXT-X-PART', '#EXT-X-I-FRAME-STREAM-INF')


def lines(url):
    _, _, body = net.fetch(url, limit=MAX_PLAYLIST)
    try: text = body.decode('utf-8')
    except UnicodeDecodeError: raise net.Failure('UNSUPPORTED_PLAYLIST') from None
    rows = [row.strip() for row in text.splitlines()]
    if not rows or rows[0] != '#EXTM3U': raise net.Failure('UNSUPPORTED_PLAYLIST')
    return [row for row in rows if row]


def attributes(line):
    return {k: v.strip('"') for k, v in re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line.split(':', 1)[1] if ':' in line else '')}


def tag(row):
    return row.split(':', 1)[0]


def check_tags(rows, allowed):
    for row in rows:
        if not row.startswith('#'): continue
        name = tag(row)
        if name in UNSUPPORTED: raise net.Failure('UNSUPPORTED_PLAYLIST')
        if name.startswith('#EXT') and name not in allowed: raise net.Failure('UNSUPPORTED_PLAYLIST')


def variants(master_url):
    """Return video variants as {height,width,codecs,url}. Separate audio renditions are unsupported."""
    rows = lines(master_url)
    if any(r.startswith('#EXTINF') for r in rows):
        raise net.Failure('UNSUPPORTED_PLAYLIST')  # a bare media playlist has no declared resolution to verify
    check_tags(rows, MASTER_TAGS)
    if any(r.startswith('#EXT-X-MEDIA') and attributes(r).get('TYPE') == 'AUDIO' for r in rows): raise net.Failure('SEPARATE_AUDIO_UNSUPPORTED')
    result = []
    for index, row in enumerate(rows):
        if not row.startswith('#EXT-X-STREAM-INF'): continue
        values = attributes(row)
        if index + 1 >= len(rows) or rows[index + 1].startswith('#'): raise net.Failure('UNSUPPORTED_PLAYLIST')
        match = re.fullmatch(r'([0-9]{2,5})x([0-9]{2,5})', values.get('RESOLUTION', ''))
        codecs = values.get('CODECS', '')
        if not match or 'AUDIO' in values: continue
        if not all(c.strip().startswith(('avc1.', 'mp4a.40.')) for c in codecs.split(',') if c.strip()) or 'avc1.' not in codecs: continue
        result.append({'width': int(match[1]), 'height': int(match[2]), 'codecs': codecs, 'url': net.check(urllib.parse.urljoin(master_url, rows[index + 1]))})
        if len(result) > 16: raise net.Failure('UNSUPPORTED_PLAYLIST')
    if not result: raise net.Failure('UNSUPPORTED_STREAM')
    return result


def select(found, quality):
    """`best` takes the tallest variant; an exact height must exist or the episode is unavailable."""
    if quality == 'best': return max(found, key=lambda v: (v['height'], v['width']))
    height = int(quality[:-1])
    exact = [v for v in found if v['height'] == height]
    if not exact: raise net.Failure('QUALITY_UNAVAILABLE')
    return max(exact, key=lambda v: v['width'])


def media(url):
    """Return (segment URLs, total duration, manifest identity)."""
    rows = lines(url)
    check_tags(rows, MEDIA_TAGS)
    if '#EXT-X-ENDLIST' not in rows: raise net.Failure('UNSUPPORTED_PLAYLIST')  # live/event playlists are not VOD
    segments, duration, pending = [], 0.0, None
    for row in rows:
        if row.startswith('#EXTINF:'):
            try: pending = float(row[8:].split(',', 1)[0])
            except ValueError: raise net.Failure('UNSUPPORTED_PLAYLIST') from None
            if not 0 < pending <= 600: raise net.Failure('UNSUPPORTED_PLAYLIST')
        elif not row.startswith('#'):
            if pending is None: raise net.Failure('UNSUPPORTED_PLAYLIST')
            segments.append(net.check(urllib.parse.urljoin(url, row))); duration += pending; pending = None
            if len(segments) > MAX_SEGMENTS: raise net.Failure('SEGMENT_LIMIT')
    if not segments or pending is not None: raise net.Failure('UNSUPPORTED_PLAYLIST')
    identity = hashlib.sha256('\n'.join(segments).encode()).hexdigest()
    return segments, duration, identity
