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
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import LoadedConfig
from .models import HarnessError
from .safety import confined_path

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
}

# Which gate kinds decide whether the work goes on.
GATES = {"gate", "security_gate"}


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
        allowed = set(KINDS[kind].settings) | {"tries"}
        extra = sorted(set(settings) - allowed)
        if extra:
            raise PipelineError(f"{node_id} has a setting it cannot use: {extra[0]}")
        tries = settings.get("tries", 1)
        if not isinstance(tries, int) or isinstance(tries, bool) or not 1 <= tries <= MOST_TRIES:
            raise PipelineError(f"Tries on {node_id} has to be a whole number from 1 to {MOST_TRIES}")
        for key, value in settings.items():
            if key == "tries":
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
            held = json.loads(path.read_text(encoding="utf-8"))
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
        held = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"{path.name} cannot be read: {exc}") from exc
    there = str(held.get("name") or "") if isinstance(held, dict) else ""
    if there and there.strip().lower() != str(name).strip().lower():
        raise PipelineError(
            f"There is no pipeline called {name}. The one saved under that file "
            f"name is called {there}."
        )
    return path, read_it(held)


def save(config: LoadedConfig, pipeline: Any) -> dict[str, Any]:
    tidy = read_it(pipeline)
    path = file_for(config, tidy["name"])
    # "Nightly build" and "Nightly Build" become the same file name. Writing
    # anyway would throw one of them away without a word.
    if path.is_file():
        try:
            already = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            already = {}
        there = str(already.get("name") or "") if isinstance(already, dict) else ""
        if there and there != tidy["name"]:
            raise PipelineError(
                f"{there} is already saved under that file name, and saving this one "
                f"would write over it. Give {tidy['name']} a name that is different "
                "by more than capitals and spaces."
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tidy, indent=2) + "\n", encoding="utf-8")
    return tidy


def remove(config: LoadedConfig, name: str) -> str:
    path, held = _the_one_called(config, name)
    path.unlink()
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


def _decide_a_gate(node: dict[str, Any], before: list[NodeResult]) -> tuple[bool, str, str]:
    needs = str(node["settings"].get("needs") or "all").lower()
    if needs not in ("all", "any"):
        raise PipelineError("A gate needs either all or any of what came before it")
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
) -> Run:
    """Run a whole pipeline, in order, and say what happened at each step.

    Everything a node does is something the harness can already do on its own.
    What this adds is the order, the gates, and trying again.
    """

    tidy = read_it(pipeline)
    order = in_running_order(tidy)
    by_id = {node["id"]: node for node in tidy["nodes"]}
    coming_from: dict[str, list[str]] = {node_id: [] for node_id in by_id}
    for edge in tidy["edges"]:
        coming_from[edge["to"]].append(edge["from"])

    results: dict[str, NodeResult] = {
        node_id: NodeResult(id=node_id, kind=by_id[node_id]["kind"], label=by_id[node_id]["label"])
        for node_id in by_id
    }
    started_everything = time.monotonic()

    def say(result: NodeResult) -> None:
        if tell:
            tell({"kind": "pipeline_node", "node": result.id, "payload": result.to_dict()})

    for node_id in order:
        node = by_id[node_id]
        result = results[node_id]
        before = [results[other] for other in coming_from[node_id]]
        if stopping and stopping():
            result.state = SKIPPED
            result.said = "The run was stopped"
            say(result)
            continue
        # A node whose way here was blocked never runs. Saying so beats leaving
        # it at waiting, which reads as "still to come".
        blocked = [item for item in before if item.state in (FAILED, SKIPPED)]
        if blocked and node["kind"] not in GATES:
            result.state = SKIPPED
            result.said = f"Skipped, because {blocked[0].label} did not pass"
            say(result)
            continue

        result.state = RUNNING
        say(result)
        started = time.monotonic()
        tries = int(node["settings"].get("tries", 1))
        passed, said, detail = False, "", ""
        for attempt in range(1, tries + 1):
            result.tries = attempt
            try:
                passed, said, detail = _do_one(config, node, before, results, order, check_kinds)
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
            say(result)
        result.state = PASSED if passed else FAILED
        result.said = said
        result.detail = detail
        result.milliseconds = _how_long(started)
        say(result)

    ordered = [results[node_id] for node_id in order]
    failed = [item for item in ordered if item.state == FAILED]
    skipped = [item for item in ordered if item.state == SKIPPED]
    run = Run(
        name=tidy["name"],
        nodes=ordered,
        passed=not failed,
        milliseconds=_how_long(started_everything),
    )
    if failed:
        run.said = f"{len(failed)} step(s) did not pass: " + ", ".join(
            item.label for item in failed[:4]
        )
    elif skipped:
        run.said = f"Everything that ran passed. {len(skipped)} step(s) were skipped."
    else:
        run.said = f"Every one of the {len(ordered)} steps passed."
    return run


def _do_one(config, node, before, results, order, check_kinds) -> tuple[bool, str, str]:
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
