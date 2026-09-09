"""Versioned, exact-effect recovery choices for interrupted goal turns.

This is a projection of authenticated goal state, never a permission grant.
Only an explicit Resume may replace a lost read-only inference. Other remote
effects need a separate, explicit choice; saved file work is never discarded.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

CONTRACT = "goal-interrupted-turn-recovery/v2-native-workspace"


def plan(document: dict[str, Any], tasks: list[dict[str, Any]], *,
         settled: bool, setup_changed: bool) -> dict[str, Any]:
    items = []
    for task in tasks:
        agent = next((one for one in document.get("agents", [])
                      if one["id"] == task.get("assigned_agent_id")), {})
        binding = agent.get("route_binding") or {}
        provider_only = bool(task.get("provider_effect_id")) and bool(
            task.get("outcome_unknown") or task.get("reconciliation_required")
        ) and not task.get("pending_action") and not task.get("pending_transaction") \
            and task.get("provider_effect_state") in {
                "outcome_unknown", "reply_received_reconciliation_required",
                "dispatched", "reply_received",
            }
        # Bounded bridge for already-saved calls under this exact engine-owned
        # contract. It uses ephemeral cwd, ignores user config/rules, rejects
        # native tools, and enforces a read-only sandbox (codex_cli.py).
        read_only_dispatch = binding.get("effective_dispatch_contract") in {
            "codex-cli/effective-dispatch/v2", "codex-cli/effective-dispatch/v3-native-workspace",
        } and not document.get("agent_workspace_contract")
        read_only = provider_only and binding.get("binding_schema_version") == 3 \
            and binding.get("transport_contract") == "codex-cli/isolated-exec/v1" \
            and read_only_dispatch
        items.append({
            "task_id": task["id"], "agent_id": agent.get("id", ""),
            "agent_name": agent.get("name") or agent.get("id") or "Agent",
            "effect_id": task.get("provider_effect_id", ""),
            "kind": "read_only_reply" if read_only else "provider_reply" if provider_only else "saved_work",
            "reason": task.get("last_error", ""),
        })
    available = bool(items) and settled and not setup_changed
    can_retry = available and all(one["kind"] != "saved_work" for one in items)
    automatic = can_retry and all(one["kind"] == "read_only_reply" for one in items)
    material = {"contract": CONTRACT, "goal_id": document["goal_id"],
                "revision": document["revision"], "agents": document.get("agents"),
                "project": document.get("project"), "execution_contract": document.get("execution_contract"),
                "agent_workspace_contract": document.get("agent_workspace_contract"),
                "tasks": tasks, "available": available}
    fingerprint = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"schema_version": 1, "contract": CONTRACT, "fingerprint": fingerprint,
            "items": items, "can_retry": can_retry, "resume_safe": automatic,
            "message": (
                "An agent reply was interrupted. Resume team will request a fresh read-only reply and keep saved work and permissions."
                if automatic else
                "An agent call was interrupted. Review recovery below; command permission is already a separate setting."
                if can_retry else
                "The project or provider setup changed. Restore the saved setup or start a new goal; the interrupted call has been kept."
                if items and setup_changed else
                "Wait for the current agent worker to stop before recovering the interrupted call."
                if items and not settled else
                "Saved work needs inspection before retrying. Open the goal details to inspect the affected task and its files."
                if items else "")}
