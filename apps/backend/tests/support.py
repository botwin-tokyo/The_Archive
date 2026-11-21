import tempfile
from pathlib import Path
from archive_backend.config import parse_config
from archive_backend.persistence import State
from archive_backend.volumes import VolumeRegistry
from archive_backend.application import Service

class Fixture:
    def __init__(self,fault=lambda _:None,drive_ids=('files','backups')):
        self.temp=tempfile.TemporaryDirectory(prefix='archive-service-test-')
        self.root=Path(self.temp.name)
        for name in drive_ids: (self.root/name).mkdir()
        self.state=State(self.root/'state.sqlite')
        self.registry=VolumeRegistry.fixtures({name:self.root/name for name in drive_ids})
        self.service=Service(parse_config({'minFreeBytes':0}),self.state,self.registry,fault=fault)
        self.token='test-owner-'+('x'*40)
        self.owner=self.service.auth.bootstrap_owner(self.token)
        self.service.ready=True
    def call(self,capability,arguments=None,token=None):
        return self.service.call(token or self.token,{'version':1,'capability':capability,'arguments':arguments or {}})
    def put(self,name='a.bin',data=b'opaque\0data',drive='files'):
        fs=self.service.filesystems[drive]
        import uuid
        token=uuid.uuid4().hex
        with fs.create_staging(token) as f: f.write(data)
        fs.commit_staging(token,name)
        return next(e for e in self.call('files.list',{'query':{'requestId':'list','location':{'driveId':drive,'path':name.rpartition('/')[0]}}})['entries'] if e['name']==name.rsplit('/',1)[-1])
    def ref(self,entry): return {k:entry[k] for k in ('id','driveId','path','revision')}
    def close(self): self.service.close(); self.temp.cleanup()
