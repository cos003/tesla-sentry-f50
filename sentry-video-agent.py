#!/usr/bin/env python3
"""Read local archived recordings through a short-lived authenticated relay session."""
import argparse
import hashlib
import http.client
import json
import os
import random
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CATS = ('SentryClips', 'SavedClips', 'RecentClips')
MEDIA = re.compile(r'^/media/(SentryClips|SavedClips|RecentClips)/([\w. -]+)/([\w. -]+\.mp4)$', re.I)
RANGE = re.compile(r'^bytes=(\d+)-(\d*)$')
USER_AGENT = 'MyTeslaCloud-Sentry-Video/1'
THUMB_LIMIT = 512 * 1024
# 与 sentry-viewer.py 的 thumb_file 共用同一份 eMMC 抽帧缓存 (两个服务同一个 EnvironmentFile)。
# 服务本身 ProtectSystem=strict, 依赖 unit 里的 ReadWritePaths=$TSF_CACHE 才能回写。
THUMB_DIR = Path(os.environ.get('TSF_CACHE', '/opt/mu300')) / 'thumbs'


def request_json(url, token, method='GET'):
    req = urllib.request.Request(url, data=b'{}' if method == 'POST' else None, method=method,
                                 headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
                                          'User-Agent': USER_AGENT})
    with urllib.request.urlopen(req, timeout=35) as response:
        if response.status == 204:
            return None
        return json.load(response)


def events(archive):
    output = {}
    for cat in CATS:
        found = []
        base = archive / cat
        if base.is_dir():
            for folder in sorted(base.iterdir(), reverse=True):
                if not folder.is_dir() or not re.fullmatch(r'[\w. -]{1,80}', folder.name) or folder.name in ('.', '..'):
                    continue
                videos = []
                size = 0
                for file in sorted(folder.iterdir()):
                    if file.is_file() and re.fullmatch(r'[\w. -]+\.mp4', file.name, re.I):
                        videos.append({'url': '/media/%s/%s/%s' % (cat, folder.name, file.name),
                                       'cam': file.stem.rsplit('-', 1)[-1], 'f': file.name})
                        try:
                            size += file.stat().st_size
                        except OSError:
                            pass
                if videos:
                    meta = {}
                    try:
                        raw = json.loads((folder / 'event.json').read_text(encoding='utf-8'))
                        if isinstance(raw, dict):
                            meta = {key: raw[key] for key in ('reason', 'camera', 'timestamp', 'city', 'street')
                                    if key in raw and isinstance(raw[key], (str, int, float))}
                    except (OSError, ValueError):
                        pass
                    found.append({'name': folder.name, 'videos': videos, 'size': size, 'meta': meta,
                                  'thumbnail_supported': True})
                if len(found) >= 500:
                    break
        output[cat] = found
    return json.dumps(output, ensure_ascii=False).encode('utf-8')


