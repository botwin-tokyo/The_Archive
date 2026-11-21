"""Export real disposable domain DTOs for cross-language contract tests only.

Run from the repository with mobile dependencies installed. The output is never
used by production services, and contains no access credentials or host paths.
"""
import argparse
import json
from pathlib import Path
import sys
import time
import uuid

root=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(root/'apps/backend/src'),str(root/'apps/backend/tests')]
from cryptography.fernet import Fernet
from archive_backend.mobile_boundary import MobileBoundary
from archive_backend.mobile_auth import stamp
from support import Fixture

parser=argparse.ArgumentParser()
parser.add_argument('--output',required=True)
args=parser.parse_args()
fixture=Fixture(); boundary=None
try:
    boundary=MobileBoundary(fixture.service,{'publicOrigin':'https://fixture.invalid','replayKeyFile':'unused'},replay_key=Fernet.generate_key())
    boundary.auth.enroll(fixture.owner,'fixture','synthetic contract password',['read','write','trash'],[{'driveId':'files','path':''},{'driveId':'backups','path':''}])
    login=boundary.auth.login({'username':'fixture','password':'synthetic contract password','deviceLabel':'Contract fixture','requestId':str(uuid.uuid4())})
    context=boundary.auth.authenticate(login['accessToken'])
    raw=fixture.put('source.txt',b'domain fixture bytes'); fixture.put('source.txt',b'collision',drive='backups')
    profile=boundary.profile(context)
    primary=next(r for r in profile['roots'] if r['ref']['driveId']=='files')
    backup=next(r for r in profile['roots'] if r['ref']['driveId']=='backups')
    query={'queryId':'fixture-folder-query','partition':{'serverId':profile['serverId'],'accountId':profile['accountId'],'rootId':primary['id']},'folder':primary['ref'],'cursor':None,'pageSize':100,'search':'','sort':{'field':'name','direction':'asc','foldersFirst':True}}
    page=boundary.metadata.folder(context,query)
    intent={'requestId':'cf072017-51a3-4a1c-95be-744e8b261027','linkedRequestId':None,'kind':'mkdir','sources':[],'destination':primary['ref'],'name':'Created folder','collisionPolicy':'ask','conditionsRefreshedAt':stamp(time.time())}
    accepted=boundary.metadata.execute(context,intent); fixture.service.run_one()
    completed=boundary.metadata.reconcile(context,intent['requestId'])
    conflict=dict(intent,requestId='cf072017-51a3-4a1c-95be-744e8b261028',kind='copy',sources=[boundary.metadata.ref(raw)],destination=backup['ref'],name=None)
    decision=boundary.metadata.execute(context,conflict)
    result={'schemaVersion':1,'profile':profile,'home':boundary.metadata.home(context),'folderPage':page,'fileEntry':page['entries'][0],
            'operationAccepted':accepted,'operationCompleted':completed,'operationConflict':decision}
    encoded=json.dumps(result,indent=2,ensure_ascii=False)+'\n'
    for secret in (login['accessToken'],login['refreshToken'],'synthetic contract password',str(fixture.root)):
        if secret in encoded: raise RuntimeError('Fixture secret or host path exposure')
    path=Path(args.output); path.parent.mkdir(parents=True,exist_ok=True); path.write_text(encoded)
    print(json.dumps({'fixtureOutput':str(path),'synthetic':True,'credentialFree':True}))
finally:
    if boundary: boundary.close()
    fixture.close()
