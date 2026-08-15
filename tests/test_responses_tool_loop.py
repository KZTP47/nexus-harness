from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from our_harness.agent_tools import AgentToolSession
from our_harness.config import load_config
from our_harness.context import CompiledContext
from our_harness.models import ProviderResponse, ResponseFormat, ResponsesContinuation
from our_harness.workflow import HarnessApplication, WorkflowDeadline


class ResponsesToolLoopTests(unittest.TestCase):
    def test_auto_mode_continues_native_tool_call_with_typed_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "value.txt").write_text("evidence\n", encoding="utf-8")
            config = load_config(
                root,
                cli_overrides={"provider": {"name": "openai", "api_mode": "auto"}},
            )
            response_format = ResponseFormat(
                "fixture_final_v1",
                {
                    "type": "object",
                    "properties": {"answer": {"type": "string"}},
                    "required": ["answer"],
                    "additionalProperties": False,
                },
            )
            continuation = ResponsesContinuation("resp-tool", [{"type": "function_call", "call_id": "call-1"}])
            first = ProviderResponse(
                "",
                raw={
                    "tool_call_deltas": [
                        {
                            "index": 0,
                            "id": "call-1",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps(
                                    {"path": "value.txt", "start_line": 1, "end_line": 1, "max_bytes": 100}
                                ),
                            },
                        }
                    ]
                },
                responses_continuation=continuation,
            )
            second = ProviderResponse(json.dumps({"answer": "done"}), raw={})
            captured: list[tuple[tuple, dict]] = []

            def fake_response(*args, **kwargs):
                captured.append((args, kwargs))
                return first if len(captured) == 1 else second

            with HarnessApplication(config) as app:
                run_id = app.memory.start_run("responses tool fixture")
                app.agent_tool_session = AgentToolSession(
                    config,
                    app.memory,
                    WorkflowDeadline.start(5),
                    lambda *_: None,
                    run_id=run_id,
                )
                compiled = CompiledContext("policy", "prefix-hash", "", {})
                with patch.object(app, "_provider_response", side_effect=fake_response):
                    result = app._request_with_tools(compiled, "Inspect the file", response_format, "planner")

            self.assertEqual(result, {"answer": "done"})
            self.assertEqual(len(captured), 2)
            first_prompt = captured[0][0][1]
            second_prompt = captured[1][0][1]
            self.assertNotIn("ACTION ENVELOPE JSON SCHEMA", first_prompt)
            self.assertNotIn("TOOL TRANSCRIPT", second_prompt)
            self.assertIs(captured[1][1]["responses_continuation"], continuation)
            outputs = captured[1][1]["function_call_outputs"]
            self.assertEqual([item.call_id for item in outputs], ["call-1"])
            replayed_result = json.loads(outputs[0].output)
            self.assertEqual(replayed_result["status"], "ok")
            self.assertIn("evidence", replayed_result["content"])
            self.assertIs(captured[1][1]["response_format"], response_format)
            self.assertEqual(captured[0][1]["tools"], captured[1][1]["tools"])


if __name__ == "__main__":
    unittest.main()
