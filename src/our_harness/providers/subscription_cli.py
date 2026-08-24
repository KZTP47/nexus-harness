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
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..models import HarnessError, ProviderRequest, ProviderResponse
from .base import Provider
from .codex_cli import _minimal_codex_environment, _remaining, _run_bounded

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
    # For a tool that says something went wrong by putting an object where the
    # answer would be, rather than by setting a flag to true. Anything at all
    # under this name is a refusal.
    error_when_present: str = ""
    error_message_field: str = ""
    input_tokens_field: str = ""
    output_tokens_field: str = ""
    version_arguments: tuple[str, ...] = ("--version",)
    # Where else this tool might be at all, as patterns to look under. Not the
    # same as the next one: this is for a tool that is never on the path, where
    # the question is whether it is on the machine, and any copy will do.
    # Looked at only when the path has nothing, so a copy somebody put there on
    # purpose still wins.
    also_found_at: tuple[str, ...] = ()
    # Where else this tool keeps builds of itself, as patterns to look under.
    # A tool that updates itself often has a newer copy somewhere its installer
    # never put on the path, and the older one on the path can behave quite
    # differently - see _the_newest_build_of below.
    kept_under: tuple[str, ...] = ()
    # How to ask the tool about its own sign-in, and what to try when it is
    # signed in and the request is still turned down. Only ever run on the way
    # to an error message, so it costs nothing on a working machine.
    signed_in_arguments: tuple[str, ...] = ()
    # A fixed, interactive command the app may open only after the person
    # presses Sign in. None means this CLI has no supported login flow here;
    # an empty tuple means running the command itself opens that flow.
    interactive_login_arguments: tuple[str, ...] | None = None
    when_it_is_refused: str = ""
    # When the service's own answer already names what would fix it. Words to
    # look for in what it said, and what to say instead of guessing - because a
    # guess offered next to an answer that spelt it out is a few more minutes
    # somebody spends going the wrong way.
    the_answer_names_it: tuple[str, ...] = ()
    when_the_answer_names_it: str = ""
    # Where the tool says how long the service took, and what to say when that
    # is nothing at all. Nothing at all means it never asked: it turned the
    # request down here, out of what it has written down about the account, and
    # the two cases send somebody to completely different places.
    time_at_the_service_field: str = ""
    # Where the tool puts the status the service answered with. A number here is
    # proof something answered, whatever the timing says, and the timing on this
    # machine says nothing at all - it reads zero for an answer that really did
    # come back from the service.
    service_status_field: str = ""
    when_it_never_asked: str = ""
    # What to say when the tool does not say whether it asked anybody. Neither
    # of the other two can be claimed then, and claiming one is how somebody
    # ends up asking an administrator about something that never left their own
    # machine.
    when_it_is_not_clear: str = ""
    # Things this tool needs handed to it, as the name of the environment
    # variable it reads and the setting it comes from. Handed over on purpose:
    # everything else is stripped, so nothing arrives because it happened to be
    # set on the machine.
    needs_handing_over: tuple[tuple[str, str], ...] = ()
    # The environment variable this tool reads a key from, for somebody who has
    # a key and means to use it. Nothing is passed unless a route names the
    # variable to take it from - a subscription tool handed a key quietly starts
    # spending money nobody decided to spend.
    key_it_reads: str = ""
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
    interactive_login_arguments=("auth", "login"),
    key_it_reads="ANTHROPIC_API_KEY",
    # The desktop app keeps its own copy of Claude Code and updates it, while an
    # npm install months ago sits on the path never changing. Both are here on
    # this machine, and they do not answer the same way.
    kept_under=(
        "LOCALAPPDATA/Packages/Claude_*/LocalCache/Roaming/Claude/claude-code/*/claude.exe",
        "APPDATA/Claude/claude-code/*/claude.exe",
        "LOCALAPPDATA/Claude/claude-code/*/claude.exe",
        "HOME/.claude/claude-code/*/claude",
    ),
    time_at_the_service_field="duration_api_ms",
    service_status_field="api_error_status",
    when_it_never_asked=(
        "Its saved sign-in needs attention. Finish anything open in Claude first, "
        "then run: claude auth logout, claude update, and claude auth login. Pick "
        "Claude account with subscription. If a direct claude -p request still "
        "gets 403 afterwards, contact Anthropic support; the desktop app working "
        "does not repair a stale command-line OAuth entitlement."
    ),
    when_it_is_refused=(
        "Claude is installed and signed in, but this non-interactive request was "
        "rejected. Finish anything open in Claude first, then run: claude auth "
        "logout, claude update, and claude auth login. Pick Claude account with "
        "subscription. If a direct claude -p request still fails, contact "
        "Anthropic support. An API key is a separate, paid route and is never "
        "selected automatically."
    ),
    # Only what this refusal actually says, word for word. "ask your admin" on
    # its own turns up in plenty of others - a rate limit says it - and
    # answering one of those with "there is nothing to try again here" sends
    # somebody away from a wait that would have fixed it in a minute.
    the_answer_names_it=("disabled claude subscription access",),
    when_the_answer_names_it=(
        "This only says that Anthropic rejected the command line's subscription "
        "OAuth request. It does not prove that Claude is missing, that the desktop "
        "app is signed out, or that an administrator deliberately disabled it. "
        "The same 403 is also reported for stale account-entitlement records while "
        "interactive Claude still works. Finish anything open in Claude first, "
        "then run: claude auth logout, claude update, and claude auth login, and "
        "pick Claude account with subscription. If a direct claude -p request "
        "still fails, contact Anthropic support. An API key would be separately "
        "billed and is never selected automatically."
    ),
    when_it_is_not_clear=(
        "Finish anything open in Claude first, then run: claude auth logout, "
        "claude update, and claude auth login. Pick Claude account with "
        "subscription. If a direct claude -p request still fails afterwards, "
        "contact Anthropic support."
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
    key_it_reads="GH_TOKEN",
    command=("copilot",),
    arguments=("-p", "--allow-all-tools", "--model", "{model}"),
    text_field="",
    install_hint=(
        "Install the GitHub Copilot command line tool and sign in, then run: "
        "copilot --version. It comes from npm: npm install -g @github/copilot. "
        "This is GitHub Copilot, which has a command line. Microsoft 365 Copilot "
        "is a different product with none, so there is nothing here for the "
        "harness to drive - a seat for it does not make this route work. "
        "If your version takes different arguments, set them in "
        "providers.<name>.arguments."
    ),
)

