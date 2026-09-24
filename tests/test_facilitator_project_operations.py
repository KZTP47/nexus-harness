"""Portable contention, stale-edit and saved-queue regressions."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from our_harness import goal_tools, long_horizon, project_operations as ops
from tests import test_goal_access as access


class OperationLeaseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.root = self.base / "arbitrary-project"
        self.root.mkdir()
        self.state = self.base / "runtime"

    def test_nested_roots_conflict_but_independent_projects_continue(self):
        child = self.root / "nested"
        child.mkdir()
        with ops.claim(child, self.state):
            with self.assertRaises(ops.OperationBusy):
                with ops.claim(self.root, self.state):
                    self.fail("Overlapping writer admitted")
            with ops.claim(self.base / "other-project", self.state):
                pass
        with ops.claim(self.root, self.state):
            pass

    def test_exception_releases_registry_and_file_lock(self):
        with self.assertRaisesRegex(ValueError, "interrupted"):
            with ops.transaction(self.root, self.state):
                raise ValueError("interrupted")
        with ops.transaction(self.root, self.state):
            pass

    def test_other_process_conflicts_and_crashed_owner_is_reclaimed(self):
        script = "from our_harness.project_operations import claim; import sys,time;\nwith claim(sys.argv[1],sys.argv[2]):\n print('leased',flush=True); time.sleep(30)"
        process = subprocess.Popen([sys.executable, "-B", "-c", script, str(self.root), str(self.state)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "leased")
            with self.assertRaises(ops.OperationBusy):
                with ops.claim(self.root, self.state):
                    self.fail("Cross-process overlap admitted")
        finally:
            process.kill()
            process.communicate(timeout=10)
        with ops.claim(self.root, self.state):
            pass


class LeaseQueueTests(unittest.TestCase):
    """A second writer waits for the lease instead of being refused."""
    setUp = OperationLeaseTests.setUp

    def test_waiting_claim_is_admitted_when_the_holder_finishes(self):
        entered, released = threading.Event(), threading.Event()
        def holder():
            with ops.claim(self.root, self.state):
                entered.set()
                released.wait(10)
        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(entered.wait(10))
        threading.Timer(0.3, released.set).start()
        with ops.transaction(self.root, self.state, wait_seconds=10):
            self.assertTrue(released.is_set(), "The waiter was admitted while the first writer held the lease")
        thread.join(10)

    def test_wait_is_bounded_and_says_how_long_it_waited(self):
        with ops.claim(self.root, self.state):
            with self.assertRaisesRegex(ops.OperationBusy, "still using this project. Nexus waited 1 seconds"):
                with ops.claim(self.root, self.state, wait_seconds=1.2):
                    self.fail("Overlapping writer admitted")

    def test_waiting_stops_when_the_goal_is_paused(self):
        calls = []
        def stopped():
            calls.append(1)
            return True
        with ops.claim(self.root, self.state), self.assertRaises(ops.OperationBusy):
            with ops.claim(self.root, self.state, wait_seconds=60, should_stop=stopped):
                self.fail("Overlapping writer admitted")
        self.assertTrue(calls)


class FacilitatorOperationTests(unittest.TestCase):
    create = access.FacilitatorModeTests.create

    def setUp(self):
        access.FacilitatorModeTests.setUp(self)
        # Contention tests hold a lease for their whole body; keep the queue
        # short so "still busy after waiting" is observable quickly.
        for name in ("TOOL_LEASE_WAIT_SECONDS", "APPLY_LEASE_WAIT_SECONDS", "NATIVE_LEASE_WAIT_SECONDS"):
            patcher = mock.patch.object(ops, name, 0.3)
            patcher.start()
            self.addCleanup(patcher.stop)

    def proposal(self, goal, path, content):
        task = self.store.claim_ready(goal["goal_id"], "writer")[0]
        reply = access.fixtures.completion(changes=[{"path": path, "content": content, "reason": "Requested edit"}])
        with mock.patch.object(long_horizon.chat_lab, "ask_once", return_value={"text": json.dumps(reply)}):
            return self.runtime._execute_one(goal["goal_id"], task["id"])

    def apply(self, goal, proposed):
        task, action = proposed
        self.runtime._apply_node({"goal_id": goal["goal_id"], "actions": [{"task": task, "action": action}]})

    def test_two_observed_proposals_preserve_disjoint_edits(self):
        first, second = self.create(request="alpha"), self.create(request="beta")
        a = self.proposal(first, "alpha.txt", "alpha")
        b = self.proposal(second, "beta.txt", "beta")
        self.apply(second, b)
        self.apply(first, a)
        self.assertEqual((self.project / "alpha.txt").read_text(), "alpha")
        self.assertEqual((self.project / "beta.txt").read_text(), "beta")

    def test_stale_same_file_proposal_returns_to_agent_without_overwrite_or_pause(self):
        (self.project / "shared.txt").write_text("original")
        first, second = self.create(request="alpha"), self.create(request="beta")
        a = self.proposal(first, "shared.txt", "alpha")
        b = self.proposal(second, "shared.txt", "beta")
        self.apply(second, b)
        self.apply(first, a)
        self.assertEqual((self.project / "shared.txt").read_text(), "beta")
        current = self.store.get(first["goal_id"])
        task = next(t for t in current["tasks"] if t["id"] == a[0]["id"])
        self.assertEqual(task["state"], "ready")
        self.assertIn("File changed", task["last_error"])
        self.assertFalse(task["pending_action"])
        self.assertNotIn(current["status"], {"paused", "waiting_for_project", "waiting_for_user"})

    def test_busy_proposal_returns_to_author_and_can_be_replanned(self):
        goal = self.create()
        proposed = self.proposal(goal, "new.txt", "new")
        with ops.claim(self.project, self.store.root):
            self.apply(goal, proposed)
        self.assertFalse((self.project / "new.txt").exists())
        self.apply(goal, self.proposal(goal, "new.txt", "new"))
        self.assertEqual((self.project / "new.txt").read_text(), "new")

    def test_tool_write_queues_behind_a_teammate_instead_of_failing(self):
        with mock.patch.object(ops, "TOOL_LEASE_WAIT_SECONDS", 10):
            entered, released = threading.Event(), threading.Event()
            def teammate():
                with ops.transaction(self.project, self.store.root):
                    entered.set()
                    released.wait(10)
            thread = threading.Thread(target=teammate)
            thread.start()
            self.assertTrue(entered.wait(10))
            threading.Timer(0.3, released.set).start()
            result = goal_tools.execute(self.config, self.project, "write_file", {"path": "queued.txt", "content": "queued"},
                                        facilitator=True, runtime_root=self.store.root)
            thread.join(10)
        self.assertEqual(result["applied_to"], "selected_project")
        self.assertEqual((self.project / "queued.txt").read_text(), "queued")

    def test_busy_tools_and_stale_tool_writes_have_truthful_nonexecuted_results(self):
        original = long_horizon._project_baseline_manifest(self.project)
        with ops.claim(self.project, self.store.root):
            result = goal_tools.execute(self.config, self.project, "write_file", {"path": "new.txt", "content": "new"}, facilitator=True, runtime_root=self.store.root)
        self.assertEqual(result["status"], "busy")
        self.assertFalse(result["executed"])
        (self.project / "new.txt").write_text("sibling")
        result = goal_tools.execute(self.config, self.project, "write_file", {"path": "new.txt", "content": "new"}, facilitator=True, runtime_root=self.store.root, expected_baselines=original)
        self.assertEqual(result["status"], "conflict")
        self.assertEqual((self.project / "new.txt").read_text(), "sibling")

    def test_native_turn_falls_back_to_inspection_only_after_a_long_wait(self):
        goal = self.create(mode="full")
        task = self.store.claim_ready(goal["goal_id"], "writer")[0]
        def provider(*args, **kwargs):
            self.assertEqual(kwargs["native_execution"], "inspect")
            self.assertIn("Saved permissions have not changed", kwargs["context"])
            return {"text": json.dumps(access.fixtures.completion())}
        with ops.claim(self.project, self.store.root), mock.patch.object(ops, "native_capable", return_value=True), mock.patch.object(long_horizon.chat_lab, "ask_once", side_effect=provider):
            _, action = self.runtime._execute_one(goal["goal_id"], task["id"])
        self.assertEqual(action["_nexus_direct_changes"], [])
        self.assertEqual(access.goal_access.state(self.store.get(goal["goal_id"]))["mode"], "full")

    def test_api_reply_does_not_reserve_project_or_claim_sibling_changes(self):
        goal = self.create(mode="full")
        task = self.store.claim_ready(goal["goal_id"], "writer")[0]
        def provider(*args, **kwargs):
            with ops.transaction(self.project, self.store.root):
                (self.project / "sibling.txt").write_text("another conversation")
            return {"text": json.dumps(access.fixtures.completion())}
        with mock.patch.object(long_horizon.chat_lab, "ask_once", side_effect=provider):
            _, action = self.runtime._execute_one(goal["goal_id"], task["id"])
        self.assertEqual(action["_nexus_direct_changes"], [])

    def test_existing_waiter_is_promoted_after_restart_while_paused_goal_stays_paused(self):
        owner = self.create(request="owner")
        self.store.control(owner["goal_id"], "pause")
        waiting = self.create(request="waiting")
        def old_queue(doc, db):
            doc.pop("project_coordination", None)
            doc["status"] = "waiting_for_project"
            doc["project_queue"] = self.store._queue_record("waiting", long_horizon._now(), blocked_by_goal_id=owner["goal_id"])
        self.store._mutate(waiting["goal_id"], old_queue)
        restarted = long_horizon.GoalStore(self.config)
        resumed = restarted.get(waiting["goal_id"])
        self.assertEqual(resumed["status"], "queued")
        self.assertTrue(resumed["project_queue"]["auto_start_pending"])
        self.assertEqual(resumed["project_coordination"], ops.CONTRACT)
        self.assertEqual(restarted.get(owner["goal_id"])["status"], "paused")

    def test_external_legacy_owner_blocks_writes_but_not_admission(self):
        goal = self.create()
        with mock.patch.object(self.runtime, "external_project_conflicts", return_value=["legacy"]):
            self.runtime._require_available_project(goal["goal_id"])
            with self.assertRaises(ops.OperationBusy):
                ops.require_coordinated(self.runtime, goal)

    def test_native_operation_releases_after_provider_failure(self):
        goal = self.create(mode="full")
        with mock.patch.object(ops, "native_capable", return_value=True):
            with self.assertRaisesRegex(ValueError, "provider failed"):
                with ops.native_turn(self.runtime, goal, "anything", "work", []):
                    raise ValueError("provider failed")
        with ops.transaction(self.project, self.store.root):
            pass

    def test_two_native_conversations_both_keep_write_access_and_serialize(self):
        first, second = self.create(request="native-a", mode="full"), self.create(request="native-b", mode="full")
        a = self.store.claim_ready(first["goal_id"], "a")[0]
        b = self.store.claim_ready(second["goal_id"], "b")[0]
        entered, release = threading.Event(), threading.Event()
        errors, results, profiles, order = [], {}, {}, []
        def provider(*args, **kwargs):
            name = threading.current_thread().name
            profiles[name] = kwargs["native_execution"]
            order.append(name + ":start")
            if name == "native-a":
                entered.set()
                self.assertTrue(release.wait(10), "The first conversation was never released")
            (self.project / (name + ".txt")).write_text(name)
            order.append(name + ":end")
            return {"text": json.dumps(access.fixtures.completion())}
        def run(goal, task, name):
            try:
                results[name] = self.runtime._execute_one(goal["goal_id"], task["id"])
            except BaseException as exc:
                errors.append(exc)
        with mock.patch.object(ops, "NATIVE_LEASE_WAIT_SECONDS", 20), \
                mock.patch.object(ops, "native_capable", return_value=True), \
                mock.patch.object(long_horizon.chat_lab, "ask_once", side_effect=provider):
            one = threading.Thread(target=run, args=(first, a, "native-a"), name="native-a")
            one.start()
            self.assertTrue(entered.wait(10))
            two = threading.Thread(target=run, args=(second, b, "native-b"), name="native-b")
            two.start()
            two.join(0.5)
            self.assertNotIn("native-b:start", order, "Two writable native sessions overlapped")
            release.set()
            one.join(20)
            two.join(20)
        if errors:
            raise errors[0]
        self.assertEqual(profiles, {"native-a": "work", "native-b": "work"})
        self.assertEqual(order, ["native-a:start", "native-a:end", "native-b:start", "native-b:end"])
        self.assertEqual([x["path"] for x in results["native-a"][1]["_nexus_direct_changes"]], ["native-a.txt"])
        self.assertEqual([x["path"] for x in results["native-b"][1]["_nexus_direct_changes"]], ["native-b.txt"])

    def test_private_publication_respects_facilitator_operation_lease(self):
        goal = self.create(isolated=True)
        with ops.claim(self.project, self.store.root):
            with self.assertRaises(ops.OperationBusy):
                with access.goal_workspaces.publication(goal, self.store.root, timeout_seconds=0):
                    self.fail("Publication overlapped a writer")
        with access.goal_workspaces.publication(goal, self.store.root, timeout_seconds=0):
            pass

    def test_inspection_tool_still_runs_while_write_operation_is_busy(self):
        goal = self.create(mode="read_only")
        (self.project / "readme.txt").write_text("Inspection remains available")
        task = self.store.claim_ready(goal["goal_id"], "reader")[0]
        replies = [access.fixtures.completion(action="work", tool_calls=[
            {"call_id": "read", "name": "read_file", "arguments": {"path": "readme.txt", "start_line": 1, "end_line": 20, "max_bytes": 4000}}]), access.fixtures.completion()]
        seen = []
        def provider(*args, **kwargs):
            seen.append(kwargs["context"])
            return {"text": json.dumps(replies.pop(0))}
        with ops.claim(self.project, self.store.root), mock.patch.object(long_horizon.chat_lab, "ask_once", side_effect=provider):
            self.runtime._execute_one(goal["goal_id"], task["id"])
        self.assertEqual(len(seen), 2)
        self.assertIn("Inspection remains available", seen[-1])

    def test_busy_final_verification_returns_control_to_agent_without_completion(self):
        goal = self.create()
        self.apply(goal, self.proposal(goal, "result.txt", "result"))
        with ops.claim(self.project, self.store.root), mock.patch.object(long_horizon.swarm_work, "_run_selected_project_verification") as verify:
            result = self.runtime._verify_node({"goal_id": goal["goal_id"]})
        verify.assert_not_called()
        current = self.store.get(goal["goal_id"])
        self.assertEqual(result["route"], "schedule")
        self.assertEqual(current["status"], "queued")
        self.assertEqual(current["verification"]["status"], "busy")
        self.assertTrue(any(t["state"] == "ready" for t in current["tasks"]))

    def test_explicit_real_workspace_edit_is_attributed_from_its_transaction(self):
        goal = self.store.create(self.fixture.board, "tiny-game", ["Improve the project"], "direct-workspace",
            participant_ids=["creator", "reviewer"], facilitator_mode=True,
            policy={"agent_access_mode": "full", "collaboration": {"allow_direct_real_edits": True}})
        task = self.store.claim_ready(goal["goal_id"], "writer")[0]
        fingerprint = access.goal_workspaces._digest(access.workspace_collaboration.aw.inventory(self.project))
        replies = [access.fixtures.completion(action="work", tool_calls=[{
            "call_id": "workspace-save", "name": "workspace_edit", "arguments": {
                "workspace_id": "real", "expected_fingerprint": fingerprint,
                "changes": [{"path": "workspace.txt", "content": "workspace output", "delete": False, "reason": "Requested workspace edit"}]}}]),
            access.fixtures.completion()]
        with mock.patch.object(long_horizon.chat_lab, "ask_once", side_effect=lambda *a, **kw: {"text": json.dumps(replies.pop(0))}):
            _, action = self.runtime._execute_one(goal["goal_id"], task["id"])
        self.assertEqual((self.project / "workspace.txt").read_text(), "workspace output")
        self.assertEqual([one["path"] for one in action["_nexus_direct_changes"]], ["workspace.txt"])
