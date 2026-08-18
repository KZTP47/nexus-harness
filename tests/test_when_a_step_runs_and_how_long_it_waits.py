"""When a step runs, how long it waits before trying again, and what it used to be.

Three things a pipeline needs before anybody trusts it with real work:

  - A step that only runs when something went wrong, so somebody is told.
  - A step that runs either way, so the record of what happened is written even
    when the run went badly - which is when it matters most.
  - A wait before trying again, because something that failed because another
    thing was busy will fail again straight away.

And one thing it needs before anybody edits it twice: saving over a pipeline
keeps the one that was there, so the Save button cannot lose an afternoon.
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

from our_harness import pipelines
from our_harness.config import DEFAULT_CONFIG, LoadedConfig


class PipelineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})

    def stand_in(self, answers):
        """Every step stood in, so nothing real is started."""

        def one(config, node, before, results, order, check_kinds, depth=0, stopping=None, waiting_on=None):
            if node["kind"] == "start":
                return True, "Started", ""
            return answers.get(node["id"], (True, "done", ""))

        return mock.patch.object(pipelines, "_do_one", one)

    def a_pipeline(self, *, when: str = "when-all-is-well", wait: str = "no-wait",
                   tries: int = 1) -> dict:
        """Start, a step that can fail, and a last step whose rules are being tested."""

        return {
            "name": "A pipeline",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "work", "kind": "git_repo", "label": "The work",
                 "settings": {"tries": tries, "wait": wait}, "at": {"x": 200, "y": 100}},
                {"id": "after", "kind": "git_repo", "label": "The last one",
                 "settings": {"when": when}, "at": {"x": 400, "y": 100}},
            ],
            "edges": [
                {"from": "start", "to": "work"},
                {"from": "work", "to": "after"},
            ],
        }

    def by_id(self, run) -> dict[str, pipelines.NodeResult]:
        return {one.id: one for one in run.nodes}


class WhenAStepRuns(PipelineTestCase):
    def test_the_usual_step_is_skipped_when_something_failed(self) -> None:
        held = pipelines.read_it(self.a_pipeline())
        with self.stand_in({"work": (False, "it broke", "")}):
            run = pipelines.run_it(self.config, held)
        after = self.by_id(run)["after"]
        self.assertEqual(after.state, pipelines.SKIPPED)
        self.assertIn("did not pass", after.said)
        self.assertFalse(run.passed)

    def test_a_step_for_when_things_go_wrong_runs_only_then(self) -> None:
        held = pipelines.read_it(self.a_pipeline(when="when-something-failed"))
        with self.stand_in({"work": (False, "it broke", "")}):
            went_wrong = pipelines.run_it(self.config, held)
        self.assertEqual(self.by_id(went_wrong)["after"].state, pipelines.PASSED)

        with self.stand_in({}):
            went_well = pipelines.run_it(self.config, held)
        after = self.by_id(went_well)["after"]
        self.assertEqual(after.state, pipelines.SKIPPED)
        # Skipped because it was not needed is not the same as skipped because
        # something blocked it, and the run says which.
        self.assertIn("Nothing went wrong", after.said)
        # And a step that was never needed must not make a good run look bad.
        self.assertTrue(went_well.passed, went_well.said)

    def test_a_step_that_always_runs_runs_either_way(self) -> None:
        held = pipelines.read_it(self.a_pipeline(when="whatever-happens"))
        with self.stand_in({"work": (False, "it broke", "")}):
            went_wrong = pipelines.run_it(self.config, held)
        with self.stand_in({}):
            went_well = pipelines.run_it(self.config, held)
        self.assertEqual(self.by_id(went_wrong)["after"].state, pipelines.PASSED)
        self.assertEqual(self.by_id(went_well)["after"].state, pipelines.PASSED)
        # It ran, but the run as a whole still failed. A step that writes down
        # what happened must not turn a bad run into a good one.
        self.assertFalse(went_wrong.passed)

    def test_a_failure_step_on_a_branch_of_its_own_still_runs(self) -> None:
        """The one that shipped broken.

        Two steps that do not wait on each other are put in order by their
        names. Called `aa_tell` and `zz_work`, the step that was there to catch
        a failure ran first, saw nothing broken yet, and was skipped one line
        before the break happened. Whether the feature worked came down to what
        somebody called their steps.
        """

        held = pipelines.read_it({
            "name": "Two branches",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "aa_tell", "kind": "git_repo", "label": "Tell somebody",
                 "settings": {"when": "when-something-failed"}, "at": {"x": 200, "y": 40}},
                {"id": "zz_work", "kind": "git_repo", "label": "The work",
                 "settings": {}, "at": {"x": 200, "y": 200}},
            ],
            "edges": [{"from": "start", "to": "aa_tell"}, {"from": "start", "to": "zz_work"}],
        })
        with self.stand_in({"zz_work": (False, "it broke", "")}):
            run = pipelines.run_it(self.config, held)
        told = self.by_id(run)["aa_tell"]
        self.assertEqual(told.state, pipelines.PASSED, told.said)

    def test_a_failure_step_on_its_own_branch_is_still_skipped_when_all_is_well(self) -> None:
        held = pipelines.read_it({
            "name": "Two branches",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "aa_tell", "kind": "git_repo", "label": "Tell somebody",
                 "settings": {"when": "when-something-failed"}, "at": {"x": 200, "y": 40}},
                {"id": "zz_work", "kind": "git_repo", "label": "The work",
                 "settings": {}, "at": {"x": 200, "y": 200}},
            ],
            "edges": [{"from": "start", "to": "aa_tell"}, {"from": "start", "to": "zz_work"}],
        })
        with self.stand_in({}):
            run = pipelines.run_it(self.config, held)
        told = self.by_id(run)["aa_tell"]
        self.assertEqual(told.state, pipelines.SKIPPED)
        self.assertIn("Nothing went wrong", told.said)
        self.assertTrue(run.passed, run.said)

    def test_two_failure_steps_do_not_set_each_other_off(self) -> None:
        """One skipped as not needed must not look like something going wrong."""

        held = pipelines.read_it({
            "name": "Two handlers",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "aa_tell", "kind": "git_repo", "label": "Tell somebody",
                 "settings": {"when": "when-something-failed"}, "at": {"x": 200, "y": 40}},
                {"id": "bb_tell", "kind": "git_repo", "label": "Tell somebody else",
                 "settings": {"when": "when-something-failed"}, "at": {"x": 200, "y": 200}},
                {"id": "zz_work", "kind": "git_repo", "label": "The work",
                 "settings": {}, "at": {"x": 200, "y": 360}},
            ],
            "edges": [
                {"from": "start", "to": "aa_tell"},
                {"from": "start", "to": "bb_tell"},
                {"from": "start", "to": "zz_work"},
            ],
        })
        with self.stand_in({}):
            run = pipelines.run_it(self.config, held)
        for which in ("aa_tell", "bb_tell"):
            with self.subTest(step=which):
                self.assertEqual(self.by_id(run)[which].state, pipelines.SKIPPED)
        self.assertTrue(run.passed, run.said)

    def test_what_waits_on_a_failure_step_waits_with_it(self) -> None:
        held = pipelines.read_it({
            "name": "A handler with a step after it",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "aa_tell", "kind": "git_repo", "label": "Tell somebody",
                 "settings": {"when": "when-something-failed"}, "at": {"x": 200, "y": 40}},
                {"id": "bb_after", "kind": "git_repo", "label": "And then this",
                 "settings": {}, "at": {"x": 400, "y": 40}},
                {"id": "zz_work", "kind": "git_repo", "label": "The work",
                 "settings": {}, "at": {"x": 200, "y": 200}},
            ],
            "edges": [
                {"from": "start", "to": "aa_tell"},
                {"from": "aa_tell", "to": "bb_after"},
                {"from": "start", "to": "zz_work"},
            ],
        })
        with self.stand_in({"zz_work": (False, "it broke", "")}):
            run = pipelines.run_it(self.config, held)
        self.assertEqual(self.by_id(run)["aa_tell"].state, pipelines.PASSED)
        self.assertEqual(self.by_id(run)["bb_after"].state, pipelines.PASSED)

    def test_a_step_after_a_handler_still_runs_when_nothing_broke(self) -> None:
        """The other half of the one that shipped broken.

        A step that was never needed did not fail. Anything after it was being
        told it was skipped because that step "did not pass", one line under
        that step saying nothing went wrong - and the whole run came back as
        failed on a run where everything passed.
        """

        held = pipelines.read_it({
            "name": "A handler with a step after it",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "aa_tell", "kind": "git_repo", "label": "Tell somebody",
                 "settings": {"when": "when-something-failed"}, "at": {"x": 200, "y": 40}},
                {"id": "bb_after", "kind": "git_repo", "label": "And then this",
                 "settings": {}, "at": {"x": 400, "y": 40}},
                {"id": "zz_work", "kind": "git_repo", "label": "The work",
                 "settings": {}, "at": {"x": 200, "y": 200}},
            ],
            "edges": [
                {"from": "start", "to": "aa_tell"},
                {"from": "aa_tell", "to": "bb_after"},
                {"from": "start", "to": "zz_work"},
            ],
        })
        with self.stand_in({}):
            run = pipelines.run_it(self.config, held)
        after = self.by_id(run)["bb_after"]
        self.assertEqual(after.state, pipelines.PASSED, after.said)
        self.assertTrue(run.passed, run.said)

    def test_a_gate_after_a_handler_still_opens_when_nothing_broke(self) -> None:
        """A gate needing all of what came before must ignore what was not needed."""

        held = pipelines.read_it({
            "name": "A gate after a handler",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "aa_tell", "kind": "git_repo", "label": "Tell somebody",
                 "settings": {"when": "when-something-failed"}, "at": {"x": 200, "y": 40}},
                {"id": "bb_gate", "kind": "gate", "label": "Carry on?",
                 "settings": {"needs": "all"}, "at": {"x": 400, "y": 40}},
                {"id": "cc_after", "kind": "git_repo", "label": "And then this",
                 "settings": {}, "at": {"x": 600, "y": 40}},
                {"id": "zz_work", "kind": "git_repo", "label": "The work",
                 "settings": {}, "at": {"x": 200, "y": 200}},
            ],
            "edges": [
                {"from": "start", "to": "aa_tell"},
                {"from": "aa_tell", "to": "bb_gate"},
                {"from": "bb_gate", "to": "cc_after"},
                {"from": "start", "to": "zz_work"},
            ],
        })

        def one(config, node, before, results, order, check_kinds, depth=0,
                stopping=None, waiting_on=None):
            if node["kind"] == "start":
                return True, "Started", ""
            if node["kind"] in pipelines.GATES:
                return pipelines._decide_a_gate(node, before)
            return True, "done", ""

        with mock.patch.object(pipelines, "_do_one", one):
            run = pipelines.run_it(self.config, held)
        self.assertEqual(self.by_id(run)["bb_gate"].state, pipelines.PASSED,
                         self.by_id(run)["bb_gate"].said)
        self.assertEqual(self.by_id(run)["cc_after"].state, pipelines.PASSED)
        self.assertTrue(run.passed, run.said)

    def a_pipeline_with_a_handler(self) -> dict:
        return {
            "name": "With a handler",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "work", "kind": "git_repo", "label": "The work",
                 "settings": {}, "at": {"x": 200, "y": 100}},
                {"id": "gate", "kind": "gate", "label": "Carry on?",
                 "settings": {"needs": "all"}, "at": {"x": 400, "y": 100}},
                {"id": "tell", "kind": "git_repo", "label": "Tell somebody",
                 "settings": {"when": "when-something-failed"}, "at": {"x": 400, "y": 300}},
            ],
            "edges": [
                {"from": "start", "to": "work"},
                {"from": "work", "to": "gate"},
                {"from": "work", "to": "tell"},
            ],
        }

    def test_run_only_this_on_a_handler_really_runs_it(self) -> None:
        """The button says run this one. It has to run this one."""

        held = pipelines.read_it(self.a_pipeline_with_a_handler())
        with self.stand_in({}):
            run = pipelines.run_it(self.config, held, only="tell")
        told = self.by_id(run)["tell"]
        self.assertEqual(told.state, pipelines.PASSED, told.said)
        self.assertEqual(told.tries, 1)

    def test_carry_on_from_a_handler_really_runs_it(self) -> None:
        held = pipelines.read_it(self.a_pipeline_with_a_handler())
        with self.stand_in({}):
            run = pipelines.run_it(self.config, held, from_here="tell")
        self.assertEqual(self.by_id(run)["tell"].state, pipelines.PASSED)

    def test_a_gate_still_counts_the_steps_an_earlier_run_did(self) -> None:
        """"Left as it was" passed last time. A gate must not forget that."""

        held = pipelines.read_it(self.a_pipeline_with_a_handler())

        def one(config, node, before, results, order, check_kinds, depth=0,
                stopping=None, waiting_on=None):
            if node["kind"] == "start":
                return True, "Started", ""
            if node["kind"] in pipelines.GATES:
                return pipelines._decide_a_gate(node, before)
            return True, "done", ""

        with mock.patch.object(pipelines, "_do_one", one):
            run = pipelines.run_it(self.config, held, from_here="gate")
        gate = self.by_id(run)["gate"]
        self.assertEqual(gate.state, pipelines.PASSED, gate.said)
        # The work was left as it was, and still counted towards the gate.
        self.assertIn("1 of 1", gate.said)

    def test_every_choice_has_a_label_and_a_plain_meaning(self) -> None:
        for key, label, means in pipelines.WHEN_IT_RUNS:
            with self.subTest(when=key):
                self.assertTrue(label and not label.endswith("."))
                self.assertTrue(means.endswith("."), "the meaning is read as a sentence")
        self.assertIn("when-all-is-well", pipelines.WHEN_NAMES)

    def test_a_made_up_choice_is_refused_when_the_pipeline_is_read(self) -> None:
        held = self.a_pipeline(when="on-a-tuesday")
        with self.assertRaises(pipelines.PipelineError) as caught:
            pipelines.read_it(held)
        self.assertIn("when", str(caught.exception).lower())


class HowLongItWaits(PipelineTestCase):
    def test_no_wait_means_no_wait(self) -> None:
        self.assertEqual(pipelines._how_long_to_wait("no-wait", 1), 0.0)
        self.assertEqual(pipelines._how_long_to_wait("no-wait", 5), 0.0)

    def test_the_same_wait_stays_the_same(self) -> None:
        self.assertEqual(pipelines._how_long_to_wait("same-wait", 1), pipelines.FIRST_WAIT_SECONDS)
        self.assertEqual(pipelines._how_long_to_wait("same-wait", 9), pipelines.FIRST_WAIT_SECONDS)

    def test_a_growing_wait_grows_but_stops_growing(self) -> None:
        first = pipelines._how_long_to_wait("growing-wait", 1)
        second = pipelines._how_long_to_wait("growing-wait", 2)
        self.assertEqual(first, pipelines.FIRST_WAIT_SECONDS)
        self.assertEqual(second, first * 2)
        # Left to itself this doubles into hours. Nobody watching a pipeline
        # waits hours between two tries.
        self.assertEqual(
            pipelines._how_long_to_wait("growing-wait", 30),
            pipelines.LONGEST_WAIT_BETWEEN_TRIES,
        )

    def test_a_wait_nobody_set_is_no_wait(self) -> None:
        self.assertEqual(pipelines._how_long_to_wait("", 1), 0.0)
        self.assertEqual(pipelines._how_long_to_wait("something-else", 1), 0.0)

    def test_the_step_really_waits_between_tries(self) -> None:
        held = pipelines.read_it(self.a_pipeline(wait="same-wait", tries=3))
        waited: list[float] = []
        with mock.patch.object(pipelines, "_hold_on", lambda seconds, stopping: waited.append(seconds)):
            with self.stand_in({"work": (False, "busy", "")}):
                run = pipelines.run_it(self.config, held)
        # Three tries means two waits: after the first and after the second.
        self.assertEqual(waited, [pipelines.FIRST_WAIT_SECONDS] * 2)
        self.assertEqual(self.by_id(run)["work"].tries, 3)

    def test_it_does_not_wait_after_the_last_try(self) -> None:
        held = pipelines.read_it(self.a_pipeline(wait="growing-wait", tries=1))
        waited: list[float] = []
        with mock.patch.object(pipelines, "_hold_on", lambda seconds, stopping: waited.append(seconds)):
            with self.stand_in({"work": (False, "busy", "")}):
                pipelines.run_it(self.config, held)
        self.assertEqual(waited, [], "one try is one try, with nothing to wait for")

    def test_it_does_not_wait_once_the_step_has_passed(self) -> None:
        held = pipelines.read_it(self.a_pipeline(wait="same-wait", tries=3))
        waited: list[float] = []
        with mock.patch.object(pipelines, "_hold_on", lambda seconds, stopping: waited.append(seconds)):
            with self.stand_in({}):
                run = pipelines.run_it(self.config, held)
        self.assertEqual(waited, [])
        self.assertEqual(self.by_id(run)["work"].tries, 1)

    def test_stopping_the_run_cuts_the_wait_short(self) -> None:
        """A person who presses Stop must not sit through the rest of a wait."""

        began = time.monotonic()
        pipelines._hold_on(30.0, lambda: True)  # already stopping: comes straight back
        self.assertLess(time.monotonic() - began, 2.0)

    def test_a_wait_that_is_already_over_does_not_fall_over(self) -> None:
        """The clock can pass the end of the wait mid-line, and often does."""

        pipelines._hold_on(0.0, None)
        pipelines._hold_on(-1.0, None)

    def test_pressing_stop_during_a_wait_really_stops(self) -> None:
        """Stop must stop, not shorten the wait and then try once more."""

        held = pipelines.read_it(self.a_pipeline(wait="same-wait", tries=3))
        pressed = {"yet": False}
        ran = []

        def one(config, node, before, results, order, check_kinds, depth=0, stopping=None, waiting_on=None):
            if node["kind"] == "start":
                return True, "Started", ""
            ran.append(node["id"])
            return False, "busy", ""

        def hold(seconds, stopping):
            pressed["yet"] = True  # somebody presses Stop while it waits

        with mock.patch.object(pipelines, "_hold_on", hold):
            with mock.patch.object(pipelines, "_do_one", one):
                run = pipelines.run_it(
                    self.config, held, stopping=lambda: pressed["yet"]
                )
        # One try, one wait, and then nothing. Not two tries.
        self.assertEqual(ran, ["work"])
        self.assertIn("stopped", self.by_id(run)["work"].said)

    def test_every_wait_has_a_label_and_a_plain_meaning(self) -> None:
        for key, label, means in pipelines.WAITS:
            with self.subTest(wait=key):
                self.assertTrue(label and not label.endswith("."))
                self.assertTrue(means.endswith("."))
        self.assertIn("no-wait", pipelines.WAIT_NAMES)


class KeepingWhatWasThere(PipelineTestCase):
    def a_saved_one(self, label: str) -> dict:
        return {
            "name": "Kept",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "work", "kind": "git_repo", "label": label, "at": {"x": 200, "y": 100}},
            ],
            "edges": [{"from": "start", "to": "work"}],
        }

    def test_nothing_is_kept_until_something_is_saved_over(self) -> None:
        pipelines.save(self.config, self.a_saved_one("First"))
        self.assertEqual(pipelines.older_ones(self.config, "Kept"), [])

    def test_saving_over_keeps_the_one_that_was_there(self) -> None:
        pipelines.save(self.config, self.a_saved_one("First"))
        pipelines.save(self.config, self.a_saved_one("Second"))
        kept = pipelines.older_ones(self.config, "Kept")
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["pipeline"]["nodes"][1]["label"], "First")
        self.assertTrue(kept[0]["saved_at"])
        # A list of versions nobody can read is a list nobody uses.
        self.assertIn("changed", kept[0]["what_changed"])

    def test_the_newest_kept_one_is_first(self) -> None:
        for label in ("First", "Second", "Third"):
            pipelines.save(self.config, self.a_saved_one(label))
        kept = pipelines.older_ones(self.config, "Kept")
        self.assertEqual(
            [one["pipeline"]["nodes"][1]["label"] for one in kept], ["Second", "First"]
        )

    def test_saving_the_very_same_thing_keeps_nothing(self) -> None:
        pipelines.save(self.config, self.a_saved_one("Same"))
        pipelines.save(self.config, self.a_saved_one("Same"))
        self.assertEqual(pipelines.older_ones(self.config, "Kept"), [])

    def test_only_so_many_are_kept(self) -> None:
        for number in range(pipelines.HOW_MANY_KEPT + 5):
            pipelines.save(self.config, self.a_saved_one(f"Step {number}"))
        kept = pipelines.older_ones(self.config, "Kept")
        self.assertEqual(len(kept), pipelines.HOW_MANY_KEPT)
        # The ones kept are the recent ones, not the first ones.
        self.assertEqual(kept[0]["pipeline"]["nodes"][1]["label"], f"Step {pipelines.HOW_MANY_KEPT + 3}")

    def test_an_older_one_can_be_put_back(self) -> None:
        pipelines.save(self.config, self.a_saved_one("First"))
        pipelines.save(self.config, self.a_saved_one("Second"))
        going_back = pipelines.older_ones(self.config, "Kept")[0]["pipeline"]
        pipelines.save(self.config, going_back)
        now = pipelines.load(self.config, "Kept")
        self.assertEqual(now["nodes"][1]["label"], "First")
        # And putting one back is itself a change, so the one it replaced is
        # kept too. Undo must be undoable.
        kept = pipelines.older_ones(self.config, "Kept")
        self.assertEqual(kept[0]["pipeline"]["nodes"][1]["label"], "Second")

    def test_removing_a_pipeline_takes_its_old_versions_with_it(self) -> None:
        pipelines.save(self.config, self.a_saved_one("First"))
        pipelines.save(self.config, self.a_saved_one("Second"))
        self.assertTrue(pipelines.older_ones(self.config, "Kept"))
        pipelines.remove(self.config, "Kept")
        self.assertEqual(pipelines.older_ones(self.config, "Kept"), [])

    def test_an_unreadable_pile_is_the_same_as_no_pile(self) -> None:
        """A record of what came before is worth having and not worth failing over."""

        pipelines.save(self.config, self.a_saved_one("First"))
        pipelines.save(self.config, self.a_saved_one("Second"))
        where = pipelines._where_the_old_ones_live(self.config, "Kept")
        where.write_text("this is not json at all", encoding="utf-8")
        self.assertEqual(pipelines.older_ones(self.config, "Kept"), [])
        # And saving still works, rather than falling over on the broken file.
        pipelines.save(self.config, self.a_saved_one("Third"))
        self.assertEqual(pipelines.load(self.config, "Kept")["nodes"][1]["label"], "Third")

    def test_what_changed_is_readable(self) -> None:
        was = pipelines.read_it(self.a_saved_one("First"))
        now = pipelines.read_it({
            "name": "Kept",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "work", "kind": "git_repo", "label": "First", "at": {"x": 200, "y": 100}},
                {"id": "extra", "kind": "git_repo", "label": "A new one", "at": {"x": 400, "y": 100}},
            ],
            "edges": [{"from": "start", "to": "work"}, {"from": "work", "to": "extra"}],
        })
        said = pipelines._what_changed(was, now)
        self.assertIn("added", said)
        self.assertIn("A new one", said)

    def test_two_pipelines_keep_their_own_versions(self) -> None:
        first = self.a_saved_one("First")
        other = dict(self.a_saved_one("Other"), name="Another one")
        pipelines.save(self.config, first)
        pipelines.save(self.config, other)
        pipelines.save(self.config, dict(first, nodes=[
            first["nodes"][0], dict(first["nodes"][1], label="Changed")
        ]))
        self.assertEqual(len(pipelines.older_ones(self.config, "Kept")), 1)
        self.assertEqual(pipelines.older_ones(self.config, "Another one"), [])

    def test_a_reader_never_sees_half_a_pipeline(self) -> None:
        """Writing over a file empties it first, and a panel reads that empty file."""

        pipelines.save(self.config, self.a_saved_one("First"))
        where = pipelines.folder(self.config) / "kept.json"
        seen = []
        stop = threading.Event()

        def keep_reading():
            while not stop.is_set():
                try:
                    seen.append(pipelines.load(self.config, "Kept")["nodes"][1]["label"])
                except pipelines.PipelineError as exc:
                    seen.append(f"broken: {exc}")

        reader = threading.Thread(target=keep_reading, daemon=True)
        reader.start()
        try:
            for number in range(40):
                pipelines.save(self.config, self.a_saved_one(f"Step {number}"))
        finally:
            stop.set()
            reader.join(timeout=5)
        self.assertTrue(seen, "the reader never got a look in")
        broken = [one for one in seen if one.startswith("broken")]
        self.assertEqual(broken, [], f"a reader saw a half-written file: {broken[:3]}")
        self.assertTrue(where.is_file())

    def test_removing_one_waits_for_a_reader_to_let_go(self) -> None:
        """Windows will not delete a file anything has open, even to read it.

        A panel refreshing is that anything, and it lets go in a moment. Before
        this, pressing Remove in that moment answered with a page of machine
        detail and a 500, for a delete that would have worked a tenth of a
        second later.
        """

        pipelines.save(self.config, self.a_saved_one("First"))
        where = pipelines.folder(self.config) / "kept.json"
        held_open = where.open(encoding="utf-8")
        let_go = threading.Timer(0.25, held_open.close)
        let_go.start()
        self.addCleanup(let_go.cancel)
        try:
            said = pipelines.remove(self.config, "Kept")
        finally:
            held_open.close()
        self.assertIn("removed", said)
        self.assertFalse(where.is_file())

    def test_a_file_nothing_ever_lets_go_of_is_said_plainly(self) -> None:
        pipelines.save(self.config, self.a_saved_one("First"))
        with mock.patch.object(
            pipelines.Path, "unlink", side_effect=PermissionError("in use")
        ):
            with self.assertRaises(Exception) as caught:
                pipelines.remove(self.config, "Kept")
        said = str(caught.exception)
        self.assertIn("held open", said)
        self.assertNotIn("Traceback", said)

    def test_the_pile_is_written_all_at_once(self) -> None:
        """Half a file of old versions is worse than none, so it lands whole."""

        pipelines.save(self.config, self.a_saved_one("First"))
        pipelines.save(self.config, self.a_saved_one("Second"))
        where = pipelines._where_the_old_ones_live(self.config, "Kept")
        json.loads(where.read_text(encoding="utf-8"))
        left_behind = list(where.parent.glob("*.part"))
        self.assertEqual(left_behind, [], "no half-written file is left beside it")


if __name__ == "__main__":
    unittest.main()
