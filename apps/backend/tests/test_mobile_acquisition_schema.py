import json
from pathlib import Path
import unittest
import test_acquisition_auth as authority
from archive_backend.mobile_acquisitions import METHODS,input_schema
try:
    import jsonschema
except ImportError:
    jsonschema=None


@unittest.skipUnless(jsonschema,'Install jsonschema in the local test environment for contract qualification')
class MobileAcquisitionSchemaTests(unittest.TestCase):
    def test_generated_inputs_match_deployed_definitions(self):
        doc=json.loads(Path('contracts/mobile/acquisition-api.schema.json').read_text())
        for method in METHODS:
            self.assertEqual(doc['methods'][method]['request'],input_schema(method))
            jsonschema.Draft202012Validator.check_schema(doc['methods'][method]['response'])

    def test_actual_discovery_storage_and_acquisition_responses(self):
        fixture=authority.AcquisitionAuthorityTests()
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        api=fixture.boundary.acquisitions;context=fixture.context
        doc=json.loads(Path('contracts/mobile/acquisition-api.schema.json').read_text())
        jsonschema.validate(api.discovery(context),doc['discovery'])
        jsonschema.validate(api.storage(context),doc['storage'])
        def call(method,args):
            result=api.call(context,{'method':method,'params':args})
            jsonschema.validate(result,doc['methods'][method]['response'])
            return result
        for prefix in ('video','music'):
            call(prefix+'.status',{})
            args={'url':authority.URL} if prefix=='video' else {'selection':{'kind':'album','id':'c'*22}}
            plan=call(prefix+'.resolve',{'requestId':'schema',**args})
            getattr(fixture,prefix).tick()
            plan=call(prefix+'.plan.get',{'id':plan['id']})
            args={'requestId':'schema-enqueue','planId':plan['id'],'planRevision':plan['revision'],'destination':{'driveId':'backups','path':''}}
            if prefix=='video':args['choiceId']='video-720'
            job=call(prefix+'.enqueue',args)
            call('acquisitions.reconcile',{'method':prefix+'.enqueue','requestId':'schema-enqueue'})
            call(prefix+'.jobs.get',{'id':job['id']});call(prefix+'.jobs.list',{})
            call(prefix+'.jobs.control',{'id':job['id'],'revision':job['revision'],'action':'cancel'})
        search=call('music.search',{'requestId':'search-schema','query':'test','kind':'track'})
        fixture.music.tick();call('music.search.get',{'id':search['id']})
        call('music.artwork.get',{'id':plan['id']})
