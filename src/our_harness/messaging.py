"""A shared message board for the agents working on one run.

Agents already hand work to each other along the edges of a graph. That covers
"do this next", but not "I looked at the parser and it caches by file name, so
watch out". This board carries those notes.

The rules are deliberately dull:

- A message is text. Reading one never runs anything.
- An agent may only write to another agent in the same run, or to everyone.
- The board is bounded in count and in size, and it refuses a message that
  would cross a limit rather than quietly dropping an older one.
- Every message is numbered, so a reader can ask for what is new since last time
  and two runs of the same graph read the same thing in the same order.
- Credential material is stripped as a message is written, so a secret one agent
  happens to be holding never reaches the board, the run log, or another agent.
"""

from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .models import HarnessError

MESSAGE_SCHEMA_VERSION = 1
EVERYONE = "everyone"

DEFAULT_MAX_MESSAGES = 200
DEFAULT_MAX_SUBJECT_CHARS = 200
DEFAULT_MAX_BODY_CHARS = 4_000
DEFAULT_MAX_TOTAL_CHARS = 200_000
DEFAULT_INBOX_LIMIT = 20
MAX_INBOX_LIMIT = 100


@dataclass(frozen=True)
class AgentMessage:
    sequence: int
    sender: str
    recipient: str
    subject: str
    body: str
    created_at: float

    @property
    def to_everyone(self) -> bool:
        return self.recipient == EVERYONE

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "from": self.sender,
            "to": self.recipient,
            "subject": self.subject,
            "body": self.body,
            "created_at": self.created_at,
        }


