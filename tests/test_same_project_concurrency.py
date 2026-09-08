from __future__ import annotations

import json
from pathlib import Path
import threading
import time
import unittest
from unittest import mock

from our_harness import long_horizon
from tests import test_long_horizon as legacy_fixture


class SameProjectConcurrencyTests(unittest.TestCase):
    def setUp(self):
        legacy_fixture.LongHorizonTests.setUp(self)
        for provider in self.config.data["providers"].values():
            provider["max_concurrency"] = 2
        self.runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(self.runtime.close)

    def wait_for(self, goal_id, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = self.runtime.store.get(goal_id)
            if predicate(current):
                return current
            time.sleep(0.02)
        self.fail(f"Goal did not reach expected state: {current}")

    def start(self, suffix):
        return self.runtime.start(
            self.board, "project", [f"Create result-{suffix}.txt. NEXUS-ISOLATED-{suffix.upper()}"],
            f"same-project-{suffix}", lead_id="lead", participant_ids=["lead", "reviewer"],
            conversation_id=f"saved-chat-{suffix}",
        )

    @staticmethod
    def result_action(suffix):
        target = f"result-{suffix}.txt"
        return legacy_fixture.action(
            summary=f"Created the exact {suffix} result", evidence=[f"file:{target}"],
            changes=[{"path": target, "content": f"{suffix} complete\n", "delete": False}],
            criteria_evidence=[{"criterion": criterion, "evidence_refs": [f"file:{target}"]}
                for criterion in ["Original objective is satisfied", "Every required task is complete",
                                  "Configured deterministic verification passes"]],
        )

    def test_same_project_saved_chats_overlap_and_publish_b_then_a_without_lost_results(self):
        entered = {suffix: threading.Event() for suffix in "ab"}
        released = {suffix: threading.Event() for suffix in "ab"}
        calls, verification_roots = [], []
        calls_lock = threading.Lock()

        def provider(_config, route, _text, **kwargs):
            context = kwargs["context"]
            suffix = "a" if "NEXUS-ISOLATED-A" in context else "b"
            self.assertIn(f"NEXUS-ISOLATED-{suffix.upper()}", context)
            kwargs["before_provider_dispatch"]("initial")
            with calls_lock:
                calls.append((suffix, route))
                first_reply = sum(one[0] == suffix for one in calls) == 1
            entered[suffix].set()
            if not released[suffix].wait(15):
                raise AssertionError(f"Provider {suffix} barrier was never released")
            kwargs["after_provider_response"]("initial")
            answer = self.result_action(suffix)
            if not first_reply:
                answer["changes"] = []
            return {"text": json.dumps(answer)}

        def verify(_config, root, _project, objective, *_args, **_kwargs):
            suffix = "a" if "NEXUS-ISOLATED-A" in objective else "b"
            self.assertFalse(root.samefile(self.project))
            self.assertEqual((root / f"result-{suffix}.txt").read_text(), f"{suffix} complete\n")
            self.assertFalse((self.project / f"result-{suffix}.txt").exists())
            verification_roots.append((suffix, root))
            return {"status": "passed", "basis": "Exact isolated file bytes inspected before publication"}

        with mock.patch.object(long_horizon.chat_lab, "ask_once", side_effect=provider), mock.patch.object(
            long_horizon.swarm_work, "_run_selected_project_verification", side_effect=verify,
        ):
            try:
                first = self.start("a")
                self.assertTrue(entered["a"].wait(10), self.runtime.store.get(first["goal_id"]))
                second = self.start("b")
                self.assertTrue(entered["b"].wait(10), self.runtime.store.get(second["goal_id"]))
                first_now = self.runtime.store.get(first["goal_id"])
                second_now = self.runtime.store.get(second["goal_id"])
                roots = [long_horizon._execution_root(one) for one in (first_now, second_now)]
                self.assertNotEqual(*roots)
                for one in (first_now, second_now):
                    self.assertEqual(one["status"], "running")
                    self.assertTrue(Path(one["project"]["path"]).samefile(self.project))
                    self.assertTrue(any(task["provider_effect_state"] == "dispatched" for task in one["tasks"]))
                self.assertFalse((self.project / "result-a.txt").exists())
                self.assertFalse((self.project / "result-b.txt").exists())
                released["b"].set()
                second_done = self.wait_for(second["goal_id"], lambda one: one["status"] == "complete")
                self.assertEqual(second_done["workspace_publication"]["state"], "published")
                self.assertEqual((self.project / "result-b.txt").read_text(), "b complete\n")
                self.assertFalse((self.project / "result-a.txt").exists())
                self.assertFalse((roots[0] / "result-b.txt").exists(), "A must not share B's mutable working tree")
                self.assertEqual(self.runtime.store.get(first["goal_id"])["status"], "running")
                released["a"].set()
                first_done = self.wait_for(first["goal_id"], lambda one: one["status"] == "complete")
                self.assertEqual(first_done["workspace_publication"]["state"], "published")
                for suffix in "ab":
                    self.assertEqual((self.project / f"result-{suffix}.txt").read_text(), f"{suffix} complete\n")
                self.assertEqual([one[0] for one in verification_roots], ["b", "a"])
                self.assertEqual(set(calls), {(suffix, route) for suffix in "ab" for route in ("codex", "claude")})
                for one in (first_done, second_done):
                    events = self.runtime.store.events(one["goal_id"])["events"]
                    self.assertEqual(sum(event["type"] == "workspace_published" for event in events), 1)
            finally:
                for event in released.values():
                    event.set()
                self.runtime.close()

    def test_pausing_or_cancelling_one_isolated_goal_does_not_change_its_sibling(self):
        store = self.runtime.store
        first = store.create(self.board, "project", ["First"], "control-a",
                             conversation_id="chat-a", isolated_workspace=True)
        second = store.create(self.board, "project", ["Second"], "control-b",
                              conversation_id="chat-b", isolated_workspace=True)
        task = store.claim_ready(second["goal_id"], "second-worker")[0]
        store.record_dispatch(second["goal_id"], task, "second-dispatch")
        before = store.get(second["goal_id"])
        store.control(first["goal_id"], "pause")
        self.assertEqual(store.get(second["goal_id"]), before)
        cancelled = store.control(first["goal_id"], "cancel")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(store.get(second["goal_id"]), before)
        self.assertTrue(long_horizon._execution_root(before).is_dir())

    def test_plain_store_goals_keep_the_legacy_exclusive_project_contract(self):
        store = self.runtime.store
        first = store.create(self.board, "project", ["Legacy owner"], "legacy-a")
        second = store.create(self.board, "project", ["Legacy waiter"], "legacy-b")
        self.assertFalse(first.get("execution_workspace"))
        self.assertFalse(second.get("execution_workspace"))
        self.assertEqual(second["status"], "waiting_for_project")
        self.assertEqual(second["project_queue"]["blocked_by_goal_id"], first["goal_id"])


if __name__ == "__main__":
    unittest.main()
