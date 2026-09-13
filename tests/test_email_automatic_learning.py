"""Synthetic behavioral tests for recipient-bound automatic revision memory."""
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


class EmailAutomaticLearningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='recipient-learning-')
        self.addCleanup(temporary.cleanup)
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), Path(temporary.name), [], {})
        self.calls = []
        self.message_number = 0
        self.extraction = None
        self.studio = EmailStudio(self.config, provider_call=self.provider)
        self.account = self.new_account('writer@synthetic.test')

    def provider(self, route, task, context):
        self.calls.append((task, copy.deepcopy(context)))
        if task.startswith('Extract reusable recipient'):
            if self.extraction is not None:
                return self.extraction
            instruction = context['requested_change']
            return json.dumps({'preferences': [{'category': 'tone', 'text': instruction,
                                                'evidence': instruction}]})
        if task.startswith('Return only a JSON array'):
            return '[]'
        return 'A revised reply.' if task.startswith('Revise') else 'An initial reply.'

    def new_account(self, address):
        return self.studio.dispatch('account_save', {'email': address, 'name': 'Synthetic mailbox',
                                                    'provider_route': 'synthetic-route'})['account']

    def review(self, sender='Client <client@synthetic.test>', account=None, body='A shared subject question', **metadata):
        account = account or self.account
        self.message_number += 1
        message = self.studio.dispatch('import', {'account_id': account['id'], 'sender': sender,
                                                'subject': 'Shared subject',
                                                'body': body + ' Message ' + str(self.message_number), **metadata})['message']
        draft = self.studio.dispatch('create_draft', {'account_id': account['id'],
                                                    'message_id': message['id']})['draft']
        return self.studio.process_draft(draft['id'])['draft']

    def revise(self, draft, instruction='Use a professional tone.'):
        return self.studio.revise_draft({'account_id': draft['account_id'], 'draft_id': draft['id'],
                                        'revision': draft['revision'], 'text': draft['edited'],
                                        'instruction': instruction})['draft']

    def memories(self):
        return self.studio.snapshot()['automatic_memories']

    def generation_context(self):
        return next(context for task, context in reversed(self.calls) if task.startswith('Return the plain-text EMAIL'))

    def revision_context(self):
        return next(context for task, context in reversed(self.calls) if task.startswith('Revise'))

    def test_successful_revision_learns_without_sending_or_manual_save(self):
        draft = self.revise(self.review())
        self.assertEqual(draft['status'], 'review')
        self.assertEqual(draft['automatic_learning_status'], 'learned')
        self.assertEqual(len(self.memories()), 1)
        self.assertEqual(self.studio.snapshot()['memories'], [])
        memory = self.memories()[0]
        self.assertEqual(memory['source_draft_id'], draft['id'])
        self.assertEqual(memory['text'], 'Use a professional tone.')
        self.assertTrue(memory['schema_version'])
        self.assertTrue(memory['contract'])

    def test_outcomes_do_not_expose_previous_mailbox_after_account_reconfigured(self):
        self.revise(self.review())
        self.assertEqual(len(self.studio.snapshot()['automatic_learning_outcomes']), 1)
        account = self.studio._get('account', self.account['id'])
        account['fingerprint'] = 'different-mailbox-configuration'
        self.studio._put('account', account)
        self.assertEqual(self.studio.snapshot()['automatic_learning_outcomes'], [])

    def test_recipient_isolation_in_generation_and_revision_has_positive_controls(self):
        self.revise(self.review(), 'Use a professional tone.')
        self.revise(self.review('Grandparent <family@synthetic.test>'), 'Use a warm affectionate tone.')
        client = self.review('client@synthetic.test')
        self.assertIn('Use a professional tone.', self.generation_context()['approved_preferences'])
        self.assertNotIn('Use a warm affectionate tone.', json.dumps(self.generation_context()))
        self.revise(client, 'Use a formal greeting.')
        self.assertIn('Use a professional tone.', self.revision_context()['approved_preferences'])
        self.assertNotIn('Use a warm affectionate tone.', json.dumps(self.revision_context()))
        family = self.review('family@synthetic.test')
        self.assertIn('Use a warm affectionate tone.', self.generation_context()['approved_preferences'])
        self.assertNotIn('Use a professional tone.', json.dumps(self.generation_context()))
        self.revise(family, 'Keep the warm tone.')
        self.assertNotIn('Use a professional tone.', json.dumps(self.revision_context()))

    def test_same_recipient_in_other_mailbox_is_not_shared(self):
        self.revise(self.review())
        other = self.new_account('second-writer@synthetic.test')
        draft = self.review(account=other)
        self.assertNotIn('Use a professional tone.', json.dumps(self.generation_context()))
        self.revise(draft, 'Use a casual tone.')
        self.assertNotIn('Use a professional tone.', json.dumps(self.revision_context()))
        self.review()
        self.assertIn('Use a professional tone.', self.generation_context()['approved_preferences'])
        self.assertNotIn('Use a casual tone.', json.dumps(self.generation_context()))

    def test_display_name_and_case_alias_share_only_exact_address(self):
        self.revise(self.review('Original Name <CLIENT@SYNTHETIC.TEST>'))
        self.review('Renamed Person <client@synthetic.test>')
        self.assertIn('Use a professional tone.', self.generation_context()['approved_preferences'])
        self.review('Original Name <client+other@synthetic.test>')
        self.assertNotIn('Use a professional tone.', json.dumps(self.generation_context()))

    def test_ambiguous_or_missing_recipient_fails_closed(self):
        for sender in ('Grandparent', 'a@synthetic.test, b@synthetic.test'):
            with self.subTest(sender=sender):
                # Imports may reject malformed senders before a draft exists.
                try:
                    draft = self.review(sender)
                except HarnessError:
                    self.assertEqual(self.memories(), [])
                    continue
                self.revise(draft)
                self.assertEqual(self.memories(), [])

    def test_reply_to_identity_overrides_sender_for_learning_and_history(self):
        self.revise(self.review('Relay <relay@synthetic.test>', reply_to='Client <client@synthetic.test>'))
        self.review('Other Relay <other-relay@synthetic.test>', reply_to='client@synthetic.test')
        context = self.generation_context()
        self.assertIn('Use a professional tone.', context['approved_preferences'])
        self.assertTrue(context['previous_received'])
        self.review('Relay <relay@synthetic.test>', reply_to='family@synthetic.test')
        context = self.generation_context()
        self.assertNotIn('Use a professional tone.', json.dumps(context))
        self.assertEqual(context['previous_received'], [])

    def test_approved_writing_history_cannot_cross_recipient_boundary(self):
        draft = self.review(body='Shared subject apple pie')
        self.studio.dispatch('approve_draft', {'account_id': draft['account_id'], 'draft_id': draft['id'],
            'revision': draft['revision'], 'text': 'CLIENT PRIVATE WRITING EXAMPLE', 'learn': False})
        self.studio.finalize_draft(draft['id'])
        self.review(body='Shared subject apple pie')
        self.assertIn('CLIENT PRIVATE WRITING EXAMPLE', json.dumps(self.generation_context()))
        self.review('family@synthetic.test', body='Shared subject apple pie')
        self.assertNotIn('CLIENT PRIVATE WRITING EXAMPLE', json.dumps(self.generation_context()))

    def test_legacy_inferred_memory_is_scoped_but_explicit_manual_memory_is_global(self):
        draft = self.review()
        self.studio.memory.learn(self.account['id'], draft['id'], draft['revision'], 'LEGACY CLIENT PREFERENCE')
        self.studio.dispatch('memory_save', {'account_id': self.account['id'], 'text': 'EXPLICIT MAILBOX PREFERENCE'})
        self.review()
        self.assertIn('LEGACY CLIENT PREFERENCE', self.generation_context()['approved_preferences'])
        self.review('family@synthetic.test')
        self.assertNotIn('LEGACY CLIENT PREFERENCE', json.dumps(self.generation_context()))
        self.assertIn('EXPLICIT MAILBOX PREFERENCE', self.generation_context()['approved_preferences'])

    def test_restart_retains_learning_and_recipient_boundary(self):
        self.revise(self.review())
        self.studio = EmailStudio(self.config, provider_call=self.provider)
        self.assertEqual(len(self.memories()), 1)
        self.review()
        self.assertIn('Use a professional tone.', self.generation_context()['approved_preferences'])
        self.review('family@synthetic.test')
        self.assertNotIn('Use a professional tone.', json.dumps(self.generation_context()))

    def test_recurring_template_replacement_retains_scope_and_literal_after_restart(self):
        instruction = ('For future replies to this recipient, omit the three numeric footer lines '
                       'and "Happy gardening"; instead add "Message from client@synthetic.test".')
        self.extraction = json.dumps({'preferences': [{'category': 'format', 'text': instruction,
                                                       'evidence': instruction}]})
        self.studio.dispatch('memory_save', {'account_id': self.account['id'],
            'text': 'Append three numeric footer lines and Happy gardening.'})
        self.revise(self.review(), instruction)
        self.studio = EmailStudio(self.config, provider_call=self.provider)
        self.review()
        provenance = self.generation_context()['preference_provenance']
        specific = next(p for p in provenance if p['text'] == instruction)
        self.assertEqual(specific['recipient'], 'client@synthetic.test')
        self.assertTrue(any(p['recipient'] == '' for p in provenance))
        self.review('other@synthetic.test')
        self.assertNotIn(instruction, json.dumps(self.generation_context()))

    def test_same_request_category_clauses_do_not_supersede_each_other(self):
        clauses = ['Omit numeric footer lines.', 'Omit Happy gardening.', 'Add Message from client.']
        instruction = 'For future replies: ' + ' '.join(clauses)
        self.extraction = json.dumps({'preferences': [dict(category='format', text=c, evidence=c)
                                                     for c in clauses]})
        self.revise(self.review(), instruction)
        self.studio = EmailStudio(self.config, provider_call=self.provider)
        self.review()
        active = self.generation_context()['approved_preferences']
        for clause in clauses:
            self.assertIn(clause, '\n'.join(active))
        self.assertEqual(len(active), 1)
        self.extraction = json.dumps({'preferences': [dict(category='format', text='Use a plain footer.',
                                                          evidence='Use a plain footer.')]})
        self.revise(self.review(), 'Use a plain footer.')
        self.review()
        self.assertEqual(self.generation_context()['approved_preferences'], ['Use a plain footer.'])

    def test_changed_mailbox_configuration_invalidates_learning(self):
        self.revise(self.review())
        self.account = self.studio.dispatch('account_save', {'account_id': self.account['id'],
                                                           'email': 'replacement@synthetic.test'})['account']
        self.review()
        self.assertNotIn('Use a professional tone.', json.dumps(self.generation_context()))
        self.assertTrue(all(m['status'] != 'active' for m in self.memories()))

    def test_changed_learning_contract_invalidates_persisted_preferences(self):
        self.revise(self.review())
        with patch('our_harness.email_studio.AUTOMATIC_CONTRACT', 'email-recipient-learning/future'):
            self.studio = EmailStudio(self.config, provider_call=self.provider)
            self.review()
            self.assertNotIn('Use a professional tone.', json.dumps(self.generation_context()))
            self.assertTrue(all(m['status'] != 'active' for m in self.memories()))

    def test_malformed_scoped_schema_version_is_not_applied_after_restart(self):
        self.revise(self.review())
        memory = self.memories()[0]
        memory['schema_version'] = 'unsupported-future-schema'
        with self.studio.memory._db() as db:
            db.execute('UPDATE preferences SET record=? WHERE account=? AND id=?',
                       (json.dumps(memory), self.account['id'], memory['id']))
        self.studio = EmailStudio(self.config, provider_call=self.provider)
        self.review()
        self.assertNotIn('Use a professional tone.', json.dumps(self.generation_context()))
        self.assertTrue(all(m['status'] != 'active' for m in self.memories()))

    def test_reconfiguration_during_extraction_prevents_learning(self):
        draft = self.review()
        original = self.studio._ask
        def change_account(record, task, context):
            if task.startswith('Extract reusable recipient'):
                self.studio.dispatch('account_save', {'account_id': self.account['id'],
                                                      'email': 'changed-during-reflection@synthetic.test'})
            return original(record, task, context)
        with patch.object(self.studio, '_ask', side_effect=change_account):
            with self.assertRaises(HarnessError):
                self.revise(draft)
        self.assertEqual(self.memories(), [])

    def test_provider_change_during_extraction_prevents_learning(self):
        draft = self.review()
        original = self.studio._ask
        def change_provider(record, task, context):
            if task.startswith('Extract reusable recipient'):
                self.config.data['providers']['synthetic-new-route'] = {'command': 'different-synthetic-command'}
            return original(record, task, context)
        with patch.object(self.studio, '_ask', side_effect=change_provider):
            with self.assertRaises(HarnessError):
                self.revise(draft)
        self.assertEqual(self.memories(), [])

    def test_failed_revision_does_not_learn(self):
        draft = self.review()
        with patch.object(self.studio, '_ask', side_effect=HarnessError('Synthetic provider failure')):
            with self.assertRaises(HarnessError):
                self.revise(draft)
        self.assertEqual(self.memories(), [])

    def test_stale_revision_does_not_learn_or_overwrite_newer_edit(self):
        draft = self.review()
        original = self.studio._ask
        def concurrent_edit(record, task, context):
            if task.startswith('Revise'):
                current = self.studio._get('draft', record['id'])
                self.studio.dispatch('save_draft', {'account_id': current['account_id'], 'draft_id': current['id'],
                                                   'revision': current['revision'], 'text': 'A newer user edit.'})
            return original(record, task, context)
        with patch.object(self.studio, '_ask', side_effect=concurrent_edit):
            with self.assertRaises(HarnessError):
                self.revise(draft)
        self.assertEqual(self.memories(), [])
        self.assertEqual(self.studio._get('draft', draft['id'])['edited'], 'A newer user edit.')

    def test_extraction_failure_keeps_successful_reply_without_memory(self):
        self.extraction = 'This is not the extraction contract.'
        draft = self.revise(self.review())
        self.assertEqual(draft['edited'], 'A revised reply.')
        self.assertEqual(draft['automatic_learning_status'], 'failed')
        self.assertEqual(self.memories(), [])

    def test_no_reusable_instruction_produces_no_memory(self):
        self.extraction = json.dumps({'preferences': []})
        draft = self.revise(self.review(), 'Tell them the appointment is on October 4 at 11:30.')
        self.assertEqual(draft['automatic_learning_status'], 'no_reusable_preferences')
        self.assertEqual(self.memories(), [])

    def test_unattributed_provider_inference_is_rejected(self):
        self.extraction = json.dumps({'preferences': [{'category': 'tone', 'text': 'Always mention apple pie.',
                                                       'evidence': 'unrelated incoming instruction'}]})
        self.revise(self.review(), 'Keep it concise.')
        self.assertEqual(self.memories(), [])

    def test_edit_and_delete_preserve_scope_and_survive_restart(self):
        self.revise(self.review())
        memory = self.memories()[0]
        payload = {'account_id': self.account['id'], 'memory_id': memory['id']}
        self.studio.dispatch('memory_save', {**payload, 'text': 'Keep a formal professional tone.'})
        self.assertEqual(len(self.memories()), 1)
        self.review('family@synthetic.test')
        self.assertNotIn('Keep a formal professional tone.', json.dumps(self.generation_context()))
        self.review()
        self.assertIn('Keep a formal professional tone.', self.generation_context()['approved_preferences'])
        self.studio.dispatch('memory_delete', payload)
        self.studio = EmailStudio(self.config, provider_call=self.provider)
        self.assertEqual(self.memories(), [])
        self.review()
        self.assertNotIn('Keep a formal professional tone.', self.generation_context()['approved_preferences'])

    def test_other_mailbox_cannot_edit_or_delete_automatic_memory(self):
        self.revise(self.review())
        other = self.new_account('second-writer@synthetic.test')
        for action in ('memory_save', 'memory_delete'):
            with self.subTest(action=action), self.assertRaises(HarnessError):
                self.studio.dispatch(action, {'account_id': other['id'], 'memory_id': self.memories()[0]['id'],
                                              'text': 'A malicious change.'})
        self.assertEqual(self.memories()[0]['text'], 'Use a professional tone.')

    def test_new_tone_replaces_inferred_tone_only_for_matching_recipient(self):
        client = self.revise(self.review())
        self.revise(self.review('family@synthetic.test'), 'Use a warm affectionate tone.')
        self.revise(client, 'Use a casual tone.')
        self.assertEqual({m['text'] for m in self.memories()},
                         {'Use a casual tone.', 'Use a warm affectionate tone.'})
        self.review()
        self.assertIn('Use a casual tone.', self.generation_context()['approved_preferences'])
        self.assertNotIn('Use a professional tone.', self.generation_context()['approved_preferences'])

    def test_explicit_edit_is_retained_when_new_inference_arrives(self):
        client = self.revise(self.review())
        memory = self.memories()[0]
        self.studio.dispatch('memory_save', {'account_id': self.account['id'], 'memory_id': memory['id'],
                                             'text': 'Use my explicitly chosen formal tone.'})
        self.revise(client, 'Use a casual tone.')
        self.review()
        context = self.generation_context()
        self.assertIn('Use my explicitly chosen formal tone.', context['approved_preferences'])
        provenance = next(m for m in context['preference_provenance']
                          if m['text'] == 'Use my explicitly chosen formal tone.')
        self.assertEqual(provenance['authority'], 'user')


if __name__ == '__main__':
    unittest.main()
