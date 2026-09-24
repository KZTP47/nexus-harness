from __future__ import annotations

import copy
import json
import unittest
import unittest.mock

from our_harness import goal_context_progress as progress, long_horizon
from our_harness.models import HarnessError


class GoalContextProgressTests(unittest.TestCase):
    def setUp(self):
        self.binding = {"goal_id": "portable-goal", "task_id": "inspect", "source_sha256": "a" * 64,
                        "objective_epoch": 1, "route_fingerprint": "b" * 64}
        self.messages = [{"id": "peer-1", "agent_id": "peer", "sequence": 1, "summary": "Please inspect the input handler."}]

    def step(self, number, *, result=None, arguments=None, name="read_file"):
        call_id = "call-" + str(number)
        return {
            "step_id": "step-" + str(number), "state": "complete",
            "calls": [{"call_id": call_id, "name": name, "arguments": arguments or {"path": "src/input.js"}}],
            "results": [{"call_id": call_id, "name": name, "result": result or {
                "call_id": call_id, "span_id": "new-span-" + str(number), "elapsed_ms": number * 15,
                "replayed": number % 2 == 0, "content_bytes": number * 100,
                "content": json.dumps({"content": "export const held = {};", "next_cursor": ""}),
            }, "error": ""}],
        }

    def observe(self, previous, step, *, binding=None, messages=None):
        return progress.observe(previous, step, binding=binding or self.binding, speaker_id="author",
                                messages=self.messages if messages is None else messages,
                                normalize=long_horizon._semantic_tool_result)

    def test_identical_observations_notify_the_agent_and_never_pause(self):
        held = {}
        for number in range(progress.NOTICE_AFTER_IDENTICAL_REPEATS + 20):
            step = self.step(number)
            step["tool_execution"] = {"scope": "fresh-scope-" + str(number), "session_id": "fresh-session-" + str(number)}
            held = self.observe(held, step)
            self.assertEqual(held["identical_repeats"], number)
            self.assertNotEqual(held["state"], "paused")
            if number < progress.NOTICE_AFTER_IDENTICAL_REPEATS:
                self.assertEqual(held["state"], "tracking")
                self.assertEqual(held["notice"], "")
        self.assertEqual(held["state"], "repeating")
        self.assertIn("returned the same result", held["notice"])
        self.assertNotIn("Resume", held["notice"])
        self.assertNotIn("export const", json.dumps(held))

    def test_repeat_counter_saturates_within_its_durable_bound(self):
        held = {}
        with unittest.mock.patch.object(progress, "MAX_TRACKED_REPEATS", 6):
            for number in range(10):
                held = self.observe(held, self.step(number))
        self.assertEqual(held["identical_repeats"], 6)
        self.assertEqual(held["state"], "repeating")

    def test_repeated_recoverable_errors_become_a_notice_not_a_pause(self):
        failure = {"status": "error", "content": json.dumps({"error": "[wrong-skill-reader] use read_file"})}
        held = {}
        for number in range(progress.NOTICE_AFTER_IDENTICAL_REPEATS + 3):
            step = self.step(number, name="read_local_skill", arguments={"path": "notes-" + str(number) + ".md"},
                             result=failure)
            step["results"][0]["error"] = "[wrong-skill-reader] use read_file"
            held = self.observe(held, step)
            self.assertNotEqual(held["state"], "paused")
        self.assertEqual(held["state"], "repeating")
        self.assertIn("read_file", held["notice"])

    def test_changed_cursor_or_real_result_extends_arbitrarily_long_exploration(self):
        for changed in ("cursor", "result"):
            with self.subTest(changed=changed):
                held = {}
                for number in range(50):
                    args = {"path": "src/input.js", "cursor": str(number) if changed == "cursor" else ""}
                    value = {"content": json.dumps({"content": str(number) if changed == "result" else "same", "next_cursor": ""})}
                    held = self.observe(held, self.step(number, arguments=args, result=value))
                    self.assertEqual(held["state"], "tracking")
                    self.assertEqual(held["identical_repeats"], 0)

    def test_restart_retains_repeat_count_and_duplicate_completed_step_is_idempotent(self):
        held = {}
        for number in range(4):
            held = self.observe(held, self.step(number))
        restarted = json.loads(json.dumps(held))
        self.assertEqual(self.observe(restarted, self.step(3)), restarted)
        noticed = self.observe(restarted, self.step(4))
        self.assertEqual(noticed["state"], "repeating")
        self.assertEqual(noticed["identical_repeats"], 4)

    def test_peer_or_addressed_user_message_refreshes_evidence_but_own_request_does_not(self):
        initial = self.observe({}, self.step(0))
        own = [*self.messages, {"id": "my-request", "agent_id": "author", "summary": "I will inspect again."}]
        same = self.observe(initial, self.step(1), messages=own)
        self.assertEqual(same["identical_repeats"], 1)
        for new in (
            {"id": "peer-2", "agent_id": "peer", "summary": "The key is the release handler."},
            {"id": "user-2", "agent_id": "", "summary": "Please inspect the keyboard path.",
             "visibility": "agent_only", "recipient": {"agent_id": "author"}},
        ):
            with self.subTest(new=new):
                fresh = self.observe(same, self.step(2), messages=[*own, new])
                self.assertEqual(fresh["identical_repeats"], 0)
        private_other = {"id": "user-private", "agent_id": "", "summary": "Other participant's private direction.",
                         "visibility": "agent_only", "recipient": {"agent_id": "peer"}}
        unchanged = self.observe(same, self.step(2), messages=[*own, private_other])
        self.assertEqual(unchanged["identical_repeats"], 2)

    def test_conversation_polling_cannot_claim_its_own_request_echoes_as_progress(self):
        held = {}
        for number in range(5):
            page = {"messages": [*self.messages, {"id": "own-" + str(number), "agent_id": "author",
                     "sequence": number + 2, "summary": "Looking for the latest message " + str(number)}],
                    "latest_sequence": number + 2, "total_messages": number + 2, "next": number + 2,
                    "has_more": number % 2 == 0, "coverage": {"status": "complete"}}
            held = self.observe(held, self.step(number, name="read_shared_conversation", result=page,
                                               arguments={"after": 0, "limit": 20}))
        self.assertEqual(held["state"], "repeating")
        page["messages"].append({"id": "peer-new", "agent_id": "peer", "sequence": 9, "summary": "I have a finding."})
        fresh = self.observe(held, self.step(6, name="read_shared_conversation", result=page,
                                            arguments={"after": 0, "limit": 20}))
        self.assertEqual(fresh["state"], "tracking")

    def test_verification_elapsed_time_is_not_evidence_but_changed_test_output_is(self):
        held = {}
        for number in range(5):
            held = self.observe(held, self.step(number, name="run_selected_verification", result={
                "status": "passed", "commands": [{"duration_ms": number, "stderr": f"Ran 1 test in 0.{number}s\nOK\n"}],
            }))
        self.assertEqual(held["state"], "repeating")
        changed = self.observe(held, self.step(5, name="run_selected_verification", result={
            "status": "failed", "commands": [{"exit_code": 1, "stderr": "AssertionError: reward balance"}],
        }))
        self.assertEqual(changed["state"], "tracking")

    def test_project_objective_route_or_engine_contract_change_invalidates_old_counter(self):
        held = self.observe({}, self.step(0))
        held = self.observe(held, self.step(1))
        for field in ("goal_id", "task_id", "source_sha256", "objective_epoch", "route_fingerprint"):
            with self.subTest(field=field):
                changed = {**self.binding, field: "different"}
                refreshed = self.observe(held, self.step(2), binding=changed)
                self.assertEqual(refreshed["identical_repeats"], 0)
        obsolete = {**held, "contract_fingerprint_sha256": "0" * 64}
        refreshed = self.observe(obsolete, self.step(2))
        self.assertEqual(refreshed["identical_repeats"], 0)
        self.assertEqual(refreshed["contract_fingerprint_sha256"], progress.contract_fingerprint())

    def test_pending_or_missing_results_cannot_advance_counter_and_malformed_saved_record_fails(self):
        held = self.observe({}, self.step(0))
        pending = self.step(1)
        pending["state"] = "tools_pending"
        self.assertEqual(self.observe(held, pending), held)
        for malformed in ("missing", "duplicate", "mismatched"):
            step = self.step(1)
            if malformed == "missing":
                step["results"] = []
            elif malformed == "duplicate":
                step["results"].append(copy.deepcopy(step["results"][0]))
            else:
                step["results"][0]["name"] = "different_tool"
            with self.subTest(malformed=malformed), self.assertRaises(HarnessError):
                self.observe(held, step)
        for bad_count in (-1, True, progress.MAX_TRACKED_REPEATS + 1, "1"):
            with self.subTest(bad_count=bad_count), self.assertRaises(HarnessError):
                self.observe({**held, "identical_repeats": bad_count}, self.step(1))
        with self.assertRaises(HarnessError):
            self.observe({**held, "state": "paused"}, self.step(1))
        obsolete_pause = {**held, "schema_version": 1, "state": "paused"}
        self.assertEqual(self.observe(obsolete_pause, self.step(1))["state"], "tracking")
        with self.assertRaisesRegex(HarnessError, "changed after"):
            self.observe(held, self.step(0, result={"content": "changed after receipt"}))


if __name__ == "__main__":
    unittest.main()
