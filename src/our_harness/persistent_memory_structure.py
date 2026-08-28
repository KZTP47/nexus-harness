"""Human-readable information architecture for the Nexus Obsidian vault.

The Markdown vault remains canonical.  This module adds deterministic topic
maps and small managed metadata/link blocks without rewriting historical
session outcomes.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
import re
from pathlib import Path
import time
from typing import Any, Callable

from .models import HarnessError
from .persistent_memory_index import INDEX_FOLDER, _is_link_or_junction
from .safety import put_this_file_in_place, read_this_file_patiently


INFORMATION_ARCHITECTURE_VERSION = 1
PROJECT_MEMORY_FOLDER = Path("01 Project Memory")
START_HERE_NOTE = PROJECT_MEMORY_FOLDER / "Start Here.md"
TOPICS_FOLDER = Path("02 Topics")
TOPIC_INDEX_NOTE = TOPICS_FOLDER / "Topic Index.md"
SESSIONS_FOLDER = Path("Sessions")
NAVIGATION_START = "<!-- nexus-managed-navigation:start -->"
NAVIGATION_END = "<!-- nexus-managed-navigation:end -->"
SESSION_METADATA_START = "# nexus-managed-session-metadata:start"
SESSION_METADATA_END = "# nexus-managed-session-metadata:end"
SESSION_TOPICS_START = "<!-- nexus-managed-session-topics:start -->"
SESSION_TOPICS_END = "<!-- nexus-managed-session-topics:end -->"
OBSIDIAN_LAYOUT_MARKER = Path(INDEX_FOLDER) / "obsidian-layout-version.json"
STRUCTURE_MIGRATION_LOCK = Path(INDEX_FOLDER) / "structure-migration.lock"
STRUCTURE_MIGRATION_LOCK_TIMEOUT_SECONDS = 60.0
STRUCTURE_MARKERS = (
    NAVIGATION_START,
    NAVIGATION_END,
    SESSION_METADATA_START,
    SESSION_METADATA_END,
    SESSION_TOPICS_START,
    SESSION_TOPICS_END,
)


def neutralize_structure_markers(value: str) -> str:
    """Encode structure control tokens carried by untrusted new evidence."""

    for marker in STRUCTURE_MARKERS:
        value = value.replace(marker, marker.replace("nexus-managed", "nexus managed marker removed"))
    return value


@dataclass(frozen=True)
class TopicDefinition:
    title: str
    component: str
    summary: str
    keywords: tuple[str, ...]
    related: tuple[str, ...]

    @property
    def path(self) -> Path:
        return TOPICS_FOLDER / f"{self.title}.md"

    @property
    def link(self) -> str:
        return f"[[{self.path.with_suffix('').as_posix()}|{self.title}]]"

    @property
    def tag(self) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", self.title.casefold()).strip("-")
        return f"nexus/area/{slug}"


TOPICS = (
    TopicDefinition(
        "Persistent Memory and Safety",
        "persistent-memory",
        "Vault binding, retrieval, lifecycle hooks, permissions, privacy, and fail-closed policy.",
        (
            "obsidian", "vault", "persistent memory", "memory hook", "pre-work", "post-work",
            "binding", "permission", "policy", "safety", "security", "redact", "privacy",
            "boundary", "isolation", "kv", "fts", "sqlite index",
        ),
        ("Testing and Quality", "Desktop Packaging and Runtime"),
    ),
    TopicDefinition(
        "AI Agent Swarm",
        "swarm",
        "Agent boards, cooperation, delegation, run coordination, mailboxes, and durable swarm work.",
        (
            "swarm", "agent board", "board of agents", "delegat", "mailbox", "cooperation",
            "multi-agent", "subagent", "agent run", "swarm work", "swarm project", "checkpoint",
            "moving around the board", "they talk", "did they succeed",
        ),
        ("Product UI and Chat", "Testing and Quality"),
    ),
    TopicDefinition(
        "Visual Test Automation",
        "pipelines",
        "Visual pipelines, browser actions, screenshots, automation projects, and comparison evidence.",
        (
            "pipeline", "visual test", "visual automation", "screenshot", "browser action",
            "automation project", "golden image", "image comparison", "recording", "playwright",
        ),
        ("Testing and Quality", "Product UI and Chat"),
    ),
    TopicDefinition(
        "Providers and Model Integrations",
        "providers",
        "Codex, Claude, Gemini, browser-backed providers, model selection, authentication, and delivery.",
        (
            "provider", "codex", "claude", "gemini", "antigravity", "model", "openai",
            "anthropic", "google cloud", "oauth", "login", "authentication", "send ambiguity",
            "browser-backed", "browser provider", "usage reset",
        ),
        ("Product UI and Chat", "Persistent Memory and Safety"),
    ),
    TopicDefinition(
        "Desktop Packaging and Runtime",
        "desktop",
        "Electron shell, bundled runtime, installer, desktop shortcut, icons, and Windows publication.",
        (
            "desktop", "electron", "installer", "nsis", "shortcut", "icon", "packag",
            "win-unpacked", "windows runtime", "runtime publication", "zipapp", "preload",
            "main.js", "nexus harness.exe", "build-output",
        ),
        ("Testing and Quality", "Repository and Release"),
    ),
    TopicDefinition(
        "Product UI and Chat",
        "ui-chat",
        "Chat behavior, saved conversations, controls, boards, navigation, accessibility, and user-facing UX.",
        (
            " ui ", "user interface", " ux ", "chat", "conversation", "button", "panel",
            "sidebar", "layout", "navigation", "dashboard", "saved chat", "archive", "restore",
            "status list", "html", "css", "app.js",
        ),
        ("AI Agent Swarm", "Providers and Model Integrations"),
    ),
    TopicDefinition(
        "Testing and Quality",
        "testing",
        "Unit, integration, smoke, packaging, regression, CI, review, and verification evidence.",
        (
            "test", "unittest", "pytest", "regression", "smoke", "verification", "verify",
            "jenkins", " ci ", "coverage", "assert", "quality", "code review", "failure",
        ),
        ("Repository and Release", "Desktop Packaging and Runtime"),
    ),
    TopicDefinition(
        "Repository and Release",
        "repository-release",
        "Git history, branches, GitHub, documentation publication, releases, and repository hygiene.",
        (
            " git ", "github", "branch", "commit", "merge", "readme", "release", "repository",
            "repo ", "pull request", "default branch", "publish", "staging", "tracked",
        ),
        ("Testing and Quality", "Desktop Packaging and Runtime"),
    ),
    TopicDefinition(
        "General Engineering",
        "general-engineering",
        "Cross-cutting engineering work that does not belong to one narrower subsystem.",
        (),
        ("Testing and Quality", "Product UI and Chat"),
    ),
)
TOPIC_BY_TITLE = {topic.title: topic for topic in TOPICS}


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _validated_destination(vault_root: Path, relative: Path) -> Path:
    """Validate every existing component before returning a confined path."""

    vault = vault_root.resolve(strict=True)
    if relative.is_absolute() or ".." in relative.parts:
        raise HarnessError("Persistent-memory structure path must be vault-relative")
    raw = vault / relative
    current = vault
    for part in relative.parts:
        candidate = current / part
        if candidate.exists() or candidate.is_symlink():
            if _is_link_or_junction(candidate):
                raise HarnessError(
                    f"Persistent-memory structure path must not cross a link/reparse point: {relative.as_posix()}"
                )
            current = candidate.resolve(strict=True)
            if not _inside(current, vault):
                raise HarnessError(
                    f"Persistent-memory structure path escaped the vault: {relative.as_posix()}"
                )
        else:
            break
    resolved = raw.resolve(strict=False)
    if not _inside(resolved, vault):
        raise HarnessError(
            f"Persistent-memory structure path escaped the vault: {relative.as_posix()}"
        )
    return raw


def _safe_existing_file(vault_root: Path, relative: Path) -> Path:
    raw = _validated_destination(vault_root, relative)
    resolved = raw.resolve(strict=True)
    if not resolved.is_file() or not _inside(resolved, vault_root):
        raise HarnessError(f"Persistent-memory structure path escaped the vault: {relative.as_posix()}")
    return resolved


@contextmanager
def _structure_migration_lock(
    vault_root: Path,
    timeout_seconds: float = STRUCTURE_MIGRATION_LOCK_TIMEOUT_SECONDS,
):
    """Serialize one preflight/apply/rollback structure transaction."""

    if timeout_seconds <= 0:
        raise HarnessError("Persistent-memory structure lock timeout must be greater than zero")
    vault = vault_root.resolve(strict=True)
    index_root = _validated_destination(vault, Path(INDEX_FOLDER))
    index_root.mkdir(parents=True, exist_ok=True)
    _validated_destination(vault, Path(INDEX_FOLDER))
    lock_path = _validated_destination(vault, STRUCTURE_MIGRATION_LOCK)
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
                        "Timed out waiting for the bound vault's structure migration lock"
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


def _write_missing(vault_root: Path, relative: Path, content: str) -> bool:
    destination = _validated_destination(vault_root, relative)
    if destination.exists():
        _safe_existing_file(vault_root, relative)
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    if _is_link_or_junction(destination.parent):
        raise HarnessError(f"Persistent-memory structure folder must not be a link: {relative.parent}")
    if not _inside(destination.parent.resolve(strict=True), vault_root):
        raise HarnessError("Persistent-memory structure folder escaped the vault")
    put_this_file_in_place(destination, content.strip() + "\n")
    return True


def _frontmatter_end(content: str) -> int:
    if not content.startswith("---\n"):
        raise HarnessError("Persistent-memory session note has no YAML frontmatter")
    finish = content.find("\n---\n", 4)
    if finish < 0:
        raise HarnessError("Persistent-memory session frontmatter is unterminated")
    return finish


def _replace_frontmatter_metadata(content: str, block: str) -> str:
    """Recognize metadata control tokens only inside YAML frontmatter."""

    finish = _frontmatter_end(content)
    frontmatter = content[:finish]
    starts = [match.start() for match in re.finditer(re.escape(SESSION_METADATA_START), frontmatter)]
    ends = [match.start() for match in re.finditer(re.escape(SESSION_METADATA_END), frontmatter)]
    if starts or ends:
        if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
            raise HarnessError("Persistent-memory session frontmatter metadata is ambiguous")
        replaced = (
            frontmatter[: starts[0]]
            + block
            + frontmatter[ends[0] + len(SESSION_METADATA_END):]
        )
        return replaced + content[finish:]
    return frontmatter.rstrip() + "\n" + block + content[finish:]


def _replace_strict_managed_tail(
    content: str,
    *,
    start: str,
    end: str,
    required_heading: str,
    block: str,
) -> str:
    """Replace only a Nexus-shaped final block; body marker prose is inert."""

    pattern = re.compile(
        r"(?ms)(?:\n\n)?^"
        + re.escape(start)
        + r"\n"
        + re.escape(required_heading)
        + r"\n.*?^"
        + re.escape(end)
        + r"\s*\Z"
    )
    match = pattern.search(content)
    if match is not None:
        return content[: match.start()] + "\n\n" + block + "\n"
    separator = "" if content.endswith("\n\n") else ("\n" if content.endswith("\n") else "\n\n")
    return content + separator + block + "\n"


def _has_frontmatter_metadata(content: str) -> bool:
    try:
        finish = _frontmatter_end(content)
    except HarnessError:
        return False
    frontmatter = content[:finish]
    starts = frontmatter.count(SESSION_METADATA_START)
    ends = frontmatter.count(SESSION_METADATA_END)
    return starts == 1 and ends == 1 and frontmatter.index(SESSION_METADATA_START) < frontmatter.index(SESSION_METADATA_END)


def _has_strict_topic_tail(content: str) -> bool:
    pattern = re.compile(
        r"(?ms)(?:\n\n)?^"
        + re.escape(SESSION_TOPICS_START)
        + "\n"
        + re.escape("## Related topics")
        + r"\n.*?^"
        + re.escape(SESSION_TOPICS_END)
        + r"\s*\Z"
    )
    return pattern.search(content) is not None


def infer_session_metadata(task: str, result: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return bounded, deterministic Obsidian metadata for a session."""

    result = result if isinstance(result, dict) else {}
    relevant_result = {
        key: value
        for key, value in result.items()
        if key not in {"closeout_deployment", "deployment", "run_id", "payload_sha256"}
    }
    task_haystack = f" {task} ".casefold().replace("_", " ").replace("-", " ")
    evidence_haystack = (
        " "
        + json.dumps(relevant_result, ensure_ascii=False, sort_keys=True)[:16_000]
        + " "
    ).casefold().replace("_", " ").replace("-", " ")
    scored: list[tuple[int, int, TopicDefinition]] = []
    for order, topic in enumerate(TOPICS[:-1]):
        score = 0
        for keyword in topic.keywords:
            normalized = keyword.casefold().replace("_", " ").replace("-", " ")
            weight = max(1, len(normalized.split()))
            if normalized in task_haystack:
                score += weight * 4
            if normalized in evidence_haystack:
                score += weight
        if score:
            scored.append((score, -order, topic))
    scored.sort(reverse=True, key=lambda item: (item[0], item[1]))
    selected = [item[2] for item in scored[:3]] or [TOPICS[-1]]
    status = str(result.get("state") or result.get("status") or "unknown").strip()[:80]
    return {
        "area": selected[0].title,
        "status": status or "unknown",
        "components": [topic.component for topic in selected],
        "related_topics": [topic.path.with_suffix("").as_posix() for topic in selected],
        "tags": ["nexus/session", *(topic.tag for topic in selected)],
    }


