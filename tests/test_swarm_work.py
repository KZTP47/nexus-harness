from __future__ import annotations

import base64
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from our_harness import chat, swarm_work
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import ProviderRequest, ProviderResponse
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
        transcript = chat.read_it(self.config, "claude", "Claude")
        self.assertEqual([one.phase for one in transcript], [
            "user_prompt", "lead_plan", "agent_plan",
            "agent_plan_review", "agent_plan_review", "lead_execution",
            "agent_verification", "agent_verification", "final_answer",
        ])
        self.assertIn("plan by codex", transcript[2].text)
        self.assertIn("made-by-team.txt", transcript[-1].text)

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
            result = swarm_work.work_together(
                self.config, self.board, "agent-1", "Create marker.txt"
            )

        self.assertEqual(result["plan_rounds"], 2)
        self.assertEqual(review_calls, 4)
        self.assertFalse(result["goal_complete"])
        self.assertEqual(result["work_passes"], 0)
        self.assertFalse((self.project / "marker.txt").exists())
        transcript = chat.read_it(self.config, "claude", "Claude")
        reviews = [one.text for one in transcript if one.phase == "agent_plan_review"]
        self.assertTrue(any(
            "invalid nexus_board_plan_review_v1" in text
            and "missing ready_to_execute, remaining" in text
            for text in reviews
        ), reviews)
        self.assertTrue(any("Execution readiness: ready" in text for text in reviews))
        self.assertFalse(any(one.phase == "lead_execution" for one in transcript))
        self.assertIn("stopped before opening a project-file transaction", transcript[-1].text)

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
