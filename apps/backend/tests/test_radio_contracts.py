import unittest
from archive_backend.config import parse_config
from archive_backend.contracts import BackendError, CAPABILITIES
from archive_backend.radio_contracts import tool_schema, validate
from archive_backend.mcp_adapter import tool_list

class RadioContractsTests(unittest.TestCase):
    def config(self):
        return {'enabled':False,'spool':'/private/radio','roots':[{'driveId':'files','path':'Music'}],
                'stations':[{'id':'radio','receiverId':'station','endpoint':'','secretFile':'/private/key','agentIds':[]}]}
    def test_disabled_and_strict_config(self):
        self.assertIsNone(parse_config({}).radio)
        self.assertFalse(parse_config({'radio':self.config()}).radio['enabled'])
        for endpoint in ['http://127.0.0.1','https://a:b@example.com','https://example.com/path','https://example.com?key=bad']:
            c=self.config();c['stations'][0]['endpoint']=endpoint
            with self.assertRaises(BackendError):parse_config({'radio':c})
    def test_radio_schemas_are_complete_and_strict(self):
        schemas={x['name']:x['inputSchema'] for x in tool_list()}
        for name,cap in CAPABILITIES.items():
            if name.startswith('radio.'):
                self.assertEqual(schemas[name],tool_schema(cap))
                with self.assertRaises(BackendError):validate({'unexpected':1},schemas[name])
        s=schemas['radio.batches.add']
        with self.assertRaises(BackendError):validate({'requestId':'r','batchId':'b','revision':True,'tracks':[]},s)

    def test_published_schemas_match_runtime(self):
        import json
        from pathlib import Path
        schema=json.loads((Path(__file__).resolve().parents[3]/'contracts/radio/mcp-input-schemas.json').read_text())
        expected={name:tool_schema(cap) for name,cap in CAPABILITIES.items() if name.startswith('radio.')}
        self.assertEqual(schema['methods'],expected)
