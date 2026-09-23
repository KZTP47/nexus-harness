import copy
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.email_service import EmailService
from our_harness.email_studio import EmailStudio
from our_harness.models import HarnessError


class LocalAdapter:
    def __init__(self, kind):
        self.connection = dict(id='local-id', provider=kind, state='connected',
                               email='owner@example.test', name='Owner', config_fingerprint='v1')
        self.messages = []
        self.cursor = ''
        self.sent = []
        self.send_result = {'status': 'sent'}

    def status(self, kind, identity):
        return dict(self.connection)

    def adapter(self, kind):
        return self

    def sync(self, identity, cursor):
        self.cursor = cursor
        messages = [m for m in self.messages if m.get('browser_reference', {}).get('provider') == self.connection['provider']]
        return dict(messages=messages, cursor=str(len(self.messages)), warnings=[])

    def snapshot(self):
        return [dict(self.connection)]

    def prepare_reply(self, identity, incoming, body, submission_id):
        self.prepared = (identity, incoming, body, submission_id)
        return {'status': 'ready'}

    def submit_reply(self, identity, incoming, body, submission_id):
        self.sent.append((identity, incoming, body, submission_id))
        if isinstance(self.send_result, Exception):
            raise self.send_result
        return dict(self.send_result)

    def close(self):
        pass


class LocalWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mail-workflow-')
        self.addCleanup(self.temp.cleanup)
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), Path(self.temp.name), [], {})
        self.calls = []
        self.adapter = LocalAdapter('browser_outlook')
        self.studio = self.reopen()
        self.account = self.studio.connect_local('browser_outlook', 'local-id', {'provider_route': 'fixture-route'})['account']

    def provider(self, route, task, context):
        self.calls.append((task, context))
        if task.startswith('Extract'):
            return 'Use concise replies.'
        return 'Short answer.' if context.get('approved_preferences') else 'Thank you for your detailed message.'

    def reopen(self):
        return EmailStudio(self.config, provider_call=self.provider, local_mail=self.adapter)

    def arrive(self, source='one', body='Can you help?'):
        self.adapter.messages.append(dict(source_id=source, sender='colleague@example.test', subject=source, body=body,
            browser_reference={'contract': 'browser-reply/v1', 'provider': self.adapter.connection['provider'],
                               'source_hash': source, 'row_id': source, 'row_attr': 'data-convid'}))

    def service(self):
        service = EmailService(SimpleNamespace(config=self.config), studio=self.studio, engine=Mock())
        service._start_draft = lambda draft: self.studio.process_draft(draft['id'])
        return service

    def test_empty_browser_message_is_visible_without_automatic_draft(self):
        self.arrive('empty-body', body='')
        self.service()._poll_account(self.account['id'])
        snapshot = self.studio.snapshot()
        self.assertEqual(len(snapshot['messages']), 1)
        self.assertEqual(snapshot['messages'][0]['body'], '')
        self.assertFalse(snapshot['messages'][0]['auto_draft_eligible'])
        self.assertEqual(snapshot['drafts'], [])
        self.assertEqual(self.adapter.sent, [])

    def test_all_three_choices_automatically_draft_and_remember_after_restart(self):
        for kind in ('browser_outlook', 'browser_gmail', 'classic_outlook'):
            with self.subTest(kind=kind):
                self.adapter.connection['provider'] = kind
                account = self.studio.connect_local(kind, 'local-id', {'provider_route': 'fixture-route'})['account']
                self.arrive(kind)
                service = self.service()
                service._poll_account(account['id'])
                drafts = [d for d in self.studio.snapshot()['drafts'] if d['account_id'] == account['id']]
                draft = drafts[-1]
                self.assertEqual(draft['status'], 'review')
                before = len(drafts)
                service._poll_account(account['id'])
                self.assertEqual(len([d for d in self.studio.snapshot()['drafts'] if d['account_id'] == account['id']]), before)
                draft = self.studio.revise_draft(dict(account_id=account['id'], draft_id=draft['id'],
                    revision=draft['revision'], text='My own edited text.', instruction='Make this shorter.'))['draft']
                revision_call = next(call for call in reversed(self.calls) if call[0].startswith('Revise the supplied reply'))
                self.assertEqual(revision_call[1]['current_reply'], 'My own edited text.')
                self.studio.dispatch('approve_draft', dict(account_id=account['id'], draft_id=draft['id'],
                    revision=draft['revision'], text='Concise user-approved answer.', learn=True, approval_contract='browser-send/v1'))
                done = self.studio.finalize_draft(draft['id'])['draft']
                if kind == 'classic_outlook':
                    self.assertEqual(done['status'], 'exported')
                    self.assertTrue(Path(done['export_path']).is_file())
                else:
                    self.assertEqual(done['status'], 'sent')
                    self.assertEqual(done['delivery_status'], 'browser_confirmed')
                    self.assertEqual(self.adapter.sent[-1][2], 'Concise user-approved answer.')
                self.studio = self.reopen()
                self.arrive(kind + '-next')
                self.service()._poll_account(account['id'])
                self.assertIn('Use concise replies.', self.calls[-1][1]['approved_preferences'])
                self.assertTrue(self.calls[-1][1]['previous_received'])
                self.assertTrue(self.calls[-1][1]['previous_approved_replies'])
                self.assertEqual(self.studio.snapshot()['drafts'][-1]['edited'], 'Short answer.')

    def draft(self):
        self.arrive()
        self.service()._poll_account(self.account['id'])
        return self.studio.snapshot()['drafts'][0]

    def approve(self, draft):
        return self.studio.dispatch('approve_draft', dict(account_id=self.account['id'], draft_id=draft['id'],
            revision=draft['revision'], text='Precisely approved reply.', learn=False, approval_contract='browser-send/v1'))['draft']

    def test_failed_execution_recovers_frozen_approval_after_ai_route_changes(self):
        draft = self.approve(self.draft())
        draft['execution_id'] = 'failed-execution'
        self.studio._put('draft', draft)
        self.studio = self.reopen()
        service = self.service()
        service._background = lambda key, work: work()
        service.engine.execution_status.return_value = {'state': {'current': 'FAILED'}}
        phases = []
        def start(identity):
            recovered = self.studio.process_draft(identity)['draft']
            self.assertEqual(recovered['edited'], draft['edited'])
            self.assertEqual(recovered['revision'], draft['revision'])
            phases.append('pause')
            return 'replacement-execution'
        def resume(identity):
            self.assertEqual(identity, 'replacement-execution')
            self.assertEqual(phases, ['pause'])
            self.studio.finalize_draft(draft['id'])
        service.engine.start_draft.side_effect = start
        service.engine.resume.side_effect = resume
        before = len(self.calls)
        with patch.object(self.studio, '_route_fingerprint', side_effect=HarnessError('AI route no longer exists')):
            service._resume(draft)
        done = self.studio._get('draft', draft['id'])
        self.assertEqual(done['status'], 'sent')
        self.assertEqual(done['approved_at'], draft['approved_at'])
        self.assertEqual(self.adapter.sent[0][2], draft['edited'])
        self.assertEqual(len(self.calls), before)
        self.studio = self.reopen()
        self.studio.finalize_draft(draft['id'])
        self.assertEqual(len(self.adapter.sent), 1)

    def test_changed_ai_route_defers_learning_until_explicit_retry_without_resend(self):
        draft = self.approve(self.draft())
        draft['learn'] = True
        self.studio._put('draft', draft)
        before = len(self.calls)
        with patch.object(self.studio, '_route_fingerprint', return_value='changed-route'):
            done = self.studio.finalize_draft(draft['id'])['draft']
            self.assertEqual(done['status'], 'sent')
            self.assertIn('AI connection changed', done['learning_error'])
            self.assertEqual(len(self.calls), before)
            self.studio.dispatch('retry_learning', {'account_id': draft['account_id'], 'draft_id': draft['id']})
        self.assertTrue(self.studio._get('draft', draft['id'])['learning_complete'])
        self.assertEqual(len(self.calls), before + 1)
        self.assertEqual(len(self.adapter.sent), 1)

    def test_incomplete_or_changed_approval_and_changed_source_cannot_send(self):
        draft = self.approve(self.draft())
        for corruption in ({'approved_at': ''}, {'approved_revision': draft['revision'] + 1}, {'edited': ''}):
            with self.subTest(corruption=corruption):
                self.studio._put('draft', {**draft, **corruption})
                with self.assertRaisesRegex(HarnessError, 'saved approval'):
                    self.studio.finalize_draft(draft['id'])
                self.assertFalse(self.adapter.sent)
        self.studio._put('draft', draft)
        incoming = self.studio._get('message', draft['message_id'], draft['account_id'])
        incoming['account_fingerprint'] = 'different-mailbox'
        self.studio._put('message', incoming)
        with self.assertRaisesRegex(HarnessError, 'source message'):
            self.studio.finalize_draft(draft['id'])
        self.assertFalse(self.adapter.sent)

    def test_old_client_export_approval_cannot_authorize_browser_send(self):
        draft = self.draft()
        with self.assertRaisesRegex(HarnessError, 'export approval'):
            self.studio.dispatch('approve_draft', dict(account_id=self.account['id'], draft_id=draft['id'],
                revision=draft['revision'], text='Legacy export-only approval.', learn=False))
        self.assertEqual(self.studio._get('draft', draft['id'])['status'], 'review')
        self.assertFalse(self.adapter.sent)

    def uncertain(self):
        draft = self.approve(self.draft())
        self.adapter.send_result = {'status': 'unknown'}
        with self.assertRaisesRegex(HarnessError, 'unknown'):
            self.studio.finalize_draft(draft['id'])
        return self.studio._get('draft', draft['id'])

    def test_manual_browser_confirmation_records_evidence_without_resending_after_restart(self):
        draft = self.uncertain()
        draft['learn'] = True
        self.studio._put('draft', draft)
        payload = dict(account_id=draft['account_id'], draft_id=draft['id'], revision=draft['revision'],
                       confirmation_contract='browser-delivery-confirmation/v1')
        service = self.service()
        before = len(self.calls)
        with patch.object(self.adapter, 'status', side_effect=AssertionError('No browser access permitted')):
            done = service.dispatch('confirm_browser_delivery', payload)['draft']
        self.assertEqual(done['status'], 'sent')
        self.assertEqual(done['delivery_status'], 'user_confirmed')
        self.assertEqual(done['edited'], draft['edited'])
        self.assertEqual(done['revision'], draft['revision'])
        self.assertEqual(done['delivery_confirmation']['submission_id'], draft['submission_id'])
        self.assertTrue(done['delivery_confirmed_at'])
        self.assertTrue(done['learning_error'])
        self.assertEqual(len(self.calls), before)
        self.studio = self.reopen()
        self.assertEqual(self.studio._get('draft', draft['id'])['delivery_status'], 'user_confirmed')
        self.studio.finalize_draft(draft['id'])
        self.assertEqual(len(self.calls), before)
        with self.assertRaisesRegex(HarnessError, 'already approved'):
            self.service().dispatch('resume_draft', payload)
        with self.assertRaises(HarnessError):
            self.studio.dispatch('confirm_browser_delivery', payload)
        with self.assertRaises(HarnessError):
            self.studio.dispatch('retry_draft', payload)
        self.assertEqual(len(self.adapter.sent), 1)

    def test_manual_browser_confirmation_rejects_missing_stale_and_unbound_approvals(self):
        draft = self.uncertain()
        payload = dict(account_id=draft['account_id'], draft_id=draft['id'], revision=draft['revision'],
                       confirmation_contract='browser-delivery-confirmation/v1')
        for fields in ({'confirmation_contract': ''}, {'revision': draft['revision'] - 1}):
            with self.subTest(fields=fields), self.assertRaises(HarnessError):
                self.studio.dispatch('confirm_browser_delivery', {**payload, **fields})
        for fields in ({'status': 'approved'}, {'approval_contract': ''}, {'submission_contract': ''},
                       {'submission_id': ''}, {'approved_revision': -1}, {'approved_at': ''},
                       {'account_fingerprint': 'another-account'}):
            with self.subTest(fields=fields):
                self.studio._put('draft', {**draft, **fields})
                with self.assertRaises(HarnessError):
                    self.studio.dispatch('confirm_browser_delivery', payload)
        self.studio._put('draft', draft)
        account = self.studio._get('account', draft['account_id'])
        account['kind'] = 'classic_outlook'
        self.studio._put('account', account)
        with self.assertRaisesRegex(HarnessError, 'browser delivery'):
            self.studio.dispatch('confirm_browser_delivery', payload)
        self.assertEqual(len(self.adapter.sent), 1)

    def test_persisted_legacy_export_approval_requires_new_send_review(self):
        draft = self.approve(self.draft())
        draft.pop('approval_contract')
        self.studio._put('draft', draft)
        self.studio = self.reopen()
        with self.assertRaisesRegex(HarnessError, 'approved for export only'):
            self.studio.finalize_draft(draft['id'])
        current = self.studio._get('draft', draft['id'])
        self.assertEqual(current['status'], 'review')
        self.assertNotIn('approved_at', current)
        self.assertFalse(self.adapter.sent)
        self.approve(current)
        self.assertEqual(self.studio.finalize_draft(draft['id'])['draft']['status'], 'sent')
        self.assertEqual(len(self.adapter.sent), 1)

    def test_browser_no_send_before_approval_then_exactly_once_after_restart(self):
        draft = self.draft()
        with self.assertRaises(HarnessError):
            self.studio.finalize_draft(draft['id'])
        self.assertFalse(self.adapter.sent)
        self.approve(draft)
        done = self.studio.finalize_draft(draft['id'])['draft']
        self.assertEqual(done['status'], 'sent')
        self.assertEqual(self.adapter.sent[0][2], 'Precisely approved reply.')
        self.assertEqual(self.adapter.sent[0][1]['sender'], 'colleague@example.test')
        self.assertEqual(self.adapter.sent[0][3], draft['id'])
        self.studio = self.reopen()
        self.studio.finalize_draft(draft['id'])
        self.assertEqual(len(self.adapter.sent), 1)

    def test_browser_ambiguous_send_cannot_retry_after_restart(self):
        for outcome in ({'status': 'unknown'}, HarnessError('Transport disconnected'), {'status': 'unexpected'}):
            with self.subTest(outcome=outcome):
                self.arrive(str(len(self.adapter.messages)))
                self.service()._poll_account(self.account['id'])
                draft = self.studio.snapshot()['drafts'][-1]
                self.approve(draft)
                self.adapter.send_result = outcome
                before = len(self.adapter.sent)
                with self.assertRaisesRegex(HarnessError, 'outcome is unknown'):
                    self.studio.finalize_draft(draft['id'])
                self.studio = self.reopen()
                with self.assertRaises(HarnessError):
                    self.studio.finalize_draft(draft['id'])
                self.assertEqual(len(self.adapter.sent), before + 1)
                current = self.studio._get('draft', draft['id'])
                self.assertEqual(current['status'], 'delivery_unknown')

    def test_browser_definitive_preclick_failure_can_retry_same_submission(self):
        draft = self.approve(self.draft())
        self.adapter.send_result = {'status': 'not_sent', 'error': 'Recipient could not be verified.'}
        with self.assertRaisesRegex(HarnessError, 'Recipient'):
            self.studio.finalize_draft(draft['id'])
        self.assertEqual(self.studio._get('draft', draft['id'])['status'], 'approved')
        self.studio = self.reopen()
        self.adapter.send_result = {'status': 'sent'}
        self.assertEqual(self.studio.finalize_draft(draft['id'])['draft']['status'], 'sent')
        self.assertEqual(self.adapter.sent[0][3], self.adapter.sent[1][3])

    def test_browser_unknown_preserves_safe_worker_failure_stage(self):
        draft = self.approve(self.draft())
        reason = 'The browser stopped while confirming the sent reply. Check Sent mail; Nexus will not resend this approval automatically.'
        self.adapter.send_result = {'status': 'unknown', 'error': reason}
        with self.assertRaisesRegex(HarnessError, 'confirming the sent reply'):
            self.studio.finalize_draft(draft['id'])
        current = self.studio._get('draft', draft['id'])
        self.assertEqual(current['status'], 'delivery_unknown')
        self.assertEqual(current['error'], reason)

    def test_definitely_unsent_browser_reply_can_reapprove_another_version(self):
        draft = self.approve(self.draft())
        self.adapter.send_result = {'status': 'not_sent', 'error': 'Reply not ready'}
        with self.assertRaisesRegex(HarnessError, 'Reply not ready'):
            self.studio.finalize_draft(draft['id'])
        failed = self.studio._get('draft', draft['id'])
        payload = dict(account_id=self.account['id'], draft_id=draft['id'], revision=failed['revision'],
                       text=failed['original'], learn=False, approval_contract='browser-send/v1')
        approved = self.studio.dispatch('approve_draft', payload)['draft']
        self.assertEqual(approved['edited'], failed['original'])
        self.assertFalse(approved['error'])
        with self.assertRaises(HarnessError):
            self.studio.dispatch('approve_draft', payload)
        self.adapter.send_result = {'status': 'sent'}
        self.assertEqual(self.studio.finalize_draft(draft['id'])['draft']['status'], 'sent')
        self.assertEqual(self.adapter.sent[-1][2], failed['original'])
        self.assertEqual(self.adapter.sent[0][3], self.adapter.sent[-1][3])

    def test_unknown_delivery_cannot_be_edited_or_reapproved_even_with_error(self):
        draft = self.approve(self.draft())
        self.adapter.send_result = {'status': 'unknown'}
        with self.assertRaises(HarnessError):
            self.studio.finalize_draft(draft['id'])
        for action in ('save_draft', 'approve_draft'):
            with self.subTest(action=action), self.assertRaises(HarnessError):
                self.studio.dispatch(action, dict(account_id=self.account['id'], draft_id=draft['id'],
                    revision=draft['revision'], text='Different reply', approval_contract='browser-send/v1'))
        self.assertEqual(len(self.adapter.sent), 1)

    def test_preflight_recovers_failed_approval_without_sending_and_clears_old_jobs(self):
        draft = self.approve(self.draft())
        self.adapter.send_result = {'status': 'not_sent', 'error': 'Old Reply control failure'}
        with self.assertRaises(HarnessError):
            self.studio.finalize_draft(draft['id'])
        service = self.service()
        service._jobs['finalize:' + draft['id']] = {'state': 'failed', 'error': 'Old Reply control failure'}
        prepared = service.dispatch('prepare_draft', dict(account_id=self.account['id'], draft_id=draft['id'],
            revision=draft['revision'], intent='send', text='Visible reply'))['draft']
        self.assertEqual(len(self.adapter.sent), 1)
        self.assertEqual(self.adapter.prepared[2], 'Visible reply')
        self.assertEqual(prepared['status'], 'review')
        self.assertNotIn('approved_at', prepared)
        self.assertFalse(prepared['error'])
        self.assertNotIn('finalize:' + draft['id'], service._jobs)

    def test_revision_preflight_and_direct_revision_reopen_definitely_unsent_approval(self):
        for preflight in (False, True):
            with self.subTest(preflight=preflight):
                draft = self.approve(self.draft())
                self.adapter.send_result = {'status': 'not_sent', 'error': 'Reply unavailable'}
                with self.assertRaises(HarnessError):
                    self.studio.finalize_draft(draft['id'])
                payload = dict(account_id=self.account['id'], draft_id=draft['id'], revision=draft['revision'],
                               intent='revise', text='Keep this text', instruction='Make it concise')
                if preflight:
                    self.studio.prepare_draft(payload)
                result = self.studio.revise_draft(payload)['draft']
                self.assertEqual(result['status'], 'review')
                self.assertNotIn('approved_at', result)
                self.assertFalse(result['error'])
                self.assertEqual(next(context for _, context in reversed(self.calls) if 'current_reply' in context)['current_reply'], 'Keep this text')

    def test_browser_identity_changes_and_disconnect_block_approved_send(self):
        draft = self.approve(self.draft())
        for field, changed in [('email', 'someone@example.test'), ('state', 'unsupported'),
                               ('provider', 'browser_gmail'), ('config_fingerprint', 'new')]:
            old = self.adapter.connection[field]
            self.adapter.connection[field] = changed
            with self.assertRaises(HarnessError):
                self.studio.finalize_draft(draft['id'])
            self.adapter.connection[field] = old
        self.studio.disconnect_local(self.account['id'])
        with self.assertRaises(HarnessError):
            self.studio.finalize_draft(draft['id'])
        self.assertFalse(self.adapter.sent)

    def test_browser_expired_session_reaches_recovering_send_worker(self):
        draft = self.approve(self.draft())
        self.adapter.connection['state'] = 'sign_in_required'
        self.adapter.send_result = {'status': 'not_sent', 'error': 'Sign in required after recovery'}
        with self.assertRaisesRegex(HarnessError, 'after recovery'):
            self.studio.finalize_draft(draft['id'])
        self.assertEqual(len(self.adapter.sent), 1)
        self.assertEqual(self.studio._get('draft', draft['id'])['status'], 'approved')
        self.adapter.send_result = {'status': 'sent'}
        self.assertEqual(self.studio.finalize_draft(draft['id'])['draft']['status'], 'sent')
        self.assertEqual(self.adapter.sent[0][3], self.adapter.sent[1][3])

    def test_browser_reference_upgrade_preserves_original_and_draft(self):
        draft = self.draft()
        message = self.studio._get('message', draft['message_id'])
        reference = message.pop('browser_reference')
        self.studio._put('message', message)
        self.approve(draft)
        with self.assertRaisesRegex(HarnessError, 'refresh this message'):
            self.studio.finalize_draft(draft['id'])
        changed = {**self.adapter.messages[0], 'body': 'A changed original'}
        self.studio._ingest(self.account, changed, message['source_id'])
        self.assertNotIn('browser_reference', self.studio._get('message', message['id']))
        self.studio._ingest(self.account, self.adapter.messages[0], message['source_id'])
        upgraded = self.studio._get('message', message['id'])
        self.assertEqual(upgraded['browser_reference'], reference)
        self.assertEqual(upgraded['body'], message['body'])
        self.assertEqual(len(self.studio.snapshot()['drafts']), 1)
        self.assertEqual(self.studio.finalize_draft(draft['id'])['draft']['status'], 'sent')

    def test_changed_identity_and_fingerprint_stop_sync_without_import(self):
        self.arrive()
        for field, value in [('email', 'other@example.test'), ('config_fingerprint', 'changed'), ('state', 'sign_in_required')]:
            old = self.adapter.connection[field]
            self.adapter.connection[field] = value
            with self.assertRaises(HarnessError):
                self.service()._poll_account(self.account['id'])
            self.assertFalse(self.studio.snapshot()['messages'])
            self.adapter.connection[field] = old

    def test_disconnect_stops_mail_reads_and_account_settings_persist(self):
        self.studio.disconnect_local(self.account['id'])
        with self.assertRaises(HarnessError):
            self.studio.dispatch('sync', {'account_id': self.account['id']})
        self.assertFalse(self.reopen().snapshot()['accounts'][0]['poll_enabled'])

    def test_short_poll_interval_persists_and_invalid_fractions_are_rejected(self):
        payload = {'account_id': self.account['id'], 'poll_seconds': 3, 'poll_enabled': True}
        self.studio.dispatch('account_save', payload)
        self.assertEqual(self.reopen().snapshot()['accounts'][0]['poll_seconds'], 3)
        for seconds in (0, 1.5, '3.5', True, 86401):
            with self.subTest(seconds=seconds), self.assertRaises(HarnessError):
                self.studio.dispatch('account_save', {**payload, 'poll_seconds': seconds})

    def test_poll_scheduler_reschedules_short_intervals_without_overlapping_slow_scans(self):
        service = self.service()
        account = {**self.account, 'poll_enabled': True, 'poll_seconds': 60}
        calls = []
        def launch(key, work):
            calls.append(key)
            service._jobs[key] = {'state': 'running'}
        service._background = launch
        with patch('our_harness.email_service.time.monotonic', return_value=100):
            service._schedule_polls([account])
        self.assertEqual(len(calls), 1)
        account['poll_seconds'] = 3
        with patch('our_harness.email_service.time.monotonic', return_value=105):
            service._schedule_polls([account])
        self.assertEqual(len(calls), 1)
        self.assertEqual(service._poll_due[account['id']], 0)
        service._jobs[calls[-1]]['state'] = 'completed'
        with patch('our_harness.email_service.time.monotonic', return_value=118):
            service._schedule_polls([account])
        self.assertEqual(len(calls), 2)
        self.assertEqual(service._poll_due[account['id']], 121)
        service._jobs[calls[-1]]['state'] = 'completed'
        with patch('our_harness.email_service.time.monotonic', return_value=120):
            service._schedule_polls([account])
        self.assertEqual(len(calls), 2)
        with patch('our_harness.email_service.time.monotonic', return_value=121):
            service._schedule_polls([account])
        self.assertEqual(len(calls), 3)

    def test_scan_duration_recorded_even_on_failure_and_setting_save_resets_due(self):
        service = self.service()
        service._poll_due[self.account['id']] = 999999
        service.dispatch('account_save', {'account_id': self.account['id'], 'poll_seconds': 1})
        self.assertEqual(service._poll_due[self.account['id']], 0)
        with patch.object(service, '_scan_account', side_effect=HarnessError('Mailbox slow')):
            with self.assertRaises(HarnessError):
                service._poll_account(self.account['id'])
        timing = service._scan_timing[self.account['id']]
        self.assertTrue(timing['last_started_at'])
        self.assertTrue(timing['last_finished_at'])
        self.assertGreaterEqual(timing['last_duration_seconds'], 0)

    def test_fast_poll_tick_keeps_maintenance_at_five_seconds_and_reads_accounts_only(self):
        service = self.service()
        clock = [0.0]
        def wait(seconds):
            self.assertEqual(seconds, .5)
            clock[0] += seconds
            return clock[0] > 6
        service._stop = Mock()
        service._stop.wait.side_effect = wait
        service._background = Mock()
        with patch.object(self.studio, 'snapshot', side_effect=AssertionError('Hot tick must not read full history')):
            with patch.object(self.studio, 'polling_accounts', return_value=[]) as accounts:
                with patch('our_harness.email_service.time.monotonic', side_effect=lambda: clock[0]):
                    service._poll()
        self.assertEqual(accounts.call_count, 12)
        self.assertEqual(service._background.call_count, 2)
        self.assertTrue(all(c.args[0] == 'maintenance:email' for c in service._background.call_args_list))

    def test_overlapping_poll_recovery_and_stale_snapshot_launch_one_workflow(self):
        draft = self.draft()
        draft.update(status='queued', execution_id='')
        self.studio._put('draft', draft)
        entered, release = threading.Event(), threading.Event()
        def start(identity):
            entered.set()
            if not release.wait(3):
                raise AssertionError('Test release timed out')
            return 'single-execution'
        engine = Mock()
        engine.start_draft.side_effect = start
        service = EmailService(SimpleNamespace(config=self.config), studio=self.studio, engine=engine)
        service._start_draft(draft)
        self.assertTrue(entered.wait(2))
        service._start_draft(draft)
        service._poll_maintenance()
        release.set()
        import time
        deadline = time.monotonic() + 3
        while service._jobs['draft:' + draft['id']]['state'] == 'running' and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(service._jobs['draft:' + draft['id']]['state'], 'completed')
        # A late scheduler snapshot arrives after the launch job has finished.
        service._background = lambda key, work: work()
        service._start_draft(draft)
        engine.start_draft.assert_called_once_with(draft['id'])
        self.assertEqual(self.studio._get('draft', draft['id'])['execution_id'], 'single-execution')

    def test_revision_cannot_overwrite_a_newer_edit_or_learn_without_approval(self):
        draft = self.draft()
        entered, release = threading.Event(), threading.Event()
        errors = []
        def slow(*args):
            entered.set()
            release.wait(5)
            return 'Late AI reply'
        self.studio.provider_call = slow
        def revise():
            try:
                self.studio.revise_draft(dict(account_id=self.account['id'], draft_id=draft['id'],
                    revision=draft['revision'], text='Submitted edit', instruction='Shorter'))
            except HarnessError as error:
                errors.append(str(error))
        thread = threading.Thread(target=revise)
        thread.start()
        self.assertTrue(entered.wait(2))
        current = self.studio.snapshot()['drafts'][0]
        self.studio.dispatch('save_draft', dict(account_id=self.account['id'], draft_id=draft['id'],
            revision=current['revision'], text='Newer manual edit'))
        release.set(); thread.join(5)
        self.assertTrue(errors)
        self.assertEqual(self.studio.snapshot()['drafts'][0]['edited'], 'Newer manual edit')
        self.assertFalse(self.studio.snapshot()['memories'])

    def test_failed_revision_keeps_submitted_edit_and_stale_request_is_rejected(self):
        draft = self.draft()
        self.studio.provider_call = Mock(side_effect=HarnessError('Provider unavailable'))
        payload = dict(account_id=self.account['id'], draft_id=draft['id'], revision=draft['revision'],
                       text='Keep this edit', instruction='Shorter')
        with self.assertRaises(HarnessError):
            self.studio.revise_draft(payload)
        self.assertEqual(self.studio.snapshot()['drafts'][0]['edited'], 'Keep this edit')
        with self.assertRaises(HarnessError):
            self.studio.revise_draft(payload)
        self.assertEqual(self.studio.provider_call.call_count, 1)

    def test_new_arrival_job_passes_through_real_service_engine_dispatch(self):
        self.arrive()
        engine = Mock()
        engine.start_draft.return_value = 'execution-1'
        service = EmailService(SimpleNamespace(config=self.config), studio=self.studio, engine=engine)
        service._background = lambda key, work: work()
        service._poll_account(self.account['id'])
        draft = self.studio.snapshot()['drafts'][0]
        engine.ensure_started.assert_called_once()
        engine.start_draft.assert_called_once_with(draft['id'])
        self.assertEqual(draft['execution_id'], 'execution-1')
        service._poll_account(self.account['id'])
        engine.start_draft.assert_called_once()

    def test_slow_scan_does_not_block_editing_and_cannot_undo_disconnect(self):
        draft = self.draft()
        entered, release = threading.Event(), threading.Event()
        errors = []
        def slow(identity, cursor):
            entered.set(); release.wait(5)
            return {'messages': [], 'cursor': 'later', 'warnings': []}
        self.adapter.sync = slow
        def scan():
            try:
                self.studio.dispatch('sync', {'account_id': self.account['id']})
            except HarnessError as error:
                errors.append(str(error))
        thread = threading.Thread(target=scan)
        thread.start()
        self.assertTrue(entered.wait(2))
        saved = self.studio.dispatch('save_draft', dict(account_id=self.account['id'], draft_id=draft['id'],
            revision=draft['revision'], text='Saved while browser checks mail'))
        self.assertEqual(saved['draft']['edited'], 'Saved while browser checks mail')
        self.studio.disconnect_local(self.account['id'])
        release.set(); thread.join(5)
        self.assertTrue(errors)
        self.assertEqual(self.studio.snapshot()['accounts'][0]['connection_state'], 'disconnected')
        self.assertNotEqual(self.studio.snapshot()['accounts'][0].get('cursor'), 'later')


