"""Bugs found by hunting, each with the case that proves it stays fixed.

Every test here failed before its fix. They are kept together so nobody has to
guess why a rule exists.
"""

from __future__ import annotations

import copy
import tempfile
import unittest
import xml.etree.ElementTree as ElementTree
from pathlib import Path

from our_harness import contracts, qa, scan
from our_harness.config import DEFAULT_CONFIG, LoadedConfig


class SkippedRetryTests(unittest.TestCase):
    """A later attempt that never ran must not wipe out a real failure."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.addCleanup(self.temporary.cleanup)

    def kind(self, answers: list) -> dict:
        calls = {"count": 0}

        def run(case, runner):
            calls["count"] += 1
            answer = answers[min(calls["count"], len(answers)) - 1]
            if answer == "skip":
                raise qa.QaSkipped("The dependency was not there this time")
            if answer == "fail":
                return ("Found a real problem in the code",), "", ""
            return (), "", ""

        return qa.validated_kinds([
            qa.CheckKind(name="moody", summary="A check that changes its mind", run=run)
        ])

    def result(self, answers: list, run_id: str):
        kinds = self.kind(answers)
        suite = qa.parse_suite(
            {"name": "d", "cases": [{"id": "c", "kind": "moody", "retries": 1}]},
            extra_kinds=kinds,
        )
        runner = qa.QaRunner(self.config, extra_kinds=kinds)
        return runner.run(suite, run_id=run_id, write_artifacts=False).cases[0]

    def test_a_failure_then_a_skip_is_still_a_failure(self) -> None:
        case = self.result(["fail", "skip"], "r1")
        self.assertEqual(case.status, "failed")
        self.assertIn("Found a real problem", case.reasons[0])
        self.assertIn("later attempt was skipped", " ".join(case.reasons))

    def test_a_skip_on_the_first_try_is_still_a_skip(self) -> None:
        case = self.result(["skip"], "r2")
        self.assertEqual(case.status, "skipped")

    def test_a_failure_then_a_pass_is_flaky(self) -> None:
        case = self.result(["fail", "pass"], "r3")
        self.assertEqual(case.status, "flaky")

    def test_a_pass_first_time_stays_a_pass(self) -> None:
        case = self.result(["pass"], "r4")
        self.assertEqual(case.status, "passed")
        self.assertEqual(case.reasons, ())


class ExampleHeuristicTests(unittest.TestCase):
    """A real key is still a real key, whatever else the line says."""

    def test_a_real_key_beside_the_word_environment_is_still_found(self) -> None:
        line = 'AWS_KEY = "AKIAABCDEFGHIJKLMNOP"  # loaded via os.environ fallback in prod'
        found = scan.scan_text(line, "config.py")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].kind, "an Amazon key")
        self.assertNotIn("AKIAABCDEFGHIJKLMNOP", found[0].excerpt)

    def test_a_real_key_beside_the_word_sample_is_still_found(self) -> None:
        line = 'token = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"  # see the sample project'
        self.assertEqual(len(scan.scan_text(line, "a.py")), 1)

    def test_a_placeholder_is_still_left_alone(self) -> None:
        for line in (
            'KEY = "sk-your-key-here-1234567890"',
            'password = "${DB_PASSWORD}"',
            'api_key = "changeme-please-now"',
        ):
            with self.subTest(line=line):
                self.assertEqual(scan.scan_text(line, "a.py"), [])


class RowFillingTests(unittest.TestCase):
    """Every field a check reads must be filled in from the table row."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.addCleanup(self.temporary.cleanup)

    def test_a_scan_over_rows_really_uses_each_row(self) -> None:
        suite = qa.parse_suite({"name": "d", "cases": [{
            "id": "keys", "kind": "secrets",
            "paths": ["${row.folder}/**/*.py"],
            "rows": [{"folder": "one"}, {"folder": "two"}],
        }]})
        expanded = qa.QaRunner(self.config).expand(suite.cases[0])
        self.assertEqual(
            [case.paths for case in expanded],
            [("one/**/*.py",), ("two/**/*.py",)],
        )

    def test_the_fillable_list_holds_every_text_field_a_kind_uses(self) -> None:
        # A new kind with a new text field must be added to the list, or its
        # rows would quietly do nothing.
        for kind, fields in qa._CASE_FIELDS_BY_KIND.items():
            for name in fields:
                if name in qa._NOT_FILLABLE:
                    continue
                with self.subTest(kind=kind, field=name):
                    self.assertIn(name, qa.FILLABLE_CASE_FIELDS)


