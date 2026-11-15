import importlib.util
from pathlib import Path
import types
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("wifi_viewer", ROOT / "sentry-viewer.py")
viewer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(viewer)


class WifiTest(unittest.TestCase):
    def test_scan_parses_escaped_ssids_and_prefers_active_network(self):
        output = "*:Cafe\\:West:48:WPA2\n:Cafe\\:West:92:WPA2\n:Guest:70:--\n"
        def nmcli(*args, **kwargs):
            if args[-2:] == ("device", "status"):
                return types.SimpleNamespace(returncode=0, stdout="wlan0:wifi\neth0:ethernet\n")
            return types.SimpleNamespace(returncode=0, stdout=output)
        with mock.patch.object(viewer, "wifi_nmcli", side_effect=nmcli):
            scan = viewer.wifi_scan()
        self.assertEqual(scan["interface"], "wlan0")
        self.assertEqual(scan["networks"], [
            {"ssid": "Cafe:West", "signal": 48, "security": "WPA2", "connected": True},
            {"ssid": "Guest", "signal": 70, "security": "", "connected": False},
        ])

    def test_page_contains_wifi_controls(self):
        for control in ("st-wifi", "wifi-list", "wifi-password", "wifi-connect-btn"):
            self.assertIn(control, viewer.HTML)
        self.assertIn("/api/wifi/connect", viewer.HTML)

    def test_connection_saves_private_profile_without_secret_in_command(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(viewer, "WIFI_CONN_DIR", directory), \
             mock.patch.object(viewer.time, "sleep"), \
             mock.patch.object(viewer, "wifi_nmcli", return_value=types.SimpleNamespace(returncode=0)) as nmcli:
            self.assertTrue(viewer.WIFI_LOCK.acquire(False))
            viewer.wifi_connect_worker("wlan0", "Home", "secret123", "wpa-psk")
            profiles = list(Path(directory).glob("*.nmconnection"))
            self.assertEqual(len(profiles), 1)
            self.assertIn("psk=secret123", profiles[0].read_text())
            self.assertEqual(profiles[0].stat().st_mode & 0o777, 0o600)
            self.assertNotIn("secret123", repr(nmcli.call_args_list))
            self.assertEqual(viewer.WIFI_JOB["state"], "connected")


if __name__ == "__main__":
    unittest.main()