def render_session_metadata(metadata: dict[str, Any]) -> str:
    def quoted(value: Any) -> str:
        return json.dumps(str(value), ensure_ascii=False)

    def sequence(values: list[Any]) -> str:
        return json.dumps([str(value) for value in values], ensure_ascii=False)

    related = [f"[[{path}]]" for path in metadata["related_topics"]]
    return (
        f"{SESSION_METADATA_START}\n"
        f"area: {quoted(metadata['area'])}\n"
        f"status: {quoted(metadata['status'])}\n"
        f"components: {sequence(metadata['components'])}\n"
        f"related_topics: {sequence(related)}\n"
        f"tags: {sequence(metadata['tags'])}\n"
        f"{SESSION_METADATA_END}"
    )


def render_session_topic_links(metadata: dict[str, Any]) -> str:
    links = "\n".join(
        f"- [[{path}|{Path(path).name}]]" for path in metadata["related_topics"]
    )
    return (
        f"{SESSION_TOPICS_START}\n"
        "## Related topics\n\n"
        f"{links}\n"
        f"{SESSION_TOPICS_END}"
    )


def enrich_session_note(content: str, task: str, result: dict[str, Any] | None = None) -> str:
    metadata = infer_session_metadata(task, result)
    enriched = _replace_frontmatter_metadata(content, render_session_metadata(metadata))
    return _replace_strict_managed_tail(
        enriched,
        start=SESSION_TOPICS_START,
        end=SESSION_TOPICS_END,
        required_heading="## Related topics",
        block=render_session_topic_links(metadata),
    )


