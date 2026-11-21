#!/usr/bin/env python3
"""Offline vault administration. Run as service account with backend stopped.
Does not print keys or decrypted records. The vault lock rejects a live writer.
"""
import argparse,json,os,shutil,time
from pathlib import Path
from archive_backend.vault_store import VaultStore,read_at,atomic
from archive_backend.vault_crypto import seal,unseal

p=argparse.ArgumentParser();p.add_argument('--config',required=True);p.add_argument('action',choices=['provision','verify','export','repair','restore-empty']);p.add_argument('--output');p.add_argument('--input');p.add_argument('--source',type=int,choices=[0,1]);a=p.parse_args()
c=json.loads(Path(a.config).read_text())['vault'];store=VaultStore(c)
try:
 if a.action=='provision':store.provision();print('Vault provisioned on both verified devices.')
 elif a.action=='verify':_,health=store.read();print(json.dumps(health))
 elif a.action=='export':
  if not a.output:raise SystemExit('--output required')
  data,_=store.read();blob=seal(store.key,data,store.context('export')).encode()
  fd=os.open(a.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
  with os.fdopen(fd,'wb') as f:f.write(blob);f.flush();os.fsync(f.fileno())
  print('Encrypted export written. Preserve recovery key separately.')
 elif a.action=='restore-empty':
  if not a.input:raise SystemExit('--input required')
  blob=Path(a.input).read_text();data=unseal(store.key,blob,store.context('export'))
  if data['vaultId']!=store.identity:raise SystemExit('Recovery identity mismatch')
  current,_=store.read()
  if current['sequence']!=0 or current['entries']:raise SystemExit('Restore requires a newly provisioned empty vault')
  def restore(v):v['entries']=data['entries'];v['requests']=data['requests'];return True
  store.mutate(restore);print('Restored encrypted export to both replicas.')
 elif a.action=='repair':
  if a.source is None:raise SystemExit('--source required')
  source=c['replicas'][a.source];target=c['replicas'][1-a.source]
  if store.verify_mount(source)==store.verify_mount(target):raise SystemExit('Distinct devices required')
  fd=os.open(source['path'],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);anchor=store.anchor();n=anchor['sequence'];expected=anchor['hash']
  stage=Path(target['path']+'.repair-'+str(time.time_ns()));stage.mkdir(mode=0o700);out=os.open(stage,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
  try:
   for number in range(n,-1,-1):
    raw=read_at(fd,f'{number}.snapshot');value=store.verify_snapshot(raw,number,expected)
    marker=unseal(store.key,read_at(fd,f'{number}.commit').decode(),store.context('commit'))
    if marker!={'sequence':number,'hash':expected}:raise SystemExit('Source commit chain is invalid')
    atomic(out,f'{number}.snapshot',raw);store.marker(out,number,expected);expected=value.get('previous')
   os.fsync(out)
  finally:os.close(fd);os.close(out)
  old=target['path']+'.quarantine-'+str(time.time_ns());os.rename(target['path'],old);os.rename(stage,target['path'])
  parent=os.open(Path(target['path']).parent,os.O_RDONLY|os.O_DIRECTORY)
  try:os.fsync(parent)
  finally:os.close(parent)
  store.read();print('Replica repaired from anchored source; old copy preserved in quarantine.')
finally:store.close()
