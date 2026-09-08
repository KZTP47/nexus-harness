from __future__ import annotations

import copy
import json
import sys
import unittest
from unittest import mock

from our_harness import goal_verification, long_horizon
from tests import test_long_horizon_dialogue as fixtures
from tests.test_long_horizon_tool_recovery import read


reply = fixtures.reply


def proposal_read(path="result.js"):
    return {"call_id": "inspect-proposal", "name": "read_proposed_change", "arguments": {"path": path}}


class LongHorizonCollaborationRecoveryTests(unittest.TestCase):
    setUp = fixtures.LongHorizonDialogueTests.setUp
    create = fixtures.LongHorizonDialogueTests.create
    provider = fixtures.LongHorizonDialogueTests.provider
    run_replies = fixtures.LongHorizonDialogueTests.run_replies

    def restart(self):
        self.runtime.close()
        self.runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(self.runtime.close)

    def test_unavailable_review_reader_is_hidden_and_request_error_keeps_team_running(self):
        (self.project / "result.js").write_text("export const delivered = true;\n")
        goal = self.create("correct-review-tool")
        formats = []
        responses = [
            reply("work", "I will inspect the proposal.", tool_calls=[proposal_read()]),
            reply("work", "The files are already applied; I will read them.", tool_calls=[read("result.js")]),
            reply(), reply(),
        ]

        def respond(number, _route, kwargs):
            formats.append(kwargs["response_format"])
            return responses[number - 1]

        result, seen = self.run_replies(goal, respond)
        self.assertEqual(result["status"], "complete", result["note"])
        self.assertEqual([route for route, _ in seen], ["builder-route"] * 3 + ["peer-route"])
        self.assertIn("Use read_file", seen[1][1])
        self.assertIn("export const delivered = true", seen[2][1])
        for response_format in formats:
            self.assertNotIn("read_proposed_change", json.dumps(response_format.schema))
        self.assertIn("read_proposed_change", json.dumps(long_horizon._agent_action_format({
            "kind": "review", "review_of": "exact-target",
        }).schema))
        self.assertNotIn("read_proposed_change", json.dumps(long_horizon._agent_action_format({
            "kind": "review", "review_of": "",
        }).schema))
        events = self.runtime.store.events(goal["goal_id"])["events"]
        self.assertTrue(any(one["type"] == "context_tool_failed" for one in events))
        self.assertFalse(any(one["type"] == "task_failed" for one in events))
        self.assertFalse(result["tasks"][0].get("review_paths_inspected"))
        self.assertEqual(result["budget"]["context_tool_calls"], 2)
        self.assertEqual(result["tasks"][0]["context_steps"][0]["agent_id"], "builder")

    def test_legacy_pending_wrong_tool_recovers_after_restart_without_repeating_the_request(self):
        goal = self.create("restart-wrong-review-tool")
        store = self.runtime.store
        task = store.claim_ready(goal["goal_id"], "before-restart")[0]
        store.record_dispatch(goal["goal_id"], task, "original-request")
        store.record_provider_reply(goal["goal_id"], task, phase="initial")
        store.acknowledge_context_step(goal["goal_id"], task, reply("work", tool_calls=[proposal_read()]), "initial")
        store.release_scheduler(goal["goal_id"], "before-restart")
        self.restart()
        self.runtime.store.recover_dead(goal["goal_id"])
        self.runtime.store.control(goal["goal_id"], "resume")
        result, seen = self.run_replies(goal, [reply(), reply()])
        self.assertEqual(result["status"], "complete", result["note"])
        self.assertIn("Use read_file", next(context for route, context in seen if route == "builder-route"))
        step = result["tasks"][0]["context_steps"][0]
        self.assertEqual(step["state"], "complete")
        self.assertEqual(len(step["results"]), 1)
        self.assertIn("targeted review task", step["results"][0]["error"])
        self.assertIsNone(step["results"][0]["result"])
        self.assertEqual(result["budget"]["context_tool_calls"], 1)

    def test_identical_loop_counter_survives_restart(self):
        goal = self.create("restart-identical-discussion")
        store = self.runtime.store
        for number in range(6):
            task = store.claim_ready(goal["goal_id"], "before-restart")[0]
            action = reply("work", "I am still planning the same unchanged step.")
            store.record_dispatch(goal["goal_id"], task, "discussion-" + str(number))
            store.record_provider_reply(goal["goal_id"], task, phase="initial")
            store.record_action(goal["goal_id"], task, action)
            store.apply_action(goal["goal_id"], task, action)
        store.release_scheduler(goal["goal_id"], "before-restart")
        before = store.get(goal["goal_id"])
        self.assertEqual([task["no_progress"] for task in before["tasks"]], [2, 2])
        self.restart()
        self.assertEqual([task["no_progress"] for task in self.runtime.store.get(goal["goal_id"])["tasks"]], [2, 2])
        result, seen = self.run_replies(goal, lambda *_args: reply("work", "I am still planning the same unchanged step."))
        self.assertEqual(result["status"], "paused", result["note"])
        self.assertLessEqual(len(seen), 4)
        self.assertIn("no new evidence", result["note"])

    def test_tool_only_repeat_loop_pauses_durably_and_resumes_with_a_different_observation(self):
        (self.project / "same.js").write_text("export const unchanged = true;\n")
        (self.project / "next.js").write_text("export const usefulNextStep = true;\n")
        goal = self.create("tool-only-loop")
        def sequence_peer(document, _db):
            document["tasks"][1].update({"state": "waiting", "depends_on": [document["tasks"][0]["id"]]})
        self.runtime.store._mutate(goal["goal_id"], sequence_peer)
        self.assertEqual(goal["budget"]["max_provider_calls"], 0)
        self.assertEqual(goal["budget"]["max_context_tool_calls"], 0)
        def repeated(number, _route, _kwargs):
            return reply("work", "I will read this source again, request " + str(number),
                         tool_calls=[read("same.js", call_id="new-id-" + str(number))])
        result, seen = self.run_replies(goal, repeated)
        self.assertEqual(result["status"], "paused", result["note"])
        self.assertEqual(len(seen), 5)
        self.assertIn("same context-tool request", result["note"])
        progress = result["tasks"][0]["context_progress"]
        self.assertEqual(progress["identical_repeats"], 4)
        self.assertEqual(result["budget"]["context_tool_calls"], 5)
        events = self.runtime.store.events(goal["goal_id"])["events"]
        self.assertTrue(any(one["type"] == "context_progress_paused" for one in events))
        self.assertFalse(any(one["type"] == "task_failed" for one in events))
        self.restart()
        self.assertEqual(self.runtime.store.get(goal["goal_id"])["tasks"][0]["context_progress"], progress)
        self.runtime.store.control(goal["goal_id"], "resume")
        still_paused, repeated_seen = self.run_replies(goal, repeated)
        self.assertEqual(still_paused["status"], "paused")
        self.assertEqual(len(repeated_seen), 1)
        self.runtime.store.control(goal["goal_id"], "resume")
        complete, continued = self.run_replies(goal, [
            reply("work", "I will inspect the other source to answer the open question.", tool_calls=[read("next.js")]),
            reply(), reply(),
        ])
        self.assertEqual(complete["status"], "complete", complete["note"])
        self.assertIn("usefulNextStep", continued[1][1])
        self.assertNotIn("context_progress", complete["tasks"][0])

    def test_targeted_user_message_refreshes_tool_progress_without_exposing_it_to_the_peer(self):
        (self.project / "same.js").write_text("export const unchanged = true;\n")
        goal = self.create("message-refreshes-loop")
        def sequence_peer(document, _db):
            document["tasks"][1].update({"state": "waiting", "depends_on": [document["tasks"][0]["id"]]})
        self.runtime.store._mutate(goal["goal_id"], sequence_peer)
        repeat = lambda *_args: reply("work", "I will inspect the same source.", tool_calls=[read("same.js")])
        paused, _seen = self.run_replies(goal, repeat)
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(paused["tasks"][0]["context_progress"]["identical_repeats"], 4)
        private = "Check whether the exported value is a boolean before concluding."
        self.runtime.store.control(goal["goal_id"], "message", {"task_id": paused["tasks"][0]["id"], "text": private})
        continued, contexts = self.run_replies(goal, [repeat(), reply(), reply()])
        self.assertEqual(continued["status"], "complete", continued["note"])
        self.assertIn(private, contexts[0][1])
        self.assertNotIn(private, next(context for route, context in contexts if route == "peer-route"))
        events = self.runtime.store.events(goal["goal_id"])["events"]
        self.assertEqual(sum(one["type"] == "context_progress_paused" for one in events), 1)

    def test_distinct_tool_only_exploration_remains_open_until_agents_complete(self):
        for number in range(12):
            (self.project / ("source-" + str(number) + ".js")).write_text("export const value = " + str(number) + ";\n")
        goal = self.create("productive-tool-exploration")
        result, seen = self.run_replies(goal, [
            *(reply("work", "I will inspect source " + str(number), tool_calls=[read("source-" + str(number) + ".js")])
              for number in range(12)), reply(), reply(),
        ])
        self.assertEqual(result["status"], "complete", result["note"])
        self.assertEqual(len(seen), 14)
        self.assertEqual(result["budget"]["context_tool_calls"], 12)
        self.assertFalse(any(one.get("kind") == "file_transaction" for one in result["artifacts"]))

    def test_legacy_progress_fingerprint_resets_when_public_progress_contract_changes(self):
        goal = self.create("migrate-discussion-progress")
        def old_counter(document, _db):
            document["tasks"][0].update({"progress_fingerprint": "legacy-summary-omitting-fingerprint", "no_progress": 3})
        self.runtime.store._mutate(goal["goal_id"], old_counter)
        self.restart()
        result, _seen = self.run_replies(goal, [
            reply("work", "The reward catalog should use stable IDs so saved equipment survives renames."),
            reply(), reply(),
        ])
        self.assertEqual(result["status"], "complete", result["note"])
        self.assertEqual(result["tasks"][0]["no_progress"], 0)

    def test_plural_goal_verifies_applied_snapshot_after_restart_and_legacy_receipt_refresh(self):
        (self.project / "test_rewards.py").write_text(
            "import unittest\nfrom rewards import balance\n"
            "class Rewards(unittest.TestCase):\n"
            "    def test_balance(self): self.assertEqual(balance(8, 3), 5)\n",
        )
        command = [sys.executable, "-m", "unittest", "discover", "-s", ".", "-p", "test_rewards.py"]
        self.board["projects"][0]["test_commands"] = [command]
        goal = self.runtime.store.create(
            self.board, "game", ["can you guys add a shop where one can use scores to buy themed items xDDD"],
            "plural-applied-restart", lead_id="builder", participant_ids=["builder", "peer"],
            conversation_id="chat-plural-applied-restart",
        )
        task = self.runtime.store.claim_ready(goal["goal_id"], "apply-before-restart")[0]
        delivered = reply(changes=[fixtures.change("rewards.py", "def balance(score, cost): return score - cost\n")],
                          criteria_evidence=[{"criterion": "Original objective is satisfied", "evidence_refs": ["file:rewards.py"]}])
        with mock.patch.object(long_horizon.chat_lab, "ask_once", side_effect=self.provider([delivered], [])):
            executed, action = self.runtime._execute_one(goal["goal_id"], task["id"])
        self.runtime._apply_node({"goal_id": goal["goal_id"], "actions": [{"task": executed, "action": action}]})
        self.runtime.store.release_scheduler(goal["goal_id"], "apply-before-restart")
        applied = self.runtime.store.get(goal["goal_id"])
        self.assertEqual(applied["tasks"][0]["state"], "complete")

        def old_verification(document, _db):
            contract = copy.deepcopy(document["verification_contract"])
            contract.pop("fingerprint_sha256")
            contract.update({"schema_version": 2, "check_policy": copy.deepcopy(goal_verification.LEGACY_CHECK_POLICY)})
            contract["fingerprint_sha256"] = goal_verification._fingerprint(contract)
            document["verification_contract"] = contract
            document.update({"status": "paused", "note": "Old read_only_tree_drift blocked verification."})
            document["tasks"][1].update({
                "state": "blocked", "last_error": "read_proposed_change is available only to a targeted review task",
                "context_steps": [{"state": "complete", "context_binding": long_horizon._context_binding(document),
                                   "calls": [{"name": "run_selected_verification"}],
                                   "results": [{"result": {"status": "failed", "basis": "read_only_tree_drift"}}]}],
            })
        self.runtime.store._mutate(goal["goal_id"], old_verification)
        self.restart()
        resumed = self.runtime.store.control(goal["goal_id"], "resume")
        self.assertEqual(resumed["verification_contract"]["schema_version"], 3)
        self.assertEqual(resumed["verification_contract"]["test_commands"], [command])
        self.assertEqual(resumed["tasks"][1]["context_steps"][0]["state"], "superseded")
        seen = []
        responses = [reply("work", "I will run the selected reward check on the current files.", tool_calls=[{
            "call_id": "verify-current", "name": "run_selected_verification", "arguments": {},
        }]), reply()]
        with mock.patch.object(long_horizon.chat_lab, "ask_once", side_effect=self.provider(responses, seen)):
            result = self.runtime.run(goal["goal_id"])
        self.assertEqual(result["status"], "complete", result["note"])
        self.assertEqual([route for route, _ in seen], ["peer-route", "peer-route"])
        self.assertEqual(result["verification"]["status"], "passed")
        self.assertTrue(result["verification"]["verification_analysis"]["passed"])
        self.assertEqual(sum(one.get("kind") == "file_transaction" for one in result["artifacts"]), 1)
        self.assertEqual((self.project / "rewards.py").read_text(), "def balance(score, cost): return score - cost\n")


if __name__ == "__main__":
    unittest.main()
