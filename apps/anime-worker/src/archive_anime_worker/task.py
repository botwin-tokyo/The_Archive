"""One process group per attempt: fixed inputs on stdin, fixed spool outputs in the working directory."""
import json
import os
from pathlib import Path
import resource
import sys

# Codes allowed across IPC. Upstream text can contain signed URLs, so it never crosses.
CODES = {'RATE_LIMITED', 'VARIANT_UNAVAILABLE', 'QUALITY_UNAVAILABLE', 'SUBTITLE_UNAVAILABLE', 'EPISODE_UNAVAILABLE', 'UNSUPPORTED_STREAM',
         'UNSUPPORTED_PLAYLIST', 'INVALID_MEDIA', 'INCOMPLETE_SOURCE', 'DISK_BUDGET', 'SOURCE_CHANGED', 'NOT_FOUND', 'INVALID_METADATA'}
ALIASES = {'SEPARATE_AUDIO_UNSUPPORTED': 'UNSUPPORTED_STREAM', 'SEGMENT_LIMIT': 'UNSUPPORTED_PLAYLIST', 'RESPONSE_TOO_LARGE': 'INVALID_MEDIA'}


def atomic(path, value):
    pending = path.with_suffix('.tmp')
    with pending.open('w') as stream:
        json.dump(value, stream); stream.flush(); os.fsync(stream.fileno())
    os.chmod(pending, 0o640); os.replace(pending, path)


def code(error, action):
    from . import net
    if not isinstance(error, net.Failure): return 'SOURCE_UNAVAILABLE'
    value = ALIASES.get(str(error), str(error))
    # An expired or missing segment URL is retried with a fresh session and the same checkpoint.
    if value == 'NOT_FOUND' and action == 'download': return 'SOURCE_UNAVAILABLE'
    return value if value in CODES else 'SOURCE_UNAVAILABLE'


def main():
    resource.setrlimit(resource.RLIMIT_FSIZE, (7 * 1024**3, 7 * 1024**3))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    args = json.loads(sys.stdin.buffer.read(65537))
    directory = Path.cwd(); action = args.get('action')
    def progress(stage, fraction=None): atomic(directory / 'progress.json', {'stage': stage, 'progress': fraction})
    try:
        from . import download, metadata
        if action == 'search': result = metadata.search(args['payload'])
        elif action == 'lookup': result = metadata.lookup(args['payload'])
        elif action == 'download': result = download.acquire(args['payload'], directory, progress)
        else: raise ValueError('Unknown task')
        atomic(directory / 'result.json', {'ok': True, 'result': result})
    except Exception as exc:
        atomic(directory / 'result.json', {'ok': False, 'error': code(exc, action), 'category': type(exc).__name__[:80]})
        sys.exit(1)


if __name__ == '__main__': main()
