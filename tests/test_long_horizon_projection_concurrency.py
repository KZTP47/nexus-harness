"""Independent saved chats remain usable during long-horizon history catch-up."""
from __future__ import annotations

import copy
import json
import threading
import time
import unittest
from unittest import mock

from our_harness import chat, long_horizon, server
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from tests import test_goal_chat_projection as projection_fixtures
from tests import test_long_horizon as goal_fixtures
from tests import test_the_board_of_agents as panel_fixtures


class LongHorizonProjectionConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = panel_fixtures.WhatThePanelIsTold()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.panel = self.fixture.panel
        self.ask = self.fixture.ask
        self.lead, self.peer, self.first, self.second = (
            self.fixture.saved_project_pair_with_two_chats()
        )
        other_root = self.fixture.a_project("independent-project")
        board = copy.deepcopy(self.panel.swarm_standing()["board"])
        board["projects"].append({"id": "project-2", "name": "Independent", "path": str(other_root)})
        board["works_on"].extend([
            {"agent": self.lead, "project": "project-2"},
            {"agent": self.peer, "project": "project-2"},
        ])
        status, saved = self.ask("/api/swarm/save", {"board": board})
        self.assertEqual(status, 200, saved)
        status, selected = self.ask("/api/swarm/chats/project", {
            "agent": self.lead, "chat": self.second["id"], "project": "project-2",
        })
        self.assertEqual(status, 200, selected)
        self.runtime = self.fixture.fake_long_horizon_runtime()
        self.goal = {
            "goal_id": "goal-projection-concurrency-1234567890",
            "request_id": "request-projection-concurrency-1234567890",
            "conversation_id": self.first["id"],
            "project": {"id": "project-1", "name": "Shared", "path": str(self.fixture.where)},
            "lead_agent_id": self.lead,
            "requested_agent_ids": [self.lead, self.peer],
            "agents": [{"id": self.lead}, {"id": self.peer}],
            "status": "running", "revision": 2, "tasks": [], "budget": {},
        }
        self.runtime.store.list.return_value = [self.goal]

    def _request_thread(self, name, path, payload, results, finished):
        def request():
            try:
                results[name] = self.ask(path, payload)
            except BaseException as exc:
                results[name] = exc
            finally:
                finished[name].set()
        thread = threading.Thread(target=request, name="projection-test-" + name)
        thread.start()
        return thread

    def test_unrelated_chat_navigation_and_admission_finish_during_slow_projection(self):
        entered, release = threading.Event(), threading.Event()
        results, finished, workers = {}, {}, []

        def slow_projection(_server, _goal, _chat, **_context):
            entered.set()
            if not release.wait(10):
                raise RuntimeError("The projection test did not release its archive reader")
            return False

        requests = {
            "chats": (f"/api/swarm/chats?agent={self.lead}", None),
            "said": (f"/api/swarm/said?agent={self.lead}&chat={self.second['id']}", None),
            "activate": ("/api/swarm/chats/activate", {
                "agent": self.lead, "chat": self.second["id"],
            }),
            "prepare": ("/api/long-horizon/prepare-admission", {
                "project_id": "project-2", "lead_id": self.lead,
                "chat_id": self.second["id"], "text": "Create another project file",
                "request_id": "independent-chat-preparation-1234567890",
            }),
        }
        with mock.patch.object(
            server.HarnessHTTPServer, "_project_long_horizon_dialogue",
            side_effect=slow_projection,
        ):
            try:
                finished["goals"] = threading.Event()
                workers.append(self._request_thread(
                    "goals", "/api/long-horizon/goals", None, results, finished,
                ))
                self.assertTrue(entered.wait(5), results)
                for name, (path, payload) in requests.items():
                    finished[name] = threading.Event()
                    workers.append(self._request_thread(name, path, payload, results, finished))
                deadline = time.monotonic() + 3
                for name in requests:
                    self.assertTrue(
                        finished[name].wait(max(0, deadline - time.monotonic())),
                        f"{name} waited for an unrelated chat's goal projection: {results}",
                    )
                self.assertFalse(finished["goals"].is_set())
                for name in requests:
                    self.assertIsInstance(results[name], tuple, results)
                    self.assertIn(results[name][0], {200, 202}, results)
                self.assertEqual(results["said"][1]["conversation"]["id"], self.second["id"])
                self.assertEqual(results["activate"][1]["active"], self.second["id"])
                self.assertEqual(results["prepare"][1]["pending"]["chat_id"], self.second["id"])
                self.assertEqual(results["prepare"][1]["pending"]["project_id"], "project-2")
            finally:
                release.set()
                for worker in workers:
                    worker.join(5)
                    self.assertFalse(worker.is_alive(), results)
        self.assertEqual(results["goals"][0], 200, results)

    def test_same_chat_projection_keeps_its_exact_nonblocking_lease(self):
        entered, release = threading.Event(), threading.Event()
        results, finished = {}, {"first": threading.Event(), "second": threading.Event()}
        workers = []

        def slow_projection(_server, _goal, _chat, **_context):
            entered.set()
            if not release.wait(10):
                raise RuntimeError("The projection lease test did not release its reader")
            return False

        with mock.patch.object(
            server.HarnessHTTPServer, "_project_long_horizon_dialogue",
            side_effect=slow_projection,
        ) as project:
            try:
                workers.append(self._request_thread(
                    "first", "/api/long-horizon/goals", None, results, finished,
                ))
                self.assertTrue(entered.wait(5), results)
                workers.append(self._request_thread(
                    "second", "/api/long-horizon/goals", None, results, finished,
                ))
                self.assertTrue(finished["second"].wait(3), results)
                self.assertEqual(results["second"][0], 200, results)
                self.assertFalse(finished["first"].is_set())
                self.assertEqual(project.call_count, 1)
                status, rejected = self.ask("/api/swarm/chats/delete", {
                    "agent": self.lead, "chat": self.first["id"],
                })
                self.assertEqual(status, 400, rejected)
                self.assertIn("already working on another request", rejected["error"])
            finally:
                release.set()
                for worker in workers:
                    worker.join(5)
                    self.assertFalse(worker.is_alive(), results)
        self.assertEqual(results["first"][0], 200, results)


