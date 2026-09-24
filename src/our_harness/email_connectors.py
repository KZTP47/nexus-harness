"""Desktop OAuth and bounded Microsoft Graph/Gmail mail connectors.

Public-client PKCE uses the system browser and a short-lived loopback listener.
Applications must supply their own registered client IDs; no other app's ID is
borrowed. Tokens are encrypted by the supplied OS-backed secret store.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from contextlib import contextmanager
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from email import policy
from email.message import EmailMessage
from email.header import decode_header, make_header
from email.utils import getaddresses
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .models import HarnessError

SCHEMA_VERSION = 1
CONTRACT = 'email-oauth/v1'
SESSION_SECONDS = 300
_STORE_LOCKS = {}
_STORE_LOCKS_GUARD = threading.Lock()
MAX_RESPONSE = 3_000_000
MAX_MAIL = 2_000_000
PROVIDERS = {
    'outlook': {
        'name': 'Outlook / Microsoft 365',
        'scopes': 'offline_access https://graph.microsoft.com/User.Read https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/Mail.Send',
        'setup_url': 'https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app',
    },
    'gmail': {
        'name': 'Gmail / Google Workspace',
        'scopes': 'https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.send',
        'setup_url': 'https://developers.google.com/identity/protocols/oauth2/native-app',
    },
}


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _b64(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip('=')


class ConnectorHTTPError(HarnessError):
    def __init__(self, status):
        self.status = int(status)
        super().__init__('The mail provider rejected the request (HTTP %s). Reconnect or check account permissions.' % self.status)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HarnessError('Mail provider redirects are not accepted.')


def _transport(method, url, headers=None, body=None):
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=30) as response:
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise HarnessError('The mail provider response exceeded the supported size.')
            return raw
    except urllib.error.HTTPError as exc:
        # Provider error bodies can contain auth codes and personal data.
        raise ConnectorHTTPError(exc.code) from None
    except (OSError, urllib.error.URLError):
        raise HarnessError('The mail provider could not be reached. Check your network and reconnect if needed.') from None


_VOID_TAGS = frozenset(('area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'keygen', 'link', 'meta', 'param', 'source', 'track', 'wbr'))
_HEAD_TAGS = frozenset(('base', 'link', 'meta', 'noscript', 'script', 'style', 'template', 'title'))
_BLOCK_TAGS = frozenset(('p', 'div', 'li', 'tr', 'table', 'ul', 'ol', 'dl', 'dt', 'dd', 'blockquote', 'pre', 'section', 'article',
                         'header', 'footer', 'nav', 'main', 'aside', 'figure', 'figcaption', 'address', 'fieldset', 'details',
                         'summary', 'form', 'center', 'hr', 'option', 'caption', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'))
# Start tags that end an open <p>, as browsers do.
_P_ENDERS = frozenset(('address', 'article', 'aside', 'blockquote', 'center', 'details', 'dialog', 'dir', 'div', 'dl', 'fieldset',
                       'figcaption', 'figure', 'footer', 'form', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'header', 'hgroup', 'hr',
                       'main', 'menu', 'nav', 'ol', 'p', 'pre', 'search', 'section', 'summary', 'table', 'ul', 'xmp', 'listing',
                       'plaintext'))
# Elements whose content is never rendered (or is not the mail's text).
_NEVER_SHOWN = frozenset(('script', 'style', 'template', 'title', 'iframe', 'noembed', 'noframes', 'datalist', 'textarea',
                          'desc', 'metadata', 'rp', 'video', 'audio'))
_CODE = frozenset(('script', 'style', 'template', 'title'))  # not text the sender wrote at all
# An end tag never reaches past these to close an element opened outside them.
_SCOPE = frozenset(('td', 'th', 'table', 'caption', 'template', 'button', 'object', 'marquee', 'applet'))
_SPECIAL = (_P_ENDERS | frozenset(('li', 'dd', 'dt', 'td', 'th', 'tr', 'tbody', 'thead', 'tfoot', 'caption', 'select',
                                   'button', 'object', 'applet', 'marquee', 'template', 'iframe', 'textarea')))
_INLINE = frozenset(('a', 'abbr', 'b', 'bdi', 'bdo', 'big', 'cite', 'code', 'data', 'dfn', 'em', 'font', 'i', 'kbd', 'label',
                     'mark', 'q', 's', 'samp', 'small', 'span', 'strike', 'strong', 'sub', 'sup', 'time', 'tt', 'u', 'var'))
# Table structure: text or other elements placed directly inside it are moved
# out in front of the table by browsers ("foster parenting").
_TABLE_PARTS = frozenset(('table', 'tbody', 'thead', 'tfoot', 'tr'))
_TABLE_CONTENT = frozenset(('caption', 'colgroup', 'col', 'tbody', 'thead', 'tfoot', 'tr', 'td', 'th', 'script', 'style', 'template'))
_ABSOLUTE_SIZES = frozenset(('xx-small', 'x-small', 'small', 'medium', 'large', 'x-large', 'xx-large', 'xxx-large'))
_ABSOLUTE_UNITS = frozenset(('px', 'pt', 'pc', 'in', 'cm', 'mm', 'q', 'rem', 'vw', 'vh', 'vmin', 'vmax', 'svh', 'lvh', 'dvh'))
_LENGTH = re.compile(r'(\+?(?:\d+\.?\d*|\.\d+))([a-z%]*)$')
HIDDEN_TEXT_LIMIT = 4000
# Browsers stop nesting at this depth (deeper elements become siblings); it
# also keeps every per-tag step cheap on hostile, endlessly nested markup.
MAX_DEPTH = 512
MAX_HTML = 2_000_000
PARSE_BUDGET_SECONDS = 2.0
_LOG = logging.getLogger(__name__)
_KEYWORD = re.compile(r'[a-z-]+')
_CLIPPING = re.compile(r'hidden|clip|scroll|auto')
_NONZERO = re.compile(r'[1-9]')
_SPACE = re.compile(r'\s+')


class _OverBudget(Exception):
    pass


def _css(style):
    """Declarations of one inline style, in order: comments and escapes removed,
    the last declaration of a property wins unless an earlier one is !important.
    Returns {property: value} and {property: position}."""
    if not style or ':' not in style:
        return {}, {}
    style = re.sub(r'/\*.*?(?:\*/|$)', '', style, flags=re.S)

    def unescape(match):
        code = int(match.group(1), 16)
        char = chr(code) if 0 < code < 0x110000 and not 0xD800 <= code <= 0xDFFF else '\ufffd'
        # An escaped space is part of the word (`none\9` is not `none`), never trimmed.
        return '\ufffd' if char.isspace() else char
    style = re.sub(r'\\([0-9a-fA-F]{1,6})\s?', unescape, style)
    style = re.sub(r'\\(.)', lambda m: '\ufffd' if m.group(1).isspace() else m.group(1), style)
    found, order = {}, {}
    for position, declaration in enumerate(style.split(';')):
        name, colon, value = declaration.partition(':')
        if not colon:
            continue
        name, value = name.strip().lower(), value.strip().lower()
        important = bool(re.search(r'!\s*important\s*$', value))
        value = re.sub(r'!\s*important\s*$', '', value).strip()
        if name and value and (important or not found.get(name, ('', False))[1]):
            found[name] = (value, important)
            order[name] = position
    return {name: value for name, (value, _) in found.items()}, order


def _size(value):
    """('zero' | 'absolute' | 'relative' | None) for a font-size value.

    Only a literal zero hides. A relative size (em, %, larger, smaller,
    inherit) keeps the parent's size, so it stays zero under a zero parent.
    Anything that is not literal (calc(), min(), clamp(), var(), a negative or
    unknown value) is 'absolute': uncertain text is never hidden by guesswork.
    """
    value = value.strip()
    literal = _literal_size(value)
    if literal:
        return literal
    if value in ('inherit', 'larger', 'smaller', 'unset', 'revert') or not value:
        return None
    return 'absolute'  # initial, calc(), var(), negative or invalid: not zero


def _literal_size(value):
    """The size a literal length or keyword gives, else None."""
    if value in _ABSOLUTE_SIZES:
        return 'absolute'
    length = _LENGTH.match(value)
    if not length:
        return None
    number, unit = float(length.group(1)), length.group(2)
    if number == 0:
        return 'zero'
    if unit == '':
        return None
    return 'absolute' if unit in _ABSOLUTE_UNITS else 'relative'


def _font_size(css, order):
    candidates = []
    if 'font-size' in css:
        candidates.append((order.get('font-size', 0), _size(css['font-size'])))
    if 'font' in css:
        # The shorthand's size is its first token that is a size (`font:0/0 a`).
        shorthand = next((size for size in (_literal_size(token.split('/', 1)[0]) for token in css['font'].split()) if size), None)
        candidates.append((order.get('font', 0), shorthand))
    return max(candidates)[1] if candidates else None  # the later declaration wins


def _opacity(value):
    """A literal opacity (number or percentage), clamped to 0..1; 1.0 when not literal."""
    value = (value or '').strip()
    try:
        number = float(value[:-1]) / 100 if value.endswith('%') else float(value) if value else 1.0
    except ValueError:
        return 1.0  # `0px`, calc() and var() are not literal opacities: visible
    return max(0.0, min(1.0, number))


class _Element:
    __slots__ = ('tag', 'hide', 'origin', 'unseen', 'zero', 'blocks', 'ordinal', 'details', 'block')

    def __init__(self, tag, hide=False, origin=False, unseen=False, zero=False, ordinal=0, details=False):
        self.tag, self.hide, self.origin, self.unseen, self.zero = tag, hide, origin, unseen, zero
        self.blocks, self.ordinal, self.details, self.block = False, ordinal, details, tag in _BLOCK_TAGS


class _ReadableHTML(HTMLParser):
    """Extract inert visible text without loading images, links, or scripts.

    Best effort, modelled on how a browser builds the page: one stack of open
    elements with implied ends and scoped end tags, and inherited hiding
    (display:none, the hidden attribute, opacity 0, zero clipped boxes, zero
    font size, visibility:hidden). Only literal, unambiguous signals hide text;
    anything uncertain stays visible. Text a reader cannot see is kept apart in
    `hidden_parts`, never in the body. Every step is amortised O(1): per-tag
    counts answer "is one open?" without scanning, and the stack is capped.
    """
    CDATA_CONTENT_ELEMENTS = ('script', 'style', 'xmp', 'iframe', 'noembed', 'noframes', 'textarea', 'title')

    def __init__(self, exempt=(), deadline=None):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden_parts, self.hidden_size = [], [], 0
        self.stack = [_Element('#root')]
        self.open = {}  # tag -> how many are on the stack
        self.code = 0  # open script/style/template/title
        self.pending_inline = []  # hidden inline elements not yet known to hold blocks
        self.pre = 0
        self.head = False
        self.quirks = True
        self.started = False
        self.exempt = frozenset(exempt)
        self.ordinals = 0
        self.confused = None
        self.deadline = deadline
        self.steps = 0

    # -- tree building ---------------------------------------------------
    def handle_decl(self, decl):
        if not self.started and decl.lower().startswith('doctype'):
            self.quirks = False

    def _push(self, element):
        self.stack.append(element)
        self.open[element.tag] = self.open.get(element.tag, 0) + 1
        if element.tag in _CODE:
            self.code += 1

    def _pop_to(self, index):
        for element in self.stack[index:][::-1]:
            if element.tag in ('td', 'th'):
                self._emit('\t')
            elif element.block:
                self._emit('\n')
            if element.tag == 'pre' and self.pre:
                self.pre -= 1
            self.open[element.tag] -= 1
            if element.tag in _CODE:
                self.code -= 1
        del self.stack[index:]

    def _find(self, names, stop):
        if not any(self.open.get(name) for name in names):
            return None  # none open: answered without a scan
        for index in range(len(self.stack) - 1, 0, -1):
            tag = self.stack[index].tag
            if tag in names:
                return index
            if tag in stop:
                return None
        return None

    def _implied_close(self, tag):
        if tag in _P_ENDERS and not (tag == 'table' and self.quirks):
            index = self._find(('p',), _SCOPE | {'html'})
            if index:
                self._pop_to(index)
        if tag in ('li', 'dd', 'dt'):
            names = ('li',) if tag == 'li' else ('dd', 'dt')
            if any(self.open.get(name) for name in names):
                for index in range(len(self.stack) - 1, 0, -1):
                    current = self.stack[index].tag
                    if current in names:
                        self._pop_to(index)
                        break
                    if current in _SPECIAL and current not in ('address', 'div', 'p'):
                        break
        elif tag in ('td', 'th'):
            index = self._find(('td', 'th'), ('table', 'template'))
            if index:
                self._pop_to(index)
        elif tag in ('tr', 'tbody', 'thead', 'tfoot'):
            index = self._find(('tr',) if tag == 'tr' else ('tr', 'tbody', 'thead', 'tfoot'), ('table', 'template'))
            if index:
                self._pop_to(index)
        elif tag in ('option', 'optgroup'):
            index = self._find(('option',) if tag == 'option' else ('option', 'optgroup'), _SPECIAL - {'option', 'optgroup'})
            if index:
                self._pop_to(index)

    def _host(self, tag=None):
        """The element text or a new element really belongs to: content placed
        directly inside a table's structure is moved out in front of the table."""
        index = len(self.stack) - 1
        if self.stack[index].tag in _TABLE_PARTS and tag not in _TABLE_CONTENT:
            while index > 0 and self.stack[index].tag in _TABLE_PARTS:
                index -= 1
        return self.stack[index]

    def handle_startendtag(self, tag, attrs):
        # `<br/>`, `<img .../>` and even `<div/>`: HTML ignores the slash.
        self.handle_starttag(tag, attrs)

    def _state(self, tag, values, css, order, parent):
        display, opacity, clipped = '', 1.0, False
        inline = tag in _INLINE
        if css:
            display = css.get('display', '')
            display = display if _KEYWORD.fullmatch(display) else ''
            opacity = _opacity(css.get('opacity', ''))
            inline = (((tag in _INLINE and display in ('', 'inline')) or display == 'inline')
                      and css.get('float', 'none') == 'none' and css.get('position', '') not in ('absolute', 'fixed'))
            # A zero-size box that clips its overflow, unless the box still has room:
            # table cells and tables ignore a zero height, padding keeps space open.
            overflow = css.get('overflow', '') + css.get('overflow-x', '') + css.get('overflow-y', '')
            clipped = bool(overflow and not inline and tag not in ('td', 'th', 'table', 'tr')
                           and _CLIPPING.search(overflow)
                           and any(_size(css.get(name, '')) == 'zero' for name in ('height', 'max-height', 'width', 'max-width'))
                           and not any(_NONZERO.search(css.get(name, '')) for name in
                                       ('padding', 'padding-top', 'padding-bottom', 'padding-left', 'padding-right')))
        origin = (display == 'none' or ('hidden' in values and display in ('', 'none')) or opacity <= 0 or bool(clipped)
                  or tag in _NEVER_SHOWN or (tag == 'object' and values.get('data'))
                  or (tag == 'dialog' and 'open' not in values))
        self.ordinals += 1
        if origin and self.ordinals in self.exempt:
            origin = False
        # A closed <details> shows only its summary.
        hide = parent.hide or bool(origin) or (parent.details and tag != 'summary')
        visibility = css.get('visibility', '') if css else ''
        unseen = parent.unseen if visibility not in ('hidden', 'collapse', 'visible') else visibility != 'visible'
        size = _font_size(css, order) if css else None
        zero = True if size == 'zero' else False if size == 'absolute' else parent.zero
        element = _Element(tag, hide, bool(origin), unseen, zero, self.ordinals,
                           details=(tag == 'details' and 'open' not in values))
        return element, inline, display

    def handle_starttag(self, tag, attrs):
        self.started = True
        self._tick()
        values = {}
        for name, value in attrs:
            values.setdefault(name.lower(), value or '')  # the first duplicate attribute wins
        css, order = _css(values.get('style', ''))
        if tag in ('html', 'body'):
            if tag == 'body':
                self.head = False
            # Their styles apply to the whole page (a body at size 0 or opacity 0).
            root, _, _ = self._state(tag, values, css, order, self.stack[0])
            root.tag = '#root'
            self.stack[0] = root
            return
        if tag == 'head':
            self.head = True
            return
        if self.head and tag not in _HEAD_TAGS:
            self.head = False
        self._implied_close(tag)
        element, inline, display = self._state(tag, values, css, order, self._host(tag))
        block = (tag in _BLOCK_TAGS or tag in ('td', 'th')) if display in ('', 'none') else not (display.startswith('inline') or display == 'contents')
        element.block = block and tag not in ('td', 'th')
        if tag not in _VOID_TAGS and len(self.stack) < MAX_DEPTH:
            if block and self.pending_inline:
                for ancestor in self.pending_inline:
                    ancestor.blocks = True
                self.pending_inline = []
            if element.origin and tag in _INLINE:
                self.pending_inline.append(element)
            self._push(element)
        if tag == 'pre':
            self.pre += 1
        if tag == 'br':
            self._emit('\n')
        elif block and tag not in ('tr', 'li', 'option', 'td', 'th'):
            self._emit('\n')
        elif tag in ('tr', 'li', 'dt', 'dd', 'option') and self.parts and self.parts[-1] != '\n':
            self._emit('\n')

    def _tick(self):
        self.steps += 1
        if self.deadline and not self.steps % 512 and time.monotonic() > self.deadline:
            raise _OverBudget()

    def handle_endtag(self, tag):
        self._tick()
        if tag in ('html', 'body'):
            return  # content after </body> still belongs to the body
        if tag == 'head':
            self.head = False
            return
        if tag in _VOID_TAGS:
            if tag == 'br':
                self._emit('\n')  # `</br>` is read as a line break, as browsers do
            return
        stop = _SCOPE - {tag} if tag not in ('tr', 'tbody', 'thead', 'tfoot') else frozenset(('table', 'template'))
        index = self._find((tag,), stop)
        if index:
            self._pop_to(index)

    def _unseen(self, host=None):
        top = host or self.stack[-1]
        return top.hide or top.unseen or top.zero or top.details

    def _emit(self, separator):
        if not self.head and not self._unseen():
            self.parts.append(separator)

    def handle_data(self, data):
        if self.head and data.strip():
            self.head = False  # text cannot be in a head; the body has begun
        if self.head:
            return
        text = data if self.pre or self.stack[-1].tag == 'xmp' else _SPACE.sub(' ', data)
        host = self._host() if data.strip() else self.stack[-1]
        if self._unseen(host):
            if not self.code and self.hidden_size < HIDDEN_TEXT_LIMIT:
                self.hidden_parts.append(text)
                self.hidden_size += len(text)
        else:
            self.parts.append(text)

    def close(self):
        super().close()
        # An inline hidden element (a span, a font) left open around block content
        # is broken markup: a browser would hide the rest of the mail by accident.
        self.confused = next((element.ordinal for element in self.stack if element.origin
                              and element.tag in _INLINE and element.blocks), None)


