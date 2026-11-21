"""One process group per attempt: fixed inputs and fixed spool outputs."""
import json
import os
from pathlib import Path
import resource
import sys
from . import provider


def atomic(path, value):
    pending = path.with_suffix('.tmp')
    with pending.open('w') as stream:
        json.dump(value, stream); stream.flush(); os.fsync(stream.fileno())
    os.chmod(pending, 0o640); os.replace(pending, path)


def main():
    resource.setrlimit(resource.RLIMIT_FSIZE, (8*1024**3, 8*1024**3))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    args = json.loads(sys.stdin.buffer.read(65537))
    directory = Path.cwd()
    def progress(stage, fraction=None): atomic(directory/'progress.json', {'stage': stage, 'progress':fraction})
    try:
        action = args['action']; payload = args['payload']
        if action == 'resolve': result = provider.resolve(payload)
        elif action == 'download': result = provider.download(payload, directory, progress)
        else: raise ValueError('Unknown provider task')
        atomic(directory/'result.json', {'ok': True, 'result': result})
    except Exception as exc:
        # Upstream text can contain signed source URLs/cookies. It never crosses IPC.
        atomic(directory/'result.json', {'ok': False, 'error': str(exc) if isinstance(exc,ValueError) and str(exc) in {'INVALID_URL','PLAYLIST_UNSUPPORTED','UNSUPPORTED_URL','MULTI_VIDEO_UNSUPPORTED','LIVE_OR_DRM_UNSUPPORTED','UNSUPPORTED_DURATION','UNSUPPORTED_SOURCE','SOURCE_CHANGED','FORMAT_LIMIT','NO_COMPATIBLE_FORMAT','INVALID_MEDIA','INVALID_DURATION','QUALITY_CHANGED','QUALITY_UNAVAILABLE','DISK_BUDGET','INVALID_ARTIFACT'} else 'SOURCE_UNAVAILABLE', 'category': type(exc).__name__[:80]})
        sys.exit(1)


if __name__ == '__main__': main()
