"""Durable, run-scoped coordination for visual automations.

The visual editor, desktop-agent endpoint, timer process, and panel all reach
the same SQLite file for a project.  This module deliberately owns only run
admission/control/projection; the pipeline engine remains in ``pipelines.py``.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import threading
import time
from typing import Any, Iterator, NamedTuple
import uuid

from .config import LoadedConfig
from .models import HarnessError
from .redaction import CredentialRedactor


ACTIVE_STATES = ("accepted", "running", "waiting", "stopping")
TERMINAL_STATES = (
    "passed", "warning", "failed", "incomplete", "cancelled", "timed_out", "interrupted"
)
DATABASE_NAME = "runs.sqlite3"
AUTHORITY_DESCRIPTOR = Path(".harness") / "project-authority.json"
AUTHORITY_REGISTRY = "authority-registry.sqlite3"
INTEGRITY_KEY = "integrity.key"
INTEGRITY_ANCHOR = "integrity.anchor"
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@+\-]{0,199}\Z")
_WINDOWS_FILESYSTEM_KEY_PREFIX = "win-file-id-v1:"
_WINDOWS_FILESYSTEM_KEY = re.compile(
    rf"{re.escape(_WINDOWS_FILESYSTEM_KEY_PREFIX)}([0-9a-f]{{8}}):([0-9a-f]{{16}})\Z"
)
_LEGACY_FILESYSTEM_KEY = re.compile(r"(-?\d+):(\d+)\Z")


class PipelineRunConflict(HarnessError):
    """A second writer or stale control attempted to affect a pipeline run."""


class PipelineRunNotFound(HarnessError):
    """The requested immutable run identity does not exist in this project."""


def canonical_definition(definition: dict[str, Any]) -> str:
    return json.dumps(definition, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def definition_digest(definition: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_definition(definition).encode("utf-8")).hexdigest()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _process_token(pid: int) -> str:
    """Best available process birth identity; empty means verification is unavailable."""

    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not process:
                return ""
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            try:
                if not ctypes.windll.kernel32.GetProcessTimes(
                    process, ctypes.byref(creation), ctypes.byref(exit_time),
                    ctypes.byref(kernel), ctypes.byref(user),
                ):
                    return ""
                return f"{creation.dwHighDateTime}:{creation.dwLowDateTime}"
            finally:
                ctypes.windll.kernel32.CloseHandle(process)
        except (AttributeError, OSError):
            return ""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        return fields[21]
    except (OSError, IndexError):
        return ""


def _owner_is_alive(pid: int, token: str, thread_id: int = 0) -> bool:
    if pid == os.getpid() and thread_id:
        living = {int(one.native_id or 0) for one in threading.enumerate()}
        if thread_id not in living:
            return False
    if os.name == "nt":
        try:
            import ctypes

            process = ctypes.windll.kernel32.OpenProcess(0x100000 | 0x1000, False, pid)
            if not process:
                # Access denied proves a process owns the PID but prevents
                # birth-token verification, so fail closed. Invalid/missing
                # PIDs are dead and may be recovered.
                return int(ctypes.get_last_error()) == 5
            try:
                if int(ctypes.windll.kernel32.WaitForSingleObject(process, 0)) != 258:
                    return False
                current = _process_token(pid)
                return not token or not current or current == token
            finally:
                ctypes.windll.kernel32.CloseHandle(process)
        except (AttributeError, OSError):
            return True
    current = _process_token(pid)
    if current:
        return not token or current == token
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, PermissionError):
        return True
    return True


def _runtime_base() -> Path:
    override = os.environ.get("OUR_HARNESS_PIPELINE_RUN_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local).expanduser() if local else Path.home() / "AppData" / "Local"
        return (base / "OurHarness" / "pipeline-runs").resolve()
    state = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(state).expanduser() if state else Path.home() / ".local" / "state"
    return (base / "our-harness" / "pipeline-runs").resolve()


class _FilesystemIdentity(NamedTuple):
    key: str
    legacy_inode: int


def _windows_file_id(root: Path) -> tuple[int, int]:
    """Return the native, unsigned Windows volume and directory file IDs."""

    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = [wintypes.HANDLE, ctypes.POINTER(ByHandleFileInformation)]
    get_information.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    native_path = str(root)
    if not native_path.startswith("\\\\?\\"):
        if native_path.startswith("\\\\"):
            native_path = "\\\\?\\UNC\\" + native_path[2:]
        else:
            native_path = "\\\\?\\" + native_path
    handle = create_file(
        native_path,
        0x0080,  # FILE_READ_ATTRIBUTES
        0x0001 | 0x0002 | 0x0004,  # FILE_SHARE_READ | WRITE | DELETE
        None,
        3,  # OPEN_EXISTING
        0x02000000 | 0x00200000,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        None,
    )
    if handle == wintypes.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), "CreateFileW could not open the project root")
    information = ByHandleFileInformation()
    try:
        if not get_information(handle, ctypes.byref(information)):
            raise OSError(
                ctypes.get_last_error(),
                "GetFileInformationByHandle could not identify the project root",
            )
    finally:
        close_handle(handle)
    file_id = (int(information.nFileIndexHigh) << 32) | int(information.nFileIndexLow)
    return int(information.dwVolumeSerialNumber), file_id


def _filesystem_identity(root: Path) -> _FilesystemIdentity:
    metadata = root.stat()
    inode = int(metadata.st_ino)
    if inode <= 0:
        raise PipelineRunConflict(
            "This filesystem does not expose a stable project identity; automation runs are paused."
        )
    if os.name == "nt":
        try:
            volume_serial, file_id = _windows_file_id(root)
        except (AttributeError, OSError) as exc:
            raise PipelineRunConflict(
                "Windows could not expose a stable project identity; automation runs are paused."
            ) from exc
        if file_id <= 0:
            raise PipelineRunConflict(
                "Windows did not expose a stable project identity; automation runs are paused."
            )
        return _FilesystemIdentity(
            f"{_WINDOWS_FILESYSTEM_KEY_PREFIX}{volume_serial:08x}:{file_id:016x}",
            inode,
        )
    return _FilesystemIdentity(f"{int(metadata.st_dev)}:{inode}", inode)


def _filesystem_key(root: Path) -> str:
    return _filesystem_identity(root).key


def _reject_reparse_or_link(path: Path) -> None:
    if path.is_symlink():
        raise PipelineRunConflict("A linked project root cannot own automation authority.")
    attributes = int(getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0))
    reparse = int(getattr(__import__("stat"), "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    if attributes & reparse:
        raise PipelineRunConflict("A reparse-point project root cannot own automation authority.")


def _read_descriptor(where: Path) -> str:
    if not where.exists():
        return ""
    if where.is_symlink() or not where.is_file():
        raise PipelineRunConflict("The project authority descriptor is not a regular file.")
    try:
        raw = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineRunConflict("The project authority descriptor could not be verified.") from exc
    authority_id = str(raw.get("project_authority_id") or "") if isinstance(raw, dict) else ""
    try:
        parsed = uuid.UUID(authority_id)
    except ValueError as exc:
        raise PipelineRunConflict("The project authority descriptor is invalid.") from exc
    return parsed.hex


def _write_descriptor(where: Path, authority_id: str) -> None:
    where.parent.mkdir(parents=True, exist_ok=True)
    temporary = where.with_name(f".{where.name}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps({
        "schema_version": 1,
        "project_authority_id": authority_id,
    }, indent=2) + "\n"
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Hard-link publication is atomic and cannot overwrite a
            # descriptor that appeared after registry validation.
            os.link(temporary, where)
        except FileExistsError:
            if _read_descriptor(where) != authority_id:
                raise PipelineRunConflict(
                    "The project authority descriptor changed during registration."
                )
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _authority_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS project_authorities (
               project_authority_id TEXT PRIMARY KEY,
               filesystem_key TEXT NOT NULL UNIQUE,
               canonical_path TEXT NOT NULL UNIQUE,
               revision INTEGER NOT NULL,
               updated_at_ms INTEGER NOT NULL
           )"""
    )


