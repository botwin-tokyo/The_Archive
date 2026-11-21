"""Bounded spawned workers: no inherited database connection or volume descriptors."""
from __future__ import annotations

import dataclasses
import multiprocessing
import os
import threading
import time
import uuid

from .contracts import BackendError


def _worker_entry(configuration, state_path, state_identity, fixture_roots, root_identities, lease, fixture_fault, fixture_delay):
    from .application import Service
    from .persistence import State
    from .volumes import VolumeRegistry
    service = None
    try:
        configuration = dataclasses.replace(configuration, state_path=state_path, listen=None)
        state = State(state_path, validated_identity=state_identity)
        if fixture_roots is not None:
            registry = VolumeRegistry.fixtures(fixture_roots)
            service = Service(configuration, state, registry)
        else:
            service = Service.from_config(configuration, state=state)
        for volume in service.registry.list():
            if tuple(volume._root_identity) != tuple(root_identities.get(volume.volume_id, ())):
                raise BackendError("VOLUME_MISMATCH", "A worker root no longer matches its pinned identity")
        with service.state.transaction():
            from .jobs import _process_stamp
            current = service.state.get('jobs', lease['job']['id'])
            service.jobs._lease(current, lease['workerId'], lease['fence'])
            current['_lease'].update(pid=os.getpid(), processStart=_process_stamp(os.getpid()))
            service.state.put('jobs', current['id'], current)
        if fixture_fault:
            def fault(point):
                if point == fixture_fault:
                    os._exit(91)  # Explicit disposable-fixture crash injection only.
            service.operations.fault = fault
        if fixture_delay:
            time.sleep(fixture_delay)
        service.operations.execute(lease)
    except BaseException as error:
        # Parent observes exit status and performs fenced, journal-aware recovery.
        import json, sys
        print(json.dumps({'component': 'job-worker', 'jobId': lease['job']['id'],
                          'code': getattr(error, 'code', 'WORKER_UNAVAILABLE')}), file=sys.stderr, flush=True)
        raise SystemExit(1) from None
    finally:
        if service is not None:
            service.close()


