"""Durable, private delivery between agents working on one shared goal.

An answer passed directly from one in-memory loop to the next is easy to lose:
the receiving subscription can time out, its CLI can restart, or the desktop
app can close between the two turns.  This mailbox gives those handoffs an
identity and keeps them until the receiving agent has successfully answered
after seeing them.

The format deliberately contains no account identity, executable path, token,
or provider diagnostic.  The message text has already passed through the
harness redactor before it arrives here.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .models import HarnessError
from .safety import put_this_file_in_place


SCHEMA_VERSION = 1
MOST_MESSAGES = 2_000
MOST_DELIVERED_AT_ONCE = 50
LONGEST_BODY = 4_000
LONGEST_ERROR = 500


class MailboxError(HarnessError):
    """The mailbox could not safely accept or identify a message."""


@dataclass
class AgentMessage:
    message_id: str
    thread_id: str
    shared_goal_id: str
    sender: str
    sender_name: str
    receiver: str
    receiver_name: str
    project: str
    where: str
    body: str
    created_at: str
    expects_reply: bool = True
    state: str = "queued"
    attempts: int = 0
    last_attempt_at: str = ""
    acknowledged_at: str = ""
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_lock = threading.RLock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clean(value: object, limit: int = 100) -> str:
    return " ".join(str(value or "").split())[:limit]


def _read(where: Path) -> list[dict[str, Any]]:
    try:
        body = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(body, dict) or body.get("schema_version") != SCHEMA_VERSION:
        return []
    held = body.get("messages")
    return [dict(one) for one in held if isinstance(one, dict)] if isinstance(held, list) else []


def _write(where: Path, messages: list[dict[str, Any]]) -> None:
    where.parent.mkdir(parents=True, exist_ok=True)
    put_this_file_in_place(where, json.dumps({
        "schema_version": SCHEMA_VERSION,
        "messages": messages,
    }, indent=2) + "\n")


def _pruned(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bound the history without silently discarding undelivered work."""

    if len(messages) <= MOST_MESSAGES:
        return messages
    queued = [one for one in messages if one.get("state") != "acknowledged"]
    acknowledged = [one for one in messages if one.get("state") == "acknowledged"]
    if len(queued) > MOST_MESSAGES:
        raise MailboxError(
            "The agent mailbox is full of undelivered messages. Let the receiving "
            "agents catch up before starting another run."
        )
    return acknowledged[-(MOST_MESSAGES - len(queued)):] + queued


def enqueue(
    where: Path,
    *,
    shared_goal_id: str,
    sender: str,
    sender_name: str,
    receiver: str,
    receiver_name: str,
    project: str,
    project_name: str,
    body: str,
    expects_reply: bool = True,
    thread_id: str = "",
) -> AgentMessage:
    """Queue one handoff and return its durable identity."""

    text = str(body or "").strip()[:LONGEST_BODY]
    if not text:
        raise MailboxError("An empty agent message cannot be queued.")
    required = {
        "shared goal": shared_goal_id,
        "sender": sender,
        "receiver": receiver,
        "project": project,
    }
    missing = [name for name, value in required.items() if not _clean(value)]
    if missing:
        raise MailboxError(f"An agent message is missing its {', '.join(missing)}.")
    message_id = uuid.uuid4().hex
    message = AgentMessage(
        message_id=message_id,
        thread_id=_clean(thread_id, 100) or message_id,
        shared_goal_id=_clean(shared_goal_id, 100),
        sender=_clean(sender),
        sender_name=_clean(sender_name),
        receiver=_clean(receiver),
        receiver_name=_clean(receiver_name),
        project=_clean(project),
        where=_clean(project_name),
        body=text,
        created_at=_now(),
        expects_reply=bool(expects_reply),
    )
    with _lock:
        messages = _read(where)
        messages.append(message.to_dict())
        _write(where, _pruned(messages))
    return message


def pending(
    where: Path,
    *,
    shared_goal_id: str,
    receiver: str,
    allowed_senders: Iterable[str],
    limit: int = MOST_DELIVERED_AT_ONCE,
) -> list[AgentMessage]:
    """Read queued messages oldest first without acknowledging them."""

    allowed = {_clean(one) for one in allowed_senders}
    maximum = max(1, min(int(limit), MOST_DELIVERED_AT_ONCE))
    with _lock:
        selected = [
            one for one in _read(where)
            if one.get("state") == "queued"
            and one.get("shared_goal_id") == shared_goal_id
            and one.get("receiver") == receiver
            and one.get("sender") in allowed
        ][:maximum]
    answer: list[AgentMessage] = []
    for one in selected:
        try:
            answer.append(AgentMessage(
                message_id=_clean(one.get("message_id"), 100),
                thread_id=_clean(one.get("thread_id"), 100),
                shared_goal_id=_clean(one.get("shared_goal_id"), 100),
                sender=_clean(one.get("sender")),
                sender_name=_clean(one.get("sender_name")),
                receiver=_clean(one.get("receiver")),
                receiver_name=_clean(one.get("receiver_name")),
                project=_clean(one.get("project")),
                where=_clean(one.get("where")),
                body=str(one.get("body") or "")[:LONGEST_BODY],
                created_at=_clean(one.get("created_at"), 100),
                expects_reply=bool(one.get("expects_reply", True)),
                state="queued",
                attempts=max(0, int(one.get("attempts") or 0)),
                last_attempt_at=_clean(one.get("last_attempt_at"), 100),
                acknowledged_at=_clean(one.get("acknowledged_at"), 100),
                last_error=_clean(one.get("last_error"), LONGEST_ERROR),
            ))
        except (TypeError, ValueError):
            continue
    return answer


def attempted(where: Path, message_ids: Iterable[str], error: str = "") -> None:
    """Record a delivery attempt; a failed one remains queued for replay."""

    wanted = {_clean(one, 100) for one in message_ids}
    if not wanted:
        return
    with _lock:
        messages = _read(where)
        for one in messages:
            if one.get("message_id") not in wanted or one.get("state") != "queued":
                continue
            one["attempts"] = max(0, int(one.get("attempts") or 0)) + 1
            one["last_attempt_at"] = _now()
            # Provider diagnostics can contain an email address, tenant name,
            # executable path, or echoed secret. The detailed, redacted reason
            # already lives with the turn; the durable mailbox needs only the
            # fact that this delivery must be retried.
            one["last_error"] = (
                "The receiving assistant did not answer; queued for retry."
                if error else ""
            )
        _write(where, messages)


def acknowledge(where: Path, message_ids: Iterable[str]) -> None:
    """Acknowledge only after the receiver successfully answered."""

    wanted = {_clean(one, 100) for one in message_ids}
    if not wanted:
        return
    with _lock:
        messages = _read(where)
        for one in messages:
            if one.get("message_id") not in wanted:
                continue
            one["state"] = "acknowledged"
            one["acknowledged_at"] = _now()
            one["last_error"] = ""
        _write(where, _pruned(messages))


def status(where: Path) -> dict[str, int]:
    """Small counts suitable for the UI; never message text or identities."""

    with _lock:
        messages = _read(where)
    queued = [one for one in messages if one.get("state") == "queued"]
    return {
        "queued": len(queued),
        "acknowledged": len(messages) - len(queued),
        "retrying": len([one for one in queued if int(one.get("attempts") or 0) > 0]),
    }
