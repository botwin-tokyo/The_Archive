"""Anime DTO validation, deterministic names and MCP schemas, without provider imports."""
import math
import re
from .contracts import BackendError, object_fields, path
from .music_contracts import label, public

MAX_EPISODES_PER_JOB = 200
MAX_EPISODE_BYTES = 6 * 1024**3
AUDIO = ('sub', 'dub')
SUCCESS = frozenset(('completed', 'already-present'))
TERMINAL = SUCCESS | {'failed', 'conflict', 'unavailable', 'blocked-auth', 'canceled'}
ACTIVE = ('resolving', 'downloading', 'verifying')
SUBTITLE_LANGUAGES = ('en', 'es', 'pt', 'fr', 'de', 'it', 'ru', 'ar', 'ja', 'zh', 'ko', 'id', 'th', 'vi', 'pl', 'tr', 'hi', 'ms')
FORMATS = ('TV', 'TV_SHORT', 'MOVIE', 'SPECIAL', 'OVA', 'ONA', 'MUSIC')
PROVIDER_SERIES = re.compile(r'^[a-z]{2,20}/[a-z0-9]+(?:-[a-z0-9]+)*-[0-9]{1,12}$')
EPISODE_ID = re.compile(r'^[A-Za-z0-9._-]{1,200}$')
EPISODE_NUMBER = re.compile(r'^(0|[1-9][0-9]{0,4})(\.[0-9]{1,2})?$')
QUALITY = re.compile(r'^(best|[1-9][0-9]{2,3}p)$')
COVER = re.compile(r'^data:image/(jpeg|png|webp);base64,[A-Za-z0-9+/=]+$')
SHA256 = re.compile(r'^[a-f0-9]{64}$')
RELATIONS = ('ADAPTATION', 'PREQUEL', 'SEQUEL', 'PARENT', 'SIDE_STORY', 'CHARACTER', 'SUMMARY', 'ALTERNATIVE', 'SPIN_OFF', 'OTHER', 'SOURCE', 'COMPILATION', 'CONTAINS')


def invalid(message='The anime provider returned incompatible metadata'):
    return BackendError('INVALID_RESPONSE', message)


def text(value, maximum=500, *, optional=False):
    if value is None and optional: return None
    if not isinstance(value, str) or len(value) > maximum or '\0' in value: raise invalid()
    return value


def integer(value, low, high, *, optional=False):
    if value is None and optional: return None
    if type(value) is not int or not low <= value <= high: raise invalid()
    return value


def anilist_id(value):
    if type(value) is not int or not 1 <= value <= 10**9:
        raise BackendError('INVALID_ARGUMENT', 'A valid AniList series ID is required')
    return value


def provider_series(value):
    if not isinstance(value, str) or not PROVIDER_SERIES.fullmatch(value):
        raise BackendError('INVALID_ARGUMENT', 'A valid provider series identity is required')
    return value


def episode_number(value):
    """Provider numbers become canonical decimal strings, never floats or list positions."""
    if type(value) is int and 0 <= value <= 99999: return str(value)
    if type(value) is float and math.isfinite(value) and 0 <= value <= 99999:
        normalized = ('%.2f' % value).rstrip('0').rstrip('.')
        if float(normalized) == value: return normalized
    if isinstance(value, str) and EPISODE_NUMBER.fullmatch(value): return value
    raise invalid('Invalid episode number')


def catalog_item(value):
    object_fields(value, ('anilistId', 'malId', 'title', 'altTitles', 'year', 'format', 'status', 'episodes', 'hasCover'))
    integer(value['anilistId'], 1, 10**9); integer(value['malId'], 1, 10**9, optional=True)
    text(value['title'], 300)
    if not isinstance(value['altTitles'], list) or len(value['altTitles']) > 8: raise invalid()
    for title in value['altTitles']: text(title, 300)
    integer(value['year'], 1900, 2200, optional=True); integer(value['episodes'], 0, 99999, optional=True)
    if value['format'] is not None and value['format'] not in FORMATS: raise invalid()
    text(value['status'], 40, optional=True)
    if type(value['hasCover']) is not bool: raise invalid()
    return value


def search_result(value):
    object_fields(value, ('items', 'hasNextPage', 'covers'))
    if not isinstance(value['items'], list) or len(value['items']) > 20 or type(value['hasNextPage']) is not bool: raise invalid()
    items = [catalog_item(v) for v in value['items']]
    return items, value['hasNextPage'], covers(value['covers'], {v['anilistId'] for v in items})