class WorkerPool:
    def __init__(self, service, *, poll_interval: float = 0.1, fixture_roots=None, fixture_fault: str | None = None, fixture_delay: float = 0, _reconcile_hook=None):
        if service.state is None or service.state.path == ':memory:' or not 0.01 <= poll_interval <= 5:
            raise BackendError('INVALID_ARGUMENT', 'Workers require durable state and a bounded polling interval')
        self.service = service
        info = os.stat(service.state.path, follow_symlinks=False)
        self.state_identity = (info.st_dev, info.st_ino)
        self.poll_interval = poll_interval
        self.maximum = min(2, service.config.max_workers)
        volumes = service.registry.list()
        verified_fixtures = bool(volumes) and all(volume._fixture for volume in volumes)
        if fixture_roots is not None and not verified_fixtures:
            raise BackendError('INVALID_ARGUMENT', 'Fixture workers require a verified disposable fixture registry')
        self.fixture_roots = ({volume.volume_id: volume.root_path for volume in volumes} if verified_fixtures else None)
        if fixture_roots is not None and dict(fixture_roots) != self.fixture_roots:
            raise BackendError('INVALID_ARGUMENT', 'Fixture roots must match the verified registry')
        if (fixture_fault or fixture_delay) and not verified_fixtures:
            raise BackendError('INVALID_ARGUMENT', 'Fault injection is restricted to disposable fixtures')
        if not 0 <= fixture_delay <= 60:
            raise BackendError('INVALID_ARGUMENT', 'Invalid fixture delay')
        self.fixture_fault, self.fixture_delay = fixture_fault, fixture_delay
        self.root_identities = {volume.volume_id: tuple(volume._root_identity) for volume in volumes}
        configs = []
        for value in service.config.volumes:
            try:
                volume = service.registry.get(value['id'])
            except BackendError:
                configs.append(value)
            else:
                configs.append(dict(value, expectedMountId=volume._mount.mount_id, expectedMountRoot=volume._mount.root))
        self.configuration = dataclasses.replace(service.config, volumes=tuple(configs))
        self._context = multiprocessing.get_context('spawn')
        self._workers = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self.last_error = None
        self._reconcile_hook = _reconcile_hook

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._supervise, name='archive-worker-supervisor', daemon=True)
            self._thread.start()

    def _supervise(self):
        while not self._stop.is_set():
            try:
                self.run_once()
                self.last_error = None
            except BackendError as error:
                self.last_error = error.code
            except Exception:
                self.last_error = 'WORKER_UNAVAILABLE'
            self._stop.wait(self.poll_interval)

    @property
    def active(self):
        with self._lock:
            return {job_id: {'pid': item['process'].pid, 'fence': item['lease']['fence']} for job_id, item in self._workers.items()}

    def _fence_dead(self, job_id, lease):
        # This method is called only after is_alive false and join completed.
        with self.service.state.transaction():
            record = self.service.state.get('jobs', job_id)
            current = (record or {}).get('_lease')
            if not current:
                # A crash after the terminal receipt may precede finally-cleanup.
                # These keys belong to this job; never sweep unrelated history.
                if record and record['state'] in {'completed', 'canceled', 'failed', 'paused'}:
                    for namespace in ('locks', 'reservations'):
                        self.service.state.connection.execute('DELETE FROM records WHERE namespace=? AND key>=? AND key<?', (namespace, job_id + ':', job_id + ';'))
                return
            if current['worker'] != lease['workerId'] or current['fence'] != lease['fence']:
                return
            current['expires'] = 0
            record['_workerStopped'] = True
            self.service.state.put('jobs', job_id, record)
        self.service.jobs.recover([job_id])
        if self._reconcile_hook is not None:
            self._reconcile_hook(self.service)
        else:
            from .recovery import reconcile_job
            reconcile_job(self.service, job_id)

    def _finish(self, job_id, item):
        process = item['process']
        process.join(timeout=0)
        if process.is_alive():
            return False
        self._workers.pop(job_id, None)
        self._fence_dead(job_id, item['lease'])
        process.close()
        return True

    def _terminate(self, job_id, item):
        process = item['process']
        if process.is_alive():
            process.terminate()
            process.join(timeout=1)
        if process.is_alive():
            process.kill()
            process.join(timeout=2)
        if process.is_alive():
            self.last_error = 'WORKER_STOP_UNCONFIRMED'
            return False  # Never reclaim while the old process may still write.
        return self._finish(job_id, item)

    def terminate_worker(self, job_id):
        with self._lock:
            item = self._workers.get(job_id)
            if not item:
                return False
            return self._terminate(job_id, item)

    def run_once(self):
        with self._lock:
            for job_id, item in list(self._workers.items()):
                process = item['process']
                if not process.is_alive():
                    self._finish(job_id, item)
                    continue
                record = self.service.state.get('jobs', job_id)
                lease = (record or {}).get('_lease')
                if lease and lease['worker'] == item['lease']['workerId'] and lease['expires'] <= time.time():
                    self._terminate(job_id, item)
            if self._stop.is_set():
                return
            while len(self._workers) < self.maximum:
                worker = 'process-' + uuid.uuid4().hex
                lease = self.service.jobs.claim(worker)
                if lease is None:
                    break
                job_id = lease['job']['id']
                process = self._context.Process(target=_worker_entry, args=(self.configuration, self.service.state.path, self.state_identity, self.fixture_roots, self.root_identities, lease, self.fixture_fault, self.fixture_delay), name='archive-job-worker')
                try:
                    process.start()
                except Exception:
                    self._fence_dead(job_id, lease)
                    raise BackendError('WORKER_UNAVAILABLE', 'The worker process could not start', retryable=True) from None
                self._workers[job_id] = {'process': process, 'lease': lease}

    def close(self, graceful_timeout: float = 2):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1)
        deadline = time.monotonic() + min(max(graceful_timeout, 0), 10)
        while time.monotonic() < deadline:
            with self._lock:
                for job_id, item in list(self._workers.items()):
                    if not item['process'].is_alive():
                        self._finish(job_id, item)
                if not self._workers:
                    return
            time.sleep(0.02)
        with self._lock:
            for job_id, item in list(self._workers.items()):
                self._terminate(job_id, item)
