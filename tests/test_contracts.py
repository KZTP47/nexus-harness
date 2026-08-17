"""Checking that an answer has the shape the contract promised."""

from __future__ import annotations

import unittest

from our_harness import contracts
from our_harness.models import HarnessError


class SchemaReadingTests(unittest.TestCase):
    def test_a_word_this_tool_cannot_enforce_is_refused(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            contracts.check_schema({"type": "object", "patternProperties": {"^x": {}}})
        message = str(caught.exception)
        self.assertIn("patternProperties", message)
        self.assertIn("would pass while that rule was ignored", message)

    def test_a_reference_to_the_web_is_refused(self) -> None:
        for reference in ("https://example.com/schema.json", "other.json#/thing", "#thing"):
            with self.subTest(reference=reference), self.assertRaises(HarnessError):
                contracts.check_schema({"$ref": reference})

    def test_a_reference_inside_the_same_file_is_allowed(self) -> None:
        contracts.check_schema({"$defs": {"price": {"type": "number"}}, "$ref": "#/$defs/price"})

    def test_a_type_that_does_not_exist_is_refused(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            contracts.check_schema({"type": "text"})
        self.assertIn("must be one of", str(caught.exception))

    def test_broken_rules_are_refused_with_the_place_they_are_in(self) -> None:
        for schema in (
            {"properties": {"a": {"type": "nonsense"}}},
            {"required": "name"},
            {"minLength": -1},
            {"multipleOf": 0},
            {"pattern": "["},
            {"pattern": "x" * 400},
            {"format": "credit-card"},
            {"anyOf": []},
            {"items": 5},
            {"uniqueItems": "yes"},
        ):
            with self.subTest(schema=str(schema)[:40]), self.assertRaises(HarnessError):
                contracts.check_schema(schema)

    def test_a_pattern_that_could_run_forever_is_refused(self) -> None:
        # A repeat inside a repeat can take longer than the age of the universe
        # on the wrong text, and Python cannot interrupt it, so the whole run
        # would sit there doing nothing.
        for pattern in ("(a+)+$", "^(a|aa)+$", "(x*)*y", "^(ab{1,}){2,}$"):
            with self.subTest(pattern=pattern), self.assertRaises(HarnessError) as caught:
                contracts.check_schema({"type": "string", "pattern": pattern})
            self.assertIn("repeat inside a repeat", str(caught.exception))

    def test_ordinary_patterns_are_still_allowed(self) -> None:
        for pattern in ("^[a-z]+$", r"^\d{4}-\d{2}$", "^(cat|dog)$", "^a+b*$"):
            with self.subTest(pattern=pattern):
                contracts.check_schema({"type": "string", "pattern": pattern})

    def test_text_too_long_to_test_is_reported_rather_than_tested(self) -> None:
        schema = {"type": "string", "pattern": "^[a-z]+$"}
        huge = "a" * (contracts.MAX_PATTERN_INPUT + 1)
        found = contracts.problems(huge, schema)
        self.assertEqual(len(found), 1)
        self.assertIn("was not checked", found[0])
        self.assertNotIn(huge, found[0])

    def test_a_contract_nested_far_too_deep_is_refused(self) -> None:
        schema: dict = {"type": "object"}
        deepest = schema
        for _ in range(40):
            deepest["properties"] = {"next": {"type": "object"}}
            deepest = deepest["properties"]["next"]
        with self.assertRaises(HarnessError):
            contracts.check_schema(schema)

    def test_words_that_only_describe_the_contract_are_allowed(self) -> None:
        contracts.check_schema({
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "A price", "description": "How much", "examples": [1],
            "type": "number",
        })


class MatchingTests(unittest.TestCase):
    def test_a_matching_answer_has_no_problems(self) -> None:
        schema = {
            "type": "object",
            "required": ["id", "name"],
            "properties": {
                "id": {"type": "integer", "minimum": 1},
                "name": {"type": "string", "minLength": 1},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
        }
        self.assertTrue(contracts.matches({"id": 3, "name": "Ada", "tags": ["x"]}, schema))

    def test_a_missing_field_is_named(self) -> None:
        found = contracts.problems({"id": 3}, {"type": "object", "required": ["id", "name"]})
        self.assertEqual(found, ("the answer.name is missing",))

    def test_the_wrong_kind_of_value_says_what_it_holds(self) -> None:
        found = contracts.problems(
            {"price": "12.50"},
            {"type": "object", "properties": {"price": {"type": "number"}}},
        )
        self.assertEqual(len(found), 1)
        self.assertIn("the answer.price must be number", found[0])
        self.assertIn('"12.50"', found[0])
        self.assertIn("which is string", found[0])

    def test_the_place_of_a_problem_inside_a_list_is_named(self) -> None:
        found = contracts.problems(
            {"items": [{"price": 1}, {"price": "two"}]},
            {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {"type": "object", "properties": {"price": {"type": "number"}}},
                    }
                },
            },
        )
        self.assertIn("the answer.items[1].price", found[0])

    def test_true_is_never_a_number(self) -> None:
        self.assertTrue(contracts.problems(True, {"type": "number"}))
        self.assertTrue(contracts.problems(True, {"type": "integer"}))
        self.assertFalse(contracts.problems(True, {"type": "boolean"}))

    def test_a_whole_number_counts_as_a_number(self) -> None:
        self.assertFalse(contracts.problems(4, {"type": "number"}))
        self.assertFalse(contracts.problems(4.0, {"type": "integer"}))
        self.assertTrue(contracts.problems(4.5, {"type": "integer"}))

    def test_null_is_only_allowed_when_the_contract_says_so(self) -> None:
        self.assertTrue(contracts.problems(None, {"type": "string"}))
        self.assertFalse(contracts.problems(None, {"type": ["string", "null"]}))
        self.assertFalse(contracts.problems(None, {"type": "string", "nullable": True}))

    def test_numbers_are_held_to_their_limits(self) -> None:
        schema = {"type": "number", "minimum": 1, "maximum": 10, "multipleOf": 0.5}
        self.assertFalse(contracts.problems(2.5, schema))
        self.assertIn("1 or more", contracts.problems(0, schema)[0])
        self.assertIn("10 or less", contracts.problems(11, schema)[0])
        self.assertIn("multiple of", contracts.problems(2.3, schema)[0])
        self.assertIn("more than 1", contracts.problems(1, {"exclusiveMinimum": 1})[0])
        self.assertIn("less than 1", contracts.problems(1, {"exclusiveMaximum": 1})[0])

    def test_text_is_held_to_its_length_and_pattern(self) -> None:
        schema = {"type": "string", "minLength": 2, "maxLength": 4, "pattern": "^[a-z]+$"}
        self.assertFalse(contracts.problems("abc", schema))
        self.assertIn("at least 2", contracts.problems("a", schema)[0])
        self.assertIn("at most 4", contracts.problems("abcde", schema)[0])
        self.assertIn("pattern", contracts.problems("AB", schema)[0])

    def test_the_shapes_of_text_it_knows_are_checked(self) -> None:
        self.assertFalse(contracts.problems("2026-08-15T10:00:00Z", {"format": "date-time"}))
        self.assertTrue(contracts.problems("yesterday", {"format": "date-time"}))
        self.assertFalse(contracts.problems("a@b.co", {"format": "email"}))
        self.assertTrue(contracts.problems("a@b", {"format": "email"}))
        self.assertFalse(contracts.problems("10.0.0.1", {"format": "ipv4"}))
        self.assertTrue(contracts.problems("10.0.0.256", {"format": "ipv4"}))

    def test_lists_are_held_to_their_size_and_sameness(self) -> None:
        schema = {"type": "array", "minItems": 1, "maxItems": 2, "uniqueItems": True}
        self.assertFalse(contracts.problems([1, 2], schema))
        self.assertIn("at least 1", contracts.problems([], schema)[0])
        self.assertIn("at most 2", contracts.problems([1, 2, 3], schema)[0])
        self.assertIn("same item twice", contracts.problems([1, 1], schema)[0])

    def test_extra_fields_can_be_refused(self) -> None:
        schema = {"type": "object", "properties": {"a": {}}, "additionalProperties": False}
        found = contracts.problems({"a": 1, "b": 2}, schema)
        self.assertIn("the answer.b is a field the contract does not allow", found[0])

    def test_extra_fields_can_be_shaped_instead(self) -> None:
        schema = {"type": "object", "additionalProperties": {"type": "string"}}
        self.assertFalse(contracts.problems({"a": "x"}, schema))
        self.assertTrue(contracts.problems({"a": 1}, schema))

    def test_a_choice_of_shapes_is_understood(self) -> None:
        schema = {"anyOf": [{"type": "string"}, {"type": "integer", "minimum": 0}]}
        self.assertFalse(contracts.problems("x", schema))
        self.assertFalse(contracts.problems(3, schema))
        self.assertIn("matches none of the 2 shapes", contracts.problems(-1, schema)[0])

    def test_exactly_one_shape_means_exactly_one(self) -> None:
        schema = {"oneOf": [{"type": "integer"}, {"type": "number"}]}
        self.assertIn("exactly one", contracts.problems(3, schema)[0])
        self.assertFalse(contracts.problems(3.5, schema))

    def test_every_shape_at_once_is_understood(self) -> None:
        schema = {"allOf": [{"type": "string"}, {"minLength": 3}]}
        self.assertFalse(contracts.problems("abc", schema))
        self.assertTrue(contracts.problems("ab", schema))

    def test_a_shape_it_must_not_have_is_understood(self) -> None:
        schema = {"not": {"type": "string"}}
        self.assertFalse(contracts.problems(5, schema))
        self.assertIn("not allowed to have", contracts.problems("x", schema)[0])

    def test_a_named_rule_can_be_reused(self) -> None:
        schema = {
            "$defs": {"money": {"type": "number", "minimum": 0}},
            "type": "object",
            "properties": {"paid": {"$ref": "#/$defs/money"}, "due": {"$ref": "#/$defs/money"}},
        }
        self.assertFalse(contracts.problems({"paid": 1, "due": 2}, schema))
        found = contracts.problems({"paid": -1, "due": 2}, schema)
        self.assertIn("the answer.paid must be 0 or more", found[0])

    def test_a_named_rule_that_is_not_there_is_refused(self) -> None:
        with self.assertRaises(HarnessError):
            contracts.problems({"a": 1}, {"properties": {"a": {"$ref": "#/$defs/missing"}}})

    def test_a_rule_that_points_at_itself_cannot_run_forever(self) -> None:
        schema = {"$defs": {"loop": {"$ref": "#/$defs/loop"}}, "$ref": "#/$defs/loop"}
        # The depth cap stops it, and it says so. Stopping quietly would mean a
        # deep enough answer was never checked while the case reported success,
        # which is the one thing this checker must never do.
        found = contracts.problems({"a": 1}, schema)
        self.assertTrue(found)
        self.assertIn("deep", found[0])

    def test_the_list_of_problems_is_bounded(self) -> None:
        schema = {"type": "array", "items": {"type": "string"}}
        found = contracts.problems(list(range(500)), schema)
        self.assertEqual(len(found), contracts.MAX_PROBLEMS)

    def test_the_value_shown_in_a_problem_is_short(self) -> None:
        found = contracts.problems({"a": "x" * 5000}, {"properties": {"a": {"type": "number"}}})
        self.assertLess(len(found[0]), 200)


class ContractsInsideAnHttpCheckTests(unittest.TestCase):
    """The whole way through: a check that says what shape the answer must have."""

    def setUp(self) -> None:
        import copy
        import tempfile
        from pathlib import Path

        from our_harness.config import DEFAULT_CONFIG, LoadedConfig

        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.addCleanup(self.temporary.cleanup)

    def run_case(self, expect: dict, body: str, run_id: str = "r"):
        from our_harness import qa

        suite = qa.parse_suite({
            "name": "api",
            "cases": [{
                "id": "answer", "kind": "http", "url": "http://127.0.0.1:9/thing", "expect": expect,
            }],
        })

        def fetch(case, timeout):
            return 200, body, len(body)

        runner = qa.QaRunner(self.config, http_fetch=fetch)
        return runner.run(suite, run_id=run_id, write_artifacts=False).cases[0]

    def test_an_answer_with_the_promised_shape_passes(self) -> None:
        case = self.run_case(
            {"status": 200, "contract": {
                "type": "object",
                "required": ["id", "name"],
                "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
            }},
            '{"id": 1, "name": "Ada"}',
        )
        self.assertEqual(case.status, "passed")

    def test_a_wrong_shape_fails_and_says_where(self) -> None:
        case = self.run_case(
            {"contract": {
                "type": "object",
                "required": ["id"],
                "properties": {"id": {"type": "integer"}},
            }},
            '{"id": "one"}',
            run_id="r2",
        )
        self.assertEqual(case.status, "failed")
        self.assertIn("the answer.id must be integer", case.reasons[0])

    def test_an_answer_that_is_not_json_fails_plainly(self) -> None:
        case = self.run_case({"contract": {"type": "object"}}, "<html>not json</html>", run_id="r3")
        self.assertEqual(case.status, "failed")
        self.assertIn("not JSON", case.reasons[0])

    def test_a_contract_kept_in_a_file_is_used(self) -> None:
        (self.root / "api.schema.json").write_text(
            '{"type": "object", "required": ["ok"]}', encoding="utf-8"
        )
        good = self.run_case({"contract_file": "api.schema.json"}, '{"ok": true}', run_id="r4")
        self.assertEqual(good.status, "passed")
        bad = self.run_case({"contract_file": "api.schema.json"}, '{"no": true}', run_id="r5")
        self.assertEqual(bad.status, "failed")
        self.assertIn("the answer.ok is missing", bad.reasons[0])

    def test_a_missing_contract_file_fails_rather_than_passes(self) -> None:
        # Nothing was checked, so the check must not report success. The old
        # tool passed here, which is the worst possible answer.
        case = self.run_case({"contract_file": "gone.json"}, '{"anything": 1}', run_id="r6")
        self.assertEqual(case.status, "failed")
        self.assertIn("nothing was checked", case.reasons[0])

    def test_a_broken_contract_file_fails_rather_than_passes(self) -> None:
        (self.root / "broken.json").write_text("{ this is not json", encoding="utf-8")
        case = self.run_case({"contract_file": "broken.json"}, "{}", run_id="r7")
        self.assertEqual(case.status, "failed")
        self.assertIn("not valid JSON", case.reasons[0])

    def test_a_contract_file_holding_a_rule_this_tool_cannot_enforce_fails(self) -> None:
        (self.root / "future.json").write_text(
            '{"type": "object", "patternProperties": {"^x": {"type": "string"}}}', encoding="utf-8"
        )
        case = self.run_case({"contract_file": "future.json"}, "{}", run_id="r8")
        self.assertEqual(case.status, "failed")
        self.assertIn("patternProperties", case.reasons[0])

    def test_a_contract_may_not_be_read_from_the_git_folder(self) -> None:
        case = self.run_case({"contract_file": ".git/config.json"}, "{}", run_id="r9")
        self.assertEqual(case.status, "failed")
        self.assertIn(".git", case.reasons[0])

    def test_a_case_may_not_hold_a_contract_and_name_a_file(self) -> None:
        from our_harness import qa

        with self.assertRaises(HarnessError):
            qa.parse_suite({"name": "d", "cases": [{
                "id": "a", "kind": "http", "url": "http://127.0.0.1:9/",
                "expect": {"contract": {"type": "object"}, "contract_file": "x.json"},
            }]})

    def test_a_token_from_saved_settings_can_be_sent_with_the_request(self) -> None:
        from our_harness import datasets, qa

        datasets.save_environments(self.config, {"staging": {"TOKEN": "abc123"}})
        suite = qa.parse_suite({"name": "api", "cases": [{
            "id": "answer", "kind": "http", "url": "http://127.0.0.1:9/thing",
            "headers": {"Authorization": "Bearer ${env.TOKEN}"},
            "expect": {"contract": {"type": "object"}},
        }]})
        seen: list = []

        def fetch(case, timeout):
            seen.append(dict(case.headers))
            return 200, "{}", 2

        runner = qa.QaRunner(self.config, http_fetch=fetch, environment="staging")
        result = runner.run(suite, run_id="r10", write_artifacts=False)
        self.assertEqual(result.cases[0].status, "passed")
        self.assertEqual(seen[0]["Authorization"], "Bearer abc123")


if __name__ == "__main__":
    unittest.main()
