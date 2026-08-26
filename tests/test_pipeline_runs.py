from __future__ import annotations

import copy
import multiprocessing
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import unittest

from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness import editor, pipelines
from our_harness.pipeline_runs import (
    PipelineRunConflict,
    PipelineRunNotFound,
    PipelineRunStore,
)


def _definition(label: str = "Start") -> dict:
    return {
        "name": "Durable automation",
        "nodes": [{"id": "start", "kind": "start", "label": label, "settings": {}}],
        "edges": [],
    }


def _accept_in_process(root: str, runtime: str, request_id: str, label: str, queue) -> None:
    os.environ["OUR_HARNESS_PIPELINE_RUN_DIR"] = runtime
    config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), Path(root), [], {})
    try:
        accepted, created = PipelineRunStore(config).accept(
            _definition(label), source="process-test", request_id=request_id
        )
        queue.put(("ok", accepted["run_id"], created))
    except Exception as exc:  # pragma: no cover - asserted in the parent
        queue.put(("error", type(exc).__name__, str(exc)))


def _leave_running_in_process(root: str, runtime: str, queue) -> None:
    os.environ["OUR_HARNESS_PIPELINE_RUN_DIR"] = runtime
    config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), Path(root), [], {})
    store = PipelineRunStore(config)
    accepted, _ = store.accept(_definition(), source="crash-test", request_id="crash-once")
    store.start(accepted["run_id"], accepted["attempt_id"])
    queue.put(accepted["run_id"])


class PipelineRunStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.container = Path(self.temporary.name).resolve()
        self.root = self.container / "project"
        (self.root / ".harness").mkdir(parents=True)
        self.runtime = self.container / "trusted-user-control"
        self.prior_override = os.environ.get("OUR_HARNESS_PIPELINE_RUN_DIR")
        os.environ["OUR_HARNESS_PIPELINE_RUN_DIR"] = str(self.runtime)
        self.addCleanup(self._restore_override)
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})

    def _restore_override(self) -> None:
        if self.prior_override is None:
            os.environ.pop("OUR_HARNESS_PIPELINE_RUN_DIR", None)
        else:
            os.environ["OUR_HARNESS_PIPELINE_RUN_DIR"] = self.prior_override

    def test_two_processes_replay_exact_request_and_reject_other_concurrent_work(self) -> None:
        store = PipelineRunStore(self.config)
        first, created = store.accept(
            _definition(), source="process-test", request_id="same-request"
        )
        self.assertTrue(created)
        context = multiprocessing.get_context("spawn")

        exact_queue = context.Queue()
        exact = context.Process(
            target=_accept_in_process,
            args=(str(self.root), str(self.runtime), "same-request", "Start", exact_queue),
        )
        exact.start()
        exact.join(10)
        self.assertEqual(exact.exitcode, 0)
        self.assertEqual(exact_queue.get(timeout=2), ("ok", first["run_id"], False))

        other_queue = context.Queue()
        other = context.Process(
            target=_accept_in_process,
            args=(str(self.root), str(self.runtime), "other-request", "Start", other_queue),
        )
        other.start()
        other.join(10)
        self.assertEqual(other.exitcode, 0)
        status = other_queue.get(timeout=2)
        self.assertEqual(status[0:2], ("error", "PipelineRunConflict"))

    def test_request_id_digest_conflict_never_creates_another_run(self) -> None:
        store = PipelineRunStore(self.config)
        first, _ = store.accept(_definition(), source="panel", request_id="one")
        with self.assertRaises(PipelineRunConflict):
            store.accept(_definition("Changed"), source="panel", request_id="one")
        self.assertEqual(store.active()["run_id"], first["run_id"])

    def test_editor_entry_cannot_overlap_a_panel_owned_run(self) -> None:
        saved = _definition()
        pipelines.save(self.config, saved)
        store = PipelineRunStore(self.config)
        store.accept(saved, source="panel", request_id="panel-is-active")
        with self.assertRaises(PipelineRunConflict):
            editor._run_an_automation(self.config, {"name": saved["name"]})

    def test_stale_stop_and_decision_cannot_control_another_run(self) -> None:
        store = PipelineRunStore(self.config)
        accepted, _ = store.accept(_definition(), source="panel")
        run_id = accepted["run_id"]
        attempt_id = accepted["attempt_id"]
        store.start(run_id, attempt_id)
        store.set_waiting(run_id, attempt_id, "start")
        with self.assertRaises(PipelineRunNotFound):
            store.request_stop("not-this-run")
        with self.assertRaises(PipelineRunConflict):
            store.decide(run_id, "old-step", True)
        with self.assertRaises(PipelineRunConflict):
            store.append_event(
                run_id, "stale-worker-attempt",
                {"kind": "pipeline_node", "payload": {"state": "passed"}},
            )
        with self.assertRaises(PipelineRunConflict):
            store.finish(
                run_id, "stale-worker-attempt",
                {"passed": True, "outcome": "passed", "said": "stale"},
            )
        self.assertEqual(store.get(run_id)["state"], "waiting")
        self.assertEqual(store.decide(run_id, "start", True)["run_id"], run_id)

    def test_stop_fences_a_late_success(self) -> None:
        store = PipelineRunStore(self.config)
        accepted, _ = store.accept(_definition(), source="panel")
        run_id = accepted["run_id"]
        attempt_id = accepted["attempt_id"]
        store.start(run_id, attempt_id)
        store.request_stop(run_id)
        finished = store.finish(
            run_id, attempt_id, {"passed": True, "outcome": "passed", "said": "late"}
        )
        self.assertEqual(finished["state"], "cancelled")
        self.assertFalse(finished["result"]["passed"])
        self.assertEqual(finished["result"]["outcome"], "cancelled")
        with self.assertRaises(PipelineRunConflict):
            store.append_event(
                run_id, attempt_id,
                {"kind": "pipeline_node", "payload": {"state": "passed"}},
            )

    def test_restart_recovers_a_dead_owner_as_interrupted(self) -> None:
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        process = context.Process(
            target=_leave_running_in_process,
            args=(str(self.root), str(self.runtime), queue),
        )
        process.start()
        run_id = queue.get(timeout=10)
        process.join(10)
        self.assertEqual(process.exitcode, 0)
        recovered = PipelineRunStore(self.config).get(run_id)
        self.assertEqual(recovered["state"], "interrupted")
        self.assertFalse(recovered["result"]["passed"])
        self.assertIn("owner stopped", recovered["result"]["said"])

    def test_runtime_path_inside_project_is_rejected(self) -> None:
        os.environ["OUR_HARNESS_PIPELINE_RUN_DIR"] = str(self.root / ".runtime")
        with self.assertRaises(PipelineRunConflict):
            PipelineRunStore(self.config)

    def test_every_persisted_sink_is_redacted_and_events_are_sequenced(self) -> None:
        secret = "sk-abcdefghijklmnop"
        store = PipelineRunStore(self.config)
        definition = _definition(secret)
        accepted, _ = store.accept(definition, source=f"panel {secret}")
        run_id = accepted["run_id"]
        attempt_id = accepted["attempt_id"]
        store.start(run_id, attempt_id)
        store.append_event(run_id, attempt_id, {
            "kind": "pipeline_node", "node": "start",
            "payload": {"said": f"Bearer {secret}"},
        })
        store.finish(run_id, attempt_id, {
            "passed": False, "outcome": "failed", "said": f"token={secret}", "nodes": [],
        })
        events = store.events(run_id)
        self.assertEqual([one["sequence"] for one in events], list(range(1, len(events) + 1)))
        persisted = repr(store.get(run_id)) + repr(events)
        self.assertNotIn(secret, persisted)
        self.assertIn("[REDACTED]", persisted)

    def test_unsafe_or_secret_request_ids_are_rejected_before_any_sink(self) -> None:
        store = PipelineRunStore(self.config)
        for request_id in ("../outside", "has a space", "sk-abcdefghijklmnop"):
            with self.subTest(request_id=request_id), self.assertRaises(PipelineRunConflict):
                store.accept(_definition(), source="panel", request_id=request_id)
        secret = b"sk-abcdefghijklmnop"
        for path in (store.path, Path(str(store.path) + "-wal"), Path(str(store.path) + "-shm")):
            if path.exists():
                self.assertNotIn(secret, path.read_bytes())

    def test_blind_database_state_forgery_fails_closed(self) -> None:
        store = PipelineRunStore(self.config)
        accepted, _ = store.accept(_definition(), source="panel", request_id="tamper-test")
        connection = sqlite3.connect(store.path)
        try:
            connection.execute(
                "UPDATE pipeline_runs SET state='passed' WHERE run_id=?", (accepted["run_id"],)
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(PipelineRunConflict):
            store.get(accepted["run_id"])

    def test_a_dead_worker_thread_is_recovered_before_next_admission(self) -> None:
        store = PipelineRunStore(self.config)
        accepted, _ = store.accept(_definition(), source="panel", request_id="dead-thread")

        def start_and_die() -> None:
            store.start(accepted["run_id"], accepted["attempt_id"])

        worker = threading.Thread(target=start_and_die)
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        next_run, created = store.accept(
            _definition("Next"), source="panel", request_id="after-dead-thread"
        )
        self.assertTrue(created)
        self.assertNotEqual(next_run["run_id"], accepted["run_id"])
        recovered = store.get(accepted["run_id"])
        self.assertEqual(recovered["state"], "interrupted")

    def test_same_request_replays_after_project_directory_rename(self) -> None:
        store = PipelineRunStore(self.config)
        first, _ = store.accept(
            _definition(), source="panel", request_id="across-rename"
        )
        store.start(first["run_id"], first["attempt_id"])
        store.finish(
            first["run_id"], first["attempt_id"],
            {"passed": True, "outcome": "passed", "said": "done"},
        )
        moved = self.container / "renamed-project"
        self.root.rename(moved)
        self.root = moved
        moved_config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), moved, [], {})
        replay, created = PipelineRunStore(moved_config).accept(
            _definition(), source="panel", request_id="across-rename"
        )
        self.assertFalse(created)
        self.assertEqual(replay["run_id"], first["run_id"])
        with self.assertRaises(PipelineRunConflict):
            PipelineRunStore(moved_config).accept(
                _definition("Changed"), source="panel", request_id="across-rename"
            )

    def test_copied_project_descriptor_cannot_open_the_original_authority(self) -> None:
        original = PipelineRunStore(self.config)
        original.accept(_definition(), source="panel")
        copied = self.container / "copied-project"
        shutil.copytree(self.root, copied)
        copied_config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), copied, [], {})
        with self.assertRaises(PipelineRunConflict):
            PipelineRunStore(copied_config)


if __name__ == "__main__":
    unittest.main()
