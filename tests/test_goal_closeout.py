from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

from our_harness import agent_workspaces as aw, goal_closeout as closeout, goal_workspaces as gw, long_horizon as lh
from our_harness.models import HarnessError
import test_long_horizon as fixtures
from test_long_horizon import action


class GoalCloseoutTests(unittest.TestCase):
    setUp = fixtures.LongHorizonTests.setUp

    def test_real_command_authorization_does_not_endlessly_supersede_closeout(self):
        (self.project / "test_submission.py").write_text(
            "import unittest\nfrom pathlib import Path\nclass Submission(unittest.TestCase):\n"
            " def test_program(self):\n  self.assertEqual(Path('app.txt').read_text(), 'submitted program')\n",
            encoding="utf-8")
        self.board["projects"][0]["test_commands"] = [
            [sys.executable, "-m", "unittest", "discover", "-s", ".", "-p", "test_submission.py"]]
        runtime, goal, root = self.prepared(objective="Update app.txt")
        self.assertEqual(runtime._verify_node({"goal_id": goal["goal_id"]}), {"route": "schedule"})
        task = runtime.store.claim_ready(goal["goal_id"], "judge")[0]
        self.assertEqual(task["closeout_packet"]["verification"]["status"], "passed")
        self.execute(runtime, goal, task, self.verdict(task))
        with patch.object(lh.swarm_work, "_run_selected_project_verification") as rerun:
            self.assertEqual(runtime._verify_node({"goal_id": goal["goal_id"]}), {"route": "end"})
        rerun.assert_not_called()
        self.assertEqual(runtime.store.get(goal["goal_id"])["status"], "complete")
        self.assertEqual((self.project / "app.txt").read_text(), "submitted program")

    def prepared(self, *, single=False, success_criteria=None,
                 objective="Write the requested program AND its usage guide"):
        (self.project / "app.txt").write_text("original", encoding="utf-8")
        runtime = lh.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        board = copy.deepcopy(self.board)
        if single:
            board["agents"] = board["agents"][:1]
            board["works_on"] = board["works_on"][:1]
        goal = runtime.store.create(board, "project", [objective],
                                    "closeout-test", isolated_workspace=True, success_criteria=success_criteria)
        root = gw.root(goal, runtime.store.root)
        (root / "app.txt").write_text("submitted program", encoding="utf-8")
        task = runtime.store.claim_ready(goal["goal_id"], "author")[0]
        runtime.store.apply_action(goal["goal_id"], task, action(criteria_evidence=[{
            "criterion": "Original objective is satisfied", "evidence_refs": ["file:app.txt"]}]),
            artifact={"kind": "file_transaction", "transaction_id": "author-tx", "patch_sha256": "d" * 64,
                      "changes": [{"path": "app.txt", "delete": False}]})
        return runtime, runtime.store.get(goal["goal_id"]), root

    def verify(self, runtime, goal):
        with patch.object(lh.swarm_work, "_run_selected_project_verification", return_value={
                "status": "passed", "basis": "test", "reason": "Selected checks passed", "commands": []}):
            return runtime._verify_node({"goal_id": goal["goal_id"]})

    def judge(self, runtime, goal):
        self.assertEqual(self.verify(runtime, goal), {"route": "schedule"})
        task = runtime.store.claim_ready(goal["goal_id"], "judge")[0]
        self.assertTrue(task.get("closeout_packet"))
        return task

    def verdict(self, task, *, approve=True):
        return action("complete" if approve else "blocked", review_verdict="approve" if approve else "changes_requested",
            review_findings=["Program and guide inspected" if approve else "The original prompt also requires a usage guide; write it."],
            evidence=["review-packet:" + task["review_packet_sha256"]],
            criteria_evidence=[{"criterion": c, "evidence_refs": ["file:app.txt"]}
                for c in task["closeout_packet"]["scope"]["acceptance_criteria"]] if approve else [])

    def execute(self, runtime, goal, task, verdict, inspect=None):
        def ask(_config, _route, _prompt, **kwargs):
            if inspect:
                inspect(kwargs)
            kwargs["before_provider_dispatch"]("initial")
            kwargs["after_provider_response"]("initial")
            return {"text": json.dumps(verdict)}
        with patch.object(lh.chat_lab, "ask_once", side_effect=ask):
            claimed, proposal = runtime._execute_one(goal["goal_id"], task["id"])
        self.assertNotIn(proposal.get("action"), {"failed", "superseded"}, proposal)
        runtime._apply_node({"goal_id": goal["goal_id"], "actions": [{"task": claimed, "action": proposal}]})
        return runtime.store.get(goal["goal_id"])

    def test_whole_goal_repair_loop_and_exact_review_snapshot_before_publication(self):
        runtime, goal, root = self.prepared()
        with aw.workspace(goal, goal["agents"][-1], runtime.store.root) as b:
            (b.root / "app.txt").write_text("unrelated reviewer draft")
            earlier_copy = b.root
        task = self.judge(runtime, goal)
        self.assertEqual(task["assigned_agent_id"], "reviewer")
        self.assertEqual((self.project / "app.txt").read_text(), "original")
        def inspect(kwargs):
            snapshot = Path(kwargs["working_directory"])
            self.assertNotEqual(snapshot, earlier_copy)
            self.assertNotEqual(snapshot, root)
            self.assertEqual((snapshot / "app.txt").read_text(), "submitted program")
            self.assertEqual(kwargs["native_execution"], "inspect")
            self.assertIn(goal["original_objective"], kwargs["context"])
            self.assertIn(closeout.OVERALL, kwargs["context"])
        rejected = self.execute(runtime, goal, task, self.verdict(task, approve=False), inspect)
        self.assertEqual(rejected["tasks"][-1]["state"], "complete")
        self.assertFalse(closeout.approved(rejected, root))
        self.verify(runtime, goal)
        repair = runtime.store.claim_ready(goal["goal_id"], "repair")[0]
        self.assertEqual(repair["kind"], "repair")
        self.assertEqual(repair["assigned_agent_id"], "lead")
        reminder = runtime._agent_context(runtime.store.get(goal["goal_id"]), repair)
        self.assertIn(goal["original_objective"], reminder)
        self.assertIn("usage guide", reminder)
        (root / "guide.txt").write_text("Usage guide")
        runtime.store.apply_action(goal["goal_id"], repair, action(), artifact={
            "kind": "file_transaction", "transaction_id": "repair-tx", "patch_sha256": "e" * 64,
            "changes": [{"path": "guide.txt", "delete": False}]})
        final = self.judge(runtime, goal)
        self.assertNotEqual(task["id"], final["id"])
        accepted = self.execute(runtime, goal, final, self.verdict(final))
        self.assertTrue(closeout.approved(accepted, root))
        self.assertEqual(self.verify(runtime, goal), {"route": "end"})
        self.assertEqual(runtime.store.get(goal["goal_id"])["status"], "complete")
        self.assertEqual((self.project / "app.txt").read_text(), "submitted program")
        self.assertEqual((self.project / "guide.txt").read_text(), "Usage guide")
        self.assertEqual((earlier_copy / "app.txt").read_text(), "unrelated reviewer draft")

    def test_cannot_bypass_judge_or_approve_missing_criteria_or_nonexistent_proof(self):
        runtime, goal, root = self.prepared()
        publish = unittest.mock.Mock()
        held = runtime.store.complete_verification(goal["goal_id"], {"status": "passed"}, publish_workspace=publish)
        self.assertNotEqual(held["status"], "complete")
        publish.assert_not_called()
        # Complete the generated repair so this goal reaches the real closeout boundary.
        repair = runtime.store.claim_ready(goal["goal_id"], "repair")[0]
        runtime.store.apply_action(goal["goal_id"], repair, action(), artifact={"kind": "verified_no_change", "tree_merkle": "a" * 64})
        task = self.judge(runtime, goal)
        # The judge's snapshot stays read-only: it cannot edit or delegate.
        for alteration in ("edit", "delegate", "handoff"):
            with self.subTest(alteration=alteration):
                answer = self.verdict(task)
                if alteration == "edit": answer["changes"] = [{"path": "app.txt", "content": "tampered"}]
                if alteration == "delegate": answer["tasks"] = [{"title": "approve me"}]
                if alteration == "handoff": answer["handoff_agent_id"] = "lead"
                with self.assertRaises(HarnessError):
                    closeout.validate_action(runtime.store.get(goal["goal_id"]), copy.deepcopy(task), answer, root)
        # Evidence-format gaps in an approval are reported, never a veto.
        criteria = task["closeout_packet"]["scope"]["acceptance_criteria"]
        for alteration in ("missing", "unknown-file", "no-verdict", "no-packet-ref", "no-findings"):
            with self.subTest(alteration=alteration):
                answer = self.verdict(task)
                if alteration == "missing": answer["criteria_evidence"] = answer["criteria_evidence"][1:]
                if alteration == "unknown-file": answer["criteria_evidence"][0]["evidence_refs"] = ["file:missing.txt"]
                if alteration == "no-verdict": answer.pop("review_verdict")
                if alteration == "no-packet-ref": answer["evidence"] = []
                if alteration == "no-findings": answer["review_findings"] = []
                judged = copy.deepcopy(task)
                closeout.validate_action(runtime.store.get(goal["goal_id"]), judged, answer, root)
                outcome = judged["closeout_outcome"]
                self.assertEqual(outcome["verdict"], "approve")
                self.assertTrue(outcome["findings"])
                if alteration in {"missing", "unknown-file"}:
                    self.assertEqual(outcome["evidence_notes"], [
                        "The judge approved without a recognised evidence reference for: " + criteria[0]])
                else:
                    self.assertNotIn("evidence_notes", outcome)
        rejection = self.verdict(task, approve=False)
        rejection.pop("review_verdict")
        judged = copy.deepcopy(task)
        closeout.validate_action(runtime.store.get(goal["goal_id"]), judged, rejection, root)
        self.assertEqual(judged["closeout_outcome"]["verdict"], "changes_requested")
        # A stale verdict for files that changed is still refused.
        (root / "app.txt").write_text("edited after the judge's snapshot")
        with self.assertRaisesRegex(HarnessError, "stale"):
            closeout.validate_action(runtime.store.get(goal["goal_id"]), copy.deepcopy(task), self.verdict(task), root)
        self.assertEqual((self.project / "app.txt").read_text(), "original")

    def test_criterion_and_evidence_matching_tolerates_formatting(self):
        criteria = [closeout.OVERALL, "Original objective is satisfied", "Every required task is complete"]
        for mapping, expected in (
            ({"criterion": "  original OBJECTIVE is satisfied!! "}, 1),
            ({"criterion": "“Every required task is complete.”"}, 2),
            ({"criterion": "2"}, 1), ({"criterion": "#3"}, 2), ({"criterion": "criterion 1"}, 0),
            ({"criterion_id": "AC-2"}, 1), ({"criterion": "acceptance_criteria[2]"}, 2),
            ({"criterion": "Criterion: Original objective is satisfied (see app.txt)"}, 1),
            ({"criterion": "Every required tasks are complete"}, 2),
        ):
            with self.subTest(mapping=mapping):
                self.assertEqual(closeout.match_criterion(criteria, mapping), expected)
        for mapping in ({"criterion": "Unrelated performance budget"}, {"criterion": "9"}, {}, "not a mapping"):
            with self.subTest(mapping=mapping):
                self.assertIsNone(closeout.match_criterion(criteria, mapping))
        packet = {"scope": {"acceptance_criteria": criteria}, "files": {"src/app.txt": "a" * 64},
                  "contributions": [{"id": "author-task", "state": "complete"}],
                  "verification": {"status": "not_configured"}}
        mappings = [
            {"criterion": "1", "evidence_refs": ["file: ./src/App.txt"]},
            {"criterion": "original objective is satisfied", "evidence_refs": "src\\app.txt"},
            {"criterion": "every required task is complete.", "evidence_refs": ["task:author-task"]},
        ]
        self.assertEqual(closeout.evidence_notes(packet, mappings), [])
        self.assertEqual(len(closeout.evidence_notes(packet, [
            {"criterion": "1", "evidence_refs": ["test:verified"]}])), 3)

    def test_formatting_mismatch_never_vetoes_an_approved_closeout(self):
        runtime, goal, root = self.prepared()
        task = self.judge(runtime, goal)
        answer = self.verdict(task)
        answer["criteria_evidence"] = [{"criterion": str(i + 1), "evidence_refs": ["file:./APP.txt"]}
            for i in range(len(task["closeout_packet"]["scope"]["acceptance_criteria"]))]
        answer.pop("review_verdict")
        answer["evidence"] = ["Inspected app.txt"]
        accepted = self.execute(runtime, goal, task, answer)
        self.assertTrue(closeout.approved(accepted, root))
        self.assertNotIn("evidence_notes", accepted["tasks"][-1]["closeout_outcome"])
        self.assertEqual(self.verify(runtime, goal), {"route": "end"})
        self.assertEqual(runtime.store.get(goal["goal_id"])["status"], "complete")

    def test_verdict_and_action_mismatch_follows_the_judges_explicit_verdict(self):
        runtime, goal, root = self.prepared()
        task = self.judge(runtime, goal)
        rejection = self.verdict(task, approve=False)
        rejection["action"] = "complete"
        rejected = self.execute(runtime, goal, task, rejection)
        self.assertEqual(rejected["tasks"][-1]["closeout_outcome"]["verdict"], "changes_requested")
        self.assertEqual(rejected["tasks"][-1]["state"], "complete")
        self.assertFalse(closeout.approved(rejected, root))
        self.verify(runtime, goal)
        repair = runtime.store.claim_ready(goal["goal_id"], "repair")[0]
        (root / "guide.txt").write_text("Usage guide")
        runtime.store.apply_action(goal["goal_id"], repair, action(), artifact={
            "kind": "file_transaction", "transaction_id": "mismatch-tx", "patch_sha256": "f" * 64,
            "changes": [{"path": "guide.txt", "delete": False}]})
        final = self.judge(runtime, goal)
        # An approve verdict that conflicts with a blocked action is not an
        # approval either: only an unambiguous approval approves.
        conflicted = self.verdict(final)
        conflicted["action"] = "blocked"
        conflicted["review_findings"] = ["Looks fine but I am not sure"]
        held = self.execute(runtime, goal, final, conflicted)
        self.assertEqual(held["tasks"][-1]["closeout_outcome"]["verdict"], "changes_requested")
        self.assertFalse(closeout.approved(held, root))

    def test_unaccepted_verdict_is_feedback_and_the_goal_keeps_running(self):
        runtime, goal, _root = self.prepared(objective="Update app.txt")
        self.assertEqual(self.verify(runtime, goal), {"route": "schedule"})
        task = runtime.store.claim_ready(goal["goal_id"], "judge")[0]
        self.assertTrue(task.get("closeout_packet"))
        with patch.object(closeout, "validate_action",
                          side_effect=HarnessError("Closeout lacks concrete evidence for: the criterion")):
            current = self.execute(runtime, goal, task, self.verdict(task))
        judged = next(one for one in current["tasks"] if one["id"] == task["id"])
        self.assertEqual(judged["state"], "ready")
        self.assertIn("closeout verdict was not accepted", judged["last_error"])
        self.assertIn("lacks concrete evidence", judged["last_error"])
        self.assertNotIn(current["status"], {"failed", "cancelled", "complete"})
        events = runtime.store.events(goal["goal_id"])["events"]
        self.assertFalse(any(one["type"] in {"task_failed", "goal_failed"} for one in events))
        self.assertEqual((self.project / "app.txt").read_text(), "original")

    def test_repeated_unacceptable_verdicts_pause_after_the_correction_cap(self):
        runtime, goal, _root = self.prepared(objective="Update app.txt")
        self.assertEqual(self.verify(runtime, goal), {"route": "schedule"})
        held = runtime.store.get(goal["goal_id"])
        for attempt in range(lh.MAX_CLOSEOUT_CORRECTIONS):
            task = runtime.store.claim_ready(goal["goal_id"], "judge")[0]
            self.assertTrue(task.get("closeout_packet"))
            with patch.object(closeout, "validate_action", side_effect=HarnessError("unusable verdict")):
                held = self.execute(runtime, goal, task, self.verdict(task))
            if attempt < lh.MAX_CLOSEOUT_CORRECTIONS - 1:
                self.assertNotEqual(held["status"], "paused", held["note"])
        self.assertEqual(held["status"], "paused")
        self.assertIn("could not be accepted", held["note"])
        self.assertEqual((self.project / "app.txt").read_text(), "original")
        self.assertEqual(runtime.store.claim_ready(goal["goal_id"], "judge"), [])
        # Restart keeps the pause and the work.
        self.assertEqual(lh.GoalStore(self.config).get(goal["goal_id"])["status"], "paused")
        # Resume resets the count: one more unusable verdict does not re-pause.
        resumed = runtime.store.control(goal["goal_id"], "resume")
        self.assertFalse(any(one.get("closeout_corrections") for one in resumed["tasks"]))
        task = runtime.store.claim_ready(goal["goal_id"], "judge")[0]
        with patch.object(closeout, "validate_action", side_effect=HarnessError("unusable verdict")):
            held = self.execute(runtime, goal, task, self.verdict(task))
        self.assertNotEqual(held["status"], "paused", held["note"])

    def test_closeout_verdict_format_is_owned_by_goal_closeout(self):
        # The generic review-packet check does not second-guess a verdict that
        # goal_closeout.validate_action accepted.
        runtime, goal, _root = self.prepared(objective="Update app.txt")
        self.assertEqual(self.verify(runtime, goal), {"route": "schedule"})
        task = runtime.store.claim_ready(goal["goal_id"], "judge")[0]
        verdict = self.verdict(task)
        verdict["evidence"] = ["Inspected app.txt"]  # no generic review-packet reference
        with patch.object(closeout, "validate_action", return_value=None):
            current = self.execute(runtime, goal, task, verdict)
        judged = next(one for one in current["tasks"] if one["id"] == task["id"])
        self.assertEqual(judged["state"], "complete", judged.get("last_error"))

    def test_new_cache_files_are_reported_in_the_goal_note_and_not_published(self):
        runtime, goal, root = self.prepared(objective="Update app.txt")
        (root / ".cache" / "tool").mkdir(parents=True)
        (root / ".cache" / "tool" / "state.bin").write_text("tool litter")
        task = self.judge(runtime, goal)
        self.execute(runtime, goal, task, self.verdict(task))
        self.assertEqual(self.verify(runtime, goal), {"route": "end"})
        done = runtime.store.get(goal["goal_id"])
        self.assertEqual(done["status"], "complete", done["note"])
        self.assertIn("1 new cache file was not published", done["note"])
        self.assertEqual(done["workspace_publication"]["held_back_cache_files"], [".cache/tool/state.bin"])
        self.assertEqual((self.project / "app.txt").read_text(), "submitted program")
        self.assertFalse((self.project / ".cache").exists())

    def test_unrecognised_or_qualified_verdicts_never_approve(self):
        runtime, goal, root = self.prepared(objective="Update app.txt")
        task = self.judge(runtime, goal)
        for wording in ("not_approved", "changes_required", "needs_revision", "incomplete", "denied",
                        "Approve with changes"):
            with self.subTest(wording=wording):
                answer = self.verdict(task)
                answer["review_verdict"] = wording
                closeout.validate_action(runtime.store.get(goal["goal_id"]), copy.deepcopy(task), answer, root)
                self.assertEqual(answer["review_verdict"], "changes_requested")
                self.assertEqual(answer["action"], "blocked")
        for wording, expected in ((None, "approve"), ("", "approve"), ("Approved", "approve")):
            with self.subTest(wording=wording):
                answer = self.verdict(task)
                answer["review_verdict"] = wording
                closeout.validate_action(runtime.store.get(goal["goal_id"]), copy.deepcopy(task), answer, root)
                self.assertEqual(answer["review_verdict"], expected)

    def test_closeout_snapshot_matches_projects_with_generated_files_and_links(self):
        # Every inventory shares one exclusion rule, so a project holding
        # stray bytecode, a virtual environment, tool caches and a link still
        # reaches its judge instead of failing the snapshot comparison forever.
        runtime, goal, root = self.prepared(objective="Update app.txt")
        (root / "main.py").write_text("print('hi')\n", encoding="utf-8")
        (root / "legacy.pyc").write_bytes(b"\x00bytecode")
        (root / "env").mkdir()
        (root / "env" / "pyvenv.cfg").write_text("home = anywhere", encoding="utf-8")
        (root / "env" / "tool.py").write_text("x = 1", encoding="utf-8")
        for folder in (".yarn-cache", ".pnpm-store", ".hypothesis"):
            (root / folder).mkdir()
            (root / folder / "a").write_text("cache", encoding="utf-8")
        outside = self.base / "outside-link-target"
        outside.mkdir()
        try:
            (root / "linked").symlink_to(outside, target_is_directory=True)
        except OSError:
            pass  # Symbolic links need a privilege on some Windows machines.
        self.assertEqual(sorted(aw.inventory(root)), sorted(gw._manifest(root)))
        task = self.judge(runtime, goal)
        with closeout.workspace(runtime.store.get(goal["goal_id"]), task, runtime.store.root) as snapshot:
            self.assertEqual(aw.inventory(snapshot.root), task["closeout_packet"]["files"])
        accepted = self.execute(runtime, goal, task, self.verdict(task))
        self.assertTrue(closeout.approved(accepted, root))

    def test_snapshot_failure_pauses_without_asking_the_judge_to_correct(self):
        runtime, goal, _root = self.prepared(objective="Update app.txt")
        task = self.judge(runtime, goal)
        verdict = self.verdict(task)
        def ask(_config, _route, _prompt, **kwargs):
            kwargs["before_provider_dispatch"]("initial")
            kwargs["after_provider_response"]("initial")
            return {"text": json.dumps(verdict)}
        with patch.object(lh.chat_lab, "ask_once", side_effect=ask):
            claimed, proposal = runtime._execute_one(goal["goal_id"], task["id"])
        with patch.object(closeout, "workspace", side_effect=HarnessError("Closeout snapshot differs from the exact submitted files")):
            runtime._apply_node({"goal_id": goal["goal_id"], "actions": [{"task": claimed, "action": proposal}]})
        held = runtime.store.get(goal["goal_id"])
        judged = next(one for one in held["tasks"] if one["id"] == task["id"])
        self.assertEqual(held["status"], "paused")
        self.assertIn("could not prepare the closeout judge's inspection snapshot", held["note"])
        self.assertEqual(judged["state"], "pending_apply")
        self.assertTrue(judged["pending_action"])
        self.assertNotIn("closeout verdict was not accepted", str(judged.get("last_error") or ""))
        # The provider is not called again for a harness-side failure.
        with patch.object(closeout, "workspace", side_effect=HarnessError("snapshot unavailable")), \
                patch.object(lh.chat_lab, "ask_once") as provider:
            _task, deferred = runtime._execute_one(goal["goal_id"], task["id"])
        provider.assert_not_called()
        self.assertEqual(deferred["action"], "deferred")

    def test_restart_preserves_complete_prompt_amendments_and_requested_file_context(self):
        runtime, goal, root = self.prepared()
        original = "First requirement. " * 2000 + "FINAL REQUIREMENT AT END"
        def update(g, db):
            g.update(original_objective=original, objective=original + "\nLater clarification: preserve the API.",
                     objective_revisions=[{"kind": "steer", "text": "Preserve the API exactly."}])
        runtime.store._mutate(goal["goal_id"], update)
        task = self.judge(runtime, goal)
        with closeout.workspace(runtime.store.get(goal["goal_id"]), task, runtime.store.root) as first:
            snapshot = first.root
        restarted = lh.LongHorizonRuntime(self.config)
        self.addCleanup(restarted.close)
        saved = restarted.store.get(goal["goal_id"])
        with closeout.workspace(saved, task, restarted.store.root) as second:
            self.assertEqual(snapshot, second.root)
            text = restarted._agent_context(saved, task, ["app.txt"], workspace_root=second.root)
        self.assertIn(original, text)
        self.assertIn("Preserve the API exactly.", text)
        self.assertIn("submitted program", text)

    def test_repair_reminder_preserves_findings_beyond_short_task_description(self):
        runtime, goal, root = self.prepared()
        task = self.judge(runtime, goal)
        answer = self.verdict(task, approve=False)
        answer["review_findings"] = [f"Finding {i}: " + "Missing required work. " * 50 for i in range(8)]
        answer["review_findings"].append("Final finding: the required final deliverable is missing.")
        self.execute(runtime, goal, task, answer)
        self.verify(runtime, goal)
        repair = runtime.store.claim_ready(goal["goal_id"], "repair")[0]
        saved = lh.GoalStore(self.config).get(goal["goal_id"])
        reminder = runtime._agent_context(saved, repair)
        for finding in answer["review_findings"]:
            self.assertIn(finding, reminder)
        self.assertIn("Do not call the work complete after fixing only one part", reminder)

    def test_stale_submission_or_scope_cannot_reuse_approval(self):
        runtime, goal, root = self.prepared()
        task = self.judge(runtime, goal)
        accepted = self.execute(runtime, goal, task, self.verdict(task))
        self.assertTrue(closeout.approved(accepted, root))
        for key, value in [("objective", "A changed request"), ("execution_contract", {"changed": True}),
                           ("verification_contract", {"changed": True}), ("closeout_contract", "future")]:
            changed = copy.deepcopy(accepted)
            changed[key] = value
            if key == "closeout_contract":
                with self.assertRaises(HarnessError): closeout.enabled(changed)
            else:
                self.assertFalse(closeout.approved(changed, root))
        changed = copy.deepcopy(accepted)
        # Any change of the saved access decision invalidates the approval
        # (goals now default to full access, so change it to ask).
        changed["agent_access"]["mode"] = "ask" if accepted["agent_access"]["mode"] == "full" else "full"
        self.assertFalse(closeout.approved(changed, root))
        (root / "app.txt").write_text("later unreviewed edits")
        self.assertFalse(closeout.approved(accepted, root))
        new = self.judge(runtime, goal)
        (root / "app.txt").write_text("changed again")
        with patch.object(lh.chat_lab, "ask_once") as ask:
            _, answer = runtime._execute_one(goal["goal_id"], new["id"])
        ask.assert_not_called()
        self.assertEqual(answer["action"], "superseded")
        self.assertEqual(runtime.store.get(goal["goal_id"])["tasks"][-1]["state"], "cancelled")
        self.assertEqual((self.project / "app.txt").read_text(), "original")

    def test_modified_snapshot_is_rejected_after_restart(self):
        runtime, goal, root = self.prepared()
        task = self.judge(runtime, goal)
        with self.assertRaisesRegex(HarnessError, "modified"), closeout.workspace(runtime.store.get(goal["goal_id"]), task, runtime.store.root) as snapshot:
            (snapshot.root / "app.txt").write_text("judge edited its evidence")
        with self.assertRaisesRegex(HarnessError, "differs"), closeout.workspace(runtime.store.get(goal["goal_id"]), task, runtime.store.root):
            pass
        self.assertEqual((root / "app.txt").read_text(), "submitted program")

    def test_changes_during_verification_do_not_rebind_old_test_results_to_new_submission(self):
        runtime, goal, root = self.prepared()
        def verify(*args, **kwargs):
            (root / "app.txt").write_text("changed while tests ran")
            return {"status": "passed"}
        with patch.object(lh.swarm_work, "_run_selected_project_verification", side_effect=verify):
            self.assertEqual(runtime._verify_node({"goal_id": goal["goal_id"]}), {"route": "schedule"})
        self.assertFalse(any(t.get("closeout_packet") for t in runtime.store.get(goal["goal_id"])["tasks"]))
        task = self.judge(runtime, goal)
        self.assertEqual(task["closeout_packet"]["files"], aw.inventory(root))
        self.assertEqual((self.project / "app.txt").read_text(), "original")

    def test_maximum_accepted_scope_fits_the_provider_schema_and_can_complete(self):
        runtime, goal, root = self.prepared(success_criteria=[f"User requirement {i}" for i in range(lh.MAX_CRITERIA - len(lh.BASELINE_CRITERIA))])
        task = self.judge(runtime, goal)
        self.assertEqual(len(task["closeout_packet"]["scope"]["acceptance_criteria"]),
                         lh.MAX_CRITERIA + 2)
        accepted = self.execute(runtime, goal, task, self.verdict(task))
        self.assertTrue(closeout.approved(accepted, root))
        self.assertEqual(self.verify(runtime, goal), {"route": "end"})
        self.assertEqual(runtime.store.get(goal["goal_id"])["status"], "complete")

    def test_pending_judgment_is_discarded_when_submission_changes_before_apply(self):
        runtime, goal, root = self.prepared()
        task = self.judge(runtime, goal)
        def ask(_config, _route, _prompt, **kwargs):
            kwargs["before_provider_dispatch"]("initial")
            kwargs["after_provider_response"]("initial")
            return {"text": json.dumps(self.verdict(task))}
        with patch.object(lh.chat_lab, "ask_once", side_effect=ask):
            claimed, proposal = runtime._execute_one(goal["goal_id"], task["id"])
        (root / "app.txt").write_text("new unreviewed content")
        runtime._apply_node({"goal_id": goal["goal_id"], "actions": [{"task": claimed, "action": proposal}]})
        held = runtime.store.get(goal["goal_id"])
        self.assertEqual(held["tasks"][-1]["state"], "cancelled")
        self.assertFalse(closeout.approved(held, root))
        self.assertEqual((self.project / "app.txt").read_text(), "original")

    def test_single_provider_gets_fresh_judge_and_repeated_findings_never_pause_the_agents(self):
        runtime, goal, root = self.prepared(single=True)
        for turn in range(closeout.REPEATED_REJECTION_NOTICE + 1):
            task = self.judge(runtime, goal)
            self.assertEqual(task["assigned_agent_id"], "lead")
            if turn:
                self.assertEqual(task["closeout_packet"]["previous_rejections_of_this_submission"], turn)
                self.assertIn("Earlier judges requested changes to this same submission", closeout.context(task))
            self.execute(runtime, goal, task, self.verdict(task, approve=False))
            self.verify(runtime, goal)
            repair = runtime.store.claim_ready(goal["goal_id"], "repair")[0]
            runtime.store.apply_action(goal["goal_id"], repair, action(summary=f"Still incomplete {turn}"),
                artifact={"kind": "verified_no_change", "tree_merkle": "a" * 64})
        # The same unfinished result keeps receiving fresh judgment; the user
        # sees a notice and can pause, but Nexus itself does not stop the work.
        self.assertEqual(self.verify(runtime, goal), {"route": "schedule"})
        held = runtime.store.get(goal["goal_id"])
        self.assertEqual(held["status"], "queued")
        self.assertIn("the agents keep working", held["note"])
        events = runtime.store.events(goal["goal_id"])["events"]
        self.assertTrue(any(item["type"] == "closeout_repeated_findings" for item in events))
        self.assertFalse(any(item["type"] == "goal_paused" for item in events))
        self.assertTrue(runtime.store.claim_ready(goal["goal_id"], "judge")[0].get("closeout_packet"))
        self.assertEqual((self.project / "app.txt").read_text(), "original")

    def test_shared_chat_graph_adds_judge_after_both_agents_without_inventing_test_requirement(self):
        (self.project / "report.txt").write_text("The complete requested report.")
        runtime = lh.LongHorizonRuntime(self.config)
        self.addCleanup(runtime.close)
        goal = runtime.store.create(self.board, "project", ["Make the requested report together and agree on the finished document"],
            "shared-closeout", isolated_workspace=True, participant_ids=["lead", "reviewer"], conversation_id="report-chat")
        calls = []
        def ask(_config, route, _prompt, **kwargs):
            context = kwargs["context"]
            calls.append((route, kwargs["conversation_key"], context))
            kwargs["before_provider_dispatch"]("initial")
            kwargs["after_provider_response"]("initial")
            if context.startswith("INDEPENDENT WHOLE-GOAL CLOSEOUT JUDGE"):
                packet, _ = json.JSONDecoder().raw_decode(context[context.index("{"):])
                self.assertEqual(packet["verification"]["status"], "not_configured")
                answer = action(review_verdict="approve", review_findings=["The report satisfies the request. No tests were configured or run."],
                    evidence=["review-packet:" + packet["fingerprint"]],
                    criteria_evidence=[{"criterion": c, "evidence_refs": ["file:report.txt"]}
                        for c in packet["scope"]["acceptance_criteria"]])
            else:
                answer = action(criteria_evidence=[{"criterion": "Original objective is satisfied", "evidence_refs": ["verified-no-change"]}])
            return {"text": json.dumps(answer)}
        with patch.object(lh.chat_lab, "ask_once", side_effect=ask):
            completed = runtime.run(goal["goal_id"])
        self.assertEqual(completed["status"], "complete", completed["note"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(len({key for _, key, _ in calls}), 3)
        self.assertEqual(completed["verification"]["status"], "not_configured")
        self.assertTrue(closeout.approved(runtime.store.get(goal["goal_id"]), gw.root(goal, runtime.store.root)))


if __name__ == "__main__":
    unittest.main()
