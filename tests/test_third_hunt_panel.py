"""Bugs an independent reader found in the control panel.

The worst of these replaced somebody's checks with a single new one and
answered that it had worked.
"""

from __future__ import annotations

import copy
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from our_harness import bundle, comparison, coverage
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError
from our_harness.server import HarnessHTTPServer


class PanelTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness" / "qa").mkdir(parents=True)
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["ui"].update({"host": "127.0.0.1", "port": 0, "open_browser": False})
        self.server = HarnessHTTPServer(("127.0.0.1", 0), LoadedConfig(data, self.root, [], {}))
        threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        ).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def ask(self, how: str, path: str, body: dict | None = None) -> tuple[int, str]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=20)
        try:
            connection.request(
                how, path, json.dumps(body) if body is not None else None,
                {"Host": f"127.0.0.1:{self.server.server_port}",
                 "Content-Type": "application/json",
                 "X-Harness-Token": self.server.token},
            )
            answer = connection.getresponse()
            return answer.status, answer.read().decode("utf-8")
        finally:
            connection.close()


class SuiteIsNeverReplacedTests(PanelTestCase):
    """A suite that cannot be read is not an empty suite."""

    def broken_suite(self) -> str:
        spot = self.root / ".harness" / "qa" / "suite.json"
        spot.write_text(json.dumps({
            "schema_version": 1, "name": "mysuite",
            "cases": [
                {"id": "tests", "title": "Project tests pass", "kind": "command",
                 "command": ["python", "-m", "pytest"], "expect": {"exit_code": 0}},
                {"id": "sso", "title": "sso", "kind": "grpc", "expect": {}},
            ],
        }), encoding="utf-8")
        return spot.read_text(encoding="utf-8")

    def test_adding_a_ready_made_check_does_not_wipe_the_others(self) -> None:
        before = self.broken_suite()
        status, body = self.ask("POST", "/api/qa/add", {
            "starter": "page-opens", "url": "http://127.0.0.1:8765/", "name": "newcheck"})
        self.assertEqual(status, 400, body)
        self.assertEqual(
            (self.root / ".harness" / "qa" / "suite.json").read_text(encoding="utf-8"), before
        )

    def test_writing_a_check_for_a_page_does_not_wipe_the_others(self) -> None:
        before = self.broken_suite()
        status, body = self.ask(
            "POST", "/api/qa/coverage/add", {"addresses": ["http://127.0.0.1:8765/settings"]}
        )
        self.assertEqual(status, 400, body)
        self.assertEqual(
            (self.root / ".harness" / "qa" / "suite.json").read_text(encoding="utf-8"), before
        )

    def test_a_project_with_no_suite_at_all_still_works(self) -> None:
        status, body = self.ask("POST", "/api/qa/coverage/add",
                                {"addresses": ["http://127.0.0.1:8765/settings"]})
        self.assertEqual(status, 200, body)
        self.assertIn("settings-opens", body)


class ReadingAnswersPlainlyTests(PanelTestCase):
    def test_asking_for_a_workflow_that_is_not_there_gets_a_sentence(self) -> None:
        # This dropped the connection and printed a stack trace on the console.
        for name in ("gone", "con", "..%2F..%2F.."):
            with self.subTest(name=name):
                status, body = self.ask("GET", f"/api/workflows?name={name}")
                self.assertEqual(status, 400, body)
                self.assertIn("error", body)

    def test_a_number_too_large_to_write_down_is_a_plain_refusal(self) -> None:
        status, body = self.ask("POST", "/api/bundle", {"runs": 1e999, "parts": ["machine"]})
        self.assertEqual(status, 400, body)
        self.assertIn("whole number", body)


class ReasonsFromAFileTests(unittest.TestCase):
    """A run report is a file on disk and can hold anything."""

    def compare(self, reasons: object) -> comparison.Comparison:
        return comparison.compare(
            {"run_id": "r1", "cases": [{"id": "c", "status": "passed", "duration_ms": 1}]},
            {"run_id": "r2", "cases": [
                {"id": "c", "status": "failed", "duration_ms": 1, "reasons": reasons}]},
        )

    def test_a_reason_that_is_not_a_list_does_not_stop_the_comparison(self) -> None:
        for odd in (7, {"why": "x"}, None, True):
            with self.subTest(odd=odd):
                self.assertIn("no reason given", self.compare(odd).broke[0].detail)

    def test_a_reason_written_as_one_piece_of_text_is_not_read_letter_by_letter(self) -> None:
        # It used to show the first character and present it as the reason.
        self.assertIn("no reason given", self.compare("boom").broke[0].detail)

    def test_an_ordinary_reason_still_comes_through(self) -> None:
        self.assertIn("the button moved", self.compare(["the button moved"]).broke[0].detail)

    def test_an_empty_entry_is_stepped_over(self) -> None:
        self.assertIn("the button moved", self.compare([None, "", "the button moved"]).broke[0].detail)


class PageNamesTests(unittest.TestCase):
    def test_a_page_with_a_name_in_another_language_gets_a_usable_check(self) -> None:
        from our_harness import qa as qalab
        from our_harness import starters

        for address in ("http://127.0.0.1:8765/café", "http://127.0.0.1:8765/説明",
                        "http://127.0.0.1:8765/settings"):
            with self.subTest(address=address):
                built = starters.build(
                    "page-opens", url=address, case_id=coverage.name_for(address)
                )
                qalab.parse_suite({"schema_version": 1, "name": "d", "cases": [built]})


class BundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})

    def test_two_bundles_in_the_same_second_both_get_a_name(self) -> None:
        first = bundle.output_path(self.config)
        first.parent.mkdir(parents=True, exist_ok=True)
        first.write_bytes(b"one")
        second = bundle.output_path(self.config)
        self.assertNotEqual(first, second)
        self.assertFalse(second.exists())

    def test_a_file_too_big_to_send_is_not_read_into_memory_first(self) -> None:
        import tracemalloc

        big = self.root / "trace.zip"
        big.write_bytes(b"0" * (bundle.MAX_FILE_BYTES + 5_000_000))
        import zipfile

        spot = self.root / "out.zip"
        tracemalloc.start()
        with zipfile.ZipFile(spot, "w") as archive:
            from our_harness.redaction import CredentialRedactor

            writer = bundle._Writer(archive, CredentialRedactor(None))
            writer.file("runs/trace.zip", big)
        used = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        self.assertTrue(any("trace.zip" in note for note in writer.left_out))
        self.assertLess(used, bundle.MAX_FILE_BYTES, "it should not read what it will not keep")


if __name__ == "__main__":
    unittest.main()
