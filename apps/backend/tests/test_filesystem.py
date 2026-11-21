import ctypes
import errno
import os
from pathlib import Path
import socket
import stat as statmod
import tempfile
import unittest
from unittest.mock import patch

from archive_backend.contracts import BackendError
from archive_backend.filesystem import (ConfinedFilesystem, FilesystemError, INTERNAL_ROOT,
    CONFINED_RESOLVE, RESOLVE_BENEATH, RESOLVE_NO_SYMLINKS, RESOLVE_NO_XDEV,
    openat2, validate_name, validate_path)
from archive_backend.volumes import VolumeRegistry


class ConfinedFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='archive-fs-test-')
        self.root = Path(self.temp.name)
        self.registry = VolumeRegistry.fixtures({'files': self.root})
        self.fs = ConfinedFilesystem(self.registry.get('files'))
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.registry.close)

    def assert_code(self, code, action):
        with self.assertRaises(BackendError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def put(self, path='file', data=b'opaque\x00bytes'):
        with self.fs.create_staging('fixture') as target:
            target.write(data)
        return self.fs.commit_staging('fixture', path)

    def test_unicode_bytes_and_independent_file_identity(self):
        self.fs.create_dir('音楽')
        entry = self.put('音楽/café.bin')
        self.assertEqual(entry.kind, 'file')
        self.assertLess(entry.revision, 2**53)
        self.assertEqual(entry.revision, self.fs.stat(entry.path).revision)
        with self.fs.open_read(entry.path, entry.revision) as source:
            self.assertEqual(source.read(), b'opaque\x00bytes')
        self.assertEqual([e.name for e in self.fs.iter_entries()], ['音楽'])
        self.assertEqual(entry.nlink, 1)
        self.assertEqual(entry.mode, 0o600)

    def test_path_validation_never_normalizes(self):
        for value in ('../outside', '/etc/passwd', 'one/../two', 'a//b', 'a/./b',
                      'a/', 'a\\b', 'a\x00b', '\ud800', '/','a/'*64+'b', 'a'*256):
            with self.subTest(value=repr(value)):
                self.assert_code('INVALID_PATH', lambda: self.fs.stat(value))
        for value in ('', '.', '..', 'one/two', 'one\\two'):
            self.assert_code('INVALID_PATH', lambda: validate_name(value))
        self.assertEqual(validate_path('e\u0301'), 'e\u0301')
        self.assertEqual(validate_path('é'), 'é')
        self.assertNotEqual(validate_path('e\u0301'), validate_path('é'))

    def test_root_cannot_be_mutated(self):
        self.assert_code('INVALID_PATH', lambda: self.fs.create_dir(''))
        self.assert_code('INVALID_PATH', lambda: self.fs.rename_noreplace('', 'other'))
        self.assert_code('INVALID_PATH', lambda: self.fs.unlink('', directory=True))

    def test_protected_internal_paths_hidden_and_denied(self):
        with self.fs.create_staging('abc') as target:
            target.write(b'secret partial')
        for path in (INTERNAL_ROOT, INTERNAL_ROOT+'/staging/abc', '.ARCHIVE-console',
                     'folder/.archive-state', '.archive-unrecognized'):
            self.assert_code('INVALID_PATH', lambda: self.fs.stat(path))
            self.assert_code('INVALID_PATH', lambda: self.fs.create_dir(path))
        self.assertEqual(self.fs.list(), [])
        self.assertEqual((self.root/INTERNAL_ROOT).stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.root/INTERNAL_ROOT/'staging').stat().st_mode & 0o777, 0o700)

    def test_symlinks_display_without_following(self):
        self.put()
        os.symlink('file', self.root/'link')
        self.assertEqual(self.fs.stat('link').kind, 'symlink')
        self.assertIn('symlink', {e.kind for e in self.fs.list()})
        self.assert_code('INVALID_PATH', lambda: self.fs.open_read('link'))
        self.assert_code('UNSUPPORTED_ENTRY', lambda: self.fs.rename_noreplace('link','renamed'))

    def test_parent_symlink_cannot_escape_or_mutate_outside(self):
        with tempfile.TemporaryDirectory() as outside:
            sentinel = Path(outside)/'sentinel'
            sentinel.write_bytes(b'unchanged')
            os.symlink(outside, self.root/'escape')
            for action in (lambda: self.fs.open_read('escape/sentinel'),
                           lambda: self.fs.create_dir('escape/new'),
                           lambda: self.fs.rename_noreplace('escape/sentinel','stolen'),
                           lambda: self.fs.unlink('escape/sentinel')):
                self.assert_code('INVALID_PATH', action)
            self.assertEqual(sentinel.read_bytes(), b'unchanged')
            self.assertEqual(set(os.listdir(outside)), {'sentinel'})

    def test_symlink_swap_after_open_is_detected_before_mkdir(self):
        self.fs.create_dir('parent')
        original = self.fs._assert_parent
        swapped = False
        with tempfile.TemporaryDirectory() as outside:
            def swap(path, fd, **kwargs):
                nonlocal swapped
                if path == 'parent' and not swapped:
                    swapped = True
                    os.rename(self.root/'parent', self.root/'old-parent')
                    os.symlink(outside, self.root/'parent')
                return original(path, fd, **kwargs)
            with patch.object(self.fs, '_assert_parent', side_effect=swap):
                self.assert_code('INVALID_PATH', lambda: self.fs.create_dir('parent/new'))
            self.assertEqual(os.listdir(outside), [])
            self.assertEqual(os.listdir(self.root/'old-parent'), [])

    def test_parent_move_and_replacement_detected(self):
        self.fs.create_dir('parent')
        original = self.fs._assert_parent
        replaced = False
        def replace(path, fd, **kwargs):
            nonlocal replaced
            if path == 'parent' and not replaced:
                replaced = True
                os.rename(self.root/'parent', self.root/'old-parent')
                os.mkdir(self.root/'parent')
            return original(path, fd, **kwargs)
        with patch.object(self.fs, '_assert_parent', side_effect=replace):
            self.assert_code('STALE_REVISION', lambda: self.fs.create_dir('parent/new'))
        self.assertFalse((self.root/'parent'/'new').exists())
        self.assertFalse((self.root/'old-parent'/'new').exists())

    def test_hardlink_alias_rejected_and_never_mutated(self):
        with tempfile.TemporaryDirectory() as outside:
            alias = Path(outside)/'private'
            alias.write_bytes(b'outside bytes')
            os.link(alias, self.root/'alias')
            for action in (lambda: self.fs.stat('alias'), lambda: self.fs.open_read('alias'),
                           lambda: self.fs.rename_noreplace('alias','moved'),lambda: self.fs.unlink('alias')):
                self.assert_code('UNSUPPORTED_ENTRY', action)
            self.assertEqual(alias.read_bytes(), b'outside bytes')
            self.assertEqual(alias.stat().st_nlink, 2)

    def test_hardlink_created_after_open_invalidates_read(self):
        self.put()
        with self.fs.open_read('file') as source:
            os.link(self.root/'file', self.root/'alias')
            self.assert_code('UNSUPPORTED_ENTRY', lambda: source.read())

    def test_fifo_and_socket_denied_without_blocking(self):
        os.mkfifo(self.root/'pipe')
        self.assert_code('UNSUPPORTED_ENTRY', lambda: self.fs.stat('pipe'))
        self.assert_code('UNSUPPORTED_ENTRY', lambda: self.fs.open_read('pipe'))
    def test_unix_socket_denied_when_executor_supports_socket_creation(self):
        try:
            sock = socket.socket(socket.AF_UNIX)
        except PermissionError:
            self.skipTest('Executor disallows socket creation; FIFO special-file gate still exercised')
        with sock:
            sock.bind(str(self.root/'socket'))
            self.assert_code('UNSUPPORTED_ENTRY', lambda: self.fs.stat('socket'))

    def test_no_replace_and_stale_revision(self):
        original = self.put('source', b'one')
        destination = self.put('target', b'two')
        self.assert_code('CONFLICT', lambda: self.fs.rename_noreplace('source','target', original.revision))
        self.assertEqual((self.root/'target').read_bytes(), b'two')
        self.assertEqual(self.fs.stat('target').inode, destination.inode)
        (self.root/'source').write_bytes(b'changed')
        self.assert_code('STALE_REVISION', lambda: self.fs.rename_noreplace('source','renamed', original.revision))
        fresh = self.fs.stat('source')
        renamed = self.fs.rename_noreplace('source','renamed', fresh.revision)
        self.assertEqual(renamed.inode, original.inode)
        self.assert_code('NOT_FOUND', lambda: self.fs.stat('source'))

    def test_publication_collision_retains_partial(self):
        self.put('target', b'first')
        with self.fs.create_staging('second') as output:
            output.write(b'second')
        self.assert_code('CONFLICT', lambda: self.fs.commit_staging('second','target'))
        self.assertEqual((self.root/'target').read_bytes(), b'first')
        with self.fs.read_internal('staging','second') as retained:
            self.assertEqual(retained.read(), b'second')

    def test_revision_changes_mid_read(self):
        original = self.put(data=b'first')
        with self.fs.open_read('file') as source:
            (self.root/'file').write_bytes(b'second')
            self.assert_code('STALE_REVISION', lambda: source.read())
        self.assert_code('STALE_REVISION', lambda: self.fs.open_read('file', original.revision))

    def test_recursive_staging_trash_restore_and_authorized_purge_primitives(self):
        self.fs.create_internal_dir('staging', 'tree')
        self.fs.create_internal_dir('staging', 'tree', 'nested')
        with self.fs.create_internal_file('staging','tree','nested/opaque') as file:
            file.write(b'data')
        committed = self.fs.commit_internal('staging', 'tree', 'folder')
        self.assertTrue(committed.is_dir)
        self.fs.move_to_internal('folder', 'trash', 'trash-id', expected_revision=committed.revision)
        self.assert_code('NOT_FOUND', lambda: self.fs.stat('folder'))
        self.assertEqual([e.name for e in self.fs.iter_internal('trash','trash-id')], ['nested'])
        with self.fs.read_internal('trash','trash-id','nested/opaque') as file:
            self.assertEqual(file.read(), b'data')
        restored = self.fs.restore_internal('trash', 'trash-id', 'restored')
        self.fs.move_to_internal('restored', 'trash', 'trash-id', expected_revision=restored.revision)
        self.fs.remove_internal('trash', 'trash-id')
        self.assert_code('NOT_FOUND', lambda: self.fs.internal_stat('trash','trash-id'))

    def test_internal_path_tokens_and_unsafe_permissions(self):
        for token in ('', '../escape', 'a/b', 'é', 'a'*129):
            self.assert_code('INVALID_PATH', lambda: self.fs.create_staging(token))
        (self.root/INTERNAL_ROOT).mkdir(mode=0o755)
        self.assert_code('PERMISSION_DENIED', lambda: self.fs.create_staging('safe'))
        self.assertFalse((self.root/INTERNAL_ROOT/'staging').exists())

    def test_internal_symlink_never_followed(self):
        with tempfile.TemporaryDirectory() as outside:
            os.symlink(outside, self.root/INTERNAL_ROOT)
            self.assert_code('INVALID_PATH', lambda: self.fs.create_staging('safe'))
            self.assertEqual(os.listdir(outside), [])

    def test_read_only_volume_allows_read_but_denies_mutation(self):
        self.put()
        capacity = self.fs.volume.capacity()
        with patch.object(self.fs.volume, 'capacity', return_value={**capacity,'read_only':True}):
            with self.fs.open_read('file') as file:
                self.assertTrue(file.read())
            self.assert_code('READ_ONLY', lambda: self.fs.create_dir('new'))
            self.assert_code('READ_ONLY', lambda: self.fs.create_staging('new'))

    def test_explicit_directory_unlink_and_bounded_listing(self):
        self.fs.create_dir('one')
        self.fs.create_dir('two')
        self.assert_code('TOO_MANY_ENTRIES', lambda: self.fs.list(max_entries=1))
        self.assert_code('UNSUPPORTED_ENTRY', lambda: self.fs.unlink('one'))
        self.fs.unlink('one', directory=True)
        self.assertEqual([e.name for e in self.fs.list()], ['two'])

    def test_closed_volume_invalidates_open_handle_without_extra_writes(self):
        with self.fs.create_staging('active') as file:
            file.write(b'first')
            self.registry.close()
            self.assert_code('DRIVE_UNAVAILABLE', lambda: file.write(b'second'))
        self.assertEqual((self.root/INTERNAL_ROOT/'staging'/'active').read_bytes(), b'first')

    def test_special_file_check_precedes_any_data_open(self):
        os.mkfifo(self.root/'pipe')
        original = os.open
        data_opens = []
        def tracked_open(path, *args, **kwargs):
            if str(path).startswith('/proc/self/fd/'):
                data_opens.append(path)
            return original(path, *args, **kwargs)
        with patch('archive_backend.filesystem.os.open', side_effect=tracked_open):
            self.assert_code('UNSUPPORTED_ENTRY', lambda: self.fs.open_read('pipe'))
        self.assertEqual(data_opens, [])

    def test_device_and_socket_metadata_types_are_rejected(self):
        from archive_backend.filesystem import _kind
        for kind in (statmod.S_IFCHR, statmod.S_IFBLK, statmod.S_IFSOCK, statmod.S_IFIFO):
            info = os.stat_result((kind | 0o600, 1, 1, 1, 1, 1, 0, 0, 0, 0))
            self.assert_code('UNSUPPORTED_ENTRY', lambda: _kind(info))

    def test_replacement_after_inode_pinning_never_opens_new_target(self):
        self.put(data=b'original')
        original = os.open
        replaced = False
        def tracked_open(path, *args, **kwargs):
            nonlocal replaced
            if str(path).startswith('/proc/self/fd/') and not replaced:
                replaced = True
                os.rename(self.root/'file', self.root/'pinned-old')
                os.mkfifo(self.root/'file')
            return original(path, *args, **kwargs)
        with patch('archive_backend.filesystem.os.open', side_effect=tracked_open):
            self.assert_code('UNSUPPORTED_ENTRY', lambda: self.fs.open_read('file'))
        self.assertEqual((self.root/'pinned-old').read_bytes(), b'original')

    def test_short_write_and_enospc_are_not_falsely_acknowledged(self):
        with self.fs.create_staging('short') as file:
            with patch.object(file._file, 'write', return_value=2):
                self.assertEqual(file.write(b'longer'), 2)
            with patch.object(file._file, 'write', side_effect=OSError(errno.ENOSPC, 'full')):
                self.assert_code('INSUFFICIENT_SPACE', lambda: file.write(b'bytes'))
        self.assertEqual((self.root/INTERNAL_ROOT/'staging'/'short').read_bytes(), b'')

    def test_internal_reopen_no_create_and_safe_partial_rewrite(self):
        self.fs.create_internal_dir('staging','tree')
        with self.fs.create_internal_file('staging','tree','child') as file:
            file.write(b'partial')
            file.flush()
        self.assert_code('NOT_FOUND',lambda:self.fs.open_internal_file('staging','tree','missing'))
        with self.fs.open_internal_file('staging','tree','child') as file:
            self.assertEqual(file.read(),b'partial')
            file.truncate(0)
            file.seek(0)
            file.write(b'complete')
            file.flush()
        with self.fs.open_internal_file('staging','tree','child',writable=False) as file:
            self.assertEqual(file.read(),b'complete')
            self.assertFalse(file._writable)

    def test_staged_flush_fsyncs_file_before_success(self):
        with self.fs.create_staging('durable') as file:
            file.write(b'bytes')
            with patch('archive_backend.filesystem.os.fsync', wraps=os.fsync) as sync:
                file.flush()
            sync.assert_called_once_with(file.fileno())
            with patch('archive_backend.filesystem.os.fsync', side_effect=OSError(errno.ENOSPC, 'full')):
                self.assert_code('INSUFFICIENT_SPACE', file.flush)

    def test_flush_failure_does_not_publish_staging(self):
        with self.fs.create_staging('durable') as file:
            file.write(b'partial')
        with patch('archive_backend.filesystem.os.fsync', side_effect=OSError(errno.EIO, 'fault')):
            with self.assertRaises(OSError):
                self.fs.commit_staging('durable','published')
        self.assertFalse((self.root/'published').exists())
        self.assertEqual((self.root/INTERNAL_ROOT/'staging'/'durable').read_bytes(), b'partial')

    def test_actual_kernel_rejects_nested_proc_mount(self):
        root = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            self.assert_code('INVALID_PATH', lambda: openat2(root,'proc',os.O_RDONLY|os.O_DIRECTORY))
        finally:
            os.close(root)

    def test_openat2_resolution_flags_and_unsupported_kernel_fail_closed(self):
        self.assertEqual(CONFINED_RESOLVE, RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_XDEV)
        def unavailable(*_):
            ctypes.set_errno(errno.ENOSYS)
            return -1
        with patch('archive_backend.filesystem._LIBC.syscall', side_effect=unavailable):
            self.assert_code('UNSUPPORTED_ENTRY', lambda: self.fs.stat())
        self.assertFalse((self.root/INTERNAL_ROOT).exists())

    def test_renameat2_never_falls_back_to_overwrite(self):
        self.put('one')
        from archive_backend import filesystem
        original = filesystem._LIBC.syscall
        def unsupported_rename(number, *args):
            if number.value in (316,276):
                ctypes.set_errno(errno.ENOSYS)
                return -1
            return original(number, *args)
        with patch.object(filesystem._LIBC, 'syscall', side_effect=unsupported_rename):
            self.assert_code('UNSUPPORTED_ENTRY', lambda: self.fs.rename_noreplace('one','two'))
        self.assertTrue((self.root/'one').exists())
        self.assertFalse((self.root/'two').exists())


