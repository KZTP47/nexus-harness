""""Agents lead; Nexus only supports" (AGENTS.md) for the work-together engine.

Nexus restricts agents only on explicit user wording (read-only, "don't touch
X", "only change X") or an explicit UI restriction. Everything it infers from
goal wording, file names or plans is a hint that never blocks the agents.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from our_harness import chat, swarm_work
from our_harness.collaboration_ledger import CollaborationLedger
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError


class AgentsLeadPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.project = self.root / "project"
        self.project.mkdir()
        (self.project / "src").mkdir()
        (self.project / "src" / "parser.py").write_text("def parse(text):\n    return text\n", encoding="utf-8")
        (self.project / "config.py").write_text("DEBUG = False\n", encoding="utf-8")
        (self.project / "test_nexus_acceptance.py").write_text(
            "import unittest\n\nclass NexusAcceptance(unittest.TestCase):\n"
            "    def test_selected_project_runner(self):\n        self.assertTrue(True)\n",
            encoding="utf-8",
        )
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.config.data["providers"] = {
            "claude": {"kind": "claude-cli", "model": "claude"},
            "codex": {"kind": "codex-cli", "model": "codex"},
        }
        self.board = {
            "agents": [
                {"id": "agent-1", "name": "Claude", "who": "claude", "job": "lead", "ready": True},
                {"id": "agent-2", "name": "Codex", "who": "codex", "job": "review", "ready": True},
            ],
            "projects": [{
                "id": "project-1", "name": "Demo", "path": str(self.project), "tasks": [],
                "test_commands": [[sys.executable, "-m", "unittest", "discover"]],
            }],
            "works_on": [
                {"agent": "agent-1", "project": "project-1"},
                {"agent": "agent-2", "project": "project-1"},
            ],
            "talks_to": [{"one": "agent-1", "other": "agent-2"}],
        }

    # -- helpers -----------------------------------------------------------

    def team(self, changes_by_route: dict[str, list[dict]], effect_paths=None, contexts=None):
        """A two-agent team that plans, applies its changes once, and agrees."""

        applied: set[str] = set()

        def answer(_config, route, _text, **kwargs):
            if contexts is not None:
                contexts.append(str(kwargs.get("context") or ""))
            response_format = kwargs.get("response_format")
            if response_format is swarm_work.PLAN_FORMAT:
                value = {
                    "contribution": f"plan by {route}", "message_to_lead": "go",
                    "needs_files": [], "effect_paths": list(effect_paths or []),
                }
            elif response_format is swarm_work.PLAN_REVIEW_FORMAT:
                value = {
                    "contribution": "reviewed", "message_to_lead": "ready", "needs_files": [],
                    "ready_to_execute": True, "remaining": [],
                    "effect_paths": list(effect_paths or []),
                }
            elif response_format is swarm_work.WORK_VERIFICATION_FORMAT:
                value = {"goal_complete": True, "feedback": "Done.", "remaining": []}
            else:
                changes = [] if route in applied else [
                    {"reason": "requested", **one} for one in changes_by_route.get(route, [])
                ]
                applied.add(route)
                value = {"reply": f"{route} worked", "changes": changes}
            return {"text": json.dumps(value), "milliseconds": 1, "model": route}

        return answer

    def run_team(self, goal: str, changes_by_route, **kwargs):
        effect_paths = kwargs.pop("effect_paths", None)
        contexts = kwargs.pop("contexts", None)
        with mock.patch.object(
            chat, "ask_once", side_effect=self.team(changes_by_route, effect_paths, contexts),
        ):
            return swarm_work.work_together(self.config, self.board, "agent-1", goal, **kwargs)

    def ledger_phases(self, result) -> list[str]:
        ledger = CollaborationLedger(
            self.config, "claude", "Claude",
            session_id=result["collaboration_ledger"]["session_id"],
        )
        return [str(one.get("phase") or "") for one in ledger._read()]

    # -- item 1: a mentioned file is never protected -------------------------

    def test_mentioned_files_are_never_protected_without_explicit_wording(self) -> None:
        for goal in (
            "The bug is in src/parser.py; please fix it.",
            "src/parser.py throws on empty input. Please fix.",
            "Please review src/parser.py and fix any bugs you find",
            "Look at src/parser.py and add logging",
            "Use notes.md to update src/parser.py",
            "Read src/parser.py and consult docs/spec.md",
        ):
            with self.subTest(goal=goal):
                roles = swarm_work._goal_path_roles(goal)
                self.assertNotIn("src/parser.py", roles["protected"])
                contract = swarm_work._derive_requirement_contract(self.project, goal)
                self.assertEqual(contract["protected_paths"], [])
                spec = swarm_work._compile_goal_spec(self.project, goal)
                self.assertEqual(spec["write_policy"]["mode"], "OPEN")
                accepted = swarm_work._validated_changes(
                    self.project,
                    [{"path": "src/parser.py", "content": "fixed\n"}],
                    None, contract["protected_paths"], None,
                )
                self.assertEqual([one.path for one in accepted], ["src/parser.py"])

    def test_explicit_do_not_touch_wording_still_protects(self) -> None:
        for goal, protected in (
            ("Fix app.py but don't touch config.py", "config.py"),
            ("Leave config.py alone and fix app.py", "config.py"),
            ("config.py is read-only. Fix app.py.", "config.py"),
            ("Keep config.py unchanged while you refactor app.py", "config.py"),
            ("Refactor app.py without changing config.py", "config.py"),
            ("Update app.py; config.py must not be changed", "config.py"),
            ("Treat config.py as read-only and implement the spec in app.py", "config.py"),
            ("Preserve config.py and update app.py.", "config.py"),
            ("Never edit src/ and fix app.py", "src"),
        ):
            with self.subTest(goal=goal):
                contract = swarm_work._derive_requirement_contract(self.project, goal)
                self.assertIn(protected, contract["protected_paths"])
                self.assertNotIn("app.py", contract["protected_paths"])
                with self.assertRaisesRegex(HarnessError, "protected"):
                    swarm_work._validated_changes(
                        self.project,
                        [{"path": protected + ("/parser.py" if protected == "src" else ""), "content": "x"}],
                        None, contract["protected_paths"], None,
                    )

    def test_behaviour_prohibitions_about_the_program_protect_nothing(self) -> None:
        for goal in (
            "Users cannot delete files in uploads/ folder; fix it",
            "The app must not delete config.py when the user cancels; fix the bug",
        ):
            with self.subTest(goal=goal):
                self.assertEqual(swarm_work._goal_path_roles(goal)["protected"], [])

    def test_explicit_protection_in_work_together_is_enforced_and_reported(self) -> None:
        contexts: list[str] = []
        result = self.run_team(
            "Fix src/parser.py but don't touch config.py",
            {
                "claude": [{"path": "src/parser.py", "content": "fixed\n"}],
                "codex": [{"path": "config.py", "content": "DEBUG = True\n"}],
            },
            contexts=contexts,
        )
        self.assertEqual((self.project / "config.py").read_text(), "DEBUG = False\n")
        self.assertEqual((self.project / "src" / "parser.py").read_text(), "fixed\n")
        self.assertIn("src/parser.py", result["changed"])
        self.assertTrue(any("PATHS THE USER EXPLICITLY SAID NOT TO CHANGE" in one for one in contexts))
        # Only the protected entry is refused (per-entry refusal, item 7).
        self.assertIn("execution_changes_refused", self.ledger_phases(result))
        self.assertTrue(any(one["path"] == "config.py" for one in result["refused_changes"]))

    # -- item 2: named files add, never restrict -----------------------------

    def test_named_files_add_to_what_agents_may_write_and_never_restrict(self) -> None:
        result = self.run_team(
            "Fix the empty-input bug in src/parser.py",
            {
                "claude": [{"path": "src/parser.py", "content": "fixed\n"}],
                "codex": [
                    {"path": "src/helpers.py", "content": "def helper():\n    return 1\n"},
                    {"path": "config.py", "content": "DEBUG = True\n"},
                ],
            },
        )
        self.assertTrue(result["goal_complete"], result["remaining"])
        self.assertFalse(result["write_scope_restricted"])
        self.assertEqual(result["exact_write_grants"], {})
        self.assertEqual(
            sorted(result["changed"]), ["config.py", "src/helpers.py", "src/parser.py"],
        )

    def test_a_named_file_to_fix_may_be_created_when_missing(self) -> None:
        result = self.run_team(
            "Update docs/guide.md with the install steps",
            {"claude": [{"path": "docs/guide.md", "content": "# Install\n"}]},
        )
        self.assertTrue(result["goal_complete"], result["remaining"])
        self.assertEqual((self.project / "docs" / "guide.md").read_text(), "# Install\n")

    def test_validated_changes_treats_grants_as_additive(self) -> None:
        change = [{"path": "other.py", "content": "x"}]
        # Grants without an explicit restriction never restrict.
        self.assertEqual(
            len(swarm_work._validated_changes(self.project, change, None, [], {"a.py": {"MODIFY"}})), 1,
        )
        # With an explicit restriction a grant adds one more writable path.
        self.assertEqual(len(swarm_work._validated_changes(
            self.project, change, ["docs"], [], {"other.py": {"MODIFY"}},
        )), 1)
        with self.assertRaisesRegex(HarnessError, "outside the explicit write destinations"):
            swarm_work._validated_changes(self.project, change, ["docs"], [], None)

    def test_explicit_only_wording_restricts_writes(self) -> None:
        for goal, scope in (
            ("Only change src/parser.py", ["src/parser.py"]),
            ("Fix the bug; change only src/parser.py and config.py", ["src/parser.py", "config.py"]),
            ("Don't change anything except src/parser.py", ["src/parser.py"]),
            ("Limit your changes to docs/", ["docs"]),
            ("Update README.md and nothing else", ["README.md"]),
            ("Make changes only in docs/", ["docs"]),
            ("Only README.md may be changed", ["README.md"]),
        ):
            with self.subTest(goal=goal):
                spec = swarm_work._compile_goal_spec(self.project, goal)
                self.assertEqual(spec["write_policy"]["mode"], "SCOPED")
                self.assertEqual(spec["write_policy"]["only_scope"], scope)
        for goal in ("Fix src/parser.py", "The only bug is in src/parser.py", "Not only fix it but test it"):
            with self.subTest(goal=goal):
                self.assertEqual(swarm_work._compile_goal_spec(self.project, goal)["write_policy"]["mode"], "OPEN")

    def test_explicit_only_scope_is_enforced_in_work_together(self) -> None:
        result = self.run_team(
            "Only change src/parser.py: make it handle empty input",
            {
                "claude": [{"path": "src/parser.py", "content": "fixed\n"}],
                "codex": [{"path": "config.py", "content": "DEBUG = True\n"}],
            },
        )
        self.assertTrue(result["write_scope_restricted"])
        self.assertEqual(result["allowed_write_roots"], ["src/parser.py"])
        self.assertEqual(result["changed"], ["src/parser.py"])
        self.assertEqual((self.project / "config.py").read_text(), "DEBUG = False\n")

    def test_ui_write_roots_still_restrict(self) -> None:
        result = self.run_team(
            "Write the install guide",
            {
                "claude": [{"path": "docs/guide.md", "content": "# Guide\n"}],
                "codex": [{"path": "config.py", "content": "DEBUG = True\n"}],
            },
            allowed_write_roots=["docs"],
        )
        self.assertEqual(result["changed"], ["docs/guide.md"])
        self.assertEqual((self.project / "config.py").read_text(), "DEBUG = False\n")

    # -- item 3: read-only only on explicit wording --------------------------

    def test_questions_and_behaviour_wording_are_never_read_only(self) -> None:
        for goal in (
            "Can you make the app faster?", "Could you optimize the parser?",
            "Why is login broken? Fix it.", "How should we structure this? Then implement it.",
            "Check whether the build works and fix whatever is broken",
            "Show a spinner while loading", "List all users on the admin page",
            "Without breaking the API, speed up the handler",
            "Avoid global state when you implement caching",
            "Files must not be deleted when the user cancels the upload dialog.",
            "The cache should not be updated on failed requests.",
            "Why is login broken?", "Explain how the parser works",
            "Could you both inspect src/parser.py and report whether a cache is needed?",
        ):
            with self.subTest(goal=goal):
                self.assertNotEqual(swarm_work._goal_intent(goal), "read_only")
                self.assertFalse(swarm_work._informational_goal(goal))
                spec = swarm_work._compile_goal_spec(self.project, goal)
                self.assertNotEqual(spec["write_policy"]["mode"], "DENY_ALL")

    def test_explicit_read_only_wording_is_read_only(self) -> None:
        for goal in (
            "Read-only: explain how the parser works",
            "Explain the parser. Don't change anything.",
            "Just explain, don't change.",
            "Review the code, no changes.",
            "Make no changes; tell me why login fails",
            "Do not modify any files. Describe the architecture.",
            "This is a read-only review of src/",
            "Analyze the repo without making any changes",
            "Don't make any changes, just report",
        ):
            with self.subTest(goal=goal):
                self.assertEqual(swarm_work._goal_intent(goal), "read_only")
                spec = swarm_work._compile_goal_spec(self.project, goal)
                self.assertEqual(spec["write_policy"]["mode"], "DENY_ALL")
        for goal in (
            "Don't change anything except src/parser.py",
            "Don't change anything. However, fix the typo in README.md",
            "Refactor the project but do not touch ./config.py",
        ):
            with self.subTest(goal=goal):
                self.assertNotEqual(swarm_work._goal_intent(goal), "read_only")

    def test_question_goal_changes_are_applied(self) -> None:
        result = self.run_team(
            "Can you make the parser faster?",
            {"claude": [{"path": "src/parser.py", "content": "def parse(text):\n    return text  # fast\n"}]},
        )
        self.assertTrue(result["goal_complete"], result["remaining"])
        self.assertEqual(result["changed"], ["src/parser.py"])

    def test_explicit_read_only_proposal_is_reported_not_silently_dropped(self) -> None:
        contexts: list[str] = []
        before = (self.project / "src" / "parser.py").read_text()
        result = self.run_team(
            "Explain the parser. Don't change anything.",
            {"claude": [{"path": "src/parser.py", "content": "changed\n"}]},
            contexts=contexts,
        )
        self.assertEqual((self.project / "src" / "parser.py").read_text(), before)
        self.assertEqual(result["changed"], [])
        self.assertIn("read-only run: src/parser.py", result["answer"]["text"])
        self.assertTrue(any("explicitly asked for a read-only run" in one for one in contexts))
        self.assertIn("read_only_proposal_rejected", self.ledger_phases(result))

    # -- item 4: absolute project paths --------------------------------------

    def test_unquoted_absolute_paths_end_at_prose(self) -> None:
        docs = str(self.project / "docs")
        found = swarm_work._absolute_prompt_paths(f"Put the guide in {docs} please and update README.md")
        self.assertEqual([raw for _line, raw in found], [docs])
        spaced = self.project / "My Projects" / "app"
        spaced.mkdir(parents=True)
        found = swarm_work._absolute_prompt_paths(f"Work in {spaced} and fix it")
        self.assertEqual([raw for _line, raw in found], [str(spaced)])

    def test_naming_the_project_root_means_full_access(self) -> None:
        for goal in (
            f"Work in {self.project} and add a guide",
            f"Create all result files in:\n{self.project}\n",
            f"Only work in {self.project}",
        ):
            with self.subTest(goal=goal):
                authority = swarm_work._path_authority_from_goal(self.project, goal)
                self.assertEqual(authority["invalid_writable"], [])
                self.assertEqual(authority["writable"], [])
                self.assertEqual(
                    swarm_work._compile_goal_spec(self.project, goal)["write_policy"]["mode"], "OPEN",
                )
        result = self.run_team(
            f"Create all result files in:\n{self.project}\n",
            {"claude": [{"path": "notes/result.md", "content": "done\n"}]},
        )
        self.assertFalse(result["write_scope_restricted"])
        self.assertEqual(result["changed"], ["notes/result.md"])

    def test_mentioning_a_sub_folder_does_not_restrict_writes(self) -> None:
        result = self.run_team(
            f"Put the guide in {self.project / 'docs'} please",
            {"claude": [
                {"path": "docs/guide.md", "content": "# Guide\n"},
                {"path": "README.md", "content": "See docs/guide.md\n"},
            ]},
        )
        self.assertFalse(result["write_scope_restricted"])
        self.assertEqual(sorted(result["changed"]), ["README.md", "docs/guide.md"])
        spec = swarm_work._compile_goal_spec(
            self.project, f"Only change files in {self.project / 'docs'}",
        )
        self.assertEqual(spec["write_policy"]["only_scope"], ["docs"])

    def test_an_outside_destination_is_reported_instead_of_stopping_the_run(self) -> None:
        contexts: list[str] = []
        result = self.run_team(
            "Create all result files in:\nZ:\\outside\\project\\output",
            {"claude": [{"path": "output/result.txt", "content": "x\n"}]},
            contexts=contexts,
        )
        self.assertTrue(any("PATHS OUTSIDE THE SELECTED PROJECT" in one for one in contexts))
        self.assertIn("goal_outside_project_destination", self.ledger_phases(result))
        self.assertEqual(result["changed"], ["output/result.txt"])

    # -- item 5: only well-formed path tokens --------------------------------

    def test_prose_around_paths_never_becomes_a_path_or_operation(self) -> None:
        goal = "Rename the Save button to Submit in index.html"
        self.assertEqual(swarm_work._goal_named_paths(goal), ["index.html"])
        self.assertEqual(
            [one for one in swarm_work._goal_operations(goal) if one["kind"] == "rename"], [],
        )
        goal = "Fix the list rendering (see api/users.py and web/list.js)"
        self.assertEqual(sorted(swarm_work._goal_named_paths(goal)), ["api/users.py", "web/list.js"])
        for operation in swarm_work._goal_operations(goal):
            for field in ("target", "source", "destination"):
                self.assertNotIn(" ", str(operation.get(field) or ""))
        self.assertEqual(swarm_work._goal_named_paths('Update "Release Notes.md"'), ["Release Notes.md"])
        self.assertEqual(swarm_work._goal_named_paths("Update Release Notes.md"), ["Notes.md"])

    def test_derived_operations_are_hints_that_never_block_completion(self) -> None:
        (self.project / "a.md").write_text("a\n", encoding="utf-8")
        contract = swarm_work._derive_requirement_contract(self.project, "Move a.md to docs/a.md")
        self.assertTrue(any(one["kind"] == "operation_postcondition" for one in contract["requirements"]))
        self.assertTrue(all(one["mandatory"] is False for one in contract["requirements"]))
        evidence = swarm_work._requirement_artifact_evidence(self.project, contract, ["notes.md"])
        self.assertTrue(evidence["passed"], evidence)
        self.assertEqual(evidence["unmet"], [])
        self.assertTrue(evidence["advisory_unmet"])

    # -- item 6: plans are hints ---------------------------------------------

    def test_planned_effect_paths_are_hints_and_never_raise(self) -> None:
        goal = "Fix src/parser.py but don't touch config.py"
        contract = swarm_work._derive_requirement_contract(
            self.project, goal, ["src/parser.py", "src/extra.py", "config.py"],
        )
        self.assertEqual(contract["planned_effect_paths"], ["src/parser.py", "src/extra.py"])
        self.assertFalse(any(
            "src/extra.py" in one.get("effect_paths", []) for one in contract["requirements"]
        ))
        effect = swarm_work._goal_effect_evidence(
            self.project, goal, ["src/parser.py"], ["src/extra.py", "config.py"],
        )
        self.assertTrue(effect["passed"], effect)
        self.assertIn("src/extra.py", effect["unchanged_hints"])

    def test_a_planned_file_left_alone_does_not_block_completion(self) -> None:
        result = self.run_team(
            "Improve the parser",
            {"claude": [{"path": "src/parser.py", "content": "better\n"}]},
            effect_paths=["src/parser.py", "src/cache.py", "config.py"],
        )
        self.assertTrue(result["goal_complete"], result["remaining"])
        self.assertEqual(result["changed"], ["src/parser.py"])

    # -- item 7: tests only when explicitly asked ----------------------------

    def test_test_requirements_need_an_explicit_request(self) -> None:
        for goal in (
            "Add Stripe integration to checkout.js", "Fix the unit conversion bug",
            "Update the API client", "Fix the bug that breaks login",
        ):
            with self.subTest(goal=goal):
                self.assertFalse(swarm_work._is_test_goal(goal))
                contract = swarm_work._derive_requirement_contract(self.project, goal)
                self.assertNotIn("tests", [one["id"] for one in contract["requirements"]])
        for goal, levels in (
            ("Add tests for the parser", []), ("Write a unit test for parse()", ["unit"]),
            ("Make the tests pass", []), ("Run the test suite", []),
            ("Add unit, API and E2E tests for checkout", ["E2E", "API", "unit"]),
        ):
            with self.subTest(goal=goal):
                self.assertTrue(swarm_work._is_test_goal(goal))
                contract = swarm_work._derive_requirement_contract(self.project, goal)
                tests = next(one for one in contract["requirements"] if one["id"] == "tests")
                self.assertTrue(tests["mandatory"])
                self.assertEqual(tests["requested_levels"], levels)

    def test_integration_wording_does_not_demand_test_files(self) -> None:
        project = self.root / "shop"
        project.mkdir()
        (project / "checkout.js").write_text("module.exports = {};\n", encoding="utf-8")
        record = {"id": "shop", "name": "Shop", "path": str(project), "tasks": []}
        result = swarm_work._run_selected_project_verification(
            self.config, project, record, "Add Stripe integration to checkout.js", ["checkout.js"], None,
        )
        self.assertEqual(result["status"], "unavailable", result)
        result = swarm_work._run_selected_project_verification(
            self.config, project, record, "Add tests for checkout.js", ["checkout.js"], None,
        )
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(result["basis"], "test_preflight")

    # -- item 8: completion vetoes -------------------------------------------

    def test_unavailable_verification_completes_by_agent_consensus(self) -> None:
        self.board["projects"][0].pop("test_commands")
        (self.project / "test_nexus_acceptance.py").unlink()
        result = self.run_team(
            "Update README.md with install instructions",
            {"claude": [{"path": "README.md", "content": "# Install\npip install .\n"}]},
        )
        self.assertTrue(result["goal_complete"], result["remaining"])
        self.assertTrue(result["verified"])
        self.assertFalse(result["machine_verified"])
        self.assertEqual(result["verification_status"], "agent_verified")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["deterministic_verification"]["status"], "unavailable")
        self.assertIn("not machine-verified", result["answer"]["text"])
        self.assertIn("completed_without_machine_verification", self.ledger_phases(result))

    def test_missing_containment_completes_without_a_fabricated_pass(self) -> None:
        unavailable = {
            "status": "unavailable", "basis": "verification_containment_unavailable",
            "commands": [], "reason": "No containment profile for cargo.",
        }
        with mock.patch.object(swarm_work, "_run_selected_project_verification", return_value=unavailable):
            result = self.run_team(
                "Speed up the Rust parser",
                {"claude": [{"path": "src/lib.rs", "content": "pub fn parse() {}\n"}]},
            )
        self.assertTrue(result["goal_complete"])
        self.assertFalse(result["machine_verified"])
        self.assertEqual(result["deterministic_verification"]["status"], "unavailable")

    def test_failing_checks_still_do_not_complete(self) -> None:
        failed = {"status": "failed", "basis": "selected_project", "commands": [], "reason": "tests failed"}
        with mock.patch.object(swarm_work, "_run_selected_project_verification", return_value=failed):
            result = self.run_team(
                "Speed up the parser",
                {"claude": [{"path": "src/parser.py", "content": "broken\n"}]},
                round_limit=1,
            )
        self.assertFalse(result["goal_complete"])
        self.assertFalse(result["verified"])

    def test_goal_wording_never_creates_hard_behaviour_requirements(self) -> None:
        (self.project / "src" / "parser.py").write_text(
            "def parse(text):\n    return text\n\ndef tokens(text):\n    return text.split()\n",
            encoding="utf-8",
        )
        for goal in (
            "Refactor the project for readability", "Upgrade all dependencies",
            "Fix src/parser.py so empty input is rejected",
        ):
            with self.subTest(goal=goal):
                contract = swarm_work._derive_requirement_contract(self.project, goal)
                self.assertFalse(any(one.get("mandatory") for one in contract["requirements"]))
                execution = swarm_work._executed_requirement_evidence(
                    contract, [], [], {"verification_evidence": []}, {}, {}, [],
                )
                self.assertTrue(execution["passed"], execution)
                spec = swarm_work._compile_goal_spec(self.project, goal)
                decision = swarm_work._acceptance_target_decision(self.project, goal, spec)
                self.assertNotEqual(decision["status"], "needs_clarification")

    def test_no_change_needed_is_a_valid_completion(self) -> None:
        effect = swarm_work._goal_effect_evidence(self.project, "Fix src/parser.py", [])
        self.assertTrue(effect["passed"])
        self.assertTrue(effect["no_change"])
        result = self.run_team("Fix the parser if it is broken", {})
        self.assertTrue(result["goal_complete"], result["remaining"])
        self.assertEqual(result["changed"], [])
        # An explicitly read-only goal that changed files still fails.
        effect = swarm_work._goal_effect_evidence(self.project, "Read-only: inspect src/parser.py", ["src/parser.py"])
        self.assertFalse(effect["passed"])

    def test_any_project_edit_counts_as_semantic_progress(self) -> None:
        contract = swarm_work._derive_requirement_contract(
            self.project, "Fix src/parser.py so empty input is rejected",
        )
        for relative in ("src/parser.py", "src/anything_else.py", "README.md"):
            self.assertTrue(swarm_work._delta_path_matches_requirement(self.project, contract, relative))
        for relative in (".harness/state.json", ".git/config", "../outside.py"):
            self.assertFalse(swarm_work._delta_path_matches_requirement(self.project, contract, relative))

    # -- regressions from review (E1, E2, E4, E5) ----------------------------

    def test_references_and_code_scopes_never_become_project_paths(self) -> None:
        for goal in (
            "Create localhost:3000/index.html",
            "Update the page served at localhost:3000/app.js",
            "Fix the handler at api:8080/routes.py",
            "Update std::chrono/clock.h",
            "Edit /etc/app.py", "Edit std::/etc/app.py", "Edit x:1//srv/share/evil.py",
            "Edit ns::C:\\Windows\\evil.py", "Edit a::b::.git/config.py",
            "Edit std::.harness/x.json", "Edit localhost:3000/.git/hooks/pre-commit.py",
            "See C:\\x\\y.txt", "See \\\\server\\share\\y.txt",
        ):
            with self.subTest(goal=goal):
                self.assertEqual(swarm_work._goal_named_paths(goal), [])
        self.assertEqual(
            swarm_work._goal_named_paths("Make the page at localhost:8080/index.html load app.js"),
            ["app.js"],
        )

    def test_dot_slash_and_backslash_protections_survive(self) -> None:
        (self.project / "config").mkdir()
        (self.project / "config" / "secret.txt").write_text("s", encoding="utf-8")
        for goal in (
            "Refactor the project but do not touch config/secret.txt",
            "Refactor the project but do not touch ./config/secret.txt",
            "Refactor the project but do not touch .\\config\\secret.txt",
            "Update app.py; keep ./config/secret.txt read-only",
            "Update app.py; do not touch config/secret.txt:1",
        ):
            with self.subTest(goal=goal):
                contract = swarm_work._derive_requirement_contract(self.project, goal)
                self.assertEqual(contract["protected_paths"], ["config/secret.txt"])
                self.assertNotEqual(contract["intent"], "read_only")
                with self.assertRaisesRegex(HarnessError, "protected"):
                    swarm_work._validated_changes(
                        self.project, [{"path": "config/secret.txt", "content": "pwned"}],
                        None, contract["protected_paths"], None,
                    )
        self.assertEqual(swarm_work._normalize_goal_path("./config/secret.txt"), "config/secret.txt")

    def test_absolute_or_drive_glob_is_a_tool_error_not_a_crash(self) -> None:
        for query in ("glob:/src/*.py", "glob:C:x", "glob:C:*", "glob:/x"):
            with self.subTest(query=query):
                paths, diagnostic = swarm_work._safe_query_paths(self.project, query)
                self.assertEqual(paths, [])
                self.assertIn("unsafe glob request rejected", diagnostic)
        text = swarm_work._requested_files(self.project, [({}, {"needs_files": ["glob:/src/*.py"]})])
        self.assertIn("unsafe glob request rejected", text)

    def test_file_line_references_with_folders_are_accepted(self) -> None:
        self.assertEqual(
            swarm_work._goal_named_paths("Fix src/app.py:42 and tests/test_app.py:10"),
            ["src/app.py", "tests/test_app.py"],
        )
        self.assertEqual(swarm_work._goal_path_roles("Fix src/app.py:42 crash")["effects"], ["src/app.py"])
        # An unsafe spelling never refuses the goal; it is reported and grants nothing.
        self.assertEqual(
            swarm_work._validate_goal_path_syntax("Fix localhost:3000/../secret.txt"),
            ["localhost:3000/../secret.txt"],
        )


    # -- agents keep working: pauses, failures, leniency ----------------------

    @staticmethod
    def _default_value(response_format, route: str) -> dict:
        if response_format is swarm_work.PLAN_FORMAT:
            return {"contribution": f"plan by {route}", "message_to_lead": "go", "needs_files": []}
        if response_format is swarm_work.PLAN_REVIEW_FORMAT:
            return {
                "contribution": "reviewed", "message_to_lead": "ready", "needs_files": [],
                "ready_to_execute": True, "remaining": [],
            }
        if response_format is swarm_work.WORK_VERIFICATION_FORMAT:
            return {"goal_complete": True, "feedback": "Done.", "remaining": []}
        return {"reply": f"{route} worked", "changes": []}

    @staticmethod
    def _format_slip() -> Exception:
        try:
            raise chat.StructuredReplyError(
                "The assistant returned malformed nexus_board_file_work_v2 JSON twice."
            )
        except chat.StructuredReplyError as cause:
            error = chat.ChatError(
                "codex was asked and did not answer: The assistant returned malformed "
                "nexus_board_file_work_v2 JSON twice."
            )
            error.__cause__ = cause
            return error

    def _journals(self) -> list[dict]:
        folder = self.project / ".harness" / "swarm-mutation-sagas"
        return [json.loads(path.read_text(encoding="utf-8")) for path in folder.glob("*.json")]

    def test_a_provider_pause_keeps_applied_work_and_resume_continues(self) -> None:
        state = {"codex_work": 0, "claude_done": False}
        contexts: list[str] = []

        def answer(_config, route, _text, **kwargs):
            response_format = kwargs.get("response_format")
            if response_format is swarm_work.EXECUTION_FORMAT:
                contexts.append(str(kwargs.get("context") or ""))
                if route == "claude" and not state["claude_done"]:
                    state["claude_done"] = True
                    value = {"reply": "fixed", "changes": [{"path": "src/parser.py", "content": "fixed\n"}]}
                    return {"text": json.dumps(value), "milliseconds": 1, "model": route}
                if route == "codex":
                    state["codex_work"] += 1
                    if state["codex_work"] == 1:
                        raise chat.ChatError("codex was asked and did not answer: connection reset")
            value = self._default_value(response_format, route)
            return {"text": json.dumps(value), "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            with self.assertRaises(swarm_work.ResumableSwarmError) as paused:
                swarm_work.work_together(self.config, self.board, "agent-1", "Fix src/parser.py")
        payload = paused.exception.payload
        self.assertEqual(payload["status"], "paused_provider")
        self.assertEqual(payload["stopped_because"], "provider_unavailable")
        self.assertEqual(payload["failure_code"], "provider_turn_failed")
        self.assertEqual(payload["mutation_recovery"]["status"], "kept")
        self.assertIn("src/parser.py", payload["changed"])
        self.assertIn("src/parser.py", payload["checkpoint"]["changed"])
        self.assertIn("kept the project changes", str(paused.exception))
        self.assertEqual((self.project / "src" / "parser.py").read_text(), "fixed\n")
        self.assertEqual({one["phase"] for one in self._journals()}, {"kept"})
        # Restart: a kept saga is never compensated later.
        self.assertEqual(swarm_work._MutationSaga.recover_orphans(self.project), [])
        self.assertEqual((self.project / "src" / "parser.py").read_text(), "fixed\n")
        contexts.clear()
        with mock.patch.object(chat, "ask_once", side_effect=answer):
            resumed = swarm_work.work_together(
                self.config, self.board, "agent-1", "",
                resume_session_id=payload["resume_token"],
            )
        self.assertTrue(resumed["goal_complete"], resumed["remaining"])
        self.assertIn("src/parser.py", resumed["changed"])
        self.assertTrue(any("FILE src/parser.py\nfixed" in one for one in contexts))

    def test_a_format_slip_in_execution_skips_only_that_turn(self) -> None:
        state = {"codex_work": 0}

        def answer(_config, route, _text, **kwargs):
            response_format = kwargs.get("response_format")
            if response_format is swarm_work.EXECUTION_FORMAT and route == "claude":
                value = {"reply": "fixed", "changes": [{"path": "src/parser.py", "content": "fixed\n"}]}
                return {"text": json.dumps(value), "milliseconds": 1, "model": route}
            if response_format is swarm_work.EXECUTION_FORMAT and route == "codex":
                state["codex_work"] += 1
                if state["codex_work"] == 1:
                    raise self._format_slip()
            value = self._default_value(response_format, route)
            return {"text": json.dumps(value), "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            result = swarm_work.work_together(self.config, self.board, "agent-1", "Fix src/parser.py")
        self.assertTrue(result["goal_complete"], result["remaining"])
        self.assertEqual(result["changed"], ["src/parser.py"])
        self.assertEqual([one["id"] for one in result["format_failures"]], ["agent-2"])
        self.assertIn("provider_protocol_failure", self.ledger_phases(result))

    def test_failure_causes_are_reported_accurately(self) -> None:
        self.assertTrue(swarm_work._is_protocol_failure(self._format_slip()))
        self.assertTrue(swarm_work._is_protocol_failure(
            swarm_work.StructuredCollaborationError("x did not return the structured collaboration result")
        ))
        self.assertFalse(swarm_work._is_protocol_failure(chat.ChatError("network down")))
        self.assertEqual(swarm_work._failure_code(self._format_slip()), "invalid_structured_result")
        self.assertEqual(swarm_work._failure_code(chat.ChatError("network down")), "provider_turn_failed")

    def test_an_incomplete_run_keeps_the_applied_work(self) -> None:
        def answer(_config, route, _text, **kwargs):
            response_format = kwargs.get("response_format")
            if response_format is swarm_work.EXECUTION_FORMAT and route == "claude":
                value = {"reply": "fixed", "changes": [{"path": "src/parser.py", "content": "fixed\n"}]}
            elif response_format is swarm_work.WORK_VERIFICATION_FORMAT:
                value = {"goal_complete": False, "feedback": "Not yet.", "remaining": ["Handle unicode"]}
            else:
                value = self._default_value(response_format, route)
            return {"text": json.dumps(value), "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            result = swarm_work.work_together(
                self.config, self.board, "agent-1", "Fix src/parser.py", round_limit=1,
            )
        self.assertFalse(result["goal_complete"])
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["stopped_because"], "round_limit")
        self.assertEqual(result["mutation_recovery"]["status"], "kept")
        self.assertEqual((self.project / "src" / "parser.py").read_text(), "fixed\n")
        self.assertIn("kept in the project", result["answer"]["text"])
        self.assertTrue(result["resume_token"])
        self.assertNotIn("compensated", {one["phase"] for one in self._journals()})

    def test_a_failed_transaction_is_undone_alone_and_the_run_continues(self) -> None:
        from our_harness.changes import FileTransaction

        real_apply = FileTransaction.apply

        def apply(transaction, plans, *args, **kwargs):
            plans = list(plans)
            if any(one.path == "config.py" for one in plans):
                raise HarnessError("Baseline conflict before exclusive replacement: config.py")
            return real_apply(transaction, plans, *args, **kwargs)

        with mock.patch.object(FileTransaction, "apply", apply):
            result = self.run_team("Fix src/parser.py and config.py", {
                "claude": [{"path": "src/parser.py", "content": "fixed\n"}],
                "codex": [{"path": "config.py", "content": "DEBUG = True\n"}],
            })
        self.assertEqual((self.project / "src" / "parser.py").read_text(), "fixed\n")
        self.assertEqual((self.project / "config.py").read_text(), "DEBUG = False\n")
        self.assertEqual(result["changed"], ["src/parser.py"])
        self.assertEqual(result["transaction_failures"][0]["paths"], ["config.py"])
        self.assertTrue(result["transaction_failures"][0]["restored"])
        self.assertIn("mutation_failed", self.ledger_phases(result))

    def test_one_bad_change_entry_is_refused_alone(self) -> None:
        changes, refusals = swarm_work._partition_changes(self.project, [
            {"path": "a.txt", "content": "a"},
            {"path": "a.txt", "content": "again"},
            {"path": "b.txt", "content": "b", "mode": 99999},
            {"path": ".git/config", "content": "x"},
            "not an object",
            {"path": "c.txt", "content": "c"},
        ])
        self.assertEqual([one.path for one in changes], ["a.txt", "c.txt"])
        self.assertEqual([one["index"] for one in refusals], [1, 2, 3, 4])
        self.assertIn("duplicate", refusals[0]["reason"])
        self.assertIn("permission bits", refusals[1]["reason"])
        notice = swarm_work._refusal_notice("Codex", refusals)
        self.assertIn("#2 a.txt", notice)
        self.assertIn("applied the rest", notice)
        # The strict helper other engines use is unchanged.
        with self.assertRaisesRegex(HarnessError, "duplicate"):
            swarm_work._validated_changes(self.project, [
                {"path": "a.txt", "content": "a"}, {"path": "a.txt", "content": "b"},
            ])

    def test_replies_are_read_leniently(self) -> None:
        text = (
            "Here is my work:\n```json\n" + json.dumps({
                "reply": "done", "notes": "extra key",
                "changes": [{"path": "x.txt", "content": "x", "extra": 1, "reason": None}],
                "tool_calls": [{"name": "read_file", "arguments": {"path": "src/parser.py"}}],
            }) + "\n```\nThanks!"
        )
        self.assertEqual(swarm_work.EXECUTION_FORMAT.received_problem(text), "")
        value = swarm_work._decode({"text": text}, "Claude", swarm_work.EXECUTION_FORMAT)
        self.assertNotIn("notes", value)
        self.assertEqual(value["changes"], [{"path": "x.txt", "content": "x"}])
        prepared, _notes = swarm_work._tool_call_with_defaults(value["tool_calls"][0], 0)
        self.assertEqual(prepared["arguments"], {
            "path": "src/parser.py", "start_line": 1, "end_line": 10_000_000, "max_bytes": 32_000,
        })
        long_message = "m" * (swarm_work._REPLY_TEXT_CHARACTERS + 50)
        decoded = swarm_work._decode({"text": json.dumps({
            "message": long_message, "goal_complete": "true", "remaining": "one item",
        })}, "Codex", swarm_work.DISCUSSION_FORMAT)
        self.assertTrue(decoded["goal_complete"])
        self.assertEqual(decoded["remaining"], ["one item"])
        self.assertIn("Nexus shortened this field: 50 more characters", decoded["message"])
        # Nothing usable still fails, so chat's one correction can run.
        self.assertTrue(swarm_work.DISCUSSION_FORMAT.received_problem("I agree with the plan."))
        self.assertTrue(swarm_work.DISCUSSION_FORMAT.received_problem('{"answer": 1}'))
        # chat.py's repair helper asks the format itself.
        self.assertEqual(chat._contract_failure(text, swarm_work.EXECUTION_FORMAT), "")
        self.assertTrue(chat._contract_failure(text, swarm_work.WORK_FORMAT))
        # Long-horizon's strict decoder path is unchanged.
        with self.assertRaises(swarm_work.StructuredCollaborationError):
            swarm_work._decode({"text": text}, "Claude", swarm_work.WORK_FORMAT)

    def test_the_sent_schema_stays_strict_compatible(self) -> None:
        from our_harness.providers.codex_cli import _codex_output_schema

        native = _codex_output_schema(swarm_work.EXECUTION_FORMAT.schema)
        self.assertIs(native["additionalProperties"], False)
        read_file = next(
            one for one in native["properties"]["tool_calls"]["items"]["anyOf"]
            if one["properties"]["name"]["enum"] == ["read_file"]
        )
        self.assertEqual(
            sorted(read_file["properties"]["arguments"]["required"]),
            sorted(read_file["properties"]["arguments"]["properties"]),
        )
        self.assertIn("null", read_file["properties"]["arguments"]["properties"]["start_line"]["type"])
        self.assertEqual(swarm_work.EXECUTION_FORMAT.schema["properties"]["changes"]["maxItems"], 200)
        # Long-horizon goals embed the v1 contract, which is unchanged.
        self.assertEqual(swarm_work.WORK_FORMAT.schema["properties"]["changes"]["maxItems"], 12)

    def test_tool_calls_and_changes_in_one_response_both_happen(self) -> None:
        contexts: list[str] = []
        state = {"claude": 0}

        def answer(_config, route, _text, **kwargs):
            response_format = kwargs.get("response_format")
            if response_format is swarm_work.EXECUTION_FORMAT and route == "claude":
                contexts.append(str(kwargs.get("context") or ""))
                state["claude"] += 1
                if state["claude"] == 1:
                    return {"text": "Sure.\n" + json.dumps({
                        "reply": "applying and checking",
                        "changes": [{"path": "src/parser.py", "content": "def parse(text):\n    return text.strip()\n"}],
                        "tool_calls": [{"name": "read_file", "arguments": {"path": "src/parser.py"}}, "junk"],
                    }), "milliseconds": 1, "model": route}
            value = self._default_value(response_format, route)
            return {"text": json.dumps(value), "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            result = swarm_work.work_together(self.config, self.board, "agent-1", "Fix src/parser.py")
        self.assertTrue(result["goal_complete"], result["remaining"])
        self.assertEqual(result["changed"], ["src/parser.py"])
        self.assertIn("Applied before running your tools: src/parser.py", contexts[1])
        self.assertIn("text.strip()", contexts[1])
        self.assertIn("malformed_tool_call", contexts[1])

    def test_identical_rounds_only_earn_a_notice_and_never_stop_the_team(self) -> None:
        contexts: list[str] = []

        def answer(_config, route, _text, **kwargs):
            contexts.append(str(kwargs.get("context") or ""))
            if kwargs.get("response_format") is swarm_work.DISCUSSION_FORMAT:
                value = {"message": "still thinking", "goal_complete": False, "remaining": ["decide"]}
                return {"text": json.dumps(value), "milliseconds": 1, "model": route}
            return {"text": f"{route} says hi", "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            result = swarm_work.collaborate(
                self.config, self.board, "agent-1", "Decide the plan", round_limit=9,
            )
        self.assertEqual(result["stopped_because"], "round_limit")
        self.assertEqual(result["discussion_rounds"], 9)
        self.assertTrue(any("NEXUS NOTICE" in one for one in contexts))

    def test_only_a_long_stretch_with_no_change_at_all_stops_an_unlimited_run(self) -> None:
        state = {"calls": 0}

        def answer(_config, route, _text, **kwargs):
            if kwargs.get("response_format") is swarm_work.DISCUSSION_FORMAT:
                state["calls"] += 1
                value = {
                    "message": f"reworded {state['calls']}", "goal_complete": False,
                    "remaining": ["decide"],
                }
                return {"text": json.dumps(value), "milliseconds": 1, "model": route}
            return {"text": f"{route} says hi", "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer), \
                mock.patch.object(swarm_work, "NO_CHANGE_GUARD_ROUNDS", 5):
            result = swarm_work.collaborate(
                self.config, self.board, "agent-1", "Decide the plan", round_limit=None,
            )
        self.assertEqual(result["stopped_because"], "no_change_guard")
        self.assertEqual(result["discussion_rounds"], 5)
        self.assertTrue(any("without a new outcome" in one for one in result["remaining"]))
        # Rewording the remaining list is not a new outcome either (F3).
        state["calls"] = 0

        def changing(_config, route, _text, **kwargs):
            if kwargs.get("response_format") is swarm_work.DISCUSSION_FORMAT:
                state["calls"] += 1
                value = {"message": "m", "goal_complete": False, "remaining": [f"step {state['calls']}"]}
                return {"text": json.dumps(value), "milliseconds": 1, "model": route}
            return {"text": f"{route} says hi", "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=changing),                 mock.patch.object(swarm_work, "NO_CHANGE_GUARD_ROUNDS", 5):
            reworded = swarm_work.collaborate(
                self.config, self.board, "agent-1", "Decide the plan", round_limit=12,
            )
        self.assertEqual(reworded["stopped_because"], "no_change_guard")
        self.assertEqual(reworded["discussion_rounds"], 5)
        # The user's own smaller round limit still applies first.
        with mock.patch.object(chat, "ask_once", side_effect=changing),                 mock.patch.object(swarm_work, "NO_CHANGE_GUARD_ROUNDS", 5):
            limited = swarm_work.collaborate(
                self.config, self.board, "agent-1", "Decide the plan", round_limit=3,
            )
        self.assertEqual(limited["stopped_because"], "round_limit")

    def test_consensus_allows_optional_and_repeated_complete_claims(self) -> None:
        def run(codex_remaining):
            def answer(_config, route, _text, **kwargs):
                if kwargs.get("response_format") is swarm_work.DISCUSSION_FORMAT:
                    value = {
                        "message": "done", "goal_complete": True,
                        "remaining": codex_remaining if route == "codex" else [],
                    }
                    return {"text": json.dumps(value), "milliseconds": 1, "model": route}
                return {"text": f"{route} answer", "milliseconds": 1, "model": route}

            with mock.patch.object(chat, "ask_once", side_effect=answer):
                return swarm_work.collaborate(self.config, self.board, "agent-1", "Answer it")

        optional = run(["Optional: add a docs example", "None"])
        self.assertTrue(optional["goal_complete"])
        self.assertEqual(optional["discussion_rounds"], 1)
        quirk = run(["rewrite section two"])
        self.assertTrue(quirk["goal_complete"])
        self.assertEqual(quirk["discussion_rounds"], 2)
        self.assertIn("rewrite section two", quirk["advisory_remaining"])
        self.assertEqual(swarm_work._blocking_remaining(
            ["N/A", "Optional: tidy", "Non-blocking: rename", "Fix the crash"],
        ), ["Fix the crash"])

    def test_an_agent_that_fails_a_round_rejoins_the_next_one(self) -> None:
        state = {"codex_first": 0, "codex_discussion": 0}

        def answer(_config, route, _text, **kwargs):
            response_format = kwargs.get("response_format")
            if response_format is swarm_work.DISCUSSION_FORMAT:
                if route == "codex":
                    state["codex_discussion"] += 1
                    if state["codex_discussion"] == 1:
                        raise self._format_slip()
                value = {"message": "agreed", "goal_complete": True, "remaining": []}
                return {"text": json.dumps(value), "milliseconds": 1, "model": route}
            if route == "codex" and not state["codex_first"]:
                state["codex_first"] = 1
                raise chat.ChatError("codex was asked and did not answer: timeout")
            return {"text": f"{route} answer", "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            result = swarm_work.collaborate(self.config, self.board, "agent-1", "Answer it")
        # Round 1 of the first answers lost Codex, discussion round 1 had a
        # format slip; Codex rejoined and the team finished together.
        self.assertTrue(result["goal_complete"], result)
        self.assertEqual(result["discussion_rounds"], 2)
        self.assertEqual(state["codex_discussion"], 2)
        self.assertEqual(result["stopped_because"], "complete")

    def test_more_than_six_agents_take_part_and_a_cap_is_announced(self) -> None:
        agents = [dict(self.board["agents"][0])] + [
            {"id": f"peer-{n}", "name": f"Peer {n}", "who": "codex", "job": "review", "ready": True}
            for n in range(29)
        ]
        board = {
            "agents": agents, "projects": self.board["projects"],
            "works_on": [{"agent": one["id"], "project": "project-1"} for one in agents],
            "talks_to": [{"one": "agent-1", "other": one["id"]} for one in agents[1:]],
        }
        found, left_out = swarm_work._participants_and_left_out(board, agents[0])
        self.assertEqual(len(found), swarm_work.MOST_PARTICIPANTS)
        self.assertEqual(len(left_out), 30 - swarm_work.MOST_PARTICIPANTS)
        self.assertIn("Peer 28", swarm_work._left_out_notice(left_out))
        small = {**board, "agents": agents[:8]}
        self.assertEqual(len(swarm_work._participants(small, agents[0])), 8)
        project_found, project_left = swarm_work._project_participants_and_left_out(
            board, agents[0], "project-1",
        )
        self.assertEqual(len(project_found), swarm_work.MOST_PARTICIPANTS)
        self.assertTrue(project_left)

    def test_unsafe_path_spellings_never_refuse_the_goal(self) -> None:
        goal = "Fix cfg//secret.txt and src/../app.py, then check localhost:3000/../x"
        spec = swarm_work._compile_goal_spec(self.project, goal)
        self.assertIn("cfg//secret.txt", spec["ignored_path_tokens"])
        self.assertNotIn("cfg//secret.txt", spec["write_policy"]["grants"])
        self.assertFalse(any(".." in one or "//" in one for one in spec["write_policy"]["grants"]))
        protected = swarm_work._compile_goal_spec(self.project, "Fix app.py but don't touch cfg//secret.txt")
        self.assertIn("cfg/secret.txt", protected["write_policy"]["protected"])
        contexts: list[str] = []
        result = self.run_team(
            "Fix src/parser.py (see cfg//notes.txt)",
            {"claude": [{"path": "src/parser.py", "content": "fixed\n"}]}, contexts=contexts,
        )
        self.assertTrue(result["goal_complete"], result["remaining"])
        self.assertTrue(any("cfg//notes.txt" in one and "IGNORED" in one for one in contexts))
        # A write through an unsafe spelling is still refused when applied.
        _changes, refusals = swarm_work._partition_changes(self.project, [
            {"path": "../outside.txt", "content": "x"},
        ])
        self.assertEqual(len(refusals), 1)

    def test_an_earlier_rollback_conflict_never_locks_the_project(self) -> None:
        from our_harness.changes import FileTransaction, file_sha256
        from our_harness.models import ChangePlan

        target = self.project / "notes.txt"
        target.write_text("before\n", encoding="utf-8")
        saga = swarm_work._MutationSaga(self.project, "old-conflict")
        transaction_id = FileTransaction.new_transaction_id()
        saga.prepare(transaction_id)
        manifest = FileTransaction(self.project).apply([ChangePlan(
            "notes.txt", file_sha256(target), "after\n", reason="test",
        )], transaction_id=transaction_id)
        saga.applied(transaction_id, swarm_work._manifest_sha256(manifest))
        target.write_text("user edit\n", encoding="utf-8")
        self.assertEqual(saga.compensate("user_cancelled")["status"], "rollback_conflict")
        result = self.run_team(
            "Fix src/parser.py", {"claude": [{"path": "src/parser.py", "content": "fixed\n"}]},
        )
        self.assertTrue(result["goal_complete"], result["remaining"])
        notice = " ".join(result["prior_run_notices"])
        self.assertIn("notes.txt", notice)
        self.assertIn("old-conflict.json", notice)
        self.assertEqual(target.read_text(encoding="utf-8"), "user edit\n")
        journal = json.loads(saga.path.read_text(encoding="utf-8"))
        self.assertEqual(journal["phase"], "conflict_acknowledged")
        self.assertEqual(swarm_work._MutationSaga.recover_orphans(self.project), [])

    def test_a_dead_run_keeps_applied_work_and_restores_only_a_half_applied_one(self) -> None:
        from our_harness.changes import FileTransaction, file_sha256
        from our_harness.models import ChangePlan

        done = self.project / "done.txt"
        done.write_text("before\n", encoding="utf-8")
        half = self.project / "half.txt"
        half.write_text("before\n", encoding="utf-8")
        saga = swarm_work._MutationSaga(self.project, "crashed-run")
        applied_id = FileTransaction.new_transaction_id()
        saga.prepare(applied_id)
        manifest = FileTransaction(self.project).apply([ChangePlan(
            "done.txt", file_sha256(done), "after\n", reason="test",
        )], transaction_id=applied_id)
        saga.applied(applied_id, swarm_work._manifest_sha256(manifest))
        half_id = FileTransaction.new_transaction_id()
        saga.prepare(half_id)
        FileTransaction(self.project).prepare([ChangePlan(
            "half.txt", file_sha256(half), "partial\n", reason="test",
        )], transaction_id=half_id)
        swarm_work._active_mutation_sagas.pop("crashed-run", None)
        finished = subprocess.Popen([sys.executable, "-c", "pass"])
        finished.wait(10)
        value = json.loads(saga.path.read_text(encoding="utf-8"))
        value["owner_pid"] = finished.pid
        saga.path.write_text(json.dumps(value), encoding="utf-8")
        recovered = swarm_work._MutationSaga.recover_orphans(self.project)
        self.assertEqual(recovered[0]["status"], "recovered")
        self.assertEqual(recovered[0]["kept_transaction_ids"], [applied_id])
        self.assertEqual(done.read_text(encoding="utf-8"), "after\n")
        self.assertEqual(half.read_text(encoding="utf-8"), "before\n")
        self.assertEqual(swarm_work._MutationSaga.recover_orphans(self.project), [])

    @unittest.skipUnless(os.name == "nt", "Windows access-denied process handles")
    def test_an_access_denied_owner_keeps_only_a_fresh_lease(self) -> None:
        alive = swarm_work._MutationSaga._owner_alive
        self.assertTrue(alive(4))
        self.assertTrue(alive(4, "birth-token", time.time()))
        self.assertFalse(alive(4, "birth-token", time.time() - 3 * 60 * 60))
        self.assertFalse(alive(4, "birth-token", "not a time"))

    # -- review round 2 (F1-F8) ------------------------------------------------

    def _scripted_run(self, execution, verification, **kwargs):
        calls: list[tuple[str, str]] = []
        contexts: list[str] = []

        def answer(_config, route, _text, **inner):
            response_format = inner.get("response_format")
            calls.append((route, getattr(response_format, "name", "")))
            contexts.append(str(inner.get("context") or ""))
            if response_format is swarm_work.EXECUTION_FORMAT:
                value = execution(route, calls)
            elif response_format is swarm_work.WORK_VERIFICATION_FORMAT:
                value = verification(route, calls)
            else:
                value = self._default_value(response_format, route)
            if isinstance(value, Exception):
                raise value
            return {"text": value if isinstance(value, str) else json.dumps(value), "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            result = swarm_work.work_together(
                self.config, self.board, "agent-1", kwargs.pop("goal", "Create report.txt"), **kwargs,
            )
        return result, calls, contexts

    def test_f1_an_unreadable_dissent_blocks_completion_and_is_shown(self) -> None:
        def execution(route, _calls):
            return {"reply": "done", "changes": [{"path": "report.txt", "content": "wrong\n"}]} \
                if route == "claude" else {"reply": "ok", "changes": []}

        def verification(route, _calls):
            if route == "codex":
                return chat.StructuredReplyError(
                    "returned malformed nexus_board_work_verification_v1 JSON twice: "
                    "NO - report.txt is wrong, NOT complete"
                )
            return {"goal_complete": True, "feedback": "Looks done.", "remaining": []}

        result, calls, contexts = self._scripted_run(execution, verification, round_limit=2)
        # Two passes, and the dissent blocked both: no completion.
        self.assertFalse(result["goal_complete"])
        self.assertEqual(result["stopped_because"], "round_limit")
        self.assertTrue(any("could not be read" in one for one in result["remaining"]))
        self.assertTrue(any("NOT complete" in one for one in contexts))
        # After it failed the format three passes in a row it abstains (the
        # team is not held forever), and the reply says it was not counted.
        result, _calls, _contexts = self._scripted_run(execution, verification, round_limit=5)
        self.assertTrue(result["goal_complete"])
        self.assertEqual(result["work_passes"], 3)
        self.assertIn("Not counted", result["answer"]["text"])
        self.assertNotIn("all agreed", result["answer"]["text"])

    def test_f2_malformed_tool_calls_spend_the_call_allowance(self) -> None:
        self.config.data.setdefault("workflow", {})["max_tool_calls"] = 5
        for bad in ({"arguments": {}}, "read_file README.md", ["x"]):
            with self.subTest(bad=bad):
                def execution(route, calls, bad=bad):
                    return {"reply": "looking", "changes": [], "tool_calls": [bad]}

                def verification(_route, _calls):
                    return {"goal_complete": True, "feedback": "ok", "remaining": []}

                result, calls, _contexts = self._scripted_run(execution, verification, round_limit=1)
                work_asks = [one for one in calls if one[1] == "nexus_board_file_work_v2"]
                # Each executor: 5 counted calls, a told-once limit, then its
                # turn ends; far from unbounded.
                self.assertLessEqual(len(work_asks), 2 * 8, len(work_asks))
                self.assertIn("context_tool_unrunnable_call", self.ledger_phases(result))
        self.assertEqual(swarm_work.MOST_TOOL_ROUNDS_PER_TURN, 200)

    def test_f3_ping_pong_edits_stop_as_a_cycle_keeping_the_work(self) -> None:
        counter = {"n": 0}

        def execution(route, _calls):
            if route != "claude":
                return {"reply": "ok", "changes": []}
            counter["n"] += 1
            return {"reply": "edit", "changes": [{"path": "report.txt", "content": "AB"[counter["n"] % 2] + "\n"}]}

        def verification(_route, calls):
            return {"goal_complete": False, "feedback": "not yet",
                    "remaining": [f"Still need report.txt (check {len(calls)})"]}

        result, _calls, _contexts = self._scripted_run(execution, verification, round_limit=None)
        self.assertEqual(result["stopped_because"], "no_change_guard")
        self.assertEqual(result["work_passes"], 5)
        self.assertTrue(any("kept returning to a project state" in one for one in result["remaining"]))
        self.assertTrue((self.project / "report.txt").exists())
        self.assertEqual(result["mutation_recovery"]["status"], "kept")
        self.assertTrue(result["resume_token"])
        # Rewording the same blocker is not a new outcome.
        guard = swarm_work._NoChangeGuard(rounds=4)
        self.assertEqual([guard.stuck(["same outcome"]) for _ in range(4)], [False, False, False, True])
        self.assertEqual(guard.reason, "no_new_outcome")
        # New outcomes keep a run going; a single return to an old state is fine.
        moving = swarm_work._NoChangeGuard(rounds=4, detect_cycles=True)
        self.assertEqual({moving.stuck([n]) for n in range(30)}, {False})
        self.assertFalse(moving.stuck([3]))

    def test_f4_a_conflict_is_acknowledged_only_after_its_notice_is_recorded(self) -> None:
        from our_harness.changes import FileTransaction, file_sha256
        from our_harness.models import ChangePlan

        saga = swarm_work._MutationSaga(self.project, "f4-conflict")
        paths = {}
        for name in ("c", "a", "b"):  # oldest to newest
            target = self.project / f"{name}.txt"
            target.write_text(f"{name} original\n", encoding="utf-8")
            transaction_id = FileTransaction.new_transaction_id()
            saga.prepare(transaction_id)
            manifest = FileTransaction(self.project).apply([ChangePlan(
                f"{name}.txt", file_sha256(target), f"{name} by agent\n", reason="test",
            )], transaction_id=transaction_id)
            saga.applied(transaction_id, swarm_work._manifest_sha256(manifest))
            paths[name] = target
        paths["a"].write_text("a edited by user\n", encoding="utf-8")
        self.assertEqual(saga.compensate("user_cancelled")["status"], "rollback_conflict")
        # An early stop (no ready peer) must not consume the notice.
        board = copy.deepcopy(self.board)
        board["agents"][1]["ready"] = False
        with self.assertRaises(Exception):
            swarm_work.work_together(self.config, board, "agent-1", "Fix src/parser.py")
        self.assertEqual(json.loads(saga.path.read_text(encoding="utf-8"))["phase"], "rollback_conflict")
        reported = swarm_work._MutationSaga.recover_orphans(self.project)[0]
        self.assertEqual(reported["rolled_back_files"], ["b.txt"])
        self.assertEqual(sorted(reported["still_applied_files"]), ["a.txt", "c.txt"])
        self.assertIn("already undone: b.txt", reported["message"])
        self.assertNotIn("nothing else was rolled back", reported["message"])
        result = self.run_team("Fix src/parser.py", {"claude": [{"path": "src/parser.py", "content": "fixed\n"}]})
        self.assertTrue(any("b.txt" in one and "c.txt" in one for one in result["prior_run_notices"]))
        self.assertEqual(json.loads(saga.path.read_text(encoding="utf-8"))["phase"], "conflict_acknowledged")

    def test_f5_quoted_json_in_prose_is_not_the_reply_and_labels_are_explicit(self) -> None:
        prose = (
            'The format asks for {"goal_complete": true, "feedback": "", "remaining": []} '
            "when done. It is NOT done: report.txt is wrong."
        )
        self.assertTrue(swarm_work.WORK_VERIFICATION_FORMAT.received_problem(prose))
        ending = "Here is my verdict:\n" + json.dumps({"goal_complete": False, "feedback": "no", "remaining": ["x"]})
        self.assertEqual(swarm_work.WORK_VERIFICATION_FORMAT.received_problem(ending), "")
        two_fences = (
            "Example:\n```json\n{\"goal_complete\": true, \"feedback\": \"\", \"remaining\": []}\n```\n"
            "Actual:\n```json\n{\"goal_complete\": false, \"feedback\": \"no\", \"remaining\": [\"x\"]}\n```"
        )
        decoded = swarm_work._decode({"text": two_fences}, "Codex", swarm_work.WORK_VERIFICATION_FORMAT)
        self.assertFalse(decoded["goal_complete"])
        self.assertEqual(swarm_work._blocking_remaining([
            "Could not run the test suite", "May still crash", "Note: 3 tests fail",
            "Minor bug: login returns 500", "Later steps are not implemented", "Done",
            "Optional: add docs", "Advisory: rename", "Non-blocking: tidy", "Follow-up: CI",
            "None", "N/A",
        ]), [
            "Could not run the test suite", "May still crash", "Note: 3 tests fail",
            "Minor bug: login returns 500", "Later steps are not implemented", "Done",
        ])

    def test_f7_team_plans_in_execution_prompts_are_bounded(self) -> None:
        long_plan = "p" * (swarm_work.PROMPT_TRANSCRIPT_CHARACTERS // 2)
        contexts: list[str] = []

        def answer(_config, route, _text, **kwargs):
            response_format = kwargs.get("response_format")
            if response_format in (swarm_work.PLAN_FORMAT, swarm_work.PLAN_REVIEW_FORMAT):
                value = self._default_value(response_format, route)
                value["contribution"] = long_plan + route
            elif response_format is swarm_work.EXECUTION_FORMAT:
                contexts.append(str(kwargs.get("context") or ""))
                value = {"reply": "ok", "changes": []}
            else:
                value = self._default_value(response_format, route)
            return {"text": json.dumps(value), "milliseconds": 1, "model": route}

        agents = [dict(self.board["agents"][0]), dict(self.board["agents"][1])] + [
            {"id": f"peer-{n}", "name": f"Peer {n}", "who": "codex", "job": "review", "ready": True}
            for n in range(3)
        ]
        board = {
            "agents": agents, "projects": self.board["projects"],
            "works_on": [{"agent": one["id"], "project": "project-1"} for one in agents],
            "talks_to": [{"one": "agent-1", "other": one["id"]} for one in agents[1:]],
        }
        with mock.patch.object(chat, "ask_once", side_effect=answer):
            swarm_work.work_together(self.config, board, "agent-1", "Fix src/parser.py", round_limit=1)
        section = contexts[0].split("CURRENT TEAM PLANS\n", 1)[1].split("\n\nACTUAL PROJECT TREE NOW", 1)[0]
        self.assertLessEqual(len(section), swarm_work.PROMPT_TRANSCRIPT_CHARACTERS + 10_000)

    def test_f8_long_executor_tool_loops_heartbeat_the_saga(self) -> None:
        touched: list[int] = []
        original = swarm_work._MutationSaga.touch

        def touch(saga):
            touched.append(1)
            return original(saga)

        def execution(route, calls):
            asks = sum(1 for one in calls if one[1] == "nexus_board_file_work_v2")
            if route == "claude" and asks <= 4:
                return {"reply": "look", "changes": [], "tool_calls": [
                    {"name": "list_tree", "arguments": {}},
                ]}
            return {"reply": "ok", "changes": []}

        with mock.patch.object(swarm_work._MutationSaga, "touch", touch):
            result, _calls, _contexts = self._scripted_run(
                execution, lambda *_: {"goal_complete": True, "feedback": "ok", "remaining": []},
                round_limit=1,
            )
        self.assertTrue(result["goal_complete"], result["remaining"])
        # One heartbeat per executor ask/tool round plus one per verifier.
        self.assertGreaterEqual(len(touched), 5 + 1 + 2)

if __name__ == "__main__":
    unittest.main()
