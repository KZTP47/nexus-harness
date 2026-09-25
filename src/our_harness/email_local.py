"""Product-owned local mailbox adapters, isolated from publisher OAuth settings."""
from __future__ import annotations

import threading

from .models import HarnessError

LOCAL_KINDS = frozenset({'browser_outlook', 'browser_gmail', 'classic_outlook'})


class LocalMail:
    def __init__(self, root):
        self.root = root
        self._adapters = {}
        self._observed = {}
        self._lock = threading.RLock()

    def adapter(self, kind):
        if kind not in LOCAL_KINDS:
            raise HarnessError('Choose browser Outlook, browser Gmail, or classic Outlook.')
        family = 'classic' if kind == 'classic_outlook' else 'browser'
        with self._lock:
            if family not in self._adapters:
                # Both hand mail attachments to the store through its incoming folder.
                incoming = self.root / 'attachments' / 'incoming'
                if family == 'classic':
                    from .email_classic import EmailClassic
                    self._adapters[family] = EmailClassic(self.root / 'classic', attachments_dir=incoming)
                else:
                    from .email_browser import EmailBrowser
                    self._adapters[family] = EmailBrowser(self.root / 'browser', attachments_dir=incoming)
            return self._adapters[family]

    def observe(self, connection):
        public = {key: connection[key] for key in
                  ('id', 'provider', 'state', 'email', 'name', 'config_fingerprint', 'message',
                   'browser_mode', 'browser_mode_contract', 'actual_browser_mode')
                  if key in connection}
        with self._lock:
            self._observed[public['id']] = public
        return public

    def snapshot(self):
        with self._lock:
            # Pending sign-ins survive restarts without opening a browser on a GET.
            adapter = self.adapter('browser_outlook')
            for connection in adapter.connections():
                if connection['id'] not in self._observed:
                    self.observe(connection)
            return [dict(item) for item in self._observed.values()]

    def open(self, kind, identity='', browser_mode='headed'):
        if kind not in {'browser_outlook', 'browser_gmail'}:
            raise HarnessError('Choose Outlook or Gmail browser sign-in.')
        return self.observe(self.adapter(kind).open(kind, identity, browser_mode=browser_mode))

    def configure_mode(self, kind, identity, mode):
        if kind not in {'browser_outlook', 'browser_gmail'}:
            raise HarnessError('Browser mode applies only to Outlook or Gmail browser connections.')
        adapter = self.adapter(kind)
        connection = adapter._binding(identity)
        if connection.get('provider') != kind:
            raise HarnessError('This mailbox belongs to a different connection method.')
        changed = adapter.configure_mode(identity, mode)
        with self._lock:
            observed = self._observed.get(identity, {})
            for field in ('state', 'email', 'name', 'actual_browser_mode'):
                if field in observed:
                    changed[field] = observed[field]
            return self.observe(changed)

    def discover(self):
        return [self.observe(item) for item in self.adapter('classic_outlook').discover()]

    def status(self, kind, identity):
        connection = self.adapter(kind).status(identity)
        if connection.get('provider') != kind:
            raise HarnessError('This mailbox belongs to a different connection method.')
        return self.observe(connection)

    def close(self):
        for adapter in list(self._adapters.values()):
            adapter.close()
