"""Offline remux from validated local inputs, then independent container/stream verification."""
import json
import math
import subprocess
from . import net

FFMPEG = '/usr/bin/ffmpeg'
FFPROBE = '/usr/bin/ffprobe'
ISO = {'en': 'eng', 'es': 'spa', 'pt': 'por', 'fr': 'fre', 'de': 'ger', 'it': 'ita', 'ru': 'rus', 'ar': 'ara', 'ja': 'jpn', 'zh': 'chi', 'ko': 'kor',
       'id': 'ind', 'th': 'tha', 'vi': 'vie', 'pl': 'pol', 'tr': 'tur', 'hi': 'hin', 'ms': 'may'}


def run(arguments, timeout):
    # No network protocols: every input is a local file named by the worker, never by the provider.
    try:
        return subprocess.run(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout, check=True).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        raise net.Failure('INVALID_MEDIA') from None


def remux(video, subtitles, audio, output):
    """Copy H.264/AAC from MPEG-TS into MP4 and convert WebVTT tracks to mov_text. No transcoding."""
    command = [FFMPEG, '-nostdin', '-v', 'error', '-y', '-protocol_whitelist', 'file', '-f', 'mpegts', '-i', str(video)]
    for _, path in subtitles:
        command += ['-protocol_whitelist', 'file', '-f', 'webvtt', '-i', str(path)]
    command += ['-map', '0:v:0', '-map', '0:a:0']
    for index in range(len(subtitles)): command += ['-map', f'{index + 1}:0']
    command += ['-c:v', 'copy', '-c:a', 'copy', '-c:s', 'mov_text', '-metadata:s:a:0', 'language=' + ('jpn' if audio == 'sub' else 'eng')]
    for index, (code, _) in enumerate(subtitles): command += [f'-metadata:s:s:{index}', 'language=' + ISO[code]]
    command += ['-movflags', '+faststart', '-f', 'mp4', str(output)]
    run(command, 1800)


def verify(path, height, duration, subtitles, audio):
    """Require one H.264 stream at the selected height, one tagged AAC stream, the selected subtitle tracks and a plausible duration."""
    raw = run([FFPROBE, '-v', 'error', '-show_entries', 'format=format_name,duration:stream=codec_type,codec_name,height:stream_tags=language', '-of', 'json', str(path)], 60)
    if len(raw) > 65536: raise net.Failure('INVALID_MEDIA')
    try:
        data = json.loads(raw); streams = data['streams']; actual = float(data['format']['duration'])
        formats = data['format']['format_name'].split(',')
    except (ValueError, KeyError, TypeError): raise net.Failure('INVALID_MEDIA') from None
    video = [s for s in streams if s.get('codec_type') == 'video']; sound = [s for s in streams if s.get('codec_type') == 'audio']
    text = [s for s in streams if s.get('codec_type') == 'subtitle']
    if 'mp4' not in formats or len(streams) != 2 + len(subtitles) or len(video) != 1 or len(sound) != 1 or len(text) != len(subtitles): raise net.Failure('INVALID_MEDIA')
    if video[0].get('codec_name') != 'h264' or sound[0].get('codec_name') != 'aac' or any(s.get('codec_name') != 'mov_text' for s in text): raise net.Failure('INVALID_MEDIA')
    # Quality is the provider's variant label, bound by manifest identity. Coded height may differ
    # (e.g. an anamorphic 720x576 master labeled 480p) but must be plausible for that label.
    coded = video[0].get('height')
    if type(coded) is not int or not 0.6 * height <= coded <= 1.4 * height: raise net.Failure('QUALITY_UNAVAILABLE')
    if (sound[0].get('tags') or {}).get('language') != ('jpn' if audio == 'sub' else 'eng'): raise net.Failure('INVALID_MEDIA')
    if [(s.get('tags') or {}).get('language') for s in text] != [ISO[c] for c in subtitles]: raise net.Failure('INVALID_MEDIA')
    if not math.isfinite(actual) or actual <= 0 or abs(actual - duration) > max(3.0, duration * 0.01): raise net.Failure('INVALID_MEDIA')
    # Demux every packet once (no decoding) so a damaged container fails here, not in the library.
    run([FFMPEG, '-nostdin', '-v', 'error', '-xerror', '-protocol_whitelist', 'file', '-i', str(path), '-map', '0', '-c', 'copy', '-f', 'null', '-'], 600)
    return actual
