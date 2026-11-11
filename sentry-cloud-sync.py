#!/usr/bin/env python3
"""Optional HTTPS telemetry uploader. No third-party packages; never uploads videos."""
import argparse
from contextlib import ExitStack
import datetime as dt
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path
import random
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = '1.3.1'
TELEMETRY_FIELDS = ['offset_ms', 'gear', 'speed_kmh', 'steering_deg', 'turn_signal',
                    'accel_x_mps2', 'accel_y_mps2', 'accel_z_mps2', 'ap_raw_state', 'accelerator_pct', 'brake_pressed']
LEGACY_FIELDS = TELEMETRY_FIELDS[:9]


def finite(value, limit):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or abs(value) > limit:
        return None
    return value


def brake_value(value):
    if value is None or (isinstance(value, float) and value != value):
        return None
    return bool(value)


def telemetry_samples(frames, duration_ms=None):
    samples, previous = [], -1
    for f in frames:
        if len(samples) >= 4000:
            break
        offset = round(f[0] * 1000)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset <= previous or offset < 0:
            continue
        if duration_ms is not None and offset > duration_ms:
            continue
        pedal = finite(f[3], 100)
        state = f[7]
        samples.append([
            offset, f[1] if f[1] in ('P', 'D', 'R', 'N') else None, finite(f[2], 10000), finite(f[4], 10000),
            {0: 'off', 1: 'left', 2: 'right', 3: 'both'}.get(f[6]),
            finite(f[11], 10000), finite(f[12], 10000), finite(f[13], 10000),
            state if isinstance(state, int) and not isinstance(state, bool) and 0 <= state <= 255 else None,
            None if pedal is None or pedal < 0 else pedal, brake_value(f[5])])
        previous = offset
    return {'fields': TELEMETRY_FIELDS, 'samples': samples}


def conform_clip(clip):
    """Make a queued clip satisfy the cloud contract without dropping the whole batch."""
    duration = clip.get('duration_ms')
    if isinstance(duration, bool) or not isinstance(duration, int):
        return clip
    telemetry = clip.get('telemetry') if isinstance(clip.get('telemetry'), dict) else None
    rows = telemetry.get('samples') if telemetry and isinstance(telemetry.get('samples'), list) else None
    width = 11 if (telemetry and telemetry.get('fields') == TELEMETRY_FIELDS) or (rows and isinstance(rows[0], list) and len(rows[0]) == 11) else 9
    samples, previous = [], -1
    for r in rows or []:
        if len(samples) >= 4000:
            break
        if not isinstance(r, list) or len(r) != width:
            continue
        offset = r[0]
        if isinstance(offset, bool) or not isinstance(offset, int) or offset <= previous or offset < 0 or offset > duration:
            continue
        row = list(r)
        if row[1] not in ('P', 'D', 'R', 'N'):
            row[1] = None
        if row[4] not in ('off', 'left', 'right', 'both'):
            row[4] = None
        for i in (2, 3, 5, 6, 7):
            row[i] = finite(row[i], 10000)
        if isinstance(row[8], bool) or not isinstance(row[8], int) or not 0 <= row[8] <= 255:
            row[8] = None
        if width == 11:
            pedal = finite(row[9], 100)
            row[9] = None if pedal is None or pedal < 0 else pedal
            row[10] = row[10] if isinstance(row[10], bool) else None
        previous = offset
        samples.append(row)
    segments, last = [], 0
    for s in clip.get('ap_segments') or []:
        if len(segments) >= 600 or not isinstance(s, dict):
            continue
        start, end, state = s.get('start_offset_ms'), s.get('end_offset_ms'), s.get('raw_state')
        if any(isinstance(v, bool) or not isinstance(v, int) for v in (start, end, state)):
            continue
        if start < last or end <= start or end > duration or not 0 <= state <= 255:
            continue
        segments.append({'start_offset_ms': start, 'end_offset_ms': end, 'raw_state': state})
        last = end
    status = clip.get('telemetry_status')
    if status not in ('available', 'no_sei', 'no_ap') or (status == 'available') != bool(segments):
        status = 'available' if segments else ('no_ap' if samples else 'no_sei')
    event = None
    raw_event = clip.get('event')
    if isinstance(raw_event, dict):
        event = {k: raw_event[k] for k in ('timestamp', 'city', 'street', 'reason')
                 if isinstance(raw_event.get(k), str) and len(raw_event[k]) <= 256}
    out = dict(clip)
    out['ap_segments'] = segments
    out['telemetry_status'] = status
    out['event'] = event
    if telemetry is not None:
        out['telemetry'] = {'fields': TELEMETRY_FIELDS if width == 11 else LEGACY_FIELDS, 'samples': samples}
    return out
