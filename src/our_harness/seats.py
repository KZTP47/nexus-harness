"""Setting up the assistants you already pay for, without editing a file.

Plenty of organisations have Claude or Copilot seats and no API keys. Each of
those assistants ships a command line tool that is already signed in, and the
harness can drive it. Doing that by hand means knowing which tools are on the
machine, what a provider route looks like, and that the settings file has to be
trusted afterwards.

This does all three. It looks for each tool, asks it its version, writes the
routes for the ones that are really there, and records the file as trusted.

Two rules it keeps to:

  - It only ever writes routes for tools it has actually found. A route to a
    tool that is not installed is a run that fails later for a reason nobody
    can see now.
  - It adds to the settings file rather than replacing it. Somebody else's
    settings are not this tool's to throw away.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import LoadedConfig, is_project_local_config_trusted, trust_project_local_config
from .models import HarnessError
from .providers.subscription_cli import RECIPES, available, recipe_for
from .safety import confined_path

# The assistants this can set up on its own. Anything else is still possible by
# hand, and the guide says how.
KNOWN_SEATS = ("claude-cli", "copilot-cli", "gemini-cli", "codex-cli")
# A short name for the route, so the settings read as a person would write them.
ROUTE_NAMES = {
    "claude-cli": "claude", "copilot-cli": "copilot",
    "gemini-cli": "gemini", "codex-cli": "codex",
}
# What each assistant is asked to do by default, when a workflow is set up for
# two of them. The reviewer is deliberately not the coder: two assistants that
# share no training tend not to share the same blind spot.
DEFAULT_JOBS = {"planner": 0, "coder": 1, "review": 0}
# Long enough for a program to start on a slow machine, short enough that a
# hung tool does not hold up the page.
VERSION_TIMEOUT_SECONDS = 20.0
# How many programs' versions are kept. Far more than any machine has
# tools, and a number rather than for ever.
MOST_VERSIONS_REMEMBERED = 40


# One at a time while the settings file is changed: it is read, added to and
# written back, and two of those at once each write back what the other never
# saw.
_while_the_settings_are_written = threading.Lock()


# One at a time while the remembered versions are changed: read, changed and
# written back is three things, and two at once each write back what the
# other never saw.
_while_versions_are_written = threading.Lock()


class SeatError(HarnessError):
    """A problem setting up an assistant."""


@dataclass
class Seat:
    """One assistant, and whether this machine can use it."""

    kind: str
    label: str
    route: str
    command: str
    found_at: str = ""
    version: str = ""
    ready: bool = False
    already_set_up: bool = False
    why_not: str = ""
    install_hint: str = ""
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "route": self.route,
            "command": self.command,
            "found_at": self.found_at,
            "version": self.version,
            "ready": self.ready,
            "already_set_up": self.already_set_up,
            "why_not": self.why_not,
            "install_hint": self.install_hint,
            "model": self.model,
        }


@dataclass
class Look:
    """What is on this machine, and what is already set up."""

    seats: list[Seat] = field(default_factory=list)
    settings_file: str = ""
    trusted: bool = False

    @property
    def ready(self) -> list[Seat]:
        return [seat for seat in self.seats if seat.ready]

    def to_dict(self) -> dict[str, Any]:
        return {
            "seats": [seat.to_dict() for seat in self.seats],
            "settings_file": self.settings_file,
            "trusted": self.trusted,
            "ready_count": len(self.ready),
        }


def settings_to_work_from(root: Path) -> tuple[LoadedConfig, str]:
    """Settings good enough to set seats up with, even when they are broken.

    A settings file holding routes that have not been trusted stops the whole
    config being read. That is the right answer everywhere else, and the wrong
    one here: this is the tool for fixing exactly that, and a repair tool that
    cannot start when the thing it repairs is broken is no use to anybody.

    So the file is read the ordinary way first, and only when that is refused
    does this fall back to the defaults, saying plainly that it did.
    """

    from .config import DEFAULT_CONFIG, load_config

    try:
        return load_config(root), ""
    except HarnessError as exc:
        import copy as _copy

        return (
            LoadedConfig(_copy.deepcopy(DEFAULT_CONFIG), root.resolve(), [], {}),
            f"Your current settings could not be read ({exc}), so this worked from "
            "the defaults. Setting the seats up again will put that right.",
        )


# Everything a settings file can do only once somebody has trusted it, and
# what each of those means in plain words. The names on the left are the
# settings config.py refuses to honour from a file nobody has trusted, and a
# test holds this list against that code: a new power added there and not
# here would mean the panel promising somebody there was nothing to worry
# about while handing over exactly the thing they should have worried about.
WHAT_TRUSTING_UNLOCKS: tuple[tuple[str, str], ...] = (
    ("providers", "a route that can start a program or call an address"),
    ("provider", "the assistant used by default, and the address it calls"),
    ("mcp", "other programs the harness starts and talks to"),
    ("plugins", "code of somebody else's that the harness loads and runs"),
    ("execution", "how programs are started, and what they are handed"),
    ("git", "committing and pushing to your repository"),
    ("qa", "addresses your checks are allowed to call"),
    ("project", "the commands the harness runs to test, lint, and build"),
    ("memory", "sending pieces of your code away to be turned into numbers"),
    ("workflow", "how many reviews a change has to survive"),
)


def what_makes_it_risky(settings: dict[str, Any]) -> list[str]:
    """What trusting this file would allow. See _read_the_worrying_parts.

    This reads a file nobody has checked, so it treats every shape as possible.
    Falling over here would be the worst outcome of all: the routes are already
    written by the time it runs, so an error would leave somebody with a
    changed file, no way back, and no warning ever shown. Whatever happens, a
    line comes back.
    """

    try:
        return _read_the_worrying_parts(settings)
    except Exception as exc:  # noqa: BLE001 - a warning must never be the thing that breaks
        return [
            "This file is not shaped the way settings usually are, and reading it "
            f"for anything worrying did not finish ({exc}). Read all of it yourself "
            "before deciding."
        ]


def _read_the_worrying_parts(settings: dict[str, Any]) -> list[str]:
    """What trusting this file would allow, in plain words.

    Somebody deciding whether to trust a file they did not write needs to know
    what trusting it lets happen. "It might be dangerous" tells them nothing.
    This names the actual parts: the route that starts a program, the address
    off this machine, the other programs it would start, the code it would
    load, and the commands it would run.

    Where a part is worth spelling out further it is, and where it is not, the
    section is still named. Saying nothing about a section this does not
    recognise would be the worst answer of the three.
    """

    if not isinstance(settings, dict):
        return ["This file does not hold settings at all."]
    worrying: list[str] = []
    # Which sections have already been spelled out, so the sweep at the end
    # names only what is left rather than saying everything twice.
    spoken_for: set[str] = set()
    routes = settings.get("providers")
    if isinstance(routes, dict):
        for name, route in routes.items():
            if not isinstance(route, dict):
                continue
            command = route.get("command")
            if command:
                shown = (
                    " ".join(str(part) for part in command)
                    if isinstance(command, list)
                    else str(command)
                )
                worrying.append(f"The route {name} starts this program: {shown}")
                spoken_for.add("providers")
            endpoint = str(route.get("endpoint") or "")
            if endpoint and not _is_this_machine(endpoint):
                worrying.append(f"The route {name} sends your code to {endpoint}")
                spoken_for.add("providers")
    default = settings.get("provider")
    if isinstance(default, dict):
        endpoint = str(default.get("endpoint") or "")
        if endpoint and not _is_this_machine(endpoint):
            worrying.append(f"By default your code goes to {endpoint}")
            spoken_for.add("provider")
    running = settings.get("execution")
    if isinstance(running, dict):
        if running.get("inherit_environment"):
            worrying.append(
                "Programs started by the harness are handed every variable in your "
                "environment, which is where keys and passwords usually live."
            )
            spoken_for.add("execution")
        if str(running.get("mode") or "") == "docker":
            worrying.append(
                "Work is run inside a container this file chooses: "
                f"{running.get('docker_image') or 'an image it names'}"
            )
            spoken_for.add("execution")
    servers = settings.get("mcp")
    if isinstance(servers, dict) and servers.get("servers"):
        # A list of servers, each naming itself. That is the shape the config
        # reader and the schema use; anything else is somebody's mistake, and a
        # warning that falls over on a mistake warns nobody about anything.
        listed = servers["servers"]
        listed = listed if isinstance(listed, list) else [listed]
        for server in listed:
            if not isinstance(server, dict):
                continue
            name = str(server.get("name") or "one with no name")
            command = server.get("command")
            arguments = server.get("args")
            parts = [str(command)] if command else []
            if isinstance(arguments, list):
                parts.extend(str(part) for part in arguments)
            shown = " ".join(parts) or str(server.get("url") or "")
            worrying.append(f"It starts another program called {name}: {shown}")
            spoken_for.add("mcp")
    plugins = settings.get("plugins")
    if isinstance(plugins, dict) and (plugins.get("enabled") or plugins.get("paths")):
        named = ", ".join(str(item) for item in (plugins.get("enabled") or plugins.get("paths")))
        worrying.append(f"It loads and runs code of somebody else's: {named}")
        spoken_for.add("plugins")
    commands = settings.get("project")
    if isinstance(commands, dict):
        for kind in ("test_commands", "lint_commands", "build_commands",
                     "security_commands", "performance_commands"):
            listed = commands.get(kind)
            # A string here would be walked letter by letter, and turn one odd
            # setting into a page of nonsense nobody can read.
            if not isinstance(listed, list):
                continue
            for command in listed:
                shown = (
                    " ".join(str(part) for part in command)
                    if isinstance(command, list)
                    else str(command)
                )
                worrying.append(f"The harness would run this as your {kind.split('_')[0]}: {shown}")
                spoken_for.add("project")
    repository = settings.get("git")
    if isinstance(repository, dict):
        if repository.get("allow_commit"):
            worrying.append("It may commit to your repository.")
            spoken_for.add("git")
        if repository.get("allow_push"):
            worrying.append("It may push to your repository.")
            spoken_for.add("git")
    checks = settings.get("qa")
    if isinstance(checks, dict):
        away = [
            str(host) for host in (checks.get("allow_hosts") or [])
            if str(host).lower() not in ("127.0.0.1", "localhost", "::1")
        ]
        if away:
            worrying.append("Your checks may call these addresses: " + ", ".join(away))
            spoken_for.add("qa")
    remembering = settings.get("memory")
    if isinstance(remembering, dict) and remembering.get("allow_remote_embeddings"):
        worrying.append("Pieces of your code are sent away to be turned into numbers.")
        spoken_for.add("memory")

    # Anything else this file holds that only a trusted file may hold. Named
    # even when there is nothing more to say about it, because a section
    # nobody mentions is a section nobody reads.
    for section, means in WHAT_TRUSTING_UNLOCKS:
        held = settings.get(section)
        if not held:
            continue
        if section in spoken_for:
            continue
        worrying.append(f"It sets {section}, which decides {means}.")
    return worrying


def _is_this_machine(endpoint: str) -> bool:
    import urllib.parse

    try:
        host = (urllib.parse.urlsplit(endpoint).hostname or "").lower()
    except ValueError:
        return False
    return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def _a_model_codex_really_has(where: str) -> str:
    """One model this copy of Codex carries, asked of it.

    Codex refuses a model it does not have, and which models it has changes with
    the version. A name written down in here is right until the day it is not,
    and then it is a route that fails with a message about a catalog.
    """

    try:
        done = subprocess.run(
            [where, "debug", "models", "--bundled"],
            capture_output=True, text=True, timeout=VERSION_TIMEOUT_SECONDS,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if done.returncode != 0:
        return ""
    try:
        held = json.loads(done.stdout or "{}")
    except json.JSONDecodeError:
        return ""
    for one in held.get("models", []) or []:
        if isinstance(one, dict) and isinstance(one.get("slug"), str) and one["slug"]:
            return one["slug"]
    return ""


def _default_model(config: LoadedConfig, kind: str) -> str:
    """The model already written down for this kind, or the one it ships with."""

    for name, route in (config.get("providers", {}) or {}).items():
        if isinstance(route, dict) and route.get("kind") == kind and route.get("model"):
            return str(route["model"])
    # Every kind this can set up needs one. Left out, the route written for it
    # holds an empty model, an empty model is refused, and the settings file
    # stops loading at all - so connecting one assistant took every other route
    # down with it. Connecting Codex turned Claude off.
    return {
        "claude-cli": "claude-sonnet-4-5",
        "copilot-cli": "gpt-5",
        "gemini-cli": "gemini-2.5-pro",
        "codex-cli": "gpt-5-codex",
    }.get(kind, "")


# What each tool said its version was, against the file it was asked of. Kept on
# the disk: the panel is started and stopped all day, and one tool here takes
# nine seconds to answer because it loads its extensions first. Nine seconds
# once per install is fair. Nine seconds every time somebody opens a tab is not,
# and the tab is drawn every time the board is.
def _where_versions_are_remembered() -> Path:
    from .config import user_config_path

    return user_config_path().parent / "tool-versions.json"


def _what_it_said_last_time(program: str) -> tuple[str, str] | None:
    """The version this exact program answered with before, if it is the same one.

    The same one means the same path, last written at the same moment, the same
    size. A program that has been replaced is a different program and is asked
    again.
    """

    try:
        stamp = Path(program).stat()
        held = json.loads(_where_versions_are_remembered().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    one = held.get(program) if isinstance(held, dict) else None
    if not isinstance(one, dict):
        return None
    if one.get("when") != stamp.st_mtime_ns or one.get("size") != stamp.st_size:
        return None
    version, trouble = one.get("version"), one.get("trouble")
    if not isinstance(version, str) or not isinstance(trouble, str):
        return None
    return version, trouble


def _remember_what_it_said(program: str, version: str, trouble: str) -> None:
    """Write down what a tool answered, without ever failing over it.

    This is bookkeeping around looking at a machine. If it cannot be written the
    looking still worked, and throwing here would turn a working tab into a
    broken one over a note.
    """

    where = _where_versions_are_remembered()
    try:
        stamp = Path(program).stat()
        with _while_versions_are_written:
            try:
                held = json.loads(where.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                held = {}
            if not isinstance(held, dict):
                held = {}
            # A lid on it, because every program ever asked would otherwise stay
            # in here for good. Far more than any machine has tools.
            if len(held) >= MOST_VERSIONS_REMEMBERED and program not in held:
                held.pop(next(iter(held)), None)
            held[program] = {
                "when": stamp.st_mtime_ns,
                "size": stamp.st_size,
                "version": version,
                "trouble": trouble,
            }
            where.parent.mkdir(parents=True, exist_ok=True)
            beside = where.with_name(f"{where.name}.{os.getpid()}.part")
            beside.write_text(json.dumps(held, indent=2) + "\n", encoding="utf-8")
            os.replace(beside, where)
    except (OSError, ValueError):
        return


def _ask_its_version(command: str, arguments: tuple[str, ...]) -> tuple[str, str]:
    """What the tool says when asked its version, and why it did not answer.

    The command is the full path already found on this machine, never anything
    from a request, and it is started without a shell, so nothing a caller says
    can change what runs. The full path matters on Windows, where these tools
    install as a small .CMD wrapper that the bare name cannot start.
    """

    remembered = _what_it_said_last_time(command)
    if remembered is not None:
        return remembered
    version, trouble = _really_ask_its_version(command, arguments)
    _remember_what_it_said(command, version, trouble)
    return version, trouble


def _really_ask_its_version(command: str, arguments: tuple[str, ...]) -> tuple[str, str]:
    """Start the tool and read what it says. The slow part.

    One tool here takes nine seconds to answer this, because it loads its
    extensions before it will say anything, so nothing should call this twice
    for the same program.
    """


    try:
        finished = subprocess.run(
            [command, *arguments],
            capture_output=True,
            text=True,
            timeout=VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        return "", "The command went away between finding it and running it."
    except subprocess.TimeoutExpired:
        return "", (
            f"It did not answer within {int(VERSION_TIMEOUT_SECONDS)} seconds. "
            "Run it once by hand: it may be waiting for a sign-in."
        )
    except OSError as exc:
        return "", f"It could not be started: {exc}"
    said = (finished.stdout or finished.stderr or "").strip().splitlines()
    first = said[0].strip() if said else ""
    if finished.returncode != 0:
        # A tool that prints something and still fails is the ordinary shape of
        # "installed but not signed in". Taking the printed line as a version
        # would call that seat ready, and the first real run would then fail
        # for a reason nobody could see at setup time.
        told = f": {first[:120]}" if first else " and said nothing"
        return "", (
            f"It answered with code {finished.returncode}{told}. "
            "Run it once by hand: it may be waiting for a sign-in."
        )
    return first[:120], ""


def look(config: LoadedConfig) -> Look:
    """Go and see which assistants this machine can use."""

    local = confined_path(
        config.project_root, ".harness/config.local.json",
        allow_missing=True, allow_control=True,
    )
    already = config.get("providers", {}) or {}
    found: list[Seat] = []
    # The ones that are here and still have to be asked their version.
    asking: list[tuple[Seat, str, tuple[str, ...]]] = []
    for kind in KNOWN_SEATS:
        recipe = recipe_for(kind)
        route = ROUTE_NAMES.get(kind, kind)
        command = recipe.command[0] if recipe.command else ""
        seat = Seat(
            kind=kind,
            label=recipe.label,
            route=route,
            command=command,
            install_hint=recipe.install_hint,
            model=_default_model(config, kind),
            already_set_up=any(
                isinstance(item, dict) and item.get("kind") == kind
                for item in already.values()
            ),
        )
        where = available(kind)
        if not where:
            seat.why_not = f"The {command} command is not on this machine."
            found.append(seat)
            continue
        seat.found_at = where
        asking.append((seat, where, recipe.version_arguments))
        found.append(seat)
    # All of them at once. Asking a tool its version means starting it, these
    # tools are slow to start, and one at a time the wait is the sum of every
    # one of them - which was ten seconds of somebody looking at a blank tab,
    # and this is asked again every time the board is drawn.
    if asking:
        with ThreadPoolExecutor(max_workers=len(asking)) as crowd:
            answers = list(crowd.map(
                lambda held: _ask_its_version(held[1], held[2]), asking))
        for (seat, _where, _arguments), (version, trouble) in zip(asking, answers):
            if trouble:
                seat.why_not = trouble
            else:
                seat.version = version
                seat.ready = True
    return Look(
        seats=found,
        settings_file=local.as_posix(),
        trusted=local.is_file() and is_project_local_config_trusted(config.project_root, local),
    )


def routes_for(config: LoadedConfig, kinds: list[str]) -> dict[str, Any]:
    """The provider routes that would be written for these assistants."""

    if not kinds:
        raise SeatError("Choose at least one assistant to set up")
    unknown = [kind for kind in kinds if kind not in KNOWN_SEATS]
    if unknown:
        raise SeatError(f"{unknown[0]} is not an assistant this can set up on its own")
    routes: dict[str, Any] = {}
    for kind in kinds:
        held: dict[str, Any] = {
            "kind": kind,
            "model": _default_model(config, kind),
            # A signed-in tool has no address to call, and a route holding both
            # is refused, so this is written out plainly rather than left off.
            "endpoint": "",
        }
        # Codex is the one that has to be told where it is. Its desktop app
        # keeps it in a folder of its own and nothing ever puts it on the path,
        # so a route without the full path is a route that cannot find it - and
        # its settings refuse an empty command rather than let that happen
        # quietly.
        if kind == "codex-cli":
            where = available(kind)
            if not where:
                raise SeatError(
                    "Codex is not on this machine, so there is nothing to point a "
                    "route at. Install it and sign in, then try again."
                )
            held["command"] = [where]
            held["auth_mode"] = "chatgpt"
            # Asked rather than guessed. A model name written down here ages
            # badly - Codex refuses one it does not carry, and the guess that
            # was right last month is the reason somebody's route stops working
            # this month with a message about a catalog.
            asked = _a_model_codex_really_has(where)
            if asked:
                held["model"] = asked
        routes[ROUTE_NAMES[kind]] = held
    return routes


@dataclass
class Before:
    """The settings file as it was a moment ago, so a setup can be undone.

    The trust mark is part of it. Putting a file back and trusting it when it
    was not trusted before would quietly hand it authority it never had.
    """

    contents: str | None
    trusted: bool = False


@dataclass
class Outcome:
    """What was written, and what happened next."""

    settings_file: str
    routes: list[str]
    contents: str
    trusted: bool
    kept: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)
    note: str = ""
    # True when the file was already there, nobody had trusted it, and the
    # decision is now yours. What is in it that matters is listed beside it.
    needs_your_say: bool = False
    risky_parts: list[str] = field(default_factory=list)
    # The mark of the file as it was written, handed to the panel so that
    # saying "I have read it" can be checked against what was really read.
    mark: str = ""
    # What the file held a moment ago. None contents means there was no file at
    # all before, and undoing means removing the one written.
    previous: Before | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "settings_file": self.settings_file,
            "routes": self.routes,
            "contents": self.contents,
            "trusted": self.trusted,
            "kept": self.kept,
            "replaced": self.replaced,
            "note": self.note,
            "needs_your_say": self.needs_your_say,
            "risky_parts": self.risky_parts,
            "mark": self.mark,
            "can_be_undone": True,
        }


def set_up(config: LoadedConfig, kinds: list[str], trust: bool = True) -> Outcome:
    """Write the routes for these assistants, and trust the file if that is ours to do.

    Anything already in the file stays. Only the named routes are added or
    brought up to date, and the whole file is handed back so a person can read
    exactly what they now have.

    Trusting is the one part this will not do on somebody else's behalf. A
    settings file can start programs and name addresses to call, which is why
    trusting it is a deliberate act with the whole file in front of you. If a
    file was already there and nobody had trusted it, this writes the routes and
    stops: trusting it here would hand authority to lines this tool never read
    and nobody ever agreed to. A file this tool created, or one that was already
    trusted, is a different matter, and those it trusts.
    """

    ready = {seat.kind for seat in look(config).ready}
    missing = [kind for kind in kinds if kind not in ready]
    if missing:
        recipe = recipe_for(missing[0])
        raise SeatError(
            f"{recipe.label} is not ready on this machine, so no route was written for it. "
            f"{recipe.install_hint}"
        )
    routes = routes_for(config, kinds)
    local = confined_path(
        config.project_root, ".harness/config.local.json",
        allow_missing=True, allow_control=True,
    )
    # The do-it-for-me button writes this same file, from another card and
    # possibly another browser tab. Read, change, write without a lock means
    # whichever write lands second throws away what the other one added.
    from .safety import ProjectTransactionLock

    with ProjectTransactionLock(config.project_root).held(60.0):
        return _write_the_routes(config, kinds, routes, local, trust)


def write_one_route(config: LoadedConfig, name: str, route: dict[str, Any]) -> Outcome:
    """Add one route to the settings without touching anything else.

    Setting up a seat writes routes and picks a default, because somebody who
    has just chosen their first assistant wants one. Adding a model that is
    already running here is a smaller thing: it is one more assistant to hand a
    job to, and whatever was already the default was somebody's decision.
    """

    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name):
        raise SeatError(
            f"{name!r} is not a name a route can have. Names start with a letter "
            "and hold letters, numbers, dashes and underscores."
        )
    local = confined_path(
        config.project_root, ".harness/config.local.json",
        allow_missing=True, allow_control=True,
    )
    # Checked against the file that is really on disk, inside the same lock the
    # write happens in. Checked against the settings held in memory instead, it
    # was checking a different thing from the one being written: the write
    # re-reads the file and merges onto whatever is there. And when that file is
    # already broken, the settings in memory are quietly the defaults - so the
    # check saw a clean slate, passed, and the file stayed exactly as unloadable
    # as before while this said it had worked.
    with _while_the_settings_are_written:
        _would_the_settings_still_load(local, name, route)
        return _write_the_routes(config, [], {name: route}, local, True)


def _would_the_settings_still_load(local: Path, name: str, route: dict[str, Any]) -> None:
    """Refuse a route that would stop the settings file loading.

    Read off the disk rather than out of memory, because the disk is what the
    write is about to change and the two are not always the same thing.
    """

    import copy as copy_lab

    from .config import DEFAULT_CONFIG, validate_config

    on_disk: dict[str, Any] = {}
    if local.is_file():
        try:
            on_disk = json.loads(local.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SeatError(
                f"{local.name} is there and cannot be read: {exc}. Fix or move "
                "that file first; nothing was changed."
            ) from exc
    if not isinstance(on_disk, dict):
        raise SeatError(f"{local.name} does not hold settings, so it was left alone")
    trying = copy_lab.deepcopy(DEFAULT_CONFIG)
    for key, value in on_disk.items():
        if isinstance(value, dict) and isinstance(trying.get(key), dict):
            trying[key] = {**trying[key], **value}
        else:
            trying[key] = value
    trying.setdefault("providers", {})
    was_already_broken = False
    try:
        validate_config(copy_lab.deepcopy(trying))
    except HarnessError as exc:
        # Broken before this touched it. Said plainly rather than blamed on the
        # route being added, and rather than reported as a success that changed
        # nothing.
        was_already_broken = True
        already = str(exc)
    trying["providers"][name] = route
    try:
        validate_config(trying)
    except HarnessError as exc:
        if was_already_broken:
            raise SeatError(
                f"{local.name} does not load as it is, before anything was added: "
                f"{already} Fix that first; nothing was changed."
            ) from exc
        raise SeatError(
            f"{name} was not written, because the settings would not load with it: "
            f"{exc} Nothing was changed."
        ) from exc
    if was_already_broken:
        raise SeatError(
            f"{local.name} does not load as it is: {already} Adding {name} would "
            "not have helped, so nothing was changed. Fix that first."
        )


def _write_the_routes(config, kinds, routes, local, trust) -> Outcome:
    settings: dict[str, Any] = {}
    kept: list[str] = []
    was_there = local.is_file()
    was_trusted = was_there and is_project_local_config_trusted(config.project_root, local)
    previous = Before(
        contents=local.read_text(encoding="utf-8") if was_there else None,
        trusted=was_trusted,
    )
    if was_there:
        try:
            settings = json.loads(local.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SeatError(
                f"{local.name} is there and cannot be read: {exc}. "
                "Fix or move that file first; nothing was changed."
            ) from exc
        if not isinstance(settings, dict):
            raise SeatError(f"{local.name} does not hold settings, so it was left alone")
        kept = sorted(key for key in settings if key not in ("providers", "provider"))
    existing = settings.get("providers")
    existing = existing if isinstance(existing, dict) else {}
    # Say out loud what is being written over. A route with the same name, or a
    # default already chosen, is somebody's earlier decision.
    replaced = sorted(name for name in routes if name in existing)
    if isinstance(settings.get("provider"), dict) and settings["provider"].get("name"):
        replaced.append(f"the assistant used by default ({settings['provider']['name']})")
    settings["providers"] = {**existing, **routes}
    # The route used when nothing else is said. The first one chosen, so a
    # single seat needs no further setting up.
    #
    # Unless no seat was chosen at all, which is what adding one more assistant
    # on its own looks like. Whatever was already the default was somebody's
    # decision and this is not the moment to overturn it.
    if kinds:
        first = kinds[0]
        settings["provider"] = {
            "name": first,
            "model": _default_model(config, first),
            "endpoint": "",
            "api_key_env": "",
        }
        replaced_default = True
    else:
        replaced_default = False
    if not replaced_default:
        replaced = [one for one in replaced if not one.startswith("the assistant used by")]
    local.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(settings, indent=2) + "\n"
    local.write_text(body, encoding="utf-8")

    trusted = False
    note = ""
    needs_your_say = False
    risky_parts: list[str] = []
    mine_to_trust = not was_there or was_trusted
    if trust and mine_to_trust:
        try:
            trust_project_local_config(config.project_root, local)
            trusted = True
        except HarnessError as exc:
            note = f"The settings were written, and trusting them did not work: {exc}"
    elif trust:
        # Not this tool's decision to make on its own, and not this tool's
        # decision to refuse either. The routes are written, the whole file is
        # handed back to read, what carries risk is named, and the choice is
        # put in front of the person whose machine it is.
        needs_your_say = True
        risky_parts = what_makes_it_risky(settings)
        note = (
            f"{local.name} was already here and nobody has said it is theirs. "
            "Trusting it lets everything in it act, including anything this tool "
            "did not write. Read it below, then decide."
        )
    else:
        note = "Not trusted yet. Run: harness trust"
    return Outcome(
        settings_file=local.as_posix(),
        routes=sorted(routes),
        contents=body,
        trusted=trusted,
        kept=kept,
        replaced=replaced,
        note=note,
        needs_your_say=needs_your_say,
        risky_parts=risky_parts,
        mark=mark_of(body),
        previous=previous,
    )


def mark_of(contents: str) -> str:
    """A short mark for a piece of text, so two of them can be compared."""

    return hashlib.sha256(contents.encode("utf-8")).hexdigest()[:32]


def trust_it_anyway(config: LoadedConfig, seen: str = "") -> str:
    """Trust the settings file because the person whose machine it is said so.

    Only ever called from a deliberate press or a typed --yes, never on the way
    past. It also insists on being told the mark of the file that was read: a
    file can change between being shown and being trusted, and trusting one
    nobody read is the one thing this whole path exists to prevent. Without
    that, "the panel shows the file first" is a promise the panel makes and
    nothing checks.
    """

    local = confined_path(
        config.project_root, ".harness/config.local.json",
        allow_missing=True, allow_control=True,
    )
    if not local.is_file():
        raise SeatError("There is no settings file to trust yet")
    now = mark_of(local.read_text(encoding="utf-8"))
    if not seen:
        raise SeatError(
            "Trusting a settings file means saying you have read it, so this has "
            "to be told which file you read. Set the seats up again to see it."
        )
    if seen != now:
        raise SeatError(
            f"{local.name} has changed since it was shown to you. Look at it again "
            "before deciding: what you read is not what you would be trusting."
        )
    trust_project_local_config(config.project_root, local)
    return (
        f"{local.name} is trusted. Change it again and it goes back to untrusted, "
        "on purpose, so a later edit gets the same look."
    )


def put_it_back(config: LoadedConfig, previous: Before | None) -> str:
    """Undo a setup, by putting the file back exactly as it was, trust and all.

    Only ever writes text this tool itself read a moment earlier, never text
    from a request, so undoing cannot be turned into a way of writing anything
    anywhere. The trust mark goes back to what it was too: a file that nobody
    had trusted before must not come back trusted.
    """

    local = confined_path(
        config.project_root, ".harness/config.local.json",
        allow_missing=True, allow_control=True,
    )
    if previous is None or previous.contents is None:
        local.unlink(missing_ok=True)
        return "There was no settings file before, so the one written was removed."
    local.write_text(previous.contents, encoding="utf-8")
    if not previous.trusted:
        return (
            "Your settings were put back. They were not trusted before either, "
            "so they are untrusted again: harness trust"
        )
    try:
        trust_project_local_config(config.project_root, local)
    except HarnessError:
        return "Your settings were put back. They will need trusting again: harness trust"
    return "Your settings were put back exactly as they were."


def share_the_work(graph: dict[str, Any], routes: list[str]) -> dict[str, Any]:
    """Give each agent in a workflow one of these routes, and let them talk.

    With two routes the reviewer is put on the other assistant from the coder,
    which is the point of using two: they do not share a blind spot. With one
    route every agent uses it.
    """

    if not routes:
        raise SeatError("There are no routes to share out")
    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list):
        raise SeatError("That is not a workflow this can change")
    changed = json.loads(json.dumps(graph))
    for node in changed["nodes"]:
        if not isinstance(node, dict):
            continue
        job = DEFAULT_JOBS.get(str(node.get("id")))
        if job is None:
            continue
        config = node.setdefault("config", {})
        if not isinstance(config, dict):
            config = node["config"] = {}
        config["provider_route"] = routes[job % len(routes)]
        wanted = {"team.message", "workspace.read"}
        if str(node.get("type")) == "coder":
            wanted.add("workspace.write")
        config["capabilities"] = sorted(set(config.get("capabilities") or []) | wanted)
    return changed


def summary(look_found: Look) -> list[str]:
    """Plain lines about what is on this machine."""

    out: list[str] = []
    for seat in look_found.seats:
        if seat.ready:
            out.append(f"{seat.label}: ready, {seat.version}")
        else:
            out.append(f"{seat.label}: not ready. {seat.why_not}")
    if not look_found.ready:
        out.append("No assistant is ready yet, so there is nothing to set up.")
    return out
