"""Resolve operator settings for local deployment scripts and service templates.

Copy deploy/local.example.json to deploy/local.json and fill the values needed by
that deployment. ARCHIVE_DEPLOYMENT_CONFIG can select a private file elsewhere.
This module never installs services or reads runtime credentials.
"""
import json
import os
from pathlib import Path
import re
import uuid
from urllib.parse import urlsplit

_SETTINGS = None
_TOKEN = re.compile(r'@([A-Z_]+)@')


def configured(text):
    """Expand only explicit template tokens; missing settings fail closed."""
    global _SETTINGS
    if not _TOKEN.search(text):
        return text
    if _SETTINGS is None:
        path = Path(os.environ.get('ARCHIVE_DEPLOYMENT_CONFIG') or
                    Path(__file__).resolve().parents[1] / 'deploy/local.json')
        try:
            _SETTINGS = json.loads(path.read_text())
        except (OSError, ValueError):
            raise SystemExit('Provide deploy/local.json from deploy/local.example.json, or set ARCHIVE_DEPLOYMENT_CONFIG') from None
        if not isinstance(_SETTINGS, dict):
            raise SystemExit('Deployment settings must be a JSON object')

    def replace(match):
        key = match[1]
        value = _SETTINGS.get(key)
        if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 for c in value) or '@' in value:
            raise SystemExit('Set a nonempty deployment value for ' + key)
        if key.endswith(('_ROOT', '_MOUNT')):
            if not value.startswith('/') or value == '/' or any(p in ('', '.', '..') for p in value[1:].split('/')) or not re.fullmatch(r'/[A-Za-z0-9_./-]+', value):
                raise SystemExit('Use a canonical absolute directory for ' + key)
        elif key.endswith('_UUID'):
            try:
                uuid.UUID(value)
            except ValueError:
                raise SystemExit('Use the verified filesystem UUID for ' + key) from None
        elif key == 'FLEET_ENDPOINT':
            parsed = urlsplit(value)
            if parsed.scheme != 'wss' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise SystemExit('FLEET_ENDPOINT must be a wss URL without credentials, query or fragment')
        elif key == 'FLEET_NODE_ID' and not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
            raise SystemExit('FLEET_NODE_ID must be a simple node identifier')
        return value
    return _TOKEN.sub(replace, text)


def install_template(source, destination):
    """Write a rendered unit/config; never start or enable it."""
    source, destination = Path(source), Path(destination)
    if destination.is_dir():
        destination = destination / source.name
    destination.write_text(configured(source.read_text()))
    destination.chmod(0o644)


def validate_deployment(*keys):
    """Validate all settings needed by a script before its first host mutation."""
    for key in keys:
        configured("@" + key + "@")


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Render a deployment template with private operator settings; does not install or start services.')
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    install_template(args.source, args.destination)
