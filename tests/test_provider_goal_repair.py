"""A working connection does not clear an authenticated goal-action failure."""

from __future__ import annotations

import copy
import json
import unittest

from our_harness import provider_repair


class ProviderGoalRepairTests(unittest.TestCase):
    def setUp(self):
        self.route = "portable-team-route"
        self.goal = {
            "goal_id": "goal-on-another-machine", "revision": 47, "status": "paused",
            "conversation_id": "chat-independent", "project": {"id": "project-portable"},
            "requested_agent_ids": ["writer", "reviewer"],
            "agents": [{"id": "writer", "who": self.route}, {"id": "reviewer", "who": "other-provider"}],
            "tasks": [{"id": "write-game", "assigned_agent_id": "writer", "state": "blocked",
                       "provider_effect_state": "known_reply_failed", "outcome_unknown": False,
                       "reconciliation_required": True, "pending_action": {}, "pending_transaction": {},
                       "last_error": "Delegated tasks are allowed only in a delegate action"},
                      {"id": "review-game", "assigned_agent_id": "reviewer", "state": "blocked",
                       "provider_effect_state": "acknowledged", "outcome_unknown": False,
                       "last_error": "Waiting for the other agent: Delegated tasks are allowed only in a delegate action"}],
        }
        self.verdict = {"schema_version": 1, "goal_id": self.goal["goal_id"], "goal_revision": 47,
                        "eligible": True, "task_ids": ["write-game"], "action": "resume",
                        "code": "legacy_semantic_rejection", "reason": "Authenticated delivered reply can be corrected."}
        self.plan = provider_repair._finish_plan({
            "route": self.route, "kind": "arbitrary-cli", "state": "isolated-ready",
            "authentication": "unknown", "installed": True, "can_login": False,
        }, {
            "state": "needs-verification", "tone": "attention", "title": "Protected mode is ready",
            "summary": "User configuration is bypassed by agent turns.", "steps": [],
            "actions": [provider_repair.LIVE_TEST, provider_repair.CHECK],
        }, {"schema_version": 1, "source": "non-billing-status", "category": "none", "summary": "Protected mode ready."})

    def attach(self, plan=None, goal=None, verdict=None, *, agent_id="writer"):
        return provider_repair.with_goal_failure(
            plan or self.plan, goal or self.goal, self.verdict if verdict is None else verdict,
            agent_id=agent_id,
        )

    def test_delivered_action_failure_is_separate_from_free_connection_status(self):
        original = copy.deepcopy(self.goal)
        found = self.attach()
        repair = found["repair"]
        self.assertEqual(found["state"], "isolated-ready")
        self.assertEqual(repair["state"], "goal-action-invalid")
        self.assertEqual(repair["diagnosis"]["source"], "authenticated-goal-task")
        self.assertEqual(repair["connection_repair"]["state"], "needs-verification")
        self.assertEqual([one["id"] for one in repair["actions"]], ["resume-goal", "open-goal", "live-test", "check"])
        self.assertEqual(next(one for one in repair["actions"] if one["id"] == "live-test")["label"], "Test connection only")
        for action in repair["actions"][:2]:
            self.assertEqual(action["goal_id"], self.goal["goal_id"])
            self.assertEqual(action["goal_revision"], self.goal["revision"])
            self.assertEqual(action["agent_id"], "writer")
            self.assertEqual(action["project_id"], "project-portable")
            self.assertEqual(action["participant_ids"], ["writer", "reviewer"])
            self.assertEqual(action["diagnosis_fingerprint"], repair["diagnosis_fingerprint"])
        self.assertEqual(self.goal, original)
        self.assertTrue(self.goal["tasks"][0]["reconciliation_required"])

    def test_no_recovery_authorization_is_inferred_from_error_words(self):
        found = self.attach(verdict={})["repair"]
        self.assertFalse(found["goal_issue"]["can_resume"])
        self.assertNotIn("resume-goal", [one["id"] for one in found["actions"]])
        self.assertIn("open-goal", [one["id"] for one in found["actions"]])

    def test_stale_mismatched_or_negative_engine_verdict_cannot_offer_resume(self):
        for update in ({"goal_revision": 46}, {"goal_id": "other-goal"}, {"task_ids": ["other-task"]},
                       {"eligible": False}, {"action": "retry"}, {"schema_version": 2}):
            with self.subTest(update=update):
                repair = self.attach(verdict={**self.verdict, **update})["repair"]
                self.assertFalse(repair["goal_issue"]["can_resume"])
                self.assertNotIn("resume-goal", [one["id"] for one in repair["actions"]])

    def test_pending_effects_never_gain_resume_from_an_inconsistent_verdict(self):
        for update in ({"pending_action": {"action": "work"}}, {"pending_transaction": {"state": "prepared"}}):
            with self.subTest(update=update):
                goal = copy.deepcopy(self.goal)
                goal["tasks"][0].update(update)
                self.assertFalse(self.attach(goal=goal)["repair"]["goal_issue"]["can_resume"])

    def test_typed_pending_and_exhausted_corrections_keep_their_actual_recovery_limit(self):
        for state in ("pending", "exhausted"):
            with self.subTest(state=state):
                goal = copy.deepcopy(self.goal)
                goal["tasks"][0].update({
                    "provider_effect_state": "protocol_rejected", "last_error": "",
                    "protocol_recovery": {"schema_version": 1, "state": state,
                                          "error_code": "tasks_require_delegate",
                                          "error": "Delegated tasks are allowed only in a delegate action"},
                })
                verdict = {**self.verdict, "eligible": state == "pending", "code": "pending_protocol_correction"}
                found = self.attach(goal=goal, verdict=verdict)["repair"]
                self.assertEqual(found["state"], "goal-action-invalid")
                self.assertEqual(found["goal_issue"]["can_resume"], state == "pending")
                self.assertEqual("resume-goal" in [one["id"] for one in found["actions"]], state == "pending")

    def test_arbitrary_runtime_error_is_not_misdiagnosed_as_action_protocol(self):
        goal = copy.deepcopy(self.goal)
        goal["tasks"][0]["last_error"] = "The project filesystem is no longer mounted"
        self.assertNotIn("goal_issue", self.attach(goal=goal)["repair"])

    def test_route_and_agent_identity_cannot_attach_another_agents_failure(self):
        for route, agent in (("other-provider", "reviewer"), ("new-route", "writer"), (self.route, "reviewer")):
            with self.subTest(route=route, agent=agent):
                found = self.attach(plan={**self.plan, "route": route}, agent_id=agent)
                self.assertNotIn("goal_issue", found["repair"])
        # A peer merely quoting the exact error is not a transport failure.
        self.assertEqual(self.attach()["repair"]["goal_issue"]["task_ids"], ["write-game"])

    def test_uncertain_delivery_and_completed_goals_are_not_correction_candidates(self):
        for update in ({"outcome_unknown": True}, {"provider_effect_state": "outcome_unknown"},
                       {"provider_effect_state": "dispatched"}, {"state": "complete"}):
            with self.subTest(update=update):
                goal = copy.deepcopy(self.goal)
                goal["tasks"][0].update(update)
                self.assertNotIn("goal_issue", self.attach(goal=goal)["repair"])
        for status in ("complete", "cancelled", "running", "waiting_for_user"):
            self.assertNotIn("goal_issue", self.attach(goal={**self.goal, "status": status})["repair"])

    def test_real_connection_problems_keep_their_own_repair_boundary(self):
        for state, action in (("needs-login", provider_repair.LOGIN), ("configuration-error", provider_repair.SETTINGS),
                              ("outcome-unknown", provider_repair.CHECK), ("service-unreachable", provider_repair.SETTINGS)):
            with self.subTest(state=state):
                plan = copy.deepcopy(self.plan)
                plan["repair"].update({"state": state, "actions": [action]})
                found = self.attach(plan=plan)["repair"]
                self.assertEqual(found["state"], state)
                self.assertEqual(found["actions"][0]["id"], action["id"])
                self.assertNotIn("resume-goal", [one["id"] for one in found["actions"]])

    def test_live_ready_verifies_connection_without_erasing_goal_failure(self):
        found = provider_repair.verified_plan(self.attach(), 250)["repair"]
        self.assertEqual(found["state"], "goal-action-invalid")
        self.assertEqual(found["connection_repair"]["state"], "verified")
        self.assertEqual(found["goal_issue"]["goal_id"], self.goal["goal_id"])
        self.assertIn("does not resume or repair", " ".join(found["connection_repair"]["steps"]))
        self.assertNotIn("login", [one["id"] for one in found["actions"]])

    def test_restart_roundtrip_changed_revision_and_finished_goal_refresh_the_offer(self):
        found = json.loads(json.dumps(self.attach()))
        repeated = self.attach(plan=found)
        self.assertEqual(found, repeated)
        advanced = self.attach(plan=found, goal={**self.goal, "revision": 48})
        self.assertFalse(advanced["repair"]["goal_issue"]["can_resume"])
        self.assertNotEqual(found["repair"]["diagnosis_fingerprint"], advanced["repair"]["diagnosis_fingerprint"])
        finished = self.attach(plan=found, goal={**self.goal, "status": "complete", "revision": 49})
        self.assertNotIn("goal_issue", finished["repair"])
        self.assertEqual(finished["repair"]["state"], "needs-verification")
        self.assertEqual([one["id"] for one in finished["repair"]["actions"]], ["live-test", "check"])


if __name__ == "__main__":
    unittest.main()
