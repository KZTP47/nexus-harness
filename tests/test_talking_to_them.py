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
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from our_harness import chat
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError


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
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})

    def standing_in(self, answering: Answering):
        return mock.patch.object(chat, "create_provider", lambda config: answering)


class OneConversation(TalkingTestCase):
    def test_saying_something_gets_an_answer_back(self) -> None:
        answering = Answering()
        with self.standing_in(answering):
            got = chat.say(self.config, "", "What is a unit test for?")
        self.assertEqual(got["answer"]["text"], "It checks one small piece on its own.")
        self.assertEqual([one["who"] for one in got["said"]], ["you", "them"])
        self.assertEqual(got["said"][0]["text"], "What is a unit test for?")

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

    def test_it_is_still_there_after_the_panel_is_closed(self) -> None:
        with self.standing_in(Answering()):
            chat.say(self.config, "", "Remember this")
        # A brand new reading of the same project, as if the panel restarted.
        again = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        kept = chat.read_it(again, "")
        self.assertEqual([one.who for one in kept], ["you", "them"])
        self.assertEqual(kept[0].text, "Remember this")

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

    def test_only_the_last_few_dozen_turns_are_kept(self) -> None:
        """A conversation is the thread of thought, not everything ever said."""

        with self.standing_in(Answering()):
            for number in range(chat.MOST_KEPT):
                chat.say(self.config, "", f"Message {number}")
        kept = chat.read_it(self.config, "")
        self.assertEqual(len(kept), chat.MOST_KEPT)
        # And the ones kept are the recent ones.
        self.assertIn(f"Message {chat.MOST_KEPT - 1}", kept[-2].text)

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
        self.assertIn("point at the file", str(caught.exception))

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

    def test_a_very_long_answer_is_cut(self) -> None:
        with self.standing_in(Answering("x" * (chat.LONGEST_ANSWER + 500))):
            got = chat.say(self.config, "", "Anything")
        self.assertEqual(len(got["answer"]["text"]), chat.LONGEST_ANSWER)

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

    def test_an_unreadable_conversation_starts_a_new_one(self) -> None:
        where = chat.where_it_is_kept(self.config, "")
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text("this is not json at all", encoding="utf-8")
        self.assertEqual(chat.read_it(self.config, ""), [])
        with self.standing_in(Answering()):
            got = chat.say(self.config, "", "Carry on anyway")
        self.assertEqual(len(got["said"]), 2)


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

        class Slow:
            def complete(self, request):
                time.sleep(0.4)
                return Back("done")

        with mock.patch.object(chat, "create_provider", lambda config: Slow()):
            began = time.monotonic()
            chat.ask_everyone(self.config, "What do you think?")
            took = time.monotonic() - began
        self.assertLess(took, 0.7, "they were asked one after another")

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


if __name__ == "__main__":
    unittest.main()
