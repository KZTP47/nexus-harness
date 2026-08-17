"""What a failure means, and the pipelines somebody can start from.

The explaining is held to one rule above all: it must never sound sure about
something it has not recognised. A confident wrong answer sends a person
looking in the wrong place for an hour, which is worse than saying plainly
that this one is not known.
"""

from __future__ import annotations

import unittest

from our_harness import explain, pipeline_starters, pipelines
from our_harness.models import HarnessError


class WhatAFailureMeansTests(unittest.TestCase):
    def test_the_ones_people_hit_are_recognised(self) -> None:
        # Every one of these is a real message, copied from a real failure.
        cases = {
            "page.goto: net::ERR_CONNECTION_REFUSED at http://127.0.0.1:8000/":
                "Nothing was listening",
            "browserType.launch: Executable doesn't exist at ...chromium-1097":
                "browser this check needs",
            "'node' is not recognized as an internal or external command":
                "Node.js is not on this machine",
            "Timeout 10000ms exceeded while waiting for selector \"#sign-in\"":
                "waited for something",
            "The page text does not hold the text \"Welcome\"":
                "did not say what the check expected",
            "The command finished with exit code 1":
                "finished badly",
            "FileNotFoundError: No such file or directory: 'report.html'":
                "is not there",
            "Read 42 files, skipped 3. Found 2 to look at.":
                "looks like a credential",
            "provider configuration is missing: no provider":
                "no model it can use",
            "Found 3 console error messages, more than the 0 allowed. First one: Uncaught TypeError":
                "hit an error",
            "OSError: [WinError 10048] Only one usage of each socket address":
                "already using that port",
            "The page looks different: 4210 pixels differ from the baseline":
                "looks different",
        }
        for said, wanted in cases.items():
            with self.subTest(said=said[:40]):
                meaning = explain.what_it_means(said)
                self.assertTrue(meaning.sure, f"it did not recognise: {said}")
                self.assertIn(wanted, meaning.headline)
                self.assertTrue(meaning.try_this, "it recognised it and suggested nothing")

    def test_it_never_sounds_sure_about_one_it_does_not_know(self) -> None:
        meaning = explain.what_it_means("gremlins in the flux capacitor")
        self.assertFalse(meaning.sure)
        self.assertIn("not a failure the harness recognises", meaning.headline)
        self.assertTrue(meaning.try_this, "it should still say what is worth looking at")

    def test_it_says_something_useful_when_told_nothing(self) -> None:
        meaning = explain.what_it_means("")
        self.assertFalse(meaning.sure)
        self.assertTrue(meaning.headline)
        self.assertTrue(meaning.try_this)

    def test_it_does_not_hand_back_a_wall_of_stack_trace(self) -> None:
        said = (
            "Traceback (most recent call last):\n"
            '  File "thing.py", line 4, in <module>\n'
            "    raise ValueError('the parser gave up')\n"
            "ValueError: the parser gave up"
        )
        meaning = explain.what_it_means(said)
        self.assertNotIn("File \"", meaning.because)
        self.assertIn("the parser gave up", meaning.because)

    def test_what_it_says_is_about_the_kind_of_check(self) -> None:
        browser = explain.what_it_means("something nobody knows", kind="browser")
        command = explain.what_it_means("something nobody knows", kind="command")
        self.assertNotEqual(browser.try_this, command.try_this)
        self.assertIn("picture", " ".join(browser.try_this))

    def test_every_rule_says_what_to_try(self) -> None:
        for rule in explain.RULES:
            with self.subTest(rule=rule.name):
                self.assertTrue(rule.headline.endswith("."), "a headline is a sentence")
                self.assertTrue(rule.because)
                self.assertTrue(rule.try_this)

    def test_it_never_falls_over_on_anything_it_is_handed(self) -> None:
        for odd in ("", " ", "\x00", "a" * 50000, "no words at all 12345", "\n\n\n"):
            with self.subTest(odd=odd[:20]):
                meaning = explain.what_it_means(odd)
                self.assertTrue(meaning.headline)


class ReadyMadePipelinesTests(unittest.TestCase):
    def test_there_are_several_to_start_from(self) -> None:
        self.assertGreaterEqual(len(pipeline_starters.listed()), 4)

    def test_every_one_of_them_is_a_pipeline_the_harness_accepts(self) -> None:
        # The real gate: a ready-made one that the harness refuses would leave
        # somebody stuck on their first press, which is the worst moment.
        for starter in pipeline_starters.STARTERS:
            with self.subTest(starter=starter.key):
                tidy = pipelines.read_it(pipeline_starters.build(starter.key))
                self.assertTrue(tidy["nodes"])
                self.assertTrue(pipelines.in_running_order(tidy))

    def test_each_one_says_what_it_is_for(self) -> None:
        for starter in pipeline_starters.listed():
            with self.subTest(starter=starter["key"]):
                self.assertTrue(starter["title"])
                self.assertTrue(starter["when"])
                self.assertGreater(starter["steps"], 1)

    def test_they_only_use_steps_the_harness_really_has(self) -> None:
        for starter in pipeline_starters.STARTERS:
            for node in starter.draws["nodes"]:
                with self.subTest(starter=starter.key, node=node["id"]):
                    self.assertIn(node["kind"], pipelines.KINDS)

    def test_asking_for_one_that_is_not_there_says_so(self) -> None:
        with self.assertRaises(HarnessError):
            pipeline_starters.build("something-else")

    def test_one_handed_out_is_a_copy(self) -> None:
        # Two people pressing the same starter must not share one drawing.
        first = pipeline_starters.build("nightly")
        first["nodes"][0]["label"] = "changed"
        second = pipeline_starters.build("nightly")
        self.assertNotEqual(second["nodes"][0]["label"], "changed")

    def test_the_one_that_asks_a_model_writes_a_draft_and_not_a_test(self) -> None:
        # It must not put a model's writing where a test runner would find it.
        drawn = pipeline_starters.build("let-a-model-write-a-test")
        writing = [node for node in drawn["nodes"] if node["kind"] == "ai_unit_test"][0]
        self.assertNotIn("/", writing["settings"]["write_to"])
        self.assertNotIn("\\", writing["settings"]["write_to"])


if __name__ == "__main__":
    unittest.main()
