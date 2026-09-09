"""Persistent per-agent candidates; only the goal publisher owns the real project.

Copies are independent files, not OS security sandboxes. Provider permission
enforcement belongs to the native execution backend. State lives outside the
mutable copy and uses the same authenticated storage as goal workspaces.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import time
from contextlib import contextmanager

from . import cancellation
from . import goal_workspaces as gw
from .changes import atomic_write
from .models import HarnessError
from .safety import ProjectTransactionLock, confined_path, portable_relative_path_key

CONTRACT = "nexus-agent-workspace/v1"
_GENERATED = {".git", ".harness", ".nexus-verification", "node_modules", ".venv", "venv", "__pycache__"}


def inventory(root: Path) -> dict:
    result, keys, total = {}, set(), 0
    pending = [root]
    while pending:
        folder = pending.pop()
        for path in sorted(folder.iterdir()):
            cancellation.checkpoint()
            if path.name.casefold() in _GENERATED:
                continue
            relative = path.relative_to(root).as_posix()
            key = portable_relative_path_key(relative)
            if key in keys:
                raise HarnessError("Agent workspace contains aliased paths: " + relative)
            keys.add(key)
            safe = confined_path(root, relative, allow_missing=False)
            info = safe.stat()
            if stat.S_ISDIR(info.st_mode):
                pending.append(safe)
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise HarnessError("Agent workspace needs independent regular files: " + relative)
            total += info.st_size
            if total > 2_000_000_000 or len(result) >= 100_000:
                raise HarnessError("Agent workspace exceeds its copy budget")
            digest = hashlib.sha256()
            with safe.open("rb") as stream:
                before = os.fstat(stream.fileno())
                while chunk := stream.read(1024 * 1024):
                    cancellation.checkpoint()
                    digest.update(chunk)
                after = os.fstat(stream.fileno())
            signature = lambda v: (v.st_dev, v.st_ino, v.st_size, v.st_mtime_ns, v.st_mode, v.st_nlink)
            if signature(info) != signature(before) or signature(before) != signature(after):
                raise HarnessError("Agent file changed while being inspected: " + relative)
            result[relative] = {"sha256": digest.hexdigest(), "size": info.st_size, "mode": stat.S_IMODE(info.st_mode)}
    return result


def binding(goal, agent):
    return gw._digest({"contract": CONTRACT, "goal_id": goal["goal_id"],
        "project": goal["project"], "execution_workspace": goal.get("execution_workspace"),
        "execution_contract": goal.get("execution_contract"),
        "agent": {k: agent.get(k) for k in ("id", "who", "route_binding")}})


def layout(goal, agent, runtime_root):
    if not goal.get("execution_workspace"):
        raise HarnessError("Native agents require a private goal execution workspace")
    gw.validate(goal, runtime_root)
    if not any(one.get("id") == agent.get("id") and binding(goal, one) == binding(goal, agent) for one in goal["agents"]):
        raise HarnessError("The agent does not belong to this goal")
    home = confined_path(Path(runtime_root), "agent-workspaces", allow_control=True)
    # Hash identifiers rather than using provider/user strings as path components.
    slot = gw._digest([goal["goal_id"], agent["id"]])[:32]
    folder = confined_path(home, slot + "/" + binding(goal, agent), allow_control=True)
    return home, folder, folder / "project"


def _read(home, folder):
    path = confined_path(folder, "state.json", allow_control=True, allow_missing=False)
    if path.stat().st_size > 100_000_000:
        raise HarnessError("Agent workspace state exceeds its limit")
    return gw._verify(gw._key(home), json.loads(path.read_text(encoding="utf-8")))


def _write(home, folder, state):
    path = confined_path(folder, "state.json", allow_control=True)
    content = gw._canonical(gw._sign(gw._key(home), state))
    for attempt in range(20):
        try:
            atomic_write(path, content)
            return
        except PermissionError as exc:
            # Windows readers and indexers may briefly prevent atomic replace.
            # Keep the old authenticated state intact; never delete it to retry.
            if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 19:
                raise
            cancellation.checkpoint()
            time.sleep(0.025)


def existing_root(goal, agent, runtime_root):
    home, folder, project = layout(goal, agent, runtime_root)
    if not (folder / "state.json").exists():
        return None
    state = _read(home, folder)
    if state.get("contract") != CONTRACT or state.get("binding") != binding(goal, agent) \
            or state.get("identity") != gw._source_identity(gw._direct(project)):
        raise HarnessError("Agent workspace ownership changed")
    return project


@contextmanager
def workspace(goal, agent, runtime_root):
    """Serialize an agent's tasks; synchronize accepted work without losing edits.

    A sync intent survives interruption. Repeating a partly applied sync accepts
    only the recorded before or after contents, never silently overwrites a third
    version. Old configuration generations are retained for inspection/recovery.
    """
    home, folder, project = layout(goal, agent, runtime_root)
    folder.mkdir(parents=True, exist_ok=True)
    gw._key(home, create=True)
    with ProjectTransactionLock(folder).held():
        source = gw.root(goal, runtime_root)
        incoming = inventory(source)
        if not (folder / "state.json").exists():
            project.mkdir(exist_ok=True)
            if any(project.iterdir()):
                raise HarnessError("Unowned agent workspace is not empty")
            state = {"schema_version": 1, "contract": CONTRACT, "binding": binding(goal, agent),
                     "identity": gw._source_identity(project), "baseline": {}, "sync": None}
            _write(home, folder, state)
        existing_root(goal, agent, runtime_root)
        state = _read(home, folder)
        local = inventory(project)
        if not state.get("sync"):
            old = state["baseline"]
            updates, conflicts = {}, []
            for path in sorted(set(old) | set(incoming)):
                if gw._content(old.get(path)) == gw._content(incoming.get(path)):
                    continue
                actual = gw._content(local.get(path))
                if actual not in (gw._content(old.get(path)), gw._content(incoming.get(path))):
                    conflicts.append(path)
                else:
                    updates[path] = {"before": local.get(path), "after": incoming.get(path)}
            if conflicts:
                raise gw.WorkspaceConflict("Agent changes conflict with accepted team changes", conflicts)
            state["sync"] = {"incoming": incoming, "updates": updates}
            _write(home, folder, state)
        intent = state["sync"]
        if gw._contents(incoming) != gw._contents(intent["incoming"]):
            raise HarnessError("Accepted team files changed during interrupted agent synchronization")
        for path, update in intent["updates"].items():
            cancellation.checkpoint()
            actual = gw._content(local.get(path))
            if actual == gw._content(update["after"]):
                continue
            if actual != gw._content(update["before"]):
                raise gw.WorkspaceConflict("Agent file changed during synchronization", [path])
            if update["after"] is None:
                confined_path(project, path, allow_missing=False).unlink()
            else:
                gw._copy_file(source, project, path, update["after"])
        if inventory(source) != incoming:
            raise HarnessError("Accepted team files changed during agent synchronization")
        state.update(baseline=incoming, sync=None)
        _write(home, folder, state)
        yield AgentWorkspace(project, source, incoming)


class AgentWorkspace:
    def __init__(self, root, source, baseline):
        self.root, self.source, self.baseline = root, source, baseline

    def changes(self, *, max_files=12, max_bytes=4_000_000):
        current = inventory(self.root)
        changes, total = [], 0
        for path in sorted(set(current) | set(self.baseline)):
            if gw._content(current.get(path)) == gw._content(self.baseline.get(path)):
                continue
            one = {"path": path, "reason": "Collected from the agent's working copy"}
            if path not in current:
                one["delete"] = True
            else:
                one["mode"] = current[path]["mode"] & 0o777
                raw = confined_path(self.root, path, allow_missing=False).read_bytes()
                if hashlib.sha256(raw).hexdigest() != current[path]["sha256"]:
                    raise HarnessError("Agent changed its output while Nexus collected it: " + path)
                total += len(raw)
                try:
                    one["content"] = raw.decode("utf-8")
                except UnicodeError:
                    one["content_base64"] = base64.b64encode(raw).decode("ascii")
            changes.append(one)
            if len(changes) > max_files or total > max_bytes:
                raise HarnessError("Agent candidate exceeds the configured publication budget; its copy is retained")
        return changes

    def collect_action(self, action, *, max_bytes, max_files=12):
        from .swarm_work import _validated_changes
        from .changes import FileTransaction
        if action.get("tool_calls") or action.get("needs_files") or action.get("action") not in {"work", "complete", "request_review"}:
            return action
        # Native output and structured proposals must agree if they touch the
        # same path. Apply remaining proposals to the private copy for inspection.
        native = {one["path"]: one for one in self.changes(max_bytes=max_bytes, max_files=max_files)}
        for one in action.get("changes", []):
            prior = native.get(one.get("path"))
            if prior and any((prior.get(key) or "") != (one.get(key) or "")
                             for key in ("content", "content_base64", "delete")):
                raise HarnessError("Native edits and structured proposal disagree: " + str(one.get("path")))
        plans = _validated_changes(self.root, action.get("changes", []))
        if plans:
            FileTransaction(self.root, max_files=max_files, max_bytes=max_bytes).apply(plans)
        changes = self.changes(max_bytes=max_bytes, max_files=max_files)
        return {**action, "changes": changes,
                "_nexus_agent_workspace": str(self.root),
                "_nexus_agent_baseline": {one["path"]: self.baseline.get(one["path"]) for one in changes}}


def descriptors(goal, runtime_root):
    entries = [{"id": "real", "label": "Real project", "path": str(goal["project"]["path"]), "available": True}]
    if goal.get("execution_workspace"):
        for agent in goal.get("agents", []):
            root = existing_root(goal, agent, runtime_root)
            entries.append({"id": str(agent["id"]), "label": str(agent["name"]) + "'s copy",
                            "path": str(root or layout(goal, agent, runtime_root)[2]), "available": root is not None})
    return entries


def inspect(goal, runtime_root, workspace_id, relative="", *, cursor=""):
    """Read-only UI browsing: callers select an owned ID, never an absolute root."""
    if workspace_id == "real":
        root = gw._direct(Path(goal["project"]["path"]))
    else:
        agent = next((one for one in goal.get("agents", []) if one["id"] == workspace_id), None)
        if agent is None:
            raise HarnessError("This workspace does not belong to the selected goal")
        root = existing_root(goal, agent, runtime_root)
        if root is None:
            raise HarnessError("This agent's copy will be created when it starts work")
    target = root if relative in ("", ".") else confined_path(root, relative, allow_missing=False)
    if any(part.casefold() in _GENERATED for part in Path(relative).parts):
        raise HarnessError("Runtime and dependency folders are not shown in the workspace viewer")
    if target.is_dir():
        entries = []
        for path in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold())):
            if path.name.casefold() in _GENERATED:
                continue
            path = confined_path(root, path.relative_to(root).as_posix(), allow_missing=False)
            entries.append({"name": path.name, "path": path.relative_to(root).as_posix(),
                            "directory": path.is_dir()})
            if len(entries) > 2000:
                raise HarnessError("This folder has too many entries for the workspace viewer")
        return {"kind": "directory", "path": relative, "entries": entries, "root": str(root)}
    if target.stat().st_size > 8_000_000:
        return {"kind": "binary", "path": relative, "size": target.stat().st_size, "root": str(root)}
    from .bounded_file_read import read_file_page
    raw = target.read_bytes()
    try:
        if not relative.lower().endswith(".docx"):
            raw.decode("utf-8")
        page = read_file_page(raw, {"path": relative, "start_line": 1, "end_line": 10_000_000,
            "max_bytes": 12000, "cursor": cursor},
            output_limit=20000, configured_output_limit=20000, max_file_bytes=8_000_000)
    except UnicodeError:
        return {"kind": "binary", "path": relative, "size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(), "root": str(root)}
    return {"kind": "file", "root": str(root), **page}
