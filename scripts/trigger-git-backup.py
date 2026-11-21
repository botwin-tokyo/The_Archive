#!/usr/bin/python3
"""Local timer trigger: UID-authenticated socket, no bearer credentials."""
import datetime
import json
import os
import socket
import struct
import time
from zoneinfo import ZoneInfo
for attempt in range(3):
    try:
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
            connection.settimeout(15);connection.connect('/run/archive-console/git-control.sock')
            _,uid,_=struct.unpack('3i',connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
            if uid!=os.geteuid():raise RuntimeError('Unexpected coordinator identity')
            day=datetime.datetime.now(ZoneInfo('UTC')).date().isoformat()
            connection.sendall(json.dumps({'action':'trigger','requestId':'midnight-'+day}).encode()+b'\n')
            with connection.makefile('rb') as f:reply=json.loads(f.readline(8192))
            if not reply.get('ok'):raise RuntimeError('Backup trigger rejected')
            print('Git backup trigger:',reply['result']['state']);break
    except (OSError,ValueError,RuntimeError):
        if attempt==2:raise SystemExit('Git backup trigger failed; last saved repositories remain available')
        time.sleep(10)
