"""Keep delivered agent prose in the conversation without treating it as control."""
from __future__ import annotations

import hashlib
import json
import re

CONTRACT = "collaboration-reply/v1-prose-continuation"


def is_action_envelope(answer: dict) -> bool:
    """Classify for rejection/display only, never parse or authorize an action."""
    raw = str(answer.get("text") or "").lstrip("\ufeff \t\r\n")
    return bool(re.match(r'(?:```(?:json)?\s*|json\s+)?\{', raw, re.IGNORECASE)
                and re.search(r'"(?:action|changes|tool_calls|needs_files)"\s*:', raw))


def has_proposed_changes(answer: dict) -> bool:
    # This conservative marker only prevents a repair from silently dropping
    # a proposal. It never recovers or applies malformed source bytes.
    return bool(re.search(r'"changes"\s*:\s*\[\s*\{', str(answer.get("text") or "")))


def is_format_failure(reason: str) -> bool:
    return any(marker in reason for marker in (
        "did not return the structured collaboration result Nexus requested",
        "returned the wrong collaboration result shape",
        "returned an invalid nexus_long_horizon_action_v1 result:",
        "The assistant returned malformed nexus_long_horizon_action_v1 JSON",
    ))


def continuation(answer: dict) -> dict:
    """Prose is dialogue, never a completion verdict, command, or permission."""
    raw = str(answer.get("text") or "").strip()
    summary = ("The provider returned an invalid action. Its proposed edits were not applied."
               if is_action_envelope(answer) else raw[:12000]) or "My reply was empty. I need to inspect the current work and continue."
    return {
        "action": "work", "summary": summary, "evidence": [], "risk": "low",
        "changes": [], "needs_files": [], "tasks": [], "questions": [],
        "tool_calls": [], "handoff_agent_id": "", "criteria_evidence": [],
    }


def receipt(answer: dict, schema: dict) -> dict:
    return {"schema_version": 1, "contract": CONTRACT,
            "contract_fingerprint_sha256": hashlib.sha256(json.dumps(
                {"contract": CONTRACT, "schema": schema}, sort_keys=True).encode()).hexdigest(),
            "reply_sha256": hashlib.sha256(str(answer.get("text") or "").encode()).hexdigest()}


def repair_binding(schema, provider_identity):
    return hashlib.sha256(json.dumps({"contract": "collaboration-format-recovery/v1",
        "schema": schema, "provider": provider_identity}, sort_keys=True).encode()).hexdigest()


def repair_exhausted(task, binding):
    held = task.get("format_repair_state") or {}
    return held.get("schema_version") == 1 and held.get("binding") == binding and held.get("failed_repairs", 0) >= 1


def undelivered_on_resume(document: dict) -> list[str]:
    """Reopen legacy prose-fallback completions on explicit Resume only.

    Authenticated dialogue is diagnostic evidence, never an executable patch.
    A no-change completion after a raw proposed patch needs another inspection;
    preserve every real artifact and never reapply the raw proposal.
    """
    reopened = []
    messages = (document.get("dialogue") or {}).get("messages", [])
    for task in document.get("tasks", []):
        if task.get("state") != "complete" or any(a.get("changes") for a in task.get("artifacts", [])):
            continue
        if not any(m.get("task_id") == task.get("id") and m.get("action") == "work"
                   and m.get("objective_epoch", 1) == document.get("objective_epoch", 1)
                   and is_action_envelope({"text": m.get("summary")})
                   and has_proposed_changes({"text": m.get("summary")}) for m in messages):
            continue
        agent = next((a for a in document.get("agents", []) if a.get("id") == task.get("assigned_agent_id")), {})
        binding = hashlib.sha256(json.dumps({"contract": "undelivered-proposal-recovery/v1",
            "epoch": document.get("objective_epoch", 1), "provider": agent.get("route_binding", {}),
            "goal_id": document.get("goal_id"), "task_id": task.get("id")}, sort_keys=True).encode()).hexdigest()
        if task.get("delivery_recovery") == {"schema_version": 1, "binding": binding}:
            continue
        task.update({"state": "ready", "criteria_evidence": [], "agreed_artifact_generation": -1,
                     "delivery_recovery": {"schema_version": 1, "binding": binding}})
        task.pop("applied_action_receipt", None)
        reopened.append(task["id"])
    return reopened
