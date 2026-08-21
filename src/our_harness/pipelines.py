"""Pipelines: many jobs, wired together, with gates between them.

A check suite answers "does this project work?". A pipeline answers a bigger
question: run these suites side by side, scan the code for credentials, only go
on if enough of that passed, then run the unit tests, and try the flaky one
again if it fails.

Every part of that already exists in the harness. What was missing was a way to
say how the parts fit together, and a picture of it while it runs.

The pieces
  - A pipeline is nodes and arrows, saved as ordinary JSON in .harness/pipelines.
  - An arrow means "after". A node runs once everything pointing at it is done.
  - A gate looks at what came before it and decides whether the work goes on.
  - Any node can be told to try again a number of times before it gives up.

What it will not do
  - Run anything that is not one of the kinds below. There is no "run this
    shell line" node, because a saved pipeline would then be a saved way of
    running anything on the machine that opened it.
  - Reach outside the project. Every path is confined the same way the rest of
    the harness confines them.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import LoadedConfig
from .models import HarnessError
from .safety import confined_path, take_the_file_away

WAITING = "waiting"
RUNNING = "running"
PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"

# A pipeline is a picture as much as a program, so a name has to survive being
# a file name on any machine.
NAME_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$")
WHERE_THEY_LIVE = ".harness/pipelines"
# Where a model's writing goes. Deliberately somewhere no test runner looks:
# what a model wrote is a draft for a person to read, not something the next
# step of the same run should be able to execute.
DRAFTS = ".harness/pipelines/drafts"
# Bounds. A pipeline is drawn by hand, so these are far above anything anyone
# would draw and far below anything that would hurt the machine.
MOST_NODES = 200
MOST_EDGES = 400
MOST_TRIES = 5
# The longest a single step may be given. Four hours is far past anything
# sensible and still a number rather than for ever, which is the point.
LONGEST_A_STEP_MAY_TAKE = 4 * 60 * 60
# How long a step may go on being retried. A single try is bounded by
# whatever it runs - a check has its own timeout, a model has its own - and
# this stops a slow step being started again and again on top of that.
LONGEST_STEP_SECONDS = 1800.0


class PipelineError(HarnessError):
    """A pipeline that cannot be read, saved, or run."""


@dataclass(frozen=True)
class Kind:
    """One kind of node: what it is called, what it needs, and what it means."""

    id: str
    label: str
    colour: str
    group: str
    summary: str
    settings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "colour": self.colour,
            "group": self.group,
            "summary": self.summary,
            "settings": list(self.settings),
        }


# Every kind of node a pipeline can hold. The colours are the ones the pipeline
# view draws with, kept here so the picture and the engine cannot disagree
# about what exists.
KINDS: dict[str, Kind] = {
    "start": Kind(
        id="start", label="Start", colour="green", group="Flow",
        summary="Where a run begins. Everything it points at starts together.",
    ),
    "suite": Kind(
        id="suite", label="Test suite", colour="mint", group="Tests",
        summary="Runs your checks, or only the ones carrying a tag.",
        settings=("suite", "tag", "case"),
    ),
    "unit_test": Kind(
        id="unit_test", label="Unit test", colour="purple", group="Tests",
        summary="Runs the project's own test command.",
        settings=("command_kind",),
    ),
    "security_scan": Kind(
        id="security_scan", label="Security scan", colour="red", group="Security",
        summary="Reads your files for credentials left in them.",
        settings=("paths",),
    ),
    "security_gate": Kind(
        id="security_gate", label="Security gate", colour="yellow", group="Security",
        summary="Lets the work go on only if the scans before it went well enough.",
        settings=("needs",),
    ),
    "gate": Kind(
        id="gate", label="Gate", colour="blue", group="Flow",
        summary="Lets the work go on if all, or any, of what came before passed.",
        settings=("needs",),
    ),
    "git_repo": Kind(
        id="git_repo", label="Git repo", colour="orange", group="Integrations",
        summary="Reads which branch you are on and whether anything is uncommitted.",
        settings=(),
    ),
    "ai_unit_test": Kind(
        id="ai_unit_test", label="AI writes a test", colour="cyan", group="Integrations",
        summary="Asks the model you have set up to write a test, and saves it.",
        settings=("instructions", "write_to"),
    ),
    "artifact": Kind(
        id="artifact", label="Keep the evidence", colour="grey", group="Flow",
        summary="Writes what happened into one file you can send to somebody.",
        settings=("write_to",),
    ),
    "wait_for_a_person": Kind(
        id="wait_for_a_person", label="Wait for a person", colour="yellow", group="Flow",
        summary="Stops here until somebody says to carry on. For the step before something that matters.",
        settings=("question",),
    ),
    "ask_for_help": Kind(
        id="ask_for_help", label="Ask an assistant", colour="cyan", group="Integrations",
        summary="Asks one question and keeps the answer. It cannot change anything.",
        settings=("question", "who"),
    ),
    "another_pipeline": Kind(
        id="another_pipeline", label="Run another pipeline", colour="blue", group="Flow",
        summary="Runs one of your saved pipelines as a single step, so nobody copies steps about.",
        settings=("pipeline",),
    ),
}

# Which gate kinds decide whether the work goes on.
GATES = {"gate", "security_gate"}

# When a step runs. Most steps run when everything before them passed, which is
# what anybody expects. The other two are for the work that has to happen when
# things go wrong, and the work that has to happen either way.
WHEN_IT_RUNS: tuple[tuple[str, str, str], ...] = (
    (
        "when-all-is-well",
        "When everything before it passed",
        "The usual one. If anything before it failed, this is skipped.",
    ),
    (
        "when-something-failed",
        "Only when something before it failed",
        "For putting things right, or telling somebody. Skipped when all is well.",
    ),
    (
        "whatever-happens",
        "Whatever happened before it",
        "Runs either way. For the step that writes down what happened, which is "
        "needed most when the run went badly.",
    ),
)
WHEN_NAMES = {key for key, _label, _means in WHEN_IT_RUNS}

# How long a step waits before trying again. Trying again at once is the wrong
# answer for anything that failed because something else was busy.
WAITS: tuple[tuple[str, str, str], ...] = (
    ("no-wait", "Straight away", "No wait at all. For a step that fails for its own reasons."),
    (
        "same-wait",
        "Wait a few seconds each time",
        "The same short wait before every try. For a test that needs a moment.",
    ),
    (
        "growing-wait",
        "Wait longer each time",
        "Two seconds, then four, then eight. For something busy that needs room.",
    ),
)
WAIT_NAMES = {key for key, _label, _means in WAITS}
# The first wait, in seconds, and the longest any single wait may be. Small
# enough that a person watching does not think it has hung.
FIRST_WAIT_SECONDS = 2.0
LONGEST_WAIT_BETWEEN_TRIES = 30.0

# How deep one pipeline may call another. A pipeline that runs itself, or two
# that run each other, would go round forever, and each round would look like
# ordinary work.
DEEPEST_NESTING = 3


@dataclass
class NodeResult:
    """What happened at one node."""

    id: str
    kind: str
    label: str
    state: str = WAITING
    said: str = ""
    detail: str = ""
    tries: int = 0
    milliseconds: int = 0
    # True when this run left the step alone: it was done in an earlier run, or
    # this run was only ever about one other step. A picture of the run has to
    # say that, or "passed" would claim more than happened.
    skipped_this_time: bool = False
    # When it started, counted from the beginning of the run. This is what the
    # timeline is drawn from.
    started_after: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "state": self.state,
            "said": self.said,
            "detail": self.detail,
            "tries": self.tries,
            "milliseconds": self.milliseconds,
            "skipped_this_time": self.skipped_this_time,
            "started_after": self.started_after,
        }


@dataclass
class Run:
    """What happened to a whole pipeline."""

    name: str
    nodes: list[NodeResult] = field(default_factory=list)
    passed: bool = False
    said: str = ""
    milliseconds: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "nodes": [node.to_dict() for node in self.nodes],
            "passed": self.passed,
            "said": self.said,
            "milliseconds": self.milliseconds,
        }


def _text(value: Any, what: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise PipelineError(f"{what} has to be text")
    if not allow_empty and not value.strip():
        raise PipelineError(f"{what} cannot be empty")
    if any(ord(letter) < 32 and letter not in "\t\n" for letter in value):
        raise PipelineError(f"{what} holds a control character")
    return value


def check_the_name(name: str) -> str:
    name = _text(name, "The name", allow_empty=False).strip()
    if not NAME_SHAPE.match(name):
        raise PipelineError(
            "A pipeline name can hold letters, numbers, spaces, dashes and "
            "underscores, and has to start with a letter or number"
        )
    return name


def read_it(pipeline: Any) -> dict[str, Any]:
    """Check a pipeline over, and hand back a tidy copy of it.

    Everything a pipeline can say is checked here, once, so nothing further in
    has to wonder whether it is looking at something a person drew or something
    a request made up.
    """

    if not isinstance(pipeline, dict):
        raise PipelineError("That is not a pipeline")
    nodes = pipeline.get("nodes")
    edges = pipeline.get("edges") or []
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise PipelineError("A pipeline is a list of nodes and a list of arrows")
    if len(nodes) > MOST_NODES:
        raise PipelineError(f"A pipeline can hold at most {MOST_NODES} nodes")
    if len(edges) > MOST_EDGES:
        raise PipelineError(f"A pipeline can hold at most {MOST_EDGES} arrows")

    tidy_nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            raise PipelineError("One of the nodes is not a node")
        node_id = _text(node.get("id"), "A node id", allow_empty=False).strip()
        if node_id in seen:
            raise PipelineError(f"Two nodes are both called {node_id}")
        seen.add(node_id)
        kind = _text(node.get("kind"), "A node kind", allow_empty=False)
        if kind not in KINDS:
            raise PipelineError(f"{kind} is not a kind of node this can run")
        settings = node.get("settings") or {}
        if not isinstance(settings, dict):
            raise PipelineError(f"The settings on {node_id} are not settings")
        allowed = set(KINDS[kind].settings) | {
            "tries", "asks", "when", "wait", "longest", "even_if_it_fails"}
        extra = sorted(set(settings) - allowed)
        if extra:
            raise PipelineError(f"{node_id} has a setting it cannot use: {extra[0]}")
        # A step can say which of its own settings to ask about when the run
        # starts, so one saved pipeline serves two jobs without being copied.
        asked = settings.get("asks", [])
        if not isinstance(asked, list) or not all(isinstance(one, str) for one in asked):
            raise PipelineError(f"What {node_id} asks about has to be a list of setting names")
        unknown = sorted(set(asked) - set(KINDS[kind].settings))
        if unknown:
            raise PipelineError(
                f"{node_id} says it asks about {unknown[0]}, which is not one of its settings"
            )
        tries = settings.get("tries", 1)
        if not isinstance(tries, int) or isinstance(tries, bool) or not 1 <= tries <= MOST_TRIES:
            raise PipelineError(f"Tries on {node_id} has to be a whole number from 1 to {MOST_TRIES}")
        when = settings.get("when", "when-all-is-well")
        if when not in WHEN_NAMES:
            raise PipelineError(
                f"When {node_id} runs has to be one of: " + ", ".join(sorted(WHEN_NAMES))
            )
        # How long this step may take. A step with nothing to say for itself
        # otherwise holds the whole run up until the run's own limit runs out,
        # which on a long automation is the rest of the afternoon.
        longest = settings.get("longest", 0)
        if (not isinstance(longest, int) or isinstance(longest, bool)
                or not 0 <= longest <= LONGEST_A_STEP_MAY_TAKE):
            raise PipelineError(
                f"How long {node_id} may take has to be a whole number of seconds "
                f"from 0 to {LONGEST_A_STEP_MAY_TAKE}, where 0 means no limit of its own"
            )
        # Whether the rest may carry on without it. Some steps are the point of
        # the whole run and some are a nice-to-have, and one nice-to-have
        # failing should not throw away the work that already passed.
        carry_on = settings.get("even_if_it_fails", False)
        if not isinstance(carry_on, bool):
            raise PipelineError(
                f"Whether the rest carries on without {node_id} is yes or no")
        wait = settings.get("wait", "no-wait")
        if wait not in WAIT_NAMES:
            raise PipelineError(
                f"How {node_id} waits before trying again has to be one of: "
                + ", ".join(sorted(WAIT_NAMES))
            )
        for key, value in settings.items():
            if key in ("tries", "asks", "when", "wait", "longest", "even_if_it_fails"):
                continue
            if isinstance(value, list):
                for item in value:
                    _text(item, f"{node_id}.{key}")
            else:
                _text(value, f"{node_id}.{key}")
        where = node.get("at") or {}
        at = {
            "x": float(where.get("x", 0)) if isinstance(where, dict) else 0.0,
            "y": float(where.get("y", 0)) if isinstance(where, dict) else 0.0,
        }
        tidy_nodes.append({
            "id": node_id,
            "kind": kind,
            "label": _text(node.get("label") or KINDS[kind].label, "A node label")[:80],
            "settings": settings,
            "at": at,
        })

    tidy_edges: list[dict[str, str]] = []
    for edge in edges:
        if not isinstance(edge, dict):
            raise PipelineError("One of the arrows is not an arrow")
        source = _text(edge.get("from"), "An arrow start", allow_empty=False)
        target = _text(edge.get("to"), "An arrow end", allow_empty=False)
        if source not in seen or target not in seen:
            raise PipelineError("An arrow points at a node that is not here")
        if source == target:
            raise PipelineError(f"{source} points at itself")
        if any(item["from"] == source and item["to"] == target for item in tidy_edges):
            continue
        tidy_edges.append({"from": source, "to": target})

    tidy = {
        "name": check_the_name(pipeline.get("name") or "Pipeline"),
        "nodes": tidy_nodes,
        "edges": tidy_edges,
    }
    going_round = the_loop_in_it(tidy)
    if going_round:
        raise PipelineError(
            "This pipeline goes round in a circle and would never finish: "
            + " then ".join(going_round)
        )
    return tidy


def the_loop_in_it(pipeline: dict[str, Any]) -> list[str]:
    """The nodes that lead back to themselves, or nothing if none do."""

    onward: dict[str, list[str]] = {node["id"]: [] for node in pipeline["nodes"]}
    for edge in pipeline["edges"]:
        onward[edge["from"]].append(edge["to"])
    colour: dict[str, int] = {}
    path: list[str] = []

    def walk(node_id: str) -> list[str]:
        colour[node_id] = 1
        path.append(node_id)
        for onto in onward.get(node_id, []):
            if colour.get(onto) == 1:
                return path[path.index(onto):] + [onto]
            if colour.get(onto, 0) == 0:
                found = walk(onto)
                if found:
                    return found
        colour[node_id] = 2
        path.pop()
        return []

    for node_id in onward:
        if colour.get(node_id, 0) == 0:
            found = walk(node_id)
            if found:
                return found
    return []


def in_running_order(pipeline: dict[str, Any]) -> list[str]:
    """The node ids in an order where nothing runs before what it waits on."""

    waiting_on: dict[str, set[str]] = {node["id"]: set() for node in pipeline["nodes"]}
    for edge in pipeline["edges"]:
        waiting_on[edge["to"]].add(edge["from"])
    order: list[str] = []
    left = dict(waiting_on)
    while left:
        ready = sorted(node_id for node_id, needs in left.items() if not needs)
        if not ready:  # pragma: no cover - read_it refuses a circle first
            raise PipelineError("This pipeline goes round in a circle")
        for node_id in ready:
            order.append(node_id)
            del left[node_id]
        for needs in left.values():
            needs.difference_update(ready)
    return order


def _and_everything_after(
    node_id: str, going_to: dict[str, list[str]], found: set[str]
) -> None:
    """This step and every step downstream of it, however far down."""

    if node_id in found:
        return
    found.add(node_id)
    for next_one in going_to.get(node_id, []):
        _and_everything_after(next_one, going_to, found)


def _handlers_last(
    order: list[str], by_id: dict[str, Any], coming_from: dict[str, list[str]]
) -> list[str]:
    """Steps that only run when something failed go after everything else.

    Two steps that do not wait on each other are put in order by their names,
    which is fine until one of them is there to catch the other one failing.
    Then "tell somebody it broke" runs first, sees nothing broken yet, and is
    skipped one line before the break happens - which is the whole feature not
    working, quietly, depending on what somebody called their steps.

    So a step that only runs on failure, and everything waiting on it, is moved
    to the end. Their own order between themselves is kept, and everything they
    wait on is still before them, so this is still an order nothing runs early
    in.
    """

    going_to: dict[str, list[str]] = {node_id: [] for node_id in order}
    for node_id, before in coming_from.items():
        for other in before:
            going_to.setdefault(other, []).append(node_id)
    later: set[str] = set()
    for node_id in order:
        settings = by_id[node_id].get("settings") or {}
        if settings.get("when") == "when-something-failed":
            _and_everything_after(node_id, going_to, later)
    if not later:
        return order
    return ([one for one in order if one not in later]
            + [one for one in order if one in later])


# ---- where they are kept ----------------------------------------------------


def folder(config: LoadedConfig) -> Path:
    # Pipelines live beside the rest of the harness's own files, so this is one
    # of the few places allowed to name that folder.
    return confined_path(config.project_root, WHERE_THEY_LIVE, allow_missing=True, allow_control=True)


def file_for(config: LoadedConfig, name: str) -> Path:
    """The file one pipeline lives in.

    The name is checked into a shape that cannot climb out of the folder, and
    then confined anyway, because one guard is a guard nobody notices removing.
    """

    safe = check_the_name(name).replace(" ", "-").lower()
    return confined_path(
        config.project_root, f"{WHERE_THEY_LIVE}/{safe}.json",
        allow_missing=True, allow_control=True,
    )


def saved_ones(config: LoadedConfig) -> list[str]:
    where = folder(config)
    if not where.is_dir():
        return []
    found = []
    for path in sorted(where.glob("*.json")):
        try:
            held = _read_it_whole(path)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(held, dict) and isinstance(held.get("name"), str):
            found.append(held["name"])
    return found


def load(config: LoadedConfig, name: str) -> dict[str, Any]:
    return _the_one_called(config, name)[1]


def _the_one_called(config: LoadedConfig, name: str) -> tuple[Path, dict[str, Any]]:
    """The file a pipeline lives in, and what is in it.

    "Nightly build" and "Nightly-Build" share one file name. Saving already
    refuses to write one over the other. Reading and removing did not, so
    asking for a name nobody ever saved handed back somebody else's pipeline,
    and removing it took theirs away while naming one that never existed.
    """

    path = file_for(config, name)
    if not path.is_file():
        raise PipelineError(f"There is no pipeline called {name}")
    try:
        held = _read_it_whole(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"{path.name} cannot be read: {exc}") from exc
    there = str(held.get("name") or "") if isinstance(held, dict) else ""
    if there and there.strip().lower() != str(name).strip().lower():
        raise PipelineError(
            f"There is no pipeline called {name}. The one saved under that file "
            f"name is called {there}."
        )
    return path, read_it(held)


# How many earlier versions of one pipeline are kept. Enough to undo an
# afternoon of changes; few enough that nobody has to scroll to find the one
# they meant.
HOW_MANY_KEPT = 20
OLD_ONES = ".harness/pipelines/before"


def _write_it_whole(path: Path, written: str) -> None:
    """Write the file beside itself, then move it into place.

    Writing straight over a file empties it first and fills it after. A panel
    refreshing in that moment reads an empty file and tells somebody their
    pipeline cannot be read, about a pipeline that is perfectly fine. Moving a
    finished file into place is one step as far as any reader is concerned.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    # One name each, so two writers never share the file beside it.
    beside = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.part")
    beside.write_text(written, encoding="utf-8")
    # Windows will not move a file into place while somebody has it open, even
    # only to read. That somebody lets go in a moment, so this waits rather
    # than losing the change.
    for wait in (0.02, 0.05, 0.1, 0.2, 0.4, 0.8):
        try:
            os.replace(beside, path)
            return
        except PermissionError:
            time.sleep(wait)
    try:
        os.replace(beside, path)
    except PermissionError:
        beside.unlink(missing_ok=True)
        raise PipelineError(
            f"{path.name} is held open by something else, so it could not be "
            "saved. Close whatever has it open and try again."
        ) from None


