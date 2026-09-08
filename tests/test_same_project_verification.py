"""Owned goal copies retain exact selected-project verification authority."""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from our_harness import goal_verification, goal_workspaces, swarm_work
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError
from tests import test_long_horizon_verification_policy as policy_tests


class SameProjectVerificationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.source = self.base / "independent source project"
        self.source.mkdir()
        self.installation = self.base / "separate installation"
        self.installation.mkdir()
        self.runtime = self.base / "private runtime"
        environment = mock.patch.dict(os.environ, {"OUR_HARNESS_SWARM_RUN_DIR": str(self.runtime)})
        environment.start()
        self.addCleanup(environment.stop)
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.installation, [], {})
        self.package = {"name": "arbitrary-game", "scripts": {"test": "node --test game.test.cjs"}}
        (self.source / "package.json").write_text(json.dumps(self.package), encoding="utf-8")
        (self.source / "game.cjs").write_text("exports.score = n => n + 1;\n", encoding="utf-8")
        (self.source / "game.test.cjs").write_text(
            "const {test}=require('node:test');const assert=require('node:assert/strict');"
            "const game=require('./game.cjs');test('score changes',()=>assert.equal(game.score(2),3));\n",
            encoding="utf-8",
        )
        self.project = {"id": "selected", "name": "Independent source", "path": str(self.source), "test_commands": []}
        self.commands, source = swarm_work._verification_commands(self.config, self.source, self.project)
        self.assertEqual(source, "discovered")
        self.assertEqual(self.commands, [["npm", "run", "test"]])
        self.project["approved_test_command_digest"] = swarm_work._command_approval_digest(
            self.source, self.commands, declared_path=str(self.source),
        )

    def snapshot(self, name="first-goal"):
        goal = {
            "goal_id": name, "project": copy.deepcopy(self.project),
            "status": "paused", "revision": 4,
            "project_authority_id": "arbitrary-selected-authority", "objective": "Build a scoring game",
            "verification_contract": goal_verification.capture_verification_contract(self.config, self.project, self.source),
        }
        goal["execution_workspace"] = goal_workspaces.create(goal, self.runtime)
        return goal, goal_workspaces.root(goal, self.runtime)

    def run_verification(self, goal, root, *, config=None, project=None):
        selected = project if project is not None else goal_verification.verification_project(self.config, goal)
        return swarm_work._run_selected_project_verification(
            config or self.config, root, selected, goal["objective"], ["game.cjs"], None,
            verification_profile="shared_goal_v1", context_check=True,
        )

    def runner(self):
        def completed(_config, _root, command, **_kwargs):
            output = "Vitest\nTests 1 passed (1)\n" if command[0] == "npm" else (
                "TAP version 13\n1..1\n# tests 1\n# pass 1\n# fail 0\n# skipped 0\n"
            )
            return {"argv": command, "cwd": ".", "exit_code": 0, "stdout": output,
                    "stderr": "", "duration_ms": 1, "timed_out": False, "output_truncated": False}
        patcher = mock.patch.object(swarm_work, "_run_disposable_verification_command", side_effect=completed)
        run = patcher.start()
        self.addCleanup(patcher.stop)
        availability = mock.patch.object(swarm_work, "_containment_owns_runner_availability", return_value=True)
        availability.start()
        self.addCleanup(availability.stop)
        return run

    def test_selected_approval_runs_checks_in_owned_copy_and_survives_reload(self):
        goal, workspace = self.snapshot()
        selected = goal_verification.verification_project(self.config, goal)
        self.assertEqual(selected["path"], str(self.source))
        self.assertEqual(selected["approved_test_command_digest"], self.project["approved_test_command_digest"])
        run = self.runner()
        for held in (goal, json.loads(json.dumps(goal))):
            result = self.run_verification(held, workspace)
            self.assertEqual(result["status"], "passed", result)
            self.assertEqual(run.call_args.args[1], workspace)
        self.assertNotEqual(
            swarm_work._command_approval_digest(workspace, self.commands, declared_path=str(self.source)),
            self.project["approved_test_command_digest"],
        )

    def test_changed_workspace_manifest_requires_new_exact_approval(self):
        goal, workspace = self.snapshot()
        for package in (
            {**self.package, "version": "another-build"},
            {**self.package, "scripts": {"test": "node --test another.test.cjs"}},
        ):
            with self.subTest(package=package), mock.patch.object(swarm_work, "_run_disposable_verification_command") as run:
                (workspace / "package.json").write_text(json.dumps(package), encoding="utf-8")
                result = self.run_verification(goal, workspace)
                self.assertEqual(result["basis"], "discovered_command_approval_required", result)
                self.assertNotEqual(result["approval_digest"], self.project["approved_test_command_digest"])
                run.assert_not_called()

    def test_authority_cannot_be_redirected_to_source_or_another_goal_copy(self):
        goal, workspace = self.snapshot()
        _other, other_workspace = self.snapshot("other-goal")
        selected = goal_verification.verification_project(self.config, goal)
        for wrong_root in (self.source, other_workspace):
            with self.subTest(root=wrong_root), mock.patch.object(swarm_work, "_run_disposable_verification_command") as run:
                with self.assertRaisesRegex(HarnessError, "different execution root"):
                    self.run_verification(goal, wrong_root, project=selected)
                run.assert_not_called()

    def test_json_metadata_cannot_fabricate_snapshot_authority(self):
        goal, workspace = self.snapshot()
        selected = copy.deepcopy(self.project)
        selected["_nexus_workspace_verification"] = {"execution_root": str(workspace), "project_root": str(self.source)}
        with self.assertRaisesRegex(HarnessError, "engine-issued binding"):
            self.run_verification(goal, workspace, project=selected)

    def test_mutated_command_capsule_or_persisted_workspace_binding_is_rejected(self):
        goal, workspace = self.snapshot()
        selected = goal_verification.verification_project(self.config, goal)
        selected["test_commands"] = [["unapproved-runner"]]
        with self.assertRaisesRegex(HarnessError, "commands or selected project authority changed"):
            self.run_verification(goal, workspace, project=selected)
        changed = copy.deepcopy(goal)
        changed["execution_workspace"]["goal_id"] = "another-goal"
        with self.assertRaisesRegex(HarnessError, "authenticated state"):
            goal_verification.verification_project(self.config, changed)

    def test_context_tools_retain_source_config_commands_without_importing_host_commands(self):
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.source, [], {})
        selected_command = ["node", "--test", "game.test.cjs"]
        self.config.data["project"]["test_commands"] = [selected_command]
        self.project["approved_test_command_digest"] = ""
        goal, workspace = self.snapshot()
        selected = goal_verification.verification_project(self.config, goal)
        ledger = swarm_work.CollaborationLedger(self.config, "arbitrary-route", "isolated-context", session_id="context-snapshot")
        tools = swarm_work._ProjectContextTools(
            self.config, workspace, ledger, selected, goal["objective"], [], None,
            verification_profile="shared_goal_v1",
        )
        self.addCleanup(tools.close)
        self.assertEqual(tools.config.get("project.test_commands"), [])
        self.assertEqual(swarm_work._verification_commands(tools.config, workspace, selected), (
            [selected_command], "project_config",
        ))
        run = self.runner()
        result = tools._run_selected_verification({})
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(run.call_args.args[1:3], (workspace, selected_command))
        self.config.data["project"]["test_commands"] = [["changed-check-runner"]]
        with self.assertRaisesRegex(HarnessError, "test commands changed"):
            tools._run_selected_verification({})

    def test_unrelated_installation_commands_do_not_become_snapshot_checks(self):
        self.config.data["project"]["test_commands"] = [["installation-only-command"]]
        goal, workspace = self.snapshot()
        selected = goal_verification.verification_project(self.config, goal)
        rebound = LoadedConfig(copy.deepcopy(self.config.data), workspace, [], {})
        self.assertEqual(swarm_work._verification_commands(rebound, workspace, selected), (self.commands, "discovered"))

    def test_absolute_selected_operands_run_owned_bytes_and_keep_approved_argv(self):
        checks = self.source / "checks"
        checks.mkdir()
        script = checks / "check.py"
        external = self.base / "external toolchain"
        external.mkdir()
        library = external / "toolchain.py"
        library.write_text("external toolchain\n", encoding="utf-8")
        command = [sys.executable, str(script), str(checks), str(self.source), str(library), "relative-input"]
        contract = {"command": command, "format": "json-stdout", "total_field": "total", "failed_field": "failed"}
        for profile in ("shared_goal_v1", "legacy"):
            for settings in ("project", "config"):
                with self.subTest(profile=profile, settings=settings):
                    self.config = LoadedConfig(
                        copy.deepcopy(DEFAULT_CONFIG),
                        self.source if settings == "config" else self.installation, [], {},
                    )
                    self.project["test_commands"] = [command] if settings == "project" else []
                    self.project["test_evidence_contracts"] = [contract] if settings == "project" else []
                    self.project["approved_test_command_digest"] = ""
                    if settings == "config":
                        self.config.data["project"]["test_commands"] = [command]
                        self.config.data["project"]["test_evidence_contracts"] = [contract]
                    script.write_text("selected source version\n", encoding="utf-8")
                    goal, workspace = self.snapshot(profile + "-" + settings)
                    (workspace / "checks" / "check.py").write_text("owned updated version\n", encoding="utf-8")
                    selected = goal_verification.verification_project(self.config, goal)

                    def contained(_config, snapshot, argv, **kwargs):
                        self.assertEqual(argv, [
                            command[0], str(snapshot / "checks" / "check.py"),
                            str(snapshot / "checks"), str(snapshot), str(library), "relative-input",
                        ])
                        self.assertEqual(Path(argv[1]).read_text(encoding="utf-8"), "owned updated version\n")
                        self.assertEqual(kwargs["denied_root"], workspace)
                        return {
                            "argv": list(argv), "cwd": str(snapshot), "exit_code": 0,
                            "stdout": '{"total":1,"failed":0}', "stderr": "", "duration_ms": 1,
                            "timed_out": False, "output_truncated": False,
                        }

                    with mock.patch.object(swarm_work, "_contained_snapshot_command", side_effect=contained) as run:
                        result = swarm_work._run_selected_project_verification(
                            self.config, workspace, selected, "Update game.cjs", ["game.cjs"], None,
                            verification_profile=profile, context_check=True,
                        )
                    self.assertEqual(result["status"], "passed", result)
                    run.assert_called_once()
                    self.assertEqual(result["commands"][0]["argv"], command)
                    self.assertEqual(result["commands"][0]["cwd"], ".")
                    self.assertEqual(selected["test_commands"], [command] if settings == "project" else [])
                    self.assertEqual(script.read_text(encoding="utf-8"), "selected source version\n")
                    self.assertEqual(library.read_text(encoding="utf-8"), "external toolchain\n")

    def test_absolute_selected_operand_setup_failure_keeps_original_evidence(self):
        script = self.source / "check.py"
        script.write_text("pass\n", encoding="utf-8")
        command = [sys.executable, str(script)]
        self.project["test_commands"] = [command]
        goal, workspace = self.snapshot()
        with mock.patch.object(swarm_work, "_contained_snapshot_command", side_effect=OSError("fixture setup failed")):
            result = self.run_verification(goal, workspace)
        self.assertEqual(result["basis"], "verification_containment_unavailable", result)
        self.assertEqual(result["commands"][0]["argv"], command)

    def test_explicit_runtime_root_remains_bound_when_default_runtime_changes(self):
        goal, workspace = self.snapshot()
        with mock.patch("our_harness.swarm_runs._base", return_value=self.base / "different runtime"):
            selected = goal_verification.verification_project(self.config, goal, runtime_root=self.runtime)
            self.assertEqual(swarm_work._verification_commands(self.config, workspace, selected), (self.commands, "discovered"))

    def test_goal_previews_discover_unpublished_checks_and_bind_each_snapshot(self):
        first, first_root = self.snapshot("first-preview")
        second, second_root = self.snapshot("second-preview")
        for index, workspace in enumerate((first_root, second_root)):
            package = {**self.package, "scripts": {"test": f"node --test scenario-{index}.test.cjs"}}
            (workspace / "package.json").write_text(json.dumps(package), encoding="utf-8")
        proposals = [goal_verification.workspace_verification_approval(
            self.config, goal, runtime_root=self.runtime,
        ) for goal in (first, second)]
        self.assertNotEqual(proposals[0]["approval_digest"], proposals[1]["approval_digest"])
        for goal, proposal in zip((first, second), proposals):
            self.assertEqual(proposal["goal_id"], goal["goal_id"])
            self.assertEqual(proposal["revision"], 4)
            self.assertEqual(proposal["project_path"], str(self.source))
            self.assertTrue(proposal["can_approve"])
            self.assertFalse(proposal["approved"])
        board_proposal = swarm_work.verification_command_approval(self.config, self.project)
        self.assertTrue(board_proposal["approved"])
        self.assertNotIn(board_proposal["approval_digest"], [one["approval_digest"] for one in proposals])

    def test_preview_requires_a_paused_settled_goal(self):
        goal, _workspace = self.snapshot()
        for status, tasks in (("running", []), ("paused", [{"state": "running"}])):
            with self.subTest(status=status, tasks=tasks):
                goal.update(status=status, tasks=tasks)
                proposal = goal_verification.workspace_verification_approval(self.config, goal, runtime_root=self.runtime)
                self.assertFalse(proposal["can_approve"])


