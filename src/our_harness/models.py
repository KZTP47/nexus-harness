from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class HarnessError(RuntimeError):
    """Expected user-facing failure."""


class DeadlineExpired(HarnessError):
    """A bounded operation exhausted its owning workflow or tool clock."""


class Deadline(Protocol):
    """Shared workflow clock accepted by bounded subsystems."""

    def check(self, operation: str) -> None: ...

    def remaining_seconds(self, operation: str, cap: float | None = None) -> float: ...


class RunState(StrEnum):
    DISCOVER = "discover"
    PLAN = "plan"
    APPLY = "apply"
    VERIFY = "verify"
    REVIEW = "review"
    HEAL = "heal"
    COMPLETE = "complete"
    FAILED = "failed"
    PAUSED = "paused"


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    cwd: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    output_truncated: bool = False

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    @property
    def complete_success(self) -> bool:
        """True only when a successful command's complete output was captured."""
        return self.passed and not self.output_truncated

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Detection:
    stack: str
    evidence: list[str]
    test_commands: list[list[str]] = field(default_factory=list)
    lint_commands: list[list[str]] = field(default_factory=list)
    build_commands: list[list[str]] = field(default_factory=list)
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ResponseFormat:
    name: str
    schema: dict[str, Any]
    strict: bool = True


@dataclass(frozen=True)
class ResponsesContinuation:
    """Opaque Responses API state that can be resumed after tool execution.

    ``replay_items`` contains the complete input/output item history needed by
    endpoints that do not support ``previous_response_id``. Keeping these
    items typed preserves reasoning and function-call linkage.
    """

    response_id: str | None
    replay_items: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ChatCompletionsContinuation:
    """Typed Chat Completions history awaiting function outputs."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    pending_call_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FunctionCallOutput:
    call_id: str
    output: str


@dataclass(frozen=True)
class NativeToolContinuation:
    """Provider-owned state for a native tool round.

    The state is intentionally opaque to workflow code.  It must only be
    returned to an adapter for the same provider profile and model.
    """

    provider: str
    state: dict[str, Any] = field(default_factory=dict)
    pending_call_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProviderRequest:
    system_prefix: str
    dynamic_context: str
    messages: list[dict[str, Any]]
    model: str
    temperature: float = 0.2
    max_output_tokens: int = 4096
    tools: list[dict[str, Any]] = field(default_factory=list)
    timeout_seconds: float | None = None
    response_format: ResponseFormat | None = None
    prompt_cache_key: str | None = None
    prompt_cache_retention: str | None = None
    responses_continuation: ResponsesContinuation | None = None
    function_call_outputs: list[FunctionCallOutput] = field(default_factory=list)
    chat_continuation: ChatCompletionsContinuation | None = None
    chat_function_call_outputs: list[FunctionCallOutput] = field(default_factory=list)
    native_continuation: NativeToolContinuation | None = None
    native_function_call_outputs: list[FunctionCallOutput] = field(default_factory=list)
    reasoning_effort: str | None = None
    # User-selected files that belong to this one request.  Adapters translate
    # image bytes to their provider's native multimodal shape; text files are
    # already represented in the bounded dynamic context.
    attachments: list[dict[str, Any]] = field(default_factory=list)
    # Opaque durable-conversation identity for stateful transports. API and
    # command providers may ignore it; Electron web-chat providers use it to
    # keep two Nexus chats from sharing one provider-site thread.
    conversation_key: str = ""
    # The first chat attached to a manually selected provider conversation may
    # adopt that existing remote thread. Later Nexus chats always start new.
    prefer_existing_conversation: bool = False


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    # Empty means the adapter has not proved a terminal provider outcome. No
    # caller may infer success merely because some plausible text arrived.
    finish_reason: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    tool_use_tokens: int | None = None
    billed_output_tokens: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    responses_continuation: ResponsesContinuation | None = None
    chat_continuation: ChatCompletionsContinuation | None = None
    native_continuation: NativeToolContinuation | None = None

    def usage(self) -> dict[str, int | None]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "cache_write_input_tokens": self.cache_write_input_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "tool_use_tokens": self.tool_use_tokens,
            "billed_output_tokens": self.billed_output_tokens,
        }


@dataclass(frozen=True)
class ChangePlan:
    path: str
    baseline_sha256: str | None
    content: str | bytes | None
    delete: bool = False
    reason: str = ""
    mode: int | None = None


@dataclass(frozen=True)
class ReviewVerdict:
    verdict: str
    findings: list[dict[str, Any]]
    residual_risks: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        if self.verdict.upper() != "PASS":
            return False
        return all(
            isinstance(finding, dict) and str(finding.get("severity", "")).lower() == "advisory"
            for finding in self.findings
        )
