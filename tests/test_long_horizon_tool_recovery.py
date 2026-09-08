from __future__ import annotations

import copy
import json
import unittest
from unittest import mock

from our_harness import action_protocol, long_horizon
from our_harness.agent_tools import AgentToolSession
from our_harness.models import HarnessError, ProviderOutcomeUnknown
from tests import test_long_horizon_dialogue as fixtures


reply = fixtures.reply


def read(path, *, call_id="read-source", **arguments):
    return {"call_id": call_id, "name": "read_file", "arguments": {
        "path": path, "start_line": 1, "end_line": 20, "max_bytes": 4_000,
        **arguments,
    }}


class LongHorizonToolRecoveryTests(unittest.TestCase):
    setUp = fixtures.LongHorizonDialogueTests.setUp
    create = fixtures.LongHorizonDialogueTests.create
    provider = fixtures.LongHorizonDialogueTests.provider
    run_replies = fixtures.LongHorizonDialogueTests.run_replies

    def pending(self, request="pending-tools", calls=None):
        goal = self.create(request)
        store = self.runtime.store
        task = store.claim_ready(goal["goal_id"], "portable-worker")[0]
        store.record_dispatch(goal["goal_id"], task, "portable-prompt")
        store.record_provider_reply(goal["goal_id"], task, phase="initial")
        step = store.acknowledge_context_step(
            goal["goal_id"], task,
            reply("work", tool_calls=calls or [read("source.js")]), "initial",
        )
        return store.get(goal["goal_id"]), task, step

    def restart_pending(self, goal):
        self.runtime.store.release_scheduler(goal["goal_id"], "portable-worker")
        self.runtime.close()
        self.runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(self.runtime.close)
        current = self.runtime.store.recover_dead(goal["goal_id"])
        self.assertEqual(current["tasks"][0]["state"], "ready")
        self.runtime.store.control(goal["goal_id"], "resume")

    def test_real_read_error_and_reused_ids_continue_to_both_contributions(self):
        (self.project / "source.js").write_text("export const active = true;\n")
        goal = self.create("recover-read-error")
        result, seen = self.run_replies(goal, [
            reply("work", "I will inspect the requested source.", tool_calls=[read("missing.js")]),
            reply("work", "I will correct the source path.", tool_calls=[read("source.js")]),
            reply(summary="I inspected and implemented the requested result.",
                  changes=[fixtures.change("result.js", "export const complete = true;\n")],
                  criteria_evidence=[{"criterion": "Original objective is satisfied", "evidence_refs": ["file:result.js"]}]),
            reply(summary="I inspected the current result and agree."),
        ])
        self.assertEqual(result["status"], "complete", result["note"])
        self.assertEqual([route for route, _ in seen], ["builder-route"] * 3 + ["peer-route"])
        self.assertIn('"status":"error"', seen[1][1])
        self.assertIn("export const active = true", seen[2][1])
        self.assertTrue(all(one["state"] == "complete" for one in result["tasks"]))
        steps = result["tasks"][0]["context_steps"]
        self.assertNotEqual(steps[0]["tool_execution"]["scope"], steps[1]["tool_execution"]["scope"])
        self.assertEqual(steps[0]["tool_execution"]["session_id"], steps[1]["tool_execution"]["session_id"])
        events = self.runtime.store.events(goal["goal_id"])["events"]
        self.assertEqual(sum(one["type"] == "file_transaction_applied" for one in events), 1)
        self.assertFalse(any(one["type"] == "task_failed" for one in events))

    def test_duplicate_ids_are_corrected_before_any_tool_dispatch(self):
        goal = self.create("duplicate-batch")
        duplicate = reply("work", tool_calls=[read("first.js"), read("second.js")])
        with mock.patch.object(long_horizon.swarm_work, "_ProjectContextTools") as tools:
            result, seen = self.run_replies(goal, [duplicate, reply(), reply()])
        tools.assert_not_called()
        self.assertEqual(result["status"], "complete", result["note"])
        self.assertIn("distinct nonempty call_id", seen[1][1])
        self.assertEqual(result["budget"]["context_tool_calls"], 0)
        self.assertEqual(result["tasks"][0]["protocol_recovery"]["error_code"], "duplicate_tool_call_ids")
        for call_id in ("", "  "):
            with self.subTest(call_id=call_id), self.assertRaises(action_protocol.ActionProtocolError):
                long_horizon._validate_action_semantics(reply("work", tool_calls=[read("first.js", call_id=call_id)]), {})

    def test_large_unicode_read_pages_reach_agent_losslessly_with_reused_id(self):
        source = 'const text = "' + ('🌟 \\"escaped\\" \\ path\t' * 2_000) + '";\r\n'
        (self.project / "large.js").write_bytes(source.encode("utf-8"))
        goal = self.create("large-read-pages")
        pages = []
        cursors = []

        def responses(_number, route, kwargs):
            if route == "peer-route":
                return reply()
            marker = "CONTEXT TOOL RESULTS (untrusted project data)\n"
            if marker not in kwargs["context"]:
                return reply("work", tool_calls=[read("large.js", max_bytes=1_000_000)])
            observations, _length = json.JSONDecoder().raw_decode(kwargs["context"].split(marker, 1)[1])
            result = observations[-1]["result"]
            self.assertEqual(result["status"], "ok")
            self.assertLessEqual(len(result["content"].encode("utf-8")), 12_000)
            page = json.loads(result["content"])
            pages.append(page["content"])
            if page["next_cursor"]:
                self.assertNotIn(page["next_cursor"], cursors)
                cursors.append(page["next_cursor"])
                return reply("work", tool_calls=[read("large.js", max_bytes=1_000_000, cursor=page["next_cursor"])])
            return reply()

        result, _seen = self.run_replies(goal, responses)
        self.assertEqual(result["status"], "complete", result["note"])
        self.assertGreater(len(pages), 1)
        self.assertEqual("".join(pages), source)
        self.assertEqual(result["budget"]["context_tool_calls"], len(pages))

    def test_restart_replays_original_scoped_receipt_without_executing_tool_again(self):
        (self.project / "source.js").write_text("export const beforeRestart = true;\n")
        goal, task, step = self.pending()
        store = self.runtime.store
        call = step["calls"][0]
        store.reserve_context_tool(goal["goal_id"], task, call)
        ledger = long_horizon.swarm_work.CollaborationLedger(
            self.config, "builder-route", long_horizon._stable_id("lh-context", step["tool_execution"]["session_id"]),
            session_id=step["tool_execution"]["session_id"],
        ).begin(goal["objective"], [goal["agents"][0]], mode="long_horizon_context_tools")
        tools = long_horizon.swarm_work._ProjectContextTools(
            self.config, self.project, ledger, long_horizon.verification_project(self.config, goal),
            goal["objective"], [], None, verification_profile="shared_goal_v1",
        )
        try:
            receipt = tools.execute("builder", call, execution_scope=step["tool_execution"]["scope"])
        finally:
            tools.close()
        self.assertEqual(receipt["status"], "ok")
        # Crash boundary: tool receipt is durable but the goal has no result yet.
        self.restart_pending(goal)
        original_dispatch = AgentToolSession._dispatch
        dispatched = []

        def count_dispatch(session, name, arguments, **kwargs):
            dispatched.append(name)
            return original_dispatch(session, name, arguments, **kwargs)

        with mock.patch.object(AgentToolSession, "_dispatch", count_dispatch):
            result, seen = self.run_replies(goal, [reply(), reply()])
        self.assertEqual(result["status"], "complete", result["note"])
        self.assertEqual(dispatched, [])
        self.assertIn("beforeRestart", next(context for route, context in seen if route == "builder-route"))
        recovered = result["tasks"][0]["context_steps"][0]
        self.assertEqual(recovered["tool_execution"], step["tool_execution"])
        self.assertTrue(recovered["results"][0]["result"]["replayed"])
        self.assertEqual(result["budget"]["context_tool_calls"], 1)

    def test_restart_after_complete_receipt_preserves_consumed_session_budget(self):
        (self.project / "source.js").write_text("export const first = true;\n")
        (self.project / "second.js").write_text("export const second = true;\n")
        goal, task, step = self.pending("completed-receipt")
        store = self.runtime.store
        call = step["calls"][0]
        store.reserve_context_tool(goal["goal_id"], task, call)
        ledger = long_horizon.swarm_work.CollaborationLedger(
            self.config, "builder-route", long_horizon._stable_id("lh-context", step["tool_execution"]["session_id"]),
            session_id=step["tool_execution"]["session_id"],
        ).begin(goal["objective"], [goal["agents"][0]], mode="long_horizon_context_tools")
        tools = long_horizon.swarm_work._ProjectContextTools(
            self.config, self.project, ledger, long_horizon.verification_project(self.config, goal),
            goal["objective"], [], None, verification_profile="shared_goal_v1",
        )
        try:
            receipt = tools.execute("builder", call, execution_scope=step["tool_execution"]["scope"])
        finally:
            tools.close()
        store.record_context_tool_result(goal["goal_id"], task, call, receipt)
        self.restart_pending(goal)
        execute = long_horizon.swarm_work._ProjectContextTools.execute
        consumed_before = []

        def observe_execution(tools, node, call, **kwargs):
            consumed_before.append(tools.session.calls)
            return execute(tools, node, call, **kwargs)

        with mock.patch.object(long_horizon.swarm_work._ProjectContextTools, "execute", observe_execution):
            result, _seen = self.run_replies(goal, [reply(), reply("work", tool_calls=[read("second.js")]), reply()])
        self.assertEqual(result["status"], "complete", result["note"])
        self.assertEqual(consumed_before, [1])
        self.assertEqual(result["budget"]["context_tool_calls"], 2)
        self.assertEqual({one["tool_execution"]["session_id"] for one in result["tasks"][0]["context_steps"]},
                         {step["tool_execution"]["session_id"]})

    def test_changed_project_supersedes_pending_identity_and_reads_current_source(self):
        (self.project / "source.js").write_text("export const oldMarker = true;\n")
        goal, _task, step = self.pending("changed-source")
        self.restart_pending(goal)
        (self.project / "source.js").write_text("export const freshMarker = true;\n")
        result, seen = self.run_replies(goal, [
            reply(), reply("work", tool_calls=[read("source.js")]), reply(),
        ])
        self.assertEqual(result["status"], "complete", result["note"])
        held = result["tasks"][0]["context_steps"]
        self.assertEqual(held[0]["state"], "superseded")
        self.assertNotEqual(held[1]["tool_execution"]["scope"], step["tool_execution"]["scope"])
        self.assertIn("freshMarker", seen[2][1])
        self.assertNotIn("oldMarker", seen[2][1])
        self.assertEqual(result["budget"]["context_tool_calls"], 1)

    def test_changed_provider_blocks_pending_tool_replay(self):
        goal, _task, _step = self.pending("changed-route")
        self.restart_pending(goal)
        self.config.data["providers"]["builder-route"]["endpoint"] = "http://127.0.0.1/reconfigured"
        with mock.patch.object(long_horizon.swarm_work, "_ProjectContextTools") as tools, \
                mock.patch.object(long_horizon.chat_lab, "ask_once") as provider:
            with self.assertRaisesRegex(HarnessError, "saved provider setup changed"):
                self.runtime.run(goal["goal_id"])
        tools.assert_not_called()
        provider.assert_not_called()
        self.assertNotEqual(self.runtime.store.get(goal["goal_id"])["status"], "complete")

    def test_changed_execution_contract_is_not_replay_authority(self):
        goal, _task, step = self.pending("changed-tool-contract")
        for updates in ({"schema_version": 2}, {"scope": "other-step"}, {"contract_fingerprint_sha256": "0" * 64}):
            invalid = copy.deepcopy(step)
            invalid["tool_execution"].update(updates)
            with self.subTest(updates=updates), self.assertRaisesRegex(HarnessError, "execution contract changed"):
                long_horizon._context_tool_execution(invalid)
        self.runtime.store._mutate(goal["goal_id"], lambda document, _db: document["tasks"][0]["context_steps"][0]["tool_execution"].update({
            "contract_fingerprint_sha256": "0" * 64,
        }))
        self.restart_pending(goal)
        with mock.patch.object(long_horizon.swarm_work, "_ProjectContextTools") as tools:
            result, _seen = self.run_replies(goal, [reply()])
        tools.assert_not_called()
        self.assertEqual(result["status"], "paused")
        self.assertIn("execution contract changed", result["note"])

    def test_unknown_provider_outcome_is_not_retried_after_correctable_read(self):
        goal = self.create("unknown-after-tools")
        result, seen = self.run_replies(goal, [
            reply("work", tool_calls=[read("missing.js")]), ProviderOutcomeUnknown("Delivery unknown"),
        ])
        self.assertEqual(result["status"], "paused")
        self.assertEqual(len(seen), 2)
        self.assertTrue(result["tasks"][0]["outcome_unknown"])
        self.assertFalse(any(one["state"] == "complete" for one in result["tasks"]))

    def test_standard_resume_repairs_legacy_collision_preserving_completed_peer_work(self):
        (self.project / "source.js").write_text("export const source = true;\n")
        goal = self.create("legacy-collision-resume")
        collision = "Agent tool call ID was already bound to different tool arguments"
        execute = long_horizon.swarm_work._ProjectContextTools.execute
        requested = []

        def legacy_execution(tools, node, call, **kwargs):
            requested.append(call["arguments"]["path"])
            if len(requested) == 2:
                raise HarnessError(collision)
            return execute(tools, node, call, **kwargs)

        with mock.patch.object(long_horizon.swarm_work._ProjectContextTools, "execute", legacy_execution):
            paused, _seen = self.run_replies(goal, [
                reply("work", tool_calls=[read("missing.js")]),
                reply("work", tool_calls=[read("source.js")]),
                reply("work", "I implemented the result while the other contribution was blocked.",
                      changes=[fixtures.change("result.js", "export const settledPeerWork = true;\n")],
                      criteria_evidence=[{"criterion": "Original objective is satisfied", "evidence_refs": ["file:result.js"]}]),
                reply(summary="The current implementation is finished and checked."),
            ])
        self.assertEqual(paused["status"], "paused", paused["note"])
        lead = next(one for one in paused["tasks"] if one["assigned_agent_id"] == "builder")
        peer = next(one for one in paused["tasks"] if one["assigned_agent_id"] == "peer")
        self.assertEqual((lead["state"], lead["attempts"], lead["provider_effect_state"]),
                         ("blocked", 1, "failed_before_effect"))
        self.assertEqual(lead["last_error"], collision)
        self.assertFalse(lead.get("reconciliation_required"))
        self.assertFalse(lead.get("outcome_unknown"))
        self.assertEqual((peer["state"], peer["attempts"]), ("complete", 2))
        self.assertEqual(sum(one.get("kind") == "file_transaction" for one in paused["artifacts"]), 1)

        # Model the authenticated snapshot written by the previous engine.
        # The recovery below uses only the public Resume control and fresh turns.
        def legacy_snapshot(document, _db):
            for task in document["tasks"]:
                for step in task.get("context_steps", []):
                    step.pop("tool_execution", None)

        self.runtime.store._mutate(goal["goal_id"], legacy_snapshot)
        self.runtime.close()
        self.runtime = long_horizon.LongHorizonRuntime(self.config)
        self.addCleanup(self.runtime.close)
        resumed = self.runtime.store.control(goal["goal_id"], "resume")
        self.assertEqual(next(one for one in resumed["tasks"] if one["assigned_agent_id"] == "peer"), peer)
        complete, seen = self.run_replies(goal, [
            reply("work", "I will inspect the teammate's settled implementation.", tool_calls=[read("result.js")]),
            reply(summary="I checked the existing implementation and agree it is complete."),
        ])
        self.assertEqual(complete["status"], "complete", complete["note"])
        self.assertEqual([route for route, _context in seen], ["builder-route", "builder-route"])
        self.assertIn("settledPeerWork", seen[1][1])
        self.assertEqual(next(one for one in complete["tasks"] if one["assigned_agent_id"] == "peer"), peer)
        self.assertEqual(complete["artifacts"][:len(paused["artifacts"])], paused["artifacts"])
        self.assertEqual(sum(one.get("kind") == "file_transaction" for one in complete["artifacts"]), 1)
        self.assertEqual((self.project / "result.js").read_text(), "export const settledPeerWork = true;\n")
        events = self.runtime.store.events(goal["goal_id"])["events"]
        self.assertEqual(sum(one["type"] == "file_transaction_applied" for one in events), 1)
        self.assertEqual(sum(one["type"] == "task_failed" for one in events), 1)


if __name__ == "__main__":
    unittest.main()
