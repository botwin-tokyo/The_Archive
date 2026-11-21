"""Centralized verifier-based identity and scope checks. Never returns credentials."""
from __future__ import annotations

import hashlib
import hmac
import os
import time
import uuid
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Iterable

from .contracts import BackendError, path as validate_path, entry_ref
from .persistence import State, digest

_SCOPES = frozenset({"read", "write", "trash", "purge", "settings", "acquire", "radio", "vault.read", "vault.reveal", "vault.write", "vault.export", "skills.read", "skills.write", "phone_transfer.send", "phone_transfer.read", "phone_transfer.delete"})
_ACTIONS = {
    "list": "read", "stat": "read", "download": "read", "files.list": "read", "files.stat": "read",
    "drives.list": "read", "status": "read", "status.get": "read", "jobs.read": "read", "audit.read": "read",
    "create": "write", "new-folder": "write", "rename": "write", "copy": "write", "move": "write", "upload": "write", "restore": "trash",
}
_ITERATIONS = 240_000


def logical_path(path: str) -> str:
    return validate_path(path)


def _roots(values: Iterable[dict | tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    result = []
    for value in values:
        drive, path = (value.get("driveId", value.get("drive_id")), value.get("path", "")) if isinstance(value, dict) else value
        if not isinstance(drive, str) or not drive or len(drive) > 256:
            raise BackendError("INVALID_ARGUMENT", "Allowed roots require a drive identifier")
        result.append((drive, logical_path(path)))
        if len(result) > 128:
            raise BackendError("INVALID_ARGUMENT", "Too many allowed roots")
    return tuple(sorted(set(result)))


@dataclass(frozen=True)
class Principal:
    id: str
    label: str
    kind: str
    credential_id: str
    scope_version: int
    scopes: tuple[str, ...]
    roots: tuple[tuple[str, str], ...]

    def as_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "kind": self.kind, "credentialId": self.credential_id, "scopeVersion": self.scope_version, "scopes": list(self.scopes), "roots": [{"driveId": drive, "path": path} for drive, path in self.roots]}


class Auth:
    def __init__(self, state: State, *, clock=time.time):
        self.state = state
        self.clock = clock
        self._verified = OrderedDict()
        self._verified_lock = threading.Lock()

    @staticmethod
    def _token(token: str) -> str:
        if not isinstance(token, str) or not 32 <= len(token) <= 512 or not token.isascii() or any(char.isspace() for char in token):
            raise BackendError("PERMISSION_DENIED", "Invalid credentials")
        return token

    @staticmethod
    def _selector(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    @staticmethod
    def _principal(record: dict, credential_id: str) -> Principal:
        return Principal(record["id"], record["label"], record["kind"], credential_id, record["scopeVersion"], tuple(record["scopes"]), _roots(record["roots"]))

    def _store_credential(self, principal_id: str, token: str) -> str:
        self._token(token)
        selector = self._selector(token)
        if self.state.get("credentials", selector):
            raise BackendError("CONFLICT", "This credential is already registered")
        salt = os.urandom(24)
        credential_id = str(uuid.uuid4())
        record = {"id": credential_id, "principalId": principal_id, "salt": salt.hex(), "iterations": _ITERATIONS, "verifier": hashlib.pbkdf2_hmac("sha256", token.encode(), salt, _ITERATIONS).hex(), "revoked": False, "createdAt": time.time()}
        self.state.put("credentials", selector, record)
        self.state.put("credential_ids", credential_id, {"selector": selector})
        return credential_id

    def bootstrap_owner(self, token: str, label: str = "Owner") -> Principal:
        """Requires an explicitly supplied secure enrollment secret; generates no token."""
        with self.state.transaction():
            if self.state.list("principals", limit=1):
                raise BackendError("CONFLICT", "Owner enrollment is already complete")
            return self._register(label, token, _SCOPES, (), "owner")

    def _register(self, label: str, token: str, scopes: Iterable[str], roots: Iterable, kind: str) -> Principal:
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 120 or kind not in ("owner", "agent"):
            raise BackendError("INVALID_ARGUMENT", "Invalid principal description")
        normalized = sorted(set(scopes))
        if not set(normalized) <= _SCOPES:
            raise BackendError("INVALID_ARGUMENT", "Unknown permission scope")
        allowed = _roots(roots)
        if kind != "owner" and ("settings" in normalized or (not allowed and not (normalized and all(scope.startswith(("vault.", "skills.", "phone_transfer.")) for scope in normalized)))):
            raise BackendError("INVALID_ARGUMENT", "Agents need explicit roots and cannot administer settings")
        principal_id = str(uuid.uuid4())
        credential_id = self._store_credential(principal_id, token)
        record = {"id": principal_id, "label": label.strip(), "kind": kind, "scopes": normalized, "roots": [{"driveId": drive, "path": path} for drive, path in allowed], "scopeVersion": 1, "revoked": False, "createdAt": time.time()}
        self.state.put("principals", principal_id, record)
        self.state.audit(principal_id, "credential.register", "registered", credential_id)
        return self._principal(record, credential_id)

    def register(self, owner: Principal, label: str, token: str, scopes: Iterable[str], roots: Iterable, kind: str = "agent") -> Principal:
        with self.state.transaction():
            self.owner(owner)
            if kind != "agent":
                raise BackendError("INVALID_ARGUMENT", "Only one owner is supported")
            result = self._register(label, token, scopes, roots, kind)
            self.state.audit(owner.id, "agent.register", "registered", result.id)
            return result

    def authenticate(self, token: str) -> Principal:
        self._token(token)
        selector = self._selector(token)
        record = self.state.get("credentials", selector)
        if not record or record["revoked"]:
            # Constant-cost rejection avoids a cheap verifier-existence timing oracle.
            hashlib.pbkdf2_hmac("sha256", token.encode(), bytes(24), _ITERATIONS)
            raise BackendError("PERMISSION_DENIED", "Invalid or revoked credentials")
        # Cache only successful expensive verification, never authorization or raw
        # secrets. Read credential and principal records on EVERY request so
        # revocation, rotation and scope changes remain immediately effective.
        proof = (selector, record["id"], record["principalId"], record["salt"], record["iterations"], record["verifier"])
        now = time.monotonic()
        with self._verified_lock:
            cached = self._verified.get(proof, 0) > now
            if cached:
                self._verified.move_to_end(proof)
        if not cached:
            candidate = hashlib.pbkdf2_hmac("sha256", token.encode(), bytes.fromhex(record["salt"]), record["iterations"]).hex()
            if not hmac.compare_digest(candidate, record["verifier"]):
                raise BackendError("PERMISSION_DENIED", "Invalid or revoked credentials")
            with self._verified_lock:
                self._verified[proof] = now + 300
                self._verified.move_to_end(proof)
                while len(self._verified) > 256:
                    self._verified.popitem(last=False)
        principal = self.state.get("principals", record["principalId"])
        if not principal or principal["revoked"]:
            raise BackendError("PERMISSION_REVOKED", "This identity has been revoked")
        return self._principal(principal, record["id"])

    def refresh(self, principal: Principal) -> Principal:
        index = self.state.get("credential_ids", principal.credential_id)
        credential = self.state.get("credentials", index["selector"]) if index else None
        record = self.state.get("principals", principal.id)
        if not record or record["revoked"] or not credential or credential["revoked"] or credential["principalId"] != principal.id:
            raise BackendError("PERMISSION_REVOKED", "This identity or credential has been revoked")
        # Mobile access-token rotation does not change the durable job principal.
        # Its device session still gates every worker/stream authorization refresh.
        if record.get("mobileSessionId"):
            session = self.state.get("mobile_sessions", record["mobileSessionId"])
            account = self.state.get("mobile_accounts", "current")
            if (not session or session.get("revoked") or not account or account.get("revoked")
                    or session.get("accountId") != account["id"]
                    or min(session["absoluteExpires"], session["idleExpires"]) <= self.clock()):
                raise BackendError("PERMISSION_REVOKED", "This mobile device session is unavailable")
            account_roots=_roots(account['roots'])
            if (not set(record['scopes'])<=set(account['scopes'])
                    or any(not any(drive==allowed_drive and (allowed_path=='' or root==allowed_path or root.startswith(allowed_path+'/'))
                                   for allowed_drive,allowed_path in account_roots)
                           for drive,root in _roots(record['roots']))):
                raise BackendError("PERMISSION_REVOKED", "Device permissions exceed the current mobile account assignment")
        if record.get('mobileAcquisitionAccountId'):
            account = self.state.get('mobile_accounts', 'current')
            if not account or account.get('revoked') or account['id'] != record['mobileAcquisitionAccountId']:
                raise BackendError('PERMISSION_REVOKED', 'Mobile acquisition account is unavailable')
            if not set(record['scopes']) <= set(account['scopes']) or _roots(record['roots']) != _roots(account['roots']):
                raise BackendError('PERMISSION_REVOKED', 'Mobile acquisition permissions changed')
        return self._principal(record, principal.credential_id)

    def owner(self, principal: Principal) -> Principal:
        fresh = self.refresh(principal)
        if fresh.kind != "owner":
            raise BackendError("PERMISSION_DENIED", "Owner access is required", details={"actor":fresh.id})
        return fresh

    def authorize(self, principal: Principal, action: str, drive: str | None = None, path: str = "") -> Principal:
        fresh = self.refresh(principal)
        normalized = logical_path(path)
        if drive == 'git' and action not in ('read', 'list', 'download') and self.state.get('git_protection', 'active'):
            if normalized == 'GitHub' or normalized.startswith('GitHub/'):
                raise BackendError('READ_ONLY', 'Generated GitHub backups are read-only; copy files to another folder to edit')
        if fresh.kind == "owner":
            return fresh
        scope = _ACTIONS.get(action, action)
        if scope not in fresh.scopes or scope == "settings":
            raise BackendError("PERMISSION_DENIED", "This identity lacks the required scope", details={"actor":fresh.id})
        if drive is not None and not any(root_drive == drive and (root == "" or normalized == root or normalized.startswith(root + "/")) for root_drive, root in fresh.roots):
            raise BackendError("PERMISSION_DENIED", "This location is outside the allowed roots", details={"actor":fresh.id})
        return fresh

    def list(self, owner: Principal) -> list[dict]:
        self.owner(owner)
        return [{key: value for key, value in record.items() if key in {"id", "label", "kind", "scopes", "roots", "scopeVersion", "revoked", "createdAt"}} for record in self.state.list("principals")]

    def revoke(self, owner: Principal, principal_id: str) -> None:
        with self.state.transaction():
            self.owner(owner)
            record = self.state.get("principals", principal_id)
            if not record:
                raise BackendError("NOT_FOUND", "The identity does not exist")
            if record["kind"] == "owner":
                raise BackendError("PERMISSION_DENIED", "Owner removal requires a separate recovery flow")
            record["revoked"] = True
            record["scopeVersion"] += 1
            self.state.put("principals", principal_id, record)
            self.state.audit(owner.id, "agent.revoke", "revoked", principal_id)
            self.state.emit("identity", principal_id, {"id": principal_id, "revoked": True})

    def update_scopes(self, owner: Principal, principal_id: str, scopes: Iterable[str], roots: Iterable) -> dict:
        with self.state.transaction():
            self.owner(owner)
            record = self.state.get("principals", principal_id)
            if not record or record["kind"] == "owner":
                raise BackendError("PERMISSION_DENIED", "Only agent scopes can be edited")
            normalized, allowed = sorted(set(scopes)), _roots(roots)
            if not set(normalized) <= _SCOPES or "settings" in normalized or (not allowed and not (normalized and all(scope.startswith(("vault.", "skills.", "phone_transfer.")) for scope in normalized))):
                raise BackendError("INVALID_ARGUMENT", "Invalid agent scopes or roots")
            record.update(scopes=normalized, roots=[{"driveId": drive, "path": path} for drive, path in allowed], scopeVersion=record["scopeVersion"] + 1)
            self.state.put("principals", principal_id, record)
            self.state.audit(owner.id, "agent.scopes", "updated", principal_id)
            self.state.emit("identity", principal_id, {"id": principal_id, "scopeVersion": record["scopeVersion"]})
            return {key: record[key] for key in ("id", "scopes", "roots", "scopeVersion")}

    def rotate(self, owner: Principal, principal_id: str, token: str) -> dict:
        with self.state.transaction():
            self.owner(owner)
            record = self.state.get("principals", principal_id)
            if not record or record["revoked"]:
                raise BackendError("NOT_FOUND", "The identity is unavailable")
            credential_id = self._store_credential(principal_id, token)
            for old in self.state.list("credentials", limit=10000):
                if old["principalId"] == principal_id and old["id"] != credential_id:
                    old["revoked"] = True
                    index = self.state.get("credential_ids", old["id"])
                    self.state.put("credentials", index["selector"], old)
            record["scopeVersion"] += 1
            self.state.put("principals", principal_id, record)
            self.state.audit(owner.id, "credential.rotate", "rotated", credential_id)
            return {"principalId": principal_id, "credentialId": credential_id}


class Confirmations:
    def __init__(self, state: State, auth: Auth, clock=time.time):
        self.state, self.auth, self.clock = state, auth, clock

    @staticmethod
    def _digest(action: str, targets: list[dict], drive_generations: dict) -> str:
        if not targets or len(targets) > 1000 or not isinstance(drive_generations, dict):
            raise BackendError("INVALID_ARGUMENT", "Confirmation requires a bounded exact target set")
        for target in targets:
            entry_ref(target)
        identities = [(target["driveId"], target["id"]) for target in targets]
        if len(set(identities)) != len(identities) or set(drive_generations) != {target["driveId"] for target in targets} or any(isinstance(value, bool) or not isinstance(value, (str, int)) or value == "" for value in drive_generations.values()):
            raise BackendError("INVALID_ARGUMENT", "Confirmation must bind unique targets and exact drive generations")
        return digest({"action": action, "targets": sorted(targets, key=lambda value: digest(value)), "driveGenerations": drive_generations})

    def issue(self, principal: Principal, action: str, targets: list[dict], drive_generations: dict, ttl: int = 60) -> dict:
        if action not in ("purge", "empty-trash") or not 1 <= ttl <= 300:
            raise BackendError("INVALID_ARGUMENT", "Invalid confirmation action or expiry")
        with self.state.transaction() as connection:
            fresh = self.auth.refresh(principal)
            for target in targets:
                self.auth.authorize(fresh, "purge", target.get("driveId"), target.get("path", ""))
            value = self._digest(action, targets, drive_generations)
            grant_id, expires = str(uuid.uuid4()), self.clock() + ttl
            self.state.mark_guard()
            connection.execute("INSERT INTO confirmations VALUES(?,?,?,?,?,?,NULL)", (grant_id, fresh.id, fresh.credential_id, fresh.scope_version, value, expires))
            self.state.audit(fresh.id, "confirmation.issue", "issued", grant_id)
            return {"id": grant_id, "expiresAt": expires, "digest": value, "count": len(targets)}

    def consume(self, principal: Principal, action: str, targets: list[dict], drive_generations: dict, grant_id: str) -> None:
        with self.state.transaction() as connection:
            fresh = self.auth.refresh(principal)
            for target in targets:
                self.auth.authorize(fresh, "purge", target.get("driveId"), target.get("path", ""))
            row = connection.execute("SELECT * FROM confirmations WHERE id=?", (grant_id,)).fetchone()
            if not row or row["consumed"] is not None or row["expires"] <= self.clock() or row["actor"] != fresh.id or row["credential"] != fresh.credential_id or row["scope_version"] != fresh.scope_version or not hmac.compare_digest(row["digest"], self._digest(action, targets, drive_generations)):
                raise BackendError("CONFIRMATION_REQUIRED", "Confirmation expired or no longer matches these targets")
            connection.execute("UPDATE confirmations SET consumed=? WHERE id=? AND consumed IS NULL", (self.clock(), grant_id))
            self.state.mark_guard()
            self.state.audit(fresh.id, action, "confirmed", grant_id)
