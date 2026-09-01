"""Durable, event-driven long-horizon work for the agent board.

This is intentionally not a group-chat loop.  A goal owns a small dependency
graph of concrete tasks.  Ready tasks are claimed by useful agents, agents may
delegate or request a targeted review, and deterministic evidence decides when
the goal is finished.  LangGraph supplies the resumable scheduler and interrupt
boundary; the authenticated goal store is the UI-facing source of truth.
"""

from __future__ import annotations

import copy
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from typing import Any, Callable, TypedDict
import uuid

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from . import chat as chat_lab
from . import user_questions
from .changes import FileTransaction
from .config import LoadedConfig
from .models import HarnessError, ProviderOutcomeUnknown, ResponseFormat
from .pipeline_runs import _owner_is_alive, _process_token, inspect_project_authority, project_identity
from .redaction import CredentialRedactor
from .runtime_integrity import mac, quarantine_marker
from .swarm_runs import _base
from . import swarm_work


SCHEMA_VERSION = 2
EVENT_SCHEMA_VERSION = 1
REQUEST_TOMBSTONE_SCHEMA_VERSION = 1
AGENT_BINDING_SCHEMA_VERSION = 3
MAX_GOALS = 40
MAX_TASKS = 200
MAX_EVENTS = 4_000
MAX_PARALLEL = 3
MAX_PROVIDER_CALLS = 1_000
MAX_CONTEXT_TOOL_CALLS = 500
MAX_NO_PROGRESS = 4
MAX_CRITERIA = 32
MAX_OBJECTIVE_CHARACTERS = 240_000
MAX_PENDING_ACTION_BYTES = 8_000_000
MAX_REQUEST_ID_CHARACTERS = 160
MAX_CONVERSATION_ID_CHARACTERS = (
    chat_lab.DIRECT_LONG_HORIZON_CHAT_ID_CHARACTERS
)
TERMINAL_GOALS = {"complete", "cancelled", "failed"}
RELEASED_GOALS = {"complete", "cancelled"}
PROJECT_OWNER_GOALS = {
    "queued", "running", "paused", "waiting_for_user", "failed", "cancelling",
}
ACTIVE_GOALS = PROJECT_OWNER_GOALS | {"waiting_for_project"}
EXECUTION_CONTRACT_SCHEMA_VERSION = 1
PROJECT_QUEUE_SCHEMA_VERSION = 1
CANCELLATION_SCHEMA_VERSION = 1
SCHEDULER_LEASE_SCHEMA_VERSION = 1
_NO_MUTATION = object()
TASK_STATES = {
    "ready", "running", "pending_apply", "waiting", "waiting_review", "blocked", "failed",
    "complete", "cancelled",
}
INTERRUPT_REASONS = {
    "requirement_ambiguity", "new_authority", "risky_action",
    "missing_access", "unresolved_blocker",
}


AGENT_ACTION_FORMAT = ResponseFormat("nexus_long_horizon_action_v1", {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "work", "complete", "delegate", "handoff",
                "request_review", "ask_user", "blocked",
            ],
        },
        "summary": {"type": "string", "maxLength": 8_000},
        "evidence": {"type": "array", "maxItems": 24,
                     "items": {"type": "string", "maxLength": 1_000}},
        "risk": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
        "changes": copy.deepcopy(swarm_work.WORK_FORMAT.schema["properties"]["changes"]),
        "needs_files": {"type": "array", "maxItems": 16,
                        "items": {"type": "string", "maxLength": 240}},
        "tool_calls": copy.deepcopy(swarm_work.WORK_FORMAT.schema["properties"]["tool_calls"]),
        "tasks": {
            "type": "array", "maxItems": 12,
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "maxLength": 240},
                    "description": {"type": "string", "maxLength": 2_000},
                    "assigned_agent_id": {"type": "string", "maxLength": 160},
                    "depends_on": {"type": "array", "maxItems": 20,
                                   "items": {"type": "string", "maxLength": 160}},
                    "parallel_safe": {"type": "boolean"},
                    "resource_paths": {"type": "array", "maxItems": 20,
                                       "items": {"type": "string", "maxLength": 240}},
                },
                "required": ["title", "description", "assigned_agent_id",
                             "depends_on", "parallel_safe", "resource_paths"],
                "additionalProperties": False,
            },
        },
        "handoff_agent_id": {"type": "string", "maxLength": 160},
        "questions": copy.deepcopy(user_questions.QUESTIONS_SCHEMA),
        "interrupt_reason": {"type": "string", "enum": sorted(INTERRUPT_REASONS)},
        "criteria_evidence": {
            "type": "array", "maxItems": 24,
            "items": {
                "type": "object",
                "properties": {
                    "criterion": {"type": "string", "maxLength": 1_000},
                    "evidence_refs": {"type": "array", "maxItems": 20,
                                      "items": {"type": "string", "maxLength": 500}},
                },
                "required": ["criterion", "evidence_refs"],
                "additionalProperties": False,
            },
        },
        "review_verdict": {
            "type": "string",
            "enum": ["approve", "reject", "changes_requested"],
        },
        "review_findings": {"type": "array", "maxItems": 24,
                            "items": {"type": "string", "maxLength": 2_000}},
    },
    "required": ["action", "summary", "evidence", "risk", "changes",
                 "needs_files", "tasks", "handoff_agent_id", "questions", "criteria_evidence"],
    "additionalProperties": False,
})
AGENT_ACTION_FORMAT.schema["properties"]["tool_calls"]["items"]["properties"]["name"]["enum"].append(
    "read_proposed_change"
)


class GoalGraphState(TypedDict, total=False):
    goal_id: str
    task_ids: list[str]
    actions: list[dict[str, Any]]
    route: str
    interrupt_ids: list[str]


