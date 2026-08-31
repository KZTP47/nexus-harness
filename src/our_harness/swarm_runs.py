"""Durable user-scoped command journal for board-chat orchestration."""

from __future__ import annotations

import contextvars
from contextlib import contextmanager, nullcontext
import hashlib
import base64
import hmac
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Iterator
import uuid

from .config import LoadedConfig
from .models import HarnessError, ProviderOutcomeUnknown
from .pipeline_runs import _owner_is_alive, _process_token, project_identity
from .redaction import CredentialRedactor, bounded_redacted_text
from . import cancellation, user_questions
from .providers.registry import ProviderRegistry
from .runtime_integrity import atomic_text, mac, quarantine_marker


ACTIVE = {"accepted", "running", "stopping"}
TERMINAL = {
    "complete", "failed", "stopped", "interrupted", "delivery_unknown", "outcome_unknown"
}
INTEGRITY_VERSION = "1"
INTEGRITY_ANCHOR = "swarm-runs.integrity-anchor.json"
MAX_EVENT_PAGE_ROWS = 200
MAX_EVENT_PAGE_BYTES = 256_000
MAX_RECOVERY_PROJECTION_BYTES = 256_000
_CURRENT: contextvars.ContextVar[tuple["SwarmRunStore", str] | None] = contextvars.ContextVar(
    "nexus_swarm_run", default=None
)
_UNSCOPED_STORES_LOCK = threading.Lock()
_UNSCOPED_STORES: dict[str, "_ProviderResourceStore"] = {}
_MOST_UNSCOPED_STORES = 64
def _base() -> Path:
    override = os.environ.get("OUR_HARNESS_SWARM_RUN_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        root = Path(local) if local else Path.home() / "AppData" / "Local"
        return (root / "OurHarness" / "swarm-runs").resolve()
    state = os.environ.get("XDG_STATE_HOME", "").strip()
    root = Path(state) if state else Path.home() / ".local" / "state"
    return (root / "our-harness" / "swarm-runs").resolve()


def _records_degraded_provider_result(value: object) -> bool:
    """True only when a saved projection explicitly owns provider failures.

    An uncertain effect may coexist with useful work from other agents.  The
    result is safe to save when it names those failed providers; saving it does
    not reconcile or resend their remote turn, and the per-effect journal keeps
    the uncertainty durable.
    """

    return isinstance(value, dict) and isinstance(value.get("provider_failures"), list) \
        and any(
            isinstance(one, dict) and one.get("outcome_unknown") is True
            for one in value.get("provider_failures", [])
        )


class _ProviderResourceStore:
    """Small cross-process lock boundary for chats outside a durable run.

    Opening a full :class:`SwarmRunStore` verifies every signed historical run.
    That verification belongs at the durable run boundary, but ordinary chat
    fan-out only needs the unsigned, ephemeral physical-resource lease table.
    Keeping this boundary small prevents first-use journal verification from
    delaying every participant in an otherwise parallel ``ask everyone``.
    """

    def __init__(self, config: LoadedConfig) -> None:
        self.root = _base()
        self._validate_location(config)
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "runs.sqlite3"
        with self._tx() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS resources(
                  resource_key TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                  owner_pid INTEGER NOT NULL, owner_token TEXT NOT NULL,
                  acquired_ms INTEGER NOT NULL
                )
            """)

    def _validate_location(self, config: LoadedConfig) -> None:
        project = config.project_root.resolve()
        runtime = self.root.resolve()
        if runtime == project or project in runtime.parents or runtime in project.parents:
            raise HarnessError("Swarm runtime storage must be external to the project authority")

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def resource(
        self, run_id: str, route: str, conversation_key: str,
        timeout: float = 180.0,
    ) -> Iterator[str]:
        key = hashlib.sha256(f"{route}\0{conversation_key}".encode()).hexdigest()
        began = time.monotonic()
        while True:
            with self._tx() as db:
                held = db.execute(
                    "SELECT * FROM resources WHERE resource_key=?", (key,)
                ).fetchone()
                if not held or not _owner_is_alive(
                    int(held["owner_pid"]), str(held["owner_token"])
                ):
                    db.execute(
                        "INSERT OR REPLACE INTO resources(resource_key,run_id,owner_pid,owner_token,acquired_ms) "
                        "VALUES(?,?,?,?,?)",
                        (
                            key, run_id, os.getpid(), _process_token(os.getpid()),
                            int(time.time() * 1000),
                        ),
                    )
                    break
            if time.monotonic() - began >= timeout:
                raise HarnessError("The selected provider conversation is busy in another Swarm run")
            time.sleep(0.05)
        try:
            yield key
        finally:
            with self._tx() as db:
                db.execute(
                    "DELETE FROM resources WHERE resource_key=? AND run_id=? AND owner_pid=? AND owner_token=?",
                    (key, run_id, os.getpid(), _process_token(os.getpid())),
                )


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class _GlobalBoardStore:
    """The one board lease, without opening or migrating execution journals."""

    def __init__(self, config: LoadedConfig) -> None:
        self.root = _base()
        project = config.project_root.resolve()
        runtime = self.root.resolve()
        if runtime == project or project in runtime.parents or runtime in project.parents:
            raise HarnessError("Swarm runtime storage must be external to the project authority")
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "runs.sqlite3"
        self._prepare_board_only()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
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

    @staticmethod
    def _material(row: sqlite3.Row) -> list[Any]:
        return [row[name] for name in (
            "singleton", "generation", "active_run_id", "mutation_owner_pid",
            "mutation_owner_token", "updated_ms",
        )]

    def _verify(self, row: sqlite3.Row | None) -> None:
        if row is None:
            raise HarnessError("The global Swarm board authority is missing.")
        claimed = str(row["integrity_mac"] or "")
        if not claimed or not hmac.compare_digest(
            claimed, mac("swarm-board-authority-v1", self._material(row))
        ):
            quarantine_marker(
                "swarm-board-authority", self.database,
                "The global Swarm board authority failed keyed integrity.",
            )
            raise HarnessError("The global Swarm board authority failed keyed integrity.")

    def _seal(self, db: sqlite3.Connection) -> None:
        row = db.execute("SELECT * FROM board_authority WHERE singleton=1").fetchone()
        if row is None:
            raise HarnessError("The global Swarm board authority is missing.")
        db.execute(
            "UPDATE board_authority SET integrity_mac=? WHERE singleton=1",
            (mac("swarm-board-authority-v1", self._material(row)),),
        )

    def _prepare_board_only(self) -> None:
        """Create/verify only the lease table; never inspect or rewrite runs/events."""

        with self._tx() as db:
            existed = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='board_authority'"
            ).fetchone() is not None
            db.execute("""
                CREATE TABLE IF NOT EXISTS board_authority(
                  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                  generation INTEGER NOT NULL DEFAULT 0,
                  active_run_id TEXT NOT NULL DEFAULT '',
                  mutation_owner_pid INTEGER NOT NULL DEFAULT 0,
                  mutation_owner_token TEXT NOT NULL DEFAULT '',
                  updated_ms INTEGER NOT NULL DEFAULT 0,
                  integrity_mac TEXT NOT NULL DEFAULT ''
                )
            """)
            columns = {str(row[1]) for row in db.execute("PRAGMA table_info(board_authority)")}
            if "integrity_mac" not in columns:
                db.execute(
                    "ALTER TABLE board_authority ADD COLUMN integrity_mac TEXT NOT NULL DEFAULT ''"
                )
            db.execute("INSERT OR IGNORE INTO board_authority(singleton) VALUES(1)")
            row = db.execute("SELECT * FROM board_authority WHERE singleton=1").fetchone()
            anchor_exists = (self.root / INTEGRITY_ANCHOR).exists()
            if anchor_exists and not existed:
                raise HarnessError(
                    "The anchored Swarm journal is missing its global board authority."
                )
            if existed and anchor_exists:
                self._verify(row)
            elif not str(row["integrity_mac"] or ""):
                self._seal(db)

    def _active(self, db: sqlite3.Connection) -> str:
        held = db.execute("SELECT * FROM board_authority WHERE singleton=1").fetchone()
        self._verify(held)
        return str(held["active_run_id"] or "")

    @staticmethod
    def _run_material(row: sqlite3.Row) -> list[Any]:
        return [row[name] for name in (
            "run_id", "request_id", "project_authority", "snapshot_json",
            "snapshot_sha256", "status", "owner_pid", "owner_token",
            "stop_requested", "effect_status", "effect_id", "effect_ordinal",
            "effect_digest", "checkpoint_ordinal", "board_generation",
            "event_count", "event_head", "result_json", "error",
            "created_ms", "updated_ms",
        )]

    def _recover_dead_active_run(self, db: sqlite3.Connection) -> None:
        """Release only a provably dead/terminal compatible board owner.

        This deliberately does not prepare, migrate, or terminalize the full
        execution journal. Invalid authority still needs harmless board edits,
        so this lease boundary reads the exact signed owner row and changes only
        the lightweight board lease.
        """

        run_id = self._active(db)
        if not run_id:
            return
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'"
        ).fetchone() is None:
            return
        columns = {str(row[1]) for row in db.execute("PRAGMA table_info(runs)")}
        required = {
            "run_id", "request_id", "project_authority", "snapshot_json",
            "snapshot_sha256", "status", "owner_pid", "owner_token",
            "stop_requested", "effect_status", "effect_id", "effect_ordinal",
            "effect_digest", "checkpoint_ordinal", "board_generation",
            "event_count", "event_head", "result_json", "error",
            "created_ms", "updated_ms", "integrity_mac",
        }
        if not required.issubset(columns):
            return
        row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return
        expected = mac("swarm-run-v1", self._run_material(row))
        if not row["integrity_mac"] or not hmac.compare_digest(
            str(row["integrity_mac"]), expected
        ):
            raise HarnessError("The active Swarm board run failed keyed integrity.")
        snapshot = str(row["snapshot_json"] or "")
        if hashlib.sha256(snapshot.encode("utf-8")).hexdigest() != str(
            row["snapshot_sha256"]
        ):
            raise HarnessError("The active Swarm board snapshot failed integrity verification.")
        try:
            kind = json.loads(snapshot).get("kind")
        except (AttributeError, TypeError, ValueError) as exc:
            raise HarnessError("The active Swarm board snapshot could not be verified.") from exc
        if kind != "board_order":
            raise HarnessError("The active global board lease does not name a board run.")
        status = str(row["status"] or "")
        if status not in ACTIVE | TERMINAL:
            raise HarnessError("The active Swarm board run has an unknown status.")
        if status not in TERMINAL and _owner_is_alive(
            int(row["owner_pid"]), str(row["owner_token"])
        ):
            return
        db.execute(
            "UPDATE board_authority SET active_run_id='',updated_ms=? "
            "WHERE singleton=1 AND active_run_id=?",
            (int(time.time() * 1000), run_id),
        )
        self._seal(db)

    def pause_reason(self) -> str:
        with self._tx() as db:
            self._recover_dead_active_run(db)
            if self._active(db):
                return (
                    "The board is going, so it cannot be changed until it finishes. "
                    "Press Stop first."
                )
        return ""

    @contextmanager
    def mutation(self) -> Iterator[int]:
        with self._tx() as db:
            self._recover_dead_active_run(db)
            if self._active(db):
                raise HarnessError(
                    "The global Swarm board is running in another Nexus process. "
                    "Stop that exact run before changing the board."
                )
            held = db.execute("SELECT * FROM board_authority WHERE singleton=1").fetchone()
            generation = int(held["generation"]) + 1
            db.execute(
                "UPDATE board_authority SET generation=?,mutation_owner_pid=?,"
                "mutation_owner_token=?,updated_ms=? WHERE singleton=1",
                (generation, os.getpid(), _process_token(os.getpid()), int(time.time() * 1000)),
            )
            self._seal(db)
            try:
                yield generation
            finally:
                db.execute(
                    "UPDATE board_authority SET mutation_owner_pid=0,mutation_owner_token='',"
                    "updated_ms=? WHERE singleton=1",
                    (int(time.time() * 1000),),
                )
                self._seal(db)

    @contextmanager
    def metadata_mutation(self) -> Iterator[None]:
        """Serialize a topology-neutral live-board metadata update.

        A board run owns an immutable topology snapshot. Clearing the name of a
        deleted saved snapshot cannot affect that work, but it must still not
        race a whole-board write after the run finishes.
        """

        with self._tx() as db:
            self._verify(db.execute(
                "SELECT * FROM board_authority WHERE singleton=1"
            ).fetchone())
            yield


class SwarmRunStore:
    def __init__(self, config: LoadedConfig) -> None:
        self.authority = project_identity(config.project_root)
        self._open_storage(config)

    @classmethod
    def for_communication(cls, config: LoadedConfig) -> "SwarmRunStore":
        """Open the durable chat journal without granting project execution.

        Ordinary conversation needs idempotency, provider-effect reconciliation,
        and cross-process chat leases, but it does not execute the checked-out
        project.  Its authority is therefore the canonical local folder path,
        not the mutation authority descriptor that deliberately rejects copied
        projects.  Callers must still use the normal constructor for any mode
        that can run commands or change project files.
        """

        root = config.project_root.resolve(strict=True)
        held = cls.__new__(cls)
        held.authority = "communication-" + hashlib.sha256(
            os.path.normcase(str(root)).encode("utf-8")
        ).hexdigest()
        held._open_storage(config)
        return held

    def _open_storage(self, config: LoadedConfig) -> None:
        self.redactor = CredentialRedactor(config)
        self.root = _base()
        project = config.project_root.resolve()
        # Execution and communication journals deliberately use different
        # authorities, but they still refer to the same physical project's
        # saved chats. Conversation leases therefore need one authority-neutral
        # project scope or a copied-project communication turn could overlap an
        # execution-journal turn for the same chat.
        self.chat_scope = hashlib.sha256(
            os.path.normcase(str(project)).encode("utf-8")
        ).hexdigest()
        runtime = self.root.resolve()
        if runtime == project or project in runtime.parents or runtime in project.parents:
            raise HarnessError("Swarm runtime storage must be external to the project authority")
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "runs.sqlite3"
        self._prepare()
        self.recover_interrupted()

    @property
    def _anchor_path(self) -> Path:
        return self.root / INTEGRITY_ANCHOR

    def _anchor_value(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "database": hashlib.sha256(
                str(self.database.resolve()).encode("utf-8")
            ).hexdigest(),
            "integrity_version": INTEGRITY_VERSION,
        }

    def _anchor_exists_and_is_valid(self) -> bool:
        where = self._anchor_path
        if not where.exists():
            return False
        if where.is_symlink() or not where.is_file():
            raise HarnessError("The Swarm integrity anchor is not a regular file.")
        try:
            held = json.loads(where.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HarnessError("The Swarm integrity anchor cannot be read.") from exc
        value = held.get("value") if isinstance(held, dict) else None
        claimed = held.get("integrity_mac") if isinstance(held, dict) else None
        if value != self._anchor_value() or not hmac.compare_digest(
            str(claimed or ""), mac("swarm-run-anchor-v1", value)
        ):
            raise HarnessError("The Swarm integrity anchor was changed.")
        return True

    def _write_anchor(self) -> None:
        value = self._anchor_value()
        held = {
            "value": value,
            "integrity_mac": mac("swarm-run-anchor-v1", value),
        }
        atomic_text(
            self._anchor_path,
            json.dumps(held, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n",
        )

    def _quarantine(self, reason: str) -> None:
        quarantine_marker("swarm-run-journal", self.database, reason)

    def _integrity_failure(self, reason: str) -> None:
        try:
            self._quarantine(reason)
        finally:
            raise HarnessError(reason)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
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
    def _read(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            # Integrity verification compares the signed run projection with
            # several event/effect rows. In autocommit mode, a concurrent Nexus
            # process could append between those SELECTs, making one legitimate
            # state look like a truncated or reordered journal. A deferred read
            # transaction pins every SELECT to one WAL snapshot without blocking
            # writers.
            connection.execute("BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _prepare(self) -> None:
        # This is one user-scoped database so physical provider resources are
        # serialized across every open project.  Request idempotency is scoped
        # by project authority, however: two independent projects are allowed
        # to receive the same client-generated request ID.
        db = self._connect()
        try:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("BEGIN IMMEDIATE")
            old_runs = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='runs'"
            ).fetchone()
            migrate = False
            old_columns: set[str] = set()
            if old_runs:
                old_columns = {str(row[1]) for row in db.execute("PRAGMA table_info(runs)")}
                for index in db.execute("PRAGMA index_list(runs)").fetchall():
                    if not int(index[2]):
                        continue
                    names = [
                        str(row[2]) for row in db.execute(
                            f'PRAGMA index_info("{str(index[1]).replace(chr(34), chr(34) * 2)}")'
                        ).fetchall()
                    ]
                    if names == ["project_authority", "request_id"]:
                        break
                else:
                    migrate = True
            if migrate:
                if db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='events'"
                ).fetchone():
                    db.execute("ALTER TABLE events RENAME TO events_global_request_legacy")
                db.execute("ALTER TABLE runs RENAME TO runs_global_request_legacy")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS runs(
              run_id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
              project_authority TEXT NOT NULL DEFAULT '',
              snapshot_json TEXT NOT NULL, snapshot_sha256 TEXT NOT NULL,
              status TEXT NOT NULL, owner_pid INTEGER NOT NULL, owner_token TEXT NOT NULL,
              stop_requested INTEGER NOT NULL DEFAULT 0,
              effect_status TEXT NOT NULL DEFAULT '', effect_id TEXT NOT NULL DEFAULT '',
              effect_ordinal INTEGER NOT NULL DEFAULT 0, effect_digest TEXT NOT NULL DEFAULT '',
              checkpoint_ordinal INTEGER NOT NULL DEFAULT 0,
              board_generation INTEGER NOT NULL DEFAULT 0,
              event_count INTEGER NOT NULL DEFAULT 0,
              event_head TEXT NOT NULL DEFAULT '',
              result_json TEXT, error TEXT NOT NULL DEFAULT '',
              created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
              integrity_mac TEXT NOT NULL DEFAULT '',
              UNIQUE(project_authority, request_id)
            );
            CREATE TABLE IF NOT EXISTS events(
              run_id TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL,
              payload_json TEXT NOT NULL, at_ms INTEGER NOT NULL,
              previous_mac TEXT NOT NULL DEFAULT '',
              integrity_mac TEXT NOT NULL DEFAULT '',
              PRIMARY KEY(run_id, seq), FOREIGN KEY(run_id) REFERENCES runs(run_id)
            );
            CREATE INDEX IF NOT EXISTS events_run_kind_seq
              ON events(run_id,kind,seq DESC);
            CREATE TABLE IF NOT EXISTS provider_effects(
              effect_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
              ordinal INTEGER NOT NULL, resource_key TEXT NOT NULL,
              digest TEXT NOT NULL, status TEXT NOT NULL,
              created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
              integrity_mac TEXT NOT NULL DEFAULT '',
              UNIQUE(run_id,ordinal), FOREIGN KEY(run_id) REFERENCES runs(run_id)
            );
            CREATE INDEX IF NOT EXISTS provider_effects_run_status
              ON provider_effects(run_id,status,ordinal);
            CREATE TABLE IF NOT EXISTS resources(
              resource_key TEXT PRIMARY KEY, run_id TEXT NOT NULL,
              owner_pid INTEGER NOT NULL, owner_token TEXT NOT NULL, acquired_ms INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS board_authority(
              singleton INTEGER PRIMARY KEY CHECK(singleton=1),
              generation INTEGER NOT NULL DEFAULT 0,
              active_run_id TEXT NOT NULL DEFAULT '',
              mutation_owner_pid INTEGER NOT NULL DEFAULT 0,
              mutation_owner_token TEXT NOT NULL DEFAULT '',
              updated_ms INTEGER NOT NULL DEFAULT 0,
              integrity_mac TEXT NOT NULL DEFAULT ''
            );
            INSERT OR IGNORE INTO board_authority(singleton) VALUES(1);
            CREATE TABLE IF NOT EXISTS swarm_integrity_meta(
              key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            """)
            current_columns = {str(row[1]) for row in db.execute("PRAGMA table_info(runs)")}
            if "checkpoint_ordinal" not in current_columns:
                db.execute(
                    "ALTER TABLE runs ADD COLUMN checkpoint_ordinal INTEGER NOT NULL DEFAULT 0"
                )
            if "board_generation" not in current_columns:
                db.execute(
                    "ALTER TABLE runs ADD COLUMN board_generation INTEGER NOT NULL DEFAULT 0"
                )
            for name, declaration in (
                ("event_count", "INTEGER NOT NULL DEFAULT 0"),
                ("event_head", "TEXT NOT NULL DEFAULT ''"),
                ("integrity_mac", "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in current_columns:
                    db.execute(f"ALTER TABLE runs ADD COLUMN {name} {declaration}")
            event_columns = {
                str(row[1]) for row in db.execute("PRAGMA table_info(events)")
            }
            for name in ("previous_mac", "integrity_mac"):
                if name not in event_columns:
                    db.execute(
                        f"ALTER TABLE events ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
                    )
            board_columns = {
                str(row[1]) for row in db.execute("PRAGMA table_info(board_authority)")
            }
            if "integrity_mac" not in board_columns:
                db.execute(
                    "ALTER TABLE board_authority ADD COLUMN integrity_mac TEXT NOT NULL DEFAULT ''"
                )
            # Upgrade the former single-effect run journal without losing an
            # in-flight delivery. Old completed runs need no operational rows;
            # the event chain remains their durable history.
            now = int(time.time() * 1000)
            for row in db.execute(
                "SELECT run_id,effect_id,effect_ordinal,effect_digest,effect_status "
                "FROM runs WHERE effect_id<>'' AND status IN ('accepted','running','stopping')"
            ).fetchall():
                if db.execute(
                    "SELECT 1 FROM provider_effects WHERE effect_id=?", (row[1],)
                ).fetchone():
                    continue
                material = [row[1], row[0], row[2], "legacy", row[3], row[4], now, now]
                db.execute(
                    "INSERT INTO provider_effects(effect_id,run_id,ordinal,resource_key,digest,status,created_ms,updated_ms,integrity_mac) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (*material, mac("swarm-provider-effect-v1", material)),
                )
            legacy = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='runs_global_request_legacy'"
            ).fetchone()
            if legacy:
                target = [
                    "run_id", "request_id", "project_authority", "snapshot_json",
                    "snapshot_sha256", "status", "owner_pid", "owner_token",
                    "stop_requested", "effect_status", "effect_id", "effect_ordinal",
                    "effect_digest", "result_json", "error", "created_ms", "updated_ms",
                    "checkpoint_ordinal",
                    "board_generation",
                    "event_count", "event_head", "integrity_mac",
                ]
                source = [
                    column if column in old_columns else
                    "?" if column == "project_authority" else
                    "0" if column in {"stop_requested", "effect_ordinal", "checkpoint_ordinal", "board_generation", "event_count"} else
                    "NULL" if column == "result_json" else "''"
                    for column in target
                ]
                parameters = (self.authority,) if "project_authority" not in old_columns else ()
                db.execute(
                    f"INSERT INTO runs({','.join(target)}) SELECT {','.join(source)} FROM runs_global_request_legacy",
                    parameters,
                )
                old_events = db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='events_global_request_legacy'"
                ).fetchone()
                if old_events:
                    db.execute(
                        "INSERT INTO events(run_id,seq,kind,payload_json,at_ms) "
                        "SELECT run_id,seq,kind,payload_json,at_ms FROM events_global_request_legacy"
                    )
                    db.execute("DROP TABLE events_global_request_legacy")
                db.execute("DROP TABLE runs_global_request_legacy")
            anchored = self._anchor_exists_and_is_valid()
            version = db.execute(
                "SELECT value FROM swarm_integrity_meta WHERE key='version'"
            ).fetchone()
            if anchored and (version is None or str(version[0]) != INTEGRITY_VERSION):
                raise HarnessError("The Swarm integrity metadata is missing or changed.")
            if anchored:
                # Verification happens before any cleanup write. Corrupt or
                # rewritten evidence is quarantined in place, never blessed by
                # an open-time migration.
                self._verify_all(db)
            else:
                # Explicit one-time migration of the unanchored legacy store.
                # Once the external anchor exists, this path can never be used
                # to re-authorize a rewritten journal.
                for row in db.execute(
                    "SELECT run_id,snapshot_json,result_json,error FROM runs"
                ).fetchall():
                    try:
                        snapshot = self.redactor.value(json.loads(row[1]))
                        result = self.redactor.value(json.loads(row[2])) if row[2] else None
                    except (json.JSONDecodeError, TypeError) as exc:
                        raise HarnessError(
                            "The durable Swarm run journal is corrupt; Nexus will not guess at its commands."
                        ) from exc
                    error = bounded_redacted_text(self.redactor, row[3], 65_536)
                    snapshot_json = _canonical(snapshot)
                    db.execute(
                        "UPDATE runs SET snapshot_json=?,snapshot_sha256=?,result_json=?,error=? WHERE run_id=?",
                        (
                            snapshot_json,
                            hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest(),
                            _canonical(result) if result is not None else None,
                            error,
                            row[0],
                        ),
                    )
                for row in db.execute("SELECT run_id,seq,payload_json FROM events").fetchall():
                    try:
                        clean = self.redactor.value(json.loads(row[2]))
                    except (json.JSONDecodeError, TypeError) as exc:
                        raise HarnessError(
                            "The durable Swarm event journal is corrupt; Nexus will not skip evidence."
                        ) from exc
                    db.execute(
                        "UPDATE events SET payload_json=? WHERE run_id=? AND seq=?",
                        (_canonical(clean), row[0], row[1]),
                    )
                self._initialize_integrity(db)
                db.execute(
                    "INSERT OR REPLACE INTO swarm_integrity_meta(key,value) VALUES('version',?)",
                    (INTEGRITY_VERSION,),
                )
            self._reconcile_legacy_marked_web_receipts(db)
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise HarnessError("The durable Swarm run journal failed its authority migration check.")
            db.commit()
            if not anchored:
                self._write_anchor()
            if not anchored:
                # Scrubbing a legacy row is insufficient if SQLite can still
                # recover the old page from WAL or its freelist. Checkpoint and
                # rebuild only during the one-time migration, before serving.
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                db.execute("VACUUM")
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as exc:
            db.rollback()
            try:
                self._quarantine(str(exc))
            except Exception:
                pass
            raise
        finally:
            db.close()

    @staticmethod
    def _legacy_marked_web_receipt(error: str) -> bool:
        """Recognise the one pre-fix relay diagnostic that proves acceptance.

        Older desktop relays could keep ``submission_state=outcome_unknown``
        after the answer poll had found Nexus's unique marker in a newly-added
        provider user turn.  A paired reply and a cleared post-submit Stop state
        make that a known accepted delivery, even when the reply itself later
        timed out.  Keep this parser deliberately narrower than the public
        error prose: it is only a compatibility bridge for already-signed
        journals and must never turn a genuinely ambiguous delivery into one
        that may be retried.
        """

        held = str(error or "")
        if not re.match(
            r"^web:chatgpt-[a-z0-9-]{1,55} has an unreconciled provider turn: "
            r"ChatGPT may have accepted this message, but Nexus could not match "
            r"its marked turn and reply\.",
            held,
        ):
            return False
        match = re.search(r"\[relay diagnostic: ([^\]]+)\]", held)
        if match is None:
            return False
        fields: dict[str, str] = {}
        for item in match.group(1).split(", "):
            key, separator, value = item.partition("=")
            if separator and re.fullmatch(r"[a-z_]+", key):
                fields[key] = value
        try:
            answer_characters = int(fields.get("answer_characters", "0"))
            reply_count = int(fields.get("reply_count", "0"))
            user_count = int(fields.get("user_count", "0"))
        except ValueError:
            return False
        return (
            fields.get("submission_state") == "outcome_unknown"
            and fields.get("marker_found") == "True"
            and fields.get("reply_seen") == "True"
            and fields.get("stop_cleared_after_submission") == "True"
            and answer_characters > 0
            and reply_count > 0
            and user_count > 0
        )

    def _reconcile_legacy_marked_web_receipts(
        self, db: sqlite3.Connection,
    ) -> None:
        """Repair false delivery fences created by the pre-fix web relay.

        This never resends a request.  It only promotes the sole uncertain
        effect of a signed terminal run when that run contains the exact DOM
        receipt evidence above.  Runs with multiple uncertain effects or any
        weaker evidence remain fenced for explicit human reconciliation.
        """

        for run in db.execute(
            "SELECT * FROM runs WHERE status='delivery_unknown' "
            "AND effect_status='delivery_unknown'"
        ).fetchall():
            if not self._legacy_marked_web_receipt(str(run["error"] or "")):
                continue
            effects = db.execute(
                "SELECT * FROM provider_effects WHERE run_id=? ORDER BY ordinal",
                (run["run_id"],),
            ).fetchall()
            # The pre-fix diagnostic did not persist an effect ID.  Only a run
            # with one total effect can therefore prove which signed effect the
            # receipt describes. Multi-provider runs stay fenced.
            if len(effects) != 1:
                continue
            effect = effects[0]
            self._verify_effect(effect)
            if (
                str(effect["status"]) != "delivery_unknown"
                or str(effect["effect_id"]) != str(run["effect_id"])
                or int(effect["ordinal"]) != int(run["effect_ordinal"])
                or str(effect["digest"]) != str(run["effect_digest"])
            ):
                continue
            now = int(time.time() * 1000)
            db.execute(
                "UPDATE provider_effects SET status='acknowledged',updated_ms=? "
                "WHERE effect_id=? AND status='delivery_unknown'",
                (now, effect["effect_id"]),
            )
            self._seal_effect(db, str(effect["effect_id"]))
            remaining = int(db.execute(
                "SELECT COUNT(*) FROM provider_effects WHERE run_id=? "
                "AND status='delivery_unknown'",
                (run["run_id"],),
            ).fetchone()[0])
            if remaining:
                continue
            db.execute(
                "UPDATE runs SET status='failed',effect_status='acknowledged',"
                "checkpoint_ordinal=effect_ordinal,updated_ms=? WHERE run_id=? "
                "AND status='delivery_unknown' AND effect_status='delivery_unknown'",
                (now, run["run_id"]),
            )
            self._seal_run(db, str(run["run_id"]))
            self._append(
                db, str(run["run_id"]), "provider_late_receipt_reconciled", {
                    "effect_id": str(effect["effect_id"]),
                    "basis": "marked_user_turn_and_reply_observed",
                    "automatic_resend": False,
                },
            )

    @staticmethod
    def _run_material(row: sqlite3.Row) -> list[Any]:
        return [row[name] for name in (
            "run_id", "request_id", "project_authority", "snapshot_json",
            "snapshot_sha256", "status", "owner_pid", "owner_token",
            "stop_requested", "effect_status", "effect_id", "effect_ordinal",
            "effect_digest", "checkpoint_ordinal", "board_generation",
            "event_count", "event_head", "result_json", "error",
            "created_ms", "updated_ms",
        )]

    @staticmethod
    def _event_material(row: sqlite3.Row) -> list[Any]:
        return [row[name] for name in (
            "run_id", "seq", "kind", "payload_json", "at_ms", "previous_mac",
        )]

    @staticmethod
    def _board_material(row: sqlite3.Row) -> list[Any]:
        return [row[name] for name in (
            "singleton", "generation", "active_run_id", "mutation_owner_pid",
            "mutation_owner_token", "updated_ms",
        )]

    @staticmethod
    def _effect_material(row: sqlite3.Row) -> list[Any]:
        return [row[name] for name in (
            "effect_id", "run_id", "ordinal", "resource_key", "digest",
            "status", "created_ms", "updated_ms",
        )]

    def _seal_effect(self, db: sqlite3.Connection, effect_id: str) -> None:
        row = db.execute(
            "SELECT * FROM provider_effects WHERE effect_id=?", (effect_id,)
        ).fetchone()
        if row is None:
            raise HarnessError("That provider effect does not exist")
        db.execute(
            "UPDATE provider_effects SET integrity_mac=? WHERE effect_id=?",
            (mac("swarm-provider-effect-v1", self._effect_material(row)), effect_id),
        )

    def _verify_effect(self, row: sqlite3.Row | None) -> None:
        if row is None:
            return
        expected = mac("swarm-provider-effect-v1", self._effect_material(row))
        if not row["integrity_mac"] or not hmac.compare_digest(
            str(row["integrity_mac"]), expected
        ):
            self._integrity_failure(
                "The durable provider-effect journal failed keyed integrity."
            )

    def _seal_run(self, db: sqlite3.Connection, run_id: str) -> None:
        row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise HarnessError("That Swarm run does not exist")
        db.execute(
            "UPDATE runs SET integrity_mac=? WHERE run_id=?",
            (mac("swarm-run-v1", self._run_material(row)), run_id),
        )

    def _seal_board(self, db: sqlite3.Connection) -> None:
        row = db.execute(
            "SELECT * FROM board_authority WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise HarnessError("The global Swarm board authority is missing.")
        db.execute(
            "UPDATE board_authority SET integrity_mac=? WHERE singleton=1",
            (mac("swarm-board-authority-v1", self._board_material(row)),),
        )

    def _verify_event(self, row: sqlite3.Row) -> None:
        expected = mac("swarm-event-v1", self._event_material(row))
        if not row["integrity_mac"] or not hmac.compare_digest(
            str(row["integrity_mac"]), expected
        ):
            self._integrity_failure(
                "The durable Swarm event journal failed keyed integrity."
            )

    def _verify_run(self, db: sqlite3.Connection, row: sqlite3.Row | None) -> None:
        if row is None:
            return
        expected = mac("swarm-run-v1", self._run_material(row))
        if not row["integrity_mac"] or not hmac.compare_digest(
            str(row["integrity_mac"]), expected
        ):
            self._integrity_failure(
                "The durable Swarm run journal failed keyed integrity."
            )
        summary = db.execute(
            "SELECT COUNT(*),COALESCE(MAX(seq),0) FROM events WHERE run_id=?",
            (row["run_id"],),
        ).fetchone()
        count = int(summary[0])
        last = db.execute(
            "SELECT integrity_mac FROM events WHERE run_id=? ORDER BY seq DESC LIMIT 1",
            (row["run_id"],),
        ).fetchone()
        head = str(last[0]) if last else ""
        if (
            count != int(row["event_count"])
            or int(summary[1]) != count
            or head != str(row["event_head"])
        ):
            self._integrity_failure(
                "The durable Swarm event history was truncated or reordered."
            )

    def _verify_board(self, row: sqlite3.Row | None) -> None:
        if row is None:
            raise HarnessError("The global Swarm board authority is missing.")
        expected = mac("swarm-board-authority-v1", self._board_material(row))
        if not row["integrity_mac"] or not hmac.compare_digest(
            str(row["integrity_mac"]), expected
        ):
            self._integrity_failure(
                "The global Swarm board authority failed keyed integrity."
            )

    def _initialize_integrity(self, db: sqlite3.Connection) -> None:
        for run in db.execute("SELECT run_id FROM runs ORDER BY run_id").fetchall():
            run_id = str(run[0])
            previous = ""
            count = 0
            for event in db.execute(
                "SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,)
            ).fetchall():
                count += 1
                if int(event["seq"]) != count:
                    raise HarnessError("The legacy Swarm event journal has a sequence gap.")
                material = [
                    event["run_id"], event["seq"], event["kind"],
                    event["payload_json"], event["at_ms"], previous,
                ]
                event_mac = mac("swarm-event-v1", material)
                db.execute(
                    "UPDATE events SET previous_mac=?,integrity_mac=? WHERE run_id=? AND seq=?",
                    (previous, event_mac, run_id, event["seq"]),
                )
                previous = event_mac
            db.execute(
                "UPDATE runs SET event_count=?,event_head=? WHERE run_id=?",
                (count, previous, run_id),
            )
            self._seal_run(db, run_id)
        self._seal_board(db)

    def _verify_all(self, db: sqlite3.Connection) -> None:
        self._verify_board(db.execute(
            "SELECT * FROM board_authority WHERE singleton=1"
        ).fetchone())
        for run in db.execute("SELECT * FROM runs").fetchall():
            self._verify_run(db, run)
        previous_by_run: dict[str, str] = {}
        expected_by_run: dict[str, int] = {}
        for event in db.execute("SELECT * FROM events ORDER BY run_id,seq").fetchall():
            self._verify_event(event)
            run_id = str(event["run_id"])
            expected = expected_by_run.get(run_id, 0) + 1
            if (
                int(event["seq"]) != expected
                or str(event["previous_mac"]) != previous_by_run.get(run_id, "")
            ):
                self._integrity_failure(
                    "The durable Swarm event chain is discontinuous."
                )
            expected_by_run[run_id] = expected
            previous_by_run[run_id] = str(event["integrity_mac"])
        for effect in db.execute(
            "SELECT * FROM provider_effects ORDER BY run_id,ordinal"
        ).fetchall():
            self._verify_effect(effect)

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["snapshot"] = json.loads(value.pop("snapshot_json"))
        result = value.pop("result_json")
        value["result"] = json.loads(result) if result else None
        value["stop_requested"] = bool(value["stop_requested"])
        value.pop("integrity_mac", None)
        value.pop("event_head", None)
        status = str(value.get("status") or "")
        if status == "interrupted":
            value["resumable"] = False
            value["recovery_action"] = "start_over"
        elif status in {"delivery_unknown", "outcome_unknown"}:
            value["resumable"] = False
            value["recovery_action"] = "reconcile"
        return value

    def _append(self, db: sqlite3.Connection, run_id: str, kind: str, payload: object) -> int:
        run = db.execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if run is None:
            raise HarnessError("That Swarm run does not exist")
        seq = int(run["event_count"]) + 1
        previous = str(run["event_head"] or "")
        kind = self.redactor.text(str(kind or "event"))[:80]
        payload_json = _canonical(self.redactor.value(payload))
        at_ms = int(time.time() * 1000)
        event_mac = mac(
            "swarm-event-v1",
            [run_id, seq, kind, payload_json, at_ms, previous],
        )
        db.execute(
            "INSERT INTO events(run_id,seq,kind,payload_json,at_ms,previous_mac,integrity_mac) "
            "VALUES(?,?,?,?,?,?,?)",
            (run_id, seq, kind, payload_json, at_ms, previous, event_mac),
        )
        db.execute(
            "UPDATE runs SET event_count=?,event_head=? WHERE run_id=?",
            (seq, event_mac, run_id),
        )
        self._seal_run(db, run_id)
        return seq

    def _release_board_lease(self, db: sqlite3.Connection, run_id: str) -> None:
        db.execute(
            "UPDATE board_authority SET active_run_id='',updated_ms=? "
            "WHERE singleton=1 AND active_run_id=?",
            (int(time.time() * 1000), run_id),
        )
        self._seal_board(db)

    def _recover_dead_board_lease(self, db: sqlite3.Connection) -> None:
        held = db.execute(
            "SELECT * FROM board_authority WHERE singleton=1"
        ).fetchone()
        self._verify_board(held)
        run_id = str(held["active_run_id"] or "") if held else ""
        if not run_id:
            return
        row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        self._verify_run(db, row)
        if row is None or row["status"] in TERMINAL:
            self._release_board_lease(db, run_id)
            return
        if _owner_is_alive(int(row["owner_pid"]), str(row["owner_token"])):
            return
        status = (
            "delivery_unknown" if row["effect_status"] == "dispatched"
            else "outcome_unknown"
            if int(row["effect_ordinal"]) > int(row["checkpoint_ordinal"])
            else "interrupted"
        )
        message = (
            "Nexus restarted while a provider outcome was uncertain; it will not resend automatically."
            if status != "interrupted"
            else (
                "Nexus restarted between durable checkpoints, but this run does not contain a complete "
                "step-level continuation program. It is terminal: start a new request. Nexus will not "
                "automatically resend any provider turn."
            )
        )
        db.execute(
            "UPDATE runs SET status=?,error=?,updated_ms=? WHERE run_id=?",
            (status, message, int(time.time() * 1000), run_id),
        )
        self._append(db, run_id, status, {"automatic_resend": False})
        self._release_board_lease(db, run_id)

    @contextmanager
    def board_mutation(self) -> Iterator[int]:
        """Hold the global board authority while one atomic board write lands."""

        with self._tx() as db:
            self._recover_dead_board_lease(db)
            held = db.execute(
                "SELECT * FROM board_authority WHERE singleton=1"
            ).fetchone()
            self._verify_board(held)
            if held and str(held["active_run_id"] or ""):
                raise HarnessError(
                    "The global Swarm board is running in another Nexus process. Stop that exact run before changing the board."
                )
            generation = int(held["generation"] if held else 0) + 1
            db.execute(
                "UPDATE board_authority SET generation=?,mutation_owner_pid=?,"
                "mutation_owner_token=?,updated_ms=? WHERE singleton=1",
                (generation, os.getpid(), _process_token(os.getpid()), int(time.time() * 1000)),
            )
            self._seal_board(db)
            try:
                yield generation
            finally:
                db.execute(
                    "UPDATE board_authority SET mutation_owner_pid=0,mutation_owner_token='',updated_ms=? "
                    "WHERE singleton=1",
                    (int(time.time() * 1000),),
                )
                self._seal_board(db)

    def board_change_pause_reason(self) -> str:
        """Why the shared board cannot change, independent of project identity."""

        with self._tx() as db:
            self._recover_dead_board_lease(db)
            held = db.execute(
                "SELECT * FROM board_authority WHERE singleton=1"
            ).fetchone()
            self._verify_board(held)
            if held and str(held["active_run_id"] or ""):
                return (
                    "The board is going, so it cannot be changed until it finishes. "
                    "Press Stop first."
                )
        return ""

    def accept(self, request_id: str, snapshot: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        request = str(request_id or "").strip()
        if not request or len(request) > 160:
            raise HarnessError("A stable request ID is required for a Swarm run")
        clean_snapshot = self.redactor.value(snapshot)
        canonical_snapshot = _canonical(clean_snapshot)
        digest = hashlib.sha256(canonical_snapshot.encode("utf-8")).hexdigest()
        now = int(time.time() * 1000)
        with self._tx() as db:
            existing = db.execute(
                "SELECT * FROM runs WHERE project_authority=? AND request_id=?",
                (self.authority, request),
            ).fetchone()
            if existing:
                self._verify_run(db, existing)
                if existing["snapshot_sha256"] != digest:
                    raise HarnessError("That request ID already belongs to a different immutable Swarm command")
                return self._row(existing), False
            if clean_snapshot.get("kind") == "board_order":
                self._recover_dead_board_lease(db)
                authority = db.execute(
                    "SELECT * FROM board_authority WHERE singleton=1"
                ).fetchone()
                self._verify_board(authority)
                if authority and str(authority["active_run_id"] or ""):
                    raise HarnessError(
                        "The global Swarm board is already running in another Nexus process."
                    )
            run_id = uuid.uuid4().hex
            board_generation = 0
            if clean_snapshot.get("kind") == "board_order":
                board_generation = int(authority["generation"] if authority else 0) + 1
            db.execute(
                "INSERT INTO runs(run_id,request_id,project_authority,snapshot_json,snapshot_sha256,status,owner_pid,owner_token,board_generation,created_ms,updated_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, request, self.authority, canonical_snapshot, digest, "accepted", os.getpid(), _process_token(os.getpid()), board_generation, now, now),
            )
            if board_generation:
                db.execute(
                    "UPDATE board_authority SET generation=?,active_run_id=?,updated_ms=? WHERE singleton=1",
                    (board_generation, run_id, now),
                )
                self._seal_board(db)
            self._append(db, run_id, "accepted", {"request_id": request, "snapshot_sha256": digest})
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            return self._row(row), True

    def start(self, run_id: str) -> dict[str, Any]:
        with self._tx() as db:
            before = db.execute(
                "SELECT * FROM runs WHERE run_id=? AND project_authority=?",
                (run_id, self.authority),
            ).fetchone()
            self._verify_run(db, before)
            now = int(time.time() * 1000)
            changed = db.execute(
                "UPDATE runs SET status='running',owner_pid=?,owner_token=?,updated_ms=? WHERE run_id=? AND status='accepted' AND project_authority=?",
                (os.getpid(), _process_token(os.getpid()), now, run_id, self.authority),
            ).rowcount
            if changed != 1:
                raise HarnessError("That Swarm run is not accepted or was already started")
            self._append(db, run_id, "started", {"owner_pid": os.getpid()})
        return self.get(run_id)

    def event(self, run_id: str, kind: str, payload: object) -> int:
        with self._tx() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=? AND project_authority=?", (run_id, self.authority)).fetchone()
            self._verify_run(db, row)
            if not row or row["status"] not in ACTIVE:
                raise HarnessError("Events can only be appended to this project's active Swarm run")
            seq = self._append(db, run_id, kind, payload)
            db.execute("UPDATE runs SET updated_ms=? WHERE run_id=?", (int(time.time()*1000), run_id))
            self._seal_run(db, run_id)
            return seq

    def checkpoint(self, run_id: str, kind: str, payload: object) -> int:
        """Persist a projection that incorporates every acknowledged effect so far."""

        with self._tx() as db:
            row = db.execute(
                "SELECT * FROM runs "
                "WHERE run_id=? AND project_authority=?",
                (run_id, self.authority),
            ).fetchone()
            self._verify_run(db, row)
            degraded = _records_degraded_provider_result(payload)
            if not row or row["status"] not in ACTIVE or (
                row["effect_status"] not in {"", "acknowledged"}
                and not (row["effect_status"] == "delivery_unknown" and degraded)
            ):
                raise HarnessError(
                    "A Swarm checkpoint requires an active run with no provider delivery in doubt"
                )
            seq = self._append(db, run_id, kind, payload)
            db.execute(
                "UPDATE runs SET checkpoint_ordinal=?,updated_ms=? "
                "WHERE run_id=? AND project_authority=?",
                (int(row["effect_ordinal"]), int(time.time() * 1000), run_id, self.authority),
            )
            self._seal_run(db, run_id)
            return seq

    def begin_effect(self, run_id: str, resource_key: str, digest: str) -> str:
        with self._tx() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=? AND project_authority=?", (run_id, self.authority)).fetchone()
            self._verify_run(db, row)
            if not row or row["status"] != "running":
                raise HarnessError("A provider effect requires this project's running Swarm run")
            prior_unknown = db.execute(
                "SELECT 1 FROM provider_effects "
                "WHERE resource_key=? AND status='delivery_unknown' LIMIT 1",
                (resource_key,),
            ).fetchone()
            if prior_unknown:
                raise HarnessError(
                    "This provider conversation has an uncertain prior delivery; Nexus will not resend to it"
                )
            ordinal = int(row["effect_ordinal"]) + 1
            effect_id = hashlib.sha256(f"{run_id}:{ordinal}:{resource_key}:{digest}".encode()).hexdigest()
            now = int(time.time() * 1000)
            db.execute(
                "INSERT INTO provider_effects(effect_id,run_id,ordinal,resource_key,digest,status,created_ms,updated_ms) "
                "VALUES(?,?,?,?,?,'dispatched',?,?)",
                (effect_id, run_id, ordinal, resource_key, digest, now, now),
            )
            self._seal_effect(db, effect_id)
            db.execute(
                "UPDATE runs SET effect_status='dispatched',effect_id=?,effect_ordinal=?,effect_digest=?,updated_ms=? WHERE run_id=?",
                (effect_id, ordinal, digest, now, run_id),
            )
            self._append(db, run_id, "provider_dispatched", {"effect_id": effect_id, "ordinal": ordinal, "resource": resource_key, "digest": digest})
            return effect_id

    def finish_effect(self, run_id: str, effect_id: str, accepted: bool) -> None:
        status = "acknowledged" if accepted else "delivery_unknown"
        with self._tx() as db:
            before = db.execute(
                "SELECT * FROM runs WHERE run_id=? AND project_authority=?",
                (run_id, self.authority),
            ).fetchone()
            self._verify_run(db, before)
            effect = db.execute(
                "SELECT * FROM provider_effects WHERE effect_id=? AND run_id=?",
                (effect_id, run_id),
            ).fetchone()
            self._verify_effect(effect)
            if effect is None or effect["status"] != "dispatched":
                raise HarnessError("That provider acknowledgement does not own an active Swarm effect")
            now = int(time.time() * 1000)
            changed = db.execute(
                "UPDATE provider_effects SET status=?,updated_ms=? "
                "WHERE effect_id=? AND run_id=? AND status='dispatched'",
                (status, now, effect_id, run_id),
            ).rowcount
            if changed != 1:
                raise HarnessError("That provider acknowledgement does not own the active Swarm effect")
            self._seal_effect(db, effect_id)
            remaining = int(db.execute(
                "SELECT COUNT(*) FROM provider_effects WHERE run_id=? AND status='dispatched'",
                (run_id,),
            ).fetchone()[0])
            uncertain = int(db.execute(
                "SELECT COUNT(*) FROM provider_effects WHERE run_id=? AND status='delivery_unknown'",
                (run_id,),
            ).fetchone()[0])
            summary = "delivery_unknown" if uncertain else (
                "dispatched" if remaining else "acknowledged"
            )
            db.execute(
                "UPDATE runs SET effect_status=?,updated_ms=? "
                "WHERE run_id=? AND project_authority=?",
                (summary, now, run_id, self.authority),
            )
            self._append(db, run_id, status, {"effect_id": effect_id})

    def finish(self, run_id: str, result: dict[str, Any]) -> None:
        clean_result = self.redactor.value(result)
        # ``resume_token`` is an opaque collaboration-ledger session ID, not a
        # provider/API credential. The generic credential redactor quite
        # correctly hides keys containing "token", so restore this one tightly
        # validated recovery identity after redaction; otherwise a desktop
        # restart irreversibly turns every resumable run into "[REDACTED]".
        resume_token = result.get("resume_token")
        if (
            isinstance(clean_result, dict)
            and isinstance(resume_token, str)
            and re.fullmatch(r"[A-Za-z0-9_-]{8,128}", resume_token)
        ):
            clean_result["resume_token"] = resume_token
        with self._tx() as db:
            before = db.execute(
                "SELECT * FROM runs WHERE run_id=? AND project_authority=?",
                (run_id, self.authority),
            ).fetchone()
            self._verify_run(db, before)
            degraded = _records_degraded_provider_result(clean_result)
            changed = db.execute(
                "UPDATE runs SET status='complete',result_json=?,updated_ms=? "
                "WHERE run_id=? AND status='running' "
                "AND (effect_status IN ('','acknowledged') OR (effect_status='delivery_unknown' AND ?)) "
                "AND checkpoint_ordinal=effect_ordinal AND project_authority=?",
                (_canonical(clean_result), int(time.time()*1000), run_id,
                 1 if degraded else 0, self.authority),
            ).rowcount
            if changed != 1:
                raise HarnessError("That Swarm run cannot be completed from its current state")
            self._append(db, run_id, "complete", {"result_saved": True})
            self._release_board_lease(db, run_id)

    def fail(
        self,
        run_id: str,
        message: str,
        stopped: bool = False,
        acknowledged_outcome: bool = False,
    ) -> None:
        clean_message = bounded_redacted_text(self.redactor, message, 65_536)
        with self._tx() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=? AND project_authority=?", (run_id, self.authority)).fetchone()
            self._verify_run(db, row)
            if not row:
                return
            if row["status"] in TERMINAL:
                # A provider-effect context records uncertainty first, before
                # the orchestration layer converts the concrete exception to a
                # safe user-facing failure. Preserve that later detail on the
                # already-terminal row; otherwise every delivery_unknown run
                # has an empty error and cannot be diagnosed or judged.
                if clean_message and not str(row["error"] or ""):
                    db.execute(
                        "UPDATE runs SET error=?,updated_ms=? WHERE run_id=? AND project_authority=?",
                        (clean_message, int(time.time() * 1000), run_id, self.authority),
                    )
                    self._append(db, run_id, "failure_detail", {"error": clean_message})
                return
            checkpoint_ordinal = int(row["checkpoint_ordinal"])
            if (
                acknowledged_outcome
                and row["effect_status"] == "acknowledged"
                and int(row["effect_ordinal"]) > checkpoint_ordinal
            ):
                # The provider reply was received, but a later local protocol
                # or validation step failed. Persist that receipt atomically
                # with the failure classification so it cannot be mislabeled
                # outcome_unknown merely because no success checkpoint exists.
                checkpoint_ordinal = int(row["effect_ordinal"])
                self._append(db, run_id, "provider_reply_received_before_failure", {
                    "error": clean_message,
                })
                db.execute(
                    "UPDATE runs SET checkpoint_ordinal=?,updated_ms=? "
                    "WHERE run_id=? AND project_authority=?",
                    (checkpoint_ordinal, int(time.time() * 1000), run_id, self.authority),
                )
            status = (
                "delivery_unknown" if row["effect_status"] in {"dispatched", "delivery_unknown"}
                else "outcome_unknown" if int(row["effect_ordinal"]) > checkpoint_ordinal
                else "stopped" if stopped else "failed"
            )
            changed = db.execute("UPDATE runs SET status=?,error=?,updated_ms=? WHERE run_id=? AND status IN ('accepted','running','stopping') AND project_authority=?", (status, clean_message, int(time.time()*1000), run_id, self.authority)).rowcount
            if changed != 1:
                return
            self._append(db, run_id, status, {"error": clean_message})
            self._release_board_lease(db, run_id)

    def request_stop(self, run_id: str) -> dict[str, Any]:
        with self._tx() as db:
            row = db.execute(
                "SELECT * FROM runs WHERE project_authority=? AND run_id=?",
                (self.authority, run_id),
            ).fetchone()
            if row is None:
                row = db.execute(
                    "SELECT * FROM runs WHERE project_authority=? AND request_id=?",
                    (self.authority, run_id),
                ).fetchone()
            if not row:
                raise HarnessError("That Swarm run does not exist")
            self._verify_run(db, row)
            canonical_run_id = str(row["run_id"])
            if row["status"] not in {"accepted", "running"}:
                pass
            else:
                db.execute("UPDATE runs SET stop_requested=1,status='stopping',updated_ms=? WHERE run_id=?", (int(time.time()*1000), canonical_run_id))
                self._append(db, canonical_run_id, "stop_requested", {})
        return self.get(canonical_run_id)

    def should_stop(self, run_id: str) -> bool:
        with self._read() as db:
            row = db.execute(
                "SELECT * FROM runs WHERE project_authority=? AND run_id=?",
                (self.authority, str(run_id or "")),
            ).fetchone()
            self._verify_run(db, row)
        if not row:
            raise HarnessError("That Swarm run does not exist")
        return bool(row["stop_requested"]) or str(row["status"]) in {"stopping", "stopped"}

    @contextmanager
    def post_provider_mutation(self, run_id: str) -> Iterator[None]:
        """Linearize one post-provider mutation against exact Stop.

        The SQLite write reservation is deliberately held while the external
        mutation lands. Stop uses the same reservation, so either the mutation
        finishes before Stop is accepted or the accepted Stop prevents it from
        starting. There is no check-then-write gap across processes.
        """

        with self._tx() as db:
            row = db.execute(
                "SELECT * FROM runs WHERE run_id=? AND project_authority=?",
                (str(run_id or ""), self.authority),
            ).fetchone()
            self._verify_run(db, row)
            if (
                row is None
                or row["status"] != "running"
                or bool(row["stop_requested"])
            ):
                raise HarnessError(
                    "Stop was accepted before this post-provider mutation; Nexus refused the mutation."
                )
            # The durable reservation above linearizes against Stop requests
            # from every process. This local boundary also covers fail-closed
            # watcher cancellation when the journal itself cannot be polled.
            with cancellation.mutation_boundary():
                yield

    def get(self, identity: str) -> dict[str, Any]:
        with self._read() as db:
            # Exact run IDs are authoritative. A caller-controlled request ID
            # may coincidentally equal another row's generated run ID; a single
            # OR query leaves SQLite free to return the request alias instead.
            row = db.execute(
                "SELECT * FROM runs WHERE project_authority=? AND run_id=?",
                (self.authority, identity),
            ).fetchone()
            if row is None:
                row = db.execute(
                    "SELECT * FROM runs WHERE project_authority=? AND request_id=?",
                    (self.authority, identity),
                ).fetchone()
            if not row:
                raise HarnessError("That Swarm run does not exist")
            self._verify_run(db, row)
            return self._row(row)

    def get_by_request_any_authority(self, request_id: str) -> dict[str, Any]:
        """Find one queue-owned request after the control-panel project changed.

        Board goals may target folders other than the project currently shown
        in the first tab. The queue's random request identity is global, while
        ordinary chat lookup remains scoped to this store's project authority.
        Reconciliation needs this narrow cross-authority read so an ambiguous
        app-close response cannot strand or replay an already verified goal.
        """

        wanted = str(request_id or "").strip()
        if not wanted:
            raise HarnessError("A board-goal work request ID is required")
        with self._read() as db:
            rows = db.execute(
                "SELECT * FROM runs WHERE request_id=? ORDER BY updated_ms DESC LIMIT 2",
                (wanted,),
            ).fetchall()
            for row in rows:
                self._verify_run(db, row)
        if not rows:
            raise HarnessError("That Swarm run does not exist")
        if len(rows) != 1:
            raise HarnessError(
                "That board-goal request identity is ambiguous across project authorities."
            )
        return self._row(rows[0])

    def active(self) -> dict[str, Any] | None:
        """Return this project's current durable command, if one is active.

        Project switching uses this under the same server-side acceptance lock
        as board/chat start.  The signed journal remains the cross-process
        authority, so a command accepted by another Nexus process is visible
        here too.
        """

        with self._read() as db:
            row = db.execute(
                "SELECT * FROM runs WHERE project_authority=? "
                "AND status IN ('accepted','running','stopping') "
                "ORDER BY updated_ms DESC LIMIT 1",
                (self.authority,),
            ).fetchone()
            if row is not None:
                self._verify_run(db, row)
                return self._row(row)
        return None

    def active_runs(self) -> list[dict[str, Any]]:
        """Return every verified active command for this project authority.

        Callers which fence project ownership must inspect the complete active
        set: a newer unrelated chat or work run must not hide an older run that
        still owns the project being admitted.
        """

        with self._read() as db:
            rows = db.execute(
                "SELECT * FROM runs WHERE project_authority=? "
                "AND status IN ('accepted','running','stopping') "
                "ORDER BY updated_ms DESC, run_id DESC",
                (self.authority,),
            ).fetchall()
            for row in rows:
                self._verify_run(db, row)
            return [self._row(row) for row in rows]

    def recoverable_work(self, limit: int = 50) -> dict[str, Any]:
        """Return the newest durable project-work outcome for each pair chat.

        Renderer localStorage is only a convenience cache.  The signed run
        journal is the restart authority, so a new desktop process can rebuild
        recovery cards without replaying a provider turn or exposing the full
        provider transcript through this small inventory endpoint.
        """

        maximum = max(1, min(200, int(limit)))
        with self._read() as db:
            rows = db.execute(
                "SELECT * FROM runs WHERE project_authority=? AND result_json IS NOT NULL "
                "ORDER BY updated_ms DESC LIMIT ?",
                (self.authority, maximum * 10),
            ).fetchall()
            for row in rows:
                self._verify_run(db, row)
        def strings(value: object, maximum_items: int, maximum_chars: int) -> list[str]:
            if not isinstance(value, list):
                return []
            return [
                one[:maximum_chars]
                for one in value[:maximum_items]
                if isinstance(one, str) and one.strip()
            ]

        def budget(value: object) -> dict[str, Any]:
            if not isinstance(value, dict):
                return {}
            projected: dict[str, Any] = {}
            for key in (
                "epoch", "epoch_call_limit", "epoch_calls_used",
                "epoch_calls_remaining", "lifetime_calls_used",
                "absolute_call_limit", "tool_execution_ceiling_seconds",
                "tool_execution_consumed_seconds",
                "tool_execution_remaining_seconds",
            ):
                held = value.get(key)
                if (
                    isinstance(held, (int, float))
                    and not isinstance(held, bool)
                    and math.isfinite(float(held))
                ):
                    projected[key] = held
            if (
                "tool_execution_remaining_seconds" in value
                and value.get("tool_execution_remaining_seconds") is None
            ):
                projected["tool_execution_remaining_seconds"] = None
            if isinstance(value.get("tool_execution_exhausted"), bool):
                projected["tool_execution_exhausted"] = value[
                    "tool_execution_exhausted"
                ]
            for key, bound in (
                ("tool_execution_mode", 32),
                ("tool_execution_accounting", 1_000),
                ("tool_execution_recovery", 1_000),
                ("renewal_policy", 1_000),
                ("summary", 2_000),
            ):
                held = value.get(key)
                if isinstance(held, str):
                    projected[key] = held[:bound]
            return projected

        recoverable = {
            "paused_provider", "paused_for_user", "paused_tool_budget", "incomplete",
            "applied_unverified", "needs_verification",
        }
        seen: set[str] = set()
        found: list[dict[str, Any]] = []
        resolved: list[str] = []
        projection_bytes = len(_canonical({
            "recoveries": [], "resolved_recovery_keys": [],
        }).encode("utf-8"))
        omitted = 0
        for row in rows:
            value = self._row(row)
            snapshot = value.get("snapshot")
            result = value.get("result")
            if not isinstance(snapshot, dict) or not isinstance(result, dict):
                continue
            if str(snapshot.get("requested_mode") or "") != "work":
                continue
            conversation = snapshot.get("conversation")
            conversation = conversation if isinstance(conversation, dict) else {}
            agent_id = str(snapshot.get("agent_id") or "")
            chat_id = str(conversation.get("id") or "legacy")
            key = f"{agent_id}:{chat_id}"
            if not agent_id or key in seen:
                continue
            seen.add(key)
            status = str(result.get("status") or result.get("verification_status") or "")
            token = str(result.get("resume_token") or "")
            if (
                status not in recoverable
                or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", token)
            ):
                resolved.append(key)
                continue
            project = result.get("project")
            project = project if isinstance(project, dict) else {}
            project_id = str(project.get("id") or conversation.get("project") or "")
            project_name = str(project.get("name") or "")
            if not project_name:
                projects = conversation.get("projects")
                if isinstance(projects, list):
                    selected = next((
                        one for one in projects
                        if isinstance(one, dict) and str(one.get("id") or "") == project_id
                    ), None)
                    if isinstance(selected, dict):
                        project_name = str(selected.get("name") or "")
            objective = str(snapshot.get("objective") or "")
            record = {
                "recovery_key": key,
                "agent_id": agent_id,
                "chat_id": chat_id,
                "status": status,
                "resume_token": token,
                "objective": objective[:2_000],
                "objective_truncated": len(objective) > 2_000,
                "allowed_write_roots": strings(
                    result.get("allowed_write_roots"), 24, 240,
                ),
                "write_scope_restricted": bool(result.get("write_scope_restricted")),
                "context_tool_budget": budget(result.get("context_tool_budget")),
                "questions": user_questions.frozen(result.get("questions")),
                "remaining": strings(result.get("remaining"), 12, 500),
                "project": {
                    "id": project_id[:160],
                    "name": (project_name or "the selected project")[:200],
                },
                "updated_ms": int(value.get("updated_ms") or 0),
            }
            encoded = len(_canonical(record).encode("utf-8"))
            if projection_bytes + encoded > MAX_RECOVERY_PROJECTION_BYTES - 4_096:
                omitted += 1
                continue
            found.append(record)
            projection_bytes += encoded
            if len(found) >= maximum:
                break
        bounded_resolved: list[str] = []
        omitted_resolved = 0
        for key in resolved[: maximum * 10]:
            held = key[:360]
            encoded = len(_canonical(held).encode("utf-8"))
            if projection_bytes + encoded > MAX_RECOVERY_PROJECTION_BYTES - 2_048:
                omitted_resolved += 1
                continue
            bounded_resolved.append(held)
            projection_bytes += encoded
        omitted_resolved += max(0, len(resolved) - maximum * 10)
        response = {
            "recoveries": found,
            "resolved_recovery_keys": bounded_resolved,
            "omitted_recoveries": omitted,
            "omitted_resolved_recovery_keys": omitted_resolved,
            "projection_limit_bytes": MAX_RECOVERY_PROJECTION_BYTES,
        }
        response["projection_bytes"] = len(_canonical(response).encode("utf-8"))
        return response

    def projection(self, identity: str, after: int = 0) -> dict[str, Any]:
        run = self.get(identity)
        with self._read() as db:
            maximum = int(db.execute(
                "SELECT COALESCE(MAX(seq),0) FROM events WHERE run_id=?", (run["run_id"],)
            ).fetchone()[0])
            effective_after = min(maximum, max(0, int(after)))
            rows = db.execute(
                "SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
                (run["run_id"], effective_after, MAX_EVENT_PAGE_ROWS + 1),
            ).fetchall()
            events: list[dict[str, Any]] = []
            used = 0
            for row in rows[:MAX_EVENT_PAGE_ROWS]:
                self._verify_event(row)
                event = {
                    "seq": row["seq"], "kind": row["kind"],
                    "payload": json.loads(row["payload_json"]),
                    "at_ms": row["at_ms"],
                }
                encoded = len(_canonical(event).encode("utf-8"))
                if used + encoded > MAX_EVENT_PAGE_BYTES:
                    if not events:
                        # Progress remains available from the exact run/latest
                        # projection. The page advances without returning an
                        # unbounded single payload.
                        event["payload"] = {
                            "projection_truncated": True,
                            "original_bytes": encoded,
                            "sha256": hashlib.sha256(
                                row["payload_json"].encode("utf-8")
                            ).hexdigest(),
                            "recover_with": (
                                f"/api/swarm/event-payload?run_id={run['run_id']}"
                                f"&seq={row['seq']}&offset=0"
                            ),
                            "chunk_encoding": "base64_utf8_json",
                        }
                        events.append(event)
                    break
                events.append(event)
                used += encoded
        next_cursor = int(events[-1]["seq"]) if events else effective_after
        has_more = next_cursor < maximum
        return {
            **run,
            "events": events,
            "cursor": next_cursor,
            "next_cursor": next_cursor,
            "has_more": has_more,
        }

    def event_payload(
        self, identity: str, seq: int, offset: int = 0,
        limit: int = MAX_EVENT_PAGE_BYTES,
    ) -> dict[str, Any]:
        """One exact resumable byte chunk of an oversized canonical event payload."""

        run = self.get(identity)
        start = max(0, int(offset))
        maximum = max(1, min(int(limit), MAX_EVENT_PAGE_BYTES))
        with self._read() as db:
            row = db.execute(
                "SELECT * FROM events WHERE run_id=? AND seq=?",
                (run["run_id"], int(seq)),
            ).fetchone()
            if row is None:
                raise HarnessError("That durable Swarm event does not exist.")
            self._verify_event(row)
            raw = str(row["payload_json"]).encode("utf-8")
        if start > len(raw):
            raise HarnessError("That Swarm event payload offset is past the end.")
        chunk = raw[start:start + maximum]
        end = start + len(chunk)
        return {
            "run_id": run["run_id"],
            "seq": int(seq),
            "offset": start,
            "next_offset": end,
            "total_bytes": len(raw),
            "has_more": end < len(raw),
            "encoding": "base64_utf8_json",
            "payload_base64": base64.b64encode(chunk).decode("ascii"),
            "payload_sha256": hashlib.sha256(raw).hexdigest(),
        }

    def latest_event(self, identity: str, kind: str) -> dict[str, Any] | None:
        run = self.get(identity)
        with self._read() as db:
            row = db.execute(
                "SELECT * FROM events "
                "WHERE run_id=? AND kind=? ORDER BY seq DESC LIMIT 1",
                (run["run_id"], str(kind)),
            ).fetchone()
            if row is not None:
                self._verify_event(row)
        if not row:
            return None
        return {
            "seq": row["seq"], "kind": row["kind"],
            "payload": json.loads(row["payload_json"]), "at_ms": row["at_ms"]
        }

    def recover_interrupted(self) -> None:
        with self._tx() as db:
            for row in db.execute("SELECT * FROM runs WHERE project_authority=? AND status IN ('accepted','running','stopping')", (self.authority,)).fetchall():
                self._verify_run(db, row)
                if _owner_is_alive(int(row["owner_pid"]), str(row["owner_token"])):
                    continue
                status = (
                    "delivery_unknown" if row["effect_status"] == "dispatched"
                    else "outcome_unknown"
                    if int(row["effect_ordinal"]) > int(row["checkpoint_ordinal"])
                    else "interrupted"
                )
                if status == "interrupted":
                    message = (
                        "Nexus restarted between durable checkpoints, but this run does not contain a complete "
                        "step-level continuation program. It is terminal: start a new request. Nexus will not "
                        "automatically resend any provider turn."
                    )
                    recovery = "terminal_start_over"
                else:
                    message = (
                        "Nexus restarted while a provider outcome was uncertain. Inspect or reconcile the "
                        "provider conversation before deciding what to do; Nexus will not resend automatically."
                    )
                    recovery = "reconcile_before_retry"
                db.execute(
                    "UPDATE runs SET status=?,error=?,updated_ms=? WHERE run_id=?",
                    (status, message, int(time.time()*1000), row["run_id"]),
                )
                self._append(db, row["run_id"], status, {
                    "automatic_resend": False, "recovery": recovery,
                })
                self._release_board_lease(db, str(row["run_id"]))

    @contextmanager
    def resource(self, run_id: str, route: str, conversation_key: str, timeout: float = 180.0) -> Iterator[str]:
        key = hashlib.sha256(f"{route}\0{conversation_key}".encode()).hexdigest()
        began = time.monotonic()
        while True:
            with self._tx() as db:
                held = db.execute("SELECT * FROM resources WHERE resource_key=?", (key,)).fetchone()
                if not held or not _owner_is_alive(int(held["owner_pid"]), str(held["owner_token"])):
                    db.execute("INSERT OR REPLACE INTO resources(resource_key,run_id,owner_pid,owner_token,acquired_ms) VALUES(?,?,?,?,?)", (key, run_id, os.getpid(), _process_token(os.getpid()), int(time.time()*1000)))
                    break
            if time.monotonic() - began >= timeout:
                raise HarnessError("The selected provider conversation is busy in another Swarm run")
            time.sleep(0.05)
        try:
            yield key
        finally:
            with self._tx() as db:
                db.execute(
                    "DELETE FROM resources WHERE resource_key=? AND run_id=? AND owner_pid=? AND owner_token=?",
                    (key, run_id, os.getpid(), _process_token(os.getpid())),
                )

    @contextmanager
    def conversation_turn(
        self, run_id: str, conversation_key: str, timeout: float = 0.0,
    ) -> Iterator[str]:
        """Own one logical chat turn across every Nexus server process.

        The renderer and :class:`ChatCancellationRegistry` prevent duplicate
        sends inside one window/process. They cannot protect a conversation
        from a second Nexus server (for example a diagnostic runner) using the
        same project at the same time. Collaboration ledgers deliberately fence
        older generations, so that race used to let a newer request erase the
        user's in-flight objective. Hold a distinct whole-turn lease before any
        collaboration ledger is opened. Provider calls retain their narrower
        route/conversation leases and unrelated chats remain parallel.
        """

        logical_key = f"{self.chat_scope}\0{str(conversation_key or '').strip()}"
        scope = self.resource(
            run_id, "nexus-logical-chat-turn", logical_key, timeout=timeout,
        )
        try:
            resource_key = scope.__enter__()
        except HarnessError as exc:
            if "provider conversation is busy" not in str(exc):
                raise
            raise HarnessError(
                "This chat is already working on another request in a Nexus window or process. "
                "The existing turn was left running; stop it explicitly before starting a replacement."
            ) from exc
        try:
            yield resource_key
        finally:
            scope.__exit__(None, None, None)


@contextmanager
def bind(store: SwarmRunStore, run_id: str) -> Iterator[None]:
    token = _CURRENT.set((store, run_id))
    try:
        yield
    finally:
        _CURRENT.reset(token)


@contextmanager
def post_provider_mutation() -> Iterator[None]:
    """Linearize a mutation for the durable run bound to this worker."""

    current = _CURRENT.get()
    if current is None:
        # Direct unit callers without a durable server run retain the file
        # transaction's own atomicity and fresh-baseline checks.
        yield
        return
    store, run_id = current
    with store.post_provider_mutation(run_id):
        yield


@contextmanager
def global_board_mutation(config: LoadedConfig) -> Iterator[int]:
    """Mutate user-owned board data without granting project execution authority."""

    with _GlobalBoardStore(config).mutation() as generation:
        yield generation


@contextmanager
def global_board_metadata_mutation(config: LoadedConfig) -> Iterator[None]:
    with _GlobalBoardStore(config).metadata_mutation():
        yield


def global_board_change_pause_reason(config: LoadedConfig) -> str:
    return _GlobalBoardStore(config).pause_reason()


def _unscoped_store(config: LoadedConfig) -> _ProviderResourceStore:
    """Reuse a lightweight cross-process resource store for ordinary chats."""

    key = str(_base())
    with _UNSCOPED_STORES_LOCK:
        held = _UNSCOPED_STORES.get(key)
        if held is None:
            if len(_UNSCOPED_STORES) >= _MOST_UNSCOPED_STORES:
                _UNSCOPED_STORES.pop(next(iter(_UNSCOPED_STORES)))
            held = _ProviderResourceStore(config)
            _UNSCOPED_STORES[key] = held
        else:
            held._validate_location(config)
        return held


def _provider_capacity_spec(
    config: LoadedConfig, route: str,
) -> tuple[str, str, int, float] | None:
    """Return the cross-process capacity domain for one configured profile."""

    named = str(route or "").strip()
    if named.startswith("web:"):
        # Electron owns consumer-web-chat concurrency. Each saved provider
        # conversation has its own view and queue there; it is not a trusted
        # ProviderRegistry profile and must not be folded into a CLI slot.
        return None
    project_scope = hashlib.sha256(
        os.path.normcase(str(config.project_root.resolve())).encode("utf-8")
    ).hexdigest()
    if not named:
        # The legacy unnamed route reads the top-level provider directly and
        # has no profile entry. Preserve its conservative single-flight
        # behavior while named profiles use their declared capacity.
        return (
            project_scope, "legacy-default", 1,
            float(config.get("provider.timeout_seconds") or 600),
        )
    registry = ProviderRegistry(config)
    try:
        profile = registry.profile(named)
    except HarnessError as exc:
        if not str(exc).startswith("Unknown provider profile:"):
            raise
        # Provider-effect tests and plugin/legacy callers may supply a route
        # which was resolved outside ProviderRegistry. Unknown capacity is
        # conservative single-flight, never unlimited.
        return (
            project_scope, f"unregistered-{named}", 1,
            float(config.get("provider.timeout_seconds") or 600),
        )
    return (
        project_scope, profile.id, max(1, int(profile.max_concurrency)),
        float(max(1, int(profile.timeout_seconds))),
    )


def _capacity_checkpoint(store: object, run_id: str) -> None:
    cancellation.checkpoint()
    if isinstance(store, SwarmRunStore) and store.should_stop(run_id):
        raise cancellation.ChatCancelled(cancellation.STOPPED_MESSAGE)


@contextmanager
def _provider_capacity_slot(
    store: object, run_id: str, project_scope: str, profile_id: str,
    maximum: int, timeout: float,
) -> Iterator[str]:
    """Claim one configured provider slot using the crash-fenced lease table."""

    count = max(1, min(32, int(maximum)))
    keys = [
        hashlib.sha256(
            f"nexus-provider-profile-capacity-v1\0{project_scope}\0{profile_id}\0{slot}".encode(
                "utf-8"
            )
        ).hexdigest()
        for slot in range(count)
    ]
    began = time.monotonic()
    pid = os.getpid()
    owner_token = _process_token(pid)
    claimed = ""
    while not claimed:
        _capacity_checkpoint(store, run_id)
        # Both SwarmRunStore and its lightweight unscoped counterpart expose
        # the same private transactional resource table boundary.
        with store._tx() as db:  # type: ignore[attr-defined]
            for key in keys:
                held = db.execute(
                    "SELECT * FROM resources WHERE resource_key=?", (key,)
                ).fetchone()
                if held and _owner_is_alive(
                    int(held["owner_pid"]), str(held["owner_token"])
                ):
                    continue
                db.execute(
                    "INSERT OR REPLACE INTO resources"
                    "(resource_key,run_id,owner_pid,owner_token,acquired_ms) "
                    "VALUES(?,?,?,?,?)",
                    (key, run_id, pid, owner_token, int(time.time() * 1000)),
                )
                claimed = key
                break
        if claimed:
            break
        if time.monotonic() - began >= max(1.0, float(timeout)):
            raise HarnessError(
                f"Provider profile {profile_id} is still at its configured capacity "
                f"of {count} concurrent request{'s' if count != 1 else ''}. "
                "The queued chat was left intact; try again after another answer finishes."
            )
        time.sleep(0.05)
    try:
        _capacity_checkpoint(store, run_id)
        yield claimed
    finally:
        with store._tx() as db:  # type: ignore[attr-defined]
            db.execute(
                "DELETE FROM resources WHERE resource_key=? AND run_id=? "
                "AND owner_pid=? AND owner_token=?",
                (claimed, run_id, pid, owner_token),
            )


def _provider_effect_key(route: str, conversation_key: str, digest: str) -> str:
    """Journal uncertainty at the provider's real remote-effect boundary.

    Electron web chats reuse one visible provider-site conversation, so an
    ambiguous send fences that whole conversation until it is reconciled.
    API and command providers ignore ``conversation_key`` and execute one
    stateless request at a time. For those routes, fence only the exact request
    digest: an automatic replay of the uncertain request remains blocked, but
    one interrupted call cannot poison every future turn in the Nexus chat.
    """

    identity = f"{route}\0{conversation_key}"
    if not str(route or "").startswith("web:"):
        identity += f"\0{digest}"
    return hashlib.sha256(identity.encode()).hexdigest()


def _provider_resource_conversation_key(
    route: str, conversation_key: str, digest: str,
) -> str:
    """Serialize only provider requests which share mutable remote state.

    A consumer web route owns one visible conversation, so every turn for that
    conversation must remain single-flight. CLI/API routes are stateless and
    their configured profile capacity is the concurrency authority; including
    the request digest here prevents an unrelated slow request from occupying
    the conversation lease for another agent on the same route.
    """

    if str(route or "").startswith("web:"):
        return str(conversation_key or "")
    return f"{conversation_key}\0{digest}"


@contextmanager
def provider_effect(
    config: LoadedConfig, route: str, conversation_key: str, digest: str,
    *, before_dispatch: Callable[[], None] | None = None,
) -> Iterator[None]:
    current = _CURRENT.get()
    # Durable runs use their already-validated signed store. Ordinary chats use
    # only the shared ephemeral resource-lease boundary; validating every
    # historical run here would turn first-use fan-out into serialized setup.
    store = current[0] if current else _unscoped_store(config)
    run_id = current[1] if current else f"unscoped-{os.getpid()}-{uuid.uuid4().hex}"
    capacity = _provider_capacity_spec(config, route)
    with store.resource(
        run_id, route,
        _provider_resource_conversation_key(route, conversation_key, digest),
    ):
        capacity_scope = (
            _provider_capacity_slot(store, run_id, *capacity)
            if capacity is not None else nullcontext("web")
        )
        with capacity_scope:
            _capacity_checkpoint(store, run_id)
            if before_dispatch is not None:
                before_dispatch()
            _capacity_checkpoint(store, run_id)
            effect_id = store.begin_effect(
                run_id, _provider_effect_key(route, conversation_key, digest), digest
            ) if current else ""
            try:
                yield
                # Stop can win while the provider is returning. Check again
                # before acknowledging its effect or allowing transcript/file
                # mutation so a durable cross-process Stop has a linear edge.
                _capacity_checkpoint(store, run_id)
            except ProviderOutcomeUnknown:
                if current:
                    store.finish_effect(run_id, effect_id, False)
                raise
            except Exception:
                # Returning an explicit exception is itself a terminal receipt. It
                # may be a refusal, provider error, invalid reply, or acknowledged
                # response timeout, but it is not the crash/restart ambiguity that
                # delivery_unknown exists to represent. Providers must use
                # ProviderOutcomeUnknown only when the remote effect really cannot
                # be reconciled. This distinction lets healthy peers keep working.
                if current:
                    store.finish_effect(run_id, effect_id, True)
                raise
            else:
                if current:
                    store.finish_effect(run_id, effect_id, True)
