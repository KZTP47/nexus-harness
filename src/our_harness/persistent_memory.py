"""Project-bound Obsidian memory enforced at workflow session boundaries.

The vault is deliberately outside the project and may never be inside a Git
worktree.  A binding file ties it to one canonical project root.  LangGraph is
used as the small lifecycle graph that dispatches every invocation to exactly
one of the mandatory pre-work (consult) and post-work (record) hooks.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Literal, TypedDict

from .config import LoadedConfig, load_config
from .models import HarnessError
from .persistent_memory_index import INDEX_FOLDER, VaultMemoryIndex, _is_link_or_junction
from .persistent_memory_structure import (
    START_HERE_NOTE,
    TOPICS,
    TOPIC_INDEX_NOTE,
    enrich_session_note,
    ensure_information_architecture,
    neutralize_structure_markers,
    organization_status,
)
from .safety import put_this_file_in_place, read_this_file_patiently


BINDING_FILE = ".nexus-project-memory.json"
SESSIONS_FOLDER = "Sessions"
PINNED_NOTE = "Project Memory.md"
PROJECT_MEMORY_FOLDER = Path("01 Project Memory")
START_NOTES = (
    Path(PINNED_NOTE),
    START_HERE_NOTE,
    PROJECT_MEMORY_FOLDER / "How To Use This Vault.md",
    PROJECT_MEMORY_FOLDER / "Codex Working Memory.md",
    PROJECT_MEMORY_FOLDER / "Current State.md",
    PROJECT_MEMORY_FOLDER / "AI Engineering Guide.md",
)
MAX_NOTES = 2_000
MAX_NOTE_CHARS = 20_000
DEPLOYMENT_LOCK = Path(".harness") / "desktop-deployment.lock"
DEPLOYMENT_LOCK_OWNER = Path(".harness") / "desktop-deployment.owner.json"
DESKTOP_CLOSEOUT_MIN_TIMEOUT_SECONDS = 900.0
CURRENT_STATE_START = "<!-- nexus-managed-current:start -->"
CURRENT_STATE_END = "<!-- nexus-managed-current:end -->"
POST_MEMORY_LOCK = Path(INDEX_FOLDER) / "post-memory.lock"
POST_MEMORY_LOCK_TIMEOUT_SECONDS = 60.0


def _neutralize_managed_sentinels(value: str) -> str:
    """Keep untrusted task/outcome prose from manufacturing managed blocks."""

    neutralized = (
        value.replace(CURRENT_STATE_START, "[nexus managed marker removed: start]")
        .replace(CURRENT_STATE_END, "[nexus managed marker removed: end]")
    )
    return neutralize_structure_markers(neutralized)


def _managed_current_bounds(content: str) -> tuple[int, int]:
    """Return the sole managed block bounds, failing closed on ambiguity."""

    starts = [match.start() for match in re.finditer(re.escape(CURRENT_STATE_START), content)]
    ends = [match.start() for match in re.finditer(re.escape(CURRENT_STATE_END), content)]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise HarnessError(
            "Persistent-memory Current State must contain exactly one well-ordered managed block"
        )
    return starts[0], ends[0] + len(CURRENT_STATE_END)


@contextmanager
def _post_memory_lock(vault_root: Path, timeout_seconds: float = POST_MEMORY_LOCK_TIMEOUT_SECONDS):
    """Serialize canonical post-memory writes and their derived-index refresh."""

    if timeout_seconds <= 0:
        raise HarnessError("Persistent-memory post lock timeout must be greater than zero")
    vault = vault_root.resolve(strict=True)
    index_root = vault / INDEX_FOLDER
    if index_root.exists() and _is_link_or_junction(index_root):
        raise HarnessError("Persistent-memory index folder must not be a link or junction")
    index_root.mkdir(parents=True, exist_ok=True)
    if not _inside(index_root.resolve(strict=True), vault):
        raise HarnessError("Persistent-memory post lock escaped the bound vault")
    lock_path = vault / POST_MEMORY_LOCK
    if lock_path.exists() and _is_link_or_junction(lock_path):
        raise HarnessError("Persistent-memory post lock must not be a link or junction")
    stream = lock_path.open("a+b")
    stream.seek(0, os.SEEK_END)
    if stream.tell() == 0:
        stream.write(b"\0")
        stream.flush()
    acquired = False
    started = time.monotonic()
    try:
        while not acquired:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (OSError, BlockingIOError):
                if time.monotonic() - started >= timeout_seconds:
                    raise HarnessError(
                        "Timed out waiting for the bound vault's persistent-memory post lock"
                    )
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


VAULT_SCAFFOLD = {
    PROJECT_MEMORY_FOLDER / "How To Use This Vault.md": """---
kind: agent-memory-guide
project: Nexus Harness
private: true
---

# How to use this vault

This vault is the durable second brain for Nexus Harness agents. It is not a transcript archive to read wholesale.

## Start-of-session retrieval order

1. Read `Project Memory.md` for the ownership and privacy boundary.
2. Open `Start Here.md` for the curated navigation map.
3. Read `Codex Working Memory.md` for stable facts and invariants.
4. Read `Current State.md` for the newest operational truth and next step.
5. Read `AI Engineering Guide.md` before implementation.
6. Use the private SQLite/FTS index to retrieve task-relevant notes and then verify important claims against current repository or runtime evidence.

The pre-work hook performs this retrieval automatically. `python scripts/persistent_memory_hook.py search --project . --query "terms"` is available for focused follow-up.

## What belongs where

- `Codex Working Memory.md`: durable constraints, proven architecture facts, fragile areas, and established workflows.
- `Current State.md`: changing status, blockers, latest verification, and the next concrete action.
- `Sessions/`: append-only chronological evidence produced by the post-work hook.
- `02 Topics/`: subsystem maps. Session metadata and body links are generated automatically, so each map's Backlinks pane is the historical evidence browser.

## Evidence and correction rules

- Current repository files and fresh runtime evidence outrank memory.
- Preserve historical session notes. Correct stale claims in a newer note or living summary.
- Record conclusions, paths, symbols, commands, and results; do not dump source files or raw command noise.
- Keep secrets, credentials, provider transcripts, and full patches out of durable memory.
- Obsidian Markdown is canonical. `.nexus-memory/memory-index.sqlite3` is generated, private, and disposable.
- This project intentionally has no operator-facing memory dashboard.

## End-of-session rules

The post-work hook must pass the desktop rebuild/installer/shortcut gate before recording a session. Agents should supply a bounded result with `state`, `summary`, `verification`, `next_step`, and `blockers` when those fields are known.
""",
    PROJECT_MEMORY_FOLDER / "Codex Working Memory.md": """---
