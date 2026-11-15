import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('sentry_video_agent', Path(__file__).resolve().parents[1] / 'sentry-video-agent.py')
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


class VideoAgentTest(unittest.TestCase):
    def test_events_and_range_are_limited_to_archive(self):
        with tempfile.TemporaryDirectory() as root:
            archive = Path(root)
            event = archive / 'SentryClips' / '2026-11-15_10-00-00'
            event.mkdir(parents=True)
            file = event / '2026-11-15_10-00-00-front.mp4'
            file.write_bytes(b'0123456789')
            (event / 'event.json').write_text('{"reason":"sentry_aware_object_detection","camera":"4","city":"上海"}')
            listing = json.loads(agent.events(archive))
            self.assertEqual(listing['SentryClips'][0]['size'], 10)
            self.assertEqual(listing['SentryClips'][0]['meta']['city'], '上海')
            listing = json.dumps(listing)
            self.assertIn('/media/SentryClips/2026-11-15_10-00-00/2026-11-15_10-00-00-front.mp4', listing)
            path = '/media/SentryClips/2026-11-15_10-00-00/2026-11-15_10-00-00-front.mp4'
            status, found, start, length, content_range = agent.file_job(archive, path, 'bytes=3-6')
            self.assertEqual((status, found, start, length, content_range),
                             (206, file, 3, 4, 'bytes 3-6/10'))
            self.assertEqual(agent.file_job(archive, path, 'bytes=20-')[0], 416)
            self.assertEqual(agent.file_job(archive, '/media/SentryClips/../secret.mp4', '')[0], 404)
            self.assertEqual(agent.thumb_job(archive, '/media/SentryClips/../secret.mp4', 1)[0], 404)

    def _clip(self, archive):
        event = archive / 'SentryClips' / '2026-11-15_10-00-00'
        event.mkdir(parents=True)
        file = event / 'clip-front.mp4'
        file.write_bytes(b'0123456789')
        return file, '/media/SentryClips/2026-11-15_10-00-00/clip-front.mp4'

    def test_thumb_serves_shared_viewer_cache(self):
        with tempfile.TemporaryDirectory() as root:
            archive = Path(root) / 'archive'
            file, path = self._clip(archive)
            target = os.path.realpath(str(file))
            stat = os.stat(target)
            key = hashlib.md5(('%s|%d|%d|%g' % (target, stat.st_size, int(stat.st_mtime), 1.0)).encode()).hexdigest()
            cached = Path(root) / 'cache' / 'thumbs'
            cached.mkdir(parents=True)
            (cached / (key + '.jpg')).write_bytes(b'\xff\xd8cached')
            original = agent.THUMB_DIR
            agent.THUMB_DIR = cached
            try:  # 假 mp4 抽帧必失败, 返回 200 只能来自缓存
                self.assertEqual(agent.thumb_job(archive, path, 1), (200, b'\xff\xd8cached'))
            finally:
                agent.THUMB_DIR = original

    def test_thumb_generation_populates_shared_cache(self):
        ffmpeg = '/opt/mu300/ffmpeg/ffmpeg' if os.path.exists('/opt/mu300/ffmpeg/ffmpeg') else shutil.which('ffmpeg')
        if not ffmpeg:
            self.skipTest('ffmpeg not available')
        with tempfile.TemporaryDirectory() as root:
            archive = Path(root) / 'archive'
            _, path = self._clip(archive)
            target = archive / 'SentryClips' / '2026-11-15_10-00-00' / 'clip-front.mp4'
            made = subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
                                   '-f', 'lavfi', '-i', 'testsrc=size=320x240:rate=10:duration=3',
                                   '-c:v', 'mpeg4', str(target)], capture_output=True, timeout=60)
            if made.returncode:
                self.skipTest('ffmpeg cannot generate test clip: %s' % made.stderr[-200:])
            original = agent.THUMB_DIR
            agent.THUMB_DIR = Path(root) / 'cache' / 'thumbs'
            try:
                status, body = agent.thumb_job(archive, path, 1)
                self.assertEqual(status, 200)
                self.assertTrue(body.startswith(b'\xff\xd8'))
                self.assertTrue(any(agent.THUMB_DIR.glob('*.jpg')))
                self.assertEqual(agent.thumb_job(archive, path, 1), (200, body))  # 第二次走缓存, 字节一致
            finally:
                agent.THUMB_DIR = original


if __name__ == '__main__':
    unittest.main()
