from __future__ import annotations

import copy
import json
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from our_harness import long_horizon
from our_harness.models import HarnessError
from tests import test_long_horizon_dialogue as fixtures


class GoalRecoveryTests(unittest.TestCase):
    setUp = fixtures.LongHorizonDialogueTests.setUp
    create = fixtures.LongHorizonDialogueTests.create
    provider = fixtures.LongHorizonDialogueTests.provider
    run_replies = fixtures.LongHorizonDialogueTests.run_replies

    def interrupted(self, *, codex=False):
        if codex:
            self.config.data['providers']['builder-route'] = {'kind': 'codex-cli', 'model': 'portable-model'}
        goal = self.create('interrupted-turn', policy={'agent_access_mode': 'full'})
        store = self.runtime.store
        task = store.claim_ready(goal['goal_id'], 'crashed-worker')[0]
        store.record_dispatch(goal['goal_id'], task, 'exact-prompt')
        store.release_scheduler(goal['goal_id'], 'crashed-worker')
        recovered = store.recover_dead(goal['goal_id'])
        self.assertEqual(recovered['status'], 'paused')
        return recovered, task

    @staticmethod
    def choice(goal):
        return {'expected_revision': goal['revision'], 'recovery': {
            'schema_version': 1, 'fingerprint': goal['resume_recovery']['fingerprint'], 'decision': 'retry_provider'}}

    def test_codex_resume_after_restart_finishes_real_scheduler_and_preserves_access(self):
        goal, old_task = self.interrupted(codex=True)
        self.assertTrue(goal['resume_recovery']['resume_safe'])
        budget = copy.deepcopy(goal['budget'])
        self.runtime.close()
        self.runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(self.runtime.close)
        store = self.runtime.store
        current = store.get(goal['goal_id'])
        peer = copy.deepcopy(current['tasks'][1])
        resumed = store.control(goal['goal_id'], 'resume', {'expected_revision': current['revision']})
        self.assertEqual(resumed['budget'], budget)
        self.assertEqual(resumed['tasks'][1], peer)
        self.assertEqual(resumed['agent_access']['mode'], 'full')
        with self.assertRaises(HarnessError):
            store.record_provider_reply(goal['goal_id'], old_task, phase='initial')
        result, seen = self.run_replies(resumed, [
            fixtures.reply(changes=[fixtures.change('recovered.js', 'export const recovered = true;\n')],
                criteria_evidence=[{'criterion': 'Original objective is satisfied', 'evidence_refs': ['file:recovered.js']}]),
            fixtures.reply(),
        ])
        self.assertEqual(result['status'], 'complete', result['note'])
        self.assertEqual(len(seen), 2)
        self.assertTrue((self.project / 'recovered.js').is_file())
        events = store.events(goal['goal_id'])['events']
        self.assertEqual(sum(e['type'] == 'interrupted_turn_superseded' for e in events), 1)
        self.assertEqual(sum(e['type'] == 'file_transaction_applied' for e in events), 1)

    def test_other_provider_requires_exact_explicit_choice_and_rejects_repeat(self):
        goal, _ = self.interrupted()
        self.assertFalse(goal['resume_recovery']['resume_safe'])
        self.assertTrue(goal['resume_recovery']['can_retry'])
        with self.assertRaisesRegex(HarnessError, 'recovery card'):
            self.runtime.store.control(goal['goal_id'], 'resume')
        resumed = self.runtime.store.control(goal['goal_id'], 'resume', self.choice(goal))
        self.assertEqual(resumed['status'], 'queued')
        self.assertFalse(resumed['tasks'][0]['outcome_unknown'])
        with self.assertRaises(HarnessError):
            self.runtime.store.control(goal['goal_id'], 'resume', self.choice(goal))

    def test_native_workspace_contract_never_inherits_read_only_inference_retry(self):
        goal, _ = self.interrupted(codex=True)
        self.assertTrue(goal['resume_recovery']['resume_safe'])
        document = self.runtime.store.get(goal['goal_id'])
        document['agent_workspace_contract'] = long_horizon.agent_workspaces.CONTRACT
        projection = self.runtime.store.resume_recovery(document)
        self.assertFalse(projection['resume_safe'])
        self.assertTrue(projection['can_retry'])
        self.assertNotEqual(projection['fingerprint'], goal['resume_recovery']['fingerprint'])

    def test_stale_permission_change_invalidates_recovery_but_does_not_erase_blocker(self):
        goal, _ = self.interrupted()
        saved = self.runtime.store.update_access(goal['goal_id'], expected_revision=goal['revision'], mode='ask')
        self.assertTrue(saved['tasks'][0]['outcome_unknown'])
        with self.assertRaises(HarnessError):
            self.runtime.store.control(goal['goal_id'], 'resume', self.choice(goal))
        with self.assertRaisesRegex(HarnessError, 'call changed'):
            self.runtime.store.control(goal['goal_id'], 'resume', {
                **self.choice(goal), 'expected_revision': saved['revision']})
        fresh = self.runtime.store.public(self.runtime.store.get(goal['goal_id']))
        resumed = self.runtime.store.control(goal['goal_id'], 'resume', self.choice(fresh))
        self.assertEqual(resumed['agent_access']['mode'], 'ask')

    def test_changed_route_and_project_binding_never_adopt_automatic_retry(self):
        goal, _ = self.interrupted(codex=True)
        self.config.data['providers']['builder-route']['model'] = 'different-model'
        projected = self.runtime.store.public(self.runtime.store.get(goal['goal_id']))
        self.assertFalse(projected['resume_recovery']['can_retry'])
        with self.assertRaises(HarnessError):
            self.runtime.store.control(goal['goal_id'], 'resume', self.choice(goal))
        self.assertTrue(self.runtime.store.get(goal['goal_id'])['tasks'][0]['outcome_unknown'])

    def test_saved_action_or_transaction_is_not_discarded_by_provider_recovery(self):
        goal, _ = self.interrupted(codex=True)
        for field, value in [('pending_action', {'changes': [{'path': 'kept.txt'}]}),
                             ('pending_transaction', {'state': 'prepared', 'transaction_id': 'held'})]:
            with self.subTest(field=field):
                document = self.runtime.store.get(goal['goal_id'])
                document['tasks'][0][field] = value
                projection = self.runtime.store.resume_recovery(document)
                self.assertFalse(projection['can_retry'])
                self.assertEqual(projection['items'][0]['kind'], 'saved_work')
                with self.runtime.store._connect() as db, self.assertRaises(HarnessError):
                    self.runtime.store._resume_interrupted_turns(document, db, {
                        'schema_version': 1, 'fingerprint': projection['fingerprint'], 'decision': 'retry_provider'})
                self.assertEqual(document['tasks'][0][field], value)

    def test_live_worker_never_gets_recovery_choice(self):
        goal, _ = self.interrupted(codex=True)
        with mock.patch.object(self.runtime.store, '_scheduler_live', return_value=True):
            projection = self.runtime.store.resume_recovery(self.runtime.store.get(goal['goal_id']))
        self.assertFalse(projection['can_retry'])

    def test_failed_resume_rolls_back_recovery_and_completed_peer_is_preserved(self):
        goal, _ = self.interrupted(codex=True)
        store = self.runtime.store
        def finish_peer(document, db):
            document['tasks'][1].update(state='complete', summary='Earlier peer work is saved',
                evidence=['Existing project inspected'], provider_effect_state='never_dispatched')
        store._mutate(goal['goal_id'], finish_peer)
        before = store.get(goal['goal_id'])
        with mock.patch.object(store, '_adopt_project_verification_settings', side_effect=HarnessError('Checks changed')):
            with self.assertRaisesRegex(HarnessError, 'Checks changed'):
                store.control(goal['goal_id'], 'resume', project_verification_settings={})
        self.assertEqual(store.get(goal['goal_id']), before)
        self.assertFalse(any(e['type'] == 'interrupted_turn_superseded' for e in store.events(goal['goal_id'])['events']))
        resumed = store.control(goal['goal_id'], 'resume')
        self.assertEqual(resumed['tasks'][1], before['tasks'][1])
        self.assertEqual(resumed['agent_access'], store.public(before)['agent_access'])

    def test_http_recovery_checks_chat_identity_then_runs_to_completion(self):
        from our_harness.server import HarnessHTTPServer
        goal, _ = self.interrupted()
        panel = HarnessHTTPServer(('127.0.0.1', 0), self.config)
        panel._long_horizon = self.runtime
        self.addCleanup(panel.server_close)
        threading.Thread(target=panel.serve_forever, daemon=True).start()
        self.addCleanup(panel.shutdown)
        body = {'goal_id': goal['goal_id'], 'chat_id': 'chat-interrupted-turn',
                'project_id': 'game', 'participant_ids': ['builder', 'peer'],
                'action': 'resume', 'payload': self.choice(goal)}
        def post(value, token=True):
            request = urllib.request.Request(f'http://127.0.0.1:{panel.server_address[1]}/api/long-horizon/control',
                data=json.dumps(value).encode(), headers={'Content-Type': 'application/json',
                    **({'X-Harness-Token': panel.token} if token else {})})
            try:
                with urllib.request.urlopen(request) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                return error.code, json.load(error)
        self.assertNotEqual(post(body, False)[0], 200)
        self.assertEqual(post({**body, 'chat_id': 'different-chat'})[0], 400)
        seen = []
        def run(goal_id, **kwargs):
            return self.runtime.run(goal_id)
        with mock.patch.object(panel, 'swarm_standing', return_value={'board': self.board}), \
                mock.patch.object(self.runtime, 'start_background', side_effect=run), \
                mock.patch.object(long_horizon.chat_lab, 'ask_once', side_effect=self.provider([fixtures.reply(), fixtures.reply()], seen)), \
                mock.patch.object(long_horizon.swarm_work, '_run_selected_project_verification', return_value={'status': 'passed', 'basis': 'fixture check'}):
            status, response = post(body)
        self.assertEqual(status, 200, response)
        self.assertEqual(len(seen), 2)
        self.assertEqual(self.runtime.store.get(goal['goal_id'])['status'], 'complete')


if __name__ == '__main__':
    unittest.main()
