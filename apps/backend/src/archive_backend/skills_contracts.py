"""Skill Library schemas shared by HTTP, MCP and service dispatch."""
import os
from .contracts import BackendError, object_fields

STRING = {'type': 'string', 'minLength': 1, 'maxLength': 256}
REV = {'type': 'integer', 'minimum': 0, 'maximum': 9007199254740991}
FILE = {'type': 'object', 'properties': {'path': {'type': 'string', 'minLength': 1, 'maxLength': 1024}, 'size': {'type': 'integer', 'minimum': 0, 'maximum': 16777216}, 'sha256': {'type': 'string', 'minLength': 64, 'maxLength': 64}}, 'required': ['path', 'size', 'sha256'], 'additionalProperties': False}
TYPES = dict(id=STRING, uploadId=STRING, requestId=STRING, slug={'type': 'string', 'minLength': 1, 'maxLength': 80}, expectedRevision=REV, revision=REV,
             query={'type': 'string', 'maxLength': 256}, cursor={'type': 'string', 'maxLength': 256}, limit={'type': 'integer', 'minimum': 1, 'maximum': 100}, trashed={'type': 'boolean'},
             path=FILE['properties']['path'], offset={'type': 'integer', 'minimum': 0, 'maximum': 16777216}, length={'type': 'integer', 'minimum': 1, 'maximum': 65536},
             files={'type': 'array', 'minItems': 1, 'maxItems': 1000, 'items': FILE},
             textFiles={'type': 'array', 'minItems': 1, 'maxItems': 100, 'items': {'type': 'object', 'properties': {'path': FILE['properties']['path'], 'text': {'type': 'string', 'maxLength': 131072}}, 'required': ['path', 'text'], 'additionalProperties': False}})
from .contracts import SKILLS_OPS as OPS


def schema(name):
    _, required, optional = OPS[name.removeprefix('skills.')]
    return {'type': 'object', 'properties': {k: TYPES[k] for k in required + optional}, 'required': list(required), 'additionalProperties': False}


def validate(value, spec):
    kind = spec['type']; valid = True
    if kind == 'object':
        object_fields(value, spec.get('required', ()), tuple(spec['properties']))
        for k, v in value.items(): validate(v, spec['properties'][k])
    elif kind == 'array':
        valid = isinstance(value, list) and spec.get('minItems', 0) <= len(value) <= spec['maxItems']
        if valid:
            for v in value: validate(v, spec['items'])
    elif kind == 'string':
        try: valid = isinstance(value, str) and spec.get('minLength', 0) <= len(value.encode('utf-8')) <= spec['maxLength']
        except UnicodeError: valid = False
    elif kind == 'integer': valid = type(value) is int and spec['minimum'] <= value <= spec['maximum']
    elif kind == 'boolean': valid = type(value) is bool
    if not valid: raise BackendError('INVALID_ARGUMENT', 'Invalid skill arguments or size limit exceeded')


def parse_config(value, volumes):
    if value is None: return None
    object_fields(value, ('enabled', 'volumeId', 'root'))
    if type(value['enabled']) is not bool or not isinstance(value['root'], str): raise BackendError('INVALID_ARGUMENT', 'Invalid skill configuration')
    root = value['root']; volume = next((v for v in volumes if v['id'] == value['volumeId']), None)
    if not volume or not os.path.isabs(root) or os.path.normpath(root) != root or '\0' in root or not root.startswith(volume['mountPoint'].rstrip('/')+'/'):
        raise BackendError('INVALID_ARGUMENT', 'Skill directory must belong to a configured data volume')
    for v in volumes:
        managed = v['rootPath'].rstrip('/')
        if root == managed or root.startswith(managed+'/') or managed.startswith(root+'/'):
            raise BackendError('INVALID_ARGUMENT', 'Skill directory must be separate from generic managed roots')
    return value