def _authority_rows(
    connection: sqlite3.Connection, *, described_id: str,
    filesystem_key: str, canonical_path: str,
) -> tuple[sqlite3.Row | None, sqlite3.Row | None, sqlite3.Row | None]:
    by_identity = connection.execute(
        "SELECT * FROM project_authorities WHERE filesystem_key=?", (filesystem_key,)
    ).fetchone()
    by_path = connection.execute(
        "SELECT * FROM project_authorities WHERE canonical_path=?", (canonical_path,)
    ).fetchone()
    by_descriptor = connection.execute(
        "SELECT * FROM project_authorities WHERE project_authority_id=?", (described_id,)
    ).fetchone() if described_id else None
    return by_identity, by_path, by_descriptor


def _migrate_legacy_windows_filesystem_key(
    connection: sqlite3.Connection, *, identity: _FilesystemIdentity,
    described_id: str, canonical_path: str,
    by_identity: sqlite3.Row | None, by_path: sqlite3.Row | None,
    by_descriptor: sqlite3.Row | None,
) -> bool:
    """Upgrade only a legacy row proven to describe this exact directory.

    Old keys used CPython's integer representation of ``st_dev``.  Windows
    runtimes have exposed both the raw volume serial and a wider value whose
    low 32 bits are that same serial.  Migration therefore requires both that
    native volume serial and the native file ID/inode to agree.  A copied
    descriptor cannot pass this test, while the same directory can still be
    recognized after a runtime change or a rename.
    """

    native_match = _WINDOWS_FILESYSTEM_KEY.fullmatch(identity.key)
    if native_match is None:
        return False
    if not described_id or by_identity is not None:
        return False
    if by_descriptor is None:
        return False
    if str(by_descriptor["project_authority_id"]) != described_id:
        return False
    if by_path is not None and str(by_path["project_authority_id"]) != described_id:
        return False
    legacy_key = str(by_descriptor["filesystem_key"])
    legacy_match = _LEGACY_FILESYSTEM_KEY.fullmatch(legacy_key)
    if legacy_match is None:
        return False
    legacy_device = int(legacy_match.group(1))
    legacy_inode = int(legacy_match.group(2))
    native_volume = int(native_match.group(1), 16)
    native_file_id = int(native_match.group(2), 16)
    if legacy_device & 0xFFFFFFFF != native_volume:
        return False
    if legacy_inode != identity.legacy_inode or legacy_inode != native_file_id:
        return False
    changed = connection.execute(
        "UPDATE project_authorities "
        "SET filesystem_key=?,revision=revision+1,updated_at_ms=? "
        "WHERE project_authority_id=? AND filesystem_key=? "
        "AND canonical_path=? AND revision=?",
        (
            identity.key, _now_ms(), described_id, legacy_key,
            str(by_descriptor["canonical_path"]), int(by_descriptor["revision"]),
        ),
    ).rowcount
    return changed == 1


def _authority_fingerprint(
    authority_id: str, filesystem_key: str, canonical_path: str, reason_code: str,
) -> str:
    return hashlib.sha256(
        f"{authority_id}\0{filesystem_key}\0{canonical_path}\0{reason_code}".encode("utf-8")
    ).hexdigest()


def _authority_state_from_rows(
    *, described_id: str, filesystem_key: str, canonical_path: str,
    by_identity: sqlite3.Row | None, by_path: sqlite3.Row | None,
    by_descriptor: sqlite3.Row | None,
) -> dict[str, Any]:
    """Project execution status without registering or relocating anything."""

    def paused(reason: str, code: str, *, repairable: bool = False) -> dict[str, Any]:
        return {
            "can_run": False,
            "reason": reason,
            "reason_code": code,
            "repairable": repairable,
            "fingerprint": _authority_fingerprint(
                described_id, filesystem_key, canonical_path, code
            ),
        }

    if not described_id:
        if by_identity is not None or by_path is not None:
            return paused(
                "The project authority registry and local descriptor disagree.",
                "ambiguous_registry",
            )
        return {
            "can_run": True, "reason": "", "reason_code": "new_project",
            "repairable": False, "fingerprint": "",
        }
    if by_descriptor is None:
        if by_identity is not None or by_path is not None:
            return paused(
                "The project authority descriptor conflicts with an existing local registration.",
                "ambiguous_registry",
            )
        return paused(
            "The project authority descriptor is not registered for this user.",
            "unregistered",
            repairable=True,
        )
    if str(by_descriptor["filesystem_key"]) != filesystem_key:
        if by_identity is not None or by_path is not None:
            return paused(
                "The copied project also conflicts with an existing local registration.",
                "ambiguous_registry",
            )
        return paused(
            "The project authority descriptor was copied or substituted; automation is paused.",
            "copied_or_substituted",
            repairable=True,
        )
    if by_path is not None and str(by_path["project_authority_id"]) != described_id:
        return paused(
            "This path is already bound to another project authority.",
            "ambiguous_registry",
        )
    old_path = Path(str(by_descriptor["canonical_path"]))
    if os.path.normcase(str(old_path)) != canonical_path and old_path.exists():
        return paused(
            "The registered project location still exists; a copied alias cannot be opened.",
            "live_alias",
        )
    return {
        "can_run": True, "reason": "", "reason_code": "registered",
        "repairable": False, "fingerprint": "",
    }


