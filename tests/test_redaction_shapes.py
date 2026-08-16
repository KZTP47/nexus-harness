"""Every shape of credential the remover has been caught missing.

Each line here leaked once. They are kept together, with the ordinary text that
must survive untouched, because a credential remover is only worth anything if
both halves stay true.
"""

from __future__ import annotations

import copy
import time
import unittest
from pathlib import Path

from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.redaction import CredentialRedactor


class HiddenShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.remover = CredentialRedactor(None)

    def test_every_shape_that_once_leaked_is_hidden(self) -> None:
        cases = {
            "a bare token field": ("token=abc123SECRETTOKENvalue999", "abc123SECRETTOKENvalue999"),
            "a name with a prefix": (
                "auth_token: abc123SECRETTOKENvalue999", "abc123SECRETTOKENvalue999"
            ),
            "a json key": ('{"password": "hunter2hunter2"}', "hunter2hunter2"),
            "a json secret": ('{"secret": "mySuperSecretValue123"}', "mySuperSecretValue123"),
            "a json token": ('{"token": "abcXYZ999secretvalue"}', "abcXYZ999secretvalue"),
            "a fat arrow": ('password => "hunter2hunter2"', "hunter2hunter2"),
            "a basic header": (
                'curl -H "Authorization: Basic dXNlcjpwYXNzd29yZDEyMw=="',
                "dXNlcjpwYXNzd29yZDEyMw==",
            ),
            "a long amazon name": (
                'aws_secret_access_key = "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY"',
                "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY",
            ),
            "an address with a password": (
                'DB = "postgres://user:hunterhunter2@host/db"', "hunterhunter2"
            ),
            "a quoted key": ('api_key="sk-live-abcdefghij"', "sk-live-abcdefghij"),
            "a header name": ("X-Auth-Token: abcdefghijklmnop", "abcdefghijklmnop"),
            "a bearer header": (
                "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9abcdef", "eyJhbGciOiJIUzI1NiJ9abcdef"
            ),
        }
        for label, (line, secret) in cases.items():
            with self.subTest(shape=label):
                cleaned = self.remover.text(line)
                self.assertNotIn(secret, cleaned)
                self.assertIn("[REDACTED]", cleaned)

    def test_a_partly_hidden_value_never_happens(self) -> None:
        # A line that looks redacted while the secret is still in it is worse
        # than one that was left alone, because nobody looks twice.
        for line, secret in (
            ('password => "hunter2hunter2"', "hunter2hunter2"),
            ("password : hunter2hunter2", "hunter2hunter2"),
            ("password:hunter2hunter2", "hunter2hunter2"),
            ("PASSWORD = hunter2hunter2", "hunter2hunter2"),
        ):
            with self.subTest(line=line):
                cleaned = self.remover.text(line)
                self.assertNotIn(secret, cleaned)


class OrdinaryTextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.remover = CredentialRedactor(None)

    def test_ordinary_words_are_left_exactly_as_they_were(self) -> None:
        for line in (
            "we baked cookies: chocolate chip today",
            "The manager was secretive: he said nothing useful",
            "the token ring network was slow",
            "password rules are written in the docs",
            "name = Ada",
            "See the authorisation policy for details",
            "https://example.com/docs/passwords",
        ):
            with self.subTest(line=line):
                self.assertEqual(self.remover.text(line), line)

    def test_counts_of_tokens_are_numbers_worth_keeping(self) -> None:
        for line in ("input_tokens: 120", "output_tokens = 45", "billed_output_tokens: 7"):
            with self.subTest(line=line):
                self.assertEqual(self.remover.text(line), line)
        counted = self.remover.value(
            {"input_tokens": 12, "output_tokens": 34, "cached_input_tokens": 2}
        )
        self.assertEqual(counted, {"input_tokens": 12, "output_tokens": 34, "cached_input_tokens": 2})

    def test_a_real_credential_field_is_still_hidden_in_an_object(self) -> None:
        hidden = self.remover.value({"access_token": "abcdef123456", "api_key": "sk-live-abc"})
        self.assertEqual(hidden, {"access_token": "[REDACTED]", "api_key": "[REDACTED]"})


class SpeedTests(unittest.TestCase):
    """Removing credentials must not be what makes the harness look stuck."""

    def setUp(self) -> None:
        self.remover = CredentialRedactor(None)

    def test_a_large_run_of_text_with_a_trigger_word_is_quick(self) -> None:
        # This shape took fifteen seconds once: one word at the end made the
        # whole file be picked apart.
        text = ("x" * 5_000_000) + "secret"
        started = time.monotonic()
        self.remover.text(text)
        self.assertLess(time.monotonic() - started, 5.0)

    def test_a_log_line_followed_by_a_lot_of_output_is_quick(self) -> None:
        text = "password authentication failed for user admin " + ("QUJDREVGRw" * 500_000)
        started = time.monotonic()
        self.remover.text(text)
        self.assertLess(time.monotonic() - started, 5.0)

    def test_many_ordinary_pairs_are_quick(self) -> None:
        started = time.monotonic()
        self.remover.text("name: value " * 200_000)
        self.assertLess(time.monotonic() - started, 5.0)

    def test_many_credential_pairs_are_quick(self) -> None:
        started = time.monotonic()
        cleaned = self.remover.text("password: hunter2hunter2 " * 50_000)
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertNotIn("hunter2hunter2", cleaned)


