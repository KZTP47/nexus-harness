"""Your team: which assistants are on this machine, and how they work together.

Most organisations have seats rather than keys. Somebody signs in to Claude
once, signs in to Copilot once, and both are then usable from the command line
without a key anywhere. The harness can drive either of them - what it cannot
do is guess who you have, what job each one should do, or who should check
whose work.

This is that part, in one place:

  - **Who is here.** Look on this machine for each assistant, ask it its
    version, and say plainly whether it is signed in and ready.
  - **What they do.** A team is a small picture: a box per assistant with a job
    written on it, and an arrow for each hand-over. Claude plans, Copilot
    writes, Claude reads it back. Or whatever you draw instead.
  - **What they say to each other.** Along an arrow the work moves. On the
    board beside it they leave notes - "the parser caches by file name, watch
    out" - which is the part that makes two assistants better than one used
    twice.

The team picture is an ordinary saved workflow, so everything that already runs
a workflow runs a team. Nothing here is a second way of doing the same thing.

Two rules, the same two the seat setup keeps:

  - A job is only ever given to an assistant that was really found. A team that
    names a tool nobody has is a run that fails later for a reason nobody can
    see now.
  - Nothing is set up quietly. Setting up writes routes into your own settings
    file and says so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import seats as seats_lab
from . import workflows as workflows_lab
from .config import LoadedConfig
from .graphs import migrate_graph, validate_graph
from .models import HarnessError

# The jobs an assistant can be given, in the words the panel shows. The key is
# the node type the rest of the harness already understands, so a team is a
# workflow and not a thing of its own.
JOBS: tuple[tuple[str, str, str], ...] = (
    (
        "planner",
        "Plans the work",
        "Reads the task and writes down what has to happen, before anybody changes a file.",
    ),
    (
        "coder",
        "Writes the code",
        "Takes the plan and makes the change.",
    ),
    (
        "evaluator",
        "Reads the work back",
        "Looks at what was written and says whether it really does what was asked.",
    ),
    (
        "merge",
        "Puts several answers together",
        "Where two assistants answered the same question, this decides what to keep.",
    ),
)
JOB_NAMES = {key for key, _label, _means in JOBS}

# How many boxes one team may hold. Past this it stops being a picture somebody
# can read, which is the whole point of it.
MOST_MEMBERS = 12
NAME_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$")


class TeamError(HarnessError):
    """Something about a team a person can put right."""


# ---------------------------------------------------------------------------
# Who is on this machine
# ---------------------------------------------------------------------------


@dataclass
class Member:
    """One assistant, and whether this machine can really use it."""

    route: str
    label: str
    kind: str
    ready: bool = False
    signed_in: bool = False
    already_set_up: bool = False
    version: str = ""
    found_at: str = ""
    why_not: str = ""
    install_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "label": self.label,
            "kind": self.kind,
            "ready": self.ready,
            "signed_in": self.signed_in,
            "already_set_up": self.already_set_up,
            "version": self.version,
            "found_at": self.found_at,
            "why_not": self.why_not,
            "install_hint": self.install_hint,
        }


def who_is_here(config: LoadedConfig) -> dict[str, Any]:
    """Look on this machine and say who could work here.

    The looking is the seat setup's own, so this and the seats view can never
    disagree about what is installed. What is added here is the answer to the
    question the team view actually asks: can I give this one a job right now,
    and if not, what is in the way.
    """

    look = seats_lab.look(config)
    members: list[Member] = []
    for seat in look.seats:
        members.append(
            Member(
                route=seat.route,
                label=seat.label,
                kind=seat.kind,
                ready=bool(seat.ready),
                # Being installed is not being signed in. The tool answers with
                # its version either way, so this says what was really shown.
                signed_in=bool(seat.ready and seat.version),
                already_set_up=bool(seat.already_set_up),
                version=seat.version,
                found_at=seat.found_at,
                why_not=seat.why_not,
                install_hint=seat.install_hint,
            )
        )
    ready = [one for one in members if one.ready]
    return {
        "members": [one.to_dict() for one in members],
        "settings_file": look.settings_file,
        "trusted": look.trusted,
        "how_many_ready": len(ready),
        "note": _how_it_looks(members),
    }


def _how_it_looks(members: list[Member]) -> str:
    ready = [one for one in members if one.ready]
    if len(ready) >= 2:
        names = " and ".join(one.label for one in ready[:2])
        return f"{names} are both here, so they can work on the same job and check each other."
    if len(ready) == 1:
        return (
            f"{ready[0].label} is here. One assistant can do the whole job, but nobody "
            "reads its work back except you. A second one is worth having."
        )
    return "No assistant was found on this machine yet. Set one up and it turns up here."


# ---------------------------------------------------------------------------
# A team as a picture
# ---------------------------------------------------------------------------


def a_starting_team(config: LoadedConfig | None = None, members: list[dict] | None = None) -> dict[str, Any]:
    """The team to start from: one assistant plans and reads back, another writes.

    Two assistants that were trained apart tend not to share a blind spot, so
    the one that reads the work back is deliberately not the one that wrote it.
    Where only one is here, it does every job and the picture says so.
    """

    here = list(members or [])
    if config is not None and not here:
        here = [one for one in who_is_here(config)["members"] if one.get("ready")]
    ready = [one for one in here if one.get("ready")] or here
    first = str(ready[0]["route"]) if ready else "claude"
    second = str(ready[1]["route"]) if len(ready) > 1 else first
    first_label = str(ready[0]["label"]) if ready else "Claude"
    second_label = str(ready[1]["label"]) if len(ready) > 1 else first_label
    return {
        "schema_version": 2,
        "name": "Your team",
        "entry": "start",
        "nodes": [
            {"id": "start", "type": "start", "label": "The task"},
            {
                "id": "planner",
                "type": "planner",
                "label": f"{first_label} plans it",
                "config": {"provider_route": first},
            },
            {
                "id": "coder",
                "type": "coder",
                "label": f"{second_label} writes it",
                "config": {"provider_route": second},
            },
            {
                "id": "reviewer",
                "type": "evaluator",
                "label": f"{first_label} reads it back",
                "config": {"provider_route": first},
            },
            {"id": "end", "type": "end", "label": "Done"},
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "planner", "variables": ["task"]},
            {"id": "e2", "source": "planner", "target": "coder", "variables": ["plan"]},
            {"id": "e3", "source": "coder", "target": "reviewer", "variables": ["candidate"]},
            {"id": "e4", "source": "reviewer", "target": "end", "variables": ["review"]},
            {
                # Where the work goes when the review says no. It is allowed to
                # go round a few times and then stop, because a team that can
                # argue forever is a team that never finishes.
                "id": "e5",
                "source": "reviewer",
                "target": "coder",
                "condition": "review_passed != true",
                "variables": ["review"],
                "loop": {"max_iterations": 3},
            },
        ],
    }


@dataclass
class HandOver:
    """One arrow, said out loud."""

    who: str
    to_whom: str
    what: str
    only_when: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"who": self.who, "to_whom": self.to_whom, "what": self.what, "only_when": self.only_when}


def in_plain_words(graph: dict[str, Any]) -> dict[str, Any]:
    """A team picture read out as sentences.

    The picture is for looking at. This is the same thing for somebody who
    would rather read it, or cannot see it at all.
    """

    if not isinstance(graph, dict):
        return {"members": [], "hand_overs": [], "note": "This is not a team."}
    nodes = [node for node in (graph.get("nodes") or []) if isinstance(node, dict)]
    edges = [edge for edge in (graph.get("edges") or []) if isinstance(edge, dict)]
    label_of = {str(node.get("id")): str(node.get("label") or node.get("id")) for node in nodes}
    said_jobs = {key: label for key, label, _means in JOBS}

    members = []
    for node in nodes:
        if node.get("type") not in JOB_NAMES:
            continue
        settings = node.get("config")
        settings = settings if isinstance(settings, dict) else {}
        members.append({
            "id": str(node.get("id")),
            "label": str(node.get("label") or node.get("id")),
            "job": str(node.get("type")),
            "job_said": said_jobs.get(str(node.get("type")), str(node.get("type"))),
            "route": str(settings.get("provider_route") or ""),
        })

    hand_overs = []
    for edge in edges:
        source, target = str(edge.get("source")), str(edge.get("target"))
        if source not in label_of or target not in label_of:
            continue
        carried = [str(one) for one in (edge.get("variables") or [])]
        hand_overs.append(
            HandOver(
                who=label_of[source],
                to_whom=label_of[target],
                what=", ".join(carried) if carried else "the work so far",
                only_when=str(edge.get("condition") or ""),
            ).to_dict()
        )

    routes = {one["route"] for one in members if one["route"]}
    if len(routes) > 1:
        note = (
            f"{len(members)} on this team, using {len(routes)} different assistants. "
            "The one that reads the work back is not the one that wrote it, which is "
            "the point of having two."
        )
    elif members:
        note = (
            f"{len(members)} on this team, all using the same assistant. It will do the "
            "work and then read back its own work, which catches less."
        )
    else:
        note = "Nobody has a job on this team yet."
    return {"members": members, "hand_overs": hand_overs, "note": note}


def check_it(
    config: LoadedConfig, graph: Any, here: dict[str, dict] | None = None
) -> list[str]:
    """Everything wrong with a team, said in words somebody can act on.

    Hand in `here` when checking several teams at once. Finding out who is on
    this machine means running each assistant's own command line tool, and a
    tool waiting on a sign-in can sit there for the best part of a minute.
    Doing that once for a list of ten teams rather than ten times is the
    difference between a view that opens and one that hangs.
    """

    problems: list[str] = []
    if not isinstance(graph, dict):
        return ["A team is a picture holding boxes and arrows, and this is not one."]
    nodes = [node for node in (graph.get("nodes") or []) if isinstance(node, dict)]
    with_jobs = [node for node in nodes if node.get("type") in JOB_NAMES]
    if len(with_jobs) > MOST_MEMBERS:
        problems.append(
            f"There are {len(with_jobs)} on this team. Past {MOST_MEMBERS} it stops being a "
            "picture anybody can read."
        )
    if here is None:
        here = {one["route"]: one for one in who_is_here(config)["members"]}
    for node in with_jobs:
        label = str(node.get("label") or node.get("id"))
        settings = node.get("config")
        settings = settings if isinstance(settings, dict) else {}
        route = str(settings.get("provider_route") or "")
        if not route:
            problems.append(f"{label} has a job but nobody doing it. Choose who it is.")
            continue
        known = here.get(route)
        if known is None:
            problems.append(
                f"{label} is set to {route}, which is not an assistant this machine has. "
                "Choose one from the list."
            )
        elif not known.get("ready"):
            problems.append(
                f"{label} is set to {known.get('label') or route}, which is not ready yet: "
                f"{known.get('why_not') or 'it was not found on this machine'}."
            )
    # And everything the harness itself would refuse, said as it says it. A
    # team that cannot run is not a team.
    try:
        for issue in validate_graph(migrate_graph(graph)):
            problems.append(f"{issue.path}: {issue.message}")
    except HarnessError as exc:
        problems.append(str(exc))
    return problems


# ---------------------------------------------------------------------------
# Keeping them
# ---------------------------------------------------------------------------


def teams(config: LoadedConfig) -> list[dict[str, Any]]:
    """Every saved team, and what is wrong with each.

    A team saved on a machine with two assistants can be opened on a machine
    with one. The picture is still well formed; the team still cannot run. The
    list said it would, which is the sort of disagreement that wastes an
    afternoon.
    """

    listed = []
    # Looked for once, not once per team.
    here = {one["route"]: one for one in who_is_here(config)["members"]}
    for saved in workflows_lab.listed(config):
        value = saved.to_dict()
        problems = check_it(config, saved.graph, here)
        if problems:
            value["valid"] = False
            value["issues"] = list(dict.fromkeys([*value.get("issues", []), *problems]))[:5]
        listed.append(value)
    return listed


def load_team(config: LoadedConfig, name: str) -> dict[str, Any]:
    saved = workflows_lab.load(config, name)
    value = saved.to_dict(include_graph=True)
    value["plain"] = in_plain_words(saved.graph)
    return value


def save_team(config: LoadedConfig, name: str, graph: Any, *, was: str = "") -> dict[str, Any]:
    """Write a team down, refusing one that could not really run.

    Hand in `was` when changing a team that already exists and its name has
    changed, so the old one is moved rather than left behind under the old
    name while a second appears under the new one.
    """

    if not NAME_SHAPE.fullmatch(str(name or "").strip()):
        raise TeamError(
            "A team name is letters, numbers, spaces, dashes and underscores, up to 64 of them."
        )
    problems = check_it(config, graph)
    if problems:
        raise TeamError("This team cannot be saved yet: " + " ".join(problems[:3]))
    wanted = str(name).strip()
    was = str(was or "").strip()
    if was and was.lower() != wanted.lower():
        # Changing the name of one team onto the name of another used to write
        # over that other team and then take this one away, so two teams became
        # one and nobody was told.
        already = {one["name"].lower() for one in teams(config)}
        if wanted.lower() in already:
            raise TeamError(
                f"There is already a team called {wanted}. Give this one a different "
                "name, or open that one and change it there."
            )
    # A name that differs only in capital letters is the same file, and this is
    # the same team moving into it.
    saved = workflows_lab.save(
        config, wanted, graph, taking_over=bool(was and was.lower() == wanted.lower())
    )
    if was and was.lower() != saved.name.lower():
        # The name changed, so this is one team moving, not two teams existing.
        try:
            workflows_lab.delete(config, was)
        except HarnessError:
            pass
    value = saved.to_dict(include_graph=True)
    value["plain"] = in_plain_words(saved.graph)
    return value


def remove_team(config: LoadedConfig, name: str) -> str:
    return workflows_lab.delete(config, name)


def everything(config: LoadedConfig) -> dict[str, Any]:
    """What the team view needs to draw itself, in one answer."""

    who = who_is_here(config)
    starting = a_starting_team(members=who["members"])
    return {
        "who": who,
        "jobs": [{"job": key, "label": label, "means": means} for key, label, means in JOBS],
        "teams": teams(config),
        "starting_team": starting,
        "starting_plain": in_plain_words(starting),
        "most_members": MOST_MEMBERS,
    }