def _now() -> int:
    return int(time.time() * 1000)


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _short(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _exact_request_id(value: object, *, what: str = "request") -> str:
    """Validate an idempotency identity without creating prefix aliases."""

    raw = str(value or "")
    exact = raw.strip()
    if not exact:
        raise HarnessError(f"A stable {what} ID is required")
    if exact != raw:
        raise HarnessError(
            f"A long-horizon {what} ID may not contain surrounding whitespace. "
            "Nexus did not normalize it."
        )
    if len(exact) > MAX_REQUEST_ID_CHARACTERS:
        raise HarnessError(
            f"A long-horizon {what} ID may contain at most "
            f"{MAX_REQUEST_ID_CHARACTERS} characters. Nexus did not truncate it."
        )
    return exact


def _exact_conversation_id(value: object) -> str:
    """Keep an accepted chat identity exact instead of creating prefix aliases."""

    exact = str(value or "")
    if len(exact) > MAX_CONVERSATION_ID_CHARACTERS:
        raise HarnessError(
            "A long-horizon chat identity may contain at most "
            f"{MAX_CONVERSATION_ID_CHARACTERS} characters. Nexus did not truncate it."
        )
    return exact


def _stable_id(prefix: str, *values: object) -> str:
    material = "\0".join(str(one) for one in values)
    return prefix + "-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _provider_identity(agent: dict[str, Any] | None) -> str:
    """Hash the effective physical dispatch identity captured at admission.

    Route names are aliases. Two aliases can resolve to the same executable,
    provider profile, model, account, and adapter contract. Missing or legacy
    bindings deliberately fail closed instead of looking independent.
    """

    binding = agent.get("route_binding") if isinstance(agent, dict) else None
    if not isinstance(binding, dict) or binding.get(
        "binding_schema_version"
    ) != AGENT_BINDING_SCHEMA_VERSION:
        return ""
    version = binding.get("provider_principal_version")
    contract = str(binding.get("provider_principal_contract") or "")
    fingerprint = str(binding.get("provider_principal_fingerprint_sha256") or "")
    if version is None or version == "" or not contract \
            or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        return ""
    return hashlib.sha256(_canonical({
        "version": version,
        "contract": contract,
        "fingerprint_sha256": fingerprint,
    }).encode("utf-8")).hexdigest()


def _providers_independent(
    one: dict[str, Any] | None, other: dict[str, Any] | None,
) -> bool:
    left = _provider_identity(one)
    right = _provider_identity(other)
    return bool(left and right) and not hmac.compare_digest(left, right)


def _project_key(path: Path) -> str:
    return hashlib.sha256(os.path.normcase(str(path.resolve())).encode("utf-8")).hexdigest()


def _exclusive_project_contract(project_path: Path, project_authority_id: str) -> dict[str, Any]:
    basis = {
        "schema_version": EXECUTION_CONTRACT_SCHEMA_VERSION,
        "mode": "exclusive_project",
        "project_authority_id": str(project_authority_id),
        "root_fingerprint_sha256": _project_key(project_path),
    }
    return {
        **basis,
        "fingerprint_sha256": hashlib.sha256(
            _canonical(basis).encode("utf-8")
        ).hexdigest(),
    }


def _redacted_objectives(
    redactor: CredentialRedactor, objectives: list[str],
) -> list[str]:
    """Return every accepted objective without silently clipping user text."""

    accepted: list[str] = []
    for raw in objectives:
        text = redactor.text(str(raw or ""))
        if text.strip():
            accepted.append(text)
    if len("\n\n".join(accepted)) > MAX_OBJECTIVE_CHARACTERS:
        raise HarnessError(
            "The combined goal text is too large for one bounded long-horizon goal"
        )
    return accepted


def _goal_admission_digest(
    redactor: CredentialRedactor, *, project_id: str, project_path: Path,
    project_authority_id: str, conversation_id: str, participant_ids: list[str],
    lead_id: str, objectives: list[str], success_criteria: list[str] | None,
    policy: dict[str, Any] | None, attachments: object,
    agent_bindings: list[dict[str, Any]] | None = None,
) -> str:
    """Bind an idempotency key to the exact user-authorized goal intent."""

    payload = {
        "project_id": str(project_id),
        "project_path": str(project_path.resolve()),
        "project_authority_id": str(project_authority_id),
        "conversation_id": _exact_conversation_id(conversation_id),
        "participant_ids": list(dict.fromkeys(str(one) for one in participant_ids if str(one))),
        "lead_id": str(lead_id),
        "agent_bindings": copy.deepcopy(agent_bindings or []),
        "execution_contract": _exclusive_project_contract(
            project_path, project_authority_id,
        ),
        "objectives": _redacted_objectives(redactor, objectives),
        "success_criteria": [
            _short(redactor.text(one), 1_000)
            for one in (success_criteria or []) if _short(one, 1_000)
        ],
        "policy": copy.deepcopy(policy or {}),
        "attachments": copy.deepcopy(attachments or []),
    }
    try:
        canonical = _canonical(payload)
    except (TypeError, ValueError) as exc:
        raise HarnessError("Long-horizon admission inputs must be JSON-compatible") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _path_baseline_marker(root: Path, relative: str) -> str:
    path = swarm_work.confined_path(root, relative, allow_missing=True)
    if path.is_symlink():
        try:
            return "symlink:" + os.readlink(path)
        except OSError:
            return "unreadable"
    if path.is_file():
        return "file:" + str(swarm_work.file_sha256(path) or "unreadable")
    return "other" if path.exists() else "missing"


def _project_baseline_manifest(root: Path) -> dict[str, str]:
    """Hash the useful source surface once per task, excluding dependency/build trees."""
    manifest: dict[str, str] = {}
    skipped = {".git", ".harness", "node_modules", ".venv", "venv", "dist", "build"}
    for folder, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(one for one in directories if one not in skipped)
        base = Path(folder)
        for name in sorted(files):
            path = base / name
            try:
                relative = path.relative_to(root).as_posix()
                manifest[relative] = _path_baseline_marker(root, relative)
            except (OSError, ValueError, HarnessError):
                continue
    return manifest


def _bounded_json(value: object, limit: int = 32_000) -> object:
    raw = _canonical(value)
    if len(raw.encode("utf-8")) <= limit:
        return copy.deepcopy(value)
    return {"truncated": True, "summary": _short(raw, max(100, limit // 2))}


def _semantic_artifact(value: object) -> object:
    """Remove execution identity and timestamps from progress comparisons."""
    if not isinstance(value, dict):
        return value
    kind = str(value.get("kind") or "")
    if kind == "file_transaction":
        return {
            "kind": kind,
            "patch_sha256": str(value.get("patch_sha256") or ""),
            "changes": [
                {
                    "path": str(one.get("path") or ""),
                    "before_sha256": one.get("before_sha256"),
                    "after_sha256": one.get("after_sha256"),
                    "delete": one.get("delete") is True,
                }
                for one in value.get("changes", []) if isinstance(one, dict)
            ],
        }
    if kind == "verified_no_change":
        return {
            "kind": kind,
            "tree_merkle": str(value.get("tree_merkle") or ""),
            "file_count": int(value.get("file_count") or 0),
        }
    return {
        key: copy.deepcopy(one) for key, one in value.items()
        if key not in {"transaction_id", "created_at", "created_ms", "observed_at_ms", "updated_ms"}
    }


def _task_has_unsettled_effect(task: dict[str, Any]) -> bool:
    return bool(
        task.get("pending_action") or task.get("pending_transaction")
        or task.get("reconciliation_required")
    ) or str(
        task.get("provider_effect_state") or ""
    ) in {
        "dispatched", "acknowledged", "outcome_unknown", "context_step_acknowledged",
        "reply_received", "reply_received_reconciliation_required",
    }


def _durable_evidence(value: object, *, string_limit: int = 32_000, list_limit: int = 500) -> object:
    """Bound noisy leaves without destroying the structured evidence envelope."""
    if isinstance(value, dict):
        return {str(key): _durable_evidence(one, string_limit=string_limit, list_limit=list_limit)
                for key, one in value.items()}
    if isinstance(value, list):
        kept = [
            _durable_evidence(one, string_limit=string_limit, list_limit=list_limit)
            for one in value[:list_limit]
        ]
        if len(value) > list_limit:
            kept.append({"truncated_items": len(value) - list_limit})
        return kept
    if isinstance(value, str) and len(value) > string_limit:
        half = max(1, string_limit // 2)
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return (
            value[:half]
            + f"\n...[truncated {len(value) - (half * 2):,} characters; sha256 {digest}]...\n"
            + value[-half:]
        )
    return copy.deepcopy(value)


def _validate_action_semantics(action: dict[str, Any], task: dict[str, Any]) -> None:
    kind = str(action.get("action") or "")
    changes = action.get("changes") or []
    questions = action.get("questions") or []
    delegated = action.get("tasks") or []
    handoff = str(action.get("handoff_agent_id") or "")
    if changes and kind not in {"work", "complete", "request_review"}:
        raise HarnessError(f"The {kind or 'unknown'} action cannot also change project files")
    if questions and kind != "ask_user":
        raise HarnessError("Structured questions are allowed only in an ask_user action")
    if delegated and kind != "delegate":
        raise HarnessError("Delegated tasks are allowed only in a delegate action")
    if handoff and kind != "handoff":
        raise HarnessError("A handoff target is allowed only in a handoff action")
    if task.get("kind") == "review":
        if changes:
            raise HarnessError("Independent review is read-only and cannot change project files")
        if kind not in {"complete", "blocked"}:
            raise HarnessError("A review task must return a read-only approve or reject verdict")


class GoalStore:
    """Authenticated snapshots plus a strictly ordered typed event journal."""

    def __init__(
        self, config: LoadedConfig, *, migrate_execution_metadata: bool = True,
    ) -> None:
        self.config = config
        self.redactor = CredentialRedactor(config)
        self.authority_key = _project_key(config.project_root)
        self.root = _base()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "long-horizon.sqlite3"
        self.checkpoints = self.root / "long-horizon-checkpoints.sqlite3"
        project = config.project_root.resolve()
        runtime = self.root.resolve()
        if runtime == project or project in runtime.parents or runtime in project.parents:
            raise HarnessError("Long-horizon runtime storage must be outside project authority")
        self.lock = threading.RLock()
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS long_goals(
                  goal_id TEXT PRIMARY KEY,
                  request_id TEXT NOT NULL UNIQUE,
                  project_key TEXT NOT NULL,
                  status TEXT NOT NULL,
                  revision INTEGER NOT NULL,
                  document_json TEXT NOT NULL,
                  document_sha256 TEXT NOT NULL,
                  integrity_mac TEXT NOT NULL,
                  created_ms INTEGER NOT NULL,
                  updated_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS long_goal_events(
                  goal_id TEXT NOT NULL,
                  seq INTEGER NOT NULL,
                  event_id TEXT NOT NULL UNIQUE,
                  type TEXT NOT NULL,
                  event_json TEXT NOT NULL,
                  event_sha256 TEXT NOT NULL,
                  integrity_mac TEXT NOT NULL,
                  PRIMARY KEY(goal_id, seq),
                  FOREIGN KEY(goal_id) REFERENCES long_goals(goal_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS long_goal_request_tombstones(
                  request_id TEXT PRIMARY KEY,
                  tombstone_json TEXT NOT NULL,
                  tombstone_sha256 TEXT NOT NULL,
                  integrity_mac TEXT NOT NULL,
                  retired_ms INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS long_goals_updated
                  ON long_goals(updated_ms DESC);
                CREATE INDEX IF NOT EXISTS long_goals_status_created
                  ON long_goals(status,created_ms,goal_id);
            """)
        if migrate_execution_metadata:
            self._migrate_execution_metadata()

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.database, timeout=30.0, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    def _decode_shared(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        raw = str(row["document_json"])
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        material = [
            str(row["goal_id"]), str(row["request_id"]), str(row["project_key"]),
            str(row["status"]), int(row["revision"]), raw, digest,
            int(row["created_ms"]), int(row["updated_ms"]),
        ]
        if digest != str(row["document_sha256"]) or not hmac.compare_digest(
            str(row["integrity_mac"]), mac("long-horizon-goal-v1", material)
        ):
            quarantine_marker("long-horizon", self.database, "Goal integrity failed")
            raise HarnessError("Long-horizon goal state failed integrity verification")
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HarnessError("Long-horizon goal state is unreadable") from exc
        if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
            raise HarnessError("Long-horizon goal state has an unsupported schema")
        if str(document.get("goal_id")) != str(row["goal_id"]):
            raise HarnessError("Long-horizon goal identity does not match its record")
        return document

    def _decode(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        document = self._decode_shared(row)
        if document is None:
            return None
        if str(document.get("authority_key") or "") != self.authority_key:
            raise HarnessError("That long-horizon goal belongs to a different Nexus project authority")
        return document

    def _decode_request_tombstone(
        self, row: sqlite3.Row | None,
    ) -> dict[str, Any] | None:
        if row is None:
            return None
        raw = str(row["tombstone_json"])
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        material = [
            str(row["request_id"]), raw, digest, int(row["retired_ms"]),
        ]
        if digest != str(row["tombstone_sha256"]) or not hmac.compare_digest(
            str(row["integrity_mac"]),
            mac("long-horizon-request-tombstone-v1", material),
        ):
            quarantine_marker(
                "long-horizon-request-tombstones", self.database,
                "Request tombstone integrity failed",
            )
            raise HarnessError(
                "Long-horizon request replay protection failed integrity verification"
            )
        try:
            document = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HarnessError(
                "Long-horizon request replay protection is unreadable"
            ) from exc
        if not isinstance(document, dict) \
                or document.get("request_tombstone_schema_version") \
                != REQUEST_TOMBSTONE_SCHEMA_VERSION \
                or document.get("request_tombstone") is not True:
            raise HarnessError(
                "Long-horizon request replay protection has an unsupported schema"
            )
        if str(document.get("request_id") or "") != str(row["request_id"]):
            raise HarnessError(
                "Long-horizon request replay identity does not match its record"
            )
        if str(document.get("authority_key") or "") != self.authority_key:
            raise HarnessError(
                "That long-horizon request belongs to a different Nexus project authority"
            )
        if str(document.get("status") or "") not in RELEASED_GOALS:
            raise HarnessError(
                "Long-horizon request replay protection has a non-terminal state"
            )
        return document

    @staticmethod
    def _request_tombstone_document(
        document: dict[str, Any], retired_ms: int,
    ) -> dict[str, Any]:
        """Keep identity/binding evidence after detailed terminal history is pruned."""

        stored_request_id = str(document.get("request_id") or "")
        client_request_id = str(document.get("client_request_id") or "")
        if not client_request_id and ":" in stored_request_id:
            client_request_id = stored_request_id.split(":", 1)[1]
        return {
            "request_tombstone_schema_version": REQUEST_TOMBSTONE_SCHEMA_VERSION,
            "request_tombstone": True,
            "goal_id": str(document.get("goal_id") or ""),
            "request_id": stored_request_id,
            "client_request_id": client_request_id,
            "authority_key": str(document.get("authority_key") or ""),
            "status": str(document.get("status") or ""),
            "retired_ms": retired_ms,
            "admission_digest": str(document.get("admission_digest") or ""),
            "project": {
                "id": str(
                    document.get("project", {}).get("id")
                    if isinstance(document.get("project"), dict) else ""
                ),
            },
            "conversation_id": str(document.get("conversation_id") or ""),
            "requested_agent_ids": [
                str(one) for one in document.get("requested_agent_ids", [])
                if str(one)
            ],
            "lead_agent_id": str(document.get("lead_agent_id") or ""),
            "parent_goal_id": str(document.get("parent_goal_id") or ""),
        }

    def _remember_request_tombstone(
        self, db: sqlite3.Connection, document: dict[str, Any],
    ) -> dict[str, Any]:
        if str(document.get("status") or "") not in RELEASED_GOALS:
            raise HarnessError(
                "Only released long-horizon goals can become replay tombstones"
            )
        request_id = str(document.get("request_id") or "")
        existing = self._decode_request_tombstone(db.execute(
            "SELECT * FROM long_goal_request_tombstones WHERE request_id=?",
            (request_id,),
        ).fetchone())
        if existing is not None:
            if str(existing.get("goal_id") or "") != str(document.get("goal_id") or "") \
                    or not hmac.compare_digest(
                        str(existing.get("admission_digest") or ""),
                        str(document.get("admission_digest") or ""),
                    ):
                raise HarnessError(
                    "A retired long-horizon request identity conflicts with its "
                    "authenticated replay tombstone"
                )
            return existing
        retired_ms = _now()
        tombstone = self._request_tombstone_document(document, retired_ms)
        raw = _canonical(tombstone)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        material = [request_id, raw, digest, retired_ms]
        db.execute(
            "INSERT INTO long_goal_request_tombstones(request_id,tombstone_json,"
            "tombstone_sha256,integrity_mac,retired_ms) VALUES(?,?,?,?,?)",
            (
                request_id, raw, digest,
                mac("long-horizon-request-tombstone-v1", material), retired_ms,
            ),
        )
        return tombstone

    def _prune_released_goals(self, db: sqlite3.Connection) -> None:
        """Retire request identities before bounded terminal details disappear."""

        old = db.execute(
            "SELECT * FROM long_goals WHERE status IN ('complete','cancelled') "
            "AND request_id LIKE ? "
            "ORDER BY updated_ms DESC,created_ms DESC,rowid DESC",
            (self.authority_key + ":%",),
        ).fetchall()
        for row in old[MAX_GOALS:]:
            released = self._decode(row)
            if released is None:
                raise HarnessError(
                    "A terminal goal disappeared before replay protection was saved"
                )
            self._remember_request_tombstone(db, released)
            if db.execute(
                "DELETE FROM long_goals WHERE goal_id=?",
                (str(row["goal_id"]),),
            ).rowcount != 1:
                raise HarnessError(
                    "A terminal goal changed while replay protection was being saved"
                )

    @staticmethod
    def _project_queue_state(document: dict[str, Any]) -> str:
        queue = document.get("project_queue")
        if isinstance(queue, dict) and queue.get("schema_version") == PROJECT_QUEUE_SCHEMA_VERSION:
            return str(queue.get("state") or "")
        if document.get("status") == "waiting_for_project":
            return "waiting"
        if document.get("status") in RELEASED_GOALS:
            return "released"
        return "owner"

    @classmethod
    def _is_project_owner(cls, document: dict[str, Any]) -> bool:
        return document.get("status") in PROJECT_OWNER_GOALS \
            and cls._project_queue_state(document) == "owner"

    @classmethod
    def _is_project_waiter(cls, document: dict[str, Any]) -> bool:
        return document.get("status") == "waiting_for_project" \
            and cls._project_queue_state(document) == "waiting"

    @staticmethod
    def _project_paths_overlap(left: Path, right: Path) -> bool:
        left = left.resolve()
        right = right.resolve()
        return left == right or left in right.parents or right in left.parents

    @classmethod
    def _goals_overlap(cls, left: dict[str, Any], right: dict[str, Any]) -> bool:
        left_authority = str(left.get("project_authority_id") or "")
        right_authority = str(right.get("project_authority_id") or "")
        if left_authority and right_authority and hmac.compare_digest(
            left_authority, right_authority,
        ):
            return True
        return cls._project_paths_overlap(
            Path(str(left.get("project", {}).get("path") or "")),
            Path(str(right.get("project", {}).get("path") or "")),
        )

    @classmethod
    def _goal_overlaps_target(
        cls, document: dict[str, Any], project_path: Path, project_authority_id: str,
    ) -> bool:
        held_authority = str(document.get("project_authority_id") or "")
        if held_authority and project_authority_id and hmac.compare_digest(
            held_authority, project_authority_id,
        ):
            return True
        return cls._project_paths_overlap(
            Path(str(document.get("project", {}).get("path") or "")), project_path,
        )

    @staticmethod
    def _pristine_for_queue_migration(document: dict[str, Any]) -> bool:
        if document.get("artifacts"):
            return False
        for task in document.get("tasks", []):
            if not isinstance(task, dict):
                return False
            if task.get("state") not in {"ready", "waiting"}:
                return False
            if task.get("pending_action") or task.get("pending_transaction"):
                return False
            if str(task.get("provider_effect_state") or "never_dispatched") \
                    not in {"", "never_dispatched"}:
                return False
        return True

    def _execution_contract_for(self, document: dict[str, Any]) -> dict[str, Any]:
        return _exclusive_project_contract(
            Path(str(document.get("project", {}).get("path") or "")),
            str(document.get("project_authority_id") or ""),
        )

    def _validate_execution_metadata(self, document: dict[str, Any]) -> None:
        contract = document.get("execution_contract")
        expected = self._execution_contract_for(document)
        if not isinstance(contract, dict) or contract.get(
            "schema_version"
        ) != EXECUTION_CONTRACT_SCHEMA_VERSION or contract.get("mode") != "exclusive_project" \
                or not hmac.compare_digest(
                    str(contract.get("project_authority_id") or ""),
                    str(expected["project_authority_id"]),
                ) or not hmac.compare_digest(
                    str(contract.get("root_fingerprint_sha256") or ""),
                    str(expected["root_fingerprint_sha256"]),
                ) or not hmac.compare_digest(
                    str(contract.get("fingerprint_sha256") or ""),
                    str(expected["fingerprint_sha256"]),
                ):
            raise HarnessError("Long-horizon execution ownership metadata is invalid")
        queue = document.get("project_queue")
        if not isinstance(queue, dict) or queue.get(
            "schema_version"
        ) != PROJECT_QUEUE_SCHEMA_VERSION or queue.get("state") not in {
            "owner", "waiting", "released",
        }:
            raise HarnessError("Long-horizon project queue metadata is invalid")
        state = str(queue["state"])
        status = str(document.get("status") or "")
        if (status == "waiting_for_project") != (state == "waiting"):
            raise HarnessError("Long-horizon project queue status does not match its claim")
        if status in RELEASED_GOALS and state != "released":
            raise HarnessError("A completed long-horizon goal still claims its project")
        if status in PROJECT_OWNER_GOALS - {"failed"} and state != "owner":
            raise HarnessError("An executable long-horizon goal does not own its project")
        if status == "failed" and state not in {"owner", "released"}:
            raise HarnessError("A failed long-horizon goal has invalid project ownership")
        cancellation = document.get("cancellation")
        if not isinstance(cancellation, dict) or cancellation.get(
            "schema_version"
        ) != CANCELLATION_SCHEMA_VERSION or cancellation.get("state") not in {
            "none", "draining", "settled",
        }:
            raise HarnessError("Long-horizon cancellation metadata is invalid")
        if status == "cancelling" and cancellation.get("state") != "draining":
            raise HarnessError("A cancelling goal has no durable drain request")

    @staticmethod
    def _queue_record(
        state: str, now: int, *, blocked_by_goal_id: str = "",
        queued_ms: int = 0, promoted_ms: int = 0,
        auto_start_pending: bool = False,
    ) -> dict[str, Any]:
        return {
            "schema_version": PROJECT_QUEUE_SCHEMA_VERSION,
            "state": state,
            "blocked_by_goal_id": str(blocked_by_goal_id),
            "queued_ms": int(queued_ms),
            "promoted_ms": int(promoted_ms),
            "released_ms": int(now if state == "released" else 0),
            "auto_start_pending": bool(auto_start_pending and state == "owner"),
        }

    def _shared_documents(
        self, db: sqlite3.Connection, statuses: set[str],
    ) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in statuses)
        rows = db.execute(
            f"SELECT * FROM long_goals WHERE status IN ({placeholders}) "
            "ORDER BY created_ms,goal_id",
            tuple(sorted(statuses)),
        ).fetchall()
        return [self._decode_shared(row) for row in rows if row is not None]

    def _shared_project_owners(
        self, db: sqlite3.Connection, project_path: Path, project_authority_id: str,
        *, except_goal_id: str = "",
    ) -> list[dict[str, Any]]:
        return [
            goal for goal in self._shared_documents(db, PROJECT_OWNER_GOALS)
            if goal["goal_id"] != except_goal_id and self._is_project_owner(goal)
            and self._goal_overlaps_target(goal, project_path, project_authority_id)
        ]

    def _promote_eligible_waiters(
        self, db: sqlite3.Connection, *, auto_start_pending: bool = True,
    ) -> list[str]:
        documents = self._shared_documents(db, ACTIVE_GOALS)
        owners = [one for one in documents if self._is_project_owner(one)]
        waiters = [one for one in documents if self._is_project_waiter(one)]
        promoted: list[str] = []
        for waiter in waiters:
            blockers = [one for one in owners if self._goals_overlap(waiter, one)]
            blockers.sort(key=lambda one: (int(one.get("created_ms") or 0), one["goal_id"]))
            queue = waiter["project_queue"]
            if blockers:
                blocker_id = str(blockers[0]["goal_id"])
                if str(queue.get("blocked_by_goal_id") or "") != blocker_id:
                    queue["blocked_by_goal_id"] = blocker_id
                    waiter["note"] = (
                        "Waiting for long-horizon goal " + blocker_id[:8]
                        + " to release this project."
                    )
                    self._event(db, waiter, "goal_project_wait_rebased", payload={
                        "blocked_by_goal_id": blocker_id,
                    })
                    waiter["revision"] = int(waiter["revision"]) + 1
                    self._write(db, waiter)
                continue
            now = _now()
            waiter["status"] = "queued"
            waiter["project_queue"] = self._queue_record(
                "owner", now,
                queued_ms=int(queue.get("queued_ms") or waiter.get("created_ms") or now),
                promoted_ms=now,
                auto_start_pending=auto_start_pending,
            )
            waiter["note"] = "The prior project owner finished; this goal is ready to continue."
            self._event(db, waiter, "goal_project_promoted", payload={
                "execution_contract_fingerprint": waiter["execution_contract"]["fingerprint_sha256"],
            })
            waiter["revision"] = int(waiter["revision"]) + 1
            self._write(db, waiter)
            owners.append(waiter)
            promoted.append(str(waiter["goal_id"]))
        return promoted

    def _migrate_execution_metadata(self) -> None:
        """Add the v1 project claim contract to authenticated schema-v2 rows."""

        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                rows = db.execute(
                    "SELECT * FROM long_goals ORDER BY created_ms,goal_id"
                ).fetchall()
                owners: list[dict[str, Any]] = []
                for row in rows:
                    document = self._decode_shared(row)
                    if document is None:
                        continue
                    changed = False
                    if not isinstance(document.get("execution_contract"), dict):
                        document["execution_contract"] = self._execution_contract_for(document)
                        changed = True
                    queue = document.get("project_queue")
                    if not isinstance(queue, dict):
                        now = int(document.get("created_ms") or _now())
                        if document.get("status") in RELEASED_GOALS:
                            queue = self._queue_record("released", now)
                        elif document.get("status") == "failed":
                            # A legacy failed row with an unresolved provider
                            # or file boundary remains the exclusive owner. It
                            # is unsafe to let newer work build on a partial
                            # effect and attempt a delayed rollback afterward.
                            queue = self._queue_record(
                                "owner" if any(
                                    _task_has_unsettled_effect(task)
                                    for task in document.get("tasks", [])
                                ) else "released",
                                now,
                            )
                        elif document.get("status") == "waiting_for_project":
                            queue = self._queue_record("waiting", now, queued_ms=now)
                        else:
                            blockers = [
                                owner for owner in owners if self._goals_overlap(document, owner)
                            ]
                            if blockers:
                                if not self._pristine_for_queue_migration(document):
                                    raise HarnessError(
                                        "Conflicting legacy long-horizon goals have project effects; "
                                        "reconcile them before Nexus can migrate project ownership."
                                    )
                                document["status"] = "waiting_for_project"
                                queue = self._queue_record(
                                    "waiting", now,
                                    blocked_by_goal_id=str(blockers[0]["goal_id"]),
                                    queued_ms=now,
                                )
                                document["note"] = (
                                    "Waiting for long-horizon goal "
                                    + str(blockers[0]["goal_id"])[:8]
                                    + " to release this project."
                                )
                            else:
                                queue = self._queue_record("owner", now)
                        document["project_queue"] = queue
                        changed = True
                    elif "auto_start_pending" not in queue:
                        # Never infer automatic dispatch for an existing row.  A
                        # legacy checkpoint may already have crossed a provider
                        # boundary even when its top-level status is queued.
                        queue["auto_start_pending"] = False
                        changed = True
                    if not isinstance(document.get("cancellation"), dict):
                        document["cancellation"] = {
                            "schema_version": CANCELLATION_SCHEMA_VERSION,
                            "state": "none",
                            "requested_ms": 0,
                            "settled_ms": 0,
                        }
                        changed = True
                    worker = document.get("worker")
                    if not isinstance(worker, dict):
                        worker = {}
                        document["worker"] = worker
                    if worker.get("schema_version") != SCHEDULER_LEASE_SCHEMA_VERSION:
                        worker.update({
                            "schema_version": SCHEDULER_LEASE_SCHEMA_VERSION,
                            "pid": int(worker.get("pid") or 0),
                            "token": str(worker.get("token") or ""),
                            "worker_id": str(worker.get("worker_id") or ""),
                            "acquired_ms": int(worker.get("acquired_ms") or 0),
                        })
                        changed = True
                    self._validate_execution_metadata(document)
                    if self._is_project_owner(document):
                        blockers = [
                            owner for owner in owners if self._goals_overlap(document, owner)
                        ]
                        if blockers:
                            raise HarnessError(
                                "Authenticated long-horizon project ownership conflicts; "
                                "Nexus stopped before dispatching more work."
                            )
                        owners.append(document)
                    if changed:
                        self._event(db, document, "execution_contract_migrated", payload={
                            "execution_contract": document["execution_contract"],
                            "project_queue_state": document["project_queue"]["state"],
                        })
                        document["revision"] = int(document["revision"]) + 1
                        self._write(db, document)
                self._promote_eligible_waiters(db, auto_start_pending=False)
                db.commit()
            except Exception:
                db.rollback()
                raise

    def _write(self, db: sqlite3.Connection, document: dict[str, Any]) -> None:
        document["updated_ms"] = _now()
        raw = _canonical(document)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        material = [
            document["goal_id"], document["request_id"], document["project_key"],
            document["status"], document["revision"], raw, digest,
            document["created_ms"], document["updated_ms"],
        ]
        changed = db.execute(
            "UPDATE long_goals SET status=?,revision=?,document_json=?,document_sha256=?,"
            "integrity_mac=?,updated_ms=? WHERE goal_id=?",
            (document["status"], document["revision"], raw, digest,
             mac("long-horizon-goal-v1", material), document["updated_ms"],
             document["goal_id"]),
        ).rowcount
        if changed != 1:
            raise HarnessError("The long-horizon goal disappeared while it was changing")

    def _event(
        self, db: sqlite3.Connection, document: dict[str, Any], kind: str,
        *, task_id: str = "", agent_id: str = "", payload: object = None,
        run_id: str = "",
    ) -> dict[str, Any]:
        seq = int(document.get("event_seq") or 0) + 1
        document["event_seq"] = seq
        previous = str(document.get("event_head_sha256") or "")
        event = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "event_id": uuid.uuid4().hex,
            "seq": seq,
            "type": _short(kind, 100),
            "at_ms": _now(),
            "goal_id": document["goal_id"],
            "task_id": _short(task_id, 160),
            "agent_id": _short(agent_id, 160),
            "run_id": _short(run_id, 160),
            # Events are written in the same transaction as the snapshot they
            # describe. The mutation publishes revision+1 when it commits.
            "revision": int(document["revision"]) + 1,
            "previous_sha256": previous,
            "payload": _bounded_json(self.redactor.value(payload or {})),
        }
        raw = _canonical(event)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        material = [event["goal_id"], seq, event["event_id"], event["type"], raw, digest]
        db.execute(
            "INSERT INTO long_goal_events(goal_id,seq,event_id,type,event_json,event_sha256,integrity_mac) "
            "VALUES(?,?,?,?,?,?,?)",
            (event["goal_id"], seq, event["event_id"], event["type"], raw, digest,
             mac("long-horizon-event-v1", material)),
        )
        document["event_head_sha256"] = digest
        # A current snapshot plus the newest deltas is sufficient to rebuild
        # the UI. Keep the journal bounded without ever reusing sequence IDs.
        if seq > MAX_EVENTS:
            cutoff = seq - MAX_EVENTS
            deleted = db.execute(
                "SELECT event_sha256 FROM long_goal_events WHERE goal_id=? AND seq=?",
                (event["goal_id"], cutoff),
            ).fetchone()
            db.execute(
                "DELETE FROM long_goal_events WHERE goal_id=? AND seq<=?",
                (event["goal_id"], cutoff),
            )
            document["event_floor_seq"] = cutoff + 1
            document["event_floor_previous_sha256"] = str(deleted["event_sha256"] if deleted else "")
        return event

    def _mutate(
        self, goal_id: str, change: Callable[[dict[str, Any], sqlite3.Connection], Any]
    ) -> tuple[dict[str, Any], Any]:
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM long_goals WHERE goal_id=?", (goal_id,)).fetchone()
                document = self._decode(row)
                if document is None:
                    raise HarnessError("That long-horizon goal does not exist")
                was_owner = self._is_project_owner(document)
                result = change(document, db)
                if result is _NO_MUTATION:
                    db.rollback()
                    returned = copy.deepcopy(document)
                    returned["_promoted_goal_ids"] = []
                    return returned, None
                released = was_owner and document.get("status") in RELEASED_GOALS
                if document.get("status") in RELEASED_GOALS \
                        and self._project_queue_state(document) != "released":
                    queued_ms = int((document.get("project_queue") or {}).get("queued_ms") or 0)
                    promoted_ms = int((document.get("project_queue") or {}).get("promoted_ms") or 0)
                    document["project_queue"] = self._queue_record(
                        "released", _now(), queued_ms=queued_ms, promoted_ms=promoted_ms,
                    )
                    self._event(db, document, "goal_project_released", payload={
                        "terminal_status": document["status"],
                        "execution_contract_fingerprint": document[
                            "execution_contract"
                        ]["fingerprint_sha256"],
                    })
                document["revision"] = int(document["revision"]) + 1
                self._write(db, document)
                if document.get("status") in RELEASED_GOALS:
                    self._prune_released_goals(db)
                promoted = self._promote_eligible_waiters(db) if released else []
                db.commit()
                returned = copy.deepcopy(document)
                returned["_promoted_goal_ids"] = promoted
                return returned, result
            except Exception:
                db.rollback()
                raise

    def _agents_for_project(
        self, board: dict[str, Any], project_id: str,
        participant_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        assigned = {
            str(line.get("agent") or "") for line in board.get("works_on", [])
            if isinstance(line, dict) and str(line.get("project") or "") == project_id
        }
        available = []
        for one in board.get("agents", []):
            if not isinstance(one, dict) \
                    or str(one.get("id") or "") not in assigned \
                    or one.get("ready") is not True or not str(one.get("who") or ""):
                continue
            route = _short(one.get("who"), 300)
            _kind, context = chat_lab._route_failure_context(  # noqa: SLF001 - shared contract
                self.config, route
            )
            available.append({
                "id": _short(one.get("id"), 160),
                "name": _short(one.get("name") or "agent", 300),
                "who": route,
                "ready": one.get("ready") is True,
                "route_binding": {
                    "binding_schema_version": AGENT_BINDING_SCHEMA_VERSION,
                    "route": route,
                    **context,
                },
            })
        required = list(dict.fromkeys(
            str(one or "") for one in (participant_ids or []) if str(one or "")
        ))
        if not required:
            return available
        by_id = {one["id"]: one for one in available}
        missing = [one for one in required if one not in by_id]
        if missing:
            board_agents = {
                str(one.get("id") or ""): str(one.get("name") or one.get("id") or "agent")
                for one in board.get("agents", []) if isinstance(one, dict)
            }
            raise HarnessError(
                "Every selected agent must be ready and assigned to "
                "the selected project before work starts. Unavailable: "
                + ", ".join(board_agents.get(one, one) for one in missing)
            )
        return [by_id[one] for one in required]

    def provider_setup_status(self, document: dict[str, Any]) -> dict[str, Any]:
        """Compare saved dispatch semantics with this runtime configuration.

        Route names are aliases.  The hash-only binding also covers the
        provider profile and adapter contract, so a paused goal can never
        resume through a silently repointed alias after settings reload.
        """

        changed: list[dict[str, str]] = []
        for agent in document.get("agents", []):
            if not isinstance(agent, dict):
                continue
            name = _short(agent.get("name") or agent.get("id") or "agent", 300)
            route = _short(agent.get("who"), 300)
            expected = agent.get("route_binding")
            if not isinstance(expected, dict) or expected.get(
                "binding_schema_version"
            ) != AGENT_BINDING_SCHEMA_VERSION:
                changed.append({
                    "agent_id": _short(agent.get("id"), 160),
                    "name": name,
                    "route": route,
                    "reason": "This saved goal predates provider-route binding.",
                })
                continue
            _kind, current = chat_lab._route_failure_context(  # noqa: SLF001 - shared contract
                self.config, route
            )
            same = (
                str(expected.get("route") or "") == route
                and expected.get("failure_context_version")
                    == current.get("failure_context_version")
                and hmac.compare_digest(
                    str(expected.get("route_fingerprint_sha256") or ""),
                    str(current.get("route_fingerprint_sha256") or ""),
                )
                and hmac.compare_digest(
                    str(expected.get("transport_contract") or ""),
                    str(current.get("transport_contract") or ""),
                )
                and expected.get("effective_dispatch_version")
                    == current.get("effective_dispatch_version")
                and hmac.compare_digest(
                    str(expected.get("effective_dispatch_fingerprint_sha256") or ""),
                    str(current.get("effective_dispatch_fingerprint_sha256") or ""),
                )
                and hmac.compare_digest(
                    str(expected.get("effective_dispatch_contract") or ""),
                    str(current.get("effective_dispatch_contract") or ""),
                )
                and expected.get("provider_principal_version")
                    == current.get("provider_principal_version")
                and hmac.compare_digest(
                    str(expected.get("provider_principal_fingerprint_sha256") or ""),
                    str(current.get("provider_principal_fingerprint_sha256") or ""),
                )
                and hmac.compare_digest(
                    str(expected.get("provider_principal_contract") or ""),
                    str(current.get("provider_principal_contract") or ""),
                )
            )
            if not same:
                changed.append({
                    "agent_id": _short(agent.get("id"), 160),
                    "name": name,
                    "route": route,
                    "reason": (
                        "Its resolved executable, executable version, provider configuration, "
                        "or transport contract changed."
                    ),
                })
        if not changed:
            return {
                "changed": False,
                "code": "current",
                "message": "Every goal agent still matches the provider setup admitted for this goal.",
                "agents": [],
                "recovery_action": "",
            }
        names = ", ".join(one["name"] for one in changed[:6])
        return {
            "changed": True,
            "code": "provider_setup_changed",
            "message": (
                "The saved provider setup changed for " + names + ". Nexus protected this goal "
                "and will not silently redirect or continue its history. Keep it for inspection, or "
                "start a new goal from the current board setup."
            ),
            "agents": changed,
            "recovery_action": "start_new_goal_with_current_setup",
        }

    def sanitize_action(self, action: dict[str, Any]) -> dict[str, Any]:
        clean = copy.deepcopy(action)
        clean["summary"] = self.redactor.text(str(clean.get("summary") or ""))
        clean["evidence"] = [
            self.redactor.text(str(one)) for one in clean.get("evidence", [])
        ]
        clean["review_findings"] = [
            self.redactor.text(str(one)) for one in clean.get("review_findings", [])
        ]
        clean["questions"] = self.redactor.value(clean.get("questions", []))
        for delegated in clean.get("tasks", []):
            if isinstance(delegated, dict):
                delegated["title"] = self.redactor.text(str(delegated.get("title") or ""))
                delegated["description"] = self.redactor.text(str(delegated.get("description") or ""))
        for change in clean.get("changes", []):
            if isinstance(change, dict):
                change["reason"] = self.redactor.text(str(change.get("reason") or ""))
        return clean

    def validate_create(
        self, board: dict[str, Any], project_id: str, objectives: list[str], request_id: str,
        *, success_criteria: list[str] | None = None, policy: dict[str, Any] | None = None,
        attachment_text: str = "", participant_ids: list[str] | None = None,
        require_all_participants: bool | None = None,
    ) -> None:
        """Run every non-persistent admission check used by goal creation."""
        _exact_request_id(request_id)
        project = next((
            one for one in board.get("projects", []) if isinstance(one, dict)
            and str(one.get("id") or "") == project_id
        ), None)
        if project is None or project.get("is_there") is not True:
            raise HarnessError("Choose an available project for the long-horizon goal")
        root = Path(str(project.get("path") or "")).resolve(strict=True)
        if root.is_symlink() or not root.is_dir():
            raise HarnessError("The long-horizon project must be a real local folder")
        admitted_agents = self._agents_for_project(board, project_id, participant_ids)
        if not admitted_agents:
            raise HarnessError("Assign at least one ready agent to this project")
        clean_objectives = _redacted_objectives(self.redactor, objectives)
        if not clean_objectives:
            raise HarnessError("Write at least one concrete project goal")
        requested_max_tasks = min(
            MAX_TASKS, max(1, int((policy or {}).get("max_tasks") or MAX_TASKS))
        )
        require_all = bool(participant_ids) if require_all_participants is None \
            else bool(require_all_participants)
        required_initial_tasks = max(
            len(clean_objectives), len(admitted_agents) if require_all else 0,
        )
        if required_initial_tasks > requested_max_tasks:
            raise HarnessError(
                "The initial objectives and required chat-participant contributions "
                "exceed the explicit task budget"
            )
        if len("\n\n".join(clean_objectives)) + len(str(attachment_text or "")) \
                > MAX_OBJECTIVE_CHARACTERS:
            raise HarnessError("The goal plus extracted attachment text is too large for one bounded goal")
        raw_criteria = list(success_criteria or [])
        if len(raw_criteria) > MAX_CRITERIA:
            raise HarnessError(f"Use at most {MAX_CRITERIA} explicit success criteria")
        criteria = list(dict.fromkeys(
            _short(self.redactor.text(one), 1_000) for one in raw_criteria if _short(one, 1_000)
        ))
        if len(criteria) + 3 > MAX_CRITERIA:
            raise HarnessError(f"Use at most {MAX_CRITERIA - 3} custom success criteria")

    def inspect_runtime_admission(
        self, board: dict[str, Any], project_id: str, objectives: list[str],
        request_id: str, *, lead_id: str = "",
        success_criteria: list[str] | None = None,
        policy: dict[str, Any] | None = None, attachments: object = None,
        participant_ids: list[str] | None = None, conversation_id: str = "",
        expected_project_authority_id: str = "",
        require_all_participants: bool | None = None,
    ) -> dict[str, Any]:
        """Inspect one exact runtime admission without changing scheduler state."""

        exact_conversation_id = _exact_conversation_id(conversation_id)
        require_all = bool(participant_ids) if require_all_participants is None \
            else bool(require_all_participants)
        project = next((
            one for one in board.get("projects", []) if isinstance(one, dict)
            and str(one.get("id") or "") == project_id
        ), None)
        if project is None or project.get("is_there") is not True:
            raise HarnessError("Choose an available project for the long-horizon goal")
        root = Path(str(project.get("path") or "")).resolve(strict=True)
        self.validate_create(
            board, project_id, objectives, request_id,
            success_criteria=success_criteria, policy=policy,
            participant_ids=participant_ids,
            require_all_participants=require_all,
        )
        agents = self._agents_for_project(board, project_id, participant_ids)
        if lead_id and not any(one["id"] == lead_id for one in agents):
            raise HarnessError(
                "The selected lead is not one of this chat's ready project agents"
            )
        lead = next((one for one in agents if one["id"] == lead_id), agents[0])
        exact_participants = [one["id"] for one in agents] if participant_ids else []
        actual_authority_id = project_identity(root)
        if expected_project_authority_id and not hmac.compare_digest(
            expected_project_authority_id, actual_authority_id,
        ):
            raise HarnessError(
                "The selected project's execution authority changed during goal admission."
            )
        admission_digest = _goal_admission_digest(
            self.redactor,
            project_id=project_id,
            project_path=root,
            project_authority_id=actual_authority_id,
            conversation_id=exact_conversation_id,
            participant_ids=exact_participants,
            lead_id=lead["id"],
            objectives=objectives,
            success_criteria=success_criteria,
            policy={
                **(policy or {}),
                **({"participant_requirement": "adaptive"}
                   if participant_ids and not require_all else {}),
            },
            attachments=attachments,
            agent_bindings=[one.get("route_binding") for one in agents],
        )
        goal = self.get_by_request(request_id)
        conflict = ""
        request_retired = bool(
            goal is not None and goal.get("request_tombstone") is True
        )
        if goal is not None and not request_retired:
            same_root = Path(
                str(goal.get("project", {}).get("path") or "")
            ).resolve() == root
            if (
                str(goal.get("project", {}).get("id") or "") != project_id
                or not same_root
                or str(goal.get("conversation_id") or "")
                != exact_conversation_id
                or list(goal.get("requested_agent_ids") or []) != exact_participants
                or str(goal.get("lead_agent_id") or "") != lead["id"]
                or bool(goal.get(
                    "require_all_participants",
                    bool(goal.get("requested_agent_ids")),
                )) != require_all
            ):
                conflict = (
                    "That long-horizon request identity is already bound to a different "
                    "project, chat, participant set, or lead agent."
                )
        if goal is not None and not conflict:
            stored_digest = str(goal.get("admission_digest") or "")
            if not stored_digest:
                conflict = (
                    "That saved request predates intent-bound retries and cannot be "
                    "resumed by replay. Inspect it in Mission control, then use a new "
                    "request for changed work."
                )
            elif not hmac.compare_digest(stored_digest, admission_digest):
                conflict = (
                    "That long-horizon request identity is already bound to a different "
                    "project, chat, participant set, objective, policy, or attachment set."
                )
        return {
            "project": project,
            "root": root,
            "agents": agents,
            "lead": lead,
            "participant_ids": exact_participants,
            "require_all_participants": require_all,
            "project_authority_id": actual_authority_id,
            "admission_digest": admission_digest,
            "goal": goal,
            "request_retired": request_retired,
            "conflict": conflict,
        }

    def preflight_runtime_admission(
        self, board: dict[str, Any], project_id: str, objectives: list[str],
        request_id: str, **kwargs: Any,
    ) -> dict[str, Any]:
        """Require an exact existing binding, or prove that the identity is unused."""

        inspected = self.inspect_runtime_admission(
            board, project_id, objectives, request_id, **kwargs,
        )
        if inspected["conflict"]:
            raise HarnessError(str(inspected["conflict"]))
        return inspected

    def create(
        self, board: dict[str, Any], project_id: str, objectives: list[str],
        request_id: str, *, lead_id: str = "", success_criteria: list[str] | None = None,
        policy: dict[str, Any] | None = None, input_bundle: dict[str, Any] | None = None,
        participant_ids: list[str] | None = None, conversation_id: str = "",
        admission_digest: str = "", expected_project_authority_id: str = "",
        require_all_participants: bool | None = None,
    ) -> dict[str, Any]:
        exact_conversation_id = _exact_conversation_id(conversation_id)
        require_all = bool(participant_ids) if require_all_participants is None \
            else bool(require_all_participants)
        self.validate_create(
            board, project_id, objectives, request_id,
            success_criteria=success_criteria, policy=policy,
            attachment_text=str((input_bundle or {}).get("attachment_text") or ""),
            participant_ids=participant_ids,
            require_all_participants=require_all,
        )
        client_request_id = _exact_request_id(request_id)
        request_id = f"{self.authority_key}:{client_request_id}"
        projects = [one for one in board.get("projects", []) if isinstance(one, dict)]
        project = next((one for one in projects if str(one.get("id") or "") == project_id), None)
        if project is None or project.get("is_there") is not True:
            raise HarnessError("Choose an available project for the long-horizon goal")
        root = Path(str(project.get("path") or "")).resolve(strict=True)
        if root.is_symlink() or not root.is_dir():
            raise HarnessError("The long-horizon project must be a real local folder")
        agents = self._agents_for_project(board, project_id, participant_ids)
        if not agents:
            raise HarnessError("Assign at least one ready agent to this project")
        if lead_id and not any(one["id"] == lead_id for one in agents):
            raise HarnessError("The selected lead is not one of this chat's ready project agents")
        lead = next((one for one in agents if one["id"] == lead_id), agents[0])
        target_authority_id = project_identity(root)
        if expected_project_authority_id and not hmac.compare_digest(
            expected_project_authority_id, target_authority_id
        ):
            raise HarnessError(
                "The selected project's execution authority changed during goal admission."
            )
        clean_objectives = _redacted_objectives(self.redactor, objectives)
        if not clean_objectives:
            raise HarnessError("Write at least one concrete project goal")
        requested_max_tasks = min(
            MAX_TASKS, max(1, int((policy or {}).get("max_tasks") or MAX_TASKS))
        )
        required_initial_tasks = max(
            len(clean_objectives), len(agents) if require_all else 0,
        )
        if required_initial_tasks > requested_max_tasks:
            raise HarnessError(
                "The initial objectives and required chat-participant contributions "
                "exceed the explicit task budget"
            )
        now = _now()
        goal_id = uuid.uuid4().hex
        tasks: list[dict[str, Any]] = []
        collaboration_order = [lead, *[one for one in agents if one["id"] != lead["id"]]]
        initial_ids: list[str] = []
        represented_agents: set[str] = set()
        for position, objective in enumerate(clean_objectives, start=1):
            owner = (
                collaboration_order[(position - 1) % len(collaboration_order)]
                if require_all else lead
            )
            task_id = _stable_id("task", goal_id, position, objective)
            initial_ids.append(task_id)
            represented_agents.add(owner["id"])
            tasks.append({
                "id": task_id,
                "title": _short(objective.splitlines()[0], 240),
                "description": objective,
                "kind": "work",
                "state": "ready",
                "depends_on": [],
                "parent_id": "",
                "review_of": "",
                "assigned_agent_id": owner["id"],
                "required_contributor_id": owner["id"] if require_all else "",
                "parallel_safe": len(clean_objectives) > 1,
                "resource_paths": [],
                "attempts": 0,
                "no_progress": 0,
                "lease_id": "",
                "owner_pid": 0,
                "owner_token": "",
                "created_ms": now,
                "updated_ms": now,
                "summary": "",
                "last_error": "",
                "evidence": [],
                "artifacts": [],
                "criteria_evidence": [],
                "provider_effect_state": "never_dispatched",
                "provider_effect_id": "",
                "claim_objective_epoch": 0,
                "outcome_unknown": False,
                "pending_action": {},
                "pending_transaction": {},
            })
        if require_all:
            for owner in collaboration_order:
                if owner["id"] in represented_agents:
                    continue
                contribution = (
                    "Make a distinct, useful contribution to the shared user objective after "
                    "reviewing the preceding task results. Close a concrete gap, add or improve "
                    "an artifact, or perform targeted verification; do not merely restate earlier "
                    "work. If the user asked each agent to create or own something, produce this "
                    "agent's distinct requested artifact.\n\nSHARED OBJECTIVE\n"
                    + "\n\n".join(clean_objectives)
                )
                task_id = _stable_id("participant", goal_id, owner["id"], contribution)
                tasks.append({
                    "id": task_id,
                    "title": f"{owner['name']} contribution",
                    "description": contribution,
                    "kind": "work",
                    "state": "waiting" if initial_ids else "ready",
                    "depends_on": list(initial_ids),
                    "parent_id": "",
                    "review_of": "",
                    "assigned_agent_id": owner["id"],
                    "required_contributor_id": owner["id"],
                    "parallel_safe": False,
                    "resource_paths": [],
                    "attempts": 0,
                    "no_progress": 0,
                    "lease_id": "",
                    "owner_pid": 0,
                    "owner_token": "",
                    "created_ms": now,
                    "updated_ms": now,
                    "summary": "",
                    "last_error": "",
                    "evidence": [],
                    "artifacts": [],
                    "criteria_evidence": [],
                    "provider_effect_state": "never_dispatched",
                    "provider_effect_id": "",
                    "claim_objective_epoch": 0,
                    "outcome_unknown": False,
                    "pending_action": {},
                    "pending_transaction": {},
                })
        objective = "\n\n".join(clean_objectives)
        attachment_text = str((input_bundle or {}).get("attachment_text") or "")
        if attachment_text:
            objective += "\n\nUSER-SUPPLIED ATTACHMENT TEXT\n" + attachment_text
        if len(objective) > MAX_OBJECTIVE_CHARACTERS:
            raise HarnessError("The goal plus extracted attachment text is too large for one bounded goal")
        public_inputs = _bounded_json((input_bundle or {}).get("public_files") or [], 40_000)
        provider_inputs = [
            {key: one.get(key) for key in ("id", "name", "type", "size", "path")}
            for one in ((input_bundle or {}).get("provider_files") or []) if isinstance(one, dict)
        ]
        raw_criteria = list(success_criteria or [])
        if len(raw_criteria) > MAX_CRITERIA:
            raise HarnessError(f"Use at most {MAX_CRITERIA} explicit success criteria")
        criteria = list(dict.fromkeys(
            _short(self.redactor.text(one), 1_000) for one in raw_criteria if _short(one, 1_000)
        ))
        baseline_criteria = [
            "Original objective is satisfied",
            "Every required task is complete",
            "Configured deterministic verification passes",
        ]
        criteria = list(dict.fromkeys([*baseline_criteria, *criteria]))
        if len(criteria) > MAX_CRITERIA:
            raise HarnessError(f"Use at most {MAX_CRITERIA - len(baseline_criteria)} custom success criteria")
        runtime_policy = {
            "max_tasks": requested_max_tasks,
            "max_provider_calls": min(MAX_PROVIDER_CALLS, max(1, int((policy or {}).get("max_provider_calls") or MAX_PROVIDER_CALLS))),
            "max_parallel": min(MAX_PARALLEL, max(1, int((policy or {}).get("max_parallel") or MAX_PARALLEL))),
            "max_context_tool_calls": min(
                MAX_CONTEXT_TOOL_CALLS,
                max(1, int((policy or {}).get("max_context_tool_calls") or MAX_CONTEXT_TOOL_CALLS)),
            ),
            "review_risk": _short((policy or {}).get("review_risk") or "high", 20),
            "legacy_available": True,
        }
        execution_contract = _exclusive_project_contract(root, target_authority_id)
        bound_admission_digest = _short(admission_digest, 128) or hashlib.sha256(
            _canonical({
                "project_id": project_id,
                "project_path": str(root),
                "project_authority_id": target_authority_id,
                "conversation_id": exact_conversation_id,
                "participant_ids": [one["id"] for one in agents] if participant_ids else [],
                "require_all_participants": require_all,
                "lead_id": lead["id"],
                "agent_bindings": [one.get("route_binding") for one in agents],
                "execution_contract": execution_contract,
                "objectives": clean_objectives,
                "success_criteria": criteria,
                "policy": runtime_policy,
                "public_inputs": public_inputs,
                "provider_inputs": provider_inputs,
            })
            .encode("utf-8")
        ).hexdigest()
        document = {
            "schema_version": SCHEMA_VERSION,
            "goal_id": goal_id,
            "request_id": request_id,
            "client_request_id": client_request_id,
            "authority_key": self.authority_key,
            "status": "queued",
            "revision": 1,
            "event_seq": 0,
            "event_head_sha256": "",
            "event_floor_seq": 1,
            "event_floor_previous_sha256": "",
            "created_ms": now,
            "updated_ms": now,
            "project_key": _project_key(root),
            "project": {"id": project_id, "name": _short(project.get("name") or root.name, 300), "path": str(root)},
            "project_authority_id": target_authority_id,
            "execution_contract": execution_contract,
            "project_queue": self._queue_record(
                "owner", now, auto_start_pending=True,
            ),
            "admission_digest": bound_admission_digest,
            "conversation_id": exact_conversation_id,
            "requested_agent_ids": [one["id"] for one in agents] if participant_ids else [],
            "require_all_participants": require_all,
            "objective": objective,
            "original_objective": objective,
            "objective_epoch": 1,
            "objective_revisions": [{"revision": 1, "at_ms": now, "text": objective, "reason": "original"}],
            "success_criteria": criteria,
            "agents": agents,
            "lead_agent_id": lead["id"],
            "tasks": tasks,
            "interrupts": [],
            "artifacts": [],
            "input_attachments": public_inputs,
            "input_provider_attachments": provider_inputs,
            "verification": {"status": "not_run", "reason": "Work has not finished yet", "commands": []},
            "budget": {"provider_calls": 0, "max_provider_calls": runtime_policy["max_provider_calls"],
                       "context_tool_calls": 0,
                       "max_context_tool_calls": runtime_policy["max_context_tool_calls"],
                       "tasks_created": len(tasks), "max_tasks": runtime_policy["max_tasks"]},
            "policy": runtime_policy,
            "worker": {
                "schema_version": SCHEDULER_LEASE_SCHEMA_VERSION,
                "pid": 0, "token": "", "worker_id": "", "acquired_ms": 0,
            },
            "cancellation": {
                "schema_version": CANCELLATION_SCHEMA_VERSION,
                "state": "none", "requested_ms": 0, "settled_ms": 0,
            },
            "note": "Ready to begin useful project work.",
            "parent_goal_id": "",
            "fork_checkpoint": 0,
        }
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = db.execute("SELECT * FROM long_goals WHERE request_id=?", (request_id,)).fetchone()
                if existing is not None:
                    existing_document = self._decode(existing)
                    if not existing_document or not hmac.compare_digest(
                        str(existing_document.get("admission_digest") or ""),
                        bound_admission_digest,
                    ):
                        raise HarnessError(
                            "That long-horizon request identity is already bound to different work."
                        )
                    self._promote_eligible_waiters(db)
                    existing_document = self._decode(db.execute(
                        "SELECT * FROM long_goals WHERE request_id=?", (request_id,)
                    ).fetchone())
                    db.commit()
                    return self.public(existing_document, reused=True)
                retired = self._decode_request_tombstone(db.execute(
                    "SELECT * FROM long_goal_request_tombstones WHERE request_id=?",
                    (request_id,),
                ).fetchone())
                if retired is not None:
                    if not str(retired.get("admission_digest") or "") \
                            or not hmac.compare_digest(
                                str(retired.get("admission_digest") or ""),
                                bound_admission_digest,
                            ):
                        raise HarnessError(
                            "That retired long-horizon request identity is already "
                            "bound to different work."
                        )
                    db.commit()
                    return self.public(retired, reused=True)
                # Reconcile an eligible older waiter before admitting newer
                # work.  The write transaction makes owner selection atomic
                # across Nexus processes and configuration authorities.
                self._promote_eligible_waiters(db)
                blockers = self._shared_project_owners(
                    db, root, target_authority_id,
                )
                blockers.sort(key=lambda one: (
                    int(one.get("created_ms") or 0), str(one["goal_id"]),
                ))
                if blockers:
                    blocker_id = str(blockers[0]["goal_id"])
                    document["status"] = "waiting_for_project"
                    document["project_queue"] = self._queue_record(
                        "waiting", now, blocked_by_goal_id=blocker_id, queued_ms=now,
                    )
                    document["note"] = (
                        "Waiting for long-horizon goal " + blocker_id[:8]
                        + " to release this project."
                    )
                raw = _canonical(document)
                digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                material = [
                    goal_id, request_id, document["project_key"], document["status"],
                    1, raw, digest, now, now,
                ]
                db.execute(
                    "INSERT INTO long_goals(goal_id,request_id,project_key,status,revision,document_json,"
                    "document_sha256,integrity_mac,created_ms,updated_ms) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (goal_id, request_id, document["project_key"], document["status"], 1, raw, digest,
                     mac("long-horizon-goal-v1", material), now, now),
                )
                self._event(db, document, "goal_created", agent_id=lead["id"], payload={
                    "objective": objective, "success_criteria": criteria,
                    "task_ids": [one["id"] for one in tasks], "policy": runtime_policy,
                    "execution_contract": execution_contract,
                    "project_queue_state": document["project_queue"]["state"],
                })
                if document["status"] == "waiting_for_project":
                    self._event(db, document, "goal_waiting_for_project", payload={
                        "blocked_by_goal_id": document["project_queue"]["blocked_by_goal_id"],
                        "execution_contract_fingerprint": execution_contract["fingerprint_sha256"],
                    })
                if public_inputs:
                    self._event(db, document, "input_attached", agent_id=lead["id"], payload={
                        "files": public_inputs,
                    })
                document["revision"] = 2
                self._write(db, document)
                # Repair any legacy overage during admission too. New terminal
                # transitions prune in their own transaction at MAX_GOALS + 1.
                self._prune_released_goals(db)
                db.commit()
            except Exception:
                db.rollback()
                raise
        return self.public(document)

    def get(self, goal_id: str) -> dict[str, Any]:
        with self.lock, self._connect() as db:
            document = self._decode(db.execute("SELECT * FROM long_goals WHERE goal_id=?", (goal_id,)).fetchone())
        if document is None:
            raise HarnessError("That long-horizon goal does not exist")
        return document

    def get_by_request(self, request_id: str) -> dict[str, Any] | None:
        stored = f"{self.authority_key}:{_exact_request_id(request_id)}"
        with self.lock, self._connect() as db:
            document = self._decode(db.execute(
                "SELECT * FROM long_goals WHERE request_id=?", (stored,)
            ).fetchone())
            if document is None:
                document = self._decode_request_tombstone(db.execute(
                    "SELECT * FROM long_goal_request_tombstones WHERE request_id=?",
                    (stored,),
                ).fetchone())
        return self.public(document, reused=True) if document else None

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.lock, self._connect() as db:
            active_rows = db.execute(
                "SELECT * FROM long_goals WHERE request_id LIKE ? AND status IN "
                "('queued','running','paused','waiting_for_user','waiting_for_project',"
                "'failed','cancelling') ORDER BY updated_ms DESC",
                (self.authority_key + ":%",),
            ).fetchall()
            active_documents = [self._decode(row) for row in active_rows]
            active = [
                document for document in active_documents
                if document is not None and (
                    self._is_project_owner(document) or self._is_project_waiter(document)
                )
            ]
            history_limit = max(1, min(100, int(limit)))
            # Failed owners are included above but share a SQL status with
            # released legacy history. Fetch enough candidates to skip every
            # active failed row without letting it crowd terminal history out.
            history_rows = db.execute(
                "SELECT * FROM long_goals WHERE request_id LIKE ? AND status IN "
                "('complete','cancelled','failed') ORDER BY updated_ms DESC LIMIT ?",
                (self.authority_key + ":%", history_limit + len(active)),
            ).fetchall()
            history_documents = [self._decode(row) for row in history_rows]
            history = [
                document for document in history_documents
                if document is not None and not self._is_project_owner(document)
                and not self._is_project_waiter(document)
            ][:history_limit]
            by_id = {
                str(document["goal_id"]): document for document in [*active, *history]
            }
            ordered = sorted(
                by_id.values(), key=lambda one: (
                    int(one.get("updated_ms") or 0), str(one["goal_id"]),
                ), reverse=True,
            )
            return [self.public(document) for document in ordered]

    def active_for_project(self, project_key: str, *, except_goal_id: str = "") -> list[dict[str, Any]]:
        with self.lock, self._connect() as db:
            rows = db.execute(
                "SELECT * FROM long_goals WHERE project_key=? "
                "AND status IN ('queued','running','paused','waiting_for_user','failed','cancelling') "
                "AND goal_id<>? AND request_id LIKE ? ORDER BY updated_ms DESC",
                (project_key, except_goal_id, self.authority_key + ":%"),
            ).fetchall()
            return [
                self.public(document) for row in rows
                if (document := self._decode(row)) is not None
                and self._is_project_owner(document)
            ]

    def active_authority_goals(self) -> list[dict[str, Any]]:
        """Return every active goal for this authority without a UI page limit."""
        with self.lock, self._connect() as db:
            rows = db.execute(
                "SELECT * FROM long_goals WHERE "
                "status IN ('queued','running','paused','waiting_for_user','waiting_for_project','failed','cancelling') "
                "AND request_id LIKE ? ORDER BY updated_ms DESC",
                (self.authority_key + ":%",),
            ).fetchall()
            documents = [self._decode(row) for row in rows]
            return [
                self.public(document) for document in documents
                if document is not None and (
                    self._is_project_owner(document) or self._is_project_waiter(document)
                )
            ]

    def auto_startable_authority_goals(self, limit: int = 8) -> list[dict[str, Any]]:
        """Return pristine durable starts owned by this configuration authority."""

        with self.lock, self._connect() as db:
            rows = db.execute(
                "SELECT * FROM long_goals WHERE status='queued' AND request_id LIKE ? "
                "ORDER BY created_ms,goal_id LIMIT ?",
                (self.authority_key + ":%", MAX_GOALS),
            ).fetchall()
            documents = [self._decode(row) for row in rows]
        return [
            self.public(document) for document in documents
            if document is not None and self._is_project_owner(document)
            and (document.get("project_queue") or {}).get("auto_start_pending") is True
            and self._pristine_for_queue_migration(document)
            and not str((document.get("worker") or {}).get("worker_id") or "")
        ][:max(1, min(MAX_GOALS, int(limit) if limit else MAX_GOALS))]

    def auto_startable_authority_page(
        self, after_created_ms: int, after_goal_id: str, *, limit: int = 16,
    ) -> tuple[list[dict[str, Any]], tuple[int, str]]:
        """Scan one fair bounded page so old blocked starts cannot starve newer ones."""

        page_size = max(1, min(64, int(limit)))
        with self.lock, self._connect() as db:
            rows = db.execute(
                "SELECT * FROM long_goals WHERE status='queued' AND request_id LIKE ? "
                "AND (created_ms>? OR (created_ms=? AND goal_id>?)) "
                "ORDER BY created_ms,goal_id LIMIT ?",
                (
                    self.authority_key + ":%", int(after_created_ms),
                    int(after_created_ms), str(after_goal_id), page_size,
                ),
            ).fetchall()
            if not rows and (after_created_ms or after_goal_id):
                rows = db.execute(
                    "SELECT * FROM long_goals WHERE status='queued' AND request_id LIKE ? "
                    "ORDER BY created_ms,goal_id LIMIT ?",
                    (self.authority_key + ":%", page_size),
                ).fetchall()
            documents = [self._decode(row) for row in rows]
        cursor = (
            (int(rows[-1]["created_ms"]), str(rows[-1]["goal_id"]))
            if rows else (0, "")
        )
        return ([
            self.public(document) for document in documents
            if document is not None and self._is_project_owner(document)
            and (document.get("project_queue") or {}).get("auto_start_pending") is True
            and self._pristine_for_queue_migration(document)
            and not str((document.get("worker") or {}).get("worker_id") or "")
        ], cursor)

    def active_overlapping_project(self, project_path: Path, *, except_goal_id: str = "") -> list[dict[str, Any]]:
        wanted = project_path.resolve()
        try:
            wanted_authority = project_identity(wanted)
        except Exception:
            wanted_authority = ""
        with self.lock, self._connect() as db:
            owners = self._shared_project_owners(
                db, wanted, wanted_authority, except_goal_id=except_goal_id,
            )
        return [self.public(goal) for goal in owners]

    def reconcile_project_queue(self) -> list[dict[str, Any]]:
        """Promote every globally eligible waiter without dispatching provider work."""

        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                promoted_ids = self._promote_eligible_waiters(db)
                rows = [
                    db.execute("SELECT * FROM long_goals WHERE goal_id=?", (goal_id,)).fetchone()
                    for goal_id in promoted_ids
                ]
                promoted = [
                    self._decode_shared(row) for row in rows if row is not None
                ]
                db.commit()
            except Exception:
                db.rollback()
                raise
        return [
            self.public(goal) for goal in promoted
            if str(goal.get("authority_key") or "") == self.authority_key
        ]

    def owned_queued_goals(self) -> list[dict[str, Any]]:
        with self.lock, self._connect() as db:
            rows = db.execute(
                "SELECT * FROM long_goals WHERE status='queued' AND request_id LIKE ? "
                "ORDER BY created_ms,goal_id",
                (self.authority_key + ":%",),
            ).fetchall()
            documents = [self._decode(row) for row in rows]
        return [
            self.public(document) for document in documents
            if document is not None and self._is_project_owner(document)
        ]

    def public(self, document: dict[str, Any] | None, *, reused: bool = False) -> dict[str, Any]:
        if document is None:
            return {}
        value = copy.deepcopy(document)
        if value.get("request_tombstone") is True:
            value["request_id"] = value.get(
                "client_request_id", value.get("request_id", "")
            )
            value["reused"] = reused
            value["promoted_goal_ids"] = []
            value["progress"] = {"complete": 0, "total": 0}
            value["pending_interrupts"] = []
            value["note"] = (
                "This terminal goal's detailed history was pruned, but its exact "
                "request identity remains permanently retired to prevent replay."
            )
            value["provider_setup_changed"] = False
            value["provider_setup_status"] = {
                "changed": False,
                "code": "terminal_request_retired",
                "message": (
                    "Detailed terminal history was pruned; the exact request remains "
                    "retired and cannot dispatch again."
                ),
                "agents": [],
                "recovery_action": "",
            }
            return value
        value["promoted_goal_ids"] = list(value.pop("_promoted_goal_ids", []))
        value.pop("input_provider_attachments", None)
        value["request_id"] = value.get("client_request_id", value.get("request_id", ""))
        value["reused"] = reused
        value["progress"] = {
            "complete": sum(one["state"] == "complete" for one in value["tasks"]),
            "total": len(value["tasks"]),
        }
        value["pending_interrupts"] = [
            one for one in value.get("interrupts", []) if one.get("state") == "pending"
        ]
        for agent in value.get("agents", []):
            if isinstance(agent, dict):
                agent["provider_identity_sha256"] = _provider_identity(agent)
        setup = self.provider_setup_status(value)
        value["provider_setup_changed"] = setup["changed"]
        value["provider_setup_status"] = setup
        return value

    def clone_to_project(
        self, source: dict[str, Any], project_id: str, project_name: str,
        project_path: Path, request_id: str,
    ) -> dict[str, Any]:
        if str(source.get("authority_key") or "") != self.authority_key:
            raise HarnessError("That long-horizon goal belongs to a different Nexus project authority")
        if any(one.get("state") == "pending" for one in source.get("interrupts", [])):
            raise HarnessError("Answer or cancel the pending decision before forking this goal")
        client_request_id = _exact_request_id(request_id, what="fork request")
        stored_request_id = f"{self.authority_key}:{client_request_id}"
        now = _now()
        document = copy.deepcopy(source)
        old_goal_id = str(source["goal_id"])
        target_authority_id = project_identity(project_path)
        document.update({
            "goal_id": uuid.uuid4().hex,
            "request_id": stored_request_id,
            "client_request_id": client_request_id,
            "authority_key": self.authority_key,
            "status": "paused", "revision": 1, "event_seq": 0,
            "event_head_sha256": "", "event_floor_seq": 1,
            "event_floor_previous_sha256": "", "created_ms": now, "updated_ms": now,
            "project_key": _project_key(project_path),
            "project": {"id": project_id, "name": project_name, "path": str(project_path)},
            "project_authority_id": target_authority_id,
            "execution_contract": _exclusive_project_contract(
                project_path, target_authority_id,
            ),
            "project_queue": self._queue_record("owner", now),
            "worker": {
                "schema_version": SCHEDULER_LEASE_SCHEMA_VERSION,
                "pid": 0, "token": "", "worker_id": "", "acquired_ms": 0,
            },
            "cancellation": {
                "schema_version": CANCELLATION_SCHEMA_VERSION,
                "state": "none", "requested_ms": 0, "settled_ms": 0,
            },
            "parent_goal_id": old_goal_id,
            "fork_checkpoint": int(source.get("event_seq") or 0),
            "note": "Forked from the saved task/evidence checkpoint into an isolated Git worktree. Resume when ready.",
        })
        by_id = {one["id"]: one for one in document["tasks"]}
        for task in document["tasks"]:
            task.update({"lease_id": "", "owner_pid": 0, "owner_token": ""})
            if task["state"] not in {"complete", "cancelled"}:
                deps_complete = all(by_id.get(dep, {}).get("state") == "complete" for dep in task.get("depends_on", []))
                task["state"] = "ready" if deps_complete else "waiting"
                task["pending_action"] = {}
                task["pending_transaction"] = {}
                task["outcome_unknown"] = False
                task["provider_effect_state"] = "forked_checkpoint"
        raw = _canonical(document)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        material = [document["goal_id"], stored_request_id, document["project_key"], "paused", 1,
                    raw, digest, now, now]
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                existing = db.execute("SELECT * FROM long_goals WHERE request_id=?", (stored_request_id,)).fetchone()
                if existing is not None:
                    existing_document = self._decode(existing)
                    if existing_document is None or str(
                        existing_document.get("parent_goal_id") or ""
                    ) != old_goal_id:
                        raise HarnessError(
                            "That fork request identity already belongs to another goal"
                        )
                    db.rollback()
                    return self.public(existing_document, reused=True)
                retired = self._decode_request_tombstone(db.execute(
                    "SELECT * FROM long_goal_request_tombstones WHERE request_id=?",
                    (stored_request_id,),
                ).fetchone())
                if retired is not None:
                    if str(retired.get("parent_goal_id") or "") != old_goal_id:
                        raise HarnessError(
                            "That retired fork request identity already belongs to "
                            "another goal"
                        )
                    db.commit()
                    return self.public(retired, reused=True)
                db.execute(
                    "INSERT INTO long_goals(goal_id,request_id,project_key,status,revision,document_json,"
                    "document_sha256,integrity_mac,created_ms,updated_ms) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (document["goal_id"], stored_request_id, document["project_key"], "paused", 1,
                     raw, digest, mac("long-horizon-goal-v1", material), now, now),
                )
                self._event(db, document, "goal_forked", payload={
                    "parent_goal_id": old_goal_id,
                    "checkpoint": document["fork_checkpoint"],
                    "isolated_project": str(project_path),
                    "preserved_tasks": len(document["tasks"]),
                    "preserved_artifacts": len(document.get("artifacts", [])),
                })
                document["revision"] = 2
                self._write(db, document)
                db.commit()
            except Exception:
                db.rollback()
                raise
        return self.public(document)

    def events(self, goal_id: str, after: int = 0, limit: int = 200) -> dict[str, Any]:
        document = self.get(goal_id)  # integrity, ownership, and existence check
        floor = int(document.get("event_floor_seq") or 1)
        requested_after = max(0, after)
        truncated = requested_after < floor - 1
        effective_after = max(requested_after, floor - 1)
        with self.lock, self._connect() as db:
            rows = db.execute(
                "SELECT * FROM long_goal_events WHERE goal_id=? AND seq>? ORDER BY seq LIMIT ?",
                (goal_id, effective_after, max(1, min(500, limit)) + 1),
            ).fetchall()
            previous_row = db.execute(
                "SELECT event_sha256 FROM long_goal_events WHERE goal_id=? AND seq=?",
                (goal_id, effective_after),
            ).fetchone()
        has_more = len(rows) > max(1, min(500, limit))
        rows = rows[:max(1, min(500, limit))]
        events = []
        expected_previous = (
            str(document.get("event_floor_previous_sha256") or "")
            if effective_after == floor - 1 else str(previous_row["event_sha256"] if previous_row else "")
        )
        expected_seq = effective_after + 1
        for row in rows:
            raw = str(row["event_json"])
            digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
            material = [goal_id, int(row["seq"]), str(row["event_id"]), str(row["type"]), raw, digest]
            if digest != str(row["event_sha256"]) or not hmac.compare_digest(
                str(row["integrity_mac"]), mac("long-horizon-event-v1", material)
            ):
                quarantine_marker("long-horizon-events", self.database, "Event integrity failed")
                raise HarnessError("Long-horizon event history failed integrity verification")
            event = json.loads(raw)
            if int(event.get("seq") or 0) != expected_seq or str(event.get("previous_sha256") or "") != expected_previous:
                quarantine_marker("long-horizon-events", self.database, "Event chain failed")
                raise HarnessError("Long-horizon event history is missing or reordered")
            events.append(event)
            expected_previous = digest
            expected_seq += 1
        if events and not has_more and int(events[-1]["seq"]) == int(document["event_seq"]):
            if expected_previous != str(document.get("event_head_sha256") or ""):
                raise HarnessError("Long-horizon event head does not match its goal snapshot")
        return {"goal_id": goal_id, "events": events,
                "next": events[-1]["seq"] if events else effective_after,
                "has_more": has_more, "oldest_available": floor, "truncated": truncated}

    @staticmethod
    def _refresh_waiting(document: dict[str, Any]) -> None:
        by_id = {one["id"]: one for one in document["tasks"]}
        changed = True
        while changed:
            changed = False
            for task in document["tasks"]:
                if task["state"] != "waiting":
                    continue
                deps = [by_id.get(one) for one in task["depends_on"]]
                if deps and all(one and one["state"] == "complete" for one in deps):
                    task["state"] = "ready"
                    task["last_error"] = ""
                    changed = True
                else:
                    stopped = [
                        one for one in deps
                        if one is None or one["state"] in {"failed", "cancelled", "blocked"}
                    ]
                    if stopped:
                        names = ", ".join(
                            str(one.get("title") or one.get("id")) if one else "missing prerequisite"
                            for one in stopped
                        )
                        task["state"] = "blocked"
                        task["last_error"] = f"Prerequisite work cannot complete: {names}."
                        changed = True

    @staticmethod
    def _compatible(task: dict[str, Any], chosen: list[dict[str, Any]]) -> bool:
        if not task.get("parallel_safe") and chosen:
            return False
        resources = {str(one).casefold() for one in task.get("resource_paths", [])}
        for other in chosen:
            if not other.get("parallel_safe"):
                return False
            held = {str(one).casefold() for one in other.get("resource_paths", [])}
            if not resources or not held or resources & held:
                return False
        return True

    @staticmethod
    def _scheduler_record(worker_id: str, *, kind: str = "runtime") -> dict[str, Any]:
        return {
            "schema_version": SCHEDULER_LEASE_SCHEMA_VERSION,
            "pid": os.getpid(),
            "token": _process_token(os.getpid()),
            "worker_id": str(worker_id),
            "kind": str(kind),
            "acquired_ms": _now(),
        }

    @staticmethod
    def _scheduler_live(document: dict[str, Any]) -> bool:
        worker = document.get("worker") or {}
        return bool(str(worker.get("worker_id") or "")) and _owner_is_alive(
            int(worker.get("pid") or 0), str(worker.get("token") or ""),
        )

    def claim_scheduler(self, goal_id: str, worker_id: str) -> bool:
        """Atomically acquire the one durable graph-dispatch lease for a goal."""

        worker_id = str(worker_id)
        if not worker_id:
            raise HarnessError("A stable scheduler identity is required")
        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT * FROM long_goals WHERE goal_id=?", (goal_id,),
                ).fetchone()
                document = self._decode(row)
                if document is None:
                    raise HarnessError("That long-horizon goal does not exist")
                if document.get("status") != "queued" or not self._is_project_owner(document):
                    db.rollback()
                    return False
                held = document.get("worker") or {}
                same = (
                    str(held.get("worker_id") or "") == worker_id
                    and int(held.get("pid") or 0) == os.getpid()
                    and hmac.compare_digest(
                        str(held.get("token") or ""), _process_token(os.getpid()),
                    )
                )
                if same and self._scheduler_live(document):
                    db.commit()
                    return True
                if str(held.get("worker_id") or "") and not (
                    held.get("kind") == "claim"
                    and not any(one.get("state") == "running" for one in document["tasks"])
                ):
                    # A dead lease is recovered separately so a process cannot
                    # skip provider-effect reconciliation while taking over.
                    db.rollback()
                    return False
                document["worker"] = self._scheduler_record(worker_id)
                self._event(db, document, "goal_scheduler_claimed", payload={
                    "worker_id": worker_id,
                })
                document["revision"] = int(document["revision"]) + 1
                self._write(db, document)
                db.commit()
                return True
            except Exception:
                db.rollback()
                raise

    def release_scheduler(self, goal_id: str, worker_id: str) -> bool:
        """CAS-clear a scheduler lease without disturbing a newer runtime."""

        with self.lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                document = self._decode(db.execute(
                    "SELECT * FROM long_goals WHERE goal_id=?", (goal_id,),
                ).fetchone())
                if document is None:
                    raise HarnessError("That long-horizon goal does not exist")
                held = document.get("worker") or {}
                same = (
                    str(held.get("worker_id") or "") == str(worker_id)
                    and int(held.get("pid") or 0) == os.getpid()
                    and hmac.compare_digest(
                        str(held.get("token") or ""), _process_token(os.getpid()),
                    )
                )
                if not same:
                    db.rollback()
                    return False
                document["worker"] = {
                    "schema_version": SCHEDULER_LEASE_SCHEMA_VERSION,
                    "pid": 0, "token": "", "worker_id": "", "kind": "runtime",
                    "acquired_ms": 0,
                }
                self._event(db, document, "goal_scheduler_released", payload={
                    "worker_id": str(worker_id),
                })
                document["revision"] = int(document["revision"]) + 1
                self._write(db, document)
                db.commit()
                return True
            except Exception:
                db.rollback()
                raise

    def claim_ready(self, goal_id: str, worker_id: str) -> list[dict[str, Any]]:
        def change(document: dict[str, Any], db: sqlite3.Connection):
            if document["status"] in TERMINAL_GOALS or document["status"] in {
                "paused", "waiting_for_user", "waiting_for_project", "cancelling",
            }:
                return []
            held = document.get("worker") or {}
            live = self._scheduler_live(document)
            exact = live and (
                int(held.get("pid") or 0) == os.getpid()
                and hmac.compare_digest(
                    str(held.get("token") or ""), _process_token(os.getpid()),
                )
                and str(held.get("worker_id") or "") == str(worker_id)
            )
            if live and not exact:
                # Low-level claim leases used by store tests may hand off only
                # after their prior task is no longer running. Runtime leases
                # cover the entire graph invocation and are never replaceable.
                if held.get("kind") != "claim" or any(
                    one.get("state") == "running" for one in document["tasks"]
                ):
                    return []
                live = False
            if not live:
                if str(held.get("worker_id") or "") and held.get("kind") == "runtime":
                    return []
                document["worker"] = self._scheduler_record(worker_id, kind="claim")
            self._refresh_waiting(document)
            if document["budget"]["provider_calls"] >= document["budget"]["max_provider_calls"]:
                document["status"] = "paused"
                document["note"] = "The explicit provider-call budget was reached."
                self._event(db, document, "goal_paused", payload={"reason": "provider_budget"})
                return []
            available_calls = (
                int(document["budget"]["max_provider_calls"])
                - int(document["budget"]["provider_calls"])
            )
            chosen: list[dict[str, Any]] = []
            agents = {one["id"]: one for one in document["agents"]}
            for task in document["tasks"]:
                if task["state"] != "ready" or not self._compatible(task, chosen):
                    continue
                if any(not _providers_independent(
                    agents.get(one["assigned_agent_id"]),
                    agents.get(task["assigned_agent_id"]),
                ) for one in chosen):
                    continue
                chosen.append(task)
                if len(chosen) >= min(int(document["policy"]["max_parallel"]), available_calls):
                    break
            for task in chosen:
                task.update({
                    "state": "running", "attempts": int(task["attempts"]) + 1,
                    "lease_id": uuid.uuid4().hex, "owner_pid": os.getpid(),
                    "owner_token": _process_token(os.getpid()), "updated_ms": _now(),
                    "outcome_unknown": False,
                    "claim_objective_epoch": int(document.get("objective_epoch") or 1),
                })
                self._event(db, document, "task_claimed", task_id=task["id"],
                            agent_id=task["assigned_agent_id"], payload={"lease_id": task["lease_id"], "attempt": task["attempts"]})
                self._event(db, document, "agent_started", task_id=task["id"],
                            agent_id=task["assigned_agent_id"], payload={"lease_id": task["lease_id"]})
            if chosen:
                document["status"] = "running"
                document["note"] = f"{len(chosen)} useful task(s) are running."
            return copy.deepcopy(chosen)
        return self._mutate(goal_id, change)[1]

    def record_dispatch(
        self, goal_id: str, task: dict[str, Any], prompt_digest: str, *, phase: str = "initial"
    ) -> None:
        def change(document: dict[str, Any], db: sqlite3.Connection):
            current = next(one for one in document["tasks"] if one["id"] == task["id"])
            if current["lease_id"] != task["lease_id"] or current["state"] != "running":
                raise HarnessError("That task lease is stale; Nexus did not dispatch it")
            if document["status"] in {
                "paused", "waiting_for_user", "cancelled", "cancelling",
            } \
                    or int(current.get("claim_objective_epoch") or 0) \
                    != int(document.get("objective_epoch") or 1):
                raise HarnessError("The goal changed or paused before this provider continuation")
            if document["budget"]["provider_calls"] >= document["budget"]["max_provider_calls"]:
                raise HarnessError("The explicit provider-call budget was reached before dispatch")
            document["budget"]["provider_calls"] += 1
            queue = document.get("project_queue") or {}
            if queue.get("auto_start_pending") is True:
                queue["auto_start_pending"] = False
                self._event(db, document, "goal_auto_start_consumed", payload={
                    "task_id": current["id"],
                })
            current["provider_effect_state"] = "dispatched"
            current["provider_effect_id"] = _stable_id(
                "effect", goal_id, current["id"], current["attempts"],
                document["budget"]["provider_calls"], prompt_digest,
            )
            self._event(db, document, "provider_dispatched", task_id=current["id"],
                        agent_id=current["assigned_agent_id"], payload={
                            "prompt_sha256": prompt_digest, "attempt": current["attempts"],
                            "phase": phase,
                            "provider_call": document["budget"]["provider_calls"],
                            "effect_id": current["provider_effect_id"],
                        }, run_id=goal_id)
        self._mutate(goal_id, change)

    def record_provider_reply(
        self, goal_id: str, task: dict[str, Any], *, phase: str
    ) -> None:
        """Durably receipt one physical provider call before parsing/repair."""

        def change(document: dict[str, Any], db: sqlite3.Connection):
            current = next(one for one in document["tasks"] if one["id"] == task["id"])
            if current.get("lease_id") != task.get("lease_id") or current["state"] != "running":
                raise HarnessError("That task lease is stale; Nexus did not receipt the provider reply")
            if current.get("provider_effect_state") != "dispatched" \
                    or not current.get("provider_effect_id"):
                raise HarnessError("There is no dispatched provider effect to receipt")
            current["provider_effect_state"] = "reply_received"
            self._event(
                db, document, "provider_reply_received",
                task_id=current["id"], agent_id=current["assigned_agent_id"],
                payload={
                    "effect_id": current["provider_effect_id"],
                    "phase": phase,
                }, run_id=goal_id,
            )

        self._mutate(goal_id, change)

    def block_received_reply(
        self, goal_id: str, task: dict[str, Any], error: str
    ) -> None:
        """Require reconciliation when parsing/repair stopped after a real reply."""

        def change(document: dict[str, Any], db: sqlite3.Connection):
            current = next(one for one in document["tasks"] if one["id"] == task["id"])
            if current.get("lease_id") != task.get("lease_id"):
                return
            current.update({
                "state": "blocked", "last_error": _short(error, 4_000),
                "lease_id": "", "owner_pid": 0, "owner_token": "",
                "outcome_unknown": False, "reconciliation_required": True,
                "provider_effect_state": "reply_received_reconciliation_required",
            })
            if document["status"] not in {
                "cancelled", "waiting_for_user", "cancelling",
            }:
                document["status"] = "paused"
            document["note"] = current["last_error"]
            self._event(
                db, document, "provider_reply_reconciliation_required",
                task_id=current["id"], agent_id=current["assigned_agent_id"],
                payload={"error": current["last_error"], "retry_requires_user": True},
            )

        self._mutate(goal_id, change)

    def reserve_context_tool(
        self, goal_id: str, task: dict[str, Any], call: dict[str, Any]
    ) -> bool:
        def change(document: dict[str, Any], db: sqlite3.Connection):
            current = next(one for one in document["tasks"] if one["id"] == task["id"])
            if current.get("lease_id") != task.get("lease_id") or current["state"] != "running":
                raise HarnessError("That task lease is stale; Nexus did not run its context tool")
            if document["status"] in {
                "paused", "waiting_for_user", "cancelled", "cancelling",
            } \
                    or int(current.get("claim_objective_epoch") or 0) \
                    != int(document.get("objective_epoch") or 1):
                raise HarnessError("The goal changed or paused before this context tool could run")
            steps = current.get("context_steps") or []
            call_id = str(call.get("call_id") or "")
            if steps and call_id in set(steps[-1].get("reserved_call_ids") or []):
                return False
            used = int(document["budget"].get("context_tool_calls") or 0)
            maximum = int(document["budget"].get("max_context_tool_calls") or MAX_CONTEXT_TOOL_CALLS)
            if used >= maximum:
                raise HarnessError("The explicit context-tool call budget was reached")
            document["budget"]["context_tool_calls"] = used + 1
            if steps:
                steps[-1].setdefault("reserved_call_ids", []).append(call_id)
            self._event(db, document, "context_tool_requested", task_id=current["id"],
                        agent_id=current["assigned_agent_id"], payload={
                            "call_id": call.get("call_id"), "name": call.get("name"),
                            "arguments_sha256": hashlib.sha256(
                                _canonical(call.get("arguments", {})).encode("utf-8")
                            ).hexdigest(),
                            "tool_call": used + 1,
                        }, run_id=goal_id)
            return True
        return bool(self._mutate(goal_id, change)[1])

    def acknowledge_context_step(
        self, goal_id: str, task: dict[str, Any], action: dict[str, Any], phase: str
    ) -> None:
        def change(document: dict[str, Any], db: sqlite3.Connection):
            current = next(one for one in document["tasks"] if one["id"] == task["id"])
            if current.get("lease_id") != task.get("lease_id") or current["state"] != "running":
                raise HarnessError("The context-tool request belongs to a stale task lease")
            if document.get("status") == "cancelling":
                raise HarnessError("The goal is draining cancellation before context tools run")
            if int(current.get("claim_objective_epoch") or 0) != int(document.get("objective_epoch") or 1):
                raise HarnessError("The context-tool request was superseded by user steering")
            calls = copy.deepcopy(action.get("tool_calls") or [])
            if not calls:
                raise HarnessError("A context step must contain at least one tool request")
            step = {
                "step_id": _stable_id(
                    "context", goal_id, current["id"], current.get("attempts"),
                    current.get("provider_effect_id"), _canonical(calls),
                ),
                "phase": _short(phase, 100), "calls": calls, "results": [],
                "provider_effect_id": current.get("provider_effect_id", ""),
                "state": "tools_pending", "created_ms": _now(),
            }
            history = current.setdefault("context_steps", [])
            if not history or history[-1].get("step_id") != step["step_id"]:
                history.append(step)
            current["provider_effect_state"] = "context_step_acknowledged"
            self._event(db, document, "context_step_acknowledged", task_id=current["id"],
                        agent_id=current["assigned_agent_id"], payload={
                            "step_id": step["step_id"], "phase": phase,
                            "calls": calls, "effect_id": current.get("provider_effect_id", ""),
                        }, run_id=goal_id)
        self._mutate(goal_id, change)

    def record_context_tool_result(
        self, goal_id: str, task: dict[str, Any], call: dict[str, Any],
        result: object = None, *, error: str = "",
    ) -> None:
        def change(document: dict[str, Any], db: sqlite3.Connection):
            current = next(one for one in document["tasks"] if one["id"] == task["id"])
            if current.get("lease_id") != task.get("lease_id") or current["state"] != "running":
                raise HarnessError("The context-tool result belongs to a stale task lease")
            payload = {
                "call_id": call.get("call_id"), "name": call.get("name"),
                "result": _durable_evidence(result), "error": _short(error, 4_000),
            }
            steps = current.get("context_steps") or []
            if steps:
                step = steps[-1]
                if not any(str(one.get("call_id") or "") == str(call.get("call_id") or "")
                           for one in step.get("results", [])):
                    step.setdefault("results", []).append(copy.deepcopy(payload))
                completed_ids = {str(one.get("call_id") or "") for one in step.get("results", [])}
                requested_ids = {str(one.get("call_id") or "") for one in step.get("calls", [])}
                if requested_ids and requested_ids <= completed_ids:
                    step["state"] = "complete"
                    step["completed_ms"] = _now()
            if not error and str(call.get("name") or "") == "read_proposed_change":
                arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
                relative = str(arguments.get("path") or "").replace("\\", "/").strip()
                if relative and isinstance(result, dict):
                    offset = int(result.get("offset") or 0)
                    length = len(str(result.get("content") or ""))
                    total = int(result.get("total_characters") or 0)
                    ranges = current.setdefault("review_path_ranges", {}).setdefault(relative, [])
                    ranges.append([offset, offset + length])
                    covered = 0
                    for start, end in sorted(ranges):
                        if start > covered:
                            break
                        covered = max(covered, end)
                    inspected = current.setdefault("review_paths_inspected", [])
                    if covered >= total and relative not in inspected:
                        inspected.append(relative)
            elif not error and str(call.get("name") or "") == "read_file" \
                    and current.get("kind") == "review" and current.get("review_of"):
                parent = next((
                    one for one in document["tasks"] if one["id"] == current["review_of"]
                ), None)
                if parent and not parent.get("pending_action"):
                    arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
                    relative = str(arguments.get("path") or "").replace("\\", "/").strip()
                    inspected = current.setdefault("review_paths_inspected", [])
                    if relative and relative not in inspected:
                        inspected.append(relative)
            self._event(db, document, "context_tool_failed" if error else "context_tool_result",
                        task_id=current["id"], agent_id=current["assigned_agent_id"],
                        payload=payload, run_id=goal_id)
        self._mutate(goal_id, change)

    def record_action(self, goal_id: str, task: dict[str, Any], action: dict[str, Any]) -> bool:
        action = self.sanitize_action(action)
        def change(document: dict[str, Any], db: sqlite3.Connection):
            current = next(one for one in document["tasks"] if one["id"] == task["id"])
            if int(task.get("claim_objective_epoch") or 0) != int(document.get("objective_epoch") or 1):
                current.update({
                    "state": "ready", "lease_id": "", "owner_pid": 0, "owner_token": "",
                    "pending_action": {}, "pending_transaction": {}, "outcome_unknown": False,
                    "provider_effect_state": "superseded_by_steering",
                    "last_error": "",
                })
                self._event(db, document, "provider_result_superseded", task_id=current["id"],
                            agent_id=current["assigned_agent_id"], payload={
                                "claimed_objective_epoch": current.get("claim_objective_epoch", 0),
                                "current_objective_epoch": document.get("objective_epoch", 1),
                                "effect_id": current.get("provider_effect_id", ""),
                            }, run_id=goal_id)
                if document.get("status") != "cancelling":
                    document["status"] = "queued"
                    document["note"] = "A stale provider result was discarded after user steering."
                return False
            if current["lease_id"] != task["lease_id"] or current["state"] != "running":
                raise HarnessError("A late agent result cannot overwrite the task's current owner")
            if document["status"] == "cancelled":
                raise HarnessError("The goal was cancelled while its provider call was in flight")
            action_bytes = len(_canonical(action).encode("utf-8"))
            if action_bytes > MAX_PENDING_ACTION_BYTES:
                raise HarnessError(
                    f"The structured action is {action_bytes:,} bytes, above the durable "
                    f"{MAX_PENDING_ACTION_BYTES:,}-byte acknowledgement limit"
                )
            current["pending_action"] = copy.deepcopy(action)
            current["provider_effect_state"] = "acknowledged"
            current["reconciliation_required"] = False
            self._event(db, document, "provider_acknowledged", task_id=current["id"],
                        agent_id=current["assigned_agent_id"], payload={
                            "action": action["action"], "summary": action["summary"],
                            "effect_id": current.get("provider_effect_id", ""),
                        }, run_id=goal_id)
            self._event(db, document, "agent_stopped", task_id=current["id"],
                        agent_id=current["assigned_agent_id"], payload={"outcome": "structured_action"})
            return True
        return bool(self._mutate(goal_id, change)[1])

    def fail_task(
        self, goal_id: str, task: dict[str, Any], error: str, *,
        uncertain: bool = False, allow_failover: bool = False,
    ) -> None:
        def change(document: dict[str, Any], db: sqlite3.Connection):
            if document["status"] in TERMINAL_GOALS:
                return
            current = next(one for one in document["tasks"] if one["id"] == task["id"])
            if current.get("lease_id") != task.get("lease_id"):
                return
            previous_agent = str(current.get("assigned_agent_id") or "")
            previous_record = next((
                one for one in document.get("agents", [])
                if str(one.get("id") or "") == previous_agent
            ), {})
            previous_route = str(previous_record.get("who") or "")
            previous_identity = _provider_identity(previous_record)
            reply_was_received = str(current.get("provider_effect_state") or "") \
                in {"reply_received", "reply_received_reconciliation_required"}
            current.update({"state": "blocked", "last_error": _short(error, 4_000),
                            "owner_pid": 0, "owner_token": "", "lease_id": ""})
            current["outcome_unknown"] = bool(uncertain)
            current["reconciliation_required"] = bool(
                current.get("reconciliation_required") or reply_was_received
            )
            current["provider_effect_state"] = (
                "outcome_unknown" if uncertain else
                "known_reply_failed" if reply_was_received else "failed_before_effect"
            )
            failed_ids = list(dict.fromkeys([
                *[str(one) for one in current.get("failed_agent_ids", []) if str(one)],
                previous_agent,
            ]))
            current["failed_agent_ids"] = failed_ids[-len(document.get("agents", [])):]
            failed_routes = list(dict.fromkeys([
                *[str(one) for one in current.get("failed_provider_routes", []) if str(one)],
                previous_route,
            ]))
            current["failed_provider_routes"] = [one for one in failed_routes if one][
                -len(document.get("agents", [])):
            ]
            failed_identities = list(dict.fromkeys([
                *[
                    str(one) for one in current.get("failed_provider_identities", [])
                    if re.fullmatch(r"[0-9a-f]{64}", str(one))
                ],
                previous_identity,
            ]))
            current["failed_provider_identities"] = [
                one for one in failed_identities if one
            ][-len(document.get("agents", [])):]
            history = current.setdefault("provider_failures", [])
            history.append({
                "agent_id": previous_agent, "error": current["last_error"],
                "provider_route": previous_route,
                "provider_identity_sha256": previous_identity,
                "at_ms": _now(), "outcome_unknown": bool(uncertain),
            })
            del history[:-12]
            if document.get("status") == "cancelling":
                self._event(
                    db, document, "task_drained_for_cancellation",
                    task_id=current["id"], agent_id=current["assigned_agent_id"],
                    payload={"error": current["last_error"], "uncertain": bool(uncertain)},
                )
                return
            replacement = None
            if allow_failover and not uncertain and not current.get("required_contributor_id"):
                # Route aliases are not independent failover. Once a provider
                # route has failed this task, keep it excluded for every later
                # attempt rather than cycling A -> B -> alias-of-A.
                forbidden_routes = set(current["failed_provider_routes"])
                forbidden_identities = set(current["failed_provider_identities"])
                if current.get("kind") == "review" and current.get("review_of"):
                    parent = next((
                        one for one in document["tasks"]
                        if one["id"] == current.get("review_of")
                    ), None)
                    author = next((
                        one for one in document.get("agents", [])
                        if parent and one["id"] == parent.get("assigned_agent_id")
                    ), {})
                    forbidden_routes.add(str(author.get("who") or ""))
                    author_identity = _provider_identity(author)
                    if author_identity:
                        forbidden_identities.add(author_identity)
                replacement = next((
                    one for one in document.get("agents", [])
                    if str(one.get("id") or "") not in failed_ids
                    and str(one.get("who") or "")
                    and str(one.get("who") or "") not in forbidden_routes
                    and _provider_identity(one)
                    and _provider_identity(one) not in forbidden_identities
                ), None)
            if replacement is not None:
                current.update({
                    "assigned_agent_id": str(replacement["id"]),
                    "state": "ready", "outcome_unknown": False,
                    "provider_effect_state": "known_failure_reassigned",
                })
                # A provider failover may prepare a different ready owner, but
                # it must never override a user Pause or waiting-for-user
                # boundary. Resume will admit the prepared task later.
                if document["status"] not in {"paused", "waiting_for_user", "cancelling"}:
                    document["status"] = "queued"
                document["note"] = (
                    f"{previous_agent} failed with a known provider outcome; Nexus reassigned "
                    f"the saved task to {replacement['id']} without discarding progress."
                )
                self._event(
                    db, document, "task_reassigned_after_provider_failure",
                    task_id=current["id"], agent_id=str(replacement["id"]),
                    payload={
                        "from_agent_id": previous_agent,
                        "to_agent_id": str(replacement["id"]),
                        "error": current["last_error"],
                    },
                )
                return
            document["status"] = "paused"
            document["note"] = current["last_error"]
            self._event(db, document, "provider_outcome_unknown" if uncertain else "task_failed",
                        task_id=current["id"], agent_id=current["assigned_agent_id"],
                        payload={"error": current["last_error"], "retry_requires_user": uncertain})
        self._mutate(goal_id, change)

    def defer_pending_action(self, goal_id: str, task: dict[str, Any]) -> None:
        def change(document: dict[str, Any], db: sqlite3.Connection):
            current = next(one for one in document["tasks"] if one["id"] == task["id"])
            if current.get("lease_id") != task.get("lease_id") or not current.get("pending_action"):
                raise HarnessError("The in-flight action is no longer current")
            current["state"] = "pending_apply"
            self._event(db, document, "task_apply_deferred", task_id=current["id"],
                        agent_id=current["assigned_agent_id"], payload={"reason": document["status"]})
        self._mutate(goal_id, change)

    def defer_context_continuation(self, goal_id: str, task: dict[str, Any]) -> None:
        def change(document: dict[str, Any], db: sqlite3.Connection):
            current = next(one for one in document["tasks"] if one["id"] == task["id"])
            if current.get("lease_id") != task.get("lease_id"):
                return
            current.update({
                "state": "ready", "lease_id": "", "owner_pid": 0, "owner_token": "",
                "updated_ms": _now(),
            })
            self._event(db, document, "context_step_deferred", task_id=current["id"],
                        agent_id=current["assigned_agent_id"], payload={
                            "reason": document["status"],
                            "objective_epoch": document.get("objective_epoch", 1),
                        })
        self._mutate(goal_id, change)

    def acknowledge_file_request(
        self, goal_id: str, task: dict[str, Any], paths: list[str], phase: str
    ) -> None:
        def change(document: dict[str, Any], db: sqlite3.Connection):
            current = next(one for one in document["tasks"] if one["id"] == task["id"])
            if current.get("lease_id") != task.get("lease_id") or current["state"] != "running":
                raise HarnessError("The requested-file continuation belongs to a stale task lease")
            if document.get("status") == "cancelling":
                raise HarnessError("The goal is draining cancellation before more file context")
            if int(current.get("claim_objective_epoch") or 0) != int(document.get("objective_epoch") or 1):
                raise HarnessError("The requested-file continuation was superseded by user steering")
            step = {
                "step_id": _stable_id(
                    "files", goal_id, current["id"], current.get("attempts"),
                    current.get("provider_effect_id"), *paths,
                ),
                "phase": _short(phase, 100), "calls": [], "results": [],
                "requested_files": list(paths), "provider_effect_id": current.get("provider_effect_id", ""),
                "state": "complete", "created_ms": _now(), "completed_ms": _now(),
            }
            history = current.setdefault("context_steps", [])
            if not history or history[-1].get("step_id") != step["step_id"]:
                history.append(step)
            current["provider_effect_state"] = "context_step_acknowledged"
            self._event(db, document, "file_context_requested", task_id=current["id"],
                        agent_id=current["assigned_agent_id"], payload={
                            "paths": paths, "effect_id": current.get("provider_effect_id", ""),
                        }, run_id=goal_id)
        self._mutate(goal_id, change)

    def prepare_transaction(
        self, goal_id: str, task: dict[str, Any], transaction_id: str,
        changes: list[dict[str, Any]],
    ) -> None:
        def change(document: dict[str, Any], db: sqlite3.Connection):
            current = next(one for one in document["tasks"] if one["id"] == task["id"])
            if document.get("status") == "cancelling":
                raise HarnessError("The goal is draining cancellation before file preparation")
            if current.get("lease_id") != task.get("lease_id") or current["state"] not in {"running", "pending_apply"}:
                raise HarnessError("The task lease changed before its file transaction was prepared")
            current["pending_transaction"] = {
                "transaction_id": transaction_id,
                "paths": [str(one.get("path") or "") for one in changes],
                "changes_sha256": hashlib.sha256(_canonical(changes).encode("utf-8")).hexdigest(),
                "state": "prepared", "artifact": {},
            }
            self._event(db, document, "file_transaction_prepared", task_id=current["id"],
                        agent_id=current["assigned_agent_id"], payload=current["pending_transaction"])
        self._mutate(goal_id, change)

    def record_transaction_applied(
        self, goal_id: str, task: dict[str, Any], artifact: dict[str, Any]
    ) -> None:
        def change(document: dict[str, Any], db: sqlite3.Connection):
            current = next(one for one in document["tasks"] if one["id"] == task["id"])
            pending = current.get("pending_transaction") or {}
            if pending.get("transaction_id") != artifact.get("transaction_id"):
                raise HarnessError("The applied file transaction does not match the prepared transaction")
            pending["state"] = "applied"
            pending["artifact"] = _bounded_json(artifact, 100_000)
            self._event(db, document, "file_transaction_applied", task_id=current["id"],
                        agent_id=current["assigned_agent_id"], payload=artifact)
        self._mutate(goal_id, change)

    @staticmethod
    def _review_packet_sha256(task: dict[str, Any], action: dict[str, Any]) -> str:
        return hashlib.sha256(_canonical({
            "task_id": task["id"],
            "provider_effect_id": task.get("provider_effect_id", ""),
            "summary": action.get("summary", ""),
            "risk": action.get("risk", "low"),
            "changes": action.get("changes", []),
            "evidence": action.get("evidence", []),
            "criteria_evidence": action.get("criteria_evidence", []),
        }).encode("utf-8")).hexdigest()

    def stage_review_if_needed(
        self, goal_id: str, task: dict[str, Any], action: dict[str, Any]
    ) -> tuple[bool, list[str]]:
        """Durably gate risky work before any project file can be changed."""
        interrupt_ids: list[str] = []

        def change(document: dict[str, Any], db: sqlite3.Connection):
            if document.get("status") == "cancelling":
                raise HarnessError("The goal is draining cancellation before risk review")
            current = next(one for one in document["tasks"] if one["id"] == task["id"])
            if current.get("lease_id") != task.get("lease_id") \
                    or current["state"] not in {"running", "pending_apply"}:
                raise HarnessError("The task lease changed before risk review")
            effect_id = str(current.get("provider_effect_id") or "")
            approved = bool(effect_id) and str(current.get("review_approved_effect_id") or "") == effect_id
            needed = (not approved) and (
                str(action.get("action") or "") == "request_review"
                or self._needs_review(document, current, action, None)
            )
            if not needed:
                return False
            if current.get("kind") == "review":
                raise HarnessError("A review task cannot create a review-of-review ritual")
            packet_sha = self._review_packet_sha256(current, action)
            current["review_packet_sha256"] = packet_sha
            owner = next(one for one in document["agents"] if one["id"] == current["assigned_agent_id"])
            reviewer = next((
                one for one in document["agents"]
                if one["id"] != current["assigned_agent_id"]
                and _providers_independent(one, owner)
            ), None)
            if reviewer is not None and len(document["tasks"]) < int(document["policy"]["max_tasks"]):
                review_id = _stable_id(
                    "review", goal_id, current["id"], current["attempts"], effect_id, packet_sha
                )
                if not any(one["id"] == review_id for one in document["tasks"]):
                    proposed_changes = [
                        one for one in action.get("changes", []) if isinstance(one, dict)
                    ]
                    required_paths = [
                        str(one.get("path") or "").replace("\\", "/").strip()
                        for one in proposed_changes if str(one.get("path") or "").strip()
                    ]
                    inline_paths = [
                        str(one.get("path") or "").replace("\\", "/").strip()
                        for one in proposed_changes
                        if str(one.get("path") or "").strip()
                        and (one.get("delete") is True or len(str(one.get("content") or "")) <= 4_000)
                    ]
                    document["tasks"].append({
                        "id": review_id, "title": f"Review: {current['title']}",
                        "description": (
                            "Independently review only the linked proposed action, its exact diff/content, "
                            "evidence, and verification state. Return a structured verdict and cite the "
                            f"review packet as review-packet:{packet_sha}."
                        ),
                        "kind": "review", "state": "ready", "depends_on": [],
                        "parent_id": current["id"], "review_of": current["id"],
                        "review_packet_sha256": packet_sha,
                        "review_required_paths": required_paths,
                        "review_paths_inspected": inline_paths,
                        "assigned_agent_id": reviewer["id"], "parallel_safe": True,
                        "resource_paths": [], "attempts": 0, "no_progress": 0,
                        "lease_id": "", "owner_pid": 0, "owner_token": "",
                        "created_ms": _now(), "updated_ms": _now(), "summary": "",
                        "last_error": "", "evidence": [], "artifacts": [],
                        "criteria_evidence": [], "provider_effect_state": "never_dispatched",
                        "provider_effect_id": "", "claim_objective_epoch": 0,
                        "outcome_unknown": False, "pending_action": {}, "pending_transaction": {},
                    })
                    document["budget"]["tasks_created"] += 1
                    self._event(db, document, "review_requested", task_id=review_id,
                                agent_id=reviewer["id"], payload={
                                    "review_of": current["id"], "risk": action.get("risk"),
                                    "review_packet_sha256": packet_sha,
                                    "before_file_mutation": True,
                                })
                current["state"] = "waiting_review"
                document["status"] = "queued"
                document["note"] = "Risky proposed work is awaiting independent review before file mutation."
                return True

            question = user_questions.normalize([{
                "id": "review-without-independent-agent",
                "prompt": "This proposed change is high risk, but no independent reviewer is available. Continue using deterministic checks only?",
                "options": [
                    {"label": "Continue with checks", "description": "Authorize this exact saved proposal without an independent agent review.", "recommended": True},
                    {"label": "Stop this task", "description": "Reject the proposal without changing project files.", "recommended": False},
                ],
                "multiple": False, "allow_other": False,
            }])[0]
            question_fingerprint = hashlib.sha256(_canonical({
                "reason": "risky_action", "questions": [question],
                "proposal": {
                    "action": str(action.get("action") or ""),
                    "summary": str(action.get("summary") or ""),
                    "risk": str(action.get("risk") or ""),
                    "changes": action.get("changes", []),
                    "evidence": action.get("evidence", []),
                    "criteria_evidence": action.get("criteria_evidence", []),
                },
            }).encode("utf-8")).hexdigest()
            if question_fingerprint == current.get("question_fingerprint"):
                current["question_repeat_count"] = int(
                    current.get("question_repeat_count") or 0
                ) + 1
            else:
                current["question_fingerprint"] = question_fingerprint
                current["question_repeat_count"] = 0
            if int(current.get("question_repeat_count") or 0) >= MAX_NO_PROGRESS:
                current.update({
                    "state": "blocked",
                    "last_error": "The same risky proposal repeatedly returned after the user rejected it.",
                    "pending_action": {}, "pending_transaction": {}, "lease_id": "",
                    "owner_pid": 0, "owner_token": "",
                })
                document["status"] = "paused"
                document["note"] = current["last_error"]
                self._event(db, document, "task_blocked", task_id=current["id"],
                            agent_id=current["assigned_agent_id"], payload={
                                "reason": "repeated_risk_question",
                                "question_sha256": question_fingerprint,
                            })
                return True
            request_id = uuid.uuid4().hex
            request = {
                "id": request_id, "state": "pending", "reason": "risky_action",
                "goal_id": goal_id, "task_id": current["id"],
                "agent_id": current["assigned_agent_id"], "created_ms": _now(),
                "resolved_ms": 0, "goal_revision": int(document["revision"]) + 1,
                "questions": [question], "answer": "", "purpose": "risk_review",
                "review_packet_sha256": packet_sha, "actions": ["respond"],
            }
            document["interrupts"].append(request)
            interrupt_ids.append(request_id)
            current["state"] = "waiting_review"
            document["status"] = "waiting_for_user"
            document["note"] = "Risky proposed work needs a user decision before file mutation."
            self._event(db, document, "interrupt_asked", task_id=current["id"], payload=request)
            return True

        document, staged = self._mutate(goal_id, change)
        return bool(staged), interrupt_ids

    def apply_action(
        self, goal_id: str, task: dict[str, Any], action: dict[str, Any],
        *, artifact: dict[str, Any] | None = None,
    ) -> list[str]:
        action = self.sanitize_action(action)
        interrupt_ids: list[str] = []
        def change(document: dict[str, Any], db: sqlite3.Connection):
            nonlocal interrupt_ids
            current = next(one for one in document["tasks"] if one["id"] == task["id"])
            if current["lease_id"] != task["lease_id"] or current["state"] not in {"running", "pending_apply"}:
                raise HarnessError("A stale task action cannot change long-horizon state")
            if document["status"] in {"cancelled", "cancelling"}:
                raise HarnessError(
                    "The goal was cancelled or is draining before this result could be applied"
                )
            _validate_action_semantics(action, current)
            current["pending_action"] = {}
            current["summary"] = _short(action.get("summary"), 8_000)
            evidence = [_short(one, 1_000) for one in action.get("evidence", []) if _short(one, 1_000)]
            current["evidence"].extend(one for one in evidence if one not in current["evidence"])
            valid_criteria = set(document["success_criteria"])
            current["criteria_evidence"] = [
                {
                    "criterion": _short(one.get("criterion"), 1_000),
                    "evidence_refs": [_short(ref, 500) for ref in one.get("evidence_refs", []) if _short(ref, 500)],
                }
                for one in action.get("criteria_evidence", []) if isinstance(one, dict)
                and _short(one.get("criterion"), 1_000) in valid_criteria
            ]
            if artifact:
                # A provider can explicitly say that a criterion is supported
                # by a verified no-change observation, but it cannot know the
                # authenticated Merkle root until Nexus computes it after the
                # provider response. Bind that reserved declaration to the
                # exact snapshot created at this apply boundary. Generic prose
                # remains untrusted and is never promoted to artifact evidence.
                if artifact.get("kind") == "verified_no_change" \
                        and str(artifact.get("tree_merkle") or ""):
                    snapshot_ref = "snapshot:" + str(artifact["tree_merkle"])
                    for mapping in current["criteria_evidence"]:
                        mapping["evidence_refs"] = list(dict.fromkeys(
                            snapshot_ref
                            if ref == "verified-no-change" or ref.startswith("verified-no-change:")
                            else ref
                            for ref in mapping.get("evidence_refs", [])
                        ))
                current["artifacts"].append(_bounded_json(artifact, 100_000))
                document["artifacts"].append(_bounded_json({"task_id": current["id"], **artifact}, 100_000))
                self._event(db, document, "artifact_changed", task_id=current["id"],
                            agent_id=current["assigned_agent_id"], payload=artifact)
            kind = str(action["action"])
            if kind == "ask_user":
                reason = str(action.get("interrupt_reason") or "")
                if reason not in INTERRUPT_REASONS:
                    raise HarnessError("The agent tried to interrupt for a non-permitted reason")
                questions = user_questions.frozen(action.get("questions"))
                if not questions:
                    raise HarnessError("A user interrupt must contain a structured question")
                question_fingerprint = hashlib.sha256(
                    _canonical({"reason": reason, "questions": questions}).encode("utf-8")
                ).hexdigest()
                if question_fingerprint == current.get("question_fingerprint"):
                    current["question_repeat_count"] = int(current.get("question_repeat_count") or 0) + 1
                else:
                    current["question_fingerprint"] = question_fingerprint
                    current["question_repeat_count"] = 0
                if int(current.get("question_repeat_count") or 0) >= MAX_NO_PROGRESS:
                    current["state"] = "blocked"
                    current["last_error"] = (
                        "The agent repeated the same already-answered question without making progress."
                    )
                    document["status"] = "paused"
                    document["note"] = current["last_error"]
                    self._event(db, document, "task_blocked", task_id=current["id"],
                                agent_id=current["assigned_agent_id"], payload={
                                    "reason": "repeated_user_question",
                                    "question_sha256": question_fingerprint,
                                })
                    current.update({
                        "lease_id": "", "owner_pid": 0, "owner_token": "",
                        "pending_transaction": {}, "updated_ms": _now(),
                    })
                    return
                interrupt_id = uuid.uuid4().hex
                request = {
                    "id": interrupt_id, "state": "pending", "reason": reason,
                    "goal_id": goal_id, "task_id": current["id"],
                    "agent_id": current["assigned_agent_id"],
                    "created_ms": _now(), "resolved_ms": 0,
                    "goal_revision": int(document["revision"]) + 1,
                    "questions": questions, "answer": "",
                    "actions": ["respond"],
                }
                document["interrupts"].append(request)
                interrupt_ids.append(interrupt_id)
                current["state"] = "waiting"
                document["status"] = "waiting_for_user"
                document["note"] = "An agent needs a real user decision before continuing."
                self._event(db, document, "interrupt_asked", task_id=current["id"],
                            agent_id=current["assigned_agent_id"], payload=request)
            elif kind == "handoff":
                target = str(action.get("handoff_agent_id") or "")
                if target not in {one["id"] for one in document["agents"]}:
                    raise HarnessError("The requested handoff agent is not authorized for this project")
                old = current["assigned_agent_id"]
                if target == old:
                    raise HarnessError("A task cannot be handed off to the same agent")
                if current.get("required_contributor_id"):
                    raise HarnessError(
                        "This chat-bound task is the named agent's required contribution and cannot be handed off"
                    )
                handoff_fingerprint = hashlib.sha256(_canonical({
                    "evidence": current["evidence"], "artifacts": current["artifacts"],
                }).encode("utf-8")).hexdigest()
                if handoff_fingerprint == current.get("handoff_progress_fingerprint"):
                    current["no_progress"] = int(current.get("no_progress") or 0) + 1
                else:
                    current["handoff_progress_fingerprint"] = handoff_fingerprint
                    current["no_progress"] = 0
                current.update({"assigned_agent_id": target, "state": "ready", "last_error": ""})
                if current["no_progress"] >= MAX_NO_PROGRESS:
                    current["state"] = "blocked"
                    current["last_error"] = "Repeated agent handoffs produced no new evidence or artifact."
                self._event(db, document, "task_handed_off", task_id=current["id"], agent_id=target,
                            payload={"from_agent_id": old, "to_agent_id": target,
                                     "no_progress": current["no_progress"]})
            elif kind == "delegate":
                delegation_fingerprint = hashlib.sha256(_canonical([
                    {
                        "title": _short(one.get("title"), 240),
                        "description": _short(one.get("description"), 2_000),
                        "assigned_agent_id": str(one.get("assigned_agent_id") or current["assigned_agent_id"]),
                        "depends_on": [str(dep) for dep in one.get("depends_on", [])],
                        "parallel_safe": one.get("parallel_safe") is True,
                        "resource_paths": [_short(path, 240) for path in one.get("resource_paths", [])],
                    }
                    for one in action.get("tasks", []) if isinstance(one, dict)
                ]).encode("utf-8")).hexdigest()
                if delegation_fingerprint == current.get("delegation_fingerprint"):
                    current["delegation_repeat_count"] = int(
                        current.get("delegation_repeat_count") or 0
                    ) + 1
                else:
                    current["delegation_fingerprint"] = delegation_fingerprint
                    current["delegation_repeat_count"] = 0
                if int(current.get("delegation_repeat_count") or 0) >= MAX_NO_PROGRESS:
                    current["state"] = "blocked"
                    current["last_error"] = (
                        "The agent repeatedly delegated the same subtask without making progress."
                    )
                    document["status"] = "paused"
                    document["note"] = current["last_error"]
                    self._event(db, document, "task_blocked", task_id=current["id"],
                                agent_id=current["assigned_agent_id"], payload={
                                    "reason": "repeated_delegation",
                                    "delegation_sha256": delegation_fingerprint,
                                })
                    created = []
                else:
                    created = self._add_tasks(document, current, list(action.get("tasks") or []), db)
                if not created and current["state"] != "blocked":
                    raise HarnessError("Delegation must create at least one concrete subtask")
                if created:
                    current["depends_on"] = list(dict.fromkeys([*current["depends_on"], *created]))
                    current["state"] = "waiting"
            elif kind == "request_review" and not (
                current.get("provider_effect_id")
                and str(current.get("review_approved_effect_id") or "")
                == str(current.get("provider_effect_id") or "")
            ):
                raise HarnessError("Risk review must be staged before applying the proposed action")
            elif kind == "blocked":
                if current.get("kind") == "review":
                    packet_ref = "review-packet:" + str(current.get("review_packet_sha256") or "")
                    missing_paths = set(current.get("review_required_paths") or []) - set(
                        current.get("review_paths_inspected") or []
                    )
                    if action.get("review_verdict") not in {"reject", "changes_requested"} \
                            or packet_ref not in action.get("evidence", []) \
                            or not action.get("review_findings") or missing_paths:
                        raise HarnessError(
                            "A review rejection needs a structured verdict, findings, and its exact review-packet reference"
                        )
                current["state"] = "blocked"
                current["last_error"] = current["summary"] or "The agent reported a concrete blocker."
                if current.get("review_of"):
                    parent = next((one for one in document["tasks"] if one["id"] == current["review_of"]), None)
                    if parent and parent["state"] == "waiting_review":
                        parent["state"] = "blocked"
                        parent["last_error"] = "Independent review did not accept the work: " + current["last_error"]
                        parent.update({
                            "pending_action": {}, "pending_transaction": {}, "lease_id": "",
                            "owner_pid": 0, "owner_token": "",
                        })
                self._event(db, document, "task_blocked", task_id=current["id"],
                            agent_id=current["assigned_agent_id"], payload={"reason": current["last_error"]})
            elif kind == "complete" or (
                kind == "request_review"
                and current.get("provider_effect_id")
                and str(current.get("review_approved_effect_id") or "")
                == str(current.get("provider_effect_id") or "")
            ):
                if current.get("kind") == "review":
                    packet_ref = "review-packet:" + str(current.get("review_packet_sha256") or "")
                    missing_paths = set(current.get("review_required_paths") or []) - set(
                        current.get("review_paths_inspected") or []
                    )
                    if action.get("review_verdict") != "approve" \
                            or packet_ref not in action.get("evidence", []) \
                            or not action.get("review_findings") or missing_paths:
                        raise HarnessError(
                            "Review approval needs an approve verdict, findings, and its exact review-packet reference"
                        )
                concrete = bool(current["artifacts"])
                if not concrete:
                    raise HarnessError("A task cannot complete until Nexus records a concrete artifact or verified no-change snapshot")
                current["state"] = "complete"
                self._event(db, document, "task_completed", task_id=current["id"],
                            agent_id=current["assigned_agent_id"], payload={"evidence": current["evidence"], "artifacts": current["artifacts"]})
                if current.get("review_of"):
                    parent = next((one for one in document["tasks"] if one["id"] == current["review_of"]), None)
                    if parent and parent["state"] == "waiting_review":
                        if parent.get("pending_action"):
                            parent["state"] = "pending_apply"
                            parent["review_approved_effect_id"] = parent.get("provider_effect_id", "")
                        else:
                            parent["state"] = str(parent.pop("review_return_state", "complete"))
                        parent["evidence"].append(
                            f"Independent review completed by {current['assigned_agent_id']} "
                            f"for review-packet:{current.get('review_packet_sha256', '')}"
                        )
            else:
                fingerprint = hashlib.sha256(_canonical({
                    "summary": current["summary"],
                    "evidence": evidence,
                    "artifact": _semantic_artifact(artifact or {}),
                }).encode("utf-8")).hexdigest()
                if fingerprint == current.get("progress_fingerprint"):
                    current["no_progress"] = int(current.get("no_progress") or 0) + 1
                else:
                    current["no_progress"] = 0
                current["progress_fingerprint"] = fingerprint
                if current["no_progress"] >= MAX_NO_PROGRESS:
                    current["state"] = "blocked"
                    current["last_error"] = "Repeated agent turns produced no new evidence or artifact."
                    document["status"] = "paused"
                    document["note"] = current["last_error"]
                else:
                    current["state"] = "ready"
                    self._event(db, document, "task_progress", task_id=current["id"],
                                agent_id=current["assigned_agent_id"], payload={"summary": current["summary"]})
            current.update({"lease_id": "", "owner_pid": 0, "owner_token": "", "updated_ms": _now()})
            current["pending_transaction"] = {}
            self._refresh_waiting(document)
            if document["status"] not in {"waiting_for_user", "paused", "cancelling"}:
                document["status"] = "queued"
                document["note"] = current["summary"] or "The scheduler is choosing the next useful task."
            return interrupt_ids
        return self._mutate(goal_id, change)[1]

    @staticmethod
    def _needs_review(document: dict[str, Any], task: dict[str, Any], action: dict[str, Any], artifact: object) -> bool:
        if task.get("kind") == "review":
            return False
        if task.get("provider_effect_id") and str(task.get("review_approved_effect_id") or "") \
                == str(task.get("provider_effect_id") or ""):
            return False
        threshold = str(document.get("policy", {}).get("review_risk") or "high")
        levels = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        changes = (
            list((artifact or {}).get("changes", []))
            if isinstance(artifact, dict) and (artifact or {}).get("changes")
            else list(action.get("changes") or [])
        )
        broad = len(changes) > 6
        paths = [str(one.get("path") or "").casefold() for one in changes]
        sensitive = any(re.search(
            r"(^|/)(\.github|\.gitlab|auth|security|permissions?|secrets?|config|migrations?|infra|deploy|package-lock\.json|pyproject\.toml)(/|$)",
            path.replace("\\", "/"),
        ) for path in paths)
        destructive = any(one.get("delete") is True
                          or str(one.get("kind") or "").casefold() in {"delete", "remove"}
                          for one in changes)
        failed_checks = str(document.get("verification", {}).get("status") or "") == "failed"
        return broad or sensitive or destructive or failed_checks or levels.get(
            str(action.get("risk") or "low"), 0
        ) >= levels.get(threshold, 2)

    def _add_tasks(
        self, document: dict[str, Any], parent: dict[str, Any], raw_tasks: list[dict[str, Any]],
        db: sqlite3.Connection,
    ) -> list[str]:
        if len(document["tasks"]) + len(raw_tasks) > int(document["policy"]["max_tasks"]):
            raise HarnessError("The bounded long-horizon task budget would be exceeded")
        known_agents = {one["id"] for one in document["agents"]}
        known_tasks = {one["id"] for one in document["tasks"]}
        dependency_graph = {
            one["id"]: list(one.get("depends_on", [])) for one in document["tasks"]
        }

        def reaches(start: str, target: str) -> bool:
            pending = [start]
            seen: set[str] = set()
            while pending:
                node = pending.pop()
                if node == target:
                    return True
                if node in seen:
                    continue
                seen.add(node)
                pending.extend(dependency_graph.get(node, []))
            return False

        created: list[str] = []
        for position, raw in enumerate(raw_tasks):
            if not isinstance(raw, dict):
                raise HarnessError("A delegated task is malformed")
            title = _short(raw.get("title"), 240)
            description = _short(raw.get("description"), 2_000)
            assigned = str(raw.get("assigned_agent_id") or parent["assigned_agent_id"])
            if not title or not description or assigned not in known_agents:
                raise HarnessError("A delegated task needs a title, description, and authorized agent")
            dependencies = [str(one) for one in raw.get("depends_on", [])]
            if any(one not in known_tasks and one not in created for one in dependencies):
                raise HarnessError("A delegated task names an unknown dependency")
            if parent["id"] in dependencies:
                raise HarnessError("A delegated task cannot depend on the parent that waits for it")
            if any(reaches(dependency, parent["id"]) for dependency in dependencies):
                raise HarnessError(
                    "Delegation would create a transitive dependency cycle with its waiting parent"
                )
            task_id = _stable_id("task", document["goal_id"], parent["id"], document["revision"], position, title)
            if task_id in known_tasks or task_id in created:
                raise HarnessError("A delegated task identity is duplicated")
            task = {
                "id": task_id, "title": title, "description": description,
                "kind": "work", "state": "ready" if not dependencies else "waiting",
                "depends_on": dependencies, "parent_id": parent["id"], "review_of": "",
                "assigned_agent_id": assigned, "parallel_safe": raw.get("parallel_safe") is True,
                "resource_paths": [_short(one, 240) for one in raw.get("resource_paths", []) if _short(one, 240)],
                "attempts": 0, "no_progress": 0, "lease_id": "", "owner_pid": 0,
                "owner_token": "", "created_ms": _now(), "updated_ms": _now(),
                "summary": "", "last_error": "", "evidence": [], "artifacts": [],
                "criteria_evidence": [],
                "provider_effect_state": "never_dispatched", "outcome_unknown": False,
                "provider_effect_id": "",
                "pending_action": {}, "pending_transaction": {},
            }
            document["tasks"].append(task)
            dependency_graph[task_id] = dependencies
            document["budget"]["tasks_created"] += 1
            created.append(task_id)
            self._event(db, document, "task_created", task_id=task_id, agent_id=assigned,
                        payload={"parent_id": parent["id"], "dependencies": dependencies, "parallel_safe": task["parallel_safe"]})
        return created

    def resolve_interrupts(self, goal_id: str, answers: object) -> None:
        envelope = answers if isinstance(answers, dict) else {}
        supplied = envelope.get("answers") if isinstance(envelope.get("answers"), dict) else {}
        expected_revision = int(envelope.get("expected_revision") or 0)
        expected_pending_ids = {
            str(one) for one in envelope.get("pending_ids", []) if str(one)
        } if isinstance(envelope.get("pending_ids"), list) else set()
        def change(document: dict[str, Any], db: sqlite3.Connection):
            if document["status"] in TERMINAL_GOALS | {"cancelling"}:
                raise HarnessError("A terminal goal is immutable; its old decision cards cannot be answered")
            risk_rejected = False
            pending = [one for one in document["interrupts"] if one["state"] == "pending"]
            if not pending:
                raise HarnessError("That goal has no pending user interrupt")
            actual_pending_ids = {str(one["id"]) for one in pending}
            if expected_revision != int(document["revision"]) or expected_pending_ids != actual_pending_ids:
                raise HarnessError(
                    "The goal or its pending questions changed after this decision card was shown; refresh before answering"
                )
            for item in pending:
                answer = supplied.get(item["id"])
                if answer is None:
                    raise HarnessError("Answer every pending question for this exact goal")
                item.update({
                    "state": "resolved", "resolved_ms": _now(),
                    "answer": _short(self.redactor.text(answer), 20_000),
                })
                task = next(one for one in document["tasks"] if one["id"] == item["task_id"])
                task["evidence"].append("User decision: " + item["answer"])
                if item.get("purpose") == "risk_review":
                    normalized_answer = item["answer"].strip().casefold()
                    labels = [
                        str(option.get("label") or "").strip()
                        for question in item.get("questions", []) if isinstance(question, dict)
                        for option in question.get("options", []) if isinstance(option, dict)
                    ]
                    stop_labels = [label for label in labels if label.casefold().startswith("stop")]
                    continue_labels = [label for label in labels if label.casefold().startswith("continue")]
                    selected_stop = any(
                        normalized_answer == label.casefold()
                        or normalized_answer.endswith(": " + label.casefold())
                        for label in stop_labels
                    )
                    selected_continue = any(
                        normalized_answer == label.casefold()
                        or normalized_answer.endswith(": " + label.casefold())
                        for label in continue_labels
                    )
                    if selected_stop:
                        risk_rejected = True
                        task.update({
                            "state": "blocked",
                            "last_error": "The user rejected the risky proposal before project files changed.",
                            "pending_action": {}, "pending_transaction": {}, "lease_id": "",
                            "owner_pid": 0, "owner_token": "",
                            "provider_effect_state": "rejected_by_user",
                            "outcome_unknown": False,
                        })
                    elif selected_continue:
                        if not task.get("pending_action"):
                            raise HarnessError("The reviewed proposal is no longer available")
                        task["state"] = "pending_apply"
                        task["review_approved_effect_id"] = task.get("provider_effect_id", "")
                    else:
                        raise HarnessError("Choose Continue with checks or Stop this task from the decision card")
                else:
                    task["state"] = "ready"
                self._event(db, document, "interrupt_resolved", task_id=task["id"],
                            agent_id=item["agent_id"], payload={"interrupt_id": item["id"], "answer": item["answer"]})
            if risk_rejected:
                document["status"] = "paused"
                document["note"] = "The user rejected a risky proposal; no project file was changed."
            else:
                document["status"] = "queued"
                document["note"] = "The exact paused task has the user's answer and can continue."
        self._mutate(goal_id, change)

    def pause_deadlock(self, goal_id: str, reason: str) -> None:
        def change(document: dict[str, Any], db: sqlite3.Connection):
            if document["status"] in TERMINAL_GOALS | {
                "paused", "waiting_for_user", "cancelling",
            }:
                return
            document["status"] = "paused"
            document["note"] = _short(reason, 4_000)
            self._event(db, document, "goal_paused", payload={
                "reason": "dependency_deadlock", "detail": document["note"],
            })
        self._mutate(goal_id, change)

    def control(self, goal_id: str, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        cancellation_error: list[str] = []

        def change(document: dict[str, Any], db: sqlite3.Connection):
            if document["status"] == "waiting_for_project" and action != "cancel":
                raise HarnessError(
                    "This goal is waiting for the current project owner and cannot dispatch or change yet"
                )
            if document["status"] == "cancelling" and action != "cancel":
                raise HarnessError(
                    "This goal is draining cancellation and cannot accept other controls"
                )
            def publish_recovered_applied(task: dict[str, Any], reason: str) -> bool:
                pending = task.get("pending_transaction") or {}
                artifact = pending.get("artifact") if pending.get("state") == "applied" else None
                if not isinstance(artifact, dict) or not artifact.get("transaction_id"):
                    return False
                transaction_id = str(artifact["transaction_id"])
                if not any(str(one.get("transaction_id") or "") == transaction_id
                           for one in task.get("artifacts", [])):
                    held = _bounded_json(artifact, 100_000)
                    task["artifacts"].append(held)
                    document["artifacts"].append(_bounded_json({"task_id": task["id"], **artifact}, 100_000))
                    task["evidence"].append(
                        f"Recovered applied transaction {transaction_id} was preserved during {reason}."
                    )
                    self._event(db, document, "artifact_recovered", task_id=task["id"],
                                agent_id=task["assigned_agent_id"], payload={
                                    **artifact, "reconciliation": reason,
                                })
                task["pending_action"] = {}
                task["pending_transaction"] = {}
                task["provider_effect_state"] = "applied_recovered_and_published"
                return True

            def settle_for_cancellation(task: dict[str, Any]) -> None:
                pending = task.get("pending_transaction") or {}
                if not pending:
                    if task.get("pending_action") or _task_has_unsettled_effect(task):
                        task["pending_action"] = {}
                        task["outcome_unknown"] = False
                        task["reconciliation_required"] = False
                        task["provider_effect_state"] = "cancelled_before_project_file_effect"
                        self._event(
                            db, document, "provider_effect_cancelled_before_file_apply",
                            task_id=task["id"], agent_id=task["assigned_agent_id"],
                            payload={
                                "effect_id": task.get("provider_effect_id", ""),
                                "reason": "goal_cancelled",
                            },
                        )
                    return
                transaction_id = str(pending.get("transaction_id") or "")
                pending_state = str(pending.get("state") or "")
                if not transaction_id or pending_state not in {"prepared", "applied"}:
                    raise HarnessError(
                        f"{task.get('title') or task['id']} has an unrecognized file transaction state"
                    )
                root = Path(str(document["project"]["path"])).resolve(strict=True)
                transaction = FileTransaction(root)
                try:
                    manifest = transaction.load_manifest(transaction_id)
                except HarnessError:
                    backup_root = (root / ".harness" / "backups" / transaction_id).resolve()
                    expected_backups = (root / ".harness" / "backups").resolve()
                    baselines = (task.get("pending_action") or {}).get("_nexus_baselines")
                    paths = [str(one or "").replace("\\", "/").strip()
                             for one in pending.get("paths", []) if str(one or "").strip()]
                    no_manifest_was_created = (
                        expected_backups in backup_root.parents
                        and not backup_root.exists()
                    )
                    no_file_effect = bool(paths) and isinstance(baselines, dict) and all(
                        relative in baselines
                        and hmac.compare_digest(
                            str(baselines[relative]),
                            _path_baseline_marker(root, relative),
                        )
                        for relative in paths
                    )
                    if pending_state != "prepared" or not no_manifest_was_created or not no_file_effect:
                        raise
                    task["pending_action"] = {}
                    task["pending_transaction"] = {}
                    task["outcome_unknown"] = False
                    task["reconciliation_required"] = False
                    task["provider_effect_state"] = "cancelled_before_file_effect"
                    self._event(
                        db, document, "transaction_cancelled_before_prepare",
                        task_id=task["id"], agent_id=task["assigned_agent_id"],
                        payload={"transaction_id": transaction_id},
                    )
                    return

                manifest_state = str(manifest.get("state") or "")
                if manifest_state == "applied":
                    transaction.verify_applied(manifest)
                    artifact = {
                        "kind": "file_transaction", "transaction_id": transaction_id,
                        "changes": manifest.get("changes", []),
                        "patch": _short(manifest.get("patch"), 80_000),
                        "patch_sha256": manifest.get("patch_sha256", ""),
                    }
                    retained = pending.get("artifact")
                    if pending_state == "applied" and isinstance(retained, dict) \
                            and retained.get("transaction_id") != transaction_id:
                        raise HarnessError(
                            f"Applied transaction provenance does not match {transaction_id}"
                        )
                    pending["state"] = "applied"
                    pending["artifact"] = _bounded_json(artifact, 100_000)
                    if not publish_recovered_applied(task, "cancellation"):
                        raise HarnessError(
                            f"Applied transaction {transaction_id} could not be published"
                        )
                    return

                if pending_state == "applied":
                    raise HarnessError(
                        f"Applied transaction {transaction_id} no longer has an applied manifest"
                    )
                if manifest_state in {"prepared", "rolling_back"}:
                    transaction.rollback(transaction_id)
                    self._event(
                        db, document, "transaction_rolled_back_for_cancellation",
                        task_id=task["id"], agent_id=task["assigned_agent_id"],
                        payload={"transaction_id": transaction_id},
                    )
                elif manifest_state not in {"aborted", "rolled_back"}:
                    raise HarnessError(
                        f"Transaction {transaction_id} has unsupported state {manifest_state or 'missing'}"
                    )
                task["pending_action"] = {}
                task["pending_transaction"] = {}
                task["outcome_unknown"] = False
                task["reconciliation_required"] = False
                task["provider_effect_state"] = "file_effect_rolled_back_for_cancellation"

            if document["status"] in {"complete", "cancelled"} and action != "cancel":
                raise HarnessError("A terminal goal is immutable; fork it to continue with new work")
            if action in {"resume", "retry", "reassign", "steer", "message", "criteria", "request_review"} \
                    and any(one.get("state") == "pending" for one in document.get("interrupts", [])):
                raise HarnessError(
                    "Answer the pending decision before changing or continuing this goal"
                )
            if action == "pause":
                if document["status"] in TERMINAL_GOALS:
                    raise HarnessError("A terminal goal cannot be paused")
                document["status"] = "paused"
                document["note"] = "Paused by the user at the next safe boundary."
                self._event(db, document, "goal_paused", payload={"reason": "user"})
            elif action == "resume":
                if document["status"] == "waiting_for_user":
                    raise HarnessError("Answer the pending question before resuming")
                if document["status"] not in {"paused", "failed"}:
                    raise HarnessError("Only a paused or failed goal can resume")
                released_failed = document["status"] == "failed" \
                    and self._project_queue_state(document) == "released"
                if any(
                    one["state"] in {"blocked", "failed"} and _task_has_unsettled_effect(one)
                    for one in document["tasks"]
                ):
                    raise HarnessError(
                        "Reconcile or supersede pending provider/file effects before resuming this goal"
                    )
                for task in document["tasks"]:
                    if task["state"] in {"blocked", "failed"} and task.get("outcome_unknown") is not True:
                        task["state"] = "ready"
                        task["last_error"] = ""
                if released_failed:
                    blockers = self._shared_project_owners(
                        db,
                        Path(str(document["project"]["path"])),
                        str(document.get("project_authority_id") or ""),
                        except_goal_id=str(document["goal_id"]),
                    )
                    blockers.sort(key=lambda one: (
                        int(one.get("created_ms") or 0), str(one["goal_id"]),
                    ))
                    now = _now()
                    if blockers:
                        blocker_id = str(blockers[0]["goal_id"])
                        document["status"] = "waiting_for_project"
                        document["project_queue"] = self._queue_record(
                            "waiting", now,
                            blocked_by_goal_id=blocker_id, queued_ms=now,
                        )
                        document["note"] = (
                            "Resume is waiting for long-horizon goal "
                            + blocker_id[:8] + " to release this project."
                        )
                        self._event(db, document, "goal_resume_waiting_for_project", payload={
                            "blocked_by_goal_id": blocker_id,
                            "automatic_dispatch": False,
                        })
                        return
                    document["project_queue"] = self._queue_record(
                        "owner", now, auto_start_pending=True,
                    )
                    self._event(db, document, "goal_project_claimed_for_resume", payload={
                        "automatic_dispatch": False,
                        "execution_contract_fingerprint": document[
                            "execution_contract"
                        ]["fingerprint_sha256"],
                    })
                document["status"] = "queued"
                document["note"] = "Resumed from the saved state."
                self._event(db, document, "goal_resumed", payload={"budgets_preserved": True})
            elif action == "cancel":
                if document["status"] in {"complete", "cancelled"}:
                    return _NO_MUTATION
                released_failed = document["status"] == "failed" \
                    and self._project_queue_state(document) == "released"
                if released_failed and any(
                    _task_has_unsettled_effect(task) for task in document["tasks"]
                ):
                    blockers = self._shared_project_owners(
                        db,
                        Path(str(document["project"]["path"])),
                        str(document.get("project_authority_id") or ""),
                        except_goal_id=str(document["goal_id"]),
                    )
                    if blockers:
                        blockers.sort(key=lambda one: (
                            int(one.get("created_ms") or 0), str(one["goal_id"]),
                        ))
                        raise HarnessError(
                            "Cancellation must wait for long-horizon goal "
                            + str(blockers[0]["goal_id"])[:8]
                            + " to release this project before reconciling saved file effects"
                        )
                    now = _now()
                    document["project_queue"] = self._queue_record("owner", now)
                    released_failed = False
                    self._event(db, document, "goal_project_claimed_for_cancellation", payload={
                        "automatic_dispatch": False,
                        "execution_contract_fingerprint": document[
                            "execution_contract"
                        ]["fingerprint_sha256"],
                    })
                worker = document.get("worker") or {}
                worker_live = self._scheduler_live(document) \
                    and worker.get("kind") == "runtime"
                drain_complete = payload.get("drain_complete") is True
                exact_drainer = worker_live and (
                    int(worker.get("pid") or 0) == os.getpid()
                    and hmac.compare_digest(
                        str(worker.get("token") or ""), _process_token(os.getpid()),
                    )
                    and str(worker.get("worker_id") or "")
                    == str(payload.get("scheduler_id") or "")
                )
                if drain_complete and worker_live and not exact_drainer:
                    raise HarnessError(
                        "Only the durable scheduler lease holder can finish cancellation draining"
                    )
                if worker_live and not drain_complete:
                    cancellation = document.get("cancellation") or {}
                    if document.get("status") == "cancelling" \
                            and cancellation.get("state") == "draining":
                        return _NO_MUTATION
                    requested_ms = _now()
                    document["cancellation"] = {
                        "schema_version": CANCELLATION_SCHEMA_VERSION,
                        "state": "draining",
                        "requested_ms": requested_ms,
                        "settled_ms": 0,
                    }
                    document["status"] = "cancelling"
                    document["note"] = (
                        "Cancellation requested; Nexus is waiting for the in-flight "
                        "provider boundary to drain before releasing the project."
                    )
                    self._event(db, document, "goal_cancellation_requested", payload={
                        "worker_id": str(worker.get("worker_id") or ""),
                        "release_deferred": True,
                    })
                    return
                try:
                    for task in document["tasks"]:
                        settle_for_cancellation(task)
                except HarnessError as exc:
                    detail = _short(exc, 2_000)
                    document["status"] = "failed" if released_failed else "paused"
                    document["note"] = (
                        (
                            "Cancellation could not finish because a file effect could not be reconciled safely: "
                            if released_failed else
                            "Cancellation paused because a file effect could not be reconciled safely: "
                        )
                        + detail
                    )
                    document["cancellation"] = {
                        "schema_version": CANCELLATION_SCHEMA_VERSION,
                        "state": "none", "requested_ms": 0, "settled_ms": 0,
                    }
                    for held in document["tasks"]:
                        if held.get("pending_transaction") and held["state"] in {
                            "running", "pending_apply", "failed",
                        }:
                            held.update({
                                "state": "blocked", "last_error": document["note"],
                                "lease_id": "", "owner_pid": 0, "owner_token": "",
                            })
                    self._event(db, document, "goal_cancellation_blocked", payload={
                        "reason": "unsettled_file_effect", "detail": detail,
                    })
                    cancellation_error.append(document["note"])
                    return
                if any(_task_has_unsettled_effect(task) for task in document["tasks"]):
                    raise HarnessError(
                        "Cancellation could not settle every provider or file effect"
                    )
                document["status"] = "cancelled"
                requested_ms = int((document.get("cancellation") or {}).get(
                    "requested_ms"
                ) or _now())
                document["cancellation"] = {
                    "schema_version": CANCELLATION_SCHEMA_VERSION,
                    "state": "settled", "requested_ms": requested_ms,
                    "settled_ms": _now(),
                }
                document["worker"] = {
                    "schema_version": SCHEDULER_LEASE_SCHEMA_VERSION,
                    "pid": 0, "token": "", "worker_id": "", "kind": "runtime",
                    "acquired_ms": 0,
                }
                for task in document["tasks"]:
                    if task["state"] not in {"complete", "cancelled"}:
                        task["state"] = "cancelled"
                for item in document.get("interrupts", []):
                    if item.get("state") == "pending":
                        item.update({"state": "cancelled", "resolved_ms": _now(), "answer": ""})
                        self._event(db, document, "interrupt_cancelled",
                                    task_id=str(item.get("task_id") or ""),
                                    agent_id=str(item.get("agent_id") or ""),
                                    payload={"interrupt_id": item.get("id"), "reason": "goal_cancelled"})
                document["note"] = "Cancelled by the user; completed evidence remains recorded."
                self._event(db, document, "goal_cancelled", payload={})
            elif action == "retry":
                task_id = str(payload.get("task_id") or "")
                task = next((one for one in document["tasks"] if one["id"] == task_id), None)
                if task is None or task["state"] not in {"blocked", "failed"}:
                    raise HarnessError("Choose a blocked or failed task to retry")
                needs_reconciliation = bool(
                    task.get("outcome_unknown") is True
                    or task.get("reconciliation_required") is True
                )
                reconciled_effect = needs_reconciliation and payload.get("reconciled") is True
                if _task_has_unsettled_effect(task) and not reconciled_effect:
                    raise HarnessError(
                        "Reconcile or supersede the pending provider/file effect before retrying this task"
                    )
                if task.get("outcome_unknown") is True and payload.get("reconciled") is not True:
                    raise HarnessError("Reconcile the uncertain provider outcome before retrying")
                prior_effect_state = str(task.get("provider_effect_state") or "")
                recovered_transaction = False
                if reconciled_effect:
                    recovered_transaction = publish_recovered_applied(
                        task, "explicit retry reconciliation"
                    )
                    # Explicit reconciliation means the operator has inspected
                    # the old effect and chosen a fresh attempt. Never carry an
                    # acknowledged response or prepared/applied transaction
                    # into that attempt; doing so can apply or dispatch it twice.
                    task["pending_action"] = {}
                    task["pending_transaction"] = {}
                task.update({"state": "ready", "last_error": "", "lease_id": "", "owner_pid": 0,
                             "owner_token": "", "outcome_unknown": False,
                             "reconciliation_required": False,
                             "provider_effect_state": "reconciled_for_retry"})
                document["status"] = "queued"
                self._event(db, document, "task_retried", task_id=task_id,
                            agent_id=task["assigned_agent_id"], payload={
                                "explicit": True,
                                "reconciled": reconciled_effect,
                                "previous_effect_state": prior_effect_state,
                                "recovered_applied_transaction": recovered_transaction,
                            })
            elif action == "reassign":
                task_id = str(payload.get("task_id") or "")
                agent_id = str(payload.get("agent_id") or "")
                task = next((one for one in document["tasks"] if one["id"] == task_id), None)
                if task is None or task["state"] not in {"ready", "blocked", "failed", "waiting"}:
                    raise HarnessError(
                        "Only unfinished work without an acknowledged or in-flight result can be reassigned"
                    )
                if task.get("pending_action") or task.get("pending_transaction") \
                        or task.get("outcome_unknown") is True \
                        or task.get("reconciliation_required") is True \
                        or str(task.get("provider_effect_state") or "") in {
                            "dispatched", "acknowledged", "outcome_unknown", "context_step_acknowledged",
                            "reply_received", "reply_received_reconciliation_required",
                        }:
                    raise HarnessError(
                        "Reconcile the existing provider/file effect before reassigning this task"
                    )
                if agent_id not in {one["id"] for one in document["agents"]}:
                    raise HarnessError("That agent is not authorized for this project")
                required_contributor = str(task.get("required_contributor_id") or "")
                if required_contributor and agent_id != required_contributor:
                    raise HarnessError(
                        "This chat-bound task must remain with its required contributing agent"
                    )
                if task.get("kind") == "review" and task.get("review_of"):
                    parent = next(
                        (one for one in document["tasks"] if one["id"] == task["review_of"]), None
                    )
                    owner = next(
                        (one for one in document["agents"] if parent and one["id"] == parent["assigned_agent_id"]),
                        None,
                    )
                    replacement = next(one for one in document["agents"] if one["id"] == agent_id)
                    if parent is None or owner is None or agent_id == parent["assigned_agent_id"] \
                            or not _providers_independent(replacement, owner):
                        raise HarnessError(
                            "A review must remain assigned to a different provider identity than its author"
                        )
                previous = task["assigned_agent_id"]
                task["assigned_agent_id"] = agent_id
                if task["state"] in {"blocked", "failed"}:
                    task["state"] = "ready"
                if document["status"] not in {"paused", "waiting_for_user"}:
                    document["status"] = (
                        "running" if any(one["state"] == "running" for one in document["tasks"])
                        else "queued"
                    )
                self._event(db, document, "task_reassigned", task_id=task_id, agent_id=agent_id,
                            payload={"from_agent_id": previous, "to_agent_id": agent_id})
            elif action in {"steer", "message"}:
                words = _short(self.redactor.text(payload.get("text")), 20_000)
                if not words:
                    raise HarnessError("Write the steering instruction first")
                if action == "steer":
                    for candidate in document["tasks"]:
                        if publish_recovered_applied(candidate, "user steering"):
                            candidate["state"] = "complete"
                            candidate.update({"lease_id": "", "owner_pid": 0, "owner_token": ""})
                            continue
                        pending = candidate.get("pending_transaction") or {}
                        if pending.get("state") == "prepared" and pending.get("transaction_id"):
                            FileTransaction(Path(document["project"]["path"])).rollback(
                                str(pending["transaction_id"])
                            )
                            self._event(db, document, "transaction_superseded",
                                        task_id=candidate["id"],
                                        agent_id=candidate["assigned_agent_id"], payload={
                                            "transaction_id": pending["transaction_id"],
                                            "reason": "user_steering",
                                        })
                needs_steering_task = action == "steer" and not any(
                    one["state"] not in {"complete", "cancelled"} for one in document["tasks"]
                )
                if needs_steering_task and len(document["tasks"]) >= int(document["policy"]["max_tasks"]):
                    raise HarnessError("The bounded task budget leaves no room to implement this steering revision")
                document["objective_revisions"].append({
                    "revision": int(document["revision"]) + 1, "at_ms": _now(),
                    "text": words, "reason": action,
                })
                if action == "steer":
                    document["objective_epoch"] = int(document.get("objective_epoch") or 1) + 1
                    document["objective"] = (
                        str(document.get("original_objective") or document["objective"])
                        + "\n\nACTIVE USER STEERING\n"
                        + "\n".join(
                            str(one["text"]) for one in document["objective_revisions"]
                            if one.get("reason") == "steer"
                        )
                    )
                    review_parents = {
                        one["id"] for one in document["tasks"]
                        if one["state"] == "waiting_review" or one.get("pending_action")
                    }
                    for unfinished in document["tasks"]:
                        if unfinished.get("kind") == "review" \
                                and unfinished.get("review_of") in review_parents \
                                and unfinished["state"] not in {"complete", "cancelled"}:
                            unfinished.update({
                                "state": "cancelled", "pending_action": {}, "pending_transaction": {},
                                "lease_id": "", "owner_pid": 0, "owner_token": "",
                                "provider_effect_state": "superseded_by_steering",
                                "outcome_unknown": False,
                            })
                            self._event(
                                db, document, "review_superseded", task_id=unfinished["id"],
                                agent_id=unfinished["assigned_agent_id"],
                                payload={"reason": "objective_steered"}, run_id=goal_id,
                            )
                            continue
                        if unfinished["state"] in {
                            "running", "pending_apply", "waiting_review", "blocked", "failed"
                        }:
                            for step in unfinished.get("context_steps", []):
                                if step.get("state") != "complete":
                                    step["state"] = "superseded"
                            unfinished.update({
                                "state": "ready", "pending_action": {}, "pending_transaction": {},
                                "lease_id": "", "owner_pid": 0, "owner_token": "",
                                "provider_effect_state": "superseded_by_steering",
                                "outcome_unknown": False,
                            })
                            self._event(
                                db, document, "provider_result_superseded",
                                task_id=unfinished["id"], agent_id=unfinished["assigned_agent_id"],
                                payload={"reason": "user_steering_before_apply"}, run_id=goal_id,
                            )
                task_id = str(payload.get("task_id") or "")
                task = next((one for one in document["tasks"] if one["id"] == task_id), None)
                if task:
                    if action == "message" and task["state"] in {"blocked", "failed"} \
                            and _task_has_unsettled_effect(task):
                        raise HarnessError(
                            "Reconcile or supersede the pending provider/file effect before continuing this task"
                        )
                    task["evidence"].append("User steering: " + words)
                    if task["state"] in {"blocked", "failed", "waiting"}:
                        task["state"] = "ready"
                if needs_steering_task:
                    owner_id = task["assigned_agent_id"] if task else document["lead_agent_id"]
                    steering_id = _stable_id("steering", goal_id, document["revision"], words)
                    document["tasks"].append({
                        "id": steering_id, "title": _short(words.splitlines()[0], 240),
                        "description": "Implement the active user steering revision:\n" + words,
                        "kind": "steering", "state": "ready", "depends_on": [],
                        "parent_id": "", "review_of": "", "assigned_agent_id": owner_id,
                        "parallel_safe": False, "resource_paths": [], "attempts": 0,
                        "no_progress": 0, "lease_id": "", "owner_pid": 0, "owner_token": "",
                        "created_ms": _now(), "updated_ms": _now(), "summary": "",
                        "last_error": "", "evidence": ["User steering: " + words],
                        "artifacts": [], "criteria_evidence": [],
                        "provider_effect_state": "never_dispatched", "provider_effect_id": "",
                        "claim_objective_epoch": 0, "outcome_unknown": False,
                        "pending_action": {}, "pending_transaction": {},
                    })
                    document["budget"]["tasks_created"] += 1
                    self._event(db, document, "task_created", task_id=steering_id,
                                agent_id=owner_id, payload={"kind": "steering", "reason": "user_steering"})
                if document["status"] not in TERMINAL_GOALS:
                    document["status"] = (
                        "running" if any(one["state"] == "running" for one in document["tasks"])
                        else "queued"
                    )
                self._event(db, document, "goal_steered" if action == "steer" else "agent_messaged",
                            task_id=task_id, agent_id=str(payload.get("agent_id") or ""), payload={"text": words})
            elif action == "criteria":
                criteria = [
                    _short(self.redactor.text(one), 1_000)
                    for one in payload.get("success_criteria", []) if _short(one, 1_000)
                ]
                criteria = list(dict.fromkeys([
                    "Original objective is satisfied",
                    "Every required task is complete",
                    "Configured deterministic verification passes",
                    *criteria,
                ]))
                if len(criteria) > MAX_CRITERIA:
                    raise HarnessError(f"Use at most {MAX_CRITERIA} total success criteria")
                document["success_criteria"] = criteria
                document["objective_revisions"].append({
                    "revision": int(document["revision"]) + 1, "at_ms": _now(),
                    "text": "\n".join(criteria), "reason": "success_criteria",
                })
                self._event(db, document, "success_criteria_changed", payload={"success_criteria": criteria})
            elif action == "request_review":
                task_id = str(payload.get("task_id") or "")
                task = next((one for one in document["tasks"] if one["id"] == task_id), None)
                reviewer = str(payload.get("agent_id") or "")
                if task is None or task["state"] not in {"ready", "complete"}:
                    raise HarnessError("Choose ready or completed work with settled provenance to review")
                if task.get("kind") == "review" or task.get("review_of"):
                    raise HarnessError("A review task cannot create a review-of-review ritual")
                if task.get("pending_action") or task.get("pending_transaction") \
                        or task.get("outcome_unknown") is True \
                        or str(task.get("provider_effect_state") or "") in {
                            "dispatched", "acknowledged", "outcome_unknown", "context_step_acknowledged",
                        }:
                    raise HarnessError("Reconcile pending or uncertain provider/file effects before requesting review")
                if reviewer not in {one["id"] for one in document["agents"]} or reviewer == task["assigned_agent_id"]:
                    raise HarnessError("Choose a different authorized agent as reviewer")
                owner = next(one for one in document["agents"] if one["id"] == task["assigned_agent_id"])
                reviewing = next(one for one in document["agents"] if one["id"] == reviewer)
                if not _providers_independent(owner, reviewing):
                    raise HarnessError(
                        "Independent review requires a different effective provider identity, "
                        "not another alias for the same backend"
                    )
                if len(document["tasks"]) >= int(document["policy"]["max_tasks"]):
                    raise HarnessError("The bounded task budget leaves no room for a review task")
                review_id = _stable_id("review", goal_id, task_id, document["revision"], reviewer)
                if any(one["id"] == review_id for one in document["tasks"]):
                    raise HarnessError("That exact review is already present")
                review_paths = list(dict.fromkeys(
                    str(change.get("path") or "").replace("\\", "/").strip()
                    for artifact in task.get("artifacts", []) if isinstance(artifact, dict)
                    for change in artifact.get("changes", []) if isinstance(change, dict)
                    and str(change.get("path") or "").strip()
                ))
                review_packet_sha = hashlib.sha256(_canonical({
                    "task_id": task["id"], "summary": task.get("summary", ""),
                    "evidence": task.get("evidence", []), "artifacts": task.get("artifacts", []),
                    "verification": document.get("verification", {}),
                    "goal_revision": int(document["revision"]) + 1,
                }).encode("utf-8")).hexdigest()
                document["tasks"].append({
                    "id": review_id, "title": f"Review: {task['title']}",
                    "description": "Independently review the linked task, artifacts, and evidence against the goal.",
                    "kind": "review", "state": "ready", "depends_on": [],
                    "parent_id": task_id, "review_of": task_id,
                    "review_packet_sha256": review_packet_sha,
                    "review_required_paths": review_paths, "review_paths_inspected": [],
                    "assigned_agent_id": reviewer,
                    "parallel_safe": True, "resource_paths": [], "attempts": 0, "no_progress": 0,
                    "lease_id": "", "owner_pid": 0, "owner_token": "", "created_ms": _now(),
                    "updated_ms": _now(), "summary": "", "last_error": "", "evidence": [], "artifacts": [],
                    "criteria_evidence": [],
                    "provider_effect_state": "never_dispatched", "outcome_unknown": False,
                    "provider_effect_id": "",
                    "pending_action": {}, "pending_transaction": {},
                })
                document["budget"]["tasks_created"] += 1
                task["review_return_state"] = task["state"]
                task["state"] = "waiting_review"
                document["status"] = (
                    "running" if any(one["state"] == "running" for one in document["tasks"])
                    else "queued"
                )
                self._event(db, document, "review_requested", task_id=review_id, agent_id=reviewer,
                            payload={"review_of": task_id, "requested_by": "user",
                                     "review_packet_sha256": review_packet_sha})
            else:
                raise HarnessError("That long-horizon control is not recognized")
        changed = self._mutate(goal_id, change)[0]
        if cancellation_error:
            raise HarnessError(cancellation_error[0])
        return self.public(changed)

    def complete_verification(
        self, goal_id: str, result: dict[str, Any], *,
        expected_revision: int | None = None, expected_objective_epoch: int | None = None,
    ) -> dict[str, Any]:
        def change(document: dict[str, Any], db: sqlite3.Connection):
            if document["status"] in TERMINAL_GOALS | {"cancelling"}:
                return
            if (expected_revision is not None and int(document["revision"]) != int(expected_revision)) \
                    or (expected_objective_epoch is not None and int(document.get("objective_epoch") or 1)
                        != int(expected_objective_epoch)):
                document["verification"] = {
                    "status": "superseded",
                    "reason": "The goal changed while verification was running; this result was not used.",
                    "commands": [],
                }
                if document["status"] not in {"paused", "waiting_for_user", "cancelling"}:
                    document["status"] = (
                        "running" if any(one["state"] == "running" for one in document["tasks"])
                        else "queued"
                    )
                self._event(db, document, "verification_superseded", payload={
                    "expected_revision": expected_revision,
                    "current_revision": document["revision"],
                    "expected_objective_epoch": expected_objective_epoch,
                    "current_objective_epoch": document.get("objective_epoch", 1),
                })
                return
            checked = copy.deepcopy(result)
            known_refs: set[str] = set()
            for artifact in document.get("artifacts", []):
                if artifact.get("transaction_id"):
                    known_refs.add("artifact:" + str(artifact["transaction_id"]))
                for changed in artifact.get("changes", []):
                    if isinstance(changed, dict) and changed.get("path"):
                        known_refs.add("file:" + str(changed["path"]))
                if artifact.get("tree_merkle"):
                    known_refs.add("snapshot:" + str(artifact["tree_merkle"]))
            for task in document["tasks"]:
                if task.get("kind") == "review" and task.get("state") == "complete":
                    known_refs.add("review:" + str(task["id"]))
            criteria_results = []
            for criterion in document["success_criteria"]:
                if criterion == "Every required task is complete":
                    passed = all(one["state"] in {"complete", "cancelled"} for one in document["tasks"])
                    refs = ["task-ledger"]
                elif criterion == "Configured deterministic verification passes":
                    passed = result.get("status") == "passed"
                    refs = ["verification"] if passed else []
                else:
                    declared = [
                        ref for task in document["tasks"] for mapping in task.get("criteria_evidence", [])
                        if mapping.get("criterion") == criterion
                        for ref in mapping.get("evidence_refs", [])
                    ]
                    refs = [ref for ref in declared if ref in known_refs]
                    passed = result.get("status") == "passed" and bool(refs)
                criteria_results.append({
                    "criterion": criterion, "status": "passed" if passed else "failed",
                    "evidence_refs": refs,
                    "basis": "Authenticated task/artifact evidence plus deterministic verification",
                })
            checked["criteria_results"] = criteria_results
            if result.get("status") == "passed" and not all(one["status"] == "passed" for one in criteria_results):
                checked["status"] = "failed"
                missing = [one["criterion"] for one in criteria_results if one["status"] != "passed"]
                checked["reason"] = "Success criteria lack authenticated evidence: " + "; ".join(missing)
            document["verification"] = _durable_evidence(checked)
            self._event(db, document, "test_result", payload=checked)
            if checked.get("status") == "passed":
                document["status"] = "complete"
                document["note"] = "All required tasks and deterministic verification are complete."
                self._event(db, document, "goal_completed", payload={"basis": result.get("basis"), "success_criteria": document["success_criteria"]})
                return
            reason = _short(checked.get("reason") or "Deterministic verification failed", 4_000)
            prior = [one for one in document["tasks"] if one.get("kind") == "repair" and one.get("last_error") == reason]
            if len(prior) >= MAX_NO_PROGRESS:
                document["status"] = "paused"
                document["note"] = reason
                self._event(db, document, "goal_paused", payload={"reason": "repeated_verification_failure", "detail": reason})
                return
            if len(document["tasks"]) >= int(document["policy"]["max_tasks"]):
                document["status"] = "paused"
                document["note"] = "Verification failed, but the bounded task budget is exhausted: " + reason
                self._event(db, document, "goal_paused", payload={"reason": "task_budget", "detail": reason})
                return
            task_id = _stable_id("repair", goal_id, document["revision"], reason)
            document["tasks"].append({
                "id": task_id, "title": "Repair failed verification", "description": reason,
                "kind": "repair", "state": "ready", "depends_on": [], "parent_id": "",
                "review_of": "", "assigned_agent_id": document["lead_agent_id"],
                "parallel_safe": False, "resource_paths": [], "attempts": 0, "no_progress": 0,
                "lease_id": "", "owner_pid": 0, "owner_token": "", "created_ms": _now(),
                "updated_ms": _now(), "summary": "", "last_error": reason,
                "evidence": ["Deterministic verification failure: " + reason], "artifacts": [],
                "criteria_evidence": [],
                "provider_effect_state": "never_dispatched", "outcome_unknown": False,
                "provider_effect_id": "",
                "pending_action": {}, "pending_transaction": {},
            })
            document["budget"]["tasks_created"] += 1
            document["status"] = "queued"
            document["note"] = "Verification created one concrete repair task."
            self._event(db, document, "task_created", task_id=task_id,
                        agent_id=document["lead_agent_id"], payload={"kind": "repair", "verification_failure": reason})
        return self.public(self._mutate(goal_id, change)[0])

    def recover_dead(self, goal_id: str) -> dict[str, Any]:
        def change(document: dict[str, Any], db: sqlite3.Connection):
            worker = document.get("worker", {})
            was_cancelling = document.get("status") == "cancelling" \
                and (document.get("cancellation") or {}).get("state") == "draining"
            has_running_lease = any(one["state"] == "running" for one in document["tasks"])
            if not has_running_lease or _owner_is_alive(
                int(worker.get("pid") or 0), str(worker.get("token") or "")
            ):
                return
            for task in document["tasks"]:
                if task["state"] == "running":
                    effect = str(task.get("provider_effect_state") or "")
                    if effect == "context_step_acknowledged" and task.get("context_steps"):
                        task["state"] = "ready"
                        task["last_error"] = ""
                        task["outcome_unknown"] = False
                        task.update({"lease_id": "", "owner_pid": 0, "owner_token": ""})
                    elif effect == "reply_received":
                        task["state"] = "blocked"
                        task["outcome_unknown"] = False
                        task["reconciliation_required"] = True
                        task["provider_effect_state"] = "reply_received_reconciliation_required"
                        task["last_error"] = (
                            "A provider reply was received before restart, but local parsing/repair "
                            "did not finish. Inspect the provider transcript and explicitly reconcile "
                            "before retrying; Nexus will not resend automatically."
                        )
                        task.update({"lease_id": "", "owner_pid": 0, "owner_token": ""})
                    elif effect == "acknowledged" and task.get("pending_action"):
                        pending = task.get("pending_transaction") or {}
                        if pending.get("state") == "prepared":
                            transaction = FileTransaction(Path(document["project"]["path"]))
                            try:
                                manifest = transaction.load_manifest(str(pending.get("transaction_id") or ""))
                            except HarnessError:
                                # The goal intent committed before FileTransaction
                                # created a manifest. No project mutation began.
                                task["pending_transaction"] = {}
                            else:
                                if manifest.get("state") == "applied":
                                    pending["state"] = "applied"
                                    pending["artifact"] = {
                                        "kind": "file_transaction",
                                        "transaction_id": pending["transaction_id"],
                                        "changes": manifest.get("changes", []),
                                        "patch": _short(manifest.get("patch"), 80_000),
                                        "patch_sha256": manifest.get("patch_sha256", ""),
                                    }
                                    try:
                                        transaction.verify_applied(manifest)
                                    except HarnessError as exc:
                                        task["state"] = "blocked"
                                        task["outcome_unknown"] = True
                                        task["last_error"] = (
                                            "An applied crash-recovery transaction was changed afterward and "
                                            "needs manual reconciliation: " + _short(exc, 2_000)
                                        )
                                elif manifest.get("state") == "prepared":
                                    # Whether no replacement happened or all
                                    # replacements happened, rollback reconciles
                                    # to the before boundary. Resume reapplies the
                                    # acknowledged action under a fresh ID.
                                    try:
                                        transaction.rollback(str(pending["transaction_id"]))
                                    except HarnessError as exc:
                                        task["state"] = "blocked"
                                        task["last_error"] = "Interrupted file transaction needs manual recovery: " + _short(exc, 2_000)
                                        task["outcome_unknown"] = True
                                    else:
                                        task["pending_transaction"] = {}
                                else:
                                    task["pending_transaction"] = {}
                        if task["state"] != "blocked":
                            task["state"] = "pending_apply"
                            task["last_error"] = ""
                    else:
                        task["state"] = "blocked"
                        task["outcome_unknown"] = effect == "dispatched"
                        task["provider_effect_state"] = "outcome_unknown" if effect == "dispatched" else effect
                        task["last_error"] = (
                            "Provider outcome is unknown after restart; explicitly reconcile before retrying."
                            if task["outcome_unknown"] else
                            "Nexus restarted before the provider effect began; retry is safe."
                        )
                        task.update({"lease_id": "", "owner_pid": 0, "owner_token": ""})
            document["worker"] = {
                "schema_version": SCHEDULER_LEASE_SCHEMA_VERSION,
                "pid": 0, "token": "", "worker_id": "", "kind": "runtime",
                "acquired_ms": 0,
            }
            if was_cancelling:
                document["status"] = "cancelling"
                document["note"] = (
                    "Recovered the cancelled scheduler after restart; Nexus will settle "
                    "the drained boundary without resending provider work."
                )
                self._event(db, document, "goal_cancellation_drain_recovered", payload={
                    "automatic_retry": False,
                })
            else:
                document["status"] = "paused"
                document["note"] = "Recovered exact goal state after restart; uncertain effects were not resent."
                self._event(db, document, "goal_recovered", payload={"automatic_retry": False})
        return self.public(self._mutate(goal_id, change)[0])

    def recover_orphaned_queue(self, goal_id: str) -> dict[str, Any]:
        """Make a committed between-node queue state explicitly resumable after restart."""
        def change(document: dict[str, Any], db: sqlite3.Connection):
            worker = document.get("worker", {})
            if document["status"] != "queued" or _owner_is_alive(
                int(worker.get("pid") or 0), str(worker.get("token") or "")
            ):
                return
            if any(one["state"] == "running" for one in document["tasks"]):
                raise HarnessError("A queued goal with an active lease must use provider-effect recovery")
            if (document.get("project_queue") or {}).get("auto_start_pending") is True \
                    and self._pristine_for_queue_migration(document):
                document["worker"] = {
                    "schema_version": SCHEDULER_LEASE_SCHEMA_VERSION,
                    "pid": 0, "token": "", "worker_id": "", "kind": "runtime",
                    "acquired_ms": 0,
                }
                document["note"] = (
                    "Recovered a pristine committed start before any provider dispatch."
                )
                self._event(db, document, "goal_pristine_auto_start_recovered", payload={
                    "automatic_retry": True, "provider_dispatched": False,
                })
                return
            document["status"] = "paused"
            document["note"] = (
                "Recovered a committed scheduling boundary after restart. Resume to continue; "
                "Nexus did not repeat a provider or file effect."
            )
            document["worker"] = {
                "schema_version": SCHEDULER_LEASE_SCHEMA_VERSION,
                "pid": 0, "token": "", "worker_id": "", "kind": "runtime",
                "acquired_ms": 0,
            }
            self._event(db, document, "goal_recovered", payload={
                "automatic_retry": False, "boundary": "queued_between_nodes",
            })
        return self.public(self._mutate(goal_id, change)[0])

    def fail_pending_apply(self, goal_id: str, error: str) -> dict[str, Any]:
        def change(document: dict[str, Any], db: sqlite3.Connection):
            changed = False
            for task in document["tasks"]:
                if task["state"] in {"running", "pending_apply"} and task.get("pending_action"):
                    task["state"] = "blocked"
                    task["last_error"] = _short(error, 4_000)
                    task.update({"lease_id": "", "owner_pid": 0, "owner_token": ""})
                    self._event(db, document, "task_failed", task_id=task["id"],
                                agent_id=task["assigned_agent_id"], payload={"phase": "apply", "error": task["last_error"]})
                    changed = True
            if changed:
                if document.get("status") != "cancelling":
                    document["status"] = "paused"
                    document["note"] = (
                        "A structured result could not be safely applied: "
                        + _short(error, 2_000)
                    )
        return self.public(self._mutate(goal_id, change)[0])


class LongHorizonRuntime:
    """LangGraph scheduler over the authenticated goal/task/event store."""

    def __init__(
        self, config: LoadedConfig, *,
        external_project_conflicts: Callable[[Path], list[str]] | None = None,
    ) -> None:
        self.config = config
        self.store = GoalStore(config)
        self.redactor = CredentialRedactor(config)
        self._checkpoint_context = SqliteSaver.from_conn_string(str(self.store.checkpoints))
        self.checkpointer = self._checkpoint_context.__enter__()
        self.graph = self._build_graph()
        self.lock = threading.RLock()
        self.workers: dict[str, threading.Thread] = {}
        self.scheduler_ids: dict[str, str] = {}
        self._auto_start_attempted: dict[str, float] = {}
        self._auto_start_cursor: tuple[int, str] = (0, "")
        self.external_project_conflicts = external_project_conflicts
        self._auto_start_enabled = False
        self._watcher_stop = threading.Event()
        self._watcher_wake = threading.Event()
        self._watcher = threading.Thread(
            target=self._auto_start_watch,
            name="nexus-long-horizon-auto-start",
            daemon=True,
        )
        self._watcher.start()

    def _require_no_external_owner(self, root: Path) -> None:
        conflicts = self.external_project_conflicts(root) if self.external_project_conflicts else []
        if conflicts:
            raise HarnessError(
                "Legacy project work already owns this project. Cancel or finish it before starting long-horizon work."
            )

    def _require_agent_setup(self, goal: dict[str, Any]) -> None:
        setup = self.store.provider_setup_status(goal)
        if setup.get("changed"):
            raise HarnessError(str(setup.get("message") or (
                "The saved provider setup changed. Start a new goal with the current board setup."
            )))

    @staticmethod
    def _require_goal_authority(goal: dict[str, Any]) -> str:
        root = Path(str(goal.get("project", {}).get("path") or "")).resolve(strict=True)
        status = inspect_project_authority(root)
        if not status.get("can_run"):
            raise HarnessError(str(status.get("reason") or "Project execution is paused."))
        actual = project_identity(root)
        expected = str(goal.get("project_authority_id") or "")
        if not expected:
            raise HarnessError(
                "This saved goal predates target-folder authority binding and cannot resume safely. "
                "Fork or recreate it after explicitly selecting the current project folder."
            )
        if not hmac.compare_digest(expected, actual):
            raise HarnessError(
                "The selected project's execution authority changed after this goal was admitted."
            )
        return actual

    def close(self) -> None:
        self._watcher_stop.set()
        self._watcher_wake.set()
        if self._watcher.is_alive() and threading.current_thread() is not self._watcher:
            self._watcher.join(timeout=2.0)
        deadline = time.monotonic() + 5.0
        with self.lock:
            workers = list(self.workers.values())
        for worker in workers:
            if worker is threading.current_thread() or not worker.is_alive():
                continue
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
        with self.lock:
            workers_alive = any(one.is_alive() for one in self.workers.values())
        if workers_alive:
            # A provider may be legitimately blocked outside our process. Do
            # not close LangGraph's SQLite handle from underneath its thread.
            return
        try:
            self._checkpoint_context.__exit__(None, None, None)
        except Exception:
            pass

    def _enable_auto_start_watcher(self) -> None:
        if not self._auto_start_enabled:
            self._auto_start_enabled = True
            self._watcher_wake.set()

    def _auto_start_watch(self) -> None:
        while not self._watcher_stop.is_set():
            self._watcher_wake.wait(timeout=0.25)
            self._watcher_wake.clear()
            if self._watcher_stop.is_set() or not self._auto_start_enabled:
                continue
            try:
                goals, cursor = self.store.auto_startable_authority_page(
                    *self._auto_start_cursor,
                )
                self._auto_start_cursor = cursor
            except Exception:
                continue
            for goal in goals:
                if self._watcher_stop.is_set():
                    break
                try:
                    with self.lock:
                        recent = self._auto_start_attempted.get(str(goal["goal_id"]), 0.0)
                    if time.monotonic() - recent < 60.0:
                        continue
                    self.start_background(str(goal["goal_id"]))
                except Exception:
                    continue

    def _build_graph(self):
        graph = StateGraph(GoalGraphState)
        graph.add_node("schedule", self._schedule_node)
        graph.add_node("act", self._act_node)
        graph.add_node("apply", self._apply_node)
        graph.add_node("human", self._human_node)
        graph.add_node("verify", self._verify_node)
        graph.add_edge(START, "schedule")
        graph.add_conditional_edges("schedule", lambda state: state["route"], {
            "act": "act", "apply": "apply", "verify": "verify", "end": END,
        })
        graph.add_edge("act", "apply")
        graph.add_conditional_edges("apply", lambda state: state["route"], {
            "human": "human", "schedule": "schedule", "end": END,
        })
        graph.add_edge("human", "schedule")
        graph.add_conditional_edges("verify", lambda state: state["route"], {
            "schedule": "schedule", "end": END,
        })
        return graph.compile(checkpointer=self.checkpointer)

    def _schedule_node(self, state: GoalGraphState) -> GoalGraphState:
        goal = self.store.get(state["goal_id"])
        if goal["status"] in TERMINAL_GOALS or goal["status"] in {
            "paused", "waiting_for_user", "waiting_for_project", "cancelling",
        }:
            return {"task_ids": [], "route": "end"}
        pending = [one["id"] for one in goal["tasks"] if one["state"] == "pending_apply" and one.get("pending_action")]
        if pending:
            return {"task_ids": pending, "actions": [], "route": "apply"}
        with self.lock:
            scheduler_id = self.scheduler_ids.get(goal["goal_id"], "")
        tasks = self.store.claim_ready(
            goal["goal_id"], scheduler_id or uuid.uuid4().hex,
        )
        if tasks:
            return {"task_ids": [one["id"] for one in tasks], "route": "act"}
        goal = self.store.get(goal["goal_id"])
        if all(one["state"] in {"complete", "cancelled"} for one in goal["tasks"]):
            return {"route": "verify", "task_ids": []}
        if not any(one["state"] in {"ready", "running", "pending_apply"} for one in goal["tasks"]):
            waiting = [
                one for one in goal["tasks"]
                if one["state"] in {"waiting", "waiting_review", "blocked", "failed"}
            ]
            detail = "; ".join(
                f"{one['title']}: {one.get('last_error') or one['state']}" for one in waiting[:8]
            )
            self.store.pause_deadlock(
                goal["goal_id"],
                "No runnable task remains in the dependency graph"
                + ((": " + detail) if detail else "."),
            )
        return {"route": "end", "task_ids": []}

    def _agent_context(self, goal: dict[str, Any], task: dict[str, Any], extra_files: list[str] | None = None) -> str:
        root = Path(goal["project"]["path"])
        ledger = [{"id": one["id"], "title": one["title"], "state": one["state"],
                   "owner": one["assigned_agent_id"], "depends_on": one["depends_on"],
                   "summary": _short(one.get("summary"), 1_000)} for one in goal["tasks"]]
        files = swarm_work._file_snapshot(root, list(extra_files or [])) if extra_files else "No additional file contents requested yet."
        review_packet = ""
        if task.get("kind") == "review" and task.get("review_of"):
            target = next((
                one for one in goal["tasks"] if one["id"] == task["review_of"]
            ), None)
            if target:
                proposed = target.get("pending_action") or {}
                packet = {
                    "review_packet_sha256": task.get("review_packet_sha256", ""),
                    "target": {
                        "id": target["id"], "title": target["title"],
                        "description": _short(target.get("description"), 4_000),
                        "summary": _short(proposed.get("summary") or target.get("summary"), 4_000),
                        "provider_effect_id": target.get("provider_effect_id", ""),
                    },
                    "proposed_action": {
                        "risk": proposed.get("risk"),
                        "observed_baselines": proposed.get("_nexus_baselines", {}),
                        "changes": [
                            {
                                "path": one.get("path"), "delete": one.get("delete") is True,
                                "reason": _short(one.get("reason"), 1_000),
                                "content_preview": _short(one.get("content"), 4_000),
                                "content_characters": len(str(one.get("content") or "")),
                                "content_sha256": hashlib.sha256(
                                    str(one.get("content") or "").encode("utf-8")
                                ).hexdigest(),
                            }
                            for one in proposed.get("changes", []) if isinstance(one, dict)
                        ],
                    },
                    "target_evidence": target.get("evidence", [])[-24:],
                    "target_artifacts": target.get("artifacts", [])[-12:],
                    "verification": goal.get("verification", {}),
                }
                review_packet = (
                    "\n\nTARGETED REVIEW PACKET\n"
                    + _canonical(_durable_evidence(packet, string_limit=4_000, list_limit=100))
                    + "\nReturn review_verdict=approve only when justified, include concrete review_findings, "
                      "and include the exact review-packet:<sha256> token in evidence. Use reject or "
                      "changes_requested with the same proof requirements when the proposal is unsafe. "
                      "Every proposed path is listed. Use read_proposed_change with path/offset/limit to "
                      "inspect any content that is longer than its preview before returning a verdict."
                )
        return (
            "LONG-HORIZON GOAL\n" + goal["objective"]
            + "\n\nSUCCESS CRITERIA\n- " + "\n- ".join(goal["success_criteria"])
            + "\n\nCURRENT CONCRETE TASK\n" + task["description"]
            + "\n\nSHARED TASK LEDGER\n" + json.dumps(ledger, ensure_ascii=False)
            + "\n\nPROJECT TREE\n" + swarm_work._tree(root)
            + "\n\nREQUESTED FILE CONTENTS\n" + files
            + "\n\nUSER STEERING / EVIDENCE\n" + "\n".join(task.get("evidence", [])[-12:])
            + review_packet
            + "\n\nCOMPLETION EVIDENCE\nFor every success criterion this task supports, return criteria_evidence using the exact criterion text and refs such as artifact:<transaction-id>, file:<relative-path>, or review:<task-id>. For genuinely read-only work, use the exact reserved ref verified-no-change; Nexus will bind that declaration to the authenticated snapshot it records after your response. Generic claims or a generic test pass do not prove a custom criterion. Nexus records file transactions or a no-change tree observation itself and still runs deterministic project verification before completing the goal."
            + "\n\nChoose only the next useful action. Work alone when you can. Delegate only a concrete bounded subtask that another authorized agent can do independently. "
              "Request review only for meaningful risk, broad changes, failed checks, or when you need it. Ask the user only for genuine ambiguity, new authority, risky/irreversible action, missing access, or an unresolved blocker. "
              "Do not hold meetings, restate the plan, or ask another agent merely because it exists."
        )

    def _execute_one(self, goal_id: str, task_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        goal = self.store.get(goal_id)
        self._require_agent_setup(goal)
        task = next(one for one in goal["tasks"] if one["id"] == task_id)
        agent = next(one for one in goal["agents"] if one["id"] == task["assigned_agent_id"])
        root = Path(goal["project"]["path"])
        conversation_key = f"long-goal-{goal_id}-{task_id}"
        dispatched = False
        provider_attempt_started = False
        dispatch_admission_failed = False
        effect_acknowledged = False
        context_tools = None
        tool_results: list[dict[str, Any]] = []
        requested_files: list[str] = []

        def account_dispatch(prefix: str, request_text: str, request_context: str):
            def before_dispatch(phase: str) -> None:
                nonlocal dispatched, dispatch_admission_failed
                # Re-evaluate after context preparation and provider creation,
                # immediately before the physical send admission. A PATH or
                # binary swap between resume and this boundary must not inherit
                # a long-running goal's authority.
                digest = hashlib.sha256(
                    (prefix + "\0" + phase + "\0" + request_text + "\0" + request_context).encode("utf-8")
                ).hexdigest()
                event_phase = prefix if phase == "initial" else f"{prefix}_{phase}"
                try:
                    self._require_agent_setup(self.store.get(goal_id))
                    self.store.record_dispatch(goal_id, task, digest, phase=event_phase)
                except Exception:
                    dispatch_admission_failed = True
                    raise
                dispatched = True
            return before_dispatch

        def account_reply(prefix: str):
            def after_response(phase: str) -> None:
                event_phase = prefix if phase == "initial" else f"{prefix}_{phase}"
                self.store.record_provider_reply(
                    goal_id, task, phase=event_phase,
                )
            return after_response

        def ask_action(request_text: str, request_context: str, phase: str) -> dict[str, Any]:
            nonlocal effect_acknowledged, provider_attempt_started
            effect_acknowledged = False
            # This boundary starts before route/provider resolution inside
            # chat.ask_once. A known unavailable/misconfigured provider can
            # therefore fail over even when no physical dispatch occurred,
            # while earlier local file/context failures remain non-provider.
            provider_attempt_started = True
            answer = chat_lab.ask_once(
                self.config, agent["who"], request_text, context=request_context,
                provider_attachments=provider_attachments,
                response_format=AGENT_ACTION_FORMAT,
                conversation_key=conversation_key,
                before_provider_dispatch=account_dispatch(phase, request_text, request_context),
                after_provider_response=account_reply(phase),
            )
            try:
                return swarm_work._decode(answer, agent["name"], AGENT_ACTION_FORMAT)
            except HarnessError:
                if not str(agent.get("who") or "").startswith("web:"):
                    raise
                correction_prompt = (
                    "Correct your immediately preceding delivered answer into the required JSON schema. "
                    "Return only one fenced JSON object, preserve the same substantive answer, and do not redo the task."
                )
                correction_context = (
                    "FORMAT CORRECTION ONLY\nThe prior reply was delivered but was not valid for the "
                    "required Nexus action schema. Correct it once without adding commentary."
                )
                corrected = chat_lab.ask_once(
                    self.config, agent["who"], correction_prompt,
                    context=correction_context, provider_attachments=provider_attachments,
                    response_format=AGENT_ACTION_FORMAT,
                    conversation_key=conversation_key,
                    prefer_existing_conversation=True,
                    before_provider_dispatch=account_dispatch(
                        f"{phase}_format_repair", correction_prompt, correction_context,
                    ),
                    after_provider_response=account_reply(
                        f"{phase}_format_repair"
                    ),
                )
                return swarm_work._decode(corrected, agent["name"], AGENT_ACTION_FORMAT)

        try:
            provider_attachments = []
            for descriptor in goal.get("input_provider_attachments", []):
                path = Path(str(descriptor.get("path") or ""))
                if path.is_file():
                    provider_attachments.append({
                        **descriptor, "data": base64.b64encode(path.read_bytes()).decode("ascii"),
                    })
            baseline_manifest = _project_baseline_manifest(root)
            phase = "initial"

            def continuation_route() -> str:
                current_goal = self.store.get(goal_id)
                current_task = next(one for one in current_goal["tasks"] if one["id"] == task_id)
                if current_goal["status"] == "cancelled" \
                        or int(current_task.get("claim_objective_epoch") or 0) \
                        != int(current_goal.get("objective_epoch") or 1) \
                        or current_task.get("lease_id") != task.get("lease_id"):
                    return "superseded"
                if current_goal["status"] in {
                    "paused", "waiting_for_user", "cancelling",
                }:
                    self.store.defer_context_continuation(goal_id, task)
                    return "deferred"
                return "continue"

            def ensure_project_tools():
                nonlocal context_tools
                if context_tools is None:
                    ledger = swarm_work.CollaborationLedger(
                        self.config, str(agent.get("who") or ""),
                        f"long-horizon-{goal_id}-{task_id}-{task.get('attempts', 0)}",
                        session_id=_stable_id("lh-tools", goal_id, task_id, task.get("attempts", 0)),
                    ).begin(goal["objective"], [agent], mode="long_horizon_context_tools")
                    context_tools = swarm_work._ProjectContextTools(
                        self.config, root, ledger,
                        {**goal["project"], "tasks": [goal["objective"]]},
                        goal["objective"], [], None,
                    )
                return context_tools

            def run_context_calls(calls: list[dict[str, Any]], completed_ids: set[str]) -> bool:
                for call in calls:
                    if not isinstance(call, dict):
                        raise HarnessError("A context-tool call is malformed")
                    call_id = str(call.get("call_id") or "")
                    if call_id in completed_ids:
                        continue
                    if continuation_route() != "continue":
                        return False
                    self.store.reserve_context_tool(goal_id, task, call)
                    try:
                        if str(call.get("name") or "") == "read_proposed_change":
                            if task.get("kind") != "review" or not task.get("review_of"):
                                raise HarnessError(
                                    "read_proposed_change is available only to a targeted review task"
                                )
                            arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
                            relative = str(arguments.get("path") or "").replace("\\", "/").strip()
                            offset = int(arguments.get("offset") or 0)
                            limit = min(20_000, max(1, int(arguments.get("limit") or 20_000)))
                            if offset < 0:
                                raise HarnessError("A proposed-change offset cannot be negative")
                            latest = self.store.get(goal_id)
                            parent = next(
                                one for one in latest["tasks"] if one["id"] == task["review_of"]
                            )
                            proposed = next((
                                one for one in (parent.get("pending_action") or {}).get("changes", [])
                                if isinstance(one, dict)
                                and str(one.get("path") or "").replace("\\", "/").strip() == relative
                            ), None)
                            if proposed is None:
                                raise HarnessError("That path is not in the exact proposed review packet")
                            content = str(proposed.get("content") or "")
                            result = {
                                "path": relative, "delete": proposed.get("delete") is True,
                                "reason": _short(proposed.get("reason"), 1_000),
                                "offset": offset, "content": content[offset:offset + limit],
                                "total_characters": len(content),
                                "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                                "has_more": offset + limit < len(content),
                            }
                        else:
                            result = ensure_project_tools().execute(task["assigned_agent_id"], call)
                    except Exception as exc:
                        self.store.record_context_tool_result(goal_id, task, call, error=str(exc))
                        raise
                    self.store.record_context_tool_result(goal_id, task, call, result)
                    tool_results.append({
                        "call_id": call.get("call_id"), "name": call.get("name"),
                        "result": result,
                    })
                return True

            for prior_step in task.get("context_steps", []):
                requested_files.extend(
                    one for one in prior_step.get("requested_files", []) if one not in requested_files
                )
                for held in prior_step.get("results", []):
                    tool_results.append({
                        "call_id": held.get("call_id"), "name": held.get("name"),
                        "result": held.get("result"), "error": held.get("error"),
                    })
            pending_step = next((
                one for one in reversed(task.get("context_steps", []))
                if one.get("state") == "tools_pending"
            ), None)
            if pending_step:
                completed_ids = {
                    str(one.get("call_id") or "") for one in pending_step.get("results", [])
                }
                if not run_context_calls(list(pending_step.get("calls") or []), completed_ids):
                    return task, {"action": "deferred", "summary": "Paused at a context-tool boundary", "changes": []}
                effect_acknowledged = True
                phase = "context_tools_resume"

            while True:
                boundary = continuation_route()
                if boundary != "continue":
                    return task, {"action": boundary, "summary": "Stopped at a user-control boundary", "changes": []}
                context = self._agent_context(goal, task, requested_files)
                if tool_results:
                    context += (
                        "\n\nCONTEXT TOOL RESULTS (untrusted project data)\n"
                        + _canonical(_durable_evidence(tool_results, string_limit=12_000, list_limit=80))
                    )
                prompt = (
                    "Take the next useful action for this exact task. Use bounded context tools when "
                    "repository evidence or a targeted check is needed. Request tools or propose changes, "
                    "never both in one response. Return the structured action only."
                )
                action = ask_action(prompt, context, phase)
                _validate_action_semantics(action, task)
                calls = action.get("tool_calls") or []
                if calls:
                    if action.get("changes"):
                        raise HarnessError(
                            "An agent response may request context tools or propose changes, not both atomically"
                        )
                    self.store.acknowledge_context_step(goal_id, task, action, phase)
                    effect_acknowledged = True
                    if not run_context_calls(calls, set()):
                        return task, {"action": "deferred", "summary": "Paused at a context-tool boundary", "changes": []}
                    phase = "context_tools"
                    continue
                requested = [
                    _short(one, 240) for one in action.get("needs_files", []) if _short(one, 240)
                ]
                new_requested = [one for one in requested if one not in requested_files]
                if new_requested and not action.get("changes") and action.get("action") == "work":
                    requested_files.extend(new_requested)
                    self.store.acknowledge_file_request(
                        goal_id, task, new_requested, phase
                    )
                    effect_acknowledged = True
                    phase = "requested_files"
                    continue
                break
            action = self.store.sanitize_action(action)
            action["_nexus_baselines"] = {
                str(one.get("path") or "").replace("\\", "/").strip(): baseline_manifest.get(
                    str(one.get("path") or "").replace("\\", "/").strip(), "missing"
                )
                for one in action.get("changes", []) if isinstance(one, dict)
                and str(one.get("path") or "").strip()
            }
            if not self.store.record_action(goal_id, task, action):
                return task, {
                    "action": "superseded",
                    "summary": "Discarded because the user steered the goal after this provider turn began.",
                    "changes": [],
                }
            return task, action
        except Exception as exc:
            uncertain = isinstance(exc, ProviderOutcomeUnknown)
            if dispatch_admission_failed:
                current_goal = self.store.get(goal_id)
                current_task = next(
                    one for one in current_goal["tasks"] if one["id"] == task_id
                )
                if str(current_task.get("provider_effect_state") or "") == "reply_received":
                    self.store.block_received_reply(
                        goal_id, task,
                        "A provider reply was received, but the next repair/continuation was not admitted. "
                        "Inspect the provider transcript and explicitly reconcile before retrying.",
                    )
                    return task, {
                        "action": "deferred",
                        "summary": "A received provider reply requires reconciliation before retry.",
                        "changes": [],
                    }
                if current_goal["status"] in {"paused", "waiting_for_user"}:
                    self.store.defer_context_continuation(goal_id, task)
                    return task, {
                        "action": "deferred",
                        "summary": "The provider dispatch was not admitted after the user-control boundary.",
                        "changes": [],
                    }
                if current_goal["status"] == "cancelled":
                    return task, {
                        "action": "superseded", "summary": "The goal was cancelled before dispatch.",
                        "changes": [],
                    }
            self.store.fail_task(
                goal_id, task, str(exc), uncertain=uncertain,
                allow_failover=(
                    provider_attempt_started and not dispatch_admission_failed
                    and not effect_acknowledged and not uncertain
                ),
            )
            return task, {"action": "failed", "summary": str(exc), "changes": []}
        finally:
            if context_tools is not None:
                context_tools.close()

    def _act_node(self, state: GoalGraphState) -> GoalGraphState:
        task_ids = list(state.get("task_ids") or [])
        actions: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=max(1, len(task_ids))) as pool:
            futures = {pool.submit(self._execute_one, state["goal_id"], task_id): task_id for task_id in task_ids}
            for future in as_completed(futures):
                task, action = future.result()
                actions.append({"task": task, "action": action})
        return {"actions": actions, "route": "apply"}

    def _apply_node(self, state: GoalGraphState) -> GoalGraphState:
        # Project mutation and user controls share this critical section. A
        # Pause/Cancel/Steer that arrives after file application begins takes
        # effect at the next transaction boundary, never in the middle of it.
        with self.lock:
            return self._apply_node_locked(state)

    def _apply_node_locked(self, state: GoalGraphState) -> GoalGraphState:
        interrupts: list[str] = []
        goal_id = state["goal_id"]
        action_items = list(state.get("actions", []))
        if not action_items and state.get("task_ids"):
            snapshot = self.store.get(goal_id)
            for task_id in state["task_ids"]:
                task = next(one for one in snapshot["tasks"] if one["id"] == task_id)
                if task.get("pending_action"):
                    action_items.append({"task": task, "action": task["pending_action"]})
        for item in action_items:
            task, action = item["task"], item["action"]
            if action.get("action") in {"failed", "superseded", "deferred"}:
                continue
            current_goal = self.store.get(goal_id)
            if current_goal["status"] == "cancelled":
                continue
            current_task = next(one for one in current_goal["tasks"] if one["id"] == task["id"])
            if current_task["state"] not in {"running", "pending_apply"} \
                    or int(current_task.get("claim_objective_epoch") or 0) \
                    != int(current_goal.get("objective_epoch") or 1):
                continue
            _validate_action_semantics(action, current_task)
            if current_goal["status"] in {
                "paused", "waiting_for_user", "cancelling",
            }:
                # Parallel provider replies are acknowledged before this apply
                # loop. Once any one action opens a human decision boundary,
                # later actions must remain durable pending work; mutating files
                # behind the decision card violates the pause contract.
                self.store.defer_pending_action(goal_id, task)
                continue
            if (
                action.get("action") == "request_review"
                or self.store._needs_review(current_goal, current_task, action, None)
            ):
                staged, review_interrupts = self.store.stage_review_if_needed(
                    goal_id, task, action
                )
                interrupts.extend(review_interrupts)
                if staged:
                    continue
            artifact = None
            changes = action.get("changes") or []
            if changes:
                goal = self.store.get(goal_id)
                self._require_goal_authority(goal)
                root = Path(goal["project"]["path"])
                current_task = next(one for one in goal["tasks"] if one["id"] == task["id"])
                pending = current_task.get("pending_transaction") or {}
                if pending.get("state") == "applied" and pending.get("artifact"):
                    artifact = pending["artifact"]
                else:
                    baselines = action.get("_nexus_baselines")
                    if not isinstance(baselines, dict):
                        raise HarnessError(
                            "The provider proposal is not bound to the project snapshot it observed"
                        )
                    for raw_change in changes:
                        relative = str(raw_change.get("path") or "").replace("\\", "/").strip()
                        if relative not in baselines:
                            raise HarnessError(
                                f"The provider proposal has no observed baseline for {relative}"
                            )
                        observed = str(baselines[relative])
                        current = _path_baseline_marker(root, relative)
                        if not hmac.compare_digest(observed, current):
                            raise HarnessError(
                                f"Baseline conflict: {relative}; the project changed after the agent observed it"
                            )
                    plans = swarm_work._validated_changes(root, changes)
                    if not plans:
                        raise HarnessError(
                            "The proposed file plan makes no effective change to the current project snapshot"
                        )
                    transaction_id = str(pending.get("transaction_id") or FileTransaction.new_transaction_id())
                    if not pending:
                        self.store.prepare_transaction(goal_id, task, transaction_id, changes)
                    manifest = FileTransaction(
                        root, max_files=12,
                        max_bytes=int(self.config.get("execution.max_changed_bytes")),
                    ).apply(plans, transaction_id=transaction_id)
                    artifact = {
                        "kind": "file_transaction", "transaction_id": transaction_id,
                        "changes": manifest.get("changes", []),
                        "patch": _short(manifest.get("patch"), 80_000),
                        "patch_sha256": manifest.get("patch_sha256", ""),
                    }
                    self.store.record_transaction_applied(goal_id, task, artifact)
            elif action.get("action") in {"complete", "request_review"}:
                goal = self.store.get(goal_id)
                self._require_goal_authority(goal)
                root = Path(goal["project"]["path"])
                merkle, manifest = swarm_work._project_tree_merkle(root)
                artifact = {
                    "kind": "verified_no_change", "tree_merkle": merkle,
                    "file_count": len(manifest), "observed_at_ms": _now(),
                }
            interrupts.extend(self.store.apply_action(goal_id, task, action, artifact=artifact))
        goal = self.store.get(goal_id)
        if goal["status"] == "waiting_for_user" and interrupts:
            return {"interrupt_ids": interrupts, "route": "human"}
        return {
            "route": "end" if goal["status"] in TERMINAL_GOALS | {
                "paused", "cancelling",
            } else "schedule",
        }

    def _human_node(self, state: GoalGraphState) -> GoalGraphState:
        goal = self.store.get(state["goal_id"])
        pending = [one for one in goal["interrupts"] if one["id"] in state.get("interrupt_ids", []) and one["state"] == "pending"]
        answers = interrupt({"goal_id": goal["goal_id"], "revision": goal["revision"], "interrupts": pending})
        if not (isinstance(answers, dict) and answers.get("_nexus_resolved") is True):
            self.store.resolve_interrupts(goal["goal_id"], answers)
        return {"route": "schedule", "interrupt_ids": []}

    def _verify_node(self, state: GoalGraphState) -> GoalGraphState:
        goal = self.store.get(state["goal_id"])
        self._require_goal_authority(goal)
        root = Path(goal["project"]["path"])
        changed = [
            str(change.get("path")) for artifact in goal.get("artifacts", []) if isinstance(artifact, dict)
            for change in artifact.get("changes", []) if isinstance(change, dict) and change.get("path")
        ]
        project = {"id": goal["project"]["id"], "name": goal["project"]["name"],
                   "path": goal["project"]["path"], "tasks": [goal["objective"]]}
        result = swarm_work._run_selected_project_verification(
            self.config, root, project, goal["objective"], list(dict.fromkeys(changed)), None,
            verification_session_id=goal["goal_id"],
        )
        updated = self.store.complete_verification(
            goal["goal_id"], result,
            expected_revision=int(goal["revision"]),
            expected_objective_epoch=int(goal.get("objective_epoch") or 1),
        )
        self._start_promoted_goals(updated.get("promoted_goal_ids", []))
        return {
            "route": "end" if updated["status"] in TERMINAL_GOALS | {
                "paused", "cancelling",
            } else "schedule",
        }

    def run(
        self, goal_id: str, answers: dict[str, Any] | None = None,
        *, _scheduler_id: str = "",
    ) -> dict[str, Any]:
        scheduler_id = str(_scheduler_id or uuid.uuid4().hex)
        if not _scheduler_id and not self.store.claim_scheduler(goal_id, scheduler_id):
            return self.store.public(self.store.get(goal_id), reused=True)
        with self.lock:
            self.scheduler_ids[goal_id] = scheduler_id
        config = {"configurable": {"thread_id": goal_id}, "recursion_limit": 10_000}
        promoted: list[str] = []
        try:
            if answers is not None:
                self.graph.invoke(Command(resume=answers), config=config)
            else:
                self.graph.invoke({"goal_id": goal_id}, config=config)
        finally:
            with self.lock:
                try:
                    current = self.store.get(goal_id)
                    if current.get("status") == "cancelling" \
                            and (current.get("cancellation") or {}).get("state") == "draining":
                        finalized = self.store.control(goal_id, "cancel", {
                            "drain_complete": True,
                            "scheduler_id": scheduler_id,
                        })
                        promoted = list(finalized.get("promoted_goal_ids", []))
                finally:
                    self.store.release_scheduler(goal_id, scheduler_id)
                    if self.scheduler_ids.get(goal_id) == scheduler_id:
                        self.scheduler_ids.pop(goal_id, None)
                # A different Nexus process does not share this runtime lock.
                # If its Cancel committed between our first read and the lease
                # CAS-clear, finish the now-drained request from a fresh durable
                # snapshot. A later Cancel sees no live lease and settles in its
                # own transaction, so neither interleaving can strand ownership.
                after_release = self.store.get(goal_id)
                if after_release.get("status") == "cancelling" \
                        and not self.store._scheduler_live(after_release):
                    finalized = self.store.control(goal_id, "cancel", {
                        "drain_complete": True,
                    })
                    promoted.extend(finalized.get("promoted_goal_ids", []))
            self._start_promoted_goals(promoted)
        return self.store.public(self.store.get(goal_id))

    def start_background(self, goal_id: str, answers: dict[str, Any] | None = None) -> dict[str, Any]:
        self._enable_auto_start_watcher()
        with self.lock:
            self._auto_start_attempted[goal_id] = time.monotonic()
            existing = self.workers.get(goal_id)
            if existing is not None and existing.is_alive():
                return self.store.public(self.store.get(goal_id), reused=True)
            goal = self.store.get(goal_id)
            self._require_goal_authority(goal)
            self._require_agent_setup(goal)
            if goal["status"] == "waiting_for_project":
                return self.store.public(goal)
            if not self.store._is_project_owner(goal):
                raise HarnessError("This long-horizon goal does not own its target project")
            self._require_no_external_owner(Path(goal["project"]["path"]))
            competing = self.store.active_overlapping_project(
                Path(goal["project"]["path"]), except_goal_id=goal_id
            )
            if competing:
                raise HarnessError(
                    "Another long-horizon goal already owns this project. Cancel or finish it, or create an isolated fork."
                )
            scheduler_id = uuid.uuid4().hex
            if not self.store.claim_scheduler(goal_id, scheduler_id):
                return self.store.public(self.store.get(goal_id), reused=True)
            self.scheduler_ids[goal_id] = scheduler_id
            def work() -> None:
                try:
                    self.run(goal_id, answers, _scheduler_id=scheduler_id)
                except Exception as exc:
                    try:
                        goal = self.store.get(goal_id)
                        if goal["status"] not in TERMINAL_GOALS | {
                            "waiting_for_user", "waiting_for_project", "paused", "cancelling",
                        }:
                            self.store.fail_pending_apply(goal_id, str(exc))
                            goal = self.store.get(goal_id)
                            if goal["status"] not in TERMINAL_GOALS | {
                                "waiting_for_user", "waiting_for_project", "paused", "cancelling",
                            }:
                                self.store.control(goal_id, "pause")
                    except Exception:
                        pass
                finally:
                    with self.lock:
                        self.workers.pop(goal_id, None)
            thread = threading.Thread(target=work, name=f"nexus-goal-{goal_id[:8]}", daemon=True)
            self.workers[goal_id] = thread
            try:
                thread.start()
            except Exception:
                self.workers.pop(goal_id, None)
                self.scheduler_ids.pop(goal_id, None)
                self.store.release_scheduler(goal_id, scheduler_id)
                raise
        return self.store.public(self.store.get(goal_id))

    def _require_available_project(self, goal_id: str) -> dict[str, Any]:
        goal = self.store.get(goal_id)
        self._require_goal_authority(goal)
        self._require_agent_setup(goal)
        if goal["status"] == "waiting_for_project":
            raise HarnessError(
                "This goal is waiting for the current project owner and cannot continue yet"
            )
        if not self.store._is_project_owner(goal):
            raise HarnessError("This long-horizon goal does not own its target project")
        self._require_no_external_owner(Path(goal["project"]["path"]))
        competing = self.store.active_overlapping_project(
            Path(goal["project"]["path"]), except_goal_id=goal_id,
        )
        if competing:
            raise HarnessError(
                "Another long-horizon goal already owns this project. Cancel or finish it, or create an isolated fork."
            )
        return goal

    def control(
        self, goal_id: str, action: str, payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        activates = {"resume", "retry", "reassign", "steer", "message", "request_review"}
        with self.lock:
            current = self.store.get(goal_id)
            released_failed_cancel_with_effects = action == "cancel" \
                and current.get("status") == "failed" \
                and self.store._project_queue_state(current) == "released" \
                and any(_task_has_unsettled_effect(one) for one in current["tasks"])
            if released_failed_cancel_with_effects:
                self._require_goal_authority(current)
                self._require_agent_setup(current)
                self._require_no_external_owner(Path(current["project"]["path"]))
            if action in activates:
                released_failed_resume = action == "resume" \
                    and current.get("status") == "failed" \
                    and self.store._project_queue_state(current) == "released"
                if released_failed_resume:
                    self._require_goal_authority(current)
                    self._require_agent_setup(current)
                    self._require_no_external_owner(Path(current["project"]["path"]))
                else:
                    self._require_available_project(goal_id)
            goal = self.store.control(goal_id, action, payload)
            if action in activates and goal["status"] == "queued":
                self.start_background(goal_id)
            self._start_promoted_goals(goal.get("promoted_goal_ids", []))
            return goal

    def _start_promoted_goals(self, goal_ids: object) -> None:
        if not isinstance(goal_ids, list):
            return
        for goal_id in list(dict.fromkeys(str(one) for one in goal_ids if str(one))):
            try:
                goal = self.store.get(goal_id)
                if goal["status"] == "queued" and self.store._is_project_owner(goal):
                    self.start_background(goal_id)
            except HarnessError:
                # Promotion is already durable.  A changed provider setup or a
                # newly arrived legacy owner must not roll back terminal work
                # or cause an automatic resend; recovery exposes the queued
                # checkpoint for explicit continuation.
                continue

    def start_board(self, board: dict[str, Any], request_id: str) -> list[dict[str, Any]]:
        with self.lock:
            goals = []
            selected_projects = [
                project for project in board.get("projects", [])
                if isinstance(project, dict) and project.get("is_there") is True
                and any(isinstance(one, str) and one.strip() for one in project.get("tasks", []))
            ]
            roots = [Path(str(project.get("path") or "")).resolve(strict=True) for project in selected_projects]
            specs = []
            for project, root in zip(selected_projects, roots):
                objectives = [str(one) for one in project.get("tasks", []) if isinstance(one, str) and one.strip()]
                per_request = _stable_id("board", request_id, project.get("id"))
                existing = self.store.get_by_request(per_request)
                self._require_no_external_owner(root)
                if existing is None:
                    self.store.validate_create(
                        board, str(project.get("id") or ""), objectives, per_request,
                    )
                specs.append((project, objectives, per_request))
            for project, objectives, per_request in specs:
                # Reuse the same intent-bound admission path as explicit goals.
                # A stable legacy request ID must never silently return an old
                # goal after its saved objectives, team, route, or authority
                # have changed.
                goal = self.start(
                    board, str(project.get("id") or ""), objectives, per_request,
                )
                goals.append(goal)
            if not goals:
                raise HarnessError("Write at least one goal on a project with one ready assigned agent")
            return goals

    def recover_all(self) -> list[dict[str, Any]]:
        self._enable_auto_start_watcher()
        recovered: list[dict[str, Any]] = list(self.store.reconcile_project_queue())
        for goal in self.store.active_authority_goals():
            if goal["status"] == "waiting_for_project":
                continue
            if goal["status"] == "cancelling":
                if self.store._scheduler_live(goal):
                    continue
                if any(one["state"] == "running" for one in goal["tasks"]):
                    recovered.append(self.store.recover_dead(goal["goal_id"]))
                current = self.store.get(goal["goal_id"])
                if current.get("status") == "cancelling" \
                        and not self.store._scheduler_live(current):
                    finalized = self.store.control(goal["goal_id"], "cancel", {
                        "drain_complete": True,
                    })
                    recovered.append(finalized)
                    self._start_promoted_goals(finalized.get("promoted_goal_ids", []))
            elif any(one["state"] == "running" for one in goal["tasks"]):
                recovered.append(self.store.recover_dead(goal["goal_id"]))
            elif goal["status"] == "queued":
                queued = self.store.recover_orphaned_queue(goal["goal_id"])
                recovered.append(queued)
                if queued.get("status") == "queued" \
                        and (queued.get("project_queue") or {}).get(
                            "auto_start_pending"
                        ) is True:
                    try:
                        self.start_background(goal["goal_id"])
                    except Exception:
                        # Recovery is an availability path. A removed project,
                        # provider drift, or external owner must leave the
                        # durable checkpoint inspectable instead of preventing
                        # the runtime from loading other goals.
                        continue
        return recovered

    def preflight_start(
        self, board: dict[str, Any], project_id: str, objectives: list[str],
        request_id: str, *, lead_id: str = "",
        success_criteria: list[str] | None = None,
        policy: dict[str, Any] | None = None, attachments: object = None,
        participant_ids: list[str] | None = None, conversation_id: str = "",
        expected_project_authority_id: str = "",
        require_all_participants: bool | None = None,
    ) -> dict[str, Any]:
        """Validate and bind an admission without scheduling or dispatching it."""

        with self.lock:
            return self.store.preflight_runtime_admission(
                board, project_id, objectives, request_id, lead_id=lead_id,
                success_criteria=success_criteria, policy=policy,
                attachments=attachments, participant_ids=participant_ids,
                conversation_id=conversation_id,
                expected_project_authority_id=expected_project_authority_id,
                require_all_participants=require_all_participants,
            )

    def start(
        self, board: dict[str, Any], project_id: str, objectives: list[str],
        request_id: str, *, lead_id: str = "", success_criteria: list[str] | None = None,
        policy: dict[str, Any] | None = None, attachments: object = None,
        participant_ids: list[str] | None = None, conversation_id: str = "",
        expected_project_authority_id: str = "",
        require_all_participants: bool | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            inspected = self.store.preflight_runtime_admission(
                board, project_id, objectives, request_id, lead_id=lead_id,
                success_criteria=success_criteria, policy=policy,
                attachments=attachments, participant_ids=participant_ids,
                conversation_id=conversation_id,
                expected_project_authority_id=expected_project_authority_id,
                require_all_participants=require_all_participants,
            )
            require_all = bool(inspected["require_all_participants"])
            root = Path(inspected["root"])
            agents = list(inspected["agents"])
            lead = dict(inspected["lead"])
            goal = inspected["goal"]
            request_retired = bool(inspected["request_retired"])
            actual_authority_id = str(inspected["project_authority_id"])
            admission_digest = str(inspected["admission_digest"])
            if goal is not None:
                if request_retired:
                    # Replay protection is already the terminal result. Do not
                    # start the watcher, reconcile ownership, call get(), stage
                    # attachments, or reach any provider/background dispatch.
                    return self.store.public(goal, reused=True)
                self._enable_auto_start_watcher()
                self._require_no_external_owner(root)
                self.store.reconcile_project_queue()
                goal = self.store.get_by_request(request_id)
            else:
                self._enable_auto_start_watcher()
                self._require_no_external_owner(root)
                input_bundle = None
                if attachments:
                    agents = self.store._agents_for_project(
                        board, project_id, participant_ids
                    )
                    if not agents:
                        raise HarnessError("Assign at least one ready agent to this project")
                    lead = next((one for one in agents if one["id"] == lead_id), agents[0])
                    attachment_root = (
                        self.store.root / "long-horizon-inputs" / self.store.authority_key
                        / hashlib.sha256(_exact_request_id(request_id).encode("utf-8")).hexdigest()
                    ).resolve()
                    expected_parent = (self.store.root / "long-horizon-inputs" / self.store.authority_key).resolve()
                    if expected_parent not in attachment_root.parents:
                        raise HarnessError("The request attachment staging path escaped its authority")
                    if attachment_root.exists():
                        shutil.rmtree(attachment_root)
                    attachment_root.mkdir(parents=True)
                    attachment_config = LoadedConfig(
                        copy.deepcopy(self.config.data), attachment_root,
                        list(self.config.sources), dict(self.config.provenance),
                        copy.deepcopy(self.config.trusted_floor),
                    )
                    try:
                        kept, provider_files, attachment_text = chat_lab.keep_attachments(
                            attachment_config, lead["who"], attachments, lead["name"],
                        )
                        input_bundle = {
                            "public_files": kept,
                            "provider_files": provider_files,
                            "attachment_text": attachment_text,
                        }
                        goal = self.store.create(
                            board, project_id, objectives, request_id, lead_id=lead_id,
                            success_criteria=success_criteria, policy=policy,
                            input_bundle=input_bundle,
                            participant_ids=participant_ids,
                            require_all_participants=require_all,
                            conversation_id=conversation_id,
                            admission_digest=admission_digest,
                            expected_project_authority_id=actual_authority_id,
                        )
                    except Exception:
                        if attachment_root.exists() and expected_parent in attachment_root.parents:
                            shutil.rmtree(attachment_root)
                        raise
                else:
                    goal = self.store.create(
                        board, project_id, objectives, request_id, lead_id=lead_id,
                        success_criteria=success_criteria, policy=policy,
                        input_bundle=input_bundle,
                        participant_ids=participant_ids,
                        require_all_participants=require_all,
                        conversation_id=conversation_id,
                        admission_digest=admission_digest,
                        expected_project_authority_id=actual_authority_id,
                    )
            if goal.get("request_tombstone") is True:
                # Detailed terminal history is intentionally bounded, but a
                # retired request identity is a permanent idempotency result.
                # Never ask the scheduler for a pruned goal or dispatch it.
                return self.store.public(goal, reused=True)
            if goal["status"] == "queued":
                self.start_background(goal["goal_id"])
            return self.store.public(
                self.store.get(goal["goal_id"]), reused=goal.get("reused", False),
            )

    def resume(self, goal_id: str, answers: dict[str, Any] | None = None) -> dict[str, Any]:
        if answers is None:
            return self.control(goal_id, "resume")
        with self.lock:
            self._require_available_project(goal_id)
            # Validate and commit the exact decision synchronously so stale or
            # malformed cards are rejected by the HTTP request itself instead
            # of disappearing into a background worker.
            self.store.resolve_interrupts(goal_id, answers)
            return self.start_background(goal_id, {"_nexus_resolved": True})

    def fork(self, goal_id: str, request_id: str) -> dict[str, Any]:
        with self.lock:
            return self._fork_locked(goal_id, request_id)

    def _fork_locked(self, goal_id: str, request_id: str) -> dict[str, Any]:
        existing = self.store.get_by_request(request_id)
        if existing is not None:
            if existing.get("parent_goal_id") != goal_id:
                raise HarnessError("That fork request identity already belongs to another goal")
            return existing
        source = self.store.get(goal_id)
        # Forking creates a Git worktree, so all saved execution authority
        # must still match before that filesystem side effect.  A renderer
        # normally disables this control when provider setup drifts, but the
        # authenticated API is the actual security/reliability boundary.
        self._require_agent_setup(source)
        self._require_goal_authority(source)
        if any(one.get("state") == "pending" for one in source.get("interrupts", [])):
            raise HarnessError("Answer or cancel the pending decision before forking this goal")
        root = Path(source["project"]["path"])
        if subprocess.run(["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True).stdout.strip():
            raise HarnessError("Forking project work requires a clean Git worktree so branches cannot silently lose changes")
        fork_id = hashlib.sha256(
            f"{self.store.authority_key}\0{request_id}".encode("utf-8")
        ).hexdigest()[:32]
        target = self.store.root / "goal-worktrees" / fork_id
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            probe = subprocess.run(
                ["git", "-C", str(target), "rev-parse", "--show-toplevel"],
                capture_output=True, text=True,
            )
            if probe.returncode != 0 or Path(probe.stdout.strip()).resolve() != target.resolve():
                raise HarnessError("The deterministic fork path exists but is not the expected Git worktree")
        else:
            result = subprocess.run(
                ["git", "-C", str(root), "worktree", "add", "--detach", str(target), "HEAD"],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                raise HarnessError("Git could not create an isolated goal fork: " + _short(result.stderr, 2_000))
        return self.store.clone_to_project(
            source, source["project"]["id"] + "-fork",
            source["project"]["name"] + " fork", target, request_id,
        )