def covers(value, allowed):
    if not isinstance(value, dict) or len(value) > 21: raise invalid()
    result = {}
    for key, data in value.items():
        if not re.fullmatch(r'[1-9][0-9]{0,9}', key) or int(key) not in allowed: raise invalid()
        if not isinstance(data, str) or len(data) > 256 * 1024 or not COVER.fullmatch(data): raise invalid('Invalid cover preview')
        result[int(key)] = data
    return result


def candidate(value):
    object_fields(value, ('providerSeriesId', 'title', 'year', 'type', 'episodeCount', 'anilistId', 'malId', 'evidence'))
    provider_series(value['providerSeriesId']); text(value['title'], 300); text(value['type'], 40, optional=True)
    integer(value['year'], 1900, 2200, optional=True); integer(value['episodeCount'], 0, 99999, optional=True)
    integer(value['anilistId'], 1, 10**10, optional=True); integer(value['malId'], 1, 10**9, optional=True)
    if value['evidence'] not in ('anilist-id', 'mal-id', 'title'): raise invalid()
    return value


def episode(value):
    object_fields(value, ('id', 'number', 'title', 'sub', 'dub'))
    if not isinstance(value['id'], str) or not EPISODE_ID.fullmatch(value['id']): raise invalid('Invalid episode identity')
    value['number'] = episode_number(value['number']); text(value['title'], 300, optional=True)
    if type(value['sub']) is not bool or type(value['dub']) is not bool: raise invalid()
    return value


def sample(value):
    if value is None: return None
    object_fields(value, ('heights', 'subtitles'))
    if not isinstance(value['heights'], list) or not 1 <= len(value['heights']) <= 8: raise invalid()
    for height in value['heights']: integer(height, 144, 4320)
    if not isinstance(value['subtitles'], list) or len(value['subtitles']) > len(SUBTITLE_LANGUAGES) or any(s not in SUBTITLE_LANGUAGES for s in value['subtitles']): raise invalid()
    return {'heights': sorted(set(value['heights']), reverse=True), 'subtitles': sorted(set(value['subtitles']), key=SUBTITLE_LANGUAGES.index)}


def lookup_result(value, requested_anilist):
    """Validate a worker lookup: catalog details, provider candidates/match and episode inventory."""
    object_fields(value, ('catalog', 'description', 'genres', 'relations', 'candidates', 'match', 'episodes', 'samples', 'cover'))
    catalog = catalog_item(value['catalog'])
    if catalog['anilistId'] != requested_anilist: raise invalid('Catalog identity did not match the requested series')
    description = text(value['description'], 6000, optional=True)
    if not isinstance(value['genres'], list) or len(value['genres']) > 20: raise invalid()
    for genre in value['genres']: text(genre, 60)
    if not isinstance(value['relations'], list) or len(value['relations']) > 40: raise invalid()
    for relation in value['relations']:
        object_fields(relation, ('anilistId', 'title', 'relation', 'format', 'year'))
        integer(relation['anilistId'], 1, 10**9); text(relation['title'], 300); integer(relation['year'], 1900, 2200, optional=True)
        if relation['relation'] not in RELATIONS or relation['format'] is not None and relation['format'] not in FORMATS: raise invalid()
    if not isinstance(value['candidates'], list) or len(value['candidates']) > 20: raise invalid()
    candidates = [candidate(c) for c in value['candidates']]
    match = value['match']
    if match is not None and match not in {c['providerSeriesId'] for c in candidates}: raise invalid('Provider match is not one of the candidates')
    episodes = value['episodes']
    if match is None:
        if episodes != []: raise invalid()
    elif not isinstance(episodes, list) or len(episodes) > 5000: raise invalid('Too many episodes')
    episodes = [episode(e) for e in episodes]
    if len({e['id'] for e in episodes}) != len(episodes) or len({e['number'] for e in episodes}) != len(episodes): raise invalid('Repeated provider episode')
    object_fields(value['samples'], (), AUDIO)
    samples = {audio: sample(value['samples'].get(audio)) for audio in AUDIO}
    cover = value['cover']
    if cover is not None and (not isinstance(cover, str) or len(cover) > 512 * 1024 or not COVER.fullmatch(cover)): raise invalid('Invalid cover preview')
    return {'catalog': catalog, 'description': description, 'genres': value['genres'], 'relations': value['relations'],
            'candidates': candidates, 'match': match, 'episodes': episodes, 'samples': samples}, cover


