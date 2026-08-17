"""The panel's own script has to parse before anybody opens the panel.

A stray character in app.js does not fail a Python test, does not fail the
audit, and does not fail anything at all until somebody opens the panel and
finds every button dead. It has happened three times while writing this, each
time from a newline landing inside a piece of text.

So this asks the machine's own JavaScript to read the file. It is the same
check the browser does, done in a tenth of a second instead of after a panel is
started, a page is loaded, and a check has failed for a reason that looks like
something else entirely.
"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "src" / "our_harness" / "ui"


def scripts() -> list[Path]:
    return sorted(PANEL.glob("*.js")) + sorted((ROOT / "desktop").glob("*.js"))


def _a_string_runs_off_the_end(line: str) -> bool:
    """Is a piece of text opened on this line and never closed?

    Read left to right. Outside text, a comment ends the line and a slash may
    start a regular expression, which is left alone: quotation marks inside one
    are just characters. Inside text, a backslash swallows what follows.
    """

    inside = ""
    spot = 0
    while spot < len(line):
        letter = line[spot]
        if inside:
            if letter == "\\":
                spot += 2
                continue
            if letter == inside:
                inside = ""
            spot += 1
            continue
        if letter == "/" and spot + 1 < len(line) and line[spot + 1] in "/*":
            return False
        if letter == "/":
            # A regular expression, or a division. Either way the rest of the
            # line is not worth reading: this is looking for one mistake, and
            # guessing at which of those it is would find mistakes that are not
            # there.
            return False
        if letter in "\"'`":
            inside = letter
        spot += 1
    # A back-tick may hold a real newline on purpose, which is what it is for.
    return inside in ("\"", "'")


class ThePanelScriptParsesTests(unittest.TestCase):
    def test_there_are_scripts_to_read(self) -> None:
        # Without this, moving the panel would make the test below pass by
        # finding nothing to check.
        self.assertTrue(scripts())
        self.assertTrue(any(path.name == "app.js" for path in scripts()))

    def test_every_script_parses(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not on this machine, so it cannot read the scripts")
        broken: list[str] = []
        for path in scripts():
            finished = subprocess.run(
                [node, "--check", str(path)],
                capture_output=True, text=True, timeout=60, check=False,
            )
            if finished.returncode != 0:
                first = (finished.stderr or "").strip().splitlines()
                broken.append(f"{path.name}: {first[-1] if first else 'would not parse'}")
        self.assertEqual(broken, [], "These scripts do not parse: " + "; ".join(broken))

    def test_no_line_of_text_is_left_hanging(self) -> None:
        # The way it has gone wrong every time: a piece of text with a real
        # newline inside it. Node catches that, and so does this, so a machine
        # with no Node still cannot ship it.
        #
        # Counting quotation marks is not enough - "I don't care" has an odd
        # number and is perfectly fine - so this reads each line the way a
        # reader does: once a piece of text is opened, everything until the
        # matching mark is inside it, and a backslash means the next character
        # is part of the text whatever it is.
        hanging: list[str] = []
        for path in scripts():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if _a_string_runs_off_the_end(line):
                    hanging.append(f"{path.name}:{number}")
        self.assertEqual(hanging, [], "A piece of text runs off the end of these lines: "
                         + ", ".join(hanging))


if __name__ == "__main__":
    unittest.main()
