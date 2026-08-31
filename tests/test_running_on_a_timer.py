"""Running an automation with nobody watching.

The promise is the one thing watch mode's `--every` cannot keep: close
everything, go home, and the run still happens. Everything here is about the
ways that goes wrong - a timer that fires the moment it is made, a machine that
was off for a week coming back to a hundred runs, two runs on top of each
other, and an automation that stops to ask somebody who is not there.
"""

from __future__ import annotations

import copy
import gc
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from our_harness import pipelines, safety, timer
from our_harness.models import HarnessError
from our_harness.config import DEFAULT_CONFIG, LoadedConfig


def _a_process_that_has_finished() -> int:
    """The number of a process that has been and gone.

    Made up numbers do not do: on a busy machine 1234 is somebody. This one
    really ran and really stopped, so the answer cannot be a coincidence.
    """

    going = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    going.wait(timeout=60)
    number = going.pid
    # Let go of it, or Windows keeps the number alive for whoever still holds a
    # handle on it, and it looks like it is still going.
    del going
    gc.collect()
    return number


class TimerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.runtime_temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.runtime_temporary.cleanup)
        prior_runtime = os.environ.get("OUR_HARNESS_PIPELINE_RUN_DIR")
        os.environ["OUR_HARNESS_PIPELINE_RUN_DIR"] = self.runtime_temporary.name
        self.addCleanup(
            lambda: os.environ.pop("OUR_HARNESS_PIPELINE_RUN_DIR", None)
            if prior_runtime is None
            else os.environ.__setitem__("OUR_HARNESS_PIPELINE_RUN_DIR", prior_runtime)
        )
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

    def a_timer(self, **changes) -> timer.Timer:
        return timer.save(self.config, {
            "name": "Every night",
            "automation": "Nightly check",
            "how_often": "every-day",
            "at": "02:00",
            **changes,
        })

    def stand_in(self, passed: bool = True, said: str = "all three passed"):
        """Every step stood in, so no real check or command is run."""

        def one(config, node, before, results, order, check_kinds, depth=0,
                stopping=None, waiting_on=None):
            if node["kind"] == "start":
                return True, "Started", ""
            return passed, said, ""

        return mock.patch.object(pipelines, "_do_one", one)


class WhenItRuns(TimerTestCase):
    def test_every_hour_is_on_the_hour(self) -> None:
        one = self.a_timer(how_often="every-hour")
        when = timer.when_it_runs_next(one, datetime(2026, 8, 19, 13, 30))
        self.assertEqual(when, datetime(2026, 8, 19, 14, 0))

    def test_every_day_is_at_the_time_you_picked(self) -> None:
        one = self.a_timer(at="17:30")
        self.assertEqual(
            timer.when_it_runs_next(one, datetime(2026, 8, 19, 13, 30)),
            datetime(2026, 8, 19, 17, 30),
        )
        # And past it, that is tomorrow.
        self.assertEqual(
            timer.when_it_runs_next(one, datetime(2026, 8, 19, 18, 0)),
            datetime(2026, 8, 20, 17, 30),
        )

    def test_a_weekday_timer_skips_the_weekend(self) -> None:
        one = self.a_timer(how_often="every-weekday", at="07:00")
        # From Friday evening, and from Saturday, the answer is Monday.
        for from_when in (datetime(2026, 8, 21, 20, 0), datetime(2026, 8, 22, 3, 0)):
            with self.subTest(from_when=from_when):
                when = timer.when_it_runs_next(one, from_when)
                self.assertEqual(when.weekday(), 0, "Monday")
                self.assertEqual(when.hour, 7)

    def test_a_weekly_timer_lands_on_the_day_you_picked(self) -> None:
        one = self.a_timer(how_often="every-week", on="sunday", at="02:00")
        when = timer.when_it_runs_next(one, datetime(2026, 8, 19, 13, 30))
        self.assertEqual(when.weekday(), 6, "Sunday")
        self.assertEqual((when.hour, when.minute), (2, 0))

    def test_it_says_when_it_runs_in_plain_words(self) -> None:
        for changes, wanted in (
            ({"how_often": "every-hour"}, "Every hour, on the hour"),
            ({"how_often": "every-day", "at": "02:00"}, "Every day at 02:00"),
            ({"how_often": "every-weekday", "at": "07:00"}, "Every weekday at 07:00"),
            ({"how_often": "every-week", "on": "sunday"}, "Every Sunday at 02:00"),
        ):
            with self.subTest(changes=changes):
                self.assertEqual(
                    timer.in_plain_words(self.a_timer(**changes)), wanted
                )

    def test_every_way_of_running_has_a_label_and_a_plain_meaning(self) -> None:
        for key, label, means in timer.HOW_OFTEN:
            with self.subTest(how_often=key):
                self.assertTrue(label and not label.endswith("."))
                self.assertTrue(means.endswith("."))


class WhatItRefuses(TimerTestCase):
    def test_a_time_that_is_not_a_time(self) -> None:
        for bad in ("25:00", "2pm", "02:60", "half two", "2", "0200"):
            with self.subTest(at=bad):
                with self.assertRaises(timer.TimerError) as caught:
                    timer.read_it({"name": "x", "automation": "y", "at": bad})
                self.assertIn("hours and minutes", str(caught.exception))

    def test_no_time_at_all_takes_the_usual_one(self) -> None:
        """Blank is not wrong, it is "the usual", and the usual is two."""

        for nothing in ("", None):
            with self.subTest(at=nothing):
                one = timer.read_it({"name": "x", "automation": "y", "at": nothing})
                self.assertEqual(one.at, "02:00")

    def test_a_time_written_the_short_way_is_tidied(self) -> None:
        self.assertEqual(
            timer.read_it({"name": "x", "automation": "y", "at": "7:05"}).at, "07:05"
        )

    def test_a_way_of_running_nobody_offers(self) -> None:
        with self.assertRaises(timer.TimerError) as caught:
            timer.read_it({"name": "x", "automation": "y", "how_often": "every-fortnight"})
        self.assertIn("every-day", str(caught.exception), "it lists the real ones")

    def test_a_name_that_could_reach_outside_the_project(self) -> None:
        for bad in ("../secrets", "a/b", "a\\b", "", "."):
            with self.subTest(name=bad):
                with self.assertRaises(timer.TimerError):
                    timer.save(self.config, {"name": bad, "automation": "Nightly check"})

    def test_a_timer_with_no_automation(self) -> None:
        with self.assertRaises(timer.TimerError) as caught:
            timer.save(self.config, {"name": "Nowhere"})
        self.assertIn("which automation", str(caught.exception))

    def test_an_automation_that_stops_to_ask_a_person(self) -> None:
        """Nobody is there at two in the morning, and it says so now."""

        pipelines.save(self.config, {
            "name": "Asks first",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "ask", "kind": "wait_for_a_person", "label": "Are you sure?",
                 "settings": {"question": "Shall I?"}},
            ],
            "edges": [{"from": "start", "to": "ask"}],
        })
        why_not = timer.what_stops_it_running_alone(self.config, "Asks first")
        self.assertIn("Nobody is there", why_not)
        self.assertIn("Are you sure?", why_not, "and which step it is")

    def test_an_ordinary_automation_has_nothing_against_it(self) -> None:
        self.assertEqual(
            timer.what_stops_it_running_alone(self.config, "Nightly check"), ""
        )

    def test_an_automation_that_is_not_there(self) -> None:
        said = timer.what_stops_it_running_alone(self.config, "Never saved")
        self.assertIn("Never saved", said)


