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
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from our_harness import pipelines, qa
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


class PipelineQaPreservationTests(PipelineTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.library = self.root / ".harness" / "pipelines"
        self.library.mkdir()
        self._write_definition(self.library / "mine.json", "Mine")
        (self.library / "nested").mkdir()
        (self.library / "nested" / "note.bin").write_bytes(b"\x00\xffkept")
        self.case = mock.Mock(tags=("pipelines",), touches=())

    @staticmethod
    def _write_definition(path: Path, name: str) -> None:
        definition = pipelines.a_starting_pipeline()
        definition["name"] = name
        path.write_text(json.dumps(definition, indent=2) + "\n", encoding="utf-8")

    def test_success_restores_every_original_byte_and_removes_only_new_artifacts(self) -> None:
        before = qa._pipeline_tree(self.library)
        with qa._pipeline_definitions_put_back_afterwards(
            self.root, [self.case], "success"
        ):
            (self.library / "mine.json").write_text("changed", encoding="utf-8")
            (self.library / "nested" / "note.bin").unlink()
            (self.library / "made-by-the-check.json").write_text(
                '{"name":"Made by the check"}', encoding="utf-8"
            )
        self.assertEqual(qa._pipeline_tree(self.library), before)
        self.assertFalse((self.library / "made-by-the-check.json").exists())
        recoveries = list(
            (self.root / qa.WHERE_PIPELINES_ARE_COPIED).glob("*/displaced-after-interruption")
        )
        self.assertEqual(len(recoveries), 1)
        self.assertEqual(
            (recoveries[0] / "made-by-the-check.json").read_text(encoding="utf-8"),
            '{"name":"Made by the check"}',
        )

    def test_a_normal_save_while_browser_qa_is_active_is_never_discarded(self) -> None:
        before = qa._pipeline_tree(self.library)
        wrote = threading.Event()

        def user_saves() -> None:
            self._write_definition(
                self.library / "mine.json", "Mine edited while QA runs"
            )
            self._write_definition(
                self.library / "user-saved-during-qa.json", "User saved during QA"
            )
            wrote.set()

        with qa._pipeline_definitions_put_back_afterwards(
            self.root, [self.case], "concurrent-user-save"
        ):
            writer = threading.Thread(target=user_saves)
            writer.start()
            writer.join(timeout=2)
            self.assertTrue(wrote.is_set())

        self.assertEqual(qa._pipeline_tree(self.library), before)
        recovery_root = self.root / qa.WHERE_PIPELINES_ARE_COPIED
        displaced = next(recovery_root.glob("*/displaced-after-interruption"))
        self.assertEqual(
            json.loads((displaced / "mine.json").read_text(encoding="utf-8"))["name"],
            "Mine edited while QA runs",
        )
        self.assertEqual(json.loads(
            (displaced / "user-saved-during-qa.json").read_text(encoding="utf-8")
        )["name"], "User saved during QA")

    def test_assertion_failure_still_restores_the_library(self) -> None:
        before = qa._pipeline_tree(self.library)
        with self.assertRaisesRegex(AssertionError, "visual assertion"):
            with qa._pipeline_definitions_put_back_afterwards(
                self.root, [self.case], "failed"
            ):
                (self.library / "mine.json").unlink()
                (self.library / "failed-check.json").write_text("test", encoding="utf-8")
                raise AssertionError("visual assertion")
        self.assertEqual(qa._pipeline_tree(self.library), before)
        displaced = next(
            (self.root / qa.WHERE_PIPELINES_ARE_COPIED)
            .glob("*/displaced-after-interruption")
        )
        self.assertEqual(
            (displaced / "failed-check.json").read_text(encoding="utf-8"), "test"
        )

    def test_file_and_folder_type_changes_are_restored_exactly(self) -> None:
        before = qa._pipeline_tree(self.library)
        with qa._pipeline_definitions_put_back_afterwards(
            self.root, [self.case], "type-changes"
        ):
            (self.library / "mine.json").unlink()
            (self.library / "mine.json").mkdir()
            (self.library / "mine.json" / "artifact.txt").write_text("test", encoding="utf-8")
            (self.library / "nested" / "note.bin").unlink()
            (self.library / "nested").rmdir()
            (self.library / "nested").write_text("test", encoding="utf-8")
        self.assertEqual(qa._pipeline_tree(self.library), before)

    def test_a_library_created_entirely_by_a_check_is_removed_afterwards(self) -> None:
        qa._remove_pipeline_tree(self.library)
        self.assertFalse(self.library.exists())
        with qa._pipeline_definitions_put_back_afterwards(
            self.root, [self.case], "originally-absent"
        ):
            self.library.mkdir(parents=True)
            (self.library / "only-a-check.json").write_text("test", encoding="utf-8")
        self.assertFalse(self.library.exists())

    def test_an_interrupted_transaction_is_recovered_before_the_next_check(self) -> None:
        before = qa._pipeline_tree(self.library)
        abandoned = qa._begin_pipeline_preservation(self.root, "interrupted")
        (self.library / "mine.json").write_text("left wrong", encoding="utf-8")
        (self.library / "interrupted-check.json").write_text("test", encoding="utf-8")
        post_crash_mine = (self.library / "mine.json").read_bytes()
        post_crash_extra = (self.library / "interrupted-check.json").read_bytes()

        with qa._pipeline_definitions_put_back_afterwards(
            self.root, [self.case], "next"
        ):
            self.assertEqual(qa._pipeline_tree(self.library), before)

        self.assertEqual(qa._pipeline_tree(self.library), before)
        self.assertTrue(abandoned.exists())
        displaced = abandoned / "displaced-after-interruption"
        self.assertEqual((displaced / "mine.json").read_bytes(), post_crash_mine)
        self.assertEqual(
            (displaced / "interrupted-check.json").read_bytes(), post_crash_extra
        )
        journal = json.loads((abandoned / "journal.json").read_text(encoding="utf-8"))
        self.assertEqual(journal["state"], "restored")
        self.assertTrue(journal["keep_recovery_copy"])
        self.assertIn("never automatically deleted", journal["recovery_note"])
        self.assertTrue((abandoned / "RECOVERY_NOTICE.txt").is_file())

    def test_transient_windows_directory_denials_retry_without_overwriting_evidence(self) -> None:
        before = qa._pipeline_tree(self.library)
        transaction = qa._begin_pipeline_preservation(self.root, "transient-directory")
        self._write_definition(self.library / "mine.json", "Changed during QA")
        current = qa._pipeline_tree(self.library)
        # Simulate a move which reached the destination before its journal
        # update, but whose bytes do not match this newly observed live tree.
        existing = transaction / "displaced-after-interruption"
        existing.mkdir()
        (existing / "older-evidence.txt").write_text("keep me", encoding="utf-8")
        real_replace = qa._replace_pipeline_directory_once
        attempts = 0

        def briefly_denied(stage: Path, destination: Path) -> None:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise PermissionError(13, "transient access denied")
            real_replace(stage, destination)

        with mock.patch.object(
            qa, "_replace_pipeline_directory_once", side_effect=briefly_denied,
        ), mock.patch.object(qa.time, "sleep") as slept:
            qa._restore_pipeline_transaction(
                self.root, transaction, preserve_displaced=True,
            )

        self.assertEqual(attempts, 3)
        self.assertEqual(slept.call_count, 2)
        self.assertEqual(qa._pipeline_tree(self.library), before)
        self.assertEqual(
            (existing / "older-evidence.txt").read_text(encoding="utf-8"), "keep me",
        )
        journal = qa._read_pipeline_journal(transaction)
        placed = transaction / journal["displaced_copies"][0]["path"]
        self.assertNotEqual(placed, existing)
        self.assertEqual(qa._pipeline_tree(placed), current)

    def test_exhausted_directory_denial_keeps_incomplete_and_later_recovers(self) -> None:
        before = qa._pipeline_tree(self.library)
        transaction = qa._begin_pipeline_preservation(self.root, "denied-directory")
        self._write_definition(self.library / "mine.json", "Exact live variant")
        self._write_definition(self.library / "new.json", "New during QA")
        current = qa._pipeline_tree(self.library)

        with mock.patch.object(
            qa, "_replace_pipeline_directory_once",
            side_effect=PermissionError(13, "persistent access denied"),
        ) as replace, mock.patch.object(qa.time, "sleep"):
            for _attempt in range(2):
                with self.assertRaisesRegex(qa.QaError, "exact incomplete evidence remains"):
                    qa._restore_pipeline_transaction(
                        self.root, transaction, preserve_displaced=True,
                    )
                incomplete_now = list(transaction.glob(".*.incomplete"))
                self.assertEqual(len(incomplete_now), 1)
                self.assertEqual(qa._pipeline_tree(incomplete_now[0]), current)

        self.assertEqual(
            replace.call_count,
            2 * (len(qa.PIPELINE_DIRECTORY_REPLACE_RETRY_SECONDS) + 1),
        )
        self.assertEqual(qa._pipeline_tree(self.library), current)
        journal = qa._read_pipeline_journal(transaction)
        self.assertEqual(journal["state"], "prepared")
        self.assertFalse(journal.get("displaced_copies"))
        incomplete = list(transaction.glob(".*.incomplete"))
        self.assertEqual(len(incomplete), 1)
        self.assertEqual(qa._pipeline_tree(incomplete[0]), current)
        self.assertFalse((transaction / "displaced-after-interruption").exists())

        # A later product recovery verifies the deterministic staged tree
        # again before atomically placing it. It never rewrites those bytes or
        # creates a second full stage.
        qa._recover_pipeline_transactions(self.root)

        self.assertEqual(qa._pipeline_tree(self.library), before)
        self.assertFalse(incomplete[0].exists())
        recovered = qa._read_pipeline_journal(transaction)
        self.assertEqual(recovered["state"], "restored")
        self.assertTrue(recovered["keep_recovery_copy"])
        displaced = transaction / recovered["displaced_copies"][0]["path"]
        self.assertEqual(qa._pipeline_tree(displaced), current)

    def test_destination_appearing_during_retry_is_never_overwritten(self) -> None:
        transaction = qa._begin_pipeline_preservation(self.root, "destination-race")
        self._write_definition(self.library / "mine.json", "Current live bytes")
        current = qa._pipeline_tree(self.library)
        expected = qa._pipeline_manifest(*current)
        expected["original_exists"] = True
        destination = transaction / "displaced-after-interruption"
        stage = transaction / ".displaced-after-interruption.incomplete"

        def destination_appears(_stage: Path, where: Path) -> None:
            where.mkdir()
            (where / "somebody-elses-evidence.txt").write_text(
                "do not overwrite", encoding="utf-8",
            )
            raise PermissionError(13, "access denied after destination appeared")

        with mock.patch.object(
            qa, "_replace_pipeline_directory_once", side_effect=destination_appears,
        ), mock.patch.object(qa.time, "sleep") as slept:
            with self.assertRaisesRegex(qa.QaError, "did not overwrite either"):
                qa._save_displaced_pipeline_copy(
                    transaction, destination, current, expected,
                )

        slept.assert_not_called()
        self.assertTrue(stage.is_dir())
        self.assertEqual(qa._pipeline_tree(stage), current)
        self.assertEqual(
            (destination / "somebody-elses-evidence.txt").read_text(encoding="utf-8"),
            "do not overwrite",
        )
        self.assertEqual(qa._pipeline_tree(self.library), current)

    def test_loading_inventory_recovers_a_stale_journal_without_waiting_for_qa(self) -> None:
        before = qa._pipeline_tree(self.library)
        abandoned = qa._begin_pipeline_preservation(self.root, "stale-inventory")
        self._write_definition(self.library / "mine.json", "Edited after crash")
        self._write_definition(
            self.library / "saved-after-crash.json", "Saved after crash"
        )

        saved, problems = pipelines.saved_inventory(self.config)

        self.assertEqual(saved, ["Mine"])
        self.assertEqual(len(problems), 1)
        self.assertIn(str(abandoned), problems[0])
        self.assertIn("copy or import", problems[0])
        self.assertIn("RECOVERY_NOTICE.txt", problems[0])
        self.assertEqual(qa._pipeline_tree(self.library), before)
        self.assertEqual(json.loads(
            (abandoned / "displaced-after-interruption" / "saved-after-crash.json")
            .read_text(encoding="utf-8")
        )["name"], "Saved after crash")

    def test_inventory_refresh_skips_active_qa_lock_without_blocking(self) -> None:
        before = qa._pipeline_tree(self.library)
        abandoned = qa._begin_pipeline_preservation(self.root, "active-lock")
        self._write_definition(self.library / "mine.json", "Temporary QA view")
        outcome: list[tuple[list[str], list[str]]] = []

        def refresh() -> None:
            outcome.append(pipelines.saved_inventory(self.config))

        started = time.monotonic()
        with qa._pipeline_preservation_file_lock(self.root):
            reader = threading.Thread(target=refresh)
            reader.start()
            reader.join(timeout=1)
            self.assertFalse(reader.is_alive(), "inventory waited for active browser QA")
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.0)
        self.assertEqual(outcome, [(["Temporary QA view"], [])])
        self.assertNotEqual(qa._pipeline_tree(self.library), before)
        saved, problems = pipelines.saved_inventory(self.config)
        self.assertEqual(saved, ["Mine"])
        self.assertEqual(len(problems), 1)
        self.assertIn(str(abandoned), problems[0])
        self.assertEqual(qa._pipeline_tree(self.library), before)
        self.assertTrue((abandoned / "displaced-after-interruption").is_dir())

    def test_recovery_retention_is_bounded_without_evicting_old_evidence(self) -> None:
        retained = []
        for number in range(qa.MAX_RETAINED_PIPELINE_RECOVERIES):
            transaction = qa._begin_pipeline_preservation(self.root, f"held-{number}")
            (self.library / "mine.json").write_text(f"variant {number}", encoding="utf-8")
            qa._restore_pipeline_transaction(
                self.root, transaction, preserve_displaced=True
            )
            retained.append(transaction)
        victim = qa._begin_pipeline_preservation(self.root, "one-too-many")
        (self.library / "mine.json").write_text("must stay live", encoding="utf-8")
        current = qa._pipeline_tree(self.library)

        with self.assertRaisesRegex(qa.QaError, "maximum of 20"):
            qa._restore_pipeline_transaction(
                self.root, victim, preserve_displaced=True
            )

        self.assertEqual(qa._pipeline_tree(self.library), current)
        self.assertTrue(all(one.exists() for one in retained))

    def test_retained_post_crash_bytes_are_checksum_validated(self) -> None:
        transaction = qa._begin_pipeline_preservation(self.root, "retained-checksum")
        (self.library / "mine.json").write_text("post-crash change", encoding="utf-8")
        qa._restore_pipeline_transaction(
            self.root, transaction, preserve_displaced=True
        )
        (transaction / "displaced-after-interruption" / "mine.json").write_text(
            "tampered", encoding="utf-8"
        )
        live = qa._pipeline_tree(self.library)

        journal = qa._read_pipeline_journal(transaction)
        with self.assertRaisesRegex(qa.QaError, "failed its checksum"):
            qa._validate_displaced_pipeline_copies(transaction, journal)

        self.assertEqual(qa._pipeline_tree(self.library), live)

    def test_inventory_does_not_read_retained_displaced_payloads(self) -> None:
        transaction = qa._begin_pipeline_preservation(self.root, "cheap-inventory")
        self._write_definition(self.library / "mine.json", "Post-QA bytes")
        qa._restore_pipeline_transaction(
            self.root, transaction, preserve_displaced=True
        )
        real_tree = qa._pipeline_tree

        def no_displaced_payload_reads(path: Path):
            if any(part.startswith("displaced-after-interruption") for part in path.parts):
                raise AssertionError(f"inventory traversed retained payloads at {path}")
            return real_tree(path)

        with mock.patch.object(qa, "_pipeline_tree", side_effect=no_displaced_payload_reads):
            saved, problems = pipelines.saved_inventory(self.config)

        self.assertEqual(saved, ["Mine"])
        self.assertEqual(len(problems), 1)
        self.assertIn(str(transaction), problems[0])

    def test_a_corrupt_recovery_copy_fails_closed_before_touching_live_files(self) -> None:
        transaction = qa._begin_pipeline_preservation(self.root, "corrupt")
        (transaction / "tree" / "mine.json").write_text("corrupt", encoding="utf-8")
        (self.library / "mine.json").write_text("current uncertain state", encoding="utf-8")
        current = qa._pipeline_tree(self.library)

        with self.assertRaisesRegex(qa.QaError, "failed its checksum"):
            qa._recover_pipeline_transactions(self.root)

        self.assertEqual(qa._pipeline_tree(self.library), current)
        self.assertTrue(transaction.exists(), "the recovery evidence must remain")

    def test_checks_unrelated_to_automations_do_not_snapshot_or_rewrite_them(self) -> None:
        unrelated = mock.Mock(tags=("ui",), touches=("settings",))
        with qa._pipeline_definitions_put_back_afterwards(
            self.root, [unrelated], "unrelated"
        ):
            (self.library / "mine.json").write_text("user change", encoding="utf-8")
        self.assertEqual(
            (self.library / "mine.json").read_text(encoding="utf-8"), "user change"
        )

    def test_byte_identical_pipeline_qa_leaves_no_recovery_evidence(self) -> None:
        before = qa._pipeline_tree(self.library)
        with qa._pipeline_definitions_put_back_afterwards(
            self.root, [self.case], "clean"
        ):
            self.assertEqual(qa._pipeline_tree(self.library), before)
        recovery_root = self.root / qa.WHERE_PIPELINES_ARE_COPIED
        self.assertEqual(
            list(recovery_root.iterdir()) if recovery_root.is_dir() else [], []
        )


