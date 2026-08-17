"""Two checks that change the same thing must not run at the same time.

Checks run several at a time, which is what makes a big suite quick. It also
means two checks that write to the same place stand on each other's work: one
empties a folder while the other is counting what is in it, and the run fails
for a reason that is nothing to do with the code being checked.

A check can now say what it touches. Everything below is about that promise
being kept.
"""

from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path

from our_harness import qa
from our_harness.config import DEFAULT_CONFIG, LoadedConfig


class TouchesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})


class ReadingItTests(TouchesTestCase):
    def test_a_check_can_say_what_it_touches(self) -> None:
        suite = qa.parse_suite({
            "name": "d",
            "cases": [{
                "id": "one", "kind": "file", "path": "x",
                "touches": ["The Vault", "the settings file"],
            }],
        })
        self.assertEqual(suite.cases[0].touches, ("the vault", "the settings file"))

    def test_it_is_written_back_out_again(self) -> None:
        suite = qa.parse_suite({
            "name": "d",
            "cases": [{"id": "one", "kind": "file", "path": "x", "touches": ["the vault"]}],
        })
        self.assertEqual(suite.cases[0].to_dict()["touches"], ["the vault"])

    def test_a_check_that_touches_nothing_says_nothing(self) -> None:
        suite = qa.parse_suite({"name": "d", "cases": [{"id": "one", "kind": "file", "path": "x"}]})
        self.assertEqual(suite.cases[0].touches, ())
        self.assertNotIn("touches", suite.cases[0].to_dict())

    def test_nonsense_is_refused_while_the_suite_is_read(self) -> None:
        for bad in ("not a list", ["a" * 60], [""], ["../escape"], ["Two\nlines"], [1]):
            with self.subTest(bad=bad):
                with self.assertRaises(qa.QaError):
                    qa.parse_suite({
                        "name": "d",
                        "cases": [{"id": "one", "kind": "file", "path": "x", "touches": bad}],
                    })

    def test_the_same_thing_twice_is_only_one_thing(self) -> None:
        suite = qa.parse_suite({
            "name": "d",
            "cases": [{
                "id": "one", "kind": "file", "path": "x",
                "touches": ["the vault", "The vault"],
            }],
        })
        self.assertEqual(suite.cases[0].touches, ("the vault",))


class WaitingForEachOtherTests(TouchesTestCase):
    """The part that matters: they really do wait."""

    # Two checks that meet at the gate both go through it. Two that never meet
    # leave one of them waiting until the time runs out. Neither answer depends
    # on how fast this machine happens to be today, which is what a test about
    # things running at the same time usually gets wrong.
    def kind(self, gate: threading.Barrier, busy: dict):
        lock = threading.Lock()

        def run(case, runner):
            with lock:
                busy["now"] += 1
                busy["most"] = max(busy["most"], busy["now"])
            try:
                gate.wait(timeout=5)
                busy["met"] = True
            except threading.BrokenBarrierError:
                pass
            with lock:
                busy["now"] -= 1
            return (), "", ""

        return qa.validated_kinds([
            qa.CheckKind(name="slow", summary="A check that waits at a gate", run=run)
        ])

    def watch(self, cases: list[dict]) -> dict:
        gate = threading.Barrier(2)
        busy = {"now": 0, "most": 0, "met": False}
        kinds = self.kind(gate, busy)
        suite = qa.parse_suite({"name": "d", "cases": cases}, extra_kinds=kinds)
        runner = qa.QaRunner(self.config, extra_kinds=kinds)
        result = runner.run(suite, workers=4, write_artifacts=False)
        self.assertTrue(all(case.status in ("passed", "failed") for case in result.cases))
        return busy

    def test_checks_that_touch_the_same_thing_never_overlap(self) -> None:
        busy = self.watch([
            {"id": f"c{number}", "kind": "slow", "touches": ["the vault"]}
            for number in range(4)
        ])
        self.assertEqual(busy["most"], 1, "two checks changing the vault ran at the same time")
        self.assertFalse(busy["met"], "two of them reached the gate together")

    def test_checks_that_touch_nothing_still_run_together(self) -> None:
        # Holding checks apart is only worth having if it costs nothing when
        # there is nothing to hold apart.
        busy = self.watch([{"id": f"c{number}", "kind": "slow"} for number in range(4)])
        self.assertTrue(busy["met"], "checks that share nothing were made to queue up")

    def test_different_things_do_not_hold_each_other_up(self) -> None:
        busy = self.watch([
            {"id": "a", "kind": "slow", "touches": ["the vault"]},
            {"id": "b", "kind": "slow", "touches": ["the settings"]},
            {"id": "c", "kind": "slow", "touches": ["the notes"]},
            {"id": "d", "kind": "slow", "touches": ["the pipelines"]},
        ])
        self.assertTrue(busy["met"], "four checks changing four different things queued up")

    def test_two_checks_holding_two_things_do_not_get_stuck(self) -> None:
        # The old way of getting this wrong: one check takes A then B while
        # another takes B then A, and both wait for the other forever. They are
        # always taken in the same order, so that cannot happen.
        busy = self.watch([
            {"id": "a", "kind": "slow", "touches": ["the vault", "the settings"]},
            {"id": "b", "kind": "slow", "touches": ["the settings", "the vault"]},
            {"id": "c", "kind": "slow", "touches": ["the settings", "the vault"]},
        ])
        self.assertEqual(busy["most"], 1)


class TheRealSuiteTests(unittest.TestCase):
    """The checks in this project's own suite say what they change."""

    def test_every_check_that_writes_notes_says_it_touches_the_vault(self) -> None:
        written = Path(__file__).resolve().parents[1] / ".harness/qa/workflows.json"
        suite = qa.parse_suite(json.loads(written.read_text(encoding="utf-8")))
        forgot = [
            case.id
            for case in suite.cases
            # Reading counts too: a check reading the notes while another one
            # writes them sees a vault that is halfway through changing.
            if any("/api/vault" in str(step.get("script") or "") for step in case.steps)
            and "the vault" not in case.touches
        ]
        self.assertEqual(
            forgot,
            [],
            "These checks write notes and do not say they touch the vault, so they can "
            "run at the same time as each other and undo each other's work: "
            + ", ".join(forgot),
        )


if __name__ == "__main__":
    unittest.main()
