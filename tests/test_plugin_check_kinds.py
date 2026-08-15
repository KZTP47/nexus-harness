from __future__ import annotations

import copy
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from our_harness import qa
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError
from our_harness.plugins import PluginRegistry, load_plugins


EXAMPLE_PLUGIN = Path(__file__).resolve().parents[1] / "examples" / "plugins" / "sqlite_check.py"


def counting_kind(calls: list) -> qa.CheckKind:
    def run(case, runner):
        calls.append(case.id)
        wanted = case.expect_extra("answer")
        found = str(case.field("question", ""))[::-1]
        if wanted is not None and found != wanted:
            return (f"The answer is \"{found}\"; the case expects \"{wanted}\"",), found, found
        return (), found, found

    return qa.CheckKind(
        name="reverse",
        summary="Turn the question around and check the answer.",
        fields=frozenset({"question"}),
        expectations=frozenset({"answer"}),
        run=run,
    )


class KindValidationTests(unittest.TestCase):
    def test_a_kind_needs_a_plain_name_and_a_way_to_run(self) -> None:
        for kind in (
            qa.CheckKind(name="Bad Name", summary="s", run=lambda case, runner: ((), "", "")),
            qa.CheckKind(name="ok", summary="s", run=None),
        ):
            with self.subTest(name=kind.name), self.assertRaises(HarnessError):
                kind.validate()

    def test_a_plugin_may_not_replace_a_built_in_kind(self) -> None:
        kind = qa.CheckKind(name="command", summary="s", run=lambda case, runner: ((), "", ""))
        with self.assertRaises(HarnessError) as caught:
            kind.validate()
        self.assertIn("may not replace the built-in command", str(caught.exception))

    def test_a_plugin_may_not_take_over_a_field_the_suite_owns(self) -> None:
        kind = qa.CheckKind(
            name="mine", summary="s", fields=frozenset({"retries"}),
            run=lambda case, runner: ((), "", ""),
        )
        with self.assertRaises(HarnessError) as caught:
            kind.validate()
        self.assertIn("retries", str(caught.exception))

    def test_two_plugins_cannot_share_one_name(self) -> None:
        first = counting_kind([])
        second = counting_kind([])
        with self.assertRaises(HarnessError) as caught:
            qa.validated_kinds([first, second])
        self.assertIn("Two plugins", str(caught.exception))

    def test_something_that_is_not_a_kind_is_refused(self) -> None:
        with self.assertRaises(HarnessError):
            qa.validated_kinds([{"name": "reverse"}])

    def test_no_kinds_is_not_an_error(self) -> None:
        self.assertEqual(qa.validated_kinds(None), {})
        self.assertEqual(qa.validated_kinds([]), {})


class RegistryTests(unittest.TestCase):
    def test_the_registry_keeps_one_kind_per_name(self) -> None:
        registry = PluginRegistry()
        registry.add_check_kind(counting_kind([]))
        self.assertIn("reverse", registry.check_kinds)
        with self.assertRaises(HarnessError):
            registry.add_check_kind(counting_kind([]))

    def test_a_kind_without_a_name_is_refused(self) -> None:
        with self.assertRaises(HarnessError):
            PluginRegistry().add_check_kind(object())

    def test_a_project_with_no_plugins_has_no_extra_kinds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), root, [], {})
            self.assertEqual(load_plugins(config).check_kinds, {})


class SuiteWithPluginKindTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.calls: list[str] = []
        self.kinds = qa.validated_kinds([counting_kind(self.calls)])
        self.addCleanup(self.temporary.cleanup)

    def suite(self, case: dict) -> qa.QaSuite:
        return qa.parse_suite({"name": "d", "cases": [case]}, extra_kinds=self.kinds)

    def test_a_plugin_case_is_read_and_kept(self) -> None:
        suite = self.suite({
            "id": "turn", "kind": "reverse", "question": "abc", "expect": {"answer": "cba"},
        })
        case = suite.cases[0]
        self.assertEqual(case.kind, "reverse")
        self.assertEqual(case.field("question"), "abc")
        self.assertEqual(case.expect_extra("answer"), "cba")

    def test_a_plugin_case_survives_a_round_trip(self) -> None:
        original = self.suite({
            "id": "turn", "title": "It turns around", "kind": "reverse", "tags": ["fast"],
            "question": "abc", "expect": {"answer": "cba"},
        })
        again = qa.parse_suite(json.loads(json.dumps(original.to_dict())), extra_kinds=self.kinds)
        self.assertEqual(again.to_dict(), original.to_dict())

    def test_the_kind_is_unknown_without_its_plugin(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            qa.parse_suite({"name": "d", "cases": [{"id": "t", "kind": "reverse", "question": "a"}]})
        self.assertIn("command, file, http, browser", str(caught.exception))

    def test_the_plugin_kind_is_listed_when_a_name_is_wrong(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            qa.parse_suite({"name": "d", "cases": [{"id": "t", "kind": "nonsense"}]}, extra_kinds=self.kinds)
        self.assertIn("reverse", str(caught.exception))

    def test_a_field_the_plugin_did_not_declare_is_still_refused(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            self.suite({"id": "t", "kind": "reverse", "question": "a", "command": ["rm", "-rf", "/"]})
        self.assertIn("command", str(caught.exception))

    def test_an_expectation_the_plugin_did_not_declare_is_still_refused(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            self.suite({"id": "t", "kind": "reverse", "question": "a", "expect": {"exit_code": 0}})
        self.assertIn("exit_code", str(caught.exception))

    def test_a_plugin_case_runs_and_can_pass(self) -> None:
        suite = self.suite({"id": "turn", "kind": "reverse", "question": "abc", "expect": {"answer": "cba"}})
        result = qa.QaRunner(self.config, extra_kinds=self.kinds).run(
            suite, run_id="p1", write_artifacts=False
        )
        self.assertEqual(result.cases[0].status, "passed")
        self.assertEqual(self.calls, ["turn"])

    def test_a_plugin_case_runs_and_can_fail_with_its_own_words(self) -> None:
        suite = self.suite({"id": "turn", "kind": "reverse", "question": "abc", "expect": {"answer": "xyz"}})
        result = qa.QaRunner(self.config, extra_kinds=self.kinds).run(
            suite, run_id="p2", write_artifacts=False
        )
        self.assertEqual(result.cases[0].status, "failed")
        self.assertIn('the case expects "xyz"', result.cases[0].reasons[0])

    def test_a_plugin_that_breaks_only_fails_its_own_case(self) -> None:
        def broken(case, runner):
            raise ZeroDivisionError("the plugin has a bug")

        kinds = qa.validated_kinds([
            qa.CheckKind(name="broken", summary="s", run=broken),
            counting_kind(self.calls),
        ])
        suite = qa.parse_suite({
            "name": "d",
            "cases": [
                {"id": "bad", "kind": "broken"},
                {"id": "good", "kind": "reverse", "question": "ab", "expect": {"answer": "ba"}},
            ],
        }, extra_kinds=kinds)
        result = qa.QaRunner(self.config, extra_kinds=kinds).run(suite, run_id="p3", write_artifacts=False)
        statuses = {case.id: case.status for case in result.cases}
        self.assertEqual(statuses, {"bad": "failed", "good": "passed"})
        broken_case = next(case for case in result.cases if case.id == "bad")
        self.assertIn("The check itself broke", broken_case.reasons[0])

    def test_a_plugin_can_say_a_case_cannot_run_here(self) -> None:
        def unavailable(case, runner):
            raise qa.QaSkipped("The thing this needs is not installed.")

        kinds = qa.validated_kinds([qa.CheckKind(name="absent", summary="s", run=unavailable)])
        suite = qa.parse_suite({"name": "d", "cases": [{"id": "x", "kind": "absent"}]}, extra_kinds=kinds)
        result = qa.QaRunner(self.config, extra_kinds=kinds).run(suite, run_id="p4", write_artifacts=False)
        self.assertEqual(result.cases[0].status, "skipped")
        self.assertTrue(result.passed)


class PluginValueBoundsTests(unittest.TestCase):
    """A suite is a data file. A plugin field is not a way around that."""

    def setUp(self) -> None:
        self.kinds = qa.validated_kinds([
            qa.CheckKind(
                name="demo", summary="s",
                fields=frozenset({"target"}),
                expectations=frozenset({"answer"}),
                run=lambda case, runner: ((), "", ""),
            )
        ])

    def case(self, **extra: object) -> dict:
        return {"id": "a", "kind": "demo", **extra}

    def parse(self, case: dict) -> qa.QaCase:
        return qa.parse_suite({"name": "d", "cases": [case]}, extra_kinds=self.kinds).cases[0]

    def test_plain_values_are_kept_as_written(self) -> None:
        found = self.parse(self.case(target=["a", 1, 2.5, True, None]))
        self.assertEqual(found.field("target"), ["a", 1, 2.5, True, None])

    def test_a_nested_object_is_refused(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            self.parse(self.case(target={"nested": 1}))
        self.assertIn("text, a number, true, false, null", str(caught.exception))

    def test_a_list_of_lists_is_refused(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            self.parse(self.case(target=[[1]]))
        self.assertIn("flat list", str(caught.exception))

    def test_a_very_long_value_is_refused(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            self.parse(self.case(target="x" * 20_001))
        self.assertIn("at most 20000 characters", str(caught.exception))

    def test_a_very_long_list_is_refused(self) -> None:
        with self.assertRaises(HarnessError):
            self.parse(self.case(target=list(range(101))))

    def test_a_list_of_allowed_values_that_adds_up_is_refused(self) -> None:
        """A hundred values under the limit can still make two megabytes."""

        with self.assertRaises(HarnessError) as caught:
            self.parse(self.case(target=["x" * 20_000] * 100))
        self.assertIn("in total", str(caught.exception))

    def test_a_number_that_is_not_real_is_refused(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            self.parse(self.case(target=float("inf")))
        self.assertIn("must be a real number", str(caught.exception))

    def test_the_same_bound_applies_to_expectations(self) -> None:
        with self.assertRaises(HarnessError):
            self.parse(self.case(expect={"answer": {"nested": 1}}))
        with self.assertRaises(HarnessError):
            self.parse(self.case(expect={"answer": "y" * 20_001}))

    def test_a_broken_kind_is_refused_before_any_case_is_read(self) -> None:
        shadow = {"command": qa.CheckKind(name="command", summary="s", run=lambda case, runner: ((), "", ""))}
        with self.assertRaises(HarnessError) as caught:
            qa.parse_suite({"name": "d", "cases": []}, extra_kinds=shadow)
        self.assertIn("may not replace the built-in command", str(caught.exception))

    def test_a_kind_with_no_way_to_run_is_refused_at_read_time(self) -> None:
        broken = {"x": qa.CheckKind(name="x", summary="s", run=None)}
        with self.assertRaises(HarnessError):
            qa.parse_suite({"name": "d", "cases": []}, extra_kinds=broken)


class ExamplePluginTests(unittest.TestCase):
    """The shipped example must really work, not just read well."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        if str(EXAMPLE_PLUGIN.parent) not in sys.path:
            sys.path.insert(0, str(EXAMPLE_PLUGIN.parent))
        import sqlite_check

        self.kinds = qa.validated_kinds([sqlite_check.SQLITE_CHECK])
        self.addCleanup(self.temporary.cleanup)

    def database(self, rows: list[tuple[str, str]]) -> None:
        (self.root / "data").mkdir(exist_ok=True)
        connection = sqlite3.connect(self.root / "data" / "app.db")
        connection.execute("CREATE TABLE users(name TEXT, role TEXT)")
        connection.executemany("INSERT INTO users VALUES(?, ?)", rows)
        connection.commit()
        connection.close()

    def run_case(self, case: dict) -> qa.QaCaseResult:
        suite = qa.parse_suite({"name": "d", "cases": [case]}, extra_kinds=self.kinds)
        result = qa.QaRunner(self.config, extra_kinds=self.kinds).run(
            suite, run_id="e1", write_artifacts=False
        )
        return result.cases[0]

    def test_the_example_reads_a_real_database(self) -> None:
        self.database([("ada", "admin"), ("bob", "user")])
        found = self.run_case({
            "id": "one-admin", "kind": "sqlite", "database": "data/app.db",
            "query": "SELECT count(*) FROM users WHERE role = 'admin'",
            "expect": {"rows": 1, "first_value": "1"},
        })
        self.assertEqual(found.status, "passed")

    def test_a_wrong_answer_is_reported_in_plain_words(self) -> None:
        self.database([("ada", "admin"), ("bob", "admin")])
        found = self.run_case({
            "id": "one-admin", "kind": "sqlite", "database": "data/app.db",
            "query": "SELECT count(*) FROM users WHERE role = 'admin'",
            "expect": {"first_value": "1"},
        })
        self.assertEqual(found.status, "failed")
        self.assertIn('The first value is "2"', found.reasons[0])

    def test_a_missing_database_is_skipped_not_failed(self) -> None:
        found = self.run_case({
            "id": "absent", "kind": "sqlite", "database": "data/app.db",
            "query": "SELECT 1",
        })
        self.assertEqual(found.status, "skipped")
        self.assertIn("no database at data/app.db", found.reasons[0])

    def test_the_example_refuses_to_change_anything(self) -> None:
        self.database([("ada", "admin")])
        found = self.run_case({
            "id": "naughty", "kind": "sqlite", "database": "data/app.db",
            "query": "DELETE FROM users",
        })
        self.assertEqual(found.status, "failed")
        self.assertIn("only run a SELECT query", found.reasons[0])
        connection = sqlite3.connect(self.root / "data" / "app.db")
        self.assertEqual(connection.execute("SELECT count(*) FROM users").fetchone()[0], 1)
        connection.close()

    def test_a_with_query_that_only_reads_is_allowed(self) -> None:
        self.database([("ada", "admin"), ("bob", "user")])
        found = self.run_case({
            "id": "counted", "kind": "sqlite", "database": "data/app.db",
            "query": "WITH admins AS (SELECT * FROM users WHERE role = 'admin') "
                     "SELECT count(*) FROM admins",
            "expect": {"first_value": "1"},
        })
        self.assertEqual(found.status, "passed")

    def test_a_query_that_never_ends_is_stopped(self) -> None:
        """A plugin runs inside the harness, so it has to stop itself."""

        self.database([("ada", "admin")])
        found = self.run_case({
            "id": "runaway", "kind": "sqlite", "database": "data/app.db",
            "query": "WITH RECURSIVE c(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM c) "
                     "SELECT count(*) FROM c",
        })
        self.assertEqual(found.status, "failed")
        self.assertIn("took too long", found.reasons[0])

    def test_the_example_cannot_read_outside_the_project(self) -> None:
        found = self.run_case({
            "id": "escape", "kind": "sqlite", "database": "../secrets.db",
            "query": "SELECT 1",
        })
        self.assertEqual(found.status, "failed")
        self.assertTrue(found.reasons)


if __name__ == "__main__":
    unittest.main()
