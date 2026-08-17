"""Do the setting up for somebody who does not want to read any of it.

Every way of connecting a model has a short list of things to do, and the first
screen prints that list. For somebody who has never done it, a list is still
work. This does the parts a program is allowed to do, and says plainly which
part is left, because some parts are nobody's job but yours.

What it will do
  - Start Ollama if it is installed but not running, and fetch the model.
  - Write the provider route into your own settings file, and choose it as the
    one used by default.
  - Trust that file, when trusting it is this tool's to do. See seats.set_up:
    a settings file that was already there and never trusted is left untrusted,
    because it can start programs and nobody has read it.

What it will not do
  - Install software. Fetching a program off the internet and running it is not
    something a button should do behind somebody's back.
  - Make an account or an API key. Only the person paying can do that.
  - Ask for, hold, or write down a key. A key belongs in your terminal, never
    in a settings file and never in this panel.

Each of those is said out loud instead, with the one command or the one page
that finishes the job.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from .config import LoadedConfig, is_project_local_config_trusted, trust_project_local_config
from .models import HarnessError
from .safety import confined_path

DOING = "doing"
DONE = "done"
CANNOT = "cannot"

# Fetching a model is a large download on a slow line.
PULL_TIMEOUT_SECONDS = 1800.0
# Starting a local server should take a moment, not a coffee break.
START_TIMEOUT_SECONDS = 60.0
SHORT_COMMAND_SECONDS = 30.0


class SetupError(HarnessError):
    """A problem doing somebody's setting up for them."""


@dataclass
class Step:
    """One thing that was done, or could not be."""

    text: str
    state: str = DOING
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "state": self.state, "detail": self.detail}


@dataclass
class Job:
    """What is being done right now, and how far it has got."""

    option: str
    label: str
    running: bool = True
    finished: bool = False
    worked: bool = False
    said: str = ""
    steps: list[Step] = field(default_factory=list)
    left_for_you: list[str] = field(default_factory=list)
    settings_file: str = ""
    contents: str = ""
    trusted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "option": self.option,
            "label": self.label,
            "running": self.running,
            "finished": self.finished,
            "worked": self.worked,
            "said": self.said,
            "steps": [step.to_dict() for step in self.steps],
            "left_for_you": self.left_for_you,
            "settings_file": self.settings_file,
            "contents": self.contents,
            "trusted": self.trusted,
        }


@dataclass(frozen=True)
class Plan:
    """What this option needs, and what route it ends up writing."""

    option: str
    label: str
    route_name: str
    kind: str
    model: str
    endpoint: str = ""
    key_name: str = ""
    command: str = ""
    where_to_get_it: str = ""
    # Anything else this kind of route needs before the harness will accept it.
    extra: tuple[tuple[str, Any], ...] = ()
    # Some kinds may be named as a route but not as the one used by default.
    can_be_the_default: bool = True


# One plan for every way of connecting a model the first screen offers. The
# names on the left are the ids provider_help gives its options, and a test
# holds the two lists against each other: a new way of connecting a model must
# arrive with a button, or arrive knowing it has none.
PLANS: dict[str, Plan] = {
    "ollama": Plan(
        option="ollama",
        label="Ollama on this machine",
        route_name="ollama",
        kind="ollama",
        model="qwen2.5-coder:7b",
        endpoint="http://127.0.0.1:11434",
        command="ollama",
        where_to_get_it="ollama.com",
    ),
    "claude-cli": Plan(
        option="claude-cli",
        label="Claude command line",
        route_name="claude",
        kind="claude-cli",
        model="claude-sonnet-4-5",
        command="claude",
        where_to_get_it="the Claude Code install page",
    ),
    "copilot-cli": Plan(
        option="copilot-cli",
        label="GitHub Copilot command line",
        route_name="copilot",
        kind="copilot-cli",
        model="gpt-5",
        command="copilot",
        where_to_get_it="the GitHub Copilot command line install page",
    ),
    "codex-cli": Plan(
        option="codex-cli",
        label="Codex command line",
        route_name="codex",
        kind="codex-cli",
        model="gpt-5-codex",
        command="codex",
        extra=(("command", ("codex",)), ("auth_mode", "chatgpt")),
        can_be_the_default=False,
        where_to_get_it="the Codex command line install page",
    ),
    "anthropic": Plan(
        option="anthropic",
        label="Anthropic",
        route_name="anthropic",
        kind="anthropic",
        model="claude-sonnet-4-5",
        endpoint="https://api.anthropic.com/v1",
        key_name="ANTHROPIC_API_KEY",
        where_to_get_it="console.anthropic.com",
    ),
    "openai": Plan(
        option="openai",
        label="OpenAI",
        route_name="openai",
        kind="openai",
        model="gpt-5",
        endpoint="https://api.openai.com/v1",
        key_name="OPENAI_API_KEY",
        where_to_get_it="platform.openai.com",
    ),
    "gemini": Plan(
        option="gemini",
        label="Google Gemini",
        route_name="gemini",
        kind="gemini",
        model="gemini-2.5-pro",
        endpoint="https://generativelanguage.googleapis.com/v1beta",
        key_name="GEMINI_API_KEY",
        where_to_get_it="aistudio.google.com",
    ),
}


