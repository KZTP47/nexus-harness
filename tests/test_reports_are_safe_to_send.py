"""Every report the harness writes for a person to read or send.

A report gets attached to a ticket, committed beside the build, or pasted into
a chat. What a check saw is a program's own output, and a program prints
whatever it was given, keys included. So each of these has to be clean, and
each of them has to still say what went wrong.
"""

from __future__ import annotations

import unittest

from our_harness.qa import QaAttempt, QaCaseResult, QaRunResult, render_report

SECRETS = ("sk-live-abcdefghijklmno", "hunter2hunter2", "eyJhbGciOiJIUzI1NiJ9abcdef")


def leaky_run() -> QaRunResult:
    return QaRunResult(
        run_id="20260101-000001",
        suite_name="mine",
        started_at="2026-01-01T00:00:00Z",
        duration_ms=5,
        workers=1,
        cases=(
            QaCaseResult(
                id="sign-in",
                title="Signing in works",
                kind="http",
                status="failed",
                duration_ms=5,
                reasons=("connecting with api_key=sk-live-abcdefghijklmno gave 401",),
                attempts=(
                    QaAttempt(
                        number=1,
                        passed=False,
                        duration_ms=5,
                        evidence=(
                            '{"password": "hunter2hunter2"}\n'
                            "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9abcdef"
                        ),
                    ),
                ),
            ),
            QaCaseResult(
                id="unit-tests", title="The unit tests pass", kind="command",
                status="passed", duration_ms=3,
            ),
        ),
    )


class WrittenReportTests(unittest.TestCase):
    def test_no_report_a_person_reads_carries_a_credential(self) -> None:
        for kind in ("markdown", "junit", "html"):
            written = render_report(leaky_run(), kind)
            for secret in SECRETS:
                with self.subTest(kind=kind, secret=secret[:16]):
                    self.assertNotIn(secret, written)
            self.assertIn("[REDACTED]", written)

    def test_every_report_still_says_which_check_failed_and_why(self) -> None:
        for kind in ("markdown", "junit", "html"):
            written = render_report(leaky_run(), kind)
            with self.subTest(kind=kind):
                self.assertIn("sign-in", written)
                self.assertIn("401", written)

    def test_a_credential_in_the_name_or_title_of_a_check_is_hidden_too(self) -> None:
        run = QaRunResult(
            run_id="r", suite_name="mine", started_at="now", duration_ms=1, workers=1,
            cases=(QaCaseResult(
                id="api-key-sk-live-abcdefghijklmno",
                title='the check for password="hunter2hunter2"',
                kind="command", status="failed", duration_ms=1, reasons=("no",),
            ),),
        )
        for kind in ("markdown", "junit", "html"):
            written = render_report(run, kind)
            with self.subTest(kind=kind):
                self.assertNotIn("sk-live-abcdefghijklmno", written)
                self.assertNotIn("hunter2hunter2", written)

    def test_ordinary_words_in_a_report_are_left_exactly_as_they_were(self) -> None:
        run = QaRunResult(
            run_id="r", suite_name="mine", started_at="now", duration_ms=1, workers=1,
            cases=(QaCaseResult(
                id="sign-in", title="Signing in works", kind="browser",
                status="failed", duration_ms=1,
                reasons=("the button moved, so nothing was clicked",),
            ),),
        )
        for kind in ("markdown", "junit", "html"):
            written = render_report(run, kind)
            with self.subTest(kind=kind):
                self.assertIn("the button moved", written)
                self.assertNotIn("[REDACTED]", written)

    def test_the_machine_record_is_left_as_it_is_and_that_is_on_purpose(self) -> None:
        # The run folder already holds this, unchanged, like a log file. Hiding
        # things in one copy and not the other would only give false comfort.
        written = render_report(leaky_run(), "json")
        self.assertIn("sk-live-abcdefghijklmno", written)

    def test_the_junit_report_is_still_readable_by_a_build_server(self) -> None:
        from xml.etree import ElementTree

        tree = ElementTree.fromstring(render_report(leaky_run(), "junit"))
        self.assertEqual(tree.tag, "testsuites")
        names = [node.get("id") for node in tree.iter("testcase")]
        self.assertIn("sign-in", names)


if __name__ == "__main__":
    unittest.main()
