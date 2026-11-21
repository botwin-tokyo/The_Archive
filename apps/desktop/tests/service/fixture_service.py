"""Disposable contract-test process. No production state, disk, token or listener config."""
import base64
import json
import tempfile
import threading
from pathlib import Path
from archive_backend.application import Service
from archive_backend.config import parse_config
from archive_backend.volumes import VolumeRegistry
from archive_backend.http_adapter import create_server

with tempfile.TemporaryDirectory(prefix='archive-desktop-http-fixture-') as directory:
    root=Path(directory)
    data=root/'data';data.mkdir()
    (data/'seed.bin').write_bytes(b'opaque\x00seed')
    (data/'preview.png').write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNovrEeAAPsAgsNlzATAAAAAElFTkSuQmCC'))
    service=Service(parse_config({'statePath':str(root/'state.sqlite'),'minFreeBytes':0}),registry=VolumeRegistry.fixtures({'files':data}))
    service.auth.bootstrap_owner('desktop-test-fixture-owner-credential-00000000')
    service.auth.register(service.authenticate('desktop-test-fixture-owner-credential-00000000'),'Fixture reader','desktop-test-fixture-agent-credential-00000000',['read'],[{'driveId':'files','path':''}])
    service.server=create_server(service,'127.0.0.1',0)
    thread=threading.Thread(target=service.server.serve_forever,daemon=True);thread.start();service.ready=True
    stop=threading.Event()
    def work():
        while not stop.wait(0.01):
            service.run_one()
    worker=threading.Thread(target=work,daemon=True);worker.start()
    print(json.dumps({'port':service.server.server_address[1]}),flush=True)
    try: input()
    finally:
        stop.set();worker.join(timeout=2);service.close()
