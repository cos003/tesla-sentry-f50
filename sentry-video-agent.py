#!/usr/bin/env python3
"""Read local archived recordings through a short-lived authenticated relay session."""
import argparse
import http.client
import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CATS = ('SentryClips', 'SavedClips', 'RecentClips')
MEDIA = re.compile(r'^/media/(SentryClips|SavedClips|RecentClips)/([\w. -]+)/([\w. -]+\.mp4)$', re.I)
RANGE = re.compile(r'^bytes=(\d+)-(\d*)$')


def request_json(url, token, method='GET'):
    req = urllib.request.Request(url, data=b'{}' if method == 'POST' else None, method=method,
                                 headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
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
                for file in sorted(folder.iterdir()):
                    if file.is_file() and re.fullmatch(r'[\w. -]+\.mp4', file.name, re.I):
                        videos.append({'url': '/media/%s/%s/%s' % (cat, folder.name, file.name),
                                       'cam': file.stem.rsplit('-', 1)[-1], 'f': file.name})
                if videos:
                    found.append({'name': folder.name, 'videos': videos})
                if len(found) >= 500:
                    break
        output[cat] = found
    return json.dumps(output, ensure_ascii=False).encode('utf-8')


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
            raise OSError('video gateway rejected result')
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
                job = request_json(session['origin'] + '/agent/jobs', session['ticket'])
                if not job:
                    continue
                if job.get('kind') == 'recordings':
                    body = events(archive)
                    send_result(session['origin'], session['ticket'], job, 200, body, 0, len(body), '')
                elif job.get('kind') == 'video':
                    status, source, start, length, content_range = file_job(archive, job.get('path'), job.get('range'))
                    send_result(session['origin'], session['ticket'], job, status, source, start, length, content_range)
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
