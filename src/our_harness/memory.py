from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import struct
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .config import LoadedConfig
from .models import HarnessError
from .runstate import (
    RUN_CHECKPOINT_SCHEMA_VERSION,
    RunCheckpoint,
    RunCheckpointConflict,
    canonical_json,
    canonical_json_sha256,
    checkpoint_safe_copy,
    graph_sha256,
)
from .redaction import CredentialRedactor
from .safety import confined_path


SCHEMA_VERSION = 7
MAX_CHUNK_CHARS = 6_000
MAX_AGENT_TOOL_RESULT_BYTES = 262_144


@dataclass(frozen=True)
class MemoryHit:
    source: str
    key: str
    text: str
    score: float
    metadata: dict[str, Any]


def _pack_vector(values: list[float] | None) -> bytes | None:
    return struct.pack(f"<{len(values)}f", *values) if values else None


def _unpack_vector(value: bytes | None) -> list[float]:
    if not value:
        return []
    return list(struct.unpack(f"<{len(value) // 4}f", value))


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right))
    denominator = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return numerator / denominator if denominator else 0.0


def _fts_query(text: str) -> str:
    terms = re.findall(r"[A-Za-z0-9_]{2,}", text)
    return " OR ".join(f'"{term}"' for term in terms[:20])


def bounded_chunks(text: str, limit: int = MAX_CHUNK_CHARS) -> list[dict[str, Any]]:
    """Split text deterministically on line boundaries, including oversized-line fallback."""
    if limit <= 0:
        raise ValueError("chunk limit must be positive")
    lines = text.splitlines(keepends=True)
    if not lines:
        return [{"content": "", "start_line": 1, "end_line": 1}]
    chunks: list[dict[str, Any]] = []
    parts: list[str] = []
    size = 0
    start_line = 1

    def flush(end_line: int) -> None:
        nonlocal parts, size, start_line
        if parts:
            chunks.append({"content": "".join(parts), "start_line": start_line, "end_line": end_line})
            parts = []
            size = 0

    for line_number, line in enumerate(lines, 1):
        remaining = line
        while remaining:
            available = limit - size
            if available == 0:
                flush(max(start_line, line_number - 1))
                start_line = line_number
                available = limit
            piece = remaining[:available]
            parts.append(piece)
            size += len(piece)
            remaining = remaining[len(piece) :]
            if remaining:
                flush(line_number)
                start_line = line_number
        if size >= limit:
            flush(line_number)
            start_line = line_number + 1
        elif line_number < len(lines) and size + len(lines[line_number]) > limit:
            flush(line_number)
            start_line = line_number + 1
    flush(len(lines))
    return chunks or [{"content": "", "start_line": 1, "end_line": 1}]


def _prune_hits(hits: list[MemoryHit], limit: int, *, dedupe_content: bool = False) -> list[MemoryHit]:
    ordered = sorted(
        hits,
        key=lambda hit: (-hit.score, hit.key, str(hit.metadata.get("chunk_id", ""))),
    )
    selected: list[MemoryHit] = []
    paths: set[str] = set()
    digests: set[str] = set()
    for hit in ordered:
        digest = str(hit.metadata.get("content_sha256", ""))
        if hit.key in paths or (dedupe_content and digest and digest in digests):
            continue
        selected.append(hit)
        paths.add(hit.key)
        if digest:
            digests.add(digest)
        if len(selected) >= limit:
            break
    return selected


