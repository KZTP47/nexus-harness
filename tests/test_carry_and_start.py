"""Carrying a setup to another machine, and setting one up in one command.

The rule that matters most here is what does not travel. The file that names
the tools on your machine, the addresses you call, and the variables holding
your keys is the whole reason there are two settings files, and a feature that
carried it to somebody else's laptop would undo that in one step.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from our_harness import carry, pipelines
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError


class CarryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness" / "qa").mkdir(parents=True)
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        (self.root / ".harness" / "config.json").write_text(
            json.dumps({"qa": {"workers": 3}}), encoding="utf-8"
        )
        (self.root / ".harness" / "qa" / "suite.json").write_text(
            json.dumps({"schema_version": 1, "name": "mine", "cases": []}), encoding="utf-8"
        )
        pipelines.save(self.config, pipelines.a_starting_pipeline())

    def elsewhere(self) -> tuple[Path, LoadedConfig]:
        another = tempfile.TemporaryDirectory()
        self.addCleanup(another.cleanup)
        root = Path(another.name).resolve()
        (root / ".harness").mkdir()
        return root, LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), root, [], {})


class WhatTravelsTests(CarryTestCase):
    def test_the_settings_the_checks_and_the_pipelines_travel(self) -> None:
        packed = carry.pack(self.config)
        self.assertIn(".harness/config.json", packed["files"])
        self.assertIn(".harness/qa/suite.json", packed["files"])
        self.assertIn(".harness/pipelines", packed["files"])
        self.assertTrue(packed["holds"])

    def test_your_own_settings_file_never_travels(self) -> None:
        # The one that would matter. It names the tools on your machine, the
        # addresses you call, and the variables holding your keys.
        (self.root / ".harness" / "config.local.json").write_text(
            json.dumps({
                "providers": {"mine": {"kind": "claude-cli", "model": "x", "endpoint": ""}},
                "provider": {"name": "claude-cli", "model": "x",
                             "endpoint": "", "api_key_env": "MY_SECRET_NAME"},
            }),
            encoding="utf-8",
        )
        packed = json.dumps(carry.pack(self.config))
        self.assertNotIn("config.local", packed)
        self.assertNotIn("MY_SECRET_NAME", packed)
        self.assertNotIn("claude-cli", packed)
        self.assertIn("never travels", json.dumps(carry.pack(self.config)["left_out"]))

    def test_nothing_a_run_wrote_travels(self) -> None:
        runs = self.root / ".harness" / "qa" / "runs" / "20260101-000000"
        runs.mkdir(parents=True)
        (runs / "evidence.json").write_text(json.dumps({"secret": "x"}), encoding="utf-8")
        (self.root / ".harness" / "pipelines" / "last-run.json").write_text(
            json.dumps([{"id": "a", "said": "something a run said"}]), encoding="utf-8"
        )
        packed = carry.pack(self.config)
        as_text = json.dumps(packed)
        self.assertNotIn("something a run said", as_text)
        self.assertNotIn("20260101", as_text)
        # A step called "Keep the evidence" is a step, not evidence: what must
        # not travel is what a run wrote, so that is what is asked about.
        self.assertNotIn("last-run.json", packed["files"].get(".harness/pipelines", {}))
        self.assertNotIn(".harness/qa/runs", packed["files"])

    def test_it_writes_a_file_you_can_carry(self) -> None:
        packed = carry.write_to(self.config, "harness-setup.json")
        where = self.root / "harness-setup.json"
        self.assertTrue(where.is_file())
        self.assertTrue(packed.holds)
        carry.read_it(json.loads(where.read_text(encoding="utf-8")))


class UnpackingTests(CarryTestCase):
    def test_a_setup_lands_on_another_machine(self) -> None:
        packed = carry.pack(self.config)
        root, config = self.elsewhere()
        done = carry.unpack(config, packed)
        self.assertTrue(done.written)
        self.assertEqual(
            json.loads((root / ".harness" / "config.json").read_text(encoding="utf-8")),
            {"qa": {"workers": 3}},
        )
        self.assertEqual(pipelines.saved_ones(config), ["First pipeline"])

    def test_it_writes_over_nothing_unless_somebody_says_so(self) -> None:
        packed = carry.pack(self.config)
        root, config = self.elsewhere()
        (root / ".harness" / "config.json").write_text(
            json.dumps({"qa": {"workers": 9}}), encoding="utf-8"
        )
        done = carry.unpack(config, packed)
        self.assertTrue(any("already here" in line for line in done.left_alone))
        self.assertEqual(
            json.loads((root / ".harness" / "config.json").read_text(encoding="utf-8")),
            {"qa": {"workers": 9}},
            "what was already there was left exactly as it was",
        )

    def test_saying_so_writes_over_it(self) -> None:
        packed = carry.pack(self.config)
        root, config = self.elsewhere()
        (root / ".harness" / "config.json").write_text(
            json.dumps({"qa": {"workers": 9}}), encoding="utf-8"
        )
        carry.unpack(config, packed, over_the_top=True)
        self.assertEqual(
            json.loads((root / ".harness" / "config.json").read_text(encoding="utf-8")),
            {"qa": {"workers": 3}},
        )

    def test_a_pipeline_in_a_setup_is_read_before_it_lands(self) -> None:
        # A setup file is something somebody was handed. A drawing nobody
        # checked must not be written into a project.
        packed = carry.pack(self.config)
        packed["files"][".harness/pipelines"]["nasty.json"] = {
            "name": "Nasty", "nodes": [{"id": "x", "kind": "run_anything", "label": "x"}],
            "edges": [],
        }
        _root, config = self.elsewhere()
        with self.assertRaises(HarnessError):
            carry.unpack(config, packed)

    def test_a_file_that_is_not_a_setup_is_refused(self) -> None:
        for bad in (None, "text", 7, {}, {"what": "something else"},
                    {"what": carry.WHAT_THIS_IS, "version": 99, "files": {}}):
            with self.subTest(bad=bad):
                with self.assertRaises(HarnessError):
                    carry.read_it(bad)

    def test_a_setup_may_not_carry_anything_else(self) -> None:
        packed = carry.pack(self.config)
        packed["files"][".harness/config.local.json"] = {"providers": {}}
        with self.assertRaises(HarnessError) as caught:
            carry.read_it(packed)
        self.assertIn("may not carry", str(caught.exception))

    def test_it_says_the_model_routes_are_still_yours_to_set_up(self) -> None:
        _root, config = self.elsewhere()
        done = carry.unpack(config, carry.pack(self.config))
        self.assertIn("still yours to set up", done.note)


if __name__ == "__main__":
    unittest.main()
