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
    # How to ask the tool about its own sign-in, and what to try when it is
    # signed in and the request is still turned down. Only ever run on the way
    # to an error message, so it costs nothing on a working machine.
    signed_in_arguments: tuple[str, ...] = ()
    when_it_is_refused: str = ""
    install_hint: str = ""
    verified: bool = False

    def check(self) -> None:
        """Refuse an argument list the harness could not honour.

        A "{model}" argument only makes sense in one of two shapes: on its own
        after a flag, or joined to a flag with an equals sign. Anything else
        cannot be dropped cleanly when a request names no model, and silently
        losing an argument is worse than saying so.
        """

        for position, argument in enumerate(self.arguments):
            if "{model}" not in argument:
                continue
            if argument == "{model}":
                if position == 0 or not self.arguments[position - 1].startswith("-"):
                    raise HarnessError(
                        f"{self.label}: a {{model}} argument must come straight after a flag, "
                        "such as --model"
                    )
                continue
            if not (argument.startswith("-") and "=" in argument):
                raise HarnessError(
                    f"{self.label}: {argument} holds {{model}} in a shape the harness cannot "
                    "drop when no model is asked for. Write it as --flag {{model}} or "
                    "--flag={{model}}."
                )

    def argv(self, command: list[str], model: str) -> list[str]:
        self.check()
        built: list[str] = list(command)
        for argument in self.arguments:
            if "{model}" not in argument:
                built.append(argument)
                continue
            if model:
                built.append(argument.replace("{model}", model))
                continue
            # No model was asked for. A joined --flag={model} goes on its own;
            # a bare {model} takes the flag in front of it too, because a
            # dangling flag would confuse the tool.
            if argument == "{model}" and built and len(built) > len(command):
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
    signed_in_arguments=("auth", "status"),
    when_it_is_refused=(
        "A tool that is signed in and still turned down usually has no token of "
        "its own for work nobody is watching. Run: claude setup-token. If that "
        "is not allowed either, whoever administers your organisation has to "
        "turn Claude Code on for it - being able to use Claude in a window of "
        "its own is not the same permission."
    ),
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


# How much of a tool's own reason for refusing is worth reading. Far above a
# real sentence, far below a page.
LONGEST_REASON = 4000


