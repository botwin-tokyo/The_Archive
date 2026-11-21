"""ani.pm (settlar) provider adapter: series matching, episode inventory and stream sessions.

Provider series identity is `<source>/<route>`; the route's trailing number is the
site's series ID and the slug is cosmetic. Matching uses AniList or MAL identity,
never a title alone.
"""
import json
import re
import urllib.parse
from . import net

BASE = 'https://ani.pm'
SERIES = re.compile(r'^([a-z]{2,20})/([a-z0-9]+(?:-[a-z0-9]+)*)-([0-9]{1,12})$')
EPISODE_ID = re.compile(r'^[A-Za-z0-9._-]{1,200}$')
SUBTITLES = {'en': 'en', 'es': 'es', 'es-419': 'es', 'pt': 'pt', 'pt-br': 'pt', 'fr': 'fr', 'de': 'de', 'it': 'it', 'ru': 'ru', 'ar': 'ar', 'ja': 'ja',
             'zh': 'zh', 'zh-cn': 'zh', 'zh-tw': 'zh', 'ko': 'ko', 'id': 'id', 'th': 'th', 'vi': 'vi', 'pl': 'pl', 'tr': 'tr', 'hi': 'hi', 'ms': 'ms'}


def api(url, limit=2 * 1024 * 1024):
    _, _, body = net.fetch(url, limit=limit)
    try: return json.loads(body)
    except ValueError: raise net.Failure('SOURCE_UNAVAILABLE') from None


def parse_series(provider_series):
    match = SERIES.fullmatch(provider_series or '')
    if not match: raise net.Failure('INVALID_REQUEST')
    return match[1], match[3]


def integer_id(value):
    try: number = int(str(value))
    except (TypeError, ValueError): return None
    return number if 1 <= number <= 10**10 else None


def candidate(item, catalog):
    source, route = item.get('source'), item.get('routeId')
    provider = f'{source}/{route}' if isinstance(source, str) and isinstance(route, str) else ''
    if not SERIES.fullmatch(provider): return None
    anilist, mal = integer_id(item.get('anilistId')), integer_id(item.get('malId'))
    evidence = 'anilist-id' if anilist == catalog['anilistId'] else 'mal-id' if catalog['malId'] and mal == catalog['malId'] else 'title'
    year = item.get('year')
    count = item.get('episodeCount')
    return {'providerSeriesId': provider, 'title': str(item.get('title') or 'Untitled')[:300], 'year': year if type(year) is int and 1900 <= year <= 2200 else None,
            'type': str(item['type'])[:40] if isinstance(item.get('type'), str) else None, 'episodeCount': count if type(count) is int and 0 <= count <= 99999 else None,
            'anilistId': anilist, 'malId': mal if mal and mal <= 10**9 else None, 'evidence': evidence}


def search(text):
    data = api(f'{BASE}/api/anime/search?q=' + urllib.parse.quote(text[:200]))
    items = data.get('items') if isinstance(data, dict) else None
    return [i for i in items[:40] if isinstance(i, dict)] if isinstance(items, list) else []


def series(provider_series):
    source, number = parse_series(provider_series)
    data = api(f'{BASE}/api/anime/series/{number}', limit=4 * 1024 * 1024)
    if not isinstance(data, dict) or str(data.get('id')) != number or data.get('source') != source: raise net.Failure('NOT_FOUND')
    return data


def candidates(catalog, titles):
    """Search by catalog titles; identity evidence ranks first. At most 20 distinct candidates."""
    found = {}
    for title in titles:
        for item in search(title):
            value = candidate(item, catalog)
            if value and value['providerSeriesId'] not in found: found[value['providerSeriesId']] = value
        if any(v['evidence'] != 'title' for v in found.values()): break
    rank = {'anilist-id': 0, 'mal-id': 1, 'title': 2}
    return sorted(found.values(), key=lambda v: rank[v['evidence']])[:20]


