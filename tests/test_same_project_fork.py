from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import subprocess
import unittest
from unittest import mock

from our_harness import long_horizon
from our_harness.models import HarnessError
from tests import test_long_horizon as legacy_fixture


class SameProjectForkTests(unittest.TestCase):
    def setUp(self):
        legacy_fixture.LongHorizonTests.setUp(self)
        self.git("init", "--quiet")
        self.git("config", "user.name", "Nexus temporary fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "core.autocrlf", "false")
        (self.project / ".gitignore").write_text(".harness/\n", encoding="utf-8")
        (self.project / "work.txt").write_text("committed source\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "Initial fixture")
        self.runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(self.runtime.close)

    def git(self, *arguments):
        result = subprocess.run(["git", "-C", str(self.project), *arguments],
                                capture_output=True, text=True, check=True)
        return result.stdout

    def paused_goal(self, request="isolated-source", *, isolated=True):
        goal = self.runtime.store.create(
            self.board, "project", ["Keep this exact file checkpoint"], request,
            conversation_id="saved-" + request, isolated_workspace=isolated,
        )
        self.runtime.store.control(goal["goal_id"], "pause")
        return self.runtime.store.get(goal["goal_id"])

    def test_private_changed_added_or_deleted_file_refuses_fork_without_git_effects(self):
        for kind in ("changed", "added", "deleted"):
            with self.subTest(kind=kind):
                goal = self.paused_goal("private-" + kind)
                private = long_horizon._execution_root(goal)
                if kind == "changed":
                    (private / "work.txt").write_text("private saved progress\n", encoding="utf-8")
                elif kind == "added":
                    (private / "private.txt").write_text("private added progress\n", encoding="utf-8")
                else:
                    (private / "work.txt").unlink()
                before = {path.name: path.read_bytes() for path in private.iterdir() if path.is_file()}
                worktrees = self.git("worktree", "list", "--porcelain")
                self.assertEqual(self.git("status", "--porcelain"), "")
                with self.assertRaisesRegex(HarnessError, "independent working copy differs.*Publish or reconcile"):
                    self.runtime.fork(goal["goal_id"], "rejected-" + kind)
                self.assertEqual(self.git("worktree", "list", "--porcelain"), worktrees)
                self.assertIsNone(self.runtime.store.get_by_request("rejected-" + kind))
                self.assertFalse((self.runtime.store.root / "goal-worktrees").exists())
                self.assertEqual({path.name: path.read_bytes() for path in private.iterdir() if path.is_file()}, before)
                self.assertEqual((self.project / "work.txt").read_text(), "committed source\n")

    def test_private_mode_difference_also_refuses_without_discarding_it(self):
        goal = self.paused_goal("private-mode")
        target = long_horizon._execution_root(goal) / "work.txt"
        original = stat.S_IMODE(target.stat().st_mode)
        wanted = original & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) if os.name == "nt" else original ^ stat.S_IXUSR
        target.chmod(wanted)
        try:
            self.assertNotEqual(stat.S_IMODE(target.stat().st_mode), original)
            with self.assertRaisesRegex(HarnessError, "independent working copy differs"):
                self.runtime.fork(goal["goal_id"], "rejected-mode")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), wanted)
            self.assertFalse((self.runtime.store.root / "goal-worktrees").exists())
        finally:
            target.chmod(original)

    def test_identical_clean_paused_isolated_goal_forks_the_exact_checkpoint(self):
        source = self.paused_goal()
        private = long_horizon._execution_root(source)
        forked = self.runtime.fork(source["goal_id"], "clean-isolated-fork")
        self.assertEqual(forked["parent_goal_id"], source["goal_id"])
        self.assertEqual(forked["status"], "paused")
        self.assertEqual((Path(forked["project"]["path"]) / "work.txt").read_bytes(), (private / "work.txt").read_bytes())
        self.assertEqual(self.runtime.store.get(source["goal_id"])["execution_workspace"], source["execution_workspace"])
        self.assertEqual(self.runtime.fork(source["goal_id"], "clean-isolated-fork")["goal_id"], forked["goal_id"])
        from our_harness import agent_workspaces, goal_verification
        reopened = long_horizon.GoalStore(self.config).get(forked["goal_id"])
        selected = goal_verification.verification_project(self.config, reopened, runtime_root=self.runtime.store.root)
        _, authority_root = goal_verification.verification_authority(
            self.config, long_horizon._execution_root(reopened), selected)
        self.assertEqual(authority_root, Path(forked["project"]["path"]))
        with agent_workspaces.workspace(reopened, reopened['agents'][0], self.runtime.store.root) as draft:
            self.assertEqual((draft.root / 'work.txt').read_bytes(), (private / 'work.txt').read_bytes())

    def test_legacy_clean_fork_remains_available(self):
        source = self.paused_goal("legacy-source", isolated=False)
        forked = self.runtime.fork(source["goal_id"], "clean-legacy-fork")
        self.assertEqual(forked["parent_goal_id"], source["goal_id"])
        self.assertEqual((Path(forked["project"]["path"]) / "work.txt").read_text(), "committed source\n")

    def test_unsettled_isolated_goal_is_rejected_before_git_worktree_creation(self):
        source = self.paused_goal("live-source")
        self.runtime.store.control(source["goal_id"], "resume")
        self.assertTrue(self.runtime.store.claim_scheduler(source["goal_id"], "live-fixture"))
        try:
            with self.assertRaisesRegex(HarnessError, "Pause this isolated goal"):
                self.runtime.fork(source["goal_id"], "reject-live")
            self.assertFalse((self.runtime.store.root / "goal-worktrees").exists())
        finally:
            self.runtime.store.release_scheduler(source["goal_id"], "live-fixture")

    def test_reused_git_worktree_cannot_supply_different_bytes_to_a_fresh_checkpoint(self):
        source = self.paused_goal("reuse-source")
        request = "interrupted-fork"
        fork_id = hashlib.sha256(f"{self.runtime.store.authority_key}\0{request}".encode()).hexdigest()[:32]
        target = self.runtime.store.root / "goal-worktrees" / fork_id
        target.parent.mkdir(parents=True, exist_ok=True)
        self.git("worktree", "add", "--detach", str(target), "HEAD")
        (target / "work.txt").write_text("old interrupted worktree result\n", encoding="utf-8")
        with self.assertRaisesRegex(HarnessError, "saved fork worktree differs"):
            self.runtime.fork(source["goal_id"], request)
        self.assertIsNone(self.runtime.store.get_by_request(request))
        self.assertEqual((long_horizon._execution_root(source) / "work.txt").read_text(), "committed source\n")

    def test_paused_reconciliation_without_pending_payload_cannot_fork_away_its_effect(self):
        source = self.paused_goal("reconciliation-source")

        def pending_response(document, _db):
            document["tasks"][0].update({
                "state": "blocked", "reconciliation_required": True,
                "provider_effect_state": "reply_received_reconciliation_required",
                "pending_action": {}, "pending_transaction": {}, "outcome_unknown": False,
            })

        self.runtime.store._mutate(source["goal_id"], pending_response)
        before = self.runtime.store.get(source["goal_id"])
        worktrees = self.git("worktree", "list", "--porcelain")
        with self.assertRaisesRegex(HarnessError, "current work to settle"):
            self.runtime.fork(source["goal_id"], "reject-unsettled-reply")
        self.assertEqual(self.runtime.store.get(source["goal_id"]), before)
        self.assertEqual(self.git("worktree", "list", "--porcelain"), worktrees)
        self.assertIsNone(self.runtime.store.get_by_request("reject-unsettled-reply"))
        self.assertFalse((self.runtime.store.root / "goal-worktrees").exists())

    def test_revision_changed_during_git_inspection_does_not_clone_a_stale_checkpoint(self):
        source = self.paused_goal("revision-source")
        original_run = subprocess.run
        changed = False

        def resume_during_git(command, *args, **kwargs):
            nonlocal changed
            if "status" in command and not changed:
                changed = True
                self.runtime.store.control(source["goal_id"], "resume")
            return original_run(command, *args, **kwargs)

        with mock.patch.object(long_horizon.subprocess, "run", side_effect=resume_during_git):
            with self.assertRaisesRegex(HarnessError, "goal changed before its fork checkpoint"):
                self.runtime.fork(source["goal_id"], "changed-revision-fork")
        self.assertIsNone(self.runtime.store.get_by_request("changed-revision-fork"))
        self.assertEqual((long_horizon._execution_root(source) / "work.txt").read_text(), "committed source\n")


if __name__ == "__main__":
    unittest.main()
