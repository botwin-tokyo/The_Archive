"""Stateless MCP 2025-11-25 JSON-response profile over authenticated HTTP.

The custom local bearer profile is not OAuth authorization compliance. All tools
are generated from the application capability registry, never filesystem calls.
"""
import json
from .contracts import CAPABILITIES, BackendError, translate_error
from .video_contracts import tool_schema as video_schema
from .anime_contracts import tool_schema as anime_schema
from .vault_contracts import schema as vault_schema
from .skills_contracts import schema as skills_schema
from .phone_transfer_contracts import SCHEMAS as PHONE_SCHEMAS
from .radio_contracts import tool_schema as radio_schema
from .music_contracts import tool_schema

PROTOCOL_VERSION = '2025-11-25'


def tool_list():
    return [{'name': c.name, 'description': ('Password Manager: '+c.name+'. Use vault.help for the credential workflow; secret fields are returned only by reveal/generate. Preserve exact values and request IDs.' if c.name.startswith('vault.') else 'The Archive '+c.name),
             'inputSchema': PHONE_SCHEMAS[c.name.split('.')[1]] if c.name.startswith('phone_transfer.') else skills_schema(c.name) if c.name.startswith('skills.') else vault_schema(c.name) if c.name.startswith('vault.') else radio_schema(c) if c.name.startswith('radio.') else anime_schema(c) if c.name.startswith('anime.') else video_schema(c) if c.name.startswith('video.') else tool_schema(c) if c.name.startswith('music.') else {'type': 'object', 'properties': {k: {} for k in c.required + c.optional},
                             'required': list(c.required), 'additionalProperties': False},
             'annotations': {'readOnlyHint': not c.mutation, 'destructiveHint': c.confirmation}}
            for c in CAPABILITIES.values()]


def rpc_error(request_id, code, message):
    return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': code, 'message': message}}


def dispatch(service, token, message):
    """Return a JSON-RPC object, or None for accepted notifications/responses.

    Authentication failures propagate to the HTTP layer. Tool execution errors
    are MCP tool results with isError, separate from malformed protocol errors.
    """
    service.authenticate(token)
    if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
        return rpc_error(None, -32600, 'Invalid request')
    request_id = message.get('id')
    if 'id' in message and (type(request_id) not in (str, int) or isinstance(request_id, bool)):
        return rpc_error(None, -32600, 'Invalid request ID')
    method = message.get('method')
    if method is None and ('result' in message or 'error' in message) and 'id' in message:
        return None
    if not isinstance(method, str):
        return rpc_error(request_id, -32600, 'Invalid request')
    params = message.get('params', {})
    if not isinstance(params, dict):
        return rpc_error(request_id, -32602, 'Invalid parameters')
    if 'id' not in message:
        if method.startswith('notifications/'):
            # Protocol cancellation does not cancel a durable application job.
            return None
        return rpc_error(None, -32600, 'Request ID required')
    if method == 'initialize':
        if not isinstance(params.get('protocolVersion'), str) or not isinstance(params.get('capabilities'), dict) or not isinstance(params.get('clientInfo'), dict):
            return rpc_error(request_id, -32602, 'Invalid initialization')
        result = {'protocolVersion': PROTOCOL_VERSION, 'capabilities': {'tools': {'listChanged': False}, 'resources': {'listChanged':False,'subscribe':False}},
                  'serverInfo': {'name': 'archive-console', 'version': '0.2.0'}}
    elif method == 'ping':
        result = {}
    elif method == 'resources/list':
        result={'resources':[{'uri':'archive://skills/phone-transfer-hold','name':'Phone Transfer Hold workflow','mimeType':'text/plain','description':'Send files, messages and links; resume uploads; synchronize phone receipts without push'},{'uri':'archive://skills/skill-library','name':'Skill Library workflow','mimeType':'text/plain','description':'Find, read and publish agent skill packages through authenticated API/MCP'}, {'uri':'archive://skills/password-manager','name':'Password Manager workflow','mimeType':'text/plain','description':'Authenticated vault selection, reveal, writes and recovery instructions'}]}
    elif method == 'resources/read':
        if params.get('uri') not in ('archive://skills/password-manager','archive://skills/skill-library','archive://skills/phone-transfer-hold'): return rpc_error(request_id,-32602,'Unknown resource')
        value=service.call(token,{'version':1,'capability':'phone_transfer.help' if params['uri']=='archive://skills/phone-transfer-hold' else 'skills.help' if params['uri']=='archive://skills/skill-library' else 'vault.help','arguments':{}})
        result={'contents':[{'uri':params['uri'],'mimeType':'text/plain','text':value['instructions']}]}
    elif method == 'tools/list':
        if params.get('cursor'):
            return rpc_error(request_id, -32602, 'Unsupported cursor')
        result = {'tools': tool_list()}
    elif method == 'tools/call':
        name = params.get('name')
        if not isinstance(name, str) or name not in CAPABILITIES or not isinstance(params.get('arguments', {}), dict):
            return rpc_error(request_id, -32602, 'Unknown tool or invalid arguments')
        try:
            value = service.call(token, {'version': 1, 'capability': name, 'arguments': params.get('arguments', {})})
            value = value if isinstance(value, dict) else {'value': value}
            result = {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False, allow_nan=False)}],
                      'structuredContent': value, 'isError': False}
        except Exception as exc:
            safe = translate_error(exc).envelope()
            result = {'content': [{'type': 'text', 'text': json.dumps(safe)}], 'structuredContent': {'error': safe}, 'isError': True}
    else:
        return rpc_error(request_id, -32601, 'Method not found')
    return {'jsonrpc': '2.0', 'id': request_id, 'result': result}
