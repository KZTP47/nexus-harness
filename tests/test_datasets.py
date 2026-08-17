from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from our_harness import datasets, qa
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError


def isolated(root: Path) -> LoadedConfig:
    return LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), root, [], {})


class CsvTests(unittest.TestCase):
    """The tool this replaces split on newlines and broke on quoted values."""

    def test_a_plain_table_is_read(self) -> None:
        rows = datasets.rows_from_csv("name,age\nada,36\nbob,41\n")
        self.assertEqual([row.mapping() for row in rows], [
            {"name": "ada", "age": "36"}, {"name": "bob", "age": "41"},
        ])

    def test_a_quoted_comma_stays_in_one_value(self) -> None:
        rows = datasets.rows_from_csv('name,note\nada,"one, two"\n')
        self.assertEqual(rows[0].mapping()["note"], "one, two")

    def test_a_quoted_line_break_stays_in_one_value(self) -> None:
        rows = datasets.rows_from_csv('name,note\nada,"line one\nline two"\n')
        self.assertEqual(rows[0].mapping()["note"], "line one\nline two")
        self.assertEqual(len(rows), 1, "a quoted line break must not start a new row")

    def test_windows_line_endings_leave_no_stray_character(self) -> None:
        rows = datasets.rows_from_csv("name,age\r\nada,36\r\n")
        self.assertEqual(rows[0].mapping(), {"name": "ada", "age": "36"})

    def test_leading_and_trailing_spaces_in_a_value_are_kept(self) -> None:
        rows = datasets.rows_from_csv('name,note\nada,"  spaced  "\n')
        self.assertEqual(rows[0].mapping()["note"], "  spaced  ")

    def test_a_short_row_is_refused_rather_than_lining_up_wrongly(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            datasets.rows_from_csv("a,b,c\n1,2\n")
        self.assertIn("has 2 values but the header names 3", str(caught.exception))

    def test_a_repeated_column_name_is_refused(self) -> None:
        with self.assertRaises(HarnessError):
            datasets.rows_from_csv("name,name\na,b\n")

    def test_a_table_with_no_rows_is_refused(self) -> None:
        with self.assertRaises(HarnessError):
            datasets.rows_from_csv("name,age\n")

    def test_blank_lines_are_skipped(self) -> None:
        rows = datasets.rows_from_csv("name\n\nada\n\n\nbob\n")
        self.assertEqual([row.mapping()["name"] for row in rows], ["ada", "bob"])


class JsonTableTests(unittest.TestCase):
    def test_a_list_of_objects_is_read(self) -> None:
        rows = datasets.rows_from_json('[{"a": "1"}, {"a": "2"}]')
        self.assertEqual([row.mapping() for row in rows], [{"a": "1"}, {"a": "2"}])

    def test_an_object_holding_rows_is_read(self) -> None:
        rows = datasets.rows_from_json('{"rows": [{"a": "1"}]}')
        self.assertEqual(rows[0].mapping(), {"a": "1"})

    def test_numbers_and_flags_become_text_without_surprise(self) -> None:
        rows = datasets.rows_from_json('[{"n": 5, "f": true, "e": null, "d": 1.5}]')
        self.assertEqual(rows[0].mapping(), {"n": "5", "f": "true", "e": "", "d": "1.5"})

    def test_a_nested_value_is_refused(self) -> None:
        with self.assertRaises(HarnessError):
            datasets.rows_from_json('[{"a": {"deep": 1}}]')

    def test_broken_json_says_so(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            datasets.rows_from_json("{not json}")
        self.assertIn("not valid JSON", str(caught.exception))


class RowLabelTests(unittest.TestCase):
    def test_a_label_column_names_the_row(self) -> None:
        rows = datasets.rows_from_list([{"label": "admin user", "u": "ada"}])
        self.assertEqual(rows[0].label, "admin user")

    def test_the_first_value_names_the_row_when_there_is_no_label(self) -> None:
        rows = datasets.rows_from_list([{"u": "ada", "p": "x"}])
        self.assertEqual(rows[0].label, "ada")

    def test_an_empty_row_still_gets_a_name(self) -> None:
        rows = datasets.rows_from_list([{"u": ""}])
        self.assertEqual(rows[0].label, "row 1")


class FillTests(unittest.TestCase):
    def test_a_row_value_goes_in(self) -> None:
        self.assertEqual(datasets.fill("hello ${row.name}", {"name": "ada"}, {}, "x"), "hello ada")

    def test_a_setting_goes_in(self) -> None:
        self.assertEqual(
            datasets.fill("${env.BASE_URL}/health", {}, {"BASE_URL": "http://x"}, "x"),
            "http://x/health",
        )

    def test_a_name_with_no_value_says_which_name(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            datasets.fill("${row.missing}", {"name": "ada"}, {}, "Check login")
        message = str(caught.exception)
        self.assertIn("Check login", message)
        self.assertIn("column named missing", message)
        self.assertIn("name", message)

    def test_a_column_name_with_odd_characters_is_still_literal(self) -> None:
        """Nothing built from a user's text is compiled as a pattern."""

        values = {"total (net)": "5", "a.b": "6"}
        self.assertEqual(datasets.fill("${row.a.b}", values, {}, "x"), "6")
        self.assertEqual(datasets.fill("cost ${row.total (net)}", values, {}, "x"), "cost 5")

    def test_a_value_holding_a_placeholder_is_not_filled_again(self) -> None:
        self.assertEqual(
            datasets.fill("${row.a}", {"a": "${row.b}", "b": "no"}, {}, "x"),
            "${row.b}",
            "one pass only, so a value cannot reach into another",
        )

    def test_a_value_holding_quotes_or_semicolons_is_just_a_value(self) -> None:
        found = datasets.fill("${row.v}", {"v": '"; rm -rf /'}, {}, "x")
        self.assertEqual(found, '"; rm -rf /')

    def test_text_with_no_placeholder_is_left_alone(self) -> None:
        self.assertEqual(datasets.fill("plain $ {row} text", {}, {}, "x"), "plain $ {row} text")

    def test_filling_reaches_into_lists_and_objects(self) -> None:
        found = datasets.fill_value(
            {"a": ["${row.x}", {"b": "${env.Y}"}]}, {"x": "1"}, {"Y": "2"}, "x"
        )
        self.assertEqual(found, {"a": ["1", {"b": "2"}]})


class EnvironmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.config = isolated(self.root)
        self.addCleanup(self.temporary.cleanup)

    def test_nothing_is_saved_to_begin_with(self) -> None:
        self.assertEqual(datasets.load_environments(self.config), {})
        self.assertEqual(datasets.chosen_environment(self.config), ("", {}))

    def test_settings_survive_a_save_and_load(self) -> None:
        datasets.save_environments(self.config, {"local": {"BASE_URL": "http://x"}})
        self.assertEqual(datasets.load_environments(self.config), {"local": {"BASE_URL": "http://x"}})

    def test_asking_for_settings_that_do_not_exist_names_the_ones_that_do(self) -> None:
        datasets.save_environments(self.config, {"local": {"A": "1"}, "test": {"A": "2"}})
        with self.assertRaises(HarnessError) as caught:
            datasets.chosen_environment(self.config, "staging")
        message = str(caught.exception)
        self.assertIn("no settings named staging", message)
        self.assertIn("local, test", message)

    def test_a_broken_settings_file_says_so(self) -> None:
        datasets.environments_path(self.config).parent.mkdir(parents=True, exist_ok=True)
        datasets.environments_path(self.config).write_text("not json", encoding="utf-8")
        with self.assertRaises(HarnessError):
            datasets.load_environments(self.config)

    def test_a_nested_settings_value_is_refused(self) -> None:
        with self.assertRaises(HarnessError):
            datasets.save_environments(self.config, {"local": {"A": {"deep": 1}}})


class RunningRowsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.config = isolated(self.root)
        self.addCleanup(self.temporary.cleanup)

    def run_suite(self, case: dict, environment: str = "") -> qa.QaRunResult:
        suite = qa.parse_suite({"name": "d", "cases": [case]})
        runner = qa.QaRunner(self.config, environment=environment)
        return runner.run(suite, run_id="rows", write_artifacts=False)

    def test_one_written_check_becomes_one_run_for_each_row(self) -> None:
        result = self.run_suite({
            "id": "greet", "kind": "command",
            "command": ["python", "-c", "print('hello ${row.name}')"],
            "expect": {"stdout_contains": ["hello ${row.name}"]},
            "rows": [{"name": "ada"}, {"name": "bob"}, {"name": "cleo"}],
        })
        self.assertEqual([case.id for case in result.cases], ["greet#1", "greet#2", "greet#3"])
        self.assertTrue(result.passed)
        self.assertEqual(result.cases[0].title, "greet [ada]")

    def test_only_the_wrong_row_fails(self) -> None:
        result = self.run_suite({
            "id": "add", "kind": "command",
            "command": ["python", "-c", "print(int('${row.a}') + int('${row.b}'))"],
            "expect": {"stdout_contains": ["${row.total}"]},
            "rows": [
                {"a": "2", "b": "2", "total": "4"},
                {"a": "2", "b": "3", "total": "99"},
                {"a": "1", "b": "1", "total": "2"},
            ],
        })
        statuses = {case.id: case.status for case in result.cases}
        self.assertEqual(statuses, {"add#1": "passed", "add#2": "failed", "add#3": "passed"})
        self.assertFalse(result.passed)

    def test_a_check_with_no_table_is_untouched(self) -> None:
        result = self.run_suite({"id": "plain", "kind": "command", "command": ["python", "-c", "pass"]})
        self.assertEqual([case.id for case in result.cases], ["plain"])

    def test_a_table_can_come_from_a_file(self) -> None:
        (self.root / "users.csv").write_text("name,greeting\nada,hi ada\nbob,hi bob\n", encoding="utf-8")
        result = self.run_suite({
            "id": "greet", "kind": "command",
            "command": ["python", "-c", "print('hi ${row.name}')"],
            "expect": {"stdout_contains": ["${row.greeting}"]},
            "rows_file": "users.csv",
        })
        self.assertEqual(len(result.cases), 2)
        self.assertTrue(result.passed)

    def test_a_table_file_that_is_missing_fails_the_run_clearly(self) -> None:
        suite = qa.parse_suite({"name": "d", "cases": [
            {"id": "x", "kind": "command", "command": ["python", "-c", "pass"], "rows_file": "gone.csv"},
        ]})
        with self.assertRaises(HarnessError) as caught:
            qa.QaRunner(self.config).run(suite, run_id="x", write_artifacts=False)
        self.assertIn("no table at gone.csv", str(caught.exception))

    def test_a_table_may_not_be_read_from_the_git_folder(self) -> None:
        with self.assertRaises(HarnessError):
            datasets.read_rows(self.config, ".git/config")

    def test_naming_a_table_twice_is_refused(self) -> None:
        with self.assertRaises(HarnessError) as caught:
            qa.parse_suite({"name": "d", "cases": [{
                "id": "x", "kind": "file", "path": "a", "rows": [{"a": "1"}], "rows_file": "b.csv",
            }]})
        self.assertIn("name a table or hold one, not both", str(caught.exception))

    def test_settings_reach_a_check(self) -> None:
        datasets.save_environments(self.config, {"local": {"WORD": "sunshine"}})
        result = self.run_suite({
            "id": "say", "kind": "command",
            "command": ["python", "-c", "print('${env.WORD}')"],
            "expect": {"stdout_contains": ["sunshine"]},
        }, environment="local")
        self.assertTrue(result.passed)
        self.assertEqual([case.id for case in result.cases], ["say"])

    def test_a_row_and_a_setting_can_be_used_together(self) -> None:
        datasets.save_environments(self.config, {"local": {"PREFIX": "user"}})
        result = self.run_suite({
            "id": "both", "kind": "command",
            "command": ["python", "-c", "print('${env.PREFIX}-${row.n}')"],
            "expect": {"stdout_contains": ["user-${row.n}"]},
            "rows": [{"n": "1"}, {"n": "2"}],
        }, environment="local")
        self.assertTrue(result.passed)
        self.assertEqual(len(result.cases), 2)

    def test_a_file_check_can_use_a_row_in_its_path(self) -> None:
        (self.root / "one.txt").write_text("first", encoding="utf-8")
        (self.root / "two.txt").write_text("second", encoding="utf-8")
        result = self.run_suite({
            "id": "files", "kind": "file", "path": "${row.file}",
            "expect": {"exists": True, "contains": ["${row.holds}"]},
            "rows": [{"file": "one.txt", "holds": "first"}, {"file": "two.txt", "holds": "second"}],
        })
        self.assertTrue(result.passed)

    def test_a_row_that_asks_for_a_missing_column_fails_that_row_only(self) -> None:
        suite = qa.parse_suite({"name": "d", "cases": [{
            "id": "x", "kind": "command", "command": ["python", "-c", "print('${row.here}')"],
            "rows": [{"here": "1"}],
        }]})
        runner = qa.QaRunner(self.config)
        # A column that exists works; one that does not is reported when selected.
        self.assertEqual(len(runner.select(suite)), 1)

    def test_the_row_is_remembered_on_the_case_for_reports(self) -> None:
        suite = qa.parse_suite({"name": "d", "cases": [{
            "id": "x", "kind": "command", "command": ["python", "-c", "pass"],
            "rows": [{"user": "ada"}],
        }]})
        chosen = qa.QaRunner(self.config).select(suite)
        self.assertEqual(chosen[0].row.mapping(), {"user": "ada"})
        self.assertIn("user=ada", datasets.describe_row(chosen[0].row))


class CommandLineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        (self.root / ".harness" / "config.json").write_text(
            json.dumps({"schema_version": 1, "memory": {"enabled": False}}), encoding="utf-8"
        )
        self.addCleanup(self.temporary.cleanup)

    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        from contextlib import redirect_stderr, redirect_stdout
        from io import StringIO

        from our_harness import cli

        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--project", str(self.root), "qa", *arguments])
        return code, out.getvalue(), err.getvalue()

    def test_settings_can_be_added_listed_and_removed(self) -> None:
        code, output, _ = self.run_cli("env", "list")
        self.assertEqual(code, 0)
        self.assertIn("No settings are saved", output)

        code, output, _ = self.run_cli("env", "set", "local", "BASE_URL=http://x", "USER=ada")
        self.assertEqual(code, 0)
        self.assertIn("Saved 2 values under local", output)

        code, output, _ = self.run_cli("env", "list")
        self.assertIn("BASE_URL = http://x", output)
        self.assertIn("USER = ada", output)

        code, output, _ = self.run_cli("env", "delete", "local")
        self.assertEqual(code, 0)
        code, output, _ = self.run_cli("env", "list")
        self.assertIn("No settings are saved", output)

    def test_a_value_written_without_an_equals_sign_says_so(self) -> None:
        code, _out, errors = self.run_cli("env", "set", "local", "JUSTAKEY")
        self.assertEqual(code, 2)
        self.assertIn("KEY=value", errors)

    def test_removing_settings_that_are_not_there_says_so(self) -> None:
        code, _out, errors = self.run_cli("env", "delete", "nothing")
        self.assertEqual(code, 2)
        self.assertIn("no settings named nothing", errors)

    def test_running_with_settings_that_do_not_exist_stops_early(self) -> None:
        self.run_cli("init")
        code, _out, errors = self.run_cli("run", "--environment", "nothing")
        self.assertEqual(code, 2)
        self.assertIn("no settings named nothing", errors)


if __name__ == "__main__":
    unittest.main()