def inspect_project_authority(project_root: Path) -> dict[str, Any]:
    """Read status without registering; verified legacy identities may be upgraded."""

    supplied = project_root.expanduser().absolute()
    _reject_reparse_or_link(supplied)
    root = supplied.resolve(strict=True)
    _reject_reparse_or_link(root)
    base = _runtime_base()
    if base == root or root in base.parents:
        raise PipelineRunConflict("Pipeline runtime storage must be outside the project tree.")
    described_id = _read_descriptor(root / AUTHORITY_DESCRIPTOR)
    identity = _filesystem_identity(root)
    filesystem_key = identity.key
    canonical_path = os.path.normcase(str(root))
    registry = base / AUTHORITY_REGISTRY
    if not registry.exists():
        return _authority_state_from_rows(
            described_id=described_id, filesystem_key=filesystem_key,
            canonical_path=canonical_path, by_identity=None, by_path=None,
            by_descriptor=None,
        )
    if registry.is_symlink() or not registry.is_file():
        raise PipelineRunConflict("The project authority registry is not a regular file.")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            f"file:{registry.as_posix()}?mode=rw", uri=True, timeout=10.0
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA synchronous=FULL")
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='project_authorities'"
        ).fetchone()
        if table is None:
            by_identity = by_path = by_descriptor = None
        else:
            by_identity, by_path, by_descriptor = _authority_rows(
                connection, described_id=described_id,
                filesystem_key=filesystem_key, canonical_path=canonical_path,
            )
            migrated = _migrate_legacy_windows_filesystem_key(
                connection, identity=identity, described_id=described_id,
                canonical_path=canonical_path, by_identity=by_identity,
                by_path=by_path, by_descriptor=by_descriptor,
            )
            migration_attempted = connection.in_transaction
            if migrated:
                connection.commit()
            elif migration_attempted:
                connection.rollback()
            if migrated or migration_attempted:
                # Another inspector may have won the conditional migration;
                # report its committed result instead of a transient conflict.
                by_identity, by_path, by_descriptor = _authority_rows(
                    connection, described_id=described_id,
                    filesystem_key=filesystem_key, canonical_path=canonical_path,
                )
    except sqlite3.Error as exc:
        raise PipelineRunConflict("The project authority registry could not be verified.") from exc
    finally:
        if connection is not None:
            connection.close()
    return _authority_state_from_rows(
        described_id=described_id, filesystem_key=filesystem_key,
        canonical_path=canonical_path, by_identity=by_identity, by_path=by_path,
        by_descriptor=by_descriptor,
    )


def _descriptor_is_explicitly_ignored(root: Path) -> bool:
    ignore = root / ".harness" / ".gitignore"
    if ignore.exists():
        _reject_reparse_or_link(ignore)
    if ignore.is_symlink() or not ignore.is_file():
        return False
    try:
        lines = ignore.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    ignored = False
    for raw in lines:
        line = raw.strip()
        if line in {"project-authority.json", "/project-authority.json"}:
            ignored = True
        elif line in {"!project-authority.json", "!/project-authority.json"}:
            ignored = False
    return ignored


def _reject_reparse_chain(path: Path) -> None:
    current = path
    while True:
        if current.exists():
            _reject_reparse_or_link(current)
        if current.parent == current:
            return
        current = current.parent


def _descriptor_is_git_tracked(root: Path) -> bool:
    has_git_marker = any(
        (candidate / ".git").exists() or (candidate / ".git").is_symlink()
        for candidate in (root, *root.parents)
    )
    try:
        checked = subprocess.run(
            [
                "git", "-C", str(root), "ls-files", "--error-unmatch", "--",
                ".harness/project-authority.json",
            ],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=3, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PipelineRunConflict(
            "Git tracking could not be checked, so the authority descriptor was not rotated."
        ) from exc
    if checked.returncode == 0:
        return True
    if checked.returncode == 1:
        return False
    error = checked.stderr.decode("utf-8", errors="replace").lower()
    if (checked.returncode == 128 and not has_git_marker
            and "not a git repository" in error):
        return False
    raise PipelineRunConflict(
        "Git tracking could not be checked, so the authority descriptor was not rotated."
    )


def _commit_authority_rotation(connection: sqlite3.Connection) -> None:
    connection.commit()


def repair_project_authority(project_root: Path, expected_fingerprint: str) -> str:
    """Explicitly make a copied/unregistered folder a new local authority."""

    expected = str(expected_fingerprint or "").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise PipelineRunConflict("A current project-authority confirmation is required.")
    supplied = project_root.expanduser().absolute()
    _reject_reparse_chain(supplied)
    root = supplied.resolve(strict=True)
    _reject_reparse_or_link(root)
    harness = root / ".harness"
    descriptor = root / AUTHORITY_DESCRIPTOR
    _reject_reparse_or_link(harness)
    if not harness.is_dir():
        raise PipelineRunConflict("The local .harness folder could not be verified.")
    _reject_reparse_or_link(descriptor)
    if not _descriptor_is_explicitly_ignored(root):
        raise PipelineRunConflict(
            "The authority descriptor is not explicitly ignored by .harness/.gitignore."
        )
    if _descriptor_is_git_tracked(root):
        raise PipelineRunConflict(
            "The authority descriptor is tracked by Git and cannot be rotated as local-only state."
        )
    described_id = _read_descriptor(descriptor)
    prior_descriptor = descriptor.read_bytes()
    before = descriptor.stat(follow_symlinks=False)
    before_signature = (
        int(before.st_dev), int(before.st_ino), int(before.st_size),
        int(before.st_mtime_ns), described_id,
    )
    filesystem_key = _filesystem_key(root)
    canonical_path = os.path.normcase(str(root))
    base = _runtime_base()
    if base == root or root in base.parents:
        raise PipelineRunConflict("Pipeline runtime storage must be outside the project tree.")
    base.mkdir(parents=True, exist_ok=True)
    registry = base / AUTHORITY_REGISTRY
    connection = sqlite3.connect(registry, timeout=10.0)
    connection.row_factory = sqlite3.Row
    temporary = descriptor.with_name(f".{descriptor.name}.{uuid.uuid4().hex}.tmp")
    replaced = False
    authority_id = ""
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=10000")
        _authority_schema(connection)
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        by_identity = connection.execute(
            "SELECT * FROM project_authorities WHERE filesystem_key=?", (filesystem_key,)
        ).fetchone()
        by_path = connection.execute(
            "SELECT * FROM project_authorities WHERE canonical_path=?", (canonical_path,)
        ).fetchone()
        by_descriptor = connection.execute(
            "SELECT * FROM project_authorities WHERE project_authority_id=?", (described_id,)
        ).fetchone()
        state = _authority_state_from_rows(
            described_id=described_id, filesystem_key=filesystem_key,
            canonical_path=canonical_path, by_identity=by_identity,
            by_path=by_path, by_descriptor=by_descriptor,
        )
        if state["fingerprint"] != expected:
            raise PipelineRunConflict(
                "Project authority changed after confirmation. Refresh and review it again."
            )
        if not state["repairable"] or state["reason_code"] not in {
            "copied_or_substituted", "unregistered",
        }:
            raise PipelineRunConflict("This project authority must not be rotated.")
        if by_identity is not None or by_path is not None:
            raise PipelineRunConflict("The project has an ambiguous local authority registration.")
        now = descriptor.stat(follow_symlinks=False)
        now_signature = (
            int(now.st_dev), int(now.st_ino), int(now.st_size),
            int(now.st_mtime_ns), _read_descriptor(descriptor),
        )
        if now_signature != before_signature:
            raise PipelineRunConflict(
                "The project authority descriptor changed during confirmation."
            )
        authority_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO project_authorities VALUES (?,?,?,?,?)",
            (authority_id, filesystem_key, canonical_path, 1, _now_ms()),
        )
        payload = json.dumps({
            "schema_version": 1, "project_authority_id": authority_id,
        }, indent=2) + "\n"
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, descriptor)
        replaced = True
        _commit_authority_rotation(connection)
        return authority_id
    except Exception:
        registry_may_have_committed = replaced and not connection.in_transaction
        connection.rollback()
        try:
            if registry_may_have_committed and authority_id:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "DELETE FROM project_authorities WHERE project_authority_id=? "
                    "AND filesystem_key=? AND canonical_path=?",
                    (authority_id, filesystem_key, canonical_path),
                )
                connection.commit()
        finally:
            if replaced:
                restoration = descriptor.with_name(
                    f".{descriptor.name}.{uuid.uuid4().hex}.restore"
                )
                try:
                    with restoration.open("xb") as stream:
                        stream.write(prior_descriptor)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(restoration, descriptor)
                finally:
                    restoration.unlink(missing_ok=True)
        raise
    finally:
        connection.close()
        temporary.unlink(missing_ok=True)