def _extract_task(content: str) -> str:
    match = re.search(r"(?ms)^## Task\s*\n+(.*?)(?=\n## |\Z)", content)
    return match.group(1).strip()[:4_000] if match else "Historical Nexus Harness session"


def _extract_state(content: str) -> str:
    match = re.search(r"(?m)^state:\s*(.+?)\s*$", content)
    if match is None:
        return "unknown"
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        value = match.group(1).strip(" '\"")
    return str(value)[:80]


def _extract_bounded_outcome(content: str) -> dict[str, Any]:
    match = re.search(
        r"(?ms)^## Bounded outcome\s*\n+```json\s*\n(.*?)\n```",
        content,
    )
    if match is None:
        return {}
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def migrate_sessions(vault_root: Path) -> dict[str, int]:
    """Validate historical sessions without rewriting their canonical bytes."""

    folder = vault_root / SESSIONS_FOLDER
    if not folder.exists():
        return {"sessions": 0, "updated": 0}
    if _is_link_or_junction(folder) or not folder.is_dir():
        raise HarnessError("Persistent-memory Sessions folder must be a real directory")
    sessions = 0
    for raw in sorted(folder.glob("*.md")):
        relative = raw.relative_to(vault_root)
        path = _safe_existing_file(vault_root, relative)
        read_this_file_patiently(path)
        sessions += 1
    return {"sessions": sessions, "updated": 0}


