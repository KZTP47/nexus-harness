"""The panel may only ask a question in a way the desktop app really has.

Seven buttons did nothing at all in the app. Every one of them asked for a line
of text with `window.prompt`, and Electron does not have it - it is the one
browser thing they took out on purpose. No box appeared, no error was printed,
nothing happened. Pressing Rename simply did nothing, for months.

In a browser every one of them worked perfectly, which is exactly why nothing
caught it: every browser check runs in a browser.

So the panel asks with a box of its own, and this holds it to that. It also
allows the two the app really does have - alert and confirm - because taking
those away would be a different job and would not have found this one.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "src" / "our_harness" / "ui" / "app.js"

# What the app does not have.
#
# Every way of writing the same call, not just the two obvious ones: window,
# globalThis, top, self and parent all reach it, and so does taking a reference
# to it first. Written as one pattern rather than a list of spellings, because
# a list of spellings is a list somebody adds to after it has already gone
# wrong.
NOT_IN_THE_APP = re.compile(
    # The global itself, reached by any of its names, however it is used -
    # including taking a reference to it and calling that later.
    r"(?:window|globalThis|self|top|parent)\.prompt(?![\w$])"
    # Or a bare call. Not the word on its own: a step of an automation has
    # a setting called prompt, and telling somebody off for that is how a
    # guard gets turned off.
    r"|(?<![\w.$\-])prompt\s*\("
)

# The box the panel uses instead.
THE_WAY_TO_ASK = "askForOneLine"


def what_really_runs(path: Path) -> str:
    """The file with its comments taken out.

    A comment explaining why something is not used is how a file explains
    itself, and telling somebody off for that is how a guard gets turned off.
    """

    kept = []
    for line in path.read_text(encoding="utf-8").splitlines():
        said = line.strip()
        if said.startswith("//") or said.startswith("*") or said.startswith("/*"):
            continue
        kept.append(line)
    return "\n".join(kept)


class ThePanelOnlyAsksInWaysTheAppHas(unittest.TestCase):
    def test_nothing_asks_with_prompt(self) -> None:
        said = what_really_runs(PANEL)
        found = [
            number
            for number, line in enumerate(said.splitlines(), 1)
            if NOT_IN_THE_APP.search(line)
        ]
        self.assertEqual(
            found,
            [],
            "These lines ask with prompt, which the desktop app does not have, "
            "so the button does nothing at all there. Ask with "
            f"{THE_WAY_TO_ASK} instead:\n" + "\n".join(str(one) for one in found),
        )

    def test_there_is_a_way_to_ask_that_does_work(self) -> None:
        said = PANEL.read_text(encoding="utf-8")
        self.assertIn(f"function {THE_WAY_TO_ASK}", said)
        self.assertIn("askDialog", said, "and it uses a box on the page")

    def test_the_box_it_asks_with_is_really_in_the_page(self) -> None:
        page = (ROOT / "src" / "our_harness" / "ui" / "index.html").read_text(
            encoding="utf-8"
        )
        for needed in ("askDialog", "askDialogInput", "askDialogOk", "askDialogCancel"):
            with self.subTest(needed=needed):
                self.assertIn(f'id="{needed}"', page)

    def test_it_knows_a_real_one_from_a_comment(self) -> None:
        """A guard that cannot tell those apart gets turned off the first time
        somebody writes an honest comment."""

        self.assertIsNotNone(NOT_IN_THE_APP.search("const x = prompt('hello');"))
        self.assertIsNotNone(NOT_IN_THE_APP.search("const x = window.prompt('hi');"))
        self.assertIsNotNone(NOT_IN_THE_APP.search("globalThis.prompt('hi');"))
        self.assertIsNotNone(NOT_IN_THE_APP.search("const p = window.prompt;"))
        self.assertIsNotNone(NOT_IN_THE_APP.search("top.prompt('hi');"))
        self.assertIsNone(NOT_IN_THE_APP.search("askForOneLine('hello');"))
        self.assertIsNone(NOT_IN_THE_APP.search("this.promptCache = 1;"))
        self.assertIsNone(NOT_IN_THE_APP.search("said.prompt_history = [];"))
        # A step of an automation really has a setting called this.
        self.assertIsNone(NOT_IN_THE_APP.search("prompt: $('teamCustomPrompt').value,"))
        self.assertIsNone(NOT_IN_THE_APP.search("if (!one.prompt.trim()) {"))
        self.assertIsNone(NOT_IN_THE_APP.search('make("p", "team-node-prompt",'))


class TheCheckThatRunsInTheRealApp(unittest.TestCase):
    """A guard on the source is not the same as pressing the button.

    The source guard here would have caught this the day it was written. It was
    not written, because nobody knew. So there is also a check that starts the
    app somebody installs, presses Rename by the word on it, types in the box
    and looks at whether the name really changed.
    """

    def test_it_is_there_and_can_be_run(self) -> None:
        where = ROOT / "desktop" / "asking.smoke.js"
        self.assertTrue(where.is_file(), str(where))
        said = where.read_text(encoding="utf-8")
        self.assertIn("askDialog", said, "it waits for the box to open")
        self.assertIn("Rename", said, "and finds the button by the word on it")
        self.assertIn("projectBarName", said, "and looks at what really changed")

    def test_there_is_a_way_to_run_it(self) -> None:
        import json

        package = json.loads(
            (ROOT / "desktop" / "package.json").read_text(encoding="utf-8")
        )
        self.assertIn("smoke:asking", package["scripts"])
        self.assertIn(
            "!asking.smoke.js", package["build"]["files"],
            "and it stays out of what gets shipped",
        )

    def test_it_puts_the_name_back_whatever_happens(self) -> None:
        """A check that leaves a project called "Renamed by a check" is a check
        somebody has to tidy up after."""

        said = (ROOT / "desktop" / "asking.smoke.js").read_text(encoding="utf-8")
        self.assertIn("finally", said)
        self.assertIn("was put back", said)


class TheBrowserChecksRunTheWayTheAppDoes(unittest.TestCase):
    def test_the_runner_takes_prompt_away_before_any_page_loads(self) -> None:
        """One change that upgrades every browser check at once: they all now
        run somewhere as unforgiving as where people use this."""

        said = (ROOT / "src" / "our_harness" / "qa.py").read_text(encoding="utf-8")
        self.assertIn("addInitScript", said)
        self.assertIn("prompt() is and will not be supported", said)

    def test_no_check_stubs_prompt_any_more(self) -> None:
        """Setting window.prompt in a check would put the forgiving browser
        back, one check at a time."""

        import json

        suite = json.loads(
            (ROOT / ".harness" / "qa" / "workflows.json").read_text(encoding="utf-8")
        )
        found = []
        for case in suite["cases"]:
            for step in case.get("steps", []):
                script = str(step.get("script") or "")
                if "window.prompt" not in script:
                    continue
                # A check may make it stricter - the sweep makes it throw, the
                # way the app does. What is not allowed is putting an answering
                # prompt back, because that is the forgiving browser again.
                where = script.index("window.prompt")
                if "throw" in script[where:where + 200]:
                    continue
                found.append(case["id"])
        self.assertEqual(
            sorted(set(found)),
            [],
            "These checks give prompt an answer, which is the forgiving browser "
            "again:\n" + "\n".join(sorted(set(found))),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
