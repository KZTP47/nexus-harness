from __future__ import annotations

import copy
import fnmatch
import os
import shutil
import stat
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from .changes import atomic_write, sha256_bytes
from .config import LoadedConfig
from .execution import CommandRunner
from .models import ChangePlan, CommandResult, Deadline, HarnessError
from .safety import confined_path, portable_relative_path_key


def _remove_stage_tree(path: Path) -> None:
    """Remove a stage tree, including mode-preserved read-only files on Windows."""
    def make_writable_and_retry(function: object, target: str, _error: object) -> None:
        os.chmod(target, stat.S_IREAD | stat.S_IWRITE)
        function(target)  # type: ignore[operator]

    shutil.rmtree(path, onerror=make_writable_and_retry)


@dataclass(frozen=True)
class TextReplacement:
    """One exact text substitution in the current staged file."""

    old: str
    new: str
    count: int = 1


@dataclass(frozen=True)
class VerificationAction:
    """A verification command approved before the staged session starts."""

    name: str
    argv: tuple[str, ...]
    cwd: str = "."
    timeout_seconds: float | None = None


@dataclass(frozen=True)
class StagedVerification:
    action: str
    revision: int
    result: CommandResult

    def to_dict(self) -> dict[str, object]:
        return {"action": self.action, "revision": self.revision, "result": self.result.to_dict()}


@dataclass(frozen=True)
class StagedCandidate:
    """Verified changes ready for the existing FileTransaction boundary."""

    revision: int
    changes: tuple[ChangePlan, ...]
    verifications: tuple[StagedVerification, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "revision": self.revision,
            "changes": [asdict(change) for change in self.changes],
            "verifications": [verification.to_dict() for verification in self.verifications],
        }


@dataclass(frozen=True)
class _Snapshot:
    content: bytes | None
    mode: int | None

    @property
    def sha256(self) -> str | None:
        return None if self.content is None else sha256_bytes(self.content)


