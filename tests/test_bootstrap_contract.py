from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from our_harness import server as server_module


class Events:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, item: dict) -> None:
        self.items.append(item)


class FakeApplication:
    proof: dict = {}

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def run_task(self, *_args, **_kwargs):
        return {"run_id": "bootstrap", "state": "complete"}

    def test(self, **_kwargs):
        return self.proof


class BootstrapContractTests(unittest.TestCase):
    def handler(self):
        events = Events()
        panel = SimpleNamespace(config=object(), events=events, release_run=mock.Mock())
        return SimpleNamespace(server=panel), events

    def test_bootstrap_cannot_publish_completion_without_real_test_evidence(self) -> None:
        handler, events = self.handler()
        FakeApplication.proof = {
            "passed": False, "commands": [["python", "-m", "unittest"]],
            "verification_problems": [{"reason": "test command reported that zero tests ran"}],
        }
        with mock.patch.object(server_module, "HarnessApplication", FakeApplication):
            server_module.HarnessHandler._run_task(handler, "make it", False, None, True)
        self.assertFalse(any(item["kind"] == "run_result" for item in events.items))
        self.assertTrue(any(item["kind"] == "run_error" and "milestone" in item["payload"]["error"] for item in events.items))
        handler.server.release_run.assert_called_once()

    def test_bootstrap_completion_includes_server_side_verification_proof(self) -> None:
        handler, events = self.handler()
        FakeApplication.proof = {
            "passed": True, "commands": [["pytest"]], "results": [{"stdout": "2 passed"}],
            "verification_problems": [],
        }
        with mock.patch.object(server_module, "HarnessApplication", FakeApplication):
            server_module.HarnessHandler._run_task(handler, "make it", False, None, True)
        result = next(item for item in events.items if item["kind"] == "run_result")
        self.assertTrue(result["payload"]["bootstrap_verification"]["passed"])
        states = [item["payload"]["state"] for item in events.items if item["kind"] == "bootstrap_milestone"]
        self.assertEqual(states, ["required", "passed"])


if __name__ == "__main__":
    unittest.main()
