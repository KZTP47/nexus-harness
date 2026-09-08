"""Repair buttons bind to authenticated task evidence, not a READY reply."""

from __future__ import annotations

import copy
from unittest import mock

from our_harness import long_horizon, provider_repair
from tests import test_provider_repair


class GoalRepairContextTests(test_provider_repair.RepairEndpointTests):
    def setUp(self) -> None:
        super().setUp()
        self.goal = {
            "goal_id": "portable-goal", "revision": 14, "status": "paused",
            "conversation_id": "conversation-alpha", "require_all_participants": True,
            "project": {"id": "project-alpha", "path": str(self.config.project_root)},
            "requested_agent_ids": ["writer", "reviewer"],
            "agents": [{"id": "writer", "who": "codex"}, {"id": "reviewer", "who": "other"}],
            "tasks": [{"id": "task-alpha", "assigned_agent_id": "writer", "state": "blocked",
                       "provider_effect_state": "known_reply_failed", "outcome_unknown": False,
                       "pending_action": {}, "pending_transaction": {},
                       "last_error": "Delegated tasks are allowed only in a delegate action"}],
        }
        self.verdict = {"schema_version": 1, "eligible": True, "action": "resume",
                        "goal_id": self.goal["goal_id"], "goal_revision": 14,
                        "task_ids": ["task-alpha"], "code": "legacy_protocol_rejection",
                        "reason": "Authenticated rejected reply had no applied effects."}
        self.board = {"agents": copy.deepcopy(self.goal["agents"]), "projects": [self.goal["project"]]}
        self.store = mock.Mock()
        self.store.get.side_effect = lambda _goal: copy.deepcopy(self.goal)
        self.store.active_authority_goals.side_effect = lambda: [copy.deepcopy(self.goal)]
        self.store.protocol_recovery_status.side_effect = lambda _goal: copy.deepcopy(self.verdict)
        for patcher in [
            mock.patch.object(self.server, "swarm_standing", side_effect=lambda: {"board": self.board}),
            mock.patch.object(long_horizon, "GoalStore", return_value=self.store),
            mock.patch.object(provider_repair, "repair_plan", side_effect=lambda *_a, **_k: self.allowed_plan()),
        ]:
            patcher.start()
            self.addCleanup(patcher.stop)

    def diagnosed(self) -> dict:
        status, result = self.call("/api/team/repair-plan", {
            "route": "codex", "agent_id": "writer", "goal_id": self.goal["goal_id"],
        })
        self.assertEqual(status, 200, result)
        return result

    def resume_payload(self) -> dict:
        plan = self.diagnosed()
        offered = next(one for one in plan["repair"]["actions"] if one["id"] == "resume-goal")
        return {"goal_id": self.goal["goal_id"], "action": "resume", "payload": {
            "chat_id": self.goal["conversation_id"], "project_id": self.goal["project"]["id"],
            "participant_ids": self.goal["requested_agent_ids"],
            "repair_context": {"route": "codex", "agent_id": "writer", "goal_id": self.goal["goal_id"],
                               "diagnosis_fingerprint": offered["diagnosis_fingerprint"]},
        }}

    def runtime(self):
        runtime = mock.Mock(store=self.store)
        runtime.resume.return_value = {**self.goal, "status": "running"}
        self.server._long_horizon = runtime
        self.addCleanup(setattr, self.server, "_long_horizon", None)
        return runtime

    def test_diagnosis_reads_saved_failure_without_starting_runtime_or_model(self) -> None:
        with mock.patch("our_harness.chat.ask_once") as asked:
            plan = self.diagnosed()
        self.assertIsNone(self.server._long_horizon)
        asked.assert_not_called()
        self.assertEqual(plan["repair"]["state"], "goal-action-invalid")
        self.assertEqual(plan["repair"]["goal_issue"]["agent_id"], "writer")
        self.store.protocol_recovery_status.assert_called_once()

    def test_agent_only_diagnosis_selects_its_active_goal(self) -> None:
        status, result = self.call("/api/team/repair-plan", {"route": "codex", "agent_id": "writer"})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["repair"]["goal_issue"]["goal_id"], self.goal["goal_id"])

    def test_stale_selected_route_and_foreign_goal_are_rejected(self) -> None:
        self.board["agents"][0]["who"] = "changed-route"
        status, _ = self.call("/api/team/repair-plan", {"route": "codex", "agent_id": "writer"})
        self.assertEqual(status, 400)
        self.store.get.assert_not_called()
        self.board["agents"][0]["who"] = "codex"
        self.goal["agents"][0]["who"] = "another-route"
        status, _ = self.call("/api/team/repair-plan", {
            "route": "codex", "agent_id": "writer", "goal_id": self.goal["goal_id"],
        })
        self.assertEqual(status, 400)

    def test_same_route_other_agent_does_not_inherit_the_failure(self) -> None:
        self.goal["agents"][1]["who"] = "codex"
        self.board["agents"][1]["who"] = "codex"
        status, result = self.call("/api/team/repair-plan", {
            "route": "codex", "agent_id": "reviewer", "goal_id": self.goal["goal_id"],
        })
        self.assertEqual(status, 200, result)
        self.assertNotIn("goal_issue", result["repair"])

    def test_live_ready_cannot_claim_saved_goal_repaired(self) -> None:
        with mock.patch("our_harness.chat.ask_once", return_value={"text": "READY", "milliseconds": 1}):
            status, result = self.call("/api/team/test-route", {
                "route": "codex", "agent_id": "writer", "goal_id": self.goal["goal_id"],
            })
        self.assertEqual(status, 200, result)
        self.assertTrue(result["answered"])
        self.assertEqual(result["plan"]["repair"]["state"], "goal-action-invalid")
        self.assertIsNone(self.server._long_horizon)

    def test_live_ready_refreshes_goal_if_it_completed_during_connection_test(self) -> None:
        def answer(*_args, **_kwargs):
            self.goal.update(status="complete", revision=15)
            return {"text": "READY", "milliseconds": 1}
        with mock.patch("our_harness.chat.ask_once", side_effect=answer):
            status, result = self.call("/api/team/test-route", {
                "route": "codex", "agent_id": "writer", "goal_id": self.goal["goal_id"],
            })
        self.assertEqual(status, 200, result)
        self.assertEqual(result["plan"]["repair"]["state"], "verified")
        self.assertNotIn("goal_issue", result["plan"]["repair"])

    def test_connection_proof_cannot_verify_a_replacement_route(self) -> None:
        seen = []
        def answer(config, *_args, **_kwargs):
            seen.append(config.get("providers.codex.model"))
            self.config.data["providers"]["codex"]["model"] = "replacement-model"
            self.assertEqual(config.get("providers.codex.model"), "gpt", "The admitted test config was mutable")
            return {"text": "READY", "milliseconds": 1}
        with mock.patch("our_harness.chat.ask_once", side_effect=answer):
            status, result = self.call("/api/team/test-route", {"route": "codex"})
        self.assertEqual(status, 200, result)
        self.assertEqual(seen, ["gpt"])
        self.assertTrue(result["test_superseded"])
        self.assertNotEqual(result["plan"]["repair"]["state"], "verified")
        self.assertIn("does not verify the current route", result["note"])

    def test_route_test_identity_changes_with_project_and_command(self) -> None:
        from our_harness.config import LoadedConfig
        first = self.server.route_test_identity(self.config, "codex")
        data = copy.deepcopy(self.config.data)
        data["providers"]["codex"]["command"] = ["another-command"]
        changed = LoadedConfig(data, self.config.project_root, [], {})
        self.assertNotEqual(first, self.server.route_test_identity(changed, "codex"))
        moved = LoadedConfig(copy.deepcopy(self.config.data), self.config.project_root / "another project", [], {})
        self.assertNotEqual(first, self.server.route_test_identity(moved, "codex"))

    def test_matching_repair_resumes_exact_goal_through_normal_gateway(self) -> None:
        payload = self.resume_payload()
        runtime = self.runtime()
        with mock.patch.object(self.server, "require_project_execution_authority"):
            status, result = self.call("/api/long-horizon/control", payload)
        self.assertEqual(status, 200, result)
        runtime.resume.assert_called_once_with(self.goal["goal_id"], project_verification_settings=self.goal["project"], expected_revision=14)

    def test_changed_goal_revision_or_recovery_verdict_never_resumes(self) -> None:
        payload = self.resume_payload()
        runtime = self.runtime()
        self.goal["revision"] += 1
        self.verdict["goal_revision"] += 1
        with mock.patch.object(self.server, "require_project_execution_authority"):
            status, _ = self.call("/api/long-horizon/control", payload)
        self.assertEqual(status, 400)
        runtime.resume.assert_not_called()
        self.verdict["eligible"] = False
        with mock.patch.object(self.server, "require_project_execution_authority"):
            status, _ = self.call("/api/long-horizon/control", payload)
        self.assertEqual(status, 400)
        runtime.resume.assert_not_called()

    def test_repair_context_cannot_redirect_a_valid_button(self) -> None:
        payload = self.resume_payload()
        runtime = self.runtime()
        payload["payload"]["repair_context"]["goal_id"] = "other-goal"
        with mock.patch.object(self.server, "require_project_execution_authority"):
            status, _ = self.call("/api/long-horizon/control", payload)
        self.assertEqual(status, 400)
        runtime.resume.assert_not_called()
