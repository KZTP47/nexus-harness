"""Calling for help part way through a job.

One question, one answer, on an assistant you already pay for. The rules that
matter are the ones about what it is *not*: it cannot read files, it cannot run
anything, it goes through the same provider routes as everything else, and
nothing it is told or says keeps a credential in it.
"""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from our_harness import helper, pipelines
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError


class Said:
    """What a provider hands back, with only the part this reads."""

    def __init__(self, text: str):
        self.text = text


class Answering:
    """A provider stood in for, so no assistant is really started."""

    def __init__(self, text: str = "Yes, twice, in the parser and the tests."):
        self.text = text
        self.asked: list = []

    def complete(self, request):
        self.asked.append(request)
        return Said(self.text)


class CallingForHelp(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})

    def standing_in(self, answering: Answering):
        return mock.patch.object(helper, "create_provider", lambda config: answering)

    def test_one_question_gets_one_answer(self) -> None:
        answering = Answering()
        with self.standing_in(answering):
            said = helper.ask_for_help(self.config, "Is the old parser still used?")
        self.assertEqual(said.answer, "Yes, twice, in the parser and the tests.")
        self.assertEqual(said.question, "Is the old parser still used?")
        self.assertGreaterEqual(said.milliseconds, 0)

    def test_it_is_told_not_to_do_the_work(self) -> None:
        """A helper that thinks it is running the job starts trying to run it."""

        answering = Answering()
        with self.standing_in(answering):
            helper.ask_for_help(self.config, "Which of these two is the entry point?")
        told = answering.asked[0].system_prefix
        self.assertIn("one question", told)
        self.assertIn("do not offer to do the work", told.lower())

    def test_it_is_given_no_tools_at_all(self) -> None:
        answering = Answering()
        with self.standing_in(answering):
            helper.ask_for_help(self.config, "Anything?")
        request = answering.asked[0]
        # Nothing that would let it read a file, run a command or change
        # anything is put in front of it.
        self.assertFalse(getattr(request, "tools", None))
        self.assertEqual(request.dynamic_context, "")
        self.assertEqual(len(request.messages), 1)

    def test_an_empty_question_is_refused(self) -> None:
        with self.assertRaises(helper.HelperError) as caught:
            helper.ask_for_help(self.config, "   ")
        self.assertIn("empty", str(caught.exception))

    def test_a_question_the_size_of_a_document_is_refused(self) -> None:
        with self.assertRaises(helper.HelperError) as caught:
            helper.ask_for_help(self.config, "a" * (helper.LONGEST_QUESTION + 1))
        # And it says what to do instead, rather than only saying no.
        self.assertIn("belongs in the project", str(caught.exception))

    def test_a_question_with_a_control_character_is_refused(self) -> None:
        with self.assertRaises(helper.HelperError):
            helper.ask_for_help(self.config, "Is this used\x00anywhere?")

    def test_tabs_and_newlines_are_allowed(self) -> None:
        answering = Answering()
        with self.standing_in(answering):
            said = helper.ask_for_help(self.config, "One line\nand\ta second")
        self.assertTrue(said.answer)

    def test_a_long_wait_is_brought_back_inside_the_limit(self) -> None:
        answering = Answering()
        with self.standing_in(answering):
            helper.ask_for_help(self.config, "Anything?", seconds=99_999)
        self.assertLessEqual(
            answering.asked[0].timeout_seconds, helper.LONGEST_WAIT_SECONDS
        )

    def test_a_route_nobody_set_up_cannot_be_used(self) -> None:
        with self.assertRaises(helper.HelperError) as caught:
            helper.ask_for_help(self.config, "Anything?", who="a-route-nobody-made")
        said = str(caught.exception)
        self.assertIn("a-route-nobody-made", said)
        self.assertIn("cannot be reached", said)

    def test_an_answer_of_nothing_is_said_plainly(self) -> None:
        with self.standing_in(Answering("   ")):
            with self.assertRaises(helper.HelperError) as caught:
                helper.ask_for_help(self.config, "Anything?")
        self.assertIn("nothing at all", str(caught.exception))

    def test_a_very_long_answer_is_cut(self) -> None:
        with self.standing_in(Answering("x" * (helper.LONGEST_ANSWER + 500))):
            said = helper.ask_for_help(self.config, "Anything?")
        self.assertEqual(len(said.answer), helper.LONGEST_ANSWER)

    def test_credentials_never_reach_the_assistant_or_the_record(self) -> None:
        self.config.data.setdefault("redaction", {})
        answering = Answering("Your key sk-abcdef0123456789abcdef01 is in the file.")
        with self.standing_in(answering):
            said = helper.ask_for_help(
                self.config, "Is sk-abcdef0123456789abcdef01 used anywhere?"
            )
        sent = answering.asked[0].messages[0]["content"]
        self.assertNotIn("sk-abcdef0123456789abcdef01", sent)
        self.assertNotIn("sk-abcdef0123456789abcdef01", said.answer)

    def test_a_refusal_is_shown_as_a_sentence_not_a_page_of_json(self) -> None:
        """A signed-in tool says why in one sentence, then buries it in detail."""

        def wont(config):
            raise HarnessError(
                'claude said: {"type": "result", "is_error": true, '
                '"result": "Your organisation does not have access to this model.", '
                '"usage": {"input_tokens": 4}}'
            )

        with mock.patch.object(helper, "create_provider", wont):
            with self.assertRaises(helper.HelperError) as caught:
                helper.ask_for_help(self.config, "Anything?")
        said = str(caught.exception)
        self.assertIn("does not have access", said)
        self.assertNotIn("input_tokens", said)

    def test_something_that_is_not_json_is_left_as_it_is(self) -> None:
        said = helper._in_plain_words(HarnessError("The command was not found"))
        self.assertEqual(said, "The command was not found")

    def test_broken_json_does_not_lose_the_message(self) -> None:
        said = helper._in_plain_words(HarnessError("it went wrong: {not really json"))
        self.assertIn("went wrong", said)

    def test_who_could_help_lists_the_usual_one_when_there_are_no_routes(self) -> None:
        found = helper.who_could_help(self.config)
        self.assertTrue(found, "a machine with one seat set up still has one answer")
        self.assertTrue(found[0]["kind"])

    def test_who_could_help_lists_every_named_route(self) -> None:
        self.config.data["providers"] = {
            "second": {"kind": "openai_compatible", "model": "qwen"},
            "first": {"kind": "claude_cli", "model": "sonnet"},
        }
        found = helper.who_could_help(self.config)
        self.assertEqual([one["route"] for one in found], ["first", "second"])