kind: codex-working-memory
project: Nexus Harness
private: true
---

# Codex Working Memory

## Durable project identity

- Nexus Harness is a local-first Python 3.11+ test lab and coding assistant with an Electron desktop shell.
- The two primary product workspaces are the AI Agent Swarm orchestrator and visual test automation.
- Python owners live under `src/our_harness/`; desktop ownership lives under `desktop/`; verification lives under `tests/` and Electron `*.test.js`/smoke scripts.

## Non-negotiable invariants

- Current source and runtime evidence outrank this vault.
- Project and agent boundaries must fail closed; ordinary chat is read-only and file-changing work uses an explicit bounded workflow.
- Private runtime data, chats, local configuration, credentials, and this external vault must never be staged or published.
- Existing user changes in a dirty worktree are preserved unless the user explicitly scopes them into the task.
- A substantive session is not closed until the mandatory post-work deployment gate rebuilds the app and installer and refreshes the desktop shortcut/icon.

## Memory architecture

- Obsidian Markdown is canonical durable memory.
- `Sessions/` is an append-only audit trail, not the primary working set.
- `.nexus-memory/memory-index.sqlite3` is a generated SQLite FTS5 retrieval index plus hook-health KV state. It may be rebuilt from Markdown.
- Pre-work retrieval always includes the curated startup notes, then adds task-relevant indexed notes and recent session evidence within a bounded context budget.
- `Start Here.md` and `02 Topics/` are the visible navigation layer. The post hook classifies every session with area/status/components/topics and creates graph/backlink edges automatically.

## Fragile or high-risk surfaces

- Provider/browser send ambiguity must stop rather than risk duplicate delivery.
- Saved chat identity, archive/restore behavior, and pair/project boundaries are durable user state.
- Run checkpoints, deadlines, transaction rollback, and cancellation must survive interruption without silently renewing budgets or losing evidence.
- Desktop closeout may stop only the exact executable owned by this checkout when replacing a locked build artifact.

## Update discipline

Add only verified, durable facts here. Put changing status in `Current State.md` and chronological detail in `Sessions/`.
""",
    PROJECT_MEMORY_FOLDER / "Current State.md": f"""---
kind: current-state
project: Nexus Harness
private: true
---

# Current State

This is the compact operational truth for the next agent. The managed block is refreshed by the post-work hook; add durable facts to `Codex Working Memory.md` instead.

{CURRENT_STATE_START}
No post-work session has refreshed this managed state yet.
{CURRENT_STATE_END}
""",
    PROJECT_MEMORY_FOLDER / "AI Engineering Guide.md": """---
kind: ai-engineering-guide
project: Nexus Harness
private: true
---

# AI Engineering Guide

## Orient before editing

1. Use the pre-work hook and consult its bounded memory packet.
2. Inspect `git status --short`; preserve unrelated user work.
3. Read the nearest owner and focused tests before broad searching.
4. Treat memory as historical evidence and verify any change-driving claim in current files or runtime output.

## Ownership map

- `src/our_harness/workflow.py`: run lifecycle, planning/coding/review orchestration, checkpoints, and persistent-memory integration.
- `src/our_harness/memory.py`: operational SQLite persistence for runs and events; it is not the Obsidian knowledge base.
- `src/our_harness/context.py`: bounded agent prompt/context compilation.
- `src/our_harness/server.py`: local HTTP API and UI-facing orchestration.
- `src/our_harness/swarm*.py`, `cooperation.py`, and `agent_mailbox.py`: multi-agent/project collaboration behavior.
- `src/our_harness/pipelines.py` and `pipeline_runs.py`: visual automation definitions and execution.
- `desktop/`: Electron packaging, preload/main-process boundaries, and packaged smoke tests.
- `tests/`: behavioral regression contracts; prefer the narrowest relevant test module first.

## Change discipline

- Patch the narrowest cohesive owner and avoid expanding already-large orchestration files when a stable helper boundary exists.
- For new or materially changed behavior, cover the strongest practical unit, happy-path, negative/failure, and persistence/restart lenses. A lens may share a test; record why an impractical lens was skipped.
- Keep provider output untrusted until schema, path, permission, and requirement-ledger checks pass.
- Use atomic/recoverable writes for durable local state and explicit ownership for locks and process termination.
- Never weaken vault binding, Git exclusion, redaction, bounded-context, or closeout deployment rules as a convenience.

## Verification order

1. Syntax/import checks for touched modules.
2. Focused Python or Electron tests for the changed owner.
3. Relevant integration/regression suites proportional to risk.
4. Inspect the diff for accidental changes and private-path leakage.
5. Run the mandatory post-work hook; do not claim closure if its deployment gate fails.
""",
}


def _is_owned_build_lock_failure(detail: str) -> bool:
    """Recognize Windows' equivalent wordings for a locked unpacked build."""

    folded = detail.casefold()
    lock_wording = (
        "access is denied" in folded
        or "being used by another process" in folded
        or "cannot access the file" in folded
    )
    return lock_wording and "win-unpacked" in folded


def _deployment_lock_owner(project_root: Path) -> dict[str, Any]:
    try:
        value = json.loads(read_this_file_patiently(project_root / DEPLOYMENT_LOCK_OWNER))
        return value if isinstance(value, dict) else {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}


@contextmanager
def _checkout_deployment_lock(
    project_root: Path,
    timeout_seconds: float,
    *,
    purpose: str = "persistent-memory desktop closeout",
):
    """Serialize the complete checkout-owned desktop deployment across processes.

    The OS lock is authoritative. Metadata is deliberately not used to break a
    lock: after a crash the kernel releases the lock with the dead process, and
    only then may the next owner overwrite the stale diagnostic record.
    """

    if timeout_seconds <= 0:
        raise HarnessError("Desktop deployment lock timeout must be greater than zero")
    root = project_root.resolve()
    lock_path = root / DEPLOYMENT_LOCK
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    stream = lock_path.open("a+b")
    stream.seek(0, os.SEEK_END)
    if stream.tell() == 0:
        stream.write(b"\0")
        stream.flush()
    started = time.monotonic()
    acquired = False
    try:
        while not acquired:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (OSError, BlockingIOError):
                if time.monotonic() - started >= timeout_seconds:
                    owner = _deployment_lock_owner(root)
                    detail = json.dumps(owner, sort_keys=True) if owner else "unreadable owner metadata"
                    raise HarnessError(
                        "Timed out waiting for this checkout's desktop deployment lock; "
                        f"the live OS lock was not broken. Current owner: {detail}"
                    )
                time.sleep(0.1)
        owner = {
            "schema_version": 1,
            "state": "owning",
            "pid": os.getpid(),
            "project_root": str(root),
            "purpose": purpose,
            "acquired_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        put_this_file_in_place(
            root / DEPLOYMENT_LOCK_OWNER,
            json.dumps(owner, indent=2, sort_keys=True) + "\n",
        )
        yield owner
        owner["state"] = "released"
        owner["released_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        put_this_file_in_place(
            root / DEPLOYMENT_LOCK_OWNER,
            json.dumps(owner, indent=2, sort_keys=True) + "\n",
        )
    finally:
        if acquired:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


