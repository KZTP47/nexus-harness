"""Who works on what: a board of agents, projects, and the lines between them.

The harness could already do each of these things in its own tab. You could
build a team, you could talk to an assistant, you could pick a project. What
you could not do was see all of it at once - which agents you have, which
projects they are on, which of them are allowed to talk to each other - or
change any of it by dragging a box.

That is what this is. One board, above every project rather than inside any
one, because the whole point is agents working on more than one project at a
time.

What is on it
-------------

  - **Agents.** A name, which assistant on this machine it uses, and one line
    saying what it is for. Each keeps its own conversation, so two agents both
    using Claude do not read each other's half of it.
  - **Projects.** A folder, and the jobs you want done in it.
  - **Works on.** Which agents are on which projects. Many to many: one agent
    can be on three projects, one project can have three agents.
  - **Talks to.** Which pairs of agents may pass notes to each other while a
    run is going. Two agents that should not know about each other are two
    agents that will not hear from each other.

Nothing happens until somebody presses the button
------------------------------------------------

Adding an agent starts nothing, drawing a line starts nothing, writing a job
down starts nothing. `Running` below is the one part that reaches an assistant,
and only when somebody presses Set them going. A board that quietly set twelve
assistants going the moment you dragged a line would be a board nobody would
dare touch.
"""

from __future__ import annotations

import json
import os
import hashlib
import re
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any

from .models import HarnessError

# The most of each, so a board stays something a person can look at and a file
# stays something a machine can read quickly.
MOST_AGENTS = 24
MOST_PROJECTS = 12
BOARD_BINDING_SCHEMA_VERSION = 1
_WORKSPACE_ID = re.compile(
    r"^workspace-(?:[0-9a-f]{32}|legacy-[0-9a-f]{24}|saved-[0-9a-f]{24})$"
)


def _new_workspace_id() -> str:
    """Return an opaque identity for one board/chat workspace lineage."""

    return f"workspace-{uuid.uuid4().hex}"


MOST_TASKS = 40
# The most answers kept from one run of the board. Each agent on the second
# round is shown one answer per agent it may hear from, so a project with
# everybody talking to everybody writes down a great many copies of the same
# words. Past this the oldest fall off the end, and it says so.
MOST_NOTES = 200
# The most letters of one answer kept in the exchange. The whole answer is in
# that agent's own chat either way; this is the copy for reading who was shown
# what.
LONGEST_NOTE = 4000

LONGEST_NAME = 60
# A role description can contain real operating instructions, and a project
# task is the long-horizon goal itself.  These are deliberately aligned with
# the disclosed system-prompt and main-input boundaries.  The board reader
# rejects anything larger; it never slices a saved or pasted instruction.
LONGEST_JOB = 100_000
LONGEST_TASK = 200_000

# A name for an agent, which is also the name its conversation is filed under.
# Letters, numbers, spaces, dashes and underscores: the same shape the chat
# already allows, because that is where the name ends up.
A_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,59}$")
AN_AGENT_COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")
AN_AGENT_PICTURE = re.compile(
    r"^data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/]+={0,2}$"
)
AGENT_ICONS = {"robot", "person", "code", "star", "brain"}
DEFAULT_AGENT_COLOUR = "#52d5ea"
DEFAULT_BUBBLE_COLOUR = "#173b49"
DEFAULT_PICTURE_ZOOM = 100
DEFAULT_PICTURE_HUE = 0
# The page resizes a chosen image before saving it. Keep a second boundary
# here because the board is durable user configuration, not an image store.
# At the limit, all 24 agents still fit below the server's request-body cap.
LONGEST_PROFILE_PICTURE = 400_000


def _agent_colour(said: Any, otherwise: str) -> str:
    value = str(said or "").strip()
    return value.lower() if AN_AGENT_COLOUR.fullmatch(value) else otherwise


def _agent_icon(said: Any) -> str:
    value = str(said or "").strip().lower()
    return value if value in AGENT_ICONS else "robot"


def _agent_picture(said: Any) -> str:
    value = str(said or "").strip()
    if len(value) > LONGEST_PROFILE_PICTURE:
        return ""
    return value if AN_AGENT_PICTURE.fullmatch(value) else ""


def _picture_number(said: Any, otherwise: int, least: int, most: int) -> int:
    if isinstance(said, bool) or not isinstance(said, (int, float)):
        return otherwise
    return max(least, min(most, int(said)))


class SwarmError(HarnessError):
    """Something wrong with the board, or with what somebody asked of it."""


@dataclass
class Agent:
    """One assistant on the board, with a job and a place to sit."""

    id: str
    name: str
    who: str = ""
    job: str = ""
    at: dict[str, int] = field(default_factory=lambda: {"x": 40, "y": 40})
    colour: str = DEFAULT_AGENT_COLOUR
    icon: str = "robot"
    bubble_colour: str = DEFAULT_BUBBLE_COLOUR
    profile_picture: str = ""
    picture_zoom: int = DEFAULT_PICTURE_ZOOM
    picture_hue: int = DEFAULT_PICTURE_HUE
    # Direct chats in early Nexus versions were keyed by the visible name.
    # Keep that original key when the display name changes so history cannot
    # become an orphan merely because an agent was renamed.
    filed_as_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "who": self.who,
            "job": self.job,
            "at": dict(self.at),
            "colour": self.colour,
            "icon": self.icon,
            "bubble_colour": self.bubble_colour,
            "profile_picture": self.profile_picture,
            "picture_zoom": self.picture_zoom,
            "picture_hue": self.picture_hue,
            "filed_as": self.filed_as_name or filed_as(self.name),
        }


@dataclass
class OneProject:
    """One project folder on the board, and the jobs wanted in it."""

    id: str
    path: str
    tasks: list[str] = field(default_factory=list)
    at: dict[str, int] = field(default_factory=lambda: {"x": 40, "y": 320})
    # Approval is deliberately attached to one exact project box. The digest
    # also binds the canonical/declared path, discovered argv, and discovery
    # files; swarm_work recomputes it immediately before any project code can
    # run. An empty value is the fail-closed/default state for older boards.
    approved_test_command_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        from . import projects as projects_lab

        where = Path(self.path)
        return {
            "id": self.id,
            "path": self.path,
            "name": projects_lab.name_of(where),
            "is_there": where.is_dir(),
            "tasks": list(self.tasks),
            "at": dict(self.at),
            "approved_test_command_digest": self.approved_test_command_digest,
        }


@dataclass
class Board:
    # Agent and project ids are short, human-editable canvas identifiers. They
    # are not sufficient chat ownership: an imported or separately saved board
    # can legitimately contain the same ids. This opaque, server-owned id
    # scopes every durable pair chat to one board lineage.
    workspace_id: str = field(default_factory=_new_workspace_id)
    # How many times this board has been written. Sent out with it and sent
    # back with a save, so a window that has been looking at an old board is
    # told rather than quietly writing over somebody else's change.
    version: int = 0
    # How many agents, and how many projects, have ever been put on this board.
    # Each only ever goes up, and the next box is named from it, so a name is
    # never handed out twice. One count per kind rather than one for the board,
    # so a board written down by hand still gets agent-1 and project-1 and not
    # agent-1 and project-3.
    made_agents: int = 0
    made_projects: int = 0
    agents: list[Agent] = field(default_factory=list)
    projects: list[OneProject] = field(default_factory=list)
    # Which agent works on which project.
    works_on: list[dict[str, str]] = field(default_factory=list)
    # Which pairs may pass notes to each other. A pair that is not here is a
    # pair that may not: two agents that should not know about each other are
    # two agents that will not hear from each other.
    talks_to: list[dict[str, str]] = field(default_factory=list)
    # Which named saved board this live board came from. The live board is the
    # copy that keeps every edit, so this identity belongs beside those edits
    # rather than in a separate preference that can drift away from them.
    active_saved_board: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "binding_schema_version": BOARD_BINDING_SCHEMA_VERSION,
            "workspace_id": self.workspace_id,
            "version": self.version,
            "made_agents": self.made_agents,
            "made_projects": self.made_projects,
            "agents": [one.to_dict() for one in self.agents],
            "projects": [one.to_dict() for one in self.projects],
            "works_on": [dict(one) for one in self.works_on],
            "talks_to": [dict(one) for one in self.talks_to],
            "active_saved_board": self.active_saved_board,
        }


def filed_as_on_the_board(name: str) -> str:
    """Where a swarm run's side of the conversation with one agent is kept.

    Apart from the person's own chat with that agent, on purpose. A run asked
    each agent under the plain name, which is the very file the person's
    conversation lives in - so a run left somebody's chat with The reviewer full
    of machine-to-machine talk they never said a word of, and answers that were
    never to them.
    """

    plain = " ".join(str(name or "").split()) or "an-agent"
    suffix = " on the board"
    if len(plain) + len(suffix) <= LONGEST_NAME:
        return plain + suffix
    # Preserve the namespace marker and the identity-bearing tail. Merely
    # appending the suffix and then passing through filed_as() removed the
    # suffix for a 60-character agent name, merging board-run traffic into the
    # person's private chat. The digest also prevents two long names with the
    # same visible prefix from sharing a board conversation.
    marker = "~" + hashlib.sha256(plain.encode("utf-8")).hexdigest()[:12]
    prefix = plain[:LONGEST_NAME - len(marker) - len(suffix)]
    return prefix + marker + suffix


def filed_as(name: str) -> str:
    """What one agent's conversation is filed under.

    Its own name, tidied. Two agents both using Claude would otherwise share
    one conversation and each would read the other's half of it.
    """

    said = " ".join(str(name or "").split())
    return said[:LONGEST_NAME] or "an-agent"


# --------------------------------------------------------------------------
# Reading and writing it. Kept beside the list of projects, because a board
# spans projects and belongs to none of them.
# --------------------------------------------------------------------------


def where_it_lives() -> Path:
    from .config import user_config_path

    return user_config_path().parent / "swarm.json"


def _how_many_ever(kind: str, said: Any, held: Any, least: int = 0) -> int:
    """How many boxes of one kind have ever been on this board.

    Taken from what the board says, never less than the highest number already
    in use - an older board says nothing about it, and its boxes still have to
    keep the names they have - and never less than what the board on disk says,
    which is what `least` is for.
    """

    most = max(_how_many_times(said), least)
    for one in held if isinstance(held, list) else []:
        if not isinstance(one, dict):
            continue
        found = re.fullmatch(rf"{kind}-(\d{{1,9}})", str(one.get("id") or ""))
        if found:
            most = max(most, int(found.group(1)))
    return most


def _the_next_name(kind: str, made: int) -> str:
    """The name for one new box. Never one that has been used before.

    Handed out by taking the lowest number nothing was using, removing an agent
    and adding another gave the new one the name the old one had. The panel
    holds which agent it is waiting on by that name, so an answer already on its
    way back landed in the new agent's chat - one agent's words in another
    agent's box.
    """

    return f"{kind}-{made}"


