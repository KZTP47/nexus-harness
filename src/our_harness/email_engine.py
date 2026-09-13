"""EmailEngine API adapter; configuration and credentials belong to the caller.

API contracts: https://learn.emailengine.app/docs/api-reference/messages-api
and /sending-api, /accounts/hosted-authentication (checked 2026-09-12).
Polling reconciles the whole Inbox, one continuation plus one head page per call. It is not an
event log: callers must atomically deduplicate imports and persist the returned
cursor. The caller must also persist failed_messages before acknowledging that
cursor: missing or oversized messages are quarantined, never drafted or silently
discarded. Restarting a pass catches arrivals that shifted earlier boundaries.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from html.parser import HTMLParser
from email.utils import getaddresses

from .models import HarnessError

CONTRACT = 'emailengine/inbox-v1'
MAX_RESPONSE = 4_000_000
PAGE_SIZE = 50
MAX_TEXT = 200_000


class EmailEngineError(HarnessError):
    def __init__(self, message, *, status=0):
        self.status = status
        super().__init__(message)


def validate_base_url(value):
    """Allow TLS servers or explicit loopback HTTP, with an optional URL prefix."""
    if not isinstance(value, str) or any(ord(c) < 33 for c in value) or '\\' in value:
        raise EmailEngineError('Enter a valid EmailEngine server URL.')
    try:
        url = urllib.parse.urlsplit(value)
        host, port = url.hostname, url.port
        loopback = host == 'localhost'
        if host and not loopback:
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
        valid = host and url.username is None and url.password is None and not url.query and not url.fragment
        valid = valid and url.scheme in ('https', 'http') and (url.scheme == 'https' or loopback)
        valid = valid and not any(part in ('.', '..') for part in urllib.parse.unquote(url.path).split('/'))
        if not valid or (port is not None and port < 1):
            raise ValueError()
    except ValueError:
        raise EmailEngineError('Use HTTPS for EmailEngine, or HTTP on localhost, without URL credentials or query parameters.') from None
    return urllib.parse.urlunsplit((url.scheme, url.netloc.lower(), url.path.rstrip('/'), '', ''))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise EmailEngineError('EmailEngine redirects are not accepted. Check the server URL.')


def _transport(method, url, headers, body):
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=40) as response:
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise EmailEngineError('EmailEngine response exceeds the supported size.')
            return response.status, json.loads(raw)
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except EmailEngineError:
        raise
    except Exception:
        raise EmailEngineError('EmailEngine could not be reached or returned an invalid response. Check service health and connection settings.') from None


def _identifier(value):
    if not isinstance(value, str) or not value or len(value) > 1024 or any(ord(c) < 32 for c in value):
        raise EmailEngineError('A valid EmailEngine account or message identifier is required.')
    return urllib.parse.quote(value, safe='')


def _date(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def _header(value, limit=4096):
    if value is None:
        return ''
    if not isinstance(value, str):
        raise EmailEngineError('EmailEngine returned an invalid header value.')
    return ' '.join(value.replace('\r', ' ').replace('\n', ' ').split())[:limit]


def _address(value):
    """EmailEngine address fields must contain one usable mailbox, not a list."""
    if not isinstance(value, str) or len(value) > 320 or not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+', value):
        return None
    try:
        addresses = getaddresses([value])
    except ValueError:
        return None
    return value if len(addresses) == 1 and addresses[0][1] == value else None


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('p', 'br', 'div', 'li'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


class EmailEngineClient:
    def __init__(self, base_url, token, *, transport=None):
        self.base_url = validate_base_url(base_url)
        if not isinstance(token, str) or not token.strip() or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise EmailEngineError('An EmailEngine API access token is required.')
        self._token = token
        self._transport = transport or _transport

    def _clean(self, value):
        if isinstance(value, str):
            return value.replace(self._token, '[redacted]')
        if isinstance(value, list):
            return [self._clean(item) for item in value]
        if isinstance(value, dict):
            return {self._clean(str(key)): self._clean(item) for key, item in value.items()}
        return value

    def _request(self, method, path, *, query=None, payload=None, headers=None):
        url = self.base_url + path
        if query:
            url += '?' + urllib.parse.urlencode(query)
        auth = {'Authorization': 'Bearer ' + self._token, 'Accept': 'application/json'}
        auth.update(headers or {})
        body = None
        if payload is not None:
            auth['Content-Type'] = 'application/json'
            body = json.dumps(payload).encode('utf-8')
        try:
            status, result = self._transport(method, url, auth, body)
        except Exception:
            # Third-party errors can echo headers, mailbox content or credentials.
            raise EmailEngineError('EmailEngine request failed. Check service health and connection settings.') from None
        if not isinstance(status, int) or not 200 <= status < 300:
            code = status if isinstance(status, int) else 0
            raise EmailEngineError('EmailEngine rejected the request (HTTP %s).' % code, status=code)
        if not isinstance(result, dict):
            raise EmailEngineError('EmailEngine returned an invalid response.')
        return self._clean(result)

    def health(self):
        result = self._request('GET', '/health')
        if result.get('success') is not True:
            raise EmailEngineError('EmailEngine reports an unhealthy service.')
        stats = self._request('GET', '/v1/stats')
        version = str(stats.get('version', ''))
        match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?', version)
        supports = bool(match and tuple(map(int, match.groups())) >= (2, 52, 0))
        return {'healthy': True, 'version': version, 'supports_idempotency': supports}

    @staticmethod
    def _public_account(item):
        return {key: item.get(key, '') for key in ('account', 'name', 'email', 'state', 'type')}

    def accounts(self):
        result, page = [], 0
        while True:
            data = self._request('GET', '/v1/accounts', query={'page': page, 'pageSize': 100})
            rows = data.get('accounts')
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise EmailEngineError('EmailEngine returned an invalid account list.')
            result.extend(self._public_account(row) for row in rows)
            pages = data.get('pages', 1)
            if not isinstance(pages, int) or pages < 0 or pages > 100:
                raise EmailEngineError('EmailEngine account list exceeds the supported page bound.')
            page += 1
            if page >= pages:
                return result

    def account(self, account_id):
        data = self._request('GET', '/v1/account/' + _identifier(account_id))
        if data.get('account') != account_id:
            raise EmailEngineError('EmailEngine returned a different account identity.')
        return self._public_account(data)

    def authentication_form(self, redirect_url, account_id=None):
        redirect_url = validate_base_url(redirect_url)
        account_id = account_id or 'nexus-' + uuid.uuid4().hex
        _identifier(account_id)
        result = self._request('POST', '/v1/authentication/form', payload={'account': account_id, 'redirectUrl': redirect_url})
        value = result.get('url', '')
        try:
            url = urllib.parse.urlsplit(value)
            base = urllib.parse.urlsplit(self.base_url)
            if (url.scheme, url.netloc) != (base.scheme, base.netloc) or url.username or url.password or url.fragment or any(ord(c) < 33 for c in value):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise EmailEngineError('EmailEngine returned an authentication link for a different server. Check its public service URL.') from None
        return value

    def sync(self, account_id, cursor=''):
        prefix = '/v1/account/' + _identifier(account_id)
        fingerprint = hashlib.sha256(json.dumps([CONTRACT, self.base_url, account_id]).encode()).hexdigest()
        if cursor:
            try:
                if len(cursor) > 32_768:
                    raise ValueError()
                state = json.loads(cursor)
                if state.get('contract') != CONTRACT or state.get('fingerprint') != fingerprint or not _date(state.get('baseline')):
                    raise ValueError()
                if not isinstance(state.get('next'), str) or len(state['next']) > 16_384:
                    raise ValueError()
                head_ids = state.get('head_ids', [])
                if not isinstance(head_ids, list) or len(head_ids) > PAGE_SIZE or any(not isinstance(v, str) or len(v) > 1024 for v in head_ids):
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise EmailEngineError('EmailEngine sync checkpoint belongs to another connection or contract. Reconnect to start a new checkpoint.') from None
        else:
            state = {'contract': CONTRACT, 'fingerprint': fingerprint, 'baseline': datetime.now(timezone.utc).isoformat(), 'next': ''}
        query = {'path': 'INBOX', 'pageSize': PAGE_SIZE}
        if state['next']:
            query['cursor'] = state['next']
        warnings = []
        try:
            listing = self._request('GET', prefix + '/messages', query=query)
        except EmailEngineError as exc:
            if not state['next'] or exc.status not in (400, 410):
                raise
            # Provider paging tokens are not permanent sync tokens. Reconcile
            # again without changing the account's original historical cutoff.
            state['next'] = ''
            query.pop('cursor', None)
            listing = self._request('GET', prefix + '/messages', query=query)
            warnings.append('The mail paging checkpoint expired or was rejected; Inbox reconciliation restarted without changing the history cutoff.')
        rows = listing.get('messages')
        if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
            raise EmailEngineError('EmailEngine returned an invalid message page.')
        next_cursor = listing.get('nextPageCursor') or ''
        if not isinstance(next_cursor, str) or len(next_cursor) > 16_384 or (next_cursor and next_cursor == state['next']):
            raise EmailEngineError('EmailEngine returned an invalid paging checkpoint.')
        # A continuation can take hours on a large mailbox. Recheck the newest
        # page each time, so concurrent arrivals do not wait for old-mail import.
        # Commit head_ids only with successfully returned imports and checkpoint.
        head_rows = rows
        if state['next']:
            head = self._request('GET', prefix + '/messages', query={'path': 'INBOX', 'pageSize': PAGE_SIZE})
            head_rows = head.get('messages')
            if not isinstance(head_rows, list) or len(head_rows) > PAGE_SIZE:
                raise EmailEngineError('EmailEngine returned an invalid head page.')
            acknowledged = set(state.get('head_ids', []))
            fresh = [row for row in head_rows if isinstance(row, dict) and row.get('id') not in acknowledged]
            rows = fresh + rows
        new_head_ids = []
        for row in head_rows:
            if not isinstance(row, dict):
                raise EmailEngineError('EmailEngine returned invalid head metadata.')
            _identifier(row.get('id'))
            new_head_ids.append(row['id'])
        messages, failed_messages, seen = [], [], set()
        for row in rows:
            if not isinstance(row, dict):
                raise EmailEngineError('EmailEngine returned invalid message metadata.')
            source_id = row.get('id')
            encoded_id = _identifier(source_id)
            if source_id in seen:
                continue
            seen.add(source_id)
            try:
                item = self._request('GET', prefix + '/message/' + encoded_id, query={'textType': '*', 'maxBytes': 500_000, 'markAsSeen': 'false'})
            except EmailEngineError as exc:
                if exc.status not in (404, 410):
                    raise
                failed_messages.append({'source_id': source_id, 'error': 'The message moved or is no longer available. No draft was generated.'})
                continue
            if item.get('id') != source_id:
                raise EmailEngineError('EmailEngine returned a different message identity.')
            text = item.get('text') or {}
            if not isinstance(text, dict):
                raise EmailEngineError('EmailEngine returned invalid email text.')
            if text.get('hasMore'):
                failed_messages.append({'source_id': source_id, 'error': 'The email text exceeds the retrieval size limit. No draft was generated.'})
                continue
            if any(value is not None and not isinstance(value, str) for value in (text.get('plain'), text.get('html'))):
                raise EmailEngineError('EmailEngine returned invalid email text.')
            body = text.get('plain')
            if not body and text.get('html'):
                parser = _PlainText()
                parser.feed(text['html'])
                body = ''.join(parser.parts).strip()
            if body and len(body) > MAX_TEXT:
                failed_messages.append({'source_id': source_id, 'error': 'The email text exceeds the 200,000 character processing limit. No draft was generated.'})
                continue
            received = item.get('date', '')
            if not isinstance(received, str):
                received = ''
            date = _date(received)
            if date is None:
                warnings.append('A message has no valid timestamp and was imported as history.')
            sender = item.get('from') or {}
            sender_address = _address(sender.get('address')) if isinstance(sender, dict) else None
            if not sender_address:
                failed_messages.append({'source_id': source_id, 'error': 'The email has no valid single sender address. No draft was generated.'})
                continue
            if not isinstance(item.get('headers') or {}, dict):
                raise EmailEngineError('EmailEngine returned invalid email headers.')
            reply_to = item.get('replyTo') or []
            references = (item.get('headers') or {}).get('references') or []
            if not isinstance(references, list):
                raise EmailEngineError('EmailEngine returned invalid reply headers.')
            if not isinstance(reply_to, list) or len(reply_to) > 1 or any(not isinstance(addr, dict) or not _address(addr.get('address')) for addr in reply_to):
                failed_messages.append({'source_id': source_id, 'error': 'This email has multiple or unsupported Reply-To recipients. Review it in the mailbox; no draft was generated.'})
                continue
            flags = item.get('flags') if isinstance(item.get('flags'), list) else []
            labels = item.get('labels') if isinstance(item.get('labels'), list) else []
            messages.append({'source_id': source_id, 'sender': sender_address,
                             'subject': _header(item.get('subject', '')), 'body': body or '(This email has no readable text. Attachments have not been imported.)',
                             'received_at': received, 'reply_to': reply_to[0]['address'] if reply_to else '',
                             'internet_message_id': _header(item.get('messageId', '')),
                             'thread_id': _header(item.get('threadId', '')), 'references': ' '.join(_header(value) for value in references)[:8192],
                             'is_historical': date is None or date <= _date(state['baseline']),
                             'answered': item.get('answered') is True or '\\Answered' in flags,
                             'draft': item.get('draft') is True or '\\Draft' in flags or 'DRAFT' in labels})
        state['next'] = next_cursor
        state['head_ids'] = new_head_ids
        if failed_messages:
            warnings.append('%s message(s) need review; failed message identities must be retained for retry.' % len(failed_messages))
        return {'messages': messages, 'cursor': json.dumps(state, separators=(',', ':')),
                'has_more': bool(next_cursor), 'warnings': list(dict.fromkeys(warnings)), 'failed_messages': failed_messages}

    def submit_reply(self, account_id, source_id, text, submission_id):
        prefix = '/v1/account/' + _identifier(account_id)
        _identifier(source_id)
        _identifier(submission_id)
        if any(ord(c) < 33 or ord(c) > 126 for c in submission_id):
            raise EmailEngineError('The submission identifier must contain printable ASCII without spaces.')
        if not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > 500_000:
            raise EmailEngineError('A nonempty reply within the supported size is required.')
        if not self.health()['supports_idempotency']:
            raise EmailEngineError('EmailEngine 2.52.0 or newer is required for safe reply submission.')
        result = self._request('POST', prefix + '/submit',
                               payload={'text': text, 'reference': {'message': source_id, 'action': 'reply', 'ignoreMissing': False}},
                               headers={'Idempotency-Key': submission_id})
        if not result.get('queueId') or (result.get('reference') or {}).get('success') is False:
            raise EmailEngineError('EmailEngine submission outcome is unresolved. Do not submit another reply; reconcile this approval.')
        return {'queue_id': result['queueId'], 'message_id': result.get('messageId', ''), 'status': 'queued'}

    def submission(self, account_id, queue_id):
        _identifier(account_id)
        try:
            item = self._request('GET', '/v1/outbox/' + _identifier(queue_id))
        except EmailEngineError as exc:
            if exc.status == 404:
                return {'queue_id': queue_id, 'status': 'unknown', 'evidence': 'outbox_missing'}
            raise
        if item.get('account') != account_id or item.get('queueId') != queue_id:
            raise EmailEngineError('EmailEngine returned an outbox entry for a different account or submission.')
        progress = (item.get('progress') or {}).get('status', '')
        state = 'queued'
        if progress in ('submitted', 'smtp-completed'):
            state = 'submitted'
        elif progress == 'error':
            state = 'failed' if item.get('nextAttempt') is False else 'retrying'
        elif progress not in ('queued', 'processing', 'smtp-starting'):
            state = 'unknown'
        return {'queue_id': queue_id, 'message_id': item.get('messageId', ''), 'status': state,
                'evidence': 'outbox_' + str(progress), 'attempts': item.get('attemptsMade', 0)}
