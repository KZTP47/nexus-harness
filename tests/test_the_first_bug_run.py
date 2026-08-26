"""The bugs found in the first bug run, each held down by a test.

Every one of these was reproduced before it was fixed. They are kept together
because they were found together, and because the shapes repeat: a name that
two things share, a record written before the thing it records has happened, a
success reported for work that was quietly skipped.
"""

from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from our_harness import autosetup, explain, pipelines, plain_graph, qa, settings as settings_lab, vault
from our_harness.config import DEFAULT_CONFIG, LoadedConfig


class ProjectTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / ".harness").mkdir()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})


class TwoPipelinesOneFileNameTests(ProjectTestCase):
    """Saving refused this. Reading and removing did not."""

    def setUp(self) -> None:
        super().setUp()
        pipelines.save(self.config, {
            "name": "A B",
            "nodes": [{"id": "start", "kind": "start", "label": "Start"}],
            "edges": [],
        })

    def test_asking_for_a_name_nobody_saved_does_not_hand_back_somebody_elses(self) -> None:
        with self.assertRaises(pipelines.PipelineError) as caught:
            pipelines.load(self.config, "A-B")
        self.assertIn("no pipeline called A-B", str(caught.exception))
        self.assertIn("called A B", str(caught.exception), "and it says which one is there")

    def test_removing_a_name_nobody_saved_does_not_remove_somebody_elses(self) -> None:
        with self.assertRaises(pipelines.PipelineError):
            pipelines.remove(self.config, "A-B")
        self.assertEqual(pipelines.saved_ones(self.config), ["A B"], "theirs is still here")

    def test_the_real_name_still_reads_and_removes(self) -> None:
        self.assertEqual(pipelines.load(self.config, "A B")["name"], "A B")
        self.assertEqual(pipelines.remove(self.config, "A B"), "A B was removed.")
        self.assertEqual(pipelines.saved_ones(self.config), [])


class TheStoryReadsInOrderTests(unittest.TestCase):
    """Where two paths meet again, the story used to read backwards."""

    def graph(self) -> dict:
        # The work splits and comes back together: start goes both straight to
        # the merge and the long way round through the check. Nothing here
        # loops.
        return {
            "nodes": [
                {"id": "start", "type": "start", "label": "Start"},
                {"id": "check", "type": "review", "label": "Is it safe?"},
                {"id": "merge", "type": "apply", "label": "It changes the files"},
            ],
            "edges": [
                {"source": "start", "target": "merge"},
                {"source": "start", "target": "check"},
                {"source": "check", "target": "merge"},
            ],
        }

    def test_nothing_is_told_before_the_thing_it_waits_for(self) -> None:
        told = [stage.id for stage in plain_graph.story(self.graph())]
        self.assertLess(told.index("check"), told.index("merge"))

    def test_an_ordinary_arrow_is_not_called_a_retry(self) -> None:
        for stage in plain_graph.story(self.graph()):
            self.assertEqual(stage.goes_back_to, [], f"{stage.id} was called a retry loop")

    def test_a_real_loop_is_still_found(self) -> None:
        graph = self.graph()
        graph["edges"].append({"source": "merge", "target": "check"})
        back = {stage.id: stage.goes_back_to for stage in plain_graph.story(graph)}
        self.assertEqual(back["merge"], ["check"])

    def test_every_stage_is_still_in_the_story(self) -> None:
        graph = self.graph()
        graph["nodes"].append({"id": "lonely", "type": "review", "label": "Nothing points here"})
        told = {stage.id for stage in plain_graph.story(graph)}
        self.assertEqual(told, {"start", "check", "merge", "lonely"})


class TheEvidenceStepTests(ProjectTestCase):
    def test_it_does_not_write_itself_down_while_it_is_still_running(self) -> None:
        pipeline = pipelines.read_it({
            "name": "With evidence",
            "nodes": [
                {"id": "start", "kind": "start", "label": "Start"},
                {"id": "evidence", "kind": "artifact", "label": "Keep the evidence"},
            ],
            "edges": [{"from": "start", "to": "evidence"}],
        })
        pipelines.run_it(self.config, pipeline)
        evidence = list(
            (self.root / ".harness" / "pipelines" / "evidence").rglob("*.json")
        )
        self.assertEqual(len(evidence), 1, "the run wrote one immutable evidence file")
        kept = json.loads(evidence[0].read_text(encoding="utf-8"))
        states = {one["id"]: one["state"] for one in kept}
        self.assertNotIn(
            "evidence", states,
            "the record of the run held a half-finished note about itself",
        )
        self.assertEqual(states["start"], "passed")


class WritingOverSomebodyElsesNoteTests(ProjectTestCase):
    def test_a_new_note_with_a_title_somebody_used_is_refused(self) -> None:
        vault.write_one(self.config, vault.Note(
            name="", title="How we deploy", kind="how-to", body="The real, careful list."
        ))
        vault.used(self.config, "how-we-deploy", went_well=True)
        with self.assertRaises(vault.VaultError) as caught:
            vault.write_one(self.config, vault.Note(
                name="", title="How we deploy", kind="how-to", body="quick draft"
            ))
        self.assertIn("already a note", str(caught.exception))
        kept = vault.read_one(self.config, "how-we-deploy")
        self.assertEqual(kept.body, "The real, careful list.")
        self.assertEqual(kept.uses, 1, "and what it had earned is still there")

    def test_changing_the_note_you_opened_still_works(self) -> None:
        vault.write_one(self.config, vault.Note(
            name="", title="How we deploy", kind="how-to", body="One."
        ))
        vault.write_one(
            self.config,
            vault.Note(name="", title="How we deploy", kind="how-to", body="Two."),
            was="how-we-deploy",
        )
        self.assertEqual(vault.read_one(self.config, "how-we-deploy").body, "Two.")

    def test_saying_a_note_helped_still_works(self) -> None:
        vault.write_one(self.config, vault.Note(
            name="", title="How we deploy", kind="how-to", body="One."
        ))
        note = vault.used(self.config, "how-we-deploy", went_well=True)
        self.assertEqual((note.uses, note.worked), (1, 1))


