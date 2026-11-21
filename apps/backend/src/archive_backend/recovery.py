"""Conservative journal reconciliation. Never repeats a filesystem mutation."""
from __future__ import annotations

import hashlib
import json
import time

from .contracts import BackendError, CHUNK_BYTES, bytes_value, entry_ref
from .catalog import present_entry


# A legacy/incomplete JSON record must not prevent independent records from
# being inspected. Do not catch arbitrary runtime or process-control failures.
_RECORD_ERRORS = (BackendError, KeyError, TypeError, ValueError, AttributeError, IndexError)


def _stat(fs, path):
    try:
        return fs.stat(path)
    except BackendError as error:
        if error.code == 'NOT_FOUND':
            return None
        raise


def _internal(fs, area, token):
    try:
        return fs.internal_stat(area, token)
    except BackendError as error:
        if error.code == 'NOT_FOUND':
            return None
        raise


def _identity(entry, intent):
    return entry is not None and (entry.device, entry.inode) == (intent.get('device'), intent.get('inode'))


def _mark_intent(service, key, intent, outcome):
    current = service.state.get('intents', key)
    if current is None or current.get('status') == 'done':
        return
    current.update(status='done', recovery=outcome)
    service.state.put('intents', key, current)
    job_id = key.split(':', 1)[0]
    record = service.state.get('jobs', job_id)
    actor = record['actor']['id'] if record else 'service-recovery'
    service.state.audit(actor, 'recovery.' + intent['kind'], outcome, key)


def reconcile_transfer(service, transfer_id: str) -> bool:
    """Converge a proven published upload, including lost final response/metadata.

    A matching durable destination identity + recorded SHA/size is required. Merely
    finding a name, a missing temporary, or 100% bytes never implies completion.
    """
    record = service.state.get('transfers', transfer_id)
    if not record or record.get('direction') == 'download' or record.get('state') in {'discarded', 'canceled'}:
        return False
    if record.get('state') == 'completed':
        return True
    intent_key = 'transfer:' + transfer_id
    intent = service.state.get('intents', intent_key)
    if not intent or intent.get('kind') != 'publish' or intent.get('drive') != record['drive']:
        return False
    job = service.state.get('jobs', record['jobId'])
    if not job:
        return False
    principal = service.auth.refresh(service.jobs._principal(job))
    fs = service.catalog.filesystem(record['drive'])
    service.auth.authorize(principal, 'write', record['drive'], intent['destination'])
    service.auth.authorize(principal, 'read', record['drive'], intent['destination'])
    fs.volume.check(record['generation'])
    if intent.get('generation') is not None:
        fs.volume.check(intent['generation'])
    published = _stat(fs, intent['destination'])
    if not _identity(published, intent) or published.kind != 'file' or published.size != int(record['size']):
        return False
    if _internal(fs, 'staging', intent['token']) is not None:
        return False
    verification = record.get('verification') or {}
    expected = record.get('verifiedSha256') or verification.get('digest') or record.get('expected')
    verified_bytes = record.get('verifiedBytes') or verification.get('bytes') or record['size']
    if not isinstance(expected, str) or len(expected) != 64 or int(verified_bytes) != int(record['size']):
        return False
    checksum, count = hashlib.sha256(), 0
    with fs.open_read(intent['destination'], published.revision) as source:
        while True:
            service.auth.authorize(principal, 'read', record['drive'], intent['destination'])
            chunk = source.read(CHUNK_BYTES)
            if not chunk:
                break
            count += len(chunk)
            if count > int(record['size']):
                raise BackendError('INTEGRITY_FAILED', 'Published upload size changed during reconciliation')
            checksum.update(chunk)
    if count != int(record['size']) or checksum.hexdigest() != expected:
        raise BackendError('INTEGRITY_FAILED', 'Published upload checksum does not match durable verification')
    with service.state.transaction():
        latest = service.state.get('transfers', transfer_id)
        if latest.get('state') == 'completed':
            return True
        if latest.get('state') in {'discarded', 'canceled'} or latest.get('offset') != record.get('offset'):
            return False
        fresh_intent = service.state.get('intents', intent_key)
        current = fs.stat(intent['destination'])
        if fresh_intent != intent or current.revision != published.revision or not _identity(current, intent):
            return False
        service.auth.authorize(principal, 'write', record['drive'], intent['destination'])
        fs.volume.check(record['generation'])
        latest.update(state='completed', entry=present_entry(current, record['drive'], service.auth, principal), verification={'algorithm': 'sha256', 'digest': checksum.hexdigest(), 'bytes': str(count), 'level': 'destination-sha256'})
        service.state.put('transfers', transfer_id, latest)
        _mark_intent(service, intent_key, intent, 'verified-published-upload')
        job = service.state.get('jobs', record['jobId'])
        if job['state'] != 'completed':
            job.update(state='completed', bytesDone=str(count), bytesTotal=str(count), error=None, outcome='complete', resultingNames=[current.name], _lease=None, _fence=job.get('_fence', 0) + 1)
            service.jobs._save(job, 'recovered-published-upload')
        service.state.audit(principal.id, 'upload.recover', 'completed', transfer_id)
    return True


