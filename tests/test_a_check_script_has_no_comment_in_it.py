"""A comment in a check's script swallows the rest of the check.

Every script in a check is squeezed onto one line before the browser is given
it. Written across several lines, a two-slash comment reads fine. Squeezed onto
one, everything after the two slashes is a comment too - the click, the waiting,
the answer, all of it. The browser then says "Unexpected end of input", which
points at nothing in particular and takes an afternoon to understand.

That is exactly what happened while writing the timer checks, so it is held here
rather than left for the next person.

The two slashes in `http://` are not a comment, and those are left alone.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FLOWS = ROOT / ".harness" / "qa" / "workflows.json"

# Two slashes that do not follow a colon, so `http://` and `https://` pass.
A_COMMENT = re.compile(r"(?<!:)//")


class ACheckScriptHasNoCommentInIt(unittest.TestCase):
    def test_no_script_on_one_line_has_a_comment_in_it(self) -> None:
        cases = json.loads(FLOWS.read_text(encoding="utf-8"))["cases"]
        with_a_comment = []
        for case in cases:
            for spot, step in enumerate(case.get("steps", []), 1):
                script = step.get("script", "")
                if not isinstance(script, str) or "\n" in script:
                    continue
                found = A_COMMENT.search(script)
                if found:
                    with_a_comment.append(
                        f"{case['id']} step {spot}: ...{script[max(0, found.start() - 40):found.start() + 40]}..."
                    )
        self.assertEqual(
            with_a_comment,
            [],
            "These scripts are one line with a comment in them, so everything "
            "after the two slashes never runs. Say it in the step's note "
            "instead:\n" + "\n".join(with_a_comment),
        )

    def test_it_knows_a_web_address_from_a_comment(self) -> None:
        self.assertIsNone(A_COMMENT.search("await goTo('http://127.0.0.1:8765/')"))
        self.assertIsNotNone(A_COMMENT.search("const one = 1; // and the rest"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
