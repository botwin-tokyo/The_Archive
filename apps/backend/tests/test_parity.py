"""Every registry capability is exercised at both application/JSON-RPC boundaries."""
import unittest
from archive_backend.contracts import CAPABILITIES,BackendError
from archive_backend.mcp_adapter import dispatch
from support import Fixture

class ParityTests(unittest.TestCase):
    def setUp(self): self.f=Fixture(); self.addCleanup(self.f.close)
    def mcp(self,name,args,token=None):
        return dispatch(self.f.service,token or self.f.token,{'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':args}})
    def test_registry_exposes_all_and_denies_revoked_identity(self):
        listing=dispatch(self.f.service,self.f.token,{'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}})
        self.assertEqual({t['name'] for t in listing['result']['tools']},set(CAPABILITIES))
        token='fixture-agent-parity-'+('x'*40)
        agent=self.f.service.auth.register(self.f.owner,'Agent',token,['read','write','trash','purge'],[{'driveId':'files','path':''}]); self.f.service.auth.revoke(self.f.owner,agent.id)
        for name,cap in CAPABILITIES.items():
            with self.subTest(capability=name):
                args={k:None for k in cap.required}
                with self.assertRaises(BackendError) as error: self.f.call(name,args,token)
                with self.assertRaises(BackendError) as mcp_error: self.mcp(name,args,token)
                self.assertEqual(error.exception.code,mcp_error.exception.code)
    def test_valid_read_admin_and_transfer_tools_use_same_result_shapes(self):
        entry=self.f.put(); ref=self.f.ref(entry)
        cases=[('drives.list',{}),('files.list',{'query':{'requestId':'q','location':{'driveId':'files','path':''}}}),('files.stat',{'ref':ref}),('status.get',{}),('jobs.snapshot',{}),('jobs.events',{'after':0}),('trash.list',{'driveId':'files'}),('configuration.get',{}),('configuration.testHealth',{}),('agents.list',{}),('sessions.list',{}),('diagnostics.list',{}),('audit.list',{}),('operations.reconcile',{'requestId':'absent'})]
        for name,args in cases:
            with self.subTest(name=name):
                result=self.f.call(name,args); reply=self.mcp(name,args)
                self.assertIn('result',reply)
                value=reply['result']['structuredContent']
                # MCP wrapper may carry scalar/list result under result by protocol convention.
                if isinstance(value,dict) and set(value)=={'value'}: value=value['value']
                self.assertEqual(type(value),type(result))
        draft=self.f.call('configuration.get'); draft['appearance']['compactRows']=True
        reply=self.mcp('configuration.save',{'draft':draft}); self.assertFalse(reply['result'].get('isError',False))
        request={'requestId':'folder','kind':'new-folder','name':'via-mcp','destination':{'driveId':'files','path':''}}
        reply=self.mcp('operations.submit',{'request':request}); self.assertFalse(reply['result'].get('isError',False)); self.f.service.run_one()
        self.assertTrue((self.f.root/'files/via-mcp').is_dir())
    def test_scope_outcomes_match(self):
        token='fixture-agent-scope-'+('y'*40)
        self.f.service.auth.register(self.f.owner,'Scoped',token,['read'],[{'driveId':'backups','path':''}])
        args={'query':{'requestId':'q','location':{'driveId':'files','path':''}}}
        with self.assertRaises(BackendError) as error: self.f.call('files.list',args,token)
        reply=self.mcp('files.list',args,token)
        self.assertTrue(reply['result']['isError']); self.assertIn(error.exception.code,str(reply))

    def test_mcp_mutation_and_transfer_control_families(self):
        def call(name,args):
            reply=self.mcp(name,args)
            self.assertFalse(reply['result'].get('isError'),reply)
            value=reply['result']['structuredContent']
            return value['value'] if isinstance(value,dict) and set(value)=={'value'} else value
        secret='fixture-mcp-admin-'+('z'*40)
        agent=call('agents.register',{'label':'MCP fixture','secret':secret,'scopes':['read'],'allowedRoots':[{'driveId':'files','path':''}]})
        summary=next(a for a in call('agents.list',{}) if a['id']==agent['id'])
        summary=call('agents.updateScopes',{'id':agent['id'],'revision':summary['revision'],'scopes':['read','write'],'allowedRoots':[{'driveId':'files','path':''}]})
        summary=call('agents.rotate',{'id':agent['id'],'secret':'fixture-mcp-rotated-'+('q'*40)})
        call('agents.revoke',{'id':agent['id'],'revision':summary['revision']})
        session=call('transfers.initiate',{'requestId':'mcp-upload','destination':{'driveId':'files','path':''},'name':'mcp-empty','sizeBytes':'0'})
        call('transfers.query',{'transferId':session['transferId']})
        published=call('transfers.finalize',{'transferId':session['transferId']})
        ref=self.f.ref(published['entry'])
        download=call('transfers.download',{'requestId':'mcp-download','ref':ref})
        call('transfers.downloadProgress',{'transferId':download['transferId'],'offset':'0','complete':True})
        batch=call('transfers.batchDownload',{'requestId':'mcp-batch-download','refs':[ref]})
        call('transfers.downloadProgress',{'transferId':batch['sessions'][0]['transferId'],'offset':'0','complete':True})
        batch=call('transfers.batchInitiate',{'requestId':'mcp-batch','files':[{'destination':{'driveId':'files','path':''},'name':'mcp-partial','sizeBytes':'1'}]})
        tid=batch['sessions'][0]['transferId']
        call('transfers.clientError',{'transferId':tid,'code':'INTERRUPTED'})
        call('transfers.cancel',{'transferId':tid}); call('transfers.discard',{'transferId':tid})
        receipt=call('operations.submit',{'request':{'requestId':'mcp-trash','kind':'trash','targets':[ref]}}); self.f.service.run_one()
        trashed=self.f.ref(call('trash.list',{'driveId':'files'})[0]['entry'])
        grant=call('trash.confirm',{'driveId':'files','targets':[trashed]})
        call('operations.submit',{'request':{'requestId':'mcp-purge','kind':'purge','targets':[trashed],'confirmation':{'grantId':grant['id']}}}); self.f.service.run_one()
        call('jobs.get',{'id':receipt['jobIds'][0]}); call('jobs.hide',{'ids':receipt['jobIds']})
        queued=call('operations.submit',{'request':{'requestId':'mcp-cancel','kind':'new-folder','name':'cancel','destination':{'driveId':'files','path':''}}})
        call('jobs.control',{'id':queued['jobIds'][0],'action':'cancel'})
