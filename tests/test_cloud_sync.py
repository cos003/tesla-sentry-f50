import importlib.util
import io
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
STAMP = '2026-01-01_08-00-00'
FIXED = 1767225600  # STAMP at UTC+8


def frame(t, state):
    return [t, 'D', 0, 0, 0, 0, 0, state, None, None, None, .1, .2, None]


class CloudTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive = self.root / 'archive'
        folder = self.archive / 'RecentClips' / STAMP
        folder.mkdir(parents=True)
        self.video = folder / (STAMP + '-front.mp4')
        self.video.write_bytes(b'fixture')
        os.utime(self.video, (FIXED, FIXED))
        self.db_path = self.root / 'queue.sqlite'
        self.db = sync.open_queue(self.db_path)
        self.addCleanup(lambda: self.db.close())
        self.tel = {'duration': 2, 'frames': [frame(0,1),frame(.1,1),frame(.2,2),frame(.3,2)]}

    def add_clip(self, cat, stamp, cam='front'):
        folder = self.archive / cat / stamp
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ('%s-%s.mp4' % (stamp, cam))
        path.write_bytes(b'fixture')
        os.utime(path, (FIXED, FIXED))
        return path

    def default_extractor(self, archive, viewer, jobs):
        return [dict(self.tel) for _ in jobs]

    def collect(self, **kwargs):
        kwargs.setdefault('clock', lambda: FIXED + 60)
        kwargs.setdefault('extractor', self.default_extractor)
        return sync.collect(self.db, self.archive, ROOT/'sentry-viewer.py', 480, **kwargs)

    def acknowledge(self):
        with self.db:
            self.db.execute('UPDATE clips SET pending=0')

    def test_recording_age_limits_parsing_even_when_mtime_is_recent(self):
        for age, expected in ((1800, 1), (1801, 0), (-1, 0)):  # RecentClips 窗口 30 分钟
            with self.subTest(age=age):
                extractor = mock.Mock(side_effect=lambda archive, viewer, jobs: [dict(self.tel) for _ in jobs])
                self.db.execute('DELETE FROM clips')
                self.db.commit()
                result = sync.collect(self.db, self.archive, ROOT/'sentry-viewer.py', 480,
                                      extractor=extractor, clock=lambda: FIXED + age)
                self.assertEqual(result['queued'], expected)
                self.assertEqual(extractor.call_count, expected)

    def test_sentry_alert_clips_are_never_parsed(self):
        self.video.unlink()
        folder = self.archive / 'SentryClips' / '2026-01-01_07-59-00'
        folder.mkdir(parents=True)
        (folder / 'event.json').write_text(json.dumps({'reason': 'sentry_aware_object_detection'}), encoding='utf-8')
        self.add_clip('SentryClips', '2026-01-01_07-59-00')
        calls = []
        def extractor(archive, viewer, jobs):
            calls.extend(jobs)
            return [dict(self.tel) for _ in jobs]
        result = self.collect(extractor=extractor)
        self.assertEqual(result['queued'], 0)
        self.assertEqual(calls, [])          # 停车告警不读视频
        self.assertEqual(self.db.execute('SELECT count(*) FROM clips').fetchone()[0], 0)

    def test_saved_clips_are_parsed_no_matter_how_old(self):
        self.video.unlink()
        self.add_clip('SavedClips', '2025-12-20_07-00-00')  # 十几天前的手动保存
        result = self.collect()
        self.assertEqual(result['queued'], 1)   # 无视积压
        self.assertEqual(result['skipped'], 0)

    def test_saved_clip_is_scanned_before_a_routine_recent_clip(self):
        self.add_clip('SavedClips', '2026-01-01_07-58-00')
        seen = []
        def extractor(archive, viewer, jobs):
            seen.extend(Path(job[0]).name for job in jobs)
            return [dict(self.tel) for _ in jobs]
        self.collect(extractor=extractor)
        self.assertEqual(seen, ['2026-01-01_07-58-00-front.mp4'])

    def test_oldest_in_window_clip_is_scanned_first(self):
        self.add_clip('RecentClips', '2026-01-01_07-57-00')
        seen = []
        def extractor(archive, viewer, jobs):
            seen.extend(str(job[0]) for job in jobs)
            return [dict(self.tel) for _ in jobs]
        result = self.collect(extractor=extractor)
        self.assertEqual(result['queued'], 1)
        self.assertTrue(result['more'])  # 还有刚录完的那一段没扫
        self.assertEqual(Path(seen[0]).name, '2026-01-01_07-57-00-front.mp4')

    def test_near_deadline_cycle_scans_more_than_one_clip(self):
        self.video.unlink()
        for minute in ('07-33-00', '07-32-00', '07-31-00'):  # 窗口剩 120/60/0 秒
            self.add_clip('RecentClips', '2026-01-01_' + minute)
        result = self.collect(clock=lambda: FIXED + 60)
        self.assertEqual(result['queued'], 3)  # budget=1 + 三段都进入紧急区
        self.assertFalse(result['more'])

    def test_coarse_scan_is_refined_after_the_window_closes(self):
        strides = []
        def extractor(archive, viewer, jobs):
            strides.extend(job[1] for job in jobs)
            return [dict(self.tel) for _ in jobs]
        self.assertEqual(self.collect(extractor=extractor)['queued'], 1)
        self.assertEqual(self.db.execute('SELECT quality,status FROM clips').fetchone(), ('coarse', 'available'))
        self.acknowledge()
        self.assertEqual(self.collect(extractor=extractor, clock=lambda: FIXED + 2000)['queued'], 1)
        self.assertEqual(strides, [sync.COARSE_STRIDE, sync.DENSE_STRIDE])
        self.assertEqual(self.db.execute('SELECT quality,revision FROM clips').fetchone(), ('dense', 2))

    def test_parked_clip_is_not_rescanned_after_the_window(self):
        def parked(archive, viewer, jobs):
            return [{'duration': 2, 'frames': []} for _ in jobs]
        self.assertEqual(self.collect(extractor=parked)['queued'], 1)
        self.acknowledge()
        calls = []
        def later(archive, viewer, jobs):
            calls.extend(jobs)
            return [{'duration': 2, 'frames': []} for _ in jobs]
        self.assertEqual(self.collect(extractor=later, clock=lambda: FIXED + 2000)['queued'], 0)
        self.assertEqual(calls, [])  # 停车片段没有 SEI, 补精扫不会读出任何东西

    def test_refine_steps_aside_for_the_deadline_queue(self):
        strides = []
        def extractor(archive, viewer, jobs):
            strides.extend(job[1] for job in jobs)
            return [dict(self.tel) for _ in jobs]
        self.assertEqual(self.collect(extractor=extractor)['queued'], 1)   # 窗口内粗扫
        self.assertEqual(strides, [sync.COARSE_STRIDE])
        self.acknowledge()
        strides.clear()
        self.add_clip('RecentClips', '2026-01-01_08-33-00')               # 刚录完, 还没扫
        later = lambda: FIXED + 2000                                      # 08-00-00 已出窗口
        self.assertEqual(self.collect(extractor=extractor, clock=later)['queued'], 1)
        self.assertEqual(strides, [sync.COARSE_STRIDE])                   # 先扫新片段, 不精扫
        self.acknowledge()
        self.assertEqual(self.collect(extractor=extractor, clock=later)['queued'], 1)
        self.assertEqual(strides, [sync.COARSE_STRIDE, sync.DENSE_STRIDE])  # 空闲轮再补精扫
        self.assertEqual(self.db.execute("SELECT revision FROM clips WHERE quality='dense'").fetchone()[0], 2)

    def test_only_the_main_camera_is_parsed_per_group(self):
        for cam in ('back', 'left_repeater'):
            rear = self.video.with_name(self.video.name.replace('front', cam))
            rear.write_bytes(b'fixture')
            os.utime(rear, (FIXED, FIXED))
        calls = []
        def no_sei(archive, viewer, jobs):
            calls.extend(job[0].name for job in jobs)
            return [{'duration': 2, 'frames': []} for _ in jobs]
        self.assertEqual(self.collect(extractor=no_sei)['queued'], 1)
        self.assertEqual(calls, ['2026-01-01_08-00-00-front.mp4'])  # 停车片段不再把每一路都解一遍
        self.db.execute('DELETE FROM clips')
        self.db.commit()
        tried = []
        def broken_main(archive, viewer, jobs):
            tried.extend(job[0].name for job in jobs)
            return [None if job[0].name.endswith('-front.mp4') else dict(self.tel) for job in jobs]
        self.assertEqual(self.collect(extractor=broken_main)['queued'], 1)
        self.assertEqual(len(tried), 2)  # 主摄读不出来才回退到下一路
        self.assertTrue(tried[0].endswith('-front.mp4'))

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
        f[3], f[5] = 150, 1
        self.assertEqual(sync.telemetry_samples([f])['samples'][0][-2:], [None, True])
        self.assertEqual(sync.telemetry_samples([frame(0, 1), frame(0, 1)], 1000)['samples'].__len__(), 1)

    def test_rejected_clip_does_not_block_the_rest_of_the_queue(self):
        self.collect()
        good = self.db.execute('SELECT clip_id,revision,payload FROM clips').fetchone()
        bad = json.loads(good[2])
        bad['clip_id'] = 'b' * 64
        bad['utc_offset_minutes'] = 0
        self.db.execute('INSERT INTO clips (clip_id,signature,revision,payload,pending,updated_at) VALUES (?,?,?,?,1,?)',
                        (bad['clip_id'], 'sig', 1, sync.canonical(bad), time.time() + 1))
        self.db.commit()
        config = {'server': 'https://cloud.test', 'device_token': 'x' * 43}

        def sender(server, path, token, body):
            if any(c['utc_offset_minutes'] != 480 for c in body['clips']):
                raise urllib.error.HTTPError(
                    'https://cloud.test', 400, 'bad', {},
                    io.BytesIO(b'{"ok":false,"error":{"code":"invalid_clip_time"}}'))
            return {'batch_id': body['batch_id'],
                    'accepted': [{'clip_id': c['clip_id'], 'revision': c['revision']} for c in body['clips']]}

        self.assertEqual(sync.upload(self.db, config, sender=sender), 1)
        self.assertEqual(self.db.execute('SELECT pending FROM clips WHERE clip_id=?', (good[0],)).fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT pending FROM clips WHERE clip_id=?', (bad['clip_id'],)).fetchone()[0], 1)
        self.assertGreater(self.db.execute('SELECT retry_at FROM failures WHERE clip_id=?', (bad['clip_id'],)).fetchone()[0], time.time())
        scene = json.loads(self.db.execute('SELECT scene FROM failures WHERE clip_id=?', (bad['clip_id'],)).fetchone()[0])
        self.assertEqual(scene['code'], 'invalid_clip_time')
        self.assertEqual(scene['utc_offset_minutes'], 0)
        self.assertEqual(scene['clip_id'], bad['clip_id'])
        self.assertEqual(sync.upload(self.db, config, sender=sender), 0)

    def test_only_observed_ranges_are_covered(self):
        result = sync.segments([frame(0,1),frame(.1,1),frame(.2,None),frame(1.5,2),frame(1.6,2)],2000)
        self.assertEqual(result,[{'start_offset_ms':0,'end_offset_ms':200,'raw_state':1},
                                 {'start_offset_ms':1500,'end_offset_ms':1600,'raw_state':2}])

    def test_grouping_timezone_and_incremental_discovery(self):
        rear=self.video.with_name(self.video.name.replace('front','back'))
        rear.write_bytes(b'fixture'); os.utime(rear,(FIXED,)*2)
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
        os.utime(self.video,(FIXED,)*2)
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
        def changing(archive, viewer, jobs):
            self.video.write_bytes(b'changed')
            return [dict(self.tel) for _ in jobs]
        result=sync.collect(self.db,self.archive,ROOT/'sentry-viewer.py',480,extractor=changing,clock=lambda:FIXED+60)
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

    def test_clicked_retry_uploads_during_backoff(self):
        config = self.root / 'cloud.json'
        sync.private_json(config, {'server': 'https://cloud.test', 'device_id': 'device_one',
                                   'device_token': 'a' * 43, 'utc_offset_minutes': 480})
        class StopLoop(Exception):
            pass
        calls = {'n': 0}
        def sleeper(_delay):
            calls['n'] += 1
            if calls['n'] == 1:
                db = sync.sqlite3.connect(str(self.root / 'backoff.sqlite'))
                db.execute("INSERT OR REPLACE INTO state VALUES ('retry_requested','clicked')")
                db.commit()
                db.close()
            if calls['n'] == 2:
                raise StopLoop()
        with mock.patch.object(sync, 'collect', return_value={'failed': 0, 'backpressure': False}), \
             mock.patch.object(sync, 'upload', side_effect=urllib.error.HTTPError(
                 'https://cloud.test', 400, 'bad', {}, None)) as upload, \
             mock.patch.object(sync.time, 'sleep', side_effect=sleeper):
            with self.assertRaises(StopLoop):
                sync.main(['run', '--config', str(config), '--state', str(self.root / 'backoff.sqlite'),
                           '--archive', str(self.archive)])
        self.assertEqual(upload.call_count, 2)

    def test_run_uploads_after_one_clip_and_drains_existing_queue(self):
        config = self.root/'cloud.json'
        sync.private_json(config, {'server':'https://cloud.test','device_id':'device_one',
                                  'device_token':'a'*43,'utc_offset_minutes':480})
        events = []
        def collect(*args, **kwargs):
            self.assertEqual(kwargs['budget'], 1)
            events.append('parse')
            return {'failed':0, 'backpressure':False, 'queued':1, 'coarse':1, 'dense':0,
                    'skipped':2, 'seconds':3.5, 'more':False}
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
        state = dict(sync.sqlite3.connect(str(self.db_path)).execute('SELECT key,value FROM state'))
        self.assertEqual(state['scan_coarse'], '1')
        self.assertEqual(state['scan_skipped'], '2')
        self.assertEqual(state['scan_seconds'], '3.5')
        self.assertEqual(state['parse_window_seconds'], '1800')
        self.assertIn('refine_pending', state)


class ExtractTest(unittest.TestCase):
    def test_one_process_handles_a_whole_same_stride_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            log = tmp / 'calls.txt'
            viewer = tmp / 'fake-viewer.py'
            viewer.write_text(
                'import json, os, sys\n'
                'argv = sys.argv[1:]\n'
                'open(os.environ["FAKE_LOG"], "a").write(" ".join(argv) + "\\n")\n'
                'if "--cloud-stride" in argv:\n'
                '    i = argv.index("--cloud-stride")\n'
                '    del argv[i:i + 2]\n'
                'for path in argv[argv.index("--cloud-telemetry") + 1:]:\n'
                '    print(json.dumps({"file": path, "duration": 60, "frames": []}))\n', encoding='utf-8')
            jobs = [(tmp/'a.mp4', 4, 45), (tmp/'b.mp4', 4, 45), (tmp/'c.mp4', 1, 120)]
            with mock.patch.dict(os.environ, {'FAKE_LOG': str(log)}):
                results = sync.extract(tmp, viewer, jobs)
            calls = log.read_text().strip().splitlines()
            self.assertEqual(len(calls), 2)  # 同一 stride 合成一次进程
            self.assertTrue(calls[0].endswith('--cloud-stride 4'))
            self.assertTrue(calls[1].endswith('--cloud-stride 1'))
            self.assertEqual([r['duration'] for r in results], [60, 60, 60])
            self.assertEqual([r['frames'] for r in results], [[], [], []])

    def test_a_failed_line_only_fails_that_clip(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            viewer = tmp / 'fake-viewer.py'
            viewer.write_text(
                'import json, sys\n'
                'argv = sys.argv[1:]\n'
                'if "--cloud-stride" in argv:\n'
                '    i = argv.index("--cloud-stride")\n'
                '    del argv[i:i + 2]\n'
                'for path in argv[argv.index("--cloud-telemetry") + 1:]:\n'
                '    if path.endswith("bad.mp4"):\n'
                '        print(json.dumps({"file": path, "error": "telemetry extraction failed"}))\n'
                '    else:\n'
                '        print(json.dumps({"file": path, "duration": 60, "frames": []}))\n', encoding='utf-8')
            jobs = [(tmp/'good.mp4', 4, 45), (tmp/'bad.mp4', 4, 45)]
            results = sync.extract(tmp, viewer, jobs)
            self.assertEqual(results[0]['duration'], 60)
            self.assertIsNone(results[1])


class ExtractorTest(unittest.TestCase):
    def viewer(self, tmp):
        with mock.patch.dict(os.environ, {'TSF_CACHE': tmp}):
            spec = importlib.util.spec_from_file_location('viewer', ROOT/'sentry-viewer.py')
            viewer = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(viewer)
        return viewer

    def test_real_sei_parser_preserves_missing_ap_as_unknown_for_cloud_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            viewer = self.viewer(tmp)
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

    def test_parked_clip_is_probed_instead_of_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            viewer = self.viewer(tmp)
            fp = Path(tmp) / 'parked.mp4'
            nal = b'\x65' + b'\x11' * 16          # 普通 NAL, 没有 SEI
            sample = struct.pack('>I', len(nal)) + nal
            fp.write_bytes(sample * 4)
            offs = [(i * len(sample), len(sample)) for i in range(4)]
            with mock.patch.object(viewer, '_mp4_video_samples', return_value=(offs, 4)), \
                 mock.patch.object(viewer, '_mp4_duration', return_value=2.0), \
                 mock.patch.object(viewer, 'probe_duration', return_value=0.0), \
                 mock.patch.object(viewer, 'parse_telemetry') as parse:
                out = viewer.cloud_extract(fp, 4)
            parse.assert_not_called()
            self.assertEqual(out, {'duration': 2.0, 'frames': []})

    def test_probe_finds_sei_that_starts_mid_clip(self):
        with tempfile.TemporaryDirectory() as tmp:
            viewer = self.viewer(tmp)
            fp = Path(tmp) / 'leaving.mp4'
            samples = []
            for index in range(40):
                if 16 <= index < 20:   # 停车后开走: SEI 只出现在后半段
                    nal = b'\x06\x05\xffBBi' + b'\x08\x01' + b'\x80'
                else:
                    nal = b'\x65' + b'\x11' * 8
                samples.append(struct.pack('>I', len(nal)) + nal)
            fp.write_bytes(b''.join(samples))
            offs = []
            offset = 0
            for sample in samples:
                offs.append((offset, len(sample)))
                offset += len(sample)
            with mock.patch.object(viewer, '_mp4_video_samples', return_value=(offs, 4)), \
                 mock.patch.object(viewer, '_mp4_duration', return_value=2.0), \
                 mock.patch.object(viewer, 'parse_telemetry', return_value={'duration': 2.0, 'frames': [[0,'D',1,0,0,0,0,2,None,None,None,0,0,0]]}) as parse:
                out = viewer.cloud_extract(fp, 4)
            parse.assert_called_once()
            self.assertEqual(parse.call_args.kwargs['stride'], 4)
            self.assertEqual(len(out['frames']), 1)


if __name__ == '__main__':
    unittest.main()
