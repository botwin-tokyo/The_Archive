from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from archive_backend.contracts import BackendError
from archive_backend.filesystem import ConfinedFilesystem
from archive_backend.volumes import (MountInfo, VolumeConfig, VolumeError, VolumeRegistry,
    VerifiedVolume, parse_mountinfo, _mountinfo)


def escaped(path):
    return path.replace('\\', '\\134').replace(' ', '\\040').replace('\t','\\011').replace('\n','\\012')


class VolumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='archive-volume-test-')
        self.root = Path(self.temp.name)
        self.device = (os.major(self.root.stat().st_dev), os.minor(self.root.stat().st_dev))
        self.mount_id = 8701
        self.config = VolumeConfig('files', 'File Storage', str(self.root), str(self.root),
            'synthetic-uuid', 'ext4', self.device, '/dev/synthetic-a', self.mount_id)
        self.mount_text = self.row()
        self.addCleanup(self.temp.cleanup)

    def row(self, **changes):
        value = {'mount_id':self.mount_id,'device':self.device,'root':'/',
                 'point':str(self.root),'options':'rw,relatime','filesystem':'ext4','source':'/dev/synthetic-a'}
        value.update(changes)
        return f"{value['mount_id']} 1 {value['device'][0]}:{value['device'][1]} {escaped(value['root'])} {escaped(value['point'])} {value['options']} shared:9 - {value['filesystem']} {value['source']} rw\n"

    def registry(self, configs=None, resolver=None):
        registry = VolumeRegistry(configs if configs is not None else [self.config],
            mountinfo_reader=lambda:self.mount_text, uuid_resolver=resolver or (lambda uuid:self.device))
        self.addCleanup(registry.close)
        return registry

    def assert_code(self, code, action):
        with self.assertRaises(BackendError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def test_mountinfo_parsing_optional_fields_and_escapes(self):
        rows = parse_mountinfo(self.row(point='/mnt/File Storage', root='/some\tpath'))
        self.assertEqual(rows[0].mount_point, '/mnt/File Storage')
        self.assertEqual(rows[0].root, '/some\tpath')
        self.assertEqual(rows[0].device, self.device)
        self.assertEqual(rows[0].mount_id, self.mount_id)
        self.assertEqual(rows[0].source, '/dev/synthetic-a')
        self.assert_code('DRIVE_UNAVAILABLE', lambda:parse_mountinfo('invalid data'))

    def test_no_configured_volumes_by_default(self):
        registry = VolumeRegistry()
        self.assertEqual(registry.list(), [])
        self.assert_code('DRIVE_UNAVAILABLE', lambda: registry.get('missing'))
        self.assertEqual(os.listdir(self.root), [])

    def test_mountinfo_cache_does_not_share_mutable_results(self):
        rows = parse_mountinfo(self.mount_text)
        rows.clear()
        self.assertEqual(parse_mountinfo(self.mount_text)[0].mount_id, self.mount_id)

    def test_cached_mountinfo_still_reads_and_rejects_next_changed_snapshot(self):
        volume = self.registry().get('files')
        for _ in range(3):
            volume.check(volume.generation)
        self.mount_text = self.row(mount_id=self.mount_id + 1)
        self.assert_code('VOLUME_MISMATCH', lambda: volume.check(volume.generation))

    def test_cached_valid_mountinfo_does_not_hide_malformed_snapshot(self):
        volume = self.registry().get('files')
        volume.check(volume.generation)
        self.mount_text = 'invalid data'
        self.assert_code('DRIVE_UNAVAILABLE', lambda: volume.check(volume.generation))

    def test_verified_root_capacity_and_generation(self):
        volume = self.registry().get('files')
        volume.check(volume.generation)
        fd = volume.open_root()
        try:
            self.assertEqual(os.fstat(fd).st_ino, self.root.stat().st_ino)
        finally:
            os.close(fd)
        capacity = volume.capacity()
        self.assertGreater(capacity['total'],0)
        self.assertGreaterEqual(capacity['available'],0)
        self.assertFalse(capacity['read_only'])
        self.assertEqual(capacity['generation'],volume.generation)
        self.assertGreater(volume.generation,0)
        self.assertLess(volume.generation,2**53)

    def test_existing_bare_mount_directory_is_not_a_mounted_drive(self):
        self.mount_text = _mountinfo()
        self.assert_code('DRIVE_UNAVAILABLE', self.registry)
        self.assertEqual(os.listdir(self.root), [])

    def test_missing_root_never_created(self):
        absent = self.root/'missing'
        cfg = replace(self.config,root_path=str(absent))
        self.assert_code('DRIVE_UNAVAILABLE',lambda:self.registry([cfg]))
        self.assertFalse(absent.exists())
        self.assertEqual(os.listdir(self.root), [])

    def test_wrong_source_filesystem_device_root_and_mount_id(self):
        changes = ({'source':'/dev/wrong'}, {'filesystem':'btrfs'},
                   {'device':(999,999)}, {'root':'/bind-subtree'}, {'mount_id':9999})
        for change in changes:
            with self.subTest(change=change):
                self.mount_text = self.row(**change)
                self.assert_code('VOLUME_MISMATCH',self.registry)
                self.assertEqual(os.listdir(self.root), [])

    def test_wrong_uuid_device(self):
        self.assert_code('VOLUME_MISMATCH', lambda:self.registry(resolver=lambda uuid:(999,999)))
        self.assertEqual(os.listdir(self.root), [])

    def test_stale_device_stat_rejects_correct_looking_mountinfo(self):
        wrong_device=(999,999)
        self.mount_text=self.row(device=wrong_device)
        cfg=replace(self.config,expected_device=wrong_device)
        self.assert_code('VOLUME_MISMATCH',lambda:self.registry([cfg], resolver=lambda uuid:wrong_device))

    def test_duplicate_mount_point_and_nested_root_mount_rejected(self):
        self.mount_text += self.row(mount_id=8702)
        self.assert_code('DRIVE_UNAVAILABLE', self.registry)
        child=self.root/'child'
        child.mkdir()
        self.mount_text=self.row()+self.row(mount_id=8702,point=str(child))
        cfg=replace(self.config,root_path=str(child))
        self.assert_code('VOLUME_MISMATCH',lambda:self.registry([cfg]))

    def test_mount_generation_changed_invalidates_open_streams(self):
        volume=self.registry().get('files')
        fs=ConfinedFilesystem(volume)
        with fs.create_staging('write') as target:
            target.write(b'first')
            self.mount_text=self.row(mount_id=8702)
            self.assert_code('VOLUME_MISMATCH',lambda:target.write(b'second'))
            self.assertGreater(volume.generation,1)
            self.assert_code('DRIVE_UNAVAILABLE',lambda:target.write(b'second'))
        self.assertEqual((self.root/'.archive-console'/'staging'/'write').read_bytes(),b'first')

    def test_unpinned_mount_id_still_tracks_first_generation(self):
        volume=self.registry([replace(self.config,expected_mount_id=None)]).get('files')
        self.mount_text=self.row(mount_id=8702)
        self.assert_code('DRIVE_UNAVAILABLE',volume.check)

    def test_mount_disappears_before_mutation_zero_new_files(self):
        volume=self.registry().get('files')
        fs=ConfinedFilesystem(volume)
        self.mount_text=''
        self.assert_code('DRIVE_UNAVAILABLE',lambda:fs.create_dir('must-not-exist'))
        self.assertEqual(os.listdir(self.root),[])
        self.mount_text=self.row()
        self.assert_code('DRIVE_UNAVAILABLE',volume.check)

    def test_remount_at_commit_retains_partial_without_publishing(self):
        volume=self.registry().get('files')
        fs=ConfinedFilesystem(volume)
        with fs.create_staging('partial') as file:
            file.write(b'safe partial')
        self.mount_text=self.row(source='/dev/wrong')
        self.assert_code('VOLUME_MISMATCH',lambda:fs.commit_staging('partial','published'))
        self.assertFalse((self.root/'published').exists())
        self.assertEqual((self.root/'.archive-console'/'staging'/'partial').read_bytes(),b'safe partial')

    def test_root_replaced_invalidates_descriptor(self):
        volume=self.registry().get('files')
        renamed=self.root.with_name(self.root.name+'-old')
        os.rename(self.root,renamed)
        self.root.mkdir()
        try:
            self.assert_code('DRIVE_UNAVAILABLE',volume.check)
            self.assert_code('DRIVE_UNAVAILABLE',volume.open_root)
            self.assertEqual(os.listdir(self.root),[])
        finally:
            self.root.rmdir()
            os.rename(renamed,self.root)

    def test_root_symlink_ancestor_rejected(self):
        real=self.root/'real'
        real.mkdir()
        child=real/'child'
        child.mkdir()
        link=self.root/'link'
        link.symlink_to(real,target_is_directory=True)
        cfg=replace(self.config,root_path=str(link/'child'))
        self.assert_code('DRIVE_UNAVAILABLE',lambda:self.registry([cfg]))
        self.assertEqual(os.listdir(child),[])

    def test_mount_read_only_flag_discovered(self):
        self.mount_text=self.row(options='ro,relatime')
        volume=self.registry().get('files')
        self.assertTrue(volume.capacity()['read_only'])
        self.assert_code('READ_ONLY',lambda:ConfinedFilesystem(volume).create_dir('denied'))
        self.assertEqual(os.listdir(self.root),[])

    def test_uuid_revalidated_at_every_checkpoint(self):
        current=[self.device]
        volume=self.registry(resolver=lambda uuid:current[0]).get('files')
        current[0]=(999,999)
        self.assert_code('VOLUME_MISMATCH',volume.check)

    def test_production_rejects_host_root_or_duplicate_device(self):
        self.assert_code('INVALID_ARGUMENT',lambda:self.registry([replace(self.config,mount_point='/')]))
        self.assert_code('INVALID_ARGUMENT',lambda:self.registry([self.config,replace(self.config,volume_id='backups')]))
        self.assertEqual(os.listdir(self.root),[])

    def test_configuration_requires_stable_fields_and_canonical_paths(self):
        for cfg in (replace(self.config,filesystem_uuid=''), replace(self.config,expected_filesystem=''),
                    replace(self.config,root_path=str(self.root)+'/..'),replace(self.config,volume_id='../bad'),
                    replace(self.config,expected_device=(-1,2))):
            self.assert_code('INVALID_ARGUMENT',lambda:self.registry([cfg]))

    def test_fixture_same_device_independent_roots_and_source_identity(self):
        files=self.root/'files'
        backups=self.root/'backups'
        files.mkdir(); backups.mkdir()
        with VolumeRegistry.fixtures({'files':files,'backups':backups}) as registry:
            first=ConfinedFilesystem(registry.get('files'))
            second=ConfinedFilesystem(registry.get('backups'))
            first.create_dir('only-files')
            second.create_dir('only-backups')
            self.assertEqual([e.name for e in first.list()],['only-files'])
            self.assertEqual([e.name for e in second.list()],['only-backups'])
            self.assertNotEqual(first.stat().inode,second.stat().inode)
            self.assertEqual(first.stat().device,second.stat().device)

    def test_fixture_rejects_overlap_missing_and_outside_temp(self):
        child=self.root/'child'
        child.mkdir()
        self.assert_code('INVALID_ARGUMENT',lambda:VolumeRegistry.fixtures({'a':self.root,'b':child}))
        self.assert_code('INVALID_ARGUMENT',lambda:VolumeRegistry.fixtures({'a':'/'}))
        with self.assertRaises(FileNotFoundError):
            VolumeRegistry.fixtures({'a':self.root/'missing'})
        self.assertFalse((self.root/'missing').exists())

    def test_private_namespace_restart_preserves_backing_generation(self):
        cfg=replace(self.config,expected_mount_id=None)
        host=self.row(mount_id=101)
        def volume(local):
            result=VerifiedVolume(cfg,mountinfo_reader=lambda:local,uuid_resolver=lambda _:self.device,generation_mountinfo_reader=lambda:host)
            self.addCleanup(result.close)
            return result
        first=volume(self.row(mount_id=201));second=volume(self.row(mount_id=301))
        self.assertEqual(first.generation,second.generation)
        second.check(first.generation)
        host=self.row(mount_id=102)
        self.assert_code('DRIVE_UNAVAILABLE',second.check)
        third=volume(self.row(mount_id=302))
        self.assertNotEqual(first.generation,third.generation)

    def test_generation_stable_across_registry_recreation_same_mount(self):
        first=self.registry().get('files')
        second=self.registry().get('files')
        self.assertEqual(first.generation,second.generation)
        second.check(first.generation)

    def test_generation_changes_after_remount_across_registry_recreation(self):
        cfg=replace(self.config,expected_mount_id=None)
        first=self.registry([cfg]).get('files')
        old_generation=first.generation
        first.close()
        self.mount_text=self.row(mount_id=self.mount_id+1)
        second=self.registry([cfg]).get('files')
        self.assertNotEqual(old_generation,second.generation)
        self.assert_code('DRIVE_UNAVAILABLE',lambda:second.check(old_generation))
        second.check()

    def test_generation_changes_after_reboot_even_if_mount_ids_reused(self):
        with patch('archive_backend.volumes._boot_identity',return_value='11111111-1111-1111-1111-111111111111'):
            first=self.registry().get('files')
        with patch('archive_backend.volumes._boot_identity',return_value='22222222-2222-2222-2222-222222222222'):
            second=self.registry().get('files')
        self.assertNotEqual(first.generation,second.generation)
        self.assert_code('DRIVE_UNAVAILABLE',lambda:second.check(first.generation))

    def test_generation_changes_when_managed_root_identity_changes(self):
        first=self.registry().get('files')
        generation=first.generation
        moved=self.root.with_name(self.root.name+'-held')
        os.rename(self.root,moved)
        self.root.mkdir()
        try:
            second=self.registry().get('files')
            self.assertNotEqual(second.generation,generation)
            self.assert_code('DRIVE_UNAVAILABLE',lambda:second.check(generation))
        finally:
            self.root.rmdir()
            os.rename(moved,self.root)

    def test_world_writable_root_rejected(self):
        self.root.chmod(0o777)
        self.assert_code('DRIVE_UNAVAILABLE',self.registry)
        self.assertEqual(os.listdir(self.root),[])


if __name__=='__main__':
    unittest.main()