class MemoryHookState(TypedDict, total=False):
    phase: Literal["pre", "post"]
    task: str
    result: dict[str, Any]
    context: str
    consulted: list[str]
    index_status: dict[str, Any]
    deployment: dict[str, Any]
    written: str


def _canonical(path: Path) -> str:
    value = str(path.resolve())
    return value.casefold() if os.name == "nt" else value


def _digest_path(path: Path) -> str:
    return hashlib.sha256(_canonical(path).encode("utf-8")).hexdigest()


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _git_ancestor(path: Path) -> Path | None:
    current = path.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def binding_for(project_root: Path, vault_root: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "project_root": str(project_root.resolve()),
        "project_sha256": _digest_path(project_root),
        "vault_root": str(vault_root.resolve()),
        "github_upload_allowed": False,
    }


def _ensure_vault_scaffold(vault_root: Path) -> list[str]:
    """Create and maintain the curated, human-readable vault structure."""

    structure = ensure_information_architecture(
        vault_root,
        navigation_notes=(Path(PINNED_NOTE), *VAULT_SCAFFOLD.keys()),
        additional_scaffold=VAULT_SCAFFOLD,
    )
    return [str(path) for path in structure["created"]]


def initialize_vault(project_root: Path, vault_root: Path) -> Path:
    """Explicitly bind a non-Git vault to exactly one project."""

    project_root = project_root.resolve()
    vault_root = vault_root.resolve()
    if not project_root.is_dir():
        raise HarnessError(f"Persistent-memory project root does not exist: {project_root}")
    if not vault_root.is_dir():
        raise HarnessError(f"Persistent-memory vault does not exist: {vault_root}")
    if _inside(vault_root, project_root) or _inside(project_root, vault_root):
        raise HarnessError("Persistent-memory vault and project root must be separate directory trees")
    git_root = _git_ancestor(vault_root)
    if git_root is not None:
        raise HarnessError(f"Persistent-memory vault must not be inside a Git worktree: {git_root}")
    binding_path = vault_root / BINDING_FILE
    expected = binding_for(project_root, vault_root)
    if binding_path.exists():
        try:
            current = json.loads(read_this_file_patiently(binding_path))
        except (OSError, json.JSONDecodeError) as exc:
            raise HarnessError("Persistent-memory vault binding is unreadable") from exc
        if current != expected:
            raise HarnessError("Persistent-memory vault is already bound to a different project or path")
    else:
        put_this_file_in_place(binding_path, json.dumps(expected, indent=2, sort_keys=True) + "\n")
    ignore_path = vault_root / ".gitignore"
    required = "# This private vault must never be committed or uploaded.\n*\n!.gitignore\n"
    if not ignore_path.exists() or read_this_file_patiently(ignore_path) != required:
        put_this_file_in_place(ignore_path, required)
    pinned = vault_root / PINNED_NOTE
    if not pinned.exists():
        put_this_file_in_place(
            pinned,
            "---\nkind: project-memory-policy\nproject: Nexus Harness\nprivate: true\n"
            "github_upload_allowed: false\n---\n\n# Nexus Harness project memory\n\n"
            "This Obsidian vault is persistent memory for the Nexus Harness project only.\n\n"
            "- Consult this vault before work begins.\n"
            "- Record a bounded session note after every session, including failures and pauses.\n"
            "- Current repository files and fresh runtime evidence outrank stale memory.\n"
            "- Never copy, commit, bundle, publish, or upload this vault to GitHub.\n"
            "- Never use this vault as memory for another project.\n",
        )
    _ensure_vault_scaffold(vault_root)
    return binding_path


