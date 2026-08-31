"""Durable, server-owned sequencing for the goals written on the Swarm board.

The long-horizon engine already persists each individual project-work session.
This store owns the level above it: which exact board goal is current and which
verified goals must never be dispatched again after a renderer reload or an app
restart.  The live board and pair-chat files remain untouched.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
from typing import Any, Callable
import uuid

from .models import HarnessError
from . import user_questions
from .pipeline_runs import _owner_is_alive, _process_token
from .swarm_runs import _base
from .runtime_integrity import atomic_text, mac, quarantine_marker


ACTIVE = {"queued", "running", "paused"}
SCHEMA_VERSION = 1
ANCHOR_SCHEMA_VERSION = 3
INTEGRITY_ANCHOR = "swarm-goal-queue.integrity-anchor.json"
INTEGRITY_LOCK = "swarm-goal-queue.integrity.lock"
MAX_RETAINED_TERMINAL_QUEUES = 20
_INTEGRITY_THREAD_LOCK = threading.RLock()


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _now() -> int:
    return int(time.time() * 1000)


class SwarmGoalQueueStore:
    """One durable board-wide goal queue, serialized across Nexus processes."""

    def __init__(self, config: Any) -> None:
        self.root = _base()
        project = Path(config.project_root).resolve()
        runtime = self.root.resolve()
        if runtime == project or project in runtime.parents or runtime in project.parents:
            raise HarnessError("Swarm goal-queue storage must be external to project authority")
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "goal-queue.sqlite3"
        with self._integrity_file_lock():
            database = self._connect()
            try:
                database.execute("BEGIN IMMEDIATE")
                queue_table_existed = database.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='goal_queues'"
                ).fetchone() is not None
                integrity_table_existed = database.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='goal_queue_integrity'"
                ).fetchone() is not None
                previous_integrity_columns = {
                    str(row["name"])
                    for row in database.execute("PRAGMA table_info(goal_queue_integrity)").fetchall()
                } if integrity_table_existed else set()
                database.execute("""
                    CREATE TABLE IF NOT EXISTS goal_queues(
                      queue_id TEXT PRIMARY KEY,
                      request_id TEXT NOT NULL UNIQUE,
                      status TEXT NOT NULL,
                      document_json TEXT NOT NULL,
                      document_sha256 TEXT NOT NULL,
                      integrity_mac TEXT NOT NULL,
                      created_ms INTEGER NOT NULL,
                      updated_ms INTEGER NOT NULL
                    )
                """)
                database.execute("""
                    CREATE TABLE IF NOT EXISTS goal_queue_integrity(
                      singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                      revision INTEGER NOT NULL,
                      set_sha256 TEXT NOT NULL,
                      queue_count INTEGER NOT NULL,
                      head_queue_id TEXT NOT NULL,
                      active_queue_id TEXT NOT NULL,
                      public_json TEXT NOT NULL,
                      public_sha256 TEXT NOT NULL,
                      integrity_mac TEXT NOT NULL
                    )
                """)
                for column in ("public_json", "public_sha256"):
                    if integrity_table_existed and column not in previous_integrity_columns:
                        database.execute(
                            f"ALTER TABLE goal_queue_integrity ADD COLUMN {column} "
                            "TEXT NOT NULL DEFAULT ''"
                        )
                self._prepare_anchor(
                    database,
                    queue_table_existed=queue_table_existed,
                    metadata_had_public=(
                        not integrity_table_existed
                        or {"public_json", "public_sha256"}.issubset(
                            previous_integrity_columns
                        )
                    ),
                )
                database.commit()
            except Exception:
                database.rollback()
                raise
            finally:
                database.close()

    @property
    def _anchor(self) -> Path:
        return self.root / INTEGRITY_ANCHOR

    @property
    def _lock_path(self) -> Path:
        return self.root / INTEGRITY_LOCK

    @contextlib.contextmanager
    def _integrity_file_lock(self):
        """Serialize DB commit plus external-anchor publication across processes."""

        acquired_thread = _INTEGRITY_THREAD_LOCK.acquire(timeout=30.0)
        if not acquired_thread:
            raise HarnessError("The durable board-goal queue is busy. Retry shortly.")
        try:
            with self._lock_path.open("a+b") as stream:
                if stream.seek(0, os.SEEK_END) == 0:
                    stream.write(b"\0")
                    stream.flush()
                acquired = False
                deadline = time.monotonic() + 30.0
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
                            if time.monotonic() >= deadline:
                                raise HarnessError(
                                    "The durable board-goal queue is busy. Retry shortly."
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
        finally:
            _INTEGRITY_THREAD_LOCK.release()

    def _legacy_anchor_value(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "database": str(self.database.resolve()),
            "purpose": "durable-server-owned-board-goal-cursor",
        }

    def _state_snapshot(self, database: sqlite3.Connection) -> dict[str, Any]:
        rows = database.execute(
            "SELECT queue_id,request_id,status,document_sha256,"
            "integrity_mac,created_ms,updated_ms FROM goal_queues ORDER BY queue_id"
        ).fetchall()
        records = [[
            str(row["queue_id"]), str(row["request_id"]), str(row["status"]),
            str(row["document_sha256"]), str(row["integrity_mac"]),
            int(row["created_ms"]), int(row["updated_ms"]),
        ] for row in rows]
        head = database.execute(
            "SELECT queue_id FROM goal_queues ORDER BY updated_ms DESC,queue_id DESC LIMIT 1"
        ).fetchone()
        active = database.execute(
            "SELECT queue_id FROM goal_queues WHERE status IN ('queued','running','paused') "
            "ORDER BY updated_ms DESC,queue_id DESC LIMIT 1"
        ).fetchone()
        return {
            "set_sha256": hashlib.sha256(_canonical(records).encode("utf-8")).hexdigest(),
            "queue_count": len(records),
            "head_queue_id": str(head["queue_id"]) if head else "",
            "active_queue_id": str(active["queue_id"]) if active else "",
        }

    @staticmethod
    def _state_value(
        revision: int, snapshot: dict[str, Any], public_sha256: str,
    ) -> dict[str, Any]:
        return {
            "revision": int(revision),
            "set_sha256": str(snapshot["set_sha256"]),
            "queue_count": int(snapshot["queue_count"]),
            "head_queue_id": str(snapshot["head_queue_id"]),
            "active_queue_id": str(snapshot["active_queue_id"]),
            "public_sha256": str(public_sha256),
        }

    def _head_public_payload(
        self, database: sqlite3.Connection,
    ) -> tuple[str, str]:
        row = database.execute(
            "SELECT * FROM goal_queues ORDER BY updated_ms DESC,queue_id DESC LIMIT 1"
        ).fetchone()
        document = self._decode(row)
        raw = _canonical(self._public(document) if document is not None else None)
        return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _anchor_value(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": ANCHOR_SCHEMA_VERSION,
            "queue_schema_version": SCHEMA_VERSION,
            "database": str(self.database.resolve()),
            "purpose": "durable-server-owned-board-goal-cursor",
            **state,
        }

    def _write_anchor(self, state: dict[str, Any]) -> None:
        value = self._anchor_value(state)
        payload = dict(
            value, integrity_mac=mac("swarm-goal-queue-anchor-v3", value),
        )
        atomic_text(self._anchor, json.dumps(payload, indent=2, sort_keys=True) + "\n")

    def _read_anchor(self) -> dict[str, Any]:
        try:
            payload = json.loads(self._anchor.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HarnessError("The board-goal queue integrity anchor cannot be read.") from exc
        if not isinstance(payload, dict):
            raise HarnessError("The board-goal queue integrity anchor was changed.")
        return payload

    def _integrity_failure(self, detail: str) -> None:
        quarantine_marker("swarm-goal-queue", self.database, detail)
        raise HarnessError(
            "The durable board-goal queue failed keyed integrity and monotonic head verification. "
            "Nexus did not forget, roll back, or replay a goal."
        )

    def _metadata_state(self, database: sqlite3.Connection) -> dict[str, Any]:
        row = database.execute(
            "SELECT * FROM goal_queue_integrity WHERE singleton=1"
        ).fetchone()
        if row is None:
            self._integrity_failure("The queue-set integrity record disappeared.")
        state = {
            "revision": int(row["revision"]),
            "set_sha256": str(row["set_sha256"]),
            "queue_count": int(row["queue_count"]),
            "head_queue_id": str(row["head_queue_id"]),
            "active_queue_id": str(row["active_queue_id"]),
            "public_sha256": str(row["public_sha256"]),
        }
        public_raw = str(row["public_json"])
        if (
            state["revision"] < 0
            or not hmac.compare_digest(
                state["public_sha256"],
                hashlib.sha256(public_raw.encode("utf-8")).hexdigest(),
            )
            or not hmac.compare_digest(
                str(row["integrity_mac"]),
                mac("swarm-goal-queue-state-v3", state),
            )
        ):
            self._integrity_failure("The queue-set integrity record failed its keyed MAC.")
        actual = self._state_value(
            state["revision"], self._state_snapshot(database), state["public_sha256"],
        )
        if actual != state:
            self._integrity_failure(
                "The queue row set no longer matches its authenticated head record."
            )
        return state

    def _metadata_public(
        self, database: sqlite3.Connection,
    ) -> dict[str, Any] | None:
        row = database.execute(
            "SELECT public_json,public_sha256 FROM goal_queue_integrity WHERE singleton=1"
        ).fetchone()
        if row is None:
            self._integrity_failure("The authenticated current-goal projection disappeared.")
        raw = str(row["public_json"])
        if not hmac.compare_digest(
            str(row["public_sha256"]),
            hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        ):
            self._integrity_failure("The authenticated current-goal projection was changed.")
        try:
            public = json.loads(raw)
        except (TypeError, ValueError) as exc:
            self._integrity_failure(
                f"The authenticated current-goal projection is unreadable: {exc}"
            )
        if public is not None and (
            not isinstance(public, dict)
            or public.get("schema_version") != SCHEMA_VERSION
            or not isinstance(public.get("queue_id"), str)
            or not isinstance(public.get("status"), str)
        ):
            self._integrity_failure("The authenticated current-goal projection is invalid.")
        return public

    def _metadata_has_v3_mac(self, database: sqlite3.Connection) -> bool:
        row = database.execute(
            "SELECT * FROM goal_queue_integrity WHERE singleton=1"
        ).fetchone()
        if row is None:
            return False
        try:
            state = {
                "revision": int(row["revision"]),
                "set_sha256": str(row["set_sha256"]),
                "queue_count": int(row["queue_count"]),
                "head_queue_id": str(row["head_queue_id"]),
                "active_queue_id": str(row["active_queue_id"]),
                "public_sha256": str(row["public_sha256"]),
            }
        except (IndexError, KeyError, TypeError, ValueError):
            return False
        return hmac.compare_digest(
            str(row["integrity_mac"]), mac("swarm-goal-queue-state-v3", state),
        )

    def _metadata_state_v2(
        self, database: sqlite3.Connection, *, had_public: bool,
    ) -> dict[str, Any]:
        """Verify the exact immediately-prior metadata before upgrading it."""

        row = database.execute(
            "SELECT * FROM goal_queue_integrity WHERE singleton=1"
        ).fetchone()
        if row is None:
            self._integrity_failure("The prior queue-set integrity record disappeared.")
        try:
            state = {
                "revision": int(row["revision"]),
                "set_sha256": str(row["set_sha256"]),
                "queue_count": int(row["queue_count"]),
                "head_queue_id": str(row["head_queue_id"]),
                "active_queue_id": str(row["active_queue_id"]),
            }
            if had_public:
                public_raw = str(row["public_json"])
                state["public_sha256"] = str(row["public_sha256"])
                if not hmac.compare_digest(
                    state["public_sha256"],
                    hashlib.sha256(public_raw.encode("utf-8")).hexdigest(),
                ):
                    self._integrity_failure(
                        "The prior authenticated current-goal projection was changed."
                    )
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            self._integrity_failure(f"The prior queue metadata is malformed: {exc}")
        if not hmac.compare_digest(
            str(row["integrity_mac"]), mac("swarm-goal-queue-state-v2", state),
        ):
            self._integrity_failure("The prior queue-set metadata failed its keyed MAC.")
        actual = {
            "revision": state["revision"],
            **self._state_snapshot(database),
        }
        if had_public:
            actual["public_sha256"] = state["public_sha256"]
        if actual != state:
            self._integrity_failure(
                "The prior queue row set disagrees with its authenticated metadata."
            )
        return state

    def _upgrade_v2_metadata(
        self, database: sqlite3.Connection, *, had_public: bool,
    ) -> dict[str, Any]:
        old = self._metadata_state_v2(database, had_public=had_public)
        if had_public:
            row = database.execute(
                "SELECT public_json,public_sha256 FROM goal_queue_integrity WHERE singleton=1"
            ).fetchone()
            public_raw = str(row["public_json"])
            public_sha = str(row["public_sha256"])
        else:
            public_raw, public_sha = self._head_public_payload(database)
        state = self._state_value(
            int(old["revision"]), self._state_snapshot(database), public_sha,
        )
        database.execute(
            "UPDATE goal_queue_integrity SET public_json=?,public_sha256=?,integrity_mac=? "
            "WHERE singleton=1",
            (
                public_raw, public_sha,
                mac("swarm-goal-queue-state-v3", state),
            ),
        )
        return state

    def _migrate_v2_anchor(
        self, database: sqlite3.Connection, anchor: dict[str, Any], *, had_public: bool,
    ) -> None:
        claimed = str(anchor.get("integrity_mac") or "")
        value = {key: item for key, item in anchor.items() if key != "integrity_mac"}
        if (
            value.get("schema_version") != 2
            or value.get("queue_schema_version") != SCHEMA_VERSION
            or value.get("database") != str(self.database.resolve())
            or value.get("purpose") != "durable-server-owned-board-goal-cursor"
            or not hmac.compare_digest(
                claimed, mac("swarm-goal-queue-anchor-v2", value),
            )
        ):
            self._integrity_failure("The prior external queue-head anchor was changed.")
        anchor_had_public = "public_sha256" in value
        if self._metadata_has_v3_mac(database):
            # The authenticated schema-3 DB commit won the crash race, but the
            # process died before its atomic anchor publication. It is safe to
            # move the still-valid schema-2 anchor forward; never vice versa.
            state = self._metadata_state(database)
            anchored = {
                key: value.get(key) for key in state
                if key != "public_sha256" or anchor_had_public
            }
            expected = {
                key: item for key, item in state.items()
                if key != "public_sha256" or anchor_had_public
            }
            if anchored != expected:
                self._integrity_failure(
                    "The upgraded queue metadata disagrees with its prior monotonic anchor."
                )
            database.commit()
            self._write_anchor(state)
            database.execute("BEGIN IMMEDIATE")
            return
        if anchor_had_public != had_public:
            self._integrity_failure(
                "The prior queue projection schema disagrees with its authenticated anchor."
            )
        old = self._metadata_state_v2(database, had_public=anchor_had_public)
        anchored = {
            key: value.get(key) for key in old
        }
        if old != anchored:
            self._integrity_failure(
                "The prior queue metadata disagrees with its external monotonic anchor."
            )
        state = self._upgrade_v2_metadata(
            database, had_public=anchor_had_public,
        )
        database.commit()
        self._write_anchor(state)
        database.execute("BEGIN IMMEDIATE")

    def _insert_metadata(self, database: sqlite3.Connection, revision: int = 0) -> dict[str, Any]:
        public_raw, public_sha = self._head_public_payload(database)
        state = self._state_value(
            revision, self._state_snapshot(database), public_sha,
        )
        database.execute(
            "INSERT INTO goal_queue_integrity(singleton,revision,set_sha256,queue_count,"
            "head_queue_id,active_queue_id,public_json,public_sha256,integrity_mac) "
            "VALUES(1,?,?,?,?,?,?,?,?)",
            (
                state["revision"], state["set_sha256"], state["queue_count"],
                state["head_queue_id"], state["active_queue_id"],
                public_raw, state["public_sha256"],
                mac("swarm-goal-queue-state-v3", state),
            ),
        )
        return state

    def _advance_metadata(self, database: sqlite3.Connection) -> dict[str, Any]:
        row = database.execute(
            "SELECT * FROM goal_queue_integrity WHERE singleton=1"
        ).fetchone()
        if row is None:
            self._integrity_failure("The queue-set integrity record disappeared during update.")
        previous = {
            "revision": int(row["revision"]),
            "set_sha256": str(row["set_sha256"]),
            "queue_count": int(row["queue_count"]),
            "head_queue_id": str(row["head_queue_id"]),
            "active_queue_id": str(row["active_queue_id"]),
            "public_sha256": str(row["public_sha256"]),
        }
        if not hmac.compare_digest(
            str(row["integrity_mac"]), mac("swarm-goal-queue-state-v3", previous),
        ):
            self._integrity_failure("The queue-set integrity record failed during update.")
        public_raw, public_sha = self._head_public_payload(database)
        state = self._state_value(
            int(previous["revision"]) + 1,
            self._state_snapshot(database), public_sha,
        )
        changed = database.execute(
            "UPDATE goal_queue_integrity SET revision=?,set_sha256=?,queue_count=?,"
            "head_queue_id=?,active_queue_id=?,public_json=?,public_sha256=?,"
            "integrity_mac=? WHERE singleton=1",
            (
                state["revision"], state["set_sha256"], state["queue_count"],
                state["head_queue_id"], state["active_queue_id"],
                public_raw, state["public_sha256"],
                mac("swarm-goal-queue-state-v3", state),
            ),
        ).rowcount
        if changed != 1:
            self._integrity_failure("The queue-set integrity record disappeared during update.")
        return state

    def _verify_anchor_against_database(
        self, database: sqlite3.Connection, anchor: dict[str, Any], *, allow_forward: bool,
    ) -> dict[str, Any]:
        claimed = str(anchor.get("integrity_mac") or "")
        value = {key: item for key, item in anchor.items() if key != "integrity_mac"}
        if (
            value.get("schema_version") != ANCHOR_SCHEMA_VERSION
            or value.get("queue_schema_version") != SCHEMA_VERSION
            or value.get("database") != str(self.database.resolve())
            or value.get("purpose") != "durable-server-owned-board-goal-cursor"
            or not hmac.compare_digest(
                claimed, mac("swarm-goal-queue-anchor-v3", value),
            )
        ):
            self._integrity_failure("The external queue-head anchor failed its keyed MAC.")
        state = self._metadata_state(database)
        anchored_state = {
            key: value.get(key) for key in (
                "revision", "set_sha256", "queue_count", "head_queue_id",
                "active_queue_id", "public_sha256",
            )
        }
        if state == anchored_state:
            return state
        if allow_forward and int(state["revision"]) == int(anchored_state.get("revision", -2)) + 1:
            # The DB commit is durable and keyed, but the process may have died
            # before the external atomic replace. Moving the anchor forward is
            # safe; moving it backward is never accepted.
            self._write_anchor(state)
            return state
        self._integrity_failure(
            "The database revision is behind or disagrees with its external monotonic anchor."
        )

    def _prepare_anchor(
        self, database: sqlite3.Connection, *,
        queue_table_existed: bool, metadata_had_public: bool,
    ) -> None:
        anchor_exists = self._anchor.exists()
        metadata = database.execute(
            "SELECT 1 FROM goal_queue_integrity WHERE singleton=1"
        ).fetchone()
        if not anchor_exists:
            count = int(database.execute("SELECT COUNT(*) FROM goal_queues").fetchone()[0])
            if count:
                self._integrity_failure("The external queue-head anchor disappeared.")
            if metadata is not None:
                if metadata_had_public and self._metadata_has_v3_mac(database):
                    state = self._metadata_state(database)
                else:
                    state = self._upgrade_v2_metadata(
                        database, had_public=metadata_had_public,
                    )
                if state["revision"] != 0 or state["queue_count"] != 0:
                    self._integrity_failure("The external queue-head anchor disappeared.")
            else:
                state = self._insert_metadata(database)
            # The authenticated DB must be durable before its external anchor
            # can name it. A crash can therefore leave DB-ahead, which the
            # next startup repairs; it can never leave an anchor ahead of a
            # rolled-back schema or metadata update.
            database.commit()
            self._write_anchor(state)
            database.execute("BEGIN IMMEDIATE")
            return
        if not queue_table_existed:
            self._integrity_failure("The anchored queue database lost its queue table.")
        anchor = self._read_anchor()
        if anchor.get("schema_version") == SCHEMA_VERSION and "queue_schema_version" not in anchor:
            legacy = dict(anchor)
            claimed = str(legacy.pop("integrity_mac", ""))
            expected = self._legacy_anchor_value()
            if legacy != expected or not hmac.compare_digest(
                claimed, mac("swarm-goal-queue-anchor-v1", expected),
            ):
                self._integrity_failure("The legacy queue anchor failed its keyed MAC.")
            if metadata is None:
                state = self._insert_metadata(database)
            elif metadata_had_public and self._metadata_has_v3_mac(database):
                state = self._metadata_state(database)
            else:
                state = self._upgrade_v2_metadata(
                    database, had_public=metadata_had_public,
                )
            database.commit()
            self._write_anchor(state)
            database.execute("BEGIN IMMEDIATE")
            return
        if metadata is None:
            self._integrity_failure("The anchored queue-set integrity record disappeared.")
        if anchor.get("schema_version") == 2:
            self._migrate_v2_anchor(
                database, anchor, had_public=metadata_had_public,
            )
            return
        self._verify_anchor_against_database(database, anchor, allow_forward=True)

    def _connect(self) -> sqlite3.Connection:
        database = sqlite3.connect(self.database, timeout=30.0, isolation_level=None)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("PRAGMA synchronous=FULL")
        return database

    def _tx(self):
        class Transaction:
            def __init__(inner, outer):
                inner.outer = outer
                inner.lock = None
                inner.database = None
                inner.changes = 0

            def __enter__(inner):
                inner.lock = inner.outer._integrity_file_lock()
                inner.lock.__enter__()
                try:
                    inner.database = inner.outer._connect()
                    inner.database.execute("BEGIN IMMEDIATE")
                    anchor = inner.outer._read_anchor()
                    inner.outer._verify_anchor_against_database(
                        inner.database, anchor, allow_forward=True,
                    )
                    inner.changes = inner.database.total_changes
                    return inner.database
                except Exception:
                    if inner.database is not None:
                        try:
                            inner.database.rollback()
                        finally:
                            inner.database.close()
                    inner.lock.__exit__(*sys.exc_info())
                    raise

            def __exit__(inner, kind, value, trace):
                state = None
                try:
                    if kind:
                        inner.database.rollback()
                    else:
                        if inner.database.total_changes > inner.changes:
                            state = inner.outer._advance_metadata(inner.database)
                        inner.database.commit()
                        if state is not None:
                            inner.outer._write_anchor(state)
                except Exception:
                    inner.database.rollback()
                    raise
                finally:
                    try:
                        inner.database.close()
                    finally:
                        inner.lock.__exit__(kind, value, trace)

        return Transaction(self)

    def _decode(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        raw = str(row["document_json"] or "")
        material = [
            str(row["queue_id"]), str(row["request_id"]), str(row["status"]),
            raw, str(row["document_sha256"]), int(row["created_ms"]),
            int(row["updated_ms"]),
        ]
        invalid = (
            hashlib.sha256(raw.encode("utf-8")).hexdigest()
            != str(row["document_sha256"] or "")
            or not hmac.compare_digest(
                str(row["integrity_mac"] or ""),
                mac("swarm-goal-queue-record-v1", material),
            )
        )
        if invalid:
            quarantine_marker(
                "swarm-goal-queue", self.database,
                "A board-goal queue record failed keyed integrity verification.",
            )
            raise HarnessError(
                "The durable board-goal queue failed keyed integrity verification. "
                "Nexus did not guess which goal was complete."
            )
        try:
            document = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise HarnessError(
                "The durable board-goal queue cannot be read. Nexus did not skip a goal."
            ) from exc
        if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
            raise HarnessError("The durable board-goal queue has an unsupported format.")
        return document

    @staticmethod
    def _write(database: sqlite3.Connection, document: dict[str, Any]) -> None:
        document["updated_ms"] = _now()
        raw = _canonical(document)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        material = [
            str(document["queue_id"]), str(document["request_id"]),
            str(document["status"]), raw, digest,
            int(document["created_ms"]), int(document["updated_ms"]),
        ]
        changed = database.execute(
            "UPDATE goal_queues SET status=?,document_json=?,document_sha256=?,integrity_mac=?,updated_ms=? "
            "WHERE queue_id=?",
            (
                document["status"], raw, digest,
                mac("swarm-goal-queue-record-v1", material),
                document["updated_ms"], document["queue_id"],
            ),
        ).rowcount
        if changed != 1:
            raise HarnessError("The durable board-goal queue disappeared while it was changing.")

    @staticmethod
    def _prune_terminal(database: sqlite3.Connection) -> None:
        terminal = database.execute(
            "SELECT queue_id FROM goal_queues WHERE status IN ('complete','cancelled') "
            "ORDER BY updated_ms DESC"
        ).fetchall()
        for old in terminal[MAX_RETAINED_TERMINAL_QUEUES:]:
            database.execute(
                "DELETE FROM goal_queues WHERE queue_id=?", (str(old["queue_id"]),)
            )

    @staticmethod
    def _public(document: dict[str, Any], *, reused: bool = False) -> dict[str, Any]:
        items = document["items"]
        cursor = int(document["cursor"])
        current = dict(items[cursor]) if cursor < len(items) else None
        return {
            "schema_version": SCHEMA_VERSION,
            "queue_id": document["queue_id"],
            "request_id": document["request_id"],
            "status": document["status"],
            "total": len(items),
            "completed": sum(one["state"] == "complete" for one in items),
            "cursor": cursor,
            "current": current,
            "created_ms": document["created_ms"],
            "updated_ms": document["updated_ms"],
            "reused": reused,
            "note": document.get("note", ""),
        }

    @staticmethod
    def _items(board: dict[str, Any]) -> list[dict[str, Any]]:
        agents = {
            str(one.get("id") or ""): one for one in board.get("agents", [])
            if isinstance(one, dict)
        }
        works: dict[str, list[str]] = {}
        for line in board.get("works_on", []):
            if isinstance(line, dict):
                works.setdefault(str(line.get("project") or ""), []).append(
                    str(line.get("agent") or "")
                )
        talking = {
            frozenset((str(line.get("one") or ""), str(line.get("other") or "")))
            for line in board.get("talks_to", []) if isinstance(line, dict)
        }
        items: list[dict[str, Any]] = []
        blocked: list[str] = []
        for project in board.get("projects", []):
            if not isinstance(project, dict):
                continue
            tasks = [one for one in project.get("tasks", []) if isinstance(one, str) and one]
            if not tasks or not project.get("is_there"):
                continue
            assigned = [
                agents[agent_id] for agent_id in works.get(str(project.get("id") or ""), [])
                if agent_id in agents
                and agents[agent_id].get("ready") is True
                and str(agents[agent_id].get("who") or "")
            ]
            pair = next((
                (lead, peer)
                for lead in assigned for peer in assigned
                if lead is not peer
                and frozenset((str(lead["id"]), str(peer["id"]))) in talking
            ), None)
            if pair is None:
                blocked.append(str(project.get("name") or project.get("path") or "project"))
                continue
            lead, peer = pair
            for objective in tasks:
                ordinal = len(items)
                material = (
                    f"{ordinal}\0{project.get('id')}\0{lead.get('id')}\0"
                    f"{peer.get('id')}\0{objective}"
                )
                items.append({
                    "id": hashlib.sha256(material.encode("utf-8")).hexdigest(),
                    "ordinal": ordinal,
                    "project_id": str(project.get("id") or ""),
                    "project_name": str(project.get("name") or project.get("path") or "project"),
                    "project_path": str(project.get("path") or ""),
                    "lead_id": str(lead.get("id") or ""),
                    "lead_name": str(lead.get("name") or "agent"),
                    "peer_id": str(peer.get("id") or ""),
                    "peer_name": str(peer.get("name") or "agent"),
                    "objective": objective,
                    "state": "queued",
                    "conversation_id": "",
                    "work_request_id": "",
                    "run_id": "",
                    "resume_token": "",
                    "last_error": "",
                    "attempts": 0,
                    "owner_pid": 0,
                    "owner_token": "",
                })
        if blocked:
            raise HarnessError(
                "Connect at least two ready agents who both work on "
                + ", ".join(blocked)
                + ", with a green line between them. No goal was started."
            )
        if not items:
            raise HarnessError("Write at least one goal on an available project first.")
        return items

    def start(self, board: dict[str, Any], request_id: object) -> dict[str, Any]:
        request = str(request_id or "").strip()
        if not request or len(request) > 160:
            raise HarnessError("A stable request ID is required for board-goal work.")
        with self._tx() as database:
            existing = database.execute(
                "SELECT * FROM goal_queues WHERE request_id=?", (request,)
            ).fetchone()
            if existing is not None:
                return self._public(self._decode(existing), reused=True)
            active = database.execute(
                "SELECT * FROM goal_queues WHERE status IN ('queued','running','paused') "
                "ORDER BY updated_ms DESC LIMIT 1"
            ).fetchone()
            if active is not None:
                return self._public(self._recover_dead(database, active), reused=True)
            # Snapshot only after both idempotency lookups. An ambiguous retry
            # belongs to the already frozen queue even if the live board was
            # edited, disconnected, or temporarily unavailable meanwhile.
            items = self._items(board)
            now = _now()
            queue_id = uuid.uuid4().hex
            document = {
                "schema_version": SCHEMA_VERSION,
                "queue_id": queue_id,
                "request_id": request,
                "status": "queued",
                "cursor": 0,
                "items": items,
                "created_ms": now,
                "updated_ms": now,
                "note": "The exact board goals are saved and ready to run.",
            }
            raw = _canonical(document)
            digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            material = [queue_id, request, "queued", raw, digest, now, now]
            database.execute(
                "INSERT INTO goal_queues(queue_id,request_id,status,document_json,"
                "document_sha256,integrity_mac,created_ms,updated_ms) VALUES(?,?,?,?,?,?,?,?)",
                (
                    queue_id, request, "queued", raw, digest,
                    mac("swarm-goal-queue-record-v1", material), now, now,
                ),
            )
            # Keep a deliberate, bounded audit tail. Objectives can be large;
            # retaining every completed queue forever would create an
            # unbounded hidden user-data store. Active/paused work is never
            # pruned, and the live board remains untouched.
            self._prune_terminal(database)
            return self._public(document)

    def _recover_dead(
        self, database: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        document = self._decode(row)
        if document["status"] != "running":
            return document
        current = document["items"][int(document["cursor"])]
        if _owner_is_alive(int(current.get("owner_pid") or 0), str(current.get("owner_token") or "")):
            return document
        current["state"] = "paused"
        current["owner_pid"] = 0
        current["owner_token"] = ""
        current["last_error"] = (
            current.get("last_error")
            or "Nexus closed while this exact goal was active. Its durable work record can be resumed."
        )
        document["status"] = "paused"
        document["note"] = current["last_error"]
        self._write(database, document)
        return document

    def status(self) -> dict[str, Any] | None:
        with self._tx() as database:
            public = self._metadata_public(database)
            if public is None:
                return None
            current = public.get("current")
            if public.get("status") == "running" and isinstance(current, dict):
                if not _owner_is_alive(
                    int(current.get("owner_pid") or 0),
                    str(current.get("owner_token") or ""),
                ):
                    # Only dead-owner recovery needs the complete queue. The
                    # ordinary 1.2-second poll uses the small authenticated
                    # current projection and never reads future objectives.
                    row = database.execute(
                        "SELECT * FROM goal_queues WHERE queue_id=?",
                        (str(public["queue_id"]),),
                    ).fetchone()
                    return self._public(self._recover_dead(database, row))
            return public

    def active_project_paths(self) -> list[str]:
        """Return every unfinished project reserved by an executing legacy queue."""
        with self._tx() as database:
            row = database.execute(
                "SELECT * FROM goal_queues WHERE status IN ('queued','running','paused') "
                "ORDER BY updated_ms DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return []
            document = self._recover_dead(database, row)
            if document["status"] not in {"queued", "running", "paused"}:
                return []
            cursor = int(document["cursor"])
            return list(dict.fromkeys(
                str(one.get("project_path") or "") for one in document["items"][cursor:]
                if str(one.get("project_path") or "")
            ))

    def claim(
        self, queue_id: object, item_id: object, *, objective: str,
        agent_id: str, peer_id: str, project_id: str, conversation_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        with self._tx() as database:
            row = database.execute(
                "SELECT * FROM goal_queues WHERE queue_id=?", (str(queue_id or ""),)
            ).fetchone()
            document = self._recover_dead(database, row) if row else None
            if document is None or document["status"] not in ACTIVE:
                raise HarnessError("That board-goal queue is not active.")
            cursor = int(document["cursor"])
            current = document["items"][cursor]
            expected = {
                "item": (str(item_id or ""), current["id"]),
                "goal": (objective, current["objective"]),
                "lead": (agent_id, current["lead_id"]),
                "peer": (peer_id, current["peer_id"]),
                "project": (project_id, current["project_id"]),
            }
            wrong = [name for name, values in expected.items() if values[0] != values[1]]
            if wrong:
                raise HarnessError(
                    "The board-goal request no longer matches the server's exact current "
                    + ", ".join(wrong) + ". Nexus did not skip or substitute a goal."
                )
            if current["state"] == "complete":
                raise HarnessError("That board goal is already verified complete and will not run twice.")
            if current["state"] == "running":
                if current.get("work_request_id") != request_id:
                    raise HarnessError("That exact board goal is already running. Nexus did not dispatch it twice.")
                # An ambiguous retransmission owns the same durable work
                # request, but it does not replace the original worker's
                # liveness identity. The run journal will return its existing
                # result/state without a second provider dispatch.
                return self._public(document, reused=True)
            previous_request = current.get("work_request_id")
            current.update({
                "state": "running",
                "conversation_id": str(conversation_id or ""),
                "work_request_id": str(request_id or ""),
                "attempts": int(current.get("attempts") or 0) + (
                    0 if previous_request == request_id else 1
                ),
                "owner_pid": os.getpid(),
                "owner_token": _process_token(os.getpid()),
                "last_error": "",
            })
            document["status"] = "running"
            document["note"] = (
                f"Goal {cursor + 1} of {len(document['items'])} is running in its durable pair chat."
            )
            self._write(database, document)
            if document["status"] in {"complete", "cancelled"}:
                self._prune_terminal(database)
            return self._public(document)

    def record_result(
        self, queue_id: object, item_id: object, result: dict[str, Any],
        *, run_id: str = "",
    ) -> dict[str, Any]:
        with self._tx() as database:
            row = database.execute(
                "SELECT * FROM goal_queues WHERE queue_id=?", (str(queue_id or ""),)
            ).fetchone()
            document = self._decode(row)
            if document is None:
                raise HarnessError("That board-goal queue does not exist.")
            cursor = int(document["cursor"])
            if cursor >= len(document["items"]):
                return self._public(document, reused=True)
            current = document["items"][cursor]
            if current["id"] != str(item_id or ""):
                # A late duplicate response for an already advanced item must
                # not advance the new current item.
                if any(
                    one["id"] == str(item_id or "") and one["state"] == "complete"
                    for one in document["items"][:cursor]
                ):
                    return self._public(document, reused=True)
                raise HarnessError("That result is not for the queue's exact current goal.")
            current["run_id"] = str(run_id or result.get("run_id") or "")
            current["resume_token"] = str(result.get("resume_token") or "")
            current["recovery"] = {
                "status": str(result.get("status") or result.get("verification_status") or ""),
                "resume_token": str(result.get("resume_token") or ""),
                "allowed_write_roots": list(result.get("allowed_write_roots") or [])[:20],
                "write_scope_restricted": result.get("write_scope_restricted") is True,
                "context_tool_budget": dict(result.get("context_tool_budget") or {}),
                "questions": user_questions.frozen(result.get("questions")),
                "remaining": [str(one) for one in list(result.get("remaining") or [])[:50]],
                "project": {
                    "id": current["project_id"], "name": current["project_name"],
                },
            }
            current["owner_pid"] = 0
            current["owner_token"] = ""
            verified = result.get("goal_complete") is True and result.get("verified") is True
            if verified:
                current["state"] = "complete"
                current["last_error"] = ""
                document["cursor"] = cursor + 1
                if document["cursor"] >= len(document["items"]):
                    document["status"] = "complete"
                    document["note"] = (
                        f"All {len(document['items'])} board goals are verified complete."
                    )
                else:
                    document["status"] = "queued"
                    document["note"] = (
                        f"Goal {cursor + 1} is verified. Goal {cursor + 2} is the exact next goal."
                    )
            else:
                current["state"] = "paused"
                current["last_error"] = str(
                    result.get("message") or result.get("error")
                    or "This goal is saved but is not yet verified complete."
                )[:4_000]
                document["status"] = "paused"
                document["note"] = current["last_error"]
            self._write(database, document)
            if document["status"] in {"complete", "cancelled"}:
                self._prune_terminal(database)
            return self._public(document)

    def record_failure(
        self, queue_id: object, item_id: object, error: object
    ) -> dict[str, Any] | None:
        with self._tx() as database:
            row = database.execute(
                "SELECT * FROM goal_queues WHERE queue_id=?", (str(queue_id or ""),)
            ).fetchone()
            document = self._decode(row)
            if document is None or int(document["cursor"]) >= len(document["items"]):
                return self._public(document) if document else None
            current = document["items"][int(document["cursor"])]
            if current["id"] != str(item_id or ""):
                return self._public(document, reused=True)
            current.update({
                "state": "paused", "owner_pid": 0, "owner_token": "",
                "last_error": str(error or "The exact goal stopped before completion.")[:4_000],
            })
            document["status"] = "paused"
            document["note"] = current["last_error"]
            self._write(database, document)
            return self._public(document)

    def reconcile(
        self, run_lookup: Callable[[str], dict[str, Any]]
    ) -> dict[str, Any] | None:
        status = self.status()
        if not status or status["status"] not in ACTIVE or not status["current"]:
            return status
        current = status["current"]
        request_id = str(current.get("work_request_id") or "")
        if not request_id:
            return status
        try:
            run = run_lookup(request_id)
        except HarnessError:
            return status
        result = run.get("result")
        if isinstance(result, dict):
            return self.record_result(
                status["queue_id"], current["id"], result,
                run_id=str(run.get("run_id") or ""),
            )
        if str(run.get("status") or "") in {
            "failed", "stopped", "interrupted", "delivery_unknown", "outcome_unknown"
        }:
            return self.record_failure(
                status["queue_id"], current["id"],
                run.get("error") or "The durable work command stopped without a verified result.",
            )
        return status

    def cancel(self, queue_id: object) -> dict[str, Any]:
        with self._tx() as database:
            row = database.execute(
                "SELECT * FROM goal_queues WHERE queue_id=?", (str(queue_id or ""),)
            ).fetchone()
            document = self._decode(row)
            if document is None:
                raise HarnessError("That board-goal queue does not exist.")
            if document["status"] == "running":
                raise HarnessError(
                    "Stop the exact active chat run first; then cancel the remaining board goals."
                )
            if document["status"] in ACTIVE:
                document["status"] = "cancelled"
                document["note"] = (
                    "The remaining board goals were cancelled. Completed goals remain recorded and were not undone."
                )
                self._write(database, document)
                self._prune_terminal(database)
            return self._public(document)
