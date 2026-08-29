from __future__ import annotations

import codecs
import copy
import http.client
import json
import os
import queue
import socket
import threading
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any

from .. import cancellation
from ..config import LoadedConfig, validate_embedding_provider_route
from ..execution import CommandRunner
from ..models import (
    ChatCompletionsContinuation,
    FunctionCallOutput,
    HarnessError,
    NativeToolContinuation,
    ProviderRequest,
    ProviderResponse,
    ResponsesContinuation,
)
from ..redaction import CredentialRedactor, bounded_redacted_text


# Provider payload transport is separate from local command/test output. The
# public answer contract permits eight million characters; a JSON encoder can
# represent each non-BMP character as a twelve-byte surrogate pair.
MAX_PROVIDER_RESPONSE_BYTES = 100_000_000


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep provider credentials bound to the configured HTTP endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise HarnessError("Provider HTTP redirects are not accepted")


def _interrupt_http_response(response: Any) -> None:
    """Best-effort interruption for a read blocked below urllib's framing layer."""
    stream = getattr(response, "fp", None)
    raw = getattr(stream, "raw", None)
    connection = getattr(raw, "_sock", None)
    if connection is not None:
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            connection.close()
        except OSError:
            pass
    try:
        response.close()
    except OSError:
        pass


