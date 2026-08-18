"""Start the panel from a checkout, without anybody setting PYTHONPATH first.

The code lives in src/, so `python -m our_harness` only works when something
has already put src on the path. That is fine in a terminal where you set it
once, and it is a trap everywhere else: the panel is started by editors, by
launchers and by the checks, and every one of those has its own idea of what
the environment holds. This puts src on the path itself and starts the panel.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from our_harness.cli import main  # noqa: E402  (the path has to come first)

if __name__ == "__main__":
    sys.exit(main([*sys.argv[1:]] or ["ui"]))