def _navigation_block() -> str:
    return (
        f"{NAVIGATION_START}\n"
        "## Vault navigation\n\n"
        "- [[01 Project Memory/Start Here|Start Here]]\n"
        "- [[01 Project Memory/Current State|Current State]]\n"
        "- [[01 Project Memory/Codex Working Memory|Codex Working Memory]]\n"
        "- [[02 Topics/Topic Index|Topic Index]]\n"
        f"{NAVIGATION_END}"
    )


def _topic_note(topic: TopicDefinition) -> str:
    related = "\n".join(
        f"- {TOPIC_BY_TITLE[title].link}" for title in topic.related
    )
    return f"""---
kind: topic-map
project: Nexus Harness
private: true
area: {json.dumps(topic.title)}
component: {json.dumps(topic.component)}
tags: ["nexus/topic", {json.dumps(topic.tag)}]
---

# {topic.title}

{topic.summary}

## Navigate

- [[01 Project Memory/Start Here|Start Here]]
- [[02 Topics/Topic Index|All topics]]

## Related subsystems

{related}

## Session evidence

Sessions link back to this topic automatically. Open **Backlinks** for the complete evidence trail, or use this embedded search:

```query
path:"Sessions" "[[{topic.path.with_suffix('').as_posix()}]]"
```
"""