def _read_it_whole(path: Path) -> Any:
    """Read a file that something else may be moving into place right now."""

    for wait in (0.0, 0.02, 0.05, 0.1, 0.2):
        if wait:
            time.sleep(wait)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            continue
    return json.loads(path.read_text(encoding="utf-8"))


def _where_the_old_ones_live(config: LoadedConfig, name: str) -> Path:
    safe = check_the_name(name).replace(" ", "-").lower()
    return confined_path(
        config.project_root, f"{OLD_ONES}/{safe}.json",
        allow_missing=True, allow_control=True,
    )


def older_ones(config: LoadedConfig, name: str) -> list[dict[str, Any]]:
    """Every version of this pipeline that was saved over, newest first."""

    where = _where_the_old_ones_live(config, name)
    if not where.is_file():
        return []
    try:
        held = _read_it_whole(where)
    except (OSError, json.JSONDecodeError):
        # A record of what came before is worth having and not worth failing
        # over. An unreadable one means there is nothing to go back to.
        return []
    if not isinstance(held, list):
        return []
    kept = []
    for one in held:
        if not isinstance(one, dict) or not isinstance(one.get("pipeline"), dict):
            continue
        kept.append({
            "saved_at": str(one.get("saved_at") or ""),
            "steps": len(one["pipeline"].get("nodes") or []),
            "arrows": len(one["pipeline"].get("edges") or []),
            "what_changed": str(one.get("what_changed") or ""),
            "pipeline": one["pipeline"],
        })
    return kept


