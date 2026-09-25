"""Portable fake-HTTP contracts; these do not claim live provider delivery."""
import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from our_harness.email_engine import EmailEngineClient, EmailEngineError, validate_base_url


class Server:
    def __init__(self):
        self.calls = []
        self.pages = {'': {'messages': [{'id': 'one'}]}}
        self.details = {'one': {'id': 'one', 'from': {'address': 'sender@example.test'},
                               'replyTo': [{'address': 'reply@example.test'}], 'subject': 'Subject',
                               'date': '2020-01-01T00:00:00Z', 'messageId': '<original@example.test>',
                               'threadId': 'thread', 'headers': {'references': ['<prior@example.test>']},
                               'text': {'plain': 'Body'}}}
        self.version = '2.79.5'
        self.outbox = {'account': 'mailbox', 'queueId': 'queue', 'progress': {'status': 'queued'}}
        self.submissions = {}

    def __call__(self, method, url, headers, body):
        path = urlsplit(url).path
        query = parse_qs(urlsplit(url).query)
        payload = json.loads(body) if body else None
        self.calls.append((method, path, query, headers, payload))
        if path.endswith('/health'):
            return 200, {'success': True}
        if path.endswith('/v1/stats'):
            return 200, {'version': self.version}
        if path.endswith('/v1/accounts'):
            return 200, {'pages': 1, 'accounts': [{'account': 'mailbox', 'email': 'owner@example.test', 'smtp': {'auth': 'secret'}}]}
        if path.endswith('/v1/account/mailbox'):
            return 200, {'account': 'mailbox', 'state': 'connected', 'email': 'owner@example.test', 'oauth2': {'accessToken': 'private'}}
        if path.endswith('/messages'):
            page = self.pages[query.get('cursor', [''])[0]]
            return page if isinstance(page, tuple) else (200, page)
        if '/message/' in path:
            item = self.details[path.rsplit('/', 1)[-1]]
            return item if isinstance(item, tuple) else (200, item)
        if path.endswith('/submit'):
            self.submissions.setdefault(headers['Idempotency-Key'], {'queueId': 'queue', 'messageId': '<reply@example.test>'})
            return 200, self.submissions[headers['Idempotency-Key']]
        if path.endswith('/outbox/queue'):
            return (404, {}) if self.outbox is None else (200, self.outbox)
        if path.endswith('/authentication/form'):
            return 200, {'url': 'https://service.example.test/accounts/new?data=signed'}
        return 404, {}


