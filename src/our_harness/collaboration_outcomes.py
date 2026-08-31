"""Provider-neutral participant delivery outcomes for board collaboration.

The collaboration ledger owns detailed orchestration history.  This module
owns the small, versioned projection that belongs in the ordinary chat
transcript so a reopened chat can still say which requested agents answered
and which provider connection needs attention.
"""

from __future__ import annotations

from typing import Any, Iterable


SCHEMA_VERSION = 1
OUTCOMES = {"complete", "partial", "none"}
PARTICIPANT_STATUSES = {
    "answered",
    "failed",
    "outcome_unknown",
    "answered_then_failed",
    "answered_then_outcome_unknown",
}


def _text(value: object, limit: int) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


def identity(agent: dict[str, Any]) -> dict[str, str]:
    """Return the stable public identity used by delivery projections."""

    return {
        "agent_id": _text(agent.get("agent_id") or agent.get("id"), 120),
        "name": _text(agent.get("name") or "An agent", 240),
        "route": _text(agent.get("route") or agent.get("who"), 120),
    }


def _failure_identity(failure: dict[str, Any]) -> str:
    return _text(failure.get("agent_id") or failure.get("id"), 120)


def build(
    participants: Iterable[dict[str, Any]],
    *,
    answered_agent_ids: Iterable[str],
    failures: Iterable[dict[str, Any]] = (),
    requested_mode: str,
) -> dict[str, Any]:
    """Build one deterministic schema-v1 delivery projection.

    Multiple stage failures for one participant collapse into one row.  An
    unknown outcome dominates a known failure because it is the stricter
    recovery boundary.  A participant may still have ``answer_saved`` when a
    later discussion or final-report turn failed.
    """

    expected = [identity(one) for one in participants]
    answered = {_text(one, 120) for one in answered_agent_ids if _text(one, 120)}
    by_agent: dict[str, dict[str, Any]] = {}
    for failure in failures:
        if not isinstance(failure, dict):
            continue
        agent_id = _failure_identity(failure)
        if not agent_id:
            continue
        previous = by_agent.get(agent_id, {})
        reason = _text(
            failure.get("provider_reason") or previous.get("provider_reason"),
            65_536,
        )
        by_agent[agent_id] = {
            "outcome_unknown": bool(
                previous.get("outcome_unknown")
                or failure.get("outcome_unknown")
                or failure.get("_provider_outcome_unknown")
            ),
            "provider_reason": reason,
        }

    rows: list[dict[str, Any]] = []
    actions: list[dict[str, str]] = []
    for one in expected:
        agent_id = one["agent_id"]
        answer_saved = agent_id in answered
        failure = by_agent.get(agent_id)
        if failure is None and answer_saved:
            status = "answered"
            reason = ""
            outcome_unknown = False
        else:
            failure = failure or {
                "outcome_unknown": False,
                "provider_reason": "Nexus did not receive an answer from this requested agent.",
            }
            outcome_unknown = bool(failure["outcome_unknown"])
            reason = _text(failure.get("provider_reason"), 65_536)
            if answer_saved:
                status = (
                    "answered_then_outcome_unknown" if outcome_unknown
                    else "answered_then_failed"
                )
            else:
                status = "outcome_unknown" if outcome_unknown else "failed"
            if outcome_unknown:
                actions.append({
                    "id": "inspect-provider-turn",
                    "agent_id": agent_id,
                    "route": one["route"],
                    "label": f"Inspect {one['name']}'s provider turn",
                })
            else:
                actions.append({
                    "id": "repair-provider",
                    "agent_id": agent_id,
                    "route": one["route"],
                    "label": f"Repair {one['name']}'s provider",
                })
        row: dict[str, Any] = {
            **one,
            "status": status,
            "answer_saved": answer_saved,
            "provider_reason": reason,
            "outcome_unknown": outcome_unknown,
        }
        if status != "answered":
            if outcome_unknown:
                row["retry_allowed"] = False
                row["repair_allowed"] = False
            else:
                row["repair_allowed"] = True
        rows.append(row)

    answered_count = sum(1 for one in rows if one["answer_saved"])
    degraded = any(one["status"] != "answered" for one in rows)
    outcome = (
        "complete" if rows and answered_count == len(rows) and not degraded
        else "none" if answered_count == 0
        else "partial"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "outcome": outcome,
        "requested_mode": _text(requested_mode, 40),
        "expected_count": len(rows),
        "answered_count": answered_count,
        "participants": rows,
        "actions": actions,
    }