class NamesThatWouldTreadOnSomething(TimerTestCase):
    def test_a_timer_called_what_happened_cannot_wipe_the_record(self) -> None:
        """The record of when everything last ran lived under a name a timer
        could be given. One called "What Happened" wrote over it, and every
        timer in the project quietly stopped firing with nothing to say why."""

        self.a_timer()
        timer.looked_just_now(self.config, datetime(2026, 8, 19, 12, 0))
        timer.save(self.config, {
            "name": "What Happened", "automation": "Nightly check",
        })
        # The first one is still known, and still runs.
        names = [one.name for one in timer.every_one(self.config)]
        self.assertIn("Every night", names)
        self.assertIn("What Happened", names, "and the new one is not invisible")
        due = timer.what_is_due(self.config, datetime(2026, 8, 20, 2, 30))
        self.assertIn("Every night", [one.name for one, _missed in due])

    def test_two_names_that_come_to_one_file_are_refused(self) -> None:
        """Not silently written over, which is what losing work looks like."""

        timer.save(self.config, {"name": "Nightly Build", "automation": "Nightly check"})
        with self.assertRaises(timer.TimerError) as caught:
            timer.save(self.config, {"name": "nightly build", "automation": "Nightly check"})
        said = str(caught.exception)
        self.assertIn("Nightly Build", said)
        self.assertIn("capitals", said, "and it says why")
        # And the first one is untouched.
        self.assertEqual(
            [one.name for one in timer.every_one(self.config)], ["Nightly Build"]
        )

    def test_saving_the_same_one_again_is_not_a_collision(self) -> None:
        one = self.a_timer()
        one.at = "03:00"
        again = timer.save(self.config, one.to_dict())
        self.assertEqual(again.at, "03:00")


class HowManyItMissed(TimerTestCase):
    def test_a_number_it_stopped_counting_is_said_as_more_than(self) -> None:
        """Said as a number, it would be a wrong number."""

        self.assertEqual(timer.how_many_missed_in_words(0), "")
        self.assertIn("3 missed", timer.how_many_missed_in_words(3))
        said = timer.how_many_missed_in_words(timer.MOST_COUNTED)
        self.assertIn("more than", said)
        self.assertIn(str(timer.MOST_COUNTED), said)

    def test_a_very_long_outage_is_counted_no_further_than_that(self) -> None:
        self.a_timer(how_often="every-hour")
        timer.looked_just_now(self.config, datetime(2020, 1, 1, 12, 0))
        began = time.monotonic()
        due = timer.what_is_due(self.config, datetime(2026, 8, 19, 12, 0))
        self.assertLess(time.monotonic() - began, 2.0, "and it does not take all day")
        _one, missed = due[0]
        self.assertEqual(missed, timer.MOST_COUNTED)
        self.assertIn("more than", timer.how_many_missed_in_words(missed))


class ARunByHand(TimerTestCase):
    def test_it_is_written_down(self) -> None:
        """Pressing the button used to leave "last ran" saying nothing, for ever."""

        one = self.a_timer()
        timer.write_down_a_run(self.config, one, "all good", True, by_hand=True)
        kept = timer.load(self.config, "Every night")
        self.assertEqual(len(kept.runs), 1)
        self.assertTrue(kept.runs[-1]["passed"])

    def test_it_does_not_push_the_next_one_back(self) -> None:
        """One extra, not one instead."""

        one = self.a_timer()
        timer.looked_just_now(self.config, datetime(2026, 8, 19, 12, 0))
        timer.write_down_a_run(
            self.config, one, "all good", True,
            when=datetime(2026, 8, 19, 13, 0), by_hand=True,
        )
        due = timer.what_is_due(self.config, datetime(2026, 8, 20, 2, 30))
        self.assertEqual([held.name for held, _missed in due], ["Every night"])

    def test_a_run_the_timer_started_does_move_it(self) -> None:
        one = self.a_timer()
        timer.looked_just_now(self.config, datetime(2026, 8, 19, 12, 0))
        timer.write_down_a_run(
            self.config, one, "all good", True, when=datetime(2026, 8, 20, 2, 30)
        )
        self.assertEqual(timer.what_is_due(self.config, datetime(2026, 8, 20, 2, 31)), [])


class WritingDownARunDoesNotUndoAnEdit(TimerTestCase):
    def test_a_switch_flipped_during_a_run_stays_flipped(self) -> None:
        """A run may take the best part of an hour. Somebody who turned the
        timer off halfway through found it turned back on afterwards, by a run
        that had been holding a copy from before they touched it."""

        held_from_before = self.a_timer()
        # While it runs, somebody turns it off.
        meanwhile = timer.load(self.config, "Every night")
        meanwhile.turned_on = False
        timer.save(self.config, meanwhile.to_dict())
        # Now the run finishes and writes down what it did.
        timer.write_down_a_run(self.config, held_from_before, "all good", True)
        after = timer.load(self.config, "Every night")
        self.assertFalse(after.turned_on, "it must not be turned back on")
        self.assertEqual(len(after.runs), 1, "and the run is still written down")

    def test_a_time_changed_during_a_run_stays_changed(self) -> None:
        held_from_before = self.a_timer()
        meanwhile = timer.load(self.config, "Every night")
        meanwhile.at = "05:30"
        meanwhile.automation = "Nightly check"
        timer.save(self.config, meanwhile.to_dict())
        timer.write_down_a_run(self.config, held_from_before, "all good", True)
        self.assertEqual(timer.load(self.config, "Every night").at, "05:30")

    def test_a_run_written_by_somebody_else_is_not_lost(self) -> None:
        one = self.a_timer()
        two = timer.load(self.config, "Every night")
        timer.write_down_a_run(self.config, one, "the first", True)
        timer.write_down_a_run(self.config, two, "the second", True)
        said = [run["said"] for run in timer.load(self.config, "Every night").runs]
        self.assertEqual(said, ["the first", "the second"])

    def test_the_list_of_runs_does_not_grow_for_ever(self) -> None:
        one = self.a_timer()
        for count in range(timer.HOW_MANY_KEPT + 5):
            timer.write_down_a_run(self.config, one, f"run {count}", True)
        kept = timer.load(self.config, "Every night").runs
        self.assertEqual(len(kept), timer.HOW_MANY_KEPT)
        self.assertEqual(kept[-1]["said"], f"run {timer.HOW_MANY_KEPT + 4}")

    def test_a_key_printed_by_a_failing_step_is_not_written_down(self) -> None:
        """The docs tell people to commit this folder. A key in a failing
        command's output would be committed with it, for good."""

        one = self.a_timer()
        timer.write_down_a_run(
            self.config, one,
            "it failed: Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz012345",
            False,
        )
        written = (timer.folder(self.config) / "every-night.json").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz012345", written)


