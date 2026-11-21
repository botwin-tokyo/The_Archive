"""Complete, bounded artist release enumeration using pinned spotapi pagination."""
from spotapi.artist import Artist

MAX_RELEASES = 500


def resolve_artist(identity):
    from .provider import ID
    from .media import artwork
    api = Artist()
    artist = api.get_artist(identity)['data']['artistUnion']
    if artist.get('id') != identity:
        raise ValueError('Artist identity mismatch')
    name = str(artist['profile']['name'])[:500]
    releases = []; seen = set(); offset = 0; expected = None
    while True:
        section = api.get_artist_discography(identity, section='all', offset=offset, limit=50, order='DATE_ASC')['data']['artistUnion']['discography']['all']
        total = section['totalCount']; groups = section['items']
        if type(total) is not int or not 0 < total <= MAX_RELEASES or not isinstance(groups, list):
            raise ValueError('Discography size is unsupported')
        if expected is None: expected = total
        if total != expected or not groups or offset + len(groups) > total:
            raise ValueError('Incomplete or changing discography')
        for group in groups:
            editions = group['releases']['items']
            if not editions: raise ValueError('Empty release group')
            # Spotify groups alternate editions together. Keep the primary listing;
            # deluxe/live releases in separate groups remain separate releases.
            raw = editions[0]; album_id = raw['id']
            if not isinstance(album_id, str) or not ID.fullmatch(album_id) or album_id in seen:
                raise ValueError('Invalid or repeated release page')
            seen.add(album_id)
            count = raw['tracks']['totalCount']
            if type(count) is not int or not 1 <= count <= 500: raise ValueError('Invalid album size')
            releases.append({'id': album_id, 'title': str(raw['name'])[:500], 'year': str(raw.get('date', {}).get('year', ''))[:4],
                             'trackCount': count, 'releaseType': str(raw.get('type', 'ALBUM')).lower()})
        offset += len(groups)
        if offset == expected: break
    return {'kind': 'artist', 'title': (name + ' · Discography')[:500], 'artist': name, 'year': '', 'tracks': [],
            'expectedTracks': sum(r['trackCount'] for r in releases), 'releases': releases,
            'artwork': artwork(artist.get('visuals', {}).get('avatarImage', {}).get('sources', []))}


def search_artists(query, offset):
    from .provider import item, ID
    page = Artist().query_artists(query, limit=20, offset=offset)['data']['searchV2']['artists']
    results = []
    for wrapper in page['items']:
        raw = wrapper['data']
        identity = raw.get('uri', '').split(':')[-1]
        if raw.get('__typename') != 'Artist' or not ID.fullmatch(identity): continue
        name = raw['profile']['name']
        results.append(item({'id': identity, 'name': name, 'artists': [{'name': name}], 'total_tracks': 0}, 'artist'))
    return {'items': results, 'nextOffset': offset + 20 if offset + 20 < min(1000, page['totalCount']) else None}
