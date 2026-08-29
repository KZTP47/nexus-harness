from __future__ import annotations

import base64
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from our_harness import chat, playwright_runtime, swarm_work
from our_harness import cancellation
from our_harness.changes import FileTransaction, file_sha256
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import ChangePlan, HarnessError, ProviderRequest, ProviderResponse
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
        (self.project / "test_nexus_acceptance.py").write_text(
            "import unittest\n\nclass NexusAcceptance(unittest.TestCase):\n"
            "    def test_selected_project_runner(self):\n        self.assertTrue(True)\n",
            encoding="utf-8",
        )
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
                {
                    "id": "project-1", "name": "Demo", "path": str(self.project), "tasks": [],
                    "test_commands": [[sys.executable, "-m", "unittest", "discover"]],
                },
            ],
            "works_on": [
                {"agent": "agent-1", "project": "project-1"},
                {"agent": "agent-2", "project": "project-1"},
            ],
            "talks_to": [{"one": "agent-1", "other": "agent-2"}],
        }

    def test_long_team_conversation_is_not_silently_tail_sliced(self) -> None:
        first = "BEGIN-OF-CONVERSATION:" + ("a" * 170_000)
        second = "END-OF-CONVERSATION:" + ("b" * 40_000)
        transcript = swarm_work._actual_conversation([
            {"speaker_name": "First", "speaker_route": "one", "text": first},
            {"speaker_name": "Second", "speaker_route": "two", "text": second},
        ])
        self.assertIn("BEGIN-OF-CONVERSATION:", transcript)
        self.assertIn("END-OF-CONVERSATION:", transcript)
        self.assertGreater(len(transcript), 210_000)

    def test_original_realmat_prompt_derives_all_and_only_authorized_destinations(self) -> None:
        prompt = (Path(__file__).parent / "fixtures" / "realmat_original_prompt.txt").read_text(
            encoding="utf-8"
        ).replace("{ROOT}", str(self.project))
        authority = swarm_work._path_authority_from_goal(self.project, prompt)
        expected_writable = [
            "2_Github repos/3_WITH my tests",
            "2_Github repos/4_upload to github",
            "3_test traceability",
            "4_LangGraph for this project",
            "0_Obsidian vault",
        ]
        expected_read_only = [
            "2_Github repos/0_OLD",
            "2_Github repos/1_day one, untouched version made by devs",
            "2_Github repos/2_current version, read only",
        ]
        self.assertEqual(authority["writable"], expected_writable)
        self.assertEqual(authority["read_only"], expected_read_only)
        self.assertEqual(len(authority["references"]), 2)
        self.assertEqual(authority["invalid_writable"], [])
        accepted = swarm_work._validated_changes(self.project, [
            {"path": root + "/proof.txt", "content": "proof", "reason": "required"}
            for root in expected_writable
        ], expected_writable)
        self.assertEqual(len(accepted), 5)
        for denied in [*expected_read_only, "2_Github repos/2_current", "myPages/reference"]:
            with self.subTest(denied=denied), self.assertRaisesRegex(
                HarnessError, "outside the explicit write destinations"
            ):
                swarm_work._validated_changes(self.project, [{
                    "path": denied + "/proof.txt", "content": "bad", "reason": "not authorized",
                }], expected_writable)

    def test_surrounding_path_authority_never_grants_reference_or_read_only_roots(self) -> None:
        output = self.project / "output"
        source = self.project / "source"
        reference = self.project / "references" / "myPages"
        prompt = (
            "The source below is strictly read-only.\n" + str(source) + "\n\n"
            "Create all result files in the destination shown in the next paragraph.\n"
            + str(output) + "\n\nUse this existing project only as inspiration and reference:\n"
            + str(reference)
        )
        authority = swarm_work._path_authority_from_goal(self.project, prompt)
        self.assertEqual(authority["writable"], ["output"])
        self.assertEqual(authority["read_only"], ["source"])
        self.assertEqual(authority["references"], ["references/myPages"])

    def test_explicit_empty_and_invalid_write_authority_fail_closed(self) -> None:
        change = [{"path": "anything.txt", "content": "x", "reason": "probe"}]
        self.assertEqual(len(swarm_work._validated_changes(self.project, change, None)), 1)
        with self.assertRaisesRegex(HarnessError, "no project paths are writable"):
            swarm_work._validated_changes(self.project, change, [])
        valid, rejected = swarm_work._normalized_write_authority(
            self.project, ["output", "Z:/outside/project/output", ""]
        )
        self.assertEqual(valid, ["output"])
        self.assertEqual(rejected, ["Z:/outside/project/output", ""])
        with mock.patch.object(chat, "ask_once") as ask:
            with self.assertRaisesRegex(Exception, "explicit empty write scope"):
                swarm_work.work_together(
                    self.config, self.board, "agent-1", "Create anything.txt",
                    allowed_write_roots=[],
                )
            with self.assertRaisesRegex(Exception, "outside the selected project"):
                swarm_work.work_together(
                    self.config, self.board, "agent-1", "Create output/result.txt",
                    allowed_write_roots=["output", "Z:/outside/project/output"],
                )
            with self.assertRaisesRegex(Exception, "outside the selected project"):
                swarm_work.work_together(
                    self.config, self.board, "agent-1",
                    "Create all result files in:\nZ:\\outside\\project\\output",
                )
            ask.assert_not_called()

    def test_project_work_rejects_oversized_or_control_text_before_any_side_effect(self) -> None:
        with mock.patch.object(swarm_work, "_project_participants", side_effect=RuntimeError("boundary reached")) as participants:
            with self.assertRaisesRegex(RuntimeError, "boundary reached"):
                swarm_work.work_together(self.config, self.board, "agent-1", "x" * 200_000)
            self.assertEqual(participants.call_count, 1)
        with mock.patch.object(swarm_work, "_project_participants") as participants, mock.patch.object(chat, "ask_once") as ask:
            with self.assertRaisesRegex(Exception, "200,001 characters"):
                swarm_work.work_together(self.config, self.board, "agent-1", "x" * 200_001)
            with self.assertRaisesRegex(Exception, "control character"):
                swarm_work.work_together(self.config, self.board, "agent-1", "fix\x00this")
            with self.assertRaisesRegex(Exception, "200,001 characters"):
                swarm_work.work_together(
                    self.config, self.board, "agent-1", "ignored",
                    resume_session_id="validresume1", user_answers="ü" * 200_001,
                )
            participants.assert_not_called()
            ask.assert_not_called()
        self.assertFalse((self.root / ".harness" / "chats").exists())

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
        self.assertEqual(result["discussion_rounds"], 14)
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

        self.assertEqual(result["discussion_rounds"], 14)
        self.assertEqual(result["stopped_because"], "stalled")
        self.assertEqual(discussion_calls, 28)

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
        self.assertIn("the web submit control rejected the turn", saved)

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
        # Provider-authored alpha/beta wording is not authenticated progress,
        # so it follows the conservative stable-state threshold.
        self.assertEqual(result["discussion_rounds"], 14)
        self.assertEqual(result["stopped_because"], "stalled")

    def test_unlimited_collaboration_can_reach_eighteen_advancing_checkpoints(self) -> None:
        discussion_calls = 0

        def answer(_config, route, _text, **kwargs):
            nonlocal discussion_calls
            if kwargs.get("response_format") is swarm_work.DISCUSSION_FORMAT:
                cycle = discussion_calls // 2 + 1
                discussion_calls += 1
                finished = cycle >= 18
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
                self.config, self.board, "agent-1", "Complete eighteen checkpoints",
                round_limit=None,
            )

        self.assertTrue(result["goal_complete"])
        self.assertEqual(result["discussion_rounds"], 18)
        self.assertEqual(result["stopped_because"], "complete")
        self.assertEqual(discussion_calls, 36)

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
                return ProviderResponse(text="the follow-up answer", finish_reason="stop")

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
        self.assertTrue(decision["pair_chat_implicit_collaboration"])
        ask.assert_not_called()

    def test_implicit_pair_keeps_the_lead_answer_when_a_peer_provider_fails(self) -> None:
        calls: list[str] = []

        def answer(_config, route, _text, **_kwargs):
            calls.append(route)
            if route == "codex":
                raise swarm_work.HarnessError("temporary provider failure")
            return {"text": "I am Claude.", "milliseconds": 7, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            result = swarm_work.collaborate(
                self.config, self.board, "agent-1", "Who is this?",
                peer_id="agent-2", filed_as="pair-chat",
                allow_partial_lead_answer=True,
            )

        self.assertCountEqual(calls, ["claude", "codex"])
        self.assertEqual(result["answer"]["text"], "I am Claude.")
        self.assertEqual(result["stopped_because"], "partial_provider_failure")
        self.assertEqual(result["provider_failures"], [{
            "id": "agent-2", "name": "Codex", "route": "codex",
            "provider_reason": "temporary provider failure",
        }])
        self.assertIn("Claude answered", result["partial_provider_failure"])
        self.assertIn("Codex could not join", result["partial_provider_failure"])
        transcript = chat.read_it(self.config, "claude", "pair-chat")
        self.assertEqual([one.phase for one in transcript], ["user_prompt", "final_answer"])
        self.assertEqual(transcript[-1].text, "I am Claude.")
        ledger = self.root / result["collaboration_ledger"]["canonical_path"]
        saved = ledger.read_text(encoding="utf-8")
        self.assertIn("provider_transport_failure", saved)
        self.assertIn("partial_provider_failure", saved)

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
        self.assertTrue(result["verified"])
        self.assertEqual(result["verification_status"], "deterministically_verified")
        transcript = chat.read_it(self.config, "claude", "Claude")
        self.assertEqual([one.phase for one in transcript], [
            "user_prompt", "lead_plan", "agent_plan",
            "agent_plan_review", "agent_plan_review", "lead_execution",
            "agent_execution",
            "agent_verification", "agent_verification", "final_answer",
        ])
        self.assertIn("plan by codex", transcript[2].text)
        self.assertIn("made-by-team.txt", transcript[-1].text)
        self.assertIn("deterministic checks passed", transcript[-1].text)

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

    def test_provider_failure_after_staging_names_the_rolled_back_file_truthfully(self) -> None:
        work_calls = 0
        live_turns: list[dict[str, object]] = []

        def answer(_config, route, _text, **kwargs):
            nonlocal work_calls
            response_format = kwargs.get("response_format")
            if response_format is swarm_work.PLAN_FORMAT:
                value = {"contribution": "create", "message_to_lead": "create", "needs_files": []}
            elif response_format is swarm_work.PLAN_REVIEW_FORMAT:
                value = {
                    "contribution": "create", "message_to_lead": "ready",
                    "needs_files": [], "ready_to_execute": True, "remaining": [],
                }
            elif response_format is swarm_work.WORK_FORMAT:
                work_calls += 1
                if work_calls == 2:
                    raise chat.ChatError("provider unavailable")
                value = {
                    "reply": "Created the requested file.",
                    "changes": [{
                        "path": "index.html", "content": "<p>complete</p>\n",
                        "reason": "requested",
                    }],
                }
            else:
                raise AssertionError(f"unexpected response format for {route}")
            return {"text": json.dumps(value), "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            with self.assertRaisesRegex(
                swarm_work.SwarmError,
                r"rolled back.*index\.html.*provisional changes are not applied",
            ):
                swarm_work.work_together(
                    self.config, self.board, "agent-1", "Create index.html",
                    live_turn=live_turns.append,
                )

        self.assertFalse((self.project / "index.html").exists())
        staged = "\n".join(str(one.get("text") or "") for one in live_turns)
        self.assertIn("Staged provisionally", staged)
        self.assertNotIn("Applied in this turn", staged)

    def test_crashed_process_saga_is_compensated_before_the_next_run(self) -> None:
        target = self.project / "crash.txt"
        target.write_text("before\n", encoding="utf-8")
        script = r'''import os, sys
from pathlib import Path
from our_harness.changes import FileTransaction, file_sha256
from our_harness.models import ChangePlan
from our_harness.swarm_work import _MutationSaga, _manifest_sha256
root = Path(sys.argv[1]).resolve(); target = root / "crash.txt"
saga = _MutationSaga(root, "crash-injection")
txid = FileTransaction.new_transaction_id(); saga.prepare(txid)
manifest = FileTransaction(root).apply([ChangePlan("crash.txt", file_sha256(target), "after\n", reason="crash test")], transaction_id=txid)
saga.applied(txid, _manifest_sha256(manifest))
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

    def test_crash_after_mutation_commit_leaves_a_resumable_checkpoint(self) -> None:
        def answer(_config, route, _text, **kwargs):
            response_format = kwargs.get("response_format")
            if response_format is swarm_work.PLAN_FORMAT:
                value = {"contribution": "create", "message_to_lead": "create", "needs_files": []}
            elif response_format is swarm_work.PLAN_REVIEW_FORMAT:
                value = {"contribution": "review", "message_to_lead": "ready", "needs_files": [], "ready_to_execute": True, "remaining": []}
            elif response_format is swarm_work.WORK_VERIFICATION_FORMAT:
                value = {"goal_complete": True, "feedback": "done", "remaining": []}
            else:
                value = {"reply": "done", "changes": [{"path": "durable.txt", "content": "ok\n", "reason": "requested"}]}
            return {"text": json.dumps(value), "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer), mock.patch.object(
            chat, "keep_multiparty_exchange", side_effect=RuntimeError("crash after commit")
        ):
            with self.assertRaisesRegex(RuntimeError, "crash after commit"):
                swarm_work.work_together(self.config, self.board, "agent-1", "Create durable.txt")
        ledger_path = next((self.root / ".harness" / "chats").glob("*.collaboration.jsonl"))
        events = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
        session_id = str(events[0]["session_id"])
        self.assertTrue(any(event.get("phase") == "mutation_terminal_checkpoint" for event in events))
        with mock.patch.object(chat, "ask_once", side_effect=answer):
            resumed = swarm_work.work_together(
                self.config, self.board, "agent-1", "ignored",
                resume_session_id=session_id, user_answers="continue",
            )
        self.assertTrue(resumed["goal_complete"])
        self.assertEqual((self.project / "durable.txt").read_text(encoding="utf-8"), "ok\n")
        journals = [json.loads(path.read_text(encoding="utf-8")) for path in
                    (self.project / ".harness" / "swarm-mutation-sagas").glob("*.json")]
        self.assertTrue(any(value.get("phase") == "committed" for value in journals))

    def test_saga_conflict_is_durable_and_blocks_later_mutation_recovery(self) -> None:
        target = self.project / "saga-conflict.txt"
        target.write_text("before\n", encoding="utf-8")
        saga = swarm_work._MutationSaga(self.project, "durable-conflict")
        transaction_id = FileTransaction.new_transaction_id()
        saga.prepare(transaction_id)
        manifest = FileTransaction(self.project).apply([ChangePlan(
            "saga-conflict.txt", file_sha256(target), "after\n", reason="test"
        )], transaction_id=transaction_id)
        saga.applied(transaction_id, swarm_work._manifest_sha256(manifest))
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
        self.assertEqual(result["work_passes"], 14)
        self.assertEqual(work_calls, 28)
        self.assertEqual(verification_calls, 28)
        self.assertIn(
            "Nexus detected a repeated end-of-pass project state with unchanged deterministic verification; the run can be resumed after new evidence or user input.",
            result["remaining"],
        )

    def test_project_phases_do_not_resend_the_original_question_as_each_new_turn(self) -> None:
        original = (
            "ORIGINAL_SENTINEL: are you ChatGPT or Gemini? Then create marker.txt"
        )
        calls: list[tuple[object, str, str]] = []
        work_calls = 0

        def answer(_config, route, asked, **kwargs):
            nonlocal work_calls
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
                work_calls += 1
                value = {
                    "reply": "Created the requested marker." if work_calls == 1 else "No new change needed.",
                    "changes": ([{
                        "path": "marker.txt", "content": "done\n", "reason": "requested",
                    }] if work_calls == 1 else []),
                }
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
        self.assertIn("invalid nexus_board_plan_review_v1", rendered)
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

    def test_fenced_web_file_payload_preserves_source_operators_exactly(self) -> None:
        source = "const width = Math.round(canvas.width * dpr);\nbody{height:100vh}"
        answer = {"text": "```json\n" + json.dumps({
            "reply": "Created the requested file.",
            "changes": [{"path": "index.html", "content": source, "reason": "requested"}],
        }) + "\n```"}

        decoded = swarm_work._decode(answer, "Claude web", swarm_work.WORK_FORMAT)

        self.assertEqual(decoded["changes"][0]["content"], source)
        self.assertIn(" * ", decoded["changes"][0]["content"])

    def test_consumer_web_agent_gets_one_strict_format_correction_turn(self) -> None:
        ledger = mock.Mock()
        corrected = {"text": json.dumps({
            "message": "The saved-board relay is verified.",
            "goal_complete": True,
            "remaining": [],
        })}
        with mock.patch.object(chat, "ask_once", return_value=corrected) as asked:
            decoded = swarm_work._decode_with_one_web_repair(
                self.config,
                {"id": "agent-web", "name": "Claude web", "who": "web:claude-example"},
                {"text": "not valid json: C:\\Users\\example"},
                swarm_work.DISCUSSION_FORMAT,
                ledger,
                "pair-chat-example",
                False,
            )

        self.assertTrue(decoded["goal_complete"])
        self.assertEqual(decoded["remaining"], [])
        self.assertIn("STRUCTURED FORMAT CORRECTION", asked.call_args.args[2])
        self.assertIs(asked.call_args.kwargs["response_format"], swarm_work.DISCUSSION_FORMAT)
        ledger.acknowledge.assert_called_once_with("agent-web")

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

    def test_provider_consensus_never_becomes_complete_without_deterministic_verification(self) -> None:
        board = copy.deepcopy(self.board)
        board["projects"][0].pop("test_commands")

        def answer(_config, route, _text, **kwargs):
            response_format = kwargs.get("response_format")
            if response_format is swarm_work.PLAN_FORMAT:
                value = {"contribution": "create", "message_to_lead": "create", "needs_files": []}
            elif response_format is swarm_work.PLAN_REVIEW_FORMAT:
                value = {
                    "contribution": "create", "message_to_lead": "ready", "needs_files": [],
                    "ready_to_execute": True, "remaining": [],
                }
            elif response_format is swarm_work.WORK_VERIFICATION_FORMAT:
                value = {"goal_complete": True, "feedback": "Looks done.", "remaining": []}
            else:
                value = {
                    "reply": "Created it.",
                    "changes": [{"path": "claimed.txt", "content": "done\n", "reason": "requested"}],
                }
            return {"text": json.dumps(value), "milliseconds": 1, "model": "same-model"}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            result = swarm_work.work_together(
                self.config, board, "agent-1", "Create claimed.txt",
            )

        self.assertFalse(result["goal_complete"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["status"], "applied_unverified")
        self.assertEqual(result["verification_status"], "applied_unverified")
        self.assertTrue(result["resume_token"])
        self.assertTrue((self.project / "claimed.txt").is_file())

    def test_realmat_style_false_green_and_missing_runner_dependencies_fail_preflight(self) -> None:
        (self.project / "package.json").write_text(json.dumps({
            "scripts": {"test": "node -e \"console.log('No unit tests to run')\" || exit 0"},
            "devDependencies": {},
        }), encoding="utf-8")
        test = self.project / "tests" / "API" / "health.spec.ts"
        test.parent.mkdir(parents=True)
        test.write_text(
            "import { test, expect } from '@playwright/test';\n"
            "test('health', async () => { expect(204).toBe(204); });\n",
            encoding="utf-8",
        )
        result = swarm_work._run_selected_project_verification(
            self.config, self.project, {}, "Create API tests",
            ["tests/API/health.spec.ts"], None,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["basis"], "test_preflight")
        self.assertIn("@playwright/test", result["reason"])
        self.assertIn("known zero-test", result["preflight"]["false_green"])
        missing_e2e = swarm_work._run_selected_project_verification(
            self.config, self.project, {}, "Create API and E2E tests",
            ["tests/API/health.spec.ts"], None,
        )
        self.assertEqual(missing_e2e["basis"], "test_requirement_coverage")
        self.assertIn("E2E", missing_e2e["reason"])

    def test_each_requested_test_level_requires_runnable_and_executed_evidence(self) -> None:
        unit = self.project / "tests" / "UNIT" / "test_unit_real.py"
        unit.parent.mkdir(parents=True)
        unit.write_text(
            "import unittest\nclass UnitReal(unittest.TestCase):\n"
            "    def test_unit_real(self): self.assertTrue(True)\n",
            encoding="utf-8",
        )
        e2e_readme = self.project / "tests" / "E2E" / "README.md"
        e2e_readme.parent.mkdir(parents=True)
        e2e_readme.write_text("E2E tests belong here\n", encoding="utf-8")
        changed = ["tests/UNIT/test_unit_real.py", "tests/E2E/README.md"]
        result = swarm_work._run_selected_project_verification(
            self.config, self.project, self.board["projects"][0],
            "Create real unit and E2E tests", changed, None,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["basis"], "test_requirement_coverage")
        self.assertIn("E2E", result["reason"])

        skipped = self.project / "tests" / "E2E" / "test_template.py"
        skipped.write_text(
            "import unittest\nclass Template(unittest.TestCase):\n"
            "    @unittest.skip('template')\n"
            "    def test_e2e_template(self): self.fail('never runs')\n",
            encoding="utf-8",
        )
        skipped_result = swarm_work._run_selected_project_verification(
            self.config, self.project, self.board["projects"][0],
            "Create real unit and E2E tests",
            ["tests/UNIT/test_unit_real.py", "tests/E2E/test_template.py"], None,
        )
        self.assertEqual(skipped_result["basis"], "test_requirement_coverage")

        e2e = self.project / "tests" / "E2E" / "test_e2e_real.py"
        e2e.write_text(
            "import unittest\nclass E2EReal(unittest.TestCase):\n"
            "    def test_e2e_real(self): self.assertTrue(True)\n",
            encoding="utf-8",
        )
        passed = swarm_work._run_selected_project_verification(
            self.config, self.project, self.board["projects"][0],
            "Create real unit and E2E tests",
            ["tests/UNIT/test_unit_real.py", "tests/E2E/test_e2e_real.py"], None,
        )
        self.assertEqual(passed["status"], "passed", passed)
        execution = passed["requirement_evidence"]["execution"]
        self.assertEqual(execution["proven_test_levels"], ["E2E", "unit"])

    def test_whole_goal_contract_rejects_one_artifact_for_multi_artifact_goal(self) -> None:
        unit = self.project / "tests" / "UNIT" / "test_only.py"
        unit.parent.mkdir(parents=True)
        unit.write_text(
            "import unittest\nclass Only(unittest.TestCase):\n"
            "    def test_only(self): self.assertTrue(True)\n",
            encoding="utf-8",
        )
        goal = (
            "Create tests, a traceability workbook, LangGraph enforcement, "
            "an upload bundle, and lasting Obsidian memory"
        )
        contract = swarm_work._derive_requirement_contract(
            self.project, goal, ratified_by=["agent-1", "agent-2"]
        )
        self.assertEqual(contract["status"], "ratified")
        self.assertEqual(
            {one["id"] for one in contract["requirements"]},
            {"tests", "traceability", "langgraph_artifact", "langgraph_enforcement", "upload_bundle", "durable_memory"},
        )
        result = swarm_work._run_selected_project_verification(
            self.config, self.project, self.board["projects"][0], goal,
            ["tests/UNIT/test_only.py"], None,
            requirement_contract=contract,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["basis"], "requirement_contract")
        self.assertIn("traceability", result["reason"])
        self.assertIn("durable_memory", result["reason"])

    def test_novel_coordinated_artifacts_get_independent_requirement_evidence(self) -> None:
        goal = "Create a PDF report and a deployment manifest"
        contract = swarm_work._derive_requirement_contract(self.project, goal)
        generic = [
            one for one in contract["requirements"]
            if one["kind"] == "generic_artifact"
        ]
        self.assertEqual(len(generic), 2, contract)
        (self.project / "audit-report.pdf").write_bytes(b"%PDF-fixture")
        evidence = swarm_work._requirement_artifact_evidence(
            self.project, contract, ["audit-report.pdf"]
        )
        self.assertFalse(evidence["passed"])
        self.assertEqual(len(evidence["unmet"]), 1)
        (self.project / "deployment-manifest.json").write_text("{}", encoding="utf-8")
        complete = swarm_work._requirement_artifact_evidence(
            self.project, contract,
            ["audit-report.pdf", "deployment-manifest.json"],
        )
        self.assertTrue(complete["passed"], complete)

    def test_independent_artifact_contract_rejects_cross_kind_and_name_collisions(self) -> None:
        test_path = self.project / "tests" / "UNIT" / "test_only.py"
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text(
            "import unittest\nclass Only(unittest.TestCase):\n    def test_only(self): self.assertTrue(True)\n",
            encoding="utf-8",
        )
        goals = (
            "Create tests and an upload bundle for GitHub",
            "Create tests and a traceability workbook",
            "Create tests and lasting Obsidian memory",
            "Create tests and a PDF report",
            "Create tests and a deployment manifest",
        )
        for goal in goals:
            with self.subTest(goal=goal):
                contract = swarm_work._derive_requirement_contract(self.project, goal)
                evidence = swarm_work._requirement_artifact_evidence(
                    self.project, contract, ["tests/UNIT/test_only.py"]
                )
                self.assertFalse(evidence["passed"], evidence)
                self.assertTrue(any(one != "tests" for one in evidence["unmet"]), evidence)

        collision_goal = (
            "Create an upload bundle with a commit message and lasting Obsidian memory"
        )
        collision_contract = swarm_work._derive_requirement_contract(self.project, collision_goal)
        collision = self.project / "upload_bundle" / "commit-message-memory.md"
        collision.parent.mkdir(parents=True)
        collision.write_text("one file cannot be three deliverables", encoding="utf-8")
        collision_evidence = swarm_work._requirement_artifact_evidence(
            self.project, collision_contract, ["upload_bundle/commit-message-memory.md"]
        )
        self.assertFalse(collision_evidence["passed"], collision_evidence)
        self.assertIn("durable_memory", collision_evidence["unmet"])

        generic_contract = swarm_work._derive_requirement_contract(
            self.project, "Create a PDF report and a deployment manifest"
        )
        (self.project / "deployment-report.pdf").write_bytes(b"%PDF")
        one_generic = swarm_work._requirement_artifact_evidence(
            self.project, generic_contract, ["deployment-report.pdf"]
        )
        self.assertFalse(one_generic["passed"], one_generic)
        (self.project / "deployment-manifest.json").write_text("{}", encoding="utf-8")
        two_generic = swarm_work._requirement_artifact_evidence(
            self.project, generic_contract,
            ["deployment-report.pdf", "deployment-manifest.json"],
        )
        self.assertTrue(two_generic["passed"], two_generic)

        novel_contract = swarm_work._derive_requirement_contract(
            self.project, "Create a dependency provenance ledger"
        )
        (self.project / "unrelated.txt").write_text("x", encoding="utf-8")
        unrelated = swarm_work._requirement_artifact_evidence(
            self.project, novel_contract, ["unrelated.txt"]
        )
        self.assertFalse(unrelated["passed"], unrelated)
        (self.project / "dependency-provenance-ledger.json").write_text("{}", encoding="utf-8")
        named = swarm_work._requirement_artifact_evidence(
            self.project, novel_contract, ["dependency-provenance-ledger.json"]
        )
        self.assertTrue(named["passed"], named)

    def test_local_imperative_artifacts_survive_long_background_and_ignore_relational_words(self) -> None:
        background = "Background context only. " + ("constraint detail " * 70)
        paired = (
            ("Create a release checklist.", "release-checklist.md"),
            ("Create an SBOM.", "software-sbom.json"),
            ("Create a migration guide.", "migration-guide.md"),
            ("Create a certificate of compliance.", "compliance-certificate.pdf"),
            ("Create a rollout plan for production.", "production-rollout-plan.md"),
            ("Create a guide to deployment.", "deployment-guide.md"),
        )
        for imperative, relative in paired:
            for goal in (imperative, imperative + " " + background, background + " " + imperative):
                with self.subTest(goal=goal[:60], relative=relative):
                    contract = swarm_work._derive_requirement_contract(self.project, goal)
                    generic = [
                        one for one in contract["requirements"]
                        if one["kind"] == "generic_artifact"
                    ]
                    self.assertEqual(len(generic), 1, contract)
                    target = self.project / relative
                    target.write_text("evidence", encoding="utf-8")
                    positive = swarm_work._requirement_artifact_evidence(
                        self.project, contract, [relative]
                    )
                    near_miss = swarm_work._requirement_artifact_evidence(
                        self.project, contract, ["unrelated.txt"]
                    )
                    self.assertTrue(positive["passed"], positive)
                    self.assertFalse(near_miss["passed"], near_miss)

        multiple_goal = background + " Create an SBOM and a migration guide. " + background
        multiple = swarm_work._derive_requirement_contract(self.project, multiple_goal)
        generic = [one for one in multiple["requirements"] if one["kind"] == "generic_artifact"]
        self.assertEqual(len(generic), 2, multiple)
        self.assertFalse(swarm_work._requirement_artifact_evidence(
            self.project, multiple, ["software-sbom.json"]
        )["passed"])
        self.assertTrue(swarm_work._requirement_artifact_evidence(
            self.project, multiple, ["software-sbom.json", "migration-guide.md"]
        )["passed"])

    def test_terminal_punctuation_preserves_exact_filename_and_extension_contracts(self) -> None:
        cases = (
            ("Update parser.py.", "parser.py"),
            ("Update parser.py, then report status", "parser.py"),
            ("Update parser.py; keep the API stable", "parser.py"),
            ("Update parser.py: preserve behavior", "parser.py"),
            ("Update parser.py!", "parser.py"),
            ("Update parser.py?", "parser.py"),
            ("Update (parser.py)", "parser.py"),
            ("Update config.test.py.", "config.test.py"),
            ("Update folder/dir with spaces/config.test.py.", "folder/dir with spaces/config.test.py"),
            ("Create notes.md.", "notes.md"),
        )
        for goal, relative in cases:
            with self.subTest(goal=goal):
                contract = swarm_work._derive_requirement_contract(self.project, goal)
                exact = [one for one in contract["requirements"] if one["kind"] == "exact_path"]
                self.assertEqual([relative], exact[0]["effect_paths"], contract)
                path = self.project / Path(relative)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("changed", encoding="utf-8")
                self.assertTrue(swarm_work._requirement_artifact_evidence(
                    self.project, contract, [relative]
                )["passed"])
                near = str(Path(relative).with_suffix(".txt")).replace("\\", "/")
                self.assertFalse(swarm_work._requirement_artifact_evidence(
                    self.project, contract, [near]
                )["passed"])

    def test_coordinated_imperative_verbs_do_not_become_phantom_artifact_nouns(self) -> None:
        cases = (
            (
                "Create a report and document findings in notes.md.",
                {"report.pdf", "notes.md"},
                {"report"},
            ),
            (
                "Create a manifest and write instructions to README.md.",
                {"manifest.json", "README.md"},
                {"manifest"},
            ),
        )
        for goal, changed, expected_generic_terms in cases:
            with self.subTest(goal=goal):
                contract = swarm_work._derive_requirement_contract(self.project, goal)
                generic = [one for one in contract["requirements"] if one["kind"] == "generic_artifact"]
                self.assertEqual(
                    expected_generic_terms,
                    {term for one in generic for term in one.get("artifact_terms", [])},
                    contract,
                )
                for relative in changed:
                    (self.project / relative).write_text("evidence", encoding="utf-8")
                self.assertTrue(swarm_work._requirement_artifact_evidence(
                    self.project, contract, sorted(changed)
                )["passed"])

        noun_coordination = swarm_work._derive_requirement_contract(
            self.project, "Create a compliance report and a deployment guide."
        )
        self.assertEqual(
            2,
            len([one for one in noun_coordination["requirements"] if one["kind"] == "generic_artifact"]),
            noun_coordination,
        )

    def test_coordinated_content_actions_stay_with_prior_artifact_without_filename(self) -> None:
        for goal, forbidden in (
            ("Create a report and document findings.", "findings"),
            ("Create a report and include analysis.", "analysis"),
            ("Create a report and describe results.", "results"),
            ("Create a report and summarize conclusions.", "conclusions"),
        ):
            with self.subTest(goal=goal):
                contract = swarm_work._derive_requirement_contract(self.project, goal)
                generic = [
                    one for one in contract["requirements"]
                    if one["kind"] == "generic_artifact"
                ]
                self.assertEqual([["report"]], [one["artifact_terms"] for one in generic], contract)
                self.assertNotIn(forbidden, str(contract).casefold())
                (self.project / "findings-report.pdf").write_bytes(b"%PDF evidence")
                self.assertTrue(swarm_work._requirement_artifact_evidence(
                    self.project, contract, ["findings-report.pdf"]
                )["passed"])

        for goal in (
            "Create a report and a findings log.",
            "Create a report and create a findings log.",
            "Create a report and generate a findings log.",
        ):
            with self.subTest(two_deliverables=goal):
                contract = swarm_work._derive_requirement_contract(self.project, goal)
                generic = [
                    one for one in contract["requirements"]
                    if one["kind"] == "generic_artifact"
                ]
                self.assertEqual(2, len(generic), contract)

    def test_artifact_heads_have_no_word_count_cliff_or_global_modifier_blacklist(self) -> None:
        phrases = (
            "release readiness validation checklist document",
            "production release readiness validation checklist document",
            "production disaster recovery readiness validation checklist document",
            "enterprise production regional disaster recovery operational readiness validation evidence checklist document",
            "session log",
            "folder inventory",
            "copy index",
        )
        for phrase in phrases:
            goal = f"Create a {phrase}."
            relative = phrase.replace(" ", "-") + ".md"
            with self.subTest(goal=goal):
                contract = swarm_work._derive_requirement_contract(self.project, goal)
                generic = [
                    one for one in contract["requirements"]
                    if one["kind"] == "generic_artifact"
                ]
                self.assertEqual(1, len(generic), contract)
                self.assertNotEqual(["project_effect"], [one["id"] for one in contract["requirements"]])
                (self.project / relative).write_text("evidence", encoding="utf-8")
                self.assertTrue(swarm_work._requirement_artifact_evidence(
                    self.project, contract, [relative]
                )["passed"])
                self.assertFalse(swarm_work._requirement_artifact_evidence(
                    self.project, contract, ["unrelated.txt"]
                )["passed"])

    def test_explicit_goal_paths_support_spaces_unicode_and_extensionless_names(self) -> None:
        cases = (
            ("Update release notes.md.", "release notes.md"),
            ('Update "release notes.md".', "release notes.md"),
            ("Update release notes.md; preserve formatting", "release notes.md"),
            ("Update folder/dir with spaces/config.test.py.", "folder/dir with spaces/config.test.py"),
            ("Update Makefile.", "Makefile"),
            ("Update Dockerfile.", "Dockerfile"),
            ("Update résumé.md.", "résumé.md"),
            ("Update dokumentation-åäö.md.", "dokumentation-åäö.md"),
        )
        for goal, relative in cases:
            with self.subTest(goal=goal):
                self.assertEqual([relative], swarm_work._goal_named_paths(goal))
                contract = swarm_work._derive_requirement_contract(self.project, goal)
                exact = [one for one in contract["requirements"] if one["kind"] == "exact_path"]
                self.assertEqual([[relative]], [one["effect_paths"] for one in exact], contract)
                target = self.project / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("changed", encoding="utf-8")
                self.assertTrue(swarm_work._requirement_artifact_evidence(
                    self.project, contract, [relative]
                )["passed"])
                near = "notes.md" if relative == "release notes.md" else "unrelated.txt"
                self.assertFalse(swarm_work._requirement_artifact_evidence(
                    self.project, contract, [near]
                )["passed"])

        for reference in (
            "Update https://example.com/file.md",
            r"Update C:\outside\file.md",
            r"Update \\server\share\file.md",
        ):
            with self.subTest(external=reference):
                self.assertEqual([], swarm_work._goal_named_paths(reference))

    def test_named_path_roles_separate_required_effects_from_protected_references(self) -> None:
        cases = (
            ("Do not change reference.md; update parser.py.", ["parser.py"], ["reference.md"]),
            ("Review reference.md and update parser.py", ["parser.py"], ["reference.md"]),
            ("Update parser.py without changing API.md", ["parser.py"], ["API.md"]),
            ("Using reference.md as a read-only reference, update parser.py", ["parser.py"], ["reference.md"]),
            ("Fix parser.py; review notes.md, but do not modify notes.md.", ["parser.py"], ["notes.md"]),
        )
        for goal, effects, protected in cases:
            with self.subTest(goal=goal):
                for relative in set(effects + protected):
                    (self.project / relative).write_text("baseline", encoding="utf-8")
                roles = swarm_work._goal_path_roles(goal)
                self.assertEqual(effects, roles["effects"], roles)
                self.assertEqual(protected, roles["protected"], roles)
                contract = swarm_work._derive_requirement_contract(self.project, goal)
                exact = [
                    path for one in contract["requirements"] if one["kind"] == "exact_path"
                    for path in one["effect_paths"]
                ]
                self.assertEqual(effects, exact, contract)
                self.assertEqual(protected, contract["protected_paths"], contract)
                self.assertTrue(swarm_work._requirement_artifact_evidence(
                    self.project, contract, effects
                )["passed"])
                violated = swarm_work._requirement_artifact_evidence(
                    self.project, contract, effects + protected
                )
                self.assertFalse(violated["passed"], violated)
                self.assertEqual(protected, violated["protected_violations"])
                with self.assertRaises(HarnessError):
                    swarm_work._validated_changes(
                        self.project,
                        [{"path": protected[0], "content": "changed"}],
                        protected_paths=protected,
                    )

    def test_artifact_extraction_requires_positive_user_imperatives(self) -> None:
        for goal in (
            "Do not create a report; update parser.py",
            "Use existing report; do not create a new report. Update parser.py",
            "The script can create a report; update parser.py",
            "The tool could generate a manifest; update parser.py",
        ):
            with self.subTest(goal=goal):
                contract = swarm_work._derive_requirement_contract(self.project, goal)
                self.assertFalse(any(
                    one["kind"] == "generic_artifact" for one in contract["requirements"]
                ), contract)

        for goal in (
            "Create a report; update parser.py",
            "Please generate a manifest and update parser.py",
            "In this folder, you will create a session log.",
        ):
            with self.subTest(positive=goal):
                contract = swarm_work._derive_requirement_contract(self.project, goal)
                self.assertTrue(any(
                    one["kind"] == "generic_artifact" for one in contract["requirements"]
                ), contract)

    def test_plural_counted_and_oxford_artifacts_are_independent(self) -> None:
        cases = (
            ("Create reports and manifests.", ["report-1.md", "manifest-1.json"], 2),
            (
                "Create two reports and three checklists.",
                ["report-1.md", "report-2.md", "checklist-1.md", "checklist-2.md", "checklist-3.md"],
                5,
            ),
            ("Generate guides, logs, and inventories.", ["guide.md", "log.md", "inventory.md"], 3),
            (
                "Create a session log, folder inventory, and copy index.",
                ["session-log.md", "folder-inventory.md", "copy-index.md"],
                3,
            ),
        )
        for goal, changed, count in cases:
            with self.subTest(goal=goal):
                contract = swarm_work._derive_requirement_contract(self.project, goal)
                generic = [one for one in contract["requirements"] if one["kind"] == "generic_artifact"]
                self.assertEqual(count, len(generic), contract)
                self.assertFalse(any("and" in one["artifact_terms"] for one in generic), generic)
                for relative in changed:
                    (self.project / relative).write_text("evidence", encoding="utf-8")
                self.assertTrue(swarm_work._requirement_artifact_evidence(
                    self.project, contract, changed
                )["passed"])
                self.assertFalse(swarm_work._requirement_artifact_evidence(
                    self.project, contract, changed[:-1]
                )["passed"])

    def test_goal_path_spans_are_longest_coordinated_and_fail_closed(self) -> None:
        cases = (
            ('Update "docs.v2/release notes.md".', ["docs.v2/release notes.md"]),
            ("Update docs.v2/release notes.md.", ["docs.v2/release notes.md"]),
            ("Update .github/workflows/TEST-ci.yml.", [".github/workflows/TEST-ci.yml"]),
            (
                "Create docs/release notes.md and docs/setup guide.md.",
                ["docs/release notes.md", "docs/setup guide.md"],
            ),
            ("Fix the bug. Review notes.md", ["notes.md"]),
            ("Update My Report v2.1.md.", ["My Report v2.1.md"]),
        )
        for goal, expected in cases:
            with self.subTest(goal=goal):
                self.assertEqual(expected, swarm_work._goal_named_paths(goal))
                contract = swarm_work._derive_requirement_contract(self.project, goal)
                exact = [
                    path for one in contract["requirements"] if one["kind"] == "exact_path"
                    for path in one["effect_paths"]
                ]
                if goal.startswith("Fix the bug"):
                    self.assertEqual([], exact)
                    self.assertEqual(["notes.md"], contract["protected_paths"])
                else:
                    self.assertEqual(expected, exact, contract)

        for goal in ("Update ../outside.md.", r"Update ..\outside.md", "Update file.md:stream"):
            with self.subTest(unsafe=goal):
                with self.assertRaises(HarnessError):
                    swarm_work._goal_named_paths(goal)

    def test_artifact_complements_are_content_and_named_files_are_exact(self) -> None:
        cases = (
            ("Create a report that includes findings and conclusions.", ["report"]),
            ("Create a report for the project.", ["report"]),
        )
        for goal, terms in cases:
            with self.subTest(goal=goal):
                contract = swarm_work._derive_requirement_contract(self.project, goal)
                generic = [one for one in contract["requirements"] if one["kind"] == "generic_artifact"]
                self.assertEqual([terms], [one["artifact_terms"] for one in generic], contract)
                (self.project / "report.pdf").write_bytes(b"%PDF")
                self.assertTrue(swarm_work._requirement_artifact_evidence(
                    self.project, contract, ["report.pdf"]
                )["passed"])
                self.assertFalse(swarm_work._requirement_artifact_evidence(
                    self.project, contract, ["findings.txt"]
                )["passed"])

        named = swarm_work._derive_requirement_contract(
            self.project, "Create a report named final-report.md."
        )
        self.assertEqual([], [one for one in named["requirements"] if one["kind"] == "generic_artifact"])
        exact = [one for one in named["requirements"] if one["kind"] == "exact_path"]
        self.assertEqual([["final-report.md"]], [one["effect_paths"] for one in exact], named)

    def test_remote_attachments_are_ignored_by_git_boundary(self) -> None:
        checkout = Path(__file__).resolve().parents[1]
        ignored = subprocess.run(
            ["git", "check-ignore", "--no-index", ".codex-remote-attachments/evidence.jpg"],
            cwd=checkout, text=True, capture_output=True, check=False,
        )
        self.assertEqual(0, ignored.returncode, ignored.stderr)

    def test_clause_roles_cover_reference_preservation_and_transfer_semantics(self) -> None:
        cases = (
            ("Update app.py, but preserve README.md unchanged.", ["app.py"], ["README.md"]),
            ("Update app.py and keep README.md unchanged.", ["app.py"], ["README.md"]),
            ("Update app.py and leave README.md untouched.", ["app.py"], ["README.md"]),
            ("Fix parser.py and use notes.md only as reference.", ["parser.py"], ["notes.md"]),
            ("Use reference.md to update parser.py.", ["parser.py"], ["reference.md"]),
            ("Consult notes.md as reference, then fix parser.py.", ["parser.py"], ["notes.md"]),
            ("Read notes.md as reference and update parser.py.", ["parser.py"], ["notes.md"]),
            ("Fix parser.py using notes.md as reference.", ["parser.py"], ["notes.md"]),
            ("Update parser.py from notes.md.", ["parser.py"], ["notes.md"]),
            ("Fix parser.py based on notes.md.", ["parser.py"], ["notes.md"]),
            ("Fix parser.py after reviewing notes.md.", ["parser.py"], ["notes.md"]),
            ("Fix parser.py with notes.md as a read-only reference.", ["parser.py"], ["notes.md"]),
            ("Fix parser.py without changing API.md.", ["parser.py"], ["API.md"]),
            ("Update parser.py, not API.md.", ["parser.py"], ["API.md"]),
            ("Rename old name.md to new name.md.", ["old name.md", "new name.md"], []),
            ("Move old name.md to new name.md.", ["old name.md", "new name.md"], []),
            ("Replace old name.md with new name.md.", ["old name.md"], ["new name.md"]),
            ("Copy source file.md to destination file.md.", ["destination file.md"], ["source file.md"]),
        )
        for goal, effects, protected in cases:
            with self.subTest(goal=goal):
                self.assertEqual(effects, swarm_work._goal_path_roles(goal)["effects"])
                self.assertEqual(protected, swarm_work._goal_path_roles(goal)["protected"])
                contract = swarm_work._derive_requirement_contract(self.project, goal)
                exact = [
                    path for one in contract["requirements"] if one["kind"] == "exact_path"
                    for path in one["effect_paths"]
                ]
                self.assertEqual(effects, exact, contract)
                self.assertEqual(protected, contract["protected_paths"], contract)

    def test_required_contrast_target_is_effect_and_cannot_complete_from_unrelated_change(self) -> None:
        for connector, adjective in (
            ("However", "required"),
            ("Nevertheless", "essential"),
            ("Nonetheless", "necessary"),
        ):
            goal = f"Do not modify anything. {connector}, repairs to parser.py are {adjective}."
            with self.subTest(goal=goal):
                contract = swarm_work._derive_requirement_contract(self.project, goal)
                self.assertEqual("mutation", contract["intent"], contract)
                self.assertEqual([], contract["protected_paths"], contract)
                exact = [
                    path for one in contract["requirements"] if one["kind"] == "exact_path"
                    for path in one["effect_paths"]
                ]
                self.assertEqual(["parser.py"], exact, contract)
                self.assertFalse(swarm_work._goal_effect_evidence(
                    self.project, goal, ["unrelated.py"]
                )["passed"])

    def test_informational_questions_and_advice_are_read_only(self) -> None:
        for goal in (
            "Can parser.py create reports?",
            "Does parser.py need a fix?",
            "How should I update parser.py?",
            "Should parser.py be refactored?",
            "What would fixing parser.py change?",
            "Explain how to create a report from parser.py.",
            "Tell me whether to update parser.py.",
        ):
            with self.subTest(goal=goal):
                self.assertEqual("read_only", swarm_work._goal_intent(goal))
                contract = swarm_work._derive_requirement_contract(self.project, goal)
                self.assertEqual([], contract["requirements"], contract)
                self.assertEqual([], swarm_work._explicit_created_artifacts(goal))
                self.assertTrue(swarm_work._goal_effect_evidence(
                    self.project, goal, []
                )["passed"])

    def test_implementation_purpose_infinitives_do_not_create_output_artifacts(self) -> None:
        for goal in (
            "Add functionality to create a report.",
            "Implement a command to generate a manifest.",
            "Write code to build a checklist.",
            "Implement logic to produce a session log.",
        ):
            with self.subTest(goal=goal):
                self.assertEqual([], swarm_work._explicit_created_artifacts(goal))
                contract = swarm_work._derive_requirement_contract(self.project, goal)
                self.assertTrue(any(
                    one["kind"] == "behavior" for one in contract["requirements"]
                ), contract)
                self.assertFalse(any(
                    one["kind"] == "generic_artifact" for one in contract["requirements"]
                ), contract)

        for direct in ("Create a report.", "Generate a manifest.", "Build a checklist."):
            with self.subTest(direct=direct):
                self.assertTrue(swarm_work._explicit_created_artifacts(direct))

    def test_generic_behavior_requires_goal_and_changed_state_bound_probe(self) -> None:
        goal = "Implement retry behavior so failed requests are retried."
        (self.project / "retry.py").write_text("enabled = True\n", encoding="utf-8")
        contract = swarm_work._derive_requirement_contract(self.project, goal)
        behavior = next(one for one in contract["requirements"] if one["kind"] == "behavior")
        goal_hash = behavior["goal_sha256"]

        def project_for(changed_hash: str) -> dict:
            payload = {
                "summary": {"executed": 1, "failed": 0, "retry_verified": True},
                "goal_sha256": goal_hash,
                "changed_paths_sha256": changed_hash,
            }
            command = [sys.executable, "-c", f"import json; print(json.dumps({payload!r}))"]
            project = copy.deepcopy(self.board["projects"][0])
            project["test_commands"] = [command]
            project["test_evidence_contracts"] = [{
                "command": command,
                "format": "json-stdout",
                "total_field": "summary.executed",
                "failed_field": "summary.failed",
                "requirement_probes": {
                    behavior["id"]: {
                        "field": "summary.retry_verified",
                        "goal_sha256_field": "goal_sha256",
                        "changed_paths_sha256_field": "changed_paths_sha256",
                    },
                },
            }]
            return project

        stale = project_for("0" * 64)
        negative = swarm_work._run_selected_project_verification(
            self.config, self.project, stale, goal, ["retry.py"], None,
            requirement_contract=contract,
        )
        self.assertEqual("failed", negative["status"], negative)
        self.assertEqual("requirement_execution_evidence", negative["basis"], negative)

        changed_hash = __import__("hashlib").sha256(
            json.dumps(["retry.py"], separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        positive = swarm_work._run_selected_project_verification(
            self.config, self.project, project_for(changed_hash), goal, ["retry.py"], None,
            requirement_contract=contract,
        )
        self.assertEqual("failed", positive["status"], positive)
        self.assertFalse(positive["requirement_evidence"]["execution"]["behavior_proof"])

    def test_unsafe_path_shapes_and_protected_effect_conflicts_fail_closed(self) -> None:
        for goal in (
            'Update "../evil.txt".',
            r"Update folder:name/file.md.",
            "Update docs//notes.md.",
            r"Update docs\\\\notes.md.",
        ):
            with self.subTest(goal=goal):
                with self.assertRaises(HarnessError):
                    swarm_work._goal_named_paths(goal)

        coordinated = swarm_work._derive_requirement_contract(
            self.project, "Update config.py, .env, and README.md."
        )
        exact = [
            path for one in coordinated["requirements"] if one["kind"] == "exact_path"
            for path in one["effect_paths"]
        ]
        self.assertEqual(["config.py", "README.md", ".env"], exact, coordinated)

        protected_goal = "Fix parser.py using notes.md as a read-only reference."
        with self.assertRaisesRegex(HarnessError, "conflicts with protected"):
            swarm_work._derive_requirement_contract(
                self.project, protected_goal, required_effect_paths=["notes.md"]
            )
        with self.assertRaisesRegex(HarnessError, "conflicts with protected"):
            swarm_work._goal_effect_evidence(
                self.project, protected_goal, ["parser.py"], required_effect_paths=["notes.md"]
            )

    def test_realmat_contract_has_no_procedural_phantom_artifacts_and_exact_ci_path_is_exact(self) -> None:
        prompt = (Path(__file__).parent / "fixtures" / "realmat_original_prompt.txt").read_text(
            encoding="utf-8"
        ).replace("{ROOT}", str(self.project))
        contract = swarm_work._derive_requirement_contract(self.project, prompt)
        words = " ".join(
            str(one.get("description", "")) + " " + str(one.get("artifact_terms", ""))
            for one in contract["requirements"]
        ).casefold()
        for phantom in ("hour", "every session", "year after year", "copies"):
            self.assertNotIn(phantom, words, contract)
        ci_requirement = next(
            one for one in contract["requirements"]
            if any(str(path).casefold().endswith("test-ci.yml") for path in one.get("effect_paths", []))
        )
        self.assertEqual(ci_requirement["kind"], "exact_path")
        ci_relative = str(ci_requirement["effect_paths"][0])
        ci = self.project / Path(ci_relative)
        ci.parent.mkdir(parents=True, exist_ok=True)
        ci.write_text("name: tests\n", encoding="utf-8")
        present = swarm_work._requirement_artifact_evidence(
            self.project,
            {"requirements": [ci_requirement]},
            [ci_relative],
        )
        absent = swarm_work._requirement_artifact_evidence(
            self.project,
            {"requirements": [ci_requirement]},
            [],
        )
        self.assertTrue(present["passed"], present)
        self.assertFalse(absent["passed"], absent)

        deliverables = {
            "2_Github repos/3_WITH my tests/tests/UNIT/test_unit.py": (
                "import unittest\nclass Unit(unittest.TestCase):\n    def test_unit(self): self.assertTrue(True)\n"
            ),
            "2_Github repos/3_WITH my tests/tests/API/test_api.py": (
                "import unittest\nclass API(unittest.TestCase):\n    def test_api(self): self.assertTrue(True)\n"
            ),
            "2_Github repos/3_WITH my tests/tests/E2E/test_e2e.py": (
                "import unittest\nclass E2E(unittest.TestCase):\n    def test_e2e(self): self.assertTrue(True)\n"
            ),
            "3_test traceability/run/workbook.html": "<html>traceability</html>",
            "4_LangGraph for this project/workflow.py": "graph = 'enforced'\n",
            "2_Github repos/4_upload to github/run/source.py": "copied = True\n",
            "2_Github repos/4_upload to github/run/commit-message.md": "Fix: tests\n",
            "0_Obsidian vault/session.md": "# Durable memory\n",
            ci_relative: "name: tests\n",
        }
        for relative, content in deliverables.items():
            path = self.project / Path(relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        ideal = swarm_work._requirement_artifact_evidence(
            self.project, contract, list(deliverables)
        )
        self.assertTrue(ideal["passed"], ideal)
        missing_ci = swarm_work._requirement_artifact_evidence(
            self.project, contract,
            [path for path in deliverables if path != ci_relative],
        )
        self.assertFalse(missing_ci["passed"], missing_ci)
        self.assertIn(ci_requirement["id"], missing_ci["unmet"])

    def test_provider_claims_cannot_terminalize_multi_artifact_goal_and_contract_survives_resume(self) -> None:
        goal = (
            "Create tests, a traceability workbook, LangGraph enforcement, "
            "an upload bundle, and lasting Obsidian memory"
        )
        made_test = False

        def answer(_config, route, _text, **kwargs):
            nonlocal made_test
            response_format = kwargs.get("response_format")
            if response_format is swarm_work.PLAN_FORMAT:
                value = {"contribution": "complete all artifacts", "message_to_lead": "done", "needs_files": [], "effect_paths": []}
            elif response_format is swarm_work.PLAN_REVIEW_FORMAT:
                value = {"contribution": "reviewed all", "message_to_lead": "ready", "needs_files": [], "effect_paths": [], "ready_to_execute": True, "remaining": []}
            elif response_format is swarm_work.WORK_VERIFICATION_FORMAT:
                value = {"goal_complete": True, "feedback": "Everything is complete.", "remaining": []}
            else:
                changes = []
                if not made_test:
                    made_test = True
                    changes = [{
                        "path": "tests/UNIT/test_claim.py",
                        "content": "import unittest\nclass Claim(unittest.TestCase):\n    def test_claim(self): self.assertTrue(True)\n",
                        "reason": "claimed full completion",
                    }]
                value = {"reply": "All requested artifacts are complete.", "changes": changes}
            return {"text": json.dumps(value), "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            first = swarm_work.work_together(self.config, self.board, "agent-1", goal)
            second = swarm_work.work_together(
                self.config, self.board, "agent-1", goal,
                resume_session_id=first["resume_token"],
            )
        self.assertFalse(first["goal_complete"])
        self.assertFalse(second["goal_complete"])
        self.assertIn(first["status"], {"needs_verification", "applied_unverified"})
        ledger = CollaborationLedger(
            self.config, "claude", "Claude", session_id=first["resume_token"]
        )
        contracts = [
            event["state"]["contract"] for event in ledger._read()
            if event.get("phase") == "requirement_contract"
        ]
        self.assertGreaterEqual(len(contracts), 2)
        self.assertTrue(contracts[-1].get("resumed_from_persisted_contract"))
        self.assertEqual(
            {one["id"] for one in contracts[0]["requirements"]},
            {one["id"] for one in contracts[-1]["requirements"]},
        )

    def test_langgraph_behavior_requires_trusted_requirement_probe_not_names(self) -> None:
        goal = "Implement and enforce a LangGraph workflow"
        (self.project / "langgraph_notes.md").write_text("placeholder", encoding="utf-8")
        (self.project / "test_langgraph_placeholder.py").write_text(
            "import unittest\nclass Placeholder(unittest.TestCase):\n"
            "    def test_langgraph_placeholder(self): self.assertTrue(True)\n",
            encoding="utf-8",
        )
        negative = swarm_work._run_selected_project_verification(
            self.config, self.project, self.board["projects"][0], goal,
            ["langgraph_notes.md", "test_langgraph_placeholder.py"], None,
        )
        self.assertEqual(negative["status"], "failed", negative)
        self.assertEqual(negative["basis"], "requirement_execution_evidence", negative)
        self.assertFalse(negative["requirement_evidence"]["execution"]["behavior_proof"])

        command = [
            sys.executable, "-c",
            "import json; print(json.dumps({'summary': {'executed': 1, 'failed': 0, 'langgraph_enforced': True}}))",
        ]
        project = copy.deepcopy(self.board["projects"][0])
        project["test_commands"] = [command]
        project["test_evidence_contracts"] = [{
            "command": command,
            "format": "json-stdout",
            "total_field": "summary.executed",
            "failed_field": "summary.failed",
            "requirement_probes": {
                "langgraph_enforcement": "summary.langgraph_enforced",
            },
        }]
        positive = swarm_work._run_selected_project_verification(
            self.config, self.project, project, goal, ["langgraph_notes.md"], None,
        )
        self.assertEqual(positive["status"], "passed", positive)
        self.assertEqual(
            positive["requirement_evidence"]["execution"]["proven_behavior_requirements"],
            ["langgraph_enforcement"],
        )

    def test_manifest_includes_empty_directory_after_the_old_eighty_sibling_boundary(self) -> None:
        for index in range(101):
            (self.project / f"folder-{index:03d}").mkdir()
        manifest = swarm_work._tree(self.project)
        self.assertIn("folder-100/", manifest)
        self.assertIn("101 directories", manifest)
        self.assertNotIn("tree truncated", manifest)

    def test_explicit_destination_contract_rejects_top_level_lookalike(self) -> None:
        allowed = ["2_Github repos/4_upload to github/20260827-14"]
        with self.assertRaisesRegex(Exception, "outside the explicit write destinations"):
            swarm_work._validated_changes(self.project, [{
                "path": "4_upload to github/commit-message.md",
                "content": "wrong nesting\n", "reason": "upload",
            }], allowed)
        changes = swarm_work._validated_changes(self.project, [{
            "path": "2_Github repos/4_upload to github/20260827-14/commit-message.md",
            "content": "correct nesting\n", "reason": "upload",
        }], allowed)
        self.assertEqual(changes[0].path, "2_Github repos/4_upload to github/20260827-14/commit-message.md")

    def test_plan_can_pause_for_user_and_resume_the_same_durable_session(self) -> None:
        resumed = False
        (self.project / "output").mkdir()

        def answer(_config, route, _text, **kwargs):
            response_format = kwargs.get("response_format")
            if response_format is swarm_work.PLAN_FORMAT:
                value = {"contribution": "plan", "message_to_lead": "plan", "needs_files": []}
            elif response_format is swarm_work.PLAN_REVIEW_FORMAT:
                value = {
                    "contribution": "plan", "message_to_lead": "ready" if resumed else "ask",
                    "needs_files": [], "ready_to_execute": resumed, "remaining": [],
                    "questions": [] if resumed else ["Which compatibility target is required?"],
                }
            elif response_format is swarm_work.WORK_VERIFICATION_FORMAT:
                value = {"goal_complete": True, "feedback": "Verified.", "remaining": []}
            else:
                value = {
                    "reply": "Implemented the answered target.",
                    "changes": [{"path": "output/target.txt", "content": "windows\n", "reason": "answered"}],
                }
            return {"text": json.dumps(value), "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            paused = swarm_work.work_together(
                self.config, self.board, "agent-1", "Create output/target.txt",
                allowed_write_roots=["output"],
            )
            with self.assertRaisesRegex(
                swarm_work.SwarmError, "Answer the paused questions"
            ):
                swarm_work.work_together(
                    self.config, self.board, "agent-1", "ignored on resume",
                    resume_session_id=paused["resume_token"],
                )
            resumed = True
            done = swarm_work.work_together(
                self.config, self.board, "agent-1", "ignored on resume",
                resume_session_id=paused["resume_token"],
                user_answers="Windows 11 is the compatibility target.",
            )

        self.assertEqual(paused["status"], "paused_for_user")
        self.assertFalse((self.project / "output" / "target.txt").exists() and not done["goal_complete"])
        self.assertEqual(done["collaboration_ledger"]["session_id"], paused["resume_token"])
        self.assertTrue(done["goal_complete"])
        self.assertEqual(done["allowed_write_roots"], ["output"])
        self.assertEqual((self.project / "output" / "target.txt").read_text(), "windows\n")

    def test_provider_pause_resumes_same_session_without_a_user_answer(self) -> None:
        provider_available = False

        def answer(_config, route, _text, **kwargs):
            response_format = kwargs.get("response_format")
            if (
                response_format is swarm_work.PLAN_FORMAT
                and route == "codex" and not provider_available
            ):
                raise HarnessError("provider temporarily unavailable")
            if response_format is swarm_work.PLAN_FORMAT:
                value = {
                    "contribution": "create the requested file",
                    "message_to_lead": "ready", "needs_files": [],
                    "effect_paths": ["provider-retry.txt"],
                }
            elif response_format is swarm_work.PLAN_REVIEW_FORMAT:
                value = {
                    "contribution": "reviewed", "message_to_lead": "ready",
                    "needs_files": [], "effect_paths": ["provider-retry.txt"],
                    "ready_to_execute": True, "remaining": [],
                }
            elif response_format is swarm_work.WORK_FORMAT:
                value = {
                    "reply": "created", "changes": ([{
                        "path": "provider-retry.txt", "content": "recovered\n",
                        "reason": "requested",
                    }] if route == "claude" else []),
                }
            else:
                value = {"goal_complete": True, "feedback": "verified", "remaining": []}
            return {"text": json.dumps(value), "milliseconds": 1, "model": route}

        verification = {
            "status": "passed", "basis": "selected_project", "commands": [],
            "reason": "test fixture", "requirement_evidence": {"execution": {"unmet": []}},
        }
        with mock.patch.object(chat, "ask_once", side_effect=answer), mock.patch.object(
            swarm_work, "_run_selected_project_verification", return_value=verification,
        ):
            with self.assertRaises(swarm_work.ResumableSwarmError) as stopped:
                swarm_work.work_together(
                    self.config, self.board, "agent-1", "Create provider-retry.txt"
                )
            self.assertEqual(stopped.exception.payload["status"], "paused_provider")
            token = stopped.exception.payload["resume_token"]
            provider_available = True
            resumed = swarm_work.work_together(
                self.config, self.board, "agent-1", "ignored on resume",
                resume_session_id=token,
            )

        self.assertTrue(resumed["goal_complete"], resumed)
        self.assertEqual(resumed["collaboration_ledger"]["session_id"], token)
        self.assertEqual(
            (self.project / "provider-retry.txt").read_text(encoding="utf-8"),
            "recovered\n",
        )
        events = CollaborationLedger(
            self.config, "claude", "Claude", session_id=token,
        )._read()
        self.assertTrue(any(
            event.get("phase") == "provider_recovery_resume" for event in events
        ))
        self.assertFalse(any(
            event.get("kind") == "user_answer" and not str(event.get("text") or "").strip()
            for event in events
        ))

    def test_same_route_and_model_reviewers_are_disclosed_as_correlated(self) -> None:
        result = swarm_work._review_correlation([
            ({"name": "Coder", "who": "claude"}, {"_model": "sonnet"}),
            ({"name": "Reviewer", "who": "claude"}, {"_model": "sonnet"}),
        ])
        self.assertFalse(result["independent"])
        self.assertIn("not independent", result["warning"])
        self.assertEqual(result["duplicates"][0]["agents"], ["Coder", "Reviewer"])

    def test_mutation_goal_cannot_complete_when_agents_make_no_effect(self) -> None:
        def answer(_config, route, _text, **kwargs):
            response_format = kwargs.get("response_format")
            if response_format is swarm_work.PLAN_FORMAT:
                value = {"contribution": "create", "message_to_lead": "do it", "needs_files": []}
            elif response_format is swarm_work.PLAN_REVIEW_FORMAT:
                value = {"contribution": "review", "message_to_lead": "ready", "needs_files": [], "ready_to_execute": True, "remaining": []}
            elif response_format is swarm_work.WORK_VERIFICATION_FORMAT:
                value = {"goal_complete": True, "feedback": "claimed complete", "remaining": []}
            else:
                value = {"reply": "claimed complete", "changes": []}
            return {"text": json.dumps(value), "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            result = swarm_work.work_together(self.config, self.board, "agent-1", "Create marker.txt")
        self.assertFalse(result["goal_complete"])
        self.assertFalse((self.project / "marker.txt").exists())
        self.assertIn("no project-file effect", result["deterministic_verification"]["reason"])

    def test_zero_test_output_never_passes_for_non_test_worded_goal(self) -> None:
        (self.project / "feature.py").write_text("enabled = True\n", encoding="utf-8")
        for output in (
            "No unit tests to run", "Tests: 0 total", "collected 0 items",
            "? example/pkg [no test files]", "TAP version 13\n1..0", "ok",
        ):
            with self.subTest(output=output):
                project = copy.deepcopy(self.board["projects"][0])
                project["test_commands"] = [[sys.executable, "-c", f"print({output!r})"]]
                result = swarm_work._run_selected_project_verification(
                    self.config, self.project, project,
                    "Implement requested feature", ["feature.py"], None,
                )
                self.assertEqual(result["status"], "failed")
                self.assertIn(result["basis"], {"selected_project", "positive_test_evidence"})

    def test_every_non_read_only_project_goal_requires_changed_state(self) -> None:
        (self.project / "parser.py").write_text("value = 1\n", encoding="utf-8")
        for verb in ("Repair", "Ensure", "Resolve", "Migrate", "Refactor", "Update", "Fix"):
            with self.subTest(verb=verb):
                result = swarm_work._run_selected_project_verification(
                    self.config, self.project, self.board["projects"][0],
                    f"{verb} parser.py", [], None,
                )
                self.assertEqual(result["basis"], "goal_effect")
                self.assertEqual(result["status"], "failed")
        read_only = swarm_work._goal_effect_evidence(
            self.project, "Read-only inspect parser.py", [],
        )
        self.assertTrue(read_only["passed"])

    def test_preservation_constraints_never_turn_mutation_goals_read_only(self) -> None:
        (self.project / "parser.py").write_text("value = 1\n", encoding="utf-8")
        mutating_goals = (
            "Fix parser.py; do not change unrelated files",
            "Refactor parser.py without changing its public API",
            "Update parser.py without changing behavior",
            "Review and explain parser.py, then fix it; do not change unrelated files",
            "Analyze parser.py and report status, then repair it without changing its API",
        )
        for goal in mutating_goals:
            with self.subTest(goal=goal):
                evidence = swarm_work._goal_effect_evidence(self.project, goal, [])
                self.assertFalse(evidence["passed"])
                self.assertEqual(evidence["intent"], "mutation")
                self.assertTrue(evidence["effect_required"])

    def test_positive_exception_imperatives_override_outer_prohibitions(self) -> None:
        (self.project / "parser.py").write_text("value = 1\n", encoding="utf-8")
        for goal in (
            "Do not modify the repository except to fix parser.py",
            "Never change the repository except by repairing parser.py",
            "Avoid editing files except when necessary to resolve parser.py",
            "Without changing unrelated code, update parser.py",
        ):
            with self.subTest(goal=goal):
                parsed = swarm_work._parse_goal_intent(goal)
                evidence = swarm_work._goal_effect_evidence(self.project, goal, [])
                self.assertEqual(parsed["intent"], "mutation")
                self.assertTrue(parsed["mutation_actions"])
                self.assertTrue(parsed["exceptions"] or "update" in parsed["mutation_actions"])
                self.assertFalse(evidence["passed"])

    def test_affirmative_informational_goals_are_read_only(self) -> None:
        (self.project / "parser.py").write_text("value = 1\n", encoding="utf-8")
        (self.project / "update.py").write_text("value = 2\n", encoding="utf-8")
        for goal in (
            "Review parser.py and explain the findings",
            "Analyze parser.py; report its status",
            "Inspect parser.py without making any changes",
            "Diagnose parser.py and update me on its status without changing any files",
            "Review update.py and report the status of the update",
        ):
            with self.subTest(goal=goal):
                evidence = swarm_work._goal_effect_evidence(self.project, goal, [])
                self.assertTrue(evidence["passed"])
                self.assertEqual(evidence["intent"], "read_only")
                self.assertFalse(evidence["effect_required"])

    def test_mutation_nouns_inside_read_only_imperatives_stay_read_only(self) -> None:
        (self.project / "parser.py").write_text("value = 1\n", encoding="utf-8")
        for goal in (
            "Verify the fix in parser.py without making changes",
            "Review parser.py and report whether the repair is correct",
            "Analyze whether a refactor of parser.py is necessary; do not make changes",
            "Explain the update and whether the change is safe without editing files",
        ):
            with self.subTest(goal=goal):
                parsed = swarm_work._parse_goal_intent(goal)
                evidence = swarm_work._goal_effect_evidence(self.project, goal, [])
                self.assertEqual(parsed["intent"], "read_only")
                self.assertEqual(parsed["mutation_actions"], [])
                self.assertTrue(parsed["read_only_actions"])
                self.assertTrue(evidence["passed"])

    def test_goal_intent_contrast_and_deliberation_grammar_matrix(self) -> None:
        (self.project / "parser.py").write_text("value = 1\n", encoding="utf-8")
        cases = (
            ("mutation", "Do not modify anything other than fixing parser.py"),
            ("mutation", "Never change files unless repairing parser.py is necessary"),
            ("mutation", "Do not alter unrelated code but fix parser.py"),
            ("mutation", "Leave the repository untouched save for repairing parser.py"),
            ("mutation", "Avoid edits apart from updating parser.py"),
            ("mutation", "Make no changes with the exception of resolving parser.py"),
            ("mutation", "Review only yet repair parser.py if it is broken"),
            ("mutation", "Keep everything unchanged aside from fixing parser.py"),
            ("mutation", "Do not alter files besides repairing parser.py"),
            ("mutation", "Make no edits excluding updating parser.py"),
            ("mutation", "Leave files untouched excepting a repair to parser.py"),
            ("mutation", "No changes with the sole exception of refactoring parser.py"),
            ("mutation", "Do not modify code outside of fixing parser.py"),
            ("mutation", "No edits bar resolving parser.py"),
            ("mutation", "Avoid changes barring a necessary repair to parser.py"),
            ("read_only", "Check whether to fix parser.py; do not make changes"),
            ("read_only", "Evaluate whether repairing parser.py would be appropriate without editing files"),
            ("read_only", "Decide if parser.py should be fixed and report the recommendation only"),
            ("read_only", "Analyze whether a change to parser.py is needed; never modify it"),
            ("read_only", "Review the proposed fix and determine whether to apply it; read-only"),
            ("read_only", "Review parser.py aside from explaining the findings; do not edit files"),
            ("read_only", "Check whether to fix parser.py besides evaluating the evidence; make no changes"),
            ("read_only", "Excluding any modifications, inspect parser.py and report status"),
            ("read_only", "Do not modify parser.py"),
            ("read_only", "Never change unrelated files"),
            ("project_work", "Parser.py behavior expectations"),
        )
        for expected, goal in cases:
            with self.subTest(goal=goal):
                parsed = swarm_work._parse_goal_intent(goal)
                evidence = swarm_work._goal_effect_evidence(self.project, goal, [])
                self.assertEqual(parsed["intent"], expected)
                self.assertEqual(evidence["passed"], expected == "read_only")
                if expected == "mutation":
                    self.assertTrue(parsed["mutation_actions"])
                elif expected == "read_only":
                    self.assertEqual(parsed["mutation_actions"], [])
                    self.assertTrue(
                        parsed["deliberative_mentions"]
                        or parsed["read_only_actions"]
                        or parsed["constraints"]
                    )
                else:
                    self.assertEqual(parsed["mutation_actions"], [])
                    self.assertEqual(parsed["read_only_actions"], [])
                    self.assertEqual(parsed["constraints"], [])

    def test_exception_action_noun_inflection_and_polarity_matrix(self) -> None:
        cases = (
            ("mutation", "Do not modify anything excluding repairs to parser.py"),
            ("mutation", "Do not modify anything excepting fixes to parser.py"),
            ("mutation", "Do not modify anything, excluding repairs to parser.py"),
            ("mutation", "Do not modify anything; excepting fixes to parser.py"),
            ("mutation", "Never change files. Except repairs to parser.py"),
            ("mutation", "Keep files unchanged aside from modifications to parser.py"),
            ("mutation", "No edits besides corrections to parser.py"),
            ("mutation", "Avoid changes apart from migrations of parser.py"),
            ("read_only", "Review parser.py, excluding modifying any files"),
            ("read_only", "Excluding updating dependencies, review parser.py without making changes"),
            ("read_only", "Inspect parser.py aside from changing any source files"),
            ("read_only", "Review the proposed repairs; never make changes"),
        )
        for expected, goal in cases:
            with self.subTest(goal=goal):
                parsed = swarm_work._parse_goal_intent(goal)
                self.assertEqual(parsed["intent"], expected, parsed)

    def test_discourse_connector_punctuation_preserves_only_prior_prohibition(self) -> None:
        (self.project / "parser.py").write_text("value = 1\n", encoding="utf-8")
        cases = (
            ("mutation", "Do not modify anything. However, repairs to parser.py are required"),
            ("mutation", "Do not modify anything; however, fixes to parser.py are required"),
            ("mutation", "Never change files. Nevertheless, updates to parser.py are required"),
            ("mutation", "Make no edits; nonetheless, repair parser.py"),
            ("read_only", "Do not modify anything. However, repairs are described in the report"),
            ("read_only", "Never edit files. However, fixes are only being reviewed"),
            ("read_only", "Review parser.py. However, repairs are described in the report"),
            ("read_only", "Inspect parser.py; nevertheless, the fixes are only being reviewed"),
            ("read_only", "Analyze parser.py. Nonetheless, report whether a repair is needed"),
        )
        for expected, goal in cases:
            with self.subTest(goal=goal):
                parsed = swarm_work._parse_goal_intent(goal)
                evidence = swarm_work._goal_effect_evidence(self.project, goal, [])
                self.assertEqual(expected, parsed["intent"], parsed)
                self.assertEqual(expected == "read_only", evidence["passed"], evidence)
                self.assertEqual(bool(parsed["mutation_actions"]), expected == "mutation", parsed)

    def test_missing_selected_runner_is_classified_without_crashing(self) -> None:
        project = copy.deepcopy(self.board["projects"][0])
        project["test_commands"] = [["definitely-missing-nexus-runner", "test"]]
        (self.project / "feature.py").write_text("enabled = True\n", encoding="utf-8")
        result = swarm_work._run_selected_project_verification(
            self.config, self.project, project, "Implement feature", ["feature.py"], None,
        )
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["basis"], "missing_runner")

    def test_empty_host_path_never_preempts_containment_owned_runners(self) -> None:
        (self.project / "feature.py").write_text("enabled = True\n", encoding="utf-8")

        def contained(_config, _root, command, **_kwargs):
            return {
                "argv": list(command), "cwd": ".", "exit_code": 0,
                "stdout": "Ran 1 test\nOK\n", "stderr": "", "duration_ms": 1,
                "timed_out": False, "output_truncated": False,
                "disposable_snapshot": True,
                "containment_profile": "bounded-test-containment",
            }

        cases = (
            ["python", "-m", "unittest", "discover"],
            ["python3", "-m", "unittest", "discover"],
            ["py", "-m", "unittest", "discover"],
            ["pytest", "-q"],
            ["py.test", "-q"],
            ["node", "--test"],
            ["nodejs", "--test"],
        )
        for command in cases:
            with self.subTest(command=command):
                project = copy.deepcopy(self.board["projects"][0])
                project["test_commands"] = [command]
                with mock.patch.object(
                    swarm_work.shutil, "which", return_value=None,
                ), mock.patch.object(
                    swarm_work, "_run_disposable_verification_command",
                    side_effect=contained,
                ) as run_contained:
                    result = swarm_work._run_selected_project_verification(
                        self.config, self.project, project,
                        "Create feature.py", ["feature.py"], None,
                    )
                self.assertNotEqual("missing_runner", result.get("basis"), result)
                run_contained.assert_called()

    def test_empty_host_path_leaves_node_availability_to_containment_broker(self) -> None:
        (self.project / "feature.py").write_text("enabled = True\n", encoding="utf-8")
        project = copy.deepcopy(self.board["projects"][0])
        project["test_commands"] = [["node", "--test"]]
        with mock.patch.object(
            swarm_work.shutil, "which", return_value=None,
        ), mock.patch.object(
            swarm_work, "discover_bundled_playwright_runtime", return_value=None,
        ):
            result = swarm_work._run_selected_project_verification(
                self.config, self.project, project,
                "Create feature.py", ["feature.py"], None,
            )
        self.assertEqual("unavailable", result["status"], result)
        self.assertEqual("verification_containment_unavailable", result["basis"], result)
        self.assertIn("Node", result["commands"][0]["stderr"])

    def test_prompt_summary_is_bounded_while_canonical_history_stays_full(self) -> None:
        contributions = [
            {
                "speaker_name": "Agent", "speaker_route": "route", "phase": "work",
                "text": (
                    "EARLY SEMANTIC SENTINEL: E2E browser coverage must remain a requirement. "
                    if index == 0 else str(index)
                ) + ("x" * 20_000),
            }
            for index in range(20)
        ]
        canonical = swarm_work._actual_conversation(contributions)
        prompt = swarm_work._prompt_conversation(contributions)
        self.assertGreater(len(canonical), 400_000)
        self.assertLessEqual(len(prompt), swarm_work.PROMPT_TRANSCRIPT_CHARACTERS)
        self.assertIn("canonical_sha256", prompt)
        self.assertIn("EARLY SEMANTIC SENTINEL", prompt)
        self.assertIn("E2E browser coverage must remain a requirement", prompt)
        limits = chat.effective_limits(self.config, "")
        self.assertEqual(
            limits["long_horizon_context"],
            {
                **chat.LONG_HORIZON_CONTEXT_POLICY,
                "phases": list(chat.LONG_HORIZON_CONTEXT_POLICY["phases"]),
                "note": limits["long_horizon_context"]["note"],
            },
        )
        self.assertEqual(
            limits["long_horizon_context"]["prompt_transcript_characters"],
            swarm_work.PROMPT_TRANSCRIPT_CHARACTERS,
        )
        checkpoint = swarm_work._prompt_summary_state(contributions)
        self.assertEqual(
            checkpoint["context_policy"], chat.LONG_HORIZON_CONTEXT_POLICY,
        )

    def test_prompt_summary_prioritizes_recent_omitted_decisions(self) -> None:
        contributions = [
            {
                "speaker_name": "Agent", "speaker_route": "route", "phase": "work",
                "text": f"old discussion {index} " + ("x" * 1_000),
            }
            for index in range(80)
        ]
        contributions[-2]["text"] = (
            "LATEST OMITTED BLOCKER SENTINEL: never publish until browser E2E passes. "
            + ("y" * 1_000)
        )

        summary = swarm_work._semantic_history_summary(contributions, 4_000)

        self.assertIn("LATEST OMITTED BLOCKER SENTINEL", summary)
        self.assertIn("MOST RECENT OMITTED EVIDENCE FIRST", summary)
        self.assertLessEqual(len(summary), 4_000)

    def test_every_long_horizon_phase_uses_the_disclosed_projection(self) -> None:
        source = Path(swarm_work.__file__).read_text(encoding="utf-8")
        self.assertNotIn("+ _actual_conversation(contributions)", source)
        self.assertGreaterEqual(
            source.count("+ _prompt_conversation(contributions)"), 5,
        )
        self.assertEqual(chat.LONG_HORIZON_CONTEXT_POLICY["phases"], [
            "team_discussion", "planning", "execution", "verification",
            "final_synthesis",
        ])

    def test_execution_snapshot_discloses_path_cap_and_invalid_utf8(self) -> None:
        paths = []
        for index in range(31):
            relative = f"snapshot-{index:02}.txt"
            (self.project / relative).write_text(str(index), encoding="utf-8")
            paths.append(relative)
        (self.project / paths[0]).write_bytes(b"before\xffafter")
        snapshot = swarm_work._file_snapshot(self.project, paths)
        self.assertIn("not valid UTF-8", snapshot)
        self.assertIn("1 path(s) omitted", snapshot)

    def test_changed_remaining_requirements_advance_without_optional_progress(self) -> None:
        guard = swarm_work._ProgressGuard()
        stopped = []
        for index in range(1, 6):
            state = swarm_work._canonical_progress_state(
                "agent", False, False, {"remaining": [f"Implement requirement {index}"]}
            )
            stopped.append(guard.stalled((state,)))
        self.assertEqual(stopped, [False] * 5)

    def test_read_only_verification_never_executes_discovered_project_code(self) -> None:
        marker = self.project / "DISCOVERED_CODE_RAN"
        (self.project / "test_untrusted.py").write_text(
            "from pathlib import Path\nPath('DISCOVERED_CODE_RAN').write_text('yes')\n", encoding="utf-8",
        )
        result = swarm_work._run_selected_project_verification(
            self.config, self.project, {"path": str(self.project)}, "Read-only inspect behavior", [], None,
        )
        self.assertEqual(result["basis"], "read_only_zero_write")
        self.assertEqual(result["commands"], [])
        self.assertFalse(marker.exists())

    def test_external_discovered_commands_need_visible_path_bound_approval_and_expire(self) -> None:
        (self.project / "pyproject.toml").write_text(
            "[project]\nname = 'approval-fixture'\nversion = '1.0.0'\n",
            encoding="utf-8",
        )
        (self.project / "feature.py").write_text("enabled = True\n", encoding="utf-8")
        project = {"id": "external-project", "path": str(self.project)}

        proposal = swarm_work.verification_command_approval(self.config, project)
        self.assertTrue(proposal["requires_approval"], proposal)
        self.assertFalse(proposal["approved"])
        self.assertEqual(
            proposal["commands"], [["python", "-m", "unittest", "discover"]]
        )
        blocked = swarm_work._run_selected_project_verification(
            self.config, self.project, project,
            "Create feature.py", ["feature.py"], None,
        )
        self.assertEqual(blocked["status"], "unavailable", blocked)
        self.assertEqual(blocked["basis"], "discovered_command_approval_required")
        self.assertEqual(blocked["proposed_commands"], proposal["commands"])

        project["approved_test_command_digest"] = proposal["approval_digest"]
        approved = swarm_work.verification_command_approval(self.config, project)
        self.assertTrue(approved["approved"], approved)
        # The engine-owned packaged/source Python staging does not depend on a
        # host PATH entry. This is a real contained execution, not a runner
        # mock: the approved external project can still become verified.
        with mock.patch.object(swarm_work.shutil, "which", return_value=None):
            passed = swarm_work._run_selected_project_verification(
                self.config, self.project, project,
                "Create feature.py", ["feature.py"], None,
            )
        self.assertEqual(passed["status"], "passed", passed)

        # The argv remains the same, but changing a command-selection manifest
        # changes the digest and expires the old approval before project code.
        (self.project / "pyproject.toml").write_text(
            "[project]\nname = 'approval-fixture'\nversion = '2.0.0'\n",
            encoding="utf-8",
        )
        stale = swarm_work.verification_command_approval(self.config, project)
        self.assertTrue(stale["stale_approval"], stale)
        self.assertNotEqual(stale["approval_digest"], proposal["approval_digest"])
        blocked_again = swarm_work._run_selected_project_verification(
            self.config, self.project, project,
            "Create feature.py", ["feature.py"], None,
        )
        self.assertEqual(
            blocked_again["basis"], "discovered_command_approval_required",
            blocked_again,
        )

    def test_cancel_during_deterministic_verification_rolls_back(self) -> None:
        def answer(_config, route, _text, **kwargs):
            response_format = kwargs.get("response_format")
            if response_format is swarm_work.PLAN_FORMAT:
                value = {"contribution": "create", "message_to_lead": "create", "needs_files": []}
            elif response_format is swarm_work.PLAN_REVIEW_FORMAT:
                value = {"contribution": "review", "message_to_lead": "ready", "needs_files": [], "ready_to_execute": True, "remaining": []}
            elif response_format is swarm_work.WORK_VERIFICATION_FORMAT:
                value = {"goal_complete": True, "feedback": "done", "remaining": []}
            else:
                value = {"reply": "done", "changes": [{"path": "cancelled.txt", "content": "partial\n", "reason": "requested"}]}
            return {"text": json.dumps(value), "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer), mock.patch.object(
            swarm_work, "_run_disposable_verification_command",
            side_effect=cancellation.ChatCancelled(cancellation.STOPPED_MESSAGE),
        ):
            with self.assertRaises(cancellation.ChatCancelled):
                swarm_work.work_together(self.config, self.board, "agent-1", "Create cancelled.txt")
        self.assertFalse((self.project / "cancelled.txt").exists())

    def test_execution_can_retrieve_new_context_before_proposing_changes(self) -> None:
        (self.project / "source.txt").write_text("needed evidence\n", encoding="utf-8")
        work_calls = 0
        saw_result = False

        def answer(_config, route, _text, **kwargs):
            nonlocal work_calls, saw_result
            response_format = kwargs.get("response_format")
            if response_format is swarm_work.PLAN_FORMAT:
                value = {"contribution": "inspect", "message_to_lead": "ready", "needs_files": []}
            elif response_format is swarm_work.PLAN_REVIEW_FORMAT:
                value = {"contribution": "review", "message_to_lead": "ready", "needs_files": [], "ready_to_execute": True, "remaining": []}
            elif response_format is swarm_work.WORK_FORMAT:
                work_calls += 1
                if work_calls == 1:
                    value = {"reply": "need context", "changes": [], "tool_calls": [{"call_id": "read-1", "name": "read_file", "arguments": {"path": "source.txt", "start_line": 1, "end_line": 20, "max_bytes": 2000}}]}
                else:
                    saw_result = saw_result or "needed evidence" in kwargs.get("context", "")
                    value = {"reply": "created", "changes": [{"path": "from-context.txt", "content": "done\n", "reason": "requested"}], "tool_calls": []}
            else:
                value = {"goal_complete": True, "feedback": "done", "remaining": []}
            return {"text": json.dumps(value), "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            result = swarm_work.work_together(self.config, self.board, "agent-1", "Create from-context.txt")
        self.assertTrue(result["goal_complete"])
        self.assertTrue(saw_result)
        ledger = next((self.root / ".harness" / "chats").glob("*.collaboration.jsonl"))
        self.assertIn('"phase":"context_tool_result"', ledger.read_text(encoding="utf-8"))

    def test_selected_verification_tool_uses_budget_idempotence_and_durable_replay(self) -> None:
        config = LoadedConfig(copy.deepcopy(self.config.data), self.root, [], {})
        config.data["workflow"]["max_tool_calls"] = 3
        ledger = CollaborationLedger(config, "claude", "tool-budget").begin(
            "Read-only inspect project", self.board["agents"], mode="project_work"
        )
        call = {"call_id": "verify-once", "name": "run_selected_verification", "arguments": {}}
        result = {
            "status": "passed", "basis": "selected_project", "commands": [],
            "reason": "test fixture",
        }
        with mock.patch.object(swarm_work, "_run_selected_project_verification", return_value=result) as run:
            first = swarm_work._ProjectContextTools(
                config, self.project, ledger, self.board["projects"][0],
                "Read-only inspect project", [], None,
            )
            one = first.execute("agent-1", call)
            first.close()
            resumed = swarm_work._ProjectContextTools(
                config, self.project, ledger, self.board["projects"][0],
                "Read-only inspect project", [], None,
            )
            two = resumed.execute("agent-1", call)
            resumed.execute("agent-1", {**call, "call_id": "verify-two"})
            with self.assertRaisesRegex(HarnessError, "tool call limit"):
                resumed.execute("agent-1", {**call, "call_id": "verify-three"})
            resumed.close()
        self.assertEqual(run.call_count, 1)
        self.assertFalse(one["replayed"])
        self.assertTrue(two["replayed"])

    def test_productive_context_epochs_exceed_twelve_calls_but_restart_does_not_mint_budget(self) -> None:
        (self.project / "source.txt").write_text("evidence\n", encoding="utf-8")
        config = LoadedConfig(copy.deepcopy(self.config.data), self.root, [], {})
        config.data["workflow"]["max_tool_calls"] = 16
        ledger = CollaborationLedger(config, "claude", "productive-epochs").begin(
            "Create implemented.txt after exploring the project", self.board["agents"], mode="project_work"
        )
        tools = swarm_work._ProjectContextTools(
            config, self.project, ledger, self.board["projects"][0],
            "Create implemented.txt after exploring the project", [], None,
        )
        saga = swarm_work._MutationSaga(self.project, ledger.session_id)
        try:
            for index in range(13):
                result = tools.execute("agent-1", {
                    "call_id": f"read-{index}", "name": "read_file",
                    "arguments": {"path": "source.txt", "start_line": 1, "end_line": 2, "max_bytes": 100},
                })
                self.assertEqual(result["status"], "ok")
            self.assertEqual(tools.disclosure()["epoch_calls_remaining"], 3)
            transaction_id = FileTransaction.new_transaction_id()
            saga.prepare(transaction_id)
            manifest = FileTransaction(self.project).apply([ChangePlan(
                path="implemented.txt", baseline_sha256=None,
                content="material result\n", reason="productive checkpoint",
            )], transaction_id=transaction_id)
            swarm_work._record_applied_transaction(
                ledger, saga, transaction_id, manifest,
            )
            self.assertTrue(tools.renew_after_progress([transaction_id]))
            self.assertEqual(tools.disclosure()["epoch"], 2)
            self.assertEqual(tools.disclosure()["epoch_calls_remaining"], 16)
        finally:
            tools.close()
        resumed = swarm_work._ProjectContextTools(
            config, self.project, ledger, self.board["projects"][0],
            "Create implemented.txt after exploring the project", [], None,
        )
        try:
            self.assertEqual(resumed.disclosure()["epoch"], 2)
            self.assertEqual(resumed.disclosure()["epoch_calls_remaining"], 16)
            for index in range(16):
                resumed.execute("agent-2", {
                    "call_id": f"epoch-2-{index}", "name": "read_file",
                    "arguments": {"path": "source.txt", "start_line": 1, "end_line": 2, "max_bytes": 100},
                })
        finally:
            resumed.close()
        restarted = swarm_work._ProjectContextTools(
            config, self.project, ledger, self.board["projects"][0],
            "Create implemented.txt after exploring the project", [], None,
        )
        try:
            self.assertEqual(restarted.disclosure()["epoch_calls_remaining"], 0)
            with self.assertRaisesRegex(HarnessError, "tool call limit"):
                restarted.execute("agent-1", {
                    "call_id": "restart-must-not-renew", "name": "read_file",
                    "arguments": {"path": "source.txt", "start_line": 1, "end_line": 2, "max_bytes": 100},
                })
            self.assertIn("restart/resume alone never renews", restarted.disclosure()["renewal_policy"])
        finally:
            restarted.close()
            saga.complete("test_complete")

    def test_context_epoch_renewal_requires_goal_relevant_live_net_digest_delta(self) -> None:
        parser = self.project / "parser.py"
        parser.write_text("value = 1\n", encoding="utf-8")
        ledger = CollaborationLedger(self.config, "claude", "semantic-epoch").begin(
            "Fix parser.py", self.board["agents"], mode="project_work"
        )
        tools = swarm_work._ProjectContextTools(
            self.config, self.project, ledger, self.board["projects"][0],
            "Fix parser.py", [], None,
        )
        saga = swarm_work._MutationSaga(self.project, ledger.session_id)

        def applied(plan: ChangePlan) -> tuple[str, dict]:
            transaction_id = FileTransaction.new_transaction_id()
            saga.prepare(transaction_id)
            manifest = FileTransaction(self.project).apply(
                [plan], transaction_id=transaction_id,
            )
            swarm_work._record_applied_transaction(
                ledger, saga, transaction_id, manifest,
            )
            return transaction_id, manifest

        try:
            forged = [{
                "path": "parser.py", "before_sha256": "0" * 64,
                "after_sha256": file_sha256(parser),
            }]
            self.assertFalse(tools.renew_after_progress(forged))
            self.assertFalse(tools.renew_after_progress(["1700000000-deadbeef00"]))

            unrelated_id, unrelated = applied(ChangePlan(
                path="unrelated.tmp", baseline_sha256=None,
                content="churn\n", reason="unrelated churn",
            ))
            self.assertFalse(tools.renew_after_progress([unrelated_id]))

            same = parser.read_bytes()
            no_op_id, no_op = applied(ChangePlan(
                path="parser.py", baseline_sha256=file_sha256(parser),
                content=same, reason="no-op claim",
            ))
            self.assertFalse(tools.renew_after_progress([no_op_id]))

            original_bytes = parser.read_bytes()
            original_hash = file_sha256(parser)
            changed_id, changed = applied(ChangePlan(
                path="parser.py", baseline_sha256=original_hash,
                content=original_bytes.replace(b"1", b"2"), reason="real fix",
            ))
            self.assertTrue(tools.renew_after_progress([changed_id]))
            self.assertEqual(tools.epoch, 2)
            self.assertFalse(tools.renew_after_progress([changed_id]))

            manifest_path = (
                self.project / ".harness" / "backups" / changed_id / "manifest.json"
            )
            authentic_manifest = manifest_path.read_text(encoding="utf-8")
            tampered = json.loads(authentic_manifest)
            tampered["changes"][0]["reason"] = "tampered claim"
            manifest_path.write_text(json.dumps(tampered), encoding="utf-8")
            try:
                self.assertFalse(tools.renew_after_progress([changed_id]))
            finally:
                manifest_path.write_text(authentic_manifest, encoding="utf-8")

            other_ledger = CollaborationLedger(
                self.config, "claude", "cross-session-epoch"
            ).begin("Fix parser.py", self.board["agents"], mode="project_work")
            other_tools = swarm_work._ProjectContextTools(
                self.config, self.project, other_ledger, self.board["projects"][0],
                "Fix parser.py", [], None,
            )
            try:
                self.assertFalse(other_tools.renew_after_progress([changed_id]))
            finally:
                other_tools.close()

            reverted_id, reverted = applied(ChangePlan(
                path="parser.py", baseline_sha256=file_sha256(parser),
                content=original_bytes, reason="revert",
            ))
            self.assertFalse(tools.renew_after_progress([reverted_id]))
            self.assertEqual(tools.epoch, 2)
        finally:
            tools.close()

        reopened = swarm_work._ProjectContextTools(
            self.config, self.project, ledger, self.board["projects"][0],
            "Fix parser.py", [], None,
        )
        try:
            self.assertEqual(reopened.epoch, 2)
            self.assertFalse(reopened.renew_after_progress([reverted_id]))
            self.assertEqual(reopened.epoch, 2)
        finally:
            reopened.close()
            saga.complete("test_complete")

    def test_context_tool_lifetime_byte_ceiling_bounds_current_result_and_persists_rejection(self) -> None:
        (self.project / "bytes.txt").write_text("bounded evidence\n" * 20, encoding="utf-8")
        call = {
            "call_id": "bytes", "name": "read_file",
            "arguments": {"path": "bytes.txt", "start_line": 1, "end_line": 30, "max_bytes": 4096},
        }

        def opened(label: str) -> tuple[CollaborationLedger, object]:
            ledger = CollaborationLedger(self.config, "claude", label).begin(
                "Review bytes.txt without making changes", self.board["agents"], mode="project_work"
            )
            return ledger, swarm_work._ProjectContextTools(
                self.config, self.project, ledger, self.board["projects"][0],
                "Review bytes.txt without making changes", [], None,
            )

        baseline_ledger, baseline = opened("byte-size")
        try:
            expected = baseline.execute("agent-1", call)["content_bytes"]
        finally:
            baseline.close()
        self.assertGreater(expected, 1)

        for remaining in (expected + 1, expected):
            ledger, tools = opened(f"byte-ok-{remaining}")
            tools.absolute_byte_limit = remaining
            try:
                result = tools.execute("agent-1", call)
                self.assertFalse(result["truncated"], result)
                self.assertLessEqual(tools.session.total_bytes, remaining)
            finally:
                tools.close()

        (self.project / "large-bytes.txt").write_text("x" * 120_000, encoding="utf-8")
        per_call_ledger, per_call = opened("byte-per-call")
        per_call.absolute_byte_limit = 100_000
        per_call.session.per_call_bytes = 32_000
        try:
            bounded = per_call.execute("agent-1", {
                "call_id": "large-per-call", "name": "read_file",
                "arguments": {
                    "path": "large-bytes.txt", "start_line": 1,
                    "end_line": 2, "max_bytes": 32_000,
                },
            })
            self.assertTrue(bounded["truncated"], bounded)
            self.assertEqual(bounded["status"], "ok", bounded)
            self.assertEqual(bounded["content_bytes"], 32_000)
            self.assertFalse(any(
                event.get("phase") == "context_tool_absolute_limit_rejected"
                for event in per_call_ledger._read()
            ))
        finally:
            per_call.close()

        over_ledger, over = opened("byte-over")
        over.absolute_byte_limit = expected - 1
        try:
            with self.assertRaisesRegex(HarnessError, "during this result"):
                over.execute("agent-1", call)
            budget = next(
                event["state"] for event in reversed(over_ledger._read())
                if event.get("phase") == "context_tool_budget"
            )
            self.assertEqual(budget["budget"]["calls"], 1)
            self.assertEqual(budget["budget"]["total_bytes"], expected - 1)
            self.assertTrue(any(
                event.get("phase") == "context_tool_absolute_limit_rejected"
                for event in over_ledger._read()
            ))
        finally:
            over.close()

    def test_context_tool_execution_budget_ignores_idle_time_and_process_downtime(self) -> None:
        now = [100.0]
        clock = lambda: now[0]
        budget = swarm_work._SwarmToolExecutionBudget(20, clock=clock)

        # Provider/model thinking and any other time between tool calls are not
        # active accounting intervals.
        now[0] += 600
        self.assertEqual(budget.remaining_seconds("after provider wait"), 20)
        budget.begin_tool_execution()
        now[0] += 3.25
        budget.check("during a tool")
        budget.finish_tool_execution()
        state = budget.budget_state()
        self.assertAlmostEqual(state["consumed_seconds"], 3.25)
        self.assertAlmostEqual(state["remaining_seconds"], 16.75)

        # A restart restores consumed active time, not an absolute timestamp.
        now[0] += 86_400
        resumed = swarm_work._SwarmToolExecutionBudget(20, clock=clock)
        resumed.restore_budget_state(state)
        self.assertAlmostEqual(resumed.remaining_seconds("after process downtime"), 16.75)
        resumed.begin_tool_execution()
        now[0] += 17
        with self.assertRaisesRegex(
            swarm_work.ContextToolBudgetExhausted, "execution budget exhausted"
        ):
            resumed.check("during a long tool")
        resumed.finish_tool_execution()
        self.assertTrue(resumed.budget_state()["remaining_seconds"] == 0)
        resumed.reset_by_user()
        self.assertEqual(resumed.remaining_seconds("after explicit reset"), 20)

        unlimited = swarm_work._SwarmToolExecutionBudget(0, clock=clock)
        unlimited.begin_tool_execution()
        now[0] += 1_000_000
        unlimited.finish_tool_execution()
        self.assertIsNone(unlimited.budget_state()["remaining_seconds"])
        unlimited.check("after an unlimited active interval")

    def test_context_tool_execution_budget_persists_and_has_explicit_user_reset(self) -> None:
        config = LoadedConfig(copy.deepcopy(self.config.data), self.root, [], {})
        config.data["workflow"]["context_tool_execution_seconds"] = 20
        ledger = CollaborationLedger(config, "claude", "time-budget").begin(
            "Inspect the project", self.board["agents"], mode="project_work"
        )
        tools = swarm_work._ProjectContextTools(
            config, self.project, ledger, self.board["projects"][0],
            "Inspect the project", [], None,
        )
        tools.execution_budget.consumed_seconds = 4.5
        tools._record_budget()
        tools.close()

        reopened = swarm_work._ProjectContextTools(
            config, self.project, ledger, self.board["projects"][0],
            "Inspect the project", [], None,
        )
        try:
            self.assertAlmostEqual(
                reopened.disclosure()["tool_execution_remaining_seconds"], 15.5
            )
        finally:
            reopened.close()
        reset = swarm_work._ProjectContextTools(
            config, self.project, ledger, self.board["projects"][0],
            "Inspect the project", [], None, reset_execution_budget=True,
        )
        try:
            disclosed = reset.disclosure()
            self.assertEqual(disclosed["tool_execution_consumed_seconds"], 0)
            self.assertEqual(disclosed["tool_execution_remaining_seconds"], 20)
            self.assertTrue(any(
                event.get("phase") == "context_tool_budget_reset"
                for event in ledger._read()
            ))
        finally:
            reset.close()

    def test_context_tool_exhaustion_is_not_classified_as_provider_outage(self) -> None:
        ledger = CollaborationLedger(self.config, "claude", "tool-pause").begin(
            "Inspect the project", self.board["agents"], mode="project_work"
        )
        budget = {
            "tool_execution_mode": "configured",
            "tool_execution_ceiling_seconds": 10.0,
            "tool_execution_consumed_seconds": 10.0,
            "tool_execution_remaining_seconds": 0.0,
            "tool_execution_exhausted": True,
            "summary": "No tool execution time remains.",
        }
        with self.assertRaises(swarm_work.ResumableSwarmError) as paused:
            swarm_work._pause_context_tool_budget(
                ledger, budget, "execution",
                checkpoint={"allowed_write_roots": [], "write_scope_restricted": False},
                cause=swarm_work.ContextToolBudgetExhausted("spent"),
            )
        self.assertEqual(paused.exception.payload["status"], "paused_tool_budget")
        self.assertEqual(
            paused.exception.payload["stopped_because"],
            "context_tool_budget_exhausted",
        )
        phases = {event.get("phase") for event in ledger._read()}
        self.assertIn("context_tool_budget_exhausted", phases)
        self.assertNotIn("provider_transport_failure", phases)

    def test_selected_verification_is_cancelled_at_context_tool_execution_budget(self) -> None:
        marker = self.project / "deadline-child.txt"
        child = (
            "import time,pathlib; time.sleep(.55); "
            "pathlib.Path('deadline-child.txt').write_text('survived', encoding='utf-8')"
        )
        parent = (
            "import subprocess,sys,time; "
            f"subprocess.Popen([sys.executable, '-c', {child!r}]); "
            "time.sleep(.8); print('Ran 1 test')"
        )
        project = copy.deepcopy(self.board["projects"][0])
        project["test_commands"] = [[sys.executable, "-c", parent]]
        ledger = CollaborationLedger(self.config, "claude", "deadline").begin(
            "Inspect selected verification status", self.board["agents"], mode="project_work"
        )
        (self.project / "parser.py").write_text("value = 1\n", encoding="utf-8")
        tools = swarm_work._ProjectContextTools(
            self.config, self.project, ledger, project,
            "Update parser.py", ["parser.py"], None,
        )
        tools.execution_budget.shorten(0.150)
        started = time.monotonic()
        try:
            try:
                returned = tools.execute("agent-1", {
                    "call_id": "deadline-verification",
                    "name": "run_selected_verification",
                    "arguments": {},
                })
            except HarnessError as exc:
                self.assertIn("execution budget exhausted", str(exc))
            else:
                self.fail(f"verification returned instead of reporting its tool budget: {returned!r}")
        finally:
            tools.close()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.900)
        time.sleep(0.650)
        self.assertFalse(marker.exists(), "selected-verification process tree survived its tool deadline")
        budget_events = [
            event for event in ledger._read()
            if event.get("phase") == "context_tool_budget"
        ]
        self.assertTrue(budget_events)
        self.assertEqual(budget_events[-1]["state"]["budget"]["calls"], 1)
        self.assertIn("tool_execution_budget", budget_events[-1]["state"])
        resumed = swarm_work._ProjectContextTools(
            self.config, self.project, ledger, project,
            "Update parser.py", ["parser.py"], None,
        )
        try:
            self.assertEqual(resumed.session.calls, 1)
            self.assertTrue(resumed.execution_budget.unlimited)
            resumed.execution_budget.check("after resume and process downtime")
        finally:
            resumed.close()

    def test_cancelled_context_tool_call_persists_consumed_budget_and_reraises(self) -> None:
        ledger = CollaborationLedger(self.config, "claude", "cancel-budget").begin(
            "Inspect selected verification status", self.board["agents"], mode="project_work"
        )
        tools = swarm_work._ProjectContextTools(
            self.config, self.project, ledger, self.board["projects"][0],
            "Inspect selected verification status", [], None,
        )
        try:
            with mock.patch.object(
                swarm_work, "_run_selected_project_verification",
                side_effect=cancellation.ChatCancelled("Stopped by you."),
            ):
                with self.assertRaises(cancellation.ChatCancelled):
                    tools.execute("agent-1", {
                        "call_id": "cancelled-verification",
                        "name": "run_selected_verification",
                        "arguments": {},
                    })
        finally:
            tools.close()
        budgets = [
            event for event in ledger._read()
            if event.get("phase") == "context_tool_budget"
        ]
        self.assertEqual(budgets[-1]["state"]["budget"]["calls"], 1)

    def test_search_preparation_exceptions_are_counted_persisted_and_restored(self) -> None:
        for error in (
            cancellation.ChatCancelled("Stopped by you."),
            swarm_work.ContextToolBudgetExhausted(
                "Project context-tool execution budget exhausted during indexing"
            ),
        ):
            with self.subTest(error=type(error).__name__):
                config = LoadedConfig(copy.deepcopy(self.config.data), self.root, [], {})
                config.data["workflow"]["max_tool_calls"] = 1
                config.data["workflow"]["context_tool_execution_seconds"] = 30
                ledger = CollaborationLedger(config, "claude", type(error).__name__).begin(
                    "Inspect project", self.board["agents"], mode="project_work"
                )
                tools = swarm_work._ProjectContextTools(
                    config, self.project, ledger, self.board["projects"][0],
                    "Inspect project", [], None,
                )
                original_budget = tools.execution_budget.budget_state()
                try:
                    with mock.patch.object(swarm_work.WorkspaceIndexer, "scan", side_effect=error):
                        with self.assertRaises(type(error)):
                            tools.execute("agent-1", {
                                "call_id": "search-preparation",
                                "name": "search_workspace",
                                "arguments": {"query": "parser", "max_results": 5},
                            })
                finally:
                    tools.close()
                budgets = [
                    event for event in ledger._read()
                    if event.get("phase") == "context_tool_budget"
                ]
                self.assertEqual(budgets[-1]["state"]["budget"]["calls"], 1)
                persisted_budget = budgets[-1]["state"]["tool_execution_budget"]
                self.assertGreaterEqual(
                    persisted_budget["consumed_seconds"],
                    original_budget["consumed_seconds"],
                )
                self.assertGreater(persisted_budget["remaining_seconds"], 0)

                resumed = swarm_work._ProjectContextTools(
                    config, self.project, ledger, self.board["projects"][0],
                    "Inspect project", [], None,
                )
                try:
                    self.assertEqual(resumed.session.calls, 1)
                    restored = resumed.execution_budget.budget_state()
                    self.assertAlmostEqual(
                        restored["consumed_seconds"],
                        persisted_budget["consumed_seconds"],
                        delta=0.01,
                    )
                    self.assertAlmostEqual(
                        restored["remaining_seconds"],
                        persisted_budget["remaining_seconds"],
                        delta=0.01,
                    )
                    with self.assertRaisesRegex(HarnessError, "tool call limit"):
                        resumed.execute("agent-1", {
                            "call_id": "search-after-reopen",
                            "name": "search_workspace",
                            "arguments": {"query": "parser", "max_results": 5},
                        })
                finally:
                    resumed.close()

    def test_successful_search_preparation_is_counted_once_without_double_call(self) -> None:
        ledger = CollaborationLedger(self.config, "claude", "search-once").begin(
            "Inspect project", self.board["agents"], mode="project_work"
        )
        tools = swarm_work._ProjectContextTools(
            self.config, self.project, ledger, self.board["projects"][0],
            "Inspect project", [], None,
        )
        try:
            with mock.patch.object(swarm_work.WorkspaceIndexer, "scan", return_value={}) as scan:
                first = tools.execute("agent-1", {
                    "call_id": "search-one", "name": "search_workspace",
                    "arguments": {"query": "parser", "max_results": 5},
                })
                second = tools.execute("agent-1", {
                    "call_id": "search-two", "name": "search_workspace",
                    "arguments": {"query": "different", "max_results": 5},
                })
            self.assertEqual(first["status"], "ok")
            self.assertEqual(second["status"], "ok")
            self.assertEqual(scan.call_count, 1)
            self.assertEqual(tools.session.calls, 2)
        finally:
            tools.close()

    def test_goal_spec_speech_acts_compile_action_and_information_authority(self) -> None:
        actions = (
            "Can you update parser.py?", "Could you fix parser.py?",
            "Would you please fix parser.py?", "Would you mind fixing parser.py?",
            "Can you create report.md?", "Parser.py needs updating",
            "I need parser.py updated", "I want parser.py fixed",
            "Please have parser.py updated", "Parser.py should be updated",
            "It would be great if you updated parser.py",
        )
        information = (
            "Can the tool update parser.py?", "How do I update parser.py?",
            "Explain how to update parser.py", "Should I update parser.py?",
            "Should parser.py be refactored?", "What would updating parser.py change?",
            "Is it necessary to update parser.py?", "Do we need to update parser.py?",
            "Please tell me if parser.py should be updated",
        )
        for goal in actions:
            with self.subTest(action=goal):
                spec = swarm_work._compile_goal_spec(self.project, goal)
                self.assertEqual("SCOPED", spec["write_policy"]["mode"], spec)
                self.assertTrue(spec["write_policy"]["grants"], spec)
        for goal in information:
            with self.subTest(information=goal):
                spec = swarm_work._compile_goal_spec(self.project, goal)
                self.assertEqual("DENY_ALL", spec["write_policy"]["mode"], spec)
                self.assertEqual([], spec["write_policy"]["grants"], spec)

    def test_goal_spec_operation_frames_preserve_directional_path_roles(self) -> None:
        cases = (
            ("Consult ref.md before updating target.py", ["target.py"], ["ref.md"]),
            ("Update target.py using ref.md", ["target.py"], ["ref.md"]),
            ("Update target.py according to ref.md", ["target.py"], ["ref.md"]),
            ("Preserve README.md unchanged while updating app.py", ["app.py"], ["README.md"]),
            ("Update app.py but do not touch README.md", ["app.py"], ["README.md"]),
            ("Move old file.md to archive/old file.md", ["old file.md", "archive/old file.md"], []),
            ("Copy source.md to dest.md", ["dest.md"], ["source.md"]),
            ("Copy dest.md from source.md", ["dest.md"], ["source.md"]),
            ("Replace contents of target.md with source.md", ["target.md"], ["source.md"]),
            ("Replace source.md in target.md", ["target.md"], ["source.md"]),
            ("Rename old.md as new.md", ["old.md", "new.md"], []),
            ("Do not change anything; yet parser.py requires repair", ["parser.py"], []),
            ("Do not change anything; still parser.py requires repair", ["parser.py"], []),
        )
        for goal, effects, protected in cases:
            with self.subTest(goal=goal):
                roles = swarm_work._goal_path_roles(goal)
                self.assertEqual(effects, roles["effects"], roles)
                self.assertEqual(protected, roles["protected"], roles)

    def test_unsafe_wrapped_paths_fail_before_any_provider_call(self) -> None:
        unsafe = (
            '../outside.md', '..\\outside.md', '"../outside.md"',
            '`../outside.md`', '(../outside.md)', 'dir//file.md',
            'file.md:stream', 'C:relative.md',
        )
        for candidate in unsafe:
            with self.subTest(candidate=candidate), mock.patch.object(chat, "ask_once") as ask:
                with self.assertRaisesRegex(HarnessError, "Unsafe explicit project path"):
                    swarm_work.work_together(
                        self.config, self.board, "agent-1", f"Update {candidate}"
                    )
                ask.assert_not_called()

    def test_read_only_work_rejects_provider_mutations_and_proves_zero_write(self) -> None:
        (self.project / "parser.py").write_text("value = 1\n", encoding="utf-8")
        before, _manifest = swarm_work._project_tree_merkle(self.project)

        def answer(_config, route, _text, **kwargs):
            response_format = kwargs.get("response_format")
            if response_format is swarm_work.PLAN_FORMAT:
                value = {"contribution": "review", "message_to_lead": "review", "needs_files": []}
            elif response_format is swarm_work.PLAN_REVIEW_FORMAT:
                value = {
                    "contribution": "review", "message_to_lead": "ready", "needs_files": [],
                    "ready_to_execute": True, "remaining": [],
                }
            elif response_format is swarm_work.WORK_VERIFICATION_FORMAT:
                value = {"goal_complete": True, "feedback": "review complete", "remaining": []}
            else:
                value = {"reply": "reviewed", "changes": [{
                    "path": "unrelated.py", "content": "forbidden = True\n", "reason": "provider tried",
                }]}
            return {"text": json.dumps(value), "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            result = swarm_work.work_together(
                self.config, self.board, "agent-1", "Review parser.py without making changes"
            )
        after, _manifest = swarm_work._project_tree_merkle(self.project)
        self.assertTrue(result["goal_complete"], result)
        self.assertEqual([], result["transaction_ids"])
        self.assertFalse((self.project / "unrelated.py").exists())
        self.assertEqual(before, after)

    def test_behavior_contract_distinguishes_runtime_outcomes_from_file_state(self) -> None:
        behaviors = (
            "Fix retry handling", "Change parser so invalid input is rejected",
            "Add Unicode support", "Prevent malformed JSON from crashing",
            "Support retries", "Handle timeouts", "Resolve the deadlock",
            "Make failed requests retry", "Correct timeout logic", "Address auth failures",
            "Add functionality to create a report",
        )
        file_state = (
            "Ensure README.md is updated", "Create report.md",
            "Move old.md to new.md", "Rename old.md as new.md",
        )
        for goal in behaviors:
            with self.subTest(behavior=goal):
                contract = swarm_work._derive_requirement_contract(self.project, goal)
                self.assertTrue(any(
                    one["kind"] == "behavior" for one in contract["requirements"]
                ), contract)
        for goal in file_state:
            with self.subTest(file_state=goal):
                contract = swarm_work._derive_requirement_contract(self.project, goal)
                self.assertFalse(any(
                    one["kind"] in {"behavior", "behavior_preservation"}
                    for one in contract["requirements"]
                ), contract)
        preservation = swarm_work._derive_requirement_contract(
            self.project, "Refactor parser.py without changing behavior"
        )
        self.assertTrue(any(
            one["kind"] == "behavior_preservation" for one in preservation["requirements"]
        ), preservation)

    def test_fresh_causal_test_receipt_proves_behavior_and_rejects_stale_content(self) -> None:
        goal = "Fix parse in calc.py so empty input is rejected"
        (self.project / "calc.py").write_text(
            "def parse(value):\n    return value\n", encoding="utf-8"
        )
        ledger = CollaborationLedger(self.config, "claude", "causal-proof").begin(
            goal, self.board["agents"], mode="project_work"
        )
        saga = swarm_work._MutationSaga(self.project, ledger.session_id)
        transaction_id = FileTransaction.new_transaction_id()
        saga.prepare(transaction_id)
        manifest = FileTransaction(self.project).apply([
            ChangePlan(
                path="calc.py", baseline_sha256=file_sha256(self.project / "calc.py"),
                content=(
                    "def parse(value):\n"
                    "    if value == '':\n        raise ValueError('empty input')\n"
                    "    return value\n"
                ), reason="reject empty input",
            ),
            ChangePlan(
                path="test_calc_rejects_empty_input.py", baseline_sha256=None,
                content=(
                    "import unittest\nfrom calc import parse\n"
                    "class CalcRejectsEmptyInput(unittest.TestCase):\n"
                    "    def test_calc_rejects_empty_input(self):\n"
                    "        with self.assertRaises(ValueError): parse('')\n"
                ), reason="fresh regression witness",
            ),
        ], transaction_id=transaction_id)
        swarm_work._record_applied_transaction(ledger, saga, transaction_id, manifest)
        contract = swarm_work._derive_requirement_contract(self.project, goal)
        result = swarm_work._run_selected_project_verification(
            self.config, self.project, self.board["projects"][0], goal,
            ["calc.py", "test_calc_rejects_empty_input.py"], None,
            requirement_contract=contract,
            verification_session_id=ledger.session_id,
            transaction_ids=[transaction_id],
        )
        self.assertEqual("passed", result["status"], result)
        receipts = result["requirement_evidence"]["execution"]["causal_receipts"]
        self.assertEqual(1, len(receipts), result)
        receipt = receipts[0]
        for field in (
            "session_id", "run_nonce", "goal_sha256", "requirement_id",
            "command_digest", "toolchain_digest", "production_content",
            "test_content", "counterfactual_output_sha256", "coverage_hits",
        ):
            self.assertTrue(receipt.get(field), receipt)

        (self.project / "calc.py").write_text("def parse(value): return 'tampered'\n", encoding="utf-8")
        stale = swarm_work._executed_requirement_evidence(
            contract,
            [one["argv"] for one in result["commands"]], result["commands"],
            result["verification_analysis"], self.board["projects"][0], {},
            ["calc.py", "test_calc_rejects_empty_input.py"],
            causal_receipts=receipts, root=self.project,
        )
        self.assertFalse(stale["passed"], stale)

    def test_actual_playwright_e2e_receipt_uses_fresh_v8_causal_trace(self) -> None:
        playwright_cli = Path.cwd() / "node_modules" / "playwright" / "cli.js"
        playwright_package = Path.cwd() / "node_modules" / "playwright" / "test.js"
        if not playwright_cli.is_file():
            self.skipTest("repository Playwright runtime is not installed")
        goal = "Fix server.js so E2E invalid input is rejected"
        (self.project / "server.js").write_text(
            "exports.parse = value => value;\n", encoding="utf-8"
        )
        ledger = CollaborationLedger(self.config, "claude", "playwright-causal").begin(
            goal, self.board["agents"], mode="project_work"
        )
        saga = swarm_work._MutationSaga(self.project, ledger.session_id)
        transaction_id = FileTransaction.new_transaction_id()
        saga.prepare(transaction_id)
        test_relative = "tests/E2E/server-invalid.spec.js"
        manifest = FileTransaction(self.project).apply([
            ChangePlan(
                path="server.js", baseline_sha256=file_sha256(self.project / "server.js"),
                content=(
                    "exports.parse = value => {\n"
                    "  if (value === '') throw new Error('invalid input');\n"
                    "  return value;\n};\n"
                ), reason="reject invalid input",
            ),
            ChangePlan(
                path=test_relative, baseline_sha256=None,
                content=(
                    f"const {{ test, expect }} = require({json.dumps(str(playwright_package))});\n"
                    "const { parse } = require('../../server.js');\n"
                    "test('server E2E rejects invalid input', async () => {\n"
                    "  expect(() => parse('')).toThrow('invalid input');\n"
                    "});\n"
                ), reason="fresh Playwright E2E regression",
            ),
        ], transaction_id=transaction_id)
        swarm_work._record_applied_transaction(ledger, saga, transaction_id, manifest)
        project = copy.deepcopy(self.board["projects"][0])
        project["test_commands"] = [[
            shutil.which("node") or "node", str(playwright_cli), "test",
        ]]
        contract = swarm_work._derive_requirement_contract(self.project, goal)
        result = swarm_work._run_selected_project_verification(
            self.config, self.project, project, goal, ["server.js", test_relative], None,
            requirement_contract=contract,
            verification_session_id=ledger.session_id,
            transaction_ids=[transaction_id],
        )
        # A Playwright-labelled Node unit assertion is not an E2E witness.
        # Nexus must not launch project-authored workers or mint browser proof
        # when there is no route/action/DOM scenario to replay itself.
        self.assertEqual("unavailable", result["status"], result)
        self.assertEqual("verification_containment_unavailable", result["basis"])
        self.assertIn("engine-provable relative route", result["commands"][0]["stderr"])

    def test_mainstream_causal_adapters_narrow_trusted_runner_commands(self) -> None:
        node = shutil.which("node") or "node"
        cases = (
            ([node, "node_modules/playwright/cli.js", "test"], "tests/E2E/a.spec.js"),
            ([node, "node_modules/vitest/vitest.mjs", "run"], "tests/unit/a.test.js"),
            ([node, "node_modules/jest/bin/jest.js"], "tests/unit/a.test.js"),
            (["go", "test", "./..."], "pkg/parser/parser_test.go"),
            (["cargo", "test"], "tests/parser_test.rs"),
            (["dotnet", "test"], "tests/ParserTests.cs"),
            (["mvn", "test"], "src/test/java/ParserTest.java"),
            (["gradlew.bat", "test"], "src/test/java/ParserTest.java"),
        )
        for command, relative in cases:
            with self.subTest(command=command):
                self.assertIsNotNone(
                    swarm_work._level_probe_command(command, [relative])
                )
        with tempfile.TemporaryDirectory() as temporary:
            cover = Path(temporary)
            for command in cases[:4]:
                with self.subTest(trace=command[0]):
                    self.assertIsNotNone(swarm_work._trace_probe_command(
                        command[0], [command[1]], cover / Path(command[1]).stem, self.project
                    ))

    def test_progress_guard_uses_exact_state_and_conservative_cycle_thresholds(self) -> None:
        guard = swarm_work._ProgressGuard()
        states = [
            (swarm_work._canonical_progress_state(
                "agent", False, False, {"remaining": [f"requirement-{index}"]}
            ),)
            for index in range(8)
        ]
        self.assertEqual([False] * 8, [guard.stalled(state) for state in states])
        stable = swarm_work._ProgressGuard()
        one = states[0]
        results = [stable.stalled(one) for _ in range(14)]
        self.assertEqual([False] * 13 + [True], results)
        cycle = swarm_work._ProgressGuard()
        a, b = states[:2]
        provider_cycle = (a, b) * 7
        self.assertEqual(
            [False] * 13 + [True],
            [cycle.stalled(state) for state in provider_cycle],
        )
        evidence = swarm_work._ProgressGuard()
        evidence_results = [
            evidence.stalled((swarm_work._canonical_progress_state(
                "agent", False, False,
                {
                    "remaining": ["same requirement"],
                    "progress": [{
                        "id": "diagnostic", "state": "observed",
                        "evidence": f"engine-evidence-{index}",
                    }],
                },
            ),))
            for index in range(20)
        ]
        self.assertEqual([False] * 13 + [True] * 7, evidence_results)

    def test_arbitrary_novel_checkpoint_state_churn_cannot_buy_rounds(self) -> None:
        guard = swarm_work._ProgressGuard()
        results = [
            guard.stalled((swarm_work._canonical_progress_state(
                "agent", False, False,
                {
                    "remaining": ["same unresolved requirement"],
                    "progress": [{
                        "id": "stable-checkpoint",
                        "state": f"nonce-{index}",
                        "evidence": f"provider claim {index}",
                    }],
                },
            ),))
            for index in range(1, 41)
        ]
        self.assertEqual([False] * 13 + [True] * 27, results)
        self.assertFalse(swarm_work._meaningful_checkpoint_advance(
            "nonce-1", "nonce-2",
        ))
        self.assertTrue(swarm_work._meaningful_checkpoint_advance("17", "18"))
        self.assertTrue(swarm_work._meaningful_checkpoint_advance(
            "tested", "verified",
        ))
        self.assertFalse(swarm_work._meaningful_checkpoint_advance(
            "verified", "working",
        ))

    def test_loop14_exact_operation_capabilities_and_passive_requests(self) -> None:
        actions = (
            "Could parser.py be updated for me?",
            "Can parser.py be updated for me?",
            "Would it be possible for you to update parser.py?",
            "Could I get parser.py updated?",
            "I was hoping parser.py could be updated",
            "Parser.py requires an update",
        )
        for goal in actions:
            with self.subTest(goal=goal):
                spec = swarm_work._compile_goal_spec(self.project, goal)
                self.assertEqual(["parser.py"], [one.casefold() for one in spec["write_policy"]["grants"]])
                self.assertEqual(["MODIFY"], spec["write_policy"]["exact_capabilities"]["parser.py"])
        cases = (
            (
                "Preserve README.md unchanged while updating app.py",
                {"app.py": ["MODIFY"]}, ["README.md"],
            ),
            (
                "Move old name.md into archive/new name.md",
                {"old name.md": ["DELETE"], "archive/new name.md": ["CREATE_OR_MODIFY"]}, [],
            ),
            (
                "Copy source file.md as destination file.md",
                {"destination file.md": ["CREATE_OR_MODIFY"]}, ["source file.md"],
            ),
        )
        for goal, grants, protected in cases:
            with self.subTest(goal=goal):
                spec = swarm_work._compile_goal_spec(self.project, goal)
                self.assertEqual(grants, spec["write_policy"]["exact_capabilities"], spec)
                self.assertEqual(protected, spec["write_policy"]["protected"], spec)

    def test_loop14_exact_scope_rejects_whole_unrelated_executor_proposal(self) -> None:
        (self.project / "parser.py").write_text("value = 1\n", encoding="utf-8")

        def answer(_config, route, _text, **kwargs):
            response_format = kwargs.get("response_format")
            if response_format is swarm_work.PLAN_FORMAT:
                value = {
                    "contribution": "update parser only", "message_to_lead": "exact",
                    "needs_files": [], "effect_paths": ["parser.py"],
                }
            elif response_format is swarm_work.PLAN_REVIEW_FORMAT:
                value = {
                    "contribution": "update parser only", "message_to_lead": "ready",
                    "needs_files": [], "effect_paths": ["parser.py"],
                    "ready_to_execute": True, "remaining": [], "questions": [],
                }
            elif response_format is swarm_work.WORK_VERIFICATION_FORMAT:
                value = {"goal_complete": True, "feedback": "claimed done", "remaining": []}
            else:
                value = {"reply": "done", "changes": [
                    {"path": "parser.py", "content": "value = 2\n", "reason": "requested"},
                    {"path": "unrelated.py", "content": "bad = True\n", "reason": "unrelated"},
                ]}
            return {"text": json.dumps(value), "milliseconds": 1, "model": route}

        with mock.patch.object(chat, "ask_once", side_effect=answer):
            result = swarm_work.work_together(
                self.config, self.board, "agent-1", "Update parser.py", round_limit=1,
            )
        self.assertFalse(result["goal_complete"], result)
        self.assertEqual("value = 1\n", (self.project / "parser.py").read_text(encoding="utf-8"))
        self.assertFalse((self.project / "unrelated.py").exists())
        self.assertEqual([], result["transaction_ids"])

    def test_loop14_semantic_witness_rejects_version_and_source_inspection_decoys(self) -> None:
        (self.project / "calc.py").write_text(
            "VERSION = 'reject-empty-v2'\ndef parse(value):\n    if value == '': raise ValueError()\n    return value\n",
            encoding="utf-8",
        )
        requirement = next(
            one for one in swarm_work._derive_requirement_contract(
                self.project, "Fix calc.py so empty input is rejected"
            )["requirements"] if one["kind"] == "behavior"
        )
        decoys = {
            "test_version_rejects_empty.py": (
                "import unittest, calc\nfrom calc import parse\n"
                "class T(unittest.TestCase):\n def test_rejects_empty_input(self):\n"
                "  parse('ok')\n  self.assertEqual(calc.VERSION, 'reject-empty-v2')\n"
            ),
            "test_source_rejects_empty.py": (
                "import unittest, runpy\nclass T(unittest.TestCase):\n"
                " def test_rejects_empty_input(self):\n"
                "  runpy.run_path('calc.py')\n"
                "  self.assertIn('raise', open('calc.py').read())\n"
            ),
            "test_local_fake_rejects_empty.py": (
                "import unittest\nfrom calc import parse\n"
                "def fake(value): raise ValueError()\n"
                "class T(unittest.TestCase):\n def test_rejects_empty_input(self):\n"
                "  parse('ok')\n  with self.assertRaises(ValueError): fake('')\n"
            ),
        }
        for name, source in decoys.items():
            (self.project / name).write_text(source, encoding="utf-8")
        witnesses = swarm_work._behavior_test_witnesses(
            self.project, ["calc.py"], [requirement],
        )
        self.assertEqual({}, witnesses)

    def test_loop14_preexisting_semantic_regression_test_is_a_witness(self) -> None:
        goal = "Fix calc.py so empty input is rejected"
        (self.project / "calc.py").write_text("def parse(value):\n    return value\n", encoding="utf-8")
        (self.project / "test_calc_existing.py").write_text(
            "import unittest\nfrom calc import parse\n"
            "class T(unittest.TestCase):\n"
            " def test_existing_empty_rejection(self):\n"
            "  with self.assertRaises(ValueError): parse('')\n",
            encoding="utf-8",
        )
        contract = swarm_work._derive_requirement_contract(self.project, goal)
        ledger = CollaborationLedger(self.config, "claude", "preexisting-causal").begin(
            goal, self.board["agents"], mode="project_work"
        )
        saga = swarm_work._MutationSaga(self.project, ledger.session_id)
        transaction_id = FileTransaction.new_transaction_id()
        saga.prepare(transaction_id)
        manifest = FileTransaction(self.project).apply([ChangePlan(
            path="calc.py", baseline_sha256=file_sha256(self.project / "calc.py"),
            content=(
                "def parse(value):\n"
                "    if value == '': raise ValueError()\n"
                "    return value\n"
            ), reason="implement existing regression",
        )], transaction_id=transaction_id)
        swarm_work._record_applied_transaction(ledger, saga, transaction_id, manifest)
        requirement = next(
            one for one in contract["requirements"] if one["kind"] == "behavior"
        )
        witnesses = swarm_work._behavior_test_witnesses(
            self.project, ["calc.py"], [requirement],
        )
        witness = witnesses[requirement["id"]]
        self.assertEqual("test_calc_existing.py", witness["path"])
        self.assertEqual("runtime_call_observable_native_assertion", witness["semantic_trace"]["provenance"])
        result = swarm_work._run_selected_project_verification(
            self.config, self.project, self.board["projects"][0], goal,
            ["calc.py"], None, requirement_contract=contract,
            verification_session_id=ledger.session_id,
            transaction_ids=[transaction_id],
        )
        self.assertEqual("passed", result["status"], result)
        receipt = result["requirement_evidence"]["execution"]["causal_receipts"][0]
        self.assertEqual("test_calc_existing.py", receipt["selected_test_files"][0])

    def test_loop14_scaffolding_file_operations_create_no_behavior_requirements(self) -> None:
        goals = (
            "Would you mind updating parser.py?",
            "Consult ref.md before updating target.py",
            "Preserve README.md unchanged while updating app.py",
            "Update app.py but do not touch README.md",
            "Use notes.md only as reference after fixing parser.py",
            "Replace contents of target.md with source.md",
            "Copy source.md to dest.md",
            "Create retry-report.md",
        )
        for goal in goals:
            with self.subTest(goal=goal):
                contract = swarm_work._derive_requirement_contract(self.project, goal)
                self.assertFalse(any(
                    one["kind"] in {"behavior", "behavior_preservation"}
                    for one in contract["requirements"]
                ), contract)

    def test_loop14_transfer_transactions_enforce_directional_postconditions(self) -> None:
        (self.project / "old name.md").write_text("payload\n", encoding="utf-8")
        move_goal = "Move old name.md into archive/new name.md"
        move_spec = swarm_work._compile_goal_spec(self.project, move_goal)
        move_contract = swarm_work._derive_requirement_contract(self.project, move_goal)
        move_grants = {
            path: set(capabilities)
            for path, capabilities in move_spec["write_policy"]["exact_capabilities"].items()
        }
        move_plans = swarm_work._validated_changes(
            self.project,
            [
                {"path": "old name.md", "delete": True, "reason": "move source"},
                {"path": "archive/new name.md", "content": "payload\n", "reason": "move destination"},
            ],
            exact_write_grants=move_grants,
        )
        FileTransaction(self.project).apply(
            move_plans, allowed_exact_capabilities=move_grants,
        )
        move_evidence = swarm_work._requirement_artifact_evidence(
            self.project, move_contract, ["old name.md", "archive/new name.md"],
        )
        self.assertTrue(move_evidence["passed"], move_evidence)
        self.assertFalse((self.project / "old name.md").exists())
        self.assertEqual("payload\n", (self.project / "archive/new name.md").read_text(encoding="utf-8"))

        (self.project / "source file.md").write_text("copy\n", encoding="utf-8")
        copy_goal = "Copy source file.md as destination file.md"
        copy_spec = swarm_work._compile_goal_spec(self.project, copy_goal)
        copy_contract = swarm_work._derive_requirement_contract(self.project, copy_goal)
        copy_grants = {
            path: set(capabilities)
            for path, capabilities in copy_spec["write_policy"]["exact_capabilities"].items()
        }
        copy_plans = swarm_work._validated_changes(
            self.project,
            [{"path": "destination file.md", "content": "copy\n", "reason": "copy destination"}],
            protected_paths=copy_spec["write_policy"]["protected"],
            exact_write_grants=copy_grants,
        )
        FileTransaction(self.project).apply(
            copy_plans, allowed_exact_capabilities=copy_grants,
            protected_paths=copy_spec["write_policy"]["protected"],
        )
        copy_evidence = swarm_work._requirement_artifact_evidence(
            self.project, copy_contract, ["destination file.md"],
        )
        self.assertTrue(copy_evidence["passed"], copy_evidence)
        self.assertEqual("copy\n", (self.project / "source file.md").read_text(encoding="utf-8"))

    def test_loop15_root_and_exact_capabilities_compose_without_broadening(self) -> None:
        roots = [
            "2_Github repos/3_WITH my tests",
            "2_Github repos/4_upload to github",
            "3_test traceability",
            "4_LangGraph for this project",
            "0_Obsidian vault",
        ]
        raw = [
            {"path": root + "/loop15-proof.txt", "content": root, "reason": "deliverable"}
            for root in roots
        ] + [{
            "path": "2_Github repos/3_WITH my tests/TEST-ci.yml",
            "content": "name: tests\n", "reason": "exact artifact",
        }]
        exact = {
            "2_github repos/3_with my tests/test-ci.yml": {"CREATE_OR_MODIFY"},
        }
        accepted = swarm_work._validated_changes(
            self.project, raw, allowed_write_roots=roots,
            exact_write_grants=exact,
        )
        self.assertEqual(len(raw), len(accepted))
        manifest = FileTransaction(self.project).apply(
            accepted, allowed_write_roots=roots,
            allowed_exact_capabilities=exact,
        )
        self.assertEqual(len(raw), len(manifest["changes"]))
        with self.assertRaisesRegex(HarnessError, "not authorized"):
            swarm_work._validated_changes(
                self.project, [{
                    "path": "2_Github repos/3_WITH my tests/TEST-ci.yml",
                    "content": "name: changed\n",
                }], allowed_write_roots=roots,
                exact_write_grants={
                    "2_github repos/3_with my tests/test-ci.yml": {"CREATE"},
                },
            )
        with self.assertRaisesRegex(HarnessError, "not authorized"):
            swarm_work._validated_changes(
                self.project,
                [{"path": "unrelated.py", "content": "bad = True\n"}],
                exact_write_grants={"parser.py": {"MODIFY"}},
            )
        with self.assertRaisesRegex(HarnessError, "outside"):
            swarm_work._validated_changes(
                self.project,
                [{"path": "unrelated.py", "content": "bad = True\n"}],
                allowed_write_roots=roots, exact_write_grants=exact,
            )

    def test_loop15_original_realmat_prompt_authorizes_every_intended_deliverable(self) -> None:
        prompt = (Path(__file__).parent / "fixtures" / "realmat_original_prompt.txt").read_text(
            encoding="utf-8"
        ).replace("{ROOT}", str(self.project))
        authority = swarm_work._path_authority_from_goal(self.project, prompt)
        spec = swarm_work._compile_goal_spec(self.project, prompt)
        exact = {
            path: set(capabilities)
            for path, capabilities in spec["write_policy"]["exact_capabilities"].items()
        }
        deliverables = [
            "2_Github repos/3_WITH my tests/tests/UNIT/test_unit.py",
            "2_Github repos/3_WITH my tests/tests/API/test_api.py",
            "2_Github repos/3_WITH my tests/tests/E2E/test_e2e.py",
            "2_Github repos/3_WITH my tests/TEST-ci.yml",
            "2_Github repos/4_upload to github/20260828-08/commit-message.md",
            "3_test traceability/20260828-08/index.html",
            "4_LangGraph for this project/workflow.py",
            "0_Obsidian vault/session.md",
        ]
        accepted = swarm_work._validated_changes(
            self.project,
            [{"path": path, "content": "proof\n", "reason": "required deliverable"}
             for path in deliverables],
            allowed_write_roots=authority["writable"],
            protected_paths=authority["read_only"],
            exact_write_grants=exact,
        )
        self.assertEqual(deliverables, [one.path for one in accepted])

    def test_loop15_pure_prohibitions_and_remaining_requests_compile_authority(self) -> None:
        (self.project / "parser.py").write_text("value = 1\n", encoding="utf-8")
        prohibitions = (
            "Parser.py must not be changed.",
            "Parser.py should not be changed.",
            "Parser.py may not be changed.",
            "Parser.py is not to be changed.",
            "Under no circumstances modify parser.py.",
        )
        for goal in prohibitions:
            with self.subTest(goal=goal):
                spec = swarm_work._compile_goal_spec(self.project, goal)
                self.assertEqual("read_only", spec["intent"], spec)
                self.assertEqual("DENY_ALL", spec["write_policy"]["mode"], spec)
                self.assertEqual([], spec["write_policy"]["grants"], spec)
        requests = (
            "Parser.py ought to be updated.",
            "It is requested that parser.py be updated.",
            "I would appreciate it if you updated parser.py.",
            "See to it that parser.py is updated.",
            "Make sure parser.py is updated.",
        )
        for goal in requests:
            with self.subTest(goal=goal):
                spec = swarm_work._compile_goal_spec(self.project, goal)
                self.assertEqual("mutation", spec["intent"], spec)
                self.assertEqual(["parser.py"], [
                    str(one).casefold() for one in spec["write_policy"]["grants"]
                ], spec)

    def test_loop15_coordinated_operations_and_replace_content_postcondition(self) -> None:
        spec = swarm_work._compile_goal_spec(
            self.project,
            "Move a.md to archive/a.md and b.md to archive/b.md",
        )
        moves = [one for one in spec["operations"] if one["kind"] == "move"]
        self.assertEqual([
            ("a.md", "archive/a.md"), ("b.md", "archive/b.md"),
        ], [(one["source"], one["destination"]) for one in moves], spec)
        split = swarm_work._compile_goal_spec(
            self.project, "Delete old.md and create dest.md",
        )
        self.assertEqual(["delete", "create"], [
            one["kind"] for one in split["operations"]
        ], split)
        mixed = swarm_work._compile_goal_spec(
            self.project, "Copy source.md to dest.md and update config.py",
        )
        self.assertEqual(["copy", "modify"], [
            one["kind"] for one in mixed["operations"]
        ], mixed)
        preserved = swarm_work._compile_goal_spec(
            self.project, "Move old.md to new.md and preserve README.md",
        )
        self.assertEqual(["move", "preserve"], [
            one["kind"] for one in preserved["operations"]
        ], preserved)
        self.assertEqual(["README.md"], preserved["write_policy"]["protected"])

        (self.project / "target.md").write_text("old\n", encoding="utf-8")
        (self.project / "source.md").write_text("source\n", encoding="utf-8")
        contract = swarm_work._derive_requirement_contract(
            self.project, "Replace contents of target.md with source.md",
        )
        (self.project / "target.md").write_text("WRONG\n", encoding="utf-8")
        wrong = swarm_work._requirement_artifact_evidence(
            self.project, contract, ["target.md"],
        )
        self.assertFalse(wrong["passed"], wrong)
        (self.project / "target.md").write_text("source\n", encoding="utf-8")
        correct = swarm_work._requirement_artifact_evidence(
            self.project, contract, ["target.md"],
        )
        self.assertTrue(correct["passed"], correct)

    def test_loop15_selected_verification_runs_side_effecting_tests_only_in_disposable_copy(self) -> None:
        (self.project / "verification-modify.txt").write_text("before\n", encoding="utf-8")
        (self.project / "verification-delete.txt").write_text("keep\n", encoding="utf-8")
        (self.project / "verification-rename.txt").write_text("keep name\n", encoding="utf-8")
        test_path = self.project / "test_side_effect.py"
        test_path.write_text(
            "import pathlib, unittest\n"
            "pathlib.Path('verification-created.txt').write_text('sandbox')\n"
            "pathlib.Path('verification-modify.txt').write_text('changed')\n"
            "pathlib.Path('verification-delete.txt').unlink()\n"
            "pathlib.Path('verification-rename.txt').rename('verification-renamed.txt')\n"
            "class T(unittest.TestCase):\n"
            " def test_real(self): self.assertEqual(2 + 2, 4)\n",
            encoding="utf-8",
        )
        before, _ = swarm_work._project_tree_merkle(self.project)
        result = swarm_work._run_selected_project_verification(
            self.config, self.project, self.board["projects"][0],
            "Create unit tests", ["test_side_effect.py"], None,
        )
        after, _ = swarm_work._project_tree_merkle(self.project)
        self.assertEqual("passed", result["status"], result)
        self.assertEqual(before, after)
        self.assertFalse((self.project / "verification-created.txt").exists())
        self.assertEqual("before\n", (self.project / "verification-modify.txt").read_text(encoding="utf-8"))
        self.assertEqual("keep\n", (self.project / "verification-delete.txt").read_text(encoding="utf-8"))
        self.assertEqual("keep name\n", (self.project / "verification-rename.txt").read_text(encoding="utf-8"))
        self.assertFalse((self.project / "verification-renamed.txt").exists())
        self.assertTrue(all(one.get("disposable_snapshot") for one in result["commands"]))

    def test_loop15_absolute_verification_escape_is_restored_and_rejected(self) -> None:
        victim = self.project / "real-victim.txt"
        victim.write_text("original\n", encoding="utf-8")
        created = self.project / "real-created.txt"
        test_path = self.project / "test_absolute_escape.py"
        test_path.write_text(
            "import pathlib, unittest\n"
            f"pathlib.Path({str(victim)!r}).write_text('escaped')\n"
            f"pathlib.Path({str(created)!r}).write_text('escaped')\n"
            "class T(unittest.TestCase):\n"
            " def test_real(self): self.assertTrue(True)\n",
            encoding="utf-8",
        )
        before, _ = swarm_work._project_tree_merkle(self.project)
        result = swarm_work._run_selected_project_verification(
            self.config, self.project, self.board["projects"][0],
            "Create unit tests", ["test_absolute_escape.py"], None,
        )
        after, _ = swarm_work._project_tree_merkle(self.project)
        self.assertIn(result["status"], {"failed", "unavailable"}, result)
        self.assertEqual("verification_containment_denied", result["basis"], result)
        self.assertEqual(before, after)
        self.assertEqual("original\n", victim.read_text(encoding="utf-8"))
        self.assertFalse(created.exists())
        if os.name == "nt":
            self.assertEqual(
                "windows-appcontainer-job-v1",
                result["commands"][0]["containment_profile"],
            )
            attestation = result["commands"][0]["containment_attestation"]
            self.assertTrue(attestation["native_write_denied"], attestation)
            self.assertTrue(attestation["child_inherited_boundary"], attestation)
            self.assertTrue(attestation["reparse_checked"], attestation)
        else:
            self.assertEqual("unavailable", result["status"], result)

    def test_loop16_engine_direct_probe_freezes_exact_python_callable(self) -> None:
        baseline = self.project / "baseline"
        current = self.project / "current"
        baseline.mkdir()
        current.mkdir()
        (baseline / "calc.py").write_text(
            "def parse(value):\n    return value\n", encoding="utf-8",
        )
        (current / "calc.py").write_text(
            "def parse(value):\n    if value == '': raise ValueError('empty')\n    return value\n",
            encoding="utf-8",
        )
        scenario = {
            "predicate": "REJECT", "stimulus_property": "empty",
            "acceptance_digest": "scenario-1",
        }
        witness = {
            "path": "test_existing.py",
            "semantic_trace": {
                "production_component": "calc", "production_call": "calc.parse",
            },
        }
        target = swarm_work._frozen_acceptance_target(
            current, baseline, ["calc.py"], witness,
            "Fix calc.parse so empty input is rejected", ["calc.py"],
        )
        self.assertIsNotNone(target)
        config = LoadedConfig(copy.deepcopy(self.config.data), current, [], {})
        receipt = swarm_work._run_direct_acceptance_probe(
            config, current, baseline, target or {}, scenario,
        )
        if os.name != "nt":
            self.assertIsNone(receipt)
            return
        self.assertIsNotNone(receipt)
        self.assertEqual("parse", receipt["target"]["qualname"])
        self.assertTrue(all(
            one["runtime_identity"]["qualname"] == "parse"
            for one in receipt["current_observations"]
        ))
        self.assertFalse(receipt["baseline_observation"]["passed"])

    def test_loop16_new_test_cannot_self_select_ambiguous_acceptance_target(self) -> None:
        baseline = self.project / "baseline"
        current = self.project / "current"
        baseline.mkdir()
        current.mkdir()
        source = (
            "def parse(value): return value\n"
            "def validate(value): return value\n"
        )
        (baseline / "calc.py").write_text(source, encoding="utf-8")
        (current / "calc.py").write_text(source, encoding="utf-8")
        witness = {
            "path": "test_new.py",
            "semantic_trace": {
                "production_component": "calc", "production_call": "calc.parse",
            },
        }
        target = swarm_work._frozen_acceptance_target(
            current, baseline, ["calc.py"], witness,
            "Fix calc.py so empty input is rejected", ["calc.py", "test_new.py"],
        )
        self.assertIsNone(target)

    def test_loop17_clause_frames_cover_transfer_prohibition_and_speech_matrix(self) -> None:
        operation_cases = {
            "Move old.md to new.md without changing README.md": [
                ("preserve", "README.md", ""), ("move", "old.md", "new.md"),
            ],
            "No changes except update parser.py": [("modify", "parser.py", "")],
            "Move a.md to archive/a.md then b.md to archive/b.md": [
                ("move", "a.md", "archive/a.md"), ("move", "b.md", "archive/b.md"),
            ],
            "Move a.md and b.md into archive/": [
                ("move", "a.md", "archive/a.md"), ("move", "b.md", "archive/b.md"),
            ],
            "Copy a.md and b.md into archive/": [
                ("copy", "a.md", "archive/a.md"), ("copy", "b.md", "archive/b.md"),
            ],
            "Copy source.md to dest.md while preserving source.md": [
                ("preserve", "source.md", ""), ("copy", "source.md", "dest.md"),
            ],
            "Replace target.md using source.md": [("replace", "source.md", "target.md")],
        }
        for goal, expected in operation_cases.items():
            with self.subTest(goal=goal):
                observed = []
                for operation in swarm_work._goal_operations(goal):
                    if operation["kind"] == "preserve":
                        observed.append(("preserve", operation["target"], ""))
                    elif operation["kind"] in {"modify", "create", "delete"}:
                        observed.append((operation["kind"], operation["target"], ""))
                    elif operation["kind"] == "replace":
                        observed.append(("replace", operation["source"], operation["target"]))
                    else:
                        observed.append((operation["kind"], operation["source"], operation["destination"]))
                self.assertEqual(expected, observed)

        actionable = [
            "I would appreciate parser.py being updated", "May I have parser.py updated?",
            "I request that parser.py be updated", "Parser.py has to be updated",
            "Parser.py is due for an update", "Please arrange for parser.py to be updated",
            "Have parser.py updated", "Let parser.py be updated",
            "An update is required for parser.py",
        ]
        informational = [
            "Parser.py does not need to be changed", "Does parser.py need updating?",
            "Do you think parser.py should be updated?",
        ]
        (self.project / "parser.py").write_text("value = 1\n", encoding="utf-8")
        for goal in actionable:
            with self.subTest(actionable=goal):
                spec = swarm_work._compile_goal_spec(self.project, goal)
                self.assertEqual("SCOPED", spec["write_policy"]["mode"])
                self.assertIn("parser.py", [one.casefold() for one in spec["write_policy"]["grants"]])
        for goal in informational:
            with self.subTest(informational=goal):
                self.assertEqual("DENY_ALL", swarm_work._compile_goal_spec(
                    self.project, goal,
                )["write_policy"]["mode"])

    def test_loop17_ambiguous_behavior_target_pauses_before_provider_and_answer_ratifies(self) -> None:
        (self.project / "calc.py").write_text(
            "def parse(value):\n    return value\n\n"
            "def unrelated(value):\n    return value\n",
            encoding="utf-8",
        )
        goal = "Fix calc.py so empty input is rejected"
        spec = swarm_work._compile_goal_spec(self.project, goal)
        decision = swarm_work._acceptance_target_decision(self.project, goal, spec)
        self.assertEqual("needs_clarification", decision["status"])
        self.assertEqual({"parse", "unrelated"}, {
            one["qualname"] for one in decision["candidates"]
        })
        with mock.patch.object(chat, "ask_once") as ask:
            paused = swarm_work.work_together(
                self.config, self.board, "agent-1", goal,
            )
        ask.assert_not_called()
        self.assertEqual("paused_for_user", paused["status"])
        self.assertFalse(paused["goal_complete"])
        self.assertEqual([], paused["changed"])
        self.assertIn("parse", paused["questions"][0])
        ratified = swarm_work._acceptance_target_decision(
            self.project, goal, spec, "Use parse",
        )
        self.assertEqual("ratified", ratified["status"])
        self.assertEqual("parse", ratified["target"]["qualname"])
        self.assertRegex(ratified["ratification_digest"], r"^[0-9a-f]{64}$")

    def test_loop17_https_acceptance_origin_is_external_not_project_path_authority(self) -> None:
        goal = (
            "Test https://staging.example.com/login and ensure the submitted status is Saved; "
            "update tests/E2E/login.spec.ts."
        )
        self.assertEqual(["tests/E2E/login.spec.ts"], swarm_work._goal_named_paths(goal))
        roles = swarm_work._goal_path_roles(goal)
        self.assertEqual(["tests/E2E/login.spec.ts"], roles["effects"])
        self.assertNotIn("staging.example.com/login", roles["effects"])

    @unittest.skipUnless(os.name == "nt" and shutil.which("node"), "Windows Node direct probe")
    def test_loop16_engine_direct_probe_invokes_exact_async_js_export(self) -> None:
        baseline = self.project / "baseline-js"
        current = self.project / "current-js"
        baseline.mkdir()
        current.mkdir()
        (baseline / "server.js").write_text(
            "exports.parse = async value => value; exports.unrelated = () => { throw new Error('x'); };\n",
            encoding="utf-8",
        )
        (current / "server.js").write_text(
            "exports.parse = async value => { if (value === '') throw new TypeError('empty'); return value; }; "
            "exports.unrelated = () => { throw new Error('x'); };\n",
            encoding="utf-8",
        )
        scenario = {
            "predicate": "REJECT", "stimulus_property": "empty",
            "acceptance_digest": "scenario-js-1",
        }
        witness = {
            "path": "server-existing.spec.js",
            "semantic_trace": {
                "production_component": "server", "production_call": "server.parse",
            },
        }
        target = swarm_work._frozen_acceptance_target(
            current, baseline, ["server.js"], witness,
            "Fix server.parse so empty input is rejected", ["server.js"],
        )
        self.assertIsNotNone(target)
        config = LoadedConfig(copy.deepcopy(self.config.data), current, [], {})
        receipt = swarm_work._run_direct_acceptance_probe(
            config, current, baseline, target or {}, scenario,
        )
        self.assertIsNotNone(receipt)
        self.assertEqual("parse", receipt["target"]["qualname"])
        self.assertTrue(all(
            one["observable"] == "exception"
            for one in receipt["current_observations"]
        ))
        self.assertFalse(receipt["baseline_observation"]["passed"])

    def test_loop16_intent_constraints_and_operation_frames_remain_separate(self) -> None:
        cases = {
            "Preserve README.md unchanged while updating app.py": (
                ["app.py"], ["README.md"], ["preserve", "modify"]
            ),
            "Do not modify anything. However, repairs to parser.py are required": (
                ["parser.py"], [], ["modify"]
            ),
            "Move a.md to archive/a.md then b.md to archive/b.md; do not modify README.md": (
                ["a.md", "archive/a.md", "b.md", "archive/b.md"],
                ["README.md"], ["move", "move", "preserve"],
            ),
            "Delete old.md and then create dest.md": (
                ["old.md", "dest.md"], [], ["delete", "create"]
            ),
        }
        for goal, (grants, protected, kinds) in cases.items():
            with self.subTest(goal=goal):
                spec = swarm_work._compile_goal_spec(self.project, goal)
                self.assertEqual("mutation", spec["intent"], spec)
                self.assertEqual(
                    [one.casefold() for one in grants],
                    [str(one).casefold() for one in spec["write_policy"]["grants"]], spec,
                )
                self.assertEqual(
                    [one.casefold() for one in protected],
                    [str(one).casefold() for one in spec["write_policy"]["protected"]], spec,
                )
                self.assertEqual(kinds, [one["kind"] for one in spec["operations"]], spec)

        for goal in (
            "Parser.py must not be changed",
            "Parser.py should not be changed",
            "Parser.py may not be changed",
            "Parser.py is not to be changed",
            "Under no circumstances modify parser.py",
        ):
            with self.subTest(goal=goal):
                spec = swarm_work._compile_goal_spec(self.project, goal)
                self.assertEqual("read_only", spec["intent"], spec)
                self.assertEqual("DENY_ALL", spec["write_policy"]["mode"], spec)

    def test_loop16_context_progress_requires_requirement_relevance(self) -> None:
        contract = {
            "requirements": [{
                "id": "behavior_parser_reject_empty", "kind": "behavior",
                "effect_paths": ["parser.py"],
                "acceptance_terms": ["reject", "empty"],
            }],
        }
        terms = swarm_work._requirement_context_terms(contract)
        irrelevant = {
            "name": "read_file", "arguments_sha256": "a" * 64,
            "arguments": {"path": "unrelated.md"},
            "result": {"status": "ok", "content": "vacation notes", "truncated": False},
        }
        relevant = copy.deepcopy(irrelevant)
        relevant["arguments"] = {"path": "parser.py"}
        relevant["result"]["content"] = "empty input rejection branch"
        self.assertEqual("", swarm_work._context_result_evidence_digest(irrelevant, terms))
        self.assertTrue(swarm_work._context_result_evidence_digest(relevant, terms))

    def test_loop17_exact_https_base_url_selects_bound_safe_broker_receipt(self) -> None:
        (self.project / "playwright.config.ts").write_text(
            "export default { use: { baseURL: 'https://example.com/' } };\n",
            encoding="utf-8",
        )
        spec = self.project / "tests" / "exact.spec.ts"
        spec.parent.mkdir(parents=True)
        spec.write_text(
            "test('remote', async ({ page }) => {\n"
            "  await page.goto('/');\n"
            "  await page.locator('h1').click();\n"
            "  await expect(page.locator('h1')).toHaveText('Example Domain');\n"
            "});\n", encoding="utf-8",
        )
        suite_result = {
            "passed": True, "execution_mode": "unmodified-suite-inprocess-worker-main",
            "approved_base_url": "https://example.com/", "approved_origin": "https://example.com",
            "receipt": {"tests": [{"status": "passed", "expectedStatus": "passed"}]},
            "broker": {
                "passed": True, "runner": {"stdout": "1 passed", "stderr": "", "timed_out": False},
                "origin_routes": [{"route": "https-connect", "allowed": True}],
                "boundary_inheritance_attested": True,
            },
        }
        with mock.patch.object(
            swarm_work, "run_brokered_playwright_suite", return_value=suite_result,
        ) as run:
            result = swarm_work._run_brokered_playwright_specs(
                self.project, ["node", "playwright", str(spec)], timeout=10,
            )
        self.assertEqual(0, result["exit_code"], result)
        run.assert_called_once()
        args = run.call_args.args
        self.assertEqual("https://example.com/", args[2])
        self.assertTrue(result["ordinary_suite_executed"])
        self.assertEqual(
            "unmodified-suite-inprocess-worker-main",
            result["brokered_e2e_receipts"][0]["execution_mode"],
        )

    def test_loop17_exact_origin_local_decoy_is_rejected_before_browser_launch(self) -> None:
        source = (
            "test('decoy', async ({ page }) => {\n"
            " await page.goto('https://127.0.0.1:8443/');\n"
            " await expect(page.locator('h1')).toHaveText('Decoy');\n"
            "});\n"
        )
        lifted = swarm_work._playwright_exact_origin_scenario(
            source, "https://example.com/",
        )
        self.assertIsNotNone(lifted)
        assert lifted is not None
        with self.assertRaisesRegex(ValueError, "exact approved origin"):
            playwright_runtime.validate_safe_playwright_scenario(
                lifted["safe_scenario"], lifted["approved_base_url"],
            )

    @unittest.skipUnless(os.name == "nt" and shutil.which("node"), "Windows Node containment probe")
    def test_loop16_node_permission_and_appcontainer_deny_sibling_write(self) -> None:
        snapshot = self.project / "node-snapshot"
        snapshot.mkdir()
        sibling = snapshot.parent / "node-escape.txt"
        command = [
            shutil.which("node") or "node", "-e",
            "require('fs').writeFileSync("
            + json.dumps(str(sibling)) + ",'bad')",
        ]
        result = swarm_work._contained_snapshot_command(
            self.config, snapshot, command, timeout=10, denied_root=self.project,
        )
        self.assertNotEqual(0, result["exit_code"], result)
        self.assertFalse(sibling.exists())
        self.assertEqual("windows-appcontainer-job-v1", result["containment_profile"])
        self.assertRegex(result["stderr"], r"(?:ERR_ACCESS_DENIED|EPERM|EACCES)")
        self.assertTrue(result["containment_attestation"]["child_inherited_boundary"])

    @unittest.skipUnless(os.name == "nt", "Windows Python containment probe")
    def test_loop17_python_child_and_native_dependency_inherit_containment(self) -> None:
        snapshot = self.project / "python-child-snapshot"
        snapshot.mkdir()
        sibling = snapshot.parent / "python-child-escape.txt"
        local = snapshot / "child-local.txt"
        child_source = (
            "from pathlib import Path;"
            f"Path({str(sibling)!r}).write_text('escape',encoding='utf-8')"
        )
        source = (
            "import ctypes, pathlib, socket, subprocess, sys;"
            f"result=subprocess.run([sys.executable,'-c',{child_source!r}]);"
            "assert result.returncode != 0;"
            "server=socket.socket();server.bind(('127.0.0.1',0));server.listen(1);"
            "client=socket.socket();client.connect(server.getsockname());"
            "accepted,_=server.accept();client.sendall(b'ok');"
            "assert accepted.recv(2)==b'ok';accepted.close();client.close();server.close();"
            "pathlib.Path('child-local.txt').write_text("
            "str(ctypes.windll.kernel32.GetCurrentProcessId()),encoding='utf-8')"
        )
        result = swarm_work._contained_snapshot_command(
            self.config, snapshot, ["python", "-c", source],
            timeout=20, denied_root=self.project,
        )
        self.assertEqual(0, result["exit_code"], result)
        self.assertTrue(local.is_file(), result)
        self.assertFalse(sibling.exists(), result)
        self.assertEqual("windows-appcontainer-job-v1", result["containment_profile"])
        self.assertEqual("kill-on-close-no-breakaway", result["job_policy"])
        self.assertTrue(result["containment_attestation"]["child_inherited_boundary"])

    @unittest.skipUnless(os.name == "nt", "Windows immutable engine containment probe")
    def test_python_engine_runtime_blocks_native_laundering_and_mutable_anchors(self) -> None:
        snapshot = self.project / "python-engine-immutable-snapshot"
        snapshot.mkdir()
        local = snapshot / "ordinary-project-write.txt"
        source = r'''
import ctypes, json, os, pathlib, shutil, subprocess, sys
from ctypes import wintypes
root=pathlib.Path(os.environ['NEXUS_VERIFICATION_ROOT'])
engine=pathlib.Path(os.environ['NEXUS_VERIFICATION_ENGINE_ROOT'])
runtime=pathlib.Path(os.environ['NEXUS_ALLOWED_EXEC_ROOTS'].split(os.pathsep)[0])
actual_system_root=pathlib.Path(os.environ['SystemRoot'])

# Ordinary interpreted project mutation, including rename and delete, remains
# available in the disposable snapshot.
pathlib.Path('ordinary-project-write.txt').write_text('allowed',encoding='utf-8')
rename_source=pathlib.Path('ordinary-rename-source.txt')
rename_target=pathlib.Path('ordinary-rename-target.txt')
rename_source.write_text('rename',encoding='utf-8')
os.replace(rename_source,rename_target)
rename_target.unlink()

# Use real, valid Windows images so an access-denied loader/process result
# proves the snapshot FILE_EXECUTE denial rather than malformed test bytes.
project_native=pathlib.Path('project-native.pyd').resolve()
project_child=pathlib.Path('project-child.exe').resolve()
shutil.copyfile(actual_system_root/'System32'/'winmm.dll',project_native)
shutil.copyfile(actual_system_root/'System32'/'cmd.exe',project_child)
nested=pathlib.Path('nested-project-output');nested.mkdir()
nested_native=(nested/'nested-native.dll').resolve()
nested_child=(nested/'nested-child.exe').resolve()
shutil.copyfile(actual_system_root/'System32'/'winmm.dll',nested_native)
shutil.copyfile(actual_system_root/'System32'/'cmd.exe',nested_child)

# Mutable Python globals must not redefine either trust anchor after the hook
# has captured the approved interpreter and actual Windows directory.
approved_executable=sys.executable
sys.executable=str(project_child)
try:
    subprocess.run([sys.executable,'/d','/c','exit','0'],check=False)
except PermissionError:
    pass
else:
    raise AssertionError('mutated sys.executable became an approved child')
finally:
    sys.executable=approved_executable
os.environ['SystemRoot']=str(root)
try:
    ctypes.CDLL(str(project_native))
except PermissionError:
    pass
else:
    raise AssertionError('mutated SystemRoot approved a project native library')

# Audit-hook globals can themselves be changed by the process, so everything
# below calls kernel32 directly after deliberately broadening those globals.
guard=sys.modules['sitecustomize']
guard._SELF_EXECUTABLE=str(project_child)
guard._SYSTEM_ROOTS=(str(root),)
guard._EXEC_ROOTS=(str(root),)
kernel32=ctypes.WinDLL('kernel32',use_last_error=True)
kernel32.CreateFileW.argtypes=[ctypes.c_wchar_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p]
kernel32.CreateFileW.restype=ctypes.c_void_p
kernel32.CopyFileW.argtypes=[ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_int]
kernel32.CopyFileW.restype=ctypes.c_int
kernel32.MoveFileW.argtypes=[ctypes.c_wchar_p,ctypes.c_wchar_p]
kernel32.MoveFileW.restype=ctypes.c_int
kernel32.MoveFileExW.argtypes=[ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_uint32]
kernel32.MoveFileExW.restype=ctypes.c_int
kernel32.CreateHardLinkW.argtypes=[ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_void_p]
kernel32.CreateHardLinkW.restype=ctypes.c_int
kernel32.LoadLibraryW.argtypes=[ctypes.c_wchar_p]
kernel32.LoadLibraryW.restype=ctypes.c_void_p
invalid=ctypes.c_void_p(-1).value

# Native writes/copies cannot alter or add bytes below the external RX engine.
for target in (runtime/'laundered-native.dll',engine/'sitecustomize.py'):
    handle=kernel32.CreateFileW(str(target),0x40000000,7,None,2,0x80,None)
    assert handle == invalid and ctypes.get_last_error()==5, ('engine write unexpectedly succeeded',target,ctypes.get_last_error())
assert not kernel32.CopyFileW(str(project_native),str(runtime/'copied-native.dll'),False)
assert ctypes.get_last_error()==5 and not (runtime/'copied-native.dll').exists()
assert not kernel32.CopyFileW(str(project_child),str(runtime/'copied-child.exe'),False)
assert ctypes.get_last_error()==5 and not (runtime/'copied-child.exe').exists()

# Writable snapshot parents cannot rename the engine, runtime, or guard away
# and recreate their formerly trusted lexical paths.
move_targets=(
    (engine/'sitecustomize.py',root/'stolen-sitecustomize.py'),
    (runtime,root/'stolen-runtime'),
    (engine,root/'stolen-engine'),
)
for source_path,destination in move_targets:
    assert not kernel32.MoveFileW(str(source_path),str(destination))
    assert ctypes.get_last_error()==5 and source_path.exists() and not destination.exists()
assert not kernel32.MoveFileExW(str(engine),str(root/'replaced-engine'),1)
assert ctypes.get_last_error()==5 and engine.is_dir()

# A hard link is an ACL alias rather than a pathname traversal.  Attempt the
# attack from the real AppContainer.  If Windows permits the link, Nexus must
# remove its snapshot name before privileged recursive ACL cleanup; if source
# file permissions deny creation, record the kernel denial for host-side
# sanitizer coverage to complement.
engine_hardlink=root/'project-engine-hardlink.py'
hardlink_created=bool(kernel32.CreateHardLinkW(
    str(engine_hardlink),str(engine/'sitecustomize.py'),None
))
hardlink_error=0 if hardlink_created else ctypes.get_last_error()
assert hardlink_created or hardlink_error==5, ('unexpected hard-link result',hardlink_error)
pathlib.Path('hardlink-attempt.json').write_text(json.dumps({
    'created':hardlink_created,'error':hardlink_error,
}),encoding='utf-8')

# Even fully valid images copied into project-writable space have no
# FILE_EXECUTE for the package SID: direct native loading and process creation
# fail at the OS boundary after the hook's Python globals were compromised.
for library in (project_native,nested_native):
    assert not kernel32.LoadLibraryW(str(library))
    assert ctypes.get_last_error()==5
class STARTUPINFO(ctypes.Structure):
    _fields_=[('cb',wintypes.DWORD),('lpReserved',wintypes.LPWSTR),('lpDesktop',wintypes.LPWSTR),('lpTitle',wintypes.LPWSTR),('dwX',wintypes.DWORD),('dwY',wintypes.DWORD),('dwXSize',wintypes.DWORD),('dwYSize',wintypes.DWORD),('dwXCountChars',wintypes.DWORD),('dwYCountChars',wintypes.DWORD),('dwFillAttribute',wintypes.DWORD),('dwFlags',wintypes.DWORD),('wShowWindow',wintypes.WORD),('cbReserved2',wintypes.WORD),('lpReserved2',ctypes.POINTER(ctypes.c_byte)),('hStdInput',wintypes.HANDLE),('hStdOutput',wintypes.HANDLE),('hStdError',wintypes.HANDLE)]
class PROCESS_INFORMATION(ctypes.Structure):
    _fields_=[('hProcess',wintypes.HANDLE),('hThread',wintypes.HANDLE),('dwProcessId',wintypes.DWORD),('dwThreadId',wintypes.DWORD)]
kernel32.CreateProcessW.argtypes=[wintypes.LPCWSTR,wintypes.LPWSTR,ctypes.c_void_p,ctypes.c_void_p,wintypes.BOOL,wintypes.DWORD,ctypes.c_void_p,wintypes.LPCWSTR,ctypes.POINTER(STARTUPINFO),ctypes.POINTER(PROCESS_INFORMATION)]
kernel32.CreateProcessW.restype=wintypes.BOOL
startup=STARTUPINFO();startup.cb=ctypes.sizeof(startup)
for executable in (project_child,nested_child):
    process=PROCESS_INFORMATION()
    command=ctypes.create_unicode_buffer('"'+str(executable)+'" /d /c exit 0')
    assert not kernel32.CreateProcessW(str(executable),command,None,None,False,0,None,str(root),ctypes.byref(startup),ctypes.byref(process))
    assert ctypes.get_last_error()==5
'''
        result = swarm_work._contained_snapshot_command(
            self.config, snapshot, ["python", "-c", source],
            timeout=25, denied_root=self.project,
        )
        self.assertEqual(0, result["exit_code"], result)
        self.assertTrue(local.is_file(), result)
        self.assertEqual("windows-appcontainer-job-v1", result["containment_profile"])
        self.assertTrue(result["snapshot_file_execute_denied"])
        self.assertTrue(result["containment_attestation"]["private_write_allowed"])
        hardlink_attempt = json.loads(
            (snapshot / "hardlink-attempt.json").read_text(encoding="utf-8")
        )
        if hardlink_attempt["created"]:
            self.assertIn(
                "project-engine-hardlink.py",
                result["cleanup_reparse_entries_removed"],
            )
        else:
            self.assertEqual(5, hardlink_attempt["error"])
        self.assertFalse((snapshot / "project-engine-hardlink.py").exists())

    @unittest.skipUnless(os.name == "nt", "Windows 8.3 path-alias containment probe")
    def test_verification_guard_accepts_true_short_alias_but_denies_project_native(self) -> None:
        import ctypes

        snapshot = self.project / "verification alias snapshot"
        snapshot.mkdir()
        engine_root = self.root / "verification-alias-engine"
        python_guard, _ = swarm_work._verification_guard_files(snapshot, engine_root)
        runtime = python_guard.parent / "runtime"
        runtime.mkdir()
        allowed_native = runtime / "allowed-native.pyd"
        allowed_native.write_bytes(b"not loaded; only its containment identity is checked")
        denied_native = snapshot / "project-native.pyd"
        denied_native.write_bytes(b"project native extensions must remain denied")
        inside_write = snapshot / "inside-write.txt"
        outside_write = snapshot.parent / "outside-write.txt"

        def short_path(path: Path) -> Path:
            buffer = ctypes.create_unicode_buffer(32768)
            copied = ctypes.windll.kernel32.GetShortPathNameW(
                str(path), buffer, len(buffer),
            )
            if copied == 0 or copied >= len(buffer):
                self.skipTest("this Windows volume does not expose an 8.3 alias")
            return Path(buffer.value)

        short_snapshot = short_path(snapshot)
        short_runtime = short_path(runtime)
        self.assertTrue(snapshot.samefile(short_snapshot))
        self.assertTrue(runtime.samefile(short_runtime))
        if os.path.normcase(str(short_runtime)) == os.path.normcase(str(runtime)):
            self.skipTest("this Windows volume does not expose a distinct 8.3 alias")

        probe = (
            "import runpy\n"
            f"guard=runpy.run_path({str(python_guard)!r})\n"
            f"guard['_deny_path']({str(inside_write)!r},'write')\n"
            "try:\n"
            f"    guard['_deny_path']({str(outside_write)!r},'write')\n"
            "except PermissionError:\n"
            "    pass\n"
            "else:\n"
            "    raise SystemExit('outside write was accepted')\n"
            f"guard['_audit']('import',('allowed',{str(allowed_native)!r}))\n"
            "try:\n"
            f"    guard['_audit']('import',('denied',{str(denied_native)!r}))\n"
            "except PermissionError:\n"
            "    pass\n"
            "else:\n"
            "    raise SystemExit('project native extension was accepted')\n"
        )
        environment = dict(os.environ)
        environment.update({
            "NEXUS_VERIFICATION_ROOT": str(short_snapshot),
            "NEXUS_VERIFICATION_ENGINE_ROOT": str(engine_root),
            "NEXUS_ALLOWED_EXEC_ROOTS": str(short_runtime),
        })
        result = subprocess.run(
            [sys.executable, "-S", "-c", probe],
            cwd=str(snapshot), env=environment, capture_output=True, text=True,
            timeout=20, check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr or result.stdout)

    @unittest.skipUnless(os.name == "nt", "Windows brokered Playwright verification")
    def test_loop16_brokered_playwright_proves_real_route_dom_observable(self) -> None:
        runtime = swarm_work.discover_bundled_playwright_runtime()
        if runtime is None:
            self.skipTest("bundled Playwright runtime is not installed")
        (self.project / "index.html").write_text(
            "<input id='name'><button id='save' "
            "onclick=\"document.querySelector('#result').textContent=document.querySelector('#name').value\">"
            "Save</button><p id='result'></p>",
            encoding="utf-8",
        )
        relative = "tests/E2E/browser-save.spec.js"
        test_path = self.project / relative
        test_path.parent.mkdir(parents=True)
        test_path.write_text(
            "const { test, expect } = require('@playwright/test');\n"
            "test('saves the entered name in the real browser', async ({ page }) => {\n"
            "  await page.goto('/index.html');\n"
            "  await page.locator('#name').fill('Loop16');\n"
            "  await page.locator('#save').click();\n"
            "  await expect(page.locator('#result')).toHaveText('Loop16');\n"
            "});\n",
            encoding="utf-8",
        )
        project = copy.deepcopy(self.board["projects"][0])
        project["test_commands"] = [[
            str(runtime.node), str(runtime.cli), "test",
        ]]
        goal = "Create genuine E2E tests for the browser interface"
        result = swarm_work._run_selected_project_verification(
            self.config, self.project, project, goal,
            ["index.html", relative], None,
        )
        self.assertEqual("passed", result["status"], result)
        receipts = [
            receipt
            for command in result["commands"]
            for receipt in command.get("brokered_e2e_receipts", [])
        ]
        self.assertTrue(receipts, result)
        self.assertTrue(all(one["receipt"]["externalWriteDenied"] for one in receipts))
        self.assertTrue(all(one["receipt"]["observed"] == "Loop16" for one in receipts))
        self.assertTrue(all(
            one["broker"]["runner"]["containment_profile"] == "windows-appcontainer-job-v1"
            and one["broker"]["browser"]["containment_profile"] == "windows-appcontainer-job-v1"
            for one in receipts
        ))

    @unittest.skipUnless(os.name == "nt", "Windows brokered Playwright causal proof")
    def test_loop16_brokered_playwright_behavior_uses_current_current_baseline_dom(self) -> None:
        runtime = swarm_work.discover_bundled_playwright_runtime()
        if runtime is None:
            self.skipTest("bundled Playwright runtime is not installed")
        index = self.project / "index.html"
        index.write_text(
            "<input id='name'><button id='save' "
            "onclick=\"document.querySelector('#result').textContent=document.querySelector('#name').value\">"
            "Save</button><p id='result'></p>", encoding="utf-8",
        )
        relative = "tests/E2E/reject-empty.spec.js"
        test_path = self.project / relative
        test_path.parent.mkdir(parents=True)
        test_path.write_text(
            "const { test, expect } = require('@playwright/test');\n"
            "test('rejects empty input in the real browser', async ({ page }) => {\n"
            "  await page.goto('/index.html');\n"
            "  await page.locator('#name').fill('');\n"
            "  await page.locator('#save').click();\n"
            "  await expect(page.locator('#result')).toHaveText('Invalid');\n"
            "});\n", encoding="utf-8",
        )
        goal = "Fix index.html so empty input is rejected in the browser"
        ledger = CollaborationLedger(self.config, "claude", "brokered-e2e-causal").begin(
            goal, self.board["agents"], mode="project_work",
        )
        saga = swarm_work._MutationSaga(self.project, ledger.session_id)
        transaction_id = FileTransaction.new_transaction_id()
        saga.prepare(transaction_id)
        manifest = FileTransaction(self.project).apply([
            ChangePlan(
                path="index.html", baseline_sha256=file_sha256(index),
                content=(
                    "<input id='name'><button id='save' onclick=\"const v=document.querySelector('#name').value;"
                    "document.querySelector('#result').textContent=v===''?'Invalid':v\">Save</button>"
                    "<p id='result'></p>"
                ), reason="reject empty browser input",
            ),
        ], transaction_id=transaction_id)
        swarm_work._record_applied_transaction(ledger, saga, transaction_id, manifest)
        project = copy.deepcopy(self.board["projects"][0])
        project["test_commands"] = [[str(runtime.node), str(runtime.cli), "test"]]
        contract = swarm_work._derive_requirement_contract(self.project, goal)
        result = swarm_work._run_selected_project_verification(
            self.config, self.project, project, goal, ["index.html"], None,
            requirement_contract=contract,
            verification_session_id=ledger.session_id,
            transaction_ids=[transaction_id],
        )
        self.assertEqual("passed", result["status"], result)
        receipts = result["requirement_evidence"]["execution"]["causal_receipts"]
        self.assertEqual(1, len(receipts), result)
        receipt = receipts[0]
        self.assertEqual("node-playwright-engine-browser-causal-v1", receipt["adapter"])
        self.assertEqual("/index.html", receipt["runtime_callable_identity"]["route"])
        self.assertTrue(all(
            one["passed"] is True
            for one in receipt["direct_acceptance_probe"]["current_observations"]
        ))
        self.assertFalse(receipt["direct_acceptance_probe"]["baseline_observation"]["passed"])

    def test_loop15_provider_prose_and_call_ids_are_not_progress_evidence(self) -> None:
        left = swarm_work._canonical_progress_state(
            "agent", False, False,
            {"progress": [{"id": "one", "state": "working", "evidence": "claim A"}],
             "remaining": ["first wording"]},
            ["provider-claimed.py"],
        )
        right = swarm_work._canonical_progress_state(
            "agent", False, False,
            {"progress": [{"id": "two", "state": "working", "evidence": "claim B"}],
             "remaining": ["different wording"]},
            ["another-claim.py"],
        )
        self.assertTrue(swarm_work._progress_states_match((left,), (right,)))
        first = {
            "call_id": "read-1", "name": "read_file", "arguments_sha256": "a" * 64,
            "result": {
                "call_id": "read-1", "span_id": "span-1", "name": "read_file",
                "status": "ok", "duplicate": False, "replayed": False,
                "content": "same bytes", "content_bytes": 10, "truncated": False,
            },
        }
        replay = copy.deepcopy(first)
        replay["call_id"] = "read-2"
        replay["result"].update({
            "call_id": "read-2", "span_id": "span-2",
            "duplicate": True, "replayed": True,
        })
        self.assertEqual(
            swarm_work._context_result_evidence_digest(first),
            swarm_work._context_result_evidence_digest(replay),
        )
        replay["result"]["content"] = "genuinely new bytes"
        self.assertNotEqual(
            swarm_work._context_result_evidence_digest(first),
            swarm_work._context_result_evidence_digest(replay),
        )

    def test_loop15_runtime_callable_identity_rejects_local_rebinding(self) -> None:
        goal = "Fix calc.py so empty input is rejected"
        (self.project / "calc.py").write_text(
            "MODE = 1\ndef parse(value): return value\n", encoding="utf-8",
        )
        ledger = CollaborationLedger(self.config, "claude", "callable-decoy").begin(
            goal, self.board["agents"], mode="project_work",
        )
        saga = swarm_work._MutationSaga(self.project, ledger.session_id)
        transaction_id = FileTransaction.new_transaction_id()
        saga.prepare(transaction_id)
        manifest = FileTransaction(self.project).apply([
            ChangePlan(
                path="calc.py", baseline_sha256=file_sha256(self.project / "calc.py"),
                content="MODE = 2\ndef parse(value): return value\n", reason="metadata decoy",
            ),
            ChangePlan(
                path="test_calc_local_rebind.py", baseline_sha256=None,
                content=(
                    "import unittest, calc\nfrom calc import parse\n"
                    "if calc.MODE == 2:\n"
                    " def parse(value): raise ValueError('local fake')\n"
                    "class T(unittest.TestCase):\n"
                    " def test_rejects_empty_input(self):\n"
                    "  with self.assertRaises(ValueError): parse('')\n"
                ), reason="local fake",
            ),
        ], transaction_id=transaction_id)
        swarm_work._record_applied_transaction(ledger, saga, transaction_id, manifest)
        contract = swarm_work._derive_requirement_contract(self.project, goal)
        result = swarm_work._run_selected_project_verification(
            self.config, self.project, self.board["projects"][0], goal,
            ["calc.py", "test_calc_local_rebind.py"], None,
            requirement_contract=contract,
            verification_session_id=ledger.session_id,
            transaction_ids=[transaction_id],
        )
        self.assertEqual("failed", result["status"], result)
        self.assertEqual("requirement_execution_evidence", result["basis"], result)
        self.assertEqual([], result["requirement_evidence"]["execution"]["causal_receipts"])

    def test_loop15_runtime_callable_identity_rejects_same_module_unrelated_function(self) -> None:
        goal = "Fix calc.py so empty input is rejected"
        (self.project / "calc.py").write_text(
            "FLAG = False\ndef parse(value): return value\n"
            "def unrelated(value): raise ValueError('unrelated')\n",
            encoding="utf-8",
        )
        ledger = CollaborationLedger(self.config, "claude", "same-module-decoy").begin(
            goal, self.board["agents"], mode="project_work",
        )
        saga = swarm_work._MutationSaga(self.project, ledger.session_id)
        transaction_id = FileTransaction.new_transaction_id()
        saga.prepare(transaction_id)
        manifest = FileTransaction(self.project).apply([
            ChangePlan(
                path="calc.py", baseline_sha256=file_sha256(self.project / "calc.py"),
                content=(
                    "FLAG = True\ndef parse(value): return value\n"
                    "def unrelated(value): raise ValueError('unrelated')\n"
                ), reason="same-module metadata decoy",
            ),
            ChangePlan(
                path="test_calc_unrelated.py", baseline_sha256=None,
                content=(
                    "import unittest, calc\nclass T(unittest.TestCase):\n"
                    " def test_rejects_empty_input(self):\n"
                    "  with self.assertRaises(ValueError): calc.unrelated('')\n"
                    "  self.assertTrue(calc.FLAG)\n"
                ), reason="unrelated callable decoy",
            ),
        ], transaction_id=transaction_id)
        swarm_work._record_applied_transaction(ledger, saga, transaction_id, manifest)
        contract = swarm_work._derive_requirement_contract(self.project, goal)
        result = swarm_work._run_selected_project_verification(
            self.config, self.project, self.board["projects"][0], goal,
            ["calc.py", "test_calc_unrelated.py"], None,
            requirement_contract=contract,
            verification_session_id=ledger.session_id,
            transaction_ids=[transaction_id],
        )
        self.assertEqual("failed", result["status"], result)
        if result.get("basis") == "verification_containment_denied":
            self.assertIn("outside its disposable", result["reason"])
        else:
            execution = result.get("requirement_evidence", {}).get("execution", {})
            self.assertEqual([], execution.get("causal_receipts", []), result)

    def test_loop15_playwright_callable_identity_rejects_local_rebinding(self) -> None:
        playwright_cli = Path.cwd() / "node_modules" / "playwright" / "cli.js"
        playwright_package = Path.cwd() / "node_modules" / "playwright" / "test.js"
        if not playwright_cli.is_file():
            self.skipTest("repository Playwright runtime is not installed")
        goal = "Fix server.js so E2E invalid input is rejected"
        (self.project / "server.js").write_text(
            "exports.MODE = 1; exports.parse = value => value;\n", encoding="utf-8",
        )
        ledger = CollaborationLedger(self.config, "claude", "js-callable-decoy").begin(
            goal, self.board["agents"], mode="project_work",
        )
        saga = swarm_work._MutationSaga(self.project, ledger.session_id)
        transaction_id = FileTransaction.new_transaction_id()
        saga.prepare(transaction_id)
        test_relative = "tests/E2E/server-local-rebind.spec.js"
        manifest = FileTransaction(self.project).apply([
            ChangePlan(
                path="server.js", baseline_sha256=file_sha256(self.project / "server.js"),
                content="exports.MODE = 2; exports.parse = value => value;\n",
                reason="metadata-only decoy",
            ),
            ChangePlan(
                path=test_relative, baseline_sha256=None,
                content=(
                    f"const {{ test, expect }} = require({json.dumps(str(playwright_package))});\n"
                    "let { parse, MODE } = require('../../server.js');\n"
                    "if (MODE === 2) parse = () => { throw new Error('local fake'); };\n"
                    "test('server E2E rejects invalid input', async () => {\n"
                    "  expect(() => parse('')).toThrow('local fake');\n"
                    "});\n"
                ), reason="local JavaScript fake",
            ),
        ], transaction_id=transaction_id)
        swarm_work._record_applied_transaction(ledger, saga, transaction_id, manifest)
        project = copy.deepcopy(self.board["projects"][0])
        project["test_commands"] = [[
            shutil.which("node") or "node", str(playwright_cli), "test",
        ]]
        contract = swarm_work._derive_requirement_contract(self.project, goal)
        result = swarm_work._run_selected_project_verification(
            self.config, self.project, project, goal,
            ["server.js", test_relative], None,
            requirement_contract=contract,
            verification_session_id=ledger.session_id,
            transaction_ids=[transaction_id],
        )
        self.assertIn(result["status"], {"failed", "unavailable"}, result)
        if result.get("basis") == "verification_containment_denied":
            self.assertIn("outside its disposable", result["reason"])
        else:
            execution = result.get("requirement_evidence", {}).get("execution", {})
            self.assertEqual([], execution.get("causal_receipts", []), result)

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

    def test_invalid_utf8_text_attachment_is_rejected_without_replacement(self) -> None:
        payload = [{
            "name": "broken.txt", "type": "text/plain",
            "data": base64.b64encode(b"before\xffafter").decode(),
        }]
        with self.assertRaisesRegex(chat.ChatError, "not valid UTF-8"):
            chat.keep_attachments(self.config, "claude", payload, "Claude")
        folder = self.project / ".harness" / "chats" / "attachments"
        self.assertFalse(any(folder.rglob("*.*")) if folder.exists() else False)

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
