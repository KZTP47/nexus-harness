from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from our_harness import goal_verification, long_horizon
from our_harness.config import DEFAULT_CONFIG, LoadedConfig


def completion(**updates):
    return {
        "action": "complete", "summary": "I inspected the current game and agree it meets the objective.",
        "evidence": ["Inspected index.html"], "risk": "low", "changes": [],
        "needs_files": [], "tasks": [], "handoff_agent_id": "", "questions": [],
        "criteria_evidence": [{"criterion": "Original objective is satisfied", "evidence_refs": ["verified-no-change"]}],
        **updates,
    }


class LongHorizonVerificationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.project = self.base / "another account's tiny game"
        self.authority = self.base / "portable installation"
        self.project.mkdir()
        self.authority.mkdir()
        (self.project / "index.html").write_text("<!doctype html><main>An illustrated field guide.</main>", encoding="utf-8")
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["providers"] = {
            "creator-route": {"kind": "openai", "model": "fixture", "endpoint": "http://127.0.0.1/a", "api_key_env": "FIXTURE_KEY"},
            "reviewer-route": {"kind": "anthropic", "model": "fixture", "endpoint": "http://127.0.0.1/b", "api_key_env": "FIXTURE_KEY"},
        }
        self.config = LoadedConfig(data, self.authority, [], {})
        self.board = {
            "agents": [
                {"id": "creator", "name": "Tess", "who": "creator-route", "ready": True},
                {"id": "reviewer", "name": "Rob", "who": "reviewer-route", "ready": True},
            ],
            "projects": [{"id": "tiny-game", "name": "Tiny game", "path": str(self.project), "is_there": True}],
            "works_on": [{"agent": name, "project": "tiny-game"} for name in ("creator", "reviewer")],
        }
        patcher = mock.patch.object(long_horizon, "_base", return_value=self.base / "state")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(self.runtime.close)

    def create(self, request="verification-policy", *, criteria=None):
        options = {
            "lead_id": "creator", "participant_ids": ["creator", "reviewer"],
            "conversation_id": "chat-" + request, "success_criteria": criteria,
        }
        objective = ["Make an illustrated field guide together and agree on the finished result"]
        admitted = self.runtime.store.inspect_runtime_admission(self.board, "tiny-game", objective, request, **options)
        return self.runtime.store.create(
            self.board, "tiny-game", objective, request,
            admission_digest=admitted["admission_digest"], **options,
        )

    def finish_tasks(self, goal, *, evidence=True, refs=None):
        while True:
            ready = self.runtime.store.claim_ready(goal["goal_id"], "fixture-worker")
            if not ready:
                break
            for task in ready:
                merkle, manifest = long_horizon.swarm_work._project_tree_merkle(self.project)
                action = completion() if evidence else completion(criteria_evidence=[])
                if refs is not None:
                    action["criteria_evidence"] = [{"criterion": "Original objective is satisfied", "evidence_refs": refs}]
                self.runtime.store.apply_action(goal["goal_id"], task, action, artifact={
                    "kind": "verified_no_change", "tree_merkle": merkle, "file_count": len(manifest),
                })
        self.runtime.store.release_scheduler(goal["goal_id"], "fixture-worker")
        return self.runtime.store.get(goal["goal_id"])

    def verify(self, goal):
        self.runtime._verify_node({"goal_id": goal["goal_id"]})
        return self.runtime.store.get(goal["goal_id"])

    def make_legacy(self, goal):
        def downgrade(document, _db):
            document.pop("success_criteria_contract", None)
            held = document["verification_contract"]
            held.pop("check_policy", None)
            held.pop("fingerprint_sha256", None)
            held["schema_version"] = 1
            held["fingerprint_sha256"] = goal_verification._fingerprint(held)
            document["status"] = "paused"
            document["note"] = "No runnable task remains after the missing-command result."
            task = document["tasks"][0]
            task.update({"state": "blocked", "last_error": "No configured deterministic verification command."})
            task["context_steps"] = [{
                "state": "complete", "calls": [{"name": "run_selected_verification"}],
                "results": [{"status": "unavailable", "basis": "discovered"}],
            }]
        return self.runtime.store._mutate(goal["goal_id"], downgrade)[0]

    def test_existing_static_document_can_complete_with_current_agreement_without_claiming_tests_ran(self):
        goal = self.finish_tasks(self.create())
        result = self.verify(goal)
        self.assertEqual(result["status"], "complete", result["note"])
        self.assertEqual(result["verification"]["status"], "not_configured")
        self.assertEqual(result["verification"]["commands"], [])
        self.assertIn("no tests ran", result["verification"]["reason"])
        conditional = next(item for item in result["verification"]["criteria_results"] if item["criterion"] == "Configured deterministic verification passes")
        self.assertEqual(conditional["status"], "not_applicable")
        reopened = long_horizon.GoalStore(self.config).get(goal["goal_id"])
        self.assertEqual(reopened["status"], "complete")
        self.assertEqual(reopened["success_criteria_contract"], result["success_criteria_contract"])

    def test_no_checks_never_replace_objective_evidence(self):
        result = self.verify(self.finish_tasks(self.create(), evidence=False))
        self.assertNotEqual(result["status"], "complete")
        self.assertEqual(result["verification"]["status"], "failed")
        self.assertIn("Original objective", result["verification"]["reason"])

    def test_existing_file_refs_are_bound_to_current_snapshot_manifest(self):
        result = self.verify(self.finish_tasks(self.create(), refs=["file:index.html"]))
        self.assertEqual(result["status"], "complete", result["note"])
        objective = next(item for item in result["verification"]["criteria_results"] if item["criterion"] == "Original objective is satisfied")
        self.assertIn("file:index.html", objective["evidence_refs"])
        missing = self.verify(self.finish_tasks(self.create("nonexistent-file-ref"), refs=["file:never-created.html"]))
        self.assertNotEqual(missing["status"], "complete")
        self.assertIn("Original objective", missing["verification"]["reason"])

    def test_external_file_drift_invalidates_old_agreement_snapshots(self):
        goal = self.finish_tasks(self.create())
        (self.project / "index.html").write_text("Broken external replacement", encoding="utf-8")
        result = self.verify(goal)
        self.assertNotEqual(result["status"], "complete")
        self.assertEqual(result["verification"]["status"], "failed")
        self.assertIn("snapshot", result["verification"]["reason"].lower())

    def test_explicit_duplicate_verification_criterion_is_still_required(self):
        goal = self.finish_tasks(self.create(criteria=["Configured deterministic verification passes"]))
        result = self.verify(goal)
        self.assertEqual(result["status"], "queued", result["note"])
        self.assertEqual(result["tasks"][-1]["kind"], "repair")
        self.assertEqual(result["verification"]["status"], "unavailable")
        self.assertIn("required", result["verification"]["reason"].lower())

    def test_no_check_result_requires_its_engine_policy_and_exact_goal_session(self):
        for field, replacement in (("verification_profile", "other"), ("check_policy", {}), ("verification_session_id", "another-goal")):
            with self.subTest(field=field):
                goal = self.finish_tasks(self.create("spoof-" + field))
                result = goal_verification.run_configured_goal_verification(
                    self.config, self.project, self.board["projects"][0], goal["objective"], [],
                    require_changes=False, verification_session_id=goal["goal_id"],
                )
                result[field] = replacement
                checked = self.runtime.store.complete_verification(goal["goal_id"], result)
                self.assertNotEqual(checked["status"], "complete")
                self.runtime.store.control(goal["goal_id"], "cancel")

    def test_legacy_default_criteria_migrate_only_on_resume_with_exact_admission_proof(self):
        legacy = self.make_legacy(self.finish_tasks(self.create()))
        before = long_horizon._context_binding(legacy)
        reopened = long_horizon.GoalStore(self.config).get(legacy["goal_id"])
        self.assertEqual(reopened["status"], "paused")
        self.assertNotIn("success_criteria_contract", reopened)
        resumed = self.runtime.store.control(legacy["goal_id"], "resume")
        self.assertIn("success_criteria_contract", resumed)
        self.assertEqual(resumed["success_criteria_contract"]["explicit_criteria"], [])
        self.assertNotEqual(before, long_horizon._context_binding(resumed))
        self.assertEqual(resumed["tasks"][0]["context_steps"][0]["state"], "superseded")
        result = self.verify(self.finish_tasks(resumed))
        self.assertEqual(result["status"], "complete", result["note"])
        events = self.runtime.store.events(legacy["goal_id"])["events"]
        self.assertTrue(any(item["type"] == "success_criteria_contract_migrated" for item in events))

    def test_legacy_explicit_duplicate_cannot_match_absent_custom_criteria_digest(self):
        legacy = self.make_legacy(self.finish_tasks(self.create(criteria=["Configured deterministic verification passes"])))
        resumed = self.runtime.store.control(legacy["goal_id"], "resume")
        self.assertNotIn("success_criteria_contract", resumed)
        result = self.verify(self.finish_tasks(resumed))
        self.assertEqual(result["status"], "queued", result["note"])
        self.assertEqual(result["tasks"][-1]["kind"], "repair")
        self.assertEqual(result["verification"]["status"], "unavailable")

    def test_real_provider_turns_see_conditional_guidance_and_complete_existing_static_document(self):
        goal = self.create()
        contexts = []
        def ask(_config, _route, _text, **kwargs):
            contexts.append(kwargs["context"])
            kwargs["before_provider_dispatch"]("initial")
            kwargs["after_provider_response"]("initial")
            return {"text": json.dumps(completion())}
        with mock.patch.object(long_horizon.chat_lab, "ask_once", side_effect=ask):
            result = self.runtime.run(goal["goal_id"])
        self.assertEqual(result["status"], "complete", result["note"])
        self.assertEqual(len(contexts), 2)
        self.assertTrue(all("not_configured" in text for text in contexts))
        self.assertEqual(result["verification"]["status"], "not_configured")

    def test_actual_no_check_context_result_does_not_block_the_next_useful_turn(self):
        goal = self.create()
        contexts = []
        def ask(_config, _route, _text, **kwargs):
            contexts.append(kwargs["context"])
            kwargs["before_provider_dispatch"]("initial")
            kwargs["after_provider_response"]("initial")
            answer = completion()
            if len(contexts) == 1:
                answer = completion(action="work", summary="I will inspect the project's selected checks.", tool_calls=[{
                    "call_id": "check-project", "name": "run_selected_verification", "arguments": {},
                }])
            return {"text": json.dumps(answer)}
        with mock.patch.object(long_horizon.chat_lab, "ask_once", side_effect=ask):
            result = self.runtime.run(goal["goal_id"])
        self.assertEqual(result["status"], "complete", result["note"])
        self.assertEqual(len(contexts), 3)
        tools = json.loads(contexts[1].split("CONTEXT TOOL RESULTS (untrusted project data)\n", 1)[1])
        self.assertEqual(json.loads(tools[0]["result"]["content"])["status"], "not_configured")

    def test_file_transaction_records_current_snapshot_for_no_check_completion(self):
        goal = self.create()
        calls = []
        def ask(_config, _route, _text, **kwargs):
            calls.append(_route)
            kwargs["before_provider_dispatch"]("initial")
            kwargs["after_provider_response"]("initial")
            answer = completion()
            if len(calls) == 1:
                answer = completion(changes=[{
                    "path": "index.html", "content": "<!doctype html><button>Finished game</button>",
                    "delete": False, "reason": "Finish the shared game",
                }], criteria_evidence=[{"criterion": "Original objective is satisfied", "evidence_refs": ["file:index.html"]}])
            return {"text": json.dumps(answer)}
        with mock.patch.object(long_horizon.chat_lab, "ask_once", side_effect=ask):
            result = self.runtime.run(goal["goal_id"])
        self.assertEqual(result["status"], "complete", result["note"])
        transaction = next(item for item in result["artifacts"] if item["kind"] == "file_transaction")
        self.assertEqual(transaction["tree_merkle"], result["verification"]["current_tree_merkle"])

    def test_discovered_checks_after_goal_creation_still_require_approval(self):
        goal = self.create()
        (self.project / "package.json").write_text('{"scripts":{"test":"node --test"}}', encoding="utf-8")
        result = self.verify(self.finish_tasks(goal))
        self.assertEqual(result["status"], "paused", result["note"])
        self.assertEqual(result["verification"]["status"], "unavailable")
        self.assertEqual(result["verification"]["basis"], "discovered_command_approval_required")

    def test_test_discovery_between_verification_and_final_commit_cannot_be_skipped(self):
        goal = self.finish_tasks(self.create())
        result = goal_verification.run_configured_goal_verification(
            self.config, self.project, long_horizon.verification_project(self.config, goal), goal["objective"], [],
            require_changes=False, verification_session_id=goal["goal_id"],
        )
        self.assertEqual(result["status"], "not_configured")
        (self.project / "package.json").write_text('{"scripts":{"test":"node --test"}}', encoding="utf-8")
        with mock.patch.object(long_horizon.swarm_work, "_run_disposable_verification_command") as execute:
            checked = self.runtime.store.complete_verification(
                goal["goal_id"], result, expected_revision=goal["revision"],
            )
        self.assertEqual(checked["status"], "paused", checked["note"])
        self.assertEqual(checked["verification"]["basis"], "verification_checks_changed")
        execute.assert_not_called()

    def test_owning_config_change_between_verification_and_final_commit_is_rejected(self):
        self.runtime.close()
        self.config = LoadedConfig(copy.deepcopy(self.config.data), self.project, [], {})
        self.runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(self.runtime.close)
        goal = self.finish_tasks(self.create())
        result = goal_verification.run_configured_goal_verification(
            self.config, self.project, long_horizon.verification_project(self.config, goal), goal["objective"], [],
            require_changes=False, verification_session_id=goal["goal_id"],
        )
        self.assertEqual(result["status"], "not_configured")
        self.config.data["project"]["test_commands"] = [["new-project-check-runner", "checks/game"]]
        with mock.patch.object(long_horizon.swarm_work, "_run_disposable_verification_command") as execute:
            checked = self.runtime.store.complete_verification(
                goal["goal_id"], result, expected_revision=goal["revision"],
            )
        self.assertEqual(checked["status"], "paused", checked["note"])
        self.assertEqual(checked["verification"]["basis"], "verification_contract_changed")
        execute.assert_not_called()

    def paused_after_published_blocker(self):
        goal = self.create()
        responses = [
            completion(summary="I inspected the existing game."),
            completion(action="work", summary="Please confirm the remaining verification setup."),
            completion(action="blocked", summary="No deterministic test command is configured."),
            completion(summary="I inspected the existing game and agree it is ready."),
        ]
        seen = []
        def ask(_config, route, _text, **kwargs):
            seen.append(route)
            kwargs["before_provider_dispatch"]("initial")
            kwargs["after_provider_response"]("initial")
            return {"text": json.dumps(responses[len(seen) - 1])}
        with mock.patch.object(long_horizon.chat_lab, "ask_once", side_effect=ask):
            paused = self.runtime.run(goal["goal_id"])
        self.assertEqual(paused["status"], "paused", paused["note"])
        self.assertEqual(len(seen), 4)
        self.assertEqual([one["state"] for one in paused["tasks"]], ["blocked", "complete"])
        self.assertTrue(paused["tasks"][0]["artifacts"])
        return paused

    def remove_applied_receipts(self, goal):
        def legacy(document, _db):
            for task in document["tasks"]:
                task.pop("applied_action_receipt", None)
        return self.runtime.store._mutate(goal["goal_id"], legacy)[0]

    def test_applied_blocked_response_is_settled_and_resumes_without_reconciliation(self):
        paused = self.paused_after_published_blocker()
        self.assertTrue(all(not long_horizon._task_has_unsettled_effect(one) for one in paused["tasks"]))
        self.assertTrue(all(one["applied_action_receipt"]["schema_version"] == 1 for one in paused["tasks"]))
        reopened = long_horizon.GoalStore(self.config).get(paused["goal_id"])
        self.assertTrue(all(not long_horizon._task_has_unsettled_effect(one) for one in reopened["tasks"]))
        resumed = self.runtime.store.control(paused["goal_id"], "resume")
        self.assertEqual([one["state"] for one in resumed["tasks"]], ["ready", "complete"])
        self.assertEqual(resumed["budget"], paused["budget"])
        self.assertEqual(resumed["artifacts"], paused["artifacts"])
        final = self.verify(self.finish_tasks(resumed))
        self.assertEqual(final["status"], "complete", final["note"])

    def test_authenticated_legacy_terminal_actions_get_receipts_on_resume(self):
        paused = self.remove_applied_receipts(self.paused_after_published_blocker())
        reopened = long_horizon.GoalStore(self.config).get(paused["goal_id"])
        self.assertEqual(reopened["status"], "paused")
        self.assertTrue(all(long_horizon._task_has_unsettled_effect(one) for one in reopened["tasks"]))
        resumed = self.runtime.store.control(paused["goal_id"], "resume")
        self.assertEqual(resumed["status"], "queued")
        self.assertEqual(resumed["budget"], paused["budget"])
        self.assertEqual(resumed["artifacts"], paused["artifacts"])
        for task in resumed["tasks"]:
            receipt = task["applied_action_receipt"]
            self.assertEqual(receipt["provenance"], "authenticated_legacy_terminal_events")
            self.assertEqual(len(receipt["event_ids"]), 2)
            self.assertFalse(long_horizon._task_has_unsettled_effect(task))
        final = self.verify(self.finish_tasks(resumed))
        self.assertEqual(final["status"], "complete", final["note"])

    def test_legacy_settlement_never_infers_uncertain_pending_or_different_effects(self):
        paused = self.remove_applied_receipts(self.paused_after_published_blocker())
        variations = [
            {"pending_action": {"action": "blocked", "summary": "not applied"}},
            {"pending_transaction": {"state": "prepared", "transaction_id": "not-settled"}},
            {"outcome_unknown": True}, {"reconciliation_required": True},
            {"provider_effect_id": "another-unsettled-provider-effect"},
            {"provider_effect_state": "reply_received"},
            {"artifacts": [*paused["tasks"][0]["artifacts"], {"kind": "file_transaction", "transaction_id": "unpublished"}]},
        ]
        for update in variations:
            with self.subTest(update=update):
                def change(document, _db):
                    document["tasks"] = copy.deepcopy(paused["tasks"])
                    document["tasks"][0].update(update)
                self.runtime.store._mutate(paused["goal_id"], change)
                with self.assertRaisesRegex(long_horizon.HarnessError, "pending provider/file effects"):
                    self.runtime.store.control(paused["goal_id"], "resume")

    def test_pruned_legacy_terminal_proof_never_becomes_settlement(self):
        paused = self.remove_applied_receipts(self.paused_after_published_blocker())
        def noise(document, db):
            self.runtime.store._event(db, document, "fixture_progress", payload={"observed": True})
        with mock.patch.object(long_horizon, "MAX_EVENTS", 1):
            self.runtime.store._mutate(paused["goal_id"], noise)
        with self.assertRaisesRegex(long_horizon.HarnessError, "pending provider/file effects"):
            self.runtime.store.control(paused["goal_id"], "resume")

    def test_receipt_cannot_settle_a_new_dispatch_or_hide_pending_effects(self):
        task = self.paused_after_published_blocker()["tasks"][0]
        self.assertFalse(long_horizon._task_has_unsettled_effect(task))
        for update in (
            {"provider_effect_id": "new-dispatch"}, {"provider_effect_state": "dispatched"},
            {"outcome_unknown": True}, {"reconciliation_required": True},
            {"pending_action": {"action": "complete"}},
            {"pending_transaction": {"state": "applied"}},
        ):
            with self.subTest(update=update):
                self.assertTrue(long_horizon._task_has_unsettled_effect({**task, **update}))


if __name__ == "__main__":
    unittest.main()