class WhatReachesAModelTests(unittest.TestCase):
    def test_nothing_a_check_saw_reaches_a_model_in_any_of_these_shapes(self) -> None:
        from our_harness import handover

        evidence = "\n".join([
            "token=abc123SECRETTOKENvalue999",
            'DB = "postgres://user:hunterhunter2@host/db"',
            'aws_secret_access_key = "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY"',
            '{"password": "jsonhunter2value"}',
            "Authorization: Basic dXNlcjpwYXNzd29yZDEyMw==",
        ])
        question = handover.failure_question(
            {"id": "a", "title": "A check", "kind": "http", "reasons": ["boom"]}, evidence
        )
        for secret in (
            "abc123SECRETTOKENvalue999",
            "hunterhunter2",
            "wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY",
            "jsonhunter2value",
            "dXNlcjpwYXNzd29yZDEyMw==",
        ):
            with self.subTest(secret=secret[:20]):
                self.assertNotIn(secret, question)
        self.assertIn("boom", question)


class NestedShellTests(unittest.TestCase):
    """A shell line hidden inside a language one-liner is still a shell line."""

    def runner(self):
        from our_harness.execution import CommandRunner

        return CommandRunner(LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), Path.cwd(), [], {}))

    def test_a_shell_inside_a_one_liner_forgives_nothing(self) -> None:
        from our_harness.models import HarnessError

        for argv in (
            ["python", "-c", "import os; os.system('cmd /c format C: /y')"],
            ["python3", "-c", "import subprocess; subprocess.run(['cmd','/c','format','C:','/y'])"],
            ["node", "-e", "require('child_process').execSync('cmd /c format C: /y')"],
            ["python", "-c", "import os; os.system('bash -c format')"],
        ):
            with self.subTest(argv=argv[0]), self.assertRaises(HarnessError):
                self.runner()._check(argv)

    def test_ordinary_code_with_format_in_it_still_runs(self) -> None:
        for argv in (
            ["python", "-c", "print('{}'.format(1))"],
            ["python", "-m", "pytest", "-q"],
            ["node", "build.js"],
            ["npm", "run", "format"],
        ):
            with self.subTest(argv=argv[:3]):
                self.runner()._check(argv)


if __name__ == "__main__":
    unittest.main()


class PluralAndAwkwardValueTests(unittest.TestCase):
    """Names in the plural, and values holding the characters that broke it."""

    def setUp(self) -> None:
        self.remover = CredentialRedactor(None)

    def test_plural_credential_names_are_still_credentials(self) -> None:
        for line, secret in (
            ('{"credentials": "actualsecretvalue123"}', "actualsecretvalue123"),
            ('{"passwords": "hunter2list987"}', "hunter2list987"),
            ('{"secrets": "topsecretvalue555"}', "topsecretvalue555"),
            ('{"api_keys": "sk-live-abcdefghij"}', "sk-live-abcdefghij"),
            ('{"private_keys": "abcdefghijklmnop"}', "abcdefghijklmnop"),
        ):
            with self.subTest(line=line):
                cleaned = self.remover.text(line)
                self.assertNotIn(secret, cleaned)

    def test_a_value_holding_a_quote_or_a_brace_is_hidden_whole(self) -> None:
        # Half a redaction is worse than none: the line looks safe and is not.
        for line, leftover in (
            ('password = "it\'s-a-secret123"', "s-a-secret123"),
            ("token: abc{def}ghi", "{def}ghi"),
            ("secret: one'two'three", "two'three"),
            ('password: a"b"c', 'b"c'),
        ):
            with self.subTest(line=line):
                cleaned = self.remover.text(line)
                self.assertNotIn(leftover, cleaned)
                self.assertIn("[REDACTED]", cleaned)

    def test_the_shape_around_the_value_survives(self) -> None:
        self.assertEqual(
            self.remover.text('{"password": "x", "other": 2}'),
            '{"password": "[REDACTED]", "other": 2}',
        )
        self.assertEqual(self.remover.text("password=x;"), "password=[REDACTED];")

    def test_ordinary_plurals_are_still_ordinary(self) -> None:
        for line in (
            "we baked cookies: chocolate chip today",
            "input_tokens: 120",
            "the tokens: two of them",
        ):
            with self.subTest(line=line):
                self.assertEqual(self.remover.text(line), line)


