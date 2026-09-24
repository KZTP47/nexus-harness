"""Durable private messages with dispatch-bound delivery and retry receipts."""

from __future__ import annotations
import copy
import hashlib
import json
import re
from .models import HarnessError

CONTRACT = "goal-directed-messages/v1"
# Per-message and pending-delivery bounds protect the machine and the
# recipients' prompts. The lifetime history bound only protects the durable
# goal record from runaway growth; no real goal reaches it.
MAX_MESSAGE_CHARACTERS = 20_000
MAX_PENDING_CHARACTERS = 240_000
MAX_MESSAGES = 50_000
MAX_HISTORY_CHARACTERS = 50_000_000


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def binding(goal: dict, task: dict) -> str:
    return digest(
        {
            "contract": CONTRACT,
            "goal_id": goal["goal_id"],
            "project_authority_id": goal.get("project_authority_id"),
            "conversation_id": goal.get("conversation_id"),
            "task_id": task["id"],
            "agent_id": task["assigned_agent_id"],
        }
    )


def records(goal: dict, task: dict) -> list[dict]:
    items = task.get("directed_messages", [])
    if not isinstance(items, list) or any(
        one.get("schema_version") != 1
        or one.get("contract") != CONTRACT
        or one.get("binding")
        != binding(goal, {**task, "assigned_agent_id": one.get("agent_id")})
        for one in items
    ):
        raise HarnessError(
            "Saved directed messages have a changed ownership or contract."
        )
    return items


def pending(goal: dict, task: dict) -> list[dict]:
    delivered = int(task.get("directed_messages_delivered") or 0)
    return [
        one
        for one in records(goal, task)
        if one["sequence"] > delivered and not one.get("cancelled")
    ]


def highwater(goal: dict, task: dict) -> int:
    return max((one["sequence"] for one in pending(goal, task)), default=0)


def prompt(goal: dict, task: dict) -> str:
    items = pending(goal, task)
    if any(one["agent_id"] != task["assigned_agent_id"] for one in items):
        raise HarnessError(
            "Pending private messages belong to another recipient; restore the original assignment."
        )
    if not items:
        return ""
    return (
        "\n\nUNREAD USER MESSAGES FOR YOU (exact recipient; respond before finishing)\n"
        + "\n\n".join(one["text"] for one in items)
    )


def submission(goal: dict, payload: dict) -> tuple[dict, str, str, str]:
    task = next(
        (one for one in goal["tasks"] if one["id"] == payload.get("task_id")), None
    )
    if task is None:
        raise HarnessError("Select an existing task for the directed message.")
    agent = payload.get("agent_id") or task["assigned_agent_id"]
    if agent != task["assigned_agent_id"] or agent not in {
        one["id"] for one in goal["agents"]
    }:
        raise HarnessError(
            "The message recipient no longer owns this task; refresh before sending."
        )
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip() or len(text.strip()) > MAX_MESSAGE_CHARACTERS:
        raise HarnessError(
            f"Agent messages require between 1 and {MAX_MESSAGE_CHARACTERS:,} text characters."
        )
    request = payload.get("request_id", "")
    if not isinstance(request, str) or (
        request and not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", request)
    ):
        raise HarnessError("The directed message request ID is invalid.")
    return (
        task,
        text.strip(),
        request,
        digest(
            {
                "goal_id": goal["goal_id"],
                "task_id": task["id"],
                "agent_id": agent,
                "text": text,
            }
        ),
    )


def public_digest(goal_id: str, payload: dict) -> str:
    return digest(
        {
            "goal_id": goal_id,
            **{key: payload[key] for key in ("task_id", "agent_id", "text")},
        }
    )


def receipt(goal: dict, payload: dict) -> dict | None:
    request = payload.get("request_id")
    if request:
        for candidate in goal["tasks"]:
            for one in records(goal, candidate):
                if not one.get("inherited_from") and one["request_id"] == request:
                    supplied = {
                        **payload,
                        "agent_id": payload.get("agent_id")
                        or candidate["assigned_agent_id"],
                    }
                    if one["submission_sha256"] != public_digest(
                        goal["goal_id"], supplied
                    ):
                        raise HarnessError(
                            "This directed message request ID already belongs to different input."
                        )
                    return copy.deepcopy(one["receipt"])
    submission(goal, payload)
    return None


def rejection(goal: dict, payload: dict) -> dict | None:
    """A readable ledger can prove this rejected identity was never accepted."""
    if not all(
        isinstance(payload.get(key), str)
        for key in ("task_id", "agent_id", "text", "request_id")
    ):
        return None
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", payload["request_id"]):
        return None
    if any(
        one["request_id"] == payload["request_id"] and not one.get("inherited_from")
        for task in goal["tasks"]
        for one in records(goal, task)
    ):
        return None
    return {
        "schema_version": 1,
        "accepted": False,
        "request_id": payload["request_id"],
        "goal_id": goal["goal_id"],
        "task_id": payload["task_id"],
        "agent_id": payload["agent_id"],
        "submission_sha256": public_digest(goal["goal_id"], payload),
    }


def accept(goal: dict, payload: dict, *, safe_text: str) -> dict:
    task, text, request, sha = submission(goal, payload)
    existing = receipt(goal, payload)
    if existing:
        return existing
    if task["state"] == "cancelled":
        raise HarnessError(
            "A cancelled task cannot receive new messages; choose active work or fork the goal."
        )
    all_records = [
        one
        for candidate in goal["tasks"]
        for one in records(goal, candidate)
        if not one.get("inherited_from")
    ]
    if (
        len(all_records) >= MAX_MESSAGES
        or sum(len(one["text"]) for one in all_records) + len(text) > MAX_HISTORY_CHARACTERS
    ):
        raise HarnessError(
            "This goal's saved directed-message history is too large to grow safely. "
            "Nothing was discarded; continue in a follow-up goal."
        )
    active_text = sum(
        len(one["text"])
        for candidate in goal["tasks"]
        for one in pending(goal, candidate)
    )
    if active_text + len(safe_text.strip()) > MAX_PENDING_CHARACTERS:
        raise HarnessError(
            "Pending private messages exceed the bounded delivery context. Let the recipients read them before sending more."
        )
    sequence = max((one["sequence"] for one in records(goal, task)), default=0) + 1
    accepted = {
        "schema_version": 1,
        "accepted": True,
        "request_id": request,
        "goal_id": goal["goal_id"],
        "task_id": task["id"],
        "agent_id": task["assigned_agent_id"],
        "submission_sha256": sha,
    }
    task.setdefault("directed_messages", []).append(
        {
            "schema_version": 1,
            "contract": CONTRACT,
            "binding": binding(goal, task),
            "agent_id": task["assigned_agent_id"],
            "sequence": sequence,
            "text": safe_text.strip(),
            "request_id": request,
            "submission_sha256": sha,
            "receipt": accepted,
        }
    )
    return copy.deepcopy(accepted)


def inherit(source: dict, target: dict) -> None:
    for task in target["tasks"]:
        original = next(one for one in source["tasks"] if one["id"] == task["id"])
        items = copy.deepcopy(records(source, original))
        for one in items:
            one["inherited_from"] = one.get("inherited_from") or {
                "goal_id": source["goal_id"],
                "request_id": one["request_id"],
            }
            one["binding"] = binding(
                target, {**task, "assigned_agent_id": one["agent_id"]}
            )
            one.pop("receipt", None)
        if items:
            task["directed_messages"] = items
