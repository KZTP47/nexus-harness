"""Authenticated public speech, independent of bounded goal/event projections.

The goal snapshot authenticates the archive head and coverage. Each immutable
row authenticates its exact text and previous row, so pagination cannot silently
turn missing, reordered, or modified speech into an apparently complete history.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import sqlite3
from typing import Any

from .models import HarnessError
from .runtime_integrity import mac


SCHEMA_VERSION = 1
CONTRACT = {
    "schema_version": SCHEMA_VERSION,
    "storage": "authenticated-append-only-public-speech-v1",
    "projection": "newest-complete-messages-with-scoped-range-retrieval-v1",
    "migration": "authenticated-retained-events-and-exact-snapshot-v1",
}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _binding(document: dict[str, Any]) -> str:
    return _digest({key: document.get(key, "") for key in (
        "goal_id", "authority_key", "project_authority_id", "conversation_id",
    )})


def empty(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_fingerprint_sha256": _digest(CONTRACT),
        "binding_sha256": _binding(document),
        "count": 0, "latest_sequence": 0, "head_sequence": 0, "head_sha256": "",
        "coverage": {"status": "complete", "reason": "", "unavailable_before_sequence": 0,
                     "unavailable_ranges": []},
    }


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS long_goal_dialogue_messages(
          goal_id TEXT NOT NULL,
          sequence INTEGER NOT NULL,
          message_id TEXT NOT NULL,
          message_json TEXT NOT NULL,
          message_sha256 TEXT NOT NULL,
          integrity_mac TEXT NOT NULL,
          PRIMARY KEY(goal_id,sequence),
          UNIQUE(goal_id,message_id),
          FOREIGN KEY(goal_id) REFERENCES long_goals(goal_id) ON DELETE CASCADE
        );
    """)


def metadata(document: dict[str, Any]) -> dict[str, Any]:
    value = document.get("dialogue_archive")
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION \
            or value.get("contract_fingerprint_sha256") != _digest(CONTRACT) \
            or value.get("binding_sha256") != _binding(document):
        raise HarnessError("Shared conversation archive has an unsupported contract or mismatched project binding")
    return value


def _decode(row: sqlite3.Row, document: dict[str, Any]) -> dict[str, Any]:
    raw = str(row["message_json"])
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    material = [document["goal_id"], int(row["sequence"]), str(row["message_id"]), raw, digest]
    if digest != row["message_sha256"] or not hmac.compare_digest(
        str(row["integrity_mac"]), mac("long-horizon-dialogue-message-v1", material),
    ):
        raise HarnessError("Shared conversation archive failed integrity verification")
    value = json.loads(raw)
    if value.get("schema_version") != SCHEMA_VERSION \
            or value.get("goal_id") != document["goal_id"] \
            or value.get("sequence") != row["sequence"] \
            or value.get("id") != row["message_id"]:
        raise HarnessError("Shared conversation archive has mismatched message identity")
    return value


def append(db: sqlite3.Connection, document: dict[str, Any], message: dict[str, Any]) -> None:
    held = metadata(document)
    existing = db.execute(
        "SELECT * FROM long_goal_dialogue_messages WHERE goal_id=? AND message_id=?",
        (document["goal_id"], message["id"]),
    ).fetchone()
    if existing is not None:
        previous = _decode(existing, document)
        if previous.get("summary") != message.get("summary"):
            raise HarnessError("Shared conversation message identity was reused for different text")
        return
    sequence = int(message["sequence"])
    if sequence <= int(held["head_sequence"]):
        raise HarnessError("Shared conversation archive cannot reorder earlier messages")
    value = {
        **copy.deepcopy(message), "schema_version": SCHEMA_VERSION,
        "goal_id": document["goal_id"],
        "previous_sequence": int(held["head_sequence"]),
        "previous_sha256": str(held["head_sha256"]),
    }
    raw = _json(value)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    material = [document["goal_id"], sequence, value["id"], raw, digest]
    db.execute(
        "INSERT INTO long_goal_dialogue_messages VALUES(?,?,?,?,?,?)",
        (*material[:3], raw, digest, mac("long-horizon-dialogue-message-v1", material)),
    )
    held.update({"count": int(held["count"]) + 1, "head_sequence": sequence,
                 "latest_sequence": max(int(held["latest_sequence"]), sequence), "head_sha256": digest})


