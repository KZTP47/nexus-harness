"""Bounded recovery for known rejected, unapplied action-field combinations."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from typing import Any

from .models import HarnessError
from .runtime_integrity import mac

SCHEMA_VERSION = 2
MAX_CORRECTIONS = 2
ERRORS = {
    "duplicate_tool_call_ids": "Give every tool call in one response a distinct nonempty call_id; IDs may be reused in later responses",
    "tasks_require_delegate": "Delegated tasks are allowed only in a delegate action",
    "questions_require_ask_user": "Structured questions are allowed only in an ask_user action",
    "target_requires_handoff": "A handoff target is allowed only in a handoff action",
    "tools_and_changes_conflict": "An agent response may request context tools or propose changes, not both atomically",
}
RULES = (
    "Choose exactly one action. tasks must be [] unless action is delegate; they are executable "
    "delegations, not a todo list or a description of the current team. questions must be [] unless "
    "action is ask_user. handoff_agent_id must be an empty string unless action is handoff. "
    "changes may be populated only for work, complete, or request_review. Never combine tool_calls "
    "with changes. Keep all unused action-specific arrays empty. To speak to your teammate or "
    "describe plans, put the actual message in summary and use work, without creating tasks. "
    "Nexus supplies the teammate's next turn automatically. These rules do not relax any path, "
    "review, evidence, authorization, or completion requirement."
)


class ActionProtocolError(HarnessError):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or ERRORS[code])


def error_code(error: str) -> str:
    for code, message in ERRORS.items():
        if error == message:
            return code
    if error in {f"The {kind} action cannot also change project files" for kind in (
        "delegate", "handoff", "ask_user", "blocked",
    )}:
        return "changes_require_work"
    return ""


def contract(response_schema: object, *, schema_version: int = SCHEMA_VERSION) -> dict[str, Any]:
    if schema_version not in {1, SCHEMA_VERSION}:
        raise HarnessError("Unsupported action-protocol correction contract")
    # Version 1 must remain byte-for-byte reproducible for authenticated records
    # written by a still-running older worker. Reads never upgrade those records.
    basis = {"schema_version": schema_version, "max_attempts": MAX_CORRECTIONS,
             "rules": RULES, "recovery": "known-unapplied-mutual-field-rejection-v1",
             "response_schema_sha256": hashlib.sha256(json.dumps(response_schema, sort_keys=True).encode()).hexdigest()}
    if schema_version == 2:
        basis.update({"attempt_scope": "consecutive-rejections-reset-only-after-corrected",
                      "cumulative_attempts": "all-admitted-protocol-corrections",
                      "legacy_counter_migration": "preserve-count-unless-previous-episode-corrected"})
    return {**basis, "fingerprint_sha256": hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()}


def upgrade_record(recovery: dict[str, Any], response_schema: object) -> None:
    """Upgrade an already validated record only inside an owned mutation."""
    if recovery.get("schema_version") != 1:
        return
    recovery["counter_migration"] = {
        "from_schema_version": 1,
        "from_contract_fingerprint_sha256": recovery["contract_fingerprint_sha256"],
        "carried_attempts": recovery["attempts"],
    }
    recovery.update({"schema_version": SCHEMA_VERSION,
                     "contract_fingerprint_sha256": contract(response_schema)["fingerprint_sha256"],
                     "cumulative_attempts": recovery["attempts"]})


def authenticated_events(db: sqlite3.Connection, document: dict[str, Any]) -> list[dict[str, Any]]:
    previous = str(document.get("event_floor_previous_sha256") or "")
    expected = int(document.get("event_floor_seq") or 1)
    events = []
    for row in db.execute("SELECT * FROM long_goal_events WHERE goal_id=? ORDER BY seq", (document["goal_id"],)):
        raw = str(row["event_json"])
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        event = json.loads(raw)
        material = [document["goal_id"], int(row["seq"]), str(row["event_id"]), str(row["type"]), raw, digest]
        if digest != row["event_sha256"] or not hmac.compare_digest(str(row["integrity_mac"]), mac("long-horizon-event-v1", material)) \
                or event.get("previous_sha256") != previous or event.get("seq") != expected:
            raise HarnessError("Action-protocol recovery evidence failed event integrity verification")
        events.append(event)
        previous, expected = digest, expected + 1
    if expected - 1 != int(document.get("event_seq") or 0) or previous != str(document.get("event_head_sha256") or ""):
        raise HarnessError("Action-protocol recovery evidence does not match its authenticated goal")
    return events


def legacy_rejection(task: dict[str, Any], document: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Require the exact delivered-reply/validator-failure chain, never just text."""
    code = error_code(str(task.get("last_error") or ""))
    if not code or task.get("protocol_recovery") or task.get("state") not in {"blocked", "failed"} \
            or task.get("provider_effect_state") != "known_reply_failed" \
            or task.get("outcome_unknown") or not task.get("reconciliation_required") \
            or task.get("pending_action") or task.get("pending_transaction") or task.get("applied_action_receipt") \
            or task.get("lease_id") or task.get("owner_pid") \
            or int(task.get("claim_objective_epoch") or 0) != int(document.get("objective_epoch") or 1):
        return None
    held = [one for one in events if one.get("task_id") == task["id"]]
    if len(held) < 3:
        return None
    dispatched, received, failed = held[-3:]
    effect = str(task.get("provider_effect_id") or "")
    if [one["type"] for one in (dispatched, received, failed)] != [
        "provider_dispatched", "provider_reply_received", "task_failed",
    ] or not effect or any(one.get("agent_id") != task.get("assigned_agent_id") for one in (dispatched, received, failed)) \
            or (dispatched.get("payload") or {}).get("effect_id") != effect \
            or (received.get("payload") or {}).get("effect_id") != effect \
            or (failed.get("payload") or {}).get("error") != task.get("last_error"):
        return None
    return {"error_code": code, "error": task["last_error"], "previous_effect_id": effect,
            "legacy_proof_event_ids": [one["event_id"] for one in (dispatched, received, failed)]}
