from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
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
from our_harness.models import HarnessError, ProviderOutcomeUnknown
from our_harness import cancellation, chat, swarm, swarm_work
from our_harness.swarm_runs import (
    SwarmRunStore, bind, global_board_change_pause_reason, provider_effect,
    _provider_resource_conversation_key,
)


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


def _leave_board_owner_dead(root: str, runtime: str, marker: str) -> None:
    os.environ["OUR_HARNESS_SWARM_RUN_DIR"] = runtime
    config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), Path(root), [], {})
    store = SwarmRunStore(config)
    accepted, _ = store.accept(
        "dead-board-owner", {"kind": "board_order", "board": {"version": 1}}
    )
    store.start(accepted["run_id"])
    Path(marker).write_text(accepted["run_id"], encoding="utf-8")


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


def _hold_provider_capacity(
    root: str, runtime: str, ready_marker: str, release_marker: str,
) -> None:
    os.environ["OUR_HARNESS_SWARM_RUN_DIR"] = runtime
    data = copy.deepcopy(DEFAULT_CONFIG)
    data["providers"] = {"limited": {"max_concurrency": 1}}
    config = LoadedConfig(data, Path(root), [], {})
    with provider_effect(config, "limited", "child-chat", "child-digest"):
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
        self.prior_authority_override = os.environ.get("OUR_HARNESS_PIPELINE_RUN_DIR")
        os.environ["OUR_HARNESS_SWARM_RUN_DIR"] = str(self.runtime)
        os.environ["OUR_HARNESS_PIPELINE_RUN_DIR"] = str(
            self.container / "trusted-authority-control"
        )
        self.addCleanup(self._restore_override)
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})

    def _restore_override(self) -> None:
        if self.prior_override is None:
            os.environ.pop("OUR_HARNESS_SWARM_RUN_DIR", None)
        else:
            os.environ["OUR_HARNESS_SWARM_RUN_DIR"] = self.prior_override
        if self.prior_authority_override is None:
            os.environ.pop("OUR_HARNESS_PIPELINE_RUN_DIR", None)
        else:
            os.environ["OUR_HARNESS_PIPELINE_RUN_DIR"] = self.prior_authority_override

    def test_communication_journal_is_durable_without_granting_project_authority(self) -> None:
        descriptor = self.root / ".harness" / "project-authority.json"
        store = SwarmRunStore.for_communication(self.config)
        self.assertTrue(store.authority.startswith("communication-"))
        self.assertFalse(descriptor.exists())
        accepted, created = store.accept("plain-chat", {"kind": "chat"})
        self.assertTrue(created)
        store.start(accepted["run_id"])
        store.finish(accepted["run_id"], {"answer": "hello"})
        reopened = SwarmRunStore.for_communication(self.config)
        self.assertEqual(reopened.get("plain-chat")["result"]["answer"], "hello")
        self.assertFalse(descriptor.exists())

    def _running(self, request: str = "request", snapshot: dict | None = None):
        store = SwarmRunStore(self.config)
        accepted, _ = store.accept(request, snapshot or {"objective": "test"})
        return store, store.start(accepted["run_id"])["run_id"]

    def test_active_runs_returns_every_verified_active_run(self) -> None:
        store, older_run = self._running("older-active", {"kind": "work", "name": "older"})
        accepted, _ = store.accept("newer-active", {"kind": "chat", "name": "newer"})
        newer_run = store.start(accepted["run_id"])["run_id"]

        active = store.active_runs()

        self.assertEqual({one["run_id"] for one in active}, {older_run, newer_run})
        self.assertTrue(all(one["status"] == "running" for one in active))

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

    def test_execution_and_communication_journals_share_the_exact_chat_lease(self) -> None:
        execution = SwarmRunStore(self.config)
        communication = SwarmRunStore.for_communication(self.config)
        owner, _ = execution.accept("execution-chat-owner", {"kind": "work"})
        same, _ = communication.accept("communication-same-chat", {"kind": "chat"})
        other, _ = communication.accept("communication-other-chat", {"kind": "chat"})

        with execution.conversation_turn(owner["run_id"], "chat-one", timeout=0):
            with self.assertRaisesRegex(HarnessError, "already working"):
                with communication.conversation_turn(
                    same["run_id"], "chat-one", timeout=0,
                ):
                    self.fail("the communication journal entered an execution-owned chat")
            with communication.conversation_turn(
                other["run_id"], "chat-two", timeout=0,
            ):
                pass

    def _capacity_config(self, maximum: int) -> LoadedConfig:
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["providers"] = {"limited": {"max_concurrency": maximum}}
        return LoadedConfig(data, self.root, [], {})

    def test_provider_profile_capacity_serializes_distinct_chats_at_one(self) -> None:
        config = self._capacity_config(1)
        first_entered = threading.Event()
        second_dispatched = threading.Event()
        second_entered = threading.Event()
        release_first = threading.Event()
        release_second = threading.Event()
        failures: list[BaseException] = []

        def call(
            conversation: str, entered: threading.Event, release: threading.Event,
            before_dispatch=None,
        ) -> None:
            try:
                with provider_effect(
                    config, "limited", conversation, "digest-" + conversation,
                    before_dispatch=before_dispatch,
                ):
                    entered.set()
                    self.assertTrue(release.wait(5))
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        first = threading.Thread(
            target=call, args=("chat-one", first_entered, release_first),
        )
        second = threading.Thread(
            target=call,
            args=("chat-two", second_entered, release_second, second_dispatched.set),
        )
        first.start()
        self.assertTrue(first_entered.wait(5))
        second.start()
        self.assertFalse(second_dispatched.wait(0.15))
        self.assertFalse(second_entered.is_set())
        release_first.set()
        self.assertTrue(second_dispatched.wait(5))
        self.assertTrue(second_entered.wait(5))
        release_second.set()
        first.join(5)
        second.join(5)
        self.assertFalse(failures)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())

    def test_provider_profile_capacity_allows_declared_parallelism_only(self) -> None:
        config = self._capacity_config(2)
        entered = [threading.Event() for _ in range(3)]
        release = [threading.Event() for _ in range(3)]
        failures: list[BaseException] = []

        def call(index: int) -> None:
            try:
                with provider_effect(
                    config, "limited", f"chat-{index}", f"digest-{index}",
                ):
                    entered[index].set()
                    self.assertTrue(release[index].wait(5))
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        threads = [threading.Thread(target=call, args=(index,)) for index in range(3)]
        threads[0].start()
        threads[1].start()
        self.assertTrue(entered[0].wait(5))
        self.assertTrue(entered[1].wait(5))
        threads[2].start()
        self.assertFalse(entered[2].wait(0.15))
        release[0].set()
        self.assertTrue(entered[2].wait(5))
        release[1].set()
        release[2].set()
        for thread in threads:
            thread.join(5)
        self.assertFalse(failures)
        self.assertTrue(all(not thread.is_alive() for thread in threads))

    def test_provider_profile_capacity_is_shared_across_processes(self) -> None:
        config = self._capacity_config(1)
        ready = self.container / "provider-capacity-ready"
        release = self.container / "provider-capacity-release"
        process = multiprocessing.Process(
            target=_hold_provider_capacity,
            args=(str(self.root), str(self.runtime), str(ready), str(release)),
        )
        process.start()
        entered = threading.Event()
        failure: list[BaseException] = []

        def contender() -> None:
            try:
                with provider_effect(
                    config, "limited", "parent-chat", "parent-digest",
                ):
                    entered.set()
            except BaseException as exc:  # pragma: no cover - asserted below
                failure.append(exc)

        thread = threading.Thread(target=contender)
        try:
            limit = time.time() + 5
            while time.time() < limit and not ready.exists():
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "the child did not claim provider capacity")
            thread.start()
            self.assertFalse(entered.wait(0.15))
            release.write_text("release", encoding="utf-8")
            self.assertTrue(entered.wait(5))
            thread.join(5)
            process.join(5)
        finally:
            release.write_text("release", encoding="utf-8")
            if thread.is_alive():
                thread.join(5)
            if process.is_alive():
                process.terminate()
                process.join(2)
        self.assertFalse(failure)
        self.assertFalse(thread.is_alive())
        self.assertEqual(process.exitcode, 0)

    def test_queued_provider_capacity_wait_is_cancellable_before_dispatch(self) -> None:
        config = self._capacity_config(1)
        owner_entered = threading.Event()
        release_owner = threading.Event()
        queued_dispatched = threading.Event()
        queued_body = threading.Event()
        queued_cancelled = threading.Event()
        token = cancellation.Cancellation()

        def owner() -> None:
            with provider_effect(config, "limited", "owner", "owner-digest"):
                owner_entered.set()
                self.assertTrue(release_owner.wait(5))

        def queued() -> None:
            try:
                with cancellation.use(token):
                    with provider_effect(
                        config, "limited", "queued", "queued-digest",
                        before_dispatch=queued_dispatched.set,
                    ):
                        queued_body.set()
            except cancellation.ChatCancelled:
                queued_cancelled.set()

        first = threading.Thread(target=owner)
        second = threading.Thread(target=queued)
        first.start()
        self.assertTrue(owner_entered.wait(5))
        second.start()
        self.assertFalse(queued_dispatched.wait(0.15))
        token.cancel()
        self.assertTrue(queued_cancelled.wait(5))
        self.assertFalse(queued_dispatched.is_set())
        self.assertFalse(queued_body.is_set())
        release_owner.set()
        first.join(5)
        second.join(5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())

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

    def test_delivery_unknown_effect_blocks_only_its_resource_and_unsafe_completion(self) -> None:
        store, run_id = self._running("unknown-effect")
        effect = store.begin_effect(run_id, "resource", "digest")
        store.finish_effect(run_id, effect, False)
        self.assertEqual(store.get(run_id)["status"], "running")
        self.assertEqual(store.get(run_id)["effect_status"], "delivery_unknown")
        with self.assertRaises(HarnessError):
            store.checkpoint(run_id, "unsafe", {})
        with self.assertRaises(HarnessError):
            store.begin_effect(run_id, "resource", "digest-two")
        healthy = store.begin_effect(run_id, "different-resource", "digest-three")
        store.finish_effect(run_id, healthy, True)
        with self.assertRaises(HarnessError):
            store.finish(run_id, {"complete": True})
        store.fail(run_id, "provider rejected the schema")
        projected = store.projection(run_id)
        self.assertEqual(projected["error"], "provider rejected the schema")
        self.assertEqual(projected["status"], "delivery_unknown")

    def test_one_uncertain_parallel_effect_preserves_and_completes_healthy_work(self) -> None:
        store, run_id = self._running("parallel-uncertain")
        failed = store.begin_effect(run_id, "resource-a", "digest-a")
        succeeded = store.begin_effect(run_id, "resource-b", "digest-b")
        store.finish_effect(run_id, failed, False)
        store.finish_effect(run_id, succeeded, True)
        projected = store.get(run_id)
        self.assertEqual(projected["status"], "running")
        self.assertEqual(projected["effect_status"], "delivery_unknown")
        with self.assertRaises(HarnessError):
            store.checkpoint(run_id, "unsafe", {})
        result = {
            "answer": "healthy peer answer",
            "provider_failures": [{"route": "resource-a", "outcome_unknown": True}],
        }
        store.checkpoint(run_id, "degraded_result_saved", result)
        store.finish(run_id, result)
        self.assertEqual(store.get(run_id)["status"], "complete")
        next_store, next_run = self._running("parallel-uncertain-restart")
        with self.assertRaisesRegex(HarnessError, "uncertain prior delivery"):
            next_store.begin_effect(next_run, "resource-a", "must-not-resend")
        independent = next_store.begin_effect(
            next_run, "resource-c", "different-provider-conversation"
        )
        next_store.finish_effect(next_run, independent, True)
        next_store.checkpoint(next_run, "independent_saved", {"ok": True})
        next_store.finish(next_run, {"ok": True})
        other_root = self.container / "other-uncertain-project"
        (other_root / ".harness").mkdir(parents=True)
        other = SwarmRunStore(LoadedConfig(
            copy.deepcopy(DEFAULT_CONFIG), other_root, [], {}
        ))
        accepted, _created = other.accept(
            "cross-authority-uncertain", {"objective": "must not replay"}
        )
        other.start(accepted["run_id"])
        with self.assertRaisesRegex(HarnessError, "uncertain prior delivery"):
            other.begin_effect(
                accepted["run_id"], "resource-a", "cross-authority-resend"
            )
        other.fail(accepted["run_id"], "did not dispatch", stopped=True)

    def test_explicit_provider_failure_is_known_and_does_not_poison_the_run(self) -> None:
        store, run_id = self._running("known-provider-failure")
        with bind(store, run_id):
            with self.assertRaisesRegex(HarnessError, "provider refused"):
                with provider_effect(self.config, "claude", "pair-chat", "digest"):
                    raise HarnessError("provider refused the request")

        projected = store.projection(run_id)
        self.assertEqual(projected["status"], "running")
        self.assertEqual(projected["effect_status"], "acknowledged")
        self.assertIn("acknowledged", [one["kind"] for one in projected["events"]])

    def test_only_typed_provider_ambiguity_becomes_delivery_unknown(self) -> None:
        store, run_id = self._running("typed-provider-unknown")
        with bind(store, run_id):
            with self.assertRaises(ProviderOutcomeUnknown):
                with provider_effect(self.config, "web:gemini", "pair-chat", "digest"):
                    raise ProviderOutcomeUnknown("renderer vanished after Send")

        projected = store.get(run_id)
        self.assertEqual(projected["status"], "running")
        self.assertEqual(projected["effect_status"], "delivery_unknown")

    def test_stateless_uncertainty_blocks_exact_replay_but_not_a_new_turn(self) -> None:
        store, run_id = self._running("stateless-provider-unknown")
        with bind(store, run_id):
            with self.assertRaises(ProviderOutcomeUnknown):
                with provider_effect(self.config, "codex", "pair-chat", "digest-one"):
                    raise ProviderOutcomeUnknown("process vanished after dispatch")
        store.fail(run_id, "uncertain Codex result")

        next_store, next_run = self._running("stateless-provider-next-turn")
        with bind(next_store, next_run):
            with self.assertRaisesRegex(HarnessError, "uncertain prior delivery"):
                with provider_effect(self.config, "codex", "pair-chat", "digest-one"):
                    self.fail("the exact uncertain request must not be replayed")
            with provider_effect(self.config, "codex", "pair-chat", "digest-two"):
                pass
        next_store.checkpoint(next_run, "new_turn_saved", {"answer": "new"})
        next_store.finish(next_run, {"answer": "new"})

    def test_stateless_same_route_requests_do_not_share_a_conversation_lease(self) -> None:
        self.assertNotEqual(
            _provider_resource_conversation_key("codex", "pair-chat", "digest-one"),
            _provider_resource_conversation_key("codex", "pair-chat", "digest-two"),
        )
        self.assertEqual(
            _provider_resource_conversation_key(
                "web:chatgpt", "pair-chat", "digest-one",
            ),
            _provider_resource_conversation_key(
                "web:chatgpt", "pair-chat", "digest-two",
            ),
        )

        self.config.data["providers"] = {
            "codex": {
                "kind": "codex-cli", "model": "codex", "max_concurrency": 2,
            },
        }
        store, run_id = self._running("same-stateless-route-parallel")
        original_resource = store.resource

        @contextmanager
        def short_resource(one_run, route, conversation_key, timeout=180.0):
            with original_resource(one_run, route, conversation_key, timeout=0.3) as key:
                yield key

        both_entered = threading.Barrier(2)

        def ask(digest: str) -> None:
            with provider_effect(self.config, "codex", "pair-chat", digest):
                both_entered.wait(timeout=2)

        with bind(store, run_id), mock.patch.object(
            store, "resource", side_effect=short_resource,
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    cancellation.submit(pool, ask, digest)
                    for digest in ("digest-one", "digest-two")
                ]
                for future in futures:
                    future.result(timeout=5)
        store.checkpoint(run_id, "both_saved", {"answers": 2})
        store.finish(run_id, {"answers": 2})

    def test_web_uncertainty_still_fences_the_whole_remote_conversation(self) -> None:
        store, run_id = self._running("web-provider-unknown")
        with bind(store, run_id):
            with self.assertRaises(ProviderOutcomeUnknown):
                with provider_effect(self.config, "web:gemini", "pair-chat", "digest-one"):
                    raise ProviderOutcomeUnknown("renderer vanished after Send")
        store.fail(run_id, "uncertain web-chat result")

        next_store, next_run = self._running("web-provider-next-turn")
        with bind(next_store, next_run):
            with self.assertRaisesRegex(HarnessError, "uncertain prior delivery"):
                with provider_effect(self.config, "web:gemini", "pair-chat", "digest-two"):
                    self.fail("a stateful provider conversation must remain fenced")
        next_store.fail(next_run, "web conversation needs reconciliation")

    def test_restart_repairs_pre_fix_web_fence_with_affirmative_marker_receipt(self) -> None:
        store, run_id = self._running("marked-web-receipt")
        effect = store.begin_effect(run_id, "web-conversation-resource", "digest-one")
        store.finish_effect(run_id, effect, False)
        store.fail(
            run_id,
            "web:chatgpt-abcdef has an unreconciled provider turn: ChatGPT may "
            "have accepted this message, but Nexus could not match its marked turn "
            "and reply. [relay diagnostic: "
            "submission_state=outcome_unknown, reply_seen=True, "
            "answer_characters=17, stop_visible=True, "
            "stale_stop_at_submission=False, stop_cleared_after_submission=True, "
            "stable_polls=181, polls=182, marker_found=True, reply_count=1, "
            "user_count=1, document_visibility=visible, "
            "page_url=https://chatgpt.com/c/example]",
        )

        reopened = SwarmRunStore(self.config)
        repaired = reopened.projection(run_id)

        self.assertEqual(repaired["status"], "failed")
        self.assertEqual(repaired["effect_status"], "acknowledged")
        self.assertIn(
            "provider_late_receipt_reconciled",
            [one["kind"] for one in repaired["events"]],
        )
        next_store, next_run = self._running("marked-web-receipt-next-turn")
        retried = next_store.begin_effect(
            next_run, "web-conversation-resource", "digest-two",
        )
        next_store.finish_effect(next_run, retried, True)

    def test_restart_keeps_web_fence_when_marker_receipt_is_incomplete(self) -> None:
        store, run_id = self._running("incomplete-web-receipt")
        effect = store.begin_effect(run_id, "still-uncertain-resource", "digest-one")
        store.finish_effect(run_id, effect, False)
        store.fail(
            run_id,
            "web:chatgpt-abcdef has an unreconciled provider turn: ChatGPT may "
            "have accepted this message, but Nexus could not match its marked turn "
            "and reply. [relay diagnostic: "
            "submission_state=outcome_unknown, reply_seen=True, "
            "answer_characters=17, stop_cleared_after_submission=True, "
            "marker_found=False, reply_count=1, user_count=1]",
        )

        reopened = SwarmRunStore(self.config)
        self.assertEqual(reopened.get(run_id)["status"], "delivery_unknown")
        next_store, next_run = self._running("incomplete-web-receipt-next-turn")
        with self.assertRaisesRegex(HarnessError, "uncertain prior delivery"):
            next_store.begin_effect(
                next_run, "still-uncertain-resource", "must-not-resend",
            )
        next_store.fail(next_run, "still requires reconciliation")

    def test_restart_never_applies_legacy_receipt_to_a_multi_effect_run(self) -> None:
        store, run_id = self._running("multi-effect-web-receipt")
        accepted = store.begin_effect(run_id, "accepted-resource", "digest-one")
        store.finish_effect(run_id, accepted, True)
        uncertain = store.begin_effect(run_id, "uncertain-resource", "digest-two")
        store.finish_effect(run_id, uncertain, False)
        store.fail(
            run_id,
            "web:chatgpt-abcdef has an unreconciled provider turn: ChatGPT may "
            "have accepted this message, but Nexus could not match its marked turn "
            "and reply. [relay diagnostic: "
            "submission_state=outcome_unknown, reply_seen=True, "
            "answer_characters=17, stop_cleared_after_submission=True, "
            "marker_found=True, reply_count=1, user_count=1]",
        )

        reopened = SwarmRunStore(self.config)
        self.assertEqual(reopened.get(run_id)["status"], "delivery_unknown")
        next_store, next_run = self._running("multi-effect-web-receipt-next-turn")
        with self.assertRaisesRegex(HarnessError, "uncertain prior delivery"):
            next_store.begin_effect(next_run, "uncertain-resource", "must-not-resend")
        next_store.fail(next_run, "multi-effect run requires explicit reconciliation")

    def test_bound_collaboration_saves_healthy_answer_with_one_unknown_peer(self) -> None:
        store, run_id = self._running("bound-degraded-collaboration")
        board = {
            "agents": [
                {"id": "lead", "name": "Lead", "who": "web:lead-route", "ready": True},
                {"id": "peer", "name": "Peer", "who": "web:peer-route", "ready": True},
            ],
            "talks_to": [{"one": "lead", "other": "peer"}],
            "projects": [], "works_on": [],
        }
        calls: list[str] = []

        def answer(_config, route, _text, **_kwargs):
            calls.append(route)
            if route == "web:peer-route":
                # Exercise the real effect wrapper so the bound run owns one
                # unresolved provider effect while the lead succeeds.
                with provider_effect(self.config, route, "pair-chat", "peer-digest"):
                    raise ProviderOutcomeUnknown("peer browser vanished after Send")
            with provider_effect(self.config, route, "pair-chat", "lead-digest"):
                return {"text": "healthy lead answer", "milliseconds": 1, "model": route}

        with bind(store, run_id), mock.patch.object(chat, "ask_once", side_effect=answer):
            result = swarm_work.collaborate(
                self.config, board, "lead", "Work together", round_limit=None,
            )
        store.checkpoint(run_id, "degraded_answer_saved", result)
        store.finish(run_id, result)

        self.assertCountEqual(calls, ["web:lead-route", "web:peer-route"])
        self.assertEqual(result["answer"]["text"], "healthy lead answer")
        self.assertTrue(result["provider_failures"])
        self.assertIs(result["provider_failures"][0]["outcome_unknown"], True)
        self.assertEqual(store.get(run_id)["status"], "complete")

    def test_parallel_workers_journal_overlapping_provider_effects_before_final_checkpoint(self) -> None:
        store, run_id = self._running("parallel-provider-effects")
        active = 0
        most_active = 0
        guard = threading.Lock()
        both_effects_entered = threading.Barrier(2)

        def ask(route: str) -> None:
            nonlocal active, most_active
            with provider_effect(self.config, route, "pair-chat", f"digest-{route}"):
                with guard:
                    active += 1
                    most_active = max(most_active, active)
                both_effects_entered.wait(timeout=10)
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

    def test_acknowledged_reply_with_local_protocol_failure_is_not_outcome_unknown(self) -> None:
        store, run_id = self._running("known-invalid-reply")
        effect = store.begin_effect(run_id, "web:claude", "reply-digest")
        store.finish_effect(run_id, effect, True)

        store.fail(
            run_id,
            "Claude returned an invalid structured collaboration result",
            acknowledged_outcome=True,
        )

        projected = store.projection(run_id)
        self.assertEqual(projected["status"], "failed")
        self.assertEqual(
            projected["checkpoint_ordinal"], projected["effect_ordinal"]
        )
        self.assertIn(
            "provider_reply_received_before_failure",
            [one["kind"] for one in projected["events"]],
        )

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

    def test_exact_run_id_wins_over_a_colliding_caller_request_alias(self) -> None:
        store = SwarmRunStore(self.config)
        first, _created = store.accept("ordinary-request", {"objective": "first"})
        first_id = first["run_id"]
        store.start(first_id)
        alias, alias_created = store.accept(first_id, {"objective": "alias"})
        self.assertTrue(alias_created)
        self.assertNotEqual(alias["run_id"], first_id)

        self.assertEqual(store.get(first_id)["snapshot"]["objective"], "first")
        stopped = store.request_stop(first_id)
        self.assertEqual(stopped["run_id"], first_id)
        self.assertEqual(stopped["status"], "stopping")
        self.assertEqual(store.get(alias["run_id"])["status"], "accepted")

    def test_accepted_stop_fences_orchestrated_success_transcript_commit(self) -> None:
        store, run_id = self._running("fenced-success-transcript")
        with bind(store, run_id):
            store.request_stop(run_id)
            with self.assertRaisesRegex(HarnessError, "Stop was accepted"):
                chat.keep_exchange(
                    self.config, "claude", "question", "late answer",
                    filed_as="exact-chat",
                )
        self.assertEqual(chat.read_it(self.config, "claude", "exact-chat"), [])

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

    def test_long_failure_keeps_redacted_head_tail_and_digest_marker(self) -> None:
        store, run_id = self._running("long-cause")
        secret = "swarm-bearer-secret-0123456789"
        cause = "SWARM-CAUSE-HEAD " + ("z" * 70_000) + f" Bearer {secret} SWARM-CAUSE-TAIL"
        store.fail(run_id, cause)
        saved = store.get(run_id)["error"]
        self.assertNotIn(secret, saved)
        self.assertIn("[REDACTED]", saved)
        self.assertIn("NEXUS_REDACTED_CAUSE_BOUNDARY", saved)
        self.assertIn("SWARM-CAUSE-HEAD", saved)
        self.assertIn("SWARM-CAUSE-TAIL", saved)
        self.assertLessEqual(len(saved), 65_536)

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

    def test_lightweight_board_lease_recovers_dead_owner_but_not_live_owner(self) -> None:
        dead_marker = self.container / "dead-board-owner"
        dead = multiprocessing.Process(
            target=_leave_board_owner_dead,
            args=(str(self.root), str(self.runtime), str(dead_marker)),
        )
        dead.start()
        dead.join(10)
        self.assertEqual(dead.exitcode, 0)
        self.assertTrue(dead_marker.exists())
        self.assertEqual(global_board_change_pause_reason(self.config), "")

        board_file = self.container / "settings" / "swarm.json"
        with mock.patch.object(swarm, "where_it_lives", return_value=board_file):
            saved = swarm.save({"agents": [{"name": "Recovered"}]}, self.config)
        self.assertEqual(saved.agents[0].name, "Recovered")

        live_marker = self.container / "live-board-owner"
        live = multiprocessing.Process(
            target=_own_board_until_stopped,
            args=(str(self.root), str(self.runtime), str(live_marker)),
        )
        live.start()
        try:
            limit = time.time() + 5
            while time.time() < limit and not live_marker.exists():
                time.sleep(0.02)
            self.assertTrue(live_marker.exists(), "the live board owner did not start")
            self.assertIn("cannot be changed", global_board_change_pause_reason(self.config))
            with mock.patch.object(swarm, "where_it_lives", return_value=board_file):
                with self.assertRaisesRegex(HarnessError, "global Swarm board"):
                    swarm.save({"agents": [{"name": "Must stay blocked"}]}, self.config)
            SwarmRunStore(self.config).request_stop(live_marker.read_text(encoding="utf-8"))
            live.join(5)
        finally:
            if live.is_alive():
                live.terminate()
                live.join(2)
        self.assertEqual(live.exitcode, 0)

    def test_live_board_run_allows_saved_library_crud_but_not_open_or_live_save(self) -> None:
        board_file = self.container / "settings" / "swarm.json"
        saved_boards = self.container / "settings" / "saved"
        path_patches = (
            mock.patch.object(swarm, "where_it_lives", return_value=board_file),
            mock.patch.object(swarm, "where_the_kept_ones_live", return_value=saved_boards),
        )
        with path_patches[0], path_patches[1]:
            initial = swarm.save({
                "agents": [{"name": "Topology stays"}],
                "active_saved_board": "Named",
            }, self.config)
            swarm.keep_this_board("Named", self.config)
            portable = swarm.export_kept_board("Named")

            marker = self.container / "library-live-owner"
            live = multiprocessing.Process(
                target=_own_board_until_stopped,
                args=(str(self.root), str(self.runtime), str(marker)),
            )
            live.start()
            try:
                limit = time.time() + 5
                while time.time() < limit and not marker.exists():
                    time.sleep(0.02)
                self.assertTrue(marker.exists(), "the live board owner did not start")

                swarm.keep_this_board("During run", self.config)
                self.assertEqual(swarm.export_kept_board("During run")["name"], "During run")
                swarm.import_kept_board(portable, "Imported during run")
                swarm.forget_this_board("Named", self.config)

                after = swarm.load()
                self.assertEqual([one.name for one in after.agents], ["Topology stays"])
                self.assertEqual(after.projects, initial.projects)
                self.assertEqual(after.works_on, initial.works_on)
                self.assertEqual(after.talks_to, initial.talks_to)
                self.assertEqual(after.active_saved_board, "")
                self.assertEqual(
                    {one["name"] for one in swarm.every_kept_board()},
                    {"During run", "Imported during run"},
                )
                with self.assertRaisesRegex(HarnessError, "global Swarm board"):
                    swarm.open_this_board("During run", self.config)
                with self.assertRaisesRegex(HarnessError, "global Swarm board"):
                    swarm.save({"agents": [{"name": "Forbidden live change"}]}, self.config)

                SwarmRunStore(self.config).request_stop(marker.read_text(encoding="utf-8"))
                live.join(5)
            finally:
                if live.is_alive():
                    live.terminate()
                    live.join(2)
            self.assertEqual(live.exitcode, 0)

    def test_harmless_board_save_does_not_upgrade_legacy_execution_journal(self) -> None:
        self.runtime.mkdir(parents=True)
        database = self.runtime / "runs.sqlite3"
        with closing(sqlite3.connect(database)) as db:
            db.execute(
                "CREATE TABLE runs(run_id TEXT PRIMARY KEY, request_id TEXT NOT NULL)"
            )
            db.execute("INSERT INTO runs VALUES('legacy-run','legacy-request')")
            db.commit()
        board_file = self.container / "settings" / "swarm.json"
        with mock.patch.object(swarm, "where_it_lives", return_value=board_file):
            saved = swarm.save({"agents": [{"name": "Still editable"}]}, self.config)
        self.assertEqual(saved.agents[0].name, "Still editable")
        self.assertEqual(global_board_change_pause_reason(self.config), "")
        with closing(sqlite3.connect(database)) as db:
            self.assertEqual(
                [row[1] for row in db.execute("PRAGMA table_info(runs)")],
                ["run_id", "request_id"],
            )
            self.assertEqual(
                db.execute("SELECT * FROM runs").fetchall(),
                [("legacy-run", "legacy-request")],
            )
            self.assertIsNone(db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='runs_global_request_legacy'"
            ).fetchone())

    def test_named_board_library_stays_editable_while_open_is_globally_fenced(self) -> None:
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
            swarm.keep_this_board("Another", self.config)
            with self.assertRaisesRegex(HarnessError, "global Swarm board"):
                swarm.open_this_board("Known", self.config)
            swarm.forget_this_board("Known", self.config)
            self.assertEqual([one["name"] for one in swarm.every_kept_board()], ["Another"])
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
        # The owner can observe the durable stop request and finish between the
        # request transaction and this projection. Both values are truthful,
        # monotonic snapshots; a terminal ``stopped`` result is not a lost
        # fence or a reason to report stale ``stopping`` to the caller.
        self.assertIn(stopped["status"], {"stopping", "stopped"})
        process.join(10)
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(controller.get(run_id)["status"], "stopped")

    def test_running_stop_accepts_owner_terminalizing_before_projection_returns(self) -> None:
        store, run_id = self._running("running-stop-terminal-race")
        running = swarm.Running(store)
        request_stop = store.request_stop

        def owner_finishes_during_projection(identity: str) -> dict:
            request_stop(identity)
            store.fail(run_id, "stopped by owner", stopped=True)
            return store.get(run_id)

        with mock.patch.object(
            store, "request_stop", side_effect=owner_finishes_during_projection,
        ):
            note = running.stop(run_id)

        self.assertIn("already stopped", note)
        self.assertEqual(store.get(run_id)["status"], "stopped")

        completed, completed_run = self._running("running-stop-completed")
        completed.finish(completed_run, {"complete": True})
        with self.assertRaisesRegex(swarm.SwarmError, "already over"):
            swarm.Running(completed).stop(completed_run)

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
