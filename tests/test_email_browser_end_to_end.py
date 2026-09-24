"""Opt-in joined mail workflow through a real worker process and Chromium.

All provider requests are routed to local synthetic DOM fixtures. AI completion
is a deterministic callback; this test does not certify a live AI or Kestra.
"""
import copy
import json
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.email_local import LocalMail
from our_harness.email_service import EmailService
from our_harness.email_studio import EmailStudio
from our_harness.models import HarnessError


@unittest.skipUnless(os.environ.get('NEXUS_TEST_BROWSER_E2E') == '1',
                     'Set NEXUS_TEST_BROWSER_E2E=1 for real offline Chromium workflow tests.')
class BrowserEndToEndTests(unittest.TestCase):
    def test_poll_review_browser_send_memory_restart_and_modes(self):
        node = shutil.which('node')
        self.assertTrue(node, 'Node is required for the opted-in browser workflow test.')
        worker = Path(__file__).resolve().parents[1] / 'desktop' / 'email-browser-e2e-worker.cjs'
        for kind, initial_mode in [('browser_outlook', 'headed'), ('browser_gmail', 'headless')]:
            with self.subTest(provider=kind), tempfile.TemporaryDirectory(prefix='nexus-mail-e2e-') as directory:
                root = Path(directory)
                state_file, receipt_file = root / 'fixture.json', root / 'sent.json'
                fixture = dict(provider=kind, email='owner@example.test', sender='colleague@example.test',
                    recipient='colleague@example.test', subject='Project question', body='Can you confirm the project plan?',
                    messageId='original-one', rowId='thread-one', acknowledge=True)
                state_file.write_text(json.dumps(fixture), encoding='utf-8')
                calls = []

                def provider(route, task, context):
                    calls.append((task, context))
                    if task.startswith('Extract reusable recipient'):
                        return json.dumps({'preferences': [{'category': 'length', 'text': 'Use concise replies.',
                                                            'evidence': context['requested_change']}]})
                    if task.startswith('Extract'):
                        return 'Use concise replies.'
                    return 'Concise remembered answer.' if context.get('approved_preferences') else 'Initial AI answer.'

                config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), root, [], {})
                environment = {'NEXUS_EMAIL_BROWSER_NODE': node, 'NEXUS_EMAIL_BROWSER_WORKER': str(worker),
                    'NEXUS_BROWSER_E2E_STATE': str(state_file), 'NEXUS_BROWSER_E2E_RECEIPT': str(receipt_file)}
                local, service = None, None

                def setup():
                    mail = LocalMail(root / '.harness' / 'email-studio')
                    studio = EmailStudio(config, provider_call=provider, local_mail=mail)
                    owner = EmailService(SimpleNamespace(config=config), studio=studio, engine=Mock())
                    # Exercise the real poll/import/queue boundary; only the
                    # orchestration dispatch is replaced by its draft callback.
                    owner._start_draft = lambda draft: studio.process_draft(draft['id'])
                    return mail, studio, owner

                with patch.dict(os.environ, environment):
                    try:
                        local, studio, service = setup()
                        connection = local.open(kind, browser_mode=initial_mode)
                        self.assertEqual(connection['actual_browser_mode'], 'headed')
                        account = studio.connect_local(kind, connection['id'],
                            {'provider_route':'synthetic-route', 'poll_enabled':True})['account']
                        self.assertEqual(local.status(kind, connection['id'])['actual_browser_mode'], initial_mode)
                        service._poll_account(account['id'])
                        snapshot = studio.snapshot()
                        self.assertEqual(len(snapshot['messages']), 1)
                        self.assertEqual(len(snapshot['drafts']), 1)
                        draft = snapshot['drafts'][0]
                        self.assertEqual(draft['status'], 'review')
                        self.assertEqual(draft['edited'], 'Initial AI answer.')
                        self.assertFalse(receipt_file.exists())
                        with self.assertRaises(HarnessError):
                            studio.finalize_draft(draft['id'])
                        self.assertTrue(service.dispatch('prepare_draft', dict(account_id=account['id'], draft_id=draft['id'], revision=draft['revision'], intent='revise', text='My UI edit.'))['ready'])
                        revised = studio.revise_draft(dict(account_id=account['id'], draft_id=draft['id'],
                            revision=draft['revision'], text='My UI edit.', instruction='Make it concise.'))['draft']
                        revision_context = next(context for task, context in reversed(calls) if task.startswith('Revise'))
                        self.assertEqual(revision_context['current_reply'], 'My UI edit.')
                        self.assertEqual(revised['automatic_learning_status'], 'learned')
                        self.assertEqual(studio.snapshot()['automatic_memories'][0]['recipient'], fixture['recipient'])
                        self.assertFalse(receipt_file.exists(), 'Automatic learning never sends the reply')
                        approved_body = 'Exactly approved first line.\nConcise second line.'
                        self.assertTrue(service.dispatch('prepare_draft', dict(account_id=account['id'], draft_id=draft['id'], revision=revised['revision'], intent='send', text=approved_body))['ready'])
                        self.assertFalse(receipt_file.exists(), 'Readiness must never dispatch')
                        studio.dispatch('approve_draft', dict(account_id=account['id'], draft_id=draft['id'],
                            revision=revised['revision'], text=approved_body, learn=True, approval_contract='browser-send/v1'))
                        sent = studio.finalize_draft(draft['id'])['draft']
                        self.assertEqual(sent['status'], 'sent')
                        receipt = json.loads(receipt_file.read_text())
                        self.assertEqual(receipt['sendCount'], 1)
                        self.assertEqual(receipt['body'], approved_body)
                        self.assertEqual(receipt['recipient'], fixture['recipient'])
                        service.close()
                        local, studio, service = setup()
                        self.assertEqual(studio.snapshot()['automatic_memories'][0]['recipient'], fixture['recipient'])
                        persisted = local.snapshot()[0]
                        self.assertEqual(persisted['browser_mode'], initial_mode)
                        self.assertEqual(persisted['config_fingerprint'], connection['config_fingerprint'])
                        studio.finalize_draft(draft['id'])
                        self.assertEqual(json.loads(receipt_file.read_text())['sendCount'], 1)
                        service._poll_account(account['id'])
                        self.assertEqual(len(studio.snapshot()['drafts']), 1)
                        # Also replay the same worker submission after process
                        # restart: durable worker receipt must prevent another click.
                        original = studio._get('message', draft['message_id'])
                        replay = local.adapter(kind).submit_reply(connection['id'], original, approved_body, draft['id'])
                        self.assertEqual(replay['status'], 'sent')
                        self.assertEqual(json.loads(receipt_file.read_text())['sendCount'], 1)
                        next_mode = 'headless' if initial_mode == 'headed' else 'headed'
                        changed = local.configure_mode(kind, connection['id'], next_mode)
                        self.assertEqual(changed['config_fingerprint'], connection['config_fingerprint'])
                        self.assertEqual(local.status(kind, connection['id'])['actual_browser_mode'], next_mode)
                        fixture.update(messageId='original-two', rowId='thread-two',
                            subject='Project follow-up', body='Please confirm the next project step.')
                        state_file.write_text(json.dumps(fixture), encoding='utf-8')
                        service._poll_account(account['id'])
                        latest = studio.snapshot()['drafts'][-1]
                        self.assertEqual(len(studio.snapshot()['drafts']), 2)
                        self.assertEqual(latest['edited'], 'Concise remembered answer.')
                        self.assertIn('Use concise replies.', calls[-1][1]['approved_preferences'])
                        self.assertTrue(calls[-1][1]['previous_received'])
                        self.assertTrue(calls[-1][1]['previous_approved_replies'])
                        self.assertEqual(json.loads(receipt_file.read_text())['sendCount'], 1)
                    finally:
                        if service:
                            service.close()
                        elif local:
                            local.close()


if __name__ == '__main__':
    unittest.main()
