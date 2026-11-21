"""Strict routing for the Skill Library; no filesystem access in this adapter."""
import re
from urllib.parse import parse_qs
from .contracts import BackendError


def query(value):
    try:
        result = parse_qs(value, keep_blank_values=True, strict_parsing=True, max_num_fields=8)
        if any(len(v) != 1 for v in result.values()): raise ValueError()
        result = {k: v[0] for k, v in result.items()}
        for key in ('revision', 'limit', 'offset', 'length'):
            if key in result:
                if not re.fullmatch('[0-9]{1,16}', result[key]): raise ValueError()
                result[key] = int(result[key])
        if 'trashed' in result:
            if result['trashed'] not in ('true', 'false'): raise ValueError()
            result['trashed'] = result['trashed'] == 'true'
        return result
    except ValueError: raise BackendError('INVALID_ARGUMENT', 'Invalid skill query') from None


def route(method, path, params, body):
    routes = {('GET', '/skills/v1/status'): 'status', ('GET', '/skills/v1/help'): 'help', ('GET', '/skills/v1/skills'): 'list', ('POST', '/skills/v1/skills/put'): 'put', ('POST', '/skills/v1/uploads'): 'uploadBegin'}
    op = routes.get((method, path)); args = dict(params if method == 'GET' else body)
    if method != 'GET' and params: raise BackendError('INVALID_ARGUMENT', 'Unexpected query fields')
    if not op:
        match = re.fullmatch(r'/skills/v1/(skills|uploads)/([a-zA-Z0-9-]{1,256})(?:/(document|publish|trash|restore))?', path)
        if match:
            area, ident, action = match.groups()
            op = ({('GET', None): 'get', ('GET', 'document'): 'document', ('POST', 'trash'): 'trash', ('POST', 'restore'): 'restore'} if area == 'skills' else {('GET', None): 'uploadStatus', ('POST', 'publish'): 'uploadPublish', ('DELETE', None): 'uploadCancel'}).get((method, action))
            key = 'id' if area == 'skills' else 'uploadId'
            if key in args: raise BackendError('INVALID_ARGUMENT', 'Identity belongs in the route')
            args[key] = ident
    if not op: raise BackendError('NOT_FOUND', 'Unknown skill route')
    return {'version': 1, 'capability': 'skills.'+op, 'arguments': args}
