"""Named workflows you can keep, switch between, and share.

A workflow is the graph of agents that do a run. Most people end up with more
than one: a quick one for small fixes, a careful one with two reviewers, a
different one for a different project. This keeps them as separate files under
`.harness/workflows`, so they can be looked at, edited, and checked into a
repository like anything else.

Every workflow is validated before it is written, so a saved file is one the
harness can really run.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import LoadedConfig
from .graphs import migrate_graph, validate_graph
from .models import HarnessError
from .safety import confined_path

WORKFLOW_FOLDER = ".harness/workflows"
MAX_WORKFLOWS = 100
MAX_WORKFLOW_BYTES = 2_000_000
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$")
# Windows keeps these names for its own devices, whatever extension follows, so
# a file called con.json cannot be created at all. Refusing them here gives the
# user a sentence they can act on instead of a raw operating system error.
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{digit}" for digit in range(1, 10)}
    | {f"lpt{digit}" for digit in range(1, 10)}
)


class WorkflowError(HarnessError):
    """A workflow name or file problem the user can fix."""


@dataclass(frozen=True)
class SavedWorkflow:
    name: str
    file: str
    graph: dict[str, Any]
    saved_at: str
    nodes: int
    edges: int
    valid: bool
    issues: tuple[str, ...] = ()

    def to_dict(self, include_graph: bool = False) -> dict[str, Any]:
        value: dict[str, Any] = {
            "name": self.name,
            "file": self.file,
            "saved_at": self.saved_at,
            "nodes": self.nodes,
            "edges": self.edges,
            "valid": self.valid,
            "issues": list(self.issues),
        }
        if include_graph:
            value["graph"] = self.graph
        return value


def clean_name(value: object) -> str:
    """A workflow name a person can read and a file system can hold."""

    if not isinstance(value, str):
        raise WorkflowError("A workflow name must be text")
    name = " ".join(value.split())
    if not name:
        raise WorkflowError("A workflow needs a name")
    if not _NAME_PATTERN.fullmatch(name):
        raise WorkflowError(
            "A workflow name may hold letters, digits, spaces, dashes and underscores, "
            "must start with a letter or digit, and may be at most 64 characters"
        )
    return name


def file_name(name: str) -> str:
    """The file one workflow lives in. Two names never collide on one file."""

    stem = re.sub(r"[^a-z0-9]+", "-", clean_name(name).casefold()).strip("-")
    if not stem:
        raise WorkflowError("A workflow name needs at least one letter or digit")
    if stem in _RESERVED_NAMES:
        raise WorkflowError(
            f"{clean_name(name)} is a name Windows keeps for itself, so it cannot be a file. "
            "Pick another one."
        )
    return stem + ".json"


def folder(config: LoadedConfig) -> Path:
    return confined_path(config.project_root, WORKFLOW_FOLDER, allow_missing=True, allow_control=True)


def _read(path: Path) -> SavedWorkflow:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise WorkflowError(f"Cannot read {path.name}: {exc}") from exc
    if size > MAX_WORKFLOW_BYTES:
        raise WorkflowError(f"{path.name} is larger than the {MAX_WORKFLOW_BYTES} byte limit")
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"{path.name} is not readable as JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise WorkflowError(f"{path.name} must hold an object")
    graph = body.get("graph") if isinstance(body.get("graph"), dict) else body
    name = str(body.get("name") or path.stem)
    saved_at = str(body.get("saved_at") or "")
    migrated = migrate_graph(graph)
    issues = tuple(f"{issue.path}: {issue.message}" for issue in validate_graph(migrated))
    return SavedWorkflow(
        name=name,
        file=path.name,
        graph=migrated,
        saved_at=saved_at,
        nodes=len(migrated.get("nodes", [])),
        edges=len(migrated.get("edges", [])),
        valid=not issues,
        issues=issues,
    )


def listed(config: LoadedConfig) -> list[SavedWorkflow]:
    """Every saved workflow, in name order. A broken file is reported, not hidden."""

    base = folder(config)
    if not base.is_dir():
        return []
    found: list[SavedWorkflow] = []
    # Read every file, then cap by name order, so the cap never depends on how
    # the file names happen to sort.
    for path in sorted(base.glob("*.json"))[: MAX_WORKFLOWS * 2]:
        try:
            found.append(_read(path))
        except WorkflowError as exc:
            found.append(
                SavedWorkflow(
                    name=path.stem,
                    file=path.name,
                    graph={},
                    saved_at="",
                    nodes=0,
                    edges=0,
                    valid=False,
                    issues=(str(exc),),
                )
            )
    return sorted(found, key=lambda item: item.name.casefold())[:MAX_WORKFLOWS]


def load(config: LoadedConfig, name: str) -> SavedWorkflow:
    path = folder(config) / file_name(name)
    if not path.is_file():
        known = ", ".join(item.name for item in listed(config)) or "none yet"
        raise WorkflowError(f"There is no workflow named {clean_name(name)}. Saved ones: {known}")
    return _read(path)


def save(
    config: LoadedConfig,
    name: str,
    graph: object,
    *,
    replace: bool = True,
    taking_over: bool = False,
) -> SavedWorkflow:
    """Write one workflow after checking the harness could really run it."""

    clean = clean_name(name)
    if not isinstance(graph, dict):
        raise WorkflowError("A workflow must be an object holding nodes and edges")
    migrated = migrate_graph(graph)
    issues = [f"{issue.path}: {issue.message}" for issue in validate_graph(migrated)]
    if issues:
        raise WorkflowError(
            "This workflow cannot be saved because the harness could not run it: " + "; ".join(issues[:3])
        )
    base = folder(config)
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkflowError(f"Cannot make the workflows folder: {exc}") from exc
    path = base / file_name(clean)
    if path.exists() and not replace:
        raise WorkflowError(f"A workflow named {clean} already exists")
    if path.exists():
        # Two names that differ only in capital letters share one file. Saving
        # over somebody else's workflow by accident is worse than being told to
        # pick another name.
        try:
            already = json.loads(path.read_text(encoding="utf-8")).get("name", "")
        except (OSError, json.JSONDecodeError, AttributeError):
            already = ""
        if already and already != clean and not taking_over:
            raise WorkflowError(
                f"A workflow named {already} is already kept under the same file name. "
                f"Rename it first, or choose a name that differs by more than capital letters."
            )
    if not path.exists() and len(listed(config)) >= MAX_WORKFLOWS:
        raise WorkflowError(f"There are already {MAX_WORKFLOWS} workflows, which is the limit")
    body = {
        "schema_version": 1,
        "name": clean,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "graph": migrated,
    }
    raw = json.dumps(body, indent=2, sort_keys=False) + "\n"
    if len(raw.encode("utf-8")) > MAX_WORKFLOW_BYTES:
        raise WorkflowError(f"This workflow is larger than the {MAX_WORKFLOW_BYTES} byte limit")
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(raw, encoding="utf-8")
    temporary.replace(path)
    return _read(path)


def delete(config: LoadedConfig, name: str) -> str:
    clean = clean_name(name)
    path = folder(config) / file_name(clean)
    if not path.is_file():
        raise WorkflowError(f"There is no workflow named {clean}")
    path.unlink()
    return clean


def rename(config: LoadedConfig, name: str, new_name: str) -> SavedWorkflow:
    """Move one workflow to a new name, leaving no copy behind either way."""

    found = load(config, name)
    if file_name(new_name) == file_name(name):
        # Only the spelling changed, so rewrite the one file in place. This
        # is the one time a name may take over a file another name held.
        return save(config, new_name, found.graph, taking_over=True)
    saved = save(config, new_name, found.graph, replace=False)
    try:
        delete(config, name)
    except (WorkflowError, OSError):
        # Writing the new one worked but removing the old one did not. Two
        # copies would be worse than none, so undo and say what happened.
        try:
            (folder(config) / file_name(new_name)).unlink(missing_ok=True)
        except OSError:
            pass
        raise WorkflowError(
            f"{clean_name(name)} could not be renamed because the old file could not be "
            "removed. Nothing was changed."
        ) from None
    return saved
