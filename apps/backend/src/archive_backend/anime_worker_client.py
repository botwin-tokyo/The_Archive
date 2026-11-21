"""Only a UID-verified local anime worker can return catalog or acquisition results."""
import json
import socket
import struct
from .contracts import BackendError

MAX_RESPONSE = 4 * 1024 * 1024


class AnimeWorkerClient:
    def __init__(self, config): self.config=config

    def call(self, **args):
        try:
            with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
                connection.settimeout(5); connection.connect(self.config['socket'])
                _,uid,_=struct.unpack('3i',connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
                if uid!=self.config['workerUid']: raise BackendError('PERMISSION_DENIED','Unexpected anime worker identity')
                connection.sendall(json.dumps(args).encode()+b'\n')
                with connection.makefile('rb') as stream: raw=stream.readline(MAX_RESPONSE+1)
            if len(raw)>MAX_RESPONSE: raise ValueError('Response limit')
            value=json.loads(raw)
            if not value.get('ok'): raise BackendError('UNAVAILABLE','Anime worker is busy or unavailable',True)
            return value['result']
        except BackendError: raise
        except (OSError,ValueError,KeyError): raise BackendError('UNAVAILABLE','Anime worker is unavailable',True) from None
