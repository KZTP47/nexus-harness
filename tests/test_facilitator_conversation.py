"""Facilitator routing and continuation through portable runtime projects."""
import unittest

from our_harness import facilitator, long_horizon
from our_harness.models import HarnessError
from tests import test_long_horizon_dialogue as fixtures


def say(kind="complete", text="Finished my contribution", target="user", **kwargs):
    return fixtures.reply(kind, text, summary_delivery={
        "kind": target if target in {"user", "team", "auto"} else "agent",
        "agent_id": "" if target in {"user", "team", "auto"} else target,
    }, **kwargs)


class FacilitatorConversationTests(unittest.TestCase):
    setUp = fixtures.LongHorizonDialogueTests.setUp
    create = fixtures.LongHorizonDialogueTests.create
    provider = fixtures.LongHorizonDialogueTests.provider
    run_replies = fixtures.LongHorizonDialogueTests.run_replies

    def test_changes_do_not_force_finished_peer_to_agree_again(self):
        goal = self.create("agent-owned-completion", facilitator_mode=True)
        result, seen = self.run_replies(goal, [say(), say(changes=[fixtures.change("result.txt", "saved")])])
        self.assertEqual(result["status"], "complete", result.get("note"))
        self.assertEqual(len(seen), 2)
        self.assertEqual((self.project / "result.txt").read_text(), "saved")
        self.assertTrue(all(m["recipient"]["kind"] == "user" for m in result["dialogue"]["messages"]))

    def test_explicit_request_reopens_finished_peer_and_delivers_exact_text(self):
        goal = self.create("request-reply", facilitator_mode=True)
        result, seen = self.run_replies(goal, [say(), say(target="builder", text="Ada, explain the edge case?"), say()])
        self.assertEqual(result["status"], "complete", result.get("note"))
        self.assertEqual([route for route, _ in seen], ["builder-route", "peer-route", "builder-route"])
        self.assertIn("Ada, explain the edge case?", seen[2][1])
        self.assertEqual(result["dialogue"]["messages"][1]["recipient"]["agent_id"], "builder")
        reopened = long_horizon.GoalStore(self.config).get(goal["goal_id"])
        self.assertEqual(reopened["dialogue"]["messages"][1]["recipient"]["agent_id"], "builder")

    def test_repeated_conversation_can_continue_to_agent_completion(self):
        goal = self.create("discussion", facilitator_mode=True)
        responses = [say("work", "Let us revisit this assumption", target="team") for _ in range(12)]
        result, seen = self.run_replies(goal, responses + [say(), say()])
        self.assertEqual(result["status"], "complete", result.get("note"))
        self.assertEqual(len(seen), 14)

    def test_repeated_tools_return_observations_without_pausing(self):
        (self.project / "sample.txt").write_text("unchanged", encoding="utf-8")
        goal = self.create("repeated-tools", facilitator_mode=True)
        responses = [say("work", "Checking again", tool_calls=[{
            "name": "read_file", "call_id": "read", "arguments": {
                "path": "sample.txt", "start_line": 1, "end_line": 2, "max_bytes": 1000,
            },
        }]) for _ in range(7)]
        result, seen = self.run_replies(goal, responses + [say(), say()])
        self.assertEqual(result["status"], "complete", result.get("note"))
        self.assertEqual(len(seen), 9)
        events = self.runtime.store.events(goal["goal_id"])["events"]
        self.assertTrue(any(e["type"] == "context_progress_observed" for e in events))
        self.assertFalse(any(e["type"] == "context_progress_paused" for e in events))

    def test_unknown_and_self_recipient_rejected(self):
        goal = self.create("recipient-boundary", facilitator_mode=True)
        for target in ("outside-project", "builder"):
            with self.assertRaises(HarnessError):
                facilitator.recipient(goal, goal["tasks"][0], say(target=target))
        # Routing cannot masquerade as a user decision request's destination.
        self.assertEqual(long_horizon._summary_delivery(goal, goal["tasks"][0],
                         say("ask_user", target="peer"))["kind"], "user")

    def test_explicit_budget_still_pauses_repeated_discussion(self):
        goal = self.create("bounded-discussion", facilitator_mode=True, policy={"max_provider_calls": 12})
        result, seen = self.run_replies(goal, lambda *_: say("work", "Revisit the assumption", target="team"))
        self.assertEqual(result["status"], "paused")
        self.assertEqual(len(seen), 12)
        self.assertEqual(result["budget"]["provider_calls"], 12)

    def test_selected_recipient_precedes_default_round_robin_after_restart(self):
        self.board["agents"].append({"id": "third", "name": "Sam", "who": "builder-route", "ready": True})
        self.board["works_on"].append({"agent": "third", "project": "game"})
        goal = self.runtime.store.create(self.board, "game", ["Discuss the implementation"], "three",
            lead_id="builder", participant_ids=["builder", "peer", "third"],
            conversation_id="three-chat", facilitator_mode=True)
        store = self.runtime.store
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        action = say("work", "Sam, take the next turn", target="third")
        store.record_action(goal["goal_id"], task, action)
        store.apply_action(goal["goal_id"], task, action)
        restarted = long_horizon.GoalStore(self.config)
        chosen = restarted.claim_ready(goal["goal_id"], "worker")
        self.assertEqual(chosen[0]["assigned_agent_id"], "third")
