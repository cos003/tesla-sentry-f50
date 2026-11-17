import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("upload_name_viewer", ROOT / "sentry-viewer.py")
viewer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(viewer)


class SanitizeNameTest(unittest.TestCase):
    def test_lockchime_parentheses_become_a_safe_wav_name(self):
        self.assertEqual(viewer.sanitize_name("LockChime (18).wav"), "LockChime 18.wav")

    def test_plain_name_is_unchanged(self):
        self.assertEqual(viewer.sanitize_name("LockChime.wav"), "LockChime.wav")

    def test_path_and_only_illegal_characters_are_rejected(self):
        self.assertEqual(viewer.sanitize_name(r"C:\Users\linc\Downloads\LockChime (18).wav"), "LockChime 18.wav")
        self.assertEqual(viewer.sanitize_name("().wav"), "wav")
        self.assertIsNone(viewer.sanitize_name("()"))
        self.assertIsNone(viewer.safe_name("LockChime (18).wav"))


class InstallLockChimeTest(unittest.TestCase):
    def _root(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return root

    def test_writes_usb_root_then_gives_the_disk_back(self):
        root = self._root()
        data = b"RIFF" + b"\x00" * 32
        with mock.patch.object(viewer, "MNT", root), \
             mock.patch.object(viewer, "usb_attached", False), \
             mock.patch.object(viewer.os.path, "ismount", return_value=False), \
             mock.patch.object(viewer, "usb_attach", return_value=(True, "attached")) as attach, \
             mock.patch.object(viewer, "usb_detach", return_value=(True, "detached")) as detach, \
             mock.patch.object(viewer.subprocess, "run", return_value=mock.Mock(returncode=0)):
            ok, msg = viewer.install_lockchime(data)
        self.assertTrue(ok, msg)
        self.assertIn("车机读取已恢复", msg)
        with open(os.path.join(root, "LockChime.wav"), "rb") as f:
            self.assertEqual(f.read(), data)
        attach.assert_called_once()
        detach.assert_called_once()

    def test_keeps_an_existing_management_mount(self):
        root = self._root()
        with mock.patch.object(viewer, "MNT", root), \
             mock.patch.object(viewer, "usb_attached", True), \
             mock.patch.object(viewer.os.path, "ismount", return_value=True), \
             mock.patch.object(viewer, "usb_attach") as attach, \
             mock.patch.object(viewer, "usb_detach") as detach, \
             mock.patch.object(viewer.subprocess, "run", return_value=mock.Mock(returncode=0)):
            ok, msg = viewer.install_lockchime(b"wav")
        self.assertTrue(ok, msg)
        self.assertTrue(os.path.isfile(os.path.join(root, "LockChime.wav")))
        attach.assert_not_called()
        detach.assert_not_called()

    def test_attach_failure_does_not_pretend_to_write(self):
        root = self._root()
        with mock.patch.object(viewer, "MNT", root), \
             mock.patch.object(viewer, "usb_attached", False), \
             mock.patch.object(viewer.os.path, "ismount", return_value=False), \
             mock.patch.object(viewer, "usb_attach", return_value=(False, "losetup 失败")), \
             mock.patch.object(viewer, "usb_detach") as detach:
            ok, msg = viewer.install_lockchime(b"wav")
        self.assertFalse(ok)
        self.assertEqual(msg, "losetup 失败")
        self.assertFalse(os.path.exists(os.path.join(root, "LockChime.wav")))
        detach.assert_not_called()
