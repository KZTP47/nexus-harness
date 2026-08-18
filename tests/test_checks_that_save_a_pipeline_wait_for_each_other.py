"""A check that saves a pipeline has to hold the pipeline.

The panel keeps one pipeline on screen at a time, in one variable, and saving
writes one file and keeps the version it wrote over. Two checks doing that side
by side write over each other: one of them saves, the other saves the same thing
a moment later, and the version that should have been kept never was.

That is exactly how it failed - once in a hundred runs, in a check about
something else entirely, with nothing in the report to say why. Saying "these
two touch the same thing" is what stops it, and this holds every check that
saves to saying it.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FLOWS = ROOT / ".harness" / "qa" / "workflows.json"

# The words the suite uses for the one pipeline the panel has on screen.
THE_THING = "the pipeline on screen"

# The two ways a check saves one.
WAYS_OF_SAVING = ("pipelineSave", "/api/pipelines/save")


class ChecksThatSaveAPipelineWait(unittest.TestCase):
    def test_every_check_that_saves_one_says_it_touches_it(self) -> None:
        cases = json.loads(FLOWS.read_text(encoding="utf-8"))["cases"]
        free = []
        for case in cases:
            body = json.dumps(case)
            if not any(way in body for way in WAYS_OF_SAVING):
                continue
            if THE_THING not in (case.get("touches") or []):
                free.append(case["id"])
        self.assertEqual(
            free,
            [],
            "These checks save a pipeline without saying they touch it, so two "
            f'of them can run at once and lose a version. Add "touches": '
            f'["{THE_THING}"]:\n' + "\n".join(free),
        )

    def test_the_words_are_really_used_by_the_suite(self) -> None:
        """A guard that names a thing nobody uses guards nothing."""

        cases = json.loads(FLOWS.read_text(encoding="utf-8"))["cases"]
        holding = [one["id"] for one in cases if THE_THING in (one.get("touches") or [])]
        self.assertGreater(len(holding), 1, holding)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
