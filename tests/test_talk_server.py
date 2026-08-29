"""Talking to them through the panel, not through the library.

The library tests drive the conversation directly. That leaves the part between
the button and the file untested, and that is where the expensive mistakes
live: one way in that takes the lock and another that does not, a turn read and
written over and gone with nobody told, a name from a request that reaches a
file it should not.
"""

from __future__ import annotations

import copy
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from our_harness import chat, seats as seats_lab, server
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError

NOTHING_INSTALLED = seats_lab.Look(seats=[], settings_file=".harness/config.local.json")


class Back:
    def __init__(self, text: str):
        self.text = text


class Slowly:
    """An assistant that takes a moment, so two at once really overlap."""

    def __init__(self, seconds: float = 0.25, text: str = "an answer"):
        self.seconds = seconds
        self.text = text

    def complete(self, request):
        time.sleep(self.seconds)
        return Back(self.text)


class MeetAtTheSameTime:
    """A provider probe that can finish only when both calls overlap."""

    def __init__(
        self,
        meeting: threading.Barrier,
        call_threads: set[int],
        call_threads_lock: threading.Lock,
    ) -> None:
        self.meeting = meeting
        self.call_threads = call_threads
        self.call_threads_lock = call_threads_lock

    def complete(self, request):
        with self.call_threads_lock:
            self.call_threads.add(threading.get_ident())
        try:
            # The timeout is only a deadlock guard for a serialization
            # regression. Success depends on rendezvous, not runner speed.
            self.meeting.wait(timeout=10)
        except threading.BrokenBarrierError as exc:
            raise HarnessError("the provider calls did not overlap") from exc
        return Back("an answer")


class PanelTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["providers"] = {
            "one": {"kind": "claude-cli", "model": "a"},
            "two": {"kind": "copilot-cli", "model": "b"},
        }
        config = LoadedConfig(data, self.root, [], {})
        self.panel = server.HarnessHTTPServer(("127.0.0.1", 0), config)
        self.addCleanup(self.panel.server_close)
        self.port = self.panel.server_address[1]
        threading.Thread(target=self.panel.serve_forever, daemon=True).start()
        self.addCleanup(self.panel.shutdown)
        self.config = config
        looking = mock.patch.object(seats_lab, "look", return_value=NOTHING_INSTALLED)
        looking.start()
        self.addCleanup(looking.stop)
        # Nobody's real assistant is started by a test.
        self.standing_in = mock.patch.object(
            chat, "create_provider", lambda config: Slowly(0.25)
        )
        self.standing_in.start()
        self.addCleanup(self.standing_in.stop)

    def ask(self, path: str, body: dict | None = None) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={"Content-Type": "application/json", "X-Harness-Token": self.panel.token},
            method="POST" if body is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as answer:
                return answer.status, json.loads(answer.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class OpeningTheTab(PanelTestCase):
    def test_it_says_who_can_be_talked_to(self) -> None:
        status, said = self.ask("/api/chat")
        self.assertEqual(status, 200)
        self.assertEqual(sorted(one["route"] for one in said["who"]), ["one", "two"])
        self.assertEqual(said["open"], "one", "the first one that can answer is open")
        self.assertEqual(said["said"], [])

    def test_opening_the_tab_writes_nothing(self) -> None:
        self.ask("/api/chat")
        self.assertFalse((self.root / ".harness" / "chats").exists())

    def test_asking_for_somebody_who_is_not_there_falls_back(self) -> None:
        status, said = self.ask("/api/chat?who=nobody-made-this")
        self.assertEqual(status, 200, "the whole tab still comes back")
        self.assertEqual(said["open"], "one")

    def test_it_says_the_bounds_it_keeps(self) -> None:
        _status, said = self.ask("/api/chat")
        self.assertEqual(said["most_letters"], chat.MOST_LETTERS)
        self.assertEqual(said["most_kept"], chat.MOST_KEPT)
        self.assertEqual(said["limits"]["input_characters"], chat.MOST_LETTERS)
        self.assertEqual(said["limits"]["answer_characters"], chat.LONGEST_ANSWER)
        self.assertEqual(said["limits"]["overflow_policy"], "reject_without_truncation")


class SayingSomething(PanelTestCase):
    def test_it_goes_through_and_comes_back(self) -> None:
        status, said = self.ask("/api/chat/say", {"who": "one", "text": "Hello"})
        self.assertEqual(status, 200)
        self.assertEqual([one["who"] for one in said["said"]], ["you", "them"])
        self.assertEqual(said["answer"]["text"], "an answer")

    def test_it_is_still_there_when_the_tab_is_opened_again(self) -> None:
        self.ask("/api/chat/say", {"who": "one", "text": "Hello"})
        _status, said = self.ask("/api/chat?who=one")
        self.assertEqual(len(said["said"]), 2)

    def test_saying_nothing_is_refused_with_a_sentence(self) -> None:
        status, said = self.ask("/api/chat/say", {"who": "one", "text": "  "})
        self.assertEqual(status, 400)
        self.assertIn("Type something", said["error"])
        self.assertNotIn("Traceback", said["error"])

    def test_starting_again_empties_it(self) -> None:
        self.ask("/api/chat/say", {"who": "one", "text": "Hello"})
        status, said = self.ask("/api/chat/start-again", {"who": "one"})
        self.assertEqual(status, 200)
        self.assertIn("gone", said["note"])
        _status, now = self.ask("/api/chat?who=one")
        self.assertEqual(now["said"], [])


class NobodyLosesATurn(PanelTestCase):
    def two_at_once(self, first: tuple[str, dict], second: tuple[str, dict]) -> None:
        answers: list = []

        def go(path, body):
            answers.append(self.ask(path, body))

        threads = [
            threading.Thread(target=go, args=first),
            threading.Thread(target=go, args=second),
        ]
        for one in threads:
            one.start()
        for one in threads:
            one.join(timeout=60)
        for status, said in answers:
            self.assertEqual(status, 200, said)

    def test_two_sends_at_once_do_not_start_two_provider_turns(self) -> None:
        answers: list = []

        def go(text: str) -> None:
            answers.append(self.ask("/api/chat/say", {"who": "one", "text": text}))

        threads = [threading.Thread(target=go, args=(text,)) for text in ("message A", "message B")]
        for one in threads:
            one.start()
        for one in threads:
            one.join(timeout=60)

        self.assertEqual(sorted(status for status, _said in answers), [200, 400])
        refused = next(said for status, said in answers if status == 400)
        self.assertIn("already waiting", refused["error"])
        _status, said = self.ask("/api/chat?who=one")
        words = " ".join(one["text"] for one in said["said"])
        self.assertTrue(("message A" in words) ^ ("message B" in words), said["said"])
        self.assertEqual(len(said["said"]), 2, said["said"])

    def test_two_ask_everyone_turns_fail_fast_instead_of_queueing(self) -> None:
        answers: list = []

        def go(text: str) -> None:
            answers.append(self.ask("/api/chat/ask-everyone", {"text": text}))

        threads = [
            threading.Thread(target=go, args=(text,))
            for text in ("message A", "message B")
        ]
        for one in threads:
            one.start()
        for one in threads:
            one.join(timeout=60)

        self.assertEqual(sorted(status for status, _said in answers), [200, 400])
        refused = next(said for status, said in answers if status == 400)
        self.assertIn("already waiting", refused["error"])

    def test_asking_everyone_does_not_lose_a_turn_either(self) -> None:
        """The one the lock was missing from.

        Asking everyone says something to every assistant, so it is a write to
        the same conversation as Send - and it used to do it without taking the
        lock Send takes.
        """

        self.two_at_once(
            ("/api/chat/ask-everyone", {"text": "message A"}),
            ("/api/chat/say", {"who": "one", "text": "message B"}),
        )
        _status, said = self.ask("/api/chat?who=one")
        words = " ".join(one["text"] for one in said["said"])
        self.assertIn("message A", words)
        self.assertIn("message B", words)
        self.assertEqual(len(said["said"]), 4, said["said"])

    def test_starting_again_cannot_land_in_the_middle_of_a_send(self) -> None:
        self.two_at_once(
            ("/api/chat/say", {"who": "one", "text": "message A"}),
            ("/api/chat/start-again", {"who": "one"}),
        )
        _status, said = self.ask("/api/chat?who=one")
        # Either the send happened and then it was cleared, or the other way
        # round. What must not happen is half a turn.
        self.assertIn(len(said["said"]), (0, 2), said["said"])


class AskingEveryoneThroughThePanel(PanelTestCase):
    def test_all_of_them_answer(self) -> None:
        status, said = self.ask("/api/chat/ask-everyone", {"text": "What do you think?"})
        self.assertEqual(status, 200)
        self.assertEqual(sorted(one["route"] for one in said["answers"]), ["one", "two"])
        for one in said["answers"]:
            with self.subTest(who=one["route"]):
                self.assertEqual(one["answer"], "an answer")

    def test_they_are_asked_at_the_same_time(self) -> None:
        meeting = threading.Barrier(2)
        call_threads: set[int] = set()
        call_threads_lock = threading.Lock()
        with mock.patch.object(
            chat,
            "create_provider",
            lambda config: MeetAtTheSameTime(
                meeting, call_threads, call_threads_lock,
            ),
        ):
            status, said = self.ask(
                "/api/chat/ask-everyone", {"text": "What do you think?"},
            )

        self.assertEqual(status, 200, said)
        self.assertFalse(meeting.broken, said)
        self.assertEqual(len(call_threads), 2, call_threads)
        self.assertTrue(
            all(not one["went_wrong"] for one in said["answers"]), said,
        )


class NothingReachesOutside(PanelTestCase):
    def test_a_name_from_a_request_cannot_reach_another_folder(self) -> None:
        for bad in ("../../secrets", "..\\..\\secrets", "C:/Windows/System32/x",
                    "a/b", "....", "CON", "NUL", "COM1", "name.", "name "):
            with self.subTest(name=bad):
                status, said = self.ask("/api/chat/say", {"who": bad, "text": "hello"})
                self.assertEqual(status, 400, f"{bad} was allowed")
                self.assertNotIn("Traceback", said["error"])
        # And nothing was written anywhere.
        left = list(self.root.rglob("*.json"))
        self.assertFalse(
            [one for one in left if "chats" not in one.parts], left
        )

    def test_two_names_that_differ_only_by_a_capital_keep_apart(self) -> None:
        """A file name on Windows does not care about capitals. Two people do."""

        self.config.data["providers"]["MyBot"] = {"kind": "claude-cli", "model": "c"}
        self.config.data["providers"]["mybot"] = {"kind": "claude-cli", "model": "d"}
        self.ask("/api/chat/say", {"who": "MyBot", "text": "only for the capital one"})
        self.ask("/api/chat/say", {"who": "mybot", "text": "only for the small one"})
        _status, capital = self.ask("/api/chat?who=MyBot")
        _status, small = self.ask("/api/chat?who=mybot")
        self.assertEqual(len(capital["said"]), 2, capital["said"])
        self.assertEqual(len(small["said"]), 2, small["said"])
        self.assertIn("capital", capital["said"][0]["text"])
        self.assertNotIn("small", " ".join(one["text"] for one in capital["said"]))


class WhenSomethingWillNotAnswer(PanelTestCase):
    def test_a_web_page_comes_back_as_a_sentence(self) -> None:
        """Something in between can answer instead, and it answers in HTML."""

        page = (
            "Provider HTTP 501: <!DOCTYPE HTML><html><head>"
            "<title>Error response</title></head><body><h1>Error response</h1>"
            "<p>Error code: 501</p><p>Message: Unsupported method ('POST').</p>"
            "</body></html>"
        )

        class Wont:
            def complete(self, request):
                raise HarnessError(page)

        with mock.patch.object(chat, "create_provider", lambda config: Wont()):
            status, said = self.ask("/api/chat/say", {"who": "one", "text": "hello"})
        self.assertEqual(status, 400)
        words = said["error"]
        self.assertNotIn("<", words, words)
        self.assertNotIn("DOCTYPE", words)
        self.assertIn("Error response", words)
        self.assertLess(len(words), 200, words)

    def test_a_key_in_the_reason_never_reaches_the_panel(self) -> None:
        """The reason a key was refused is the one place a key comes back."""

        class Fussy:
            def complete(self, request):
                raise HarnessError(
                    "401 Unauthorized: Incorrect API key provided: "
                    "sk-abcdef0123456789abcdef01"
                )

        with mock.patch.object(chat, "create_provider", lambda config: Fussy()):
            status, said = self.ask("/api/chat/say", {"who": "one", "text": "hello"})
        self.assertEqual(status, 400)
        self.assertNotIn("sk-abcdef0123456789abcdef01", said["error"])
        self.assertIn("Unauthorized", said["error"])

    def test_a_route_named_after_the_unnamed_one_keeps_its_own(self) -> None:
        self.config.data["providers"][chat.THE_USUAL_ONE] = {
            "kind": "claude-cli", "model": "c",
        }
        self.ask("/api/chat/say", {"who": chat.THE_USUAL_ONE, "text": "for the named one"})
        self.ask("/api/chat/say", {"who": "", "text": "for the usual one"})
        _status, named = self.ask(f"/api/chat?who={chat.THE_USUAL_ONE}")
        self.assertEqual(len(named["said"]), 2, named["said"])
        self.assertIn("named", named["said"][0]["text"])
        self.assertNotIn(
            "usual", " ".join(one["text"] for one in named["said"])
        )

    def test_one_that_will_not_answer_does_not_stop_the_others(self) -> None:
        class Fussy:
            def complete(self, request):
                raise HarnessError("This one is not signed in.")

        def which(config):
            return Fussy() if config.get("provider.model") == "a" else Slowly(0.05)

        with mock.patch.object(chat, "create_provider", which):
            status, said = self.ask("/api/chat/ask-everyone", {"text": "hello"})
        self.assertEqual(status, 200)
        by_route = {one["route"]: one for one in said["answers"]}
        self.assertIn("not signed in", by_route["one"]["went_wrong"])
        self.assertTrue(by_route["two"]["answer"])


class TheLocksAreBounded(unittest.TestCase):
    def test_a_stream_of_made_up_names_cannot_fill_the_machine(self) -> None:
        """The lock for a conversation is kept. Kept for ever is a leak."""

        before = dict(chat._locks)
        try:
            chat._locks.clear()
            for number in range(chat.MOST_LOCKS + 50):
                chat._the_lock_for(f"made-up-{number}")
            self.assertLessEqual(len(chat._locks), chat.MOST_LOCKS + 1)
        finally:
            chat._locks.clear()
            chat._locks.update(before)


if __name__ == "__main__":
    unittest.main()
