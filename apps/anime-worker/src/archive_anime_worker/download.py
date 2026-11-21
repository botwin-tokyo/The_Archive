"""Episode acquisition with a checkpoint bound to the exact segment manifest.

Retained segments are reused only when the manifest digest, variant and per-segment
length/SHA-256 all still match. A changed manifest discards every old segment.
"""
import hashlib
import json
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from . import anipm, hls, media, net

SEGMENT_MAX = 64 * 1024 * 1024
SUBTITLE_MAX = 4 * 1024 * 1024
ARTIFACT_MAX = 6 * 1024**3
SPOOL_MAX = 24 * 1024**3
FREE_MIN = 8 * 1024**3
CONCURRENCY = 4


def used(root):
    total = 0
    for base, _, files in os.walk(root):
        for name in files:
            try: total += os.lstat(os.path.join(base, name)).st_size
            except FileNotFoundError: pass
    return total


def budget(directory):
    spool = os.environ.get('ARCHIVE_ANIME_SPOOL', str(directory))
    if used(spool) > SPOOL_MAX or shutil.disk_usage(directory).free < FREE_MIN: raise net.Failure('DISK_BUDGET')


def atomic_json(path, value):
    pending = path.with_suffix('.tmp')
    with pending.open('w') as stream:
        json.dump(value, stream); stream.flush(); os.fsync(stream.fileno())
    os.replace(pending, path)


def digest(path):
    value = hashlib.sha256()
    with open(path, 'rb') as stream:
        while chunk := stream.read(1024 * 1024): value.update(chunk)
    return value.hexdigest()


def identify(payload):
    """The selected provider episode must still exist with the same number and requested audio."""
    data = anipm.series(payload['providerSeriesId'])
    episode = next((e for e in anipm.episodes(data) if e['id'] == payload['episodeId']), None)
    if episode is None or anipm.number_text(episode['number']) != payload['number']: raise net.Failure('EPISODE_UNAVAILABLE')
    if not episode[payload['audio']]: raise net.Failure('VARIANT_UNAVAILABLE')
    # Listed runtime in minutes (reliable even where runtimeSeconds is not); used to spot truncated uploads.
    raw = next((e for e in data.get('episodes') or [] if isinstance(e, dict) and e.get('routeId') == payload['episodeId']), {})
    runtime = raw.get('runtime')
    episode['runtimeMinutes'] = runtime if type(runtime) in (int, float) and 0 < runtime <= 360 else None
    return episode


def checkpoint(directory, identity, height):
    segments = directory / 'segments'; segments.mkdir(mode=0o750, exist_ok=True)
    path = directory / 'checkpoint.json'
    try: state = json.loads(path.read_text())
    except (OSError, ValueError): state = None
    if not isinstance(state, dict) or state.get('manifest') != identity or state.get('height') != height or not isinstance(state.get('done'), dict):
        for item in segments.iterdir(): item.unlink()
        state = {'manifest': identity, 'height': height, 'done': {}}
    # Never trust a retained file by name alone.
    for key, meta in list(state['done'].items()):
        file = segments / ('%05d.ts' % int(key))
        try: valid = file.stat().st_size == meta['size'] and digest(file) == meta['sha256']
        except (OSError, KeyError, TypeError, ValueError): valid = False
        if not valid: state['done'].pop(key)
    for item in segments.iterdir():
        if item.suffix != '.ts' or not item.stem.isdigit() or str(int(item.stem)) not in state['done']: item.unlink()
    atomic_json(path, state)
    return state


def transport_stream(body):
    """True when 0x47 sync bytes repeat at a 188-byte stride from some offset in the first packet.

    Real provider segments may carry a short partial packet, so total length need not be a
    multiple of 188. HTML, images or truncated bodies have no such stride. FFmpeg's full demux
    later rejects any remaining damage.
    """
    if len(body) < 188 * 8: return False
    for offset in range(188):
        packets = (len(body) - offset) // 188
        if packets >= 8 and all(body[offset + 188 * k] == 0x47 for k in range(min(packets, 32))) \
                and all(body[offset + 188 * k] == 0x47 for k in range(0, packets, 64)):
            return True
    return False