def project_identity(project_root: Path) -> str:
    """Resolve one stable registered authority, including same-volume renames.

    The descriptor is only a lookup key.  The trusted user registry remains
    authoritative and rejects copied/substituted descriptors and live aliases.
    """

    root = project_root.resolve(strict=True)
    _reject_reparse_or_link(root)
    base = _runtime_base()
    if base == root or root in base.parents:
        raise PipelineRunConflict("Pipeline runtime storage must be outside the project tree.")
    base.mkdir(parents=True, exist_ok=True)
    try:
        base.chmod(0o700)
    except OSError:
        pass
    registry = base / AUTHORITY_REGISTRY
    descriptor = root / AUTHORITY_DESCRIPTOR
    described_id = _read_descriptor(descriptor)
    identity = _filesystem_identity(root)
    filesystem_key = identity.key
    canonical_path = os.path.normcase(str(root))
    connection = sqlite3.connect(registry, timeout=10.0)
    connection.row_factory = sqlite3.Row
    authority_id = ""
    write_descriptor = False
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=10000")
        _authority_schema(connection)
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        by_identity, by_path, by_descriptor = _authority_rows(
            connection, described_id=described_id,
            filesystem_key=filesystem_key, canonical_path=canonical_path,
        )
        if _migrate_legacy_windows_filesystem_key(
            connection, identity=identity, described_id=described_id,
            canonical_path=canonical_path, by_identity=by_identity,
            by_path=by_path, by_descriptor=by_descriptor,
        ):
            by_identity, by_path, by_descriptor = _authority_rows(
                connection, described_id=described_id,
                filesystem_key=filesystem_key, canonical_path=canonical_path,
            )
        if described_id:
            if by_descriptor is None:
                raise PipelineRunConflict(
                    "The project authority descriptor is not registered for this user."
                )
            if by_descriptor["filesystem_key"] != filesystem_key:
                raise PipelineRunConflict(
                    "The project authority descriptor was copied or substituted; automation is paused."
                )
            if by_path is not None and by_path["project_authority_id"] != described_id:
                raise PipelineRunConflict("This path is already bound to another project authority.")
            old_path = Path(str(by_descriptor["canonical_path"]))
            if os.path.normcase(str(old_path)) != canonical_path:
                if old_path.exists():
                    raise PipelineRunConflict(
                        "The registered project location still exists; a copied alias cannot be opened."
                    )
                changed = connection.execute(
                    "UPDATE project_authorities SET canonical_path=?,revision=revision+1,updated_at_ms=? "
                    "WHERE project_authority_id=? AND revision=? AND canonical_path=?",
                    (canonical_path, _now_ms(), described_id, by_descriptor["revision"],
                     by_descriptor["canonical_path"]),
                ).rowcount
                if changed != 1:
                    raise PipelineRunConflict("Project relocation raced another authority update.")
            authority_id = described_id
        elif by_identity is not None:
            if by_path is not None and by_path["project_authority_id"] != by_identity["project_authority_id"]:
                raise PipelineRunConflict("The project identity and location disagree.")
            old_path = Path(str(by_identity["canonical_path"]))
            if os.path.normcase(str(old_path)) != canonical_path:
                if old_path.exists():
                    raise PipelineRunConflict("A live project alias already owns this identity.")
                connection.execute(
                    "UPDATE project_authorities SET canonical_path=?,revision=revision+1,updated_at_ms=? "
                    "WHERE project_authority_id=? AND revision=?",
                    (canonical_path, _now_ms(), by_identity["project_authority_id"],
                     by_identity["revision"]),
                )
            authority_id = str(by_identity["project_authority_id"])
            write_descriptor = True
        elif by_path is not None:
            raise PipelineRunConflict("The registered project filesystem identity changed unexpectedly.")
        else:
            authority_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO project_authorities VALUES (?,?,?,?,?)",
                (authority_id, filesystem_key, canonical_path, 1, _now_ms()),
            )
            write_descriptor = True
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    if write_descriptor:
        _write_descriptor(descriptor, authority_id)
    return authority_id


def runtime_database_path(project_root: Path) -> Path:
    """Return an ACL/user-scoped path that is never inside the project tree."""

    base = _runtime_base()
    path = (base / project_identity(project_root) / DATABASE_NAME).resolve()
    root = project_root.resolve()
    if path == root or root in path.parents:
        raise PipelineRunConflict("Pipeline runtime storage must be outside the project tree.")
    return path


