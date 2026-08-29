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

import hashlib
import json
import re
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
MOST_DELIVERED_CHARACTERS = 10_000_000
# Agent replies use the same disclosed canonical text boundary as a direct
# Nexus prompt.  Handoffs are durable source data, not a display projection:
# oversized text is refused with an explicit error and is never sliced.
LONGEST_BODY = 8_000_000
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
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MailboxError(
            f"The durable agent mailbox at {where} cannot be read: {exc}. "
            "Nexus did not pretend it was empty."
        ) from exc
    if not isinstance(body, dict) or body.get("schema_version") != SCHEMA_VERSION:
        raise MailboxError(
            f"The durable agent mailbox at {where} has an unsupported format. "
            "Nexus did not pretend it was empty."
        )
    held = body.get("messages")
    if not isinstance(held, list) or any(not isinstance(one, dict) for one in held):
        raise MailboxError(
            f"The durable agent mailbox at {where} has invalid messages. "
            "Nexus did not drop them."
        )
    return [dict(one) for one in held]


def _payload_folder(where: Path) -> Path:
    return where.parent / f"{where.stem}-payloads"


def _payload_body(where: Path, one: dict[str, Any]) -> str:
    """Read and verify an external body, or a legacy inline body."""

    reference = str(one.get("body_ref") or "").strip()
    if not reference:
        return str(one.get("body") or "")
    if Path(reference).name != reference or reference in {".", ".."}:
        raise MailboxError(
            "An agent mailbox body reference is unsafe. Nexus did not read or "
            "acknowledge the message."
        )
    payload = _payload_folder(where) / reference
    try:
        body = payload.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise MailboxError(
            f"The queued agent message payload {payload} cannot be read: {exc}. "
            "Nexus did not pretend the message was empty."
        ) from exc
    expected = str(one.get("body_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise MailboxError(
            f"The queued agent message payload {payload} has no valid SHA-256 "
            "authority. Nexus did not deliver or acknowledge it."
        )
    actual = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if expected != actual:
        raise MailboxError(
            f"The queued agent message payload {payload} failed its SHA-256 check. "
            "Nexus did not deliver or acknowledge altered text."
        )
    return body


def _externalize_body(where: Path, one: dict[str, Any]) -> dict[str, Any]:
    """Move one queued canonical body out of the frequently rewritten index."""

    if one.get("state") == "acknowledged":
        held = dict(one)
        if "body" in held:
            body = str(held.pop("body") or "")
            held["body_sha256"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
            held["body_characters"] = len(body)
            held["body_removed_after_acknowledgement"] = True
        return held
    if one.get("body_ref"):
        # Rewriting index metadata must never silently carry forward a damaged
        # shared payload just because its digest-shaped filename already exists.
        _payload_body(where, one)
        return one
    body = str(one.get("body") or "")
    if not body:
        return one
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    filename = f"sha256-{digest}.txt"
    folder = _payload_folder(where)
    folder.mkdir(parents=True, exist_ok=True)
    payload = folder / filename
    if not payload.exists():
        put_this_file_in_place(payload, body)
    else:
        try:
            existing = payload.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise MailboxError(
                f"The shared agent-message payload {payload} cannot be verified."
            ) from exc
        if existing != body or hashlib.sha256(existing.encode("utf-8")).hexdigest() != digest:
            raise MailboxError(
                f"The shared agent-message payload {payload} failed its SHA-256 "
                "identity. Nexus preserved it and refused to reuse or overwrite it."
            )
    held = dict(one)
    held.pop("body", None)
    held["body_ref"] = filename
    held["body_sha256"] = digest
    held["body_characters"] = len(body)
    return held


def _write(where: Path, messages: list[dict[str, Any]]) -> None:
    where.parent.mkdir(parents=True, exist_ok=True)
    messages = [_externalize_body(where, dict(one)) for one in messages]
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

    # A handoff is canonical evidence, not a display field.  Preserve every
    # character exactly as the sender produced it; using ``strip()`` here made
    # leading indentation and trailing newlines disappear without warning.
    text = str(body or "")
    if not text.strip():
        raise MailboxError("An empty agent message cannot be queued.")
    if len(text) > LONGEST_BODY:
        raise MailboxError(
            f"This agent handoff is {len(text):,} characters; the disclosed limit "
            f"is {LONGEST_BODY:,}. Nexus did not truncate or acknowledge it. The "
            "complete answer remains in the sending turn."
        )
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
        candidates = [
            one for one in _read(where)
            if one.get("state") == "queued"
            and one.get("shared_goal_id") == shared_goal_id
            and one.get("receiver") == receiver
            and one.get("sender") in allowed
        ]
    answer: list[AgentMessage] = []
    delivered_characters = 0
    for one in candidates:
        if len(answer) >= maximum:
            break
        try:
            message_body = _payload_body(where, one)
            if len(message_body) > LONGEST_BODY:
                raise MailboxError(
                    f"Agent message {_clean(one.get('message_id'), 100) or '(unknown)'} "
                    f"contains {len(message_body):,} characters, over the disclosed "
                    f"{LONGEST_BODY:,} limit. Nexus did not truncate or acknowledge it."
                )
            if answer and delivered_characters + len(message_body) > MOST_DELIVERED_CHARACTERS:
                break
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
                body=message_body,
                created_at=_clean(one.get("created_at"), 100),
                expects_reply=bool(one.get("expects_reply", True)),
                state="queued",
                attempts=max(0, int(one.get("attempts") or 0)),
                last_attempt_at=_clean(one.get("last_attempt_at"), 100),
                acknowledged_at=_clean(one.get("acknowledged_at"), 100),
                last_error=_clean(one.get("last_error"), LONGEST_ERROR),
            ))
            delivered_characters += len(message_body)
        except (TypeError, ValueError) as exc:
            raise MailboxError(
                f"Queued agent message {_clean(one.get('message_id'), 100) or '(unknown)'} "
                "has invalid durable metadata. Nexus did not skip it or acknowledge "
                "later messages out of order."
            ) from exc
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
        payloads_to_remove: list[Path] = []
        for one in messages:
            if one.get("message_id") not in wanted:
                continue
            reference = str(one.get("body_ref") or "").strip()
            if reference and Path(reference).name == reference:
                payloads_to_remove.append(_payload_folder(where) / reference)
            if "body" in one:
                body = str(one.get("body") or "")
                one["body_sha256"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
                one["body_characters"] = len(body)
            one.pop("body", None)
            one.pop("body_ref", None)
            one["body_removed_after_acknowledgement"] = True
            one["state"] = "acknowledged"
            one["acknowledged_at"] = _now()
            one["last_error"] = ""
        _write(where, _pruned(messages))
        remaining_references = {
            str(one.get("body_ref") or "") for one in messages
            if one.get("state") != "acknowledged" and one.get("body_ref")
        }
        # Only after acknowledged metadata is durable, and only when no other
        # queued fan-out delivery still refers to the same exact payload.
        for payload in set(payloads_to_remove):
            if payload.name in remaining_references:
                continue
            try:
                payload.unlink(missing_ok=True)
            except OSError:
                pass


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