def fetch_segment(url, target):
    _, _, body = net.fetch(url, limit=SEGMENT_MAX, timeout=60)
    if not transport_stream(body): raise net.Failure('INVALID_MEDIA')
    pending = target.with_suffix('.part')
    with open(pending, 'wb') as stream:
        stream.write(body); stream.flush(); os.fsync(stream.fileno())
    os.replace(pending, target)
    return {'size': len(body), 'sha256': hashlib.sha256(body).hexdigest()}


def acquire(payload, directory, progress):
    progress('resolving', None)
    episode = identify(payload)
    session = anipm.session(payload['providerSeriesId'], episode['number'], payload['audio'])
    available = anipm.session_subtitles(session)
    if any(code not in available for code in payload['subtitles']): raise net.Failure('SUBTITLE_UNAVAILABLE')
    variant = hls.select(hls.variants(session['source']), payload['quality'])
    segments, duration, identity = hls.media(variant['url'])
    # A provider copy far shorter than the listed runtime is a broken or preview upload, not the episode.
    if episode['runtimeMinutes'] and duration < 0.5 * episode['runtimeMinutes'] * 60: raise net.Failure('INCOMPLETE_SOURCE')
    state = checkpoint(directory, identity, variant['height'])
    done, lock, last = state['done'], threading.Lock(), [0.0]
    budget(directory)

    def get(index):
        meta = fetch_segment(segments[index], directory / 'segments' / ('%05d.ts' % index))
        with lock:
            done[str(index)] = meta
            if time.monotonic() - last[0] >= 2:
                last[0] = time.monotonic(); budget(directory)
                atomic_json(directory / 'checkpoint.json', state); progress('downloading', 0.95 * len(done) / len(segments))

    progress('downloading', 0.95 * len(done) / len(segments))
    with ThreadPoolExecutor(CONCURRENCY) as pool:
        for _ in pool.map(get, [i for i in range(len(segments)) if str(i) not in done]): pass
    atomic_json(directory / 'checkpoint.json', state)
    progress('verifying', 0.96)
    video = directory / 'video.ts'; total = 0
    # Concatenate in order, releasing each segment once appended to bound peak spool use.
    with open(video, 'wb') as out:
        for index in range(len(segments)):
            part = directory / 'segments' / ('%05d.ts' % index)
            with open(part, 'rb') as stream: data = stream.read()
            if hashlib.sha256(data).hexdigest() != done[str(index)]['sha256']: raise net.Failure('INVALID_MEDIA')
            out.write(data); total += len(data)
            if total > ARTIFACT_MAX: raise net.Failure('INVALID_MEDIA')
        out.flush(); os.fsync(out.fileno())
    shutil.rmtree(directory / 'segments'); (directory / 'checkpoint.json').unlink()
    tracks = []
    for code in payload['subtitles']:
        _, _, body = net.fetch(available[code], limit=SUBTITLE_MAX)
        try: text = body.decode('utf-8-sig')
        except UnicodeDecodeError: raise net.Failure('INVALID_MEDIA') from None
        if not text.startswith('WEBVTT'): raise net.Failure('INVALID_MEDIA')
        path = directory / f'subtitle.{code}.vtt'; path.write_text(text, encoding='utf-8'); tracks.append((code, path))
    output = directory / 'media.partial.mp4'
    media.remux(video, tracks, payload['audio'], output)
    actual = media.verify(output, variant['height'], duration, payload['subtitles'], payload['audio'])
    video.unlink()
    for _, path in tracks: path.unlink()
    size = output.stat().st_size
    if not 0 < size <= ARTIFACT_MAX: raise net.Failure('INVALID_MEDIA')
    final = directory / 'media.mp4'; os.chmod(output, 0o640); os.replace(output, final)
    return {'filename': 'media.mp4', 'sizeBytes': str(size), 'sha256': digest(final), 'height': variant['height'], 'durationSeconds': round(actual, 3),
            'audioLanguage': 'jpn' if payload['audio'] == 'sub' else 'eng', 'subtitles': list(payload['subtitles']), 'manifest': identity}
