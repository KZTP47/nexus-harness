"""Start the harness from a download, with nothing set up beforehand.

The code lives in src/, so `python -m our_harness` only works when something
has already put src on the path. That is fine in a terminal where you set it
once, and it is a trap everywhere else: the harness is started by editors, by
the machine's own scheduler, by the desktop app and by the checks, and every
one of those has its own idea of what the environment holds. Somebody who has
only downloaded the project has none of it.

This puts src on the path itself. With no arguments it opens the panel, which
is what somebody double-clicking it wants; with arguments it is the harness,
so anything that can write one command line can write this one.
"""

from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info < (3, 11):
    found = ".".join(str(part) for part in sys.version_info[:3])
    raise SystemExit(f"Nexus Harness requires Python 3.11 or newer; this interpreter is {found}.")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from our_harness.cli import main  # noqa: E402  (the path has to come first)

if __name__ == "__main__":
    sys.exit(main([*sys.argv[1:]] or ["ui"]))
