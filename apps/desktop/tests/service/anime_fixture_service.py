"""Disposable anime contract-test process: real HTTP/MCP adapters and AnimeService, fake worker.

No production state, disk, token, network or listener config. The fake worker returns
fixed catalog data and writes a small artifact into a temporary spool.
"""
import base64
import hashlib
import json
import os
import tempfile
import threading
from dataclasses import replace
from pathlib import Path
from archive_backend.application import Service
from archive_backend.anime_service import AnimeService
from archive_backend.config import parse_config
from archive_backend.volumes import VolumeRegistry
from archive_backend.http_adapter import create_server

CATALOG={'anilistId':154587,'malId':52991,'title':'Fixture Frieren','altTitles':[],'year':2023,'format':'TV','status':'FINISHED','episodes':2,'hasCover':True}
COVER='data:image/png;base64,'+base64.b64encode(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNovrEeAAPsAgsNlzATAAAAAElFTkSuQmCC')).decode()
PROVIDER='settlar/fixture-frieren-6351'


class Worker:
    def __init__(self, spool): self.spool=spool; self.tasks={}
    def call(self, **args):
        op=args['op']; task=args.get('taskId')
        if op=='status': return {'available':True,'epoch':'fixture'}
        if op=='start':
            payload=args['payload']
            if args['action']=='search': result={'items':[CATALOG],'hasNextPage':False,'covers':{'154587':COVER}}
            elif args['action']=='lookup':
                result={'catalog':CATALOG,'description':'Fixture description','genres':['Adventure'],'relations':[],'match':PROVIDER,
                        'candidates':[{'providerSeriesId':PROVIDER,'title':'Fixture Frieren','year':2023,'type':'TV','episodeCount':2,'anilistId':154587,'malId':52991,'evidence':'anilist-id'}],
                        'episodes':[{'id':'e1.one','number':1,'title':'One','sub':True,'dub':True},{'id':'e1.two','number':2,'title':None,'sub':True,'dub':False}],
                        'samples':{'sub':{'heights':[1080],'subtitles':['en']},'dub':{'heights':[1080],'subtitles':['en']}},'cover':COVER}
            else:
                directory=self.spool/task; directory.mkdir(exist_ok=True); data=b'fixture episode '+payload['episodeId'].encode()
                path=directory/'media.mp4'; path.write_bytes(data); path.chmod(0o640)
                result={'filename':'media.mp4','sizeBytes':str(len(data)),'sha256':hashlib.sha256(data).hexdigest(),'height':1080,'durationSeconds':1440.0,
                        'audioLanguage':'jpn' if payload['audio']=='sub' else 'eng','subtitles':payload['subtitles'],'manifest':'0'*64}
            self.tasks[task]={'state':'completed','result':result,'epoch':'fixture'}
        if op=='cleanup': self.tasks.pop(task,None)
        return self.tasks.get(task,{'state':'absent','epoch':'fixture'})


with tempfile.TemporaryDirectory(prefix='archive-desktop-anime-fixture-') as directory:
    root=Path(directory); data=root/'data'; data.mkdir(); spool=root/'spool'; spool.mkdir()
    service=Service(parse_config({'statePath':str(root/'state.sqlite'),'minFreeBytes':0}),registry=VolumeRegistry.fixtures({'files':data}))
    service.config=replace(service.config,volumes=({'id':'files','role':'files','label':'File Storage'},))
    service.anime=AnimeService(service,{'socket':'/unused','spool':str(spool),'workerUid':os.getuid()},Worker(spool))
    service.auth.bootstrap_owner('desktop-test-fixture-owner-credential-00000000')
    service.auth.register(service.authenticate('desktop-test-fixture-owner-credential-00000000'),'Fixture writer','desktop-test-fixture-agent-credential-00000000',['read','write'],[{'driveId':'files','path':''}])
    service.server=create_server(service,'127.0.0.1',0)
    thread=threading.Thread(target=service.server.serve_forever,daemon=True); thread.start(); service.ready=True
    stop=threading.Event()
    def work():
        while not stop.wait(0.05):
            service.run_one()
            with service.anime.lock: service.anime.tick()
    worker=threading.Thread(target=work,daemon=True); worker.start()
    print(json.dumps({'port':service.server.server_address[1]}),flush=True)
    try: input()
    finally:
        stop.set(); worker.join(timeout=2); service.close()