def _legacy_events(db: sqlite3.Connection, document: dict[str, Any]) -> list[dict[str, Any]]:
    rows = db.execute("SELECT * FROM long_goal_events WHERE goal_id=? ORDER BY seq", (document["goal_id"],))
    previous = str(document.get("event_floor_previous_sha256") or "")
    sequence = int(document.get("event_floor_seq") or 1)
    public = []
    for row in rows:
        raw = str(row["event_json"])
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        material = [document["goal_id"], int(row["seq"]), str(row["event_id"]), str(row["type"]), raw, digest]
        event = json.loads(raw)
        if digest != row["event_sha256"] or not hmac.compare_digest(
            str(row["integrity_mac"]), mac("long-horizon-event-v1", material),
        ) or event.get("previous_sha256") != previous or event.get("seq") != sequence:
            raise HarnessError("Shared conversation migration found unauthenticated or missing source events")
        if event["type"] in {"provider_acknowledged", "goal_steered", "agent_messaged", "interrupt_resolved"}:
            public.append(event)
        previous, sequence = digest, sequence + 1
    if sequence - 1 != int(document.get("event_seq") or 0) \
            or previous != str(document.get("event_head_sha256") or ""):
        raise HarnessError("Shared conversation migration source event head does not match its goal")
    return public


def migrate(db: sqlite3.Connection, document: dict[str, Any]) -> bool:
    """Recover only authenticated public text that an older version still has."""
    if "dialogue_archive" in document:
        metadata(document)
        return False
    legacy_dialogue_sequence = int((document.get("dialogue") or {}).get("sequence") or 0)
    legacy_event_seq = int(document.get("event_seq") or 0)
    public_events = _legacy_events(db, document)
    events = [one for one in public_events if one["type"] in {"provider_acknowledged", "goal_steered"}]
    targeted_events = [one for one in public_events if one["type"] in {"agent_messaged", "interrupt_resolved"}]
    snapshots = {
        int(one["sequence"]): {**copy.deepcopy(one), "legacy_dialogue_sequence": int(one["sequence"])}
        for one in (document.get("dialogue") or {}).get("messages", [])
    }
    latest = max(int((document.get("dialogue") or {}).get("sequence") or 0), len(events))
    first = max(1, latest - len(events) + 1)
    recovered: dict[int, dict[str, Any]] = {}
    epoch = int(document.get("objective_epoch") or 1)
    # Work backwards to retain objective epochs even when earlier events rolled over.
    for sequence, event in reversed(list(enumerate(events, first))):
        payload = event.get("payload") or {}
        snapshot = snapshots.get(sequence)
        is_user = event["type"] == "goal_steered"
        body = payload.get("text" if is_user else "summary", "")
        # _bounded_json historically replaced large event payloads with a JSON
        # excerpt. It is not the speaker's message and must never become one.
        if not payload.get("truncated") and isinstance(body, str) and body:
            message = {
                "id": "legacy-" + event["event_id"], "sequence": sequence,
                "agent_id": "" if is_user else event.get("agent_id", ""),
                "task_id": event.get("task_id", ""), "summary": body,
                "action": "steer" if is_user else payload.get("action", "work"),
                "phase": "user" if is_user else payload.get("phase", "action"),
                "objective_epoch": epoch,
                "recipient": payload.get("summary_delivery") or {"kind": "team", "name": "the team"},
                "at_ms": event.get("at_ms", 0),
            }
        elif snapshot:
            message = copy.deepcopy(snapshot)
        else:
            if is_user:
                epoch = max(1, epoch - 1)
            continue
        if snapshot:
            # This snapshot stores full Unicode strings rather than the event
            # byte projection; keep its stable identity and exact words.
            message.update(snapshot)
        message.update({"source_goal_event_id": event["event_id"],
                        "source_goal_event_seq": event["seq"], "source_goal_event_type": event["type"]})
        recovered[sequence] = message
        if is_user:
            epoch = max(1, epoch - 1)
    for sequence, snapshot in snapshots.items():
        recovered.setdefault(sequence, {
            **snapshot, "source_goal_event_id": "", "source_goal_event_seq": 0,
            "source_goal_event_type": "goal_steered" if snapshot.get("phase") == "user" else "provider_acknowledged",
        })
    ordered = [{**value, "_legacy_sequence": sequence} for sequence, value in recovered.items()]
    for event in targeted_events:
        payload = event.get("payload") or {}
        body = payload.get("answer" if event["type"] == "interrupt_resolved" else "text", "")
        if payload.get("truncated") or not isinstance(body, str) or not body:
            ordered.append({"_missing": True, "source_goal_event_seq": event["seq"]})
        else:
            ordered.append(_user_message(document, event, payload))
    # Snapshot-only messages necessarily predate the retained event floor.
    # Retained targeted messages were absent from the old dialogue counter;
    # merge them by their authenticated event order, not that old counter.
    ordered.sort(key=lambda one: (
        1 if one.get("source_goal_event_seq") else 0,
        int(one.get("source_goal_event_seq") or one.get("_legacy_sequence") or 0),
    ))
    recovered = {}
    extra, last_sequence = 0, 0
    for value in ordered:
        legacy_sequence = int(value.pop("_legacy_sequence", 0))
        if not legacy_sequence:
            extra += 1
        sequence = max(last_sequence + 1, legacy_sequence + extra)
        last_sequence = sequence
        if value.pop("_missing", False):
            continue
        value["sequence"] = sequence
        recovered[sequence] = value
    latest = max(latest + len(targeted_events), last_sequence)
    document["dialogue_archive"] = empty(document)
    for sequence in sorted(recovered):
        append(db, document, recovered[sequence])
    held = metadata(document)
    held["latest_sequence"] = latest
    dialogue = document.get("dialogue")
    if isinstance(dialogue, dict) and dialogue:
        by_id = {one["id"]: one for one in recovered.values()}
        dialogue["messages"] = [copy.deepcopy(by_id.get(one["id"], one)) for one in dialogue.get("messages", [])]
        dialogue["sequence"] = latest
    missing = []
    cursor = 1
    for sequence in sorted(recovered):
        if sequence > cursor:
            missing.append({"first": cursor, "last": sequence - 1})
        cursor = sequence + 1
    if cursor <= latest:
        missing.append({"first": cursor, "last": latest})
    unresolved_recipient = any(one.get("visibility") == "operator_only" for one in recovered.values())
    if missing or int(document.get("event_floor_seq") or 1) > 1 or unresolved_recipient:
        held["coverage"] = {
            "status": "partial_legacy",
            "reason": "An older engine discarded some public history or recipient metadata before this archive existed; only surviving authenticated messages were recovered. Messages with an unknown original recipient are available to the user only.",
            "unavailable_before_sequence": missing[0]["last"] + 1 if missing and missing[0]["first"] == 1 else 0,
            "unavailable_ranges": missing,
        }
    # Freeze the pre-migration cardinality and event boundary. Together with
    # each snapshot's original ordinal, these authenticate a conservative UI
    # match to already projected legacy messages without collapsing repeats.
    held["coverage"].update({
        "legacy_dialogue_sequence": legacy_dialogue_sequence,
        "legacy_event_seq": legacy_event_seq,
    })
    return True


