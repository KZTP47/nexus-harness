"""Timers through the panel, not through the library.

The timer tests drive the library directly. That leaves the part between the
button and the library untested, and that is where two of the worst bugs lived:
turning a timer off sent the whole timer back from a panel that had been open a
while, putting the old time and the old automation back over whatever somebody
else had changed; and the panel would put an automation that stops to ask a
person on a timer without a word, where a terminal refuses to.
"""

from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from our_harness import pipelines, server, timer
from our_harness.config import DEFAULT_CONFIG, LoadedConfig


class TimerPanelTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        pipelines.save(self.config, {
            "name": "Nightly check",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "work", "kind": "git_repo", "label": "The work"},
            ],
            "edges": [{"from": "start", "to": "work"}],
        })
        pipelines.save(self.config, {
            "name": "Asks first",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "ask", "kind": "wait_for_a_person", "label": "Are you sure?",
                 "settings": {"question": "Shall I?"}},
            ],
            "edges": [{"from": "start", "to": "ask"}],
        })
        self.panel = server.HarnessHTTPServer(("127.0.0.1", 0), self.config)
        self.addCleanup(self.panel.server_close)
        self.port = self.panel.server_address[1]
        thread = threading.Thread(target=self.panel.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.panel.shutdown)

    def ask(self, path: str, body: dict | None = None) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={
                "Content-Type": "application/json",
                "X-Harness-Token": self.panel.token,
            },
            method="POST" if body is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as answer:
                return answer.status, json.loads(answer.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def a_timer(self, *, anyway: bool = False, **changes) -> dict:
        said = {
            "name": "Every night",
            "automation": "Nightly check",
            "how_often": "every-day",
            "at": "02:00",
            "turned_on": True,
            **changes,
        }
        status, answer = self.ask(
            "/api/timers/save", {"timer": said, "anyway": anyway}
        )
        self.assertEqual(status, 200, answer)
        return answer


class TheSwitchOnlySendsTheSwitch(TimerPanelTestCase):
    def test_turning_one_off_does_not_put_back_an_old_time(self) -> None:
        self.a_timer()
        # Somebody else moves it to half five. The panel still holds the old one.
        one = timer.load(self.config, "Every night")
        one.at = "05:30"
        timer.save(self.config, one.to_dict())

        status, said = self.ask(
            "/api/timers/turn", {"name": "Every night", "turned_on": False}
        )
        self.assertEqual(status, 200, said)
        after = timer.load(self.config, "Every night")
        self.assertFalse(after.turned_on)
        self.assertEqual(after.at, "05:30", "the newer time stands")
        self.assertIn("turned off", said["note"])

    def test_turning_one_back_on_says_so(self) -> None:
        self.a_timer(turned_on=False)
        _status, said = self.ask(
            "/api/timers/turn", {"name": "Every night", "turned_on": True}
        )
        self.assertIn("turned on", said["note"])
        self.assertTrue(timer.load(self.config, "Every night").turned_on)

    def test_a_timer_that_is_not_there(self) -> None:
        status, said = self.ask(
            "/api/timers/turn", {"name": "Nothing like it", "turned_on": True}
        )
        self.assertGreaterEqual(status, 400)
        self.assertIn("no timer", json.dumps(said).lower())


class WhyItShouldNotRunAlone(TimerPanelTestCase):
    def test_the_panel_can_ask_before_it_puts_one_on(self) -> None:
        _status, said = self.ask("/api/pipelines/why-not-alone?name=Asks%20first")
        self.assertIn("nobody", said["why_not"].lower())

    def test_an_automation_with_nothing_wrong_with_it(self) -> None:
        _status, said = self.ask("/api/pipelines/why-not-alone?name=Nightly%20check")
        self.assertEqual(said["why_not"], "")

    def test_an_automation_that_is_not_there_is_not_a_crash(self) -> None:
        status, said = self.ask("/api/pipelines/why-not-alone?name=Nothing%20like%20it")
        self.assertEqual(status, 200, said)
        self.assertIsInstance(said["why_not"], str)


class TheGateIsOnThisSideOfTheWire(TimerPanelTestCase):
    """Held in the panel's own code alone, it was not held at all.

    Anything talking to the harness directly - another program, a script, the
    address typed into a browser - skipped the warning entirely and left an
    automation that stops to ask a person running with nobody there.
    """

    def test_turning_one_on_is_refused_unless_they_meant_it(self) -> None:
        self.a_timer(automation="Asks first", turned_on=False, anyway=True)
        status, said = self.ask(
            "/api/timers/turn", {"name": "Every night", "turned_on": True}
        )
        self.assertGreaterEqual(status, 400, said)
        self.assertIn("nobody", json.dumps(said).lower())
        self.assertFalse(timer.load(self.config, "Every night").turned_on)

    def test_and_goes_on_when_they_say_they_meant_it(self) -> None:
        self.a_timer(automation="Asks first", turned_on=False, anyway=True)
        status, said = self.ask(
            "/api/timers/turn",
            {"name": "Every night", "turned_on": True, "anyway": True},
        )
        self.assertEqual(status, 200, said)
        self.assertTrue(timer.load(self.config, "Every night").turned_on)

    def test_turning_one_off_is_never_refused(self) -> None:
        self.a_timer(automation="Asks first", anyway=True)
        status, said = self.ask(
            "/api/timers/turn", {"name": "Every night", "turned_on": False}
        )
        self.assertEqual(status, 200, said)

    def test_saving_one_turned_on_is_refused_the_same_way(self) -> None:
        status, said = self.ask("/api/timers/save", {"timer": {
            "name": "Straight in", "automation": "Asks first", "turned_on": True,
        }})
        self.assertGreaterEqual(status, 400, said)
        self.assertIn("nobody", json.dumps(said).lower())
        self.assertEqual(timer.every_one(self.config), [])

    def test_one_with_nothing_wrong_with_it_needs_nothing_said(self) -> None:
        status, said = self.ask("/api/timers/save", {"timer": {
            "name": "Straight in", "automation": "Nightly check", "turned_on": True,
        }})
        self.assertEqual(status, 200, said)


class NothingComesBackWithAKeyInIt(TimerPanelTestCase):
    A_KEY = "sk-abcdefghijklmnopqrstuvwxyz012345"

    def test_running_one_by_hand_answers_in_safe_words(self) -> None:
        """Cleaned on its way to the file, the same words still came back to
        the browser and onto the screen."""

        self.a_timer()
        from our_harness import pipelines as pipeline_lab

        class Pretend:
            passed = False
            said = f"it failed: Authorization: Bearer {self.A_KEY}"

        with mock.patch.object(pipeline_lab, "run_it", lambda *a, **k: Pretend()):
            status, said = self.ask("/api/timers/run-now", {"name": "Every night"})
        self.assertEqual(status, 200, said)
        self.assertNotIn(self.A_KEY, json.dumps(said))
        self.assertIn("it failed", said["said"])
        written = json.dumps(timer.load(self.config, "Every night").to_dict())
        self.assertNotIn(self.A_KEY, written)


class WhatThePanelIsTold(TimerPanelTestCase):
    def test_it_gets_the_timers_the_ways_and_the_line_for_this_machine(self) -> None:
        self.a_timer()
        status, said = self.ask("/api/timers")
        self.assertEqual(status, 200, said)
        self.assertEqual([one["name"] for one in said["timers"]], ["Every night"])
        self.assertIn("Nightly check", said["automations"])
        self.assertEqual(len(said["how_often"]), len(timer.HOW_OFTEN))
        self.assertIn("timer run", said["how_to_ask_this_machine"]["what"])
        self.assertEqual(said["could_not_be_read"], "")

    def test_it_is_told_when_the_record_had_to_be_put_aside(self) -> None:
        self.a_timer()
        where = timer.folder(self.config) / ".what-happened.json"
        where.write_text("{ this is not json", encoding="utf-8")
        timer._what_happened(self.config)
        _status, said = self.ask("/api/timers")
        self.assertIn("could not be read", said["could_not_be_read"])

    def test_a_run_by_hand_is_written_down_but_does_not_move_it(self) -> None:
        self.a_timer()
        status, said = self.ask("/api/timers/run-now", {"name": "Every night"})
        self.assertEqual(status, 200, said)
        kept = timer.load(self.config, "Every night")
        self.assertEqual(len(kept.runs), 1)
        # Still due tonight, because one by hand is extra, not instead.
        due = timer.what_is_due(self.config, __import__("datetime").datetime.now()
                                + __import__("datetime").timedelta(days=1))
        self.assertEqual([one.name for one, _missed in due], ["Every night"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
