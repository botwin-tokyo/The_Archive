import base64
import hashlib
import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from support import Fixture
from archive_backend.skills_store import SkillStore
from archive_backend.contracts import BackendError
from archive_backend.http_adapter import create_server
from archive_backend.mcp_adapter import dispatch, tool_list


class SkillsServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.f = Fixture()
        self.f.service.skills.store = SkillStore(self.tmp.name)
        self.token = 'skills-full-test-'+('x'*40)
        self.agent = self.f.service.auth.register(self.f.owner, 'skills', self.token, ['skills.read', 'skills.write'], [])
        self.http = create_server(self.f.service, '127.0.0.1', 0)
        self.thread = threading.Thread(target=self.http.serve_forever); self.thread.start()

    def tearDown(self):
        self.http.shutdown(); self.http.server_close(); self.thread.join(); self.f.close(); self.tmp.cleanup()

    def call(self, op, args=None, token=None):
        return self.f.call('skills.'+op, args or {}, token or self.token)

    def put(self):
        return self.call('put', {'requestId': 'put', 'slug': 'sample', 'textFiles': [{'path': 'SKILL.md', 'text': '# Hello\n\n Exact Ω \n'}, {'path': 'references/a.txt', 'text': 'data'}]})

    def request(self, method, path, value=None, raw=None):
        headers = {'Authorization': 'Bearer '+self.token, 'Content-Type': 'application/octet-stream' if raw is not None else 'application/json', 'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-11-25'}
        req = urllib.request.Request('http://127.0.0.1:'+str(self.http.server_port)+path, data=raw if raw is not None else json.dumps(value).encode() if value is not None else None, headers=headers, method=method)
        return urllib.request.urlopen(req)

    def test_put_read_mcp_and_auth(self):
        e = self.put(); self.assertEqual(e, self.put())
        self.assertEqual(self.call('document', {'id': e['id']})['text'], '# Hello\n\n Exact Ω \n')
        file = self.call('fileRead', {'id': e['id'], 'path': 'references/a.txt', 'length': 2})
        self.assertEqual(base64.b64decode(file['data']), b'da'); self.assertEqual(file['nextOffset'], 2)
        result = dispatch(self.f.service, self.token, {'jsonrpc': '2.0', 'id': 1, 'method': 'resources/read', 'params': {'uri': 'archive://skills/skill-library'}})
        self.assertIn('skills.put', result['result']['contents'][0]['text'])
        self.assertTrue(any(t['name'] == 'skills.put' for t in tool_list()))
        read = 'skills-read-test-'+('z'*40); a = self.f.service.auth.register(self.f.owner, 'read', read, ['skills.read'], [])
        self.assertEqual(len(self.call('list', token=read)['items']), 1)
        with self.assertRaises(BackendError): self.call('trash', {'id': e['id'], 'expectedRevision': 1, 'requestId': 'no'}, read)
        self.f.service.auth.revoke(self.f.owner, a.id)
        with self.assertRaises(BackendError): self.call('list', token=read)
        with self.assertRaises(BackendError): self.f.call('vault.entries.list', {}, self.token)
        self.assertNotIn('Exact Ω', str(self.f.state.connection.execute('SELECT * FROM audit').fetchall()))

    def test_http_binary_and_discovery(self):
        values = {'SKILL.md': b'# Binary\n', 'assets/file.bin': bytes(range(256))*12288}
        mf = [{'path': p, 'size': len(v), 'sha256': hashlib.sha256(v).hexdigest()} for p, v in values.items()]
        with self.request('POST', '/skills/v1/uploads', {'requestId': 'binary', 'slug': 'binary', 'files': mf}) as response: s = json.load(response)['result']
        for path, data in values.items():
            for offset in range(0, len(data), 1048576):
                with self.request('PUT', '/skills/v1/uploads/'+s['uploadId']+'/files?path='+path+'&offset='+str(offset), raw=data[offset:offset+1048576]) as response: self.assertEqual(response.status, 200)
        with self.request('POST', '/skills/v1/uploads/'+s['uploadId']+'/publish', {'requestId': 'pub'}) as response: e = json.load(response)['result']
        with self.request('GET', '/skills/v1/skills/'+e['id']+'/files?path=assets/file.bin&revision=1') as response:
            self.assertEqual(response.headers['Content-Disposition'], 'attachment'); self.assertEqual(response.read(), values['assets/file.bin'])
        with self.request('GET', '/skills/v1/skills?query=binary&limit=1') as response: self.assertEqual(len(json.load(response)['result']['items']), 1)
        with self.request('POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'skills.document', 'arguments': {'id': e['id']}}}) as response: self.assertEqual(json.load(response)['result']['structuredContent']['text'], '# Binary\n')

    def test_invalid_fields_limits_and_query(self):
        with self.assertRaises(BackendError): self.call('list', {'limit': True})
        with self.assertRaises(BackendError): self.call('put', {'requestId': 'bad', 'slug': 'sample', 'textFiles': [{'path': 'SKILL.md', 'text': 'x'*131073}]})
        for url in ('/skills/v1/skills?limit=1&limit=2', '/skills/v1/skills?trashed=yes', '/skills/v1/skills?unknown=1'):
            with self.assertRaises(urllib.error.HTTPError) as error: self.request('GET', url)
            self.assertEqual(error.exception.code, 400)

    def test_boot_config_rejects_managed_root_overlap(self):
        from archive_backend.skills_contracts import parse_config
        volumes = [{'id': 'files', 'rootPath': '/mnt/drive/archive', 'mountPoint': '/mnt/drive'}]
        for root in ('/mnt/drive/archive/Skills', '/mnt/drive/archive', '/mnt/drive', '/tmp/Skills'):
            with self.assertRaises(BackendError): parse_config({'enabled': True, 'volumeId': 'files', 'root': root}, volumes)
        self.assertEqual(parse_config({'enabled': True, 'volumeId': 'files', 'root': '/mnt/drive/Skills'}, volumes)['root'], '/mnt/drive/Skills')

    def test_large_escaped_document_transport(self):
        raw = b'\x00'*400000
        s = self.call('uploadBegin', {'requestId': 'escaped', 'slug': 'escaped', 'files': [{'path': 'SKILL.md', 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}]})
        with self.request('PUT', '/skills/v1/uploads/'+s['uploadId']+'/files?path=SKILL.md&offset=0', raw=raw): pass
        e = self.call('uploadPublish', {'requestId': 'escaped-publish', 'uploadId': s['uploadId']})
        for url, body in [('/skills/v1/skills/'+e['id']+'/document', None), ('/v1/call', {'version': 1, 'capability': 'skills.document', 'arguments': {'id': e['id']}})]:
            with self.request('GET' if body is None else 'POST', url, body) as response: self.assertEqual(json.load(response)['result']['text'].encode(), raw)
