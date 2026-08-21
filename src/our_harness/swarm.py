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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import HarnessError

# The most of each, so a board stays something a person can look at and a file
# stays something a machine can read quickly.
MOST_AGENTS = 24
MOST_PROJECTS = 12
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
LONGEST_JOB = 300
LONGEST_TASK = 300

# A name for an agent, which is also the name its conversation is filed under.
# Letters, numbers, spaces, dashes and underscores: the same shape the chat
# already allows, because that is where the name ends up.
A_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,59}$")


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "who": self.who,
            "job": self.job,
            "at": dict(self.at),
            # What its conversation is filed under. Worked out from the name
            # rather than stored, so renaming an agent renames its file too and
            # nothing is left behind pointing at the old one.
            "filed_as": filed_as(self.name),
        }


@dataclass
class OneProject:
    """One project folder on the board, and the jobs wanted in it."""

    id: str
    path: str
    tasks: list[str] = field(default_factory=list)
    at: dict[str, int] = field(default_factory=lambda: {"x": 40, "y": 320})

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
        }


@dataclass
class Board:
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "version": self.version,
            "made_agents": self.made_agents,
            "made_projects": self.made_projects,
            "agents": [one.to_dict() for one in self.agents],
            "projects": [one.to_dict() for one in self.projects],
            "works_on": [dict(one) for one in self.works_on],
            "talks_to": [dict(one) for one in self.talks_to],
        }


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
    agents: list[Agent] = []
    seen: set[str] = set()
    made_agents = _how_many_ever(
        "agent", said.get("made_agents"), said.get("agents"), made_agents)
    made_projects = _how_many_ever(
        "project", said.get("made_projects"), said.get("projects"), made_projects)
    for one in _a_list(said.get("agents"))[:MOST_AGENTS]:
        name = _some_words(one.get("name"), LONGEST_NAME)
        if not name:
            raise SwarmError("Every agent needs a name")
        if not A_NAME.match(name):
            raise SwarmError(
                f"{name!r} is not a name an agent can have. Names hold letters, "
                "numbers, spaces, dots, dashes and underscores."
            )
        if filed_as(name).lower() in {filed_as(held.name).lower() for held in agents}:
            raise SwarmError(
                f"There is already an agent called {name}. Two with one name "
                "would share one conversation and each would read the other's "
                "half of it."
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
            who=_some_words(one.get("who"), 64),
            job=_some_words(one.get("job"), LONGEST_JOB),
            at=_a_place(one.get("at"), across, down),
        ))
    projects: list[OneProject] = []
    for one in _a_list(said.get("projects"))[:MOST_PROJECTS]:
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
        written = one.get("tasks")
        written = written if isinstance(written, list) else []
        tasks = [
            _some_words(task, LONGEST_TASK)
            for task in written[:MOST_TASKS]
            if isinstance(task, str)
        ]
        across, down = _somewhere_free(len(projects), 320)
        projects.append(OneProject(
            id=held_id,
            path=str(where),
            tasks=[task for task in tasks if task],
            at=_a_place(one.get("at"), across, down),
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
    return Board(
        version=_how_many_times(said.get("version")),
        made_agents=made_agents,
        made_projects=made_projects,
        agents=agents,
        projects=projects,
        works_on=works_on,
        talks_to=talks_to,
    )


def _a_list(said: Any) -> list[dict[str, Any]]:
    """The objects in a list, and nothing else that happens to be in it."""

    return [one for one in said if isinstance(one, dict)] if isinstance(said, list) else []


def load() -> Board:
    try:
        said = json.loads(where_it_lives().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return Board()
    try:
        return read_it(said)
    except SwarmError:
        # One nobody can read is one that starts empty, rather than a panel
        # that will not open at all.
        return Board()


def save(said: Any) -> Board:
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


def how_it_stands(config) -> dict[str, Any]:
    """The board, and everything the panel needs to draw and judge it."""

    from . import chat as chat_lab
    from . import projects as projects_lab

    board = load()
    # The one route with no name of its own - "the one this project uses" - is
    # left out here. A board spans projects, so "this project" means nothing on
    # it; and its name being empty is the same as an agent having chosen
    # nobody, so an agent nobody had set up read as ready and pointed at
    # whatever that project happened to use. Somebody with no named routes is
    # sent to Your team, where one press makes them.
    can_talk = [one for one in chat_lab.who_can_talk(config) if one.get("route")]
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
        # What happened the last time this route was asked anything. Not the
        # same as not being ready - this one is still tried - so that somebody
        # knows before they type instead of after.
        held["trouble_last_time"] = (known or {}).get("trouble_last_time", "")
        # How much has been said to this one, so the list of chats can be drawn
        # without asking after every conversation one at a time. It is read
        # rather than counted from anything kept in memory: a chat that was had
        # yesterday is still a chat.
        held.update(_how_much_was_said_to(config, one.who, filed_as(one.name)))
        agents.append(held)
    return {
        "board": dict(board.to_dict(), agents=agents),
        # The boards somebody saved under a name. Sent with the board itself, so
        # opening the panel shows what is kept without pressing anything - and
        # so anything that changes the list has the new one in its answer.
        "kept": every_kept_board(),
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
        }


@dataclass
class OneTurn:
    """One agent, one project, one round of it."""

    agent: str
    name: str
    project: str
    where: str
    round: str
    state: str = "waiting"     # waiting, asking, done, went wrong, not done
    said: str = ""
    why_not: str = ""
    milliseconds: int = 0
    # The agents whose answers this one was shown before it was asked. Empty on
    # the first round, because that is the point of the first round.
    shown: list[str] = field(default_factory=list)

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
            "state": self.state,
            "letters": len(self.said),
            "why_not": self.why_not,
            "milliseconds": self.milliseconds,
            "shown": list(self.shown),
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

    def to_dict(self) -> dict[str, Any]:
        return {
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
    return (
        f"You are {agent['name']}, working on {project['name']}, "
        f"which is the folder at {project['path']}."
        f"{what_it_is_for}\n\n"
        f"The jobs wanted there:\n{jobs}\n\n"
        "Say how you would do them, shortest way first, and say what you would "
        "need to look at before starting. You cannot read the files or run "
        "anything from here, so say what you would do rather than doing it."
    )


def what_the_others_said(
    agent: dict[str, Any], project: dict[str, Any], notes: list[tuple[str, str]]
) -> str:
    """What one agent is shown of the others, second time round."""

    said = "\n\n".join(f"{name} said:\n{text}" for name, text in notes)
    return (
        f"Here is what the others working on {project['name']} said. You are "
        "being shown these because somebody said you two may talk.\n\n"
        f"{said}\n\n"
        "Now say your own answer again, taking theirs into account. Say plainly "
        "where you disagree with them and why. Do not agree with something only "
        "because somebody else said it."
    )


class Running:
    """One run of the board at a time.

    One at a time because these are real assistants working on real folders,
    and two runs of the same board would have every agent doing its job twice
    while reading half of somebody else's.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
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

    def how_it_is_going(self) -> dict[str, Any] | None:
        with self._lock:
            return self._doing.to_dict() if self._doing else None

    def what_they_said(self) -> dict[str, Any]:
        """What was passed from one agent to another, whole.

        The run that is going, if one is, so it can be read as it happens; and
        otherwise the last one, read back off the disk so it is still there
        after the panel has been closed and opened again.
        """

        with self._lock:
            doing = self._doing
        if doing is not None:
            return {
                "note": doing.note,
                "going": doing.going,
                "dropped": doing.dropped,
                "most": MOST_NOTES,
                "notes": [one.to_dict() for one in doing.notes],
            }
        return dict(read_what_they_said(), going=False, most=MOST_NOTES)

    def stop(self) -> str:
        with self._lock:
            if not (self._doing and self._doing.going):
                return "Nothing is going, so there is nothing to stop."
            self._stop = True
            self._doing.note = "Stopping after the turn that is going now."
        return (
            "Stopping. The turn already asked for will finish, because there is "
            "no way to un-ask it, and nothing after it will be asked."
        )

    def start(self, config) -> dict[str, Any]:
        said = how_it_stands(config)
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
            self._stop = False
            self._doing = Doing(
                note="Asking each of them on their own first.", turns=turns)
            doing = self._doing

        def work() -> None:
            try:
                self._do_it(config, said, doing)
            except BaseException as exc:  # noqa: BLE001 - nothing may leave it going
                doing.note = f"This stopped in a way nobody expected: {exc}"
            finally:
                # However this ends, the run stops being one that is going. One
                # that says it is still going refuses every later press, and the
                # only way out would be restarting the panel.
                doing.going = False

        thread = threading.Thread(target=work, name="the-board", daemon=True)
        self._thread = thread
        thread.start()
        return doing.to_dict()

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
                    ))
        return first + second

    def _do_it(self, config, said: dict[str, Any], doing: Doing) -> None:
        from . import chat as chat_lab

        board = said["board"]
        agents = {one["id"]: one for one in board["agents"]}
        projects = {one["id"]: one for one in board["projects"]}
        # What each agent said about each project, so the second round can be
        # shown only the notes that agent is allowed to see.
        heard: dict[tuple[str, str], str] = {}
        for turn in doing.turns:
            with self._lock:
                stopping = self._stop
            if stopping:
                turn.state = "not done"
                turn.why_not = "Stopped before this one was asked."
                doing.stopped = True
                continue
            agent = agents[turn.agent]
            project = projects[turn.project]
            if turn.round == ON_ITS_OWN:
                asking = what_to_ask(agent, project)
            else:
                notes = [
                    (agents[held]["name"], text)
                    for (held, where), text in heard.items()
                    if where == turn.project
                    and held != turn.agent
                    and may_they_talk(board, turn.agent, held)
                ]
                if not notes:
                    turn.state = "not done"
                    turn.why_not = "Nobody it may talk to had anything to show it."
                    continue
                asking = what_the_others_said(agent, project, notes)
                # Written down as it is passed, so somebody watching can read
                # what each agent was actually given rather than take it on
                # trust that the right thing was shown to the right one.
                turn.shown = [name for name, _text in notes]
                for (held, where), text in heard.items():
                    if (where != turn.project or held == turn.agent
                            or not may_they_talk(board, turn.agent, held)):
                        continue
                    doing.notes.append(OneNote(
                        said_by=held,
                        said_by_name=agents[held]["name"],
                        shown_to=turn.agent,
                        shown_to_name=agent["name"],
                        project=turn.project,
                        where=project["name"],
                        text=text[:LONGEST_NOTE],
                        at=_the_time_now(),
                    ))
                    # The oldest fall off the end rather than the newest never
                    # being written: what somebody reads this for is what just
                    # happened. How many were dropped is said out loud below.
                    if len(doing.notes) > MOST_NOTES:
                        doing.dropped += len(doing.notes) - MOST_NOTES
                        del doing.notes[:len(doing.notes) - MOST_NOTES]
            turn.state = "asking"
            doing.note = f"Asking {turn.name} about {turn.where}, {turn.round}."
            started = time.monotonic()
            try:
                answered = chat_lab.say(
                    config, agent["who"], asking, filed_as=filed_as(agent["name"]))
            except HarnessError as exc:
                turn.state = "went wrong"
                turn.why_not = str(exc)
            else:
                turn.state = "done"
                turn.said = str(answered.get("answer", {}).get("text") or "")
                heard[(turn.agent, turn.project)] = turn.said
            turn.milliseconds = int((time.monotonic() - started) * 1000)
        doing.note = _how_it_went(doing)
        _keep_what_they_said(doing)


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

# How many a person can keep. Far more than anybody has, low enough that this
# cannot quietly become somewhere a folder fills up.
MOST_KEPT_BOARDS = 60
# How long a name may be, so it fits on a row and in a file name.
LONGEST_BOARD_NAME = 48


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


def every_kept_board() -> list[dict[str, Any]]:
    """Every board somebody saved, newest first."""

    where = where_the_kept_ones_live()
    found: list[dict[str, Any]] = []
    try:
        files = sorted(where.glob("*.json"))
    except OSError:
        return []
    for one in files:
        try:
            held = json.loads(one.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A board that cannot be read is one that is not offered. It is not
            # worth stopping the list of all the others over.
            continue
        if not isinstance(held, dict) or not isinstance(held.get("name"), str):
            continue
        board = held.get("board") if isinstance(held.get("board"), dict) else {}
        found.append({
            "name": held["name"],
            "saved_at": str(held.get("saved_at") or ""),
            "agents": len(board.get("agents") or []),
            "projects": len(board.get("projects") or []),
        })
    return sorted(found, key=lambda one: one["saved_at"], reverse=True)


def keep_this_board(name: str) -> dict[str, Any]:
    """Save the board as it stands now, under a name.

    What is saved is what is on the board this moment, not what was last set
    going. Somebody pressing save has just finished arranging it.
    """

    filed = _filed_under(name)
    where = where_the_kept_ones_live()
    where.mkdir(parents=True, exist_ok=True)
    already = every_kept_board()
    if len(already) >= MOST_KEPT_BOARDS and not (where / filed).is_file():
        raise SwarmError(
            f"There are already {MOST_KEPT_BOARDS} saved boards, which is the most. "
            "Delete one you no longer want first."
        )
    held = {
        "name": " ".join(str(name).split()),
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "board": load().to_dict(),
    }
    # Written beside and moved into place, so a panel reading the list never
    # catches a board half written.
    beside = where / f"{filed}.{os.getpid()}.part"
    beside.write_text(json.dumps(held, indent=2) + "\n", encoding="utf-8")
    os.replace(beside, where / filed)
    return {"name": held["name"], "saved_at": held["saved_at"]}


def open_this_board(name: str) -> Board:
    """Put a saved board back on the screen, as the one being worked on.

    The board it replaces is not lost by accident: it can be saved first, and
    the panel asks. What this will not do is quietly merge the two, which is how
    somebody ends up with a board holding both and belonging to neither.
    """

    where = where_the_kept_ones_live() / _filed_under(name)
    try:
        held = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SwarmError(f"There is no saved board called {name}.") from exc
    board = held.get("board") if isinstance(held, dict) else None
    if not isinstance(board, dict):
        raise SwarmError(f"The board saved as {name} cannot be read.")
    # Through the same door as any other save, so a board saved by an older
    # version is checked and tidied the same way rather than trusted.
    return save(dict(board, version=None))


def forget_this_board(name: str) -> None:
    """Throw away one saved board."""

    where = where_the_kept_ones_live() / _filed_under(name)
    try:
        where.unlink()
    except FileNotFoundError as exc:
        raise SwarmError(f"There is no saved board called {name}.") from exc
    except OSError as exc:
        raise SwarmError(f"The board saved as {name} could not be deleted: {exc}") from exc
