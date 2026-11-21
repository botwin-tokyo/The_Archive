"""Storage safety regressions derived from deterministic commit/interleaving faults.

These assert the required invariant, rather than accepting a known unsafe result.
All fixtures are disposable; no real disks, network, or credentials are touched.
"""
import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import unittest

from support import Fixture
from archive_backend.contracts import BackendError


class InjectedCrash(RuntimeError):
    pass


class StorageFaultTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)

    def begin(self, data=b'abc', name='upload.bin', request='upload'):
        return self.f.call('transfers.initiate', {
            'requestId':request,'destination':{'driveId':'files','path':''},
            'name':name,'sizeBytes':str(len(data)),
            'sha256':hashlib.sha256(data).hexdigest()})

    def lease(self, request):
        receipt = self.f.call('operations.submit', {'request':request})
        lease = self.f.service.jobs.claim('fault-worker', job_id=receipt['jobIds'][0])
        self.assertIsNotNone(lease)
        return lease

    def test_chunk_ack_requires_fsync_before_durable_offset_advance(self):
        session = self.begin()
        tid = session['transferId']
        events = []
        original_sync = os.fsync
        original_put = self.f.state.put
        def sync(fd):
            events.append('fsync')
            return original_sync(fd)
        def put(namespace, key, record):
            if namespace == 'transfers' and key == tid and record['offset'] == '3':
                events.append('acknowledged-offset')
            return original_put(namespace, key, record)
        with patch('archive_backend.filesystem.os.fsync', side_effect=sync), patch.object(self.f.state,'put',side_effect=put):
            result = self.f.service.write_chunk(self.f.token,tid,0,b'abc')
        self.assertEqual(result['offset'],'3')
        self.assertIn('fsync',events)
        self.assertLess(events.index('fsync'),events.index('acknowledged-offset'))

    def test_stale_concurrent_chunk_cannot_overwrite_acknowledged_range(self):
        session = self.begin(b'NEW')
        tid = session['transferId']
        transfers = self.f.service.transfers
        original_session = transfers._session
        captured, resume = threading.Event(), threading.Event()
        blocked = False
        def session_with_barrier(*args, **kwargs):
            nonlocal blocked
            record = original_session(*args, **kwargs)
            if threading.current_thread().name.startswith('stale-range') and not blocked:
                blocked = True
                captured.set()
                if not resume.wait(5):
                    raise AssertionError('Concurrent range test timed out')
            return record
        def stale_write():
            try:
                self.f.service.write_chunk(self.f.token,tid,0,b'OLD')
            except BackendError:
                return True
            return False
        with patch.object(transfers,'_session',side_effect=session_with_barrier):
            with ThreadPoolExecutor(max_workers=1,thread_name_prefix='stale-range') as pool:
                result=pool.submit(stale_write)
                try:
                    self.assertTrue(captured.wait(5))
                    committed=self.f.service.write_chunk(self.f.token,tid,0,b'NEW')
                    self.assertEqual(committed['offset'],'3')
                finally:
                    resume.set()
                self.assertTrue(result.result(timeout=5),'Conflicting replay must be rejected after lease acquisition')
        with self.f.service.filesystems['files'].read_internal('staging',tid) as stream:
            self.assertEqual(stream.read(),b'NEW','A stale request overwrote already acknowledged bytes')
        self.assertEqual(self.f.call('transfers.query',{'transferId':tid})['offset'],'3')

    def test_recovered_trash_intent_restores_visible_metadata(self):
        source = self.f.put('source.bin',b'keep me')
        operations = self.f.service.operations
        def fault(point):
            if point == 'after-trash-rename':
                raise InjectedCrash(point)
        operations.fault = fault
        with self.assertRaises(InjectedCrash):
            operations._trash(self.f.owner,'files',source['path'],source['revision'],'trash-fault')
        self.assertFalse((self.f.root/'files/source.bin').exists())
        operations.fault = lambda _:None
        token = operations._trash(self.f.owner,'files',source['path'],source['revision'],'trash-fault')
        self.assertIsNotNone(self.f.state.get('trash',token),'Recovered Trash content must be discoverable')
        self.assertEqual(len(self.f.call('trash.list',{'driveId':'files'})),1)

    def test_stale_source_replace_does_not_change_destination(self):
        source = self.f.put('source.bin',b'first')
        self.f.put('target.bin',b'original target')
        (self.f.root/'files/source.bin').write_bytes(b'changed source')
        lease = self.lease({'requestId':'stale-rename','kind':'rename','targets':[self.f.ref(source)],
                            'name':'target.bin','conflictPolicy':'replace'})
        with self.assertRaises(BackendError):
            self.f.service.operations.execute(lease)
        self.assertTrue((self.f.root/'files/target.bin').exists(),'A rejected stale operation moved the destination')
        self.assertEqual((self.f.root/'files/target.bin').read_bytes(),b'original target')

    def test_same_location_replace_does_not_trash_source(self):
        source = self.f.put('same.bin',b'keep')
        lease = self.lease({'requestId':'same-rename','kind':'rename','targets':[self.f.ref(source)],
                            'name':'same.bin','conflictPolicy':'replace'})
        try:
            self.f.service.operations.execute(lease)
        except BackendError:
            pass
        self.assertTrue((self.f.root/'files/same.bin').exists(),'Same-name rename removed its own source')
        self.assertEqual((self.f.root/'files/same.bin').read_bytes(),b'keep')

    def test_rename_replay_reconciles_before_replace_side_effect(self):
        source = self.f.put('source.bin',b'keep')
        operations = self.f.service.operations
        lease = self.lease({'requestId':'rename-replay','kind':'rename','targets':[self.f.ref(source)],
                            'name':'target.bin','conflictPolicy':'replace'})
        operations.fault = lambda point: (_ for _ in ()).throw(InjectedCrash(point)) if point=='after-rename' else None
        with self.assertRaises(InjectedCrash):
            operations.execute(lease)
        self.assertEqual((self.f.root/'files/target.bin').read_bytes(),b'keep')
        operations.fault = lambda _:None
        try:
            operations.execute(lease)
        except BackendError:
            pass
        self.assertTrue((self.f.root/'files/target.bin').exists(),'Retry removed the already committed destination')
        self.assertEqual((self.f.root/'files/target.bin').read_bytes(),b'keep')
        self.assertEqual(self.f.service.jobs.get(self.f.owner,lease['job']['id'])['state'],'completed')

    def test_verified_staging_resume_rechecks_content_not_only_size(self):
        source = self.f.put('source.bin',b'GOOD')
        operations = self.f.service.operations
        lease = self.lease({'requestId':'copy-replay','kind':'copy','targets':[self.f.ref(source)],
                            'destination':{'driveId':'backups','path':''}})
        operations.fault = lambda point: (_ for _ in ()).throw(InjectedCrash(point)) if point=='after-copy-verify' else None
        with self.assertRaises(InjectedCrash):
            operations.execute(lease)
        token=hashlib.sha256((lease['job']['id']+':0:stage').encode()).hexdigest()
        with self.f.service.filesystems['backups'].open_staging(token) as file:
            file.write(b'EVIL')
            file.flush()
        operations.fault=lambda _:None
        try:
            operations.execute(lease)
        except BackendError:
            pass
        self.assertFalse((self.f.root/'backups/source.bin').exists(),'Corrupted same-size staging was published as verified')
        self.assertEqual((self.f.root/'files/source.bin').read_bytes(),b'GOOD')

    def test_recursive_move_preserves_source_if_child_changes_after_copy(self):
        fs=self.f.service.filesystems['files']
        fs.create_dir('folder')
        self.f.put('folder/child',b'old data')
        source = next(e for e in self.f.call('files.list',{'query':{'requestId':'folder','location':{'driveId':'files','path':''}}})['entries'] if e['name']=='folder')
        lease=self.lease({'requestId':'move-change','kind':'move','targets':[self.f.ref(source)],
                          'destination':{'driveId':'backups','path':''}})
        def change(point):
            if point=='after-copy-verify':
                (self.f.root/'files/folder/child').write_bytes(b'new data')
        self.f.service.operations.fault=change
        try:
            self.f.service.operations.execute(lease)
        except BackendError:
            pass
        self.assertTrue((self.f.root/'files/folder/child').exists(),'Changed source tree was removed after stale verification')
        self.assertEqual((self.f.root/'files/folder/child').read_bytes(),b'new data')
        self.assertFalse((self.f.root/'backups/folder').exists(),'A source change must be detected before publication')

    def test_recursive_move_detects_new_child_inside_nested_directory(self):
        fs=self.f.service.filesystems['files']
        fs.create_dir('folder')
        fs.create_dir('folder/nested')
        self.f.put('folder/nested/original',b'old')
        source=next(e for e in self.f.call('files.list',{'query':{'requestId':'nested-folder',
            'location':{'driveId':'files','path':''}}})['entries'] if e['name']=='folder')
        lease=self.lease({'requestId':'move-new-child','kind':'move','targets':[self.f.ref(source)],
                          'destination':{'driveId':'backups','path':''}})
        def change(point):
            if point=='after-copy-verify':
                (self.f.root/'files/folder/nested/added').write_bytes(b'new uncopied data')
        self.f.service.operations.fault=change
        try:
            self.f.service.operations.execute(lease)
        except BackendError:
            pass
        self.assertTrue((self.f.root/'files/folder/nested/added').exists(),
                        'A newly added nested child was moved away without being copied')
        self.assertFalse((self.f.root/'backups/folder').exists())

    def test_upload_publication_respects_locked_copy_source(self):
        source=self.f.put('source.bin',b'GOOD')
        session=self.f.call('transfers.initiate',{'requestId':'replace-source',
            'destination':{'driveId':'files','path':''},'name':'source.bin',
            'sizeBytes':'3','sha256':hashlib.sha256(b'NEW').hexdigest(),'conflictPolicy':'replace'})
        self.f.service.write_chunk(self.f.token,session['transferId'],0,b'NEW')
        lease=self.lease({'requestId':'locked-copy','kind':'copy','targets':[self.f.ref(source)],
                          'destination':{'driveId':'backups','path':''}})
        operations=self.f.service.operations
        # Conflicting work is a wait, not a failure: publication must block until the lock is released.
        with ThreadPoolExecutor(1) as pool:
            with operations._locks(lease['job']['id'],lease['workerId'],lease['fence'],[source]):
                finalize=pool.submit(self.f.call,'transfers.finalize',{'transferId':session['transferId']})
                with self.assertRaises(TimeoutError):
                    finalize.result(timeout=1)
                self.assertEqual((self.f.root/'files/source.bin').read_bytes(),b'GOOD',
                                 'Upload publication bypassed a live source lock held by another job')
            try:
                finalize.result(timeout=30)
            except BackendError:
                pass

    def test_listing_scan_budget_applies_to_nonmatching_entries(self):
        self.f.put('one.bin')
        self.f.put('two.bin')
        catalog=self.f.service.catalog
        catalog.max_entries=1
        with self.assertRaises(BackendError) as caught:
            self.f.call('files.list',{'query':{'requestId':'bounded-scan','location':{'driveId':'files','path':''},'search':'no-matches'}})
        self.assertEqual(caught.exception.code,'QUOTA_EXCEEDED')

    def test_upload_download_session_direction_cannot_be_confused(self):
        source=self.f.put('source.bin',b'abc')
        download=self.f.call('transfers.download',{'ref':self.f.ref(source),'requestId':'download-direction'})
        upload=self.begin()
        for action in (lambda:self.f.service.write_chunk(self.f.token,download['transferId'],0,b'x'),
                       lambda:self.f.call('transfers.finalize',{'transferId':download['transferId']}),
                       lambda:self.f.call('transfers.downloadProgress',{'transferId':upload['transferId'],'offset':'0','complete':False})):
            with self.assertRaises(BackendError) as caught:
                action()
            self.assertEqual(caught.exception.code,'INVALID_ARGUMENT')
        self.assertEqual(self.f.service.jobs.get(self.f.owner,download['jobId'])['state'],'waiting-for-client')
        self.assertEqual(self.f.service.jobs.get(self.f.owner,upload['jobId'])['state'],'waiting-for-client')

    def test_published_upload_recovers_lost_terminal_update(self):
        from archive_backend.recovery import reconcile_transfer
        for point in ('after-publish-rename','before-transfer-terminal'):
            with self.subTest(point=point):
                session=self.begin(b'abc',name=point,request=point)
                self.f.service.write_chunk(self.f.token,session['transferId'],0,b'abc')
                component=self.f.service.operations if point=='after-publish-rename' else self.f.service.transfers
                component.fault=lambda actual: (_ for _ in ()).throw(InjectedCrash(actual)) if actual==point else None
                with self.assertRaises(BackendError) as safe:
                    self.f.call('transfers.finalize',{'transferId':session['transferId']})
                self.assertEqual(safe.exception.code,'INTERNAL_ERROR')
                component.fault=lambda _:None
                self.assertEqual((self.f.root/'files'/point).read_bytes(),b'abc')
                self.assertTrue(reconcile_transfer(self.f.service,session['transferId']))
                result=self.f.call('transfers.finalize',{'transferId':session['transferId']})
                self.assertEqual(result['verification']['digest'],hashlib.sha256(b'abc').hexdigest())
                self.assertEqual(self.f.service.jobs.get(self.f.owner,session['jobId'])['state'],'completed')

    def test_upload_recovery_rejects_changed_published_bytes(self):
        from archive_backend.recovery import reconcile_transfer
        session=self.begin(b'GOOD')
        self.f.service.write_chunk(self.f.token,session['transferId'],0,b'GOOD')
        self.f.service.transfers.fault=lambda point: (_ for _ in ()).throw(InjectedCrash(point)) if point=='before-transfer-terminal' else None
        with self.assertRaises(BackendError) as safe:
            self.f.call('transfers.finalize',{'transferId':session['transferId']})
        self.assertEqual(safe.exception.code,'INTERNAL_ERROR')
        (self.f.root/'files/upload.bin').write_bytes(b'EVIL')
        with self.assertRaises(BackendError) as caught:
            reconcile_transfer(self.f.service,session['transferId'])
        self.assertEqual(caught.exception.code,'INTEGRITY_FAILED')
        self.assertNotEqual(self.f.state.get('transfers',session['transferId'])['state'],'completed')

    def test_skipped_upload_partial_remains_explicitly_discardable(self):
        self.f.put('existing.bin',b'keep original')
        session=self.f.call('transfers.initiate',{'requestId':'skip-retained',
            'destination':{'driveId':'files','path':''},'name':'existing.bin','sizeBytes':'3',
            'sha256':hashlib.sha256(b'NEW').hexdigest(),'conflictPolicy':'skip'})
        self.f.service.write_chunk(self.f.token,session['transferId'],0,b'NEW')
        result=self.f.call('transfers.finalize',{'transferId':session['transferId']})
        self.assertIsNone(result['entry'])
        discarded=self.f.call('transfers.discard',{'transferId':session['transferId']})
        self.assertEqual(discarded['state'],'discarded')
        self.assertEqual((self.f.root/'files/existing.bin').read_bytes(),b'keep original')
        with self.assertRaises(BackendError) as caught:
            self.f.service.filesystems['files'].internal_stat('staging',session['transferId'])
        self.assertEqual(caught.exception.code,'NOT_FOUND')

    def test_existing_upload_reservation_is_respected_by_copy(self):
        self.f.put('source.bin',b'x'*80,drive='backups')
        source=self.f.call('files.list',{'query':{'requestId':'src','location':{'driveId':'backups','path':''}}})['entries'][0]
        volume=self.f.service.filesystems['files'].volume
        real=volume.capacity()
        with patch.object(volume,'capacity',return_value={**real,'available':100}):
            self.begin(b'x'*70)
            lease=self.lease({'requestId':'reserved-copy','kind':'copy','targets':[self.f.ref(source)],
                              'destination':{'driveId':'files','path':''}})
            try:
                self.f.service.operations.execute(lease)
            except BackendError:
                pass
        self.assertFalse((self.f.root/'files/source.bin').exists(),'Copy consumed space reserved for an accepted upload')


if __name__=='__main__':
    unittest.main()