class NotANumberTests(unittest.TestCase):
    """A value that is not a real number must not slip past a limit."""

    def test_not_a_number_fails_a_range(self) -> None:
        schema = {"type": "object", "properties": {"score": {"type": "number", "minimum": 0, "maximum": 100}}}
        found = contracts.problems({"score": float("nan")}, schema)
        self.assertTrue(found)
        self.assertIn("real number", found[0])

    def test_endless_numbers_fail_too(self) -> None:
        schema = {"type": "number", "maximum": 10}
        self.assertTrue(contracts.problems(float("inf"), schema))
        self.assertTrue(contracts.problems(float("-inf"), schema))

    def test_ordinary_numbers_still_pass(self) -> None:
        self.assertFalse(contracts.problems(5, {"type": "number", "minimum": 0, "maximum": 10}))


class SameItemTwiceTests(unittest.TestCase):
    def test_one_and_one_point_zero_are_the_same_number(self) -> None:
        schema = {"type": "array", "items": {"type": "number"}, "uniqueItems": True}
        self.assertTrue(contracts.problems([1, 1.0], schema))
        self.assertTrue(contracts.problems([1, 1], schema))
        self.assertFalse(contracts.problems([1, 2], schema))

    def test_the_same_object_written_in_a_different_order_is_the_same(self) -> None:
        schema = {"type": "array", "uniqueItems": True}
        self.assertTrue(contracts.problems([{"a": 1, "b": 2}, {"b": 2, "a": 1}], schema))

    def test_true_is_not_the_number_one(self) -> None:
        schema = {"type": "array", "uniqueItems": True}
        self.assertFalse(contracts.problems([True, 1.5], schema))


class JunitReportTests(unittest.TestCase):
    """A build server must never choke on the whole report over one odd byte."""

    def report(self) -> str:
        result = qa.QaRunResult(
            run_id="r", suite_name="suite\x01", started_at="2026-01-01T00:00:00Z",
            duration_ms=5, workers=1,
            cases=(
                qa.QaCaseResult(
                    id="bad\x03", title="A test\x1b[31m", kind="com\x04mand", status="failed",
                    duration_ms=2, reasons=("boom \x00\x1b[0m bad",),
                    attempts=(qa.QaAttempt(number=1, passed=False, duration_ms=2, evidence="saw \x07 this"),),
                ),
                qa.QaCaseResult(
                    id="odd", title="Sometimes\x05", kind="command", status="flaky",
                    duration_ms=1, reasons=("changed its mind \x06",),
                ),
                qa.QaCaseResult(
                    id="gone", title="Not runnable", kind="browser", status="skipped", duration_ms=1,
                ),
            ),
        )
        return qa.report_junit_xml(result)

    def test_the_report_can_always_be_read_back(self) -> None:
        tree = ElementTree.fromstring(self.report())
        self.assertEqual(tree.tag, "testsuites")
        self.assertEqual(len(tree.findall(".//testcase")), 3)

    def test_the_words_survive_even_though_the_odd_bytes_do_not(self) -> None:
        body = self.report()
        self.assertIn("boom", body)
        self.assertNotIn("\x00", body)
        self.assertNotIn("\x1b", body)

    def test_tabs_and_new_lines_are_kept(self) -> None:
        self.assertEqual(qa.plain_xml_text("a\tb\nc\rd"), "a\tb\nc\rd")
        self.assertEqual(qa.plain_xml_text("a\x00b"), "a b")


if __name__ == "__main__":
    unittest.main()
