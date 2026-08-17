"""Splitting a suite across machines.

A long run is made short by giving each machine a part of it. The danger is
obvious: a machine that ran a quarter of the checks and said "all checks
passed" is worse than no run at all. So every part is written down in the
result and in the report, and anything that is not two plain numbers is
refused rather than guessed at.
"""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from our_harness import qa
from our_harness.cli import _which_part
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError


def suite_of(how_many: int) -> qa.QaSuite:
    return qa.parse_suite({
        "name": "d",
        "cases": [
            {"id": f"c{number:02d}", "kind": "file", "path": "x"} for number in range(how_many)
        ],
    })


class ReadingThePartTests(unittest.TestCase):
    def test_two_numbers(self) -> None:
        self.assertEqual(_which_part("2/4"), (2, 4))
        self.assertEqual(_which_part(" 1 / 3 "), (1, 3))
        self.assertEqual(_which_part("2 of 4"), (2, 4))
        self.assertEqual(_which_part("2-4"), (2, 4))

    def test_nothing_means_the_whole_suite(self) -> None:
        self.assertEqual(_which_part(""), (0, 0))

    def test_nonsense_is_refused(self) -> None:
        for bad in ("half", "2", "2/", "/4", "a/b", "2/4/6", "0/4", "5/4", "2/0", "2/1000", "-1/4"):
            with self.subTest(bad=bad):
                with self.assertRaises(HarnessError):
                    _which_part(bad)


class SplittingTheSuiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name).resolve()
        (root / ".harness").mkdir()
        self.runner = qa.QaRunner(LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), root, [], {}))

    def parts(self, how_many: int, of: int) -> list[list[str]]:
        suite = suite_of(how_many)
        return [
            [case.id for case in self.runner.select(suite, part=(number, of))]
            for number in range(1, of + 1)
        ]

    def test_the_parts_together_are_the_whole_suite(self) -> None:
        # The one that matters. A check that falls between two parts is a check
        # nobody runs, and nobody would notice.
        for how_many, of in ((84, 4), (10, 3), (7, 7), (100, 6), (5, 2)):
            with self.subTest(how_many=how_many, of=of):
                got = [case for part in self.parts(how_many, of) for case in part]
                self.assertEqual(sorted(got), [f"c{n:02d}" for n in range(how_many)])
                self.assertEqual(len(got), len(set(got)), "a check ran in two parts")

    def test_the_parts_are_about_the_same_size(self) -> None:
        sizes = [len(part) for part in self.parts(84, 4)]
        self.assertEqual(sizes, [21, 21, 21, 21])
        sizes = [len(part) for part in self.parts(10, 3)]
        self.assertEqual(max(sizes) - min(sizes), 1)

    def test_neighbouring_checks_go_to_different_parts(self) -> None:
        # Checks written next to each other are usually alike and take about as
        # long, so dealing them out beats cutting the list into blocks.
        first = self.parts(8, 4)[0]
        self.assertEqual(first, ["c00", "c04"])

    def test_the_same_part_twice_holds_the_same_checks(self) -> None:
        self.assertEqual(self.parts(84, 4)[1], self.parts(84, 4)[1])

    def test_more_parts_than_checks_is_refused(self) -> None:
        with self.assertRaises(qa.QaError) as caught:
            self.runner.select(suite_of(3), part=(4, 5))
        self.assertIn("fewer checks than parts", str(caught.exception))

    def test_a_part_outside_the_count_is_refused(self) -> None:
        with self.assertRaises(qa.QaError):
            self.runner.select(suite_of(10), part=(9, 4))

    def test_a_part_can_be_narrowed_by_tag_as_well(self) -> None:
        suite = qa.parse_suite({
            "name": "d",
            "cases": [
                {"id": f"c{n}", "kind": "file", "path": "x", "tags": ["slow" if n % 2 else "fast"]}
                for n in range(8)
            ],
        })
        got = [case.id for case in self.runner.select(suite, tags=["fast"], part=(1, 2))]
        self.assertEqual(got, ["c0", "c4"])


