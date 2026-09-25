"""Read-only classic Outlook inbox connector, with acknowledged durable batches.

The Outlook Object Model is deliberately accessed in a bounded child process:
new Outlook does not expose it. No mailbox is selected implicitly and no message
is marked read, modified, or sent. The caller acknowledges a batch by passing its
cursor on the next sync, after committing the imported messages.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import uuid

from .models import HarnessError

SCHEMA = 1
CONTRACT = 'classic-outlook-inbox/v1'
BATCH_SIZE = 25
MAX_HEADERS = 100_000
_LOCKS = {}
_LOCK_GUARD = threading.Lock()

# Input is JSON on stdin, never interpolated into executable PowerShell text.
_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
try {
  $outlook = New-Object -ComObject Outlook.Application
  $session = $outlook.GetNamespace('MAPI')
  $profile = [string]$session.CurrentProfileName
  $accounts = @()
  foreach ($account in $session.Accounts) {
    $store = $account.DeliveryStore
    if ($null -eq $store) { continue }
    try { $folder = $store.GetDefaultFolder(6) } catch { continue }
    $accounts += @{store_id=[string]$store.StoreID; folder_id=[string]$folder.EntryID;
      email=[string]$account.SmtpAddress; name=[string]$account.DisplayName; profile=$profile}
  }
  if ($request.operation -eq 'discover') {
    @{accounts=@($accounts)} | ConvertTo-Json -Depth 8 -Compress
    exit 0
  }
  $matches = @($accounts | Where-Object {
    $_.store_id -ceq $request.store_id -and $_.folder_id -ceq $request.folder_id -and
    $_.profile -ceq $request.profile -and $_.email -ieq $request.email
  })
  if ($matches.Count -ne 1) { throw 'The selected Outlook profile or account changed. Discover accounts again and reconnect.' }
  $folder = $session.GetFolderFromID($request.folder_id, $request.store_id)
  if ($request.operation -eq 'headers') {
    $table = $folder.GetTable()
    $table.Columns.RemoveAll()
    [void]$table.Columns.Add('EntryID')
    [void]$table.Columns.Add('MessageClass')
    $ids = [System.Collections.Generic.List[string]]::new()
    while (-not $table.EndOfTable) {
      $row = $table.GetNextRow()
      if ([string]$row.Item('MessageClass') -like 'IPM.Note*') { $ids.Add([string]$row.Item('EntryID')) }
      if ($ids.Count -gt 100000) { throw 'Inbox exceeds the supported 100000 messages. Move older mail to an archive before connecting.' }
    }
    @{ids=@($ids.ToArray())} | ConvertTo-Json -Depth 8 -Compress
    exit 0
  }
  if ($request.operation -ne 'messages') { throw 'Unknown Outlook operation.' }
  $messages = @()
  $missing = @()
  foreach ($id in $request.ids) {
    try { $mail = $session.GetItemFromID([string]$id, $request.store_id) }
    catch { $missing += [string]$id; continue }
    # A message may move after the header snapshot: never read outside this Inbox.
    if ([string]$mail.Parent.EntryID -cne $request.folder_id) { $missing += [string]$id; continue }
    $sender = [string]$mail.SenderEmailAddress
    if ($mail.SenderEmailType -eq 'EX') {
      $sender = ''
      try { $sender = [string]$mail.Sender.GetExchangeUser().PrimarySmtpAddress } catch {}
      if (-not $sender) {
        try { $sender = [string]$mail.Sender.PropertyAccessor.GetProperty('http://schemas.microsoft.com/mapi/proptag/0x39FE001E') } catch {}
      }
    }
    $body = [string]$mail.Body
    if ($body.Length -gt 2000000) { throw 'An Outlook message exceeds the supported size. Move it from Inbox before retrying.' }
    $received = ''
    try { $received = [string]$mail.ReceivedTime.ToUniversalTime().ToString('o') } catch {}
    # Copies of the attachments go to Nexus's private staging folder; the mail is not changed.
    $files = @()
    if ($request.staging) {
      $index = 0
      foreach ($attachment in $mail.Attachments) {
        $index++
        if ($index -gt 20) { break }
        $name = [string]$attachment.FileName
        $size = 0
        try { $size = [int64]$attachment.Size } catch {}
        $cid = ''
        try { $cid = [string]$attachment.PropertyAccessor.GetProperty('http://schemas.microsoft.com/mapi/proptag/0x3712001F') } catch {}
        # 1 = a file, 5 = an attached Outlook item; links and OLE objects stay in Outlook.
        if ($attachment.Type -ne 1 -and $attachment.Type -ne 5) { $files += @{name=$name; size=$size; omitted='A linked or embedded object; open it in Outlook.'}; continue }
        if ($size -gt 15200000) { $files += @{name=$name; size=$size; omitted='Larger than the attachment size limit; open it in Outlook.'}; continue }
        $target = Join-Path ([string]$request.staging) ([guid]::NewGuid().ToString('N'))
        try { $attachment.SaveAsFile($target); $files += @{name=$name; size=$size; path=$target; content_id=$cid} }
        catch { $files += @{name=$name; size=$size; omitted='Outlook could not copy this file.'} }
      }
    }
    $messages += @{entry_id=[string]$id; sender=$sender; subject=[string]$mail.Subject; body=$body; received_at=$received; attachments=@($files)}
  }
  @{messages=@($messages); missing=@($missing)} | ConvertTo-Json -Depth 8 -Compress
} catch {
  [Console]::Error.Write('Classic Outlook could not be read. Open classic Outlook with a configured profile and allow access if prompted. New Outlook is not supported by this connection. ' + $_.Exception.Message)
  exit 1
}
'''


def _powershell_executable():
    system_root = os.environ.get('SystemRoot') or os.environ.get('WINDIR')
    if not system_root:
        raise HarnessError('Windows system directory is unavailable; restart Nexus in a normal Windows session.')
    root = Path(system_root)
    if not root.is_absolute():
        raise HarnessError('Windows system directory must be an absolute path.')
    return root / 'System32/WindowsPowerShell/v1.0/powershell.exe'


def _transport(operation, payload):
    if os.name != 'nt':
        raise HarnessError('Classic Outlook requires Windows and classic Outlook. Use the browser connection on this computer.')
    executable = _powershell_executable()
    encoded = base64.b64encode(_SCRIPT.encode('utf-16-le')).decode('ascii')
    # Files avoid unbounded captured pipe buffers. Timeout also covers Outlook
    # security prompts; do not kill the user's Outlook process when timing out.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
        try:
            process = subprocess.run(
                [str(executable), '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                input=json.dumps(dict(payload, operation=operation)).encode('utf-8'),
                stdout=output, stderr=error, timeout=45,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise HarnessError('Classic Outlook did not respond. Open classic Outlook, check its access prompts, and retry. New Outlook requires the browser connection.') from exc
        if process.returncode:
            error.seek(0)
            detail = error.read(3000).decode('utf-8', errors='replace')
            raise HarnessError(detail or 'Classic Outlook is unavailable. Open classic Outlook or choose the browser connection.')
        output.seek(0)
        raw = output.read(55_000_001)
        if len(raw) > 55_000_000:
            raise HarnessError('Classic Outlook response exceeded the supported size.')
    try:
        result = json.loads(raw.decode('utf-8-sig'))
        if not isinstance(result, dict):
            raise ValueError('Expected object')
        return result
    except (ValueError, UnicodeError) as exc:
        raise HarnessError('Classic Outlook returned an unreadable response.') from exc


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class EmailClassic:
    def __init__(self, root, *, transport=None, attachments_dir=None):
        self.root = Path(root).resolve() / 'classic-outlook'
        self.path = self.root / 'connections.json'
        self.transport = transport or _transport
        # Where mail attachments are handed to the mail store (None: text only).
        self.attachments_dir = Path(attachments_dir) if attachments_dir else None
        with _LOCK_GUARD:
            self.lock = _LOCKS.setdefault(str(self.root), threading.RLock())

    def _read(self):
        if not self.path.exists():
            return {'schema': SCHEMA, 'contract': CONTRACT, 'connections': {}}
        try:
            value = json.loads(self.path.read_text(encoding='utf-8'))
            if value.get('schema') != SCHEMA or value.get('contract') != CONTRACT:
                raise ValueError('Contract changed')
            if not isinstance(value.get('connections'), dict):
                raise ValueError('Invalid connections')
            return value
        except (OSError, ValueError, AttributeError) as exc:
            raise HarnessError('Classic Outlook connection metadata is incompatible or damaged. Reconnect using a new connection storage location.') from exc

    def _write(self, state):
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name('connections-' + uuid.uuid4().hex + '.tmp')
        try:
            with temporary.open('w', encoding='utf-8') as stream:
                json.dump(state, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _public(connection):
        return {key: connection[key] for key in ('id', 'provider', 'state', 'email', 'name', 'config_fingerprint')}

    def discover(self):
        """Enumerate account identities only. Never read messages during discovery."""
        with self.lock:
            state = self._read()
            accounts = self.transport('discover', {}).get('accounts', [])
            found = []
            for account in accounts:
                if not all(isinstance(account.get(k), str) and account[k] for k in ('store_id', 'folder_id', 'profile', 'email')):
                    continue
                identity = {k: account[k] for k in ('store_id', 'folder_id', 'profile', 'email')}
                fingerprint = _fingerprint({'contract': CONTRACT, **identity})
                connection_id = 'classic-' + fingerprint[:32]
                if connection_id in found:
                    continue
                previous = state['connections'].get(connection_id, {})
                state['connections'][connection_id] = dict(previous, **identity, id=connection_id,
                    provider='classic_outlook', state='connected', name=account.get('name') or account['email'],
                    config_fingerprint=fingerprint)
                found.append(connection_id)
            for key, connection in state['connections'].items():
                if key not in found:
                    connection['state'] = 'disconnected'
            self._write(state)
            return [self._public(state['connections'][key]) for key in found]

    def status(self, connection_id):
        with self.lock:
            return self._public(self._connection(self._read(), connection_id))

    @staticmethod
    def _connection(state, connection_id):
        connection = state['connections'].get(connection_id)
        if not connection:
            raise HarnessError('Choose a discovered classic Outlook account before connecting.')
        return connection

    def sync(self, connection_id, cursor=''):
        with self.lock:
            state = self._read()
            connection = self._connection(state, connection_id)
            if connection['state'] != 'connected':
                raise HarnessError('The selected classic Outlook profile changed. Discover accounts again and reconnect.')
            identity = {k: connection[k] for k in ('store_id', 'folder_id', 'profile', 'email')}
            pending = connection.get('pending')
            # Replay saved bodies before touching COM when an import was interrupted.
            if pending and cursor != pending['cursor']:
                # A replayed batch still has its own queue behind it, and a batch
                # saved before this key existed must not claim an empty backlog.
                return dict({k: pending[k] for k in ('messages', 'cursor', 'warnings')},
                            has_more=bool(pending.get('has_more', connection.get('queue'))))
            seen = set(connection.get('seen', []))
            if pending:
                seen.update(pending['ids'])
            ids = self.transport('headers', identity).get('ids', [])
            if not isinstance(ids, list) or len(ids) > MAX_HEADERS or any(not isinstance(x, str) or not x for x in ids):
                raise HarnessError('Classic Outlook returned an invalid or oversized Inbox listing.')
            current = set(ids)
            # Keep the previous scan's queue order: newly arriving mail cannot push
            # older unprocessed mail perpetually beyond a fixed latest-N window.
            queue = [x for x in connection.get('queue', []) if x in current and x not in seen]
            queued = set(queue)
            queue.extend(x for x in dict.fromkeys(ids) if x not in seen and x not in queued)
            selected = queue[:BATCH_SIZE]
            staging = None
            if self.attachments_dir and selected:
                staging = self.attachments_dir / ('classic-' + uuid.uuid4().hex)
                staging.mkdir(parents=True, exist_ok=True)
            try:
                result = self.transport('messages', dict(identity, ids=selected, **({'staging': str(staging)} if staging else {}))) if selected else {'messages': [], 'missing': []}
                files = {id(message): self._hand_over(message.get('attachments'), staging) for message in result.get('messages', [])
                         if isinstance(message, dict)} if staging else {}
            finally:
                if staging:
                    shutil.rmtree(staging, ignore_errors=True)
            messages = []
            returned = set()
            for message in result.get('messages', []):
                entry_id = message.get('entry_id')
                if entry_id not in selected or entry_id in returned:
                    raise HarnessError('Classic Outlook returned a message from a different batch.')
                if any(not isinstance(message.get(k), str) for k in ('sender', 'subject', 'body')) or len(message['body']) > 2_000_000:
                    raise HarnessError('Classic Outlook returned an invalid message.')
                returned.add(entry_id)
                received_at = message.get('received_at')
                messages.append(dict(source_id='classic:' + _fingerprint([identity['store_id'], entry_id]),
                    sender=message['sender'], subject=message['subject'], body=message['body'],
                    received_at=received_at if isinstance(received_at, str) else '',
                    **({'attachments': files[id(message)]} if files.get(id(message)) else {})))
            missing = result.get('missing', [])
            if not isinstance(missing, list) or any(x not in selected for x in missing) or returned | set(missing) != set(selected):
                raise HarnessError('Classic Outlook did not return the complete requested batch. Retry the connection.')
            warnings = ['Some messages moved or became unavailable during the scan; they will be retried if still in Inbox.'] if missing else []
            # Retry unavailable items after the rest of this queue, without marking
            # them seen. A single inaccessible item cannot starve subsequent mail.
            connection['seen'] = sorted(seen & current)
            connection['queue'] = queue[len(selected):] + missing
            batch = dict(messages=messages, cursor=uuid.uuid4().hex, warnings=warnings, ids=sorted(returned),
                         has_more=bool(connection['queue']))
            connection['pending'] = batch
            self._write(state)
            return {k: batch[k] for k in ('messages', 'cursor', 'warnings', 'has_more')}

    def _hand_over(self, entries, staging):
        """Move files Outlook saved into the mail store's incoming folder, named by checksum."""
        from .email_attachments import MAX_FILE_BYTES, MAX_FILES
        handed = []
        for entry in (entries if isinstance(entries, list) else [])[:MAX_FILES]:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get('name') or '')[:500]
            if entry.get('omitted') or not entry.get('path'):
                handed.append({'name': name, 'omitted': str(entry.get('omitted') or 'Outlook could not copy this file.')[:300]})
                continue
            try:
                path = Path(str(entry['path'])).resolve(strict=True)
                if path.parent != staging.resolve() or path.stat().st_size > MAX_FILE_BYTES:
                    raise OSError('outside staging or too large')
                raw = path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                os.replace(path, self.attachments_dir / digest)
            except OSError:
                handed.append({'name': name, 'omitted': 'Outlook could not copy this file.'})
                continue
            content_id = str(entry.get('content_id') or '')[:300]
            handed.append({'name': name, 'sha256': digest, 'content_id': content_id, 'inline': bool(content_id)})
        return handed

    def close(self):
        """No Outlook process is owned or terminated by this adapter."""