def _tidy(text):
    text = re.sub(r' *\t[ \t]*', '\t', text)
    text = re.sub(r'[ \t]*\n[ \t]*', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def _strip_tags(value):
    """Linear last resort: every text run, tags removed. Nothing is judged hidden."""
    import html as entities
    value = re.sub(r'<!--.*?(?:-->|$)', ' ', value, flags=re.S)
    value = re.sub(r'<(?:br|/p|/div|/tr|/li|/h[1-6])\b[^>]*>', '\n', value, flags=re.I)
    value = re.sub(r'<[^>]*>', ' ', value)
    return _tidy(re.sub(r'[ \t\r\f\v]+', ' ', entities.unescape(value)))


def _html_parts(value):
    """(visible text, hidden text) of an HTML body, both plain text."""
    value = str(value or '')[:MAX_HTML]
    value = re.sub(r'<!--(?:->|>)', '<!---->', value)  # `<!-->` and `<!--->` are empty comments
    value = value.replace('--!>', '-->')
    unclosed = value.rfind('<!--')
    if unclosed != -1 and value.find('-->', unclosed + 4) == -1:
        value = value[:unclosed]  # a comment never closed runs to the end (as on every Python version)
    deadline = time.monotonic() + PARSE_BUDGET_SECONDS

    def read(exempt=()):
        parser = _ReadableHTML(exempt, deadline)
        parser.feed(value)
        parser.close()
        return _tidy(''.join(parser.parts)), _tidy(''.join(parser.hidden_parts)), parser.confused
    try:
        visible, hidden, confused = read()
        if not visible and confused is not None:
            # Nothing else is visible and the markup is broken: read that one element,
            # rather than refusing mail a person can plainly read.
            visible, hidden, _ = read((confused,))
    except _OverBudget:
        # Pathological markup: keep every word rather than stall the mailbox.
        _LOG.warning('HTML mail took longer than %.1f s to read; used the plain tag-stripping reader.', PARSE_BUDGET_SECONDS)
        return _strip_tags(value), ''
    return visible, hidden[:HIDDEN_TEXT_LIMIT]


def _plain_html(value):
    # Nothing visible (image-only mail, or text that is all hidden) is ''. The
    # mail is refused as unreadable rather than drafted from hidden text.
    return _html_parts(value)[0]


class MessageImportError(HarnessError):
    """A single message must be retained for review without generating a draft."""


def _bounded_body(body):
    text = str(body or '').strip()
    if len(text) > 200_000:
        raise MessageImportError('The email text exceeds the 200,000 character processing limit. No draft was generated.')
    return text or '[This email has no readable text body. Attachments were not imported.]'


def _mail_header(value, limit):
    try:
        text = str(make_header(decode_header(str(value or ''))))
    except (LookupError, ValueError):
        text = str(value or '')
    return re.sub(r'[\r\n]+', ' ', text).strip()[:limit]


def _single_reply_to(value):
    text = _mail_header(value, MAX_MAIL)
    try:
        addresses = getaddresses([text]) if text else []
    except ValueError:
        addresses = []
    if len(text) > 500 or (text and (len(addresses) != 1 or not addresses[0][1])):
        raise MessageImportError('This message has multiple or unsupported Reply-To recipients. Review it in the mailbox; no draft was generated.')
    return text


def _gmail_body(payload, hidden=None):
    """The readable text of a Gmail payload. Text an HTML body hides from view
    is appended to `hidden` (when given), never to the body."""
    plain, html = [], []
    pending = [(payload, 0)]
    visited = 0
    while pending and visited < 200:
        part, depth = pending.pop(0)
        visited += 1
        if not isinstance(part, dict):
            continue
        headers = {str(h.get('name', '')).lower(): str(h.get('value', '')) for h in part.get('headers', []) if isinstance(h, dict)}
        if part.get('filename') or headers.get('content-disposition', '').lower().startswith('attachment'):
            continue
        if depth > 20:
            raise MessageImportError('The email MIME structure exceeds the processing depth limit. No draft was generated.')
        body = part.get('body') or {}
        mime = str(part.get('mimeType') or '').lower()
        if body.get('attachmentId'):
            if mime in ('text/plain', 'text/html'):
                raise MessageImportError('The email text requires a separate body download. No draft was generated.')
            # Never fetch attachments, including inline images and attached mail.
            continue
        if mime in ('text/plain', 'text/html') and body.get('data'):
            encoded = body['data']
            if not isinstance(encoded, str) or len(encoded) > MAX_MAIL * 2:
                raise MessageImportError('Gmail returned an oversized message body. No draft was generated.')
            try:
                raw = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True)
                charset = re.search(r'charset=["\']?([^;"\' ]+)', headers.get('content-type', ''), re.I)
                text = raw.decode(charset.group(1) if charset else 'utf-8', errors='replace')
            except (ValueError, LookupError):
                raise MessageImportError('Gmail returned an invalid message body. No draft was generated.') from None
            (plain if mime == 'text/plain' else html).append(text)
        elif mime.startswith('multipart/'):
            parts = part.get('parts', [])
            if len(parts) > 200:
                raise MessageImportError('The email has too many MIME parts to process completely. No draft was generated.')
            pending.extend((child, depth + 1) for child in parts)
    if pending:
        raise MessageImportError('The email has too many MIME parts to process completely. No draft was generated.')
    if plain:
        return _bounded_body('\n'.join(plain))
    visible, unseen = _html_parts('\n'.join(html))
    if hidden is not None and unseen:
        hidden.append(unseen)
    return _bounded_body(visible)


