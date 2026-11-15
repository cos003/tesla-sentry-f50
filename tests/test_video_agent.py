import importlib.util
from pathlib import Path
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
            listing = agent.events(archive).decode()
            self.assertIn('/media/SentryClips/2026-11-15_10-00-00/2026-11-15_10-00-00-front.mp4', listing)
            path = '/media/SentryClips/2026-11-15_10-00-00/2026-11-15_10-00-00-front.mp4'
            status, found, start, length, content_range = agent.file_job(archive, path, 'bytes=3-6')
            self.assertEqual((status, found, start, length, content_range),
                             (206, file, 3, 4, 'bytes 3-6/10'))
            self.assertEqual(agent.file_job(archive, path, 'bytes=20-')[0], 416)
            self.assertEqual(agent.file_job(archive, '/media/SentryClips/../secret.mp4', '')[0], 404)


if __name__ == '__main__':
    unittest.main()
