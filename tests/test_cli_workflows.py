"""Every command a person can type, tried for real.

The panel is covered by browser checks. This is the other half: each command
line the harness offers must start, answer, and end with the right code. A
command that crashes with a traceback, or one that says it worked when it did
not, fails here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from our_harness.cli import parser

SOURCE = str(Path(__file__).resolve().parents[1] / "src")


def run(arguments: list[str], project: Path | None = None, timeout: int = 180):
    environment = {**os.environ, "PYTHONPATH": SOURCE, "NO_COLOR": "1"}
    argv = [sys.executable, "-m", "our_harness"]
    if project is not None:
        argv += ["--project", str(project)]
    return subprocess.run(
        argv + arguments, capture_output=True, text=True, timeout=timeout, env=environment
    )


def command_names() -> list[list[str]]:
    """Every command and sub-command the parser offers."""

    found: list[list[str]] = []

    def walk(part, prefix: list[str]) -> None:
        for action in part._subparsers._group_actions if part._subparsers else []:
            for name, sub in action.choices.items():
                found.append(prefix + [name])
                if sub._subparsers:
                    walk(sub, prefix + [name])

    walk(parser(), [])
    return found


class HelpTests(unittest.TestCase):
    """No command may fall over when a person asks what it does."""

    def test_every_command_explains_itself(self) -> None:
        names = command_names()
        self.assertGreater(len(names), 30, "the parser should offer many commands")
        for name in names:
            with self.subTest(command=" ".join(name)):
                finished = run(name + ["--help"], timeout=60)
                self.assertEqual(finished.returncode, 0, finished.stderr)
                self.assertIn("usage:", finished.stdout)
                self.assertNotIn("Traceback", finished.stderr)

    def test_the_top_level_help_lists_the_main_commands(self) -> None:
        finished = run(["--help"], timeout=60)
        self.assertEqual(finished.returncode, 0)
        for name in ("qa", "bundle", "doctor", "ui", "run", "audit"):
            self.assertIn(name, finished.stdout)


class RealProjectTests(unittest.TestCase):
    """Read-only commands against a project made just for this test."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name).resolve()
        (cls.root / "README.md").write_text("# A project\n", encoding="utf-8")
        (cls.root / "app.py").write_text("print('hello')\n", encoding="utf-8")
        started = run(["init", "--yes", "--provider", "ollama"], cls.root, timeout=240)
        assert started.returncode == 0, started.stdout + started.stderr

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_a_new_project_is_set_up(self) -> None:
        self.assertTrue((self.root / ".harness" / "config.json").is_file())

    def test_the_starter_checks_can_be_written_and_listed(self) -> None:
        written = run(["qa", "init", "--force"], self.root)
        self.assertEqual(written.returncode, 0, written.stderr)
        listed = run(["qa", "list"], self.root)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("holds", listed.stdout)

    def test_writing_starter_checks_twice_is_refused_with_a_sentence(self) -> None:
        run(["qa", "init", "--force"], self.root)
        again = run(["qa", "init"], self.root)
        self.assertEqual(again.returncode, 2)
        self.assertIn("already exists", again.stderr)
        self.assertNotIn("Traceback", again.stderr)

    def test_checks_run_and_report(self) -> None:
        run(["qa", "init", "--force"], self.root)
        finished = run(["qa", "run"], self.root, timeout=300)
        self.assertIn(finished.returncode, (0, 1), finished.stderr)
        self.assertIn("Test run", finished.stdout)

    def test_the_settings_can_be_kept_and_removed(self) -> None:
        saved = run(["qa", "env", "set", "staging", "BASE=http://127.0.0.1:9"], self.root)
        self.assertEqual(saved.returncode, 0, saved.stderr)
        listed = run(["qa", "env", "list"], self.root)
        self.assertIn("staging", listed.stdout)
        removed = run(["qa", "env", "delete", "staging"], self.root)
        self.assertEqual(removed.returncode, 0)
        gone = run(["qa", "env", "delete", "staging"], self.root)
        self.assertEqual(gone.returncode, 2)
        self.assertIn("no settings named", gone.stderr.lower())

    def test_the_advice_and_flaky_commands_answer(self) -> None:
        for name in (["qa", "advise"], ["qa", "flaky"], ["qa", "candidates"]):
            with self.subTest(command=name):
                finished = run(name, self.root)
                self.assertIn(finished.returncode, (0, 1), finished.stderr)
                self.assertNotIn("Traceback", finished.stderr)

    def test_a_bundle_is_written_and_can_be_read_back(self) -> None:
        made = run(["bundle", "--part", "machine"], self.root)
        self.assertEqual(made.returncode, 0, made.stderr)
        self.assertIn("Wrote", made.stdout)
        name = made.stdout.splitlines()[0].split("Wrote ", 1)[1].strip()
        read = run(["bundle", "--read", name], self.root)
        self.assertEqual(read.returncode, 0, read.stderr)
        self.assertIn("machine", read.stdout)

    def test_asking_for_a_bundle_and_a_read_at_once_is_refused(self) -> None:
        finished = run(["bundle", "--read", "any.zip", "--part", "checks"], self.root)
        self.assertEqual(finished.returncode, 2)
        self.assertIn("one or the other", finished.stderr)

    def test_the_doctor_answers(self) -> None:
        finished = run(["doctor"], self.root, timeout=240)
        self.assertIn(finished.returncode, (0, 1), finished.stderr)
        self.assertNotIn("Traceback", finished.stderr)

    def test_reading_the_project_answers(self) -> None:
        for name in (["index"], ["brief"], ["memory", "search", "hello"]):
            with self.subTest(command=name):
                finished = run(name, self.root, timeout=240)
                self.assertIn(finished.returncode, (0, 1), finished.stderr)
                self.assertNotIn("Traceback", finished.stderr)

    def test_a_missing_file_is_a_sentence_not_a_crash(self) -> None:
        for arguments in (
            ["qa", "run", "--suite", "nothing/here.json"],
            ["qa", "list", "--suite", "nothing/here.json"],
            ["bundle", "--read", "nothing/here.zip"],
            ["graph", "validate", "nothing/here.json"],
        ):
            with self.subTest(command=arguments):
                finished = run(arguments, self.root)
                self.assertEqual(finished.returncode, 2, finished.stdout + finished.stderr)
                self.assertNotIn("Traceback", finished.stderr)
                self.assertTrue(finished.stderr.strip().startswith("error:"), finished.stderr)

    def test_a_path_that_leaves_the_project_is_refused(self) -> None:
        for arguments in (
            ["qa", "run", "--suite", "../outside.json"],
            ["bundle", "--output", "../outside.zip"],
        ):
            with self.subTest(command=arguments):
                finished = run(arguments, self.root)
                self.assertEqual(finished.returncode, 2, finished.stdout)
                self.assertNotIn("Traceback", finished.stderr)

    def test_odd_numbers_are_refused_with_a_sentence(self) -> None:
        for arguments in (
            ["bundle", "--runs", "-5"],
            ["bundle", "--runs", "10000"],
            ["qa", "run", "--workers", "-3"],
        ):
            with self.subTest(command=arguments):
                finished = run(arguments, self.root, timeout=300)
                self.assertNotIn("Traceback", finished.stderr)

    def test_the_picker_refuses_a_page_it_may_not_open(self) -> None:
        finished = run(["qa", "pick", "--url", "http://example.com/"], self.root)
        self.assertEqual(finished.returncode, 2)
        self.assertIn("may not open", finished.stderr)

    def test_the_baseline_command_says_when_there_is_nothing_to_photograph(self) -> None:
        run(["qa", "init", "--force"], self.root)
        finished = run(["qa", "baseline"], self.root, timeout=300)
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertIn("no screenshot checks", finished.stdout)

    def test_a_workflow_file_can_be_checked(self) -> None:
        graph = json.loads(
            (Path(SOURCE) / "our_harness" / "templates" / "gauntlet.json").read_text(encoding="utf-8")
        )
        path = self.root / "flow.json"
        path.write_text(json.dumps(graph), encoding="utf-8")
        finished = run(["graph", "validate", "flow.json"], self.root)
        self.assertIn(finished.returncode, (0, 1), finished.stderr)
        self.assertNotIn("Traceback", finished.stderr)