def _keep_what_was_there(config: LoadedConfig, was: dict[str, Any], now: dict[str, Any]) -> None:
    """Put the version being saved over on the pile, before it is gone."""

    where = _where_the_old_ones_live(config, was["name"])
    kept = [
        {"saved_at": one["saved_at"], "what_changed": one["what_changed"],
         "pipeline": one["pipeline"]}
        for one in older_ones(config, was["name"])
    ]
    kept.insert(0, {
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "what_changed": _what_changed(was, now),
        "pipeline": was,
    })
    del kept[HOW_MANY_KEPT:]
    _write_it_whole(where, json.dumps(kept, indent=2) + "\n")


def _what_changed(was: dict[str, Any], now: dict[str, Any]) -> str:
    """One line saying how the two differ, so a list of versions can be read."""

    was_steps = {node["id"]: node for node in was.get("nodes", [])}
    now_steps = {node["id"]: node for node in now.get("nodes", [])}
    added = sorted(set(now_steps) - set(was_steps))
    gone = sorted(set(was_steps) - set(now_steps))
    changed = sorted(
        one for one in set(was_steps) & set(now_steps)
        if was_steps[one] != now_steps[one]
    )
    said = []
    if added:
        said.append(f"{len(added)} step(s) added: " + ", ".join(
            now_steps[one].get("label", one) for one in added[:3]))
    if gone:
        said.append(f"{len(gone)} taken out: " + ", ".join(
            was_steps[one].get("label", one) for one in gone[:3]))
    if changed:
        said.append(f"{len(changed)} changed: " + ", ".join(
            now_steps[one].get("label", one) for one in changed[:3]))
    if len(was.get("edges", [])) != len(now.get("edges", [])):
        said.append("the arrows changed")
    return "; ".join(said) or "nothing that shows here"


