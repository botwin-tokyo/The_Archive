import unittest
from archive_backend.contracts import BackendError, bytes_value, path, name, validate_call, CAPABILITIES, translate_error
from archive_backend.config import parse_config
from archive_backend.bootstrap import Lifecycle

class Contracts(unittest.TestCase):
    def test_closed_defaults(self):
        c=parse_config({}); self.assertIsNone(c.listen); self.assertEqual(c.volumes,()); self.assertIsNone(c.state_path)
    def test_unknown_and_versions(self):
        for v in ({'banana':1},{'version':2},{'listen':{'host':'0.0.0.0','port':8000}},{'volumes':[{'rootPath':''}]}):
            with self.assertRaises(BackendError): parse_config(v)
    def test_multiple_volumes_keep_identity_validation_and_budget(self):
        def volume(index):
            return {'id':f'disk{index}', 'label':f'Disk {index}', 'role':'files',
                    'rootPath':f'/mnt/disk{index}/archive', 'mountPoint':f'/mnt/disk{index}',
                    'filesystemUuid':f'uuid-{index}', 'expectedFilesystem':'ext4',
                    'expectedDevice':[8,index], 'expectedMountSource':f'/dev/disk{index}'}
        volumes=[volume(i) for i in range(16)]
        self.assertEqual(len(parse_config({'volumes':volumes[:3]}).volumes),3)
        self.assertEqual(len(parse_config({'volumes':volumes}).volumes),16)
        for invalid in (volumes+[volume(16)], volumes[:3]+[volumes[0]], 'invalid'):
            with self.assertRaises(BackendError): parse_config({'volumes':invalid})
    def test_paths(self):
        for v in ('/etc','../x','a/../b','a//b','a\\b','.archive-console/a','\0','a/' ):
            with self.assertRaises(BackendError): path(v)
        self.assertEqual(path('猫/hello.txt'),'猫/hello.txt')
        with self.assertRaises(BackendError): name('a/b')
    def test_decimal(self):
        self.assertEqual(bytes_value('9223372036854775807'),2**63-1)
        for v in ('00','-1','1.0','9223372036854775808',1):
            with self.assertRaises(BackendError): bytes_value(v)
    def test_registry_validation(self):
        self.assertGreaterEqual(len(CAPABILITIES),30)
        c,_=validate_call({'version':1,'capability':'drives.list','arguments':{}}); self.assertEqual(c.scope,'read')
        for v in ({'version':2,'capability':'drives.list','arguments':{}},{'version':1,'capability':'drives.list','arguments':{'root':'/'}}):
            with self.assertRaises(BackendError): validate_call(v)
    def test_safe_error_lifecycle(self):
        e=translate_error(ValueError('SECRET /private/file')); self.assertNotIn('SECRET',str(e)); self.assertEqual(e.code,'INTERNAL_ERROR')
        life=Lifecycle(); called=[]; life.add(lambda:called.append(True)); life.ready=True; life.close(); self.assertFalse(life.ready); self.assertEqual(called,[True])
    def test_independent_service_scaffold(self):
        from archive_backend.application import Service
        service=Service.from_config(parse_config({})); service.start(); self.assertTrue(service.ready); service.close(); self.assertFalse(service.ready)
