"""Talking to the assistants you have hooked up.

The promise: one box, one assistant, and a conversation that is a conversation -
what was said before goes with the next thing said, and it is still there
tomorrow. Everything else here is about the ways that could go wrong: a
credential written down, a message the size of a file, one assistant that will
not answer stopping the rest, and two sends at once losing a turn.
"""

from __future__ import annotations

import copy
import json
import pathlib
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from our_harness import chat, user_questions, web_chats
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError, ResponseFormat


class Back:
    """What a provider hands back, with only the part this reads."""

    def __init__(self, text: str):
        self.text = text


class Answering:
    """A provider stood in for, so no assistant is really started."""

    def __init__(self, text: str = "It checks one small piece on its own."):
        self.text = text
        self.asked: list = []

    def complete(self, request):
        self.asked.append(request)
        return Back(self.text)


class TalkingTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.container = Path(self.temporary.name).resolve()
        self.root = self.container / "project"
        self.root.mkdir()
        (self.root / ".harness").mkdir()
        runtime = mock.patch.dict(os.environ, {
            "OUR_HARNESS_SWARM_RUN_DIR": str(self.container / "runtime")
        })
        runtime.start()
        self.addCleanup(runtime.stop)
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})

    def standing_in(self, answering: Answering):
        return mock.patch.object(chat, "create_provider", lambda config: answering)


