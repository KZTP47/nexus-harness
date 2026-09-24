from __future__ import annotations

import unittest
from unittest import mock

from our_harness import goal_messages
from our_harness.models import HarnessError


class GoalMessagesTests(unittest.TestCase):
    def goal(self):
        return {
            "goal_id": "portable-goal", "project_authority_id": "authority", "conversation_id": "chat",
            "agents": [{"id": "builder"}, {"id": "peer"}],
            "tasks": [{"id": "build", "assigned_agent_id": "builder", "state": "running"}],
        }

    def send(self, goal, number, text=None):
        payload = {"task_id": "build", "agent_id": "builder", "text": text or f"Direction {number}",
                   "request_id": f"request-{number}"}
        return goal_messages.accept(goal, payload, safe_text=payload["text"])

    def deliver(self, goal):
        task = goal["tasks"][0]
        task["directed_messages_delivered"] = goal_messages.highwater(goal, task) or task.get("directed_messages_delivered", 0)

    def test_long_goals_accept_far_more_than_the_old_lifetime_message_cap(self):
        goal = self.goal()
        for number in range(400):
            self.assertTrue(self.send(goal, number)["accepted"])
            self.deliver(goal)
        self.assertEqual(len(goal["tasks"][0]["directed_messages"]), 400)
        self.assertEqual(goal_messages.pending(goal, goal["tasks"][0]), [])

    def test_lifetime_characters_beyond_old_cap_are_accepted_once_delivered(self):
        goal = self.goal()
        text = "x" * goal_messages.MAX_MESSAGE_CHARACTERS
        for number in range(20):  # 400,000 characters, above the old 240,000 lifetime cap
            self.send(goal, number, text)
            self.deliver(goal)
        self.assertEqual(sum(len(one["text"]) for one in goal["tasks"][0]["directed_messages"]), 400_000)

    def test_per_message_and_pending_delivery_bounds_still_protect_the_machine(self):
        goal = self.goal()
        with self.assertRaisesRegex(HarnessError, "20,000"):
            self.send(goal, 1, "x" * (goal_messages.MAX_MESSAGE_CHARACTERS + 1))
        text = "y" * goal_messages.MAX_MESSAGE_CHARACTERS
        count = goal_messages.MAX_PENDING_CHARACTERS // goal_messages.MAX_MESSAGE_CHARACTERS
        for number in range(count):
            self.send(goal, number, text)
        with self.assertRaisesRegex(HarnessError, "Let the recipients read them"):
            self.send(goal, count, text)
        self.deliver(goal)
        self.assertTrue(self.send(goal, count, text)["accepted"])

    def test_runaway_history_guard_reports_without_discarding(self):
        goal = self.goal()
        with mock.patch.object(goal_messages, "MAX_MESSAGES", 2):
            self.send(goal, 1)
            self.send(goal, 2)
            with self.assertRaisesRegex(HarnessError, "Nothing was discarded"):
                self.send(goal, 3)
        self.assertEqual(len(goal["tasks"][0]["directed_messages"]), 2)

    def test_retry_of_same_request_returns_the_original_receipt(self):
        goal = self.goal()
        first = self.send(goal, 7)
        self.assertEqual(self.send(goal, 7), first)
        self.assertEqual(len(goal["tasks"][0]["directed_messages"]), 1)


if __name__ == "__main__":
    unittest.main()
