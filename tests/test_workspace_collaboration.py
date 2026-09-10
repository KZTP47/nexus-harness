from __future__ import annotations
import copy
import json
import sys
import threading
import urllib.request
import urllib.error
from pathlib import Path
import unittest
from unittest.mock import patch

from our_harness import long_horizon as lh, workspace_collaboration as wc, agent_workspaces as aw, goal_workspaces as gw
from our_harness.models import HarnessError
import test_long_horizon as fixtures


class WorkspaceCollaborationTests(unittest.TestCase):
    setUp = fixtures.LongHorizonTests.setUp
    stage_review = fixtures.LongHorizonTests.stage_review

    def create(self, mode="flexible", direct=False):
        (self.project / "app.txt").write_text("original")
        self.runtime = lh.LongHorizonRuntime(self.config)
        self.addCleanup(self.runtime.close)
        self.goal = self.runtime.store.create(self.board, "project", ["Build the complete requested result"], "collaboration",
            isolated_workspace=True, participant_ids=["lead", "reviewer"], require_all_participants=False,
            policy={"agent_access_mode": "full", "collaboration": {"mode": mode, "writer_id": "lead",
                "reviewer_id": "reviewer", "allow_direct_real_edits": direct}})
        self.task = self.runtime.store.claim_ready(self.goal["goal_id"], "worker")[0]
        return self.goal

    def call(self, name, **args):
        return wc.execute(self.runtime, self.runtime.store.get(self.goal["goal_id"]), self.task, name, args)

    def test_flexible_peer_edits_are_visible_without_handoff_and_conflicts_do_not_overwrite(self):
        self.create()
        catalog = self.call("workspace_catalog")
        self.assertEqual({w["id"] for w in catalog["workspaces"]}, {"real", "lead", "reviewer"})
        read = self.call("workspace_read", workspace_id="reviewer", path="app.txt")
        answer = self.call("workspace_edit", workspace_id="reviewer", expected_fingerprint=read["fingerprint"],
            changes=[{"path": "app.txt", "content": "peer contribution", "reason": "collaboration"}])
        self.assertNotEqual(answer["fingerprint"], read["fingerprint"])
        self.assertEqual(self.call("workspace_read", workspace_id="reviewer", path="app.txt")["content"], "peer contribution")
        with self.assertRaisesRegex(HarnessError, "changed"):
            self.call("workspace_edit", workspace_id="reviewer", expected_fingerprint=read["fingerprint"],
                changes=[{"path": "app.txt", "content": "stale overwrite", "reason": "stale"}])
        self.assertEqual((self.project / "app.txt").read_text(), "original")

    def test_team_can_prepare_files_added_to_real_project_after_goal_admission(self):
        self.create()
        late = self.project / "extracted-repository" / "source.txt"
        late.parent.mkdir()
        late.write_text("newly extracted source", encoding="utf-8")
        listed = self.call("workspace_read", workspace_id="real", path="extracted-repository/source.txt")
        draft = self.call("workspace_read", workspace_id="lead", path="app.txt")
        self.call("workspace_edit", workspace_id="lead", expected_fingerprint=draft["fingerprint"],
            changes=[{"path": "extracted-repository/source.txt", "content": listed["content"], "reason": "Prepare source for implementation"}])
        self.assertEqual(self.call("workspace_read", workspace_id="lead", path="extracted-repository/source.txt")["content"], "newly extracted source")
        prompt = wc.prompt(self.runtime.store.get(self.goal["goal_id"]), self.task, self.runtime.store.root)
        self.assertIn("Do not ask the user to copy, synchronize, or extract", prompt)
        self.assertEqual(self.runtime.store.get(self.goal["goal_id"])["interrupts"], [])
        self.assertEqual(late.read_text(encoding="utf-8"), "newly extracted source")

    def test_fixed_roles_block_peer_drafts_review_edits_and_unauthorized_real_writes(self):
        self.create("fixed")
        for target in ("reviewer", "real"):
            read = self.call("workspace_read", workspace_id=target, path="app.txt")
            with self.assertRaisesRegex(HarnessError, "does not allow"):
                self.call("workspace_edit", workspace_id=target, expected_fingerprint=read["fingerprint"], changes=[])
        reviewer = {**self.task, "assigned_agent_id": "reviewer"}
        for target in ("lead", "reviewer", "real"):
            self.assertFalse(wc.can_write(self.goal, reviewer, target))
        with self.assertRaises(HarnessError):
            wc.validate_action(self.goal, reviewer, fixtures.action(changes=[{"path":"app.txt", "content":"bad"}]))
        self.assertEqual(wc.reviewer(self.goal, "lead")["id"], "reviewer")
        self.assertIn('"mode": "fixed"', wc.prompt(self.goal, self.task, self.runtime.store.root))

    def test_direct_real_edit_requires_its_separate_option(self):
        self.create(direct=True)
        read = self.call("workspace_read", workspace_id="real", path="app.txt")
        result = self.call("workspace_edit", workspace_id="real", expected_fingerprint=read["fingerprint"],
            changes=[{"path":"app.txt", "content":"directly edited", "reason":"user enabled direct editing"}])
        self.assertTrue(result["direct_real_edit"])
        self.assertEqual((self.project / "app.txt").read_text(), "directly edited")
        # Existing publisher merges the real edit into the team version for final judgment.
        with gw.publication(self.goal, self.runtime.store.root):
            receipt = gw.prepare_publish(self.goal, self.runtime.store.root)
        self.assertTrue(receipt["rebased"])
        self.assertEqual((gw.root(self.goal, self.runtime.store.root) / "app.txt").read_text(), "directly edited")

    def test_snapshot_is_exact_restartable_and_cannot_be_rebound_to_another_goal(self):
        self.create()
        snap = self.call("workspace_snapshot", workspace_id="lead")
        self.assertEqual((Path(snap["path"]) / "app.txt").read_text(), "original")
        self.assertEqual(snap, self.call("workspace_snapshot", workspace_id="lead"))
        with self.assertRaises(HarnessError): wc.load_snapshot(self.goal, self.runtime.store.root, snap["snapshot_id"])
        held = copy.deepcopy(self.goal)
        held["tasks"][0]["context_steps"] = [{"results":[{"result":snap}]}]
        self.assertEqual(wc.load_snapshot(held, self.runtime.store.root, snap["snapshot_id"]), Path(snap["path"]))
        (Path(snap["path"]) / "app.txt").write_text("tampered")
        with self.assertRaisesRegex(HarnessError, "changed"):
            wc.load_snapshot(held, self.runtime.store.root, snap["snapshot_id"])

    def test_formal_reviewer_inspects_author_candidate_not_own_draft_and_stale_review_is_rejected(self):
        self.create("fixed")
        proposed = fixtures.action("request_review", changes=[{"path":"app.txt", "content":"author submission", "reason":"requested"}])
        self.stage_review(self.runtime.store, self.goal, self.task, proposed)
        review = self.runtime.store.claim_ready(self.goal["goal_id"], "reviewer")[0]
        with aw.workspace(self.goal, self.goal["agents"][-1], self.runtime.store.root) as draft:
            (draft.root / "app.txt").write_text("unrelated reviewer draft")
        seen = []
        def ask(*args, **kwargs):
            root = Path(kwargs["working_directory"])
            self.assertEqual((root / "app.txt").read_text(), "author submission")
            self.assertEqual(kwargs["native_execution"], "inspect")
            seen.append(root)
            kwargs["before_provider_dispatch"]("initial"); kwargs["after_provider_response"]("initial")
            return {"text":json.dumps(fixtures.action(review_verdict="approve", review_findings=["Inspected exact submitted project"],
                evidence=["review-packet:"+review["review_packet_sha256"]]))}
        with patch.object(lh.chat_lab, "ask_once", side_effect=ask):
            claimed, action = self.runtime._execute_one(self.goal["goal_id"], review["id"])
        self.assertEqual(len(seen), 1, action)
        self.assertNotEqual(action["action"], "failed", action)
        (gw.root(self.goal, self.runtime.store.root) / "dependency.txt").write_text("unreviewed change")
        self.runtime._apply_node({"goal_id":self.goal["goal_id"], "actions":[{"task":claimed,"action":action}]})
        held = self.runtime.store.get(self.goal["goal_id"])
        self.assertEqual(held["tasks"][0]["state"], "ready")
        self.assertEqual(held["tasks"][-1]["state"], "cancelled")
        self.assertEqual((self.project / "app.txt").read_text(), "original")

    def test_policy_is_versioned_bound_and_survives_restart(self):
        self.create()
        policy = wc.state(lh.GoalStore(self.config).get(self.goal["goal_id"]))
        self.assertEqual(policy["mode"], "flexible")
        for key, value in [("goal_id", "other"), ("project", {"path":"/elsewhere"})]:
            goal = copy.deepcopy(self.goal); goal[key] = value
            with self.assertRaises(HarnessError): wc.state(goal)
        self.runtime.store.apply_action(self.goal["goal_id"], self.task, fixtures.action("work"))
        self.runtime.store.release_scheduler(self.goal["goal_id"], "worker")
        held = self.runtime.store.get(self.goal["goal_id"])
        saved = wc.update(self.runtime.store, held["goal_id"], held["revision"], {"mode":"fixed", "writer_id":"lead", "reviewer_id":"reviewer"})
        self.assertEqual(wc.state(saved)["mode"], "fixed")
        with self.assertRaises(HarnessError): wc.update(self.runtime.store, held["goal_id"], held["revision"], {})

    def test_workspace_tools_run_through_real_agent_context_continuation(self):
        self.create()
        replies = [fixtures.action("work", tool_calls=[{"call_id":"peer-read", "name":"workspace_read",
            "arguments":{"workspace_id":"reviewer", "path":"app.txt", "cursor":""}}]), fixtures.action()]
        def ask(*args, **kwargs):
            kwargs["before_provider_dispatch"]("initial"); kwargs["after_provider_response"]("initial")
            return {"text":json.dumps(replies.pop(0))}
        with patch.object(lh.chat_lab, "ask_once", side_effect=ask):
            _, result = self.runtime._execute_one(self.goal["goal_id"], self.task["id"])
        self.assertEqual(result["action"], "complete", result)
        held = self.runtime.store.get(self.goal["goal_id"])["tasks"][0]
        self.assertEqual(held["context_steps"][0]["results"][0]["result"]["content"], "original")

    def test_flexible_low_risk_edits_do_not_require_formal_handoff(self):
        self.create()
        action = fixtures.action(changes=[{"path":"app.txt","content":"done"}])
        self.assertFalse(self.runtime.store._needs_review(self.goal, self.task, action, None))
        wc.install(self.goal, {"mode":"fixed"})
        self.assertTrue(self.runtime.store._needs_review(self.goal, self.task, action, None))

    def test_snapshot_verification_runs_real_tests_in_a_disposable_copy(self):
        (self.project / "test_result.py").write_text("import unittest\nfrom pathlib import Path\nclass Tests(unittest.TestCase):\n def test_result(self):\n  self.assertEqual(Path('app.txt').read_text(), 'original')\n  Path('test-side-effect.txt').write_text('temporary')\n")
        self.board["projects"][0]["test_commands"] = [[sys.executable, "-m", "unittest", "discover", "-s", ".", "-p", "test_result.py"]]
        self.create()
        responses = 0
        captured = {}
        def ask(*args, **kwargs):
            nonlocal responses
            kwargs["before_provider_dispatch"]("initial"); kwargs["after_provider_response"]("initial")
            responses += 1
            if responses == 1:
                action = fixtures.action("work", tool_calls=[{"call_id":"snapshot", "name":"workspace_snapshot", "arguments":{"workspace_id":"lead"}}])
            elif responses == 2:
                captured.update(self.runtime.store.get(self.goal["goal_id"])["tasks"][0]["context_steps"][0]["results"][0]["result"])
                action = fixtures.action("work", tool_calls=[{"call_id":"verify", "name":"workspace_verify", "arguments":{"snapshot_id":captured["snapshot_id"]}}])
            else:
                action = fixtures.action()
            return {"text":json.dumps(action)}
        with patch.object(lh.chat_lab, "ask_once", side_effect=ask):
            _, result = self.runtime._execute_one(self.goal["goal_id"], self.task["id"])
        self.assertEqual(result["action"], "complete", result)
        held = self.runtime.store.get(self.goal["goal_id"])["tasks"][0]
        check = held["context_steps"][1]["results"][0]
        self.assertIsNotNone(check["result"], check)
        self.assertEqual(check["result"]["status"], "passed", check)
        self.assertFalse((Path(captured["path"]) / "test-side-effect.txt").exists())
        self.assertFalse((self.project / "test-side-effect.txt").exists())

    def test_wrong_workspace_requests_return_correctable_observations_without_stopping_goal(self):
        self.create()
        replies = [fixtures.action("work", tool_calls=[{"call_id":"bad", "name":"workspace_read",
            "arguments":{"workspace_id":"foreign-agent", "path":"app.txt", "cursor":""}}]), fixtures.action()]
        def ask(*args, **kwargs):
            kwargs["before_provider_dispatch"]("initial"); kwargs["after_provider_response"]("initial")
            return {"text":json.dumps(replies.pop(0))}
        with patch.object(lh.chat_lab, "ask_once", side_effect=ask):
            _, result = self.runtime._execute_one(self.goal["goal_id"], self.task["id"])
        self.assertEqual(result["action"], "complete", result)
        self.assertNotEqual(self.runtime.store.get(self.goal["goal_id"])["status"], "paused")

    def test_http_settings_require_token_current_revision_and_matching_chat(self):
        from our_harness.server import HarnessHTTPServer
        self.create()
        self.runtime.store.apply_action(self.goal["goal_id"], self.task, fixtures.action("work"))
        self.runtime.store.release_scheduler(self.goal["goal_id"], "worker")
        held = self.runtime.store.get(self.goal["goal_id"])
        panel = HarnessHTTPServer(("127.0.0.1", 0), self.config)
        panel._long_horizon = self.runtime
        self.addCleanup(panel.server_close)
        threading.Thread(target=panel.serve_forever, daemon=True).start()
        self.addCleanup(panel.shutdown)
        body = {"goal_id":held["goal_id"], "expected_revision":held["revision"],
                "settings":{"mode":"fixed", "writer_id":"lead", "reviewer_id":"reviewer"}}
        def post(value, token=True):
            request = urllib.request.Request(f"http://127.0.0.1:{panel.server_address[1]}/api/long-horizon/collaboration",
                data=json.dumps(value).encode(), headers={"Content-Type":"application/json", **({"X-Harness-Token":panel.token} if token else {})})
            try:
                with urllib.request.urlopen(request) as response: return response.status, json.load(response)
            except urllib.error.HTTPError as error: return error.code, json.load(error)
        self.assertNotEqual(post(body, False)[0], 200)
        self.assertEqual(post({**body,"chat_id":"foreign-chat","project_id":"project"})[0],400)
        status, saved = post(body)
        self.assertEqual(status,200,saved)
        self.assertEqual(saved["goal"]["workspace_collaboration"]["mode"],"fixed")
        self.assertEqual(post(body)[0],400)


if __name__ == "__main__": unittest.main()