STAMP = re.compile(r'^(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})-(.+)\.mp4$', re.I)
CATS = ('RecentClips', 'SavedClips', 'SentryClips')
MAX_PENDING = 10000
MAX_BYTES = 64 * 1024 * 1024
PARSE_WINDOW_SECONDS = 300


class UploadDeferred(Exception):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a device credential to another origin.


def origin(value, local=False):
    url = urllib.parse.urlsplit(value)
    if (url.scheme != 'https' and not (local and url.scheme == 'http' and url.hostname in ('127.0.0.1', 'localhost'))) or not url.hostname:
        raise ValueError('HTTPS cloud origin required')
    if url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
        raise ValueError('cloud URL must contain only an origin')
    return value.rstrip('/')


def post(server, endpoint, token, body):
    req = urllib.request.Request(server + endpoint, data=canonical(body).encode(), method='POST',
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
                 'User-Agent': 'SentryCloudSync/' + VERSION})
    with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as response:
        data = json.loads(response.read(1024 * 1024))
    if data.get('ok') is not True or not isinstance(data.get('data'), dict):
        raise ValueError('cloud did not acknowledge the request')
    return data['data']


def private_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w') as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(canonical(value))
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def open_queue(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path))
    os.chmod(path, 0o600)
    db.executescript('''
      PRAGMA journal_mode=WAL;
      PRAGMA synchronous=FULL;
      CREATE TABLE IF NOT EXISTS clips (
        clip_id TEXT PRIMARY KEY, signature TEXT NOT NULL, revision INTEGER NOT NULL,
        payload TEXT NOT NULL, pending INTEGER NOT NULL DEFAULT 1, updated_at REAL NOT NULL
      );
      CREATE TABLE IF NOT EXISTS progress (
        clip_id TEXT PRIMARY KEY, phase TEXT NOT NULL, updated_at REAL NOT NULL,
        source TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT ''
      );
      CREATE TABLE IF NOT EXISTS failures (clip_id TEXT PRIMARY KEY, retry_at REAL NOT NULL);
      CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    ''')
    return db


def progress(db, clip_id, phase, source='', detail='', clock=time.time):
    with db:
        db.execute("INSERT INTO progress VALUES (?,?,?,?,?) ON CONFLICT(clip_id) DO UPDATE SET phase=excluded.phase, updated_at=excluded.updated_at, source=CASE WHEN excluded.source='' THEN progress.source ELSE excluded.source END, detail=excluded.detail",
                   (clip_id, phase, clock(), source, detail))


def segments(frames, duration_ms):
    out = []
    # Only bridge adjacent samples, never extrapolate through a gap or the tail.
    for a, b in zip(frames, frames[1:]):
        start, end = round(a[0] * 1000), round(b[0] * 1000)
        state = a[7]
        if type(state) is not int or not 0 <= state <= 255 or start < 0 or end > duration_ms or not 0 < end - start <= 500:
            continue
        if out and out[-1]['end_offset_ms'] == start and out[-1]['raw_state'] == state:
            out[-1]['end_offset_ms'] = end
        else:
            out.append(dict(start_offset_ms=start, end_offset_ms=end, raw_state=state))
    if len(out) > 600:
        raise ValueError('too many state changes')
    return out


