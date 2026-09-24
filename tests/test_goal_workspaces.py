from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from our_harness import goal_workspaces as workspaces
from our_harness.changes import FileTransaction
from our_harness.models import HarnessError


class GoalWorkspaces(unittest.TestCase):
    def test_runtime_nested_sources_require_the_owned_fork_contract_and_exact_location(self):
        for relative, contract in [("goal-worktrees/" + "a" * 32, None),
                                   ("goal-worktrees/" + "a" * 32, "obsolete"),
                                   ("arbitrary-project", workspaces.FORK_SOURCE_CONTRACT),
                                   ("goal-worktrees/not-an-owned-id", workspaces.FORK_SOURCE_CONTRACT)]:
            source = self.runtime / relative
            source.mkdir(parents=True, exist_ok=True)
            goal = {"goal_id": "test-nested", "project": {"path": str(source)},
                    "parent_goal_id": "parent", "fork_workspace_contract": contract}
            with self.subTest(relative=relative, contract=contract), self.assertRaisesRegex(HarnessError, "outside"):
                workspaces.create(goal, self.runtime)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.source = self.folder / "arbitrary-selected-project"
        self.runtime = self.folder / "independent-runtime"
        self.source.mkdir()
        (self.source / "app.txt").write_text("original dirty bytes", encoding="utf-8")
        (self.source / "support.txt").write_text("support baseline", encoding="utf-8")

    def goal(self, name: str = "goal-one", source: Path | None = None) -> dict:
        document = {"goal_id": name, "project": {"id": "portable-project", "path": str(source or self.source)},
                    "project_authority_id": "selected-authority"}
        document["execution_workspace"] = workspaces.create(document, self.runtime)
        return document

    def publish(self, document: dict) -> dict:
        with workspaces.publication(document, self.runtime):
            receipt = workspaces.prepare_publish(document, self.runtime)
            return workspaces.publish(document, self.runtime, receipt)

    def test_non_git_dirty_sources_and_two_goals_are_independent(self) -> None:
        for control in [".git", ".harness", ".nexus-verification"]:
            (self.source / control).mkdir()
            (self.source / control / "private.txt").write_text("private control bytes")
        first, second = self.goal(), self.goal("goal-two")
        one, two = workspaces.root(first, self.runtime), workspaces.root(second, self.runtime)
        self.assertNotEqual(one, two)
        self.assertNotIn(self.source, one.parents)
        self.assertEqual((one / "app.txt").read_text(), "original dirty bytes")
        self.assertFalse(any((one / control).exists() for control in [".git", ".harness", ".nexus-verification"]))
        (one / "app.txt").write_text("goal one")
        self.assertEqual((two / "app.txt").read_text(), "original dirty bytes")
        self.assertEqual((self.source / "app.txt").read_text(), "original dirty bytes")

    def test_disjoint_changes_rebase_then_publish_without_erasing_prior_goal(self) -> None:
        first, second = self.goal(), self.goal("goal-two")
        (workspaces.root(first, self.runtime) / "app.txt").write_text("first result")
        (workspaces.root(second, self.runtime) / "second.txt").write_text("second result")
        self.publish(first)
        with workspaces.publication(second, self.runtime):
            receipt = workspaces.prepare_publish(second, self.runtime)
            self.assertTrue(receipt["rebased"])
            self.assertEqual(receipt["changed"], ["second.txt"])
            self.assertEqual((workspaces.root(second, self.runtime) / "app.txt").read_text(), "first result")
            workspaces.publish(second, self.runtime, receipt)
        self.assertEqual((self.source / "app.txt").read_text(), "first result")
        self.assertEqual((self.source / "second.txt").read_text(), "second result")

    def test_same_file_conflict_preserves_both_candidates_and_user_reconciliation(self) -> None:
        first, second = self.goal(), self.goal("goal-two")
        (workspaces.root(first, self.runtime) / "app.txt").write_text("first result")
        (workspaces.root(second, self.runtime) / "app.txt").write_text("second result")
        self.publish(first)
        with workspaces.publication(second, self.runtime):
            with self.assertRaises(workspaces.WorkspaceConflict) as error:
                workspaces.prepare_publish(second, self.runtime)
            self.assertEqual(error.exception.conflicts, ["app.txt"])
        self.assertEqual((self.source / "app.txt").read_text(), "first result")
        self.assertEqual((workspaces.root(second, self.runtime) / "app.txt").read_text(), "second result")
        (self.source / "app.txt").write_text("second result")
        with workspaces.publication(second, self.runtime):
            receipt = workspaces.prepare_publish(second, self.runtime)
            self.assertEqual(receipt["changed"], [])
            self.assertEqual(receipt["reconciled"], ["app.txt"])
            self.assertEqual(workspaces.publish(second, self.runtime, receipt)["changes"], [])

    def test_external_source_or_workspace_edits_after_verification_block_publication(self) -> None:
        for edited in ["source", "workspace"]:
            with self.subTest(edited=edited):
                document = self.goal("edit-" + edited)
                candidate = workspaces.root(document, self.runtime)
                (candidate / "app.txt").write_text("proposed")
                with workspaces.publication(document, self.runtime):
                    receipt = workspaces.prepare_publish(document, self.runtime)
                    target = self.source if edited == "source" else candidate
                    (target / "support.txt").write_text("external edit " + edited)
                    with self.assertRaises(workspaces.WorkspaceConflict):
                        workspaces.publish(document, self.runtime, receipt)
                self.assertEqual((self.source / "app.txt").read_text(), "original dirty bytes")

    def test_create_delete_and_binary_changes_publish_only_goal_delta(self) -> None:
        document = self.goal()
        candidate = workspaces.root(document, self.runtime)
        (candidate / "app.txt").unlink()
        (candidate / "nested").mkdir()
        (candidate / "nested" / "new.bin").write_bytes(bytes(range(256)))
        self.publish(document)
        self.assertFalse((self.source / "app.txt").exists())
        self.assertEqual((self.source / "nested" / "new.bin").read_bytes(), bytes(range(256)))
        self.assertEqual((self.source / "support.txt").read_text(), "support baseline")

    def test_restart_keeps_candidate_and_idempotent_publication_receipt(self) -> None:
        document = self.goal()
        (workspaces.root(document, self.runtime) / "app.txt").write_text("restart result")
        restored = json.loads(json.dumps(document))
        workspaces.validate(restored, self.runtime)
        self.assertEqual(workspaces.create(restored, self.runtime), document["execution_workspace"])
        first = self.publish(restored)
        second = self.publish(json.loads(json.dumps(restored)))
        self.assertEqual(first["transaction_id"], second["transaction_id"])
        self.assertEqual(len(list((self.source / ".harness" / "backups").iterdir())), 1)

    def test_recovery_rejects_changed_support_before_completion_acknowledgement(self) -> None:
        for journal in ["published", "prepared"]:
            for changed_tree in ["source", "workspace"]:
                for entry in ["prepare", "publish"]:
                    with self.subTest(journal=journal, changed_tree=changed_tree, entry=entry):
                        (self.source / "app.txt").write_text("old app")
                        (self.source / "support.txt").write_text("compatible support")
                        document = self.goal(f"recover-{journal}-{changed_tree}-{entry}")
                        candidate = workspaces.root(document, self.runtime)
                        (candidate / "app.txt").write_text("new app using compatible support")
                        original = workspaces._write_state
                        def lose_acknowledgement(home, folder, state):
                            if state.get("publication", {}).get("state") == "published":
                                raise OSError("lost acknowledgement")
                            return original(home, folder, state)
                        with workspaces.publication(document, self.runtime):
                            receipt = workspaces.prepare_publish(document, self.runtime)
                            if journal == "prepared":
                                with patch.object(workspaces, "_write_state", side_effect=lose_acknowledgement):
                                    with self.assertRaisesRegex(OSError, "lost acknowledgement"):
                                        workspaces.publish(document, self.runtime, receipt)
                            else:
                                workspaces.publish(document, self.runtime, receipt)
                        target = self.source if changed_tree == "source" else candidate
                        (target / "support.txt").write_text("incompatible support after publication")
                        with workspaces.publication(document, self.runtime):
                            with self.assertRaises(workspaces.WorkspaceConflict) as error:
                                if entry == "prepare":
                                    workspaces.prepare_publish(document, self.runtime)
                                else:
                                    workspaces.publish(document, self.runtime, receipt)
                            self.assertEqual(error.exception.conflicts, ["support.txt"])
                        self.assertEqual((target / "support.txt").read_text(), "incompatible support after publication")
                        self.assertEqual((self.source / "app.txt").read_text(), "new app using compatible support")

    def test_root_and_lightweight_validation_do_not_hash_project_files(self) -> None:
        document = self.goal()
        with patch.object(workspaces, "_manifest", side_effect=AssertionError("unexpected full scan")):
            workspaces.root(document, self.runtime)
            workspaces.validate(document, self.runtime)

    def test_project_edit_during_copy_creation_restarts_from_a_fresh_listing_on_retry(self) -> None:
        for interruption in ["changed", "crashed"]:
            with self.subTest(interruption=interruption):
                source = self.folder / ("edited-during-copy-" + interruption)
                source.mkdir()
                (source / "app.txt").write_text("before")
                (source / "removed.txt").write_text("deleted by the user mid-copy")
                (source / "locked.txt").write_text("read only bytes")
                os.chmod(source / "locked.txt", 0o444)
                self.addCleanup(os.chmod, source / "locked.txt", 0o666)
                document = {"goal_id": "copy-" + interruption, "project": {"path": str(source)},
                            "project_authority_id": "selected-authority"}
                original = workspaces._copy_file
                def edit_while_copying(*args):
                    original(*args)
                    if not (source / "added.txt").exists():
                        (source / "app.txt").write_text("edited while copying")
                        (source / "removed.txt").unlink()
                        (source / "added.txt").write_text("added while copying")
                        if interruption == "crashed":
                            raise OSError("process stopped mid-copy")
                with patch.object(workspaces, "_copy_file", side_effect=edit_while_copying):
                    with self.assertRaises((HarnessError, OSError)):
                        workspaces.create(document, self.runtime)
                # Every retry used to refuse against the stale listing forever.
                document["execution_workspace"] = workspaces.create(document, self.runtime)
                copy = workspaces.root(document, self.runtime)
                self.assertEqual(sorted(path.name for path in copy.iterdir()), ["added.txt", "app.txt", "locked.txt"])
                self.assertEqual((copy / "app.txt").read_text(), "edited while copying")
                self.assertEqual(workspaces.differing_files(document, self.runtime), [])
                workspaces.validate(document, self.runtime, full=True)
                self.assertEqual(workspaces.create(document, self.runtime), document["execution_workspace"])

    def test_ready_workspace_is_never_discarded_when_the_project_later_changes(self) -> None:
        document = self.goal()
        (workspaces.root(document, self.runtime) / "app.txt").write_text("agent work")
        (self.source / "support.txt").write_text("user edited after the copy was ready")
        self.assertEqual(workspaces.create(document, self.runtime), document["execution_workspace"])
        self.assertEqual((workspaces.root(document, self.runtime) / "app.txt").read_text(), "agent work")

    def test_interrupted_rebase_retains_the_original_goal_delta_on_retry(self) -> None:
        document = self.goal()
        candidate = workspaces.root(document, self.runtime)
        (candidate / "goal.txt").write_text("private goal delta")
        (self.source / "app.txt").write_text("another goal app")
        (self.source / "support.txt").write_text("another goal support")
        original = workspaces._copy_file
        copies = []
        def copy_then_crash(*args):
            original(*args)
            copies.append(args[2])
            raise OSError("interrupted private rebase")
        with workspaces.publication(document, self.runtime):
            with patch.object(workspaces, "_copy_file", side_effect=copy_then_crash):
                with self.assertRaisesRegex(OSError, "interrupted private rebase"):
                    workspaces.prepare_publish(document, self.runtime)
        self.assertEqual(len(copies), 1)
        (self.source / "later.txt").write_text("even newer unrelated edit")
        with workspaces.publication(document, self.runtime):
            receipt = workspaces.prepare_publish(document, self.runtime)
            self.assertEqual(receipt["changed"], ["goal.txt"])
            workspaces.publish(document, self.runtime, receipt)
        self.assertEqual((self.source / "app.txt").read_text(), "another goal app")
        self.assertEqual((self.source / "support.txt").read_text(), "another goal support")
        self.assertEqual((self.source / "later.txt").read_text(), "even newer unrelated edit")
        self.assertEqual((self.source / "goal.txt").read_text(), "private goal delta")

    def test_fully_written_transaction_recovers_from_prepared_manifest(self) -> None:
        from our_harness import changes
        document = self.goal()
        (workspaces.root(document, self.runtime) / "app.txt").write_text("fully written before crash")
        original = changes.atomic_write
        def lose_process(path, content, *args, **kwargs):
            if path.name == "manifest.json" and json.loads(content).get("state") == "applied":
                raise SystemExit("simulated abrupt process loss")
            return original(path, content, *args, **kwargs)
        with workspaces.publication(document, self.runtime):
            receipt = workspaces.prepare_publish(document, self.runtime)
            with patch.object(changes, "atomic_write", side_effect=lose_process):
                with self.assertRaises(SystemExit):
                    workspaces.publish(document, self.runtime, receipt)
        manifest = self.publish(document)
        self.assertEqual(manifest["state"], "applied")
        self.assertEqual(len(list((self.source / ".harness" / "backups").iterdir())), 1)

    def test_crash_after_transaction_apply_recovers_without_duplicate_write(self) -> None:
        document = self.goal()
        (workspaces.root(document, self.runtime) / "app.txt").write_text("accepted before crash")
        original = workspaces._write_state
        def fail_completion(home, folder, state):
            if state.get("publication", {}).get("state") == "published":
                raise OSError("simulated process loss after FileTransaction applied")
            return original(home, folder, state)
        with workspaces.publication(document, self.runtime):
            receipt = workspaces.prepare_publish(document, self.runtime)
            with patch.object(workspaces, "_write_state", side_effect=fail_completion):
                with self.assertRaisesRegex(OSError, "process loss"):
                    workspaces.publish(document, self.runtime, receipt)
        self.assertEqual((self.source / "app.txt").read_text(), "accepted before crash")
        manifest = self.publish(document)
        self.assertEqual(manifest["state"], "applied")
        self.assertEqual(len(list((self.source / ".harness" / "backups").iterdir())), 1)

    def test_cancelled_goal_keeps_private_work_without_source_effects(self) -> None:
        document = self.goal()
        (workspaces.root(document, self.runtime) / "app.txt").write_text("cancelled private work")
        document["status"] = "cancelled"
        workspaces.validate(document, self.runtime)
        self.assertEqual((self.source / "app.txt").read_text(), "original dirty bytes")

    def test_descriptor_state_and_receipt_tampering_fail_closed(self) -> None:
        document = self.goal()
        bad = copy.deepcopy(document)
        bad["execution_workspace"]["path"] = "../different"
        with self.assertRaises(HarnessError):
            workspaces.root(bad, self.runtime)
        with workspaces.publication(document, self.runtime):
            receipt = workspaces.prepare_publish(document, self.runtime)
            receipt["changed"] = ["../outside"]
            with self.assertRaisesRegex(HarnessError, "authentication"):
                workspaces.publish(document, self.runtime, receipt)
        state_path = self.runtime / "goal-workspaces" / document["goal_id"] / "state.json"
        state = json.loads(state_path.read_text())
        state["baseline"] = {}
        state_path.write_text(json.dumps(state))
        with self.assertRaisesRegex(HarnessError, "authentication"):
            workspaces.validate(document, self.runtime)

    def test_candidate_bytes_changed_between_manifest_and_plan_cannot_be_published(self) -> None:
        document = self.goal()
        candidate = workspaces.root(document, self.runtime)
        (candidate / "app.txt").write_text("verified candidate")
        original = workspaces._manifest
        def change_after_scan(project, **kwargs):
            manifest = original(project, **kwargs)
            if project == candidate:
                (candidate / "app.txt").write_text("unverified late bytes")
            return manifest
        with workspaces.publication(document, self.runtime):
            receipt = workspaces.prepare_publish(document, self.runtime)
            with patch.object(workspaces, "_manifest", side_effect=change_after_scan):
                with self.assertRaises(workspaces.WorkspaceConflict):
                    workspaces.publish(document, self.runtime, receipt)
        self.assertEqual((self.source / "app.txt").read_text(), "original dirty bytes")

    def test_directory_replacement_and_junction_escape_are_rejected(self) -> None:
        document = self.goal()
        candidate = workspaces.root(document, self.runtime)
        candidate.rename(candidate.with_name("original-project"))
        candidate.mkdir()
        with self.assertRaisesRegex(HarnessError, "directory identity"):
            workspaces.root(document, self.runtime)
        outside = self.folder / "outside-directory"
        outside.mkdir()
        (outside / "private.txt").write_text("outside data")
        linked = self.source / "linked-directory"
        if os.name == "nt":
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(linked), str(outside)],
                                    capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            linked.symlink_to(outside, target_is_directory=True)
        # A link is skipped with a note rather than failing the goal, and it
        # is never followed: nothing outside the project is copied or touched.
        linked_goal = self.goal("linked-source")
        copy_root = workspaces.root(linked_goal, self.runtime)
        self.assertFalse((copy_root / "linked-directory").exists())
        self.assertFalse(any(path.name == "private.txt" for path in copy_root.rglob("*")))
        self.assertEqual(linked_goal["execution_workspace"]["skipped_links"]["paths"], ["linked-directory"])
        (copy_root / "app.txt").write_text("changed beside a link")
        self.publish(linked_goal)
        self.assertEqual((self.source / "app.txt").read_text(), "changed beside a link")
        self.assertEqual((outside / "private.txt").read_text(), "outside data")
        self.assertEqual(sorted(path.name for path in outside.iterdir()), ["private.txt"])

    def test_legacy_root_and_nested_publication_creation(self) -> None:
        document = {"goal_id": "legacy-goal", "project": {"path": str(self.source)}}
        self.assertTrue(workspaces.root(document, self.runtime).samefile(self.source))
        with workspaces.publication(document, self.runtime):
            descriptor = workspaces.create(document, self.runtime, publication_locked=True)
        document["execution_workspace"] = descriptor
        workspaces.validate(document, self.runtime)
        with self.assertRaisesRegex(HarnessError, "context"):
            workspaces.create(document, self.runtime, publication_locked=True)

    def test_inside_project_runtime_is_rejected_and_hard_links_are_tolerated(self) -> None:
        # uv and pnpm install by hard link. A hard-linked file is an ordinary
        # file to Nexus: it validates and publishes. Only writing *through* a
        # hard-linked project file stays refused, because the other names of
        # that file can live outside the project.
        store = self.folder / "package-store.txt"
        store.write_text("store bytes")
        try:
            os.link(store, self.source / "source-linked.txt")
        except OSError:
            self.skipTest("Hard links unavailable on this filesystem")
        document = self.goal()
        with self.assertRaises(HarnessError):
            workspaces.create({"goal_id": "escape", "project": {"path": str(self.source)}}, self.source / "runtime")
        os.link(store, workspaces.root(document, self.runtime) / "linked.txt")
        workspaces.validate(document, self.runtime, full=True)
        self.publish(document)
        self.assertEqual((self.source / "linked.txt").read_text(), "store bytes")
        self.assertEqual(store.read_text(), "store bytes")
        document = self.goal("writes-through-a-link")
        (workspaces.root(document, self.runtime) / "source-linked.txt").write_text("goal bytes")
        with self.assertRaisesRegex(workspaces.WorkspaceConflict, "hard-linked project file"):
            self.publish(document)
        self.assertEqual(store.read_text(), "store bytes")
        self.assertEqual((self.source / "source-linked.txt").read_text(), "store bytes")
        self.assertEqual((workspaces.root(document, self.runtime) / "source-linked.txt").read_text(), "goal bytes")

    def test_dependencies_are_provided_but_never_compared_or_published_and_caches_are_absent(self) -> None:
        for relative in ["node_modules/pkg/index.js", ".venv/Lib/site.py", "src/__pycache__/app.cpython-313.pyc",
                         "custom-env/Lib/tool.py", ".pytest_cache/v/cache/nodeids"]:
            (self.source / relative).parent.mkdir(parents=True, exist_ok=True)
            (self.source / relative).write_text("generated " + relative)
        (self.source / "custom-env" / "pyvenv.cfg").write_text("home = anywhere")
        (self.source / "stray.pyc").write_bytes(b"\x00bytecode")
        (self.source / "dist").mkdir()
        (self.source / "dist" / "bundle.js").write_text("deliverable")
        (self.source / "tools" / ".cache").mkdir(parents=True)
        (self.source / "tools" / ".cache" / "fixture.json").write_text("{}")
        document = self.goal()
        copy_root = workspaces.root(document, self.runtime)
        # Checks in the copy need installed dependencies: they are provided.
        for provided in ["node_modules/pkg/index.js", ".venv/Lib/site.py", "custom-env/Lib/tool.py"]:
            self.assertEqual((copy_root / provided).read_text(), "generated " + provided, provided)
        # Regenerable caches and bytecode are absent.
        for skipped in [".pytest_cache", "src/__pycache__", "stray.pyc"]:
            self.assertFalse((copy_root / skipped).exists(), skipped)
        self.assertEqual((copy_root / "dist" / "bundle.js").read_text(), "deliverable")
        self.assertEqual((copy_root / "tools" / ".cache" / "fixture.json").read_text(), "{}")
        (copy_root / "tools" / ".cache" / "fixture.json").write_text('{"updated": true}')
        # The agent's own installs and bytecode in the copy never become a
        # publication delta or a false conflict; real outputs still publish.
        (copy_root / "node_modules" / "new").mkdir(parents=True)
        (copy_root / "node_modules" / "new" / "index.js").write_text("installed by the agent")
        (copy_root / "app.pyc").write_bytes(b"\x01different bytecode")
        (self.source / "stray.pyc").write_bytes(b"\x02the user's bytecode changed")
        (copy_root / "dist" / "bundle.js").write_text("rebuilt deliverable")
        self.assertEqual(workspaces.differing_files(document, self.runtime), ["dist/bundle.js", "tools/.cache/fixture.json"])
        manifest = self.publish(document)
        self.assertEqual(sorted(one["path"] for one in manifest["changes"]), ["dist/bundle.js", "tools/.cache/fixture.json"])
        self.assertEqual((self.source / "tools" / ".cache" / "fixture.json").read_text(), '{"updated": true}')
        self.assertFalse((self.source / "node_modules" / "new").exists())
        self.assertEqual((self.source / "dist" / "bundle.js").read_text(), "rebuilt deliverable")
        self.assertEqual((self.source / "node_modules" / "pkg" / "index.js").read_text(), "generated node_modules/pkg/index.js")

    def test_new_cache_folders_in_the_copy_are_not_published_but_reported(self) -> None:
        # A .cache folder that already existed in the project is content; one
        # that only appears in the copy is tool output: kept there, reported,
        # never published into the user's project.
        (self.source / "tools" / ".cache").mkdir(parents=True)
        (self.source / "tools" / ".cache" / "fixture.json").write_text("{}")
        document = self.goal()
        copy_root = workspaces.root(document, self.runtime)
        (copy_root / "tools" / ".cache" / "second.json").write_text("new fixture in an existing folder")
        for litter in (".cache/tool/state.bin", "pkg/.cache/entry"):
            (copy_root / litter).parent.mkdir(parents=True, exist_ok=True)
            (copy_root / litter).write_text("tool litter")
        (copy_root / "app.txt").write_text("real change")
        self.assertEqual(workspaces.differing_files(document, self.runtime), ["app.txt", "tools/.cache/second.json"])
        with workspaces.publication(document, self.runtime):
            receipt = workspaces.prepare_publish(document, self.runtime)
            self.assertEqual(receipt["held_back_cache_count"], 2)
            self.assertEqual(receipt["held_back_cache_files"], [".cache/tool/state.bin", "pkg/.cache/entry"])
            manifest = workspaces.publish(document, self.runtime, receipt)
        self.assertEqual(sorted(one["path"] for one in manifest["changes"]), ["app.txt", "tools/.cache/second.json"])
        self.assertEqual(manifest["held_back_cache_count"], 2)
        self.assertFalse((self.source / ".cache").exists())
        self.assertFalse((self.source / "pkg" / ".cache").exists())
        self.assertEqual((self.source / "tools" / ".cache" / "second.json").read_text(), "new fixture in an existing folder")
        self.assertTrue((copy_root / ".cache" / "tool" / "state.bin").exists())  # kept in the copy

    def test_cache_folder_tracked_in_git_head_is_content_even_if_missing_locally(self) -> None:
        try:
            subprocess.run(["git", "init", "-q"], cwd=self.source, check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("git is unavailable")
        (self.source / "assets" / ".cache").mkdir(parents=True)
        (self.source / "assets" / ".cache" / "tracked.json").write_text('{"v": 1}')
        environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        environment.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                           GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
        subprocess.run(["git", "add", "-A"], cwd=self.source, check=True, capture_output=True, env=environment)
        subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=self.source, check=True, capture_output=True, env=environment)
        # Deleted from the working tree, still tracked in HEAD.
        (self.source / "assets" / ".cache" / "tracked.json").unlink()
        (self.source / "assets" / ".cache").rmdir()
        document = self.goal("tracked-cache")
        copy_root = workspaces.root(document, self.runtime)
        (copy_root / "assets" / ".cache").mkdir(parents=True)
        (copy_root / "assets" / ".cache" / "tracked.json").write_text('{"v": 2}')
        manifest = self.publish(document)
        self.assertEqual([one["path"] for one in manifest["changes"]], ["assets/.cache/tracked.json"])
        self.assertEqual((self.source / "assets" / ".cache" / "tracked.json").read_text(), '{"v": 2}')
        self.assertNotIn("held_back_cache_count", manifest)

    def test_publication_lock_serializes_nested_selected_roots_across_processes(self) -> None:
        document = self.goal()
        child = self.source / "subproject"
        child.mkdir()
        other = self.goal("nested-goal", child)
        marker = self.folder / "acquired.txt"
        ready = self.folder / "ready.txt"
        script = "\n".join([
            "import json,sys", "from pathlib import Path", "from our_harness import goal_workspaces as w",
            "doc=json.loads(sys.argv[1])", "Path(sys.argv[4]).write_text('ready')",
            "with w.publication(doc,Path(sys.argv[2])):",
            " Path(sys.argv[3]).write_text('acquired')",
        ])
        environment = dict(os.environ)
        source_path = str(Path(__file__).resolve().parents[1] / "src")
        environment["PYTHONPATH"] = source_path + (os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else "")
        with workspaces.publication(document, self.runtime):
            process = subprocess.Popen([sys.executable, "-c", script, json.dumps(other), str(self.runtime), str(marker), str(ready)],
                                       env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 10
                while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                if process.poll() is not None:
                    self.fail(f"Publication child exited before the lock check: {process.communicate()!r}")
                self.assertTrue(ready.exists(), "Publication child did not finish importing the exact source")
                with self.assertRaises(subprocess.TimeoutExpired):
                    process.wait(timeout=0.25)
                self.assertFalse(marker.exists())
            except BaseException:
                process.kill()
                process.communicate()
                raise
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, (stdout, stderr))
        self.assertEqual(marker.read_text(), "acquired")

    def test_nonblocking_publication_returns_promptly_for_either_contended_lock(self) -> None:
        document = self.goal()
        for blocked in ["publication", "source"]:
            with self.subTest(blocked=blocked):
                acquired, release = threading.Event(), threading.Event()
                failures = []
                def hold():
                    try:
                        context = (workspaces.publication(document, self.runtime) if blocked == "publication"
                                   else FileTransaction(self.source).locked())
                        with context:
                            acquired.set()
                            if not release.wait(5):
                                raise AssertionError("Test lock release was never signalled")
                    except BaseException as error:
                        failures.append(error)
                worker = threading.Thread(target=hold)
                worker.start()
                try:
                    self.assertTrue(acquired.wait(3), failures)
                    began = time.monotonic()
                    with self.assertRaisesRegex(HarnessError, "Another harness process holds the project transaction lock|Another write operation is (still )?using this project"):
                        with workspaces.publication(document, self.runtime, timeout_seconds=0):
                            self.fail("A contended publication lock was acquired")
                    self.assertLess(time.monotonic() - began, 0.5)
                finally:
                    release.set()
                    worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(failures, [])
                # Failure at the second lock must also release the first one.
                with workspaces.publication(document, self.runtime, timeout_seconds=0):
                    pass


if __name__ == "__main__":
    unittest.main()
