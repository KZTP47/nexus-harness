"""Publisher registration settings and UI-facing mailbox sign-in ownership."""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from .models import HarnessError

PROVIDERS = ('outlook', 'gmail')


def _load_overrides(studio):
    path = studio.data_dir / 'oauth-registration.json'
    if not path.is_file():
        return {}
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
        if document['schema_version'] != 1:
            raise ValueError('version')
        overrides = json.loads(studio.secrets.unprotect(document['encrypted']))
        if not isinstance(overrides, dict) or any(key not in PROVIDERS or not isinstance(value, dict) for key, value in overrides.items()):
            raise ValueError('shape')
        return overrides
    except Exception:
        raise HarnessError('Mailbox sign-in settings could not be unlocked. Restore them in advanced setup.') from None


def _resolved_registrations(studio):
    # Only product-owned registration sources: no provider caches, browser
    # profiles, unrelated applications, or guessed IDs derived from addresses.
    overrides = _load_overrides(studio)
    configured = studio.config.get('email_oauth.clients', {}) or {}
    from .email_oauth_defaults import PUBLISHER_REGISTRATIONS
    if not isinstance(configured, dict):
        if not overrides:
            raise HarnessError('Mailbox publisher registration settings are invalid.')
        configured = {}
    registrations, sources = {}, {}
    for key in PROVIDERS:
        project = configured.get(key, {})
        if not isinstance(project, dict):
            if key not in overrides:
                raise HarnessError('Mailbox publisher registration settings are invalid.')
            project = {}
        merged, source = {}, 'unconfigured'
        for layer, values in (('publisher', PUBLISHER_REGISTRATIONS.get(key, {})), ('project_config', project), ('local_override', overrides.get(key, {}))):
            if 'client_id' in values:
                # A desktop registration value issued for another Google ID
                # must not silently follow a changed client ID.
                if values['client_id'] != merged.get('client_id'):
                    merged.pop('client_secret', None)
                source = layer if values['client_id'] else 'unconfigured'
            merged.update(values)
        registrations[key], sources[key] = merged, source
    return registrations, sources


def load_registrations(studio):
    return _resolved_registrations(studio)[0]


def _public_registration(provider, registration, source):
    return {'provider': provider, 'source': source,
            'client_id': str(registration.get('client_id') or '').strip(),
            'tenant': str(registration.get('tenant') or 'common') if provider == 'outlook' else ''}


def _recognizable_registration(provider, registration):
    client_id = str(registration.get('client_id') or '').strip()
    pattern = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}' if provider == 'outlook' else r'[0-9]+(?:-[A-Za-z0-9_-]+)?\.apps\.googleusercontent\.com'
    return bool(re.fullmatch(pattern, client_id))