ASSISTANT_RECIPE = CliRecipe(
    id="assistant-cli",
    label="Another signed-in assistant",
    command=(),
    install_hint="Set providers.<name>.command and providers.<name>.arguments for your tool.",
)


# Google's Gemini command line. Signs in with a Google account, which is what
# somebody with Antigravity or a Code Assist seat already has - no key needed,
# and none accepted unless one is asked for by name.
GEMINI_RECIPE = CliRecipe(
    id="gemini-cli",
    label="Gemini command line",
    command=("gemini",),
    # The prompt goes in on standard input, which this reads when there is
    # anything there. Nothing is auto-approved and no tools are allowed: this is
    # a conversation, and a conversation that can change files is not one.
    arguments=("--output-format", "json", "--approval-mode", "default", "--model", "{model}"),
    text_field="response",
    # It says so with an object rather than a flag.
    error_when_present="error",
    error_message_field="error.message",
    input_tokens_field="stats.models.*.tokens.prompt",
    output_tokens_field="stats.models.*.tokens.candidates",
    # A Workspace account will not be answered at all until it names a Cloud
    # project, and the message Google sends for that is a link and a shrug.
    needs_handing_over=(
        ("GOOGLE_CLOUD_PROJECT", "google_project"),
    ),
    key_it_reads="GEMINI_API_KEY",
    interactive_login_arguments=(),
    when_it_is_refused=(
        "Signed in and still turned down usually means the account has no "
        "Gemini to give. A personal Google account gets some for free; a work "
        "one needs a Gemini Code Assist seat, which somebody who administers "
        "your organisation hands out."
    ),
    the_answer_names_it=("google_cloud_project", "goo.gle/gemini-cli-auth-docs"),
    when_the_answer_names_it=(
        "Google will not answer this account until it is told which Cloud "
        "project to bill the work to. It is not a sign-in problem and signing "
        "in again will not help. Put the project id in this route's settings as "
        "google_project. Whoever set up your Google Workspace knows it, and it "
        "is on the front page of the Google Cloud console."
    ),
    when_it_never_asked=(
        "It turned this down here, without asking Google. Run: gemini, and see "
        "what it says on the way in - it is usually waiting to be signed in."
    ),
    when_it_is_not_clear=(
        "Run: gemini, on its own, and see what it says. It asks for whatever it "
        "is missing on the way in, which is quicker than guessing from here."
    ),
    install_hint=(
        "Install the Gemini command line and sign in with your Google account, "
        "then run: gemini --version. It comes from npm: "
        "npm install -g @google/gemini-cli. A Google account is all it needs - "
        "no key. If yours is a work account, it will also want a Cloud project "
        "id in this route's settings as google_project."
    ),
    verified=True,
)


# Codex, described here so it can be found and picked like the others.
#
# Only for finding it and saying what it is. Codex has its own way of being
# talked to, in codex_cli.py, which came first and is far better tested than
# this one - and that is still what does the talking. What was missing was
# never the talking: it was that the app could not find Codex at all, because
# its desktop app keeps it in a folder of its own and never puts it on the path.
CODEX_RECIPE = CliRecipe(
    id="codex-cli",
    label="Codex command line",
    key_it_reads="OPENAI_API_KEY",
    command=("codex",),
    also_found_at=(
        "LOCALAPPDATA/Packages/OpenAI.Codex_*/LocalCache/Local/OpenAI/Codex/bin/codex.exe",
        "LOCALAPPDATA/Programs/codex/codex.exe",
        "HOME/.codex/bin/codex",
    ),
    signed_in_arguments=("login", "status"),
    interactive_login_arguments=("login",),
    install_hint=(
        "Install Codex and sign in with your ChatGPT account, then run: "
        "codex --version. It comes with the Codex desktop app, or from npm: "
        "npm install -g @openai/codex. A ChatGPT subscription is enough - no key."
    ),
    verified=True,
)

