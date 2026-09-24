"""Allowlisted public CLI events. Never forward raw streams or private reasoning."""

from __future__ import annotations

import hashlib
import json
import threading
from collections import deque
from typing import Any, Callable

from .models import HarnessError


CONTRACT = "public-provider-activity/v1"
FINGERPRINT = hashlib.sha256(CONTRACT.encode()).hexdigest()
TEXT_LIMIT = 24_000
SUMMARY_CONTRACT = "codex-exec-public-reasoning-summary/v1"
# Claude Code prints summarized thinking in its own interface; the same
# summarized text from its stream is shown to the user here (never to agents).
CLAUDE_THINKING_CONTRACT = "claude-code-summarized-thinking/v1"
SUMMARY_CONTRACTS = frozenset({SUMMARY_CONTRACT, CLAUDE_THINKING_CONTRACT})


class PublicStream:
    """Bounded incremental JSONL observer; transport validation stays authoritative.

    Executed on the stdout reader. Callback errors are retained while pipes keep
    draining, then raised by the owner after process cleanup. Only completed
    public text blocks and tool lifecycle snapshots are admitted.
    """

    def __init__(self, provider: str, sink: Callable[[dict[str, Any]], None], redactor,
                 schema: dict | None = None, line_limit: int = 4_000_000):
        self.provider, self.sink, self.redactor = provider, sink, redactor
        self.schema = schema or {}
        self.line_limit = line_limit
        self.buffer = bytearray()
        self.error: Exception | None = None
        self.seen: dict[str, str] = {}
        self.tools: dict[str, tuple[str, dict]] = {}
        self.ordinal = 0
        self.observation_failures = 0
        self._pending = 0
        self._settled = threading.Condition()

    def detach_sink(self):
        """A slow archive must never backpressure the subprocess stdout pipe.

        A bounded shared queue and fixed workers limit abandoned observers. The
        final response remains authoritative even if optional activity is lost.
        """
        from .activity_observer import offer
        sink = self.sink
        queued = deque()
        draining = False

        def drain(_):
            nonlocal draining
            while True:
                with self._settled:
                    if not queued:
                        draining = False
                        return
                    value = queued.popleft()
                try:
                    sink(value)
                except Exception:
                    self.observation_failures += 1
                finally:
                    with self._settled:
                        self._pending -= 1
                        self._settled.notify_all()

        def enqueue(value):
            nonlocal draining
            with self._settled:
                if self._pending >= 64:
                    self.observation_failures += 1
                    return
                queued.append(value)
                self._pending += 1
                if not draining:
                    draining = True
                    if not offer(drain, None):
                        draining = False
                        self.observation_failures += self._pending
                        queued.clear()
                        self._pending = 0
        self.sink = enqueue

    def settle_observations(self):
        with self._settled:
            self._settled.wait_for(lambda: self._pending == 0, timeout=0.05)

    def feed(self, chunk: bytes) -> None:
        if self.error:
            return
        try:
            self.buffer.extend(chunk)
            while b"\n" in self.buffer:
                end = self.buffer.index(b"\n")
                line = bytes(self.buffer[:end])
                del self.buffer[:end + 1]
                self.line(line)
            if len(self.buffer) > self.line_limit:
                raise HarnessError("Provider activity line exceeded its bounded capture limit")
        except Exception as exc:
            self.error = exc
            self.buffer.clear()

    def finish(self) -> None:
        if self.buffer and not self.error:
            try:
                self.line(bytes(self.buffer))
            except Exception as exc:
                self.error = exc
            self.buffer.clear()
        if self.error:
            raise HarnessError("Public provider activity could not be recorded") from self.error

    def line(self, line: bytes) -> None:
        if len(line) > self.line_limit:
            raise HarnessError("Provider activity line exceeded its bounded capture limit")
        if not line.strip():
            return
        event = json.loads(line)
        if not isinstance(event, dict):
            return
        self.ordinal += 1
        if self.provider == "codex":
            self.codex(event)
        elif self.provider == "claude":
            self.claude(event)

    def emit(self, identity: str, kind: str, **fields) -> None:
        if not 0 < len(identity) <= 200:
            identity = hashlib.sha256(identity.encode()).hexdigest()
        value = {"schema_version": 1, "contract_fingerprint": FINGERPRINT,
                 "provider": self.provider, "id": identity, "kind": kind, **fields}
        # Bound and redact before persistence; keep an explicit truncation marker.
        raw = json.dumps(self.redactor.value(value), ensure_ascii=False)
        if len(raw) > TEXT_LIMIT:
            value = {key: value[key] for key in (
                "schema_version", "contract_fingerprint", "provider", "id", "kind")}
            value.update({"status": fields.get("status", "finished"),
                          "name": fields.get("name", "provider_activity"),
                          "text": self.redactor.text(str(fields.get("text") or ""))[:8000],
                          "details": raw[:12000], "truncated": True})
            if kind == "reasoning_summary":
                value["summary_contract"] = fields.get("summary_contract", SUMMARY_CONTRACT)
        else:
            value = json.loads(raw)
        key = identity + ":" + str(value.get("status", "message"))
        digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
        if self.seen.get(key) == digest:
            return
        # Ignore in-progress snapshots: emit only start and terminal, not deltas.
        if key in self.seen:
            raise HarnessError("A provider activity identity changed its recorded content")
        if kind == "reasoning_summary":
            from . import summary_observer
            summary_observer.offer(self.sink, value)
        else:
            self.sink(value)
        self.seen[key] = digest

    def text(self, identity: str, words: object) -> None:
        if not isinstance(words, str) or not words.strip():
            return
        # Structured transport output belongs to the final action path only.
        candidate = words.strip()
        if candidate.startswith("```json") and candidate.endswith("```"):
            candidate = candidate[7:-3].strip()
        try:
            value = json.loads(candidate)
        except ValueError:
            value = None
        required = self.schema.get("required") or []
        if required and isinstance(value, dict) and all(key in value for key in required):
            return
        self.emit(identity, "message", text=words)

    def codex(self, event: dict) -> None:
        lifecycle = event.get("type")
        if lifecycle in {"error", "turn.failed"}:
            error = event.get("error")
            words = error.get("message") if isinstance(error, dict) else event.get("message")
            if isinstance(words, str) and words.strip():
                self.emit(f"transport-error-{self.ordinal}", "notice", text="Provider error: " + words)
            return
        if lifecycle not in {"item.started", "item.completed"}:
            return
        item = event.get("item")
        if not isinstance(item, dict):
            return
        identity = str(item.get("id") or f"event-{self.ordinal}")
        kind = item.get("type")
        done = lifecycle == "item.completed"
        if kind == "reasoning" and done and isinstance(item.get("text"), str) and item["text"].strip():
            # Codex SDK ReasoningItem explicitly carries a public summary.
            # No raw reasoning fields, thinking blocks, or summary-generation call.
            try:
                self.emit(identity, "reasoning_summary", text=item["text"], summary_contract=SUMMARY_CONTRACT)
            except Exception:
                pass  # Optional display must never fail the provider turn.
            return
        if kind == "agent_message" and done:
            self.text(identity, item.get("text"))
            return
        if kind == "error" and done:
            self.emit(identity, "message", text="Provider error: " + str(item.get("message") or "Unknown error"))
            return
        if kind not in {"command_execution", "file_change", "mcp_tool_call", "web_search", "todo_list"}:
            return
        status = "finished" if done else "requested"
        if done and (item.get("status") == "failed" or item.get("error")
                     or isinstance(item.get("exit_code"), int) and item["exit_code"] != 0):
            status = "failed"
        arguments = {key: item[key] for key in ("command", "changes", "server", "tool", "arguments", "query", "items") if key in item}
        result = {key: item[key] for key in ("aggregated_output", "exit_code", "result", "error") if key in item}
        self.emit(identity, "tool", name=str(item.get("tool") or kind), status=status,
                  arguments=arguments, result=result)

    def claude(self, event: dict) -> None:
        kind = event.get("type")
        if kind not in {"assistant", "user"}:
            return
        message = event.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            return
        identity = str(message.get("id") or event.get("uuid") or f"event-{self.ordinal}")
        for index, block in enumerate(message["content"]):
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            # Claude Code prints one assistant event per content block, all
            # with the same message id and each block at index 0, so the line
            # ordinal keeps two blocks of one message from colliding.
            if kind == "assistant" and block_type == "text":
                self.text(f"{identity}-{self.ordinal}-{index}", block.get("text"))
            elif kind == "assistant" and block_type == "thinking" \
                    and isinstance(block.get("thinking"), str) and block["thinking"].strip():
                try:
                    self.emit(f"{identity}-{self.ordinal}-{index}-thinking", "reasoning_summary",
                              text=block["thinking"], summary_contract=CLAUDE_THINKING_CONTRACT)
                except Exception:
                    pass  # Optional display must never fail the provider turn.
            elif kind == "assistant" and block_type == "tool_use" and block.get("id"):
                tool_id = str(block["id"])
                name = str(block.get("name") or "provider_tool")
                arguments = block.get("input") if isinstance(block.get("input"), dict) else {}
                self.tools[tool_id] = (name, arguments)
                self.emit(tool_id, "tool", name=name, status="requested", arguments=arguments)
            elif kind == "user" and block_type == "tool_result" and block.get("tool_use_id"):
                tool_id = str(block["tool_use_id"])
                name, arguments = self.tools.get(tool_id, ("provider_tool", {}))
                content = block.get("content", "")
                # Images, thinking blocks and arbitrary provider metadata are excluded.
                if isinstance(content, list):
                    content = [one.get("text", "") for one in content
                               if isinstance(one, dict) and one.get("type") == "text"]
                self.emit(tool_id, "tool", name=name, arguments=arguments,
                          status="failed" if block.get("is_error") else "finished", result=content)
