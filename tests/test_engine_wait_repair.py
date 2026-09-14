"""Portable regressions for status, observer backpressure and format loops."""
import copy
import json
import sys
import threading
import time
import unittest
from unittest import mock

from our_harness import collaboration_reply, goal_status, long_horizon
from our_harness.provider_activity import PublicStream
from our_harness.providers.codex_cli import _run_bounded
from our_harness.redaction import CredentialRedactor
from tests import test_long_horizon_dialogue as fixtures


class EngineWaitTests(unittest.TestCase):
    setUp = fixtures.LongHorizonDialogueTests.setUp
    create = fixtures.LongHorizonDialogueTests.create
    provider = fixtures.LongHorizonDialogueTests.provider
    run_replies = fixtures.LongHorizonDialogueTests.run_replies

    def test_status_does_not_change_running_paused_or_completed_goal_after_restart(self):
        goal = self.create("status")
        task = self.runtime.store.claim_ready(goal['goal_id'], 'worker')[0]
        self.runtime.store.record_dispatch(goal['goal_id'], task, 'digest')
        for state in ('running', 'paused', 'complete'):
            def arrange(document, _db):
                document['status'] = state
            self.runtime.store._mutate(goal['goal_id'], arrange)
            before = self.runtime.store.get(goal['goal_id'])
            with mock.patch.object(self.runtime, 'start_background') as start:
                result = self.runtime.control(goal['goal_id'], 'steer', {'text': 'what the hell is taking so long?'})
            self.assertIn('Checking status does not restart', result['status_response'])
            start.assert_not_called()
            self.assertEqual(before, self.runtime.store.get(goal['goal_id']))
        restarted = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(restarted.close)
        before = restarted.store.get(goal['goal_id'])
        restarted.control(goal['goal_id'], 'status')
        self.assertEqual(before, restarted.store.get(goal['goal_id']))

    def test_mixed_instructions_and_attachments_remain_work(self):
        for words in ('Status update', 'Any progress?', 'Are you still working?', 'why is this taking so long?'):
            self.assertTrue(goal_status.is_inquiry({'text': words}), words)
        for payload in ({'text': 'What is the status? Change the title.'},
                        {'text': 'status', 'attachments': [{'name': 'evidence'}]},
                        {'text': 'Implement a status update button'}, {'text': 'Stop and tell me the status'}):
            self.assertFalse(goal_status.is_inquiry(payload))

    def test_failed_format_repair_cannot_repeat_forever_and_peer_work_is_retained(self):
        goal = self.create('format-loop')
        def answer(number, route, _kwargs):
            if route == 'peer-route':
                return fixtures.reply(summary='Peer inspected the project.')
            return 'JSON\n{"action":"work","summary":"damaged\nsource"}'
        result, seen = self.run_replies(goal, answer)
        self.assertEqual(result['status'], 'paused')
        self.assertLessEqual(len(seen), 4)
        self.assertIn('repeated repair loop', result['note'])
        held = self.runtime.store.get(goal['goal_id'])
        self.assertTrue(any(t['state'] == 'complete' for t in held['tasks']))
        state = next(t for t in held['tasks'] if t['assigned_agent_id'] == 'builder')['format_repair_state']
        restarted = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(restarted.close)
        task = next(t for t in restarted.store.get(goal['goal_id'])['tasks'] if t['assigned_agent_id'] == 'builder')
        self.assertTrue(collaboration_reply.repair_exhausted(task, state['binding']))
        self.assertFalse(collaboration_reply.repair_exhausted(task, 'changed-route-or-schema'))

    def test_known_timeout_preserves_peer_and_resume_only_retries_failed_contribution(self):
        goal = self.create('timeout-recovery')
        calls = []
        def answer(_number, route, _kwargs):
            calls.append(route)
            if route == 'builder-route':
                return long_horizon.chat_lab.ChatError('Codex CLI provider timed out at its wall-clock deadline')
            return fixtures.reply()
        result, _ = self.run_replies(goal, answer)
        self.assertEqual(result['status'], 'paused')
        self.assertIn('Completed contributions are retained', result['note'])
        event = [e for e in self.runtime.store.events(goal['goal_id'])['events'] if e['type'] == 'goal_paused'][-1]
        self.assertEqual(event['payload']['reason'], 'provider_failure')
        peer = copy.deepcopy(next(t for t in self.runtime.store.get(goal['goal_id'])['tasks'] if t['assigned_agent_id'] == 'peer'))
        self.runtime.store.control(goal['goal_id'], 'resume')
        result, seen = self.run_replies(goal, [fixtures.reply()])
        self.assertEqual(result['status'], 'complete', result['note'])
        self.assertEqual([route for route, _ in seen], ['builder-route'])
        after = next(t for t in self.runtime.store.get(goal['goal_id'])['tasks'] if t['assigned_agent_id'] == 'peer')
        self.assertEqual(peer['attempts'], after['attempts'])

    def test_stalled_activity_archive_cannot_block_provider_pipe(self):
        release = threading.Event()
        entered = threading.Event()
        self.addCleanup(release.set)
        def sink(_value):
            entered.set()
            release.wait(10)
        stream = PublicStream('codex', sink, CredentialRedactor())
        code = "import json\nfor i in range(100): print(json.dumps({'type':'item.completed','item':{'id':str(i),'type':'agent_message','text':'x'*4000}}),flush=True)"
        began = time.monotonic()
        result = _run_bounded([sys.executable, '-c', code], cwd=self.base, stdin_text=None,
            timeout_seconds=3, max_output_bytes=1000000, public_stream=stream)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.exit_code, 0)
        self.assertLess(time.monotonic() - began, 3)
        self.assertTrue(entered.is_set())
        self.assertGreater(stream.observation_failures, 0)
        release.set()

    def test_failed_activity_archive_preserves_final_output_and_event_order(self):
        seen = []
        def sink(value):
            seen.append(value['id'])
            if len(seen) == 1:
                raise RuntimeError('archive temporarily unavailable')
        stream = PublicStream('codex', sink, CredentialRedactor())
        code = "import json\nfor i in range(3): print(json.dumps({'type':'item.completed','item':{'id':str(i),'type':'agent_message','text':'message'}}),flush=True)"
        result = _run_bounded([sys.executable, '-c', code], cwd=self.base, stdin_text=None,
            timeout_seconds=3, max_output_bytes=10000, public_stream=stream)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(seen, ['0', '1', '2'])
        self.assertEqual(stream.observation_failures, 1)

    def test_durable_pause_and_steering_stop_exact_subprocess_but_status_does_not(self):
        from our_harness import cancellation, goal_provider_control
        for control in ('pause', 'steer'):
            with self.subTest(control=control):
                goal = self.create('control-' + control)
                task = self.runtime.store.claim_ready(goal['goal_id'], 'worker')[0]
                started = threading.Event()
                errors = []
                def work():
                    try:
                        with goal_provider_control.watch(self.runtime.store, goal['goal_id'], task):
                            _run_bounded([sys.executable, '-c', "import json,time; print(json.dumps({'type':'item.completed','item':{'type':'agent_message','id':'ready','text':'ready'}}),flush=True);time.sleep(15)"],
                                cwd=self.base, stdin_text=None, timeout_seconds=20, max_output_bytes=10000,
                                public_stream=PublicStream('codex', lambda _: started.set(), CredentialRedactor()))
                    except Exception as exc:
                        errors.append(exc)
                worker = threading.Thread(target=work)
                worker.start()
                self.assertTrue(started.wait(3))
                self.runtime.control(goal['goal_id'], 'steer', {'text': 'Any progress?'})
                self.assertTrue(worker.is_alive())
                # A separate store/window supplies the durable control.
                other = long_horizon.GoalStore(self.config)
                began = time.monotonic()
                other.control(goal['goal_id'], control, {'text': 'Use a different layout.'})
                worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertLess(time.monotonic() - began, 3)
                self.assertIsInstance(errors[0], cancellation.ChatCancelled)
                # Release this isolated project claim before the next fixture.
                self.runtime.store.control(goal['goal_id'], 'cancel')

    def test_timeout_diagnostic_is_bounded_redacted_and_keeps_transport_stage(self):
        from tests.test_codex_cli_provider import CodexCLIProviderTests
        fixture = CodexCLIProviderTests()
        _, provider, _ = fixture.make_provider(self.base, mode='timeout')
        script = self.base / 'fake_codex.py'
        script.write_text(script.read_text(encoding='utf-8').replace('    time.sleep(5)',
            "    print(json.dumps({'type':'turn.started'}),flush=True)\n    print('password=secret-diagnostic',file=sys.stderr,flush=True)\n    time.sleep(5)"), encoding='utf-8')
        with self.assertRaisesRegex(Exception, 'Last transport events: turn.started') as caught:
            provider.complete(fixture.request(timeout=3))
        self.assertNotIn('secret-diagnostic', str(caught.exception))

    def test_optional_malformed_activity_cannot_mask_process_timeout(self):
        stream = PublicStream('codex', lambda _: None, CredentialRedactor())
        result = _run_bounded([sys.executable, '-c', "import time; print('not-json',flush=True); time.sleep(5)"],
            cwd=self.base, stdin_text=None, timeout_seconds=0.5, max_output_bytes=10000, public_stream=stream)
        self.assertTrue(result.timed_out)
        self.assertGreater(stream.observation_failures, 0)

    def test_public_transport_error_is_visible_without_private_fields(self):
        rows = []
        stream = PublicStream('codex', rows.append, CredentialRedactor())
        stream.feed((json.dumps({'type': 'turn.failed', 'error': {'message': 'Connection dropped password=private-token'},
            'raw_reasoning': 'PRIVATE_SENTINEL'}) + '\n').encode())
        stream.finish()
        self.assertIn('Connection dropped', rows[0]['text'])
        self.assertNotIn('private-token', str(rows))
        self.assertNotIn('PRIVATE_SENTINEL', str(rows))
