from __future__ import annotations

import copy
import json
import unittest

from our_harness.bounded_file_read import (
    READ_FILE_INPUT_SCHEMA, read_file_page, validate_read_file_arguments,
)
from our_harness.models import HarnessError
from our_harness.providers.base import _strict_output_schema


class BoundedFileReadTests(unittest.TestCase):
    def args(self, **overrides):
        return {
            "path": "arbitrary-user/project/source.txt", "start_line": 1,
            "end_line": 100, "max_bytes": 32_000, **overrides,
        }

    def page(self, text, arguments=None, *, output_limit=1024, configured_output_limit=1024, max_file_bytes=1_000_000):
        return read_file_page(
            text if isinstance(text, bytes) else text.encode("utf-8"),
            arguments or self.args(), output_limit=output_limit,
            configured_output_limit=configured_output_limit, max_file_bytes=max_file_bytes,
        )

    def assertFits(self, page, limit=1024):
        encoded = json.dumps(page, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(encoded), limit)
        self.assertEqual(json.loads(encoded), page)

    def test_schema_valid_large_request_is_safely_reduced(self):
        result = self.page("large source content " * 2000, self.args(max_bytes=10**12))
        self.assertTrue(result["truncated"])
        self.assertTrue(result["content"])
        self.assertTrue(result["next_cursor"])
        self.assertEqual(result["start_line"], 1)
        self.assertEqual(result["end_line"], 1)
        self.assertFits(result)

    def test_pages_reconstruct_escaped_utf8_long_lines_exactly(self):
        text = ('\\"\t\x00\U0001f680caf\u00e9\r\n' * 30) + ('\U0001f680\\"' * 600) + "\nlast\u2028after"
        args = self.args(max_bytes=100_000)
        content = []
        offset = 0
        pages = 0
        while True:
            page = self.page(text, args)
            self.assertFits(page)
            self.assertEqual(page["byte_offset"], offset)
            self.assertEqual(page["next_byte_offset"], offset + len(page["content"].encode("utf-8")))
            content.append(page["content"])
            offset = page["next_byte_offset"]
            pages += 1
            if not page["next_cursor"]:
                self.assertFalse(page["truncated"])
                break
            self.assertTrue(page["content"])
            args["cursor"] = page["next_cursor"]
            self.assertLess(pages, 100)
        self.assertGreater(pages, 1)
        self.assertEqual("".join(content), text)

    def test_line_range_continuation_reports_actual_page_lines(self):
        text = "skip\n" + "middle " * 600 + "\nend\nskip again\n"
        args = self.args(start_line=2, end_line=3, max_bytes=200)
        first = self.page(text, args)
        self.assertEqual((first["start_line"], first["end_line"]), (2, 2))
        self.assertEqual((first["requested_start_line"], first["requested_end_line"]), (2, 3))
        parts = [first["content"]]
        while first["next_cursor"]:
            args["cursor"] = first["next_cursor"]
            first = self.page(text, args)
            parts.append(first["content"])
        self.assertEqual(first["end_line"], 3)
        self.assertEqual("".join(parts), "middle " * 600 + "\nend\n")

    def test_saved_cursor_survives_restart_and_declining_remaining_budget(self):
        text = "a complete long line " * 200
        args = self.args()
        first = self.page(text, args, output_limit=2048, configured_output_limit=2048)
        persisted_args = json.loads(json.dumps({**args, "cursor": first["next_cursor"]}))
        second = self.page(text, persisted_args, output_limit=1024, configured_output_limit=2048)
        self.assertEqual(first["next_byte_offset"], second["byte_offset"])
        self.assertEqual(first["contract_fingerprint_sha256"], second["contract_fingerprint_sha256"])
        self.assertFits(second)

    def test_source_path_range_and_configuration_changes_invalidate_cursor(self):
        text = "source data " * 1000
        args = self.args()
        first = self.page(text, args)
        resumed = {**args, "cursor": first["next_cursor"]}
        cases = [
            (text + "changed", resumed, {}),
            (text, {**resumed, "path": "different-root/source.txt"}, {}),
            (text, {**resumed, "end_line": 99}, {}),
            (text, resumed, {"configured_output_limit": 2048}),
            (text, resumed, {"max_file_bytes": 2_000_000}),
        ]
        for source, arguments, config in cases:
            with self.subTest(arguments=arguments, config=config):
                with self.assertRaisesRegex(HarnessError, "invalid or stale"):
                    self.page(source, arguments, **config)

    def test_invalid_arguments_and_binary_text_remain_correctable_errors(self):
        for value in (0, -1, True, 1.5, "32000", None):
            with self.subTest(max_bytes=value):
                with self.assertRaisesRegex(HarnessError, "positive integer"):
                    self.page("source", self.args(max_bytes=value))
        for args in (self.args(start_line=0), self.args(start_line=True), self.args(end_line=-2), self.args(start_line=4, end_line=3)):
            with self.assertRaises(HarnessError):
                self.page("source", args)
        with self.assertRaisesRegex(HarnessError, "missing fields"):
            validate_read_file_arguments({"path": "source.txt", "start_line": 1})
        with self.assertRaisesRegex(HarnessError, "unknown fields"):
            self.page("source", self.args(extra="field"))
        with self.assertRaisesRegex(HarnessError, "not valid UTF-8"):
            self.page(b"\xff\xfe")
        with self.assertRaisesRegex(HarnessError, "max_file_bytes"):
            self.page("too big", max_file_bytes=2)

    def test_tiny_budget_fails_explicitly_without_partial_json_or_empty_progress(self):
        for budget in (0, 1, 2, 100, 400):
            with self.subTest(budget=budget):
                with self.assertRaisesRegex(HarnessError, "insufficient output allowance"):
                    self.page("source data " * 100, output_limit=budget)
        with self.assertRaisesRegex(HarnessError, "next UTF-8 character"):
            self.page("\U0001f680next", self.args(max_bytes=1))

    def test_empty_and_past_end_reads_return_complete_json(self):
        for text, args in (("", self.args()), ("one\n", self.args(start_line=20, end_line=30))):
            result = self.page(text, args)
            self.assertEqual(result["content"], "")
            self.assertFalse(result["truncated"])
            self.assertIsNone(result["next_cursor"])
            self.assertFits(result)

    def test_forged_cursor_offsets_cannot_split_utf8_or_skip_past_file(self):
        text = "\U0001f680" * 1000
        first = self.page(text)
        prefix = first["next_cursor"].rsplit(".", 1)[0] + "."
        for suffix in ("-1", "1.5", "99999999", "1", "\uff11", "9" * 100):
            with self.subTest(suffix=suffix):
                with self.assertRaises(HarnessError):
                    self.page(text, self.args(cursor=prefix + suffix))

    def test_optional_cursor_schema_is_closed_by_strict_adapters_without_mutation(self):
        original = copy.deepcopy(READ_FILE_INPUT_SCHEMA)
        strict = _strict_output_schema(READ_FILE_INPUT_SCHEMA)
        self.assertEqual(READ_FILE_INPUT_SCHEMA, original)
        self.assertNotIn("cursor", original["required"])
        self.assertIn("cursor", strict["required"])
        self.assertEqual(strict["properties"]["cursor"]["type"], ["string", "null"])
        self.assertFalse(strict["additionalProperties"])
        for cursor in (None, ""):
            self.assertEqual(self.page("source", self.args(cursor=cursor))["content"], "source")


if __name__ == "__main__":
    unittest.main()
