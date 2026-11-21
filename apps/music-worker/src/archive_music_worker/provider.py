"""Pinned spotDL public metadata adapter. No first-match text downloads."""
import copy
import re
from pathlib import Path

ID = re.compile(r'^[A-Za-z0-9]{22}$')


def client():
    from spotdl.utils.spotify import SpotifyClient
    SpotifyClient.init(client_id='', client_secret='', user_auth=False, headless=True,
                       no_cache=True, use_official_api=False)
    return SpotifyClient()


def item(raw, kind='track', album=None, ordinal=1):
    raw = raw or {}; album = album or raw.get('album') or {}
    identity = raw.get('id') or '0' * 22
    if not ID.fullmatch(identity): identity = '0' * 22
    artists = raw.get('artists') or album.get('artists') or []
    return {'kind': kind, 'id': identity, 'title': str(raw.get('name') or 'Unavailable track')[:500],
            'artist': ', '.join(str(a.get('name', '')) for a in artists)[:500],
            'year': str((raw if kind == 'album' else album).get('release_date', ''))[:4],
            'trackCount': min(500, max(0, int(raw.get('total_tracks', 1)))),
            'durationMs': max(0, int(raw.get('duration_ms') or 0)),
            'disc': max(1, int(raw.get('disc_number') or 1)),
            'number': max(1, int(raw.get('track_number') or ordinal)),
            'available': identity != '0' * 22 and not raw.get('is_local', False) and raw.get('is_playable', True) is not False}


def search(args):
    if args['kind'] == 'artist':
        from .discography import search_artists
        return search_artists(args['query'], args.get('offset', 0))
    # SpotipyFree ignores type/offset and always returns its first track page.
    # Its pinned underlying spotapi client exposes the complete search response.
    from spotapi.song import Song as PublicSearch
    offset=args.get('offset',0); kind=args['kind']
    page=PublicSearch().query_songs(args['query'],limit=20,offset=offset)['data']['searchV2'][kind+'sV2']
    results=[]
    for wrapper in page['items']:
        raw=wrapper.get('item',wrapper).get('data',{})
        if raw.get('__typename') != ('Album' if kind=='album' else 'Track'): continue
        identity=raw.get('id') or raw.get('uri','').split(':')[-1]
        if not ID.fullmatch(identity): continue
        artists=[{'name':a.get('profile',{}).get('name','')} for a in raw.get('artists',{}).get('items',[])]
        normalized={'id':identity,'name':raw.get('name'),'artists':artists,
                    'release_date':str(raw.get('date',{}).get('year','')),
                    'duration_ms':raw.get('duration',{}).get('totalMilliseconds',0),
                    'total_tracks':0 if kind=='album' else 1,
                    'is_playable':raw.get('playability',{}).get('playable',True)}
        results.append(item(normalized,kind))
    return {'items':results,'nextOffset':offset+20 if offset+20<min(1000,page['totalCount']) else None}


def resolve(args):
    if args['selection']['kind'] == 'artist':
        from .discography import resolve_artist
        return resolve_artist(args['selection']['id'])
    spotify = client(); selection = args['selection']
    if selection['kind'] == 'track':
        track = spotify.track(selection['id']); album = track['album']
        tracks = [item(track, album=album)]; expected = 1
        title = tracks[0]['title']; artist = tracks[0]['artist']
    else:
        album = spotify.album(selection['id']); expected = int(album['total_tracks'])
        if not 1 <= expected <= 500: raise ValueError('Album track limit')
        page = spotify.album_tracks(selection['id'], limit=50); rows = []; seen = set()
        while True:
            rows.extend(page['items'])
            if len(rows) > 500: raise ValueError('Album track limit')
            next_url = page.get('next')
            if not next_url: break
            if next_url in seen: raise ValueError('Repeated album page')
            seen.add(next_url); page = spotify.next(page)
        if len(rows) != expected: raise ValueError('Incomplete album metadata')
        tracks = [item(row, album=album, ordinal=i+1) for i, row in enumerate(rows)]
        title = str(album['name'])[:500]; artist = ', '.join(a['name'] for a in album['artists'])[:500]
    from .media import artwork
    preview=artwork(album.get('images',[]))
    # Internal album information is retained only by the core for deterministic naming.
    return {'title': title, 'artist': artist, 'year': str(album.get('release_date', ''))[:4],
            'kind': selection['kind'], 'tracks': tracks, 'expectedTracks': expected,
            'albumId': album['id'], 'albumTitle': str(album['name'])[:500],
            'albumArtist': ', '.join(a['name'] for a in album['artists'])[:500],
            'artistId': (album.get('artists') or [{}])[0].get('id'), 'artwork':preview}


def download(args, directory, progress):
    from spotdl.types.song import Song
    from spotdl.download.downloader import Downloader
    from spotdl.utils.config import DOWNLOADER_OPTIONS
    client(); progress('resolving-source')
    song = Song.from_url('https://open.spotify.com/track/' + args['trackId'])
    settings = copy.deepcopy(DOWNLOADER_OPTIONS)
    settings.update(output=str(Path(directory) / 'audio.{output-ext}'), format='mp3', bitrate='auto',
                    threads=1, ffmpeg='/usr/bin/ffmpeg', lyrics_providers=[],
                    audio_providers=['youtube-music', 'youtube'], overwrite='skip',
                    print_errors=False, simple_tui=True, save_file=None,
                    yt_dlp_args='--socket-timeout 20 --retries 2 --fragment-retries 2 --max-filesize 512M --no-playlist')
    progress('downloading')
    downloader = Downloader(settings=settings)
    _, output = downloader.download_song(song)
    if output is None or Path(output) != Path(directory) / 'audio.mp3':
        raise ValueError('No matching audio was downloaded')
    progress('verifying')
    from .media import verify
    artifact=verify(Path(output), args.get('durationMs', 0))
    from .lyrics import lookup, embed
    try:
        lyrics=lookup(song.name,song.artists,song.album_name,(args.get('durationMs') or song.duration*1000)/1000,artifact['durationMs']/1000)
        lyrics=embed(Path(output),lyrics,artifact['durationMs']/1000)
    except Exception:
        lyrics={'status':'failed','plain':None,'synced':None,'sourceId':None,'embedded':False}
    artifact=verify(Path(output), args.get('durationMs', 0))
    artifact['lyrics']=lyrics
    return artifact
