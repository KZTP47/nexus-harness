"""Bugs an independent reader found in the check runner and the reports.

The worst kind here is a check that passes when it should fail, because a tool
that says yes wrongly is worse than one that says nothing at all.
"""

from __future__ import annotations

import copy
import tempfile
import unittest
import xml.etree.ElementTree as ElementTree
import zlib
from pathlib import Path

from our_harness import contracts, images
from our_harness import qa as qalab
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError


def one_run(**changes) -> qalab.QaRunResult:
    case = qalab.QaCaseResult(
        id=changes.pop("id", "a"),
        title=changes.pop("title", "A check"),
        kind="file",
        status=changes.pop("status", "failed"),
        duration_ms=1,
        reasons=changes.pop("reasons", ("it did not work",)),
    )
    return qalab.QaRunResult(
        run_id="r", suite_name="mine", started_at="now", duration_ms=1, workers=1, cases=(case,)
    )


class CheckPassesWronglyTests(unittest.TestCase):
    def test_true_is_not_the_number_one(self) -> None:
        # Python says True equals 1. JSON does not, and a check that accepted
        # one for the other reported success while the answer had changed.
        self.assertTrue(qalab._json_reasons('{"ok": true}', (("ok", 1),)))
        self.assertTrue(qalab._json_reasons('{"ok": false}', (("ok", 0),)))
        self.assertTrue(qalab._json_reasons('{"n": 1}', (("n", True),)))

    def test_the_same_value_still_passes(self) -> None:
        self.assertEqual(qalab._json_reasons('{"ok": true}', (("ok", True),)), [])
        self.assertEqual(qalab._json_reasons('{"n": 1}', (("n", 1),)), [])
        self.assertEqual(qalab._json_reasons('{"s": "x"}', (("s", "x"),)), [])

    def test_a_list_is_compared_item_by_item(self) -> None:
        self.assertEqual(qalab._json_reasons('{"a": [1, true]}', (("a", [1, True]),)), [])
        self.assertTrue(qalab._json_reasons('{"a": [1, 1]}', (("a", [1, True]),)))

    def test_a_contract_that_points_at_itself_says_when_it_gave_up(self) -> None:
        # The answer decided how deep the checking went, so a deep enough
        # answer was never checked and the case passed with nothing looked at.
        schema = {
            "$ref": "#/$defs/node",
            "$defs": {"node": {"type": "object", "properties": {
                "next": {"$ref": "#/$defs/node"}, "leaf": {"type": "string"}}}},
        }

        def nested(depth: int) -> dict:
            value: dict = {"leaf": 12345}
            for _ in range(depth):
                value = {"next": value}
            return value

        self.assertTrue(contracts.problems(nested(14), schema))
        said = contracts.problems(nested(40), schema)
        self.assertTrue(said, "giving up quietly is the one thing it must not do")
        self.assertIn("deep", said[0])

    def test_a_pointer_at_something_that_is_not_a_rule_is_refused(self) -> None:
        schema = {"$defs": {"n": {"type": "string"}}, "$ref": "#/$defs/n/type"}
        with self.assertRaises(HarnessError) as caught:
            contracts.check_schema(schema)
        self.assertIn("not a rule", str(caught.exception))

    def test_an_ordinary_pointer_still_works(self) -> None:
        schema = {"$defs": {"n": {"type": "string"}}, "$ref": "#/$defs/n"}
        contracts.check_schema(schema)
        self.assertTrue(contracts.matches("hello", schema))
        self.assertFalse(contracts.matches(5, schema))