class LocalSyncReportingTests(unittest.TestCase):
    """A local mailbox must report its backlog, its skipped mail and its dates."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mail-reporting-')
        self.addCleanup(self.temp.cleanup)
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), Path(self.temp.name), [], {})
        self.adapter = LocalAdapter('browser_outlook')
        self.studio = EmailStudio(self.config, provider_call=lambda *a, **k: 'Draft.', local_mail=self.adapter)
        self.account = self.studio.connect_local('browser_outlook', 'local-id', {'provider_route': 'fixture-route'})['account']

    def service(self):
        service = EmailService(SimpleNamespace(config=self.config), studio=self.studio, engine=Mock())
        service._start_draft = lambda draft: None
        return service

    def reference(self, source):
        return {'contract': 'browser-reply/v1', 'provider': 'browser_outlook',
                'source_hash': source, 'row_id': source, 'row_attr': 'data-convid'}

    def message(self, source, **extra):
        return dict(source_id=source, sender='colleague@example.test', subject=source,
                    body='Can you help?', browser_reference=self.reference(source), **extra)

    def test_a_reported_backlog_drains_within_one_check_instead_of_one_batch_per_interval(self):
        batches = [dict(messages=[self.message('one')], cursor='c1', warnings=[], has_more=True),
                   dict(messages=[self.message('two')], cursor='c2', warnings=[], has_more=True),
                   dict(messages=[self.message('three')], cursor='c3', warnings=[], has_more=False)]
        calls = []

        def sync(identity, cursor):
            calls.append(cursor)
            return batches[min(len(calls) - 1, len(batches) - 1)]

        self.adapter.sync = sync
        self.service()._poll_account(self.account['id'])
        self.assertEqual(len(calls), 3, 'the poller must keep draining while the mailbox reports more')
        stored = {m['source_id'] for m in self.studio.snapshot()['messages']}
        self.assertEqual(stored, {'one', 'two', 'three'})

    def test_a_backlog_that_is_not_reported_still_stops_after_one_batch(self):
        calls = []

        def sync(identity, cursor):
            calls.append(cursor)
            return dict(messages=[], cursor='only', warnings=[], has_more=False)

        self.adapter.sync = sync
        self.service()._poll_account(self.account['id'])
        self.assertEqual(len(calls), 1, 'a finished mailbox must not be polled in a loop')

    def test_large_backlog_resumes_before_slow_draft_preparation(self):
        calls = []

        def sync(identity, cursor):
            calls.append(cursor)
            number = len(calls)
            return dict(messages=[self.message(str(number))], cursor=str(number),
                        warnings=[], has_more=number < 10)

        self.adapter.sync = sync
        service = self.service()
        with patch.object(service, '_start_draft') as start:
            service._poll_account(self.account['id'])
            self.assertEqual(len(self.studio.snapshot()['messages']), 8)
            self.assertEqual(self.studio.snapshot()['drafts'], [])
            start.assert_not_called()
            service._poll_account(self.account['id'])
            self.assertEqual(calls[-2:], ['8', '9'])
            self.assertEqual(len(self.studio.snapshot()['messages']), 10)
            self.assertEqual(start.call_count, 10)

    def test_mail_a_scan_could_not_read_is_visible_instead_of_silently_dropped(self):
        failure = {'source_id': 'row-9', 'error': 'The selected message did not finish loading.'}
        self.adapter.sync = lambda identity, cursor: dict(messages=[], cursor='c1', warnings=[], failed_messages=[failure])
        self.studio.dispatch('sync', {'account_id': self.account['id']})
        reported = self.studio.snapshot()['failed_imports']
        self.assertEqual([f['source_id'] for f in reported], ['row-9'])
        self.assertEqual(reported[0]['error'], failure['error'])
        self.assertEqual(reported[0]['account_id'], self.account['id'])

    def test_a_previously_unreadable_conversation_stops_being_reported_once_it_imports(self):
        # The browser scan keys a failure by its inbox row, which never equals the
        # content hash an imported message carries, so the scan names what it resolved.
        row_key = 'a1b2c3d4' * 8
        failure = {'source_id': row_key, 'error': 'The selected message did not finish loading.'}
        self.adapter.sync = lambda identity, cursor: dict(messages=[], cursor='c1', warnings=[], failed_messages=[failure])
        self.studio.dispatch('sync', {'account_id': self.account['id']})
        self.assertEqual([f['source_id'] for f in self.studio.snapshot()['failed_imports']], [row_key])
        self.adapter.sync = lambda identity, cursor: dict(
            messages=[self.message('unrelated-content-hash')], cursor='c2', warnings=[],
            failed_messages=[], resolved_failures=[row_key])
        self.studio.dispatch('sync', {'account_id': self.account['id']})
        self.assertEqual(self.studio.snapshot()['failed_imports'], [],
                         'a conversation that finally imported must stop being reported as skipped')

    def test_a_conversation_that_keeps_failing_stays_reported(self):
        row_key = 'f0f0f0f0' * 8
        failure = {'source_id': row_key, 'error': 'The selected message did not finish loading.'}
        self.adapter.sync = lambda identity, cursor: dict(messages=[], cursor='c1', warnings=[], failed_messages=[failure])
        self.studio.dispatch('sync', {'account_id': self.account['id']})
        self.adapter.sync = lambda identity, cursor: dict(
            messages=[self.message('another-message')], cursor='c2', warnings=[],
            failed_messages=[failure], resolved_failures=[])
        self.studio.dispatch('sync', {'account_id': self.account['id']})
        self.assertEqual([f['source_id'] for f in self.studio.snapshot()['failed_imports']], [row_key],
                         'importing other mail must not hide a conversation that still cannot be read')

    def test_a_browser_message_keeps_the_date_its_mailbox_showed(self):
        received = '2026-09-17T08:30:00+00:00'
        self.adapter.sync = lambda identity, cursor: dict(
            messages=[self.message('dated', received_at=received)], cursor='c1', warnings=[])
        self.studio.dispatch('sync', {'account_id': self.account['id']})
        stored = self.studio.snapshot()['messages'][-1]
        self.assertEqual(stored['received_at'], received,
                         'without a real date the newest mail cannot sort first')

    def test_a_message_with_no_usable_date_is_still_imported(self):
        self.adapter.sync = lambda identity, cursor: dict(
            messages=[self.message('undated')], cursor='c1', warnings=[])
        self.studio.dispatch('sync', {'account_id': self.account['id']})
        stored = self.studio.snapshot()['messages'][-1]
        self.assertEqual(stored['source_id'], 'undated')
        self.assertEqual(stored['received_at'], '')
