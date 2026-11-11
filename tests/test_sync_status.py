import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
def module(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / file)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

sync = module('status_sync', 'sentry-cloud-sync.py')
viewer = module('status_viewer', 'sentry-viewer.py')

class StatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.archive = self.path / 'archive'
        stamp = sync.dt.datetime.now(sync.dt.timezone(sync.dt.timedelta(hours=8))).strftime('%Y-%m-%d_%H-%M-%S')
        folder = self.archive / 'SavedClips' / stamp
        folder.mkdir(parents=True)
        self.video = folder / (stamp + '-front.mp4')
        self.video.write_bytes(b'fixture')
        os.utime(self.video, (time.time()-60,)*2)
        self.dbpath = self.path / 'queue.sqlite'
        self.db = sync.open_queue(self.dbpath)
        self.addCleanup(self.db.close)
        self.key = next(sync.discover(self.archive))[0]
        patch = mock.patch.dict(os.environ, TSF_CLOUD_STATE=str(self.dbpath))
        patch.start(); self.addCleanup(patch.stop)

    def status(self):
        return viewer.cloud_clip_status([self.key])['clips'][self.key]

    def collect(self, extractor):
        return sync.collect(self.db, self.archive, ROOT/'sentry-viewer.py', 480, extractor=extractor)

    def test_wait_parse_queue_failure_retry_ack_lifecycle(self):
        self.assertEqual(self.status()['phase'], 'waiting')
        def extract(*args):
            self.assertEqual(self.status()['phase'], 'parsing')
            return {'duration': 1, 'frames': []}
        self.collect(extract)
        self.assertEqual(self.status()['phase'], 'queued')
        config = {'server':'https://cloud.test','device_token':'x'*43}
        def fail(*args):
            self.assertEqual(self.status()['phase'], 'uploading')
            raise OSError('private detail must not be published')
        with self.assertRaises(OSError):
            sync.upload(self.db, config, sender=fail)
        self.assertEqual(self.status()['phase'], 'upload_failed')
        self.assertNotIn('private', json.dumps(self.status()))
        self.assertEqual(self.db.execute('SELECT pending FROM clips').fetchone()[0], 1)
        ok, msg = viewer.cloud_retry_uploads([self.key])
        self.assertTrue(ok)
        self.assertEqual(msg, '已安排重新上传')
        self.assertEqual(self.status()['phase'], 'queued')
        self.assertNotEqual(self.db.execute("SELECT value FROM state WHERE key='retry_requested'").fetchone()[0], '')
        self.assertFalse(viewer.cloud_retry_uploads([self.key])[0])
        def ack(*args):
            body = args[-1]
            return {'batch_id':body['batch_id'],'accepted':[{'clip_id':r['clip_id'],'revision':r['revision']} for r in body['clips']]}
        sync.upload(self.db, config, sender=ack)
        self.assertEqual(self.status()['phase'], 'uploaded')
        self.assertEqual(self.status()['source'], self.video.name)
        self.assertEqual(self.db.execute('SELECT payload FROM clips').fetchone()[0], '')

    def test_parse_failure_stale_and_legacy_progress(self):
        def fail(*args): raise ValueError('bad')
        self.collect(fail)
        self.assertEqual(self.status()['phase'], 'parse_failed')
        sync.progress(self.db, self.key, 'parsing', clock=lambda:time.time()-1000)
        self.assertEqual(self.status()['phase'], 'interrupted')
        self.db.execute('DROP TABLE progress')
        self.db.commit()
        self.assertFalse(viewer.cloud_clip_status([self.key])['tracking'])
        self.assertEqual(self.status()['phase'], 'parse_failed')

    def test_list_and_scanner_use_same_id_for_all_cameras(self):
        self.video.with_name(self.video.name.replace('front','back')).write_bytes(b'fixture')
        with mock.patch.object(viewer, 'ARCHIVE', str(self.archive)):
            videos = viewer.list_events()['SavedClips'][0]['videos']
        self.assertEqual([v['sync_id'] for v in videos], [self.key, self.key])

    def test_incomplete_ack_never_marks_success(self):
        self.collect(lambda *_: {'duration': 1, 'frames': []})
        with self.assertRaises(ValueError):
            sync.upload(self.db, {'server':'https://cloud.test','device_token':'x'*43},
                        sender=lambda *_: {'batch_id':'wrong','accepted':[]})
        self.assertEqual(self.status()['phase'], 'upload_failed')
        self.assertEqual(self.db.execute('SELECT pending FROM clips').fetchone()[0], 1)

    def test_legacy_success_is_visible_without_progress_table(self):
        self.collect(lambda *_: {'duration': 1, 'frames': []})
        self.db.execute('DROP TABLE progress')
        self.db.execute('UPDATE clips SET pending=0')
        self.db.commit()
        self.assertEqual(self.status()['phase'], 'uploaded')

    def test_missing_database_is_not_reported_as_queued(self):
        with mock.patch.dict(os.environ, TSF_CLOUD_STATE=str(self.path/'absent.sqlite')):
            report = viewer.cloud_clip_status([self.key])
        self.assertFalse(report['available'])
        self.assertEqual(report['reason'], 'no_queue')

if __name__ == '__main__': unittest.main()
