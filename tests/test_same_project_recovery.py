from __future__ import annotations

import copy
import unittest
from unittest import mock

from our_harness import long_horizon
from tests import test_long_horizon as legacy_fixture


class SameProjectRecoveryTests(unittest.TestCase):
    def setUp(self):
        legacy_fixture.LongHorizonTests.setUp(self)
        self.runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(self.runtime.close)
        self.store = self.runtime.store

    def legacy(self, request, objectives=None):
        return self.store.create(self.board, "project", objectives or [request], request,
                                 conversation_id="saved-" + request)

    def recover_without_dispatch(self):
        with mock.patch.object(self.runtime, "_enable_auto_start_watcher"), mock.patch.object(
            self.runtime, "start_background", side_effect=lambda goal_id, *_args, **_kwargs: self.store.get(goal_id),
        ), mock.patch.object(long_horizon.chat_lab, "ask_once", side_effect=AssertionError("Recovery sent a provider request")):
            return self.runtime.recover_all()

    def test_stopped_preupgrade_chats_adopt_independent_roots_and_preserve_prior_effects_and_budget(self):
        original = self.legacy("old-owner", ["Keep earlier work", "Finish remaining work"])
        target = self.project / "earlier.txt"
        target.write_text("already applied before upgrade\n", encoding="utf-8")

        def settled_work(document, _db):
            document["status"] = "paused"
            document["budget"]["provider_calls"] = 3
            document["tasks"][0]["state"] = "complete"
            document["tasks"][0]["attempts"] = 2
            document["artifacts"] = [{"kind": "applied", "changes": [{"path": "earlier.txt", "delete": False}]}]

        self.store._mutate(original["goal_id"], settled_work)
        waiter = self.legacy("old-waiter")
        self.assertEqual(waiter["status"], "waiting_for_project")
        before = self.store.get(original["goal_id"])
        self.recover_without_dispatch()
        after = self.store.get(original["goal_id"])
        waiting_after = self.store.get(waiter["goal_id"])
        self.assertEqual(after["status"], "paused")
        self.assertEqual(after["budget"], before["budget"])
        self.assertEqual(after["tasks"], before["tasks"])
        self.assertEqual(after["artifacts"], before["artifacts"])
        self.assertEqual(after["conversation_id"], before["conversation_id"])
        self.assertEqual(after["project"], before["project"])
        self.assertTrue(after["execution_workspace"])
        self.assertTrue(waiting_after["execution_workspace"])
        self.assertNotEqual(waiting_after["status"], "waiting_for_project")
        first_root = long_horizon._execution_root(after)
        second_root = long_horizon._execution_root(waiting_after)
        self.assertNotEqual(first_root, second_root)
        for root in [self.project, first_root, second_root]:
            self.assertEqual((root / "earlier.txt").read_text(), "already applied before upgrade\n")
        self.recover_without_dispatch()
        self.assertEqual(self.store.get(original["goal_id"])["execution_workspace"], after["execution_workspace"])
        events = self.store.events(original["goal_id"])["events"]
        self.assertEqual(sum(one["type"] == "goal_workspace_adopted" for one in events), 1)

    def test_preupgrade_pending_decision_survives_adoption_with_exact_interrupt_and_budget(self):
        original = self.legacy("old-question")
        task = self.store.claim_ready(original["goal_id"], "question-worker")[0]
        self.store.apply_action(original["goal_id"], task, legacy_fixture.action(
            "ask_user", interrupt_reason="requirement_ambiguity", questions=[{
                "id": "choice", "prompt": "Which saved option?", "multiple": False, "allow_other": True,
                "options": [{"label": "Keep A", "description": "Retain the first option", "recommended": True}],
            }],
        ))
        self.store.release_scheduler(original["goal_id"], "question-worker")
        before = self.store.get(original["goal_id"])
        self.recover_without_dispatch()
        after = self.store.get(original["goal_id"])
        self.assertTrue(after["execution_workspace"])
        self.assertEqual(after["status"], "waiting_for_user")
        self.assertEqual(after["interrupts"], before["interrupts"])
        self.assertEqual(after["budget"], before["budget"])
        self.assertEqual(after["tasks"], before["tasks"])

    def test_live_preupgrade_scheduler_prevents_moving_it_or_its_waiter_until_released(self):
        owner = self.legacy("live-old-owner")
        waiter = self.legacy("live-old-waiter")
        self.assertTrue(self.store.claim_scheduler(owner["goal_id"], "live-scheduler"))
        try:
            self.recover_without_dispatch()
            for one in (owner, waiter):
                self.assertFalse(self.store.get(one["goal_id"]).get("execution_workspace"))
            self.assertEqual(self.store.get(owner["goal_id"])["worker"]["worker_id"], "live-scheduler")
            self.assertEqual(self.store.get(waiter["goal_id"])["status"], "waiting_for_project")
        finally:
            self.store.release_scheduler(owner["goal_id"], "live-scheduler")
        self.recover_without_dispatch()
        for one in (owner, waiter):
            self.assertTrue(self.store.get(one["goal_id"])["execution_workspace"])

    def test_unsettled_preupgrade_action_is_not_rebound_to_a_new_execution_root(self):
        original = self.legacy("old-pending-action")
        task = self.store.claim_ready(original["goal_id"], "pending-worker")[0]
        pending = legacy_fixture.action(changes=[{"path": "pending.txt", "content": "exact pending bytes", "delete": False}])
        pending["_nexus_baselines"] = {"pending.txt": "missing"}
        self.store.record_dispatch(original["goal_id"], task, "old-pending-digest")
        self.store.record_action(original["goal_id"], task, pending)
        self.store.control(original["goal_id"], "pause")
        self.store.defer_pending_action(original["goal_id"], task)
        self.store.release_scheduler(original["goal_id"], "pending-worker")
        before = self.store.get(original["goal_id"])
        self.recover_without_dispatch()
        after = self.store.get(original["goal_id"])
        self.assertFalse(after.get("execution_workspace"))
        self.assertEqual(after["tasks"][0]["pending_action"], before["tasks"][0]["pending_action"])
        self.assertEqual(after["budget"], before["budget"])
        self.assertFalse((self.project / "pending.txt").exists())

    def test_cancel_recovery_rolls_back_only_its_own_isolated_transaction(self):
        (self.project / "shared.txt").write_text("selected original\n", encoding="utf-8")
        first = self.store.create(self.board, "project", ["First"], "isolated-transaction-a",
                                  conversation_id="transaction-a", isolated_workspace=True)
        second = self.store.create(self.board, "project", ["Second"], "isolated-transaction-b",
                                   conversation_id="transaction-b", isolated_workspace=True)
        first_root, second_root = [long_horizon._execution_root(one) for one in (first, second)]
        (second_root / "shared.txt").write_text("sibling result remains\n", encoding="utf-8")
        task = self.store.claim_ready(first["goal_id"], "transaction-worker")[0]
        pending = legacy_fixture.action(changes=[{"path": "shared.txt", "content": "first after\n", "delete": False}])
        pending["_nexus_baselines"] = {"shared.txt": long_horizon._path_baseline_marker(first_root, "shared.txt")}
        self.store.record_dispatch(first["goal_id"], task, "transaction-digest")
        self.store.record_action(first["goal_id"], task, pending)
        transaction_id = long_horizon.FileTransaction.new_transaction_id()
        self.store.prepare_transaction(first["goal_id"], task, transaction_id, pending["changes"])
        changes = long_horizon.swarm_work._validated_changes(first_root, pending["changes"])
        long_horizon.FileTransaction(first_root).prepare(changes, transaction_id=transaction_id)
        (first_root / "shared.txt").write_bytes(b"first after\n")
        sibling_before = copy.deepcopy(self.store.get(second["goal_id"]))

        restarted = long_horizon.GoalStore(self.config)
        cancelled = restarted.control(first["goal_id"], "cancel")

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertFalse(cancelled["tasks"][0]["pending_transaction"])
        self.assertFalse(cancelled["tasks"][0]["pending_action"])
        self.assertEqual((first_root / "shared.txt").read_text(), "selected original\n")
        self.assertEqual((second_root / "shared.txt").read_text(), "sibling result remains\n")
        self.assertEqual((self.project / "shared.txt").read_text(), "selected original\n")
        self.assertEqual(restarted.get(second["goal_id"]), sibling_before)

    def test_unavailable_legacy_project_does_not_block_other_chat_recovery(self):
        unavailable = self.legacy("unavailable-source")
        self.store.control(unavailable["goal_id"], "pause")
        before = self.store.get(unavailable["goal_id"])
        other = self.base / "another-project"
        other.mkdir()
        other_board = copy.deepcopy(self.board)
        other_board["projects"][0]["path"] = str(other)
        available = self.store.create(other_board, "project", ["Independent work"], "available-source",
                                      conversation_id="available-chat")
        moved = self.base / "disconnected-project"
        self.project.rename(moved)
        self.recover_without_dispatch()
        after = self.store.get(unavailable["goal_id"])
        self.assertEqual(after["status"], "paused")
        self.assertEqual(after["workspace_migration"]["state"], "unavailable")
        self.assertFalse(after.get("execution_workspace"))
        self.assertEqual(after["tasks"], before["tasks"])
        self.assertEqual(after["budget"], before["budget"])
        self.assertTrue(self.store.get(available["goal_id"])["execution_workspace"])
        # A changed directory at the old spelling must not acquire the saved authority.
        self.project.mkdir()
        self.recover_without_dispatch()
        self.assertFalse(self.store.get(unavailable["goal_id"]).get("execution_workspace"))
        self.project.rmdir()
        moved.rename(self.project)
        self.recover_without_dispatch()
        restored = self.store.get(unavailable["goal_id"])
        self.assertTrue(restored["execution_workspace"])
        self.assertNotIn("workspace_migration", restored)
        self.assertEqual(restored["status"], "paused")
        self.assertEqual(restored["budget"], before["budget"])


if __name__ == "__main__":
    unittest.main()
