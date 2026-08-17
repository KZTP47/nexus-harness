"""Looking for credentials left in the project, and never passing without looking."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from our_harness import qa, scan
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError


class FindingTests(unittest.TestCase):
    def test_the_shapes_it_knows_are_found(self) -> None:
        lines = {
            "an OpenAI key": 'KEY = "sk-abcdefghijklmnopqrst1234"',
            "a GitHub token": 'T = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"',
            "a Slack token": 'T = "xoxb-123456789012-abcdefghijkl"',
            "an Amazon key": 'AWS = "AKIA1234567890ABCDEF"',
            "a private key file": "-----BEGIN RSA PRIVATE KEY-----",
            "a password or key written into the code": 'password = "correct-horse-battery"',
            "an address with a password in it": 'DB = "postgres://user:secretpass@host/db"',
        }
        for kind, line in lines.items():
            with self.subTest(kind=kind):
                found = scan.scan_text(line, "a.py")
                self.assertEqual(len(found), 1, line)
                self.assertEqual(found[0].kind, kind)
                self.assertEqual(found[0].line, 1)

    def test_the_value_itself_never_appears_in_the_report(self) -> None:
        for line, secret in (
            ('KEY = "sk-abcdefghijklmnopqrst1234"', "sk-abcdefghijklmnopqrst1234"),
            ('DB = "postgres://user:secretpass@host/db"', "secretpass"),
            ('T = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"', "ghp_ABCDEFGHIJ"),
        ):
            with self.subTest(line=line):
                found = scan.scan_text(line, "a.py")
                self.assertNotIn(secret, found[0].excerpt)
                self.assertIn("[REDACTED]", found[0].excerpt)

    def test_a_line_showing_somebody_where_to_put_a_key_is_left_alone(self) -> None:
        for line in (
            'KEY = "sk-your-key-here-1234567890"',
            'KEY = os.environ["OPENAI_API_KEY"]',
            'password = "${DB_PASSWORD}"',
            'token = "<your token>"',
            'api_key = "changeme-please-now"',
        ):
            with self.subTest(line=line):
                self.assertEqual(scan.scan_text(line, "a.py"), [])

    def test_a_line_marked_as_allowed_is_kept_apart(self) -> None:
        line = 'KEY = "sk-abcdefghijklmnopqrst1234"  # harness: allow secret'
        found = scan.scan_text(line, "a.py")
        self.assertTrue(found[0].allowed)
        report = scan.Report(findings=tuple(found), files_read=1, files_skipped=0)
        self.assertEqual(report.real, ())
        self.assertEqual(len(report.allowed), 1)
        self.assertEqual(scan.reasons(report), [])

    def test_the_line_number_is_right(self) -> None:
        text = 'clean\nclean\nKEY = "sk-abcdefghijklmnopqrst1234"\n'
        self.assertEqual(scan.scan_text(text, "a.py")[0].line, 3)

    def test_one_line_is_reported_once(self) -> None:
        line = 'password = "sk-abcdefghijklmnopqrst1234"'
        self.assertEqual(len(scan.scan_text(line, "a.py")), 1)

    def test_ordinary_code_is_not_flagged(self) -> None:
        text = (
            "def add(first, second):\n"
            "    return first + second\n"
            "url = 'https://example.com/docs'\n"
            "name = 'a fairly long ordinary string value'\n"
        )
        self.assertEqual(scan.scan_text(text, "a.py"), [])


class ProjectScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.addCleanup(self.temporary.cleanup)
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
        (self.root / "src" / "settings.py").write_text(
            'GITHUB = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"\n', encoding="utf-8"
        )

    def test_a_planted_credential_is_found_with_its_place(self) -> None:
        report = scan.scan_project(self.config, include=["src/**/*.py"])
        self.assertEqual(report.files_read, 2)
        self.assertEqual(len(report.real), 1)
        self.assertEqual(report.real[0].path, "src/settings.py")

    def test_reading_nothing_is_a_failure_not_a_pass(self) -> None:
        report = scan.scan_project(self.config, include=["nowhere/**/*.py"])
        self.assertEqual(report.files_read, 0)
        found = scan.reasons(report)
        self.assertIn("nothing was checked", found[0])
        self.assertIn("must not pass", found[0])

    def test_files_can_be_left_out_on_purpose(self) -> None:
        report = scan.scan_project(self.config, include=["src/**/*.py"], skip=["src/settings.py"])
        self.assertEqual(report.files_read, 1)
        self.assertEqual(report.real, ())
        self.assertEqual(report.files_skipped, 1)

    def test_other_peoples_code_and_the_git_folder_are_never_read(self) -> None:
        for folder in (".git", "node_modules"):
            (self.root / folder).mkdir()
            (self.root / folder / "leak.py").write_text(
                'KEY = "sk-abcdefghijklmnopqrst1234"\n', encoding="utf-8"
            )
        report = scan.scan_project(self.config, include=["**/*.py"])
        looked_at = [item.path for item in report.findings]
        self.assertNotIn(".git/leak.py", looked_at)
        self.assertNotIn("node_modules/leak.py", looked_at)
        # The project's own planted key is still found, so this proves the
        # folders were skipped and not that the scan did nothing.
        self.assertEqual([item.path for item in report.real], ["src/settings.py"])

    def test_pictures_and_programs_are_not_read(self) -> None:
        (self.root / "logo.png").write_bytes(b"\x89PNG" + b'KEY = "sk-abcdefghijklmnopqrst1234"')
        report = scan.scan_project(self.config, include=["*.png"])
        self.assertEqual(report.files_read, 0)

    def test_a_path_that_leaves_the_project_is_refused(self) -> None:
        for pattern in ("../**/*.py", "/etc/*"):
            with self.subTest(pattern=pattern), self.assertRaises(HarnessError):
                scan.scan_project(self.config, include=[pattern])


class SecretsCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.addCleanup(self.temporary.cleanup)
        (self.root / "app.py").write_text("print('hello')\n", encoding="utf-8")

    def run_case(self, case: dict, run_id: str):
        suite = qa.parse_suite({"name": "d", "cases": [case]})
        return qa.QaRunner(self.config).run(suite, run_id=run_id, write_artifacts=False).cases[0]

    def test_a_clean_project_passes(self) -> None:
        case = self.run_case({"id": "keys", "kind": "secrets", "paths": ["*.py"]}, "s1")
        self.assertEqual(case.status, "passed")

    def test_a_project_with_a_key_in_it_fails_and_names_the_file(self) -> None:
        (self.root / "settings.py").write_text(
            'KEY = "sk-abcdefghijklmnopqrst1234"\n', encoding="utf-8"
        )
        case = self.run_case({"id": "keys", "kind": "secrets", "paths": ["*.py"]}, "s2")
        self.assertEqual(case.status, "failed")
        self.assertIn("settings.py line 1", " ".join(case.reasons))
        self.assertNotIn("sk-abcdefghijklmnopqrst1234", " ".join(case.reasons))

    def test_a_check_that_read_nothing_fails(self) -> None:
        # This is the whole point. The old gate passed here.
        case = self.run_case({"id": "keys", "kind": "secrets", "paths": ["nothing/*.py"]}, "s3")
        self.assertEqual(case.status, "failed")
        self.assertIn("nothing was checked", case.reasons[0])

    def test_nothing_is_allowed_unless_the_case_says_so(self) -> None:
        suite = qa.parse_suite({"name": "d", "cases": [{"id": "k", "kind": "secrets"}]})
        self.assertEqual(suite.cases[0].expect.max_findings, 0)
        self.assertEqual(suite.cases[0].paths, ("**/*",))

    def test_a_known_count_can_be_allowed_while_it_is_being_cleaned_up(self) -> None:
        (self.root / "settings.py").write_text(
            'KEY = "sk-abcdefghijklmnopqrst1234"\n', encoding="utf-8"
        )
        case = self.run_case(
            {"id": "keys", "kind": "secrets", "paths": ["*.py"], "expect": {"max_findings": 1}}, "s4"
        )
        self.assertEqual(case.status, "passed")

    def test_the_case_survives_a_round_trip(self) -> None:
        first = qa.parse_suite({"name": "d", "cases": [{
            "id": "k", "kind": "secrets", "paths": ["src/**/*.py"], "skip": ["src/fixtures/*"],
            "expect": {"max_findings": 2},
        }]})
        again = qa.parse_suite(json.loads(json.dumps(first.to_dict())))
        self.assertEqual(again.to_dict(), first.to_dict())

    def test_a_path_that_leaves_the_project_is_refused_when_the_suite_is_read(self) -> None:
        with self.assertRaises(HarnessError):
            qa.parse_suite({"name": "d", "cases": [{
                "id": "k", "kind": "secrets", "paths": ["../elsewhere/**/*"],
            }]})


if __name__ == "__main__":
    unittest.main()


class PatternMatchingTests(unittest.TestCase):
    """The file patterns, worked out once so a scan can skip whole folders."""

    def test_stars_mean_what_people_expect(self) -> None:
        cases = {
            "src/**/*.py": ["src/a.py", "src/deep/b.py", "src/deep/down/c.py"],
            "*.md": ["README.md"],
            "docs/*.md": ["docs/QA.md"],
            "**/*.json": ["a.json", "one/two/b.json"],
            "src/a?.py": ["src/ab.py"],
        }
        for pattern, should_match in cases.items():
            rule = scan.as_pattern(pattern)
            for name in should_match:
                with self.subTest(pattern=pattern, name=name):
                    self.assertTrue(rule.match(name))

    def test_a_pattern_does_not_match_more_than_it_says(self) -> None:
        misses = {
            "*.md": ["docs/QA.md", "README.txt"],
            "docs/*.md": ["docs/deep/QA.md", "other/QA.md"],
            "src/**/*.py": ["tests/a.py", "src/a.txt"],
        }
        for pattern, names in misses.items():
            rule = scan.as_pattern(pattern)
            for name in names:
                with self.subTest(pattern=pattern, name=name):
                    self.assertIsNone(rule.match(name))

    def test_a_dot_in_a_pattern_is_a_dot(self) -> None:
        rule = scan.as_pattern("a.py")
        self.assertTrue(rule.match("a.py"))
        self.assertIsNone(rule.match("axpy"))

    def test_folders_never_worth_reading_are_not_walked_into(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for folder in (".git", "node_modules", "src"):
                (root / folder).mkdir()
                (root / folder / "a.py").write_text("x = 1\n", encoding="utf-8")
            found = scan._files_under(root)
            self.assertEqual(found, ["src/a.py"])
