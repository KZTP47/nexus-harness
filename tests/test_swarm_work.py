from __future__ import annotations

import base64
import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from our_harness import chat, swarm_work
from our_harness import cancellation
from our_harness.changes import FileTransaction, file_sha256
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import ChangePlan, ProviderRequest, ProviderResponse
from our_harness.collaboration_ledger import CollaborationLedger, _event_hash
from our_harness.providers.base import AnthropicProvider, OllamaProvider, OpenAIProvider
from our_harness.providers.gemini import GeminiProvider


class SwarmWorkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.project = self.root / "project"
        self.project.mkdir()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.config.data["providers"] = {
            "claude": {"kind": "claude-cli", "model": "claude"},
            "codex": {"kind": "codex-cli", "model": "codex"},
        }
        self.board = {
            "agents": [
                {"id": "agent-1", "name": "Claude", "who": "claude", "job": "lead", "ready": True},
                {"id": "agent-2", "name": "Codex", "who": "codex", "job": "review", "ready": True},
            ],
            "projects": [
                {"id": "project-1", "name": "Demo", "path": str(self.project), "tasks": []},
            ],
            "works_on": [
                {"agent": "agent-1", "project": "project-1"},
                {"agent": "agent-2", "project": "project-1"},
            ],
            "talks_to": [{"one": "agent-1", "other": "agent-2"}],
        }

    def test_board_context_names_the_real_connected_agent_without_claiming_a_relay(self) -> None:
        context = swarm_work.board_context(self.board, "agent-1")
        self.assertIn("Codex (route codex)", context)
        self.assertIn("No relay has happened", context)

    def test_publicly_rehashed_collaboration_rewrite_fails_keyed_authorship(self) -> None:
        ledger = CollaborationLedger(
            self.config, "claude", "keyed-ledger", session_id="keyed-session"
        ).begin(
            "Keep the private evidence intact",
            self.board["agents"],
            mode="collaborate",
        )
        ledger.record_contribution({
            "speaker_id": "agent-1", "recipient_id": "agent-2",
            "recipient_name": "Codex", "text": "original private evidence",
            "phase": "agent_discussion",
        })
        records = [
            json.loads(line)
            for line in ledger.paths.jsonl.read_text(encoding="utf-8").splitlines()
        ]
        records[-1]["text"] = "forged private evidence"
        previous = ""
        for event in records:
            event["previous_hash"] = previous
            event["hash"] = _event_hash(event)
            previous = event["hash"]
        rewritten = "".join(
            json.dumps(one, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for one in records
        )
        ledger.paths.jsonl.write_text(rewritten, encoding="utf-8")

        reopened = CollaborationLedger(
            self.config, "claude", "keyed-ledger", session_id="keyed-session"
        )
        with self.assertRaisesRegex(Exception, "failed keyed integrity"):
            reopened.projection_for("agent-2")
        self.assertEqual(ledger.paths.jsonl.read_text(encoding="utf-8"), rewritten)

    def test_collaboration_relays_the_real_peer_answer_to_the_lead(self) -> None:
        contexts: list[tuple[str, str]] = []

        def answer(_config, route, _text, **kwargs):
            contexts.append((route, kwargs.get("context", "")))
            if kwargs.get("response_format") is swarm_work.DISCUSSION_FORMAT:
                return {"text": json.dumps({
                    "message": f"discussion from {route}", "goal_complete": True, "remaining": [],
                }), "milliseconds": 1, "model": route}
            return {"text": f"answer from {route}", "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            result = swarm_work.collaborate(
                self.config, self.board, "agent-1", "Solve this together"
            )
        final_context = contexts[-1][1]
        self.assertIn("FULL ACTUAL TEAM CONVERSATION", final_context)
        self.assertIn("discussion from codex", final_context)
        self.assertEqual(result["collaborated_with"][0]["name"], "Codex")
        transcript = chat.read_it(self.config, "claude", "Claude")
        self.assertEqual([one.speaker_name for one in transcript], [
            "You", "Claude", "Codex", "Claude", "Codex", "Claude",
        ])
        self.assertEqual([one.phase for one in transcript], [
            "user_prompt", "lead_draft", "agent_reply",
            "agent_discussion", "agent_discussion", "final_answer",
        ])
        self.assertEqual(transcript[2].text, "answer from codex")
        self.assertEqual(transcript[2].recipient_name, "Claude")
        self.assertEqual(transcript[2].speaker_route, "codex")
        self.assertEqual(transcript[-1].text, "answer from claude")

    def test_collaboration_reports_real_relay_stages(self) -> None:
        stages: list[tuple[str, str]] = []

        def answer(_config, route, _text, **_kwargs):
            if _kwargs.get("response_format") is swarm_work.DISCUSSION_FORMAT:
                return {"text": json.dumps({
                    "message": f"discussion from {route}", "goal_complete": True, "remaining": [],
                }), "milliseconds": 1, "model": route}
            return {"text": f"answer from {route}", "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            swarm_work.collaborate(
                self.config, self.board, "agent-1", "Solve this together",
                progress=lambda stage, detail: stages.append((stage, detail)),
            )
        words = "\n".join(stage for stage, _detail in stages)
        self.assertIn("Contacting 2 agents in parallel", words)
        self.assertIn("Starting goal-directed team discussion", words)
        self.assertIn("Team discussion round 1", words)
        self.assertIn("Waiting for Claude to report the outcome", words)

    def test_collaboration_publishes_each_completed_reply_before_the_final_answer(self) -> None:
        turns: list[dict] = []

        def answer(_config, route, _text, **kwargs):
            if kwargs.get("response_format") is swarm_work.DISCUSSION_FORMAT:
                return {"text": json.dumps({
                    "message": f"discussion from {route}", "goal_complete": True, "remaining": [],
                }), "milliseconds": 1, "model": route}
            if "FULL ACTUAL TEAM CONVERSATION" not in kwargs.get("context", ""):
                time.sleep(0.01 if route == "codex" else 0.04)
            return {"text": f"answer from {route}", "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            swarm_work.collaborate(
                self.config, self.board, "agent-1", "Solve this together",
                live_turn=turns.append,
            )
        self.assertEqual([one["speaker_name"] for one in turns[:2]], ["Codex", "Claude"])
        self.assertEqual([one["speaker_name"] for one in turns[2:]], ["Claude", "Codex"])
        self.assertEqual(turns[0]["phase"], "agent_reply")
        self.assertEqual(turns[0]["recipient_name"], "Claude")

    def test_collaboration_continues_until_every_agent_marks_the_goal_complete(self) -> None:
        discussion_calls = 0

        def answer(_config, route, _text, **kwargs):
            nonlocal discussion_calls
            if kwargs.get("response_format") is swarm_work.DISCUSSION_FORMAT:
                discussion_calls += 1
                finished = discussion_calls > 2
                value = {
                    "message": f"round message {discussion_calls} from {route}",
                    "goal_complete": finished,
                    "remaining": [] if finished else ["Continue checking the result."],
                }
                return {"text": json.dumps(value), "milliseconds": 1, "model": route}
            return {"text": f"answer from {route}", "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            result = swarm_work.collaborate(
                self.config, self.board, "agent-1", "Solve this together"
            )
        self.assertTrue(result["goal_complete"])
        self.assertEqual(result["discussion_rounds"], 2)
        transcript = chat.read_it(self.config, "claude", "Claude")
        discussions = [one for one in transcript if one.phase == "agent_discussion"]
        self.assertEqual(len(discussions), 4)
        self.assertEqual([one.speaker_name for one in discussions], [
            "Claude", "Codex", "Claude", "Codex",
        ])

    def test_unlimited_collaboration_stops_reworded_no_progress_cycles(self) -> None:
        discussion_calls = 0
        remaining = [
            "Await the missing provider reply from the connected peer.",
            "Still await the missing connected provider reply from that peer.",
            "The connected peer's missing provider reply is still awaited.",
        ]

        def answer(_config, route, _text, **kwargs):
            nonlocal discussion_calls
            if kwargs.get("response_format") is swarm_work.DISCUSSION_FORMAT:
                cycle = discussion_calls // 2
                discussion_calls += 1
                value = {
                    "message": f"Cosmetically different wording {discussion_calls} from {route}.",
                    "goal_complete": False,
                    "remaining": [remaining[min(cycle, len(remaining) - 1)]],
                }
                return {"text": json.dumps(value), "milliseconds": 1, "model": route}
            return {"text": f"answer from {route}", "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            result = swarm_work.collaborate(
                self.config, self.board, "agent-1", "Solve this together",
                round_limit=None,
            )

        self.assertFalse(result["goal_complete"])
        self.assertEqual(result["discussion_rounds"], 3)
        self.assertEqual(result["stopped_because"], "stalled")
        self.assertIsNone(result["round_limit"])
        self.assertTrue(any("no-progress cycle" in one for one in result["remaining"]))

    def test_incident_rewording_cannot_hide_the_same_file_capability_blocker(self) -> None:
        discussion_calls = 0
        rounds = [
            [
                [
                    "GPT Codex to create `arrayStats.js` via Nexus Work together action.",
                    "Verify `arrayStats([1,2,3,4,5])` returns `{ sum: 15, average: 3, count: 5 }`.",
                    "Verify `arrayStats([])` returns `{ sum: 0, average: 0, count: 0 }`.",
                    "Verify `arrayStats([10])` returns `{ sum: 10, average: 10, count: 1 }`.",
                ],
                [
                    "Nexus Work together must apply creation of `arrayStats.js` in the active project root.",
                    "Verify `arrayStats([1,2,3,4,5])` returns `{ sum: 15, average: 3, count: 5 }`.",
                    "Verify `arrayStats([])` returns `{ sum: 0, average: 0, count: 0 }`.",
                    "Verify `arrayStats([10])` returns `{ sum: 10, average: 10, count: 1 }`.",
                ],
            ],
            [
                [
                    "Nexus Work together action to create `arrayStats.js` in project root with GPT Codex's prepared implementation.",
                    "Verify `arrayStats([1,2,3,4,5])` returns `{ sum: 15, average: 3, count: 5 }`.",
                    "Verify `arrayStats([])` returns `{ sum: 0, average: 0, count: 0 }`.",
                    "Verify `arrayStats([10])` returns `{ sum: 10, average: 10, count: 1 }`.",
                ],
                [
                    "Nexus Work together action to create `arrayStats.js` in the active project root with the prepared implementation.",
                    "Verify `arrayStats([1,2,3,4,5])` returns `{ sum: 15, average: 3, count: 5 }`.",
                    "Verify `arrayStats([])` returns `{ sum: 0, average: 0, count: 0 }`.",
                    "Verify `arrayStats([10])` returns `{ sum: 10, average: 10, count: 1 }`.",
                ],
            ],
            [
                [
                    "Nexus Work together action needed to allow GPT Codex to write `arrayStats.js` to project root.",
                    "Verify arrayStats([1,2,3,4,5]) returns correct values after file creation.",
                    "Verify arrayStats([]) returns all zeros.",
                    "Verify arrayStats([10]) returns correct values.",
                ],
                [
                    "Nexus Work together action to create `arrayStats.js` in the active project root with GPT Codex's prepared implementation.",
                    "Verify `arrayStats([1,2,3,4,5])` returns `{ sum: 15, average: 3, count: 5 }`.",
                    "Verify `arrayStats([])` returns `{ sum: 0, average: 0, count: 0 }`.",
                    "Verify `arrayStats([10])` returns `{ sum: 10, average: 10, count: 1 }`.",
                ],
            ],
        ]

        def answer(_config, route, _text, **kwargs):
            nonlocal discussion_calls
            if kwargs.get("response_format") is swarm_work.DISCUSSION_FORMAT:
                round_index, agent_index = divmod(discussion_calls, 2)
                discussion_calls += 1
                value = {
                    "message": f"The unchanged file-write blocker was reworded by {route}.",
                    "goal_complete": False,
                    "remaining": rounds[min(round_index, len(rounds) - 1)][agent_index],
                }
                return {"text": json.dumps(value), "milliseconds": 1, "model": route}
            return {"text": f"answer from {route}", "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            result = swarm_work.collaborate(
                self.config, self.board, "agent-1",
                "Claude, delegate a small verifiable task to GPT Codex.",
                round_limit=None,
            )

        self.assertEqual(result["discussion_rounds"], 3)
        self.assertEqual(result["stopped_because"], "stalled")
        self.assertEqual(discussion_calls, 6)

    def test_ask_once_preserves_user_cancellation_for_the_collaboration_engine(self) -> None:
        class StoppedProvider:
            def complete(self, _request):
                raise cancellation.ChatCancelled(cancellation.STOPPED_MESSAGE)

        with mock.patch.object(chat, "create_provider", return_value=StoppedProvider()):
            with self.assertRaisesRegex(cancellation.ChatCancelled, "Stopped by you"):
                chat.ask_once(self.config, "claude", "continue")

    def test_structured_provider_failure_pauses_without_becoming_agent_speech(self) -> None:
        discussion_calls = 0

        def answer(_config, route, _text, **kwargs):
            nonlocal discussion_calls
            if kwargs.get("response_format") is swarm_work.DISCUSSION_FORMAT:
                discussion_calls += 1
                if route == "claude":
                    return {
                        "text": "I cannot return the Nexus structure; ask me directly.",
                        "milliseconds": 1, "model": route,
                    }
                value = {
                    "message": "The connected provider still has not supplied a usable turn.",
                    "goal_complete": False,
                    "remaining": ["Obtain one valid structured reply from the connected provider."],
                }
                return {"text": json.dumps(value), "milliseconds": 1, "model": route}
            return {"text": f"initial or final answer from {route}", "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            with self.assertRaisesRegex(
                swarm_work.SwarmError, "paused this collaboration"
            ):
                swarm_work.collaborate(
                    self.config, self.board, "agent-1", "Are you ChatGPT or Gemini?",
                    round_limit=None,
                )

        self.assertEqual(discussion_calls, 1)
        ledger = next((self.root / ".harness" / "chats").glob("*.collaboration.md"))
        saved = ledger.read_text(encoding="utf-8")
        self.assertIn("provider_transport_failure", saved)
        self.assertNotIn("This agent could not continue", saved)

    def test_initial_provider_failure_does_not_start_a_reasoning_round(self) -> None:
        calls: list[tuple[str, object]] = []

        def answer(_config, route, _text, **kwargs):
            calls.append((route, kwargs.get("response_format")))
            if route == "codex":
                raise swarm_work.HarnessError("the web submit control rejected the turn")
            return {"text": "lead draft", "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            with self.assertRaisesRegex(
                swarm_work.SwarmError, "not counted as an agent reply or a discussion round"
            ):
                swarm_work.collaborate(
                    self.config, self.board, "agent-1", "Investigate together",
                    round_limit=None,
                )

        self.assertEqual(len(calls), 2)
        self.assertTrue(all(response_format is None for _route, response_format in calls))
        ledger = next((self.root / ".harness" / "chats").glob("*.collaboration.md"))
        saved = ledger.read_text(encoding="utf-8")
        self.assertIn("provider_unavailable", saved)
        self.assertNotIn("the web submit control rejected the turn", saved)

    def test_unlimited_collaboration_stops_a_two_state_oscillation(self) -> None:
        discussion_calls = 0

        def answer(_config, route, _text, **kwargs):
            nonlocal discussion_calls
            if kwargs.get("response_format") is swarm_work.DISCUSSION_FORMAT:
                cycle = discussion_calls // 2
                discussion_calls += 1
                blocker = "Choose design alpha." if cycle % 2 == 0 else "Choose design beta."
                value = {
                    "message": f"Switched position again on {route}.",
                    "goal_complete": False, "remaining": [blocker],
                }
                return {"text": json.dumps(value), "milliseconds": 1, "model": route}
            return {"text": f"answer from {route}", "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            result = swarm_work.collaborate(
                self.config, self.board, "agent-1", "Agree on one design",
                round_limit=None,
            )

        self.assertFalse(result["goal_complete"])
        self.assertEqual(result["discussion_rounds"], 3)
        self.assertEqual(result["stopped_because"], "stalled")

    def test_unlimited_collaboration_can_exceed_the_old_twelve_round_ceiling(self) -> None:
        discussion_calls = 0

        def answer(_config, route, _text, **kwargs):
            nonlocal discussion_calls
            if kwargs.get("response_format") is swarm_work.DISCUSSION_FORMAT:
                cycle = discussion_calls // 2 + 1
                discussion_calls += 1
                finished = cycle >= 14
                value = {
                    "message": f"Completed distinct checkpoint-{cycle} on {route}.",
                    "goal_complete": finished,
                    "remaining": [] if finished else [
                        f"Produce distinct checkpoint-{cycle + 1}."
                    ],
                    "progress": [{
                        "id": "checkpoint",
                        "state": str(cycle),
                        "evidence": f"checkpoint-{cycle} completed",
                    }],
                }
                return {"text": json.dumps(value), "milliseconds": 1, "model": route}
            return {"text": f"answer from {route}", "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            result = swarm_work.collaborate(
                self.config, self.board, "agent-1", "Complete fourteen checkpoints",
                round_limit=None,
            )

        self.assertTrue(result["goal_complete"])
        self.assertEqual(result["discussion_rounds"], 14)
        self.assertEqual(result["stopped_because"], "complete")
        self.assertEqual(discussion_calls, 28)

    def test_user_round_limit_stops_at_the_exact_selected_round(self) -> None:
        discussion_calls = 0

        def answer(_config, route, _text, **kwargs):
            nonlocal discussion_calls
            if kwargs.get("response_format") is swarm_work.DISCUSSION_FORMAT:
                discussion_calls += 1
                value = {
                    "message": f"New work {discussion_calls} from {route}.",
                    "goal_complete": False,
                    "remaining": [f"Continue with unique item {discussion_calls}."],
                }
                return {"text": json.dumps(value), "milliseconds": 1, "model": route}
            return {"text": f"answer from {route}", "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            result = swarm_work.collaborate(
                self.config, self.board, "agent-1", "Keep investigating",
                round_limit=2,
            )

        self.assertFalse(result["goal_complete"])
        self.assertEqual(result["discussion_rounds"], 2)
        self.assertEqual(result["round_limit"], 2)
        self.assertEqual(result["stopped_because"], "round_limit")
        self.assertEqual(discussion_calls, 4)
        self.assertTrue(any("user-set limit of 2" in one for one in result["remaining"]))

    def test_user_round_limit_is_strict_and_unlimited_is_explicit(self) -> None:
        self.assertIsNone(swarm_work.user_round_limit(None))
        self.assertEqual(swarm_work.user_round_limit(37), 37)
        for invalid in (True, 0, -1, 10_001, "12", 1.5):
            with self.subTest(invalid=invalid), self.assertRaises(Exception):
                swarm_work.user_round_limit(invalid)

    def test_direct_follow_up_keeps_the_group_transcript_without_impersonating_peers(self) -> None:
        def answer(_config, route, _text, **_kwargs):
            if _kwargs.get("response_format") is swarm_work.DISCUSSION_FORMAT:
                return {"text": json.dumps({
                    "message": f"discussion from {route}", "goal_complete": True, "remaining": [],
                }), "milliseconds": 1, "model": route}
            return {"text": f"answer from {route}", "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            swarm_work.collaborate(
                self.config, self.board, "agent-1", "Solve this together"
            )

        seen: list[dict] = []

        class Provider:
            def complete(self, request):
                seen.extend(request.messages)
                return ProviderResponse(text="the follow-up answer")

        with mock.patch.object(chat, "create_provider", return_value=Provider()):
            chat.say(self.config, "claude", "What about the follow-up?", filed_as="Claude")

        self.assertEqual([one["role"] for one in seen], ["user", "assistant", "user"])
        contents = [one["content"] for one in seen]
        self.assertNotIn("answer from codex", contents)
        self.assertEqual(contents.count("answer from claude"), 1)

    def test_clear_team_request_is_automatically_routed_without_a_classifier_call(self) -> None:
        with mock.patch.object(chat, "ask_once") as ask:
            decision = swarm_work.automatic_mode(
                self.config, self.board, "agent-1",
                "Please work together and compare your answers",
            )
        self.assertEqual(decision["mode"], "collaborate")
        ask.assert_not_called()

    def test_named_message_request_uses_a_sequential_directed_relay(self) -> None:
        decision = swarm_work.automatic_mode(
            self.config, self.board, "agent-1",
            "Are you Claude? Send a message to Codex.",
        )
        self.assertEqual(decision["mode"], "relay")

    def test_selected_pair_chat_relays_casual_messages_without_keywords(self) -> None:
        with mock.patch.object(chat, "ask_once") as ask:
            decision = swarm_work.automatic_mode(
                self.config, self.board, "agent-1", "Can you tell them hi?",
                peer_id="agent-2",
            )
        self.assertEqual(decision["mode"], "collaborate")
        self.assertIn("selected pair chat", decision["reason"])
        ask.assert_not_called()

    def test_directed_relay_addresses_the_user_to_the_lead_and_the_relay_to_the_peer(self) -> None:
        calls: list[tuple[str, str, str]] = []

        def answer(_config, route, text, **kwargs):
            context = kwargs.get("context", "")
            calls.append((route, text, context))
            if "DIRECTED RELAY — FINAL USER REPORT" in context:
                value = "I am Claude. Codex replied: relay received."
            elif route == "claude":
                value = "Codex, please confirm this relay."
            else:
                value = "Claude, relay received."
            return {"text": value, "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            result = swarm_work.relay(
                self.config, self.board, "agent-1",
                "Are you Claude? Send a message to Codex.",
            )

        self.assertEqual([one[0] for one in calls], ["claude", "codex", "claude"])
        self.assertEqual(calls[1][1], "Message relayed by Nexus from Claude:\nCodex, please confirm this relay.")
        self.assertNotEqual(calls[2][1], "Are you Claude? Send a message to Codex.")
        self.assertIn("NEXUS CURRENT TURN", calls[2][1])
        self.assertIn("Are you Claude? Send a message to Codex.", calls[2][2])
        self.assertIn("not to Codex", calls[0][2])
        self.assertIn("Reply to Claude", calls[1][2])
        self.assertTrue(result["relay_complete"])
        transcript = chat.read_it(self.config, "claude", "Claude")
        self.assertEqual([one.phase for one in transcript], [
            "user_prompt", "lead_draft", "agent_reply", "final_answer",
        ])
        self.assertEqual(transcript[2].speaker_name, "Codex")
        self.assertEqual(transcript[2].recipient_name, "Claude")

    def test_implicit_team_request_is_routed_locally_without_a_provider_control_turn(self) -> None:
        with mock.patch.object(chat, "ask_once") as ask:
            decision = swarm_work.automatic_mode(
                self.config, self.board, "agent-1",
                "Assess the design from implementation and review perspectives.",
            )
        self.assertEqual(decision["mode"], "collaborate")
        ask.assert_not_called()

    def test_web_chat_greeting_never_receives_an_internal_json_routing_turn(self) -> None:
        board = copy.deepcopy(self.board)
        board["agents"][0]["who"] = "web:gemini-example"
        with mock.patch.object(chat, "ask_once") as ask:
            decision = swarm_work.automatic_mode(
                self.config, board, "agent-1", "Gemini, is this you?",
            )
        self.assertEqual(decision["mode"], "chat")
        ask.assert_not_called()

    def test_explicit_file_request_is_automatically_routed_to_confirmed_work(self) -> None:
        with mock.patch.object(chat, "ask_once") as ask:
            decision = swarm_work.automatic_mode(
                self.config, self.board, "agent-1", "Create a file for this"
            )
        self.assertEqual(decision["mode"], "work")
        ask.assert_not_called()

    def test_automatic_routing_without_a_ready_peer_stays_direct(self) -> None:
        board = copy.deepcopy(self.board)
        board["agents"][1]["ready"] = False
        with mock.patch.object(chat, "ask_once") as ask:
            decision = swarm_work.automatic_mode(
                self.config, board, "agent-1", "Get another opinion"
            )
        self.assertEqual(decision["mode"], "chat")
        ask.assert_not_called()

    def test_project_work_applies_a_baseline_checked_transaction(self) -> None:
        def answer(_config, route, _text, **kwargs):
            if kwargs.get("response_format") is swarm_work.PLAN_FORMAT:
                value = {"contribution": f"plan by {route}", "message_to_lead": "make it", "needs_files": []}
            elif kwargs.get("response_format") is swarm_work.PLAN_REVIEW_FORMAT:
                value = {
                    "contribution": f"reviewed plan by {route}", "message_to_lead": "ready",
                    "needs_files": [], "ready_to_execute": True, "remaining": [],
                }
            elif kwargs.get("response_format") is swarm_work.WORK_VERIFICATION_FORMAT:
                value = {"goal_complete": True, "feedback": "The marker exists.", "remaining": []}
            else:
                value = {
                    "reply": "The team made the requested marker.",
                    "changes": [{"path": "made-by-team.txt", "content": "claude + codex\n", "reason": "requested"}],
                }
            return {"text": json.dumps(value), "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            result = swarm_work.work_together(
                self.config, self.board, "agent-1", "Create a marker file"
            )
        self.assertEqual((self.project / "made-by-team.txt").read_text(), "claude + codex\n")
        self.assertEqual(result["changed"], ["made-by-team.txt"])
        self.assertTrue(result["transaction_id"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["verification_status"], "provider_consensus_unverified")
        transcript = chat.read_it(self.config, "claude", "Claude")
        self.assertEqual([one.phase for one in transcript], [
            "user_prompt", "lead_plan", "agent_plan",
            "agent_plan_review", "agent_plan_review", "lead_execution",
            "agent_execution",
            "agent_verification", "agent_verification", "final_answer",
        ])
        self.assertIn("plan by codex", transcript[2].text)
        self.assertIn("made-by-team.txt", transcript[-1].text)
        self.assertIn("not independently verified", transcript[-1].text)

    def test_incomplete_run_rolls_back_all_applied_transactions(self) -> None:
        target = self.project / "existing.txt"
        target.write_text("original\n", encoding="utf-8")

        def answer(_config, route, _text, **kwargs):
            response_format = kwargs.get("response_format")
            if response_format is swarm_work.PLAN_FORMAT:
                value = {"contribution": "edit", "message_to_lead": "edit", "needs_files": []}
            elif response_format is swarm_work.PLAN_REVIEW_FORMAT:
                value = {
                    "contribution": "edit", "message_to_lead": "ready",
                    "needs_files": [], "ready_to_execute": True, "remaining": [],
                }
            elif response_format is swarm_work.WORK_VERIFICATION_FORMAT:
                value = {
                    "goal_complete": False, "feedback": "still incomplete",
                    "remaining": ["A deterministic acceptance check is missing."],
                }
            else:
                value = {
                    "reply": "edited", "changes": [{
                        "path": "existing.txt", "content": f"changed by {route}\n",
                        "reason": "requested",
                    }],
                }
            return {"text": json.dumps(value), "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            result = swarm_work.work_together(
                self.config, self.board, "agent-1", "Edit existing.txt", round_limit=1,
            )
        self.assertEqual(target.read_text(encoding="utf-8"), "original\n")
        self.assertEqual(result["changed"], [])
        self.assertEqual(result["mutation_recovery"]["status"], "rolled_back")
        self.assertFalse(result["goal_complete"])

    def test_rollback_conflict_preserves_external_content_and_reports_uncertainty(self) -> None:
        target = self.project / "conflict.txt"
        target.write_text("before\n", encoding="utf-8")
        manifest = FileTransaction(self.project).apply([ChangePlan(
            "conflict.txt", file_sha256(target), "after\n", reason="test"
        )])
        target.write_text("external\n", encoding="utf-8")
        result = swarm_work._rollback_transactions(
            self.project, [str(manifest["transaction_id"])]
        )
        self.assertEqual(result["status"], "rollback_conflict")
        self.assertEqual(target.read_text(encoding="utf-8"), "external\n")

    def test_cancellation_after_a_partial_execution_rolls_back_before_propagating(self) -> None:
        work_calls = 0

        def answer(_config, route, _text, **kwargs):
            nonlocal work_calls
            response_format = kwargs.get("response_format")
            if response_format is swarm_work.PLAN_FORMAT:
                value = {"contribution": "edit", "message_to_lead": "edit", "needs_files": []}
            elif response_format is swarm_work.PLAN_REVIEW_FORMAT:
                value = {
                    "contribution": "edit", "message_to_lead": "ready",
                    "needs_files": [], "ready_to_execute": True, "remaining": [],
                }
            elif response_format is swarm_work.WORK_FORMAT:
                work_calls += 1
                if work_calls == 2:
                    raise cancellation.ChatCancelled(cancellation.STOPPED_MESSAGE)
                value = {
                    "reply": "partial", "changes": [{
                        "path": "partial.txt", "content": "partial\n", "reason": "requested",
                    }],
                }
            else:
                raise AssertionError(f"unexpected response format for {route}")
            return {"text": json.dumps(value), "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            with self.assertRaises(cancellation.ChatCancelled):
                swarm_work.work_together(
                    self.config, self.board, "agent-1", "Create partial.txt"
                )
        self.assertFalse((self.project / "partial.txt").exists())

    def test_crashed_process_saga_is_compensated_before_the_next_run(self) -> None:
        target = self.project / "crash.txt"
        target.write_text("before\n", encoding="utf-8")
        script = r'''import os, sys
from pathlib import Path
from our_harness.changes import FileTransaction, file_sha256
from our_harness.models import ChangePlan
from our_harness.swarm_work import _MutationSaga
root = Path(sys.argv[1]).resolve(); target = root / "crash.txt"
saga = _MutationSaga(root, "crash-injection")
txid = FileTransaction.new_transaction_id(); saga.prepare(txid)
FileTransaction(root).apply([ChangePlan("crash.txt", file_sha256(target), "after\n", reason="crash test")], transaction_id=txid)
saga.applied(txid)
os._exit(23)
'''
        env = dict(os.environ, PYTHONPATH=str(Path.cwd() / "src"))
        process = subprocess.run(
            [sys.executable, "-c", script, str(self.project)], env=env, check=False,
        )
        self.assertEqual(process.returncode, 23)
        self.assertEqual(target.read_text(encoding="utf-8"), "after\n")
        recovered = swarm_work._MutationSaga.recover_orphans(self.project)
        self.assertEqual(recovered[0]["status"], "rolled_back")
        self.assertEqual(target.read_text(encoding="utf-8"), "before\n")
        journal = json.loads(next(
            (self.project / ".harness" / "swarm-mutation-sagas").glob("*.json")
        ).read_text(encoding="utf-8"))
        self.assertEqual(journal["phase"], "compensated")

    def test_saga_conflict_is_durable_and_blocks_later_mutation_recovery(self) -> None:
        target = self.project / "saga-conflict.txt"
        target.write_text("before\n", encoding="utf-8")
        saga = swarm_work._MutationSaga(self.project, "durable-conflict")
        transaction_id = FileTransaction.new_transaction_id()
        saga.prepare(transaction_id)
        FileTransaction(self.project).apply([ChangePlan(
            "saga-conflict.txt", file_sha256(target), "after\n", reason="test"
        )], transaction_id=transaction_id)
        saga.applied(transaction_id)
        target.write_text("external\n", encoding="utf-8")
        result = saga.compensate("test_conflict")
        self.assertEqual(result["status"], "rollback_conflict")
        self.assertEqual(target.read_text(encoding="utf-8"), "external\n")
        blocked = swarm_work._MutationSaga.recover_orphans(self.project)
        self.assertEqual(blocked[0]["status"], "rollback_conflict")

    def test_project_work_reports_planning_validation_and_application(self) -> None:
        stages: list[str] = []
        plan_review_contexts: list[str] = []

        def answer(_config, route, _text, **kwargs):
            if kwargs.get("response_format") is swarm_work.PLAN_FORMAT:
                value = {"contribution": f"plan by {route}", "message_to_lead": "make it", "needs_files": []}
            elif kwargs.get("response_format") is swarm_work.PLAN_REVIEW_FORMAT:
                plan_review_contexts.append(kwargs.get("context", ""))
                value = {"contribution": f"plan by {route}", "message_to_lead": "ready", "needs_files": [], "ready_to_execute": True, "remaining": []}
            elif kwargs.get("response_format") is swarm_work.WORK_VERIFICATION_FORMAT:
                value = {"goal_complete": True, "feedback": "Done.", "remaining": []}
            else:
                value = {"reply": "Done.", "changes": []}
            return {"text": json.dumps(value), "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            swarm_work.work_together(
                self.config, self.board, "agent-1", "Check the project",
                progress=lambda stage, _detail: stages.append(stage),
            )
        self.assertIn("Collecting project plans from 2 agents", stages)
        self.assertIn("Reading the requested project files", stages)
        self.assertIn("Team plan review round 1", stages)
        self.assertIn("Project execution pass 1", stages)
        self.assertIn("Team verification pass 1", stages)
        self.assertTrue(plan_review_contexts)
        self.assertIn("does not mean the files already exist", plan_review_contexts[0])
        self.assertIn("remaining to an empty list", plan_review_contexts[0])

    def test_project_work_revises_files_after_agents_find_remaining_work(self) -> None:
        work_calls = 0
        verification_calls = 0

        def answer(_config, route, _text, **kwargs):
            nonlocal work_calls, verification_calls
            response_format = kwargs.get("response_format")
            if response_format is swarm_work.PLAN_FORMAT:
                value = {"contribution": f"plan by {route}", "message_to_lead": "make two files", "needs_files": []}
            elif response_format is swarm_work.PLAN_REVIEW_FORMAT:
                value = {"contribution": f"review by {route}", "message_to_lead": "ready", "needs_files": [], "ready_to_execute": True, "remaining": []}
            elif response_format is swarm_work.WORK_VERIFICATION_FORMAT:
                verification_calls += 1
                finished = verification_calls > 2
                value = {
                    "goal_complete": finished,
                    "feedback": "Both files now exist." if finished else "The Codex file is still missing.",
                    "remaining": [] if finished else ["Create Codex.txt"],
                }
            else:
                work_calls += 1
                changes = [{"path": "Claude.txt", "content": "Claude\n", "reason": "requested"}]
                if work_calls > 1:
                    changes.append({"path": "Codex.txt", "content": "Codex\n", "reason": "verification feedback"})
                value = {"reply": f"Execution pass {work_calls}.", "changes": changes}
            return {"text": json.dumps(value), "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            result = swarm_work.work_together(
                self.config, self.board, "agent-1", "Create one file named for each agent"
            )
        self.assertTrue(result["goal_complete"])
        self.assertEqual(result["work_passes"], 2)
        self.assertEqual((self.project / "Claude.txt").read_text(), "Claude\n")
        self.assertEqual((self.project / "Codex.txt").read_text(), "Codex\n")
        self.assertEqual(result["changed"], ["Claude.txt", "Codex.txt"])

    def test_each_agent_executes_its_own_turn_instead_of_lead_looping_for_itself(self) -> None:
        work_routes: list[str] = []
        codex_contexts: list[str] = []
        final_content = (
            "Created through Nexus collaboration between Claude and Codex.\n"
        )

        def answer(_config, route, _text, **kwargs):
            response_format = kwargs.get("response_format")
            if response_format is swarm_work.PLAN_FORMAT:
                value = {
                    "contribution": (
                        "Create task.txt with a placeholder" if route == "claude"
                        else "Replace the placeholder with the final collaboration note"
                    ),
                    "message_to_lead": "I own this contribution.",
                    "needs_files": [],
                }
            elif response_format is swarm_work.PLAN_REVIEW_FORMAT:
                value = {
                    "contribution": (
                        "Create task.txt with a placeholder" if route == "claude"
                        else "Replace the placeholder with the final collaboration note"
                    ),
                    "message_to_lead": "Ready.", "needs_files": [],
                    "ready_to_execute": True, "remaining": [],
                }
            elif response_format is swarm_work.WORK_FORMAT:
                work_routes.append(route)
                if route == "claude":
                    value = {
                        "reply": "I created my part.",
                        "changes": [{
                            "path": "task.txt", "content": "# awaiting Codex\n",
                            "reason": "Claude owns file creation",
                        }],
                    }
                else:
                    codex_contexts.append(kwargs.get("context", ""))
                    value = {
                        "reply": "I populated my assigned file.",
                        "changes": [{
                            "path": "task.txt", "content": final_content,
                            "reason": "Codex owns file population",
                        }],
                    }
            else:
                value = {
                    "goal_complete": True,
                    "feedback": "The file contains the final collaboration note.",
                    "remaining": [],
                }
            return {"text": json.dumps(value), "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            result = swarm_work.work_together(
                self.config, self.board, "agent-1",
                "Have Claude create a file, then have Codex populate it",
            )

        self.assertEqual(work_routes, ["claude", "codex"])
        self.assertEqual((self.project / "task.txt").read_text(), final_content)
        self.assertEqual(len(result["transaction_ids"]), 2)
        self.assertEqual(result["work_passes"], 1)
        self.assertTrue(result["goal_complete"])
        self.assertIn("EXECUTION TURN — YOU ARE THE ACTING AGENT", codex_contexts[0])
        self.assertIn("You are Codex", codex_contexts[0])
        self.assertIn("# awaiting Codex", codex_contexts[0])
        transcript = chat.read_it(self.config, "claude", "Claude")
        codex_execution = [one for one in transcript if one.phase == "agent_execution"]
        self.assertEqual(len(codex_execution), 1)
        self.assertEqual(codex_execution[0].speaker_name, "Codex")
        self.assertIn("I populated my assigned file", codex_execution[0].text)

    def test_two_no_change_team_passes_stop_even_when_feedback_is_paraphrased(self) -> None:
        work_calls = 0
        verification_calls = 0

        def answer(_config, route, _text, **kwargs):
            nonlocal work_calls, verification_calls
            response_format = kwargs.get("response_format")
            if response_format is swarm_work.PLAN_FORMAT:
                value = {
                    "contribution": f"plan by {route}",
                    "message_to_lead": "ready", "needs_files": [],
                }
            elif response_format is swarm_work.PLAN_REVIEW_FORMAT:
                value = {
                    "contribution": f"review by {route}",
                    "message_to_lead": "ready", "needs_files": [],
                    "ready_to_execute": True, "remaining": [],
                }
            elif response_format is swarm_work.WORK_FORMAT:
                work_calls += 1
                value = {"reply": "Waiting for somebody else.", "changes": []}
            else:
                verification_calls += 1
                value = {
                    "goal_complete": False,
                    "feedback": f"Different wording number {verification_calls}.",
                    "remaining": [f"Paraphrased remaining item {verification_calls}."],
                }
            return {"text": json.dumps(value), "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            result = swarm_work.work_together(
                self.config, self.board, "agent-1", "Create stalled.txt",
            )

        self.assertFalse(result["goal_complete"])
        self.assertEqual(result["work_passes"], 2)
        self.assertEqual(work_calls, 4)
        self.assertEqual(verification_calls, 4)
        self.assertIn(
            "Two complete team execution passes made no file changes.",
            result["remaining"],
        )

    def test_project_phases_do_not_resend_the_original_question_as_each_new_turn(self) -> None:
        original = (
            "ORIGINAL_SENTINEL: are you ChatGPT or Gemini? Then create marker.txt"
        )
        calls: list[tuple[object, str, str]] = []

        def answer(_config, route, asked, **kwargs):
            response_format = kwargs.get("response_format")
            calls.append((response_format, asked, kwargs.get("context", "")))
            if response_format is swarm_work.PLAN_FORMAT:
                value = {
                    "contribution": f"plan by {route}",
                    "message_to_lead": "ready", "needs_files": [],
                }
            elif response_format is swarm_work.PLAN_REVIEW_FORMAT:
                value = {
                    "contribution": f"review by {route}",
                    "message_to_lead": "ready", "needs_files": [],
                    "ready_to_execute": True, "remaining": [],
                }
            elif response_format is swarm_work.WORK_FORMAT:
                value = {"reply": "No new change needed.", "changes": []}
            else:
                value = {
                    "goal_complete": True,
                    "feedback": "Current project state satisfies the goal.",
                    "remaining": [],
                }
            return {"text": json.dumps(value), "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            result = swarm_work.work_together(
                self.config, self.board, "agent-1", original,
            )

        self.assertTrue(result["goal_complete"])
        initial = [asked for format_, asked, _context in calls if format_ is swarm_work.PLAN_FORMAT]
        later = [
            (asked, context) for format_, asked, context in calls
            if format_ is not swarm_work.PLAN_FORMAT
        ]
        self.assertEqual(initial, [original, original])
        self.assertEqual(len(later), 6)
        for asked, context in later:
            self.assertNotIn("ORIGINAL_SENTINEL", asked)
            self.assertIn("NEXUS CURRENT TURN", asked)
            self.assertIn("not as a question to answer again", asked)
            self.assertIn("consider them silently", asked)
            self.assertIn("Do not even state", asked)
            self.assertIn(original, context)
            self.assertIn("NEXUS SHARED COLLABORATION LEDGER", context)
            self.assertIn("Readable full-chat mirror: .harness/chats/", context)
        self.assertTrue(Path(
            self.root / result["collaboration_ledger"]["canonical_path"]
        ).is_file())

    def test_conversation_rounds_continue_instead_of_reasking_the_initial_prompt(self) -> None:
        original = "ORIGINAL_CONVERSATION_SENTINEL: identify yourself and solve this"
        calls: list[tuple[object, str, str]] = []

        def answer(_config, route, asked, **kwargs):
            response_format = kwargs.get("response_format")
            calls.append((response_format, asked, kwargs.get("context", "")))
            if response_format is swarm_work.DISCUSSION_FORMAT:
                value = {
                    "message": f"New progress from {route}.",
                    "goal_complete": True, "remaining": [],
                }
                text = json.dumps(value)
            else:
                text = f"Current response from {route}."
            return {"text": text, "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            result = swarm_work.collaborate(
                self.config, self.board, "agent-1", original,
            )

        self.assertTrue(result["goal_complete"])
        initial = calls[:2]
        later = calls[2:]
        self.assertEqual([asked for _format, asked, _context in initial], [original, original])
        self.assertEqual(len(later), 3)
        for _format, asked, context in later:
            self.assertNotIn("ORIGINAL_CONVERSATION_SENTINEL", asked)
            self.assertIn("NEXUS CURRENT TURN", asked)
            self.assertIn(original, context)
            self.assertIn("NEXUS SHARED COLLABORATION LEDGER", context)
        # Board-order discussion means Codex's turn sees Claude's immediately
        # preceding ledger event even though both independent drafts began in
        # parallel.
        discussion_contexts = [
            context for format_, _asked, context in later
            if format_ is swarm_work.DISCUSSION_FORMAT
        ]
        self.assertIn("New progress from claude.", discussion_contexts[1])
        ledger_path = self.root / result["collaboration_ledger"]["canonical_path"]
        phases = [
            json.loads(line)["phase"]
            for line in ledger_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertIn("agent_discussion", phases)
        self.assertEqual(phases[-1], "final_state")

    def test_invalid_plan_review_stops_before_any_file_transaction(self) -> None:
        review_calls = 0

        def answer(_config, route, _text, **kwargs):
            nonlocal review_calls
            response_format = kwargs.get("response_format")
            if response_format is swarm_work.PLAN_FORMAT:
                value = {
                    "contribution": f"create marker by {route}",
                    "message_to_lead": "create it", "needs_files": [],
                }
            elif response_format is swarm_work.PLAN_REVIEW_FORMAT:
                review_calls += 1
                if route == "claude":
                    # Exact shape of the stale initial Gemini answer from the
                    # real incident: valid for PLAN_FORMAT, invalid for review.
                    value = {
                        "contribution": "create marker",
                        "message_to_lead": "create it", "needs_files": [],
                    }
                else:
                    value = {
                        "contribution": f"same executable plan, wording {review_calls}",
                        "message_to_lead": "ready", "needs_files": [],
                        "ready_to_execute": True, "remaining": [],
                    }
            elif response_format is swarm_work.WORK_VERIFICATION_FORMAT:
                value = {"goal_complete": True, "feedback": "Marker exists.", "remaining": []}
            else:
                value = {
                    "reply": "Created marker.",
                    "changes": [{
                        "path": "marker.txt", "content": "done\n", "reason": "requested",
                    }],
                }
            return {"text": json.dumps(value), "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            with self.assertRaisesRegex(
                swarm_work.SwarmError,
                "paused this collaboration.*not counted as agent speech",
            ):
                swarm_work.work_together(
                    self.config, self.board, "agent-1", "Create marker.txt"
                )

        self.assertEqual(review_calls, 1)
        self.assertFalse((self.project / "marker.txt").exists())
        ledger_paths = list(
            (self.root / ".harness" / "chats").glob("*.collaboration.jsonl")
        )
        self.assertEqual(len(ledger_paths), 1)
        events = [
            json.loads(line)
            for line in ledger_paths[0].read_text(encoding="utf-8").splitlines()
        ]
        self.assertTrue(any(
            event.get("phase") == "provider_transport_failure"
            and event.get("state", {}).get("stage") == "plan_review"
            and event.get("state", {}).get("round") == 1
            for event in events
        ))
        rendered = "\n".join(str(event.get("text") or "") for event in events)
        self.assertNotIn("invalid nexus_board_plan_review_v1", rendered)
        self.assertNotIn("Plan review failed", rendered)

    def test_structured_web_style_json_fence_is_accepted_but_schema_stays_strict(self) -> None:
        answer = {"text": "```json\n{\"contribution\":\"plan\",\"message_to_lead\":\"go\",\"needs_files\":[]}\n```"}
        decoded = swarm_work._decode(answer, "ChatGPT", swarm_work.PLAN_FORMAT)
        self.assertEqual(decoded["contribution"], "plan")

        with self.assertRaisesRegex(Exception, "unexpected extra"):
            swarm_work._decode(
                {"text": "```json\n{\"contribution\":\"plan\",\"message_to_lead\":\"go\",\"needs_files\":[],\"extra\":true}\n```"},
                "ChatGPT", swarm_work.PLAN_FORMAT,
            )

    def test_project_plans_are_published_as_each_agent_finishes(self) -> None:
        turns: list[dict] = []

        def answer(_config, route, _text, **kwargs):
            if kwargs.get("response_format") is swarm_work.PLAN_FORMAT:
                time.sleep(0.01 if route == "codex" else 0.04)
                value = {"contribution": f"plan by {route}", "message_to_lead": "review it", "needs_files": []}
            elif kwargs.get("response_format") is swarm_work.PLAN_REVIEW_FORMAT:
                value = {"contribution": f"reviewed by {route}", "message_to_lead": "ready", "needs_files": [], "ready_to_execute": True, "remaining": []}
            elif kwargs.get("response_format") is swarm_work.WORK_VERIFICATION_FORMAT:
                value = {"goal_complete": True, "feedback": "Done.", "remaining": []}
            else:
                value = {"reply": "Done.", "changes": []}
            return {"text": json.dumps(value), "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            swarm_work.work_together(
                self.config, self.board, "agent-1", "Check the project",
                live_turn=turns.append,
            )
        self.assertEqual([one["speaker_name"] for one in turns[:2]], ["Codex", "Claude"])
        self.assertIn("plan by codex", turns[0]["text"])

    def test_project_work_requires_a_connected_peer_on_the_same_project(self) -> None:
        board = copy.deepcopy(self.board)
        board["works_on"] = [{"agent": "agent-1", "project": "project-1"}]
        with self.assertRaisesRegex(swarm_work.SwarmError, "works on this project"):
            swarm_work.work_together(
                self.config, board, "agent-1", "Create a marker file"
            )

    def test_attachment_is_persisted_but_only_metadata_enters_the_transcript(self) -> None:
        payload = [{
            "name": "screen.png", "type": "image/png",
            "data": "data:image/png;base64," + base64.b64encode(b"not-really-a-png").decode(),
        }]
        public, provider, text = chat.keep_attachments(
            self.config, "claude", payload, "Claude"
        )
        self.assertEqual(text, "")
        self.assertNotIn("data", public[0])
        self.assertEqual(provider[0]["data"], base64.b64encode(b"not-really-a-png").decode())
        path = Path(provider[0]["path"])
        self.assertTrue(path.is_file())

    def test_image_is_translated_to_each_api_providers_native_shape(self) -> None:
        request = ProviderRequest(
            "policy", "context", [{"role": "user", "content": "Look at this"}], "model",
            attachments=[{"name": "screen.png", "type": "image/png", "data": "aW1hZ2U="}],
        )
        openai_response = OpenAIProvider._with_images(request, request.messages, "responses")
        self.assertEqual(openai_response[0]["content"][1]["type"], "input_image")
        openai_chat = OpenAIProvider._with_images(request, request.messages, "chat")
        self.assertEqual(openai_chat[0]["content"][1]["type"], "image_url")

        anthropic = object.__new__(AnthropicProvider)
        anthropic_messages = anthropic._messages(request)
        self.assertEqual(anthropic_messages[0]["content"][1]["source"]["media_type"], "image/png")

        gemini_steps = GeminiProvider._initial_input(request)
        self.assertEqual(gemini_steps[0]["content"][1]["type"], "image")

        ollama = object.__new__(OllamaProvider)
        ollama_messages = ollama._messages(request)
        self.assertEqual(ollama_messages[-1]["images"], ["aW1hZ2U="])


if __name__ == "__main__":
    unittest.main()