def _somewhere_free(which: int, down: int) -> tuple[int, int]:
    """Where a box goes when nothing says where it should sit.

    Along a row and then down to the next, rather than every one of them at
    forty and forty. Given one spot, a board written anywhere but the panel -
    by hand, or by a check - stacked every box exactly on top of the last, and
    what you saw was one box with the others hidden underneath it.
    """

    return 40 + (which % 4) * 210, down + (which // 4) * 110


def _a_place(said: Any, x: int, y: int) -> dict[str, int]:
    if not isinstance(said, dict):
        return {"x": x, "y": y}
    return {
        "x": _a_number(said.get("x"), x),
        "y": _a_number(said.get("y"), y),
    }


def _a_number(said: Any, instead: int) -> int:
    if isinstance(said, bool) or not isinstance(said, (int, float)):
        return instead
    # Kept on the board. A box dragged to minus four thousand is a box nobody
    # can find again.
    return max(0, min(4000, int(said)))


def _is_a_count(said: Any) -> bool:
    """A whole number of times, and nothing that merely looks like one."""

    return isinstance(said, int) and not isinstance(said, bool) and said >= 0


def _how_many_times(said: Any) -> int:
    """How many times a board has been written.

    Not read with the helper that keeps a box on the board: that one holds
    numbers between nothing and four thousand, which is right for a place to
    sit and wrong here. Capped at four thousand, the count would stop rising
    on the four thousandth save and the check that two windows are looking at
    the same board would quietly stop working, with nothing to show for it.
    """

    if isinstance(said, bool) or not isinstance(said, (int, float)):
        return 0
    return max(0, int(said))


def _some_words(said: Any, longest: int) -> str:
    return " ".join(str(said or "").split())[:longest]


def _bounded_words(said: Any, longest: int, what: str) -> str:
    """Normalise a short identifier without silently cutting it."""

    words = " ".join(str(said or "").split())
    if len(words) > longest:
        raise SwarmError(
            f"{what} is {len(words):,} characters; the disclosed limit is "
            f"{longest:,}. Nexus did not truncate it."
        )
    return words


def _bounded_instruction(said: Any, longest: int, what: str) -> str:
    """Keep every accepted instruction character exactly as the user supplied it."""

    text = "" if said is None else str(said)
    if len(text) > longest:
        raise SwarmError(
            f"{what} is {len(text):,} characters; the disclosed limit is "
            f"{longest:,}. Nexus did not truncate it."
        )
    return text if text.strip() else ""


def read_it(said: Any, made_agents: int = 0, made_projects: int = 0) -> Board:
    """A board, from whatever was written down or sent.

    `made_agents` and `made_projects` are the counts the board on disk holds.
    They are a floor, never a ceiling: a caller that replaces the whole roster
    with fresh names sends no counts at all, and working them out from what was
    sent alone would hand the next box a name a removed one used to have. An id
    written down on purpose is still honoured - saying which one you mean is a
    deliberate act, unlike leaving it out.
    """

    if not isinstance(said, dict):
        raise SwarmError("A board is written as an object")
    schema_version = said.get("schema_version")
    if schema_version is not None and (
        isinstance(schema_version, bool) or schema_version != 1
    ):
        qualifier = (
            "a newer" if isinstance(schema_version, int) and schema_version > 1
            else "an unsupported"
        )
        raise SwarmError(
            f"This board uses {qualifier} schema version ({schema_version!r}). "
            "Nexus did not reinterpret or rewrite it."
        )
    binding_schema_version = said.get("binding_schema_version")
    if binding_schema_version is not None and (
        isinstance(binding_schema_version, bool)
        or binding_schema_version != BOARD_BINDING_SCHEMA_VERSION
    ):
        qualifier = (
            "a newer"
            if isinstance(binding_schema_version, int)
            and binding_schema_version > BOARD_BINDING_SCHEMA_VERSION
            else "an unsupported"
        )
        raise SwarmError(
            f"This board uses {qualifier} chat-binding schema version "
            f"({binding_schema_version!r}). Nexus did not reinterpret or rewrite it."
        )
    raw_agents_value = said.get("agents", [])
    raw_projects_value = said.get("projects", [])
    if not isinstance(raw_agents_value, list) or not all(
        isinstance(one, dict) for one in raw_agents_value
    ):
        raise SwarmError("A board's agents must be a list of objects. Nothing was dropped.")
    if not isinstance(raw_projects_value, list) or not all(
        isinstance(one, dict) for one in raw_projects_value
    ):
        raise SwarmError("A board's projects must be a list of objects. Nothing was dropped.")
    raw_agents = list(raw_agents_value)
    raw_projects = list(raw_projects_value)
    if len(raw_agents) > MOST_AGENTS:
        raise SwarmError(
            f"This board has {len(raw_agents)} agents; the visible limit is "
            f"{MOST_AGENTS}. Nexus did not drop any agents."
        )
    if len(raw_projects) > MOST_PROJECTS:
        raise SwarmError(
            f"This board has {len(raw_projects)} projects; the visible limit is "
            f"{MOST_PROJECTS}. Nexus did not drop any projects."
        )
    agents: list[Agent] = []
    seen: set[str] = set()
    made_agents = _how_many_ever(
        "agent", said.get("made_agents"), said.get("agents"), made_agents)
    made_projects = _how_many_ever(
        "project", said.get("made_projects"), said.get("projects"), made_projects)
    for one in raw_agents:
        name = _bounded_words(one.get("name"), LONGEST_NAME, "An agent name")
        if not name:
            raise SwarmError("Every agent needs a name")
        if not A_NAME.match(name):
            raise SwarmError(
                f"{name!r} is not a name an agent can have. Names hold letters, "
                "numbers, spaces, dots, dashes and underscores."
            )
        held_filed_as = filed_as(
            _bounded_words(
                one.get("filed_as"), LONGEST_NAME, "A saved chat identity"
            ) or name
        )
        if held_filed_as.casefold() in {
            (held.filed_as_name or filed_as(held.name)).casefold()
            for held in agents
        }:
            raise SwarmError(
                f"There is already an agent using the saved chat identity for {name}. "
                "Two agents cannot share one conversation."
            )
        held_id = str(one.get("id") or "").strip()
        if not held_id or held_id in seen:
            made_agents += 1
            held_id = _the_next_name("agent", made_agents)
        seen.add(held_id)
        across, down = _somewhere_free(len(agents), 40)
        agents.append(Agent(
            id=held_id,
            name=name,
            who=_bounded_words(one.get("who"), 64, "An assistant route"),
            job=_bounded_instruction(
                one.get("job"), LONGEST_JOB, "An agent role description"
            ),
            at=_a_place(one.get("at"), across, down),
            colour=_agent_colour(one.get("colour"), DEFAULT_AGENT_COLOUR),
            icon=_agent_icon(one.get("icon")),
            bubble_colour=_agent_colour(
                one.get("bubble_colour"), DEFAULT_BUBBLE_COLOUR
            ),
            profile_picture=_agent_picture(one.get("profile_picture")),
            picture_zoom=_picture_number(
                one.get("picture_zoom"), DEFAULT_PICTURE_ZOOM, 100, 300
            ),
            picture_hue=_picture_number(
                one.get("picture_hue"), DEFAULT_PICTURE_HUE, 0, 360
            ),
            filed_as_name=held_filed_as,
        ))
    projects: list[OneProject] = []
    for one in raw_projects:
        path = str(one.get("path") or "").strip()
        if not path:
            raise SwarmError("Every project on the board needs a folder")
        where = Path(path)
        if str(where) in {held.path for held in projects}:
            raise SwarmError(f"{path} is on the board twice")
        held_id = str(one.get("id") or "").strip()
        if not held_id or held_id in seen:
            made_projects += 1
            held_id = _the_next_name("project", made_projects)
        seen.add(held_id)
        # Jobs are lines of text, not objects. Read with the helper for lists of
        # objects, every one of them was quietly dropped and the board said the
        # project had nothing to do in it.
        written = one.get("tasks", [])
        if not isinstance(written, list) or not all(
            isinstance(task, str) for task in written
        ):
            raise SwarmError(
                f"Every job for {path} must be text in a list. Nothing was dropped."
            )
        if len(written) > MOST_TASKS:
            raise SwarmError(
                f"{path} has {len(written)} jobs; the visible limit is "
                f"{MOST_TASKS}. Nexus did not drop any jobs."
            )
        if any(not task.strip() for task in written):
            raise SwarmError(
                f"Every job for {path} must contain non-whitespace text. "
                "Nothing was dropped."
            )
        tasks = [
            _bounded_instruction(
                task, LONGEST_TASK, f"A job for {path}"
            )
            for task in written
        ]
        across, down = _somewhere_free(len(projects), 320)
        projects.append(OneProject(
            id=held_id,
            path=str(where),
            tasks=tasks,
            at=_a_place(one.get("at"), across, down),
            approved_test_command_digest=(
                str(one.get("approved_test_command_digest") or "").lower()
                if re.fullmatch(
                    r"[0-9a-fA-F]{64}",
                    str(one.get("approved_test_command_digest") or ""),
                )
                else ""
            ),
        ))
    known_agents = {one.id for one in agents}
    known_projects = {one.id for one in projects}
    works_on = []
    for one in _a_list(said.get("works_on")):
        agent = str(one.get("agent") or "")
        project = str(one.get("project") or "")
        if agent not in known_agents or project not in known_projects:
            # A line to a box somebody has since removed. Dropped rather than
            # refused: the board somebody can see is the truth, and a line to
            # nothing is not something they can point at to fix.
            continue
        if {"agent": agent, "project": project} not in works_on:
            works_on.append({"agent": agent, "project": project})
    talks_to = []
    for one in _a_list(said.get("talks_to")):
        first = str(one.get("one") or "")
        other = str(one.get("other") or "")
        if first == other or first not in known_agents or other not in known_agents:
            continue
        # Held one way round only, smallest first, so "A talks to B" and "B
        # talks to A" are the same line and cannot disagree with each other.
        pair = {"one": min(first, other), "other": max(first, other)}
        if pair not in talks_to:
            talks_to.append(pair)
    active_saved_board = _some_words(said.get("active_saved_board"), 48)
    if not A_NAME.fullmatch(active_saved_board):
        active_saved_board = ""
    workspace_id = str(said.get("workspace_id") or "").strip().lower()
    if workspace_id and not _WORKSPACE_ID.fullmatch(workspace_id):
        # Absence is the one compatibility bridge for a pre-identity board.
        # A non-empty malformed identity is corruption, not legacy data: making
        # it legacy would orphan its scoped chats and could let it claim an
        # unrelated unscoped registry.
        raise SwarmError(
            "This board has an invalid workspace identity. Nexus kept it unchanged "
            "and did not claim or retarget any saved chats."
        )
    return Board(
        workspace_id=workspace_id,
        version=_how_many_times(said.get("version")),
        made_agents=made_agents,
        made_projects=made_projects,
        agents=agents,
        projects=projects,
        works_on=works_on,
        talks_to=talks_to,
        active_saved_board=active_saved_board,
    )


def _a_list(said: Any) -> list[dict[str, Any]]:
    """The objects in a list, and nothing else that happens to be in it."""

    return [one for one in said if isinstance(one, dict)] if isinstance(said, list) else []


_recovering_board_qa = threading.local()
_board_qa_access_state = threading.local()
_board_qa_request = threading.local()
_recovered_board_qa_authorities: set[str] = set()
_recovered_board_qa_authorities_lock = threading.Lock()


def _board_qa_authority() -> str:
    return os.path.normcase(os.path.abspath(str(where_it_lives())))


def _set_board_qa_request_capability(token: str) -> None:
    """Set the capability validated for this one server request only."""

    _board_qa_request.capability = token if isinstance(token, str) else ""


@contextmanager
def _using_board_qa_request_capability(token: str):
    """Scope a real QA transaction capability to only this worker thread."""

    previous = str(getattr(_board_qa_request, "capability", "") or "")
    _set_board_qa_request_capability(token)
    try:
        yield
    finally:
        _set_board_qa_request_capability(previous)


def _recover_abandoned_board_qa() -> None:
    """Run QA's own recovery before ordinary board reads, without an import loop."""

    if bool(getattr(_recovering_board_qa, "active", False)):
        return
    authority = _board_qa_authority()
    with _recovered_board_qa_authorities_lock:
        if authority in _recovered_board_qa_authorities:
            return
        _recovering_board_qa.active = True
        try:
            from . import qa as qa_lab

            recovered = qa_lab.recover_abandoned_board_transactions()
            if recovered is False:
                raise SwarmError(
                    "A board check is in progress, so Nexus will not show or change its "
                    "temporary or displaced board as your real one. Retry when that check finishes."
                )
            _recovered_board_qa_authorities.add(authority)
        finally:
            _recovering_board_qa.active = False


@contextmanager
def _board_qa_access():
    """Hold the nonblocking board-QA isolation lock for one complete operation."""

    if bool(getattr(_board_qa_access_state, "active", False)):
        yield
        return
    from . import qa as qa_lab

    capability = str(getattr(_board_qa_request, "capability", "") or "")
    if capability:
        if not qa_lab.board_qa_capability_is_active(capability, where_it_lives()):
            raise SwarmError(
                "The board-check transaction capability is no longer valid. "
                "No board data was read or changed."
            )
        _board_qa_access_state.active = True
        try:
            yield
        finally:
            _board_qa_access_state.active = False
        return

    _recover_abandoned_board_qa()

    try:
        with qa_lab._board_preservation_file_lock(  # noqa: SLF001 - shared board authority
            where_it_lives(), timeout_seconds=0.0,
        ):
            _board_qa_access_state.active = True
            try:
                yield
            finally:
                _board_qa_access_state.active = False
    except qa_lab.BoardPreservationBusy as exc:
        raise SwarmError(
            "A board check is in progress, so Nexus will not show or change its "
            "temporary or displaced board as your real one. Retry when that check finishes."
        ) from exc


def _requires_board_qa_access(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        with _board_qa_access():
            return function(*args, **kwargs)
    return guarded


@_requires_board_qa_access
def load() -> Board:
    where = where_it_lives()
    if not where.exists():
        return Board()
    try:
        said = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SwarmError(
            f"The active board at {where} cannot be read. Nexus preserved it and "
            "did not pretend your agents, projects, or goals were gone."
        ) from exc
    try:
        board = read_it(said)
        if not board.workspace_id:
            # One deterministic bridge lets the live board which predates
            # workspace identities claim its existing project-local chat
            # registry exactly once. It is persisted on the next normal save.
            canonical = os.path.normcase(str(where.resolve(strict=False)))
            marked = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
            board.workspace_id = f"workspace-legacy-{marked}"
        return board
    except SwarmError as exc:
        raise SwarmError(
            f"The active board at {where} is invalid. Nexus preserved it and did "
            f"not replace it with an empty board: {exc}"
        ) from exc


@_requires_board_qa_access
def save(
    said: Any,
    config: Any,
    *,
    allow_command_approval_changes: bool = False,
) -> Board:
    """Persist a board through the process-wide board authority.

    The authority lives outside every project and is shared with durable board
    runs.  Keeping this gate here (rather than only in the HTTP handler) means
    CLI and named-board callers cannot accidentally mutate the global board
    while another process is executing a snapshot of it.
    """

    from .swarm_runs import global_board_mutation

    with global_board_mutation(config):
        return _save_while_board_authority_is_held(
            said,
            config,
            allow_command_approval_changes=allow_command_approval_changes,
        )


def _save_while_board_authority_is_held(
    said: Any,
    config: Any,
    *,
    allow_command_approval_changes: bool = False,
    workspace_id_override: str | None = None,
) -> Board:
    """Write the whole board down, if it is still the board that was read.

    A whole board at a time, because it is one small picture and saving it
    whole means a line can never point at a box that half a save had already
    taken away.

    That has one trap, which is what the version is for. Two windows open on
    the same board would each send a whole board built from what it read
    before the other wrote, and the second would quietly throw the first one's
    change away with no sign that anything had happened. So a board that says
    which version it was built from has to agree with the one on disk. A board
    that says nothing - a test, or the very first write - is taken as is.

    Reading the version and writing the new board are two steps, so the caller
    has to hold something around them. The panel's server does: it takes the
    board lock around this and around setting the board going, so no save can
    slip between a run reading the board and the run owning it. Two separate
    programs writing the same board at the same instant would still be last one
    wins - the write itself is atomic, so nothing is ever half written, but the
    earlier change would be gone. That is worth knowing and not worth a lock
    file for a board only ever changed by hand.
    """

    from .safety import put_this_file_in_place

    # Anybody adding a second caller: read the paragraph above about the lock.
    # Reading the version and writing the board are two steps, and without
    # something held around both of them the check below proves nothing.
    now = load()
    board = read_it(said, now.made_agents, now.made_projects)
    # Workspace identity is server-owned. A stale panel may omit it and a
    # hand-edited request may try to substitute it; neither may retarget saved
    # conversations. Opening a locally validated named board is the one
    # explicit internal replacement path.
    if workspace_id_override is None:
        board.workspace_id = now.workspace_id or _new_workspace_id()
    elif _WORKSPACE_ID.fullmatch(str(workspace_id_override or "")):
        board.workspace_id = str(workspace_id_override)
    else:
        raise SwarmError("That saved board has an invalid workspace identity.")
    if not allow_command_approval_changes:
        # Execution approval is not ordinary board layout. A caller that can
        # move boxes or save task text must not be able to smuggle a digest in
        # through the JSON document and thereby cause project code to run.
        # Preserve only approval already held by the same local project box at
        # the exact same path. Only the dedicated, visibly confirmed approval
        # route and a local named snapshot created after such approval may
        # change it.
        approvals = {
            (one.id, one.path): one.approved_test_command_digest
            for one in now.projects
        }
        for project in board.projects:
            project.approved_test_command_digest = approvals.get(
                (project.id, project.path), ""
            )
    # An older panel knows nothing about the named-board identity. Treat an
    # omitted field as "leave it alone", not "forget it", so one stale window
    # cannot make startup lose the board somebody explicitly opened elsewhere.
    if isinstance(said, dict) and "active_saved_board" not in said:
        board.active_saved_board = now.active_saved_board
    asked = said.get("version") if isinstance(said, dict) else None
    if asked is not None and not _is_a_count(asked):
        # Refused rather than waved through. Taken as "said nothing", a version
        # written as 3.0 or as "3" would slip past the check below and quietly
        # put the whole point of it back the way it was.
        raise SwarmError(
            f"{asked!r} is not a version. A board says which version it was "
            "built from as a whole number, or says nothing at all."
        )
    if asked is not None and asked != now.version:
        raise SwarmError(
            "Somebody changed the board in another window while this one was "
            "open. Press Look again to see how it stands now, then make your "
            "change on top of theirs."
        )
    board.version = now.version + 1
    from . import swarm_chats as swarm_chats_lab

    swarm_chats_lab.fence_for_board_change(
        config, now.to_dict(), board.to_dict()
    )
    where = where_it_lives()
    where.parent.mkdir(parents=True, exist_ok=True)
    put_this_file_in_place(where, json.dumps(board.to_dict(), indent=2) + "\n")
    return board


# --------------------------------------------------------------------------
# What the panel is told.
# --------------------------------------------------------------------------


# The three things the board wants out of a conversation, kept against when the
# file was last written. Drawing the board reads every agent's chat, and the
# board is drawn every time somebody looks at it and every time anything on it
# moves - dragging a box a few pixels saves the board. Chats change when
# somebody types, which almost never lines up with any of that, so nearly all
# of those reads would come back with what they came back with last time.
#
# Only the three small values are kept, never the conversation itself, and the
# oldest are dropped once there are more than a board could hold, so this cannot
# grow into somewhere conversations quietly pile up in memory.
_MOST_KEPT_ABOUT_CHATS = 200
_what_was_said: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
# One request at a time in here. Every way into the board today happens to take
# a lock further up, so nothing needs this yet - which is exactly the problem
# with leaving it out. That care is held somewhere else, by callers who have no
# reason to know this depends on it, and the first one that arrives without it
# throws while the oldest entry is being dropped and takes the whole board down
# with it. It is one lock around three lines and it is nobody else's business.
_while_reading_chats = threading.Lock()
# How fresh is too fresh to trust. A file written twice inside the same tick of
# the clock, to the same length, looks unchanged - and it is the same length
# more often than sounds likely, because these are lines of chat. Typing at a
# keyboard never comes close; a run driving several agents does, measured at
# about one time in six when two writes land within a millisecond. So a file
# written a moment ago is read rather than remembered.
_TOO_FRESH_TO_TRUST = 2_000_000_000  # two seconds, in the same units as the clock


def _how_much_was_said_to(config, who: str, filed: str) -> dict[str, Any]:
    """How much has been said in one agent's chat, and the last line of it."""

    from . import chat as chat_lab
    from . import pages as pages_lab

    nothing = {"said": 0, "last_said": "", "last_said_at": ""}
    # An agent with nobody to ask has no conversation of its own. A name outlives
    # what it was given to, so a chat filed under this name may well be there -
    # from before somebody unset the assistant, or from an agent that had the
    # name before - and it belongs to none of this one's business.
    if not who:
        return nothing
    where = chat_lab.where_it_is_kept(config, who, filed)
    try:
        stamp = where.stat()
        when = (stamp.st_mtime_ns, stamp.st_size)
    except OSError:
        return nothing
    key = f"{who}\n{where}"
    # Written a moment ago, so what is remembered about it cannot be trusted:
    # two writes inside one tick of the clock, to the same length, are the same
    # file as far as this can tell.
    settled = time.time_ns() - stamp.st_mtime_ns > _TOO_FRESH_TO_TRUST
    with _while_reading_chats:
        known = _what_was_said.get(key)
        if settled and known is not None and known[0] == when:
            return dict(known[1])
    said = chat_lab.read_it(config, who, filed)
    held = {
        "said": len(said),
        "last_said": said[-1].text[:120] if said else "",
        "last_said_at": said[-1].at if said else "",
    }
    with _while_reading_chats:
        if len(_what_was_said) >= _MOST_KEPT_ABOUT_CHATS:
            _what_was_said.pop(next(iter(_what_was_said)), None)
        _what_was_said[key] = (when, dict(held))
    return held


def discover_who_can_be_used(config) -> list[dict[str, Any]]:
    """Probe provider routes without reading or locking the durable board.

    Installed command-line providers can take many seconds to answer a version
    or readiness probe. Keeping that machine discovery separate from board
    hydration lets the server perform it outside the topology mutation lock, so
    a visible board remains editable while provider status is refreshed.
    """

    from . import chat as chat_lab

    can_talk = [
        one for one in chat_lab.who_can_talk(config) if one.get("route")
    ]
    for one in can_talk:
        one["can_be_connected"] = (
            "" if one.get("ready")
            else _which_one_to_connect(one.get("route", ""), one)
        )
    return can_talk


def how_it_stands(config, *, known_routes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The board, and everything the panel needs to draw and judge it.

    A normal read probes provider availability. A save may pass the routes from
    the most recent read so acknowledging a board write never waits on every
    installed CLI merely to rediscover status the panel already has.
    """

    from . import chat as chat_lab
    from . import pages as pages_lab
    from . import projects as projects_lab
    from . import qa as qa_lab

    board = load()
    # The one route with no name of its own - "the one this project uses" - is
    # left out here. A board spans projects, so "this project" means nothing on
    # it; and its name being empty is the same as an agent having chosen
    # nobody, so an agent nobody had set up read as ready and pointed at
    # whatever that project happened to use. Somebody with no named routes is
    # sent to Your team, where one press makes them.
    probing = known_routes is None
    can_talk = (
        discover_who_can_be_used(config)
        if probing else [dict(one) for one in known_routes or []]
    )
    # Which of them one press would connect: on this machine, and nothing
    # pointing at it yet. Anything else must not offer a button, because a
    # button that fails is worse than no button.
    for one in can_talk:
        if "can_be_connected" not in one:
            one["can_be_connected"] = (
                "" if one.get("ready") else _which_one_to_connect(one.get("route", ""), one))
    ready = {one["route"]: one for one in can_talk}
    agents = []
    for one in board.agents:
        held = one.to_dict()
        known = ready.get(one.who) if one.who else None
        held["ready"] = bool(known and known.get("ready"))
        held["why_not"] = (
            "" if held["ready"]
            else (known or {}).get("why_not")
            or (
                "Nothing is chosen for this one yet. Pick which assistant it "
                "uses in its settings." if not one.who
                else f"{one.who} is not on this machine."
            )
        )
        held["how_to_fix_it"] = (known or {}).get("how_to_fix_it", "")
        held["assistant_kind"] = (known or {}).get("kind", "")
        held["can_sign_in"] = bool((known or {}).get("can_sign_in"))
        held["chat_destination"] = chat_lab.chat_destination(
            config, one.who, one.filed_as_name or filed_as(one.name)
        )
        # Which assistant this one would need set up, when that is all that is
        # missing. Somebody had Claude installed and signed in, an agent set to
        # use it, and the board still said not ready - with nothing on screen to
        # press, because the only way to point the settings at it was a terminal.
        held["can_be_connected"] = (
            _which_one_to_connect(one.who, known) if probing
            else str((known or {}).get("can_be_connected") or "")
        )
        # What happened the last time this route was asked anything. Not the
        # same as not being ready - this one is still tried - so that somebody
        # knows before they type instead of after.
        held["trouble_last_time"] = (known or {}).get("trouble_last_time", "")
        # How much has been said to this one, so the list of chats can be drawn
        # without asking after every conversation one at a time. It is read
        # rather than counted from anything kept in memory: a chat that was had
        # yesterday is still a chat.
        held.update(_how_much_was_said_to(
            config, one.who, one.filed_as_name or filed_as(one.name)
        ))
        agents.append(held)
    kept, kept_problems = kept_board_inventory()
    return {
        "board": dict(board.to_dict(), agents=agents),
        # The boards somebody saved under a name. Sent with the board itself, so
        # opening the panel shows what is kept without pressing anything - and
        # so anything that changes the list has the new one in its answer.
        "kept": kept,
        "kept_problems": kept_problems,
        "board_recovery_notices": qa_lab.retained_board_recovery_notices(),
        "who_can_be_used": can_talk,
        "projects_on_this_machine": [
            one.to_dict() for one in projects_lab.every_one(config.project_root)
        ],
        "most": {
            "agents": MOST_AGENTS,
            "projects": MOST_PROJECTS,
            "tasks": MOST_TASKS,
        },
    }


def _the_lines_in(board: Board | dict[str, Any], which: str) -> list[dict[str, str]]:
    """The lines of one kind, whether the board is a Board or plain data.

    The board is a Board while it is being read and written, and plain data
    everywhere the panel or a run touches it. Both had their own copy of the
    rules below, which is the pair that quietly disagrees the day one of them
    is fixed.
    """

    if isinstance(board, dict):
        held = board.get(which)
        return [one for one in held if isinstance(one, dict)] if isinstance(held, list) else []
    return list(getattr(board, which))


def may_they_talk(board: Board | dict[str, Any], one: str, other: str) -> bool:
    """May these two pass notes to each other?

    No unless somebody drew the line. Two agents that should not know about
    each other are two agents that will not hear from each other, and that is
    the safer way round to be wrong.
    """

    pair = {"one": min(one, other), "other": max(one, other)}
    return one != other and pair in _the_lines_in(board, "talks_to")


def the_agent(board: Board, agent_id: str) -> Agent:
    """One agent by its name on the board, or a plain no.

    Everything that talks to an agent goes through here, so there is one place
    that decides what happens when somebody asks for one that is not there any
    more - a panel left open while the board was changed in another window.
    """

    for one in board.agents:
        if one.id == agent_id:
            return one
    raise SwarmError(
        "That agent is not on the board any more. Somebody may have removed it "
        "in another window. Refresh the board to see how it stands now."
    )


def who_works_on(board: Board | dict[str, Any], project_id: str) -> list[Any]:
    """The agents on one project, in the order they sit on the board."""

    on_it = {
        one["agent"] for one in _the_lines_in(board, "works_on")
        if one.get("project") == project_id
    }
    agents = board["agents"] if isinstance(board, dict) else board.agents
    return [
        one for one in agents
        if (one["id"] if isinstance(one, dict) else one.id) in on_it
    ]


def _which_one_to_connect(who: str, known: dict[str, Any] | None) -> str:
    """The assistant a not-ready agent needs, when one press would fix it.

    Only when the tool really is on this machine and nothing points at it. A
    button offering to connect something that is not installed is a button that
    fails, and a button that fails is worse than no button.
    """

    from . import seats as seats_lab

    if not who or (known or {}).get("ready") or (known or {}).get("setup_blocked"):
        return ""
    for kind, route in seats_lab.ROUTE_NAMES.items():
        if route != who:
            continue
        from .providers.subscription_cli import available

        return kind if available(kind) else ""
    return ""


def what_is_not_ready(config, said: dict[str, Any] | None = None) -> list[str]:
    """Everything standing between this board and it being any use.

    Said as plain sentences, in the order somebody would fix them.

    A caller that has just asked how it stands can hand that back rather than
    have the whole board read off the disk a second time to say the same thing.
    """

    said = said if said is not None else how_it_stands(config)
    problems: list[str] = []
    board = said["board"]
    if not any(one["ready"] for one in said["who_can_be_used"]):
        problems.append(
            "No assistant on this machine is set up to be used by name. Open "
            "Your team and press Set them up, then come back."
        )
    on_it = {
        (one["agent"], one["project"]) for one in board["works_on"]
    }
    if not board["agents"]:
        problems.append("There are no agents yet. Add one to get started.")
    for one in board["agents"]:
        if not one["ready"]:
            problems.append(f"{one['name']}: {one['why_not']}")
    if not board["projects"]:
        problems.append("No project folders are on the board yet.")
    for one in board["projects"]:
        if not one["is_there"]:
            problems.append(f"{one['path']} is not a folder on this machine any more.")
        elif not any(project == one["id"] for _agent, project in on_it):
            problems.append(
                f"Nobody works on {one['name']} yet. Draw a line from an agent to it."
            )
        elif not one["tasks"]:
            problems.append(
                f"{one['name']} has no jobs written down yet, so there is nothing "
                "for anybody to do there."
            )
    return problems


# --------------------------------------------------------------------------
# Setting them going.
#
# The board says who works on what and who may talk to whom. This is the part
# that acts on it: every agent is told about the project it is on and the jobs
# wanted there, and then - only where a line was drawn - it is shown what the
# agents it may talk to said, and asked again.
#
# Two rounds rather than one, because that is the whole point of having two
# assistants. The first round is each of them on their own, so nobody reads
# anybody else's answer before writing their own. The second is with the notes
# they are allowed to see. An agent that read the others first is not a second
# opinion; it is the first opinion agreeing with itself.
#
# One at a time, on purpose. These are command line tools signed in to
# somebody's subscription, and six at once is six ways to be turned away. It
# also means Stop can mean stop: it is looked at between turns, so the worst it
# costs is the turn already asked for.
# --------------------------------------------------------------------------

ON_ITS_OWN = "on its own"
AFTER_THE_OTHERS = "after reading the others"


@dataclass
class OneNote:
    """One agent's answer, shown to another because a line said they may talk.

    Kept so somebody watching can read what was passed. The second round showed
    it to the agent and nothing else; the one thing you want to look at when two
    assistants disagree is what each of them was actually given.
    """

    said_by: str
    said_by_name: str
    shown_to: str
    shown_to_name: str
    project: str
    where: str
    text: str
    at: str = ""
    # A durable delivery identity.  Older saved runs have neither, which is
    # fine: they remain readable as the plain notes they always were.
    message_id: str = ""
    thread_id: str = ""
    status: str = "acknowledged"
    attempts: int = 0
    original_characters: int = 0
    projection_source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "said_by": self.said_by,
            "said_by_name": self.said_by_name,
            "shown_to": self.shown_to,
            "shown_to_name": self.shown_to_name,
            "project": self.project,
            "where": self.where,
            "text": self.text,
            "at": self.at,
            "message_id": self.message_id,
            "thread_id": self.thread_id,
            "status": self.status,
            "attempts": self.attempts,
            "original_characters": self.original_characters or len(self.text),
            "projection_source": self.projection_source,
        }


@dataclass
class OneTurn:
    """One agent, one project, one round of it."""

    agent: str
    name: str
    project: str
    where: str
    round: str
    shared_goal: str = ""
    state: str = "waiting"     # waiting, asking, done, went wrong, not done
    said: str = ""
    why_not: str = ""
    milliseconds: int = 0
    # The agents whose answers this one was shown before it was asked. Empty on
    # the first round, because that is the point of the first round.
    shown: list[str] = field(default_factory=list)
    # Which part of the shared page this answer became, and how far the page had
    # got when this agent was asked. The second is how the page can tell it what
    # turned up while it was writing.
    part: int = 0
    after: int = 0

    def to_dict(self) -> dict[str, Any]:
        # What the agent said is not in here. It is kept where somebody would
        # go looking for it - that agent's own conversation - and sending a
        # copy of every answer to the panel as well, every second and a half
        # while a run is going, would be a lot of words nothing on screen ever
        # shows. How long it is said instead, which is the part the list uses.
        return {
            "agent": self.agent,
            "name": self.name,
            "project": self.project,
            "where": self.where,
            "round": self.round,
            "shared_goal": self.shared_goal,
            "state": self.state,
            "letters": len(self.said),
            "why_not": self.why_not,
            "milliseconds": self.milliseconds,
            "shown": list(self.shown),
            "part": self.part,
        }


@dataclass
class Doing:
    """One run of the board, and everything the panel shows about it."""

    going: bool = True
    stopped: bool = False
    note: str = ""
    turns: list[OneTurn] = field(default_factory=list)
    # Every answer that was passed from one agent to another, in the order it
    # was passed. Asked for on its own, because these are whole answers and
    # sending them with every "how is it going" would be a lot of words.
    notes: list[OneNote] = field(default_factory=list)
    # How many answers fell off the end of that list. Said rather than quietly
    # dropped: a list that has been cut short and does not say so reads like the
    # whole of it.
    dropped: int = 0
    run_id: str = ""
    request_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "request_id": self.request_id,
            "going": self.going,
            "stopped": self.stopped,
            "note": self.note,
            "notes": len(self.notes),
            "dropped": self.dropped,
            "turns": [one.to_dict() for one in self.turns],
            "done": len([one for one in self.turns if one.state == "done"]),
            "of": len(self.turns),
            "went_wrong": len([one for one in self.turns if one.state == "went wrong"]),
        }


def what_to_ask(agent: dict[str, Any], project: dict[str, Any]) -> str:
    """What one agent is told about one project, first time round."""

    jobs = "\n".join(f"- {one}" for one in project["tasks"])
    what_it_is_for = f"\nWhat you are here for: {agent['job']}" if agent["job"] else ""
    goal = shared_goal_id(project)
    return (
        f"SHARED GOAL {goal}\n"
        f"You are {agent['name']}, working on {project['name']}, "
        f"which is the folder at {project['path']}."
        f"{what_it_is_for}\n\n"
        f"The jobs wanted there:\n{jobs}\n\n"
        "Say how you would do them, shortest way first, and say what you would "
        "need to look at before starting. You cannot read the files or run "
        "anything from here, so say what you would do rather than doing it."
    )


def what_the_page_says(
    agent: dict[str, Any], project: dict[str, Any], page_text: str,
    messages: list[tuple[str, str]] | None = None,
) -> str:
    """What one agent is shown of the page, second time round.

    The whole page rather than a few notes somebody else picked out. An agent
    handed a tidied-up version of what the others said is being told what to
    think about it; an agent handed the page can read it for itself, in the
    order it was written, with names on it.
    """

    inbox = _messages_for_a_prompt(messages or [])
    return (
        f"SHARED GOAL {shared_goal_id(project)}\n"
        f"{page_text}\n\n"
        f"That is the page everybody working on {project['name']} shares. All of "
        "you read the same one and add to the bottom, so nothing anybody writes "
        "is written over and nobody has to wait for a turn to speak.\n\n"
        f"{inbox}"
        "Now say your own answer again, taking the page into account. Say "
        "plainly where you disagree with what is on it and why. Do not agree "
        "with something only because somebody else wrote it down."
    )


def what_the_others_said(
    agent: dict[str, Any], project: dict[str, Any], notes: list[tuple[str, str]]
) -> str:
    """What one agent is shown of the others, second time round.

    Kept for the board with no page to share - a project box whose folder is
    gone, where there is nowhere to write. The page is what a run uses.
    """

    said = _messages_for_a_prompt(notes)
    return (
        f"SHARED GOAL {shared_goal_id(project)}\n"
        f"Here is what the others working on {project['name']} said. You are "
        "being shown these because somebody said you two may talk.\n\n"
        f"{said}"
        "Now say your own answer again, taking theirs into account. Say plainly "
        "where you disagree with them and why. Do not agree with something only "
        "because somebody else said it."
    )


def shared_goal_id(project: dict[str, Any]) -> str:
    """Stable identity for one project's current list of jobs.

    A changed job list is a changed goal.  Messages for the previous goal stay
    in the audit file but are never replayed into the new one.
    """

    body = json.dumps({
        "project": str(project.get("id") or ""),
        "tasks": [str(one) for one in project.get("tasks", [])],
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _messages_for_a_prompt(notes: list[tuple[str, str]]) -> str:
    if not notes:
        return ""
    said = "\n\n".join(f"Message from {name}:\n{text}" for name, text in notes)
    return (
        "AGENT INBOX\n"
        "These messages were durably queued for you. They are acknowledged only "
        "after you answer this turn.\n\n"
        f"{said}\n\n"
    )


def _note_for_the_screen(
    text: str, *, run_id: str, message_id: str, sender: str, project: str
) -> tuple[str, int, str]:
    """Make a visibly recoverable audit projection; never imply it is canonical."""

    complete = str(text or "")
    source = (
        f"board run {run_id or '(pending)'}, sender {sender}, project {project}"
        + (f", durable message {message_id}" if message_id else "")
    )
    if len(complete) <= LONGEST_NOTE:
        return complete, len(complete), source
    marker = (
        f"\n\n[Display shortened from {len(complete):,} characters. "
        f"Full answer: {source}. The exact answer remains in the sender's "
        "saved board chat and, for a durable handoff, in the exact mailbox until "
        "acknowledgement. It is on the shared page only when that page write is "
        "recorded as successful.]"
    )
    keep = max(0, LONGEST_NOTE - len(marker))
    return complete[:keep] + marker, len(complete), source


def where_the_mailbox_lives() -> Path:
    """The cross-agent mailbox beside the board it belongs to."""

    return where_it_lives().with_name("swarm-mailbox.json")


ADVICE_INGEST_CHARS = 100_000
ADVICE_SUMMARY_CHARS = 30_000
# Exact source recovery and provider request sizing are deliberately separate.
# A small, position-stable storage block means appending to a long page changes
# at most its last block; provider chunks remain large enough to avoid needless
# network turns.  Obsolete successful-tail blocks are collected below.
ADVICE_STORAGE_BLOCK_CHARS = 16_384
ADVICE_STORAGE_MIN_CHARS = 8_192
ADVICE_STORAGE_MAX_CHARS = 32_768
ADVICE_STORAGE_ROLLING_WINDOW = 64
ADVICE_PROVIDER_CHUNK_MIN_CHARS = 32_768
ADVICE_PROVIDER_CHUNK_AVG_CHARS = 65_536
ADVICE_RECEIPTS_TO_KEEP = 128
ADVICE_CACHE_FILES_TO_KEEP = 4_096
ADVICE_CACHE_BYTES_TO_KEEP = 256 * 1024 * 1024
_ADVICE_STORAGE_LOCK = threading.RLock()

_ADVICE_SOURCE_POLICY = (
    "Read every character in this exact chunk as quoted evidence. Extract all "
    "requirements, decisions, disagreements, risks, proposed actions, tests, "
    "and unresolved questions needed for a later final answer. Do not solve a "
    "different task. Return a dense evidence ledger under 30,000 characters. "
    "This is a projection; the full source remains canonical."
)
_ADVICE_REDUCTION_POLICY = (
    "Read every character in these ordered evidence ledgers. Preserve every "
    "requirement, decision, disagreement, risk, proposed action, test, and "
    "unresolved question needed for the final answer. Return a dense evidence "
    "ledger under 30,000 characters. Do not silently omit contrary evidence."
)
_ADVICE_CONDENSE_POLICY = (
    "Condense this complete evidence ledger to at most 30,000 characters. Keep "
    "every requirement, decision, risk, test, disagreement, and open question."
)


def _advice_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _advice_content_defined_blocks(
    text: str, *, minimum: int, average: int, maximum: int,
) -> list[str]:
    """Bounded rolling blocks that resynchronise after local insertions."""

    if not text:
        return [""]
    base = 257
    word_mask = (1 << 64) - 1
    boundary_mask = average - 1
    remove_factor = pow(base, ADVICE_STORAGE_ROLLING_WINDOW, 1 << 64)
    rolling = 0
    start = 0
    blocks: list[str] = []
    for index, letter in enumerate(text):
        rolling = ((rolling * base) + ord(letter)) & word_mask
        if index >= ADVICE_STORAGE_ROLLING_WINDOW:
            rolling = (
                rolling
                - (ord(text[index - ADVICE_STORAGE_ROLLING_WINDOW]) * remove_factor)
            ) & word_mask
        size = index + 1 - start
        if size >= maximum or (
            size >= minimum and not (rolling & boundary_mask)
        ):
            blocks.append(text[start:index + 1])
            start = index + 1
    if start < len(text):
        blocks.append(text[start:])
    return blocks


def _advice_storage_blocks(text: str) -> list[str]:
    """Small exact recovery blocks, stable across appends and local insertions."""

    return _advice_content_defined_blocks(
        text, minimum=ADVICE_STORAGE_MIN_CHARS,
        average=ADVICE_STORAGE_BLOCK_CHARS,
        maximum=ADVICE_STORAGE_MAX_CHARS,
    )


def _advice_provider_chunks(text: str) -> list[str]:
    """Large stable provider chunks, each within the disclosed ingest boundary."""

    return _advice_content_defined_blocks(
        text, minimum=ADVICE_PROVIDER_CHUNK_MIN_CHARS,
        average=ADVICE_PROVIDER_CHUNK_AVG_CHARS,
        maximum=ADVICE_INGEST_CHARS,
    )


def _advice_policy_id(kind: str) -> str:
    policy = _ADVICE_SOURCE_POLICY if kind == "source" else _ADVICE_REDUCTION_POLICY
    described = json.dumps({
        "schema_version": 1,
        "kind": kind,
        "policy": policy,
        "condense_policy": _ADVICE_CONDENSE_POLICY,
        "summary_characters": ADVICE_SUMMARY_CHARS,
    }, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return _advice_sha(described)


def _advice_route_identity(config: Any, chat_lab: Any, route: str) -> dict[str, str]:
    describe = getattr(chat_lab, "chat_destination", None)
    if not callable(describe):
        raise SwarmError(
            "Nexus cannot resolve the exact provider route/model identity for "
            "paged-advice caching, so it stopped instead of sharing an ambiguous cache."
        )
    try:
        destination = describe(config, route)
    except HarnessError as exc:
        raise SwarmError(
            "Nexus could not resolve the exact provider route/model identity for "
            "paged-advice caching, so it stopped instead of sharing an ambiguous cache."
        ) from exc
    if (
        not isinstance(destination, dict)
        or not str(destination.get("provider_kind") or "")
        or "model" not in destination
    ):
        raise SwarmError(
            "Nexus received an incomplete provider route/model identity for paged-"
            "advice caching, so it stopped instead of sharing an ambiguous cache."
        )
    return {
        "route": str(route or ""),
        "provider_kind": str(destination.get("provider_kind") or ""),
        "model": str(destination.get("model") or ""),
    }


def _advice_existing_text(path: Path, expected: str, expected_sha: str) -> None:
    """Verify, never trust or repair, an existing content-addressed block."""

    try:
        held = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise SwarmError(
            f"Saved paged-advice block {path} is unreadable as exact UTF-8. "
            "Nexus stopped before publishing a manifest or receipt."
        ) from exc
    actual_sha = _advice_sha(held)
    if actual_sha != expected_sha or held != expected:
        raise SwarmError(
            f"Saved paged-advice block {path} failed its content-addressed "
            "integrity check. Nexus stopped before publishing a manifest or receipt."
        )


def _advice_store_exact_block(path: Path, expected: str, expected_sha: str) -> None:
    from .safety import put_this_file_in_place

    if path.exists():
        _advice_existing_text(path, expected, expected_sha)
        return
    put_this_file_in_place(path, expected)
    # The atomic writer is trusted for publication, but verifying the result also
    # closes an antivirus/filesystem race before a manifest can refer to it.
    _advice_existing_text(path, expected, expected_sha)


def _advice_cache_key(identity: dict[str, Any]) -> str:
    exact = json.dumps(
        identity, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return _advice_sha(exact)


def _advice_cached_summary(path: Path, identity: dict[str, Any]) -> str | None:
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8", errors="strict")
        saved = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SwarmError(
            f"Paged-advice provider cache {path} is unreadable or malformed. "
            "Nexus did not reuse unverified evidence."
        ) from exc
    from .runtime_integrity import compare

    summary = saved.get("summary") if isinstance(saved, dict) else None
    expected_key = _advice_cache_key(identity)
    material = {
        key: value for key, value in saved.items() if key != "integrity_mac"
    } if isinstance(saved, dict) else {}
    valid = (
        isinstance(saved, dict)
        and saved.get("schema_version") == 1
        and saved.get("kind") == "paged-advice-provider-summary"
        and saved.get("cache_key") == expected_key
        and saved.get("identity") == identity
        and isinstance(summary, str)
        and saved.get("summary_characters") == len(summary)
        and saved.get("summary_sha256") == _advice_sha(summary)
        and len(summary) <= ADVICE_SUMMARY_CHARS
        and compare(
            "paged-advice-provider-summary-v1", material,
            saved.get("integrity_mac"),
        )
    )
    if not valid:
        raise SwarmError(
            f"Paged-advice provider cache {path} failed its exact identity or "
            "content integrity check. Nexus did not reuse unverified evidence."
        )
    try:
        os.utime(path, None)
    except OSError:
        pass
    return summary


def _advice_save_summary(
    path: Path, identity: dict[str, Any], summary: str,
) -> str:
    """Publish one first-writer-wins provider result, verifying any race winner."""

    from .safety import put_this_file_in_place
    from .runtime_integrity import mac

    with _ADVICE_STORAGE_LOCK:
        held = _advice_cached_summary(path, identity)
        if held is not None:
            return held
        saved = {
            "schema_version": 1,
            "kind": "paged-advice-provider-summary",
            "cache_key": _advice_cache_key(identity),
            "identity": identity,
            "summary": summary,
            "summary_characters": len(summary),
            "summary_sha256": _advice_sha(summary),
        }
        saved["integrity_mac"] = mac("paged-advice-provider-summary-v1", saved)
        put_this_file_in_place(
            path, json.dumps(saved, ensure_ascii=False, indent=2) + "\n",
        )
        return _advice_cached_summary(path, identity) or ""


def _advice_reduction_groups(
    summaries: list[str],
) -> list[tuple[str, list[str]]]:
    """Group whole ledgers deterministically without cutting one or using totals."""

    groups: list[tuple[str, list[str]]] = []
    parts: list[str] = []
    hashes: list[str] = []
    for summary in summaries:
        summary_sha = _advice_sha(summary)
        part = f"EVIDENCE LEDGER SHA-256 {summary_sha}\n{summary}"
        proposed = "\n\n".join([*parts, part])
        if parts and len(proposed) > ADVICE_INGEST_CHARS:
            groups.append(("\n\n".join(parts), hashes))
            parts = [part]
            hashes = [summary_sha]
        else:
            parts.append(part)
            hashes.append(summary_sha)
    if parts:
        groups.append(("\n\n".join(parts), hashes))
    if any(len(context) > ADVICE_INGEST_CHARS for context, _hashes in groups):
        raise SwarmError(
            "A complete paged-advice ledger exceeded the disclosed reduction "
            "boundary. Nexus did not slice or silently omit it."
        )
    return groups


def _advice_json(path: Path) -> dict[str, Any] | None:
    try:
        held = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return held if isinstance(held, dict) else None


def _advice_prune_provider_cache(folder: Path) -> None:
    """Bound noncanonical provider projections independently of source records."""

    with _ADVICE_STORAGE_LOCK:
        cache = folder / "provider-cache"
        cached = [one for one in cache.glob("*.json") if one.is_file()]
        cached.sort(key=lambda one: one.stat().st_mtime)
        total = sum(one.stat().st_size for one in cached)
        while cached and (
            len(cached) > ADVICE_CACHE_FILES_TO_KEEP
            or total > ADVICE_CACHE_BYTES_TO_KEEP
        ):
            old = cached.pop(0)
            try:
                size = old.stat().st_size
                old.unlink()
                total -= size
            except OSError:
                pass


def _advice_prune_success(folder: Path) -> None:
    """Bound success-only source projections while preserving active failures."""

    _advice_prune_provider_cache(folder)
    with _ADVICE_STORAGE_LOCK:
        completed: list[Path] = []
        for path in folder.glob("advice-receipt-*.json"):
            saved = _advice_json(path)
            if saved is not None and saved.get("status") == "completed":
                completed.append(path)
        completed.sort(key=lambda one: one.stat().st_mtime if one.exists() else 0)
        for old in completed[:-ADVICE_RECEIPTS_TO_KEEP]:
            try:
                old.unlink(missing_ok=True)
            except OSError:
                pass

        # Fail closed on cleanup if any surviving source/receipt is malformed:
        # leaked disk is preferable to deleting an exact recovery block.
        referenced: set[str] = set()
        records = [*folder.glob("advice-active-*.json"), *folder.glob("advice-receipt-*.json")]
        for record in records:
            saved = _advice_json(record)
            if saved is None:
                return
            entries = saved.get("chunks", [])
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict):
                        referenced.add(str(entry.get("sha256") or ""))
            for field in ("storage_block_sha256", "chunk_sha256"):
                values = saved.get(field, [])
                if isinstance(values, list):
                    referenced.update(str(one) for one in values)
        chunks = folder / "chunks"
        for path in chunks.glob("*.txt"):
            if path.stem not in referenced:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass



def _advice_chunks(text: str, limit: int = ADVICE_INGEST_CHARS) -> list[str]:
    """Every character exactly once, preferring paragraph boundaries."""

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + limit)
        if end < len(text):
            boundary = text.rfind("\n\n", start + limit // 2, end)
            if boundary > start:
                end = boundary + 2
        chunks.append(text[start:end])
        start = end
    return chunks or [""]


def _say_with_paged_advice_context(
    config: Any, chat_lab: Any, route: str, asking: str, *, filed_under: str,
    audit_scope: str = "",
) -> dict[str, Any]:
    """Process an oversized advice prompt completely, then answer from reductions.

    Advice is optional/non-mutating, but it must not fail merely because several
    full agent handoffs no longer fit one provider request. Nexus persists the
    complete redacted source, asks the provider to extract evidence from every
    exact chunk, recursively reduces those summaries, and records only the final
    answer in the visible board chat. No input chunk is silently discarded.
    """

    if len(asking) <= chat_lab.MOST_LETTERS:
        return chat_lab.say(config, route, asking, filed_as=filed_under)

    from .redaction import CredentialRedactor
    from .safety import put_this_file_in_place

    complete = CredentialRedactor(config).text(str(asking))
    digest = _advice_sha(complete)
    folder = where_it_lives().with_name("swarm-context")
    folder.mkdir(parents=True, exist_ok=True)
    # One active manifest per exact run/turn/project. Small exact storage blocks
    # are independent from the larger provider chunks. A growing append-only
    # page therefore reuses every settled block, while success-only obsolete tail
    # blocks and old receipts can be collected without touching active failures.
    scope = hashlib.sha256(
        f"{audit_scope or filed_under}\0{route}".encode("utf-8")
    ).hexdigest()[:24]
    source = folder / f"advice-active-{scope}.json"
    audit = folder / f"advice-active-{scope}-summaries.json"
    receipt = folder / f"advice-receipt-{scope}.json"
    chunks = _advice_provider_chunks(complete)
    storage_blocks = _advice_storage_blocks(complete)
    chunk_folder = folder / "chunks"
    chunk_folder.mkdir(parents=True, exist_ok=True)
    chunk_entries: list[dict[str, Any]] = []
    # Verify every pre-existing hash object before publishing either manifest or
    # receipt. Holding the short storage lock also prevents local cleanup from
    # observing newly created blocks before their manifest exists.
    with _ADVICE_STORAGE_LOCK:
        for one in storage_blocks:
            chunk_sha = _advice_sha(one)
            chunk_path = chunk_folder / f"{chunk_sha}.txt"
            _advice_store_exact_block(chunk_path, one, chunk_sha)
            chunk_entries.append({
                "file": chunk_path.name,
                "sha256": chunk_sha,
                "characters": len(one),
            })
        put_this_file_in_place(source, json.dumps({
            "schema_version": 2,
            "kind": "paged-advice-exact-source",
            "source_sha256": digest,
            "source_characters": len(complete),
            "storage_block_characters": {
                "minimum": ADVICE_STORAGE_MIN_CHARS,
                "average": ADVICE_STORAGE_BLOCK_CHARS,
                "maximum": ADVICE_STORAGE_MAX_CHARS,
                "rolling_window": ADVICE_STORAGE_ROLLING_WINDOW,
            },
            "chunks_folder": str(chunk_folder),
            "chunks": chunk_entries,
            "provider_chunk_sha256": [_advice_sha(one) for one in chunks],
            "reconstruction": (
                "Verify each named storage-block SHA-256 and concatenate its exact "
                "UTF-8 text in order. Provider grouping does not alter this source."
            ),
        }, ensure_ascii=False, indent=2) + "\n")
    # Resolve the exact cache authority before publishing a processing receipt.
    # An unresolved route still leaves the exact source manifest reconstructable.
    try:
        provider = _advice_route_identity(config, chat_lab, route)
    except Exception:
        _advice_prune_provider_cache(folder)
        raise
    put_this_file_in_place(receipt, json.dumps({
        "schema_version": 2,
        "status": "processing",
        "source_sha256": digest,
        "source_characters": len(complete),
        "storage_block_sha256": [one["sha256"] for one in chunk_entries],
        "provider_chunk_sha256": [_advice_sha(one) for one in chunks],
    }, ensure_ascii=False, indent=2) + "\n")

    cache_folder = folder / "provider-cache"
    cache_folder.mkdir(parents=True, exist_ok=True)

    def extract(context: str, kind: str, input_hashes: list[str]) -> str:
        policy_id = _advice_policy_id(kind)
        identity: dict[str, Any] = {
            "schema_version": 1,
            "kind": kind,
            "input_sha256": list(input_hashes),
            "route": provider["route"],
            "provider_kind": provider["provider_kind"],
            "model": provider["model"],
            "policy_sha256": policy_id,
        }
        cache_key = _advice_cache_key(identity)
        cache_path = cache_folder / f"{kind}-{cache_key}.json"
        with _ADVICE_STORAGE_LOCK:
            cached = _advice_cached_summary(cache_path, identity)
        if cached is not None:
            return cached
        if kind == "source":
            prompt = (
                "NEXUS PAGED ADVICE INGEST source exact-chunk SHA-256 "
                f"{input_hashes[0]}. {_ADVICE_SOURCE_POLICY}"
            )
        else:
            prompt = (
                "NEXUS PAGED ADVICE INGEST reduction-stable ordered-ledger "
                f"SHA-256 values {', '.join(input_hashes)}. "
                f"{_ADVICE_REDUCTION_POLICY}"
            )
        result = chat_lab.ask_once(
            config, route, prompt, context=context,
            conversation_key=f"advice-{kind}-{cache_key[:32]}",
        )
        summary = str(result.get("text") or "")
        # Providers occasionally ignore a requested summary size. Loop on the
        # complete result while it still fits the disclosed message boundary;
        # never slice it into a plausible-looking summary.
        for attempt in range(3):
            if len(summary) <= ADVICE_SUMMARY_CHARS:
                break
            if len(summary) > chat_lab.MOST_LETTERS:
                raw = folder / f"advice-active-{scope}-oversize.txt"
                put_this_file_in_place(raw, summary)
                raise SwarmError(
                    f"{route or 'The assistant'} returned a {len(summary):,}-character "
                    f"ingest summary. Nexus preserved it at {raw} and did not truncate "
                    "it, but cannot safely use it as a bounded reduction."
                )
            result = chat_lab.ask_once(
                config, route, _ADVICE_CONDENSE_POLICY, context=summary,
                conversation_key=f"advice-condense-{cache_key[:32]}-{attempt}",
            )
            summary = str(result.get("text") or "")
        if len(summary) > ADVICE_SUMMARY_CHARS:
            raise SwarmError(
                f"{route or 'The assistant'} repeatedly refused the disclosed "
                "30,000-character evidence-ledger size. The full source remains saved; "
                "Nexus did not truncate or pretend the reduction succeeded."
            )
        return _advice_save_summary(cache_path, identity, summary)

    try:
        summaries = [extract(chunk, "source", [_advice_sha(chunk)]) for chunk in chunks]
        generation = 0
        while len("\n\n".join(summaries)) > ADVICE_INGEST_CHARS:
            generation += 1
            groups = _advice_reduction_groups(summaries)
            summaries = [
                extract(group, "reduction", input_hashes)
                for group, input_hashes in groups
            ]
            if generation > 20:
                raise SwarmError(
                    "The advice evidence reduction made no bounded progress after 20 "
                    "passes. Nexus stopped visibly and kept the complete source."
                )
        put_this_file_in_place(audit, json.dumps({
            "schema_version": 2,
            "source": str(source),
            "source_sha256": digest,
            "source_characters": len(complete),
            "provider": provider,
            "source_policy_sha256": _advice_policy_id("source"),
            "reduction_policy_sha256": _advice_policy_id("reduction"),
            "summaries": summaries,
        }, ensure_ascii=False, indent=2) + "\n")
        final_prompt = (
            "NEXUS PAGED ADVICE FINAL. Answer the original board request using the "
            "evidence ledgers below. Every character of the complete redacted source "
            f"was processed in ordered chunks. Verified source manifest: {source}; "
            f"SHA-256: {digest}; {len(complete):,} characters. Durable receipt: "
            f"{receipt}. Reduction working record: {audit}. State "
            "uncertainty plainly; do not claim you edited or tested project files.\n\n"
            + "\n\n".join(summaries)
        )
        if len(final_prompt) > chat_lab.MOST_LETTERS:
            raise SwarmError(
                "The final advice evidence projection still exceeds the disclosed chat "
                "boundary. Nexus kept the complete source and reductions and did not "
                "silently omit them."
            )
        answered = chat_lab.say(config, route, final_prompt, filed_as=filed_under)
    except Exception:
        # The active exact-source manifest is the reconstructable failure record.
        # A second per-run processing receipt would grow without adding recovery
        # authority, so only completed receipts are retained.
        for redundant in (receipt, audit):
            try:
                redundant.unlink(missing_ok=True)
            except OSError:
                pass
        _advice_prune_provider_cache(folder)
        raise

    put_this_file_in_place(receipt, json.dumps({
        "schema_version": 2,
        "status": "completed",
        "source_sha256": digest,
        "source_characters": len(complete),
        "storage_block_sha256": [one["sha256"] for one in chunk_entries],
        "provider_chunk_sha256": [_advice_sha(one) for one in chunks],
        "summary_sha256": [_advice_sha(one) for one in summaries],
        "provider": provider,
        "source_policy_sha256": _advice_policy_id("source"),
        "reduction_policy_sha256": _advice_policy_id("reduction"),
        "canonical_inputs": (
            "The board task, durable mailbox, and shared page remain canonical. "
            "This bounded receipt and verified storage blocks reconstruct its exact "
            "projection; provider summaries are integrity-checked caches only."
        ),
    }, ensure_ascii=False, indent=2) + "\n")
    for temporary in (source, audit, folder / f"advice-active-{scope}-oversize.txt"):
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # Stable names bound storage even if antivirus holds one briefly;
            # the next attempt atomically replaces the same file.
            pass
    _advice_prune_success(folder)
    return answered


class Running:
    """One run of the board at a time.

    One at a time because these are real assistants working on real folders,
    and two runs of the same board would have every agent doing its job twice
    while reading half of somebody else's.
    """

    def __init__(self, run_store=None) -> None:
        self._lock = threading.Lock()
        self._run_store = run_store
        self._run_id = ""
        self._doing: Doing | None = None
        self._stop = False
        self._thread: threading.Thread | None = None
        self._board_version = 0

    @property
    def busy(self) -> bool:
        with self._lock:
            return bool(self._doing and self._doing.going)

    def why_it_cannot_be_changed(self) -> str:
        """Why the board may not be changed right now, or nothing.

        Said in one place, because the panel greys the buttons out and the
        server turns the save down, and those two have to agree.
        """

        if not self.busy:
            return ""
        return (
            "The board is going, so it cannot be changed until it finishes. "
            "An agent renamed halfway through would have what it says land in "
            "a conversation nothing points at any more. Press Stop first."
        )

    def how_it_is_going(
        self, run_id: str = "", after: int = 0
    ) -> dict[str, Any] | None:
        identity = str(run_id or "").strip()
        if self._run_store is not None and (identity or self._run_id):
            identity = identity or self._run_id
            full = self._run_store.get(identity)
            delta = self._run_store.projection(identity, after)
            doing = None
            if isinstance(full.get("result"), dict):
                doing = full["result"].get("doing")
            if not isinstance(doing, dict):
                event = self._run_store.latest_event(identity, "board_progress")
                if event is not None and isinstance(event["payload"], dict):
                    doing = event["payload"]
            if not isinstance(doing, dict):
                doing = {
                    "run_id": full["run_id"], "request_id": full["request_id"],
                    "going": full["status"] in {"accepted", "running", "stopping"},
                    "stopped": full["status"] == "stopped", "note": full["error"],
                    "turns": [], "done": 0, "of": 0, "went_wrong": 0,
                }
            answer = dict(doing)
            answer.update({
                "run_id": full["run_id"], "request_id": full["request_id"],
                "status": full["status"], "cursor": delta["cursor"],
                "next_cursor": delta["next_cursor"],
                "has_more": delta["has_more"],
                "events": delta["events"],
                "going": full["status"] in {"accepted", "running", "stopping"},
                "resumable": bool(full.get("resumable")),
                "recovery_action": str(full.get("recovery_action") or ""),
            })
            if full["status"] in {
                "failed", "stopped", "interrupted", "delivery_unknown", "outcome_unknown"
            } and full.get("error"):
                # A stale board_progress projection is useful for counts, but
                # it must not hide the terminal recovery instruction.
                answer["note"] = str(full["error"])
            return answer
        with self._lock:
            return self._doing.to_dict() if self._doing else None

    def what_they_said(self) -> dict[str, Any]:
        """What was passed from one agent to another, whole.

        The run that is going, if one is, so it can be read as it happens; and
        otherwise the last one, read back off the disk so it is still there
        after the panel has been closed and opened again.
        """

        from . import agent_mailbox as mailbox

        delivery_trouble = ""
        try:
            delivery = mailbox.status(where_the_mailbox_lives())
            delivery["counts_known"] = True
        except (OSError, HarnessError) as exc:
            delivery_trouble = (
                "Nexus could not verify the durable handoff mailbox, so delivery "
                f"counts are unknown: {exc}"
            )
            delivery = {
                "queued": None,
                "acknowledged": None,
                "retrying": None,
                "counts_known": False,
                "trouble": delivery_trouble,
            }
        with self._lock:
            doing = self._doing
        if doing is not None:
            result = {
                "note": doing.note,
                "going": doing.going,
                "dropped": doing.dropped,
                "most": MOST_NOTES,
                "delivery": delivery,
                "notes": [one.to_dict() for one in doing.notes],
            }
            if delivery_trouble:
                result["delivery_trouble"] = delivery_trouble
            return result
        result = dict(
            read_what_they_said(), going=False, most=MOST_NOTES,
            delivery=delivery,
        )
        if delivery_trouble:
            result["delivery_trouble"] = delivery_trouble
        return result

    def stop(self, run_id: str = "") -> str:
        wanted = str(run_id or "").strip()
        if self._run_store is not None and not wanted:
            raise SwarmError("The exact Swarm run ID is required to stop a board run.")
        durable = None
        if self._run_store is not None:
            # The journal is the authority, not this server process. Another
            # Nexus process can therefore stop the exact board run it can see;
            # the owning worker observes this barrier before its next effect.
            durable = self._run_store.request_stop(wanted)
            if durable["status"] == "stopped":
                # The owner may observe the committed Stop barrier and publish
                # its terminal state before request_stop projects the row back
                # to this process. That is successful monotonic completion,
                # including for a repeated exact Stop, not a failed request.
                return "Stopped. This Swarm run is already stopped."
            if durable["status"] != "stopping":
                raise SwarmError("That Swarm run is already over; nothing was stopped.")
        with self._lock:
            locally_owned = bool(
                self._doing and self._doing.going
                and (self._run_store is None or wanted == self._run_id)
            )
            if not locally_owned:
                if self._run_store is not None:
                    return (
                        "Stopping. The owning Nexus process will observe the durable Stop barrier before another effect."
                    )
                return "Nothing is going, so there is nothing to stop."
            self._stop = True
            self._doing.note = "Stopping after the turn that is going now."
        return (
            "Stopping. The turn already asked for will finish, because there is "
            "no way to un-ask it, and nothing after it will be asked."
        )

    def start(
        self, config, standing: dict[str, Any] | None = None,
        request_id: str = "",
    ) -> dict[str, Any]:
        # Electron decorates the ordinary board view with live consumer-web
        # routes.  A server-started run must use that same view or a web agent
        # appears ready in its settings and then vanishes from the run plan.
        said = standing if standing is not None else how_it_stands(config)
        # Which board this run is working from. Anything that would change it
        # is turned down while the run is going: a turn already in flight
        # writes what it says under the name the agent had when it was asked,
        # and an agent renamed halfway through would have that answer land in
        # a conversation nothing points at any more.
        self._board_version = said["board"]["version"]
        turns = self._plan(said)
        if not turns:
            raise SwarmError(
                "There is nothing to set going. An agent needs an assistant "
                "chosen, a project needs jobs written down, and somebody has to "
                "work on it. What is not ready says which of those is missing."
            )
        with self._lock:
            if self._doing and self._doing.going:
                raise SwarmError(
                    "The board is already going. Wait for it, or press Stop."
                )
            run_id = ""
            accepted = None
            if self._run_store is not None:
                snapshot = {
                    "kind": "board_order",
                    "board": said["board"],
                    "objective_generations": {
                        str(project.get("id") or ""): shared_goal_id(project)
                        for project in said["board"].get("projects", [])
                    },
                }
                accepted, created = self._run_store.accept(request_id, snapshot)
                run_id = str(accepted["run_id"])
                if not created:
                    return self.how_it_is_going(run_id) or {
                        "run_id": run_id, "request_id": accepted["request_id"],
                        "going": accepted["status"] in {"accepted", "running", "stopping"},
                    }
            self._stop = False
            self._doing = Doing(
                note="Asking each of them on their own first.", turns=turns,
                run_id=run_id,
                request_id=str(accepted["request_id"] if accepted else request_id),
            )
            doing = self._doing
            self._run_id = run_id
        if self._run_store is not None:
            try:
                self._run_store.start(run_id)
                self._record_progress(doing)
            except BaseException as exc:
                doing.going = False
                self._run_store.fail(run_id, f"The board run could not start: {exc}")
                raise

        def work() -> None:
            run_scope = None
            try:
                if self._run_store is not None:
                    from . import swarm_runs as swarm_runtime

                    run_scope = swarm_runtime.bind(self._run_store, run_id)
                    run_scope.__enter__()
                self._do_it(config, said, doing)
            except BaseException as exc:  # noqa: BLE001 - nothing may leave it going
                doing.note = f"This stopped in a way nobody expected: {exc}"
                if self._run_store is not None:
                    self._run_store.fail(run_id, str(exc), stopped=doing.stopped)
            finally:
                # However this ends, the run stops being one that is going. One
                # that says it is still going refuses every later press, and the
                # only way out would be restarting the panel.
                doing.going = False
                if self._run_store is not None:
                    try:
                        if doing.stopped:
                            self._run_store.fail(run_id, doing.note, stopped=True)
                        else:
                            durable_status = self._run_store.get(run_id)["status"]
                            if durable_status == "stopping":
                                doing.stopped = True
                                doing.note = "Stopped after the in-flight provider turn."
                                self._run_store.fail(run_id, doing.note, stopped=True)
                            elif durable_status == "running":
                                self._run_store.finish(run_id, {"doing": doing.to_dict()})
                    finally:
                        if run_scope is not None:
                            run_scope.__exit__(None, None, None)

        thread = threading.Thread(target=work, name="the-board", daemon=True)
        self._thread = thread
        try:
            thread.start()
        except BaseException as exc:
            doing.going = False
            if self._run_store is not None:
                self._run_store.fail(run_id, f"The board worker could not start: {exc}")
            raise
        return doing.to_dict()

    def _record_progress(self, doing: Doing) -> None:
        if self._run_store is not None and doing.run_id:
            self._run_store.checkpoint(doing.run_id, "board_progress", doing.to_dict())

    def _stop_was_requested(self, doing: Doing) -> bool:
        with self._lock:
            if self._stop:
                return True
        if self._run_store is None or not doing.run_id:
            return False
        try:
            return self._run_store.should_stop(doing.run_id)
        except HarnessError:
            # Losing the authority record is never permission to keep causing
            # effects. The final error path preserves the failure evidence.
            return True

    @contextmanager
    def _post_provider_mutation(self, doing: Doing):
        """Linearize an external write against the exact Stop command."""

        if self._run_store is not None and doing.run_id:
            with self._run_store.post_provider_mutation(doing.run_id):
                yield
            return
        # Legacy in-process callers use the same lock as stop(). Stop either
        # waits for this mutation to finish or is observed before it begins.
        with self._lock:
            if self._stop:
                raise HarnessError(
                    "Stop was accepted before this post-provider mutation."
                )
            yield

    def wait(self, seconds: float = 5.0) -> None:
        """Only used by tests, so they never depend on how fast a machine is."""

        thread = self._thread
        if thread:
            thread.join(seconds)

    # -- the work itself ---------------------------------------------------

    def _plan(self, said: dict[str, Any]) -> list[OneTurn]:
        """Every turn this run will take, worked out before any of it starts.

        Written down first so the panel can show what is coming rather than a
        list that grows while somebody watches it, and so a run with nothing to
        do is refused at the press rather than after the first assistant has
        already been asked.
        """

        board = said["board"]
        agents = {one["id"]: one for one in board["agents"]}
        first: list[OneTurn] = []
        second: list[OneTurn] = []
        for project in board["projects"]:
            if not project["tasks"] or not project["is_there"]:
                continue
            ready = [
                one for one in who_works_on(board, project["id"]) if one["ready"]
            ]
            for agent in ready:
                first.append(OneTurn(
                    agent=agent["id"], name=agent["name"],
                    project=project["id"], where=project["name"], round=ON_ITS_OWN,
                    shared_goal=shared_goal_id(project),
                ))
            for agent in ready:
                if any(
                    may_they_talk(board, agent["id"], other["id"])
                    for other in ready if other["id"] != agent["id"]
                ):
                    second.append(OneTurn(
                        agent=agent["id"], name=agent["name"],
                        project=project["id"], where=project["name"],
                        round=AFTER_THE_OTHERS,
                        shared_goal=shared_goal_id(project),
                    ))
        return first + second

    def _do_it(self, config, said: dict[str, Any], doing: Doing) -> None:
        from . import chat as chat_lab
        from . import pages as pages_lab
        from . import agent_mailbox as mailbox

        board = said["board"]
        agents = {one["id"]: one for one in board["agents"]}
        projects = {one["id"]: one for one in board["projects"]}
        # What each agent said about each project, so the second round can be
        # shown only the notes that agent is allowed to see.
        heard: dict[tuple[str, str], str] = {}
        for turn in doing.turns:
            stopping = self._stop_was_requested(doing)
            if stopping:
                turn.state = "not done"
                turn.why_not = "Stopped before this one was asked."
                doing.stopped = True
                self._record_progress(doing)
                continue
            agent = agents[turn.agent]
            project = projects[turn.project]
            if turn.round == ON_ITS_OWN:
                asking = what_to_ask(agent, project)
                incoming = []
            else:
                allowed = [
                    held for held in agents
                    if held != turn.agent
                    and may_they_talk(board, turn.agent, held)
                ]
                try:
                    incoming = mailbox.pending(
                        where_the_mailbox_lives(),
                        shared_goal_id=turn.shared_goal,
                        receiver=turn.agent,
                        allowed_senders=allowed,
                    )
                except (OSError, HarnessError) as error:
                    # A broken durable handoff is not the same thing as an
                    # empty handoff.  Continuing from only the in-memory
                    # fallback would silently omit evidence another agent had
                    # queued and could let an incomplete turn look successful.
                    turn.state = "not done"
                    turn.why_not = (
                        "The durable agent handoff could not be read, so Nexus "
                        "stopped this turn instead of silently omitting another "
                        f"agent's evidence: {error}"
                    )
                    self._record_progress(doing)
                    continue
                notes = [(one.sender_name, one.body) for one in incoming]
                already = {one.sender for one in incoming}
                notes.extend([
                    (agents[held]["name"], text)
                    for (held, where), text in heard.items()
                    if where == turn.project
                    and held not in already
                    and held != turn.agent
                    and may_they_talk(board, turn.agent, held)
                ])
                if not notes:
                    turn.state = "not done"
                    turn.why_not = "Nobody it may talk to had anything to show it."
                    self._record_progress(doing)
                    continue
                # The page itself, rather than the notes gathered above. Read
                # here, so what goes in front of this agent is the page as it
                # stands this moment - including anything another agent wrote
                # while this one was waiting its turn.
                page_now = None
                if project.get("path"):
                    try:
                        page_now = pages_lab.read_the_page(
                            config, project["path"], project["name"])
                    except HarnessError as error:
                        # A missing page is already represented by an empty
                        # readable page. Corruption, tampering, or an I/O error
                        # must not be relabelled as "no page" and answered from
                        # only the notes, because that silently omits evidence.
                        turn.state = "not done"
                        turn.why_not = (
                            "The shared project page could not be read, so Nexus "
                            "stopped this turn instead of silently omitting its "
                            f"evidence: {error}"
                        )
                        self._record_progress(doing)
                        continue
                if page_now is not None and page_now.parts:
                    turn.after = page_now.up_to
                    allowed_page_writers = {"person", turn.agent, *allowed}
                    # Advice agents have no project-file tool, so a raw path to
                    # an overflow manifest would be operationally useless. Give
                    # the paged-ingestion workflow the complete authorised page;
                    # it processes every exact chunk and keeps its own audit.
                    prompt_page = pages_lab.complete_page_for_transfer(
                        page_now, only_from=allowed_page_writers
                    )
                    asking = what_the_page_says(
                        agent,
                        project,
                        prompt_page,
                        notes,
                    )
                else:
                    # No page to share, which happens when a project box points
                    # at a folder that is not there any more. The notes gathered
                    # by hand are what is left, and are better than nothing.
                    asking = what_the_others_said(agent, project, notes)
                # Written down as it is passed, so somebody watching can read
                # what each agent was actually given rather than take it on
                # trust that the right thing was shown to the right one.
                turn.shown = [name for name, _text in notes]
                delivered = incoming or [None for _one in notes]
                for at, note in enumerate(notes):
                    message = delivered[at] if at < len(delivered) else None
                    sender_name, text = note
                    sender = message.sender if message is not None else next(
                        (held for held, known in agents.items()
                         if known["name"] == sender_name), "")
                    message_id = message.message_id if message is not None else ""
                    shown_text, original_characters, projection_source = _note_for_the_screen(
                        text,
                        run_id=doing.run_id,
                        message_id=message_id,
                        sender=sender or sender_name,
                        project=turn.project,
                    )
                    doing.notes.append(OneNote(
                        said_by=sender,
                        said_by_name=sender_name,
                        shown_to=turn.agent,
                        shown_to_name=agent["name"],
                        project=turn.project,
                        where=project["name"],
                        text=shown_text,
                        at=_the_time_now(),
                        message_id=message_id,
                        thread_id=(message.thread_id if message is not None else ""),
                        status="queued" if message is not None else "acknowledged",
                        attempts=(message.attempts if message is not None else 0),
                        original_characters=original_characters,
                        projection_source=projection_source,
                    ))
                    # The oldest fall off the end rather than the newest never
                    # being written: what somebody reads this for is what just
                    # happened. How many were dropped is said out loud below.
                    if len(doing.notes) > MOST_NOTES:
                        doing.dropped += len(doing.notes) - MOST_NOTES
                        del doing.notes[:len(doing.notes) - MOST_NOTES]
            turn.state = "asking"
            doing.note = f"Asking {turn.name} about {turn.where}, {turn.round}."
            self._record_progress(doing)
            started = time.monotonic()
            try:
                answered = _say_with_paged_advice_context(
                    config, chat_lab, agent["who"], asking,
                    filed_under=filed_as(filed_as_on_the_board(
                        str(agent.get("filed_as") or agent["name"])
                    )),
                    audit_scope=(
                        f"{doing.run_id}:{turn.project}:{turn.shared_goal}:"
                        f"{turn.agent}:{turn.round}"
                    ),
                )
            except HarnessError as exc:
                turn.state = "went wrong"
                turn.why_not = str(exc)
                if incoming and not self._stop_was_requested(doing):
                    try:
                        with self._post_provider_mutation(doing):
                            mailbox.attempted(
                                where_the_mailbox_lives(),
                                [one.message_id for one in incoming],
                                str(exc),
                            )
                    except (OSError, HarnessError):
                        pass
            else:
                turn.state = "done"
                turn.said = str(answered.get("answer", {}).get("text") or "")
                if self._stop_was_requested(doing):
                    # The in-flight provider answer is evidence and is kept in
                    # this turn, but Stop is an effect barrier: no page write,
                    # inbox acknowledgement, or new handoff may follow it.
                    doing.stopped = True
                    turn.why_not = (
                        "It answered after Stop. The answer was kept, but Nexus did not write it to the shared page or create another handoff."
                    )
                    turn.milliseconds = int((time.monotonic() - started) * 1000)
                    self._record_progress(doing)
                    continue
                # On the page, in the order the lock let it through. This is the
                # part that makes two agents unable to talk over each other:
                # nobody writes into anybody else's words, so there is nothing
                # to interrupt.
                handoff_is_durable = not (turn.said and project.get("path"))
                if turn.said and project.get("path"):
                    try:
                        with self._post_provider_mutation(doing):
                            landed = pages_lab.add_to_the_page(
                                config, project["path"],
                                who=agent["name"],
                                text=turn.said,
                                # Compared against the round itself rather than a
                                # sentence typed out again here. Written twice, the
                                # two drifted apart and every part on the page said
                                # "after reading the page", including the first
                                # round where nobody had read anything.
                                what_they_were_doing=(
                                    ON_ITS_OWN if turn.round == ON_ITS_OWN
                                    else "after reading the page"),
                                after=turn.after,
                                author_id=turn.agent,
                                name=project["name"],
                            )
                        turn.part = int(landed.get("number") or 0)
                        handoff_is_durable = True
                        # Said out loud when somebody got there first, because
                        # that is the moment a person wants to know two of them
                        # were working at once.
                        if landed.get("note"):
                            doing.note = str(landed["note"])
                    except HarnessError as exc:
                        if self._stop_was_requested(doing):
                            doing.stopped = True
                            turn.why_not = (
                                "It answered after Stop. The answer was kept, but Nexus refused the shared-page write."
                            )
                            turn.milliseconds = int((time.monotonic() - started) * 1000)
                            self._record_progress(doing)
                            continue
                        # The answer is not lost over this. It is in the turn,
                        # it is on the screen, and the page is a record rather
                        # than the only copy.
                        turn.why_not = f"It answered, and the page would not take it: {exc}"
                # A queued handoff is acknowledged only after the receiving
                # answer is durably present in its chat and, when the project
                # has a shared notebook, after that notebook accepted it. A
                # page failure must leave the exact incoming message retryable.
                if incoming and handoff_is_durable:
                    try:
                        with self._post_provider_mutation(doing):
                            mailbox.acknowledge(
                                where_the_mailbox_lives(),
                                [one.message_id for one in incoming],
                            )
                        acknowledged = {one.message_id for one in incoming}
                        for note in doing.notes:
                            if note.message_id in acknowledged:
                                note.status = "acknowledged"
                                note.attempts += 1
                    except (OSError, HarnessError) as exc:
                        if self._stop_was_requested(doing):
                            doing.stopped = True
                            turn.why_not = (
                                "It answered after Stop. The answer was kept, but its inbox was not acknowledged."
                            )
                            turn.milliseconds = int((time.monotonic() - started) * 1000)
                            self._record_progress(doing)
                            continue
                        turn.why_not = (
                            f"It answered, but its inbox acknowledgement could not be "
                            f"written: {exc}"
                        )
                heard[(turn.agent, turn.project)] = turn.said
                # First-round answers are durable messages to every connected
                # collaborator. If a receiving provider fails later, these are
                # replayed on the next run instead of disappearing with this
                # process.
                if turn.round == ON_ITS_OWN and turn.said:
                    thread_id = ""
                    for receiver in who_works_on(board, turn.project):
                        if (receiver["id"] == turn.agent or not receiver.get("ready")
                                or not may_they_talk(
                                    board, turn.agent, receiver["id"])):
                            continue
                        try:
                            with self._post_provider_mutation(doing):
                                queued = mailbox.enqueue(
                                    where_the_mailbox_lives(),
                                    shared_goal_id=turn.shared_goal,
                                    sender=turn.agent,
                                    sender_name=agent["name"],
                                    receiver=receiver["id"],
                                    receiver_name=receiver["name"],
                                    project=turn.project,
                                    project_name=project["name"],
                                    body=turn.said,
                                    expects_reply=True,
                                    thread_id=thread_id,
                                )
                            thread_id = queued.thread_id
                        except (OSError, HarnessError) as exc:
                            if self._stop_was_requested(doing):
                                doing.stopped = True
                                turn.why_not = (
                                    "It answered after Stop. The answer was kept, but Nexus refused a new handoff."
                                )
                                break
                            warning = f"Its answer was kept, but one handoff was not queued: {exc}"
                            turn.why_not = f"{turn.why_not} {warning}".strip()
            turn.milliseconds = int((time.monotonic() - started) * 1000)
            self._record_progress(doing)
        doing.note = _how_it_went(doing)
        _keep_what_they_said(doing)
        self._record_progress(doing)


def _the_time_now() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


def where_what_they_said_lives() -> Path:
    """Where the last run's exchange is kept.

    Beside the board, for the same reason: a run spans projects. Only the last
    one is kept - it is there so somebody can read what happened after the
    panel has been closed and opened again, not as a history of everything.
    """

    return where_it_lives().with_name("swarm-last-run.json")


def _keep_what_they_said(doing: Doing) -> None:
    from .safety import put_this_file_in_place

    try:
        where = where_what_they_said_lives()
        where.parent.mkdir(parents=True, exist_ok=True)
        put_this_file_in_place(where, json.dumps({
            "schema_version": 1,
            "note": doing.note,
            "dropped": doing.dropped,
            "notes": [one.to_dict() for one in doing.notes],
        }, indent=2) + "\n")
    except OSError:
        # Not being able to write down what was said is not worth failing a run
        # that has already happened for. It is still on screen either way.
        pass


def read_what_they_said() -> dict[str, Any]:
    """The last run's exchange, read back off the disk."""

    try:
        said = json.loads(where_what_they_said_lives().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"note": "", "notes": [], "dropped": 0}
    if not isinstance(said, dict):
        return {"note": "", "notes": [], "dropped": 0}
    held = said.get("notes")
    return {
        "note": str(said.get("note") or ""),
        "dropped": _how_many_times(said.get("dropped")),
        "notes": [one for one in held if isinstance(one, dict)] if isinstance(held, list) else [],
    }


def _how_it_went(doing: Doing) -> str:
    done = len([one for one in doing.turns if one.state == "done"])
    wrong = len([one for one in doing.turns if one.state == "went wrong"])
    if doing.stopped:
        return f"Stopped. {done} of {len(doing.turns)} were asked."
    if wrong:
        return (
            f"{done} answered and {wrong} went wrong. What each of them said is "
            "in its own conversation, on its box."
        )
    return (
        f"All {done} answered. What each of them said is in its own "
        "conversation, on its box."
    )


# ---------------------------------------------------------------------------
# Boards kept under a name
# ---------------------------------------------------------------------------
#
# The board you are working on is written down on its own and comes back on its
# own. These are the ones somebody meant to keep: a shape worth having again,
# saved under a name, opened when it is wanted.
#
# Kept beside the working board rather than inside it, so nothing about saving
# can damage the one you are actually using.

# How long a name may be, so it fits on a row and in a file name.
LONGEST_BOARD_NAME = 48
SAVED_BOARD_DOCUMENT = "nexus-harness.saved-board.v1"
# A full board may contain 24 resized profile pictures of up to 400 kB each.
# Keep the exchange boundary aligned with the server's ordinary request limit
# so every valid board can be backed up instead of discovering a smaller,
# hidden import/export limit later.
# All individually valid agents, role descriptions, pictures, and long-horizon
# tasks must fit one portable saved-board document. This matches the dedicated
# Swarm HTTP transport boundary; no lower hidden aggregate cap exists.
# The field limits permit roughly 96 million task characters. UTF-8 and JSON
# escaping can use more than one byte per character, so the transport envelope
# must be substantially larger than the character count. This ceiling fits the
# worst valid structured board; it is not a smaller hidden product limit.
MAX_SAVED_BOARD_DOCUMENT_BYTES = 768_000_000


def _portable_board_document(
    name: str, saved_at: str, board: Board
) -> dict[str, Any]:
    """Build and size the exact portable file shown to the user.

    A tolerant board read may derive display fields such as a project's name.
    Those fields can make the canonical export larger than the uploaded input,
    so the exported envelope itself is the boundary that matters.
    """

    document = {
        "format": SAVED_BOARD_DOCUMENT,
        "name": name,
        "saved_at": saved_at,
        "board": board.to_dict(),
    }
    written = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    try:
        written_bytes = written.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise SwarmError(
            "This board contains an invalid lone Unicode surrogate. Nexus did "
            "not save, import, or partially rewrite it."
        ) from exc
    if len(written_bytes) > MAX_SAVED_BOARD_DOCUMENT_BYTES:
        raise SwarmError(
            f"This board is larger than the disclosed "
            f"{MAX_SAVED_BOARD_DOCUMENT_BYTES:,}-byte saved-board limit after "
            "Nexus prepares its portable JSON. Shorten an unusually long project "
            "path or use smaller profile pictures; nothing was saved."
        )
    return document


def where_the_kept_ones_live() -> Path:
    from .config import user_config_path

    return user_config_path().parent / "swarms"


def _filed_under(name: str) -> str:
    """The file one saved board goes in.

    A name is something somebody typed, so it is checked rather than trusted,
    and it may not reach out of the folder it belongs in.
    """

    said = " ".join(str(name or "").split())
    if not said:
        raise SwarmError("Give the board a name first.")
    if len(said) > LONGEST_BOARD_NAME:
        raise SwarmError(
            f"That name is too long. {LONGEST_BOARD_NAME} letters is the most.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]*", said):
        raise SwarmError(
            f"{said!r} is not a name a board can be kept under. Names hold "
            "letters, numbers, spaces, dots, dashes and underscores."
        )
    tidy = said.replace(" ", "-").lower()
    # A file name on Windows does not care about capitals, so "Friday" and
    # "friday" - two names somebody meant to keep apart - would share one file
    # and one of them would quietly become the other.
    marked = hashlib.sha256(said.encode("utf-8")).hexdigest()[:8]
    return f"{tidy}-{marked}.json"


@_requires_board_qa_access
def kept_board_inventory() -> tuple[list[dict[str, Any]], list[str]]:
    """Return healthy saved boards and honest, non-fatal file problems.

    A damaged file is still the person's data. Silently omitting it makes the
    panel say it vanished, so disclose its filename while leaving every healthy
    board available.
    """

    where = where_the_kept_ones_live()
    active = load().active_saved_board
    found: list[dict[str, Any]] = []
    problems: list[str] = []
    try:
        files = sorted(where.glob("*.json"))
    except OSError as exc:
        return [], [f"Saved-board folder cannot be read: {exc}"]
    for one in files:
        try:
            held = json.loads(one.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            problems.append(f"{one.name}: {exc}")
            continue
        if not isinstance(held, dict) or not isinstance(held.get("name"), str):
            problems.append(f"{one.name}: saved board root or name is invalid")
            continue
        if not isinstance(held.get("board"), dict):
            problems.append(f"{one.name}: saved board content is missing or invalid")
            continue
        board = held["board"]
        try:
            checked = read_it(board)
        except SwarmError as exc:
            problems.append(f"{held['name']} ({one.name}): {exc}")
            continue
        found.append({
            "name": held["name"],
            "saved_at": str(held.get("saved_at") or ""),
            "agents": len(checked.agents),
            "projects": len(checked.projects),
            "active": held["name"] == active,
        })
    return sorted(found, key=lambda one: one["saved_at"], reverse=True), problems


def every_kept_board() -> list[dict[str, Any]]:
    """Every healthy board somebody saved, newest first."""

    return kept_board_inventory()[0]


@_requires_board_qa_access
def export_kept_board(name: str) -> dict[str, Any]:
    """Return one validated, portable saved-board document."""

    opened_as = " ".join(str(name).split())
    where = where_the_kept_ones_live() / _filed_under(opened_as)
    try:
        written = where.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SwarmError(f"There is no saved board called {name}.") from exc
    except OSError as exc:
        raise SwarmError(
            f"The saved board called {name} is still on disk but cannot be read: {exc}"
        ) from exc
    try:
        held = json.loads(written)
    except json.JSONDecodeError as exc:
        raise SwarmError(
            f"The saved board called {name} is still on disk but its JSON is damaged: {exc.msg}."
        ) from exc
    except RecursionError as exc:
        raise SwarmError(
            f"The saved board called {name} is nested too deeply to read."
        ) from exc
    board = held.get("board") if isinstance(held, dict) else None
    if not isinstance(board, dict):
        raise SwarmError(f"The board saved as {name} cannot be read.")
    # Export only a board that this version can actually open. This prevents a
    # damaged local file becoming a convincing-looking backup that cannot be
    # restored on the next computer. Command approval is local authority, not
    # portable board content: another machine/path must ask explicitly again.
    checked = read_it(board)
    if not checked.workspace_id:
        marked = hashlib.sha256(opened_as.casefold().encode("utf-8")).hexdigest()[:24]
        checked.workspace_id = f"workspace-saved-{marked}"
    for project in checked.projects:
        project.approved_test_command_digest = ""
    return _portable_board_document(
        opened_as, str(held.get("saved_at") or ""), checked
    )


@_requires_board_qa_access
def import_kept_board(document: Any, name: str = "") -> dict[str, Any]:
    """Validate and save one portable board without opening it.

    Importing a named snapshot is deliberately independent of project run
    authority. A copied project's automation may be paused, but that must not
    hide or hold hostage the user's own backup files. Opening or changing the
    live board still goes through the run-aware mutation authority.
    """

    if not isinstance(document, dict):
        raise SwarmError("That JSON file is not a saved Nexus board.")
    if set(document) - {"format", "name", "saved_at", "board"}:
        raise SwarmError(
            "That saved-board file contains unsupported top-level fields. "
            "Nothing was imported."
        )
    if "format" in document and document.get("format") != SAVED_BOARD_DOCUMENT:
        raise SwarmError("That JSON file is not a saved Nexus board.")
    board_value = document.get("board")
    if not isinstance(board_value, dict):
        raise SwarmError("That JSON file does not contain a board.")
    _check_import_shape(board_value)
    wanted = " ".join(str(name or document.get("name") or "").split())
    filed = _filed_under(wanted)
    checked = read_it(board_value)
    # A JSON import is authority to preserve a layout, not authority to execute
    # project-discovered commands. Even a correctly shaped imported digest is
    # cleared so opening the snapshot requires a visible local approval.
    for project in checked.projects:
        project.approved_test_command_digest = ""
    # An imported layout starts a new local workspace. Portable agent ids and
    # a workspace id from another computer are not authority to claim this
    # installation's transcripts or provider conversations.
    checked.workspace_id = _new_workspace_id()
    # A board imported as a saved snapshot is not silently made the active
    # board. The person chooses when to replace the canvas by opening it.
    checked.active_saved_board = ""
    where = where_the_kept_ones_live()
    where.mkdir(parents=True, exist_ok=True)
    from .safety import ProjectTransactionLock

    # The target filename contains the display-name hash, so Friday and friday
    # are different targets. Serialize the casefolded inventory check across
    # processes as well as using a no-clobber final link.
    with ProjectTransactionLock(where.parent).held(timeout_seconds=10):
        target = where / filed
        if target.exists() or any(
            one["name"].casefold() == wanted.casefold() for one in every_kept_board()
        ):
            raise SwarmError(
                f'There is already a saved board called "{wanted}". Choose another name.'
            )
        held = {
            "name": wanted,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "board": checked.to_dict(),
        }
        _portable_board_document(wanted, held["saved_at"], checked)
        beside = where / f".{filed}.{os.getpid()}.{time.time_ns()}.part"
        try:
            beside.write_text(json.dumps(held, indent=2) + "\n", encoding="utf-8")
            try:
                os.link(beside, target)
            except FileExistsError as exc:
                raise SwarmError(
                    f'There is already a saved board called "{wanted}". Choose another name.'
                ) from exc
        finally:
            beside.unlink(missing_ok=True)
    return {"name": wanted, "saved_at": held["saved_at"]}


def _check_import_shape(board: dict[str, Any]) -> None:
    """Reject exchange files that the ordinary tolerant loader would trim."""

    if set(board) - {
        "schema_version", "version", "made_agents", "made_projects", "agents",
        "projects", "works_on", "talks_to", "active_saved_board",
        "binding_schema_version", "workspace_id",
    }:
        raise SwarmError(
            "The imported board contains unsupported board fields. Nothing was imported."
        )
    if "schema_version" in board and board["schema_version"] != 1:
        raise SwarmError("The imported board uses an unsupported schema version.")
    if (
        "binding_schema_version" in board
        and board["binding_schema_version"] != BOARD_BINDING_SCHEMA_VERSION
    ):
        raise SwarmError("The imported board uses an unsupported binding schema version.")
    if "workspace_id" in board and not _WORKSPACE_ID.fullmatch(
        str(board.get("workspace_id") or "").strip().lower()
    ):
        raise SwarmError("The imported board has an invalid workspace identity.")
    for key in ("version", "made_agents", "made_projects"):
        if key in board and not _is_a_count(board[key]):
            raise SwarmError(
                f"The imported board's {key} must be a non-negative whole number. "
                "Nothing was imported."
            )

    limits = {
        "agents": MOST_AGENTS,
        "projects": MOST_PROJECTS,
        "works_on": MOST_AGENTS * MOST_PROJECTS,
        "talks_to": MOST_AGENTS * (MOST_AGENTS - 1) // 2,
    }
    for key, maximum in limits.items():
        value = board.get(key, [])
        if not isinstance(value, list) or any(not isinstance(one, dict) for one in value):
            raise SwarmError(f"The imported board's {key} must be a list of objects.")
        if len(value) > maximum:
            raise SwarmError(
                f"The imported board has {len(value)} {key}; {maximum} is the most. "
                "Nothing was imported."
            )
    for project in board.get("projects", []):
        tasks = project.get("tasks", [])
        if not isinstance(tasks, list) or any(not isinstance(task, str) for task in tasks):
            raise SwarmError("Every imported project task must be a line of text.")
        if len(tasks) > MOST_TASKS:
            raise SwarmError(
                f"An imported project has {len(tasks)} tasks; {MOST_TASKS} is the most. "
                "Nothing was imported."
            )
    explicit_ids: set[str] = set()

    def explicit_id(item: dict[str, Any], kind: str) -> str:
        if "id" not in item:
            return ""
        value = item.get("id")
        if not isinstance(value, str) or not value or value.strip() != value:
            raise SwarmError(
                f"Every explicit imported {kind} id must be non-empty text. "
                "Nothing was imported."
            )
        if value in explicit_ids:
            raise SwarmError(
                f"The imported board uses id {value!r} more than once. Nothing was imported."
            )
        explicit_ids.add(value)
        return value

    def exact_place(item: dict[str, Any], kind: str) -> None:
        if "at" not in item:
            return
        at = item.get("at")
        if not isinstance(at, dict):
            raise SwarmError(f"Every imported {kind} position must be an object.")
        if set(at) - {"x", "y"}:
            raise SwarmError(
                f"An imported {kind} position contains unsupported fields. "
                "Nothing was imported."
            )
        for axis in ("x", "y"):
            value = at.get(axis)
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 4000:
                raise SwarmError(
                    f"Every imported {kind} {axis} position must be a whole number "
                    "from 0 to 4000. Nothing was imported."
                )

    agent_ids: set[str] = set()
    for agent in board.get("agents", []):
        if set(agent) - {
            "id", "name", "who", "job", "at", "colour", "icon",
            "bubble_colour", "profile_picture", "picture_zoom", "picture_hue",
            "filed_as",
        }:
            raise SwarmError(
                "An imported agent contains unsupported fields. Nothing was imported."
            )
        held_id = explicit_id(agent, "agent")
        if held_id:
            agent_ids.add(held_id)
        exact_place(agent, "agent")
        for key, maximum in {
            "name": LONGEST_NAME,
            "filed_as": LONGEST_NAME,
            "who": 64,
            "job": LONGEST_JOB,
        }.items():
            value = agent.get(key, "")
            if value is not None and not isinstance(value, str):
                raise SwarmError(f"Every imported agent {key} must be text.")
            measured = str(value or "")
            if len(measured if key == "job" else measured.strip()) > maximum:
                raise SwarmError(
                    f"An imported agent {key} is longer than {maximum} characters. "
                    "Nothing was imported."
                )
            if (
                isinstance(value, str)
                and key != "job"
                and " ".join(value.split()) != value
            ):
                raise SwarmError(
                    f"An imported agent {key} contains leading or repeated whitespace. "
                    "Nothing was imported."
                )
        for key in ("colour", "bubble_colour"):
            if key in agent and (
                not isinstance(agent[key], str)
                or not AN_AGENT_COLOUR.fullmatch(agent[key])
                or agent[key] != agent[key].lower()
            ):
                raise SwarmError(
                    f"An imported agent {key} must be a six-digit #RRGGBB colour. "
                    "Nothing was imported."
                )
        if "icon" in agent and (
            not isinstance(agent["icon"], str)
            or agent["icon"] not in AGENT_ICONS
        ):
            raise SwarmError("An imported agent icon is not supported. Nothing was imported.")
        picture = agent.get("profile_picture", "")
        if picture is not None and not isinstance(picture, str):
            raise SwarmError("Every imported agent profile picture must be text.")
        if len(str(picture or "").strip()) > LONGEST_PROFILE_PICTURE:
            raise SwarmError(
                f"An imported profile picture is larger than {LONGEST_PROFILE_PICTURE} "
                "characters. Nothing was imported."
            )
        if isinstance(picture, str) and picture.strip() != picture:
            raise SwarmError(
                "An imported profile picture has surrounding whitespace. Nothing was imported."
            )
        if picture and not AN_AGENT_PICTURE.fullmatch(picture.strip()):
            raise SwarmError("An imported profile picture is invalid. Nothing was imported.")
        for key, least, most in (
            ("picture_zoom", 100, 300), ("picture_hue", 0, 360)
        ):
            if key in agent:
                value = agent[key]
                if isinstance(value, bool) or not isinstance(value, int) or not least <= value <= most:
                    raise SwarmError(
                        f"An imported agent {key} must be a whole number from {least} to {most}. "
                        "Nothing was imported."
                    )
    project_ids: set[str] = set()
    for project in board.get("projects", []):
        if set(project) - {
            "id", "path", "name", "is_there", "tasks", "at",
            "approved_test_command_digest",
        }:
            raise SwarmError(
                "An imported project contains unsupported fields. Nothing was imported."
            )
        held_id = explicit_id(project, "project")
        if held_id:
            project_ids.add(held_id)
        exact_place(project, "project")
        path = project.get("path")
        if not isinstance(path, str) or not path or path.strip() != path:
            raise SwarmError(
                "Every imported project path must be non-empty text without surrounding whitespace. "
                "Nothing was imported."
            )
        approval = project.get("approved_test_command_digest", "")
        if not isinstance(approval, str) or (
            approval and not re.fullmatch(r"[0-9a-fA-F]{64}", approval)
        ):
            raise SwarmError(
                "An imported project command approval digest is invalid. Nothing was imported."
            )
        tasks = project.get("tasks", [])
        if not isinstance(tasks, list) or len(tasks) > MOST_TASKS or not all(
            isinstance(task, str) for task in tasks
        ):
            raise SwarmError(
                f"Every imported project may contain at most {MOST_TASKS} text jobs. "
                "Nothing was imported."
            )
        for task in tasks:
            if not task.strip():
                raise SwarmError(
                    "Every imported project task must contain non-whitespace text. "
                    "Nothing was imported."
                )
            if len(task) > LONGEST_TASK:
                raise SwarmError(
                    f"An imported project task is longer than {LONGEST_TASK} characters. "
                    "Nothing was imported."
                )
    seen_work: set[tuple[str, str]] = set()
    for link in board.get("works_on", []):
        if set(link) - {"agent", "project"}:
            raise SwarmError(
                "An imported work line contains unsupported fields. Nothing was imported."
            )
        agent = link.get("agent")
        project = link.get("project")
        if not isinstance(agent, str) or not isinstance(project, str):
            raise SwarmError("Every imported work line must name text ids.")
        pair = (agent, project)
        if agent not in agent_ids or project not in project_ids:
            raise SwarmError(
                "An imported work line points to an agent or project that is not on the board. "
                "Nothing was imported."
            )
        if pair in seen_work:
            raise SwarmError("The imported board contains a duplicate work line. Nothing was imported.")
        seen_work.add(pair)
    seen_talk: set[tuple[str, str]] = set()
    for link in board.get("talks_to", []):
        if set(link) - {"one", "other"}:
            raise SwarmError(
                "An imported talk line contains unsupported fields. Nothing was imported."
            )
        first = link.get("one")
        other = link.get("other")
        if not isinstance(first, str) or not isinstance(other, str):
            raise SwarmError("Every imported talk line must name text ids.")
        if first == other or first not in agent_ids or other not in agent_ids:
            raise SwarmError(
                "An imported talk line points to a missing agent or to itself. Nothing was imported."
            )
        pair = tuple(sorted((first, other)))
        if (first, other) != pair:
            raise SwarmError(
                "An imported talk line is not in canonical id order. Nothing was imported."
            )
        if pair in seen_talk:
            raise SwarmError("The imported board contains a duplicate talk line. Nothing was imported.")
        seen_talk.add(pair)


@_requires_board_qa_access
def keep_this_board(name: str, config: Any) -> dict[str, Any]:
    """Save the board as it stands now, under a name.

    What is saved is what is on the board this moment, not what was last set
    going. Somebody pressing save has just finished arranging it.
    """

    from .safety import ProjectTransactionLock

    where = where_the_kept_ones_live()
    where.mkdir(parents=True, exist_ok=True)
    with ProjectTransactionLock(where.parent).held(timeout_seconds=10):
        filed = _filed_under(name)
        saved_name = " ".join(str(name).split())
        live = load()
        # Save-as is a fork, not an alias. Two separately named boards may use
        # the same short agent/project ids, so copying the live workspace id
        # into both would make their pair chats indistinguishable. Saving the
        # board which is currently open back onto that same name is an update
        # and deliberately retains its identity and conversations.
        workspace_id = (
            live.workspace_id
            if live.active_saved_board == saved_name
            else _new_workspace_id()
        )
        snapshot = live.to_dict()
        snapshot["workspace_id"] = workspace_id
        held = {
            "name": saved_name,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "board": snapshot,
        }
        _portable_board_document(
            held["name"], held["saved_at"], read_it(held["board"])
        )
        # Written beside and moved into place, so a panel reading the list never
        # catches a board half written.
        beside = where / f".{filed}.{os.getpid()}.{time.time_ns()}.part"
        try:
            beside.write_text(json.dumps(held, indent=2) + "\n", encoding="utf-8")
            os.replace(beside, where / filed)
        finally:
            beside.unlink(missing_ok=True)
        return {"name": held["name"], "saved_at": held["saved_at"]}


@_requires_board_qa_access
def open_this_board(name: str, config: Any) -> Board:
    """Put a saved board back on the screen, as the one being worked on.

    The board it replaces is not lost by accident: it can be saved first, and
    the panel asks. What this will not do is quietly merge the two, which is how
    somebody ends up with a board holding both and belonging to neither.
    """

    from .swarm_runs import global_board_mutation

    with global_board_mutation(config):
        opened_as = " ".join(str(name).split())
        where = where_the_kept_ones_live() / _filed_under(opened_as)
        try:
            held = json.loads(where.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SwarmError(f"There is no saved board called {name}.") from exc
        board = held.get("board") if isinstance(held, dict) else None
        if not isinstance(board, dict):
            raise SwarmError(f"The board saved as {name} cannot be read.")
        checked = read_it(board)
        workspace_id = checked.workspace_id
        if not workspace_id:
            # Old local snapshots did not carry a workspace id. Bind one to
            # the exact saved-board file/name so reopening it is stable, while
            # remaining distinct from the legacy live board and every import.
            identity = f"{where.resolve(strict=False)}\0{opened_as.casefold()}"
            marked = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
            workspace_id = f"workspace-saved-{marked}"
        # The global authority is already held, so do the validated file write
        # directly instead of trying to acquire the non-reentrant lease twice.
        return _save_while_board_authority_is_held(dict(
            checked.to_dict(),
            version=None,
            active_saved_board=opened_as,
        ), config,
            allow_command_approval_changes=True,
            workspace_id_override=workspace_id,
        )


@_requires_board_qa_access
def forget_this_board(name: str, config: Any) -> None:
    """Throw away one saved board."""

    from .safety import ProjectTransactionLock
    from .swarm_runs import global_board_metadata_mutation

    library = where_the_kept_ones_live()
    library.mkdir(parents=True, exist_ok=True)
    with ProjectTransactionLock(library.parent).held(timeout_seconds=10):
        forgotten = " ".join(str(name).split())
        where = library / _filed_under(forgotten)
        try:
            where.unlink()
        except FileNotFoundError as exc:
            raise SwarmError(f"There is no saved board called {name}.") from exc
        except OSError as exc:
            raise SwarmError(f"The board saved as {name} could not be deleted: {exc}") from exc
    with global_board_metadata_mutation(config):
        board = load()
        if board.active_saved_board == forgotten:
            _save_while_board_authority_is_held(
                dict(board.to_dict(), active_saved_board=""), config
            )
