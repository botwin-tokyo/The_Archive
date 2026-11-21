"""Strict boot configuration. A blank root is never replaced by a host path."""
import os
from dataclasses import dataclass, field
from .contracts import BackendError, object_fields

@dataclass(frozen=True)
class Configuration:
    version: int = 1
    state_path: str | None = None
    listen: dict | None = None
    volumes: tuple = ()
    max_workers: int = 2
    max_pending_jobs: int = 128
    min_free_bytes: int = 67108864
    telemetry: dict = field(default_factory=lambda: {'enabled':False,'endpoint':'','intervalSeconds':30})
    mobile: dict | None = None
    music: dict | None = None
    video: dict | None = None
    anime: dict | None = None
    git_backup: dict | None = None
    radio: dict | None = None
    vault: dict | None = None
    skills: dict | None = None
    phone_transfer: dict | None = None


def parse_config(value):
    object_fields(value, (), ('version','statePath','listen','volumes','maxWorkers','maxPendingJobs','minFreeBytes','telemetry','mobile','music','video','anime','gitBackup','radio','vault','skills','phoneTransfer'))
    if value.get('version',1) != 1:
        raise BackendError('UNSUPPORTED_VERSION','Unsupported configuration version')
    state = value.get('statePath')
    if state is not None and (not isinstance(state,str) or not state.startswith('/') or state == '/' or '\0' in state):
        raise BackendError('INVALID_ARGUMENT','Explicit absolute service-state file required')
    volumes = value.get('volumes',[])
    if not isinstance(volumes,list) or len(volumes) > 16:
        raise BackendError('INVALID_ARGUMENT','At most 16 independent configured volumes')
    seen=set(); roots=set()
    for volume in volumes:
        object_fields(volume, ('id','label','role','rootPath','mountPoint','filesystemUuid','expectedFilesystem','expectedDevice','expectedMountSource'), ('expectedMountId','expectedMountRoot'))
        if volume['id'] in seen or volume['rootPath'] in roots or volume['role'] not in ('files','harness-backups'):
            raise BackendError('INVALID_ARGUMENT','Independent volume identities are required')
        for field_name in ('rootPath','mountPoint'):
            path=volume[field_name]
            if not isinstance(path,str) or not path.startswith('/') or path in ('/','/home','/root','/tmp') or '/../' in path or '\0' in path:
                raise BackendError('INVALID_ARGUMENT','A verified non-system mount/root must be explicitly configured')
        if not volume['filesystemUuid'] or len(volume['expectedDevice']) != 2:
            raise BackendError('INVALID_ARGUMENT','Stable volume identity is required')
        if state and (state == volume['rootPath'] or state.startswith(volume['rootPath'].rstrip('/')+'/')):
            raise BackendError('INVALID_ARGUMENT','State must be outside managed volumes')
        seen.add(volume['id']); roots.add(volume['rootPath'])
    listener=value.get('listen')
    if listener is not None:
        object_fields(listener,('host','port'))
        if listener['host'] not in ('127.0.0.1','::1') or type(listener['port']) is not int or not 1024 <= listener['port'] <= 65535:
            raise BackendError('INVALID_ARGUMENT','This profile supports explicitly configured loopback listeners only')
    workers=value.get('maxWorkers',2); pending=value.get('maxPendingJobs',128); reserve=value.get('minFreeBytes',67108864)
    if type(workers) is not int or not 1 <= workers <= 2 or type(pending) is not int or not 1 <= pending <= 1024 or type(reserve) is not int or reserve < 0:
        raise BackendError('INVALID_ARGUMENT','Invalid resource budgets')
    telemetry=value.get('telemetry',{'enabled':False,'endpoint':'','intervalSeconds':30})
    object_fields(telemetry,('enabled','endpoint','intervalSeconds'),('secretFile','protocol','nodeId','sourceId','manifestRevision','spoolPath'))
    from urllib.parse import urlsplit
    if telemetry.get('protocol','legacy') not in ('legacy','fleet-v1'):
        raise BackendError('INVALID_ARGUMENT','Invalid telemetry protocol')
    if telemetry.get('protocol') == 'fleet-v1':
        import re
        for key in ('nodeId','sourceId'):
            if not isinstance(telemetry.get(key),str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}',telemetry[key]):
                raise BackendError('INVALID_ARGUMENT','Invalid fleet identity')
        if type(telemetry.get('manifestRevision')) is not int or telemetry['manifestRevision'] < 1:
            raise BackendError('INVALID_ARGUMENT','Invalid fleet revision')
        if not isinstance(telemetry.get('spoolPath'),str) or not os.path.isabs(telemetry['spoolPath']):
            raise BackendError('INVALID_ARGUMENT','An absolute fleet spool path is required')
    endpoint=urlsplit(telemetry['endpoint'])
    if telemetry['enabled'] and (endpoint.scheme != 'wss' or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment):
        raise BackendError('INVALID_ARGUMENT','Health endpoint requires wss with no inline secrets')
    if type(telemetry['intervalSeconds']) is not int or not 5 <= telemetry['intervalSeconds'] <= 3600:
        raise BackendError('INVALID_ARGUMENT','Invalid telemetry interval')
    mobile=value.get('mobile')
    if mobile is not None:
        object_fields(mobile,('listen','publicOrigin','replayKeyFile'),('maxAcceptedFileBytes','stagingBudgetBytes'))
        binding=mobile['listen']; object_fields(binding,('host','port'))
        if binding['host'] not in ('127.0.0.1','::1') or type(binding['port']) is not int or not 1024<=binding['port']<=65535 or binding==listener:
            raise BackendError('INVALID_ARGUMENT','Mobile requires a separate numeric loopback listener')
        origin=urlsplit(mobile['publicOrigin']) if isinstance(mobile['publicOrigin'],str) else None
        try:
            valid=bool(origin and origin.scheme=='https' and origin.hostname and origin.port!=0 and not origin.username and not origin.password and not origin.path and not origin.query and not origin.fragment and origin.netloc==origin.netloc.lower() and not any(c.isspace() for c in origin.netloc))
        except ValueError: valid=False
        if not valid: raise BackendError('INVALID_ARGUMENT','Mobile requires an exact HTTPS origin without path or credentials')
        key=mobile['replayKeyFile']
        if not isinstance(key,str) or not key.startswith('/') or '\0' in key:
            raise BackendError('INVALID_ARGUMENT','Mobile replay key requires an explicit absolute private file')
        from .contracts import bytes_value
        maximum=mobile.get('maxAcceptedFileBytes',str(64*1024**3)); budget=mobile.get('stagingBudgetBytes',str(128*1024**3))
        if not 0<bytes_value(maximum)<=2**40 or not bytes_value(maximum)<=bytes_value(budget):
            raise BackendError('INVALID_ARGUMENT','Invalid mobile file/staging budget')
        mobile=dict(mobile,maxAcceptedFileBytes=maximum,stagingBudgetBytes=budget)
    music=value.get('music')
    if music is not None:
        object_fields(music,('socket','spool','workerUid'))
        if any(not isinstance(music[k],str) or not music[k].startswith('/') or '\0' in music[k] or '..' in music[k].split('/') for k in ('socket','spool')) or type(music['workerUid']) is not int or music['workerUid']<=0:
            raise BackendError('INVALID_ARGUMENT','Music requires explicit protected worker paths and a non-root UID')
    video=value.get('video')
    if video is not None:
        object_fields(video,('socket','spool','workerUid'))
        if any(not isinstance(video[k],str) or not video[k].startswith('/') or '\0' in video[k] or '..' in video[k].split('/') for k in ('socket','spool')) or type(video['workerUid']) is not int or video['workerUid']<=0:
            raise BackendError('INVALID_ARGUMENT','Video requires protected worker paths and non-root UID')
    anime=value.get('anime')
    if anime is not None:
        object_fields(anime,('socket','spool','workerUid'),('defaultDestination',))
        if any(not isinstance(anime[k],str) or not anime[k].startswith('/') or '\0' in anime[k] or '..' in anime[k].split('/') for k in ('socket','spool')) or type(anime['workerUid']) is not int or anime['workerUid']<=0:
            raise BackendError('INVALID_ARGUMENT','Anime requires protected worker paths and a non-root UID')
        if 'defaultDestination' in anime:
            from .contracts import location
            location(anime['defaultDestination'])
    from .git_config import parse as parse_git
    git_backup=parse_git(value.get('gitBackup'))
    from .radio_contracts import parse_config as parse_radio
    radio=parse_radio(value.get('radio'))
    from .vault_contracts import parse_config as parse_vault
    vault=parse_vault(value.get('vault'),volumes)
    from .skills_contracts import parse_config as parse_skills
    skills=parse_skills(value.get('skills'),volumes)
    from .phone_transfer_contracts import parse_config as parse_phone_transfer
    phone_transfer=parse_phone_transfer(value.get('phoneTransfer'),volumes,vault,skills)
    return Configuration(1,state,listener,tuple(volumes),workers,pending,reserve,telemetry,mobile,music,video,anime=anime,git_backup=git_backup,radio=radio,vault=vault,skills=skills,phone_transfer=phone_transfer)
