from __future__ import annotations

import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from our_harness import agent_workspaces as aw, goal_workspaces as gw
from our_harness.changes import FileTransaction
from our_harness.models import HarnessError, ProviderWorkspaceContext
from our_harness.swarm_work import _validated_changes
from our_harness.providers import native_execution


class AgentWorkspaces(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project, self.runtime = self.base / "customer project", self.base / "runtime"
        self.project.mkdir()
        (self.project / "app.txt").write_text("dirty original", encoding="utf-8")
        self.agents = [{"id": "first/portable", "name": "Agent A", "who": "custom-route-A"},
                       {"id": "second", "name": "Agent B", "who": "custom-route-B"}]
        self.goal = {"goal_id": "test-goal", "project": {"id": "project", "path": str(self.project)},
                     "agents": self.agents, "project_authority_id": "authority"}
        self.goal["execution_workspace"] = gw.create(self.goal, self.runtime)
        self.team = gw.root(self.goal, self.runtime)

    def open(self, agent=0, goal=None):
        return aw.workspace(goal or self.goal, (goal or self.goal)["agents"][agent], self.runtime)

    def test_transient_windows_metadata_lock_retries_without_losing_authenticated_state(self):
        with self.open():
            pass
        home, folder, root = aw.layout(self.goal, self.agents[0], self.runtime)
        state = aw._read(home, folder)
        error = PermissionError("transient reader lock")
        error.winerror = 32
        write = aw.atomic_write
        calls = []
        def locked(path, content):
            calls.append(path)
            if len(calls) == 1:
                self.assertEqual(aw._read(home, folder), state)
                raise error
            return write(path, content)
        with patch.object(aw, "atomic_write", side_effect=locked), patch.object(aw.time, "sleep"):
            aw._write(home, folder, state)
        self.assertEqual(len(calls), 2)
        self.assertEqual(aw.existing_root(self.goal, self.agents[0], self.runtime), root)
        self.assertEqual(aw._read(home, folder), state)

    def test_persistent_metadata_denial_is_bounded_and_preserves_previous_state(self):
        with self.open():
            pass
        home, folder, _ = aw.layout(self.goal, self.agents[0], self.runtime)
        state = aw._read(home, folder)
        for windows_code, attempts in [(5, 20), (None, 1)]:
            error = PermissionError("persistent denial")
            if windows_code is not None:
                error.winerror = windows_code
            with patch.object(aw, "atomic_write", side_effect=error) as write, patch.object(aw.time, "sleep"):
                with self.assertRaises(PermissionError):
                    aw._write(home, folder, {**state, "baseline": {}})
                self.assertEqual(write.call_count, attempts)
            self.assertEqual(aw._read(home, folder), state)

    def test_native_and_structured_edits_are_private_then_publish_exact_binary_and_text(self):
        with self.open() as a:
            (a.root / "app.txt").write_text("candidate", encoding="utf-8")
            (a.root / "image.bin").write_bytes(bytes(range(256)))
            action = a.collect_action({"action": "complete", "changes": [
                {"path": "new.txt", "content": "structured candidate", "reason": "needed"}]}, max_bytes=10000)
            with self.open(1) as b:
                self.assertEqual((b.root / "app.txt").read_text(), "dirty original")
                self.assertNotEqual(a.root, b.root)
            self.assertEqual((self.project / "app.txt").read_text(), "dirty original")
            self.assertEqual((self.team / "app.txt").read_text(), "dirty original")
            self.assertEqual((a.root / "new.txt").read_text(), "structured candidate")
            FileTransaction(self.team).apply(_validated_changes(self.team, action["changes"]))
        with gw.publication(self.goal, self.runtime):
            gw.publish(self.goal, self.runtime, gw.prepare_publish(self.goal, self.runtime))
        self.assertEqual((self.project / "image.bin").read_bytes(), bytes(range(256)))
        self.assertEqual((self.project / "app.txt").read_text(), "candidate")

    def test_restart_retains_rejected_work_and_rebases_disjoint_accepted_work(self):
        with self.open() as a:
            root = a.root
            (root / "draft.txt").write_text("not accepted")
        (self.team / "peer.txt").write_text("accepted B")
        with self.open() as restarted:
            self.assertEqual(root, restarted.root)
            self.assertEqual((root / "draft.txt").read_text(), "not accepted")
            self.assertEqual((root / "peer.txt").read_text(), "accepted B")
            self.assertEqual([x["path"] for x in restarted.changes()], ["draft.txt"])

    def test_conflict_preserves_both_versions(self):
        with self.open() as a:
            root = a.root
            (root / "app.txt").write_text("agent draft")
        (self.team / "app.txt").write_text("accepted other draft")
        with self.assertRaises(gw.WorkspaceConflict), self.open():
            pass
        self.assertEqual((root / "app.txt").read_text(), "agent draft")
        self.assertEqual((self.team / "app.txt").read_text(), "accepted other draft")

    def test_native_candidates_honor_the_configured_file_budget(self):
        with self.open() as candidate:
            for index in range(15):
                (candidate.root / (str(index) + ".txt")).write_text("candidate")
            with self.assertRaises(HarnessError):
                candidate.collect_action({"action": "complete", "changes": []}, max_files=12, max_bytes=10000)
            proposed = candidate.collect_action({"action": "complete", "changes": []}, max_files=24, max_bytes=10000)
            self.assertEqual(len(proposed["changes"]), 15)
        self.assertEqual(list(self.project.glob("[0-9]*.txt")), [])

    def test_cancelled_task_preserves_draft_and_cancellation_stops_copying(self):
        from our_harness import cancellation
        with self.assertRaises(cancellation.ChatCancelled), self.open() as candidate:
            root = candidate.root
            (root / "draft.txt").write_text("unfinished")
            raise cancellation.ChatCancelled("stopped")
        with self.open() as restarted:
            self.assertEqual((restarted.root / "draft.txt").read_text(), "unfinished")
        with patch.object(cancellation, "checkpoint", side_effect=cancellation.ChatCancelled("stopped")):
            with self.assertRaises(cancellation.ChatCancelled):
                aw.inventory(root)
        self.assertFalse((self.project / "draft.txt").exists())

    def test_interrupted_copy_retries_the_recorded_sync(self):
        original = gw._copy_file
        with patch.object(gw, "_copy_file", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError), self.open():
                pass
        with self.open() as recovered:
            self.assertEqual((recovered.root / "app.txt").read_text(), "dirty original")
            self.assertEqual(recovered.changes(), [])

    def test_changed_route_or_contract_does_not_reuse_old_state(self):
        with self.open() as a:
            old = a.root
            (old / "kept.txt").write_text("old route draft")
        changed = copy.deepcopy(self.goal)
        changed["agents"][0]["who"] = "arbitrary-different-route"
        with self.open(goal=changed) as new:
            self.assertNotEqual(old, new.root)
            self.assertFalse((new.root / "kept.txt").exists())
        self.assertEqual((old / "kept.txt").read_text(), "old route draft")

    def test_tampered_state_and_links_are_rejected(self):
        with self.open() as a:
            root = a.root
        state = root.parent / "state.json"
        held = json.loads(state.read_text())
        held["baseline"] = {}
        state.write_text(json.dumps(held))
        with self.assertRaises(HarnessError), self.open():
            pass
        with self.open(1) as b:
            os.link(self.project / "app.txt", b.root / "linked.txt")
            with self.assertRaises(HarnessError):
                b.changes()

    def test_copy_viewer_is_scoped_and_paginates_exact_text(self):
        with self.open() as a:
            content = "long unicode 🙂\n" * 4000
            (a.root / "long.txt").write_text(content, encoding="utf-8")
            (a.root / "binary.bin").write_bytes(b"\xff\x00\x81")
        first = aw.inspect(self.goal, self.runtime, self.agents[0]["id"], "long.txt")
        combined = first["content"]
        page = first
        while page["next_cursor"]:
            page = aw.inspect(self.goal, self.runtime, self.agents[0]["id"], "long.txt", cursor=page["next_cursor"])
            combined += page["content"]
        self.assertEqual(combined.replace("\r\n", "\n"), content)
        self.assertEqual(aw.inspect(self.goal, self.runtime, self.agents[0]["id"], "binary.bin")["kind"], "binary")
        for workspace, path in [("unknown", ""), ("real", "../escape"), ("real", str(self.project / "app.txt")), ("real", ".harness/private")]:
            with self.subTest(workspace=workspace, path=path), self.assertRaises((HarnessError, FileNotFoundError)):
                aw.inspect(self.goal, self.runtime, workspace, path)

    def test_native_contract_refuses_real_project_and_mismatched_copy(self):
        from our_harness.models import ProviderRequest
        with self.open() as a:
            req = ProviderRequest("", "", [], "model", native_execution="work", working_directory=str(a.root),
                workspace_context=ProviderWorkspaceContext("project", str(self.project), str(a.root)))
            self.assertEqual(native_execution.workspace(req), a.root)
            with self.assertRaises(HarnessError):
                native_execution.workspace(replace(req, working_directory=str(self.project)))
            with self.assertRaises(HarnessError):
                native_execution.workspace(replace(req, native_execution="bypass"))

    def test_no_silent_native_structured_disagreement_or_over_budget_publication(self):
        with self.open() as a:
            (a.root / "app.txt").write_text("native")
            with self.assertRaisesRegex(HarnessError, "disagree"):
                a.collect_action({"action": "complete", "changes": [{"path": "app.txt", "content": "different"}]}, max_bytes=1000)
            with self.assertRaisesRegex(HarnessError, "budget"):
                a.changes(max_bytes=1)
        self.assertEqual((self.project / "app.txt").read_text(), "dirty original")


if __name__ == "__main__":
    unittest.main()