class WorkspaceApprovalHTTPTests(unittest.TestCase):
    def setUp(self):
        from our_harness import server as harness_server

        self.fixture = policy_tests.LongHorizonVerificationPolicyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runtime = self.fixture.runtime
        self.panel = harness_server.HarnessHTTPServer(("127.0.0.1", 0), self.fixture.config)
        self.panel._long_horizon = self.runtime
        self.addCleanup(self.panel.server_close)
        thread = threading.Thread(target=self.panel.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.panel.shutdown)

    def create_goal(self, request):
        goal = self.runtime.store.create(
            self.fixture.board, "tiny-game", ["Build a game with a scoring check"], request,
            lead_id="creator", participant_ids=["creator", "reviewer"],
            conversation_id="chat-" + request, isolated_workspace=True,
        )
        workspace = goal_workspaces.root(goal, self.runtime.store.root)
        (workspace / "package.json").write_text(json.dumps({
            "name": "independent-goal", "scripts": {"test": f"node --test {request}.test.cjs"},
        }), encoding="utf-8")
        self.runtime.store.control(goal["goal_id"], "pause")
        return self.runtime.store.get(goal["goal_id"]), workspace

    def request(self, goal_id, body=None, *, authenticated=True):
        route = "/api/long-horizon/verification-approval"
        if body is None:
            route += "?goal_id=" + goal_id
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["X-Harness-Token"] = self.panel.token
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.panel.server_address[1]}" + route,
            data=None if body is None else json.dumps(body).encode("utf-8"), headers=headers,
            method="GET" if body is None else "POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read().decode("utf-8"))

    @staticmethod
    def approval_body(goal, preview):
        return {
            "goal_id": goal["goal_id"], "expected_revision": preview["revision"],
            "command_digest": preview["approval_digest"], "approved": True,
            "chat_id": goal["conversation_id"], "project_id": goal["project"]["id"],
            "participant_ids": goal["requested_agent_ids"],
        }

    def test_exact_goal_approval_is_durable_paused_and_does_not_approve_sibling_or_board(self):
        first, first_root = self.create_goal("first-http")
        second, _second_root = self.create_goal("second-http")
        status, preview = self.request(first["goal_id"])
        self.assertEqual(status, 200, preview)
        self.assertEqual(preview["goal_id"], first["goal_id"])
        self.assertTrue(preview["can_approve"])
        self.assertFalse((self.fixture.project / "package.json").exists())
        with mock.patch.object(self.runtime, "start_background") as start, mock.patch.object(
            swarm_work, "_run_disposable_verification_command",
        ) as run:
            status, answer = self.request(first["goal_id"], self.approval_body(first, preview))
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["goal"]["status"], "paused")
        self.assertTrue(answer["approval"]["approved"])
        start.assert_not_called()
        run.assert_not_called()
        from our_harness.long_horizon import GoalStore

        reloaded = GoalStore(self.fixture.config).get(first["goal_id"])
        self.assertEqual(reloaded["verification_contract"]["approved_test_command_digest"], preview["approval_digest"])
        self.assertEqual(self.runtime.store.get(second["goal_id"])["verification_contract"]["approved_test_command_digest"], "")
        self.assertNotIn("approved_test_command_digest", self.fixture.board["projects"][0])
        self.assertFalse((self.fixture.project / "package.json").exists())
        with mock.patch.object(self.runtime, "start_background"):
            resumed = self.runtime.resume(first["goal_id"], project_verification_settings=self.fixture.board["projects"][0])
        self.assertEqual(resumed["verification_contract"]["approved_test_command_digest"], preview["approval_digest"])
        self.assertTrue((first_root / "package.json").exists())

    def approved_goal_with_existing_board_approval(self, name):
        project = self.fixture.board["projects"][0]
        (self.fixture.project / "package.json").write_text(json.dumps({
            "name": "existing-project", "scripts": {"test": "node --test original.test.cjs"},
        }), encoding="utf-8")
        commands, source = swarm_work._verification_commands(self.fixture.config, self.fixture.project, project)
        self.assertEqual(source, "discovered")
        original_digest = swarm_work._command_approval_digest(
            self.fixture.project, commands, declared_path=project["path"],
        )
        project["approved_test_command_digest"] = original_digest
        goal, workspace = self.create_goal(name)
        status, preview = self.request(goal["goal_id"])
        self.assertEqual(status, 200, preview)
        self.assertNotEqual(preview["approval_digest"], original_digest)
        status, result = self.request(goal["goal_id"], self.approval_body(goal, preview))
        self.assertEqual(status, 200, result)
        return self.runtime.store.get(goal["goal_id"]), workspace, original_digest

    def test_resume_retains_repeated_goal_approval_over_unchanged_nonempty_board_approval(self):
        from our_harness.long_horizon import GoalStore

        goal, workspace, original_digest = self.approved_goal_with_existing_board_approval("existing-approval")
        first_digest = goal["verification_contract"]["approved_test_command_digest"]
        (workspace / "package.json").write_text(json.dumps({
            "name": "improved-project", "scripts": {"test": "node --test improved-again.test.cjs"},
        }), encoding="utf-8")
        status, preview = self.request(goal["goal_id"])
        self.assertEqual(status, 200, preview)
        self.assertNotEqual(preview["approval_digest"], first_digest)
        status, result = self.request(goal["goal_id"], self.approval_body(goal, preview))
        self.assertEqual(status, 200, result)
        reloaded = GoalStore(self.fixture.config).get(goal["goal_id"])
        receipt = reloaded["workspace_command_approval"]
        self.assertEqual(receipt["schema_version"], 2)
        self.assertEqual(receipt["board_verification_contract"]["approved_test_command_digest"], original_digest)
        with mock.patch.object(self.runtime, "start_background"):
            resumed = self.runtime.resume(goal["goal_id"], project_verification_settings=self.fixture.board["projects"][0])
        self.assertEqual(resumed["verification_contract"]["approved_test_command_digest"], preview["approval_digest"])
        self.assertEqual(self.fixture.board["projects"][0]["approved_test_command_digest"], original_digest)

    def test_changed_or_revoked_board_settings_do_not_inherit_goal_approval(self):
        for change in ("changed-digest", "changed-evidence", "revoked", "workspace-drift"):
            with self.subTest(change=change):
                goal, workspace, original_digest = self.approved_goal_with_existing_board_approval(change)
                settings = copy.deepcopy(self.fixture.board["projects"][0])
                if change == "changed-digest":
                    settings["approved_test_command_digest"] = "f" * 64
                elif change == "changed-evidence":
                    settings["test_evidence_contracts"] = [{
                        "command": ["npm", "run", "test"], "format": "json-stdout",
                        "total_field": "new.total", "failed_field": "new.failed",
                    }]
                elif change == "revoked":
                    settings["approved_test_command_digest"] = ""
                else:
                    (workspace / "package.json").write_text('{"scripts":{"test":"node --test changed.test.cjs"}}', encoding="utf-8")
                with mock.patch.object(self.runtime, "start_background"), mock.patch.object(
                    swarm_work, "_run_disposable_verification_command",
                ) as run:
                    if change == "revoked":
                        resumed = self.runtime.resume(goal["goal_id"], project_verification_settings=settings)
                        self.assertEqual(resumed["verification_contract"]["approved_test_command_digest"], "")
                        stored = self.runtime.store.get(goal["goal_id"])
                        self.assertNotIn("workspace_command_approval", stored)
                        selected = goal_verification.verification_project(
                            self.fixture.config, stored, runtime_root=self.runtime.store.root,
                        )
                        result = swarm_work._run_selected_project_verification(
                            self.fixture.config, workspace, selected, stored["objective"], [], None,
                            verification_profile="shared_goal_v1", context_check=True,
                        )
                        self.assertEqual(result["basis"], "discovered_command_approval_required", result)
                    else:
                        with self.assertRaisesRegex(HarnessError, "checks changed"):
                            self.runtime.resume(goal["goal_id"], project_verification_settings=settings)
                        self.assertEqual(self.runtime.store.get(goal["goal_id"])["status"], "paused")
                    run.assert_not_called()
                self.assertEqual(self.fixture.board["projects"][0]["approved_test_command_digest"], original_digest)

    def test_http_rejects_changed_manifest_stale_revision_and_wrong_chat(self):
        goal, workspace = self.create_goal("reject-http")
        _status, preview = self.request(goal["goal_id"])
        original = (workspace / "package.json").read_bytes()
        cases = [
            {**self.approval_body(goal, preview), "expected_revision": preview["revision"] - 1},
            {**self.approval_body(goal, preview), "chat_id": "different-chat"},
            {**self.approval_body(goal, preview), "command_digest": "0" * 64},
            {**self.approval_body(goal, preview), "approved": False},
        ]
        for body in cases:
            with self.subTest(body=body):
                status, answer = self.request(goal["goal_id"], body)
                self.assertEqual(status, 400, answer)
        (workspace / "package.json").write_bytes(original + b"\n")
        status, answer = self.request(goal["goal_id"], self.approval_body(goal, preview))
        self.assertEqual(status, 400, answer)
        self.assertEqual(self.runtime.store.get(goal["goal_id"])["verification_contract"]["approved_test_command_digest"], "")

    def test_preview_requires_session_token(self):
        goal, _workspace = self.create_goal("token-http")
        status, answer = self.request(goal["goal_id"], authenticated=False)
        self.assertEqual(status, 400, answer)


if __name__ == "__main__":
    unittest.main()
