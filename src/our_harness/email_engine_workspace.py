"""Private EmailEngine configuration and account discovery for the email workspace."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .models import HarnessError


class EmailEngineWorkspace:
    def __init__(self, studio):
        self.studio = studio
        self.path = studio.root / 'emailengine.json'
        self._client = None
        self._managed = None
        self._accounts = []

    @property
    def managed(self):
        if self._managed is None:
            from .email_engine_managed import ManagedEmailEngine
            self._managed = ManagedEmailEngine(self.studio.root, self.studio.secrets)
        return self._managed

    def settings(self):
        if self.path.is_file():
            try:
                stored = json.loads(self.path.read_text(encoding='utf-8'))
                if stored.get('schema_version') != 1:
                    raise ValueError('schema')
                return json.loads(self.studio.secrets.unprotect(stored['encrypted']))
            except Exception:
                raise HarnessError('EmailEngine settings could not be unlocked. Reconnect the service.') from None
        configured = self.studio.config.get('email_engine', {}) or {}
        return {key: str(configured.get(key) or '') for key in ('url', 'token')}

    @property
    def client(self):
        if self._client is None:
            from .email_engine import EmailEngineClient
            settings = self.settings()
            if settings.get('mode') == 'managed':
                settings = self.managed.start()
            if not settings.get('url') or not settings.get('token'):
                raise HarnessError('Configure the EmailEngine service URL and access token first.')
            self._client = EmailEngineClient(settings['url'], settings['token'])
        return self._client

    @property
    def fingerprint(self):
        settings = self.settings()
        return hashlib.sha256(json.dumps(['emailengine-workspace/v1', settings.get('url', '').rstrip('/')], sort_keys=True).encode()).hexdigest()

    def configure(self, payload):
        from .email_engine import EmailEngineClient
        old = self.settings()
        url = str(payload.get('url') or '').strip()
        token = str(payload.get('token') or (old.get('token') if url == old.get('url') else '') or '').strip()
        client = EmailEngineClient(url, token)
        # Verify authenticated API access before replacing a working setup.
        accounts = client.accounts()
        settings = {'url': url.rstrip('/'), 'token': token, 'mode': 'external'}
        self._save(settings, client, accounts)
        return {'configured': True, 'accounts': accounts}

    def _save(self, settings, client, accounts):
        sealed = {'schema_version': 1, 'encrypted': self.studio.secrets.protect(json.dumps(settings))}
        with self.studio._mutation():
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(sealed), encoding='utf-8')
            temporary.replace(self.path)
            self._client = client
            self._accounts = accounts

    def prepare_local(self):
        from .email_engine import EmailEngineClient
        settings = self.managed.start()
        client = EmailEngineClient(settings['url'], settings['token'])
        accounts = client.accounts()
        self._save({**settings, 'mode': 'managed'}, client, accounts)
        return {'configured': True, 'accounts': accounts}

    def sign_in(self, redirect_url):
        import webbrowser
        url = self.client.authentication_form(redirect_url)
        try:
            opened = bool(webbrowser.open(url))
        except Exception:
            opened = False
        return {'authorization_url': url, 'browser_opened': opened}

    def snapshot(self):
        try:
            settings = self.settings()
            configured = bool(settings.get('url') and settings.get('token'))
            local = self._managed.status() if self._managed is not None else None
            return {'configured': configured, 'url': settings.get('url', ''), 'accounts': self._accounts,
                    'mode': settings.get('mode', 'external'), 'local_runtime': local,
                    'message': 'Service configured. Check accounts to verify connectivity.' if configured else
                    'EmailEngine requires a running service with Redis-compatible storage. Connect a service below; browser and direct API connections remain available.'}
        except HarnessError as exc:
            return {'configured': False, 'url': '', 'message': str(exc)}

    def accounts(self):
        self._accounts = self.client.accounts()
        return {'accounts': self._accounts}

    def close(self):
        if self._managed is not None:
            self._managed.close()