RECIPES: dict[str, CliRecipe] = {
    CLAUDE_RECIPE.id: CLAUDE_RECIPE,
    COPILOT_RECIPE.id: COPILOT_RECIPE,
    ASSISTANT_RECIPE.id: ASSISTANT_RECIPE,
    GEMINI_RECIPE.id: GEMINI_RECIPE,
    CODEX_RECIPE.id: CODEX_RECIPE,
}

_connection_cache_lock = threading.Lock()
_connection_cache: dict[tuple[str, str, int], tuple[float, dict[str, Any]]] = {}
CONNECTION_STATUS_CACHE_SECONDS = 60.0


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
    found = shutil.which(parts[0])
    # Desktop applications often keep the current build in their own data
    # folder. It is still an installed command even when no launcher was put on
    # PATH. Prefer the newest known build for discovery for the same reason the
    # provider prefers it when a request is sent.
    kept = _every_build_of(recipe.kept_under)
    if kept:
        newest, newest_version = max(kept, key=lambda one: one[1])
        if not found or newest_version > _the_version_of(found):
            return str(newest)
    if found:
        return found
    # Not on the path is not the same as not here. Codex is installed by its own
    # desktop app, into a folder of its own, and nothing ever puts it on the
    # path - so this said it was not on the machine while it sat there signed
    # in, which is a wrong answer that sends somebody off installing what they
    # already have.
    somewhere = _where_else_it_might_be(recipe.also_found_at)
    return str(somewhere[0]) if somewhere else ""


def connection_status(
    kind: str,
    timeout_seconds: float = 10.0,
    *,
    use_cache: bool = False,
    probe: bool = True,
) -> dict[str, Any]:
    """Installed and CLI-authenticated are reported as different facts.

    Opening a provider's desktop app proves neither that its separate command
    line OAuth session exists nor that non-interactive subscription calls are
    entitled.  This asks only the CLI's local status command; it sends no model
    prompt and returns none of the account details that command may print.
    """

    recipe = recipe_for(kind)
    program = available(kind)
    try:
        changed = Path(program).stat().st_mtime_ns if program else 0
    except OSError:
        changed = 0
    cache_key = (kind, program, changed)
    if use_cache:
        with _connection_cache_lock:
            remembered = _connection_cache.get(cache_key)
        if remembered and time.monotonic() - remembered[0] < CONNECTION_STATUS_CACHE_SECONDS:
            return dict(remembered[1])

    def done(result: dict[str, Any]) -> dict[str, Any]:
        # Fresh explicit checks populate the cache; ordinary page refreshes may
        # then reuse the fact without starting provider CLIs over and over.
        with _connection_cache_lock:
            _connection_cache[cache_key] = (time.monotonic(), dict(result))
        return result

    base = {
        "kind": kind,
        "installed": bool(program),
        "authentication": "unknown",
        "can_login": recipe.interactive_login_arguments is not None,
    }
    if not program:
        return done(dict(base, state="not-installed"))
    if not recipe.signed_in_arguments:
        return done(dict(base, state="installed"))
    if not probe:
        return dict(base, state="installed")
    try:
        result = _run_bounded(
            [program, *recipe.signed_in_arguments],
            cwd=Path.cwd(),
            stdin_text=None,
            timeout_seconds=max(1.0, min(float(timeout_seconds), 15.0)),
            max_output_bytes=32_000,
        )
    except HarnessError:
        return done(dict(base, state="installed"))
    if result.timed_out:
        return done(dict(base, state="installed"))
    answer = f"{result.stdout}\n{result.stderr}".lower()
    compact = answer.replace(" ", "")
    negative = (
        '"loggedin":false', '"logged_in":false', "not logged in",
        "not signed in", "login required", "authentication required",
    )
    positive = (
        '"loggedin":true', '"logged_in":true', "logged in", "signed in",
        "chatgpt",
    )
    if any(mark in compact for mark in negative[:2]) or any(
            mark in answer for mark in negative[2:]):
        authentication = "signed-out"
    elif result.exit_code == 0 and (
            not answer.strip() or any(mark in compact for mark in positive[:2])
            or any(mark in answer for mark in positive[2:])):
        authentication = "signed-in"
    elif result.exit_code != 0:
        authentication = "signed-out"
    else:
        authentication = "unknown"
    return done(dict(
        base,
        authentication=authentication,
        state=("authenticated" if authentication == "signed-in" else (
            "needs-login" if authentication == "signed-out" else "installed"
        )),
    ))


