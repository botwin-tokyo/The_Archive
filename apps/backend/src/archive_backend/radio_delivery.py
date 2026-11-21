"""Single-flight station dispatcher with fenced durable attempts and receipt recovery."""
import json
import random
import threading
import time
import uuid
from .contracts import BackendError
from .radio_receiver_client import ReceiverClient,ReceiverError
from .radio_sources import prepare,open_payload,cleanup


class RadioDispatcher:
    def __init__(self,radio,*,client_factory=ReceiverClient,clock=time.time):
        self.radio=radio;self.state=radio.state;self.client_factory=client_factory;self.clock=clock
        self.owner=str(uuid.uuid4());self.next_station=0;self.stop=threading.Event();self.thread=None

    def start(self):
        self.thread=threading.Thread(target=self.run,name='archive-radio-delivery',daemon=True);self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=45)
            if self.thread.is_alive():raise RuntimeError('Radio worker did not stop; state must remain open')

    def run(self):
        while not self.stop.is_set():
            try:self.tick()
            except Exception:
                self.radio.service.observability.record('RADIO_WORKER_ERROR',level='warning')
            self.stop.wait(1)

    def claim(self,station):
        with self.state.transaction() as c:
            row=c.execute('SELECT owner,fence,expires FROM radio_leases WHERE station=?',(station,)).fetchone()
            if row and row[2]>self.clock():return None
            b=c.execute("SELECT body FROM radio_batches INDEXED BY radio_pending_station WHERE station=? AND state IN ('queued','preparing','sending','pausing','canceling') ORDER BY created,id LIMIT 1",(station,)).fetchone()
            if not b:return None
            fence=(row[1] if row else 0)+1
            c.execute('INSERT INTO radio_leases VALUES(?,?,?,?) ON CONFLICT(station) DO UPDATE SET owner=excluded.owner,fence=excluded.fence,expires=excluded.expires',(station,self.owner,fence,self.clock()+45))
            return json.loads(b[0]),fence

    def fenced(self,station,fence):
        row=self.state.connection.execute('SELECT owner,fence,expires FROM radio_leases WHERE station=?',(station,)).fetchone()
        if not row or row[0]!=self.owner or row[1]!=fence or row[2]<=self.clock():raise BackendError('INTERRUPTED','Radio worker lease was superseded')

    def tick(self):
        if not self.radio.config or not self.radio.config['enabled']:return
        self.clean_terminal()
        stations=self.radio.config['stations']
        for offset in range(len(stations)):
            index=(self.next_station+offset)%len(stations);station=stations[index]
            if not station['endpoint']:continue
            claimed=self.claim(station['id'])
            if not claimed:continue
            b,fence=claimed
            self.next_station=(index+1)%len(stations)
            try:self.deliver(station,b,fence)
            finally:
                with self.state.transaction() as c:
                    c.execute('UPDATE radio_leases SET expires=0 WHERE station=? AND owner=? AND fence=?',(station['id'],self.owner,fence))
            return  # One item per tick; HTTP never holds a DB writer transaction.

    def clean_terminal(self):
        with self.state._lock:
            rows=self.state.connection.execute("SELECT body FROM radio_batches INDEXED BY radio_cleanup WHERE state IN ('completed','completed-with-errors','canceled') AND COALESCE(json_extract(body,'$._spoolCleaned'),0)=0 LIMIT 4").fetchall()
        for row in rows:
            b=json.loads(row[0])
            for item in self.radio.items(b['id']):
                if item.get('_manifest'):cleanup(self.radio.config,item)
            with self.state.transaction():
                b=self.radio.batch(b['id']);b['_spoolCleaned']=True;self.radio.save(b)

    def deliver(self,station,b,fence):
        radio=self.radio;bid=b['id'];sid=station['id'];last=[0.0];current=[None]
        def checkpoint(sent=None,*,intent=True):
            if self.stop.is_set():raise ReceiverError('UNAVAILABLE','Radio service is stopping',True)
            fresh=radio.batch(bid);p=radio.principal(fresh);radio.station(p,sid)
            if current[0]:radio.source(p,current[0]['source'],check=False)
            if fresh.get('_receiverId',station['receiverId'])!=station['receiverId']:raise BackendError('CONFLICT','Configured receiver identity changed')
            with self.state.transaction() as c:
                self.fenced(sid,fence)
                if self.clock()-last[0]>=2:
                    c.execute('UPDATE radio_leases SET expires=? WHERE station=? AND owner=? AND fence=?',(self.clock()+45,sid,self.owner,fence));last[0]=self.clock()
                    if current[0] and sent is not None:
                        current[0]['uploadedBytes']=str(sent);current[0]['lastProgressAt']=self.clock();radio.save_item(bid,current[0])
            if intent and fresh['state'] in ('pausing','canceling'):raise ReceiverError('INTERRUPTED','Requested radio control is settling',True)
            return p
        item=None
        try:
            items=radio.items(bid)
            item=next((i for i in items if i['state'] not in ('delivered','canceled')),None)
            if item is None:
                with self.state.transaction():
                    self.fenced(sid,fence);b=radio.batch(bid)
                    b['state']='canceled' if b['state']=='canceling' else 'completed-with-errors' if any(i['state']=='canceled' for i in items) else 'completed'
                    radio.save(b);radio.event(b,'finished')
                for i in items:
                    if i.get('_manifest'):cleanup(radio.config,i)
                return
            current[0]=item
            if (item.get('retryAt') or 0)>self.clock():return
            p=checkpoint(intent=False)
            # Configured receiver identity is frozen at first attempt and checked
            # again on every subsequent attempt. No silent station replacement.
            with self.state.transaction():
                self.fenced(sid,fence);b=radio.batch(bid)
                b.setdefault('_receiverId',station['receiverId'])
                if b['state'] in ('pausing','canceling') and not item.get('_uncertain'):
                    self.settle_control(b,item);return
                item['attempts']+=1;item.setdefault('firstAttemptAt',self.clock());item['retryAt']=None
                item['state']='reconciling' if item.get('_manifest') else 'preparing'
                if b['state'] not in ('pausing','canceling'):b['state']='preparing'
                radio.save_item(bid,item);radio.save(b)
            if not item.get('_manifest'):
                item['_manifest']=prepare(radio,p,item,lambda:checkpoint())
                item['sizeBytes']=item['_manifest']['sizeBytes'];item['sha256']=item['_manifest']['sha256']
                with self.state.transaction():self.fenced(sid,fence);radio.save_item(bid,item)
            client=self.client_factory(station);cap=client.capabilities();checkpoint(intent=False)
            if int(item['_manifest']['sizeBytes'])>int(cap['maxUploadBytes']):raise BackendError('QUOTA_EXCEEDED','Receiver upload size limit is smaller than this MP3')
            receipt=client.lookup(item);checkpoint(intent=False)
            with self.state.transaction():
                self.fenced(sid,fence);b=radio.batch(bid)
                if receipt is None and b['state'] in ('pausing','canceling'):
                    item['_uncertain']=False;self.settle_control(b,item);return
            if receipt is None:
                # A definitive absent lookup clears an older uncertain attempt.
                # Validate the spool BEFORE marking a new network send uncertain.
                item['_uncertain']=False
                with self.state.transaction():self.fenced(sid,fence);radio.save_item(bid,item)
                with open_payload(radio.config,item,lambda:checkpoint()) as payload:
                    checkpoint()
                    with self.state.transaction():
                        self.fenced(sid,fence);b=radio.batch(bid)
                        if b['state'] in ('pausing','canceling'):self.settle_control(b,item);return
                        item['_uncertain']=True;item['state']='sending';b['state']='sending'
                        radio.save_item(bid,item);radio.save(b)
                    receipt=client.upload(item,payload,checkpoint)
            # Receipt may have arrived after pause/cancel. Persist truth first.
            with self.state.transaction():
                self.fenced(sid,fence);b=radio.batch(bid);item['state']='delivered';item['receipt']=receipt;item['_uncertain']=False;item['error']=None;item['uploadedBytes']=item['_manifest']['sizeBytes']
                radio.save_item(bid,item);b['deliveredCount']+=1;b['confirmedBytes']=str(int(b['confirmedBytes'])+int(receipt['sizeBytes']));b['error']=None
                if b['state'] in ('pausing','canceling'):self.settle_control(b,item)
                else:b['state']='queued';radio.save(b);radio.event(b,'delivered')
            cleanup(radio.config,item)
        except Exception as exc:
            error=exc if isinstance(exc,BackendError) else BackendError('INTERNAL_ERROR','Radio delivery failed safely')
            with self.state.transaction():
                self.fenced(sid,fence);b=radio.batch(bid)
                if item and item['state']=='delivered':return  # Receipt persisted; cleanup can retry later.
                retry=bool(error.retryable and item and item.get('attempts',0)<8 and self.clock()-item.get('firstAttemptAt',self.clock())<86400)
                b['error']=error.envelope()
                if item:
                    item['error']=error.envelope();item['state']='retry-wait' if retry else 'failed'
                    delay=max(getattr(error,'retry_after',0),min(300,2**min(item.get('attempts',1),8))+random.uniform(0,1))
                    item['retryAt']=self.clock()+delay if retry else None
                    radio.save_item(bid,item)
                if not retry:b['state']='blocked'
                elif b['state'] not in ('pausing','canceling'):b['state']='queued'
                radio.save(b);radio.event(b,'retry' if retry else 'blocked')

    def settle_control(self,b,item):
        radio=self.radio
        if b['state']=='canceling':
            for other in radio.items(b['id']):
                if other['state']!='delivered':other['state']='canceled';other['_uncertain']=False;radio.save_item(b['id'],other)
            b['state']='canceled'
        else:
            if item['state']!='delivered':item['state']='ready' if item.get('_manifest') else 'pending';radio.save_item(b['id'],item)
            b['state']='paused'
        radio.save(b);radio.event(b,'control-settled')
