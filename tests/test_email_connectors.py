from __future__ import annotations
import base64
import hashlib
import json
import tempfile
import time
import unittest
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.parser import BytesParser
from email import policy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from our_harness.email_connectors import CONTRACT, ConnectorHTTPError, EmailConnectors
from our_harness.models import HarnessError


class Secrets:
    def protect(self, value):
        return base64.b64encode(value.encode()).decode()
    def unprotect(self, value):
        return base64.b64decode(value).decode()


class EmailConnectorsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='arbitrary-oauth-')
        self.addCleanup(self.temp.cleanup)
        self.calls=[]
        self.opened=[]
        self.profile_email='user@example.test'
        self.registrations={'outlook':{'client_id':'test-microsoft-registration'},'gmail':{'client_id':'test-google-registration','client_secret':'test-desktop-value'}}
        def transport(method,url,headers,body):
            self.calls.append((method,url,headers,body))
            if url.endswith('/token'):
                return {'access_token':'SECRET_ACCESS','refresh_token':'SECRET_REFRESH','expires_in':3600}
            if 'graph.microsoft.com/v1.0/me?' in url:
                return {'mail':self.profile_email,'displayName':'Example'}
            if url.endswith('/profile'):
                return {'emailAddress':self.profile_email,'historyId':'100'}
            return {}
        self.transport=transport
        self.connectors=EmailConnectors(Path(self.temp.name),Secrets(),self.registrations,transport=transport,browser_open=lambda url:self.opened.append(url) or True)
        self.addCleanup(self.connectors.close)

    def connect(self, provider='outlook', previous=''):
        started=self.connectors.begin(provider,previous)
        fields=urllib.parse.parse_qs(urllib.parse.urlsplit(started['authorization_url']).query)
        self.connectors._complete(started['session_id'],{'state':fields['state'],'code':['AUTH_CODE']})
        return self.connectors.status(started['session_id'])['connection']

    def test_missing_registration_is_explicit_no_browser_or_network(self):
        self.connectors.update_registrations({})
        self.assertTrue(all(not item['configured'] for item in self.connectors.registration_status()))
        with self.assertRaisesRegex(HarnessError,'client ID is required'):
            self.connectors.begin('outlook')
        self.assertEqual(self.calls,[])
        self.assertEqual(self.opened,[])

    def test_pkce_system_browser_and_loopback_state(self):
        result=self.connectors.begin('outlook')
        fields=urllib.parse.parse_qs(urllib.parse.urlsplit(result['authorization_url']).query)
        session=self.connectors.sessions[result['session_id']]
        self.assertTrue(fields['redirect_uri'][0].startswith('http://localhost:'))
        self.assertEqual(fields['code_challenge_method'],['S256'])
        challenge=base64.urlsafe_b64encode(hashlib.sha256(session['verifier'].encode()).digest()).decode().rstrip('=')
        self.assertEqual(fields['code_challenge'],[challenge])
        self.assertNotIn(session['verifier'],result['authorization_url'])
        with self.assertRaises(HarnessError): self.connectors._complete(result['session_id'],{'state':['wrong'],'code':['x']})
        self.assertEqual(self.connectors.status(result['session_id'])['state'],'pending')
        self.assertEqual(self.calls,[])

    def test_real_loopback_callback_and_token_exchange(self):
        result=self.connectors.begin('gmail')
        fields=urllib.parse.parse_qs(urllib.parse.urlsplit(result['authorization_url']).query)
        self.assertTrue(fields['redirect_uri'][0].startswith('http://127.0.0.1:'))
        callback=fields['redirect_uri'][0]+'?'+urllib.parse.urlencode({'state':fields['state'][0],'code':'AUTH_CODE'})
        with urllib.request.urlopen(callback,timeout=3) as response:
            self.assertEqual(response.status,200)
        public=self.connectors.status(result['session_id'])
        self.assertEqual(public['state'],'connected')
        self.assertEqual(public['connection']['email'],'user@example.test')
        exchange=urllib.parse.parse_qs(self.calls[0][3].decode())
        self.assertIn('code_verifier',exchange)
        self.assertEqual(exchange['client_secret'],['test-desktop-value'])
        self.assertNotIn('SECRET',json.dumps(public))
        self.assertNotIn('SECRET',next(self.connectors.root.glob('*.json')).read_text())

    def test_replay_expiry_and_denial(self):
        result=self.connectors.begin('outlook')
        session=self.connectors.sessions[result['session_id']]
        session['expires_at']=time.time()-1
        self.assertEqual(self.connectors.status(result['session_id'])['state'],'expired')
        with self.assertRaises(HarnessError): self.connectors._complete(result['session_id'],{'state':['x'],'code':['x']})
        fresh=self.connectors.begin('outlook')
        state=self.connectors.sessions[fresh['session_id']]['state_token']
        with self.assertRaises(HarnessError): self.connectors._complete(fresh['session_id'],{'state':[state],'error':['access_denied']})
        self.assertEqual(self.connectors.status(fresh['session_id'])['state'],'error')
        connection=self.connect()
        self.assertEqual(connection['state'],'connected')

    def test_reconnect_rejects_another_mailbox_and_preserves_original(self):
        connection=self.connect()
        self.profile_email='other@example.test'
        result=self.connectors.begin('outlook',connection['id'])
        state=self.connectors.sessions[result['session_id']]['state_token']
        with self.assertRaisesRegex(HarnessError,'different mailbox'):
            self.connectors._complete(result['session_id'],{'state':[state],'code':['new']})
        self.assertEqual(self.connectors.connection(connection['id'])['email'],'user@example.test')
        with self.assertRaises(HarnessError): self.connectors.begin('gmail',connection['id'])

    def test_registration_change_invalidates_pending_and_durable_tokens(self):
        connection=self.connect()
        pending=self.connectors.begin('outlook')
        self.connectors.update_registrations({'outlook':{'client_id':'replacement'}})
        self.assertEqual(self.connectors.status(pending['session_id'])['state'],'error')
        self.assertEqual(self.connectors.connection(connection['id'])['state'],'reconnect_required')
        with self.assertRaises(HarnessError): self.connectors.sync(connection['id'])

    def test_refresh_rotates_and_persists_after_restart(self):
        connection=self.connect()
        value=self.connectors._load(connection['id'])
        value['expires_at']=time.time()-1
        self.connectors._save(value)
        self.connectors._ready(connection['id'])
        refresh=urllib.parse.parse_qs(self.calls[-1][3].decode())
        self.assertEqual(refresh['grant_type'],['refresh_token'])
        self.assertEqual(refresh['refresh_token'],['SECRET_REFRESH'])
        restarted=EmailConnectors(Path(self.temp.name),Secrets(),self.registrations,transport=self.transport)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.connection(connection['id'])['state'],'connected')
        self.assertGreater(restarted._load(connection['id'])['expires_at'],time.time())

    def test_refresh_rejection_requests_reconnect_without_token_leak(self):
        connection=self.connect()
        value=self.connectors._load(connection['id']); value['expires_at']=0; self.connectors._save(value)
        def rejected(*args): raise ConnectorHTTPError(400)
        self.connectors.transport=rejected
        with self.assertRaises(HarnessError): self.connectors.sync(connection['id'])
        self.assertEqual(self.connectors.connection(connection['id'])['state'],'reconnect_required')

    def test_graph_delta_text_body_and_opaque_bound_cursor(self):
        connection=self.connect()
        message={'from':{'emailAddress':{'address':'colleague@example.test'}},
                 'replyTo':[{'emailAddress':{'address':'reply-desk@example.test'}}],
                 'subject':'Test','body':{'contentType':'text','content':'Hello'},
                 'internetMessageId':'<source@example.test>', 'conversationId':'conversation-opaque',
                 'receivedDateTime':'2026-09-01T12:34:56Z'}
        def graph(method,url,headers,body):
            self.calls.append((method,url,headers,body))
            parsed=urllib.parse.urlsplit(url)
            if parsed.path.startswith('/v1.0/me/messages/'):
                self.assertEqual(set(urllib.parse.parse_qs(parsed.query)['$select'][0].split(',')),
                                 {'id','from','replyTo','subject','body','internetMessageId','conversationId','receivedDateTime'})
                self.assertEqual(headers['Prefer'],'outlook.body-content-type="text"')
                return message
            return {'value':[{'id':'message-one'},{'id':'gone','@removed':{}}],'@odata.deltaLink':'https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$deltatoken=opaque'}
        self.connectors.transport=graph
        page=self.connectors.sync(connection['id'])
        self.assertEqual(page['messages'],[{'source_id':'message-one','sender':'colleague@example.test','subject':'Test','body':'Hello',
                                          'reply_to':'reply-desk@example.test','internet_message_id':'<source@example.test>',
                                          'thread_id':'conversation-opaque','received_at':'2026-09-01T12:34:56Z'}])
        self.assertFalse(page['has_more'])
        self.assertEqual(json.loads(page['cursor'])['connection'],connection['id'])
        cursor=json.loads(page['cursor']);cursor['connection']='another'
        with self.assertRaises(HarnessError):self.connectors.sync(connection['id'],json.dumps(cursor))

    def test_graph_external_continuation_never_receives_credentials(self):
        connection=self.connect()
        self.connectors.transport=lambda *args:{'value':[],'@odata.nextLink':'https://attacker.example.test/steal'}
        with self.assertRaises(HarnessError):self.connectors.sync(connection['id'])
        calls=[]
        self.connectors.transport=lambda *args:calls.append(args) or {}
        cursor=self.connectors._cursor(self.connectors._load(connection['id']),{'next':'https://attacker.example.test/v1.0/me/mailFolders/inbox/messages/delta'})
        with self.assertRaises(HarnessError):self.connectors.sync(connection['id'],cursor)
        self.assertEqual(calls,[])

    def test_gmail_initial_history_and_text_message(self):
        connection=self.connect('gmail')
        def gmail(method,url,headers,body):
            self.calls.append((method,url,headers,body))
            if url.endswith('profile'):return {'historyId':'100'}
            if '/messages?' in url:return {'messages':[{'id':'one'}]}
            if '/history?' in url:return {'history':[{'messagesAdded':[{'message':{'id':'two'}}]}],'historyId':'105'}
            self.assertIn('format=full',url)
            return {'threadId':'gmail-thread-opaque','internalDate':'1704067200000',
                    'payload':{'mimeType':'text/plain','headers':[
                        {'name':'From','value':'Colleague <colleague@example.test>'},
                        {'name':'rEpLy-To','value':'Reply Desk <reply-desk@example.test>'},
                        {'name':'Message-ID','value':'<source@example.test>'},
                        {'name':'References','value':'<older@example.test> <prior@example.test>'},
                        {'name':'Subject','value':'A conversation'}],
                        'body':{'data':base64.urlsafe_b64encode(b'Hello').decode().rstrip('=')}}}
        self.connectors.transport=gmail
        first=self.connectors.sync(connection['id'])
        self.assertEqual(first['messages'][0]['body'],'Hello')
        self.assertEqual(first['messages'][0]['reply_to'],'Reply Desk <reply-desk@example.test>')
        self.assertEqual(first['messages'][0]['internet_message_id'],'<source@example.test>')
        self.assertEqual(first['messages'][0]['references'],'<older@example.test> <prior@example.test>')
        self.assertEqual(first['messages'][0]['thread_id'],'gmail-thread-opaque')
        self.assertEqual(first['messages'][0]['received_at'],'2024-01-01T00:00:00+00:00')
        self.assertEqual(json.loads(first['cursor'])['mode'],'history')
        second=self.connectors.sync(connection['id'],first['cursor'])
        self.assertEqual(second['messages'][0]['source_id'],'two')
        self.assertEqual(json.loads(second['cursor'])['history'],'105')

    def test_send_sender_binding_and_no_retry_on_ambiguous_401(self):
        connection=self.connect()
        mail=EmailMessage();mail['From']='user@example.test';mail['To']='colleague@example.test';mail.set_content('Reviewed reply')
        calls=[]
        def rejected(*args):calls.append(args);raise ConnectorHTTPError(401)
        self.connectors.transport=rejected
        with self.assertRaises(ConnectorHTTPError):self.connectors.send(connection['id'],mail)
        self.assertEqual(len(calls),1)
        mail.replace_header('From','another@example.test')
        with self.assertRaises(HarnessError):self.connectors.send(connection['id'],mail)
        self.assertEqual(len(calls),1)

    def test_outlook_and_gmail_send_mime(self):
        mail=EmailMessage();mail['From']='user@example.test';mail['To']='colleague@example.test';mail.set_content('Reviewed reply')
        for provider in ('outlook','gmail'):
            with self.subTest(provider=provider):
                self.connectors.transport=self.transport
                connection=self.connect(provider)
                sent=[]
                self.connectors.transport=lambda *args:sent.append(args) or {'id':'sent-id'}
                self.assertTrue(self.connectors.send(connection['id'],mail)['accepted'])
                self.assertEqual(sent[0][0],'POST')
                if provider=='outlook':self.assertEqual(sent[0][2]['Content-Type'],'text/plain')
                else:self.assertIn('raw',json.loads(sent[0][3]))

    def test_reviewed_reply_thread_headers_and_reply_to_recipient_reach_provider(self):
        mail=EmailMessage()
        mail['From']='user@example.test'
        mail['To']='Reply Desk <reply-desk@example.test>'
        mail['Subject']='Re: A conversation'
        mail['In-Reply-To']='<source@example.test>'
        mail['References']='<older@example.test> <source@example.test>'
        mail.set_content('Reviewed Unicode reply: åäö')
        for provider in ('outlook','gmail'):
            with self.subTest(provider=provider):
                self.connectors.transport=self.transport
                connection=self.connect(provider)
                sent=[]
                self.connectors.transport=lambda *args:sent.append(args) or {'id':'sent-id'}
                response=self.connectors.send(connection['id'],mail,thread_id='provider-thread-opaque')
                self.assertTrue(response['accepted'])
                self.assertEqual(len(sent),1)
                if provider=='outlook':
                    self.assertEqual(sent[0][1],'https://graph.microsoft.com/v1.0/me/sendMail')
                    raw=base64.b64decode(sent[0][3])
                else:
                    payload=json.loads(sent[0][3])
                    self.assertEqual(payload['threadId'],'provider-thread-opaque')
                    raw=base64.urlsafe_b64decode(payload['raw']+'='*(-len(payload['raw'])%4))
                actual=BytesParser(policy=policy.default).parsebytes(raw)
                self.assertEqual(str(actual['To']),'Reply Desk <reply-desk@example.test>')
                self.assertEqual(str(actual['In-Reply-To']),'<source@example.test>')
                self.assertEqual(str(actual['References']),'<older@example.test> <source@example.test>')
                self.assertEqual(str(actual['Subject']),'Re: A conversation')
                self.assertIn('Reviewed Unicode reply: åäö',actual.get_content())

    def test_disconnect_erases_tokens_and_close_expires_listener(self):
        connection=self.connect()
        public=self.connectors.disconnect(connection['id'])
        self.assertEqual(public['state'],'disconnected')
        self.assertFalse(self.connectors._load(connection['id'])['refresh_token'])
        with self.assertRaises(HarnessError):self.connectors.sync(connection['id'])
        pending=self.connectors.begin('gmail')
        self.connectors.close()
        self.assertEqual(self.connectors.status(pending['session_id'])['state'],'expired')
        self.assertFalse(self.connectors.sessions[pending['session_id']]['thread'].is_alive())

    def test_reconnect_callback_after_other_instance_disconnect_is_rejected(self):
        connection=self.connect()
        started=self.connectors.begin('outlook',connection['id'])
        session=self.connectors.sessions[started['session_id']]
        second=EmailConnectors(Path(self.temp.name),Secrets(),self.registrations,transport=self.transport)
        self.addCleanup(second.close)
        second.disconnect(connection['id'])
        before=len(self.calls)
        with self.assertRaisesRegex(HarnessError,'connection changed'):
            self.connectors._complete(started['session_id'],{'state':[session['state_token']],'code':['late']})
        self.assertEqual(len(self.calls),before)
        self.assertEqual(second.connection(connection['id'])['state'],'disconnected')

    def test_registration_update_other_instance_invalidates_late_callback(self):
        started=self.connectors.begin('outlook')
        session=self.connectors.sessions[started['session_id']]
        second=EmailConnectors(Path(self.temp.name),Secrets(),self.registrations,transport=self.transport)
        self.addCleanup(second.close)
        second.update_registrations({'outlook':{'client_id':'changed-registration'}})
        with self.assertRaisesRegex(HarnessError,'registration changed'):
            self.connectors._complete(started['session_id'],{'state':[session['state_token']],'code':['late']})
        self.assertEqual(self.calls,[])

    def test_two_instances_refresh_once_and_keep_rotated_token(self):
        connection=self.connect()
        value=self.connectors._load(connection['id']);value['expires_at']=0;self.connectors._save(value)
        second=EmailConnectors(Path(self.temp.name),Secrets(),self.registrations,transport=self.transport)
        self.addCleanup(second.close)
        before=len(self.calls)
        with ThreadPoolExecutor(max_workers=2) as pool:
            result=list(pool.map(lambda instance:instance._ready(connection['id']),[self.connectors,second]))
        self.assertEqual(len(self.calls)-before,1)
        self.assertTrue(all(item['refresh_token']=='SECRET_REFRESH' for item in result))

    def test_expired_graph_delta_restarts_full_sync_once(self):
        connection=self.connect()
        calls=[]
        def transport(method,url,*args):
            calls.append(url)
            if 'expired' in url:raise ConnectorHTTPError(410)
            return {'value':[],'@odata.deltaLink':'https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$deltatoken=fresh'}
        self.connectors.transport=transport
        cursor=self.connectors._cursor(self.connectors._load(connection['id']),{'next':'https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$deltatoken=expired'})
        page=self.connectors.sync(connection['id'],cursor)
        self.assertEqual(len(calls),2)
        self.assertIn('fresh',page['cursor'])

    def test_graph_html_fallback_is_readable_and_attachment_free(self):
        connection=self.connect()
        requested=[]
        def transport(method,url,headers,body):
            requested.append(url)
            if urllib.parse.urlsplit(url).path.startswith('/v1.0/me/messages/'):
                return {'from':{'emailAddress':{'address':'colleague@example.test'}},'subject':'HTML message','body':{'contentType':'html','content':'<html><head><style>hidden-style</style></head><body><p>Could you review the report?</p><script>steal()</script><img src="https://example.test/tracker"><p>Thanks &amp; regards</p></body></html>'},'attachments':[{'contentBytes':'NOT_IMPORTED'}]}
            return {'value':[{'id':'html-mail'}],'@odata.deltaLink':'https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$deltatoken=next'}
        self.connectors.transport=transport
        message=self.connectors.sync(connection['id'])['messages'][0]
        self.assertIn('Could you review the report?',message['body'])
        self.assertIn('Thanks & regards',message['body'])
        for unwanted in ('steal','hidden-style','tracker','NOT_IMPORTED','<p>'):
            self.assertNotIn(unwanted,message['body'])
        self.assertEqual(len(requested),2)
        self.assertTrue(all('$value' not in url and '/attachments' not in url for url in requested))

    def test_gmail_html_only_ignores_attachment_ids_and_inline_attachment_data(self):
        from our_harness.email_connectors import _gmail_body
        encode=lambda text:base64.urlsafe_b64encode(text.encode()).decode()
        payload={'mimeType':'multipart/mixed','parts':[
            {'mimeType':'text/html','body':{'data':encode('<p>Please review this <b>ordinary email</b>.</p><script>bad()</script>')}},
            {'mimeType':'application/pdf','filename':'report.pdf','body':{'attachmentId':'huge-file','size':80_000_000}},
            {'mimeType':'text/plain','filename':'attachment.txt','body':{'data':encode('ATTACHED SECRET TEXT')}},
            {'mimeType':'text/plain','headers':[{'name':'Content-Disposition','value':'attachment'}],'body':{'data':encode('ATTACHMENT WITH NO NAME')}}]}
        text=_gmail_body(payload)
        self.assertIn('Please review this ordinary email.',text)
        for excluded in ('ATTACHED','ATTACHMENT','bad()'):
            self.assertNotIn(excluded,text)
        payload['parts'].insert(0,{'mimeType':'text/plain','body':{'data':encode('Preferred plain-text alternative')}})
        self.assertEqual(_gmail_body(payload),'Preferred plain-text alternative')

    def test_deleted_message_warns_and_advances_without_blocking_sync(self):
        for provider in ('outlook','gmail'):
            self.connectors.transport=self.transport
            connection=self.connect(provider)
            def transport(method,url,headers,body):
                if url.endswith('profile'):return {'historyId':'100'}
                if '/messages/gone' in url:raise ConnectorHTTPError(404)
                if provider=='outlook':return {'value':[{'id':'gone'}],'@odata.deltaLink':'https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$deltatoken=next'}
                return {'messages':[{'id':'gone'}]}
            self.connectors.transport=transport
            page=self.connectors.sync(connection['id'])
            self.assertEqual(page['messages'],[])
            self.assertTrue(page['warnings'])
            self.assertTrue(page['cursor'])
            self.assertEqual(page['failed_messages'][0]['source_id'],'gone')

    def test_oversized_body_and_multiple_reply_to_are_quarantined_without_blocking_good_mail(self):
        for provider in ('outlook','gmail'):
            with self.subTest(provider=provider):
                self.connectors.transport=self.transport
                connection=self.connect(provider)
                def transport(method,url,headers,body):
                    path=urllib.parse.urlsplit(url).path
                    if path.endswith('/profile'):
                        return {'historyId':'100'}
                    if path.endswith('/messages/delta'):
                        return {'value':[{'id':key} for key in ('oversized','multiple','good')],
                                '@odata.deltaLink':'https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$deltatoken=next'}
                    if path.endswith('/messages'):
                        return {'messages':[{'id':key} for key in ('oversized','multiple','good')]}
                    message_id=path.rsplit('/',1)[-1]
                    content='x'*200001 if message_id=='oversized' else 'Complete readable message'
                    if provider=='outlook':
                        return {'from':{'emailAddress':{'address':'sender@example.test'}},
                                'replyTo':[{'emailAddress':{'address':address}} for address in
                                           (['first@example.test','second@example.test'] if message_id=='multiple' else ['reply@example.test'])],
                                'subject':'Subject','body':{'contentType':'text','content':content}}
                    return {'payload':{'mimeType':'text/plain',
                                       'headers':[{'name':'From','value':'sender@example.test'},
                                                  {'name':'Reply-To','value':'first@example.test, second@example.test' if message_id=='multiple' else 'reply@example.test'}],
                                       'body':{'data':base64.urlsafe_b64encode(content.encode()).decode()}}}
                self.connectors.transport=transport
                page=self.connectors.sync(connection['id'])
                self.assertEqual([item['source_id'] for item in page['messages']],['good'])
                self.assertEqual([item['source_id'] for item in page['failed_messages']],['oversized','multiple'])
                self.assertIn('200,000',page['failed_messages'][0]['error'])
                self.assertIn('Reply-To',page['failed_messages'][1]['error'])
                self.assertEqual(page['messages'][0]['body'],'Complete readable message')
                self.assertTrue(page['cursor'])
                self.assertTrue(page['warnings'])

    def test_provider_service_failure_is_not_quarantined_or_checkpointed(self):
        for provider in ('outlook','gmail'):
            with self.subTest(provider=provider):
                self.connectors.transport=self.transport
                connection=self.connect(provider)
                def transport(method,url,headers,body):
                    path=urllib.parse.urlsplit(url).path
                    if path.endswith('/profile'):return {'historyId':'100'}
                    if path.endswith('/messages/delta'):
                        return {'value':[{'id':'failure'}],
                                '@odata.deltaLink':'https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?$deltatoken=next'}
                    if path.endswith('/messages'):return {'messages':[{'id':'failure'}]}
                    raise ConnectorHTTPError(503)
                self.connectors.transport=transport
                with self.assertRaises(ConnectorHTTPError) as caught:
                    self.connectors.sync(connection['id'])
                self.assertEqual(caught.exception.status,503)

    def test_gmail_does_not_treat_unfetched_text_or_truncated_mime_as_complete(self):
        from our_harness.email_connectors import MessageImportError, _gmail_body, _bounded_body
        self.assertEqual(len(_bounded_body('x'*200000)),200000)
        with self.assertRaises(MessageImportError):_bounded_body('x'*200001)
        with self.assertRaisesRegex(MessageImportError,'separate body download'):
            _gmail_body({'mimeType':'text/plain','body':{'attachmentId':'external-body'}})
        with self.assertRaisesRegex(MessageImportError,'too many MIME parts'):
            _gmail_body({'mimeType':'multipart/mixed','parts':[{'mimeType':'image/png'}]*201})
        nested={'mimeType':'text/plain','body':{'data':'SGVsbG8='}}
        for _ in range(22):nested={'mimeType':'multipart/alternative','parts':[nested]}
        with self.assertRaisesRegex(MessageImportError,'depth limit'):_gmail_body(nested)

    def test_path_traversal_and_unexpected_provider_rejected(self):
        with self.assertRaises(HarnessError):self.connectors.connection('../anything')
        with self.assertRaises(HarnessError):self.connectors.begin('unknown')

if __name__=='__main__':unittest.main()
