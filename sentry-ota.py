#!/usr/bin/env python3
"""Systemd bundle updater. Runs outside the viewer service; never updates user data."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

FILES = ('sentry-viewer.py', 'sentry-cloud-sync.py', 'sentry-ota.py', 'scripts/install-cloud-sync.sh')
UNIT = Path('/etc/systemd/system/sentry-cloud-sync.service')
RECOVERY = Path('/etc/systemd/system/tesla-sentry-ota-recover.service')


def atomic(path, data, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.ota-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            os.fchmod(f.fileno(), mode)
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(temp, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(temp): os.unlink(temp)


def save(path, data):
    atomic(path, json.dumps(data).encode(), 0o600)


def validate(stage):
    stage = Path(stage)
    manifest = json.loads((stage / 'version.json').read_text())
    if not re.fullmatch(r'\d+(?:\.\d+){1,3}', manifest.get('version', '')) or manifest.get('bundle_format') != 1:
        raise ValueError('invalid release manifest')
    files = manifest.get('files', {})
    if set(files) != set(FILES): raise ValueError('unexpected bundle files')
    for name in FILES:
        path = stage / name
        if path.is_symlink() or not path.is_file(): raise ValueError('invalid bundle file')
        data = path.read_bytes()
        if len(data) > 8 << 20 or hashlib.sha256(data).hexdigest() != files[name]: raise ValueError('checksum mismatch')
        if name.endswith('.py'):
            compile(data, name, 'exec')
        else:
            subprocess.run(['sh', '-n', str(path)], check=True, capture_output=True, timeout=30)
    data = (stage / FILES[0]).read_bytes()
    version = re.search(rb'^APP_VERSION\s*=\s*"([^"]+)"', data, re.M)
    if not version or version[1].decode() != manifest['version']: raise ValueError('version mismatch')
    subprocess.run([sys.executable, str(stage / FILES[0]), '--ota-check'], check=True, capture_output=True, timeout=90,
                   env=dict(os.environ, TSF_CACHE=str(stage / 'validation-cache'), TSF_AUTH_FILE='/dev/null'))
    subprocess.run([sys.executable, str(stage / FILES[1]), '--help'], check=True, capture_output=True, timeout=30)
    return manifest


def systemctl(*args, check=True):
    return subprocess.run(['systemctl', *args], check=check, capture_output=True, timeout=60)


def active(name):
    return systemctl('is-active', '--quiet', name, check=False).returncode == 0


def health(port, cloud):
    for _ in range(20):
        if active('sentry-viewer.service') and (not cloud or active('sentry-cloud-sync.service')):
            try:
                with urllib.request.urlopen('http://127.0.0.1:%d/api/auth/info' % port, timeout=2) as r:
                    if r.status == 200: return
            except urllib.error.HTTPError as e:
                if e.code == 401: return
            except OSError: pass
        time.sleep(1)
    raise RuntimeError('service health check failed')


def restore(root, journal):
    backup = root / '.ota' / journal['backup']
    systemctl('stop', 'sentry-cloud-sync.service', check=False)
    for item in journal['originals']:
        target = root / item['name'] if item['name'] != '@unit' else UNIT
        if item['existed']:
            atomic(target, (backup / item['backup']).read_bytes(), item['mode'])
        elif target.exists():
            target.unlink()
    systemctl('daemon-reload')
    systemctl('restart', 'sentry-viewer.service')
    if journal['cloud_active']: systemctl('start', 'sentry-cloud-sync.service')
    journal['phase'] = 'rolled_back'
    save(root / '.ota' / 'journal.json', journal)


def install(root, stage, port):
    manifest = validate(stage)
    ota = root / '.ota'
    prior = ota / 'journal.json'
    if prior.exists() and json.loads(prior.read_text())['phase'] == 'installing':
        raise RuntimeError('interrupted update needs recovery first')
    backup = Path(tempfile.mkdtemp(prefix='backup-', dir=ota))
    cloud_active = active('sentry-cloud-sync.service')
    journal = {'phase': 'prepared', 'version': manifest['version'], 'cloud_active': cloud_active,
               'backup': backup.name, 'originals': []}
    for index, name in enumerate((*FILES, '.ota/installed.json', '@unit')):
        target = UNIT if name == '@unit' else root / name
        if target.is_symlink(): raise ValueError('symlink update target')
        item = {'name': name, 'backup': str(index), 'existed': target.exists(), 'mode': 0o644}
        if target.exists():
            item['mode'] = target.stat().st_mode & 0o777
            atomic(backup / str(index), target.read_bytes(), item['mode'])
        journal['originals'].append(item)
    save(prior, journal)
    # Keep a separate verified recovery runner even if the installed updater is rolled back.
    atomic(ota / 'recovery.py', (stage / 'sentry-ota.py').read_bytes())
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+', str(root)): raise ValueError('unsupported installation path')
    unit = '''[Unit]
Description=Recover interrupted Tesla Sentry bundle update
After=local-fs.target
Before=sentry-viewer.service sentry-cloud-sync.service
[Service]
Type=oneshot
ExecStart=%s %s/.ota/recovery.py --root %s --recover
[Install]
WantedBy=multi-user.target
''' % (sys.executable, root, root)
    atomic(RECOVERY, unit.encode())
    systemctl('daemon-reload'); systemctl('enable', RECOVERY.name)
    journal['phase'] = 'installing'; save(prior, journal)
    try:
        if cloud_active: systemctl('stop', 'sentry-cloud-sync.service')
        for name in FILES:
            atomic(root / name, (stage / name).read_bytes(), 0o755)
        if (root / 'cloud.json').exists():
            subprocess.run(['sh', str(stage / 'scripts/install-cloud-sync.sh'), str(root)],
                           env=dict(os.environ, TSF_CLOUD_OTA='1'), check=True, capture_output=True, timeout=90)
        save(ota / 'installed.json', {'version': manifest['version'], 'files': manifest['files']})
        systemctl('restart', 'sentry-viewer.service')
        if cloud_active: systemctl('start', 'sentry-cloud-sync.service')
        health(port, cloud_active)
        journal['phase'] = 'complete'; save(prior, journal)
        for path in ota.iterdir():
            if path.is_dir() and ((path.name.startswith('backup-') and path != backup) or
                                  (path.name.startswith('stage-') and path.resolve() != stage.resolve())):
                shutil.rmtree(path, ignore_errors=True)
    except Exception:
        restore(root, journal)
        raise


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', required=True, type=Path)
    p.add_argument('--stage', type=Path)
    p.add_argument('--port', type=int, default=8080)
    p.add_argument('--rollback', action='store_true')
    p.add_argument('--recover', action='store_true')
    args = p.parse_args()
    root = args.root.resolve()
    if os.geteuid() != 0: p.error('root required')
    (root / '.ota').mkdir(exist_ok=True, mode=0o700)
    with open(root / '.ota' / 'lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            if not args.recover:
                error_file = root / '.ota' / 'last-error.json'
                if error_file.exists(): error_file.unlink()
                save(root / '.ota' / 'request.json', {'phase': 'running', 'at': int(time.time())})
            journal_path = root / '.ota' / 'journal.json'
            if args.rollback or args.recover:
                if not journal_path.exists():
                    if args.recover: return 0
                    raise ValueError('no bundle backup')
                journal = json.loads(journal_path.read_text())
                if args.recover and journal['phase'] != 'installing': return 0
                if args.rollback and journal['phase'] != 'complete': raise ValueError('no complete bundle to roll back')
                # During boot the recovery unit runs Before= viewer: do not wait for a restart job.
                if args.recover:
                    backup = root / '.ota' / journal['backup']
                    for item in journal['originals']:
                        target = UNIT if item['name'] == '@unit' else root / item['name']
                        if item['existed']: atomic(target, (backup / item['backup']).read_bytes(), item['mode'])
                        elif target.exists(): target.unlink()
                    systemctl('daemon-reload')
                    journal['phase'] = 'rolled_back'; save(journal_path, journal)
                else: restore(root, journal)
            else:
                if not args.stage: raise ValueError('stage required')
                time.sleep(2)  # Let the viewer finish sending its accepted response.
                install(root, args.stage.resolve(), args.port)
            return 0
        except Exception as e:
            save(root / '.ota' / 'last-error.json', {'error': type(e).__name__, 'at': int(time.time())})
            print('OTA failed; see journal and service status.', file=sys.stderr)
            return 1
        finally:
            save(root / '.ota' / 'request.json', {'phase': 'finished'})


if __name__ == '__main__': sys.exit(main())
