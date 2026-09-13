import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from our_harness.email_classic import EmailClassic, BATCH_SIZE, _SCRIPT, _powershell_executable
from our_harness.models import HarnessError


class Transport:
    def __init__(self):
        self.accounts = [dict(store_id='store-a', folder_id='inbox-a', profile='profile-a', email='a@example.test', name='A'),
                         dict(store_id='store-b', folder_id='inbox-b', profile='profile-a', email='b@example.test', name='B')]
        self.ids = [str(i) for i in range(BATCH_SIZE * 3 + 2)]
        self.calls = []
        self.missing = set()
        self.fail = False

    def __call__(self, operation, payload):
        self.calls.append((operation, payload))
        if self.fail:
            raise HarnessError('New Outlook is unsupported; open classic Outlook.')
        if operation == 'discover':
            return {'accounts': self.accounts}
        if not any(all(account[k] == payload[k] for k in ('store_id', 'folder_id', 'profile', 'email')) for account in self.accounts):
            raise HarnessError('Selected profile changed')
        if operation == 'headers':
            return {'ids': list(self.ids)}
        return {'messages': [dict(entry_id=x, sender='sender@example.test', subject='Subject ' + x, body='Body ' + x)
                             for x in payload['ids'] if x not in self.missing],
                'missing': [x for x in payload['ids'] if x in self.missing]}


