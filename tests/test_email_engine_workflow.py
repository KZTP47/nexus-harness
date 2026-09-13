"""Nexus service/studio integration with a fake authenticated mailbox boundary."""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.email_service import EmailService
from our_harness.email_studio import EmailStudio
from our_harness.models import HarnessError
from tests.test_email_engine_workspace import TestSecrets


class EmailEngineWorkflowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='portable-workflow-')
        self.addCleanup(temp.cleanup)
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), Path(temp.name), [], {})
        self.config.data['email_engine'] = {'url': 'https://service.example.test', 'token': 'synthetic-token'}
        self.secrets = TestSecrets()
        self.contexts = []
        def provider(route, task, context):
            self.contexts.append((task, context))
            if task.startswith('Return only a JSON array'):
                return '["concise", "Göteborg"]'
            if task.startswith('Extract'):
                return 'Prefer concise replies.'
            return 'Draft prepared for review.'
        self.provider = provider
        self.studio = EmailStudio(self.config, secret_store=self.secrets, provider_call=provider)
        self.client = Mock()
        self.client.account.return_value = {'email': 'owner@example.test', 'name': 'Synthetic mailbox', 'state': 'connected'}
        self.studio.mail_backend._client = self.client
        self.account = self.studio.connect_engine({'remote_account_id': 'remote', 'provider_route': 'portable-route'})['account']
        self.client.sync.return_value = {'messages': [self.mail('one')], 'cursor': 'cursor-one', 'has_more': False}
        self.client.submit_reply.return_value = {'queue_id': 'q-one', 'message_id': 'remote-message'}

    @staticmethod
    def mail(identity, body='Could you help?', sender='sender@example.test', received='2099-01-01T12:00:00+00:00'):
        return dict(source_id=identity, sender=sender, subject='A question', body=body, received_at=received,
                    internet_message_id='<synthetic-' + identity + '@example.test>')

    def auto_draft(self):
        service = EmailService(SimpleNamespace(config=self.config), studio=self.studio)
        # Exercise production polling orchestration, replacing only the external Kestra launch.
        service._start_draft = lambda draft: self.studio.process_draft(draft['id'])
        service._poll_account(self.account['id'])
        return self.studio.snapshot()['drafts'][0]

    def approve(self, draft):
        return self.studio.dispatch('approve_draft', {'account_id': self.account['id'], 'draft_id': draft['id'],
            'revision': draft['revision'], 'text': 'My reviewed response.', 'learn': False})['draft']

    def test_poll_generates_once_approval_queues_restart_check_does_not_resend(self):
        draft = self.auto_draft()
        self.assertEqual(draft['status'], 'review')
        self.auto_draft()
        self.assertEqual(len(self.studio.snapshot()['drafts']), 1)
        self.client.submit_reply.assert_not_called()
        self.approve(draft)
        queued = self.studio.finalize_draft(draft['id'])['draft']
        self.assertEqual(queued['status'], 'submitted')
        self.assertEqual(queued['queue_id'], 'q-one')
        self.studio = EmailStudio(self.config, secret_store=self.secrets, provider_call=self.provider)
        self.studio.mail_backend._client = self.client
        self.studio.finalize_draft(draft['id'])
        self.client.submission.return_value = {'status': 'unknown'}
        payload = {'account_id': self.account['id'], 'draft_id': draft['id']}
        self.assertEqual(self.studio.check_delivery(payload)['draft']['status'], 'delivery_unknown')
        with self.assertRaises(HarnessError):
            self.studio.finalize_draft(draft['id'])
        self.client.submission.return_value = {'status': 'submitted'}
        self.assertEqual(self.studio.check_delivery(payload)['draft']['status'], 'sent')
        self.client.submit_reply.assert_called_once_with('remote', 'one', 'My reviewed response.', draft['id'])

    def test_submission_disconnect_is_ambiguous_and_never_replayed(self):
        draft = self.auto_draft()
        self.approve(draft)
        self.client.submit_reply.side_effect = ConnectionError('lost response after submission')
        with self.assertRaises(HarnessError):
            self.studio.finalize_draft(draft['id'])
        self.assertEqual(self.studio._get('draft', draft['id'])['status'], 'delivery_unknown')
        with self.assertRaises(HarnessError):
            self.studio.finalize_draft(draft['id'])
        with self.assertRaisesRegex(HarnessError, 'queue ID'):
            self.studio.check_delivery({'account_id': self.account['id'], 'draft_id': draft['id']})
        self.client.submit_reply.assert_called_once()

    def test_old_mail_is_memory_not_auto_drafted_and_expansion_respects_recipient(self):
        old = self.mail('old', 'Göteborg correspondence should be concise.', 'sender@example.test', '2020-01-01T12:00:00+00:00')
        other_recipient = self.mail('other-recipient', 'Göteborg concise CUSTOMER_ONLY_SENTINEL', 'customer@example.test', '2020-01-01T12:00:00+00:00')
        self.client.sync.return_value = {'messages': [old, other_recipient], 'cursor': 'history', 'has_more': False}
        service = EmailService(SimpleNamespace(config=self.config), studio=self.studio)
        service._start_draft = lambda draft: self.studio.process_draft(draft['id'])
        service._poll_account(self.account['id'])
        self.assertEqual(self.studio.snapshot()['drafts'], [])
        self.studio = EmailStudio(self.config, secret_store=self.secrets, provider_call=self.provider)
        self.studio.mail_backend._client = self.client
        self.client.sync.return_value = {'messages': [self.mail('new', 'Could you keep it brief?')], 'cursor': 'new', 'has_more': False}
        self.auto_draft()
        drafting = [context for task, context in self.contexts if task.startswith('Return the plain-text EMAIL BODY')][-1]
        self.assertEqual([m['source_id'] for m in drafting['previous_received']], ['old'])
        self.assertNotIn('CUSTOMER_ONLY_SENTINEL', str(drafting))
        self.assertEqual(drafting['previous_received'][0]['account_id'], self.account['id'])
        self.assertEqual(drafting['previous_approved_replies'], [])

    def test_quarantine_is_durable_before_cursor_and_clears_after_recovery(self):
        self.client.sync.return_value = {'messages': [], 'cursor': 'after-failure', 'has_more': False,
                                        'failed_messages': [{'source_id': 'broken', 'error': 'Body temporarily unavailable'}]}
        original_put = self.studio._put
        def reject_quarantine(kind, value):
            if kind == 'failed_import':
                raise HarnessError('Synthetic storage failure')
            return original_put(kind, value)
        with patch.object(self.studio, '_put', side_effect=reject_quarantine):
            with self.assertRaises(HarnessError):
                self.studio.dispatch('sync', {'account_id': self.account['id']})
        self.assertEqual(self.studio._get('account', self.account['id'])['cursor'], '')
        def verify_order(kind, value):
            if kind == 'account' and value.get('cursor') == 'after-failure':
                self.assertEqual(self.studio._all('failed_import', self.account['id'])[0]['source_id'], 'broken')
            return original_put(kind, value)
        with patch.object(self.studio, '_put', side_effect=verify_order):
            self.studio.dispatch('sync', {'account_id': self.account['id']})
        self.studio = EmailStudio(self.config, secret_store=self.secrets, provider_call=self.provider)
        self.studio.mail_backend._client = self.client
        self.assertEqual(self.studio._all('failed_import', self.account['id'])[0]['source_id'], 'broken')
        self.assertEqual(self.studio._get('account', self.account['id'])['cursor'], 'after-failure')
        self.client.sync.return_value = {'messages': [self.mail('broken')], 'cursor': 'recovered', 'has_more': False}
        self.studio.dispatch('sync', {'account_id': self.account['id']})
        self.assertEqual(self.studio._all('failed_import', self.account['id']), [])
        self.assertEqual(self.studio.snapshot()['messages'][0]['source_id'], 'broken')

    def test_answered_draft_and_missing_timestamp_are_not_automatically_drafted(self):
        answered = {**self.mail('answered'), 'answered': True}
        draft = {**self.mail('draft'), 'draft': True}
        unknown_date = self.mail('unknown', received='')
        self.client.sync.return_value = {'messages': [answered, draft, unknown_date], 'cursor': 'checked', 'has_more': False}
        service = EmailService(SimpleNamespace(config=self.config), studio=self.studio)
        service._start_draft = Mock()
        service._poll_account(self.account['id'])
        self.assertEqual(len(self.studio.snapshot()['messages']), 3)
        self.assertTrue(all(not m['auto_draft_eligible'] for m in self.studio.snapshot()['messages']))
        service._start_draft.assert_not_called()

    def test_full_history_search_survives_restart_and_user_preference_edits_and_forget_apply(self):
        # Sixty irrelevant later messages must not displace an older fact.
        historical = [self.mail('old-relevant', 'Göteborg correspondence must be concise.', 'sender@example.test', '2020-01-01T12:00:00+00:00')]
        historical.append(self.mail('other-recipient', 'Göteborg concise CUSTOMER_ONLY_SENTINEL', 'customer@example.test', '2020-01-01T12:00:00+00:00'))
        historical += [self.mail('neighbor-' + str(i), 'Unrelated lunch arrangement.', 'unrelated@example.test', '2020-02-01T12:00:00+00:00') for i in range(60)]
        for message in historical:
            message['subject'] = 'Archived correspondence' if message['source_id'] == 'old-relevant' else 'Lunch arrangement'
        self.client.sync.return_value = {'messages': historical, 'cursor': 'sixty-one', 'has_more': False}
        self.studio.dispatch('sync', {'account_id': self.account['id']})
        other = self.studio.dispatch('account_save', {'email': 'other@example.test', 'provider_route': 'portable-route'})['account']
        self.studio.dispatch('import', {'account_id': other['id'], 'sender': 'private@example.test', 'subject': 'Göteborg concise', 'body': 'TENANT_PRIVATE_SENTINEL'})
        memory = self.studio.dispatch('memory_save', {'account_id': self.account['id'], 'text': 'Prefer short replies.'})['memory']
        self.studio.dispatch('memory_save', {'account_id': self.account['id'], 'memory_id': memory['id'], 'text': 'Prefer detailed explanations.'})
        with self.assertRaises(HarnessError):
            self.studio.dispatch('memory_delete', {'account_id': other['id'], 'memory_id': memory['id']})
        self.studio = EmailStudio(self.config, secret_store=self.secrets, provider_call=self.provider)
        self.studio.mail_backend._client = self.client
        self.client.sync.return_value = {'messages': [self.mail('fresh', 'Could you keep it brief?')], 'cursor': 'fresh', 'has_more': False}
        self.auto_draft()
        context = [context for task, context in self.contexts if task.startswith('Return the plain-text EMAIL BODY')][-1]
        self.assertEqual([m['source_id'] for m in context['previous_received']], ['old-relevant'])
        self.assertNotIn('TENANT_PRIVATE_SENTINEL', str(context))
        self.assertNotIn('CUSTOMER_ONLY_SENTINEL', str(context))
        self.assertEqual(context['approved_preferences'], ['Prefer detailed explanations.'])
        self.studio.dispatch('memory_delete', {'account_id': self.account['id'], 'memory_id': memory['id']})
        self.studio = EmailStudio(self.config, secret_store=self.secrets, provider_call=self.provider)
        self.studio.mail_backend._client = self.client
        self.client.sync.return_value = {'messages': [self.mail('fresh-two', 'Another brief reply please')], 'cursor': 'fresh-two', 'has_more': False}
        self.auto_draft()
        latest = [context for task, context in self.contexts if task.startswith('Return the plain-text EMAIL BODY')][-1]
        self.assertEqual(latest['approved_preferences'], [])


    def test_service_mailbox_identity_change_blocks_sync_and_sending(self):
        draft = self.auto_draft()
        draft = self.studio.snapshot()['drafts'][0]
        self.studio.dispatch('approve_draft', {'account_id': self.account['id'], 'draft_id': draft['id'],
            'revision': draft['revision'], 'text': 'Explicitly reviewed reply.', 'learn': False})
        self.client.account.return_value['email'] = 'replacement@example.test'
        self.client.sync.reset_mock()
        with self.assertRaises(HarnessError):
            self.studio.dispatch('sync', {'account_id': self.account['id']})
        self.client.sync.assert_not_called()
        with self.assertRaises(HarnessError):
            self.studio.finalize_draft(draft['id'])
        self.client.submit_reply.assert_not_called()


if __name__ == '__main__':
    unittest.main()