class ARunCannotBringATimerBack(TimerTestCase):
    def test_one_taken_off_while_it_ran_stays_off(self) -> None:
        """A run may take the best part of an hour. Somebody who took the timer
        off halfway through found it back afterwards, put there by the run that
        had not heard."""

        held_from_before = self.a_timer()
        timer.remove(self.config, "Every night")
        where = timer.folder(self.config) / "every-night.json"
        self.assertFalse(where.is_file())

        timer.write_down_a_run(self.config, held_from_before, "all good", True)
        self.assertFalse(where.is_file(), "it must not come back")

    def test_it_does_not_write_over_a_different_timer(self) -> None:
        """Two names can come to one file. Taking the first off and making the
        second frees the name - and the first one's run, finishing later, put
        the dead one back over the new one."""

        held_from_before = self.a_timer()
        timer.remove(self.config, "Every night")
        timer.save(self.config, {
            "name": "EVERY NIGHT", "automation": "Nightly check", "at": "09:15",
        })
        timer.write_down_a_run(self.config, held_from_before, "all good", True)
        after = timer.load(self.config, "EVERY NIGHT")
        self.assertEqual(after.name, "EVERY NIGHT")
        self.assertEqual(after.at, "09:15", "the new one is untouched")
        self.assertEqual(after.runs, [], "and did not take the dead one's runs")

    def test_the_run_is_still_handed_back(self) -> None:
        """Not kept is not the same as not run. Whoever asked still hears."""

        held_from_before = self.a_timer()
        timer.remove(self.config, "Every night")
        timer.write_down_a_run(self.config, held_from_before, "all good", True)
        self.assertEqual(held_from_before.runs[-1]["said"], "all good")


class NothingPrintedGivesAKeyAway(TimerTestCase):
    A_KEY = "sk-abcdefghijklmnopqrstuvwxyz012345"

    def a_run_that_says(self, said: str):
        """Stand in for the whole run, so what it says really is what it says.

        Standing in for one step is not enough: the words that come back at the
        top are put together from the step labels, so a key printed by a step
        never reached them and the test passed with the cleaning turned off.
        """

        class Pretend:
            passed = False

            def __init__(self) -> None:
                self.said = said

        return mock.patch.object(pipelines, "run_it", lambda *a, **k: Pretend())

    def test_what_the_runner_hands_back_is_already_clean(self) -> None:
        """Cleaned only where it was written down, the same words still reached
        the terminal, the panel and the log."""

        self.a_timer()
        timer.looked_just_now(self.config, datetime.now() - timedelta(days=1))
        with self.a_run_that_says(f"it failed: Bearer {self.A_KEY}"):
            said = timer.run_what_is_due(self.config)
        self.assertTrue(said["ran"], said)
        self.assertNotIn(self.A_KEY, json.dumps(said))
        self.assertIn("it failed", said["ran"][0]["said"], "and the rest is kept")

    def test_the_test_above_would_notice_if_the_cleaning_stopped(self) -> None:
        """A test that passes with the thing it tests turned off is not a test.

        The first way this was written did exactly that, so the cleaning is
        turned off here on purpose and the key is expected to get through.
        """

        self.a_timer()
        timer.looked_just_now(self.config, datetime.now() - timedelta(days=1))
        # The durable run store is a second mandatory sink boundary now. Turn
        # off both boundaries so this sentinel still proves the assertion can
        # detect a complete redaction regression.
        with self.a_run_that_says(f"it failed: Bearer {self.A_KEY}"), \
                mock.patch.object(timer, "in_safe_words", lambda config, said: said), \
                mock.patch(
                    "our_harness.pipeline_runs.CredentialRedactor.value",
                    lambda redactor, value: value,
                ):
            said = timer.run_what_is_due(self.config)
        self.assertIn(self.A_KEY, json.dumps(said))

    def test_it_is_taken_out_of_a_failure_as_well(self) -> None:
        self.a_timer(automation="Nothing like it")
        timer.looked_just_now(self.config, datetime.now() - timedelta(days=1))
        said = timer.run_what_is_due(self.config)
        self.assertFalse(said["ran"][0]["passed"])
        self.assertIn("Nothing like it", said["ran"][0]["said"])

    def test_the_cleaning_is_there_for_anybody_to_use(self) -> None:
        self.assertNotIn(
            self.A_KEY,
            timer.in_safe_words(self.config, f"Authorization: Bearer {self.A_KEY}"),
        )


class ARecordNobodyCanRead(TimerTestCase):
    def test_it_is_put_aside_and_said_out_loud(self) -> None:
        """Read as nothing, every timer looked brand new and the week the
        machine was off went missing without a word."""

        self.a_timer()
        timer.looked_just_now(self.config, datetime(2026, 8, 19, 12, 0))
        where = timer.folder(self.config) / ".what-happened.json"
        where.write_text("{ this is not json", encoding="utf-8")
        self.assertEqual(timer._what_happened(self.config), {})
        self.assertFalse(where.is_file(), "the bad one is moved out of the way")
        beside = where.with_name(where.name + ".could-not-be-read")
        self.assertTrue(beside.is_file(), "and kept, to be looked at")
        said = timer.what_could_not_be_read(self.config)
        self.assertIn("could not be read", said)

    def test_nothing_is_said_when_there_is_nothing_wrong(self) -> None:
        self.a_timer()
        timer.looked_just_now(self.config)
        self.assertEqual(timer.what_could_not_be_read(self.config), "")


