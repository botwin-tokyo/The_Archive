"""Durable job identity, authorization, fenced leases and bounded event pages."""
from __future__ import annotations

import time
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from .contracts import BackendError
from .persistence import State, digest
from .security import Auth, Principal

_TERMINAL = frozenset({"completed", "failed", "canceled"})
_STATES = frozenset({"queued", "preparing", "running", "verifying", "committing", "completed", "pausing", "paused", "waiting-for-client", "waiting-for-drive", "needs-decision", "failed", "canceling", "canceled"})
_TRANSITIONS = {
    "queued": {"preparing", "waiting-for-client", "waiting-for-drive", "canceling", "failed"},
    "preparing": {"running", "waiting-for-client", "waiting-for-drive", "needs-decision", "canceling", "failed"},
    "running": {"verifying", "pausing", "waiting-for-client", "waiting-for-drive", "needs-decision", "canceling", "failed"},
    "verifying": {"committing", "waiting-for-drive", "canceling", "failed"},
    "committing": {"completed", "needs-decision", "failed"},
    "pausing": {"paused", "failed"}, "paused": {"preparing", "canceling", "failed"},
    "waiting-for-client": {"preparing", "running", "canceling", "failed"},
    "waiting-for-drive": {"preparing", "canceling", "failed"},
    "needs-decision": {"preparing", "canceling", "failed"}, "canceling": {"canceled", "failed"},
    "failed": set(), "completed": set(), "canceled": set(),
}
_KINDS = frozenset({"new-folder", "rename", "copy", "move", "upload", "download", "trash", "restore", "purge"})


def _process_stamp(pid: int):
    try:
        with open(f'/proc/{pid}/stat', encoding='ascii') as source:
            return source.read().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _owner_stopped(lease):
    pid = lease.get('pid')
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    try:
        with open(f'/proc/{pid}/stat', encoding='ascii') as source:
            if source.read().rsplit(')', 1)[1].split()[0] == 'Z':
                return True
    except (OSError, IndexError):
        pass
    current = _process_stamp(pid)
    return bool(current and lease.get('processStart') and current != lease['processStart'])


