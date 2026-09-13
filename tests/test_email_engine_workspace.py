"""Portable workspace configuration integration; no network or real credentials."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.email_studio import EmailStudio
from our_harness.models import HarnessError


class TestSecrets:
    def __init__(self):
        self.values = {}

    def protect(self, value):
        opaque = 'opaque-' + str(len(self.values))
        self.values[opaque] = value
        return opaque

    def unprotect(self, value):
        return self.values[value]


class EmailEngineWorkspaceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='mail-workspace-')
        self.addCleanup(temp.cleanup)
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), Path(temp.name), [], {})
        self.secrets = TestSecrets()
        self.studio = EmailStudio(self.config, secret_store=self.secrets, provider_call=lambda *args: 'Reply')
        self.client = Mock()
        self.client.accounts.return_value = [{'account': 'remote', 'email': 'owner@example.test'}]
        self.client.account.return_value = {'account': 'remote', 'email': 'owner@example.test', 'state': 'connected'}
        self.factory = patch('our_harness.email_engine.EmailEngineClient', return_value=self.client)
        self.factory.start()
        self.addCleanup(self.factory.stop)

    def configure(self, url='https://service.example.test', token='PRIVATE_TEST_TOKEN'):
        return self.studio.mail_backend.configure({'url': url, 'token': token})

    def test_secret_store_boundary_persists_and_never_returns_token(self):
        result = self.configure()
        self.assertEqual(result['accounts'][0]['account'], 'remote')
        persisted = self.studio.mail_backend.path.read_text(encoding='utf-8')
        self.assertNotIn('PRIVATE_TEST_TOKEN', persisted)
        self.assertNotIn('PRIVATE_TEST_TOKEN', json.dumps(self.studio.mail_backend.snapshot()))
        self.assertEqual(json.loads(persisted)['schema_version'], 1)
        reopened = EmailStudio(self.config, secret_store=self.secrets, provider_call=lambda *args: 'Reply')
        self.assertEqual(reopened.mail_backend.settings()['token'], 'PRIVATE_TEST_TOKEN')
        self.assertEqual(reopened.mail_backend.snapshot()['url'], 'https://service.example.test')

    def test_failed_authenticated_probe_preserves_working_configuration(self):
        self.configure()
        previous = self.studio.mail_backend.path.read_bytes()
        self.client.accounts.side_effect = HarnessError('Service unavailable')
        with self.assertRaises(HarnessError):
            self.configure('https://replacement.example.test', 'new-private-token')
        self.assertEqual(self.studio.mail_backend.path.read_bytes(), previous)

    def test_endpoint_change_invalidates_account_and_reconnect_resets_cursor(self):
        self.configure()
        account = self.studio.connect_engine({'remote_account_id': 'remote', 'provider_route': 'route'})['account']
        current = self.studio._get('account', account['id'])
        current['cursor'] = 'old-service-cursor'
        self.studio._put('account', current)
        original_fingerprint = self.studio.mail_backend.fingerprint
        self.configure(token='rotated-private-token')
        self.assertEqual(self.studio.mail_backend.fingerprint, original_fingerprint)
        self.configure('https://replacement.example.test')
        with self.assertRaisesRegex(HarnessError, 'service changed'):
            self.studio.dispatch('sync', {'account_id': account['id']})
        self.client.sync.assert_not_called()
        connected = self.studio.connect_engine({'remote_account_id': 'remote', 'provider_route': 'route'})['account']
        self.assertEqual(connected['id'], account['id'])
        self.assertNotEqual(connected['fingerprint'], account['fingerprint'])
        self.assertEqual(self.studio._get('account', account['id'])['cursor'], '')

    def test_locked_or_future_schema_settings_fail_closed_without_public_secrets(self):
        self.configure()
        self.studio.mail_backend.path.write_text(json.dumps({'schema_version': 999, 'encrypted': 'PRIVATE_TEST_TOKEN'}), encoding='utf-8')
        state = self.studio.mail_backend.snapshot()
        self.assertFalse(state['configured'])
        self.assertNotIn('PRIVATE_TEST_TOKEN', json.dumps(state))
        with self.assertRaises(HarnessError):
            self.studio.mail_backend.settings()

    def test_unconnected_service_account_is_not_claimed_as_connected(self):
        self.configure()
        for state in ('connecting', 'authenticationError', 'unset', None):
            self.client.account.return_value['state'] = state
            with self.assertRaises(HarnessError):
                self.studio.connect_engine({'remote_account_id': 'remote', 'provider_route': 'route'})
        self.assertEqual(self.studio.snapshot()['accounts'], [])


if __name__ == '__main__':
    unittest.main()
