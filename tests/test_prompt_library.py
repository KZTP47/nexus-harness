import copy
import sqlite3
import tempfile
import unittest
import json
import threading
import urllib.request
import urllib.error
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

from our_harness import prompt_library
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError


class PromptLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})

    def test_create_edit_restart_exact_text_reuse_and_delete(self):
        text = '  Read the files.\n\nUse café and 日本語; preserve "quotes".\n'
        prompt = prompt_library.update(self.config, {'action': 'save', 'title': 'Review code', 'body': text})['prompt']
        self.assertEqual(prompt['body'], text)
        fresh = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.assertEqual(prompt_library.listing(fresh)['prompts'], [prompt])
        changed = prompt_library.update(fresh, {**prompt, 'action': 'save', 'body': text + 'Check tests.'})['prompt']
        self.assertEqual(changed['revision'], 2)
        for action in ['save', 'delete']:
            with self.assertRaisesRegex(HarnessError, 'another window'):
                prompt_library.update(fresh, {**prompt, 'action': action})
        self.assertEqual(prompt_library.listing(fresh)['prompts'], [changed])
        prompt_library.update(fresh, {**changed, 'action': 'delete'})
        self.assertEqual(prompt_library.listing(fresh)['prompts'], [])

    def test_concurrent_creation_does_not_lose_prompts_and_limits_never_truncate(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda number: prompt_library.update(self.config,
                {'action': 'save', 'title': str(number), 'body': 'text ' + str(number)}), range(12)))
        self.assertEqual(len(prompt_library.listing(self.config)['prompts']), 12)
        for update in [{'title': ''}, {'body': ' '}, {'body': 'x' * 100001}, {'title': 'x' * 161}]:
            with self.assertRaises(HarnessError):
                prompt_library.update(self.config, {'action': 'save', 'title': 'valid', 'body': 'valid', **update})
        self.assertEqual(len(prompt_library.listing(self.config)['prompts']), 12)

    def test_library_is_per_installation_and_unknown_contract_keeps_rows(self):
        prompt = prompt_library.update(self.config, {'action': 'save', 'title': 'Kept', 'body': 'Exact body'})['prompt']
        other = self.root / 'other installation'
        other.mkdir()
        self.assertEqual(prompt_library.listing(LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), other, [], {}))['prompts'], [])
        target = self.root / '.harness/prompt-library.sqlite3'
        with closing(sqlite3.connect(target)) as db, db:
            db.execute("UPDATE library_contract SET fingerprint='future'")
        with self.assertRaisesRegex(HarnessError, 'compatible'):
            prompt_library.listing(self.config)
        with closing(sqlite3.connect(target)) as db:
            self.assertEqual(db.execute('SELECT body FROM prompts').fetchone()[0], prompt['body'])

    def test_http_library_requires_token_and_round_trips_exact_saved_prompt(self):
        from our_harness.server import HarnessHTTPServer
        server = HarnessHTTPServer(('127.0.0.1', 0), self.config)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        url = f'http://127.0.0.1:{server.server_address[1]}/api/prompt-library'
        def call(payload=None, token=True):
            request = urllib.request.Request(url, data=json.dumps(payload).encode() if payload else None,
                headers={'Content-Type': 'application/json', **({'X-Harness-Token': server.token} if token else {})})
            try:
                with urllib.request.urlopen(request) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                return error.code, json.load(error)
        self.assertNotEqual(call(token=False)[0], 200)
        body = {'action': 'save', 'title': 'HTTP prompt', 'body': 'Exact saved prompt\n'}
        self.assertNotEqual(call(body, token=False)[0], 200)
        code, result = call(body)
        self.assertEqual(code, 200, result)
        self.assertEqual(call()[1]['prompts'], [result['prompt']])
        self.assertEqual(call({**result['prompt'], 'action': 'delete'})[0], 200)
        self.assertEqual(call()[1]['prompts'], [])


if __name__ == '__main__':
    unittest.main()
