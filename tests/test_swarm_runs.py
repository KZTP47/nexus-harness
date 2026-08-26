from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError
from our_harness import cancellation, swarm
from our_harness.swarm_runs import SwarmRunStore, bind, provider_effect


def _leave_provider_effect_dispatched(root: str, runtime: str, marker: str) -> None:
    os.environ["OUR_HARNESS_SWARM_RUN_DIR"] = runtime
    config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), Path(root), [], {})
    store = SwarmRunStore(config)
    accepted, _ = store.accept("crash-request", {"objective": "one"})
    run_id = accepted["run_id"]
    store.start(run_id)
    store.begin_effect(run_id, "route-and-chat", "request-digest")
    Path(marker).write_text(run_id, encoding="utf-8")


def _leave_accepted(root: str, runtime: str, marker: str) -> None:
    os.environ["OUR_HARNESS_SWARM_RUN_DIR"] = runtime
    config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), Path(root), [], {})
    accepted, _ = SwarmRunStore(config).accept("accepted-crash", {"kind": "chat"})
    Path(marker).write_text(accepted["run_id"], encoding="utf-8")


def _leave_acknowledged_without_checkpoint(root: str, runtime: str, marker: str) -> None:
    os.environ["OUR_HARNESS_SWARM_RUN_DIR"] = runtime
    config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), Path(root), [], {})
    store = SwarmRunStore(config)
    accepted, _ = store.accept("ack-crash", {"kind": "chat"})
    run_id = accepted["run_id"]
    store.start(run_id)
    effect = store.begin_effect(run_id, "resource", "digest")
    store.finish_effect(run_id, effect, True)
    Path(marker).write_text(run_id, encoding="utf-8")


def _own_board_until_stopped(root: str, runtime: str, marker: str) -> None:
    os.environ["OUR_HARNESS_SWARM_RUN_DIR"] = runtime
    config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), Path(root), [], {})
    store = SwarmRunStore(config)
    accepted, _ = store.accept(
        "cross-process-board", {"kind": "board_order", "board": {"version": 1}}
    )
    run_id = accepted["run_id"]
    store.start(run_id)
    Path(marker).write_text(run_id, encoding="utf-8")
    limit = time.time() + 10
    while time.time() < limit and not store.should_stop(run_id):
        time.sleep(0.02)
    if store.should_stop(run_id):
        store.fail(run_id, "stopped by another process", stopped=True)


def _hold_conversation_turn(
    root: str, runtime: str, ready_marker: str, release_marker: str,
) -> None:
    os.environ["OUR_HARNESS_SWARM_RUN_DIR"] = runtime
    config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), Path(root), [], {})
    store = SwarmRunStore(config)
    accepted, _ = store.accept("cross-process-chat-owner", {"kind": "chat"})
    with store.conversation_turn(accepted["run_id"], "chat-one"):
        Path(ready_marker).write_text("ready", encoding="utf-8")
        limit = time.time() + 10
        while time.time() < limit and not Path(release_marker).exists():
            time.sleep(0.02)


class SwarmRunStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.container = Path(self.temporary.name).resolve()
        self.root = self.container / "project"
        (self.root / ".harness").mkdir(parents=True)
        self.runtime = self.container / "trusted-user-control"
        self.prior_override = os.environ.get("OUR_HARNESS_SWARM_RUN_DIR")
        os.environ["OUR_HARNESS_SWARM_RUN_DIR"] = str(self.runtime)
        self.addCleanup(self._restore_override)
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})

    def _restore_override(self) -> None:
        if self.prior_override is None:
            os.environ.pop("OUR_HARNESS_SWARM_RUN_DIR", None)
        else:
            os.environ["OUR_HARNESS_SWARM_RUN_DIR"] = self.prior_override

    def _running(self, request: str = "request", snapshot: dict | None = None):
        store = SwarmRunStore(self.config)
        accepted, _ = store.accept(request, snapshot or {"objective": "test"})
        return store, store.start(accepted["run_id"])["run_id"]

    def test_logical_chat_turn_is_exclusive_across_processes_but_other_chats_run(self) -> None:
        ready = self.container / "chat-owner-ready"
        release = self.container / "chat-owner-release"
        process = multiprocessing.Process(
            target=_hold_conversation_turn,
            args=(str(self.root), str(self.runtime), str(ready), str(release)),
        )
        process.start()
        try:
            limit = time.time() + 5
            while time.time() < limit and not ready.exists():
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "the other process did not claim the chat")

            store = SwarmRunStore(self.config)
            same, _ = store.accept("same-chat-contender", {"kind": "chat"})
            with self.assertRaisesRegex(HarnessError, "already working"):
                with store.conversation_turn(same["run_id"], "chat-one", timeout=0):
                    self.fail("a second process entered the same logical chat")

            other, _ = store.accept("other-chat-contender", {"kind": "chat"})
            with store.conversation_turn(other["run_id"], "chat-two", timeout=0):
                pass
        finally:
            release.write_text("release", encoding="utf-8")
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(2)
        self.assertEqual(process.exitcode, 0)

    def test_integrity_read_uses_one_snapshot_while_another_connection_appends(self) -> None:
        store, run_id = self._running("snapshot-read")
        with store._read() as db:
            before = db.execute(
                "SELECT event_count FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            store.event(run_id, "concurrent_append", {"from": "another connection"})
            visible = db.execute(
                "SELECT COUNT(*) FROM events WHERE run_id=?", (run_id,)
            ).fetchone()
            self.assertEqual(int(visible[0]), int(before[0]))
        self.assertEqual(store.get(run_id)["event_count"], int(before[0]) + 1)

    def _standing(self) -> dict:
        return {"board": {
            "version": 1,
            "agents": [{
                "id": "agent-1", "name": "One", "who": "route", "job": "",
                "ready": True, "filed_as": "one", "why_not": "",
            }],
            "projects": [{
                "id": "project-1", "path": str(self.root), "name": "project",
                "is_there": True, "tasks": ["Do it"],
            }],
            "works_on": [{"agent": "agent-1", "project": "project-1"}],
            "talks_to": [],
        }}

    def test_request_id_is_project_scoped_but_snapshot_is_immutable_within_project(self) -> None:
        store = SwarmRunStore(self.config)
        first, created = store.accept("same-browser-id", {"objective": "one"})
        self.assertTrue(created)
        replay, created = store.accept("same-browser-id", {"objective": "one"})
        self.assertFalse(created)
        self.assertEqual(replay["run_id"], first["run_id"])
        with self.assertRaises(HarnessError):
            store.accept("same-browser-id", {"objective": "changed"})

        other_root = self.container / "other-project"
        (other_root / ".harness").mkdir(parents=True)
        other = SwarmRunStore(LoadedConfig(
            copy.deepcopy(DEFAULT_CONFIG), other_root, [], {}
        ))
        independent, created = other.accept(
            "same-browser-id", {"objective": "changed"}
        )
        self.assertTrue(created)
        self.assertNotEqual(independent["run_id"], first["run_id"])

    def test_state_transitions_and_effect_acknowledgement_are_cas_fenced(self) -> None:
        store, run_id = self._running()
        with self.assertRaises(HarnessError):
            store.start(run_id)
        effect = store.begin_effect(run_id, "resource", "digest")
        second = store.begin_effect(run_id, "other-resource", "digest-2")
        with self.assertRaises(HarnessError):
            store.finish_effect(run_id, "not-the-effect", True)
        dispatched = store.projection(run_id)["events"]
        self.assertEqual(
            len([one for one in dispatched if one["kind"] == "provider_dispatched"]), 2
        )
        self.assertFalse(any(one["kind"] == "acknowledged" for one in dispatched))
        store.finish_effect(run_id, effect, True)
        with self.assertRaises(HarnessError):
            store.checkpoint(run_id, "too_early", {})
        store.finish_effect(run_id, second, True)
        with self.assertRaises(HarnessError):
            store.finish_effect(run_id, effect, True)
        store.checkpoint(run_id, "turn_saved", {"answer": "done"})
        store.finish(run_id, {"answer": "done"})
        with self.assertRaises(HarnessError):
            store.finish(run_id, {"answer": "late"})
        store.fail(run_id, "late failure")
        with self.assertRaises(HarnessError):
            store.event(run_id, "late", {})
        finished = store.projection(run_id)
        self.assertEqual(finished["status"], "complete")
        self.assertEqual(finished["result"], {"answer": "done"})
        self.assertEqual(
            len([one for one in finished["events"] if one["kind"] == "complete"]), 1
        )

    def test_delivery_unknown_effect_cannot_checkpoint_dispatch_again_or_complete(self) -> None:
        store, run_id = self._running("unknown-effect")
        effect = store.begin_effect(run_id, "resource", "digest")
        store.finish_effect(run_id, effect, False)
        self.assertEqual(store.get(run_id)["status"], "delivery_unknown")
        with self.assertRaises(HarnessError):
            store.checkpoint(run_id, "unsafe", {})
        with self.assertRaises(HarnessError):
            store.begin_effect(run_id, "resource", "digest-two")
        with self.assertRaises(HarnessError):
            store.finish(run_id, {"complete": True})
        store.fail(run_id, "provider rejected the schema")
        projected = store.projection(run_id)
        self.assertEqual(projected["error"], "provider rejected the schema")
        self.assertEqual(projected["events"][-1]["kind"], "failure_detail")

    def test_one_uncertain_parallel_effect_keeps_the_whole_run_uncertain(self) -> None:
        store, run_id = self._running("parallel-uncertain")
        failed = store.begin_effect(run_id, "resource-a", "digest-a")
        succeeded = store.begin_effect(run_id, "resource-b", "digest-b")
        store.finish_effect(run_id, failed, False)
        store.finish_effect(run_id, succeeded, True)
        projected = store.get(run_id)
        self.assertEqual(projected["status"], "delivery_unknown")
        self.assertEqual(projected["effect_status"], "delivery_unknown")
        with self.assertRaises(HarnessError):
            store.checkpoint(run_id, "unsafe", {})

    def test_parallel_workers_journal_overlapping_provider_effects_before_final_checkpoint(self) -> None:
        store, run_id = self._running("parallel-provider-effects")
        active = 0
        most_active = 0
        guard = threading.Lock()

        def ask(route: str) -> None:
            nonlocal active, most_active
            with provider_effect(self.config, route, "pair-chat", f"digest-{route}"):
                with guard:
                    active += 1
                    most_active = max(most_active, active)
                time.sleep(0.03)
                with guard:
                    active -= 1

        with bind(store, run_id):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    cancellation.submit(pool, ask, route)
                    for route in ("web:chatgpt", "web:claude")
                ]
                for future in futures:
                    future.result()

        projected = store.projection(run_id)
        self.assertEqual(most_active, 2)
        self.assertEqual(projected["effect_ordinal"], 2)
        self.assertEqual(projected["checkpoint_ordinal"], 0)
        self.assertEqual(
            len([one for one in projected["events"] if one["kind"] == "acknowledged"]), 2
        )
        store.checkpoint(run_id, "all_turns_saved", {"agents": 2})
        store.finish(run_id, {"complete": True})

    def test_begin_effect_requires_a_real_running_run(self) -> None:
        store = SwarmRunStore(self.config)
        accepted, _ = store.accept("accepted", {"objective": "one"})
        with self.assertRaises(HarnessError):
            store.begin_effect(accepted["run_id"], "resource", "digest")
        with self.assertRaises(HarnessError):
            store.begin_effect("missing", "resource", "digest")

    def test_duplicate_stop_is_idempotent_and_appends_one_command_event(self) -> None:
        store, run_id = self._running("stop-once")
        first = store.request_stop("stop-once")
        second = store.request_stop(run_id)
        self.assertEqual(first["status"], "stopping")
        self.assertEqual(second["status"], "stopping")
        events = store.projection(run_id)["events"]
        self.assertEqual(
            len([one for one in events if one["kind"] == "stop_requested"]), 1
        )

    def test_restart_closes_dispatched_effect_as_delivery_unknown_without_resend(self) -> None:
        marker = self.container / "run-id"
        context = multiprocessing.get_context("spawn")
        process = context.Process(
            target=_leave_provider_effect_dispatched,
            args=(str(self.root), str(self.runtime), str(marker)),
        )
        process.start()
        process.join(10)
        self.assertEqual(process.exitcode, 0)
        run_id = marker.read_text(encoding="utf-8")
        store = SwarmRunStore(self.config)
        recovered = store.projection(run_id)
        self.assertEqual(recovered["status"], "delivery_unknown")
        self.assertIn("will not resend automatically", recovered["error"])
        self.assertEqual(
            len([one for one in recovered["events"] if one["kind"] == "provider_dispatched"]),
            1,
        )
        replay, created = store.accept("crash-request", {"objective": "one"})
        self.assertFalse(created)
        self.assertEqual(replay["run_id"], run_id)
        with self.assertRaises(HarnessError):
            store.start(run_id)

    def test_restart_closes_dead_accepted_lease_instead_of_leaving_it_stuck(self) -> None:
        marker = self.container / "accepted-run-id"
        process = multiprocessing.get_context("spawn").Process(
            target=_leave_accepted,
            args=(str(self.root), str(self.runtime), str(marker)),
        )
        process.start()
        process.join(10)
        self.assertEqual(process.exitcode, 0)
        recovered = SwarmRunStore(self.config).get(marker.read_text(encoding="utf-8"))
        self.assertEqual(recovered["status"], "interrupted")
        self.assertFalse(recovered["result"])
        self.assertFalse(recovered["resumable"])
        self.assertEqual(recovered["recovery_action"], "start_over")
        self.assertIn("start a new request", recovered["error"].lower())
        self.assertNotIn("resume", recovered["error"].lower())

    def test_restart_marks_acknowledged_but_uncheckpointed_provider_outcome_unknown(self) -> None:
        marker = self.container / "ack-run-id"
        process = multiprocessing.get_context("spawn").Process(
            target=_leave_acknowledged_without_checkpoint,
            args=(str(self.root), str(self.runtime), str(marker)),
        )
        process.start()
        process.join(10)
        self.assertEqual(process.exitcode, 0)
        store = SwarmRunStore(self.config)
        recovered = store.get(marker.read_text(encoding="utf-8"))
        self.assertEqual(recovered["status"], "outcome_unknown")
        self.assertEqual(recovered["effect_status"], "acknowledged")
        self.assertGreater(recovered["effect_ordinal"], recovered["checkpoint_ordinal"])

    def test_every_payload_sink_is_redacted_before_it_reaches_database(self) -> None:
        secret = "sk-abcdefghijklmnop"
        store = SwarmRunStore(self.config)
        accepted, _ = store.accept("redaction", {"objective": secret})
        run_id = accepted["run_id"]
        store.start(run_id)
        store.event(run_id, "progress", {"detail": f"Bearer {secret}"})
        store.fail(run_id, f"token={secret}")
        for persisted in self.runtime.iterdir():
            if persisted.is_file():
                self.assertNotIn(secret.encode(), persisted.read_bytes(), persisted.name)
        projected = repr(store.projection(run_id))
        self.assertNotIn(secret, projected)
        self.assertIn("[REDACTED]", projected)

    def test_runtime_must_not_be_inside_or_own_the_project(self) -> None:
        os.environ["OUR_HARNESS_SWARM_RUN_DIR"] = str(self.root / ".runtime")
        with self.assertRaises(HarnessError):
            SwarmRunStore(self.config)
        os.environ["OUR_HARNESS_SWARM_RUN_DIR"] = str(self.container)
        with self.assertRaises(HarnessError):
            SwarmRunStore(self.config)

    def test_resource_release_is_fenced_by_process_birth_token(self) -> None:
        store, run_id = self._running("resource-owner")
        with store.resource(run_id, "route", "conversation"):
            with closing(sqlite3.connect(store.database)) as db:
                db.execute("UPDATE resources SET owner_token='replacement-owner'")
                db.commit()
        with closing(sqlite3.connect(store.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM resources").fetchone()[0], 1)

    def test_same_provider_conversation_is_serialized_across_store_instances(self) -> None:
        first, first_run = self._running("first-resource")
        second, second_run = self._running("second-resource")
        entered: list[str] = []
        first_ready = threading.Event()

        def hold_first() -> None:
            with first.resource(first_run, "route", "same-chat"):
                entered.append("first")
                first_ready.set()
                time.sleep(0.2)

        def wait_second() -> None:
            first_ready.wait(2)
            with second.resource(second_run, "route", "same-chat"):
                entered.append("second")

        threads = [threading.Thread(target=hold_first), threading.Thread(target=wait_second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        self.assertEqual(entered, ["first", "second"])

    def test_only_one_board_order_is_admitted_per_project_authority(self) -> None:
        first = SwarmRunStore(self.config)
        accepted, _ = first.accept("board-one", {"kind": "board_order", "board": {}})
        second = SwarmRunStore(self.config)
        with self.assertRaisesRegex(HarnessError, "already running"):
            second.accept("board-two", {"kind": "board_order", "board": {"version": 2}})
        first.fail(accepted["run_id"], "cancelled before start", stopped=True)
        admitted, created = second.accept(
            "board-two", {"kind": "board_order", "board": {"version": 2}}
        )
        self.assertTrue(created)
        self.assertNotEqual(admitted["run_id"], accepted["run_id"])

    def test_board_order_and_mutation_are_global_across_project_authorities(self) -> None:
        first = SwarmRunStore(self.config)
        accepted, _ = first.accept(
            "global-board-one", {"kind": "board_order", "board": {"version": 1}}
        )
        first.start(accepted["run_id"])
        other_root = self.container / "other-project"
        (other_root / ".harness").mkdir(parents=True)
        second = SwarmRunStore(LoadedConfig(
            copy.deepcopy(DEFAULT_CONFIG), other_root, [], {}
        ))
        with self.assertRaisesRegex(HarnessError, "global Swarm board"):
            second.accept(
                "global-board-two", {"kind": "board_order", "board": {"version": 2}}
            )
        with self.assertRaisesRegex(HarnessError, "global Swarm board"):
            with second.board_mutation():
                self.fail("a mutation must not enter while a board order is active")
        first.request_stop(accepted["run_id"])
        first.fail(accepted["run_id"], "stopped", stopped=True)
        with second.board_mutation() as generation:
            self.assertGreater(generation, 0)

    def test_named_board_save_open_and_forget_share_the_global_run_fence(self) -> None:
        board_file = self.container / "settings" / "swarm.json"
        saved_boards = self.container / "settings" / "saved"
        with mock.patch.object(swarm, "where_it_lives", return_value=board_file), \
                mock.patch.object(
                    swarm, "where_the_kept_ones_live", return_value=saved_boards
                ):
            swarm.save({"agents": [{"name": "One"}]}, self.config)
            swarm.keep_this_board("Known", self.config)
            store = SwarmRunStore(self.config)
            accepted, _ = store.accept(
                "named-board-fence", {"kind": "board_order", "board": {}}
            )
            store.start(accepted["run_id"])
            for mutation in (
                lambda: swarm.keep_this_board("Another", self.config),
                lambda: swarm.open_this_board("Known", self.config),
                lambda: swarm.forget_this_board("Known", self.config),
            ):
                with self.subTest(mutation=mutation), self.assertRaisesRegex(
                    HarnessError, "global Swarm board"
                ):
                    mutation()
            self.assertEqual([one["name"] for one in swarm.every_kept_board()], ["Known"])
            store.fail(accepted["run_id"], "done", stopped=True)

    def test_exact_stop_from_another_process_fences_the_owner(self) -> None:
        marker = self.container / "cross-process-board-run"
        process = multiprocessing.get_context("spawn").Process(
            target=_own_board_until_stopped,
            args=(str(self.root), str(self.runtime), str(marker)),
        )
        process.start()
        limit = time.time() + 5
        while time.time() < limit and not marker.exists():
            time.sleep(0.02)
        self.assertTrue(marker.exists(), "the board owner did not publish its run ID")
        run_id = marker.read_text(encoding="utf-8")
        controller = SwarmRunStore(self.config)
        stopped = controller.request_stop(run_id)
        self.assertEqual(stopped["status"], "stopping")
        process.join(10)
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(controller.get(run_id)["status"], "stopped")

    def test_busy_local_runner_refuses_before_it_creates_a_durable_command(self) -> None:
        store = SwarmRunStore(self.config)
        running = swarm.Running(store)
        running._doing = swarm.Doing(going=True)  # deterministic occupied-process fixture
        with self.assertRaisesRegex(HarnessError, "already going"):
            running.start(self.config, self._standing(), "must-not-be-accepted")
        with closing(sqlite3.connect(store.database)) as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM runs WHERE request_id='must-not-be-accepted'").fetchone()[0],
                0,
            )

    def test_worker_thread_start_failure_terminally_closes_durable_run(self) -> None:
        store = SwarmRunStore(self.config)
        running = swarm.Running(store)
        with mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("no thread")):
            with self.assertRaisesRegex(RuntimeError, "no thread"):
                running.start(self.config, self._standing(), "thread-failed")
        durable = store.get("thread-failed")
        self.assertEqual(durable["status"], "failed")
        self.assertFalse(running.busy)

    def test_large_history_projection_returns_only_the_requested_delta(self) -> None:
        store, run_id = self._running("long-history")
        for number in range(250):
            store.event(run_id, "board_progress", {"number": number})
        latest = store.latest_event(run_id, "board_progress")
        self.assertEqual(latest["payload"], {"number": 249})
        after = latest["seq"] - 2
        delta = store.projection(run_id, after)
        self.assertEqual(len(delta["events"]), 2)
        self.assertEqual(delta["cursor"], latest["seq"])
        forged = store.projection(run_id, 10**9)
        self.assertEqual(forged["events"], [])
        self.assertEqual(forged["cursor"], latest["seq"])

    def test_projection_pages_are_row_and_byte_bounded_and_drain_without_gaps(self) -> None:
        store, run_id = self._running("bounded-history")
        for number in range(450):
            store.event(run_id, "progress", {"number": number, "text": "z" * 2000})
        cursor = 0
        seen: list[int] = []
        pages = 0
        while True:
            page = store.projection(run_id, cursor)
            pages += 1
            self.assertLessEqual(len(page["events"]), 200)
            self.assertLessEqual(
                len(json.dumps(page["events"], ensure_ascii=False).encode("utf-8")),
                260_000,
            )
            seen.extend(one["seq"] for one in page["events"])
            self.assertGreaterEqual(page["next_cursor"], cursor)
            cursor = page["next_cursor"]
            if not page["has_more"]:
                break
            self.assertLess(pages, 20)
        self.assertEqual(seen, list(range(1, 453)))

    def test_post_provider_mutation_and_exact_stop_are_linearized(self) -> None:
        store, run_id = self._running("stop-linearization")
        entered = threading.Event()
        release = threading.Event()
        stopped = threading.Event()
        order: list[str] = []

        def mutate() -> None:
            with store.post_provider_mutation(run_id):
                entered.set()
                self.assertTrue(release.wait(5))
                order.append("mutation")

        def stop() -> None:
            store.request_stop(run_id)
            order.append("stop accepted")
            stopped.set()

        mutation = threading.Thread(target=mutate)
        mutation.start()
        self.assertTrue(entered.wait(5))
        stopper = threading.Thread(target=stop)
        stopper.start()
        self.assertFalse(stopped.wait(0.1), "Stop was accepted inside an active mutation")
        release.set()
        mutation.join(5)
        stopper.join(5)
        self.assertEqual(order, ["mutation", "stop accepted"])
        with self.assertRaisesRegex(HarnessError, "Stop was accepted"):
            with store.post_provider_mutation(run_id):
                self.fail("a post-Stop mutation entered")

    def test_worker_finalizer_terminalizes_a_late_stopping_run(self) -> None:
        store = SwarmRunStore(self.config)
        running = swarm.Running(store)

        def stop_after_last_worker_check(_config, _standing, doing) -> None:
            store.request_stop(doing.run_id)

        with mock.patch.object(running, "_do_it", side_effect=stop_after_last_worker_check):
            started = running.start(self.config, self._standing(), "late-stop-finalizer")
            running.wait(5)
        self.assertEqual(store.get(started["run_id"])["status"], "stopped")

    def test_legacy_global_request_schema_migrates_columns_events_and_foreign_key(self) -> None:
        self.runtime.mkdir(parents=True)
        database = self.runtime / "runs.sqlite3"
        with closing(sqlite3.connect(database)) as db:
            db.executescript("""
            CREATE TABLE runs(
              run_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
              snapshot_json TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL,
              status TEXT NOT NULL, owner_pid INTEGER NOT NULL, owner_token TEXT NOT NULL,
              stop_requested INTEGER NOT NULL DEFAULT 0,
              effect_status TEXT NOT NULL DEFAULT '', effect_id TEXT NOT NULL DEFAULT '',
              effect_ordinal INTEGER NOT NULL DEFAULT 0, effect_digest TEXT NOT NULL DEFAULT '',
              result_json TEXT, error TEXT NOT NULL DEFAULT '',
              created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL
            );
            CREATE TABLE events(
              run_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,
              payload_json TEXT NOT NULL, at_ms INTEGER NOT NULL,
              PRIMARY KEY(run_id,seq), FOREIGN KEY(run_id) REFERENCES runs(run_id)
            );
            CREATE TABLE resources(
              resource_key TEXT PRIMARY KEY, run_id TEXT NOT NULL,
              owner_pid INTEGER NOT NULL, owner_token TEXT NOT NULL, acquired_ms INTEGER NOT NULL
            );
            """)
            db.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "legacy-run", "legacy-request", '{"objective":"kept"}', "old-digest",
                    "complete", 1, "birth", 0, "", "", 0, "", '{"done":true}',
                    "", 10, 20,
                ),
            )
            db.execute(
                "INSERT INTO events VALUES(?,?,?,?,?)",
                ("legacy-run", 1, "complete", '{"result_saved":true}', 20),
            )
            db.commit()

        store = SwarmRunStore(self.config)
        migrated = store.projection("legacy-request")
        self.assertEqual(migrated["run_id"], "legacy-run")
        self.assertEqual(migrated["snapshot"], {"objective": "kept"})
        self.assertEqual(migrated["status"], "complete")
        self.assertEqual(migrated["result"], {"done": True})
        self.assertEqual(migrated["events"][0]["kind"], "complete")
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            indexes = [
                [column[2] for column in db.execute(f'PRAGMA index_info("{row[1]}")')]
                for row in db.execute("PRAGMA index_list(runs)") if row[2]
            ]
        self.assertIn(["project_authority", "request_id"], indexes)

    def test_corrupt_json_fails_closed_without_partially_rewriting_the_journal(self) -> None:
        store = SwarmRunStore(self.config)
        accepted, _ = store.accept("corrupt", {"objective": "valid"})
        with closing(sqlite3.connect(store.database)) as db:
            db.execute(
                "UPDATE runs SET snapshot_json='not-json' WHERE run_id=?",
                (accepted["run_id"],),
            )
            db.commit()
        with self.assertRaisesRegex(HarnessError, "journal (?:is corrupt|failed keyed integrity)"):
            SwarmRunStore(self.config)
        with closing(sqlite3.connect(store.database)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT snapshot_json FROM runs WHERE run_id=?", (accepted["run_id"],)
                ).fetchone()[0],
                "not-json",
            )

    def test_blind_sqlite_rewrite_is_quarantined_without_being_blessed(self) -> None:
        store, run_id = self._running("blind-rewrite")
        with closing(sqlite3.connect(store.database)) as db:
            db.execute(
                "UPDATE runs SET status='complete',result_json='{\"forged\":true}' "
                "WHERE run_id=?",
                (run_id,),
            )
            db.commit()
        with self.assertRaisesRegex(HarnessError, "failed keyed integrity"):
            store.get(run_id)
        with self.assertRaisesRegex(HarnessError, "failed keyed integrity"):
            SwarmRunStore(self.config)
        self.assertTrue(any((self.runtime / "quarantine").glob("*.json")))


if __name__ == "__main__":
    unittest.main()