class SubscriptionCLIProvider(Provider):
    """Drive a signed-in assistant's command line as an ordinary program."""

    def __init__(self, config, kind: str = "", settings: Mapping[str, Any] | None = None):  # type: ignore[no-untyped-def]
        super().__init__(config)
        chosen = kind or str(self.settings.get("kind") or self.settings.get("name") or "")
        self.recipe = recipe_for(chosen)
        self._checked = False
        # What the last call ran, and by when it had to be done. Kept so the
        # error path can ask the very tool that was run, inside the time the
        # caller allowed, rather than looking it up again and helping itself to
        # more.
        self._asked_with: list[str] = []
        self._deadline: float | None = None

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
        recipe = CliRecipe(**{**self.recipe.__dict__, "arguments": tuple(configured)})
        recipe.check()
        return recipe

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
        # Kept so the error path does not look the tool up on the disk a second
        # time, and so it asks the very tool that was run.
        self._asked_with = command
        timeout = self._timeout(request.timeout_seconds)
        deadline_at = time.monotonic() + timeout
        self._deadline = deadline_at
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
            # Some of these tools say plainly why they would not answer and then
            # exit non-zero anyway. That sentence is the part somebody can read,
            # so it is used instead of the exit code and the page of JSON that
            # came with it - which is what was being shown, and told nobody
            # anything.
            said = self._why_it_would_not(recipe, result.stdout, result.stderr)
            if said:
                # The label is left off the front on purpose: whoever asked puts
                # the name of the route in front of this, and "claude was asked
                # and did not answer: the Claude command line would not answer"
                # is the same thing said twice.
                raise HarnessError(
                    f"{said}{self._and_what_it_says_about_itself(recipe, deadline_at)}"
                )
            # Nothing in what it printed reads as a reason, so the code it
            # stopped with is the most anybody can be told - and the last few
            # words of what it printed, at the end and kept short. In front of
            # the sentence and at full length, it was a page of machine output
            # where the reason should be, which is what this was written to stop.
            raise HarnessError(
                f"{recipe.label} stopped with code {result.exit_code}, and nothing "
                f"it printed says why."
                f"{self._and_what_it_says_about_itself(recipe, deadline_at)}"
                f" It printed: {self._just_a_glimpse(result.stderr or result.stdout)}"
            )
        return self._read_answer(recipe, result.stdout, result.stderr, started)

    def _why_it_would_not(self, recipe: CliRecipe, stdout: str, stderr: str = "") -> str:
        """The reason the tool gave, if it gave one anywhere in what it printed.

        Looked for in every object it printed, not only in a whole answer that is
        one object and nothing else. These tools print a line of progress, or a
        banner, or one object per line, and any of those left this finding
        nothing - which dropped the reader into the message that says only what
        code it stopped with.

        The last object wins: a tool that says one thing and then a better one
        is telling you the second.
        """

        if not (recipe.error_field and recipe.error_message_field):
            return ""
        for body in reversed(list(_every_object_in(f"{stdout}\n{stderr}"))):
            if _dotted(body, recipe.error_field) is not True:
                continue
            said = _dotted(body, recipe.error_message_field)
            if isinstance(said, str) and said.strip():
                return self._redactor.text(" ".join(said.split()))[:LONGEST_REASON]
        return ""

    def _just_a_glimpse(self, said: str) -> str:
        """The first few words of what a tool printed, and no more.

        Enough to recognise, short enough to stay a sentence. The whole of it
        belongs in a log, not in the one line somebody reads.
        """

        held = self._redactor.text(" ".join(said.split()))
        return held[:200] + ("..." if len(held) > 200 else "") if held else "nothing at all"

    def _and_what_it_says_about_itself(
        self, recipe: CliRecipe, deadline_at: float | None = None
    ) -> str:
        """What else the harness knows, tacked onto a refusal.

        The tool's own sentence, read on its own, can say the wrong thing:
        "your organization does not have access to Claude" is what somebody
        sees while looking at a working Claude window. What the harness knows
        and was not saying is that the tool is on this machine, that it
        answered, and what it says about its own sign-in - which together move
        the question from "have I got this at all" to "this one thing is not
        allowed", and those take you to different places.

        Anything that goes wrong while asking is left out rather than piled on
        top: this is already an error message, and a second failure inside it
        helps nobody.
        """

        said = ["", (
            f"The {recipe.label} is on this machine and did answer, so nothing "
            "failed to reach it - what turned this down was the service behind it."
        )]
        about = self._how_it_describes_its_sign_in(recipe, deadline_at)
        if about:
            said.append(f"It says of itself: {about}.")
        if recipe.when_it_is_refused:
            said.append(recipe.when_it_is_refused)
        return " ".join(said)

    def _how_it_describes_its_sign_in(
        self, recipe: CliRecipe, deadline_at: float | None = None
    ) -> str:
        if not recipe.signed_in_arguments:
            return ""
        # Inside the time the caller allowed for the whole thing, not on top of
        # it. Given its own fifteen seconds, a call told to take no more than
        # five could take twenty - and the extra was spent on an explanation,
        # after the work had already failed.
        left = 10.0 if deadline_at is None else min(10.0, _remaining(deadline_at))
        if left <= 0.5:
            return ""
        try:
            result = _run_bounded(
                [*(self._asked_with or self._command()), *recipe.signed_in_arguments],
                cwd=Path.cwd(),
                stdin_text=None,
                timeout_seconds=left,
                max_output_bytes=32_000,
            )
        except HarnessError:
            return ""
        if result.timed_out or result.exit_code != 0:
            return ""
        return self._redactor.text(_in_a_few_words(result.stdout))[:300]

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
            # Cut to a length a person reads. Every other way of building one of
            # these caps what it holds; this one did not, so a tool that answers
            # with a page of detail put a page of detail in a sentence.
            why = self._redactor.text(str(said or "no reason given"))[:LONGEST_REASON]
            raise HarnessError(
                f"{recipe.label} refused the request: {why}"
                f"{self._and_what_it_says_about_itself(recipe, self._deadline)}"
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


def _every_object_in(said: str):
    """Every JSON object in what a tool printed, in the order they appear.

    A whole line at a time first, which is how a tool that prints one object per
    line reads. Then the whole of it, for a tool that prints one object across
    several lines. A banner, a line of progress or a word at the end is stepped
    over rather than throwing the reason away with it.
    """

    seen = []
    for line in said.splitlines():
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            held = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(held, dict):
            seen.append(held)
    if seen:
        return seen
    start = said.find("{")
    end = said.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        held = json.loads(said[start:end + 1])
    except json.JSONDecodeError:
        return []
    return [held] if isinstance(held, dict) else []


def _in_a_few_words(said: str) -> str:
    """What a tool printed about itself, as one line somebody can read.

    These tools answer with JSON, and the whole of it in the middle of a
    sentence is worse than none of it. The few fields that say who is signed in
    are picked out by name; anything else falls back to the first line.
    """

    try:
        held = json.loads(said.strip() or "{}")
    except json.JSONDecodeError:
        held = None
    if not isinstance(held, dict):
        return " ".join(said.split())[:300]
    words = []
    # Named one at a time on purpose, and nothing here holds a secret. A field
    # called authMethod was read at first: it said "claude.ai", which told
    # nobody anything, and the same name on another tool holds a session.
    for name in ("loggedIn", "email", "orgName", "subscriptionType",
                 "account", "user", "plan", "status"):
        found = held.get(name)
        if isinstance(found, bool):
            words.append("signed in" if found else "not signed in")
        elif isinstance(found, (str, int)) and str(found).strip():
            words.append(str(found).strip())
    return ", ".join(words) or " ".join(said.split())[:300]


def _whole(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, int(value))
