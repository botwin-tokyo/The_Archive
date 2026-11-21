"""Durable, serialized metadata. Filesystem effects belong in operation journals."""
from __future__ import annotations

import hashlib
import json
import os
import fcntl
import stat
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .contracts import BackendError

_SCHEMA = """
CREATE TABLE records(namespace TEXT NOT NULL, key TEXT NOT NULL, body TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, updated REAL NOT NULL, PRIMARY KEY(namespace,key));
CREATE TABLE idempotency(actor TEXT NOT NULL, operation TEXT NOT NULL, key TEXT NOT NULL, digest TEXT NOT NULL, receipt TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(actor,operation,key));
CREATE TABLE audit(sequence INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, operation TEXT NOT NULL, subject TEXT, outcome TEXT NOT NULL, correlation_id TEXT, at REAL NOT NULL);
CREATE TABLE outbox(cursor INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT NOT NULL, actor TEXT NOT NULL, body TEXT NOT NULL, at REAL NOT NULL);
CREATE INDEX outbox_actor ON outbox(actor,cursor);
CREATE TABLE confirmations(id TEXT PRIMARY KEY, actor TEXT NOT NULL, credential TEXT NOT NULL, scope_version INTEGER NOT NULL, digest TEXT NOT NULL, expires REAL NOT NULL, consumed REAL);
"""
_MIGRATIONS = [(1, _SCHEMA), (2, "CREATE TABLE mobile_compatibility(schema_version INTEGER NOT NULL CHECK(schema_version=2)); INSERT INTO mobile_compatibility VALUES(2); CREATE INDEX mobile_intent_token ON records(json_extract(body,'$.token')) WHERE namespace='intents' AND json_valid(body);")]
_MAX_DOCUMENT = 2 * 1024 * 1024


def canonical(value: Any) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise BackendError("INVALID_ARGUMENT", "Metadata must be finite JSON") from exc
    if len(encoded.encode("utf-8")) > _MAX_DOCUMENT:
        raise BackendError("INVALID_ARGUMENT", "Metadata document exceeds its size limit")
    return encoded


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _database_error(exc: sqlite3.Error) -> BackendError:
    code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
    if code == sqlite3.SQLITE_FULL:
        return BackendError("INSUFFICIENT_SPACE", "The metadata store has no free space")
    if code == sqlite3.SQLITE_READONLY:
        return BackendError("READ_ONLY", "The metadata store is read only")
    if code in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
        return BackendError("INTEGRITY_FAILED", "The metadata store failed integrity validation")
    return BackendError("STATE_UNAVAILABLE", "The metadata store is unavailable", retryable=True)


