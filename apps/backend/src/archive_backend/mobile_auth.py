"""Opt-in personal mobile account and device sessions; no public enrollment.

Optional dependencies load only when MobileAuth is constructed. Tokens are random
opaque values; verifiers persist. Only bounded retry responses are Fernet-encrypted.
The caller supplies the replay key; this module never provisions production keys.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from .contracts import BackendError, canonical, identifier, object_fields
from .security import Principal, _roots

ACCESS_SECONDS = 15 * 60
IDLE_SECONDS = 30 * 86400
ABSOLUTE_SECONDS = 90 * 86400
REPLAY_SECONDS = 60


def stamp(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def token_digest(token):
    if not isinstance(token, str) or not 32 <= len(token) <= 512 or not token.isascii() or any(c.isspace() for c in token):
        raise BackendError('UNAUTHENTICATED', 'Sign in again')
    return hashlib.sha256(token.encode('ascii')).hexdigest()


def request_id(value):
    if not isinstance(value, str) or len(value) != 36:
        raise BackendError('INVALID_ARGUMENT', 'A UUID requestId is required')
    try:
        parsed = uuid.UUID(value)
        if str(parsed) != value.lower() or parsed.variant != uuid.RFC_4122 or parsed.version not in range(1,9): raise ValueError()
    except ValueError:
        raise BackendError('INVALID_ARGUMENT', 'A UUID requestId is required') from None
    return value.lower()


@dataclass(frozen=True)
class DeviceContext:
    session_id: str
    account_id: str
    principal: Principal


class MobileAuth:
    def __init__(self, state, auth, replay_key, *, clock=time.time, max_sessions=16):
        from argon2 import PasswordHasher
        from cryptography.fernet import Fernet
        self.state, self.auth, self.clock = state, auth, clock
        self.cipher = Fernet(replay_key)
        self.hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)
        self._password_slot = threading.BoundedSemaphore(1)
        self._dummy_hash = self.hasher.hash(secrets.token_urlsafe(32))
        self.max_sessions = max_sessions
        with state.transaction():
            info = state.get('mobile_meta', 'server')
            if info is None:
                info = {'id': str(uuid.uuid4()), 'schemaVersion': 1}
                state.put('mobile_meta', 'server', info)
            self.server_id = info['id']
        from .mobile_guard import ReceiptGuard
        ReceiptGuard.enable(state,self.server_id)

    @staticmethod
    def _username(value):
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 128 or any(ord(c) < 32 for c in value):
            raise BackendError('INVALID_ARGUMENT', 'Invalid account name')
        return value.strip().casefold()

    @staticmethod
    def _password(value):
        if not isinstance(value, str) or not 12 <= len(value.encode('utf-8')) <= 1024:
            raise BackendError('INVALID_ARGUMENT', 'Password must contain 12–1024 UTF-8 bytes')
        return value

    @staticmethod
    def _assignment(scopes, roots):
        if not isinstance(scopes, list) or not scopes or not set(scopes) <= {'read', 'write', 'trash', 'acquire'} or 'read' not in scopes:
            raise BackendError('INVALID_ARGUMENT', 'Mobile scope assignment requires read and excludes administration/purge')
        normalized = _roots(roots)
        from .mobile_contracts import mobile_path
        for _,root_path in normalized: mobile_path(root_path)
        # Match the centralized explicit-root ceiling; older clients must update their schema.
        if not 1 <= len(normalized) <= 128:
            raise BackendError('INVALID_ARGUMENT', 'Assign between one and 128 explicit mobile roots')
        return sorted(set(scopes)), [{'driveId': d, 'path': p} for d, p in normalized]

    def enroll(self, owner, username, password, scopes, roots):
        """Private operator call only. Never mounted on the mobile HTTP surface."""
        self.auth.owner(owner)
        username = self._username(username); password = self._password(password)
        scopes, roots = self._assignment(scopes, roots)
        encoded = self.hasher.hash(password)
        with self.state.transaction():
            if self.state.get('mobile_accounts', 'current'):
                raise BackendError('CONFLICT', 'Mobile account already enrolled; use private recovery')
            account = {'id': str(uuid.uuid4()), 'username': username, 'passwordHash': encoded,
                       'scopes': scopes, 'roots': roots, 'revision': 1, 'revoked': False}
            self.state.put('mobile_accounts', 'current', account)
            self.state.audit(owner.id, 'mobile.enroll', 'completed', account['id'])
        return {'accountId': account['id']}

    def _rate(self, username, peer):
        # All attempts cost budget, including successful/unknown-account attempts.
        # Hash keys avoid retaining cleartext usernames or client addresses.
        now = self.clock(); window = int(now // 300)
        keys = [('global', 120), ('account:' + username, 10), ('peer:' + str(peer)[:256], 30)]
        with self.state.transaction() as c:
            c.execute("DELETE FROM records WHERE namespace='mobile_rate' AND json_extract(body,'$.window') < ?", (window,))
            count = c.execute("SELECT COUNT(*) FROM records WHERE namespace='mobile_rate'").fetchone()[0]
            if count >= 4096: raise BackendError('RATE_LIMITED', 'Try signing in later', True)
            records = []
            for key, limit in keys:
                key = hashlib.sha256(key.encode()).hexdigest()
                record = self.state.get('mobile_rate', key, {'window': window, 'count': 0})
                if record['count'] >= limit: raise BackendError('RATE_LIMITED', 'Try signing in later', True)
                records.append((key, record))
            for key, record in records:
                record['count'] += 1
                self.state.put('mobile_rate', key, record)

    def _context(self, session):
        if not session or session['revoked']:
            raise BackendError('PERMISSION_REVOKED', 'This device session has been revoked')
        if min(session['idleExpires'], session['absoluteExpires']) <= self.clock():
            raise BackendError('AUTH_EXPIRED', 'Sign in again')
        saved = session['principal']
        principal = Principal(saved['id'], saved['label'], saved['kind'], saved['credentialId'], saved['scopeVersion'], tuple(saved['scopes']), tuple((r['driveId'], r['path']) for r in saved['roots']))
        principal = self.auth.refresh(principal)
        return DeviceContext(session['id'], session['accountId'], principal)

    def _tokens(self, session):
        count=self.state.connection.execute("SELECT COUNT(*) FROM records WHERE namespace='mobile_tokens' AND json_extract(body,'$.session')=? ",(session['id'],)).fetchone()[0]
        if count+2>8192: raise BackendError('RATE_LIMITED','Device token retention budget reached; wait before refreshing',True)
        now = self.clock(); access = secrets.token_urlsafe(48); refresh = secrets.token_urlsafe(48)
        session['idleExpires'] = min(now + IDLE_SECONDS, session['absoluteExpires'])
        session['lastSeen'] = now; session['refreshDigest'] = token_digest(refresh)
        self.state.put('mobile_tokens', token_digest(access), {'kind': 'access', 'session': session['id'], 'expires': min(now + ACCESS_SECONDS, session['absoluteExpires'])})
        self.state.put('mobile_tokens', token_digest(refresh), {'kind': 'refresh', 'session': session['id'], 'expires': session['idleExpires'], 'used': False})
        self.state.put('mobile_sessions', session['id'], session)
        return {'schemaVersion': 1, 'serverId': self.server_id, 'accountId': session['accountId'],
                'deviceSessionId': session['id'], 'accessToken': access, 'refreshToken': refresh,
                'accessExpiresAt': stamp(min(now + ACCESS_SECONDS, session['absoluteExpires'])),
                'refreshExpiresAt': stamp(session['idleExpires'])}

    def _cache(self, key, session, response, binding):
        self.state.put('mobile_replays', key, {'session': session['id'], 'binding': binding,
            'expires': self.clock() + REPLAY_SECONDS,
            'refreshDigest': session['refreshDigest'],
            'ciphertext': self.cipher.encrypt(canonical(response).encode()).decode('ascii')})

    def _cached(self, key, binding):
        from cryptography.fernet import InvalidToken
        cache = self.state.get('mobile_replays', key)
        if not cache or cache['expires'] <= self.clock() or cache['binding'] != binding:
            return None
        session = self.state.get('mobile_sessions', cache['session'])
        self._context(session)
        if session['refreshDigest'] != cache['refreshDigest']:
            return None
        try:
            return json.loads(self.cipher.decrypt(cache['ciphertext'].encode()))
        except (InvalidToken, ValueError):
            raise BackendError('AUTH_EXPIRED', 'Sign in again') from None

    def login(self, args, peer='local'):
        from argon2.exceptions import VerificationError, InvalidHashError
        object_fields(args, ('username', 'password', 'deviceLabel', 'requestId'))
        rid = request_id(args['requestId'])
        username = self._username(args['username'])
        label = args['deviceLabel']
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 100 or any(ord(c) < 32 for c in label):
            raise BackendError('INVALID_ARGUMENT', 'Invalid device label')
        self._rate(username, peer)
        password = args['password']
        valid_shape = isinstance(password, str) and 1 <= len(password.encode('utf-8')) <= 1024
        account = self.state.get('mobile_accounts', 'current')
        matching = bool(account and account['username'] == username and not account['revoked'])
        if not self._password_slot.acquire(blocking=False):
            raise BackendError('RATE_LIMITED', 'Try signing in later', True)
        try:
            try:
                valid = self.hasher.verify(account['passwordHash'] if matching else self._dummy_hash, password if valid_shape else 'invalid')
            except (VerificationError, InvalidHashError): valid = False
        finally: self._password_slot.release()
        if not (valid and matching and valid_shape):
            raise BackendError('UNAUTHENTICATED', 'Account name or password was not accepted')
        binding = hashlib.sha256(canonical({'username': username, 'deviceLabel': label}).encode()).hexdigest()
        key = 'login:' + account['id'] + ':' + rid
        with self.state.transaction():
            current = self.state.get('mobile_accounts', 'current')
            if current != account: raise BackendError('AUTH_EXPIRED', 'Account changed; sign in again')
            cached = self._cached(key, binding)
            if cached: return cached
            if self.state.get('mobile_replays', key):
                raise BackendError('CONFLICT', 'Sign-in retry expired or changed; start a new sign-in')
            active = self._active_sessions()
            if len(active) >= self.max_sessions: raise BackendError('QUOTA_EXCEEDED', 'Device session limit reached; revoke an unused device')
            history=self.state.connection.execute("SELECT COUNT(*) FROM records WHERE namespace='mobile_sessions'").fetchone()[0]
            if history>=10000: raise BackendError('QUOTA_EXCEEDED','Device history requires private operator capacity review')
            sid = str(uuid.uuid4()); now = self.clock()
            # Existing domain principal, with an unexposed random backing credential.
            # Mobile API tokens never authenticate against the legacy owner surface.
            principal = self.auth._register('Mobile: ' + label.strip(), secrets.token_urlsafe(48), account['scopes'], account['roots'], 'agent')
            record = self.state.get('principals', principal.id); record['mobileSessionId'] = sid
            self.state.put('principals', principal.id, record)
            session = {'id': sid, 'accountId': account['id'], 'deviceLabel': label.strip(), 'principal': principal.as_dict(),
                       'created': now, 'lastSeen': now, 'idleExpires': now + IDLE_SECONDS,
                       'absoluteExpires': now + ABSOLUTE_SECONDS, 'revoked': False}
            response = self._tokens(session)
            self._cache(key, session, response, binding)
            self.state.audit(principal.id, 'mobile.login', 'completed', sid)
            return response

    def authenticate(self, token):
        record = self.state.get('mobile_tokens', token_digest(token))
        if not record or record['kind'] != 'access': raise BackendError('UNAUTHENTICATED', 'Sign in again')
        context = self._context(self.state.get('mobile_sessions', record['session']))
        if record['expires'] <= self.clock(): raise BackendError('AUTH_EXPIRED', 'Refresh this device session')
        return context

    def _active_sessions(self):
        now=self.clock()
        return [json.loads(r[0]) for r in self.state.connection.execute("SELECT body FROM records WHERE namespace='mobile_sessions' AND json_extract(body,'$.revoked')=0 AND json_extract(body,'$.idleExpires')>? AND json_extract(body,'$.absoluteExpires')>?",(now,now))]

    def issue_transfer(self,context,direction,resource,minimum,maximum,*,expires_at=None):
        """Finite resource/direction/byte-bound bearer; never a browsing credential."""
        if direction not in ('upload','download') or not 0<=minimum<=maximum:
            raise BackendError('INVALID_ARGUMENT','Invalid transfer authority')
        identifier(resource)
        with self.state.transaction() as c:
            session=self.state.get('mobile_sessions',context.session_id); self._context(session)
            if session['principal']['id']!=context.principal.id: raise BackendError('PERMISSION_DENIED','Device context mismatch')
            now=self.clock()
            count=c.execute("SELECT COUNT(*) FROM records WHERE namespace='mobile_transfer_tokens' AND json_extract(body,'$.session')=? AND json_extract(body,'$.expires')>?",(context.session_id,now)).fetchone()[0]
            if count>=256: raise BackendError('RATE_LIMITED','Device transfer credential budget reached; retain existing valid authority',True)
            token=secrets.token_urlsafe(48); expires=min(now+86400,session['idleExpires'],session['absoluteExpires'])
            if expires_at is not None: expires=min(expires,expires_at)
            self.state.put('mobile_transfer_tokens',token_digest(token),{'session':context.session_id,'direction':direction,
                'resource':resource,'minimum':str(minimum),'maximum':str(maximum),'expires':expires})
        return {'transferToken':token,'expiresAt':stamp(expires)}

    def authenticate_transfer(self,token,direction,resource,offset=0,length=0):
        record=self.state.get('mobile_transfer_tokens',token_digest(token))
        if not record or record['direction']!=direction or record['resource']!=resource:
            raise BackendError('PERMISSION_DENIED','Transfer authority does not match this resource')
        context=self._context(self.state.get('mobile_sessions',record['session']))
        if record['expires']<=self.clock(): raise BackendError('AUTH_EXPIRED','Renew transfer authority')
        if type(offset) is not int or type(length) is not int or length<0 or offset<int(record['minimum']) or offset+length>int(record['maximum']):
            raise BackendError('PERMISSION_DENIED','Byte range exceeds transfer authority')
        return context

    def refresh(self, args):
        object_fields(args, ('refreshToken', 'requestId'))
        rid = request_id(args['requestId']); digest = token_digest(args['refreshToken'])
        error = None
        with self.state.transaction():
            token = self.state.get('mobile_tokens', digest)
            if not token or token['kind'] != 'refresh': raise BackendError('UNAUTHENTICATED', 'Sign in again')
            session = self.state.get('mobile_sessions', token['session']); self._context(session)
            key = 'refresh:' + digest + ':' + rid
            if token.get('used'):
                cached = self._cached(key, digest)
                if cached: return cached
                self._revoke(session, 'refresh-reuse')
                error = BackendError('PERMISSION_REVOKED', 'Refresh credential reuse detected; sign in again')
            elif token['expires'] <= self.clock() or session['refreshDigest'] != digest:
                self._revoke(session, 'refresh-expired')
                error = BackendError('AUTH_EXPIRED', 'Sign in again')
            else:
                window=int(self.clock()//60)
                rate=self.state.get('mobile_refresh_rate',session['id'],{'window':window,'count':0})
                if rate['window']!=window: rate={'window':window,'count':0}
                if rate['count']>=8: raise BackendError('RATE_LIMITED','Wait before rotating this device session again',True)
                rate['count']+=1; self.state.put('mobile_refresh_rate',session['id'],rate)
                token['used'] = True
                self.state.put('mobile_tokens', digest, token)
                response = self._tokens(session)
                self._cache(key, session, response, digest)
                return response
        # Raise after committing revocation, never roll it back with the error.
        raise error

    def _revoke(self, session, reason):
        session['revoked'] = True
        self.state.put('mobile_sessions', session['id'], session)
        principal = self.state.get('principals', session['principal']['id'])
        principal['revoked'] = True; principal['scopeVersion'] += 1
        self.state.put('principals', principal['id'], principal)
        self.state.connection.execute("DELETE FROM records WHERE namespace='mobile_replays' AND json_extract(body,'$.session')=?", (session['id'],))
        self.state.audit(principal['id'], 'mobile.revoke', reason, session['id'])

    def logout(self, access_token):
        context = self.authenticate(access_token)
        with self.state.transaction():
            self._revoke(self.state.get('mobile_sessions', context.session_id), 'logout')
        return {'revoked': True}

    def revoke_device(self, owner, session_id):
        self.auth.owner(owner); identifier(session_id)
        with self.state.transaction():
            session = self.state.get('mobile_sessions', session_id)
            if not session: raise BackendError('NOT_FOUND', 'Device session not found')
            if not session['revoked']: self._revoke(session, 'owner-revocation')
        return {'revoked': True}

    def acquisition_principal(self, context):
        """Accepted work belongs to the account, not the phone's expiring session."""
        self.auth.authorize(context.principal, 'acquire')
        with self.state.transaction():
            account = self.state.get('mobile_accounts', 'current')
            if not account or account['id'] != context.account_id or account.get('revoked'):
                raise BackendError('PERMISSION_REVOKED', 'Mobile account unavailable')
            saved = account.get('acquisitionPrincipal')
            if saved:
                return self.auth.refresh(self.auth._principal(
                    self.state.get('principals', saved['id']), saved['credentialId']))
            actor = self.auth._register('Mobile acquisition account', secrets.token_urlsafe(48),
                account['scopes'], account['roots'], 'agent')
            record = self.state.get('principals', actor.id)
            record['mobileAcquisitionAccountId'] = account['id']
            self.state.put('principals', actor.id, record)
            account['acquisitionPrincipal'] = {'id':actor.id, 'credentialId':actor.credential_id}
            self.state.put('mobile_accounts', 'current', account)
            return actor

    def assign(self, owner, scopes, roots):
        self.auth.owner(owner); scopes, roots = self._assignment(scopes, roots)
        with self.state.transaction():
            account = self.state.get('mobile_accounts', 'current')
            if not account: raise BackendError('NOT_FOUND', 'Mobile account not enrolled')
            account.update(scopes=scopes, roots=roots, revision=account['revision'] + 1)
            if account.get('acquisitionPrincipal'):
                self.auth.update_scopes(owner, account['acquisitionPrincipal']['id'], scopes, roots)
            for session in self._active_sessions():
                if not session['revoked']:
                    self.auth.update_scopes(owner, session['principal']['id'], scopes, roots)
            self.state.put('mobile_accounts', 'current', account)
        return {'accountId': account['id'], 'revision': account['revision']}

    def recover_password(self, owner, password):
        self.auth.owner(owner); encoded = self.hasher.hash(self._password(password))
        with self.state.transaction():
            account = self.state.get('mobile_accounts', 'current')
            if not account: raise BackendError('NOT_FOUND', 'Mobile account not enrolled')
            for session in self._active_sessions():
                if not session['revoked']: self._revoke(session, 'account-recovery')
            account['passwordHash'] = encoded; account['revision'] += 1
            self.state.put('mobile_accounts', 'current', account)
        return {'accountId': account['id'], 'recovered': True}
