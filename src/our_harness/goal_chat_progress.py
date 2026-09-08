"""Plain-English chat milestones derived only from recorded engine events."""

from __future__ import annotations

import json
from typing import Any


EVENT_TYPES = frozenset({
    "provider_dispatched", "provider_reply_received", "context_step_acknowledged",
    "context_tool_requested", "context_tool_result", "context_tool_failed",
})

TOOL_NAMES = {
    "run_selected_verification": "project checks", "read_file": "file reading",
    "request_file_context": "project file reading", "search_workspace": "project search",
    "list_directory": "file listing", "read_proposed_change": "proposed change inspection",
    "read_shared_conversation": "conversation history reading",
}


def tool_outcome(name: str, result: object, error: str = "") -> tuple[str, str]:
    """Read execution status, never interpret arbitrary file contents as status."""
    value = result if isinstance(result, dict) else {}
    if error:
        return "failed", error
    if value.get("status") == "error":
        return "failed", str(value.get("error") or "The tool reported an error.")
    # Verification is an engine-owned structured report inside a tool envelope.
    # A successful transport does not mean its project checks passed.
    if name == "run_selected_verification" and isinstance(value.get("content"), str):
        try:
            report = json.loads(value["content"])
        except (TypeError, ValueError):
            report = None
        if isinstance(report, dict):
            value = report
    status = str(value.get("status") or "")
    reason = str(value.get("reason") or value.get("error") or "")
    if value.get("passed") is False or value.get("success") is False \
            or status in {"failed", "error"} \
            or isinstance(value.get("exit_code"), int) and value["exit_code"] != 0:
        return "failed", reason
    if status in {"unavailable", "blocked", "approval_required", "not_configured"}:
        approval = "approval" in str(value.get("basis") or "") or status == "approval_required"
        return "approval_required" if approval else "unavailable", reason
    if name == "run_selected_verification" and (status == "passed" or value.get("passed") is True):
        return "passed", reason
    return "finished", reason


def milestone(event: dict[str, Any], agent_name: str) -> tuple[str, str] | None:
    """Return public wording and outcome; dispatch does not prove generation."""
    kind = event.get("type")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    if kind == "provider_dispatched":
        if payload.get("phase") in {"context_tools", "context_tools_resume", "requested_files"}:
            return f"Nexus sent the tool results and updated context to {agent_name}. Waiting for the next reply.", "waiting"
        return f"Nexus sent a request to {agent_name}. Waiting for a reply.", "waiting"
    if kind == "provider_reply_received":
        return f"{agent_name} returned a response. Nexus is checking it.", "received"
    if kind == "context_step_acknowledged":
        calls = payload.get("calls") or []
        names = list(dict.fromkeys(TOOL_NAMES.get(str(call.get("name")), "a tool")
                                  for call in calls if isinstance(call, dict)))
        if names:
            return f"{agent_name} requested {'; '.join(names[:6])}.", "requested"
        return None
    name = str(payload.get("name") or "")
    label = TOOL_NAMES.get(name, "tool execution")
    if kind == "context_tool_requested":
        # Reservation is recorded before execution, which can still be denied.
        return f"Nexus is starting {label} requested by {agent_name}.", "started"
    if kind in {"context_tool_result", "context_tool_failed"}:
        outcome, reason = tool_outcome(name, payload.get("result"), str(payload.get("error") or ""))
        words = {"failed": "failed", "passed": "passed", "finished": "returned a result",
                 "approval_required": "needs approval", "unavailable": "could not complete"}[outcome]
        text = f"{label.capitalize()} {words}."
        if reason:
            text += " " + " ".join(reason.split())[:300]
        return text, outcome
    return None
