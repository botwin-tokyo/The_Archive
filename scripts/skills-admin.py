"""Offline Skill Library integrity check and consistent full-directory backup."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'apps/backend/src'))
from archive_backend.skills_store import SkillStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('action', choices=['verify', 'backup'])
    parser.add_argument('--destination')
    args = parser.parse_args(); root = Path(args.root)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink() or not (root/'catalog.sqlite3').is_file():
        parser.error('An existing absolute library directory with catalog is required')
    store = SkillStore(str(root))
    try:
        with store.transaction():
            if store.db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': raise RuntimeError('Catalog integrity check failed')
            count = 0
            for entry in store.rows('skill'):
                for version in entry['versions']:
                    for file in version['files']:
                        store.read(entry['id'], file['path'], version['revision']); count += 1
            if args.action == 'backup':
                if not args.destination: parser.error('backup requires --destination')
                dest = Path(args.destination)
                if not dest.is_absolute() or dest.exists() or dest.resolve().is_relative_to(root.resolve()): parser.error('Choose a new absolute destination outside the library')
                shutil.copytree(root, dest, symlinks=False)
                # No concurrent writes: writer lock remains held throughout copy.
                for directory, _, filenames in os.walk(dest, topdown=False):
                    for name in filenames:
                        fd = os.open(Path(directory)/name, os.O_RDONLY | os.O_NOFOLLOW)
                        try: os.fsync(fd)
                        finally: os.close(fd)
                    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                    try: os.fsync(fd)
                    finally: os.close(fd)
                fd = os.open(dest.parent, os.O_RDONLY | os.O_DIRECTORY)
                try: os.fsync(fd)
                finally: os.close(fd)
        print(json.dumps({'verifiedFiles': count, 'backupCreated': args.action == 'backup'}))
    finally: store.close()


if __name__ == '__main__': main()
