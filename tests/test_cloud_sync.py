import importlib.util
import json
import os
from pathlib import Path
import struct
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cloud_sync', ROOT / 'sentry-cloud-sync.py')
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


def frame(t, state):
    return [t, 'D', 0, 0, 0, 0, 0, state, None, None, None, .1, .2, None]


class CloudTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive = self.root / 'archive'
        folder = self.archive / 'RecentClips' / '2026-01-01_08-00-00'
        folder.mkdir(parents=True)
        self.video = folder / '2026-01-01_08-00-00-front.mp4'
        self.video.write_bytes(b'fixture')
        os.utime(self.video, (1767225600, 1767225600))
        self.db_path = self.root / 'queue.sqlite'
        self.db = sync.open_queue(self.db_path)
        self.addCleanup(lambda: self.db.close())
        self.tel = {'duration': 2, 'frames': [frame(0,1),frame(.1,1),frame(.2,2),frame(.3,2)]}

    def collect(self, **kwargs):
        kwargs.setdefault('clock', lambda: 1767225660)
        return sync.collect(self.db,self.archive,ROOT/'sentry-viewer.py',480,extractor=lambda *_:self.tel,**kwargs)

    def test_recording_age_limits_parsing_even_when_mtime_is_recent(self):
        for age, expected in ((300, 1), (301, 0), (-1, 0)):
            with self.subTest(age=age):
                extractor = mock.Mock(return_value=self.tel)
                self.db.execute('DELETE FROM clips')
                self.db.commit()
                result = sync.collect(self.db, self.archive, ROOT/'sentry-viewer.py', 480,
                                      extractor=extractor, clock=lambda: 1767225600 + age)
                self.assertEqual(result['queued'], expected)
                self.assertEqual(extractor.call_count, expected)

    def test_sentry_is_not_scanned_but_old_saved_clips_are(self):
        for category in ('SentryClips', 'SavedClips'):
            folder = self.archive/category/self.video.parent.name
            folder.mkdir(parents=True)
            video = folder/self.video.name
            video.write_bytes(b'fixture')
            os.utime(video, (1767225600,)*2)
        seen = []
        def extract(viewer, path):
            seen.append(path.parents[1].name)
            return self.tel
        original_iterdir = Path.iterdir
        def iterdir(path):
            if path.name == 'SentryClips':
                raise AssertionError('sentry scanned')
            return original_iterdir(path)
        with mock.patch.object(Path, 'iterdir', iterdir):
            result = sync.collect(self.db, self.archive, ROOT/'sentry-viewer.py', 480,
                                  extractor=extract, clock=lambda:1767225600+86400)
        self.assertEqual(result['queued'], 1)
        self.assertEqual(seen, ['SavedClips'])
        self.assertEqual(self.collect(clock=lambda:1767225600+86400)['queued'], 0)

    def test_only_front_camera_is_parsed_without_fallback(self):
        for camera in ('back', 'left_repeater', 'right_repeater', 'left_pillar', 'right_pillar'):
            path = self.video.with_name(self.video.name.replace('front', camera))
            path.write_bytes(b'fixture')
            os.utime(path, (1767225600,)*2)
        for result in (self.tel, {'duration':2, 'frames':[]}, ValueError('bad front')):
            with self.subTest(result=result):
                self.db.execute('DELETE FROM clips')
                self.db.execute('DELETE FROM failures')
                self.db.commit()
                extractor = mock.Mock(side_effect=result) if isinstance(result, Exception) else mock.Mock(return_value=result)
                sync.collect(self.db, self.archive, ROOT/'sentry-viewer.py', 480,
                             extractor=extractor, clock=lambda:1767225660)
                self.assertEqual(extractor.call_count, 1)
                self.assertEqual(extractor.call_args.args[1], self.video)
        self.video.unlink()
        self.db.execute('DELETE FROM failures')
        self.db.commit()
        extractor = mock.Mock()
        stats = sync.collect(self.db, self.archive, ROOT/'sentry-viewer.py', 480,
                             extractor=extractor, clock=lambda:1767225660)
        extractor.assert_not_called()
        self.assertEqual(stats['failed'], 1)

    def test_pedal_samples(self):
        f = frame(.1, 1)
        f[3], f[5] = 37, 1
        data = sync.telemetry_samples([f])
        self.assertEqual(data['fields'][-2:], ['accelerator_pct', 'brake_pressed'])
        self.assertEqual(data['samples'][0][-2:], [37, True])
        f[3], f[5] = 0, 0
        self.assertEqual(sync.telemetry_samples([f])['samples'][0][-2:], [0, False])
        f[3], f[5] = None, None
        self.assertEqual(sync.telemetry_samples([f])['samples'][0][-2:], [None, None])

    def test_only_observed_ranges_are_covered(self):
        result = sync.segments([frame(0,1),frame(.1,1),frame(.2,None),frame(1.5,2),frame(1.6,2)],2000)
        self.assertEqual(result,[{'start_offset_ms':0,'end_offset_ms':200,'raw_state':1},
                                 {'start_offset_ms':1500,'end_offset_ms':1600,'raw_state':2}])

    def test_grouping_timezone_and_incremental_discovery(self):
        rear=self.video.with_name(self.video.name.replace('front','back'))
        rear.write_bytes(b'fixture'); os.utime(rear,(1767225600,)*2)
        self.assertEqual(self.collect()['queued'],1)
        self.assertEqual(self.collect()['queued'],0)
        payload=json.loads(self.db.execute('SELECT payload FROM clips').fetchone()[0])
        self.assertEqual(payload['started_at_ms'],1767225600000)  # 08:00 at UTC+8 == 00:00 UTC
        self.assertEqual(payload['revision'],1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM clips').fetchone()[0],1)

    def test_lost_ack_survives_restart_and_does_not_overwrite_pending_revision(self):
        self.collect()
        batches=[]
        def offline(server,path,token,body):
            batches.append(body)
            raise OSError('offline')
        config={'server':'https://cloud.test','device_token':'x'*43}
        with self.assertRaises(OSError): sync.upload(self.db,config,sender=offline)
        self.video.write_bytes(b'changed file')
        os.utime(self.video,(1767225600,)*2)
        self.assertEqual(self.collect()['queued'],0)
        self.db.close(); self.db=sync.open_queue(self.db_path)
        def ack(server,path,token,body):
            self.assertEqual(body,batches[0])
            return {'batch_id':body['batch_id'],'accepted':[{'clip_id':c['clip_id'],'revision':c['revision']} for c in body['clips']]}
        self.assertEqual(sync.upload(self.db,config,sender=ack),1)
        self.assertEqual(self.collect()['queued'],1)
        self.assertEqual(self.db.execute('SELECT revision FROM clips').fetchone()[0],2)

    def test_incomplete_ack_keeps_queue_and_deleted_video_can_still_upload(self):
        self.collect(); self.video.unlink()
        config={'server':'https://cloud.test','device_token':'x'*43}
        with self.assertRaises(ValueError):
            sync.upload(self.db,config,sender=lambda *a:{'batch_id':'wrong','accepted':[]})
        self.assertEqual(self.db.execute('SELECT pending FROM clips').fetchone()[0],1)
        def ack(*args):
            body=args[-1]
            return {'batch_id':body['batch_id'],'accepted':[{'clip_id':c['clip_id'],'revision':c['revision']} for c in body['clips']]}
        self.assertEqual(sync.upload(self.db,config,sender=ack),1)
        self.assertEqual(self.db.execute('SELECT payload FROM clips').fetchone()[0],'')

    def test_backpressure_preserves_existing_queue(self):
        self.collect()
        with mock.patch.object(sync,'MAX_PENDING',1):
            self.assertTrue(self.collect()['backpressure'])
        self.assertEqual(self.db.execute('SELECT pending FROM clips').fetchone()[0],1)

    def test_changing_file_is_not_queued_and_failure_is_retried_later(self):
        def changing(*args):
            self.video.write_bytes(b'changed'); return self.tel
        result=sync.collect(self.db,self.archive,ROOT/'sentry-viewer.py',480,extractor=changing,clock=lambda:1767225660)
        self.assertEqual(result['failed'],1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM clips').fetchone()[0],0)

    def test_cloud_origin_and_redirect_policy(self):
        for bad in ('http://cloud.test','https://user:secret@cloud.test','https://cloud.test/api','https://cloud.test?token=x'):
            with self.assertRaises(ValueError): sync.origin(bad)
        self.assertEqual(sync.origin('http://127.0.0.1:8000',True),'http://127.0.0.1:8000')
        self.assertIsNone(sync.NoRedirect().redirect_request(None,None,302,None,None,'https://evil.test'))

    def test_auth_failure_backs_off_upload_without_stopping_collection(self):
        config=self.root/'cloud.json'
        sync.private_json(config,{'server':'https://cloud.test','device_id':'device_one',
                                  'device_token':'a'*43,'utc_offset_minutes':480})
        class StopLoop(Exception): pass
        stats={'failed':0,'backpressure':False}
        with mock.patch.object(sync,'collect',return_value=stats) as collect, \
             mock.patch.object(sync,'upload',side_effect=urllib.error.HTTPError('https://cloud.test',401,'unauthorized',{},None)) as upload, \
             mock.patch.object(sync.time,'sleep',side_effect=[None,None,StopLoop()]):
            with self.assertRaises(StopLoop):
                sync.main(['run','--config',str(config),'--state',str(self.root/'backoff.sqlite'),
                           '--archive',str(self.archive)])
            self.assertEqual(collect.call_count,3)
            self.assertEqual(upload.call_count,1)

    def test_run_uploads_after_one_clip_and_drains_existing_queue(self):
        config = self.root/'cloud.json'
        sync.private_json(config, {'server':'https://cloud.test','device_id':'device_one',
                                  'device_token':'a'*43,'utc_offset_minutes':480})
        events = []
        def collect(*args, **kwargs):
            self.assertEqual(kwargs['budget'], 1)
            events.append('parse')
            return {'failed':0, 'backpressure':False}
        def upload(db, config):
            events.append('upload')
            if events.count('upload') == 2:
                db.execute('UPDATE clips SET pending=0')
                db.commit()
            return 1
        self.collect()
        self.db.close()
        with mock.patch.object(sync, 'collect', side_effect=collect), \
             mock.patch.object(sync, 'upload', side_effect=upload), \
             mock.patch.object(sync, 'post', return_value={}):
            self.assertEqual(sync.main(['run','--once','--config',str(config),
                                        '--state',str(self.db_path),'--archive',str(self.archive)]), 0)
        self.assertEqual(events, ['parse','upload','upload'])


class ExtractorTest(unittest.TestCase):
    def test_real_sei_parser_preserves_missing_ap_as_unknown_for_cloud_only(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{'TSF_CACHE':tmp}):
            spec=importlib.util.spec_from_file_location('viewer',ROOT/'sentry-viewer.py')
            viewer=importlib.util.module_from_spec(spec); spec.loader.exec_module(viewer)
            samples=[]
            for state in (1,None,2):
                payload=b'\x08\x01'+(bytes([80,state]) if state is not None else b'')
                nal=b'\x06\x05\xffBBi'+payload+b'\x80'
                samples.append(struct.pack('>I',len(nal))+nal)
            fp=Path(tmp)/'test.mp4'; fp.write_bytes(b''.join(samples))
            offs=[]; offset=0
            for sample in samples: offs.append((offset,len(sample))); offset+=len(sample)
            with mock.patch.object(viewer,'_mp4_video_samples',return_value=(offs,4)),mock.patch.object(viewer,'probe_duration',return_value=.3):
                tel=viewer.parse_telemetry(fp,stride=1,missing_ap_unknown=True)
                self.assertEqual([f[7] for f in tel['frames']],[1,None,2])
                self.assertEqual(viewer.parse_telemetry(fp,stride=1)['frames'][1][7],0)


if __name__ == '__main__':
    unittest.main()
