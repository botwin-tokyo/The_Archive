"""Pure versioned public contracts. No transport or infrastructure imports."""
from dataclasses import dataclass
from typing import Any
import hashlib
import json
import re
import uuid

SCHEMA_VERSION = 1
MAX_BYTES = 2**63 - 1
CHUNK_BYTES = 1024 * 1024

class BackendError(Exception):
    def __init__(self, code: str, message: str = '', retryable: bool = False, *, details=None, recovery=None):
        self.code = code
        self.correlation_id = str(uuid.uuid4())
        self.message = message or code.replace('_', ' ').capitalize()
        self.retryable = retryable
        self.details = details or {}
        self.recovery = recovery or (['retry'] if retryable else ['refresh'])
        super().__init__(self.message)

    def envelope(self, correlation_id=None):
        return {'code': self.code, 'message': self.message[:500], 'retryable': self.retryable,
                'correlationId': correlation_id or self.correlation_id, 'recovery': self.recovery}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def object_fields(value, required=(), optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise BackendError('INVALID_ARGUMENT', 'Missing, unknown or malformed fields')
    return value


def identifier(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or '\0' in value:
        raise BackendError('INVALID_ARGUMENT', 'Invalid identifier')
    return value


def path(value, *, allow_root=True):
    if not isinstance(value, str) or len(value.encode('utf-8', 'surrogatepass')) > 4096:
        raise BackendError('INVALID_PATH', 'Invalid logical path')
    if value == '' and allow_root:
        return value
    parts = value.split('/')
    if len(parts) > 128 or any(not p or p in ('.', '..') or p.startswith('.archive-') or len(p.encode('utf-8', 'surrogatepass')) > 255 for p in parts) or '\\' in value or '\0' in value:
        raise BackendError('INVALID_PATH', 'Invalid logical path')
    try:
        value.encode('utf-8', 'strict')
    except UnicodeError:
        raise BackendError('INVALID_PATH', 'Invalid UTF-8 path') from None
    return value


def name(value):
    path(value, allow_root=False)
    if '/' in value or not value.strip():
        raise BackendError('INVALID_PATH', 'A single nonempty name is required')
    return value


def bytes_value(value):
    if not isinstance(value, str) or not re.fullmatch(r'0|[1-9][0-9]{0,18}', value) or int(value) > MAX_BYTES:
        raise BackendError('INVALID_ARGUMENT', 'Expected a bounded decimal byte string')
    return int(value)


def location(value):
    object_fields(value, ('driveId', 'path'))
    identifier(value['driveId']); path(value['path'])
    return value


def entry_ref(value):
    object_fields(value, ('driveId', 'path', 'id', 'revision'))
    identifier(value['driveId']); identifier(value['id']); path(value['path'])
    if type(value['revision']) is not int or not 0 <= value['revision'] <= 2**53 - 1:
        raise BackendError('INVALID_ARGUMENT', 'Invalid entry revision')
    return value

@dataclass(frozen=True)
class Capability:
    name: str
    scope: str
    required: tuple = ()
    optional: tuple = ()
    mutation: bool = False
    owner_only: bool = False
    confirmation: bool = False

# One registry drives API, MCP tool schemas and application dispatch.
CAPABILITIES = {c.name: c for c in [
    Capability('radio.status','radio',(),(),mutation=False),
    Capability('radio.tracks.list','radio',('location',),('query', 'cursor', 'limit'),mutation=False),
    Capability('radio.batches.create','radio',('requestId', 'stationId', 'name'),(),mutation=True),
    Capability('radio.batches.add','radio',('requestId', 'batchId', 'revision', 'tracks'),(),mutation=True),
    Capability('radio.batches.remove','radio',('requestId', 'batchId', 'revision', 'itemIds'),(),mutation=True),
    Capability('radio.batches.reorder','radio',('requestId', 'batchId', 'revision', 'itemIds'),(),mutation=True),
    Capability('radio.batches.list','radio',(),('cursor', 'limit'),mutation=False),
    Capability('radio.batches.get','radio',('batchId',),('cursor', 'limit'),mutation=False),
    Capability('radio.batches.push','radio',('requestId', 'batchId', 'revision'),(),mutation=True),
    Capability('radio.batches.control','radio',('requestId', 'batchId', 'revision', 'action'),(),mutation=True),
    Capability('radio.requests.reconcile','radio',('requestId', 'operation'),(),mutation=False),
    Capability('anime.status','read',owner_only=True),
    Capability('anime.search','read',('requestId','query'),('page',),mutation=True,owner_only=True),
    Capability('anime.search.get','read',('id',),owner_only=True),
    Capability('anime.series.lookup','read',('requestId','anilistId'),('providerSeriesId',),mutation=True,owner_only=True),
    Capability('anime.series.get','read',('id',),owner_only=True),
    Capability('anime.cover.get','read',('anilistId',),owner_only=True),
    Capability('anime.resolve','read',('requestId','seriesId','seriesRevision','episodeIds','audio','quality','subtitles','destination'),mutation=True,owner_only=True),
    Capability('anime.plan.get','read',('id',),owner_only=True),
    Capability('anime.enqueue','write',('requestId','planId','planRevision'),mutation=True,owner_only=True),
    Capability('anime.reconcile','read',('requestId',),owner_only=True),
    Capability('anime.jobs.list','read',(),('cursor','limit'),owner_only=True),
    Capability('anime.jobs.get','read',('id',),owner_only=True),
    Capability('anime.jobs.control','write',('id','revision','action'),mutation=True,owner_only=True),
    Capability('video.status','read',owner_only=True),
    Capability('video.resolve','write',('requestId','url'),mutation=True,owner_only=True),
    Capability('video.plan.get','read',('id',),owner_only=True),
    Capability('video.enqueue','write',('requestId','planId','planRevision','choiceId','destination'),mutation=True,owner_only=True),
    Capability('video.jobs.list','read',(),('cursor','limit'),owner_only=True),
    Capability('video.jobs.get','read',('id',),owner_only=True),
    Capability('video.jobs.control','write',('id','revision','action'),mutation=True,owner_only=True),
    Capability('gitBackup.status','read',owner_only=True),
    Capability('gitBackup.configure','settings',('settings',),mutation=True,owner_only=True),
    Capability('gitBackup.retry','write',('requestId',),mutation=True,owner_only=True),
    Capability('gitBackup.run','write',('requestId',),mutation=True,owner_only=True),
    Capability('gitBackup.reconcile','read',('requestId',),owner_only=True),
    Capability('gitBackup.cancel','write',mutation=True,owner_only=True),
    Capability('gitBackup.preview','read',owner_only=True),
    Capability('music.lyrics.status','read',owner_only=True),
    Capability('music.lyrics.backfill','write',('requestId',),mutation=True,owner_only=True),
    Capability('music.artwork.get','read',('id',),owner_only=True),
    Capability('music.status','read',owner_only=True),
    Capability('music.search','read',('requestId','query','kind'),('cursor',),True,True),
    Capability('music.search.get','read',('id',),owner_only=True),
    Capability('music.resolve','read',('requestId','selection'),mutation=True,owner_only=True),
    Capability('music.plan.get','read',('id',),owner_only=True),
    Capability('music.enqueue','write',('requestId','planId','planRevision','destination'),mutation=True,owner_only=True),
    Capability('music.jobs.list','read',(),('cursor','limit'),owner_only=True),
    Capability('music.jobs.get','read',('id',),owner_only=True),
    Capability('music.jobs.control','write',('id','revision','action'),mutation=True,owner_only=True),
    Capability('drives.list','read'), Capability('files.list','read',('query',)),
    Capability('files.stat','read',('ref',)), Capability('status.get','read'),
    Capability('operations.submit','write',('request',),mutation=True),
    Capability('operations.reconcile','read',('requestId',)),
    Capability('jobs.snapshot','read'), Capability('jobs.get','read',('id',)), Capability('jobs.events','read',('after',),('limit',)),
    Capability('jobs.control','write',('id','action'),mutation=True),
    Capability('jobs.hide','read',('ids',),mutation=True), Capability('trash.list','trash',('driveId',)),
    Capability('trash.confirm','purge',('driveId','targets'),('empty',),True,False,True),
    Capability('configuration.get','settings',owner_only=True),
    Capability('configuration.save','settings',('draft',),mutation=True,owner_only=True),
    Capability('configuration.testHealth','settings',owner_only=True),
    Capability('agents.list','settings',owner_only=True),
    Capability('agents.register','settings',('label','secret','scopes','allowedRoots'),mutation=True,owner_only=True),
    Capability('agents.updateScopes','settings',('id','revision','scopes','allowedRoots'),mutation=True,owner_only=True),
    Capability('agents.revoke','settings',('id','revision'),mutation=True,owner_only=True),
    Capability('agents.rotate','settings',('id','secret'),mutation=True,owner_only=True),
    Capability('sessions.list','settings',owner_only=True),
    Capability('diagnostics.list','read'), Capability('audit.list','read',(),('after','limit')),
    Capability('transfers.folderBegin','write',('requestId','destination','name','sizeBytes','fileCount','manifestDigest'),mutation=True,owner_only=True),
    Capability('transfers.folderStatus','write',('folderId',),owner_only=True),
    Capability('transfers.folderAck','write',('folderId','transferId'),mutation=True,owner_only=True),
    Capability('transfers.folderFinish','write',('folderId',),mutation=True,owner_only=True),
    Capability('transfers.folderError','write',('folderId','code'),mutation=True,owner_only=True),
    Capability('transfers.initiate','write',('requestId','destination','name','sizeBytes'),('sha256','conflictPolicy','parentRequestId','folderId'),True),
    Capability('transfers.batchInitiate','write',('requestId','files'),mutation=True),
    Capability('transfers.resumeInfo','read',('jobId',)),
    Capability('transfers.restartDownload','read',('transferId',),mutation=True),
    Capability('transfers.query','read',('transferId',)),
    Capability('transfers.finalize','write',('transferId',),mutation=True),
    Capability('transfers.cancel','write',('transferId',),mutation=True),
    Capability('transfers.discard','write',('transferId',),mutation=True),
    Capability('transfers.download','read',('ref','requestId'),('parentRequestId',)),
    Capability('transfers.batchDownload','read',('requestId','refs'),mutation=True),
    Capability('transfers.clientError','read',('transferId','code'),('message',),True),
    Capability('transfers.downloadProgress','read',('transferId','offset','complete'),mutation=True),
]}


def validate_call(value):
    object_fields(value, ('version','capability','arguments'))
    if value['version'] != SCHEMA_VERSION:
        raise BackendError('UNSUPPORTED_VERSION', 'Unsupported control schema; upgrade client')
    cap = CAPABILITIES.get(value['capability']) if isinstance(value['capability'],str) else None
    if cap is None:
        raise BackendError('INVALID_ARGUMENT', 'Unknown capability')
    object_fields(value['arguments'], cap.required, cap.optional)
    return cap, value['arguments']


def translate_error(error):
    if isinstance(error, BackendError):
        return error
    # Infrastructure owns detailed cause logging. Never expose raw exception strings.
    mapping = {'ENOENT':'NOT_FOUND','ENOSPC':'INSUFFICIENT_SPACE','EROFS':'READ_ONLY','EACCES':'PERMISSION_DENIED'}
    import errno
    if isinstance(error, OSError):
        return BackendError(mapping.get(errno.errorcode.get(error.errno),'INTERNAL_ERROR'), 'Storage operation failed safely', error.errno in (errno.EBUSY, errno.EIO))
    return BackendError('INTERNAL_ERROR', 'Operation failed; use the correlation ID for diagnostics')

# Vault schemas share the central capability registry.
VAULT_OPS={
 'status':('vault.read',(),()), 'help':('vault.read',(),()),
 'entries.list':('vault.read',(),('query','cursor','limit','trashed')),
 'entries.get':('vault.read',('id',),()),
 'entries.reveal':('vault.reveal',('id',),('version',)),
 'entries.create':('vault.write',('requestId','displayName','login','password'),('url','notes')),
 'entries.update':('vault.write',('requestId','id','revision'),('displayName','url','login','password','notes')),
 'entries.trash':('vault.write',('requestId','id','revision'),()),
 'entries.restore':('vault.write',('requestId','id','revision'),()),
 'entries.history':('vault.read',('id',),('cursor','limit')),
 'entries.restoreVersion':('vault.write',('requestId','id','revision','version'),()),
 'passwords.generate':('vault.reveal',(),('length','alphabet')),
 'requests.reconcile':('vault.write',('requestId','operation'),()),
 'backup.export':('vault.export',(),()),
 'backup.import':('vault.export',('requestId','archive'),()),
}

CAPABILITIES.update({"vault."+name: Capability("vault."+name,scope,required,optional,mutation=name in ("entries.create","entries.update","entries.trash","entries.restore","entries.restoreVersion","backup.import")) for name,(scope,required,optional) in VAULT_OPS.items()})

SKILLS_OPS = {
    'status': ('skills.read', (), ()), 'help': ('skills.read', (), ()),
    'list': ('skills.read', (), ('query', 'cursor', 'limit', 'trashed')),
    'get': ('skills.read', ('id',), ('revision',)),
    'document': ('skills.read', ('id',), ('revision',)),
    'fileRead': ('skills.read', ('id', 'path'), ('revision', 'offset', 'length')),
    'put': ('skills.write', ('requestId', 'slug', 'textFiles'), ('id', 'expectedRevision')),
    'uploadBegin': ('skills.write', ('requestId', 'slug', 'files'), ('id', 'expectedRevision')),
    'uploadStatus': ('skills.write', ('uploadId',), ()),
    'uploadPublish': ('skills.write', ('requestId', 'uploadId'), ()),
    'uploadCancel': ('skills.write', ('uploadId',), ()),
    'trash': ('skills.write', ('requestId', 'id', 'expectedRevision'), ()),
    'restore': ('skills.write', ('requestId', 'id', 'expectedRevision'), ()),
}
CAPABILITIES.update({"skills."+name: Capability("skills."+name,scope,required,optional,mutation=scope=="skills.write") for name,(scope,required,optional) in SKILLS_OPS.items()})

PHONE_TRANSFER_OPS = {
 'capabilities':('phone_transfer.read',(),()), 'help':('phone_transfer.read',(),()),
 'prepare':('phone_transfer.send',('requestId','inboxId','title'),('senderLabel','message','links','attachments','retentionDays')),
 'operation':('phone_transfer.send',('requestId',),()), 'uploads':('phone_transfer.send',('deliveryId',),()),
 'commit':('phone_transfer.send',('deliveryId','requestId'),()),
 'list':('phone_transfer.read',('inboxId',),('limit','cursor')), 'changes':('phone_transfer.read',('inboxId','cursor'),('limit',)),
 'get':('phone_transfer.read',('deliveryId',),()),
 'device':('phone_transfer.read',('inboxId','deviceId','platform','displayName','pushToken','notificationsEnabled'),()),
 'receipt':('phone_transfer.read',('deliveryId','deviceId'),('downloadedAttachmentIds','opened')),
 'delete':('phone_transfer.delete',('deliveryId','requestId'),()),
}
CAPABILITIES.update({'phone_transfer.'+name:Capability('phone_transfer.'+name,scope,required,optional,mutation=name in ('prepare','commit','device','receipt','delete')) for name,(scope,required,optional) in PHONE_TRANSFER_OPS.items()})