class LongHorizonProjectionContextTests(unittest.TestCase):
    def test_late_projection_keeps_original_configuration_store_and_transcript(self):
        fixture = projection_fixtures.GoalChatProjectionTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        other_root = fixture.root / "replacement configuration"
        other_root.mkdir()
        other_config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), other_root, [], {})
        replacement_runtime = mock.Mock()
        replacement_runtime.store.dialogue_history.side_effect = AssertionError("History crossed configurations")
        replacement_runtime.store.events.side_effect = AssertionError("Events crossed configurations")
        message = fixture.message(1, "The old goal's recovered public statement")

        def archive(_goal_id, after=0, **_kwargs):
            # A completed/paused goal's projection can outlive a permitted
            # settings reload. All subsequent reads and writes retain the
            # configuration captured when this projection was admitted.
            state.config = other_config
            state.long_horizon = replacement_runtime
            return fixture.page([message] if after == 0 else [])

        state = fixture.server(archive)
        original_runtime = state.long_horizon
        server.HarnessHTTPServer.project_long_horizon_chat_statuses(state, [fixture.goal])
        self.assertEqual([one.text for one in fixture.speech()], [message["summary"]])
        original_turns = chat.read_it(fixture.config, "route-blue", fixture.filed_as)
        self.assertTrue(any(one.correlation.get("kind") == "long_horizon_status" for one in original_turns))
        self.assertEqual(chat.read_it(other_config, "route-blue", fixture.filed_as), [])
        self.assertFalse(list(other_root.rglob("*.events.jsonl")))
        original_runtime.store.events.assert_called_once()
        replacement_runtime.store.events.assert_not_called()


class IndependentLongHorizonExecutionTests(unittest.TestCase):
    def test_distinct_roots_share_an_agent_and_cancel_independently(self):
        fixture = goal_fixtures.LongHorizonTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        other = fixture.base / "independent target"
        other.mkdir()
        board = copy.deepcopy(fixture.board)
        board["projects"].append({
            "id": "other", "name": "Other", "path": str(other), "is_there": True, "tasks": [],
        })
        board["works_on"].append({"agent": "lead", "project": "other"})
        runtime = long_horizon.LongHorizonRuntime(fixture.config)
        self.addCleanup(runtime.close)
        entered = [threading.Event(), threading.Event()]
        released = [threading.Event(), threading.Event()]
        calls, lock = [], threading.Lock()

        def provider(*_args, **kwargs):
            before, after = kwargs.get("before_provider_dispatch"), kwargs.get("after_provider_response")
            if before:
                before("initial")
            with lock:
                index = len(calls)
                calls.append(kwargs["conversation_key"])
            self.assertLess(index, 2)
            entered[index].set()
            self.assertTrue(released[index].wait(10))
            if after:
                after("initial")
            return {"text": json.dumps(goal_fixtures.action(criteria_evidence=[{
                "criterion": "Original objective is satisfied", "evidence_refs": ["verified-no-change"],
            }]))}

        def await_status(goal_id, expected):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if runtime.store.get(goal_id)["status"] == expected:
                    return
                time.sleep(0.02)
            self.assertEqual(runtime.store.get(goal_id)["status"], expected)

        with mock.patch.object(long_horizon.chat_lab, "ask_once", side_effect=provider), mock.patch.object(
            long_horizon.swarm_work, "_run_selected_project_verification",
            return_value={"status": "passed", "basis": "independent temporary-root execution"},
        ):
            try:
                first = runtime.start(board, "project", ["First"], "independent-first", participant_ids=["lead"])
                self.assertTrue(entered[0].wait(5))
                second = runtime.start(board, "other", ["Second"], "independent-second", participant_ids=["lead"])
                self.assertTrue(entered[1].wait(5), "Second goal was serialized behind the first")
                self.assertEqual(runtime.store.get(first["goal_id"])["status"], "running")
                self.assertEqual(runtime.store.get(second["goal_id"])["status"], "running")
                self.assertNotEqual(calls[0], calls[1])
                draining = runtime.control(first["goal_id"], "cancel")
                self.assertEqual(draining["status"], "cancelling")
                released[0].set()
                await_status(first["goal_id"], "cancelled")
                self.assertEqual(runtime.store.get(second["goal_id"])["status"], "running")
                released[1].set()
                await_status(second["goal_id"], "complete")
                self.assertEqual(len(calls), 2)
            finally:
                for event in released:
                    event.set()
                runtime.close()


if __name__ == "__main__":
    unittest.main()