def thumb_job(archive, path, second):
    status, source, _, _, _ = file_job(archive, path, '')
    if status != 200:
        return 404, b''
    try:
        second = float(second)
        if not 0 <= second <= 70:
            return 404, b''
    except (TypeError, ValueError):
        return 404, b''
    # 键与 sentry-viewer.py thumb_file 完全一致: realpath|size|mtime|秒(%g), 谁先生成都互相受益。
    target = os.path.realpath(str(source))
    try:
        stat = os.stat(target)
    except OSError:
        return 404, b''
    key = hashlib.md5(('%s|%d|%d|%g' % (target, stat.st_size, int(stat.st_mtime), second)).encode()).hexdigest()
    cached = THUMB_DIR / (key + '.jpg')
    try:
        data = cached.read_bytes()
        if data.startswith(b'\xff\xd8') and len(data) <= THUMB_LIMIT:
            return 200, data
        cached.unlink()  # 上次 ffmpeg 失败留下的残骸, 视为无效缓存
    except OSError:
        pass
    ffmpeg = '/opt/mu300/ffmpeg/ffmpeg' if Path('/opt/mu300/ffmpeg/ffmpeg').exists() else 'ffmpeg'
    tmp = THUMB_DIR / ('%s.%d.tmp.jpg' % (key, os.getpid()))
    for ss in ('%g' % second, '1', '0'):  # 指定秒可能落在片段末尾之外, 逐级回落
        try:
            result = subprocess.run([ffmpeg, '-hide_banner', '-loglevel', 'error', '-ss', ss,
                                     '-i', target, '-frames:v', '1', '-vf', 'scale=400:-2',
                                     '-q:v', '7', '-f', 'image2pipe', '-vcodec', 'mjpeg', '-'],
                                    capture_output=True, timeout=25, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return 503, b''
        if not result.returncode and result.stdout.startswith(b'\xff\xd8') and len(result.stdout) <= THUMB_LIMIT:
            try:  # 回写共享缓存; 写失败不影响本次返回
                THUMB_DIR.mkdir(parents=True, exist_ok=True)
                tmp.write_bytes(result.stdout)
                os.replace(tmp, cached)
            except OSError:
                pass
            return 200, result.stdout
    try:
        tmp.unlink()
    except OSError:
        pass
    return 404, b''


def file_job(archive, path, range_header):
    match = MEDIA.fullmatch(path or '')
    if not match or '..' in path:
        return 404, b'', 0, 0, ''
    file = archive.joinpath(*match.groups())
    if not file.is_file() or not str(file.resolve()).startswith(str(archive.resolve()) + os.sep):
        return 404, b'', 0, 0, ''
    size = file.stat().st_size
    start, end = 0, size - 1
    if range_header:
        match_range = RANGE.fullmatch(range_header)
        if not match_range:
            return 416, b'', 0, 0, ''
        start = int(match_range[1]); end = min(size - 1, int(match_range[2])) if match_range[2] else size - 1
        if start >= size or end < start:
            return 416, b'', 0, 0, ''
    length = end - start + 1
    return (206 if range_header else 200), file, start, length, 'bytes %d-%d/%d' % (start, end, size)


def send_result(origin, ticket, job, status, source, start, length, content_range):
    target = urllib.parse.urlsplit(origin)
    if target.scheme != 'https' or not target.hostname or target.username or target.password or target.path not in ('', '/'):
        raise ValueError('invalid video gateway origin')
    connection = http.client.HTTPSConnection(target.hostname, target.port or 443, timeout=60)
    try:
        connection.putrequest('POST', '/agent/result/' + job['job_id'])
        connection.putheader('Authorization', 'Bearer ' + ticket)
        connection.putheader('User-Agent', USER_AGENT)
        connection.putheader('X-Video-Status', str(status))
        connection.putheader('Content-Length', str(length))
        if status == 206:
            connection.putheader('X-Content-Range', content_range)
        connection.endheaders()
        if isinstance(source, bytes):
            connection.send(source)
        elif length:
            with open(source, 'rb') as stream:
                stream.seek(start)
                remaining = length
                while remaining:
                    chunk = stream.read(min(65536, remaining))
                    if not chunk:
                        raise OSError('recording changed during playback')
                    connection.send(chunk)
                    remaining -= len(chunk)
        response = connection.getresponse()
        response.read()
        if response.status != 204:
            # 多半是网关已经把该任务判超时并回了 503; 丢弃结果即可,
            # 不值得为它重启整个会话、重新登录和重握手。
            print('Video gateway dropped result %s: HTTP %d' % (job.get('job_id'), response.status), flush=True)
    finally:
        connection.close()


def serve(config, archive, once=False):
    retry = 3
    while True:
        try:
            session = request_json(config['server'].rstrip('/') + '/api/sentry/video-session', config['device_token'], 'POST')['data']
            if not session.get('origin') or not session.get('ticket'):
                raise ValueError('invalid video session')
            while time.time() < session['expires_at'] - 30:
                try:
                    job = request_json(session['origin'] + '/agent/jobs', session['ticket'])
                except urllib.error.HTTPError as error:
                    if error.code == 409:
                        continue  # 上一次长轮询还挂在网关没释放, 稍后再试, 别重启会话
                    raise
                if not job:
                    continue
                if job.get('kind') == 'recordings':
                    body = events(archive)
                    send_result(session['origin'], session['ticket'], job, 200, body, 0, len(body), '')
                elif job.get('kind') == 'video':
                    status, source, start, length, content_range = file_job(archive, job.get('path'), job.get('range'))
                    send_result(session['origin'], session['ticket'], job, status, source, start, length, content_range)
                elif job.get('kind') == 'thumbnail':
                    status, body = thumb_job(archive, job.get('path'), job.get('second', 1))
                    send_result(session['origin'], session['ticket'], job, status, body, 0, len(body), '')
            retry = 3
        except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError, http.client.HTTPException) as error:
            print('Video relay unavailable: %s' % type(error).__name__, flush=True)
            if once:
                return 1
            time.sleep(retry + random.uniform(0, 2))
            retry = min(60, retry * 2)
        if once:
            return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('/opt/tesla-sentry/cloud.json'))
    parser.add_argument('--archive', type=Path, default=Path(os.environ.get('TSF_ARCHIVE', '/archive')))
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    raise SystemExit(serve(json.loads(args.config.read_text()), args.archive, args.once))
