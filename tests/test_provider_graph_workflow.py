from __future__ import annotations

import hashlib
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from our_harness.config import load_config as _load_config
from our_harness.cli import main
from our_harness.graphs import ProductionGraphInterpreter, built_in_workflow_graph, resolve_graph_execution_policy, simulate_graph, validate_graph
from our_harness.models import (
    CommandResult,
    Detection,
    FunctionCallOutput,
    HarnessError,
    ProviderRequest,
    ProviderResponse,
    ResponseFormat,
    ResponsesContinuation,
    ReviewVerdict,
)
from our_harness.providers.base import OpenAIProvider, OllamaProvider, StreamDecoder, collect_stream
from our_harness.workflow import HarnessApplication, WorkflowDeadline


def load_config(root: Path, **kwargs):
    local = root / ".harness" / "config.local.json"
    return _load_config(root, explicit=local if local.is_file() else None, **kwargs)


def requirement(requirement_text: str, source_quote: str, category: str = "behavior") -> dict[str, str]:
    return {
        "id": "R1",
        "requirement": requirement_text,
        "category": category,
        "counterexample": f"R1: {requirement_text} is not satisfied",
    }


def witness(path: str) -> dict[str, str]:
    return {
        "requirement_id": "R1",
        "file": path,
        "code_path": "implementation for R1",
        "counterexample_result": "The R1 counterexample now has the required result",
    }


class StreamTests(unittest.TestCase):
    def test_split_utf8_and_line_frames(self) -> None:
        decoder = StreamDecoder()
        raw = '{"text":"å"}\n{"done":true}\n'.encode("utf-8")
        lines = []
        for byte in raw:
            lines.extend(decoder.feed(bytes([byte])))
        lines.extend(decoder.feed(b"", final=True))
        self.assertEqual(lines, ['{"text":"å"}', '{"done":true}'])

    def test_buffer_limit(self) -> None:
        decoder = StreamDecoder(max_buffer_bytes=5)
        with self.assertRaisesRegex(Exception, "limit"):
            decoder.feed(b"123456")

    def test_workflow_stream_collector_is_strict_and_bounded(self) -> None:
        class FixtureProvider:
            def stream(self, _request):
                yield {"type": "text_delta", "text": "hel"}
                yield {"type": "text_delta", "text": "lo"}
                yield {"type": "usage", "input_tokens": 4, "output_tokens": 2}
                yield {"type": "done", "finish_reason": "stop"}

        request = ProviderRequest("prefix", "dynamic", [], "fixture")
        response = collect_stream(FixtureProvider(), request, max_text_chars=10)
        self.assertEqual(response.text, "hello")
        self.assertEqual(response.input_tokens, 4)

        class MissingDone:
            def stream(self, _request):
                yield {"type": "text_delta", "text": "partial"}

        with self.assertRaisesRegex(HarnessError, "completion"):
            collect_stream(MissingDone(), request)

        class UnknownFrame:
            def stream(self, _request):
                yield {"type": "mystery"}

        with self.assertRaisesRegex(HarnessError, "Unknown"):
            collect_stream(UnknownFrame(), request)

    def test_http_stream_wall_clock_deadline_stops_incomplete_trickle(self) -> None:
        class TrickleHandler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format, *_args):
                return

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Connection", "close")
                self.end_headers()
                until = time.monotonic() + 2.0
                while time.monotonic() < until:
                    try:
                        self.wfile.write(b"{")
                        self.wfile.flush()
                    except OSError:
                        return
                    time.sleep(0.01)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), TrickleHandler)
        server.daemon_threads = True
        server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        server_thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / ".harness").mkdir()
                endpoint = f"http://127.0.0.1:{server.server_port}"
                (root / ".harness" / "config.json").write_text(
                    json.dumps({"provider": {"name": "ollama", "endpoint": endpoint, "timeout_seconds": 5}}),
                    encoding="utf-8",
                )
                provider = OllamaProvider(load_config(root))
                started = time.monotonic()
                with self.assertRaisesRegex(HarnessError, "wall-clock deadline"):
                    list(provider._stream_lines(f"{endpoint}/trickle", {}, timeout_seconds=0.25))
                elapsed = time.monotonic() - started
                self.assertGreaterEqual(elapsed, 0.20)
                self.assertLess(elapsed, 0.80)
                self.assertFalse(
                    any(thread.name == "harness-http-stream-reader" and thread.is_alive() for thread in threading.enumerate())
                )
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=1)

    def test_provider_complete_stream_and_embeddings_refuse_redirects(self) -> None:
        target_hits: list[tuple[str, str | None]] = []

        class TargetHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def do_GET(self):
                target_hits.append(("GET", self.headers.get("Authorization")))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def do_POST(self):
                target_hits.append(("POST", self.headers.get("Authorization")))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        target = http.server.ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
        target.daemon_threads = True
        target_thread = threading.Thread(target=target.serve_forever, daemon=True)
        target_thread.start()

        class RedirectHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target.server_port}/capture")
                self.send_header("Content-Length", "0")
                self.end_headers()

        source = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        source.daemon_threads = True
        source_thread = threading.Thread(target=source.serve_forever, daemon=True)
        source_thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary, patch.dict(
                os.environ, {"REDIRECT_TEST_KEY": "credential-must-not-move"}, clear=False
            ):
                root = Path(temporary)
                (root / ".harness").mkdir()
                (root / ".harness" / "config.local.json").write_text(
                    json.dumps(
                        {
                            "provider": {
                                "name": "openai-compatible",
                                "model": "fixture",
                                "endpoint": f"http://127.0.0.1:{source.server_port}/v1",
                                "api_key_env": "REDIRECT_TEST_KEY",
                            },
                            "memory": {"embedding_model": "fixture-embedding"},
                        }
                    ),
                    encoding="utf-8",
                )
                provider = OpenAIProvider(load_config(root))
                request = ProviderRequest("policy", "context", [{"role": "user", "content": "x"}], "fixture")
                with self.assertRaisesRegex(HarnessError, "redirects are not accepted"):
                    provider.complete(request)
                with self.assertRaisesRegex(HarnessError, "redirects are not accepted"):
                    list(provider.stream(request))
                with self.assertRaisesRegex(HarnessError, "redirects are not accepted"):
                    provider.embed(["source text"])
                self.assertEqual(target_hits, [])
        finally:
            source.shutdown()
            source.server_close()
            source_thread.join(timeout=1)
            target.shutdown()
            target.server_close()
            target_thread.join(timeout=1)


