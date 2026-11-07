import contextlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('configure_frp', ROOT / 'tools/configure_frp.py')
frp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(frp)


def valid_parameters():
    return dict(server_addr='frp.example.com', server_port=7000,
                remote_port=18080, device_id='pi-zero-01', token='a' * 64)


class ParametersTest(unittest.TestCase):
    def test_rejects_out_of_range_ports_and_coercion(self):
        for key in ('server_port', 'remote_port', 'local_port'):
            for value in (0, 65536, True, '7000', 1.5):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    frp.parameters(dict(valid_parameters(), **{key: value}))

    def test_rejects_untrusted_options_and_missing_fields(self):
        for extra in ({'local_ip': '10.0.0.1'}, {'includes': ['/tmp/extra.toml']}, {'command': 'id'}):
            with self.assertRaises(ValueError):
                frp.parameters(dict(valid_parameters(), **extra))
        p = valid_parameters()
        del p['token']
        with self.assertRaises(ValueError):
            frp.parameters(p)

    def test_rejects_host_and_proxy_name_injection(self):
        for key, values in {
            'server_addr': ['https://frp.example.com', 'host\n[[proxies]]', 'a b', ''],
            'server_name': ['a"\nb = 1', '*'],
            'device_id': ['../other', 'x\ncommand', '', 'x' * 65],
            'token': ['short', 'a' * 32 + '\n', 'a' * 32 + ' ', 'REPLACE_' + 'x' * 64,
                      'a' * 32 + '{{ .Envs.SECRET }}'],
        }.items():
            for value in values:
                with self.subTest(key=key), self.assertRaises(ValueError):
                    frp.parameters(dict(valid_parameters(), **{key: value}))

    def test_token_escaping_cannot_add_configuration(self):
        token = 'x' * 32 + '"\\abc'
        text = frp.render(frp.parameters(dict(valid_parameters(), token=token)), '/tmp/ca.crt')
        line = next(line for line in text.splitlines() if line.startswith('auth.token = '))
        self.assertEqual(json.loads(line.split(' = ', 1)[1]), token)
        self.assertEqual(text.count('[[proxies]]'), 1)
        self.assertIn('localIP = "127.0.0.1"', text)
        self.assertIn('transport.tls.trustedCaFile', text)


class RenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cert_tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.cert_tmp.cleanup)
        cert = Path(cls.cert_tmp.name) / 'ca.crt'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(Path(cls.cert_tmp.name) / 'test.key'), '-out', str(cert),
                        '-days', '1', '-subj', '/CN=frp-test'],
                       check=True, capture_output=True)
        cls.ca_bytes = cert.read_bytes()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.parameters = self.root / 'parameters.json'
        self.parameters.write_text(json.dumps(valid_parameters()))
        self.ca = self.root / 'ca.crt'
        self.ca.write_bytes(self.ca_bytes)
        self.output = self.root / 'output'

    def run_cli(self, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = frp.main(['--parameters', str(self.parameters), '--ca', str(self.ca),
                               '--output', str(self.output), *extra])
        return result, stdout.getvalue() + stderr.getvalue()

    def test_render_is_offline_and_secret_file_is_private(self):
        with mock.patch.object(frp.subprocess, 'run', side_effect=AssertionError('no subprocess expected')):
            result, output = self.run_cli()
        self.assertEqual(result, 0, output)
        config = self.output / 'frpc.toml'
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
        self.assertIn(str(self.output / 'ca.crt'), config.read_text())
        self.assertIn('LoadCredential=', (self.output / frp.UNIT.name).read_text())
        self.assertNotIn(valid_parameters()['token'], output)
        # Reconfiguration also repairs overly broad permissions on an existing file.
        config.chmod(0o644)
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)

    def test_bad_ca_fails_without_writing_configuration(self):
        self.ca.write_text('not a certificate')
        result, output = self.run_cli()
        self.assertEqual(result, 1)
        self.assertFalse(self.output.exists())
        self.assertNotIn(valid_parameters()['token'], output)

    def test_frpc_diagnostic_is_not_leaked(self):
        failed = subprocess.CalledProcessError(1, 'frpc', stderr=valid_parameters()['token'].encode())
        with mock.patch.object(frp.subprocess, 'run', side_effect=failed):
            result, output = self.run_cli('--frpc', '/tmp/frpc')
        self.assertEqual(result, 1)
        self.assertNotIn(valid_parameters()['token'], output)
        self.assertFalse(self.output.exists())

    def test_incompatible_binary_rejected(self):
        for version in (b'0.63.0\n', b'unknown\n'):
            with mock.patch.object(frp.subprocess, 'run', return_value=mock.Mock(stdout=version)):
                self.assertEqual(self.run_cli('--frpc', '/tmp/frpc')[0], 1)
        self.assertFalse(self.output.exists())

    def test_valid_binary_is_asked_to_verify_before_output(self):
        def execute(command, **kwargs):
            if '--version' in command:
                return mock.Mock(stdout=b'0.64.0\n')
            self.assertEqual(command[1:3], ['verify', '-c'])
            config = Path(command[3]).read_text()
            self.assertIn('serverAddr = "frp.example.com"', config)
            self.assertFalse(self.output.exists())
            return mock.Mock(stdout=b'configuration is valid\n')
        with mock.patch.object(frp.subprocess, 'run', side_effect=execute) as run:
            self.assertEqual(self.run_cli('--frpc', '/tmp/frpc')[0], 0)
            self.assertEqual(run.call_count, 2)


if __name__ == '__main__':
    unittest.main()