def _user_message(
    document: dict[str, Any], event: dict[str, Any], payload: dict[str, Any], *, current_recipient: bool = False,
) -> dict[str, Any]:
    target_task = next((one for one in document.get("tasks", []) if one["id"] == event.get("task_id")), {})
    # Historical task ownership can change. A stored event's explicit recipient
    # wins; only a new authenticated write may resolve its current task owner.
    recipient_id = str(event.get("agent_id") or (target_task.get("assigned_agent_id") if current_recipient else "") or "")
    unresolved = bool(not recipient_id and event.get("task_id") and not current_recipient)
    recipient = next((one for one in document.get("agents", []) if one["id"] == recipient_id), {})
    return {
        "id": "user-event-" + event["event_id"],
        "sequence": 0, "agent_id": "", "task_id": event.get("task_id", ""),
        "action": "answer" if event["type"] == "interrupt_resolved" else "message",
        "phase": "user", "summary": str(payload.get("answer") or payload.get("text") or ""),
        "objective_epoch": int(document.get("objective_epoch") or 1), "at_ms": event.get("at_ms", 0),
        "recipient": {"kind": "unknown" if unresolved else "agent" if recipient_id else "team", "agent_id": recipient_id,
                      "name": "original recipient (unavailable)" if unresolved else str(recipient.get("name") or recipient_id or "the team")},
        "visibility": "operator_only" if unresolved else "agent_only" if recipient_id else "team",
        "source_goal_event_id": event["event_id"], "source_goal_event_seq": event["seq"],
        "source_goal_event_type": event["type"],
    }


def record_user_event(db: sqlite3.Connection, document: dict[str, Any], event: dict[str, Any], payload: object) -> None:
    value = _user_message(document, event, payload if isinstance(payload, dict) else {}, current_recipient=True)
    if not value["summary"]:
        return
    value["sequence"] = int(metadata(document)["latest_sequence"]) + 1
    append(db, document, value)
    if isinstance(document.get("dialogue"), dict) and document["dialogue"]:
        document["dialogue"]["sequence"] = value["sequence"]