class AskingAsAStepOfAPipeline(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})

    def test_a_step_with_no_question_says_so(self) -> None:
        passed, said, _detail = pipelines._ask_for_help(
            self.config, {"kind": "ask_for_help", "settings": {}}
        )
        self.assertFalse(passed)
        self.assertIn("does not say what to ask", said)

    def test_the_answer_is_kept_with_the_run(self) -> None:
        answer = helper.Answer(
            question="Is it used?", answer="Yes.\nTwice.", who="the usual one",
            model="sonnet", milliseconds=12,
        )
        with mock.patch.object(helper, "ask_for_help", lambda *a, **k: answer):
            passed, said, detail = pipelines._ask_for_help(
                self.config,
                {"kind": "ask_for_help", "settings": {"question": "Is it used?"}},
            )
        self.assertTrue(passed)
        # The line somebody reads at a glance, and the whole answer underneath.
        self.assertIn("Yes.", said)
        self.assertEqual(detail, "Yes.\nTwice.")

    def test_a_step_that_could_not_ask_fails_without_ending_the_run(self) -> None:
        def wont(*a, **k):
            raise helper.HelperError("Nobody answered.")

        with mock.patch.object(helper, "ask_for_help", wont):
            passed, said, _detail = pipelines._ask_for_help(
                self.config,
                {"kind": "ask_for_help", "settings": {"question": "Is it used?"}},
            )
        self.assertFalse(passed)
        self.assertEqual(said, "Nobody answered.")


if __name__ == "__main__":
    unittest.main()