class OneConversation(TalkingTestCase):
    def test_web_budget_reports_bridge_capability_not_api_token_fiction(self) -> None:
        limits = chat.effective_limits(self.config, "web:claude-example")
        self.assertEqual(limits["turn_timeout_seconds"], web_chats.WEB_WAIT_SECONDS)
        self.assertIsNone(limits["configured_provider_output_tokens"])
        self.assertEqual(limits["output_token_control"], "provider_page_uncontrolled")

    def test_budget_facts_match_api_and_cli_runtime_contracts(self) -> None:
        api = chat.effective_limits(self.config, "")
        self.assertEqual(api["output_token_control"], "nexus_requested_maximum")
        self.assertEqual(api["provider_capture_bytes"], 100_000_000)

        data = copy.deepcopy(DEFAULT_CONFIG)
        data["providers"] = {
            "codex": {"kind": "codex-cli", "model": "gpt-5", "command": ["codex"]},
        }
        cli_config = LoadedConfig(data, self.root, [], {})
        cli = chat.effective_limits(cli_config, "codex")
        self.assertEqual(cli["output_token_control"], "provider_cli_uncontrolled")
        self.assertIsNone(cli["configured_provider_output_tokens"])
        self.assertEqual(cli["provider_capture_bytes"], 2_000_000)
        self.assertEqual(cli["structured_capture_policy"], "schema_derived")

    def test_effective_limits_disclose_one_long_horizon_context_policy(self) -> None:
        limits = chat.effective_limits(self.config, "")
        policy = limits["long_horizon_context"]
        for key, value in chat.LONG_HORIZON_CONTEXT_POLICY.items():
            self.assertEqual(policy[key], value)
        self.assertEqual(policy["prompt_transcript_characters"], 120_000)
        self.assertEqual(policy["semantic_summary_characters"], 40_000)
        self.assertEqual(policy["phases"], [
            "team_discussion", "planning", "execution", "verification",
            "final_synthesis",
        ])
        self.assertIn("semantic projection", limits["note"])
        self.assertNotIn("never clips prompts", limits["note"].casefold())

    def test_saying_something_gets_an_answer_back(self) -> None:
        answering = Answering()
        with self.standing_in(answering):
            got = chat.say(self.config, "", "What is a unit test for?")
        self.assertEqual(got["answer"]["text"], "It checks one small piece on its own.")
        self.assertEqual([one["who"] for one in got["said"]], ["you", "them"])
        self.assertEqual(got["said"][0]["text"], "What is a unit test for?")

    def test_visual_chat_and_bounded_agent_turn_preserve_the_routed_codex_effort(self) -> None:
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["providers"] = {
            "codex": {
                "kind": "codex-cli", "model": "gpt-5.5",
                "command": ["codex"], "auth_mode": "chatgpt",
                "reasoning_effort": "high",
            },
        }
        config = LoadedConfig(data, self.root, [], {})
        provider = Answering()
        with self.standing_in(provider):
            chat.say(config, "codex", "Remember the visual chat effort")
            chat.ask_once(config, "codex", "Run one bounded agent turn")
        self.assertEqual([one.reasoning_effort for one in provider.asked], ["high", "high"])

    def test_it_is_a_conversation_and_not_a_row_of_questions(self) -> None:
        """The whole point: the second thing said knows about the first."""

        answering = Answering()
        with self.standing_in(answering):
            chat.say(self.config, "", "What is a unit test for?")
            chat.say(self.config, "", "And in three words?")
        sent = answering.asked[-1].messages
        self.assertEqual(
            [one["role"] for one in sent], ["user", "assistant", "user"]
        )
        self.assertEqual(sent[0]["content"], "What is a unit test for?")
        self.assertEqual(sent[-1]["content"], "And in three words?")

    def test_board_agent_can_return_structured_questions_and_resume_in_the_same_chat(self) -> None:
        envelope = {
            "questions": [{
                "id": "procurement",
                "prompt": "How should device procurement work?",
                "options": [{
                    "label": "Request-based",
                    "description": "Employees submit requests for fulfillment.",
                    "recommended": True,
                }, {
                    "label": "Self-service catalog",
                    "description": "Employees order from an approved catalog.",
                    "recommended": False,
                }],
                "multiple": False,
                "allow_other": True,
            }],
        }
        answering = Answering(
            "I need one product decision.\n```nexus-user-input\n"
            + json.dumps(envelope) + "\n```"
        )
        speaker = {"id": "agent-1", "name": "Claude", "who": "claude"}
        with self.standing_in(answering):
            paused = chat.say(
                self.config, "", "Write the PRD", filed_as="pair-chat",
                speaker=speaker, recipients=[speaker],
            )

        self.assertEqual(paused["status"], "waiting_for_user")
        self.assertEqual(paused["answer"]["text"], "I need one product decision.")
        self.assertEqual(paused["questions"][0]["id"], "procurement")
        self.assertTrue(paused["questions"][0]["options"][0]["recommended"])
        saved = chat.read_it(self.config, "", "pair-chat")
        self.assertEqual(saved[-1].questions, paused["questions"])
        self.assertIn("NEXUS USER-INPUT CAPABILITY", answering.asked[0].dynamic_context)

        answering.text = "Thanks, continuing with request-based procurement."
        with self.standing_in(answering):
            chat.say(
                self.config, "", "Request-based.", filed_as="pair-chat",
                speaker=speaker, recipients=[speaker],
            )
        history = "\n".join(message["content"] for message in answering.asked[-1].messages)
        self.assertIn("How should device procurement work?", history)
        self.assertIn("Request-based", history)

    def test_invalid_question_envelope_is_kept_as_agent_text(self) -> None:
        text = "Question\n```nexus-user-input\n{not json}\n```"
        visible, questions = user_questions.extract(text)
        self.assertEqual(visible, text)
        self.assertEqual(questions, [])

    def test_it_is_still_there_after_the_panel_is_closed(self) -> None:
        with self.standing_in(Answering()):
            chat.say(self.config, "", "Remember this")
        # A brand new reading of the same project, as if the panel restarted.
        again = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        kept = chat.read_it(again, "")
        self.assertEqual([one.who for one in kept], ["you", "them"])
        self.assertEqual(kept[0].text, "Remember this")

    def test_full_transcript_survives_while_provider_and_ui_views_stay_bounded(self) -> None:
        turns = [
            chat.Said(
                who="you" if number % 2 == 0 else "them",
                text=f"historical-{number}", at=f"2026-01-01T00:00:{number:02d}Z",
            )
            for number in range(chat.MOST_KEPT + 10)
        ]
        chat._keep_it(self.config, "", turns)
        answering = Answering("new answer")
        with self.standing_in(answering):
            result = chat.say(self.config, "", "new question")

        saved = chat.read_it(self.config, "")
        self.assertEqual(len(saved), chat.MOST_KEPT + 12)
        self.assertEqual(saved[0].text, "historical-0")
        self.assertEqual(saved[-1].text, "new answer")
        self.assertEqual(len(answering.asked[-1].messages), chat.MOST_KEPT + 2)
        self.assertIn(
            "NEXUS CHAT-HISTORY PROJECTION",
            answering.asked[-1].messages[0]["content"],
        )
        self.assertEqual(
            answering.asked[-1].messages[1]["content"], "historical-10"
        )
        self.assertEqual(len(result["said"]), chat.MOST_KEPT)

    def test_legacy_snapshot_migrates_to_a_physically_append_only_hash_chain(self) -> None:
        where = chat.where_it_is_kept(self.config, "")
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(json.dumps([
            chat.Said("you", "legacy", "2026-01-01T00:00:00Z").to_dict()
        ]), encoding="utf-8")
        self.assertEqual(chat.read_it(self.config, "")[0].text, "legacy")
        events = where.with_suffix(".events.jsonl")
        first = events.read_bytes()
        with self.standing_in(Answering("new")):
            chat.say(self.config, "", "question")
        after = events.read_bytes()
        self.assertTrue(after.startswith(first))
        self.assertGreater(len(after.splitlines()), len(first.splitlines()))
        self.assertEqual([one.text for one in chat.read_it(self.config, "")], [
            "legacy", "question", "new",
        ])

    def test_corrupt_legacy_chat_is_preserved_without_a_partial_migration(self) -> None:
        where = chat.where_it_is_kept(self.config, "")
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(json.dumps([
            chat.Said("you", "valid first turn", "2026-01-01T00:00:00Z").to_dict(),
            {"who": "unknown", "text": "invalid second turn", "at": "later"},
        ]), encoding="utf-8")
        before = where.read_bytes()
        events = where.with_suffix(".events.jsonl")
        anchor = chat._transcript_anchor_path(events)

        with self.assertRaisesRegex(chat.ChatError, "turn 2 has no valid speaker/text"):
            chat.read_it(self.config, "")

        self.assertEqual(where.read_bytes(), before)
        self.assertFalse(events.exists())
        self.assertFalse(anchor.exists())

    def test_missing_anchor_after_migration_crash_recovers_without_duplicate_turns(self) -> None:
        where = chat.where_it_is_kept(self.config, "")
        where.parent.mkdir(parents=True, exist_ok=True)
        legacy = chat.Said(
            "you", "one legacy turn", "2026-01-01T00:00:00Z"
        )
        where.write_text(json.dumps([legacy.to_dict()]), encoding="utf-8")
        events = where.with_suffix(".events.jsonl")
        anchor = chat._transcript_anchor_path(events)

        with mock.patch.object(
            chat, "_write_transcript_anchor",
            side_effect=OSError("crash before anchor replace"),
        ), self.assertRaisesRegex(OSError, "crash before anchor replace"):
            chat.read_it(self.config, "")

        event_bytes = events.read_bytes()
        self.assertFalse(anchor.exists())
        recovered = chat.read_it(self.config, "")

        self.assertEqual([one.to_dict() for one in recovered], [legacy.to_dict()])
        self.assertEqual(events.read_bytes(), event_bytes)
        self.assertTrue(anchor.is_file())
        self.assertEqual(len(events.read_text(encoding="utf-8").splitlines()), 1)

    def test_rehashed_transcript_rewrite_fails_keyed_integrity_without_repair(self) -> None:
        with self.standing_in(Answering("answer")):
            chat.say(self.config, "", "original")
        events = chat.where_it_is_kept(self.config, "").with_suffix(".events.jsonl")
        records = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
        records[0]["turns"][0]["text"] = "attacker rewrite"
        previous = ""
        for event in records:
            event["previous_hash"] = previous
            event["hash"] = chat._transcript_event_hash(event)
            previous = event["hash"]
        rewritten = "".join(
            json.dumps(one, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for one in records
        )
        events.write_text(rewritten, encoding="utf-8")

        with self.assertRaisesRegex(chat.ChatError, "keyed integrity"):
            chat.read_it(self.config, "")
        self.assertEqual(events.read_text(encoding="utf-8"), rewritten)
        self.assertTrue(any((self.container / "runtime" / "quarantine").glob("*.json")))

    def test_cross_process_writers_do_not_lose_transcript_turns(self) -> None:
        script = r'''import copy, sys
from pathlib import Path
from our_harness import chat
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
root = Path(sys.argv[1]).resolve()
config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), root, [], {})
for n in range(8):
    chat.keep_exchange(config, "", f"p{sys.argv[2]}-{n}", f"a{sys.argv[2]}-{n}")
'''
        env = dict(os.environ, PYTHONPATH=str(Path.cwd() / "src"))
        stop_reading = threading.Event()
        reader_errors: list[BaseException] = []
        reads = [0]

        def read_repeatedly() -> None:
            while not stop_reading.is_set():
                try:
                    chat.read_it(self.config, "")
                    reads[0] += 1
                except BaseException as exc:  # captured: thread failures must fail the test
                    reader_errors.append(exc)
                    stop_reading.set()

        readers = [threading.Thread(target=read_repeatedly) for _ in range(3)]
        for reader in readers:
            reader.start()
        processes = [
            subprocess.Popen([sys.executable, "-c", script, str(self.root), str(index)], env=env)
            for index in range(3)
        ]
        try:
            self.assertTrue(all(process.wait(timeout=30) == 0 for process in processes))
        finally:
            stop_reading.set()
            for reader in readers:
                reader.join(5)
        self.assertGreater(reads[0], 3)
        self.assertEqual(reader_errors, [])
        turns = chat.read_it(self.config, "")
        self.assertEqual(len(turns), 48)
        self.assertEqual(len({one.text for one in turns}), 48)

    def test_starting_again_throws_it_away(self) -> None:
        with self.standing_in(Answering()):
            chat.say(self.config, "", "Something")
        self.assertTrue(chat.read_it(self.config, ""))
        said = chat.start_again(self.config, "")
        self.assertIn("gone", said)
        self.assertEqual(chat.read_it(self.config, ""), [])

    def test_starting_again_on_a_conversation_that_never_was(self) -> None:
        self.assertIn("gone", chat.start_again(self.config, ""))

    def test_two_assistants_keep_their_own_conversations(self) -> None:
        self.config.data["providers"] = {
            "one": {"kind": "claude-cli", "model": "a"},
            "two": {"kind": "copilot-cli", "model": "b"},
        }
        with self.standing_in(Answering()):
            chat.say(self.config, "one", "Only to the first")
        self.assertTrue(chat.read_it(self.config, "one"))
        self.assertEqual(chat.read_it(self.config, "two"), [])

    def test_claude_chat_names_its_real_home_and_not_claude_desktop(self) -> None:
        self.config.data["providers"] = {
            "claude": {"kind": "claude-cli", "model": "claude-sonnet-4-5"},
        }
        destination = chat.chat_destination(self.config, "claude", "Claude2")
        self.assertEqual(destination["owner_label"], "Nexus Harness")
        self.assertEqual(destination["provider_label"], "Claude Code command line")
        self.assertEqual(destination["provider_app_name"], "Claude Desktop")
        self.assertFalse(destination["provider_app_linked"])
        self.assertIn("will not contain these messages", destination["explanation"])
        self.assertEqual(
            destination["transcript_path"],
            chat.where_it_is_kept(self.config, "claude", "Claude2")
            .relative_to(self.root).as_posix(),
        )

    def test_every_supported_provider_says_the_nexus_chat_is_not_an_app_chat(self) -> None:
        for kind in (
            "openai", "anthropic", "gemini", "ollama", "local", "openai-compatible",
            "claude-cli", "copilot-cli", "assistant-cli", "gemini-cli", "codex-cli",
            "m365-copilot",
        ):
            with self.subTest(kind=kind):
                self.config.data["providers"] = {
                    "one": {"kind": kind, "model": "model"},
                }
                destination = chat.chat_destination(self.config, "one", "Agent")
                self.assertEqual(destination["owner"], "nexus")
                self.assertFalse(destination["provider_app_linked"])
                self.assertTrue(destination["provider_label"])
                self.assertTrue(destination["explanation"])

    def test_a_removed_provider_route_is_named_without_losing_the_saved_chat(self) -> None:
        destination = chat.chat_destination(self.config, "gone", "Agent")
        self.assertFalse(destination["connected"])
        self.assertIn("Missing route", destination["provider_label"])
        self.assertTrue(destination["transcript_path"].startswith(".harness/chats/"))

    def test_a_live_web_chat_is_a_connected_destination_with_its_saved_transcript(self) -> None:
        broker = web_chats.WebChatBroker()
        broker.heartbeat([{
            "id": "gemini-abcdef123456", "provider": "gemini",
            "title": "Release helper", "url": "https://gemini.google.com/app/one",
        }])
        with mock.patch.object(web_chats, "_active", broker):
            destination = chat.chat_destination(
                self.config, "web:gemini-abcdef123456", "Gemini and Codex"
            )
        self.assertTrue(destination["connected"])
        self.assertEqual(destination["provider_kind"], "web-chat")
        self.assertEqual(destination["web_chat_id"], "gemini-abcdef123456")
        self.assertEqual(destination["url"], "https://gemini.google.com/app/one")
        self.assertIn("Gemini web", destination["provider_label"])
        self.assertTrue(destination["transcript_path"].startswith(".harness/chats/"))

    def test_a_disconnected_web_chat_stays_identifiable_and_reconnectable(self) -> None:
        with mock.patch.object(web_chats, "_active", web_chats.WebChatBroker()):
            destination = chat.chat_destination(
                self.config, "web:gemini-abcdef123456", "Gemini and Codex"
            )
        self.assertFalse(destination["connected"])
        self.assertEqual(destination["web_chat_id"], "gemini-abcdef123456")
        self.assertIn("Disconnected web chat", destination["provider_label"])
        self.assertIn("Reconnect", destination["explanation"])

    def test_the_project_default_provider_names_the_same_nexus_chat_home(self) -> None:
        destination = chat.chat_destination(self.config, "")
        self.assertTrue(destination["connected"])
        self.assertEqual(destination["route"], "project default")
        self.assertEqual(destination["owner"], "nexus")
        self.assertTrue(destination["transcript_path"].endswith("the-usual-one.json"))

    def test_all_turns_are_kept_but_only_recent_turns_are_prompted(self) -> None:
        """Durable history is lossless while a model prompt remains bounded."""

        answering = Answering()
        with self.standing_in(answering):
            for number in range(chat.MOST_KEPT):
                chat.say(self.config, "", f"Message {number}")
        kept = chat.read_it(self.config, "")
        self.assertEqual(len(kept), chat.MOST_KEPT * 2)
        self.assertEqual(len(answering.asked[-1].messages), chat.MOST_KEPT + 2)
        self.assertIn(
            "NEXUS CHAT-HISTORY PROJECTION",
            answering.asked[-1].messages[0]["content"],
        )
        self.assertIn(f"Message {chat.MOST_KEPT - 1}", kept[-2].text)

    def test_history_character_budget_omits_only_complete_turns_with_a_reference(self) -> None:
        huge = "begin-very-long-turn\n" + (
            "x" * (chat.CHAT_HISTORY_PROMPT_CHARACTERS + 1)
        ) + "\nend-very-long-turn"
        eligible = [
            chat.Said("them", huge, "now"),
            chat.Said("you", "small complete turn", "now"),
        ]
        messages = chat._project_chat_history(
            eligible, speaker=None, filed_as="history-budget", route="claude"
        )
        shown = "\n".join(one["content"] for one in messages)
        self.assertIn("NEXUS CHAT-HISTORY PROJECTION", shown)
        self.assertIn("No turn was sliced", shown)
        self.assertIn("small complete turn", shown)
        self.assertNotIn("begin-very-long-turn", shown)
        self.assertNotIn("end-very-long-turn", shown)

    def test_it_is_told_it_cannot_do_anything(self) -> None:
        """An assistant that thinks it can read files offers to read files."""

        answering = Answering()
        with self.standing_in(answering):
            chat.say(self.config, "", "Anything")
        told = answering.asked[0].system_prefix
        self.assertIn("cannot read their files", told)
        self.assertFalse(getattr(answering.asked[0], "tools", None))


class WhatItRefuses(TalkingTestCase):
    def test_an_empty_message_is_refused(self) -> None:
        with self.assertRaises(chat.ChatError) as caught:
            chat.say(self.config, "", "   ")
        self.assertIn("Type something", str(caught.exception))

    def test_a_message_the_size_of_a_file_is_refused(self) -> None:
        with self.assertRaises(chat.ChatError) as caught:
            chat.say(self.config, "", "a" * (chat.MOST_LETTERS + 1))
        self.assertIn("point at a file", str(caught.exception))
        self.assertIn("did not truncate", str(caught.exception))

    def test_a_message_with_a_control_character_is_refused(self) -> None:
        with self.assertRaises(chat.ChatError):
            chat.say(self.config, "", "Is this used\x00anywhere?")

    def test_tabs_and_newlines_are_allowed(self) -> None:
        with self.standing_in(Answering()):
            got = chat.say(self.config, "", "One line\nand\ta second")
        self.assertTrue(got["answer"]["text"])

    def test_a_route_nobody_set_up_cannot_be_talked_to(self) -> None:
        with self.assertRaises(chat.ChatError) as caught:
            chat.say(self.config, "nobody-made-this", "Hello")
        self.assertIn("cannot be reached", str(caught.exception))

    def test_a_name_that_could_reach_outside_the_project_is_refused(self) -> None:
        for bad in ("../secrets", "..\\secrets", "a/b", "a\\b", ""):
            with self.subTest(name=bad):
                if bad == "":
                    # The empty one is the usual assistant, and is filed under
                    # a name of its own rather than refused.
                    self.assertTrue(
                        str(chat.where_it_is_kept(self.config, bad)).endswith(
                            f"{chat.THE_USUAL_ONE}.json"
                        )
                    )
                    continue
                with self.assertRaises(chat.ChatError):
                    chat.where_it_is_kept(self.config, bad)

    def test_the_name_kept_for_the_unnamed_one_is_its_own(self) -> None:
        """A route really called the-usual-one is not the usual one."""

        self.assertEqual(chat._filed_under(""), chat.THE_USUAL_ONE)
        self.assertNotEqual(chat._filed_under(chat.THE_USUAL_ONE), chat.THE_USUAL_ONE)
        self.config.data["providers"] = {
            chat.THE_USUAL_ONE: {"kind": "claude-cli", "model": "a"}
        }
        with self.standing_in(Answering()):
            chat.say(self.config, chat.THE_USUAL_ONE, "only for the named one")
            chat.say(self.config, "", "only for the usual one")
        named = chat.read_it(self.config, chat.THE_USUAL_ONE)
        usual = chat.read_it(self.config, "")
        self.assertEqual(len(named), 2, named)
        self.assertEqual(len(usual), 2, usual)
        self.assertIn("named", named[0].text)
        self.assertIn("usual", usual[0].text)

    def test_an_answer_of_nothing_is_said_plainly(self) -> None:
        with self.standing_in(Answering("   ")):
            with self.assertRaises(chat.ChatError) as caught:
                chat.say(self.config, "", "Anything")
        self.assertIn("nothing at all", str(caught.exception))

    def test_an_over_limit_answer_is_rejected_without_saving_a_fragment(self) -> None:
        with self.standing_in(Answering("x" * (chat.LONGEST_ANSWER + 500))):
            with self.assertRaises(chat.ChatError) as caught:
                chat.say(self.config, "", "Anything")
        self.assertIn("did not save or truncate", str(caught.exception))
        self.assertEqual(chat.read_it(self.config, ""), [])

    def test_the_incident_sized_prompt_and_answer_are_preserved_end_to_end(self) -> None:
        prompt = "goal:" + ("p" * 6_253)
        answer = "result:" + ("r" * 35_782)
        provider = Answering(answer)
        with self.standing_in(provider):
            got = chat.say(self.config, "", prompt)
        self.assertEqual(provider.asked[0].messages[-1]["content"], prompt)
        self.assertEqual(got["answer"]["text"], answer)
        self.assertEqual(chat.read_it(self.config, "")[-1].text, answer)

    def test_long_failed_turn_keeps_the_canonical_user_goal(self) -> None:
        goal = "g" * chat.MOST_LETTERS
        chat.keep_failed_exchange(
            self.config, "", goal, "provider unavailable", state="failed"
        )
        turns = chat.read_it(self.config, "")
        self.assertEqual(turns[0].text, goal)
        self.assertIn("provider unavailable", turns[-1].text)

    def test_failed_team_turn_keeps_every_contribution_and_bounded_redacted_cause(self) -> None:
        secret = "secret-value-that-must-never-survive"
        cause = ("first cause words " + ("x" * 70_000)
                 + f" Bearer {secret} FINAL-CAUSE-TAIL")
        contributions = [
            {
                "speaker_id": f"agent-{number}",
                "speaker_name": f"Agent {number}",
                "text": f"contribution-{number}",
            }
            for number in range(50)
        ]
        chat.keep_failed_exchange(
            self.config, "", "long team failure", cause,
            state="failed", contributions=contributions,
        )
        turns = chat.read_it(self.config, "")
        self.assertEqual(len(turns), 52)
        self.assertEqual(
            [one.text for one in turns[1:-1]],
            [f"contribution-{number}" for number in range(50)],
        )
        self.assertNotIn(secret, turns[-1].text)
        self.assertIn("[REDACTED]", turns[-1].text)
        self.assertIn("NEXUS_REDACTED_CAUSE_BOUNDARY", turns[-1].text)
        self.assertIn("FINAL-CAUSE-TAIL", turns[-1].text)

    def test_structured_cli_gets_exactly_one_repair_and_never_leaks_raw_failure(self) -> None:
        class Twice(Answering):
            structured_retry_is_safe = True
            def __init__(self, answers):
                super().__init__("")
                self.answers = iter(answers)

            def complete(self, request):
                self.asked.append(request)
                return Back(next(self.answers))

        wanted = ResponseFormat("demo", {
            "type": "object", "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"], "additionalProperties": False,
        })
        secret = "sk-abcdef0123456789abcdef01"
        provider = Twice([f"not-json {secret}", json.dumps({"ok": True})])
        with self.standing_in(provider):
            got = chat.ask_once(self.config, "", "Do it", response_format=wanted)
        self.assertEqual(json.loads(got["text"]), {"ok": True})
        self.assertEqual(len(provider.asked), 2)
        self.assertNotIn(secret, json.dumps(provider.asked[1].messages))

        provider = Twice([f"not-json {secret}", f"still-not-json {secret}"])
        with self.standing_in(provider):
            with self.assertRaises(chat.ChatError) as caught:
                chat.ask_once(self.config, "", "Do it", response_format=wanted)
        self.assertEqual(len(provider.asked), 2)
        self.assertNotIn(secret, str(caught.exception))

    def test_structured_provider_not_proven_pure_is_never_retried(self) -> None:
        provider = Answering("not-json")
        wanted = ResponseFormat("demo", {
            "type": "object", "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"], "additionalProperties": False,
        })
        with self.standing_in(provider):
            with self.assertRaisesRegex(chat.ChatError, "not proven side-effect-free"):
                chat.ask_once(self.config, "", "Do it", response_format=wanted)
        self.assertEqual(len(provider.asked), 1)

    def test_a_refusal_is_shown_as_a_sentence_not_a_page(self) -> None:
        def wont(config):
            raise HarnessError(
                'claude said: {"type": "result", "is_error": true, '
                '"result": "Your organisation does not have access.", '
                '"usage": {"input_tokens": 4}}'
            )

        with mock.patch.object(chat, "create_provider", wont):
            with self.assertRaises(chat.ChatError) as caught:
                chat.say(self.config, "", "Anything")
        said = str(caught.exception)
        self.assertIn("does not have access", said)
        self.assertNotIn("input_tokens", said)

    def test_an_unreadable_conversation_is_preserved_and_fails_visibly(self) -> None:
        where = chat.where_it_is_kept(self.config, "")
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text("this is not json at all", encoding="utf-8")
        before = where.read_bytes()
        with self.assertRaisesRegex(
            chat.ChatError, "did not pretend the chat was empty",
        ):
            chat.read_it(self.config, "")
        with self.standing_in(Answering()), self.assertRaisesRegex(
            chat.ChatError, "did not pretend the chat was empty",
        ):
            chat.say(self.config, "", "Carry on anyway")
        self.assertEqual(where.read_bytes(), before)