class EmailConnectors:
    def __init__(self, data_dir, secret_store, registrations=None, transport=None, browser_open=None):
        self.root = Path(data_dir).resolve() / 'oauth'
        self.root.mkdir(parents=True, exist_ok=True)
        self.secrets = secret_store
        self.registrations = dict(registrations or {})
        self.transport = transport or _transport
        self.browser_open = browser_open or webbrowser.open
        self.sessions = {}
        with _STORE_LOCKS_GUARD:
            self.lock = _STORE_LOCKS.setdefault(str(self.root), threading.RLock())
        self._local = threading.local()
        self.closed = False

    @contextmanager
    def _mutation(self):
        with self.lock:
            if getattr(self._local, 'depth', 0):
                self._local.depth += 1
                try:
                    yield
                finally:
                    self._local.depth -= 1
                return
            with (self.root / 'mutation.lock').open('a+b') as handle:
                handle.seek(0, 2)
                if not handle.tell():
                    handle.write(b'0')
                    handle.flush()
                if os.name == 'nt':
                    import msvcrt
                    while True:
                        try:
                            handle.seek(0)
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                            break
                        except OSError:
                            time.sleep(0.03)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                self._local.depth = 1
                try:
                    yield
                finally:
                    self._local.depth = 0
                    if os.name == 'nt':
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _registration_current(self, provider):
        marker = self.root / 'registration-bindings.json'
        if not marker.exists():
            return True
        try:
            return json.loads(marker.read_text())[provider] == self._fingerprint(provider)
        except (OSError, ValueError, KeyError):
            return False

    def update_registrations(self, registrations):
        with self._mutation():
            self.registrations = dict(registrations or {})
            marker = self.root / 'registration-bindings.json'
            temporary = marker.with_suffix('.tmp')
            temporary.write_text(json.dumps({provider: self._fingerprint(provider) for provider in PROVIDERS}))
            temporary.replace(marker)
            # A callback cannot complete under a registration different from
            # the one which supplied its PKCE authorization request.
            for session in self.sessions.values():
                if session['state'] == 'pending' and session['config_fingerprint'] != self._fingerprint(session['provider']):
                    session.update(state='error', error='The sign-in registration changed. Start sign-in again.')

    def _registration(self, provider):
        if provider not in PROVIDERS:
            raise HarnessError('Choose Outlook or Gmail.')
        value = self.registrations.get(provider) or {}
        if not isinstance(value, dict):
            raise HarnessError('Mail sign-in registration is invalid.')
        client_id = str(value.get('client_id') or '').strip()
        tenant = str(value.get('tenant') or 'common').strip()
        if not re.fullmatch(r'[A-Za-z0-9._-]{1,200}', tenant):
            raise HarnessError('Microsoft tenant must be a tenant ID or domain.')
        if client_id and not re.fullmatch(r'[A-Za-z0-9._-]{1,500}', client_id):
            raise HarnessError('Mail client registration ID is invalid.')
        return {**value, 'client_id': client_id, 'tenant': tenant}

    def _fingerprint(self, provider):
        registration = self._registration(provider)
        return _hash({'contract': CONTRACT, 'provider': provider, 'client_id': registration['client_id'],
                      'tenant': registration['tenant'] if provider == 'outlook' else '', 'scopes': PROVIDERS[provider]['scopes']})

    def registration_status(self):
        return [{'provider': provider, 'name': details['name'], 'configured': bool(self._registration(provider)['client_id']),
                 'setup_url': details['setup_url'], 'config_fingerprint': self._fingerprint(provider)} for provider, details in PROVIDERS.items()]

    def _path(self, connection_id):
        if not re.fullmatch(r'[0-9a-f]{32}', str(connection_id)):
            raise HarnessError('Unknown mail connection.')
        return self.root / (connection_id + '.json')

    def _save(self, value):
        with self._mutation():
            self._save_locked(value)

    def _save_locked(self, value):
        target = self._path(value['id'])
        temporary = target.with_suffix('.' + uuid.uuid4().hex + '.tmp')
        sealed = self.secrets.protect(json.dumps(value))
        temporary.write_text(json.dumps({'schema_version': SCHEMA_VERSION, 'sealed': sealed}), encoding='utf-8')
        temporary.replace(target)

    def _load(self, identity):
        try:
            envelope = json.loads(self._path(identity).read_text(encoding='utf-8'))
            if envelope.get('schema_version') != SCHEMA_VERSION:
                raise ValueError('version')
            value = json.loads(self.secrets.unprotect(envelope['sealed']))
            if value.get('id') != identity or value.get('contract') != CONTRACT:
                raise ValueError('binding')
            return value
        except (OSError, ValueError, KeyError):
            raise HarnessError('This mail connection is unavailable. Connect the account again.') from None

    def _public(self, value):
        return {key: value.get(key, '') for key in ('id', 'provider', 'email', 'name', 'state', 'config_fingerprint')}

    def connection(self, identity):
        with self._mutation():
            value = self._load(identity)
            if value['config_fingerprint'] != self._fingerprint(value['provider']) or not self._registration_current(value['provider']):
                value = {**value, 'state': 'reconnect_required'}
            return self._public(value)

    def disconnect(self, identity):
        with self._mutation():
            value = self._load(identity)
            value.update(state='disconnected', access_token='', refresh_token='', expires_at=0, credential_epoch=uuid.uuid4().hex)
            self._save(value)
            return self._public(value)

    def _endpoints(self, provider):
        if provider == 'outlook':
            base = 'https://login.microsoftonline.com/' + self._registration(provider)['tenant'] + '/oauth2/v2.0/'
            return base + 'authorize', base + 'token'
        return 'https://accounts.google.com/o/oauth2/v2/auth', 'https://oauth2.googleapis.com/token'

    def _json_request(self, method, url, headers=None, body=None):
        try:
            raw = self.transport(method, url, headers or {}, body)
            if isinstance(raw, dict):  # Injectable transport for isolated tests.
                return raw
            if len(raw) > MAX_RESPONSE:
                raise HarnessError('The mail provider response exceeded the supported size.')
            value = json.loads(raw) if raw else {}
            if not isinstance(value, dict):
                raise ValueError('object expected')
            return value
        except (ValueError, TypeError):
            raise HarnessError('The mail provider returned an invalid response.') from None

    def _token_request(self, provider, fields):
        registration = self._registration(provider)
        fields = {**fields, 'client_id': registration['client_id']}
        if provider == 'gmail' and registration.get('client_secret'):
            # Google desktop registrations may issue this value. It is not
            # treated as proof of client identity; PKCE remains mandatory.
            fields['client_secret'] = registration['client_secret']
        return self._json_request('POST', self._endpoints(provider)[1], {'Content-Type': 'application/x-www-form-urlencoded'}, urllib.parse.urlencode(fields).encode())

    def begin(self, provider, account_id=''):
        with self._mutation():
            if self.closed:
                raise HarnessError('Mail sign-in is shutting down.')
            registration = self._registration(provider)
            if not registration['client_id']:
                raise HarnessError('A registered ' + PROVIDERS[provider]['name'] + ' desktop application client ID is required. Use manual setup until an administrator configures sign-in.')
            expected = self._load(account_id) if account_id else None
            if expected and expected['provider'] != provider:
                raise HarnessError('Reconnect using the original mail provider.')
            for identity, old in list(self.sessions.items()):
                if old['state'] != 'pending' and time.time() > old['expires_at']:
                    self.sessions.pop(identity, None)
            if len(self.sessions) >= 32:
                completed = [identity for identity, old in self.sessions.items() if old['state'] != 'pending']
                for identity in completed[:len(self.sessions) - 31]:
                    self.sessions.pop(identity, None)
            if sum(s['state'] == 'pending' for s in self.sessions.values()) >= 4:
                raise HarnessError('Finish or cancel an existing mail sign-in first.')
            session_id = uuid.uuid4().hex
            session = dict(id=session_id, provider=provider, connection_id=account_id, expected_email=(expected or {}).get('email', ''),
                           expected_epoch=(expected or {}).get('credential_epoch', ''), state='pending', error='', connection=None, expires_at=time.time() + SESSION_SECONDS,
                           state_token=secrets.token_urlsafe(32), verifier=secrets.token_urlsafe(64), config_fingerprint=self._fingerprint(provider))
            owner = self
            class Callback(BaseHTTPRequestHandler):
                def setup(self):
                    self.request.settimeout(3)
                    super().setup()
                def log_message(self, *args):
                    pass
                def do_GET(self):
                    if len(self.path) > 12_000:
                        self.send_error(400)
                        return
                    parsed = urllib.parse.urlsplit(self.path)
                    if parsed.path != '/callback':
                        self.send_error(404)
                        return
                    if self.headers.get('Host') not in ('127.0.0.1:' + str(self.server.server_port), 'localhost:' + str(self.server.server_port)):
                        self.send_error(400)
                        return
                    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
                    try:
                        owner._complete(session_id, query)
                        content = b'Sign-in finished. You can close this window and return to Nexus Harness.'
                        status = 200
                    except HarnessError:
                        content = b'Sign-in could not finish. Return to Nexus Harness for details.'
                        status = 400
                    self.send_response(status)
                    self.send_header('Content-Type', 'text/plain; charset=utf-8')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Content-Length', str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
            server = HTTPServer(('127.0.0.1', 0), Callback)
            server.timeout = 0.25
            redirect_host = 'localhost' if provider == 'outlook' else '127.0.0.1'
            session['redirect_uri'] = f'http://{redirect_host}:{server.server_port}/callback'
            session['server'] = server
            self.sessions[session_id] = session
            fields = {'client_id': registration['client_id'], 'redirect_uri': session['redirect_uri'], 'response_type': 'code',
                      'scope': PROVIDERS[provider]['scopes'], 'state': session['state_token'],
                      'code_challenge': _b64(hashlib.sha256(session['verifier'].encode()).digest()), 'code_challenge_method': 'S256'}
            if provider == 'gmail':
                fields.update(access_type='offline', prompt='consent')
            else:
                fields['prompt'] = 'select_account'
            url = self._endpoints(provider)[0] + '?' + urllib.parse.urlencode(fields)
            def listen():
                try:
                    while not self.closed and session['state'] == 'pending' and time.time() < session['expires_at']:
                        server.handle_request()
                    if session['state'] == 'pending':
                        session.update(state='expired', error='Sign-in expired. Start again.')
                finally:
                    server.server_close()
                    session.pop('verifier', None)
                    session.pop('state_token', None)
            thread = threading.Thread(target=listen, name='email-oauth-callback', daemon=True)
            session['thread'] = thread
            thread.start()
        try:
            opened = self.browser_open(url)
        except Exception:
            opened = False
        return {'session_id': session_id, 'authorization_url': url, 'expires_at': session['expires_at'], 'browser_opened': bool(opened)}

    def _complete(self, session_id, query):
        with self._mutation():
            session = self.sessions.get(session_id)
            if not session or session['state'] != 'pending':
                raise HarnessError('This sign-in is no longer pending.')
            if time.time() >= session['expires_at']:
                session.update(state='expired', error='Sign-in expired. Start again.')
                raise HarnessError(session['error'])
            supplied = query.get('state', [])
            if len(supplied) != 1 or not hmac.compare_digest(str(supplied[0]), session['state_token']):
                # An unrelated local request must not consume the real session.
                raise HarnessError('Sign-in state did not match.')
            if session['config_fingerprint'] != self._fingerprint(session['provider']) or not self._registration_current(session['provider']):
                session.update(state='error', error='Sign-in registration changed. Start again.')
                raise HarnessError(session['error'])
            if session['connection_id']:
                current = self._load(session['connection_id'])
                if current.get('credential_epoch', '') != session['expected_epoch']:
                    session.update(state='error', error='The mailbox connection changed while sign-in was open. Start again.')
                    raise HarnessError(session['error'])
            if query.get('error'):
                session.update(state='error', error='Sign-in was denied or cancelled. Start again when ready.')
                raise HarnessError(session['error'])
            codes = query.get('code', [])
            if len(codes) != 1 or not codes[0] or len(codes[0]) > 8000:
                raise HarnessError('No valid authorization code was returned.')
            try:
                tokens = self._token_request(session['provider'], {'grant_type': 'authorization_code', 'code': codes[0], 'code_verifier': session['verifier'], 'redirect_uri': session['redirect_uri']})
                value = {'id': session['connection_id'] or uuid.uuid4().hex, 'provider': session['provider'], 'contract': CONTRACT,
                         'config_fingerprint': session['config_fingerprint'], 'state': 'connected', 'credential_epoch': uuid.uuid4().hex}
                self._apply_tokens(value, tokens)
                profile_url = 'https://graph.microsoft.com/v1.0/me?$select=mail,userPrincipalName,displayName' if value['provider'] == 'outlook' else 'https://gmail.googleapis.com/gmail/v1/users/me/profile'
                profile = self._json_request('GET', profile_url, {'Authorization': 'Bearer ' + value['access_token']})
                email = str(profile.get('mail') or profile.get('userPrincipalName') or profile.get('emailAddress') or '').strip()
                if not re.fullmatch(r'[^\s@<>]+@[^\s@<>]+', email):
                    raise HarnessError('The provider did not identify an email account.')
                if session['expected_email'] and email.casefold() != session['expected_email'].casefold():
                    raise HarnessError('You signed in to a different mailbox. Reconnect with the original email address or add a separate account.')
                value.update(email=email, name=str(profile.get('displayName') or email)[:100])
                if not value.get('refresh_token'):
                    raise HarnessError('Offline mailbox access was not granted. Sign in again and approve the requested permissions.')
                self._save(value)
                session.update(state='connected', connection=self._public(value), error='')
            except Exception as exc:
                reason = str(exc) if isinstance(exc, HarnessError) else 'Sign-in could not finish. Check account permissions and try again.'
                session.update(state='error', error=reason)
                raise HarnessError(reason) from None

    def status(self, session_id):
        with self._mutation():
            session = self.sessions.get(session_id)
            if not session:
                raise HarnessError('Unknown sign-in session. Start sign-in again.')
            if session['state'] == 'pending' and time.time() >= session['expires_at']:
                session.update(state='expired', error='Sign-in expired. Start again.')
            return {key: session.get(key) for key in ('state', 'connection', 'error', 'expires_at')}

    def _apply_tokens(self, value, tokens):
        access = str(tokens.get('access_token') or '')
        if not access or len(access) > 100_000:
            raise HarnessError('The provider did not return a usable access token.')
        value['access_token'] = access
        value['refresh_token'] = str(tokens.get('refresh_token') or value.get('refresh_token') or '')
        try:
            value['expires_at'] = time.time() + max(1, min(int(tokens.get('expires_in', 3600)), 86400))
        except (TypeError, ValueError):
            raise HarnessError('The provider returned an invalid token lifetime.') from None

    def _ready(self, identity, force_refresh=False):
        with self._mutation():
            return self._ready_locked(identity, force_refresh)

    def _ready_locked(self, identity, force_refresh=False):
        value = self._load(identity)
        if value['state'] != 'connected' or value['config_fingerprint'] != self._fingerprint(value['provider']) or not self._registration_current(value['provider']):
            raise HarnessError('Reconnect this mailbox before using it.')
        if force_refresh or time.time() + 60 >= value['expires_at']:
            try:
                fields = {'grant_type': 'refresh_token', 'refresh_token': value['refresh_token']}
                if value['provider'] == 'outlook':
                    fields['scope'] = PROVIDERS['outlook']['scopes']
                self._apply_tokens(value, self._token_request(value['provider'], fields))
                self._save(value)
            except ConnectorHTTPError as exc:
                if exc.status in (400, 401, 403):
                    value['state'] = 'reconnect_required'
                    self._save(value)
                raise
        return value

    def _api(self, value, method, url, *, body=None, content_type=None, raw=False, extra_headers=None):
        provider = value['provider']
        parsed = urllib.parse.urlsplit(url)
        expected = 'graph.microsoft.com' if provider == 'outlook' else 'gmail.googleapis.com'
        if parsed.scheme != 'https' or parsed.hostname != expected or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.fragment:
            raise HarnessError('The provider returned an unsafe continuation URL.')
        headers = {'Authorization': 'Bearer ' + value['access_token'], **(extra_headers or {})}
        if content_type:
            headers['Content-Type'] = content_type
        if raw:
            result = self.transport(method, url, headers, body)
            if not isinstance(result, bytes) or len(result) > MAX_MAIL:
                raise HarnessError('Email exceeds the supported size.')
            return result
        return self._json_request(method, url, headers, body)

    def sync(self, identity, cursor=''):
        with self._mutation():
            value = self._ready(identity)
            try:
                return self._sync(value, cursor)
            except ConnectorHTTPError as exc:
                if exc.status != 401:
                    raise
                return self._sync(self._ready(identity, True), cursor)

    def _cursor(self, value, payload):
        return json.dumps({'version': SCHEMA_VERSION, 'connection': value['id'], 'fingerprint': value['config_fingerprint'], **payload}, separators=(',', ':'))

    def _sync(self, value, cursor):
        held = {}
        if cursor:
            try:
                if len(cursor) > 32_000:
                    raise ValueError('size')
                held = json.loads(cursor)
                if held.get('version') != SCHEMA_VERSION or held.get('connection') != value['id'] or held.get('fingerprint') != value['config_fingerprint']:
                    raise ValueError('binding')
            except (TypeError, ValueError, AttributeError):
                raise HarnessError('The mailbox sync cursor is stale. Reset sync for this account.') from None
        if value['provider'] == 'outlook':
            base = 'https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta'
            url = held.get('next') or base + '?$select=id&$top=25'
            parsed = urllib.parse.urlsplit(url)
            if parsed.path.lower() != '/v1.0/me/mailfolders/inbox/messages/delta':
                raise HarnessError('The mailbox continuation does not belong to the inbox.')
            try:
                result = self._api(value, 'GET', url)
            except ConnectorHTTPError as exc:
                if exc.status == 410 and held:
                    return self._sync(value, '')
                raise
            messages, warnings, failed_messages = [], [], []
            items = result.get('value', [])
            if not isinstance(items, list) or len(items) > 250:
                raise HarnessError('The mailbox page exceeded the supported size.')
            for item in items:
                if '@removed' in item:
                    continue
                message_id = str(item.get('id') or '')
                if not message_id or len(message_id) > 2000:
                    raise HarnessError('The mailbox returned an invalid message ID.')
                try:
                    message = self._api(value, 'GET', 'https://graph.microsoft.com/v1.0/me/messages/' + urllib.parse.quote(message_id, safe='') + '?$select=id,from,replyTo,subject,body,internetMessageId,conversationId,receivedDateTime', extra_headers={'Prefer': 'outlook.body-content-type="text"'})
                except ConnectorHTTPError as exc:
                    if exc.status in (404, 410):
                        warnings.append('A message was removed or moved before it could be imported.')
                        failed_messages.append({'source_id': message_id, 'error': 'The message moved or is no longer available. No draft was generated.'})
                        continue
                    raise
                body = message.get('body') or {}
                content, hidden_text = str(body.get('content') or ''), ''
                if str(body.get('contentType') or '').lower() == 'html':
                    content, hidden_text = _html_parts(content)
                sender = (message.get('from') or {}).get('emailAddress') or {}
                try:
                    reply_addresses = message.get('replyTo') or []
                    if len(reply_addresses) > 1:
                        raise MessageImportError('This message has multiple Reply-To recipients. Review it in the mailbox; no draft was generated.')
                    reply_to = _single_reply_to((reply_addresses or [{}])[0].get('emailAddress', {}).get('address', ''))
                    content = _bounded_body(content)
                except MessageImportError as exc:
                    failed_messages.append({'source_id': message_id, 'error': str(exc)})
                    warnings.append(str(exc))
                    continue
                messages.append({'source_id': message_id, 'sender': _mail_header(sender.get('address'), 500), 'subject': _mail_header(message.get('subject'), 1000), 'body': content,
                                 'reply_to': reply_to, 'internet_message_id': _mail_header(message.get('internetMessageId'), 1000),
                                 'thread_id': str(message.get('conversationId') or ''), 'received_at': str(message.get('receivedDateTime') or ''),
                                 **({'hidden_text': hidden_text} if hidden_text else {})})
            next_url = result.get('@odata.nextLink') or result.get('@odata.deltaLink')
            if not isinstance(next_url, str) or not next_url:
                raise HarnessError('The mailbox did not return a continuation cursor.')
            # Validate before persisting a provider-supplied credential target.
            parsed = urllib.parse.urlsplit(next_url)
            if parsed.scheme != 'https' or parsed.netloc != 'graph.microsoft.com' or parsed.path.lower() != '/v1.0/me/mailfolders/inbox/messages/delta':
                raise HarnessError('The provider returned an unsafe continuation URL.')
            return {'messages': messages, 'cursor': self._cursor(value, {'next': next_url}), 'has_more': bool(result.get('@odata.nextLink')), 'warnings': list(dict.fromkeys(warnings)), 'failed_messages': failed_messages}
        base = 'https://gmail.googleapis.com/gmail/v1/users/me/'
        if not held:
            profile = self._api(value, 'GET', base + 'profile')
            held = {'mode': 'initial', 'history': str(profile.get('historyId') or '')}
            if not held['history']:
                raise HarnessError('Gmail did not return a sync history cursor.')
        params = {'maxResults': 25}
        if held.get('page'):
            params['pageToken'] = held['page']
        if held.get('mode') == 'initial':
            params['labelIds'] = 'INBOX'
            result = self._api(value, 'GET', base + 'messages?' + urllib.parse.urlencode(params))
            ids = [str(item.get('id') or '') for item in result.get('messages', [])]
        elif held.get('mode') == 'history':
            params.update(startHistoryId=held['history'], historyTypes='messageAdded', labelId='INBOX')
            try:
                result = self._api(value, 'GET', base + 'history?' + urllib.parse.urlencode(params))
            except ConnectorHTTPError as exc:
                if exc.status == 404:
                    return self._sync(value, '')  # Expired history: deduped full sync.
                raise
            ids = list(dict.fromkeys(str(item.get('message', {}).get('id') or '') for history in result.get('history', []) for item in history.get('messagesAdded', [])))
        else:
            raise HarnessError('Unsupported Gmail sync cursor.')
        if len(ids) > 250 or any(not identity or len(identity) > 2000 for identity in ids):
            raise HarnessError('Gmail returned an invalid or oversized page.')
        messages, warnings, failed_messages = [], [], []
        for message_id in ids:
            try:
                item = self._api(value, 'GET', base + 'messages/' + urllib.parse.quote(message_id, safe='') + '?format=full')
            except ConnectorHTTPError as exc:
                if exc.status in (404, 410):
                    warnings.append('A message was removed before it could be imported.')
                    failed_messages.append({'source_id': message_id, 'error': 'The message moved or is no longer available. No draft was generated.'})
                    continue
                raise
            payload = item.get('payload') or {}
            headers = {str(h.get('name', '')).lower(): h.get('value', '') for h in payload.get('headers', []) if isinstance(h, dict)}
            from datetime import datetime, timezone
            try:
                received = datetime.fromtimestamp(int(item.get('internalDate', 0)) / 1000, timezone.utc).isoformat() if item.get('internalDate') else ''
            except (ValueError, OverflowError, OSError):
                received = ''
            hidden = []
            try:
                content = _gmail_body(payload, hidden)
                reply_to = _single_reply_to(headers.get('reply-to'))
            except MessageImportError as exc:
                failed_messages.append({'source_id': message_id, 'error': str(exc)})
                warnings.append(str(exc))
                continue
            messages.append({'source_id': message_id, 'sender': _mail_header(headers.get('from'), 500), 'subject': _mail_header(headers.get('subject'), 1000), 'body': content,
                             'reply_to': reply_to, 'internet_message_id': _mail_header(headers.get('message-id'), 1000),
                             'references': _mail_header(headers.get('references'), 4000), 'thread_id': str(item.get('threadId') or ''), 'received_at': received,
                             **({'hidden_text': '\n'.join(hidden)} if hidden else {})})
        next_page = result.get('nextPageToken', '')
        history = held['history'] if next_page or held['mode'] == 'initial' else str(result.get('historyId') or held['history'])
        mode = held['mode'] if next_page else 'history'
        return {'messages': messages, 'cursor': self._cursor(value, {'mode': mode, 'history': history, 'page': next_page}), 'has_more': bool(next_page), 'warnings': list(dict.fromkeys(warnings)), 'failed_messages': failed_messages}

    def send(self, identity, mail, *, thread_id=''):
        with self._mutation():
            value = self._ready(identity)
            if not isinstance(mail, EmailMessage):
                raise HarnessError('Expected a reviewed email message.')
            if str(mail.get('From') or '').strip().casefold() != value['email'].casefold():
                raise HarnessError('The reply sender does not match the connected mailbox.')
            raw = mail.as_bytes(policy=policy.SMTP)
            if len(raw) > MAX_MAIL:
                raise HarnessError('This reply exceeds the supported size.')
            # Deliberately no retry, including 401: delivery can be ambiguous.
            if value['provider'] == 'outlook':
                self._api(value, 'POST', 'https://graph.microsoft.com/v1.0/me/sendMail', body=base64.b64encode(raw), content_type='text/plain')
                return {'accepted': True}
            payload = {'raw': _b64(raw)}
            if thread_id:
                payload['threadId'] = str(thread_id)
            result = self._api(value, 'POST', 'https://gmail.googleapis.com/gmail/v1/users/me/messages/send', body=json.dumps(payload).encode(), content_type='application/json')
            return {'accepted': True, 'id': str(result.get('id') or '')}

    def close(self):
        self.closed = True
        threads = []
        with self._mutation():
            for session in self.sessions.values():
                if session['state'] == 'pending':
                    session.update(state='expired', error='Sign-in stopped. Start again after reopening Nexus.')
                if session.get('thread'):
                    threads.append(session['thread'])
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=4)
