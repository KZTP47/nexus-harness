"""Use a coding assistant you already pay for, through its own command line.

Some organisations have seats for Claude or GitHub Copilot but no API keys, and
will never get any. Those assistants each ship a command line tool that is
already signed in. This provider drives one of those tools as a plain program:
it hands the prompt in on standard input, reads the answer back, and reports
usage as subscription work with no price attached.

A recipe says how to talk to one tool: what to run, how to pass the model, and
where the answer sits in what comes back. Two recipes ship, and a third lets
someone describe a tool the harness has never heard of without changing code.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..models import HarnessError, ProviderRequest, ProviderResponse
from .base import Provider
from .codex_cli import _remaining, _run_bounded

SUBSCRIPTION_KINDS = ("claude-cli", "copilot-cli", "assistant-cli")
UNPRICED = "subscription-unpriced"


@dataclass(frozen=True)
class CliRecipe:
    """How to talk to one signed-in assistant on the command line."""

    id: str
    label: str
    command: tuple[str, ...]
    # Arguments placed after the command. "{model}" is replaced, and an argument
    # holding "{model}" is dropped when the request names no model.
    arguments: tuple[str, ...] = ()
    # Where the answer text sits in a JSON reply. Empty means the whole output
    # is the answer, as plain text.
    text_field: str = ""
    error_field: str = ""
    error_message_field: str = ""
    input_tokens_field: str = ""
    output_tokens_field: str = ""
    version_arguments: tuple[str, ...] = ("--version",)
    install_hint: str = ""
    verified: bool = False

    def argv(self, command: list[str], model: str) -> list[str]:
        built: list[str] = list(command)
        for argument in self.arguments:
            if "{model}" not in argument:
                built.append(argument)
                continue
            if model:
                built.append(argument.replace("{model}", model))
                continue
            # No model was asked for, so drop the value and the flag in front of
            # it. Leaving a bare "--model" behind would confuse the tool.
            if built and len(built) > len(command) and built[-1].startswith("-"):
                built.pop()
        return built


def _dotted(value: Any, path: str) -> Any:
    found: Any = value
    for part in path.split("."):
        if isinstance(found, Mapping) and part in found:
            found = found[part]
        elif isinstance(found, list) and part.isdigit() and int(part) < len(found):
            found = found[int(part)]
        else:
            return None
    return found


# Claude Code, signed in with a Claude subscription. The shape below is what the
# tool really prints with --output-format json, including an is_error flag that
# can be true while the subtype still says success.
CLAUDE_RECIPE = CliRecipe(
    id="claude-cli",
    label="Claude command line",
    command=("claude",),
    arguments=("-p", "--output-format", "json", "--model", "{model}"),
    text_field="result",
    error_field="is_error",
    error_message_field="result",
    input_tokens_field="usage.input_tokens",
    output_tokens_field="usage.output_tokens",
    install_hint=(
        "Install Claude Code and sign in with your subscription, then run: claude --version"
    ),
    verified=True,
)

# GitHub Copilot's command line. Its flags are read from config so a change in
# the tool does not need a change here.
COPILOT_RECIPE = CliRecipe(
    id="copilot-cli",
    label="GitHub Copilot command line",
    command=("copilot",),
    arguments=("-p", "--allow-all-tools", "--model", "{model}"),
    text_field="",
    install_hint=(
        "Install the GitHub Copilot command line tool and sign in, then run: copilot --version. "
        "If your version takes different arguments, set them in providers.<name>.arguments."
    ),
)

ASSISTANT_RECIPE = CliRecipe(
    id="assistant-cli",
    label="Another signed-in assistant",
    command=(),
    install_hint="Set providers.<name>.command and providers.<name>.arguments for your tool.",
)

RECIPES: dict[str, CliRecipe] = {
    CLAUDE_RECIPE.id: CLAUDE_RECIPE,
    COPILOT_RECIPE.id: COPILOT_RECIPE,
    ASSISTANT_RECIPE.id: ASSISTANT_RECIPE,
}


def recipe_for(kind: str) -> CliRecipe:
    if kind not in RECIPES:
        raise HarnessError(f"There is no built-in recipe for {kind}")
    return RECIPES[kind]


def available(kind: str, command: list[str] | None = None) -> str:
    """The full path of the tool when it is on this machine, else empty."""

    recipe = recipe_for(kind)
    parts = command or list(recipe.command)
    if not parts:
        return ""
    return shutil.which(parts[0]) or ""


def _prompt(request: ProviderRequest) -> str:
    """One plain prompt, because a command line tool takes text and nothing else."""

    sections = [
        "SYSTEM INSTRUCTIONS\n" + request.system_prefix,
        "DYNAMIC CONTEXT (UNTRUSTED DATA)\n" + request.dynamic_context,
    ]
    for message in request.messages:
        role = str(message.get("role", "user")).upper()
        sections.append(f"{role}\n{message.get('content', '')}")
    if request.response_format is not None:
        sections.append(
            "ANSWER FORMAT\n"
            "Answer with one JSON value that fits this schema, and nothing else. "
            "No explanation, no fenced block.\n"
            + json.dumps(request.response_format.schema, sort_keys=True)
        )
    return "\n\n".join(sections)


def _plain_text(raw: str) -> str:
    """The answer when the tool prints text rather than JSON."""

    text = raw.strip()
    fenced = re.findall(r"```(?:[a-zA-Z0-9_-]*)\r?\n(.*?)```", text, re.DOTALL)
    return (fenced[0].strip() if fenced else text)


class SubscriptionCLIProvider(Provider):
    """Drive a signed-in assistant's command line as an ordinary program."""

    def __init__(self, config, kind: str = "", settings: Mapping[str, Any] | None = None):  # type: ignore[no-untyped-def]
        super().__init__(config)
        chosen = kind or str(self.settings.get("kind") or self.settings.get("name") or "")
        self.recipe = recipe_for(chosen)
        self._checked = False

    def _command(self) -> list[str]:
        configured = self.settings.get("command") or list(self.recipe.command)
        if not isinstance(configured, list) or not configured:
            raise HarnessError(
                f"{self.recipe.label} needs a command. {self.recipe.install_hint}"
            )
        if any(not isinstance(part, str) or not part for part in configured):
            raise HarnessError(f"{self.recipe.label} command must be a list of words")
        parts = list(configured)
        # These tools are usually installed as a small wrapper script. On Windows
        # that wrapper is a .CMD file, which cannot be started by its bare name
        # without a shell, so the real path is looked up here instead.
        found = shutil.which(parts[0])
        if not found:
            raise HarnessError(
                f"{parts[0]} is not on this machine. {self.recipe.install_hint}"
            )
        parts[0] = found
        return parts

    def _arguments(self) -> CliRecipe:
        configured = self.settings.get("arguments")
        if configured is None:
            return self.recipe
        if not isinstance(configured, list) or any(not isinstance(part, str) for part in configured):
            raise HarnessError(f"{self.recipe.label} arguments must be a list of words")
        return CliRecipe(**{**self.recipe.__dict__, "arguments": tuple(configured)})

    def _preflight(self, command: list[str], deadline_at: float) -> None:
        if self._checked:
            return
        result = _run_bounded(
            [*command, *self.recipe.version_arguments],
            cwd=Path.cwd(),
            stdin_text=None,
            timeout_seconds=min(30.0, _remaining(deadline_at)),
            max_output_bytes=32_000,
        )
        if result.timed_out or result.exit_code != 0:
            detail = self._redactor.text((result.stderr or result.stdout).strip()[:500])
            raise HarnessError(
                f"{self.recipe.label} did not answer when asked for its version. {detail}"
            )
        self._checked = True

    @staticmethod
    def _reject_native_contract(request: ProviderRequest) -> None:
        if (
            request.tools
            or request.responses_continuation
            or request.function_call_outputs
            or request.chat_continuation
            or request.chat_function_call_outputs
            or request.native_continuation
            or request.native_function_call_outputs
        ):
            raise HarnessError(
                "A command line assistant answers one prompt at a time, with no tool calls"
            )

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self._reject_native_contract(request)
        recipe = self._arguments()
        command = self._command()
        timeout = self._timeout(request.timeout_seconds)
        deadline_at = time.monotonic() + timeout
        self._preflight(command, deadline_at)
        output_limit = min(2_000_000, int(self.config.get("execution.max_output_bytes")))
        argv = recipe.argv(command, str(request.model or ""))
        started = time.monotonic()
        result = _run_bounded(
            argv,
            cwd=Path.cwd(),
            stdin_text=self._redactor.text(_prompt(request)),
            timeout_seconds=_remaining(deadline_at),
            max_output_bytes=output_limit,
        )
        if result.timed_out:
            raise HarnessError(f"{recipe.label} ran past its {timeout:g} second limit")
        if result.output_truncated:
            raise HarnessError(f"{recipe.label} printed more than the {output_limit} byte limit")
        if result.exit_code != 0:
            detail = self._redactor.text((result.stderr or result.stdout).strip()[:4_000])
            raise HarnessError(f"{recipe.label} stopped with code {result.exit_code}. {detail}")
        return self._read_answer(recipe, result.stdout, result.stderr, started)

    def _read_answer(
        self, recipe: CliRecipe, stdout: str, stderr: str, started: float
    ) -> ProviderResponse:
        latency = max(0, int((time.monotonic() - started) * 1000))
        if not recipe.text_field:
            text = _plain_text(stdout)
            if not text:
                raise HarnessError(f"{recipe.label} answered with nothing")
            return ProviderResponse(
                text=text,
                finish_reason="stop",
                raw={"tool": recipe.id, "price_status": UNPRICED, "latency_ms": latency},
            )
        try:
            body = json.loads(stdout.strip() or "{}")
        except json.JSONDecodeError as exc:
            raise HarnessError(f"{recipe.label} did not answer with JSON: {exc.msg}") from exc
        if recipe.error_field and _dotted(body, recipe.error_field) is True:
            said = _dotted(body, recipe.error_message_field) if recipe.error_message_field else ""
            raise HarnessError(
                f"{recipe.label} refused the request: {self._redactor.text(str(said or 'no reason given'))}"
            )
        text = _dotted(body, recipe.text_field)
        if not isinstance(text, str) or not text.strip():
            raise HarnessError(
                f"{recipe.label} answered without a {recipe.text_field} field holding text"
            )
        return ProviderResponse(
            text=_plain_text(text),
            finish_reason="stop",
            input_tokens=_whole(_dotted(body, recipe.input_tokens_field)),
            output_tokens=_whole(_dotted(body, recipe.output_tokens_field)),
            raw={"tool": recipe.id, "price_status": UNPRICED, "latency_ms": latency},
        )


def _whole(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, int(value))
