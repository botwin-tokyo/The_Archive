"""Opt-in disposable >8-GiB transfer qualification. Never accepts a real root."""
import hashlib
import json
import resource
import time
from support import Fixture
from archive_backend.contracts import CHUNK_BYTES

fixture=Fixture()
started=time.monotonic(); size=8*1024**3+CHUNK_BYTES; chunk=bytes(range(256))*(CHUNK_BYTES//256)
try:
    session=fixture.service.transfers.initiate(fixture.owner,{'requestId':'large-fixture','destination':{'driveId':'files','path':''},'name':'large.bin','sizeBytes':str(size)})
    expected=hashlib.sha256(); tid=session['transferId']
    for offset in range(0,size,CHUNK_BYTES):
        expected.update(chunk); fixture.service.transfers.write(fixture.owner,tid,offset,chunk)
        if offset and offset%(1024**3)==0: print(json.dumps({'acknowledgedBytes':offset+CHUNK_BYTES,'elapsedSeconds':round(time.monotonic()-started,2)}),flush=True)
    completed=fixture.service.transfers.finalize(fixture.owner,tid)
    assert completed['verification']['digest']==expected.hexdigest()
    downloaded=hashlib.sha256(); ref=completed['entry']
    for offset in range(0,size,CHUNK_BYTES): downloaded.update(fixture.service.transfers.read(fixture.owner,'files','large.bin',ref['revision'],offset,CHUNK_BYTES))
    assert downloaded.hexdigest()==expected.hexdigest()
    print(json.dumps({'result':'passed','bytes':size,'chunkBytes':CHUNK_BYTES,'sha256':expected.hexdigest(),'elapsedSeconds':round(time.monotonic()-started,2),'maxRssKiB':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,'scope':'development-host application-layer upload/download on disposable temp storage; not Pi/HAT or HTTP throughput'}),flush=True)
finally: fixture.close()