class SavingIsWhatRefuses(TimerTestCase):
    """The gate is in the one function that really writes a timer down.

    Kept at each caller, it was missed by the next caller somebody added, which
    is how the panel came to be the only place that asked.
    """

    def setUp(self) -> None:
        super().setUp()
        pipelines.save(self.config, {
            "name": "Asks first",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "ask", "kind": "wait_for_a_person", "label": "Are you sure?",
                 "settings": {"question": "Shall I?"}},
            ],
            "edges": [{"from": "start", "to": "ask"}],
        })

    def test_it_refuses_one_left_on_that_stops_to_ask_a_person(self) -> None:
        with self.assertRaises(timer.TimerError) as caught:
            timer.save(self.config, {"name": "Risky", "automation": "Asks first"})
        self.assertIn("nobody", str(caught.exception).lower())
        self.assertEqual(timer.every_one(self.config), [], "and nothing is written")

    def test_it_goes_on_when_they_said_to_anyway(self) -> None:
        saved = timer.save(
            self.config, {"name": "Risky", "automation": "Asks first"},
            they_meant_it=True,
        )
        self.assertTrue(saved.turned_on)

    def test_one_turned_off_is_never_refused(self) -> None:
        saved = timer.save(self.config, {
            "name": "Risky", "automation": "Asks first", "turned_on": False,
        })
        self.assertFalse(saved.turned_on)

    def test_turning_one_back_on_is_refused_the_same_way(self) -> None:
        timer.save(self.config, {
            "name": "Risky", "automation": "Asks first", "turned_on": False,
        })
        one = timer.load(self.config, "Risky")
        one.turned_on = True
        with self.assertRaises(timer.TimerError):
            timer.save(self.config, one.to_dict())
        self.assertFalse(timer.load(self.config, "Risky").turned_on)

    def test_moving_an_on_timer_onto_one_that_asks_is_refused(self) -> None:
        one = self.a_timer()
        one.automation = "Asks first"
        with self.assertRaises(timer.TimerError):
            timer.save(self.config, one.to_dict())
        self.assertEqual(timer.load(self.config, "Every night").automation,
                         "Nightly check")


class NothingFiresEarly(TimerTestCase):
    def test_adding_one_does_not_set_it_off(self) -> None:
        """Added at noon, a two-in-the-morning job must not run at noon."""

        self.a_timer()
        timer.looked_just_now(self.config, datetime(2026, 8, 19, 12, 0))
        self.assertEqual(
            timer.what_is_due(self.config, datetime(2026, 8, 19, 12, 1)), []
        )

    def test_it_is_due_once_its_time_has_passed(self) -> None:
        self.a_timer()
        timer.looked_just_now(self.config, datetime(2026, 8, 19, 12, 0))
        due = timer.what_is_due(self.config, datetime(2026, 8, 20, 2, 30))
        self.assertEqual([one.name for one, _missed in due], ["Every night"])

    def test_a_timer_turned_off_is_not_due(self) -> None:
        self.a_timer(turned_on=False)
        timer.looked_just_now(self.config, datetime(2026, 8, 19, 12, 0))
        self.assertEqual(
            timer.what_is_due(self.config, datetime(2026, 8, 25, 12, 0)), []
        )

    def test_a_machine_off_for_a_week_comes_back_to_one_run(self) -> None:
        """Not a hundred and sixty-eight, and it says how many it skipped."""

        self.a_timer(how_often="every-hour")
        timer.looked_just_now(self.config, datetime(2026, 8, 19, 12, 0))
        due = timer.what_is_due(self.config, datetime(2026, 8, 26, 12, 0))
        self.assertEqual(len(due), 1, "one run, not a pile of them")
        _one, missed = due[0]
        self.assertGreater(missed, 100, "and it knows how many it skipped")


class WhatHappensWhenItRuns(TimerTestCase):
    def test_long_visible_history_is_marked_and_points_to_full_run(self) -> None:
        one = self.a_timer()
        long_result = "start-" + "x" * 800 + "-tail-sentinel"
        timer.write_down_a_run(
            self.config, one, long_result, False, run_id="exact-long-run"
        )
        kept = timer.load(self.config, "Every night").runs[-1]
        self.assertTrue(kept["said_truncated"])
        self.assertEqual(kept["said_original_characters"], len(long_result))
        self.assertEqual(kept["full_result_reference"], "pipeline-run:exact-long-run")
        self.assertLessEqual(len(kept["said"]), timer.RUN_HISTORY_SAID_CHARACTERS)
        self.assertIn("shortened from", kept["said"])
        self.assertIn("pipeline-run:exact-long-run", kept["said"])
        self.assertNotIn("tail-sentinel", kept["said"])

    def test_history_without_a_full_run_reference_is_never_silently_cut(self) -> None:
        one = self.a_timer()
        long_result = "y" * 800 + "-unreferenced-tail"
        timer.write_down_a_run(self.config, one, long_result, False, run_id="")
        kept = timer.load(self.config, "Every night").runs[-1]
        self.assertFalse(kept["said_truncated"])
        self.assertEqual(kept["said"], long_result)
        self.assertEqual(kept["said_original_characters"], len(long_result))
        self.assertEqual(kept["full_result_reference"], "")

    def test_a_notification_points_to_the_exact_persisted_automation_run(self) -> None:
        one = timer.Timer(name="Every night", automation="Nightly check")
        with mock.patch(
            "our_harness.tell_somebody.tell_everybody", return_value=[]
        ) as tell:
            timer._tell_somebody_about_it(
                self.config, one, False, "failed", run_id="exact-run-123"
            )
        self.assertEqual(
            tell.call_args.kwargs["full_result_reference"],
            "Nexus → Visual test automation → run exact-run-123 "
            "(pipeline-run:exact-run-123)",
        )

    def test_it_runs_the_automation_and_writes_down_what_happened(self) -> None:
        self.a_timer()
        timer.looked_just_now(self.config, datetime(2026, 8, 19, 12, 0))
        with self.stand_in():
            said = timer.run_what_is_due(self.config, datetime(2026, 8, 20, 2, 30))
        self.assertEqual(len(said["ran"]), 1)
        self.assertTrue(said["ran"][0]["passed"])
        kept = timer.load(self.config, "Every night")
        self.assertEqual(len(kept.runs), 1)
        self.assertTrue(kept.runs[-1]["passed"])
        self.assertIn("passed", kept.runs[-1]["said"])
        self.assertTrue(said["ran"][0]["run_id"])
        self.assertEqual(kept.runs[-1]["run_id"], said["ran"][0]["run_id"])

    def test_a_run_that_failed_is_written_down_as_one(self) -> None:
        self.a_timer()
        timer.looked_just_now(self.config, datetime(2026, 8, 19, 12, 0))
        with self.stand_in(passed=False, said="the tests broke"):
            said = timer.run_what_is_due(self.config, datetime(2026, 8, 20, 2, 30))
        self.assertFalse(said["ran"][0]["passed"])
        self.assertIn("did not pass", said["ran"][0]["said"])

    def test_an_automation_that_has_gone_is_said_plainly(self) -> None:
        self.a_timer(automation="Never saved")
        timer.looked_just_now(self.config, datetime(2026, 8, 19, 12, 0))
        said = timer.run_what_is_due(self.config, datetime(2026, 8, 20, 2, 30))
        self.assertFalse(said["ran"][0]["passed"])
        self.assertIn("Never saved", said["ran"][0]["said"])
        self.assertNotIn("Traceback", said["ran"][0]["said"])

    def test_running_twice_does_not_run_it_twice(self) -> None:
        """The second look must find nothing owed."""

        self.a_timer()
        timer.looked_just_now(self.config, datetime(2026, 8, 19, 12, 0))
        with self.stand_in():
            first = timer.run_what_is_due(self.config, datetime(2026, 8, 20, 2, 30))
            second = timer.run_what_is_due(self.config, datetime(2026, 8, 20, 2, 31))
        self.assertEqual(len(first["ran"]), 1)
        self.assertEqual(second["ran"], [])

    def test_nothing_due_writes_nothing_but_the_look(self) -> None:
        self.a_timer()
        timer.looked_just_now(self.config, datetime(2026, 8, 19, 12, 0))
        said = timer.run_what_is_due(self.config, datetime(2026, 8, 19, 12, 1))
        self.assertEqual(said["ran"], [])
        self.assertEqual(timer.load(self.config, "Every night").runs, [])

    def test_only_so_many_runs_are_remembered(self) -> None:
        one = self.a_timer()
        one.runs = [{"at": f"2026-08-{n:02d}T02:00:00", "passed": True, "said": "x",
                     "missed": 0} for n in range(1, timer.HOW_MANY_KEPT + 6)]
        timer.save(self.config, one.to_dict())
        # Saving keeps what was there, so read it back from a fresh save.
        kept = timer.read_it(json.loads(
            timer._where_it_lives(self.config, "Every night").read_text(encoding="utf-8")
        ))
        self.assertLessEqual(len(kept.runs), timer.HOW_MANY_KEPT)


