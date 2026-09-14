"""Independent HTTP contract checks for team follow-up file admission."""
from __future__ import annotations

import base64
import copy
import json
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from our_harness import long_horizon, server
import test_long_horizon_dialogue as fixtures
import test_goal_decisions as decisions


class TeamFollowupAPITests(unittest.TestCase):
    setUp = fixtures.LongHorizonDialogueTests.setUp
    create = fixtures.LongHorizonDialogueTests.create

    def test_status_question_is_read_only_and_keeps_chat_binding(self):
        goal = self.create("status-http")
        panel = self.panel()
        body = {"goal_id": goal["goal_id"], "action": "steer", "payload": {
            "chat_id": goal["conversation_id"], "project_id": goal["project"]["id"],
            "participant_ids": ["builder", "peer"], "text": "what the hell is taking so long?"}}
        before = self.runtime.store.get(goal["goal_id"])
        with mock.patch.object(self.runtime, "start_background") as start:
            status, result = self.post(panel, body)
        self.assertEqual(status, 200, result)
        self.assertIn("Checking status does not restart", result["goal"]["status_response"])
        self.assertEqual(before, self.runtime.store.get(goal["goal_id"]))
        start.assert_not_called()
        body["action"] = "status"
        body["payload"]["chat_id"] = "another-chat"
        self.assertNotEqual(self.post(panel, body)[0], 200)

    def panel(self):
        panel = server.HarnessHTTPServer(("127.0.0.1", 0), self.config)
        panel._long_horizon = self.runtime
        self.addCleanup(panel.server_close)
        thread = threading.Thread(target=panel.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(panel.shutdown)
        return panel

    def post(self, panel, body, *, endpoint="control", token=True):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Harness-Token"] = panel.token
        request = urllib.request.Request(
            f"http://127.0.0.1:{panel.server_address[1]}/api/long-horizon/{endpoint}",
            data=json.dumps(body).encode(), headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def payload(self, goal):
        return {"goal_id": goal["goal_id"], "action": "steer", "payload": {
            "chat_id": goal["conversation_id"], "project_id": "game",
            "participant_ids": ["builder", "peer"], "request_id": "file-http-once",
            "text": "Use the attached reproduction.", "attachments": [{
                "name": "reproduction.txt", "type": "text/plain",
                "data": base64.b64encode(b"Exact reproduction: open /arbitrary/route").decode(),
            }],
        }}

    def test_http_receipt_retry_and_chat_boundaries(self):
        goal = self.create("files-api")
        panel, body = self.panel(), self.payload(goal)
        before = self.runtime.store.get(goal["goal_id"])
        with mock.patch.object(self.runtime, "start_background") as schedule:
            for change in ({"chat_id": "other-chat"}, {"project_id": "other-project"},
                           {"participant_ids": ["builder", "stranger"]}):
                wrong = copy.deepcopy(body)
                wrong["payload"].update(change)
                self.assertNotEqual(self.post(panel, wrong)[0], 200)
                self.assertEqual(self.runtime.store.get(goal["goal_id"])["revision"], before["revision"])
            self.assertNotEqual(self.post(panel, body, token=False)[0], 200)
            status, accepted = self.post(panel, body)
            self.assertEqual(status, 200, accepted)
            receipt = accepted["followup_receipt"]
            self.assertTrue(receipt["accepted"])
            self.assertEqual(receipt["goal_id"], goal["goal_id"])
            self.assertEqual(receipt["chat_id"], goal["conversation_id"])
            self.assertEqual(receipt["attachment_count"], 1)
            after = self.runtime.store.get(goal["goal_id"])
            calls = schedule.call_count
            status, replay = self.post(panel, body)
            self.assertEqual(status, 200, replay)
            self.assertEqual(replay["followup_receipt"], receipt)
            self.assertEqual(self.runtime.store.get(goal["goal_id"])["revision"], after["revision"])
            self.assertEqual(schedule.call_count, calls)
            changed = copy.deepcopy(body)
            changed["payload"]["attachments"][0]["data"] = base64.b64encode(b"Different input").decode()
            self.assertNotEqual(self.post(panel, changed)[0], 200)
        public = json.dumps(accepted)
        self.assertNotIn(body["payload"]["attachments"][0]["data"], public)
        self.assertNotIn("long-horizon-inputs", public)

    def test_http_rejects_unsupported_action_without_mutation(self):
        goal = self.create("files-api-unsupported")
        panel, body = self.panel(), self.payload(goal)
        before = self.runtime.store.get(goal["goal_id"])
        with mock.patch.object(self.runtime, "start_background") as schedule:
            for action in ("message", "pause", "cancel", "resume", "fork"):
                body["action"] = action
                status, result = self.post(panel, body)
                self.assertNotEqual(status, 200, (action, result))
                self.assertEqual(self.runtime.store.get(goal["goal_id"])["revision"], before["revision"])
            schedule.assert_not_called()

    def test_answer_files_are_atomic_and_replay_after_scheduling_failure(self):
        goal = self.create("files-answer-api")
        _task, ids, held = decisions.GoalDecisionTests.ask(self, goal)
        panel = self.panel()
        control = self.payload(goal)["payload"]
        body = {key: control[key] for key in ("chat_id", "project_id", "participant_ids", "attachments")}
        body.update(goal_id=goal["goal_id"], request_id="answer-files-once",
            expected_revision=held["revision"], pending_ids=ids,
            decision_snapshot=self.runtime.store.public(held)["decision_snapshot"],
            answers={ids[0]: decisions.answer(decisions.question(), "Use the attached reproduction.", "team")})
        with mock.patch.object(self.runtime, "start_background", side_effect=RuntimeError("fixture scheduler unavailable")):
            status, accepted = self.post(panel, body, endpoint="answer")
        self.assertEqual(status, 200, accepted)
        self.assertTrue(accepted["followup_receipt"]["accepted"])
        self.assertIn("scheduler", accepted["scheduling_error"])
        after = self.runtime.store.get(goal["goal_id"])
        self.assertFalse(any(one["state"] == "pending" for one in after["interrupts"]))
        self.runtime.close()
        self.runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(self.runtime.close)
        panel._long_horizon = self.runtime
        with mock.patch.object(self.runtime, "start_background") as schedule:
            status, replay = self.post(panel, body, endpoint="answer")
            self.assertEqual(status, 200, replay)
            self.assertEqual(replay["followup_receipt"], accepted["followup_receipt"])
            self.assertEqual(self.runtime.store.get(goal["goal_id"])["revision"], after["revision"])
            schedule.assert_not_called()
            changed = copy.deepcopy(body)
            changed["attachments"][0]["data"] = base64.b64encode(b"Altered after acceptance").decode()
            self.assertNotEqual(self.post(panel, changed, endpoint="answer")[0], 200)


if __name__ == "__main__":
    unittest.main()
