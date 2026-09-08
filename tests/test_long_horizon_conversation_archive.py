from __future__ import annotations

import copy
import json
import unittest
from unittest import mock

from our_harness import goal_dialogue, long_horizon
from our_harness.config import LoadedConfig
from our_harness.models import HarnessError
import test_long_horizon_dialogue as fixtures

reply = fixtures.reply


class LongHorizonConversationArchiveTests(unittest.TestCase):
    setUp = fixtures.LongHorizonDialogueTests.setUp
    create = fixtures.LongHorizonDialogueTests.create
    provider = fixtures.LongHorizonDialogueTests.provider
    run_replies = fixtures.LongHorizonDialogueTests.run_replies

    def publish(self, goal_id, bodies, *, noise_events=0):
        def change(document, db):
            for number, body in enumerate(bodies):
                task = document["tasks"][number % 2]
                task["provider_effect_id"] = "arbitrary-effect-" + str(document["dialogue"]["sequence"])
                self.runtime.store._record_dialogue_message(db, document, task, reply("work", body))
            for number in range(noise_events):
                self.runtime.store._event(db, document, "fixture_progress", payload={"number": number})
        return self.runtime.store._mutate(goal_id, change)[0]

    def forget_archive_like_legacy_version(self, goal_id):
        def change(document, db):
            document.pop("dialogue_archive")
            db.execute("DELETE FROM long_goal_dialogue_messages WHERE goal_id=?", (goal_id,))
        self.runtime.store._mutate(goal_id, change)

    def test_all_public_messages_survive_prompt_and_four_thousand_event_retention(self):
        goal = self.create("whole-public-conversation")
        bodies = [f"PUBLIC_MESSAGE_{number:03d}: inspect controls and report evidence." for number in range(70)]
        goal = self.publish(goal["goal_id"], bodies, noise_events=long_horizon.MAX_EVENTS + 1)
        context = self.runtime._agent_context(goal, goal["tasks"][1])
        self.assertEqual(len(goal["dialogue"]["messages"]), 64)
        self.assertNotIn(bodies[0], context)
        self.assertIn("6 earlier archived messages are omitted", context)
        self.assertIn("read_shared_conversation", context)
        self.assertTrue(self.runtime.store.events(goal["goal_id"])["truncated"])
        reopened = long_horizon.GoalStore(self.config)
        found, cursor = [], 0
        while True:
            page = reopened.dialogue_history(goal["goal_id"], cursor, 7)
            found.extend(page["messages"])
            if not page["has_more"]:
                break
            cursor = page["next"]
        self.assertEqual([one["summary"] for one in found], bodies)
        self.assertEqual([one["sequence"] for one in found], list(range(1, 71)))
        self.assertEqual(page["total_messages"], 70)
        self.assertEqual(page["coverage"]["status"], "complete")
        self.assertTrue(all(one["source_goal_event_id"] for one in found))

    def test_valid_eight_thousand_unicode_message_keeps_exact_body_and_routing(self):
        goal = self.create("unicode-message")
        body = "🧭" * 8_000
        self.publish(goal["goal_id"], [body])
        page = self.runtime.store.dialogue_history(goal["goal_id"])
        self.assertEqual(page["messages"][0]["summary"], body)
        self.assertEqual(page["messages"][0]["recipient"]["agent_id"], "peer")
        self.assertEqual(page["messages"][0]["total_characters"], 8_000)
        self.assertFalse(page["messages"][0]["has_more_characters"])
        self.forget_archive_like_legacy_version(goal["goal_id"])
        restored = self.runtime.store.dialogue_history(goal["goal_id"])
        self.assertEqual(restored["messages"][0]["summary"], body)
        self.assertEqual(restored["coverage"]["status"], "complete")

    def test_range_reads_reconstruct_long_steering_without_slicing_silently(self):
        goal = self.create("range-read")
        body = "Continue with these actual details: " + "é水🧭" * 5_000
        self.runtime.store.control(goal["goal_id"], "steer", {"text": body})
        first = self.runtime.store.dialogue_history(goal["goal_id"], character_limit=1_000)
        held = first["messages"][0]
        self.assertTrue(held["has_more_characters"])
        found = held["summary"]
        while held["has_more_characters"]:
            part = self.runtime.store.dialogue_history(
                goal["goal_id"], message_id=held["id"], offset=held["next_offset"], character_limit=1_000,
            )
            held = part["messages"][0]
            found += held["summary"]
        self.assertEqual(found, body)
        self.assertEqual(held["total_characters"], len(body))
        self.assertEqual(held["source_goal_event_type"], "goal_steered")
        self.assertEqual(held["recipient"]["kind"], "team")

    def test_legacy_pruned_messages_are_reported_missing_instead_of_invented(self):
        goal = self.create("partial-legacy")
        old = self.publish(goal["goal_id"], [f"old message {number}" for number in range(70)],
                           noise_events=long_horizon.MAX_EVENTS + 1)
        self.forget_archive_like_legacy_version(goal["goal_id"])
        page = self.runtime.store.dialogue_history(goal["goal_id"])
        self.assertEqual(page["total_messages"], 64)
        self.assertEqual(page["coverage"]["status"], "partial_legacy")
        self.assertEqual(page["coverage"]["unavailable_ranges"], [{"first": 1, "last": 6}])
        self.assertEqual(page["messages"][0]["summary"], "old message 6")
        self.assertEqual(page["coverage"]["legacy_dialogue_sequence"], 70)
        self.assertEqual(page["coverage"]["legacy_event_seq"], old["event_seq"])
        self.assertEqual([one["legacy_dialogue_sequence"] for one in page["messages"]], list(range(7, 71)))
        context = self.runtime._agent_context(self.runtime.store.get(goal["goal_id"]), goal["tasks"][1])
        self.assertIn("LEGACY HISTORY GAP", context)

    def test_legacy_retained_events_recover_messages_outside_bounded_snapshot(self):
        goal = self.create("recover-events")
        bodies = [f"retained public words {number}" for number in range(70)]
        self.publish(goal["goal_id"], bodies)
        self.forget_archive_like_legacy_version(goal["goal_id"])
        page = self.runtime.store.dialogue_history(goal["goal_id"])
        self.assertEqual([one["summary"] for one in page["messages"]], bodies)
        self.assertEqual(page["coverage"]["status"], "complete")

    def test_archive_rejects_other_authority_other_goal_message_and_bad_offsets(self):
        goal = self.create("isolation")
        self.publish(goal["goal_id"], ["public message in one conversation"])
        message = self.runtime.store.dialogue_history(goal["goal_id"])["messages"][0]
        other_authority = self.base / "another-installation"
        other_authority.mkdir()
        other = long_horizon.GoalStore(LoadedConfig(copy.deepcopy(self.config.data), other_authority, [], {}))
        with self.assertRaisesRegex(HarnessError, "different Nexus project authority"):
            other.dialogue_history(goal["goal_id"])
        other_goal = self.create("different-goal")
        with self.assertRaisesRegex(HarnessError, "not in this goal"):
            self.runtime.store.dialogue_history(other_goal["goal_id"], message_id=message["id"])
        for args in ({"after": -1}, {"offset": 1}, {"character_limit": 0}, {"message_id": message["id"], "offset": 999}):
            with self.subTest(args=args), self.assertRaises(HarnessError):
                self.runtime.store.dialogue_history(goal["goal_id"], **args)

    def test_tampered_or_missing_archive_rows_fail_closed_after_restart(self):
        for mode in ("tamper", "delete-middle", "delete-head"):
            with self.subTest(mode=mode):
                goal = self.create("integrity-" + mode)
                self.publish(goal["goal_id"], ["first exact words", "middle exact words", "latest exact words"])
                with self.runtime.store._connect() as db:
                    if mode == "tamper":
                        db.execute("UPDATE long_goal_dialogue_messages SET message_json='{}' WHERE goal_id=? AND sequence=2", (goal["goal_id"],))
                    else:
                        db.execute("DELETE FROM long_goal_dialogue_messages WHERE goal_id=? AND sequence=?", (goal["goal_id"], 2 if mode == "delete-middle" else 3))
                reopened = long_horizon.GoalStore(self.config)
                with self.assertRaisesRegex(HarnessError, "Shared conversation archive"):
                    reopened.dialogue_history(goal["goal_id"])

    def test_message_and_event_recording_roll_back_together(self):
        goal = self.create("atomic-public-utterance")
        def fail_after_speech(document, db):
            task = document["tasks"][0]
            task["provider_effect_id"] = "portable-effect"
            self.runtime.store._record_dialogue_message(db, document, task, reply("work", "uncommitted words"))
            raise HarnessError("fixture rollback")
        with self.assertRaisesRegex(HarnessError, "fixture rollback"):
            self.runtime.store._mutate(goal["goal_id"], fail_after_speech)
        self.assertEqual(self.runtime.store.dialogue_history(goal["goal_id"])["messages"], [])
        self.assertFalse(any(one["type"] == "provider_acknowledged" for one in self.runtime.store.events(goal["goal_id"])["events"]))

    def test_scoped_tool_returns_earlier_public_speech_to_the_actual_provider_context(self):
        goal = self.create("actual-tool-path")
        self.publish(goal["goal_id"], [f"ARCHIVED_PUBLIC_{number}" for number in range(70)])
        result, seen = self.run_replies(goal, [
            reply("work", "Lin, I am reading our earlier agreement.", tool_calls=[{
                "call_id": "earlier-chat", "name": "read_shared_conversation", "arguments": {
                    "after": 0, "limit": 2, "message_id": "", "offset": 0, "character_limit": 12_000,
                },
            }]),
            reply(summary="Lin, I read our exact earlier messages and agree."),
            reply(summary="Ada, I agree with the current result."),
        ])
        self.assertEqual(result["status"], "complete", result["note"])
        self.assertNotIn('"message":"ARCHIVED_PUBLIC_0"', seen[0][1])
        tool_packet = seen[1][1].split("CONTEXT TOOL RESULTS", 1)[1]
        self.assertIn('"summary":"ARCHIVED_PUBLIC_0"', tool_packet)
        self.assertIn('"summary":"ARCHIVED_PUBLIC_1"', tool_packet)

    def test_targeted_user_messages_keep_recipient_scope_in_prompt_and_paginated_reads(self):
        goal = self.create("targeted-user-message")
        builder = goal["tasks"][0]
        secret = "USER_WORDS_ADDRESSED_ONLY_TO_ADA"
        self.runtime.store.control(goal["goal_id"], "message", {
            "task_id": builder["id"], "agent_id": "builder", "text": secret,
        })
        self.publish(goal["goal_id"], ["Public words to both participants"])
        goal = self.runtime.store.get(goal["goal_id"])
        self.assertIn(secret, self.runtime._agent_context(goal, goal["tasks"][0]))
        self.assertNotIn(secret, self.runtime._agent_context(goal, goal["tasks"][1]))
        human = self.runtime.store.dialogue_history(goal["goal_id"])
        self.assertEqual(human["messages"][0]["summary"], secret)
        self.assertEqual(human["messages"][0]["recipient"]["agent_id"], "builder")
        hidden = self.runtime.store.dialogue_history(goal["goal_id"], limit=1, viewer_agent_id="peer")
        self.assertEqual(hidden["messages"], [])
        self.assertEqual(hidden["next"], 1)
        self.assertTrue(hidden["has_more"])
        next_page = self.runtime.store.dialogue_history(goal["goal_id"], after=hidden["next"], limit=1, viewer_agent_id="peer")
        self.assertEqual(next_page["messages"][0]["summary"], "Public words to both participants")
        self.assertFalse(next_page["has_more"])
        with self.assertRaisesRegex(HarnessError, "not addressed"):
            self.runtime.store.dialogue_history(goal["goal_id"], message_id=human["messages"][0]["id"], viewer_agent_id="peer")

    def test_targeted_legacy_events_merge_between_actual_shared_messages(self):
        goal = self.create("legacy-targeted-order")
        self.publish(goal["goal_id"], ["first public message"])
        with mock.patch.object(goal_dialogue, "record_user_event"):
            self.runtime.store.control(goal["goal_id"], "message", {
                "task_id": goal["tasks"][0]["id"], "agent_id": "builder", "text": "old targeted user message",
            })
        self.publish(goal["goal_id"], ["next public message"])
        self.forget_archive_like_legacy_version(goal["goal_id"])
        page = self.runtime.store.dialogue_history(goal["goal_id"])
        self.assertEqual([one["summary"] for one in page["messages"]], [
            "first public message", "old targeted user message", "next public message",
        ])
        self.assertEqual([one["sequence"] for one in page["messages"]], [1, 2, 3])
        self.assertEqual(page["coverage"]["status"], "complete")
        self.assertEqual(page["coverage"]["legacy_dialogue_sequence"], 2)
        self.assertEqual([one.get("legacy_dialogue_sequence") for one in page["messages"]], [1, None, 2])
        boundary = page["coverage"]["legacy_event_seq"]
        self.publish(goal["goal_id"], ["new post-migration message"])
        reopened = long_horizon.GoalStore(self.config).dialogue_history(goal["goal_id"])
        self.assertEqual(reopened["coverage"]["legacy_event_seq"], boundary)
        self.assertEqual(reopened["coverage"]["legacy_dialogue_sequence"], 2)

    def test_interrupt_answers_are_archived_for_their_target_and_oversize_batch_is_atomic(self):
        goal = self.create("decision-scope")
        def waiting(document, _db):
            document["status"] = "waiting_for_user"
            document["interrupts"] = [{
                "id": f"decision-{number}", "state": "pending", "purpose": "question",
                "task_id": task["id"], "agent_id": task["assigned_agent_id"], "questions": [],
            } for number, task in enumerate(document["tasks"])]
        goal, _ = self.runtime.store._mutate(goal["goal_id"], waiting)
        envelope = {"expected_revision": goal["revision"], "pending_ids": ["decision-0", "decision-1"],
                    "answers": {"decision-0": "ADA_DECISION_ONLY", "decision-1": "x" * 20_001}}
        with self.assertRaisesRegex(HarnessError, "20,000 characters"):
            self.runtime.store.resolve_interrupts(goal["goal_id"], envelope)
        self.assertEqual(self.runtime.store.dialogue_history(goal["goal_id"])["messages"], [])
        self.assertTrue(all(one["state"] == "pending" for one in self.runtime.store.get(goal["goal_id"])["interrupts"]))
        envelope["answers"]["decision-1"] = "LIN_DECISION_ONLY"
        self.runtime.store.resolve_interrupts(goal["goal_id"], envelope)
        answered = self.runtime.store.get(goal["goal_id"])
        page = self.runtime.store.dialogue_history(goal["goal_id"])
        self.assertEqual([one["source_goal_event_type"] for one in page["messages"]], ["interrupt_resolved"] * 2)
        ada = self.runtime._agent_context(answered, answered["tasks"][0])
        lin = self.runtime._agent_context(answered, answered["tasks"][1])
        self.assertIn("ADA_DECISION_ONLY", ada)
        self.assertNotIn("LIN_DECISION_ONLY", ada)
        self.assertIn("LIN_DECISION_ONLY", lin)
        self.assertNotIn("ADA_DECISION_ONLY", lin)
        self.assertEqual([one["summary"] for one in self.runtime.store.dialogue_history(
            goal["goal_id"], viewer_agent_id="peer",
        )["messages"]], ["LIN_DECISION_ONLY"])

    def test_oversized_user_steering_is_rejected_before_state_changes(self):
        goal = self.create("steering-size")
        with self.assertRaisesRegex(HarnessError, "20,000 characters"):
            self.runtime.store.control(goal["goal_id"], "steer", {"text": "x" * 20_001})
        current = self.runtime.store.get(goal["goal_id"])
        self.assertEqual(current["objective"], goal["objective"])
        self.assertEqual(current["objective_epoch"], goal["objective_epoch"])
        self.assertEqual(self.runtime.store.dialogue_history(goal["goal_id"])["messages"], [])

    def test_fork_rebinds_full_archive_without_borrowing_source_event_cursors(self):
        goal = self.create("fork-history")
        goal = self.publish(goal["goal_id"], [f"shared message {number}" for number in range(70)])
        target = self.base / "independent fork project"
        target.mkdir()
        forked = self.runtime.store.clone_to_project(goal, "fork", "Fork", target, "fork-history-copy")
        copied = self.runtime.store.dialogue_history(forked["goal_id"])
        self.assertEqual([one["summary"] for one in copied["messages"]], [f"shared message {number}" for number in range(70)])
        self.assertTrue(all(one["goal_id"] == forked["goal_id"] for one in copied["messages"]))
        self.assertTrue(all(one["source_goal_event_seq"] == 0 for one in copied["messages"]))
        self.assertTrue(all(one["origin_goal_id"] == goal["goal_id"] for one in copied["messages"]))
        self.assertNotEqual(forked["dialogue_archive"]["binding_sha256"], goal["dialogue_archive"]["binding_sha256"])

    def test_legacy_targeted_recipient_does_not_follow_later_task_reassignment(self):
        goal = self.create("historic-recipient")
        task_id = goal["tasks"][0]["id"]
        with mock.patch.object(goal_dialogue, "record_user_event"):
            self.runtime.store.control(goal["goal_id"], "message", {
                "task_id": task_id, "agent_id": "builder", "text": "message for the original recipient",
            })
            self.runtime.store.control(goal["goal_id"], "message", {
                "task_id": task_id, "text": "legacy message with unknown original recipient",
            })
        def change_owner(document, _db):
            document["tasks"][0]["assigned_agent_id"] = "peer"
        self.runtime.store._mutate(goal["goal_id"], change_owner)
        self.forget_archive_like_legacy_version(goal["goal_id"])
        human = self.runtime.store.dialogue_history(goal["goal_id"])
        self.assertEqual(human["messages"][0]["recipient"]["agent_id"], "builder")
        self.assertEqual(human["messages"][1]["visibility"], "operator_only")
        self.assertEqual(self.runtime.store.dialogue_history(goal["goal_id"], viewer_agent_id="peer")["messages"], [])
        self.assertEqual(len(self.runtime.store.dialogue_history(goal["goal_id"], viewer_agent_id="builder")["messages"]), 1)


if __name__ == "__main__":
    unittest.main()
