"""Explicit user restrictions in the work-together engine are always honoured.

Under "Agents lead; Nexus only supports" (AGENTS.md) the one thing Nexus must
never drop is what the user explicitly protected or restricted. These tests
cover the spellings a review found were silently lost (F1-F9).
"""

from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from our_harness import chat, goal_verification, swarm_work
from our_harness.config import DEFAULT_CONFIG, LoadedConfig


class UserRestrictionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        (self.base / ".harness").mkdir()
        self.root = self.base / "proj"
        for relative in (
            "config/secret.txt", "config/other.txt", "src/app.py", "src/util.py",
            "tests/test_app.py", "README.md", "docs/guide.md", "Config.ini", "LICENSE",
        ):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x\n", encoding="utf-8")

    def policy(self, goal: str) -> dict:
        spec = swarm_work._compile_goal_spec(self.root, goal)
        contract = swarm_work._derive_requirement_contract(self.root, goal)
        policy = dict(spec["write_policy"])
        policy["contract_protected"] = contract["protected_paths"]
        return policy

    def writable(self, goal: str, relative: str) -> bool:
        policy = self.policy(goal)
        if policy["mode"] == "DENY_ALL":
            return False
        scope = policy["only_scope"] if policy["mode"] == "SCOPED" else None
        changes, refusals = swarm_work._partition_changes(
            self.root, [{"path": relative, "content": "pwn\n"}], scope,
            policy["contract_protected"], None,
        )
        return bool(changes) and not refusals

    # -- F1 -------------------------------------------------------------------

    def test_absolute_in_project_protections_are_honoured(self) -> None:
        root = str(self.root)
        for goal in (
            f"Refactor but do not touch {root}\\config\\secret.txt",
            f"Refactor but do not touch {root}\\config",
            f'Refactor but do not touch "{root}\\config\\secret.txt"',
            f"Refactor. {root}\\config\\secret.txt is read-only",
            f"Refactor. Leave {root}\\config alone",
            f"Refactor but do not touch {root}/config/secret.txt",
        ):
            with self.subTest(goal=goal):
                policy = self.policy(goal)
                self.assertEqual("OPEN", policy["mode"])
                self.assertFalse(self.writable(goal, "config/secret.txt"))
                self.assertTrue(self.writable(goal, "src/app.py"))

    def test_standalone_read_only_heading_paths_are_protected(self) -> None:
        goal = (
            "Work on the project.\nThe following folders are read-only:\n"
            f"{self.root / 'config'}\n{self.root / 'docs'}\n\nFix src/app.py."
        )
        self.assertFalse(self.writable(goal, "config/secret.txt"))
        self.assertFalse(self.writable(goal, "docs/guide.md"))
        self.assertTrue(self.writable(goal, "src/app.py"))
        # An inline absolute path next to unrelated "do not change" wording is
        # not a standalone heading item and is not protected by it.
        inline = f"Fix the bug in {self.root / 'src' / 'app.py'}. Do not change the API."
        self.assertTrue(self.writable(inline, "src/app.py"))

    # -- F2 -------------------------------------------------------------------

    def test_list_forms_dashes_and_colons_protect(self) -> None:
        cases = (
            ("Refactor the code.\nDo not modify:\n- config/secret.txt\n- LICENSE\n", ["config/secret.txt", "LICENSE"]),
            ("Refactor.\nDon't touch: config/secret.txt, README.md", ["config/secret.txt", "README.md"]),
            ("Refactor src/app.py.\nProtected files: config/secret.txt, LICENSE", ["config/secret.txt", "LICENSE"]),
            ("Refactor src/app.py.\nRead-only files:\n- config/secret.txt", ["config/secret.txt"]),
            ("Refactor. Read-only: config/secret.txt", ["config/secret.txt"]),
            ("Refactor the code — don't touch config/secret.txt", ["config/secret.txt"]),
            ("Refactor the code—do not touch config/secret.txt", ["config/secret.txt"]),
            ("Refactor the code – never edit config/secret.txt", ["config/secret.txt"]),
            ("Refactor the code; DON’T touch config/secret.txt", ["config/secret.txt"]),
        )
        for goal, protected in cases:
            with self.subTest(goal=goal):
                policy = self.policy(goal)
                self.assertNotEqual("DENY_ALL", policy["mode"])
                for relative in protected:
                    self.assertFalse(self.writable(goal, relative), relative)
                self.assertTrue(self.writable(goal, "src/util.py"))

    def test_a_labelled_file_is_never_a_grant(self) -> None:
        goal = "Fix src/app.py (config/secret.txt: read-only)"
        policy = self.policy(goal)
        self.assertNotIn("config/secret.txt", policy["grants"])
        self.assertIn("config/secret.txt", policy["protected"])
        self.assertFalse(self.writable(goal, "config/secret.txt"))

    # -- F3 -------------------------------------------------------------------

    def test_per_file_labels_protect_that_file_not_the_whole_run(self) -> None:
        for goal, protected in (
            ("Fix the crash in src/app.py.\nconfig/secret.txt: do not modify", "config/secret.txt"),
            ("Fix the crash in src/app.py; tests/test_app.py: read-only", "tests/test_app.py"),
            ("Fix src/app.py. For config/secret.txt, don't change.", "config/secret.txt"),
            ("Fix src/app.py but config/secret.txt, don't change it.", "config/secret.txt"),
            ("Fix src/app.py.\nFiles:\n- src/app.py: fix\n- config/secret.txt: do not modify\n- LICENSE: read-only", "LICENSE"),
        ):
            with self.subTest(goal=goal):
                policy = self.policy(goal)
                self.assertEqual("OPEN", policy["mode"], policy)
                self.assertFalse(self.writable(goal, protected))
                self.assertTrue(self.writable(goal, "src/app.py"))
        # Global read-only wording still makes the whole run read-only.
        for goal in ("Explain the parser. Don't change anything.", "Read-only: explain the parser",
                     "Just explain, don't change."):
            with self.subTest(read_only=goal):
                self.assertEqual("DENY_ALL", self.policy(goal)["mode"])

    # -- F4 -------------------------------------------------------------------

    def test_glob_and_invalid_names_in_protections_never_crash_and_globs_protect(self) -> None:
        for goal in (
            "Refactor the code but do not touch config/*.txt",
            "Refactor but do not touch **/*.txt",
            "Refactor but do not touch *.md",
            "Refactor but never touch config/a?b.txt",
            "Refactor but never touch config/<x>.txt",
            "Refactor but never touch config/a|b.txt",
            "Update src/<name>.py",
            'Update src/"app".py',
        ):
            with self.subTest(goal=goal):
                swarm_work._compile_goal_spec(self.root, goal)
                swarm_work._derive_requirement_contract(self.root, goal)
        self.assertFalse(self.writable("Refactor but do not touch config/*.txt", "config/secret.txt"))
        self.assertTrue(self.writable("Refactor but do not touch config/*.txt", "config/app.ini"))
        for relative in ("README.md", "docs/guide.md"):
            self.assertFalse(self.writable("Refactor but do not touch *.md", relative))
        self.assertTrue(self.writable("Refactor but do not touch *.md", "src/app.py"))
        self.assertFalse(self.writable("Refactor but do not touch **/*.txt", "config/secret.txt"))
        self.assertFalse(self.writable("Refactor but do not touch **/*.txt", "notes.txt"))
        self.assertTrue(swarm_work._path_is_under("docs/a/b.md", ["*.md"]))
        self.assertFalse(swarm_work._path_is_under("docs/a/b.py", ["*.md"]))

    # -- F5 -------------------------------------------------------------------

    def test_non_portable_agent_paths_are_refused_one_at_a_time(self) -> None:
        bad = [
            "a" * 300 + ".txt", "src/a<b.txt", "src/a>b.txt", "src/a|b.txt", "src/a*b.txt",
            "src/a?b.txt", 'src/a"b.txt', "src/a\tb.txt", "src/a\x1fb.txt", "src/app.py/x",
        ]
        raw = [{"path": one, "content": "x"} for one in bad] + [{"path": "src/new.py", "content": "ok\n"}]
        changes, refusals = swarm_work._partition_changes(self.root, raw)
        self.assertEqual(["src/new.py"], [one.path for one in changes])
        self.assertEqual(len(bad), len(refusals))
        with mock.patch.object(swarm_work, "confined_path", side_effect=OSError(22, "Invalid argument")):
            changes, refusals = swarm_work._partition_changes(
                self.root, [{"path": "src/fine.py", "content": "x"}],
            )
        self.assertEqual([], changes)
        self.assertIn("file system cannot use this path", refusals[0]["reason"])

    # -- F6 -------------------------------------------------------------------

    def test_one_shared_test_classifier_reads_negation_and_requirement_wording(self) -> None:
        def tests_requirement(goal: str):
            contract = swarm_work._derive_requirement_contract(self.root, goal)
            return next((one for one in contract["requirements"] if one["id"] == "tests"), None)

        for goal in ("Do not add tests; just fix src/app.py", "Fix src/app.py without adding tests",
                     "Fix src/app.py. No need to add tests.", "Add Stripe integration to checkout.js",
                     "Fix the unit conversion bug"):
            with self.subTest(no_tests=goal):
                self.assertIsNone(tests_requirement(goal))
                self.assertFalse(swarm_work._is_test_goal(goal))
        for goal, kind in (
            ("Fix src/app.py. Tests are required.", "write"),
            ("Fix src/app.py; make sure the tests pass", "run"),
            ("Fix src/app.py. It must have tests.", "write"),
            ("Fix src/app.py and write one test for it", "write"),
            ("Fix src/app.py. Don't forget tests!", "write"),
            ("Fix src/app.py and add another test", "write"),
            ("Run the test suite", "run"),
        ):
            with self.subTest(tests=goal):
                requirement = tests_requirement(goal)
                self.assertIsNotNone(requirement)
                self.assertTrue(requirement["mandatory"])
                self.assertEqual(kind, requirement["test_request"])
                self.assertEqual(
                    swarm_work._is_test_goal(goal), goal_verification.tests_explicitly_requested(goal),
                )
        # A run request needs the existing tests to run, not new test files.
        contract = swarm_work._derive_requirement_contract(self.root, "Fix src/app.py; make sure the tests pass")
        evidence = swarm_work._requirement_artifact_evidence(self.root, contract, ["src/app.py"])
        self.assertTrue(evidence["passed"], evidence)
        contract = swarm_work._derive_requirement_contract(self.root, "Fix src/app.py. Tests are required.")
        evidence = swarm_work._requirement_artifact_evidence(self.root, contract, ["src/app.py"])
        self.assertIn("tests", evidence["unmet"])
        self.assertFalse(hasattr(swarm_work, "_TEST_WRITE_REQUEST"))
        self.assertEqual(["E2E", "API", "unit"], swarm_work._requested_test_levels("Add unit, API and E2E tests for checkout"))
        self.assertEqual([], swarm_work._requested_test_levels("Fix the unit conversion bug and add tests"))

    # -- F7 -------------------------------------------------------------------

    def test_explicit_only_wording_is_never_widened(self) -> None:
        cases = (
            ("Only change the tests", "tests/test_app.py", "src/app.py"),
            ("Only change tests", "tests/test_app.py", "src/app.py"),
            ("Edit src/app.py only", "src/app.py", "src/util.py"),
            ("Fix the bug; change nothing except src/app.py", "src/app.py", "src/util.py"),
            ("Fix the bug by editing only src/app.py", "src/app.py", "src/util.py"),
            ("Fix the bug, touching only src/app.py", "src/app.py", "src/util.py"),
            ("Touch nothing but tests/test_app.py", "tests/test_app.py", "src/app.py"),
            ("Refactor src/app.py. Everything else is read-only.", "src/app.py", "src/util.py"),
            ("Only edit docs", "docs/guide.md", "src/app.py"),
            ("Only change config", "config/secret.txt", "src/app.py"),
            ("Everything except src/app.py is read-only", "src/app.py", "src/util.py"),
            ("Leave everything except src/app.py untouched", "src/app.py", "src/util.py"),
            (f"Only change {self.root / 'src'}", "src/app.py", "README.md"),
        )
        for goal, allowed, refused in cases:
            with self.subTest(goal=goal):
                policy = self.policy(goal)
                self.assertEqual("SCOPED", policy["mode"], policy)
                self.assertTrue(self.writable(goal, allowed), policy)
                self.assertFalse(self.writable(goal, refused), policy)

    def test_an_unusable_only_target_stays_restricted_and_asks_the_user(self) -> None:
        for goal in (
            "Only change ../outside", "Only change the sibling folder ../shared", "Only change .git",
            "Only change /etc", "Only change [::1]:8080/src",
            "Only change src/app.py; do not touch src/app.py",
        ):
            with self.subTest(goal=goal):
                policy = self.policy(goal)
                self.assertEqual("SCOPED", policy["mode"], policy)
                self.assertEqual([], policy["only_scope"])
                self.assertTrue(policy["only_requested"])
                self.assertTrue(policy["only_unresolved"])
        for goal in (
            "Only change what is needed", "Only edit the files you need",
            "Not only fix src/app.py but also update docs", "Fix src/app.py. Only then run the tests",
            "Don't only change tests, also fix src/app.py", "Make src/app.py accept only integers",
            f"Work only in {self.root}",
        ):
            with self.subTest(open=goal):
                self.assertEqual("OPEN", self.policy(goal)["mode"])

    def test_work_together_asks_instead_of_widening_an_unusable_only(self) -> None:
        config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.base, [], {})
        config.data["providers"] = {
            "claude": {"kind": "claude-cli", "model": "claude"},
            "codex": {"kind": "codex-cli", "model": "codex"},
        }
        board = {
            "agents": [
                {"id": "agent-1", "name": "Claude", "who": "claude", "job": "lead", "ready": True},
                {"id": "agent-2", "name": "Codex", "who": "codex", "job": "review", "ready": True},
            ],
            "projects": [{
                "id": "project-1", "name": "Demo", "path": str(self.root), "tasks": [],
                "test_commands": [[sys.executable, "-m", "unittest", "discover"]],
            }],
            "works_on": [
                {"agent": "agent-1", "project": "project-1"},
                {"agent": "agent-2", "project": "project-1"},
            ],
            "talks_to": [{"one": "agent-1", "other": "agent-2"}],
        }
        with mock.patch.object(chat, "ask_once") as ask:
            with self.assertRaisesRegex(Exception, "did not widen the restriction"):
                swarm_work.work_together(config, board, "agent-1", "Only change ../outside")
            ask.assert_not_called()

    # -- F8 -------------------------------------------------------------------

    def test_more_protection_wordings_and_bare_folder_names(self) -> None:
        cases = (
            ("Update README.md, but config/secret.txt must not change", "config/secret.txt"),
            ("Update README.md. Keep your hands off config/secret.txt", "config/secret.txt"),
            ("Update README.md. Hands off config/secret.txt", "config/secret.txt"),
            ("Update README.md. Freeze config/secret.txt", "config/secret.txt"),
            ("Update README.md. Nothing in config/ may change", "config/secret.txt"),
            ("Refactor everything except src/util.py", "src/util.py"),
            ("Refactor the code but do not touch config", "config/secret.txt"),
            ("Refactor the code but do not touch anything in src", "src/app.py"),
            ("Refactor the code but do not touch the files in config", "config/secret.txt"),
            ("Update README.md. No changes to config/secret.txt", "config/secret.txt"),
        )
        for goal, protected in cases:
            with self.subTest(goal=goal):
                self.assertFalse(self.writable(goal, protected))
                self.assertTrue(self.writable(goal, "docs/guide.md"))
        # A bare name only resolves to an existing project entry.
        self.assertTrue(self.writable("Refactor the code but do not touch nonexistent", "src/app.py"))
        self.assertEqual("config", swarm_work._resolve_bare_project_name(self.root, "config"))
        self.assertEqual("", swarm_work._resolve_bare_project_name(self.root, "nothing-here"))

    # -- F9 -------------------------------------------------------------------

    def test_reference_tokens_never_become_path_hints(self) -> None:
        for goal in (
            "Fix it. See [::1]:8080/x.py", "Fix C:foo.py", "Fix a.txt:stream",
            "Update SECRET~1.TXT", "Mail mailto:x@y.com about it", "Ping x@y.com",
        ):
            with self.subTest(goal=goal):
                self.assertEqual([], swarm_work._goal_named_paths(goal))
                self.assertEqual([], swarm_work._compile_goal_spec(self.root, goal)["write_policy"]["grants"])
        self.assertEqual(["src/app.py"], swarm_work._goal_named_paths("Fix src/app.py and mail x@y.com"))


if __name__ == "__main__":
    unittest.main()