def _stamp(now: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if now is None else now, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Jobs:
    def __init__(self, state: State, auth: Auth, *, clock=time.time, max_active: int = 2, max_queued: int = 1000, per_principal: int = 2, per_volume: int = 2):
        if not 1 <= max_active <= 16 or not 1 <= max_queued <= 10000 or per_principal < 1 or per_volume < 1:
            raise BackendError("INVALID_ARGUMENT", "Invalid worker limits")
        self.state, self.auth, self.clock = state, auth, clock
        self.max_active, self.max_queued = max_active, max_queued
        self.per_principal, self.per_volume = per_principal, per_volume
        if state.validated_identity is not None:
            return  # The parent initialized indexes before spawning this worker.
        # Polling and chunk leases must visit live work, not the entire upload
        # history. Keep terminal jobs with a lease visible for fencing/accounting.
        with state.transaction() as connection:
            connection.execute("CREATE INDEX IF NOT EXISTS active_upload_jobs ON records(namespace,key) WHERE namespace='jobs' AND json_extract(body,'$.state') NOT IN ('completed','failed','canceled')")
            connection.execute("CREATE INDEX IF NOT EXISTS schedulable_jobs ON records(namespace,updated,key) WHERE namespace='jobs' AND (json_extract(body,'$.state') NOT IN ('completed','failed','canceled') OR json_type(body,'$._lease')='object')")
            connection.execute("CREATE INDEX IF NOT EXISTS job_snapshot_order ON records(namespace,CASE WHEN json_extract(body,'$.state') IN ('completed','failed','canceled') THEN 1 ELSE 0 END, updated DESC, key) WHERE namespace='jobs'")

    def _principal(self, record: dict) -> Principal:
        stored = record["_principal"]
        return Principal(stored["id"], stored["label"], stored["kind"], stored["credentialId"], stored["scopeVersion"], tuple(stored["scopes"]), tuple((root["driveId"], root["path"]) for root in stored["roots"]))

    def _authorize_payload(self, principal: Principal, operation: str, payload: dict) -> Principal:
        fresh = self.auth.refresh(principal)
        targets = payload.get("targets", [])
        if not isinstance(targets, list) or len(targets) > 1000:
            raise BackendError("INVALID_ARGUMENT", "Too many job targets")
        scope = "read" if operation == "download" else "trash" if operation in {"trash", "restore"} else "purge" if operation == "purge" else "write"
        for target in targets:
            if not isinstance(target, dict) or not isinstance(target.get("driveId"), str):
                raise BackendError("INVALID_ARGUMENT", "Jobs require explicit logical targets")
            self.auth.authorize(fresh, "read" if operation == "copy" else scope, target["driveId"], target.get("path", ""))
        for name in ("source", "destination"):
            reference = payload.get(name)
            if reference is not None:
                if not isinstance(reference, dict) or not isinstance(reference.get("driveId"), str):
                    raise BackendError("INVALID_ARGUMENT", "Jobs require explicit logical locations")
                self.auth.authorize(fresh, "read" if name == "source" and operation in {"copy", "download"} else scope, reference["driveId"], reference.get("path", ""))
        if fresh.kind != "owner" and not targets and not payload.get("source") and not payload.get("destination"):
            raise BackendError("PERMISSION_DENIED", "Agent work requires an authorized logical location")
        return fresh

    def accept(self, principal: Principal, operation: str, payload: dict, idempotency: str, *, initial_state: str = "queued") -> dict:
        if operation not in _KINDS or initial_state not in {"queued", "waiting-for-client"} or not isinstance(payload, dict):
            raise BackendError("INVALID_ARGUMENT", "Unsupported job operation or initial state")
        with self.state.transaction():
            fresh = self._authorize_payload(principal, operation, payload)
            request_digest = digest({"operation": operation, "payload": payload})
            prior = self.state.reconcile(fresh.id, operation, idempotency)
            if prior:
                # atomic_accept checks the digest even on retries.
                return self.state.atomic_accept(fresh.id, operation, idempotency, request_digest, {"id": "unused"})
            count = self.state.connection.execute("SELECT COUNT(*) FROM records INDEXED BY active_upload_jobs WHERE namespace='jobs' AND json_extract(body,'$.state') NOT IN ('completed','failed','canceled')").fetchone()[0]
            if count >= self.max_queued:
                raise BackendError("UNAVAILABLE", "The bounded job queue is full", retryable=True)
            job_id, now = str(uuid.uuid4()), _stamp(self.clock())
            targets = payload.get("targets", [])
            source = payload.get("source") or ({"driveId": targets[0]["driveId"], "path": targets[0].get("path", "")} if targets else None)
            destination = payload.get("destination")
            source = {"driveId": source["driveId"], "path": source.get("path", "")} if source else None
            destination = {"driveId": destination["driveId"], "path": destination.get("path", "")} if destination else None
            total = payload.get("bytesTotal")
            if total is not None and (not isinstance(total, str) or not total.isdigit() or len(total) > 40):
                raise BackendError("INVALID_ARGUMENT", "Byte totals must be decimal strings")
            record = {"id": job_id, "requestId": idempotency, "revision": 1, "actor": {"id": fresh.id, "label": fresh.label, "kind": fresh.kind}, "operation": operation, "label": str(payload.get("label", payload.get("name", operation)))[:512], "source": source, "destination": destination, "state": initial_state, "bytesDone": "0", "bytesTotal": total, "rateBytesPerSecond": None, "etaSeconds": None, "createdAt": now, "updatedAt": now, "parentId": payload.get("parentId"), "childIds": [], "previousAttemptId": payload.get("previousAttemptId"), "error": None, "outcome": None, "resultingNames": [], "simulated": False, "_payload": payload, "_principal": fresh.as_dict(), "_lease": None, "_fence": 0}
            return self.state.atomic_accept(fresh.id, operation, idempotency, request_digest, record)

    def _visible(self, principal: Principal, record: dict) -> Principal:
        fresh = self.auth.refresh(principal)
        if fresh.kind != "owner" and record["actor"]["id"] != fresh.id:
            raise BackendError("PERMISSION_DENIED", "This job belongs to another identity")
        # Changes in allowed roots also affect status and event visibility.
        if fresh.kind != "owner":
            references = [record.get("source"), record.get("destination"), *record["_payload"].get("targets", [])]
            self.auth.authorize(fresh, "read")
            for reference in references:
                if reference:
                    self.auth.authorize(fresh, "read", reference["driveId"], reference.get("path", ""))
        return fresh

    def _serialize(self, record: dict) -> dict:
        visible = {key: value for key, value in record.items() if not key.startswith("_")}
        authorized = True
        try:
            self._authorize_payload(self._principal(record), record["operation"], record["_payload"])
        except BackendError:
            authorized = False
        state = record["state"]
        visible["capabilities"] = {"pause": authorized and (state == "running" or (record["operation"] in {"upload","download"} and state == "waiting-for-client")), "resume": authorized and state in {"paused", "waiting-for-drive", "waiting-for-client"}, "cancel": state not in _TERMINAL and state not in {"committing", "canceling"}, "retry": authorized and state == "failed" and bool((record.get("error") or {}).get("retryable")) and bool(record.get("_retrySafe"))}
        # Older clients left resumed folder parents queued without a worker.
        # They remain client-owned and must be recoverable after an upgrade.
        if record['_payload'].get('folderUpload') and state == 'queued' and not record.get('_lease'):
            visible['capabilities'].update(pause=authorized,resume=authorized)
        if record.get("_workPlanChild"):
            visible["capabilities"]={"pause":False,"resume":False,"cancel":False,"retry":False}
        return visible

    def get(self, principal: Principal, job_id: str) -> dict:
        with self.state.transaction():
            record = self.state.get("jobs", job_id)
            if not record:
                raise BackendError("NOT_FOUND", "The job does not exist")
            self._visible(principal, record)
            return self._serialize(record)

    def payload(self, job_id: str) -> dict:
        record = self.state.get("jobs", job_id)
        if not record:
            raise BackendError("NOT_FOUND", "The job does not exist")
        return record["_payload"]

    def snapshot(self, principal: Principal, limit: int = 1000) -> dict:
        with self.state.transaction():
            fresh = self.auth.refresh(principal)
            jobs = []
            if not 1 <= limit <= 10000:
                raise BackendError("INVALID_ARGUMENT", "Invalid job snapshot bound")
            actor_filter = " AND json_extract(body,'$.actor.id')=?" if fresh.kind != "owner" else ""
            parameters = (fresh.id, limit) if fresh.kind != "owner" else (limit,)
            records = [json.loads(row[0]) for row in self.state.connection.execute("SELECT body FROM records WHERE namespace='jobs'" + actor_filter + " ORDER BY CASE WHEN json_extract(body,'$.state') IN ('completed','failed','canceled') THEN 1 ELSE 0 END, updated DESC, key LIMIT ?", parameters)]
            for record in records:
                try:
                    self._visible(fresh, record)
                except BackendError:
                    continue
                jobs.append(self._serialize(record))
            return {"schemaVersion": 1, "cursor": self.state.cursor(), "jobs": jobs, "stale": False}

    def events(self, principal: Principal, after: int = 0, limit: int = 250) -> dict:
        with self.state.transaction():
            fresh = self.auth.refresh(principal)
            rows = self.state.events(after, actor=None if fresh.kind == "owner" else fresh.id, limit=limit)
            events = []
            for row in rows:
                if row["topic"] != "job":
                    continue
                record = self.state.get("jobs", row["body"]["id"])
                if not record:
                    continue
                try:
                    self._visible(fresh, record)
                except BackendError:
                    continue
                events.append({"schemaVersion": 1, "cursor": row["cursor"], "job": self._serialize(row["body"])})
            cursor = rows[-1]["cursor"] if rows else self.state.cursor()
            oldest = self.state.connection.execute("SELECT MIN(cursor) FROM outbox").fetchone()[0]
            gap = (oldest is not None and after < oldest - 1) or after > self.state.cursor()
            return {"events": events, "cursor": cursor, "gap": gap}

    def _save(self, record: dict, outcome: str) -> dict:
        record["revision"] += 1
        record["updatedAt"] = _stamp(self.clock())
        self.state.put("jobs", record["id"], record)
        self.state.audit(record["actor"]["id"], "job." + outcome, record["state"], record["id"], record["requestId"])
        self.state.emit("job", record["actor"]["id"], record)
        return self._serialize(record)

    def control(self, principal: Principal, job_id: str, action: str) -> dict:
        with self.state.transaction():
            record = self.state.get("jobs", job_id)
            if not record:
                raise BackendError("NOT_FOUND", "The job does not exist")
            fresh = self._visible(principal, record)
            available = self._serialize(record)["capabilities"]
            if action not in available or not available[action]:
                raise BackendError("UNSUPPORTED", "The job does not support this control now")
            if action == "retry":
                payload = dict(record["_payload"], previousAttemptId=job_id)
                receipt = self.accept(fresh, record["operation"], payload, "retry-" + str(uuid.uuid4()))
                retried = self.state.get("jobs", receipt["jobIds"][0])
                retried["_checkpoint"] = record.get("_checkpoint")
                self.state.put("jobs", retried["id"], retried)
                return self.get(fresh, receipt["jobIds"][0])
            if action == "resume":
                self._authorize_payload(self._principal(record), record["operation"], record["_payload"])
            record["state"] = {"pause": "paused" if not record.get("_lease") else "pausing", "resume": "queued", "cancel": "canceling"}[action]
            if action == "resume":
                record["_lease"] = None
                record["_fence"] += 1
                if record['_payload'].get('folderUpload'):
                    record['state'] = 'waiting-for-client'
            return self._save(record, action)

    def acknowledge_cancel(self, principal: Principal, job_id: str) -> dict:
        with self.state.transaction():
            record = self.state.get("jobs", job_id)
            if not record:
                raise BackendError("NOT_FOUND", "The job does not exist")
            self._visible(principal, record)
            if record["state"] != "canceling" or record.get("_lease"):
                raise BackendError("CONFLICT", "A live worker must acknowledge its own cancellation")
            record["state"] = "canceled"
            record["_fence"] += 1
            return self._save(record, "cancel-acknowledged")

    @staticmethod
    def _drives(record: dict) -> set[str]:
        return {value["driveId"] for value in [record.get("source"), record.get("destination"), *record["_payload"].get("targets", [])] if value}

    def claim(self, worker_id: str, lease_seconds: int = 30, *, job_id: str | None = None) -> dict | None:
        if not worker_id or not 1 <= lease_seconds <= 300:
            raise BackendError("INVALID_ARGUMENT", "Invalid worker lease")
        with self.state.transaction():
            records = [json.loads(row[0]) for row in self.state.connection.execute("SELECT body FROM records WHERE namespace='jobs' AND (json_extract(body,'$.state') NOT IN ('completed','failed','canceled') OR json_type(body,'$._lease')='object') ORDER BY updated,key LIMIT ?", (self.max_queued + 1,))]
            for record in records:
                if record["state"] == "canceling" and not record.get("_lease"):
                    record["state"] = "canceled"
                    record["_fence"] += 1
                    self._save(record, "cancel-acknowledged")
            active = [record for record in records if record.get("_lease") and not record.get("_workerStopped") and (record["_lease"]["expires"] > self.clock() or not _owner_stopped(record["_lease"]))]
            if len(active) >= self.max_active:
                return None
            last_actor = self.state.get("scheduler", "last_actor", "")
            candidates = sorted((record for record in records if record["state"] == "queued" and (job_id is not None or record["operation"] not in {"upload", "download"}) and (job_id is None or record["id"] == job_id)), key=lambda record: (record["actor"]["id"] == last_actor, record["createdAt"], record["id"]))
            for record in candidates:
                if sum(other["actor"]["id"] == record["actor"]["id"] for other in active) >= self.per_principal:
                    continue
                if any(sum(drive in self._drives(other) for other in active) >= self.per_volume for drive in self._drives(record)):
                    continue
                try:
                    self._authorize_payload(self._principal(record), record["operation"], record["_payload"])
                except BackendError as exc:
                    record["state"] = "failed"
                    record["error"] = {"code": getattr(exc, "code", "PERMISSION_DENIED"), "message": "The initiating identity is no longer authorized", "retryable": False, "correlationId": record["id"], "recovery": []}
                    self._save(record, "authorization-failed")
                    continue
                record["_fence"] += 1
                record["_workerStopped"] = False
                record["_lease"] = {"worker": worker_id, "expires": self.clock() + lease_seconds, "fence": record["_fence"], "pid": os.getpid(), "processStart": _process_stamp(os.getpid())}
                record["state"] = "preparing"
                self._save(record, "claimed")
                self.state.put("scheduler", "last_actor", record["actor"]["id"])
                return {"job": self._serialize(record), "payload": record["_payload"], "workerId": worker_id, "fence": record["_fence"], "expires": record["_lease"]["expires"]}
            return None

    def claim_id(self, principal: Principal, job_id: str, worker_id: str, lease_seconds: int = 30) -> dict:
        with self.state.transaction():
            record = self.state.get("jobs", job_id)
            if not record:
                raise BackendError("NOT_FOUND", "The job does not exist")
            self._visible(principal, record)
            self._authorize_payload(self._principal(record), record["operation"], record["_payload"])
            if record["state"] not in {"queued", "waiting-for-client"} or record.get("_lease"):
                raise BackendError("CONFLICT", "The client job cannot acquire a lease now")
            record["state"] = "queued"
            self.state.put("jobs", job_id, record)
            lease = self.claim(worker_id, lease_seconds, job_id=job_id)
            if lease is None:
                raise BackendError("UNAVAILABLE", "The bounded worker pool is busy", retryable=True)
            return lease

    def update(self, job_id: str, worker_id: str, fence: int, updates: dict) -> dict:
        record = self.state.get("jobs", job_id)
        if not record:
            raise BackendError("NOT_FOUND", "The job does not exist")
        return self.transition(job_id, record["state"], worker_id, fence, updates)

    def authorize_retry(self, job_id: str, checkpoint: dict) -> None:
        """Internal journal-recovery hook; never expose directly as an adapter command."""
        with self.state.transaction():
            record = self.state.get("jobs", job_id)
            if not record or record["state"] != "failed" or record.get("_lease") or not record.get("_workerStopped"):
                raise BackendError("CONFLICT", "Only reconciled failed work can become retryable")
            self._authorize_payload(self._principal(record), record["operation"], record["_payload"])
            if not isinstance(checkpoint, dict) or not checkpoint.get("reconciled"):
                raise BackendError("INVALID_ARGUMENT", "An explicit reconciled checkpoint is required")
            record["_checkpoint"] = checkpoint
            record["_retrySafe"] = True
            if record.get("error"):
                record["error"]["retryable"] = True
            self._save(record, "reconciled")

    def _lease(self, record: dict, worker_id: str, fence: int) -> None:
        lease = record.get("_lease")
        if not lease or record.get("_fence") != fence or lease["worker"] != worker_id or lease["fence"] != fence or lease["expires"] <= self.clock():
            raise BackendError("STALE_REVISION", "The worker lease is expired or fenced")

    def heartbeat(self, job_id: str, worker_id: str, fence: int, lease_seconds: int = 30) -> None:
        if not 1 <= lease_seconds <= 300:
            raise BackendError("INVALID_ARGUMENT", "Invalid worker lease")
        with self.state.transaction():
            record = self.state.get("jobs", job_id)
            if not record:
                raise BackendError("NOT_FOUND", "The job does not exist")
            self._lease(record, worker_id, fence)
            self._authorize_payload(self._principal(record), record["operation"], record["_payload"])
            record["_lease"]["expires"] = self.clock() + lease_seconds
            self.state.put("jobs", job_id, record)

    def transition(self, job_id: str, new_state: str, worker_id: str | None = None, fence: int | None = None, updates: dict[str, Any] | None = None) -> dict:
        with self.state.transaction():
            record = self.state.get("jobs", job_id)
            if not record:
                raise BackendError("NOT_FOUND", "The job does not exist")
            if worker_id is not None and fence is not None:
                self._lease(record, worker_id, fence)
            elif record["state"] not in {"queued", "waiting-for-client"} or new_state not in {"waiting-for-client", "canceling", "failed"}:
                raise BackendError("STALE_REVISION", "A current worker lease is required")
            if new_state not in _STATES or (new_state not in _TRANSITIONS[record["state"]] and not (new_state == record["state"] and new_state not in _TERMINAL)):
                raise BackendError("CONFLICT", "Invalid job state transition")
            if new_state not in {"failed", "canceling", "canceled"}:
                self._authorize_payload(self._principal(record), record["operation"], record["_payload"])
            updates = updates or {}
            allowed = {"bytesDone", "bytesTotal", "rateBytesPerSecond", "etaSeconds", "error", "outcome", "resultingNames", "childIds", "checkpoint"}
            if not set(updates) <= allowed:
                raise BackendError("INVALID_ARGUMENT", "Invalid job update fields")
            for field in ("bytesDone", "bytesTotal", "rateBytesPerSecond"):
                value = updates.get(field)
                if value is not None and (not isinstance(value, str) or not value.isdigit() or len(value) > 40):
                    raise BackendError("INVALID_ARGUMENT", "Byte values must be decimal strings")
            proposed = dict(record, **{key: value for key, value in updates.items() if key != "checkpoint"})
            if proposed["bytesTotal"] is not None and int(proposed["bytesDone"]) > int(proposed["bytesTotal"]):
                raise BackendError("INVALID_ARGUMENT", "Job progress exceeds total bytes")
            if int(proposed["bytesDone"]) < int(record["bytesDone"]):
                raise BackendError("INVALID_ARGUMENT", "Job progress cannot move backwards")
            if "checkpoint" in updates:
                proposed["_checkpoint"] = updates["checkpoint"]
            proposed["state"] = new_state
            if new_state in _TERMINAL or new_state in {"paused", "waiting-for-client", "waiting-for-drive", "needs-decision"}:
                proposed["_lease"] = None
                proposed["_workerStopped"] = True
                proposed["_fence"] += 1
            return self._save(proposed, "transition")

    def recover(self, job_ids: list[str] | None = None) -> int:
        """Fence expired work; do not automatically replay filesystem effects."""
        count = 0
        with self.state.transaction():
            if job_ids is not None:
                active_records = [record for job_id in set(job_ids) if (record := self.state.get('jobs', job_id))]
            else:
                active_records = [json.loads(row[0]) for row in self.state.connection.execute("SELECT body FROM records WHERE namespace='jobs' AND (json_extract(body,'$.state') NOT IN ('completed','failed','canceled') OR json_type(body,'$._lease')='object') AND json_type(body,'$._lease')='object' LIMIT ?", (self.max_queued + 1,))]
            for record in active_records:
                if job_ids is not None and record["id"] not in job_ids:
                    continue
                lease = record.get("_lease")
                if record["state"] in {"completed", "canceled"} or not lease:
                    continue
                stopped = bool(record.get("_workerStopped")) or _owner_stopped(lease)
                if lease["expires"] > self.clock() and not stopped:
                    continue
                if record["state"] == "failed" and not stopped:
                    continue
                record["_workerStopped"] = stopped
                # An expired but known-live in-process writer remains represented;
                # no new writer may acquire this job until termination is proven.
                if stopped:
                    record["_lease"] = None
                record["_fence"] += 1
                if record['state'] != 'failed':
                    record['_interruptedState'] = record['state']
                record["state"] = "failed"
                record["error"] = {"code": "INTERRUPTED", "message": "Worker interrupted; termination and journal reconciliation are required before retry", "retryable": False, "correlationId": record["id"], "recovery": ["refresh"]}
                self._save(record, "lease-expired")
                count += 1
        return count
