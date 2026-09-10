from __future__ import annotations

import copy
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from our_harness import collaboration_reply, long_horizon
from our_harness.models import HarnessError, ProviderResponse
from tests import test_long_horizon_dialogue as fixtures


class CollaborationReplyTests(unittest.TestCase):
    setUp = fixtures.LongHorizonDialogueTests.setUp
    create = fixtures.LongHorizonDialogueTests.create
    run_replies = fixtures.LongHorizonDialogueTests.run_replies
    provider = fixtures.LongHorizonDialogueTests.provider

    def test_cli_shape_repair_continues_same_task_then_peer(self):
        goal = self.create('cli-format-repair')
        result, seen = self.run_replies(goal, ['A useful plain reply', fixtures.reply(), fixtures.reply()])
        self.assertEqual(result['status'], 'complete', result['note'])
        self.assertEqual([one[0] for one in seen], ['builder-route', 'builder-route', 'peer-route'])
        self.assertIn('A useful plain reply', seen[1][1])
        self.assertIn('do not', seen[1][1].lower())

    def test_real_chat_dispatch_delivers_malformed_transport_output_to_team_repair(self):
        goal = self.create('transport-format-repair')
        requests = []
        replies = iter(['Please inspect my work.', json.dumps(fixtures.reply()), json.dumps(fixtures.reply())])
        class Transport:
            structured_retry_is_safe = False
            def complete(self, request):
                requests.append(request)
                return ProviderResponse(text=next(replies), finish_reason='stop')
        with mock.patch.dict(os.environ, {'FIXTURE_KEY': 'fixture-key'}), \
                mock.patch.object(long_horizon.chat_lab, 'create_provider', return_value=Transport()), \
                mock.patch.object(long_horizon.swarm_work, '_run_selected_project_verification', return_value={'status': 'passed', 'basis': 'fixture'}):
            result = self.runtime.run(goal['goal_id'])
        self.assertEqual(result['status'], 'complete', result['note'])
        self.assertEqual(len(requests), 3)
        self.assertIn('FORMAT CORRECTION ONLY', requests[1].dynamic_context)
        self.assertEqual(result['budget']['provider_calls'], 3)

    def test_prose_fallback_yields_to_peer_and_never_completes_without_validated_agreement(self):
        goal = self.create('prose-handoff')
        result, seen = self.run_replies(goal, [
            'Lin, please inspect the controls.', 'Still plain text', fixtures.reply(), fixtures.reply(),
        ])
        self.assertEqual(result['status'], 'complete', result['note'])
        self.assertEqual([one[0] for one in seen], ['builder-route', 'builder-route', 'peer-route', 'builder-route'])
        self.assertIn('Lin, please inspect the controls.', seen[2][1])
        events = self.runtime.store.events(goal['goal_id'])['events']
        self.assertEqual(sum(one['type'] == 'collaboration_prose_continued' for one in events), 1)
        self.assertEqual(result['budget']['provider_calls'], 4)
        for text in ['Everything is complete', '{"action":"complete","changes":[{"path":"../escape"}]}', '']:
            action = collaboration_reply.continuation({'text': text})
            self.assertEqual(action['action'], 'work')
            self.assertEqual(action['changes'], [])
            self.assertEqual(action['criteria_evidence'], [])

    def test_native_draft_is_collected_after_inspect_only_repair(self):
        goal = self.create('native-format-repair', policy={'agent_access_mode': 'full'})
        task = self.runtime.store.claim_ready(goal['goal_id'], 'native-test')[0]
        seen = []
        class Workspace:
            root = self.project
            baseline = {}
            def collect_action(this, action, **kwargs):
                self.assertEqual((this.root / 'draft.txt').read_text(), 'native work retained')
                return {**action, 'changes': []}
        def answer(_config, _route, _text, **kwargs):
            seen.append(kwargs)
            kwargs['before_provider_dispatch']('initial')
            if len(seen) == 1:
                Path(kwargs['working_directory'], 'draft.txt').write_text('native work retained')
            kwargs['after_provider_response']('initial')
            return {'text': 'Please inspect my saved draft.' if len(seen) == 1 else json.dumps(fixtures.reply('work'))}
        with mock.patch.object(long_horizon.chat_lab, 'ask_once', side_effect=answer):
            _, action = self.runtime._execute_in_workspace(goal['goal_id'], task['id'], agent_workspace=Workspace())
        self.assertEqual([one['native_execution'] for one in seen], ['work', 'inspect'])
        self.assertEqual(action['action'], 'work')

    def test_legacy_format_failure_force_proceeds_after_restart_with_same_goal_and_budget(self):
        goal = self.create('legacy-format', policy={'agent_access_mode': 'full'})
        store = self.runtime.store
        task = store.claim_ready(goal['goal_id'], 'old-worker')[0]
        store.record_dispatch(goal['goal_id'], task, 'original-prompt')
        store.record_provider_reply(goal['goal_id'], task, phase='initial')
        store.fail_task(goal['goal_id'], task, HarnessError('Ada did not return the structured collaboration result Nexus requested'))
        store.release_scheduler(goal['goal_id'], 'old-worker')
        self.runtime.close()
        self.runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(self.runtime.close)
        store = self.runtime.store
        before = store.public(store.get(goal['goal_id']))
        self.assertTrue(before['resume_recovery']['can_retry'])
        resumed = store.control(goal['goal_id'], 'resume', {'expected_revision': before['revision'], 'force_proceed': True})
        self.assertEqual(resumed['budget'], before['budget'])
        self.assertEqual(resumed['agent_access']['mode'], 'full')
        result, seen = self.run_replies(resumed, [fixtures.reply(), fixtures.reply()])
        self.assertEqual(result['status'], 'complete', result['note'])
        self.assertTrue(any('choose a different useful approach' in context for _, context in seen))
        with self.assertRaises(HarnessError):
            store.control(goal['goal_id'], 'resume', {'expected_revision': before['revision'], 'force_proceed': True})

    def test_force_does_not_discard_pending_files_or_adopt_changed_provider(self):
        goal = self.create('force-boundary')
        self.runtime.store.control(goal['goal_id'], 'pause')
        before = self.runtime.store.get(goal['goal_id'])
        self.config.data['providers']['builder-route']['model'] = 'changed-model'
        with self.assertRaises(HarnessError):
            self.runtime.store.control(goal['goal_id'], 'resume', {'expected_revision': before['revision'], 'force_proceed': True})
        self.assertEqual(self.runtime.store.get(goal['goal_id']), before)

    def test_force_reopens_exhausted_correction_with_audited_cumulative_usage(self):
        from tests.test_long_horizon_protocol_recovery import LongHorizonProtocolRecoveryTests
        goal = self.create('force-exhausted')
        bad = LongHorizonProtocolRecoveryTests.invalid()
        paused, _ = self.run_replies(goal, lambda number, *_: bad if number <= 3 else fixtures.reply())
        self.assertEqual(paused['status'], 'paused')
        before = self.runtime.store.get(goal['goal_id'])
        self.assertEqual(before['tasks'][0]['protocol_recovery']['state'], 'exhausted')
        self.runtime.store.control(goal['goal_id'], 'resume', {'expected_revision': before['revision'], 'force_proceed': True})
        result, seen = self.run_replies(goal, [fixtures.reply(), fixtures.reply()])
        self.assertEqual(result['status'], 'complete', result['note'])
        self.assertEqual(result['tasks'][0]['protocol_recovery']['cumulative_attempts'], 3)
        self.assertEqual(result['budget']['provider_calls'], before['budget']['provider_calls'] + len(seen))
        self.assertFalse((self.project / 'rejected.js').exists())

    def test_force_keeps_an_unsettled_file_transaction_and_requires_its_recovery(self):
        from tests.test_goal_recovery import GoalRecoveryTests
        goal, _ = GoalRecoveryTests.interrupted(self)
        store = self.runtime.store
        def pending(document, _db):
            document['tasks'][0]['pending_action'] = {'changes': [{'path': 'kept.txt'}]}
        store._mutate(goal['goal_id'], pending)
        before = store.get(goal['goal_id'])
        with self.assertRaisesRegex(HarnessError, 'recovery card'):
            store.control(goal['goal_id'], 'resume', {'expected_revision': before['revision'], 'force_proceed': True})
        self.assertEqual(store.get(goal['goal_id']), before)


if __name__ == '__main__':
    unittest.main()
