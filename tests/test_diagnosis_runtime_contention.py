"""An agent's free diagnosis must not wait behind saved-work recovery."""

from __future__ import annotations

import copy
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from our_harness import long_horizon, provider_repair
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.server import HarnessHTTPServer


class DiagnosisDuringRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        (root / ".harness").mkdir()
        data = copy.deepcopy(DEFAULT_CONFIG)
        data["providers"] = {
            name: {"kind": "local", "model": "fixture", "command": ["fixture-tool"]}
            for name in ("portable-route", "another-route")
        }
        config = LoadedConfig(data, root, [], {})
        self.server = HarnessHTTPServer(("127.0.0.1", 0), config)
        serving = threading.Thread(target=self.server.serve_forever,
                                   kwargs={"poll_interval": 0.01}, daemon=True)
        serving.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.board = {"agents": [{"id": "portable-agent", "who": "portable-route"}],
                      "projects": []}

    def call(self, body: dict) -> tuple[int, dict]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port,
                                                timeout=5)
        try:
            connection.request("POST", "/api/team/repair-plan", json.dumps(body), {
                "Content-Type": "application/json", "X-Harness-Token": self.server.token,
            })
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def diagnose_while_recovering(self, body: dict) -> tuple[int, dict]:
        recovering = threading.Event()
        release = threading.Event()
        responded = threading.Event()
        probed = threading.Event()
        results: list[tuple[int, dict]] = []
        errors: list[BaseException] = []
        runtime = mock.Mock()

        def recover() -> None:
            recovering.set()
            if not release.wait(10):
                raise RuntimeError("Fixture recovery was not released")

        def initialize() -> None:
            try:
                self.server.long_horizon
            except BaseException as error:
                errors.append(error)

        def diagnose() -> None:
            try:
                results.append(self.call(body))
            except BaseException as error:
                errors.append(error)
            finally:
                responded.set()

        def plan(_config, route, **_kwargs):
            probed.set()
            return {"route": route, "state": "ready", "kind": "local",
                    "repair": {"state": "ready", "summary": "Exact route checked",
                               "actions": [], "diagnosis_costs_model_request": False}}

        runtime.recover_all.side_effect = recover
        store = mock.Mock()
        store.active_authority_goals.return_value = []
        with mock.patch.object(long_horizon, "LongHorizonRuntime", return_value=runtime), \
                mock.patch.object(long_horizon, "GoalStore", return_value=store), \
                mock.patch.object(provider_repair, "repair_plan", side_effect=plan), \
                mock.patch("our_harness.server.swarm_lab.how_it_stands",
                           return_value={"board": self.board}), \
                mock.patch("our_harness.chat.already_set_up", return_value=[]), \
                mock.patch("our_harness.chat.ask_once") as model:
            initializer = threading.Thread(target=initialize, daemon=True)
            caller = threading.Thread(target=diagnose, daemon=True)
            initializer.start()
            try:
                self.assertTrue(recovering.wait(3), "Runtime recovery did not begin")
                caller.start()
                self.assertTrue(probed.wait(3), "The non-billing provider probe did not run")
                self.assertTrue(responded.wait(2),
                                "Agent diagnosis blocked behind the recovery authority lock")
                self.assertFalse(release.is_set(), "Fixture released recovery too early")
                self.assertFalse(errors, errors)
                model.assert_not_called()
            finally:
                release.set()
                initializer.join(5)
                if caller.ident is not None:
                    caller.join(5)
            self.assertFalse(initializer.is_alive())
            self.assertFalse(caller.is_alive())
        return results[0]

    def test_selected_agent_gets_real_diagnosis_before_recovery_finishes(self) -> None:
        status, body = self.diagnose_while_recovering({
            "route": "portable-route", "agent_id": "portable-agent",
        })
        self.assertEqual(status, 200, body)
        self.assertEqual(body["repair"]["summary"], "Exact route checked")
        self.assertFalse(body["repair"]["diagnosis_costs_model_request"])

    def test_diagnosis_does_not_bypass_agent_route_ownership_during_recovery(self) -> None:
        status, body = self.diagnose_while_recovering({
            "route": "another-route", "agent_id": "portable-agent",
        })
        self.assertEqual(status, 400, body)
        self.assertIn("another agent or route", body["error"])

    def test_route_only_probe_remains_available_during_recovery(self) -> None:
        status, body = self.diagnose_while_recovering({"route": "portable-route"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["repair"]["state"], "ready")
