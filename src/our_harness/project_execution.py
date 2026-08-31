"""Durable, user-scoped coordination for project-mutating engines.

The coordinator deliberately lives outside project folders.  It gives every
engine the same atomic answer to one question: may this request own all of
these project roots?  Reservations begin as short provisional claims and are
then explicitly bound to a durable engine/job owner.  Only provisional claims
may be reaped automatically; running, paused, and waiting claims survive both
process death and application restart until their capability holder releases
them.

The HMAC protects against corruption and blind SQLite rewrites.  As with the
other user-runtime integrity stores, it is not an isolation boundary against
arbitrary code already running as the same OS user.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
import time
from typing import Any, Callable, Iterator, Sequence
import uuid

from .models import HarnessError


SCHEMA_VERSION = 1
DATABASE_NAME = "project-execution.sqlite3"
INTEGRITY_KEY_NAME = "integrity.key"
INTEGRITY_ANCHOR_NAME = "integrity.anchor"
DEFAULT_PROVISIONAL_TTL_MS = 30_000
MAX_PROVISIONAL_TTL_MS = 24 * 60 * 60 * 1000

PROVISIONAL_STATE = "provisional"
DURABLE_STATES = frozenset({"running", "paused", "waiting"})
ACTIVE_STATES = frozenset({PROVISIONAL_STATE, *DURABLE_STATES})
TERMINAL_STATES = frozenset({"released"})
ALL_STATES = frozenset({*ACTIVE_STATES, *TERMINAL_STATES})

_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")
_OWNER_ID = re.compile(r"[^\x00-\x1f\x7f]{1,200}")
_TOKEN = re.compile(r"[0-9a-f]{64}")
_MAX_INTENT_BYTES = 1024 * 1024
_MAX_ROOTS = 64

_CONTRACT = {
    "name": "our-harness-project-execution-coordinator",
    "schema_version": SCHEMA_VERSION,
    "path_identity": "existing-directory-final-handle-os-normcase-v2",
    "overlap": "exact-or-ancestor-in-either-direction-v1",
    "intent": "canonical-json-plus-canonical-roots-sha256-v1",
    "reservation": "atomic-begin-immediate-multi-root-v1",
    "provisional_reclaim": "owner-dead-or-deadline-expired-v1",
    "durable_states": sorted(DURABLE_STATES),
    "mutation_authority": "rotating-reservation-id-generation-capability-v2",
    "bind_replay": "source-capability-hmac-exact-owner-v1",
    "integrity": "hmac-sha256-user-key-store-commitment-anchor-v2",
}


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


CONTRACT_FINGERPRINT = hashlib.sha256(_canonical(_CONTRACT)).hexdigest()


class ProjectExecutionError(HarnessError):
    """Base class for coordinator failures."""


class ProjectExecutionConflict(ProjectExecutionError):
    """One or more requested roots overlap another active reservation."""

    def __init__(
        self,
        message: str,
        *,
        reservation_id: str = "",
        request_id: str = "",
        state: str = "",
        roots: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.reservation_id = reservation_id
        self.request_id = request_id
        self.state = state
        self.roots = tuple(roots)


class ProjectExecutionIntentConflict(ProjectExecutionError):
    """An idempotency key was reused for a different canonical intent."""


class ProjectExecutionTokenError(ProjectExecutionError):
    """A stale or invalid reservation capability attempted a mutation."""


class ProjectExecutionStateError(ProjectExecutionError):
    """A requested state transition is not permitted."""


class ProjectExecutionIntegrityError(ProjectExecutionError):
    """Coordinator metadata or a reservation failed integrity validation."""


@dataclass(frozen=True)
class ProjectExecutionReservation:
    schema_version: int
    contract_fingerprint: str
    reservation_id: str
    request_id: str
    intent_sha256: str
    roots: tuple[str, ...]
    state: str
    generation: int
    lease_token: str
    provisional_owner_id: str
    durable_owner_id: str
    owner_pid: int
    owner_process_token: str
    expires_at_ms: int
    created_at_ms: int
    updated_at_ms: int
    released_at_ms: int
    release_reason: str

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_STATES

    @property
    def durable(self) -> bool:
        return self.state in DURABLE_STATES

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "contract_fingerprint": self.contract_fingerprint,
            "reservation_id": self.reservation_id,
            "request_id": self.request_id,
            "intent_sha256": self.intent_sha256,
            "roots": list(self.roots),
            "state": self.state,
            "generation": self.generation,
            "lease_token": self.lease_token,
            "provisional_owner_id": self.provisional_owner_id,
            "durable_owner_id": self.durable_owner_id,
            "owner_pid": self.owner_pid,
            "owner_process_token": self.owner_process_token,
            "expires_at_ms": self.expires_at_ms,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
            "released_at_ms": self.released_at_ms,
            "release_reason": self.release_reason,
        }


def _default_base_dir() -> Path:
    override = os.environ.get("OUR_HARNESS_PROJECT_EXECUTION_DIR", "").strip()
    if override:
        # Preserve this exact final component until _prepare_base_dir has had
        # a chance to reject a caller-supplied link or reparse point.
        return Path(override).expanduser()
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "OurHarness" / "project-execution"
    state = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(state) if state else Path.home() / ".local" / "state"
    return base / "our-harness" / "project-execution"


def _is_link_or_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except (FileNotFoundError, OSError):
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _prepare_base_dir(value: str | os.PathLike[str]) -> Path:
    """Create and return one canonical identity for the user runtime.

    The spelling supplied by an environment variable or caller is not a
    durable identity.  In particular, Windows may hand the same profile to
    different processes as both ``RUNNER~1`` and ``runneradmin``.  Resolve
    before creation so existing ancestor aliases converge, then resolve the
    completed directory again so every coordinator publishes paths beneath
    the same physical directory spelling.
    """

    requested = Path(value).expanduser()
    if _is_link_or_reparse(requested):
        raise ProjectExecutionIntegrityError(
            "The project-execution runtime directory must not be a link or reparse point."
        )
    try:
        selected = requested.resolve(strict=False)
        selected.mkdir(parents=True, exist_ok=True)
        # Check the caller's exact final component again after publication so
        # a concurrent redirect cannot become the durable SQLite owner.
        if _is_link_or_reparse(requested):
            raise ProjectExecutionIntegrityError(
                "The project-execution runtime directory must not be a link or reparse point."
            )
        canonical = selected.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProjectExecutionIntegrityError(
            "The project-execution runtime directory could not be prepared."
        ) from exc
    if _is_link_or_reparse(canonical) or not canonical.is_dir():
        raise ProjectExecutionIntegrityError(
            "The project-execution runtime directory is not a regular directory."
        )
    return canonical


def _current_user_scope() -> str:
    if os.name == "nt":
        # Environment labels and account names can change while the Windows
        # security principal remains the same.  Bind durable state to the
        # access-token SID instead.
        try:
            import ctypes
            from ctypes import wintypes

            token_query = 0x0008
            token_user = 1
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
            kernel32.LocalFree.restype = wintypes.HLOCAL
            advapi32.OpenProcessToken.argtypes = (
                wintypes.HANDLE,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.HANDLE),
            )
            advapi32.OpenProcessToken.restype = wintypes.BOOL
            advapi32.GetTokenInformation.argtypes = (
                wintypes.HANDLE,
                ctypes.c_uint,
                wintypes.LPVOID,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
            )
            advapi32.GetTokenInformation.restype = wintypes.BOOL
            advapi32.ConvertSidToStringSidW.argtypes = (
                wintypes.LPVOID,
                ctypes.POINTER(wintypes.LPWSTR),
            )
            advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL

            token = wintypes.HANDLE()
            if not advapi32.OpenProcessToken(
                kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)
            ):
                raise OSError(ctypes.get_last_error(), "OpenProcessToken failed")
            sid_text = wintypes.LPWSTR()
            try:
                needed = wintypes.DWORD()
                advapi32.GetTokenInformation(
                    token, token_user, None, 0, ctypes.byref(needed)
                )
                if not needed.value:
                    raise OSError(
                        ctypes.get_last_error(), "GetTokenInformation sizing failed"
                    )
                token_data = ctypes.create_string_buffer(needed.value)
                if not advapi32.GetTokenInformation(
                    token,
                    token_user,
                    token_data,
                    needed,
                    ctypes.byref(needed),
                ):
                    raise OSError(
                        ctypes.get_last_error(), "GetTokenInformation failed"
                    )
                sid_pointer = ctypes.cast(
                    token_data, ctypes.POINTER(ctypes.c_void_p)
                ).contents.value
                if not sid_pointer or not advapi32.ConvertSidToStringSidW(
                    sid_pointer, ctypes.byref(sid_text)
                ):
                    raise OSError(
                        ctypes.get_last_error(), "ConvertSidToStringSidW failed"
                    )
                principal = f"sid:{sid_text.value}"
            finally:
                if sid_text:
                    kernel32.LocalFree(sid_text)
                kernel32.CloseHandle(token)
        except (AttributeError, OSError, ValueError):
            # A SID lookup failure must not silently select a mutable identity.
            # Callers with a constrained host can inject an explicit scope.
            raise ProjectExecutionError(
                "The current Windows user SID could not be resolved."
            )
    else:
        try:
            principal = f"uid:{os.getuid()}"
        except AttributeError:
            principal = getpass.getuser()
    return hashlib.sha256(
        f"our-harness-user-scope-v1\x00{principal}".encode("utf-8")
    ).hexdigest()


def _process_token(pid: int) -> str:
    """Return a best-effort process birth identity to protect against PID reuse."""

    if pid <= 0:
        return ""
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = (
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            )
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetProcessTimes.argtypes = (
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            )
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            process = kernel32.OpenProcess(0x1000, False, pid)
            if not process:
                return ""
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            try:
                if not kernel32.GetProcessTimes(
                    process,
                    ctypes.byref(creation),
                    ctypes.byref(exit_time),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                ):
                    return ""
                return f"{creation.dwHighDateTime}:{creation.dwLowDateTime}"
            finally:
                kernel32.CloseHandle(process)
        except (AttributeError, OSError):
            return ""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        # comm is parenthesized and may itself contain spaces or parentheses.
        # Field 3 begins after the final ") "; starttime is field 22.
        delimiter = stat.rfind(") ")
        if delimiter < 0:
            return ""
        fields_after_comm = stat[delimiter + 2 :].split()
        return fields_after_comm[19]
    except (OSError, IndexError):
        return ""


def _owner_is_alive(pid: int, expected_token: str) -> bool:
    if pid <= 0:
        return True
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = (
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            )
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = (
                wintypes.HANDLE,
                wintypes.DWORD,
            )
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL
            process = kernel32.OpenProcess(0x100000 | 0x1000, False, pid)
            if not process:
                # Access denied proves the PID exists, but not its birth token.
                # Treat it as alive so automatic cleanup fails closed.
                return int(ctypes.get_last_error()) == 5
            try:
                wait_result = int(kernel32.WaitForSingleObject(process, 0))
                if wait_result == 0:
                    return False
                if wait_result != 258:
                    return True
                current = _process_token(pid)
                return not expected_token or not current or current == expected_token
            finally:
                kernel32.CloseHandle(process)
        except (AttributeError, OSError):
            return True
    current = _process_token(pid)
    if current:
        return not expected_token or current == expected_token
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, PermissionError):
        return True
    return True


class ProjectExecutionCoordinator:
    """Owns atomic, durable execution reservations for one desktop user."""

    def __init__(
        self,
        *,
        base_dir: str | os.PathLike[str] | None = None,
        provisional_ttl_ms: int = DEFAULT_PROVISIONAL_TTL_MS,
        clock_ms: Callable[[], int] | None = None,
        owner_alive: Callable[[int, str], bool] | None = None,
        user_scope: str | None = None,
    ) -> None:
        requested_base_dir = Path(
            base_dir if base_dir is not None else _default_base_dir()
        ).expanduser()
        self.base_dir = requested_base_dir.resolve(strict=False)
        self._default_ttl_ms = self._validate_ttl(provisional_ttl_ms)
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._owner_alive = owner_alive or _owner_is_alive
        self.user_scope = str(user_scope or _current_user_scope())
        if not _TOKEN.fullmatch(self.user_scope):
            raise ProjectExecutionError("The user scope must be a 64-character lowercase SHA-256 value.")

        self.base_dir = _prepare_base_dir(requested_base_dir)
        try:
            self.base_dir.chmod(0o700)
        except OSError:
            pass

        self.path = self.base_dir / DATABASE_NAME
        self.key_path = self.base_dir / INTEGRITY_KEY_NAME
        self.anchor_path = self.base_dir / INTEGRITY_ANCHOR_NAME
        self._initialized = False
        self._integrity_key = self._load_or_create_key()
        self._anchor_preexisting = self.anchor_path.exists()
        if self._anchor_preexisting:
            self._verify_anchor()
        self._local_lock = threading.RLock()
        self._initialize()
        self._ensure_anchor()
        self._initialized = True

    @property
    def schema_version(self) -> int:
        return SCHEMA_VERSION

    @property
    def contract_fingerprint(self) -> str:
        return CONTRACT_FINGERPRINT

    @staticmethod
    def _validate_ttl(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ProjectExecutionError("The provisional TTL must be an integer number of milliseconds.")
        ttl = value
        if ttl <= 0 or ttl > MAX_PROVISIONAL_TTL_MS:
            raise ProjectExecutionError(
                f"The provisional TTL must be between 1 and {MAX_PROVISIONAL_TTL_MS} milliseconds."
            )
        return ttl

    def _publish_new_file(self, path: Path, payload: bytes, *, mode: int) -> bool:
        """Atomically publish complete immutable initialization material.

        A hard-link publication never exposes the temporary file's partial
        contents and, unlike replace(), never overwrites another initializer's
        winner.  The temporary file always lives on the same filesystem.
        """

        temporary = self.base_dir / f".{path.name}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            mode,
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
                published = True
            except FileExistsError:
                published = False
            try:
                path.chmod(mode)
            except OSError:
                pass
            return published
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _load_or_create_key(self) -> bytes:
        if self.key_path.exists():
            if self.key_path.is_symlink() or not self.key_path.is_file():
                raise ProjectExecutionIntegrityError(
                    "The project-execution integrity key is not a regular file."
                )
            key = self.key_path.read_bytes()
            if len(key) != 32:
                raise ProjectExecutionIntegrityError(
                    "The project-execution integrity key is invalid."
                )
            return key
        if self.path.exists() or self.anchor_path.exists():
            raise ProjectExecutionIntegrityError(
                "Initialized project-execution state exists without its integrity key."
            )
        key = secrets.token_bytes(32)
        if self._publish_new_file(self.key_path, key, mode=0o600):
            return key
        loaded = self.key_path.read_bytes()
        if len(loaded) != 32:
            raise ProjectExecutionIntegrityError(
                "The project-execution integrity key is invalid."
            )
        return loaded

    def _mac(self, kind: str, value: Any) -> str:
        payload = _canonical({"kind": kind, "value": value})
        return hmac.new(self._integrity_key, payload, hashlib.sha256).hexdigest()

    def _anchor_value(self) -> dict[str, Any]:
        material = [SCHEMA_VERSION, CONTRACT_FINGERPRINT, self.user_scope]
        return {
            "schema_version": SCHEMA_VERSION,
            "contract_fingerprint": CONTRACT_FINGERPRINT,
            "user_scope": self.user_scope,
            "integrity_mac": self._mac("project-execution-anchor-v1", material),
        }

    def _verify_anchor(self) -> None:
        if self.anchor_path.is_symlink() or not self.anchor_path.is_file():
            raise ProjectExecutionIntegrityError(
                "The project-execution integrity anchor is not a regular file."
            )
        try:
            value = json.loads(self.anchor_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProjectExecutionIntegrityError(
                "The project-execution integrity anchor cannot be read."
            ) from exc
        expected = self._anchor_value()
        try:
            valid = (
                isinstance(value, dict)
                and set(value) == set(expected)
                and int(value.get("schema_version", 0)) == SCHEMA_VERSION
                and str(value.get("contract_fingerprint", ""))
                == CONTRACT_FINGERPRINT
                and str(value.get("user_scope", "")) == self.user_scope
                and self._valid_mac(
                    value.get("integrity_mac"), expected["integrity_mac"]
                )
            )
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise ProjectExecutionIntegrityError(
                "The project-execution integrity anchor failed validation."
            )

    def _ensure_anchor(self) -> None:
        if self.anchor_path.exists():
            self._verify_anchor()
            return
        encoded = (json.dumps(
            self._anchor_value(), ensure_ascii=False, sort_keys=True, indent=2
        ) + "\n").encode("utf-8")
        if not self._publish_new_file(self.anchor_path, encoded, mode=0o600):
            self._verify_anchor()

    def _connect(self) -> sqlite3.Connection:
        if self._initialized and not self.anchor_path.exists():
            raise ProjectExecutionIntegrityError(
                "The initialized project-execution integrity anchor is missing."
            )
        if self.anchor_path.exists() and not self.path.exists():
            raise ProjectExecutionIntegrityError(
                "The initialized project-execution database is missing."
            )
        if self.path.exists() and (self.path.is_symlink() or not self.path.is_file()):
            raise ProjectExecutionIntegrityError(
                "The project-execution database is not a regular file."
            )
        db = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        return db

    @staticmethod
    def _valid_mac(actual: Any, expected: str) -> bool:
        return (
            isinstance(actual, str)
            and _TOKEN.fullmatch(actual) is not None
            and hmac.compare_digest(actual, expected)
        )

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        with self._local_lock:
            db = self._connect()
            try:
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                self._verify_metadata(db)
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    def _initialize(self) -> None:
        with self._local_lock:
            db = self._connect()
            try:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS project_execution_metadata(
                      singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                      schema_version INTEGER NOT NULL,
                      contract_fingerprint TEXT NOT NULL,
                      user_scope TEXT NOT NULL,
                      reservation_count INTEGER NOT NULL,
                      reservations_head TEXT NOT NULL,
                      integrity_mac TEXT NOT NULL
                    )
                    """
                )
                db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS project_execution_reservations(
                      reservation_id TEXT PRIMARY KEY,
                      request_id TEXT NOT NULL UNIQUE,
                      schema_version INTEGER NOT NULL,
                      contract_fingerprint TEXT NOT NULL,
                      intent_sha256 TEXT NOT NULL,
                      intent_json TEXT NOT NULL,
                      roots_json TEXT NOT NULL,
                      state TEXT NOT NULL,
                      generation INTEGER NOT NULL,
                      lease_token TEXT NOT NULL,
                      bind_replay_mac TEXT NOT NULL,
                      provisional_owner_id TEXT NOT NULL,
                      durable_owner_id TEXT NOT NULL,
                      owner_pid INTEGER NOT NULL,
                      owner_process_token TEXT NOT NULL,
                      expires_at_ms INTEGER NOT NULL,
                      created_at_ms INTEGER NOT NULL,
                      updated_at_ms INTEGER NOT NULL,
                      released_at_ms INTEGER NOT NULL,
                      release_reason TEXT NOT NULL,
                      integrity_mac TEXT NOT NULL
                    )
                    """
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS project_execution_active_state "
                    "ON project_execution_reservations(state)"
                )
                self._verify_schema(db)
                held = db.execute(
                    "SELECT * FROM project_execution_metadata WHERE singleton=1"
                ).fetchone()
                if held is None:
                    count = int(
                        db.execute(
                            "SELECT COUNT(*) FROM project_execution_reservations"
                        ).fetchone()[0]
                    )
                    if count or self._anchor_preexisting:
                        raise ProjectExecutionIntegrityError(
                            "Coordinator metadata is missing from an initialized database."
                        )
                    reservation_count, reservations_head = self._store_commitment(db)
                    material = self._metadata_material(
                        reservation_count, reservations_head
                    )
                    db.execute(
                        "INSERT INTO project_execution_metadata("
                        "singleton,schema_version,contract_fingerprint,user_scope,"
                        "reservation_count,reservations_head,integrity_mac"
                        ") VALUES(1,?,?,?,?,?,?)",
                        (
                            SCHEMA_VERSION,
                            CONTRACT_FINGERPRINT,
                            self.user_scope,
                            reservation_count,
                            reservations_head,
                            self._mac("project-execution-metadata-v1", material),
                        ),
                    )
                else:
                    self._verify_metadata(db)
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _expected_columns() -> dict[str, tuple[str, ...]]:
        return {
            "project_execution_metadata": (
                "singleton",
                "schema_version",
                "contract_fingerprint",
                "user_scope",
                "reservation_count",
                "reservations_head",
                "integrity_mac",
            ),
            "project_execution_reservations": (
                "reservation_id",
                "request_id",
                "schema_version",
                "contract_fingerprint",
                "intent_sha256",
                "intent_json",
                "roots_json",
                "state",
                "generation",
                "lease_token",
                "bind_replay_mac",
                "provisional_owner_id",
                "durable_owner_id",
                "owner_pid",
                "owner_process_token",
                "expires_at_ms",
                "created_at_ms",
                "updated_at_ms",
                "released_at_ms",
                "release_reason",
                "integrity_mac",
            ),
        }

    def _verify_schema(self, db: sqlite3.Connection) -> None:
        for table, expected in self._expected_columns().items():
            actual = tuple(
                str(row[1]) for row in db.execute(f"PRAGMA table_info({table})").fetchall()
            )
            if actual != expected:
                raise ProjectExecutionIntegrityError(
                    f"Unsupported or damaged project-execution schema for {table}."
                )

    def _metadata_material(
        self, reservation_count: int, reservations_head: str
    ) -> list[Any]:
        return [
            SCHEMA_VERSION,
            CONTRACT_FINGERPRINT,
            self.user_scope,
            int(reservation_count),
            str(reservations_head),
        ]

    def _verify_metadata_row(self, row: sqlite3.Row) -> None:
        try:
            schema_version = self._db_int(row["schema_version"])
            reservation_count = self._db_int(row["reservation_count"])
            reservations_head = row["reservations_head"]
            integrity_mac = row["integrity_mac"]
        except (TypeError, ValueError) as exc:
            raise ProjectExecutionIntegrityError(
                "The project-execution metadata is malformed."
            ) from exc
        if (
            schema_version != SCHEMA_VERSION
            or str(row["contract_fingerprint"]) != CONTRACT_FINGERPRINT
            or str(row["user_scope"]) != self.user_scope
            or reservation_count < 0
            or not isinstance(reservations_head, str)
            or _TOKEN.fullmatch(reservations_head) is None
        ):
            raise ProjectExecutionIntegrityError(
                "The project-execution schema, contract, or user scope changed."
            )
        expected = self._mac(
            "project-execution-metadata-v1",
            [
                schema_version,
                str(row["contract_fingerprint"]),
                str(row["user_scope"]),
                reservation_count,
                reservations_head,
            ],
        )
        if not self._valid_mac(integrity_mac, expected):
            raise ProjectExecutionIntegrityError(
                "The project-execution metadata failed integrity validation."
            )

    def _verify_metadata(self, db: sqlite3.Connection) -> None:
        row = db.execute(
            "SELECT * FROM project_execution_metadata WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise ProjectExecutionIntegrityError(
                "The project-execution metadata is missing."
            )
        self._verify_metadata_row(row)
        reservation_count, reservations_head = self._store_commitment(db)
        if (
            self._db_int(row["reservation_count"]) != reservation_count
            or not self._valid_mac(row["reservations_head"], reservations_head)
        ):
            raise ProjectExecutionIntegrityError(
                "The project-execution reservation set failed integrity validation."
            )

    def _store_commitment(self, db: sqlite3.Connection) -> tuple[int, str]:
        rows = db.execute(
            "SELECT * FROM project_execution_reservations ORDER BY reservation_id"
        ).fetchall()
        leaves: list[list[str]] = []
        for row in rows:
            try:
                expected = self._row_mac(row)
            except (TypeError, ValueError) as exc:
                raise ProjectExecutionIntegrityError(
                    f"Reservation {row['reservation_id']} is malformed."
                ) from exc
            if not self._valid_mac(row["integrity_mac"], expected):
                raise ProjectExecutionIntegrityError(
                    f"Reservation {row['reservation_id']} failed integrity validation."
                )
            leaves.append([str(row["reservation_id"]), expected])
        return len(rows), self._mac("project-execution-store-head-v1", leaves)

    def _refresh_metadata_commitment(self, db: sqlite3.Connection) -> None:
        reservation_count, reservations_head = self._store_commitment(db)
        material = self._metadata_material(reservation_count, reservations_head)
        changed = db.execute(
            "UPDATE project_execution_metadata SET reservation_count=?,"
            "reservations_head=?,integrity_mac=? WHERE singleton=1",
            (
                reservation_count,
                reservations_head,
                self._mac("project-execution-metadata-v1", material),
            ),
        )
        if changed.rowcount != 1:
            raise ProjectExecutionIntegrityError(
                "The project-execution metadata disappeared during an update."
            )

    @staticmethod
    def _validate_request_id(request_id: str) -> str:
        value = str(request_id or "").strip()
        if not _REQUEST_ID.fullmatch(value):
            raise ProjectExecutionError(
                "request_id must be 1-160 safe characters and may not be truncated."
            )
        return value

    @staticmethod
    def _validate_owner_id(owner_id: str, *, label: str) -> str:
        value = str(owner_id or "").strip()
        if not _OWNER_ID.fullmatch(value):
            raise ProjectExecutionError(
                f"{label} must be 1-200 printable characters."
            )
        return value

    @staticmethod
    def _validate_capability(lease_token: str, generation: int) -> tuple[str, int]:
        token = str(lease_token or "").strip()
        if not _TOKEN.fullmatch(token):
            raise ProjectExecutionTokenError("The reservation capability is invalid.")
        if isinstance(generation, bool) or not isinstance(generation, int):
            raise ProjectExecutionTokenError("The reservation generation is invalid.")
        held_generation = generation
        if held_generation <= 0:
            raise ProjectExecutionTokenError("The reservation generation is invalid.")
        return token, held_generation

    @staticmethod
    def _without_windows_namespace(value: str) -> str:
        folded = value.casefold()
        if folded.startswith("\\\\?\\unc\\"):
            return "\\\\" + value[8:]
        if folded.startswith("\\\\?\\"):
            return value[4:]
        if folded.startswith("\\\\.\\") or folded.startswith("\\??\\"):
            raise ProjectExecutionError(
                "Windows device-namespace project roots are not supported."
            )
        return value

    @classmethod
    def _windows_final_path(cls, candidate: Path) -> str:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.GetFinalPathNameByHandleW.argtypes = (
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        )
        kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.CreateFileW(
            str(candidate),
            0,
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,
            0x02000000,
            None,
        )
        if handle == wintypes.HANDLE(-1).value:
            raise OSError(ctypes.get_last_error(), "CreateFileW failed")
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            length = int(
                kernel32.GetFinalPathNameByHandleW(
                    handle, buffer, len(buffer), 0
                )
            )
            if length <= 0 or length >= len(buffer):
                raise OSError(
                    ctypes.get_last_error(), "GetFinalPathNameByHandleW failed"
                )
            return cls._without_windows_namespace(buffer.value)
        finally:
            kernel32.CloseHandle(handle)

    @classmethod
    def _normalize_one_root(
        cls, root: str | os.PathLike[str]
    ) -> str:
        raw = os.fspath(root)
        if not raw or "\x00" in raw:
            raise ProjectExecutionError("Project roots must be non-empty paths.")
        lexical = cls._without_windows_namespace(str(raw)) if os.name == "nt" else str(raw)
        candidate = Path(lexical).expanduser()
        if not candidate.is_absolute():
            raise ProjectExecutionError("Project roots must be absolute paths.")
        try:
            resolved = candidate.resolve(strict=False)
            ancestor = resolved
            missing_parts: list[str] = []
            while not ancestor.exists():
                if ancestor.parent == ancestor:
                    raise ProjectExecutionError(
                        "A project root has no resolvable directory ancestor."
                    )
                if os.name == "nt" and (
                    ancestor.name.endswith(".") or ancestor.name.endswith(" ")
                ):
                    raise ProjectExecutionError(
                        "Non-existing Windows path components may not end in a dot or space."
                    )
                missing_parts.append(ancestor.name)
                ancestor = ancestor.parent
            if not ancestor.is_dir():
                raise ProjectExecutionError(
                    "A project root's existing ancestor is not a directory."
                )
            final_ancestor = (
                cls._windows_final_path(ancestor) if os.name == "nt" else str(ancestor)
            )
            final = str(Path(final_ancestor).joinpath(*reversed(missing_parts)))
            normalized = os.path.normcase(os.path.normpath(final))
        except (OSError, RuntimeError, ValueError) as exc:
            raise ProjectExecutionError(f"A project root could not be resolved: {raw}") from exc
        return normalized

    @staticmethod
    def _contains(parent: str, child: str) -> bool:
        try:
            return os.path.commonpath((parent, child)) == parent
        except ValueError:
            return False

    @classmethod
    def _overlaps(cls, left: str, right: str) -> bool:
        return cls._contains(left, right) or cls._contains(right, left)

    @classmethod
    def _normalize_roots(
        cls, roots: Sequence[str | os.PathLike[str]]
    ) -> tuple[str, ...]:
        if isinstance(roots, (str, bytes, os.PathLike)):
            raise ProjectExecutionError("roots must be a sequence of absolute paths.")
        values = list(roots)
        if not values or len(values) > _MAX_ROOTS:
            raise ProjectExecutionError(f"A reservation must contain 1-{_MAX_ROOTS} roots.")
        normalized = sorted(
            set(cls._normalize_one_root(root) for root in values),
            key=lambda value: (len(Path(value).parts), len(value), value),
        )
        minimal: list[str] = []
        for value in normalized:
            if any(cls._contains(parent, value) for parent in minimal):
                continue
            minimal.append(value)
        return tuple(sorted(minimal))

    @staticmethod
    def _db_int(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("SQLite value is not an exact integer")
        return value

    @classmethod
    def _stored_roots_are_canonical(cls, roots: Any) -> bool:
        if (
            not isinstance(roots, list)
            or not roots
            or len(roots) > _MAX_ROOTS
            or any(not isinstance(root, str) or not root for root in roots)
        ):
            return False
        try:
            for root in roots:
                lexical = (
                    cls._without_windows_namespace(root)
                    if os.name == "nt"
                    else root
                )
                if lexical != root or not Path(root).is_absolute():
                    return False
                if os.path.normcase(os.path.normpath(root)) != root:
                    return False
            if roots != sorted(set(roots)):
                return False
            return not any(
                cls._contains(parent, child)
                for index, parent in enumerate(roots)
                for child in roots[index + 1 :]
            )
        except (OSError, RuntimeError, ValueError, ProjectExecutionError):
            return False

    @staticmethod
    def _intent_json(intent: Any) -> str:
        try:
            raw = _canonical(intent)
        except (TypeError, ValueError) as exc:
            raise ProjectExecutionError("intent must be JSON serializable.") from exc
        if len(raw) > _MAX_INTENT_BYTES:
            raise ProjectExecutionError("intent exceeds the 1 MiB coordinator limit.")
        return raw.decode("utf-8")

    @staticmethod
    def _intent_digest(intent_json: str, roots: Sequence[str]) -> str:
        material = {
            "contract_fingerprint": CONTRACT_FINGERPRINT,
            "intent": json.loads(intent_json),
            "roots": list(roots),
        }
        return hashlib.sha256(_canonical(material)).hexdigest()

    @staticmethod
    def _row_material(row: sqlite3.Row | dict[str, Any]) -> list[Any]:
        return [
            row["reservation_id"],
            row["request_id"],
            ProjectExecutionCoordinator._db_int(row["schema_version"]),
            row["contract_fingerprint"],
            row["intent_sha256"],
            row["intent_json"],
            row["roots_json"],
            row["state"],
            ProjectExecutionCoordinator._db_int(row["generation"]),
            row["lease_token"],
            row["bind_replay_mac"],
            row["provisional_owner_id"],
            row["durable_owner_id"],
            ProjectExecutionCoordinator._db_int(row["owner_pid"]),
            row["owner_process_token"],
            ProjectExecutionCoordinator._db_int(row["expires_at_ms"]),
            ProjectExecutionCoordinator._db_int(row["created_at_ms"]),
            ProjectExecutionCoordinator._db_int(row["updated_at_ms"]),
            ProjectExecutionCoordinator._db_int(row["released_at_ms"]),
            row["release_reason"],
        ]

    def _row_mac(self, row: sqlite3.Row | dict[str, Any]) -> str:
        return self._mac("project-execution-reservation-v1", self._row_material(row))

    def _decode_row(self, row: sqlite3.Row) -> ProjectExecutionReservation:
        try:
            expected_mac = self._row_mac(row)
        except (TypeError, ValueError) as exc:
            raise ProjectExecutionIntegrityError(
                f"Reservation {row['reservation_id']} is malformed."
            ) from exc
        if not self._valid_mac(row["integrity_mac"], expected_mac):
            raise ProjectExecutionIntegrityError(
                f"Reservation {row['reservation_id']} failed integrity validation."
            )
        try:
            roots_value = json.loads(str(row["roots_json"]))
            intent_value = json.loads(str(row["intent_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProjectExecutionIntegrityError(
                f"Reservation {row['reservation_id']} contains invalid JSON."
            ) from exc
        if (
            self._db_int(row["schema_version"]) != SCHEMA_VERSION
            or str(row["contract_fingerprint"]) != CONTRACT_FINGERPRINT
            or str(row["state"]) not in ALL_STATES
            or not self._stored_roots_are_canonical(roots_value)
            or self._intent_digest(str(row["intent_json"]), roots_value)
            != str(row["intent_sha256"])
            or not _TOKEN.fullmatch(str(row["lease_token"]))
            or (
                str(row["bind_replay_mac"])
                and not _TOKEN.fullmatch(str(row["bind_replay_mac"]))
            )
        ):
            raise ProjectExecutionIntegrityError(
                f"Reservation {row['reservation_id']} violates the coordinator contract."
            )
        # Parsing the intent above is deliberate validation; retain the canonical
        # serialized value rather than exposing mutable caller data.
        del intent_value
        return ProjectExecutionReservation(
            schema_version=self._db_int(row["schema_version"]),
            contract_fingerprint=str(row["contract_fingerprint"]),
            reservation_id=str(row["reservation_id"]),
            request_id=str(row["request_id"]),
            intent_sha256=str(row["intent_sha256"]),
            roots=tuple(roots_value),
            state=str(row["state"]),
            generation=self._db_int(row["generation"]),
            lease_token=str(row["lease_token"]),
            provisional_owner_id=str(row["provisional_owner_id"]),
            durable_owner_id=str(row["durable_owner_id"]),
            owner_pid=self._db_int(row["owner_pid"]),
            owner_process_token=str(row["owner_process_token"]),
            expires_at_ms=self._db_int(row["expires_at_ms"]),
            created_at_ms=self._db_int(row["created_at_ms"]),
            updated_at_ms=self._db_int(row["updated_at_ms"]),
            released_at_ms=self._db_int(row["released_at_ms"]),
            release_reason=str(row["release_reason"]),
        )

    def _provisional_is_stale(
        self, reservation: ProjectExecutionReservation, now_ms: int
    ) -> bool:
        if reservation.state != PROVISIONAL_STATE:
            return False
        if reservation.expires_at_ms > 0 and now_ms >= reservation.expires_at_ms:
            return True
        try:
            return not self._owner_alive(
                reservation.owner_pid, reservation.owner_process_token
            )
        except Exception:
            # An unavailable liveness oracle must not steal ownership.
            return False

    def _write_values(self, db: sqlite3.Connection, values: dict[str, Any]) -> None:
        values = dict(values)
        values["integrity_mac"] = self._row_mac(values)
        columns = tuple(values)
        placeholders = ",".join("?" for _ in columns)
        db.execute(
            f"INSERT INTO project_execution_reservations({','.join(columns)}) "
            f"VALUES({placeholders})",
            tuple(values[column] for column in columns),
        )
        self._refresh_metadata_commitment(db)

    def _replace_row(
        self,
        db: sqlite3.Connection,
        current: sqlite3.Row,
        updates: dict[str, Any],
        *,
        expected_token: str | None = None,
        expected_generation: int | None = None,
        expected_state: str | None = None,
    ) -> sqlite3.Row:
        values = {key: current[key] for key in current.keys() if key != "integrity_mac"}
        values.update(updates)
        integrity_mac = self._row_mac(values)
        assignments = [f"{key}=?" for key in updates]
        assignments.append("integrity_mac=?")
        parameters: list[Any] = [updates[key] for key in updates]
        parameters.append(integrity_mac)
        where = ["reservation_id=?"]
        parameters.append(str(current["reservation_id"]))
        if expected_token is not None:
            where.append("lease_token=?")
            parameters.append(expected_token)
        if expected_generation is not None:
            where.append("generation=?")
            parameters.append(expected_generation)
        if expected_state is not None:
            where.append("state=?")
            parameters.append(expected_state)
        changed = db.execute(
            f"UPDATE project_execution_reservations SET {','.join(assignments)} "
            f"WHERE {' AND '.join(where)}",
            tuple(parameters),
        )
        if changed.rowcount != 1:
            raise ProjectExecutionTokenError(
                "The reservation owner changed before the operation could commit."
            )
        self._refresh_metadata_commitment(db)
        row = db.execute(
            "SELECT * FROM project_execution_reservations WHERE reservation_id=?",
            (str(current["reservation_id"]),),
        ).fetchone()
        assert row is not None
        self._decode_row(row)
        return row

    def _expire_row(
        self, db: sqlite3.Connection, row: sqlite3.Row, now_ms: int
    ) -> sqlite3.Row:
        reservation = self._decode_row(row)
        if reservation.state != PROVISIONAL_STATE:
            return row
        return self._replace_row(
            db,
            row,
            {
                "state": "released",
                "updated_at_ms": now_ms,
                "released_at_ms": now_ms,
                "release_reason": "provisional_expired",
            },
            expected_token=reservation.lease_token,
            expected_generation=reservation.generation,
            expected_state=PROVISIONAL_STATE,
        )

    def _assert_no_overlap(
        self,
        db: sqlite3.Connection,
        roots: Sequence[str],
        now_ms: int,
        *,
        excluding_reservation_id: str = "",
    ) -> None:
        rows = db.execute(
            "SELECT * FROM project_execution_reservations "
            "WHERE state IN ('provisional','running','paused','waiting') "
            "ORDER BY created_at_ms,reservation_id"
        ).fetchall()
        for row in rows:
            reservation = self._decode_row(row)
            if reservation.reservation_id == excluding_reservation_id:
                continue
            if self._provisional_is_stale(reservation, now_ms):
                self._expire_row(db, row, now_ms)
                continue
            if any(
                self._overlaps(requested, held)
                for requested in roots
                for held in reservation.roots
            ):
                raise ProjectExecutionConflict(
                    "The requested project roots overlap an active execution reservation.",
                    reservation_id=reservation.reservation_id,
                    request_id=reservation.request_id,
                    state=reservation.state,
                    roots=reservation.roots,
                )

    def reserve(
        self,
        *,
        request_id: str,
        intent: Any,
        roots: Sequence[str | os.PathLike[str]],
        provisional_owner_id: str,
        owner_pid: int | None = None,
        owner_process_token: str | None = None,
        ttl_ms: int | None = None,
    ) -> ProjectExecutionReservation:
        """Atomically reserve all roots or none of them.

        Repeating the same request and canonical intent returns the original
        reservation.  If its provisional owner was lost or its deadline elapsed,
        the same request may reclaim it with a new generation and capability.
        """

        request = self._validate_request_id(request_id)
        provisional_owner = self._validate_owner_id(
            provisional_owner_id, label="provisional_owner_id"
        )
        canonical_roots = self._normalize_roots(roots)
        canonical_intent = self._intent_json(intent)
        digest = self._intent_digest(canonical_intent, canonical_roots)
        ttl = self._default_ttl_ms if ttl_ms is None else self._validate_ttl(ttl_ms)
        if owner_pid is None:
            pid = os.getpid()
        elif isinstance(owner_pid, bool) or not isinstance(owner_pid, int):
            raise ProjectExecutionError("owner_pid must be an integer.")
        else:
            pid = owner_pid
        if pid < 0:
            raise ProjectExecutionError("owner_pid may not be negative.")
        process_token = (
            _process_token(pid)
            if owner_process_token is None
            else str(owner_process_token)
        )
        now = int(self._clock_ms())

        with self._transaction(write=True) as db:
            existing = db.execute(
                "SELECT * FROM project_execution_reservations WHERE request_id=?",
                (request,),
            ).fetchone()
            if existing is not None:
                held = self._decode_row(existing)
                if not hmac.compare_digest(held.intent_sha256, digest):
                    raise ProjectExecutionIntentConflict(
                        "request_id is already bound to a different project-execution intent."
                    )
                if held.state != PROVISIONAL_STATE or not self._provisional_is_stale(
                    held, now
                ):
                    return held
                self._assert_no_overlap(
                    db,
                    canonical_roots,
                    now,
                    excluding_reservation_id=held.reservation_id,
                )
                next_generation = held.generation + 1
                row = self._replace_row(
                    db,
                    existing,
                    {
                        "state": PROVISIONAL_STATE,
                        "generation": next_generation,
                        "lease_token": secrets.token_hex(32),
                        "bind_replay_mac": "",
                        "provisional_owner_id": provisional_owner,
                        "durable_owner_id": "",
                        "owner_pid": pid,
                        "owner_process_token": process_token,
                        "expires_at_ms": now + ttl,
                        "updated_at_ms": now,
                        "released_at_ms": 0,
                        "release_reason": "",
                    },
                    expected_token=held.lease_token,
                    expected_generation=held.generation,
                    expected_state=PROVISIONAL_STATE,
                )
                return self._decode_row(row)

            self._assert_no_overlap(db, canonical_roots, now)
            values: dict[str, Any] = {
                "reservation_id": uuid.uuid4().hex,
                "request_id": request,
                "schema_version": SCHEMA_VERSION,
                "contract_fingerprint": CONTRACT_FINGERPRINT,
                "intent_sha256": digest,
                "intent_json": canonical_intent,
                "roots_json": _canonical(list(canonical_roots)).decode("utf-8"),
                "state": PROVISIONAL_STATE,
                "generation": 1,
                "lease_token": secrets.token_hex(32),
                "bind_replay_mac": "",
                "provisional_owner_id": provisional_owner,
                "durable_owner_id": "",
                "owner_pid": pid,
                "owner_process_token": process_token,
                "expires_at_ms": now + ttl,
                "created_at_ms": now,
                "updated_at_ms": now,
                "released_at_ms": 0,
                "release_reason": "",
            }
            self._write_values(db, values)
            row = db.execute(
                "SELECT * FROM project_execution_reservations WHERE reservation_id=?",
                (values["reservation_id"],),
            ).fetchone()
            assert row is not None
            return self._decode_row(row)

    def renew_provisional(
        self,
        reservation_id: str,
        lease_token: str,
        generation: int,
        *,
        ttl_ms: int | None = None,
    ) -> ProjectExecutionReservation:
        """Extend a live provisional reservation without changing its capability."""

        token, held_generation = self._validate_capability(lease_token, generation)
        ttl = self._default_ttl_ms if ttl_ms is None else self._validate_ttl(ttl_ms)
        now = int(self._clock_ms())
        with self._transaction(write=True) as db:
            row = self._require_row(db, reservation_id)
            current = self._decode_row(row)
            self._require_capability(current, token, held_generation)
            if current.state != PROVISIONAL_STATE:
                raise ProjectExecutionStateError(
                    "Only a provisional reservation can be renewed."
                )
            if self._provisional_is_stale(current, now):
                raise ProjectExecutionStateError(
                    "The provisional reservation expired before it could be renewed."
                )
            updated = self._replace_row(
                db,
                row,
                {"expires_at_ms": now + ttl, "updated_at_ms": now},
                expected_token=token,
                expected_generation=held_generation,
                expected_state=PROVISIONAL_STATE,
            )
            return self._decode_row(updated)

    def bind(
        self,
        reservation_id: str,
        lease_token: str,
        generation: int,
        *,
        durable_owner_id: str,
    ) -> ProjectExecutionReservation:
        """CAS a live provisional claim to a durable engine/job owner."""

        token, held_generation = self._validate_capability(lease_token, generation)
        durable_owner = self._validate_owner_id(
            durable_owner_id, label="durable_owner_id"
        )
        now = int(self._clock_ms())
        with self._transaction(write=True) as db:
            row = self._require_row(db, reservation_id)
            current = self._decode_row(row)
            if current.state in DURABLE_STATES:
                if current.durable_owner_id != durable_owner:
                    raise ProjectExecutionStateError(
                        "The reservation is already bound to a different durable owner."
                    )
                replay_mac = self._mac(
                    "project-execution-bind-replay-v1",
                    [current.reservation_id, token, held_generation, durable_owner],
                )
                if self._valid_mac(row["bind_replay_mac"], replay_mac):
                    return current
                self._require_capability(current, token, held_generation)
                return current
            self._require_capability(current, token, held_generation)
            if current.state != PROVISIONAL_STATE:
                raise ProjectExecutionStateError(
                    "Only a live provisional reservation can be bound."
                )
            if self._provisional_is_stale(current, now):
                raise ProjectExecutionStateError(
                    "The provisional reservation expired before it could be bound."
                )
            updated = self._replace_row(
                db,
                row,
                {
                    "state": "running",
                    "generation": current.generation + 1,
                    "lease_token": secrets.token_hex(32),
                    "bind_replay_mac": self._mac(
                        "project-execution-bind-replay-v1",
                        [
                            current.reservation_id,
                            token,
                            held_generation,
                            durable_owner,
                        ],
                    ),
                    "durable_owner_id": durable_owner,
                    "expires_at_ms": 0,
                    "updated_at_ms": now,
                },
                expected_token=token,
                expected_generation=held_generation,
                expected_state=PROVISIONAL_STATE,
            )
            return self._decode_row(updated)

    def set_state(
        self,
        reservation_id: str,
        lease_token: str,
        generation: int,
        state: str,
    ) -> ProjectExecutionReservation:
        """Move a bound reservation among running, paused, and waiting."""

        token, held_generation = self._validate_capability(lease_token, generation)
        target = str(state or "").strip().lower()
        if target not in DURABLE_STATES:
            raise ProjectExecutionStateError(
                "A bound reservation state must be running, paused, or waiting."
            )
        now = int(self._clock_ms())
        with self._transaction(write=True) as db:
            row = self._require_row(db, reservation_id)
            current = self._decode_row(row)
            self._require_capability(current, token, held_generation)
            if current.state not in DURABLE_STATES:
                raise ProjectExecutionStateError(
                    "Only a bound reservation can change durable state."
                )
            if current.state == target:
                return current
            updated = self._replace_row(
                db,
                row,
                {"state": target, "updated_at_ms": now},
                expected_token=token,
                expected_generation=held_generation,
                expected_state=current.state,
            )
            return self._decode_row(updated)

    def release(
        self,
        reservation_id: str,
        lease_token: str,
        generation: int,
        *,
        reason: str = "completed",
    ) -> ProjectExecutionReservation:
        """Release exactly the generation and capability supplied by its owner."""

        token, held_generation = self._validate_capability(lease_token, generation)
        release_reason = str(reason or "released").strip()[:500]
        now = int(self._clock_ms())
        with self._transaction(write=True) as db:
            row = self._require_row(db, reservation_id)
            current = self._decode_row(row)
            self._require_capability(current, token, held_generation)
            if current.state == "released":
                return current
            updated = self._replace_row(
                db,
                row,
                {
                    "state": "released",
                    "expires_at_ms": 0,
                    "updated_at_ms": now,
                    "released_at_ms": now,
                    "release_reason": release_reason,
                },
                expected_token=token,
                expected_generation=held_generation,
                expected_state=current.state,
            )
            return self._decode_row(updated)

    @staticmethod
    def _require_row(db: sqlite3.Connection, reservation_id: str) -> sqlite3.Row:
        held_id = str(reservation_id or "").strip()
        row = db.execute(
            "SELECT * FROM project_execution_reservations WHERE reservation_id=?",
            (held_id,),
        ).fetchone()
        if row is None:
            raise ProjectExecutionError("The project-execution reservation was not found.")
        return row

    @staticmethod
    def _require_capability(
        reservation: ProjectExecutionReservation, token: str, generation: int
    ) -> None:
        if reservation.generation != generation or not hmac.compare_digest(
            reservation.lease_token, token
        ):
            raise ProjectExecutionTokenError(
                "A stale reservation capability cannot change the current owner."
            )

    def get(self, reservation_id: str) -> ProjectExecutionReservation:
        with self._transaction(write=False) as db:
            return self._decode_row(self._require_row(db, reservation_id))

    def by_request(self, request_id: str) -> ProjectExecutionReservation | None:
        request = self._validate_request_id(request_id)
        with self._transaction(write=False) as db:
            row = db.execute(
                "SELECT * FROM project_execution_reservations WHERE request_id=?",
                (request,),
            ).fetchone()
            return self._decode_row(row) if row is not None else None

    def list_active(self) -> list[ProjectExecutionReservation]:
        with self._transaction(write=False) as db:
            rows = db.execute(
                "SELECT * FROM project_execution_reservations "
                "WHERE state IN ('provisional','running','paused','waiting') "
                "ORDER BY created_at_ms,reservation_id"
            ).fetchall()
            return [self._decode_row(row) for row in rows]

    def sweep_provisionals(self) -> int:
        """Release only stale provisional rows; durable rows are never swept."""

        now = int(self._clock_ms())
        changed = 0
        with self._transaction(write=True) as db:
            rows = db.execute(
                "SELECT * FROM project_execution_reservations WHERE state='provisional'"
            ).fetchall()
            for row in rows:
                reservation = self._decode_row(row)
                if self._provisional_is_stale(reservation, now):
                    self._expire_row(db, row, now)
                    changed += 1
        return changed


__all__ = [
    "ACTIVE_STATES",
    "CONTRACT_FINGERPRINT",
    "DURABLE_STATES",
    "ProjectExecutionConflict",
    "ProjectExecutionCoordinator",
    "ProjectExecutionError",
    "ProjectExecutionIntegrityError",
    "ProjectExecutionIntentConflict",
    "ProjectExecutionReservation",
    "ProjectExecutionStateError",
    "ProjectExecutionTokenError",
    "SCHEMA_VERSION",
]