class PipelineFullScreenViewTests(unittest.TestCase):
    def setUp(self) -> None:
        here = Path(__file__).resolve().parents[1] / "src" / "our_harness" / "ui"
        self.markup = (here / "index.html").read_text(encoding="utf-8")
        self.script = (here / "app.js").read_text(encoding="utf-8")
        self.styles = (here / "styles.css").read_text(encoding="utf-8")

    def test_the_automation_controls_flow_steps_and_picture_share_one_full_screen_stage(self) -> None:
        stage = self.markup[self.markup.index('<div id="pipelineStage"'):]
        stage = stage[:stage.index('<h3 id="pipelineLogTitle"')]
        for element in ("pipelineFocusSide", "pipelineFullScreen", "pipelineCanvas"):
            self.assertIn(f'id="{element}"', stage)
        self.assertIn(".pipeline-stage.is-fullscreen", self.styles)
        self.assertIn('$("pipelineLibraryControls")', self.script)
        self.assertIn('$("pipelineFocusSide").append(home.element)', self.script)
        self.assertIn("putTheFlowStepsBack()", self.script)

    def test_a_named_new_automation_starts_with_a_clean_slate(self) -> None:
        self.assertIn('id="pipelineNew"', self.markup)
        self.assertIn('Create new automation</button>', self.markup)
        self.assertIn('"Create new automation", "What should the new automation be called?"',
                      self.script)
        self.assertIn('request("/api/pipelines/create"', self.script)
        self.assertIn("pipelineSavedName = pipeline.name", self.script)
        self.assertIn("pipelineSaved = said.saved || []", self.script)

    def test_saved_automations_restore_visibly_and_disclose_unreadable_files(self) -> None:
        for element in ("pipelineSavedCount", "pipelineSavedProblems", "pipelineList"):
            self.assertIn(f'id="{element}"', self.markup)
        self.assertIn(
            "pipelineSaved.includes(requestedName) ? requestedName", self.script,
        )
        self.assertIn("/api/pipelines?recover_missing=1", self.script)
        self.assertIn("pipelineSavedProblems = said.saved_problems || []", self.script)
        self.assertIn("automation library notice", self.script)
        self.assertIn("name === pipelineSavedName", self.script)
        self.assertIn("pipelineCannotRun = String(said.cannot_run", self.script)
        self.assertIn('$("pipelineRun").disabled = Boolean(pipelineCannotRun)', self.script)
        self.assertIn('$("pipelineDelete").disabled = !hasSavedSelection', self.script)

    def test_delete_targets_only_the_exact_saved_selection_and_stale_empty_state_clears(self) -> None:
        self.assertIn(
            'id="pipelineDelete" class="danger" type="button" disabled', self.markup
        )
        refresh = self.script[self.script.index("async function refreshPipelines"):
                              self.script.index("async function refreshAgentContract")]
        self.assertIn("const resolvedName = pipelineSaved.length ?", refresh)
        self.assertIn("pipelineSaved.includes(said.selected_name)", refresh)

        delete = self.script[self.script.index("async function deletePipeline"):
                             self.script.index("async function newPipeline")]
        self.assertIn("const name = pipelineSavedName", delete)
        self.assertIn("!pipelineSaved.includes(name)", delete)
        self.assertIn('pipelineSavedName = ""', delete)
        self.assertNotIn('$("pipelineName").value.trim()', delete)

    def test_visual_automations_have_intuitive_json_import_and_export_controls(self) -> None:
        for element in ("pipelineImport", "pipelineExport", "pipelineImportFile"):
            self.assertIn(f'id="{element}"', self.markup)
        self.assertIn('accept="application/json,.json"', self.markup)
        self.assertIn('aria-label="Choose an automation JSON file to import"', self.markup)
        self.assertIn('request("/api/pipelines/import"', self.script)
        self.assertIn("/api/pipelines/export?name=", self.script)
        self.assertIn("nexus-harness.visual-automation", self.script)
        self.assertIn("is already saved. Choose a name for the imported copy", self.script)
        self.assertIn("Nothing was imported", self.script)

    def test_renderer_refuses_non_utf8_automation_before_json_or_server_import(self) -> None:
        imported = self.script[self.script.index("async function importPipeline"):
                               self.script.index("async function exportPipeline")]
        decoder = imported.index('new TextDecoder("utf-8", {fatal: true})')
        parsed = imported.index("JSON.parse(written)")
        sent = imported.index('request("/api/pipelines/import"')
        self.assertLess(decoder, parsed)
        self.assertLess(parsed, sent)
        self.assertIn(
            "That automation file is not valid UTF-8. Nothing was imported.", imported,
        )

    def test_unsaved_changes_are_visible_and_guard_every_drawing_replacement(self) -> None:
        for element in (
            "pipelineDirtyState", "pipelineUnsavedDialog", "pipelineUnsavedSave",
            "pipelineUnsavedWhy", "pipelineUnsavedSaid",
        ):
            self.assertIn(f'id="{element}"', self.markup)
        for choice in ('value="cancel"', 'value="discard"', "Save and continue"):
            self.assertIn(choice, self.markup)
        self.assertIn("function canonicalPipelineValue", self.script)
        self.assertIn("function currentPipelineSnapshot", self.script)
        self.assertIn('status.textContent = dirty ? "Unsaved changes" : "All changes saved"',
                      self.script)

        guarded = self.script[
            self.script.index("function askHowToReplaceUnsavedPipeline"):
            self.script.index("function applyAgentRunPanelPreference")
        ]
        self.assertNotIn("window.confirm", guarded)
        self.assertNotIn("pipeline =", guarded,
                         "Cancel must not alter the current automation object")
        self.assertIn('choice === "save" || choice === "discard"', guarded)

        opened = self.script[self.script.index("async function openSavedPipeline"):
                             self.script.index("function renderPipelineStarters")]
        self.assertLess(opened.index("mayReplacePipeline"),
                        opened.index("refreshPipelines"))
        created = self.script[self.script.index("async function newPipeline"):
                              self.script.index("function pipelineImportName")]
        self.assertLess(created.index("mayReplacePipeline"),
                        created.index('/api/pipelines/create'))
        starter = self.script[self.script.index("async function usePipelineStarter"):
                              self.script.index("function renderPipelinePalette")]
        self.assertLess(starter.index("mayReplacePipeline"),
                        starter.index('/api/pipelines/starter'))

    def test_import_saves_to_the_library_without_replacing_an_unsaved_drawing(self) -> None:
        imported = self.script[self.script.index("async function importPipeline"):
                               self.script.index("async function exportPipeline")]
        self.assertIn('/api/pipelines/import', imported)
        self.assertIn("pipelineSaved = said.saved || []", imported)
        self.assertNotIn("pipeline = said.pipeline", imported)
        self.assertNotIn("pipelineSavedName =", imported)
        self.assertIn("The current drawing was not changed", imported)
        self.assertIn(
            'refreshPipelines(undefined, {replaceDrawing: !pipelineBaselineReady})',
            self.script,
        )
        self.assertIn('refreshPipelines(name, {replaceDrawing: false})', self.script)
        refresh = self.script[self.script.index("async function refreshPipelines"):
                              self.script.index("async function refreshAgentContract")]
        self.assertIn(
            "if (pipelineSavedName && pipelineSaved.includes(pipelineSavedName))",
            refresh,
        )
        self.assertIn("markPipelineDrawingUnsaved();", refresh)

    def test_pipeline_full_screen_uses_the_native_window_and_is_a_toggle(self) -> None:
        self.assertIn("window.harnessDesktop?.setFullScreen", self.script)
        self.assertIn('$("pipelineStage").classList.toggle("is-fullscreen", pipelineIsFullScreen)',
                      self.script)
        self.assertIn('$("pipelineFullScreen").textContent = pipelineIsFullScreen ? "Exit full screen" : "Full screen"',
                      self.script)

    def test_large_pipelines_can_be_zoomed_fitted_and_dragged(self) -> None:
        for control in ("pipelineZoomOut", "pipelineZoomReset", "pipelineZoomIn",
                        "pipelineFit"):
            self.assertIn(f'id="{control}"', self.markup)
        self.assertIn("const PIPELINE_ZOOM_MIN = 0.35", self.script)
        self.assertIn("const PIPELINE_ZOOM_MAX = 1.8", self.script)
        self.assertIn("function fitTheWholePipeline()", self.script)
        self.assertIn("function makePipelineCanvasPannable()", self.script)
        self.assertIn("nodes.style.transform = `scale(${pipelineZoom})`", self.script)

    def test_agent_choice_and_live_projection_are_bound_to_an_exact_automation(self) -> None:
        self.assertIn('"name", resolvedName || priorAgentChoice', self.script)
        self.assertIn('eventRunId !== pipelineProjectionRunId', self.script)
        self.assertIn('JSON.stringify({run_id: pipelineActiveRunId})', self.script)
        self.assertIn('JSON.stringify({run_id: pipelineActiveRunId, step, carry_on: carryOn})',
                      self.script)
        self.assertIn('/api/pipeline-runs/${encodeURIComponent(runId)}', self.script)
        self.assertIn('if (mine !== pipelineNewestRefresh) return;', self.script)

    def test_run_requests_keep_idempotency_identity_until_the_outcome_is_known(self) -> None:
        self.assertIn('const PIPELINE_PENDING_KEY_PREFIX = "nexus.pipeline.pending.v2:"', self.script)
        self.assertIn("project_authority_id: pipelineAuthorityId", self.script)
        self.assertIn('request_id: pending.request_id', self.script)
        self.assertIn('if (pipelineRequestWasDefinitelyRejected(error))', self.script)
        self.assertIn('Retry reuses request ${pending.request_id}', self.script)
        for state in ("passed", "warning", "failed", "incomplete", "cancelled",
                      "timed_out", "interrupted"):
            self.assertIn(f'"{state}"', self.script)

    def test_an_exact_run_can_be_adopted_after_reload_without_matching_a_mutable_name(self) -> None:
        for element in ("pipelineActiveRun", "pipelineOpenActiveRun", "pipelineStopActive"):
            self.assertIn(f'id="{element}"', self.markup)
        self.assertIn('run.definition || run.snapshot || run.frozen_definition', self.script)
        self.assertIn('pipelineProjectionRunId = runId', self.script)
        self.assertIn('pipelineSavedName = ""', self.script)
        self.assertIn('Open its immutable snapshot', self.script)

    def test_terminal_reconciliation_never_paints_the_mutable_editor_before_exact_open(self) -> None:
        refresh = self.script[self.script.index("async function refreshPipelines"):
                              self.script.index("async function refreshAgentContract")]
        terminal = refresh[refresh.index("pipelineRunIsTerminal(reconciledRun)"):
                           refresh.index("else if (pipelineActiveRunId")]
        self.assertNotIn("showPipelineRun", terminal)
        self.assertIn("the current editor is unchanged", terminal)

        opened = self.script[self.script.index("async function openExactPipelineRun"):
                             self.script.index("function sizeThePipelineCanvas")]
        adopts_definition = opened.index("pipeline = structuredClone(snapshot)")
        binds_run = opened.index("pipelineProjectionRunId = runId")
        paints_result = opened.index("showPipelineRun(run.result)")
        self.assertLess(adopts_definition, paints_result)
        self.assertLess(binds_run, paints_result)
        self.assertIn('$("pipelineLog").replaceChildren()', opened)

    def test_ambiguous_run_reconciliation_is_authority_scoped_and_request_first(self) -> None:
        self.assertIn("function usePipelineAuthority(authorityId)", self.script)
        self.assertIn("value.project_authority_id === authorityId", self.script)
        self.assertIn('/api/pipeline-runs/by-request?request_id=${encodeURIComponent(pending.request_id)}',
                      self.script)
        lookup = self.script[self.script.index("async function lookupPipelineRunByRequest"):
                             self.script.index("async function openExactPipelineRun")]
        self.assertLess(lookup.index("by-request?request_id"),
                        lookup.index("fetchExactPipelineRun(runId)"))
        refresh = self.script[self.script.index("async function refreshPipelines"):
                              self.script.index("async function refreshAgentContract")]
        self.assertNotIn("said.latest_run?.request_id", refresh)
        self.assertNotIn("said.active_run?.request_id", refresh)

    def test_pipeline_tabs_and_connections_have_complete_keyboard_semantics(self) -> None:
        self.assertIn('role="tab" data-pipeline-tab="board" aria-controls="pipelineStage"',
                      self.markup)
        self.assertIn('role="tabpanel" aria-labelledby="pipelineTabBoard"', self.markup)
        self.assertIn('tab.addEventListener("keydown", moveBetweenPipelineTabs)', self.script)
        for key in ("ArrowRight", "ArrowLeft", "Home", "End"):
            self.assertIn(f'event.key === "{key}"', self.script)
        self.assertIn('id="pipelineStructure"', self.markup)
        self.assertIn('Remove connection from ${pipelineNodeName(edge.from)}', self.script)

    def test_pipeline_reflows_without_making_the_document_a_wide_canvas(self) -> None:
        self.assertIn('html, body { max-width: 100%; overflow-x: clip; }', self.styles)
        self.assertIn('@media (max-width: 380px)', self.styles)
        self.assertIn('.pipeline-tabs { flex-wrap: nowrap; overflow-x: auto;', self.styles)

    def test_desktop_agent_instructions_collapse_and_keep_accessible_contrast(self) -> None:
        self.assertIn('<details id="agentRunPanel" class="agent-run-panel"', self.markup)
        self.assertIn('class="agent-run-summary"', self.markup)
        self.assertNotIn('<details id="agentRunPanel" class="agent-run-panel" open', self.markup)
        self.assertIn('AGENT_RUN_PANEL_KEY', self.script)
        self.assertIn('$("agentRunPanel").addEventListener("toggle"', self.script)
        self.assertIn('.agent-run-summary:focus-visible', self.styles)
        self.assertIn('.agent-run-body > .hint { margin: 10px 0; color: #334155;', self.styles)
        self.assertIn('.agent-run-controls button:disabled { opacity: 1; color: #475569;',
                      self.styles)
        self.assertIn('color: #f8fafc; background: #0f172a;', self.styles)

        def contrast(foreground: str, background: str) -> float:
            def luminance(colour: str) -> float:
                channels = [int(colour[index:index + 2], 16) / 255
                            for index in (1, 3, 5)]
                linear = [value / 12.92 if value <= .04045
                          else ((value + .055) / 1.055) ** 2.4
                          for value in channels]
                return .2126 * linear[0] + .7152 * linear[1] + .0722 * linear[2]

            lighter, darker = sorted((luminance(foreground), luminance(background)),
                                     reverse=True)
            return (lighter + .05) / (darker + .05)

        for foreground, background in (
            ("#334155", "#f8fafc"),  # explanatory text
            ("#172033", "#ffffff"),  # ordinary button
            ("#ffffff", "#1d4ed8"),  # primary button
            ("#475569", "#e2e8f0"),  # disabled control without opacity loss
            ("#f8fafc", "#0f172a"),  # agent contract
        ):
            with self.subTest(foreground=foreground, background=background):
                self.assertGreaterEqual(contrast(foreground, background), 4.5)


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

    def test_ai_instructions_at_the_disclosed_boundary_are_preserved_exactly(self) -> None:
        exact = "x" * pipelines.AI_UNIT_INSTRUCTION_CHARACTERS
        drawn = a_line("start", "ai_unit_test")
        drawn["nodes"][1]["settings"] = {
            "instructions": exact,
            "write_to": "generated_test.py",
        }
        read = pipelines.read_it(drawn)
        self.assertEqual(read["nodes"][1]["settings"]["instructions"], exact)

    def test_oversized_ai_instructions_are_rejected_not_truncated(self) -> None:
        drawn = a_line("start", "ai_unit_test")
        drawn["nodes"][1]["settings"] = {
            "instructions": "x" * (pipelines.AI_UNIT_INSTRUCTION_CHARACTERS + 1),
            "write_to": "generated_test.py",
        }
        with self.assertRaisesRegex(pipelines.PipelineError, "did not truncate"):
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
    def test_creating_a_blank_one_saves_it_immediately(self) -> None:
        created = pipelines.create_blank(self.config, "Fresh automation")
        self.assertEqual(created, {"name": "Fresh automation", "nodes": [], "edges": []})
        self.assertEqual(pipelines.saved_ones(self.config), ["Fresh automation"])
        self.assertEqual(pipelines.load(self.config, "Fresh automation"), created)

    def test_creating_one_never_replaces_an_existing_automation(self) -> None:
        existing = pipelines.a_starting_pipeline()
        existing["name"] = "Already here"
        pipelines.save(self.config, existing)
        with self.assertRaisesRegex(pipelines.PipelineError, "already an automation"):
            pipelines.create_blank(self.config, "Already here")
        self.assertEqual(len(pipelines.load(self.config, "Already here")["nodes"]), 6)

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
        saved, problems = pipelines.saved_inventory(self.config)
        self.assertEqual(saved, ["First pipeline"])
        self.assertEqual(len(problems), 1)
        self.assertIn("broken.json", problems[0])

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


