import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('sei_viewer', ROOT / 'sentry-viewer.py')
viewer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(viewer)


class DiagnosticsTest(unittest.TestCase):
    def test_trace_and_cloud_parser_produce_same_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            fp = Path(tmp) / 'video.mp4'
            payload = b'\x08\x01\x28\x15\x48\x01'  # version=1, accelerator=21, brake=1; no AP
            nal = b'\x06\x05\xffBBi' + payload + b'\x80'
            sample = struct.pack('>I', len(nal)) + nal
            fp.write_bytes(sample * 2)
            with mock.patch.object(viewer, '_mp4_video_samples', return_value=([(0, len(sample)), (len(sample), len(sample))], 4)), mock.patch.object(viewer, 'probe_duration', return_value=1):
                cloud = viewer.parse_telemetry(fp, stride=1, missing_ap_unknown=True)
                report = viewer.diagnose_telemetry(str(fp))
            self.assertEqual(report['status'], 'no_ap')
            self.assertEqual(report['samples'], 2)
            self.assertEqual(report['sample_examples'], cloud['frames'])
            self.assertEqual(report['trace']['sei_units'], 2)
            self.assertEqual(report['trace']['tesla_markers'], 2)
            self.assertEqual(report['trace']['fields']['5'], 2)
            self.assertEqual(report['field_summary']['accelerator_pct']['max'], 21)
            self.assertEqual(report['field_summary']['ap_raw_state']['present'], 0)

    def test_missing_mp4_structure_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            fp = Path(tmp) / 'empty.mp4'
            fp.write_bytes(b'no mp4 structure')
            report = viewer.diagnose_telemetry(str(fp))
            self.assertEqual(report['status'], 'no_sei')
            self.assertFalse(report['trace']['moov_found'])
            self.assertEqual(report['samples'], 0)

    def test_errors_retain_stage_details(self):
        with tempfile.NamedTemporaryFile() as fp:
            with mock.patch.object(viewer, '_mp4_video_samples', side_effect=ValueError('invalid sample table')):
                report = viewer.diagnose_telemetry(fp.name)
            self.assertEqual(report['status'], 'parse_error')
            self.assertIn('invalid sample table', report['error'])

    def test_http_gate_and_archive_boundary(self):
        server = viewer.ThreadingHTTPServer(('127.0.0.1', 0), viewer.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = 'http://127.0.0.1:%d' % server.server_port
        with mock.patch.object(viewer, 'AUTH_ON', True):
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(base + '/api/telemetry/diagnose?path=/etc/passwd')
            self.assertEqual(err.exception.code, 401)
        with mock.patch.object(viewer, 'AUTH_ON', False):
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(base + '/api/telemetry/diagnose?path=/etc/passwd')
            self.assertEqual(err.exception.code, 404)
            page = urllib.request.urlopen(base + '/sei-diagnostics').read().decode()
            self.assertIn('SEI 解析诊断', page)


if __name__ == '__main__':
    unittest.main()