def automatic(found):
    """One distinct identity match is accepted; several identity matches are ambiguous."""
    for evidence in ('anilist-id', 'mal-id'):
        matches = {c['providerSeriesId'] for c in found if c['evidence'] == evidence}
        if len(matches) == 1: return matches.pop()
        if matches: return None
    return None


def explicit(provider_series, catalog):
    """An owner-selected series is loaded directly; it need not appear in a title search."""
    data = series(provider_series)
    value = candidate({'source': parse_series(provider_series)[0], 'routeId': provider_series.split('/', 1)[1], 'title': data.get('title'), 'year': data.get('year'),
                       'type': data.get('type'), 'episodeCount': data.get('episodeCount'), 'anilistId': data.get('anilistId'), 'malId': data.get('malId')}, catalog)
    return value, data


def episodes(data):
    result = []
    for item in (data.get('episodes') or [])[:5000]:
        if not isinstance(item, dict) or not isinstance(item.get('routeId'), str) or not EPISODE_ID.fullmatch(item['routeId']): continue
        number = item.get('number')
        if type(number) not in (int, float) or not 0 <= number <= 99999: continue
        if type(number) is float and number.is_integer(): number = int(number)
        title = item.get('title') if isinstance(item.get('title'), str) and item['title'].strip() else None
        result.append({'id': item['routeId'], 'number': number, 'title': title[:300] if title else None, 'sub': item.get('sub') is True, 'dub': item.get('dub') is True})
    numbers = set()
    # Repeated provider numbers cannot be named or selected unambiguously.
    return [e for e in result if not (e['number'] in numbers or numbers.add(e['number']))]


def number_text(number):
    return str(int(number)) if float(number).is_integer() else ('%.2f' % number).rstrip('0').rstrip('.')


def session(provider_series, number, audio):
    """Resolve a short-lived playback session; returns the session JSON and its audio language."""
    source, _ = parse_series(provider_series)
    route = provider_series.split('/', 1)[1]
    ep = number_text(number)
    boot = api(f'{BASE}/api/anime/playback-bootstrap/{source}/{route}?ep={ep}&lang={audio}&core=0')
    if not isinstance(boot, dict) or boot.get('effectiveLanguage') != audio or not isinstance(boot.get('settlarSelection'), str): raise net.Failure('VARIANT_UNAVAILABLE')
    selection = urllib.parse.quote(boot['settlarSelection'][:4096], safe='')
    preview = api(f'{BASE}/api/anime/settlar/preview-session?selection={selection}&provider=anipm&ep={ep}&channel={audio}&telemetry=0')
    embed_url = preview.get('embedUrl') if isinstance(preview, dict) else None
    net.check(embed_url if isinstance(embed_url, str) else '')
    embed = urllib.parse.urlsplit(embed_url)
    token = urllib.parse.parse_qs(embed.query).get('t', [''])[0]
    if not token or len(token) > 8192: raise net.Failure('UNSUPPORTED_STREAM')
    value = api(f'https://{embed.hostname}/api/embed/session?t=' + urllib.parse.quote(token, safe=''))
    if not isinstance(value, dict) or value.get('kind') != 'hls' or not isinstance(value.get('source'), str): raise net.Failure('UNSUPPORTED_STREAM')
    net.check(value['source'])
    expected = 'ja' if audio == 'sub' else 'en'
    if value.get('audioLang') not in (None, expected): raise net.Failure('VARIANT_UNAVAILABLE')
    return value


def session_subtitles(value):
    """Map advertised WebVTT renditions to project language codes; unknown languages are ignored."""
    result = {}
    for item in (value.get('subtitles') or [])[:40]:
        if not isinstance(item, dict) or not isinstance(item.get('url'), str): continue
        code = SUBTITLES.get(str(item.get('srclang', '')).lower())
        if code and code not in result:
            try: result[code] = net.check(item['url'])
            except net.Failure: continue
    return result