class MovingSavedAutomationsTests(PipelineTestCase):
    def test_export_is_versioned_and_import_accepts_legacy_saved_json(self) -> None:
        original = pipelines.a_starting_pipeline()
        original["name"] = "Portable checks"
        original = pipelines.save(self.config, original)
        exported = pipelines.export_document(self.config, original["name"])
        self.assertEqual(exported["schema"], pipelines.AUTOMATION_DOCUMENT_SCHEMA)
        self.assertEqual(exported["version"], pipelines.AUTOMATION_DOCUMENT_VERSION)
        self.assertEqual(exported["automation"], original)

        pipelines.remove(self.config, original["name"])
        imported = pipelines.import_document(self.config, json.dumps(original))
        self.assertEqual(imported, original)
        self.assertEqual(pipelines.load(self.config, original["name"]), original)

    def test_a_versioned_export_round_trips_after_a_restart(self) -> None:
        original = pipelines.a_starting_pipeline()
        original["name"] = "Restart survivor"
        original = pipelines.save(self.config, original)
        exported = json.dumps(pipelines.export_document(self.config, original["name"]))
        pipelines.remove(self.config, original["name"])
        pipelines.import_document(self.config, exported)

        reopened = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.assertEqual(pipelines.saved_ones(reopened), ["Restart survivor"])
        self.assertEqual(pipelines.load(reopened, "Restart survivor"), original)

    def test_invalid_json_changes_nothing(self) -> None:
        before = list(self.root.rglob("*"))
        with self.assertRaisesRegex(pipelines.PipelineError, "not valid JSON"):
            pipelines.import_document(self.config, '{"name": "Half written"')
        self.assertEqual(list(self.root.rglob("*")), before)
        self.assertEqual(pipelines.saved_ones(self.config), [])

    def test_escaped_lone_surrogate_import_is_a_domain_error_and_changes_no_inventory(self) -> None:
        definition = pipelines.a_starting_pipeline()
        definition["name"] = "Broken Unicode"
        definition["nodes"][0]["label"] = "lone surrogate: \ud800"
        written = json.dumps(definition, ensure_ascii=True)
        before = pipelines.saved_inventory(self.config)

        with self.assertRaisesRegex(pipelines.PipelineError, "lone Unicode surrogate"):
            pipelines.import_document(self.config, written)

        self.assertEqual(pipelines.saved_inventory(self.config), before)
        self.assertEqual(list(pipelines.folder(self.config).glob("*.json")), [])

    def test_duplicate_import_never_overwrites_and_an_explicit_new_name_works(self) -> None:
        original = pipelines.a_starting_pipeline()
        original["name"] = "Daily checks"
        original = pipelines.save(self.config, original)
        changed = copy.deepcopy(original)
        changed["nodes"] = []
        changed["edges"] = []
        document = json.dumps({
            "schema": pipelines.AUTOMATION_DOCUMENT_SCHEMA,
            "version": pipelines.AUTOMATION_DOCUMENT_VERSION,
            "automation": changed,
        })
        with self.assertRaisesRegex(pipelines.PipelineError, "nothing was overwritten"):
            pipelines.import_document(self.config, document)
        self.assertEqual(pipelines.load(self.config, "Daily checks"), original)

        imported = pipelines.import_document(self.config, document, name="Daily checks copy")
        self.assertEqual(imported["name"], "Daily checks copy")
        self.assertEqual(pipelines.saved_ones(self.config), ["Daily checks", "Daily checks copy"])

    def test_two_concurrent_imports_have_exactly_one_winner(self) -> None:
        document = pipelines.a_starting_pipeline()
        document["name"] = "Race winner"
        written = json.dumps(document)
        barrier = threading.Barrier(2)
        original_link = pipelines.os.link
        outcomes: list[str] = []

        def linked(source, target):
            barrier.wait(timeout=5)
            return original_link(source, target)

        def importing() -> None:
            try:
                pipelines.import_document(self.config, written)
                outcomes.append("saved")
            except pipelines.PipelineError:
                outcomes.append("refused")

        with mock.patch.object(pipelines.os, "link", side_effect=linked):
            threads = [threading.Thread(target=importing) for _ in range(2)]
            for one in threads:
                one.start()
            for one in threads:
                one.join(timeout=10)
        self.assertEqual(sorted(outcomes), ["refused", "saved"])
        self.assertEqual(pipelines.saved_ones(self.config), ["Race winner"])

    def test_unknown_exchange_versions_and_invalid_automations_fail_closed(self) -> None:
        for document in (
            {"schema": pipelines.AUTOMATION_DOCUMENT_SCHEMA, "version": 99,
             "automation": pipelines.a_starting_pipeline()},
            {"schema": "some.other.file", "version": 1,
             "automation": pipelines.a_starting_pipeline()},
            {"schema": pipelines.AUTOMATION_DOCUMENT_SCHEMA, "version": 1,
             "future_envelope": {"must": "survive-or-reject"},
             "automation": pipelines.a_starting_pipeline()},
            {"name": "Bad", "nodes": "not a list", "edges": []},
        ):
            with self.subTest(document=document):
                with self.assertRaises(pipelines.PipelineError):
                    pipelines.import_document(self.config, json.dumps(document))
        self.assertEqual(pipelines.saved_ones(self.config), [])

    def test_import_refuses_values_the_editor_reader_would_change(self) -> None:
        overlong = pipelines.a_starting_pipeline()
        overlong["name"] = "Overlong label"
        overlong["nodes"][0]["label"] = "x" * 81
        duplicate_arrow = pipelines.a_starting_pipeline()
        duplicate_arrow["name"] = "Duplicate arrow"
        duplicate_arrow["edges"].append(copy.deepcopy(duplicate_arrow["edges"][0]))
        spaced_id = pipelines.a_starting_pipeline()
        spaced_id["name"] = "Spaced id"
        spaced_id["nodes"][0]["id"] = " start "
        spaced_id["edges"][0]["from"] = " start "
        for document, message in (
            (overlong, "Nothing was imported"),
            (duplicate_arrow, "Nothing was imported"),
            (spaced_id, "not here"),
        ):
            with self.subTest(name=document["name"]):
                with self.assertRaisesRegex(pipelines.PipelineError, message):
                    pipelines.import_document(self.config, json.dumps(document))
        self.assertEqual(pipelines.saved_ones(self.config), [])

    def test_invalid_utf8_saved_file_does_not_hide_healthy_automations(self) -> None:
        good = pipelines.a_starting_pipeline()
        good["name"] = "Healthy"
        pipelines.save(self.config, good)
        (pipelines.folder(self.config) / "invalid-utf8.json").write_bytes(b"\xff\xfe")
        saved, problems = pipelines.saved_inventory(self.config)
        self.assertEqual(saved, ["Healthy"])
        self.assertEqual(len(problems), 1)
        self.assertIn("invalid-utf8.json", problems[0])

    def test_saved_and_imported_definitions_share_one_visible_size_boundary(self) -> None:
        definition = pipelines.a_starting_pipeline()
        definition["name"] = "Bounded"
        written = json.dumps(definition)
        with mock.patch.object(pipelines, "MAX_AUTOMATION_DOCUMENT_BYTES", 400):
            with self.assertRaisesRegex(pipelines.PipelineError, "visible .* limit"):
                pipelines.save(self.config, definition)
            with self.assertRaisesRegex(pipelines.PipelineError, "1 to 400"):
                pipelines.import_document(self.config, written)
        self.assertEqual(pipelines.saved_ones(self.config), [])

    def test_the_size_boundary_includes_the_portable_export_envelope(self) -> None:
        definition = pipelines.read_it(pipelines.a_starting_pipeline())
        inner_size = len((json.dumps(definition, indent=2) + "\n").encode("utf-8"))
        portable_size = len((json.dumps({
            "schema": pipelines.AUTOMATION_DOCUMENT_SCHEMA,
            "version": pipelines.AUTOMATION_DOCUMENT_VERSION,
            "automation": definition,
        }, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        self.assertGreater(portable_size, inner_size)
        with mock.patch.object(
            pipelines, "MAX_AUTOMATION_DOCUMENT_BYTES", portable_size - 1
        ):
            with self.assertRaisesRegex(pipelines.PipelineError, "visible 10 MB"):
                pipelines.save(self.config, definition)
        self.assertEqual(pipelines.saved_ones(self.config), [])


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
        self.assertEqual(by_id["tests"].effective_outcome, pipelines.OUTCOME_WARNING)
        self.assertEqual(run.outcome, pipelines.OUTCOME_WARNING)
        self.assertTrue(run.passed)

    def test_an_allowed_failure_is_a_truthful_warning_and_does_not_block_a_gate(self) -> None:
        drawn = {
            "name": "Allowed", "nodes": [
                {"id": "start", "kind": "start", "label": "Start", "settings": {}},
                {"id": "optional", "kind": "git_repo", "label": "Optional",
                 "settings": {"even_if_it_fails": True}},
                {"id": "gate", "kind": "gate", "label": "Gate", "settings": {"needs": "all"}},
            ],
            "edges": [{"from": "start", "to": "optional"}, {"from": "optional", "to": "gate"}],
        }
        with self.stand_in({"optional": (False, "optional failed", "")}):
            run = pipelines.run_it(self.config, drawn)
        by_id = {node.id: node for node in run.nodes}
        self.assertEqual(by_id["optional"].state, pipelines.FAILED)
        self.assertEqual(by_id["optional"].effective_outcome, pipelines.OUTCOME_WARNING)
        self.assertEqual(by_id["gate"].state, pipelines.PASSED)
        self.assertTrue(run.passed)
        self.assertEqual(run.outcome, pipelines.OUTCOME_WARNING)

    def test_stop_after_a_handler_returns_fences_its_late_success(self) -> None:
        stopped = False

        def finishes_late(*args, **kwargs):
            nonlocal stopped
            stopped = True
            return True, "late success", ""

        with mock.patch.object(pipelines, "_do_one", finishes_late):
            run = pipelines.run_it(
                self.config, a_line("start"), stopping=lambda: stopped
            )
        self.assertFalse(run.passed)
        self.assertEqual(run.outcome, pipelines.OUTCOME_CANCELLED)
        self.assertEqual(run.nodes[0].state, pipelines.CANCELLED)

    def test_deadline_after_a_handler_returns_fences_its_late_success(self) -> None:
        clock = [0.0]
        drawn = a_line("start")
        drawn["nodes"][0]["settings"] = {"longest": 1}

        def finishes_late(*args, **kwargs):
            clock[0] = 2.0
            return True, "late success", ""

        with mock.patch.object(pipelines.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(pipelines, "_do_one", finishes_late):
            run = pipelines.run_it(self.config, drawn)
        self.assertFalse(run.passed)
        self.assertEqual(run.outcome, pipelines.OUTCOME_TIMED_OUT)
        self.assertEqual(run.nodes[0].state, pipelines.TIMED_OUT)

    def test_an_empty_automation_cannot_report_a_pass(self) -> None:
        with self.assertRaises(pipelines.PipelineError):
            pipelines.run_it(self.config, {"name": "Empty", "nodes": [], "edges": []})

    def test_a_skipped_suite_is_incomplete_and_a_flaky_suite_is_a_warning(self) -> None:
        drawn = a_line("start")
        with mock.patch.object(
            pipelines, "_do_one",
            return_value=(False, "one check was skipped", "", pipelines.OUTCOME_INCOMPLETE),
        ):
            skipped = pipelines.run_it(self.config, drawn)
        self.assertFalse(skipped.passed)
        self.assertEqual(skipped.outcome, pipelines.OUTCOME_INCOMPLETE)
        with mock.patch.object(
            pipelines, "_do_one",
            return_value=(True, "one check was flaky", "", pipelines.OUTCOME_WARNING),
        ):
            flaky = pipelines.run_it(self.config, drawn)
        self.assertTrue(flaky.passed)
        self.assertEqual(flaky.outcome, pipelines.OUTCOME_WARNING)

    def test_a_nested_definition_is_frozen_before_a_later_edit(self) -> None:
        child = {
            "name": "Child", "nodes": [
                {"id": "old", "kind": "start", "label": "Old child", "settings": {}}
            ], "edges": [],
        }
        pipelines.save(self.config, child)
        parent = {
            "name": "Parent", "nodes": [
                {"id": "child", "kind": "another_pipeline", "label": "Child run",
                 "settings": {"pipeline": "Child"}}
            ], "edges": [],
        }
        frozen = pipelines.freeze_definition(self.config, parent)
        child["nodes"][0]["label"] = "New child"
        pipelines.save(self.config, child)
        run = pipelines.run_it(self.config, parent, frozen=frozen)
        self.assertIn("Old child", run.nodes[0].detail)
        self.assertNotIn("New child", run.nodes[0].detail)

    def test_repeated_nested_human_gates_have_distinct_occurrence_decisions(self) -> None:
        child = {
            "name": "Approval child", "edges": [],
            "nodes": [{
                "id": "approve", "kind": "wait_for_a_person", "label": "Approve",
                "settings": {"question": "Carry on?"},
            }],
        }
        pipelines.save(self.config, child)
        parent = {
            "name": "Twice", "edges": [],
            "nodes": [
                {"id": "first", "kind": "another_pipeline", "label": "First",
                 "settings": {"pipeline": "Approval child"}},
                {"id": "second", "kind": "another_pipeline", "label": "Second",
                 "settings": {"pipeline": "Approval child"}},
            ],
        }
        seen: list[str] = []

        def answer(decision_id: str) -> bool:
            seen.append(decision_id)
            return True

        run = pipelines.run_it(
            self.config, parent, run_id="nested-run", decision_nonce="attempt-one",
            waiting_on=answer,
        )
        self.assertTrue(run.passed, run.said)
        self.assertEqual(len(seen), 2)
        self.assertNotEqual(seen[0], seen[1])

    def test_nested_warning_remains_a_warning_in_the_parent(self) -> None:
        child = {
            "name": "Warning child", "edges": [],
            "nodes": [{"id": "optional", "kind": "git_repo", "label": "Optional",
                       "settings": {"even_if_it_fails": True}}],
        }
        pipelines.save(self.config, child)
        parent = {
            "name": "Parent", "edges": [],
            "nodes": [{"id": "child", "kind": "another_pipeline", "label": "Child",
                       "settings": {"pipeline": "Warning child"}}],
        }
        with mock.patch.object(
            pipelines, "_run_git_repo", return_value=(False, "allowed failure", "")
        ):
            run = pipelines.run_it(self.config, parent)
        self.assertTrue(run.passed)
        self.assertEqual(run.outcome, pipelines.OUTCOME_WARNING)
        self.assertEqual(run.nodes[0].effective_outcome, pipelines.OUTCOME_WARNING)

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
        passed, said, _detail, _outcome = pipelines._run_security_scan(self.config, node, None)
        self.assertTrue(passed, said)
        self.assertTrue(said, "a step that says nothing tells nobody anything")
        self.assertNotIn("went wrong", said)

    def test_a_check_that_fails_says_why_in_words(self) -> None:
        # A shape the scanner really knows, so this tests the pipeline rather
        # than the scanner's opinion of made-up text.
        (self.root / "keys.txt").write_text("AKIA" + "Q" * 16 + "\n", encoding="utf-8")
        node = {"id": "scan", "label": "Security scan", "settings": {"paths": ["keys.txt"]}}
        passed, said, detail, _outcome = pipelines._run_security_scan(self.config, node, None)
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
        kept = list((self.root / ".harness" / "pipelines" / "evidence").rglob("*.json"))
        self.assertEqual(len(kept), 1, "the evidence node wrote one run-scoped file")
        qa_results = list((self.root / ".harness" / "qa" / "runs").rglob("result.json"))
        self.assertEqual(len(qa_results), 1, "the one-off scan kept immutable run evidence")


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
        answer = mock.Mock(text="print('hello')", finish_reason="stop")
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

    def test_the_ai_node_sends_all_200k_instruction_characters_to_the_provider(self) -> None:
        exact = "x" * pipelines.AI_UNIT_INSTRUCTION_CHARACTERS
        complete = mock.Mock(return_value=mock.Mock(text="assert True", finish_reason="stop"))
        with mock.patch(
            "our_harness.providers.create_provider",
            return_value=mock.Mock(complete=complete),
        ):
            passed, said, _detail = pipelines._run_ai_unit_test(
                self.config,
                {"id": "ai", "label": "AI", "settings": {
                    "instructions": exact,
                    "write_to": "full_instruction_test.py",
                }},
                None,
            )
        self.assertTrue(passed, said)
        request = complete.call_args.args[0]
        self.assertEqual(request.messages[0]["content"], exact)
        self.assertEqual(len(request.messages[0]["content"]), len(exact))
        self.assertEqual(
            request.max_output_tokens,
            self.config.get("provider.max_output_tokens"),
        )

    def test_exact_200k_instructions_keep_whitespace_at_both_edges_to_provider(self) -> None:
        exact = "\n\t  " + (
            "x" * (pipelines.AI_UNIT_INSTRUCTION_CHARACTERS - 8)
        ) + "  \t\n"
        self.assertEqual(len(exact), pipelines.AI_UNIT_INSTRUCTION_CHARACTERS)
        drawn = a_line("start", "ai_unit_test")
        drawn["nodes"][1]["settings"] = {
            "instructions": exact,
            "write_to": "edge_whitespace_test.py",
        }
        node = pipelines.read_it(drawn)["nodes"][1]
        complete = mock.Mock(return_value=mock.Mock(text="assert True", finish_reason="stop"))

        with mock.patch(
            "our_harness.providers.create_provider",
            return_value=mock.Mock(complete=complete),
        ):
            passed, said, _detail = pipelines._run_ai_unit_test(
                self.config, node, None,
            )

        self.assertTrue(passed, said)
        handed_to_provider = complete.call_args.args[0].messages[0]["content"]
        self.assertEqual(handed_to_provider, exact)
        self.assertTrue(handed_to_provider.startswith("\n\t  "))
        self.assertTrue(handed_to_provider.endswith("  \t\n"))

    def test_the_ai_node_never_saves_a_provider_truncated_test_file(self) -> None:
        answer = mock.Mock(text="def test_half():\n    assert ", finish_reason="length")
        with mock.patch(
            "our_harness.providers.create_provider",
            return_value=mock.Mock(complete=mock.Mock(return_value=answer)),
        ):
            passed, said, detail = pipelines._run_ai_unit_test(
                self.config,
                {"id": "ai", "label": "AI", "settings": {
                    "instructions": "write a comprehensive suite",
                    "write_to": "not_partial_test.py",
                }},
                None,
            )
        self.assertFalse(passed)
        self.assertIn("incomplete", said.lower())
        self.assertIn("did not save", detail)
        self.assertFalse(
            (self.root / ".harness" / "pipelines" / "drafts" / "not_partial_test.py").exists()
        )

    def test_the_ai_node_never_saves_an_unknown_nonterminal_response(self) -> None:
        answer = mock.Mock(
            text="def test_partial():\n    assert ", finish_reason="unknown",
        )
        with mock.patch(
            "our_harness.providers.create_provider",
            return_value=mock.Mock(complete=mock.Mock(return_value=answer)),
        ):
            passed, said, detail = pipelines._run_ai_unit_test(
                self.config,
                {"id": "ai", "label": "AI", "settings": {
                    "instructions": "write a complete suite",
                    "write_to": "unknown_partial.py",
                }},
                None,
            )
        self.assertFalse(passed)
        self.assertIn("incomplete", said.lower())
        self.assertIn("explicit successful completion", detail)
        self.assertFalse(
            (self.root / ".harness" / "pipelines" / "drafts" / "unknown_partial.py").exists()
        )

    def test_the_ai_node_never_infers_success_when_completion_reason_is_missing(self) -> None:
        from our_harness.models import ProviderResponse

        answer = ProviderResponse(text="def test_partial():\n    assert ")
        with mock.patch(
            "our_harness.providers.create_provider",
            return_value=mock.Mock(complete=mock.Mock(return_value=answer)),
        ):
            passed, said, detail = pipelines._run_ai_unit_test(
                self.config,
                {"id": "ai", "label": "AI", "settings": {
                    "instructions": "write a complete suite",
                    "write_to": "missing_reason_partial.py",
                }},
                None,
            )
        self.assertFalse(passed)
        self.assertIn("incomplete", said.lower())
        self.assertIn("no completion reason", detail)
        self.assertFalse(
            (self.root / ".harness" / "pipelines" / "drafts" / "missing_reason_partial.py").exists()
        )

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
        answer = mock.Mock(text="print('hello')", finish_reason="stop")
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
        answer = mock.Mock(
            text="import os\nos.system('echo pwned')", finish_reason="stop",
        )
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
        answer = mock.Mock(
            text="```js\nconsole.log('hello');\n```", finish_reason="stop",
        )
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
        evidence = list((self.root / ".harness" / "pipelines" / "evidence").rglob("*.json"))
        self.assertEqual(len(evidence), 1)
        written = json.loads(evidence[0].read_text(encoding="utf-8"))
        self.assertEqual(written[0]["label"], "Checks")

    def test_evidence_is_redacted_and_never_clobbered(self) -> None:
        secret = "sk-abcdefghijklmnop"
        done = [pipelines.NodeResult(
            id="a", kind="suite", label=secret, state=pipelines.FAILED, said=f"token={secret}"
        )]
        node = {"id": "e", "label": "Evidence", "settings": {"write_to": "evidence.json"}}
        first, _said, _detail = pipelines._run_artifact(
            self.config, node, None, so_far=done
        )
        where = self.root / "evidence.json"
        first_body = where.read_text(encoding="utf-8")
        second, said, _detail = pipelines._run_artifact(
            self.config, node, None, so_far=[]
        )
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertIn("already exists", said)
        self.assertEqual(where.read_text(encoding="utf-8"), first_body)
        self.assertNotIn(secret, first_body)
        self.assertIn("[REDACTED]", first_body)

    def test_the_evidence_node_will_not_write_outside_the_project(self) -> None:
        with self.assertRaises(HarnessError):
            pipelines._run_artifact(
                self.config,
                {"id": "e", "label": "Evidence", "settings": {"write_to": "../out.json"}},
                None, so_far=[],
            )

    def test_a_unit_test_node_with_no_command_says_what_to_do(self) -> None:
        passed, said, detail, _outcome = pipelines._run_unit_test(
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
