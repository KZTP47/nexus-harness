"""The ideas taken from Kestra, and the promises each one has to keep.

Four things somebody asks for the first afternoon they draw a pipeline:

  - Start again from the step that broke, not from the top.
  - Run one step on its own while building it.
  - Stop and ask me before the thing that matters.
  - Run my other pipeline here, rather than copying its steps.

Plus the two that make a pipeline worth saving twice: it can ask a couple of
questions when the run starts, and there are enough ready-made ones to find one
worth starting from.

The promise underneath all of them: **a run that did less than everything must
never look like a run that did everything.**
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

from our_harness import pipeline_starters, pipelines
from our_harness.config import DEFAULT_CONFIG, LoadedConfig


class PipelineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})

    def stand_in(self, answers: dict[str, tuple[bool, str, str]]):
        """Every kind stood in, so no real suite or model is started."""

        def one(config, node, before, results, order, check_kinds, depth=0):
            if node["kind"] == "start":
                return True, "Started", ""
            if node["kind"] in pipelines.GATES:
                return pipelines._decide_a_gate(node, before)
            if node["kind"] == "another_pipeline":
                return pipelines._run_another_pipeline(config, node, check_kinds, depth)
            return answers.get(node["id"], (True, "done", ""))

        return mock.patch.object(pipelines, "_do_one", one)

    def three_steps(self) -> dict:
        return {
            "name": "Three steps",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "middle", "kind": "git_repo", "label": "The middle one"},
                {"id": "last", "kind": "artifact", "label": "The last one"},
            ],
            "edges": [{"from": "start", "to": "middle"}, {"from": "middle", "to": "last"}],
        }


class CarryingOnFromAStepTests(PipelineTestCase):
    def test_it_runs_that_step_and_everything_after_it(self) -> None:
        with self.stand_in({}):
            run = pipelines.run_it(self.config, self.three_steps(), from_here="middle")
        by_id = {one.id: one for one in run.nodes}
        self.assertTrue(by_id["start"].skipped_this_time, "the first one should be left alone")
        self.assertFalse(by_id["middle"].skipped_this_time)
        self.assertFalse(by_id["last"].skipped_this_time, "what waits on it runs too")

    def test_a_step_left_alone_says_so_rather_than_claiming_it_passed(self) -> None:
        # The whole danger of running part of something: the report reading as
        # if it covered all of it.
        with self.stand_in({}):
            run = pipelines.run_it(self.config, self.three_steps(), from_here="middle")
        start = next(one for one in run.nodes if one.id == "start")
        self.assertIn("earlier run", start.said)
        self.assertTrue(start.skipped_this_time)

    def test_asking_for_a_step_that_is_not_there_is_refused(self) -> None:
        with self.assertRaises(pipelines.PipelineError) as caught:
            pipelines.run_it(self.config, self.three_steps(), from_here="nope")
        self.assertIn("no step called nope", str(caught.exception))


class RunningOneStepTests(PipelineTestCase):
    def test_only_that_step_runs(self) -> None:
        with self.stand_in({}):
            run = pipelines.run_it(self.config, self.three_steps(), only="middle")
        ran = [one.id for one in run.nodes if not one.skipped_this_time]
        self.assertEqual(ran, ["middle"])

    def test_a_step_that_fails_on_its_own_still_says_so(self) -> None:
        with self.stand_in({"middle": (False, "it did not work", "")}):
            run = pipelines.run_it(self.config, self.three_steps(), only="middle")
        by_id = {one.id: one for one in run.nodes}
        self.assertEqual(by_id["middle"].state, pipelines.FAILED)
        self.assertFalse(run.passed)

    def test_asking_for_a_step_that_is_not_there_is_refused(self) -> None:
        with self.assertRaises(pipelines.PipelineError):
            pipelines.run_it(self.config, self.three_steps(), only="nope")


class WaitingForAPersonTests(PipelineTestCase):
    def waiting_pipeline(self) -> dict:
        return {
            "name": "Asking",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "ask", "kind": "wait_for_a_person", "label": "Say yes",
                 "settings": {"question": "Shall this go out?"}},
                {"id": "after", "kind": "artifact", "label": "Keep the evidence"},
            ],
            "edges": [{"from": "start", "to": "ask"}, {"from": "ask", "to": "after"}],
        }

    def test_saying_carry_on_lets_the_rest_run(self) -> None:
        answers: dict[str, bool] = {}

        def answer_soon() -> None:
            time.sleep(0.3)
            answers["ask"] = True

        threading.Thread(target=answer_soon).start()
        with self.stand_in({}):
            run = pipelines.run_it(
                self.config, self.waiting_pipeline(), waiting_on=answers.get
            )
        by_id = {one.id: one for one in run.nodes}
        self.assertEqual(by_id["ask"].state, pipelines.PASSED)
        self.assertEqual(by_id["after"].state, pipelines.PASSED)
        self.assertTrue(run.passed)

    def test_saying_no_stops_everything_after_it(self) -> None:
        answers = {"ask": False}
        with self.stand_in({}):
            run = pipelines.run_it(
                self.config, self.waiting_pipeline(), waiting_on=answers.get
            )
        by_id = {one.id: one for one in run.nodes}
        self.assertEqual(by_id["ask"].state, pipelines.FAILED)
        self.assertEqual(by_id["after"].state, pipelines.SKIPPED)
        self.assertFalse(run.passed)

    def test_nobody_listening_at_all_does_not_hang(self) -> None:
        # A run from the command line, or a test. Waiting for an answer nobody
        # can give would hold the run until the machine was turned off.
        started = time.monotonic()
        with self.stand_in({}):
            run = pipelines.run_it(self.config, self.waiting_pipeline())
        self.assertLess(time.monotonic() - started, 5.0)
        by_id = {one.id: one for one in run.nodes}
        self.assertEqual(by_id["ask"].state, pipelines.SKIPPED)
        self.assertIn("Nobody answered", by_id["ask"].said)

    def test_stopping_the_run_stops_the_waiting(self) -> None:
        stop = {"now": False}

        def stop_soon() -> None:
            time.sleep(0.3)
            stop["now"] = True

        threading.Thread(target=stop_soon).start()
        started = time.monotonic()
        with self.stand_in({}):
            run = pipelines.run_it(
                self.config, self.waiting_pipeline(),
                waiting_on=lambda step: None,
                stopping=lambda: stop["now"],
            )
        self.assertLess(time.monotonic() - started, 5.0, "Stop did not stop the waiting")
        self.assertFalse(run.passed)


class RunningAnotherPipelineTests(PipelineTestCase):
    def setUp(self) -> None:
        super().setUp()
        pipelines.save(self.config, {
            "name": "The inner one",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "work", "kind": "git_repo", "label": "Read the repo"},
            ],
            "edges": [{"from": "start", "to": "work"}],
        })

    def calling(self, name: str) -> dict:
        return {
            "name": "The outer one",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "inner", "kind": "another_pipeline", "label": "Run the inner one",
                 "settings": {"pipeline": name}},
            ],
            "edges": [{"from": "start", "to": "inner"}],
        }

    def test_it_really_runs_the_other_one(self) -> None:
        with self.stand_in({}):
            run = pipelines.run_it(self.config, self.calling("The inner one"))
        inner = next(one for one in run.nodes if one.id == "inner")
        self.assertEqual(inner.state, pipelines.PASSED)
        self.assertIn("The inner one", inner.said)
        self.assertIn("Read the repo", inner.detail)

    def test_a_name_nobody_saved_is_said_plainly(self) -> None:
        with self.stand_in({}):
            run = pipelines.run_it(self.config, self.calling("Never saved"))
        inner = next(one for one in run.nodes if one.id == "inner")
        self.assertEqual(inner.state, pipelines.FAILED)
        self.assertIn("no pipeline called Never saved", inner.said)

    def test_a_pipeline_that_calls_itself_stops_rather_than_going_round(self) -> None:
        pipelines.save(self.config, {
            "name": "Round and round",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "again", "kind": "another_pipeline", "label": "Itself",
                 "settings": {"pipeline": "Round and round"}},
            ],
            "edges": [{"from": "start", "to": "again"}],
        })
        started = time.monotonic()
        with self.stand_in({}):
            run = pipelines.run_it(self.config, pipelines.load(self.config, "Round and round"))
        self.assertLess(time.monotonic() - started, 30.0, "it went round and round")
        self.assertFalse(run.passed)
        said = " ".join(one.said for one in run.nodes)
        self.assertIn("deep", said)


class AskingBeforeARunTests(PipelineTestCase):
    def asking_pipeline(self) -> dict:
        return {
            "name": "Asks first",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "checks", "kind": "suite", "label": "The checks",
                 "settings": {"tag": "fast", "asks": ["tag"]}},
            ],
            "edges": [{"from": "start", "to": "checks"}],
        }

    def test_an_answer_is_used_for_that_run(self) -> None:
        seen: dict[str, str] = {}

        def one(config, node, before, results, order, check_kinds, depth=0):
            if node["kind"] == "suite":
                seen["tag"] = node["settings"].get("tag", "")
            return True, "done", ""

        with mock.patch.object(pipelines, "_do_one", one):
            pipelines.run_it(
                self.config, self.asking_pipeline(), answers={"checks.tag": "slow"}
            )
        self.assertEqual(seen["tag"], "slow")

    def test_with_no_answer_what_was_saved_is_used(self) -> None:
        seen: dict[str, str] = {}

        def one(config, node, before, results, order, check_kinds, depth=0):
            if node["kind"] == "suite":
                seen["tag"] = node["settings"].get("tag", "")
            return True, "done", ""

        with mock.patch.object(pipelines, "_do_one", one):
            pipelines.run_it(self.config, self.asking_pipeline())
        self.assertEqual(seen["tag"], "fast")

    def test_answering_does_not_change_the_saved_pipeline(self) -> None:
        pipelines.save(self.config, self.asking_pipeline())
        with self.stand_in({}):
            pipelines.run_it(
                self.config, pipelines.load(self.config, "Asks first"),
                answers={"checks.tag": "slow"},
            )
        kept = pipelines.load(self.config, "Asks first")
        checks = next(one for one in kept["nodes"] if one["id"] == "checks")
        self.assertEqual(checks["settings"]["tag"], "fast", "the saved one was changed")

    def test_a_step_can_only_ask_about_its_own_settings(self) -> None:
        with self.assertRaises(pipelines.PipelineError) as caught:
            pipelines.read_it({
                "name": "Odd",
                "nodes": [{"id": "a", "kind": "start", "label": "Start",
                           "settings": {"asks": ["something-else"]}}],
                "edges": [],
            })
        self.assertIn("not one of its settings", str(caught.exception))


class TheGalleryTests(unittest.TestCase):
    def test_there_are_enough_to_be_worth_searching(self) -> None:
        listed = pipeline_starters.listed()
        self.assertGreaterEqual(len(listed), 10)

    def test_every_one_says_what_it_is_for_and_how_to_find_it(self) -> None:
        for one in pipeline_starters.listed():
            with self.subTest(one=one["key"]):
                self.assertTrue(one["title"])
                self.assertGreater(len(one["when"]), 20, "it does not say when to reach for it")
                self.assertTrue(one["group"])
                self.assertTrue(one["found_by"], "no words would find it")

    def test_every_one_really_runs(self) -> None:
        # A gallery of things that do not work is worse than an empty page, so
        # every one of them is really run - with each kind stood in, so no real
        # suite or model is started, but through the same runner a person uses.
        def one_step(config, node, before, results, order, check_kinds, depth=0):
            if node["kind"] == "start":
                return True, "Started", ""
            if node["kind"] in pipelines.GATES:
                return pipelines._decide_a_gate(node, before)
            return True, "done", ""

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            (root / ".harness").mkdir()
            config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), root, [], {})
            # The one that runs other pipelines needs those to exist first.
            for name in ("Before a commit", "Before a release"):
                pipelines.save(config, {
                    "name": name,
                    "nodes": [{"id": "start", "kind": "start", "label": "Start"}],
                    "edges": [],
                })
            for one in pipeline_starters.listed():
                with self.subTest(one=one["key"]):
                    drawn = pipeline_starters.build(one["key"])
                    with mock.patch.object(pipelines, "_do_one", one_step):
                        run = pipelines.run_it(config, drawn)
                    ran = [step for step in run.nodes if not step.skipped_this_time]
                    self.assertTrue(ran, "nothing in it ran at all")
                    for step in ran:
                        self.assertIn(
                            step.state,
                            (pipelines.PASSED, pipelines.SKIPPED),
                            f"{one['key']}: {step.label} came back as {step.state}: {step.said}",
                        )

    def test_the_words_that_find_one_really_find_it(self) -> None:
        by_word = {
            "release": "ask-before-a-release",
            "security": "just-the-security",
            "subflow": "the-long-one",
            "git": "what-changed-here",
        }
        for word, key in by_word.items():
            with self.subTest(word=word):
                found = [
                    one["key"] for one in pipeline_starters.listed()
                    if word in " ".join([one["title"], one["when"], *one["found_by"]]).lower()
                ]
                self.assertIn(key, found)


class TheTimelineHasWhatItNeedsTests(PipelineTestCase):
    def test_every_step_says_when_it_started_and_how_long_it_took(self) -> None:
        # The timeline is drawn from these two numbers. Without both, bars have
        # a length but no place to sit.
        with self.stand_in({}):
            run = pipelines.run_it(self.config, self.three_steps())
        for one in run.nodes:
            with self.subTest(step=one.id):
                self.assertGreaterEqual(one.started_after, 0)
                self.assertGreaterEqual(one.milliseconds, 0)
                self.assertIn("started_after", one.to_dict())
                self.assertIn("skipped_this_time", one.to_dict())

    def test_they_start_in_the_order_they_ran(self) -> None:
        with self.stand_in({}):
            run = pipelines.run_it(self.config, self.three_steps())
        when = [one.started_after for one in run.nodes]
        self.assertEqual(when, sorted(when))


if __name__ == "__main__":
    unittest.main()