if __name__ == "__main__":
    unittest.main()


class OtherCommandTests(unittest.TestCase):
    """The rest of the commands, run for real against a throwaway project.

    A command that cannot do its job here must still answer with a sentence and
    the right code, never a traceback. That is what these check.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name).resolve()
        (cls.root / "README.md").write_text("# A project\n", encoding="utf-8")
        started = run(["init", "--yes", "--provider", "ollama"], cls.root, timeout=240)
        assert started.returncode == 0, started.stdout + started.stderr

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_the_lists_that_should_be_empty_say_so(self) -> None:
        for arguments in (
            ["runs", "list"],
            ["recovery", "list"],
            ["checkpoint", "list"],
            ["refine", "list"],
            ["refine", "candidates"],
            ["config"],
            ["qa", "candidates"],
        ):
            with self.subTest(command=arguments):
                finished = run(arguments, self.root, timeout=120)
                self.assertIn(finished.returncode, (0, 1), finished.stderr)
                self.assertNotIn("Traceback", finished.stderr)

    def test_a_daemon_that_is_not_running_says_so(self) -> None:
        for arguments in (["daemon", "status"], ["jobs", "list"]):
            with self.subTest(command=arguments):
                finished = run(arguments, self.root, timeout=120)
                self.assertEqual(finished.returncode, 2, finished.stdout)
                self.assertTrue(finished.stderr.strip().startswith("error:"), finished.stderr)
                self.assertNotIn("Traceback", finished.stderr)

    def test_a_server_that_is_not_configured_says_so(self) -> None:
        finished = run(["mcp", "list", "nothing-here"], self.root, timeout=120)
        self.assertEqual(finished.returncode, 2)
        self.assertNotIn("Traceback", finished.stderr)

    def test_trusting_the_local_settings_works_and_says_so(self) -> None:
        shown = run(["trust", "--show"], self.root, timeout=120)
        self.assertIn(shown.returncode, (0, 1), shown.stderr)
        self.assertNotIn("Traceback", shown.stderr)
        agreed = run(["trust", "--yes"], self.root, timeout=120)
        self.assertEqual(agreed.returncode, 0, agreed.stderr)
        self.assertIn("Trusted", agreed.stdout)

    def test_a_command_that_asks_a_question_copes_with_nobody_there(self) -> None:
        # Run by a script, with the keyboard closed, it must answer and stop
        # rather than wait for ever or fall over.
        finished = subprocess.run(
            [sys.executable, "-m", "our_harness", "--project", str(self.root), "trust"],
            capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
            env={**os.environ, "PYTHONPATH": SOURCE},
        )
        self.assertIn(finished.returncode, (0, 1), finished.stderr)
        self.assertNotIn("Traceback", finished.stderr)
        self.assertIn("Left as it was", finished.stdout)

    def test_a_workflow_can_be_simulated(self) -> None:
        graph = json.loads(
            (Path(SOURCE) / "our_harness" / "templates" / "gauntlet.json").read_text(encoding="utf-8")
        )
        (self.root / "flow.json").write_text(json.dumps(graph), encoding="utf-8")
        finished = run(["graph", "simulate", "flow.json"], self.root, timeout=180)
        self.assertIn(finished.returncode, (0, 1), finished.stderr)
        self.assertNotIn("Traceback", finished.stderr)

    def test_asking_a_question_answers_or_says_why_not(self) -> None:
        finished = run(["ask", "what does this project do"], self.root, timeout=240)
        self.assertIn(finished.returncode, (0, 1, 2), finished.stderr)
        self.assertNotIn("Traceback", finished.stderr)

    def test_proposing_checks_with_no_model_says_what_to_do(self) -> None:
        finished = run(["qa", "generate"], self.root, timeout=240)
        self.assertEqual(finished.returncode, 2, finished.stdout)
        self.assertNotIn("Traceback", finished.stderr)

    def test_accepting_a_check_that_was_never_proposed_says_so(self) -> None:
        for name in (["qa", "accept", "nothing"], ["qa", "reject", "nothing"]):
            with self.subTest(command=name):
                finished = run(name, self.root, timeout=120)
                self.assertEqual(finished.returncode, 2)
                self.assertNotIn("Traceback", finished.stderr)

    def test_watching_runs_the_checks_and_stops_when_told(self) -> None:
        run(["qa", "init", "--force"], self.root, timeout=180)
        finished = run(
            ["qa", "watch", "--max-runs", "1", "--every", "0.2", "--interval", "0.1", "--no-artifacts"],
            self.root, timeout=300,
        )
        self.assertIn(finished.returncode, (0, 1), finished.stderr)
        self.assertIn("Stopped after", finished.stdout)
        self.assertNotIn("Traceback", finished.stderr)

    def test_the_ready_made_checks_can_be_listed_and_added(self) -> None:
        listed = run(["qa", "starters"], self.root, timeout=120)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("page-opens", listed.stdout)
        run(["qa", "init", "--force"], self.root, timeout=180)
        added = run(
            ["qa", "add", "no-keys-in-the-code", "--name", "my-secret-check"], self.root, timeout=120
        )
        self.assertEqual(added.returncode, 0, added.stderr)
        listed_again = run(["qa", "list"], self.root, timeout=120)
        self.assertIn("my-secret-check", listed_again.stdout)

    def test_adding_the_same_check_twice_is_refused(self) -> None:
        run(["qa", "init", "--force"], self.root, timeout=180)
        run(["qa", "add", "page-opens", "--name", "twice"], self.root, timeout=120)
        again = run(["qa", "add", "page-opens", "--name", "twice"], self.root, timeout=120)
        self.assertEqual(again.returncode, 2)
        self.assertIn("already holds", again.stderr)

    def test_made_up_data_can_be_written_to_a_file(self) -> None:
        finished = run(
            ["qa", "fake", "--rows", "4", "--column", "name", "--column", "email",
             "--output", "people.csv"],
            self.root, timeout=120,
        )
        self.assertEqual(finished.returncode, 0, finished.stderr)
        body = (self.root / "people.csv").read_text(encoding="utf-8")
        self.assertIn("name,email", body)
        self.assertEqual(len(body.strip().splitlines()), 5)

    def test_a_build_file_can_be_written(self) -> None:
        finished = run(["qa", "ci", "github"], self.root, timeout=120)
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertTrue((self.root / ".github" / "workflows" / "checks.yml").is_file())
        again = run(["qa", "ci", "github"], self.root, timeout=120)
        self.assertEqual(again.returncode, 2)
        self.assertIn("already there", again.stderr)

    def test_explaining_a_failure_shows_the_question_without_asking(self) -> None:
        runs = self.root / ".harness" / "qa" / "runs" / "20260101-000001"
        runs.mkdir(parents=True, exist_ok=True)
        (runs / "result.json").write_text(
            json.dumps({
                "run_id": "r", "cases": [{
                    "id": "broken", "title": "A check", "kind": "command", "status": "failed",
                    "reasons": ["it did not work"], "attempts": [{"evidence": "nothing was there"}],
                }],
            }),
            encoding="utf-8",
        )
        finished = run(["qa", "explain", "--dry-run"], self.root, timeout=120)
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertIn("it did not work", finished.stdout)
        self.assertIn("plain English", finished.stdout)


class TimerCommandTests(unittest.TestCase):
    """Putting an automation on a timer from a terminal.

    A timer runs with nobody watching, so every refusal has to come now, while
    somebody is still there to read it - not at two in the morning.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        (self.root / ".harness" / "pipelines").mkdir()
        for name, kind in (("nightly-check", "git_repo"), ("asks-first", "wait_for_a_person")):
            (self.root / ".harness" / "pipelines" / f"{name}.json").write_text(
                json.dumps({
                    "name": "Nightly check" if kind == "git_repo" else "Asks first",
                    "nodes": [
                        {"id": "start", "kind": "start", "label": "Start", "settings": {}},
                        {"id": "work", "kind": kind, "label": "The work",
                         "settings": {"question": "Shall I?"} if kind == "wait_for_a_person" else {}},
                    ],
                    "edges": [{"from": "start", "to": "work"}],
                }),
                encoding="utf-8",
            )

    def test_one_that_stops_to_ask_a_person_is_refused_until_you_say_anyway(self) -> None:
        refused = run(["timer", "add", "Every night", "Asks first"], self.root)
        self.assertEqual(refused.returncode, 1, refused.stdout)
        self.assertIn("--anyway", refused.stdout)
        went_on = run(
            ["timer", "add", "Every night", "Asks first", "--anyway"], self.root
        )
        self.assertEqual(went_on.returncode, 0, went_on.stderr)
        self.assertIn("every day at 02:00", went_on.stdout)

    def test_turning_one_back_on_asks_again(self) -> None:
        """The reason it should not run alone has not gone away in the
        meantime, so being asked once is not enough."""

        run(["timer", "add", "Every night", "Asks first", "--anyway"], self.root)
        run(["timer", "off", "Every night"], self.root)
        refused = run(["timer", "on", "Every night"], self.root)
        self.assertEqual(refused.returncode, 1, refused.stdout)
        self.assertIn("--anyway", refused.stdout)
        went_on = run(["timer", "on", "Every night", "--anyway"], self.root)
        self.assertEqual(went_on.returncode, 0, went_on.stderr)
        self.assertIn("turned on", went_on.stdout)

    def test_one_with_nothing_wrong_with_it_needs_no_anyway(self) -> None:
        added = run(["timer", "add", "Every night", "Nightly check"], self.root)
        self.assertEqual(added.returncode, 0, added.stderr)
        run(["timer", "off", "Every night"], self.root)
        back_on = run(["timer", "on", "Every night"], self.root)
        self.assertEqual(back_on.returncode, 0, back_on.stdout)

    def test_the_line_for_this_machine_is_printed_and_never_run(self) -> None:
        told = run(["timer", "install"], self.root)
        self.assertEqual(told.returncode, 0, told.stderr)
        self.assertIn("timer run", told.stdout)
        self.assertIn("your decision", told.stdout)

    def test_nothing_on_a_timer_says_so_rather_than_nothing(self) -> None:
        listed = run(["timer", "list"], self.root)
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("Nothing is on a timer yet", listed.stdout)
