"""Decision memory and terminal receipts, adapted from t3code's input contracts.

Upstream eb115063634c416c6362cc407f8572cb0c136ddf:
apps/web/src/pendingUserInput.ts and packages/client-runtime/src/pendingRequests.ts.
Nexus-specific regressions exercise authenticated goals, private recipients,
cosmetic repeated questions and response-loss retries rather than UI strings.
"""
import copy
import hashlib
import json
import unittest
from unittest import mock

from our_harness import goal_decisions, long_horizon, user_questions
from our_harness.models import HarnessError
from tests import test_long_horizon as single
from tests import test_long_horizon_dialogue as fixtures


def question(identity="folder", prompt="Which folder should contain the new game?"):
    return {"id": identity, "prompt": prompt, "multiple": False, "allow_other": True,
            "options": [{"label": "Selected project", "description": "Use the saved destination", "recommended": True}]}


def answer(q, text="Use the exact selected project", audience="requesting_agent"):
    return {"schema_version": 1, "audience": audience, "questions": [
        {"question_id": q["id"], "selected_options": [], "text": text},
    ]}


class RawAnswerTests(unittest.TestCase):
    def test_repeat_normalization_preserves_exact_technical_literals(self):
        cases = [
            ('https://Example.test/Stage/A', 'https://Example.test/Stage/a'),
            ('https://example.test/game?q=AbC', 'https://example.test/game?q=abc'),
            ('https://example.test/game#Stage-A', 'https://example.test/game#Stage-a'),
            ('https://example.test/A%2fB', 'https://example.test/A%2FB'),
            ('a/b', 'a-b'), ('Game.js', 'game.js'),
            (r'"C:\Mixed Case\Game"', r'"C:\Mixed Case\game"'),
            ('`/selected/Case Sensitive/Game`', '`/selected/Case Sensitive/game`'),
        ]
        for first, second in cases:
            with self.subTest(first=first, second=second):
                keys = [goal_decisions.question_key('requirement_ambiguity', [question(prompt='Use ' + one + '?')])
                        for one in (first, second)]
                self.assertNotEqual(*keys)
        self.assertEqual(goal_decisions.question_key('requirement_ambiguity', [question()]),
                         goal_decisions.question_key('requirement_ambiguity', [question('changed-id',
                            '  WHICH folder should contain the new game?!  ')]))

    def test_exact_typed_paths_and_urls_survive_answer_projection(self):
        q = question()
        for text in (r'C:\Mixed Case\Game #2\new folder',
                     'file:///C:/Mixed%20Case/PLOQQIZ/Game%23Two/index.html#Stage-A',
                     'https://Example.test/Case%2fKept?q=AbC%2Fz&mode=Play#Chapter-2'):
            with self.subTest(text=text):
                structured = user_questions.answer_record([q], answer(q, text))
                legacy = user_questions.answer_record([q], q['prompt'] + ': ' + text)
                self.assertEqual(structured['answer_text'], text)
                self.assertEqual(structured['answers'][0]['text'], text)
                self.assertEqual(legacy['answer_text'], text)

    def test_question_framing_and_typed_answer_remain_separate(self):
        q = question(prompt="Use WRONG_SCREENSHOT_ROOT or temporary transport?")
        text = "  Make a new folder inside CORRECT_TYPED_ROOT.\nKeep this line.  "
        result = user_questions.answer_record([q], answer(q, text, "team"))
        self.assertEqual(result["answers"][0]["text"], text)
        self.assertNotIn("WRONG_SCREENSHOT_ROOT", result["answer_text"])
        self.assertEqual(result["questions"][0]["prompt"], q["prompt"])
        legacy = user_questions.answer_record([q], q["prompt"] + ": " + text)
        self.assertEqual(legacy["answers"][0]["text"], text.rstrip())
        self.assertEqual(legacy["audience"], "requesting_agent")
        self.assertTrue(legacy["raw_answer"].startswith(q["prompt"]))
        explicit = user_questions.answer_record([q], answer(q, q["prompt"] + ": my literal text"))
        self.assertTrue(explicit["answer_text"].startswith(q["prompt"]))

    def test_invalid_structured_answers_fail_closed(self):
        q = question()
        cases = [None, 1, True, {}, [], "", " " * 10, "x" * 20001, " " * 20001 + "A"]
        base = answer(q)
        cases += [{**base, "schema_version": True}, {**base, "audience": "everyone"}, {**base, "extra": True},
                  {**base, "questions": []}, {**base, "questions": base["questions"] * 2}]
        for update in ({"question_id": "unknown"}, {"text": 4}, {"text": ""},
                       {"selected_options": ["unknown"]}, {"selected_options": ["Selected project"] * 2},
                       {"text": "x" * 20001}):
            cases.append({**base, "questions": [{**base["questions"][0], **update}]})
        for value in cases:
            with self.subTest(value=str(value)[:80]), self.assertRaises(HarnessError):
                user_questions.answer_record([q], value)
        fixed = {**q, "allow_other": False}
        with self.assertRaisesRegex(HarnessError, "custom"):
            user_questions.answer_record([fixed], base)
        selected = answer(fixed, "")
        selected["questions"][0]["selected_options"] = ["Selected project"]
        self.assertEqual(user_questions.answer_record([fixed], selected)["answer_text"], "Selected project")


