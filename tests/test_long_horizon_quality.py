"""Runtime deliverables require execution, not just agreeing on existing files."""

from __future__ import annotations

import copy
import json
import os
import sys
import unittest
from unittest import mock

from our_harness import goal_verification, long_horizon
from our_harness.playwright_runtime import discover_bundled_playwright_runtime
from our_harness.windows_containment import appcontainer_available
from tests import test_long_horizon_verification_policy as policy_tests


class LongHorizonQualityTests(unittest.TestCase):
    create = policy_tests.LongHorizonVerificationPolicyTests.create
    finish_tasks = policy_tests.LongHorizonVerificationPolicyTests.finish_tasks
    verify = policy_tests.LongHorizonVerificationPolicyTests.verify

    def setUp(self):
        policy_tests.LongHorizonVerificationPolicyTests.setUp(self)
        (self.project / "index.html").write_text(
            "<!doctype html><button onclick='missingFunction()'>Play</button>", encoding="utf-8",
        )

    def test_untested_broken_interaction_cannot_complete_from_agreement_or_file_refs(self):
        for index, refs in enumerate((["verified-no-change"], ["file:index.html"])):
            with self.subTest(refs=refs):
                goal = self.finish_tasks(self.create(f"broken-interaction-{index}"), refs=refs)
                result = self.verify(goal)
                self.assertNotEqual(result["status"], "complete")
                self.assertEqual(result["verification"]["basis"], "runtime_verification_required")
                self.assertEqual(result["verification"]["commands"], [])
                repairs = [task for task in result["tasks"] if task.get("kind") == "repair"]
                self.assertEqual(len(repairs), 1)
                self.assertEqual(repairs[0]["state"], "ready")
                self.assertIn("meaningful automated checks", repairs[0]["description"])
                self.runtime.store.control(goal["goal_id"], "cancel")

    def test_passing_checks_do_not_prove_a_missing_claimed_file_was_delivered(self):
        goal = self.finish_tasks(self.create("missing-delivery"), refs=["file:index.html"])
        (self.project / "index.html").unlink()
        result = self.runtime.store.complete_verification(goal["goal_id"], {"status": "passed"})
        self.assertNotEqual(result["status"], "complete")
        self.assertEqual(result["verification"]["basis"], "missing_deliverable_files")
        self.assertEqual(result["verification"]["missing_files"], ["index.html"])
        self.assertTrue(any(t["kind"] == "repair" and t["state"] == "ready" for t in result["tasks"]))

    def test_passing_checks_and_existing_file_produce_an_exact_delivery_receipt(self):
        goal = self.finish_tasks(self.create("existing-delivery"), refs=["file:index.html"])
        result = self.runtime.store.complete_verification(goal["goal_id"], {"status": "passed"})
        self.assertEqual(result["status"], "complete", result["note"])
        self.assertEqual(result["delivery_receipt"]["files"][0]["path"], "index.html")
        reopened = self.runtime.store.get(goal["goal_id"])
        self.assertEqual(reopened["delivery_receipt"], result["delivery_receipt"])

    def test_final_boundary_rejects_snapshot_even_if_context_check_claims_not_configured(self):
        goal = self.finish_tasks(self.create())
        tree, _ = long_horizon.swarm_work._project_tree_merkle(self.project)
        result = self.runtime.store.complete_verification(goal["goal_id"], {
            "status": "not_configured", "basis": "no_selected_checks", "commands": [],
            "verification_profile": goal_verification.SHARED_GOAL_PROFILE,
            "check_policy": copy.deepcopy(goal_verification.CHECK_POLICY),
            "verification_session_id": goal["goal_id"], "current_tree_merkle": tree,
        })
        self.assertNotEqual(result["status"], "complete")
        self.assertEqual(result["verification"]["basis"], "runtime_verification_required")

    def test_repeating_unsupported_completion_has_a_bounded_repair_limit(self):
        goal = self.create()
        for _ in range(long_horizon.MAX_NO_PROGRESS + 1):
            goal = self.verify(self.finish_tasks(goal))
            if goal["status"] == "paused":
                break
        self.assertEqual(goal["status"], "paused", goal["note"])
        self.assertNotEqual(goal["verification"]["status"], "passed")
        self.assertLessEqual(
            len([task for task in goal["tasks"] if task.get("kind") == "repair"]),
            long_horizon.MAX_NO_PROGRESS,
        )

    def test_restart_and_legacy_contract_preserve_command_authority_but_not_old_no_check_bypass(self):
        goal = self.finish_tasks(self.create())
        original_binding = long_horizon._context_binding(goal)

        def legacy_contract(document, _db):
            held = document["verification_contract"]
            held.pop("fingerprint_sha256")
            held.update({"schema_version": 3, "check_policy": goal_verification.PREVIOUS_CHECK_POLICY})
            held["fingerprint_sha256"] = goal_verification._fingerprint(held)

        self.runtime.store._mutate(goal["goal_id"], legacy_contract)
        restarted = long_horizon.GoalStore(self.config).get(goal["goal_id"])
        selected = goal_verification.verification_project(self.config, restarted)
        self.assertEqual(selected["test_commands"], [])
        self.assertNotEqual(original_binding, long_horizon._context_binding(restarted))
        result = self.verify(restarted)
        self.assertNotEqual(result["status"], "complete")
        self.assertEqual(result["verification"]["basis"], "runtime_verification_required")
        persisted = long_horizon.GoalStore(self.config).get(goal["goal_id"])
        self.assertEqual(persisted["verification"]["basis"], "runtime_verification_required")

    def test_read_only_inspection_remains_valid_without_executing_the_broken_app(self):
        with mock.patch.object(long_horizon.swarm_work, "_run_disposable_verification_command") as run:
            result = goal_verification.run_configured_goal_verification(
                self.config, self.project, self.board["projects"][0],
                "Inspect index.html and explain why Play fails. Do not change any files.", [],
                require_changes=False,
            )
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["basis"], "read_only_zero_write")
        run.assert_not_called()

    def test_static_no_op_in_runtime_project_is_confined_to_the_named_deliverable(self):
        (self.project / "guide.md").write_text("# Controls\nPress Play to start.\n", encoding="utf-8")
        with mock.patch.object(long_horizon.swarm_work, "_verification_commands", return_value=([], "discovered")):
            result = goal_verification.run_configured_goal_verification(
                self.config, self.project, self.board["projects"][0],
                "Update guide.md with the controls; if it is already correct leave it unchanged.", [],
                require_changes=False,
            )
        self.assertEqual(result["status"], "not_configured", result)

    def test_incidental_prose_edit_and_library_name_do_not_hide_runtime_deliverables(self):
        (self.project / "notes.md").write_text("Done", encoding="utf-8")
        for goal in (
            "Make a working browser game", "Make a game using Three.js",
            "Create an app and a user guide", "Create a guide and implement a game",
            "Build a website with a style guide", "Create a game and write controls documentation",
        ):
            with self.subTest(goal=goal), mock.patch.object(
                long_horizon.swarm_work, "_verification_commands", return_value=([], "discovered"),
            ):
                result = goal_verification.run_configured_goal_verification(
                    self.config, self.project, self.board["projects"][0], goal, ["notes.md"],
                )
                self.assertEqual(result["basis"], "runtime_verification_required", result)
                self.assertTrue(result["runtime_requested"], result)

    def test_documentation_action_scope_does_not_require_tests_for_unrelated_runtime(self):
        (self.project / "docs").mkdir()
        (self.project / "docs" / "controls.md").write_text("Press Launch to start.\n", encoding="utf-8")
        (self.project / "game.js").write_text("function start() {}\n", encoding="utf-8")
        for goal in (
            "Update the controls documentation for the game",
            "Create a guide for the app",
            "Create an app user guide",
            "Build a website style guide",
            "Create an app user guide based on the design documentation",
            "Build a website style guide according to the plan",
            "Build an in-app guide",
        ):
            for changed in (["docs/controls.md"], []):
                with self.subTest(goal=goal, changed=changed), mock.patch.object(
                    long_horizon.swarm_work, "_verification_commands", return_value=([], "discovered"),
                ), mock.patch.object(long_horizon.swarm_work, "_run_disposable_verification_command") as run:
                    result = goal_verification.run_configured_goal_verification(
                        self.config, self.project, self.board["projects"][0], goal, changed,
                        require_changes=bool(changed),
                    )
                    self.assertEqual(result["status"], "not_configured", result)
                    self.assertEqual(result["basis"], "no_selected_checks", result)
                    run.assert_not_called()

    def test_runtime_reference_modifiers_do_not_exempt_the_requested_runtime_object(self):
        (self.project / "README.md").write_text("# Game design\nPlayers launch and collect rewards.\n", encoding="utf-8")
        (self.project / "notes.md").write_text("The plan was reviewed.\n", encoding="utf-8")
        (self.project / "game.js").write_text("// Gameplay is not implemented.\n", encoding="utf-8")
        cases = (
            ("Build the game described in README.md", []),
            ("Create a game based on the design documentation", ["notes.md"]),
            ("Implement the app according to the plan", ["notes.md"]),
            ("Build a game specified by the guide", ["notes.md"]),
            ("Create an app from the design notes", ["notes.md"]),
            ("Implement the game following the README.md instructions", ["notes.md"]),
            ("Create an in-browser game", ["notes.md"]),
        )
        for goal, changed in cases:
            with self.subTest(goal=goal), mock.patch.object(
                long_horizon.swarm_work, "_verification_commands", return_value=([], "discovered"),
            ):
                result = goal_verification.run_configured_goal_verification(
                    self.config, self.project, self.board["projects"][0], goal, changed,
                    require_changes=bool(changed),
                )
                self.assertEqual(result["status"], "failed", result)
                self.assertEqual(result["basis"], "runtime_verification_required", result)
                self.assertTrue(result["runtime_requested"], result)

    def test_documentation_wording_cannot_exempt_actual_executable_changes(self):
        (self.project / "game.js").write_text("function start() {}\n", encoding="utf-8")
        with mock.patch.object(long_horizon.swarm_work, "_verification_commands", return_value=([], "discovered")):
            result = goal_verification.run_configured_goal_verification(
                self.config, self.project, self.board["projects"][0],
                "Create an app user guide", ["game.js"],
            )
        self.assertEqual(result["basis"], "runtime_verification_required", result)
        self.assertIn("game.js", result["runtime_paths"])

    def test_placeholder_only_game_cannot_substitute_for_a_working_runtime(self):
        (self.project / "index.html").write_text("<h1>Game coming soon</h1>", encoding="utf-8")
        for objective in (
            "Create a detailed 3js game in a new folder",
            "Please make a working app for the team",
            "Build a website where readers can search entries",
        ):
            with self.subTest(objective=objective), mock.patch.object(
                long_horizon.swarm_work, "_verification_commands", return_value=([], "discovered"),
            ):
                result = goal_verification.run_configured_goal_verification(
                    self.config, self.project, self.board["projects"][0], objective, [], require_changes=False,
                )
                self.assertEqual(result["basis"], "runtime_verification_required", result)
                self.assertTrue(result["runtime_requested"])
                self.assertEqual(result["runtime_paths"], [])
        for objective in (
            "Write documentation about the game",
            "Create a guide for the app",
            "Explain how to build a game; do not change files",
        ):
            with self.subTest(objective=objective):
                self.assertFalse(goal_verification.requested_runtime_verification(objective))

    def test_actual_behavior_tests_pass_existing_implementation_and_fail_broken_behavior(self):
        (self.project / "game.py").write_text("def collect(score): return score + 1\n", encoding="utf-8")
        (self.project / "test_game.py").write_text(
            "import unittest\nfrom game import collect\n"
            "class GameTests(unittest.TestCase):\n"
            " def test_collection_updates_score(self):\n  self.assertEqual(collect(2), 3)\n",
            encoding="utf-8",
        )
        project = {**self.board["projects"][0], "test_commands": [[sys.executable, "-m", "unittest", "test_game"]]}
        arguments = (self.config, self.project, project, "Make game.py collect one score per pickup", [])
        result = goal_verification.run_configured_goal_verification(*arguments, require_changes=False)
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(result["verification_analysis"]["verification_evidence"][0]["executed"], 1)
        (self.project / "game.py").write_text("def collect(score): return score\n", encoding="utf-8")
        result = goal_verification.run_configured_goal_verification(*arguments, require_changes=False)
        self.assertEqual(result["status"], "failed", result)

    def test_missing_checks_can_be_authored_and_discovered_without_bypassing_approval(self):
        goal = self.finish_tasks(self.create())
        result = self.verify(goal)
        self.assertEqual(result["verification"]["basis"], "runtime_verification_required")
        (self.project / "package.json").write_text(json.dumps({
            "name": "portable-fixture", "scripts": {"test": "node --test game.test.cjs"},
        }), encoding="utf-8")
        with mock.patch.object(long_horizon.swarm_work, "_run_disposable_verification_command") as run:
            checked = goal_verification.run_configured_goal_verification(
                self.config, self.project, self.board["projects"][0], goal["objective"], [],
                require_changes=False,
            )
        self.assertEqual(checked["status"], "unavailable", checked)
        self.assertEqual(checked["basis"], "discovered_command_approval_required")
        self.assertEqual(checked["proposed_commands"], [["npm", "run", "test"]])
        run.assert_not_called()

    def test_module_game_cannot_claim_direct_file_launch_without_an_executed_check(self):
        (self.project / "index.html").write_text(
            '<!doctype html><button id="launch">Launch</button><p id="status">Waiting</p>'
            '<script type="module" src="./game.js"></script>', encoding="utf-8",
        )
        (self.project / "game.js").write_text(
            "document.querySelector('#launch').onclick = () => {"
            "document.querySelector('#status').textContent = 'Running';};", encoding="utf-8",
        )
        with mock.patch.object(long_horizon.swarm_work, "_verification_commands", return_value=([], "discovered")):
            result = goal_verification.run_configured_goal_verification(
                self.config, self.project, self.board["projects"][0],
                "Create a game that launches by double-clicking index.html directly from disk", [],
                require_changes=False,
            )
        self.assertEqual(result["basis"], "runtime_verification_required", result)
        # Existing HTTP-only browser evidence must never be relabelled as a
        # successful file-origin run: local modules have different CORS rules.
        source = self.browser_spec("file:///index.html")
        self.assertIsNone(long_horizon.swarm_work._playwright_static_browser_scenario(source))

    @staticmethod
    def browser_spec(route="/index.html"):
        return (
            "const {test, expect} = require('@playwright/test');\n"
            "test('Launch starts the application', async ({page}) => {\n"
            f" await page.goto('{route}');\n"
            " await page.locator('#launch').click();\n"
            " await expect(page.locator('#status')).toHaveText('Running');\n"
            "});\n"
        )

    def test_supported_browser_recipe_compiles_to_real_route_click_and_state_assertion(self):
        scenario = long_horizon.swarm_work._playwright_static_browser_scenario(self.browser_spec())
        self.assertEqual(scenario["route"], "/index.html")
        self.assertEqual(scenario["actions"], [{"action": "click", "selector": "#launch"}])
        self.assertEqual(scenario["expected"], {"kind": "text", "selector": "#status", "value": "Running"})

    @unittest.skipUnless(os.name == "nt", "The product's brokered local browser uses Windows AppContainer")
    def test_real_contained_browser_check_rejects_inert_launch_and_accepts_working_production_handler(self):
        if not discover_bundled_playwright_runtime() or not appcontainer_available():
            self.skipTest("The bundled browser or AppContainer is unavailable")
        (self.project / "index.html").write_text(
            '<!doctype html><button id="launch">Launch</button><p id="status">Waiting</p>'
            '<script src="./game.js"></script>', encoding="utf-8",
        )
        (self.project / "game.js").write_text("// The production launch handler is missing.\n", encoding="utf-8")
        (self.project / "launch.spec.cjs").write_text(self.browser_spec(), encoding="utf-8")
        project = {**self.board["projects"][0], "test_commands": [[
            "node", "node_modules/playwright/cli.js", "test", "launch.spec.cjs",
        ]]}
        arguments = (self.config, self.project, project, "Make an application whose Launch button starts it", [])
        broken = goal_verification.run_configured_goal_verification(*arguments, require_changes=False)
        self.assertEqual(broken["status"], "failed", broken)
        self.assertTrue(broken["commands"][0]["brokered_e2e_receipts"])
        (self.project / "game.js").write_text(
            "document.querySelector('#launch').onclick = () => {"
            "document.querySelector('#status').textContent = 'Running';};", encoding="utf-8",
        )
        working = goal_verification.run_configured_goal_verification(*arguments, require_changes=False)
        self.assertEqual(working["status"], "passed", working)
        self.assertEqual(working["verification_analysis"]["verification_evidence"][0]["executed"], 1)

    def test_provider_context_requires_critical_behavior_review_and_names_actual_tool_limits(self):
        goal = self.create()
        task = self.runtime.store.get(goal["goal_id"])["tasks"][0]
        context = self.runtime._agent_context(self.runtime.store.get(goal["goal_id"]), task)
        for expected in (
            "runtime_verification_required", "independently required outcomes", "concrete failure",
            "launch URL/protocol", "representative user interactions", "no commands launched the app",
            "selected project root", "not just assert that source text or files exist",
            "bundled Chromium", "Nexus resolves its bundled runtime", "does not verify file://",
        ):
            self.assertIn(expected, context)


if __name__ == "__main__":
    unittest.main()
