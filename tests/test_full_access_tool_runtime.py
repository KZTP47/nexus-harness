import copy
import json
from pathlib import Path
import sys
import subprocess
import unittest
from unittest import mock

from our_harness import long_horizon, goal_access, goal_tools
from tests import test_long_horizon as fixtures


class FullAccessToolRuntimeTests(unittest.TestCase):
    setUp = fixtures.LongHorizonTests.setUp
    store = fixtures.LongHorizonTests.store

    def create(self, mode="full", request="access-review", independent=False, isolated=False, solo=False):
        participants = ["lead"] if solo else ["lead", "reviewer"] if independent else ["lead", "same-route"]
        return self.store().create(self.board, "project", ["Implement the requested project change"], request,
                                  participant_ids=participants, isolated_workspace=isolated,
                                  policy={"agent_access_mode": mode})

    def propose(self, goal):
        store = self.store()
        task = store.claim_ready(goal["goal_id"], "fixture-worker")[0]
        proposed = fixtures.action("request_review", risk="high", changes=[
            {"path": "new.txt", "content": "delivered", "reason": "Requested output"}])
        proposed["_nexus_baselines"] = {"new.txt": "missing"}
        store.record_dispatch(goal["goal_id"], task, "fixture-proposal")
        store.record_action(goal["goal_id"], task, proposed)
        result = store.stage_review_if_needed(goal["goal_id"], task, proposed)
        return store, task, proposed, result

    def test_full_access_same_provider_pair_does_not_ask_again(self):
        goal = self.create()
        store, task, proposed, result = self.propose(goal)
        self.assertEqual(result, (False, []))
        current = store.get(goal["goal_id"])
        saved = next(one for one in current["tasks"] if one["id"] == task["id"])
        self.assertEqual(saved["full_access_review"]["contract"], goal_access.REVIEW_CONTRACT)
        self.assertFalse(current["interrupts"])
        self.assertFalse(store._needs_review(current, saved, proposed, None))
        restarted = long_horizon.GoalStore(self.config).get(goal["goal_id"])
        self.assertEqual(restarted["tasks"][0]["full_access_review"], saved["full_access_review"])
        # No file publication occurs merely because access was accepted.
        self.assertFalse((self.project / "new.txt").exists())

    def test_ask_and_read_only_do_not_inherit_full_access(self):
        self._assert_mode_requires_review("ask")

    def test_read_only_does_not_inherit_full_access(self):
        self._assert_mode_requires_review("read_only")

    def _assert_mode_requires_review(self, mode):
        goal = self.create(mode, request=mode, isolated=True)
        store, task, proposed, result = self.propose(goal)
        self.assertTrue(result[0])
        self.assertTrue(result[1])
        self.assertEqual(store.get(goal["goal_id"])["status"], "waiting_for_user")
        self.assertFalse((self.project / "new.txt").exists())

    def test_full_access_keeps_available_independent_review(self):
        goal = self.create(independent=True)
        store, task, proposed, result = self.propose(goal)
        self.assertEqual(result, (True, []))
        self.assertTrue(any(one["kind"] == "review" for one in store.get(goal["goal_id"])["tasks"]))

    def test_saved_prompt_recovers_on_full_access_update_and_never_on_rejection(self):
        goal = self.create("ask")
        store, task, proposed, result = self.propose(goal)
        store.release_scheduler(goal["goal_id"], "fixture-worker")
        current = store.get(goal["goal_id"])
        updated = store.update_access(goal["goal_id"], expected_revision=current["revision"], mode="full")
        self.assertEqual(updated["status"], "queued")
        self.assertEqual(updated["tasks"][0]["state"], "pending_apply")
        self.assertEqual(updated["interrupts"][0]["state"], "superseded")
        self.assertTrue(updated["project_queue"]["auto_start_pending"])
        self.assertFalse(long_horizon.GoalStore(self.config).recover_full_access_reviews(goal["goal_id"])[1])

    def test_legacy_full_access_prompt_recovers_after_restart(self):
        goal = self.create("ask")
        store, task, proposed, result = self.propose(goal)
        store.release_scheduler(goal["goal_id"], "fixture-worker")
        def legacy(document, db):
            document["agent_access"]["mode"] = "full"
        store._mutate(goal["goal_id"], legacy)
        recovered, changed = long_horizon.GoalStore(self.config).recover_full_access_reviews(goal["goal_id"])
        self.assertTrue(changed)
        self.assertEqual(recovered["tasks"][0]["state"], "pending_apply")

    def test_changed_proposal_access_or_route_invalidates_fallback(self):
        goal = self.create()
        store, task, proposed, _ = self.propose(goal)
        original = store.get(goal["goal_id"])
        for mutation in ("proposal", "access", "route"):
            current = copy.deepcopy(original)
            action = copy.deepcopy(proposed)
            held = current["tasks"][0]
            if mutation == "proposal":
                action["changes"][0]["content"] = "a different proposal"
            elif mutation == "access":
                current["agent_access"]["mode"] = "ask"
            else:
                current["agents"][0]["route_binding"]["changed"] = "other-machine"
            self.assertTrue(store._needs_review(current, held, action, None), mutation)

    def test_legacy_recovery_rejects_substituted_packet_and_stale_binding(self):
        goal = self.create("ask")
        store, task, proposed, _ = self.propose(goal)
        store.release_scheduler(goal["goal_id"], "fixture-worker")
        def change(document, db):
            document["agent_access"]["mode"] = "full"
            document["tasks"][0]["pending_action"]["changes"][0]["content"] = "substituted"
        store._mutate(goal["goal_id"], change)
        self.assertFalse(store.recover_full_access_reviews(goal["goal_id"])[1])

    def test_goal_loop_executes_own_tools_and_collects_private_changes(self):
        subprocess.run(["git", "init"], cwd=self.project, check=True, capture_output=True)
        goal = self.create(isolated=True)
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        task = runtime.store.claim_ready(goal["goal_id"], "tool-worker")[0]
        replies = [fixtures.action("work", tool_calls=[
            {"call_id": "write", "name": "write_file", "arguments": {"path": "answer.txt", "content": "prepared"}},
            {"call_id": "command", "name": "run_command", "arguments": {"argv": [sys.executable, "-c", "print('NEXUS_COMMAND_EXECUTED')"]}},
            {"call_id": "search", "name": "glob_search", "arguments": {"pattern": "*.txt"}},
            {"call_id": "git", "name": "git_status", "arguments": {}},
        ]), fixtures.action("complete", evidence=["file:answer.txt"])]
        contexts = []
        def ask(*args, **kwargs):
            contexts.append(kwargs["context"])
            return {"text": json.dumps(replies.pop(0))}
        with mock.patch.object(long_horizon.chat_lab, "ask_once", side_effect=ask):
            _, final = runtime._execute_one(goal["goal_id"], task["id"])
        self.assertIn("NEXUS_COMMAND_EXECUTED", contexts[-1])
        self.assertIn("selected_project_repository", contexts[-1])
        self.assertTrue(any(one["path"] == "answer.txt" and one.get("content") == "prepared" for one in final["changes"]))
        self.assertFalse((self.project / "answer.txt").exists())

    def test_effect_tool_requires_engine_full_access(self):
        goal = self.create("ask", isolated=True)
        runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        task = runtime.store.claim_ready(goal["goal_id"], "tool-worker")[0]
        replies = [fixtures.action("work", tool_calls=[{"call_id": "write", "name": "write_file", "arguments": {"path": "answer.txt", "content": "denied"}}]),
                   fixtures.action("complete", evidence=["verified-no-change"])]
        with mock.patch.object(long_horizon.chat_lab, "ask_once", side_effect=lambda *a, **k: {"text": json.dumps(replies.pop(0))}), \
                mock.patch.object(goal_tools, "execute") as execute:
            runtime._execute_one(goal["goal_id"], task["id"])
        execute.assert_not_called()

    def test_reserved_effect_is_not_replayed_after_runtime_restart(self):
        goal = self.create(isolated=True, solo=True)
        runtime = long_horizon.LongHorizonRuntime(self.config)
        task = runtime.store.claim_ready(goal["goal_id"], "tool-worker")[0]
        call = {"call_id": "reserved-command", "name": "run_command", "arguments": {"argv": [sys.executable, "-c", "print('must not replay')"]}}
        runtime.store.record_dispatch(goal["goal_id"], task, "first-effect")
        runtime.store.record_provider_reply(goal["goal_id"], task, phase="initial")
        runtime.store.acknowledge_context_step(goal["goal_id"], task,
            fixtures.action("work", tool_calls=[call]), "initial")
        self.assertTrue(runtime.store.reserve_context_tool(goal["goal_id"], task, call))
        runtime.store.release_scheduler(goal["goal_id"], "tool-worker")
        runtime.close()
        restarted = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(restarted.close)
        restarted.store.recover_dead(goal["goal_id"])
        restarted.store.control(goal["goal_id"], "resume")
        task = restarted.store.claim_ready(goal["goal_id"], "second-worker")[0]
        contexts = []
        def ask(*a, **kw):
            contexts.append(kw["context"])
            return {"text": json.dumps(fixtures.action("complete", evidence=["verified-no-change"]))}
        with mock.patch.object(long_horizon.chat_lab, "ask_once", side_effect=ask), mock.patch.object(goal_tools, "execute") as execute:
            restarted._execute_one(goal["goal_id"], task["id"])
        execute.assert_not_called()
        self.assertIn("outcome_unknown", contexts[-1])


if __name__ == "__main__":
    unittest.main()