class NothingLeaks(TalkingTestCase):
    def test_credentials_never_reach_the_assistant_or_the_record(self) -> None:
        answering = Answering("Your key sk-abcdef0123456789abcdef01 is in the file.")
        with self.standing_in(answering):
            got = chat.say(
                self.config, "", "Is sk-abcdef0123456789abcdef01 used anywhere?"
            )
        sent = answering.asked[0].messages[-1]["content"]
        self.assertNotIn("sk-abcdef0123456789abcdef01", sent)
        self.assertNotIn("sk-abcdef0123456789abcdef01", got["answer"]["text"])
        # And nothing with a credential in it is written to the file either.
        written = chat.where_it_is_kept(self.config, "").read_text(encoding="utf-8")
        self.assertNotIn("sk-abcdef0123456789abcdef01", written)


class WhenSomethingWillNotAnswer(TalkingTestCase):
    def test_a_key_in_the_reason_it_refused_never_reaches_the_screen(self) -> None:
        """A key that is wrong comes back inside the reason it was refused."""

        def wont(config):
            raise HarnessError(
                "401 Unauthorized: Incorrect API key provided: "
                "sk-abcdef0123456789abcdef01"
            )

        with mock.patch.object(chat, "create_provider", wont):
            with self.assertRaises(chat.ChatError) as caught:
                chat.say(self.config, "", "Anything")
        said = str(caught.exception)
        self.assertNotIn("sk-abcdef0123456789abcdef01", said)
        self.assertIn("Unauthorized", said, "and the reason is still readable")

    def test_the_same_when_it_is_the_answer_that_fails(self) -> None:
        class Fussy:
            def complete(self, request):
                raise HarnessError(
                    "rejected: token sk-abcdef0123456789abcdef01 is not valid"
                )

        with mock.patch.object(chat, "create_provider", lambda config: Fussy()):
            with self.assertRaises(chat.ChatError) as caught:
                chat.say(self.config, "", "Anything")
        self.assertNotIn("sk-abcdef0123456789abcdef01", str(caught.exception))

    def test_a_key_never_reaches_the_screen_from_asking_everyone_either(self) -> None:
        self.config.data["providers"] = {"one": {"kind": "claude-cli", "model": "a"}}

        class Fussy:
            def complete(self, request):
                raise HarnessError("rejected: sk-abcdef0123456789abcdef01")

        with mock.patch.object(chat, "create_provider", lambda config: Fussy()):
            answers = chat.ask_everyone(self.config, "Anything")
        self.assertNotIn("sk-abcdef0123456789abcdef01", answers[0]["went_wrong"])

    def test_ordinary_words_with_angle_brackets_come_back_whole(self) -> None:
        """Real error text is full of them, and cutting them out loses the point."""

        for words in (
            "Error: type mismatch, expected List<Item> but got Array<int>",
            "bash: <stdin>: syntax error near unexpected token",
            "rate limited, retry after <2026-08-18T10:00:00Z>",
            "use <name> to set it",
        ):
            with self.subTest(words=words):
                self.assertEqual(chat._without_markup(words), words)

    def test_a_real_page_still_comes_back_as_one_line(self) -> None:
        page = (
            "Provider HTTP 501: <!DOCTYPE HTML><html><head><title>Error response"
            "</title></head><body><h1>Error response</h1>"
            "<p>Message: Unsupported method.</p></body></html>"
        )
        said = chat._without_markup(page)
        self.assertNotIn("<", said)
        self.assertIn("Error response", said)


