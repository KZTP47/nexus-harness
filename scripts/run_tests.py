"""Run the Python tests, or one part of them.

    python scripts/run_tests.py                # all of them
    python scripts/run_tests.py --part 2/4     # the second part of four
    python scripts/run_tests.py --list         # just say which files would run

Splitting the tests is how a long run is made short: four machines each take a
quarter and the wait is a quarter as long. The parts are dealt out like cards
rather than cut into blocks, so files written next to each other - which tend
to be alike, and to take about as long - land on different machines.

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
    return names[number - 1::of]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--part", default="", help="Run one part of the tests, written as 2/4")
    parser.add_argument("--list", action="store_true", help="Say which files would run, and stop")
    parser.add_argument("--quiet", action="store_true", help="One line per file rather than per test")
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
