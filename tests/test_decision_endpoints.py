"""Decision HTTP controls retain exact input and authenticated chat ownership."""
from __future__ import annotations

import unittest
from unittest import mock

from our_harness.models import HarnessError
from tests.test_provider_repair import RepairEndpointTests


class DecisionEndpointTests(unittest.TestCase):
    setUp = RepairEndpointTests.setUp
    call = RepairEndpointTests.call

    def runtime(self):
        goal = {"goal_id": "goal-alpha", "revision": 7,
                "conversation_id": "chat-alpha", "requested_agent_ids": ["a", "b"],
                "project": {"id": "project-alpha", "path": str(self.config.project_root)}}
        runtime = mock.Mock()
        runtime.store.get.return_value = goal
        runtime.resume.return_value = goal
        runtime.reconsider.return_value = goal
        self.server._long_horizon = runtime
        self.addCleanup(setattr, self.server, "_long_horizon", None)
        return runtime

    def payload(self):
        return {"goal_id": "goal-alpha", "expected_revision": 7, "pending_ids": ["q1"],
                "chat_id": "chat-alpha", "project_id": "project-alpha", "participant_ids": ["a", "b"]}

    def test_reconsider_dispatches_exact_card_without_a_synthetic_answer(self):
        runtime = self.runtime()
        with mock.patch.object(self.server, "require_project_execution_authority") as authority:
            status, result = self.call("/api/long-horizon/reconsider", self.payload())
        self.assertEqual(status, 200, result)
        authority.assert_called_once_with(self.config.project_root)
        runtime.reconsider.assert_called_once_with("goal-alpha", expected_revision=7, pending_ids=["q1"])
        runtime.resume.assert_not_called()

    def test_both_decision_endpoints_reject_foreign_chat_before_dispatch(self):
        runtime = self.runtime()
        for endpoint in ("answer", "reconsider"):
            with self.subTest(endpoint=endpoint):
                status, _ = self.call("/api/long-horizon/" + endpoint,
                                      {**self.payload(), "chat_id": "foreign-chat"})
                self.assertEqual(status, 400)
        runtime.resume.assert_not_called()
        runtime.reconsider.assert_not_called()

    def test_raw_answer_request_and_invalid_revision_are_not_coerced(self):
        runtime = self.runtime()
        answers = {"q1": {"schema_version": 1, "audience": "team", "questions": [
            {"question_id": "path", "selected_options": [], "text": "https://example.test/A%2Fb?x=One#Keep"}]}}
        payload = {**self.payload(), "request_id": "exact-retry-1", "answers": answers}
        with mock.patch.object(self.server, "require_project_execution_authority"):
            status, result = self.call("/api/long-horizon/answer", payload)
        self.assertEqual(status, 200, result)
        runtime.resume.assert_called_once_with("goal-alpha", {
            "answers": answers, "request_id": "exact-retry-1", "expected_revision": 7, "pending_ids": ["q1"]})
        # The store validates types; the HTTP layer must not turn booleans,
        # strings or fractional revisions into an apparently valid integer.
        runtime.resume.side_effect = HarnessError("The exact decision submission is malformed")
        with mock.patch.object(self.server, "require_project_execution_authority"):
            status, _ = self.call("/api/long-horizon/answer", {**payload, "expected_revision": True})
        self.assertEqual(status, 400)
        self.assertIs(runtime.resume.call_args.args[1]["expected_revision"], True)


if __name__ == "__main__":
    unittest.main()
