from __future__ import annotations

import copy
import hashlib
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from our_harness import agent_mailbox, semantic_memory, timer, timer_observation
from our_harness.collaboration_status import task_delivery
from our_harness.persistent_memory_index import VaultMemoryIndex
from tests.test_running_on_a_timer import TimerTestCase


class ObservedTimers(TimerTestCase):
    def test_upgrade_reuses_previously_accepted_default_occurrence(self):
        from our_harness.pipeline_runs import PipelineRunStore
        from our_harness import pipelines
        one = self.a_timer(how_often='every-hour')
        timer.looked_just_now(self.config, datetime(2026, 7, 9, 0, 0))
        legacy = one.to_dict()
        legacy.pop('watch_files')
        legacy.pop('max_lateness_minutes')
        policy = hashlib.sha256(json.dumps(legacy, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:16]
        name = hashlib.sha256(one.name.encode()).hexdigest()[:16]
        store = PipelineRunStore(self.config)
        accepted, _ = store.accept(pipelines.freeze_definition(self.config, pipelines.load(self.config, one.automation)),
                                   source=f'timer:{one.name}', request_id=f'timer:{name}:{policy}:2026-07-09T01:00:00')
        with mock.patch('our_harness.pipelines.run_it', side_effect=AssertionError('accepted run replayed')):
            result = timer.run_what_is_due(self.config, now=datetime(2026, 7, 9, 1, 0))
        self.assertEqual(result['ran'][0]['run_id'], accepted['run_id'])
        self.assertTrue(result['ran'][0]['deferred'])

    def run_at(self, hour):
        with self.stand_in(), mock.patch.object(timer, '_tell_somebody_about_it', return_value=[]):
            return timer.run_what_is_due(self.config, now=datetime(2026, 7, 9, hour, 0))

    def test_unchanged_restart_edit_create_and_policy_change(self):
        (self.root / 'input.txt').write_text('first')
        one = self.a_timer(how_often='every-hour', watch_files=['input.txt', 'future.txt'])
        timer.looked_just_now(self.config, datetime(2026, 7, 9, 0, 0))
        self.assertTrue(self.run_at(1)['ran'][0]['passed'])
        # Read from disk through a fresh runner; no model/automation call.
        with mock.patch('our_harness.pipelines.run_it', side_effect=AssertionError('unchanged input ran')):
            result = timer.run_what_is_due(self.config, now=datetime(2026, 7, 9, 2, 0))
        self.assertEqual(result['ran'][0]['outcome'], 'unchanged')
        self.assertEqual(self.run_at(2)['ran'], [])
        (self.root / 'input.txt').write_text('second')
        self.assertTrue(self.run_at(3)['ran'][0]['passed'])
        (self.root / 'future.txt').write_text('created')
        self.assertTrue(self.run_at(4)['ran'][0]['passed'])
        changed = one.to_dict()
        changed['max_lateness_minutes'] = 15
        timer.save(self.config, changed)
        self.assertTrue(self.run_at(5)['ran'][0]['passed'])

    def test_freshness_uses_latest_occurrence_and_stale_is_not_success(self):
        self.a_timer(how_often='every-hour', max_lateness_minutes=10)
        timer.looked_just_now(self.config, datetime(2026, 7, 9, 0, 0))
        with mock.patch('our_harness.pipelines.run_it', side_effect=AssertionError('stale input ran')):
            result = timer.run_what_is_due(self.config, now=datetime(2026, 7, 9, 5, 30))
        self.assertEqual(result['ran'][0]['outcome'], 'stale')
        self.assertFalse(result['ran'][0]['passed'])
        self.assertTrue(self.run_at(6)['ran'][0]['passed'])

    def test_failed_run_does_not_consume_change(self):
        (self.root / 'input.txt').write_text('first')
        self.a_timer(how_often='every-hour', watch_files=['input.txt'])
        timer.looked_just_now(self.config, datetime(2026, 7, 9, 0, 0))
        with self.stand_in(passed=False):
            self.assertFalse(timer.run_what_is_due(self.config, now=datetime(2026, 7, 9, 1, 0))['ran'][0]['passed'])
        self.assertTrue(self.run_at(2)['ran'][0]['passed'])

    def test_watch_boundaries_and_changed_root(self):
        for value in (['../elsewhere'], ['C:/elsewhere'], ['.harness/runtime/file'], ['a'] * 65):
            with self.assertRaises(Exception):
                timer_observation.paths(value)
        (self.root / 'input.txt').write_text('same')
        with tempfile.TemporaryDirectory() as other:
            (Path(other) / 'input.txt').write_text('same')
            left = timer_observation.observe(self.root, ['input.txt'], {'version': 1})
            right = timer_observation.observe(Path(other), ['input.txt'], {'version': 1})
        self.assertNotEqual(left['contract'], right['contract'])
        self.assertEqual(left['inputs'], right['inputs'])


class HybridMemory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = semantic_memory.settings({'embedding_model': 'fixture'})
        self.index = VaultMemoryIndex(self.root, semantic=self.config)
        self.model_hash = 'a' * 64
        self.calls = []
        (self.root / 'transport.md').write_text('# Notes\nA bicycle uses pedals.', encoding='utf-8')
        (self.root / 'garden.md').write_text('# Notes\nPlants need water.', encoding='utf-8')
        self.index.refresh()
        self.patch = mock.patch.object(semantic_memory, 'request_json', self.request)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.wait_all)

    def request(self, url, data=None):
        self.calls.append((url, data))
        if url.endswith('/api/tags'):
            return {'models': [{'name': 'fixture', 'digest': self.model_hash}]}
        def embed(text):
            return [1.0, 0.0] if any(word in text.casefold() for word in ('bicycle', 'pedals', 'cycling')) else [0.0, 1.0]
        texts = data['input'] if isinstance(data['input'], list) else [data['input']]
        return {'embeddings': [embed(text) for text in texts]}

    def test_local_http_embedding_adapter(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.patch.stop()
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                self.answer(None)
            def do_POST(self):
                self.answer(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            def answer(self, data):
                payload = json.dumps(owner.request(self.path, data)).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                self.config = semantic_memory.settings({'embedding_model': 'fixture', 'embedding_url': f'http://127.0.0.1:{server.server_port}'})
                self.index = VaultMemoryIndex(self.root, semantic=self.config)
                self.assertEqual(self.ready()[0]['path'], 'transport.md')
                self.assertTrue(any(url == '/api/embed' and isinstance(data['input'], list) for url, data in self.calls if data))
            finally:
                self.wait_all()
                server.shutdown()
                worker.join(3)

    def wait_all(self):
        for state in list(semantic_memory._states.values()):
            thread = state.get('thread')
            if thread:
                thread.join(5)
                self.assertFalse(thread.is_alive())

    def ready(self, query='cycling'):
        key = (str(self.index.database_path), semantic_memory.digest(self.config))
        state = semantic_memory._states.get(key)
        if state:
            state['next_check'] = 0
        self.index.search(query)
        self.wait_all()
        return self.index.search(query)

    def test_labeled_recall_fallback_and_restart(self):
        # A synonym fixture: lexical baseline misses; hybrid ranks the labeled
        # relevant note first. This is a contract oracle, not model-quality data.
        self.assertEqual(self.index._lexical_search('cycling'), [])
        result = self.ready()
        self.assertEqual(result[0]['path'], 'transport.md')
        self.assertEqual(result[0]['retrieval_sources'], ['semantic'])
        self.assertTrue(self.index.retrieval_trace['vector_only'])
        self.index = VaultMemoryIndex(self.root, semantic=self.config)
        self.assertEqual(self.ready()[0]['path'], 'transport.md')
        # Simulate a cold process: disk vectors survive but must be validated.
        self.wait_all()
        semantic_memory._states.pop((str(self.index.database_path), semantic_memory.digest(self.config)))
        self.assertEqual(self.ready()[0]['path'], 'transport.md')
        self.assertTrue(all('keep_alive' not in (data or {}) for _, data in self.calls))

    def test_binding_and_model_changes_during_embedding_discard_batch(self):
        binding = self.root / '.nexus-project-memory.json'
        binding.write_text('{"project": "first"}')
        original = self.request
        def changed_binding(url, data=None):
            result = original(url, data)
            if data and isinstance(data['input'], list):
                binding.write_text('{"project": "second"}')
            return result
        with mock.patch.object(semantic_memory, 'request_json', changed_binding):
            self.index.search('cycling')
            self.wait_all()
        with closing(self.index._connect()) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM semantic_chunks').fetchone()[0], 0)
        binding.unlink()
        def changed_model(url, data=None):
            result = original(url, data)
            if data and isinstance(data['input'], list):
                self.model_hash = 'b' * 64
            return result
        with mock.patch.object(semantic_memory, 'request_json', changed_model):
            self.index.search('cycling')
            self.wait_all()
        with closing(self.index._connect()) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM semantic_chunks').fetchone()[0], 0)

    def test_same_mtime_edit_delete_and_model_change_invalidate(self):
        self.ready()
        old = self.root / 'transport.md'
        stat = old.stat()
        old.write_text('# Notes\nA flowers uses water.', encoding='utf-8')
        os.utime(old, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.index.refresh()
        self.assertFalse(any(row['path'] == 'transport.md' for row in self.ready()))
        old.unlink()
        self.index.refresh()
        with closing(self.index._connect()) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM semantic_chunks s LEFT JOIN chunks c ON c.id=s.chunk_id WHERE c.id IS NULL').fetchone()[0], 0)
        previous = semantic_memory._states[(str(self.index.database_path), semantic_memory.digest(self.config))]['contract']
        self.model_hash = 'b' * 64
        self.ready()
        current = semantic_memory._states[(str(self.index.database_path), semantic_memory.digest(self.config))]['contract']
        self.assertNotEqual(previous, current)

    def test_slow_or_offline_embedding_never_holds_search(self):
        gate = threading.Event()
        began = threading.Event()
        original = self.request
        def slow(url, data=None):
            began.set()
            gate.wait(3)
            return original(url, data)
        with mock.patch.object(semantic_memory, 'request_json', slow):
            start = time.monotonic()
            result = self.index.search('bicycle')
            elapsed = time.monotonic() - start
            self.assertEqual(result[0]['path'], 'transport.md')
            self.assertLess(elapsed, 0.5)
            self.assertTrue(began.wait(1))
            gate.set()
            self.wait_all()
        with mock.patch.object(semantic_memory, 'request_json', side_effect=OSError('offline')):
            self.ready('bicycle')
            self.assertEqual(self.index.retrieval_trace['mode'], 'fts')
            self.assertEqual(self.index.retrieval_trace['state'], 'unavailable')

    def test_other_vault_and_invalid_vectors_cannot_enter_results(self):
        self.ready()
        with tempfile.TemporaryDirectory() as elsewhere:
            other = VaultMemoryIndex(Path(elsewhere), semantic=self.config)
            other.refresh()
            self.assertEqual(other.search('cycling'), [])
            self.wait_all()
            self.assertEqual(other.search('cycling'), [])
        for bad in ([float('nan'), 1], [0, 0], [True, 1], [], [1, 2, 3]):
            with self.assertRaises(ValueError):
                semantic_memory.vector(bad, 2)
        for endpoint in ('https://remote.example', 'http://127.0.0.1/private', 'http://user:pass@localhost', 'http://localhost?forward=remote'):
            with self.assertRaises(ValueError):
                semantic_memory.settings({'embedding_model': 'fixture', 'embedding_url': endpoint})


class DeliveryObservations(unittest.TestCase):
    def test_observation_does_not_retry_acknowledge_or_interrupt(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'mail.json'
            message = agent_mailbox.enqueue(path, shared_goal_id='goal', sender='a', sender_name='A', receiver='b', receiver_name='B', project='project', project_name='Project', body='Continue editing the shared project.')
            before = path.read_bytes()
            self.assertEqual(agent_mailbox.delivery_details(path)[message.message_id]['stage'], 'queued')
            active = agent_mailbox.delivery_details(path, active_message_ids=[message.message_id])[message.message_id]
            self.assertEqual(active['stage'], 'dispatched')
            self.assertFalse(active['attention'])
            self.assertEqual(path.read_bytes(), before)
            agent_mailbox.attempted(path, [message.message_id], 'unavailable')
            self.assertTrue(agent_mailbox.delivery_details(path)[message.message_id]['attention'])
            agent_mailbox.acknowledge(path, [message.message_id])
            self.assertEqual(agent_mailbox.delivery_details(path)[message.message_id]['stage'], 'response_recorded')

    def test_completion_is_not_confused_with_recorded_response(self):
        goal = {'status': 'running'}
        task = {'state': 'complete'}
        original = copy.deepcopy((goal, task))
        self.assertEqual(task_delivery(goal, task)['stage'], 'response_recorded')
        self.assertEqual((goal, task), original)
        self.assertEqual(task_delivery({'status': 'complete'}, task)['stage'], 'verified')
        self.assertEqual(task_delivery(goal, {'state': 'running', 'outcome_unknown': True})['stage'], 'unknown')


from tests import test_long_horizon_dialogue as collaboration_fixtures
from our_harness import pipelines, long_horizon
from our_harness.config import LoadedConfig


class UninterruptedCollaboration(unittest.TestCase):
    setUp = collaboration_fixtures.LongHorizonDialogueTests.setUp
    create = collaboration_fixtures.LongHorizonDialogueTests.create
    provider = collaboration_fixtures.LongHorizonDialogueTests.provider
    run_replies = collaboration_fixtures.LongHorizonDialogueTests.run_replies

    def test_agents_talk_create_edit_and_finish_while_embedding_is_stalled_and_timer_checks(self):
        vault = self.base / 'independent fixture memory'
        vault.mkdir()
        (vault / 'notes.md').write_text('Bicycle pedals are an input mechanism.')
        index = VaultMemoryIndex(vault, semantic=semantic_memory.settings({'embedding_model': 'fixture'}))
        index.refresh()
        blocked = threading.Event()
        release = threading.Event()
        def slow_embed(*args, **kwargs):
            blocked.set()
            release.wait(10)
            raise OSError('offline fixture')
        timer_config = LoadedConfig(copy.deepcopy(self.config.data), self.project, [], {})
        pipelines.save(timer_config, {'name': 'Observe', 'nodes': [{'id': 's', 'kind': 'start', 'label': 'Start'}], 'edges': []})
        timer.save(timer_config, {'name': 'Background', 'automation': 'Observe', 'how_often': 'every-hour', 'watch_files': ['input.txt']})
        timer.looked_just_now(timer_config, datetime(2026, 7, 9, 0, 0))
        (self.project / 'input.txt').write_text('stable')
        goal = self.create('background-does-not-interrupt')
        reply, change = collaboration_fixtures.reply, collaboration_fixtures.change
        responses = [
            reply(summary='Lin, I created the game. Please fix the lives count.', changes=[change('game.js', 'export const lives = 1;\n')], criteria_evidence=[{'criterion': 'Original objective is satisfied', 'evidence_refs': ['file:game.js']}]),
            reply(summary='Ada, I edited your file to use three lives.', changes=[change('game.js', 'export const lives = 3;\n')], criteria_evidence=[{'criterion': 'Original objective is satisfied', 'evidence_refs': ['file:game.js']}]),
            reply(summary='Lin, I inspected your edit and agree.'),
        ]
        observed = []
        def answer(number, route, kwargs):
            self.assertFalse(release.is_set())
            before = self.runtime.store.get(goal['goal_id'])
            shown = self.runtime.store.public(before)
            observed.extend(task['delivery_observation']['stage'] for task in shown['tasks'])
            self.assertEqual(before, self.runtime.store.get(goal['goal_id']))
            result = timer.run_what_is_due(timer_config, now=datetime(2026, 7, 9, number, 0))
            if number > 1:
                self.assertEqual(result['ran'][0]['outcome'], 'unchanged')
            self.assertTrue(index.search('bicycle'))
            return responses[number - 1]
        with mock.patch.object(semantic_memory, 'request_json', slow_embed):
            try:
                index.search('cycling')
                self.assertTrue(blocked.wait(1))
                result, seen = self.run_replies(goal, answer)
                self.assertEqual(result['status'], 'complete', result.get('note'))
                self.assertEqual([route for route, _ in seen], ['builder-route', 'peer-route', 'builder-route'])
                self.assertIn('I edited your file', seen[2][1])
                self.assertEqual((self.project / 'game.js').read_text(), 'export const lives = 3;\n')
                self.assertIn('dispatched', observed)
                self.assertFalse(release.is_set())
            finally:
                release.set()
                for state in list(semantic_memory._states.values()):
                    if state.get('thread'):
                        state['thread'].join(5)


if __name__ == '__main__':
    unittest.main()
