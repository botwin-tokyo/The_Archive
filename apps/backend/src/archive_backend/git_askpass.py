#!/usr/bin/python3
"""Git child credential reader. Output goes only to Git's private askpass pipe."""
import os
import sys
if __name__ == '__main__':
    prompt=sys.argv[1] if len(sys.argv)>1 else ''
    if 'https://github.com' not in prompt and 'https://x-access-token@github.com' not in prompt:
        sys.exit(1)
    if 'Username' in prompt:print('x-access-token')
    elif 'Password' in prompt:
        from archive_backend.git_provider import read_secret
        print(read_secret(os.environ['ARCHIVE_GIT_SECRET']))
    else:sys.exit(1)