def scaffold_notes() -> dict[Path, str]:
    topic_links = "\n".join(f"- {topic.link} — {topic.summary}" for topic in TOPICS)
    start_here = f"""---
kind: vault-navigation
project: Nexus Harness
private: true
tags: ["nexus/navigation", "nexus/project-memory"]
---

# Start Here

This is the front door to the Nexus Harness second brain. It is a navigation map, not an operator dashboard.

## Current truth

- [[01 Project Memory/Current State|Current State]] — newest verified outcome, blockers, and next step.
- [[01 Project Memory/Codex Working Memory|Codex Working Memory]] — durable facts and invariants.
- [[01 Project Memory/AI Engineering Guide|AI Engineering Guide]] — ownership and verification rules.
- [[01 Project Memory/How To Use This Vault|How To Use This Vault]] — memory workflow and evidence policy.
- [[Project Memory|Project Memory Boundary]] — project ownership and privacy rules.

## Browse by subsystem

[[02 Topics/Topic Index|Open the complete Topic Index]]

{topic_links}

## Historical evidence

`Sessions/` is the append-only audit trail. It is hidden from the global graph by default to keep the map readable. Open a topic note and use **Backlinks** to browse related sessions.
"""
    index = """---
kind: topic-index
project: Nexus Harness
private: true
tags: ["nexus/navigation", "nexus/topic-index"]
---

# Topic Index

Subsystem maps connect durable guidance to related session evidence. New sessions are classified and linked automatically by the post-work hook.

""" + topic_links + "\n\n[[01 Project Memory/Start Here|Return to Start Here]]\n"
    notes = {START_HERE_NOTE: start_here, TOPIC_INDEX_NOTE: index}
    notes.update({topic.path: _topic_note(topic) for topic in TOPICS})
    return notes


