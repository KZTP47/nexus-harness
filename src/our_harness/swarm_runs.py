"""Durable user-scoped command journal for board-chat orchestration."""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Iterator
import uuid

from .config import LoadedConfig
from .models import HarnessError
from .pipeline_runs import _owner_is_alive, _process_token, project_identity
from .redaction import CredentialRedactor, bounded_redacted_text
from .runtime_integrity import atomic_text, mac, quarantine_marker


ACTIVE = {"accepted", "running", "stopping"}
TERMINAL = {
    "complete", "failed", "stopped", "interrupted", "delivery_unknown", "outcome_unknown"
}
INTEGRITY_VERSION = "1"
INTEGRITY_ANCHOR = "swarm-runs.integrity-anchor.json"
MAX_EVENT_PAGE_ROWS = 200
MAX_EVENT_PAGE_BYTES = 256_000
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


class SwarmRunStore:
    def __init__(self, config: LoadedConfig) -> None:
        self.authority = project_identity(config.project_root)
        self.redactor = CredentialRedactor(config)
        self.root = _base()
        project = config.project_root.resolve()
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
            if not row or row["status"] not in ACTIVE or row["effect_status"] not in {"", "acknowledged"}:
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
            if row["effect_status"] == "delivery_unknown":
                raise HarnessError(
                    "A prior provider delivery is uncertain; Nexus will not dispatch another"
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
                "UPDATE runs SET effect_status=?,status=CASE WHEN ? THEN status ELSE 'delivery_unknown' END,updated_ms=? "
                "WHERE run_id=? AND project_authority=?",
                (summary, 1 if accepted else 0, now, run_id, self.authority),
            )
            self._append(db, run_id, status, {"effect_id": effect_id})
            if not accepted:
                self._release_board_lease(db, run_id)

    def finish(self, run_id: str, result: dict[str, Any]) -> None:
        clean_result = self.redactor.value(result)
        with self._tx() as db:
            before = db.execute(
                "SELECT * FROM runs WHERE run_id=? AND project_authority=?",
                (run_id, self.authority),
            ).fetchone()
            self._verify_run(db, before)
            changed = db.execute(
                "UPDATE runs SET status='complete',result_json=?,updated_ms=? "
                "WHERE run_id=? AND status='running' AND effect_status IN ('','acknowledged') "
                "AND checkpoint_ordinal=effect_ordinal AND project_authority=?",
                (_canonical(clean_result), int(time.time()*1000), run_id, self.authority),
            ).rowcount
            if changed != 1:
                raise HarnessError("That Swarm run cannot be completed from its current state")
            self._append(db, run_id, "complete", {"result_saved": True})
            self._release_board_lease(db, run_id)

    def fail(self, run_id: str, message: str, stopped: bool = False) -> None:
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
            status = (
                "delivery_unknown" if row["effect_status"] in {"dispatched", "delivery_unknown"}
                else "outcome_unknown" if int(row["effect_ordinal"]) > int(row["checkpoint_ordinal"])
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
                "SELECT * FROM runs WHERE project_authority=? "
                "AND (run_id=? OR request_id=?)",
                (self.authority, run_id, run_id),
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
            yield

    def get(self, identity: str) -> dict[str, Any]:
        with self._read() as db:
            row = db.execute("SELECT * FROM runs WHERE project_authority=? AND (run_id=? OR request_id=?)", (self.authority, identity, identity)).fetchone()
            if not row:
                raise HarnessError("That Swarm run does not exist")
            self._verify_run(db, row)
            return self._row(row)

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

        logical_key = f"{self.authority}\0{str(conversation_key or '').strip()}"
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


@contextmanager
def provider_effect(config: LoadedConfig, route: str, conversation_key: str, digest: str) -> Iterator[None]:
    current = _CURRENT.get()
    # Durable runs use their already-validated signed store. Ordinary chats use
    # only the shared ephemeral resource-lease boundary; validating every
    # historical run here would turn first-use fan-out into serialized setup.
    store = current[0] if current else _unscoped_store(config)
    run_id = current[1] if current else f"unscoped-{os.getpid()}-{uuid.uuid4().hex}"
    with store.resource(run_id, route, conversation_key):
        effect_id = store.begin_effect(run_id, hashlib.sha256(f"{route}\0{conversation_key}".encode()).hexdigest(), digest) if current else ""
        try:
            yield
        except Exception:
            if current:
                store.finish_effect(run_id, effect_id, False)
            raise
        else:
            if current:
                store.finish_effect(run_id, effect_id, True)