class PipelineRunStore:
    """One durable coordinator per canonical project root."""

    def __init__(self, config: LoadedConfig):
        self.config = config
        self.redactor = CredentialRedactor(config)
        self.path = runtime_database_path(config.project_root)
        self.authority_id = self.path.parent.name
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        self._integrity_key = self._load_integrity_key()
        self._prepare()
        self._recover_orphans()

    def _load_integrity_key(self) -> bytes:
        where = self.path.parent / INTEGRITY_KEY
        if where.exists():
            if where.is_symlink() or not where.is_file():
                raise PipelineRunConflict("The automation integrity key is not a regular file.")
            key = where.read_bytes()
            if len(key) != 32:
                raise PipelineRunConflict("The automation integrity key is invalid.")
            return key
        key = secrets.token_bytes(32)
        try:
            with where.open("xb") as stream:
                stream.write(key)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                where.chmod(0o600)
            except OSError:
                pass
            return key
        except FileExistsError:
            loaded = where.read_bytes()
            if len(loaded) != 32:
                raise PipelineRunConflict("The automation integrity key is invalid.")
            return loaded

    def _mac(self, kind: str, value: Any) -> str:
        encoded = json.dumps(
            {"kind": kind, "value": value}, ensure_ascii=False,
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(self._integrity_key, encoded, hashlib.sha256).hexdigest()

    def _anchor_value(self) -> str:
        return self._mac("anchor", {"authority": self.path.parent.name, "version": 1})

    def _anchor_exists_and_is_valid(self) -> bool:
        where = self.path.parent / INTEGRITY_ANCHOR
        if not where.exists():
            return False
        if where.is_symlink() or not where.is_file():
            raise PipelineRunConflict("The automation integrity anchor is not a regular file.")
        try:
            held = where.read_text(encoding="ascii").strip()
        except OSError as exc:
            raise PipelineRunConflict("The automation integrity anchor cannot be read.") from exc
        if not hmac.compare_digest(held, self._anchor_value()):
            raise PipelineRunConflict("The automation integrity anchor was changed.")
        return True

    def _write_anchor(self) -> None:
        where = self.path.parent / INTEGRITY_ANCHOR
        try:
            with where.open("x", encoding="ascii") as stream:
                stream.write(self._anchor_value() + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                where.chmod(0o600)
            except OSError:
                pass
        except FileExistsError:
            if not self._anchor_exists_and_is_valid():
                raise PipelineRunConflict("The automation integrity anchor could not be created.")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _prepare(self) -> None:
        with self._reader() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS pipeline_runs (
                    run_id TEXT PRIMARY KEY,
                    request_id TEXT UNIQUE,
                    request_digest TEXT NOT NULL,
                    definition_digest TEXT NOT NULL,
                    definition_json TEXT NOT NULL,
                    name TEXT NOT NULL,
                    source TEXT NOT NULL,
                    state TEXT NOT NULL,
                    stop_requested INTEGER NOT NULL DEFAULT 0,
                    waiting_step TEXT NOT NULL DEFAULT '',
                    result_json TEXT,
                    owner_pid INTEGER NOT NULL,
                    owner_token TEXT NOT NULL DEFAULT '',
                    owner_thread_id INTEGER NOT NULL DEFAULT 0,
                    attempt_id TEXT NOT NULL DEFAULT '',
                    event_count INTEGER NOT NULL DEFAULT 0,
                    event_head TEXT NOT NULL DEFAULT '',
                    decision_count INTEGER NOT NULL DEFAULT 0,
                    decision_head TEXT NOT NULL DEFAULT '',
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    integrity_mac TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS pipeline_runs_state
                    ON pipeline_runs(state, updated_at_ms);
                CREATE TABLE IF NOT EXISTS pipeline_run_events (
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    node TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    previous_mac TEXT NOT NULL DEFAULT '',
                    integrity_mac TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (run_id, sequence),
                    FOREIGN KEY (run_id) REFERENCES pipeline_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS pipeline_run_decisions (
                    run_id TEXT NOT NULL,
                    step TEXT NOT NULL,
                    carry_on INTEGER NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    previous_mac TEXT NOT NULL DEFAULT '',
                    integrity_mac TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (run_id, step),
                    FOREIGN KEY (run_id) REFERENCES pipeline_runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS pipeline_integrity_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(pipeline_runs)")}
            if "owner_token" not in columns:
                connection.execute(
                    "ALTER TABLE pipeline_runs ADD COLUMN owner_token TEXT NOT NULL DEFAULT ''"
                )
            if "attempt_id" not in columns:
                connection.execute(
                    "ALTER TABLE pipeline_runs ADD COLUMN attempt_id TEXT NOT NULL DEFAULT ''"
                )
            for name, declaration in (
                ("owner_thread_id", "INTEGER NOT NULL DEFAULT 0"),
                ("event_count", "INTEGER NOT NULL DEFAULT 0"),
                ("event_head", "TEXT NOT NULL DEFAULT ''"),
                ("decision_count", "INTEGER NOT NULL DEFAULT 0"),
                ("decision_head", "TEXT NOT NULL DEFAULT ''"),
                ("integrity_mac", "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE pipeline_runs ADD COLUMN {name} {declaration}")
            event_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(pipeline_run_events)")
            }
            for name in ("previous_mac", "integrity_mac"):
                if name not in event_columns:
                    connection.execute(
                        f"ALTER TABLE pipeline_run_events ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                    )
            decision_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(pipeline_run_decisions)")
            }
            for name in ("previous_mac", "integrity_mac"):
                if name not in decision_columns:
                    connection.execute(
                        f"ALTER TABLE pipeline_run_decisions ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                    )
            anchored = self._anchor_exists_and_is_valid()
            version = connection.execute(
                "SELECT value FROM pipeline_integrity_meta WHERE key='version'"
            ).fetchone()
            if anchored and (version is None or version[0] != "1"):
                raise PipelineRunConflict("The automation integrity metadata is missing or changed.")
            if not anchored:
                self._initialize_integrity(connection)
                connection.execute(
                    "INSERT OR REPLACE INTO pipeline_integrity_meta(key,value) VALUES('version','1')"
                )
                connection.commit()
                self._write_anchor()
            else:
                self._verify_all(connection)

    @staticmethod
    def _run_material(row: sqlite3.Row) -> list[Any]:
        return [row[name] for name in (
            "run_id", "request_id", "request_digest", "definition_digest",
            "definition_json", "name", "source", "state", "stop_requested",
            "waiting_step", "result_json", "owner_pid", "owner_token",
            "owner_thread_id", "attempt_id", "event_count", "event_head",
            "decision_count", "decision_head", "created_at_ms", "updated_at_ms",
        )]

    def _seal_run(self, connection: sqlite3.Connection, run_id: str) -> None:
        row = connection.execute(
            "SELECT * FROM pipeline_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise PipelineRunNotFound(f"There is no automation run {run_id}.")
        connection.execute(
            "UPDATE pipeline_runs SET integrity_mac=? WHERE run_id=?",
            (self._mac("run", self._run_material(row)), run_id),
        )

    def _verify_run(self, connection: sqlite3.Connection, row: sqlite3.Row | None) -> None:
        if row is None:
            return
        expected = self._mac("run", self._run_material(row))
        if not row["integrity_mac"] or not hmac.compare_digest(row["integrity_mac"], expected):
            raise PipelineRunConflict("Automation runtime integrity verification failed.")
        events = connection.execute(
            "SELECT sequence,integrity_mac FROM pipeline_run_events WHERE run_id=? ORDER BY sequence",
            (row["run_id"],),
        ).fetchall()
        event_head = str(events[-1]["integrity_mac"]) if events else ""
        if len(events) != int(row["event_count"]) or event_head != str(row["event_head"]):
            raise PipelineRunConflict("Automation event history integrity verification failed.")
        decisions = connection.execute(
            "SELECT integrity_mac FROM pipeline_run_decisions "
            "WHERE run_id=? ORDER BY rowid",
            (row["run_id"],),
        ).fetchall()
        decision_head = str(decisions[-1]["integrity_mac"]) if decisions else ""
        if (len(decisions) != int(row["decision_count"])
                or decision_head != str(row["decision_head"])):
            raise PipelineRunConflict("Automation decision history integrity verification failed.")

    def _verify_event(self, row: sqlite3.Row) -> None:
        material = [
            row["run_id"], row["sequence"], row["kind"], row["node"],
            row["payload_json"], row["created_at_ms"], row["previous_mac"],
        ]
        expected = self._mac("event", material)
        if not row["integrity_mac"] or not hmac.compare_digest(row["integrity_mac"], expected):
            raise PipelineRunConflict("Automation event integrity verification failed.")

    def _verify_decision(self, row: sqlite3.Row) -> None:
        material = [
            row["run_id"], row["step"], row["carry_on"], row["created_at_ms"],
            row["previous_mac"],
        ]
        expected = self._mac("decision", material)
        if not row["integrity_mac"] or not hmac.compare_digest(row["integrity_mac"], expected):
            raise PipelineRunConflict("Automation decision integrity verification failed.")

    def _initialize_integrity(self, connection: sqlite3.Connection) -> None:
        for run in connection.execute("SELECT run_id FROM pipeline_runs").fetchall():
            run_id = str(run["run_id"])
            event_head = ""
            event_count = 0
            for event in connection.execute(
                "SELECT * FROM pipeline_run_events WHERE run_id=? ORDER BY sequence", (run_id,)
            ).fetchall():
                event_count += 1
                material = [
                    event["run_id"], event["sequence"], event["kind"], event["node"],
                    event["payload_json"], event["created_at_ms"], event_head,
                ]
                event_head = self._mac("event", material)
                connection.execute(
                    "UPDATE pipeline_run_events SET previous_mac=?,integrity_mac=? "
                    "WHERE run_id=? AND sequence=?",
                    (material[-1], event_head, run_id, event["sequence"]),
                )
            decision_head = ""
            decision_count = 0
            for decision in connection.execute(
                "SELECT * FROM pipeline_run_decisions WHERE run_id=? ORDER BY rowid",
                (run_id,),
            ).fetchall():
                decision_count += 1
                material = [
                    decision["run_id"], decision["step"], decision["carry_on"],
                    decision["created_at_ms"], decision_head,
                ]
                decision_head = self._mac("decision", material)
                connection.execute(
                    "UPDATE pipeline_run_decisions SET previous_mac=?,integrity_mac=? "
                    "WHERE run_id=? AND step=?",
                    (material[-1], decision_head, run_id, decision["step"]),
                )
            connection.execute(
                "UPDATE pipeline_runs SET event_count=?,event_head=?,decision_count=?,decision_head=? "
                "WHERE run_id=?",
                (event_count, event_head, decision_count, decision_head, run_id),
            )
            self._seal_run(connection, run_id)

    def _verify_all(self, connection: sqlite3.Connection) -> None:
        for row in connection.execute("SELECT * FROM pipeline_runs").fetchall():
            self._verify_run(connection, row)
        for row in connection.execute("SELECT * FROM pipeline_run_events").fetchall():
            self._verify_event(row)
        for row in connection.execute("SELECT * FROM pipeline_run_decisions").fetchall():
            self._verify_decision(row)

    def _recover_orphans(self) -> None:
        with self._transaction() as connection:
            self._recover_orphans_in_transaction(connection)

    def _recover_orphans_in_transaction(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT * FROM pipeline_runs WHERE state IN (?,?,?,?)", ACTIVE_STATES
        ).fetchall()
        for row in rows:
            self._verify_run(connection, row)
            if _owner_is_alive(
                int(row["owner_pid"]), str(row["owner_token"] or ""),
                int(row["owner_thread_id"] or 0),
            ):
                continue
            result = {
                "run_id": row["run_id"], "passed": False, "outcome": "interrupted",
                "state": "interrupted", "nodes": [], "milliseconds": 0,
                "definition_digest": row["definition_digest"],
                "said": "The automation owner stopped before committing a terminal result.",
            }
            connection.execute(
                "UPDATE pipeline_runs SET state='interrupted',waiting_step='',result_json=?,updated_at_ms=? "
                "WHERE run_id=? AND state IN ('accepted','running','waiting','stopping')",
                (json.dumps(result, ensure_ascii=False), _now_ms(), row["run_id"]),
            )
            self._append(
                connection, row["run_id"], "pipeline_interrupted", "pipeline", result
            )

    @staticmethod
    def _row(
        row: sqlite3.Row | None, *, include_attempt: bool = False
    ) -> dict[str, Any] | None:
        if row is None:
            return None
        result = json.loads(row["result_json"]) if row["result_json"] else None
        value = {
            "run_id": row["run_id"],
            "request_id": row["request_id"],
            "definition_digest": row["definition_digest"],
            "definition": json.loads(row["definition_json"]),
            "name": row["name"],
            "source": row["source"],
            "state": row["state"],
            "running": row["state"] in ACTIVE_STATES,
            "stop_requested": bool(row["stop_requested"]),
            "waiting_at": row["waiting_step"],
            "result": result,
            "created_at_ms": row["created_at_ms"],
            "updated_at_ms": row["updated_at_ms"],
        }
        if include_attempt:
            value["attempt_id"] = row["attempt_id"]
        return value

    def accept(
        self,
        definition: dict[str, Any],
        *,
        source: str,
        request_id: str = "",
    ) -> tuple[dict[str, Any], bool]:
        """Atomically admit one run or replay one exact request."""

        safe_definition = self.redactor.value(definition)
        if not isinstance(safe_definition, dict):
            raise PipelineRunConflict("The automation definition could not be safely stored.")
        digest = definition_digest(definition)
        named_definition = (
            definition.get("pipeline")
            if isinstance(definition.get("pipeline"), dict)
            else definition
        )
        request_digest = hashlib.sha256(
            canonical_definition({"definition": definition, "source": source}).encode("utf-8")
        ).hexdigest()
        request_id = request_id.strip() or uuid.uuid4().hex
        if (not _REQUEST_ID.fullmatch(request_id)
                or self.redactor.text(request_id) != request_id):
            raise PipelineRunConflict(
                "Request IDs may use only 1-200 non-secret letters, numbers, and ._:@+- characters."
            )
        with self._transaction() as connection:
            self._recover_orphans_in_transaction(connection)
            replay = connection.execute(
                "SELECT * FROM pipeline_runs WHERE request_id=?", (request_id,)
            ).fetchone()
            if replay is not None:
                self._verify_run(connection, replay)
                if replay["request_digest"] != request_digest:
                    raise PipelineRunConflict(
                        "That request ID was already used for a different automation run."
                    )
                return self._row(replay, include_attempt=True) or {}, False
            active = connection.execute(
                "SELECT * FROM pipeline_runs WHERE state IN (?,?,?,?) ORDER BY created_at_ms LIMIT 1",
                ACTIVE_STATES,
            ).fetchone()
            if active is not None:
                self._verify_run(connection, active)
                raise PipelineRunConflict(
                    f"A pipeline is running already: automation {active['run_id']} "
                    f"is {active['state']}. "
                    "Wait for it, or stop that exact run."
                )
            run_id = uuid.uuid4().hex
            attempt_id = uuid.uuid4().hex
            now = _now_ms()
            connection.execute(
                """INSERT INTO pipeline_runs
                   (run_id,request_id,request_digest,definition_digest,definition_json,
                    name,source,state,owner_pid,owner_token,attempt_id,created_at_ms,updated_at_ms)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    request_id,
                    request_digest,
                    digest,
                    canonical_definition(safe_definition),
                    self.redactor.text(str(named_definition.get("name") or "Pipeline")),
                    self.redactor.text(source)[:120],
                    "accepted",
                    os.getpid(),
                    _process_token(os.getpid()),
                    attempt_id,
                    now,
                    now,
                ),
            )
            self._append(connection, run_id, "pipeline_accepted", "pipeline", {
                "run_id": run_id, "name": named_definition.get("name", ""),
                "definition_digest": digest, "source": source,
            })
            row = connection.execute(
                "SELECT * FROM pipeline_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            return self._row(row, include_attempt=True) or {}, True

    def _append(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        kind: str,
        node: str,
        payload: Any,
    ) -> int:
        run = connection.execute(
            "SELECT event_count,event_head FROM pipeline_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if run is None:
            raise PipelineRunNotFound(f"There is no automation run {run_id}.")
        sequence = int(run["event_count"]) + 1
        previous = str(run["event_head"] or "")
        clean = self.redactor.value(payload)
        payload_json = json.dumps(
            clean, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        created = _now_ms()
        kind = kind[:80]
        node = node[:160]
        event_mac = self._mac(
            "event", [run_id, sequence, kind, node, payload_json, created, previous]
        )
        connection.execute(
            """INSERT INTO pipeline_run_events
               (run_id,sequence,kind,node,payload_json,created_at_ms,previous_mac,integrity_mac)
               VALUES (?,?,?,?,?,?,?,?)""",
            (run_id, sequence, kind, node, payload_json, created, previous, event_mac),
        )
        connection.execute(
            "UPDATE pipeline_runs SET event_count=?,event_head=? WHERE run_id=?",
            (sequence, event_mac, run_id),
        )
        self._seal_run(connection, run_id)
        return sequence

    def start(self, run_id: str, attempt_id: str) -> dict[str, Any]:
        with self._transaction() as connection:
            before = connection.execute(
                "SELECT * FROM pipeline_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if before is not None:
                self._verify_run(connection, before)
            changed = connection.execute(
                "UPDATE pipeline_runs SET state='running',owner_pid=?,owner_token=?,"
                "owner_thread_id=?,updated_at_ms=? "
                "WHERE run_id=? AND attempt_id=? AND state='accepted' AND stop_requested=0",
                (
                    os.getpid(), _process_token(os.getpid()),
                    int(threading.get_native_id()), _now_ms(), run_id, attempt_id,
                ),
            ).rowcount
            if not changed:
                row = connection.execute(
                    "SELECT * FROM pipeline_runs WHERE run_id=?", (run_id,)
                ).fetchone()
                if row is None:
                    raise PipelineRunNotFound(f"There is no automation run {run_id}.")
                if row["attempt_id"] != attempt_id:
                    raise PipelineRunConflict("That worker attempt does not own this automation run.")
                if row["stop_requested"]:
                    raise PipelineRunConflict(f"Automation run {run_id} was stopped before it began.")
                raise PipelineRunConflict(f"Automation run {run_id} cannot start from {row['state']}.")
            self._append(connection, run_id, "pipeline_started", "pipeline", {"run_id": run_id})
            return self._row(connection.execute(
                "SELECT * FROM pipeline_runs WHERE run_id=?", (run_id,)
            ).fetchone()) or {}

    def append_event(self, run_id: str, attempt_id: str, event: dict[str, Any]) -> int:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM pipeline_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise PipelineRunNotFound(f"There is no automation run {run_id}.")
            self._verify_run(connection, row)
            if row["attempt_id"] != attempt_id:
                raise PipelineRunConflict("That worker attempt does not own this automation run.")
            if row["state"] not in ACTIVE_STATES:
                raise PipelineRunConflict(
                    f"Automation run {run_id} is already {row['state']}; a late event was rejected."
                )
            sequence = self._append(
                connection, run_id, str(event.get("kind") or "pipeline_event"),
                str(event.get("node") or ""), event.get("payload", {}),
            )
            connection.execute(
                "UPDATE pipeline_runs SET updated_at_ms=? WHERE run_id=?", (_now_ms(), run_id)
            )
            self._seal_run(connection, run_id)
            return sequence

    def set_waiting(self, run_id: str, attempt_id: str, step: str) -> None:
        with self._transaction() as connection:
            before = connection.execute(
                "SELECT * FROM pipeline_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if before is not None:
                self._verify_run(connection, before)
            state = "waiting" if step else "running"
            changed = connection.execute(
                "UPDATE pipeline_runs SET state=?,waiting_step=?,updated_at_ms=? "
                "WHERE run_id=? AND attempt_id=? AND state IN ('running','waiting')",
                (state, step, _now_ms(), run_id, attempt_id),
            ).rowcount
            if not changed:
                raise PipelineRunConflict(f"Automation run {run_id} is no longer waiting/running.")
            self._seal_run(connection, run_id)

    def request_stop(self, run_id: str) -> dict[str, Any]:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM pipeline_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise PipelineRunNotFound(f"There is no automation run {run_id}.")
            self._verify_run(connection, row)
            if row["state"] not in ACTIVE_STATES:
                raise PipelineRunConflict(
                    f"Automation run {run_id} is already {row['state']}; Stop changed nothing."
                )
            if not row["stop_requested"]:
                connection.execute(
                    "UPDATE pipeline_runs SET stop_requested=1,state='stopping',updated_at_ms=? "
                    "WHERE run_id=?",
                    (_now_ms(), run_id),
                )
                self._append(connection, run_id, "pipeline_stop_requested", "pipeline", {
                    "run_id": run_id
                })
            return self._row(connection.execute(
                "SELECT * FROM pipeline_runs WHERE run_id=?", (run_id,)
            ).fetchone()) or {}

    def should_stop(self, run_id: str) -> bool:
        with self._reader() as connection:
            row = connection.execute(
                "SELECT * FROM pipeline_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is not None:
                self._verify_run(connection, row)
        return row is None or bool(row["stop_requested"]) or row["state"] not in ACTIVE_STATES

    def decide(self, run_id: str, step: str, carry_on: bool) -> dict[str, Any]:
        if not step or len(step) > 500 or any(ord(char) < 32 for char in step):
            raise PipelineRunConflict("The decision occurrence identity is invalid.")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM pipeline_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise PipelineRunNotFound(f"There is no automation run {run_id}.")
            self._verify_run(connection, row)
            if row["state"] != "waiting" or row["waiting_step"] != step:
                raise PipelineRunConflict(
                    f"Automation run {run_id} is not waiting at {step}; the stale answer changed nothing."
                )
            prior = connection.execute(
                "SELECT * FROM pipeline_run_decisions WHERE run_id=? AND step=?",
                (run_id, step),
            ).fetchone()
            if prior is not None:
                self._verify_decision(prior)
                if bool(prior["carry_on"]) != carry_on:
                    raise PipelineRunConflict("That step already received a different answer.")
                return {"run_id": run_id, "step": step, "carry_on": carry_on}
            created = _now_ms()
            previous = str(row["decision_head"] or "")
            decision_mac = self._mac(
                "decision", [run_id, step, int(carry_on), created, previous]
            )
            connection.execute(
                """INSERT INTO pipeline_run_decisions
                   (run_id,step,carry_on,created_at_ms,previous_mac,integrity_mac)
                   VALUES (?,?,?,?,?,?)""",
                (run_id, step, int(carry_on), created, previous, decision_mac),
            )
            connection.execute(
                "UPDATE pipeline_runs SET decision_count=decision_count+1,decision_head=? "
                "WHERE run_id=?",
                (decision_mac, run_id),
            )
            self._append(connection, run_id, "pipeline_decision", step, {
                "run_id": run_id, "step": step, "carry_on": carry_on,
            })
            return {"run_id": run_id, "step": step, "carry_on": carry_on}

    def decision(self, run_id: str, step: str) -> bool | None:
        with self._reader() as connection:
            run = connection.execute(
                "SELECT * FROM pipeline_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is not None:
                self._verify_run(connection, run)
            row = connection.execute(
                "SELECT * FROM pipeline_run_decisions WHERE run_id=? AND step=?",
                (run_id, step),
            ).fetchone()
            if row is not None:
                self._verify_decision(row)
        return None if row is None else bool(row["carry_on"])

    def finish(self, run_id: str, attempt_id: str, result: dict[str, Any]) -> dict[str, Any]:
        safe = self.redactor.value(result)
        if not isinstance(safe, dict):
            safe = {"passed": False, "said": "The result could not be safely stored."}
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM pipeline_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise PipelineRunNotFound(f"There is no automation run {run_id}.")
            self._verify_run(connection, row)
            if row["attempt_id"] != attempt_id:
                raise PipelineRunConflict("That worker attempt does not own this automation run.")
            if row["state"] not in ACTIVE_STATES:
                return self._row(row) or {}
            if row["stop_requested"]:
                state = "cancelled"
                safe["passed"] = False
                safe["outcome"] = "cancelled"
                safe["said"] = "The automation was stopped; a late result was not accepted as passed."
            else:
                outcome = str(safe.get("outcome") or ("passed" if safe.get("passed") else "failed"))
                state = outcome if outcome in TERMINAL_STATES else ("passed" if safe.get("passed") else "failed")
            safe.update({
                "run_id": run_id,
                "state": state,
                "definition_digest": row["definition_digest"],
            })
            connection.execute(
                "UPDATE pipeline_runs SET state=?,waiting_step='',result_json=?,updated_at_ms=? "
                "WHERE run_id=? AND state IN ('accepted','running','waiting','stopping')",
                (state, json.dumps(safe, ensure_ascii=False), _now_ms(), run_id),
            )
            self._append(connection, run_id, "pipeline_finished", "pipeline", safe)
            return self._row(connection.execute(
                "SELECT * FROM pipeline_runs WHERE run_id=?", (run_id,)
            ).fetchone()) or {}

    def fail(self, run_id: str, attempt_id: str, message: str) -> dict[str, Any]:
        return self.finish(run_id, attempt_id, {
            "passed": False, "outcome": "failed", "nodes": [], "milliseconds": 0,
            "said": self.redactor.text(message),
        })

    def get(self, run_id: str) -> dict[str, Any]:
        with self._reader() as connection:
            row = connection.execute(
                "SELECT * FROM pipeline_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is not None:
                self._verify_run(connection, row)
        if row is None:
            raise PipelineRunNotFound(f"There is no automation run {run_id}.")
        value = self._row(row) or {}
        value["project_authority_id"] = self.authority_id
        return value

    def by_request(self, request_id: str) -> dict[str, Any]:
        """Resolve an idempotency key only inside this project's authority store."""

        request_id = str(request_id or "").strip()
        if not _REQUEST_ID.fullmatch(request_id):
            raise PipelineRunNotFound("That automation request ID is invalid.")
        with self._reader() as connection:
            row = connection.execute(
                "SELECT * FROM pipeline_runs WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is not None:
                self._verify_run(connection, row)
        if row is None:
            raise PipelineRunNotFound(
                "There is no automation run for that request in this project authority."
            )
        value = self._row(row) or {}
        value["project_authority_id"] = self.authority_id
        return value

    def latest(self) -> dict[str, Any] | None:
        with self._reader() as connection:
            row = connection.execute(
                "SELECT * FROM pipeline_runs ORDER BY created_at_ms DESC LIMIT 1"
            ).fetchone()
            if row is not None:
                self._verify_run(connection, row)
        return self._row(row)

    def active(self) -> dict[str, Any] | None:
        with self._reader() as connection:
            row = connection.execute(
                "SELECT * FROM pipeline_runs WHERE state IN (?,?,?,?) "
                "ORDER BY created_at_ms LIMIT 1",
                ACTIVE_STATES,
            ).fetchone()
            if row is not None:
                self._verify_run(connection, row)
        return self._row(row)

    def events(self, run_id: str, after: int = 0) -> list[dict[str, Any]]:
        self.get(run_id)
        with self._reader() as connection:
            rows = connection.execute(
                "SELECT * FROM pipeline_run_events WHERE run_id=? AND sequence>? "
                "ORDER BY sequence",
                (run_id, max(0, int(after))),
            ).fetchall()
            previous = ""
            if after:
                prior = connection.execute(
                    "SELECT integrity_mac FROM pipeline_run_events WHERE run_id=? AND sequence=?",
                    (run_id, max(0, int(after))),
                ).fetchone()
                if prior is None:
                    raise PipelineRunConflict("The automation event cursor is stale or forged.")
                previous = str(prior["integrity_mac"])
            expected_sequence = max(0, int(after)) + 1
            for row in rows:
                self._verify_event(row)
                if row["sequence"] != expected_sequence or row["previous_mac"] != previous:
                    raise PipelineRunConflict("Automation event chain integrity verification failed.")
                expected_sequence += 1
                previous = str(row["integrity_mac"])
        return [{
            "run_id": run_id,
            "sequence": row["sequence"],
            "kind": row["kind"],
            "node": row["node"],
            "payload": json.loads(row["payload_json"]),
            "created_at_ms": row["created_at_ms"],
        } for row in rows]