class ReportsPeopleReadTests(unittest.TestCase):
    def test_the_junit_report_still_parses_with_awkward_characters(self) -> None:
        run = one_run(title="report ￾ title", reasons=("a reason with ￿ and \ud800",))
        written = qalab.render_report(run, "junit")
        ElementTree.fromstring(written)
        written.encode("utf-8")

    def test_a_reason_with_a_line_break_stays_in_its_row(self) -> None:
        run = one_run(reasons=("docs/one\ntwo.md was not found",))
        rows = [line for line in qalab.render_report(run, "markdown").splitlines()
                if line.startswith("| ")]
        # A heading row, a marker row, and one row for the one check.
        self.assertEqual(len(rows), 3, rows)
        self.assertIn("docs/one two.md", rows[2])

    def test_an_upright_bar_still_becomes_a_slash(self) -> None:
        run = one_run(reasons=("a | b",))
        rows = [line for line in qalab.render_report(run, "markdown").splitlines()
                if line.startswith("| ")]
        self.assertEqual(len(rows), 3)


class PictureTests(unittest.TestCase):
    def test_a_small_file_cannot_ask_for_all_the_memory(self) -> None:
        def chunk(kind: bytes, body: bytes) -> bytes:
            return (len(body).to_bytes(4, "big") + kind + body
                    + (zlib.crc32(kind + body) & 0xFFFFFFFF).to_bytes(4, "big"))

        header = (1).to_bytes(4, "big") + (1).to_bytes(4, "big") + bytes((8, 6, 0, 0, 0))
        bomb = (images.SIGNATURE + chunk(b"IHDR", header)
                + chunk(b"IDAT", zlib.compress(b"\x00" * (64 * 1024 * 1024), 9))
                + chunk(b"IEND", b""))
        self.assertLess(len(bomb), 200_000, "the file itself is small")
        with self.assertRaises(HarnessError) as caught:
            images.read_png(bomb)
        self.assertIn("unpacks to more", str(caught.exception))

    def test_an_ordinary_picture_still_reads(self) -> None:
        made = images.write_png(images.Image(3, 2, bytes([255, 0, 0, 255] * 6)))
        back = images.read_png(made)
        self.assertEqual((back.width, back.height), (3, 2))


class NumberTests(unittest.TestCase):
    def test_a_number_too_big_for_a_computer_does_not_stop_the_check(self) -> None:
        rule = {"type": "object", "properties": {"n": {"multipleOf": 2}}}
        self.assertEqual(contracts.problems({"n": 10 ** 400}, rule), ())
        self.assertTrue(contracts.problems({"n": 10 ** 400 + 1}, rule))

    def test_a_step_too_small_to_divide_by_is_said_plainly(self) -> None:
        said = contracts.problems(1e308, {"type": "number", "multipleOf": 1e-10})
        self.assertTrue(said)
        self.assertIn("cannot be checked", said[0])

    def test_ordinary_multiples_still_work(self) -> None:
        rule = {"type": "number", "multipleOf": 5}
        self.assertEqual(contracts.problems(10, rule), ())
        self.assertTrue(contracts.problems(11, rule))


class TidyingUpOldRunsTests(unittest.TestCase):
    def test_only_folders_this_tool_made_are_removed(self) -> None:
        # Pointed at a folder holding somebody's own work, it deleted the work.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "docs" / "important-notes").mkdir(parents=True)
            (root / "docs" / "important-notes" / "notes.md").write_text("work", encoding="utf-8")
            for name in ("20260101-000001", "20260101-000002", "20260101-000003"):
                (root / "docs" / name).mkdir()
                (root / "docs" / name / "result.json").write_text("{}", encoding="utf-8")
            data = copy.deepcopy(DEFAULT_CONFIG)
            data["qa"]["artifacts_dir"] = "docs"
            data["qa"]["keep_runs"] = 1
            qalab.QaRunner(LoadedConfig(data, root, [], {}))._trim_runs()
            self.assertTrue((root / "docs" / "important-notes" / "notes.md").is_file())
            self.assertTrue((root / "docs" / "20260101-000003").is_dir())
            self.assertFalse((root / "docs" / "20260101-000001").exists())


if __name__ == "__main__":
    unittest.main()
