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
import stat
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator

from .changes import FileTransaction, atomic_write
from .models import ChangePlan, HarnessError
from .safety import ProjectTransactionLock, confined_path, portable_relative_path_key

_EXCLUDED = {".git", ".harness", ".nexus-verification"}
_MAX_FILES = 100_000
_MAX_BYTES = 2_000_000_000
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
    if source == runtime or source in runtime.parents or runtime in source.parents:
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


def _manifest(project: Path, *, independent: bool = False) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    keys: set[str] = set()
    total = 0
    pending = [project]
    while pending:
        directory = pending.pop()
        for path in sorted(directory.iterdir()):
            if path.name.casefold() in _EXCLUDED:
                continue
            relative = path.relative_to(project).as_posix()
            key = portable_relative_path_key(relative)
            if key in keys:
                raise HarnessError("Goal workspace contains portable path aliases: " + relative)
            keys.add(key)
            safe = confined_path(project, relative, allow_missing=False)
            metadata = safe.stat()
            if stat.S_ISDIR(metadata.st_mode):
                pending.append(safe)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise HarnessError("Goal workspace contains a non-regular file: " + relative)
            if independent and metadata.st_nlink != 1:
                raise HarnessError("Goal workspace contains a hard-linked file: " + relative)
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


def _content(value: dict[str, Any] | None) -> Any:
    return None if value is None else (value["sha256"], value["mode"])


