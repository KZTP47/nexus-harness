import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from our_harness.email_memory import EmailMemory, canonical_recipient


class EmailMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'arbitrary tenant' / 'memory'
        self.memory = EmailMemory(self.root)

    def message(self, id, body, sender='person@example.test'):
        return dict(id=id, body=body, sender=sender, subject='Project', received_at='2025-01-01')

    def test_older_cross_sender_restart_and_account_isolation(self):
        self.memory.index_message('a', 'v1', self.message('old', 'Orion delivery deadline is February', 'other@example.test'))
        for i in range(30):
            self.memory.index_message('a', 'v1', self.message(str(i), 'Routine unrelated message'))
        self.memory.index_message('b', 'v1', self.message('old', 'Orion secret other tenant'))
        results = EmailMemory(self.root).context('a', 'v1', 'Orion deadline', sender='person@example.test')
        self.assertEqual(results[0]['id'], 'old')
        self.assertEqual(results[0]['account_id'], 'a')
        self.assertNotIn('secret', str(results))
        self.assertEqual(self.memory.context('a', 'v2', 'Orion'), [])

    def test_swedish_and_explicit_paraphrase_expansion(self):
        self.memory.index_message('a', 'v1', self.message('1', 'Leverans Göteborg. Please keep correspondence concise.'))
        self.assertEqual(self.memory.context('a', 'v1', 'brief'), [])
        self.assertEqual(self.memory.context('a', 'v1', ['brief', 'concise'])[0]['id'], '1')
        self.assertEqual(self.memory.context('a', 'v1', 'göteborg leverans')[0]['id'], '1')
        self.assertEqual(self.memory.context('b', 'v1', ['brief', 'concise']), [])
        self.memory.context('a', 'v1', '" OR NEAR ( * )')  # quoted tokens cannot inject MATCH syntax

    def test_sync_forget_contract_and_stable_attribution(self):
        self.memory.sync_messages('a', 'v1', [self.message('1', 'alpha'), self.message('2', 'beta')])
        self.memory.sync_messages('a', 'v1', [self.message('1', 'gamma')])
        self.assertEqual(self.memory.context('a', 'v1', 'alpha beta'), [])
        rebuilt = EmailMemory(self.root, contract='new-query-model-v2')
        self.assertEqual(rebuilt.context('a', 'v1', 'gamma')[0]['id'], '1')
        self.assertEqual(rebuilt.context('a', 'other-fingerprint', 'gamma'), [])
        rebuilt.forget_message('b', 'v1', '1')
        self.assertEqual(len(rebuilt.context('a', 'v1', 'gamma')), 1)
        rebuilt.forget_message('a', 'v1', '1')
        self.assertEqual(EmailMemory(self.root).context('a', 'v1', 'gamma'), [])

    def test_preference_evidence_replay_edit_history_and_forget(self):
        original = self.memory.learn('a', 'draft1', 2, 'Use short replies', evidence='Please shorten this', confidence=.9)
        replay = self.memory.learn('a', 'draft1', 2, 'Use short replies')
        self.assertEqual(original['id'], replay['id'])
        self.assertEqual(len(self.memory.preferences('a')), 1)
        edited = EmailMemory(self.root).save_preference('a', original['id'], 'Use detailed replies')
        self.assertEqual(edited['revision'], 2)
        history = self.memory.preference_history('a', original['id'])
        self.assertEqual([p['status'] for p in history], ['superseded', 'active'])
        self.assertEqual(history[0]['evidence'], 'Please shorten this')
        self.assertEqual(self.memory.preferences('a')[0]['authority'], 'user')
        with self.assertRaises(ValueError):
            self.memory.save_preference('b', original['id'], 'Hijack')
        self.assertFalse(self.memory.delete_preference('b', original['id']))
        self.assertTrue(self.memory.delete_preference('a', original['id']))
        self.assertEqual(EmailMemory(self.root).preference_history('a', original['id']), [])
        self.assertEqual(self.memory.learn('a', 'draft1', 2, 'Use short replies')['status'], 'forgotten')
        self.assertEqual(self.memory.preferences('a'), [])

    def test_concurrent_replay_produces_one_revision(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.memory.learn('a', 'd', 1, 'Brief replies'), range(12)))
        self.assertEqual(len({row['id'] for row in results}), 1)
        self.assertEqual(len(self.memory.preference_history('a', results[0]['id'])), 1)

    def test_explicit_supersession_and_isolated_rollback(self):
        old = self.memory.learn('a', 'draft1', 1, 'Use short replies')
        with self.assertRaises(ValueError):
            self.memory.learn('b', 'draft2', 1, 'Use long replies', supersedes=old['id'])
        self.assertEqual(len(self.memory.preferences('a')), 1)
        new = self.memory.learn('a', 'draft2', 1, 'Use long replies', supersedes=old['id'])
        self.memory.learn('a', 'draft1', 1, 'Use short replies')
        self.assertEqual([p['id'] for p in self.memory.preferences('a')], [new['id']])
        self.assertEqual(len(self.memory.preferences('b')), 0)

    def test_empty_scope_and_missing_id_fail_closed(self):
        with self.assertRaises(ValueError):
            self.memory.index_message('a', '', self.message('1', 'test'))
        with self.assertRaises(ValueError):
            self.memory.index_message('a', 'v1', {'body': 'unattributed'})
        self.assertEqual(self.memory.context('a', 'v1', ''), [])

    def test_recipient_identity_is_exact_and_ambiguous_values_fail_closed(self):
        self.assertEqual(canonical_recipient('Grandma <GRANDMA@example.test>'), 'grandma@example.test')
        for value in ('', 'Grandma', 'a@example.test,b@example.test', 'Friends: a@example.test;', 'a@example.test\n'):
            self.assertEqual(canonical_recipient(value), '')
        self.assertNotEqual(canonical_recipient('a+family@example.test'), canonical_recipient('a@example.test'))

    def test_a_decoded_display_name_with_a_comma_is_still_one_recipient(self):
        for value in ('Müller, Hans <H@x.de>', '"Müller, Hans" <h@x.de>', 'Smith, Dr. Jane <h@x.de>'):
            self.assertEqual(canonical_recipient(value), 'h@x.de', value)
        for value in ('boss@corp.test, <attacker@evil.test>', 'alice@corp.example, Bob <bob@evil.example>',
                      'ceo@corp.example <attacker@evil.example>', 'Team: a, b <x@y.test>', 'a, b <x@y.test>, <z@y.test>'):
            self.assertEqual(canonical_recipient(value), '', value)

    def test_preferences_edited_by_an_older_version_recover_their_origin_once(self):
        import sqlite3
        from contextlib import closing
        learned = self.memory.learn('a', 'draft1', 2, 'Sign as Dr. A', evidence={'message_id': 'm1'})
        own = self.memory.learn('a', 'manual', 1, 'Be brief', authority='user')
        edited = self.memory.save_preference('a', learned['id'], 'Sign as Dr. Ann')
        self.memory.save_preference('a', own['id'], 'Be very brief')
        # What an older version wrote: authority 'user' and no learned_authority.
        with closing(sqlite3.connect(self.memory.path)) as db, db:
            for account, identity, revision, raw in db.execute('SELECT account,id,revision,record FROM preferences').fetchall():
                record = json.loads(raw); record.pop('learned_authority', None)
                db.execute('UPDATE preferences SET record=? WHERE account=? AND id=? AND revision=?', (json.dumps(record), account, identity, revision))
            db.execute("DELETE FROM metadata WHERE key='learned-authority/v1'")
            # A preference whose first revision is gone falls back to its evidence.
            orphan = {**edited, 'id': 'orphan', 'revision': 2, 'authority': 'user', 'evidence': {'message_id': 'm9'}}
            orphan.pop('learned_authority', None)
            db.execute('INSERT INTO preferences VALUES(?,?,?,?,?,?,?)', ('a', 'orphan', 2, orphan['text'], orphan['scope'], 'active', json.dumps(orphan)))
        reopened = EmailMemory(self.root)
        active = {p['id']: p for p in reopened.preferences('a')}
        self.assertEqual(active[learned['id']]['learned_authority'], 'approved_edit')
        self.assertEqual(active[learned['id']]['authority'], 'user', 'the edited wording still leads')
        self.assertEqual(active[own['id']]['learned_authority'], 'user')
        self.assertEqual(active['orphan']['learned_authority'], 'approved_edit')
        # Idempotent: a later start does not rewrite what the user changed since.
        with closing(sqlite3.connect(self.memory.path)) as db, db:
            row = db.execute("SELECT revision,record FROM preferences WHERE account='a' AND id=? AND status='active'", (own['id'],)).fetchone()
            record = json.loads(row[1]); record['learned_authority'] = 'approved_edit'
            db.execute("UPDATE preferences SET record=? WHERE account='a' AND id=? AND revision=?", (json.dumps(record), own['id'], row[0]))
        self.assertEqual({p['id']: p for p in EmailMemory(self.root).preferences('a')}[own['id']]['learned_authority'], 'approved_edit')

    def test_an_edited_preference_keeps_the_scope_it_was_learned_for(self):
        learned = self.memory.learn('a', 'draft1', 2, 'Sign as Dr. A')
        self.assertEqual(learned['authority'], 'approved_edit')
        edited = self.memory.save_preference('a', learned['id'], 'Sign as Dr. Ann')
        self.assertEqual((edited['authority'], edited['learned_authority'], edited['scope']), ('user', 'approved_edit', learned['scope']))
        again = EmailMemory(self.root).save_preference('a', learned['id'], 'Sign as Ann')
        self.assertEqual(again['learned_authority'], 'approved_edit', 'a second edit does not forget the origin')
        own = self.memory.learn('a', 'manual', 1, 'Be brief', authority='user')
        self.assertEqual(self.memory.save_preference('a', own['id'], 'Be very brief')['learned_authority'], 'user')

    def test_automatic_category_supersession_edit_and_restart_preserve_scope(self):
        def learn(draft, recipient, text, fingerprint='v1'):
            return self.memory.learn('a', draft, 2, text, recipient=recipient,
                account_fingerprint=fingerprint, learning_mode='automatic', category='length')
        first = learn('d1', 'grandma@example.test', 'Use shorter replies')
        customer = learn('d2', 'customer@example.test', 'Use detailed replies')
        second = learn('d3', 'grandma@example.test', 'Use detailed replies')
        self.assertEqual({m['id'] for m in self.memory.preferences('a')}, {customer['id'], second['id']})
        self.assertEqual(self.memory.preference_history('a', first['id'])[0]['status'], 'superseded')
        edited = self.memory.save_preference('a', second['id'], 'Use very short replies')
        self.assertEqual(edited['recipient'], 'grandma@example.test')
        self.assertEqual(edited['learning_mode'], 'automatic')
        self.assertEqual(edited['account_fingerprint'], 'v1')
        self.assertEqual(EmailMemory(self.root).preference_history('a', second['id'])[-1], edited)
        self.memory.delete_preference('a', second['id'])
        self.assertEqual(learn('d3', 'grandma@example.test', 'Use detailed replies')['status'], 'forgotten')
        self.assertEqual([m['id'] for m in self.memory.preferences('a')], [customer['id']])

    def test_recipient_scoped_retrieval_filters_before_ranking_and_honors_reply_to(self):
        messages = [self.message(str(i), 'Matching shared topic', 'customer@example.test') for i in range(20)]
        messages.append({**self.message('family', 'Matching shared topic', 'Proxy <proxy@example.test>'),
                         'reply_to': 'Grandma <grandma@example.test>'})
        self.memory.sync_messages('a', 'v1', messages)
        self.assertEqual([m['id'] for m in self.memory.context('a', 'v1', 'Matching shared topic',
                         limit=1, recipient='grandma@example.test')], ['family'])
        self.assertEqual(self.memory.context('a', 'v1', 'Matching', recipient=''), [])


if __name__ == '__main__':
    unittest.main()
