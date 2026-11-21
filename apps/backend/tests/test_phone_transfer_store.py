import hashlib
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from archive_backend.phone_transfer_store import PhoneStore
from archive_backend.phone_transfer_contracts import PhoneError,LIMITS

class PhoneStoreTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.store=PhoneStore(self.temp.name);self.actor='sender';self.data=b'hello phone\x00\xff';self.request=str(uuid.uuid4())
 def tearDown(self):self.store.close();self.temp.cleanup()
 def args(self,data=None,**extra):
  data=self.data if data is None else data
  return {'requestId':self.request,'inboxId':'phone-primary','title':' Test ','message':'  Ω\n ','attachments':[{'clientAttachmentId':'one','filename':'../../display\r\n.pdf','mediaType':'application/octet-stream','byteLength':len(data),'sha256':hashlib.sha256(data).hexdigest()}],**extra}
 def begin(self,data=None,**extra):return self.store.prepare(self.actor,self.args(data,**extra))[0]
 def chunk(self,p,data=None):
  data=self.data if data is None else data;a=p['attachments'][0]
  return self.store.chunk(self.actor,p['deliveryId'],a['attachmentId'],f'bytes 0-{len(data)-1}/{len(data)}',hashlib.sha256(data).hexdigest(),data)
 def test_prepare_replay_defaults_and_conflict(self):
  p=self.begin();q,new=self.store.prepare(self.actor,self.args(senderLabel='Agent',links=[],retentionDays=30));self.assertFalse(new);self.assertEqual(p,q)
  with self.assertRaises(PhoneError):self.begin(title='changed')
  self.assertEqual(self.store.operation(self.actor,self.request)['state'],'uploading')
  with self.assertRaises(PhoneError):self.store.operation('other',self.request)
 def test_chunk_restart_and_replay(self):
  p=self.begin();r=self.chunk(p);self.assertEqual(r,self.chunk(p));self.store.close();self.store=PhoneStore(self.temp.name)
  self.assertEqual(self.store.uploads(self.actor,p['deliveryId'])['attachments'][0]['receivedBytes'],len(self.data))
  with self.assertRaises(PhoneError):self.store.chunk('other',p['deliveryId'],p['attachments'][0]['attachmentId'],'bytes 0-0/1',hashlib.sha256(b'x').hexdigest(),b'x')
 def test_interrupted_chunk_does_not_acknowledge(self):
  p=self.begin();self.store.fault=lambda point:(_ for _ in ()).throw(RuntimeError()) if point=='chunk-before-offset' else None
  with self.assertRaises(RuntimeError):self.chunk(p)
  self.store.close();self.store=PhoneStore(self.temp.name)
  self.assertEqual(self.store.uploads(self.actor,p['deliveryId'])['attachments'][0]['receivedBytes'],0);self.chunk(p)
 def test_zero_bytes_and_quota(self):
  p=self.begin(b'');self.assertTrue(p['attachments'][0]['complete']);self.store.quota=1
  self.request=str(uuid.uuid4())
  with self.assertRaises(PhoneError):self.begin()
 def test_chunk_validation_and_path_label(self):
  p=self.begin();a=p['attachments'][0]['attachmentId'];self.assertTrue(Path(self.temp.name,'staging',a).is_file())
  with self.assertRaises(PhoneError):self.store.chunk(self.actor,p['deliveryId'],a,'bytes 0-0/1','0'*64,b'x')
  raw=b'a'*LIMITS['chunkBytes']+b'b';self.request=str(uuid.uuid4());p=self.begin(raw);a=p['attachments'][0]['attachmentId']
  with self.assertRaises(PhoneError) as caught:self.store.chunk(self.actor,p['deliveryId'],a,f"bytes {LIMITS['chunkBytes']}-{len(raw)-1}/{len(raw)}",hashlib.sha256(b'b').hexdigest(),b'b')
  self.assertEqual(caught.exception.extra['expectedOffset'],0)
 def test_missing_catalog_not_reinitialized(self):
  self.begin();self.store.close();os.unlink(Path(self.temp.name,'catalog.sqlite3'));self.store=PhoneStore(self.temp.name)
  with self.assertRaises(PhoneError):self.begin()
 def publish(self):
  p=self.begin();self.chunk(p);return self.store.commit(self.actor,p['deliveryId'],self.request)
 def test_commit_crash_recovery_and_single_event(self):
  p=self.begin();self.chunk(p);self.store.fault=lambda point:(_ for _ in ()).throw(RuntimeError()) if point=='commit-after-files' else None
  with self.assertRaises(RuntimeError):self.store.commit(self.actor,p['deliveryId'],self.request)
  self.assertEqual(self.store.operation(self.actor,self.request)['state'],'uploading')
  self.store.close();self.store=PhoneStore(self.temp.name);first=self.store.commit(self.actor,p['deliveryId'],self.request)
  self.assertEqual(self.store.commit(self.actor,p['deliveryId'],self.request),first)
  with self.store.transaction():self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],1)
 def test_delete_expiry_never_resurrects(self):
  p=self.publish();rid=str(uuid.uuid4());self.store.delete(self.actor,p['deliveryId'],rid);self.store.delete(self.actor,p['deliveryId'],rid)
  with self.assertRaises(PhoneError):self.store.commit(self.actor,p['deliveryId'],self.request)
  self.store.maintain()
  with self.store.transaction():
   self.assertEqual(self.store.usage(),0);self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM events').fetchone()[0],2)
   self.assertEqual(self.store.delivery(p['deliveryId'])['body'],'{}')
  self.request=str(uuid.uuid4());p=self.publish();self.store.clock=lambda:9999999999;self.store.maintain()
  self.assertEqual(self.store.operation(self.actor,self.request)['state'],'expired')
 def test_full_file_hash_failure_stays_hidden(self):
  p=self.begin();self.chunk(p);Path(self.temp.name,'staging',p['attachments'][0]['attachmentId']).write_bytes(b'x'*len(self.data))
  with self.assertRaises(PhoneError):self.store.commit(self.actor,p['deliveryId'],self.request)
  self.assertEqual(self.store.operation(self.actor,self.request)['state'],'uploading')
 def test_message_only_commit_and_expired_draft(self):
  p=self.begin(attachments=[]);self.store.commit(self.actor,p['deliveryId'],self.request)
  self.request=str(uuid.uuid4());p=self.begin();self.store.clock=lambda:9999999999;self.store.maintain()
  self.assertEqual(self.store.operation(self.actor,self.request)['state'],'expired')
 def test_snapshot_feed_and_cursor_isolation(self):
  first=self.publish();self.request=str(uuid.uuid4());self.publish()
  page=self.store.listing(self.actor,'phone-primary',limit=1);self.assertIsNotNone(page['nextCursor'])
  second=self.store.listing(self.actor,'phone-primary',limit=1,cursor=page['nextCursor']);self.assertEqual(second['syncCursor'],page['syncCursor'])
  self.request=str(uuid.uuid4());new=self.publish()
  with self.assertRaises(PhoneError):self.store.listing(self.actor,'phone-primary',limit=1,cursor=page['nextCursor'])
  feed=self.store.changes(self.actor,'phone-primary',page['syncCursor']);self.assertEqual(feed['events'][0]['deliveryId'],new['deliveryId'])
  with self.assertRaises(PhoneError):self.store.changes('other','phone-primary',page['syncCursor'])
  with self.assertRaises(PhoneError):self.store.changes(self.actor,'phone-primary',page['syncCursor']+'x')
  self.store.delete(self.actor,first['deliveryId'],str(uuid.uuid4()));events=self.store.changes(self.actor,'phone-primary',feed['nextCursor'])['events'];self.assertEqual(events[0]['kind'],'deleted');self.assertIsNone(events[0]['summary'])
 def test_ranges_receipts_and_reader_survives_cleanup(self):
  p=self.publish();meta=self.store.metadata(p['deliveryId']);aid=meta['attachments'][0]['id'];self.assertEqual(meta['receipts'],[])
  status,length,headers,fd=self.store.content(p['deliveryId'],aid,'bytes=1-3')
  self.assertEqual((status,length),(206,3));self.assertEqual(os.read(fd,length),self.data[1:4]);os.close(fd)
  status,length,headers,fd=self.store.content(p['deliveryId'],aid,'bytes=1-3','"wrong"');self.assertEqual(status,200);os.close(fd)
  for value in ('bytes=0-1,3-4','bytes=999-','bytes=-0','broken'):
   with self.assertRaises(PhoneError):self.store.content(p['deliveryId'],aid,value)
  devices=[str(uuid.uuid4()),str(uuid.uuid4())]
  for device in devices:self.store.device(self.actor,'phone-primary',device,{'platform':'android','displayName':'Phone','pushToken':None,'notificationsEnabled':True})
  r=self.store.receipt(self.actor,p['deliveryId'],devices[0],{'opened':True});self.assertIsNone(r['downloadedAt']);self.assertIsNotNone(r['openedAt'])
  r=self.store.receipt(self.actor,p['deliveryId'],devices[0],{'downloadedAttachmentIds':[aid]});self.assertIsNotNone(r['downloadedAt'])
  self.assertEqual(r,self.store.receipt(self.actor,p['deliveryId'],devices[0],{'opened':False}))
  self.assertIsNone(self.store.receipt(self.actor,p['deliveryId'],devices[1],{})['downloadedAt'])
  status,length,headers,fd=self.store.content(p['deliveryId'],aid);self.store.delete(self.actor,p['deliveryId'],str(uuid.uuid4()));self.store.maintain()
  self.assertEqual(os.read(fd,length),self.data);os.close(fd)
  with self.assertRaises(PhoneError):self.store.content(p['deliveryId'],aid)
 def test_push_unsupported_and_device_owner(self):
  device=str(uuid.uuid4());args={'platform':'android','displayName':'Phone','pushToken':'not-supported','notificationsEnabled':True}
  with self.assertRaises(PhoneError):self.store.device(self.actor,'phone-primary',device,args)
  args['pushToken']=None;self.store.device(self.actor,'phone-primary',device,args)
  with self.assertRaises(PhoneError):self.store.device('other','phone-primary',device,args)
 def test_expired_cursor_requires_resync(self):
  self.publish();page=self.store.listing(self.actor,'phone-primary');self.store.clock=lambda:9999999999;self.store.maintain()
  with self.assertRaises(PhoneError) as error:self.store.changes(self.actor,'phone-primary',page['syncCursor'])
  self.assertEqual(error.exception.status,410)