class MemoryStore:
    def __init__(self, config: LoadedConfig):
        self.redactor = CredentialRedactor(config)
        self.enabled = bool(config.get("memory.enabled"))
        self.path: Path | None = None
        if self.enabled:
            relative = config.get("memory.database")
            self.path = confined_path(config.project_root, relative, allow_control=True)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(self.path, timeout=15)
        else:
            self.connection = sqlite3.connect(":memory:", timeout=15)
        self.connection.row_factory = sqlite3.Row
        self.has_fts = True
        # Every opening makes sure the tables are there. Several at once on a
        # database that does not exist yet - four browsers opening the panel on
        # a fresh machine - each try to make them at the same moment, and all
        # but one are told the database is busy. It is busy for a moment, so
        # this waits rather than giving up and reporting a broken panel.
        last: Exception | None = None
        for wait in (0.05, 0.1, 0.2, 0.4, 0.8, 1.6, 0):
            try:
                self._migrate()
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) and "busy" not in str(exc):
                    raise
                last = exc
                if not wait:
                    raise HarnessError(
                        "The memory database is busy and stayed busy. Close anything else "
                        "using this project and try again."
                    ) from last
                time.sleep(wait)

    def redact_text(self, value: str) -> str:
        return self.redactor.text(value)

    def redact_value(self, value: Any) -> Any:
        return self.redactor.value(value)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _migrate(self) -> None:
        db = self.connection
        db.execute("PRAGMA busy_timeout=15000")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs(
              id TEXT PRIMARY KEY, task TEXT NOT NULL, state TEXT NOT NULL,
              graph_version TEXT NOT NULL, started_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
              result_json TEXT
            );
            CREATE TABLE IF NOT EXISTS events(
              sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, run_id TEXT NOT NULL,
              kind TEXT NOT NULL, node_id TEXT NOT NULL, causation_id TEXT, payload_json TEXT NOT NULL,
              input_sha256 TEXT NOT NULL, created_at INTEGER NOT NULL,
              FOREIGN KEY(run_id) REFERENCES runs(id)
            );
            CREATE INDEX IF NOT EXISTS events_run_sequence ON events(run_id, sequence);
            CREATE TABLE IF NOT EXISTS episodes(
              id TEXT PRIMARY KEY, namespace TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
              metadata_json TEXT NOT NULL, embedding BLOB, trust REAL NOT NULL DEFAULT 0.5,
              created_at INTEGER NOT NULL, accessed_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS documents(
              path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, language TEXT NOT NULL,
              content TEXT NOT NULL, embedding BLOB, indexed_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dependency_edges(
              source TEXT NOT NULL, target TEXT NOT NULL, kind TEXT NOT NULL,
              PRIMARY KEY(source, target, kind)
            );
            CREATE INDEX IF NOT EXISTS dependency_target ON dependency_edges(target);
            CREATE TABLE IF NOT EXISTS document_chunks(
              id TEXT PRIMARY KEY, path TEXT NOT NULL, ordinal INTEGER NOT NULL,
              start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
              content_sha256 TEXT NOT NULL, content TEXT NOT NULL, embedding BLOB,
              UNIQUE(path, ordinal), UNIQUE(path, content_sha256)
            );
            CREATE INDEX IF NOT EXISTS document_chunks_path ON document_chunks(path, ordinal);
            CREATE TABLE IF NOT EXISTS document_symbols(
              path TEXT NOT NULL, name TEXT NOT NULL, qualified_name TEXT NOT NULL,
              kind TEXT NOT NULL, line INTEGER NOT NULL, end_line INTEGER NOT NULL,
              chunk_id TEXT NOT NULL,
              PRIMARY KEY(path, qualified_name, kind, line)
            );
            CREATE INDEX IF NOT EXISTS document_symbols_name ON document_symbols(name, qualified_name);
            CREATE TABLE IF NOT EXISTS prompt_versions(
              id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, body TEXT NOT NULL,
              parent_id TEXT, active INTEGER NOT NULL, created_at INTEGER NOT NULL,
              metadata_json TEXT NOT NULL, content_sha256 TEXT
            );
            CREATE TABLE IF NOT EXISTS prompt_version_observations(
              sequence INTEGER PRIMARY KEY AUTOINCREMENT,
              observation_sha256 TEXT UNIQUE NOT NULL, prompt_id TEXT NOT NULL,
              run_id TEXT NOT NULL, provider_route TEXT NOT NULL,
              provider TEXT NOT NULL, model TEXT NOT NULL,
              metadata_json TEXT NOT NULL, created_at_ms INTEGER NOT NULL,
              FOREIGN KEY(prompt_id) REFERENCES prompt_versions(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS prompt_observations_prompt_sequence
              ON prompt_version_observations(prompt_id,sequence);
            CREATE TABLE IF NOT EXISTS review_packets(
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL, patch_sha256 TEXT NOT NULL,
              packet_json TEXT NOT NULL, verdict_json TEXT, created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_tool_journal(
              run_id TEXT NOT NULL,
              node_id TEXT NOT NULL,
              call_id_sha256 TEXT NOT NULL,
              tool_name TEXT NOT NULL,
              arguments_sha256 TEXT NOT NULL,
              result_json TEXT NOT NULL,
              result_sha256 TEXT NOT NULL,
              content_bytes INTEGER NOT NULL,
              created_at_ms INTEGER NOT NULL,
              PRIMARY KEY(run_id,node_id,call_id_sha256,arguments_sha256),
              UNIQUE(run_id,node_id,call_id_sha256),
              FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS agent_tool_journal_run_node
              ON agent_tool_journal(run_id,node_id,created_at_ms);
            CREATE TABLE IF NOT EXISTS provider_usage(
              sequence INTEGER PRIMARY KEY AUTOINCREMENT,
              run_id TEXT NOT NULL, node_id TEXT NOT NULL, agent_role TEXT NOT NULL,
              provider_route TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
              input_tokens INTEGER, output_tokens INTEGER, cached_input_tokens INTEGER,
              cache_write_input_tokens INTEGER, reasoning_tokens INTEGER,
              tool_use_tokens INTEGER, billed_output_tokens INTEGER, latency_ms INTEGER,
              cost_microusd INTEGER, price_status TEXT NOT NULL DEFAULT 'unavailable',
              price_snapshot_id TEXT,
              cost_nanos TEXT, cost_basis TEXT NOT NULL, rate_id TEXT,
              created_at_ms INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS provider_usage_run_sequence
              ON provider_usage(run_id,sequence);
            CREATE TABLE IF NOT EXISTS memory_provenance(
              sequence INTEGER PRIMARY KEY AUTOINCREMENT,
              memory_kind TEXT NOT NULL, memory_id TEXT NOT NULL, relation TEXT NOT NULL,
              run_id TEXT NOT NULL, node_id TEXT NOT NULL,
              provider_route TEXT NOT NULL, model TEXT NOT NULL,
              created_at_ms INTEGER NOT NULL,
              UNIQUE(memory_kind,memory_id,relation,run_id,node_id)
            );
            CREATE INDEX IF NOT EXISTS memory_provenance_memory
              ON memory_provenance(memory_kind,memory_id,sequence);
            CREATE TABLE IF NOT EXISTS refinement_candidates(
              id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, body TEXT NOT NULL,
              baseline_id TEXT, evidence_json TEXT NOT NULL, expected_outcome TEXT NOT NULL,
              status TEXT NOT NULL, created_at INTEGER NOT NULL,
              verification_json TEXT, review_verdict TEXT, decision_reason TEXT,
              decided_at INTEGER, promoted_version_id TEXT, review_binding_sha256 TEXT
            );
            """
        )
        document_columns = {row[1] for row in db.execute("PRAGMA table_info(documents)")}
        if "embedding" not in document_columns:
            db.execute("ALTER TABLE documents ADD COLUMN embedding BLOB")
        candidate_columns = {row[1] for row in db.execute("PRAGMA table_info(refinement_candidates)")}
        for name, declaration in (
            ("verification_json", "TEXT"),
            ("review_verdict", "TEXT"),
            ("decision_reason", "TEXT"),
            ("decided_at", "INTEGER"),
            ("promoted_version_id", "TEXT"),
            ("review_binding_sha256", "TEXT"),
        ):
            if name not in candidate_columns:
                db.execute(f"ALTER TABLE refinement_candidates ADD COLUMN {name} {declaration}")
        usage_columns = {row[1] for row in db.execute("PRAGMA table_info(provider_usage)")}
        usage_needs_price_backfill = any(
            name not in usage_columns for name in ("cost_microusd", "price_status", "price_snapshot_id")
        )
        for name, declaration in (
            ("reasoning_tokens", "INTEGER"),
            ("tool_use_tokens", "INTEGER"),
            ("billed_output_tokens", "INTEGER"),
            ("cost_microusd", "INTEGER"),
            ("price_status", "TEXT NOT NULL DEFAULT 'unavailable'"),
            ("price_snapshot_id", "TEXT"),
        ):
            if name not in usage_columns:
                db.execute(f"ALTER TABLE provider_usage ADD COLUMN {name} {declaration}")
        if usage_needs_price_backfill:
            for row in db.execute(
                "SELECT sequence,cost_nanos,cost_basis,rate_id,cost_microusd,price_status,price_snapshot_id "
                "FROM provider_usage ORDER BY sequence"
            ).fetchall():
                cost_microusd = row["cost_microusd"]
                cost_nanos = row["cost_nanos"]
                if cost_microusd is None and isinstance(cost_nanos, str) and re.fullmatch(r"\d+", cost_nanos):
                    nanos = int(cost_nanos)
                    cost_microusd = nanos // 1000 if nanos % 1000 == 0 else None
                price_status = row["price_status"]
                if not price_status or price_status == "unavailable":
                    price_status = row["cost_basis"] or "unavailable"
                price_snapshot_id = row["price_snapshot_id"] or row["rate_id"]
                db.execute(
                    "UPDATE provider_usage SET cost_microusd=?,price_status=?,price_snapshot_id=? WHERE sequence=?",
                    (cost_microusd, price_status, price_snapshot_id, row["sequence"]),
                )
        prompt_columns = {row[1] for row in db.execute("PRAGMA table_info(prompt_versions)")}
        prompt_needs_content_backfill = "content_sha256" not in prompt_columns
        if "content_sha256" not in prompt_columns:
            db.execute("ALTER TABLE prompt_versions ADD COLUMN content_sha256 TEXT")
        if prompt_needs_content_backfill:
            seen_prompt_content: set[tuple[str, str, str]] = set()
            for row in db.execute("SELECT rowid,id,kind,name,body FROM prompt_versions ORDER BY rowid").fetchall():
                digest = hashlib.sha256(str(row["body"]).encode("utf-8")).hexdigest()
                key = (str(row["kind"]), str(row["name"]), digest)
                db.execute(
                    "UPDATE prompt_versions SET content_sha256=? WHERE id=?",
                    (None if key in seen_prompt_content else digest, row["id"]),
                )
                seen_prompt_content.add(key)
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS prompt_versions_content "
            "ON prompt_versions(kind,name,content_sha256) WHERE content_sha256 IS NOT NULL"
        )
        db.execute(
            "UPDATE prompt_versions SET active=0 WHERE kind='agent-system' OR name LIKE 'graph-agent:%'"
        )
        db.execute(
            "UPDATE refinement_candidates SET status='pending',verification_json=NULL,review_verdict=NULL,"
            "decision_reason='Review evidence must be rebound after the schema upgrade',decided_at=NULL "
            "WHERE status='reviewed' AND review_binding_sha256 IS NULL"
        )
        try:
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(id UNINDEXED, title, body)")
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(path UNINDEXED, content)")
            db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS document_chunks_fts "
                "USING fts5(id UNINDEXED, path UNINDEXED, symbols, content)"
            )
        except sqlite3.OperationalError:
            self.has_fts = False
        existing_chunk_paths = {
            row[0] for row in db.execute("SELECT DISTINCT path FROM document_chunks")
        }
        for row in db.execute("SELECT path,content,embedding FROM documents ORDER BY path").fetchall():
            if row["path"] in existing_chunk_paths:
                continue
            for ordinal, chunk in enumerate(bounded_chunks(row["content"])):
                content = str(chunk["content"])
                digest = hashlib.sha256(content.encode()).hexdigest()
                chunk_id = f"{row['path']}#chunk-{ordinal:04d}"
                inserted = db.execute(
                    "INSERT OR IGNORE INTO document_chunks"
                    "(id,path,ordinal,start_line,end_line,content_sha256,content,embedding) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        chunk_id,
                        row["path"],
                        ordinal,
                        chunk["start_line"],
                        chunk["end_line"],
                        digest,
                        content,
                        row["embedding"],
                    ),
                )
                if self.has_fts and inserted.rowcount:
                    db.execute(
                        "INSERT INTO document_chunks_fts(id,path,symbols,content) VALUES(?,?,?,?)",
                        (chunk_id, row["path"], "", content),
                    )
            db.execute(
                "UPDATE documents SET sha256='reindex-v4:' || sha256 "
                "WHERE path=? AND sha256 NOT LIKE 'reindex-v4:%'",
                (row["path"],),
            )
        db.execute("UPDATE documents SET content='',embedding=NULL")
        if self.has_fts:
            db.execute("DELETE FROM documents_fts")
        db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
        db.commit()
        self._migrate_run_checkpoints()

    def _migrate_run_checkpoints(self) -> None:
        """Install durable run state without coupling it to memory/index tables."""
        with self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS run_checkpoints(
                  run_id TEXT PRIMARY KEY,
                  schema_version INTEGER NOT NULL,
                  task TEXT NOT NULL,
                  graph_json TEXT NOT NULL,
                  graph_sha256 TEXT NOT NULL,
                  current_node TEXT NOT NULL,
                  state_json TEXT NOT NULL,
                  transaction_ids_json TEXT NOT NULL,
                  transaction_manifests_json TEXT NOT NULL,
                  remaining_deadline_seconds REAL NOT NULL,
                  pending_approval_json TEXT,
                  sequence INTEGER NOT NULL,
                  version INTEGER NOT NULL,
                  payload_sha256 TEXT NOT NULL,
                  updated_at_ms INTEGER NOT NULL,
                  FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS run_checkpoints_updated
                  ON run_checkpoints(updated_at_ms DESC, run_id);
                """
            )
            self.connection.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('run_checkpoint_schema_version',?)",
                (str(RUN_CHECKPOINT_SCHEMA_VERSION),),
            )

    def _checkpoint_columns(self, checkpoint: RunCheckpoint, updated_at_ms: int) -> tuple[Any, ...]:
        safe_graph = checkpoint_safe_copy(self.redact_value(checkpoint.frozen_graph))
        safe_state = checkpoint_safe_copy(self.redact_value(checkpoint.state))
        safe_manifests = checkpoint_safe_copy(self.redact_value(list(checkpoint.transaction_manifests)))
        safe_approval = checkpoint_safe_copy(self.redact_value(checkpoint.pending_approval))
        safe_task = self.redact_text(checkpoint.task)
        snapshot = RunCheckpoint(
            run_id=checkpoint.run_id,
            task=safe_task,
            frozen_graph=safe_graph,
            graph_sha256=graph_sha256(safe_graph),
            current_node=checkpoint.current_node,
            state=safe_state,
            transaction_ids=checkpoint.transaction_ids,
            transaction_manifests=tuple(safe_manifests),
            remaining_deadline_seconds=checkpoint.remaining_deadline_seconds,
            pending_approval=safe_approval,
            sequence=checkpoint.sequence,
        )
        snapshot.validate()
        return (
            snapshot.run_id,
            RUN_CHECKPOINT_SCHEMA_VERSION,
            snapshot.task,
            canonical_json(snapshot.frozen_graph),
            snapshot.graph_sha256,
            snapshot.current_node,
            canonical_json(snapshot.state),
            canonical_json(list(snapshot.transaction_ids)),
            canonical_json(list(snapshot.transaction_manifests)),
            float(snapshot.remaining_deadline_seconds),
            canonical_json(snapshot.pending_approval) if snapshot.pending_approval is not None else None,
            snapshot.sequence,
            snapshot.payload_sha256(),
            updated_at_ms,
        )

    @staticmethod
    def _checkpoint_storage_sha256(payload_sha256: str, version: int, updated_at_ms: int) -> str:
        envelope = {
            "payload_sha256": payload_sha256,
            "version": version,
            "updated_at_ms": updated_at_ms,
        }
        return hashlib.sha256(canonical_json(envelope).encode("utf-8")).hexdigest()

    @staticmethod
    def _decode_checkpoint(row: sqlite3.Row, *, apply_elapsed: bool = True) -> RunCheckpoint:
        if row["schema_version"] != RUN_CHECKPOINT_SCHEMA_VERSION:
            raise HarnessError(
                f"Run checkpoint {row['run_id']} uses unsupported schema version {row['schema_version']}"
            )
        try:
            graph = json.loads(row["graph_json"])
            state = json.loads(row["state_json"])
            transaction_ids = json.loads(row["transaction_ids_json"])
            manifests = json.loads(row["transaction_manifests_json"])
            approval = json.loads(row["pending_approval_json"]) if row["pending_approval_json"] is not None else None
        except (json.JSONDecodeError, TypeError) as exc:
            raise HarnessError(f"Run checkpoint {row['run_id']} contains corrupted JSON") from exc
        if not isinstance(graph, dict) or not isinstance(state, dict):
            raise HarnessError(f"Run checkpoint {row['run_id']} contains invalid graph or state data")
        if not isinstance(transaction_ids, list) or not all(isinstance(item, str) for item in transaction_ids):
            raise HarnessError(f"Run checkpoint {row['run_id']} contains invalid transaction IDs")
        if not isinstance(manifests, list) or not all(isinstance(item, dict) for item in manifests):
            raise HarnessError(f"Run checkpoint {row['run_id']} contains invalid transaction manifests")
        checkpoint = RunCheckpoint(
            run_id=row["run_id"],
            task=row["task"],
            frozen_graph=graph,
            graph_sha256=row["graph_sha256"],
            current_node=row["current_node"],
            state=state,
            transaction_ids=tuple(transaction_ids),
            transaction_manifests=tuple(manifests),
            remaining_deadline_seconds=row["remaining_deadline_seconds"],
            pending_approval=approval,
            sequence=row["sequence"],
            version=row["version"],
            updated_at_ms=row["updated_at_ms"],
        )
        checkpoint.validate()
        expected_storage_hash = MemoryStore._checkpoint_storage_sha256(
            checkpoint.payload_sha256(), checkpoint.version, checkpoint.updated_at_ms
        )
        if expected_storage_hash != row["payload_sha256"]:
            raise HarnessError(f"Run checkpoint {row['run_id']} failed payload hash validation")
        return checkpoint.with_elapsed_deadline() if apply_elapsed else checkpoint

    def save_run_checkpoint(self, checkpoint: RunCheckpoint) -> RunCheckpoint:
        """Insert or replace a checkpoint and advance its storage version atomically."""
        updated_at_ms = int(time.time() * 1000)
        values = self._checkpoint_columns(checkpoint, updated_at_ms)
        try:
            with self.connection:
                self.connection.execute(
                    """
                    INSERT INTO run_checkpoints(
                      run_id,schema_version,task,graph_json,graph_sha256,current_node,state_json,
                      transaction_ids_json,transaction_manifests_json,remaining_deadline_seconds,
                      pending_approval_json,sequence,version,payload_sha256,updated_at_ms
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)
                    ON CONFLICT(run_id) DO UPDATE SET
                      schema_version=excluded.schema_version,task=excluded.task,graph_json=excluded.graph_json,
                      graph_sha256=excluded.graph_sha256,current_node=excluded.current_node,state_json=excluded.state_json,
                      transaction_ids_json=excluded.transaction_ids_json,
                      transaction_manifests_json=excluded.transaction_manifests_json,
                      remaining_deadline_seconds=excluded.remaining_deadline_seconds,
                      pending_approval_json=excluded.pending_approval_json,sequence=excluded.sequence,
                      version=run_checkpoints.version+1,payload_sha256=excluded.payload_sha256,
                      updated_at_ms=excluded.updated_at_ms
                    """,
                    values,
                )
                row = self.connection.execute("SELECT * FROM run_checkpoints WHERE run_id=?", (checkpoint.run_id,)).fetchone()
                if row is not None:
                    storage_hash = self._checkpoint_storage_sha256(values[12], row["version"], updated_at_ms)
                    self.connection.execute(
                        "UPDATE run_checkpoints SET payload_sha256=? WHERE run_id=? AND version=?",
                        (storage_hash, checkpoint.run_id, row["version"]),
                    )
                    row = self.connection.execute(
                        "SELECT * FROM run_checkpoints WHERE run_id=?", (checkpoint.run_id,)
                    ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise HarnessError(f"Cannot save run checkpoint {checkpoint.run_id}: the run does not exist") from exc
        if row is None:
            raise HarnessError(f"Cannot save run checkpoint {checkpoint.run_id}")
        return self._decode_checkpoint(row, apply_elapsed=False)

    def compare_and_swap_run_checkpoint(self, checkpoint: RunCheckpoint, expected_version: int) -> RunCheckpoint:
        """Write only when the stored version matches; version zero creates a missing row."""
        if not isinstance(expected_version, int) or isinstance(expected_version, bool) or expected_version < 0:
            raise HarnessError("Expected run checkpoint version must be a non-negative integer")
        updated_at_ms = int(time.time() * 1000)
        values = self._checkpoint_columns(checkpoint, updated_at_ms)
        try:
            with self.connection:
                if expected_version == 0:
                    cursor = self.connection.execute(
                        """
                        INSERT INTO run_checkpoints(
                          run_id,schema_version,task,graph_json,graph_sha256,current_node,state_json,
                          transaction_ids_json,transaction_manifests_json,remaining_deadline_seconds,
                          pending_approval_json,sequence,version,payload_sha256,updated_at_ms
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)
                        ON CONFLICT(run_id) DO NOTHING
                        """,
                        values,
                    )
                else:
                    cursor = self.connection.execute(
                        """
                        UPDATE run_checkpoints SET
                          schema_version=?,task=?,graph_json=?,graph_sha256=?,current_node=?,state_json=?,
                          transaction_ids_json=?,transaction_manifests_json=?,remaining_deadline_seconds=?,
                          pending_approval_json=?,sequence=?,version=version+1,payload_sha256=?,updated_at_ms=?
                        WHERE run_id=? AND version=?
                        """,
                        (*values[1:], values[0], expected_version),
                    )
                if cursor.rowcount != 1:
                    raise RunCheckpointConflict(
                        f"Run checkpoint {checkpoint.run_id} changed since version {expected_version}"
                    )
                row = self.connection.execute("SELECT * FROM run_checkpoints WHERE run_id=?", (checkpoint.run_id,)).fetchone()
                if row is not None:
                    storage_hash = self._checkpoint_storage_sha256(values[12], row["version"], updated_at_ms)
                    self.connection.execute(
                        "UPDATE run_checkpoints SET payload_sha256=? WHERE run_id=? AND version=?",
                        (storage_hash, checkpoint.run_id, row["version"]),
                    )
                    row = self.connection.execute(
                        "SELECT * FROM run_checkpoints WHERE run_id=?", (checkpoint.run_id,)
                    ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise HarnessError(f"Cannot save run checkpoint {checkpoint.run_id}: the run does not exist") from exc
        if row is None:
            raise HarnessError(f"Cannot save run checkpoint {checkpoint.run_id}")
        return self._decode_checkpoint(row, apply_elapsed=False)

    def load_run_checkpoint(
        self, run_id: str, *, expected_graph_sha256: str | None = None
    ) -> RunCheckpoint | None:
        row = self.connection.execute("SELECT * FROM run_checkpoints WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        checkpoint = self._decode_checkpoint(row)
        if expected_graph_sha256 is not None and checkpoint.graph_sha256 != expected_graph_sha256:
            raise HarnessError(f"Run checkpoint {run_id} does not match the requested frozen graph")
        return checkpoint

    def list_run_checkpoints(self) -> list[RunCheckpoint]:
        rows = self.connection.execute(
            "SELECT * FROM run_checkpoints ORDER BY updated_at_ms DESC,run_id"
        ).fetchall()
        return [self._decode_checkpoint(row) for row in rows]

    def delete_run_checkpoint(self, run_id: str, *, expected_version: int | None = None) -> bool:
        if expected_version is not None and (
            not isinstance(expected_version, int) or isinstance(expected_version, bool) or expected_version < 0
        ):
            raise HarnessError("Expected run checkpoint version must be a non-negative integer")
        with self.connection:
            if expected_version is None:
                cursor = self.connection.execute("DELETE FROM run_checkpoints WHERE run_id=?", (run_id,))
            else:
                cursor = self.connection.execute(
                    "DELETE FROM run_checkpoints WHERE run_id=? AND version=?", (run_id, expected_version)
                )
        return cursor.rowcount == 1

    @staticmethod
    def _validate_tool_journal_key(
        run_id: str,
        node_id: str,
        call_id_sha256: str,
        tool_name: str,
        arguments_sha256: str,
    ) -> None:
        digest = re.compile(r"[0-9a-f]{64}")
        if not run_id or len(run_id) > 128 or not re.fullmatch(r"[A-Za-z0-9._-]+", run_id):
            raise HarnessError("Agent tool journal run ID is invalid")
        if not node_id or len(node_id) > 128 or not re.fullmatch(r"[A-Za-z0-9._:-]+", node_id):
            raise HarnessError("Agent tool journal node ID is invalid")
        if not tool_name or len(tool_name) > 128 or not re.fullmatch(r"[A-Za-z0-9._:-]+", tool_name):
            raise HarnessError("Agent tool journal tool name is invalid")
        if not digest.fullmatch(call_id_sha256) or not digest.fullmatch(arguments_sha256):
            raise HarnessError("Agent tool journal hash is invalid")

    def load_agent_tool_result(
        self,
        *,
        run_id: str,
        node_id: str,
        call_id_sha256: str,
        tool_name: str,
        arguments_sha256: str,
    ) -> dict[str, Any] | None:
        """Load one completed bounded tool result, rejecting call-ID rebinding or tampering."""
        self._validate_tool_journal_key(run_id, node_id, call_id_sha256, tool_name, arguments_sha256)
        row = self.connection.execute(
            "SELECT * FROM agent_tool_journal WHERE run_id=? AND node_id=? AND call_id_sha256=?",
            (run_id, node_id, call_id_sha256),
        ).fetchone()
        if row is None:
            return None
        if row["tool_name"] != tool_name or row["arguments_sha256"] != arguments_sha256:
            raise HarnessError("Agent tool call ID was already bound to different tool arguments")
        try:
            result = json.loads(row["result_json"])
        except json.JSONDecodeError as exc:
            raise HarnessError("Agent tool journal result is malformed") from exc
        if not isinstance(result, dict) or canonical_json_sha256(result) != row["result_sha256"]:
            raise HarnessError("Agent tool journal result failed integrity validation")
        if result.get("content_bytes") != row["content_bytes"]:
            raise HarnessError("Agent tool journal byte count failed integrity validation")
        return result

    def record_agent_tool_result(
        self,
        *,
        run_id: str,
        node_id: str,
        call_id_sha256: str,
        tool_name: str,
        arguments_sha256: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        """Insert one immutable completed tool result or return the identical prior record."""
        self._validate_tool_journal_key(run_id, node_id, call_id_sha256, tool_name, arguments_sha256)
        safe_result = self.redact_value(result)
        if not isinstance(safe_result, dict):
            raise HarnessError("Agent tool journal result must be an object")
        result_json = canonical_json(safe_result)
        if len(result_json.encode("utf-8")) > MAX_AGENT_TOOL_RESULT_BYTES:
            raise HarnessError("Agent tool journal result exceeds its storage limit")
        content_bytes = safe_result.get("content_bytes")
        if isinstance(content_bytes, bool) or not isinstance(content_bytes, int) or content_bytes < 0:
            raise HarnessError("Agent tool journal result has an invalid byte count")
        result_sha256 = canonical_json_sha256(safe_result)
        try:
            with self.connection:
                self.connection.execute(
                    """
                    INSERT INTO agent_tool_journal(
                      run_id,node_id,call_id_sha256,tool_name,arguments_sha256,
                      result_json,result_sha256,content_bytes,created_at_ms
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        run_id,
                        node_id,
                        call_id_sha256,
                        tool_name,
                        arguments_sha256,
                        result_json,
                        result_sha256,
                        content_bytes,
                        int(time.time() * 1000),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            prior = self.load_agent_tool_result(
                run_id=run_id,
                node_id=node_id,
                call_id_sha256=call_id_sha256,
                tool_name=tool_name,
                arguments_sha256=arguments_sha256,
            )
            if prior is None:
                raise HarnessError("Cannot record agent tool result for a missing run") from exc
            if canonical_json_sha256(prior) != result_sha256:
                raise HarnessError("Agent tool result conflicts with the retained journal entry") from exc
            return prior
        return safe_result

    def start_run(self, task: str, graph_version: str = "1") -> str:
        run_id = uuid.uuid4().hex
        now = int(time.time())
        self.connection.execute(
            "INSERT INTO runs(id, task, state, graph_version, started_at, updated_at) VALUES(?,?,?,?,?,?)",
            (run_id, self.redact_text(task), "discover", graph_version, now, now),
        )
        self.connection.commit()
        return run_id

    def ensure_external_run(
        self, run_id: str, task: str, graph_version: str = "external-tools-v1",
    ) -> None:
        """Register a caller-owned durable run identity for the tool journal."""

        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", str(run_id or "")):
            raise HarnessError("External agent-tool run ID is invalid")
        now = int(time.time())
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO runs(id, task, state, graph_version, started_at, updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    run_id, self.redact_text(task), "swarm_tools",
                    graph_version, now, now,
                ),
            )

    def append_event(self, run_id: str, kind: str, node_id: str, payload: dict[str, Any], causation_id: str | None = None) -> str:
        event_id = uuid.uuid4().hex
        canonical = json.dumps(self.redact_value(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        now = int(time.time())
        with self.connection:
            self.connection.execute(
                "INSERT INTO events(id,run_id,kind,node_id,causation_id,payload_json,input_sha256,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (event_id, run_id, kind, node_id, causation_id, canonical, digest, now),
            )
            self.connection.execute("UPDATE runs SET state=?, updated_at=? WHERE id=?", (node_id, now, run_id))
        return event_id

    def finish_run(self, run_id: str, state: str, result: dict[str, Any]) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE runs SET state=?, updated_at=?, result_json=? WHERE id=?",
                (state, int(time.time()), json.dumps(self.redact_value(result), sort_keys=True), run_id),
            )

    def record_review_packet(
        self,
        run_id: str,
        patch_sha256: str,
        packet: dict[str, Any],
        verdict: dict[str, Any],
    ) -> str:
        safe_packet = self.redact_value(packet)
        safe_verdict = self.redact_value(verdict)
        if not isinstance(safe_packet, dict) or not isinstance(safe_verdict, dict):
            raise HarnessError("Review packet and verdict must be objects")
        packet_id = str(safe_packet.get("packet_id", ""))
        packet_without_id = dict(safe_packet)
        packet_without_id.pop("packet_id", None)
        if not packet_id or packet_id != canonical_json_sha256(packet_without_id):
            raise HarnessError("Review packet ID does not match its redacted content")
        with self.connection:
            self.connection.execute(
                "INSERT INTO review_packets(id,run_id,patch_sha256,packet_json,verdict_json,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (packet_id, run_id, patch_sha256, canonical_json(safe_packet), canonical_json(safe_verdict), int(time.time())),
            )
        return packet_id

    def events(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM events WHERE run_id=? ORDER BY sequence", (run_id,)).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]

    def run_timeline(self, limit: int = 10, per_run_steps: int = 200) -> list[dict[str, Any]]:
        """Recent runs and what each step of them did, newest run first.

        A step starts when a node reports it began and ends at the next event
        for that node, so the reader can see where the time went without
        reading the whole event log.
        """

        # Start times are whole seconds, so several runs can share one. The
        # insertion order breaks the tie and keeps "newest first" true.
        runs = self.connection.execute(
            "SELECT id,task,state,started_at,updated_at FROM runs "
            "ORDER BY started_at DESC, rowid DESC LIMIT ?",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
        step_cap = max(1, min(int(per_run_steps), 1000))
        timeline: list[dict[str, Any]] = []
        for run in runs:
            rows = self.connection.execute(
                "SELECT kind,node_id,created_at FROM events WHERE run_id=? ORDER BY sequence LIMIT ?",
                (run["id"], step_cap),
            ).fetchall()
            steps: list[dict[str, Any]] = []
            open_step: dict[str, Any] | None = None
            for row in rows:
                node = str(row["node_id"] or "")
                moment = float(row["created_at"] or 0.0)
                if open_step is not None and (node != open_step["node"] or row["kind"] == "node_start"):
                    open_step["ended_at"] = moment
                    open_step["duration_ms"] = max(0, int((moment - open_step["started_at"]) * 1000))
                    steps.append(open_step)
                    open_step = None
                if open_step is None and node:
                    open_step = {"node": node, "started_at": moment, "ended_at": moment, "duration_ms": 0, "result": ""}
                if open_step is not None:
                    if row["kind"] in ("failure", "run_error"):
                        open_step["result"] = "failed"
                    elif not open_step["result"] and row["kind"] in ("success", "node_end"):
                        open_step["result"] = "passed"
            if open_step is not None:
                open_step["ended_at"] = float(run["updated_at"] or open_step["started_at"])
                open_step["duration_ms"] = max(0, int((open_step["ended_at"] - open_step["started_at"]) * 1000))
                steps.append(open_step)
            started = float(run["started_at"] or 0.0)
            finished = float(run["updated_at"] or started)
            timeline.append({
                "run_id": run["id"],
                "task": run["task"],
                "state": run["state"],
                "started_at": started,
                "duration_ms": max(0, int((finished - started) * 1000)),
                "steps": steps,
            })
        return timeline

    def agent_conversation(self, run_id: str = "", limit: int = 200) -> list[dict[str, Any]]:
        """Every note the agents wrote, newest run first when no run is named."""

        bounded = max(1, min(int(limit), 1000))
        if run_id:
            rows = self.connection.execute(
                "SELECT run_id,node_id,payload_json,created_at FROM events "
                "WHERE run_id=? AND kind='agent_message' ORDER BY sequence LIMIT ?",
                (run_id, bounded),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT run_id,node_id,payload_json,created_at FROM events "
                "WHERE kind='agent_message' ORDER BY sequence DESC LIMIT ?",
                (bounded,),
            ).fetchall()
            rows = list(reversed(rows))
        notes: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            notes.append({
                "run_id": row["run_id"],
                "sequence": payload.get("sequence"),
                "from": payload.get("from") or row["node_id"],
                "to": payload.get("to"),
                "subject": payload.get("subject"),
                "created_at": row["created_at"],
            })
        return notes

    def record_agent_prompt_version(
        self,
        role: str,
        system_prompt: str,
        *,
        provider: str,
        model: str,
        run_id: str,
        provider_route: str = "",
        parent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Record an inactive agent system-prompt version and one deduplicated observation."""
        identifiers = {
            "role": role,
            "provider": provider,
            "model": model,
            "run_id": run_id,
            "provider_route": provider_route,
        }
        for field, value in identifiers.items():
            if not isinstance(value, str) or (field != "provider_route" and not value.strip()) or len(value) > 512:
                raise HarnessError(f"Agent prompt {field} must be a string of at most 512 characters")
        if not isinstance(system_prompt, str) or len(system_prompt) > 1_048_576:
            raise HarnessError("Agent system prompt must be a string of at most 1048576 characters")
        if parent_id is not None and (not isinstance(parent_id, str) or not parent_id or len(parent_id) > 256):
            raise HarnessError("Agent prompt parent_id is invalid")
        if metadata is not None and not isinstance(metadata, dict):
            raise HarnessError("Agent prompt metadata must be an object")

        safe_identifiers = {key: self.redact_text(value.strip()) for key, value in identifiers.items()}
        safe_prompt = self.redact_text(system_prompt)
        safe_metadata = self.redact_value(metadata or {})
        if not isinstance(safe_metadata, dict):
            raise HarnessError("Agent prompt metadata must remain an object after redaction")
        kind = "agent-system"
        safe_role = safe_identifiers["role"]
        content_sha256 = hashlib.sha256(safe_prompt.encode("utf-8")).hexdigest()
        version_id = "agent-prompt-" + canonical_json_sha256(
            {"kind": kind, "role": safe_role, "content_sha256": content_sha256}
        )
        now_ms = int(time.time() * 1000)
        with self.connection:
            if parent_id is not None:
                parent = self.connection.execute(
                    "SELECT id,kind,name FROM prompt_versions WHERE id=?", (parent_id,)
                ).fetchone()
                if parent is None or parent["kind"] != kind or parent["name"] != safe_role:
                    raise HarnessError("Agent prompt parent must exist in the same role lineage")
            existing = self.connection.execute(
                "SELECT id FROM prompt_versions WHERE kind=? AND name=? AND content_sha256=?",
                (kind, safe_role, content_sha256),
            ).fetchone()
            if existing is not None:
                version_id = str(existing["id"])
                self.connection.execute("UPDATE prompt_versions SET active=0 WHERE id=?", (version_id,))
            else:
                resolved_parent = parent_id
                if resolved_parent is None:
                    latest = self.connection.execute(
                        "SELECT id FROM prompt_versions WHERE kind=? AND name=? ORDER BY rowid DESC LIMIT 1",
                        (kind, safe_role),
                    ).fetchone()
                    resolved_parent = str(latest["id"]) if latest is not None else None
                version_metadata = {
                    "source": "agent-runtime",
                    "provider": safe_identifiers["provider"],
                    "model": safe_identifiers["model"],
                    "run_id": safe_identifiers["run_id"],
                    "provider_route": safe_identifiers["provider_route"],
                    "content_sha256": content_sha256,
                    "metadata": safe_metadata,
                }
                self.connection.execute(
                    "INSERT INTO prompt_versions(id,kind,name,body,parent_id,active,created_at,metadata_json,content_sha256) "
                    "VALUES(?,?,?,?,?,0,?,?,?)",
                    (
                        version_id, kind, safe_role, safe_prompt, resolved_parent, now_ms // 1000,
                        canonical_json(version_metadata), content_sha256,
                    ),
                )
            observation_metadata = {
                "prompt_id": version_id,
                "run_id": safe_identifiers["run_id"],
                "provider_route": safe_identifiers["provider_route"],
                "provider": safe_identifiers["provider"],
                "model": safe_identifiers["model"],
                "metadata": safe_metadata,
            }
            observation_sha256 = canonical_json_sha256(observation_metadata)
            self.connection.execute(
                "INSERT OR IGNORE INTO prompt_version_observations("
                "observation_sha256,prompt_id,run_id,provider_route,provider,model,metadata_json,created_at_ms"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (
                    observation_sha256, version_id, safe_identifiers["run_id"],
                    safe_identifiers["provider_route"], safe_identifiers["provider"],
                    safe_identifiers["model"], canonical_json(safe_metadata), now_ms,
                ),
            )
        return version_id

    def record_provider_usage(self, value: dict[str, Any]) -> int:
        """Store one normalized provider request for UI reporting."""
        if not isinstance(value, dict):
            raise HarnessError("Provider usage must be an object")
        token_fields = (
            "input_tokens", "output_tokens", "cached_input_tokens", "cache_write_input_tokens",
            "reasoning_tokens", "tool_use_tokens", "billed_output_tokens",
        )
        for field in token_fields:
            number = value.get(field)
            if number is not None and (isinstance(number, bool) or not isinstance(number, int) or number < 0):
                raise HarnessError(f"Provider usage {field} must be a non-negative integer")
        latency = value.get("latency_ms")
        if latency is not None and (isinstance(latency, bool) or not isinstance(latency, int) or latency < 0):
            raise HarnessError("Provider usage latency_ms must be a non-negative integer")
        micro = value.get("cost_microusd")
        if micro is not None and (isinstance(micro, bool) or not isinstance(micro, int) or micro < 0):
            raise HarnessError("Provider usage cost_microusd must be a non-negative integer")
        cost = value.get("cost_nanos")
        if cost is None and micro is not None:
            cost = str(micro * 1000)
        if cost is not None and (not isinstance(cost, str) or not re.fullmatch(r"\d+", cost)):
            raise HarnessError("Provider usage cost_nanos must be a decimal string")
        if micro is not None and cost is not None and int(cost) != micro * 1000:
            raise HarnessError("Provider usage cost_microusd and cost_nanos disagree")
        if micro is None and isinstance(cost, str):
            nanos = int(cost)
            micro = nanos // 1000 if nanos % 1000 == 0 else None
        for field in ("price_status", "price_snapshot_id"):
            text = value.get(field)
            if text is not None and (not isinstance(text, str) or len(text) > 512):
                raise HarnessError(f"Provider usage {field} must be a string of at most 512 characters")
        if value.get("price_status") == "":
            raise HarnessError("Provider usage price_status must not be empty")
        safe = self.redact_value(value)
        if not isinstance(safe, dict):
            raise HarnessError("Provider usage must remain an object after redaction")
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO provider_usage(run_id,node_id,agent_role,provider_route,provider,model,"
                "input_tokens,output_tokens,cached_input_tokens,cache_write_input_tokens,reasoning_tokens,"
                "tool_use_tokens,billed_output_tokens,latency_ms,cost_microusd,price_status,price_snapshot_id,"
                "cost_nanos,cost_basis,rate_id,created_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(safe.get("run_id", "")), str(safe.get("node_id", "")), str(safe.get("agent_role", safe.get("role", ""))),
                    str(safe.get("provider_route", safe.get("provider_profile_id", ""))), str(safe.get("provider", "")), str(safe.get("model", "")),
                    safe.get("input_tokens"), safe.get("output_tokens"), safe.get("cached_input_tokens"),
                    safe.get("cache_write_input_tokens"), safe.get("reasoning_tokens"), safe.get("tool_use_tokens"),
                    safe.get("billed_output_tokens"), safe.get("latency_ms"), micro,
                    str(safe.get("price_status", safe.get("cost_basis", "unavailable"))),
                    safe.get("price_snapshot_id", safe.get("rate_id")), cost,
                    str(safe.get("cost_basis", safe.get("price_status", "unavailable"))),
                    safe.get("rate_id", safe.get("price_snapshot_id")),
                    int(safe.get("created_at_ms", int(time.time() * 1000))),
                ),
            )
        return int(cursor.lastrowid)

    def record_graph_prompt_version(self, node: dict[str, Any], *, run_id: str = "graph-definition") -> str:
        """Compatibility adapter for inactive graph-agent prompt history."""
        if not isinstance(node, dict) or not isinstance(node.get("id"), str):
            raise HarnessError("Graph prompt node is invalid")
        config = node.get("config")
        if not isinstance(config, dict):
            raise HarnessError("Graph prompt config is invalid")
        node_id = node["id"]
        role_name = str(config.get("role_name") or node.get("type") or node_id)
        provider_route = str(config.get("provider_route") or "default")
        provider = str(config.get("provider") or provider_route)
        model = str(config.get("model") or "unspecified")
        return self.record_agent_prompt_version(
            f"graph-agent:{node_id}",
            str(config.get("system_prompt") or ""),
            provider=provider,
            model=model,
            run_id=run_id,
            provider_route=provider_route,
            metadata={
                "source": "workflow_graph",
                "node_id": node_id,
                "node_type": str(node.get("type") or ""),
                "role_name": role_name,
            },
        )

    def record_memory_provenance(
        self,
        memory_kind: str,
        memory_id: str,
        relation: str,
        run_id: str,
        node_id: str,
        provider_route: str = "",
        model: str = "",
    ) -> int:
        if memory_kind not in {"episode", "document", "prompt"} or relation not in {"discovered_by", "read_by"}:
            raise HarnessError("Memory provenance kind or relation is invalid")
        values = [memory_id, run_id, node_id, provider_route, model]
        if any(not isinstance(item, str) or not item or len(item) > 512 for item in values[:3]):
            raise HarnessError("Memory provenance identifiers are invalid")
        safe = [self.redact_text(str(item)) for item in values]
        with self.connection:
            cursor = self.connection.execute(
                "INSERT OR IGNORE INTO memory_provenance(memory_kind,memory_id,relation,run_id,node_id,provider_route,model,created_at_ms) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (memory_kind, safe[0], relation, safe[1], safe[2], safe[3], safe[4], int(time.time() * 1000)),
            )
            if cursor.rowcount:
                return int(cursor.lastrowid)
            row = self.connection.execute(
                "SELECT sequence FROM memory_provenance WHERE memory_kind=? AND memory_id=? AND relation=? AND run_id=? AND node_id=?",
                (memory_kind, safe[0], relation, safe[1], safe[2]),
            ).fetchone()
        return int(row[0]) if row else 0

    def usage_records(self, after: int = 0, limit: int = 100, run_id: str = "") -> dict[str, Any]:
        limit = max(1, min(int(limit), 500))
        clauses = ["sequence > ?"]
        params: list[Any] = [max(0, int(after))]
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        params.append(limit + 1)
        rows = self.connection.execute(
            f"SELECT * FROM provider_usage WHERE {' AND '.join(clauses)} ORDER BY sequence LIMIT ?", params
        ).fetchall()
        more = len(rows) > limit
        records = [dict(row) for row in rows[:limit]]
        return {"records": records, "next_cursor": records[-1]["sequence"] if records else max(0, int(after)), "has_more": more}

    def prompt_lineage(self, after: int = 0, limit: int = 100, name: str = "") -> dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        clauses = ["rowid > ?"]
        params: list[Any] = [max(0, int(after))]
        if name:
            clauses.append("name = ?")
            params.append(name)
        params.append(limit + 1)
        rows = self.connection.execute(
            f"SELECT rowid AS sequence,* FROM prompt_versions WHERE {' AND '.join(clauses)} ORDER BY rowid LIMIT ?", params
        ).fetchall()
        more = len(rows) > limit
        records = []
        for row in rows[:limit]:
            item = dict(row)
            try:
                item["metadata"] = json.loads(item.pop("metadata_json"))
            except (TypeError, json.JSONDecodeError):
                item["metadata"] = {}
            observations = []
            for observation in self.connection.execute(
                "SELECT sequence,observation_sha256,run_id,provider_route,provider,model,metadata_json,created_at_ms "
                "FROM prompt_version_observations WHERE prompt_id=? ORDER BY sequence DESC LIMIT 20",
                (item["id"],),
            ).fetchall():
                observed = dict(observation)
                try:
                    observed["metadata"] = json.loads(observed.pop("metadata_json"))
                except (TypeError, json.JSONDecodeError):
                    observed["metadata"] = {}
                observations.append(observed)
            item["observations"] = observations
            records.append(item)
        return {"records": records, "next_cursor": records[-1]["sequence"] if records else max(0, int(after)), "has_more": more}

    def memory_graph(self, after: int = 0, limit: int = 100, query: str = "", kind: str = "") -> dict[str, Any]:
        limit = max(1, min(int(limit), 200))
        query = self.redact_text(str(query))[:256].casefold()
        allowed = {"episode", "document", "prompt"}
        kinds = [kind] if kind in allowed else sorted(allowed)
        records: list[dict[str, Any]] = []
        if "episode" in kinds:
            for row in self.connection.execute("SELECT rowid AS sequence,id,title,body,trust,created_at FROM episodes ORDER BY rowid"):
                label = str(row["title"])
                summary = str(row["body"])[:240]
                if not query or query in f"{label} {summary}".casefold():
                    records.append({"cursor": int(row["sequence"]) * 3, "id": row["id"], "kind": "episode", "label": label, "summary": summary, "trust": row["trust"], "created_at": row["created_at"]})
        if "document" in kinds:
            for row in self.connection.execute("SELECT rowid AS sequence,path,language,indexed_at FROM documents ORDER BY rowid"):
                label = str(row["path"])
                if not query or query in label.casefold():
                    records.append({"cursor": int(row["sequence"]) * 3 + 1, "id": row["path"], "kind": "document", "label": label, "summary": str(row["language"]), "trust": None, "created_at": row["indexed_at"]})
        if "prompt" in kinds:
            for row in self.connection.execute("SELECT rowid AS sequence,id,name,body,active,created_at FROM prompt_versions ORDER BY rowid"):
                label = str(row["name"])
                summary = str(row["body"])[:240]
                if not query or query in f"{label} {summary}".casefold():
                    records.append({"cursor": int(row["sequence"]) * 3 + 2, "id": row["id"], "kind": "prompt", "label": label, "summary": summary, "active": bool(row["active"]), "trust": None, "created_at": row["created_at"]})
        records = sorted((item for item in records if int(item["cursor"]) > max(0, int(after))), key=lambda item: (item["cursor"], item["kind"], item["id"]))
        more = len(records) > limit
        selected = records[:limit]
        keys = {(item["kind"], str(item["id"])) for item in selected}
        links = []
        if keys:
            for row in self.connection.execute("SELECT * FROM memory_provenance ORDER BY sequence"):
                if (row["memory_kind"], row["memory_id"]) in keys:
                    links.append(dict(row))
        return {"nodes": selected, "links": links, "next_cursor": selected[-1]["cursor"] if selected else max(0, int(after)), "has_more": more}

    def add_episode(self, namespace: str, title: str, body: str, metadata: dict[str, Any] | None = None, vector: list[float] | None = None, trust: float = 0.5) -> str:
        episode_id = uuid.uuid4().hex
        if not self.enabled:
            return episode_id
        now = int(time.time())
        title = self.redact_text(title)
        body = self.redact_text(body)
        safe_metadata = self.redact_value(metadata or {})
        with self.connection:
            self.connection.execute(
                "INSERT INTO episodes VALUES(?,?,?,?,?,?,?,?,?)",
                (episode_id, namespace, title, body, json.dumps(safe_metadata, sort_keys=True), _pack_vector(vector), trust, now, now),
            )
            if self.has_fts:
                self.connection.execute("INSERT INTO episodes_fts(id,title,body) VALUES(?,?,?)", (episode_id, title, body))
        return episode_id

    def search_episodes(self, query: str, limit: int = 8, vector: list[float] | None = None, namespace: str | None = None) -> list[MemoryHit]:
        if not self.enabled:
            return []
        if limit <= 0:
            return []
        clauses: list[str] = []
        params: list[Any] = []
        if namespace:
            clauses.append("namespace=?")
            params.append(namespace)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        lexical: dict[str, float] = {}
        candidate_limit = max(limit * 8, 64)
        if self.has_fts and _fts_query(query):
            sql = (
                "SELECT e.id FROM episodes_fts JOIN episodes e ON e.id=episodes_fts.id "
                "WHERE episodes_fts MATCH ?"
                + (" AND e.namespace=?" if namespace else "")
                + " ORDER BY bm25(episodes_fts) LIMIT ?"
            )
            fts_params: list[Any] = [_fts_query(query)]
            if namespace:
                fts_params.append(namespace)
            fts_params.append(candidate_limit)
            for position, row in enumerate(self.connection.execute(sql, fts_params)):
                lexical[row["id"]] = 1.0 / (1.0 + position)
        hits: list[MemoryHit] = []
        lowered = query.lower()
        for row in self.connection.execute(f"SELECT * FROM episodes {where} ORDER BY id", params):
            direct = 0.2 if lowered and lowered in f"{row['title']} {row['body']}".lower() else 0.0
            semantic = _cosine(vector or [], _unpack_vector(row["embedding"]))
            if not lexical.get(row["id"]) and direct <= 0 and semantic <= 0:
                continue
            score = 0.55 * lexical.get(row["id"], 0.0) + 0.35 * max(0.0, semantic) + direct + 0.05 * float(row["trust"])
            hits.append(MemoryHit("episode", row["id"], f"{row['title']}\n{row['body']}", score, json.loads(row["metadata_json"])))
            if len(hits) > candidate_limit * 2:
                hits = _prune_hits(hits, candidate_limit)
        selected = _prune_hits(hits, limit)
        if selected:
            now = int(time.time())
            with self.connection:
                self.connection.executemany(
                    "UPDATE episodes SET accessed_at=? WHERE id=?",
                    [(now, hit.key) for hit in selected],
                )
        return selected

    def upsert_document(
        self,
        path: str,
        digest: str,
        language: str,
        content: str,
        edges: Iterable[tuple[str, str]],
        vector: list[float] | None = None,
        chunks: list[dict[str, Any]] | None = None,
        symbols: list[dict[str, Any]] | None = None,
    ) -> None:
        if not self.enabled:
            return
        now = int(time.time())
        content = self.redact_text(content)
        prepared_chunks = self.redact_value(chunks) if chunks is not None else bounded_chunks(content)
        if not isinstance(prepared_chunks, list):
            raise HarnessError("Document chunks must be a list")
        prepared_symbols = symbols or []
        with self.connection:
            if self.has_fts:
                self.connection.execute("DELETE FROM documents_fts WHERE path=?", (path,))
                self.connection.execute("DELETE FROM document_chunks_fts WHERE path=?", (path,))
            self.connection.execute("DELETE FROM document_chunks WHERE path=?", (path,))
            self.connection.execute("DELETE FROM document_symbols WHERE path=?", (path,))
            self.connection.execute(
                "INSERT INTO documents(path,sha256,language,content,embedding,indexed_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256,language=excluded.language,"
                "content=excluded.content,embedding=excluded.embedding,indexed_at=excluded.indexed_at",
                (path, digest, language, "", None, now),
            )
            self.connection.execute("DELETE FROM dependency_edges WHERE source=?", (path,))
            self.connection.executemany("INSERT OR IGNORE INTO dependency_edges(source,target,kind) VALUES(?,?,'import')", [(path, target) for _, target in edges])
            seen_digests: set[str] = set()
            stored_chunks: list[tuple[str, int, int]] = []
            for ordinal, chunk in enumerate(prepared_chunks):
                chunk_content = str(chunk.get("content", ""))
                content_digest = hashlib.sha256(chunk_content.encode()).hexdigest()
                if content_digest in seen_digests:
                    continue
                seen_digests.add(content_digest)
                start_line = int(chunk.get("start_line", 1))
                end_line = int(chunk.get("end_line", start_line))
                chunk_id = f"{path}#chunk-{ordinal:04d}"
                chunk_symbols = sorted(
                    {
                        str(item.get("qualified_name") or item.get("name"))
                        for item in prepared_symbols
                        if int(item.get("line", 1)) <= end_line and int(item.get("end_line", item.get("line", 1))) >= start_line
                    }
                )
                chunk_vector = chunk.get("embedding", vector)
                self.connection.execute(
                    "INSERT INTO document_chunks"
                    "(id,path,ordinal,start_line,end_line,content_sha256,content,embedding) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        chunk_id,
                        path,
                        ordinal,
                        start_line,
                        end_line,
                        content_digest,
                        chunk_content,
                        _pack_vector(chunk_vector if isinstance(chunk_vector, list) else None),
                    ),
                )
                stored_chunks.append((chunk_id, start_line, end_line))
                if self.has_fts:
                    self.connection.execute(
                        "INSERT INTO document_chunks_fts(id,path,symbols,content) VALUES(?,?,?,?)",
                        (chunk_id, path, " ".join(chunk_symbols), chunk_content),
                    )
            for symbol in prepared_symbols:
                line = int(symbol.get("line", 1))
                end_line = int(symbol.get("end_line", line))
                chunk_id = next(
                    (identifier for identifier, start, end in stored_chunks if start <= line <= end),
                    stored_chunks[0][0] if stored_chunks else f"{path}#chunk-0000",
                )
                self.connection.execute(
                    "INSERT OR IGNORE INTO document_symbols"
                    "(path,name,qualified_name,kind,line,end_line,chunk_id) VALUES(?,?,?,?,?,?,?)",
                    (
                        path,
                        str(symbol.get("name", "")),
                        str(symbol.get("qualified_name") or symbol.get("name", "")),
                        str(symbol.get("kind", "symbol")),
                        line,
                        end_line,
                        chunk_id,
                    ),
                )

    def remove_documents_not_in(self, paths: set[str]) -> None:
        if not self.enabled:
            return
        existing = {row[0] for row in self.connection.execute("SELECT path FROM documents")}
        with self.connection:
            for path in existing - paths:
                self.connection.execute("DELETE FROM documents WHERE path=?", (path,))
                self.connection.execute("DELETE FROM document_chunks WHERE path=?", (path,))
                self.connection.execute("DELETE FROM document_symbols WHERE path=?", (path,))
                self.connection.execute("DELETE FROM dependency_edges WHERE source=?", (path,))
                if self.has_fts:
                    self.connection.execute("DELETE FROM documents_fts WHERE path=?", (path,))
                    self.connection.execute("DELETE FROM document_chunks_fts WHERE path=?", (path,))

    def document_hash(self, path: str) -> str | None:
        if not self.enabled:
            return None
        row = self.connection.execute("SELECT sha256 FROM documents WHERE path=?", (path,)).fetchone()
        return row[0] if row else None

    def document_needs_embedding(self, path: str) -> bool:
        if not self.enabled:
            return False
        row = self.connection.execute(
            "SELECT EXISTS(SELECT 1 FROM document_chunks WHERE path=? AND embedding IS NULL)", (path,)
        ).fetchone()
        return bool(row and row[0])

    def search_documents(self, query: str, limit: int = 12, vector: list[float] | None = None) -> list[MemoryHit]:
        if not self.enabled:
            return []
        if limit <= 0:
            return []
        candidate_limit = max(limit * 8, 64)
        lexical: dict[str, float] = {}
        symbol_scores: dict[str, float] = {}
        rows_by_id: dict[str, sqlite3.Row] = {}
        fts = _fts_query(query)
        if self.has_fts and fts:
            rows = self.connection.execute(
                "SELECT c.*,d.language FROM document_chunks_fts "
                "JOIN document_chunks c ON c.id=document_chunks_fts.id "
                "JOIN documents d ON d.path=c.path "
                "WHERE document_chunks_fts MATCH ? "
                "ORDER BY bm25(document_chunks_fts,0.0,0.0,3.0,1.0),c.path,c.ordinal",
                (fts,),
            )
            lexical_digests: set[str] = set()
            for row in rows:
                if row["content_sha256"] in lexical_digests:
                    continue
                lexical_digests.add(row["content_sha256"])
                rows_by_id[row["id"]] = row
                lexical[row["id"]] = 1.0 / len(lexical_digests)
                if len(lexical_digests) >= candidate_limit:
                    break
        lowered = query.casefold().strip()
        if lowered:
            symbol_rows = self.connection.execute(
                "SELECT s.chunk_id,c.*,d.language,s.name,s.qualified_name FROM document_symbols s "
                "JOIN document_chunks c ON c.id=s.chunk_id JOIN documents d ON d.path=s.path "
                "WHERE lower(s.name)=? OR lower(s.qualified_name)=? OR lower(s.qualified_name) LIKE ? "
                "ORDER BY s.path,s.line",
                (lowered, lowered, f"%.{lowered}"),
            )
            symbol_digests: set[str] = set()
            for row in symbol_rows:
                if row["content_sha256"] in symbol_digests:
                    continue
                symbol_digests.add(row["content_sha256"])
                rows_by_id[row["id"]] = row
                exact = lowered in {str(row["name"]).casefold(), str(row["qualified_name"]).casefold()}
                symbol_scores[row["id"]] = max(symbol_scores.get(row["id"], 0.0), 0.3 if exact else 0.18)
                if len(symbol_digests) >= candidate_limit:
                    break
            direct_rows = self.connection.execute(
                "SELECT c.*,d.language FROM document_chunks c JOIN documents d ON d.path=c.path "
                "WHERE lower(c.content) LIKE ? OR lower(c.path) LIKE ? ORDER BY c.path,c.ordinal",
                (f"%{lowered}%", f"%{lowered}%"),
            )
            direct_digests: set[str] = set()
            for row in direct_rows:
                if row["content_sha256"] in direct_digests:
                    continue
                direct_digests.add(row["content_sha256"])
                rows_by_id[row["id"]] = row
                lexical.setdefault(row["id"], 0.5)
                if len(direct_digests) >= candidate_limit:
                    break

        def make_hit(row: sqlite3.Row) -> MemoryHit | None:
            semantic = _cosine(vector or [], _unpack_vector(row["embedding"]))
            direct = 0.1 if lowered and lowered in f"{row['path']} {row['content']}".casefold() else 0.0
            score = (
                0.55 * lexical.get(row["id"], 0.0)
                + 0.35 * max(0.0, semantic)
                + direct
                + symbol_scores.get(row["id"], 0.0)
            )
            if score <= 0:
                return None
            return MemoryHit(
                "document",
                row["path"],
                row["content"],
                score,
                {
                    "language": row["language"],
                    "semantic_score": semantic,
                    "chunk_id": row["id"],
                    "start_line": row["start_line"],
                    "end_line": row["end_line"],
                    "content_sha256": row["content_sha256"],
                    "symbols": [],
                },
            )

        hits: list[MemoryHit] = []
        for row in rows_by_id.values():
            hit = make_hit(row)
            if hit is not None:
                hits.append(hit)
        if vector:
            vector_rows = self.connection.execute(
                "SELECT c.*,d.language FROM document_chunks c JOIN documents d ON d.path=c.path "
                "WHERE c.embedding IS NOT NULL ORDER BY c.path,c.ordinal"
            )
            for row in vector_rows:
                if row["id"] in rows_by_id:
                    continue
                hit = make_hit(row)
                if hit is not None:
                    hits.append(hit)
                if len(hits) > candidate_limit * 2:
                    hits = _prune_hits(hits, candidate_limit, dedupe_content=True)
        selected_hits = _prune_hits(hits, limit, dedupe_content=True)
        enriched: list[MemoryHit] = []
        for hit in selected_hits:
            metadata = dict(hit.metadata)
            metadata["symbols"] = [
                symbol_row[0]
                for symbol_row in self.connection.execute(
                    "SELECT qualified_name FROM document_symbols WHERE chunk_id=? ORDER BY line,qualified_name",
                    (metadata["chunk_id"],),
                )
            ]
            enriched.append(MemoryHit(hit.source, hit.key, hit.text, hit.score, metadata))
        return enriched

    @staticmethod
    def _dependency_aliases(path: str) -> set[str]:
        normalized = path.replace("\\", "/").removeprefix("./")
        stemmed = normalized.rsplit(".", 1)[0] if "." in normalized.rsplit("/", 1)[-1] else normalized
        basename = normalized.rsplit("/", 1)[-1]
        base_stem = basename.rsplit(".", 1)[0]
        aliases = {normalized, stemmed, basename, base_stem, stemmed.replace("/", ".")}
        if normalized.endswith("/__init__.py"):
            package = normalized[: -len("/__init__.py")]
            aliases.update({package, package.replace("/", "."), package.rsplit("/", 1)[-1]})
        return {alias.casefold() for alias in aliases if alias}

    def dependency_documents(self, seed_paths: Iterable[str], limit: int = 8) -> list[MemoryHit]:
        if not self.enabled:
            return []
        seeds = {str(path) for path in seed_paths}
        if not seeds or limit <= 0:
            return []
        path_rows = self.connection.execute("SELECT path FROM documents ORDER BY path").fetchall()
        aliases: dict[str, set[str]] = {
            row["path"]: self._dependency_aliases(row["path"]) for row in path_rows
        }
        alias_to_paths: dict[str, set[str]] = {}
        for path, values in aliases.items():
            for value in values:
                alias_to_paths.setdefault(value, set()).add(path)
        related: dict[str, list[str]] = {}
        for edge in self.connection.execute("SELECT source,target,kind FROM dependency_edges"):
            target_aliases = self._dependency_aliases(edge["target"])
            resolved_targets: set[str] = set()
            for alias in target_aliases:
                resolved_targets.update(alias_to_paths.get(alias, set()))
            if edge["source"] in seeds:
                for target in resolved_targets:
                    if target not in seeds:
                        related.setdefault(target, []).append(f"{edge['source']} -> {edge['target']}")
            if resolved_targets & seeds and edge["source"] not in seeds:
                related.setdefault(edge["source"], []).append(f"{edge['source']} -> {edge['target']}")
        if not related:
            return []
        selected = sorted(related, key=lambda path: (-len(related[path]), path))[:limit]
        placeholders = ",".join("?" for _ in selected)
        rows = self.connection.execute(
            f"SELECT c.*,d.language FROM document_chunks c JOIN documents d ON d.path=c.path "
            f"WHERE c.path IN ({placeholders}) ORDER BY c.path,c.ordinal",
            selected,
        ).fetchall()
        by_path: dict[str, sqlite3.Row] = {}
        for row in rows:
            by_path.setdefault(row["path"], row)
        hits = []
        for path in selected:
            row = by_path.get(path)
            if row is None:
                continue
            symbols = [
                item[0]
                for item in self.connection.execute(
                    "SELECT qualified_name FROM document_symbols WHERE chunk_id=? ORDER BY line,qualified_name",
                    (row["id"],),
                )
            ]
            hits.append(
                MemoryHit(
                    "dependency",
                    path,
                    row["content"],
                    0.45 + min(0.2, 0.02 * len(related[path])),
                    {
                        "language": row["language"],
                        "relations": related[path][:8],
                        "chunk_id": row["id"],
                        "start_line": row["start_line"],
                        "end_line": row["end_line"],
                        "content_sha256": row["content_sha256"],
                        "symbols": symbols,
                    },
                )
            )
        return _prune_hits(hits, limit, dedupe_content=True)

    def integrity(self) -> str:
        row = self.connection.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "unknown"

    def stats(self, retention_days: int = 180) -> dict[str, Any]:
        cutoff = int(time.time()) - retention_days * 86400
        counts = {}
        for table in (
            "runs",
            "events",
            "episodes",
            "documents",
            "document_chunks",
            "document_symbols",
            "dependency_edges",
            "prompt_versions",
            "prompt_version_observations",
            "refinement_candidates",
            "review_packets",
            "agent_tool_journal",
            "provider_usage",
            "memory_provenance",
        ):
            counts[table] = int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        old_episodes = int(self.connection.execute("SELECT COUNT(*) FROM episodes WHERE created_at < ?", (cutoff,)).fetchone()[0])
        old_runs = int(self.connection.execute("SELECT COUNT(*) FROM runs WHERE updated_at < ?", (cutoff,)).fetchone()[0])
        return {
            "enabled": self.enabled,
            "persistent": self.enabled,
            "database": str(self.path) if self.path is not None else ":memory:",
            "bytes": self.path.stat().st_size if self.path is not None and self.path.exists() else 0,
            "integrity": self.integrity(),
            "counts": counts,
            "retention_days": retention_days,
            "curation_candidates": {"episodes": old_episodes, "runs": old_runs},
            "note": (
                "Curation is read-only; no records were deleted."
                if self.enabled
                else "Memory is disabled; run and event state exists only in this process and source memory is not indexed or retained."
            ),
        }