class NothingSneaksIntoTheTopOfANoteTests(ProjectTestCase):
    def test_a_line_break_in_where_it_came_from_writes_no_extra_lines(self) -> None:
        # A run names itself, and what it says goes at the top of the note. A
        # line break in that name used to write lines of its own, and one of
        # them could quietly set how sure the harness was about the note.
        vault.write_one(self.config, vault.Note(
            name="", title="From a run", kind="lesson", body="x", sure=0.5,
            came_from="run-42\nuses: 999999\nsure: 1.0\nmade-up: yes",
        ))
        kept = vault.read_one(self.config, "from-a-run")
        self.assertEqual(kept.sure, 0.5)
        self.assertEqual(kept.uses, 0)
        self.assertNotIn("\n", kept.came_from)
        written = (self.root / ".harness" / "vault" / "from-a-run.md").read_text(encoding="utf-8")
        header = written.split("---")[1].strip().splitlines()
        self.assertEqual(
            [line.split(":")[0] for line in header],
            ["title", "kind", "tags", "sure", "learned", "touched", "from", "uses", "worked"],
            "something wrote a line of its own into the top of the note",
        )


class ACommandIsReadAsACommandTests(ProjectTestCase):
    def test_a_command_that_looks_like_a_word_is_still_a_command(self) -> None:
        for typed, wanted in (
            ("true", [["true"]]),
            ("null", [["null"]]),
            ("pytest -q", [["pytest", "-q"]]),
            ("1234", [["1234"]]),
        ):
            with self.subTest(typed=typed):
                done = settings_lab.change(self.config, "project.test_commands", typed)
                self.assertEqual(done.value, wanted, done.note)

    def test_a_written_out_list_is_still_read_as_one(self) -> None:
        done = settings_lab.change(
            self.config, "project.test_commands", '[["pytest", "-q"], ["ruff", "check"]]'
        )
        self.assertEqual(done.value, [["pytest", "-q"], ["ruff", "check"]], done.note)


class PlainEnglishIsNotALeakedKeyTests(unittest.TestCase):
    def test_a_browser_failure_is_not_called_a_credential(self) -> None:
        meaning = explain.what_it_means(
            "Step 3 did not work: nothing to look at here, the button never appeared.",
            kind="browser",
        )
        self.assertNotIn("credential", meaning.headline.lower())

    def test_the_security_scan_saying_it_found_something_still_is(self) -> None:
        meaning = explain.what_it_means("Read 42 files, skipped 3. Found 2 to look at.")
        self.assertIn("credential", meaning.headline.lower())
        self.assertTrue(meaning.sure)


class TheSuiteIsNeverReadHalfWrittenTests(ProjectTestCase):
    def test_writing_the_suite_never_leaves_an_empty_file(self) -> None:
        # It used to empty the file and then fill it. Anything reading in that
        # moment said the project had no checks at all.
        suite = qa.parse_suite({
            "name": "d",
            "cases": [{"id": f"c{n}", "kind": "file", "path": "x"} for n in range(200)],
        })
        qa.write_suite(self.config, suite)
        stop = threading.Event()
        seen: list[str] = []

        def keep_reading() -> None:
            while not stop.is_set():
                try:
                    qa.load_suite(self.config, None, {})
                except Exception as exc:  # noqa: BLE001 - any failure at all is the bug
                    seen.append(str(exc))
                    return

        reader = threading.Thread(target=keep_reading)
        reader.start()
        try:
            for _ in range(40):
                qa.write_suite(self.config, suite)
        finally:
            stop.set()
            reader.join(timeout=10)
        self.assertEqual(seen, [], "a read caught the suite half written")

    def test_it_leaves_nothing_beside_the_file(self) -> None:
        qa.write_suite(self.config, qa.parse_suite(
            {"name": "d", "cases": [{"id": "c", "kind": "file", "path": "x"}]}
        ))
        left = [path.name for path in (self.root / ".harness" / "qa").glob("*.part")]
        self.assertEqual(left, [])


class SetUpSaysWhatItDidNotDoTests(ProjectTestCase):
    def test_it_does_not_report_success_when_the_model_was_never_fetched(self) -> None:
        # Ollama can answer as a background service while its command is
        # nowhere this process can see. The fetch was then skipped without a
        # word, and the whole thing reported as ready to use.
        plan = autosetup.PLANS["ollama"]
        job = autosetup.Job(option="ollama", label="Ollama on this machine")
        with mock.patch.object(autosetup, "_answering", return_value=True), \
                mock.patch.object(autosetup.shutil, "which", return_value=None):
            autosetup._do_ollama(job, self.config, plan)
        fetching = [step for step in job.steps if "Fetch the model" in step.text]
        self.assertTrue(fetching, "the fetch step was not even shown")
        self.assertEqual(fetching[0].state, "cannot")
        self.assertTrue(job.left_for_you, "it did not say what is left for a person to do")
        self.assertIn("pull", " ".join(job.left_for_you))


if __name__ == "__main__":
    unittest.main()