class DirectSpawnTests(unittest.TestCase):
    """A denied program named straight to a process call, with no shell in sight."""

    def runner(self):
        from our_harness.execution import CommandRunner

        return CommandRunner(LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), Path.cwd(), [], {}))

    def test_a_denied_program_started_directly_from_code_is_refused(self) -> None:
        from our_harness.models import HarnessError

        for argv in (
            ["python", "-c", "import subprocess; subprocess.run(['format','C:','/y'])"],
            ["node", "-e", "require('child_process').execFileSync('format', ['C:'])"],
            ["python", "-c", "import subprocess; subprocess.Popen(['diskpart'])"],
            ["ruby", "-e", "spawn('mkfs', '/dev/sda')"],
        ):
            with self.subTest(argv=argv[0]), self.assertRaises(HarnessError):
                self.runner()._check(argv)

    def test_a_method_call_of_the_same_name_still_runs(self) -> None:
        for argv in (
            ["python", "-c", "print('{}'.format(1))"],
            ["python", "-c", "x = 'a'.format()"],
            ["python", "-c", "import json; print(json.dumps({}))"],
            ["node", "-e", "console.log(new Date().toISOString())"],
            ["python", "-m", "pytest", "-q"],
        ):
            with self.subTest(argv=argv[-1][:30]):
                self.runner()._check(argv)


class EscapedAndMultiLineValueTests(unittest.TestCase):
    """A secret with a quote or a line break inside it is still one secret."""

    def setUp(self) -> None:
        self.remover = CredentialRedactor(None)

    def test_a_quote_written_with_a_backslash_does_not_cut_the_secret_in_half(self) -> None:
        line = '{"password": "pass\\"word123secret"}'
        cleaned = self.remover.text(line)
        self.assertNotIn("word123secret", cleaned)
        self.assertIn("[REDACTED]", cleaned)

    def test_a_secret_running_over_a_line_break_is_hidden_all_the_way(self) -> None:
        cleaned = self.remover.text('password: "line1\nrealsecretvalue"')
        self.assertNotIn("realsecretvalue", cleaned)
        self.assertIn("[REDACTED]", cleaned)

    def test_a_single_quoted_secret_with_an_escape_is_hidden_whole(self) -> None:
        cleaned = self.remover.text(r"password = 'don\'t-tell-anyone-99'")
        self.assertNotIn("tell-anyone-99", cleaned)

    def test_a_loose_apostrophe_does_not_end_the_secret_early(self) -> None:
        # 'don' is a whole quoted value on its own, and reading it that way left
        # the rest of the secret sitting right next to the word REDACTED.
        cleaned = self.remover.text("password = 'don't-tell-anyone-99'")
        self.assertNotIn("tell-anyone-99", cleaned)
        self.assertIn("[REDACTED]", cleaned)

    def test_the_next_field_after_a_quoted_secret_is_left_alone(self) -> None:
        self.assertEqual(
            self.remover.text('{"password": "x", "other": 2}'),
            '{"password": "[REDACTED]", "other": 2}',
        )

    def test_a_quote_that_is_never_closed_does_not_eat_the_whole_file(self) -> None:
        rest = "ordinary line\n" * 2000
        cleaned = self.remover.text('password: "never closed\n' + rest)
        # It hides more than it has to, which is the safe way round, but it must
        # stop long before the end of a large file.
        self.assertIn("ordinary line", cleaned)

    def test_it_is_still_quick_with_escapes_everywhere(self) -> None:
        text = 'password: "' + ('a\\"b' * 100_000) + '"'
        started = time.monotonic()
        self.remover.text(text)
        self.assertLess(time.monotonic() - started, 5.0)


class UnterminatedQuoteTests(unittest.TestCase):
    """Text cut off part way through a secret is the ordinary case, not a rare one."""

    def setUp(self) -> None:
        self.remover = CredentialRedactor(None)

    def test_a_secret_cut_off_before_its_closing_quote_is_hidden_to_the_end_of_the_line(self) -> None:
        line = (
            'password: "xxxxxxxxxxxxxxxxxxxx '
            "thisIsStillTheRealSecretAfterASpaceyyyyyyyyyyyyyyyyyyyy"
        )
        cleaned = self.remover.text(line)
        self.assertNotIn("thisIsStillTheRealSecretAfterASpace", cleaned)
        self.assertIn("[REDACTED]", cleaned)

    def test_the_line_after_it_is_left_alone(self) -> None:
        cleaned = self.remover.text(
            'password: "cut off here and there\nordinary line that means nothing'
        )
        self.assertIn("ordinary line that means nothing", cleaned)

    def test_a_single_quote_cut_off_the_same_way_is_hidden_too(self) -> None:
        cleaned = self.remover.text("api_key: 'sk-live-abc def ghi jkl")
        self.assertNotIn("def ghi jkl", cleaned)

    def test_a_properly_closed_value_is_still_read_the_narrow_way(self) -> None:
        # The wide rule must not swallow the rest of a line that was fine.
        self.assertEqual(
            self.remover.text('{"password": "x", "other": 2}'),
            '{"password": "[REDACTED]", "other": 2}',
        )
        self.assertEqual(
            self.remover.text('password: "abc" and then some ordinary words'),
            'password: "[REDACTED]" and then some ordinary words',
        )

    def test_it_is_still_quick_when_many_lines_open_a_quote_and_never_close_it(self) -> None:
        text = 'password: "never closed here at all\n' * 50_000
        started = time.monotonic()
        cleaned = self.remover.text(text)
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertNotIn("never closed here at all", cleaned)
