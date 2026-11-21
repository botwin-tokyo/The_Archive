"""Metadata tasks: AniList search, and series lookup with provider match and inventory."""
from . import anilist, anipm, hls, net


def search(payload):
    return anilist.search(payload['query'], payload['page'])


def sample(provider_series, episodes, audio):
    """Probe the first episode offering `audio` for its real heights and subtitle languages."""
    episode = next((e for e in sorted(episodes, key=lambda e: e['number']) if e[audio]), None)
    if not episode: return None
    try:
        value = anipm.session(provider_series, episode['number'], audio)
        heights = sorted({v['height'] for v in hls.variants(value['source']) if 144 <= v['height'] <= 4320}, reverse=True)[:8]
    except net.Failure:
        return None
    return {'heights': heights, 'subtitles': sorted(anipm.session_subtitles(value))} if heights else None


def lookup(payload):
    anilist_id, requested = payload['anilistId'], payload['providerSeriesId']
    catalog, description, genres, relations, cover = anilist.details(anilist_id)
    titles = list(dict.fromkeys([catalog['title']] + catalog['altTitles']))[:4]
    found = anipm.candidates(catalog, titles)
    match = data = None
    if requested:
        # An explicit choice is loaded directly; failure leaves the decision to the core.
        try:
            chosen, data = anipm.explicit(requested, catalog)
            if chosen:
                found = ([chosen] + [c for c in found if c['providerSeriesId'] != requested])[:20]; match = requested
        except net.Failure:
            data = None
    if match is None:
        match = anipm.automatic(found)
        data = anipm.series(match) if match else None
    episodes = anipm.episodes(data) if data else []
    samples = {audio: sample(match, episodes, audio) for audio in ('sub', 'dub')} if match else {'sub': None, 'dub': None}
    return {'catalog': catalog, 'description': description, 'genres': genres, 'relations': relations, 'candidates': found,
            'match': match, 'episodes': episodes, 'samples': samples, 'cover': cover}