class GoalDecisionTests(unittest.TestCase):
    setUp = fixtures.LongHorizonDialogueTests.setUp
    create = fixtures.LongHorizonDialogueTests.create

    def ask(self, goal, q=None, reason="requirement_ambiguity"):
        q = q or question()
        task = self.runtime.store.claim_ready(goal["goal_id"], "decision-worker")[0]
        ids = self.runtime.store.apply_action(goal["goal_id"], task, single.action(
            "ask_user", interrupt_reason=reason, questions=[q],
        ))
        return task, ids, self.runtime.store.get(goal["goal_id"])

    def submit(self, goal, ids, value, request_id="logical-answer-request"):
        envelope = {"expected_revision": goal["revision"], "pending_ids": ids,
                    "answers": {ids[0]: value}, "request_id": request_id}
        self.assertTrue(self.runtime.store.resolve_interrupts(goal["goal_id"], envelope))
        return self.runtime.store.get(goal["goal_id"]), envelope

    def test_private_and_explicit_team_answers_keep_their_recipient_scope(self):
        for audience in ("requesting_agent", "team"):
            with self.subTest(audience=audience):
                goal = self.create("scope-" + audience, isolated_workspace=True)
                task, ids, held = self.ask(goal)
                current, _ = self.submit(held, ids, answer(question(), "SCOPED_ANSWER", audience))
                peer = next(one for one in current["tasks"] if one["assigned_agent_id"] != task["assigned_agent_id"])
                self.assertIn("SCOPED_ANSWER", self.runtime._agent_context(current, task))
                self.assertEqual("SCOPED_ANSWER" in self.runtime._agent_context(current, peer), audience == "team")
                archived = self.runtime.store.dialogue_history(goal["goal_id"], viewer_agent_id=peer["assigned_agent_id"])
                self.assertEqual(any(one["summary"] == "SCOPED_ANSWER" for one in archived["messages"]), audience == "team")

    def test_real_review_packet_keeps_private_answers_scoped_after_restart(self):
        for audience in ('requesting_agent', 'team', 'legacy'):
            with self.subTest(audience=audience):
                goal = self.create('review-scope-' + audience, isolated_workspace=True)
                task, ids, held = self.ask(goal)
                value = 'REVIEW_ANSWER_CANARY' if audience == 'legacy' else answer(question(), 'REVIEW_ANSWER_CANARY', audience)
                current, _ = self.submit(held, ids, value)
                self.runtime.store.control(goal['goal_id'], 'message', {
                    'task_id': task['id'], 'agent_id': task['assigned_agent_id'], 'text': 'PRIVATE_REVIEW_STEERING_CANARY'})
                if audience == 'legacy':
                    def legacy(document, db):
                        document['interrupts'][0].pop('answer_record', None)
                        next(one for one in document['tasks'] if one['id'] == task['id']).pop('user_evidence_scopes', None)
                    self.runtime.store._mutate(goal['goal_id'], legacy)
                builder = self.runtime.store.claim_ready(goal['goal_id'], 'review-owner')[0]
                staged, _ = self.runtime.store.stage_review_if_needed(goal['goal_id'], builder,
                    fixtures.reply('request_review', 'Inspect the useful technical proposal.', risk='high'))
                self.assertTrue(staged)
                restarted = long_horizon.LongHorizonRuntime(self.config)
                self.addCleanup(restarted.close)
                current = restarted.store.get(goal['goal_id'])
                review = next(one for one in current['tasks'] if one.get('review_of') == task['id'])
                context = restarted._agent_context(current, review)
                self.assertIn('TARGETED REVIEW PACKET', context)
                self.assertIn('verified-no-change: inspected the requested behavior', context)
                self.assertEqual('REVIEW_ANSWER_CANARY' in context, audience == 'team')
                self.assertNotIn('PRIVATE_REVIEW_STEERING_CANARY', context)
                self.assertIn('REVIEW_ANSWER_CANARY', restarted._agent_context(current,
                    next(one for one in current['tasks'] if one['id'] == task['id'])))
                self.assertIn('User decision: REVIEW_ANSWER_CANARY',
                    next(one for one in current['tasks'] if one['id'] == task['id'])['evidence'])

    def test_real_handoff_does_not_readdress_private_answer_or_steering(self):
        for legacy in (False, True):
            for audience in ('requesting_agent', 'team'):
                with self.subTest(legacy=legacy, audience=audience):
                    goal = self.create('handoff-' + str(legacy) + audience, isolated_workspace=True,
                                       require_all_participants=False)
                    task, ids, held = self.ask(goal)
                    current, _ = self.submit(held, ids, answer(question(), 'HANDOFF_ANSWER_CANARY', audience))
                    self.runtime.store.control(goal['goal_id'], 'message', {
                        'task_id': task['id'], 'agent_id': task['assigned_agent_id'], 'text': 'DIRECTED_STEERING_CANARY'})
                    self.runtime.store.control(goal['goal_id'], 'steer', {
                        'task_id': task['id'], 'text': 'TEAM_STEERING_CANARY'})
                    if legacy:
                        def legacy_scope(document, db):
                            next(one for one in document['tasks'] if one['id'] == task['id']).pop('user_evidence_scopes', None)
                            if audience == 'requesting_agent':
                                document['interrupts'][0].pop('answer_record', None)
                        self.runtime.store._mutate(goal['goal_id'], legacy_scope)
                    def unknown_and_technical(document, db):
                        owner = next(one for one in document['tasks'] if one['id'] == task['id'])
                        owner['evidence'].extend(['User steering: UNKNOWN_LEGACY_RECIPIENT_CANARY', 'USEFUL_TECHNICAL_EVIDENCE'])
                    self.runtime.store._mutate(goal['goal_id'], unknown_and_technical)
                    held = self.runtime.store.get(goal['goal_id'])
                    owner = next(one for one in held['tasks'] if one['id'] == task['id'])
                    context = self.runtime._agent_context(held, owner)
                    self.assertIn('HANDOFF_ANSWER_CANARY', context)
                    self.assertIn('DIRECTED_STEERING_CANARY', context)
                    self.assertNotIn('UNKNOWN_LEGACY_RECIPIENT_CANARY', context)
                    builder = self.runtime.store.claim_ready(goal['goal_id'], 'handoff-owner')[0]
                    self.runtime.store.apply_action(goal['goal_id'], builder,
                        fixtures.reply('handoff', 'Peer, continue the implementation.', handoff_agent_id='peer'))
                    restarted = long_horizon.LongHorizonRuntime(self.config)
                    self.addCleanup(restarted.close)
                    held = restarted.store.get(goal['goal_id'])
                    owner = next(one for one in held['tasks'] if one['id'] == task['id'])
                    self.assertEqual(owner['assigned_agent_id'], 'peer')
                    context = restarted._agent_context(held, owner)
                    self.assertEqual('HANDOFF_ANSWER_CANARY' in context, audience == 'team')
                    self.assertNotIn('DIRECTED_STEERING_CANARY', context)
                    self.assertNotIn('UNKNOWN_LEGACY_RECIPIENT_CANARY', context)
                    self.assertIn('TEAM_STEERING_CANARY', context)
                    self.assertIn('USEFUL_TECHNICAL_EVIDENCE', context)
                    self.assertIn('User steering: DIRECTED_STEERING_CANARY', owner['evidence'])
                    private_history = restarted.store.dialogue_history(goal['goal_id'], viewer_agent_id='builder')
                    self.assertTrue(any(one['summary'] == 'DIRECTED_STEERING_CANARY' for one in private_history['messages']))

    def test_persisted_private_tool_pages_follow_original_recipient_after_handoff(self):
        for handoff in (False, True):
            with self.subTest(handoff=handoff):
                goal = self.create('cached-recipient-' + str(handoff), isolated_workspace=True,
                                   require_all_participants=False)
                _task, ids, held = self.ask(goal)
                self.submit(held, ids, answer(question(), 'CACHED_PRIVATE_PAGE_CANARY'))
                builder = self.runtime.store.claim_ready(goal['goal_id'], 'cached-owner')[0]
                calls = [
                    {'call_id': 'decisions', 'name': 'read_user_decisions', 'arguments': {
                        'after': 0, 'limit': 10, 'decision_id': '', 'offset': 0, 'character_limit': 12000}},
                    {'call_id': 'conversation', 'name': 'read_shared_conversation', 'arguments': {
                        'after': 0, 'limit': 10, 'message_id': '', 'offset': 0, 'character_limit': 12000}},
                ]
                responses = [fixtures.reply('work', 'I am reading prior user input.', tool_calls=calls),
                    fixtures.reply('handoff' if handoff else 'work', 'Continue the technical work.',
                                   handoff_agent_id='peer' if handoff else '')]
                seen = []
                provider = fixtures.LongHorizonDialogueTests.provider(self, responses, seen)
                with mock.patch.object(long_horizon.chat_lab, 'ask_once', side_effect=provider) as sent:
                    _claimed, result = self.runtime._execute_one(goal['goal_id'], builder['id'])
                original_key = sent.call_args_list[0].kwargs['conversation_key']
                self.assertTrue(original_key.startswith('long-goal-v2-'))
                self.assertLess(len(original_key), 128)
                self.assertTrue(all(one.kwargs['conversation_key'] == original_key for one in sent.call_args_list))
                self.assertIn('CACHED_PRIVATE_PAGE_CANARY', seen[1][1].split('CONTEXT TOOL RESULTS', 1)[1])
                self.runtime.store.apply_action(goal['goal_id'], builder, result)
                self.runtime.store.release_scheduler(goal['goal_id'], 'cached-owner')
                restarted = long_horizon.LongHorizonRuntime(self.config)
                self.addCleanup(restarted.close)
                current = restarted.store.get(goal['goal_id'])
                owner = next(one for one in current['tasks'] if one['id'] == builder['id'])
                self.assertIn('CACHED_PRIVATE_PAGE_CANARY', json.dumps(owner['context_steps'][0]['results']))
                claimed = restarted.store.claim_ready(goal['goal_id'], 'resumed-owner')[0]
                next_seen = []
                next_provider = fixtures.LongHorizonDialogueTests.provider(self,
                    [fixtures.reply(summary='The technical work is complete.')], next_seen)
                with mock.patch.object(long_horizon.chat_lab, 'ask_once', side_effect=next_provider) as sent_again:
                    _claimed, result = restarted._execute_one(goal['goal_id'], claimed['id'])
                self.assertEqual(sent_again.call_args.kwargs['conversation_key'] == original_key, not handoff)
                self.assertEqual(result['action'], 'complete')
                self.assertEqual('CACHED_PRIVATE_PAGE_CANARY' in next_seen[0][1], not handoff)
                if not handoff:
                    self.assertIn('CACHED_PRIVATE_PAGE_CANARY', next_seen[0][1].split('CONTEXT TOOL RESULTS', 1)[1])
                saved = restarted.store.get(goal['goal_id'])
                owner = next(one for one in saved['tasks'] if one['id'] == builder['id'])
                self.assertEqual(owner['context_steps'][0]['state'] == 'superseded', handoff)

    def test_web_format_correction_uses_same_recipient_key_without_base_conversation_fallback(self):
        self.board['agents'][0]['who'] = 'web:shared-browser-connection'
        def route_context(_config, route):
            digest = hashlib.sha256(route.encode()).hexdigest()
            return 'fixture', {
                'failure_context_version': 1, 'route_fingerprint_sha256': digest,
                'transport_contract': 'fixture/route/v1',
                'effective_dispatch_version': 1, 'effective_dispatch_fingerprint_sha256': digest,
                'effective_dispatch_contract': 'fixture/effective/v1',
                'provider_principal_version': 1, 'provider_principal_fingerprint_sha256': digest,
                'provider_principal_contract': 'fixture/account/v1',
            }
        sent = []
        def provider(_config, _route, _text, **kwargs):
            sent.append(kwargs)
            kwargs['before_provider_dispatch']('initial')
            kwargs['after_provider_response']('initial')
            return {'text': 'invalid first schema' if len(sent) == 1 else json.dumps(fixtures.reply())}
        with mock.patch.object(long_horizon.chat_lab, '_route_failure_context', side_effect=route_context):
            goal = self.create('private-browser-correction', isolated_workspace=True, require_all_participants=False)
            task = self.runtime.store.claim_ready(goal['goal_id'], 'browser-worker')[0]
            with mock.patch.object(long_horizon.chat_lab, 'ask_once', side_effect=provider):
                _task, result = self.runtime._execute_one(goal['goal_id'], task['id'])
        self.assertEqual(result['action'], 'complete')
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0]['conversation_key'], sent[1]['conversation_key'])
        self.assertFalse(sent[1]['prefer_existing_conversation'])

    def test_legacy_answer_survives_evidence_and_conversation_tails_after_reload(self):
        goal = self.create("durable-answer")
        task, ids, held = self.ask(goal)
        current, _ = self.submit(held, ids, question()["prompt"] + ": DURABLE_CORRECT_ROOT")
        def noise(document, db):
            owner = next(one for one in document["tasks"] if one["id"] == task["id"])
            owner["evidence"].extend("ordinary progress " + str(n) for n in range(40))
            for number in range(70):
                self.runtime.store._record_dialogue_message(db, document, owner, fixtures.reply("work", "routine message " + str(number)))
            # Old persisted goals did not have separated answer records.
            item = document["interrupts"][0]
            item.pop("answer_record")
            item["answer"] = question()["prompt"] + ": DURABLE_CORRECT_ROOT"
        self.runtime.store._mutate(goal["goal_id"], noise)
        loaded = long_horizon.GoalStore(self.config).get(goal["goal_id"])
        own = next(one for one in loaded["tasks"] if one["id"] == task["id"])
        context = self.runtime._agent_context(loaded, own)
        self.assertIn('"answer_text":"DURABLE_CORRECT_ROOT"', context)
        self.assertIn("temporary transport", context)
        self.assertIn("Explicit user text outranks", context)

    def test_first_resolved_repeat_ignores_cosmetic_id_and_option_changes(self):
        goal = self.create("normalized-repeat")
        task, ids, held = self.ask(goal)
        current, _ = self.submit(held, ids, answer(question()))
        varied = question("regenerated-id", " WHICH folder should contain the new game?! ")
        varied["options"] = []
        next_task, repeated_ids, current = self.ask(current, varied)
        self.assertEqual(next_task["id"], task["id"])
        self.assertEqual(repeated_ids, [])
        self.assertEqual(current["status"], "queued")
        self.assertFalse(any(one["state"] == "pending" for one in current["interrupts"]))
        self.assertEqual(len(current["interrupts"]), 1)
        self.assertIn("Use the exact selected project", self.runtime._agent_context(current, next_task))

    def test_reading_saved_decisions_is_not_new_external_evidence(self):
        goal = self.create("memory-is-not-new-evidence")
        task, ids, held = self.ask(goal)
        current, _ = self.submit(held, ids, answer(question()))
        def observe_memory(document, db):
            owner = next(one for one in document['tasks'] if one['id'] == task['id'])
            owner.setdefault('context_steps', []).append({'results': [
                {'name': name, 'semantic_result_sha256': name + '-content'}
                for name in ('read_user_decisions', 'read_shared_conversation')
            ]})
        self.runtime.store._mutate(goal['goal_id'], observe_memory)
        _task, ids, _current = self.ask(current, question('new-card-id'))
        self.assertEqual(ids, [])

    def test_answered_task_priority_preserves_last_required_peer_call(self):
        goal = self.create('reserved-peer', policy={'max_provider_calls': 2})
        task = self.runtime.store.claim_ready(goal['goal_id'], 'initial')[0]
        self.runtime.store.record_dispatch(goal['goal_id'], task, 'first-question')
        ids = self.runtime.store.apply_action(goal['goal_id'], task, single.action(
            'ask_user', interrupt_reason='requirement_ambiguity', questions=[question()]))
        held = self.runtime.store.get(goal['goal_id'])
        current, _ = self.submit(held, ids, answer(question()))
        self.assertEqual(current['budget']['provider_calls'], 1)
        peer = self.runtime.store.claim_ready(goal['goal_id'], 'reserved-peer')[0]
        self.assertNotEqual(peer['assigned_agent_id'], task['assigned_agent_id'])
        self.runtime.store.record_dispatch(goal['goal_id'], peer, 'reserved-peer-question')
        self.assertEqual(self.runtime.store.get(goal['goal_id'])['budget']['provider_calls'], 2)

    def test_generic_continue_answer_does_not_grant_proposal_or_command_approval(self):
        goal = self.create('generic-answer-is-not-approval', isolated_workspace=True)
        task, ids, held = self.ask(goal)
        previous_project = copy.deepcopy(held['project'])
        current, _ = self.submit(held, ids, answer(question(), 'Continue with checks', 'team'))
        owner = next(one for one in current['tasks'] if one['id'] == task['id'])
        self.assertEqual(current['project'], previous_project)
        self.assertFalse(owner.get('review_approved_effect_id'))
        self.assertEqual(owner['state'], 'ready')
        self.assertFalse(owner['pending_action'])

    def test_new_decision_invalidates_saved_provider_context(self):
        goal = self.create('decision-context-binding')
        _task, ids, held = self.ask(goal)
        before = long_horizon._context_binding(held, {})
        current, _ = self.submit(held, ids, answer(question()))
        after = long_horizon._context_binding(current, {})
        self.assertNotEqual(before['decisions_sha256'], after['decisions_sha256'])
        changed = [key for key in before if before[key] != after[key]]
        self.assertEqual(changed, ['decisions_sha256'])

    def test_missing_or_changed_saved_image_stops_before_provider_dispatch(self):
        image_path = self.base / 'original-user-image.png'
        image_path.write_bytes(b'original-saved-bytes')
        for missing in (True, False):
            with self.subTest(missing=missing):
                goal = self.create('missing-image-' + str(missing), isolated_workspace=True)
                descriptor = {'id': 'image-input', 'name': 'exact-screenshot.png', 'type': 'image/png',
                              'path': str(self.base / 'missing.png' if missing else image_path),
                              'sha256': hashlib.sha256(b'different-original-bytes').hexdigest()}
                self.runtime.store._mutate(goal['goal_id'],
                    lambda document, db: document.update({'input_provider_attachments': [descriptor]}))
                task = self.runtime.store.claim_ready(goal['goal_id'], 'image-worker')[0]
                with mock.patch.object(long_horizon.chat_lab, 'ask_once') as provider:
                    _task, result = self.runtime._execute_one(goal['goal_id'], task['id'])
                provider.assert_not_called()
                self.assertEqual(result['action'], 'failed')
                self.assertIn('exact-screenshot.png', result['summary'])
                self.assertIn('missing' if missing else 'changed', result['summary'])
                self.assertEqual(self.runtime.store.get(goal['goal_id'])['budget']['provider_calls'], 0)

    def test_new_observed_evidence_and_new_authority_still_allow_real_questions(self):
        for reason in ("requirement_ambiguity", "missing_access", "new_authority", "risky_action"):
            with self.subTest(reason=reason):
                goal = self.create("changed-" + reason, isolated_workspace=True)
                task, ids, held = self.ask(goal, reason=reason)
                current, _ = self.submit(held, ids, answer(question()))
                if reason not in {"new_authority", "risky_action"}:
                    def observed(document, db):
                        next(one for one in document["tasks"] if one["id"] == task["id"]).setdefault("context_steps", []).append(
                            {"results": [{"semantic_result_sha256": "new-observed-access-error"}]})
                    self.runtime.store._mutate(goal["goal_id"], observed)
                _task, ids, current = self.ask(current, question("changed-id"), reason)
                self.assertEqual(len(ids), 1)
                self.assertEqual(current["status"], "waiting_for_user")

    def test_terminal_answer_receipt_survives_response_loss_without_restart_or_reopen(self):
        goal = self.create("response-loss")
        task, ids, held = self.ask(goal)
        current, envelope = self.submit(held, ids, answer(question()))
        self.assertFalse(self.runtime.store.resolve_interrupts(goal["goal_id"], envelope))
        self.runtime.store.control(goal["goal_id"], "cancel")
        restarted = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(restarted.close)
        with mock.patch.object(restarted, "start_background") as start:
            result = restarted.resume(goal["goal_id"], envelope)
        self.assertEqual(result["status"], "cancelled")
        start.assert_not_called()
        changed = copy.deepcopy(envelope)
        changed["answers"][ids[0]]["questions"][0]["text"] = "Different answer"
        with self.assertRaisesRegex(HarnessError, "different answers"):
            restarted.resume(goal["goal_id"], changed)

    def reconsiderable(self, request='reconsider', *, legacy=False):
        goal = self.create(request, isolated_workspace=True)
        task, ids, held = self.ask(goal)
        current, _ = self.submit(held, ids, answer(question(), 'EXACT_PRIOR_TYPED_DESTINATION'))
        if legacy:
            def migrate_fixture(document, db):
                item = document['interrupts'][0]
                for key in ('answer_record', 'decision_scope', 'decision_progress'):
                    item.pop(key, None)
            self.runtime.store._mutate(goal['goal_id'], migrate_fixture)
        _task, pending_ids, held = self.ask(current, question('paraphrased-id', 'Please confirm the destination folder.'))
        self.runtime.store.release_scheduler(goal['goal_id'], held['worker']['worker_id'])
        held = self.runtime.store.get(goal['goal_id'])
        return goal, task, ids[0], pending_ids, held

    def test_reconsider_is_advertised_only_after_the_waiting_scheduler_settles(self):
        goal = self.create('settling-card', isolated_workspace=True)
        _task, ids, held = self.ask(goal)
        current, _ = self.submit(held, ids, answer(question(), 'SAVED_DESTINATION'))
        _task, pending_ids, early = self.ask(current, question('followup', 'Confirm the destination folder.'))
        preview = self.runtime.store.public(early)
        self.assertTrue(preview['scheduler_live'])
        self.assertFalse(preview['decision_reconsideration']['available'])
        self.assertEqual(preview['decision_reconsideration']['pending_ids'], pending_ids)
        self.runtime.store.release_scheduler(goal['goal_id'], early['worker']['worker_id'])
        settled = self.runtime.store.get(goal['goal_id'])
        shown = self.runtime.store.public(settled)
        self.assertFalse(shown['scheduler_live'])
        self.assertTrue(shown['decision_reconsideration']['available'])
        self.assertGreater(settled['revision'], early['revision'])
        self.assertEqual(shown['pending_interrupts'], preview['pending_interrupts'])
        with self.assertRaisesRegex(HarnessError, 'changed'):
            self.runtime.store.reconsider_interrupts(goal['goal_id'], expected_revision=early['revision'], pending_ids=pending_ids)
        self.runtime.store.reconsider_interrupts(goal['goal_id'], expected_revision=settled['revision'], pending_ids=pending_ids)

    def test_dead_scheduler_record_does_not_hide_reconsideration_forever(self):
        _goal, _task, _earlier_id, _pending, held = self.reconsiderable('dead-settlement')
        held['worker'] = {'worker_id': 'dead-worker', 'pid': 12345, 'token': 'old-process'}
        with mock.patch.object(long_horizon, '_owner_is_alive', return_value=False):
            shown = self.runtime.store.public(held)
        self.assertFalse(shown['scheduler_live'])
        self.assertTrue(shown['decision_reconsideration']['available'])

    def test_explicit_legacy_reconsideration_supersedes_question_without_new_answer(self):
        goal, task, earlier_id, pending_ids, held = self.reconsiderable(legacy=True)
        previous = copy.deepcopy(next(one for one in held['interrupts'] if one['id'] == earlier_id))
        preview = self.runtime.store.public(held)['decision_reconsideration']
        self.assertEqual(preview, {'available': True, 'pending_ids': pending_ids})
        result = self.runtime.store.reconsider_interrupts(goal['goal_id'], expected_revision=held['revision'], pending_ids=pending_ids)
        current = self.runtime.store.get(goal['goal_id'])
        self.assertEqual(next(one for one in current['interrupts'] if one['id'] == earlier_id), previous)
        retired = next(one for one in current['interrupts'] if one['id'] == pending_ids[0])
        self.assertEqual(retired['state'], 'superseded')
        self.assertEqual(retired['answer'], '')
        self.assertEqual(retired['resolved_ms'], 0)
        self.assertNotIn('answer_record', retired)
        self.assertEqual(retired['superseded_by_decision_id'], earlier_id)
        self.assertEqual(result['status'], 'queued')
        next_task = self.runtime.store.claim_ready(goal['goal_id'], 'reconsidering-agent')[0]
        self.assertEqual(next_task['id'], task['id'])
        self.assertIn('EXACT_PRIOR_TYPED_DESTINATION', self.runtime._agent_context(current, next_task))

    def test_reconsideration_rejects_stale_scope_private_answer_and_authority_cards_atomically(self):
        changes = (
            lambda doc: doc.update(objective_epoch=2),
            lambda doc: doc['interrupts'][0]['decision_scope'].update(project_path='different-selected-project'),
            lambda doc: doc['interrupts'][0].update(agent_id='private-other-agent'),
            lambda doc: doc['interrupts'][-1].update(reason='new_authority'),
            lambda doc: doc['interrupts'][-1].update(reason='risky_action'),
            lambda doc: doc['interrupts'][-1].update(purpose='risk_review'),
            lambda doc: next(one for one in doc['tasks'] if one['id']==doc['interrupts'][-1]['task_id']).update(outcome_unknown=True),
        )
        for index, change in enumerate(changes):
            with self.subTest(case=index):
                goal, _task, _earlier_id, pending_ids, held = self.reconsiderable('ineligible-' + str(index))
                self.runtime.store._mutate(goal['goal_id'], lambda document, db: change(document))
                held = self.runtime.store.get(goal['goal_id'])
                self.assertFalse(self.runtime.store.public(held)['decision_reconsideration']['available'])
                with self.assertRaises(HarnessError):
                    self.runtime.store.reconsider_interrupts(goal['goal_id'], expected_revision=held['revision'], pending_ids=pending_ids)
                self.assertEqual(self.runtime.store.get(goal['goal_id']), held)

    def test_reconsideration_requires_exact_revision_and_complete_pending_ids(self):
        goal, _task, _earlier_id, pending_ids, held = self.reconsiderable('reconsider-stale')
        for revision, identities in ((held['revision']-1, pending_ids), (True, pending_ids),
                                     (held['revision'], []), (held['revision'], ['unknown']),
                                     (held['revision'], pending_ids * 2)):
            with self.subTest(revision=revision, identities=identities), self.assertRaises(HarnessError):
                self.runtime.store.reconsider_interrupts(goal['goal_id'], expected_revision=revision, pending_ids=identities)
            self.assertEqual(self.runtime.store.get(goal['goal_id']), held)
        with mock.patch.object(self.runtime, 'start_background', return_value={'status':'queued'}) as start:
            self.assertEqual(self.runtime.reconsider(goal['goal_id'], expected_revision=held['revision'], pending_ids=pending_ids)['status'], 'queued')
        start.assert_called_once_with(goal['goal_id'], {'_nexus_resolved': True})

    def test_real_checkpoint_reconsideration_delivers_decision_tool_result_and_finishes(self):
        goal = self.create('checkpoint-reread', isolated_workspace=True)
        first_question = question()
        next_question = question('paraphrase', 'Please confirm the destination folder.')
        responses = [
            fixtures.reply('ask_user', 'I need the destination.', interrupt_reason='requirement_ambiguity', questions=[first_question]),
            fixtures.reply('ask_user', 'Please confirm again.', interrupt_reason='requirement_ambiguity', questions=[next_question]),
            fixtures.reply('work', 'I will reread the saved user answer.', tool_calls=[{
                'call_id': 'read-prior-decision', 'name': 'read_user_decisions', 'arguments': {
                    'after': 0, 'limit': 10, 'decision_id': '', 'offset': 0, 'character_limit': 12000,
                }}]),
            fixtures.reply(summary='I used the exact saved destination and checked the result.'),
            fixtures.reply(summary='I checked the result and agree it is complete.'),
        ]
        seen = []
        provider = fixtures.LongHorizonDialogueTests.provider(self, responses, seen)
        with mock.patch.object(long_horizon.chat_lab, 'ask_once', side_effect=provider), mock.patch.object(
                long_horizon.swarm_work, '_run_selected_project_verification',
                return_value={'status':'passed','basis':'independent deterministic fixture check'}):
            first = self.runtime.run(goal['goal_id'])
            ids = [one['id'] for one in first['pending_interrupts']]
            self.submit(first, ids, answer(first_question, 'EXACT_CHECKPOINT_DESTINATION'))
            second = self.runtime.run(goal['goal_id'], {'_nexus_resolved': True})
            self.assertTrue(second['decision_reconsideration']['available'])
            with mock.patch.object(self.runtime, 'start_background',
                    side_effect=lambda goal_id, answers: self.runtime.run(goal_id, answers)):
                result = self.runtime.reconsider(goal['goal_id'], expected_revision=second['revision'],
                    pending_ids=second['decision_reconsideration']['pending_ids'])
        self.assertEqual(result['status'], 'complete', result['note'])
        self.assertEqual(len(seen), 5)
        self.assertEqual(seen[2][0], 'builder-route')
        self.assertIn('EXACT_CHECKPOINT_DESTINATION', seen[3][1])
        self.assertIn('CONTEXT TOOL RESULTS', seen[3][1])
        owner = next(one for one in result['tasks'] if one['assigned_agent_id']=='builder')
        step = next(one for one in owner['context_steps'] if any(c['name']=='read_user_decisions' for c in one['calls']))
        delivered = step['results'][0]['result']['decisions'][0]['content']
        self.assertEqual(json.loads(delivered)['answer_text'], 'EXACT_CHECKPOINT_DESTINATION')
        self.assertNotIn('EXACT_CHECKPOINT_DESTINATION', seen[-1][1])

    def test_invalid_batch_is_atomic_and_unknown_pending_ids_never_resolve(self):
        goal = self.create("invalid-batch")
        _task, ids, held = self.ask(goal)
        envelope = {"expected_revision": held["revision"], "pending_ids": ids,
                    "answers": {ids[0]: answer(question())}, "request_id": "invalid-test"}
        for change in ({"pending_ids": ids * 2}, {"pending_ids": ["other"]},
                       {"expected_revision": held["revision"] - 1},
                       {"answers": {**envelope["answers"], "other": "unexpected"}},
                       {"answers": {ids[0]: answer(question("wrong-id"))}}):
            with self.subTest(change=change), self.assertRaises(HarnessError):
                self.runtime.store.resolve_interrupts(goal["goal_id"], {**envelope, **change})
            unchanged = self.runtime.store.get(goal["goal_id"])
            self.assertEqual(unchanged["revision"], held["revision"])
            self.assertEqual(unchanged["interrupts"][0]["state"], "pending")

    def test_decision_paging_keeps_private_long_answers_scoped(self):
        goal = self.create("decision-paging")
        task, ids, held = self.ask(goal)
        current, _ = self.submit(held, ids, answer(question(), "a" * 18000 + "TAIL_CANARY"))
        first = goal_decisions.page(current, task["assigned_agent_id"], character_limit=1000)
        entry = first["decisions"][0]
        self.assertTrue(entry["has_more_characters"])
        text = entry["content"]
        while entry["has_more_characters"]:
            entry = goal_decisions.page(current, task["assigned_agent_id"], decision_id=ids[0],
                offset=entry["next_offset"], character_limit=1000)["decisions"][0]
            text += entry["content"]
        self.assertTrue(json.loads(text)["answer_text"].endswith("TAIL_CANARY"))
        peer = next(one for one in current["agents"] if one["id"] != task["assigned_agent_id"])
        with self.assertRaisesRegex(HarnessError, "not available"):
            goal_decisions.page(current, peer["id"], decision_id=ids[0])


if __name__ == "__main__":
    unittest.main()
