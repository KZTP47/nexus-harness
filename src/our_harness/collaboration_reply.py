"""Keep delivered agent prose in the conversation without treating it as control."""
from __future__ import annotations

import hashlib
import json

CONTRACT = "collaboration-reply/v1-prose-continuation"


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
    summary = raw[:12000] or "My reply was empty. I need to inspect the current work and continue."
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
