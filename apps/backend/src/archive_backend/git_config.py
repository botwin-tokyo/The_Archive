"""Git backup configuration, independent of provider and storage implementations."""
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .contracts import BackendError, object_fields


def settings(value):
    object_fields(value, ('enabled', 'scope', 'branchOverrides', 'retention', 'timezone'))
    if type(value['enabled']) is not bool or value['scope'] not in ('owned', 'controlled'):
        raise BackendError('INVALID_ARGUMENT', 'Invalid Git backup scope or enablement')
    if type(value['retention']) is not int or not 1 <= value['retention'] <= 365:
        raise BackendError('INVALID_ARGUMENT', 'Git retention must be 1–365 snapshots')
    if value['timezone'] != 'UTC':
        raise BackendError('INVALID_ARGUMENT', 'This deployment schedules midnight UTC')
    try: ZoneInfo(value['timezone'])
    except ZoneInfoNotFoundError: raise BackendError('INVALID_ARGUMENT', 'Timezone data unavailable') from None
    overrides = value['branchOverrides']
    if not isinstance(overrides, dict) or len(overrides) > 1000:
        raise BackendError('INVALID_ARGUMENT', 'Invalid branch overrides')
    for key, branch in overrides.items():
        if not re.fullmatch(r'[1-9][0-9]{0,19}', key) or not isinstance(branch, str) or not 1 <= len(branch) <= 250 or branch.startswith('-') or any(c in branch for c in '\0\r\n'):
            raise BackendError('INVALID_ARGUMENT', 'Invalid repository branch override')
    return dict(value)


DEFAULTS = {'enabled': False, 'scope': 'owned', 'branchOverrides': {},
            'retention': 30, 'timezone': 'UTC'}


def parse(value):
    if value is None: return None
    object_fields(value, ('socket', 'workerUid', 'spool', 'controlSocket', 'volumeId'))
    for key in ('socket', 'spool', 'controlSocket'):
        path = value[key]
        if not isinstance(path, str) or not path.startswith('/') or path == '/' or any(p in ('', '.', '..') for p in path[1:].split('/')) or '\0' in path:
            raise BackendError('INVALID_ARGUMENT', 'Git worker paths must be canonical absolute paths')
    if type(value['workerUid']) is not int or value['workerUid'] <= 0 or value['volumeId'] != 'git':
        raise BackendError('INVALID_ARGUMENT', 'Git backup requires a dedicated worker and Git volume')
    return dict(value)
