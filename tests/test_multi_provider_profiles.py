from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from our_harness.agent_tools import AgentToolSession
from our_harness.config import HarnessError, load_config, load_isolated_config
from our_harness.models import FunctionCallOutput, ProviderRequest, ProviderResponse, ResponseFormat
from our_harness.providers import ProviderRegistry
from our_harness.providers.base import AnthropicProvider, OllamaProvider, collect_stream
from our_harness.providers.gemini import GeminiProvider
from our_harness.usage import PriceCatalog
from our_harness.workflow import HarnessApplication, WorkflowDeadline


TOOL = {
    "name": "read_file",
    "description": "Read a file",
    "input_schema": {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    },
}


def request(**changes: object) -> ProviderRequest:
    values: dict[str, object] = {
        "system_prefix": "fixed",
        "dynamic_context": "current",
        "messages": [{"role": "user", "content": "inspect"}],
        "model": "fixture-model",
        "max_output_tokens": 200,
        "tools": [TOOL],
    }
    values.update(changes)
    return ProviderRequest(**values)  # type: ignore[arg-type]


class ProviderProfileTests(unittest.TestCase):
    def test_documented_multi_provider_sample_matches_runtime_and_schema_contract(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        text = (repository / "docs" / "CONFIGURATION.md").read_text(encoding="utf-8")
        section = text.split("### Provider profiles and agents", 1)[1]
        match = re.search(r"```json\s+(.*?)\s+```", section, re.DOTALL)
        self.assertIsNotNone(match)
        sample = json.loads(match.group(1))  # type: ignore[union-attr]
        schema = json.loads((repository / "harness.schema.json").read_text(encoding="utf-8"))
        profile_keys = set(schema["$defs"]["providerProfile"]["properties"])
        agent_keys = set(schema["$defs"]["agentSpec"]["properties"])
        executable_capabilities = {"workspace.read", "workspace.write", "shell.execute", "git.commit", "git.push"}
        for profile in sample["providers"].values():
            self.assertTrue(profile["allow_project_graphs"])
            self.assertFalse(set(profile) - profile_keys)
        for agent in sample["agents"].values():
            self.assertFalse(set(agent) - agent_keys)
            self.assertLessEqual(set(agent["capabilities"]), executable_capabilities)
        with tempfile.TemporaryDirectory() as temporary:
            loaded = load_isolated_config(Path(temporary), sample)
        self.assertEqual(set(loaded.get("providers")), {"planner_api", "coder_local", "review_api"})

    def test_legacy_config_resolves_as_default_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = load_isolated_config(
                Path(temporary),
                {"provider": {"name": "ollama", "model": "qwen", "endpoint": "http://127.0.0.1:11434"}},
            )
            registry = ProviderRegistry(config)
            self.assertEqual(registry.profile().name, "ollama")
            self.assertEqual(registry.profile().model, "qwen")

    def test_agent_route_applies_only_agent_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = load_isolated_config(
                Path(temporary),
                {
                    "providers": {
                        "reasoner": {
                            "kind": "anthropic",
                            "model": "claude-fixture",
                            "endpoint": "https://api.anthropic.com/v1",
                            "api_key_env": "ANTHROPIC_TEAM_KEY",
                            "max_concurrency": 3,
                            "role_output_caps": {"planner": 768, "coder": 1024},
                            "prompt_cache_retention": "in_memory",
                        }
                    },
                    "agents": {
                        "planner": {
                            "provider_ref": "reasoner",
                            "role": "planner",
                            "model": "claude-agent-fixture",
                            "temperature": 0.1,
                            "max_output_tokens": 1234,
                            "capabilities": ["file.read"],
                        }
                    },
                },
            )
            registry = ProviderRegistry(config)
            routed = registry.agent_config("planner")
            self.assertEqual(routed.get("provider.model"), "claude-agent-fixture")
            self.assertEqual(routed.get("provider.temperature"), 0.1)
            self.assertEqual(routed.get("provider.max_output_tokens"), 1234)
            self.assertEqual(routed.get("provider.role_output_caps"), {"planner": 768, "coder": 1024})
            self.assertEqual(routed.get("provider.api_key_env"), "ANTHROPIC_TEAM_KEY")
            self.assertEqual(routed.get("provider.prompt_cache_retention"), "in_memory")

    def test_named_profile_does_not_fall_back_to_global_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = load_isolated_config(
                Path(temporary),
                {
                    "providers": {
                        "reviewer": {
                            "kind": "anthropic",
                            "model": "claude-fixture",
                            "endpoint": "https://api.anthropic.com/v1",
                            "api_key_env": "REVIEWER_ONLY_KEY",
                        }
                    }
                },
            )
            provider = ProviderRegistry(config).create("reviewer")
            with patch.dict(os.environ, {"HARNESS_API_KEY": "wrong", "REVIEWER_ONLY_KEY": "right"}, clear=False):
                self.assertEqual(provider._api_key(), "right")
            with patch.dict(os.environ, {"HARNESS_API_KEY": "wrong"}, clear=True):
                with self.assertRaisesRegex(HarnessError, "REVIEWER_ONLY_KEY"):
                    provider._api_key()

    def test_official_named_profile_requires_its_own_key_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(HarnessError, "api_key_env is required"):
                load_isolated_config(
                    Path(temporary),
                    {
                        "providers": {
                            "reviewer": {
                                "kind": "gemini",
                                "model": "gemini-fixture",
                                "endpoint": "https://generativelanguage.googleapis.com/v1beta",
                            }
                        }
                    },
                )

    def test_named_profile_rejects_url_credentials_and_wrong_cache_policy(self) -> None:
        attempts = [
            {
                "kind": "gemini",
                "model": "gemini-fixture",
                "endpoint": "https://generativelanguage.googleapis.com/v1beta?key=secret",
                "api_key_env": "GEMINI_API_KEY",
            },
            {
                "kind": "anthropic",
                "model": "claude-fixture",
                "endpoint": "https://api.anthropic.com/v1",
                "api_key_env": "ANTHROPIC_API_KEY",
                "prompt_cache_retention": "24h",
            },
        ]
        for profile in attempts:
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as temporary:
                with self.assertRaises(HarnessError):
                    load_isolated_config(Path(temporary), {"providers": {"remote": profile}})

    def test_pricing_reference_must_match_profile_provider_and_model(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(HarnessError, "does not match"):
                load_isolated_config(
                    Path(temporary),
                    {
                        "providers": {
                            "reviewer": {
                                "kind": "gemini",
                                "model": "gemini-fixture",
                                "endpoint": "https://generativelanguage.googleapis.com/v1beta",
                                "api_key_env": "GEMINI_API_KEY",
                                "pricing_ref": "wrong-provider",
                            }
                        },
                        "pricing": {
                            "snapshots": [
                                {
                                    "id": "wrong-provider",
                                    "provider": "openai",
                                    "model_pattern": "gpt-*",
                                    "input_per_million_microusd": 1,
                                    "output_per_million_microusd": 1,
                                    "effective_at": "2026-01-01",
                                    "source_url": "https://example.test/prices",
                                }
                            ]
                        },
                    },
                )

    def test_shareable_project_config_cannot_add_provider_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".harness").mkdir()
            (root / ".harness" / "config.json").write_text(
                json.dumps(
                    {
                        "providers": {
                            "remote": {
                                "kind": "gemini",
                                "model": "gemini-fixture",
                                "endpoint": "https://generativelanguage.googleapis.com/v1beta",
                                "api_key_env": "GEMINI_API_KEY",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(HarnessError, "harness trust"):
                load_config(root)


class PriceCatalogTests(unittest.TestCase):
    def config(self, root: Path, provider: str = "openai"):
        return load_isolated_config(
            root,
            {
                "pricing": {
                    "snapshots": [
                        {
                            "id": "fixture-2026-01",
                            "provider": provider,
                            "model_pattern": "fixture-*",
                            "input_per_million_microusd": 2_000_000,
                            "cached_input_per_million_microusd": 500_000,
                            "cache_write_per_million_microusd": 3_000_000,
                            "output_per_million_microusd": 8_000_000,
                            "effective_at": "2026-01-01",
                            "source_url": "https://example.test/prices",
                        }
                    ]
                }
            },
        )

    def test_openai_cost_does_not_double_bill_cached_subset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            catalog = PriceCatalog(self.config(Path(temporary)))
            snapshot = catalog.preflight("openai", "fixture-model")
            assert snapshot is not None
            response = ProviderResponse(text="ok", input_tokens=1000, cached_input_tokens=400, output_tokens=100)
            self.assertEqual(catalog.cost(response, snapshot), 2200)

    def test_anthropic_cost_bills_separate_cache_categories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            catalog = PriceCatalog(self.config(Path(temporary), "anthropic"))
            snapshot = catalog.preflight("anthropic", "fixture-model")
            assert snapshot is not None
            response = ProviderResponse(
                text="ok", input_tokens=600, cached_input_tokens=400, cache_write_input_tokens=100, output_tokens=100
            )
            self.assertEqual(catalog.cost(response, snapshot), 2500)

    def test_unknown_remote_price_fails_before_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            catalog = PriceCatalog(load_isolated_config(Path(temporary)))
            with self.assertRaisesRegex(HarnessError, "No configured price snapshot"):
                catalog.preflight("gemini", "unlisted")
            self.assertIsNone(catalog.preflight("ollama", "local-model"))


class NativeToolAdapterTests(unittest.TestCase):
    def provider_config(self, root: Path, name: str):
        endpoint = {
            "anthropic": "https://api.anthropic.com/v1",
            "gemini": "https://generativelanguage.googleapis.com/v1beta",
            "ollama": "http://127.0.0.1:11434",
        }[name]
        return load_isolated_config(root, {"provider": {"name": name, "model": "fixture-model", "endpoint": endpoint}})

    def test_anthropic_tool_only_response_can_continue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            provider = AnthropicProvider(self.provider_config(Path(temporary), "anthropic"))
            replies = [
                {
                    "content": [{"type": "tool_use", "id": "call-a", "name": "read_file", "input": {"path": "a.py"}}],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 10, "output_tokens": 4},
                },
                {
                    "content": [{"type": "text", "text": "done"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 15, "output_tokens": 2},
                },
            ]
            payloads: list[dict[str, object]] = []

            def fake_post(_url, payload, _headers=None, _timeout=None):
                payloads.append(payload)
                return replies.pop(0)

            with patch.object(provider, "_post", side_effect=fake_post):
                first = provider.complete(
                    request(
                        prompt_cache_retention="in_memory",
                        response_format=ResponseFormat("answer", {"type": "object"}),
                    )
                )
                second = provider.complete(
                    request(
                        native_continuation=first.native_continuation,
                        native_function_call_outputs=[FunctionCallOutput("call-a", "source")],
                    )
                )
            self.assertEqual(first.text, "")
            self.assertEqual(second.text, "done")
            self.assertEqual(payloads[0]["system"][0]["cache_control"]["ttl"], "5m")
            self.assertEqual(payloads[0]["output_config"]["format"]["type"], "json_schema")
            result = payloads[1]["messages"][-1]["content"][0]
            self.assertEqual(result["tool_use_id"], "call-a")

    def test_anthropic_requires_an_explicit_stop_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            provider = AnthropicProvider(
                self.provider_config(Path(temporary), "anthropic"),
            )
            incomplete = {
                "content": [{"type": "text", "text": "plausible partial"}],
                "usage": {},
            }
            with patch.object(provider, "_post", return_value=incomplete), \
                    self.assertRaisesRegex(HarnessError, "stop_reason"):
                provider.complete(request())


class WorkflowProviderRoutingTests(unittest.TestCase):
    @staticmethod
    def provider_config(root: Path, name: str):
        endpoint = {
            "anthropic": "https://api.anthropic.com/v1",
            "gemini": "https://generativelanguage.googleapis.com/v1beta",
            "ollama": "http://127.0.0.1:11434",
        }[name]
        return load_isolated_config(root, {"provider": {"name": name, "model": "fixture-model", "endpoint": endpoint}})

    class RoutedProvider:
        def __init__(self, model: str):
            self.model = model

        def stream(self, provider_request):
            self.request = provider_request
            yield {"type": "text_delta", "text": json.dumps({"route": self.model})}
            yield {"type": "usage", "input_tokens": 10, "output_tokens": 2, "billed_output_tokens": 2}
            yield {"type": "done", "finish_reason": "stop"}

    @staticmethod
    def route_schema() -> ResponseFormat:
        return ResponseFormat(
            "route_fixture",
            {
                "type": "object",
                "properties": {"route": {"type": "string"}},
                "required": ["route"],
                "additionalProperties": False,
            },
        )

    def test_legacy_ollama_route_uses_native_tools_and_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "note.py").write_text("VALUE = 1\n", encoding="utf-8")
            config = self.provider_config(root, "ollama")

            class LegacyOllamaFixture:
                def __init__(self):
                    self.requests: list[ProviderRequest] = []

                def stream(self, provider_request):
                    self.requests.append(provider_request)
                    if len(self.requests) == 1:
                        yield {
                            "type": "tool_call_delta",
                            "tool_call": {
                                "index": 0,
                                "id": "read-note",
                                "function": {
                                    "name": "read_file",
                                    "arguments": {
                                        "path": "note.py",
                                        "start_line": 1,
                                        "end_line": 10,
                                        "max_bytes": 1024,
                                    },
                                },
                            },
                        }
                        yield {
                            "type": "native_state",
                            "provider": "ollama",
                            "state": {"fixture": True},
                            "pending_call_ids": ["read-note"],
                        }
                        yield {"type": "done", "finish_reason": "tool_calls"}
                        return
                    yield {"type": "text_delta", "text": '{"route":"legacy-native"}'}
                    yield {"type": "done", "finish_reason": "stop"}

            fixture = LegacyOllamaFixture()
            deadline = WorkflowDeadline.start(5)
            compiled = type(
                "Compiled",
                (),
                {"prefix": "fixed", "dynamic": "", "prefix_sha256": "a" * 64, "manifest": {}},
            )()
            with HarnessApplication(config) as app:
                app.provider = fixture
                app.agent_tool_session = AgentToolSession(config, app.memory, deadline, lambda *_: None)
                result = app._request_with_tools(
                    compiled,
                    "return route",
                    self.route_schema(),
                    "coder",
                    deadline=deadline,
                )
            self.assertEqual(result, {"route": "legacy-native"})
            self.assertTrue(fixture.requests[0].tools)
            self.assertEqual(fixture.requests[0].response_format.name, "route_fixture")
            self.assertIsNotNone(fixture.requests[1].native_continuation)
            self.assertEqual(fixture.requests[1].native_function_call_outputs[0].call_id, "read-note")

    def test_workflow_routes_three_agents_and_persists_attributed_usage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profiles = {
                role: {
                    "kind": "ollama",
                    "model": f"{role}-model",
                    "endpoint": f"http://127.0.0.1:{11434 + index}",
                    "allow_project_graphs": True,
                }
                for index, role in enumerate(("planner", "coder", "reviewer"))
            }
            agents = {
                role: {"provider_ref": role, "role": role, "capabilities": ["file.read"]}
                for role in profiles
            }
            config = load_isolated_config(root, {"providers": profiles, "agents": agents})
            created: list[str] = []

            def factory(routed):
                model = str(routed.get("provider.model"))
                created.append(model)
                return self.RoutedProvider(model)

            with HarnessApplication(config) as app, patch("our_harness.workflow.create_provider", side_effect=factory):
                run_id = app.memory.start_run("route providers")
                app._active_run_id = run_id
                app._graph_source = "submitted"
                compiled = type("Compiled", (), {"prefix": "fixed", "dynamic": "", "prefix_sha256": "a" * 64})()
                results = []
                for role in ("planner", "coder", "reviewer"):
                    node_id = f"{role}-node"
                    app._active_node = {
                        "id": node_id,
                        "type": "evaluator" if role == "reviewer" else role,
                        "config": {"agent_ref": role},
                    }
                    results.append(app._request(compiled, "route", response_format=self.route_schema(), node=node_id))
                persisted = app.memory.usage_records(run_id=run_id)["records"]
                events = [item for item in app.memory.events(run_id) if item["kind"] == "provider_usage"]

            self.assertEqual(created, ["planner-model", "coder-model", "reviewer-model"])
            self.assertEqual([item["route"] for item in results], created)
            self.assertEqual([item["node_id"] for item in persisted], ["planner-node", "coder-node", "reviewer-node"])
            self.assertEqual([item["agent_role"] for item in persisted], ["planner", "coder", "reviewer"])
            self.assertEqual([item["provider_route"] for item in persisted], ["planner", "coder", "reviewer"])
            self.assertEqual([item["payload"]["agent_id"] for item in events], ["planner", "coder", "reviewer"])

    def test_submitted_graph_route_needs_opt_in_and_priced_model_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = {
                "kind": "ollama",
                "model": "base-model",
                "endpoint": "http://127.0.0.1:11434",
                "pricing_ref": "base-price",
            }
            price = {
                "id": "base-price",
                "provider": "ollama",
                "model_pattern": "base-model",
                "input_per_million_microusd": 0,
                "output_per_million_microusd": 0,
                "effective_at": "2026-01-01",
                "source_url": "https://example.test/prices",
            }
            config = load_isolated_config(root, {"providers": {"route": base}, "pricing": {"snapshots": [price]}})
            with HarnessApplication(config) as app:
                app._graph_source = "submitted"
                app._active_node = {"id": "coder", "type": "coder", "config": {"provider_route": "route"}}
                with self.assertRaisesRegex(HarnessError, "allow_project_graphs"):
                    app._resolve_provider_route("coder")

            base["allow_project_graphs"] = True
            config = load_isolated_config(root, {"providers": {"route": base}, "pricing": {"snapshots": [price]}})
            with HarnessApplication(config) as app:
                app._graph_source = "submitted"
                app._active_node = {
                    "id": "coder",
                    "type": "coder",
                    "config": {"provider_route": "route", "model": "other-model"},
                }
                with self.assertRaisesRegex(HarnessError, "does not match pricing_ref"):
                    app._resolve_provider_route("coder")

    def test_ollama_sends_native_tools_and_tool_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            provider = OllamaProvider(self.provider_config(Path(temporary), "ollama"))
            replies = [
                {
                    "message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "a.py"}}}]},
                    "done": True,
                    "prompt_eval_count": 5,
                    "eval_count": 3,
                },
                {"message": {"role": "assistant", "content": "done"}, "done": True},
            ]
            payloads: list[dict[str, object]] = []

            def fake_post(_url, payload, **_kwargs):
                payloads.append(payload)
                return replies.pop(0)

            with patch.object(provider, "_post", side_effect=fake_post):
                first = provider.complete(request())
                second = provider.complete(
                    request(
                        native_continuation=first.native_continuation,
                        native_function_call_outputs=[FunctionCallOutput("ollama-0", "source")],
                    )
                )
            self.assertIn("tools", payloads[0])
            self.assertEqual(payloads[1]["messages"][-1], {"role": "tool", "tool_name": "read_file", "content": "source"})
            self.assertEqual(second.text, "done")

    def test_ollama_non_stream_refuses_done_false_partial_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            provider = OllamaProvider(self.provider_config(Path(temporary), "ollama"))
            incomplete = {
                "message": {"role": "assistant", "content": "partial output"},
                "done": False,
            }
            with patch.object(provider, "_post", return_value=incomplete), \
                    self.assertRaisesRegex(HarnessError, "nonterminal"):
                provider.complete(request())

    def test_ollama_stream_accumulates_tool_calls_and_thinking_before_done(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            provider = OllamaProvider(self.provider_config(Path(temporary), "ollama"))
            frames = [
                json.dumps(
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "thinking": "inspect first",
                            "tool_calls": [
                                {"function": {"name": "read_file", "arguments": {"path": "a.py"}}}
                            ],
                        },
                        "done": False,
                    }
                ),
                json.dumps(
                    {
                        "message": {"role": "assistant", "content": "", "thinking": ""},
                        "done": True,
                        "prompt_eval_count": 8,
                        "eval_count": 4,
                    }
                ),
            ]
            with patch.object(provider, "_stream_lines", return_value=iter(frames)):
                response = collect_stream(provider, request())
            self.assertEqual(response.raw["tool_call_deltas"][0]["id"], "ollama-0")
            self.assertIsNotNone(response.native_continuation)
            assistant = response.native_continuation.state["messages"][-1]
            self.assertEqual(assistant["thinking"], "inspect first")
            self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "read_file")
            followup, _ = provider._payload(
                request(
                    native_continuation=response.native_continuation,
                    native_function_call_outputs=[FunctionCallOutput("ollama-0", "source")],
                ),
                stream=True,
            )
            self.assertEqual(followup["messages"][-2], assistant)
            self.assertEqual(
                followup["messages"][-1],
                {"role": "tool", "tool_name": "read_file", "content": "source"},
            )

    def test_stream_reader_uses_available_chunks_instead_of_waiting_for_full_buffer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            provider = OllamaProvider(self.provider_config(Path(temporary), "ollama"))

            class ChunkedResponse:
                def __init__(self):
                    self.chunks = iter([b'{"message":{"content":"ok"},"done":true}\n', b""])

                def read1(self, _size):
                    return next(self.chunks)

                def read(self, _size):
                    raise AssertionError("stream reader must not wait for a full read buffer")

                def close(self):
                    return None

            class FixtureOpener:
                def open(self, _request, timeout=None):
                    return ChunkedResponse()

            provider._http_opener = FixtureOpener()
            lines = list(provider._stream_lines("http://127.0.0.1/api/chat", {}, timeout_seconds=1))
            self.assertEqual(lines, ['{"message":{"content":"ok"},"done":true}'])

    def test_gemini_tracks_thinking_and_continues_by_interaction_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            provider = GeminiProvider(self.provider_config(Path(temporary), "gemini"))
            replies = [
                {
                    "id": "ix-1",
                    "status": "requires_action",
                    "steps": [{"type": "function_call", "id": "call-g", "name": "read_file", "arguments": {"path": "a.py"}}],
                    "usage": {"total_input_tokens": 10, "total_output_tokens": 2, "total_thought_tokens": 5, "total_tool_use_tokens": 1},
                },
                {"id": "ix-2", "status": "completed", "steps": [{"type": "model_output", "content": [{"type": "text", "text": "done"}]}]},
            ]
            payloads: list[dict[str, object]] = []

            def fake_post(_url, payload, _headers=None, _timeout=None):
                payloads.append(payload)
                return replies.pop(0)

            with patch.object(provider, "_post", side_effect=fake_post):
                first = provider.complete(request(response_format=ResponseFormat("answer", {"type": "object"})))
                second = provider.complete(
                    request(
                        native_continuation=first.native_continuation,
                        native_function_call_outputs=[FunctionCallOutput("call-g", "source")],
                    )
                )
            self.assertEqual(first.billed_output_tokens, 7)
            self.assertEqual(
                payloads[0]["response_format"],
                {"type": "text", "mime_type": "application/json", "schema": {"type": "object"}},
            )
            self.assertEqual(payloads[1]["previous_interaction_id"], "ix-1")
            self.assertEqual(payloads[1]["input"][0]["type"], "function_result")
            self.assertEqual(second.text, "done")


if __name__ == "__main__":
    unittest.main()
