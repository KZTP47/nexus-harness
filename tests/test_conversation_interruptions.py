"""Portable regressions for unnecessary conversation stop paths."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from our_harness import goal_access, long_horizon, swarm_work
from our_harness.models import HarnessError
from tests import test_long_horizon as store_fixtures
from tests import test_long_horizon_dialogue as dialogue


class ConversationInterruptionTests(unittest.TestCase):
    setUp = dialogue.LongHorizonDialogueTests.setUp
    create = dialogue.LongHorizonDialogueTests.create
    provider = dialogue.LongHorizonDialogueTests.provider
    run_replies = dialogue.LongHorizonDialogueTests.run_replies

    def test_bad_archive_or_decision_request_returns_error_then_continues(self):
        for name, arguments in (
            ("read_shared_conversation", {"message_id": "nonexistent-message"}),
            ("read_user_decisions", {"decision_id": "nonexistent-decision"}),
            ("read_shared_conversation", {"offset": 2}),
        ):
            with self.subTest(name=name, arguments=arguments):
                arguments = {"after": 0, "limit": 10, "offset": 0, "character_limit": 12000,
                    "decision_id" if name == "read_user_decisions" else "message_id": "", **arguments}
                goal = self.create("request-" + str(arguments))
                result, seen = self.run_replies(goal, [
                    dialogue.reply("work", tool_calls=[{"call_id": "read", "name": name, "arguments": arguments}]),
                    dialogue.reply(), dialogue.reply(),
                ])
                self.assertEqual(result["status"], "complete", result["note"])
                self.assertIn('"error":', seen[1][1])
                self.assertEqual(result["budget"]["context_tool_calls"], 1)
                self.assertFalse(any(event["type"] == "task_failed" for event in self.runtime.store.events(goal["goal_id"])["events"]))

    def test_archive_integrity_failure_is_not_a_correctable_request(self):
        goal = self.create("archive-integrity")
        with mock.patch.object(self.runtime.store, "dialogue_history", side_effect=HarnessError("archive integrity failed")):
            result, _ = self.run_replies(goal, [dialogue.reply("work", tool_calls=[{
                "call_id": "read", "name": "read_shared_conversation", "arguments": {
                    "after": 0, "limit": 10, "offset": 0, "character_limit": 12000, "message_id": ""},
            }]), dialogue.reply()])
        self.assertNotEqual(result["status"], "complete")
        self.assertTrue(any("archive integrity failed" in task["last_error"] for task in result["tasks"]))

    def test_identical_file_proposal_records_snapshot_without_transaction(self):
        (self.project / "result.js").write_bytes(b"export const answer = 42;\n")
        goal = self.create("already-present")
        with mock.patch.object(long_horizon.FileTransaction, "apply", side_effect=AssertionError("No write needed")):
            result, _ = self.run_replies(goal, [dialogue.reply(changes=[
                dialogue.change("result.js", "export const answer = 42;\n")]), dialogue.reply()])
        self.assertEqual(result["status"], "complete", result["note"])
        self.assertEqual(result["tasks"][0]["artifacts"][-1]["kind"], "verified_no_change")
        self.assertEqual(result["dialogue"]["artifact_generation"], 0)

    def test_read_only_proposal_reaches_author_without_applying_or_user_pause(self):
        goal = self.create("read-only-correction", policy={"agent_access_mode": "read_only"})
        result, seen = self.run_replies(goal, [
            dialogue.reply(changes=[dialogue.change("unauthorized.js", "bad")]),
            dialogue.reply(), dialogue.reply(),
        ])
        self.assertEqual(result["status"], "complete", result["note"])
        self.assertFalse((self.project / "unauthorized.js").exists())
        author = [context for route, context in seen if route == "builder-route"]
        self.assertIn("Nexus rejected the unapplied proposal", author[-1])
        events = self.runtime.store.events(goal["goal_id"])["events"]
        self.assertTrue(any(event["type"] == "proposal_rejected" for event in events))
        self.assertFalse(any(event["type"] == "goal_paused" for event in events))

    def test_read_only_proposal_loop_is_bounded(self):
        goal = self.create("read-only-loop", policy={"agent_access_mode": "read_only"})
        def responses(_number, route, _kwargs):
            return dialogue.reply(changes=[dialogue.change("forbidden.js", "bad")]) if route == "builder-route" else dialogue.reply()
        result, seen = self.run_replies(goal, responses)
        self.assertEqual(result["status"], "paused")
        self.assertLessEqual(len(seen), long_horizon.MAX_NO_PROGRESS + 1)
        self.assertFalse((self.project / "forbidden.js").exists())

    def test_known_access_denials_do_not_create_new_permission_questions(self):
        for basis in ("read_only_access", "command_access_denied"):
            document = {"status": "running", "note": "working"}
            goal_access.record_block(document, {"basis": basis, "reason": "Denied"})
            self.assertEqual(document["status"], "running")
            self.assertEqual(document["command_request"]["state"], "denied")
        document = {"status": "running"}
        goal_access.record_block(document, {"basis": "discovered_command_approval_required"})
        self.assertEqual(document["status"], "paused")
        document["note"] = "Paused by you"
        goal_access.record_block(document, {"basis": "discovered_command_approval_required"})
        self.assertEqual(document["note"], "Paused by you")


class ReviewCorrectionTests(unittest.TestCase):
    setUp = store_fixtures.LongHorizonTests.setUp
    store = store_fixtures.LongHorizonTests.store
    stage_review = store_fixtures.LongHorizonTests.stage_review

    def test_missing_required_checks_for_implementation_returns_to_team(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Implement parser.py and test it"], "missing-checks")
        task = store.claim_ready(goal["goal_id"], "author")[0]
        store.apply_action(goal["goal_id"], task, store_fixtures.action(), artifact={
            "kind": "verified_no_change", "tree_merkle": "fixture"})
        result = store.complete_verification(goal["goal_id"], {
            "status": "unavailable", "basis": "required_checks_not_configured",
            "reason": "Add meaningful required checks and expose their command.", "commands": []})
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["verification"]["status"], "unavailable")
        self.assertEqual(result["tasks"][-1]["kind"], "repair")

    def test_changes_requested_return_to_author_and_survive_restart(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Improve the parser"], "review-correction")
        parent = store.claim_ready(goal["goal_id"], "author")[0]
        self.stage_review(store, goal, parent, store_fixtures.action("request_review", risk="high"))
        review = store.claim_ready(goal["goal_id"], "reviewer")[0]
        store.apply_action(goal["goal_id"], review, store_fixtures.action("blocked",
            summary="Handle empty input", evidence=["review-packet:" + review["review_packet_sha256"]],
            review_verdict="changes_requested", review_findings=["Return an empty result for empty input."]))
        store.release_scheduler(goal["goal_id"], "reviewer")
        restarted = self.store()
        saved = restarted.get(goal["goal_id"])
        self.assertEqual(saved["status"], "queued")
        self.assertEqual(saved["tasks"][0]["state"], "ready")
        self.assertFalse(saved["tasks"][0]["pending_action"])
        self.assertEqual(saved["tasks"][-1]["state"], "cancelled")
        self.assertIn("empty input", str(saved["tasks"][0]["evidence"]))
        fresh = restarted.claim_ready(goal["goal_id"], "author-again")[0]
        self.stage_review(restarted, saved, fresh, store_fixtures.action("request_review", risk="high"))
        self.assertEqual(restarted.get(goal["goal_id"])["tasks"][0]["state"], "waiting_review")

    def test_unchanged_review_corrections_are_bounded_across_new_reviews_and_restart(self):
        store = self.store()
        goal = store.create(self.board, "project", ["Improve parser.py"], "repeat-review-correction")
        for attempt in range(long_horizon.MAX_NO_PROGRESS + 1):
            parent = store.claim_ready(goal["goal_id"], "author")[0]
            self.stage_review(store, goal, parent, store_fixtures.action("request_review", risk="high"))
            review = store.claim_ready(goal["goal_id"], "reviewer")[0]
            store.apply_action(goal["goal_id"], review, store_fixtures.action("blocked",
                summary="Same defect", evidence=["review-packet:" + review["review_packet_sha256"]],
                review_verdict="changes_requested", review_findings=["Handle empty input."]))
            store.release_scheduler(goal["goal_id"], "reviewer")
            store = self.store()
            saved = store.get(goal["goal_id"])
            self.assertEqual(saved["tasks"][0]["review_corrections"]["repeats"], attempt)
        self.assertEqual(saved["tasks"][0]["state"], "blocked")
        self.assertEqual(store.claim_ready(goal["goal_id"], "no-loop"), [])


class PlanningInterruptionTests(unittest.TestCase):
    def test_missing_acceptance_target_defers_discovery_without_ratification(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            goal = "Add a new file retry.js so failed requests retry automatically"
            result = swarm_work._acceptance_target_decision(root, goal, swarm_work._compile_goal_spec(root, goal))
            self.assertEqual(result["status"], "discovery_required")
            self.assertNotIn("ratification_digest", result)

    def test_real_retrieval_advances_but_missing_and_repeated_files_do_not(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            guard = swarm_work._ProgressGuard()
            state = (swarm_work._canonical_progress_state("agent", False, False, {"remaining": ["Inspect"]}),)
            for number in range(20):
                path = "module" + str(number) + ".py"
                (root / path).write_text("value = " + str(number))
                observed = set()
                text = swarm_work._requested_files(root, [({}, {"needs_files": [path]})], observations=observed)
                self.assertIn("value =", text)
                self.assertFalse(guard.stalled(state, observations={"agent": observed}))
            for number in range(14):
                observed = set()
                swarm_work._requested_files(root, [({}, {"needs_files": ["missing" + str(number)]})], observations=observed)
                self.assertFalse(observed)
                stalled = guard.stalled(state, observations={"agent": observed})
            self.assertTrue(stalled)


if __name__ == "__main__":
    unittest.main()