def _update_navigation(vault_root: Path, relative: Path) -> bool:
    path = _safe_existing_file(vault_root, relative)
    content = read_this_file_patiently(path)
    updated = _replace_strict_managed_tail(
        content,
        start=NAVIGATION_START,
        end=NAVIGATION_END,
        required_heading="## Vault navigation",
        block=_navigation_block(),
    )
    if updated == content:
        return False
    put_this_file_in_place(path, updated)
    return True


def _read_json_object(vault_root: Path, relative: Path) -> dict[str, Any]:
    path = _validated_destination(vault_root, relative)
    if not path.exists():
        return {}
    path = _safe_existing_file(vault_root, relative)
    try:
        value = json.loads(read_this_file_patiently(path))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarnessError(f"Persistent-memory Obsidian config is unreadable: {path.name}") from exc
    if not isinstance(value, dict):
        raise HarnessError(f"Persistent-memory Obsidian config must be an object: {path.name}")
    return value


def _default_workspace() -> dict[str, Any]:
    return {
        "main": {
            "id": "nexus-memory-main",
            "type": "split",
            "children": [
                {
                    "id": "nexus-start-tabs",
                    "type": "tabs",
                    "dimension": 58,
                    "children": [{
                        "id": "nexus-start-here",
                        "type": "leaf",
                        "state": {
                            "type": "markdown",
                            "state": {"file": START_HERE_NOTE.as_posix(), "mode": "source", "source": False},
                            "icon": "lucide-compass",
                            "title": "Start Here",
                        },
                    }],
                },
                {
                    "id": "nexus-topic-graph-tabs",
                    "type": "tabs",
                    "dimension": 42,
                    "children": [{
                        "id": "nexus-topic-graph",
                        "type": "leaf",
                        "state": {"type": "graph", "state": {}, "icon": "lucide-git-fork", "title": "Topic Graph"},
                    }],
                },
            ],
            "direction": "vertical",
        },
        "left": {
            "id": "nexus-left-sidebar",
            "type": "split",
            "children": [{
                "id": "nexus-left-tabs",
                "type": "tabs",
                "children": [{
                    "id": "nexus-files",
                    "type": "leaf",
                    "state": {
                        "type": "file-explorer",
                        "state": {"sortOrder": "alphabetical", "autoReveal": True, "showSearch": True, "searchQuery": ""},
                        "icon": "lucide-folder-open",
                        "title": "Files",
                    },
                }],
            }],
            "direction": "horizontal",
            "width": 340,
        },
        "right": {
            "id": "nexus-right-sidebar",
            "type": "split",
            "children": [{
                "id": "nexus-right-tabs",
                "type": "tabs",
                "children": [
                    {"id": "nexus-backlinks", "type": "leaf", "state": {"type": "backlink", "state": {"file": START_HERE_NOTE.as_posix(), "collapseAll": False, "extraContext": True, "sortOrder": "alphabetical", "showSearch": False, "searchQuery": "", "backlinkCollapsed": False, "unlinkedCollapsed": True}, "icon": "links-coming-in", "title": "Backlinks"}},
                    {"id": "nexus-outline", "type": "leaf", "state": {"type": "outline", "state": {"file": START_HERE_NOTE.as_posix(), "followCursor": True, "showSearch": False, "searchQuery": ""}, "icon": "lucide-list", "title": "Outline"}},
                ],
            }],
            "direction": "horizontal",
            "width": 320,
            "collapsed": False,
        },
        "active": "nexus-start-here",
        "lastOpenFiles": [
            START_HERE_NOTE.as_posix(),
            TOPIC_INDEX_NOTE.as_posix(),
            (PROJECT_MEMORY_FOLDER / "Current State.md").as_posix(),
            (PROJECT_MEMORY_FOLDER / "Codex Working Memory.md").as_posix(),
            *[topic.path.as_posix() for topic in TOPICS],
        ],
    }


