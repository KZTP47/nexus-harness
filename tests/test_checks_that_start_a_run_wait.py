"""A check that starts a run of the checks has to hold the runner.

The panel runs one at a time. Ask it to start another while one is going and it
says "A check run is already active" and refuses - which is the right answer to
a person, and a failure to a check that was not expecting it.

Two checks in the suite both pressed a button that starts a run. Most of the
time one finished first. Now and again they overlapped, and the second failed
with a message about something it was not testing, in a run about something
else, with nothing in the report to say why. Saying "these two touch the same
thing" is what stops it.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FLOWS = ROOT / ".harness" / "qa" / "workflows.json"

# The words the suite uses for the one runner the panel has.
THE_THING = "the check runner"

# Every way a check can set one going.
WAYS_OF_STARTING_ONE = (
    "/api/qa/run", "/api/qa/baseline", "/api/qa/record", "/api/qa/pick",
    "runChecks", "saveBaselines", "recordSteps", "pickElement", "quickChecks",
)


class ChecksThatStartARunWait(unittest.TestCase):
    def test_every_check_that_starts_one_says_it_touches_the_runner(self) -> None:
        cases = json.loads(FLOWS.read_text(encoding="utf-8"))["cases"]
        free = []
        for case in cases:
            body = json.dumps(case)
            if not any(way in body for way in WAYS_OF_STARTING_ONE):
                continue
            if THE_THING not in (case.get("touches") or []):
                free.append(case["id"])
        self.assertEqual(
            free,
            [],
            "These checks start a run of the checks without saying they touch "
            "the runner, so two of them can overlap and one gets told no. Add "
            f'"touches": ["{THE_THING}"]:\n' + "\n".join(free),
        )

    def test_the_words_are_really_used_by_the_suite(self) -> None:
        """A guard that names a thing nobody uses guards nothing."""

        cases = json.loads(FLOWS.read_text(encoding="utf-8"))["cases"]
        holding = [one["id"] for one in cases if THE_THING in (one.get("touches") or [])]
        self.assertGreater(len(holding), 1, holding)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
