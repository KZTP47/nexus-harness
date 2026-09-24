from __future__ import annotations

import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from our_harness import agent_workspaces as aw, goal_workspaces as gw, changes as file_changes
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
        write = file_changes.os.replace
        calls = []
        def locked(path, destination):
            calls.append(path)
            if len(calls) == 1:
                self.assertEqual(aw._read(home, folder), state)
                raise error
            return write(path, destination)
        with patch.object(file_changes.os, "replace", side_effect=locked), patch.object(file_changes.time, "sleep"):
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
            with patch.object(file_changes.os, "replace", side_effect=error) as write, patch.object(file_changes.time, "sleep"):
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

    def test_agent_copy_gets_the_goal_copys_dependencies_without_publishing_them(self):
        # Isolated agents run commands and native CLIs in their own copy; its
        # tests need the installed dependencies too.
        (self.team / "node_modules" / "left-pad").mkdir(parents=True)
        (self.team / "node_modules" / "left-pad" / "index.js").write_text("module.exports = 1")
        with self.open() as candidate:
            self.assertEqual((candidate.root / "node_modules" / "left-pad" / "index.js").read_text(), "module.exports = 1")
            (candidate.root / "node_modules" / "left-pad" / "index.js").write_text("the agent's own install")
            self.assertNotIn("node_modules/left-pad/index.js", aw.inventory(candidate.root))
            proposed = candidate.collect_action({"action": "complete", "changes": []}, max_files=12, max_bytes=10000)
            self.assertEqual(proposed["changes"], [])
        # Re-entering keeps the agent's own install instead of overwriting it.
        with self.open() as candidate:
            self.assertEqual((candidate.root / "node_modules" / "left-pad" / "index.js").read_text(),
                             "the agent's own install")

    def test_tool_caches_are_not_collected_or_counted_but_output_folders_are(self):
        with self.open() as candidate:
            (candidate.root / "app.txt").write_text("real agent change")
            for cache in [".pytest_cache/v/cache", ".mypy_cache/3.13", ".ruff_cache/0.1",
                          ".hypothesis/examples", ".tox/py", ".turbo/cache", "pkg/.PYTEST_CACHE/v"]:
                folder = candidate.root / cache
                folder.mkdir(parents=True)
                for index in range(10):
                    (folder / f"entry-{index}").write_text("tool litter")
            (candidate.root / ".coverage").write_bytes(b"SQLite format 3\x00")
            (candidate.root / "dist").mkdir()
            (candidate.root / "dist" / "bundle.js").write_text("deliverable")
            # .cache is ambiguous and often a real fixture folder: it is work.
            (candidate.root / "tools" / ".cache").mkdir(parents=True)
            (candidate.root / "tools" / ".cache" / "fixture.json").write_text("{}")
            proposed = candidate.collect_action({"action": "complete", "changes": []}, max_files=12, max_bytes=10000)
            self.assertEqual(sorted(one["path"] for one in proposed["changes"]),
                             ["app.txt", "dist/bundle.js", "tools/.cache/fixture.json"])

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

    def test_sync_intent_saved_before_the_cache_exclusion_still_finishes(self):
        with self.open():
            pass
        (self.team / "peer.txt").write_text("accepted", encoding="utf-8")
        with patch.object(gw, "_copy_file", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError), self.open():
                pass
        home, folder, _root = aw.layout(self.goal, self.agents[0], self.runtime)
        state = aw._read(home, folder)
        self.assertTrue(state["sync"])
        # What an older build recorded: tool-cache files in the intent.
        litter = {"sha256": "0" * 64, "size": 1, "mode": 0o644}
        state["sync"]["incoming"][".pytest_cache/v/cache/nodeids"] = litter
        state["sync"]["updates"][".pytest_cache/v/cache/nodeids"] = {"before": None, "after": litter}
        state["baseline"]["pkg/.mypy_cache/3.13/x.json"] = litter
        aw._write(home, folder, state)
        with self.open() as recovered:
            self.assertEqual((recovered.root / "peer.txt").read_text(), "accepted")
            self.assertEqual(recovered.changes(), [])
        self.assertIsNone(aw._read(home, folder)["sync"])

    def _git(self, *arguments, cwd=None):
        import subprocess
        return subprocess.run(["git", *arguments], cwd=cwd, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, check=False)

    def _commit_project(self):
        import shutil
        if not shutil.which("git"):
            self.skipTest("git is not installed")
        identity = ["-c", "user.name=Nexus Test", "-c", "user.email=nexus@example.invalid",
                    "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"]
        for arguments in (["init", "-q"], [*identity, "add", "-A"],
                          [*identity, "commit", "-q", "-m", "first commit"]):
            done = self._git(*arguments, cwd=self.project)
            self.assertEqual(done.returncode, 0, done.stderr)
        return self._git("rev-parse", "HEAD", cwd=self.project).stdout.strip()

    def _enabled(self):
        return patch.dict(os.environ, {aw.GIT_HISTORY_ENV: "1"})

    def test_git_history_is_off_unless_opted_in(self):
        self._commit_project()
        with patch.dict(os.environ, {aw.GIT_HISTORY_ENV: ""}), self.open() as candidate:
            self.assertFalse((candidate.root / ".git").exists())
            self.assertEqual(candidate.changes(), [])

    def test_working_copy_has_readable_git_history_that_is_never_published(self):
        head = self._commit_project()
        index = (self.project / ".git" / "index").read_bytes()
        config = (self.project / ".git" / "config").read_bytes()
        refs = self._git("show-ref", cwd=self.project).stdout
        with self._enabled(), self.open() as candidate:
            copy_root = candidate.root
            self.assertTrue((copy_root / ".git").is_dir())
            self.assertEqual(self._git("status", "--porcelain", cwd=copy_root).stdout, "")
            log = self._git("log", "--format=%s", cwd=copy_root).stdout
            self.assertIn("first commit", log)
            self.assertIn(aw.BASELINE_MESSAGE, log)
            # Nothing in the clone can reach the user's repository.
            self.assertEqual(self._git("remote", cwd=copy_root).stdout, "")
            self.assertFalse((copy_root / ".git" / "objects" / "info" / "alternates").exists())
            self.assertNotIn(str(self.project).casefold(),
                             (copy_root / ".git" / "config").read_text().casefold())
            (copy_root / "app.txt").write_text("agent edit", encoding="utf-8")
            self.assertIn("app.txt", self._git("diff", "--name-only", cwd=copy_root).stdout)
            identity = ["-c", "user.name=Agent", "-c", "user.email=agent@example.invalid",
                        "-c", "commit.gpgsign=false"]
            self.assertEqual(self._git(*identity, "commit", "-qam", "agent", cwd=copy_root).returncode, 0)
            self.assertNotEqual(self._git("push", "-q", "origin", "HEAD", cwd=copy_root).returncode, 0)
            proposed = candidate.collect_action({"action": "complete", "changes": []},
                                                max_files=12, max_bytes=100000)
            self.assertEqual([one["path"] for one in proposed["changes"]], ["app.txt"])
        self.assertEqual(self._git("rev-parse", "HEAD", cwd=self.project).stdout.strip(), head)
        self.assertEqual(self._git("show-ref", cwd=self.project).stdout, refs)
        self.assertEqual((self.project / ".git" / "index").read_bytes(), index)
        self.assertEqual((self.project / ".git" / "config").read_bytes(), config)
        self.assertEqual((self.project / "app.txt").read_text(), "dirty original")
        # Restart: the agent's own commit is kept and its edit still shows.
        with self._enabled(), self.open() as restarted:
            self.assertIn("agent", self._git("log", "--format=%s", cwd=restarted.root).stdout)
            self.assertIn("app.txt", self._git("diff", "--name-only", cwd=restarted.root).stdout)

    def test_git_status_compares_with_the_accepted_baseline_and_refreshes(self):
        self._commit_project()
        # Accepted team work the user never committed.
        (self.team / "peer.txt").write_text("accepted before", encoding="utf-8")
        with self._enabled(), self.open() as candidate:
            copy_root = candidate.root
            self.assertEqual(self._git("status", "--porcelain", cwd=copy_root).stdout, "")
        (self.team / "later.txt").write_text("accepted later", encoding="utf-8")
        with self._enabled(), self.open() as candidate:
            self.assertEqual((copy_root / "later.txt").read_text(), "accepted later")
            self.assertEqual(self._git("status", "--porcelain", cwd=copy_root).stdout, "")
            # A git "cleanup" back to the baseline changes nothing to publish.
            (copy_root / "draft.txt").write_text("draft", encoding="utf-8")
            self.assertEqual(self._git("clean", "-fdq", cwd=copy_root).returncode, 0)
            self.assertEqual(self._git("checkout", "--", ".", cwd=copy_root).returncode, 0)
            self.assertEqual(candidate.changes(), [])

    def test_git_clean_never_deletes_ignored_files_from_the_project(self):
        self._commit_project()
        (self.team / ".gitignore").write_text(".env\n", encoding="utf-8")
        (self.team / ".env").write_text("SECRET=1", encoding="utf-8")
        (self.team / "gone.txt").write_text("tracked", encoding="utf-8")
        with self._enabled(), self.open() as candidate:
            copy_root = candidate.root
            self.assertEqual(self._git("clean", "-fdxq", cwd=copy_root).returncode, 0)
            self.assertFalse((copy_root / ".env").exists())
            (copy_root / "gone.txt").unlink()
            deleted = [one["path"] for one in candidate.changes() if one.get("delete")]
            self.assertEqual(deleted, ["gone.txt"])

    def test_withdrawing_opt_in_removes_the_stale_baseline_and_runs_no_git(self):
        self._commit_project()
        with self._enabled(), self.open() as candidate:
            copy_root = candidate.root
            self.assertTrue((copy_root / ".git").is_dir())
        (self.team / "app.txt").write_text("teammate's accepted edit", encoding="utf-8")
        import subprocess
        real_run = subprocess.run
        calls = []

        def counted(arguments, *rest, **named):
            if arguments and str(arguments[0]).lower().endswith(("git", "git.exe")):
                calls.append(arguments)
            return real_run(arguments, *rest, **named)

        with patch.dict(os.environ, {aw.GIT_HISTORY_ENV: ""}), \
                patch.object(aw.subprocess, "run", side_effect=counted), self.open() as candidate:
            # No stale baseline is left for `git checkout -- app.txt` to revert to.
            self.assertFalse((copy_root / ".git").exists())
            self.assertEqual((copy_root / "app.txt").read_text(), "teammate's accepted edit")
            self.assertEqual(candidate.changes(), [])
        self.assertEqual(calls, [])
        # An agent's own .git is never removed.
        self.assertEqual(self._git("init", "-q", cwd=copy_root).returncode, 0)
        with patch.dict(os.environ, {aw.GIT_HISTORY_ENV: ""}), self.open():
            self.assertTrue((copy_root / ".git").is_dir())

    def test_a_pruned_copy_still_records_its_baseline(self):
        self._commit_project()
        (self.team / "teammate.txt").write_text("teammate only", encoding="utf-8")
        with self._enabled(), self.open() as candidate:
            copy_root = candidate.root
        for arguments in (["checkout", "-q", "--orphan", "scratch"],
                          ["update-ref", "-d", aw.BASELINE_BRANCH],
                          ["reflog", "expire", "--expire=now", "--all"],
                          ["gc", "-q", "--prune=now"]):
            self._git(*arguments, cwd=copy_root)
        (self.team / "later.txt").write_text("more", encoding="utf-8")
        with self._enabled(), self.open():
            listed = self._git("ls-tree", "-r", "--name-only", aw.BASELINE_BRANCH, cwd=copy_root).stdout
            self.assertIn("teammate.txt", listed)
            self.assertIn("later.txt", listed)

    def test_a_deliberately_deleted_tracked_ignored_file_is_published(self):
        (self.project / ".gitignore").write_text("build/\n.env\n", encoding="utf-8")
        (self.project / "build").mkdir()
        (self.project / "build" / "out.txt").write_text("stale", encoding="utf-8")
        head = self._commit_project()
        self.assertTrue(head)
        self.assertEqual(self._git("add", "-f", "build/out.txt", cwd=self.project).returncode, 0)
        self.assertEqual(self._git("-c", "user.name=T", "-c", "user.email=t@x", "-c",
                                   "commit.gpgsign=false", "commit", "-qm", "force-add",
                                   cwd=self.project).returncode, 0)
        for name, text in ((".gitignore", "build/\n.env\n"), (".env", "SECRET=1")):
            (self.team / name).write_text(text, encoding="utf-8")
        (self.team / "build").mkdir(exist_ok=True)
        (self.team / "build" / "out.txt").write_text("stale", encoding="utf-8")
        with self._enabled(), self.open() as candidate:
            (candidate.root / "build" / "out.txt").unlink()
            (candidate.root / ".env").unlink()
            action = candidate.collect_action({"action": "complete", "changes": []},
                                              max_files=12, max_bytes=100000)
            self.assertEqual([one["path"] for one in action["changes"] if one.get("delete")],
                             ["build/out.txt"])
            self.assertEqual(action["_nexus_held_back_deletions"]["paths"], [".env"])

    def test_an_unreadable_ignore_check_holds_back_and_does_not_raise(self):
        self._commit_project()
        (self.team / ".env").write_text("SECRET=1", encoding="utf-8")
        with self._enabled(), self.open() as candidate:
            (candidate.root / ".env").unlink()
            import subprocess
            real_run = subprocess.run

            def slow(arguments, *rest, **named):
                if "check-ignore" in arguments:
                    raise subprocess.TimeoutExpired(arguments, 60)
                return real_run(arguments, *rest, **named)

            with patch.object(aw.subprocess, "run", side_effect=slow):
                self.assertEqual(candidate.changes(), [])
            self.assertEqual(candidate.held_back_deletions, [".env"])

    def test_agent_written_filters_and_hooks_never_run_inside_nexus(self):
        self._commit_project()
        marker = self.base / "FILTER_RAN"
        script = self.base / "evil.py"
        script.write_text(
            "import pathlib, sys\n"
            f"pathlib.Path({str(marker)!r}).write_text('ran')\n"
            "sys.stdout.write(sys.stdin.read())\n", encoding="utf-8")
        with self._enabled(), self.open() as candidate:
            dot_git = candidate.root / ".git"
        with open(dot_git / "config", "a", encoding="utf-8") as stream:
            stream.write(f'[filter "x"]\n\tclean = python {script.as_posix()}\n'
                         f"[core]\n\tfsmonitor = python {script.as_posix()}\n")
        (dot_git / "info").mkdir(exist_ok=True)
        (dot_git / "info" / "attributes").write_text("* filter=x\n", encoding="utf-8")
        hook = dot_git / "hooks" / "reference-transaction"
        hook.write_text(f"#!/bin/sh\npython {script.as_posix()} </dev/null\n", encoding="utf-8")
        (self.team / "new.txt").write_text("teammate", encoding="utf-8")
        # Flag off: nothing at all runs against the copy's .git.
        with patch.dict(os.environ, {aw.GIT_HISTORY_ENV: ""}), self.open():
            pass
        self.assertFalse(marker.exists())
        # Flag on: the baseline refresh hashes in the Nexus-owned repository.
        with self._enabled(), self.open() as candidate:
            self.assertIn("new.txt", self._git("ls-tree", "-r", "--name-only", "HEAD",
                                               cwd=candidate.root).stdout)
        self.assertFalse(marker.exists())

    def test_crlf_files_match_the_users_head_under_autocrlf(self):
        self._commit_project()
        self.assertEqual(self._git("config", "core.autocrlf", "true", cwd=self.project).returncode, 0)
        (self.project / "crlf.txt").write_bytes(b"one\r\ntwo\r\n")
        self.assertEqual(self._git("add", "crlf.txt", cwd=self.project).returncode, 0)
        self.assertEqual(self._git("-c", "user.name=T", "-c", "user.email=t@x", "-c",
                                   "commit.gpgsign=false", "commit", "-qm", "crlf",
                                   cwd=self.project).returncode, 0)
        (self.team / "crlf.txt").write_bytes(b"one\r\ntwo\r\n")
        with self._enabled(), self.open() as candidate:
            # The baseline commit adds nothing but the accepted difference.
            changed = self._git("diff", "--name-only", "HEAD~1", "HEAD", cwd=candidate.root).stdout
            self.assertNotIn("crlf.txt", changed)

    def test_git_environment_cannot_redirect_writes_to_the_real_repository(self):
        self._commit_project()
        (self.project / "staged.txt").write_text("user staged", encoding="utf-8")
        self.assertEqual(self._git("add", "staged.txt", cwd=self.project).returncode, 0)
        index = (self.project / ".git" / "index").read_bytes()
        config = (self.project / ".git" / "config").read_bytes()
        hostile = {"GIT_DIR": str(self.project / ".git"),
                   "GIT_INDEX_FILE": str(self.project / ".git" / "index"),
                   "GIT_WORK_TREE": str(self.project)}
        with self._enabled(), patch.dict(os.environ, hostile), self.open() as candidate:
            self.assertTrue((candidate.root / ".git").is_dir())
        self.assertEqual((self.project / ".git" / "index").read_bytes(), index)
        self.assertEqual((self.project / ".git" / "config").read_bytes(), config)
        self.assertIn("staged.txt", self._git("diff", "--cached", "--name-only", cwd=self.project).stdout)

    def test_a_clone_that_can_reach_the_project_is_dropped(self):
        self._commit_project()
        with self._enabled(), patch.object(aw, "_unreachable_from_the_project", return_value=False):
            with self.open() as candidate:
                self.assertFalse((candidate.root / ".git").exists())
                self.assertEqual(candidate.changes(), [])

    def test_project_inside_a_larger_repository_gets_no_misaligned_history(self):
        import shutil
        if not shutil.which("git"):
            self.skipTest("git is not installed")
        self.assertEqual(self._git("init", "-q", cwd=self.base).returncode, 0)
        with self._enabled(), self.open() as candidate:
            self.assertFalse((candidate.root / ".git").exists())
            self.assertEqual(candidate.changes(), [])

    def test_missing_git_never_fails_the_workspace(self):
        self._commit_project()
        with self._enabled(), patch.object(aw.shutil, "which", return_value=None):
            with self.open() as candidate:
                self.assertFalse((candidate.root / ".git").exists())
                self.assertEqual(candidate.changes(), [])


if __name__ == "__main__":
    unittest.main()