class WhatHappenedLastTimeTests(TalkingTestCase):
    """Ready used to mean "there is a route written down for it".

    Somebody opened the board, saw every agent marked ready and a green line
    saying so, typed a message, and was told the service had refused it. The
    board had never asked anything. It cannot ask for real before drawing -
    that is a paid message per agent every time anybody looks at the tab - but
    it does not have to forget what happened the last time somebody did.
    """

    def setUp(self) -> None:
        super().setUp()
        self.config.data["providers"] = {"claude": {"kind": "claude-cli", "model": "m"}}

    def only_one(self):
        return [one for one in chat.already_set_up(self.config) if one["route"] == "claude"][0]

    def test_a_route_nobody_has_had_trouble_with_is_ready(self) -> None:
        self.assertTrue(self.only_one()["ready"])

    def test_a_route_that_was_turned_down_says_so_before_anybody_types(self) -> None:
        chat._write_down_that_it_would_not(
            self.config, "claude", "your organisation has Claude Code turned off")
        route = self.only_one()
        self.assertIn("Anthropic rejected", route["trouble_last_time"])
        self.assertNotIn("organisation", route["trouble_last_time"])

    def test_a_gemini_route_missing_its_required_project_is_repairable(self) -> None:
        self.config.data["providers"] = {
            "gemini": {"kind": "gemini-cli", "model": "gemini-2.5-pro"}
        }
        chat._write_down_that_it_would_not(
            self.config,
            "gemini",
            "This account requires setting the GOOGLE_CLOUD_PROJECT env var",
        )
        route = chat.already_set_up(self.config)[0]
        self.assertFalse(route["ready"])
        self.assertIn("project id", route["why_not"])
        self.assertIn("Repair connection", route["how_to_fix_it"])
        self.assertIn("Set Cloud project", route["how_to_fix_it"])
        self.assertIn("installed", route["trouble_last_time"])
        self.assertIn("reached", route["trouble_last_time"])
        self.assertNotIn("GOOGLE_CLOUD_PROJECT", route["trouble_last_time"])

    def test_configuring_the_missing_gemini_project_reenables_one_verification(self) -> None:
        self.config.data["providers"] = {
            "gemini": {"kind": "gemini-cli", "model": "gemini-2.5-pro"}
        }
        chat._write_down_that_it_would_not(
            self.config,
            "gemini",
            "This account requires setting the GOOGLE_CLOUD_PROJECT env var",
        )
        self.config.data["providers"]["gemini"]["google_project"] = "configured-project"

        route = chat.already_set_up(self.config)[0]

        self.assertTrue(route["ready"])
        self.assertEqual(route["why_not"], "")
        self.assertEqual(route["trouble_last_time"], "")
        self.assertNotIn("gemini", chat.what_would_not_answer(self.config))

    def test_collaboration_remembers_a_gemini_project_failure_and_stops_relaunching(self) -> None:
        self.config.data["providers"] = {
            "gemini": {"kind": "gemini-cli", "model": "gemini-2.5-pro"}
        }

        class MissingProject:
            def __init__(self) -> None:
                self.calls = 0

            def complete(self, _request):
                self.calls += 1
                raise HarnessError(
                    "This account requires setting the GOOGLE_CLOUD_PROJECT env var"
                )

        provider = MissingProject()
        with self.standing_in(provider), self.assertRaises(chat.ChatError):
            chat.ask_once(self.config, "gemini", "hello")

        route = chat.already_set_up(self.config)[0]
        self.assertEqual(provider.calls, 1)
        self.assertFalse(route["ready"])
        self.assertIn("Project ID", route["trouble_last_time"])

        with self.standing_in(provider), self.assertRaisesRegex(
            chat.ChatError, "did not launch another doomed provider turn"
        ):
            chat.ask_once(self.config, "gemini", "try again")
        self.assertEqual(provider.calls, 1)

    def test_a_claude_subscription_refusal_stays_retryable_and_private(self) -> None:
        chat._write_down_that_it_would_not(
            self.config,
            "claude",
            "Your organization has disabled Claude subscription access for Claude Code",
        )
        route = self.only_one()
        self.assertTrue(route["ready"])
        self.assertFalse(route["setup_blocked"])
        self.assertTrue(route["retryable"])
        self.assertEqual(route["connection_state"], "needs attention")
        self.assertIn("claude auth logout", route["how_to_fix_it"])
        self.assertNotIn("administrator deliberately disabled", route["trouble_last_time"])

    def test_account_identity_is_removed_from_new_and_old_refusals(self) -> None:
        private = (
            "It says of itself: signed in, somebody@example.test, A Company, pro. "
            "Ask an administrator."
        )
        chat._write_down_that_it_would_not(self.config, "claude", private)
        kept = chat.what_would_not_answer(self.config)["claude"]["why"]
        self.assertNotIn("somebody@example.test", kept)
        self.assertNotIn("A Company", kept)
        self.assertNotIn(", pro", kept)
        self.assertIn("It says of itself: signed in.", kept)

        # Old files written by an earlier build are cleaned on the way out too.
        where = chat._where_the_noes_are(self.config)
        held = json.loads(where.read_text(encoding="utf-8"))
        held["claude"]["why"] = private
        where.write_text(json.dumps(held), encoding="utf-8")
        read_back = chat.what_would_not_answer(self.config)["claude"]["why"]
        self.assertNotIn("somebody@example.test", read_back)
        self.assertNotIn("A Company", read_back)

    def test_what_happened_last_time_does_not_stop_it_being_tried(self) -> None:
        """The whole point, and the thing this got wrong the first time.

        Hung on the word ready, one bad minute on a Tuesday stopped runs from
        starting, dropped a route out of asking everyone with nobody told it
        had not been asked, and swapped the conversation somebody was looking
        at for a different one. The note itself said "send something and it
        will try again" while nothing would send anything.
        """

        chat._write_down_that_it_would_not(self.config, "claude", "it said no")
        self.assertTrue(self.only_one()["ready"])
        with self.standing_in(Answering("here you go")):
            said = chat.say(self.config, "claude", "hello")
        self.assertTrue(said["said"], "it was asked, and it answered")

    def test_asking_everyone_still_asks_the_one_that_had_trouble(self) -> None:
        self.config.data["providers"]["copilot"] = {"kind": "copilot-cli", "model": "m"}
        chat._write_down_that_it_would_not(self.config, "claude", "it said no")
        with self.standing_in(Answering("here you go")):
            answers = chat.ask_everyone(self.config, "what do you think?")
        self.assertEqual(sorted(one["route"] for one in answers), ["claude", "copilot"])

    def test_it_is_remembered_when_something_is_really_asked(self) -> None:
        class WouldNot:
            def complete(self, request):
                raise HarnessError("the service turned this down")

        with self.standing_in(WouldNot()), self.assertRaises(chat.ChatError):
            chat.say(self.config, "claude", "hello")
        self.assertIn("turned this down", self.only_one()["trouble_last_time"])

    def test_anything_getting_through_clears_it(self) -> None:
        chat._write_down_that_it_would_not(self.config, "claude", "no")
        with self.standing_in(Answering("here you go")):
            chat.say(self.config, "claude", "hello")
        self.assertEqual(self.only_one()["trouble_last_time"], "")

    def test_a_collaboration_reply_clears_the_old_route_failure_too(self) -> None:
        chat._write_down_that_it_would_not(
            self.config, "claude", "old command-line configuration error"
        )
        with self.standing_in(Answering("here you go")):
            answer = chat.ask_once(self.config, "claude", "hello")
        self.assertEqual(answer["text"], "here you go")
        self.assertEqual(self.only_one()["trouble_last_time"], "")

    def test_current_codex_isolation_migrates_the_obsolete_config_refusal(self) -> None:
        self.config.data["providers"] = {
            "codex": {
                "kind": "codex-cli", "model": "gpt-5.5",
                "command": ["C:/Tools/codex.exe"], "auth_mode": "chatgpt",
            }
        }
        # This is the shape written by builds before failure context became a
        # versioned engine contract. It deliberately has no fingerprint.
        where = chat._where_the_noes_are(self.config)
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(json.dumps({
            "codex": {
                "why": "Codex CLI login status failed (1): Error loading configuration: "
                       "config.toml:3:26: unknown variant `ultra`",
                "when": time.time(),
                "at": "2026-08-29T21:42:57Z",
            }
        }), encoding="utf-8")

        route = chat.already_set_up(self.config)[0]

        self.assertTrue(route["ready"])
        self.assertEqual(route["connection_state"], "connected")
        self.assertEqual(route["trouble_last_time"], "")
        self.assertNotIn("codex", chat.what_would_not_answer(self.config))

    def test_codex_isolation_does_not_hide_an_unrelated_real_refusal(self) -> None:
        self.config.data["providers"] = {
            "codex": {
                "kind": "codex-cli", "model": "gpt-5.5",
                "command": ["C:/Tools/codex.exe"], "auth_mode": "chatgpt",
            }
        }
        chat._write_down_that_it_would_not(
            self.config, "codex", "ChatGPT authentication required",
        )

        route = chat.already_set_up(self.config)[0]

        self.assertEqual(route["connection_state"], "needs attention")
        self.assertIn("authentication required", route["trouble_last_time"])
        self.assertIn("codex", chat.what_would_not_answer(self.config))

    def test_versioned_failure_survives_restart_when_route_and_engine_are_identical(self) -> None:
        self.config.data["providers"]["claude"]["command"] = ["C:/Tools/claude.exe"]
        chat._write_down_that_it_would_not(self.config, "claude", "Temporary provider refusal")
        restarted = LoadedConfig(copy.deepcopy(self.config.data), self.root, [], {})

        refusal = chat.what_would_not_answer(restarted)["claude"]

        self.assertEqual(refusal["failure_context_version"], chat.FAILURE_CONTEXT_VERSION)
        self.assertEqual(len(refusal["route_fingerprint_sha256"]), 64)
        self.assertEqual(
            refusal["transport_contract"],
            chat.PROVIDER_TRANSPORT_CONTRACT_REVISIONS["claude-cli"],
        )

    def test_machine_specific_route_change_invalidates_failure_without_manual_cleanup(self) -> None:
        old_command = "C:/Users/Another User/AppData/Local/claude.exe"
        self.config.data["providers"]["claude"]["command"] = [old_command]
        chat._write_down_that_it_would_not(self.config, "claude", "Temporary provider refusal")
        raw = chat._where_the_noes_are(self.config).read_text(encoding="utf-8")
        self.assertNotIn(old_command, raw, "the portable context must store only a digest")

        moved_data = copy.deepcopy(self.config.data)
        moved_data["providers"]["claude"]["command"] = ["D:/Portable Apps/claude.exe"]
        moved_computer = LoadedConfig(moved_data, self.root, [], {})

        self.assertNotIn("claude", chat.what_would_not_answer(moved_computer))
        self.assertEqual(chat.already_set_up(moved_computer)[0]["trouble_last_time"], "")

    def test_engine_transport_revision_invalidates_failure_for_every_installation(self) -> None:
        chat._write_down_that_it_would_not(self.config, "claude", "Temporary provider refusal")

        with mock.patch.dict(
            chat.PROVIDER_TRANSPORT_CONTRACT_REVISIONS,
            {"claude-cli": "claude-cli/subscription-exec/v2"},
        ):
            self.assertNotIn("claude", chat.what_would_not_answer(self.config))

    def test_pre_fix_web_relay_failure_is_invalidated_by_the_v2_receipt_contract(self) -> None:
        route = "web:chatgpt-portable-17"
        with mock.patch.dict(
            chat.PROVIDER_TRANSPORT_CONTRACT_REVISIONS,
            {"web-chat": "web-chat/electron-relay/v1"},
        ):
            chat._write_down_that_it_would_not(
                self.config, route, "The marked provider turn could not be reconciled",
            )
            self.assertIn(route, chat.what_would_not_answer(self.config))

        self.assertNotIn(route, chat.what_would_not_answer(self.config))

    def test_unrelated_legacy_failure_remains_visible_until_success_or_expiry(self) -> None:
        where = chat._where_the_noes_are(self.config)
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(json.dumps({
            "claude": {
                "why": "ChatGPT authentication required",
                "when": time.time(),
                "at": "2026-08-29T21:42:57Z",
            }
        }), encoding="utf-8")

        self.assertIn("claude", chat.what_would_not_answer(self.config))

    def test_a_refusal_from_another_day_is_let_go(self) -> None:
        """A service that was down on Friday says nothing about Monday, and a
        note nobody can clear is a note that stops being read."""

        chat._write_down_that_it_would_not(self.config, "claude", "no")
        where = chat._where_the_noes_are(self.config)
        held = json.loads(where.read_text(encoding="utf-8"))
        held["claude"]["when"] -= chat.A_NO_IS_WORTH_MENTIONING_FOR + 60
        where.write_text(json.dumps(held), encoding="utf-8")
        self.assertEqual(self.only_one()["trouble_last_time"], "")

    def test_one_route_being_turned_down_says_nothing_about_another(self) -> None:
        self.config.data["providers"]["copilot"] = {"kind": "copilot-cli", "model": "m"}
        chat._write_down_that_it_would_not(self.config, "claude", "no")
        held = {
            one["route"]: bool(one["trouble_last_time"])
            for one in chat.already_set_up(self.config)
        }
        self.assertEqual(held, {"claude": True, "copilot": False})

    def test_two_routes_failing_at_once_both_get_remembered(self) -> None:
        """It reads the file, changes it and writes it back, which is three
        things. Two at once each wrote back what the other had not seen, and one
        of the notes simply never existed - the one nobody looks for."""

        self.config.data["providers"]["copilot"] = {"kind": "copilot-cli", "model": "m"}
        ready = threading.Barrier(2)

        def blame(route: str) -> None:
            ready.wait(timeout=10)
            chat._write_down_that_it_would_not(self.config, route, route + " said no")

        both = [threading.Thread(target=blame, args=(one,)) for one in ("claude", "copilot")]
        for one in both:
            one.start()
        for one in both:
            one.join(timeout=20)
        self.assertEqual(
            sorted(chat.what_would_not_answer(self.config)), ["claude", "copilot"])

    def test_a_long_refusal_keeps_the_actionable_final_sentence(self) -> None:

        said = ("The service turned this down. " * 40) + "Ask your admin to turn it on."
        self.assertGreater(len(said), chat.LONGEST_NO, "or nothing is being cut")
        chat._write_down_that_it_would_not(self.config, "claude", said)
        held = chat.what_would_not_answer(self.config)["claude"]["why"]
        self.assertEqual(held, said)
        self.assertTrue(held.endswith("."), held[-40:])
        self.assertIn("Ask your admin to turn it on.", held)

    def test_it_does_not_stop_at_an_abbreviation_and_call_that_a_sentence(self) -> None:
        """Stopping after "Mr." leaves something that reads like a whole
        sentence with the half somebody could act on thrown away."""

        # Put together so that "Mr." is the last full stop before the cut and
        # the real sentence before it is a good way back. Cut at the abbreviation
        # this ends on a full stop, reads like a whole sentence, and the part
        # saying what to do is gone with nothing to show it ever existed.
        ending = ("Contact Mr. Smith about it, and then ask your administrator "
                  "to turn Claude Code on for the organisation, which is the "
                  "only thing that changes this.")
        room = chat.LONGEST_NO - 3
        lead = "This was turned down. " + ("padding " * 90)
        said = lead + ending
        self.assertGreater(len(said), chat.LONGEST_NO, "or nothing is being cut")
        self.assertLess(
            len(lead) + ending.index("Mr.") + 3, room,
            "the abbreviation has to fall before the cut, or this proves nothing")

        chat._write_down_that_it_would_not(self.config, "claude", said)
        held = chat.what_would_not_answer(self.config)["claude"]["why"]
        self.assertFalse(
            held.rstrip(".").rstrip(".").endswith("Mr"),
            f"it stopped at an abbreviation: {held[-60:]}")

    def test_a_multi_sentence_refusal_is_not_silently_shortened(self) -> None:
        said = "One sentence. " * 200
        chat._write_down_that_it_would_not(self.config, "claude", said)
        held = chat.what_would_not_answer(self.config)["claude"]["why"]
        self.assertEqual(held, said)

    def test_one_very_long_sentence_is_kept_when_it_fits_safe_storage(self) -> None:
        said = "x" * 2000
        chat._write_down_that_it_would_not(self.config, "claude", said)
        held = chat.what_would_not_answer(self.config)["claude"]["why"]
        self.assertEqual(held, said)

    def test_a_refusal_that_already_fits_is_left_exactly_as_it_is(self) -> None:
        chat._write_down_that_it_would_not(self.config, "claude", "It said no.")
        self.assertEqual(
            chat.what_would_not_answer(self.config)["claude"]["why"], "It said no.")

    def test_a_note_that_cannot_be_written_does_not_break_the_chat(self) -> None:
        """This is bookkeeping around somebody's message. If it cannot be
        written the message still went and the answer still came back."""

        def no_such_place(config):
            raise OSError("nowhere to put it")

        with mock.patch.object(chat, "_where_the_noes_are", no_such_place),              self.standing_in(Answering("here you go")):
            said = chat.say(self.config, "claude", "hello")
        self.assertTrue(said["said"])


