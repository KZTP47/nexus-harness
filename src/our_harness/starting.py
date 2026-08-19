"""How to start this harness from here.

Three parts of the harness hand somebody a command line and expect it to work
later: the timer writes the line for the machine's own scheduler, the editor
setup writes the line to paste into an editor, and the desktop app starts the
panel itself. All three used to write `python -m our_harness`, and all three
were wrong in the same way for anybody who had only downloaded the project.

The code lives in a `src` folder, which is the ordinary shape of a Python
project and is not somewhere Python looks by itself. Nothing said so. Every
Python on the machine answered "No module named our_harness", the desktop app
showed three of those, and there was nothing in it anybody could act on.

So the question is asked once, here, and answered the same way everywhere.
"""

from __future__ import annotations

import sys
from pathlib import Path


def a_launcher() -> Path:
    """The little script that puts the code on the path and starts the harness.

    It sits in the project's own scripts folder, beside the code it starts.
    """

    return Path(__file__).resolve().parents[2] / "scripts" / "harness.py"


def is_it_installed() -> bool:
    """Is the harness somewhere Python finds on its own?

    Judged on where its own file is: inside a `src` folder means somebody is
    running it out of a download, and Python only found it because something
    put that folder on the path for this one command. A scheduler starting the
    same line tomorrow morning has nothing doing that.
    """

    return "src" not in Path(__file__).resolve().parts


def how_to_start_the_harness(argv0: str | None = None) -> list[str]:
    """The command that starts this harness, however it got onto this machine.

    Three ways it can be here, and all three have to work:

      - Installed into Python. `-m our_harness` finds it, and that is the
        shortest thing to write down.
      - Downloaded and run from the folder. The launcher is used, because it
        puts the code on the path itself and needs nothing set beforehand.
      - Built into one file. That file is the command.
    """

    from_a_file = Path(argv0 or sys.argv[0] or "").resolve()
    if from_a_file.suffix == ".pyz" and from_a_file.is_file():
        return [sys.executable, str(from_a_file)]
    if from_a_file.name.lower() in {"harness", "harness.exe"} and from_a_file.is_file():
        return [str(from_a_file)]
    if not is_it_installed():
        launcher = a_launcher()
        if launcher.is_file():
            return [sys.executable, str(launcher)]
    return [sys.executable, "-m", "our_harness"]
