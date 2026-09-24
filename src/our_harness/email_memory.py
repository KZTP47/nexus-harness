"""Private, attributed mail retrieval and explicit preference history.

FTS5 is lexical search, not an embedding model. Callers may supply multiple
search phrases (including model-generated paraphrases) without changing scope.
Mail documents are a derived index; the studio's canonical records own them.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from email.utils import getaddresses
from pathlib import Path

SCHEMA_VERSION = 1
CONTRACT = 'email-memory/fts5-unicode61-v1'
AUTOMATIC_CONTRACT = 'email-recipient-learning/v1'

def canonical_recipient(value):
    """An unambiguous mailbox identity; never infer groups or alias equivalence."""
    value = str(value or '')
    if not value or any(c in value for c in '\r\n;'):
        return ''
    value = value.strip()
    # A decoded display name such as `Müller, Hans <h@x.de>` keeps an unquoted
    # comma. With no '@', quote, ':' or ';' before the one angle address, the
    # display part names no other mailbox and the angle address is exact.
    decoded = re.fullmatch(r'[^<>@":;]*,[^<>@":;]*<([^<>]+)>', value)
    if decoded:
        address = decoded[1].strip()
        return address.casefold() if re.fullmatch(r'[^\s@<>,:"]+@[^\s@<>,:"]+', address) else ''
    envelope = re.fullmatch(r'(?:[^<>]*<([^<>]+)>|([^<>]+))', value)
    if not envelope:
        return ''
    candidate = (envelope[1] or envelope[2]).strip()
    addresses = getaddresses([value])
    if len(addresses) != 1:
        return ''
    address = addresses[0][1]
    if address != candidate or not re.fullmatch(r'[^\s@<>,:]+@[^\s@<>,:]+', address):
        return ''
    return address.casefold()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class EmailMemory:
    def __init__(self, root, *, contract=CONTRACT):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'email-memory.sqlite3'
        self.contract = str(contract)
        with self._db() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise ValueError('Unsupported email memory schema; canonical mail remains unchanged.')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS documents(
                    row_id INTEGER PRIMARY KEY, account TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    source_id TEXT NOT NULL, sender TEXT NOT NULL, subject TEXT NOT NULL,
                    body TEXT NOT NULL, document TEXT NOT NULL,
                    UNIQUE(account,fingerprint,source_id));
                CREATE VIRTUAL TABLE IF NOT EXISTS mail_fts USING fts5(subject,body,sender,
                    tokenize='unicode61 remove_diacritics 2');
                CREATE TABLE IF NOT EXISTS preferences(
                    account TEXT NOT NULL,id TEXT NOT NULL,revision INTEGER NOT NULL,
                    text TEXT NOT NULL, scope TEXT NOT NULL,status TEXT NOT NULL,
                    record TEXT NOT NULL, PRIMARY KEY(account,id,revision));
                CREATE TABLE IF NOT EXISTS forgotten_preferences(
                    account TEXT NOT NULL,id TEXT NOT NULL,PRIMARY KEY(account,id));
            ''')
            signature = _digest([self.contract, SCHEMA_VERSION, str(self.root)])
            old = db.execute("SELECT value FROM metadata WHERE key='index_contract'").fetchone()
            if not old or old[0] != signature:
                db.execute('DELETE FROM mail_fts')
                db.execute('INSERT INTO mail_fts(rowid,subject,body,sender) SELECT row_id,subject,body,sender FROM documents')
                db.execute("INSERT OR REPLACE INTO metadata VALUES('index_contract',?)", (signature,))
            db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
            self._migrate_learned_authority(db)

    LEARNED_AUTHORITY_MIGRATION = 'learned-authority/v1'

    @classmethod
    def _migrate_learned_authority(cls, db):
        """Once: record where each preference came from before an edit made it 'user'.

        Earlier versions overwrote the authority of an edited approved-edit
        preference, so a sender-specific rule read as mailbox-wide. The first
        revision still holds the original authority; evidence of an approved
        draft is the fallback when that revision is missing.
        """
        if db.execute('SELECT 1 FROM metadata WHERE key=?', (cls.LEARNED_AUTHORITY_MIGRATION,)).fetchone():
            return
        for account, identity, revision, raw in db.execute(
                'SELECT account,id,revision,record FROM preferences').fetchall():
            record = json.loads(raw)
            if 'learned_authority' in record:
                continue
            first = db.execute('SELECT record FROM preferences WHERE account=? AND id=? AND revision=1',
                               (account, identity)).fetchone()
            origin = json.loads(first[0]).get('authority') if first else None
            if origin not in ('user', 'approved_edit'):
                evidence = record.get('evidence') if isinstance(record.get('evidence'), dict) else {}
                learned = bool(evidence.get('message_id') or evidence.get('source_draft_id')
                               or (evidence.get('migration') and not str(record.get('source_draft_id', '')).startswith('legacy:')))
                origin = 'approved_edit' if learned else record.get('authority', 'user')
            record['learned_authority'] = origin
            db.execute('UPDATE preferences SET record=? WHERE account=? AND id=? AND revision=?',
                       (json.dumps(record), account, identity, revision))
        db.execute('INSERT OR REPLACE INTO metadata VALUES(?,?)', (cls.LEARNED_AUTHORITY_MIGRATION, _now()))

    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.create_function('canonical_recipient', 1, canonical_recipient)
        db.execute('PRAGMA secure_delete=ON')
        return _ClosingConnection(db)

    @staticmethod
    def _scope(account_id, fingerprint):
        if not account_id or not fingerprint:
            raise ValueError('Mail memory requires an account and configuration fingerprint.')
        return str(account_id), str(fingerprint)

    def _index(self, db, account_id, fingerprint, message):
        source_id = str(message.get('id') or message.get('source_id') or '')
        if not source_id:
            raise ValueError('A stable message ID is required.')
        # Allowlist attribution. Do not copy credentials or connector objects.
        document = {key: message[key] for key in (
            'id', 'source_id', 'sender', 'subject', 'body', 'received_at', 'thread_id',
            'conversation_id', 'sent_at', 'direction', 'delivery_status', 'reply_to') if key in message}
        document.update(id=source_id, account_id=account_id, account_fingerprint=fingerprint,
                        schema_version=SCHEMA_VERSION, source_type='mail')
        sender, subject, body = (str(document.get(k) or '') for k in ('sender', 'subject', 'body'))
        db.execute('''INSERT INTO documents(account,fingerprint,source_id,sender,subject,body,document)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(account,fingerprint,source_id) DO UPDATE SET
            sender=excluded.sender,subject=excluded.subject,body=excluded.body,document=excluded.document''',
            (account_id, fingerprint, source_id, sender, subject, body, json.dumps(document)))
        row_id = db.execute('SELECT row_id FROM documents WHERE account=? AND fingerprint=? AND source_id=?',
                            (account_id, fingerprint, source_id)).fetchone()[0]
        db.execute('DELETE FROM mail_fts WHERE rowid=?', (row_id,))
        db.execute('INSERT INTO mail_fts(rowid,subject,body,sender) VALUES(?,?,?,?)', (row_id, subject, body, sender))

    def index_message(self, account_id, fingerprint, message):
        account_id, fingerprint = self._scope(account_id, fingerprint)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._index(db, account_id, fingerprint, message)

    def sync_messages(self, account_id, fingerprint, messages):
        """Reconcile a complete canonical snapshot for this exact account version."""
        account_id, fingerprint = self._scope(account_id, fingerprint)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TEMP TABLE keep_ids(id TEXT PRIMARY KEY)')
            for message in messages:
                self._index(db, account_id, fingerprint, message)
                db.execute('INSERT OR IGNORE INTO keep_ids VALUES(?)', (str(message.get('id') or message.get('source_id')),))
            stale = db.execute('''SELECT row_id FROM documents WHERE account=? AND fingerprint=?
                AND source_id NOT IN (SELECT id FROM keep_ids)''', (account_id, fingerprint)).fetchall()
            for row in stale:
                db.execute('DELETE FROM mail_fts WHERE rowid=?', (row[0],))
                db.execute('DELETE FROM documents WHERE row_id=?', (row[0],))

    def context(self, account_id, fingerprint, query, sender='', limit=8, *, recipient=None):
        account_id, fingerprint = self._scope(account_id, fingerprint)
        limit = max(0, min(int(limit), 50))
        phrases = query if isinstance(query, (list, tuple)) else [query]
        tokens = list(dict.fromkeys(token.casefold() for phrase in phrases[:12]
                                   for token in re.findall(r'[^\W_]+', str(phrase or ''), re.UNICODE)))[:100]
        results = []
        recipient_filter = '' if recipient is None else " AND canonical_recipient(COALESCE(NULLIF(json_extract(d.document,'$.reply_to'),''),d.sender))=?"
        scope_values = () if recipient is None else (canonical_recipient(recipient) or '__no_recipient__',)
        with self._db() as db:
            if tokens and limit:
                match = ' OR '.join('"' + token + '"' for token in tokens)
                results = db.execute('''SELECT d.document FROM mail_fts JOIN documents d ON d.row_id=mail_fts.rowid
                    WHERE mail_fts MATCH ? AND d.account=? AND d.fingerprint=?
                    ''' + recipient_filter + ''' ORDER BY bm25(mail_fts,3.0,1.0,0.2), d.source_id LIMIT ?''',
                    (match, account_id, fingerprint, *scope_values, limit)).fetchall()
            docs = [json.loads(row[0]) for row in results]
            # Same-sender fallback supplements sparse search within the requested scope.
            if sender and len(docs) < limit:
                fallback = db.execute('''SELECT document FROM documents d WHERE account=? AND fingerprint=?
                    AND lower(sender)=lower(?) ''' + recipient_filter + ''' ORDER BY row_id DESC LIMIT ?''',
                    (account_id, fingerprint, str(sender), *scope_values, limit)).fetchall()
                seen = {doc['id'] for doc in docs}
                docs.extend(json.loads(row[0]) for row in fallback if json.loads(row[0])['id'] not in seen)
            return docs[:limit]

    def forget_message(self, account_id, fingerprint, message_id):
        account_id, fingerprint = self._scope(account_id, fingerprint)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT row_id FROM documents WHERE account=? AND fingerprint=? AND source_id=?',
                              (account_id, fingerprint, str(message_id))).fetchall()
            for row in rows:
                db.execute('DELETE FROM mail_fts WHERE rowid=?', (row[0],))
                db.execute('DELETE FROM documents WHERE row_id=?', (row[0],))

    def preferences(self, account_id):
        with self._db() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT record FROM preferences WHERE account=? AND status='active' ORDER BY rowid", (str(account_id),))]

    def preference_history(self, account_id, preference_id):
        with self._db() as db:
            return [json.loads(row[0]) for row in db.execute(
                'SELECT record FROM preferences WHERE account=? AND id=? ORDER BY revision', (str(account_id), str(preference_id)))]

    def _supersede(self, db, account_id, preference_id):
        rows = db.execute("SELECT revision,record FROM preferences WHERE account=? AND id=? AND status='active'",
                          (account_id, preference_id)).fetchall()
        for row in rows:
            record = json.loads(row['record'])
            record['status'] = 'superseded'
            db.execute("UPDATE preferences SET status='superseded',record=? WHERE account=? AND id=? AND revision=?",
                       (json.dumps(record), account_id, preference_id, row['revision']))
        return bool(rows)

    def learn(self, account_id, source_draft_id, revision, text, scope='global', *, supersedes=None, evidence=None, confidence=None, authority='approved_edit', recipient='', account_fingerprint='', learning_mode='', category=''):
        """Persist caller-approved extraction; this method does not infer preferences.

        Contradictions require an explicit supersedes ID or user edit. A replay of
        identical source revision/text is idempotent, including after supersession.
        """
        account_id, text = str(account_id), str(text).strip()
        if not account_id or not source_draft_id or not text or len(text) > 20000 or authority not in ('user', 'approved_edit'):
            raise ValueError('A bounded preference and attributed source are required.')
        if recipient:
            recipient = canonical_recipient(recipient)
            if not recipient or not account_fingerprint:
                raise ValueError('Recipient learning requires an exact mailbox and configuration fingerprint.')
            scope = 'recipient:' + recipient
        if learning_mode == 'automatic' and not recipient:
            raise ValueError('Automatic learning requires a recipient.')
        preference_id = _digest([account_id, str(source_draft_id), int(revision), text, scope, account_fingerprint] if recipient else [account_id, str(source_draft_id), int(revision), text, scope])[:32]
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM forgotten_preferences WHERE account=? AND id=?',
                          (account_id, preference_id)).fetchone():
                return dict(id=preference_id, account_id=account_id, status='forgotten', text='')
            prior = db.execute('SELECT record FROM preferences WHERE account=? AND id=? ORDER BY revision DESC LIMIT 1',
                               (account_id, preference_id)).fetchone()
            if prior:
                return json.loads(prior[0])
            if learning_mode == 'automatic':
                # Repeated instructions refresh one category for this exact mailbox.
                for row in db.execute("SELECT record FROM preferences WHERE account=? AND status='active'", (account_id,)).fetchall():
                    prior_record = json.loads(row[0])
                    if (prior_record.get('learning_mode') == 'automatic'
                            and prior_record.get('recipient') == recipient
                            and prior_record.get('account_fingerprint') == account_fingerprint
                            and prior_record.get('category') == category and category != 'other'
                            and prior_record.get('authority') != 'user'
                            and not prior_record.get('user_edited')):
                        self._supersede(db, account_id, prior_record['id'])
            if supersedes and not self._supersede(db, account_id, str(supersedes)):
                raise ValueError('Superseded preference must be active in this account.')
            record = dict(id=preference_id, account_id=account_id, revision=1, text=text, scope=str(scope),
                          status='active', source_draft_id=str(source_draft_id), source_revision=int(revision),
                          evidence=evidence, confidence=confidence, supersedes=supersedes,
                          schema_version=SCHEMA_VERSION, contract=self.contract, created_at=_now(), authority=authority)
            if recipient:
                record.update(recipient=recipient, account_fingerprint=account_fingerprint,
                              learning_contract=AUTOMATIC_CONTRACT, learning_mode=learning_mode, category=category)
            db.execute('INSERT INTO preferences VALUES(?,?,?,?,?,?,?)',
                       (account_id, preference_id, 1, text, str(scope), 'active', json.dumps(record)))
            return record

    def save_preference(self, account_id, preference_id, text):
        account_id, preference_id, text = str(account_id), str(preference_id), str(text).strip()
        if not text or len(text) > 20000:
            raise ValueError('A bounded preference is required.')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT record FROM preferences WHERE account=? AND id=? ORDER BY revision DESC LIMIT 1',
                             (account_id, preference_id)).fetchone()
            if not row or json.loads(row[0])['status'] != 'active':
                raise ValueError('Preference is not active in this account.')
            record = json.loads(row[0])
            self._supersede(db, account_id, preference_id)
            # The user's wording now leads (authority 'user'), but the preference
            # keeps the scope it was learned for: `learned_authority` records that
            # a sender-specific approved-edit rule never becomes mailbox-wide.
            record.setdefault('learned_authority', record.get('authority', 'user'))
            record.update(revision=record['revision'] + 1, text=text, authority='user', user_edited=True, updated_at=_now())
            db.execute('INSERT INTO preferences VALUES(?,?,?,?,?,?,?)',
                       (account_id, preference_id, record['revision'], text, record['scope'], 'active', json.dumps(record)))
            return record

    def delete_preference(self, account_id, preference_id):
        """Forget active text and historical revisions in this account only."""
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            deleted = db.execute('DELETE FROM preferences WHERE account=? AND id=?',
                                 (str(account_id), str(preference_id))).rowcount > 0
            if deleted:
                db.execute('INSERT OR IGNORE INTO forgotten_preferences VALUES(?,?)',
                           (str(account_id), str(preference_id)))
            return deleted


class _ClosingConnection:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db.__enter__()

    def __exit__(self, *args):
        try:
            return self.db.__exit__(*args)
        finally:
            self.db.close()