class NotTwoAtOnce(TimerTestCase):
    def test_a_second_run_stands_aside_while_the_first_is_going(self) -> None:
        """A suite that takes longer than the gap would pile up on itself."""

        self.a_timer(how_often="every-hour")
        timer.looked_just_now(self.config, datetime(2026, 8, 19, 12, 0))
        started = threading.Event()
        carry_on = threading.Event()
        second: list = []

        def slow(config, node, before, results, order, check_kinds, depth=0,
                 stopping=None, waiting_on=None):
            if node["kind"] == "start":
                return True, "Started", ""
            started.set()
            carry_on.wait(timeout=10)
            return True, "done at last", ""

        def the_other_one():
            started.wait(timeout=10)
            second.append(
                timer.run_what_is_due(self.config, datetime(2026, 8, 19, 14, 30))
            )
            carry_on.set()

        other = threading.Thread(target=the_other_one, daemon=True)
        other.start()
        with mock.patch.object(pipelines, "_do_one", slow):
            first = timer.run_what_is_due(self.config, datetime(2026, 8, 19, 13, 30))
        other.join(timeout=15)
        self.assertEqual(len(first["ran"]), 1)
        self.assertEqual(second[0]["ran"], [], "the second one stood aside")
        self.assertIn("still going", second[0]["note"])

    def test_a_lock_left_by_a_machine_that_lost_power_does_not_stop_it_for_ever(self) -> None:
        self.a_timer()
        timer.looked_just_now(self.config, datetime(2026, 8, 19, 12, 0))
        left_behind = timer.folder(self.config) / "running.lock"
        left_behind.parent.mkdir(parents=True, exist_ok=True)
        left_behind.write_text(str(_a_process_that_has_finished()), encoding="utf-8")
        # As if it were left days ago.
        long_ago = time.time() - timer.LONGEST_RUN_SECONDS * 4
        import os as the_os
        the_os.utime(left_behind, (long_ago, long_ago))
        with self.stand_in():
            said = timer.run_what_is_due(self.config, datetime(2026, 8, 20, 2, 30))
        self.assertEqual(len(said["ran"]), 1, said["note"])