class EmailOnboarding:
    def __init__(self, studio):
        self.studio = studio
        self.pending = {}
        self.lock = threading.RLock()

    def configure(self, payload):
        provider = payload.get('provider')
        if provider not in PROVIDERS:
            raise HarnessError('Choose Outlook or Gmail.')
        client_id = str(payload.get('client_id', '')).strip()
        if not re.fullmatch(r'[A-Za-z0-9._-]{1,500}', client_id):
            raise HarnessError('Enter the desktop application client ID from the provider registration.')
        registration = {'client_id': client_id}
        if provider == 'outlook':
            tenant = str(payload.get('tenant') or 'common').strip()
            if tenant != 'common':
                raise HarnessError('Use the common tenant for Outlook personal and work accounts.')
            registration['tenant'] = tenant
        else:
            secret = str(payload.get('client_secret') or '').strip()
            if len(secret) > 2000:
                raise HarnessError('The desktop registration value is too long.')
            if secret:
                registration['client_secret'] = secret
        with self.lock, self.studio._mutation():
            try:
                registrations = _load_overrides(self.studio)
            except HarnessError:
                # Explicit advanced repair can replace an unreadable override.
                # Both providers will need their registration entered again.
                registrations = {}
            registrations[provider] = registration
            target = self.studio.data_dir / 'oauth-registration.json'
            encrypted = self.studio.secrets.protect(json.dumps(registrations))
            temporary = target.with_suffix('.tmp')
            temporary.write_text(json.dumps({'schema_version': 1, 'encrypted': encrypted}), encoding='utf-8')
            temporary.replace(target)
            self.studio.connectors.update_registrations(load_registrations(self.studio))
            self.pending.clear()
        return {'configured': True}

    def auto_connect(self, payload):
        """Find Nexus's registered client identity and begin user-owned consent."""
        provider = payload.get('provider')
        if provider not in PROVIDERS:
            raise HarnessError('Choose Outlook or Gmail.')
        with self.lock:
            registrations, sources = _resolved_registrations(self.studio)
            registration = registrations[provider]
            public = _public_registration(provider, registration, sources[provider])
            from .email_connectors import PROVIDERS as CONNECTOR_PROVIDERS
            if not _recognizable_registration(provider, registration):
                explanation = ("The detected client ID does not match this provider's desktop registration format. " if public['client_id'] else '')
                return {'state': 'publisher_registration_required', 'registration': public,
                        'message': explanation + "Nexus needs its own registered application client ID from its publisher or your administrator. Automatic setup cannot create or borrow another application's identity. Manual mailbox setup is available.",
                        'setup_url': CONNECTOR_PROVIDERS[provider]['setup_url']}
            self.studio.connectors.update_registrations(registrations)
            started = self.start(payload)
            return {'state': 'authorization_pending', 'registration': public, **started,
                    'message': "Nexus found the registered application. Complete your provider's sign-in and consent to connect this mailbox."}

    def start(self, payload):
        provider = payload.get('provider')
        if provider not in PROVIDERS:
            raise HarnessError('Choose Outlook or Gmail.')
        settings = {key: payload[key] for key in ('account_id', 'provider_route', 'provider_model', 'poll_enabled', 'poll_seconds') if key in payload}
        # Validate before starting a browser or accepting mailbox credentials.
        self.studio._assistant_settings({}, settings)
        connector_id = ''
        if settings.get('account_id'):
            account = self.studio._get('account', settings['account_id'])
            if account['kind'] != provider:
                raise HarnessError('Reconnect using the original email provider.')
            connector_id = account['connector_id']
        with self.lock:
            for item in self.pending.values():
                if item['provider'] == provider and item['settings'].get('account_id') == settings.get('account_id'):
                    if self.studio.connectors.status(item['id'])['state'] == 'pending':
                        return {'request_id': item['id'], 'authorization_url': item['authorization_url']}
            result = self.studio.connectors.begin(provider, account_id=connector_id)
            identity = result['session_id']
            self.pending[identity] = {'id': identity, 'provider': provider, 'settings': settings,
                                      'authorization_url': result['authorization_url']}
            # Keep finished attempt metadata bounded in this process.
            if len(self.pending) > 20:
                self.pending.pop(next(iter(self.pending)))
            return {'request_id': identity, 'authorization_url': result['authorization_url']}

    def snapshot(self):
        with self.lock:
            registered = self.studio.connectors.registration_status()
            if isinstance(registered, list):
                registered = {item['provider']: item for item in registered}
            registrations, sources = _resolved_registrations(self.studio)
            result = {}
            for provider in PROVIDERS:
                info = registered.get(provider, {})
                configured = bool(info.get('configured'))
                result[provider] = {'configured': configured,
                                    'client_id': _public_registration(provider, registrations[provider], sources[provider])['client_id'],
                                    'tenant': _public_registration(provider, registrations[provider], sources[provider])['tenant'],
                                    'registration_source': sources[provider],
                                    'status': 'ready' if configured else 'registration_required',
                                    'message': 'Ready to connect.' if configured else 'Nexus needs its publisher application registration before sign-in is available.'}
            pending = []
            for identity, item in list(self.pending.items()):
                status = self.studio.connectors.status(identity)
                state, message = status['state'], status.get('error', '')
                if state == 'connected' and not item.get('account_id'):
                    try:
                        account = self.studio.connect_account(status['connection'], item['settings'])['account']
                        item['account_id'] = account['id']
                    except HarnessError as exc:
                        state, message = 'error', str(exc)
                pending.append({'id': identity, 'provider': item['provider'], 'state': state,
                                'message': message, 'account_id': item.get('account_id', '')})
            result['pending'] = pending
            return result
