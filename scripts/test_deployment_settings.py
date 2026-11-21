"""Deployment configuration must fail closed before touching host services."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import deployment_settings as settings


class DeploymentSettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'local.json'
        settings._SETTINGS = None
        self.addCleanup(setattr, settings, '_SETTINGS', None)
        self.environment = patch.dict('os.environ', {'ARCHIVE_DEPLOYMENT_CONFIG': str(self.path)})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def config(self, value):
        settings._SETTINGS = None
        self.path.write_text(json.dumps(value))

    def test_missing_configuration_and_missing_identity_fail_closed(self):
        with self.assertRaises(SystemExit):
            settings.configured('@RUNTIME_ROOT@/service')
        self.config({'RUNTIME_ROOT': '/srv/example', 'GIT_UUID': ''})
        with self.assertRaises(SystemExit):
            settings.configured('@GIT_UUID@')

    def test_paths_cannot_inject_unit_directives_or_escape_components(self):
        for value in ('/', '/srv/../etc', '/srv//data', '/srv/data\nUser=root', '/srv/%u'):
            self.config({'RUNTIME_ROOT': value})
            with self.assertRaises(SystemExit):
                settings.configured('@RUNTIME_ROOT@/service')

    def test_template_and_script_share_operator_paths(self):
        self.config({'RUNTIME_ROOT': '/srv/example', 'GIT_MOUNT': '/mnt/example-git'})
        source = Path(self.directory.name) / 'example.service'
        destination = Path(self.directory.name) / 'rendered.service'
        source.write_text('[Unit]\nRequiresMountsFor=@GIT_MOUNT@\n[Service]\nWorkingDirectory=@RUNTIME_ROOT@/service\n')
        settings.install_template(source, destination)
        self.assertEqual(destination.read_text(), '[Unit]\nRequiresMountsFor=/mnt/example-git\n[Service]\nWorkingDirectory=/srv/example/service\n')
        self.assertEqual(settings.configured('@RUNTIME_ROOT@/service'), '/srv/example/service')

    def test_all_service_templates_resolve_with_one_operator_configuration(self):
        root = Path(__file__).resolve().parents[1]
        values = json.loads((root / 'deploy/local.example.json').read_text())
        values.update(DATA_UUID='11111111-1111-4111-8111-111111111111',
                      GIT_UUID='22222222-2222-4222-8222-222222222222',
                      FLEET_ENDPOINT='wss://monitor.example.com/v1/ingest/ws',
                      FLEET_NODE_ID='server-01')
        self.config(values)
        for template in (root / 'deploy/systemd').iterdir():
            with self.subTest(template=template.name):
                self.assertIsNone(settings._TOKEN.search(settings.configured(template.read_text())))
        core = settings.configured((root / 'deploy/systemd/archive-console.service').read_text())
        self.assertIn('WorkingDirectory=' + values['RUNTIME_ROOT'] + '/archive-console-service', core)

    def test_fleet_identity_and_credential_free_endpoint(self):
        self.config({'FLEET_ENDPOINT': 'wss://monitor.example.com/v1/ingest/ws', 'FLEET_NODE_ID': 'server-01'})
        self.assertEqual(settings.configured('@FLEET_NODE_ID@'), 'server-01')
        self.assertTrue(settings.configured('@FLEET_ENDPOINT@').startswith('wss://'))
        for endpoint in ('http://monitor.example.com', 'wss://u:p@monitor.example.com', 'wss://monitor.example.com/?token=secret'):
            self.config({'FLEET_ENDPOINT': endpoint})
            with self.assertRaises(SystemExit):
                settings.configured('@FLEET_ENDPOINT@')


if __name__ == '__main__':
    unittest.main()