def _reconcile_client(service, transfer_id, record):
    if record.get('state') in {'completed', 'discarded', 'canceled'}:
        return False
    job = service.state.get('jobs', record['jobId'])
    if not job or job['state'] != 'failed' or job.get('_lease') or not job.get('_workerStopped') or (job.get('error') or {}).get('code') not in {'INTERRUPTED', 'TRANSFER_INTERRUPTED'}:
        return False
    direction = record.get('direction', 'upload')
    if direction not in {'upload', 'download'} or job['operation'] != direction or record['actor'] != job['actor']['id'] or record['id'] != transfer_id:
        return False
    offset, size = bytes_value(record['offset']), bytes_value(record['size'])
    if offset > size:
        return False
    generation = record.get('generation')
    if type(generation) is not int or not 1 <= generation <= 2**53 - 1:
        # Older downloads did not persist a mount generation. The current mount
        # cannot prove their original one; retain the failed job and offset for
        # an explicit, source-validated byte-zero restart instead of guessing.
        return False
    principal = service.auth.refresh(service.jobs._principal(job))
    scope = 'read' if direction == 'download' else 'write'
    service.auth.authorize(principal, scope, record['drive'], record['path'])
    fs = service.catalog.filesystem(record['drive'])
    fs.volume.check(generation)
    if direction == 'download':
        ref = entry_ref(record['ref'])
        if ref['driveId'] != record['drive'] or ref['path'] != record['path'] or record.get('revision', ref['revision']) != ref['revision'] or job['_payload'].get('targets') != [ref]:
            return False
        source = service.catalog.stat(principal, ref)
        if source['kind'] not in {'file', 'directory'} or (source['sizeBytes'] if source['kind'] == 'file' else '0') != record['size']:
            return False
    else:
        if service.state.get('intents', 'transfer:' + transfer_id):
            return False
        staged = _internal(fs, 'staging', transfer_id)
        if not staged or staged.kind != 'file' or not offset <= staged.size <= size:
            return False
    with service.state.transaction():
        current = service.state.get('jobs', job['id'])
        if current['revision'] != job['revision'] or current.get('_lease') or service.state.get('transfers', transfer_id) != record:
            return False
        service.auth.authorize(principal, scope, record['drive'], record['path'])
        fs.volume.check(generation)
        if direction == 'download':
            service.catalog.stat(principal, ref)
            record['revision'] = ref['revision']
        current.update(state='waiting-for-client', error=None, bytesDone=record['offset'], _retrySafe=False)
        service.jobs._save(current, 'client-offset-reconciled')
        record['state'] = 'waiting-for-client'
        service.state.put('transfers', transfer_id, record)
        service.state.audit(principal.id, 'transfer.recover', 'waiting-for-client', transfer_id)
    return True


