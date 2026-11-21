"""Pure mobile error DTO mapping shared by transport and domain adapters."""
import re
import uuid
from .contracts import BackendError,path,name


def stored_error(raw):
    error=BackendError(raw['code'],raw.get('message','Operation stopped safely'),raw.get('retryable',False))
    try: error.correlation_id=str(uuid.UUID(raw['correlationId']))
    except (KeyError,ValueError,TypeError,AttributeError):
        # Stable identity for old envelopes whose correlation was a non-UUID job
        # token. This does not expose the original token or message.
        error.correlation_id=str(uuid.uuid5(uuid.NAMESPACE_OID,str(raw.get('correlationId','legacy'))+':'+error.code))
    return error


def mobile_error(error):
    mapping={'UNAUTHENTICATED':('auth_expired','sign_in'),'AUTH_EXPIRED':('auth_expired','sign_in'),
             'PERMISSION_REVOKED':('access_revoked','sign_in'),'REVOKED':('access_revoked','sign_in'),
             'PERMISSION_DENIED':('permission_denied','none'),'SOURCE_CHANGED':('source_changed','refresh_listing'),
             'STALE_REVISION':('source_changed','refresh_listing'),'VOLUME_CHANGED':('volume_changed','repair_source'),
             'CONFLICT':('conflict','review_conflict'),'INSUFFICIENT_SPACE':('remote_space','free_remote_space'),
             'QUOTA_EXCEEDED':('remote_space','free_remote_space'),'RATE_LIMITED':('backend_unavailable','wait'),
             'DRIVE_UNAVAILABLE':('backend_unavailable','repair_source'),'UNSUPPORTED_ENTRY':('unsupported','none'),
             'REPRESENTATION_EXPIRED':('representation_expired','restart_download'),
             'SNAPSHOT_INTERRUPTED':('representation_expired','restart_download'),
             'UPLOAD_EXPIRED':('transfer_expired','choose_source'),
             'CURSOR_EXPIRED':('cursor_expired','refresh_listing'),
             'REQUEST_MISMATCH':('request_mismatch','reconcile'),
             'REQUEST_UNKNOWN':('request_unknown','reconcile'),
             'SCAN_LIMIT':('scan_limit','none'),
             'INTEGRITY_FAILED':('integrity_failed','none'),
             'CANCELED':('query_canceled','reconcile'),
             'INVALID_ARGUMENT':('invalid_contract','none'),'INVALID_PATH':('invalid_contract','none'),
             'NOT_FOUND':('source_changed','refresh_listing')}
    code,recovery=mapping.get(error.code,('backend_unavailable','reconcile'))
    return {'error':{'code':code,'message':error.message[:500],'recovery':recovery,'retryable':error.retryable if error.code!='INTEGRITY_FAILED' else False,
                     'safeServerCode':error.code,'correlationId':error.correlation_id}}


def error_status(error):
    if error.code in ('UNAUTHENTICATED','AUTH_EXPIRED','AUTH_REQUIRED'): return 401
    if error.code in ('PERMISSION_DENIED','PERMISSION_REVOKED','REVOKED'): return 403
    if error.code in ('NOT_FOUND','REQUEST_UNKNOWN'): return 404
    if error.code in ('STALE_REVISION','SOURCE_CHANGED','VOLUME_CHANGED','CONFLICT','REQUEST_MISMATCH','CURSOR_EXPIRED','REPRESENTATION_EXPIRED','SNAPSHOT_INTERRUPTED','UPLOAD_EXPIRED','CANCELED'): return 409
    if error.code=='RATE_LIMITED': return 429
    if error.code in ('UNSUPPORTED','UNSUPPORTED_ENTRY','UNSUPPORTED_VERSION'): return 501
    if error.code in ('INSUFFICIENT_SPACE','QUOTA_EXCEEDED','SCAN_LIMIT'): return 507
    if error.code in ('INVALID_ARGUMENT','INVALID_PATH','CONFIRMATION_REQUIRED'): return 400
    return 503


def mobile_path(value):
    path(value)
    if re.search(r'[\x00-\x1f]',value) or re.match(r'^[A-Za-z]:',value):
        raise BackendError('UNSUPPORTED_ENTRY','Logical path cannot be represented by mobile v1')
    return value


def mobile_name(value):
    name(value); mobile_path(value)
    return value
