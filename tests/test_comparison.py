"""What changed between two runs."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from our_harness import comparison
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError


def run(run_id: str, cases: list[dict]) -> dict:
    return {"run_id": run_id, "cases": cases}


def case(case_id: str, status: str, duration_ms: int = 10, reasons: list[str] | None = None) -> dict:
    return {
        "id": case_id,
        "title": f"The {case_id} check",
        "kind": "command",
        "status": status,
        "duration_ms": duration_ms,
        "reasons": reasons or ([] if status == "passed" else ["it did not work"]),
    }


class CompareTests(unittest.TestCase):
    def test_a_check_that_started_failing_is_the_headline(self) -> None:
        found = comparison.compare(
            run("one", [case("a", "passed"), case("b", "passed")]),
            run("two", [case("a", "passed"), case("b", "failed", reasons=["the button moved"])]),
        )
        self.assertEqual([item.case_id for item in found.broke], ["b"])
        self.assertIn("the button moved", found.broke[0].detail)
        self.assertEqual(found.fixed, [])

    def test_a_check_that_was_fixed_is_said_so(self) -> None:
        found = comparison.compare(
            run("one", [case("a", "failed")]),
            run("two", [case("a", "passed")]),
        )
        self.assertEqual([item.case_id for item in found.fixed], ["a"])

    def test_a_check_that_was_already_failing_is_not_news(self) -> None:
        found = comparison.compare(
            run("one", [case("a", "failed")]),
            run("two", [case("a", "failed")]),
        )
        self.assertEqual(found.broke, [])
        self.assertEqual([item.case_id for item in found.still_failing], ["a"])
        self.assertFalse(found.anything_changed)

    def test_a_flaky_check_counts_as_failing(self) -> None:
        found = comparison.compare(
            run("one", [case("a", "passed")]),
            run("two", [case("a", "flaky")]),
        )
        self.assertEqual([item.case_id for item in found.broke], ["a"])

    def test_a_new_check_and_a_removed_one_are_both_reported(self) -> None:
        found = comparison.compare(
            run("one", [case("a", "passed"), case("gone", "passed")]),
            run("two", [case("a", "passed"), case("fresh", "passed")]),
        )
        self.assertEqual([item.case_id for item in found.added], ["fresh"])
        self.assertEqual([item.case_id for item in found.gone], ["gone"])

    def test_a_check_that_got_much_slower_is_reported(self) -> None:
        found = comparison.compare(
            run("one", [case("a", "passed", duration_ms=400)]),
            run("two", [case("a", "passed", duration_ms=3000)]),
        )
        self.assertEqual([item.case_id for item in found.slower], ["a"])
        self.assertIn("3000 ms", found.slower[0].detail)

    def test_a_small_wobble_in_time_is_not_reported(self) -> None:
        for was, now in ((400, 700), (10, 30), (1000, 1400)):
            with self.subTest(was=was, now=now):
                found = comparison.compare(
                    run("one", [case("a", "passed", duration_ms=was)]),
                    run("two", [case("a", "passed", duration_ms=now)]),
                )
                self.assertEqual(found.slower, [])

    def test_nothing_changed_says_so_in_one_line(self) -> None:
        same = [case("a", "passed"), case("b", "passed")]
        found = comparison.compare(run("one", same), run("two", copy.deepcopy(same)))
        self.assertFalse(found.anything_changed)
        self.assertEqual(found.lines(), ["Nothing changed since the run before."])

    def test_the_lines_put_the_worst_news_first(self) -> None:
        found = comparison.compare(
            run("one", [case("a", "passed"), case("b", "failed"), case("old", "passed")]),
            run("two", [case("a", "failed"), case("b", "passed"), case("new", "passed")]),
        )
        lines = found.lines()
        self.assertTrue(lines[0].startswith("Started failing"))
        self.assertLess(lines.index("Fixed:"), lines.index("New:"))

    def test_a_report_that_is_not_a_run_is_refused(self) -> None:
        for value in ({}, {"cases": "nothing"}, {"cases": 5}):
            with self.subTest(value=value), self.assertRaises(HarnessError):
                comparison.compare(value, run("two", []))

    def test_a_case_with_no_name_is_skipped_rather_than_crashing(self) -> None:
        found = comparison.compare(
            run("one", [{"kind": "command", "status": "passed"}]),
            run("two", [case("a", "passed")]),
        )
        self.assertEqual([item.case_id for item in found.added], ["a"])


class ReadingRunsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.addCleanup(self.temporary.cleanup)
        self.base = self.root / ".harness" / "qa" / "runs"
        self.base.mkdir(parents=True)

    def keep(self, name: str, cases: list[dict]) -> None:
        folder = self.base / name
        folder.mkdir()
        (folder / "result.json").write_text(json.dumps(run(name, cases)), encoding="utf-8")

    def test_the_last_two_runs_are_found_oldest_first(self) -> None:
        self.keep("20260101-000001", [case("a", "passed")])
        self.keep("20260101-000002", [case("a", "failed")])
        self.keep("20260101-000003", [case("a", "passed")])
        before, after = comparison.last_two(self.config)
        self.assertEqual(before["run_id"], "20260101-000002")
        self.assertEqual(after["run_id"], "20260101-000003")

    def test_one_run_alone_says_what_to_do(self) -> None:
        self.keep("20260101-000001", [case("a", "passed")])
        with self.assertRaises(HarnessError) as caught:
            comparison.last_two(self.config)
        self.assertIn("Run the checks again", str(caught.exception))

    def test_a_folder_with_no_report_in_it_is_skipped(self) -> None:
        self.keep("20260101-000001", [case("a", "passed")])
        (self.base / "20260101-000002").mkdir()
        self.keep("20260101-000003", [case("a", "failed")])
        before, after = comparison.last_two(self.config)
        self.assertEqual(before["run_id"], "20260101-000001")
        self.assertEqual(after["run_id"], "20260101-000003")

    def test_a_broken_report_is_refused_with_a_sentence(self) -> None:
        folder = self.base / "20260101-000001"
        folder.mkdir()
        (folder / "result.json").write_text("{ not json", encoding="utf-8")
        with self.assertRaises(HarnessError):
            comparison.read_report(folder)




class CommandAndPanelTests(unittest.TestCase):
    """Saying what changed, from the command line and from the panel."""

    def test_the_command_says_what_moved(self) -> None:
        import os
        import subprocess
        import sys

        source = str(Path(__file__).resolve().parents[1] / "src")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / ".harness" / "qa" / "runs").mkdir(parents=True)
            (root / ".harness" / "qa" / "suite.json").write_text(
                json.dumps({"schema_version": 1, "name": "d", "cases": []}), encoding="utf-8"
            )
            for name, status in (("20260101-000001", "passed"), ("20260101-000002", "failed")):
                folder = root / ".harness" / "qa" / "runs" / name
                folder.mkdir()
                (folder / "result.json").write_text(
                    json.dumps(run(name, [case("sign-in", status)])), encoding="utf-8"
                )
            finished = subprocess.run(
                [sys.executable, "-m", "our_harness", "--project", str(root), "qa", "changed"],
                capture_output=True, text=True, timeout=180,
                env={**os.environ, "PYTHONPATH": source},
            )
            self.assertEqual(finished.returncode, 1, finished.stdout + finished.stderr)
            self.assertIn("Started failing", finished.stdout)
            self.assertIn("sign-in", finished.stdout)

    def test_the_command_says_when_there_is_only_one_run(self) -> None:
        import os
        import subprocess
        import sys

        source = str(Path(__file__).resolve().parents[1] / "src")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / ".harness" / "qa" / "runs").mkdir(parents=True)
            finished = subprocess.run(
                [sys.executable, "-m", "our_harness", "--project", str(root), "qa", "changed"],
                capture_output=True, text=True, timeout=180,
                env={**os.environ, "PYTHONPATH": source},
            )
            self.assertEqual(finished.returncode, 2)
            self.assertIn("Run the checks again", finished.stderr)
            self.assertNotIn("Traceback", finished.stderr)

    def test_the_panel_answers_with_the_same_thing(self) -> None:
        import http.client
        import threading

        from our_harness.server import HarnessHTTPServer

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            runs = root / ".harness" / "qa" / "runs"
            runs.mkdir(parents=True)
            for name, status in (("20260101-000001", "passed"), ("20260101-000002", "failed")):
                folder = runs / name
                folder.mkdir()
                (folder / "result.json").write_text(
                    json.dumps(run(name, [case("sign-in", status)])), encoding="utf-8"
                )
            data = copy.deepcopy(DEFAULT_CONFIG)
            data["ui"].update({"host": "127.0.0.1", "port": 0, "open_browser": False})
            server = HarnessHTTPServer(("127.0.0.1", 0), LoadedConfig(data, root, [], {}))
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
            )
            thread.start()
            try:
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=15)
                connection.request(
                    "GET", "/api/qa/changed", None,
                    {"Host": f"127.0.0.1:{server.server_port}", "X-Harness-Token": server.token},
                )
                answer = connection.getresponse()
                body = json.loads(answer.read())
                connection.close()
                self.assertEqual(answer.status, 200)
                self.assertEqual([item["case_id"] for item in body["broke"]], ["sign-in"])
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()


class JudgeFindingsTests(unittest.TestCase):
    """Every defect an independent reader reproduced, kept as its own test."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.base = self.root / ".harness" / "qa" / "runs"
        self.base.mkdir(parents=True)
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})

    def keep(self, name: str, cases: list[dict]) -> None:
        folder = self.base / name
        folder.mkdir()
        (folder / "result.json").write_text(json.dumps(run(name, cases)), encoding="utf-8")

    def test_a_run_name_cannot_reach_outside_the_project(self) -> None:
        # This read any file on the machine through the command line once.
        outside = self.root.parent / f"somewhere-else-{self.root.name}"
        outside.mkdir(exist_ok=True)
        self.addCleanup(lambda: outside.rmdir() if outside.is_dir() else None)
        (outside / "result.json").write_text(
            json.dumps(run("x", [case("leaked", "passed")])), encoding="utf-8"
        )
        self.addCleanup(lambda: (outside / "result.json").unlink(missing_ok=True))
        for name in (
            f"../../{outside.name}",
            f"..\..\{outside.name}",
            f"../{outside.name}",
            "/etc",
            "a/b",
        ):
            with self.subTest(name=name), self.assertRaises(HarnessError):
                comparison.kept_run_folder(self.config, name)

    def test_a_run_name_that_is_a_name_still_works(self) -> None:
        self.keep("20260101-000001", [case("a", "passed")])
        folder = comparison.kept_run_folder(self.config, "20260101-000001")
        self.assertEqual(comparison.read_report(folder)["run_id"], "20260101-000001")

    def test_a_run_folder_that_is_not_there_says_so_rather_than_reading_something_else(self) -> None:
        with self.assertRaises(HarnessError):
            comparison.read_report(comparison.kept_run_folder(self.config, "20991231-235959"))

    def test_the_command_refuses_a_name_that_climbs_out(self) -> None:
        import os
        import subprocess
        import sys

        outside = self.root.parent / f"somewhere-else-{self.root.name}"
        outside.mkdir(exist_ok=True)
        self.addCleanup(lambda: outside.rmdir() if outside.is_dir() else None)
        (outside / "result.json").write_text(
            json.dumps(run("x", [case("LEAKED-FILE-CONTENT-MARKER", "passed")])), encoding="utf-8"
        )
        self.addCleanup(lambda: (outside / "result.json").unlink(missing_ok=True))
        self.keep("20260101-000001", [case("a", "passed")])
        source = str(Path(__file__).resolve().parents[1] / "src")
        finished = subprocess.run(
            [
                sys.executable, "-m", "our_harness", "--project", str(self.root),
                "qa", "changed", "--before", "20260101-000001",
                f"--after", f"../{outside.name}", "--json",
            ],
            capture_output=True, text=True, timeout=180,
            env={**os.environ, "PYTHONPATH": source},
        )
        self.assertNotEqual(finished.returncode, 0)
        self.assertNotIn("LEAKED-FILE-CONTENT-MARKER", finished.stdout)
        self.assertNotIn("Traceback", finished.stderr)

    def test_a_check_that_went_from_no_time_to_seconds_is_the_loudest_news(self) -> None:
        found = comparison.compare(
            run("one", [case("a", "passed", duration_ms=0)]),
            run("two", [case("a", "passed", duration_ms=15000)]),
        )
        self.assertEqual([item.case_id for item in found.slower], ["a"])
        self.assertTrue(found.anything_changed)

    def test_a_check_that_stayed_quick_from_no_time_is_not_news(self) -> None:
        found = comparison.compare(
            run("one", [case("a", "passed", duration_ms=0)]),
            run("two", [case("a", "passed", duration_ms=40)]),
        )
        self.assertEqual(found.slower, [])

    def test_a_time_written_as_words_does_not_stop_the_comparison(self) -> None:
        for odd in ("oops", None, [], {}, True, "12ms"):
            with self.subTest(odd=odd):
                found = comparison.compare(
                    {"cases": [{"id": "c1", "status": "passed", "duration_ms": odd}]},
                    {"cases": [{"id": "c1", "status": "passed", "duration_ms": 500}]},
                )
                self.assertEqual(found.broke, [])

    def test_a_time_written_as_a_decimal_is_still_read(self) -> None:
        found = comparison.compare(
            run("one", [case("a", "passed", duration_ms=100)]),
            {"cases": [{"id": "a", "status": "passed", "duration_ms": 4000.5}]},
        )
        self.assertEqual([item.case_id for item in found.slower], ["a"])

    def test_no_runs_at_all_does_not_claim_there_is_one(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            comparison.last_two(self.config)
        said = str(caught.exception)
        self.assertIn("no kept runs yet", said)
        self.assertNotIn("only one", said)

    def test_one_run_still_says_one(self) -> None:
        self.keep("20260101-000001", [case("a", "passed")])
        with self.assertRaises(HarnessError) as caught:
            comparison.last_two(self.config)
        self.assertIn("only one kept run", str(caught.exception))


class NothingItSaysCarriesACredentialTests(unittest.TestCase):
    """Why a check failed is a program's own output, keys and all."""

    def leaky(self) -> tuple[dict, dict]:
        return (
            {"run_id": "r1", "cases": [
                {"id": "c1", "title": "x", "kind": "command", "status": "passed", "duration_ms": 5}
            ]},
            {"run_id": "r2", "cases": [{
                "id": "c1", "title": "x", "kind": "command", "status": "failed", "duration_ms": 5,
                "reasons": ["connecting with api_key=sk-live-abcdefghijklmno failed with 401"],
            }]},
        )

    def test_the_answer_a_program_reads_carries_no_credential(self) -> None:
        before, after = self.leaky()
        said = json.dumps(comparison.compare(before, after).to_dict())
        self.assertNotIn("sk-live-abcdefghijklmno", said)
        self.assertIn("[REDACTED]", said)

    def test_the_lines_a_person_reads_carry_no_credential(self) -> None:
        before, after = self.leaky()
        printed = " ".join(comparison.compare(before, after).lines())
        self.assertNotIn("sk-live-abcdefghijklmno", printed)
        self.assertIn("401", printed)

    def test_a_credential_in_a_name_or_a_title_is_hidden_too(self) -> None:
        found = comparison.compare(
            {"run_id": "r1", "cases": []},
            {"run_id": "r2", "cases": [{
                "id": "api-key-sk-live-abcdefghijklmno",
                "title": 'the check for password="hunter2hunter2"',
                "kind": "command", "status": "failed", "duration_ms": 1, "reasons": ["no"],
            }]},
        )
        said = json.dumps(found.to_dict()) + " ".join(found.lines())
        self.assertNotIn("sk-live-abcdefghijklmno", said)
        self.assertNotIn("hunter2hunter2", said)

    def test_a_credential_in_the_name_of_a_run_is_hidden_too(self) -> None:
        found = comparison.compare(
            {"run_id": "token=abcdefghijklmnop", "cases": []},
            {"run_id": "r2", "cases": []},
        )
        self.assertNotIn("abcdefghijklmnop", json.dumps(found.to_dict()))

    def test_a_caller_that_forgets_gets_one_anyway(self) -> None:
        # There is no way to ask for the unhidden version, on purpose.
        before, after = self.leaky()
        self.assertNotIn(
            "sk-live-abcdefghijklmno",
            json.dumps(comparison.compare(before, after, redactor=None).to_dict()),
        )

    def test_ordinary_reasons_are_left_exactly_as_they_were(self) -> None:
        found = comparison.compare(
            {"run_id": "r1", "cases": [case("a", "passed")]},
            {"run_id": "r2", "cases": [case("a", "failed", reasons=["the button moved"])]},
        )
        self.assertIn("the button moved", found.broke[0].detail)
        self.assertNotIn("[REDACTED]", found.broke[0].detail)