class StagedCodingWorkspace:
    """Confined candidate workspace for iterative edits and checks.

    Only planner-approved paths are copied. Mutations affect the temporary
    snapshot. ``finalize`` returns ChangePlan objects but never changes the
    source project; FileTransaction remains the sole commit boundary.
    """

    def __init__(
        self,
        config: LoadedConfig,
        approved_files: Iterable[str],
        verification_actions: Iterable[VerificationAction],
        *,
        support_files: Iterable[str] = (),
        generated_output_ignores: Iterable[str] = (),
        deadline: Deadline | None = None,
        preallocated_stage_root: Path | None = None,
    ):
        self.config = config
        self.root = config.project_root.resolve()
        self.deadline = deadline
        self.max_files = int(config.get("execution.max_changed_files"))
        self.max_bytes = int(config.get("execution.max_changed_bytes"))
        self.max_calls = int(config.get("workflow.max_tool_calls"))
        self.per_call_output_bytes = int(config.get("workflow.max_tool_output_bytes"))
        self.total_output_bytes = int(config.get("workflow.max_tool_total_bytes"))
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._owns_preallocated_root = preallocated_stage_root is not None
        if preallocated_stage_root is None:
            self._temporary = tempfile.TemporaryDirectory(prefix="our-harness-stage-")
            self.stage_root = Path(self._temporary.name).resolve()
        else:
            self.stage_root = self._validate_preallocated_stage_root(preallocated_stage_root)
        self._closed = False
        self._tainted = False
        self._calls = 0
        self._captured_output_bytes = 0
        self._action_ids: set[str] = set()
        self._revision = 0
        self._reasons: dict[str, str] = {}
        self._verifications: dict[str, StagedVerification] = {}
        try:
            self._approved = self._validate_approved_files(list(approved_files))
            self._support = self._validate_support_files(list(support_files))
            self._all_files = self._approved + self._support
            self._generated_output_ignores = self._validate_output_ignores(list(generated_output_ignores))
            self._actions = self._validate_actions(list(verification_actions))
            self._original: dict[str, _Snapshot] = {}
            support_bytes = 0
            support_limit = int(config.get("context.max_chars"))
            for path in self._all_files:
                snapshot = self._snapshot_source(path)
                self._original[path] = snapshot
                if path in self._support:
                    support_bytes += len(snapshot.content or b"")
                    if support_bytes > support_limit:
                        raise HarnessError(
                            f"Staged support files have {support_bytes} bytes; limit is context.max_chars"
                        )
            self._populate_stage()
            staged_data = copy.deepcopy(config.data)
            staged_data["execution"]["max_output_bytes"] = min(
                int(staged_data["execution"]["max_output_bytes"]), self.per_call_output_bytes
            )
            staged_config = LoadedConfig(
                staged_data,
                self.stage_root,
                [],
                dict(config.provenance),
                config.trusted_floor,
            )
            self._runner = CommandRunner(staged_config)
        except Exception:
            self.close()
            raise

    def __enter__(self) -> StagedCodingWorkspace:
        self._ensure_open()
        return self

    def __exit__(self, _kind: object, _value: object, _traceback: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._temporary is not None:
            self._temporary.cleanup()
            return
        if self._owns_preallocated_root:
            self._remove_preallocated_stage_root()

    @staticmethod
    def _validate_preallocated_stage_root(value: Path) -> Path:
        """Accept only an empty harness-owned directory under the OS temp root."""
        candidate = Path(value)
        temporary_root = Path(tempfile.gettempdir()).resolve()
        try:
            parent = candidate.parent.resolve(strict=True)
            metadata = candidate.lstat()
        except OSError as exc:
            raise HarnessError("Preallocated staged workspace root is unavailable") from exc
        attributes = getattr(metadata, "st_file_attributes", 0)
        if (
            not candidate.is_absolute()
            or parent != temporary_root
            or not candidate.name.startswith("our-harness-stage-")
            or candidate.name in {"our-harness-stage-", ".", ".."}
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
            or (os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077)
        ):
            raise HarnessError("Preallocated staged workspace root is not a private regular directory")
        try:
            if next(candidate.iterdir(), None) is not None:
                raise HarnessError("Preallocated staged workspace root must be empty")
        except OSError as exc:
            raise HarnessError("Preallocated staged workspace root cannot be inspected") from exc
        return candidate.resolve(strict=True)

    def _remove_preallocated_stage_root(self) -> None:
        """Remove only the exact regular temp directory accepted at construction."""
        try:
            metadata = self.stage_root.lstat()
        except FileNotFoundError:
            return
        attributes = getattr(metadata, "st_file_attributes", 0)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        ):
            raise HarnessError("Preallocated staged workspace root changed type; refusing cleanup")
        _remove_stage_tree(self.stage_root)

    def file_state(self, path: str) -> dict[str, object]:
        """Return the staged file hash used by the next structured edit."""
        self._ensure_usable("inspect staged file")
        normalized = self._approved_path(path)
        snapshot = self._snapshot_stage(normalized)
        return {
            "path": normalized,
            "exists": snapshot.content is not None,
            "sha256": snapshot.sha256,
            "bytes": len(snapshot.content or b""),
            "revision": self._revision,
        }

    def replace_file(
        self,
        action_id: str,
        path: str,
        expected_sha256: str | None,
        content: str | bytes,
        *,
        reason: str = "",
    ) -> dict[str, object]:
        """Replace one approved staged file after checking its current hash."""
        self._begin_action(action_id, "replace staged file")
        normalized = self._approved_path(path)
        current = self._snapshot_stage(normalized)
        self._assert_expected(normalized, expected_sha256, current.sha256)
        if not isinstance(content, (str, bytes)):
            raise HarnessError("replace_file content must be text or bytes")
        payload = content.encode("utf-8") if isinstance(content, str) else content
        self._check_candidate_budget(normalized, payload)
        target = confined_path(self.stage_root, normalized)
        mode = current.mode if current.mode is not None else self._original[normalized].mode
        atomic_write(target, payload, mode)
        self._record_mutation(normalized, reason)
        return self.file_state(normalized)

    def delete_file(
        self,
        action_id: str,
        path: str,
        expected_sha256: str,
        *,
        reason: str = "",
    ) -> dict[str, object]:
        """Delete one approved staged file after checking its current hash."""
        self._begin_action(action_id, "delete staged file")
        normalized = self._approved_path(path)
        current = self._snapshot_stage(normalized)
        self._assert_expected(normalized, expected_sha256, current.sha256)
        if current.content is None:
            raise HarnessError(f"Cannot delete a missing staged file: {normalized}")
        self._check_candidate_budget(normalized, None)
        confined_path(self.stage_root, normalized, allow_missing=False).unlink()
        self._record_mutation(normalized, reason)
        return self.file_state(normalized)

    def apply_patch(
        self,
        action_id: str,
        path: str,
        expected_sha256: str,
        replacements: Iterable[TextReplacement],
        *,
        reason: str = "",
    ) -> dict[str, object]:
        """Apply exact, counted substitutions to one UTF-8 staged file."""
        self._begin_action(action_id, "patch staged file")
        normalized = self._approved_path(path)
        current = self._snapshot_stage(normalized)
        self._assert_expected(normalized, expected_sha256, current.sha256)
        if current.content is None:
            raise HarnessError(f"Cannot patch a missing staged file: {normalized}")
        try:
            value = current.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HarnessError(f"apply_patch only accepts UTF-8 text files: {normalized}") from exc
        edits = list(replacements)
        if not edits:
            raise HarnessError("apply_patch needs at least one replacement")
        for edit in edits:
            if not isinstance(edit, TextReplacement):
                raise HarnessError("apply_patch replacements must be TextReplacement values")
            if not edit.old:
                raise HarnessError("apply_patch replacement text must not be empty")
            if isinstance(edit.count, bool) or not isinstance(edit.count, int) or edit.count < 1:
                raise HarnessError("apply_patch replacement count must be a positive integer")
            actual = value.count(edit.old)
            if actual != edit.count:
                raise HarnessError(
                    f"Patch context count mismatch for {normalized}: expected {edit.count}, found {actual}"
                )
            value = value.replace(edit.old, edit.new, edit.count)
        payload = value.encode("utf-8")
        self._check_candidate_budget(normalized, payload)
        atomic_write(confined_path(self.stage_root, normalized), payload, current.mode)
        self._record_mutation(normalized, reason)
        return self.file_state(normalized)

    def run_verification(self, action_id: str, action: str) -> StagedVerification:
        """Run one pre-approved command in the staged root without a shell."""
        self._begin_action(action_id, f"run verification {action}")
        definition = self._actions.get(action)
        if definition is None:
            raise HarnessError(f"Verification action was not approved: {action}")
        remaining_output = self.total_output_bytes - self._captured_output_bytes
        if remaining_output <= 0:
            raise HarnessError("Staged verification output budget is exhausted")
        timeout = definition.timeout_seconds
        configured_timeout = float(self.config.get("execution.timeout_seconds"))
        timeout = configured_timeout if timeout is None else min(configured_timeout, timeout)
        if self.deadline is not None:
            timeout = self.deadline.remaining_seconds(f"run verification {action}", cap=timeout)
        before = {path: self._snapshot_stage(path) for path in self._all_files}
        inventory_before = self._stage_inventory()
        result = self._runner.run(
            list(definition.argv),
            cwd=definition.cwd,
            timeout=timeout,
            max_output_bytes=min(self.per_call_output_bytes, remaining_output),
        )
        result = self._cap_result_output(result, min(self.per_call_output_bytes, remaining_output))
        self._captured_output_bytes += len(result.stdout.encode("utf-8")) + len(result.stderr.encode("utf-8"))
        try:
            after = {path: self._snapshot_stage(path) for path in self._all_files}
            inventory_after = self._stage_inventory()
        except HarnessError:
            self._tainted = True
            raise
        changed = [
            path
            for path in self._all_files
            if before[path].sha256 != after[path].sha256 or before[path].mode != after[path].mode
        ]
        if changed:
            self._tainted = True
            kind = "approved" if changed[0] in self._approved else "read-only support"
            raise HarnessError(f"Verification action changed a {kind} staged file: {changed[0]}")
        unexpected = sorted(
            path
            for path in inventory_after - inventory_before
            if not self._is_ignored_output(path)
        )
        if unexpected:
            self._tainted = True
            raise HarnessError(f"Verification action created an unexpected staged file: {unexpected[0]}")
        verification = StagedVerification(action, self._revision, result)
        self._verifications[action] = verification
        return verification

    def finalize(self) -> StagedCandidate:
        """Create a candidate only after all checks pass for this revision."""
        self._ensure_usable("finalize staged candidate")
        self._check_deadline("finalize staged candidate")
        missing = [
            name
            for name in self._actions
            if name not in self._verifications
            or self._verifications[name].revision != self._revision
            or not self._verifications[name].result.complete_success
        ]
        if missing:
            raise HarnessError(f"Staged candidate has checks that did not pass: {', '.join(missing)}")
        changes: list[ChangePlan] = []
        for path in self._all_files:
            original = self._original[path]
            current_source = self._snapshot_source(path)
            if current_source.sha256 != original.sha256 or current_source.mode != original.mode:
                raise HarnessError(f"Source baseline changed during staged repair: {path}")
            if path not in self._approved:
                continue
            staged = self._snapshot_stage(path)
            if staged.sha256 == original.sha256:
                continue
            changes.append(
                ChangePlan(
                    path=path,
                    baseline_sha256=original.sha256,
                    content=staged.content,
                    delete=staged.content is None,
                    reason=self._reasons.get(path, "staged repair"),
                    mode=staged.mode if staged.mode != original.mode else None,
                )
            )
        if not changes:
            raise HarnessError("Staged candidate contains no file changes")
        self._check_final_budget(changes)
        checks = tuple(self._verifications[name] for name in self._actions)
        return StagedCandidate(self._revision, tuple(changes), checks)

    def _validate_approved_files(self, files: list[str]) -> tuple[str, ...]:
        if not files:
            raise HarnessError("A staged workspace needs at least one planner-approved file")
        if len(files) > self.max_files:
            raise HarnessError(f"Staged workspace has {len(files)} files; limit is {self.max_files}")
        by_key: dict[str, str] = {}
        for value in files:
            if not isinstance(value, str):
                raise HarnessError("Planner-approved file paths must be strings")
            key = portable_relative_path_key(value)
            if key in by_key:
                raise HarnessError(f"Planner-approved files contain a portable path alias: {value}")
            confined_path(self.root, value)
            by_key[key] = Path(*value.replace("\\", "/").split("/")).as_posix()
        return tuple(by_key[key] for key in sorted(by_key))

    def _validate_support_files(self, files: list[str]) -> tuple[str, ...]:
        maximum = min(2_000, max(self.max_files, self.max_files * 20))
        if len(files) > maximum:
            raise HarnessError(f"Staged workspace has {len(files)} support files; limit is {maximum}")
        approved_keys = {portable_relative_path_key(path) for path in self._approved}
        by_key: dict[str, str] = {}
        for value in files:
            if not isinstance(value, str):
                raise HarnessError("Staged support file paths must be strings")
            key = portable_relative_path_key(value)
            if key in approved_keys or key in by_key:
                raise HarnessError(f"Staged support files contain a portable path alias: {value}")
            source = confined_path(self.root, value, allow_missing=False)
            if not source.is_file():
                raise HarnessError(f"Staged support path is not a regular file: {value}")
            by_key[key] = Path(*value.replace("\\", "/").split("/")).as_posix()
        return tuple(by_key[key] for key in sorted(by_key))

    @staticmethod
    def _validate_output_ignores(patterns: list[str]) -> tuple[str, ...]:
        result: list[str] = []
        for pattern in patterns:
            if (
                not isinstance(pattern, str)
                or not pattern
                or "\\" in pattern
                or pattern.startswith("/")
                or ".." in pattern.split("/")
            ):
                raise HarnessError(f"Generated-output ignore must be a safe relative glob: {pattern}")
            result.append(pattern)
        return tuple(result)

    @staticmethod
    def _validate_actions(actions: list[VerificationAction]) -> dict[str, VerificationAction]:
        if not actions:
            raise HarnessError("A staged workspace needs at least one verification action")
        result: dict[str, VerificationAction] = {}
        for action in actions:
            if not isinstance(action, VerificationAction):
                raise HarnessError("Verification actions must be VerificationAction values")
            if not action.name or action.name in result:
                raise HarnessError(f"Verification action name is empty or repeated: {action.name}")
            if not action.argv or not all(isinstance(part, str) and part for part in action.argv):
                raise HarnessError(f"Verification action argv is invalid: {action.name}")
            if action.timeout_seconds is not None and action.timeout_seconds <= 0:
                raise HarnessError(f"Verification action timeout is invalid: {action.name}")
            result[action.name] = action
        return result

    def _populate_stage(self) -> None:
        for path, snapshot in self._original.items():
            if snapshot.content is not None:
                atomic_write(confined_path(self.stage_root, path), snapshot.content, snapshot.mode)

    def _snapshot_source(self, path: str) -> _Snapshot:
        return self._read_regular_snapshot(confined_path(self.root, path), path)

    def _snapshot_stage(self, path: str) -> _Snapshot:
        return self._read_regular_snapshot(confined_path(self.stage_root, path), path)

    def _stage_inventory(self) -> set[str]:
        inventory: set[str] = set()
        pending = [self.stage_root]
        while pending:
            directory = pending.pop()
            for entry in os.scandir(directory):
                path = Path(entry.path)
                relative = path.relative_to(self.stage_root).as_posix()
                metadata = entry.stat(follow_symlinks=False)
                attributes = getattr(metadata, "st_file_attributes", 0)
                if entry.is_symlink() or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
                    raise HarnessError(f"Verification action created a linked staged path: {relative}")
                confined_path(self.stage_root, relative, allow_missing=False)
                if stat.S_ISDIR(metadata.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(metadata.st_mode):
                    inventory.add(relative)
                else:
                    raise HarnessError(f"Verification action created a non-regular staged path: {relative}")
        return inventory

    def _is_ignored_output(self, path: str) -> bool:
        return any(fnmatch.fnmatchcase(path, pattern) for pattern in self._generated_output_ignores)

    @staticmethod
    def _cap_result_output(result: CommandResult, limit: int) -> CommandResult:
        remaining = max(0, limit)

        def take(value: str) -> str:
            nonlocal remaining
            encoded = value.encode("utf-8")
            accepted = encoded[:remaining]
            remaining -= len(accepted)
            return accepted.decode("utf-8", errors="ignore")

        stdout = take(result.stdout)
        stderr = take(result.stderr)
        truncated = result.output_truncated or stdout != result.stdout or stderr != result.stderr
        return CommandResult(
            argv=result.argv,
            cwd=result.cwd,
            exit_code=result.exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=result.duration_ms,
            timed_out=result.timed_out,
            output_truncated=truncated,
        )

    def _read_regular_snapshot(self, target: Path, label: str) -> _Snapshot:
        try:
            with target.open("rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise HarnessError(f"Staged path is not a regular file: {label}")
                content = stream.read()
                after = os.fstat(stream.fileno())
        except FileNotFoundError:
            return _Snapshot(None, None)
        except (IsADirectoryError, PermissionError) as exc:
            raise HarnessError(f"Cannot read staged file: {label}") from exc
        identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if identity_before != identity_after or len(content) != after.st_size:
            raise HarnessError(f"File changed while staged snapshot was read: {label}")
        if len(content) > int(self.config.get("project.max_file_bytes")):
            raise HarnessError(f"Planner-approved file exceeds project.max_file_bytes: {label}")
        return _Snapshot(content, stat.S_IMODE(after.st_mode))

    def _approved_path(self, path: str) -> str:
        key = portable_relative_path_key(path)
        for approved in self._approved:
            if portable_relative_path_key(approved) == key:
                return approved
        raise HarnessError(f"Staged mutation path was not planner-approved: {path}")

    def _begin_action(self, action_id: str, operation: str) -> None:
        self._ensure_usable(operation)
        self._check_deadline(operation)
        if not isinstance(action_id, str) or not action_id.strip():
            raise HarnessError("Staged action_id must be a non-empty string")
        if action_id in self._action_ids:
            raise HarnessError(f"Staged action_id was already used: {action_id}")
        if self._calls >= self.max_calls:
            raise HarnessError(f"Staged tool-call budget exhausted at {self.max_calls} calls")
        self._action_ids.add(action_id)
        self._calls += 1

    def _record_mutation(self, path: str, reason: str) -> None:
        self._revision += 1
        self._reasons[path] = reason or self._reasons.get(path, "staged repair")
        self._verifications.clear()

    def _check_candidate_budget(self, target_path: str, target_content: bytes | None) -> None:
        changed: list[tuple[str, int]] = []
        for path in self._approved:
            staged = self._snapshot_stage(path)
            content = target_content if path == target_path else staged.content
            content_hash = None if content is None else sha256_bytes(content)
            if content_hash != self._original[path].sha256:
                changed.append((path, len(content or b"")))
        if len(changed) > self.max_files:
            raise HarnessError(f"Staged candidate has {len(changed)} files; limit is {self.max_files}")
        total = sum(size for _, size in changed)
        if total > self.max_bytes:
            raise HarnessError(f"Staged candidate has {total} bytes; limit is {self.max_bytes}")

    def _check_final_budget(self, changes: list[ChangePlan]) -> None:
        if len(changes) > self.max_files:
            raise HarnessError(f"Staged candidate has {len(changes)} files; limit is {self.max_files}")
        total = sum(len(change.content or b"") if isinstance(change.content, bytes) else len((change.content or "").encode("utf-8")) for change in changes)
        if total > self.max_bytes:
            raise HarnessError(f"Staged candidate has {total} bytes; limit is {self.max_bytes}")

    @staticmethod
    def _assert_expected(path: str, expected: str | None, actual: str | None) -> None:
        if expected != actual:
            raise HarnessError(f"Staged baseline conflict: {path}; inspect the file and make a new patch")

    def _check_deadline(self, operation: str) -> None:
        if self.deadline is not None:
            self.deadline.check(operation)

    def _ensure_open(self) -> None:
        if self._closed:
            raise HarnessError("Staged workspace is closed")

    def _ensure_usable(self, operation: str) -> None:
        self._ensure_open()
        if self._tainted:
            raise HarnessError(f"Staged workspace was changed by a verification action; cannot {operation}")
