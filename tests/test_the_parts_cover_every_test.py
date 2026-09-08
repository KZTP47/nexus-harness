"""Splitting the tests across machines must not drop any of them.

The build server runs the complete suite in eight parts on Python 3.13 and a
focused compatibility check on Python 3.11. If one test file fell between two parts, nobody
would run it and nobody would notice: the build would still be green, and the
file would rot.
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

    def test_eight_way_balancing_moves_the_slow_module_without_moving_others(self) -> None:
        names = self.split.every_test_file()
        self.assertIn("test_swarm_work", names, "The timed module must still exist")
        owners = {
            name: number
            for number in range(1, 9)
            for name in self.split.files_for((number, 8), names)
        }
        self.assertEqual(owners["test_swarm_work"], 3)
        for index, name in enumerate(names):
            if name != "test_swarm_work":
                with self.subTest(name=name):
                    self.assertEqual(owners[name], index % 8 + 1)

    def test_absent_timed_module_leaves_the_normal_eight_way_split(self) -> None:
        names = [f"test_{letter}" for letter in "abcdefghijklmnopq"]
        for number in range(1, 9):
            with self.subTest(number=number):
                self.assertEqual(
                    self.split.files_for((number, 8), names), names[number - 1::8]
                )

    def test_runtime_balancing_does_not_change_other_partition_sizes(self) -> None:
        names = self.split.every_test_file()
        for of in (1, 2, 3, 4, 6, 7, 9, 100):
            for number in range(1, of + 1):
                with self.subTest(of=of, number=number):
                    self.assertEqual(
                        self.split.files_for((number, of), names), names[number - 1::of]
                    )

    def test_balancing_is_repeatable_and_keeps_the_supplied_module_order(self) -> None:
        names = list(reversed(self.split.every_test_file()))
        original = list(names)
        for number in range(1, 9):
            with self.subTest(number=number):
                selected = self.split.files_for((number, 8), names)
                self.assertEqual(self.split.files_for((number, 8), names), selected)
                self.assertEqual(selected, [name for name in original if name in selected])
        self.assertEqual(names, original, "Selecting a part must not mutate its input")

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
        job = workflow[workflow.index("  tests:\n"):workflow.index("  python-compatibility:\n")]
        self.assertIn('python-version: "3.13"', job)
        self.assertIn("scripts/run_tests.py --part ${{ matrix.part }}/8 --quiet", job)
        self.assertIn("part: [1, 2, 3, 4, 5, 6, 7, 8]", job)
        self.assertIn("max-parallel: 8", job)
        self.assertNotIn("needs:", job, "Complete Python coverage must not wait on another lane")
        covered = [name for part in range(1, 9) for name in self.split.files_for((part, 8))]
        self.assertCountEqual(covered, self.split.every_test_file())
        self.assertEqual(len(covered), len(set(covered)))
        compatibility = workflow[workflow.index("  python-compatibility:\n"):workflow.index("  panel:\n")]
        self.assertIn('python-version: "3.11"', compatibility)
        self.assertNotIn("scripts/run_tests.py", compatibility)
        self.assertNotIn("matrix:", compatibility)

    def test_the_source_panel_lane_installs_declared_runtime_dependencies(self) -> None:
        # PYTHONPATH makes Nexus's own modules importable, but it does not
        # install LangGraph or make child commands independent of that one CI
        # environment variable. The panel lane is meant to model a supported
        # source installation, so require that installation before it starts.
        workflow = (ROOT / ".github" / "workflows" / "checks.yml").read_text(encoding="utf-8")
        panel = workflow[workflow.index("  panel:\n"):workflow.index("  project-checks:\n")]
        install = panel.index("python -m pip install -e .")
        start = panel.index("Start the panel and run the selected checks")
        self.assertLess(install, start)

    def test_consolidating_panel_setup_preserves_every_selected_case_serially(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "checks.yml").read_text(encoding="utf-8")
        panel = workflow[workflow.index("  panel:\n"):workflow.index("  project-checks:\n")]
        self.assertNotIn("matrix:", panel)
        self.assertNotIn("--part", panel, "A single panel lane must not keep an obsolete shard filter")
        self.assertIn("--suite .harness/qa/workflows.json", panel)
        self.assertIn("--tag swarm --tag pipelines", panel)
        self.assertIn("--workers 1", panel, "Panel cases share mutable boards and pipelines")
        self.assertIn("$counts.skipped -gt 0", panel)
        self.assertIn("$counts.total -lt 1", panel)

    def test_desktop_keeps_one_packaging_test_and_real_runtime_acceptance(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "checks.yml").read_text(encoding="utf-8")
        desktop = workflow[workflow.index("  desktop:\n"):workflow.index("  package:\n")]
        self.assertEqual(desktop.count("run: npm test"), 1)
        self.assertNotIn("run: node packaging.test.js", desktop)
        build = desktop.index("npm run build -- --win dir")
        for command in (
            "npm run e2e:multi-vendor",
            "node --test ui-navigation.test.js",
            "npm run smoke:team-chat",
            "npm run smoke:browser-long-horizon",
        ):
            with self.subTest(command=command):
                self.assertGreater(desktop.index(command), build)


if __name__ == "__main__":
    unittest.main()