def _text(value: object, label: str, limit: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise HarnessError(f"{label} must be text")
    cleaned = value.strip() if not allow_empty else value
    if not allow_empty and not cleaned:
        raise HarnessError(f"{label} must not be empty")
    if len(cleaned) > limit:
        raise HarnessError(f"{label} must be at most {limit} characters")
    return cleaned


class MessageBoard:
    """Every note the agents in one run have written to each other."""

    def __init__(
        self,
        participants: Iterable[str] = (),
        *,
        max_messages: int = DEFAULT_MAX_MESSAGES,
        max_subject_chars: int = DEFAULT_MAX_SUBJECT_CHARS,
        max_body_chars: int = DEFAULT_MAX_BODY_CHARS,
        max_total_chars: int = DEFAULT_MAX_TOTAL_CHARS,
        clock: Any = time.time,
        redact: Any = None,
    ) -> None:
        if max_messages < 1 or max_body_chars < 1 or max_total_chars < 1 or max_subject_chars < 1:
            raise HarnessError("Message board limits must be positive")
        self.participants = frozenset(str(item) for item in participants if str(item).strip())
        if EVERYONE in self.participants:
            raise HarnessError(f"An agent may not be named {EVERYONE}")
        self.max_messages = int(max_messages)
        self.max_subject_chars = int(max_subject_chars)
        self.max_body_chars = int(max_body_chars)
        self.max_total_chars = int(max_total_chars)
        self.clock = clock
        # Strips credential material as a message is written. Without one the
        # board stores exactly what it was given.
        self.redact = redact if callable(redact) else (lambda value: value)
        self._lock = threading.Lock()
        self._messages: list[AgentMessage] = []
        self._total_chars = 0
        self._sequence = 0

    # -- writing -----------------------------------------------------------

    def post(self, sender: object, recipient: object, subject: object, body: object) -> AgentMessage:
        from_id = _text(sender, "The sender", 128)
        to_id = _text(recipient, "The recipient", 128)
        clean_subject = _text(subject, "The subject", self.max_subject_chars)
        clean_body = _text(body, "The message", self.max_body_chars)
        # Strip secrets before the note exists, not after. A note that never
        # holds a key cannot pass one on, log one, or save one to a checkpoint.
        clean_subject = _text(self.redact(clean_subject), "The subject", self.max_subject_chars)
        clean_body = _text(self.redact(clean_body), "The message", self.max_body_chars)
        if self.participants and from_id not in self.participants:
            raise HarnessError(f"{from_id} is not an agent in this run")
        if to_id != EVERYONE and self.participants and to_id not in self.participants:
            known = ", ".join(sorted(self.participants))
            raise HarnessError(
                f"There is no agent named {to_id} in this run. "
                f"Write to one of these, or to {EVERYONE}: {known}"
            )
        if to_id == from_id:
            raise HarnessError("An agent cannot write to itself. Use its own notes for that.")
        cost = len(clean_subject) + len(clean_body)
        with self._lock:
            if len(self._messages) >= self.max_messages:
                raise HarnessError(
                    f"The agents on this run have already written {self.max_messages} notes, which "
                    "is the limit. Nothing was lost, but no more can be added. Carry on with the "
                    "work and put anything else in your own answer."
                )
            if self._total_chars + cost > self.max_total_chars:
                room = max(0, self.max_total_chars - self._total_chars)
                raise HarnessError(
                    f"The notes on this run already fill {self._total_chars} of "
                    f"{self.max_total_chars} characters. There is room for about {room} more, "
                    "so send a shorter note or leave this one out."
                )
            self._sequence += 1
            message = AgentMessage(
                sequence=self._sequence,
                sender=from_id,
                recipient=to_id,
                subject=clean_subject,
                body=clean_body,
                created_at=float(self.clock()),
            )
            self._messages.append(message)
            self._total_chars += cost
            return message

    # -- reading -----------------------------------------------------------

    def inbox(
        self, reader: object, *, since: int = 0, limit: int = DEFAULT_INBOX_LIMIT
    ) -> tuple[AgentMessage, ...]:
        """Messages written to this reader, or to everyone, after `since`."""

        who = _text(reader, "The reader", 128)
        if self.participants and who not in self.participants:
            raise HarnessError(f"{who} is not an agent in this run")
        if not isinstance(since, int) or isinstance(since, bool) or since < 0:
            raise HarnessError("The since number must be zero or more")
        count = max(1, min(int(limit), MAX_INBOX_LIMIT))
        with self._lock:
            found = [
                message
                for message in self._messages
                if message.sequence > since
                and message.sender != who
                and (message.recipient == who or message.to_everyone)
            ]
        return tuple(found[:count])

    def waiting(self, reader: object, since: int = 0) -> int:
        who = _text(reader, "The reader", 128)
        with self._lock:
            return sum(
                1
                for message in self._messages
                if message.sequence > since
                and message.sender != who
                and (message.recipient == who or message.to_everyone)
            )

    def conversation(self) -> tuple[AgentMessage, ...]:
        with self._lock:
            return tuple(self._messages)

    @property
    def last_sequence(self) -> int:
        with self._lock:
            return self._sequence

    def __len__(self) -> int:
        with self._lock:
            return len(self._messages)

    # -- saving and restoring ---------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema_version": MESSAGE_SCHEMA_VERSION,
                "participants": sorted(self.participants),
                "sequence": self._sequence,
                "messages": [message.to_dict() for message in self._messages],
            }

    @classmethod
    def restore(cls, snapshot: Mapping[str, Any], **limits: Any) -> "MessageBoard":
        if not isinstance(snapshot, Mapping):
            raise HarnessError("A message board snapshot must be an object")
        if snapshot.get("schema_version") != MESSAGE_SCHEMA_VERSION:
            raise HarnessError(f"Message board snapshots must use version {MESSAGE_SCHEMA_VERSION}")
        participants = snapshot.get("participants")
        if not isinstance(participants, list):
            raise HarnessError("A message board snapshot must list its agents")
        board = cls(participants, **limits)
        raw = snapshot.get("messages")
        if not isinstance(raw, list):
            raise HarnessError("A message board snapshot must list its messages")
        restored: list[AgentMessage] = []
        total = 0
        previous = 0
        for item in raw:
            if not isinstance(item, Mapping):
                raise HarnessError("Every stored message must be an object")
            sequence = item.get("sequence")
            if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= previous:
                raise HarnessError("Stored messages must be numbered in order")
            previous = sequence
            message = AgentMessage(
                sequence=sequence,
                sender=_text(item.get("from"), "A stored sender", 128),
                recipient=_text(item.get("to"), "A stored recipient", 128),
                # Stored notes go through the same credential removal as new
                # ones. A file written before a rule existed, or edited by
                # hand, must not be able to bring a secret back into the run.
                subject=board.redact(_text(item.get("subject"), "A stored subject", board.max_subject_chars)),
                body=board.redact(_text(item.get("body"), "A stored message", board.max_body_chars)),
                created_at=float(item.get("created_at") or 0.0),
            )
            restored.append(message)
            total += len(message.subject) + len(message.body)
        stored_sequence = snapshot.get("sequence")
        if not isinstance(stored_sequence, int) or isinstance(stored_sequence, bool) or stored_sequence < previous:
            raise HarnessError("A message board snapshot has a sequence behind its messages")
        if len(restored) > board.max_messages or total > board.max_total_chars:
            raise HarnessError("A message board snapshot is larger than the configured limits")
        board._messages = restored
        board._total_chars = total
        board._sequence = stored_sequence
        return board


def summarize(messages: Sequence[AgentMessage], limit: int = 5) -> str:
    """One short readable line per message, for a log or a report."""

    lines = []
    for message in list(messages)[:limit]:
        lines.append(f"{message.sender} to {message.recipient}: {message.subject}")
    remaining = max(0, len(messages) - limit)
    if remaining:
        lines.append(f"and {remaining} more")
    return "\n".join(lines)


def transcript(messages: Sequence[AgentMessage]) -> list[dict[str, Any]]:
    return [copy.deepcopy(message.to_dict()) for message in messages]
