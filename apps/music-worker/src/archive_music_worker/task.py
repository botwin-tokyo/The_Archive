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
    resource.setrlimit(resource.RLIMIT_FSIZE, (512*1024*1024, 512*1024*1024))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    args = json.loads(sys.stdin.buffer.read(65537))
    directory = Path.cwd()
    def progress(stage): atomic(directory/'progress.json', {'stage': stage})
    try:
        action = args['action']; payload = args['payload']
        if action == 'search': result = provider.search(payload)
        elif action == 'resolve': result = provider.resolve(payload)
        elif action == 'lyrics':
            from .lyrics import for_track
            result = for_track(payload)
        elif action == 'download': result = provider.download(payload, directory, progress)
        else: raise ValueError('Unknown provider task')
        atomic(directory/'result.json', {'ok': True, 'result': result})
    except Exception as exc:
        # Upstream text can contain signed source URLs/cookies. It never crosses IPC.
        atomic(directory/'result.json', {'ok': False, 'error': 'SOURCE_UNAVAILABLE', 'category': type(exc).__name__[:80]})
        sys.exit(1)


if __name__ == '__main__': main()