def artifact(value, audio, quality, subtitles):
    object_fields(value, ('filename', 'sizeBytes', 'sha256', 'height', 'durationSeconds', 'audioLanguage', 'subtitles', 'manifest'))
    if value['filename'] != 'media.mp4' or not isinstance(value['sha256'], str) or not SHA256.fullmatch(value['sha256']) or not isinstance(value['manifest'], str) or not SHA256.fullmatch(value['manifest']): raise invalid('Invalid episode artifact')
    if not isinstance(value['sizeBytes'], str) or not re.fullmatch(r'[1-9][0-9]{0,11}', value['sizeBytes']) or int(value['sizeBytes']) > MAX_EPISODE_BYTES: raise invalid('Invalid episode size')
    integer(value['height'], 144, 4320)
    if quality != 'best' and value['height'] != int(quality[:-1]): raise invalid('Episode quality changed')
    duration = value['durationSeconds']
    if type(duration) not in (int, float) or not math.isfinite(duration) or not 0 < duration <= 6 * 3600: raise invalid('Invalid episode duration')
    if value['audioLanguage'] != ('jpn' if audio == 'sub' else 'eng'): raise invalid('Episode audio language changed')
    if value['subtitles'] != subtitles: raise invalid('Episode subtitles changed')
    return value


def number_label(number):
    whole, _, fraction = number.partition('.')
    return whole.zfill(3) + ('.' + fraction if fraction else '')


def folder(destination, title, anilist):
    return path('/'.join(p for p in (destination['path'], f"{label(title, 150)} [AL-{anilist}]") if p))


def target(series_folder, number, audio):
    result = series_folder + f"/Episode {number_label(number)} [{audio}].mp4"
    if len(result.encode()) > 1024: raise BackendError('INVALID_PATH', 'Anime destination is too deep')
    return path(result)


def tool_schema(capability):
    """Discoverable nested input schemas for generic MCP clients."""
    string = {'type': 'string', 'minLength': 1, 'maxLength': 256}
    location = {'type': 'object', 'properties': {'driveId': string, 'path': {'type': 'string', 'maxLength': 1024}}, 'required': ['driveId', 'path'], 'additionalProperties': False,
                'description': 'Existing File Storage folder, as a logical drive-relative path'}
    types = {
        'id': string, 'requestId': {**string, 'description': 'Client-generated idempotency key; reuse it to retry the same request'},
        'query': {'type': 'string', 'minLength': 1, 'maxLength': 200, 'description': 'Anime title to search on AniList'},
        'page': {'type': 'integer', 'minimum': 1, 'maximum': 50},
        'anilistId': {'type': 'integer', 'minimum': 1, 'maximum': 10**9, 'description': 'AniList media ID from anime.search'},
        'providerSeriesId': {'type': 'string', 'pattern': PROVIDER_SERIES.pattern, 'description': 'Explicit provider candidate from a previous lookup, used to correct an ambiguous or wrong match'},
        'seriesId': {**string, 'description': 'Completed anime.series.lookup record ID'}, 'seriesRevision': {'type': 'integer', 'minimum': 1},
        'episodeIds': {'type': 'array', 'minItems': 1, 'maxItems': MAX_EPISODES_PER_JOB, 'uniqueItems': True, 'items': {'type': 'string', 'pattern': EPISODE_ID.pattern}, 'description': 'Provider episode IDs from the lookup inventory'},
        'audio': {'enum': list(AUDIO), 'description': 'sub = Japanese audio, dub = English audio'},
        'quality': {'type': 'string', 'pattern': QUALITY.pattern, 'description': 'best, or an exact sampled height such as 1080p'},
        'subtitles': {'type': 'array', 'maxItems': len(SUBTITLE_LANGUAGES), 'uniqueItems': True, 'items': {'enum': list(SUBTITLE_LANGUAGES)}, 'description': 'Selectable subtitle tracks to include'},
        'destination': location, 'planId': string, 'planRevision': {'type': 'integer', 'minimum': 1}, 'revision': {'type': 'integer', 'minimum': 1},
        'cursor': string, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
        'action': {'enum': ['pause', 'resume', 'cancel', 'retry-failed']},
    }
    return {'type': 'object', 'properties': {key: types[key] for key in capability.required + capability.optional}, 'required': list(capability.required), 'additionalProperties': False}