class ChecksThatShareAThingStayTogetherTests(unittest.TestCase):
    """Holding two checks apart only works while they are on the same machine.

    Split across four build servers, each one has its own idea of what is busy.
    Two checks that must never overlap would start at the same instant on two
    machines, against whatever they both change.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name).resolve()
        (root / ".harness").mkdir()
        self.runner = qa.QaRunner(LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), root, [], {}))

    def sharing(self, how_many: int, sharing: int) -> qa.QaSuite:
        cases = []
        for number in range(how_many):
            case = {"id": f"c{number:02d}", "kind": "file", "path": "x"}
            if number < sharing:
                case["touches"] = ["the sign-in page"]
            cases.append(case)
        return qa.parse_suite({"name": "d", "cases": cases})

    def test_they_all_land_in_the_same_part(self) -> None:
        for how_many, sharing, of in ((84, 6, 4), (20, 5, 3), (12, 2, 4), (30, 9, 6)):
            with self.subTest(how_many=how_many, sharing=sharing, of=of):
                where = {}
                for number in range(1, of + 1):
                    for case in self.runner.select(self.sharing(how_many, sharing), part=(number, of)):
                        where[case.id] = number
                parts = {where[f"c{n:02d}"] for n in range(sharing)}
                self.assertEqual(len(parts), 1, "checks that share a thing were split up")

    def test_two_things_shared_through_a_third_check_still_stay_together(self) -> None:
        # A touches the vault, B touches the vault and the settings, C touches
        # the settings. All three have to stay together, because B overlaps
        # both of the others.
        suite = qa.parse_suite({"name": "d", "cases": [
            {"id": "a", "kind": "file", "path": "x", "touches": ["the vault"]},
            {"id": "b", "kind": "file", "path": "x", "touches": ["the vault", "the settings"]},
            {"id": "c", "kind": "file", "path": "x", "touches": ["the settings"]},
            {"id": "d", "kind": "file", "path": "x"},
            {"id": "e", "kind": "file", "path": "x"},
            {"id": "f", "kind": "file", "path": "x"},
        ]})
        where = {}
        for number in (1, 2):
            for case in self.runner.select(suite, part=(number, 2)):
                where[case.id] = number
        self.assertEqual(len({where["a"], where["b"], where["c"]}), 1)

    def test_everything_is_still_covered_exactly_once(self) -> None:
        suite = self.sharing(30, 9)
        covered = [case.id for number in (1, 2, 3) for case in self.runner.select(suite, part=(number, 3))]
        self.assertEqual(sorted(covered), [f"c{n:02d}" for n in range(30)])
        self.assertEqual(len(covered), len(set(covered)))

    def test_one_big_group_does_not_leave_a_machine_idle(self) -> None:
        # Nine checks sharing one thing all go to one machine. The other
        # twenty-one should be spread over the rest rather than piled on too.
        suite = self.sharing(30, 9)
        sizes = [len(self.runner.select(suite, part=(number, 3))) for number in (1, 2, 3)]
        self.assertEqual(sum(sizes), 30)
        self.assertLessEqual(max(sizes), 12, sizes)


class SayingWhichPartRanTests(unittest.TestCase):
    """A run of one part must never read like a run of all of it."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        (self.root / "here.txt").write_text("x", encoding="utf-8")
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})

    def result(self, part: tuple[int, int]) -> qa.QaRunResult:
        suite = qa.parse_suite({
            "name": "d",
            "cases": [{"id": f"c{n}", "kind": "file", "path": "here.txt"} for n in range(4)],
        })
        return qa.QaRunner(self.config).run(suite, part=part, write_artifacts=False)

    def test_the_report_says_which_part_ran(self) -> None:
        report = qa.render_report(self.result((2, 4)), "markdown", None)
        self.assertIn("part 2 of 4", report)
        self.assertIn("says nothing about them", report)

    def test_a_whole_run_says_nothing_about_parts(self) -> None:
        report = qa.render_report(self.result((0, 0)), "markdown", None)
        self.assertNotIn("part", report.lower().split("| case")[0])

    def test_every_kind_of_report_says_which_part_ran(self) -> None:
        # A build server reads the machine-readable file, not the one meant for
        # a person. Saying "all checks passed" there about a quarter of the
        # suite is the whole mistake this feature exists to prevent.
        part = self.result((2, 4))
        whole = self.result((0, 0))
        for kind in ("markdown", "html", "junit"):
            with self.subTest(kind=kind):
                said = qa.render_report(part, kind, None)
                self.assertIn("2", said)
                self.assertIn("part", said.lower())
                self.assertNotIn("part", qa.render_report(whole, kind, None).lower().replace(
                    "partial", ""
                ).split("<table")[0].split("| case")[0])

    def test_the_json_report_says_which_part_ran(self) -> None:
        self.assertEqual(self.result((2, 4)).to_dict()["part"], {"number": 2, "of": 4})
        self.assertIsNone(self.result((0, 0)).to_dict()["part"])

    def test_only_that_part_actually_ran(self) -> None:
        result = self.result((2, 4))
        self.assertEqual([case.id for case in result.cases], ["c1"])
        self.assertTrue(result.passed)


if __name__ == "__main__":
    unittest.main()
