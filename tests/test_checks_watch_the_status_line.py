"""A check that reads the one status line has to watch it, not glance at it.

The checks view has a single line that says what just happened. Anything that
finishes writes there: the button you pressed, and also a run finishing in
another tab, or another check running against the same harness.

A check that presses a button and then reads that line every tenth of a second
is racing everything else that writes to it. Most of the time it wins. Twice in
one afternoon it did not, and both times the check failed for a reason that had
nothing to do with what it was checking.

Watching the line instead — keeping every value it takes — cannot lose. This
holds every check to that, so the next one written is right on the day it is
written rather than the day it goes wrong.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FLOWS = ROOT / ".harness" / "qa" / "workflows.json"

# The line everything writes to.
THE_LINE = "checkStatus"
WATCHING = "MutationObserver"


# A check may set the watching up in one step and read what it saw in a later
# one, by keeping the list on the page. That is still watching, so a step is
# judged against its whole check, not on its own.
KEPT_ON_THE_PAGE = "__everySaid"


def steps_that_wait_on_the_line():
    """Steps that ask the line itself to say something, and only glance at it.

    An expect_text on that line is a glance every tenth of a second, which is
    the very thing this file is about - and it was not looked at here until a
    check about packing up evidence failed because somebody else's "All 1 checks
    passed." landed on the line first.
    """

    cases = json.loads(FLOWS.read_text(encoding="utf-8"))["cases"]
    for case in cases:
        for spot, step in enumerate(case.get("steps", []), 1):
            if step.get("do", "").startswith("expect") and THE_LINE in str(
                step.get("target", "")
            ):
                yield case["id"], spot, step.get("note", "")


def scripts_that_read_the_line():
    cases = json.loads(FLOWS.read_text(encoding="utf-8"))["cases"]
    for case in cases:
        watched_earlier = False
        for spot, step in enumerate(case.get("steps", []), 1):
            script = step.get("script", "")
            if THE_LINE in script:
                yield case["id"], spot, script, watched_earlier
            if WATCHING in script and KEPT_ON_THE_PAGE in script:
                watched_earlier = True


class WatchTheLineTests(unittest.TestCase):
    def test_there_are_checks_that_read_it(self) -> None:
        # Without this, a change that broke the reading would make the test
        # below pass by finding nothing at all.
        self.assertGreaterEqual(len(list(scripts_that_read_the_line())), 4)

    def test_every_check_that_reads_the_line_watches_it(self) -> None:
        glancing = [
            f"{case} step {spot}"
            for case, spot, script, watched_earlier in scripts_that_read_the_line()
            # Reading it once, with nothing pressed in the same step, cannot
            # lose a sentence it never waited for.
            if "for (" in script and WATCHING not in script and not watched_earlier
        ]
        self.assertEqual(
            glancing,
            [],
            "These check steps wait for words on the checks-view status line without "
            "watching it: " + ", ".join(glancing) + ". Anything else writing to that "
            "line wipes the sentence before they look. Install a MutationObserver on "
            "it before pressing anything, and ask whether it ever held the words.",
        )

    def test_the_watching_is_set_up_before_anything_is_pressed(self) -> None:
        # A watcher installed after the press has already missed it.
        too_late = []
        for case, spot, script, _earlier in scripts_that_read_the_line():
            if WATCHING not in script or "click()" not in script:
                continue
            if script.index(WATCHING) > script.index("click()"):
                too_late.append(f"{case} step {spot}")
        self.assertEqual(too_late, [], "the watcher goes in before the press")


if __name__ == "__main__":
    unittest.main()


class NoCheckWaitsOnTheLineWithoutWatchingIt(unittest.TestCase):
    def test_nothing_expects_text_on_the_one_line_everything_writes_to(self) -> None:
        glancing = [
            f"{case} step {spot}: {note}"
            for case, spot, note in steps_that_wait_on_the_line()
        ]
        self.assertEqual(
            glancing,
            [],
            "These steps wait for words on the one line everything writes to, "
            "which is a glance every tenth of a second and loses to anything "
            "else that finishes first. Watch the line in a run step instead, "
            "keeping everything it says:\n" + "\n".join(glancing),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
