from __future__ import annotations

import copy
import json
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from our_harness import qa
from our_harness.config import DEFAULT_CONFIG, LoadedConfig, validate_config
from our_harness.models import HarnessError


def isolated_config(root: Path, **overrides: object) -> LoadedConfig:
    data = copy.deepcopy(DEFAULT_CONFIG)
    data["qa"].update(overrides)
    return LoadedConfig(data, root, [], {})


class SuiteParsingTests(unittest.TestCase):
    def test_command_case_defaults_to_a_clean_exit(self) -> None:
        suite = qa.parse_suite({
            "name": "demo",
            "cases": [{"id": "build", "kind": "command", "command": ["python", "-c", "pass"]}],
        })
        self.assertEqual(suite.cases[0].expect.exit_code, 0)
        self.assertEqual(suite.cases[0].title, "build")
        self.assertEqual(suite.cases[0].cwd, ".")

    def test_file_and_http_cases_get_useful_defaults(self) -> None:
        suite = qa.parse_suite({
            "name": "demo",
            "cases": [
                {"id": "readme", "kind": "file", "path": "README.md"},
                {"id": "health", "kind": "http", "url": "http://127.0.0.1:8765/api/health"},
            ],
        })
        self.assertIs(suite.cases[0].expect.exists, True)
        self.assertEqual(suite.cases[1].expect.status, 200)
        self.assertEqual(suite.cases[1].method, "GET")

    def test_browser_case_defaults_to_no_console_or_page_errors(self) -> None:
        suite = qa.parse_suite({
            "name": "demo",
            "cases": [{"id": "home", "kind": "browser", "url": "http://127.0.0.1:8765/"}],
        })
        case = suite.cases[0]
        self.assertEqual(case.expect.max_console_errors, 0)
        self.assertEqual(case.expect.max_page_errors, 0)
        self.assertEqual(case.viewport, (1280, 800))

    def test_browser_steps_are_read_and_kept_in_order(self) -> None:
        suite = qa.parse_suite({
            "name": "d",
            "cases": [{
                "id": "flow", "kind": "browser", "url": "http://127.0.0.1:1/",
                "steps": [
                    {"do": "click", "target": "#open", "note": "Open the panel"},
                    {"do": "type", "target": "#box", "text": "hello"},
                    {"do": "expect_text", "target": "#title", "text": "Welcome"},
                    {"do": "wait", "ms": 200},
                ],
            }],
        })
        steps = suite.cases[0].steps
        self.assertEqual([step["do"] for step in steps], ["click", "type", "expect_text", "wait"])
        self.assertEqual(steps[0]["note"], "Open the panel")
        self.assertEqual(steps[0]["timeout_ms"], 10_000)

    def test_an_unknown_step_action_lists_the_real_ones(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            qa.parse_suite({
                "name": "d",
                "cases": [{"id": "f", "kind": "browser", "url": "http://127.0.0.1:1/",
                           "steps": [{"do": "hover", "target": "#a"}]}],
            })
        self.assertIn("click", str(caught.exception))

    def test_a_step_missing_its_field_says_which_one(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            qa.parse_suite({
                "name": "d",
                "cases": [{"id": "f", "kind": "browser", "url": "http://127.0.0.1:1/",
                           "steps": [{"do": "type", "target": "#a"}]}],
            })
        self.assertIn("needs a text field", str(caught.exception))

    def test_expecting_empty_text_is_refused_because_it_matches_anything(self) -> None:
        with self.assertRaises(HarnessError):
            qa.parse_suite({
                "name": "d",
                "cases": [{"id": "f", "kind": "browser", "url": "http://127.0.0.1:1/",
                           "steps": [{"do": "expect_text", "target": "#a", "text": ""}]}],
            })

    def test_typing_empty_text_is_allowed_because_it_clears_a_box(self) -> None:
        suite = qa.parse_suite({
            "name": "d",
            "cases": [{"id": "f", "kind": "browser", "url": "http://127.0.0.1:1/",
                       "steps": [{"do": "type", "target": "#a", "text": ""}]}],
        })
        self.assertEqual(suite.cases[0].steps[0]["text"], "")

    def test_steps_need_a_single_page(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            qa.parse_suite({
                "name": "d",
                "cases": [{"id": "f", "kind": "browser", "url": "http://127.0.0.1:1/",
                           "routes": ["/", "/other"],
                           "steps": [{"do": "click", "target": "#a"}]}],
            })
        self.assertIn("at most one route", str(caught.exception))

    def test_duplicate_ids_are_refused(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            qa.parse_suite({
                "name": "demo",
                "cases": [
                    {"id": "same", "kind": "file", "path": "a"},
                    {"id": "same", "kind": "file", "path": "b"},
                ],
            })
        self.assertIn("used twice", str(caught.exception))

    def test_unknown_case_field_names_the_field(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            qa.parse_suite({"name": "d", "cases": [{"id": "a", "kind": "file", "path": "x", "shell": "rm"}]})
        self.assertIn("shell", str(caught.exception))

    def test_expectation_must_match_the_case_kind(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            qa.parse_suite({
                "name": "d",
                "cases": [{"id": "a", "kind": "file", "path": "x", "expect": {"exit_code": 0}}],
            })
        self.assertIn("exit_code", str(caught.exception))

    def test_url_must_be_http_and_carry_no_credentials(self) -> None:
        for url in ("ftp://127.0.0.1/x", "http://user:pass@127.0.0.1/x"):
            with self.subTest(url=url), self.assertRaises(HarnessError):
                qa.parse_suite({"name": "d", "cases": [{"id": "a", "kind": "http", "url": url}]})

    def test_browser_routes_must_stay_inside_the_site(self) -> None:
        with self.assertRaises(HarnessError):
            qa.parse_suite({
                "name": "d",
                "cases": [{"id": "a", "kind": "browser", "url": "http://127.0.0.1/", "routes": ["/../x"]}],
            })

    def test_a_suite_round_trips_through_json(self) -> None:
        original = qa.parse_suite({
            "name": "demo",
            "cases": [
                {
                    "id": "unit", "title": "Unit tests pass", "kind": "command", "tags": ["fast"],
                    "command": ["python", "-m", "unittest"], "retries": 2, "timeout_seconds": 30,
                    "expect": {"exit_code": 0, "stdout_contains": ["OK"]},
                },
                {
                    "id": "api", "kind": "http", "url": "http://127.0.0.1:8765/api/health",
                    "headers": {"Accept": "application/json"},
                    "expect": {"status": 200, "json_fields": {"status": "ok"}},
                },
            ],
        })
        again = qa.parse_suite(json.loads(json.dumps(original.to_dict())))
        self.assertEqual(again.to_dict(), original.to_dict())

    def test_starter_suite_uses_detected_commands(self) -> None:
        suite = qa.starter_suite([["pytest"], ["pytest", "-k", "slow"]], [["ruff", "check"]], [])
        ids = [case.id for case in suite.cases]
        self.assertEqual(ids, ["tests-1", "tests-2", "lint"])
        self.assertEqual(suite.cases[0].command, ("pytest",))


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / "README.md").write_text("Hello project", encoding="utf-8")
        self.config = isolated_config(self.root)
        self.addCleanup(self.temporary.cleanup)

    def suite(self, cases: list[dict]) -> qa.QaSuite:
        return qa.parse_suite({"name": "demo", "cases": cases})

    def test_a_passing_and_a_failing_case_are_reported_separately(self) -> None:
        suite = self.suite([
            {"id": "good", "kind": "command", "command": ["python", "-c", "print('ready')"],
             "expect": {"exit_code": 0, "stdout_contains": ["ready"]}},
            {"id": "bad", "kind": "command", "command": ["python", "-c", "raise SystemExit(3)"]},
        ])
        result = qa.QaRunner(self.config).run(suite, run_id="run-1")
        self.assertFalse(result.passed)
        self.assertEqual(result.counts["passed"], 1)
        self.assertEqual(result.counts["failed"], 1)
        failed = next(case for case in result.cases if case.id == "bad")
        self.assertIn("finished with code 3", failed.reasons[0])

    def test_results_follow_suite_order_even_when_run_in_parallel(self) -> None:
        cases = [
            {"id": f"case-{index}", "kind": "command", "command": ["python", "-c", "pass"]}
            for index in range(6)
        ]
        result = qa.QaRunner(self.config).run(self.suite(cases), workers=6, run_id="run-order")
        self.assertEqual([case.id for case in result.cases], [case["id"] for case in cases])
        self.assertEqual(result.workers, 6)

    def test_missing_text_in_output_is_explained_in_plain_words(self) -> None:
        suite = self.suite([
            {"id": "words", "kind": "command", "command": ["python", "-c", "print('one')"],
             "expect": {"exit_code": 0, "stdout_contains": ["two"]}},
        ])
        result = qa.QaRunner(self.config).run(suite, run_id="run-words")
        self.assertIn('does not hold the text "two"', result.cases[0].reasons[0])

    def test_file_checks_read_the_real_file(self) -> None:
        suite = self.suite([
            {"id": "present", "kind": "file", "path": "README.md",
             "expect": {"exists": True, "contains": ["Hello"], "min_bytes": 3}},
            {"id": "absent", "kind": "file", "path": "missing.txt"},
        ])
        result = qa.QaRunner(self.config).run(suite, run_id="run-files")
        statuses = {case.id: case.status for case in result.cases}
        self.assertEqual(statuses, {"present": "passed", "absent": "failed"})

    def test_a_file_case_cannot_read_outside_the_project(self) -> None:
        for path in ("../secret.txt", "sub/../../x", "C:/Windows/system.ini", "//server/share/x"):
            with self.subTest(path=path), self.assertRaises(HarnessError):
                self.suite([{"id": "escape", "kind": "file", "path": path}])

    def test_a_command_cannot_run_outside_the_project(self) -> None:
        with self.assertRaises(HarnessError):
            self.suite([{"id": "escape", "kind": "command", "cwd": "../..", "command": ["python", "-c", "pass"]}])

    def test_a_retry_that_finally_passes_is_marked_flaky(self) -> None:
        marker = self.root / "flag.txt"
        code = (
            "import pathlib,sys\n"
            f"p = pathlib.Path({str(marker)!r})\n"
            "if p.exists():\n    sys.exit(0)\n"
            "p.write_text('seen')\n"
            "sys.exit(1)\n"
        )
        suite = self.suite([{"id": "unstable", "kind": "command", "retries": 1, "command": ["python", "-c", code]}])
        result = qa.QaRunner(self.config).run(suite, run_id="run-flaky")
        case = result.cases[0]
        self.assertEqual(case.status, "flaky")
        self.assertEqual(len(case.attempts), 2)
        self.assertTrue(result.passed)

    def test_a_case_that_runs_too_long_is_stopped_and_explained(self) -> None:
        suite = self.suite([
            {"id": "slow", "kind": "command", "timeout_seconds": 1,
             "command": ["python", "-c", "import time; time.sleep(30)"]},
        ])
        result = qa.QaRunner(self.config).run(suite, run_id="run-slow")
        self.assertEqual(result.cases[0].status, "failed")
        self.assertIn("longer than 1 second", result.cases[0].reasons[0])

    def test_evidence_is_written_beside_the_run(self) -> None:
        suite = self.suite([{"id": "good", "kind": "command", "command": ["python", "-c", "print('hi')"]}])
        result = qa.QaRunner(self.config).run(suite, run_id="run-art")
        folder = self.root / ".harness" / "qa" / "runs" / "run-art"
        self.assertTrue((folder / "result.json").is_file())
        self.assertTrue((folder / "good" / "attempt-1.txt").is_file())
        self.assertIn("good/attempt-1.txt", result.cases[0].artifacts)

    def test_artifacts_can_be_turned_off(self) -> None:
        suite = self.suite([{"id": "good", "kind": "command", "command": ["python", "-c", "pass"]}])
        qa.QaRunner(self.config).run(suite, run_id="run-none", write_artifacts=False)
        self.assertFalse((self.root / ".harness" / "qa" / "runs").exists())

    def test_old_runs_are_trimmed(self) -> None:
        config = isolated_config(self.root, keep_runs=2)
        suite = self.suite([{"id": "good", "kind": "command", "command": ["python", "-c", "pass"]}])
        for index in range(4):
            qa.QaRunner(config).run(suite, run_id=f"run-{index}")
        kept = sorted(item.name for item in (self.root / ".harness" / "qa" / "runs").iterdir())
        self.assertEqual(kept, ["run-2", "run-3"])

    def test_tag_and_id_filters_choose_cases(self) -> None:
        suite = self.suite([
            {"id": "one", "kind": "command", "tags": ["fast"], "command": ["python", "-c", "pass"]},
            {"id": "two", "kind": "command", "tags": ["slow"], "command": ["python", "-c", "pass"]},
        ])
        runner = qa.QaRunner(self.config)
        self.assertEqual([case.id for case in runner.select(suite, tags=["fast"])], ["one"])
        self.assertEqual([case.id for case in runner.select(suite, ids=["two"])], ["two"])
        with self.assertRaises(HarnessError):
            runner.select(suite, tags=["nothing"])
        with self.assertRaises(HarnessError):
            runner.select(suite, ids=["nothing"])

    def test_an_http_case_may_not_leave_the_allowed_hosts(self) -> None:
        suite = self.suite([{"id": "remote", "kind": "http", "url": "http://example.invalid/x"}])
        result = qa.QaRunner(self.config).run(suite, run_id="run-host")
        self.assertEqual(result.cases[0].status, "failed")
        self.assertIn("may not call example.invalid", result.cases[0].reasons[0])

    def test_http_checks_read_status_body_and_json(self) -> None:
        answers = {
            "http://127.0.0.1:9/ok": (200, '{"status": "ok", "count": 2}'),
            "http://127.0.0.1:9/bad": (500, "server broke"),
        }

        def fake_fetch(case: qa.QaCase, timeout: float) -> tuple[int, str, int]:
            status, body = answers[case.url]
            return status, body, len(body)

        suite = self.suite([
            {"id": "ok", "kind": "http", "url": "http://127.0.0.1:9/ok",
             "expect": {"status": 200, "json_fields": {"status": "ok", "count": 2}}},
            {"id": "wrong-field", "kind": "http", "url": "http://127.0.0.1:9/ok",
             "expect": {"status": 200, "json_fields": {"status": "down"}}},
            {"id": "bad", "kind": "http", "url": "http://127.0.0.1:9/bad", "expect": {"status": 200}},
        ])
        runner = qa.QaRunner(self.config, http_fetch=fake_fetch)
        result = runner.run(suite, run_id="run-http")
        statuses = {case.id: case.status for case in result.cases}
        self.assertEqual(statuses, {"ok": "passed", "wrong-field": "failed", "bad": "failed"})
        wrong = next(case for case in result.cases if case.id == "wrong-field")
        self.assertIn("field status holds", wrong.reasons[0])
        broken = next(case for case in result.cases if case.id == "bad")
        self.assertIn("answered with 500", broken.reasons[0])

    def test_a_browser_case_is_skipped_when_playwright_is_missing(self) -> None:
        suite = self.suite([{"id": "home", "kind": "browser", "url": "http://127.0.0.1:8765/"}])
        runner = qa.QaRunner(self.config)
        runner.browser_available = lambda: False  # type: ignore[method-assign]
        result = runner.run(suite, run_id="run-browser")
        self.assertEqual(result.cases[0].status, "skipped")
        self.assertIn("Playwright", result.cases[0].reasons[0])
        self.assertTrue(result.passed)

    def test_browser_findings_turn_into_plain_reasons(self) -> None:
        case = qa.parse_suite({
            "name": "d",
            "cases": [{
                "id": "home", "kind": "browser", "url": "http://127.0.0.1:8765/",
                "expect": {"max_console_errors": 0, "max_failed_requests": 0, "body_contains": ["Welcome"]},
            }],
        }).cases[0]
        report = {
            "routes": [{"route": "/", "status": 200}, {"route": "/gone", "status": 404}],
            "consoleErrors": [{"route": "/", "text": "boom"}],
            "pageErrors": [],
            "requestFailures": [{"route": "/", "url": "http://127.0.0.1:8765/app.js", "text": "failed"}],
            "accessibility": [],
            "text": "Nothing here",
        }
        reasons, summary, _ = qa.QaRunner(self.config)._browser_reasons(case, report)
        joined = " ".join(reasons)
        self.assertIn("error message in the browser console", joined)
        self.assertIn("failed network request", joined)
        self.assertIn("/gone answered with 404", joined)
        self.assertIn('does not hold the text "Welcome"', joined)
        self.assertIn("console_errors", summary)

    def test_a_failed_step_names_the_step_and_what_the_browser_said(self) -> None:
        case = qa.parse_suite({
            "name": "d",
            "cases": [{
                "id": "flow", "kind": "browser", "url": "http://127.0.0.1:1/",
                "steps": [
                    {"do": "click", "target": "#one"},
                    {"do": "expect_text", "target": "#two", "text": "Done"},
                    {"do": "click", "target": "#three"},
                ],
            }],
        }).cases[0]
        report = {
            "routes": [{"route": "/", "status": 200}],
            "steps": [
                {"route": "/", "label": "click #one", "ok": True},
                {"route": "/", "label": "expect_text on #two", "ok": False, "text": "the page shows nothing"},
            ],
            "text": "",
        }
        reasons, _summary, _full = qa.QaRunner(self.config)._browser_reasons(case, report)
        joined = " ".join(reasons)
        self.assertIn("Step 2 of 3", joined)
        self.assertIn("expect_text on #two", joined)
        self.assertIn("the page shows nothing", joined)
        self.assertIn("Only 2 of 3 steps ran", joined)

    def test_the_generated_script_is_valid_javascript(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not on this machine")
        script = qa.browser_script({
            "url": "http://127.0.0.1:1/",
            "routes": ["/"],
            "steps": [{"do": "click", "target": "#a", "timeout_ms": 1000}],
            "viewport": {"width": 800, "height": 600},
            "clickAll": True,
            "checkAccessibility": True,
            "timeoutMs": 1000,
            "settleMs": 10,
        })
        path = self.root / "generated.js"
        path.write_text(script, encoding="utf-8")
        finished = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
        self.assertEqual(finished.returncode, 0, finished.stderr)

    def test_every_step_action_reaches_its_own_branch(self) -> None:
        """The script must handle each action the parser accepts, or a valid
        suite would fail at run time with 'unknown step'."""

        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not on this machine")
        script = qa.browser_script({"url": "http://127.0.0.1:1/", "routes": ["/"], "steps": []})
        # Drive runStep with a stand-in page and locator that record the call
        # instead of opening a browser, then check no action falls through.
        harness = """
const calls = [];
const locator = new Proxy({}, {get: (_t, name) => {
  if (name === 'first') return () => locator;
  if (name === 'count') return async () => 1;
  if (name === 'innerText') return async () => 'Welcome here';
  if (name === 'inputValue') return async () => 'Welcome here';
  if (name === 'evaluate') return async () => 'div';
  return async (...args) => { calls.push([String(name), ...args.filter((item) => typeof item === 'string')]); };
}});
const page = {locator: (selector) => { calls.push(['locator', selector]); return locator; },
              waitForTimeout: async () => {}};
const steps = [
  {do: 'click', target: '#a'},
  {do: 'type', target: '#a', text: 'hi'},
  {do: 'press', target: '#a', key: 'Enter'},
  {do: 'choose', target: '#a', value: 'one'},
  {do: 'expect_text', target: '#a', text: 'Welcome'},
  {do: 'expect_visible', target: '#a'},
  {do: 'expect_hidden', target: '#a'},
  {do: 'wait', ms: 1},
];
(async () => {
  for (const step of steps) {
    try { await runStep(page, {...step, timeout_ms: 500}); }
    catch (error) { console.log('BROKE ' + step.do + ': ' + error.message); process.exit(1); }
  }
  console.log('ALL_ACTIONS_HANDLED ' + steps.length);
})();
"""
        body = script.replace("const { chromium } = require('playwright');", "")
        body = body.split("(async () => {", 1)[0]
        path = self.root / "steps.js"
        path.write_text(body + harness, encoding="utf-8")
        finished = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=60)
        self.assertIn("ALL_ACTIONS_HANDLED 8", finished.stdout, finished.stdout + finished.stderr)
        self.assertEqual(sorted(qa.STEP_ACTIONS), sorted([
            "choose", "click", "expect_hidden", "expect_text", "expect_visible", "press", "type", "wait",
        ]), "a new step action needs a branch in the generated script and a line in this test")

    def test_the_generated_browser_script_carries_the_plan(self) -> None:
        script = qa.browser_script({"url": "http://127.0.0.1:1/", "routes": ["/a"], "clickAll": True})
        self.assertIn("require('playwright')", script)
        self.assertIn('"routes": ["/a"]', script)
        self.assertNotIn("__PLAN__", script)


class HistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.config = isolated_config(self.root, flaky_min_runs=4, flaky_threshold=0.2)
        self.addCleanup(self.temporary.cleanup)

    def history(self, statuses: dict[str, list[str]]) -> list[dict]:
        runs = []
        length = max(len(value) for value in statuses.values())
        for index in range(length):
            runs.append({
                "run_id": f"r{index}",
                "cases": [
                    {"id": case_id, "status": value[index], "duration_ms": 1}
                    for case_id, value in statuses.items()
                    if index < len(value)
                ],
            })
        return runs

    def test_a_case_that_alternates_is_called_unstable(self) -> None:
        runs = self.history({"wobbly": ["passed", "failed", "passed", "failed", "passed"]})
        report = qa.flaky_report(self.config, runs)
        self.assertEqual([item["id"] for item in report], ["wobbly"])
        self.assertGreater(report[0]["instability"], 0.5)

    def test_a_case_that_always_fails_is_broken_not_unstable(self) -> None:
        runs = self.history({"broken": ["failed"] * 6})
        self.assertEqual(qa.flaky_report(self.config, runs), [])

    def test_a_case_that_always_passes_is_left_out(self) -> None:
        runs = self.history({"steady": ["passed"] * 6})
        self.assertEqual(qa.flaky_report(self.config, runs), [])

    def test_a_case_with_too_little_history_is_left_out(self) -> None:
        runs = self.history({"new": ["passed", "failed"]})
        self.assertEqual(qa.flaky_report(self.config, runs), [])

    def test_a_case_that_needed_a_retry_is_reported(self) -> None:
        runs = self.history({"retried": ["passed", "passed", "flaky", "passed", "passed"]})
        report = qa.flaky_report(self.config, runs)
        self.assertEqual([item["id"] for item in report], ["retried"])
        self.assertIn("retry", report[0]["why"])

    def test_history_is_appended_and_bounded(self) -> None:
        suite = qa.parse_suite({"name": "d", "cases": [{"id": "a", "kind": "file", "path": "x", "expect": {"exists": False}}]})
        runner = qa.QaRunner(self.config)
        for index in range(3):
            qa.record_history(self.config, runner.run(suite, run_id=f"h{index}", write_artifacts=False))
        stored = qa.load_history(self.config)
        self.assertEqual([run["run_id"] for run in stored], ["h0", "h1", "h2"])
        self.assertTrue(all(run["passed"] for run in stored))


class CheckHealthTests(unittest.TestCase):
    """What the harness says about its own checks after watching them run."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.config = isolated_config(self.root, flaky_min_runs=4, flaky_threshold=0.2)
        self.addCleanup(self.temporary.cleanup)

    def runs(self, count: int, **cases: tuple) -> list[dict]:
        history = []
        for index in range(count):
            record = []
            for case_id, values in cases.items():
                status, duration = values[index] if isinstance(values[0], tuple) else values
                record.append({"id": case_id, "status": status, "duration_ms": duration})
            history.append({"run_id": f"r{index}", "cases": record})
        return history

    def by_id(self, findings: list[dict]) -> dict[str, dict]:
        return {item["id"]: item for item in findings}

    def test_a_healthy_check_is_never_mentioned(self) -> None:
        history = self.runs(6, steady=("passed", 30))
        self.assertEqual(qa.check_health(self.config, None, history), [])

    def test_a_check_that_never_passes_is_called_out(self) -> None:
        history = self.runs(6, broken=("failed", 20))
        found = self.by_id(qa.check_health(self.config, None, history))["broken"]
        self.assertEqual(found["problem"], "has never passed")
        self.assertIn("failed all 6", found["why"])
        self.assertIn("fix", found["what_to_do"])

    def test_a_check_that_alternates_is_called_out_once(self) -> None:
        history = self.runs(
            6, wobbly=[("passed", 10), ("failed", 10), ("passed", 10), ("failed", 10), ("passed", 10), ("failed", 10)]
        )
        findings = qa.check_health(self.config, None, history)
        self.assertEqual([item["id"] for item in findings], ["wobbly"])
        self.assertEqual(findings[0]["problem"], "keeps changing its mind")

    def test_a_check_that_only_ever_skips_is_called_out(self) -> None:
        history = self.runs(6, absent=("skipped", 1))
        found = self.by_id(qa.check_health(self.config, None, history))["absent"]
        self.assertEqual(found["problem"], "never actually runs")

    def test_a_check_that_got_much_slower_is_called_out(self) -> None:
        history = self.runs(
            6, slow=[("passed", 200), ("passed", 200), ("passed", 220), ("passed", 4000), ("passed", 4100), ("passed", 4200)]
        )
        found = self.by_id(qa.check_health(self.config, None, history))["slow"]
        self.assertEqual(found["problem"], "got a lot slower")
        self.assertIn("0.2 seconds", found["why"])
        self.assertIn("at least 4.1 seconds", found["why"])

    def test_one_slow_run_at_the_end_is_not_called_a_slowdown(self) -> None:
        """A busy machine makes one run slow. That is not a trend."""

        history = self.runs(
            6, blip=[("passed", 100), ("passed", 100), ("passed", 100), ("passed", 100), ("passed", 100), ("passed", 5000)]
        )
        self.assertEqual(qa.check_health(self.config, None, history), [])

    def test_one_slow_run_in_the_middle_is_not_called_a_slowdown(self) -> None:
        history = self.runs(
            6, blip=[("passed", 100), ("passed", 9000), ("passed", 100), ("passed", 100), ("passed", 100), ("passed", 100)]
        )
        self.assertEqual(qa.check_health(self.config, None, history), [])

    def test_a_slowdown_needs_enough_runs_to_see_a_trend(self) -> None:
        """With five runs the windows are one run each, which proves nothing."""

        history = self.runs(
            5, slow=[("passed", 200), ("passed", 200), ("passed", 200), ("passed", 200), ("passed", 9000)]
        )
        self.assertEqual(qa.check_health(self.config, None, history), [])

    def test_the_middle_value_is_used_so_one_odd_early_run_does_not_hide_a_slowdown(self) -> None:
        history = self.runs(
            9,
            slow=[
                ("passed", 200), ("passed", 200), ("passed", 9000),
                ("passed", 200), ("passed", 200), ("passed", 200),
                ("passed", 5000), ("passed", 5100), ("passed", 5200),
            ],
        )
        found = self.by_id(qa.check_health(self.config, None, history))["slow"]
        self.assertEqual(found["problem"], "got a lot slower")
        self.assertIn("0.2 seconds", found["why"])

    def test_a_small_slowdown_is_not_worth_mentioning(self) -> None:
        history = self.runs(
            6, fine=[("passed", 200), ("passed", 200), ("passed", 210), ("passed", 300), ("passed", 320), ("passed", 340)]
        )
        self.assertEqual(qa.check_health(self.config, None, history), [])

    def test_a_fast_check_that_doubles_is_not_worth_mentioning(self) -> None:
        history = self.runs(
            6, quick=[("passed", 10), ("passed", 10), ("passed", 10), ("passed", 25), ("passed", 25), ("passed", 25)]
        )
        self.assertEqual(qa.check_health(self.config, None, history), [])

    def test_too_little_history_says_nothing(self) -> None:
        history = self.runs(2, broken=("failed", 10))
        self.assertEqual(qa.check_health(self.config, None, history), [])

    def test_a_check_in_the_suite_with_no_history_is_named(self) -> None:
        suite = qa.parse_suite({"name": "d", "cases": [{"id": "brand-new", "kind": "file", "path": "x"}]})
        history = self.runs(6, steady=("passed", 30))
        found = self.by_id(qa.check_health(self.config, suite, history))["brand-new"]
        self.assertEqual(found["problem"], "has never been run")

    def test_nothing_is_said_about_a_suite_with_no_history_at_all(self) -> None:
        suite = qa.parse_suite({"name": "d", "cases": [{"id": "brand-new", "kind": "file", "path": "x"}]})
        self.assertEqual(qa.check_health(self.config, suite, []), [])

    def test_the_worst_problem_is_listed_first(self) -> None:
        history = self.runs(
            6,
            broken=("failed", 20),
            absent=("skipped", 1),
            slow=[("passed", 200), ("passed", 200), ("passed", 200), ("passed", 5000), ("passed", 5000), ("passed", 5000)],
        )
        findings = qa.check_health(self.config, None, history)
        self.assertEqual([item["id"] for item in findings], ["broken", "absent", "slow"])

    def test_every_finding_says_why_and_what_to_do(self) -> None:
        history = self.runs(6, broken=("failed", 20), absent=("skipped", 1))
        findings = qa.check_health(self.config, None, history)
        self.assertTrue(findings)
        for item in findings:
            self.assertEqual(set(item), {"id", "problem", "why", "what_to_do"})
            for field in ("problem", "why", "what_to_do"):
                self.assertTrue(item[field].strip(), f"{item['id']} has an empty {field}")


class ReportTests(unittest.TestCase):
    def result(self) -> qa.QaRunResult:
        return qa.QaRunResult(
            run_id="r1",
            suite_name="demo",
            started_at="2026-01-01T00:00:00Z",
            duration_ms=1200,
            workers=2,
            cases=(
                qa.QaCaseResult(id="ok", title="Everything works", kind="command", status="passed", duration_ms=10),
                qa.QaCaseResult(
                    id="bad", title="Broken check", kind="command", status="failed", duration_ms=20,
                    reasons=("The command finished with code 1; the case expects 0",),
                    attempts=(qa.QaAttempt(number=1, passed=False, duration_ms=20, evidence="traceback here"),),
                ),
                qa.QaCaseResult(id="gone", title="Not runnable", kind="browser", status="skipped", duration_ms=1,
                                reasons=("No browser driver on this machine",)),
            ),
        )

    def test_markdown_names_the_failure(self) -> None:
        text = qa.report_markdown(self.result())
        self.assertIn("Some checks failed", text)
        self.assertIn("| bad | failed |", text)
        self.assertIn("traceback here", text)

    def test_junit_xml_is_valid_and_counts_match(self) -> None:
        tree = ElementTree.fromstring(qa.report_junit_xml(self.result()))
        self.assertEqual(tree.get("tests"), "3")
        self.assertEqual(tree.get("failures"), "1")
        self.assertEqual(tree.get("skipped"), "1")
        suite = tree.find("testsuite")
        assert suite is not None
        self.assertEqual(len(suite.findall("testcase")), 3)
        self.assertIsNotNone(suite.find("./testcase[@id='bad']/failure"))
        self.assertIsNotNone(suite.find("./testcase[@id='gone']/skipped"))

    def test_html_escapes_content_and_states_the_headline(self) -> None:
        result = self.result()
        page = qa.report_html(result)
        self.assertIn("<!doctype html>", page)
        self.assertIn("Some checks failed", page)
        self.assertIn('<html lang="en">', page)
        self.assertNotIn("<script", page)

    def test_html_escapes_dangerous_text(self) -> None:
        result = qa.QaRunResult(
            run_id="r", suite_name="s", started_at="t", duration_ms=1, workers=1,
            cases=(qa.QaCaseResult(id="x", title="<img src=x onerror=alert(1)>", kind="file", status="failed",
                                   duration_ms=1, reasons=("<script>bad()</script>",)),),
        )
        page = qa.report_html(result)
        self.assertNotIn("<img src=x", page)
        self.assertIn("&lt;img src=x", page)
        self.assertNotIn("<script>bad()", page)

    def test_every_named_format_renders(self) -> None:
        result = self.result()
        for name in qa.REPORT_FORMATS:
            with self.subTest(name=name):
                self.assertTrue(qa.render_report(result, name).strip())
        with self.assertRaises(HarnessError):
            qa.render_report(result, "pdf")


class GenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.config = isolated_config(self.root)
        self.addCleanup(self.temporary.cleanup)

    def test_the_prompt_names_existing_ids_and_detected_commands(self) -> None:
        suite = qa.parse_suite({"name": "d", "cases": [{"id": "old", "kind": "file", "path": "x"}]})
        prompt = qa.generation_prompt(suite, [{"stack": "python", "test_commands": [["pytest"]]}], "the parser", 4)
        self.assertIn("old", prompt)
        self.assertIn("pytest", prompt)
        self.assertIn("the parser", prompt)
        self.assertIn("at most 4", prompt)

    def test_a_fenced_answer_is_still_read(self) -> None:
        answer = '```json\n{"cases": [{"id": "new", "kind": "file", "path": "README.md"}]}\n```'
        candidates = qa.parse_generated_cases(answer)
        self.assertEqual(candidates[0]["case"]["id"], "new")
        self.assertEqual(candidates[0]["warnings"], [])

    def test_a_reused_id_is_refused(self) -> None:
        answer = '{"cases": [{"id": "old", "kind": "file", "path": "x"}]}'
        with self.assertRaises(HarnessError) as caught:
            qa.parse_generated_cases(answer, ["old"])
        self.assertIn("reused an existing case id", str(caught.exception))

    def test_an_answer_without_json_is_refused(self) -> None:
        with self.assertRaises(HarnessError):
            qa.parse_generated_cases("I could not think of any tests.")

    def test_a_risky_proposal_is_flagged_but_still_shown(self) -> None:
        answer = json.dumps({"cases": [
            {"id": "wipe", "kind": "command", "command": ["rm", "-rf", "build"]},
            {"id": "away", "kind": "http", "url": "http://example.com/health"},
        ]})
        candidates = qa.parse_generated_cases(answer)
        self.assertIn("rm", candidates[0]["warnings"][0])
        self.assertIn("example.com", candidates[1]["warnings"][0])

    def test_accepting_a_candidate_moves_it_into_the_suite(self) -> None:
        qa.write_suite(self.config, qa.parse_suite({"name": "d", "cases": []}))
        candidates = qa.parse_generated_cases('{"cases": [{"id": "new", "kind": "file", "path": "README.md"}]}')
        qa.save_candidates(self.config, candidates)
        suite, accepted = qa.accept_candidates(self.config, ["new"])
        self.assertEqual(accepted, ("new",))
        self.assertEqual([case.id for case in suite.cases], ["new"])
        self.assertEqual(qa.load_candidates(self.config), [])
        self.assertEqual([case.id for case in qa.load_suite(self.config).cases], ["new"])

    def test_rejecting_a_candidate_removes_it_without_touching_the_suite(self) -> None:
        qa.write_suite(self.config, qa.parse_suite({"name": "d", "cases": []}))
        candidates = qa.parse_generated_cases('{"cases": [{"id": "new", "kind": "file", "path": "README.md"}]}')
        qa.save_candidates(self.config, candidates)
        self.assertEqual(qa.reject_candidates(self.config, ["new"]), ("new",))
        self.assertEqual(qa.load_candidates(self.config), [])
        self.assertEqual(qa.load_suite(self.config).cases, ())

    def test_accepting_an_unknown_id_says_so(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            qa.accept_candidates(self.config, ["missing"])
        self.assertIn("no proposed case named missing", str(caught.exception))


class CommandLineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / "README.md").write_text("# Demo\n", encoding="utf-8")
        (self.root / ".harness").mkdir()
        (self.root / ".harness" / "config.json").write_text(
            json.dumps({"schema_version": 1, "memory": {"enabled": False}}), encoding="utf-8"
        )
        self.addCleanup(self.temporary.cleanup)

    def run_cli(self, *arguments: str) -> tuple[int, str]:
        from contextlib import redirect_stdout
        from io import StringIO

        from our_harness import cli

        captured = StringIO()
        with redirect_stdout(captured):
            code = cli.main(["--project", str(self.root), "qa", *arguments])
        return code, captured.getvalue()

    def test_init_then_list_then_run(self) -> None:
        code, output = self.run_cli("init")
        self.assertEqual(code, 0)
        self.assertIn("starter check", output)
        self.assertTrue((self.root / ".harness" / "qa" / "suite.json").is_file())

        code, output = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("readme-exists", output)

        code, output = self.run_cli("run")
        self.assertEqual(code, 0)
        self.assertIn("All checks passed", output)

    def test_init_refuses_to_overwrite_without_force(self) -> None:
        self.run_cli("init")
        self.assertEqual(self.run_cli("init")[0], 2)
        self.assertEqual(self.run_cli("init", "--force")[0], 0)

    def test_run_writes_a_report_file(self) -> None:
        self.run_cli("init")
        code, output = self.run_cli("run", "--format", "html", "--output", "reports/qa.html")
        self.assertEqual(code, 0)
        page = (self.root / "reports" / "qa.html").read_text(encoding="utf-8")
        self.assertIn("<!doctype html>", page)
        self.assertIn("Report written to", output)

    def test_a_failing_suite_ends_with_a_non_zero_code(self) -> None:
        qa.write_suite(
            LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {}),
            qa.parse_suite({"name": "d", "cases": [{"id": "gone", "kind": "file", "path": "missing.txt"}]}),
        )
        code, output = self.run_cli("run")
        self.assertEqual(code, 1)
        self.assertIn("Some checks failed", output)

    def test_running_without_a_suite_says_what_to_do(self) -> None:
        from contextlib import redirect_stderr
        from io import StringIO

        from our_harness import cli

        captured = StringIO()
        with redirect_stderr(captured):
            code = cli.main(["--project", str(self.root), "qa", "run"])
        self.assertEqual(code, 2)
        self.assertIn("harness qa init", captured.getvalue())


class ConfigTests(unittest.TestCase):
    def test_the_default_qa_section_is_valid(self) -> None:
        validate_config(copy.deepcopy(DEFAULT_CONFIG))

    def test_the_suite_path_must_stay_in_the_project(self) -> None:
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["qa"]["suite"] = "../outside.json"
        with self.assertRaises(HarnessError):
            validate_config(data)

    def test_the_flaky_threshold_has_a_sane_range(self) -> None:
        for value in (0, 0.9, "half", True):
            data = copy.deepcopy(DEFAULT_CONFIG)
            data["qa"]["flaky_threshold"] = value
            with self.subTest(value=value), self.assertRaises(HarnessError):
                validate_config(data)

    def test_at_least_one_host_must_be_allowed(self) -> None:
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["qa"]["allow_hosts"] = []
        with self.assertRaises(HarnessError):
            validate_config(data)


if __name__ == "__main__":
    unittest.main()
