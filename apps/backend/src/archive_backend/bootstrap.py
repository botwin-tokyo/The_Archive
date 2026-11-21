"""Independent lifecycle. Importing this module does not open files/listeners."""
import argparse
import json
import signal
import threading
from .config import parse_config

class Lifecycle:
    def __init__(self):
        self.stopping=threading.Event()
        self.ready=False
        self._closers=[]
    def add(self, close):
        self._closers.append(close)
    def stop(self, *_):
        self.ready=False
        self.stopping.set()
    def close(self):
        self.stop()
        for close in reversed(self._closers):
            close()


def main():
    parser=argparse.ArgumentParser(description='The Archive local storage service')
    parser.add_argument('--config', required=True)
    parser.add_argument('--check-config',action='store_true')
    parser.add_argument('--enroll-owner',action='store_true',help='Explicit local interactive owner enrollment; never automatic')
    args=parser.parse_args()
    with open(args.config, encoding='utf8') as source:
        config=parse_config(json.load(source))
    if args.enroll_owner:
        from .enrollment import enroll_owner
        enroll_owner(config.state_path)
        return
    if args.check_config:
        print(json.dumps({'valid':True,'configuredVolumes':len(config.volumes),'listenerConfigured':config.listen is not None}))
        return
    from .application import Service
    service=Service.from_config(config)
    lifecycle=Lifecycle(); lifecycle.add(service.close)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig,lifecycle.stop)
    lifecycle.ready=True
    try:
        service.start()
        lifecycle.stopping.wait()
    finally:
        lifecycle.close()
