from __future__ import annotations

import copy
import json
import unittest

from our_harness import swarm_work
from our_harness.collaboration_ledger import CollaborationLedger
from our_harness.models import HarnessError
from tests import test_swarm_work as fixtures


class SharedContextResponseBudgetTests(unittest.TestCase):
    setUp = fixtures.SwarmWorkTests.setUp

    def tools(self, config=None, profile="shared_goal_v1"):
        return swarm_work._ProjectContextTools(
            config or self.config, self.project, self.ledger,
            self.board["projects"][0], "Inspect the existing source without changing files", [], None,
            verification_profile=profile,
        )

    def prepare(self):
        self.config.data["workflow"]["max_tool_calls"] = 2
        (self.project / "source.txt").write_bytes(b"first\nsecond\nthird\n")
        self.ledger = CollaborationLedger(self.config, "claude", "response-exploration").begin(
            "Inspect existing source", self.board["agents"], mode="project_work",
        )

    def read(self, tools, scope, call_id="read-source", line=1):
        return tools.execute("agent-1", {
            "call_id": call_id, "name": "read_file", "arguments": {
                "path": "source.txt", "start_line": line, "end_line": line, "max_bytes": 100,
            },
        }, execution_scope=scope)

    def test_read_only_exploration_spans_responses_and_restart_preserves_each_allowance(self):
        self.prepare()
        before, _ = swarm_work._project_tree_merkle(self.project)
        tools = self.tools()
        try:
            self.read(tools, "persisted-response-a")
            self.read(tools, "persisted-response-a", "second-read", line=2)
            with self.assertRaisesRegex(HarnessError, "call limit"):
                self.read(tools, "persisted-response-a", "over-limit")
            self.assertEqual("ok", self.read(tools, "persisted-response-b")["status"])
            self.assertEqual(3, tools.disclosure()["lifetime_calls_used"])
        finally:
            tools.close()
        reopened = self.tools()
        try:
            self.assertEqual(3, reopened.disclosure()["lifetime_calls_used"])
            self.read(reopened, "persisted-response-b", "second-read", line=2)
            with self.assertRaisesRegex(HarnessError, "call limit"):
                self.read(reopened, "persisted-response-a", "cannot-reset-old")
            with self.assertRaisesRegex(HarnessError, "call limit"):
                self.read(reopened, "persisted-response-b", "cannot-reset-current")
            self.assertEqual("ok", self.read(reopened, "persisted-response-c")["status"])
            self.assertEqual(5, reopened.disclosure()["lifetime_calls_used"])
            self.assertEqual(before, swarm_work._project_tree_merkle(self.project)[0])
        finally:
            reopened.close()

    def test_scope_does_not_reset_identity_receipts_or_explicit_lifetime_limit(self):
        self.prepare()
        tools = self.tools()
        try:
            first = self.read(tools, "original")
            self.assertEqual("ok", self.read(tools, "new-response", line=2)["status"])
            replay = self.read(tools, "original")
            self.assertTrue(replay["replayed"])
            self.assertEqual(first["content"], replay["content"])
            tools.absolute_call_limit = 3
            with self.assertRaisesRegex(HarnessError, "Absolute long-horizon"):
                self.read(tools, "third-response")
            self.assertEqual(3, tools.disclosure()["lifetime_calls_used"])
        finally:
            tools.close()

    def test_legacy_scope_and_lowered_configuration_do_not_reset_consumption(self):
        self.prepare()
        legacy = self.tools(profile="legacy")
        try:
            self.read(legacy, "")
            self.read(legacy, "", "second-read", line=2)
        finally:
            legacy.close()
        changed = copy.deepcopy(self.config)
        changed.data["workflow"]["max_tool_calls"] = 1
        shared = self.tools(changed)
        try:
            with self.assertRaisesRegex(HarnessError, "call limit"):
                self.read(shared, "inherited-response")
            self.assertEqual("ok", self.read(shared, "genuinely-new-response")["status"])
            self.assertEqual(3, shared.disclosure()["lifetime_calls_used"])
            self.assertIn("durable agent response", shared.disclosure()["renewal_policy"])
        finally:
            shared.close()

    def test_output_allowance_renews_only_for_a_new_response(self):
        self.prepare()
        tools = self.tools()
        try:
            self.read(tools, "first")
            # Exhaust a legitimate response's output allowance and checkpoint
            # that boundary without manufacturing a new response identity.
            tools.session.total_bytes = tools.epoch_byte_limit
            tools._record_budget()
        finally:
            tools.close()
        reopened = self.tools()
        try:
            exhausted = self.read(reopened, "first", "continuation")
            self.assertTrue(exhausted["truncated"])
            self.assertEqual("", exhausted["content"])
            available = self.read(reopened, "second")
            self.assertEqual("first\n", json.loads(available["content"])["content"])
        finally:
            reopened.close()

    def test_explicit_limits_matching_old_defaults_are_preserved(self):
        self.prepare()
        self.config.data["workflow"]["max_tool_calls"] = 12
        self.config.data["workflow"]["max_tool_total_bytes"] = 128_000
        tools = self.tools()
        try:
            self.assertEqual(12, tools.session.max_calls)
            self.assertEqual(128_000, tools.session.total_bytes_limit)
        finally:
            tools.close()


if __name__ == "__main__":
    unittest.main()
