"""Run the Python tests, or one part of them.

    python scripts/run_tests.py                # all of them
    python scripts/run_tests.py --part 2/8     # the second part of eight
    python scripts/run_tests.py --list         # just say which files would run

CI runs eight independent parts on Python 3.13, with a separate short Python
3.11 compatibility check instead of another full suite. Each full-suite part
runs serially in its own interpreter so tests cannot race shared process state.
The parts are dealt out like cards rather than cut into blocks. Eight-way CI
also places a measured slow module in a less busy part. Increasing the number
of parts reduces work per machine without changing suite coverage.

Every part together is every test file. Nothing falls between two parts, and
nothing runs twice. tests/test_the_parts_cover_every_test.py holds that down.
"""

from __future__ import annotations

import argparse
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"

# A complete part-8 timing profile found the swarm suite dominates its runtime.
# Keep that placement, but separate the growing HTTP/board suite: together they
# took 698 seconds in run 34361226193, exhausting part 3's job deadline.
# Part 4 finished substantially earlier and can own the complete board module.
EIGHT_PART_ASSIGNMENTS = {"test_swarm_work": 3, "test_the_board_of_agents": 4}
# Keep the measured pre-toolbox deal stable. In run 34468734077 inserting these
# modules into the sorted deck shifted existing owners and timed out part 4.
# These short additions fit in part 5 without moving any existing module.
EIGHT_PART_ADDITIONS = {"test_full_access_tool_runtime": 5, "test_harness_tools": 5}


def every_test_file() -> list[str]:
    """Every test module, in a settled order."""

    return sorted(path.stem for path in TESTS.glob("test_*.py"))


def which_part(said: str) -> tuple[int, int]:
    """Read "2/4" as: the second part of four. Nothing means all of them."""

    said = str(said or "").strip()
    if not said:
        return (0, 0)
    parts = said.replace(" of ", "/").replace("-", "/").split("/")
    if len(parts) != 2 or not all(re.fullmatch(r"[0-9]+", item.strip()) for item in parts):
        raise SystemExit("Write which part to run as two numbers, like --part 2/4")
    number, of = int(parts[0]), int(parts[1])
    if of < 1 or of > 100:
        raise SystemExit("Split the tests into between 1 and 100 parts.")
    if not 1 <= number <= of:
        raise SystemExit(f"There is no part {number} of {of}. Number the parts from 1 up to {of}.")
    return (number, of)


def files_for(part: tuple[int, int], files: list[str] | None = None) -> list[str]:
    """The test files one part covers."""

    names = list(files if files is not None else every_test_file())
    number, of = part
    if not of:
        return names
    if of == 8:
        baseline = [name for name in names if name not in EIGHT_PART_ADDITIONS]
        owners = {name: EIGHT_PART_ASSIGNMENTS.get(name, index % of + 1)
                  for index, name in enumerate(baseline)}
        owners.update(EIGHT_PART_ADDITIONS)
        return [
            name for name in names if owners[name] == number
        ]
    return names[number - 1::of]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--part", default="", help="Run one part of the tests, written as 2/8")
    parser.add_argument("--list", action="store_true", help="Say which files would run, and stop")
    parser.add_argument("--quiet", action="store_true", help="One progress character per test rather than its full name")
    args = parser.parse_args(argv)

    part = which_part(args.part)
    names = files_for(part)
    if not names:
        raise SystemExit(
            f"Part {part[0]} of {part[1]} holds no test files. There are fewer files than "
            "parts, so some machines would have nothing to do."
        )
    if args.list:
        for name in names:
            print(name)
        return 0

    # Running this file directly makes ``scripts`` (rather than the repository
    # root) Python's first import location.  Some tests intentionally exercise
    # release scripts as importable modules, so every invocation mode needs the
    # same repository-root import semantics as ``python -m unittest``.
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(TESTS))
    if part[1]:
        print(f"Part {part[0]} of {part[1]}: {len(names)} of {len(every_test_file())} test files.")
        print("The other parts run somewhere else, and this run says nothing about them.")
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.loadTestsFromNames(names))
    result = unittest.TextTestRunner(verbosity=1 if args.quiet else 2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
