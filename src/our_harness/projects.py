"""The projects you work on, and which one the panel is showing.

The harness has always worked on one project at a time, and everything it keeps
- the automations, the timers, the checks, the team, what it has learnt - lives
inside that project's own folder. That part was right from the start. What was
missing is anywhere in the panel that says *which* project you are looking at,
and any way to get to another one without stopping the harness and starting it
again with a different folder.

Two lists, kept apart on purpose
--------------------------------

**Which projects this machine knows about** is about you and this machine. It
lives beside your own settings, not inside any project, because a list of the
folders on your computer is nobody else's business and would be nonsense to
anybody who cloned your repository.

**What a project is called** lives inside the project. A name is about the
project, so it travels with it: clone it onto another machine and it is still
called the same thing. With no name, the folder's own name is used, which is
right often enough that most people never type one.

Nothing here ever deletes a project
-----------------------------------

Taking one off the list means the panel stops listing it. The folder, and
everything in it, is left exactly where it was. That is why the word is
"forget" rather than "delete": there is no version of this that loses your
work, and the panel says so where the button is.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import HarnessError

# How the list of projects can be shown. Slide-out is the way it starts: the
# panel keeps its full width, and the list comes out when you ask for it.
# Always means it stays there, the way an editor does it. It is a choice about
# how somebody likes to work, so it is theirs to make and it is remembered.
HOW_IT_CAN_LOOK = ("slide-out", "always")
THE_USUAL_LOOK = "slide-out"

# The most projects kept in the list. Past this it is not a list anybody reads,
# and the oldest one falls off the end rather than growing without limit.
MOST_KEPT = 40

# The most letters in a name somebody types.
LONGEST_NAME = 60


class ProjectError(HarnessError):
    """Something wrong with a project, or with the list of them."""


@dataclass
class Project:
    """One project this machine knows about."""

    path: str
    name: str
    is_there: bool
    last_opened: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "is_there": self.is_there,
            "last_opened": self.last_opened,
        }


# --------------------------------------------------------------------------
# What a project is called. Kept inside the project, so it travels with it.
# --------------------------------------------------------------------------


def _where_the_name_lives(where: Path) -> Path:
    return where / ".harness" / "project.json"


def name_of(where: Path | str) -> str:
    """What this project is called, or its folder's name.

    The folder's name is right often enough that most people never type one,
    and it is never wrong in a way that loses anything.
    """

    where = Path(where)
    try:
        said = json.loads(_where_the_name_lives(where).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        said = None
    if isinstance(said, dict):
        name = str(said.get("name") or "").strip()
        if name:
            return name[:LONGEST_NAME]
    return where.name or str(where)


def rename(where: Path | str, name: str) -> str:
    """Give this project a name, or take its name away again.

    Written inside the project, because a name is about the project and not
    about this machine. An empty name puts it back to the folder's own name.
    """

    from .safety import put_this_file_in_place, take_the_file_away

    where = Path(where).resolve()
    if not where.is_dir():
        raise ProjectError(f"There is no folder at {where}.")
    name = " ".join(str(name or "").split())[:LONGEST_NAME]
    at = _where_the_name_lives(where)
    if not name:
        take_the_file_away(at, missing_ok=True)
        return name_of(where)
    at.parent.mkdir(parents=True, exist_ok=True)
    put_this_file_in_place(at, json.dumps({"name": name}, indent=2) + "\n")
    return name


# --------------------------------------------------------------------------
# The list of them, which is about this machine.
# --------------------------------------------------------------------------


def where_the_list_lives() -> Path:
    from .config import user_config_path

    return user_config_path().parent / "projects.json"


def _read_the_list() -> dict[str, Any]:
    try:
        said = json.loads(where_the_list_lives().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return said if isinstance(said, dict) else {}


def _keep_the_list(said: dict[str, Any]) -> None:
    from .safety import put_this_file_in_place

    where = where_the_list_lives()
    where.parent.mkdir(parents=True, exist_ok=True)
    put_this_file_in_place(where, json.dumps(said, indent=2) + "\n")


def _the_paths(said: dict[str, Any]) -> list[dict[str, Any]]:
    held = said.get("projects")
    if not isinstance(held, list):
        return []
    kept = []
    for one in held[:MOST_KEPT]:
        if not isinstance(one, dict):
            continue
        path = str(one.get("path") or "").strip()
        if not path:
            continue
        kept.append({
            "path": path,
            "last_opened": str(one.get("last_opened") or ""),
        })
    return kept


def every_one(also: Path | str | None = None) -> list[Project]:
    """Every project this machine knows about, the one you are on first.

    The one the panel is showing is always in the list, whether or not anybody
    added it - opening a folder is how it gets on the list, and it would be odd
    for the one in front of you to be missing from it.

    And it is first. Sorted only by when each was last opened, the one you were
    looking at could sit anywhere in the list, which is a strange thing to hand
    somebody: the row that says "you are working on this one" was somewhere in
    the middle. Everything after it is newest first, as before.
    """

    said = _the_paths(_read_the_list())
    if also is not None:
        here = str(Path(also).resolve())
        if not any(one["path"] == here for one in said):
            said.insert(0, {"path": here, "last_opened": ""})
    found = []
    for one in said:
        where = Path(one["path"])
        found.append(Project(
            path=one["path"],
            name=name_of(where),
            is_there=where.is_dir(),
            last_opened=one["last_opened"],
        ))
    here = str(Path(also).resolve()) if also is not None else ""
    return sorted(
        found,
        key=lambda one: (one.path != here, _the_other_way_round(one.last_opened)),
    )


def _the_other_way_round(said: str) -> str:
    """Newest first, out of a sort that goes smallest first.

    Every time is written the same way, so turning each letter around orders
    them backwards. Simpler than sorting twice, and it keeps the one you are on
    pinned to the top whatever its time says.
    """

    return "".join(chr(0x10FFFF - ord(one)) if ord(one) < 0x10FFFF else one for one in said)


def add(where: Path | str) -> Project:
    """Put a folder on the list.

    It has to be a folder that is really there. A path with nothing at it is
    almost always a typo, and a list full of those is a list nobody trusts.
    """

    where = Path(where).expanduser()
    try:
        where = where.resolve()
    except OSError as exc:
        raise ProjectError(f"That path cannot be read: {exc}") from exc
    if not where.is_dir():
        raise ProjectError(
            f"There is no folder at {where}. Pick the folder your project is in."
        )
    said = _read_the_list()
    kept = [one for one in _the_paths(said) if one["path"] != str(where)]
    kept.insert(0, {"path": str(where), "last_opened": ""})
    said["projects"] = kept[:MOST_KEPT]
    said.setdefault("schema_version", 1)
    _keep_the_list(said)
    return Project(str(where), name_of(where), True, "")


def forget(where: Path | str) -> str:
    """Take one off the list. The folder is left exactly where it was."""

    where = str(Path(where).resolve())
    said = _read_the_list()
    kept = [one for one in _the_paths(said) if one["path"] != where]
    said["projects"] = kept
    said.setdefault("schema_version", 1)
    _keep_the_list(said)
    return (
        f"{Path(where).name} is off the list. Nothing was deleted: the folder "
        "and everything in it is exactly where it was."
    )


def opened(where: Path | str, when: datetime | None = None) -> None:
    """Remember that this one was opened, so the list is in a useful order."""

    where = str(Path(where).resolve())
    said = _read_the_list()
    kept = [one for one in _the_paths(said) if one["path"] != where]
    kept.insert(0, {
        "path": where,
        "last_opened": (when or datetime.now()).isoformat(timespec="seconds"),
    })
    said["projects"] = kept[:MOST_KEPT]
    said.setdefault("schema_version", 1)
    _keep_the_list(said)


# --------------------------------------------------------------------------
# How somebody likes the list shown.
# --------------------------------------------------------------------------


def how_it_looks() -> str:
    said = str(_read_the_list().get("sidebar") or "").strip()
    return said if said in HOW_IT_CAN_LOOK else THE_USUAL_LOOK


def make_it_look(how: str) -> str:
    """Slide-out, or always there. It is remembered on this machine.

    Kept with the list rather than with a project's settings, because it is
    about how somebody likes to work and not about any one project. Kept in a
    project, the panel would change shape every time you switched.
    """

    how = str(how or "").strip()
    if how not in HOW_IT_CAN_LOOK:
        raise ProjectError(
            f"The list is shown one of these ways: {', '.join(HOW_IT_CAN_LOOK)}."
        )
    said = _read_the_list()
    said["sidebar"] = how
    said.setdefault("schema_version", 1)
    _keep_the_list(said)
    return how


# --------------------------------------------------------------------------
# What the panel is shown about where it is.
# --------------------------------------------------------------------------


def where_we_are(config) -> dict[str, Any]:
    """The project the panel is showing, said plainly."""

    where = Path(config.project_root).resolve()
    return {
        "path": str(where),
        "name": name_of(where),
        "folder": where.name,
        # Said separately so the panel can show a short path without guessing
        # where to cut it.
        "shortened": _short(where),
    }


def _short(where: Path) -> str:
    """The path with your home folder written as ~, which is how people read it."""

    try:
        home = Path.home().resolve()
        said = where.resolve()
        if said == home or home in said.parents:
            return "~" + os.sep + str(said.relative_to(home))
    except (OSError, ValueError):
        pass
    return str(where)
