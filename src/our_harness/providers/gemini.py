from __future__ import annotations

import json
from typing import Any

from ..models import FunctionCallOutput, HarnessError, NativeToolContinuation, ProviderRequest, ProviderResponse
from .base import Provider


class GeminiProvider(Provider):
    """Gemini Interactions API adapter with native function continuation."""

    def _url(self) -> str:
        endpoint = str(self.settings["endpoint"]).rstrip("/")
        return endpoint if endpoint.endswith("/interactions") else endpoint + "/interactions"

    @staticmethod
    def _tools(request: ProviderRequest) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for tool in request.tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not isinstance(tool.get("input_schema"), dict):
                raise HarnessError("Gemini tool definition is malformed")
            tools.append(
                {
                    "type": "function",
                    "name": tool["name"],
                    "description": str(tool.get("description") or ""),
                    "parameters": tool["input_schema"],
                }
            )
        return tools

    @staticmethod
    def _initial_input(request: ProviderRequest) -> list[dict[str, Any]]:
        steps: list[dict[str, Any]] = []
        for message in request.messages:
            if not isinstance(message, dict) or not isinstance(message.get("content"), str):
                raise HarnessError("Gemini messages must contain string content")
            role = message.get("role")
            content = [{"type": "text", "text": message["content"]}]
            if role == "assistant":
                steps.append({"type": "model_output", "content": content})
            elif role == "user":
                steps.append({"type": "user_input", "content": content})
            else:
                raise HarnessError("Gemini messages support user and assistant roles")
        return steps

    def _continuation_input(self, request: ProviderRequest) -> tuple[list[dict[str, Any]], str | None]:
        continuation = request.native_continuation
        outputs = request.native_function_call_outputs
        if continuation is None:
            if outputs:
                raise HarnessError("Gemini function results require continuation state")
            return self._initial_input(request), None
        if continuation.provider != "gemini":
            raise HarnessError("Gemini received continuation state from another provider")
        if continuation.state.get("model") != request.model:
            raise HarnessError("Gemini continuation model does not match the request")
        if continuation.state.get("endpoint") != str(self.settings["endpoint"]).rstrip("/"):
            raise HarnessError("Gemini continuation endpoint does not match this provider profile")
        if not outputs:
            raise HarnessError("Gemini continuation requires function results")
        pending = set(continuation.pending_call_ids)
        seen: set[str] = set()
        names = continuation.state.get("call_names", {})
        if not isinstance(names, dict):
            raise HarnessError("Gemini continuation call names are invalid")
        results: list[dict[str, Any]] = []
        for output in outputs:
            if not isinstance(output, FunctionCallOutput) or output.call_id not in pending or output.call_id in seen:
                raise HarnessError("Gemini function results must match unique pending call IDs")
            seen.add(output.call_id)
            results.append(
                {
                    "type": "function_result",
                    "call_id": output.call_id,
                    "name": str(names.get(output.call_id) or ""),
                    "result": [{"type": "text", "text": output.output}],
                }
            )
        if seen != pending:
            raise HarnessError("Gemini continuation requires one result for every pending call")
        interaction_id = continuation.state.get("interaction_id")
        if not isinstance(interaction_id, str) or not interaction_id:
            raise HarnessError("Gemini continuation interaction ID is invalid")
        return results, interaction_id

    def _payload(self, request: ProviderRequest) -> dict[str, Any]:
        input_steps, previous = self._continuation_input(request)
        payload: dict[str, Any] = {
            "model": request.model,
            "input": input_steps,
            "system_instruction": f"{request.system_prefix}\n\n{request.dynamic_context}",
            "generation_config": {
                "max_output_tokens": request.max_output_tokens,
            },
        }
        if previous:
            payload["previous_interaction_id"] = previous
        if request.tools:
            payload["tools"] = self._tools(request)
        if request.response_format is not None:
            payload["response_format"] = {
                "type": "text",
                "mime_type": "application/json",
                "schema": request.response_format.schema,
            }
        return payload

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        key = self._api_key()
        data = self._post(
            self._url(),
            self._payload(request),
            {"x-goog-api-key": key} if key else {},
            request.timeout_seconds,
        )
        status = str(data.get("status") or "")
        if status not in {"completed", "requires_action"}:
            error = data.get("error")
            raise HarnessError(f"Gemini interaction stopped with status {status or 'unknown'}: {error or 'no details'}")
        steps = data.get("steps", [])
        if not isinstance(steps, list):
            raise HarnessError("Gemini interaction steps must be an array")
        text_parts: list[str] = []
        calls: list[dict[str, Any]] = []
        call_names: dict[str, str] = {}
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                raise HarnessError("Gemini interaction step must be an object")
            if step.get("type") == "model_output":
                content = step.get("content", [])
                if not isinstance(content, list):
                    raise HarnessError("Gemini model output content must be an array")
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                        text_parts.append(part["text"])
            elif step.get("type") == "function_call":
                call_id = step.get("id")
                name = step.get("name")
                arguments = step.get("arguments", {})
                if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name or not isinstance(arguments, dict):
                    raise HarnessError("Gemini function call is malformed")
                calls.append({"index": index, "id": call_id, "function": {"name": name, "arguments": arguments}})
                call_names[call_id] = name
        if status == "requires_action" and not calls:
            raise HarnessError("Gemini requires action but returned no function call")
        if status == "completed" and not text_parts:
            raise HarnessError("Gemini completed with no output text")
        interaction_id = data.get("id")
        continuation = None
        if calls:
            if not isinstance(interaction_id, str) or not interaction_id:
                raise HarnessError("Gemini function calls require an interaction ID")
            continuation = NativeToolContinuation(
                "gemini",
                {
                    "interaction_id": interaction_id,
                    "call_names": call_names,
                    "model": request.model,
                    "endpoint": str(self.settings["endpoint"]).rstrip("/"),
                },
                [call["id"] for call in calls],
            )
        usage = data.get("usage", {})
        if not isinstance(usage, dict):
            usage = {}
        output_tokens = usage.get("total_output_tokens")
        reasoning_tokens = usage.get("total_thought_tokens")
        billed_output = None
        if output_tokens is not None or reasoning_tokens is not None:
            billed_output = int(output_tokens or 0) + int(reasoning_tokens or 0)
        return ProviderResponse(
            text="".join(text_parts),
            finish_reason=status,
            input_tokens=usage.get("total_input_tokens"),
            output_tokens=output_tokens,
            cached_input_tokens=usage.get("total_cached_tokens"),
            reasoning_tokens=reasoning_tokens,
            tool_use_tokens=usage.get("total_tool_use_tokens"),
            billed_output_tokens=billed_output,
            raw={**data, "tool_call_deltas": calls},
            native_continuation=continuation,
        )

    def embed(self, texts: list[str], timeout_seconds: float | None = None) -> list[list[float]]:
        endpoint = str(self.settings["endpoint"]).rstrip("/")
        model = str(self.config.get("memory.embedding_model") or self.settings["model"])
        key = self._api_key()
        data = self._post(
            f"{endpoint}/models/{model}:batchEmbedContents",
            {
                "requests": [
                    {"model": f"models/{model}", "content": {"parts": [{"text": text}]}}
                    for text in texts
                ]
            },
            {"x-goog-api-key": key} if key else {},
            timeout_seconds,
        )
        embeddings = data.get("embeddings", [])
        if not isinstance(embeddings, list):
            raise HarnessError("Gemini embedding response is malformed")
        return [list(map(float, item.get("values", []))) for item in embeddings if isinstance(item, dict)]
