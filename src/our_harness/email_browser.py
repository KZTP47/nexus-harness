"""Owned browser mailbox sessions; never attaches to a user's ordinary profile."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import uuid
from .models import HarnessError


class EmailBrowser:
    MODE_CONTRACT = 'browser-mode/v1'

    def __init__(self, root, attachments_dir=None):
        self.root = Path(root).resolve() / 'browser-mail'
        self.root.mkdir(parents=True, exist_ok=True)
        # The worker saves mail pictures and files here, named by checksum (None: text only).
        self.attachments_dir = Path(attachments_dir).resolve() if attachments_dir else None
        self._process = None
        self._reader = None
        self._lock = threading.RLock()
        self._metadata_lock = threading.RLock()
        self._replies = queue.Queue()

    def _command(self):
        worker = os.environ.get('NEXUS_EMAIL_BROWSER_WORKER', '')
        node = os.environ.get('NEXUS_EMAIL_BROWSER_NODE', '')
        if not worker:
            candidate = Path(__file__).resolve().parents[2] / 'desktop' / 'email-browser-worker.js'
            if candidate.is_file():
                worker = str(candidate)
                node = shutil.which('node') or ''
        candidate = Path(worker) if worker else None
        # Electron's Node runtime reads files inside app.asar, while Python's
        # filesystem cannot stat those virtual entries. The archive itself is
        # the packaged filesystem boundary; Electron validates the entry.
        worker_available = bool(candidate and (candidate.is_file() or (
            candidate.name == 'email-browser-worker.js' and
            any(parent.suffix.lower() == '.asar' and parent.is_file() for parent in candidate.parents))))
        if not worker or not node or not worker_available or not Path(node).is_file():
            raise HarnessError('Browser mail needs the Nexus desktop runtime. Open the installed Nexus app.')
        return [node, worker]

    def _request(self, command, **payload):
        with self._lock:
            if not self._process or self._process.poll() is not None:
                if self._process is not None:
                    self.close()
                env = dict(os.environ, ELECTRON_RUN_AS_NODE='1')
                self._process = subprocess.Popen(self._command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, encoding='utf-8', env=env,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                self._replies = queue.Queue()
                def read(process, replies):
                    for line in process.stdout:
                        try:
                            replies.put(json.loads(line))
                        except ValueError:
                            pass
                    replies.put({'error': 'Browser mail stopped. Reconnect the browser.'})
                self._reader = threading.Thread(target=read, args=(self._process, self._replies), daemon=True)
                self._reader.start()
            try:
                self._process.stdin.write(json.dumps({'command': command, **payload}) + '\n')
                self._process.stdin.flush()
                result = self._replies.get(timeout=150)
            except (OSError, queue.Empty):
                self.close()
                raise HarnessError('Browser mail did not respond. Reconnect the browser and try again.') from None
            if result.get('error'):
                raise HarnessError(str(result['error']))
            return result['result']

    def _binding(self, connection_id):
        with self._metadata_lock:
            return self._binding_locked(connection_id)

    def _binding_locked(self, connection_id):
        if not re.fullmatch(r'[a-f0-9]{32}', connection_id or ''):
            raise HarnessError('Choose a valid browser mail connection.')
        path = self.root / connection_id / 'connection.json'
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if data.get('id') != connection_id or data.get('provider') not in ('browser_outlook', 'browser_gmail'):
                raise ValueError()
            expected = self._fingerprint(connection_id, data['provider'])
            previous = self._fingerprint(connection_id, data['provider'], contract='dom-v1')
            if data.get('schema_version') == 1 and data.get('config_fingerprint') == previous:
                # Parser v1 could mistake an unrelated shell heading for a
                # message subject. Preserve the authenticated profile, while
                # changing the contract fingerprint for studio invalidation.
                data['config_fingerprint'] = expected
                data['parser_contract'] = 'dom-v2'
                self._save(data)
            if data['schema_version'] != 1 or data['config_fingerprint'] != expected:
                raise ValueError()
            if 'browser_mode_contract' not in data:
                # Mode is a presentation/runtime setting, not mailbox identity.
                # Keep the parser/account fingerprint and all memory intact.
                data.update(browser_mode='headed', browser_mode_contract=self.MODE_CONTRACT)
                self._save(data)
            if data['browser_mode_contract'] != self.MODE_CONTRACT:
                raise ValueError()
            self._mode(data.get('browser_mode'))
            return data
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise HarnessError('Browser connection has changed or belongs to another installation. Create a new connection.') from None

    def _fingerprint(self, connection_id, provider, contract='dom-v2'):
        return hashlib.sha256(f'1|{self.root}|{connection_id}|{provider}|{contract}'.encode()).hexdigest()

    @staticmethod
    def _mode(mode):
        if mode not in ('headed', 'headless'):
            raise HarnessError('Choose visible or headless browser mail mode.')
        return mode

    def open(self, provider, connection_id='', browser_mode='headed'):
        with self._lock:
            return self._open(provider, connection_id, browser_mode)

    def _open(self, provider, connection_id, browser_mode):
        self._mode(browser_mode)
        provider = provider.removeprefix('browser_')
        if provider not in ('outlook', 'gmail'):
            raise HarnessError('Choose Outlook or Gmail for browser mail.')
        if connection_id:
            data = self._binding(connection_id)
            if data['provider'] != 'browser_' + provider:
                raise HarnessError('This browser connection belongs to a different mail provider.')
            data.update(browser_mode=browser_mode, browser_mode_contract=self.MODE_CONTRACT)
            self._save(data)
        else:
            connection_id = uuid.uuid4().hex
            data = {'schema_version': 1, 'id': connection_id, 'provider': 'browser_' + provider,
                    'parser_contract': 'dom-v2',
                    'browser_mode': browser_mode, 'browser_mode_contract': self.MODE_CONTRACT,
                    'email': '', 'name': '', 'config_fingerprint': self._fingerprint(connection_id, 'browser_' + provider)}
            (self.root / connection_id).mkdir()
            self._save(data)
        return self._call('open', data)

    def configure_mode(self, connection_id, mode):
        self._mode(mode)
        with self._lock:
            data = self._binding(connection_id)
            data.update(browser_mode=mode, browser_mode_contract=self.MODE_CONTRACT)
            self._save(data)
            # The worker applies mode at its next explicit operation. Changing
            # settings or rendering a snapshot must not launch a browser.
            return {**data, 'state': 'unverified',
                    'message': 'Browser mode saved. The next mailbox check uses this mode; sign-in opens visibly.'}

    def _save(self, data):
        with self._metadata_lock:
            path = self.root / data['id'] / 'connection.json'
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(data), encoding='utf-8')
            temporary.replace(path)

    def _call(self, command, data, **extra):
        result = self._request(command, connection=data, profile=str(self.root / data['id'] / 'profile'), **extra)
        if not isinstance(result, dict):
            raise HarnessError('Browser mail returned an invalid response.')
        email = result.get('email', '')
        if not isinstance(email, str):
            raise HarnessError('Browser mail returned an invalid mailbox identity.')
        if data.get('email') and email and data['email'].lower() != email.lower():
            raise HarnessError('The browser is signed into a different mailbox. Create a new connection for that account.')
        if command not in ('sync', 'send', 'prepare', 'attachments'):
            if result.get('state') == 'connected' and not email:
                raise HarnessError('Browser mail could not verify the signed-in mailbox identity.')
            if email:
                data.update(email=email, name=result.get('name', email))
                self._save(data)
            return {**data, **result}
        return result

    def status(self, connection_id):
        with self._lock:
            return self._call('status', self._binding(connection_id))

    def connections(self):
        """Read only this product-owned metadata; never launch browsers on listing."""
        # Snapshot rendering must not wait for a browser operation, which may
        # legitimately spend a minute traversing the inbox. Migrations and
        # atomic metadata writes have their own short, shared lock.
        with self._metadata_lock:
            result = []
            for path in sorted(self.root.glob('*/connection.json')):
                data = self._binding(path.parent.name)
                result.append({**data, 'state': 'sign_in_required',
                               'message': 'Reconnect the saved browser session to check sign-in.'})
            return result

    def sync(self, connection_id, cursor=''):
        with self._lock:
            data = self._binding(connection_id)
            status = self._call('status', data)
            if status.get('state') != 'connected':
                raise HarnessError('Sign into your mail in the Nexus browser, then check the connection again.')
            extra = {}
            if self.attachments_dir:
                self.attachments_dir.mkdir(parents=True, exist_ok=True)
                extra['attachments_dir'] = str(self.attachments_dir)
            return self._call('sync', data, cursor=cursor, **extra)

    def fetch_attachments(self, connection_id, incoming):
        """Reopen one stored message and save its pictures and files into the incoming folder."""
        if not self.attachments_dir:
            raise HarnessError('This browser connection cannot read attachments.')
        with self._lock:
            data = self._binding(connection_id)
            if not data.get('email'):
                raise HarnessError('Verify this mailbox identity before reading its attachments.')
            permitted = ('source_id', 'sender', 'subject', 'body', 'browser_reference')
            source = {key: incoming[key] for key in permitted if key in incoming}
            self.attachments_dir.mkdir(parents=True, exist_ok=True)
            result = self._call('attachments', data, incoming=source, attachments_dir=str(self.attachments_dir))
            files = result.get('attachments') if isinstance(result, dict) else None
            if not isinstance(files, list):
                raise HarnessError('Browser mail returned no attachment list.')
            return files

    def prepare_reply(self, connection_id, incoming, body, submission_id):
        """Verify/recover mailbox readiness without opening a composer or sending."""
        with self._lock:
            data = self._binding(connection_id)
            if not data.get('email'):
                raise HarnessError('Verify this mailbox identity before preparing a reply.')
            permitted = ('source_id', 'sender', 'subject', 'body', 'browser_reference',
                         'reply_to', 'internet_message_id', 'message_id', 'thread_id', 'references')
            source = {key: incoming[key] for key in permitted if key in incoming}
            return self._call('prepare', data, incoming=source, body=body, submission_id=submission_id)

    def submit_reply(self, connection_id, incoming, body, submission_id, attachments=None):
        """Transport an already-approved reply; never infer approval or retry send.

        The studio persists the submission intent before invoking this boundary.
        Worker acknowledgement distinguishes sent, definitely not_sent and unknown.
        """
        with self._lock:
            try:
                if not isinstance(incoming, dict) or not isinstance(incoming.get('source_id'), str) or not incoming['source_id']:
                    raise HarnessError('Choose a source email before sending a browser reply.')
                if not isinstance(body, str) or not body.strip() or len(body.encode('utf-8')) > 500_000:
                    raise HarnessError('A nonempty approved reply within the supported size is required.')
                if (not isinstance(submission_id, str) or not 1 <= len(submission_id) <= 128
                        or any(ord(char) < 33 or ord(char) > 126 for char in submission_id)):
                    raise HarnessError('A valid durable browser submission identifier is required.')
                permitted = ('source_id', 'sender', 'subject', 'body', 'browser_reference',
                             'reply_to', 'internet_message_id', 'message_id', 'thread_id', 'references')
                source = {key: incoming[key] for key in permitted if key in incoming}
                for key in ('source_id', 'sender', 'subject', 'body'):
                    if not isinstance(source.get(key), str):
                        raise HarnessError('The source email is missing browser reply metadata.')
                files = []
                for item in attachments or []:
                    path = Path(str(item.get('path') or ''))
                    if not path.is_file() or not item.get('name'):
                        raise HarnessError('A file approved for this reply is missing. Remove it or attach it again.')
                    files.append({'path': str(path), 'name': str(item['name']), 'type': str(item.get('type') or ''),
                                  'size': path.stat().st_size, 'sha256': str(item.get('sha256') or '')})
                data = self._binding(connection_id)
                if not data.get('email'):
                    raise HarnessError('Verify the browser mailbox identity before sending a reply.')
                # The send worker restores the saved session and verifies its
                # identity before locating or composing the approved reply.
                # A separate status request would reject recoverable sessions.
            except HarnessError as exc:
                return {'status': 'not_sent', 'error': str(exc)}
            result = self._call('send', data, incoming=source, body=body, submission_id=submission_id,
                                **({'attachments': files} if files else {}))
            if result.get('status') not in ('sent', 'not_sent', 'unknown'):
                return {'status': 'unknown', 'message': 'Browser send did not return a confirmed outcome. Check Sent Items before taking further action.'}
            return result

    def close(self):
        with self._lock:
            process, self._process = self._process, None
            reader, self._reader = self._reader, None
            if process and process.poll() is None:
                try:
                    process.stdin.close()
                    process.wait(timeout=8)
                except (OSError, subprocess.TimeoutExpired):
                    process.kill()
                    process.wait(timeout=5)
            if process and process.poll() is not None:
                # The reader has no ownership locks and reaches EOF when the
                # worker exits. Release pipe handles only after that boundary.
                if reader and reader is not threading.current_thread():
                    reader.join(timeout=1)
                if not reader or not reader.is_alive():
                    for stream in (process.stdin, process.stdout):
                        if stream and not stream.closed:
                            stream.close()
