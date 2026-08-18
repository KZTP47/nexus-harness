"""Pipelines: many jobs wired together, with gates between them.

The running tests stand the machine in, so nothing here starts a real suite, a
real model, or a real git. What is not stood in is the reading: a pipeline that
comes in over an HTTP request is checked by the real reader, because that is
the piece standing between a drawing and everything else.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from our_harness import pipelines
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError


def a_line(*kinds: str) -> dict:
    """A pipeline that is one node after another, for reading tests."""

    nodes = [
        {"id": f"n{spot}", "kind": kind, "label": kind, "settings": {}}
        for spot, kind in enumerate(kinds)
    ]
    edges = [{"from": f"n{spot}", "to": f"n{spot + 1}"} for spot in range(len(kinds) - 1)]
    return {"name": "Line", "nodes": nodes, "edges": edges}


class PipelineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})


class ReadingOneTests(PipelineTestCase):
    def test_the_one_it_ships_with_is_a_good_one(self) -> None:
        tidy = pipelines.read_it(pipelines.a_starting_pipeline())
        self.assertEqual(len(tidy["nodes"]), 6)
        self.assertTrue(pipelines.in_running_order(tidy))

    def test_a_kind_it_does_not_know_is_refused(self) -> None:
        with self.assertRaises(HarnessError):
            pipelines.read_it(a_line("start", "run_any_shell_line_you_like"))

    def test_a_circle_is_refused_with_the_circle_named(self) -> None:
        drawn = a_line("start", "suite", "gate")
        drawn["edges"].append({"from": "n2", "to": "n1"})
        with self.assertRaises(HarnessError) as caught:
            pipelines.read_it(drawn)
        self.assertIn("round in a circle", str(caught.exception))
        self.assertIn("n1", str(caught.exception))

    def test_an_arrow_to_nowhere_is_refused(self) -> None:
        drawn = a_line("start", "suite")
        drawn["edges"].append({"from": "n0", "to": "somewhere-else"})
        with self.assertRaises(HarnessError):
            pipelines.read_it(drawn)

    def test_two_nodes_with_one_name_are_refused(self) -> None:
        drawn = a_line("start", "suite")
        drawn["nodes"][1]["id"] = "n0"
        with self.assertRaises(HarnessError):
            pipelines.read_it(drawn)

    def test_a_setting_a_kind_cannot_use_is_refused(self) -> None:
        drawn = a_line("start", "security_scan")
        drawn["nodes"][1]["settings"] = {"instructions": "write me something"}
        with self.assertRaises(HarnessError) as caught:
            pipelines.read_it(drawn)
        self.assertIn("cannot use", str(caught.exception))

    def test_tries_has_to_be_a_small_whole_number(self) -> None:
        for bad in (0, -1, 99, "two", True, 1.5):
            with self.subTest(bad=bad):
                drawn = a_line("start", "suite")
                drawn["nodes"][1]["settings"] = {"tries": bad}
                with self.assertRaises(HarnessError):
                    pipelines.read_it(drawn)

    def test_a_control_character_in_a_setting_is_refused(self) -> None:
        drawn = a_line("start", "ai_unit_test")
        drawn["nodes"][1]["settings"] = {"instructions": "write\x07a test", "write_to": "t.js"}
        with self.assertRaises(HarnessError):
            pipelines.read_it(drawn)

    def test_a_name_that_would_climb_out_of_the_folder_is_refused(self) -> None:
        for bad in ("../escape", "..", "/etc/passwd", "a/b", "", " ", "x" * 100):
            with self.subTest(bad=bad):
                with self.assertRaises(HarnessError):
                    pipelines.check_the_name(bad)

    def test_rubbish_is_refused(self) -> None:
        for value in (None, "pipeline", 7, [], {"nodes": "none"}):
            with self.subTest(value=value):
                with self.assertRaises(HarnessError):
                    pipelines.read_it(value)

    def test_the_same_arrow_twice_is_kept_once(self) -> None:
        drawn = a_line("start", "suite")
        drawn["edges"].append({"from": "n0", "to": "n1"})
        self.assertEqual(len(pipelines.read_it(drawn)["edges"]), 1)


class KeepingThemTests(PipelineTestCase):
    def test_saving_then_loading_gives_the_same_pipeline_back(self) -> None:
        saved = pipelines.save(self.config, pipelines.a_starting_pipeline())
        again = pipelines.load(self.config, saved["name"])
        self.assertEqual(again, saved)

    def test_they_are_listed_by_name(self) -> None:
        pipelines.save(self.config, pipelines.a_starting_pipeline())
        second = pipelines.a_starting_pipeline()
        second["name"] = "Nightly"
        pipelines.save(self.config, second)
        self.assertEqual(sorted(pipelines.saved_ones(self.config)), ["First pipeline", "Nightly"])

    def test_removing_one_leaves_the_others(self) -> None:
        pipelines.save(self.config, pipelines.a_starting_pipeline())
        pipelines.remove(self.config, "First pipeline")
        self.assertEqual(pipelines.saved_ones(self.config), [])

    def test_loading_one_that_is_not_there_says_so(self) -> None:
        with self.assertRaises(HarnessError):
            pipelines.load(self.config, "Nothing")

    def test_a_saved_file_that_cannot_be_read_does_not_stop_the_list(self) -> None:
        pipelines.save(self.config, pipelines.a_starting_pipeline())
        broken = pipelines.folder(self.config) / "broken.json"
        broken.write_text("{ not json", encoding="utf-8")
        self.assertEqual(pipelines.saved_ones(self.config), ["First pipeline"])

    def test_two_names_that_share_one_file_name_are_refused(self) -> None:
        # "Nightly build" and "Nightly Build" become the same file. Saving the
        # second used to throw the first away without a word.
        first = pipelines.a_starting_pipeline()
        first["name"] = "Nightly build"
        pipelines.save(self.config, first)
        second = pipelines.a_starting_pipeline()
        second["name"] = "Nightly Build"
        with self.assertRaises(HarnessError) as caught:
            pipelines.save(self.config, second)
        self.assertIn("already saved under that file name", str(caught.exception))
        self.assertEqual(pipelines.load(self.config, "Nightly build")["name"], "Nightly build")

    def test_saving_the_same_one_again_is_fine(self) -> None:
        pipelines.save(self.config, pipelines.a_starting_pipeline())
        again = pipelines.a_starting_pipeline()
        again["nodes"] = again["nodes"][:2]
        again["edges"] = again["edges"][:1]
        pipelines.save(self.config, again)
        self.assertEqual(len(pipelines.load(self.config, "First pipeline")["nodes"]), 2)

    def test_the_file_name_stays_inside_the_folder(self) -> None:
        where = pipelines.file_for(self.config, "First pipeline")
        self.assertTrue(where.as_posix().endswith(".harness/pipelines/first-pipeline.json"))


class RunningThemTests(PipelineTestCase):
    def stand_in(self, answers: dict[str, tuple[bool, str, str]]):
        """Every node kind stood in, so no real suite or model is started."""

        def one(config, node, before, results, order, check_kinds, depth=0, stopping=None, waiting_on=None):
            if node["kind"] == "start":
                return True, "Started", ""
            if node["kind"] in pipelines.GATES:
                return pipelines._decide_a_gate(node, before)
            return answers.get(node["id"], (True, "done", ""))

        return mock.patch.object(pipelines, "_do_one", one)

    def test_every_step_runs_in_an_order_that_makes_sense(self) -> None:
        with self.stand_in({}):
            run = pipelines.run_it(self.config, pipelines.a_starting_pipeline())
        self.assertTrue(run.passed, run.said)
        self.assertEqual([node.state for node in run.nodes], [pipelines.PASSED] * 6)

    def test_a_gate_stops_the_work_going_on(self) -> None:
        with self.stand_in({"scan": (False, "a credential is in the code", "")}):
            run = pipelines.run_it(self.config, pipelines.a_starting_pipeline())
        by_id = {node.id: node for node in run.nodes}
        self.assertEqual(by_id["scan"].state, pipelines.FAILED)
        self.assertEqual(by_id["gate"].state, pipelines.FAILED, "the gate itself did not pass")
        self.assertEqual(by_id["tests"].state, pipelines.SKIPPED)
        self.assertFalse(run.passed)

    def test_a_gate_that_needs_only_one_lets_the_work_go_on(self) -> None:
        drawn = pipelines.a_starting_pipeline()
        for node in drawn["nodes"]:
            if node["id"] == "gate":
                node["settings"] = {"needs": "any"}
        drawn["edges"].append({"from": "checks", "to": "gate"})
        with self.stand_in({"scan": (False, "no", "")}):
            run = pipelines.run_it(self.config, drawn)
        by_id = {node.id: node for node in run.nodes}
        self.assertEqual(by_id["gate"].state, pipelines.PASSED)
        self.assertEqual(by_id["tests"].state, pipelines.PASSED)

    def test_a_step_told_to_try_again_really_tries_again(self) -> None:
        tries: list[int] = []

        def flaky(config, node, before, results, order, check_kinds, depth=0, stopping=None, waiting_on=None):
            if node["id"] != "tests":
                return True, "done", ""
            tries.append(1)
            return len(tries) >= 2, f"attempt {len(tries)}", ""

        with mock.patch.object(pipelines, "_do_one", flaky):
            run = pipelines.run_it(self.config, pipelines.a_starting_pipeline())
        by_id = {node.id: node for node in run.nodes}
        self.assertEqual(len(tries), 2)
        self.assertEqual(by_id["tests"].state, pipelines.PASSED)
        self.assertEqual(by_id["tests"].tries, 2)

    def test_a_step_that_throws_does_not_end_the_run(self) -> None:
        def explodes(config, node, before, results, order, check_kinds, depth=0, stopping=None, waiting_on=None):
            if node["id"] == "scan":
                raise ValueError("something nobody expected")
            return True, "done", ""

        with mock.patch.object(pipelines, "_do_one", explodes):
            run = pipelines.run_it(self.config, pipelines.a_starting_pipeline())
        by_id = {node.id: node for node in run.nodes}
        self.assertEqual(by_id["scan"].state, pipelines.FAILED)
        self.assertIn("something nobody expected", by_id["scan"].said)
        self.assertEqual(by_id["checks"].state, pipelines.PASSED, "the rest still ran")

    def test_it_says_what_is_happening_as_it_happens(self) -> None:
        heard: list[dict] = []
        with self.stand_in({}):
            pipelines.run_it(self.config, pipelines.a_starting_pipeline(), tell=heard.append)
        kinds = {event["kind"] for event in heard}
        self.assertEqual(kinds, {"pipeline_node"})
        states = {event["payload"]["state"] for event in heard}
        self.assertIn(pipelines.RUNNING, states)
        self.assertIn(pipelines.PASSED, states)

    def test_being_stopped_leaves_the_rest_skipped(self) -> None:
        with self.stand_in({}):
            run = pipelines.run_it(
                self.config, pipelines.a_starting_pipeline(), stopping=lambda: True
            )
        self.assertTrue(all(node.state == pipelines.SKIPPED for node in run.nodes))
        # It used to report this as a pass, because nothing had failed. Nothing
        # had run either. A run somebody stopped has not passed, and saying so
        # is the same rule the whole harness keeps: work that did not happen
        # must never read like work that did.
        self.assertFalse(run.passed, "a run that was stopped has not passed")
        self.assertIn("never ran", run.said)

    def test_every_kind_can_really_be_run(self) -> None:
        # A kind on the list that nothing knows how to run would be a node a
        # person can drag out and never use.
        # Waiting for a person is not run here: it is handled by the runner
        # itself, because it has to be able to wait, and _do_one cannot. Its
        # own tests are in test_running_less_than_all_of_it.py.
        for kind in set(pipelines.KINDS) - {"wait_for_a_person"}:
            with self.subTest(kind=kind):
                node = {"id": "only", "kind": kind, "label": kind, "settings": {}}
                try:
                    pipelines._do_one(self.config, node, [], {"only": None}, [], None)
                except pipelines.PipelineError as exc:
                    self.assertNotIn("nothing knows how to run", str(exc))
                except Exception:  # noqa: BLE001 - it tried, which is the point
                    pass


class ARealCheckReallyRunsTests(PipelineTestCase):
    """No standing in. These run the harness's own check runner for real.

    The tests above hand the running a stand-in, which is right for testing
    order and gates and trying again. It also meant a node could ask a result
    for a field it does not have, and every one of those tests still passed
    while every node in the panel said "this step went wrong". So this class
    runs real checks through the real runner, on a throwaway project.
    """

    def test_a_check_that_passes_comes_back_as_passed(self) -> None:
        (self.root / "README.md").write_text("# Hello\n", encoding="utf-8")
        node = {"id": "scan", "label": "Security scan", "settings": {"paths": ["README.md"]}}
        passed, said, _detail = pipelines._run_security_scan(self.config, node, None)
        self.assertTrue(passed, said)
        self.assertTrue(said, "a step that says nothing tells nobody anything")
        self.assertNotIn("went wrong", said)

    def test_a_check_that_fails_says_why_in_words(self) -> None:
        # A shape the scanner really knows, so this tests the pipeline rather
        # than the scanner's opinion of made-up text.
        (self.root / "keys.txt").write_text("AKIA" + "Q" * 16 + "\n", encoding="utf-8")
        node = {"id": "scan", "label": "Security scan", "settings": {"paths": ["keys.txt"]}}
        passed, said, detail = pipelines._run_security_scan(self.config, node, None)
        self.assertFalse(passed)
        self.assertTrue(said)
        self.assertNotIn("went wrong", said, "a real failure, not an error inside the harness")
        self.assertTrue(detail or said)

    def test_a_whole_pipeline_of_real_checks_runs(self) -> None:
        # Start, a real scan, a real gate, and the evidence file, with nothing
        # stood in anywhere.
        (self.root / "README.md").write_text("# Hello\n", encoding="utf-8")
        drawn = {
            "name": "Real",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start", "settings": {}},
                {"id": "scan", "kind": "security_scan", "label": "Scan",
                 "settings": {"paths": "README.md"}},
                {"id": "gate", "kind": "security_gate", "label": "Gate", "settings": {"needs": "all"}},
                {"id": "keep", "kind": "artifact", "label": "Evidence", "settings": {}},
            ],
            "edges": [
                {"from": "start", "to": "scan"},
                {"from": "scan", "to": "gate"},
                {"from": "gate", "to": "keep"},
            ],
        }
        run = pipelines.run_it(self.config, drawn)
        self.assertTrue(run.passed, run.said + " :: " + str([n.said for n in run.nodes]))
        for node in run.nodes:
            with self.subTest(node=node.id):
                self.assertEqual(node.state, pipelines.PASSED)
                self.assertNotIn("went wrong", node.said)
        kept = self.root / ".harness" / "pipelines" / "last-run.json"
        self.assertTrue(kept.is_file(), "the evidence node really wrote its file")


class WhatEachKindDoesTests(PipelineTestCase):
    def test_the_git_node_only_reads(self) -> None:
        asked: list[list[str]] = []

        class Finished:
            returncode = 0
            stdout = "main"
            stderr = ""

        def run(parts, **kwargs):
            asked.append(list(parts))
            return Finished()

        with mock.patch.object(pipelines.subprocess, "run", run):
            passed, said, _detail = pipelines._run_git_repo(
                self.config, {"id": "g", "label": "Git", "settings": {}}, None
            )
        self.assertTrue(passed)
        self.assertIn("main", said)
        for parts in asked:
            self.assertIn(parts[1], ("rev-parse", "status"), "it only ever reads")

    def test_the_ai_node_refuses_to_climb_out_of_the_project(self) -> None:
        for nowhere in ("../outside.js", "..\\outside.js", "/etc/passwd"):
            with self.subTest(where=nowhere):
                node = {"id": "ai", "label": "AI", "settings": {
                    "instructions": "write a test", "write_to": nowhere,
                }}
                passed, said, _detail = pipelines._run_ai_unit_test(self.config, node, None)
                self.assertFalse(passed)
                self.assertIn("writes one file", said)

    def test_the_ai_node_writes_only_into_its_own_drafts_folder(self) -> None:
        # The first attempt at this let it write into tests/, on the reasoning
        # that a test file is a safe thing to write. That was backwards: tests/
        # is exactly where every test runner looks, so a pipeline could have a
        # model write a "test" and have the very next step of the same run
        # execute it. Nothing runs what is in the drafts folder.
        answer = mock.Mock(text="print('hello')")
        with mock.patch("our_harness.providers.create_provider",
                        return_value=mock.Mock(complete=mock.Mock(return_value=answer))):
            passed, said, _detail = pipelines._run_ai_unit_test(
                self.config,
                {"id": "ai", "label": "AI", "settings": {
                    "instructions": "write a test", "write_to": "basket_test.py",
                }},
                None,
            )
        self.assertTrue(passed, said)
        landed = self.root / ".harness" / "pipelines" / "drafts" / "basket_test.py"
        self.assertTrue(landed.is_file())
        self.assertIn("move it into your tests yourself", said)

    def test_the_ai_node_refuses_a_path_and_so_cannot_choose_a_folder(self) -> None:
        for nowhere in ("tests/test_basket.py", "src/app.py", "../outside.js",
                        "src/__tests__/x.js", ".github/workflows/ci.yml"):
            with self.subTest(where=nowhere):
                passed, said, _detail = pipelines._run_ai_unit_test(
                    self.config,
                    {"id": "ai", "label": "AI", "settings": {
                        "instructions": "write a test", "write_to": nowhere,
                    }},
                    None,
                )
                self.assertFalse(passed)
                self.assertIn("writes one file", said)
                self.assertFalse((self.root / nowhere.replace("..", "x")).exists())

    def test_nothing_a_model_writes_lands_where_a_test_runner_looks(self) -> None:
        # The rule stated as the thing that matters, rather than as a path.
        # Every runner this harness knows finds tests by walking the project;
        # the harness's own folder is not part of that walk.
        self.assertTrue(pipelines.DRAFTS.startswith(".harness/"))
        answer = mock.Mock(text="print('hello')")
        with mock.patch("our_harness.providers.create_provider",
                        return_value=mock.Mock(complete=mock.Mock(return_value=answer))):
            pipelines._run_ai_unit_test(
                self.config,
                {"id": "ai", "label": "AI", "settings": {
                    "instructions": "write a test", "write_to": "sneaky_test.py",
                }},
                None,
            )
        outside_the_harness_folder = [
            path for path in self.root.rglob("*_test.py")
            if ".harness" not in path.relative_to(self.root).parts
        ]
        self.assertEqual(outside_the_harness_folder, [])

    def test_a_model_writing_then_a_test_step_running_does_not_run_it(self) -> None:
        # The whole worry, written down as one run: one step asks a model for a
        # file, and the next step runs the project's tests. Whatever the model
        # wrote must not be among what that command would find.
        tests = self.root / "tests"
        tests.mkdir()
        (tests / "test_real.py").write_text(
            "def test_one():\n    assert True\n", encoding="utf-8"
        )
        answer = mock.Mock(text="import os\nos.system('echo pwned')")
        drawn = {
            "name": "Draft then test",
            "nodes": [
                {"id": "ai", "kind": "ai_unit_test", "label": "AI", "settings": {
                    "instructions": "write a test", "write_to": "written_test.py"}},
                {"id": "tests", "kind": "unit_test", "label": "Tests",
                 "settings": {"command_kind": "test"}},
            ],
            "edges": [{"from": "ai", "to": "tests"}],
        }
        with mock.patch("our_harness.providers.create_provider",
                        return_value=mock.Mock(complete=mock.Mock(return_value=answer))):
            run = pipelines.run_it(self.config, drawn)
        said = {node.id: node.said for node in run.nodes}
        written = self.root / ".harness" / "pipelines" / "drafts" / "written_test.py"
        self.assertTrue(written.is_file(), said)
        # A test runner walks the project. Everything it would find is here,
        # and what the model wrote is not among it.
        would_be_found = sorted(
            path.name for path in self.root.rglob("*_test.py")
            if ".harness" not in path.relative_to(self.root).parts
        ) + sorted(
            path.name for path in self.root.rglob("test_*.py")
            if ".harness" not in path.relative_to(self.root).parts
        )
        self.assertEqual(would_be_found, ["test_real.py"])
        self.assertIn("os.system", written.read_text(encoding="utf-8"),
                      "it really did write what the model said, out of harm's way")

    def test_the_ai_node_says_so_when_no_model_is_connected(self) -> None:
        node = {"id": "ai", "label": "AI", "settings": {
            "instructions": "write a test", "write_to": "made.js",
        }}
        with mock.patch("our_harness.providers.create_provider",
                        side_effect=HarnessError("no provider")):
            passed, said, _detail = pipelines._run_ai_unit_test(self.config, node, None)
        self.assertFalse(passed)
        self.assertIn("No model is connected", said)

    def test_the_ai_node_saves_what_the_model_writes(self) -> None:
        node = {"id": "ai", "label": "AI", "settings": {
            "instructions": "write a test", "write_to": "made.js",
        }}
        answer = mock.Mock(text="```js\nconsole.log('hello');\n```")
        with mock.patch("our_harness.providers.create_provider",
                        return_value=mock.Mock(complete=mock.Mock(return_value=answer))):
            passed, said, _detail = pipelines._run_ai_unit_test(self.config, node, None)
        self.assertTrue(passed, said)
        written = (
            self.root / ".harness" / "pipelines" / "drafts" / "made.js"
        ).read_text(encoding="utf-8")
        self.assertEqual(written.strip(), "console.log('hello');")
        self.assertNotIn("```", written, "the fence a model adds is taken off")

    def test_the_evidence_node_writes_what_happened(self) -> None:
        done = [pipelines.NodeResult(id="a", kind="suite", label="Checks", state=pipelines.PASSED)]
        passed, said, _detail = pipelines._run_artifact(
            self.config, {"id": "e", "label": "Evidence", "settings": {}}, None, so_far=done
        )
        self.assertTrue(passed, said)
        written = json.loads(
            (self.root / ".harness" / "pipelines" / "last-run.json").read_text(encoding="utf-8")
        )
        self.assertEqual(written[0]["label"], "Checks")

    def test_the_evidence_node_will_not_write_outside_the_project(self) -> None:
        with self.assertRaises(HarnessError):
            pipelines._run_artifact(
                self.config,
                {"id": "e", "label": "Evidence", "settings": {"write_to": "../out.json"}},
                None, so_far=[],
            )

    def test_a_unit_test_node_with_no_command_says_what_to_do(self) -> None:
        passed, said, detail = pipelines._run_unit_test(
            self.config, {"id": "u", "label": "Tests", "settings": {"command_kind": "test"}}, None
        )
        self.assertFalse(passed)
        self.assertIn("no test command", said)
        self.assertIn("project.test_commands", detail)

    def test_a_unit_test_node_will_only_run_the_three_kinds_of_command(self) -> None:
        with self.assertRaises(HarnessError):
            pipelines._run_unit_test(
                self.config,
                {"id": "u", "label": "Tests", "settings": {"command_kind": "deploy"}},
                None,
            )


if __name__ == "__main__":
    unittest.main()
