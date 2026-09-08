from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from our_harness.agent_tools import AgentToolSession
from our_harness.config import load_config
from our_harness.context import CompiledContext
from our_harness.models import HarnessError, NativeToolContinuation, ProviderResponse, ResponseFormat
from our_harness.providers.base import OllamaProvider
from our_harness.workflow import HarnessApplication, WorkflowDeadline


FINAL = ResponseFormat("identity_fixture", {
    "type": "object", "properties": {"answer": {"type": "string"}},
    "required": ["answer"], "additionalProperties": False,
})


def generated_call(name: str, arguments: dict) -> ProviderResponse:
    calls, _names = OllamaProvider._tool_calls({
        "tool_calls": [{"function": {"name": name, "arguments": arguments}}],
    })
    return ProviderResponse("", finish_reason="tool_calls", raw={"tool_call_deltas": calls},
        native_continuation=NativeToolContinuation("ollama", {"fixture": True}, [calls[0]["id"]]))


class WorkflowToolIdentityTests(unittest.TestCase):
    def config(self, root: Path):
        return load_config(root, cli_overrides={"provider": {
            "name": "ollama", "endpoint": "http://127.0.0.1:11434", "model": "portable-fixture",
        }})

    def session(self, app: HarnessApplication, run_id: str | None = None, budget: dict | None = None):
        app.agent_tool_session = AgentToolSession(
            app.config, app.memory, WorkflowDeadline.start(30), lambda *_: None, run_id=run_id,
        )
        if budget is not None:
            app.agent_tool_session.restore_budget_state(budget)
        return app.agent_tool_session

    def test_generated_native_ids_are_local_to_each_response(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "first.txt").write_text("first observation", encoding="utf-8")
            (root / "second.txt").write_text("second observation", encoding="utf-8")
            calls = [generated_call("read_file", {
                "path": name, "start_line": 1, "end_line": 1, "max_bytes": 100,
            }) for name in ("first.txt", "second.txt")]
            self.assertEqual([one.raw["tool_call_deltas"][0]["id"] for one in calls], ["ollama-0"] * 2)
            responses = iter([*calls, ProviderResponse('{"answer":"done"}')])
            captured = []

            def provider(*_args, **kwargs):
                captured.append(kwargs)
                return next(responses)

            with HarnessApplication(self.config(root)) as app:
                session = self.session(app, app.memory.start_run("native response IDs"))
                with patch.object(app, "_provider_response", side_effect=provider):
                    result = app._request_with_tools(CompiledContext("policy", "prefix", "", {}), "Inspect both files", FINAL, "worker")
                self.assertEqual(session.calls, 2)
            self.assertEqual(result, {"answer": "done"})
            for index, observation in ((1, "first observation"), (2, "second observation")):
                output = captured[index]["native_function_call_outputs"][0]
                self.assertEqual(output.call_id, "ollama-0")
                receipt = json.loads(output.output)
                self.assertEqual(receipt["status"], "ok")
                self.assertIn(observation, receipt["content"])

    def test_action_envelope_ids_are_local_to_each_response(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source.txt").write_text("first\nsecond\n", encoding="utf-8")
            responses = [ProviderResponse(json.dumps({"action": "tool", "tool": {
                "call_id": "read", "name": "read_file", "arguments": {
                    "path": "source.txt", "start_line": line, "end_line": line, "max_bytes": 100,
                },
            }})) for line in (1, 2)] + [ProviderResponse('{"answer":"done"}')]
            with HarnessApplication(self.config(root)) as app:
                session = self.session(app, app.memory.start_run("envelope response IDs"))
                with patch.object(app, "_provider_response", side_effect=responses):
                    result = app._request_with_tools(CompiledContext("policy", "prefix", "", {}), "Inspect both lines", FINAL, "worker")
                self.assertEqual(session.calls, 2)
                results = [json.loads(row[0]) for row in app.memory.connection.execute("SELECT result_json FROM agent_tool_journal")]
            self.assertEqual(result, {"answer": "done"})
            self.assertEqual([one["status"] for one in results], ["ok", "ok"])
            self.assertIn("second", results[1]["content"])

    def test_restarting_same_request_replays_remote_receipt_and_preserves_consumed_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.config(root)
            compiled = CompiledContext("policy", "prefix", "unchanged evidence", {})
            definitions = [{"name": "mcp_call", "description": "Fixture remote observation", "input_schema": {"type": "object"}}]
            effect = generated_call("mcp_call", {"value": 1})
            with HarnessApplication(config) as app:
                run_id = app.memory.start_run("stable replay request")
                session = self.session(app, run_id)
                with patch.object(session, "definitions", return_value=definitions), \
                        patch.object(session, "_dispatch", return_value={"classification": "read_only", "receipt": "observation-once"}) as dispatch, \
                        patch.object(app, "_provider_response", side_effect=[effect, HarnessError("restart fixture")]):
                    with self.assertRaisesRegex(HarnessError, "restart fixture"):
                        app._request_with_tools(compiled, "Perform one effect", FINAL, "worker")
                dispatch.assert_called_once()
                budget = session.budget_state()
            for arguments, expected_status in (({"value": 1}, "ok"), ({"value": 2}, "error")):
                with self.subTest(arguments=arguments), HarnessApplication(config) as app:
                    session = self.session(app, run_id, budget)
                    self.assertEqual(session.calls, budget["calls"])
                    self.assertEqual(session.total_bytes, budget["total_bytes"])
                    captured = []
                    responses = iter([generated_call("mcp_call", arguments), ProviderResponse('{"answer":"done"}')])

                    def provider(*_args, **kwargs):
                        captured.append(kwargs)
                        return next(responses)

                    with patch.object(session, "definitions", return_value=definitions), \
                            patch.object(session, "_dispatch") as dispatch, \
                            patch.object(app, "_provider_response", side_effect=provider):
                        app._request_with_tools(compiled, "Perform one effect", FINAL, "worker")
                    dispatch.assert_not_called()
                    output = json.loads(captured[1]["native_function_call_outputs"][0].output)
                    self.assertEqual(output["status"], expected_status)
                    self.assertEqual(session.calls, budget["calls"] + 1)
                    self.assertGreater(session.total_bytes, budget["total_bytes"])
                    self.assertEqual(app.memory.connection.execute("SELECT count(*) FROM agent_tool_journal").fetchone()[0], 1)

    def test_legacy_remote_receipt_is_preserved_even_after_an_older_checkpoint(self):
        for checkpoint_after_tool in (False, True):
            for arguments, expected_status in (({"value": 1}, "ok"), ({"value": 2}, "error")):
                with self.subTest(checkpoint_after_tool=checkpoint_after_tool, arguments=arguments), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    config = self.config(root)
                    definitions = [{"name": "mcp_call", "description": "Fixture remote observation", "input_schema": {"type": "object"}}]
                    with HarnessApplication(config) as app:
                        run_id = app.memory.start_run("legacy effect receipt")
                        session = self.session(app, run_id)
                        old_checkpoint = session.budget_state()
                        with patch.object(session, "_dispatch", return_value={"classification": "read_only", "receipt": "legacy-once"}) as dispatch:
                            session.execute("worker", "ollama-0", "mcp_call", {"value": 1})
                        dispatch.assert_called_once()
                        budget = session.budget_state() if checkpoint_after_tool else old_checkpoint
                        budget["schema_version"] = 1
                        budget.pop("identity_contract_sha256")
                        budget.pop("legacy_call_ids_sha256")
                        budget["call_ids_sha256"] = {
                            hashlib.sha256(b"ollama-0").hexdigest(): next(iter(session.call_ids.values())),
                        } if checkpoint_after_tool else {}
                    with HarnessApplication(config) as app:
                        session = self.session(app, run_id, budget)
                        captured = []
                        responses = iter([generated_call("mcp_call", arguments), ProviderResponse('{"answer":"done"}')])

                        def provider(*_args, **kwargs):
                            captured.append(kwargs)
                            return next(responses)

                        with patch.object(session, "definitions", return_value=definitions), \
                                patch.object(session, "_dispatch") as dispatch, \
                                patch.object(app, "_provider_response", side_effect=provider):
                            app._request_with_tools(CompiledContext("policy", "prefix", "", {}), "Resume operation", FINAL, "worker")
                        dispatch.assert_not_called()
                        output = json.loads(captured[1]["native_function_call_outputs"][0].output)
                        self.assertEqual(output["status"], expected_status)
                        self.assertEqual(app.memory.connection.execute("SELECT count(*) FROM agent_tool_journal").fetchone()[0], 1)

    def test_real_checkpoint_resume_keeps_receipt_identity_when_context_is_recompiled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "marker.txt"
            marker.write_text("initial source context", encoding="utf-8")
            config = self.config(root)
            definitions = [{"name": "mcp_call", "description": "Fixture remote observation", "input_schema": {"type": "object"}}]
            contexts = []
            receipts = []
            run_id = ""
            for resumed in (False, True):
                if resumed:
                    marker.write_text("source context changed while the process was closed", encoding="utf-8")
                with HarnessApplication(config) as app:
                    responses = iter([generated_call("mcp_call", {"query": "remote-observation"}), ProviderResponse('{"answer":"done"}')])

                    def provider(*_args, **kwargs):
                        outputs = kwargs.get("native_function_call_outputs")
                        if outputs:
                            receipts.append(json.loads(outputs[0].output))
                        return next(responses)

                    def interrupted_plan(_task, compiled, deadline, node):
                        nonlocal run_id
                        run_id = app._active_run_id
                        contexts.append(compiled.dynamic)
                        app._request_with_tools(compiled, "Inspect the remote observation", FINAL, node, deadline=deadline, require_tools=True)
                        raise SystemExit("crash after retained tool receipt")

                    with patch.object(AgentToolSession, "definitions", return_value=definitions), \
                            patch.object(AgentToolSession, "_dispatch", return_value={"classification": "read_only", "receipt": "retained-observation"}) as dispatch, \
                            patch.object(app, "_provider_response", side_effect=provider), \
                            patch.object(app, "_plan", side_effect=interrupted_plan):
                        with self.assertRaisesRegex(SystemExit, "retained tool receipt"):
                            if resumed:
                                app.resume_task(run_id)
                            else:
                                app.run_task("Inspect a remote observation")
                    self.assertIsNotNone(app.memory.load_run_checkpoint(run_id))
                    self.assertEqual(dispatch.call_count, 0 if resumed else 1)
            self.assertNotEqual(contexts[0], contexts[1])
            self.assertFalse(receipts[0]["replayed"])
            self.assertTrue(receipts[1]["replayed"])
            self.assertEqual(receipts[0]["content"], receipts[1]["content"])


if __name__ == "__main__":
    unittest.main()