class EmailClassicTests(unittest.TestCase):
    def test_powershell_follows_changed_system_directory_without_cached_state(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ('system-a', 'relocated-system'):
                root = Path(directory) / name
                with patch.dict('os.environ', {'SystemRoot': str(root)}, clear=True):
                    self.assertEqual(_powershell_executable(), root / 'System32/WindowsPowerShell/v1.0/powershell.exe')
            with patch.dict('os.environ', {'WINDIR': str(root)}, clear=True):
                self.assertEqual(_powershell_executable(), root / 'System32/WindowsPowerShell/v1.0/powershell.exe')

    def test_powershell_rejects_missing_or_relative_system_directory(self):
        for environment in ({}, {'SystemRoot': 'relative-system'}):
            with patch.dict('os.environ', environment, clear=True), self.assertRaises(HarnessError):
                _powershell_executable()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.transport = Transport()
        self.adapter = EmailClassic(Path(self.temp.name), transport=self.transport)
        self.connections = self.adapter.discover()
        self.id = self.connections[1]['id']

    def test_discover_reads_only_identities_and_selects_explicit_store(self):
        self.assertEqual([x[0] for x in self.transport.calls], ['discover'])
        self.assertEqual(self.connections[1]['provider'], 'classic_outlook')
        batch = self.adapter.sync(self.id)
        self.assertEqual(len(batch['messages']), BATCH_SIZE)
        self.assertTrue(all(payload['store_id'] == 'store-b' for operation, payload in self.transport.calls if operation != 'discover'))
        self.assertNotIn('store_id', self.adapter.status(self.id))
        with self.assertRaises(HarnessError):
            self.adapter.sync('not-a-discovered-identity')

    def test_restart_replays_unacknowledged_batch_then_advances(self):
        first = self.adapter.sync(self.id)
        restarted = EmailClassic(self.temp.name, transport=self.transport)
        self.transport.fail = True
        self.assertEqual(restarted.sync(self.id), first)
        self.assertEqual(restarted.sync(self.id, 'stale-cursor'), first)
        self.transport.fail = False
        second = restarted.sync(self.id, first['cursor'])
        self.assertFalse({m['source_id'] for m in first['messages']} & {m['source_id'] for m in second['messages']})

    def test_burst_arrivals_do_not_starve_existing_queue(self):
        cursor = ''
        subjects = []
        for count in range(4):
            batch = self.adapter.sync(self.id, cursor)
            cursor = batch['cursor']
            subjects.extend(m['subject'] for m in batch['messages'])
            self.transport.ids = ['new-%s-%s' % (count, i) for i in range(50)] + self.transport.ids
        self.assertTrue(all('Subject ' + str(i) in subjects for i in range(BATCH_SIZE * 3 + 2)))

    def test_acknowledged_mail_not_repeated_after_restart(self):
        self.transport.ids = ['one']
        first = self.adapter.sync(self.id)
        second = EmailClassic(self.temp.name, transport=self.transport).sync(self.id, first['cursor'])
        self.assertEqual(second['messages'], [])
        self.transport.ids.append('two')
        third = EmailClassic(self.temp.name, transport=self.transport).sync(self.id, second['cursor'])
        self.assertEqual([m['subject'] for m in third['messages']], ['Subject two'])

    def test_changed_profile_never_switches_accounts(self):
        self.transport.accounts[1]['profile'] = 'different-machine-profile'
        with self.assertRaisesRegex(HarnessError, 'profile changed'):
            self.adapter.sync(self.id)
        changed = self.adapter.discover()
        self.assertNotEqual(changed[1]['id'], self.id)
        self.assertEqual(self.adapter.status(self.id)['state'], 'disconnected')
        with self.assertRaisesRegex(HarnessError, 'profile changed'):
            self.adapter.sync(self.id)

    def test_transport_failure_does_not_commit_ack(self):
        first = self.adapter.sync(self.id)
        self.transport.fail = True
        with self.assertRaises(HarnessError):
            self.adapter.sync(self.id, first['cursor'])
        self.assertEqual(self.adapter.sync(self.id), first)

    def test_missing_item_retried_without_starving_others(self):
        self.transport.missing = {'0'}
        batch = self.adapter.sync(self.id)
        self.assertTrue(batch['warnings'])
        cursor = batch['cursor']
        self.transport.missing.clear()
        subjects = []
        for _ in range(4):
            batch = self.adapter.sync(self.id, cursor)
            cursor = batch['cursor']
            subjects.extend(m['subject'] for m in batch['messages'])
        self.assertEqual(subjects.count('Subject 0'), 1)

    def test_bad_schema_fails_closed(self):
        state = json.loads(self.adapter.path.read_text())
        state['schema'] = 999
        self.adapter.path.write_text(json.dumps(state))
        with self.assertRaisesRegex(HarnessError, 'incompatible'):
            EmailClassic(self.temp.name, transport=self.transport).discover()

    def test_unavailable_classic_is_clear_and_no_real_mail_tested(self):
        self.transport.fail = True
        with self.assertRaisesRegex(HarnessError, 'New Outlook'):
            self.adapter.discover()

    def test_foreign_message_and_incomplete_batch_fail_without_advancing(self):
        original = self.adapter.transport
        for response in ({'messages': [dict(entry_id='foreign', sender='x', subject='x', body='x')], 'missing': []},
                         {'messages': [], 'missing': []}):
            self.adapter.transport = lambda operation, payload: response if operation == 'messages' else original(operation, payload)
            with self.assertRaises(HarnessError):
                self.adapter.sync(self.id)
            self.assertNotIn('pending', json.loads(self.adapter.path.read_text())['connections'][self.id])

    def test_store_identity_isolates_same_entry_id(self):
        self.transport.ids = ['same-entry']
        first = self.adapter.sync(self.connections[0]['id'])
        second = self.adapter.sync(self.connections[1]['id'])
        self.assertNotEqual(first['messages'][0]['source_id'], second['messages'][0]['source_id'])

    def test_com_script_is_read_only_and_account_scoped(self):
        self.assertIn('GetExchangeUser().PrimarySmtpAddress', _SCRIPT)
        self.assertIn('GetFolderFromID($request.folder_id, $request.store_id)', _SCRIPT)
        self.assertIn('GetItemFromID([string]$id, $request.store_id)', _SCRIPT)
        self.assertNotIn('.Send(', _SCRIPT)
        self.assertNotIn('.Save(', _SCRIPT)
        self.assertNotIn('.Logon(', _SCRIPT)


if __name__ == '__main__':
    unittest.main()
