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

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from . import seats as seats_lab
from . import workflows as workflows_lab
from .config import (
    LoadedConfig,
    is_project_local_config_trusted,
    trust_project_local_config,
)
from .graphs import migrate_graph, validate_graph
from .models import HarnessError
from .safety import confined_path

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

# How a box gets its answers. A set prompt is the ordinary one: the job is
# written down once and the same words are used every time. A conversation is
# for the work nobody can write down in advance - the run stops there, you talk
# it through, and you say when to carry on.
WAYS_OF_ASKING: tuple[tuple[str, str, str], ...] = (
    (
        "set-prompt",
        "One set prompt",
        "The same instructions every time. Write them once and the run does not stop.",
    ),
    (
        "conversation",
        "A conversation you can carry on",
        "The run stops here and waits. You talk to it as long as you like, then say carry on.",
    ),
)
ASKING_NAMES = {key for key, _label, _means in WAYS_OF_ASKING}

# How the harness reaches a model. The first is the one most people have: a
# tool they are already signed in to. The last one never holds a key - only the
# name of the place a key is kept.
WAYS_IN: tuple[tuple[str, str, str], ...] = (
    (
        "seat",
        "A tool you are signed in to",
        "Claude or Copilot on this machine. Nothing to paste, nothing to keep secret.",
    ),
    (
        "on-this-machine",
        "A model running on this machine",
        "Ollama or anything that answers like it. Nothing leaves the machine.",
    ),
    (
        "with-a-key",
        "A service, with the key kept in an environment variable",
        "The harness is told the name of the variable, never the key itself.",
    ),
)
WAY_IN_NAMES = {key for key, _label, _means in WAYS_IN}
# What each way in is written as in the settings.
KIND_FOR_WAY_IN = {"on-this-machine": "ollama", "with-a-key": "openai-compatible"}
ROUTE_SHAPE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
KEY_NAME_SHAPE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
MOST_PROMPT_LETTERS = 4000

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
    signed_in: bool | None = None
    connection_state: str = "unknown"
    can_login: bool = False
    already_set_up: bool = False
    version: str = ""
    found_at: str = ""
    why_not: str = ""
    install_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        from .seats import safe_install_location

        return {
            "route": self.route,
            "label": self.label,
            "kind": self.kind,
            "ready": self.ready,
            "signed_in": self.signed_in,
            "connection_state": self.connection_state,
            "can_login": self.can_login,
            "already_set_up": self.already_set_up,
            "version": self.version,
            "found_at": "",
            "found_via": (
                safe_install_location(self.kind, self.found_at)
                if self.kind.endswith("-cli")
                else ("a configured service" if self.found_at else "")
            ),
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

    from . import local_models as local_lab

    look = seats_lab.look(config)
    from .providers.subscription_cli import connection_status

    candidates = [seat for seat in look.seats if seat.ready]
    checked = [
        connection_status(seat.kind, use_cache=True, probe=False)
        for seat in candidates
    ]
    status_by_kind = {
        seat.kind: status for seat, status in zip(candidates, checked)
    }
    members: list[Member] = []
    for seat in look.seats:
        connection = status_by_kind.get(seat.kind, {})
        authentication = str(connection.get("authentication") or "unknown")
        members.append(
            Member(
                route=seat.route,
                label=seat.label,
                kind=seat.kind,
                ready=bool(seat.ready),
                # Being installed is not being signed in. Only a provider's
                # own auth-status command can make this true; tools without a
                # safe status command stay unknown rather than guessed.
                signed_in=(True if authentication == "signed-in" else (
                    False if authentication == "signed-out" else None
                )),
                connection_state=str(connection.get("state") or (
                    "not-installed" if not seat.ready else "installed"
                )),
                can_login=bool(connection.get("can_login")),
                already_set_up=bool(seat.already_set_up),
                version=seat.version,
                found_at=seat.found_at,
                why_not=seat.why_not,
                install_hint=seat.install_hint,
            )
        )
    # And anything already set up in the settings that is not one of those
    # tools: a model on this machine, or a service somebody wired up. Without
    # this, a model you added yourself could never be given a job.
    known = {one.route for one in members}
    for route, held in (config.get("providers", {}) or {}).items():
        if route in known or not isinstance(held, dict):
            continue
        kind = str(held.get("kind") or "")
        members.append(
            Member(
                route=str(route),
                label=f"{route} ({held.get('model') or kind or 'a model of your own'})",
                kind=kind or "your own",
                ready=True,
                signed_in=not held.get("api_key_env"),
                connection_state="configured",
                already_set_up=True,
                version=str(held.get("model") or ""),
                found_at=str(held.get("endpoint") or ""),
                why_not="",
            )
        )
    ready = [one for one in members if one.ready]
    return {
        "members": [one.to_dict() for one in members],
        "settings_file": look.settings_file,
        "trusted": look.trusted,
        "how_many_ready": len(ready),
        "note": _how_it_looks(members),
        # And the models running on this machine, which are nobody's to approve.
        # Found rather than typed in: the harness has taken an Ollama address
        # for as long as it has had settings, and somebody with Ollama running
        # still had to know the port and the model name and write both into a
        # file by hand - a strange thing to ask for the one route that needs no
        # permission at all.
        "on_this_machine": [one.to_dict() for one in local_lab.look()],
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


@dataclass
class ItsOwnWayIn:
    """A model somebody set up themselves, rather than a tool already signed in.

    The key itself is never held here, and never asked for. What is kept is the
    name of the environment variable the key lives in, which is a name and not
    a secret. Anything else would put somebody's key in a file that travels.
    """

    route: str
    way_in: str
    model: str
    endpoint: str = ""
    key_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "way_in": self.way_in,
            "model": self.model,
            "endpoint": self.endpoint,
            "key_name": self.key_name,
        }

    def as_a_route(self) -> dict[str, Any]:
        """The same thing written the way the settings file keeps it."""

        written: dict[str, Any] = {
            "kind": KIND_FOR_WAY_IN.get(self.way_in, "openai-compatible"),
            "model": self.model,
        }
        if self.endpoint:
            written["endpoint"] = self.endpoint
        if self.key_name:
            written["api_key_env"] = self.key_name
        return written


def check_its_own_way_in(said: Any) -> ItsOwnWayIn:
    """Read what somebody typed into the new-model window, and refuse nonsense."""

    if not isinstance(said, dict):
        raise TeamError("Say how this one is reached.")
    way_in = str(said.get("way_in") or "").strip()
    if way_in not in WAY_IN_NAMES:
        raise TeamError(
            "Choose how it is reached: a tool you are signed in to, a model on this "
            "machine, or a service with its key in an environment variable."
        )
    route = str(said.get("route") or "").strip().lower()
    if not ROUTE_SHAPE.fullmatch(route):
        raise TeamError(
            "A short name for this model is lowercase letters, numbers, dashes and "
            "underscores, up to 40 of them. It is the name you will see on the box."
        )
    model = str(said.get("model") or "").strip()
    if not 1 <= len(model) <= 120:
        raise TeamError("Say which model to use, in up to 120 letters.")
    endpoint = str(said.get("endpoint") or "").strip()
    if endpoint and not endpoint.startswith(("http://", "https://")):
        raise TeamError("An address starts with http:// or https://.")
    if len(endpoint) > 300:
        raise TeamError("That address is too long.")
    key_name = str(said.get("key_name") or "").strip()
    if key_name and not KEY_NAME_SHAPE.fullmatch(key_name):
        raise TeamError(
            "Give the name of the environment variable the key is kept in, like "
            "OPENAI_API_KEY - capitals, numbers and underscores. Never the key itself."
        )
    if way_in == "with-a-key" and not key_name:
        raise TeamError(
            "Say which environment variable holds the key. The harness reads the key "
            "from there when it runs, so nothing secret is written down here."
        )
    if key_name and _looks_like_a_key(key_name):
        raise TeamError(
            "That looks like a key rather than the name of one. Put the key in an "
            "environment variable and give the name of that variable here."
        )
    return ItsOwnWayIn(
        route=route, way_in=way_in, model=model, endpoint=endpoint, key_name=key_name
    )


def _looks_like_a_key(said: str) -> bool:
    """A rough guess at somebody pasting the key where the name should go."""

    return len(said) > 40 or said.lower().startswith(("sk-", "sk_", "ghp_", "xox"))


def check_a_custom_member(said: Any) -> dict[str, Any]:
    """Everything one custom box holds, checked over."""

    if not isinstance(said, dict):
        raise TeamError("A box on the team is a set of settings, and this is not one.")
    label = str(said.get("label") or "").strip()
    if not 1 <= len(label) <= 80:
        raise TeamError("Give this box a name, in up to 80 letters.")
    job = str(said.get("job") or "").strip()
    if job not in JOB_NAMES:
        raise TeamError("Choose what this one does from the list of jobs.")
    asking = str(said.get("asking") or "set-prompt").strip()
    if asking not in ASKING_NAMES:
        raise TeamError(
            "Choose whether this one gets one set prompt or a conversation you can "
            "carry on."
        )
    prompt = str(said.get("prompt") or "")
    if len(prompt) > MOST_PROMPT_LETTERS:
        raise TeamError(
            f"That prompt is longer than {MOST_PROMPT_LETTERS} letters. Anything that "
            "long belongs in the project, with the prompt pointing at it."
        )
    if any(ord(letter) < 32 and letter not in "\t\n\r" for letter in prompt):
        raise TeamError("That prompt holds a control character.")
    if asking == "set-prompt" and not prompt.strip():
        raise TeamError(
            "A box with one set prompt needs the prompt written down. Say what it "
            "should do every time."
        )
    return {
        "label": label,
        "job": job,
        "asking": asking,
        "prompt": prompt.strip(),
        "route": str(said.get("route") or "").strip().lower(),
        "model": str(said.get("model") or "").strip()[:120],
    }


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
        settings = node.get("config")
        settings = settings if isinstance(settings, dict) else {}
        asking = str(settings.get("asking") or "set-prompt")
        if asking not in ASKING_NAMES:
            problems.append(
                f"{label} does not say whether it gets one set prompt or a conversation."
            )
        # An empty prompt is not a problem: it means "the usual instructions for
        # that job", which is what every box on the ready-made team uses. The
        # window that writes a box of your own asks for one, because somebody
        # typing there has a particular thing in mind.
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


def add_its_own_way_in(config: LoadedConfig, said: Any) -> dict[str, Any]:
    """Write one model of your own into your settings, and leave the rest alone.

    Two rules, both the same ones the seat setup keeps:

      - It adds. Somebody else's settings are not this tool's to throw away, so
        every other route, and the one used by default, are left exactly as they
        were.
      - It never holds a key. What goes in the file is the name of the
        environment variable the key lives in, and the harness reads the key
        from there when it runs.
    """

    wanted = check_its_own_way_in(said)
    here = {one["route"] for one in who_is_here(config)["members"]}
    if wanted.route in here:
        raise TeamError(
            f"{wanted.route} is already the name of an assistant found on this "
            "machine. Give this one a different name."
        )
    local = confined_path(
        config.project_root, ".harness/config.local.json",
        allow_missing=True, allow_control=True,
    )
    settings: dict[str, Any] = {}
    was_there = local.is_file()
    was_trusted = was_there and is_project_local_config_trusted(config.project_root, local)
    if was_there:
        try:
            settings = json.loads(local.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TeamError(
                f"{local.name} is there and cannot be read: {exc}. Fix or move that "
                "file first; nothing was changed."
            ) from exc
        if not isinstance(settings, dict):
            raise TeamError(f"{local.name} does not hold settings, so it was left alone")
    routes = settings.get("providers")
    routes = dict(routes) if isinstance(routes, dict) else {}
    replaced = wanted.route in routes
    routes[wanted.route] = wanted.as_a_route()
    settings["providers"] = routes
    body = json.dumps(settings, indent=2) + "\n"
    local.parent.mkdir(parents=True, exist_ok=True)
    # Written beside and moved into place, so a machine turned off in the middle
    # cannot leave half a settings file behind.
    beside = local.with_name(f"{local.name}.{os.getpid()}.part")
    beside.write_text(body, encoding="utf-8")
    os.replace(beside, local)

    trusted = False
    needs_your_say = False
    note = ""
    if not was_there or was_trusted:
        try:
            trust_project_local_config(config.project_root, local)
            trusted = True
            note = f"{wanted.route} is set up and ready to give a job to."
        except HarnessError as exc:
            note = f"{wanted.route} was written, and trusting the file did not work: {exc}"
    else:
        # The same choice the seat setup puts in front of somebody: the file was
        # already here, nobody has said it is theirs, and trusting it lets
        # everything in it act - not only what was just written.
        needs_your_say = True
        note = (
            f"{wanted.route} was written into {local.name}. That file was already here "
            "and nobody has said it is theirs, so nothing in it acts yet."
        )
    return {
        "route": wanted.route,
        "way_in": wanted.way_in,
        "written_over": replaced,
        "settings_file": local.as_posix(),
        "contents": body,
        "trusted": trusted,
        "needs_your_say": needs_your_say,
        "risky_parts": seats_lab.what_makes_it_risky(settings),
        "mark": seats_lab.mark_of(body),
        "note": note,
    }


def everything(config: LoadedConfig) -> dict[str, Any]:
    """What the team view needs to draw itself, in one answer."""

    who = who_is_here(config)
    starting = a_starting_team(members=who["members"])
    return {
        "who": who,
        "jobs": [{"job": key, "label": label, "means": means} for key, label, means in JOBS],
        "ways_in": [
            {"way_in": key, "label": label, "means": means} for key, label, means in WAYS_IN
        ],
        "ways_of_asking": [
            {"asking": key, "label": label, "means": means}
            for key, label, means in WAYS_OF_ASKING
        ],
        "most_prompt_letters": MOST_PROMPT_LETTERS,
        "teams": teams(config),
        "starting_team": starting,
        "starting_plain": in_plain_words(starting),
        "most_members": MOST_MEMBERS,
    }
