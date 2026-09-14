"""Portable incident regressions: malformed proposals are never delivery."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from our_harness import collaboration_reply, long_horizon
from our_harness.providers import codex_cli
from tests import test_long_horizon_dialogue as fixtures
from tests import test_codex_cli_provider as cli_fixtures


class DeliveryRegressionTests(unittest.TestCase):
    setUp = fixtures.LongHorizonDialogueTests.setUp
    create = fixtures.LongHorizonDialogueTests.create
    run_replies = fixtures.LongHorizonDialogueTests.run_replies

    def provider(self, responses, seen):
        def ask(_config, route, _text, **kwargs):
            seen.append((route, kwargs['context']))
            kwargs['before_provider_dispatch']('initial')
            value = responses[len(seen) - 1]
            if isinstance(value, Exception):
                raise value
            kwargs['after_provider_response']('initial')
            return {'text': json.dumps(value) if isinstance(value, dict) else value}
        return ask

    def test_broken_action_is_not_speech_completion_or_a_file_write(self):
        goal = self.create('broken-action')
        raw = 'JSON\n{"action":"complete","summary":"Created pages","changes":[{"path":"page.html","content":"broken\nsource"}]}'
        result, seen = self.run_replies(goal, [raw, raw, fixtures.reply()])
        self.assertEqual(result['status'], 'paused', result['note'])
        self.assertIn('Proposed edits were not applied', result['note'])
        self.assertEqual(len(seen), 3)
        self.assertFalse((self.project / 'page.html').exists())
        held = self.runtime.store.get(goal['goal_id'])
        self.assertEqual(next(t for t in held['tasks'] if t['assigned_agent_id'] == 'builder')['state'], 'blocked')
        self.assertNotIn(raw, json.dumps(held.get('dialogue', {})))
        restarted = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.store.get(goal['goal_id'])['status'], 'paused')
        self.assertNotIn('"action"', collaboration_reply.continuation({'text': raw})['summary'])

    def test_format_repair_cannot_drop_changes_and_claim_they_were_saved(self):
        goal = self.create('dropped-files')
        raw = '{"action":"complete","changes":[{"path":"guide.html","content":"unfinished'
        result, _ = self.run_replies(goal, [raw, fixtures.reply(summary='Created guide.html'), fixtures.reply()])
        self.assertEqual(result['status'], 'paused', result['note'])
        self.assertIn('dropped proposed file changes', result['note'])
        self.assertFalse((self.project / 'guide.html').exists())

    def test_corrected_complete_files_are_applied_and_survive_restart(self):
        goal = self.create('real-delivery', policy={'agent_access_mode': 'full'})
        pages = {'guide.html': '<!doctype html>\n<h1>Delivery & checks</h1>',
                 'ops.html': '<!doctype html>\n<a href="guide.html">Guide</a>'}
        corrected = fixtures.reply(changes=[fixtures.change(k, v) for k, v in pages.items()],
                                   criteria_evidence=[{'criterion': 'Original objective is satisfied',
                                                       'evidence_refs': ['file:' + k for k in pages]}])
        result, seen = self.run_replies(goal, ['{"action":"complete","changes":[{', corrected, fixtures.reply()])
        self.assertEqual(result['status'], 'complete', result['note'])
        for name, contents in pages.items():
            self.assertEqual((self.project / name).read_text(encoding='utf-8'), contents)
        restarted = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.store.get(goal['goal_id'])['status'], 'complete')
        self.assertIn('NONE of the prior JSON', seen[1][1])

    def test_explicit_resume_reopens_legacy_undelivered_contribution_once(self):
        goal = self.create('legacy-undelivered')
        result, _ = self.run_replies(goal, [long_horizon.chat_lab.ChatError('provider timed out'), fixtures.reply()])
        def legacy(document, _db):
            task = next(t for t in document['tasks'] if t['assigned_agent_id'] == 'peer')
            document['dialogue']['messages'].append({'task_id': task['id'], 'action': 'work',
                'summary': 'JSON\n{"action":"complete","changes":[{"path":"lost.html"}]}',
                'objective_epoch': 1})
        self.runtime.store._mutate(goal['goal_id'], legacy)
        restarted = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(restarted.close)
        resumed = restarted.store.control(goal['goal_id'], 'resume')
        held = restarted.store.get(goal['goal_id'])
        builder = next(t for t in held['tasks'] if t['assigned_agent_id'] == 'peer')
        self.assertEqual(builder['state'], 'ready')
        self.assertIn('delivery_recovery', builder)
        self.assertEqual(result['budget'], resumed['budget'])
        builder['state'] = 'complete'
        self.assertEqual(collaboration_reply.undelivered_on_resume(held), [])
        held['objective_epoch'] = 2
        self.assertEqual(collaboration_reply.undelivered_on_resume(held), [])

    def test_recovery_preserves_actual_changes_and_does_not_execute_dialogue(self):
        document = {'goal_id': 'arbitrary', 'tasks': [{'id': 't', 'state': 'complete',
            'artifacts': [{'changes': [{'path': 'saved.html'}]}]}], 'dialogue': {'messages': [
                {'task_id': 't', 'action': 'work', 'summary': '{"changes":[{"path":"../escape"}]}'}]}}
        self.assertEqual(collaboration_reply.undelivered_on_resume(document), [])


class ConnectionRegressionTests(unittest.TestCase):
    def test_network_environment_survives_without_inheriting_provider_keys(self):
        values = {'HTTPS_PROXY': 'http://proxy.example:8080', 'NO_PROXY': 'localhost',
                  'SSL_CERT_FILE': '/arbitrary/company.pem', 'OPENAI_API_KEY': 'do-not-inherit'}
        with mock.patch.dict(os.environ, values, clear=True):
            actual = codex_cli._minimal_codex_environment()
        for key in ('HTTPS_PROXY', 'NO_PROXY', 'SSL_CERT_FILE'):
            self.assertEqual(actual[key], values[key])
        self.assertNotIn('OPENAI_API_KEY', actual)

    def test_changed_network_or_command_invalidates_cached_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper = cli_fixtures.CodexCLIProviderTests()
            _, provider, _ = helper.make_provider(root)
            with mock.patch.object(codex_cli, 'codex_cli_preflight', wraps=codex_cli.codex_cli_preflight) as preflight:
                provider.complete(helper.request(12))
                provider.complete(helper.request(12))
                self.assertEqual(preflight.call_count, 1)
                with mock.patch.dict(os.environ, {'NO_PROXY': 'changed.example'}):
                    provider.complete(helper.request(12))
                self.assertEqual(preflight.call_count, 2)
                provider.settings['command'] += ['--arbitrary-config-version', '2']
                provider.complete(helper.request(12))
                self.assertEqual(preflight.call_count, 3)
