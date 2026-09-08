from __future__ import annotations

import copy
import json
import unittest

from our_harness import goal_budget_policy as policy
from our_harness.models import HarnessError


class GoalBudgetPolicyTests(unittest.TestCase):
    def test_new_shared_goal_has_no_hidden_lifetime_call_ceiling(self):
        budget = policy.create_budget(shared=True)
        for counter, old_ceiling in policy.LEGACY_DEFAULTS.items():
            budget[counter] = old_ceiling + 20_000
            self.assertIsNone(policy.remaining(budget, counter))
            self.assertFalse(policy.exhausted(budget, counter))
            self.assertEqual(budget["call_limit_policy"]["sources"]["max_" + counter], "shared_default_unlimited")

    def test_adaptive_goals_keep_their_existing_default_allowance(self):
        budget = policy.create_budget(shared=False)
        for counter, default in policy.LEGACY_DEFAULTS.items():
            self.assertEqual(policy.remaining(budget, counter), default)
            budget[counter] = default
            self.assertTrue(policy.exhausted(budget, counter))

    def test_explicit_positive_limits_are_exact_even_above_old_clamps(self):
        budget = policy.create_budget({"max_provider_calls": 50_123, "max_context_tool_calls": 91_456}, shared=True)
        for counter, limit in (("provider_calls", 50_123), ("context_tool_calls", 91_456)):
            self.assertEqual(policy.remaining(budget, counter), limit)
            budget[counter] = limit - 1
            self.assertEqual(policy.remaining(budget, counter), 1)
            budget[counter] += 1
            self.assertTrue(policy.exhausted(budget, counter))
            budget[counter] += 1
            self.assertEqual(policy.remaining(budget, counter), 0)

    def test_explicit_zero_is_unlimited_in_either_goal_mode(self):
        for shared in (False, True):
            budget = policy.create_budget({"max_provider_calls": 0, "max_context_tool_calls": 0}, shared=shared)
            for counter in policy.LEGACY_DEFAULTS:
                self.assertIsNone(policy.remaining(budget, counter))
                self.assertEqual(budget["call_limit_policy"]["sources"]["max_" + counter], "explicit")

    def test_small_explicit_budget_cannot_be_replaced_by_shared_defaults(self):
        budget = policy.create_budget({"max_provider_calls": 2, "max_context_tool_calls": 1}, shared=True)
        budget.update(provider_calls=2, context_tool_calls=1)
        for counter in policy.LEGACY_DEFAULTS:
            self.assertTrue(policy.exhausted(budget, counter))

    def test_invalid_explicit_values_fail_instead_of_silent_coercion(self):
        for key in ("max_provider_calls", "max_context_tool_calls"):
            for value in (True, False, -1, 1.5, "30", None, [], {}):
                with self.subTest(key=key, value=value), self.assertRaises(HarnessError):
                    policy.create_budget({key: value}, shared=True)

    def test_json_restart_retains_configuration_provenance_and_consumed_calls(self):
        budget = policy.create_budget({"max_context_tool_calls": 10}, shared=True)
        budget.update(provider_calls=45_321, context_tool_calls=8)
        restored = json.loads(json.dumps(budget))
        before = copy.deepcopy(restored)
        self.assertIsNone(policy.remaining(restored, "provider_calls"))
        self.assertEqual(policy.remaining(restored, "context_tool_calls"), 2)
        self.assertEqual(restored, before)
        self.assertEqual(restored, budget)

    def test_legacy_finite_limits_and_counts_are_preserved_without_guessing_user_intent(self):
        for limits in ((1000, 500), (2, 3), (0, 0)):
            budget = {"provider_calls": 2, "context_tool_calls": 3,
                      "max_provider_calls": limits[0], "max_context_tool_calls": limits[1]}
            restored = json.loads(json.dumps(budget))
            self.assertEqual(policy.remaining(restored, "provider_calls"), max(0, limits[0] - 2))
            self.assertEqual(policy.remaining(restored, "context_tool_calls"), max(0, limits[1] - 3))
            self.assertEqual(restored, budget)
            self.assertNotIn("call_limit_policy", restored)

    def test_old_context_budget_default_is_retained_without_granting_provider_dispatch(self):
        self.assertEqual(policy.remaining({}, "context_tool_calls"), 500)
        self.assertTrue(policy.exhausted({}, "provider_calls"))

    def test_changed_contract_or_configuration_is_rejected_and_never_resets_counters(self):
        original = policy.create_budget({"max_provider_calls": 4}, shared=True)
        original["provider_calls"] = 3
        mutations = [
            lambda value: value.update(max_provider_calls=0),
            lambda value: value["call_limit_policy"].update(schema_version=99),
            lambda value: value["call_limit_policy"].update(contract_fingerprint_sha256="new-engine"),
            lambda value: value["call_limit_policy"].update(fingerprint_sha256="changed"),
            lambda value: value["call_limit_policy"]["sources"].update(max_provider_calls="shared_default_unlimited"),
            lambda value: value["call_limit_policy"]["limits"].pop("max_context_tool_calls"),
        ]
        for mutate in mutations:
            budget = copy.deepcopy(original)
            mutate(budget)
            before = copy.deepcopy(budget)
            with self.subTest(budget=budget), self.assertRaises(HarnessError):
                policy.remaining(budget, "provider_calls")
            self.assertEqual(budget, before)

    def test_invalid_saved_counter_cannot_turn_into_fresh_allowance(self):
        for value in (-1, True, "0", 0.5):
            budget = policy.create_budget(shared=True)
            budget["provider_calls"] = value
            with self.subTest(value=value), self.assertRaises(HarnessError):
                policy.remaining(budget, "provider_calls")


if __name__ == "__main__":
    unittest.main()
