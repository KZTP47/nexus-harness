from __future__ import annotations

import copy
import json
import unittest
import threading
import urllib.request
import urllib.error
from unittest import mock

from our_harness import goal_access, goal_verification, goal_workspaces, long_horizon, swarm_work
from our_harness.models import HarnessError
from tests import test_long_horizon_verification_policy as fixtures


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