class TheLockIsOnlyTakenFromSomethingThatStopped(TimerTestCase):
    def test_a_run_that_is_still_going_keeps_its_lock(self) -> None:
        """Judged on how long the run had taken, a slow suite had its lock
        taken away and was started a second time alongside itself."""

        where = timer.folder(self.config) / "running.lock"
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text("1234", encoding="utf-8")
        # Left a long time ago by the clock, but touched a moment ago, which is
        # what a run that is still working does.
        import os as the_os

        the_os.utime(where, None)
        self.assertIsNone(
            timer._only_one_at_a_time(self.config), "it must not be taken"
        )

    def test_a_lock_nothing_has_touched_is_taken(self) -> None:
        import os as the_os

        where = timer.folder(self.config) / "running.lock"
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(str(_a_process_that_has_finished()), encoding="utf-8")
        long_ago = time.time() - timer.LEFT_BEHIND_AFTER * 2
        the_os.utime(where, (long_ago, long_ago))
        self.assertIsNotNone(timer._only_one_at_a_time(self.config))

    def test_a_lock_is_not_taken_from_a_run_that_is_still_there(self) -> None:
        """Both the clock and the file's own time are the machine's wall
        clock, and that can jump. A jump forward made a run that was working
        look long dead, and its whole suite was started a second time."""

        import os as the_os

        where = timer.folder(self.config) / "running.lock"
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(str(the_os.getpid()), encoding="utf-8")
        long_ago = time.time() - timer.LEFT_BEHIND_AFTER * 20
        the_os.utime(where, (long_ago, long_ago))
        self.assertIsNone(
            timer._only_one_at_a_time(self.config),
            "this very process is holding it, whatever the clock says",
        )

    def test_a_number_handed_out_again_is_not_the_same_run(self) -> None:
        """The machine gives the same number to somebody else once the first
        program is gone. Read as "still going", the lock was never taken back
        and every timer in the project stopped for good."""

        import os as the_os

        where = timer.folder(self.config) / "running.lock"
        where.parent.mkdir(parents=True, exist_ok=True)
        # This process, but said to have started at a moment it did not.
        where.write_text(json.dumps({
            "process": the_os.getpid(),
            "started": 1,
            "machine": __import__("platform").node(),
        }), encoding="utf-8")
        long_ago = time.time() - timer.LEFT_BEHIND_AFTER * 2
        the_os.utime(where, (long_ago, long_ago))
        self.assertIsNotNone(
            timer._only_one_at_a_time(self.config),
            "a number that started at another moment is another program",
        )

    def test_a_lock_from_another_machine_is_not_believed(self) -> None:
        """One committed to a repository by mistake, or left on a shared
        folder. The number in it means nothing here."""

        import os as the_os

        where = timer.folder(self.config) / "running.lock"
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(json.dumps({
            "process": the_os.getpid(),
            "started": timer._when_that_process_started(the_os.getpid()),
            "machine": "somebody-elses-laptop",
        }), encoding="utf-8")
        long_ago = time.time() - timer.LEFT_BEHIND_AFTER * 2
        the_os.utime(where, (long_ago, long_ago))
        self.assertIsNotNone(timer._only_one_at_a_time(self.config))

    def test_no_lock_is_believed_for_ever(self) -> None:
        """Whatever it says, and however sure we are, a day is the end of it.
        Otherwise the lock stops being a lock and becomes a project that never
        runs anything again."""

        import os as the_os

        where = timer.folder(self.config) / "running.lock"
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(json.dumps(timer._who_is_holding_it()), encoding="utf-8")
        long_ago = time.time() - timer.NEVER_HELD_LONGER_THAN - 60
        the_os.utime(where, (long_ago, long_ago))
        self.assertIsNotNone(timer._only_one_at_a_time(self.config))

    def test_a_lock_written_the_old_way_is_still_read(self) -> None:
        """One left by a version that wrote nothing but a number."""

        import os as the_os

        where = timer.folder(self.config) / "running.lock"
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(str(the_os.getpid()), encoding="utf-8")
        long_ago = time.time() - timer.LEFT_BEHIND_AFTER * 2
        the_os.utime(where, (long_ago, long_ago))
        self.assertIsNone(
            timer._only_one_at_a_time(self.config),
            "the number is all it says, and that number is going",
        )

    def test_a_lock_says_who_is_holding_it(self) -> None:
        import os as the_os

        held = timer._only_one_at_a_time(self.config)
        self.assertIsNotNone(held)
        said = json.loads(held.read_text(encoding="utf-8"))
        self.assertEqual(said["process"], the_os.getpid())
        self.assertEqual(said["machine"], __import__("platform").node())

    def test_when_a_process_started_is_the_same_answer_twice(self) -> None:
        import os as the_os

        first = timer._when_that_process_started(the_os.getpid())
        self.assertEqual(first, timer._when_that_process_started(the_os.getpid()))
        self.assertIsNone(timer._when_that_process_started(0))

    def test_this_process_is_known_to_be_going(self) -> None:
        import os as the_os

        self.assertTrue(timer._is_it_still_going(the_os.getpid()))
        self.assertFalse(timer._is_it_still_going(_a_process_that_has_finished()))
        self.assertFalse(timer._is_it_still_going(0), "nothing said means nothing")

    def test_a_lock_that_does_not_say_who_left_it_can_be_taken(self) -> None:
        import os as the_os

        where = timer.folder(self.config) / "running.lock"
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text("", encoding="utf-8")
        long_ago = time.time() - timer.LEFT_BEHIND_AFTER * 2
        the_os.utime(where, (long_ago, long_ago))
        self.assertIsNotNone(timer._only_one_at_a_time(self.config))


class TheLineForThisMachine(TimerTestCase):
    def test_every_path_in_it_is_quoted(self) -> None:
        """This project's own folder has a space in its name, and so does the
        usual place Python is installed for everybody. Left bare, the line
        looks right, is accepted by the machine, and never runs."""

        with_a_space = Path(str(self.root)) / "Some Folder"
        with_a_space.mkdir()
        (with_a_space / ".harness").mkdir()
        config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), with_a_space, [], {})
        how = timer.how_to_ask_this_machine(config)
        said = how["what"]
        self.assertIn("Some Folder", said)
        # The folder appears with quotes around it, whichever kind this
        # machine uses.
        wrapped = (
            f'"{with_a_space}"' in said
            or f"'{with_a_space}'" in said
            or f'\\"{with_a_space}\\"' in said
        )
        self.assertTrue(wrapped, said)

    def test_it_names_the_command_and_how_to_take_it_off(self) -> None:
        how = timer.how_to_ask_this_machine(self.config, 10)
        self.assertIn("timer run", how["what"])
        self.assertTrue(how["to_take_it_off"])
        self.assertTrue(how["to_see_it"])
        self.assertTrue(how["machine"])

    def test_it_never_asks_the_machine_itself(self) -> None:
        """Asking a machine to start something is somebody's own decision."""

        with mock.patch("subprocess.run") as ran:
            with mock.patch("subprocess.Popen") as opened:
                timer.how_to_ask_this_machine(self.config, 10)
        ran.assert_not_called()
        opened.assert_not_called()

    def test_the_task_name_is_safe_to_use_as_a_name(self) -> None:
        for awkward in ("My Project", "a/b", "..", "one two three"):
            with self.subTest(name=awkward):
                said = timer.project_short_name(Path(awkward))
                self.assertTrue(said)
                self.assertNotIn("/", said)
                self.assertNotIn("\\\\", said)
                self.assertNotIn("..", said)


if __name__ == "__main__":
    unittest.main()


class OnlyTheOneRealReasonRefuses(TimerTestCase):
    """An automation nobody has drawn yet is somebody's next job.

    Read as a reason to refuse, saving a timer for an automation that does not
    exist yet became impossible, and the words it gave were about waiting for a
    person, which was not what was wrong at all.
    """

    def test_a_timer_can_point_at_an_automation_nobody_has_drawn_yet(self) -> None:
        saved = timer.save(self.config, {
            "name": "For later", "automation": "Not drawn yet",
        })
        self.assertTrue(saved.turned_on)

    def test_that_is_still_worth_saying_out_loud(self) -> None:
        said = timer.what_stops_it_running_alone(self.config, "Not drawn yet")
        self.assertIn("Not drawn yet", said)
        self.assertEqual(
            timer.does_it_stop_to_ask_a_person(self.config, "Not drawn yet"), ""
        )


