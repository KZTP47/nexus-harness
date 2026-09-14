"""Independent HTTP admission/replay checks for recipient-bound continuations."""
from __future__ import annotations

import copy
import hashlib
import json
import unittest
from unittest import mock

from our_harness import long_horizon
import test_team_followup_api as http_fixtures


class WorkflowLimitationsAPITests(unittest.TestCase):
    setUp = http_fixtures.TeamFollowupAPITests.setUp
    create = http_fixtures.TeamFollowupAPITests.create
    panel = http_fixtures.TeamFollowupAPITests.panel
    post = http_fixtures.TeamFollowupAPITests.post

    def message(self, goal):
        task = next(one for one in goal["tasks"] if one["assigned_agent_id"] == "builder")
        return {"goal_id": goal["goal_id"], "action": "message", "payload": {
            "task_id": task["id"], "agent_id": "builder",
            "request_id": "directed-http-stable-request",
            "text": "Inspect /unrelated/route with the exact recipient.",
        }}

    def test_rejected_target_auth_and_conflicting_retry_do_not_mutate(self):
        goal = self.create("directed-http")
        panel, body = self.panel(), self.message(goal)
        initial = self.runtime.store.get(goal["goal_id"])
        with mock.patch.object(self.runtime, "start_background") as schedule:
            for changes in ({"task_id": "absent-task"}, {"agent_id": "peer"},
                            {"text": ""}, {"text": "x" * 20_001}):
                wrong = copy.deepcopy(body)
                wrong["payload"].update(changes)
                status, result = self.post(panel, wrong)
                self.assertNotEqual(status, 200, result)
                self.assertEqual(self.runtime.store.get(goal["goal_id"]), initial)
                if changes == {"agent_id": "peer"}:
                    proof = result["directed_message_rejection"]
                    self.assertIs(proof["accepted"], False)
                    self.assertEqual(proof["schema_version"], 1)
                    for key in ("request_id", "task_id", "agent_id"):
                        self.assertEqual(proof[key], wrong["payload"][key])
                    envelope = {"goal_id": goal["goal_id"], **{
                        key: wrong["payload"][key] for key in ("task_id", "agent_id", "text")}}
                    self.assertEqual(proof["submission_sha256"], hashlib.sha256(json.dumps(
                        envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                    ).encode("utf-8")).hexdigest())
            self.assertNotEqual(self.post(panel, body, token=False)[0], 200)
            self.assertEqual(self.runtime.store.get(goal["goal_id"]), initial)
            status, accepted = self.post(panel, body)
            self.assertEqual(status, 200, accepted)
            receipt = accepted["directed_message_receipt"]
            self.assertEqual(receipt["schema_version"], 1)
            self.assertIs(receipt["accepted"], True)
            for key in ("request_id", "task_id", "agent_id"):
                self.assertEqual(receipt[key], body["payload"][key])
            self.assertEqual(receipt["goal_id"], goal["goal_id"])
            self.assertRegex(receipt["submission_sha256"], r"^[0-9a-f]{64}$")
            expected = {"goal_id": goal["goal_id"], **{
                key: body["payload"][key] for key in ("task_id", "agent_id", "text")}}
            self.assertEqual(receipt["submission_sha256"], hashlib.sha256(json.dumps(
                expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ).encode("utf-8")).hexdigest())
            after = self.runtime.store.get(goal["goal_id"])
            calls = schedule.call_count
            status, replay = self.post(panel, body)
            self.assertEqual(status, 200, replay)
            self.assertEqual(replay["directed_message_receipt"], receipt)
            self.assertEqual(self.runtime.store.get(goal["goal_id"]), after)
            self.assertEqual(schedule.call_count, calls)
            changed = copy.deepcopy(body)
            changed["payload"]["text"] = "Different request under the same ID"
            status, rejected = self.post(panel, changed)
            self.assertNotEqual(status, 200)
            self.assertNotIn("directed_message_rejection", rejected)
            self.assertEqual(self.runtime.store.get(goal["goal_id"]), after)

    def test_accepted_scheduling_failure_remains_replayable_after_restart(self):
        goal = self.create("directed-http-restart")
        panel, body = self.panel(), self.message(goal)
        with mock.patch.object(self.runtime, "start_background", side_effect=RuntimeError("fixture scheduler unavailable")):
            status, accepted = self.post(panel, body)
        self.assertEqual(status, 200, accepted)
        self.assertTrue(accepted["directed_message_receipt"]["accepted"])
        self.assertIn("scheduler", accepted["scheduling_error"])
        before = self.runtime.store.get(goal["goal_id"])
        self.runtime.close()
        self.runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(self.runtime.close)
        panel._long_horizon = self.runtime
        with mock.patch.object(self.runtime, "start_background") as schedule:
            status, replay = self.post(panel, body)
            self.assertEqual(status, 200, replay)
            self.assertEqual(replay["directed_message_receipt"], accepted["directed_message_receipt"])
            self.assertEqual(self.runtime.store.get(goal["goal_id"]), before)
            schedule.assert_not_called()


if __name__ == "__main__":
    unittest.main()
