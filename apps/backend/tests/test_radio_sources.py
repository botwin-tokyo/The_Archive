import hashlib,os,subprocess,unittest,uuid
import test_radio_service as fixtures
from archive_backend.radio_sources import prepare,open_payload
from archive_backend.contracts import BackendError

class RadioSourcesTests(unittest.TestCase):
    setUp=fixtures.RadioServiceTests.setUp
    tearDown=fixtures.RadioServiceTests.tearDown
    def test_recursive_discovery_and_preparation(self):
        path=self.f.root/'files'/'Music'/'real.mp3'
        subprocess.run(['/usr/bin/ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:duration=0.1','-c:a','libmp3lame',str(path)],check=True)
        listing=self.f.call('radio.tracks.list',{'location':{'driveId':'files','path':'Music'},'limit':1})
        found=list(listing['tracks'])
        while listing['nextCursor']:
            listing=self.f.call('radio.tracks.list',{'location':{'driveId':'files','path':'Music'},'cursor':listing['nextCursor'],'limit':1});found+=listing['tracks']
        e=next(e for e in found if e['name']=='real.mp3');before=path.read_bytes();metadata=path.stat()
        item={'source':self.f.ref(e),'deliveryId':str(uuid.uuid4())}
        item['_manifest']=prepare(self.f.service.radio,self.f.owner,item)
        with open_payload(self.config,item) as f:self.assertEqual(f.read(),before)
        self.assertEqual(item['_manifest']['sha256'],hashlib.sha256(before).hexdigest())
        self.assertEqual(path.stat().st_mtime_ns,metadata.st_mtime_ns)
        self.assertEqual(path.stat().st_mode,metadata.st_mode)
    def test_fake_mp3_and_unauthorized_root_rejected(self):
        with self.assertRaises(BackendError):prepare(self.f.service.radio,self.f.owner,{'source':self.ref,'deliveryId':str(uuid.uuid4())})
        with self.assertRaises(BackendError):self.f.call('radio.tracks.list',{'location':{'driveId':'files','path':''}})
    def test_preparation_attempts_never_overwrite_each_other_and_orphans_clean(self):
        from archive_backend.radio_sources import cleanup_orphans,spool_path
        path=self.f.root/'files'/'Music'/'real.mp3'
        subprocess.run(['/usr/bin/ffmpeg','-v','error','-f','lavfi','-i','sine=duration=0.05','-c:a','libmp3lame',str(path)],check=True)
        from archive_backend.catalog import present_entry
        ref=self.f.ref(present_entry(self.f.service.filesystems['files'].stat('Music/real.mp3'),'files'))
        item={'id':str(uuid.uuid4()),'position':0,'state':'ready','source':ref,'deliveryId':str(uuid.uuid4())}
        one=prepare(self.f.service.radio,self.f.owner,item);two=prepare(self.f.service.radio,self.f.owner,item)
        self.assertNotEqual(one['spoolName'],two['spoolName'])
        item['_manifest']=two
        with self.f.state.transaction():self.f.service.radio.save_item('fixture',item)
        cleanup_orphans(self.f.service.radio)
        self.assertFalse(spool_path(self.config,item['deliveryId'],one['spoolName']).exists())
        self.assertTrue(spool_path(self.config,item['deliveryId'],two['spoolName']).exists())
    def test_cursor_does_not_survive_scope_change(self):
        self.f.put('Music/other.mp3',b'not an mp3')
        result=self.f.call('radio.tracks.list',{'location':{'driveId':'files','path':'Music'},'limit':1})
        self.assertIsNotNone(result['nextCursor'])
        self.config['roots']=[{'driveId':'files','path':'Music/other'}]
        with self.assertRaises(BackendError):self.f.call('radio.tracks.list',{'location':{'driveId':'files','path':'Music'},'limit':1,'cursor':result['nextCursor']})
