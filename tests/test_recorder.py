"""Doing a workflow by hand once and getting a written check out of it."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from our_harness import qa, recorder
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError

# Exactly what a real Chromium browser wrote down when somebody filled in a
# small form: a name, a password, a choice, a tick box, a button and Enter.
REAL_ACTIONS = [
    {"do": "type", "target": "#who", "text": "Ada Archer", "note": "Type in who"},
    {"do": "choose", "target": "#size", "value": "l", "note": "Choose l"},
    {"do": "type", "target": "#pass", "text": "${env.PASSWORD}", "note": "Type the password from your saved settings"},
    {"do": "click", "target": "#agree", "note": "Tick input"},
    {"do": "click", "target": '[data-testid="buy"]', "note": "Press Buy now"},
    {"do": "press", "target": "#who", "key": "Enter", "note": "Press Enter"},
]


class ReadingWhatWasDoneTests(unittest.TestCase):
    def test_a_real_recording_becomes_steps(self) -> None:
        taken = recorder.read_actions(REAL_ACTIONS, "http://127.0.0.1:8765/")
        self.assertEqual(len(taken.steps), 6)
        self.assertEqual(taken.steps[0]["do"], "type")
        self.assertEqual(taken.steps[4]["target"], '[data-testid="buy"]')

    def test_the_steps_are_ones_a_check_really_understands(self) -> None:
        taken = recorder.read_actions(REAL_ACTIONS, "http://127.0.0.1:8765/")
        suite = qa.parse_suite({"name": "d", "cases": [taken.case("done-by-hand")]})
        self.assertEqual(len(suite.cases[0].steps), 6)
        again = qa.parse_suite(json.loads(json.dumps(suite.to_dict())))
        self.assertEqual(again.to_dict(), suite.to_dict())

    def test_a_password_is_never_written_down(self) -> None:
        # The page sends a setting name, and nothing here may turn it back into
        # a value. This is the whole reason the recorder is safe to use on a
        # real sign-in page.
        taken = recorder.read_actions(REAL_ACTIONS, "http://127.0.0.1:8765/")
        body = json.dumps(taken.case())
        self.assertIn("${env.PASSWORD}", body)
        self.assertNotIn("hunter2", body)

    def test_a_thing_with_no_usable_name_is_left_out_and_said_so(self) -> None:
        taken = recorder.read_actions(
            [{"do": "click", "target": "", "note": "Press something"}, *REAL_ACTIONS],
            "http://127.0.0.1:8765/",
        )
        self.assertEqual(len(taken.steps), 6)
        self.assertIn("nothing on the page names", taken.skipped[0])
        self.assertIn("data-testid", taken.skipped[0])

    def test_nothing_recorded_says_what_to_do(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            recorder.read_actions([], "http://127.0.0.1:8765/")
        self.assertIn("Click something", str(caught.exception))

    def test_rubbish_from_the_page_is_refused(self) -> None:
        for value in (
            "not a list",
            [{"do": "explode", "target": "#a"}],
            ["just text"],
            [{"do": "click", "target": "#" + "a" * 900}],
        ):
            with self.subTest(value=str(value)[:40]), self.assertRaises(HarnessError):
                recorder.read_actions(value, "http://127.0.0.1:8765/")

    def test_a_very_long_recording_is_cut_and_said_so(self) -> None:
        many = [{"do": "click", "target": "#a", "note": "Press"} for _ in range(recorder.MAX_STEPS + 10)]
        taken = recorder.read_actions(many, "http://127.0.0.1:8765/")
        self.assertEqual(len(taken.steps), recorder.MAX_STEPS)
        self.assertIn("Split the workflow", taken.skipped[0])

    def test_the_check_it_writes_has_a_name_and_a_title(self) -> None:
        taken = recorder.read_actions(REAL_ACTIONS, "http://127.0.0.1:8765/")
        case = taken.case("signing-in", "A person can sign in")
        self.assertEqual(case["id"], "signing-in")
        self.assertEqual(case["title"], "A person can sign in")
        self.assertIn("recorded", case["tags"])


class PageScriptTests(unittest.TestCase):
    def test_the_script_carries_the_plan_and_the_test_attributes(self) -> None:
        script = recorder.recorder_script({"url": "http://127.0.0.1:1/", "viewport": {"width": 800, "height": 600}})
        self.assertIn("http://127.0.0.1:1/", script)
        self.assertIn("data-testid", script)
        self.assertNotIn("__PLAN__", script)
        self.assertNotIn("__NAMING__", script)
        self.assertNotIn("__TEST_ATTRIBUTES__", script)

    def test_the_bar_is_written_as_text_not_as_page_code(self) -> None:
        script = recorder.recorder_script({"url": "http://127.0.0.1:1/"})
        self.assertNotIn("innerHTML", script)
        self.assertIn("textContent", script)

    def test_a_password_box_is_recognised_and_never_copied(self) -> None:
        script = recorder.recorder_script({"url": "http://127.0.0.1:1/"})
        self.assertIn("'password'", script)
        self.assertIn("${env.PASSWORD}", script)

    def test_a_tick_box_is_only_written_down_once(self) -> None:
        script = recorder.recorder_script({"url": "http://127.0.0.1:1/"})
        self.assertIn("=== 'checkbox'", script)
        self.assertIn("in the check twice", script)


class RecordingRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.addCleanup(self.temporary.cleanup)

    def runner(self, stdout: str, stderr: str = ""):
        class Fake:
            def __init__(self) -> None:
                self.argv: list[str] = []

            def run(self, argv, cwd=".", timeout=None):
                self.argv = list(argv)

                class Result:
                    pass

                answer = Result()
                answer.stdout = stdout
                answer.stderr = stderr
                answer.passed = True
                return answer

        return Fake()

    def test_a_recording_runs_the_script_and_reads_the_answer(self) -> None:
        stdout = "<<<QA_REPORT>>>" + json.dumps({"fatal": "", "actions": REAL_ACTIONS})
        taken = recorder.record(self.config, "http://127.0.0.1:8765/", commands=self.runner(stdout))
        self.assertEqual(len(taken.steps), 6)

    def test_the_script_is_cleaned_up_afterwards(self) -> None:
        stdout = "<<<QA_REPORT>>>" + json.dumps({"fatal": "", "actions": REAL_ACTIONS})
        recorder.record(self.config, "http://127.0.0.1:8765/", commands=self.runner(stdout))
        self.assertEqual(list((self.root / ".harness" / "qa" / "tmp").glob("*")), [])

    def test_a_browser_that_never_starts_says_what_to_install(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            recorder.record(
                self.config, "http://127.0.0.1:8765/",
                commands=self.runner("", "Cannot find module 'playwright'"),
            )
        self.assertIn("npm install playwright", str(caught.exception))

    def test_a_page_that_broke_is_reported(self) -> None:
        stdout = "<<<QA_REPORT>>>" + json.dumps({"fatal": "the page never loaded", "actions": []})
        with self.assertRaises(HarnessError) as caught:
            recorder.record(self.config, "http://127.0.0.1:8765/", commands=self.runner(stdout))
        self.assertIn("never loaded", str(caught.exception))

    def test_a_page_outside_the_allowed_hosts_is_refused(self) -> None:
        with self.assertRaises(HarnessError):
            recorder.record(self.config, "http://example.com/", commands=self.runner(""))


if __name__ == "__main__":
    unittest.main()