class PersistentMemoryHooks:
    """Compile and invoke the mandatory LangGraph lifecycle hooks."""

    def __init__(
        self,
        config: LoadedConfig,
        *,
        redact_text: Callable[[str], str] | None = None,
        redact_value: Callable[[Any], Any] | None = None,
        deploy_desktop: Callable[[Path], dict[str, Any]] | None = None,
    ):
        self.config = config
        self.enabled = bool(config.get("persistent_memory.enabled", False))
        self.redact_text = redact_text or (lambda value: value)
        self.redact_value = redact_value or (lambda value: value)
        self.max_context_chars = int(config.get("persistent_memory.max_context_chars", 20_000))
        self.enforce_desktop_deployment = bool(
            config.get("persistent_memory.enforce_desktop_deployment", False)
        )
        self.deploy_desktop = deploy_desktop or self._deploy_desktop
        self.vault_root: Path | None = None
        self.memory_index: VaultMemoryIndex | None = None
        self.graph: Any = None
        if not self.enabled:
            return
        configured = str(config.get("persistent_memory.vault_path", "")).strip()
        if not configured:
            raise HarnessError("persistent_memory.vault_path is required when persistent memory is enabled")
        configured_path = Path(configured).expanduser()
        if _is_link_or_junction(configured_path):
            raise HarnessError("Persistent-memory vault root must not be a link or junction")
        self.vault_root = configured_path.resolve()
        self._verify_binding()
        _ensure_vault_scaffold(self.vault_root)
        self.memory_index = VaultMemoryIndex(self.vault_root)
        try:
            from langgraph.graph import END, START, StateGraph
        except ImportError as exc:
            raise HarnessError(
                "Persistent memory requires LangGraph; install the project dependencies first"
            ) from exc
        lifecycle = StateGraph(MemoryHookState)
        lifecycle.add_node("refresh_private_memory_index_before_work", self._refresh_before_node)
        lifecycle.add_node("consult_vault_before_work", self._consult_node)
        lifecycle.add_node("rebuild_app_and_installer", self._deploy_node)
        lifecycle.add_node("write_vault_after_work", self._write_node)
        lifecycle.add_node("refresh_private_memory_index_after_work", self._refresh_after_node)
        lifecycle.add_conditional_edges(
            START,
            lambda state: state["phase"],
            {"pre": "refresh_private_memory_index_before_work", "post": "rebuild_app_and_installer"},
        )
        lifecycle.add_edge("refresh_private_memory_index_before_work", "consult_vault_before_work")
        lifecycle.add_edge("consult_vault_before_work", END)
        lifecycle.add_edge("rebuild_app_and_installer", "write_vault_after_work")
        lifecycle.add_edge("write_vault_after_work", "refresh_private_memory_index_after_work")
        lifecycle.add_edge("refresh_private_memory_index_after_work", END)
        self.graph = lifecycle.compile()

    def _verify_binding(self) -> None:
        assert self.vault_root is not None
        vault = self.vault_root
        project = self.config.project_root.resolve()
        if not vault.is_dir():
            raise HarnessError(f"Persistent-memory vault does not exist: {vault}")
        if _inside(vault, project) or _inside(project, vault):
            raise HarnessError("Persistent-memory vault and project root must be separate directory trees")
        git_root = _git_ancestor(vault)
        if git_root is not None:
            raise HarnessError(f"Persistent-memory vault must not be inside a Git worktree: {git_root}")
        binding_path = vault / BINDING_FILE
        try:
            binding = json.loads(read_this_file_patiently(binding_path))
        except FileNotFoundError as exc:
            raise HarnessError("Persistent-memory vault has no project binding; initialize it explicitly") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise HarnessError("Persistent-memory vault binding is unreadable") from exc
        if binding != binding_for(project, vault):
            raise HarnessError("Persistent-memory vault binding does not match this project")
        if binding.get("github_upload_allowed") is not False:
            raise HarnessError("Persistent-memory vault binding must prohibit GitHub upload")

    def before_session(self, task: str) -> tuple[str, list[str]]:
        if not self.enabled:
            return "", []
        if not task.strip():
            raise HarnessError("Persistent-memory pre-work task must not be empty")
        self._verify_binding()
        output = self.graph.invoke({"phase": "pre", "task": task})
        return str(output.get("context", "")), list(output.get("consulted", []))

    def after_session(self, task: str, result: dict[str, Any]) -> str:
        if not self.enabled:
            return ""
        if not task.strip():
            raise HarnessError("Persistent-memory post-work task must not be empty")
        self._verify_binding()
        output = self.graph.invoke({"phase": "post", "task": task, "result": result})
        return str(output.get("written", ""))

    def status(self) -> dict[str, Any]:
        if not self.enabled or self.memory_index is None or self.vault_root is None:
            return {"enabled": False}
        self._verify_binding()
        refreshed = self.memory_index.refresh()
        return {
            "enabled": True,
            "binding_valid": True,
            "required_notes": {
                relative.as_posix(): (self.vault_root / relative).is_file()
                for relative in START_NOTES
            },
            "refresh": refreshed,
            "index": self.memory_index.status(),
            "organization": organization_status(self.vault_root),
        }

    def search(self, query: str, *, limit: int = 8) -> list[dict[str, Any]]:
        if not self.enabled or self.memory_index is None:
            return []
        if not query.strip():
            raise HarnessError("Persistent-memory search query must not be empty")
        self._verify_binding()
        self.memory_index.refresh()
        return self.memory_index.search(query, limit=limit)

    def _refresh_before_node(self, state: MemoryHookState) -> MemoryHookState:
        assert self.memory_index is not None
        status = self.memory_index.refresh()
        self._validate_index_health(status)
        task = str(state.get("task", ""))
        self.memory_index.set_kv(
            "hook.last_pre",
            {
                "task_sha256": hashlib.sha256(task.encode("utf-8")).hexdigest(),
                "index_files": status["files"],
                "index_chunks": status["chunks"],
                "recorded_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        )
        return {"index_status": status}

    def _read_vault_note(self, relative: Path) -> str:
        assert self.vault_root is not None
        raw_path = self.vault_root / relative
        if _is_link_or_junction(raw_path):
            raise HarnessError(
                f"Persistent-memory note must not be a link or junction: {relative.as_posix()}"
            )
        try:
            path = raw_path.resolve(strict=True)
        except FileNotFoundError:
            return ""
        if not _inside(path, self.vault_root) or not path.is_file():
            raise HarnessError(f"Persistent-memory note escaped the bound vault: {relative.as_posix()}")
        try:
            return read_this_file_patiently(path)
        except (OSError, UnicodeError) as exc:
            raise HarnessError(f"Persistent-memory note is unreadable: {relative.as_posix()}") from exc

    def _validate_index_health(
        self, status: dict[str, Any], *, additional_paths: tuple[str, ...] = (),
    ) -> None:
        assert self.memory_index is not None
        required = [relative.as_posix() for relative in START_NOTES]
        required.append(TOPIC_INDEX_NOTE.as_posix())
        required.extend(topic.path.as_posix() for topic in TOPICS)
        required.extend(additional_paths)
        indexed = self.memory_index.contains_paths(required)
        missing = [path for path, present in indexed.items() if not present]
        if missing:
            raise HarnessError(
                "Persistent-memory integrity gate found required notes missing from the index: "
                + ", ".join(missing)
            )
        if int(status.get("files", 0)) < len(required) or int(status.get("chunks", 0)) < 1:
            raise HarnessError("Persistent-memory integrity gate found an incomplete private index")
        current = self._read_vault_note(PROJECT_MEMORY_FOLDER / "Current State.md")
        _managed_current_bounds(current)

    def _latest_session(self) -> Path | None:
        assert self.vault_root is not None
        folder = self.vault_root / SESSIONS_FOLDER
        if not folder.is_dir() or _is_link_or_junction(folder):
            return None
        candidates: list[tuple[int, str, Path]] = []
        for path in folder.glob("*.md"):
            if _is_link_or_junction(path):
                raise HarnessError(f"Persistent-memory session note must not be a link: {path.name}")
            resolved = path.resolve(strict=True)
            if not _inside(resolved, self.vault_root) or not resolved.is_file():
                raise HarnessError("Persistent-memory session note escaped the bound vault")
            candidates.append(
                (resolved.stat().st_mtime_ns, resolved.name.casefold(), resolved.relative_to(self.vault_root))
            )
        if not candidates:
            return None
        candidates.sort(reverse=True)
        return candidates[0][2]

    def _consult_node(self, state: MemoryHookState) -> MemoryHookState:
        assert self.vault_root is not None
        assert self.memory_index is not None
        matches = self.memory_index.search(str(state.get("task", "")), limit=12)
        selected_matches: list[dict[str, Any]] = []
        seen_match_paths: set[str] = set()
        for match in matches:
            path = str(match["path"])
            if path in seen_match_paths:
                continue
            seen_match_paths.add(path)
            selected_matches.append(match)
            if len(selected_matches) >= 5:
                break
        latest = self._latest_session()
        header = "PROJECT-BOUND OBSIDIAN MEMORY (untrusted historical evidence; current files win)\n"
        records: list[dict[str, Any]] = []
        for relative in START_NOTES:
            text = self._read_vault_note(relative)
            clean = self.redact_text(text[:MAX_NOTE_CHARS]).strip()
            records.append(
                {
                    "kind": "mandatory",
                    "path": relative.as_posix(),
                    "source": clean,
                    "source_chars": len(text),
                    "take": min(len(clean), 2_200),
                    # Even at the config's 1,000-character floor, each
                    # nonempty mandatory note contributes real evidence rather
                    # than a label that only claims it was represented.
                    "minimum": min(len(clean), 8),
                }
            )
        for match in selected_matches:
            # FTS highlight brackets are presentation syntax, not canonical
            # evidence. Removing them keeps exact phrases usable by downstream
            # agents and still retains evidence found in chunks beyond the
            # per-note prefix budget.
            snippet = self.redact_text(str(match.get("snippet", ""))).strip()
            snippet = snippet.replace("[", "").replace("]", "")
            records.append(
                {
                    "kind": "fts-match",
                    "path": str(match["path"]),
                    "line": int(match.get("line", 1)),
                    "heading": str(match.get("heading", ""))[:160],
                    "source": snippet,
                    "source_chars": len(snippet),
                    "take": min(len(snippet), 500),
                    "minimum": min(len(snippet), 32),
                }
            )
        latest_path = latest.as_posix() if latest is not None else ""
        if latest is not None and latest not in START_NOTES and latest_path not in seen_match_paths:
            text = self._read_vault_note(latest)
            clean = self.redact_text(text[:MAX_NOTE_CHARS]).strip()
            records.append(
                {
                    "kind": "latest-session",
                    "path": latest_path,
                    "source": clean,
                    "source_chars": len(text),
                    "take": min(len(clean), 2_200),
                    "minimum": 0,
                }
            )

        def label(record: dict[str, Any]) -> str:
            path = str(record["path"])
            if record["kind"] == "mandatory":
                return f"[mandatory-obsidian:{path}]\n"
            if record["kind"] == "fts-match":
                heading = str(record.get("heading", ""))
                suffix = f"; heading={heading}" if heading else ""
                return f"[fts-match:{path}#L{record['line']}{suffix}]\n"
            return f"[latest-session:{path}]\n"

        def retrieval_metadata() -> dict[str, Any]:
            mandatory_records = [one for one in records if one["kind"] == "mandatory"]
            included_fts = [one for one in records if one["kind"] == "fts-match"]
            latest_record = next(
                (one for one in records if one["kind"] == "latest-session"), None
            )
            truncated = [
                str(one["path"])
                for one in records
                if int(one["take"]) < int(one["source_chars"])
            ]
            omitted_match_paths = [
                str(one["path"])
                for one in matches
                if str(one["path"]) not in {str(hit["path"]) for hit in included_fts}
            ]
            omitted = list(dict.fromkeys(omitted_match_paths))
            if latest_path and latest_record is not None and int(latest_record["take"]) == 0:
                omitted.append(latest_path)
            return {
                "budget_chars": self.max_context_chars,
                "mandatory": {
                    "total": len(START_NOTES),
                    "included": len(START_NOTES),
                    "all_represented": all(
                        int(one["source_chars"]) == 0 or int(one["take"]) > 0
                        for one in mandatory_records
                    ),
                    "content_chars": {
                        str(one["path"]): int(one["take"])
                        for one in mandatory_records
                    },
                },
                "fts": {
                    "matches": len(matches),
                    "selected_evidence": len(included_fts),
                    "omitted": max(0, len(matches) - len(included_fts)),
                },
                "latest_session_included": bool(
                    latest_record is not None and int(latest_record["take"]) > 0
                ),
                "truncated_paths": list(dict.fromkeys(truncated)),
                "omitted_paths": list(dict.fromkeys(omitted)),
            }

        def render() -> tuple[str, dict[str, Any]]:
            metadata = retrieval_metadata()
            if self.max_context_chars >= 2_000:
                context_metadata = metadata
            else:
                context_metadata = {
                    "budget_chars": self.max_context_chars,
                    "mandatory_all_represented": metadata["mandatory"]["all_represented"],
                    "mandatory_content_min_chars": min(
                        metadata["mandatory"]["content_chars"].values(), default=0
                    ),
                    "fts_selected": metadata["fts"]["selected_evidence"],
                    "fts_omitted": metadata["fts"]["omitted"],
                    "truncated_count": len(metadata["truncated_paths"]),
                    "omitted_count": len(metadata["omitted_paths"]),
                    "paths_available_in_hook_metadata": True,
                }
            metadata_text = json.dumps(context_metadata, ensure_ascii=False, sort_keys=True)
            blocks = []
            for record in records:
                if record["kind"] == "latest-session" and int(record["take"]) == 0:
                    continue
                excerpt = str(record["source"])[: int(record["take"])]
                blocks.append(label(record) + (excerpt or "(empty note)") + "\n")
            return (
                header
                + "[retrieval-metadata]\n"
                + metadata_text
                + "\n"
                + "\n".join(blocks),
                metadata,
            )

        context, metadata = render()
        while len(context) > self.max_context_chars:
            overflow = len(context) - self.max_context_chars
            shrinkable = [
                one for one in reversed(records)
                if int(one["take"]) > int(one["minimum"])
            ]
            if shrinkable:
                record = max(shrinkable, key=lambda one: int(one["take"]) - int(one["minimum"]))
                record["take"] = max(
                    int(record["minimum"]), int(record["take"]) - overflow
                )
                context, metadata = render()
                continue
            optional_fts = [one for one in records if one["kind"] == "fts-match"]
            if len(optional_fts) > 1:
                records.remove(optional_fts[-1])
                context, metadata = render()
                continue
            # Configuration validation allows a 1,000-character floor, which
            # comfortably fits the five mandatory labels, one FTS evidence
            # excerpt, and compact metadata. This guard remains fail closed if
            # an unusually long path defeats that invariant.
            raise HarnessError(
                "Persistent-memory context budget is too small to represent all mandatory notes"
            )

        consulted = list(dict.fromkeys(
            [relative.as_posix() for relative in START_NOTES]
            + [str(one["path"]) for one in records if one["kind"] == "fts-match"]
            + ([latest_path] if metadata["latest_session_included"] else [])
        ))
        metadata["included_chars"] = len(context)
        self.memory_index.set_kv(
            "hook.last_pre",
            {
                "task_sha256": hashlib.sha256(
                    str(state.get("task", "")).encode("utf-8")
                ).hexdigest(),
                "consulted": consulted,
                "included_chars": len(context),
                "retrieval": metadata,
                "recorded_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        )
        return {"context": context, "consulted": consulted}

    def _deploy_node(self, _state: MemoryHookState) -> MemoryHookState:
        """Fail closed until the current Electron app, installer, and icon are refreshed."""

        if not self.enforce_desktop_deployment:
            return {}
        return {"deployment": self.deploy_desktop(self.config.project_root.resolve())}

    def _deploy_desktop(self, project_root: Path) -> dict[str, Any]:
        if os.name != "nt":
            raise HarnessError("The enforced Nexus Harness desktop closeout is Windows-only")
        # Ordinary test/tool commands default to a short timeout. Closeout also
        # builds a validated private runtime, may wait for legitimate consumers
        # at its atomic swap, packages Electron/NSIS, and refreshes the shortcut.
        # Give that deployment transaction its own non-escalating floor.
        timeout = max(
            float(self.config.get("execution.timeout_seconds", 3_600)),
            DESKTOP_CLOSEOUT_MIN_TIMEOUT_SECONDS,
        )
        with _checkout_deployment_lock(
            project_root, timeout,
            purpose="runtime preparation, Electron packaging, NSIS, and shortcut refresh",
        ):
            return self._deploy_desktop_while_locked(project_root, timeout)

    def _deploy_desktop_while_locked(
        self, project_root: Path, timeout: float,
    ) -> dict[str, Any]:
        desktop = project_root / "desktop"
        package = desktop / "package.json"
        shortcut_script = project_root / "scripts" / "put_it_on_your_desktop.py"
        icon = desktop / "nexus-harness.ico"
        for required in (package, shortcut_script, icon):
            if not required.is_file():
                raise HarnessError(f"Desktop closeout requires {required}")
        npm = shutil.which("npm.cmd") or shutil.which("npm")
        if not npm:
            raise HarnessError("Desktop closeout requires npm on PATH")
        started = time.time()
        output = desktop / "build-output"
        application = output / "win-unpacked" / "Nexus Harness.exe"

        def build() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [npm, "run", "build"],
                cwd=str(desktop),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
            )

        try:
            built = build()
        except (OSError, subprocess.SubprocessError) as exc:
            raise HarnessError(f"Electron app and installer rebuild could not start: {exc}") from exc
        build_detail = f"{built.stdout}\n{built.stderr}".strip()
        if built.returncode != 0 and _is_owned_build_lock_failure(build_detail):
            # Electron Builder cannot replace its unpacked app while that exact
            # development executable is open. Close only processes whose
            # resolved executable path equals this checkout's owned artifact;
            # never kill by display name, which could hit an installed app or
            # another checkout. Then retry the gate once.
            powershell = shutil.which("powershell.exe") or shutil.which("powershell")
            if powershell and application.is_file():
                environment = dict(os.environ)
                environment["NEXUS_CLOSEOUT_OWNED_EXE"] = str(application.resolve())
                close_owned = subprocess.run(
                    [
                        powershell, "-NoProfile", "-NonInteractive", "-Command",
                        "$expected=[IO.Path]::GetFullPath($env:NEXUS_CLOSEOUT_OWNED_EXE); "
                        "Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and "
                        "([IO.Path]::GetFullPath($_.ExecutablePath) -ieq $expected) } | "
                        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop }",
                    ],
                    cwd=str(project_root), env=environment, capture_output=True,
                    text=True, timeout=30, check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if close_owned.returncode == 0:
                    time.sleep(0.5)
                    built = build()
        if built.returncode != 0:
            detail = f"{built.stdout}\n{built.stderr}".strip()[-4_000:]
            raise HarnessError(
                f"Electron app and installer rebuild failed with exit code {built.returncode}:\n{detail}"
            )
        installers = sorted(
            [
                *output.glob("Nexus-Harness-Setup-*.exe"),
                *output.glob("Nexus Harness Setup *.exe"),
            ],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not application.is_file() or not installers:
            raise HarnessError("Electron build did not produce both the unpacked app and NSIS installer")
        installer = installers[0]
        for artifact in (application, installer):
            if artifact.stat().st_mtime < started - 2:
                raise HarnessError(f"Desktop closeout artifact was not refreshed by this build: {artifact}")

        try:
            shortcut = subprocess.run(
                [sys.executable, str(shortcut_script)],
                cwd=str(project_root),
                capture_output=True,
                text=True,
                timeout=min(timeout, 300.0),
                check=False,
                creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise HarnessError(f"Desktop shortcut refresh could not start: {exc}") from exc
        if shortcut.returncode != 0:
            detail = f"{shortcut.stdout}\n{shortcut.stderr}".strip()[-4_000:]
            raise HarnessError(
                f"Desktop shortcut refresh failed with exit code {shortcut.returncode}:\n{detail}"
            )
        match = re.search(r"^\s*it lives\s+(.+?)\s*$", shortcut.stdout, re.MULTILINE)
        shortcut_path = Path(match.group(1)) if match else None
        if shortcut_path is None or not shortcut_path.is_file():
            raise HarnessError("Desktop shortcut refresh did not report a readable Nexus Harness shortcut")
        if shortcut_path.stat().st_mtime < started - 2:
            raise HarnessError("The Nexus Harness desktop shortcut was not refreshed during closeout")
        return {
            "state": "deployed",
            "application": str(application),
            "installer": str(installer),
            "desktop_shortcut": str(shortcut_path),
            "icon_source": str(application),
        }

    def _update_current_state(
        self,
        *,
        task: str,
        result: dict[str, Any],
        session: Path,
        recorded_utc: str,
    ) -> None:
        """Refresh only the hook-managed block and preserve hand-written context."""

        assert self.vault_root is not None
        relative = PROJECT_MEMORY_FOLDER / "Current State.md"
        existing = self._read_vault_note(relative)
        start, end = _managed_current_bounds(existing)
        summary = _neutralize_managed_sentinels(str(
            result.get("summary")
            or result.get("verdict")
            or result.get("error")
            or "See the bounded session outcome and current repository evidence."
        )).strip()[:2_000]
        next_step = _neutralize_managed_sentinels(str(
            result.get("next_step")
            or "Read the linked session note, then verify its claims against the current repository."
        )).strip()[:1_000]
        blockers = result.get("blockers")
        verification = result.get("verification")

        def compact(value: Any) -> str:
            if value in (None, "", [], {}):
                return "None recorded."
            if isinstance(value, str):
                return _neutralize_managed_sentinels(value)[:2_000]
            return _neutralize_managed_sentinels(
                json.dumps(value, ensure_ascii=False, sort_keys=True)
            )[:2_000]

        managed = (
            f"{CURRENT_STATE_START}\n"
            f"## Latest post-work state ({recorded_utc})\n\n"
            f"- State: `{_neutralize_managed_sentinels(str(result.get('state', 'unknown')))[:80]}`\n"
            f"- Task: {_neutralize_managed_sentinels(task) or '(not available)'}\n"
            f"- Session evidence: `[[{session.as_posix()}]]`\n\n"
            f"### Summary\n\n{summary}\n\n"
            f"### Verification\n\n{compact(verification)}\n\n"
            f"### Blockers\n\n{compact(blockers)}\n\n"
            f"### Next step\n\n{next_step}\n"
            f"{CURRENT_STATE_END}"
        )
        updated = existing[:start].rstrip() + "\n\n" + managed + existing[end:]
        _managed_current_bounds(updated)
        put_this_file_in_place(self.vault_root / relative, updated)

    def _session_history_for_run_id(self, run_id: str) -> list[dict[str, Any]]:
        """Return ordered, audit-safe revisions for one explicit workflow run."""

        assert self.vault_root is not None
        run_id = _neutralize_managed_sentinels(run_id)
        folder = self.vault_root / SESSIONS_FOLDER
        if not folder.exists():
            return []
        if _is_link_or_junction(folder) or not folder.is_dir():
            raise HarnessError("Persistent-memory Sessions folder must be a real directory")
        candidates = sorted(
            folder.glob("*.md"), key=lambda path: (path.stat().st_mtime_ns, path.name)
        )
        history: list[dict[str, Any]] = []
        for raw_path in candidates:
            if _is_link_or_junction(raw_path):
                raise HarnessError(f"Persistent-memory session note must not be a link: {raw_path.name}")
            path = raw_path.resolve(strict=True)
            if not _inside(path, self.vault_root) or not path.is_file():
                raise HarnessError("Persistent-memory session note escaped the bound vault")
            try:
                content = read_this_file_patiently(path)
            except (OSError, UnicodeError) as exc:
                raise HarnessError(f"Persistent-memory session note is unreadable: {path.name}") from exc
            match = re.search(r"^run_id:\s*(.+?)\s*$", content, re.MULTILINE)
            if match is None:
                continue
            try:
                recorded_run_id = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            if recorded_run_id == run_id:
                payload_match = re.search(
                    r"^payload_sha256:\s*(.+?)\s*$", content, re.MULTILINE
                )
                recorded_payload_sha256 = ""
                if payload_match is not None:
                    try:
                        parsed_payload = json.loads(payload_match.group(1))
                    except json.JSONDecodeError as exc:
                        raise HarnessError(
                            "Persistent-memory session payload identity is malformed"
                        ) from exc
                    if not isinstance(parsed_payload, str) or not re.fullmatch(
                        r"[0-9a-f]{64}", parsed_payload
                    ):
                        raise HarnessError(
                            "Persistent-memory session payload identity is malformed"
                        )
                    recorded_payload_sha256 = parsed_payload
                revision_match = re.search(r"^revision:\s*(\d+)\s*$", content, re.MULTILINE)
                revision = int(revision_match.group(1)) if revision_match else 0
                history.append(
                    {
                        "path": path.relative_to(self.vault_root),
                        "payload_sha256": recorded_payload_sha256,
                        "revision": revision,
                    }
                )
        next_revision = 1
        for record in history:
            declared = int(record["revision"])
            record["revision"] = max(declared, next_revision)
            next_revision = int(record["revision"]) + 1
        return history

    def _write_node(self, state: MemoryHookState) -> MemoryHookState:
        assert self.vault_root is not None
        assert self.memory_index is not None
        result = self.redact_value(dict(state.get("result") or {}))
        if not isinstance(result, dict):
            result = {"state": "unknown"}
        if state.get("deployment"):
            result["closeout_deployment"] = dict(state["deployment"])
        explicit_run_id = bool(result.get("run_id"))
        run_id = str(result.get("run_id") or uuid.uuid4().hex)
        recorded_run_id = _neutralize_managed_sentinels(run_id)
        safe_run_id = re.sub(r"[^A-Za-z0-9_.-]", "-", run_id)[:80]
        stamp = time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())
        summary = self._bounded_result(result)
        task = _neutralize_managed_sentinels(
            self.redact_text(str(state.get("task", "")))
        ).strip()[:4_000]
        payload_sha256 = hashlib.sha256(
            json.dumps(
                {"task": task, "bounded_outcome": summary},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        with _post_memory_lock(self.vault_root):
            self._verify_binding()
            history = self._session_history_for_run_id(run_id) if explicit_run_id else []
            exact_record = next(
                (
                    record for record in reversed(history)
                    if record["payload_sha256"] == payload_sha256
                ),
                None,
            )
            if explicit_run_id:
                if exact_record is not None:
                    already_written = Path(exact_record["path"])
                    current_relative = PROJECT_MEMORY_FOLDER / "Current State.md"
                    current_before = self._read_vault_note(current_relative)
                    _managed_current_bounds(current_before)
                    try:
                        # Repair only the narrow crash window for the globally
                        # newest note. Retrying an older revision must not move
                        # Current State backward after a later revision/session.
                        if self._latest_session() == already_written and (
                            f"[[{already_written.as_posix()}]]" not in current_before
                        ):
                            self._update_current_state(
                                task=task,
                                result=summary,
                                session=already_written,
                                recorded_utc=stamp,
                            )
                        status = self.memory_index.refresh()
                        self._validate_index_health(
                            status, additional_paths=(already_written.as_posix(),)
                        )
                        self.memory_index.set_kv(
                            "hook.last_post",
                            {
                                "written": already_written.as_posix(),
                                "index_files": status["files"],
                                "index_chunks": status["chunks"],
                                "idempotent_retry": True,
                                "recorded_utc": time.strftime(
                                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                                ),
                            },
                        )
                    except BaseException as exc:
                        rollback_errors: list[str] = []
                        try:
                            put_this_file_in_place(
                                self.vault_root / current_relative, current_before
                            )
                        except BaseException as rollback_exc:
                            rollback_errors.append(
                                f"Current State restore failed: {rollback_exc}"
                            )
                        try:
                            self.memory_index.refresh()
                        except BaseException as rollback_exc:
                            rollback_errors.append(
                                f"index rollback refresh failed: {rollback_exc}"
                            )
                        if rollback_errors:
                            raise HarnessError(
                                f"Persistent-memory idempotent retry failed ({exc}); "
                                "rollback was incomplete: " + "; ".join(rollback_errors)
                            ) from exc
                        raise
                    return {
                        "written": already_written.as_posix(),
                        "index_status": status,
                    }

            previous_revision = history[-1] if history else None
            revision = int(previous_revision["revision"]) + 1 if previous_revision else 1
            supersedes = (
                Path(previous_revision["path"]).as_posix() if previous_revision else None
            )
            prior_payload_sha256 = (
                str(previous_revision["payload_sha256"]) or None
                if previous_revision else None
            )
            note = (
                "---\nkind: harness-session\nproject: Nexus Harness\nprivate: true\n"
                "github_upload_allowed: false\n"
                f"recorded_utc: {stamp}\nrun_id: {json.dumps(recorded_run_id, ensure_ascii=False)}\n"
                f"payload_sha256: {json.dumps(payload_sha256)}\n"
                f"revision: {revision}\n"
                f"supersedes: {json.dumps(supersedes, ensure_ascii=False)}\n"
                f"prior_payload_sha256: {json.dumps(prior_payload_sha256)}\n"
                "state: "
                + json.dumps(
                    _neutralize_managed_sentinels(str(result.get("state", "unknown"))),
                    ensure_ascii=False,
                )
                + "\n---\n\n"
                f"# Session {stamp}\n\n## Task\n\n{task or '(not available)'}\n\n"
                "## Bounded outcome\n\n```json\n"
                + json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False)
                + "\n```\n"
            )
            note = enrich_session_note(note, task, summary)

            current_relative = PROJECT_MEMORY_FOLDER / "Current State.md"
            current_before = self._read_vault_note(current_relative)
            _managed_current_bounds(current_before)
            folder = self.vault_root / SESSIONS_FOLDER
            folder_was_absent = not folder.exists()
            if folder.exists() and (_is_link_or_junction(folder) or not folder.is_dir()):
                raise HarnessError("Persistent-memory Sessions folder must be a real directory")
            folder.mkdir(parents=True, exist_ok=True)
            if not _inside(folder.resolve(strict=True), self.vault_root):
                raise HarnessError("Persistent-memory Sessions folder escaped the bound vault")
            destination = folder / f"{stamp}-{safe_run_id}.md"
            suffix = 1
            while destination.exists():
                destination = folder / f"{stamp}-{safe_run_id}-{suffix}.md"
                suffix += 1
            relative_destination = destination.relative_to(self.vault_root)
            created_session = False
            try:
                put_this_file_in_place(destination, note)
                created_session = True
                self._update_current_state(
                    task=task,
                    result=summary,
                    session=relative_destination,
                    recorded_utc=stamp,
                )
                status = self.memory_index.refresh()
                self._validate_index_health(
                    status, additional_paths=(relative_destination.as_posix(),)
                )
                self.memory_index.set_kv(
                    "hook.last_post",
                    {
                        "written": relative_destination.as_posix(),
                        "index_files": status["files"],
                        "index_chunks": status["chunks"],
                        "idempotent_retry": False,
                        "recorded_utc": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                        ),
                    },
                )
            except BaseException as exc:
                rollback_errors: list[str] = []
                try:
                    put_this_file_in_place(self.vault_root / current_relative, current_before)
                except BaseException as rollback_exc:
                    rollback_errors.append(f"Current State restore failed: {rollback_exc}")
                if created_session:
                    try:
                        destination.unlink(missing_ok=True)
                    except BaseException as rollback_exc:
                        rollback_errors.append(f"session removal failed: {rollback_exc}")
                if folder_was_absent:
                    try:
                        folder.rmdir()
                    except OSError:
                        pass
                try:
                    self.memory_index.refresh()
                except BaseException as rollback_exc:
                    rollback_errors.append(f"index rollback refresh failed: {rollback_exc}")
                if rollback_errors:
                    raise HarnessError(
                        f"Persistent-memory post transaction failed ({exc}); rollback was incomplete: "
                        + "; ".join(rollback_errors)
                    ) from exc
                raise
            return {
                "written": relative_destination.as_posix(),
                "index_status": status,
            }

    def _refresh_after_node(self, state: MemoryHookState) -> MemoryHookState:
        # The canonical writes, validation refresh, and hook KV commit are one
        # locked transaction in _write_node. Keep this explicit graph node so
        # the lifecycle remains inspectable without opening a failure window.
        return {"index_status": dict(state.get("index_status") or {})}

    @staticmethod
    def _bounded_result(result: dict[str, Any]) -> dict[str, Any]:
        excluded = {
            "candidate", "proposal", "changes", "content", "context", "events",
            "provider_usage", "agent_tools", "source_code", "patch",
        }

        def retain(value: Any, depth: int = 0) -> Any:
            if depth > 4:
                return "<depth limit>"
            if isinstance(value, str):
                return _neutralize_managed_sentinels(value)[:2_000]
            if isinstance(value, (int, float, bool)) or value is None:
                return value
            if isinstance(value, list):
                return [retain(item, depth + 1) for item in value[:24]]
            if isinstance(value, dict):
                return {
                    str(key): retain(child, depth + 1)
                    for key, child in list(value.items())[:64]
                    if str(key) not in excluded
                }
            return str(value)[:500]

        retained = retain(result)
        return retained if isinstance(retained, dict) else {"result": retained}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Nexus Harness persistent-memory lifecycle hook")
    parser.add_argument("phase", choices=("init", "pre", "post", "status", "search"))
    parser.add_argument("--project", default=".")
    parser.add_argument("--vault", default="")
    parser.add_argument("--task", default="")
    parser.add_argument("--query", default="")
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--result", default="{}", help="JSON object used by the post hook")
    args = parser.parse_args(argv)
    project = Path(args.project).resolve()
    if args.phase == "init":
        if not args.vault:
            parser.error("--vault is required for init")
        print(initialize_vault(project, Path(args.vault)))
        return 0
    config = load_config(project, explicit=project / ".harness" / "config.local.json")
    hooks = PersistentMemoryHooks(config)
    if args.phase == "pre":
        context, consulted = hooks.before_session(args.task)
        print(json.dumps({"consulted": consulted, "context": context}, indent=2, ensure_ascii=False))
    elif args.phase == "post":
        try:
            result = json.loads(args.result)
        except json.JSONDecodeError as exc:
            raise HarnessError("--result must be a JSON object") from exc
        if not isinstance(result, dict):
            raise HarnessError("--result must be a JSON object")
        print(hooks.after_session(args.task, result))
    elif args.phase == "status":
        print(json.dumps(hooks.status(), indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(
            json.dumps(
                hooks.search(args.query, limit=args.limit),
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
