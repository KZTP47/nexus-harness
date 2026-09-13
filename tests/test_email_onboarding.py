import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.email_onboarding import EmailOnboarding, load_registrations
from our_harness.email_studio import EmailStudio
from our_harness.email_service import EmailService
from our_harness.models import HarnessError
from tests.test_email_studio import Secrets


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='portable-signin-')
        self.addCleanup(self.temp.cleanup)
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), Path(self.temp.name), [], {})
        self.connector = Mock()
        self.connection = dict(id='connection-one', provider='outlook', email='owner@example.test',
                               name='Owner', state='connected', config_fingerprint='registration-v1')
        self.connector.connection.return_value = self.connection
        self.connector.registration_status.return_value = {'outlook': {'configured': True}, 'gmail': {'configured': False}}
        self.connector.begin.return_value = {'session_id': 'session-one', 'authorization_url': 'https://login.microsoftonline.com/authorize'}
        self.connector.status.return_value = {'state': 'connected', 'connection': self.connection}
        self.connector.sync.return_value = {'messages': [{'source_id': 'stable-message-id', 'sender': 'sender@example.test', 'subject': 'Hello', 'body': 'A message'}], 'cursor': 'page-two'}
        self.studio = EmailStudio(self.config, secret_store=Secrets(), provider_call=lambda *a: 'A reply.', connectors=self.connector)
        self.onboarding = EmailOnboarding(self.studio)

    def connect(self):
        result = self.onboarding.start({'provider': 'outlook', 'provider_route': 'arbitrary-local-route'})
        self.assertEqual(result['request_id'], 'session-one')
        return self.onboarding.snapshot()['pending'][0]['account_id']

    def test_completion_uses_authenticated_identity_and_defaults_to_automatic(self):
        account_id = self.connect()
        account = self.studio.snapshot()['accounts'][0]
        self.assertTrue(account['poll_enabled'])
        self.assertEqual(account['email'], 'owner@example.test')
        self.assertEqual(account['connection_state'], 'connected')
        self.onboarding.snapshot()
        self.assertEqual(len(self.studio.snapshot()['accounts']), 1)
        self.assertEqual(account_id, account['id'])

    def test_sync_cursor_and_identity_survive_restart_and_deduplicate(self):
        account_id = self.connect()
        self.studio.dispatch('sync', {'account_id': account_id})
        reopened = EmailStudio(self.config, secret_store=Secrets(), provider_call=lambda *a:'Reply', connectors=self.connector)
        reopened.dispatch('sync', {'account_id': account_id})
        self.connector.sync.assert_called_with('connection-one', 'page-two')
        self.assertEqual(len(reopened.snapshot()['messages']), 1)

    def test_configuration_change_fails_closed_and_reconnect_invalidates_old_mail(self):
        account_id = self.connect()
        self.studio.dispatch('sync', {'account_id': account_id})
        message = self.studio.snapshot()['messages'][0]
        self.connection['config_fingerprint'] = 'new-registration'
        with self.assertRaises(HarnessError):
            self.studio.dispatch('sync', {'account_id': account_id})
        self.studio.connect_account(self.connection, {'account_id': account_id, 'provider_route': 'arbitrary-local-route'})
        with self.assertRaises(HarnessError):
            self.studio.dispatch('create_draft', {'account_id': account_id, 'message_id': message['id']})

    def test_reconnect_cannot_move_account_to_different_mailbox(self):
        account_id = self.connect()
        other = {**self.connection, 'email': 'different@example.test'}
        with self.assertRaises(HarnessError):
            self.studio.connect_account(other, {'account_id': account_id, 'provider_route': 'route'})
        self.assertEqual(self.studio.snapshot()['accounts'][0]['email'], 'owner@example.test')

    def test_connecting_same_mailbox_again_preserves_account_and_preferences(self):
        account_id = self.connect()
        self.studio.dispatch('memory_save', {'account_id': account_id, 'text': 'Keep it brief.'})
        self.connection['id'] = 'new-connection'
        result = self.studio.connect_account(self.connection, {'provider_route': 'arbitrary-local-route'})
        self.assertEqual(result['account']['id'], account_id)
        self.assertEqual(len(self.studio.snapshot()['accounts']), 1)
        self.assertEqual(self.studio.snapshot()['memories'][0]['text'], 'Keep it brief.')
        self.connector.disconnect.assert_called_once_with('connection-one')

    def test_send_requires_approval_and_unknown_outcome_cannot_repeat(self):
        account_id = self.connect()
        self.studio.dispatch('sync', {'account_id': account_id})
        message = self.studio.snapshot()['messages'][0]
        draft = self.studio.dispatch('create_draft', {'account_id': account_id, 'message_id': message['id']})['draft']
        draft = self.studio.process_draft(draft['id'])['draft']
        with self.assertRaises(HarnessError):
            self.studio.finalize_draft(draft['id'])
        self.connector.send.assert_not_called()
        self.studio.dispatch('approve_draft', {'account_id': account_id, 'draft_id': draft['id'], 'revision': draft['revision'], 'text': 'Approved reply.', 'learn': False})
        self.connector.send.side_effect = TimeoutError('uncertain')
        for _ in range(2):
            with self.assertRaises(HarnessError):
                self.studio.finalize_draft(draft['id'])
        self.connector.send.assert_called_once()
        self.assertEqual(self.studio.snapshot()['drafts'][0]['status'], 'delivery_unknown')

    def test_disconnect_preserves_mail_memory_and_disables_automatic_checks(self):
        account_id = self.connect()
        self.studio.dispatch('sync', {'account_id': account_id})
        self.studio.dispatch('memory_save', {'account_id': account_id, 'text': 'Be concise.'})
        self.connector.connection.return_value = {**self.connection, 'state': 'disconnected'}
        self.studio.disconnect_account(account_id)
        self.connector.disconnect.assert_called_once_with('connection-one')
        state = self.studio.snapshot()
        self.assertFalse(state['accounts'][0]['poll_enabled'])
        self.assertEqual(len(state['messages']), 1)
        self.assertEqual(len(state['memories']), 1)

    def test_registration_override_is_encrypted_and_reopens(self):
        self.onboarding.configure({'provider': 'gmail', 'client_id': 'example.apps.googleusercontent.com', 'client_secret': 'private-desktop-value'})
        text = (self.studio.data_dir/'oauth-registration.json').read_text()
        self.assertNotIn('private-desktop-value', text)
        self.assertEqual(load_registrations(self.studio)['gmail']['client_id'], 'example.apps.googleusercontent.com')
        self.connector.update_registrations.assert_called_once()

    def test_advanced_registration_repairs_malformed_publisher_configuration(self):
        self.config.data['email_oauth'] = {'clients': ['invalid']}
        self.onboarding.configure({'provider': 'outlook', 'client_id': 'repaired-public-client'})
        self.assertEqual(load_registrations(self.studio)['outlook']['client_id'], 'repaired-public-client')

    def test_oauth_identity_cannot_be_forged_through_manual_save(self):
        with self.assertRaises(HarnessError):
            self.studio.dispatch('account_save', {'kind': 'outlook', 'email': 'forged@example.test'})
        account_id = self.connect()
        self.studio.dispatch('account_save', {'account_id': account_id, 'email': 'forged@example.test', 'poll_enabled': False})
        account = self.studio.snapshot()['accounts'][0]
        self.assertEqual(account['email'], 'owner@example.test')
        self.assertFalse(account['poll_enabled'])

    def test_automatic_poll_drafts_new_mail_but_not_previous_configuration(self):
        account_id = self.connect()
        self.studio.dispatch('sync', {'account_id': account_id})
        self.connection['config_fingerprint'] = 'changed-registration'
        self.studio.connect_account(self.connection, {'account_id': account_id, 'provider_route': 'arbitrary-local-route'})
        # This is a new arrival after reconnection, rather than undated history.
        from datetime import datetime, timezone
        self.connector.sync.return_value['messages'][0]['received_at'] = datetime.now(timezone.utc).isoformat()
        service = EmailService(Mock(config=self.config), studio=self.studio, engine=Mock())
        service._stop = Mock()
        service._stop.is_set.return_value = False
        service._stop.wait.side_effect = [False, True]
        service._background = lambda key, work: work()
        # Keep real launch ownership/binding: replacing this method entirely
        # leaves a permanently unbound queued draft for maintenance to retry.
        service.engine.start_draft.return_value = 'synthetic-execution'
        service._onboarding = Mock()
        service._onboarding.snapshot.side_effect = HarnessError('Broken unrelated OAuth configuration')
        service._poll()
        service.engine.start_draft.assert_called_once()
        draft = self.studio._get('draft', service.engine.start_draft.call_args.args[0])
        self.assertEqual(draft['account_fingerprint'], self.studio.snapshot()['accounts'][0]['fingerprint'])
        self.connector.send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