def frozen(value: object) -> dict[str, Any]:
    """Validate and detach a saved participant-outcome projection."""

    if value in (None, {}):
        return {}
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported participant outcome schema")
    outcome = _text(value.get("outcome"), 20)
    if outcome not in OUTCOMES:
        raise ValueError("invalid participant outcome")
    participants = value.get("participants")
    actions = value.get("actions")
    if not isinstance(participants, list) or not isinstance(actions, list):
        raise ValueError("malformed participant outcome")
    rows: list[dict[str, Any]] = []
    for one in participants:
        if not isinstance(one, dict):
            raise ValueError("malformed participant outcome row")
        status = _text(one.get("status"), 40)
        if status not in PARTICIPANT_STATUSES:
            raise ValueError("invalid participant outcome status")
        answer_saved = one.get("answer_saved") is True
        outcome_unknown = one.get("outcome_unknown") is True
        expected_answer_saved = status in {
            "answered", "answered_then_failed", "answered_then_outcome_unknown",
        }
        expected_unknown = status in {
            "outcome_unknown", "answered_then_outcome_unknown",
        }
        if answer_saved != expected_answer_saved or outcome_unknown != expected_unknown:
            raise ValueError("participant outcome status flags do not agree")
        if status == "answered":
            if "repair_allowed" in one or "retry_allowed" in one:
                raise ValueError("answered participant exposes a recovery permission")
        elif outcome_unknown:
            if one.get("retry_allowed") is not False or one.get("repair_allowed") is not False:
                raise ValueError("unknown provider outcome must forbid retry and repair")
        elif one.get("repair_allowed") is not True or "retry_allowed" in one:
            raise ValueError("known provider failure may expose only repair")
        row: dict[str, Any] = {
            **identity(one),
            "status": status,
            "answer_saved": answer_saved,
            "provider_reason": _text(one.get("provider_reason"), 65_536),
            "outcome_unknown": outcome_unknown,
        }
        if one.get("repair_allowed") is True:
            row["repair_allowed"] = True
        if one.get("repair_allowed") is False:
            row["repair_allowed"] = False
        if one.get("retry_allowed") is False:
            row["retry_allowed"] = False
        rows.append(row)
    agent_ids = [one["agent_id"] for one in rows]
    if any(not one for one in agent_ids) or len(set(agent_ids)) != len(agent_ids):
        raise ValueError("participant outcome roster identities are invalid")
    safe_actions: list[dict[str, str]] = []
    for action in actions:
        if not isinstance(action, dict):
            raise ValueError("malformed participant outcome action")
        action_id = _text(action.get("id"), 80)
        if action_id not in {"repair-provider", "inspect-provider-turn"}:
            raise ValueError("unsupported participant outcome action")
        safe_actions.append({
            "id": action_id,
            "agent_id": _text(action.get("agent_id"), 120),
            "route": _text(action.get("route"), 120),
            "label": _text(action.get("label"), 300),
        })
    expected_count = value.get("expected_count")
    answered_count = value.get("answered_count")
    if expected_count != len(rows) or answered_count != sum(
        1 for one in rows if one["answer_saved"]
    ):
        raise ValueError("participant outcome counts do not match its roster")
    degraded = any(one["status"] != "answered" for one in rows)
    semantic_outcome = (
        "complete" if rows and int(answered_count) == len(rows) and not degraded
        else "none" if int(answered_count) == 0
        else "partial"
    )
    if outcome != semantic_outcome:
        raise ValueError("participant outcome does not match its participant states")
    by_agent = {one["agent_id"]: one for one in rows}
    seen_actions: set[tuple[str, str]] = set()
    for action in safe_actions:
        target = by_agent.get(action["agent_id"])
        key = (action["id"], action["agent_id"])
        if target is None or key in seen_actions:
            raise ValueError("participant outcome action target is invalid")
        seen_actions.add(key)
        if action["id"] == "inspect-provider-turn" and not target["outcome_unknown"]:
            raise ValueError("inspect action requires an unknown provider outcome")
        if action["id"] == "repair-provider" and (
            target["outcome_unknown"] or target["status"] == "answered"
        ):
            raise ValueError("repair action does not match its participant state")
    required_actions = {
        (
            "inspect-provider-turn" if one["outcome_unknown"] else "repair-provider",
            one["agent_id"],
        )
        for one in rows if one["status"] != "answered"
    }
    if seen_actions != required_actions:
        raise ValueError("participant outcome recovery actions are incomplete")
    return {
        "schema_version": SCHEMA_VERSION,
        "outcome": outcome,
        "requested_mode": _text(value.get("requested_mode"), 40),
        "expected_count": len(rows),
        "answered_count": int(answered_count),
        "participants": rows,
        "actions": safe_actions,
    }


def notice_text(outcome: dict[str, Any]) -> str:
    """Plain-language transcript rendering for a delivery projection."""

    expected = int(outcome.get("expected_count") or 0)
    answered = int(outcome.get("answered_count") or 0)
    if outcome.get("outcome") == "complete":
        return f"All {expected} requested agents answered this team request."
    missing = [
        str(one.get("name") or "An agent")
        for one in outcome.get("participants", [])
        if isinstance(one, dict) and one.get("status") != "answered"
    ]
    names = ", ".join(missing) or "one or more requested agents"
    need = "needs" if len(missing) == 1 else "need"
    if outcome.get("outcome") == "none":
        return (
            f"Team response unavailable: 0 of {expected} requested agents answered. "
            f"{names} {need} provider attention. Nexus saved this outcome and did not "
            "automatically resend any uncertain provider turn."
        )
    return (
        f"Team response incomplete: {answered} of {expected} requested agents answered. "
        f"{names} {need} provider attention. Nexus kept every successful answer and did "
        "not automatically resend any uncertain provider turn."
    )


def result_fields(outcome: dict[str, Any]) -> dict[str, Any]:
    """Stable response fields shared by relay and collaboration modes."""

    participants = list(outcome.get("participants") or [])
    expected = [
        {key: one[key] for key in ("agent_id", "name", "route")}
        for one in participants
    ]
    answered = [
        {key: one[key] for key in ("agent_id", "name", "route")}
        for one in participants if one.get("answer_saved") is True
    ]
    complete = outcome.get("outcome") == "complete"
    return {
        "status": "complete" if complete else "paused_provider",
        "requires_recovery": not complete,
        "participant_outcome": outcome,
        "expected_participants": expected,
        "answered_participants": answered,
    }