def _contents(manifest: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {path: _content(value) for path, value in manifest.items()}


def _copy_file(source: Path, destination: Path, relative: str, expected: dict[str, Any]) -> None:
    origin = confined_path(source, relative, allow_missing=False)
    target = confined_path(destination, relative)
    data = origin.read_bytes()
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
                if _manifest(source) != state["baseline"]:
                    raise WorkspaceConflict("Project changed during interrupted workspace creation", list(state["baseline"]))
                project = confined_path(folder, "project", allow_control=True)
                project.mkdir(exist_ok=True)
                for relative, expected in state["baseline"].items():
                    _copy_file(source, project, relative, expected)
                if _contents(_manifest(project)) != _contents(state["baseline"]) or _manifest(source) != state["baseline"]:
                    raise HarnessError("Interrupted goal workspace copy is not its exact baseline")
                state["phase"] = "ready"
                _write_state(home, folder, state)
            candidate = {**document, "execution_workspace": state.get("descriptor")}
            validate(candidate, runtime_root)
            return dict(state["descriptor"])
        baseline = _manifest(source)
        folder.mkdir()
        project = confined_path(folder, "project", allow_control=True)
        project.mkdir()
        descriptor = {"schema_version": 1, "goal_id": document["goal_id"],
                      "source_identity": _source_identity(source),
                      "workspace_identity": _source_identity(project),
                      "project_authority_id": str(document.get("project_authority_id") or ""),
                      "baseline_sha256": _digest(baseline),
                      "path": f"goal-workspaces/{document['goal_id']}/project"}
        state = {"schema_version": 1, "descriptor": descriptor, "phase": "initializing",
                 "baseline": baseline, "publication": None}
        _write_state(home, folder, state)
        for relative, expected in baseline.items():
            _copy_file(source, project, relative, expected)
        if _manifest(source) != baseline or _contents(_manifest(project)) != _contents(baseline):
            raise HarnessError("Selected project changed while its goal copy was being created")
        state["phase"] = "ready"
        _write_state(home, folder, state)
        return descriptor


def validate(document: dict[str, Any], runtime_root: Path, *, full: bool = False) -> None:
    """Authenticate ownership and reject path substitutions; edits remain mutable."""
    if "execution_workspace" not in document:
        return
    _source, _home, project, _state = _loaded(document, runtime_root)
    if full:
        _manifest(project, independent=True)


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
    selected = _direct(other_root) if other_root is not None else source
    private_files = _contents(_manifest(project, independent=True))
    selected_files = _contents(_manifest(selected))
    return sorted(path for path in private_files.keys() | selected_files.keys()
                  if private_files.get(path) != selected_files.get(path))


@contextmanager
def publication(document: dict[str, Any], runtime_root: Path, *, timeout_seconds: float | None = None) -> Iterator[None]:
    """Serialize publish/check sections across processes, including nested roots."""
    source, home, _folder = _layout(document, runtime_root)
    with ProjectTransactionLock(home).held(timeout_seconds):
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


def _assert_published_snapshot(source: Path, project: Path, receipt: dict[str, Any]) -> None:
    """An unacknowledged publication still needs its complete verified context."""
    expected_source = dict(receipt["source"])
    for relative in receipt["changed"]:
        if relative in receipt["workspace"]:
            expected_source[relative] = receipt["workspace"][relative]
        else:
            expected_source.pop(relative, None)
    current_source = _manifest(source)
    conflicts = {name for name in expected_source.keys() | current_source.keys()
                 if _content(expected_source.get(name)) != _content(current_source.get(name))}
    current_workspace = _manifest(project, independent=True)
    conflicts.update(name for name in receipt["workspace"].keys() | current_workspace.keys()
                     if receipt["workspace"].get(name) != current_workspace.get(name))
    if conflicts:
        raise WorkspaceConflict(
            "The published project or its verified working copy changed before completion was acknowledged; "
            "the previous verification cannot be reused.", list(conflicts),
        )


def _recover_publication(source: Path, home: Path, folder: Path, state: dict[str, Any]) -> dict[str, Any] | None:
    held = state.get("publication")
    if not held:
        return None
    transaction = _transaction(source, home)
    txid = held.get("transaction_id")
    expected = held["receipt"]
    if held.get("state") == "published":
        _assert_published_snapshot(source, folder / "project", expected)
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
    _assert_published_snapshot(source, folder / "project", expected)
    transaction.verify_applied(manifest)
    held.update(state="published", manifest=manifest)
    _write_state(home, folder, state)
    return held


def prepare_publish(document: dict[str, Any], runtime_root: Path) -> dict[str, Any]:
    """Rebase untouched files; return the exact candidate for final verification."""
    source, home, project, state = _loaded(document, runtime_root)
    _transaction(source, home)
    recovered = _recover_publication(source, home, project.parent, state)
    if recovered:
        return recovered["receipt"]
    prior = state.get("publication")
    if prior:
        if _manifest(source) == prior["receipt"]["source"] and _manifest(project) == prior["receipt"]["workspace"]:
            return prior["receipt"]
        transaction = _transaction(source, home)
        manifest_path = confined_path(source, f".harness/backups/{prior['transaction_id']}/manifest.json", allow_control=True)
        if manifest_path.exists():
            transaction.recover(prior["transaction_id"], "rollback")
    baseline = state["baseline"]
    current, candidate = _manifest(source), _manifest(project, independent=True)
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
    for relative in sorted(changed):
        if current.get(relative, {}).get("links", 1) != 1:
            raise WorkspaceConflict("Goal publication refuses a hard-linked target: " + relative, [relative])
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
            confined_path(project, relative).unlink(missing_ok=True)
    if _manifest(source) != current:
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
                             "source": current, "workspace": _manifest(project), "changed": sorted(changed),
                             "rebased": _contents(candidate) != _contents(_manifest(project)),
                             "reconciled": sorted(reconciled)})


def publish(document: dict[str, Any], runtime_root: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    """Apply only this goal's delta, retaining exact durable transaction identity."""
    source, home, project, state = _loaded(document, runtime_root)
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
        return dict(recovered["manifest"])
    if _manifest(source) != verified["source"] or _manifest(project) != verified["workspace"]:
        raise WorkspaceConflict("Goal publication snapshot changed after verification", list(verified["source"].keys() | verified["workspace"].keys()))
    plans = []
    for relative in verified["changed"]:
        before, after = verified["source"].get(relative), verified["workspace"].get(relative)
        content = confined_path(project, relative).read_bytes() if after else None
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
    _assert_published_snapshot(source, project, receipt)
    state["publication"].update(state="published", manifest=manifest)
    _write_state(home, project.parent, state)
    return manifest
