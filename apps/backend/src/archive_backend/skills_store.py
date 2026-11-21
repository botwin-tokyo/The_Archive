"""Bounded, durable skill packages. Uploaded content is always inert data."""
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import threading
import time
import unicodedata
import uuid
from .contracts import BackendError
from .filesystem import openat2, validate_path

LIMITS = {'documentBytes': 1048576, 'fileBytes': 16777216, 'packageBytes': 134217728,
          'chunkBytes': 1048576, 'files': 1000, 'depth': 16, 'libraryBytes': 10737418240,
          'metadataBytes': 67108864, 'activeUploads': 16, 'skills': 2000, 'retainedRevisions': 5, 'uploadDays': 7, 'trashDays': 30}


def fail(code, message):
    raise BackendError(code, message)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'))


def path(value):
    validate_path(value, allow_root=False)
    if len(value.split('/')) > LIMITS['depth'] or len(value.encode()) > 1024:
        fail('INVALID_ARGUMENT', 'File path exceeds library limits')
    return value


def manifest(files):
    if not isinstance(files, list) or not 1 <= len(files) <= LIMITS['files']:
        fail('INVALID_ARGUMENT', 'A bounded file manifest is required')
    seen = set(); total = 0; result = []
    for item in files:
        if not isinstance(item, dict) or set(item) != {'path', 'size', 'sha256'}:
            fail('INVALID_ARGUMENT', 'Invalid manifest fields')
        name = path(item['path']); key = unicodedata.normalize('NFC', name).casefold()
        if key in seen or type(item['size']) is not int or not 0 <= item['size'] <= LIMITS['fileBytes']:
            fail('INVALID_ARGUMENT', 'Duplicate file or invalid file size')
        if not isinstance(item['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', item['sha256']):
            fail('INVALID_ARGUMENT', 'SHA-256 digest required')
        seen.add(key); total += item['size']; result.append(dict(item))
    for key in seen:
        if any('/'.join(key.split('/')[:i]) in seen for i in range(1, len(key.split('/')))):
            fail('INVALID_ARGUMENT', 'File and directory paths collide')
    doc = next((f for f in result if f['path'] == 'SKILL.md'), None)
    if not doc or doc['size'] > LIMITS['documentBytes'] or total > LIMITS['packageBytes']:
        fail('INVALID_ARGUMENT', 'Root SKILL.md is required within package limits')
    return sorted(result, key=lambda f: f['path'])


def metadata(text, slug):
    """Deliberately limited scalar frontmatter; no YAML evaluation or templates."""
    result = {'name': slug, 'description': '', 'warnings': []}
    if not text.startswith('---\n') and not text.startswith('---\r\n'):
        result['warnings'].append('No YAML frontmatter; using the package slug as its name.')
        return result
    lines = text.splitlines(); end = next((i for i, line in enumerate(lines[1:201], 1) if line == '---'), None)
    if end is None or sum(len(s) for s in lines[:end]) > 16384:
        result['warnings'].append('Frontmatter is missing its closing delimiter or exceeds the metadata limit.')
        return result
    for line in lines[1:end]:
        match = re.fullmatch(r'(name|description):\s*(.*)', line)
        if not match:
            continue
        key, value = match.groups()
        if not value or value[0] in '|>&*!{[' or len(value.encode()) > (512 if key == 'name' else 4096):
            result['warnings'].append('Unsupported or oversized '+key+' frontmatter; using the fallback.')
            continue
        if value.startswith('"'):
            try: value = json.loads(value)
            except (ValueError, TypeError): value = None
        elif value.startswith("'"):
            value = value[1:-1].replace("''", "'") if value.endswith("'") else None
        if not isinstance(value, str) or not value.strip():
            result['warnings'].append('Invalid '+key+' frontmatter; using the fallback.')
        else:
            result[key] = value
    return result


class SkillStore:
    def __init__(self, root, check=lambda: None, *, fault=lambda point: None, clock=time.time):
        self.root = root; self.check_mount = check; self.fault = fault; self.clock = clock
        self.lock = threading.RLock(); self.fd = None; self.db = None; self.lockfd = None

    def close(self):
        with self.lock:
            if self.db: self.db.close(); self.db = None
            if self.lockfd is not None: os.close(self.lockfd); self.lockfd = None
            if self.fd is not None: os.close(self.fd); self.fd = None

    def ready(self):
        self.check_mount()
        if self.fd is not None:
            info = os.stat(self.root, follow_symlinks=False)
            if (info.st_dev, info.st_ino) != (os.fstat(self.fd).st_dev, os.fstat(self.fd).st_ino):
                fail('UNAVAILABLE', 'Library directory identity changed; restart after checking the drive')
            return
        parent = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try: self.fd = openat2(parent, self.root.lstrip('/'), os.O_RDONLY | os.O_DIRECTORY, resolve=0x04 | 0x08)
        finally: os.close(parent)
        try:
            self.lockfd = openat2(self.fd, 'writer.lock', os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(self.lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for area in ('staging', 'revisions'):
                try: os.mkdir(area, 0o700, dir_fd=self.fd)
                except FileExistsError: pass
                fd = openat2(self.fd, area, os.O_RDONLY | os.O_DIRECTORY); os.close(fd)
            for name in ('catalog.sqlite3', 'catalog.sqlite3-journal', 'catalog.sqlite3-wal', 'catalog.sqlite3-shm'):
                try:
                    info = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1: fail('UNAVAILABLE', 'Invalid library catalog file')
                except FileNotFoundError: pass
            self.db = sqlite3.connect(f'/proc/self/fd/{self.fd}/catalog.sqlite3', check_same_thread=False)
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA journal_mode=DELETE')
            self.db.execute('CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, value TEXT, PRIMARY KEY(kind,id))')
            self.db.commit(); os.fsync(self.fd)
        except Exception:
            self.close(); raise

    @contextlib.contextmanager
    def transaction(self):
        with self.lock:
            self.ready()
            with self.db: yield

    def get(self, kind, ident):
        row = self.db.execute('SELECT value FROM records WHERE kind=? AND id=?', (kind, ident)).fetchone()
        return json.loads(row[0]) if row else None

    def rows(self, kind):
        return [json.loads(row[0]) for row in self.db.execute('SELECT value FROM records WHERE kind=? ORDER BY id', (kind,))]

    def save(self, kind, ident, value):
        self.db.execute('INSERT OR REPLACE INTO records VALUES(?,?,?)', (kind, ident, canonical(value)))
        allocated = self.db.execute('PRAGMA page_count').fetchone()[0] * self.db.execute('PRAGMA page_size').fetchone()[0]
        if allocated > LIMITS['metadataBytes']: fail('QUOTA_EXCEEDED', 'Library metadata storage limit reached')

    def receipt(self, actor, op, args):
        key = hashlib.sha256((actor+'\0'+op+'\0'+args['requestId']).encode()).hexdigest()
        fingerprint = hashlib.sha256(canonical(args).encode()).hexdigest()
        prior = self.get('receipt', key)
        if not prior and self.db.execute("SELECT COUNT(*) FROM records").fetchone()[0] >= 100000:
            fail('QUOTA_EXCEEDED', 'Library metadata budget reached; archive old receipts before continuing')
        if prior and prior['fingerprint'] != fingerprint: fail('CONFLICT', 'Request ID already used with different arguments')
        return key, fingerprint, prior

    def remember(self, key, fingerprint, result):
        self.save('receipt', key, {'fingerprint': fingerprint, 'result': result})
        return result

    def usage(self):
        entries = self.rows('skill'); sessions = self.rows('upload')
        return sum(sum(f['size'] for f in v['files']) for e in entries for v in e['versions']) + sum(sum(f['size'] for f in s['files']) for s in sessions if s['state'] == 'uploading')

    def entry(self, ident):
        value = self.get('skill', ident)
        if value is None: fail('NOT_FOUND', 'Skill not found')
        return value

    @staticmethod
    def summary(entry):
        return {k: entry[k] for k in ('id', 'slug', 'name', 'description', 'revision', 'createdAt', 'updatedAt', 'trashed', 'warnings')}

    def session(self, actor, ident):
        value = self.get('upload', ident)
        if not value or value['actor'] != actor: fail('NOT_FOUND', 'Upload not found')
        if value['state'] == 'uploading' and self.clock() - value['updatedAt'] > LIMITS['uploadDays'] * 86400:
            fail('CONFLICT', 'Upload expired; start a new session')
        return value

    @staticmethod
    def progress(session):
        return {'uploadId': session['id'], 'state': session['state'], 'offsets': session['offsets'], 'expiresAt': session['updatedAt'] + LIMITS['uploadDays'] * 86400}

    def _mkdir(self, relative):
        parts = relative.split('/'); fd = os.dup(self.fd)
        try:
            for part in parts:
                try: os.mkdir(part, 0o700, dir_fd=fd); os.fsync(fd)
                except FileExistsError: pass
                child = openat2(fd, part, os.O_RDONLY | os.O_DIRECTORY); os.close(fd); fd = child
        finally: os.close(fd)

    def begin(self, actor, args):
        with self.transaction():
            key, fingerprint, prior = self.receipt(actor, 'begin', args)
            if prior: return prior['result']
            files = manifest(args['files']); slug = args['slug']
            if not re.fullmatch('[a-z0-9][a-z0-9_-]{0,79}', slug): fail('INVALID_ARGUMENT', 'Use a lowercase slug of at most 80 letters, digits, hyphens or underscores')
            existing = self.get('skill', args.get('id', ''))
            if args.get('id') and not existing: fail('NOT_FOUND', 'Skill not found')
            if existing:
                if existing['trashed'] or existing['revision'] != args.get('expectedRevision') or existing['slug'] != slug:
                    fail('CONFLICT', 'Skill changed, is trashed, or slug does not match')
            elif args.get('expectedRevision', 0) != 0 or any(e['slug'] == slug for e in self.rows('skill')):
                fail('CONFLICT', 'Slug already exists or creation revision is not zero')
            if len(self.rows('skill')) >= LIMITS['skills'] and not existing: fail('QUOTA_EXCEEDED', 'Library skill limit reached')
            if sum(s['state'] == 'uploading' for s in self.rows('upload')) >= LIMITS['activeUploads']: fail('RESOURCE_EXHAUSTED', 'Too many active uploads')
            size = sum(f['size'] for f in files)
            if self.usage() + size > LIMITS['libraryBytes'] or os.fstatvfs(self.fd).f_bavail * os.fstatvfs(self.fd).f_frsize < size + 67108864:
                fail('QUOTA_EXCEEDED', 'Insufficient library quota or drive space')
            ident = uuid.uuid4().hex; now = self.clock()
            s = {'id': ident, 'actor': actor, 'skillId': existing['id'] if existing else uuid.uuid4().hex, 'slug': slug, 'expectedRevision': args.get('expectedRevision', 0), 'files': files, 'offsets': {f['path']: 0 for f in files}, 'updatedAt': now, 'state': 'uploading'}
            for f in files:
                relative = 'staging/'+ident+'/files/'+f['path']; self._mkdir(relative.rsplit('/', 1)[0])
                fd = openat2(self.fd, relative, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try: os.fsync(fd)
                finally: os.close(fd)
            for folder, _, _ in os.walk(f'/proc/self/fd/{self.fd}/staging/'+ident, topdown=False):
                directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try: os.fsync(directory)
                finally: os.close(directory)
            self.save('upload', ident, s)
            self.fault('begin-before-commit')
            return self.remember(key, fingerprint, self.progress(s))

    def chunk(self, actor, ident, name, offset, data):
        path(name)
        if type(offset) is not int or offset < 0 or not isinstance(data, bytes) or not 1 <= len(data) <= LIMITS['chunkBytes']:
            fail('INVALID_ARGUMENT', 'Invalid chunk size or offset')
        with self.transaction():
            s = self.session(actor, ident)
            if s['state'] != 'uploading': fail('CONFLICT', 'Upload is no longer writable')
            f = next((f for f in s['files'] if f['path'] == name), None)
            if not f or offset + len(data) > f['size']: fail('INVALID_ARGUMENT', 'Chunk exceeds declared file size')
            ack = s['offsets'][name]
            fd = openat2(self.fd, 'staging/'+ident+'/files/'+name, os.O_RDWR)
            try:
                if offset < ack:
                    if offset + len(data) > ack or os.pread(fd, len(data), offset) != data: fail('CONFLICT', 'Retry bytes differ from acknowledged bytes')
                    return self.progress(s)
                if offset != ack: fail('CONFLICT', 'Resume from the acknowledged offset')
                os.ftruncate(fd, ack)
                view = memoryview(data)
                while view:
                    written = os.pwrite(fd, view, offset); offset += written; view = view[written:]
                os.fsync(fd); self.fault('chunk-before-ack')
            finally: os.close(fd)
            s['offsets'][name] = offset; s['updatedAt'] = self.clock(); self.save('upload', ident, s)
            return self.progress(s)

    def _read(self, relative, maximum):
        fd = openat2(self.fd, relative, os.O_RDONLY | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum: fail('INTEGRITY_ERROR', 'Invalid library file')
            with os.fdopen(os.dup(fd), 'rb') as stream: return stream.read(maximum + 1)
        finally: os.close(fd)

    def publish(self, actor, args):
        with self.transaction():
            key, fingerprint, prior = self.receipt(actor, 'publish', args)
            if prior: return prior['result']
            s = self.session(actor, args['uploadId'])
            if s['state'] != 'uploading': fail('CONFLICT', 'Upload is no longer publishable')
            old = self.get('skill', s['skillId'])
            if (old and (old['trashed'] or old['revision'] != s['expectedRevision'])) or (not old and any(e['slug'] == s['slug'] for e in self.rows('skill'))):
                fail('CONFLICT', 'Skill changed while uploading; start an update from its latest revision')
            if any(s['offsets'][f['path']] != f['size'] for f in s['files']): fail('CONFLICT', 'Upload is incomplete')
            target = 'revisions/'+s['id']; staging = 'staging/'+s['id']
            # A crash may leave a fully flushed but not catalog-published revision.
            try: fd = openat2(self.fd, target, os.O_RDONLY | os.O_DIRECTORY); os.close(fd); base = target
            except BackendError as exc:
                if exc.code != 'NOT_FOUND': raise
                base = staging
            text = None
            for f in s['files']:
                data = self._read(base+'/files/'+f['path'], LIMITS['fileBytes'])
                if len(data) != f['size'] or hashlib.sha256(data).hexdigest() != f['sha256']: fail('INTEGRITY_ERROR', 'File does not match its manifest')
                if f['path'] == 'SKILL.md':
                    try: text = data.decode('utf-8')
                    except UnicodeError: fail('INVALID_ARGUMENT', 'SKILL.md must be valid UTF-8')
            meta = metadata(text, s['slug']); now = self.clock(); revision = (old['revision'] if old else 0) + 1
            version = {'revision': revision, 'directory': s['id'], 'files': s['files'], 'createdAt': now}
            entry = dict(old or {'id': s['skillId'], 'slug': s['slug'], 'createdAt': now, 'versions': []}, **meta, revision=revision, updatedAt=now, trashed=False)
            entry['versions'] = entry['versions'] + [version]
            if base == staging:
                raw = canonical({'skillId': entry['id'], 'slug': s['slug'], **version, **meta}).encode()
                fd = openat2(self.fd, staging+'/manifest.json', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    with os.fdopen(os.dup(fd), 'wb') as stream: stream.write(raw); stream.flush(); os.fsync(stream.fileno())
                finally: os.close(fd)
                # Flush all nested directory entries before making the revision durable.
                for folder, _, _ in os.walk(f'/proc/self/fd/{self.fd}/'+staging, topdown=False):
                    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try: os.fsync(fd)
                    finally: os.close(fd)
                os.rename(staging, target, src_dir_fd=self.fd, dst_dir_fd=self.fd)
                for area in ('staging', 'revisions'):
                    fd = openat2(self.fd, area, os.O_RDONLY | os.O_DIRECTORY)
                    try: os.fsync(fd)
                    finally: os.close(fd)
            self.fault('publish-after-rename')
            s['state'] = 'published'; s['updatedAt'] = now; self.save('upload', s['id'], s)
            self.save('skill', entry['id'], entry)
            result = self.remember(key, fingerprint, self.summary(entry))
            self.fault('publish-before-commit')
            return result

    def listing(self, query='', cursor='', limit=50, trashed=False):
        with self.transaction():
            items = [e for e in self.rows('skill') if e['trashed'] == trashed and query.casefold() in (e['slug']+' '+e['name']+' '+e['description']).casefold() and e['id'] > cursor]
            items.sort(key=lambda e: e['id']); page = items[:limit]
            return {'items': [self.summary(e) for e in page], 'nextCursor': page[-1]['id'] if len(items) > limit else None}

    def detail(self, ident, revision=None):
        with self.transaction():
            entry = self.entry(ident)
            version = next((v for v in entry['versions'] if v['revision'] == revision), None) if revision is not None else entry['versions'][-1]
            if version is None: fail('NOT_FOUND', 'Content revision is no longer retained')
            return {**self.summary(entry), 'contentRevision': version['revision'], 'files': version['files'], 'revisions': [v['revision'] for v in entry['versions']]}

    def read(self, ident, name='SKILL.md', revision=None):
        path(name)
        with self.transaction():
            entry = self.entry(ident)
            version = next((v for v in entry['versions'] if v['revision'] == revision), None) if revision is not None else entry['versions'][-1]
            if not version: fail('NOT_FOUND', 'Content revision is no longer retained')
            item = next((f for f in version['files'] if f['path'] == name), None)
            if not item: fail('NOT_FOUND', 'File not found')
            raw = self._read('revisions/'+version['directory']+'/files/'+name, LIMITS['fileBytes'])
            if len(raw) != item['size'] or hashlib.sha256(raw).hexdigest() != item['sha256']: fail('INTEGRITY_ERROR', 'File integrity check failed')
            return raw, version['revision']

    def trash(self, actor, args, restore=False):
        with self.transaction():
            key, fingerprint, prior = self.receipt(actor, 'restore' if restore else 'trash', args)
            if prior: return prior['result']
            e = self.entry(args['id'])
            if e['revision'] != args['expectedRevision']: fail('CONFLICT', 'Skill changed; refresh first')
            e.update(trashed=not restore, revision=e['revision']+1, updatedAt=self.clock()); self.save('skill', e['id'], e)
            return self.remember(key, fingerprint, self.summary(e))

    def cancel(self, actor, ident):
        with self.transaction():
            s = self.session(actor, ident)
            if s['state'] == 'published': fail('CONFLICT', 'Published upload cannot be cancelled')
            s['state'] = 'cancelled'; self.save('upload', ident, s)
            return self.progress(s)

    def cleanup(self):
        """Commit retained references first; then delete unreferenced directories safely."""
        with self.transaction():
            now = self.clock()
            for e in self.rows('skill'):
                if e['trashed'] and now-e['updatedAt'] > LIMITS['trashDays']*86400:
                    self.db.execute('DELETE FROM records WHERE kind=? AND id=?', ('skill', e['id']))
                elif len(e['versions']) > LIMITS['retainedRevisions']:
                    e['versions'] = e['versions'][-LIMITS['retainedRevisions']:]; self.save('skill', e['id'], e)
            for s in self.rows('upload'):
                if now-s['updatedAt'] > LIMITS['uploadDays']*86400 and s['state'] == 'uploading':
                    s['state'] = 'expired'; self.save('upload', s['id'], s)
        with self.transaction():
            active = {s['id'] for s in self.rows('upload') if s['state'] == 'uploading'}
            retained = {v['directory'] for e in self.rows('skill') for v in e['versions']} | active
            for area, keep in (('staging', active), ('revisions', retained)):
                fd = openat2(self.fd, area, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    for name in os.listdir(fd):
                        if name not in keep and re.fullmatch('[0-9a-f]{32}', name): shutil.rmtree(name, dir_fd=fd)
                    os.fsync(fd)
                finally: os.close(fd)