def page(
    db: sqlite3.Connection, document: dict[str, Any], after: int = 0, limit: int = 100,
    *, message_id: str = "", offset: int = 0, character_limit: int = 96_000, viewer_agent_id: str = "",
) -> dict[str, Any]:
    held = metadata(document)
    if after < 0 or offset < 0 or limit < 1 or character_limit < 1:
        raise HarnessError("Shared conversation offsets must be nonnegative and limits positive")
    if offset and not message_id:
        raise HarnessError("A shared conversation character offset requires an exact message_id")
    limit, character_limit = min(100, limit), min(96_000, character_limit)
    # Authenticate the head even for an empty or out-of-range request.
    head = db.execute("SELECT * FROM long_goal_dialogue_messages WHERE goal_id=? ORDER BY sequence DESC LIMIT 1", (document["goal_id"],)).fetchone()
    if head is None:
        if held["count"] or held["head_sequence"] or held["head_sha256"]:
            raise HarnessError("Shared conversation archive is missing its authenticated head")
    else:
        _decode(head, document)
        if head["sequence"] != held["head_sequence"] or head["message_sha256"] != held["head_sha256"]:
            raise HarnessError("Shared conversation archive head does not match its goal")
    if message_id:
        rows = db.execute("SELECT * FROM long_goal_dialogue_messages WHERE goal_id=? AND message_id=?", (document["goal_id"], message_id)).fetchall()
        if not rows:
            raise HarnessError("That message is not in this goal's shared conversation")
    else:
        rows = db.execute("SELECT * FROM long_goal_dialogue_messages WHERE goal_id=? AND sequence>? ORDER BY sequence LIMIT ?", (document["goal_id"], after, limit + 1)).fetchall()
    messages, used, next_sequence = [], 0, after
    for row in rows[:limit]:
        value = _decode(row, document)
        previous = db.execute("SELECT * FROM long_goal_dialogue_messages WHERE goal_id=? AND sequence<? ORDER BY sequence DESC LIMIT 1", (document["goal_id"], row["sequence"])).fetchone()
        if previous:
            _decode(previous, document)
        if value["previous_sequence"] != (int(previous["sequence"]) if previous else 0) \
                or value["previous_sha256"] != (str(previous["message_sha256"]) if previous else ""):
            raise HarnessError("Shared conversation archive is missing or reordering messages")
        if viewer_agent_id and (value.get("visibility") == "operator_only" or (
            value.get("visibility") == "agent_only"
            and (value.get("recipient") or {}).get("agent_id") != viewer_agent_id
        )):
            if message_id:
                raise HarnessError("That message is not addressed to this participant")
            next_sequence = int(value["sequence"])
            continue
        body = str(value["summary"])
        if offset > len(body):
            raise HarnessError("Shared conversation character offset exceeds the message length")
        if messages and used + len(body) > character_limit:
            break
        chunk = body[offset:offset + character_limit - used]
        used += len(chunk)
        value.update({"summary": chunk, "offset": offset, "total_characters": len(body),
                      "has_more_characters": offset + len(chunk) < len(body),
                      "next_offset": offset + len(chunk),
                      "summary_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()})
        messages.append(value)
        next_sequence = int(value["sequence"])
        if value["has_more_characters"]:
            break
    has_more = bool((messages and messages[-1]["has_more_characters"])
                    or (not message_id and next_sequence < int(held["head_sequence"])))
    return {"schema_version": SCHEMA_VERSION, "goal_id": document["goal_id"], "messages": messages,
            "next": next_sequence, "has_more": has_more, "coverage": copy.deepcopy(held["coverage"]),
            "total_messages": int(held["count"]), "latest_sequence": int(held["latest_sequence"])}


def clone(db: sqlite3.Connection, source: dict[str, Any], target: dict[str, Any]) -> None:
    target["dialogue_archive"] = empty(target)
    cursor = 0
    while True:
        result = page(db, source, cursor)
        for value in result["messages"]:
            # Reassemble a long public user message before signing the clone.
            while value["has_more_characters"]:
                chunk = page(db, source, message_id=value["id"], offset=value["next_offset"])["messages"][0]
                value["summary"] += chunk["summary"]
                value["next_offset"] = chunk["next_offset"]
                value["has_more_characters"] = chunk["has_more_characters"]
            for key in ("offset", "total_characters", "has_more_characters", "next_offset", "summary_sha256"):
                value.pop(key, None)
            value.update({"origin_goal_id": value["goal_id"],
                          "origin_goal_event_id": value.get("source_goal_event_id", ""),
                          "source_goal_event_id": "", "source_goal_event_seq": 0})
            append(db, target, value)
        if not result["has_more"]:
            break
        cursor = result["next"]
    target["dialogue_archive"]["coverage"] = copy.deepcopy(metadata(source)["coverage"])
    target["dialogue_archive"]["latest_sequence"] = metadata(source)["latest_sequence"]