class Provider(ABC):
    def __init__(self, config: LoadedConfig):
        self.config = config
        self.settings = config.data["provider"]
        # urllib copies Authorization and other credential headers to redirect
        # requests. A private opener that refuses redirects keeps complete,
        # stream, and embedding traffic on the reviewed endpoint.
        self._http_opener = urllib.request.build_opener(_RejectRedirectHandler())
        self._redactor = CredentialRedactor(config)

    @abstractmethod
    def complete(self, request: ProviderRequest) -> ProviderResponse: ...

    def stream(self, request: ProviderRequest) -> Iterator[dict[str, Any]]:
        response = self.complete(request)
        if response.text:
            yield {"type": "text_delta", "text": response.text}
        for call in response.raw.get("tool_call_deltas", []):
            yield {"type": "tool_call_delta", "tool_call": call}
        if any(
            value is not None
            for value in (
                response.input_tokens,
                response.output_tokens,
                response.cached_input_tokens,
                response.cache_write_input_tokens,
            )
        ):
            yield {
                "type": "usage",
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "cached_input_tokens": response.cached_input_tokens,
                "cache_write_input_tokens": response.cache_write_input_tokens,
                "reasoning_tokens": response.reasoning_tokens,
                "tool_use_tokens": response.tool_use_tokens,
                "billed_output_tokens": response.billed_output_tokens,
            }
        if response.native_continuation is not None:
            yield {
                "type": "native_state",
                "provider": response.native_continuation.provider,
                "state": response.native_continuation.state,
                "pending_call_ids": response.native_continuation.pending_call_ids,
            }
        yield {"type": "done", "finish_reason": response.finish_reason}

    def embed(self, texts: list[str], timeout_seconds: float | None = None) -> list[list[float]]:
        raise HarnessError(f"Provider {self.settings['name']} does not support embeddings")

    def _timeout(self, requested: float | None = None) -> float:
        configured = float(self.settings["timeout_seconds"])
        return max(0.000001, min(configured, float(requested))) if requested is not None else configured

    def _api_key(self) -> str:
        # HARNESS_API_KEY is retained only for a migrated single-provider
        # configuration. Each named profile binds its own environment variable.
        direct = os.environ.get("HARNESS_API_KEY", "") if not self.config.get("providers") else ""
        if direct:
            return direct
        variable = str(self.settings.get("api_key_env") or "")
        if not variable:
            return ""
        value = os.environ.get(variable, "")
        if not value:
            raise HarnessError(f"Required API key environment variable is not set: {variable}")
        return value

    def _post(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        safe_payload = self._redactor.value(payload)
        request = urllib.request.Request(
            url,
            data=json.dumps(safe_payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        response_holder: dict[str, Any] = {}
        unregister_cancel = cancellation.register(
            lambda: _interrupt_http_response(response_holder.get("response"))
            if response_holder.get("response") is not None else None
        )
        try:
            cancellation.checkpoint()
            with self._http_opener.open(request, timeout=self._timeout(timeout_seconds)) as response:
                response_holder["response"] = response
                cancellation.checkpoint()
                response_limit = max(
                    MAX_PROVIDER_RESPONSE_BYTES,
                    int(self.config.get("execution.max_output_bytes")),
                )
                raw = response.read(response_limit + 1)
                if len(raw) > response_limit:
                    raise HarnessError(
                        f"Provider response exceeded its {response_limit:,}-byte transport limit"
                    )
        except urllib.error.HTTPError as exc:
            try:
                error_raw = exc.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
                body = error_raw.decode("utf-8", errors="replace")
                if len(error_raw) > MAX_PROVIDER_RESPONSE_BYTES:
                    body += (
                        f" [Provider error body exceeded the disclosed "
                        f"{MAX_PROVIDER_RESPONSE_BYTES:,}-byte transport limit; "
                        "Nexus did not treat the captured prefix as complete.]"
                    )
            finally:
                exc.close()
            cancellation.checkpoint()
            raise HarnessError(
                f"Provider HTTP {exc.code}: "
                + bounded_redacted_text(self._redactor, body, 65_536)
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            cancellation.checkpoint()
            raise HarnessError(
                f"Provider request failed: {self._redactor.text(str(exc))}"
            ) from exc
        except (ValueError, http.client.HTTPException) as exc:
            # An address the machine cannot even take apart - a name and
            # password written into it, a port that is not a number. Left to
            # itself this is not the kind of failure anything above catches, so
            # it went all the way out with whatever was in the address.
            raise HarnessError(
                f"Provider request failed: {self._redactor.text(str(exc))}"
            ) from exc
        finally:
            unregister_cancel()
        cancellation.checkpoint()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HarnessError("Provider returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise HarnessError("Provider response must be a JSON object")
        return value

    def _stream_lines(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> Iterator[str]:
        safe_payload = self._redactor.value(payload)
        request = urllib.request.Request(
            url,
            data=json.dumps(safe_payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        decoder = StreamDecoder()
        consumed = 0
        timeout = self._timeout(timeout_seconds)
        deadline_at = time.monotonic() + timeout
        stopped = threading.Event()
        received: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=8)
        response_lock = threading.Lock()
        response_holder: dict[str, Any] = {}

        def cancel_stream() -> None:
            stopped.set()
            with response_lock:
                response = response_holder.get("response")
            if response is not None:
                _interrupt_http_response(response)

        unregister_cancel = cancellation.register(cancel_stream)

        def offer(kind: str, value: object) -> None:
            while not stopped.is_set():
                try:
                    received.put((kind, value), timeout=0.05)
                    return
                except queue.Full:
                    continue

        def read_response() -> None:
            response: Any = None
            try:
                response = self._http_opener.open(request, timeout=timeout)
                with response_lock:
                    response_holder["response"] = response
                if stopped.is_set():
                    _interrupt_http_response(response)
                    return
                read_chunk = getattr(response, "read1", response.read)
                while not stopped.is_set():
                    chunk = read_chunk(65_536)
                    if not chunk:
                        offer("eof", None)
                        return
                    offer("chunk", chunk)
            except Exception as exc:
                offer("error", exc)

        try:
            reader = threading.Thread(target=read_response, name="harness-http-stream-reader", daemon=True)
            reader.start()
            while True:
                cancellation.checkpoint()
                remaining = deadline_at - time.monotonic()
                if remaining <= 0:
                    raise HarnessError(
                        f"Provider stream timed out at its {timeout:.3f}s wall-clock deadline"
                    )
                try:
                    kind, value = received.get(timeout=min(0.05, remaining))
                except queue.Empty:
                    continue
                if kind == "chunk":
                    chunk = value
                    if not isinstance(chunk, bytes):
                        raise HarnessError("Provider stream reader returned a non-byte chunk")
                    consumed += len(chunk)
                    response_limit = max(
                        MAX_PROVIDER_RESPONSE_BYTES,
                        int(self.config.get("execution.max_output_bytes")),
                    )
                    if consumed > response_limit:
                        raise HarnessError(
                            f"Provider stream exceeded its {response_limit:,}-byte transport limit"
                        )
                    yield from decoder.feed(chunk)
                    continue
                if kind == "error":
                    if isinstance(value, Exception):
                        raise value
                    raise HarnessError("Provider stream reader failed")
                if kind != "eof":
                    raise HarnessError("Provider stream reader returned an unknown event")
                yield from decoder.feed(b"", final=True)
                break
        except urllib.error.HTTPError as exc:
            try:
                error_raw = exc.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
                body = error_raw.decode("utf-8", errors="replace")
                if len(error_raw) > MAX_PROVIDER_RESPONSE_BYTES:
                    body += (
                        f" [Provider error body exceeded the disclosed "
                        f"{MAX_PROVIDER_RESPONSE_BYTES:,}-byte transport limit; "
                        "Nexus did not treat the captured prefix as complete.]"
                    )
            finally:
                _interrupt_http_response(exc)
            cancellation.checkpoint()
            raise HarnessError(
                f"Provider HTTP {exc.code}: "
                + bounded_redacted_text(self._redactor, body, 65_536)
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError, UnicodeDecodeError) as exc:
            cancellation.checkpoint()
            raise HarnessError(f"Provider stream failed: {exc}") from exc
        finally:
            unregister_cancel()
            stopped.set()
            with response_lock:
                response = response_holder.get("response")
            if response is not None:
                _interrupt_http_response(response)
            reader.join(timeout=0.25)


def message_list(request: ProviderRequest) -> list[dict[str, Any]]:
    system = f"{request.system_prefix}\n\n{request.dynamic_context}"
    return [{"role": "system", "content": system}, *request.messages]


def request_images(request: ProviderRequest) -> list[dict[str, str]]:
    """Validated inline images selected by the user for this request."""

    found: list[dict[str, str]] = []
    for one in request.attachments:
        if not isinstance(one, dict):
            continue
        mime = str(one.get("type") or "")
        data = str(one.get("data") or "")
        if mime.startswith("image/") and data:
            found.append({"type": mime, "data": data, "name": str(one.get("name") or "image")})
    return found


def _openai_usage(usage: object) -> dict[str, int | None]:
    value = usage if isinstance(usage, dict) else {}
    input_details = value.get("input_tokens_details") or value.get("prompt_tokens_details") or {}
    output_details = value.get("output_tokens_details") or value.get("completion_tokens_details") or {}
    if not isinstance(input_details, dict):
        input_details = {}
    if not isinstance(output_details, dict):
        output_details = {}
    output_tokens = value.get("output_tokens", value.get("completion_tokens"))
    return {
        "input_tokens": value.get("input_tokens", value.get("prompt_tokens")),
        "output_tokens": output_tokens,
        "cached_input_tokens": input_details.get("cached_tokens"),
        "cache_write_input_tokens": input_details.get("cache_write_tokens"),
        "reasoning_tokens": output_details.get("reasoning_tokens"),
        "billed_output_tokens": output_tokens,
    }


def _responses_content(response: dict[str, Any]) -> tuple[str, str]:
    output = response.get("output", [])
    if not isinstance(output, list):
        raise HarnessError("OpenAI Responses output must be an array")
    text_parts: list[str] = []
    refusal_parts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content", [])
        if not isinstance(content, list):
            raise HarnessError("OpenAI Responses message content must be an array")
        for part in content:
            if not isinstance(part, dict):
                raise HarnessError("OpenAI Responses content item must be an object")
            if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                text_parts.append(part["text"])
            elif part.get("type") == "refusal" and isinstance(part.get("refusal"), str):
                refusal_parts.append(part["refusal"])
    return "".join(text_parts), "".join(refusal_parts)


def _responses_tool_calls(response: dict[str, Any]) -> list[dict[str, Any]]:
    output = response.get("output", [])
    if not isinstance(output, list):
        raise HarnessError("OpenAI Responses output must be an array")
    calls: list[dict[str, Any]] = []
    for index, item in enumerate(output):
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        name = item.get("name")
        arguments = item.get("arguments", "{}")
        call_id = item.get("call_id", item.get("id", f"response-call-{index}"))
        if not isinstance(name, str) or not name or not isinstance(arguments, (str, dict)) or not isinstance(call_id, str):
            raise HarnessError("OpenAI Responses function call is malformed")
        calls.append({"index": index, "id": call_id, "function": {"name": name, "arguments": arguments}})
    return calls


def _ensure_responses_status(response: dict[str, Any]) -> None:
    status = response.get("status")
    if status == "incomplete":
        details = response.get("incomplete_details", {})
        reason = details.get("reason", "unknown") if isinstance(details, dict) else "unknown"
        raise HarnessError(f"OpenAI Responses output is incomplete: {reason}")
    if status == "failed":
        error = response.get("error", {})
        message = error.get("message", "unknown provider failure") if isinstance(error, dict) else str(error)
        raise HarnessError(f"OpenAI Responses request failed: {message}")
    if status != "completed":
        raise HarnessError(
            "OpenAI Responses output has no explicit successful completion "
            f"status (received {status!r})"
        )


def _chat_tool_calls(fragments: object) -> list[dict[str, Any]]:
    if not isinstance(fragments, list):
        raise HarnessError("OpenAI Chat Completions tool calls must be an array")
    assembled: dict[int, dict[str, str]] = {}
    for position, fragment in enumerate(fragments):
        if not isinstance(fragment, dict):
            raise HarnessError("OpenAI Chat Completions tool call must be an object")
        index = fragment.get("index", position)
        if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index > 4095:
            raise HarnessError("OpenAI Chat Completions tool call index is invalid")
        current = assembled.setdefault(index, {"id": "", "name": "", "arguments": ""})
        call_type = fragment.get("type")
        if call_type is not None and call_type != "function":
            raise HarnessError("OpenAI Chat Completions tool call type must be function")
        call_id = fragment.get("id", "")
        if call_id:
            if not isinstance(call_id, str) or (current["id"] and current["id"] != call_id):
                raise HarnessError("OpenAI Chat Completions tool call ID is malformed")
            current["id"] = call_id
        function = fragment.get("function", {})
        if function is None:
            function = {}
        if not isinstance(function, dict):
            raise HarnessError("OpenAI Chat Completions function call is malformed")
        name = function.get("name", "")
        if name:
            if not isinstance(name, str) or (current["name"] and current["name"] != name):
                raise HarnessError("OpenAI Chat Completions function name is malformed")
            current["name"] = name
        arguments = function.get("arguments", "")
        if isinstance(arguments, dict):
            arguments = json.dumps(arguments, separators=(",", ":"), sort_keys=True)
        if not isinstance(arguments, str):
            raise HarnessError("OpenAI Chat Completions function arguments are malformed")
        current["arguments"] += arguments
    calls: list[dict[str, Any]] = []
    for index in sorted(assembled):
        current = assembled[index]
        if not current["id"] or not current["name"]:
            raise HarnessError("OpenAI Chat Completions tool call is incomplete")
        calls.append(
            {
                "index": index,
                "id": current["id"],
                "type": "function",
                "function": {"name": current["name"], "arguments": current["arguments"] or "{}"},
            }
        )
    return calls


class OpenAIProvider(Provider):
    structured_retry_is_safe = True
    @staticmethod
    def _with_images(request: ProviderRequest, messages: list[dict[str, Any]], mode: str) -> list[dict[str, Any]]:
        images = request_images(request)
        if not images:
            return copy.deepcopy(messages)
        copied = copy.deepcopy(messages)
        at = next((index for index in range(len(copied) - 1, -1, -1) if copied[index].get("role") == "user"), -1)
        if at < 0 or not isinstance(copied[at].get("content"), str):
            raise HarnessError("Image attachments require a user text message")
        text = copied[at]["content"]
        if mode == "responses":
            copied[at]["content"] = [
                {"type": "input_text", "text": text},
                *[
                    {"type": "input_image", "image_url": f"data:{one['type']};base64,{one['data']}", "detail": "auto"}
                    for one in images
                ],
            ]
        else:
            copied[at]["content"] = [
                {"type": "text", "text": text},
                *[
                    {"type": "image_url", "image_url": {"url": f"data:{one['type']};base64,{one['data']}", "detail": "auto"}}
                    for one in images
                ],
            ]
        return copied
    def _api_mode(self) -> str:
        configured = str(self.settings.get("api_mode") or "auto")
        if configured != "auto":
            return configured
        return "responses" if self.settings.get("name") == "openai" else "chat-completions"

    def _base_endpoint(self) -> str:
        endpoint = str(self.settings["endpoint"]).rstrip("/")
        for suffix in ("/chat/completions", "/responses", "/embeddings"):
            if endpoint.endswith(suffix):
                return endpoint[: -len(suffix)]
        return endpoint

    def _url(self, mode: str) -> str:
        suffix = "/responses" if mode == "responses" else "/chat/completions"
        endpoint = str(self.settings["endpoint"]).rstrip("/")
        return endpoint if endpoint.endswith(suffix) else self._base_endpoint() + suffix

    def _official_options(self, request: ProviderRequest) -> dict[str, Any]:
        if self.settings.get("name") != "openai":
            return {}
        options: dict[str, Any] = {}
        if request.prompt_cache_key:
            options["prompt_cache_key"] = request.prompt_cache_key
        if request.prompt_cache_retention:
            options["prompt_cache_options"] = {"ttl": request.prompt_cache_retention}
        return options

    def _structured_format(self, request: ProviderRequest, mode: str) -> dict[str, Any]:
        response_format = request.response_format
        supports_format = self.settings.get("name") == "openai" or mode == "responses"
        if response_format is None or not supports_format:
            return {}
        definition = {
            "name": response_format.name,
            "strict": response_format.strict,
            "schema": response_format.schema,
        }
        if mode == "responses":
            return {"text": {"format": {"type": "json_schema", **definition}}}
        return {"response_format": {"type": "json_schema", "json_schema": definition}}

    @staticmethod
    def _tools(request: ProviderRequest, mode: str) -> list[dict[str, Any]]:
        translated: list[dict[str, Any]] = []
        for tool in request.tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not isinstance(tool.get("input_schema"), dict):
                raise HarnessError("Provider tool definition is malformed")
            name = tool["name"]
            description = str(tool.get("description") or "")
            parameters = tool["input_schema"]
            if mode == "responses":
                translated.append({"type": "function", "name": name, "description": description, "parameters": parameters, "strict": True})
            else:
                translated.append(
                    {
                        "type": "function",
                        "function": {"name": name, "description": description, "parameters": parameters, "strict": True},
                    }
                )
        return translated

    def _responses_input(self, request: ProviderRequest) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
        continuation = request.responses_continuation
        outputs = request.function_call_outputs
        if continuation is None and outputs:
            raise HarnessError("Responses function outputs require continuation state")
        if continuation is not None and not outputs:
            raise HarnessError("Responses continuation requires at least one function output")
        if continuation is None:
            return self._with_images(request, request.messages, "responses"), {}, "initial"

        typed_outputs: list[dict[str, Any]] = []
        seen: set[str] = set()
        for output in outputs:
            if not isinstance(output, FunctionCallOutput) or not output.call_id or output.call_id in seen:
                raise HarnessError("Responses function outputs must have unique non-empty call IDs")
            if not isinstance(output.output, str):
                raise HarnessError("Responses function output must be a string")
            seen.add(output.call_id)
            typed_outputs.append({"type": "function_call_output", "call_id": output.call_id, "output": output.output})

        # Only the official endpoint has a stated previous_response_id contract.
        # Compatible endpoints use deterministic item replay even when they expose
        # a /responses route; this avoids assuming undeclared server-side state.
        if self.settings.get("name") == "openai" and continuation.response_id:
            return typed_outputs, {"previous_response_id": continuation.response_id}, "previous_response_id"
        if not continuation.replay_items:
            raise HarnessError("OpenAI-compatible Responses continuation needs replay items")
        return [*copy.deepcopy(continuation.replay_items), *typed_outputs], {}, "manual_replay"

    def _chat_messages(self, request: ProviderRequest) -> list[dict[str, Any]]:
        continuation = request.chat_continuation
        outputs = request.chat_function_call_outputs
        if continuation is None and outputs:
            raise HarnessError("Chat Completions function outputs require continuation state")
        if continuation is not None and not outputs:
            raise HarnessError("Chat Completions continuation requires function outputs")
        if continuation is None:
            return self._with_images(request, message_list(request), "chat")
        if self.settings.get("name") != "openai":
            raise HarnessError("Native Chat Completions continuation is available only for the official OpenAI provider")
        if not continuation.messages or not continuation.pending_call_ids:
            raise HarnessError("Chat Completions continuation is incomplete")
        output_map: dict[str, str] = {}
        for output in outputs:
            if not isinstance(output, FunctionCallOutput) or not output.call_id or output.call_id in output_map:
                raise HarnessError("Chat Completions function outputs must have unique non-empty call IDs")
            if not isinstance(output.output, str):
                raise HarnessError("Chat Completions function output must be a string")
            output_map[output.call_id] = output.output
        if set(output_map) != set(continuation.pending_call_ids):
            raise HarnessError("Chat Completions function outputs do not match the pending tool calls")
        tool_messages = [
            {"role": "tool", "tool_call_id": call_id, "content": output_map[call_id]}
            for call_id in continuation.pending_call_ids
        ]
        return [*copy.deepcopy(continuation.messages), *tool_messages]

    @staticmethod
    def _chat_continuation(
        sent_messages: list[dict[str, Any]],
        text: str,
        tool_calls: list[dict[str, Any]],
    ) -> ChatCompletionsContinuation | None:
        if not tool_calls:
            return None
        assistant_calls = [
            {
                "id": call["id"],
                "type": "function",
                "function": copy.deepcopy(call["function"]),
            }
            for call in tool_calls
        ]
        assistant = {"role": "assistant", "content": text or None, "tool_calls": assistant_calls}
        return ChatCompletionsContinuation(
            messages=[*copy.deepcopy(sent_messages), assistant],
            pending_call_ids=[call["id"] for call in tool_calls],
        )

    @staticmethod
    def _continuation(
        request: ProviderRequest,
        response: dict[str, Any],
        sent_input: list[dict[str, Any]],
        mode: str,
    ) -> ResponsesContinuation:
        response_id = response.get("id")
        if response_id is not None and (not isinstance(response_id, str) or not response_id):
            raise HarnessError("OpenAI Responses response ID must be a non-empty string")
        output = response.get("output", [])
        if not isinstance(output, list) or any(not isinstance(item, dict) for item in output):
            raise HarnessError("OpenAI Responses output must be an array of objects")
        if mode == "manual_replay":
            replay = [*sent_input, *copy.deepcopy(output)]
        elif request.responses_continuation is not None:
            prior = request.responses_continuation.replay_items
            replay = [*copy.deepcopy(prior), *sent_input, *copy.deepcopy(output)]
        else:
            replay = [*copy.deepcopy(sent_input), *copy.deepcopy(output)]
        return ResponsesContinuation(response_id=response_id, replay_items=replay)

    def _payload(self, request: ProviderRequest, mode: str, *, stream: bool) -> dict[str, Any]:
        common = {
            "model": request.model,
            "temperature": request.temperature,
            **({"tools": self._tools(request, mode)} if request.tools else {}),
            **self._official_options(request),
            **self._structured_format(request, mode),
        }
        if mode == "responses":
            if request.chat_continuation is not None or request.chat_function_call_outputs:
                raise HarnessError("Chat Completions continuation is not available in Responses mode")
            responses_input, continuation_options, _continuation_mode = self._responses_input(request)
            return {
                **common,
                "instructions": f"{request.system_prefix}\n\n{request.dynamic_context}",
                "input": responses_input,
                "max_output_tokens": request.max_output_tokens,
                "stream": stream,
                **continuation_options,
            }
        if request.responses_continuation is not None or request.function_call_outputs:
            raise HarnessError("Responses continuation is not available in Chat Completions mode")
        chat_messages = self._chat_messages(request)
        return {
            **common,
            "messages": chat_messages,
            "max_tokens": request.max_output_tokens,
            "stream": stream,
            **({"stream_options": {"include_usage": True}} if stream and self.settings.get("name") == "openai" else {}),
        }

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        mode = self._api_mode()
        key = self._api_key()
        data = self._post(
            self._url(mode),
            self._payload(request, mode, stream=False),
            {"Authorization": f"Bearer {key}"} if key else {},
            request.timeout_seconds,
        )
        if mode == "responses":
            _ensure_responses_status(data)
            text, refusal = _responses_content(data)
            tool_calls = _responses_tool_calls(data)
            if refusal:
                raise HarnessError(f"OpenAI Responses output was refused: {refusal}")
            if not text and not tool_calls:
                raise HarnessError("OpenAI Responses completed with no output text")
            sent_input, _, continuation_mode = self._responses_input(request)
            continuation = self._continuation(request, data, sent_input, continuation_mode)
            usage = _openai_usage(data.get("usage"))
            return ProviderResponse(
                text=text,
                finish_reason=str(data["status"]),
                raw={**data, "tool_call_deltas": tool_calls, "continuation_mode": continuation_mode},
                responses_continuation=continuation,
                **usage,
            )
        try:
            choice = data["choices"][0]
            message = choice["message"]
            refusal = message.get("refusal") if isinstance(message, dict) else None
            if refusal:
                raise HarnessError(f"OpenAI Chat Completions output was refused: {refusal}")
            text = message.get("content") or ""
            tool_calls = _chat_tool_calls(message.get("tool_calls", []))
        except (KeyError, IndexError, TypeError) as exc:
            raise HarnessError("OpenAI-compatible response is missing choices[0].message.content") from exc
        terminal = choice.get("finish_reason")
        if not isinstance(terminal, str) or not terminal.strip():
            raise HarnessError(
                "OpenAI Chat Completions output has no explicit finish_reason"
            )
        finish_reason = terminal.strip()
        if finish_reason in {"length", "content_filter"}:
            raise HarnessError(f"OpenAI Chat Completions output is incomplete: {finish_reason}")
        if not isinstance(text, str) or (not text and not tool_calls):
            raise HarnessError("OpenAI Chat Completions completed with no output text")
        sent_messages = self._chat_messages(request)
        continuation = self._chat_continuation(sent_messages, text, tool_calls)
        return ProviderResponse(
            text=text,
            finish_reason=finish_reason,
            raw={**data, "tool_call_deltas": tool_calls},
            chat_continuation=continuation,
            **_openai_usage(data.get("usage")),
        )

    def stream(self, request: ProviderRequest) -> Iterator[dict[str, Any]]:
        mode = self._api_mode()
        key = self._api_key()
        if mode == "responses":
            yield from self._stream_responses(request, key)
            return
        yield from self._stream_chat_completions(request, key)

    def _stream_responses(self, request: ProviderRequest, key: str) -> Iterator[dict[str, Any]]:
        saw_text = False
        refusal_parts: list[str] = []
        payload = self._payload(request, "responses", stream=True)
        sent_input, _, continuation_mode = self._responses_input(request)
        for line in self._stream_lines(
            self._url("responses"),
            payload,
            {"Authorization": f"Bearer {key}"} if key else {},
            request.timeout_seconds,
        ):
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw or raw == "[DONE]":
                continue
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise HarnessError("OpenAI Responses stream contained malformed JSON") from exc
            if not isinstance(value, dict):
                raise HarnessError("OpenAI Responses stream frame must be a JSON object")
            event_type = value.get("type")
            if event_type == "response.output_text.delta":
                delta = value.get("delta")
                if not isinstance(delta, str):
                    raise HarnessError("OpenAI Responses text delta must be a string")
                saw_text = saw_text or bool(delta)
                if delta:
                    yield {"type": "text_delta", "text": delta}
            elif event_type in {"response.refusal.delta", "response.refusal.done"}:
                refusal = value.get("delta", value.get("refusal", ""))
                if isinstance(refusal, str):
                    refusal_parts.append(refusal)
                if event_type == "response.refusal.done":
                    raise HarnessError(f"OpenAI Responses output was refused: {''.join(refusal_parts) or 'no reason provided'}")
            elif event_type in {"response.incomplete", "response.failed"}:
                response = value.get("response", value)
                if not isinstance(response, dict):
                    raise HarnessError("OpenAI Responses terminal event is malformed")
                _ensure_responses_status(response)
            elif event_type == "response.completed":
                response = value.get("response")
                if not isinstance(response, dict):
                    raise HarnessError("OpenAI Responses completion event is missing response data")
                _ensure_responses_status(response)
                final_text, refusal = _responses_content(response)
                tool_calls = _responses_tool_calls(response)
                if refusal or refusal_parts:
                    raise HarnessError(f"OpenAI Responses output was refused: {refusal or ''.join(refusal_parts)}")
                if not saw_text:
                    if not final_text and not tool_calls:
                        raise HarnessError("OpenAI Responses completed with no output text")
                    if final_text:
                        saw_text = True
                        yield {"type": "text_delta", "text": final_text}
                for call in tool_calls:
                    yield {"type": "tool_call_delta", "tool_call": call}
                continuation = self._continuation(request, response, sent_input, continuation_mode)
                yield {
                    "type": "response_state",
                    "response_id": continuation.response_id,
                    "replay_items": continuation.replay_items,
                    "continuation_mode": continuation_mode,
                }
                yield {"type": "usage", **_openai_usage(response.get("usage"))}
                yield {"type": "done", "finish_reason": "completed"}
                return

    def _stream_chat_completions(self, request: ProviderRequest, key: str) -> Iterator[dict[str, Any]]:
        finish_reason = ""
        text_parts: list[str] = []
        tool_fragments: list[dict[str, Any]] = []
        sent_messages = self._chat_messages(request)
        for line in self._stream_lines(
            self._url("chat-completions"),
            self._payload(request, "chat-completions", stream=True),
            {"Authorization": f"Bearer {key}"} if key else {},
            request.timeout_seconds,
        ):
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data:
                continue
            if data == "[DONE]":
                if not finish_reason:
                    raise HarnessError(
                        "OpenAI Chat Completions stream ended without an explicit "
                        "finish_reason"
                    )
                tool_calls = _chat_tool_calls(tool_fragments)
                continuation = self._chat_continuation(sent_messages, "".join(text_parts), tool_calls)
                if continuation is not None:
                    yield {
                        "type": "chat_state",
                        "messages": continuation.messages,
                        "pending_call_ids": continuation.pending_call_ids,
                    }
                yield {"type": "done", "finish_reason": finish_reason}
                return
            try:
                value = json.loads(data)
            except json.JSONDecodeError as exc:
                raise HarnessError("OpenAI Chat Completions stream contained malformed JSON") from exc
            if not isinstance(value, dict):
                raise HarnessError("OpenAI Chat Completions stream frame must be a JSON object")
            if isinstance(value.get("usage"), dict):
                yield {"type": "usage", **_openai_usage(value["usage"])}
            choices = value.get("choices", [])
            if not choices:
                continue
            if not isinstance(choices, list) or not isinstance(choices[0], dict):
                raise HarnessError("OpenAI stream choices must be an array of objects")
            choice = choices[0]
            terminal = choice.get("finish_reason")
            if terminal:
                finish_reason = str(terminal)
                if finish_reason in {"length", "content_filter"}:
                    raise HarnessError(f"OpenAI Chat Completions output is incomplete: {finish_reason}")
            delta = choice.get("delta", {})
            if not isinstance(delta, dict):
                raise HarnessError("OpenAI stream delta must be an object")
            if delta.get("refusal"):
                raise HarnessError(f"OpenAI Chat Completions output was refused: {delta['refusal']}")
            if delta.get("content"):
                if not isinstance(delta["content"], str):
                    raise HarnessError("OpenAI text delta must be a string")
                text_parts.append(delta["content"])
                yield {"type": "text_delta", "text": delta["content"]}
            tool_calls = delta.get("tool_calls", [])
            if not isinstance(tool_calls, list):
                raise HarnessError("OpenAI tool call deltas must be an array")
            for call in tool_calls:
                if not isinstance(call, dict):
                    raise HarnessError("OpenAI tool call delta must be an object")
                tool_fragments.append(copy.deepcopy(call))
                yield {"type": "tool_call_delta", "tool_call": call}

    def embed(self, texts: list[str], timeout_seconds: float | None = None) -> list[list[float]]:
        key = self._api_key()
        data = self._post(
            f"{self._base_endpoint()}/embeddings",
            {"model": self.config.get("memory.embedding_model") or self.settings["model"], "input": texts},
            {"Authorization": f"Bearer {key}"} if key else {},
            timeout_seconds,
        )
        return [list(map(float, item["embedding"])) for item in data.get("data", [])]


class AnthropicProvider(Provider):
    structured_retry_is_safe = True
    @staticmethod
    def _tools(request: ProviderRequest) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for tool in request.tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not isinstance(tool.get("input_schema"), dict):
                raise HarnessError("Anthropic tool definition is malformed")
            output.append(
                {
                    "name": tool["name"],
                    "description": str(tool.get("description") or ""),
                    "input_schema": tool["input_schema"],
                    "strict": True,
                }
            )
        return output

    def _messages(self, request: ProviderRequest) -> list[dict[str, Any]]:
        continuation = request.native_continuation
        outputs = request.native_function_call_outputs
        if continuation is None:
            if outputs:
                raise HarnessError("Anthropic tool results require continuation state")
            messages = copy.deepcopy(request.messages)
            images = request_images(request)
            if images:
                at = next((index for index in range(len(messages) - 1, -1, -1) if messages[index].get("role") == "user"), -1)
                if at < 0 or not isinstance(messages[at].get("content"), str):
                    raise HarnessError("Image attachments require a user text message")
                messages[at]["content"] = [
                    {"type": "text", "text": messages[at]["content"]},
                    *[
                        {"type": "image", "source": {"type": "base64", "media_type": one["type"], "data": one["data"]}}
                        for one in images
                    ],
                ]
            return messages
        if continuation.provider != "anthropic":
            raise HarnessError("Anthropic received continuation state from another provider")
        if continuation.state.get("model") != request.model:
            raise HarnessError("Anthropic continuation model does not match the request")
        if continuation.state.get("endpoint") != str(self.settings["endpoint"]).rstrip("/"):
            raise HarnessError("Anthropic continuation endpoint does not match this provider profile")
        messages = continuation.state.get("messages")
        names = continuation.state.get("call_names")
        if not isinstance(messages, list) or any(not isinstance(item, dict) for item in messages) or not isinstance(names, dict):
            raise HarnessError("Anthropic continuation state is invalid")
        pending = set(continuation.pending_call_ids)
        seen: set[str] = set()
        results: list[dict[str, Any]] = []
        for output in outputs:
            if not isinstance(output, FunctionCallOutput) or output.call_id not in pending or output.call_id in seen:
                raise HarnessError("Anthropic tool results must match unique pending call IDs")
            seen.add(output.call_id)
            results.append({"type": "tool_result", "tool_use_id": output.call_id, "content": output.output})
        if seen != pending:
            raise HarnessError("Anthropic continuation requires one result for every pending call")
        return [*copy.deepcopy(messages), {"role": "user", "content": results}]

    def _payload(self, request: ProviderRequest, *, stream: bool) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        messages = self._messages(request)
        if request.prompt_cache_retention == "in_memory":
            system: object = [
                {
                    "type": "text",
                    "text": request.system_prefix,
                    "cache_control": {"type": "ephemeral", "ttl": "5m"},
                },
                {"type": "text", "text": request.dynamic_context},
            ]
        else:
            system = f"{request.system_prefix}\n\n{request.dynamic_context}"
        payload: dict[str, Any] = {
            "model": request.model,
            "system": system,
            "messages": messages,
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
            "stream": stream,
        }
        if request.tools:
            payload["tools"] = self._tools(request)
        if request.response_format is not None:
            payload["output_config"] = {
                "format": {"type": "json_schema", "schema": request.response_format.schema}
            }
        return payload, messages

    @staticmethod
    def _content_result(content: object) -> tuple[str, list[dict[str, Any]], dict[str, str]]:
        if not isinstance(content, list):
            raise HarnessError("Anthropic response content must be an array")
        text_parts: list[str] = []
        calls: list[dict[str, Any]] = []
        names: dict[str, str] = {}
        for index, item in enumerate(content):
            if not isinstance(item, dict):
                raise HarnessError("Anthropic content block must be an object")
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                text_parts.append(item["text"])
            elif item.get("type") == "tool_use":
                call_id, name, arguments = item.get("id"), item.get("name"), item.get("input", {})
                if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name or not isinstance(arguments, dict):
                    raise HarnessError("Anthropic tool use block is malformed")
                calls.append({"index": index, "id": call_id, "function": {"name": name, "arguments": arguments}})
                names[call_id] = name
        return "".join(text_parts), calls, names

    @staticmethod
    def _check_stop(stop_reason: object) -> str:
        if not isinstance(stop_reason, str) or not stop_reason.strip():
            raise HarnessError("Anthropic output has no explicit stop_reason")
        reason = stop_reason.strip()
        if reason in {"max_tokens", "refusal", "model_context_window_exceeded"}:
            raise HarnessError(f"Anthropic output is incomplete: {reason}")
        return reason

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        endpoint = str(self.settings["endpoint"]).rstrip("/")
        url = endpoint if endpoint.endswith("/messages") else f"{endpoint}/messages"
        payload, sent_messages = self._payload(request, stream=False)
        data = self._post(
            url,
            payload,
            {"x-api-key": self._api_key(), "anthropic-version": "2023-06-01"},
            request.timeout_seconds,
        )
        text, calls, names = self._content_result(data.get("content"))
        if not text and not calls:
            raise HarnessError("Anthropic response contains no text block")
        usage = data.get("usage", {})
        if not isinstance(usage, dict):
            usage = {}
        output_details = usage.get("output_tokens_details", {})
        if not isinstance(output_details, dict):
            output_details = {}
        stop_reason = self._check_stop(data.get("stop_reason"))
        continuation = None
        if calls:
            continuation = NativeToolContinuation(
                "anthropic",
                {"messages": [*sent_messages, {"role": "assistant", "content": copy.deepcopy(data["content"])}], "call_names": names, "model": request.model, "endpoint": str(self.settings["endpoint"]).rstrip("/")},
                [call["id"] for call in calls],
            )
        return ProviderResponse(
            text=text,
            finish_reason=stop_reason,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cached_input_tokens=usage.get("cache_read_input_tokens"),
            cache_write_input_tokens=usage.get("cache_creation_input_tokens"),
            reasoning_tokens=output_details.get("thinking_tokens"),
            billed_output_tokens=usage.get("output_tokens"),
            raw={**data, "tool_call_deltas": calls},
            native_continuation=continuation,
        )

    def stream(self, request: ProviderRequest) -> Iterator[dict[str, Any]]:
        endpoint = str(self.settings["endpoint"]).rstrip("/")
        url = endpoint if endpoint.endswith("/messages") else f"{endpoint}/messages"
        payload, sent_messages = self._payload(request, stream=True)
        blocks: dict[int, dict[str, Any]] = {}
        stop_reason = ""
        for line in self._stream_lines(
            url,
            payload,
            {"x-api-key": self._api_key(), "anthropic-version": "2023-06-01"},
            request.timeout_seconds,
        ):
            if not line.startswith("data:"):
                continue
            try:
                value = json.loads(line[5:].strip())
            except json.JSONDecodeError as exc:
                raise HarnessError("Anthropic stream contained malformed JSON") from exc
            if not isinstance(value, dict):
                raise HarnessError("Anthropic stream frame must be a JSON object")
            event_type = value.get("type")
            if event_type == "content_block_start":
                index = value.get("index")
                block = value.get("content_block")
                if not isinstance(index, int) or isinstance(index, bool) or index < 0 or not isinstance(block, dict):
                    raise HarnessError("Anthropic content block start is malformed")
                blocks[index] = copy.deepcopy(block)
                if block.get("type") == "tool_use":
                    blocks[index]["_arguments_json"] = ""
                if block.get("type") == "text" and block.get("text"):
                    yield {"type": "text_delta", "text": str(block["text"])}
            delta = value.get("delta", {})
            if not isinstance(delta, dict):
                raise HarnessError("Anthropic stream delta must be an object")
            if delta.get("type") == "text_delta":
                text = delta.get("text", "")
                if not isinstance(text, str):
                    raise HarnessError("Anthropic text delta must be a string")
                index = value.get("index")
                if isinstance(index, int) and index in blocks:
                    blocks[index]["text"] = str(blocks[index].get("text") or "") + text
                yield {"type": "text_delta", "text": text}
            elif delta.get("type") == "input_json_delta":
                index = value.get("index")
                partial = delta.get("partial_json", "")
                if not isinstance(index, int) or index not in blocks or not isinstance(partial, str):
                    raise HarnessError("Anthropic tool input delta is malformed")
                blocks[index]["_arguments_json"] = str(blocks[index].get("_arguments_json") or "") + partial
            elif delta.get("type") in {"thinking_delta", "signature_delta"}:
                index = value.get("index")
                if isinstance(index, int) and index in blocks:
                    key = "thinking" if delta.get("type") == "thinking_delta" else "signature"
                    fragment = delta.get(key, "")
                    if isinstance(fragment, str):
                        blocks[index][key] = str(blocks[index].get(key) or "") + fragment
            if value.get("type") in {"message_start", "message_delta"}:
                usage = value.get("usage")
                if value.get("type") == "message_start" and isinstance(value.get("message"), dict):
                    usage = value["message"].get("usage", usage)
                if isinstance(usage, dict):
                    output_details = usage.get("output_tokens_details", {})
                    if not isinstance(output_details, dict):
                        output_details = {}
                    yield {
                        "type": "usage",
                        "input_tokens": usage.get("input_tokens"),
                        "output_tokens": usage.get("output_tokens"),
                        "cached_input_tokens": usage.get("cache_read_input_tokens"),
                        "cache_write_input_tokens": usage.get("cache_creation_input_tokens"),
                        "reasoning_tokens": output_details.get("thinking_tokens"),
                        "billed_output_tokens": usage.get("output_tokens"),
                    }
                if value.get("type") == "message_delta":
                    stop_reason = self._check_stop(delta.get("stop_reason"))
            if value.get("type") == "message_stop":
                content = [blocks[index] for index in sorted(blocks)]
                for block in content:
                    arguments_json = block.pop("_arguments_json", None)
                    if block.get("type") == "tool_use" and arguments_json:
                        try:
                            block["input"] = json.loads(arguments_json)
                        except json.JSONDecodeError as exc:
                            raise HarnessError("Anthropic streamed tool input is malformed JSON") from exc
                _, calls, names = self._content_result(content)
                for call in calls:
                    yield {"type": "tool_call_delta", "tool_call": call}
                if calls:
                    continuation = NativeToolContinuation(
                        "anthropic",
                        {"messages": [*sent_messages, {"role": "assistant", "content": content}], "call_names": names, "model": request.model, "endpoint": str(self.settings["endpoint"]).rstrip("/")},
                        [call["id"] for call in calls],
                    )
                    yield {
                        "type": "native_state",
                        "provider": continuation.provider,
                        "state": continuation.state,
                        "pending_call_ids": continuation.pending_call_ids,
                    }
                yield {"type": "done", "finish_reason": stop_reason}
                return


class OllamaProvider(Provider):
    structured_retry_is_safe = True
    @staticmethod
    def _tools(request: ProviderRequest) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for tool in request.tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not isinstance(tool.get("input_schema"), dict):
                raise HarnessError("Ollama tool definition is malformed")
            output.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": str(tool.get("description") or ""),
                        "parameters": tool["input_schema"],
                    },
                }
            )
        return output

    def _messages(self, request: ProviderRequest) -> list[dict[str, Any]]:
        continuation = request.native_continuation
        outputs = request.native_function_call_outputs
        if continuation is None:
            if outputs:
                raise HarnessError("Ollama tool results require continuation state")
            messages = message_list(request)
            images = request_images(request)
            if images:
                at = next((index for index in range(len(messages) - 1, -1, -1) if messages[index].get("role") == "user"), -1)
                if at < 0:
                    raise HarnessError("Image attachments require a user text message")
                messages[at] = {**messages[at], "images": [one["data"] for one in images]}
            return messages
        if continuation.provider != "ollama":
            raise HarnessError("Ollama received continuation state from another provider")
        if continuation.state.get("model") != request.model:
            raise HarnessError("Ollama continuation model does not match the request")
        if continuation.state.get("endpoint") != str(self.settings["endpoint"]).rstrip("/"):
            raise HarnessError("Ollama continuation endpoint does not match this provider profile")
        messages = continuation.state.get("messages")
        names = continuation.state.get("call_names")
        if not isinstance(messages, list) or any(not isinstance(item, dict) for item in messages) or not isinstance(names, dict):
            raise HarnessError("Ollama continuation state is invalid")
        pending = set(continuation.pending_call_ids)
        seen: set[str] = set()
        results: list[dict[str, Any]] = []
        for output in outputs:
            if not isinstance(output, FunctionCallOutput) or output.call_id not in pending or output.call_id in seen:
                raise HarnessError("Ollama tool results must match unique pending call IDs")
            seen.add(output.call_id)
            results.append({"role": "tool", "tool_name": str(names.get(output.call_id) or ""), "content": output.output})
        if seen != pending:
            raise HarnessError("Ollama continuation requires one result for every pending call")
        return [*copy.deepcopy(messages), *results]

    def _payload(self, request: ProviderRequest, *, stream: bool) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        messages = self._messages(request)
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "stream": stream,
            "options": {"temperature": request.temperature, "num_predict": request.max_output_tokens},
        }
        if request.tools:
            payload["tools"] = self._tools(request)
        if request.response_format is not None:
            payload["format"] = request.response_format.schema
        return payload, messages

    @staticmethod
    def _tool_calls(
        message: object, *, start_index: int = 0
    ) -> tuple[list[dict[str, Any]], dict[str, str]]:
        if not isinstance(message, dict):
            raise HarnessError("Ollama response message must be an object")
        raw_calls = message.get("tool_calls", [])
        if raw_calls is None:
            raw_calls = []
        if not isinstance(raw_calls, list):
            raise HarnessError("Ollama tool calls must be an array")
        calls: list[dict[str, Any]] = []
        names: dict[str, str] = {}
        for offset, raw in enumerate(raw_calls):
            if not isinstance(raw, dict) or not isinstance(raw.get("function"), dict):
                raise HarnessError("Ollama tool call is malformed")
            function = raw["function"]
            name, arguments = function.get("name"), function.get("arguments", {})
            if not isinstance(name, str) or not name or not isinstance(arguments, dict):
                raise HarnessError("Ollama tool call function is malformed")
            index = start_index + offset
            call_id = str(raw.get("id") or f"ollama-{index}")
            if call_id in names:
                raise HarnessError("Ollama tool call IDs must be unique")
            calls.append({"index": index, "id": call_id, "function": {"name": name, "arguments": arguments}})
            names[call_id] = name
        return calls, names

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        endpoint = str(self.settings["endpoint"]).rstrip("/")
        url = endpoint if endpoint.endswith("/api/chat") else f"{endpoint}/api/chat"
        payload, sent_messages = self._payload(request, stream=False)
        data = self._post(
            url,
            payload,
            timeout_seconds=request.timeout_seconds,
        )
        try:
            message = data["message"]
            text = message.get("content") or ""
        except (KeyError, TypeError) as exc:
            raise HarnessError("Ollama response is missing message.content") from exc
        calls, names = self._tool_calls(message)
        if not isinstance(text, str) or (not text and not calls):
            raise HarnessError("Ollama response contains no text or tool call")
        if data.get("done") is not True:
            raise HarnessError(
                "Ollama response was nonterminal (done was not true); partial "
                "output was not accepted"
            )
        continuation = None
        if calls:
            continuation = NativeToolContinuation(
                "ollama",
                {"messages": [*sent_messages, copy.deepcopy(message)], "call_names": names, "model": request.model, "endpoint": str(self.settings["endpoint"]).rstrip("/")},
                [call["id"] for call in calls],
            )
        return ProviderResponse(
            text=text,
            finish_reason="stop",
            input_tokens=data.get("prompt_eval_count"),
            output_tokens=data.get("eval_count"),
            billed_output_tokens=data.get("eval_count"),
            raw={**data, "tool_call_deltas": calls},
            native_continuation=continuation,
        )

    def stream(self, request: ProviderRequest) -> Iterator[dict[str, Any]]:
        endpoint = str(self.settings["endpoint"]).rstrip("/")
        url = endpoint if endpoint.endswith("/api/chat") else f"{endpoint}/api/chat"
        payload, sent_messages = self._payload(request, stream=True)
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        calls: list[dict[str, Any]] = []
        names: dict[str, str] = {}
        retained_calls: list[dict[str, Any]] = []
        for line in self._stream_lines(url, payload, timeout_seconds=request.timeout_seconds):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise HarnessError("Ollama stream contained malformed JSON") from exc
            if not isinstance(value, dict):
                raise HarnessError("Ollama stream frame must be a JSON object")
            message = value.get("message", {})
            if not isinstance(message, dict):
                raise HarnessError("Ollama stream message must be an object")
            text_value = message.get("content", "")
            if not isinstance(text_value, str):
                raise HarnessError("Ollama text delta must be a string")
            if text_value:
                text_parts.append(text_value)
                yield {"type": "text_delta", "text": text_value}
            thinking_value = message.get("thinking", "")
            if not isinstance(thinking_value, str):
                raise HarnessError("Ollama thinking delta must be a string")
            if thinking_value:
                thinking_parts.append(thinking_value)
            frame_calls, frame_names = self._tool_calls(message, start_index=len(calls))
            raw_calls = message.get("tool_calls") or []
            if frame_calls:
                if set(names).intersection(frame_names):
                    raise HarnessError("Ollama streamed tool call IDs must be unique")
                calls.extend(frame_calls)
                names.update(frame_names)
                retained_calls.extend(copy.deepcopy(raw_calls))
                for call in frame_calls:
                    yield {"type": "tool_call_delta", "tool_call": call}
            if value.get("done"):
                yield {
                    "type": "usage",
                    "input_tokens": value.get("prompt_eval_count"),
                    "output_tokens": value.get("eval_count"),
                    "billed_output_tokens": value.get("eval_count"),
                }
                if calls:
                    retained_message: dict[str, Any] = {
                        "role": str(message.get("role") or "assistant"),
                        "content": "".join(text_parts),
                        "tool_calls": retained_calls,
                    }
                    if thinking_parts:
                        retained_message["thinking"] = "".join(thinking_parts)
                    continuation = NativeToolContinuation(
                        "ollama",
                        {"messages": [*sent_messages, retained_message], "call_names": names, "model": request.model, "endpoint": str(self.settings["endpoint"]).rstrip("/")},
                        [call["id"] for call in calls],
                    )
                    yield {
                        "type": "native_state",
                        "provider": continuation.provider,
                        "state": continuation.state,
                        "pending_call_ids": continuation.pending_call_ids,
                    }
                yield {"type": "done", "finish_reason": value.get("done_reason", "stop")}
                return

    def embed(self, texts: list[str], timeout_seconds: float | None = None) -> list[list[float]]:
        endpoint = str(self.settings["endpoint"]).rstrip("/")
        data = self._post(
            f"{endpoint}/api/embed",
            {"model": self.config.get("memory.embedding_model") or self.settings["model"], "input": texts},
            timeout_seconds=timeout_seconds,
        )
        return [list(map(float, item)) for item in data.get("embeddings", [])]


class LocalProcessProvider(Provider):
    def complete(self, request: ProviderRequest) -> ProviderResponse:
        command = self.settings.get("command", [])
        if not isinstance(command, list) or not command:
            raise HarnessError("provider.command must be a non-empty argv list for the local provider")
        payload = json.dumps(self._redactor.value({
            "model": request.model,
            "system_prefix": request.system_prefix,
            "dynamic_context": request.dynamic_context,
            "messages": request.messages,
            "attachments": request.attachments,
            "temperature": request.temperature,
            "max_output_tokens": request.max_output_tokens,
            "response_format": (
                {"name": request.response_format.name, "schema": request.response_format.schema, "strict": request.response_format.strict}
                if request.response_format is not None
                else None
            ),
        }))
        response_limit = max(
            MAX_PROVIDER_RESPONSE_BYTES,
            int(self.config.get("execution.max_output_bytes")),
        )
        try:
            result = CommandRunner(self.config).run(
                command,
                timeout=self._timeout(request.timeout_seconds),
                stdin_text=payload,
                max_output_bytes=response_limit,
            )
        except OSError as exc:
            raise HarnessError(f"Local provider failed: {exc}") from exc
        if result.timed_out:
            raise HarnessError("Local provider timed out")
        if result.output_truncated:
            raise HarnessError(f"Local provider response exceeded its {response_limit}-byte limit")
        if result.exit_code != 0:
            detail = bounded_redacted_text(self._redactor, result.stderr, 8_000)
            raise HarnessError(f"Local provider exited {result.exit_code}: {detail}")
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise HarnessError("Local provider must print one JSON object with a text field") from exc
        if not isinstance(data, dict) or not isinstance(data.get("text"), str):
            raise HarnessError("Local provider must print one JSON object with a string text field")
        finish_reason = data.get("finish_reason", "stop")
        if not isinstance(finish_reason, str):
            raise HarnessError("Local provider finish_reason must be a string")
        return ProviderResponse(data["text"], finish_reason, raw=data)


class StreamDecoder:
    """Incremental UTF-8 line parser for SSE and newline-delimited JSON."""

    def __init__(self, max_buffer_bytes: int = 2_000_000):
        self.decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self.buffer = ""
        self.max_buffer_bytes = max_buffer_bytes

    def feed(self, chunk: bytes, final: bool = False) -> list[str]:
        self.buffer += self.decoder.decode(chunk, final=final)
        if len(self.buffer.encode("utf-8")) > self.max_buffer_bytes:
            raise HarnessError("Provider stream buffer exceeded its limit")
        lines = self.buffer.split("\n")
        if final:
            self.buffer = ""
            if lines and lines[-1] == "":
                lines.pop()
        else:
            self.buffer = lines.pop()
        return [line.rstrip("\r") for line in lines]


def collect_stream(
    provider: Provider,
    request: ProviderRequest,
    max_text_chars: int = 2_000_000,
    deadline_at: float | None = None,
) -> ProviderResponse:
    """Consume normalized provider events with strict framing and a bounded text result."""
    text_parts: list[str] = []
    text_chars = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    tool_use_tokens: int | None = None
    billed_output_tokens: int | None = None
    finish_reason = ""
    tool_calls: list[dict[str, Any]] = []
    responses_continuation: ResponsesContinuation | None = None
    continuation_mode: str | None = None
    chat_continuation: ChatCompletionsContinuation | None = None
    native_continuation: NativeToolContinuation | None = None
    done = False
    for event in provider.stream(request):
        if deadline_at is not None and time.monotonic() >= deadline_at:
            raise HarnessError("Workflow deadline expired during provider streaming")
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise HarnessError("Provider stream event must be an object with a type")
        event_type = event["type"]
        if done:
            raise HarnessError("Provider stream emitted data after completion")
        if event_type == "text_delta":
            text = event.get("text")
            if not isinstance(text, str):
                raise HarnessError("Provider text delta must be a string")
            text_chars += len(text)
            if text_chars > max_text_chars:
                raise HarnessError("Provider streamed text exceeded its character limit")
            text_parts.append(text)
        elif event_type == "tool_call_delta":
            call = event.get("tool_call")
            if not isinstance(call, dict):
                raise HarnessError("Provider tool call delta must be an object")
            if len(tool_calls) >= 4096:
                raise HarnessError("Provider stream emitted too many tool call fragments")
            tool_calls.append(call)
        elif event_type == "usage":
            if event.get("input_tokens") is not None:
                input_tokens = int(event["input_tokens"])
            if event.get("output_tokens") is not None:
                output_tokens = int(event["output_tokens"])
            if event.get("cached_input_tokens") is not None:
                cached_input_tokens = int(event["cached_input_tokens"])
            if event.get("cache_write_input_tokens") is not None:
                cache_write_input_tokens = int(event["cache_write_input_tokens"])
            if event.get("reasoning_tokens") is not None:
                reasoning_tokens = int(event["reasoning_tokens"])
            if event.get("tool_use_tokens") is not None:
                tool_use_tokens = int(event["tool_use_tokens"])
            if event.get("billed_output_tokens") is not None:
                billed_output_tokens = int(event["billed_output_tokens"])
        elif event_type == "response_state":
            response_id = event.get("response_id")
            replay_items = event.get("replay_items")
            if response_id is not None and (not isinstance(response_id, str) or not response_id):
                raise HarnessError("Provider response state has an invalid response ID")
            if not isinstance(replay_items, list) or any(not isinstance(item, dict) for item in replay_items):
                raise HarnessError("Provider response state has invalid replay items")
            responses_continuation = ResponsesContinuation(response_id, copy.deepcopy(replay_items))
            continuation_mode = str(event.get("continuation_mode") or "")
        elif event_type == "chat_state":
            messages = event.get("messages")
            pending_call_ids = event.get("pending_call_ids")
            if not isinstance(messages, list) or any(not isinstance(item, dict) for item in messages):
                raise HarnessError("Provider chat state has invalid messages")
            if not isinstance(pending_call_ids, list) or any(not isinstance(item, str) or not item for item in pending_call_ids):
                raise HarnessError("Provider chat state has invalid pending call IDs")
            chat_continuation = ChatCompletionsContinuation(copy.deepcopy(messages), list(pending_call_ids))
        elif event_type == "native_state":
            provider_name = event.get("provider")
            state = event.get("state")
            pending_call_ids = event.get("pending_call_ids")
            if not isinstance(provider_name, str) or not provider_name or not isinstance(state, dict):
                raise HarnessError("Provider native continuation state is invalid")
            if not isinstance(pending_call_ids, list) or any(not isinstance(item, str) or not item for item in pending_call_ids):
                raise HarnessError("Provider native continuation pending call IDs are invalid")
            native_continuation = NativeToolContinuation(provider_name, copy.deepcopy(state), list(pending_call_ids))
        elif event_type == "done":
            terminal = event.get("finish_reason")
            if not isinstance(terminal, str) or not terminal.strip():
                raise HarnessError(
                    "Provider completion event has no explicit finish_reason"
                )
            finish_reason = terminal.strip()
            done = True
        else:
            raise HarnessError(f"Unknown provider stream event: {event_type}")
    if deadline_at is not None and time.monotonic() >= deadline_at:
        raise HarnessError("Workflow deadline expired during provider streaming")
    if not done:
        raise HarnessError("Provider stream ended without a completion event")
    return ProviderResponse(
        text="".join(text_parts),
        finish_reason=finish_reason,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_write_input_tokens=cache_write_input_tokens,
        reasoning_tokens=reasoning_tokens,
        tool_use_tokens=tool_use_tokens,
        billed_output_tokens=billed_output_tokens,
        raw={"tool_call_deltas": tool_calls, **({"continuation_mode": continuation_mode} if continuation_mode else {})},
        responses_continuation=responses_continuation,
        chat_continuation=chat_continuation,
        native_continuation=native_continuation,
    )


def create_provider(config: LoadedConfig) -> Provider:
    name = config.get("provider.name")
    if name in {"openai", "openai-compatible"}:
        return OpenAIProvider(config)
    if name == "anthropic":
        return AnthropicProvider(config)
    if name == "gemini":
        from .gemini import GeminiProvider

        return GeminiProvider(config)
    if name == "ollama":
        return OllamaProvider(config)
    if name == "local":
        return LocalProcessProvider(config)
    if name == "codex-cli":
        from .codex_cli import CodexCLIProvider

        return CodexCLIProvider(config)
    if name in ("claude-cli", "copilot-cli", "assistant-cli", "gemini-cli"):
        from .subscription_cli import SubscriptionCLIProvider

        return SubscriptionCLIProvider(config, name)
    if name == "m365-copilot":
        from .m365_copilot import M365CopilotProvider

        return M365CopilotProvider(config)
    raise HarnessError(f"Unknown provider: {name}")


def create_embedding_provider(config: LoadedConfig) -> Provider:
    """Create the configured embedding provider without changing completion settings."""
    validate_embedding_provider_route(config)
    requested = str(config.get("memory.embedding_provider") or config.get("provider.name"))
    current = str(config.get("provider.name"))
    if requested == current:
        return create_provider(config)
    defaults = {
        "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
        "gemini": ("https://generativelanguage.googleapis.com/v1beta", "GEMINI_API_KEY"),
        "ollama": ("http://127.0.0.1:11434", ""),
        "openai-compatible": ("http://127.0.0.1:8000/v1", ""),
    }
    if requested not in defaults:
        raise HarnessError(f"Embedding provider does not support embeddings: {requested}")
    data = copy.deepcopy(config.data)
    endpoint, key_env = defaults[requested]
    data["provider"].update(
        {
            "name": requested,
            "model": config.get("memory.embedding_model") or data["provider"]["model"],
            "endpoint": endpoint,
            "api_key_env": key_env,
        }
    )
    routed = LoadedConfig(data, config.project_root, config.sources, dict(config.provenance), config.trusted_floor)
    return create_provider(routed)