class AnAskHiddenInsideAnotherAutomation(TimerTestCase):
    """One automation can run another as a single step.

    Looking only at the steps drawn on the one being put on a timer missed the
    ask completely. The timer went on with no warning at all, and at two in the
    morning the outer one started the inner one, which sat there waiting for
    somebody who had gone home.
    """

    def an_automation(self, name: str, nodes: list[dict]) -> None:
        pipelines.save(self.config, {
            "name": name,
            "nodes": [{"id": "start", "kind": "start", "label": "Start"}, *nodes],
            "edges": [{"from": "start", "to": nodes[0]["id"]}],
        })

    def setUp(self) -> None:
        super().setUp()
        self.an_automation("Inner", [
            {"id": "ask", "kind": "wait_for_a_person", "label": "Are you sure?",
             "settings": {"question": "Shall I?"}},
        ])
        self.an_automation("Outer", [
            {"id": "inside", "kind": "another_pipeline", "label": "Run the inner one",
             "settings": {"pipeline": "Inner"}},
        ])

    def test_it_is_found_one_deep(self) -> None:
        said = timer.what_stops_it_running_alone(self.config, "Outer")
        self.assertIn("Are you sure?", said)
        self.assertIn("Inner", said, "and it says which one to go and look at")

    def test_saving_it_is_refused_the_same_way(self) -> None:
        with self.assertRaises(timer.TimerError) as caught:
            timer.save(self.config, {"name": "Nightly", "automation": "Outer"})
        self.assertIn("nobody", str(caught.exception).lower())
        self.assertEqual(timer.every_one(self.config), [])

    def test_it_is_found_two_deep(self) -> None:
        self.an_automation("Outermost", [
            {"id": "inside", "kind": "another_pipeline", "label": "Run the outer one",
             "settings": {"pipeline": "Outer"}},
        ])
        self.assertIn(
            "Are you sure?",
            timer.what_stops_it_running_alone(self.config, "Outermost"),
        )

    def test_two_that_run_each_other_do_not_go_round_for_ever(self) -> None:
        self.an_automation("Ping", [
            {"id": "inside", "kind": "another_pipeline", "label": "Pong",
             "settings": {"pipeline": "Pong"}},
        ])
        self.an_automation("Pong", [
            {"id": "inside", "kind": "another_pipeline", "label": "Ping",
             "settings": {"pipeline": "Ping"}},
        ])
        began = time.monotonic()
        self.assertEqual(timer.what_stops_it_running_alone(self.config, "Ping"), "")
        self.assertLess(time.monotonic() - began, 5.0)

    def test_a_step_that_does_not_say_which_one_to_run(self) -> None:
        self.an_automation("Says nothing", [
            {"id": "inside", "kind": "another_pipeline", "label": "Run something",
             "settings": {}},
        ])
        self.assertEqual(
            timer.what_stops_it_running_alone(self.config, "Says nothing"), ""
        )

    def test_one_that_runs_an_automation_nobody_drew(self) -> None:
        self.an_automation("Points at nothing", [
            {"id": "inside", "kind": "another_pipeline", "label": "Run it",
             "settings": {"pipeline": "Not drawn yet"}},
        ])
        self.assertEqual(
            timer.what_stops_it_running_alone(self.config, "Points at nothing"), ""
        )


