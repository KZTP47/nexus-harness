from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, TypeAlias

from .changes import atomic_write, sha256_bytes
from .config import LoadedConfig
from .models import Deadline, HarnessError
from .redaction import CredentialRedactor
from .safety import ProjectTransactionLock, confined_path, portable_relative_path_key
from .staged_coding import (
    _remove_stage_tree,
    StagedCandidate,
    StagedCodingWorkspace,
    StagedVerification,
    TextReplacement,
    VerificationAction,
)


_SCHEMA_VERSION = 3
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CHECKPOINT_FIELDS = {
    "schema_version", "session_id", "project_identity", "spec_sha256", "generation", "calls_consumed",
    "baseline", "files", "journal", "chain_head", "pending_action", "tainted",
    "stage", "checkpoint_hmac_sha256",
}
_MAX_PROJECT_GUARD_FILES = 100_000
_MAX_PROJECT_GUARD_BYTES = 2_000_000_000
_MAX_STAGE_REGISTRY_BYTES = 4_096


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _hmac_digest(key: bytes, value: object) -> str:
    return hmac.new(key, _canonical(value), hashlib.sha256).hexdigest()


def _project_identity(project_root: Path) -> str:
    root = project_root.resolve(strict=True)
    metadata = root.stat()
    return _digest({
        "canonical_path": os.path.normcase(str(root)),
        "device": int(metadata.st_dev),
        "inode": int(metadata.st_ino),
    })


def _normalize_paths(values: list[str], label: str) -> list[str]:
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value:
            raise HarnessError(f"Programmatic workspace {label} paths must be non-empty strings")
        normalized.append(Path(*value.replace("\\", "/").split("/")).as_posix())
    return normalized


def _checkpoint_key_path(project_root: Path) -> Path:
    override = os.environ.get("OUR_HARNESS_CHECKPOINT_KEY_FILE", "").strip()
    if override:
        candidate = Path(override).expanduser()
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", "")).expanduser() if os.environ.get("LOCALAPPDATA") else Path.home() / "AppData" / "Local"
        candidate = base / "OurHarness" / "programmatic-checkpoint-v1.key"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", "")).expanduser() if os.environ.get("XDG_CONFIG_HOME") else Path.home() / ".config"
        candidate = base / "our-harness" / "programmatic-checkpoint-v1.key"
    path = candidate.resolve()
    root = project_root.resolve()
    if path == root or root in path.parents:
        raise HarnessError("Programmatic checkpoint authentication key must be outside the project")
    return path


def _load_checkpoint_key(project_root: Path) -> bytes:
    path = _checkpoint_key_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            metadata = path.lstat()
        else:
            key = secrets.token_bytes(32)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(key)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
                return key
            except Exception:
                try:
                    path.unlink()
                except OSError:
                    pass
                raise
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise HarnessError("Programmatic checkpoint authentication key is not a regular file")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise HarnessError("Programmatic checkpoint authentication key permissions are too broad")
    key = path.read_bytes()
    if len(key) != 32:
        raise HarnessError("Programmatic checkpoint authentication key is invalid")
    return key


class _StageLease:
    def __init__(self, path: Path, payload: bytes, *, create: bool):
        self.path = path
        self.stream: Any = None
        mode = "x+b" if create else "r+b"
        try:
            stream = path.open(mode)
        except OSError as exc:
            raise HarnessError("Cannot open the programmatic stage lease") from exc
        try:
            if create:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            else:
                retained = stream.read(_MAX_STAGE_REGISTRY_BYTES + 1)
                if retained != payload:
                    raise HarnessError("Programmatic stage lease identity is invalid")
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.stream = stream
        except Exception:
            stream.close()
            if create:
                try:
                    path.unlink()
                except OSError:
                    pass
            raise

    def close(self, *, remove: bool) -> None:
        stream = self.stream
        self.stream = None
        if stream is not None:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()
        if remove:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


@dataclass(frozen=True)
class InspectFile:
    path: str


@dataclass(frozen=True)
class ReplaceFile:
    action_id: str
    path: str
    expected_sha256: str | None
    content: str
    reason: str = ""


@dataclass(frozen=True)
class ApplyPatch:
    action_id: str
    path: str
    expected_sha256: str
    replacements: tuple[TextReplacement, ...]
    reason: str = ""


@dataclass(frozen=True)
class DeleteFile:
    action_id: str
    path: str
    expected_sha256: str
    reason: str = ""


@dataclass(frozen=True)
class RunVerification:
    action_id: str
    action: str


@dataclass(frozen=True)
class FinalizeCandidate:
    pass


ProgrammaticAction: TypeAlias = (
    InspectFile | ReplaceFile | ApplyPatch | DeleteFile | RunVerification | FinalizeCandidate
)
ProgrammaticResult: TypeAlias = dict[str, object] | StagedVerification | StagedCandidate


