"""Private, restartable goal copies with conflict-checked source publication.

Provider work and verification use ``root``. The selected project stays the
authority boundary; only ``publish`` may apply the candidate to that project.
Callers hold ``publication`` across prepare, final verification, and publish.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import sys
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator

from .changes import FileTransaction, atomic_write
from .models import ChangePlan, HarnessError
from .filesystem_paths import filesystem_path
from .safety import ProjectTransactionLock, confined_path, portable_relative_path_key

_EXCLUDED = {".git", ".harness", ".nexus-verification"}
# Dependency installs are provided in a private goal copy (so its checks can
# run) but never compared, published or counted toward the limits below. A
# directory holding pyvenv.cfg is a virtual environment whatever its name.
DEPENDENCY_TREES = frozenset({"node_modules", ".venv", "venv"})
# Caches that tools regenerate on their own. They are absent from copies and
# never compared or published. Real deliverables such as dist/ and build/, and
# ambiguous names such as .cache (often a real fixture folder), are not here.
REGENERABLE_CACHES = frozenset({
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".nox", ".gradle",
    ".parcel-cache", ".turbo", ".pnpm-store", ".yarn-cache",
})
DEPENDENCY_CACHE_DIRECTORIES = DEPENDENCY_TREES | REGENERABLE_CACHES
# Tool litter recognised by name whether it is a file or a folder.
GENERATED_NAMES = DEPENDENCY_CACHE_DIRECTORIES | frozenset({
    ".hypothesis", ".coverage", ".ipynb_checkpoints", ".eslintcache",
})
GENERATED_FILE_SUFFIXES = (".pyc", ".pyo")
_MAX_SKIP_NOTES = 50
_MAX_FILES = 100_000
_MAX_BYTES = 2_000_000_000
FORK_SOURCE_CONTRACT = "nexus-owned-git-worktree-source/v1"
_publication: ContextVar[tuple[str, str, FileTransaction] | None] = ContextVar(
    "goal_workspace_publication", default=None,
)


class WorkspaceConflict(HarnessError):
    def __init__(self, message: str, conflicts: list[str]):
        super().__init__(message)
        self.conflicts = sorted(set(conflicts))


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _direct(path: Path) -> Path:
    """Check before resolving: resolve alone would conceal a substituted link."""
    absolute = path.expanduser().absolute()
    for part in [*reversed(absolute.parents), absolute]:
        try:
            metadata = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise HarnessError(f"Goal workspace linked path is not accepted: {part}")
    return absolute.resolve()


def _layout(document: dict[str, Any], runtime_root: Path) -> tuple[Path, Path, Path]:
    goal_id = document.get("goal_id")
    if not isinstance(goal_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", goal_id) is None:
        raise HarnessError("Goal workspace goal identity is invalid")
    source_path = document.get("project", {}).get("path")
    if not isinstance(source_path, str) or not source_path.strip() or not Path(source_path).expanduser().is_absolute():
        raise HarnessError("Goal workspace needs an explicit absolute selected-project path")
    source = _direct(Path(source_path))
    if not source.is_dir():
        raise HarnessError("Goal workspace selected project is unavailable")
    runtime = _direct(Path(runtime_root))
    owned_fork = (
        document.get("fork_workspace_contract") == FORK_SOURCE_CONTRACT
        and bool(document.get("parent_goal_id"))
        and source.parent == runtime / "goal-worktrees"
        and re.fullmatch(r"[a-f0-9]{32}", source.name) is not None
    )
    if source == runtime or source in runtime.parents or runtime in source.parents and not owned_fork:
        raise HarnessError("Goal workspace runtime must be outside the selected project")
    home = confined_path(runtime, "goal-workspaces", allow_control=True)
    folder = confined_path(home, goal_id, allow_control=True)
    return source, home, folder


def _source_identity(source: Path) -> str:
    metadata = source.stat()
    return _digest({"path": os.path.normcase(str(source)), "device": metadata.st_dev, "inode": metadata.st_ino})


def _key(home: Path, *, create: bool = False) -> bytes:
    path = confined_path(home, "authentication.key", allow_control=True)
    if create and not path.exists():
        home.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as stream:
                stream.write(secrets.token_bytes(32))
                stream.flush()
                os.fsync(stream.fileno())
            path.chmod(0o600)
        except FileExistsError:
            pass
    try:
        if not path.is_file() or path.stat().st_nlink != 1:
            raise HarnessError("Goal workspace authentication key is not an independent regular file")
        value = path.read_bytes()
    except OSError as exc:
        raise HarnessError("Goal workspace authentication key is unavailable") from exc
    if len(value) != 32:
        raise HarnessError("Goal workspace authentication key is damaged")
    return value


def _sign(key: bytes, value: dict[str, Any]) -> dict[str, Any]:
    return {**value, "hmac_sha256": hmac.new(key, _canonical(value), hashlib.sha256).hexdigest()}


def _verify(key: bytes, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise HarnessError("Goal workspace authenticated record is malformed")
    unsigned = {name: item for name, item in value.items() if name != "hmac_sha256"}
    supplied = value.get("hmac_sha256")
    if not isinstance(supplied, str) or not hmac.compare_digest(supplied, _sign(key, unsigned)["hmac_sha256"]):
        raise HarnessError("Goal workspace record failed authentication")
    return unsigned


def _read_state(home: Path, folder: Path) -> dict[str, Any]:
    path = confined_path(folder, "state.json", allow_control=True, allow_missing=False)
    try:
        if path.stat().st_size > 100_000_000:
            raise HarnessError("Goal workspace state exceeds its limit")
        return _verify(_key(home), json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        raise HarnessError("Goal workspace state is unavailable or damaged") from exc


def _write_state(home: Path, folder: Path, state: dict[str, Any]) -> None:
    atomic_write(confined_path(folder, "state.json", allow_control=True), _canonical(_sign(_key(home), state)))


def _generated(path: Path, *, directory: bool) -> bool:
    """Dependency/cache trees and bytecode are regenerated, never goal work."""
    name = path.name.casefold()
    if name in GENERATED_NAMES:
        return True
    if not directory:
        return name.endswith(GENERATED_FILE_SUFFIXES)
    try:
        return (path / "pyvenv.cfg").is_file()
    except OSError:
        return False


def _is_link(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


CACHE_NAME = ".cache"
CACHE_ROOTS_CONTRACT = "content-cache-folders/v1"


def _files_under(folder: Path, relative: str, limit: int = 1_000) -> list[str]:
    found: list[str] = []
    for child in folder.rglob("*"):
        if len(found) >= limit:
            break
        try:
            if child.is_file() and not child.is_symlink():
                found.append(relative + "/" + child.relative_to(folder).as_posix())
        except OSError:
            continue
    return found


def _cache_prefix(relative: str) -> str | None:
    parts = [part for part in str(relative).replace("\\", "/").split("/") if part]
    for index, part in enumerate(parts[:-1]):
        if part.casefold() == CACHE_NAME:
            return "/".join(parts[: index + 1]).casefold()
    return None


def _tracked_cache_roots(source: Path) -> set[str] | None:
    """``.cache`` folders holding files tracked in the project's git HEAD.

    Runs git with every GIT_* variable removed, so only this project's own
    repository answers. None when the project is not a git repository or git
    is unavailable; the caller then uses "existed when the copy was made".
    """
    if not (source / ".git").exists():
        return None
    import subprocess
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    environment.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    try:
        completed = subprocess.run(
            ["git", "-C", str(source), "ls-tree", "-r", "-z", "--name-only", "HEAD"],
            capture_output=True, env=environment, timeout=60, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    roots = set()
    for name in completed.stdout.decode("utf-8", errors="replace").split("\0"):
        prefix = _cache_prefix(name)
        if prefix:
            roots.add(prefix)
    return roots


def _existing_cache_roots(source: Path) -> set[str]:
    """``.cache`` folders present in the project when the copy is made."""
    roots: set[str] = set()
    root = filesystem_path(source)
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for path in entries:
            if excluded_entry(path):
                continue
            try:
                if not path.is_dir():
                    continue
            except OSError:
                continue
            relative = path.relative_to(root).as_posix()
            if path.name.casefold() == CACHE_NAME:
                roots.add(relative.casefold())
                continue
            pending.append(path)
    return roots


def _content_cache_roots(source: Path) -> list[str]:
    roots = _existing_cache_roots(source)
    tracked = _tracked_cache_roots(source)
    if tracked:
        roots |= tracked
    return sorted(roots)


def _cache_roots(state: dict[str, Any]) -> frozenset[str]:
    """The .cache folders that are project content for this goal copy."""
    held = state.get("content_cache_roots")
    if isinstance(held, dict) and held.get("contract") == CACHE_ROOTS_CONTRACT:
        return frozenset(str(one).casefold() for one in held.get("roots") or [])
    # A copy made before this rule: every .cache folder it already contained.
    return frozenset(prefix for prefix in (_cache_prefix(path) for path in state.get("baseline", {})) if prefix)


def _dependency_tree(path: Path) -> bool:
    if path.name.casefold() in DEPENDENCY_TREES:
        return True
    try:
        return (path / "pyvenv.cfg").is_file()
    except OSError:
        return False


# Machine bounds for copying installed dependencies into a private copy. When
# one is reached the copy stops there and the goal note says so.
DEPENDENCY_COPY_MAX_FILES = 100_000
DEPENDENCY_COPY_MAX_BYTES = 2_000_000_000
DEPENDENCY_COPY_MAX_SECONDS = 120.0


def _real(path: Path) -> Path | None:
    try:
        return Path(os.path.realpath(path))
    except (OSError, ValueError):
        return None


def _inside(path: Path | None, root: Path | tuple[Path, ...]) -> bool:
    if path is None:
        return False
    for one in (root if isinstance(root, tuple) else (root,)):
        try:
            path.relative_to(one)
            return True
        except ValueError:
            continue
    return False


class _CopyBudget:
    def __init__(self):
        import time
        self.clock = time.monotonic
        self.deadline = self.clock() + DEPENDENCY_COPY_MAX_SECONDS
        self.files = 0
        self.bytes = 0
        self.exhausted = ""

    def take(self, size: int) -> bool:
        if self.exhausted:
            return False
        self.files += 1
        self.bytes += max(0, size)
        if self.files > DEPENDENCY_COPY_MAX_FILES:
            self.exhausted = f"more than {DEPENDENCY_COPY_MAX_FILES:,} files"
        elif self.bytes > DEPENDENCY_COPY_MAX_BYTES:
            self.exhausted = f"more than {DEPENDENCY_COPY_MAX_BYTES // 1_000_000_000} GB"
        elif self.clock() > self.deadline:
            self.exhausted = f"more than {int(DEPENDENCY_COPY_MAX_SECONDS)} seconds"
        return not self.exhausted


def _copy_dependency_tree(origin: Path, target: Path, source_root: Path, budget: _CopyBudget,
                          notes: list[str], relative: str, ancestors: tuple[Path, ...] = (),
                          *, dry: bool = False) -> None:
    """Copy one dependency directory, following only links that stay in the project.

    A link or junction whose target is outside the selected project (npm link,
    a global store, or a planted link) is skipped and named, so nothing from
    outside the project is copied in. An in-project link's target is copied as
    real files, never linked back. A link cycle is cut at its first repeat.
    """
    real = _real(origin)
    if real is None or real in ancestors:
        notes.append(relative + " (link cycle)")
        return
    ancestors = (*ancestors, real)
    try:
        entries = sorted(origin.iterdir())
    except OSError:
        notes.append(relative)
        return
    if not dry:
        target.mkdir(parents=True, exist_ok=True)
    for path in entries:
        if budget.exhausted:
            return
        child = relative + "/" + path.name
        try:
            metadata = path.lstat()
        except OSError:
            continue
        linked = _is_link(metadata)
        resolved = _real(path) if linked else None
        if linked and not _inside(resolved, source_root):
            notes.append(child + " (links outside the project; not copied)")
            continue
        try:
            is_dir = path.is_dir()
        except OSError:
            continue
        destination = target / path.name
        if is_dir:
            _copy_dependency_tree(path, destination, source_root, budget, notes, child, ancestors, dry=dry)
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if not budget.take(size) or dry:
            continue
        try:
            shutil.copy2(path, destination, follow_symlinks=True)
        except OSError:
            notes.append(child)


def _provide_dependency_trees(source: Path, project: Path) -> list[str]:
    """Copy installed dependencies into a private copy (goal or agent copy).

    Tests in the copy need them (node_modules, virtual environments). They are
    real copies, never links into the user's project, so nothing an agent does
    in the copy can write through to it; and they stay outside every manifest,
    so they are never compared or published back. A tree already present in
    the copy is left alone. Links leaving the project are not followed; copying
    is bounded (files, bytes, time). Anything not provided is returned as a note
    rather than failing the goal.
    """
    unavailable: list[str] = []
    root = filesystem_path(source)
    source_root = _real(root) or root
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for path in entries:
            name = path.name.casefold()
            if name in _EXCLUDED:
                continue
            try:
                metadata = path.lstat()
            except OSError:
                continue
            linked = _is_link(metadata)
            relative = path.relative_to(root).as_posix()
            if _dependency_tree(path):
                # The project's own install may itself be a junction (a shared
                # store); it is copied. Inside it, links may point only into
                # the project or into that install.
                allowed = (source_root,) + ((_real(path),) if linked and _real(path) else ())
                try:
                    target = filesystem_path(confined_path(project, relative))
                except HarnessError:
                    unavailable.append(relative)
                    continue
                if target.exists():
                    continue  # Already provided; the copy's own installs are kept.
                # Measure first (stat only): a tree over the bounds is not
                # copied at all, rather than left half-copied.
                measure = _CopyBudget()
                _copy_dependency_tree(path, target, allowed, measure, [], relative, dry=True)
                if measure.exhausted:
                    unavailable.append(relative + " (not copied: " + measure.exhausted + ")")
                    continue
                budget = _CopyBudget()
                try:
                    _copy_dependency_tree(path, target, allowed, budget, unavailable, relative)
                except (OSError, RecursionError):
                    unavailable.append(relative)
                if budget.exhausted:
                    _remove_tree(target)
                    unavailable.append(relative + " (not copied: stopped after " + budget.exhausted + ")")
                continue
            if linked or not stat.S_ISDIR(metadata.st_mode) or name in GENERATED_NAMES:
                continue
            pending.append(path)
    return unavailable[:_MAX_SKIP_NOTES]


def excluded_entry(path: Path) -> bool:
    """The one rule every project inventory uses to skip an entry.

    Control folders, dependency/cache trees (including any virtual
    environment), tool litter, bytecode, links, junctions and special files
    are never part of the work that is copied, compared or published. Goal
    copies, agent copies and closeout snapshots all share this rule, so their
    inventories of the same files always agree.
    """
    name = path.name.casefold()
    if name in _EXCLUDED:
        return True
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return True
    if _is_link(metadata):
        return True
    if stat.S_ISDIR(metadata.st_mode):
        return _generated(path, directory=True)
    if not stat.S_ISREG(metadata.st_mode):
        return True
    return _generated(path, directory=False)


def excluded_relative(relative: str) -> bool:
    """Path-only form of ``excluded_entry`` for records saved before a rule existed."""
    parts = [part.casefold() for part in str(relative).replace("\\", "/").split("/") if part]
    if not parts:
        return False
    return any(part in _EXCLUDED or part in GENERATED_NAMES for part in parts) \
        or parts[-1].endswith(GENERATED_FILE_SUFFIXES)


def _manifest(project: Path, *, independent: bool = False,
              skipped: list[str] | None = None, cache_roots: frozenset[str] | None = None,
              skipped_cache: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """Hash the project's own files.

    Symbolic links, junctions and special files are skipped (and named in
    ``skipped``) rather than followed or refused: following one could read
    outside the project, refusing one would stop the goal. Hard-linked files
    (as uv and pnpm create) are ordinary files here; publication writes an
    independent copy. ``independent`` is retained for callers' intent.
    """
    del independent
    result: dict[str, dict[str, Any]] = {}
    keys: set[str] = set()
    total = 0
    pending = [project]
    while pending:
        directory = pending.pop()
        for path in sorted(filesystem_path(directory).iterdir()):
            if path.name.casefold() in _EXCLUDED:
                continue
            relative = path.relative_to(filesystem_path(project)).as_posix()
            key = portable_relative_path_key(relative)
            if key in keys:
                raise HarnessError("Goal workspace contains portable path aliases: " + relative)
            keys.add(key)
            try:
                linked = _is_link(path.lstat())
            except FileNotFoundError:
                continue  # Removed by the project's own tools while listing.
            if linked:
                # A linked dependency install is provided separately.
                if skipped is not None and not _dependency_tree(path):
                    skipped.append(relative)
                continue
            safe = filesystem_path(confined_path(project, relative, allow_missing=False))
            metadata = safe.stat()
            if stat.S_ISDIR(metadata.st_mode):
                if _generated(safe, directory=True):
                    continue
                if cache_roots is not None and path.name.casefold() == CACHE_NAME \
                        and relative.casefold() not in cache_roots:
                    # A .cache folder that did not exist in the project (and is
                    # not tracked in its git HEAD) is tool output, not work.
                    if skipped_cache is not None:
                        skipped_cache.extend(_files_under(safe, relative))
                    continue
                pending.append(safe)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                if skipped is not None:
                    skipped.append(relative)
                continue
            if _generated(safe, directory=False):
                continue
            total += metadata.st_size
            if total > _MAX_BYTES or len(result) >= _MAX_FILES:
                raise HarnessError("Goal workspace snapshot exceeds the file or byte limit")
            digest = hashlib.sha256()
            with safe.open("rb") as stream:
                before = os.fstat(stream.fileno())
                while block := stream.read(1024 * 1024):
                    digest.update(block)
                after = os.fstat(stream.fileno())
            signature = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_mode, item.st_nlink)
            if signature(metadata) != signature(before) or signature(before) != signature(after):
                raise HarnessError("Goal workspace file changed while being copied: " + relative)
            result[relative] = {"sha256": digest.hexdigest(), "mode": stat.S_IMODE(after.st_mode),
                                "size": after.st_size, "device": after.st_dev, "inode": after.st_ino,
                                "modified_ns": after.st_mtime_ns, "links": after.st_nlink}
    return result


def _own_files(manifest: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Drop dependency/cache entries a receipt recorded before they were excluded."""
    return {path: value for path, value in manifest.items() if not excluded_relative(path)}