def save(config: LoadedConfig, pipeline: Any) -> dict[str, Any]:
    tidy = read_it(pipeline)
    path = file_for(config, tidy["name"])
    # "Nightly build" and "Nightly Build" become the same file name. Writing
    # anyway would throw one of them away without a word.
    if path.is_file():
        try:
            already = _read_it_whole(path)
        except (OSError, json.JSONDecodeError):
            already = {}
        there = str(already.get("name") or "") if isinstance(already, dict) else ""
        if there and there != tidy["name"]:
            raise PipelineError(
                f"{there} is already saved under that file name, and saving this one "
                f"would write over it. Give {tidy['name']} a name that is different "
                "by more than capitals and spaces."
            )
    # Whatever is there now goes on the pile before it is written over. This
    # is the one place a person could lose work by pressing Save.
    if path.is_file():
        try:
            was = read_it(_read_it_whole(path))
        except (OSError, json.JSONDecodeError, PipelineError):
            was = None
        if was is not None and was != tidy:
            _keep_what_was_there(config, was, tidy)
    _write_it_whole(path, json.dumps(tidy, indent=2) + "\n")
    return tidy


def remove(config: LoadedConfig, name: str) -> str:
    path, held = _the_one_called(config, name)
    take_the_file_away(path)
    # And the versions kept for it. Leaving those behind would keep a copy of
    # something somebody asked to be rid of.
    take_the_file_away(
        _where_the_old_ones_live(config, held["name"]), missing_ok=True
    )
    return f"{held['name']} was removed."


def a_starting_pipeline() -> dict[str, Any]:
    """One that shows the shape of the thing, ready to run as it stands."""

    return {
        "name": "First pipeline",
        "nodes": [
            {"id": "start", "kind": "start", "label": "Start", "at": {"x": 40, "y": 160}},
            {"id": "scan", "kind": "security_scan", "label": "Security scan",
             "settings": {}, "at": {"x": 260, "y": 60}},
            {"id": "checks", "kind": "suite", "label": "Your checks",
             "settings": {}, "at": {"x": 260, "y": 250}},
            {"id": "gate", "kind": "security_gate", "label": "Security gate",
             "settings": {"needs": "all"}, "at": {"x": 520, "y": 60}},
            {"id": "tests", "kind": "unit_test", "label": "Unit tests",
             "settings": {"command_kind": "test", "tries": 2}, "at": {"x": 780, "y": 160}},
            {"id": "evidence", "kind": "artifact", "label": "Keep the evidence",
             "settings": {}, "at": {"x": 1020, "y": 160}},
        ],
        "edges": [
            {"from": "start", "to": "scan"},
            {"from": "start", "to": "checks"},
            {"from": "scan", "to": "gate"},
            {"from": "gate", "to": "tests"},
            {"from": "checks", "to": "tests"},
            {"from": "tests", "to": "evidence"},
        ],
    }