def _reconcile_intent(service, key, intent):
    kind = intent.get('kind')
    if kind not in {'publish', 'trash', 'restore', 'purge', 'rename', 'mkdir'} or intent.get('status') == 'done':
        return 'unchanged'
    job_id = key.split(':', 1)[0]
    job = service.state.get('jobs', job_id)
    lease = (job or {}).get('_lease')
    if lease and lease['expires'] > time.time():
        return 'active'
    if 'drive' not in intent:
        return 'uncertain'
    fs = service.catalog.filesystem(intent['drive'])
    if intent.get('generation') is not None:
        fs.volume.check(intent['generation'])
    if kind == 'mkdir':
        # Its pre-effect intent lacks an inode. Existence alone is not ownership proof.
        return 'uncertain'
    with service.state.transaction():
        if service.state.get('intents', key) != intent:
            return 'unchanged'
        if kind == 'rename':
            if not job:
                return 'uncertain'
            principal = service.auth.refresh(service.jobs._principal(job))
            request = {key: value for key, value in service.jobs.payload(job_id).items() if key != 'previousAttemptId'}
            service.operations.validate(principal, request)
            service.auth.authorize(principal, 'write', intent['drive'], intent['source'])
            service.auth.authorize(principal, 'write', intent['drive'], intent['destination'])
        if kind == 'trash':
            held, source = _internal(fs, 'trash', intent['token']), _stat(fs, intent['source'])
            if _identity(held, intent) and source is None:
                service.state.put('trash', intent['token'], intent)
                _mark_intent(service, key, intent, 'trash-tombstone-repaired')
                return 'repaired'
        elif kind in {'restore', 'rename', 'publish'}:
            destination = _stat(fs, intent['destination'])
            source = _internal(fs, 'trash', intent['token']) if kind == 'restore' else _internal(fs, 'staging', intent['token']) if kind == 'publish' else _stat(fs, intent['source'])
            if _identity(destination, intent) and source is None:
                if kind == 'restore':
                    service.state.delete('trash', intent['token'])
                _mark_intent(service, key, intent, 'destination-identity-verified')
                return 'repaired'
        elif kind == 'purge':
            # No replay: absence proves the captured internal target is gone; a
            # remaining directory may be partially purged and remains explicit.
            if _internal(fs, 'trash', intent['token']) is None:
                service.state.delete('trash', intent['token'])
                _mark_intent(service, key, intent, 'purge-metadata-repaired')
                return 'repaired'
    return 'uncertain'


def _flag_review(service, job_id):
    with service.state.transaction():
        job = service.state.get('jobs', job_id)
        if not job or job['state'] in {'completed', 'canceled'} or job.get('_lease'):
            return
        if job.get('error') and job['error'].get('code') not in {'INTERRUPTED', 'TRANSFER_INTERRUPTED', 'WORKER_UNAVAILABLE'}:
            return
        checkpoint = {'reconciled': False, 'originalJobId': job_id, 'effect': 'inspected-requires-review'}
        if job['state'] == 'failed' and job.get('_checkpoint') == checkpoint:
            return
        job.update(state='failed', _retrySafe=False, error={'code': 'INTERRUPTED', 'message': 'Filesystem journal inspected; explicit operation-specific reconciliation is required', 'retryable': False, 'correlationId': job_id, 'recovery': ['refresh']}, _checkpoint=checkpoint)
        service.jobs._save(job, 'journal-reconciled')