def plan_for(option: str) -> Plan:
    if option not in PLANS:
        raise SetupError(f"There is nothing this can set up on its own for {option}")
    return PLANS[option]


def _run(parts: list[str], seconds: float) -> tuple[int, str]:
    """Run a program this module chose, never anything from a request."""

    try:
        finished = subprocess.run(
            parts, capture_output=True, text=True, timeout=seconds, check=False
        )
    except FileNotFoundError:
        return 127, "The command went away between finding it and running it."
    except subprocess.TimeoutExpired:
        return 124, f"It did not finish within {int(seconds)} seconds."
    except OSError as exc:
        return 126, f"It could not be started: {exc}"
    said = (finished.stdout or "") + (finished.stderr or "")
    return finished.returncode, said.strip()[:400]


def _answering(endpoint: str, seconds: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"{endpoint.rstrip('/')}/api/tags", timeout=seconds) as answer:
            return 200 <= int(answer.status) < 500
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


@dataclass
class Written:
    """What went into the settings file, and what it cost."""

    settings_file: str
    contents: str
    trusted: bool
    trouble: str = ""
    replaced: list[str] = field(default_factory=list)


def write_the_route(
    config: LoadedConfig,
    plan: Plan,
    *,
    make_it_the_default: bool = True,
) -> Written:
    """Add this route to the settings file, keeping everything already there.

    Gives back the file it wrote, what is now in it, whether it is trusted, and
    a line to show when it is not. The rule about trusting is the same one seat
    setup keeps to, and for the same reason: a file that was already there and
    never trusted stays untrusted, because trusting it would hand authority to
    lines nobody has read.
    """

    from .safety import ProjectTransactionLock
    from .seats import what_makes_it_risky

    local = confined_path(
        config.project_root, ".harness/config.local.json",
        allow_missing=True, allow_control=True,
    )
    # Seat setup writes this same file, from a different button and possibly a
    # different browser tab. Read, change, write without a lock means whichever
    # write lands second throws away what the other one added.
    with ProjectTransactionLock(config.project_root).held(60.0):
        was_there = local.is_file()
        was_trusted = was_there and is_project_local_config_trusted(config.project_root, local)
        settings: dict[str, Any] = {}
        if was_there:
            try:
                settings = json.loads(local.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise SetupError(
                    f"{local.name} is there and cannot be read: {exc}. "
                    "Fix or move that file first; nothing was changed."
                ) from exc
            if not isinstance(settings, dict):
                raise SetupError(f"{local.name} does not hold settings, so it was left alone")
        routes = settings.get("providers")
        routes = routes if isinstance(routes, dict) else {}
        # Somebody's earlier decision, said out loud rather than left for them
        # to notice later in the file.
        replaced = [plan.route_name] if plan.route_name in routes else []
        routes[plan.route_name] = {
            "kind": plan.kind,
            "model": plan.model,
            "endpoint": plan.endpoint,
        }
        if plan.key_name:
            routes[plan.route_name]["api_key_env"] = plan.key_name
        for key, value in plan.extra:
            routes[plan.route_name][key] = list(value) if isinstance(value, tuple) else value
        settings["providers"] = routes
        if make_it_the_default and plan.can_be_the_default:
            already = settings.get("provider")
            if isinstance(already, dict) and already.get("name") and already["name"] != plan.kind:
                replaced.append(f"the assistant used by default ({already['name']})")
            settings["provider"] = {
                "name": plan.kind,
                "model": plan.model,
                "endpoint": plan.endpoint,
                "api_key_env": plan.key_name,
            }
        body = json.dumps(settings, indent=2) + "\n"
        try:
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_text(body, encoding="utf-8")
        except OSError as exc:
            # A full disk, a file another program is holding open, a folder
            # nobody may write to. Said plainly here, because further out this
            # runs on a thread of its own where an error nobody catches would
            # simply disappear.
            raise SetupError(f"{local.name} could not be written: {exc}") from exc

        if not was_there or was_trusted:
            try:
                trust_project_local_config(config.project_root, local)
                return Written(local.as_posix(), body, True, "", replaced)
            except HarnessError as exc:
                return Written(
                    local.as_posix(), body, False, f"Trusting the file did not work: {exc}", replaced
                )
        # Not trusted here, and not refused either: the file is handed back to
        # read, what carries risk in it is named, and the choice belongs to the
        # person whose machine it is.
        worrying = what_makes_it_risky(settings)
        return Written(
            local.as_posix(), body, False,
            f"{local.name} was already here and nobody has said it is theirs. Trusting "
            "it lets everything in it act, including anything this did not write."
            + ("".join(f" {line}." for line in worrying) if worrying else "")
            + " Read it, then trust it here or run: harness trust",
            replaced,
        )


def _finish(job: Job, config: LoadedConfig, plan: Plan) -> None:
    """The last part every option shares: write the route and trust the file."""

    step = Step(f"Write the route for {plan.label} into your own settings")
    job.steps.append(step)
    written = write_the_route(config, plan)
    where, body, trusted, trouble = (
        written.settings_file, written.contents, written.trusted, written.trouble
    )
    job.settings_file = where
    job.contents = body
    job.trusted = trusted
    step.state = DONE
    step.detail = f"Written to {where}."
    if written.replaced:
        step.detail += " Written over: " + ", ".join(written.replaced) + "."
    trust_step = Step("Say the settings file is yours")
    job.steps.append(trust_step)
    if trusted:
        trust_step.state = DONE
        trust_step.detail = "Trusted, so the harness may use this route."
    else:
        trust_step.state = CANNOT
        trust_step.detail = trouble
        job.left_for_you.append(trouble)
    job.worked = trusted
    job.said = (
        f"{plan.label} is set up and ready to use."
        if trusted
        else f"{plan.label} is written down. One thing is left for you."
    )


def _do_ollama(job: Job, config: LoadedConfig, plan: Plan) -> None:
    endpoint = plan.endpoint
    looking = Step("Look for Ollama on this machine")
    job.steps.append(looking)
    if _answering(endpoint):
        looking.state = DONE
        looking.detail = "It is installed and already running."
    else:
        where = shutil.which(plan.command)
        if not where:
            looking.state = CANNOT
            looking.detail = "Ollama is not on this machine."
            job.left_for_you.append(
                f"Install Ollama from {plan.where_to_get_it}, then press this button again. "
                "Fetching and running an installer is not something this will do for you."
            )
            job.said = "Ollama has to be installed first. Everything after that is done for you."
            return
        looking.state = DONE
        looking.detail = f"Installed, but not answering at {endpoint} yet."
        starting = Step("Start Ollama")
        job.steps.append(starting)
        try:
            # Left running on purpose: it is the model server, and closing it
            # when this returns would undo the whole point.
            subprocess.Popen(  # noqa: S603 - the path came from this machine, not a request
                [where, "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        except OSError as exc:
            starting.state = CANNOT
            starting.detail = f"It would not start: {exc}"
            job.left_for_you.append("Start Ollama yourself, then press this button again.")
            job.said = "Ollama would not start."
            return
        waited = 0.0
        while waited < START_TIMEOUT_SECONDS and not _answering(endpoint):
            time.sleep(1.0)
            waited += 1.0
        if not _answering(endpoint):
            starting.state = CANNOT
            starting.detail = f"It did not answer within {int(START_TIMEOUT_SECONDS)} seconds."
            job.left_for_you.append("Start Ollama yourself, then press this button again.")
            job.said = "Ollama was started and never answered."
            return
        starting.state = DONE
        starting.detail = f"Running, and answering at {endpoint}."

    where = shutil.which(plan.command)
    if where:
        fetching = Step(f"Fetch the model {plan.model}")
        job.steps.append(fetching)
        code, said = _run([where, "pull", plan.model], PULL_TIMEOUT_SECONDS)
        if code != 0:
            fetching.state = CANNOT
            fetching.detail = said or f"It stopped with code {code}."
            job.left_for_you.append(f"Fetch the model yourself: ollama pull {plan.model}")
        else:
            fetching.state = DONE
            fetching.detail = "The model is on this machine."
    _finish(job, config, plan)


def _do_signed_in_tool(job: Job, config: LoadedConfig, plan: Plan) -> None:
    looking = Step(f"Look for the {plan.command} command")
    job.steps.append(looking)
    where = shutil.which(plan.command)
    if not where:
        looking.state = CANNOT
        looking.detail = f"The {plan.command} command is not on this machine."
        job.left_for_you.append(
            f"Install it from {plan.where_to_get_it} and sign in, then press this button again. "
            "Installing software is not something this will do for you."
        )
        job.said = f"{plan.label} has to be installed first. Everything after that is done for you."
        return
    looking.state = DONE
    looking.detail = "Found on this machine."

    asking = Step("Check it answers")
    job.steps.append(asking)
    code, said = _run([where, "--version"], SHORT_COMMAND_SECONDS)
    if code != 0:
        asking.state = CANNOT
        asking.detail = said or f"It answered with code {code}."
        job.left_for_you.append(
            f"Run {plan.command} once yourself and sign in. It is installed but not answering yet."
        )
        job.said = f"{plan.label} is installed but not signed in."
        return
    asking.state = DONE
    asking.detail = said.splitlines()[0][:120] if said else "It answered."
    _finish(job, config, plan)


def _do_hosted(job: Job, config: LoadedConfig, plan: Plan) -> None:
    looking = Step(f"Look for {plan.key_name} in this terminal")
    job.steps.append(looking)
    if not os.environ.get(plan.key_name):
        looking.state = CANNOT
        looking.detail = f"{plan.key_name} is not set."
        job.left_for_you.append(
            f"Make a key at {plan.where_to_get_it} and set it in your terminal as "
            f"{plan.key_name}, then start the harness from that same terminal. "
            "Only you can make a key, and it must never be typed into this page."
        )
        job.said = f"{plan.label} needs a key, and only you can make one."
        return
    looking.state = DONE
    looking.detail = f"{plan.key_name} is set. Its value is never read, shown, or written down."
    _finish(job, config, plan)


# Which of the three shapes each option takes. Kept beside the plans so adding
# a way of connecting a model is one line in each place, not a hunt.
HOW: dict[str, Callable[[Job, LoadedConfig, Plan], None]] = {
    "ollama": _do_ollama,
    "claude-cli": _do_signed_in_tool,
    "copilot-cli": _do_signed_in_tool,
    "codex-cli": _do_signed_in_tool,
    "anthropic": _do_hosted,
    "openai": _do_hosted,
    "gemini": _do_hosted,
}


def do_it(config: LoadedConfig, option: str, job: Job | None = None) -> Job:
    """Set this way of connecting a model up, as far as a program is allowed to.

    The job to fill in can be handed in. That matters: the thing running this
    on a background thread holds that same object and shows it to the page a
    second at a time, so filling in a different one would leave the page
    watching an empty list for as long as the work takes.
    """

    plan = plan_for(option)
    job = job or Job(option=option, label=plan.label)
    try:
        HOW[option](job, config, plan)
    except Exception as exc:  # noqa: BLE001 - see below
        # Everything, not only the harness's own errors. A disk that is full, a
        # file another program is holding, a folder nobody may write to: those
        # arrive as an OSError, and this runs on a thread of its own where an
        # error nobody catches vanishes into the void and leaves the job saying
        # "still working" for ever.
        job.said = str(exc) or exc.__class__.__name__
        for step in job.steps:
            if step.state == DOING:
                step.state = CANNOT
                step.detail = job.said
        if not job.steps:
            job.steps.append(Step("Set it up", state=CANNOT, detail=job.said))
        job.worked = False
    finally:
        job.running = False
        job.finished = True
        if not job.said:
            job.said = "Nothing happened, which should not be possible. Try again."
    return job


class Runner:
    """One job at a time, so two presses cannot write the same file at once."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job: Job | None = None
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        with self._lock:
            return bool(self._job and self._job.running)

    def latest(self) -> dict[str, Any] | None:
        with self._lock:
            return self._job.to_dict() if self._job else None

    def start(self, config: LoadedConfig, option: str) -> dict[str, Any]:
        plan = plan_for(option)
        with self._lock:
            if self._job and self._job.running:
                raise SetupError(
                    f"{self._job.label} is being set up right now. Wait for that to finish."
                )
            self._job = Job(option=option, label=plan.label)
            job = self._job

        def work() -> None:
            # The very same job the panel is watching, so each step shows up as
            # it happens instead of all at once at the end. Fetching a model can
            # take half an hour; a page that says nothing for half an hour is a
            # page nobody trusts.
            try:
                do_it(config, option, job)
            except BaseException as exc:  # noqa: BLE001 - nothing may leave it running
                # do_it already catches everything and marks the job finished.
                # This is the last line: if that itself ever failed, a job stuck
                # at "running" would refuse every later press until the panel is
                # restarted, and nobody would ever be told why.
                job.said = f"This stopped in a way nobody expected: {exc}"
                job.worked = False
            finally:
                # However this thread ends, the job stops being "running". A job
                # that says it is still going refuses every later press, and the
                # only way out is restarting the panel.
                job.running = False
                job.finished = True

        thread = threading.Thread(target=work, name=f"set-up-{option}", daemon=True)
        self._thread = thread
        thread.start()
        return job.to_dict()

    def wait(self, seconds: float = 5.0) -> None:
        """Only used by tests, so they never depend on how fast a machine is."""

        thread = self._thread
        if thread:
            thread.join(seconds)