class PersistentProgrammaticWorkspace:
    """Restartable typed controller for a confined staged coding snapshot.

    The durable file is an integrity-checked replay checkpoint, not an
    executable authority. Restore requires the caller to supply the same
    planner-approved paths and trusted verification definitions. Verification
    results are never trusted across restart and must run again.
    """

    def __init__(
        self,
        config: LoadedConfig,
        session_id: str,
        approved_files: list[str],
        verification_actions: list[VerificationAction],
        *,
        support_files: list[str] | None = None,
        generated_output_ignores: list[str] | None = None,
        deadline: Deadline | None = None,
        restore: bool = False,
        project_lock: ProjectTransactionLock | None = None,
    ):
        if not isinstance(session_id, str) or _SESSION_ID.fullmatch(session_id) is None:
            raise HarnessError("Programmatic workspace session ID is invalid")
        self.config = config
        self.root = config.project_root.resolve()
        self._project_identity = _project_identity(self.root)
        self._checkpoint_key = _load_checkpoint_key(self.root)
        self.session_id = session_id
        self.deadline = deadline
        self._support_files = _normalize_paths(list(support_files or []), "support")
        self._generated_output_ignores = list(generated_output_ignores or [])
        self._approved_files = _normalize_paths(list(approved_files), "approved")
        self._verification_actions = list(verification_actions)
        self._redactor = CredentialRedactor(config)
        self._lock = project_lock or ProjectTransactionLock(self.root)
        if self._lock.root != self.root:
            raise HarnessError("Programmatic workspace lock belongs to a different project")
        self._checkpoint_path = confined_path(
            self.root,
            Path(".harness") / "checkpoints" / "programmatic" / f"{session_id}.json",
            allow_control=True,
        )
        self._spec_sha256 = self._spec_digest()
        self._workspace: StagedCodingWorkspace | None = None
        self._stage_lease: _StageLease | None = None
        self._stage_record: dict[str, str] | None = None
        self._closed = False
        self._tainted = False
        self._generation = 0
        self._calls_consumed = 0
        self._baseline: dict[str, dict[str, object]] = {}
        self._files: dict[str, dict[str, object]] = {}
        self._journal: list[dict[str, object]] = []
        self._chain_head = "0" * 64
        self._pending_action: dict[str, object] | None = None
        self._action_ids: set[str] = set()
        reserved_stage: Path | None = None
        try:
            self._scavenge_stale_stages()
            if restore:
                document = self._read_checkpoint()
                if document.get("project_identity") != self._project_identity:
                    raise HarnessError("Programmatic workspace checkpoint belongs to a different project")
                self._cleanup_recorded_stage(document.get("stage"))
                self._load_document(document)
            else:
                if self._checkpoint_path.exists():
                    raise HarnessError(f"Programmatic workspace already exists: {session_id}")
            reserved_stage = self._reserve_stage_root()
            self._acquire_stage_lease(reserved_stage)
            self._workspace = StagedCodingWorkspace(
                config,
                self._approved_files,
                self._verification_actions,
                support_files=self._support_files,
                generated_output_ignores=self._generated_output_ignores,
                deadline=deadline,
                preallocated_stage_root=reserved_stage,
            )
            reserved_stage = None
            if restore:
                self._restore_snapshot()
                self._persist()
            else:
                self._capture_initial_state()
                self._persist(expect_absent=True)
        except Exception:
            try:
                if reserved_stage is not None:
                    self._remove_reserved_stage_root(reserved_stage)
            finally:
                self.close()
            raise

    @classmethod
    def open(
        cls,
        config: LoadedConfig,
        session_id: str,
        approved_files: list[str],
        verification_actions: list[VerificationAction],
        *,
        support_files: list[str] | None = None,
        generated_output_ignores: list[str] | None = None,
        deadline: Deadline | None = None,
        project_lock: ProjectTransactionLock | None = None,
    ) -> PersistentProgrammaticWorkspace:
        return cls(
            config,
            session_id,
            approved_files,
            verification_actions,
            support_files=support_files,
            generated_output_ignores=generated_output_ignores,
            deadline=deadline,
            restore=True,
            project_lock=project_lock,
        )

    @property
    def checkpoint_path(self) -> Path:
        return self._checkpoint_path

    @property
    def stage_root(self) -> Path:
        return self._stage().stage_root

    def _stage_registry_directory(self) -> Path:
        path = Path(tempfile.gettempdir()).resolve() / "our-harness-stage-registry-v1"
        path.mkdir(mode=0o700, exist_ok=True)
        metadata = path.lstat()
        attributes = getattr(metadata, "st_file_attributes", 0)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
            or os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise HarnessError("Programmatic stage registry is not a private regular directory")
        return path

    def _stage_lease_path(self) -> Path:
        name = _hmac_digest(
            self._checkpoint_key,
            {"project_identity": self._project_identity, "session_id": self.session_id},
        )
        return self._stage_registry_directory() / f"{name}.lease"

    def _validated_stage_record(self, value: object) -> tuple[Path, Path, str, int]:
        if not isinstance(value, dict) or set(value) != {"path", "nonce", "created_at_ms"}:
            raise HarnessError("Programmatic workspace stage record is invalid")
        raw_path, nonce, created_at_ms = (
            value.get("path"), value.get("nonce"), value.get("created_at_ms"),
        )
        if (
            not isinstance(raw_path, str)
            or not isinstance(nonce, str)
            or _DIGEST.fullmatch(nonce) is None
            or isinstance(created_at_ms, bool)
            or not isinstance(created_at_ms, int)
            or created_at_ms < 0
        ):
            raise HarnessError("Programmatic workspace stage record is invalid")
        stage = Path(raw_path)
        temporary_root = Path(tempfile.gettempdir()).resolve()
        try:
            resolved_parent = stage.parent.resolve(strict=True)
        except OSError as exc:
            raise HarnessError("Programmatic workspace stage parent is unavailable") from exc
        if (
            not stage.is_absolute()
            or resolved_parent != temporary_root
            or not stage.name.startswith("our-harness-stage-")
            or stage.name in {"our-harness-stage-", ".", ".."}
        ):
            raise HarnessError("Programmatic workspace stage path is outside the temporary root")
        canonical = resolved_parent / stage.name
        lease_path = self._stage_lease_path()
        return canonical, lease_path, nonce, created_at_ms

    def _stage_registry_payload(self, value: object) -> bytes:
        stage, _, nonce, created_at_ms = self._validated_stage_record(value)
        material: dict[str, object] = {
            "schema_version": 1,
            "stage_path": str(stage),
            "nonce": nonce,
            "created_at_ms": created_at_ms,
            "project_identity": self._project_identity,
            "session_id": self.session_id,
        }
        material["registry_hmac_sha256"] = _hmac_digest(self._checkpoint_key, material)
        payload = _canonical(material)
        if len(payload) > _MAX_STAGE_REGISTRY_BYTES:
            raise HarnessError("Programmatic stage registry exceeds its byte limit")
        return payload

    def _stage_record_from_registry(self, raw: bytes) -> dict[str, object] | None:
        if len(raw) > _MAX_STAGE_REGISTRY_BYTES:
            return None
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        fields = {
            "schema_version", "stage_path", "nonce", "created_at_ms",
            "project_identity", "session_id", "registry_hmac_sha256",
        }
        if not isinstance(value, dict) or set(value) != fields or value.get("schema_version") != 1:
            return None
        supplied = value.get("registry_hmac_sha256")
        material = dict(value)
        material.pop("registry_hmac_sha256", None)
        if (
            not isinstance(supplied, str)
            or not hmac.compare_digest(supplied, _hmac_digest(self._checkpoint_key, material))
            or value.get("project_identity") != self._project_identity
            or value.get("session_id") != self.session_id
        ):
            return None
        record = {
            "path": value.get("stage_path"),
            "nonce": value.get("nonce"),
            "created_at_ms": value.get("created_at_ms"),
        }
        try:
            self._validated_stage_record(record)
        except HarnessError:
            return None
        return record

    def _scavenge_stale_stages(self) -> None:
        lease_path = self._stage_lease_path()
        try:
            metadata = lease_path.lstat()
        except FileNotFoundError:
            return
        attributes = getattr(metadata, "st_file_attributes", 0)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
            or metadata.st_size > _MAX_STAGE_REGISTRY_BYTES
        ):
            return
        try:
            with lease_path.open("rb") as stream:
                before = os.fstat(stream.fileno())
                raw = stream.read(_MAX_STAGE_REGISTRY_BYTES + 1)
                after = os.fstat(stream.fileno())
        except OSError:
            return
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
        ):
            return
        record = self._stage_record_from_registry(raw)
        if record is not None:
            self._cleanup_recorded_stage(record)

    def _reserve_stage_root(self) -> Path:
        stage = Path(tempfile.mkdtemp(prefix="our-harness-stage-")).resolve(strict=True)
        metadata = stage.lstat()
        attributes = getattr(metadata, "st_file_attributes", 0)
        if (
            stage.parent != Path(tempfile.gettempdir()).resolve()
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
            or (os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077)
        ):
            self._remove_reserved_stage_root(stage)
            raise HarnessError("Cannot reserve a private programmatic stage root")
        return stage

    @staticmethod
    def _remove_reserved_stage_root(stage: Path) -> None:
        try:
            metadata = stage.lstat()
        except FileNotFoundError:
            return
        attributes = getattr(metadata, "st_file_attributes", 0)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        ):
            raise HarnessError("Reserved programmatic stage root changed type; refusing cleanup")
        _remove_stage_tree(stage)

    def _acquire_stage_lease(self, stage: Path) -> None:
        stage = stage.resolve(strict=True)
        nonce = secrets.token_hex(32)
        record = {"path": str(stage), "nonce": nonce, "created_at_ms": int(time.time() * 1000)}
        _, lease_path, _, _ = self._validated_stage_record(record)
        self._stage_lease = _StageLease(
            lease_path, self._stage_registry_payload(record), create=True,
        )
        self._stage_record = record

    def _cleanup_recorded_stage(self, value: object) -> None:
        stage, lease_path, _, _ = self._validated_stage_record(value)
        if stage.exists():
            metadata = stage.lstat()
            attributes = getattr(metadata, "st_file_attributes", 0)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
            ):
                raise HarnessError("Programmatic workspace stale stage is not a regular directory")
            try:
                lease_metadata = lease_path.lstat()
            except FileNotFoundError:
                raise HarnessError("Programmatic workspace stage lease is missing; refusing cleanup")
            if stat.S_ISLNK(lease_metadata.st_mode) or not stat.S_ISREG(lease_metadata.st_mode):
                raise HarnessError("Programmatic workspace stage lease is not a regular file")
        elif not lease_path.exists():
            return
        if lease_path.exists():
            lease_metadata = lease_path.lstat()
            if stat.S_ISLNK(lease_metadata.st_mode) or not stat.S_ISREG(lease_metadata.st_mode):
                raise HarnessError("Programmatic workspace stage lease is not a regular file")
        try:
            lease = _StageLease(
                lease_path, self._stage_registry_payload(value), create=False,
            )
        except Exception as exc:
            raise HarnessError("Programmatic workspace stage is still active; refusing cleanup") from exc
        try:
            if stage.exists():
                _remove_stage_tree(stage)
        finally:
            lease.close(remove=True)

    def __enter__(self) -> PersistentProgrammaticWorkspace:
        self._ensure_usable("enter")
        return self

    def __exit__(self, _kind: object, _value: object, _traceback: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        workspace, self._workspace = self._workspace, None
        lease, self._stage_lease = self._stage_lease, None
        try:
            if workspace is not None:
                workspace.close()
        finally:
            if lease is not None:
                lease.close(remove=True)

    def discard(self) -> None:
        """Explicitly remove the durable replay checkpoint and temporary stage."""
        self.close()
        with self._lock.held(timeout_seconds=5):
            path = confined_path(
                self.root,
                Path(".harness") / "checkpoints" / "programmatic" / f"{self.session_id}.json",
                allow_missing=False,
                allow_control=True,
            )
            path.unlink()

    def execute(self, action: ProgrammaticAction) -> ProgrammaticResult:
        self._ensure_usable("execute an action")
        if isinstance(action, InspectFile):
            return self._stage().file_state(action.path)
        if isinstance(action, ReplaceFile):
            return self._replace(action)
        if isinstance(action, ApplyPatch):
            return self._patch(action)
        if isinstance(action, DeleteFile):
            return self._delete(action)
        if isinstance(action, RunVerification):
            return self._verify(action)
        if isinstance(action, FinalizeCandidate):
            return self._stage().finalize()
        raise HarnessError("Programmatic workspace action has an unsupported type")

    def _replace(self, action: ReplaceFile) -> dict[str, object]:
        self._require_persistable(action.content, "replacement content")
        self._require_persistable(action.reason, "reason")
        before = self._known_state(action.path)
        if action.expected_sha256 != before["sha256"]:
            raise HarnessError(f"Programmatic workspace baseline conflict: {action.path}")
        payload_sha256 = _digest(asdict(action))
        self._begin_persistent_action(action.action_id, "replace_file", payload_sha256)
        try:
            result = self._stage().replace_file(
                action.action_id, action.path, action.expected_sha256, action.content, reason=action.reason,
            )
        except Exception:
            self._fail_closed()
            raise
        self._record_file(action.path, action.content.encode("utf-8"), action.reason)
        self._complete_action(
            action.action_id, "replace_file", action.path, before["sha256"], result["sha256"], payload_sha256,
        )
        return result

    def _patch(self, action: ApplyPatch) -> dict[str, object]:
        self._require_persistable(action.reason, "reason")
        before = self._known_state(action.path)
        if action.expected_sha256 != before["sha256"]:
            raise HarnessError(f"Programmatic workspace baseline conflict: {action.path}")
        content = before.get("content")
        if not isinstance(content, bytes):
            raise HarnessError(f"Cannot patch a missing programmatic workspace file: {action.path}")
        try:
            value = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HarnessError(f"Programmatic patch accepts UTF-8 text only: {action.path}") from exc
        for replacement in action.replacements:
            if not isinstance(replacement, TextReplacement):
                raise HarnessError("Programmatic patch replacements must be TextReplacement values")
            self._require_persistable(replacement.old, "patch context")
            self._require_persistable(replacement.new, "patch replacement")
            if (
                not replacement.old
                or isinstance(replacement.count, bool)
                or not isinstance(replacement.count, int)
                or replacement.count < 1
            ):
                raise HarnessError("Programmatic patch replacement is invalid")
            if value.count(replacement.old) != replacement.count:
                raise HarnessError(f"Programmatic patch context count mismatch: {action.path}")
            value = value.replace(replacement.old, replacement.new, replacement.count)
        payload_sha256 = _digest(asdict(action))
        self._begin_persistent_action(action.action_id, "apply_patch", payload_sha256)
        try:
            result = self._stage().apply_patch(
                action.action_id,
                action.path,
                action.expected_sha256,
                action.replacements,
                reason=action.reason,
            )
        except Exception:
            self._fail_closed()
            raise
        payload = value.encode("utf-8")
        if sha256_bytes(payload) != result["sha256"]:
            self._fail_closed()
            raise HarnessError("Programmatic patch replay diverged from staged content")
        self._record_file(action.path, payload, action.reason)
        self._complete_action(
            action.action_id, "apply_patch", action.path, before["sha256"], result["sha256"], payload_sha256,
        )
        return result

    def _delete(self, action: DeleteFile) -> dict[str, object]:
        self._require_persistable(action.reason, "reason")
        before = self._known_state(action.path)
        if action.expected_sha256 != before["sha256"]:
            raise HarnessError(f"Programmatic workspace baseline conflict: {action.path}")
        payload_sha256 = _digest(asdict(action))
        self._begin_persistent_action(action.action_id, "delete_file", payload_sha256)
        try:
            result = self._stage().delete_file(
                action.action_id, action.path, action.expected_sha256, reason=action.reason,
            )
        except Exception:
            self._fail_closed()
            raise
        self._record_file(action.path, None, action.reason)
        self._complete_action(
            action.action_id, "delete_file", action.path, before["sha256"], None, payload_sha256,
        )
        return result

    def _verify(self, action: RunVerification) -> StagedVerification:
        if self.config.get("execution.mode") != "docker":
            raise HarnessError(
                "Persistent programmatic verification requires execution.mode=docker; "
                "host-process verification is not an isolation boundary"
            )
        payload_sha256 = _digest(asdict(action))
        with self._lock.held(timeout_seconds=5):
            project_before = self._project_guard_manifest()
            self._begin_persistent_action(
                action.action_id, "run_verification", payload_sha256,
                project_manifest_sha256=str(project_before["sha256"]),
            )
            try:
                result = self._stage().run_verification(action.action_id, action.action)
            except Exception:
                try:
                    self._check_project_guard(project_before)
                finally:
                    self._fail_closed()
                raise
            self._check_project_guard(project_before)
            summary = {
                "action": action.action,
                "revision": result.revision,
                "exit_code": result.result.exit_code,
                "timed_out": result.result.timed_out,
                "stdout_sha256": sha256_bytes(result.result.stdout.encode("utf-8")),
                "stderr_sha256": sha256_bytes(result.result.stderr.encode("utf-8")),
            }
            self._complete_action(
                action.action_id, "run_verification", None, None, None, _digest(summary),
            )
            return result

    def _capture_initial_state(self) -> None:
        for path in self._approved_files:
            normalized = str(self._stage().file_state(path)["path"])
            self._baseline[normalized] = self._source_identity(normalized, allow_missing=True)
        for path in self._support_files:
            normalized = Path(*path.replace("\\", "/").split("/")).as_posix()
            self._baseline[normalized] = self._source_identity(normalized, allow_missing=False)

    def _restore_snapshot(self) -> None:
        for path, expected in self._baseline.items():
            actual = self._source_identity(path, allow_missing=path in self._approved_files)
            if actual != expected:
                raise HarnessError(f"Programmatic workspace source baseline changed: {path}")
        for sequence, (path, snapshot) in enumerate(sorted(self._files.items()), 1):
            baseline = self._stage().file_state(path)["sha256"]
            content = self._decode_content(snapshot.get("content"), path)
            if content is None:
                if baseline is not None:
                    self._stage().delete_file(f"restore-{sequence}", path, str(baseline), reason=str(snapshot.get("reason") or "restored"))
            else:
                self._stage().replace_file(
                    f"restore-{sequence}", path, baseline if isinstance(baseline, str) else None,
                    content, reason=str(snapshot.get("reason") or "restored"),
                )
        # Verification state intentionally remains empty after reconstruction.

    def _source_identity(
        self, path: str, *, allow_missing: bool, allow_control: bool = False,
    ) -> dict[str, object]:
        target = confined_path(
            self.root, path, allow_missing=allow_missing, allow_control=allow_control,
        )
        try:
            stream = target.open("rb")
        except FileNotFoundError:
            if allow_missing:
                return {
                    "kind": "missing", "sha256": None, "byte_count": 0, "mode": None,
                    "device": None, "inode": None, "mtime_ns": None,
                }
            raise HarnessError(f"Programmatic workspace source file is missing: {path}")
        except IsADirectoryError as exc:
            raise HarnessError(f"Programmatic workspace source is not a regular file: {path}") from exc
        with stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise HarnessError(f"Programmatic workspace source is not a regular file: {path}")
            digest = hashlib.sha256()
            byte_count = 0
            while True:
                chunk = stream.read(1_048_576)
                if not chunk:
                    break
                byte_count += len(chunk)
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        before_identity = (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
            stat.S_IMODE(before.st_mode),
        )
        after_identity = (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
            stat.S_IMODE(after.st_mode),
        )
        if before_identity != after_identity or byte_count != after.st_size:
            raise HarnessError(f"Programmatic workspace source changed while it was read: {path}")
        return {
            "kind": "regular", "sha256": digest.hexdigest(), "byte_count": byte_count,
            "mode": stat.S_IMODE(after.st_mode), "device": int(after.st_dev),
            "inode": int(after.st_ino), "mtime_ns": int(after.st_mtime_ns),
        }

    def _project_guard_manifest(self) -> dict[str, object]:
        checkpoint_relative = self._checkpoint_path.relative_to(self.root).as_posix()
        excluded = {
            portable_relative_path_key(checkpoint_relative, allow_control=True),
            portable_relative_path_key(".harness/transaction.lock", allow_control=True),
        }
        entries: list[dict[str, object]] = []
        pending = [self.root]
        file_count = 0
        total_bytes = 0
        while pending:
            directory = pending.pop()
            try:
                children = sorted(os.scandir(directory), key=lambda item: item.name)
            except OSError as exc:
                raise HarnessError(f"Cannot inspect project for verification isolation: {directory}") from exc
            for child in children:
                target = Path(child.path)
                relative = target.relative_to(self.root).as_posix()
                key = portable_relative_path_key(relative, allow_control=True)
                if key in excluded:
                    continue
                metadata = child.stat(follow_symlinks=False)
                attributes = getattr(metadata, "st_file_attributes", 0)
                if child.is_symlink() or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
                    raise HarnessError(f"Project verification guard rejects linked paths: {relative}")
                if stat.S_ISDIR(metadata.st_mode):
                    entries.append({
                        "path": relative, "kind": "directory", "mode": stat.S_IMODE(metadata.st_mode),
                        "device": int(metadata.st_dev), "inode": int(metadata.st_ino),
                    })
                    pending.append(target)
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise HarnessError(f"Project verification guard rejects special paths: {relative}")
                file_count += 1
                if file_count > _MAX_PROJECT_GUARD_FILES:
                    raise HarnessError(
                        f"Project verification guard exceeds {_MAX_PROJECT_GUARD_FILES} files"
                    )
                identity = self._source_identity(
                    relative, allow_missing=False, allow_control=True,
                )
                total_bytes += int(identity["byte_count"])
                if total_bytes > _MAX_PROJECT_GUARD_BYTES:
                    raise HarnessError(
                        f"Project verification guard exceeds {_MAX_PROJECT_GUARD_BYTES} bytes"
                    )
                entries.append({"path": relative, **identity})
        entries.sort(key=lambda item: portable_relative_path_key(str(item["path"]), allow_control=True))
        return {
            "sha256": _digest(entries), "entries": len(entries),
            "files": file_count, "bytes": total_bytes,
        }

    def _check_project_guard(self, expected: dict[str, object]) -> None:
        actual = self._project_guard_manifest()
        if actual != expected:
            self._fail_closed()
            raise HarnessError("Verification action changed the source project outside the staged workspace")

    def _known_state(self, path: str) -> dict[str, object]:
        key = portable_relative_path_key(path)
        normalized = next(
            (item for item in self._approved_files if portable_relative_path_key(item) == key),
            None,
        )
        if normalized is None:
            raise HarnessError(f"Programmatic workspace path was not planner-approved: {path}")
        persisted = self._files.get(normalized)
        if persisted is not None:
            content = self._decode_content(persisted.get("content"), normalized)
            return {"path": normalized, "sha256": None if content is None else sha256_bytes(content), "content": content}
        stage_path = confined_path(self._stage().stage_root, normalized)
        if not stage_path.exists():
            return {"path": normalized, "sha256": None, "content": None}
        content = stage_path.read_bytes()
        return {"path": normalized, "sha256": sha256_bytes(content), "content": content}

    def _record_file(self, path: str, content: bytes | None, reason: str) -> None:
        normalized = str(self._known_state(path)["path"])
        baseline = self._baseline[normalized]["sha256"]
        current = None if content is None else sha256_bytes(content)
        if current == baseline:
            self._files.pop(normalized, None)
            return
        self._files[normalized] = {
            "content": self._encode_content(content),
            "sha256": current,
            "reason": reason or "programmatic workspace edit",
        }

    def _begin_persistent_action(
        self,
        action_id: str,
        kind: str,
        payload_sha256: str,
        *,
        project_manifest_sha256: str | None = None,
    ) -> None:
        if not isinstance(action_id, str) or not action_id.strip():
            raise HarnessError("Programmatic action ID must be a non-empty string")
        if action_id in self._action_ids:
            raise HarnessError(f"Programmatic action ID was already used: {action_id}")
        if self._pending_action is not None:
            raise HarnessError("Programmatic workspace has an uncertain in-flight action")
        if kind not in {"replace_file", "apply_patch", "delete_file", "run_verification"}:
            raise HarnessError("Programmatic action kind is invalid")
        if _DIGEST.fullmatch(payload_sha256) is None:
            raise HarnessError("Programmatic action payload digest is invalid")
        if project_manifest_sha256 is not None and _DIGEST.fullmatch(project_manifest_sha256) is None:
            raise HarnessError("Programmatic project manifest digest is invalid")
        maximum = int(self.config.get("workflow.max_tool_calls"))
        if self._calls_consumed >= maximum:
            raise HarnessError(f"Programmatic action budget exhausted at {maximum} calls")
        if self.deadline is not None:
            self.deadline.check("before a programmatic workspace action")
        self._action_ids.add(action_id)
        self._calls_consumed += 1
        self._pending_action = {
            "action_id": action_id,
            "kind": kind,
            "payload_sha256": payload_sha256,
            "project_manifest_sha256": project_manifest_sha256,
        }
        try:
            self._persist()
        except Exception:
            self._tainted = True
            raise

    def _complete_action(
        self,
        action_id: str,
        kind: str,
        path: str | None,
        before_sha256: object,
        after_sha256: object,
        payload_sha256: str,
    ) -> None:
        pending = self._pending_action
        if (
            not isinstance(pending, dict)
            or pending.get("action_id") != action_id
            or pending.get("kind") != kind
        ):
            self._tainted = True
            raise HarnessError("Programmatic action completion does not match its durable intent")
        self._append_journal(
            action_id, kind, path, before_sha256, after_sha256, payload_sha256,
        )
        self._pending_action = None
        try:
            self._persist()
        except Exception:
            self._tainted = True
            raise

    def _append_journal(
        self,
        action_id: str,
        kind: str,
        path: str | None,
        before_sha256: object,
        after_sha256: object,
        payload_sha256: str,
    ) -> None:
        entry: dict[str, object] = {
            "sequence": len(self._journal) + 1,
            "action_id": action_id,
            "kind": kind,
            "path": path,
            "before_sha256": before_sha256,
            "after_sha256": after_sha256,
            "payload_sha256": payload_sha256,
            "previous_sha256": self._chain_head,
        }
        entry["entry_sha256"] = _digest(entry)
        self._chain_head = str(entry["entry_sha256"])
        self._journal.append(entry)

    def _persist(self, *, expect_absent: bool = False) -> None:
        self._generation += 1
        document = self._document()
        raw = _canonical(document)
        maximum = min(
            50_000_000,
            max(1_000_000, int(self.config.get("execution.max_changed_bytes")) * 2 + 262_144),
        )
        if len(raw) > maximum:
            self._tainted = True
            raise HarnessError(f"Programmatic workspace checkpoint exceeds its {maximum}-byte limit")
        with self._lock.held(timeout_seconds=5):
            if expect_absent:
                if self._checkpoint_path.exists():
                    raise HarnessError(f"Programmatic workspace already exists: {self.session_id}")
            else:
                current = self._read_checkpoint()
                if current["generation"] != self._generation - 1:
                    self._tainted = True
                    raise HarnessError("Programmatic workspace checkpoint changed concurrently")
            safe_path = confined_path(
                self.root,
                Path(".harness") / "checkpoints" / "programmatic" / f"{self.session_id}.json",
                allow_control=True,
            )
            atomic_write(safe_path, raw, 0o600)

    def _document(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_version": _SCHEMA_VERSION,
            "session_id": self.session_id,
            "project_identity": self._project_identity,
            "spec_sha256": self._spec_sha256,
            "generation": self._generation,
            "calls_consumed": self._calls_consumed,
            "baseline": self._baseline,
            "files": self._files,
            "journal": self._journal,
            "chain_head": self._chain_head,
            "pending_action": self._pending_action,
            "tainted": self._tainted,
            "stage": self._stage_record,
        }
        value["checkpoint_hmac_sha256"] = _hmac_digest(self._checkpoint_key, value)
        return value

    def _read_checkpoint(self) -> dict[str, Any]:
        path = confined_path(
            self.root,
            Path(".harness") / "checkpoints" / "programmatic" / f"{self.session_id}.json",
            allow_missing=False,
            allow_control=True,
        )
        limit = min(50_000_000, max(1_000_000, int(self.config.get("execution.max_changed_bytes")) * 2 + 262_144))
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            raw = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
        if len(raw) > limit:
            raise HarnessError("Programmatic workspace checkpoint exceeds its byte limit")
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise HarnessError("Programmatic workspace checkpoint changed while it was read")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HarnessError("Programmatic workspace checkpoint is invalid JSON") from exc
        if not isinstance(value, dict) or set(value) != _CHECKPOINT_FIELDS:
            raise HarnessError("Programmatic workspace checkpoint has unsupported fields")
        supplied = value.get("checkpoint_hmac_sha256")
        material = dict(value)
        material.pop("checkpoint_hmac_sha256", None)
        if (
            not isinstance(supplied, str)
            or not hmac.compare_digest(supplied, _hmac_digest(self._checkpoint_key, material))
        ):
            raise HarnessError("Programmatic workspace checkpoint failed authentication")
        return value

    def _load_document(self, value: dict[str, Any]) -> None:
        if value.get("schema_version") != _SCHEMA_VERSION or value.get("session_id") != self.session_id:
            raise HarnessError("Programmatic workspace checkpoint identity is invalid")
        if value.get("project_identity") != self._project_identity:
            raise HarnessError("Programmatic workspace checkpoint belongs to a different project")
        if value.get("spec_sha256") != self._spec_sha256:
            raise HarnessError("Programmatic workspace restore specification does not match")
        self._validated_stage_record(value.get("stage"))
        generation = value.get("generation")
        calls = value.get("calls_consumed")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise HarnessError("Programmatic workspace checkpoint generation is invalid")
        if isinstance(calls, bool) or not isinstance(calls, int) or calls < 0 or calls > int(self.config.get("workflow.max_tool_calls")):
            raise HarnessError("Programmatic workspace action count is invalid")
        if value.get("tainted") is not False:
            raise HarnessError("Programmatic workspace checkpoint is tainted")
        pending_action = value.get("pending_action")
        if pending_action is not None:
            if not self._valid_pending_action(pending_action):
                raise HarnessError("Programmatic workspace pending action is invalid")
            raise HarnessError(
                "Programmatic workspace has an uncertain action after interruption; it will not be replayed"
            )
        baseline = value.get("baseline")
        files = value.get("files")
        journal = value.get("journal")
        if not isinstance(baseline, dict) or not isinstance(files, dict) or not isinstance(journal, list):
            raise HarnessError("Programmatic workspace checkpoint collections are invalid")
        self._validate_journal(journal, value.get("chain_head"))
        approved_keys = {portable_relative_path_key(path): path for path in self._approved_files}
        allowed_keys = approved_keys | {portable_relative_path_key(path): path for path in self._support_files}
        if set(baseline) != set(allowed_keys.values()) or any(
            not self._valid_source_identity(item) for item in baseline.values()
        ):
            raise HarnessError("Programmatic workspace baseline set is invalid")
        if any(path not in approved_keys.values() or not isinstance(snapshot, dict) for path, snapshot in files.items()):
            raise HarnessError("Programmatic workspace changed-file set is invalid")
        for path, snapshot in files.items():
            if set(snapshot) != {"content", "sha256", "reason"} or not isinstance(snapshot.get("reason"), str):
                raise HarnessError(f"Programmatic workspace file snapshot is invalid: {path}")
            content = self._decode_content(snapshot.get("content"), path)
            expected = None if content is None else sha256_bytes(content)
            if snapshot.get("sha256") != expected:
                raise HarnessError(f"Programmatic workspace file snapshot hash is invalid: {path}")
        self._generation = generation
        self._calls_consumed = calls
        self._baseline = {str(path): dict(identity) for path, identity in baseline.items()}
        self._files = {str(path): dict(snapshot) for path, snapshot in files.items()}
        self._journal = [dict(entry) for entry in journal]
        self._chain_head = str(value["chain_head"])
        self._pending_action = None
        self._stage_record = dict(value["stage"])
        self._action_ids = {str(entry["action_id"]) for entry in journal}

    @staticmethod
    def _valid_pending_action(value: object) -> bool:
        if not isinstance(value, dict) or set(value) != {
            "action_id", "kind", "payload_sha256", "project_manifest_sha256",
        }:
            return False
        project_digest = value.get("project_manifest_sha256")
        return bool(
            isinstance(value.get("action_id"), str)
            and value["action_id"]
            and value.get("kind") in {"replace_file", "apply_patch", "delete_file", "run_verification"}
            and isinstance(value.get("payload_sha256"), str)
            and _DIGEST.fullmatch(str(value["payload_sha256"])) is not None
            and (
                project_digest is None
                or isinstance(project_digest, str)
                and _DIGEST.fullmatch(project_digest) is not None
            )
        )

    @staticmethod
    def _valid_source_identity(value: object) -> bool:
        fields = {"kind", "sha256", "byte_count", "mode", "device", "inode", "mtime_ns"}
        if not isinstance(value, dict) or set(value) != fields:
            return False
        if value.get("kind") == "missing":
            return (
                value.get("sha256") is None
                and value.get("byte_count") == 0
                and all(value.get(name) is None for name in ("mode", "device", "inode", "mtime_ns"))
            )
        return bool(
            value.get("kind") == "regular"
            and isinstance(value.get("sha256"), str)
            and _DIGEST.fullmatch(str(value["sha256"])) is not None
            and all(
                isinstance(value.get(name), int) and not isinstance(value.get(name), bool) and int(value[name]) >= 0
                for name in ("byte_count", "mode", "device", "inode", "mtime_ns")
            )
        )

    @staticmethod
    def _validate_journal(journal: list[object], expected_head: object) -> None:
        head = "0" * 64
        fields = {
            "sequence", "action_id", "kind", "path", "before_sha256", "after_sha256",
            "payload_sha256", "previous_sha256", "entry_sha256",
        }
        ids: set[str] = set()
        for sequence, raw in enumerate(journal, 1):
            if not isinstance(raw, dict) or set(raw) != fields:
                raise HarnessError("Programmatic workspace journal entry is invalid")
            if raw.get("sequence") != sequence or raw.get("previous_sha256") != head:
                raise HarnessError("Programmatic workspace journal chain is invalid")
            action_id = raw.get("action_id")
            if not isinstance(action_id, str) or not action_id or action_id in ids:
                raise HarnessError("Programmatic workspace journal action ID is invalid")
            ids.add(action_id)
            if raw.get("kind") not in {"replace_file", "apply_patch", "delete_file", "run_verification"}:
                raise HarnessError("Programmatic workspace journal action kind is invalid")
            for name in ("payload_sha256", "previous_sha256"):
                if not isinstance(raw.get(name), str) or _DIGEST.fullmatch(str(raw[name])) is None:
                    raise HarnessError("Programmatic workspace journal digest is invalid")
            supplied = raw.get("entry_sha256")
            material = dict(raw)
            material.pop("entry_sha256", None)
            if not isinstance(supplied, str) or supplied != _digest(material):
                raise HarnessError("Programmatic workspace journal entry hash is invalid")
            head = supplied
        if expected_head != head:
            raise HarnessError("Programmatic workspace journal head is invalid")

    def _spec_digest(self) -> str:
        approved = sorted((portable_relative_path_key(path), path) for path in self._approved_files)
        support = sorted((portable_relative_path_key(path), path) for path in self._support_files)
        actions = [
            {
                "name": action.name,
                "argv": list(action.argv),
                "cwd": action.cwd,
                "timeout_seconds": action.timeout_seconds,
            }
            for action in self._verification_actions
        ]
        return _digest(
            {
                "approved": approved,
                "support": support,
                "verification_actions": actions,
                "generated_output_ignores": self._generated_output_ignores,
                "config_sha256": _digest(self.config.data),
                "project_identity": self._project_identity,
            }
        )

    @staticmethod
    def _encode_content(content: bytes | None) -> dict[str, object] | None:
        if content is None:
            return None
        return {"encoding": "utf8-hex", "byte_count": len(content), "payload": content.hex()}

    @staticmethod
    def _decode_content(value: object, path: str) -> bytes | None:
        if value is None:
            return None
        if not isinstance(value, dict) or set(value) != {"encoding", "byte_count", "payload"}:
            raise HarnessError(f"Programmatic workspace content envelope is invalid: {path}")
        if value.get("encoding") != "utf8-hex" or isinstance(value.get("byte_count"), bool) or not isinstance(value.get("byte_count"), int):
            raise HarnessError(f"Programmatic workspace content encoding is invalid: {path}")
        payload = value.get("payload")
        if not isinstance(payload, str):
            raise HarnessError(f"Programmatic workspace content payload is invalid: {path}")
        try:
            content = bytes.fromhex(payload)
            content.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise HarnessError(f"Programmatic workspace content payload is invalid: {path}") from exc
        if len(content) != value["byte_count"]:
            raise HarnessError(f"Programmatic workspace content byte count is invalid: {path}")
        return content

    def _require_persistable(self, value: str, label: str) -> None:
        if not isinstance(value, str):
            raise HarnessError(f"Programmatic {label} must be text")
        if self._redactor.text(value) != value:
            raise HarnessError(f"Programmatic {label} contains credential-like material")

    def _fail_closed(self) -> None:
        self._tainted = True
        try:
            self._persist()
        except Exception:
            pass

    def _stage(self) -> StagedCodingWorkspace:
        if self._workspace is None:
            raise HarnessError("Programmatic workspace is closed")
        return self._workspace

    def _ensure_usable(self, operation: str) -> None:
        if self._closed:
            raise HarnessError(f"Programmatic workspace is closed; cannot {operation}")
        if self._tainted:
            raise HarnessError(f"Programmatic workspace is tainted; cannot {operation}")
