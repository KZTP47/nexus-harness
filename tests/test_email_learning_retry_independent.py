"""Independent synthetic regressions for recovery of recipient learning."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.email_studio import EmailStudio
from our_harness.models import HarnessError


class EmailLearningRecoveryIndependentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='independent-recovery-')
        self.addCleanup(temporary.cleanup)
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), Path(temporary.name), [], {})
        self.calls = []
        self.message_number = 0
        self.extraction = None
        self.studio = EmailStudio(self.config, provider_call=self.provider)
        self.account = self.studio.dispatch('account_save', {
            'email': 'writer@independent.test', 'provider_route': 'independent-route'})['account']
        self.draft = self.new_draft('client@independent.test')
        self.draft.update(revision=8, edited='This current reply must remain exactly unchanged.',
                          revision_requests=['Discuss the scheduled appointment.',
                                             'Use British spelling for this person.',
                                             'Say the appointment is on Thursday.'],
                          automatic_learning_status='no_reusable_preferences',
                          automatic_learning_revision=8)
        self.studio._put('draft', self.draft)

    def provider(self, route, task, context):
        self.calls.append((task, copy.deepcopy(context)))
        if task.startswith('Extract reusable recipient'):
            if self.extraction is not None:
                return self.extraction
            return json.dumps({'preferences': [{'category': 'language',
                'text': 'Use British spelling.', 'evidence': context['requested_change']}]})
        if task.startswith('Return only a JSON array'):
            return '[]'
        return 'Initial synthetic draft.'

    def new_draft(self, sender):
        self.message_number += 1
        message = self.studio.dispatch('import', {'account_id': self.account['id'],
            'sender': sender, 'subject': 'Synthetic example',
            'body': 'Please reply. Example ' + str(self.message_number)})['message']
        draft = self.studio.dispatch('create_draft', {'account_id': self.account['id'],
            'message_id': message['id']})['draft']
        return self.studio.process_draft(draft['id'])['draft']

    def current(self):
        return self.studio._get('draft', self.draft['id'])

    def memories(self):
        return self.studio.snapshot()['automatic_memories']

    def outcomes(self):
        return [entry for entry in self.studio.snapshot()['automatic_learning_outcomes']
                if entry['draft_id'] == self.draft['id']]

    def retry_payload(self, index=1):
        entry = self.outcomes()[index]
        return {key: entry[key] for key in
                ('account_id', 'draft_id', 'revision', 'request_index', 'request_fingerprint')}

    def retry(self, payload=None):
        return self.studio.dispatch('retry_automatic_learning', payload or self.retry_payload())['draft']

    def test_legacy_earlier_request_recovers_without_inventing_reply_history(self):
        before = self.current()
        self.assertEqual([e['status'] for e in self.outcomes()],
                         ['not_recorded', 'not_recorded', 'no_reusable_preferences'])
        self.calls.clear()
        after = self.retry()
        for key in ('edited', 'original', 'revision', 'status', 'approved_at', 'submission_id', 'revision_requests'):
            self.assertEqual(after.get(key), before.get(key), key)
        self.assertEqual(len(self.calls), 1)
        context = self.calls[0][1]
        self.assertEqual(context['requested_change'], before['revision_requests'][1])
        self.assertEqual(context['original_reply'], '')
        self.assertEqual(context['revised_reply'], '')
        self.assertEqual(self.outcomes()[1]['status'], 'learned')
        self.assertEqual(self.outcomes()[2]['status'], 'no_reusable_preferences')
        self.assertEqual(len(self.memories()), 1)
        self.assertEqual(self.memories()[0]['recipient'], 'client@independent.test')
        self.new_draft('grandma@independent.test')
        self.assertNotIn('Use British spelling.', json.dumps(self.calls[-1][1]))
        self.new_draft('client@independent.test')
        self.assertIn('Use British spelling.', json.dumps(self.calls[-1][1]))

    def test_replay_after_restart_keeps_one_memory(self):
        payload = self.retry_payload()
        self.retry(payload)
        ids = [entry['id'] for entry in self.memories()]
        self.studio = EmailStudio(self.config, provider_call=self.provider)
        self.retry(payload)
        self.assertEqual([entry['id'] for entry in self.memories()], ids)
        self.assertEqual(self.outcomes()[1]['status'], 'learned')

    def test_provider_failure_is_visible_and_retryable_without_draft_mutation(self):
        self.extraction = 'not valid JSON'
        before = self.current()
        self.retry()
        self.assertEqual(self.outcomes()[1]['status'], 'failed')
        self.assertTrue(self.outcomes()[1]['retry_available'])
        self.assertEqual(self.current()['edited'], before['edited'])
        self.assertEqual(self.current()['revision'], before['revision'])
        self.assertEqual(self.memories(), [])
        self.extraction = None
        self.retry()
        self.assertEqual(self.outcomes()[1]['status'], 'learned')

    def test_stale_revision_and_changed_request_fingerprint_are_rejected(self):
        for changed in ({'revision': 7}, {'request_fingerprint': 'stale-request'}):
            with self.subTest(changed=changed), self.assertRaises(HarnessError):
                self.retry(self.retry_payload() | changed)
        self.assertEqual(self.memories(), [])

    def test_changed_mailbox_binding_rejects_retry(self):
        payload = self.retry_payload()
        self.studio.dispatch('account_save', {'account_id': self.account['id'],
                                            'email': 'replacement@independent.test'})
        with self.assertRaises(HarnessError):
            self.retry(payload)
        self.assertEqual(self.memories(), [])

    def test_changed_source_binding_rejects_retry(self):
        payload = self.retry_payload()
        message = self.studio._get('message', self.draft['message_id'])
        message['account_fingerprint'] = 'obsolete-configuration'
        self.studio._put('message', message)
        with self.assertRaises(HarnessError):
            self.retry(payload)
        self.assertEqual(self.memories(), [])

    def test_explicit_retry_authorizes_current_saved_route_configuration(self):
        payload = self.retry_payload()
        self.config.data['providers']['different-route'] = {'command': 'new-command'}
        before = self.current()
        self.retry(payload)
        self.assertEqual(len(self.memories()), 1)
        self.assertEqual(self.current()['revision'], before['revision'])
        self.assertEqual(self.current()['edited'], before['edited'])

    def test_provider_change_during_extraction_rejects_retry(self):
        original = self.studio._ask
        def racing_provider(draft, task, context):
            self.config.data['providers']['different-route'] = {'command': 'new-command'}
            return original(draft, task, context)
        with patch.object(self.studio, '_ask', side_effect=racing_provider):
            with self.assertRaises(HarnessError):
                self.retry()
        self.assertEqual(self.memories(), [])

    def test_newer_edit_during_extraction_survives_and_rejects_old_retry(self):
        payload = self.retry_payload()
        original = self.studio._ask
        def racing_edit(draft, task, context):
            self.studio.dispatch('save_draft', {'account_id': self.account['id'],
                'draft_id': draft['id'], 'revision': draft['revision'], 'text': 'Newer human reply.'})
            return original(draft, task, context)
        with patch.object(self.studio, '_ask', side_effect=racing_edit):
            with self.assertRaises(HarnessError):
                self.retry(payload)
        with self.assertRaises(HarnessError):
            self.retry(payload)
        self.assertEqual(self.current()['edited'], 'Newer human reply.')
        self.assertEqual(self.memories(), [])

    def test_changed_source_during_extraction_rejects_learning(self):
        original = self.studio._ask
        def racing_source(draft, task, context):
            message = self.studio._get('message', draft['message_id'])
            message['reply_to'] = 'grandma@independent.test'
            self.studio._put('message', message)
            return original(draft, task, context)
        with patch.object(self.studio, '_ask', side_effect=racing_source):
            with self.assertRaises(HarnessError):
                self.retry()
        self.assertEqual(self.memories(), [])

    def test_sliding_history_preserves_each_request_status(self):
        self.retry()
        self.extraction = '{"preferences":[]}'
        for index in range(10):
            current = self.current()
            self.studio.revise_draft({'account_id': self.account['id'], 'draft_id': current['id'],
                'revision': current['revision'], 'text': current['edited'],
                'instruction': 'Set the synthetic appointment to day ' + str(index)})
        entries = self.outcomes()
        self.assertEqual(len(entries), 12)
        self.assertEqual(entries[0]['requested_change'], 'Use British spelling for this person.')
        self.assertEqual(entries[0]['status'], 'learned')
        self.assertTrue(all(e['status'] == 'no_reusable_preferences' for e in entries[1:]))
        self.assertEqual([e['request_index'] for e in entries], list(range(12)))
        self.studio = EmailStudio(self.config, provider_call=self.provider)
        self.assertEqual(self.outcomes(), entries)
