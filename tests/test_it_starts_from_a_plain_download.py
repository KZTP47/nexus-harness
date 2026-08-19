"""Every line the harness hands out has to start the harness.

Three parts of it write a command line for somebody or something to run later:
the timer writes the line for the machine's own scheduler, the editor setup
writes the line to paste into an editor, and the desktop app starts the panel
itself. All three wrote `python -m our_harness`.

That is right for anybody who has installed the harness into Python, and wrong
for everybody who has only downloaded it - which is everybody, the first time.
The code lives in a `src` folder, which Python does not look in by itself. The
desktop app showed three copies of "No module named our_harness" and nothing
anybody could act on. The timer's line would have failed at two in the morning,
months later, with nobody watching.

Nothing caught it because every test here already had the folder on the path.
So these run the real command with the path taken away.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from our_harness import editor, timer
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.starting import (
    a_launcher,
    how_to_start_the_harness,
    is_it_installed,
)

ROOT = Path(__file__).resolve().parents[1]


def with_nothing_set_up() -> dict[str, str]:
    """This machine's environment, with the helping hand taken away.

    Everything in this project is run with the code's folder named in
    PYTHONPATH. That is what hid this for so long: the tests never saw what
    somebody who had just downloaded it sees.
    """

    return {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}


class TheLauncherIsReallyThere(unittest.TestCase):
    def test_it_is_where_the_answer_says_it_is(self) -> None:
        self.assertTrue(a_launcher().is_file(), str(a_launcher()))

    def test_it_starts_the_harness_with_nothing_set_up(self) -> None:
        finished = subprocess.run(
            [sys.executable, str(a_launcher()), "--version"],
            capture_output=True, text=True, env=with_nothing_set_up(), timeout=120,
        )
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertIn("harness", finished.stdout)

    def test_it_passes_what_it_is_given_through(self) -> None:
        finished = subprocess.run(
            [sys.executable, str(a_launcher()), "tell", "kinds"],
            capture_output=True, text=True, env=with_nothing_set_up(), timeout=120,
        )
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertIn("needs a key", finished.stdout)

    def test_the_plain_way_really_does_fail_from_a_download(self) -> None:
        """If this ever passes, the harness is installed on this machine and
        the rest of these tests are proving nothing. Better to be told."""

        if is_it_installed():
            self.skipTest("the harness is installed here, so there is nothing to prove")
        finished = subprocess.run(
            [sys.executable, "-m", "our_harness", "--version"],
            capture_output=True, text=True, env=with_nothing_set_up(),
            cwd=str(ROOT), timeout=120,
        )
        self.assertNotEqual(finished.returncode, 0)
        self.assertIn("our_harness", finished.stderr)


class EveryLineItHandsOutStartsIt(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})

    def really_runs(self, command: list[str]) -> None:
        finished = subprocess.run(
            [*command, "--version"],
            capture_output=True, text=True, env=with_nothing_set_up(), timeout=120,
        )
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertIn("harness", finished.stdout)

    def test_the_one_answer_starts_it(self) -> None:
        self.really_runs(how_to_start_the_harness())

    def test_the_line_for_an_editor_starts_it(self) -> None:
        how = editor.how_to_tell_your_editor(self.config)
        # Everything up to the arguments that say what to do.
        upto = how["arguments"].index("--project")
        self.really_runs([how["command"], *how["arguments"][:upto]])

    def test_the_line_for_the_machine_names_the_launcher(self) -> None:
        """The scheduler's line cannot be run here - it would really put a job
        on this machine - so it is read rather than run. What it has to say is
        that it starts the harness the way that works from a download."""

        said = timer.how_to_ask_this_machine(self.config)["what"]
        if is_it_installed():
            self.skipTest("the harness is installed here, so the line is the short one")
        self.assertIn(a_launcher().name, said)

    def test_a_built_one_file_harness_is_used_as_it_is(self) -> None:
        one_file = self.root / "harness.pyz"
        one_file.write_text("not really a zipapp, but it is a file", encoding="utf-8")
        self.assertEqual(
            how_to_start_the_harness(str(one_file)), [sys.executable, str(one_file)]
        )


class TheDesktopAppTakesItsOwnCodeWithIt(unittest.TestCase):
    """The app is JavaScript and its own tests are run by node. This is the
    part that can be checked wherever the harness is checked."""

    def test_it_puts_the_code_on_the_path_itself(self) -> None:
        said = (ROOT / "desktop" / "server.js").read_text(encoding="utf-8")
        self.assertIn("PYTHONPATH", said, "it sets the path for the harness it starts")
        self.assertIn("whereTheHarnessLives", said)
        self.assertIn("our_harness", said)

    def test_it_still_starts_with_the_module_name(self) -> None:
        """Which is right once somebody has installed it, and is why the path
        is set rather than the command changed."""

        said = (ROOT / "desktop" / "server.js").read_text(encoding="utf-8")
        self.assertIn('"-m", "our_harness"', said)

    def test_its_own_tests_cover_this(self) -> None:
        said = (ROOT / "desktop" / "server.test.js").read_text(encoding="utf-8")
        self.assertIn("whereTheHarnessLives", said)
        self.assertIn("PYTHONPATH", said)


class NothingElseHandsOutTheShortLine(unittest.TestCase):
    def test_no_shipped_code_writes_it_out_by_hand(self) -> None:
        """Written out in three places, it was wrong in three places. Anything
        that needs it asks starting.py."""

        allowed = {"starting.py"}
        found = []
        for path in (ROOT / "src" / "our_harness").rglob("*.py"):
            if path.name in allowed:
                continue
            # Only what really runs. A comment saying what it used to say is
            # how a file explains itself, and telling somebody off for that is
            # how a guard gets turned off.
            said = "\n".join(
                line
                for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()
                if not line.strip().startswith("#")
            )
            if '"-m", "our_harness"' in said or "-m our_harness" in said:
                found.append(path.name)
        self.assertEqual(found, [], "these write the start line themselves")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