def _default_graph() -> dict[str, Any]:
    return {
        "search": '-path:"Sessions" -file:"Welcome"',
        "showTags": False,
        "showAttachments": False,
        "hideUnresolved": True,
        "showOrphans": False,
        "collapse-color-groups": False,
        "colorGroups": [
            {"query": 'path:"01 Project Memory" OR file:"Project Memory"', "color": {"a": 1, "rgb": 5213439}},
            {"query": 'path:"02 Topics"', "color": {"a": 1, "rgb": 5025616}},
            {"query": 'path:"Sessions"', "color": {"a": 1, "rgb": 16098851}},
        ],
        "showArrow": True,
        "textFadeMultiplier": 0,
        "nodeSizeMultiplier": 1.15,
        "lineSizeMultiplier": 1.2,
    }


def _restore_bytes(path: Path, content: bytes) -> None:
    beside = path.with_name(f"{path.name}.rollback-{os.getpid()}-{time.time_ns()}")
    beside.write_bytes(content)
    os.replace(beside, path)


def _apply_structure_plan(vault: Path, plan: list[tuple[Path, str]]) -> None:
    """Apply a fully preflighted plan and restore exact prior bytes on error."""

    backups: dict[Path, bytes | None] = {}
    created_directories: list[Path] = []
    applied: list[Path] = []
    for relative, _content in plan:
        destination = _validated_destination(vault, relative)
        backups[relative] = destination.read_bytes() if destination.exists() else None
    try:
        for relative, content in plan:
            destination = _validated_destination(vault, relative)
            missing_parents: list[Path] = []
            parent = destination.parent
            while parent != vault and not parent.exists():
                missing_parents.append(parent)
                parent = parent.parent
            _validated_destination(vault, parent.relative_to(vault))
            destination.parent.mkdir(parents=True, exist_ok=True)
            for created in reversed(missing_parents):
                if created.exists():
                    created_directories.append(created)
            _validated_destination(vault, relative)
            applied.append(relative)
            put_this_file_in_place(destination, content)
    except BaseException:
        rollback_errors: list[str] = []
        for relative in reversed(applied):
            try:
                destination = _validated_destination(vault, relative)
                prior = backups[relative]
                if prior is None:
                    destination.unlink(missing_ok=True)
                else:
                    _restore_bytes(destination, prior)
            except BaseException as exc:
                rollback_errors.append(f"{relative.as_posix()}: {exc}")
        for directory in sorted(set(created_directories), key=lambda path: len(path.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
        if rollback_errors:
            raise HarnessError(
                "Persistent-memory structure migration rollback was incomplete: "
                + "; ".join(rollback_errors)
            )
        raise


def ensure_information_architecture(
    vault_root: Path,
    *,
    navigation_notes: tuple[Path, ...],
    additional_scaffold: dict[Path, str] | None = None,
) -> dict[str, Any]:
    """Create and idempotently maintain the visible vault structure."""

    vault = vault_root.resolve(strict=True)
    with _structure_migration_lock(vault):
        plan: dict[Path, str] = {}
        created: list[str] = []

        # Complete preflight: every source and destination is validated and all
        # resulting bytes are computed before the first canonical write.
        all_scaffold = dict(additional_scaffold or {})
        all_scaffold.update(scaffold_notes())
        for relative, content in all_scaffold.items():
            destination = _validated_destination(vault, relative)
            if destination.exists():
                _safe_existing_file(vault, relative)
            else:
                plan[relative] = content.strip() + "\n"
                created.append(relative.as_posix())

        navigation_updated = 0
        for relative in navigation_notes:
            destination = _validated_destination(vault, relative)
            if destination.exists():
                path = _safe_existing_file(vault, relative)
                content = read_this_file_patiently(path)
            elif relative in plan:
                content = plan[relative]
            else:
                raise HarnessError(
                    f"Persistent-memory navigation note is missing: {relative.as_posix()}"
                )
            updated = _replace_strict_managed_tail(
                content,
                start=NAVIGATION_START,
                end=NAVIGATION_END,
                required_heading="## Vault navigation",
                block=_navigation_block(),
            )
            if updated != content:
                plan[relative] = updated
                navigation_updated += 1

        migration = migrate_sessions(vault)

        obsidian_relative = Path(".obsidian")
        obsidian = _validated_destination(vault, obsidian_relative)
        if obsidian.exists() and not obsidian.is_dir():
            raise HarnessError("Persistent-memory .obsidian path must be a real directory")
        graph_relative = obsidian_relative / "graph.json"
        workspace_relative = obsidian_relative / "workspace.json"
        graph_path = _validated_destination(vault, graph_relative)
        workspace_path = _validated_destination(vault, workspace_relative)
        if graph_path.exists():
            # Existing Obsidian preferences are entirely user-owned. Validate
            # but preserve their bytes and every key, including search/tags/
            # color groups.
            _read_json_object(vault, graph_relative)
        else:
            plan[graph_relative] = json.dumps(
                _default_graph(), indent=2, ensure_ascii=False
            ) + "\n"
        if workspace_path.exists():
            _read_json_object(vault, workspace_relative)
        else:
            plan[workspace_relative] = json.dumps(
                _default_workspace(), indent=2, ensure_ascii=False
            ) + "\n"

        marker_path = _validated_destination(vault, OBSIDIAN_LAYOUT_MARKER)
        marker_version = 0
        if marker_path.exists():
            marker_value = _read_json_object(vault, OBSIDIAN_LAYOUT_MARKER)
            try:
                marker_version = int(marker_value.get("version", 0))
            except (ValueError, TypeError):
                marker_version = 0
        if marker_version < INFORMATION_ARCHITECTURE_VERSION:
            plan[OBSIDIAN_LAYOUT_MARKER] = json.dumps(
                {"version": INFORMATION_ARCHITECTURE_VERSION}, indent=2
            ) + "\n"

        _apply_structure_plan(vault, list(plan.items()))
        layout_paths = {graph_relative, workspace_relative, OBSIDIAN_LAYOUT_MARKER}
        return {
            "version": INFORMATION_ARCHITECTURE_VERSION,
            "created": created,
            "navigation_updated": navigation_updated,
            "sessions": migration["sessions"],
            "sessions_updated": 0,
            "layout_applied": any(relative in layout_paths for relative in plan),
            "topics": len(TOPICS),
        }


def organization_status(vault_root: Path) -> dict[str, Any]:
    vault = vault_root.resolve(strict=True)
    sessions_folder = _validated_destination(vault, SESSIONS_FOLDER)
    if sessions_folder.exists() and not sessions_folder.is_dir():
        raise HarnessError("Persistent-memory Sessions folder must be a real directory")
    sessions = list(sessions_folder.glob("*.md")) if sessions_folder.is_dir() else []
    structured = 0
    linked = 0
    for raw in sessions:
        relative = raw.relative_to(vault)
        content = read_this_file_patiently(_safe_existing_file(vault, relative))
        structured += int(_has_frontmatter_metadata(content))
        linked += int(_has_strict_topic_tail(content))
    graph = _read_json_object(vault, Path(".obsidian") / "graph.json")
    workspace = _read_json_object(vault, Path(".obsidian") / "workspace.json")
    start_here = _validated_destination(vault, START_HERE_NOTE)
    topic_paths = [_validated_destination(vault, topic.path) for topic in TOPICS]
    return {
        "version": INFORMATION_ARCHITECTURE_VERSION,
        "start_here": start_here.is_file(),
        "topic_notes": sum(path.is_file() for path in topic_paths),
        "sessions": len(sessions),
        "structured_sessions": structured,
        "topic_linked_sessions": linked,
        "graph_filter": graph.get("search", ""),
        "graph_color_groups": len(graph.get("colorGroups", [])),
        "workspace_active": workspace.get("active"),
        "workspace_auto_reveal": bool(
            workspace.get("left", {})
            .get("children", [{}])[0]
            .get("children", [{}])[0]
            .get("state", {})
            .get("state", {})
            .get("autoReveal", False)
        ),
    }