class OpenAIProviderTests(unittest.TestCase):
    @staticmethod
    def _config(root: Path, name: str = "openai", api_mode: str = "auto"):
        (root / ".harness").mkdir()
        (root / ".harness" / "config.json").write_text(
            json.dumps({}),
            encoding="utf-8",
        )
        (root / ".harness" / "config.local.json").write_text(
            json.dumps({"provider": {"name": name, "api_mode": api_mode, "endpoint": "https://api.example.test/v1", "prompt_cache_retention": "24h"}}),
            encoding="utf-8",
        )
        return load_config(root)

    def test_provider_boundary_redacts_credentials_before_serialization(self) -> None:
        secret = "sk-providercredential0123456789"
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"HARNESS_TEST_API_KEY": secret}, clear=False
        ):
            provider = OpenAIProvider(self._config(Path(temporary)))
            captured: dict[str, bytes] = {}

            class Response:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return None

                def read(self, _limit):
                    return b'{"ok":true}'

            def open_request(request, timeout=None):
                captured["data"] = request.data
                return Response()

            provider._http_opener.open = open_request
            provider._post("https://api.example.test/v1/responses", {"input": secret, "api_key": "another-secret"})
            body = captured["data"].decode("utf-8")
            self.assertNotIn(secret, body)
            self.assertNotIn("another-secret", body)
            self.assertIn("[REDACTED]", body)

    def test_complete_and_embedding_http_errors_redact_named_profile_credentials(self) -> None:
        secret = "opaque-http-error-profile-value-12345"
        paths: list[str] = []

        class EchoErrorHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                paths.append(self.path)
                payload = json.dumps({"error": f"rejected credential {secret}"}).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), EchoErrorHandler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary, patch.dict(
                os.environ, {"P_ROUTE": secret}, clear=False,
            ):
                root = Path(temporary)
                (root / ".harness").mkdir()
                (root / ".harness" / "config.local.json").write_text(
                    json.dumps(
                        {
                            "provider": {
                                "name": "openai-compatible",
                                "model": "fixture-model",
                                "endpoint": f"http://127.0.0.1:{server.server_port}/v1",
                                "api_key_env": "P_ROUTE",
                            },
                            "memory": {"embedding_model": "fixture-embedding"},
                        }
                    ),
                    encoding="utf-8",
                )
                provider = OpenAIProvider(load_config(root))
                request = ProviderRequest(
                    "policy", "context", [{"role": "user", "content": "answer"}], "fixture-model",
                )
                for operation in (lambda: provider.complete(request), lambda: provider.embed(["source text"])):
                    with self.subTest(operation=operation):
                        with self.assertRaises(HarnessError) as caught:
                            operation()
                        rendered = str(caught.exception)
                        self.assertNotIn(secret, rendered)
                        self.assertIn("[REDACTED]", rendered)
            self.assertEqual(paths, ["/v1/chat/completions", "/v1/embeddings"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)

    def test_responses_mode_sends_schema_cache_options_and_captures_cache_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            provider = OpenAIProvider(self._config(Path(temporary)))
            captured = {}

            def frames(url, payload, headers=None, timeout_seconds=None):
                captured.update({"url": url, "payload": payload, "headers": headers, "timeout": timeout_seconds})
                yield 'data: {"type":"response.output_text.delta","delta":"{\\"ok\\":"}'
                yield 'data: {"type":"response.output_text.delta","delta":"true}"}'
                yield 'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":1200,"output_tokens":8,"input_tokens_details":{"cached_tokens":1000,"cache_write_tokens":128}}}}'

            provider._stream_lines = frames
            response = collect_stream(
                provider,
                ProviderRequest(
                    "stable",
                    "dynamic",
                    [{"role": "user", "content": "answer"}],
                    "model",
                    response_format=ResponseFormat("result", {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}),
                    prompt_cache_key="harness-v1:fixed",
                    prompt_cache_retention="24h",
                ),
            )

            self.assertEqual(response.text, '{"ok":true}')
            self.assertEqual(response.cached_input_tokens, 1000)
            self.assertEqual(response.cache_write_input_tokens, 128)
            self.assertEqual(captured["url"], "https://api.example.test/v1/responses")
            self.assertEqual(captured["payload"]["prompt_cache_key"], "harness-v1:fixed")
            self.assertEqual(captured["payload"]["prompt_cache_options"], {"ttl": "24h"})
            self.assertEqual(captured["payload"]["text"]["format"]["name"], "result")
            self.assertTrue(captured["payload"]["text"]["format"]["strict"])

    def test_responses_mode_reports_refusal_incomplete_and_missing_text(self) -> None:
        request = ProviderRequest("stable", "dynamic", [], "model")
        cases = [
            ([{'type': 'response.refusal.done', 'refusal': 'not available'}], "refused"),
            ([{'type': 'response.incomplete', 'response': {'status': 'incomplete', 'incomplete_details': {'reason': 'max_output_tokens'}}}], "max_output_tokens"),
            ([{'type': 'response.completed', 'response': {'status': 'completed', 'output': []}}], "no output text"),
        ]
        for events, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temporary:
                provider = OpenAIProvider(self._config(Path(temporary)))
                provider._stream_lines = lambda *_args, events=events, **_kwargs: iter(
                    "data: " + json.dumps(event) for event in events
                )
                with self.assertRaisesRegex(HarnessError, message):
                    list(provider.stream(request))

    def test_compatible_auto_mode_keeps_chat_completions_without_openai_options(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            provider = OpenAIProvider(self._config(Path(temporary), name="openai-compatible"))
            captured = {}

            def frames(url, payload, headers=None, timeout_seconds=None):
                captured.update({"url": url, "payload": payload})
                yield 'data: {"choices":[{"delta":{"content":"{\\"ok\\":true}"},"finish_reason":"stop"}]}'
                yield "data: [DONE]"

            provider._stream_lines = frames
            response = collect_stream(
                provider,
                ProviderRequest(
                    "stable",
                    "dynamic",
                    [],
                    "model",
                    response_format=ResponseFormat("result", {"type": "object"}),
                    prompt_cache_key="not-forwarded",
                    prompt_cache_retention="24h",
                ),
            )
            self.assertEqual(response.text, '{"ok":true}')
            self.assertEqual(captured["url"], "https://api.example.test/v1/chat/completions")
            self.assertNotIn("response_format", captured["payload"])
            self.assertNotIn("prompt_cache_key", captured["payload"])
            self.assertNotIn("stream_options", captured["payload"])

    def test_responses_tool_round_preserves_state_and_uses_typed_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            provider = OpenAIProvider(self._config(Path(temporary)))
            payloads: list[dict[str, object]] = []
            responses = [
                {
                    "id": "resp_1",
                    "status": "completed",
                    "output": [
                        {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"},
                        {"type": "function_call", "call_id": "call_1", "name": "read_file", "arguments": '{"path":"a.py"}'},
                    ],
                },
                {
                    "id": "resp_2",
                    "status": "completed",
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"ok":true}'}]}],
                },
            ]

            def post(_url, payload, _headers=None, _timeout=None):
                payloads.append(payload)
                return responses.pop(0)

            provider._post = post
            first = provider.complete(
                ProviderRequest("stable", "dynamic", [{"role": "user", "content": "inspect"}], "model", tools=[{"name": "read_file", "input_schema": {"type": "object"}}])
            )
            self.assertEqual(first.text, "")
            self.assertEqual(first.raw["tool_call_deltas"][0]["id"], "call_1")
            self.assertIsNotNone(first.responses_continuation)
            self.assertEqual(first.responses_continuation.response_id, "resp_1")
            self.assertEqual(first.responses_continuation.replay_items[-2]["type"], "reasoning")

            second = provider.complete(
                ProviderRequest(
                    "stable",
                    "dynamic",
                    [],
                    "model",
                    response_format=ResponseFormat("result", {"type": "object"}),
                    responses_continuation=first.responses_continuation,
                    function_call_outputs=[FunctionCallOutput("call_1", '{"content":"x"}')],
                )
            )
            self.assertEqual(second.text, '{"ok":true}')
            self.assertEqual(payloads[1]["previous_response_id"], "resp_1")
            self.assertEqual(
                payloads[1]["input"],
                [{"type": "function_call_output", "call_id": "call_1", "output": '{"content":"x"}'}],
            )
            self.assertEqual(payloads[1]["text"]["format"]["name"], "result")

    def test_compatible_responses_continuation_replays_typed_items(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            provider = OpenAIProvider(self._config(Path(temporary), name="openai-compatible", api_mode="responses"))
            continuation = ResponsesContinuation(
                "compatible-id",
                [
                    {"role": "user", "content": "inspect"},
                    {"type": "reasoning", "encrypted_content": "opaque"},
                    {"type": "function_call", "call_id": "call_7", "name": "read_file", "arguments": "{}"},
                ],
            )
            payload = provider._payload(
                ProviderRequest(
                    "stable",
                    "dynamic",
                    [],
                    "model",
                    responses_continuation=continuation,
                    function_call_outputs=[FunctionCallOutput("call_7", "result")],
                ),
                "responses",
                stream=True,
            )
            self.assertNotIn("previous_response_id", payload)
            self.assertEqual(payload["input"][:-1], continuation.replay_items)
            self.assertEqual(payload["input"][-1], {"type": "function_call_output", "call_id": "call_7", "output": "result"})
            with self.assertRaisesRegex(HarnessError, "require continuation"):
                provider._payload(
                    ProviderRequest("stable", "dynamic", [], "model", function_call_outputs=[FunctionCallOutput("x", "y")]),
                    "responses",
                    stream=False,
                )

    def test_streamed_responses_continuation_survives_collector(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            provider = OpenAIProvider(self._config(Path(temporary)))
            payloads = []
            terminal_responses = [
                {
                    "id": "resp_stream_1",
                    "status": "completed",
                    "output": [
                        {"type": "reasoning", "encrypted_content": "opaque"},
                        {"type": "function_call", "call_id": "call_stream", "name": "read_file", "arguments": "{}"},
                    ],
                },
                {
                    "id": "resp_stream_2",
                    "status": "completed",
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": '{"ok":true}'}]}],
                },
            ]

            def frames(_url, payload, _headers=None, _timeout=None):
                payloads.append(payload)
                yield "data: " + json.dumps({"type": "response.completed", "response": terminal_responses.pop(0)})

            provider._stream_lines = frames
            first = collect_stream(
                provider,
                ProviderRequest("stable", "dynamic", [{"role": "user", "content": "inspect"}], "model", tools=[{"name": "read_file", "input_schema": {"type": "object"}}]),
            )
            self.assertEqual(first.raw["tool_call_deltas"][0]["id"], "call_stream")
            self.assertEqual(first.responses_continuation.response_id, "resp_stream_1")
            second = collect_stream(
                provider,
                ProviderRequest(
                    "stable",
                    "dynamic",
                    [],
                    "model",
                    response_format=ResponseFormat("result", {"type": "object"}),
                    responses_continuation=first.responses_continuation,
                    function_call_outputs=[FunctionCallOutput("call_stream", "result")],
                ),
            )
            self.assertEqual(second.text, '{"ok":true}')
            self.assertEqual(payloads[1]["previous_response_id"], "resp_stream_1")

    def test_forced_chat_continues_with_native_tool_messages_and_strict_final_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            provider = OpenAIProvider(self._config(Path(temporary), api_mode="chat-completions"))
            payloads = []
            rounds = [
                [
                    {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_chat", "function": {"name": "read_file", "arguments": '{"path":'}}]}, "finish_reason": None}]},
                    {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"a.py"}'}}]}, "finish_reason": "tool_calls"}]},
                ],
                [
                    {"choices": [{"delta": {"content": '{"ok":true}'}, "finish_reason": "stop"}]},
                ],
            ]

            def frames(_url, payload, _headers=None, _timeout=None):
                payloads.append(payload)
                for frame in rounds.pop(0):
                    yield "data: " + json.dumps(frame)
                yield "data: [DONE]"

            provider._stream_lines = frames
            response_format = ResponseFormat("result", {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]})
            first = collect_stream(
                provider,
                ProviderRequest(
                    "stable",
                    "dynamic",
                    [{"role": "user", "content": "inspect"}],
                    "model",
                    tools=[{"name": "read_file", "input_schema": {"type": "object"}}],
                    response_format=response_format,
                ),
            )
            self.assertEqual(first.raw["tool_call_deltas"][0]["id"], "call_chat")
            self.assertIsNotNone(first.chat_continuation)
            second = collect_stream(
                provider,
                ProviderRequest(
                    "stable",
                    "dynamic",
                    [],
                    "model",
                    tools=[{"name": "read_file", "input_schema": {"type": "object"}}],
                    response_format=response_format,
                    chat_continuation=first.chat_continuation,
                    chat_function_call_outputs=[FunctionCallOutput("call_chat", '{"content":"x"}')],
                ),
            )
            self.assertEqual(second.text, '{"ok":true}')
            assistant = payloads[1]["messages"][-2]
            tool_result = payloads[1]["messages"][-1]
            self.assertEqual(assistant["role"], "assistant")
            self.assertEqual(assistant["tool_calls"][0]["id"], "call_chat")
            self.assertEqual(assistant["tool_calls"][0]["function"]["arguments"], '{"path":"a.py"}')
            self.assertEqual(tool_result, {"role": "tool", "tool_call_id": "call_chat", "content": '{"content":"x"}'})
            self.assertEqual(payloads[1]["response_format"]["json_schema"]["name"], "result")


class GraphTests(unittest.TestCase):
    @staticmethod
    def bypass_graph() -> dict:
        return {
            "schema_version": 1,
            "name": "bypass-route",
            "entry": "start",
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "planner", "type": "planner"},
                {"id": "coder", "type": "coder"},
                {"id": "syntax", "type": "tool", "config": {"role": "syntax"}},
                {"id": "unit", "type": "tool", "config": {"role": "unit_test"}},
                {"id": "review", "type": "evaluator"},
                {"id": "end", "type": "end"},
            ],
            "edges": [
                {"source": "start", "target": "planner"},
                {"source": "planner", "target": "coder", "condition": "plan_ready == true"},
                {"id": "unsafe-shortcut", "source": "coder", "target": "end"},
                {"source": "coder", "target": "syntax"},
                {"source": "syntax", "target": "unit"},
                {"source": "unit", "target": "review"},
                {"source": "review", "target": "end"},
            ],
        }

    def test_production_validation_rejects_a_success_route_around_required_stages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            with self.assertRaisesRegex(HarnessError, "bypasses required"):
                resolve_graph_execution_policy(load_config(root), self.bypass_graph())

    def test_production_validation_rejects_review_bypass_after_checks(self) -> None:
        graph = self.bypass_graph()
        graph["edges"] = [edge for edge in graph["edges"] if edge.get("id") != "unsafe-shortcut"]
        graph["edges"].insert(-1, {"id": "skip-review", "source": "unit", "target": "end"})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            with self.assertRaisesRegex(HarnessError, "bypasses required review"):
                resolve_graph_execution_policy(load_config(root), graph)

    def test_production_validation_rejects_explicit_failure_to_end_edge(self) -> None:
        graph = self.bypass_graph()
        graph["edges"] = [edge for edge in graph["edges"] if edge.get("id") != "unsafe-shortcut"]
        graph["edges"][-2] = {
            "id": "failed-unit-completes",
            "source": "unit",
            "target": "end",
            "condition": "stage_passed == false",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            with self.assertRaisesRegex(HarnessError, "routes failure state to an end node"):
                resolve_graph_execution_policy(load_config(root), graph)

    def test_cli_graph_validation_includes_production_invariants(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            graph_file = root / "graph.json"
            graph_file.write_text(json.dumps(self.bypass_graph()), encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(["--project", str(root), "graph", "validate", str(graph_file)])
            self.assertEqual(exit_code, 1)
            self.assertFalse(json.loads(output.getvalue())["valid"])

    def test_declared_edge_order_and_variable_transfer(self) -> None:
        graph = {
            "entry": "start",
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "first", "type": "end"},
                {"id": "second", "type": "end"},
            ],
            "edges": [
                {"id": "preferred", "source": "start", "target": "first", "condition": "choice == 1", "variables": ["payload.value"]},
                {"id": "fallback", "source": "start", "target": "second", "variables": []},
            ],
        }
        runtime = ProductionGraphInterpreter(graph)
        state = {"choice": 1, "payload": {"value": "typed"}}
        transition = runtime.advance(state)
        self.assertEqual(transition["edge"], "preferred")
        self.assertEqual(transition["target"], "first")
        self.assertEqual(transition["variables"], {"payload.value": "typed"})
        self.assertEqual(state["edge_inputs"], {"payload.value": "typed"})

    def test_production_loop_decay_limit_and_timeout(self) -> None:
        graph = {
            "entry": "start",
            "nodes": [{"id": "start", "type": "start"}],
            "edges": [
                {
                    "id": "again",
                    "source": "start",
                    "target": "start",
                    "loop": {"max_iterations": 3, "temperature_decay": 0.5, "timeout_seconds": 2},
                }
            ],
        }
        runtime = ProductionGraphInterpreter(graph)
        state = {"temperature": 0.2}
        runtime.advance(state, now=10.0)
        self.assertEqual(state["temperature"], 0.1)
        with self.assertRaisesRegex(HarnessError, "timeout"):
            runtime.advance(state, now=13.0)

    def test_gauntlet_fails_once_then_completes(self) -> None:
        graph = json.loads(files("our_harness.templates").joinpath("gauntlet.json").read_text(encoding="utf-8"))
        self.assertEqual(validate_graph(graph), [])
        result = simulate_graph(graph, {"test_failures_remaining": 1, "temperature": 0.2})
        self.assertTrue(result["complete"])
        coder_visits = [item for item in result["transitions"] if item.get("node") == "coder"]
        self.assertEqual(len(coder_visits), 2)
        self.assertLess(result["state"]["temperature"], 0.2)

    def test_expanded_gauntlet_is_a_valid_built_in_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            (root / ".harness" / "config.json").write_text(
                json.dumps({"workflow": {"name": "gauntlet"}}), encoding="utf-8"
            )
            graph = built_in_workflow_graph(load_config(root))
        self.assertEqual(graph["name"], "The Gauntlet Loop")
        self.assertEqual(validate_graph(graph), [])

    def test_cycle_without_limit_is_invalid(self) -> None:
        graph = {
            "entry": "a",
            "nodes": [{"id": "a", "type": "coder"}, {"id": "b", "type": "tool"}],
            "edges": [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}],
        }
        issues = validate_graph(graph)
        self.assertTrue(any("max_iterations" in issue.message for issue in issues))

    def test_unsupported_graph_schema_is_invalid(self) -> None:
        graph = json.loads(files("our_harness.templates").joinpath("gauntlet.json").read_text(encoding="utf-8"))
        graph["schema_version"] = 3
        self.assertTrue(any(issue.path == "schema_version" for issue in validate_graph(graph)))


class FakeProvider:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def complete(self, _request):
        raise AssertionError("Workflow must use the streaming provider boundary")

    def stream(self, request):
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("Unexpected provider call")
        text = json.dumps(self.responses.pop(0))
        midpoint = len(text) // 2
        yield {"type": "text_delta", "text": text[:midpoint]}
        yield {"type": "text_delta", "text": text[midpoint:]}
        yield {"type": "done", "finish_reason": "stop"}


class MutatingReviewProvider(FakeProvider):
    def __init__(self, responses, path: Path):
        super().__init__(responses)
        self.path = path

    def stream(self, request):
        yield from super().stream(request)
        if not self.responses:
            self.path.write_text("USER = 3\n", encoding="utf-8")


class WorkflowTests(unittest.TestCase):
    def test_compact_witness_only_repair_follows_two_full_evidence_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            source = "VALUE = 1\n"
            (root / "value.py").write_text(source, encoding="utf-8")
            task = "Set value.py VALUE to 2 and keep the public value name."
            plan = {
                "summary": "Set value",
                "requirement_ledger": [
                    {"id": "R1", "requirement": "value is 2", "category": "behavior", "counterexample": "value() should return 2"},
                    {"id": "R2", "requirement": "keep public name", "category": "compatibility", "counterexample": "public_value_name()"},
                ],
                "non_goals": [], "files": ["value.py"], "verification_commands": [], "risks": [],
            }
            change = {"path": "value.py", "baseline_sha256": hashlib.sha256(source.encode()).hexdigest(), "content": "VALUE = 2\n", "delete": False, "reason": "requested value"}
            invalid = {
                "summary": "Set value", "changes": [change], "commands": [],
                "review": {"verdict": "SKIP", "findings": [witness("value.py")]}, "memory": [],
            }
            repaired = {
                "requirement_witnesses": [
                    {"requirement_id": "R1", "file": "value.py", "code_path": "VALUE assignment", "counterexample_result": "R1 observes VALUE equal to 2"},
                    {"requirement_id": "R2", "file": "value.py", "code_path": "public VALUE binding", "counterexample_result": "R2 observes the same public name"},
                ]
            }
            provider = FakeProvider([plan, invalid, invalid, repaired])
            with HarnessApplication(load_config(root)) as app:
                app.provider = provider
                result = app.run_task(task, dry_run=True)
            self.assertEqual(len(provider.requests), 4)
            self.assertEqual(provider.requests[-1].response_format.name, "harness_witness_repair_wire_v2")
            self.assertEqual(len(result["proposal"]["requirement_witnesses"]), 2)
            self.assertIn("Do not propose or change source code", provider.requests[-1].messages[0]["content"])

    def test_isolated_counterexamples_block_wrong_behavior_without_mutating_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            source = "def merge_intervals(items):\n    return list(items)\n"
            target = root / "intervals.py"
            target.write_text(source, encoding="utf-8")
            plan = {
                "files": ["intervals.py"],
                "requirement_ledger": [
                    {"id": "R1", "requirement": "return sorted merged lists", "source_quote": "task", "category": "behavior", "counterexample": "Input: [(4, 1), (3, 7)] should return [[1, 7]]"},
                ],
            }
            before = hashlib.sha256(target.read_bytes()).hexdigest()
            with HarnessApplication(load_config(root)) as app:
                result = app._counterexample_verification(plan, None)
            self.assertFalse(result["passed"])
            self.assertTrue(result["results"][0]["executed"])
            self.assertFalse(result["results"][0]["passed"])
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), before)
            self.assertFalse(any(root.glob(".counterexample-sandbox-*")))

    def test_one_executable_counterexample_can_cover_two_requirement_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            (root / ".harness" / "config.json").write_text(
                json.dumps({"workflow": {"require_executable_counterexamples": True}}), encoding="utf-8"
            )
            (root / "value.py").write_text("def value():\n    return 2\n", encoding="utf-8")
            shared = "value() should return 2"
            plan = {
                "files": ["value.py"],
                "requirement_ledger": [
                    {"id": "R1", "requirement": "value is two", "source_quote": "task", "category": "behavior", "counterexample": shared},
                    {"id": "R2", "requirement": "value stays numeric", "source_quote": "task", "category": "compatibility", "counterexample": shared},
                ],
            }
            with HarnessApplication(load_config(root)) as app:
                result = app._counterexample_verification(plan, None)
            self.assertTrue(result["passed"])
            self.assertEqual([item["requirement_id"] for item in result["results"]], ["R1", "R2"])
            self.assertTrue(all(item["executed"] for item in result["results"]))

    def test_unsupported_counterexample_fails_closed_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            (root / ".harness" / "config.json").write_text(
                json.dumps({"workflow": {"require_executable_counterexamples": True}}), encoding="utf-8"
            )
            (root / "value.py").write_text("def value():\n    return 2\n", encoding="utf-8")
            plan = {
                "files": ["value.py"],
                "requirement_ledger": [
                    {"id": "R1", "requirement": "value is two", "source_quote": "task", "category": "behavior", "counterexample": "run arbitrary shell text"},
                ],
            }
            with HarnessApplication(load_config(root)) as app:
                result = app._counterexample_verification(plan, None)
            self.assertFalse(result["passed"])
            self.assertEqual(result["results"], [])
            self.assertEqual(result["issues"][0]["requirement_id"], "R1")

    def test_unsupported_counterexample_is_reported_without_blocking_compatibility_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            (root / "value.py").write_text("def value():\n    return 2\n", encoding="utf-8")
            plan = {
                "files": ["value.py"],
                "requirement_ledger": [
                    {"id": "R1", "requirement": "value is two", "source_quote": "task", "category": "behavior", "counterexample": "value is wrong"},
                ],
            }
            with HarnessApplication(load_config(root)) as app:
                result = app._counterexample_verification(plan, None)
            self.assertTrue(result["passed"])
            self.assertEqual(result["executable_coverage"], "unsupported")
            self.assertEqual(result["results"], [])

    def test_planner_semantic_contract_gets_one_actionable_correction_round(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            source = "VALUE = 1\n"
            (root / "value.py").write_text(source, encoding="utf-8")
            task = "Set value.py VALUE to 2."
            invalid_plan = {
                "summary": "Set value",
                "requirement_ledger": [requirement("value is 2", "a paraphrase that is absent")],
                "non_goals": [],
                "files": ["../value.py"],
                "verification_commands": [],
                "risks": [],
            }
            valid_plan = {
                **invalid_plan,
                "requirement_ledger": [requirement("value is 2", "Set value.py VALUE to 2")],
                "files": ["value.py"],
            }
            candidate = {
                "summary": "Set value",
                "changes": [{
                    "path": "value.py",
                    "baseline_sha256": hashlib.sha256(source.encode()).hexdigest(),
                    "content": "VALUE = 2\n",
                    "delete": False,
                    "reason": "requested value",
                }],
                "commands": [],
                "review": {"verdict": "SKIP", "findings": [witness("value.py")]},
                "memory": [],
            }
            provider = FakeProvider([invalid_plan, valid_plan, candidate])
            with HarnessApplication(load_config(root)) as app:
                app.provider = provider
                app.run_task(task, dry_run=True)
            self.assertEqual(len(provider.requests), 3)
            correction = provider.requests[1].messages[0]["content"]
            self.assertIn("SEMANTIC CONTRACT CORRECTION", correction)
            self.assertIn("escapes the project", correction)
            self.assertIn("confined project-relative file paths", correction)

    def test_coder_semantic_contract_gets_one_actionable_correction_round(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            source = "VALUE = 1\n"
            (root / "value.py").write_text(source, encoding="utf-8")
            task = "Set value.py VALUE to 2."
            plan = {
                "summary": "Set value",
                "requirement_ledger": [requirement("value is 2", "Set value.py VALUE to 2")],
                "non_goals": [],
                "files": ["value.py"],
                "verification_commands": [],
                "risks": [],
            }
            change = {
                "path": "value.py",
                "baseline_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "content": "VALUE = 2\n",
                "delete": False,
                "reason": "requested value",
            }
            invalid = {
                "summary": "Set value",
                "changes": [change],
                "commands": [],
                "review": {"verdict": "SKIP", "findings": [witness("other.py")]},
                "memory": [],
            }
            corrected = {
                **invalid,
                "review": {"verdict": "SKIP", "findings": [witness("value.py")]},
            }
            provider = FakeProvider([plan, invalid, corrected])
            with HarnessApplication(load_config(root)) as app:
                app.provider = provider
                app.run_task(task, dry_run=True)
            self.assertEqual(len(provider.requests), 3)
            correction = provider.requests[2].messages[0]["content"]
            self.assertIn("SEMANTIC CONTRACT CORRECTION", correction)
            self.assertIn("must name a planner-approved file", correction)
            self.assertIn("only requirement_id, file, code_path", correction)

    def test_small_workspace_ablation_uses_direct_task_complete_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            (root / ".harness" / "config.local.json").write_text(
                json.dumps({"provider": {"role_output_caps": {"planner": 768, "coder": 768}}}),
                encoding="utf-8",
            )
            source = "def accepts(value):\n    return True\n"
            (root / "guard.py").write_text(source, encoding="utf-8")
            task = "Update guard.py so accepts rejects booleans and null values while accepting positive integers."
            plan = {
                "summary": "Cover every named category",
                "requirement_ledger": [
                    {"id": "R1", "requirement": "reject booleans", "category": "input", "counterexample": "value=True is accepted"},
                    {"id": "R2", "requirement": "reject null values", "category": "input", "counterexample": "value=None is accepted"},
                    {"id": "R3", "requirement": "accept positive integers", "category": "input", "counterexample": "value=2 is rejected"},
                ],
                "non_goals": [],
                "files": ["guard.py"],
                "verification_commands": [],
                "risks": [],
            }
            candidate = {
                "summary": "Implement explicit guards",
                "changes": [
                    {
                        "path": "guard.py",
                        "baseline_sha256": hashlib.sha256(source.encode()).hexdigest(),
                        "content": "def accepts(value):\n    return isinstance(value, int) and not isinstance(value, bool) and value > 0\n",
                        "delete": False,
                        "reason": "cover named inputs",
                    }
                ],
                "commands": [],
                "review": {"verdict": "SKIP", "findings": [
                    {"requirement_id": f"R{index}", "file": "guard.py", "code_path": f"input guard R{index}", "counterexample_result": f"Counterexample R{index} has the expected result"}
                    for index in range(1, 4)
                ]},
                "memory": [],
            }
            provider = FakeProvider([plan, candidate])
            with HarnessApplication(load_config(root)) as app:
                app.provider = provider
                result = app.run_task(task, dry_run=True)
            self.assertTrue(result["context"]["workspace_coverage"]["complete"])
            self.assertEqual(result["agent_tools"]["calls"], 0)
            self.assertEqual(
                [request.response_format.name for request in provider.requests],
                ["harness_planner_wire_v3", "harness_coder_wire_v3"],
            )
            self.assertEqual([request.max_output_tokens for request in provider.requests], [768, 768])
            self.assertIn("\nPROJECT ROOT: .\n", provider.requests[0].dynamic_context)
            self.assertNotIn(str(root), provider.requests[0].dynamic_context)
            planner_prompt = provider.requests[0].messages[0]["content"]
            coder_prompt = provider.requests[1].messages[0]["content"]
            self.assertIn("one ledger row for every explicit behavior", planner_prompt)
            self.assertIn("TASK\n" + task, planner_prompt)
            self.assertIn("TASK\n" + task, coder_prompt)
            self.assertIn("reject booleans", coder_prompt)

    def test_incomplete_workspace_ablation_repairs_malformed_tool_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            for index in range(14):
                (root / f"module_{index:02d}.py").write_text(f"VALUE_{index} = {index}\n", encoding="utf-8")
            source = "VALUE = 1\n"
            (root / "value.py").write_text(source, encoding="utf-8")
            malformed = {
                "action": "tool",
                "tool": {"call_id": "inspect", "name": "search_workspace", "arguments": {"query": "VALUE"}},
                "explanation": "extra wrapper field",
            }
            plan = {
                "summary": "Set value",
                "requirement_ledger": [requirement("value is 2", "Set")],
                "non_goals": [],
                "files": ["value.py"],
                "verification_commands": [],
                "risks": [],
            }
            candidate = {
                "summary": "Set value",
                "changes": [
                    {
                        "path": "value.py",
                        "baseline_sha256": hashlib.sha256(source.encode()).hexdigest(),
                        "content": "VALUE = 2\n",
                        "delete": False,
                        "reason": "requested value",
                    }
                ],
                "commands": [],
                "review": {"verdict": "SKIP", "findings": [witness("value.py")]},
                "memory": [],
            }
            provider = FakeProvider([malformed, plan, candidate])
            with HarnessApplication(load_config(root)) as app:
                app.provider = provider
                result = app.run_task("Set value.py VALUE to 2.", dry_run=True)
            self.assertFalse(result["context"]["workspace_coverage"]["complete"])
            self.assertEqual(result["agent_tools"]["calls"], 0)
            self.assertEqual(len(provider.requests), 3)
            correction_prompt = provider.requests[1].messages[0]["content"]
            self.assertIn("Tool action envelope must contain only action and tool", correction_prompt)
            self.assertIn("invalid_output_excerpt", correction_prompt)
            self.assertIn("Use exactly action and tool", correction_prompt)

    def test_false_entry_condition_rejects_before_provider_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            graph = {
                "schema_version": 1,
                "name": "false-entry",
                "entry": "start",
                "nodes": [
                    {"id": "start", "type": "start"},
                    {"id": "planner", "type": "planner"},
                    {"id": "coder", "type": "coder"},
                    {"id": "syntax", "type": "tool", "config": {"role": "syntax"}},
                    {"id": "unit", "type": "tool", "config": {"role": "unit_test"}},
                    {"id": "review", "type": "evaluator"},
                    {"id": "end", "type": "end"},
                ],
                "edges": [
                    {"source": "start", "target": "planner", "condition": "enabled == true"},
                    {"source": "planner", "target": "coder"},
                    {"source": "coder", "target": "syntax"},
                    {"source": "syntax", "target": "unit"},
                    {"source": "unit", "target": "review"},
                    {"source": "review", "target": "end"},
                ],
            }
            with HarnessApplication(load_config(root)) as app:
                app.provider = FakeProvider([])
                with self.assertRaisesRegex(HarnessError, "unknown production state"):
                    app.run_task("Do not leave the entry node", graph=graph)

    def test_deadline_reaches_provider_and_command_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            command = [sys.executable, "-c", "print('ok')"]
            (root / ".harness" / "config.local.json").write_text(json.dumps({"project": {"test_commands": [command]}}), encoding="utf-8")
            provider = FakeProvider([{"ok": True}])
            seen_requests = []
            original_stream = provider.stream

            def capture(request):
                seen_requests.append(request)
                yield from original_stream(request)

            provider.stream = capture
            seen_timeouts = []

            class CapturingRunner:
                def run(self, argv, cwd=".", timeout=None):
                    seen_timeouts.append(timeout)
                    return CommandResult(argv, ".", 0, "ok", "", 1)

            with HarnessApplication(load_config(root)) as app:
                app.provider = provider
                app.runner = CapturingRunner()
                compiled = SimpleNamespace(prefix="prefix", dynamic="dynamic")
                deadline = WorkflowDeadline.start(5)
                self.assertEqual(app._request(compiled, "prompt", deadline=deadline), {"ok": True})
                app.test(check_kinds=("test",), deadline=deadline)
                with self.assertRaisesRegex(HarnessError, "deadline expired"):
                    app._request(compiled, "late", deadline=WorkflowDeadline(time.monotonic() - 1))
            self.assertGreater(seen_requests[0].timeout_seconds, 0)
            self.assertLessEqual(seen_requests[0].timeout_seconds, 5)
            self.assertGreater(seen_timeouts[0], 0)
            self.assertLessEqual(seen_timeouts[0], 5)

    def test_advisory_review_passes_and_blocker_does_not(self) -> None:
        self.assertTrue(ReviewVerdict("PASS", [{"severity": "advisory"}]).passed)
        self.assertFalse(ReviewVerdict("PASS", [{"severity": "blocker"}]).passed)
        self.assertFalse(ReviewVerdict("PASS", [{"severity": "unknown"}]).passed)

    def test_index_task_and_context_embeddings_share_workflow_budget(self) -> None:
        class SlowEmbeddingProvider:
            def __init__(self) -> None:
                self.timeouts: list[float] = []

            def embed(self, texts, timeout_seconds=None):
                self.timeouts.append(timeout_seconds)
                work_seconds = 0.35
                if timeout_seconds < work_seconds:
                    time.sleep(timeout_seconds)
                    raise HarnessError("fixture embedding timed out")
                time.sleep(work_seconds)
                return [[1.0, 0.0] for _ in texts]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            (root / "value.py").write_text("VALUE = 1\n", encoding="utf-8")
            (root / ".harness" / "config.local.json").write_text(
                json.dumps(
                    {
                        "provider": {"name": "anthropic"},
                        "memory": {"embedding_provider": "ollama", "embedding_model": "fixture"},
                        "workflow": {"max_elapsed_seconds": 1},
                    }
                ),
                encoding="utf-8",
            )
            provider = SlowEmbeddingProvider()
            started = time.monotonic()
            with patch("our_harness.providers.create_embedding_provider", return_value=provider), patch(
                "our_harness.workflow.create_embedding_provider", return_value=provider
            ), HarnessApplication(load_config(root)) as app, self.assertRaisesRegex(HarnessError, "deadline expired"):
                app.run_task("Keep the value unchanged")
            elapsed = time.monotonic() - started
            self.assertEqual(len(provider.timeouts), 3)
            self.assertGreater(provider.timeouts[0], provider.timeouts[1])
            self.assertGreater(provider.timeouts[1], provider.timeouts[2])
            self.assertLess(elapsed, 1.25)

    def test_episode_embedding_uses_memory_provider(self) -> None:
        class FixtureEmbedder:
            def embed(self, texts):
                self.texts = texts
                return [[0.25, 0.75]]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            (root / ".harness" / "config.local.json").write_text(
                json.dumps(
                    {
                        "provider": {"name": "anthropic"},
                        "memory": {"embedding_provider": "ollama", "embedding_model": "fixture"},
                    }
                ),
                encoding="utf-8",
            )
            app = HarnessApplication.__new__(HarnessApplication)
            app.config = load_config(root)
            app.embedding_provider = None
            fixture = FixtureEmbedder()
            with patch("our_harness.workflow.create_embedding_provider", return_value=fixture) as factory:
                self.assertEqual(app._embedding("episode body"), [0.25, 0.75])
                self.assertEqual(app._embedding("second body"), [0.25, 0.75])
            factory.assert_called_once_with(app.config)
            self.assertEqual(fixture.texts, ["second body"])

    def test_requests_use_total_context_fit_and_streaming_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            (root / ".harness" / "config.json").write_text(
                json.dumps({"context": {"max_chars": 6000, "reserve_chars": 1000}}), encoding="utf-8"
            )
            provider = FakeProvider([{"ok": True}])
            seen = []
            original_stream = provider.stream

            def capture(request):
                seen.append(request)
                yield from original_stream(request)

            provider.stream = capture
            with HarnessApplication(load_config(root)) as app:
                app.provider = provider
                compiled = SimpleNamespace(prefix="P" * 1200, dynamic="D" * 5000)
                self.assertEqual(app._request(compiled, "Q" * 5000), {"ok": True})
                with self.assertRaisesRegex(HarnessError, "Exact provider evidence packet"):
                    app._request(compiled, "E" * 5000, require_full_prompt=True)
            request = seen[0]
            total = len(request.system_prefix) + len(request.dynamic_context) + len(request.messages[0]["content"])
            self.assertLessEqual(total, 5000)
            self.assertLess(len(request.dynamic_context), 5000)
            self.assertLess(len(request.messages[0]["content"]), 5000)

    def test_request_rejects_output_that_violates_its_response_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            with HarnessApplication(load_config(root)) as app:
                app.provider = FakeProvider([{"ok": "not-a-boolean"}] * 3)
                schema = ResponseFormat(
                    "fixture",
                    {
                        "type": "object",
                        "properties": {"ok": {"type": "boolean"}},
                        "required": ["ok"],
                        "additionalProperties": False,
                    },
                )
                with self.assertRaisesRegex(HarnessError, "response.ok"):
                    app._request(SimpleNamespace(prefix="stable", dynamic=""), "answer", response_format=schema)
                self.assertEqual(len(app.provider.requests), 3)

    def test_failure_repair_review_and_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='1'\n", encoding="utf-8")
            (root / ".harness").mkdir()
            check = [sys.executable, "-c", "from pathlib import Path; assert Path('value.py').read_text() == 'VALUE = 2\\n'"]
            (root / ".harness" / "config.local.json").write_text(
                json.dumps({"project": {"test_commands": [check]}, "workflow": {"max_iterations": 3}}), encoding="utf-8"
            )
            bad = "VALUE = 1\n"
            good = "VALUE = 2\n"
            provider = FakeProvider([
                {"summary": "Set value", "requirement_ledger": [requirement("value is 2", "Set")], "non_goals": [], "files": ["value.py"], "verification_commands": [], "risks": []},
                {"summary": "First attempt", "changes": [{"path": "value.py", "baseline_sha256": None, "content": bad, "delete": False, "reason": "set value"}], "commands": [], "review": {"verdict": "SKIP", "findings": [witness("value.py")]}, "memory": []},
                {"summary": "Repair", "changes": [{"path": "value.py", "baseline_sha256": hashlib.sha256(bad.encode()).hexdigest(), "content": good, "delete": False, "reason": "match expected value"}], "commands": [], "review": {"verdict": "SKIP", "findings": [witness("value.py")]}, "memory": []},
                {"verdict": "PASS", "findings": [], "residual_risks": []},
            ])
            config = load_config(root)
            events = []
            with HarnessApplication(config, events.append) as app:
                app.provider = provider
                result = app.run_task("Set value.py to the tested value")
                failures = app.memory.search_episodes("tested value", namespace="failure")
                successes = app.memory.search_episodes("tested value", namespace="success")
                review_row = app.memory.connection.execute("SELECT patch_sha256,packet_json FROM review_packets").fetchone()
                review_packet = json.loads(review_row["packet_json"])
            self.assertEqual(result["state"], "complete")
            self.assertEqual(result["iterations"], 2)
            self.assertEqual(
                [request.response_format.name for request in provider.requests],
                ["harness_planner_wire_v3", "harness_coder_wire_v3", "harness_repair_wire_v3", "harness_reviewer_v1"],
            )
            self.assertEqual(result["provider_usage"]["requests"], 4)
            self.assertTrue(all(request.prompt_cache_key.startswith("our-harness:") for request in provider.requests))
            self.assertEqual((root / "value.py").read_text(encoding="utf-8"), good)
            self.assertTrue(failures)
            self.assertTrue(successes)
            self.assertTrue(any(event["kind"] == "failure" for event in events))
            self.assertIn("--- /dev/null", review_packet["patch"]["text"])
            self.assertEqual(review_row["patch_sha256"], review_packet["patch"]["sha256"])
            packet_id = review_packet.pop("packet_id")
            self.assertEqual(
                packet_id,
                hashlib.sha256(json.dumps(review_packet, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            )

    def test_scope_command_policy_and_plugin_workflow_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='1'\n", encoding="utf-8")
            (root / ".harness").mkdir()
            plugin = root / "plugin_fixture.py"
            plugin.write_text(
                "from our_harness.models import Detection\n"
                "class Plugin:\n"
                "    name = 'plugin_fixture'\n"
                "    def register(self, registry):\n"
                "        registry.add_detector(lambda root: Detection('custom', ['plugin'], [], [], [], 1.0))\n"
                "        registry.add_workflow_node('plugin-flow', lambda config: {'max_iterations': 2, 'include_build': True, 'require_review': False})\n"
                "def plugin(): return Plugin()\n",
                encoding="utf-8",
            )
            check = [sys.executable, "-c", "print('ok')"]
            (root / ".harness" / "config.json").write_text(
                "{}",
                encoding="utf-8",
            )
            (root / ".harness" / "config.local.json").write_text(
                json.dumps({
                    "project": {"test_commands": [check]},
                    "workflow": {"name": "plugin-flow"},
                    "plugins": {"enabled": ["plugin_fixture"], "paths": ["plugin_fixture.py"]},
                }),
                encoding="utf-8",
            )
            with HarnessApplication(load_config(root)) as app:
                self.assertEqual(app.workflow_policy.max_iterations, 2)
                self.assertTrue(app.workflow_policy.include_build)
                self.assertFalse(app.workflow_policy.require_review)
                self.assertNotIn("evaluator", {node["type"] for node in app.workflow_graph["nodes"]})
                self.assertIn("custom", [item.stack for item in app._detections()])
                with self.assertRaisesRegex(HarnessError, "planner-approved"):
                    app._apply_candidate(
                        {"changes": [{"path": "outside.py", "baseline_sha256": None, "content": "x", "delete": False}]},
                        {"approved.py"},
                    )
                approved = app._approved_verification_commands(
                    {"verification_commands": [check]}, {"commands": [check]}, [Detection("fixture", [], [check])]
                )
                self.assertEqual(approved, [check])
                self.assertEqual(
                    app._approved_verification_commands(
                        {"verification_commands": [["unexpected", "command"]]}, {"commands": []}, []
                    ),
                    [check],
                )

    def test_unapproved_model_command_is_ignored_while_automatic_check_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            source = "VALUE = 1\n"
            target = root / "value.py"
            target.write_text(source, encoding="utf-8")
            baseline_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
            automatic = [
                sys.executable,
                "-c",
                "from pathlib import Path; assert Path('value.py').read_text() == 'VALUE = 2\\n'; print('automatic check ran')",
            ]
            (root / ".harness" / "config.local.json").write_text(
                json.dumps({"project": {"test_commands": [automatic]}}),
                encoding="utf-8",
            )
            plan = {
                "summary": "Set value",
                "requirement_ledger": [requirement("value is 2", "Set")],
                "non_goals": [],
                "files": ["value.py"],
                "verification_commands": [],
                "risks": [],
            }
            candidate = {
                "summary": "Set value",
                "changes": [
                    {
                        "path": "value.py",
                        "baseline_sha256": baseline_sha256,
                        "content": "VALUE = 2\n",
                        "delete": False,
                        "reason": "requested value",
                    }
                ],
                "commands": [["git add value.py"]],
                "review": {"verdict": "SKIP", "findings": [witness("value.py")]},
                "memory": [],
            }
            provider = FakeProvider([plan, candidate, {"verdict": "PASS", "findings": [], "residual_risks": []}])
            with HarnessApplication(load_config(root)) as app:
                app.provider = provider
                result = app.run_task("Set value.py VALUE to 2")
            self.assertEqual(result["state"], "complete")
            self.assertEqual(target.read_text(encoding="utf-8"), "VALUE = 2\n")
            self.assertIn(automatic, result["verification"]["commands"])
            self.assertNotIn(["git add value.py"], result["verification"]["commands"])
            self.assertTrue(
                any(stage.get("kind") == "counterexample" for stage in result["verification"]["stages"])
            )
            self.assertIn("automatic check ran", result["verification"]["results"][0]["stdout"])

    def test_review_verdict_is_invalidated_when_file_changes_during_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='1'\n", encoding="utf-8")
            (root / ".harness").mkdir()
            check = [sys.executable, "-c", "from pathlib import Path; assert Path('value.py').read_text() == 'VALUE = 2\\n'"]
            (root / ".harness" / "config.local.json").write_text(json.dumps({"project": {"test_commands": [check]}}), encoding="utf-8")
            target = root / "value.py"
            provider = MutatingReviewProvider(
                [
                    {"summary": "Set value", "requirement_ledger": [requirement("value is 2", "Set")], "non_goals": [], "files": ["value.py"], "verification_commands": [], "risks": []},
                    {"summary": "Apply", "changes": [{"path": "value.py", "baseline_sha256": None, "content": "VALUE = 2\n", "delete": False, "reason": "set value"}], "commands": [], "review": {"verdict": "SKIP", "findings": [witness("value.py")]}, "memory": []},
                    {"verdict": "PASS", "findings": [], "residual_risks": []},
                ],
                target,
            )
            with HarnessApplication(load_config(root)) as app:
                app.provider = provider
                with self.assertRaisesRegex(HarnessError, "changed after verification packet"):
                    app.run_task("Set the tested value")
            self.assertEqual(target.read_text(encoding="utf-8"), "USER = 3\n")

    def test_submitted_graph_controls_real_check_order_and_attempt_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='1'\n", encoding="utf-8")
            (root / ".harness").mkdir()
            lint = [sys.executable, "-c", "print('lint')"]
            security = [sys.executable, "-c", "print('security')"]
            performance = [sys.executable, "-c", "print('performance')"]
            test = [sys.executable, "-c", "raise SystemExit(1)"]
            (root / ".harness" / "config.local.json").write_text(
                json.dumps(
                    {
                        "project": {
                            "test_commands": [test],
                            "lint_commands": [lint],
                            "security_commands": [security],
                            "performance_commands": [performance],
                        }
                    }
                ),
                encoding="utf-8",
            )
            graph = json.loads(files("our_harness.templates").joinpath("gauntlet.json").read_text(encoding="utf-8"))
            loop = next(edge["loop"] for edge in graph["edges"] if edge.get("loop") and edge["source"] == "unit")
            loop.update({"max_iterations": 1, "temperature_decay": 0.5, "timeout_seconds": 9})
            provider = FakeProvider(
                [
                    {"summary": "Create", "requirement_ledger": [requirement("checked", "Exercise")], "non_goals": [], "files": ["value.py"], "verification_commands": [], "risks": []},
                    {"summary": "Create", "changes": [{"path": "value.py", "baseline_sha256": None, "content": "VALUE = 1\n", "delete": False, "reason": "create value"}], "commands": [], "review": {"verdict": "SKIP", "findings": [witness("value.py")]}, "memory": []},
                ]
            )
            events = []
            with HarnessApplication(load_config(root), events.append) as app:
                app.provider = provider
                with self.assertRaisesRegex(HarnessError, "after 1 attempts"):
                    app.run_task("Exercise the submitted graph", graph=graph)
                graph_version = app.memory.connection.execute("SELECT graph_version FROM runs").fetchone()[0]
            verification_events = [event for event in events if event["kind"] == "verification"]
            self.assertEqual([event["node"] for event in verification_events], ["syntax", "security", "performance", "unit"])
            self.assertEqual(
                [command for event in verification_events for command in event["payload"]["commands"]],
                [lint, security, performance, test],
            )
            self.assertEqual(len(graph_version), 64)
            self.assertEqual(provider.responses, [])
            self.assertFalse((root / "value.py").exists())

    def test_configured_review_panel_controls_production_verdict_and_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='1'\n", encoding="utf-8")
            (root / ".harness").mkdir()
            check = [sys.executable, "-c", "from pathlib import Path; assert Path('value.py').is_file()"]
            (root / ".harness" / "config.local.json").write_text(
                json.dumps(
                    {
                        "project": {"test_commands": [check]},
                        "workflow": {
                            "reviewers": 2,
                            "review_parallelism": 2,
                            "reviewer_lenses": ["correctness", "counterexample"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            provider = FakeProvider(
                [
                    {"summary": "Create", "requirement_ledger": [requirement("file exists", "Create")], "non_goals": [], "files": ["value.py"], "verification_commands": [], "risks": []},
                    {"summary": "Create", "changes": [{"path": "value.py", "baseline_sha256": None, "content": "VALUE = 1\n", "delete": False, "reason": "create"}], "commands": [], "review": {"verdict": "SKIP", "findings": [witness("value.py")]}, "memory": []},
                ]
            )

            class FakePanel:
                packets = []

                def __init__(self, config):
                    self.config = config

                def review(self, packet, *, deadline_at):
                    self.packets.append(packet)
                    packet_json = json.dumps(packet, sort_keys=True, separators=(",", ":"))
                    reviews = [
                        SimpleNamespace(
                            reviewer_id=f"reviewer-{index:02d}",
                            lens=lens,
                            status="passed",
                            verdict="PASS",
                            findings=[],
                            residual_risks=[],
                            usage={"input_tokens": 10, "output_tokens": 2, "cached_input_tokens": 5, "cache_write_input_tokens": 0},
                            latency_ms=7,
                            error=None,
                        )
                        for index, lens in enumerate(("correctness", "counterexample"), 1)
                    ]
                    return SimpleNamespace(
                        verdict="PASS",
                        findings=[],
                        residual_risks=[],
                        reviews=reviews,
                        packet_sha256=hashlib.sha256(packet_json.encode()).hexdigest(),
                    )

            with patch("our_harness.workflow.ReviewPanel", FakePanel):
                with HarnessApplication(load_config(root)) as app:
                    app.provider = provider
                    result = app.run_task("Create value.py")
                    verdict = json.loads(app.memory.connection.execute("SELECT verdict_json FROM review_packets").fetchone()[0])
            self.assertEqual(result["state"], "complete")
            self.assertEqual(result["provider_usage"]["requests"], 4)
            self.assertEqual([item["lens"] for item in verdict["reviews"]], ["correctness", "counterexample"])
            self.assertEqual(len(FakePanel.packets), 1)

    def test_end_node_fails_closed_and_rolls_back_failed_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            passed = [sys.executable, "-c", "print('ok')"]
            failed = [sys.executable, "-c", "raise SystemExit(1)"]
            (root / ".harness" / "config.local.json").write_text(
                json.dumps(
                    {
                        "project": {"lint_commands": [passed], "test_commands": [failed]},
                        "workflow": {"require_review": False},
                    }
                ),
                encoding="utf-8",
            )
            graph = {
                "schema_version": 1,
                "name": "fail-closed-end",
                "entry": "start",
                "nodes": [
                    {"id": "start", "type": "start"},
                    {"id": "planner", "type": "planner"},
                    {"id": "coder", "type": "coder"},
                    {"id": "syntax", "type": "tool", "config": {"role": "syntax"}},
                    {"id": "unit", "type": "tool", "config": {"role": "unit_test"}},
                    {"id": "end", "type": "end"},
                ],
                "edges": [
                    {"source": "start", "target": "planner"},
                    {"source": "planner", "target": "coder"},
                    {"source": "coder", "target": "syntax"},
                    {"source": "syntax", "target": "unit"},
                    {"source": "unit", "target": "end"},
                ],
            }
            provider = FakeProvider(
                [
                    {"summary": "Create", "requirement_ledger": [requirement("checked", "Do not complete")], "non_goals": [], "files": ["value.py"], "verification_commands": [], "risks": []},
                    {"summary": "Create", "changes": [{"path": "value.py", "baseline_sha256": None, "content": "VALUE = 1\n", "delete": False, "reason": "create value"}], "commands": [], "review": {"verdict": "SKIP", "findings": [witness("value.py")]}, "memory": []},
                ]
            )
            with HarnessApplication(load_config(root)) as app:
                app.provider = provider
                with self.assertRaisesRegex(HarnessError, "cannot complete without passing required verification"):
                    app.run_task("Do not complete after the unit failure", graph=graph)
                run = app.memory.connection.execute("SELECT state,result_json FROM runs").fetchone()
            self.assertEqual(run["state"], "failed")
            self.assertTrue(json.loads(run["result_json"])["rolled_back"])
            self.assertFalse((root / "value.py").exists())


if __name__ == "__main__":
    unittest.main()
