"""Agent requests survive tool continuations, projection rollover and restart."""
import copy
import unittest

from our_harness import long_horizon, peer_delivery
from our_harness.models import HarnessError
from tests import test_facilitator_conversation as fixtures


def say(*args, request=False, **kwargs):
    action = fixtures.say(*args, **kwargs)
    action["summary_delivery"]["reply_requested"] = request
    return action


class PeerDeliveryTests(unittest.TestCase):
    setUp = fixtures.FacilitatorConversationTests.setUp
    create = fixtures.FacilitatorConversationTests.create
    provider = fixtures.FacilitatorConversationTests.provider
    run_replies = fixtures.FacilitatorConversationTests.run_replies

    def append(self, goal, action, sender="peer"):
        def write(document, db):
            task = next(one for one in document["tasks"] if one["assigned_agent_id"] == sender)
            self.runtime.store._record_dialogue_message(db, document, task, action)
        self.runtime.store._mutate(goal["goal_id"], write)

    def packet(self, store, goal_id, agent="builder"):
        goal = store.get(goal_id)
        task = next(one for one in goal["tasks"] if one["assigned_agent_id"] == agent)
        return store.peer_requests(goal, task)

    def test_fyi_to_finished_teammate_does_not_reopen_it(self):
        goal = self.create("fyi", facilitator_mode=True)
        result, seen = self.run_replies(goal, [say(), say(text="Ada, FYI: saved", target="builder")])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(seen), 2)
        self.assertFalse(result["dialogue"]["messages"][-1]["reply_requested"])

    def test_request_yields_after_effectful_tool_without_replay_or_duplicate_speech(self):
        goal = self.create("tool-yield", facilitator_mode=True, policy={"agent_access_mode": "full"})
        request = say("work", "Lin, inspect my saved file", target="peer", request=True, tool_calls=[{
            "name": "write_file", "call_id": "save", "arguments": {"path": "handoff.txt", "content": "saved once"},
        }])
        result, seen = self.run_replies(goal, [request, say(), say()])
        self.assertEqual(result["status"], "complete", result.get("note"))
        self.assertEqual([route for route, _ in seen], ["builder-route", "peer-route", "builder-route"])
        self.assertIn("Lin, inspect my saved file", seen[1][1])
        self.assertIn('"applied_to": "selected_project"', seen[2][1])
        self.assertEqual((self.project / "handoff.txt").read_text(), "saved once")
        self.assertEqual(sum(m["summary"] == request["summary"] for m in result["dialogue"]["messages"]), 1)
        events = self.runtime.store.events(goal["goal_id"])["events"]
        tool_results = [e for e in events if e["type"] == "context_tool_result"]
        self.assertEqual(len(tool_results), 1)
        self.assertTrue(any(c["path"] == "handoff.txt" for a in result["artifacts"] for c in a.get("changes", [])))

    def test_request_survives_projection_rollover_and_later_status(self):
        goal = self.create("rollover", facilitator_mode=True)
        self.append(goal, say("work", "Please check the missing edge case", target="builder", request=True))
        for n in range(long_horizon.MAX_DIALOGUE_MESSAGES + 3):
            self.append(goal, say(text=f"Status {n}"))
        held = self.runtime.store.get(goal["goal_id"])
        self.assertFalse(any(m["summary"] == "Please check the missing edge case" for m in held["dialogue"]["messages"]))
        restarted = long_horizon.GoalStore(self.config)
        packet = self.packet(restarted, goal["goal_id"])
        self.assertEqual([m["summary"] for m in packet], ["Please check the missing edge case"])
        self.assertIn("Please check the missing edge case", self.runtime._agent_context(held, held["tasks"][0]))

    def test_dispatch_does_not_acknowledge_until_provider_reply(self):
        goal = self.create("receipt", facilitator_mode=True)
        self.append(goal, say("work", "Please inspect", target="builder", request=True))
        store = self.runtime.store
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        sequence = self.packet(store, goal["goal_id"])[-1]["sequence"]
        store.record_dispatch(goal["goal_id"], task, "prepared", peer_sequence=sequence)
        restarted = long_horizon.GoalStore(self.config)
        self.assertEqual(len(self.packet(restarted, goal["goal_id"])), 1)
        restarted.record_provider_reply(goal["goal_id"], task, phase="initial")
        self.assertEqual(self.packet(restarted, goal["goal_id"]), [])
        # A format repair can reuse the already-receipted prompt safely.
        restarted.record_dispatch(goal["goal_id"], task, "repair", peer_sequence=sequence)
        restarted.record_provider_reply(goal["goal_id"], task, phase="repair")
        self.assertEqual(self.packet(restarted, goal["goal_id"]), [])

    def test_message_after_prompt_preparation_remains_pending(self):
        goal = self.create("late-message", facilitator_mode=True)
        store = self.runtime.store
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        self.assertEqual(self.packet(store, goal["goal_id"]), [])
        self.append(goal, say("work", "Arrived after context", target="builder", request=True))
        store.record_dispatch(goal["goal_id"], task, "older-prompt", peer_sequence=0)
        store.record_provider_reply(goal["goal_id"], task, phase="initial")
        self.assertEqual(len(self.packet(store, goal["goal_id"])), 1)

    def test_uncertain_provider_failure_does_not_claim_receipt(self):
        goal = self.create("uncertain", facilitator_mode=True)
        self.append(goal, say("work", "Still need a reply", target="builder", request=True))
        store = self.runtime.store
        task = store.claim_ready(goal["goal_id"], "worker")[0]
        sequence = self.packet(store, goal["goal_id"])[-1]["sequence"]
        store.record_dispatch(goal["goal_id"], task, "prepared", peer_sequence=sequence)
        store.fail_task(goal["goal_id"], task, "Transport outcome unknown", uncertain=True)
        restarted = long_horizon.GoalStore(self.config)
        self.assertEqual(len(self.packet(restarted, goal["goal_id"])), 1)

    def test_forked_history_is_evidence_not_a_new_request(self):
        goal = self.create("inherited", facilitator_mode=True)
        self.append(goal, say("work", "Historical request", target="builder", request=True))
        held = self.runtime.store.get(goal["goal_id"])
        target = copy.deepcopy(held)
        target["goal_id"] = "forked-fixture"
        # The archive clone marks origin_goal_id; use a rolled-back transaction
        # so this focused contract check cannot create a second live goal.
        from our_harness import goal_dialogue
        with self.runtime.store._connect() as db:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("BEGIN")
            try:
                goal_dialogue.clone(db, held, target)
                self.assertEqual(peer_delivery.pending(db, target, target["tasks"][0]), [])
            finally:
                db.rollback()

    def test_more_than_one_batch_is_delivered_before_completion(self):
        goal = self.create("batches", facilitator_mode=True)
        for n in range(peer_delivery.BATCH_SIZE + 2):
            self.append(goal, say("work", f"Request {n}", target="builder", request=True))
        result, seen = self.run_replies(goal, lambda *_: say())
        self.assertEqual(result["status"], "complete")
        self.assertEqual([route for route, _ in seen].count("builder-route"), 2)
        self.assertEqual(self.packet(self.runtime.store, goal["goal_id"]), [])

    def test_unknown_receipt_contract_and_changed_owner_do_not_skip_requests(self):
        goal = self.create("binding", facilitator_mode=True)
        self.append(goal, say("work", "Pending request", target="builder", request=True))
        held = self.runtime.store.get(goal["goal_id"])
        task = held["tasks"][0]
        receipt = peer_delivery.state(held, task)
        receipt["received"] = held["dialogue_archive"]["latest_sequence"]
        task["peer_delivery"] = receipt
        self.assertEqual(self.runtime.store.peer_requests(held, task), [])
        moved = copy.deepcopy(held)
        moved["project"]["path"] += "-moved"
        self.assertEqual(peer_delivery.state(moved, task)["received"], 0)
        task["peer_delivery"]["schema_version"] = 0
        self.assertEqual(len(self.runtime.store.peer_requests(held, task)), 1)

    def test_receipt_cannot_claim_undispatched_or_out_of_batch_messages(self):
        goal = self.create("invalid-cursor", facilitator_mode=True)
        task = self.runtime.store.claim_ready(goal["goal_id"], "worker")[0]
        with self.assertRaisesRegex(HarnessError, "prepared request batch"):
            self.runtime.store.record_dispatch(goal["goal_id"], task, "forged", peer_sequence=100)
        self.assertEqual(self.runtime.store.get(goal["goal_id"])["budget"]["provider_calls"], 0)

    def test_delegated_task_does_not_duplicate_participant_inbox(self):
        goal = self.create("single-inbox", facilitator_mode=True)
        self.append(goal, say("work", "For Ada", target="builder", request=True))
        held = self.runtime.store.get(goal["goal_id"])
        delegated = {**copy.deepcopy(held["tasks"][0]), "id": "delegated-leaf", "required_contributor_id": ""}
        held["tasks"].append(delegated)
        self.assertEqual(self.runtime.store.peer_requests(held, delegated), [])
        self.assertEqual(len(self.runtime.store.peer_requests(held, held["tasks"][0])), 1)
