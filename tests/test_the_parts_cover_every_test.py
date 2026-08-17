"""Splitting the tests across machines must not drop any of them.

The build server runs the tests in four parts at once, so a green build is four
green parts. If one test file fell between two parts, nobody would run it and
nobody would notice: the build would still be green, and the file would rot.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def splitter():
    where = ROOT / "scripts" / "run_tests.py"
    spec = importlib.util.spec_from_file_location("run_tests", where)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SplittingTheTestsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.split = splitter()

    def test_it_finds_the_test_files_at_all(self) -> None:
        # Without this, a change that broke the search would make everything
        # below pass by covering nothing.
        found = self.split.every_test_file()
        self.assertGreater(len(found), 50)
        self.assertIn(Path(__file__).stem, found)

    def test_every_test_file_is_in_exactly_one_part(self) -> None:
        for of in (2, 3, 4, 6, 8):
            with self.subTest(of=of):
                covered = [
                    name
                    for number in range(1, of + 1)
                    for name in self.split.files_for((number, of))
                ]
                self.assertEqual(sorted(covered), self.split.every_test_file())
                self.assertEqual(len(covered), len(set(covered)), "a file ran in two parts")

    def test_no_part_means_all_of_them(self) -> None:
        self.assertEqual(self.split.files_for((0, 0)), self.split.every_test_file())

    def test_the_parts_are_about_the_same_size(self) -> None:
        sizes = [len(self.split.files_for((number, 4))) for number in range(1, 5)]
        self.assertLessEqual(max(sizes) - min(sizes), 1)

    def test_neighbouring_files_go_to_different_parts(self) -> None:
        made_up = [f"test_{letter}" for letter in "abcdefgh"]
        self.assertEqual(
            self.split.files_for((1, 4), made_up), ["test_a", "test_e"]
        )

    def test_nonsense_is_refused(self) -> None:
        for bad in ("half", "2", "0/4", "5/4", "2/0", "a/b"):
            with self.subTest(bad=bad):
                with self.assertRaises(SystemExit):
                    self.split.which_part(bad)

    def test_the_build_server_asks_for_the_parts_this_can_give(self) -> None:
        # The workflow file and this splitter have to agree about how many
        # parts there are. If somebody raises one and not the other, some tests
        # stop running and the build stays green.
        workflow = (ROOT / ".github" / "workflows" / "checks.yml").read_text(encoding="utf-8")
        self.assertIn("scripts/run_tests.py --part ${{ matrix.part }}/4", workflow)
        self.assertIn("part: [1, 2, 3, 4]", workflow)


if __name__ == "__main__":
    unittest.main()
