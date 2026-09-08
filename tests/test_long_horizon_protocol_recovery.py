from __future__ import annotations

import json
import unittest
from unittest import mock

from our_harness import action_protocol, long_horizon
from our_harness.models import HarnessError, ProviderOutcomeUnknown
from tests import test_long_horizon_dialogue as fixtures

reply = fixtures.reply


class LongHorizonProtocolRecoveryTests(unittest.TestCase):
    setUp = fixtures.LongHorizonDialogueTests.setUp
    create = fixtures.LongHorizonDialogueTests.create
    run_replies = fixtures.LongHorizonDialogueTests.run_replies

    def provider(self, responses, seen):
        def ask(_config, route, _text, **kwargs):
            # Count physical admitted sends, not calls rejected by admission.
            kwargs["before_provider_dispatch"]("initial")
            seen.append((route, kwargs["context"]))
            answer = responses(len(seen), route, kwargs) if callable(responses) else responses[len(seen) - 1]
            if isinstance(answer, Exception):
                raise answer
            kwargs["after_provider_response"]("initial")
            return {"text": json.dumps(answer)}
        return ask

    @staticmethod
    def invalid():
        return reply("work", "Lin, I have a plan to build the game.", tasks=[{
            "title": "Do not execute this rejected delegation", "description": "This is a plan, not a valid delegation action.",
            "assigned_agent_id": "peer", "depends_on": [], "parallel_safe": False, "resource_paths": [],
        }], changes=[fixtures.change("rejected.js", "This rejected file must not exist.")], tool_calls=[{
            "call_id": "rejected-read", "name": "read_file", "arguments": {
                "path": "do-not-read.txt", "start_line": 1, "end_line": 1, "max_bytes": 100,
            },
        }])

    def pending(self, request="pending-protocol"):
        goal = self.create(request)
        store = self.runtime.store
        task = store.claim_ready(goal["goal_id"], "portable-worker")[0]
        store.record_dispatch(goal["goal_id"], task, "first-prompt")
        store.record_provider_reply(goal["goal_id"], task, phase="initial")
        action = self.invalid()
        try:
            long_horizon._validate_action_semantics(action, task)
        except action_protocol.ActionProtocolError as error:
            store.record_protocol_rejection(goal["goal_id"], task, action, error)
        return store.get(goal["goal_id"]), task

    def legacy(self, request="legacy-protocol", *, error=None, received=True):
        goal = self.create(request)
        store = self.runtime.store
        task = store.claim_ready(goal["goal_id"], "legacy-worker")[0]
        store.record_dispatch(goal["goal_id"], task, "legacy-prompt")
        if received:
            store.record_provider_reply(goal["goal_id"], task, phase="initial")
        store.fail_task(goal["goal_id"], task, error or action_protocol.ERRORS["tasks_require_delegate"])
        store.release_scheduler(goal["goal_id"], "legacy-worker")
        return store.get(goal["goal_id"])

    def test_invalid_delegation_gets_a_bounded_correction_and_both_agents_complete(self):
        goal = self.create("correction-completes")
        with mock.patch.object(long_horizon.swarm_work, "_ProjectContextTools") as rejected_tools:
            result, seen = self.run_replies(goal, [
                self.invalid(),
                reply(summary="Lin, I corrected my action and built the actual game.", changes=[fixtures.change("game.js", "export const playable = true;\n")],
                      criteria_evidence=[{"criterion": "Original objective is satisfied", "evidence_refs": ["file:game.js"]}]),
                reply(summary="Ada, I inspected your game and agree it is ready."),
            ])
            rejected_tools.assert_not_called()
        self.assertEqual(result["status"], "complete", result["note"])
        self.assertEqual([route for route, _ in seen], ["builder-route", "builder-route", "peer-route"])
        self.assertIn("ACTION FIELD RULES", seen[0][1])
        self.assertIn("ACTION PROTOCOL CORRECTION", seen[1][1])
        self.assertIn("tasks must be [] unless action is delegate", seen[1][1])
        self.assertFalse((self.project / "rejected.js").exists())
        self.assertTrue((self.project / "game.js").exists())
        self.assertEqual(len(result["tasks"]), 2)
        recovery = result["tasks"][0]["protocol_recovery"]
        self.assertEqual((recovery["state"], recovery["attempts"]), ("corrected", 1))
        self.assertEqual(recovery["cumulative_attempts"], 1)
        self.assertEqual(result["budget"]["provider_calls"], 3)
        events = self.runtime.store.events(goal["goal_id"])["events"]
        self.assertEqual(sum(one["type"] == "action_protocol_rejected" for one in events), 1)
        self.assertEqual(sum(one["type"] == "file_transaction_applied" for one in events), 1)
        self.assertFalse(any(one["type"] == "task_failed" for one in events))

    def test_safe_mutual_field_errors_are_correctable_but_authority_rules_stay_hard(self):
        samples = [
            (reply("work", questions=[{"id": "choice", "prompt": "Pick one", "multiple": False, "allow_other": True, "options": []}]), "questions_require_ask_user"),
            (reply("work", handoff_agent_id="peer"), "target_requires_handoff"),
            (reply("blocked", changes=[fixtures.change("bad.js", "bad")]), "changes_require_work"),
            (reply("work", changes=[fixtures.change("bad.js", "bad")], tool_calls=[{"call_id": "read", "name": "read_file", "arguments": {"path": "bad.js", "start_line": 1, "end_line": 1, "max_bytes": 50}}]), "tools_and_changes_conflict"),
        ]
        for action, code in samples:
            with self.subTest(code=code), self.assertRaises(action_protocol.ActionProtocolError) as rejected:
                long_horizon._validate_action_semantics(action, {})
            self.assertEqual(rejected.exception.code, code)
        for action, task in [
            (reply("handoff", handoff_agent_id="peer"), {"required_contributor_id": "builder"}),
            (reply("blocked", changes=[fixtures.change("bad.js", "bad")]), {"kind": "review"}),
        ]:
            with self.assertRaises(HarnessError) as rejected:
                long_horizon._validate_action_semantics(action, task)
            self.assertNotIsInstance(rejected.exception, action_protocol.ActionProtocolError)

    def test_pending_correction_survives_restart_without_repeating_the_original_provider_turn(self):
        goal, task = self.pending()
        self.runtime.store.release_scheduler(goal["goal_id"], "portable-worker")
        recovered = self.runtime.store.recover_dead(goal["goal_id"])
        self.assertEqual(recovered["tasks"][0]["state"], "ready")
        self.assertEqual(recovered["tasks"][0]["protocol_recovery"]["attempts"], 0)
        self.runtime.store.control(goal["goal_id"], "resume")
        result, seen = self.run_replies(goal, [reply(), reply()])
        self.assertEqual(result["status"], "complete", result["note"])
        self.assertIn("ACTION PROTOCOL CORRECTION", next(context for route, context in seen if route == "builder-route"))
        self.assertEqual(result["budget"]["provider_calls"], 3)
        self.assertEqual(result["tasks"][0]["protocol_recovery"]["attempts"], 1)
        self.assertFalse((self.project / "rejected.js").exists())

    def test_dispatched_correction_is_uncertain_after_restart_and_never_resent(self):
        goal, task = self.pending("dispatched-correction")
        self.runtime.store.record_dispatch(goal["goal_id"], task, "correction-prompt", phase="protocol_correction")
        self.runtime.store.release_scheduler(goal["goal_id"], "portable-worker")
        recovered = self.runtime.store.recover_dead(goal["goal_id"])
        self.assertTrue(recovered["tasks"][0]["outcome_unknown"])
        self.assertEqual(recovered["tasks"][0]["protocol_recovery"]["cumulative_attempts"], 1)
        self.assertFalse(self.runtime.store.protocol_recovery_status(recovered)["eligible"])
        with self.assertRaisesRegex(HarnessError, "Reconcile"):
            self.runtime.store.control(goal["goal_id"], "resume")
        self.assertEqual(recovered["budget"]["provider_calls"], 2)

    def test_uncertain_provider_failure_during_correction_is_not_another_protocol_retry(self):
        goal = self.create("uncertain-correction")
        result, seen = self.run_replies(goal, [self.invalid(), ProviderOutcomeUnknown("fixture unknown delivery")])
        self.assertEqual(len(seen), 2)
        self.assertEqual(result["status"], "paused")
        self.assertTrue(result["tasks"][0]["outcome_unknown"])
        self.assertFalse(self.runtime.store.protocol_recovery_status(result)["eligible"])

    def test_provider_budget_reserves_the_untouched_peer_and_persists_pending_correction(self):
        goal = self.create("reserved-correction", policy={"max_provider_calls": 2})
        result, seen = self.run_replies(goal, [self.invalid(), reply()])
        self.assertEqual([route for route, _ in seen], ["builder-route", "peer-route"])
        self.assertEqual(result["budget"]["provider_calls"], 2)
        self.assertEqual(result["status"], "paused")
        self.assertEqual(result["tasks"][0]["protocol_recovery"]["state"], "pending")
        self.assertEqual(result["tasks"][0]["protocol_recovery"]["attempts"], 0)
        self.assertEqual(result["tasks"][0]["protocol_recovery"]["cumulative_attempts"], 0)
        self.assertFalse(self.runtime.store.protocol_recovery_status(result)["eligible"])

    def test_repeated_invalid_replies_stop_at_two_corrections_and_resume_does_not_reset_budget(self):
        goal = self.create("bounded-protocol-corrections")
        result, seen = self.run_replies(goal, [self.invalid(), self.invalid(), self.invalid(), reply()])
        self.assertEqual(result["status"], "paused")
        self.assertEqual(len(seen), 4)
        self.assertEqual(result["tasks"][0]["protocol_recovery"]["state"], "exhausted")
        self.assertEqual(result["tasks"][0]["protocol_recovery"]["attempts"], 2)
        self.assertEqual(result["tasks"][0]["protocol_recovery"]["cumulative_attempts"], 2)
        self.assertEqual(result["tasks"][0]["provider_effect_state"], "protocol_rejected")
        self.assertFalse(self.runtime.store.protocol_recovery_status(result)["eligible"])
        with self.assertRaisesRegex(HarnessError, "corrections were exhausted"):
            self.runtime.store.control(goal["goal_id"], "resume")

    def test_three_independent_corrected_episodes_apply_useful_work_and_complete(self):
        goal = self.create("independent-protocol-episodes")
        responses = []
        for version in range(1, 4):
            responses.extend([
                self.invalid(),
                reply("work" if version < 3 else "complete", f"Lin, game iteration {version} is implemented.",
                      changes=[fixtures.change("game.js", f"export const gameVersion = {version};\n")],
                      criteria_evidence=[{"criterion": "Original objective is satisfied", "evidence_refs": ["file:game.js"]}]),
                reply("work" if version < 3 else "complete", f"Ada, I reviewed game iteration {version}."),
            ])
        result, seen = self.run_replies(goal, responses)
        self.assertEqual(result["status"], "complete", result["note"])
        self.assertEqual([route for route, _ in seen], ["builder-route", "builder-route", "peer-route"] * 3)
        recovery = result["tasks"][0]["protocol_recovery"]
        self.assertEqual((recovery["schema_version"], recovery["state"], recovery["attempts"], recovery["cumulative_attempts"]),
                         (2, "corrected", 1, 3))
        self.assertEqual(result["budget"]["provider_calls"], 9)
        self.assertEqual((self.project / "game.js").read_text(), "export const gameVersion = 3;\n")
        self.assertFalse((self.project / "rejected.js").exists())
        self.assertEqual(len(result["tasks"]), 2)
        events = self.runtime.store.events(goal["goal_id"])["events"]
        self.assertEqual(sum(one["type"] == "file_transaction_applied" for one in events), 3)
        rejected = [one["payload"] for one in events if one["type"] == "action_protocol_rejected"]
        self.assertEqual([(one["attempts"], one["cumulative_attempts"]) for one in rejected], [(0, 0), (0, 1), (0, 2)])
        admitted = [one["payload"] for one in events if one["type"] == "provider_dispatched"
                    and one["payload"]["phase"] == "protocol_correction"]
        self.assertEqual([one["protocol_correction_cumulative_attempts"] for one in admitted], [1, 2, 3])

    def test_schema_one_corrected_record_is_read_compatible_then_starts_a_new_episode(self):
        goal = self.create("old-corrected-protocol-episode")
        store = self.runtime.store
        responses = [self.invalid(), self.invalid(), reply("work", changes=[fixtures.change("game.js", "version one")]),
                     reply("work"), self.invalid(), reply(changes=[fixtures.change("game.js", "version two")],
                     criteria_evidence=[{"criterion": "Original objective is satisfied", "evidence_refs": ["file:game.js"]}]), reply()]

        def ask(number, _route, _kwargs):
            if number == 4:
                # Simulate the exact authenticated record left by the previous
                # engine after two corrections, while the same goal continues.
                def old_worker(document, _db):
                    recovery = document["tasks"][0]["protocol_recovery"]
                    recovery.update({"schema_version": 1, "contract_fingerprint_sha256":
                                     action_protocol.contract(long_horizon.AGENT_ACTION_FORMAT.schema, schema_version=1)["fingerprint_sha256"]})
                    recovery.pop("cumulative_attempts")
                store._mutate(goal["goal_id"], old_worker)
                before = store.get(goal["goal_id"])
                self.assertEqual(before["tasks"][0]["protocol_recovery"]["attempts"], 2)
                self.assertEqual(before["tasks"][0]["protocol_recovery"]["state"], "corrected")
                store._validate_protocol_recovery(before, before["tasks"][0], before["tasks"][0]["protocol_recovery"])
                store.protocol_recovery_status(before)
                self.assertEqual(store.get(goal["goal_id"]), before)
            return responses[number - 1]

        result, seen = self.run_replies(goal, ask)
        self.assertEqual(result["status"], "complete", result["note"])
        self.assertEqual(len(seen), 7)
        recovery = result["tasks"][0]["protocol_recovery"]
        self.assertEqual((recovery["schema_version"], recovery["attempts"], recovery["cumulative_attempts"]), (2, 1, 3))
        self.assertEqual(recovery["counter_migration"]["carried_attempts"], 2)
        self.assertEqual((self.project / "game.js").read_text(), "version two")

    def test_new_episode_still_obeys_total_provider_budget_and_keeps_latest_rejection(self):
        goal = self.create("episode-provider-budget", policy={"max_provider_calls": 7})
        latest = self.invalid()
        latest["summary"] = "Lin, this is the third independent invalid proposal."
        result, seen = self.run_replies(goal, [
            self.invalid(), reply("work", changes=[fixtures.change("game.js", "one")]), reply("work"),
            self.invalid(), reply("work", changes=[fixtures.change("game.js", "two")]), reply("work"), latest,
        ])
        self.assertEqual(result["status"], "paused")
        self.assertEqual((len(seen), result["budget"]["provider_calls"]), (7, 7))
        recovery = result["tasks"][0]["protocol_recovery"]
        self.assertEqual((recovery["state"], recovery["attempts"], recovery["cumulative_attempts"]), ("pending", 0, 2))
        self.assertEqual(recovery["rejected_summary"], latest["summary"])
        self.assertEqual(recovery["previous_effect_id"], result["tasks"][0]["provider_effect_id"])
        self.assertFalse(self.runtime.store.protocol_recovery_status(result)["eligible"])
        self.assertEqual((self.project / "game.js").read_text(), "two")

    def test_schema_one_pending_counter_survives_restart_and_exhausts_without_reset(self):
        goal, task = self.pending("old-pending-protocol-episode")
        store = self.runtime.store
        store.record_dispatch(goal["goal_id"], task, "first-correction", phase="protocol_correction")
        store.record_provider_reply(goal["goal_id"], task, phase="protocol_correction")
        store.record_protocol_rejection(goal["goal_id"], task, self.invalid(), action_protocol.ActionProtocolError("tasks_require_delegate"))
        def old_worker(document, _db):
            recovery = document["tasks"][0]["protocol_recovery"]
            recovery.update({"schema_version": 1, "contract_fingerprint_sha256":
                             action_protocol.contract(long_horizon.AGENT_ACTION_FORMAT.schema, schema_version=1)["fingerprint_sha256"]})
            recovery.pop("cumulative_attempts")
        store._mutate(goal["goal_id"], old_worker)
        store.release_scheduler(goal["goal_id"], "portable-worker")
        # A fresh runtime uses only saved state, not this worker's counters.
        self.runtime.close()
        self.runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(self.runtime.close)
        recovered = self.runtime.store.recover_dead(goal["goal_id"])
        recovery = recovered["tasks"][0]["protocol_recovery"]
        self.assertEqual((recovery["schema_version"], recovery["attempts"], recovery["state"]), (1, 1, "pending"))
        self.assertNotIn("cumulative_attempts", recovery)
        self.runtime.store.control(goal["goal_id"], "resume")
        def ask(_number, route, _kwargs):
            return self.invalid() if route == "builder-route" else reply()
        result, seen = self.run_replies(goal, ask)
        self.assertEqual(result["status"], "paused")
        self.assertEqual(sum(route == "builder-route" for route, _ in seen), 1)
        recovery = result["tasks"][0]["protocol_recovery"]
        self.assertEqual((recovery["schema_version"], recovery["state"], recovery["attempts"], recovery["cumulative_attempts"]),
                         (2, "exhausted", 2, 2))
        self.assertEqual(recovery["counter_migration"]["carried_attempts"], 1)
        self.assertEqual(result["budget"]["provider_calls"], 4)
        self.assertFalse((self.project / "rejected.js").exists())

    def test_unknown_counter_contract_and_invalid_cumulative_counts_are_rejected(self):
        goal, task = self.pending("invalid-counter-contract")
        current = goal["tasks"][0]
        recovery = current["protocol_recovery"]
        for update in ({"schema_version": 3}, {"cumulative_attempts": -1}, {"cumulative_attempts": True},
                       {"attempts": 1, "cumulative_attempts": 0}, {"schema_version": 1}):
            with self.subTest(update=update), self.assertRaisesRegex(HarnessError, "correction contract changed"):
                self.runtime.store._validate_protocol_recovery(goal, task, {**recovery, **update})

    def test_explicit_resume_repairs_only_authenticated_legacy_semantic_rejection(self):
        goal = self.legacy()
        verdict = self.runtime.store.protocol_recovery_status(goal)
        self.assertTrue(verdict["eligible"])
        self.assertEqual(verdict["goal_id"], goal["goal_id"])
        self.assertEqual(verdict["goal_revision"], goal["revision"])
        self.assertEqual(verdict["task_ids"], [goal["tasks"][0]["id"]])
        resumed = self.runtime.store.control(goal["goal_id"], "resume")
        self.assertFalse(resumed["tasks"][0]["reconciliation_required"])
        self.assertEqual(resumed["tasks"][0]["protocol_recovery"]["state"], "pending")
        result, seen = self.run_replies(goal, [reply(), reply()])
        self.assertEqual(result["status"], "complete", result["note"])
        self.assertEqual(len(seen), 2)
        self.assertEqual(result["budget"]["provider_calls"], 3)
        self.assertEqual(len(result["tasks"][0]["protocol_recovery"]["legacy_proof_event_ids"]), 3)

    def test_legacy_error_text_without_received_reply_or_with_other_failure_is_not_recoverable(self):
        for request, options in [("no-reply", {"received": False}), ("bad-json", {"error": "Provider returned malformed JSON"})]:
            with self.subTest(request=request):
                goal = self.legacy(request, **options)
                self.assertFalse(self.runtime.store.protocol_recovery_status(goal)["eligible"])
                self.assertNotIn("protocol_recovery", self.runtime.store.get(goal["goal_id"])["tasks"][0])
                self.runtime.store.control(goal["goal_id"], "cancel")

    def test_legacy_pending_file_effect_or_changed_provider_never_gets_safe_recovery(self):
        goal = self.legacy("changed-provider")
        self.config.data["providers"]["builder-route"]["endpoint"] = "http://127.0.0.1/changed-endpoint"
        self.assertFalse(self.runtime.store.protocol_recovery_status(goal)["eligible"])
        with self.assertRaisesRegex(HarnessError, "Reconcile"):
            self.runtime.store.control(goal["goal_id"], "resume")
        self.config.data["providers"]["builder-route"]["endpoint"] = "http://127.0.0.1/one"
        goal = self.runtime.store._mutate(goal["goal_id"], lambda document, _db: document["tasks"][0].update({
            "pending_transaction": {"transaction_id": "unsettled", "state": "prepared"},
        }))[0]
        self.assertFalse(self.runtime.store.protocol_recovery_status(goal)["eligible"])
        with self.assertRaisesRegex(HarnessError, "Reconcile"):
            self.runtime.store.control(goal["goal_id"], "resume")

    def test_changed_project_discards_old_correction_packet_before_a_fresh_action(self):
        goal, task = self.pending("changed-project")
        self.runtime.store.release_scheduler(goal["goal_id"], "portable-worker")
        self.runtime.store.recover_dead(goal["goal_id"])
        (self.project / "new-state.js").write_text("export const current = true;\n")
        current = self.runtime.store.get(goal["goal_id"])
        self.assertFalse(self.runtime.store.protocol_recovery_status(current)["eligible"])
        self.runtime.store.control(goal["goal_id"], "resume")
        result, seen = self.run_replies(goal, [reply(), reply()])
        self.assertEqual(result["status"], "complete", result["note"])
        builder_context = next(context for route, context in seen if route == "builder-route")
        self.assertNotIn("ACTION PROTOCOL CORRECTION", builder_context)
        self.assertIn("earlier invalid proposal was discarded", builder_context)
        self.assertEqual(result["tasks"][0]["protocol_recovery"]["state"], "superseded")

    def test_stale_diagnostic_snapshot_and_changed_recovery_contract_are_not_replay_authority(self):
        goal = self.legacy("stale-recovery-status")
        self.runtime.store.control(goal["goal_id"], "pause")
        self.assertEqual(self.runtime.store.protocol_recovery_status(goal)["code"], "stale_snapshot")
        self.runtime.store.control(goal["goal_id"], "cancel")
        pending, _task = self.pending("changed-recovery-contract")
        self.runtime.store._mutate(pending["goal_id"], lambda document, _db: document["tasks"][0]["protocol_recovery"].update({
            "contract_fingerprint_sha256": "0" * 64,
        }))
        self.runtime.store.release_scheduler(pending["goal_id"], "portable-worker")
        with self.assertRaisesRegex(HarnessError, "correction contract changed"):
            self.runtime.store.recover_dead(pending["goal_id"])

    def test_revision_bound_resume_rejects_stale_repair_before_any_mutation(self):
        goal = self.legacy("revision-bound-resume")
        offered_revision = goal["revision"]
        self.runtime.store.control(goal["goal_id"], "pause")
        before = self.runtime.store.get(goal["goal_id"])
        with mock.patch.object(self.runtime, "start_background") as background:
            with self.assertRaisesRegex(HarnessError, "changed after the repair was offered"):
                self.runtime.resume(goal["goal_id"], expected_revision=offered_revision)
            background.assert_not_called()
        after = self.runtime.store.get(goal["goal_id"])
        self.assertEqual(after, before)
        self.assertNotIn("protocol_recovery", after["tasks"][0])


if __name__ == "__main__":
    unittest.main()