def extract(viewer, path):
    result = subprocess.run([sys.executable, str(viewer), '--cloud-telemetry', str(path)],
                            capture_output=True, timeout=120,
                            env=dict(os.environ, TSF_ARCHIVE=str(path.parents[2])))
    if result.returncode:
        raise ValueError('extractor failed')
    return json.loads(result.stdout)


def discover(archive):
    root = Path(archive).resolve()
    for cat in CATS:
        base = root / cat
        if not base.is_dir() or base.is_symlink():
            continue
        for folder in sorted(base.iterdir(), reverse=True):
            if folder.is_symlink() or not folder.is_dir():
                continue
            groups = {}
            for path in folder.iterdir():
                m = STAMP.match(path.name)
                if m and path.is_file() and not path.is_symlink():
                    groups.setdefault(m[1], []).append(path)
            for stamp, files in sorted(groups.items(), reverse=True):
                relative = str(folder.relative_to(root) / stamp)
                # Prefer front, but use another camera if it has the only usable SEI.
                files.sort(key=lambda p: (not p.name.endswith('-front.mp4'), p.name))
                yield digest(relative), stamp, files


def collect(db, archive, viewer, offset, budget=10, extractor=extract, clock=time.time):
    stats = {'queued': 0, 'failed': 0, 'backpressure': False}
    pending, size = db.execute('SELECT count(*),coalesce(sum(length(payload)),0) FROM clips WHERE pending=1').fetchone()
    if pending >= MAX_PENDING or size >= MAX_BYTES:
        stats['backpressure'] = True
        return stats
    processed = 0
    for clip_id, stamp, files in discover(archive):
        if processed >= budget:
            break
        try:
            parsed = dt.datetime.strptime(stamp, '%Y-%m-%d_%H-%M-%S')
            recorded_at = parsed.replace(tzinfo=dt.timezone(dt.timedelta(minutes=offset))).timestamp()
            # Use recording time, not mtime: copying an old video must not trigger parsing.
            if not 0 <= clock() - recorded_at <= PARSE_WINDOW_SECONDS:
                continue
            before = [(p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in files]
            if any(clock() - p.stat().st_mtime < 5 for p in files):
                continue
            event_file = files[0].parent / 'event.json'
            event = None
            if event_file.is_file() and not event_file.is_symlink():
                if event_file.stat().st_size <= 8192:
                    raw = json.loads(event_file.read_text())
                    if isinstance(raw, dict):
                        event = {k: raw[k] for k in ('timestamp', 'city', 'street', 'reason')
                                 if isinstance(raw.get(k), str) and len(raw[k]) <= 256}
            signature = digest(canonical([before, event, 'telemetry-v2']))
            row = db.execute('SELECT signature,revision,pending FROM clips WHERE clip_id=?', (clip_id,)).fetchone()
            if row and (row[0] == signature or row[2]):
                continue  # Never overwrite an unacknowledged revision.
            failure = db.execute('SELECT retry_at FROM failures WHERE clip_id=?', (clip_id,)).fetchone()
            if failure and failure[0] > clock():
                continue
            processed += 1
            tel = None
            for path in files:
                try:
                    progress(db, clip_id, 'parsing', path.name, clock=clock)
                    candidate = extractor(viewer, path)
                    if tel is None or candidate.get('frames'):
                        tel = candidate
                    if candidate.get('frames'):
                        break
                except (ValueError, OSError, subprocess.SubprocessError):
                    continue
            if tel is None:
                raise ValueError('no complete camera file')
            after = [(p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in files]
            if before != after:
                raise ValueError('file changed during extraction')
            duration = round(tel['duration'] * 1000)
            if not 1 <= duration <= 600000:
                raise ValueError('invalid duration')
            start = int(recorded_at * 1000)
            ap = segments(tel.get('frames', []), duration)
            payload = dict(clip_id=clip_id, revision=row[1] + 1 if row else 1, started_at_ms=start,
                           duration_ms=duration, utc_offset_minutes=offset,
                           telemetry_status='available' if ap else ('no_ap' if tel.get('frames') else 'no_sei'),
                           ap_segments=ap, event=event, telemetry=telemetry_samples(tel.get('frames', []), duration))
            encoded = canonical(payload)
            if pending + 1 > MAX_PENDING or size + len(encoded) > MAX_BYTES:
                progress(db, clip_id, 'waiting', detail='backpressure', clock=clock)
                stats['backpressure'] = True
                break
            with db:
                db.execute('INSERT OR REPLACE INTO clips VALUES (?,?,?,?,1,?)',
                           (clip_id, signature, payload['revision'], encoded, clock()))
                db.execute('DELETE FROM failures WHERE clip_id=?', (clip_id,))
            pending += 1
            size += len(encoded)
            progress(db, clip_id, 'queued', detail=payload['telemetry_status'], clock=clock)
            stats['queued'] += 1
        except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            with db:
                db.execute('INSERT OR REPLACE INTO failures VALUES (?,?)', (clip_id, clock() + 300))
            progress(db, clip_id, 'parse_failed', detail=type(exc).__name__, clock=clock)
            stats['failed'] += 1
    return stats


def prune(db, archive):
    alive = {clip_id for clip_id, _, _ in discover(archive)}
    with db:
        for (clip_id,) in db.execute('SELECT clip_id FROM clips WHERE pending=0 AND updated_at < ?', (time.time() - 7 * 86400,)).fetchall():
            if clip_id not in alive:
                db.execute('DELETE FROM clips WHERE clip_id=? AND pending=0', (clip_id,))
        for (clip_id,) in db.execute('SELECT clip_id FROM failures').fetchall():
            if clip_id not in alive:
                db.execute('DELETE FROM failures WHERE clip_id=?', (clip_id,))

        for (clip_id,) in db.execute('SELECT clip_id FROM progress WHERE updated_at < ?', (time.time() - 7 * 86400,)).fetchall():
            if clip_id not in alive and not db.execute('SELECT 1 FROM clips WHERE clip_id=?', (clip_id,)).fetchone():
                db.execute('DELETE FROM progress WHERE clip_id=?', (clip_id,))


def take_retry(db, seen):
    """A clicked failure writes a new token. Return it and whether this loop should upload now."""
    row = db.execute("SELECT value FROM state WHERE key='retry_requested'").fetchone()
    token = row[0] if row and row[0] else ''
    return token, bool(token) and token != seen


def error_detail(exc):
    if not isinstance(exc, urllib.error.HTTPError):
        return type(exc).__name__
    try:
        raw = exc.read() or b''
    except Exception:
        raw = b''
    try:
        name = json.loads(raw.decode()).get('error', {}).get('code')
        if isinstance(name, str) and name:
            return name
    except Exception:
        pass
    return 'HTTP_%d' % exc.code


def quarantine(db, clip_id, detail):
    with db:
        db.execute('INSERT OR REPLACE INTO failures VALUES (?,?)', (clip_id, time.time() + 6 * 3600))
    progress(db, clip_id, 'upload_failed', detail=detail)
    print('Rejected clip %s: %s' % (clip_id, detail), flush=True)


def commit_upload(db, rows):
    with db:
        db.executemany("UPDATE clips SET pending=0,payload='',updated_at=? WHERE clip_id=? AND revision=?",
                       [(time.time(), clip_id, revision) for clip_id, revision in rows])
        db.executemany('DELETE FROM failures WHERE clip_id=?', [(clip_id,) for clip_id, _revision in rows])


def upload(db, config, sender=None):
    sender = sender or post
    rows = db.execute('''SELECT c.clip_id,c.revision,c.payload FROM clips c
      LEFT JOIN failures f ON f.clip_id=c.clip_id
      WHERE c.pending=1 AND (f.retry_at IS NULL OR f.retry_at<=?)
      ORDER BY c.updated_at LIMIT 10''', (time.time(),)).fetchall()
    if not rows:
        return 0
    selected, size = [], 0
    for row in rows:
        clip = conform_clip(json.loads(row[2]))
        encoded = canonical(clip)
        if encoded != row[2]:
            with db:
                db.execute('UPDATE clips SET payload=? WHERE clip_id=? AND pending=1', (encoded, row[0]))
            row = (row[0], row[1], encoded)
        size += len(row[2])
        if selected and size > 900 * 1024:
            break
        selected.append(row)
    rows = selected

    def deliver(group):
        clips = [json.loads(r[2]) for r in group]
        # Payload determines batch ID, so retries across process restarts use the same key.
        batch_id = digest(canonical(clips))
        for row in group:
            progress(db, row[0], 'uploading')
        result = sender(config['server'], '/api/sentry/ingest', config['device_token'],
                        dict(schema_version=1, batch_id=batch_id, clips=clips))
        expected = {(r[0], r[1]) for r in group}
        accepted = {(r['clip_id'], r['revision']) for r in result.get('accepted', [])}
        if result.get('batch_id') != batch_id or accepted != expected:
            raise ValueError('incomplete acknowledgement')
        commit_upload(db, [(clip_id, revision) for clip_id, revision in sorted(expected)])
        for row in group:
            progress(db, row[0], 'uploaded')
        return len(group)

    try:
        return deliver(rows)
    except urllib.error.HTTPError as exc:
        if exc.code != 400:
            for row in rows:
                progress(db, row[0], 'upload_failed', detail=error_detail(exc))
            raise
        if len(rows) == 1:
            quarantine(db, rows[0][0], error_detail(exc))
            return 0
        sent = 0
        for row in rows:
            try:
                sent += deliver([row])
            except urllib.error.HTTPError as one:
                if one.code != 400:
                    for pending in rows:
                        progress(db, pending[0], 'upload_failed', detail=error_detail(one))
                    raise
                quarantine(db, row[0], error_detail(one))
            except (ValueError, OSError, KeyError, TypeError) as exc:
                progress(db, row[0], 'upload_failed', detail=error_detail(exc))
                raise
        return sent
    except (ValueError, OSError, KeyError, TypeError) as exc:
        for row in rows:
            progress(db, row[0], 'upload_failed', detail=error_detail(exc))
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('enroll', 'run', 'status'))
    parser.add_argument('--config', type=Path, default=Path('/opt/tesla-sentry/cloud.json'))
    parser.add_argument('--server', default='https://www.myteslacloud.com')
    parser.add_argument('--name', default='Sentry Pi')
    parser.add_argument('--state', type=Path, default=Path('/var/lib/tesla-sentry-cloud/queue.sqlite'))
    parser.add_argument('--archive', type=Path, default=Path(os.environ.get('TSF_ARCHIVE', '/archive')))
    parser.add_argument('--viewer', type=Path, default=Path(__file__).resolve().with_name('sentry-viewer.py'))
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--allow-local-http', action='store_true', help='development only: permit loopback HTTP')
    args = parser.parse_args(argv)
    os.umask(0o077)
    resources = ExitStack()
    try:
        if args.command == 'enroll':
            if args.config.exists():
                raise ValueError('configuration exists; revoke the old device before rebinding')
            server = origin(args.server, args.allow_local_http)
            code = getpass.getpass('一次性绑定凭据（输入不回显）: ').strip()
            if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', code):
                raise ValueError('invalid pairing code')
            config = post(server, '/api/sentry/enroll', code, {'name': args.name})
            config['server'] = server
            private_json(args.config, config)
            print('Device enrolled. Configuration saved privately; do not share it.')
            return 0
        if args.command == 'status':
            if not args.state.exists():
                print('No queue database yet.')
                return 0
            db = sqlite3.connect('file:' + str(args.state.resolve()) + '?mode=ro', uri=True)
            resources.callback(db.close)
            print('pending=%d total=%d' % db.execute('SELECT coalesce(sum(pending),0),count(*) FROM clips').fetchone())
            for key, value in db.execute('SELECT key,value FROM state'):
                print('%s=%s' % (key, value))
            db.close()
            return 0
        config = json.loads(args.config.read_text())
        config['server'] = origin(config['server'], args.allow_local_http)
        if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', config['device_token']):
            raise ValueError('invalid device credential')
        offset = config['utc_offset_minutes']
        if type(offset) is not int or abs(offset) > 840:
            raise ValueError('invalid recording timezone offset')
        args.state.parent.mkdir(parents=True, exist_ok=True)
        lock = resources.enter_context(open(str(args.state) + '.lock', 'a'))
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        db = open_queue(args.state)
        resources.callback(db.close)
        identity = digest(config['server'] + '|' + config['device_id'])
        previous = db.execute("SELECT value FROM state WHERE key='device_identity'").fetchone()
        if previous and previous[0] != identity:
            raise ValueError('use a new queue database after rebinding')
        with db:
            db.execute("INSERT OR REPLACE INTO state VALUES ('device_identity',?)", (identity,))
        retry, last_heartbeat, last_prune, next_upload, previous_error = 5, 0, 0, 0, ''
        seen_retry, _ = take_retry(db, '')
        while True:
            # Yield after each clip so extraction never delays its upload behind other videos.
            token, requested = take_retry(db, seen_retry)
            if requested:
                seen_retry, next_upload, retry, previous_error = token, 0, 5, ''
            stats = collect(db, args.archive, args.viewer, offset, budget=1)
            error, delay = '', 10
            try:
                if time.time() < next_upload:
                    # Keep extracting every cycle while network requests are backed off.
                    raise UploadDeferred(previous_error)
                sent = upload(db, config)
                while sent and db.execute('SELECT 1 FROM clips WHERE pending=1 LIMIT 1').fetchone():
                    sent += upload(db, config)
                if time.time() - last_heartbeat > 300:
                    pending = db.execute('SELECT count(*) FROM clips WHERE pending=1').fetchone()[0]
                    post(config['server'], '/api/sentry/heartbeat', config['device_token'],
                         {'queue_pending': pending, 'client_version': VERSION})
                    last_heartbeat = time.time()
                retry = 5
                next_upload = 0
                if sent:
                    delay = 1
            except urllib.error.HTTPError as e:
                error = 'HTTP_%d' % e.code
                cooldown = 900 if e.code in (400, 401, 403, 409, 413, 415) else retry
                retry_after = e.headers.get('Retry-After', '') if e.headers else ''
                if retry_after.isdigit():
                    cooldown = max(cooldown, min(3600, int(retry_after)))
                next_upload = time.time() + cooldown
                retry = min(300, retry * 2)
            except (ValueError, OSError, KeyError):
                error = 'network_or_ack_error'
                next_upload = time.time() + retry
                retry = min(300, retry * 2)
            except UploadDeferred as e:
                error = str(e)
            if error and error != previous_error:
                print('Upload deferred: ' + error, flush=True)
            previous_error = error
            with db:
                for key, value in dict(last_cycle=str(int(time.time())), last_error=error,
                                       extraction_failures=str(stats['failed']), backpressure=str(stats['backpressure']),
                                       next_upload_at=str(int(next_upload))).items():
                    db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (key, value))
            if time.time() - last_prune > 3600:
                prune(db, args.archive)
                last_prune = time.time()
            if args.once:
                db.close()
                return 1 if error else 0
            time.sleep(delay + random.uniform(0, min(5, delay / 10)))
    except (ValueError, OSError, KeyError, sqlite3.Error):
        print('Cloud sync failed: check configuration, device binding, file access and queue state.', file=sys.stderr)
        return 1
    finally:
        resources.close()


if __name__ == '__main__':
    sys.exit(main())