class WritingAFileSomethingElseIsReading(unittest.TestCase):
    """Windows will not move a file over one anything has open to read.

    A settings file written while two checks were reading it handed back a page
    of machine detail for a write that would have worked a tenth of a second
    later. The same patience the timer already had is now there for everybody.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.where = Path(self.temporary.name).resolve() / "held" / "settings.json"

    def test_it_writes_and_leaves_nothing_beside_it(self) -> None:
        safety.put_this_file_in_place(self.where, "the first\n")
        safety.put_this_file_in_place(self.where, "the second\n")
        self.assertEqual(self.where.read_text(encoding="utf-8"), "the second\n")
        beside = [one.name for one in self.where.parent.iterdir()]
        self.assertEqual(beside, ["settings.json"], "no half-written leftovers")

    def test_it_waits_for_whoever_has_it_open(self) -> None:
        safety.put_this_file_in_place(self.where, "the first\n")
        letting_go = threading.Event()

        def hold_it_open() -> None:
            # Let go well inside the waiting, so this test is about the waiting
            # working and not about how long it is willing to wait.
            with self.where.open("r", encoding="utf-8"):
                letting_go.wait(0.3)

        holding = threading.Thread(target=hold_it_open)
        holding.start()
        try:
            time.sleep(0.05)
            safety.put_this_file_in_place(self.where, "the second\n")
        finally:
            letting_go.set()
            holding.join(timeout=5)
        self.assertEqual(self.where.read_text(encoding="utf-8"), "the second\n")

    def test_two_writing_at_once_do_not_take_each_other_s_half(self) -> None:
        went_wrong: list[str] = []

        def write_it(which: str) -> None:
            try:
                for _ in range(20):
                    safety.put_this_file_in_place(self.where, which * 500)
                    # Read patiently as well. Writing this test the plain way
                    # showed the other half of the same problem: a reader loses
                    # the race to a file being moved into place just as a
                    # writer does.
                    said = safety.read_this_file_patiently(self.where)
                    if said not in (("a" * 500), ("b" * 500)):
                        went_wrong.append(said[:40])
            except Exception as exc:  # noqa: BLE001 - said out loud below
                went_wrong.append(f"{type(exc).__name__}: {exc}")

        both = [threading.Thread(target=write_it, args=(one,)) for one in ("a", "b")]
        for one in both:
            one.start()
        for one in both:
            one.join(timeout=30)
        self.assertEqual(went_wrong, [])

    def test_reading_waits_for_a_file_being_moved_into_place(self) -> None:
        safety.put_this_file_in_place(self.where, "the first\n")
        tries = {"n": 0}
        really = Path.read_text

        def sometimes(self_path, *args, **named):
            tries["n"] += 1
            if tries["n"] <= 3:
                raise PermissionError("being moved into place")
            return really(self_path, *args, **named)

        with mock.patch.object(Path, "read_text", sometimes):
            self.assertEqual(
                safety.read_this_file_patiently(self.where), "the first\n"
            )

    def test_reading_says_something_plain_when_it_really_cannot(self) -> None:
        safety.put_this_file_in_place(self.where, "the first\n")
        with mock.patch.object(Path, "read_text", side_effect=PermissionError):
            with self.assertRaises(HarnessError) as caught:
                safety.read_this_file_patiently(self.where)
        said = str(caught.exception)
        self.assertIn("settings.json", said)
        # Both reasons, because after six tries the harness cannot tell them
        # apart and guessing one sends somebody the wrong way.
        self.assertIn("writing it", said)
        self.assertIn("permissions", said)

    def test_it_says_something_plain_when_it_really_cannot(self) -> None:
        with mock.patch("our_harness.safety.os.replace", side_effect=PermissionError):
            with self.assertRaises(HarnessError) as caught:
                safety.put_this_file_in_place(self.where, "never lands\n")
        said = str(caught.exception)
        self.assertIn("settings.json", said)
        self.assertIn("held open", said)
        self.assertEqual(
            list(self.where.parent.iterdir()), [], "and it tidies up after itself"
        )

    def test_a_transient_replace_collision_retries_the_unique_file_and_cleans_it(self) -> None:
        real_replace = os.replace
        attempts = 0
        temporary_names: list[str] = []

        def transient(source, destination):
            nonlocal attempts
            attempts += 1
            temporary_names.append(Path(source).name)
            if attempts < 3:
                raise PermissionError("another process is publishing")
            return real_replace(source, destination)

        with mock.patch("our_harness.safety.os.replace", side_effect=transient):
            safety.put_this_file_in_place(self.where, "eventually lands\n")

        self.assertEqual(self.where.read_text(encoding="utf-8"), "eventually lands\n")
        self.assertEqual(attempts, 3)
        self.assertEqual(len(set(temporary_names)), 1, "retries moved the exact same file")
        self.assertIn(f".{os.getpid()}-", temporary_names[0])
        self.assertEqual(
            list(self.where.parent.iterdir()), [self.where],
            "successful retry left its unique temporary file behind",
        )


class AsDeepAsARunReallyGoes(TimerTestCase):
    """Nearly the right depth is worse than not trying.

    Counted one automation short, the last one a run really does reach was
    never opened, and an ask sitting in it was invisible: no warning, no
    --anyway, and a night spent waiting for somebody who had gone home.
    """

    def a_chain(self, how_many: int, ask_at_the_end: bool = True) -> None:
        """Outer runs P1, P1 runs P2, and so on, with the ask on the last."""

        names = ["Outer"] + [f"P{one}" for one in range(1, how_many + 1)]
        for spot, name in enumerate(names):
            last = spot == len(names) - 1
            if last and ask_at_the_end:
                step = {"id": "ask", "kind": "wait_for_a_person",
                        "label": "Are you sure?", "settings": {"question": "Shall I?"}}
            elif last:
                step = {"id": "work", "kind": "git_repo", "label": "The work"}
            else:
                step = {"id": "inside", "kind": "another_pipeline",
                        "label": f"Run {names[spot + 1]}",
                        "settings": {"pipeline": names[spot + 1]}}
            pipelines.save(self.config, {
                "name": name,
                "nodes": [{"id": "start", "kind": "start", "label": "Start"}, step],
                "edges": [{"from": "start", "to": step["id"]}],
            })

    def test_an_ask_at_the_deepest_a_run_reaches_is_found(self) -> None:
        # Three steps into another automation is what a run itself follows.
        self.a_chain(pipelines.DEEPEST_NESTING)
        self.assertIn(
            "Are you sure?",
            timer.what_stops_it_running_alone(self.config, "Outer"),
        )
        with self.assertRaises(timer.TimerError):
            timer.save(self.config, {"name": "Nightly", "automation": "Outer"})

    def test_one_deeper_than_a_run_goes_is_not_looked_for(self) -> None:
        """A run refuses that step, so there is nothing to warn about."""

        self.a_chain(pipelines.DEEPEST_NESTING + 1)
        self.assertEqual(timer.what_stops_it_running_alone(self.config, "Outer"), "")

    def test_it_stops_where_a_run_stops_and_not_before(self) -> None:
        for how_many in range(1, pipelines.DEEPEST_NESTING + 1):
            with self.subTest(how_deep=how_many):
                self.setUp()
                self.a_chain(how_many)
                self.assertIn(
                    "Are you sure?",
                    timer.what_stops_it_running_alone(self.config, "Outer"),
                )

    def test_one_automation_run_by_two_others_is_said_once(self) -> None:
        """The same step twice reads like two problems."""

        pipelines.save(self.config, {
            "name": "Shared",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "ask", "kind": "wait_for_a_person", "label": "Are you sure?",
                 "settings": {"question": "Shall I?"}},
            ],
            "edges": [{"from": "start", "to": "ask"}],
        })
        pipelines.save(self.config, {
            "name": "Both",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "one", "kind": "another_pipeline", "label": "First",
                 "settings": {"pipeline": "Shared"}},
                {"id": "two", "kind": "another_pipeline", "label": "Again",
                 "settings": {"pipeline": "Shared"}},
            ],
            "edges": [{"from": "start", "to": "one"}, {"from": "one", "to": "two"}],
        })
        said = timer.what_stops_it_running_alone(self.config, "Both")
        self.assertEqual(said.count("Are you sure?"), 1, said)


class ItSaysWhichDrawingToGoAndLookAt(TimerTestCase):
    """A step's own name is not enough to find it.

    Named with every automation on the way down - "Are you sure? (in P3) (in
    P2) (in P1)" - it reads like three problems and points at none of them. It
    says the one drawing the step is really on, and nothing else.
    """

    def a_chain(self) -> None:
        for name, step in (
            ("Outer", {"id": "inside", "kind": "another_pipeline", "label": "Run P1",
                       "settings": {"pipeline": "P1"}}),
            ("P1", {"id": "inside", "kind": "another_pipeline", "label": "Run P2",
                    "settings": {"pipeline": "P2"}}),
            ("P2", {"id": "ask", "kind": "wait_for_a_person", "label": "Are you sure?",
                    "settings": {"question": "Shall I?"}}),
        ):
            pipelines.save(self.config, {
                "name": name,
                "nodes": [{"id": "start", "kind": "start", "label": "Start"}, step],
                "edges": [{"from": "start", "to": step["id"]}],
            })

    def test_it_names_the_one_the_step_is_on(self) -> None:
        self.a_chain()
        said = timer.what_stops_it_running_alone(self.config, "Outer")
        self.assertIn("Are you sure? (in P2)", said)
        self.assertNotIn("(in P1)", said, "not every one on the way down")

    def test_a_step_on_the_automation_itself_needs_no_in(self) -> None:
        pipelines.save(self.config, {
            "name": "Right here",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "ask", "kind": "wait_for_a_person", "label": "Are you sure?",
                 "settings": {"question": "Shall I?"}},
            ],
            "edges": [{"from": "start", "to": "ask"}],
        })
        said = timer.what_stops_it_running_alone(self.config, "Right here")
        self.assertIn("at: Are you sure?.", said)
        self.assertNotIn("(in ", said)
