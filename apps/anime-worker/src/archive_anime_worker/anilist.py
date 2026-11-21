"""AniList catalog through fixed GraphQL documents only; callers supply validated variables."""
import base64
import html
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from . import net

ENDPOINT = 'https://graphql.anilist.co'
FORMATS = ('TV', 'TV_SHORT', 'MOVIE', 'SPECIAL', 'OVA', 'ONA', 'MUSIC')
RELATIONS = ('ADAPTATION', 'PREQUEL', 'SEQUEL', 'PARENT', 'SIDE_STORY', 'CHARACTER', 'SUMMARY', 'ALTERNATIVE', 'SPIN_OFF', 'OTHER', 'SOURCE', 'COMPILATION', 'CONTAINS')
FIELDS = 'id idMal title { romaji english native userPreferred } synonyms seasonYear startDate { year } format status episodes'
SEARCH = 'query ($search: String, $page: Int) { Page(page: $page, perPage: 20) { pageInfo { hasNextPage } media(search: $search, type: ANIME, sort: SEARCH_MATCH, isAdult: false) { ' + FIELDS + ' coverImage { medium } } } }'
DETAILS = ('query ($id: Int) { Media(id: $id, type: ANIME) { ' + FIELDS + ' description(asHtml: false) genres coverImage { large } '
           'relations { edges { relationType node { id type title { userPreferred english } format seasonYear startDate { year } } } } } }')
# A shared gate across task processes: honor Retry-After and slow down when the remaining budget is low.
GATE = 'anilist-gate.json'


def gate_path():
    return Path(os.environ.get('ARCHIVE_ANIME_SPOOL', '.')) / GATE


def gate_check():
    try: not_before = json.loads(gate_path().read_text())['notBefore']
    except (OSError, ValueError, KeyError, TypeError): return
    if time.time() < not_before: raise net.RateLimited(int(not_before - time.time()) + 1)


def gate_set(seconds):
    path = gate_path(); pending = path.with_suffix('.tmp')
    try:
        pending.write_text(json.dumps({'notBefore': time.time() + seconds})); os.replace(pending, path)
    except OSError: pass


def query(document, variables):
    gate_check()
    try:
        _, headers, body = net.fetch(ENDPOINT, limit=2 * 1024 * 1024, data=json.dumps({'query': document, 'variables': variables}).encode(),
                                     headers={'Content-Type': 'application/json', 'Accept': 'application/json'})
    except net.RateLimited as limited:
        gate_set(limited.retry_after); raise
    except net.Failure as failure:
        # AniList reports an unknown ID as HTTP 404 with a GraphQL error body.
        raise net.Failure('NOT_FOUND' if str(failure) == 'NOT_FOUND' else 'SOURCE_UNAVAILABLE') from None
    try:
        remaining = int(headers.get('X-RateLimit-Remaining', '90'))
        if remaining <= 3: gate_set(20)
    except ValueError: pass
    try: value = json.loads(body)
    except ValueError: raise net.Failure('SOURCE_UNAVAILABLE') from None
    if not isinstance(value, dict) or value.get('errors') or not isinstance(value.get('data'), dict): raise net.Failure('SOURCE_UNAVAILABLE')
    return value['data']


def clean(value, maximum):
    if not isinstance(value, str): return None
    value = ' '.join(value.split()).strip()
    return value[:maximum] or None


def plain_text(value):
    """Descriptions are untrusted markup: remove tags, decode entities, keep paragraph breaks."""
    if not isinstance(value, str): return None
    value = re.sub(r'<br\s*/?>', '\n', value, flags=re.I)
    value = html.unescape(re.sub(r'<[^>]{0,500}>', '', value))
    paragraphs = [' '.join(p.split()) for p in value.split('\n')]
    return '\n'.join(p for p in paragraphs if p)[:6000] or None


def year(media):
    value = media.get('seasonYear') or (media.get('startDate') or {}).get('year')
    return value if type(value) is int and 1900 <= value <= 2200 else None


def catalog(media, has_cover):
    if type(media.get('id')) is not int or not 1 <= media['id'] <= 10**9: raise net.Failure('INVALID_METADATA')
    titles = media.get('title') or {}
    title = clean(titles.get('english'), 300) or clean(titles.get('userPreferred'), 300) or clean(titles.get('romaji'), 300) or 'Untitled'
    alternatives = []
    for value in [titles.get('romaji'), titles.get('english'), titles.get('native'), titles.get('userPreferred')] + list(media.get('synonyms') or [])[:10]:
        value = clean(value, 300)
        if value and value != title and value not in alternatives: alternatives.append(value)
    mal = media.get('idMal')
    episodes = media.get('episodes')
    return {'anilistId': media['id'], 'malId': mal if type(mal) is int and 1 <= mal <= 10**9 else None, 'title': title, 'altTitles': alternatives[:8],
            'year': year(media), 'format': media.get('format') if media.get('format') in FORMATS else None, 'status': clean(media.get('status'), 40),
            'episodes': episodes if type(episodes) is int and 0 <= episodes <= 99999 else None, 'hasCover': has_cover}


def cover(url, limit):
    """Fetch one AniList cover; return a data URL only for a recognized raster signature."""
    if not isinstance(url, str): return None
    try: _, headers, body = net.fetch(url, limit=limit, timeout=15)
    except net.Failure: return None
    if body.startswith(b'\xff\xd8\xff'): kind = 'jpeg'
    elif body.startswith(b'\x89PNG\r\n\x1a\n'): kind = 'png'
    elif body[:4] == b'RIFF' and body[8:12] == b'WEBP': kind = 'webp'
    else: return None
    return 'data:image/' + kind + ';base64,' + base64.b64encode(body).decode()


def search(text, page):
    data = query(SEARCH, {'search': text, 'page': page})['Page']
    media = [m for m in (data.get('media') or []) if isinstance(m, dict)][:20]
    with ThreadPoolExecutor(4) as pool:
        covers = list(pool.map(lambda m: cover((m.get('coverImage') or {}).get('medium'), 96 * 1024), media))
    items, images = [], {}
    for m, image in zip(media, covers):
        item = catalog(m, image is not None); items.append(item)
        if image: images[str(item['anilistId'])] = image
    return {'items': items, 'hasNextPage': bool((data.get('pageInfo') or {}).get('hasNextPage')), 'covers': images}


def details(anilist_id):
    media = query(DETAILS, {'id': anilist_id}).get('Media')
    if not isinstance(media, dict) or media.get('id') != anilist_id: raise net.Failure('NOT_FOUND')
    image = cover((media.get('coverImage') or {}).get('large'), 360 * 1024)
    relations = []
    for edge in ((media.get('relations') or {}).get('edges') or [])[:80]:
        node = (edge or {}).get('node') or {}
        if node.get('type') != 'ANIME' or edge.get('relationType') not in RELATIONS or type(node.get('id')) is not int: continue
        titles = node.get('title') or {}
        relations.append({'anilistId': node['id'], 'title': clean(titles.get('english'), 300) or clean(titles.get('userPreferred'), 300) or 'Untitled',
                          'relation': edge['relationType'], 'format': node.get('format') if node.get('format') in FORMATS else None, 'year': year(node)})
    genres = [g for g in (clean(g, 60) for g in (media.get('genres') or [])[:20]) if g]
    return catalog(media, image is not None), plain_text(media.get('description')), genres, relations[:40], image
