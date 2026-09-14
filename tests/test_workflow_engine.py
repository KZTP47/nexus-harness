"""Portable engine regressions for directed delivery and terminal forks."""

import copy
import json
import subprocess
import unittest
from unittest import mock
from our_harness import long_horizon, goal_messages
from our_harness.models import HarnessError
import test_long_horizon_dialogue as fixtures


class WorkflowEngineTests(unittest.TestCase):
    setUp = fixtures.LongHorizonDialogueTests.setUp
    create = fixtures.LongHorizonDialogueTests.create
    provider = fixtures.LongHorizonDialogueTests.provider
    run_replies = fixtures.LongHorizonDialogueTests.run_replies

    def message(self, goal, text="PRIVATE followup condition", request="once"):
        task = goal["tasks"][0]
        with mock.patch.object(self.runtime, "start_background"):
            return self.runtime.control(
                goal["goal_id"],
                "message",
                {
                    "text": text,
                    "request_id": request,
                    "task_id": task["id"],
                    "agent_id": task["assigned_agent_id"],
                },
            )

    def test_inflight_message_preserves_effect_and_gets_new_recipient_turn(self):
        goal = self.create("inflight")
        observed = []

        def responses(number, route, kwargs):
            observed.append((route, kwargs["context"]))
            if number == 1:
                before = self.runtime.store.get(goal["goal_id"])["tasks"][0]
                self.message(goal)
                after = self.runtime.store.get(goal["goal_id"])["tasks"][0]
                self.assertEqual(after["lease_id"], before["lease_id"])
                self.assertEqual(
                    after["provider_effect_id"], before["provider_effect_id"]
                )
                return fixtures.reply(
                    changes=[fixtures.change("first.txt", "original effect")],
                    criteria_evidence=[
                        {
                            "criterion": "Original objective is satisfied",
                            "evidence_refs": ["file:first.txt"],
                        }
                    ],
                )
            return fixtures.reply()

        result, _seen = self.run_replies(goal, responses)
        self.assertEqual(result["status"], "complete")
        self.assertEqual((self.project / "first.txt").read_text(), "original effect")
        builder = [context for route, context in observed if route == "builder-route"]
        self.assertNotIn("PRIVATE followup condition", builder[0])
        self.assertTrue(
            any("PRIVATE followup condition" in context for context in builder[1:])
        )
        self.assertTrue(
            all(
                "PRIVATE followup condition" not in context
                for route, context in observed
                if route == "peer-route"
            )
        )
        self.assertFalse(
            goal_messages.pending(
                self.runtime.store.get(goal["goal_id"]),
                self.runtime.store.get(goal["goal_id"])["tasks"][0],
            )
        )

    def test_completed_target_reopens_and_restart_delivers(self):
        goal = self.create("completed")
        self.runtime.store._mutate(
            goal["goal_id"], lambda d, db: d["tasks"][0].update(state="complete")
        )
        accepted = self.message(goal)
        held = self.runtime.store.get(goal["goal_id"])
        self.assertEqual(held["tasks"][0]["state"], "ready")
        restarted = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(restarted.close)
        self.runtime = restarted
        with mock.patch.object(restarted, "start_background") as schedule:
            replay = self.message(goal)
            self.assertEqual(
                replay["directed_message_receipt"], accepted["directed_message_receipt"]
            )
            schedule.assert_not_called()
        result, seen = self.run_replies(held, [fixtures.reply(), fixtures.reply()])
        self.assertEqual(result["status"], "complete")
        self.assertTrue(
            any(
                "PRIVATE followup condition" in context
                for route, context in seen
                if route == "builder-route"
            )
        )

    def test_late_message_after_context_preparation_not_acknowledged_by_old_dispatch(
        self,
    ):
        goal = self.create("prepare-race")
        original = self.runtime._agent_context
        sent = []

        def context(document, task, *args, **kwargs):
            prepared = original(document, task, *args, **kwargs)
            if task["assigned_agent_id"] == "builder" and not sent:
                self.message(goal)
                sent.append(True)
            return prepared

        with mock.patch.object(self.runtime, "_agent_context", side_effect=context):
            result, seen = self.run_replies(goal, lambda *_: fixtures.reply())
        self.assertEqual(result["status"], "complete")
        builder = [context for route, context in seen if route == "builder-route"]
        self.assertNotIn("PRIVATE followup condition", builder[0])
        self.assertIn("PRIVATE followup condition", builder[-1])

    def test_unread_blocks_completion_reassignment_and_wrong_context(self):
        goal = self.create("barrier")
        self.message(goal)
        with self.assertRaisesRegex(HarnessError, "recipients"):
            self.runtime.store.complete_verification(
                goal["goal_id"], {"status": "passed"}
            )
        with self.assertRaises(HarnessError):
            self.runtime.store.control(
                goal["goal_id"],
                "reassign",
                {"task_id": goal["tasks"][0]["id"], "agent_id": "peer"},
            )
        held = self.runtime.store.get(goal["goal_id"])
        task = copy.deepcopy(held["tasks"][0])
        task["assigned_agent_id"] = "peer"
        with self.assertRaises(HarnessError):
            goal_messages.prompt(held, task)
        self.assertNotIn(
            "directed_messages", self.runtime.store.public(held)["tasks"][0]
        )

    def test_redacted_storage_exact_raw_retry(self):
        goal = self.create("redaction")
        with mock.patch.object(
            self.runtime.store.redactor,
            "text",
            side_effect=lambda text: str(text)
            .replace("secret-one", "[redacted]")
            .replace("secret-two", "[redacted]"),
        ):
            accepted = self.message(goal, "token secret-one")
            self.assertEqual(
                self.message(goal, "token secret-one")["directed_message_receipt"],
                accepted["directed_message_receipt"],
            )
            with self.assertRaises(HarnessError):
                self.message(goal, "token secret-two")
        held = self.runtime.store.get(goal["goal_id"])
        self.assertNotIn("secret-one", json.dumps(held))
        self.assertIn("[redacted]", goal_messages.prompt(held, held["tasks"][0]))

    def git_project(self):
        subprocess.run(
            ["git", "init", str(self.project)], capture_output=True, check=True
        )
        (self.project / ".git/info/exclude").write_text(".harness/\n")
        subprocess.run(
            [
                "git",
                "-C",
                str(self.project),
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "--allow-empty",
                "-m",
                "fixture",
            ],
            capture_output=True,
            check=True,
        )

    def test_cancelled_isolated_forks_without_mutating_source(self):
        self.git_project()
        goal = self.create("cancelled-fork", isolated_workspace=True)
        self.runtime.store.control(goal["goal_id"], "cancel")
        source = self.runtime.store.get(goal["goal_id"])
        child = self.runtime.fork(goal["goal_id"], "fork-once")
        self.assertEqual(child["status"], "paused")
        self.assertNotEqual(
            child["project_authority_id"], source["project_authority_id"]
        )
        self.assertEqual(
            self.runtime.store.get(goal["goal_id"])["revision"], source["revision"]
        )
        self.assertEqual(
            self.runtime.fork(goal["goal_id"], "fork-once")["goal_id"], child["goal_id"]
        )

    def test_fork_refreshes_message_capacity_without_reusing_parent_receipt(self):
        goal = self.create("quota")
        with mock.patch.object(goal_messages, "MAX_MESSAGES", 1):
            accepted = self.message(goal)
            self.runtime.store.control(goal["goal_id"], "pause")
            source = self.runtime.store.get(goal["goal_id"])
            target = self.base / "child"
            target.mkdir()
            child = self.runtime.store.clone_to_project(
                source, "child", "Child", target, "fork-quota"
            )
            new = self.message(child, "new child instruction", "child-message")
        self.assertNotEqual(
            new["directed_message_receipt"]["goal_id"],
            accepted["directed_message_receipt"]["goal_id"],
        )
        held = self.runtime.store.get(child["goal_id"])
        self.assertTrue(
            all(
                one["binding"] == goal_messages.binding(held, held["tasks"][0])
                for one in held["tasks"][0]["directed_messages"]
            )
        )

    def test_native_effect_is_not_replayed_when_message_arrives_during_turn(self):
        goal = self.create("native-message", facilitator_mode=True)
        writes = []

        def responses(number, route, kwargs):
            if number == 1:
                (self.project / "native.txt").write_text("native already wrote this")
                writes.append(True)
                self.message(goal)
            return fixtures.reply()

        result, seen = self.run_replies(goal, responses)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(writes), 1)
        self.assertEqual(
            (self.project / "native.txt").read_text(), "native already wrote this"
        )
        self.assertTrue(
            any(
                "PRIVATE followup condition" in context
                for route, context in seen
                if route == "builder-route"
            )
        )

    def test_completed_isolated_forks_but_dirty_and_drifted_source_reject(self):
        self.git_project()
        goal = self.create("complete-fork", isolated_workspace=True)

        # Seed an authenticated settled terminal snapshot; this test exercises
        # real Git fork admission, not the independently tested closeout protocol.
        def terminal(document, _db):
            document["status"] = "complete"
            for task in document["tasks"]:
                task["state"] = "complete"

        self.runtime.store._mutate(goal["goal_id"], terminal)
        child = self.runtime.fork(goal["goal_id"], "complete-child")
        self.assertEqual(child["status"], "paused")
        (self.project / "dirty.txt").write_text("unsaved source")
        with self.assertRaises(HarnessError):
            self.runtime.fork(goal["goal_id"], "dirty-child")
        (self.project / "dirty.txt").unlink()
        self.config.data["providers"]["builder-route"]["model"] = "changed-model"
        with self.assertRaises(HarnessError):
            self.runtime.fork(goal["goal_id"], "drift-child")

    def test_cancelled_message_does_not_block_terminal_fork(self):
        self.git_project()
        goal = self.create("cancel-message", isolated_workspace=True)
        self.message(goal)
        self.runtime.store.control(goal["goal_id"], "cancel")
        held = self.runtime.store.get(goal["goal_id"])
        self.assertFalse(goal_messages.pending(held, held["tasks"][0]))
        child = self.runtime.fork(goal["goal_id"], "cancel-message-child")
        self.assertEqual(child["status"], "paused")

    def test_failure_before_provider_reply_keeps_message_unread(self):
        goal = self.create("preack-failure")
        self.message(goal)

        def fail(_config, _route, _text, **kwargs):
            kwargs["before_provider_dispatch"]("initial")
            raise HarnessError("fixture provider failed before reply")

        with mock.patch.object(long_horizon.chat_lab, "ask_once", side_effect=fail):
            self.runtime.run(goal["goal_id"])
        held = self.runtime.store.get(goal["goal_id"])
        self.assertEqual(len(goal_messages.pending(held, held["tasks"][0])), 1)
        self.assertNotEqual(held["status"], "complete")

    def test_delivered_private_evidence_stays_private_after_reassignment(self):
        goal = self.runtime.store.create(
            self.board,
            "game",
            ["Review existing files"],
            "reassign-private",
            lead_id="builder",
        )
        self.message(goal)
        task = self.runtime.store.claim_ready(goal["goal_id"], "fixture-worker")[0]
        seen = []
        with mock.patch.object(
            long_horizon.chat_lab,
            "ask_once",
            side_effect=self.provider([fixtures.reply("work")], seen),
        ):
            returned_task, action = self.runtime._execute_one(
                goal["goal_id"], task["id"]
            )
        held = self.runtime.store.get(goal["goal_id"])
        self.assertFalse(goal_messages.pending(held, held["tasks"][0]))
        self.runtime.store.apply_action(goal["goal_id"], returned_task, action)
        self.runtime.store._mutate(
            goal["goal_id"],
            lambda d, db: d["tasks"][0].update(provider_effect_state="applied"),
        )
        self.runtime.store.control(
            goal["goal_id"], "reassign", {"task_id": task["id"], "agent_id": "peer"}
        )
        changed = self.runtime.store.get(goal["goal_id"])
        context = self.runtime._agent_context(changed, changed["tasks"][0])
        self.assertNotIn("PRIVATE followup condition", context)

    def test_rejected_identity_has_proof_but_conflicting_accepted_identity_does_not(
        self,
    ):
        goal = self.create("rejection-proof")
        wrong = {
            "text": "Private message",
            "request_id": "rejected",
            "task_id": goal["tasks"][0]["id"],
            "agent_id": "peer",
        }
        with self.assertRaises(HarnessError) as caught:
            self.runtime.control(goal["goal_id"], "message", wrong)
        proof = caught.exception.directed_message_rejection
        self.assertFalse(proof["accepted"])
        self.assertEqual(
            proof["submission_sha256"],
            goal_messages.public_digest(goal["goal_id"], wrong),
        )
        self.message(goal)
        with self.assertRaises(HarnessError) as conflict:
            self.message(goal, "Changed content")
        self.assertFalse(hasattr(conflict.exception, "directed_message_rejection"))

    def test_pending_decision_is_not_resolved_or_requeued_by_private_message(self):
        import test_goal_decisions as decisions

        goal = self.create("pending-decision-message")
        _task, _ids, waiting = decisions.GoalDecisionTests.ask(self, goal)
        before_task = waiting["tasks"][0]
        with self.assertRaisesRegex(HarnessError, "pending decision"):
            self.message(goal)
        held = self.runtime.store.get(goal["goal_id"])
        self.assertEqual(held["status"], "waiting_for_user")
        self.assertEqual(held["tasks"][0]["state"], before_task["state"])
        self.assertEqual(held["interrupts"], waiting["interrupts"])
        self.assertFalse(goal_messages.pending(held, held["tasks"][0]))


if __name__ == "__main__":
    unittest.main()
