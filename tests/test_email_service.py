"""Verify the real HTTP/callback boundary separately from mail and JVM adapters."""
import json
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
from datetime import datetime
from unittest import mock

from our_harness.email_service import EmailService
from our_harness.models import HarnessError
from tests.test_team_server import PanelTestCase


class EmailHTTPTests(PanelTestCase):
    def test_automatic_learning_retry_uses_async_http_without_revision_or_delivery(self):
        service = self.panel.email
        payload = {'account_id': 'synthetic-mailbox', 'draft_id': 'synthetic-draft',
                   'revision': 7, 'request_index': 1, 'request_fingerprint': 'saved-request'}
        with mock.patch.object(service, '_background') as background, \
                mock.patch.object(service.studio, 'retry_automatic_learning', return_value={}) as retry, \
                mock.patch.object(service.studio, 'revise_draft') as revise:
            status, result = self.ask('/api/email/retry_automatic_learning', payload)
            self.assertEqual(status, 200)
            self.assertTrue(result['started'])
            key, callback = background.call_args.args
            self.assertEqual(key, 'automatic-learning:synthetic-draft')
            callback()
            retry.assert_called_once_with(payload)
            revise.assert_not_called()

    def test_browser_failures_are_actionable_without_private_selectors_or_terminal_codes(self):
        service = self.panel.email
        private_trace = '\x1b[2mlocator.click: Timeout 8000ms exceeded. Call log: [data-convid="private-synthetic-id"]\x1b[22m'
        service._background('sync:synthetic', lambda: (_ for _ in ()).throw(HarnessError(private_trace)))
        deadline = time.monotonic() + 2
        while service._jobs['sync:synthetic']['state'] == 'running' and time.monotonic() < deadline:
            time.sleep(.01)
        failure = service._jobs['sync:synthetic']
        self.assertEqual(failure['state'], 'failed')
        self.assertIn('Check inbox now', failure['error'])
        self.assertNotIn('private-synthetic-id', failure['error'])
        self.assertNotIn('\x1b', failure['error'])
        self.assertNotIn('locator.', failure['error'])
        self.assertNotIn('private-synthetic-id', service._public_error(HarnessError(private_trace), 'finalize:synthetic'))
        self.assertEqual(service._public_error(HarnessError('\x1b[2mSign in again.\x1b[22m'), 'sync:synthetic'), 'Sign in again.')

    def test_browser_mode_roundtrip_uses_real_http_and_persistent_settings(self):
        from our_harness.email_local import LocalMail
        local = self.panel.email.studio.local_mail
        adapter = local.adapter('browser_outlook')
        with mock.patch.object(adapter, '_request', return_value={
                'state': 'connected', 'email': 'owner@example.test', 'actual_browser_mode': 'headed'}) as request:
            code, opened = self.ask('/api/email/local_open', {'provider': 'browser_outlook', 'browser_mode': 'headless'})
        self.assertEqual(code, 200, opened)
        connection = opened['connection']
        self.assertEqual(connection['browser_mode'], 'headless')
        self.assertEqual(request.call_args.kwargs['connection']['browser_mode'], 'headless')
        code, saved = self.ask('/api/email/local_mode', {'kind': 'browser_outlook',
            'connection_id': connection['id'], 'browser_mode': 'headed'})
        self.assertEqual(code, 200, saved)
        self.assertEqual(saved['connection']['config_fingerprint'], connection['config_fingerprint'])
        reopened = LocalMail(self.panel.email.studio.root).snapshot()[0]
        self.assertEqual(reopened['browser_mode'], 'headed')
        self.assertEqual(reopened['id'], connection['id'])
        for kind, mode in [('classic_outlook', 'headed'), ('browser_gmail', 'headed'), ('browser_outlook', 'invalid')]:
            self.assertEqual(self.ask('/api/email/local_mode', {'kind': kind,
                'connection_id': connection['id'], 'browser_mode': mode})[0], 400)

    def test_real_oauth_callback_joins_http_workspace_without_exposing_tokens(self):
        from tests.test_email_studio import Secrets
        studio = self.panel.email.studio
        studio.secrets = Secrets()
        studio.provider_call = lambda *args: 'Synthetic reply.'
        connector = studio.connectors
        connector.browser_open = lambda url: True
        def transport(method, url, headers, body):
            if url.endswith('/token'):
                return {'access_token': 'synthetic-access-secret', 'refresh_token': 'synthetic-refresh-secret', 'expires_in': 3600}
            if '/me?' in url:
                return {'mail': 'owner@example.test', 'displayName': 'Synthetic owner'}
            raise AssertionError('Unexpected provider call: ' + url)
        connector.transport = transport
        status, configured = self.ask('/api/email/oauth_configure', {'provider': 'outlook', 'client_id': 'synthetic-public-client'})
        self.assertEqual(status, 200, configured)
        status, started = self.ask('/api/email/oauth_start', {'provider': 'outlook', 'provider_route': 'synthetic-route', 'poll_enabled': False})
        self.assertEqual(status, 200, started)
        parameters = urllib.parse.parse_qs(urllib.parse.urlsplit(started['authorization_url']).query)
        callback = parameters['redirect_uri'][0] + '?' + urllib.parse.urlencode({'state': parameters['state'][0], 'code': 'synthetic-code'})
        with urllib.request.urlopen(callback, timeout=5) as response:
            self.assertEqual(response.status, 200)
        with mock.patch.object(self.panel.email, '_engine') as engine:
            engine.status.return_value = {'state': 'stopped'}
            status, snapshot = self.ask('/api/email')
        self.assertEqual(status, 200, snapshot)
        self.assertEqual(snapshot['accounts'][0]['email'], 'owner@example.test')
        self.assertEqual(snapshot['oauth']['pending'][0]['state'], 'connected')
        self.assertNotIn('synthetic-access-secret', json.dumps(snapshot))
        self.assertNotIn('synthetic-refresh-secret', json.dumps(snapshot))

    def raw(self, path, body=None, headers=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            response = urllib.request.urlopen(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def test_dedicated_callback_capability_is_required_and_not_ui_token(self):
        service = self.panel.email
        service._studio = mock.Mock()
        service._studio.process_draft.return_value = {"draft": {"status": "review"}}
        for headers in ({}, {"X-Harness-Token": self.panel.token},
                        {"X-Nexus-Email-Token": "incorrect"}):
            status, _ = self.raw("/api/email-worker/generate", {"draft_id": "d-1"}, headers)
            self.assertEqual(status, 400)
        service._studio.process_draft.assert_not_called()
        status, value = self.raw("/api/email-worker/generate", {"draft_id": "d-1"},
                                {"X-Nexus-Email-Token": service.callback_token})
        self.assertEqual(status, 200, value)
        self.assertEqual(value, {"draft_id": "d-1", "status": "review"})
        service._studio.process_draft.assert_called_once_with("d-1")

    def test_cross_origin_callback_is_rejected_even_with_capability(self):
        self.panel.email._studio = mock.Mock()
        status, _ = self.raw("/api/email-worker/finalize", {"draft_id": "d-1"}, {
            "X-Nexus-Email-Token": self.panel.email.callback_token,
            "Origin": "https://external.example",
        })
        self.assertEqual(status, 400)
        self.panel.email._studio.finalize_draft.assert_not_called()

    def test_ui_read_and_write_require_ui_token_and_validate_action(self):
        with mock.patch.object(self.panel.email, "snapshot", return_value={"accounts": []}) as snapshot:
            self.assertEqual(self.raw("/api/email")[0], 400)
            self.assertEqual(self.ask("/api/email"), (200, {"accounts": []}))
            snapshot.assert_called_once()
        with mock.patch.object(self.panel.email, "dispatch", return_value={"saved": True}) as dispatch:
            self.assertEqual(self.raw("/api/email/account_save", {})[0], 400)
            self.assertEqual(self.ask("/api/email/account_save", {"name": "A"})[0], 200)
            dispatch.assert_called_once_with("account_save", {"name": "A"})

    def test_background_start_is_deduplicated_and_errors_visible(self):
        service = self.panel.email
        entered, release = threading.Event(), threading.Event()
        calls = []
        def work():
            calls.append(1)
            entered.set()
            release.wait(2)
            raise HarnessError("Private Java was not found")
        service._background("engine", work)
        self.assertTrue(entered.wait(2))
        running = dict(service._jobs['engine'])
        self.assertEqual(running['started_at'], running['updated_at'])
        self.assertEqual(running['finished_at'], '')
        self.assertIsNotNone(datetime.fromisoformat(running['started_at']).tzinfo)
        service._background("engine", work)
        release.set()
        end = time.monotonic() + 2
        while service._jobs["engine"]["state"] == "running" and time.monotonic() < end:
            time.sleep(.01)
        self.assertEqual(calls, [1])
        self.assertEqual(service._jobs["engine"]["state"], "failed")
        self.assertIn("Private Java", service._jobs["engine"]["error"])
        finished = service._jobs['engine']
        self.assertEqual(finished['finished_at'], finished['updated_at'])
        self.assertGreaterEqual(datetime.fromisoformat(finished['finished_at']), datetime.fromisoformat(finished['started_at']))

    def test_actual_worker_activity_stays_running_after_workflow_dispatch_completes(self):
        service = self.panel.email
        service._studio = mock.Mock()
        entered, release = threading.Event(), threading.Event()
        results = []
        def generate(draft_id):
            entered.set()
            release.wait(3)
            return {'draft': {'status': 'review', 'edited': 'Private synthetic body'}}
        service._studio.process_draft.side_effect = generate
        service._jobs['draft:d-activity'] = {'id':'draft:d-activity', 'state':'completed', 'error':''}
        thread = threading.Thread(target=lambda: results.append(service.worker('generate', {'draft_id':'d-activity'})))
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            operation = dict(service._jobs['generate:d-activity'])
            self.assertEqual(operation['state'], 'running')
            self.assertEqual(operation['finished_at'], '')
            self.assertEqual(operation['updated_at'], operation['started_at'])
            self.assertNotIn('Private synthetic body', json.dumps(operation))
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(service._jobs['generate:d-activity']['state'], 'completed')
        self.assertTrue(service._jobs['generate:d-activity']['finished_at'])
        self.assertEqual(results, [{'draft_id':'d-activity', 'status':'review'}])
        service._studio.finalize_draft.side_effect = HarnessError('An existing reply composer is open.')
        with self.assertRaises(HarnessError):
            service.worker('finalize', {'draft_id':'d-activity'})
        blocked = service._jobs['finalize:d-activity']
        self.assertEqual(blocked['state'], 'failed')
        self.assertIn('reply composer', blocked['error'])
        self.assertTrue(blocked['finished_at'])

    def test_duplicate_callback_finishing_does_not_hide_another_active_callback(self):
        service = self.panel.email
        service._studio = mock.Mock()
        entered, release = threading.Event(), threading.Event()
        first = True
        def generate(draft_id):
            nonlocal first
            if first:
                first = False
                entered.set()
                release.wait(3)
            return {'draft': {'status': 'review'}}
        service._studio.process_draft.side_effect = generate
        thread = threading.Thread(target=lambda: service.worker('generate', {'draft_id':'d-duplicate'}))
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            service.worker('generate', {'draft_id':'d-duplicate'})
            self.assertEqual(service._jobs['generate:d-duplicate']['state'], 'running')
            self.assertEqual(service._jobs['generate:d-duplicate']['finished_at'], '')
        finally:
            release.set(); thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(service._jobs['generate:d-duplicate']['state'], 'completed')

    def test_resume_checks_account_and_approval_before_touching_engine(self):
        studio, engine = mock.Mock(), mock.Mock()
        studio.snapshot.return_value = {"drafts": [{
            "id": "d-1", "account_id": "a", "status": "review", "execution_id": "ex-1",
        }]}
        service = EmailService(self.panel, studio=studio, engine=engine)
        for account in ("a", "b"):
            with self.assertRaises(HarnessError):
                service.dispatch("resume_draft", {"draft_id": "d-1", "account_id": account})
        engine.resume.assert_not_called()

    def test_worker_validation_and_close(self):
        service = self.panel.email
        service._engine = mock.Mock()
        for draft_id in ("", None, {}, "x" * 129):
            with self.assertRaises(HarnessError):
                service.worker("generate", {"draft_id": draft_id})
        service.close()
        service._engine.close.assert_called_once()
        with self.assertRaises(HarnessError):
            service._background("after-close", lambda: None)

    def test_approved_failed_workflow_rebinds_before_resume_without_direct_delivery(self):
        studio, engine = mock.Mock(), mock.Mock()
        engine.execution_status.return_value = {"state": {"current": "FAILED"}}
        engine.start_draft.return_value = "replacement"
        service = EmailService(self.panel, studio=studio, engine=engine)
        service._resume({"id": "d", "account_id": "a", "status": "approved", "execution_id": "old"})
        end = time.monotonic() + 2
        while service._jobs["approve:d"]["state"] == "running" and time.monotonic() < end:
            time.sleep(.01)
        self.assertEqual(service._jobs["approve:d"]["state"], "completed")
        studio.dispatch.assert_called_once_with("rebind_execution", {
            "account_id": "a", "draft_id": "d", "previous_execution_id": "old", "execution_id": "replacement",
        })
        engine.resume.assert_called_once_with("replacement")
        studio.finalize_draft.assert_not_called()

    def test_internal_binding_actions_are_not_exposed_to_user_api(self):
        for action in ("bind_execution", "rebind_execution"):
            with self.assertRaises(HarnessError):
                self.panel.email.dispatch(action, {})

    def test_notification_feed_needs_the_session_token_and_reads_after_a_cursor(self):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/email/notifications")
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(request, timeout=10)
        service = self.panel.email
        account = {'id': 'synthetic-mailbox', 'name': 'Work'}
        service._announce_drafting(account, {'id': 'm1', 'sender': 'a@example.test', 'subject': 'One'}, {'id': 'd1'})
        service._announce_drafting(account, {'id': 'm2', 'sender': 'b@example.test', 'subject': 'Two'}, {'id': 'd2'})
        status, feed = self.ask('/api/email/notifications')
        self.assertEqual(status, 200, feed)
        self.assertEqual([item['subject'] for item in feed['items']], ['One', 'Two'])
        self.assertEqual(feed['seq'], 2)
        status, later = self.ask('/api/email/notifications?after=1')
        self.assertEqual([item['draft_id'] for item in later['items']], ['d2'])
        self.assertEqual(later['boot'], feed['boot'])
        status, garbled = self.ask('/api/email/notifications?after=not-a-number')
        self.assertEqual(len(garbled['items']), 2)
        # Polling the feed is read-only: it never opens a mail store.
        self.assertFalse((self.root / '.harness' / 'email-studio').exists())
