from __future__ import annotations

import copy
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from our_harness import chat, goal_chat_projection, long_horizon
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError
from our_harness.server import HarnessHTTPServer


class GoalChatProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.filed_as = "another-teams-conversation"
        self.goal = {
            "goal_id": "goal-a", "request_id": "request-a", "conversation_id": "chat-a",
            "project": {"id": "project-a"}, "lead_agent_id": "builder",
            "require_all_participants": True, "status": "paused", "revision": 10,
            "event_seq": 5000, "dialogue": {"sequence": 205},
            "agents": [{"id": "builder", "name": "Builder", "who": "route-blue"},
                       {"id": "checker", "name": "Checker", "who": "route-orange"}],
            "tasks": [], "note": "No deterministic verification command is available.",
            "next_step": "Add the project's verification command and resume.",
        }
        prompt = "Discuss and create the requested artifact"
        chat.keep_long_horizon_prompt(
            self.config, "route-blue", prompt, filed_as=self.filed_as,
            request_id="request-a", chat_id="chat-a", project_id="project-a", lead_id="builder",
            intent_sha256=chat.long_horizon_intent_sha256("chat-a", "project-a", "builder", prompt, []),
        )

    def message(self, sequence, text=None, *, user=False):
        return {
            "id": f"message-{sequence}", "goal_id": "goal-a", "sequence": sequence,
            "agent_id": "" if user else "builder" if sequence % 2 else "checker",
            "task_id": "task-a", "action": "steer" if user else "work", "phase": "user" if user else "action",
            "summary": text if text is not None else f"Public message {sequence}",
            "recipient": {"schema_version": 1, "kind": "agent", "agent_id": "checker", "name": "Checker"},
            "at_ms": 1_700_000_000_000 + sequence * 1000, "objective_epoch": 1,
            "source_goal_event_id": f"{sequence:032x}", "source_goal_event_seq": sequence * 10,
            "source_goal_event_type": "goal_steered" if user else "provider_acknowledged",
        }

    def page(self, messages, *, more=False, partial=False):
        return {"schema_version": 1, "goal_id": "goal-a", "messages": messages,
                "next": messages[-1]["sequence"] if messages else 0, "has_more": more,
                "coverage": {"status": "partial_legacy" if partial else "complete"},
                "latest_sequence": 205, "total_messages": 205}

    def project(self, page, config=None, goal=None):
        return goal_chat_projection.keep_page(
            config or self.config, "route-blue", goal or self.goal, page, filed_as=self.filed_as,
        )

    def speech(self, config=None):
        return [one for one in chat.read_it(config or self.config, "route-blue", self.filed_as)
                if one.correlation.get("source_dialogue_id")]

    def server(self, archive):
        state = mock.MagicMock()
        state.config = self.config
        state.swarm_lock = threading.Lock()
        state.swarm_standing.return_value = {"board": {}}
        state._long_horizon_chat.return_value = {"transcript_route": "route-blue", "filed_as": self.filed_as}
        state.long_horizon.store.dialogue_history.side_effect = archive
        state.long_horizon.store.events.return_value = {
            "events": [], "has_more": False, "oldest_available": 5001, "truncated": True,
        }
        return state

    def test_unicode_speech_and_user_steering_stay_exact_once_after_restart(self):
        words = "🙂" * 8000
        messages = [self.message(1, words), self.message(2, "Use arrow keys.", user=True)]
        self.project(self.page(messages))
        reopened = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.project(self.page(messages), config=reopened)
        turns = self.speech(reopened)
        self.assertEqual([one.text for one in turns], [words, "Use arrow keys."])
        self.assertEqual([one.who for one in turns], ["them", "you"])
        self.assertEqual(turns[0].recipient_name, "Checker")
        self.assertEqual(goal_chat_projection.cursor(reopened, "route-blue", "goal-a", filed_as=self.filed_as), 2)

    def test_old_claims_are_marked_as_working_copy_reports_without_rewriting_or_duplicate_delivery(self):
        words = "Finished. Open arbitrary-game/start.html in your project."
        self.project(self.page([self.message(1, words), self.message(2, "Proceed", user=True)]))
        self.goal["execution_workspace"] = {"schema_version": 1, "path": "private/project"}
        self.goal["project"]["path"] = str(self.root / "selected project")
        self.goal["status"] = "complete"
        self.project(self.page([]))
        reopened = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.project(self.page([]), config=reopened)
        rows = self.speech(reopened)
        self.assertEqual([row.text for row in rows], [words, "Proceed"])
        self.assertEqual(rows[0].correlation["delivery_contract"], "selected-project-delivery/v1")
        self.assertEqual(rows[0].correlation["delivery_state"], "working_copy_report")
        self.assertNotIn("delivery_state", rows[1].correlation)

    def test_legacy_complete_status_gains_verified_location_once_at_same_revision(self):
        self.goal["status"] = "complete"
        chat.keep_long_horizon_status(self.config, "route-blue", self.goal, filed_as=self.filed_as)
        self.goal["delivery_receipt"] = {"schema_version": 1, "contract": "selected-project-delivery/v1",
                                         "state": "delivered", "project_path": str(self.root),
                                         "files": [{"path": "game/start.html", "sha256": "a" * 64}]}
        for _ in range(2):
            chat.keep_long_horizon_status(self.config, "route-blue", self.goal, filed_as=self.filed_as)
        rows = [row for row in chat.read_it(self.config, "route-blue", self.filed_as)
                if row.correlation.get("kind") == "long_horizon_status"]
        self.assertEqual(len(rows), 1)
        self.assertIn(str(self.root / "game/start.html"), rows[0].text)
        self.assertEqual(rows[0].correlation["goal_revision"], 10)

    def tool_step(self, *, result=False):
        return {"step_id": "portable-step", "agent_id": "builder", "state": "complete" if result else "tools_pending",
                "created_ms": 1_700_000_001_500, "completed_ms": 1_700_000_001_750 if result else 0,
                "private_provider_reasoning": "THIS MUST NEVER BECOME CHAT CONTENT",
                "calls": [{"call_id": "portable-call", "name": "read_file",
                           "arguments": {"path": "src/arbitrary.js", "api_key": "argument-secret"}}],
                "results": [{"call_id": "portable-call", "name": "read_file",
                             "result": {"content": "const ready = true;", "password": "output-secret"}, "error": ""}]
                if result else []}

    def tool_rows(self, config=None):
        return [one for one in chat.read_it(config or self.config, "route-blue", self.filed_as)
                if one.phase == "agent_tool"]

    def test_tool_history_recovers_from_steps_without_event_or_dialogue_polling(self):
        self.goal["tasks"] = [{"id": "task-a", "assigned_agent_id": "checker", "context_steps": [self.tool_step(result=True)]}]
        self.project(self.page([self.message(1, "I am checking the current files."), self.message(2, "The file is ready.")]))
        rows = chat.read_it(self.config, "route-blue", self.filed_as)
        evidence_rows = [one for one in rows if one.correlation.get("goal_id") == "goal-a"]
        self.assertEqual([one.phase for one in evidence_rows], ["agent_discussion", "agent_tool", "agent_discussion"])
        activity = self.tool_rows()[0]
        self.assertEqual(activity.speaker_id, "nexus")
        self.assertEqual(activity.speaker_name, "Builder · tool activity", "Use original step ownership, not reassigned task owner")
        payload = json.loads(activity.text)
        self.assertEqual(payload["status"], "finished")
        self.assertEqual(payload["arguments"]["path"], "src/arbitrary.js")
        self.assertEqual(payload["result"]["content"], "const ready = true;")
        self.assertEqual(payload["arguments"]["api_key"], "[REDACTED]")
        self.assertEqual(payload["result"]["password"], "[REDACTED]")
        self.assertNotIn("THIS MUST NEVER", activity.text)
        self.assertNotIn("argument-secret", activity.text)
        reopened = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.project(self.page([]), config=reopened)
        self.assertEqual(len(self.tool_rows(reopened)), 1)
        self.assertEqual(self.tool_rows(reopened)[0].text, activity.text)

    def test_pending_tool_activity_updates_once_and_delayed_poll_cannot_downgrade_it(self):
        self.goal["tasks"] = [{"id": "task-a", "context_steps": [self.tool_step()]}]
        earlier = copy.deepcopy(self.goal)
        self.project(self.page([]))
        pending = self.tool_rows()[0]
        self.assertEqual(json.loads(pending.text)["status"], "requested")
        step = self.goal["tasks"][0]["context_steps"][0]
        step["results"] = [{"call_id": "portable-call", "result": {"status": "failed", "exit_code": 9}, "error": ""}]
        step.update({"state": "complete", "completed_ms": 1_700_000_009_000})
        self.goal["revision"] += 1
        self.project(self.page([]))
        done = self.tool_rows()[0]
        self.assertEqual(json.loads(done.text)["status"], "failed")
        self.assertEqual(done.correlation["event_id"], pending.correlation["event_id"])
        self.assertGreater(done.at, pending.at)
        self.project(self.page([]), goal=earlier)
        self.assertEqual([one.text for one in self.tool_rows()], [done.text])

    def test_legacy_tool_ownership_is_not_guessed_and_superseded_requests_do_not_claim_execution(self):
        step = self.tool_step()
        step.pop("agent_id")
        step["state"] = "superseded"
        self.goal["tasks"] = [{"id": "task-a", "assigned_agent_id": "checker", "context_steps": [step]}]
        self.project(self.page([]))
        row = self.tool_rows()[0]
        self.assertEqual(row.speaker_name, "Team · tool activity")
        self.assertEqual(json.loads(row.text)["status"], "superseded")
        self.assertNotIn("result", json.loads(row.text))

    def test_failed_tool_envelope_exposes_its_exact_diagnostic_without_losing_original_output(self):
        step = self.tool_step(result=True)
        envelope = {"status": "error", "content": json.dumps({"error": "read_file end_line must be at least start_line"}),
                    "provenance": {"kind": "agent_tool", "untrusted_data": True}, "truncated": False}
        step["results"][0]["result"] = envelope
        self.goal["tasks"] = [{"id": "task-a", "context_steps": [step]}]
        self.project(self.page([]))
        activity = json.loads(self.tool_rows()[0].text)
        self.assertEqual(activity["status"], "failed")
        self.assertEqual(activity["error"], "read_file end_line must be at least start_line")
        self.assertEqual(activity["result"], envelope)
        self.assertEqual(activity["arguments"]["path"], "src/arbitrary.js")
        reopened = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.project(self.page([]), config=reopened)
        self.assertEqual(json.loads(self.tool_rows(reopened)[0].text), activity)

    def test_successful_read_of_error_json_is_file_content_not_a_tool_failure(self):
        step = self.tool_step(result=True)
        step["results"][0]["result"] = {"status": "success", "content": '{"error":"fixture data, not execution failure"}'}
        self.goal["tasks"] = [{"id": "task-a", "context_steps": [step]}]
        self.project(self.page([]))
        activity = json.loads(self.tool_rows()[0].text)
        self.assertEqual(activity["status"], "finished")
        self.assertNotIn("error", activity)
        self.assertEqual(activity["result"], step["results"][0]["result"])

    def test_tool_projection_obeys_origin_chat_binding_and_keeps_actual_public_updates(self):
        self.goal["tasks"] = [{"id": "task-a", "context_steps": [self.tool_step()]}]
        update = {**self.message(1, "I will compare the saved files before changing them."), "phase": "context_tools"}
        self.project(self.page([update]))
        self.assertEqual(self.speech()[0].phase, "agent_progress")
        self.assertEqual(self.speech()[0].text, update["summary"])
        before = [one.to_dict() for one in chat.read_it(self.config, "route-blue", self.filed_as)]
        with self.assertRaises(chat.ChatError):
            self.project(self.page([]), goal={**self.goal, "conversation_id": "another-chat"})
        self.assertEqual([one.to_dict() for one in chat.read_it(self.config, "route-blue", self.filed_as)], before)

    def test_unknown_legacy_recipient_is_not_fabricated_as_the_team(self):
        message = {**self.message(1, "A historical directed answer", user=True),
                   "recipient": {"kind": "unknown", "name": "original recipient (unavailable)"},
                   "visibility": "operator_only"}
        self.project(self.page([message]))
        self.assertEqual(self.speech()[0].recipient_name, "original recipient (unavailable)")

    def test_archive_recovers_before_later_projected_speech_without_duplicates(self):
        late = self.message(3)
        chat.keep_long_horizon_events(self.config, "route-blue", self.goal, [{
            "event_id": late["source_goal_event_id"], "seq": late["source_goal_event_seq"],
            "goal_id": "goal-a", "type": "provider_acknowledged", "agent_id": "builder",
            "task_id": "task-a", "payload": {"summary": late["summary"]},
        }], filed_as=self.filed_as)
        chat.keep_long_horizon_status(self.config, "route-blue", self.goal, filed_as=self.filed_as)
        self.project(self.page([self.message(1), self.message(2), late]))
        self.assertEqual([one.text for one in self.speech()], ["Public message 1", "Public message 2", "Public message 3"])
        turns = chat.read_it(self.config, "route-blue", self.filed_as)
        self.assertEqual(sum(one.text == "Public message 3" for one in turns), 1)
        self.assertLess(next(i for i, one in enumerate(turns) if one.text == "Public message 3"),
                        next(i for i, one in enumerate(turns) if one.phase == "long_horizon_status"))
        records = chat.where_it_is_kept(self.config, "route-blue", self.filed_as).with_suffix(".events.jsonl")
        self.assertTrue(any(json.loads(line)["kind"] == "snapshot" for line in records.read_text().splitlines()))

    def test_legacy_snapshot_matches_by_proven_full_count_and_ordinal_including_repeated_words(self):
        messages = [self.message(1, "The same words"), self.message(2, "An intermediate reply"),
                    self.message(3, "The same words")]
        events = [{"event_id": one["source_goal_event_id"], "seq": one["source_goal_event_seq"],
                   "goal_id": "goal-a", "type": "provider_acknowledged", "agent_id": one["agent_id"],
                   "task_id": one["task_id"], "payload": {"summary": one["summary"]}}
                  for one in messages]
        # Directed user messages never belonged to the old shared counter.
        events.insert(1, {"event_id": "f" * 32, "seq": 15, "goal_id": "goal-a",
                          "type": "agent_messaged", "task_id": "task-a", "payload": {"text": "Only check this task"}})
        chat.keep_long_horizon_events(self.config, "route-blue", self.goal, events, filed_as=self.filed_as)
        snapshots = [{**one, "source_goal_event_id": "", "source_goal_event_seq": 0,
                      "legacy_dialogue_sequence": one["sequence"]} for one in messages]
        page = self.page(snapshots, partial=True)
        page["coverage"].update({"legacy_dialogue_sequence": 3, "legacy_event_seq": 30})
        self.project(page)
        self.project(page, config=LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {}))
        turns = chat.read_it(self.config, "route-blue", self.filed_as)
        replies = [one for one in turns if one.phase == "agent_discussion"]
        self.assertEqual([one.text for one in replies], [one["summary"] for one in messages])
        self.assertEqual([one.correlation["source_goal_event_id"] for one in replies],
                         [one["source_goal_event_id"] for one in messages])
        self.assertFalse(any(one.phase == "recovered_history" for one in turns))

    def test_unproven_legacy_snapshot_is_labeled_recovered_instead_of_new_speech(self):
        original = self.message(1, "Repeated words")
        chat.keep_long_horizon_events(self.config, "route-blue", self.goal, [{
            "event_id": original["source_goal_event_id"], "seq": original["source_goal_event_seq"],
            "goal_id": "goal-a", "type": "provider_acknowledged", "agent_id": "builder",
            "task_id": "task-a", "payload": {"summary": original["summary"]},
        }], filed_as=self.filed_as)
        page = self.page([{**original, "source_goal_event_id": "", "source_goal_event_seq": 0,
                          "legacy_dialogue_sequence": 2}], partial=True)
        page["coverage"].update({"legacy_dialogue_sequence": 3, "legacy_event_seq": 30})
        self.project(page)
        self.project(page)
        turns = chat.read_it(self.config, "route-blue", self.filed_as)
        self.assertEqual(sum(one.phase == "agent_discussion" for one in turns), 1)
        recovered = [one for one in turns if one.phase == "recovered_history"]
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].text, "Repeated words")
        self.assertEqual(recovered[0].correlation["kind"], "long_horizon_recovered_dialogue")

    def test_exact_chat_binding_and_complete_message_boundaries_fail_closed(self):
        wrong = {**self.page([self.message(1)]), "goal_id": "other"}
        with self.assertRaisesRegex(HarnessError, "goal identity"):
            self.project(wrong)
        with self.assertRaises(HarnessError):
            self.project(self.page([self.message(1)]), goal={**self.goal, "conversation_id": "other-chat"})
        with self.assertRaisesRegex(HarnessError, "complete archived message"):
            self.project(self.page([{**self.message(1), "has_more_characters": True}]))
        with self.assertRaisesRegex(HarnessError, "outside this goal"):
            self.project(self.page([{**self.message(1), "agent_id": "outside-agent"}]))
        self.assertEqual(self.speech(), [])

    def test_server_pages_all_speech_when_operational_events_were_pruned(self):
        messages = [self.message(number) for number in range(1, 206)]
        def archive(goal_id, after=0, **kwargs):
            self.assertEqual(goal_id, "goal-a")
            batch = [one for one in messages if one["sequence"] > after][:100]
            return self.page(batch, more=bool(batch and batch[-1]["sequence"] < 205))
        state = self.server(archive)
        HarnessHTTPServer.project_long_horizon_chat_statuses(state, [self.goal])
        self.assertEqual([one.text for one in self.speech()], [one["summary"] for one in messages])
        self.assertEqual(state.long_horizon.store.dialogue_history.call_count, 3)
        state.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        HarnessHTTPServer.project_long_horizon_chat_statuses(state, [self.goal])
        self.assertEqual(len(self.speech()), 205)
        statuses = [one.text for one in chat.read_it(self.config, "route-blue", self.filed_as)
                    if one.phase == "long_horizon_status"]
        self.assertIn(self.goal["note"], statuses[-1])
        self.assertIn(self.goal["next_step"], statuses[-1])

    def test_server_reassembles_large_user_message_before_display(self):
        words = "Build the agreed world. " * 6000
        full = self.message(1, words, user=True)
        def archive(goal_id, after=0, message_id="", offset=0, character_limit=96000, **kwargs):
            if after:
                return self.page([])
            fragment = {**full, "summary": words[offset:offset + character_limit], "offset": offset,
                        "total_characters": len(words), "has_more_characters": offset + character_limit < len(words)}
            return self.page([fragment])
        state = self.server(archive)
        HarnessHTTPServer.project_long_horizon_chat_statuses(state, [self.goal])
        self.assertEqual(self.speech()[0].text, words)
        self.assertEqual(state.long_horizon.store.dialogue_history.call_count, 2)

    def test_server_never_projects_future_speech_through_an_earlier_goal_snapshot(self):
        state = self.server(lambda *args, **kwargs: self.page([self.message(1), self.message(2)]))
        earlier = {**self.goal, "event_seq": 10, "dialogue": {"sequence": 1}}
        HarnessHTTPServer.project_long_horizon_chat_statuses(state, [earlier])
        self.assertEqual([one.text for one in self.speech()], ["Public message 1"])

    def test_legacy_archive_renumbering_is_not_clipped_by_the_old_dialogue_counter(self):
        # Migration adds a formerly uncounted directed user message; event
        # identity still proves all three rows precede this paused snapshot.
        messages = [self.message(1), self.message(2, "A directed note", user=True), self.message(3)]
        state = self.server(lambda *args, **kwargs: self.page(messages))
        HarnessHTTPServer.project_long_horizon_chat_statuses(state, [{
            **self.goal, "event_seq": 30, "dialogue": {"sequence": 2},
        }])
        self.assertEqual([one.text for one in self.speech()], [one["summary"] for one in messages])

    def test_partial_legacy_coverage_is_disclosed_once(self):
        page = self.page([self.message(4)], partial=True)
        self.project(page)
        self.project(page)
        gaps = [one for one in chat.read_it(self.config, "route-blue", self.filed_as) if one.phase == "nexus_gap"]
        self.assertEqual(len(gaps), 1)
        self.assertIn("could not be recovered", gaps[0].text)

    def test_team_composer_reports_the_actual_follow_up_and_context_limits(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for the live composer contract")
        source = (Path(__file__).resolve().parents[1] / "src/our_harness/ui/app.js").read_text(encoding="utf-8")
        limits = source[source.index("const TEAM_FOLLOW_UP_CHARACTERS"):
                        source.index("function outputBudgetFact")]
        context = source[source.index("function longHorizonContextFact"):
                         source.index("// Which refresh is the newest.")]
        probe = """
const assert = require('node:assert/strict');
const swarmChatLimits = new Map();
const chatLimits = {input_characters: 200000, long_horizon_context: {prompt_transcript_characters: 120000}};
let activeGoal = null;
function chatLongGoalContext() { return {goal: activeGoal}; }
""" + limits + context + """
assert.equal(limitsForSwarmChat('a').input_characters, 200000);
assert.match(longHorizonContextFact(limitsForSwarmChat('a')), /semantic summaries/);
activeGoal = {require_all_participants: true};
assert.equal(limitsForSwarmChat('a').input_characters, 20000);
assert.match(longHorizonContextFact(limitsForSwarmChat('a')), /64 recent team messages.*96,000/);
assert.match(longHorizonContextFact(limitsForSwarmChat('a')), /retrieve the full saved history/);
assert.doesNotMatch(longHorizonContextFact(limitsForSwarmChat('a')), /semantic summaries/);
swarmChatLimits.set('a', {input_characters: 10000});
assert.equal(limitsForSwarmChat('a').input_characters, 10000);
activeGoal = null;
assert.equal(limitsForSwarmChat('b').input_characters, 200000);
"""
        result = subprocess.run([node, "-e", probe], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_archive_reconstruction_preserves_a_chat_append_after_its_initial_read(self):
        original = chat._keep_it
        inserted = False
        def append_before_transform(config, route, turns, filed_as="", **kwargs):
            nonlocal inserted
            if kwargs.get("transform_projection") is not None and not inserted:
                inserted = True
                current = chat.read_it(config, route, filed_as)
                original(config, route, [*current, chat.Said("you", "A concurrent human message", chat._now())], filed_as)
            return original(config, route, turns, filed_as, **kwargs)
        with mock.patch.object(chat, "_keep_it", side_effect=append_before_transform):
            self.project(self.page([self.message(1)]))
        self.assertTrue(inserted)
        self.assertIn("A concurrent human message", [one.text for one in chat.read_it(self.config, "route-blue", self.filed_as)])
        self.assertEqual([one.text for one in self.speech()], ["Public message 1"])

    def test_new_speech_after_resume_stays_after_the_prior_pause(self):
        self.project(self.page([self.message(1)]))
        chat.keep_long_horizon_status(self.config, "route-blue", {
            **self.goal, "event_seq": 12, "status": "paused", "revision": 10,
        }, filed_as=self.filed_as)
        chat.keep_long_horizon_status(self.config, "route-blue", {
            **self.goal, "event_seq": 15, "status": "running", "revision": 11,
        }, filed_as=self.filed_as)
        self.project(self.page([self.message(2)]))
        turns = chat.read_it(self.config, "route-blue", self.filed_as)
        public = [one.text if one.phase == "agent_discussion" else one.correlation.get("goal_status")
                  for one in turns if one.phase in {"agent_discussion", "long_horizon_status"}]
        self.assertEqual(public, ["Public message 1", "paused", "running", "Public message 2"])

    def test_real_store_recovers_all_messages_after_closed_ui_and_event_retirement(self):
        project = self.root / "independent workspace"
        project.mkdir()
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["providers"] = {
            "route-blue": {"kind": "openai", "model": "test", "endpoint": "http://127.0.0.1/blue", "api_key_env": "TEST_BLUE_KEY"},
            "route-orange": {"kind": "anthropic", "model": "test", "endpoint": "http://127.0.0.1/orange", "api_key_env": "TEST_ORANGE_KEY"},
        }
        self.config = LoadedConfig(data, self.root, [], {})
        board = {
            "agents": [{**one, "ready": True} for one in self.goal["agents"]],
            "projects": [{"id": "project-a", "name": "Independent workspace", "path": str(project),
                          "is_there": True, "tasks": []}],
            "works_on": [{"agent": one["id"], "project": "project-a"} for one in self.goal["agents"]],
        }
        runtime_directory = tempfile.TemporaryDirectory()
        self.addCleanup(runtime_directory.cleanup)
        with mock.patch.object(long_horizon, "_base", return_value=Path(runtime_directory.name)):
            store = long_horizon.GoalStore(self.config)
            created = store.create(
                board, "project-a", ["Discuss and create the requested artifact"], "request-a",
                lead_id="builder", participant_ids=["builder", "checker"], conversation_id="chat-a",
            )
            words = ["🙂" * 8000, *[f"Complete public message {number}" for number in range(2, 206)]]
            user_messages = ["Checker, inspect keyboard controls.", "Use arrow keys for the decision."]
            def publish(document, db):
                for index, text in enumerate(words):
                    task = document["tasks"][index % 2]
                    task["provider_effect_id"] = f"independent-effect-{index}"
                    store._record_dialogue_message(db, document, task, {"action": "work", "summary": text})
                checker = next(one for one in document["tasks"] if one["assigned_agent_id"] == "checker")
                store._event(db, document, "agent_messaged", task_id=checker["id"], agent_id="checker",
                             payload={"text": user_messages[0]})
                store._event(db, document, "interrupt_resolved", task_id=checker["id"], agent_id="checker",
                             payload={"answer": user_messages[1]})
                # No UI has polled. More than the real journal limit of
                # operational events retire every provider acknowledgement.
                for index in range(long_horizon.MAX_EVENTS + 1):
                    store._event(db, document, "inspection_progress", payload={"step": index})
                document["status"] = "paused"
                document["note"] = "Fixture stopped after storing the public conversation."
            store._mutate(created["goal_id"], publish)
            current = store.public(store.get(created["goal_id"]))
            self.assertLess(len(current["dialogue"]["messages"]), len(words))
            self.assertFalse(any(one["type"] == "provider_acknowledged"
                                 for one in store.events(created["goal_id"], limit=4000)["events"]))
            state = self.server(lambda *args, **kwargs: None)
            state.long_horizon.store = store
            HarnessHTTPServer.project_long_horizon_chat_statuses(state, [current])
            self.assertEqual([one.text for one in self.speech()], [*words, *user_messages])
            self.assertTrue(all(one.who == "you" and one.recipient_name == "Checker"
                                for one in self.speech()[-2:]))
            reopened = long_horizon.GoalStore(LoadedConfig(copy.deepcopy(data), self.root, [], {}))
            state.long_horizon.store = reopened
            HarnessHTTPServer.project_long_horizon_chat_statuses(state, [reopened.public(reopened.get(created["goal_id"]))])
            self.assertEqual([one.text for one in self.speech()], [*words, *user_messages])

    def test_real_legacy_store_maps_snapshot_only_repeats_to_existing_chat_events(self):
        project = self.root / "legacy independent workspace"
        project.mkdir()
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["providers"] = {
            "route-blue": {"kind": "openai", "model": "test", "endpoint": "http://127.0.0.1/blue", "api_key_env": "TEST_BLUE_KEY"},
            "route-orange": {"kind": "anthropic", "model": "test", "endpoint": "http://127.0.0.1/orange", "api_key_env": "TEST_ORANGE_KEY"},
        }
        self.config = LoadedConfig(data, self.root, [], {})
        board = {"agents": [{**one, "ready": True} for one in self.goal["agents"]],
                 "projects": [{"id": "project-a", "name": "Legacy fixture", "path": str(project), "is_there": True, "tasks": []}],
                 "works_on": [{"agent": one["id"], "project": "project-a"} for one in self.goal["agents"]]}
        runtime_directory = tempfile.TemporaryDirectory()
        self.addCleanup(runtime_directory.cleanup)
        with mock.patch.object(long_horizon, "_base", return_value=Path(runtime_directory.name)):
            store = long_horizon.GoalStore(self.config)
            created = store.create(board, "project-a", ["Discuss and create the requested artifact"], "request-a",
                                   lead_id="builder", participant_ids=["builder", "checker"], conversation_id="chat-a")
            words = ["Repeated exact reply", "The middle reply", "Repeated exact reply"]
            def publish(document, db):
                for index, text in enumerate(words):
                    task = document["tasks"][index % 2]
                    task["provider_effect_id"] = f"legacy-effect-{index}"
                    store._record_dialogue_message(db, document, task, {"action": "work", "summary": text})
            store._mutate(created["goal_id"], publish)
            before = store.public(store.get(created["goal_id"]))
            # The old renderer saved every real message before the events
            # retired; it had no archive message IDs or archive cursor.
            chat.keep_long_horizon_events(self.config, "route-blue", before,
                                          store.events(created["goal_id"])["events"], filed_as=self.filed_as)
            self.assertEqual([one.text for one in chat.read_it(self.config, "route-blue", self.filed_as)
                              if one.phase == "agent_discussion"], words)
            def old_version_state(document, db):
                for index in range(long_horizon.MAX_EVENTS + 1):
                    store._event(db, document, "inspection_progress", payload={"step": index})
                for message in document["dialogue"]["messages"]:
                    for key in ("source_goal_event_id", "source_goal_event_seq", "source_goal_event_type"):
                        message.pop(key, None)
                db.execute("DELETE FROM long_goal_dialogue_messages WHERE goal_id=?", (document["goal_id"],))
                document.pop("dialogue_archive")
                document["status"] = "paused"
            store._mutate(created["goal_id"], old_version_state)
            migrated = store.get(created["goal_id"])
            coverage = migrated["dialogue_archive"]["coverage"]
            self.assertEqual(coverage["legacy_dialogue_sequence"], 3)
            state = self.server(lambda *args, **kwargs: None)
            state.long_horizon.store = store
            for _ in range(2):
                HarnessHTTPServer.project_long_horizon_chat_statuses(state, [store.public(migrated)])
            turns = chat.read_it(self.config, "route-blue", self.filed_as)
            self.assertEqual([one.text for one in turns if one.phase == "agent_discussion"], words)
            self.assertFalse(any(one.phase == "recovered_history" for one in turns))
            reopened = long_horizon.GoalStore(LoadedConfig(copy.deepcopy(data), self.root, [], {}))
            state.long_horizon.store = reopened
            HarnessHTTPServer.project_long_horizon_chat_statuses(state, [reopened.public(reopened.get(created["goal_id"]))])
            self.assertEqual([one.text for one in self.speech()], words)


if __name__ == "__main__":
    unittest.main()
