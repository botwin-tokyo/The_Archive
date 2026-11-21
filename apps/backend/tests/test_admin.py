import unittest
from unittest.mock import patch
from support import Fixture
from archive_backend.contracts import BackendError

class AdminTests(unittest.TestCase):
    def setUp(self): self.f=Fixture(); self.addCleanup(self.f.close)
    def trash(self,name):
        ref=self.f.ref(self.f.put(name)); self.f.call('operations.submit',{'request':{'requestId':'trash-'+name,'kind':'trash','targets':[ref]}}); self.f.service.run_one()
        return next(self.f.ref(t['entry']) for t in self.f.call('trash.list',{'driveId':'files'}) if t['originalPath']==name)
    def test_empty_trash_binds_snapshot_and_later_arrivals_survive(self):
        first=self.trash('first'); grant=self.f.call('trash.confirm',{'driveId':'files','targets':[],'empty':True}); self.trash('later')
        request={'requestId':'purge','kind':'purge','targets':grant['targets'],'confirmation':{'grantId':grant['id']}}
        self.f.call('operations.submit',{'request':request}); self.f.service.run_one()
        self.assertEqual([t['originalPath'] for t in self.f.call('trash.list',{'driveId':'files'})],['later'])
        request['requestId']='replay'
        with self.assertRaises(BackendError): self.f.call('operations.submit',{'request':request})
    def test_settings_failure_rolls_back_and_revision_guards(self):
        original=self.f.call('configuration.get'); draft=dict(original,transferConcurrency=1)
        with patch.object(self.f.state,'audit',side_effect=BackendError('STATE_UNAVAILABLE')):
            with self.assertRaises(BackendError): self.f.call('configuration.save',{'draft':draft})
        self.assertEqual(self.f.call('configuration.get'),original)
        saved=self.f.call('configuration.save',{'draft':draft}); self.assertTrue(saved['restartRequired'])
        with self.assertRaises(BackendError): self.f.call('configuration.save',{'draft':draft})
    def test_owner_controls_and_independent_agent_rotation(self):
        token='fixture-agent-admin-'+('a'*40)
        agent=self.f.call('agents.register',{'label':'Fixture','secret':token,'scopes':['read'],'allowedRoots':[{'driveId':'files','path':''}]})
        self.assertNotIn(token,str(self.f.call('agents.list')))
        with self.assertRaises(BackendError): self.f.call('configuration.get',token=token)
        replacement='fixture-rotated-admin-'+('b'*40)
        self.f.call('agents.rotate',{'id':agent['id'],'secret':replacement})
        with self.assertRaises(BackendError): self.f.call('status.get',token=token)
        self.assertEqual(self.f.call('status.get',token=replacement)['backend'],'connected')
        self.assertEqual(self.f.call('status.get')['backend'],'connected')
    def test_client_error_is_safe_and_visible(self):
        session=self.f.call('transfers.initiate',{'requestId':'up','destination':{'driveId':'files','path':''},'name':'a','sizeBytes':'10'})
        self.f.call('transfers.clientError',{'transferId':session['transferId'],'code':'INTERRUPTED','message':'SECRET /private/absolute'})
        job=self.f.call('jobs.get',{'id':session['jobId']})
        self.assertEqual(job['error']['code'],'INTERRUPTED'); self.assertNotIn('SECRET',str(job)); self.assertNotIn('/private',str(job))
    def test_purge_revocation_stops_at_child_checkpoint(self):
        fs=self.f.service.filesystems['files']; fs.create_dir('folder'); self.f.put('folder/a'); self.f.put('folder/b')
        folder=self.f.call('files.list',{'query':{'requestId':'q','location':{'driveId':'files','path':''}}})['entries'][0]
        self.f.call('operations.submit',{'request':{'requestId':'trash-folder','kind':'trash','targets':[self.f.ref(folder)]}}); self.f.service.run_one()
        item=self.f.call('trash.list',{'driveId':'files'})[0]; ref=self.f.ref(item['entry'])
        token='fixture-purge-agent-'+('p'*40); agent=self.f.service.auth.register(self.f.owner,'Purger',token,['read','purge'],[{'driveId':'files','path':''}])
        grant=self.f.call('trash.confirm',{'driveId':'files','targets':[ref]},token)
        self.f.call('operations.submit',{'request':{'requestId':'purge-folder','kind':'purge','targets':[ref],'confirmation':{'grantId':grant['id']}}},token)
        original=fs._unlink; removed=[]
        def unlink(*args,**kwargs):
            result=original(*args,**kwargs); removed.append(args[0])
            if len(removed)==1: self.f.service.auth.revoke(self.f.owner,agent.id)
            return result
        with patch.object(fs,'_unlink',side_effect=unlink):
            with self.assertRaises(BackendError): self.f.service.run_one()
        self.assertEqual(len(removed),1)
        self.assertTrue(fs.internal_stat('trash',ref['id']).is_dir)
        self.assertEqual(self.f.call('files.list',{'query':{'requestId':'other','location':{'driveId':'backups','path':''}}})['total'],0)