def _identity(value: dict[str, Any] | None) -> Any:
    """A file's identity without its link count, which other names can change."""
    return None if value is None else {key: item for key, item in value.items() if key != "links"}


def _same(first: dict[str, dict[str, Any]], second: dict[str, dict[str, Any]]) -> bool:
    return first.keys() == second.keys() and all(_identity(first[name]) == _identity(second[name]) for name in first)


def _content(value: dict[str, Any] | None) -> Any:
    return None if value is None else (value["sha256"], value["mode"])


def _contents(manifest: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {path: _content(value) for path, value in manifest.items()}


def _copy_file(source: Path, destination: Path, relative: str, expected: dict[str, Any]) -> None:
    origin = confined_path(source, relative, allow_missing=False)
    target = confined_path(destination, relative)
    data = filesystem_path(origin).read_bytes()
    if hashlib.sha256(data).hexdigest() != expected["sha256"]:
        raise HarnessError("Goal workspace source changed during copy: " + relative)
    atomic_write(target, data, mode=expected["mode"])


def _loaded(document: dict[str, Any], runtime_root: Path) -> tuple[Path, Path, Path, dict[str, Any]]:
    source, home, folder = _layout(document, runtime_root)
    descriptor = document.get("execution_workspace")
    state = _read_state(home, folder)
    if not isinstance(descriptor, dict) or descriptor != state.get("descriptor"):
        raise HarnessError("Goal workspace descriptor does not match its authenticated state")
    if (descriptor.get("schema_version") != 1 or descriptor.get("goal_id") != document["goal_id"]
            or descriptor.get("source_identity") != _source_identity(source)
            or descriptor.get("project_authority_id") != str(document.get("project_authority_id") or "")):
        raise HarnessError("Goal workspace belongs to a different goal or selected project")
    if state.get("phase") != "ready":
        raise HarnessError("Goal workspace initialization is incomplete; retry its creation before running providers")
    project = confined_path(folder, "project", allow_control=True, allow_missing=False)
    if not project.is_dir():
        raise HarnessError("Goal workspace snapshot is missing")
    if descriptor.get("workspace_identity") != _source_identity(project):
        raise HarnessError("Goal workspace directory identity changed")
    return source, home, project, state


def _remove_tree(path: Path) -> None:
    """Remove an unused partial copy, including mode-preserved read-only files on Windows."""
    def make_writable_and_retry(function: Any, target: str, _error: Any) -> None:
        os.chmod(target, stat.S_IREAD | stat.S_IWRITE)
        function(target)

    target = filesystem_path(path)
    if target.exists():
        handler = {"onexc": make_writable_and_retry} if sys.version_info >= (3, 12) else {"onerror": make_writable_and_retry}
        shutil.rmtree(target, **handler)


def _same_directory(path: Path, identity: Any) -> bool:
    try:
        return path.is_dir() and _source_identity(path) == identity
    except OSError:
        return False


def create(document: dict[str, Any], runtime_root: Path, *, publication_locked: bool = False) -> dict[str, Any]:
    """Copy current user files, including dirty/non-Git files, before providers run."""
    source, home, folder = _layout(document, runtime_root)
    home.mkdir(parents=True, exist_ok=True)
    if publication_locked:
        _transaction(source, home)
    with nullcontext() if publication_locked else ProjectTransactionLock(home).held():
        _key(home, create=True)
        if folder.exists():
            state = _read_state(home, folder)
            if state.get("phase") == "initializing":
                descriptor = state["descriptor"]
                if (descriptor.get("goal_id") != document["goal_id"]
                        or descriptor.get("source_identity") != _source_identity(source)
                        or descriptor.get("project_authority_id") != str(document.get("project_authority_id") or "")):
                    raise HarnessError("Interrupted goal workspace belongs to a different selected project")
                project = confined_path(folder, "project", allow_control=True)
                if (_same(_manifest(source), state["baseline"])
                        and _same_directory(project, descriptor.get("workspace_identity"))):
                    for relative, expected in state["baseline"].items():
                        _copy_file(source, project, relative, expected)
                    _provide_dependency_trees(source, project)
                    if _contents(_manifest(project)) == _contents(state["baseline"]) and _same(_manifest(source), state["baseline"]):
                        state["phase"] = "ready"
                        _write_state(home, folder, state)
                        candidate = {**document, "execution_workspace": state.get("descriptor")}
                        validate(candidate, runtime_root)
                        return dict(state["descriptor"])
                # No agent has used this copy yet: the user edited the project
                # while it was being made, or the interrupted copy is not exact.
                # Throw the partial copy away and start again from the project
                # as it is now, instead of refusing on every retry forever.
                _remove_tree(project)
            else:
                candidate = {**document, "execution_workspace": state.get("descriptor")}
                validate(candidate, runtime_root)
                return dict(state["descriptor"])
        skipped: list[str] = []
        baseline = _manifest(source, skipped=skipped)
        folder.mkdir(exist_ok=True)
        project = confined_path(folder, "project", allow_control=True)
        project.mkdir()
        descriptor = {"schema_version": 1, "goal_id": document["goal_id"],
                      "source_identity": _source_identity(source),
                      "workspace_identity": _source_identity(project),
                      "project_authority_id": str(document.get("project_authority_id") or ""),
                      "baseline_sha256": _digest(baseline),
                      "path": f"goal-workspaces/{document['goal_id']}/project"}
        if skipped:
            # A note, not a failure: links are not followed out of the project.
            descriptor["skipped_links"] = {
                "count": len(skipped), "paths": sorted(skipped)[:_MAX_SKIP_NOTES],
                "note": "Symbolic links, junctions and special files were not copied into the private working copy.",
            }
        state = {"schema_version": 1, "descriptor": descriptor, "phase": "initializing",
                 "baseline": baseline, "publication": None,
                 "content_cache_roots": {"contract": CACHE_ROOTS_CONTRACT, "roots": _content_cache_roots(source)}}
        _write_state(home, folder, state)
        for relative, expected in baseline.items():
            _copy_file(source, project, relative, expected)
        unavailable = _provide_dependency_trees(source, project)
        if unavailable:
            state["dependency_trees_unavailable"] = sorted(unavailable)[:_MAX_SKIP_NOTES]
            # Shown to the user (goal note) through the descriptor.
            descriptor["dependency_trees_unavailable"] = state["dependency_trees_unavailable"]
        if not _same(_manifest(source), baseline) or _contents(_manifest(project)) != _contents(baseline):
            raise HarnessError("Selected project changed while its goal copy was being created")
        state["phase"] = "ready"
        _write_state(home, folder, state)
        return descriptor


def validate(document: dict[str, Any], runtime_root: Path, *, full: bool = False) -> None:
    """Authenticate ownership and reject path substitutions; edits remain mutable."""
    if "execution_workspace" not in document:
        return
    _source, _home, project, _state = _loaded(document, runtime_root)
    roots = _cache_roots(_state)
    if full:
        _manifest(project, independent=True, cache_roots=roots)


def published_file_manifest(document: dict[str, Any], runtime_root: Path) -> dict[str, str]:
    """Recover verified file identities for a completed pre-delivery-receipt goal."""
    _source, home, _project, state = _loaded(document, runtime_root)
    publication = state.get("publication") or {}
    if publication.get("state") != "published":
        raise HarnessError("This goal has no authenticated publication receipt")
    verified = _verify(_key(home), publication.get("receipt"))
    if verified.get("descriptor") != state["descriptor"]:
        raise HarnessError("Publication receipt belongs to another goal")
    return {path: "file:" + value["sha256"] for path, value in verified["workspace"].items()}


def root(document: dict[str, Any], runtime_root: Path) -> Path:
    if "execution_workspace" not in document:
        return _direct(Path(document["project"]["path"]))
    return _loaded(document, runtime_root)[2]


def differing_files(document: dict[str, Any], runtime_root: Path, other_root: Path | None = None) -> list[str]:
    """Compare authenticated private bytes and modes without changing either tree.

    Callers serialize the source and private file snapshots when a later action
    depends on this comparison. This helper never accesses the goal database.
    """
    source, _home, project, _state = _loaded(document, runtime_root)
    roots = _cache_roots(_state)
    selected = _direct(other_root) if other_root is not None else source
    private_files = _contents(_manifest(project, independent=True, cache_roots=roots))
    selected_files = _contents(_manifest(selected, cache_roots=roots))
    return sorted(path for path in private_files.keys() | selected_files.keys()
                  if private_files.get(path) != selected_files.get(path))


@contextmanager
def publication(document: dict[str, Any], runtime_root: Path, *, timeout_seconds: float | None = None) -> Iterator[None]:
    """Serialize publish/check sections across processes, including nested roots."""
    source, home, _folder = _layout(document, runtime_root)
    from .project_operations import publication_claim
    with publication_claim(source, runtime_root, timeout_seconds), ProjectTransactionLock(home).held(timeout_seconds):
        transaction = FileTransaction(source, max_files=_MAX_FILES, max_bytes=_MAX_BYTES)
        with transaction.locked(timeout_seconds):
            token = _publication.set((str(home), str(source), transaction))
            try:
                yield
            finally:
                _publication.reset(token)


def _transaction(source: Path, home: Path) -> FileTransaction:
    held = _publication.get()
    if held is None or held[:2] != (str(home), str(source)):
        raise HarnessError("Goal workspace publication context is required")
    return held[2]


def _assert_published_snapshot(source: Path, project: Path, receipt: dict[str, Any],
                               roots: frozenset[str] | None = None) -> None:
    """An unacknowledged publication still needs its complete verified context."""
    expected_source = _own_files(receipt["source"])
    expected_workspace = _own_files(receipt["workspace"])
    for relative in receipt["changed"]:
        if relative in expected_workspace:
            expected_source[relative] = expected_workspace[relative]
        else:
            expected_source.pop(relative, None)
    current_source = _manifest(source, cache_roots=roots)
    conflicts = {name for name in expected_source.keys() | current_source.keys()
                 if _content(expected_source.get(name)) != _content(current_source.get(name))}
    current_workspace = _manifest(project, independent=True, cache_roots=roots)
    conflicts.update(name for name in expected_workspace.keys() | current_workspace.keys()
                     if _identity(expected_workspace.get(name)) != _identity(current_workspace.get(name)))
    if conflicts:
        raise WorkspaceConflict(
            "The published project or its verified working copy changed before completion was acknowledged; "
            "the previous verification cannot be reused.", list(conflicts),
        )


def _recover_publication(source: Path, home: Path, folder: Path, state: dict[str, Any]) -> dict[str, Any] | None:
    roots = _cache_roots(state)
    held = state.get("publication")
    if not held:
        return None
    transaction = _transaction(source, home)
    txid = held.get("transaction_id")
    expected = held["receipt"]
    if held.get("state") == "published":
        _assert_published_snapshot(source, folder / "project", expected, _cache_roots(state))
        if held.get("manifest", {}).get("transaction_id"):
            try:
                transaction.verify_applied(held["manifest"])
            except HarnessError as exc:
                raise WorkspaceConflict("Published goal files changed before completion was acknowledged", held["receipt"]["changed"]) from exc
        return held
    if not txid:
        return None
    manifest_path = confined_path(source, f".harness/backups/{txid}/manifest.json", allow_control=True)
    if not manifest_path.exists():
        return None  # Journal persisted before FileTransaction created its manifest.
    manifest = transaction.load_manifest(txid)
    records = manifest.get("changes", [])
    expected_paths = expected["changed"]
    if (not isinstance(records, list) or len(records) != len(expected_paths)
            or {one.get("path") for one in records if isinstance(one, dict)} != set(expected_paths)):
        raise HarnessError("Goal publication transaction no longer matches its authenticated intent")
    for record in records:
        relative = record["path"]
        before, after = expected["source"].get(relative), expected["workspace"].get(relative)
        if (record.get("before_sha256") != (before["sha256"] if before else None)
                or record.get("after_sha256") != (after["sha256"] if after else None)
                or record.get("before_mode") != (before["mode"] if before else None)
                or record.get("after_mode") != (after["mode"] if after else before["mode"] if before else None)
                or record.get("delete") != (after is None)):
            raise HarnessError("Goal publication transaction no longer matches its authenticated intent")
    if manifest.get("state") == "prepared":
        status = next((one["status"] for one in transaction.reconcile() if one["transaction_id"] == txid), "in_doubt")
        if status == "not_applied":
            return None
        if status == "applied_after_crash":
            transaction.recover(txid, "finalize")
            manifest = transaction.load_manifest(txid)
    if manifest.get("state") != "applied":
        raise WorkspaceConflict("Goal publication has an interrupted transaction requiring reconciliation: " + txid, expected_paths)
    _assert_published_snapshot(source, folder / "project", expected, _cache_roots(state))
    transaction.verify_applied(manifest)
    held.update(state="published", manifest=manifest)
    _write_state(home, folder, state)
    return held


def prepare_publish(document: dict[str, Any], runtime_root: Path) -> dict[str, Any]:
    """Rebase untouched files; return the exact candidate for final verification."""
    source, home, project, state = _loaded(document, runtime_root)
    roots = _cache_roots(state)
    _transaction(source, home)
    recovered = _recover_publication(source, home, project.parent, state)
    if recovered:
        return recovered["receipt"]
    prior = state.get("publication")
    if prior:
        if _same(_manifest(source, cache_roots=roots), prior["receipt"]["source"]) and _same(_manifest(project, cache_roots=roots), prior["receipt"]["workspace"]):
            return prior["receipt"]
        transaction = _transaction(source, home)
        manifest_path = confined_path(source, f".harness/backups/{prior['transaction_id']}/manifest.json", allow_control=True)
        if manifest_path.exists():
            transaction.recover(prior["transaction_id"], "rollback")
    baseline = state["baseline"]
    held_cache: list[str] = []
    current = _manifest(source, cache_roots=roots)
    candidate = _manifest(project, independent=True, cache_roots=roots, skipped_cache=held_cache)
    actual_candidate = candidate
    pending_rebase = state.get("pending_rebase")
    if pending_rebase:
        # A process can stop between individual private-file replacements.
        # Recover only those exact before/after states, never reinterpret the
        # already synchronized files as a new goal-authored delta.
        before, target = pending_rebase["before"], pending_rebase["target"]
        conflicted = [name for name in candidate.keys() | before.keys() | target.keys()
                      if _content(candidate.get(name)) not in (_content(before.get(name)), _content(target.get(name)))]
        if conflicted:
            raise WorkspaceConflict("Private files changed during an interrupted rebase", conflicted)
        candidate = before
    changed = {name for name in baseline.keys() | candidate.keys()
               if _content(baseline.get(name)) != _content(candidate.get(name))}
    reconciled = {name for name in changed if _content(current.get(name)) == _content(candidate.get(name))}
    changed -= reconciled
    conflicts = [name for name in changed if _content(current.get(name)) != _content(baseline.get(name))]
    if conflicts:
        raise WorkspaceConflict("Goal publication conflicts with selected-project changes: " + ", ".join(sorted(conflicts)), conflicts)
    # FileTransaction rewrites a target in place, so publishing onto a
    # hard-linked project file would also change its other names, which can
    # live outside the project (a package store). That write stays refused;
    # hard links elsewhere, and in the private copy, are ordinary files.
    for relative in sorted(changed):
        if current.get(relative, {}).get("links", 1) != 1:
            raise WorkspaceConflict(
                "Goal publication will not write through a hard-linked project file, because that would also "
                "change its other linked copies (possibly outside the project): " + relative
                + ". The goal's version stays in its working copy; replace the link with an ordinary file to publish it.",
                [relative])
    target = {**current, **{name: candidate[name] for name in changed if name in candidate}}
    for name in changed - candidate.keys():
        target.pop(name, None)
    state["pending_rebase"] = {"before": candidate, "target": target}
    _write_state(home, project.parent, state)
    for relative in sorted((baseline.keys() | current.keys() | actual_candidate.keys()) - changed):
        if _content(actual_candidate.get(relative)) == _content(current.get(relative)):
            continue
        if relative in current:
            _copy_file(source, project, relative, current[relative])
        else:
            filesystem_path(confined_path(project, relative)).unlink(missing_ok=True)
    if not _same(_manifest(source, cache_roots=roots), current):
        raise WorkspaceConflict("Selected project changed during goal rebase; retry publication", list(current))
    # Rebase has no source effects. Persist the new comparison base so a retry
    # cannot mistake another goal's synchronized files for this goal's delta.
    state["baseline"] = {**current, **{name: baseline[name] for name in changed if name in baseline}}
    for name in changed - baseline.keys():
        state["baseline"].pop(name, None)
    state["publication"] = None
    state.pop("pending_rebase", None)
    _write_state(home, project.parent, state)
    return _sign(_key(home), {"schema_version": 1, "descriptor": state["descriptor"],
                             "source": current, "workspace": _manifest(project, cache_roots=roots), "changed": sorted(changed),
                             "rebased": _contents(candidate) != _contents(_manifest(project, cache_roots=roots)),
                             "reconciled": sorted(reconciled),
                             # New .cache folders are tool output: kept in the copy, never published.
                             "held_back_cache_files": sorted(held_cache)[:200],
                             "held_back_cache_count": len(held_cache)})


def publish(document: dict[str, Any], runtime_root: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    """Apply only this goal's delta, retaining exact durable transaction identity."""
    source, home, project, state = _loaded(document, runtime_root)
    roots = _cache_roots(state)
    transaction = _transaction(source, home)
    verified = _verify(_key(home), receipt)
    if verified.get("descriptor") != state["descriptor"]:
        raise HarnessError("Goal publication receipt belongs to a different goal")
    from . import goal_access
    if verified.get("changed") and goal_access.state(document)["mode"] == "read_only":
        raise HarnessError("Read only access does not allow applying retained changes to the selected project")
    recovered = _recover_publication(source, home, project.parent, state)
    if recovered:
        if recovered["receipt"] != receipt:
            raise HarnessError("Goal publication was already completed with a different receipt")
        return {**dict(recovered["manifest"]), **_held_back_cache(verified)}
    if not _same(_manifest(source, cache_roots=roots), verified["source"]) or not _same(_manifest(project, cache_roots=roots), verified["workspace"]):
        raise WorkspaceConflict("Goal publication snapshot changed after verification", list(verified["source"].keys() | verified["workspace"].keys()))
    plans = []
    for relative in verified["changed"]:
        before, after = verified["source"].get(relative), verified["workspace"].get(relative)
        content = filesystem_path(confined_path(project, relative)).read_bytes() if after else None
        if after and hashlib.sha256(content).hexdigest() != after["sha256"]:
            raise WorkspaceConflict("Goal workspace changed while its publication bytes were read", [relative])
        plans.append(ChangePlan(relative, before["sha256"] if before else None,
                                content,
                                delete=after is None, reason=f"Verified goal {document['goal_id']}",
                                mode=after["mode"] if after else None))
    prior = state.get("publication") or {}
    txid = prior.get("transaction_id") or FileTransaction.new_transaction_id()
    state["publication"] = {"state": "prepared", "transaction_id": txid, "receipt": receipt}
    _write_state(home, project.parent, state)
    manifest = transaction.apply(plans, transaction_id=txid) if plans else {
        "schema_version": 3, "transaction_id": None, "state": "applied", "changes": [],
    }
    _assert_published_snapshot(source, project, receipt, roots)
    state["publication"].update(state="published", manifest=manifest)
    _write_state(home, project.parent, state)
    return {**manifest, **_held_back_cache(verified)}


def _held_back_cache(receipt: dict[str, Any]) -> dict[str, Any]:
    count = int(receipt.get("held_back_cache_count") or 0)
    return {"held_back_cache_files": list(receipt.get("held_back_cache_files") or []),
            "held_back_cache_count": count} if count else {}
