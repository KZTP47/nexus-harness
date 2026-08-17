"""Carry a setup to another machine, in one file.

Somebody who has spent an afternoon getting their checks, their pipelines and
their settings right should not have to do it again on the laptop, or hand a
colleague a list of instructions. `harness qa share` sends a report of a run,
which is a different thing: this packs up the setup itself.

What goes in
  - the shareable settings, .harness/config.json
  - the checks, and any other suites beside them
  - every saved pipeline

What never goes in
  - .harness/config.local.json. That file is yours: it names the tools on your
    machine, the addresses you call, and the variables holding your keys. It is
    the whole reason there are two settings files, and carrying it to another
    machine would undo that in one step.
  - anything a run wrote: evidence, screenshots, run folders. A setup is what
    you meant, not what happened.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import LoadedConfig
from .models import HarnessError
from .safety import confined_path

WHAT_THIS_IS = "nexus-harness-setup"
VERSION = 1
# Everything that may travel, and where it lives. Nothing outside this list is
# read, so a new kind of file cannot start travelling by accident.
CARRIES = (
    (".harness/config.json", "the shared settings"),
    (".harness/qa/suite.json", "your checks"),
    (".harness/qa/workflows.json", "your workflow checks"),
)
PIPELINES = ".harness/pipelines"
# Anything larger than this is not a setup, it is somebody's project.
MOST_BYTES = 4_000_000


class CarryError(HarnessError):
    """A setup that cannot be packed up or unpacked."""


@dataclass
class Packed:
    """What went into the file, and what did not."""

    file: str
    holds: list[str] = field(default_factory=list)
    left_out: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.file, "holds": self.holds, "left_out": self.left_out}


@dataclass
class Unpacked:
    """What was written, and what was left alone."""

    written: list[str] = field(default_factory=list)
    left_alone: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"written": self.written, "left_alone": self.left_alone, "note": self.note}


def _put_the_file(where: Path, body: str) -> None:
    """Write a file in one move, or not at all.

    Writing straight over a settings file leaves a half-written one behind if
    the machine stops in the middle, and that stops every harness command until
    somebody repairs it by hand.
    """

    where.parent.mkdir(parents=True, exist_ok=True)
    beside = where.with_suffix(where.suffix + ".part")
    try:
        beside.write_text(body, encoding="utf-8")
        os.replace(beside, where)
    except OSError as exc:
        beside.unlink(missing_ok=True)
        raise CarryError(f"{where.name} could not be written: {exc}") from exc


def _lay_over(whole: dict[str, Any], changes: Any) -> None:
    """Put one set of settings on top of another, the way the harness does."""

    if not isinstance(changes, dict):
        return
    for name, value in changes.items():
        if isinstance(value, dict) and isinstance(whole.get(name), dict):
            _lay_over(whole[name], value)
        else:
            whole[name] = value


def _make_sure_it_reads(relative: str, body: Any) -> None:
    """Refuse a carried file the harness itself would refuse."""

    if relative.endswith("config.json"):
        if not isinstance(body, dict):
            raise CarryError("The settings in that setup are not settings")
        # A settings file is a few changes on top of what the harness ships
        # with, not a whole config, so it is checked the way the harness checks
        # it: laid over the defaults, and the result read.
        import copy

        from .config import DEFAULT_CONFIG, validate_config

        whole = copy.deepcopy(DEFAULT_CONFIG)
        _lay_over(whole, body)
        try:
            validate_config(whole)
        except HarnessError as exc:
            raise CarryError(
                f"The settings in that setup are ones the harness refuses: {exc}"
            ) from exc
        return
    if "/qa/" in relative:
        from . import qa as qalab

        try:
            qalab.parse_suite(body)
        except HarnessError as exc:
            raise CarryError(
                f"The checks in that setup are ones the harness refuses: {exc}"
            ) from exc


def pack(config: LoadedConfig) -> dict[str, Any]:
    """Everything about this setup that is safe to hand to somebody else."""

    held: dict[str, Any] = {}
    holds: list[str] = []
    left_out: list[str] = []
    for relative, what in CARRIES:
        where = confined_path(
            config.project_root, relative, allow_missing=True, allow_control=True
        )
        if not where.is_file():
            left_out.append(f"{what}: there is none")
            continue
        try:
            body = json.loads(where.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            left_out.append(f"{what}: it could not be read ({exc})")
            continue
        held[relative] = body
        holds.append(what)

    pipelines: dict[str, Any] = {}
    folder = confined_path(
        config.project_root, PIPELINES, allow_missing=True, allow_control=True
    )
    if folder.is_dir():
        for path in sorted(folder.glob("*.json")):
            # A run writes its own files in here. Only a pipeline travels.
            try:
                body = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(body, dict) and isinstance(body.get("nodes"), list):
                pipelines[path.name] = body
    if pipelines:
        held[PIPELINES] = pipelines
        holds.append(f"{len(pipelines)} pipeline(s)")

    left_out.append("your own settings file, which never travels")
    return {
        "what": WHAT_THIS_IS,
        "version": VERSION,
        "from": config.project_root.name,
        "files": held,
        "holds": holds,
        "left_out": left_out,
    }


def write_to(config: LoadedConfig, where: str) -> Packed:
    """Pack this setup up and write it to a file in the project."""

    packed = pack(config)
    path = confined_path(config.project_root, where or "harness-setup.json", allow_missing=True)
    body = json.dumps(packed, indent=2) + "\n"
    if len(body.encode("utf-8")) > MOST_BYTES:
        raise CarryError(
            "This setup is larger than a setup should be. Something in the checks "
            "or the pipelines is holding a great deal of text."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return Packed(file=path.as_posix(), holds=packed["holds"], left_out=packed["left_out"])


def read_it(carried: Any) -> dict[str, Any]:
    """Check a carried setup over before anything is written."""

    if not isinstance(carried, dict):
        raise CarryError("That is not a setup file")
    if carried.get("what") != WHAT_THIS_IS:
        raise CarryError("That file was not written by the harness as a setup")
    if carried.get("version") != VERSION:
        raise CarryError(
            f"That setup was written by a different version of the harness "
            f"({carried.get('version')}), and this one reads version {VERSION}"
        )
    files = carried.get("files")
    if not isinstance(files, dict) or not files:
        raise CarryError("That setup holds nothing")
    allowed = {relative for relative, _what in CARRIES} | {PIPELINES}
    for relative in files:
        if relative not in allowed:
            raise CarryError(f"A setup may not carry {relative}")
    pipelines = files.get(PIPELINES)
    if pipelines is not None and not isinstance(pipelines, dict):
        raise CarryError("The pipelines in that setup are not pipelines")
    return carried


def unpack(config: LoadedConfig, carried: Any, *, over_the_top: bool = False) -> Unpacked:
    """Write a carried setup into this project.

    Nothing is written over unless somebody says so. Landing on a project that
    already has checks and quietly replacing them would be the worst possible
    first impression of a feature meant to save time.
    """

    tidy = read_it(carried)
    from . import pipelines as pipeline_lab

    written: list[str] = []
    left_alone: list[str] = []
    for relative, what in CARRIES:
        body = tidy["files"].get(relative)
        if body is None:
            continue
        where = confined_path(
            config.project_root, relative, allow_missing=True, allow_control=True
        )
        if where.is_file() and not over_the_top:
            left_alone.append(f"{what}: {where.name} is already here")
            continue
        # Read it the way the harness reads that kind of file, before it
        # lands. A setup file is something somebody was handed, and writing
        # a broken one leaves the project unreadable at the next command.
        _make_sure_it_reads(relative, body)
        _put_the_file(where, json.dumps(body, indent=2) + "\n")
        written.append(what)

    carried_pipelines = tidy["files"].get(PIPELINES) or {}
    folder = confined_path(
        config.project_root, PIPELINES, allow_missing=True, allow_control=True
    )
    for name, body in carried_pipelines.items():
        # Read every one the way the harness reads a pipeline, before it lands.
        # A setup file is something somebody was handed, and a drawing nobody
        # checked is exactly what should not be written into a project.
        tidy_one = pipeline_lab.read_it(body)
        where = pipeline_lab.file_for(config, tidy_one["name"])
        if where.is_file() and not over_the_top:
            left_alone.append(f"the pipeline {tidy_one['name']} is already here")
            continue
        folder.mkdir(parents=True, exist_ok=True)
        _put_the_file(where, json.dumps(tidy_one, indent=2) + "\n")
        written.append(f"the pipeline {tidy_one['name']}")

    note = (
        f"{len(written)} thing(s) written."
        if written
        else "Nothing was written: everything in it is already here."
    )
    if left_alone and not over_the_top:
        note += " Nothing already here was written over."
    note += (
        " Your own settings file never travels, so the model routes on this "
        "machine are still yours to set up."
    )
    return Unpacked(written=written, left_alone=left_alone, note=note)