def reconcile_job(service, job_id):
    """Reconcile one stopped filesystem worker, never all retained transfers."""
    with service.state.transaction() as connection:
        job = service.state.get('jobs', job_id)
        if not job or job.get('_lease'):
            return False
        rows = connection.execute("SELECT key,body FROM records WHERE namespace='intents' AND key>=? AND key<? ORDER BY key", (job_id + ':', job_id + ';')).fetchall()
        # Revision 3 is the legacy accept -> claim -> interrupted sequence.
        # execute() saves 'running' before any filesystem operation, so these
        # jobs never started. Do not infer safety from an absent filename alone.
        code = (job.get('error') or {}).get('code')
        unstarted = job.get('_interruptedState') == 'preparing' or (job.get('_interruptedState') is None and job['revision'] == 3) or code == 'BUSY'
        if (job['operation'] == 'new-folder' and job['state'] == 'failed'
                and code in {'INTERRUPTED', 'BUSY'}
                and job.get('_workerStopped') and unstarted and not rows
                and not job.get('_checkpoint') and job.get('_startupRecoveries', 0) < 3):
            principal = service.jobs._principal(job)
            request = {key: value for key, value in job['_payload'].items() if key != 'previousAttemptId'}
            service.operations.validate(principal, request)
            job.update(state='queued', error=None, _retrySafe=False,
                       _startupRecoveries=job.get('_startupRecoveries', 0) + 1)
            service.jobs._save(job, 'unstarted-worker-requeued')
            return True
    for key, body in rows:
        try:
            outcome = _reconcile_intent(service, key, json.loads(body))
            if outcome in {'repaired', 'uncertain'}:
                _flag_review(service, job_id)
        except _RECORD_ERRORS:
            continue
    with service.state.transaction() as connection:
        job = service.state.get('jobs', job_id)
        if job and not job.get('_lease') and job['state'] in {'failed', 'completed', 'canceled', 'paused'}:
            for namespace in ('locks', 'reservations'):
                connection.execute('DELETE FROM records WHERE namespace=? AND key>=? AND key<?', (namespace, job_id + ':', job_id + ';'))
    return False


def reconcile(service, *, allow_client_recovery: bool = False) -> dict:
    """Inspect bounded pages; keep ambiguous effects failed with explicit recovery."""
    result = {'repaired': 0, 'uncertain': 0, 'transfers': 0, 'locksReleased': 0, 'errors': 0}
    after = ''
    while True:
        page = service.state.scan('transfers', after=after, limit=100)
        if not page:
            break
        for row in page:
            try:
                record = row['value']
                if record.get('state') == 'completed':
                    continue
                if reconcile_transfer(service, row['key']) or (allow_client_recovery and _reconcile_client(service, row['key'], record)):
                    result['transfers'] += 1
            except _RECORD_ERRORS:
                result['errors'] += 1
        after = page[-1]['key']
    after = ''
    while True:
        page = service.state.scan('intents', after=after, limit=100)
        if not page:
            break
        for row in page:
            try:
                outcome = _reconcile_intent(service, row['key'], row['value'])
                if outcome == 'repaired':
                    result['repaired'] += 1
                elif outcome == 'uncertain':
                    result['uncertain'] += 1
                if outcome in {'repaired', 'uncertain'}:
                    _flag_review(service, row['key'].split(':', 1)[0])
            except _RECORD_ERRORS:
                result['errors'] += 1
        after = page[-1]['key']
    after = ''
    while True:
        page = service.state.scan('locks', after=after, limit=100)
        if not page:
            break
        for row in page:
            try:
                lock = row['value']
                with service.state.transaction():
                    job = service.state.get('jobs', lock['job'])
                    if job and not job.get('_lease') and job['state'] in {'failed', 'completed', 'canceled', 'paused'}:
                        service.state.delete('locks', row['key'])
                        result['locksReleased'] += 1
            except _RECORD_ERRORS:
                result['errors'] += 1
        after = page[-1]['key']
    after = ''
    while True:
        page = service.state.scan('reservations', after=after, limit=100)
        if not page:
            break
        for row in page:
            try:
                with service.state.transaction():
                    job = service.state.get('jobs', row['value'].get('job', ''))
                    if job and not job.get('_lease') and job['state'] in {'failed', 'canceled', 'completed'}:
                        service.state.delete('reservations', row['key'])
            except _RECORD_ERRORS:
                result['errors'] += 1
        after = page[-1]['key']
    return result
