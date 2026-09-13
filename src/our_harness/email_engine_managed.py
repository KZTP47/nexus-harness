"""On-demand pinned Windows mailbox services, owned by this project only.

Secrets are sealed by the caller's secret store. Executables are downloaded on
explicit setup, never copied from a developer installation or registered as
global services. Ports are allocated once and retained with durable Redis data.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import threading
import time
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit

from .email_engine_runtime import EmailEngineRuntime, redis_preflight
from .models import HarnessError

CONTRACT = 'nexus-managed-mail/ee2.80.1-redis8.10.1-v1'
ARTIFACTS = {
    'emailengine': dict(url='https://github.com/postalsys/emailengine/releases/download/v2.80.1/emailengine.exe',
                        sha256='13e05eab6b2d18eff74c885be774f08e04da8220a9d4d2e2c5287de2845fbc24',
                        bytes=144507309, filename='emailengine-2.80.1.exe'),
    'redis': dict(url='https://github.com/redis-windows/redis-windows/releases/download/8.10.1/Redis-8.10.1-Windows-x64-cygwin.zip',
                  sha256='5532f2cc38a0185556b648d25a0c2ff1cc58029f37fd76eceb38736976dcb056',
                  bytes=14769085, filename='redis-8.10.1.zip'),
}
REDIS_ENTRY = 'Redis-8.10.1-Windows-x64-cygwin/redis-server.exe'


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


class _ArtifactRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, target):
        parsed = urlsplit(target)
        if (parsed.scheme != 'https' or parsed.hostname not in
                {'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com', 'github-releases.githubusercontent.com'}
                or parsed.username or parsed.password):
            raise HarnessError('Runtime download redirected outside the pinned release hosts.')
        return super().redirect_request(request, fp, code, message, headers, target)


def _download(spec, target):
    """Bounded download, verified before promotion; retain working cached files."""
    target = Path(target)
    if target.is_file() and target.stat().st_size == spec['bytes'] and _hash(target) == spec['sha256']:
        return target
    temporary = target.with_suffix(target.suffix + '.download')
    try:
        digest, count = hashlib.sha256(), 0
        opener = urllib.request.build_opener(_ArtifactRedirect())
        with opener.open(urllib.request.Request(spec['url'], headers={'User-Agent': 'Nexus-Mail-Runtime'}), timeout=40) as response, temporary.open('wb') as output:
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > spec['bytes']:
                    raise HarnessError('Runtime download exceeded its pinned size.')
                digest.update(chunk)
                output.write(chunk)
        if count != spec['bytes'] or digest.hexdigest() != spec['sha256']:
            raise HarnessError('Runtime download failed its pinned checksum. Retry setup.')
        temporary.replace(target)
        return target
    except HarnessError:
        raise
    except Exception:
        raise HarnessError('Runtime download failed. Check your connection and retry setup.') from None
    finally:
        if temporary.is_file():
            temporary.unlink()


def _extract(archive, directory):
    """Validate every entry before writing; no traversal, links, ADS or zip bombs."""
    directory = Path(directory)
    if directory.is_symlink():
        raise HarnessError('Mailbox runtime install directory cannot be a symbolic link.')
    directory = directory.resolve()
    with zipfile.ZipFile(archive) as source:
        entries, total = [], 0
        for entry in source.infolist():
            normalized = entry.filename.replace('\\', '/')
            path = PurePosixPath(normalized)
            total += entry.file_size
            if (not normalized or path.is_absolute() or any(part in ('..', '.') or ':' in part or part.endswith((' ', '.'))
                    or part.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *('COM' + str(i) for i in range(1, 10)), *('LPT' + str(i) for i in range(1, 10))} for part in path.parts)
                    or stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1
                    or total > 150 * 1024 * 1024 or len(entries) >= 2000):
                raise HarnessError('Unsafe mailbox runtime archive.')
            destination = directory.joinpath(*path.parts)
            if not destination.resolve().is_relative_to(directory) or any(parent.is_symlink() for parent in (destination, *destination.parents) if parent.is_relative_to(directory)):
                raise HarnessError('Mailbox runtime archive escapes its install directory.')
            entries.append((entry, destination))
        for entry, destination in entries:
            if entry.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source.open(entry) as incoming, destination.open('wb') as output:
                remaining = entry.file_size
                while chunk := incoming.read(min(1024 * 1024, remaining + 1)):
                    remaining -= len(chunk)
                    if remaining < 0:
                        raise HarnessError('Mailbox runtime archive size mismatch.')
                    output.write(chunk)
                if remaining:
                    raise HarnessError('Mailbox runtime archive was incomplete.')


class ManagedEmailEngine:
    def __init__(self, root, secret_store):
        self.root = Path(root).resolve() / 'managed-mail-service'
        self.secrets = secret_store
        self._lock = threading.RLock()
        self._lease = None
        self._redis = None
        self._tree = None
        self._runtime = None
        self._settings = None
        self._starting = False
        self._stop = threading.Event()
        self._supervisor = None
        self._redis_executable = None
        self._restarts = 0
        self._message = 'Prepare the local mailbox service to download its verified runtime.'

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps([CONTRACT, str(self.root)], sort_keys=True).encode()).hexdigest()

    def _acquire(self):
        # Reuse the runtime's cross-platform file lease without starting another service.
        holder = EmailEngineRuntime(self.root / 'manager-lease')
        holder._acquire()
        self._lease = holder

    def _load(self):
        path = self.root / 'settings.json'
        if path.is_file():
            try:
                sealed = json.loads(path.read_text(encoding='utf-8'))
                if sealed['schema_version'] != 1 or sealed['contract'] != CONTRACT:
                    raise ValueError()
                if sealed.get('configuration_fingerprint') != self.fingerprint:
                    raise HarnessError('The local mailbox service folder or runtime contract changed. Restore its original location or configure a separate service; existing data has been retained.')
                settings = json.loads(self.secrets.unprotect(sealed['encrypted']))
                for name in ('redis_password', 'secret'):
                    if not isinstance(settings[name], str) or len(settings[name]) < 32:
                        raise ValueError()
                if not all(isinstance(settings[name], int) and 1 <= settings[name] <= 65535 for name in ('redis_port', 'port')):
                    raise ValueError()
                return settings
            except HarnessError:
                raise
            except Exception:
                raise HarnessError('Local mailbox settings could not be unlocked. Saved mailbox data has been retained.') from None
        with socket.socket() as redis_port, socket.socket() as api_port:
            redis_port.bind(('127.0.0.1', 0))
            api_port.bind(('127.0.0.1', 0))
            settings = dict(redis_port=redis_port.getsockname()[1], port=api_port.getsockname()[1],
                            redis_password=secrets.token_hex(32), secret=secrets.token_hex(32), token='')
        self._save(settings)
        return settings

    def _save(self, settings):
        target = self.root / 'settings.json'
        temporary = target.with_suffix('.tmp')
        sealed = dict(schema_version=1, contract=CONTRACT, configuration_fingerprint=self.fingerprint,
                      encrypted=self.secrets.protect(json.dumps(settings)))
        temporary.write_text(json.dumps(sealed), encoding='utf-8')
        temporary.replace(target)

    def _provision(self):
        if os.name != 'nt':
            raise HarnessError('Automatic local mailbox service installation currently supports Windows x64. Configure an external service on this platform.')
        cache = self.root / 'runtime'
        cache.mkdir(parents=True, exist_ok=True)
        ee = _download(ARTIFACTS['emailengine'], cache / ARTIFACTS['emailengine']['filename'])
        archive = _download(ARTIFACTS['redis'], cache / ARTIFACTS['redis']['filename'])
        redis_root = cache / 'redis-8.10.1'
        # Re-extract the verified small distribution; do not trust stale unpacked binaries.
        _extract(archive, redis_root)
        executable = redis_root / REDIS_ENTRY
        if not executable.is_file():
            raise HarnessError('Verified Redis package did not contain its expected executable.')
        return ee, executable

    def _start_redis(self, executable, settings):
        data = self.root / 'redis-data'
        data.mkdir(parents=True, exist_ok=True)
        environment = {k: v for k, v in os.environ.items() if k not in {'REDIS_URL', 'REDISCLI_AUTH'}}
        options = dict(cwd=data, env=environment, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        command = [str(executable), '-']
        if self._tree:
            self._tree.close()
            self._tree = None
        if os.name == 'nt':
            from .execution import _start_windows_contained_process
            flags = subprocess.CREATE_NO_WINDOW | 0x00000004
            self._redis, self._tree = _start_windows_contained_process(
                lambda: subprocess.Popen(command, **options, creationflags=flags | 0x01000000),
                lambda: subprocess.Popen(command, **options, creationflags=flags), label='Nexus mailbox Redis')
        else:
            self._redis = subprocess.Popen(command, **options)
        config = ('bind 127.0.0.1\nprotected-mode yes\nport ' + str(settings['redis_port']) + '\nrequirepass '
                  + settings['redis_password'] + '\nappendonly yes\nappendfsync always\nsave ""\nmaxmemory-policy noeviction\ndir .\ndaemonize no\n')
        self._redis.stdin.write(config.encode())
        self._redis.stdin.close()

    def _issue_token(self, executable, runtime, redis_url, settings):
        environment = {k: v for k, v in os.environ.items() if not k.startswith('EENGINE_') and k not in {'REDIS_URL', 'NODE_OPTIONS', 'NODE_CONFIG_PATH'}}
        environment.update(EENGINE_REDIS=redis_url, EENGINE_REDIS_PREFIX='{nexus-' + runtime.fingerprint[:24] + '}',
                           EENGINE_SECRET=settings['secret'], EENGINE_LOG_LEVEL='error', EENGINE_UPDATE_CHECK_DISABLED='true')
        result = subprocess.run([str(executable), 'tokens', 'issue', '--scope', 'api', '--description', 'Nexus local mailbox'],
                                cwd=self.root, env=environment, stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        token = result.stdout.decode('utf-8', errors='replace').strip()
        if result.returncode or not re.fullmatch('[0-9a-fA-F]{64}', token):
            raise HarnessError('Local mailbox service could not create its private API token.')
        return token

    @staticmethod
    def _configure_hosted_authentication(url, token):
        """Owned service URLs must follow the actual listener, never the caller's UI URL.

        EmailEngine's POST /v1/authentication/form requires serviceUrl. Health and
        account listing succeed without it, so API liveness alone misses this
        first-user failure. Reapply on restart to repair older local installs.
        """
        from .email_engine import EmailEngineClient, validate_base_url
        service_url = validate_base_url(url)
        parsed = urlsplit(service_url)
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port or parsed.path:
            raise HarnessError('Managed mailbox authentication requires its own loopback service address.')
        client = EmailEngineClient(service_url, token)
        updated = client._request('POST', '/v1/settings', payload={'serviceUrl': service_url})
        if 'serviceUrl' not in updated.get('updated', []):
            raise HarnessError('Local mailbox service could not configure its sign-in address.')
        client.accounts()

    def start(self):
        with self._lock:
            if self._runtime and self.status()['ready']:
                url = self._runtime.status()['url']
                self._configure_hosted_authentication(url, self._settings['token'])
                return {'url': url, 'token': self._settings['token']}
            self.close()
            self.root.mkdir(parents=True, exist_ok=True)
            self._acquire()
            self._starting = True
            stage = 'unlocking private settings'
            try:
                self._message = 'Preparing verified mailbox service runtime…'
                settings = self._load()
                self._settings = settings
                stage = 'downloading and verifying the runtime'
                ee, redis = self._provision()
                self._redis_executable = redis
                stage = 'reserving the saved local ports'
                # Fail rather than attach to or kill an unrelated process using the saved ports.
                for port in (settings['redis_port'], settings['port']):
                    with socket.socket() as reservation:
                        reservation.bind(('127.0.0.1', port))
                redis_url = 'redis://:' + quote(settings['redis_password'], safe='') + '@127.0.0.1:' + str(settings['redis_port']) + '/0'
                stage = 'starting the private mailbox database'
                self._message = 'Starting the private mailbox database…'
                self._start_redis(redis, settings)
                deadline = time.monotonic() + 20
                while True:
                    try:
                        redis_preflight(redis_url, timeout=1)
                        break
                    except HarnessError:
                        if self._redis.poll() is not None or time.monotonic() >= deadline:
                            raise HarnessError('The private Redis service did not become ready. Retry local setup.') from None
                        time.sleep(.1)
                self._runtime = EmailEngineRuntime(self.root, dict(mode='local', command=[str(ee)],
                    redis_url=redis_url, secret=settings['secret'], port=settings['port']))
                stage = 'starting EmailEngine'
                self._message = 'Starting EmailEngine…'
                result = self._runtime.start(timeout=60)
                stage = 'authorizing the local service API'
                if not settings.get('token'):
                    settings['token'] = self._issue_token(ee, self._runtime, redis_url, settings)
                    self._save(settings)
                stage = 'configuring the local mailbox sign-in address'
                self._configure_hosted_authentication(result['url'], settings['token'])
                self._message = 'Local mailbox service is ready. Add your mailbox and complete provider sign-in.'
                self._stop = threading.Event()
                self._restarts = 0
                self._supervisor = threading.Thread(target=self._watch_redis, args=(self._stop,), daemon=True, name='nexus-mail-redis')
                self._supervisor.start()
                return {'url': result['url'], 'token': settings['token']}
            except Exception as exc:
                self.close()
                detail = str(exc) if isinstance(exc, HarnessError) else 'Retry setup or configure an external service.'
                self._message = 'Local mailbox setup failed while ' + stage + '. ' + detail + ' Saved data is retained.'
                raise HarnessError(self._message) from None
            finally:
                self._starting = False

    def _watch_redis(self, stop):
        while not stop.wait(2):
            with self._lock:
                if stop.is_set():
                    return
                if self._redis is not None and self._redis.poll() is not None:
                    if self._restarts >= 3:
                        self._message = 'The private mailbox database repeatedly exited. Retry local setup after checking available disk space.'
                        return
                    self._restarts += 1
                    try:
                        self._start_redis(self._redis_executable, self._settings)
                    except Exception:
                        self._message = 'The private mailbox database restart failed. Saved data is retained.'

    def status(self):
        # Do not block UI snapshots behind a multi-minute first download.
        runtime = self._runtime
        service = runtime.status() if runtime and not self._starting else {}
        redis = self._redis
        ready = redis is not None and redis.poll() is None and service.get('ready', False)
        return dict(configured=(self.root / 'settings.json').is_file(), ready=ready,
                    state='preparing' if self._starting else 'ready' if ready else 'stopped', url=service.get('url', ''),
                    message=self._message, contract=CONTRACT, managed=True, restarts=self._restarts)

    def close(self):
        self._stop.set()
        with self._lock:
            if self._runtime:
                self._runtime.close()
                self._runtime = None
            if self._redis:
                if self._redis.poll() is None:
                    self._redis.terminate()
                    try:
                        self._redis.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self._redis.kill()
                        self._redis.wait(timeout=5)
                self._redis = None
            if self._tree:
                self._tree.close()
                self._tree = None
            if self._lease:
                self._lease.close()
                self._lease = None
