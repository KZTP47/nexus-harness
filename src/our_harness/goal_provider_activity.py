"""Bind public activity to one admitted provider effect and durable chat archive."""

from __future__ import annotations

import hashlib
import json
import time

from . import goal_dialogue
from .models import HarnessError
from .provider_activity import FINGERPRINT, TEXT_LIMIT, SUMMARY_CONTRACT


def recorder(store, goal_id: str, task: dict):
    """Freeze identity after dispatch admission, before starting the subprocess."""
    goal = store.get(goal_id)
    current = next(one for one in goal["tasks"] if one["id"] == task["id"])
    effect = str(current.get("provider_effect_id") or "")
    lease = str(task.get("lease_id") or "")
    agent = str(current["assigned_agent_id"])
    if not effect or current.get("lease_id") != lease or current.get("provider_effect_state") != "dispatched":
        raise HarnessError("Public activity requires an admitted provider dispatch")

    def record(activity: dict) -> None:
        if activity.get("schema_version") != 1 or activity.get("contract_fingerprint") != FINGERPRINT \
                or activity.get("kind") not in {"message", "tool", "notice", "reasoning_summary"} \
                or not isinstance(activity.get("id"), str) or not 0 < len(activity["id"]) <= 200:
            raise HarnessError("Unsupported public provider activity contract")
        if activity["kind"] == "reasoning_summary" and activity.get("summary_contract") != SUMMARY_CONTRACT:
            raise HarnessError("Unsupported public reasoning summary")
        if activity["kind"] == "tool" and (activity.get("status") not in {"requested", "finished", "failed"}
                or not isinstance(activity.get("name"), str)):
            raise HarnessError("Unsupported public provider tool activity")
        value = store.redactor.value(activity)
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if len(raw) > TEXT_LIMIT * 2:
            raise HarnessError("Public provider activity exceeded its storage bound")
        identity = hashlib.sha256((effect + "\0" + value["id"]).encode()).hexdigest()
        message_id = hashlib.sha256((identity + "\0" + str(value.get("status", "message"))).encode()).hexdigest()

        def change(document, db):
            active = next(one for one in document["tasks"] if one["id"] == task["id"])
            if active.get("lease_id") != lease or active.get("provider_effect_id") != effect \
                    or active.get("assigned_agent_id") != agent:
                raise HarnessError("Stale provider activity cannot enter another dispatch or agent's chat")
            previous = db.execute("SELECT * FROM long_goal_dialogue_messages WHERE goal_id=? AND message_id=?",
                                  (goal_id, message_id)).fetchone()
            if previous is not None:
                held = goal_dialogue._decode(previous, document)
                if held.get("provider_activity") != value:
                    raise HarnessError("Provider activity identity was reused for different content")
                return
            words = str(value.get("text") or "") if value["kind"] in {"message", "notice", "reasoning_summary"} else raw
            if not words.strip():
                raise HarnessError("Public provider activity has no displayable content")
            event = store._event(db, document, "provider_public_activity", task_id=task["id"],
                                 agent_id=agent, payload={"message_id": message_id, "effect_id": effect,
                                                        "kind": value["kind"]})
            goal_dialogue.append(db, document, {
                "id": message_id, "sequence": int(document["dialogue_archive"]["latest_sequence"]) + 1,
                "agent_id": agent, "task_id": task["id"], "phase": "provider_activity",
                "action": "observation", "summary": words, "provider_activity": value,
                "activity_id": identity, "effect_id": effect, "visibility": "operator_only",
                "recipient": {"kind": "user"}, "at_ms": int(time.time() * 1000),
                "source_goal_event_id": event["event_id"], "source_goal_event_seq": event["seq"],
                "source_goal_event_type": event["type"],
            })
        store._mutate(goal_id, change)

    return record