if __name__ == '__main__':
    unittest.main()

    def test_tree_flush_checkpoints_precede_fenced_publication(self):
        from contextlib import contextmanager
        self.fs.create_internal_dir('staging','paced')
        for i in range(3):
            with self.fs.create_internal_file('staging','paced',str(i)) as f:f.write(b'verified')
        events=[]
        def checkpoint():
            self.assertFalse((self.root/'published').exists())
            events.append('checkpoint')
        @contextmanager
        def publication():
            events.append('enter')
            yield
            self.assertTrue((self.root/'published').is_dir())
            events.append('exit')
        self.fs.commit_staging('paced','published',checkpoint=checkpoint,publication=publication)
        self.assertEqual(events,['checkpoint']*5+['enter','exit'])

    def test_tree_flush_cancellation_preserves_stage_without_publication(self):
        self.fs.create_internal_dir('staging','cancel-flush')
        with self.fs.create_internal_file('staging','cancel-flush','file') as f:f.write(b'keep')
        def stop():raise BackendError('CANCELED','test cancellation')
        self.assert_code('CANCELED',lambda:self.fs.commit_staging('cancel-flush','absent',checkpoint=stop))
        self.assertFalse((self.root/'absent').exists())
        with self.fs.read_internal('staging','cancel-flush','file') as f:self.assertEqual(f.read(),b'keep')