class WhoIsHere(TalkingTestCase):
    def test_every_route_set_up_can_be_talked_to(self) -> None:
        self.config.data["providers"] = {
            "second": {"kind": "openai-compatible", "model": "qwen"},
            "first": {"kind": "claude-cli", "model": "sonnet"},
        }
        found = chat.who_can_talk(self.config)
        ready = [one for one in found if one["ready"]]
        self.assertEqual([one["route"] for one in ready], ["first", "second"])

    def test_the_usual_one_counts_when_there_are_no_named_routes(self) -> None:
        found = [one for one in chat.who_can_talk(self.config) if one["ready"]]
        self.assertTrue(found, "a machine with one seat still has somebody to talk to")

    def test_somebody_not_wired_up_yet_says_what_to_do(self) -> None:
        """"Nobody is here" is a worse answer than "here is who you could have"."""

        not_ready = [one for one in chat.who_can_talk(self.config) if not one["ready"]]
        for one in not_ready:
            with self.subTest(who=one["route"]):
                self.assertTrue(one["why_not"])
                self.assertTrue(one["how_to_fix_it"])


class AskingEveryone(TalkingTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.config.data["providers"] = {
            "one": {"kind": "claude-cli", "model": "a"},
            "two": {"kind": "copilot-cli", "model": "b"},
        }

    def test_the_same_question_goes_to_all_of_them(self) -> None:
        with self.standing_in(Answering("Here is what I think.")):
            answers = chat.ask_everyone(self.config, "What do you think?")
        self.assertEqual(sorted(one["route"] for one in answers), ["one", "two"])
        for one in answers:
            with self.subTest(who=one["route"]):
                self.assertEqual(one["answer"], "Here is what I think.")
                self.assertEqual(one["went_wrong"], "")

    def test_one_that_will_not_answer_does_not_stop_the_others(self) -> None:
        class Fussy:
            def complete(self, request):
                raise HarnessError("This one is not signed in.")

        def which(config):
            return Fussy() if config.get("provider.model") == "a" else Answering()

        with mock.patch.object(chat, "create_provider", which):
            answers = chat.ask_everyone(self.config, "What do you think?")
        by_route = {one["route"]: one for one in answers}
        self.assertIn("not signed in", by_route["one"]["went_wrong"])
        self.assertEqual(by_route["one"]["answer"], "")
        self.assertTrue(by_route["two"]["answer"])

    def test_one_that_falls_over_in_a_way_nobody_named_stops_nothing(self) -> None:
        """The harness has a name for a tool that will not answer, and catching
        only that let anything else out: a tool answering with an object nested
        thousands deep raises the error Python raises when it cannot read it,
        which is not that name. Out it went, past this, and it ended the whole
        round - every other assistant had already answered and nobody saw any of
        it."""

        class Falls:
            def complete(self, request):
                raise RecursionError("too deep to read")

        def which(config):
            return Falls() if config.get("provider.model") == "a" else Answering()

        with mock.patch.object(chat, "create_provider", which):
            answers = chat.ask_everyone(self.config, "What do you think?")
        by_route = {one["route"]: one for one in answers}
        self.assertEqual(by_route["one"]["answer"], "")
        self.assertIn("nobody expected", by_route["one"]["went_wrong"])
        self.assertIn("RecursionError", by_route["one"]["went_wrong"])
        # And the whole point: the other one still answered.
        self.assertTrue(by_route["two"]["answer"])
        self.assertEqual(by_route["two"]["went_wrong"], "")

    def test_what_goes_wrong_in_a_way_nobody_named_is_not_repeated_word_for_word(self) -> None:
        """Only what kind of thing it was. Anything the harness has no name for
        has not been through the part that takes credentials out, and this
        sentence goes on a screen."""

        class Falls:
            def complete(self, request):
                raise RuntimeError("sk-do-not-put-me-on-the-screen-0000")

        with mock.patch.object(chat, "create_provider", lambda config: Falls()):
            answers = chat.ask_everyone(self.config, "What do you think?")
        for one in answers:
            with self.subTest(who=one["route"]):
                self.assertIn("RuntimeError", one["went_wrong"])
                self.assertNotIn("sk-do-not-put-me-on-the-screen", one["went_wrong"])

    def test_they_are_asked_at_the_same_time(self) -> None:
        """Six one after another is six waits, which nobody sits through."""

        lock = threading.Lock()
        first_call_is_waiting = threading.Event()
        calls_overlapped = threading.Event()
        active = 0

        class Slow:
            def complete(self, request):
                nonlocal active
                with lock:
                    active += 1
                    if active == 1:
                        first_call_is_waiting.set()
                    elif first_call_is_waiting.is_set():
                        calls_overlapped.set()
                # Hold the first request briefly so a concurrent request has
                # a deterministic opportunity to enter. This proves overlap
                # directly instead of depending on a busy CI runner meeting a
                # fragile wall-clock deadline.
                if active == 1:
                    calls_overlapped.wait(timeout=1.0)
                with lock:
                    active -= 1
                return Back("done")

        with mock.patch.object(chat, "create_provider", lambda config: Slow()):
            chat.ask_everyone(self.config, "What do you think?")
        self.assertTrue(calls_overlapped.is_set(), "they were asked one after another")

    def test_asking_nobody_says_what_to_do(self) -> None:
        self.config.data["providers"] = {}
        self.config.data["provider"] = dict(self.config.data.get("provider", {}), name="")
        with self.assertRaises(chat.ChatError) as caught:
            chat.ask_everyone(self.config, "Anything?")
        self.assertIn("Set them up", str(caught.exception))

    def test_an_empty_question_is_refused_before_anybody_is_asked(self) -> None:
        asked = []

        class Counting:
            def complete(self, request):
                asked.append(request)
                return Back("done")

        with mock.patch.object(chat, "create_provider", lambda config: Counting()):
            with self.assertRaises(chat.ChatError):
                chat.ask_everyone(self.config, "  ")
        self.assertEqual(asked, [])


class TwoAtOnce(TalkingTestCase):
    def test_the_file_is_written_whole(self) -> None:
        """Half a conversation on screen is worse than none."""

        with self.standing_in(Answering()):
            chat.say(self.config, "", "One")
        where = chat.where_it_is_kept(self.config, "")
        json.loads(where.read_text(encoding="utf-8"))
        self.assertEqual(list(where.parent.glob("*.part")), [])

    def test_a_reader_never_sees_half_a_conversation(self) -> None:
        seen: list[str] = []
        stop = threading.Event()

        def keep_reading():
            while not stop.is_set():
                seen.append(str(len(chat.read_it(self.config, ""))))

        reader = threading.Thread(target=keep_reading, daemon=True)
        reader.start()
        try:
            with self.standing_in(Answering()):
                for number in range(30):
                    chat.say(self.config, "", f"Message {number}")
        finally:
            stop.set()
            reader.join(timeout=5)
        self.assertTrue(seen, "the reader never got a look in")


class TalkDocumentationLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.guide = (
            Path(__file__).resolve().parents[1] / "docs" / "TALK_TO_THEM.md"
        ).read_text(encoding="utf-8")

    def test_the_guide_discloses_current_canonical_and_projection_bounds(self) -> None:
        for disclosure in (
            "200,000 characters",
            "8,000,000 characters",
            "120,000 characters",
            "40,000 characters",
            "Every canonical turn",
            "full canonical history",
            "600 seconds",
            "route/provider configuration",
        ):
            self.assertIn(disclosure, self.guide)

    def test_the_guide_no_longer_promises_obsolete_hidden_limits(self) -> None:
        for obsolete in (
            "6,000 letters",
            "20,000 letters",
            "last forty turns are held",
            "older ones drop off",
            "3 minutes to arrive",
            "3-minute",
        ):
            self.assertNotIn(obsolete.lower(), self.guide.lower())


if __name__ == "__main__":
    unittest.main()