# ---- running them -----------------------------------------------------------


def _how_long(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _run_a_suite(config: LoadedConfig, node: dict[str, Any], check_kinds) -> tuple[bool, str, str]:
    from . import qa as qalab

    settings = node["settings"]
    suite = qalab.load_suite(config, settings.get("suite") or None, check_kinds)
    tags = [settings["tag"]] if settings.get("tag") else []
    ids = [settings["case"]] if settings.get("case") else []
    result = qalab.QaRunner(config, extra_kinds=check_kinds).run(
        suite, tags=tags, ids=ids, write_artifacts=False
    )
    counts = result.counts
    said = f"{counts.get('passed', 0)} of {counts.get('total', 0)} checks passed"
    trouble = [case.title for case in result.cases if not case.passed]
    return result.passed, said, "; ".join(trouble[:5])


def _run_one_off_check(config: LoadedConfig, case: dict[str, Any], check_kinds) -> tuple[bool, str, str]:
    """Run a single check this node made up, without saving it anywhere."""

    from . import qa as qalab

    suite = qalab.parse_suite(
        {"schema_version": 1, "name": "pipeline", "cases": [case]}, extra_kinds=check_kinds
    )
    result = qalab.QaRunner(config, extra_kinds=check_kinds).run(suite, write_artifacts=False)
    only = result.cases[0]
    # A check says why in its reasons, and each attempt keeps what it saw.
    # There is no summary field on a result: reaching for one turned every step
    # in the panel into "this step went wrong" while every test still passed.
    why = "; ".join(only.reasons)
    saw = only.attempts[-1].evidence if only.attempts else ""
    # With nothing wrong, what the check saw is worth more than "as expected".
    # With nothing wrong, what the check saw says more than "as expected".
    first_line = saw.splitlines()[0][:120] if saw.strip() else ""
    said = why or first_line or ("As expected" if only.passed else "It did not pass")
    return only.passed, said, why or saw[:400]


def _run_security_scan(config: LoadedConfig, node: dict[str, Any], check_kinds) -> tuple[bool, str, str]:
    paths = node["settings"].get("paths")
    if isinstance(paths, str):
        paths = [part.strip() for part in paths.split(",") if part.strip()]
    case: dict[str, Any] = {
        "id": f"pipeline-{node['id']}",
        "title": node["label"],
        "kind": "secrets",
    }
    if paths:
        case["paths"] = paths
    return _run_one_off_check(config, case, check_kinds)


def _run_unit_test(config: LoadedConfig, node: dict[str, Any], check_kinds) -> tuple[bool, str, str]:
    from .detect import combined_commands, detect_project

    which = node["settings"].get("command_kind") or "test"
    if which not in ("test", "lint", "build"):
        raise PipelineError("A unit test node runs the test, lint, or build command")
    commands = list(config.get(f"project.{which}_commands") or []) or combined_commands(
        detect_project(config.project_root), which
    )
    if not commands:
        return False, f"This project has no {which} command set", (
            f"Set project.{which}_commands in your settings, or let the harness find it."
        )
    said: list[str] = []
    for command in commands:
        parts = list(command) if isinstance(command, (list, tuple)) else [str(command)]
        passed, one, detail = _run_one_off_check(
            config,
            {
                "id": f"pipeline-{node['id']}-{len(said)}",
                "title": " ".join(parts)[:80],
                "kind": "command",
                "command": parts,
                "expect": {"exit_code": 0},
            },
            check_kinds,
        )
        said.append(f"{' '.join(parts)[:60]}: {one}")
        if not passed:
            return False, one, detail or "; ".join(said)
    return True, f"{len(commands)} command(s) finished", "; ".join(said)


def _run_git_repo(config: LoadedConfig, node: dict[str, Any], _kinds) -> tuple[bool, str, str]:
    """Read the state of the repository. It never writes, fetches, or pulls."""

    def ask(*parts: str) -> tuple[int, str]:
        try:
            finished = subprocess.run(
                ["git", *parts], cwd=config.project_root, capture_output=True,
                text=True, timeout=60.0, check=False,
            )
        except FileNotFoundError:
            return 127, "git is not on this machine"
        except (subprocess.TimeoutExpired, OSError) as exc:
            return 124, str(exc)
        return finished.returncode, (finished.stdout or finished.stderr or "").strip()

    code, branch = ask("rev-parse", "--abbrev-ref", "HEAD")
    if code != 0:
        return False, "This folder is not a git repository", branch[:200]
    code, changes = ask("status", "--porcelain")
    if code != 0:
        return False, "git could not read the repository", changes[:200]
    waiting = [line for line in changes.splitlines() if line.strip()]
    said = f"On {branch}, {len(waiting)} file(s) not committed"
    return True, said, "\n".join(waiting[:20])


def _run_ai_unit_test(config: LoadedConfig, node: dict[str, Any], _kinds) -> tuple[bool, str, str]:
    """Ask the model you have set up to write a test, and save what it writes."""

    from .models import ProviderRequest
    from .providers import create_provider

    settings = node["settings"]
    instructions = str(settings.get("instructions") or "").strip()
    if not instructions:
        return False, "Nothing was asked for", "Say what the test should cover."
    write_to = str(settings.get("write_to") or "").strip()
    if not write_to:
        return False, "Nowhere to write it", "Give the draft a file name."
    # A pipeline is a file people pass around, and this step writes whatever a
    # model says. So it writes into a drafts folder of its own, and nowhere
    # else.
    #
    # Writing into tests/ was the obvious thing to do and it was wrong: tests/
    # is exactly where every test runner goes looking, so a pipeline could have
    # a model write a "test" and have the very next step run it. Calling a file
    # safe because it is named like a test had it backwards. Nothing runs what
    # is written here, and a person moves it into their tests once they have
    # read it.
    if "/" in write_to or "\\" in write_to:
        return False, "That is a path, and this writes one file", (
            "Give a file name on its own. Drafts are kept together in "
            f"{DRAFTS}, and you move one into your tests once you have read it."
        )
    where = confined_path(
        config.project_root, f"{DRAFTS}/{write_to}", allow_missing=True, allow_control=True
    )
    if where.exists():
        return False, "There is already a draft with that name", (
            f"{where.name} is already in {DRAFTS}. Read it, move it or remove it, "
            "and give this one a name nothing is using."
        )
    try:
        provider = create_provider(config)
    except HarnessError as exc:
        return False, "No model is connected yet", str(exc)
    asked = ProviderRequest(
        system_prefix=(
            "You write one test file and nothing else. Answer with the contents "
            "of the file only: no explanation, no fence, no commentary."
        ),
        dynamic_context="",
        messages=[{"role": "user", "content": instructions[:4000]}],
        model=str(config.get("provider.model") or ""),
        max_output_tokens=4096,
    )
    try:
        answer = provider.complete(asked)
    except HarnessError as exc:
        return False, "The model could not be reached", str(exc)
    written = str(getattr(answer, "text", "") or "").strip()
    if not written:
        return False, "The model wrote nothing", "Try again, or say more about what you want."
    # A model can wrap its answer in a fence however plainly it is asked not to.
    if written.startswith("```"):
        lines = written.splitlines()
        keep = lines[1:-1] if lines[-1].strip().startswith("```") else lines[1:]
        written = "\n".join(keep)
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(written.strip() + "\n", encoding="utf-8")
    return True, (
        f"Wrote {DRAFTS}/{where.name}, {len(written.splitlines())} lines. "
        "Read it, then move it into your tests yourself. Nothing runs it where it is."
    ), written[:400]


def _run_artifact(config: LoadedConfig, node: dict[str, Any], _kinds, so_far=None) -> tuple[bool, str, str]:
    asked = str(node["settings"].get("write_to") or "").strip()
    # Where it goes by default is the harness's own folder. A place somebody
    # types in is an ordinary project path, and may not be one of the folders
    # the harness and git keep their workings in.
    where = (
        confined_path(config.project_root, asked, allow_missing=True)
        if asked
        else confined_path(
            config.project_root, f"{WHERE_THEY_LIVE}/last-run.json",
            allow_missing=True, allow_control=True,
        )
    )
    where.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps([result.to_dict() for result in (so_far or [])], indent=2) + "\n"
    where.write_text(body, encoding="utf-8")
    return True, f"Wrote {where.name}", f"{len(so_far or [])} step(s) recorded"


def _also_stopping_after(stopping, started: float, seconds: int):
    """The run's own stop button, and this step's time limit, as one thing.

    A step is handed something it can ask "should I stop yet". Handing it two
    things to ask would mean every kind of step remembering to ask both, and the
    one that forgot would be the one that hung.
    """

    if not seconds:
        return stopping

    def yes_it_should() -> bool:
        if stopping and stopping():
            return True
        return time.monotonic() - started >= seconds

    return yes_it_should


def _was_allowed_to_fail(pipeline: dict[str, Any], item: NodeResult) -> bool:
    """Whether this step was marked as one the rest may carry on without."""

    for node in pipeline.get("nodes", []):
        if node.get("id") == item.id:
            return bool((node.get("settings") or {}).get("even_if_it_fails"))
    return False


def _was_not_needed(item: "NodeResult") -> bool:
    """True for a step that was only there for trouble, on a run with none.

    Not the same as a step this run left as it was, which is marked the same
    way but passed in an earlier run and still counts. Telling the two apart is
    what keeps "carry on from here" honest.
    """

    return item.state == SKIPPED and item.skipped_this_time


def _decide_a_gate(node: dict[str, Any], before: list[NodeResult]) -> tuple[bool, str, str]:
    needs = str(node["settings"].get("needs") or "all").lower()
    if needs not in ("all", "any"):
        raise PipelineError("A gate needs either all or any of what came before it")
    # A step that was only there for when something goes wrong, on a run where
    # nothing did, is not something this gate is waiting on. Counting it would
    # shut every gate downstream of a handler on every clean run.
    before = [item for item in before if not _was_not_needed(item)]
    if not before:
        return True, "Nothing came before this gate", ""
    good = [item for item in before if item.state == PASSED]
    if needs == "any":
        passed = bool(good)
        said = f"{len(good)} of {len(before)} passed, and this gate needs one"
    else:
        passed = len(good) == len(before)
        said = f"{len(good)} of {len(before)} passed, and this gate needs all"
    trouble = ", ".join(item.label for item in before if item.state != PASSED)
    return passed, said, trouble


def run_it(
    config: LoadedConfig,
    pipeline: Any,
    *,
    tell: Callable[[dict[str, Any]], None] | None = None,
    check_kinds: Any = None,
    stopping: Callable[[], bool] | None = None,
    from_here: str = "",
    only: str = "",
    answers: dict[str, Any] | None = None,
    waiting_on: Callable[[str], bool] | None = None,
    depth: int = 0,
) -> Run:
    """Run a whole pipeline, in order, and say what happened at each step.

    Everything a node does is something the harness can already do on its own.
    What this adds is the order, the gates, and trying again.

    Three ways to run less than all of it:

      - `from_here` starts at one step and carries on from there. The steps
        before it are marked as already done rather than run again, which is
        what somebody wants after fixing what broke at step four of six.
      - `only` runs one step and nothing else, which is how a step gets built:
        try it, look at what it said, change it, try it again.
      - `answers` fills in the settings a step said it would ask for, so one
        saved pipeline can serve two jobs without being copied.
    """

    tidy = read_it(pipeline)
    order = in_running_order(tidy)
    if from_here and from_here not in {node["id"] for node in tidy["nodes"]}:
        raise PipelineError(f"There is no step called {from_here} in this pipeline")
    if only and only not in {node["id"] for node in tidy["nodes"]}:
        raise PipelineError(f"There is no step called {only} in this pipeline")
    answers = dict(answers or {})
    by_id = {node["id"]: node for node in tidy["nodes"]}
    coming_from: dict[str, list[str]] = {node_id: [] for node_id in by_id}
    for edge in tidy["edges"]:
        coming_from[edge["to"]].append(edge["from"])
    order = _handlers_last(order, by_id, coming_from)

    results: dict[str, NodeResult] = {
        node_id: NodeResult(id=node_id, kind=by_id[node_id]["kind"], label=by_id[node_id]["label"])
        for node_id in by_id
    }
    started_everything = time.monotonic()

    def say(result: NodeResult) -> None:
        if tell:
            tell({"kind": "pipeline_node", "node": result.id, "payload": result.to_dict()})

    # Which steps this run is really doing. The rest are marked as already
    # done, so a picture of the run says plainly what was and was not tried.
    doing = set(order)
    if only:
        doing = {only}
    elif from_here:
        doing = _from_here_on(order, coming_from, from_here)
    for node_id in order:
        node = by_id[node_id]
        result = results[node_id]
        before = [results[other] for other in coming_from[node_id]]
        # When the run reached this step, stamped whatever happens to it next.
        # A step that was skipped still happened at a moment, and the timeline
        # draws every step by this number: left at nought, a step skipped after
        # two minutes of work was drawn at the far left, as if it had run
        # alongside the very first one.
        result.started_after = int((time.monotonic() - started_everything) * 1000)
        if node_id not in doing:
            result.state = PASSED
            result.said = "Left as it was, from an earlier run"
            result.skipped_this_time = True
            say(result)
            continue
        # Anything the step said it would ask about is filled in here, once,
        # before it runs. Nothing else about the saved pipeline changes.
        asked = node["settings"].get("asks")
        if isinstance(asked, list):
            for name in asked:
                key = f"{node_id}.{name}"
                if key in answers:
                    node["settings"][name] = answers[key]
        if stopping and stopping():
            result.state = SKIPPED
            result.said = "The run was stopped"
            say(result)
            continue
        # A node whose way here was blocked never runs - unless it was put
        # there for exactly that. Saying so beats leaving it at waiting, which
        # reads as "still to come".
        when = str(node["settings"].get("when", "when-all-is-well"))
        # A step that was skipped because it was never needed did not fail, so
        # nothing after it is blocked by it. Without this, an ordinary step
        # after a "tell somebody it broke" step was skipped on every run where
        # nothing broke, and told it was skipped because that step "did not
        # pass" - one line under that step saying nothing went wrong.
        blocked = [
            item for item in before
            if item.state in (FAILED, SKIPPED)
            and not _was_not_needed(item) and not _was_allowed_to_fail(pipeline, item)
        ]
        anything_wrong = any(
            item.state in (FAILED, SKIPPED)
            and not _was_not_needed(item) and not _was_allowed_to_fail(pipeline, item)
            for item in results.values()
        )
        # Somebody who pressed "run only this" on a step, or "carry on from
        # here" starting at one, asked for that step. Skipping it because
        # nothing went wrong would be the panel arguing with the button.
        asked_for = node_id in (only, from_here)
        if when == "when-something-failed" and not anything_wrong and not asked_for:
            result.state = SKIPPED
            result.said = "Nothing went wrong, so this was not needed"
            result.skipped_this_time = True
            say(result)
            continue
        if blocked and node["kind"] not in GATES and when == "when-all-is-well":
            result.state = SKIPPED
            result.said = f"Skipped, because {blocked[0].label} did not pass"
            say(result)
            continue

        result.state = RUNNING
        result.started_after = int((time.monotonic() - started_everything) * 1000)
        say(result)
        started = time.monotonic()
        if node["kind"] == "wait_for_a_person":
            # The one step that is meant to take as long as a person takes.
            # Everything before it has run, nothing after it has, and it says
            # so while it waits.
            result.said = str(node["settings"].get("question") or "").strip() or (
                "Waiting for somebody to say carry on."
            )
            say(result)
            answered = _wait_for_a_person(node_id, waiting_on, stopping, started)
            result.milliseconds = int((time.monotonic() - started) * 1000)
            if answered is True:
                result.state = PASSED
                result.said = "Somebody said carry on."
            elif answered is False:
                result.state = FAILED
                result.said = "Somebody said no, so the rest was not run."
            else:
                result.state = SKIPPED
                result.said = (
                    "Nobody answered, so the rest was not run. Nothing after this ran."
                )
            say(result)
            continue
        tries = int(node["settings"].get("tries", 1))
        passed, said, detail = False, "", ""
        # How long this step may take, when somebody said. A step with nothing
        # to say for itself otherwise holds the whole run up until the run's own
        # limit runs out, which on a long automation is the rest of the
        # afternoon - and the person who set it going is not watching.
        its_own_limit = int(node["settings"].get("longest", 0) or 0)
        for attempt in range(1, tries + 1):
            result.tries = attempt
            try:
                passed, said, detail = _do_one(
                    config, node, before, results, order, check_kinds, depth,
                    _also_stopping_after(stopping, started, its_own_limit), waiting_on,
                )
                if (not passed and its_own_limit
                        and time.monotonic() - started >= its_own_limit):
                    said = (
                        f"This step was given {its_own_limit} second"
                        f"{'' if its_own_limit == 1 else 's'} and took longer, "
                        "so it was stopped."
                    )
            except HarnessError as exc:
                passed, said, detail = False, str(exc), ""
            except Exception as exc:  # noqa: BLE001 - one bad node must not end the run
                passed, said, detail = False, f"This step went wrong: {exc}", ""
            if passed or attempt >= tries:
                break
            if time.monotonic() - started >= LONGEST_STEP_SECONDS:
                said = (
                    f"{said} It has been going for "
                    f"{int(LONGEST_STEP_SECONDS / 60)} minutes, so it was not tried again."
                )
                break
            # Waiting before trying again. Trying at once is the wrong answer
            # for anything that failed because something else was busy, and it
            # is the only answer we used to have.
            holding = _how_long_to_wait(str(node["settings"].get("wait", "no-wait")), attempt)
            if holding:
                result.said = (
                    f"{said} Trying again in {int(holding)} second"
                    f"{'' if int(holding) == 1 else 's'}."
                )
                say(result)
                _hold_on(holding, stopping)
            else:
                say(result)
            # Stop means stop. Without this, pressing Stop during a wait only
            # cut the wait short and then ran the step one more time, which is
            # the opposite of what the button says.
            if stopping and stopping():
                said = f"{said} The run was stopped, so it was not tried again."
                break
        result.state = PASSED if passed else FAILED
        result.said = said
        result.detail = detail
        result.milliseconds = _how_long(started)
        say(result)

    ordered = [results[node_id] for node_id in order]
    # A step somebody marked as allowed to fail is not counted against the run.
    # Some steps are the point of the whole thing and some are a nice-to-have -
    # posting a note, tidying up afterwards - and one nice-to-have failing
    # should not throw away work that already passed.
    failed = [
        item for item in ordered
        if item.state == FAILED and not _was_allowed_to_fail(pipeline, item)
    ]
    # A step that never ran is not a step that passed. Being stopped part way,
    # or nobody answering, leaves work undone, and a run with work left undone
    # must not read like one that finished. The only steps that do not count
    # against it are the ones this run deliberately left alone: the ones an
    # earlier run already did, and the ones that were only ever there for when
    # something goes wrong.
    skipped = [
        item for item in ordered if item.state == SKIPPED and not item.skipped_this_time
    ]
    not_needed = [item for item in ordered if _was_not_needed(item)]
    left_alone = [
        item for item in ordered if item.state == PASSED and item.skipped_this_time
    ]
    run = Run(
        name=tidy["name"],
        nodes=ordered,
        passed=not failed and not skipped,
        milliseconds=_how_long(started_everything),
    )
    if failed:
        run.said = f"{len(failed)} step(s) did not pass: " + ", ".join(
            item.label for item in failed[:4]
        )
    elif skipped:
        run.said = (
            f"Everything that ran passed, and {len(skipped)} step(s) never ran: "
            + ", ".join(item.label for item in skipped[:4])
        )
    elif left_alone or not_needed:
        ran = len(ordered) - len(left_alone) - len(not_needed)
        said = [f"Every one of the {ran} step(s) this run covered passed."]
        if left_alone:
            said.append(f"{len(left_alone)} were left as they were.")
        if not_needed:
            said.append(
                f"{len(not_needed)} were only for when something goes wrong, "
                "and nothing did."
            )
        run.said = " ".join(said)
    else:
        run.said = f"Every one of the {len(ordered)} steps passed."
    return run


# How long a step waits for somebody to answer before giving up. Long enough to
# go and look at something; short enough that a forgotten run does not hold a
# thread until the machine is turned off.
LONGEST_WAIT_SECONDS = 3600.0


def _wait_for_a_person(node_id, waiting_on, stopping, started) -> bool | None:
    """Wait until somebody says carry on, says no, or nobody says anything."""

    if waiting_on is None:
        # Nothing is listening for an answer - a run from the command line, or
        # a test. Waiting for a person nobody asked would hang forever, so it
        # says plainly that nobody answered.
        return None
    while True:
        answer = waiting_on(node_id)
        if answer is not None:
            return bool(answer)
        if stopping and stopping():
            return None
        if time.monotonic() - started >= LONGEST_WAIT_SECONDS:
            return None
        time.sleep(0.25)


def _how_long_to_wait(wait: str, attempt: int) -> float:
    """How long to hold on before trying a step again."""

    if wait == "same-wait":
        return FIRST_WAIT_SECONDS
    if wait == "growing-wait":
        return min(FIRST_WAIT_SECONDS * (2 ** (attempt - 1)), LONGEST_WAIT_BETWEEN_TRIES)
    return 0.0


def _hold_on(seconds: float, stopping) -> None:
    """Wait, and notice at once if somebody presses Stop while waiting."""

    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if stopping and stopping():
            return
        # Never a negative sleep. The clock can pass `until` between the line
        # above and this one, and asking to sleep for less than no time at all
        # ends the run with an error instead of a pipeline.
        time.sleep(max(0.0, min(0.2, until - time.monotonic())))


def _from_here_on(
    order: list[str], coming_from: dict[str, list[str]], from_here: str
) -> set[str]:
    """This step, and everything that waits on it, however far down."""

    doing = {from_here}
    for node_id in order:
        if any(other in doing for other in coming_from.get(node_id, [])):
            doing.add(node_id)
    return doing


def _ask_for_help(config, node) -> tuple[bool, str, str]:
    """Ask an assistant one question, part way through a run.

    It reads nothing and changes nothing: it is asked something and it answers.
    The answer is kept with the run, which is where somebody reading what
    happened will look for it.
    """

    from . import helper

    question = str(node["settings"].get("question") or "").strip()
    if not question:
        return False, "This step does not say what to ask.", ""
    try:
        said = helper.ask_for_help(
            config, question, who=str(node["settings"].get("who") or "")
        )
    except HarnessError as exc:
        return False, str(exc), ""
    first = said.answer.strip().splitlines()[0] if said.answer.strip() else ""
    return True, f"{said.who} answered: {first[:160]}", said.answer


def _run_another_pipeline(
    config, node, check_kinds, depth: int, stopping=None, waiting_on=None
) -> tuple[bool, str, str]:
    """Run one saved pipeline as a single step of this one."""

    name = str(node["settings"].get("pipeline") or "").strip()
    if not name:
        return False, "This step does not say which pipeline to run.", ""
    if depth + 1 > DEEPEST_NESTING:
        return (
            False,
            f"Pipelines are only followed {DEEPEST_NESTING} deep. One of them is calling "
            "another that calls it back, or the chain is simply too long to follow.",
            "",
        )
    try:
        held = load(config, name)
    except PipelineError as exc:
        return False, str(exc), ""
    # Stop is handed down. Without it, a pipeline inside a pipeline carried on
    # to the end after somebody pressed Stop, which is the one place the button
    # could be pressed and nothing happen.
    # Stop and "somebody answered" are both handed down. Without the second,
    # a "wait for a person" step inside a nested pipeline was never asked, and
    # came back as nobody answering however loudly somebody answered.
    run = run_it(
        config, held, check_kinds=check_kinds, depth=depth + 1,
        stopping=stopping, waiting_on=waiting_on,
    )
    # Why it failed, not only that it did. Without this the reason is buried a
    # pipeline down, where nobody reading the outer one will find it.
    went_wrong = next((one for one in run.nodes if one.state == FAILED and one.said), None)
    said = f"{name}: {run.said}"
    if went_wrong:
        said = f"{said} ({went_wrong.label}: {went_wrong.said})"
    detail = "\n".join(
        f"{one.label}: {one.state}" for one in run.nodes if not one.skipped_this_time
    )
    return run.passed, said, detail


def _do_one(
    config, node, before, results, order, check_kinds, depth: int = 0,
    stopping=None, waiting_on=None,
) -> tuple[bool, str, str]:
    """One node, once. Kept apart so trying again is plainly the same work."""

    kind = node["kind"]
    if kind == "start":
        return True, "Started", ""
    if kind in GATES:
        return _decide_a_gate(node, before)
    if kind == "suite":
        return _run_a_suite(config, node, check_kinds)
    if kind == "security_scan":
        return _run_security_scan(config, node, check_kinds)
    if kind == "unit_test":
        return _run_unit_test(config, node, check_kinds)
    if kind == "git_repo":
        return _run_git_repo(config, node, check_kinds)
    if kind == "ai_unit_test":
        return _run_ai_unit_test(config, node, check_kinds)
    if kind == "ask_for_help":
        return _ask_for_help(config, node)
    if kind == "another_pipeline":
        return _run_another_pipeline(
            config, node, check_kinds, depth, stopping, waiting_on
        )
    if kind == "artifact":
        # Everything that has finished - not this step itself, which is running
        # right now. Keeping the record of a run is the one job where writing
        # down a half-finished thing about yourself is worst.
        done = [
            results[other]
            for other in order
            if other != node["id"] and results[other].state in (PASSED, FAILED, SKIPPED)
        ]
        return _run_artifact(config, node, check_kinds, so_far=done)
    # read_it refuses an unknown kind long before here, so this is the case
    # where a kind was added to the list and not to the running.
    raise PipelineError(f"{kind} is a kind of node nothing knows how to run")
