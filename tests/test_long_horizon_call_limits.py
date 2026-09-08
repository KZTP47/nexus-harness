from __future__ import annotations

import copy
import unittest

from our_harness import goal_budget_policy, long_horizon
from our_harness.models import HarnessError
from tests import test_long_horizon_dialogue as fixtures


class LongHorizonCallLimitTests(unittest.TestCase):
    setUp = fixtures.LongHorizonDialogueTests.setUp
    create = fixtures.LongHorizonDialogueTests.create
    provider = fixtures.LongHorizonDialogueTests.provider
    run_replies = fixtures.LongHorizonDialogueTests.run_replies

    def test_shared_goal_continues_beyond_old_lifetime_caps_after_restart(self):
        (self.project / "source.js").write_text("export const ready = true;\n")
        goal = self.create("past-old-limits")
        self.runtime.store._mutate(goal["goal_id"], lambda current, db: current["budget"].update(
            provider_calls=1_000, context_tool_calls=500,
        ))
        self.runtime.close()
        self.runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(self.runtime.close)
        result, seen = self.run_replies(goal, [
            fixtures.reply("work", "I will inspect the current source.", tool_calls=[{
                "call_id": "inspect", "name": "read_file", "arguments": {
                    "path": "source.js", "start_line": 1, "end_line": 2, "max_bytes": 100,
                },
            }]), fixtures.reply(), fixtures.reply(),
        ])
        self.assertEqual("complete", result["status"], result["note"])
        self.assertEqual(1003, result["budget"]["provider_calls"])
        self.assertEqual(501, result["budget"]["context_tool_calls"])
        self.assertEqual(["builder-route", "builder-route", "peer-route"], [route for route, _ in seen])
        self.assertIsNone(goal_budget_policy.remaining(result["budget"], "provider_calls"))

    def test_explicit_provider_budget_stops_at_exact_limit(self):
        goal = self.create("finite-calls", policy={"max_provider_calls": 2})
        result, seen = self.run_replies(goal, [
            fixtures.reply("work", "I investigated the input."),
            fixtures.reply("work", "I investigated the output."),
        ])
        self.assertEqual("paused", result["status"])
        self.assertEqual(2, len(seen))
        self.assertEqual(2, result["budget"]["provider_calls"])
        self.assertIn("provider-call budget", result["note"])

    def test_legacy_budget_remains_finite_and_explicit_large_limits_are_not_clamped(self):
        goal = self.create("legacy-calls", policy={"max_provider_calls": 5_000, "max_context_tool_calls": 9_000})
        self.assertEqual(5000, goal["budget"]["max_provider_calls"])
        self.assertEqual(9000, goal["budget"]["max_context_tool_calls"])
        def legacy(current, db):
            current["budget"].pop("call_limit_policy")
            current["budget"].update(provider_calls=5000)
        self.runtime.store._mutate(goal["goal_id"], legacy)
        self.assertEqual([], self.runtime.store.claim_ready(goal["goal_id"], "legacy-worker"))
        saved = self.runtime.store.get(goal["goal_id"])
        self.assertEqual("paused", saved["status"])
        self.assertEqual(5000, saved["budget"]["provider_calls"])

    def test_invalid_call_limits_fail_before_goal_creation(self):
        for field in ("max_provider_calls", "max_context_tool_calls"):
            for value in (True, -1, "100", 1.5, None):
                with self.subTest(field=field, value=value), self.assertRaisesRegex(HarnessError, "nonnegative integer"):
                    self.create("invalid-" + field + str(value), policy={field: value})

    def test_unlimited_limits_do_not_override_user_pause(self):
        goal = self.create("paused-unlimited")
        self.runtime.store.control(goal["goal_id"], "pause")
        self.assertEqual([], self.runtime.store.claim_ready(goal["goal_id"], "paused-worker"))
        self.assertEqual(0, self.runtime.store.get(goal["goal_id"])["budget"]["provider_calls"])


if __name__ == "__main__":
    unittest.main()
