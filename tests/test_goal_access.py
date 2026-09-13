from __future__ import annotations

import copy
import json
import os
import unittest
import threading
import urllib.request
import urllib.error
import sys
from pathlib import Path
from unittest import mock

from our_harness import goal_access, goal_verification, goal_workspaces, long_horizon, swarm_work
from our_harness.models import HarnessError
from tests import test_long_horizon_verification_policy as fixtures
from our_harness import facilitator, facilitator_commands, goal_tools, workspace_collaboration
from our_harness.execution import CommandRunner


class FacilitatorModeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.LongHorizonVerificationPolicyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runtime = self.fixture.runtime
        self.store = self.runtime.store
        # Hosted Windows runners may expose TEMP through an 8.3 alias.
        # Production resolves project identity before binding commands.
        self.project = self.fixture.project.resolve()
        self.config = self.fixture.config

    def create(self, *, mode="ask", isolated=False, request="facilitator", **options):
        return self.store.create(self.fixture.board, "tiny-game", ["Inspect and improve the requested project"], request,
            participant_ids=["creator", "reviewer"], conversation_id=request,
            facilitator_mode=not isolated, isolated_workspace=isolated,
            policy={"agent_access_mode": mode}, **options)

    def checks(self, fail=False):
        (self.project / "package.json").write_text(json.dumps({"scripts": {"test": "node --test example.test.cjs"}}))
        (self.project / "example.test.cjs").write_text(
            "require('node:test')('a real assertion',()=>require('node:assert/strict').equal(2+2,"
            + ("5" if fail else "4") + "));", encoding="utf-8")

    def test_new_runtime_admission_is_direct_and_replays_the_same_goal(self):
        with mock.patch.object(self.runtime, "start_background", side_effect=lambda goal_id, **kw: self.store.public(self.store.get(goal_id))):
            first = self.runtime.start(self.fixture.board, "tiny-game", ["Improve the project"], "direct-admission",
                participant_ids=["creator", "reviewer"], conversation_id="new-chat", policy={"execution_mode": "facilitator"})
            again = self.runtime.start(self.fixture.board, "tiny-game", ["Improve the project"], "direct-admission",
                participant_ids=["creator", "reviewer"], conversation_id="new-chat", policy={"execution_mode": "facilitator"})
        self.assertEqual(first["goal_id"], again["goal_id"])
        goal = self.store.get(first["goal_id"])
        self.assertEqual(goal["execution_mode"], "facilitator")
        self.assertNotIn("execution_workspace", goal)
        self.assertEqual(goal["execution_contract"]["facilitator_contract"], facilitator.CONTRACT)
        self.assertEqual(Path(first["workspace_path"]), self.project)
        self.assertEqual(long_horizon.GoalStore(self.config).get(goal["goal_id"])["execution_contract"], goal["execution_contract"])

    def test_final_command_permission_pauses_and_run_once_resumes_actual_checks(self):
        goal = self.create()
        self.checks()
        self.fixture.finish_tasks(goal)
        paused = self.fixture.verify(goal)
        self.assertEqual(paused["status"], "paused")
        self.assertEqual(paused["command_request"]["state"], "pending")
        reopened = long_horizon.GoalStore(self.config).get(goal["goal_id"])
        self.assertEqual(reopened["command_request"], paused["command_request"])
        preview = goal_verification.goal_command_approval(self.config, reopened, runtime_root=self.store.root)
        self.store.update_access(goal["goal_id"], expected_revision=preview["revision"], decision="once", command_digest=preview["approval_digest"])
        self.store.control(goal["goal_id"], "resume")
        finished = self.fixture.verify(goal)
        self.assertEqual(finished["status"], "complete", finished)
        self.assertEqual(finished["verification"]["status"], "passed")
        self.assertEqual(finished["agent_access"]["grants"][preview["approval_digest"]]["remaining"], 0)

    def test_denied_final_check_finishes_with_honest_unavailable_evidence(self):
        goal = self.create()
        self.checks()
        self.fixture.finish_tasks(goal)
        paused = self.fixture.verify(goal)
        preview = goal_verification.goal_command_approval(self.config, paused, runtime_root=self.store.root)
        self.store.update_access(goal["goal_id"], expected_revision=preview["revision"], decision="deny", command_digest=preview["approval_digest"])
        self.store.control(goal["goal_id"], "resume")
        with mock.patch.object(facilitator_commands, "run_command") as run:
            finished = self.fixture.verify(goal)
        run.assert_not_called()
        self.assertEqual(finished["status"], "complete")
        self.assertEqual(finished["verification"]["basis"], "command_access_denied")
        self.assertEqual(finished["verification"]["commands"], [])

    def test_failed_checks_keep_saved_files_and_report_unverified_criteria(self):
        goal = self.create(mode="full")
        self.checks(fail=True)
        goal_tools.execute(self.config, self.project, "write_file", {"path": "saved.txt", "content": "immediately visible"}, facilitator=True)
        self.fixture.finish_tasks(goal, evidence=False)
        finished = self.fixture.verify(goal)
        self.assertEqual(finished["status"], "complete")
        self.assertEqual(finished["verification"]["status"], "failed")
        self.assertEqual((self.project / "saved.txt").read_text(), "immediately visible")
        self.assertTrue(finished["verification"]["advisory"])
        original = next(item for item in finished["verification"]["criteria_results"] if item["criterion"] == "Original objective is satisfied")
        self.assertEqual(original["status"], "unverified")
        self.assertFalse(any(task["kind"] == "repair" for task in finished["tasks"]))

    def test_direct_native_and_tool_edits_are_observed_without_duplicate_patches(self):
        goal = self.create(mode="full")
        task = self.store.claim_ready(goal["goal_id"], "direct-worker")[0]
        replies = [fixtures.completion(action="work", tool_calls=[
            {"call_id": "save", "name": "write_file", "arguments": {"path": "tool.txt", "content": "tool output"}}]),
            fixtures.completion()]
        def ask(*args, **kwargs):
            self.assertEqual(kwargs["native_execution"], "work")
            self.assertEqual(Path(kwargs["working_directory"]), self.project)
            self.assertEqual(kwargs["workspace_context"].execution_mode, "facilitator")
            (self.project / "native.txt").write_text("native output")
            return {"text": json.dumps(replies.pop(0))}
        with mock.patch.object(long_horizon.chat_lab, "ask_once", side_effect=ask):
            held, result = self.runtime._execute_one(goal["goal_id"], task["id"])
        self.assertEqual(result["changes"], [], result)
        self.assertEqual({one["path"] for one in result["_nexus_direct_changes"]}, {"native.txt", "tool.txt"})
        self.runtime._apply_node({"goal_id": goal["goal_id"], "actions": [{"task": held, "action": result}]})
        self.assertTrue(any(one.get("kind") == "direct_work_observation" for one in self.store.get(goal["goal_id"])["artifacts"]))

    def test_read_only_provider_and_tool_requests_cannot_write(self):
        goal = self.create(mode="read_only")
        task = self.store.claim_ready(goal["goal_id"], "read-worker")[0]
        replies = [fixtures.completion(action="work", tool_calls=[
            {"call_id": "save", "name": "write_file", "arguments": {"path": "forbidden.txt", "content": "no"}}]), fixtures.completion()]
        def ask(*args, **kwargs):
            self.assertEqual(kwargs["native_execution"], "inspect")
            return {"text": json.dumps(replies.pop(0))}
        with mock.patch.object(long_horizon.chat_lab, "ask_once", side_effect=ask), mock.patch.object(goal_tools, "execute") as execute:
            self.runtime._execute_one(goal["goal_id"], task["id"])
        execute.assert_not_called()
        self.assertFalse((self.project / "forbidden.txt").exists())

    def test_ask_mode_applies_structured_changes_without_an_automatic_review_gate(self):
        goal = self.create()
        task = self.store.claim_ready(goal["goal_id"], "writer")[0]
        action = fixtures.completion(action="request_review", risk="high", changes=[
            {"path": "structured.txt", "content": "direct structured output", "reason": "Requested work"}])
        with mock.patch.object(long_horizon.chat_lab, "ask_once", return_value={"text": json.dumps(action)}):
            held, received = self.runtime._execute_one(goal["goal_id"], task["id"])
        self.runtime._apply_node({"goal_id": goal["goal_id"], "actions": [{"task": held, "action": received}]})
        self.assertEqual((self.project / "structured.txt").read_text(), "direct structured output")
        current = self.store.get(goal["goal_id"])
        self.assertFalse(any(task["kind"] == "review" for task in current["tasks"]))
        self.assertFalse(current["interrupts"])

    def test_explicit_same_provider_feedback_does_not_block_writer_or_override_fixed_roles(self):
        board = copy.deepcopy(self.fixture.board)
        board["agents"][1]["who"] = "creator-route"
        goal = self.store.create(board, "tiny-game", ["Improve the project"], "same-provider-feedback",
            participant_ids=["creator", "reviewer"], facilitator_mode=True,
            policy={"agent_access_mode": "full", "collaboration": {"mode": "fixed", "writer_id": "creator", "reviewer_id": "reviewer"}})
        task = goal["tasks"][0]
        reviewed = self.store.control(goal["goal_id"], "request_review", {"task_id": task["id"], "agent_id": "reviewer"})
        current = self.store.get(goal["goal_id"])
        feedback = next(item for item in current["tasks"] if item["kind"] == "feedback")
        self.assertNotEqual(next(item for item in current["tasks"] if item["id"] == task["id"])["state"], "waiting_review")
        self.assertFalse(workspace_collaboration.can_write(current, feedback))
        self.assertEqual(facilitator.native_profile(current, workspace_collaboration.can_write(current, feedback), self.config), "inspect")

    def test_missing_referenced_files_are_reported_without_a_delivery_claim(self):
        goal = self.create(mode="full")
        self.fixture.finish_tasks(goal, refs=["file:missing-output.txt"])
        finished = self.fixture.verify(goal)
        self.assertEqual(finished["status"], "complete")
        self.assertEqual(finished["verification"]["missing_deliverable_files"], ["missing-output.txt"])
        self.assertNotIn("delivery_receipt", finished)

    def test_same_project_queues_and_adaptive_tasks_still_serialize(self):
        goal = self.create(require_all_participants=False)
        tasks = self.store.claim_ready(goal["goal_id"], "serial-worker")
        self.assertEqual(len(tasks), 1)
        other = self.create(request="other-chat")
        self.assertEqual(other["status"], "waiting_for_project")
        self.assertFalse(self.store.claim_ready(other["goal_id"], "other-worker"))

    def test_native_request_binds_policy_cwd_timeout_and_once_survives_restart(self):
        goal = self.create()
        (self.project / "nested").mkdir()
        arguments = {"argv": [sys.executable, "-c", "print('nested execution')"], "cwd": "nested", "timeout_seconds": 12}
        block = facilitator_commands.authorize_tool(self.store, goal, self.project, arguments)
        self.store._mutate(goal["goal_id"], lambda document, db: goal_access.record_block(document, block, "creator"))
        paused = self.store.get(goal["goal_id"])
        preview = goal_verification.goal_command_approval(self.config, paused, runtime_root=self.store.root)
        self.assertEqual((preview["project_path"], preview["cwd"], preview["timeout_seconds"]), (str(self.project), "nested", 12))
        self.store.update_access(goal["goal_id"], expected_revision=preview["revision"], decision="once", command_digest=preview["approval_digest"])
        store = long_horizon.GoalStore(self.config)
        self.assertIs(facilitator_commands.authorize_tool(store, store.get(goal["goal_id"]), self.project, arguments), True)
        executed = goal_tools.execute(self.config, self.project, "run_command", arguments, facilitator=True)
        self.assertEqual(executed["result"]["exit_code"], 0)
        self.assertIn("nested execution", executed["result"]["stdout"])
        self.assertIsInstance(facilitator_commands.authorize_tool(store, store.get(goal["goal_id"]), self.project, arguments), dict)
        digest = facilitator_commands.tool_command_digest(self.project, arguments, self.config)
        self.assertNotEqual(digest, facilitator_commands.tool_command_digest(self.project, {**arguments, "timeout_seconds": 11}, self.config))
        changed = copy.deepcopy(self.config)
        changed.data["execution"]["deny_executables"].append("different-command")
        self.assertNotEqual(digest, facilitator_commands.tool_command_digest(self.project, arguments, changed))
        with self.assertRaises(HarnessError):
            facilitator_commands.tool_command_digest(self.project, {**arguments, "cwd": ".."}, self.config)

    def test_command_policy_and_configured_backend_are_not_bypassed(self):
        for argv in (["shutdown"], ["git", "reset", "--hard"]):
            with self.subTest(argv=argv), self.assertRaises(HarnessError):
                facilitator_commands.run_command(self.config, self.project, argv)
        config = copy.deepcopy(self.config)
        config.data["execution"]["mode"] = "docker"
        goal = self.create(mode="full")
        self.assertEqual(facilitator.native_profile(goal, True, config), "inspect")
        from our_harness.models import CommandResult
        def capture(runner, argv, **kwargs):
            self.assertEqual(runner.config.get("execution.mode"), "docker")
            self.assertEqual(runner.root, self.project)
            self.assertEqual(argv, ["python", "-V"])
            return CommandResult(argv, ".", 0, "fixture", "", 1)
        with mock.patch.object(CommandRunner, "run", autospec=True, side_effect=capture):
            facilitator_commands.run_command(config, self.project, ["python", "-V"])

    def test_recovery_is_explicit_keeps_roles_and_retains_both_copies(self):
        goal = self.create(isolated=True)
        self.store.control(goal["goal_id"], "pause")
        private = goal_workspaces.root(goal, self.store.root)
        (private / "recovered.txt").write_text("saved private work")
        self.store.control(goal["goal_id"], "resume")
        self.assertNotIn("execution_mode", self.store.get(goal["goal_id"]))
        self.assertFalse((self.project / "recovered.txt").exists())
        self.store.control(goal["goal_id"], "pause")
        self.store.control(goal["goal_id"], "resume", {"facilitator_mode": True})
        current = long_horizon.GoalStore(self.config).get(goal["goal_id"])
        self.assertEqual(current["execution_mode"], "facilitator")
        self.assertEqual(goal_access.state(current)["mode"], "ask")
        self.assertIsNotNone(workspace_collaboration.state(current))
        self.assertEqual((self.project / "recovered.txt").read_text(), "saved private work")
        self.assertTrue(private.exists())
        self.assertEqual(current["retained_workspaces"][0]["path"], str(private))

    def test_conflicting_legacy_recovery_preserves_both_versions(self):
        goal = self.create(isolated=True)
        self.store.control(goal["goal_id"], "pause")
        private = goal_workspaces.root(goal, self.store.root)
        (private / "index.html").write_text("private version")
        (self.project / "index.html").write_text("destination version")
        with self.assertRaisesRegex(HarnessError, "conflict"):
            self.store.control(goal["goal_id"], "resume", {"facilitator_mode": True})
        self.assertEqual((private / "index.html").read_text(), "private version")
        self.assertEqual((self.project / "index.html").read_text(), "destination version")
        self.assertNotIn("execution_mode", self.store.get(goal["goal_id"]))

    def test_changed_mode_contract_and_route_cannot_reuse_write_authority(self):
        goal = self.create(mode="full")
        changed = copy.deepcopy(goal)
        changed["execution_contract"]["facilitator_contract"] = "obsolete"
        changed["execution_contract"]["fingerprint_sha256"] = "0" * 64
        with self.assertRaisesRegex(HarnessError, "ownership metadata"):
            self.store._validate_execution_metadata(changed)
        self.assertEqual(goal_access.state(changed)["mode"], "read_only")
        changed = copy.deepcopy(goal)
        changed["agents"][0]["route_binding"]["changed"] = "another route"
        self.assertEqual(goal_access.state(changed)["mode"], "read_only")


class GoalAccessTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.LongHorizonVerificationPolicyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runtime = self.fixture.runtime
        self.store = self.runtime.store
        self.goal = self.store.create(self.fixture.board, "tiny-game", ["Inspect the current project"], "permissions",
            participant_ids=["creator", "reviewer"], conversation_id="permission-chat", isolated_workspace=True)
        self.root = goal_workspaces.root(self.goal, self.store.root)
        self.package = {"name": "portable-permission-fixture", "scripts": {"test": "node --test sample.test.cjs"}}
        (self.root / "package.json").write_text(json.dumps(self.package), encoding="utf-8")
        (self.root / "sample.test.cjs").write_text("require('node:test')('arithmetic', () => require('node:assert/strict').equal(2+2,4));", encoding="utf-8")
        self.store.control(self.goal["goal_id"], "pause")

    def current(self):
        return self.store.get(self.goal["goal_id"])

    def preview(self):
        return goal_verification.workspace_verification_approval(self.fixture.config, self.current(), runtime_root=self.store.root)

    def decide(self, decision):
        preview = self.preview()
        return self.store.update_access(self.goal["goal_id"], expected_revision=preview["revision"],
            decision=decision, command_digest=preview["approval_digest"])

    def mode(self, mode):
        return self.store.update_access(self.goal["goal_id"], expected_revision=self.current()["revision"], mode=mode)

    def verify(self, real=False):
        goal = self.current()
        def run():
            return swarm_work._run_selected_project_verification(self.fixture.config, self.root,
                self.store.access_project(goal), "Build a game with a scoring check", [], None,
                verification_profile="shared_goal_v1", context_check=True)
        if real:
            return run()
        with mock.patch.object(swarm_work, "_run_disposable_verification_command", return_value={
            "argv": ["npm", "run", "test"], "exit_code": 0, "stdout": "# tests 1\n# pass 1\n# fail 0\n",
            "stderr": "", "timed_out": False,
        }) as runner:
            result = run()
        return result, runner.call_count

    def test_default_block_is_actionable_and_does_not_execute(self):
        result, calls = self.verify()
        self.assertEqual(calls, 0)
        self.assertEqual(result["basis"], "discovered_command_approval_required")
        goal = self.current()
        goal_access.record_block(goal, result, "creator")
        self.assertEqual(goal["command_request"]["commands"], [["npm", "run", "test"]])
        self.assertEqual(goal["command_request"]["state"], "pending")
        self.assertEqual(goal["status"], "paused")

    def test_once_consumed_durably_and_always_survives_restart(self):
        self.decide("once")
        before = goal_access.context_fingerprint(self.current())
        result, calls = self.verify()
        self.assertEqual(goal_access.context_fingerprint(self.current()), before)
        self.assertEqual((result["status"], calls), ("passed", 1), result)
        self.store = long_horizon.GoalStore(self.fixture.config)
        result, calls = self.verify()
        self.assertEqual((result["basis"], calls), ("discovered_command_approval_required", 0))
        self.decide("always")
        for _ in range(2):
            self.store = long_horizon.GoalStore(self.fixture.config)
            result, calls = self.verify()
            self.assertEqual((result["status"], calls), ("passed", 1), result)

    def test_deny_overrides_previous_approval_and_can_be_reconsidered(self):
        self.decide("always")
        self.decide("deny")
        self.assertEqual(self.verify()[0]["basis"], "command_access_denied")
        self.decide("once")
        self.assertEqual(self.verify()[0]["status"], "passed")

    def test_deny_resumes_only_the_exact_engine_command_pause(self):
        result, _calls = self.verify()
        def pending(document, _db):
            document["status"] = "queued"
            goal_access.record_block(document, result, "creator")
        self.store._mutate(self.goal["goal_id"], pending)
        answered = self.decide("deny")
        self.assertTrue(answered["command_request"]["resume_after_decision"])
        self.store.control(self.goal["goal_id"], "resume")
        result, calls = self.verify()
        self.assertEqual((result["basis"], calls), ("command_access_denied", 0))
        self.store._mutate(self.goal["goal_id"], lambda document, _db: goal_access.record_block(document, result))
        self.assertEqual(self.current()["status"], "queued")
        self.store.control(self.goal["goal_id"], "pause")
        self.assertFalse(self.decide("deny")["command_request"]["resume_after_decision"])

    def test_stale_command_and_stale_revision_cannot_be_approved(self):
        preview = self.preview()
        self.mode("ask")
        with self.assertRaisesRegex(HarnessError, "chat changed"):
            self.store.update_access(self.goal["goal_id"], expected_revision=preview["revision"],
                decision="always", command_digest=preview["approval_digest"])
        self.decide("always")
        self.package["scripts"]["test"] = "node --test different.test.cjs"
        (self.root / "package.json").write_text(json.dumps(self.package), encoding="utf-8")
        result, calls = self.verify()
        self.assertEqual(calls, 0)
        self.assertEqual(result["basis"], "discovered_command_approval_required")
        with self.assertRaisesRegex(HarnessError, "command changed"):
            self.store.update_access(self.goal["goal_id"], expected_revision=self.current()["revision"],
                decision="always", command_digest=preview["approval_digest"])

    def test_full_mode_read_only_and_changed_binding(self):
        self.mode("full")
        self.assertEqual(self.verify()[0]["status"], "passed")
        goal = self.current()
        for key in ("goal_id", "project_authority_id", "execution_contract", "agents"):
            altered = copy.deepcopy(goal)
            altered[key] = "different portable environment"
            self.assertEqual(goal_access.state(altered)["mode"], "read_only")
        self.mode("read_only")
        result, calls = self.verify()
        self.assertEqual((result["basis"], calls), ("read_only_access", 0))
        with self.assertRaisesRegex(HarnessError, "Read only"):
            self.decide("once")

    def test_read_only_blocks_transaction_preparation(self):
        self.mode("read_only")
        with self.assertRaisesRegex(HarnessError, "Read only"):
            self.store.prepare_transaction(self.goal["goal_id"], self.current()["tasks"][0], "fixture",
                [{"path": "never-written.txt", "content": "unauthorized"}])
        self.assertFalse((self.root / "never-written.txt").exists())
        (self.root / "retained.md").write_text("previously proposed change", encoding="utf-8")
        with goal_workspaces.publication(self.current(), self.store.root):
            receipt = goal_workspaces.prepare_publish(self.current(), self.store.root)
            with self.assertRaisesRegex(HarnessError, "Read only"):
                goal_workspaces.publish(self.current(), self.store.root, receipt)
        self.assertFalse((self.fixture.project / "retained.md").exists())

    def test_serialized_authority_is_rejected(self):
        with self.assertRaisesRegex(HarnessError, "engine-issued"):
            goal_access.command_gate({"_nexus_command_access": {"mode": "full"}}, [["test"]], "x", "discovered")

    def test_actual_node_command_only_runs_once(self):
        self.decide("once")
        result = self.verify(real=True)
        self.assertEqual(result["status"], "passed", result)
        self.assertTrue(result["commands"])
        result = self.verify(real=True)
        self.assertEqual(result["basis"], "discovered_command_approval_required", result)

    def test_actual_npm_playwright_script_observes_production_behavior(self):
        runtime = swarm_work.discover_bundled_playwright_runtime(
            required=os.environ.get("NEXUS_REQUIRE_BROWSER_RUNTIME_TESTS") == "1")
        if runtime is None:
            self.skipTest("Bundled-browser integration runs in the required desktop CI job")
        self.package["scripts"]["test"] = "playwright test launch.spec.cjs"
        (self.root / "package.json").write_text(json.dumps(self.package), encoding="utf-8")
        (self.root / "index.html").write_text('<button id="launch">Launch</button><p id="status">Waiting</p><script src="./game.js"></script>', encoding="utf-8")
        (self.root / "game.js").write_text("document.querySelector('#launch').onclick=()=>{document.querySelector('#status').textContent='Running';};", encoding="utf-8")
        (self.root / "launch.spec.cjs").write_text("const {test,expect}=require('@playwright/test');\ntest('launch',async({page})=>{\n await page.goto('/index.html');\n await page.locator('#launch').click();\n await expect(page.locator('#status')).toHaveText('Running');\n});", encoding="utf-8")
        self.decide("always")
        result = self.verify(real=True)
        self.assertEqual(result["status"], "passed", result)
        self.assertTrue(result["commands"][0]["brokered_e2e_receipts"])
        (self.root / "game.js").write_text("// handler removed", encoding="utf-8")
        result = self.verify(real=True)
        self.assertEqual(result["status"], "failed", result)

    def test_admission_respects_explicit_mode_and_rejects_unknown(self):
        goal = self.store.create(self.fixture.board, "tiny-game", ["Read the project"], "readonly-admission",
            participant_ids=["creator", "reviewer"], conversation_id="read-chat", isolated_workspace=True,
            policy={"agent_access_mode": "read_only"})
        self.assertEqual(goal_access.state(goal)["mode"], "read_only")
        with self.assertRaisesRegex(HarnessError, "supported"):
            self.store.validate_create(self.fixture.board, "tiny-game", ["Inspect"], "invalid-mode", policy={"agent_access_mode": "unrestricted"})

    def test_http_decision_binding_and_resume_runs_actual_command(self):
        from our_harness.server import HarnessHTTPServer
        panel = HarnessHTTPServer(("127.0.0.1", 0), self.fixture.config)
        panel._long_horizon = self.runtime
        self.addCleanup(panel.server_close)
        threading.Thread(target=panel.serve_forever, daemon=True).start()
        self.addCleanup(panel.shutdown)
        preview_url = f"http://127.0.0.1:{panel.server_address[1]}/api/long-horizon/access?goal_id={self.goal['goal_id']}"
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(preview_url)
        with urllib.request.urlopen(urllib.request.Request(preview_url, headers={"X-Harness-Token": panel.token})) as response:
            preview = json.load(response)
        self.assertEqual(preview["resolved_commands"], [["node", "--test", "sample.test.cjs"]])
        self.assertEqual(preview["goal_id"], self.goal["goal_id"])
        def post(route, body, token=True):
            request = urllib.request.Request(f"http://127.0.0.1:{panel.server_address[1]}" + route,
                data=json.dumps(body).encode(), headers={"Content-Type": "application/json",
                    **({"X-Harness-Token": panel.token} if token else {})})
            try:
                with urllib.request.urlopen(request) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                return error.code, json.load(error)
        preview = self.preview()
        body = {"goal_id": self.goal["goal_id"], "chat_id": "permission-chat", "project_id": "tiny-game",
            "participant_ids": ["creator", "reviewer"], "expected_revision": preview["revision"],
            "decision": "once", "command_digest": preview["approval_digest"]}
        self.assertIn(post("/api/long-horizon/access", body, False)[0], {400, 403})
        self.assertEqual(post("/api/long-horizon/access", {**body, "chat_id": "other-chat"})[0], 400)
        status, saved = post("/api/long-horizon/access", body)
        self.assertEqual(status, 200, saved)
        with mock.patch.object(panel, "swarm_standing", return_value={"board": self.fixture.board}), \
                mock.patch.object(self.runtime, "start_background") as start:
            status, resumed = post("/api/long-horizon/control", {"goal_id": self.goal["goal_id"],
                "action": "resume", "payload": {"expected_revision": saved["goal"]["revision"]}})
        self.assertEqual(status, 200, resumed)
        start.assert_called_once()
        self.assertEqual(self.verify(real=True)["status"], "passed")
        self.assertEqual(self.verify()[0]["basis"], "discovered_command_approval_required")

    def test_context_result_pauses_before_another_agent_turn_and_survives_reload(self):
        with mock.patch.object(self.runtime, "start_background"):
            self.runtime.resume(self.goal["goal_id"], project_verification_settings=self.fixture.board["projects"][0])
        task = self.store.claim_ready(self.goal["goal_id"], "permission-worker")[0]
        call = {"name": "run_selected_verification", "call_id": "verify", "arguments": {}}
        self.store.acknowledge_context_step(self.goal["goal_id"], task, {"tool_calls": [call]}, "work")
        blocked, calls = self.verify()
        self.store.record_context_tool_result(self.goal["goal_id"], task, call, blocked)
        self.assertEqual(calls, 0)
        restored = long_horizon.GoalStore(self.fixture.config).public(self.current())
        self.assertEqual(restored["command_request"]["state"], "pending")
        self.assertEqual(restored["status"], "paused")
        self.assertEqual(self.store.claim_ready(self.goal["goal_id"], "another-worker"), [])

    def test_package_wrappers_do_not_drop_hooks_or_interpret_shell(self):
        from our_harness.verification_scripts import resolve_package_command
        for script, extra in (("node --test && echo bypass", {}), ("node --test", {"pretest": "echo side-effect"}),
                              ("node $INJECTED", {}), ("unsupported --test", {})):
            (self.root / "package.json").write_text(json.dumps({"scripts": {"test": script, **extra}}), encoding="utf-8")
            with self.assertRaises(HarnessError):
                resolve_package_command(self.root, ["npm", "run", "test"])
        (self.root / "package.json").write_text(json.dumps({"scripts": {"test": 'playwright test "tests/my page.spec.js"'}}), encoding="utf-8")
        self.assertEqual(resolve_package_command(self.root, ["npm", "run", "test"]),
            ["node", "node_modules/@playwright/test/cli.js", "test", "tests/my page.spec.js"])

    def test_board_goal_has_the_same_command_decision_path(self):
        self.store.control(self.goal["goal_id"], "cancel")
        (self.fixture.project / "package.json").write_text(json.dumps(self.package), encoding="utf-8")
        goal = self.store.create(self.fixture.board, "tiny-game", ["Build a game"], "board-permission", isolated_workspace=False)
        self.store.control(goal["goal_id"], "pause")
        goal = self.store.get(goal["goal_id"])
        preview = goal_verification.goal_command_approval(self.fixture.config, goal, runtime_root=self.store.root)
        self.assertEqual(preview["commands"], [["npm", "run", "test"]])
        self.store.update_access(goal["goal_id"], expected_revision=goal["revision"], decision="once", command_digest=preview["approval_digest"])
        allowed, _revision = self.store.authorize_commands(goal["goal_id"], preview["commands"], preview["approval_digest"], "discovered")
        self.assertIs(allowed, True)


if __name__ == "__main__":
    unittest.main()