class State:
    schema_version = 2

    def __init__(self, path: str | Path, *, mobile_recovery=False, validated_identity=None):
        self.path = str(path)
        self.validated_identity = validated_identity
        self._lock = threading.RLock()
        self._local = threading.local()
        self._closed = False
        self._service_lock_fd = None
        self._receipt_guard=None
        self._mobile_recovery=bool(mobile_recovery)
        try:
            if self.path != ':memory:' and not self.path.startswith('file:'):
                if validated_identity is None:
                    try:
                        descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                        os.close(descriptor)
                    except FileExistsError:
                        pass
                info=os.stat(self.path,follow_symlinks=False)
                parent=os.stat(str(Path(self.path).parent),follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid() or info.st_mode & 0o077 or not stat.S_ISDIR(parent.st_mode) or parent.st_uid!=os.geteuid() or parent.st_mode & 0o022:
                    raise BackendError('PERMISSION_DENIED','State must be a private owned regular file in a protected directory')
                if validated_identity is not None and tuple(validated_identity) != (info.st_dev, info.st_ino):
                    raise BackendError('INTEGRITY_FAILED', 'Worker metadata store differs from the validated service store')
            elif validated_identity is not None:
                raise BackendError('INVALID_ARGUMENT', 'Worker state requires a durable filesystem path')
            self._connection = sqlite3.connect(self.path, timeout=5, isolation_level=None, check_same_thread=False)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            if validated_identity is None:
                self._migrate()
            else:
                # The parent already migrated and checked this exact database.
                # Spawned workers must not rescan its entire history or rerun
                # migrations before they can acknowledge their lease.
                rows = dict(self._connection.execute('SELECT version,checksum FROM migrations'))
                expected = {version: hashlib.sha256(sql.encode()).hexdigest() for version, sql in _MIGRATIONS}
                if rows != expected:
                    raise BackendError('INTEGRITY_FAILED', 'Worker metadata schema differs from the validated service schema')
            if validated_identity is None and self._connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise BackendError("INTEGRITY_FAILED", "The metadata store failed integrity validation")
            from .mobile_guard import ReceiptGuard
            self._receipt_guard=ReceiptGuard.attach_existing(self)
        except sqlite3.Error as exc:
            if hasattr(self, "_connection"):
                self._connection.close()
            raise _database_error(exc) from exc
        except BackendError:
            if hasattr(self, "_connection"):
                self._connection.close()
            raise
        except OSError as exc:
            if hasattr(self, "_connection"):
                self._connection.close()
            raise BackendError("STATE_UNAVAILABLE", "The metadata store cannot be opened", retryable=True) from exc

    def _migrate(self) -> None:
        with self.transaction() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS migrations(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied REAL NOT NULL)")
            rows = {row["version"]: row["checksum"] for row in connection.execute("SELECT * FROM migrations")}
            if any(version > self.schema_version for version in rows):
                raise BackendError("UNSUPPORTED", "Metadata schema is newer than this service")
            for version, sql in _MIGRATIONS:
                checksum = hashlib.sha256(sql.encode()).hexdigest()
                if version in rows:
                    if rows[version] != checksum:
                        raise BackendError("INTEGRITY_FAILED", "Metadata migration checksum mismatch")
                    continue
                for statement in sql.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                connection.execute("INSERT INTO migrations VALUES(?,?,?)", (version, checksum, time.time()))

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._closed:
                raise BackendError("STATE_UNAVAILABLE", "The metadata store is closed")
            depth = getattr(self._local, "depth", 0)
            savepoint = f"nested_{depth}"
            advanced=False
            try:
                self._connection.execute("BEGIN IMMEDIATE" if depth == 0 else f"SAVEPOINT {savepoint}")
                self._local.depth = depth + 1
                if depth==0:
                    self._local.guard_required=False
                    if self._receipt_guard: self._receipt_guard.verify()
                yield self._connection
                if depth==0 and self._receipt_guard and self._local.guard_required:
                    self._receipt_guard.advance(); advanced=True
                self._connection.execute("COMMIT" if depth == 0 else f"RELEASE SAVEPOINT {savepoint}")
                if advanced: self._receipt_guard.blocked=False
            except BaseException as exc:
                if advanced and self._receipt_guard: self._receipt_guard.blocked=True
                try:
                    if depth == 0:
                        if self._connection.in_transaction:
                            self._connection.execute("ROLLBACK")
                    else:
                        self._connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                        self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                except sqlite3.Error:
                    pass
                if isinstance(exc, sqlite3.Error):
                    raise _database_error(exc) from exc
                raise
            finally:
                self._local.depth = depth

    @staticmethod
    def _keys(namespace: str, key: str | None = None) -> None:
        if not isinstance(namespace, str) or not namespace or len(namespace) > 80:
            raise BackendError("INVALID_ARGUMENT", "Invalid record namespace")
        if key is not None and (not isinstance(key, str) or not key or len(key) > 512):
            raise BackendError("INVALID_ARGUMENT", "Invalid record identifier")

    def get(self, namespace: str, key: str, default: Any = None) -> Any:
        self._keys(namespace, key)
        with self._lock:
            if self._closed:
                raise BackendError("STATE_UNAVAILABLE", "The metadata store is closed")
            if self._receipt_guard: self._receipt_guard.assert_available()
            try:
                row = self._connection.execute("SELECT body FROM records WHERE namespace=? AND key=?", (namespace, key)).fetchone()
                return json.loads(row[0]) if row else default
            except sqlite3.Error as exc:
                raise _database_error(exc) from exc
            except (ValueError, TypeError) as exc:
                raise BackendError("INTEGRITY_FAILED", "Stored metadata is malformed") from exc

    def put(self, namespace: str, key: str, value: Any) -> None:
        self._keys(namespace, key)
        encoded = canonical(value)
        with self.transaction() as connection:
            from .mobile_guard import CRITICAL_NAMESPACES
            if namespace in CRITICAL_NAMESPACES: self.mark_guard()
            connection.execute("INSERT INTO records VALUES(?,?,?,1,?) ON CONFLICT(namespace,key) DO UPDATE SET body=excluded.body,revision=records.revision+1,updated=excluded.updated", (namespace, key, encoded, time.time()))

    def list(self, namespace: str, limit: int = 1000, *, after: str = "") -> list[Any]:
        self._keys(namespace)
        if not 1 <= limit <= 10000:
            raise BackendError("INVALID_ARGUMENT", "Record page limit must be between 1 and 10000")
        with self._lock:
            if self._receipt_guard: self._receipt_guard.assert_available()
            try:
                return [json.loads(row[0]) for row in self._connection.execute("SELECT body FROM records WHERE namespace=? AND key>? ORDER BY key LIMIT ?", (namespace, after, limit))]
            except sqlite3.Error as exc:
                raise _database_error(exc) from exc
            except (ValueError, TypeError) as exc:
                raise BackendError("INTEGRITY_FAILED", "Stored metadata is malformed") from exc

    def delete(self, namespace: str, key: str) -> None:
        self._keys(namespace, key)
        with self.transaction() as connection:
            from .mobile_guard import CRITICAL_NAMESPACES
            if namespace in CRITICAL_NAMESPACES: self.mark_guard()
            connection.execute("DELETE FROM records WHERE namespace=? AND key=?", (namespace, key))

    def audit(self, actor: str, operation: str, outcome: str, subject: str | None = None, correlation_id: str | None = None) -> None:
        with self.transaction() as connection:
            connection.execute("INSERT INTO audit(actor,operation,subject,outcome,correlation_id,at) VALUES(?,?,?,?,?,?)", (actor, operation, subject, outcome, correlation_id, time.time()))

    def emit(self, topic: str, actor: str, body: Any) -> int:
        with self.transaction() as connection:
            row = connection.execute("INSERT INTO outbox(topic,actor,body,at) VALUES(?,?,?,?)", (topic, actor, canonical(body), time.time()))
            cursor = int(row.lastrowid)
            if cursor > 10000:
                connection.execute("DELETE FROM outbox WHERE cursor<=?", (cursor - 10000,))
            return cursor

    def cursor(self) -> int:
        with self._lock:
            return int(self._connection.execute("SELECT COALESCE(MAX(cursor),0) FROM outbox").fetchone()[0])

    def events(self, after: int, *, actor: str | None = None, limit: int = 250) -> list[dict]:
        if after < 0 or not 1 <= limit <= 1000:
            raise BackendError("INVALID_ARGUMENT", "Invalid event cursor or limit")
        with self._lock:
            query = "SELECT * FROM outbox WHERE cursor>?" + (" AND actor=?" if actor is not None else "") + " ORDER BY cursor LIMIT ?"
            values = (after, actor, limit) if actor is not None else (after, limit)
            return [{"cursor": row["cursor"], "topic": row["topic"], "actor": row["actor"], "body": json.loads(row["body"])} for row in self._connection.execute(query, values)]

    def atomic_accept(self, actor: str, operation: str, key: str, request_digest: str, job: dict, audit: dict | None = None) -> dict:
        if not isinstance(key, str) or not key or len(key) > 256:
            raise BackendError("INVALID_ARGUMENT", "An idempotency key is required")
        with self.transaction() as connection:
            previous = connection.execute("SELECT digest,receipt FROM idempotency WHERE actor=? AND operation=? AND key=?", (actor, operation, key)).fetchone()
            if previous:
                if previous["digest"] != request_digest:
                    raise BackendError("CONFLICT", "Idempotency key was already used for a different request")
                return json.loads(previous["receipt"])
            receipt = {"status": "accepted", "requestId": key, "jobIds": [job["id"]], "simulated": False}
            self.mark_guard()
            self.put("jobs", job["id"], job)
            self.audit(actor, operation, "accepted", job["id"], (audit or {}).get("correlationId", key))
            self.emit("job", actor, job)
            connection.execute("INSERT INTO idempotency VALUES(?,?,?,?,?,?)", (actor, operation, key, request_digest, canonical(receipt), time.time()))
            return receipt

    def reconcile(self, actor: str, operation: str, key: str) -> dict | None:
        with self._lock:
            if self._receipt_guard: self._receipt_guard.assert_available()
            row = self._connection.execute("SELECT receipt FROM idempotency WHERE actor=? AND operation=? AND key=?", (actor, operation, key)).fetchone()
            return json.loads(row[0]) if row else None

    def backup(self, destination: str | Path) -> None:
        """Consistent metadata backup only; caller owns approved destination selection."""
        with self._lock:
            if self._connection.in_transaction:
                raise BackendError("CONFLICT", "Metadata backup cannot run inside a write transaction")
            try:
                try:
                    descriptor = os.open(str(destination), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    os.close(descriptor)
                except FileExistsError:
                    raise BackendError("CONFLICT", "Metadata backup destination already exists")
                with sqlite3.connect(str(destination)) as target:
                    self._connection.backup(target)
            except sqlite3.Error as exc:
                raise _database_error(exc) from exc

    @property
    def connection(self) -> sqlite3.Connection:
        """Direct SQL is for trusted modules inside transaction(), never transport input."""
        if self._receipt_guard: self._receipt_guard.assert_available()
        return self._connection

    def mark_guard(self):
        if self._receipt_guard: self._local.guard_required=True

    def verify_guard(self):
        with self.transaction(): pass

    def scan(self, namespace: str, after: str = "", limit: int = 500) -> list[dict]:
        self._keys(namespace)
        if not 1 <= limit <= 1000:
            raise BackendError("INVALID_ARGUMENT", "Invalid record page size")
        with self.transaction() as connection:
            return [{"key": row["key"], "value": json.loads(row["body"]), "revision": row["revision"]} for row in connection.execute("SELECT key,body,revision FROM records WHERE namespace=? AND key>? ORDER BY key LIMIT ?", (namespace, after, limit))]

    def select(self, sql: str, parameters: tuple = ()) -> list[dict]:
        if not sql.lstrip().upper().startswith("SELECT"):
            raise BackendError("INVALID_ARGUMENT", "Read helper accepts SELECT only")
        with self.transaction() as connection:
            return [dict(row) for row in connection.execute(sql, parameters)]

    def acquire_service_lock(self) -> None:
        """Exclusive control-service admission lock; spawned workers never acquire it."""
        with self._lock:
            if self.path == ':memory:' or self._service_lock_fd is not None:
                if self._service_lock_fd is not None:
                    return
                raise BackendError("INVALID_ARGUMENT", "Service lock requires durable state")
            descriptor = None
            try:
                descriptor = os.open(self.path + '.lock', os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_mode & 0o077:
                    raise BackendError("PERMISSION_DENIED", "Service lock is not a private owned regular file")
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._service_lock_fd = descriptor
            except BlockingIOError as exc:
                if descriptor is not None:
                    os.close(descriptor)
                raise BackendError("BUSY", "Another control service owns this metadata store") from exc
            except (OSError, BackendError) as exc:
                if descriptor is not None:
                    os.close(descriptor)
                if isinstance(exc, BackendError):
                    raise
                raise BackendError("STATE_UNAVAILABLE", "The service lock is unavailable") from exc

    def release_service_lock(self) -> None:
        with self._lock:
            if self._service_lock_fd is not None:
                fcntl.flock(self._service_lock_fd, fcntl.LOCK_UN)
                os.close(self._service_lock_fd)
                self._service_lock_fd = None

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._connection.close()
                if self._receipt_guard: self._receipt_guard.close(); self._receipt_guard=None
                self._closed = True
                self.release_service_lock()
