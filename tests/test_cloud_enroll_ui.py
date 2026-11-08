import importlib.util
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('enroll_viewer', ROOT / 'sentry-viewer.py')
viewer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(viewer)


class CloudEnrollTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.config, self.uploader, self.installer = (root / 'cloud.json', root / 'sentry-cloud-sync.py', root / 'install.sh')
        self.uploader.write_text('# uploader')
        self.installer.write_text('# installer')
        for patch in (
            mock.patch.object(viewer, 'AUTH_ON', True),
            mock.patch.object(viewer.os, 'geteuid', return_value=0),
            mock.patch.object(viewer, 'cloud_enroll_paths', return_value=(str(self.config), str(self.uploader), str(self.installer))),
            mock.patch.object(viewer.os.path, 'isdir', return_value=True),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def test_invalid_or_existing_binding_never_calls_cloud(self):
        with mock.patch.object(viewer.importlib.util, 'spec_from_file_location') as load:
            self.assertEqual(viewer.cloud_enroll_once('short')[0], 400)
            self.config.write_text('existing')
            self.assertEqual(viewer.cloud_enroll_once('a' * 40)[0], 409)
            load.assert_not_called()
            self.assertEqual(self.config.read_text(), 'existing')

    def test_success_saves_only_cloud_response_then_installs_service(self):
        calls = []
        cloud = types.SimpleNamespace(
            post=lambda server, endpoint, code, body: (calls.append(('post', code)),
                {'device_token': 'x' * 43, 'device_id': 'device_1', 'utc_offset_minutes': 480})[1],
            private_json=lambda path, data: (calls.append(('save', data['server'])),
                self.config.write_text('saved')),
        )
        fake_spec = types.SimpleNamespace(loader=types.SimpleNamespace(exec_module=lambda module: None))
        with mock.patch.object(viewer.importlib.util, 'spec_from_file_location', return_value=fake_spec), \
             mock.patch.object(viewer.importlib.util, 'module_from_spec', return_value=cloud), \
             mock.patch.object(viewer.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)) as run:
            self.assertEqual(viewer.cloud_enroll_once('a' * 40)[0], 200)
        self.assertEqual([x[0] for x in calls], ['post', 'save'])
        self.assertEqual(run.call_args.args[0], ['/bin/sh', str(self.installer), str(self.config.parent)])


if __name__ == '__main__':
    unittest.main()
