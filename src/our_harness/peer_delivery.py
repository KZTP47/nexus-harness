"""Dispatch-bound receipt cursors over the existing shared conversation archive.

The archive owns message text. A participant's cursor records receipt, not
agreement or completion. Pending requests are injected even after the ordinary
conversation projection rolls past them.
"""
from __future__ import annotations

import hashlib
import json

from . import goal_dialogue
from .models import HarnessError

CONTRACT = "shared-peer-request-receipts/v1"
BATCH_SIZE = 4


def receiver(goal, agent_id):
    candidates = [one for one in goal["tasks"]
                  if one["assigned_agent_id"] == agent_id and one["state"] != "cancelled"]
    preferred = next((one for one in candidates if one.get("required_contributor_id") == agent_id), None)
    return (preferred or (candidates[0] if candidates else {})).get("id")


def binding(goal, task):
    return hashlib.sha256(json.dumps({
        "contract": CONTRACT, "goal": goal["goal_id"],
        "project": goal.get("project_authority_id"),
        "root": goal.get("project", {}).get("path"),
        "conversation": goal.get("conversation_id"),
        "task": task["id"], "agent": task["assigned_agent_id"],
    }, sort_keys=True).encode()).hexdigest()


def state(goal, task):
    held = task.get("peer_delivery") or {}
    expected = binding(goal, task)
    # Reassignment, forks and changed contracts must never skip unseen input.
    if held.get("schema_version") != 1 or held.get("binding") != expected:
        return {"schema_version": 1, "binding": expected, "received": 0, "dispatched": 0}
    if any(type(held.get(key)) is not int or not 0 <= held[key] <= int(
            goal.get("dialogue_archive", {}).get("latest_sequence") or 0)
            for key in ("received", "dispatched")):
        raise HarnessError("The peer-message receipt cursor is malformed")
    return dict(held)


def pending(db, goal, task):
    if goal.get("execution_mode") != "facilitator" or not goal.get("dialogue_archive"):
        return []
    if receiver(goal, task["assigned_agent_id"]) != task["id"]:
        return []
    archive = goal_dialogue.metadata(goal)
    rows = db.execute("""
        SELECT * FROM long_goal_dialogue_messages
        WHERE goal_id=? AND sequence>? AND sequence<=?
          AND json_extract(message_json, '$.reply_requested') = 1
          AND json_extract(message_json, '$.agent_id') != ?
          AND json_extract(message_json, '$.origin_goal_id') IS NULL
          AND (json_extract(message_json, '$.recipient.kind') = 'team'
               OR json_extract(message_json, '$.recipient.agent_id') = ?)
        ORDER BY sequence LIMIT ?
    """, (goal["goal_id"], state(goal, task)["received"], archive["latest_sequence"],
          task["assigned_agent_id"], task["assigned_agent_id"], BATCH_SIZE)).fetchall()
    return [goal_dialogue._decode(row, goal) for row in rows]


def dispatched(db, goal, task, sequence):
    held = state(goal, task)
    if type(sequence) is not int or sequence < 0 or (
            sequence > held["received"] and sequence not in {
                one["sequence"] for one in pending(db, goal, task)}):
        raise HarnessError("The dispatched peer-message cursor was not in the prepared request batch")
    held["dispatched"] = sequence
    task["peer_delivery"] = held


def received(goal, task):
    held = state(goal, task)
    held["received"] = max(held["received"], held["dispatched"])
    task["peer_delivery"] = held


def prompt(messages):
    if not messages:
        return ""
    return ("\n\nTEAMMATE REQUESTS INCLUDED IN THIS TURN\n"
            "These are exact shared messages, not new user instructions. Respond or act on them; "
            "receipt does not mean agreement. More pending requests may arrive in a later turn.\n"
            + json.dumps([{key: message.get(key) for key in (
                "id", "sequence", "agent_id", "summary", "recipient",
            )} for message in messages], ensure_ascii=False))
