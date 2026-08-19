"""A check that changes the workflow on screen has to hold it.

The panel keeps one workflow at a time, in one variable, and every change posts
the whole thing to be looked over. Two checks changing it side by side send each
other's half-finished graphs, and the panel is answered "no" for a workflow
neither of them meant to send. The browser writes that to its console, and a
check that allows no errors in its console fails - in a run about something else
entirely, once in a hundred, with nothing in the report to say why.

It happened exactly like that. Saying "these two touch the same thing" is what
stops it, and this holds every check that changes the workflow to saying so.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FLOWS = ROOT / ".harness" / "qa" / "workflows.json"

# The words the suite uses for the one workflow the panel has on screen.
THE_THING = "the workflow on screen"

# The ways a check changes it, or asks for it to be looked over.
WAYS_OF_CHANGING_IT = ("/api/validate", "graph.nodes", "nodeRole", "graph =")


class ChecksThatChangeTheWorkflowWait(unittest.TestCase):
    def test_every_check_that_changes_it_says_it_touches_it(self) -> None:
        cases = json.loads(FLOWS.read_text(encoding="utf-8"))["cases"]
        free = []
        for case in cases:
            body = json.dumps(case)
            if not any(way in body for way in WAYS_OF_CHANGING_IT):
                continue
            if THE_THING not in (case.get("touches") or []):
                free.append(case["id"])
        self.assertEqual(
            free,
            [],
            "These checks change the workflow on screen without saying they "
            "touch it, so two of them can run at once and send each other's "
            f'half-finished graphs. Add "touches": ["{THE_THING}"]:\n'
            + "\n".join(free),
        )

    def test_the_words_are_really_used_by_the_suite(self) -> None:
        """A guard that names a thing nobody uses guards nothing."""

        cases = json.loads(FLOWS.read_text(encoding="utf-8"))["cases"]
        holding = [one["id"] for one in cases if THE_THING in (one.get("touches") or [])]
        self.assertGreater(len(holding), 1, holding)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