def start_interactive_login(kind: str) -> dict[str, Any]:
    """Open the provider's own login flow after an explicit button press.

    Credentials stay between the provider CLI and its service.  The harness
    neither asks for them nor captures the terminal's output.
    """

    recipe = recipe_for(kind)
    arguments = recipe.interactive_login_arguments
    if arguments is None:
        raise HarnessError(
            f"{recipe.label} has no login command this app can safely open. "
            "Open its command line yourself and follow its sign-in instructions."
        )
    program = available(kind)
    if not program:
        raise HarnessError(f"{recipe.label} is not installed. {recipe.install_hint}")
    command = [program, *arguments]
    if os.name != "nt":
        raise HarnessError(
            f"Open a terminal and run {Path(program).name} "
            f"{' '.join(arguments)}. The app can open the interactive login "
            "window automatically on Windows."
        )
    # .cmd launchers need cmd.exe, while a real executable starts directly.
    # Both are fixed commands from the built-in recipe; no user text is placed
    # in this command line.
    if Path(program).suffix.lower() in {".cmd", ".bat"}:
        command = [
            os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c",
            subprocess.list2cmdline([program, *arguments]),
        ]
    try:
        process = subprocess.Popen(
            command,
            cwd=Path.cwd(),
            env=_minimal_codex_environment(),
            stdin=None,
            stdout=None,
            stderr=None,
            shell=False,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    except OSError as exc:
        raise HarnessError(f"The {recipe.label} login window could not open: {exc}") from exc
    return {
        "opened": True,
        "kind": kind,
        "note": (
            f"The {recipe.label} sign-in opened in its own terminal. Finish it "
            "there, then come back and send the message again."
        ),
        # Useful only to establish that a process really started. Never an
        # executable path, command, account name, or token.
        "process": int(process.pid),
    }


def start_claude_repair() -> dict[str, Any]:
    """Open Claude's own update/logout/login repair in a visible terminal.

    The repair deliberately stays outside this process.  Nexus neither reads
    the terminal nor receives the account page, cookies, credentials, or the
    provider's output.  Logging out is consequential, so the panel asks for a
    confirmation before it calls this function.
    """

    kind = "claude-cli"
    recipe = recipe_for(kind)
    program = available(kind)
    if not program:
        raise HarnessError(f"{recipe.label} is not installed. {recipe.install_hint}")
    if os.name != "nt":
        raise HarnessError(
            "Open a terminal, run 'claude update', then 'claude auth logout', "
            "then 'claude auth login'. Nexus can open this repair automatically "
            "on Windows."
        )

    # Update before logging out.  If updating cannot start, the fixed `&&`
    # chain stops and leaves the existing account session alone.  Every word
    # here is built in; no request text or setting is inserted into a shell.
    repairs = (
        [program, "update"],
        [program, "auth", "logout"],
        [program, "auth", "login"],
    )
    chain = " && ".join(subprocess.list2cmdline(one) for one in repairs)
    command = [
        os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/k", chain,
    ]
    try:
        process = subprocess.Popen(
            command,
            cwd=Path.cwd(),
            env=_minimal_codex_environment(),
            stdin=None,
            stdout=None,
            stderr=None,
            shell=False,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    except OSError as exc:
        raise HarnessError(f"The Claude repair window could not open: {exc}") from exc
    return {
        "opened": True,
        "kind": kind,
        "note": (
            "Claude's repair opened in its own terminal. It updates Claude, "
            "signs the command line out, and opens Claude's fresh sign-in. "
            "Finish there, then try the message again. Nexus cannot see the "
            "sign-in details or credentials."
        ),
        "process": int(process.pid),
    }


def _where_else_it_might_be(patterns: tuple[str, ...]) -> list[Path]:
    """Every copy of a tool found under those patterns, newest first.

    Newest by when it was last written, because these have no version in the
    folder name to go by - unlike the builds a tool keeps of itself, which do.
    """

    found: list[Path] = []
    for pattern in patterns:
        name, _, rest = pattern.partition("/")
        base = str(Path.home()) if name == "HOME" else (
            os.environ.get(name) if name.isupper() else None)
        if not base:
            continue
        try:
            found.extend(one for one in Path(base).glob(rest) if one.is_file())
        except OSError:
            continue
    return sorted(found, key=lambda one: one.stat().st_mtime, reverse=True)


def _prompt(request: ProviderRequest) -> str:
    """One plain prompt, because a command line tool takes text and nothing else."""

    sections = [
        "SYSTEM INSTRUCTIONS\n" + request.system_prefix,
        "DYNAMIC CONTEXT (UNTRUSTED DATA)\n" + request.dynamic_context,
    ]
    for message in request.messages:
        role = str(message.get("role", "user")).upper()
        sections.append(f"{role}\n{message.get('content', '')}")
    images = [
        str(one.get("path") or "") for one in request.attachments
        if isinstance(one, dict)
        and str(one.get("type") or "").startswith("image/")
        and str(one.get("path") or "")
    ]
    if images:
        sections.append(
            "USER-SELECTED IMAGE ATTACHMENTS\n"
            "Inspect these exact files as part of the user's message:\n"
            + "\n".join(images)
        )
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
            # Not on the path is not the same as not here.
            somewhere = _where_else_it_might_be(self.recipe.also_found_at)
            found = str(somewhere[0]) if somewhere else ""
        if not found and self.recipe.kept_under:
            kept = _every_build_of(self.recipe.kept_under)
            found = str(max(kept, key=lambda one: one[1])[0]) if kept else ""
        if not found:
            raise HarnessError(
                f"{parts[0]} is not on this machine. {self.recipe.install_hint}"
            )
        parts[0] = found
        newer = self._a_newer_build_than(found)
        if newer is not None:
            parts[0] = str(newer)
        return parts

    def _a_newer_build_than(self, found: str) -> Path | None:
        """A newer copy of this tool than the one on the path, if there is one.

        Only when nobody named a command themselves: somebody who wrote down
        which program to run meant that one.
        """

        if self.settings.get("command") or not self.recipe.kept_under:
            return None
        # Look for the other builds first. On a machine that has only the one,
        # which is most of them, this is where it stops - before asking the
        # program its version, which means starting it. That question is asked
        # on the way to every single message, so it has to be worth asking.
        elsewhere = _every_build_of(self.recipe.kept_under)
        if not elsewhere:
            return None
        best_where, best_version = Path(found), _the_version_of(found)
        for where, version in elsewhere:
            if version > best_version:
                best_where, best_version = where, version
        return None if best_where == Path(found) else best_where

    def _what_it_is_handed(self, recipe: CliRecipe) -> dict[str, str]:
        """What this route means to give the tool, and nothing else.

        Everything a subscription tool does not need is stripped before it runs,
        keys included, because a key that arrives because it happened to be set
        on the machine is a key nobody decided to spend. These are the ones
        somebody wrote down.
        """

        handed: dict[str, str] = {}
        for variable, setting in recipe.needs_handing_over:
            value = str(self.settings.get(setting) or "").strip()
            if value:
                handed[variable] = value
        # A key, only when a route names where to read it from. Named and empty
        # is a mistake worth saying out loud: somebody meant to use a key, and
        # letting it fall back to the subscription silently is how a route ends
        # up doing something other than what it says.
        from_where = str(self.settings.get("api_key_env") or "").strip()
        if from_where:
            if not recipe.key_it_reads:
                raise HarnessError(
                    f"{recipe.label} cannot be given a key: it signs in instead. "
                    "Leave api_key_env empty for this one."
                )
            key = os.environ.get(from_where, "")
            if not key:
                raise HarnessError(
                    f"{recipe.label} is set to use a key from {from_where}, and "
                    f"{from_where} is not set on this machine. Set it, or clear "
                    "api_key_env to go back to signing in."
                )
            handed[recipe.key_it_reads] = key
        return handed

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
        # A version banner is only a diagnostic, not the work somebody asked
        # the assistant to do.  Gemini CLI can wait for account setup even for
        # ``--version`` on Windows while the real non-interactive request still
        # starts and returns the useful setup error.  Treating that banner as a
        # gate hid the real answer behind a thirty-second, misleading failure.
        # Keep the probe short and advisory; the actual request below remains
        # bounded by the caller's deadline and is the authoritative check.
        result = _run_bounded(
            [*command, *self.recipe.version_arguments],
            cwd=Path.cwd(),
            stdin_text=None,
            timeout_seconds=min(3.0, _remaining(deadline_at)),
            max_output_bytes=32_000,
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
        image_paths = [
            str(one.get("path") or "") for one in request.attachments
            if isinstance(one, dict)
            and str(one.get("type") or "").startswith("image/")
            and str(one.get("path") or "")
        ]
        if image_paths and recipe.id not in {"claude-cli", "gemini-cli", "copilot-cli"}:
            raise HarnessError(
                f"{recipe.label} has no declared screenshot-input contract. "
                "Use a Claude, Codex, Gemini, Copilot, API, or vision-capable Ollama route."
            )
        if image_paths and recipe.id == "claude-cli":
            argv.extend(["--tools", "Read", "--allowedTools", "Read"])
            for folder in sorted({str(Path(path).parent) for path in image_paths}):
                argv.extend(["--add-dir", folder])
        elif image_paths and recipe.id == "gemini-cli":
            argv.extend(["--allowed-tools", "read_file"])
            for folder in sorted({str(Path(path).parent) for path in image_paths}):
                argv.extend(["--include-directories", folder])
        started = time.monotonic()
        result = _run_bounded(
            argv,
            cwd=Path.cwd(),
            stdin_text=self._redactor.text(_prompt(request)),
            timeout_seconds=_remaining(deadline_at),
            max_output_bytes=output_limit,
            also_in_the_environment=self._what_it_is_handed(recipe),
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
            asked = self._did_it_ask_anybody(recipe, result.stdout, result.stderr)
            if said:
                # The label is left off the front on purpose: whoever asked puts
                # the name of the route in front of this, and "claude was asked
                # and did not answer: the Claude command line would not answer"
                # is the same thing said twice.
                raise HarnessError(
                    f"{said}"
                    f"{self._and_what_it_says_about_itself(recipe, deadline_at, asked, said)}"
                )
            # Nothing in what it printed reads as a reason, so the code it
            # stopped with is the most anybody can be told - and the last few
            # words of what it printed, at the end and kept short. In front of
            # the sentence and at full length, it was a page of machine output
            # where the reason should be, which is what this was written to stop.
            raise HarnessError(
                f"{recipe.label} stopped with code {result.exit_code}, and nothing "
                f"it printed says why."
                f"{self._and_what_it_says_about_itself(recipe, deadline_at, asked)}"
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

        if not recipe.error_message_field:
            return ""
        if not (recipe.error_field or recipe.error_when_present):
            return ""
        for body in reversed(list(_every_object_in(f"{stdout}\n{stderr}"))):
            if not _that_went_wrong(recipe, body):
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

    def _did_it_ask_anybody(self, recipe: CliRecipe, stdout: str, stderr: str) -> bool | None:
        """Did the tool put the request to the service, or answer by itself?

        Nothing is a real answer here: a tool that does not say gets a shrug,
        not a guess. The ones that do say are the ones worth reading, because
        "the service said no" and "the tool said no without asking" send
        somebody to two different places, and being sent to the wrong one costs
        an afternoon.
        """

        for body in reversed(list(_every_object_in(f"{stdout}\n{stderr}"))):
            # Only what the tool printed as its answer counts. These tools print
            # lines of progress and counts alongside it, and any of those can
            # carry a number under one of these names without being about this
            # request at all - which would answer the question wrongly, in the
            # one place where being wrong sends somebody to the wrong door.
            # A tool that says whether it failed says so in its answer and
            # nowhere else, so that is what marks the answer out.
            if not _this_is_its_answer(recipe, body):
                continue
            # A status from the service first. It is only ever there because
            # something answered, and it is right where the timing is wrong:
            # this machine reports no time at the service for a refusal that
            # really did come back from it.
            if recipe.service_status_field:
                status = _dotted(body, recipe.service_status_field)
                if not isinstance(status, bool) and isinstance(status, (int, float)):
                    return True
            if recipe.time_at_the_service_field:
                took = _dotted(body, recipe.time_at_the_service_field)
                if not isinstance(took, bool) and isinstance(took, (int, float)):
                    return took > 0
        return None

    def _and_what_it_says_about_itself(
        self,
        recipe: CliRecipe,
        deadline_at: float | None = None,
        asked_anybody: bool | None = None,
        reason: str = "",
    ) -> str:
        """What else the harness knows, tacked onto a refusal.

        The tool's own sentence, read on its own, can say the wrong thing:
        "your organization does not have access to Claude" is what somebody
        sees while looking at a working Claude window. What the harness knows
        and was not saying is that the tool is on this machine, that it
        answered, and what it says about its own sign-in - which together move
        the question from "have I got this at all" to "this one thing is not
        allowed", and those take you to different places.

        And whether it asked anybody, which is the part that decides where to
        go next. A tool that says the request took no time at all never left
        this machine: it turned the job down out of what it has written down
        about the account. Saying "the service turned this down" there was
        simply wrong, and it pointed at an administrator who has nothing to do
        with it.

        Three answers, not two. A tool that does not say gets neither claim -
        folded in with the ones that did ask, every tool that prints no timing
        at all was back to being told the service refused it.

        Anything that goes wrong while asking is left out rather than piled on
        top: this is already an error message, and a second failure inside it
        helps nobody.
        """

        here = f"The {recipe.label} is on this machine and did answer, so nothing failed to reach it"
        if asked_anybody is False:
            said = ["", (
                f"{here} - and it says the request took no time at all, which "
                "means it never asked anybody. It turned this down here, out of "
                "what it has written down about your account."
            )]
            advice = recipe.when_it_never_asked
        elif asked_anybody is True:
            said = ["", f"{here} - what turned this down was the service behind it."]
            advice = recipe.when_it_is_refused
        else:
            # It did not say. Neither of the other two can be claimed, and
            # claiming the service is how somebody ends up asking an
            # administrator about something that never left their own machine.
            said = ["", (
                f"{here}. It does not say whether it asked anybody or turned "
                "this down by itself, so neither is claimed here."
            )]
            advice = recipe.when_it_is_not_clear
        # Unless the answer already said what would fix it, whichever of the
        # three this was. A refusal that names its own cause beats anything
        # worked out from timings here, and it was only being read in one of the
        # three - so the one message that spelt out what to do got a guess
        # written over the top of it whenever the tool was vague about the rest.
        plainly = (reason or "").lower()
        if recipe.the_answer_names_it and any(
                one in plainly for one in recipe.the_answer_names_it):
            advice = recipe.when_the_answer_names_it or advice
        about = self._how_it_describes_its_sign_in(recipe, deadline_at)
        if about:
            said.append(f"It says of itself: {about}.")
        if advice:
            said.append(advice)
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
        if _that_went_wrong(recipe, body):
            said = _dotted(body, recipe.error_message_field) if recipe.error_message_field else ""
            # Cut to a length a person reads. Every other way of building one of
            # these caps what it holds; this one did not, so a tool that answers
            # with a page of detail put a page of detail in a sentence.
            why = self._redactor.text(str(said or "no reason given"))[:LONGEST_REASON]
            raise HarnessError(
                f"{recipe.label} refused the request: {why}"
                f"{self._and_what_it_says_about_itself(recipe, self._deadline, self._did_it_ask_anybody(recipe, stdout, stderr), why)}"
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


# How much of what a tool printed is worth reading at all, and how long one run
# may be. There was a third limit here, on how far one object could be spread,
# and it was needed while a run was read again after every line: a tool that
# printed a few thousand lines each opening a brace had that reading walk to the
# end from every one of them. Counting the braces first means a run is read once,
# when it could be whole, so the limit stopped doing anything - a block is never
# longer than the two below allow. A limit that cannot be reached is worse than
# none: it reads like a guard and guards nothing.
MOST_LINES_READ = 2_000
LONGEST_RUN = 200_000


def _where_the_braces_are(line: str, depth: int, inside: bool) -> tuple[int, bool]:
    """How deep the braces go by the end of this line, and whether a string is
    still open.

    Braces inside a string are letters, not braces, and a backslash inside one
    means the next character is a letter whatever it is. Getting that wrong the
    other way - counting them - would have a run look closed while it is not,
    which only costs one reading that comes to nothing.
    """

    skip = False
    for letter in line:
        if skip:
            skip = False
            continue
        if inside:
            if letter == "\\":
                skip = True
            elif letter == '"':
                inside = False
            continue
        if letter == '"':
            inside = True
        elif letter == "{":
            depth += 1
        elif letter == "}":
            depth = max(0, depth - 1)
    return depth, inside


def _objects_across(said: str) -> list[dict[str, Any]] | None:
    """Every object in this run of text, or nothing if it is not all objects.

    All of it or none of it, on purpose. A tool can print two objects one after
    another with nothing between them, and that is still a tool talking; text
    with an object somewhere inside it is not, and reading one out of the middle
    of a sentence is how a line saying "this was rejected" came back as the
    tool's own answer.
    """

    if len(said) > LONGEST_RUN:
        return None
    reader = json.JSONDecoder()
    seen: list[dict[str, Any]] = []
    at = 0
    while at < len(said):
        while at < len(said) and said[at] in " \t\r\n":
            at += 1
        if at >= len(said):
            break
        try:
            held, at = reader.raw_decode(said, at)
        except (json.JSONDecodeError, RecursionError, ValueError):
            # RecursionError as well as the one about the shape. An object
            # nested a thousand deep is seven thousand letters long, and the
            # error it raises is not a JSONDecodeError - so it came out of here,
            # past the one place that catches a route which will not answer, and
            # took every other assistant's answer down with it.
            return None
        if not isinstance(held, dict):
            return None
        seen.append(held)
    return seen or None


def _every_object_in(said: str) -> list[dict[str, Any]]:
    """Every JSON object a tool printed, in the order it printed them.

    A run is only read when a line starts with a brace and everything from there
    is objects and nothing else. That covers the three ways these tools really
    answer - one object, several one after another, and one written across
    several lines - and steps over a banner, a line of progress, or a word at
    the end.

    What it will not do is reach into the middle of a line of ordinary text.
    Reading from the first brace to the last one over everything printed, a line
    like `debug: candidate {"is_error": true, ...} rejected` was read as the
    tool refusing, in those words. What goes into these tools is not always
    something anybody chose, so that was a way to put words in the tool's mouth.
    """

    seen: list[dict[str, Any]] = []
    for block in _the_ends_of(said.splitlines()):
        seen.extend(_objects_in_these_lines(block))
    return seen


def _the_ends_of(lines: list[str]) -> list[list[str]]:
    """The beginning and the end of a great many lines, and both of them.

    Only the first so many, and a tool that says three thousand lines of
    something before it says why loses the reason - which is the shape a tool
    with a lot to say has. Only the last so many, and a tool that answers first
    and then talks loses it the other way round. So both ends, read apart from
    each other, and the middle of a torrent is what goes unread.
    """

    if len(lines) <= MOST_LINES_READ:
        return [lines]
    half = MOST_LINES_READ // 2
    return [lines[:half], lines[-half:]]


def _objects_in_these_lines(lines: list[str]) -> list[dict[str, Any]]:
    """Every object in this block, in one walk from the top.

    Two shapes, both cheap. A line that is a whole answer on its own is read on
    its own, which is how these tools usually print one. Otherwise a run opens
    at a line that starts with a brace and is read when the braces it opened
    have closed - and the lines are kept in a list, joined once, rather than a
    string rebuilt from the beginning every time a line is added.

    Starting again at every line that opened a brace and never closed it, and
    rebuilding the run after each line, two thousand lines of sixty-six letters
    took thirty-eight seconds. There is no clock on this: it happens after the
    program has already finished, so nothing else would have stopped it.

    A whole object found on a line inside an open run is kept to one side rather
    than taken there and then, because a line inside an answer is not the
    answer. An outer object written over several lines can hold a list whose
    last item is written compactly on one line, and taking that line as the
    answer said "transient network hiccup, retrying" while the real answer, two
    lines further down, was thrown away with the run. What is kept to one side
    is used only if the run never closes - which is the case it was there for: a
    line that opened a brace and never closed it, and the answer after it.

    What one pass gives up is an object written across several lines that begins
    after an earlier brace was left open. That falls back to the message saying
    what code the tool stopped with, which is the safer way round.
    """

    seen: list[dict[str, Any]] = []
    # Whole objects found on a line inside a run that has not closed. Used only
    # if it never does - inside one that closes they are its own parts, and one
    # of them read as the answer is worse than no answer at all. Nothing needs
    # to empty this when a run closes: it is only ever read where a run was left
    # open, and a new run empties it. A line put here to do that as well looked
    # like it was holding something up and was holding up nothing.
    aside: list[dict[str, Any]] = []
    depth, inside = 0, False
    holding: list[str] | None = None
    for line in lines:
        if holding is None:
            if not line.strip().startswith("{"):
                continue
            # A whole answer on one line, read without opening a run at all.
            alone = _objects_across(line)
            if alone is not None:
                seen.extend(alone)
                continue
            holding = []
            aside = []
            depth, inside = 0, False
        else:
            held = line.strip()
            if held.startswith("{") and held.endswith("}"):
                alone = _objects_across(held)
                if alone is not None:
                    aside.extend(alone)
        holding.append(line)
        depth, inside = _where_the_braces_are(line, depth, inside)
        if depth == 0 and not inside:
            found = _objects_across("\n".join(holding))
            if found is not None:
                seen.extend(found)
            holding = None
    # A run left open at the end was never going to close, and a whole object
    # found inside it is the best there is.
    if holding is not None:
        seen.extend(aside)
    return seen


def _that_went_wrong(recipe: CliRecipe, body: dict[str, Any]) -> bool:
    """Whether one thing a tool printed is it saying no.

    Two ways of saying it. Most set a flag to true. Gemini puts an object where
    the answer would be and says nothing else, and read for a flag that says
    nothing at all - so a refusal came back as an empty answer.
    """

    if recipe.error_when_present and _dotted(body, recipe.error_when_present) is not None:
        return True
    return bool(recipe.error_field) and _dotted(body, recipe.error_field) is True


def _this_is_its_answer(recipe: CliRecipe, body: dict[str, Any]) -> bool:
    """Whether one thing a tool printed is its answer rather than a line beside it.

    These tools print progress and counts alongside the answer, and any of those
    can carry a number under one of the names being looked for without being
    about this request at all. A tool says whether it failed in its answer and
    nowhere else, so that is what marks the answer out - either the flag, or the
    place an error object would go, or the answer itself.
    """

    for name in (recipe.error_field, recipe.error_when_present, recipe.text_field):
        if name and _dotted(body, name) is not None:
            return True
    return not (recipe.error_field or recipe.error_when_present or recipe.text_field)


def _every_build_of(patterns: tuple[str, ...]) -> list[tuple[Path, tuple[int, ...]]]:
    """Every copy of a tool kept under those patterns, with its version.

    The version is read out of the folder name, which is how these tools lay
    themselves out, so nothing has to be run to find out how old a copy is.
    """

    found: list[tuple[Path, tuple[int, ...]]] = []
    for pattern in patterns:
        name, _, rest = pattern.partition("/")
        base = os.environ.get(name) if name.isupper() else None
        if name == "HOME":
            base = str(Path.home())
        if not base:
            continue
        try:
            for where in Path(base).glob(rest):
                if not where.is_file():
                    continue
                version = _as_numbers(where.parent.name)
                if version:
                    found.append((where, version))
        except OSError:
            continue
    return found


def _as_numbers(said: str) -> tuple[int, ...]:
    """A version written as numbers, so two of them can be compared."""

    parts = said.split(".")
    if not all(one.isdigit() for one in parts) or not parts:
        return ()
    return tuple(int(one) for one in parts)


def _the_version_of(program: str) -> tuple[int, ...]:
    """What version the copy on the path says it is.

    Asked of the program itself, because nothing about where it sits says so.
    A copy that will not answer is treated as the oldest there is, which is the
    safe way round: anything found elsewhere will be preferred to it.
    """

    try:
        done = _run_bounded(
            [program, "--version"], cwd=Path.cwd(), stdin_text=None,
            timeout_seconds=20.0, max_output_bytes=8_000,
        )
    except HarnessError:
        return ()
    if done.timed_out or done.exit_code != 0:
        return ()
    found = re.search(r"(\d+(?:\.\d+)+)", done.stdout or "")
    return _as_numbers(found.group(1)) if found else ()


def _in_a_few_words(said: str) -> str:
    """Whether a tool says it is signed in, without account identity.

    Auth-status output can contain an email address, organisation, account name
    and subscription plan. None is needed to explain a provider failure, and
    copying it into a chat turns a local diagnostic into stored personal data.
    Only the yes/no status is allowed out of this boundary.
    """

    try:
        held = json.loads(said.strip() or "{}")
    except json.JSONDecodeError:
        held = None
    if isinstance(held, dict):
        for name in ("loggedIn", "authenticated", "signedIn"):
            found = held.get(name)
            if isinstance(found, bool):
                return "signed in" if found else "not signed in"
        status = str(held.get("status") or "").strip().lower()
    else:
        status = " ".join(said.split()).lower()
    if any(words in status for words in ("not signed in", "not logged in", "logged out")):
        return "not signed in"
    if any(words in status for words in ("signed in", "logged in", "authenticated")):
        return "signed in"
    return "the sign-in check answered"


def _whole(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, int(value))
