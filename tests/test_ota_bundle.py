import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import subprocess
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ota', ROOT / 'sentry-ota.py')
ota = importlib.util.module_from_spec(spec); spec.loader.exec_module(ota)


class BundleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'install'; self.root.mkdir()
        (self.root / '.ota').mkdir()
        self.stage = Path(self.tmp.name) / 'stage'; self.stage.mkdir()
        files = {'sentry-viewer.py': b'APP_VERSION = "9.0.0"\nprint("ok")\n',
                 'sentry-cloud-sync.py': b'print("uploader help")\n',
                 'sentry-ota.py': (ROOT / 'sentry-ota.py').read_bytes(),
                 'scripts/install-cloud-sync.sh': b'#!/bin/sh\nexit 0\n'}
        for name, data in files.items():
            path = self.stage / name; path.parent.mkdir(exist_ok=True); path.write_bytes(data)
            old = self.root / name; old.parent.mkdir(exist_ok=True); old.write_bytes(b'old-' + name.encode())
        self.manifest = {'version': '9.0.0', 'bundle_format': 1,
                         'files': {n: hashlib.sha256(d).hexdigest() for n, d in files.items()}}
        (self.stage / 'version.json').write_text(json.dumps(self.manifest))
        (self.root / 'cloud.json').write_bytes(b'PRIVATE DEVICE CREDENTIALS')
        (self.root / 'queue.sqlite').write_bytes(b'PENDING TELEMETRY')
        self.unit = Path(self.tmp.name) / 'cloud.service'; self.unit.write_bytes(b'old unit')
        self.recover_unit = Path(self.tmp.name) / 'recovery.service'
        for patch in (mock.patch.object(ota, 'UNIT', self.unit), mock.patch.object(ota, 'RECOVERY', self.recover_unit),
                      mock.patch.object(ota, 'systemctl'), mock.patch.object(ota, 'active', return_value=True)):
            patch.start(); self.addCleanup(patch.stop)

    def intact_user_data(self):
        self.assertEqual((self.root / 'cloud.json').read_bytes(), b'PRIVATE DEVICE CREDENTIALS')
        self.assertEqual((self.root / 'queue.sqlite').read_bytes(), b'PENDING TELEMETRY')

    def assert_originals(self):
        for name in ota.FILES:
            self.assertEqual((self.root / name).read_bytes(), b'old-' + name.encode())
        self.assertEqual(self.unit.read_bytes(), b'old unit')
        self.intact_user_data()

    def test_checksum_and_file_allowlist_reject_before_modification(self):
        (self.stage / 'sentry-cloud-sync.py').write_bytes(b'print("tampered")')
        with self.assertRaises(ValueError): ota.install(self.root, self.stage, 8080)
        self.assert_originals()
        self.manifest['files']['cloud.json'] = '0' * 64
        (self.stage / 'version.json').write_text(json.dumps(self.manifest))
        with self.assertRaises(ValueError): ota.validate(self.stage)

    def test_install_and_manual_rollback_restore_entire_bundle(self):
        with mock.patch.object(ota, 'health'):
            ota.install(self.root, self.stage, 8080)
        for name in ota.FILES:
            self.assertEqual((self.root / name).read_bytes(), (self.stage / name).read_bytes())
        journal = json.loads((self.root / '.ota/journal.json').read_text())
        self.assertEqual(journal['phase'], 'complete'); self.intact_user_data()
        ota.restore(self.root, journal)
        self.assert_originals()

    def test_failed_health_check_automatically_restores_all_files(self):
        with mock.patch.object(ota, 'health', side_effect=RuntimeError('not healthy')):
            with self.assertRaises(RuntimeError): ota.install(self.root, self.stage, 8080)
        self.assert_originals()
        self.assertEqual(json.loads((self.root / '.ota/journal.json').read_text())['phase'], 'rolled_back')

    def test_stopped_uploader_stays_stopped(self):
        with mock.patch.object(ota, 'active', return_value=False), mock.patch.object(ota, 'health') as health:
            ota.install(self.root, self.stage, 8080)
        health.assert_called_once_with(8080, False)
        self.assertNotIn(mock.call('start', 'sentry-cloud-sync.service'), ota.systemctl.call_args_list)

    def test_boot_recovers_interrupted_multi_file_replacement(self):
        original = ota.atomic
        def power_cut(path, *args, **kwargs):
            if Path(path) == self.root / 'sentry-cloud-sync.py': raise SystemExit('power cut')
            return original(path, *args, **kwargs)
        with mock.patch.object(ota, 'atomic', side_effect=power_cut):
            with self.assertRaises(SystemExit): ota.install(self.root, self.stage, 8080)
        self.assertEqual((self.root / 'sentry-viewer.py').read_bytes(), (self.stage / 'sentry-viewer.py').read_bytes())
        with mock.patch('sys.argv', ['sentry-ota.py', '--root', str(self.root), '--recover']), mock.patch.object(ota.os, 'geteuid', return_value=0):
            self.assertEqual(ota.main(), 0)
        self.assert_originals()

    def test_same_version_bootstrap_repairs_missing_or_modified_components(self):
        with mock.patch.dict('os.environ', {'TSF_CACHE': str(self.root / 'cache')}):
            spec = importlib.util.spec_from_file_location('bundle_viewer', ROOT / 'sentry-viewer.py')
            viewer = importlib.util.module_from_spec(spec); spec.loader.exec_module(viewer)
        viewer.__file__ = str(self.root / 'sentry-viewer.py')
        viewer.APP_VERSION = '9.0.0'
        manifest = dict(self.manifest, _base='https://example.com')
        with mock.patch.object(viewer, 'ota_latest', return_value=manifest):
            self.assertTrue(viewer.ota_check()['has_update'])
            with mock.patch.object(ota, 'health'): ota.install(self.root, self.stage, 8080)
            self.assertFalse(viewer.ota_check()['has_update'])
            self.assertFalse(viewer.ota_apply()[0])
            (self.root / 'sentry-cloud-sync.py').write_bytes(b'modified')
            self.assertTrue(viewer.ota_check()['has_update'])

    def test_installer_renders_systemd_credential_variable_without_shell_expansion(self):
        script = (ROOT / 'scripts/install-cloud-sync.sh').read_text()
        unit = script.split('cat > /etc/systemd/system/sentry-cloud-sync.service <<EOF\n')[1].split('\nEOF')[0]
        shell = 'TSF_CLOUD_DIR=/opt/tesla-sentry\nTSF_PYTHON=/usr/bin/python3\ncat <<EOF\n' + unit + '\nEOF\n'
        result = subprocess.run(['sh', '-eu'], input=shell, capture_output=True, text=True, check=True)
        self.assertIn('--config ${CREDENTIALS_DIRECTORY}/cloud.json', result.stdout)


if __name__ == '__main__': unittest.main()
