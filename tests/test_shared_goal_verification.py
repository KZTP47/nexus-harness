from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from our_harness import goal_verification, swarm_work
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError


class SharedGoalVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "another user's amber game"
        self.root.mkdir()
        self.authority = self.base / "independent installation"
        self.authority.mkdir()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.authority, [], {})
        self.command = [sys.executable, "-m", "unittest", "discover", "-s", ".", "-p", "test_game.py"]
        self.project = {
            "id": "arbitrary-project", "name": "Amber game", "path": str(self.root),
            "test_commands": [self.command],
        }
        (self.root / "game.py").write_text(
            "def winner(score):\n    return 'amber' if score >= 3 else None\n", encoding="utf-8",
        )
        (self.root / "test_game.py").write_text(
            "import unittest\nfrom pathlib import Path\nfrom game import winner\n\n"
            "class GameTests(unittest.TestCase):\n"
            "    def test_winning_score(self):\n"
            "        self.assertEqual(winner(3), 'amber')\n"
            "        self.assertIsNone(winner(2))\n"
            "        Path('runner-created.txt').write_text('disposable output')\n",
            encoding="utf-8",
        )

    def verify(self, **kwargs):
        return goal_verification.run_configured_goal_verification(
            self.config, self.root, self.project,
            kwargs.pop("goal", "Create a game where players can win the amber arena and verify its scoring."),
            kwargs.pop("changed", ["game.py", "test_game.py"]),
            verification_session_id="portable-goal-verification", **kwargs,
        )

    def result(self, **updates):
        return {
            "argv": self.command, "cwd": ".", "exit_code": 0,
            "stdout": "", "stderr": "Ran 1 test in 0.001s\n\nOK\n",
            "duration_ms": 1, "timed_out": False, "output_truncated": False,
            **updates,
        }

    def test_real_configured_unittest_passes_arbitrary_game_wording_in_disposable_project(self):
        before, _ = swarm_work._project_tree_merkle(self.root)
        with mock.patch.object(
            swarm_work, "_derive_requirement_contract", side_effect=AssertionError("invented requirement gate"),
        ), mock.patch.object(
            swarm_work, "_build_causal_behavior_receipts", side_effect=AssertionError("invented causal probe"),
        ):
            result = self.verify()
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(len(result["commands"]), 1)
        proof = result["verification_analysis"]
        self.assertTrue(proof["passed"], proof)
        self.assertEqual(proof["verification_evidence"][0]["framework"], "unittest")
        self.assertEqual(proof["verification_evidence"][0]["executed"], 1)
        self.assertFalse((self.root / "runner-created.txt").exists())
        self.assertEqual(swarm_work._project_tree_merkle(self.root)[0], before)

    def test_real_broken_scoring_is_rejected(self):
        (self.root / "game.py").write_text("def winner(score):\n    return None\n", encoding="utf-8")
        result = self.verify()
        self.assertEqual(result["status"], "failed", result)
        self.assertNotEqual(result["commands"][0]["exit_code"], 0)

    def test_real_native_node_tests_execute_in_disposable_project_and_reject_bad_game(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not available on this host")
        (self.root / "arena.cjs").write_text(
            "exports.winner = score => score >= 3 ? 'amber' : null;\n", encoding="utf-8",
        )
        (self.root / "arena.test.cjs").write_text(
            "const test = require('node:test');\nconst assert = require('node:assert/strict');\n"
            "const fs = require('node:fs');\nconst {winner} = require('./arena.cjs');\n"
            "test('players can win the amber arena', () => {\n"
            "  assert.equal(winner(3), 'amber');\n  assert.equal(winner(2), null);\n"
            "  fs.writeFileSync('node-runner-created.txt', 'disposable output');\n});\n",
            encoding="utf-8",
        )
        self.project["test_commands"] = [[node, "--test", "arena.test.cjs"]]
        before, _ = swarm_work._project_tree_merkle(self.root)
        result = self.verify(changed=["arena.cjs", "arena.test.cjs"])
        self.assertEqual(result["status"], "passed", result)
        proof = result["verification_analysis"]["verification_evidence"]
        self.assertEqual(proof[0]["executed"], 1)
        self.assertIn("node", proof[0]["framework"].lower())
        self.assertFalse((self.root / "node-runner-created.txt").exists())
        self.assertEqual(swarm_work._project_tree_merkle(self.root)[0], before)
        (self.root / "arena.cjs").write_text("exports.winner = score => null;\n", encoding="utf-8")
        broken = self.verify(changed=["arena.cjs", "arena.test.cjs"])
        self.assertEqual(broken["status"], "failed", broken)
        self.assertNotEqual(broken["commands"][0]["exit_code"], 0)

    def test_zero_tests_truncation_nonzero_timeout_and_unproven_output_cannot_pass(self):
        negatives = {
            "zero_tests": self.result(stderr="Ran 0 tests in 0.000s\n\nOK\n"),
            "truncated": self.result(output_truncated=True),
            "failed": self.result(exit_code=1),
            "timeout": self.result(timed_out=True),
            "no_positive_proof": self.result(stderr="Everything looks good."),
        }
        for name, payload in negatives.items():
            with self.subTest(name=name), mock.patch.object(
                swarm_work, "_run_disposable_verification_command", return_value=payload,
            ) as run:
                result = self.verify()
                self.assertEqual(result["status"], "failed", result)
                run.assert_called_once()

    def test_unavailable_containment_and_attempted_escape_are_not_success(self):
        negatives = (
            (self.result(containment_unavailable=True, exit_code=-2), "unavailable"),
            (self.result(verification_escape_detected=True), "failed"),
            (self.result(stderr="Nexus verification containment denied"), "failed"),
        )
        for payload, expected in negatives:
            with self.subTest(payload=payload), mock.patch.object(
                swarm_work, "_run_disposable_verification_command", return_value=payload,
            ):
                result = self.verify()
                self.assertEqual(result["status"], expected, result)
                self.assertTrue("containment" in result["basis"] or "escape" in result["basis"], result)

    def test_real_project_tree_drift_refuses_otherwise_positive_results(self):
        def changed_during_execution(*_args, **_kwargs):
            (self.root / "outside-copy.txt").write_text("unexpected original-tree write", encoding="utf-8")
            return self.result()
        with mock.patch.object(
            swarm_work, "_run_disposable_verification_command", side_effect=changed_during_execution,
        ):
            result = self.verify()
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("escape", result["basis"])

    def test_no_command_and_unapproved_discovery_never_execute(self):
        for commands in ([], [self.command]):
            with self.subTest(commands=commands), mock.patch.object(
                swarm_work, "_verification_commands", return_value=(commands, "discovered"),
            ), mock.patch.object(swarm_work, "_run_disposable_verification_command") as run:
                result = self.verify()
                self.assertEqual(result["status"], "unavailable", result)
                run.assert_not_called()
                if commands:
                    self.assertEqual(result["basis"], "discovered_command_approval_required")

    def test_exact_approved_discovery_runs_but_manifest_change_invalidates_approval(self):
        manifest = self.root / "pyproject.toml"
        manifest.write_text("[project]\nname = 'amber-game'\nversion = '1.0'\n", encoding="utf-8")
        self.project["approved_test_command_digest"] = swarm_work._command_approval_digest(
            self.root, [self.command], declared_path=str(self.root),
        )
        with mock.patch.object(
            swarm_work, "_verification_commands", return_value=([self.command], "discovered"),
        ), mock.patch.object(
            swarm_work, "_run_disposable_verification_command", return_value=self.result(),
        ) as run:
            self.assertEqual(self.verify()["status"], "passed")
            run.assert_called_once()
            manifest.write_text("[project]\nname = 'amber-game'\nversion = '2.0'\n", encoding="utf-8")
            run.reset_mock()
            result = self.verify()
            self.assertEqual(result["status"], "unavailable", result)
            run.assert_not_called()

    def test_explicit_protected_path_violation_is_rejected_without_execution(self):
        (self.root / "settings.json").write_text("{}\n", encoding="utf-8")
        with mock.patch.object(swarm_work, "_run_disposable_verification_command") as run:
            result = self.verify(
                goal="Update game.py; do not modify settings.json.",
                changed=["game.py", "settings.json"],
            )
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("protected", result["basis"])
        run.assert_not_called()

    def test_preservation_scope_does_not_forbid_the_requested_game_edit(self):
        for goal in [
            "Keep settings.json unchanged while updating game.py.",
            "Preserve settings.json and update game.py.",
            "Do not modify settings.json but update game.py.",
        ]:
            with self.subTest(goal=goal), mock.patch.object(
                swarm_work, "_run_disposable_verification_command", return_value=self.result(),
            ) as run:
                allowed = self.verify(goal=goal, changed=["game.py"])
                self.assertEqual(allowed["status"], "passed", allowed)
                run.assert_called_once()
                run.reset_mock()
                prohibited = self.verify(goal=goal, changed=["settings.json"])
                self.assertEqual(prohibited["basis"], "protected_path", prohibited)
                run.assert_not_called()

    def test_project_wide_prohibition_exception_only_allows_its_named_target(self):
        goal = "Do not modify any files except to update game.py."
        with mock.patch.object(
            swarm_work, "_run_disposable_verification_command", return_value=self.result(),
        ) as run:
            allowed = self.verify(goal=goal, changed=["game.py"])
            self.assertEqual(allowed["status"], "passed", allowed)
            run.assert_called_once()
            run.reset_mock()
            prohibited = self.verify(goal=goal, changed=["game.py", "settings.json"])
            self.assertEqual(prohibited["basis"], "protected_path", prohibited)
            run.assert_not_called()

    def test_baseline_checks_can_run_before_edits_but_final_verification_requires_changes(self):
        before, _ = swarm_work._project_tree_merkle(self.root)
        baseline = self.verify(changed=[], require_changes=False)
        self.assertEqual(baseline["status"], "passed", baseline)
        self.assertTrue(baseline["verification_analysis"]["passed"])
        self.assertEqual(before, swarm_work._project_tree_merkle(self.root)[0])
        with mock.patch.object(swarm_work, "_run_disposable_verification_command") as run:
            final = self.verify(changed=[])
        self.assertEqual(final["basis"], "goal_effect", final)
        run.assert_not_called()

    def test_read_only_goal_with_no_changes_preserves_zero_write_verification(self):
        with mock.patch.object(swarm_work, "_run_disposable_verification_command") as run:
            result = self.verify(
                goal="Read game.py and explain how scoring works; do not change any files.", changed=[],
            )
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["commands"], [])
        run.assert_not_called()

    def test_read_only_goal_rejects_recorded_file_changes(self):
        with mock.patch.object(swarm_work, "_run_disposable_verification_command") as run:
            result = self.verify(
                goal="Read game.py and explain how scoring works; do not change any files.", changed=["game.py"],
            )
        self.assertEqual(result["status"], "failed", result)
        run.assert_not_called()

    def test_trusted_json_evidence_contract_supports_an_explicit_custom_runner(self):
        custom = [sys.executable, "quality_check.py"]
        self.project["test_commands"] = [custom]
        self.project["test_evidence_contracts"] = [{
            "command": custom, "total_field": "checks.executed", "failed_field": "checks.failed",
        }]
        with mock.patch.object(
            swarm_work, "_run_disposable_verification_command",
            return_value=self.result(argv=custom, stdout=json.dumps({"checks": {"executed": 4, "failed": 0}}), stderr=""),
        ):
            result = self.verify()
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["verification_analysis"]["verification_evidence"][0]["executed"], 4)

    def test_command_and_evidence_configuration_changes_invalidate_saved_fingerprint(self):
        owning = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        owning.data["project"]["test_commands"] = [self.command]
        owning.data["project"]["test_evidence_contracts"] = [{
            "command": self.command, "total_field": "total", "failed_field": "failed",
        }]
        contract = goal_verification.capture_verification_contract(owning, self.project, self.root)
        saved = {"project": self.project, "objective": "Build the amber game", "verification_contract": contract}
        goal_verification.verification_project(owning, saved)
        changed = copy.deepcopy(owning.data)
        changed["project"]["test_evidence_contracts"][0]["total_field"] = "different_total"
        other = LoadedConfig(changed, self.root, [], {})
        updated_contract = goal_verification.capture_verification_contract(other, self.project, self.root)
        self.assertNotEqual(contract["fingerprint_sha256"], updated_contract["fingerprint_sha256"])
        with self.assertRaisesRegex(HarnessError, "changed|evidence|verification"):
            goal_verification.verification_project(other, saved)
        changed_commands = copy.deepcopy(owning.data)
        changed_commands["project"]["test_commands"] = [[sys.executable, "-m", "unittest", "test_other"]]
        with self.assertRaisesRegex(HarnessError, "changed|verification"):
            goal_verification.verification_project(LoadedConfig(changed_commands, self.root, [], {}), saved)


if __name__ == "__main__":
    unittest.main()
