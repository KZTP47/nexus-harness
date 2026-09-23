import json
from pathlib import Path
import tempfile
import shutil
import threading
import unittest
from unittest.mock import patch
from our_harness.email_browser import EmailBrowser
from our_harness.email_local import LocalMail
from our_harness.models import HarnessError

class BrowserMailTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.browser=EmailBrowser(self.temp.name)
        self.addCleanup(self.browser.close)
    def request(self, command, **kwargs):
        return {'state':'connected','email':'person@example.test','name':'Person'}
    def test_packaged_asar_worker_and_missing_runtime_boundary(self):
        root = Path(self.temp.name)
        executable = root/'arbitrary-desktop.exe'
        executable.write_bytes(b'fixture')
        archive = root/'resources'/'app.asar'
        archive.parent.mkdir()
        archive.write_bytes(b'fixture archive')
        worker = archive/'email-browser-worker.js'
        with patch.dict('os.environ', {'NEXUS_EMAIL_BROWSER_NODE':str(executable),
                                     'NEXUS_EMAIL_BROWSER_WORKER':str(worker)}):
            self.assertEqual(self.browser._command(), [str(executable), str(worker)])
            archive.unlink()
            with self.assertRaises(HarnessError):
                self.browser._command()
    def test_binding_survives_restart(self):
        with patch.object(self.browser,'_request',side_effect=self.request):
            result=self.browser.open('gmail')
        other=EmailBrowser(self.temp.name)
        with patch.object(other,'_request') as request:
            self.assertEqual(other.connections()[0]['id'],result['id'])
            request.assert_not_called()
        with patch.object(other,'_request',side_effect=self.request):
            self.assertEqual(other.status(result['id'])['email'],'person@example.test')

    def test_connection_snapshot_does_not_wait_for_a_browser_scan(self):
        with patch.object(self.browser, '_request', side_effect=self.request):
            connection = self.browser.open('outlook')
        entered, release, listed = threading.Event(), threading.Event(), threading.Event()
        result = []

        def scan(command, **payload):
            if command == 'sync':
                entered.set()
                release.wait(5)
                return {'messages': [], 'cursor': ''}
            return self.request(command, **payload)

        def snapshot():
            result.extend(self.browser.connections())
            listed.set()

        with patch.object(self.browser, '_request', side_effect=scan):
            scanning = threading.Thread(target=self.browser.sync, args=(connection['id'],))
            scanning.start()
            reader = threading.Thread(target=snapshot)
            try:
                self.assertTrue(entered.wait(2))
                reader.start()
                self.assertTrue(listed.wait(1), 'inbox progress must remain readable during a long scan')
                self.assertEqual(result[0]['id'], connection['id'])
            finally:
                release.set()
                scanning.join(5)
                if reader.ident is not None:
                    reader.join(5)
    def test_cross_project_binding_rejected(self):
        with patch.object(self.browser,'_request',side_effect=self.request):
            result=self.browser.open('outlook')
        path=self.browser.root/result['id']/'connection.json'
        data=json.loads(path.read_text()); data['config_fingerprint']='old'
        path.write_text(json.dumps(data))
        with self.assertRaises(HarnessError): self.browser.status(result['id'])

    def test_parser_contract_migration_preserves_profile(self):
        with patch.object(self.browser,'_request',side_effect=self.request): result=self.browser.open('outlook')
        path=self.browser.root/result['id']/'connection.json'
        data=json.loads(path.read_text())
        previous=self.browser._fingerprint(result['id'],result['provider'],contract='dom-v1')
        data['config_fingerprint']=previous
        data.pop('parser_contract')
        path.write_text(json.dumps(data))
        profile=self.browser.root/result['id']/'profile'
        profile.mkdir(); (profile/'owned-session').write_text('retained')
        migrated=self.browser.connections()[0]
        self.assertNotEqual(migrated['config_fingerprint'],previous)
        self.assertEqual(migrated['parser_contract'],'dom-v2')
        self.assertEqual((profile/'owned-session').read_text(),'retained')
        self.assertEqual(json.loads(path.read_text())['config_fingerprint'],migrated['config_fingerprint'])
    def test_account_switch_rejected(self):
        with patch.object(self.browser,'_request',side_effect=self.request): result=self.browser.open('gmail')
        with patch.object(self.browser,'_request',return_value={'state':'connected','email':'different@example.test'}):
            with self.assertRaises(HarnessError): self.browser.status(result['id'])
    def test_invalid_id_and_provider(self):
        with self.assertRaises(HarnessError): self.browser.status('../elsewhere')
        with self.assertRaises(HarnessError): self.browser.open('imap')
    def test_login_loss_prevents_sync(self):
        with patch.object(self.browser,'_request',side_effect=self.request): result=self.browser.open('gmail')
        with patch.object(self.browser,'_request',return_value={'state':'sign_in_required','email':''}) as request:
            with self.assertRaises(HarnessError): self.browser.sync(result['id'])
            self.assertEqual(request.call_count,1)
    def test_provider_binding_rejected(self):
        with patch.object(self.browser,'_request',side_effect=self.request): result=self.browser.open('gmail')
        with self.assertRaises(HarnessError): self.browser.open('outlook',result['id'])

    def test_mode_persists_and_changes_without_identity_or_browser_launch(self):
        with patch.object(self.browser, '_request', side_effect=self.request) as request:
            result = self.browser.open('gmail', browser_mode='headless')
            self.assertEqual(request.call_args.args, ('open',))
            self.assertEqual(request.call_args.kwargs['connection']['browser_mode'], 'headless')
        fingerprint = result['config_fingerprint']
        other = EmailBrowser(self.temp.name)
        self.addCleanup(other.close)
        with patch.object(other, '_request') as request:
            self.assertEqual(other.connections()[0]['browser_mode'], 'headless')
            changed = other.configure_mode(result['id'], 'headed')
            self.assertEqual(changed['config_fingerprint'], fingerprint)
            self.assertEqual(changed['email'], result['email'])
            request.assert_not_called()
        self.assertEqual(EmailBrowser(self.temp.name).connections()[0]['browser_mode'], 'headed')

    def test_legacy_mode_migration_is_identity_preserving(self):
        with patch.object(self.browser, '_request', side_effect=self.request):
            result = self.browser.open('outlook')
        path = self.browser.root / result['id'] / 'connection.json'
        data = json.loads(path.read_text())
        data.pop('browser_mode'); data.pop('browser_mode_contract')
        path.write_text(json.dumps(data))
        with patch.object(self.browser, '_request') as request:
            migrated = self.browser.connections()[0]
            request.assert_not_called()
        self.assertEqual(migrated['browser_mode'], 'headed')
        self.assertEqual(migrated['browser_mode_contract'], 'browser-mode/v1')
        self.assertEqual(migrated['config_fingerprint'], result['config_fingerprint'])

    def test_invalid_mode_and_copied_root_fail_closed(self):
        with patch.object(self.browser, '_request', side_effect=self.request):
            result = self.browser.open('gmail')
        for mode in ('invisible', '', None, {}):
            with self.subTest(mode=mode), self.assertRaises(HarnessError):
                self.browser.configure_mode(result['id'], mode)
            with self.assertRaises(HarnessError):
                self.browser.open('outlook', browser_mode=mode)
        other = EmailBrowser(Path(self.temp.name) / 'unrelated-project')
        self.addCleanup(other.close)
        shutil.copytree(self.browser.root / result['id'], other.root / result['id'])
        with self.assertRaises(HarnessError):
            other.configure_mode(result['id'], 'headless')

    def incoming(self):
        return {'source_id':'synthetic-source', 'sender':'sender@example.test', 'subject':'Question',
                'body':'Original question', 'browser_reference':{'contract':'browser-reply/v1'},
                'private_local_path':'not-forwarded'}

    def test_prepare_uses_non_sending_command_and_only_mail_reference_fields(self):
        with patch.object(self.browser, '_request', side_effect=self.request):
            result = self.browser.open('gmail', browser_mode='headless')
        with patch.object(self.browser, '_request', return_value={'status': 'ready'}) as request:
            self.assertEqual(self.browser.prepare_reply(result['id'], self.incoming(), 'Preview', 'a' * 32), {'status': 'ready'})
            self.assertEqual(request.call_args.args[0], 'prepare')
            self.assertNotIn('private_local_path', request.call_args.kwargs['incoming'])

    def test_browser_send_forwards_exact_body_and_raw_outcome_once(self):
        with patch.object(self.browser, '_request', side_effect=self.request):
            result = self.browser.open('gmail', browser_mode='headless')
        outcome = {'status':'sent', 'evidence':'provider-toast', 'email':result['email'], 'actual_browser_mode':'headless'}
        with patch.object(self.browser, '_request', return_value=outcome) as request:
            self.assertEqual(self.browser.submit_reply(result['id'], self.incoming(), 'Exact approved\nreply', 'approval-1'), outcome)
            self.assertEqual([call.args[0] for call in request.call_args_list], ['send'])
            sent = request.call_args.kwargs
            self.assertEqual(sent['body'], 'Exact approved\nreply')
            self.assertEqual(sent['submission_id'], 'approval-1')
            self.assertEqual(sent['connection']['browser_mode'], 'headless')
            self.assertNotIn('private_local_path', sent['incoming'])

    def test_send_worker_owns_recovery_and_returns_definite_failure(self):
        with patch.object(self.browser, '_request', side_effect=self.request):
            result = self.browser.open('outlook')
        outcome = {'status': 'not_sent', 'error': 'Sign in to the saved mailbox'}
        with patch.object(self.browser, '_request', return_value=outcome) as request:
            self.assertEqual(self.browser.submit_reply(result['id'], self.incoming(), 'Approved', 'approval-2'), outcome)
            self.assertEqual([call.args[0] for call in request.call_args_list], ['send'])

    def test_send_input_validation_and_unrecognized_outcome(self):
        with patch.object(self.browser, '_request', side_effect=self.request):
            result = self.browser.open('gmail')
        for incoming, body, identity in (({}, 'reply', 'id'), (self.incoming(), '', 'id'),
                                         (self.incoming(), 'reply', 'bad\nidentifier')):
            with patch.object(self.browser, '_request') as request:
                self.assertEqual(self.browser.submit_reply(result['id'], incoming, body, identity)['status'], 'not_sent')
            request.assert_not_called()
        with patch.object(self.browser, '_request', return_value={'status':'clicked'}) as request:
            self.assertEqual(self.browser.submit_reply(result['id'], self.incoming(), 'reply', 'id')['status'], 'unknown')
            self.assertEqual(request.call_count, 1)

    def test_local_mode_public_metadata_avoids_private_paths_and_snapshot_launch(self):
        local = LocalMail(Path(self.temp.name) / 'local')
        self.addCleanup(local.close)
        adapter = local.adapter('browser_outlook')
        with patch.object(adapter, '_request', side_effect=lambda *args, **kwargs: {
                **self.request(*args, **kwargs), 'actual_browser_mode':'headed', 'profile':'private-runtime-path'}):
            opened = local.open('browser_outlook', browser_mode='headless')
        self.assertEqual(opened['browser_mode'], 'headless')
        self.assertEqual(opened['actual_browser_mode'], 'headed')
        self.assertNotIn('profile', opened)
        with patch.object(adapter, '_request') as request:
            changed = local.configure_mode('browser_outlook', opened['id'], 'headed')
            self.assertEqual(changed['browser_mode'], 'headed')
            self.assertEqual(changed['state'], 'connected')
            self.assertEqual(changed['actual_browser_mode'], 'headed')
            self.assertEqual(changed['email'], opened['email'])
            self.assertEqual(local.snapshot()[0]['id'], opened['id'])
            request.assert_not_called()
        with self.assertRaises(HarnessError): local.configure_mode('browser_gmail', opened['id'], 'headless')

    def test_transport_failure_after_send_is_not_reclassified_definitely_not_sent(self):
        with patch.object(self.browser, '_request', side_effect=self.request):
            opened = self.browser.open('gmail')
        with patch.object(self.browser, '_request', side_effect=HarnessError('worker stopped')) as request:
            with self.assertRaises(HarnessError):
                self.browser.submit_reply(opened['id'], self.incoming(), 'approved', 'submission-1')
            self.assertEqual(request.call_count, 1)

    def test_concurrent_mode_change_cannot_be_overwritten_by_stale_mail_operation(self):
        with patch.object(self.browser, '_request', side_effect=self.request):
            opened = self.browser.open('gmail')
        for operation in ('open', 'status', 'sync'):
            with self.subTest(operation=operation):
                self.browser.configure_mode(opened['id'], 'headed')
                entered, release, setting_started, setting_done = (threading.Event() for _ in range(4))
                errors = []
                def worker(command, **kwargs):
                    if not entered.is_set():
                        entered.set()
                        if not release.wait(3):
                            raise RuntimeError('Test did not release browser operation')
                    if command == 'sync':
                        return {'messages':[], 'cursor':'synthetic-cursor'}
                    return self.request(command)
                def run_operation():
                    try:
                        if operation == 'open': self.browser.open('gmail', opened['id'])
                        else: getattr(self.browser, operation)(opened['id'])
                    except Exception as exc: errors.append(exc)
                def change_setting():
                    setting_started.set()
                    try: self.browser.configure_mode(opened['id'], 'headless')
                    except Exception as exc: errors.append(exc)
                    finally: setting_done.set()
                with patch.object(self.browser, '_request', side_effect=worker):
                    running = threading.Thread(target=run_operation)
                    changing = threading.Thread(target=change_setting)
                    running.start()
                    self.assertTrue(entered.wait(2))
                    changing.start()
                    self.assertTrue(setting_started.wait(2))
                    self.assertFalse(setting_done.wait(.1))
                    release.set()
                    running.join(3); changing.join(3)
                self.assertFalse(running.is_alive())
                self.assertFalse(changing.is_alive())
                self.assertEqual(errors, [])
                persisted = self.browser.connections()[0]
                self.assertEqual(persisted['browser_mode'], 'headless')
                self.assertEqual(persisted['config_fingerprint'], opened['config_fingerprint'])