class EmailEngineTests(unittest.TestCase):
    def setUp(self):
        self.server = Server()
        self.client = EmailEngineClient('https://service.example.test', 'private-token', transport=self.server)

    def test_urls_reject_credentials_remote_http_and_non_http(self):
        for value in ('http://remote.example.test', 'https://user:pass@example.test', 'https://example.test/?access_token=secret',
                      'https://example.test/#fragment', 'file:///tmp/test', 'https://example.test/../bad',
                      'https://example.test/%2e%2e/bad', 'https://example.test\\@evil.test', 'https://example.test:bad',
                      'https://example.test\n', 'http://127.0.0.1.evil.test'):
            with self.subTest(value=value), self.assertRaises(EmailEngineError):
                validate_base_url(value)
        for value in ('http://127.0.0.1:34871', 'http://[::1]:4382', 'http://localhost:3101', 'https://example.test/prefix'):
            self.assertEqual(validate_base_url(value), value)

    def test_account_public_projection_and_health(self):
        self.assertTrue(self.client.health()['supports_idempotency'])
        self.assertNotIn('smtp', self.client.accounts()[0])
        self.assertNotIn('oauth2', self.client.account('mailbox'))

    def test_sync_normalizes_reference_and_does_not_mark_read(self):
        result = self.client.sync('mailbox')
        message = result['messages'][0]
        self.assertEqual(message['reply_to'], 'reply@example.test')
        self.assertEqual(message['references'], '<prior@example.test>')
        self.assertEqual(message['internet_message_id'], '<original@example.test>')
        self.assertEqual(message['thread_id'], 'thread')
        self.assertTrue(message['is_historical'])
        self.assertEqual(self.server.calls[-1][2]['markAsSeen'], ['false'])

    def test_paging_restart_and_concurrent_head_arrival_reconciled(self):
        self.server.pages['']['nextPageCursor'] = 'second'
        self.server.pages['second'] = {'messages': [{'id': 'two'}]}
        self.server.details['two'] = dict(self.server.details['one'], id='two')
        first = self.client.sync('mailbox')
        self.assertTrue(first['has_more'])
        self.server.details['new'] = dict(self.server.details['one'], id='new', date=(datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat())
        self.server.pages['']['messages'].insert(0, {'id': 'new'})
        restarted = EmailEngineClient(self.client.base_url, 'different-token', transport=self.server)
        second = restarted.sync('mailbox', first['cursor'])
        self.assertFalse(second['has_more'])
        self.assertEqual([item['source_id'] for item in second['messages']], ['new', 'two'])
        self.assertFalse(second['messages'][0]['is_historical'])
        third = restarted.sync('mailbox', second['cursor'])
        self.assertEqual(third['messages'][0]['source_id'], 'new')
        self.assertFalse(third['messages'][0]['is_historical'])
        self.assertEqual(json.loads(first['cursor'])['baseline'], json.loads(third['cursor'])['baseline'])

    def test_failed_detail_does_not_skip_page_on_retry(self):
        first = self.client.sync('mailbox')
        self.server.pages['']['messages'].append({'id': 'two'})
        self.server.details['two'] = (503, {})
        with self.assertRaises(EmailEngineError):
            self.client.sync('mailbox', first['cursor'])
        self.server.details['two'] = dict(self.server.details['one'], id='two')
        retried = self.client.sync('mailbox', first['cursor'])
        self.assertEqual([row['source_id'] for row in retried['messages']], ['one', 'two'])

    def test_checkpoint_account_server_and_contract_isolation(self):
        cursor = self.client.sync('mailbox')['cursor']
        for client, account in ((self.client, 'another'), (EmailEngineClient('https://different.test', 'token', transport=self.server), 'mailbox')):
            with self.assertRaises(EmailEngineError):
                client.sync(account, cursor)
        state = json.loads(cursor)
        state['contract'] = 'old'
        with self.assertRaises(EmailEngineError):
            self.client.sync('mailbox', json.dumps(state))

    def test_expired_provider_cursor_restarts_without_changing_history_cutoff(self):
        self.server.pages['']['nextPageCursor'] = 'expired'
        first = self.client.sync('mailbox')
        self.server.pages['expired'] = (410, {})
        self.server.pages[''].pop('nextPageCursor')
        resumed = self.client.sync('mailbox', first['cursor'])
        self.assertTrue(resumed['warnings'])
        self.assertFalse(resumed['has_more'])
        self.assertEqual(json.loads(resumed['cursor'])['baseline'], json.loads(first['cursor'])['baseline'])

    def test_more_than_fifty_messages_reconcile_without_inbox_cap(self):
        for page in range(3):
            key = str(page) if page else ''
            rows = []
            for index in range(page * 50, min((page + 1) * 50, 123)):
                source_id = 'id' + str(index)
                self.server.details[source_id] = dict(self.server.details['one'], id=source_id)
                rows.append({'id': source_id})
            self.server.pages[key] = {'messages': rows, 'nextPageCursor': str(page + 1) if page < 2 else None}
        cursor, imported = '', set()
        for _ in range(3):
            result = self.client.sync('mailbox', cursor)
            imported.update(item['source_id'] for item in result['messages'])
            cursor = result['cursor']
        self.assertFalse(result['has_more'])
        self.assertEqual(len(imported), 123)

    def test_malformed_and_stalled_page_fail_closed(self):
        for result in ({'messages': None}, {'messages': [{'id': 'one'}], 'nextPageCursor': 34}):
            self.server.pages[''] = result
            with self.assertRaises(EmailEngineError):
                self.client.sync('mailbox')
        self.server.pages[''] = {'messages': [], 'nextPageCursor': 'same'}
        cursor = self.client.sync('mailbox')['cursor']
        self.server.pages['same'] = {'messages': [], 'nextPageCursor': 'same'}
        with self.assertRaises(EmailEngineError):
            self.client.sync('mailbox', cursor)

    def test_truncated_body_is_quarantined_and_html_is_plain_text(self):
        self.server.details['one']['text'] = {'plain': 'partial', 'hasMore': True}
        result = self.client.sync('mailbox')
        self.assertFalse(result['messages'])
        self.assertEqual(result['failed_messages'][0]['source_id'], 'one')
        self.assertTrue(result['warnings'])
        self.server.details['one']['text'] = {'html': '<p>Hello &amp; hi</p><script>evil()</script>'}
        self.assertEqual(self.client.sync('mailbox')['messages'][0]['body'], 'Hello & hi')

    def test_missing_and_oversized_messages_do_not_block_later_good_messages(self):
        self.server.pages['']['messages'] = [{'id': 'gone'}, {'id': 'huge'}, {'id': 'one'}]
        self.server.details['gone'] = (404, {})
        self.server.details['huge'] = dict(self.server.details['one'], id='huge', text={'plain': 'x' * 200001})
        result = self.client.sync('mailbox')
        self.assertEqual([m['source_id'] for m in result['messages']], ['one'])
        self.assertEqual([m['source_id'] for m in result['failed_messages']], ['gone', 'huge'])
        self.server.details['gone'] = dict(self.server.details['one'], id='gone')
        self.server.details['huge']['text'] = {'plain': 'Now available'}
        resumed = self.client.sync('mailbox', result['cursor'])
        self.assertEqual(len(resumed['messages']), 3)
        self.assertFalse(resumed['failed_messages'])

    def test_invalid_senders_and_multiple_reply_to_are_quarantined(self):
        cases = {'absent': {}, 'invalid': {'address': 'not-mail'}, 'shape': ['unexpected'],
                 'many': {'address': 'a@example.test,b@example.test'}}
        for source_id, sender in cases.items():
            self.server.details[source_id] = dict(self.server.details['one'], id=source_id, **{'from': sender})
        self.server.details['multi-reply'] = dict(self.server.details['one'], id='multi-reply',
                                                 replyTo=[{'address': 'one@example.test'}, {'address': 'two@example.test'}])
        self.server.details['bad-reply'] = dict(self.server.details['one'], id='bad-reply', replyTo=[{'address': 'invalid'}])
        self.server.pages['']['messages'] = [{'id': name} for name in [*cases, 'multi-reply', 'bad-reply', 'one']]
        result = self.client.sync('mailbox')
        self.assertEqual([item['source_id'] for item in result['messages']], ['one'])
        self.assertEqual(len(result['failed_messages']), 6)
        self.assertTrue(result['cursor'])

    def test_flags_and_missing_timestamp_are_conservative(self):
        self.server.details['one'].update(flags=['\\Answered', '\\Draft'], date=None)
        result = self.client.sync('mailbox')['messages'][0]
        self.assertTrue(result['answered'])
        self.assertTrue(result['draft'])
        self.assertTrue(result['is_historical'])
        self.server.details['one'].update(flags=[], labels=['DRAFT'], answered=False, draft=False)
        result = self.client.sync('mailbox')['messages'][0]
        self.assertFalse(result['answered'])
        self.assertTrue(result['draft'])

    def test_attachment_only_is_explained_and_headers_are_single_line(self):
        self.server.details['one']['text'] = {}
        self.server.details['one']['subject'] = 'Hello\r\nsecond line'
        result = self.client.sync('mailbox')['messages'][0]
        self.assertIn('no readable text', result['body'])
        self.assertEqual(result['subject'], 'Hello second line')

    def test_reply_delegates_thread_recipient_and_uses_stable_idempotency(self):
        for _ in range(2):
            result = self.client.submit_reply('mailbox', 'one', 'Approved Unicode reply: å', 'approval-revision-3')
            self.assertEqual(result['status'], 'queued')
        self.assertEqual(len(self.server.submissions), 1)
        call = self.server.calls[-1]
        self.assertEqual(call[4]['reference'], {'message': 'one', 'action': 'reply', 'ignoreMissing': False})
        self.assertNotIn('to', call[4])
        self.assertNotIn('subject', call[4])

    def test_old_version_cannot_submit(self):
        self.server.version = '2.51.9'
        with self.assertRaises(EmailEngineError):
            self.client.submit_reply('mailbox', 'one', 'reply', 'approval')
        self.assertFalse(self.server.submissions)

    def test_lost_submission_response_does_not_retry_automatically(self):
        lost = [True]
        def transport(method, url, headers, body):
            result = self.server(method, url, headers, body)
            if urlsplit(url).path.endswith('/submit') and lost[0]:
                lost[0] = False
                raise TimeoutError('Response lost after queue accepted')
            return result
        client = EmailEngineClient(self.client.base_url, 'private-token', transport=transport)
        with self.assertRaises(EmailEngineError):
            client.submit_reply('mailbox', 'one', 'approved', 'revision-7')
        self.assertEqual(len([call for call in self.server.calls if call[1].endswith('/submit')]), 1)
        # An explicit immediate retry carries the identical key. Caller policy
        # must NOT perform this blindly after the server's finite cache horizon.
        self.assertEqual(client.submit_reply('mailbox', 'one', 'approved', 'revision-7')['queue_id'], 'queue')
        self.assertEqual(len(self.server.submissions), 1)

    def test_outbox_never_claims_delivery_from_missing_queue(self):
        self.assertEqual(self.client.submission('mailbox', 'queue')['status'], 'queued')
        self.server.outbox['progress']['status'] = 'submitted'
        self.assertEqual(self.client.submission('mailbox', 'queue')['status'], 'submitted')
        self.server.outbox['progress']['status'] = 'smtp-completed'
        result = self.client.submission('mailbox', 'queue')
        self.assertEqual(result['status'], 'submitted')
        self.assertEqual(result['evidence'], 'outbox_smtp-completed')
        self.server.outbox['progress']['status'] = 'error'
        self.assertEqual(self.client.submission('mailbox', 'queue')['status'], 'retrying')
        self.server.outbox['nextAttempt'] = False
        self.assertEqual(self.client.submission('mailbox', 'queue')['status'], 'failed')
        self.server.outbox['account'] = 'other'
        with self.assertRaises(EmailEngineError):
            self.client.submission('mailbox', 'queue')
        self.server.outbox = None
        self.assertEqual(self.client.submission('mailbox', 'queue')['status'], 'unknown')

    def test_auth_form_is_documented_and_same_origin(self):
        result = self.client.authentication_form('http://127.0.0.1:48123/complete')
        self.assertIn('/accounts/new?data=', result)
        call = self.server.calls[-1]
        self.assertEqual(call[1], '/v1/authentication/form')
        self.assertTrue(call[4]['account'].startswith('nexus-'))

    def test_errors_and_public_responses_do_not_expose_access_token(self):
        def broken(*args):
            raise RuntimeError('private-token sensitive data')
        client = EmailEngineClient(self.client.base_url, 'private-token', transport=broken)
        with self.assertRaises(EmailEngineError) as caught:
            client.health()
        self.assertNotIn('private-token', str(caught.exception))
        self.server.details['one']['text']['plain'] = 'private-token'
        self.assertNotIn('private-token', json.dumps(self.client.sync('mailbox')))

    def test_actual_urllib_refuses_redirect_and_never_forwards_token(self):
        calls = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append(self.path)
                self.send_response(302)
                self.send_header('Location', '/target')
                self.end_headers()

            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = EmailEngineClient('http://127.0.0.1:%s' % server.server_port, 'private-token')
            with self.assertRaises(EmailEngineError):
                client.health()
            self.assertEqual(calls, ['/health'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
