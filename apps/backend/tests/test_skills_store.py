import hashlib
import tempfile
import unittest
from pathlib import Path
from archive_backend.skills_store import SkillStore, manifest, metadata, LIMITS
from archive_backend.contracts import BackendError


def files(values):
    return [{'path': p, 'size': len(v), 'sha256': hashlib.sha256(v).hexdigest()} for p, v in values.items()]


class SkillsStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.store = SkillStore(self.tmp.name)
        self.values = {'SKILL.md': b'---\nname: Test skill\ndescription: A test\n---\n# Test\n', 'assets/a.bin': b'\x00\xff\x01', 'empty': b''}

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def begin(self, request='create', **kw):
        return self.store.begin('agent', dict(requestId=request, slug='test', files=files(self.values), **kw))

    def upload(self, session):
        for name, data in self.values.items():
            if data: self.store.chunk('agent', session['uploadId'], name, 0, data)

    def publish(self, session, request='publish'):
        return self.store.publish('agent', {'requestId': request, 'uploadId': session['uploadId']})

    def test_roundtrip_restart_and_retry(self):
        s = self.begin(); self.assertEqual(s, self.begin()); self.upload(s)
        self.store.close(); self.store = SkillStore(self.tmp.name)
        e = self.publish(s); self.assertEqual(e, self.publish(s)); self.assertEqual(e['name'], 'Test skill')
        self.assertEqual(self.store.read(e['id'], 'assets/a.bin')[0], self.values['assets/a.bin'])
        self.assertEqual(len(self.store.listing()['items']), 1)
        self.assertEqual(len(self.store.detail(e['id'])['files']), 3)

    def test_conflicts_and_isolation(self):
        s = self.begin()
        with self.assertRaises(BackendError): self.store.begin('agent', {'requestId': 'create', 'slug': 'other', 'files': files(self.values)})
        with self.assertRaises(BackendError): self.store.chunk('other', s['uploadId'], 'SKILL.md', 0, b'a')
        with self.assertRaises(BackendError): self.publish(s)
        self.upload(s); e = self.publish(s)
        a = self.begin('a', id=e['id'], expectedRevision=e['revision']); b = self.begin('b', id=e['id'], expectedRevision=e['revision'])
        self.upload(a); self.upload(b); self.publish(a, 'a')
        with self.assertRaises(BackendError): self.publish(b, 'b')

    def test_chunk_crash_offset_and_retry(self):
        s = self.begin(); ident = s['uploadId']; raw = self.values['SKILL.md']
        self.store.fault = lambda point: (_ for _ in ()).throw(RuntimeError('crash')) if point == 'chunk-before-ack' else None
        with self.assertRaises(RuntimeError): self.store.chunk('agent', ident, 'SKILL.md', 0, raw)
        self.store.close(); self.store = SkillStore(self.tmp.name)
        self.assertEqual(self.store.begin('agent', {'requestId': 'create', 'slug': 'test', 'files': files(self.values)})['offsets']['SKILL.md'], 0)
        self.store.chunk('agent', ident, 'SKILL.md', 0, raw)
        self.store.chunk('agent', ident, 'SKILL.md', 0, raw)
        with self.assertRaises(BackendError): self.store.chunk('agent', ident, 'SKILL.md', 0, b'wrong')

    def test_publish_crash_and_recovery(self):
        for boundary in ('publish-after-rename', 'publish-before-commit'):
            with self.subTest(boundary=boundary):
                s = self.begin(boundary); self.upload(s)
                self.store.fault = lambda point: (_ for _ in ()).throw(RuntimeError('crash')) if point == boundary else None
                with self.assertRaises(RuntimeError): self.publish(s, boundary)
                self.assertEqual(self.store.listing()['items'], [])
                self.store.close(); self.store = SkillStore(self.tmp.name)
                e = self.publish(s, boundary); self.assertEqual(self.store.read(e['id'])[0], self.values['SKILL.md'])
                # Independent storage for next fault boundary.
                self.store.close(); self.tmp.cleanup(); self.tmp = tempfile.TemporaryDirectory(); self.store = SkillStore(self.tmp.name)

    def test_manifest_attacks(self):
        for name in ('../escape', '/etc/passwd', 'a\\b', 'a/../b', '.archive-state/x', 'a\x00b'):
            with self.subTest(name=name), self.assertRaises(BackendError): manifest(files({'SKILL.md': b'', name: b''}))
        for names in ({'SKILL.md': b'', 'skill.md': b''}, {'SKILL.md': b'', 'a': b'', 'a/b': b''}):
            with self.assertRaises(BackendError): manifest(files(names))

    def test_corruption_symlinks_and_mount(self):
        s = self.begin(); self.upload(s); e = self.publish(s)
        with self.store.transaction(): directory = self.store.entry(e['id'])['versions'][0]['directory']
        p = Path(self.tmp.name, 'revisions', directory, 'files', 'SKILL.md'); p.write_bytes(b'corrupt')
        with self.assertRaises(BackendError): self.store.read(e['id'])
        p.unlink(); p.symlink_to('/etc/passwd')
        with self.assertRaises(BackendError): self.store.read(e['id'])
        self.store.check_mount = lambda: (_ for _ in ()).throw(BackendError('UNAVAILABLE', 'gone'))
        with self.assertRaises(BackendError): self.store.listing()

    def test_history_trash_and_retention(self):
        s = self.begin(); self.upload(s); e = self.publish(s)
        for n in range(6):
            s = self.begin('update'+str(n), id=e['id'], expectedRevision=e['revision']); self.upload(s); e = self.publish(s, 'update'+str(n))
        self.store.cleanup(); self.assertEqual(len(self.store.detail(e['id'])['revisions']), 5)
        e = self.store.trash('agent', {'requestId': 'trash', 'id': e['id'], 'expectedRevision': e['revision']})
        self.assertEqual(self.store.listing()['items'], [])
        e = self.store.trash('agent', {'requestId': 'restore', 'id': e['id'], 'expectedRevision': e['revision']}, True)
        self.assertFalse(e['trashed'])

    def test_expiry_and_cancel_cleanup(self):
        s = self.begin(); self.store.cancel('agent', s['uploadId']); self.store.cleanup()
        self.assertFalse(Path(self.tmp.name, 'staging', s['uploadId']).exists())
        s = self.begin('expired'); self.store.clock = lambda: 9999999999
        with self.assertRaises(BackendError): self.store.chunk('agent', s['uploadId'], 'SKILL.md', 0, b'a')
        self.store.cleanup(); self.assertFalse(Path(self.tmp.name, 'staging', s['uploadId']).exists())

    def test_metadata_is_inert(self):
        value = metadata('---\nname: !!python/object:danger\ndescription: >\n---\n', 'safe')
        self.assertEqual(value['name'], 'safe'); self.assertEqual(len(value['warnings']), 2)

    def test_quota_and_single_writer(self):
        old = LIMITS['libraryBytes']; LIMITS['libraryBytes'] = 1
        try:
            with self.assertRaises(BackendError): self.begin()
        finally: LIMITS['libraryBytes'] = old
        other = SkillStore(self.tmp.name)
        try:
            with self.assertRaises(BlockingIOError): other.listing()
        finally: other.close()

    def test_offline_backup_restore(self):
        import subprocess
        import sys
        s = self.begin(); self.upload(s); e = self.publish(s); self.store.close()
        with tempfile.TemporaryDirectory() as temp:
            dest = str(Path(temp)/'backup')
            result = subprocess.run([sys.executable, 'scripts/skills-admin.py', '--root', self.tmp.name, 'backup', '--destination', dest], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            restored = SkillStore(dest)
            try:
                self.assertEqual(restored.read(e['id'])[0], self.values['SKILL.md'])
                self.assertEqual(restored.publish('agent', {'requestId': 'publish', 'uploadId': s['uploadId']}), e)
            finally: restored.close()

    def test_disk_full_does_not_acknowledge_chunk(self):
        import errno
        s = self.begin()
        self.store.fault = lambda point: (_ for _ in ()).throw(OSError(errno.ENOSPC, 'test disk full')) if point == 'chunk-before-ack' else None
        with self.assertRaises(OSError): self.store.chunk('agent', s['uploadId'], 'SKILL.md', 0, self.values['SKILL.md'])
        self.store.fault = lambda _: None
        with self.store.transaction(): self.assertEqual(self.store.session('agent', s['uploadId'])['offsets']['SKILL.md'], 0)
        self.upload(s); self.assertEqual(self.store.read(self.publish(s)['id'])[0], self.values['SKILL.md'])

    def test_metadata_budget_rolls_back_unacknowledged_session(self):
        old = LIMITS['metadataBytes']; LIMITS['metadataBytes'] = 1
        try:
            with self.assertRaises(BackendError): self.begin()
            with self.store.transaction(): self.assertEqual(self.store.rows('upload'), [])
        finally: LIMITS['metadataBytes'] = old
        self.store.cleanup(); self.assertEqual(list(Path(self.tmp.name, 'staging').iterdir()), [])
