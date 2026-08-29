"""Every setting, in plain words, and a safe way to change one.

Until now everything except the model routes had to be changed by opening
.harness/config.json in an editor and knowing the name of the key. That is
fine for somebody who wrote the file and hopeless for anybody else.

This reads every setting the harness has, says what each one means, what it is
set to now, what it shipped as, and which file that value came from. Changing
one writes it to the right file and then reads the whole config back the way
the harness really reads it: anything the harness would refuse is put back
exactly as it was, and the reason is handed to the person rather than left for
them to find out at the next run.

Two files, and which one a setting goes in
  - .harness/config.json is the shareable one. It goes in your repository, and
    everyone on the project gets it.
  - .harness/config.local.json is yours. Settings that can start programs, call
    addresses, or hand over your environment only count from there, and only
    once you have said the file is yours.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import (
    is_project_shared_config_trusted,
    DEFAULT_CONFIG,
    LoadedConfig,
    is_project_local_config_trusted,
    load_config,
    trust_project_local_config,
)
from .models import HarnessError
from .safety import confined_path

SHAREABLE = ".harness/config.json"
YOURS = ".harness/config.local.json"


class SettingsError(HarnessError):
    """A setting that cannot be read or changed."""


# What each part of the settings is for, in the words the panel shows above the
# group. A section with no name here still appears, under its own.
GROUPS: dict[str, tuple[str, str]] = {
    "provider": ("The model", "Which model the harness asks, and how it asks."),
    "providers": ("Model routes", "Named routes, so different agents can use different models."),
    "qa": ("Checks", "How your checks are found and run."),
    "project": ("Your project", "The commands the harness runs, and what it reads."),
    "execution": ("Running programs", "How the harness starts anything, and what it hands over."),
    "workflow": ("The workflow", "How many agents review a change, and how hard they try."),
    "git": ("Git", "What the harness may do with your repository."),
    "memory": ("What it remembers", "What the harness keeps between runs."),
    "context": ("What it reads", "How much of your project goes to the model."),
    "agents": ("Agents", "The agents in the workflow and what each may do."),
    "mcp": ("Other tools", "Other programs the harness talks to."),
    "plugins": ("Plugins", "Code of your own that adds kinds of check."),
    "pricing": ("Cost", "What the harness assumes things cost."),
    "ui": ("The panel", "The control panel itself."),
    "schema_version": ("Version", "Which shape these settings are written in."),
}

# Plain words for the settings somebody is most likely to want to change. Every
# other setting still appears, named as it is written: a test holds this whole
# view against the settings the harness really has, so nothing can hide.
MEANS: dict[str, str] = {
    "provider.name": "Which kind of model service to use.",
    "provider.model": "The name of the model to ask.",
    "provider.endpoint": "The address to call. Empty for a tool that is already signed in.",
    "provider.api_key_env": "The name of the environment variable holding your key. Never the key itself.",
    "provider.temperature": "How adventurous the model is allowed to be. Lower is steadier.",
    "provider.max_output_tokens": "The most the model may write in one answer.",
    "provider.timeout_seconds": "How long to wait for the model before giving up.",
    "qa.suite": "Which file your checks live in.",
    "qa.workers": "How many checks run side by side.",
    "qa.default_timeout_seconds": "How long a check may take before it is called failed.",
    "qa.retries": "How many times a check is tried again before it counts as failed.",
    "qa.allow_hosts": "Addresses your checks are allowed to call.",
    "project.test_commands": "The command that runs your tests.",
    "project.lint_commands": "The command that checks your code style.",
    "project.build_commands": "The command that builds your project.",
    "project.ignore": "Files and folders the harness leaves alone.",
    "execution.timeout_seconds": "How long any program the harness starts may run.",
    "execution.inherit_environment": "Which of your environment variables programs are handed.",
    "execution.mode": "Whether work runs on this machine or inside a container.",
    "workflow.reviewers": "How many agents review a change before you see it.",
    "workflow.context_tool_execution_seconds": (
        "Total time actually spent running long-horizon context tools. "
        "Zero means no aggregate time ceiling; individual programs still have their own timeout."
    ),
    "workflow.max_repair_attempts": "How many times the harness tries again after a check fails.",
    "git.enabled": "Whether the harness looks at your repository at all.",
    "git.allow_commit": "Whether the harness may commit.",
    "git.allow_push": "Whether the harness may push.",
    "memory.enabled": "Whether the harness remembers anything between runs.",
    "context.max_files": "How many files of your project the model may be shown.",
    "ui.host": "Which address the panel listens on. Always this machine.",
}

# Settings the harness refuses to honour from the shareable file. These are the
# ones that can start a program, call an address away from this machine, or
# hand over your environment, so they only count from your own file, once you
# have said that file is yours. Held against config.py by a test.
ONLY_FROM_YOUR_OWN_FILE: tuple[str, ...] = (
    "provider.api_key_env",
    "provider.command",
    "provider.endpoint",
    "providers",
    "mcp.servers",
    "plugins.enabled",
    "plugins.paths",
    "execution.inherit_environment",
    "execution.mode",
    "execution.docker_image",
    "execution.docker_network",
    "git.allow_commit",
    "git.allow_push",
    "qa.allow_hosts",
    "project.test_commands",
    "project.lint_commands",
    "project.build_commands",
    "project.security_commands",
    "project.performance_commands",
    "memory.allow_remote_embeddings",
)


@dataclass
class Setting:
    """One setting, and everything a person needs to decide about it."""

    key: str
    group: str
    label: str
    means: str
    value: Any
    shipped: Any
    came_from: str
    kind: str
    needs_your_own_file: bool
    changed: bool = False
    choices: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "group": self.group,
            "label": self.label,
            "means": self.means,
            "value": self.value,
            "shipped": self.shipped,
            "came_from": self.came_from,
            "kind": self.kind,
            "needs_your_own_file": self.needs_your_own_file,
            "changed": self.changed,
            "choices": self.choices,
        }


def _leaves(held: dict[str, Any], above: str = "") -> list[tuple[str, Any]]:
    """Every setting in a nest of settings, by its dotted name."""

    found: list[tuple[str, Any]] = []
    for name, value in held.items():
        dotted = f"{above}.{name}" if above else name
        if isinstance(value, dict) and value and all(isinstance(key, str) for key in value):
            deeper = _leaves(value, dotted)
            # A named collection - the routes, the agents - is one setting, not
            # one per name somebody happened to add.
            found.extend(deeper if deeper else [(dotted, value)])
        else:
            found.append((dotted, value))
    return found


def _kind_of(value: Any) -> str:
    if isinstance(value, bool):
        return "yes or no"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "settings of its own"
    return "text"


def _label_for(key: str) -> str:
    """A readable name for a setting nobody has written words for."""

    last = key.split(".")[-1].replace("_", " ")
    return last[:1].upper() + last[1:]


def _tidy(value: Any) -> Any:
    return copy.deepcopy(value)


def everything(config: LoadedConfig) -> list[Setting]:
    """Every setting the harness has, with what it is and where it came from."""

    shipped = {key: value for key, value in _leaves(DEFAULT_CONFIG)}
    out: list[Setting] = []
    for key, was in shipped.items():
        section = key.split(".")[0]
        group, _about = GROUPS.get(section, (section, ""))
        now = config.get(key, was)
        came = config.provenance.get(key, "")
        out.append(
            Setting(
                key=key,
                group=group,
                label=_label_for(key),
                means=MEANS.get(key, ""),
                value=_tidy(now),
                shipped=_tidy(was),
                came_from=_where(came),
                kind=_kind_of(was),
                needs_your_own_file=needs_your_own_file(key),
                changed=_tidy(now) != _tidy(was),
            )
        )
    out.sort(key=lambda item: (item.group, item.key))
    return out


def groups() -> list[dict[str, str]]:
    return [
        {"section": section, "name": name, "about": about}
        for section, (name, about) in GROUPS.items()
    ]


def _where(source: str) -> str:
    """Which file a value came from, said the way a person would say it.

    The harness records this as the path it read, so this turns a path back
    into words. Anything it does not recognise is named by its file name,
    which is still better than a line of somebody's folders.
    """

    if not source:
        return "how it shipped"
    plain = {
        "default": "how it shipped",
        "project": "the shared settings file",
        "local": "your own settings file",
        "user": "your settings for every project",
        "environment": "an environment variable",
        "explicit": "a settings file you named",
        "command-line": "the command you typed",
    }
    if source in plain:
        return plain[source]
    tail = source.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if tail == "config.local.json":
        return "your own settings file"
    if tail == "config.json":
        return "the shared settings file"
    if tail.endswith(".json"):
        return f"the settings in {tail}"
    return source


def needs_your_own_file(key: str) -> bool:
    """Does this setting only count from the file that is yours?"""

    return any(key == named or key.startswith(f"{named}.") for named in ONLY_FROM_YOUR_OWN_FILE)


def _file_for(config: LoadedConfig, key: str) -> Path:
    relative = YOURS if needs_your_own_file(key) else SHAREABLE
    return confined_path(config.project_root, relative, allow_missing=True, allow_control=True)


def _read(where: Path) -> dict[str, Any]:
    if not where.is_file():
        return {}
    try:
        held = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SettingsError(
            f"{where.name} cannot be read: {exc}. Fix or move that file first; "
            "nothing was changed."
        ) from exc
    if not isinstance(held, dict):
        raise SettingsError(f"{where.name} does not hold settings, so it was left alone")
    return held


def _kept_in_order(now: dict[str, Any], before: Any, shipped: Any) -> dict[str, Any]:
    """The settings, in the order the file already had them.

    Somebody's settings file is theirs, and rewriting it top to bottom because
    one number changed turns a one-line change into a page of them. So the
    order the file had is kept, and only a setting that was not in it before
    has to be placed: it goes where the harness's own order would put it,
    beside the settings it belongs with. A setting taken out and put back then
    lands where it was, rather than at the bottom.
    """

    had = list(before) if isinstance(before, dict) else []
    order = [name for name in had if name in now]
    shipped_order = list(shipped) if isinstance(shipped, dict) else []
    for name in now:
        if name in order:
            continue
        if name in shipped_order:
            before_it = [
                other for other in shipped_order[: shipped_order.index(name)] if other in order
            ]
            order.insert(order.index(before_it[-1]) + 1 if before_it else 0, name)
        else:
            order.append(name)
    tidy: dict[str, Any] = {}
    for name in order:
        value = now[name]
        if isinstance(value, dict):
            tidy[name] = _kept_in_order(
                value,
                before.get(name) if isinstance(before, dict) else None,
                shipped.get(name) if isinstance(shipped, dict) else None,
            )
        else:
            tidy[name] = value
    return tidy


def _written_out(held: dict[str, Any], before: Any = None) -> str:
    """A settings file, written the one way this always writes them."""

    return json.dumps(_kept_in_order(held, before, DEFAULT_CONFIG), indent=2) + "\n"


def _put_the_file(where: Path, body: str) -> None:
    """Write a settings file in one move, or not at all.

    Writing straight over the file leaves a half-written one behind if the
    machine is turned off in the middle, and a half-written settings file stops
    every harness command until somebody repairs it by hand. Writing beside it
    and then moving it into place cannot leave anything half done.
    """

    where.parent.mkdir(parents=True, exist_ok=True)
    beside = where.with_suffix(where.suffix + ".part")
    try:
        beside.write_text(body, encoding="utf-8")
        os.replace(beside, where)
    except OSError as exc:
        beside.unlink(missing_ok=True)
        raise SettingsError(f"{where.name} could not be written: {exc}") from exc


def _put(held: dict[str, Any], key: str, value: Any) -> None:
    parts = key.split(".")
    at = held
    for part in parts[:-1]:
        nest = at.get(part)
        if not isinstance(nest, dict):
            nest = at[part] = {}
        at = nest
    at[parts[-1]] = value


def _take_out(held: dict[str, Any], key: str) -> None:
    """Take a setting out, and any nest it leaves empty behind it.

    Leaving "qa": {} behind means a file that was put back is not the file it
    was, which matters to anybody comparing the two.
    """

    parts = key.split(".")
    nests: list[tuple[dict[str, Any], str]] = []
    at = held
    for part in parts[:-1]:
        nest = at.get(part)
        if not isinstance(nest, dict):
            return
        nests.append((at, part))
        at = nest
    at.pop(parts[-1], None)
    for above, name in reversed(nests):
        if isinstance(above.get(name), dict) and not above[name]:
            above.pop(name)


def _as_the_shipped_one(key: str, value: Any) -> Any:
    """Read what somebody typed as the kind of thing this setting holds."""

    was = None
    for dotted, shipped in _leaves(DEFAULT_CONFIG):
        if dotted == key:
            was = shipped
            break
    if was is None:
        raise SettingsError(f"There is no setting called {key}")
    if isinstance(was, bool):
        if isinstance(value, bool):
            return value
        if str(value).strip().lower() in ("true", "yes", "on", "1"):
            return True
        if str(value).strip().lower() in ("false", "no", "off", "0"):
            return False
        raise SettingsError(f"{key} is a yes or no, and {value!r} is neither")
    if isinstance(was, int) and not isinstance(was, bool):
        try:
            return int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise SettingsError(f"{key} is a whole number, and {value!r} is not") from exc
    if isinstance(was, float):
        try:
            return float(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise SettingsError(f"{key} is a number, and {value!r} is not") from exc
    if isinstance(was, list):
        if isinstance(value, list):
            return value
        text = str(value).strip()
        if not text:
            return []
        # A written-out list is read as one. Anything else is read as what the
        # setting holds: "true" is a command called true, not the word true.
        if text[:1] in "[{":
            try:
                read = json.loads(text)
            except json.JSONDecodeError:
                pass
            else:
                return read if isinstance(read, list) else [read]
        elif not key.endswith("_commands"):
            try:
                read = json.loads(text)
            except json.JSONDecodeError:
                pass
            else:
                return read if isinstance(read, list) else [read]
        if key.endswith("_commands"):
            # A command is a program and its arguments, and the harness keeps
            # each one as a list so nothing is handed to a shell. Somebody
            # typing "pytest -q" means exactly that, and should not have to
            # know it is written [["pytest", "-q"]]. One command per line.
            import shlex

            commands = []
            for line in text.splitlines():
                if line.strip():
                    commands.append(shlex.split(line.strip()))
            return commands
        return [part.strip() for part in text.split(",") if part.strip()]
    if isinstance(was, dict):
        if isinstance(value, dict):
            return value
        try:
            read = json.loads(str(value))
        except json.JSONDecodeError as exc:
            raise SettingsError(f"{key} holds settings of its own, written as JSON") from exc
        if not isinstance(read, dict):
            raise SettingsError(f"{key} holds settings of its own, written as JSON")
        return read
    return str(value)


@dataclass
class Changed:
    """What a change did, and what it means."""

    key: str
    value: Any
    file: str
    note: str
    needs_trusting: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "file": self.file,
            "note": self.note,
            "needs_trusting": self.needs_trusting,
        }


def change(config: LoadedConfig, key: str, value: Any) -> Changed:
    """Write one setting, and put it straight back if the harness refuses it.

    The check is the real one: after writing, the whole config is read the way
    the harness reads it at the start of every run. Anything it will not accept
    is undone here and now, with the reason, rather than turning up as a run
    that will not start tomorrow.
    """

    wanted = _as_the_shipped_one(key, value)
    # Which file a setting belongs in is not a list to keep up to date: it is
    # whatever the harness will accept. So the shareable file is tried first,
    # and if the harness refuses the setting from there, the same change is
    # tried in the file that is yours. That covers the ones nobody would guess
    # - a limit that may be lowered by the shared file but only raised by your
    # own - without anybody having to know the rule.
    first = YOURS if needs_your_own_file(key) else SHAREABLE
    order = [first] + [other for other in (SHAREABLE, YOURS) if other != first]
    refused = ""
    for relative in order:
        done, refused = _try_writing(config, key, wanted, relative)
        if done:
            return done
    raise SettingsError(f"{key} was put back, because the harness refuses it: {refused}")


def _try_writing(config: LoadedConfig, key: str, wanted: Any, relative: str):
    """Write this setting into this file, and undo it if the harness refuses."""

    where = confined_path(
        config.project_root, relative, allow_missing=True, allow_control=True
    )
    before = where.read_text(encoding="utf-8") if where.is_file() else None
    shared = confined_path(
        config.project_root, SHAREABLE, allow_missing=True, allow_control=True
    )
    was_trusted = where.is_file() and (
        is_project_shared_config_trusted(config.project_root)
        if where == shared
        else is_project_local_config_trusted(config.project_root, where)
    )
    held = _read(where)
    had = copy.deepcopy(held)
    _put(held, key, wanted)
    where.parent.mkdir(parents=True, exist_ok=True)
    _put_the_file(where, _written_out(held, had))

    needs_trusting = False
    if relative == YOURS or was_trusted:
        # The same rule as everywhere else: a file this tool created, or one
        # already trusted, it may trust. One somebody else left, it may not.
        #
        # The second half matters more than it looks. Changing a setting
        # rewrites the file, and a file that has changed is no longer the file
        # somebody read and said yes to - so without this, using the settings
        # view once left the project unusable until it was trusted again.
        if before is None or was_trusted:
            try:
                trust_project_local_config(config.project_root, where)
            except HarnessError:
                needs_trusting = True
        else:
            needs_trusting = True

    trouble = _would_the_harness_accept_this(config.project_root)
    if not trouble:
        # Written, accepted, and still not what the harness will read: another
        # file already sets this and wins. Saying "it is now 8" while the
        # harness goes on reading 2 is the worst kind of wrong, so this is
        # noticed here rather than left for somebody to spot in the next line
        # of the panel.
        reading = _what_the_harness_reads(config.project_root, key)
        if reading is not _NOTHING and reading != wanted:
            _put_it_back(where, before)
            return None, (
                f"{key} is set to {json.dumps(reading)} somewhere that wins over "
                f"{where.name}, so writing it there would have changed nothing. "
                "Change it where it is set, or take it out there first."
            )
    if trouble and needs_trusting and "trusted" in trouble:
        # Written into the file that is yours, which nobody has said is theirs
        # yet. The harness refusing it is exactly what that means, and undoing
        # the writing would leave somebody with no way to set it at all.
        trouble = ""
    if trouble:
        _put_it_back(where, before)
        return None, trouble
    note = f"{key} is now {json.dumps(wanted)}, written in {where.name}."
    if relative == YOURS and not needs_your_own_file(key):
        note += " That one only counts from your own file, so it went there."
    if needs_trusting:
        note += (
            f" {where.name} is yours and is not trusted yet, so this will not count "
            "until you say the file is yours."
        )
    return Changed(key=key, value=wanted, file=where.as_posix(), note=note,
                   needs_trusting=needs_trusting), ""


def reset(config: LoadedConfig, key: str) -> Changed:
    """Put one setting back to how it shipped, by taking it out of your files."""

    _as_the_shipped_one(key, config.get(key))  # refuses a name that is not a setting
    touched: list[str] = []
    for relative in (SHAREABLE, YOURS):
        where = confined_path(
            config.project_root, relative, allow_missing=True, allow_control=True
        )
        if not where.is_file():
            continue
        held = _read(where)
        had = copy.deepcopy(held)
        before_text = where.read_text(encoding="utf-8")
        before = json.dumps(held, sort_keys=True)
        _take_out(held, key)
        if json.dumps(held, sort_keys=True) == before:
            continue
        was_trusted = is_project_local_config_trusted(config.project_root, where)
        _put_the_file(where, _written_out(held, had))
        if was_trusted:
            try:
                trust_project_local_config(config.project_root, where)
            except HarnessError:
                pass
        # The same gate as changing one. Taking a setting out can leave the
        # rest disagreeing with each other - a count that no longer matches the
        # list beside it - and a settings file the harness refuses stops every
        # command until somebody repairs it by hand.
        trouble = _would_the_harness_accept_this(config.project_root)
        if trouble:
            _put_it_back(where, before_text)
            if relative == YOURS and was_trusted:
                try:
                    trust_project_local_config(config.project_root, where)
                except HarnessError:
                    pass
            raise SettingsError(
                f"{key} was left as it was, because taking it out is something the "
                f"harness refuses: {trouble}"
            )
        touched.append(where.name)
    if not touched:
        return Changed(key=key, value=config.get(key), file="", note=f"{key} was already as it shipped.")
    return Changed(
        key=key,
        value=None,
        file=", ".join(touched),
        note=f"{key} is back to how it shipped, taken out of {', '.join(touched)}.",
    )


_NOTHING = object()


def _what_the_harness_reads(root: Path, key: str) -> Any:
    """What a run would really see for this setting, all files considered."""

    try:
        return load_config(root).get(key, _NOTHING)
    except HarnessError:
        return _NOTHING


def _would_the_harness_accept_this(root: Path) -> str:
    try:
        load_config(root)
    except HarnessError as exc:
        return str(exc)
    return ""


def _put_it_back(where: Path, before: str | None) -> None:
    if before is None:
        where.unlink(missing_ok=True)
        return
    where.write_text(before, encoding="utf-8")
