"""Bounded, allowlisted diagnostics. File paths and credentials never enter telemetry."""
from __future__ import annotations

import re
import queue
import math
import threading
import time
import uuid
from collections import Counter, deque

from .contracts import BackendError
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .security import Auth, Principal
from .persistence import State

_MESSAGES = {
    "STATE_UNAVAILABLE": "The metadata store is unavailable",
    "INSUFFICIENT_SPACE": "Available storage is insufficient",
    "INVALID_ARGUMENT": "The request failed validation",
    "UNAVAILABLE": "A service dependency is unavailable",
    "READ_ONLY": "The requested destination is read only",
    "FULL_DISK": "Available storage is insufficient",
    "PERMISSION_DENIED": "The operation was not authorized",
    "PERMISSION_REVOKED": "The initiating identity lost access",
    "STALE_REVISION": "A referenced revision changed",
    "INVALID_REQUEST": "The request failed validation",
    "INVALID_PATH": "A logical path failed validation",
    "CONFLICT": "An explicit conflict decision is required",
    "INTERRUPTED": "Work was interrupted and requires reconciliation",
    "INTEGRITY_FAILED": "Integrity verification failed",
    "NOT_FOUND": "The requested resource is unavailable",
    "TIMEOUT": "A bounded operation timed out",
    "UNSUPPORTED": "The requested capability is unsupported",
    "CONFIRMATION_REQUIRED": "A fresh exact confirmation is required",
    "OK": "The operation completed",
}
_UUID = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")


def redact(value: dict) -> dict:
    """Never forwards arbitrary message/headers/paths/IDs from an exception."""
    code = value.get("code", "UNAVAILABLE")
    if code not in _MESSAGES:
        code = "UNAVAILABLE"
    correlation = value.get("correlationId")
    return {"code": code, "message": _MESSAGES[code], "correlationId": correlation if isinstance(correlation, str) and _UUID.fullmatch(correlation) else str(uuid.uuid4()), "retryable": value.get("retryable") is True}


class Observability:
    def __init__(self, state: State | None = None, auth: Auth | None = None, *, limit: int = 200, sink=None):
        if not 1 <= limit <= 1000:
            raise BackendError("INVALID_REQUEST", "Invalid diagnostic retention bound")
        self.state, self.auth, self.sink = state, auth, sink
        self._records = deque(maxlen=limit)
        self._counters = Counter()
        self._lock = threading.Lock()
        self._sink_queue = queue.Queue(maxsize=limit)
        self._stopping = threading.Event()
        self._worker = None
        if sink is not None:
            self._worker = threading.Thread(target=self._drain, name="bounded-diagnostic-sink", daemon=True)
            self._worker.start()

    def record(self, code: str, *, level: str = "info", correlation_id: str | None = None, **_discarded) -> dict:
        safe = redact({"code": code, "correlationId": correlation_id})
        row = dict(safe, id=str(uuid.uuid4()), at=time.time(), level=level if level in {"info", "warning", "error"} else "error", simulated=False)
        with self._lock:
            if len(self._records) == self._records.maxlen:
                self._counters["dropped"] += 1
            self._records.append(row)
            self._counters[safe["code"]] += 1
        if self.sink is not None:
            try:
                self._sink_queue.put_nowait(dict(row))
            except queue.Full:
                with self._lock:
                    self._counters["sink_dropped"] += 1
        return dict(row)

    def _drain(self):
        while not self._stopping.is_set():
            try:
                row = self._sink_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                self.sink(row)
            except Exception:
                with self._lock:
                    self._counters["sink_errors"] += 1
            finally:
                self._sink_queue.task_done()

    def close(self):
        self._stopping.set()
        if self._worker:
            self._worker.join(timeout=0.1)

    def list(self, principal: Principal, limit: int = 100) -> list[dict]:
        if self.auth is None:
            raise BackendError("UNAVAILABLE", "Diagnostic authorization is unavailable")
        self.auth.owner(principal)
        if not 1 <= limit <= 1000:
            raise BackendError("INVALID_REQUEST", "Invalid diagnostic page size")
        with self._lock:
            return [dict(row) for row in list(self._records)[-limit:]]

    def counters(self) -> dict:
        with self._lock:
            return dict(self._counters)

    def audit(self, principal: Principal, *, after: int = 0, limit: int = 100) -> list[dict]:
        if not self.state or not self.auth:
            raise BackendError("UNAVAILABLE", "Audit access is unavailable")
        fresh = self.auth.refresh(principal)
        if not 1 <= limit <= 500 or after < 0:
            raise BackendError("INVALID_REQUEST", "Invalid audit cursor or page size")
        where = "sequence>?" + (" AND actor=?" if fresh.kind != "owner" else "")
        parameters = (after, fresh.id, limit) if fresh.kind != "owner" else (after, limit)
        # Scope-restricted agents cannot use their old audit records to rediscover revoked roots.
        if fresh.kind != "owner":
            self.auth.authorize(fresh, "read")
        with self.state.transaction() as connection:
            rows = connection.execute(f"SELECT sequence,actor,operation,outcome,correlation_id,at FROM audit WHERE {where} ORDER BY sequence LIMIT ?", parameters)
            return [dict(row) for row in rows]


def aggregate_health(status: dict, drives: list[dict], jobs: list[dict], *, uptime: float = 0, version: str = "0.1.0") -> dict:
    counts = Counter(job.get("state", "unknown") if isinstance(job.get("state"), str) else "unknown" for job in jobs[:10000])
    allowed_states = {"queued", "preparing", "running", "verifying", "committing", "completed", "paused", "pausing", "waiting-for-client", "waiting-for-drive", "needs-decision", "failed", "canceling", "canceled"}
    known_statuses = {"ready", "starting", "disabled", "error", "not-configured", "unavailable"}
    safe_drives = []
    for index, drive in enumerate(drives[:32]):
        safe_drives.append({"slot": index, "role": drive.get("role") if drive.get("role") in {"files", "harness-backups"} else "unknown", "state": drive.get("state") if drive.get("state") in {"online", "offline", "read-only", "full", "unconfigured"} else "unknown", "capacityBytes": _bytes(drive.get("capacityBytes")), "freeBytes": _bytes(drive.get("freeBytes")), "temperatureC": drive.get("temperatureC") if isinstance(drive.get("temperatureC"), (int, float)) and -50 <= drive["temperatureC"] <= 150 else None})
    return {"schemaVersion": 1, "at": time.time(), "uptimeSeconds": max(0, min(float(uptime), 10**12)) if isinstance(uptime, (int, float)) and math.isfinite(uptime) else 0, "version": version if re.fullmatch(r"\d+\.\d+\.\d+", version) else "unknown", "api": status.get("api") if status.get("api") in known_statuses else "unknown", "mcp": status.get("mcp") if status.get("mcp") in known_statuses else "unknown", "drives": safe_drives, "jobs": {key: counts[key] for key in sorted(allowed_states) if counts[key]}, "unknownJobStates": sum(count for key, count in counts.items() if key not in allowed_states)}


def _bytes(value):
    return value if isinstance(value, str) and value.isdigit() and len(value) <= 40 else None
