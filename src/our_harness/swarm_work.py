"""Explicit, provider-neutral collaboration and project-file work from the board.

Ordinary chat stays conversational. Explicit actions can request collaboration
or project work directly, while normal Send may route an unmistakable team/file
goal into the same path after the user confirms mutation. Provider text is never
treated as a command; file proposals cross the confined, baseline-checked
transaction boundary owned by Nexus.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import weakref
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from . import chat as chat_lab
from . import cancellation
from .changes import FileTransaction, atomic_write, file_sha256, sha256_bytes
from .collaboration_ledger import CollaborationLedger
from .config import LoadedConfig
from .models import ChangePlan, HarnessError, ResponseFormat
from .safety import confined_path
from .swarm import SwarmError, may_they_talk


Progress = Callable[[str, str], None]
LiveTurn = Callable[[dict[str, Any]], None]
_active_mutation_sagas: weakref.WeakValueDictionary[str, Any] = weakref.WeakValueDictionary()


def _report(progress: Progress | None, stage: str, detail: str = "") -> None:
    if progress:
        progress(stage, detail)


def _show_turn(live_turn: LiveTurn | None, turn: dict[str, Any]) -> None:
    if live_turn:
        live_turn(turn)


def _pause_provider_failure(
    ledger: CollaborationLedger,
    failed: dict[str, Any] | list[dict[str, Any]],
    stage: str,
    *,
    checkpoint: dict[str, Any] | None = None,
    cause: Exception | None = None,
    mutation_root: Path | None = None,
    transaction_ids: list[str] | None = None,
    mutation_saga: _MutationSaga | None = None,
) -> None:
    """Stop orchestration without turning a transport failure into agent speech."""

    failed_agents = failed if isinstance(failed, list) else [failed]
    names = [str(one.get("name") or "An agent") for one in failed_agents]
    state: dict[str, Any] = {
        "stage": stage,
        "status": "paused",
        "failed_agents": [
            {
                "id": str(one.get("id") or ""),
                "name": str(one.get("name") or "An agent"),
                "route": str(one.get("who") or ""),
                "failure_code": "provider_turn_failed",
            }
            for one in failed_agents
        ],
    }
    state.update(checkpoint or {})
    if mutation_saga is not None:
        state["mutation_recovery"] = mutation_saga.compensate("provider_failure")
    elif mutation_root is not None and transaction_ids:
        state["mutation_recovery"] = _rollback_transactions(mutation_root, transaction_ids)
    ledger.record_state("provider_transport_failure", state)
    remaining = [
        f"Reconnect or reconcile {name}'s provider turn before resuming."
        for name in names
    ]
    report = (
        "Nexus paused this collaboration because "
        + ", ".join(names)
        + " could not complete a provider turn. The failure was not counted as "
          "agent speech, reasoning progress, or a completed round."
    )
    ledger.finish(
        report,
        complete=False,
        stopped_because="provider_unavailable",
        remaining=remaining,
    )
    if cause is not None:
        raise SwarmError(report) from cause
    raise SwarmError(report)


def _continuation_turn(label: str, instruction: str) -> str:
    """A current user turn for an evolving collaboration phase.

    The original request remains the authoritative goal in dynamic context,
    but sending it again as the active user message makes every provider treat
    settled questions as newly asked. Later rounds need a new turn that says
    what changed and what the agent must do now.
    """

    return (
        f"NEXUS CURRENT TURN — {label}\n"
        f"{instruction.strip()}\n\n"
        "The original user request is supplied separately as authoritative goal "
        "context, not as a question to answer again from the beginning. Continue "
        "from the latest actual conversation and project state. Treat completed "
        "informational subgoals as closed: consider them silently and do not mention "
        "their answers again unless they became invalid or now block the work. Do not "
        "even state that a closed subgoal was previously answered or satisfied. Focus "
        "this response only on new work, changed facts, disagreements, remaining "
        "requirements, and blockers for this turn."
    )


PLAN_FORMAT = ResponseFormat("nexus_board_contribution_v1", {
    "type": "object",
    "properties": {
        "contribution": {"type": "string", "maxLength": 8000},
        "message_to_lead": {"type": "string", "maxLength": 4000},
        "needs_files": {
            "type": "array", "maxItems": 12,
            "items": {"type": "string", "maxLength": 240},
        },
    },
    "required": ["contribution", "message_to_lead", "needs_files"],
    "additionalProperties": False,
})

WORK_FORMAT = ResponseFormat("nexus_board_file_work_v1", {
    "type": "object",
    "properties": {
        "reply": {"type": "string", "maxLength": 12000},
        "changes": {
            "type": "array", "maxItems": 12,
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "maxLength": 240},
                    "content": {"type": "string", "maxLength": 500000},
                    "reason": {"type": "string", "maxLength": 1000},
                },
                "required": ["path", "content", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["reply", "changes"],
    "additionalProperties": False,
})

DISCUSSION_FORMAT = ResponseFormat("nexus_board_goal_discussion_v1", {
    "type": "object",
    "properties": {
        "message": {"type": "string", "maxLength": 12000},
        "goal_complete": {"type": "boolean"},
        "remaining": {
            "type": "array", "maxItems": 12,
            "items": {"type": "string", "maxLength": 500},
        },
        "progress": {
            "type": "array", "maxItems": 24,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "maxLength": 160},
                    "state": {"type": "string", "maxLength": 160},
                    "evidence": {"type": "string", "maxLength": 500},
                },
                "required": ["id", "state", "evidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["message", "goal_complete", "remaining"],
    "additionalProperties": False,
})

PLAN_REVIEW_FORMAT = ResponseFormat("nexus_board_plan_review_v1", {
    "type": "object",
    "properties": {
        "contribution": {"type": "string", "maxLength": 8000},
        "message_to_lead": {"type": "string", "maxLength": 4000},
        "needs_files": {
            "type": "array", "maxItems": 12,
            "items": {"type": "string", "maxLength": 240},
        },
        "ready_to_execute": {"type": "boolean"},
        "remaining": {
            "type": "array", "maxItems": 12,
            "items": {"type": "string", "maxLength": 500},
        },
        "progress": {
            "type": "array", "maxItems": 24,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "maxLength": 160},
                    "state": {"type": "string", "maxLength": 160},
                    "evidence": {"type": "string", "maxLength": 500},
                },
                "required": ["id", "state", "evidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "contribution", "message_to_lead", "needs_files",
        "ready_to_execute", "remaining",
    ],
    "additionalProperties": False,
})

WORK_VERIFICATION_FORMAT = ResponseFormat("nexus_board_work_verification_v1", {
    "type": "object",
    "properties": {
        "goal_complete": {"type": "boolean"},
        "feedback": {"type": "string", "maxLength": 8000},
        "remaining": {
            "type": "array", "maxItems": 12,
            "items": {"type": "string", "maxLength": 500},
        },
    },
    "required": ["goal_complete", "feedback", "remaining"],
    "additionalProperties": False,
})

MAX_FINITE_ROUNDS = 10_000
_PROGRESS_WORDS = re.compile(r"[^\W_]+", re.UNICODE)
_PROGRESS_STOP_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "been", "but", "by", "for",
    "from", "has", "have", "i", "if", "in", "is", "it", "its", "my", "not",
    "of", "on", "or", "our", "so", "that", "the", "their", "them", "then",
    "there", "this", "to", "was", "we", "were", "will", "with", "you", "your",
})
_PROGRESS_ALIASES = {
    # Providers naturally alternate nouns and verbs for the same unresolved
    # action.  Canonicalize those forms before measuring overlap.  Concrete
    # anchors such as filenames, checkpoint numbers, and issue identifiers are
    # deliberately left untouched, so real work advancing to a new target is
    # still a new state.
    **{
        word: "change"
        for word in (
            "apply", "applied", "applying", "create", "created", "creating",
            "creation", "implement", "implemented", "implementing",
            "implementation", "make", "made", "modify", "modified",
            "modifying", "write", "writing", "written",
        )
    },
    **{
        word: "verify"
        for word in (
            "check", "checked", "checking", "correct", "return", "returned",
            "returns", "test", "tested", "testing", "tests", "validate",
            "validated", "validating", "validation", "value", "values",
        )
    },
    **{
        word: "required"
        for word in (
            "allow", "allowed", "allows", "must", "need", "needed", "needs",
            "require", "required", "requires", "requiring",
        )
    },
    **{word: "file" for word in ("files", "js", "script", "scripts")},
}


def user_round_limit(value: object) -> int | None:
    """Validate a user-selected per-phase round ceiling.

    ``None`` is deliberately unlimited. It removes only the numeric ceiling;
    the progress guard still stops a conversation that is demonstrably cycling.
    """

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise SwarmError("The agent round limit must be a whole number or unlimited.")
    if value < 1 or value > MAX_FINITE_ROUNDS:
        raise SwarmError(
            f"The agent round limit must be from 1 to {MAX_FINITE_ROUNDS}, or unlimited."
        )
    return value


def _round_numbers(limit: int | None):
    number = 1
    while limit is None or number <= limit:
        yield number
        number += 1


def _progress_terms(values: object) -> frozenset[str]:
    if isinstance(values, list):
        text = " ".join(str(one) for one in values)
    else:
        text = str(values or "")
    return frozenset(
        _PROGRESS_ALIASES.get(word, word)
        for word in _PROGRESS_WORDS.findall(text.casefold())
        if (len(word) > 1 or word.isdigit()) and word not in _PROGRESS_STOP_WORDS
    )


def _progress_terms_match(left: frozenset[str], right: frozenset[str]) -> bool:
    if not left or not right:
        return left == right
    # Remaining-work prose is short and naturally paraphrased. A conservative
    # overlap threshold treats cosmetic wording as the same state while a new
    # filename, decision, fact, or subgoal makes the state novel.
    overlap = len(left & right) / max(1, min(len(left), len(right)))
    return overlap >= 0.78


def _canonical_progress_state(
    agent_id: str,
    complete: bool,
    failed: bool,
    value: dict[str, Any],
    files: object = None,
) -> tuple[str, str]:
    """Return engine-owned canonical state, never a similarity score over prose.

    Provider wording is deliberately excluded. Until providers supply durable
    structured evidence IDs, only completion state and exact confined file
    identities can prove movement between rounds.
    """

    canonical_files = sorted({
        str(path).strip().replace("\\", "/").casefold()
        for path in (files if isinstance(files, list) else [])
        if isinstance(path, str) and path.strip()
    })
    state = {
        "complete": bool(complete),
        "failed": bool(failed),
        "files": canonical_files,
        "progress": sorted(
            [
                {
                    "id": str(item.get("id") or ""),
                    "state": str(item.get("state") or ""),
                    "evidence": str(item.get("evidence") or ""),
                }
                for item in value.get("progress", [])
                if isinstance(item, dict) and str(item.get("id") or "")
            ],
            key=lambda item: (item["id"], item["state"], item["evidence"]),
        ),
    }
    return agent_id, hashlib.sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class _ProgressGuard:
    """Notice stable or oscillating actionable state without policing duration."""

    def __init__(self) -> None:
        self.recent: list[
            tuple[tuple[str, str], ...]
        ] = []
        self.repeat_hits = 0

    def stalled(
        self,
        state: tuple[tuple[str, str], ...],
    ) -> bool:
        repeated = any(
            previous == state
            for previous in self.recent[-3:]
        )
        self.repeat_hits = self.repeat_hits + 1 if repeated else 0
        self.recent.append(state)
        if len(self.recent) > 4:
            self.recent.pop(0)
        # A single repeat may be a useful retry. Two repeat hits prove either a
        # stable loop (A,A,A) or a two-state oscillation (A,B,A,B).
        return self.repeat_hits >= 2

_DIRECT_COLLABORATION = re.compile(
    r"\b(?:work\s+together|collaborat(?:e|ion|ively)?|ask\s+(?:the\s+)?(?:other|connected)\s+agents?"
    r"|both\s+of\s+you|all\s+of\s+you|team\s+up|peer\s+review|second\s+opinion|compare\s+your\s+answers)\b",
    re.IGNORECASE,
)
_IMPLICIT_COLLABORATION = re.compile(
    r"\b(?:independent|different|multiple|several|competing)\s+"
    r"(?:opinions?|perspectives?|approaches?|reviews?)\b"
    r"|\b(?:assess|analy[sz]e|evaluate|review|check|audit|verify)\b.{0,100}"
    r"\b(?:perspectives?|trade[ -]?offs?|risks?|implementation|security|testing|design)\b",
    re.IGNORECASE | re.DOTALL,
)
_REFUSE_COLLABORATION = re.compile(
    r"\b(?:do\s+not|don't|without)\b.{0,60}\b(?:collaborat|other\s+agents?|team|peer|together)\b",
    re.IGNORECASE | re.DOTALL,
)
_PROJECT_CHANGE = re.compile(
    r"\b(?:add|build|change|create|delete|edit|fix|implement|make|modify|move|"
    r"refactor|remove|rename|replace|update|write)\b.{0,100}"
    r"\b(?:code|file|files|folder|project|repo|repository|script|scripts|test|tests)\b"
    r"|\b(?:code|file|files|folder|project|repo|repository|script|scripts|test|tests)\b"
    r".{0,100}\b(?:add|build|change|create|delete|edit|fix|implement|make|modify|"
    r"move|refactor|remove|rename|replace|update|write)\b",
    re.IGNORECASE | re.DOTALL,
)
_PROJECT_SCOPE = re.compile(
    r"\b(?:code|file|files|folder|folders|game|project|repo|repository|script|scripts|test|tests|"
    r"html|css|javascript|typescript|python|app|application|website|webapp)\b",
    re.IGNORECASE,
)
_DIRECT_RELAY = re.compile(
    r"\b(?:ask|contact|forward|message|pass|relay|say|send|speak|talk|tell)\b",
    re.IGNORECASE,
)


def _agent(board: dict[str, Any], agent_id: str) -> dict[str, Any]:
    for one in board.get("agents", []):
        if isinstance(one, dict) and one.get("id") == agent_id:
            return one
    raise SwarmError("That agent is not on the board any more. Refresh the board.")


def _participants(
    board: dict[str, Any], lead: dict[str, Any], peer_id: str = ""
) -> list[dict[str, Any]]:
    found = [lead]
    for one in board.get("agents", []):
        if (
            isinstance(one, dict)
            and one.get("id") != lead.get("id")
            and one.get("ready")
            and one.get("who")
            and may_they_talk(board, str(lead.get("id")), str(one.get("id")))
        ):
            found.append(one)
    if peer_id:
        chosen = next(
            (one for one in found if str(one.get("id")) == peer_id), None
        )
        if chosen is None:
            raise SwarmError(
                "The other agent in this chat is not ready or no longer connected."
            )
        return [lead, chosen]
    return found[:chat_lab.MOST_AT_ONCE]


def board_context(
    board: dict[str, Any], agent_id: str,
    peer_id: str = "", project_id: str = "",
) -> str:
    """Truthful board identity for every normal chat provider."""

    lead = _agent(board, agent_id)
    project_ids = {
        str(line.get("project")) for line in board.get("works_on", [])
        if isinstance(line, dict) and line.get("agent") == agent_id
    }
    projects = [
        one for one in board.get("projects", [])
        if isinstance(one, dict) and str(one.get("id")) in project_ids
    ]
    peers = [one for one in _participants(board, lead, peer_id) if one is not lead]
    active = next(
        (one for one in projects if str(one.get("id")) == project_id), None
    )
    return (
        "NEXUS BOARD IDENTITY (authoritative harness data)\n"
        f"You are the board agent {lead.get('name')!r}, using provider route {lead.get('who')!r}.\n"
        f"Your stated job: {lead.get('job') or '(none written)'}.\n"
        "Assigned projects: "
        + (", ".join(
            f"{one.get('name')} ({one.get('path')})" for one in projects
        ) or "none")
        + "\nThis chat's active project: "
        + (
            f"{active.get('name')} ({active.get('path')})"
            if active else "none selected; this chat may not change project files"
        )
        + "\nConnected agents Nexus may relay to: "
        + (", ".join(
            f"{one.get('name')} (route {one.get('who')})" for one in peers
        ) or "none")
        + "\nNo relay has happened merely because this list is present."
    )


def automatic_mode(
    config: LoadedConfig,
    board: dict[str, Any],
    agent_id: str,
    text: str,
    progress: Progress | None = None,
    peer_id: str = "",
    project_id: str = "",
) -> dict[str, str]:
    """Choose direct chat, goal collaboration, or confirmed project work.

    Routing is a local control-plane decision. It must never be sent through a
    user-visible provider web chat: doing that exposed Nexus's JSON classifier
    reply in the provider conversation and could race the real user turn. Clear
    collaboration/file language and bounded expertise hints route locally;
    everything else stays ordinary chat, the least expansive action.
    """

    lead = _agent(board, agent_id)
    _report(
        progress, "Deciding whether connected agents should help",
        "Nexus is checking the request against the ready agents on the green communication lines."
    )
    asked = str(text or "").strip()
    if _REFUSE_COLLABORATION.search(asked):
        _report(progress, f"Waiting for {lead.get('name')}", "The request says to keep this chat direct.")
        return {
            "mode": "chat",
            "reason": "The request explicitly says not to involve other agents.",
        }
    if _PROJECT_CHANGE.search(asked):
        _report(
            progress, "Project work selected",
            "The request explicitly asks to change project files; Nexus will require confirmation before applying anything."
        )
        return {
            "mode": "work",
            "reason": "The request explicitly asks the connected team to change project files.",
        }
    peers = [
        one for one in _participants(board, lead, peer_id)
        if one.get("id") != agent_id
    ]
    if not peers:
        _report(progress, f"Waiting for {lead.get('name')}", "No ready connected agent is available.")
        return {
            "mode": "chat",
            "reason": "No ready connected agent is available, so Nexus kept this as ordinary chat.",
        }
    names = [str(one.get("name") or "").strip() for one in peers]
    named_peer = next(
        (name for name in names if name and name.casefold() in asked.casefold()), ""
    )
    if named_peer and _DIRECT_RELAY.search(asked):
        _report(
            progress, "Directed connected-agent relay selected",
            f"Nexus will ask {lead.get('name')} first, relay its real message to {named_peer}, then return the real reply."
        )
        return {
            "mode": "relay",
            "reason": f"The request asks the selected agent to relay a message to {named_peer}.",
        }
    if named_peer or _DIRECT_COLLABORATION.search(asked):
        _report(progress, "Connected-agent collaboration selected", "The request explicitly asks agents to confer.")
        return {
            "mode": "collaborate",
            "reason": (
                f"The request names connected agent {named_peer}." if named_peer
                else "The request explicitly asks the agents to work together."
            ),
        }

    # A pair chat is an explicit user choice of two participants.  Treating
    # it like an ordinary one-agent chat made the second agent effectively
    # decorative: casual prompts ("say hi", "what do you think?") never
    # crossed the selected communication line.  The pair itself is the
    # collaboration intent; only an explicit refusal above opts out.
    if peer_id:
        peer = next(
            (one for one in peers if str(one.get("id") or "") == str(peer_id)),
            None,
        )
        peer_name = str(peer.get("name") or "the connected agent") if peer else "the connected agent"
        _report(
            progress, "Connected-agent collaboration selected",
            f"This pair chat explicitly includes {peer_name}; Nexus will relay the turn so both agents can respond.",
        )
        return {
            "mode": "collaborate",
            "reason": f"This selected pair chat includes {peer_name}, so Nexus relayed the turn to both agents.",
            "pair_chat_implicit_collaboration": True,
        }

    use_team = bool(_IMPLICIT_COLLABORATION.search(asked))
    reason = (
        "The request asks for analysis that benefits from independent connected-agent perspectives."
        if use_team else
        "The request does not explicitly need another agent, so Nexus kept the conversation direct."
    )
    _report(
        progress,
        "Connected-agent collaboration selected" if use_team else f"Waiting for {lead.get('name')}",
        reason,
    )
    return {
        "mode": "collaborate" if use_team else "chat",
        "reason": reason,
    }


def mentions_project_scope(text: str) -> bool:
    """Whether an explicit Work action actually names project/file subject matter."""

    return bool(_PROJECT_SCOPE.search(str(text or "")))


def _contribution(
    one: dict[str, Any], answer: dict[str, Any], phase: str, text: str,
    *, recipient_id: str = "", recipient_name: str = "Team deliberation",
) -> dict[str, Any]:
    return {
        "speaker_id": one.get("id"),
        "speaker_name": one.get("name"),
        "speaker_route": one.get("who"),
        "recipient_id": recipient_id,
        "recipient_name": recipient_name,
        "text": text,
        "milliseconds": answer.get("milliseconds", answer.get("_milliseconds", 0)),
        "model": answer.get("model", answer.get("_model", "")),
        "phase": phase,
    }


def _actual_conversation(contributions: list[dict[str, Any]]) -> str:
    transcript = "\n\n".join(
        f"{one.get('speaker_name') or 'An agent'} ({one.get('speaker_route') or 'unknown route'}):\n"
        f"{one.get('text') or ''}"
        for one in contributions
    )
    return transcript[-160_000:]


class _MutationSaga:
    """Durable coordinator journal over the legacy per-change transactions."""

    FOLDER = Path(".harness") / "swarm-mutation-sagas"

    def __init__(self, root: Path, saga_id: str) -> None:
        self.root = root.resolve()
        self.path = confined_path(
            self.root, self.FOLDER / f"{saga_id}.json", allow_control=True
        )
        self.value: dict[str, Any] = {
            "schema_version": 1,
            "saga_id": saga_id,
            "owner_pid": os.getpid(),
            "owner_identity": self._owner_identity(os.getpid()),
            "phase": "active",
            "transactions": [],
            "created_at": int(time.time()),
        }
        _active_mutation_sagas[saga_id] = self
        self._write()

    def _write(self) -> None:
        atomic_write(
            self.path,
            (json.dumps(self.value, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )

    def prepare(self, transaction_id: str) -> None:
        self.value["transactions"].append({
            "transaction_id": transaction_id, "phase": "prepared"
        })
        self.value["updated_at"] = int(time.time())
        self._write()

    def applied(self, transaction_id: str) -> None:
        entry = next(
            one for one in self.value["transactions"]
            if one["transaction_id"] == transaction_id
        )
        entry["phase"] = "applied"
        self.value["updated_at"] = int(time.time())
        self._write()

    def compensate(self, reason: str) -> dict[str, Any]:
        self.value["phase"] = "compensating"
        self.value["compensation_reason"] = reason
        self.value["updated_at"] = int(time.time())
        self._write()
        rolled_back: list[str] = []
        for entry in reversed(self.value["transactions"]):
            transaction_id = str(entry["transaction_id"])
            manifest_path = confined_path(
                self.root,
                Path(".harness") / "backups" / transaction_id / "manifest.json",
                allow_missing=True,
                allow_control=True,
            )
            if not manifest_path.is_file() and entry.get("phase") == "prepared":
                entry["phase"] = "compensated"
                entry["reason"] = "transaction_manifest_was_never_created"
                rolled_back.append(transaction_id)
                self.value["updated_at"] = int(time.time())
                self._write()
                continue
            try:
                FileTransaction(self.root).rollback(transaction_id)
            except HarnessError as exc:
                entry["phase"] = "conflict"
                entry["reason"] = str(exc)
                self.value["phase"] = "rollback_conflict"
                self.value["updated_at"] = int(time.time())
                self._write()
                _active_mutation_sagas.pop(str(self.value["saga_id"]), None)
                return {
                    "status": "rollback_conflict",
                    "rolled_back_transaction_ids": rolled_back,
                    "conflict_transaction_id": transaction_id,
                    "reason": str(exc),
                    "saga_id": self.value["saga_id"],
                }
            entry["phase"] = "compensated"
            rolled_back.append(transaction_id)
            self.value["updated_at"] = int(time.time())
            self._write()
        self.value["phase"] = "compensated"
        self.value["completed_at"] = int(time.time())
        self._write()
        _active_mutation_sagas.pop(str(self.value["saga_id"]), None)
        return {
            "status": "rolled_back",
            "rolled_back_transaction_ids": rolled_back,
            "saga_id": self.value["saga_id"],
        }

    def complete(self, verification_status: str) -> None:
        self.value["phase"] = "committed"
        self.value["verification_status"] = verification_status
        self.value["completed_at"] = int(time.time())
        self._write()
        _active_mutation_sagas.pop(str(self.value["saga_id"]), None)

    @staticmethod
    def _owner_identity(pid: int) -> str:
        if os.name == "nt":
            import ctypes

            process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not process:
                return ""
            try:
                times = [(ctypes.c_ulong * 2)() for _ in range(4)]
                if not ctypes.windll.kernel32.GetProcessTimes(
                    process, *(ctypes.byref(one) for one in times)
                ):
                    return ""
                return str((int(times[0][1]) << 32) | int(times[0][0]))
            finally:
                ctypes.windll.kernel32.CloseHandle(process)
        try:
            fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
            return fields[21]
        except (OSError, IndexError):
            return ""

    @classmethod
    def _owner_alive(cls, pid: object, expected_identity: object = "") -> bool:
        if not isinstance(pid, int) or pid <= 0:
            return False
        if os.name == "nt":
            import ctypes

            process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not process:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not ctypes.windll.kernel32.GetExitCodeProcess(
                    process, ctypes.byref(exit_code)
                ):
                    return False
                alive = exit_code.value == 259  # STILL_ACTIVE
                return alive and (
                    not expected_identity
                    or cls._owner_identity(pid) == str(expected_identity)
                )
            finally:
                ctypes.windll.kernel32.CloseHandle(process)
        try:
            os.kill(pid, 0)
        except (OSError, ValueError):
            return False
        return not expected_identity or cls._owner_identity(pid) == str(expected_identity)

    @classmethod
    def recover_orphans(cls, root: Path) -> list[dict[str, Any]]:
        folder = confined_path(root.resolve(), cls.FOLDER, allow_control=True)
        if not folder.is_dir():
            return []
        recovered: list[dict[str, Any]] = []
        for path in sorted(folder.glob("*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                recovered.append({"status": "journal_damaged", "path": path.name})
                continue
            if not isinstance(value, dict):
                continue
            if value.get("phase") == "rollback_conflict":
                recovered.append({
                    "status": "rollback_conflict",
                    "saga_id": str(value.get("saga_id") or path.stem),
                    "reason": "A prior compensation conflict still requires reconciliation.",
                })
                continue
            if value.get("phase") not in {"active", "compensating"}:
                continue
            saga_id = str(value.get("saga_id") or "")
            owner_pid = value.get("owner_pid")
            locally_active = (
                owner_pid == os.getpid() and saga_id in _active_mutation_sagas
            )
            remotely_active = (
                owner_pid != os.getpid()
                and cls._owner_alive(owner_pid, value.get("owner_identity"))
            )
            if locally_active or remotely_active:
                continue
            saga = object.__new__(cls)
            saga.root = root.resolve()
            saga.path = path
            saga.value = value
            recovered.append(saga.compensate("process_crash_recovery"))
        return recovered


def _rollback_transactions(root: Path, transaction_ids: list[str]) -> dict[str, Any]:
    """Compensate a legacy multi-transaction run without overwriting conflicts."""

    rolled_back: list[str] = []
    for transaction_id in reversed(list(transaction_ids)):
        try:
            FileTransaction(root).rollback(transaction_id)
        except HarnessError as exc:
            return {
                "status": "rollback_conflict",
                "rolled_back_transaction_ids": rolled_back,
                "conflict_transaction_id": transaction_id,
                "reason": str(exc),
            }
        rolled_back.append(transaction_id)
    return {
        "status": "rolled_back",
        "rolled_back_transaction_ids": rolled_back,
    }


def _shared_context(
    ledger: CollaborationLedger,
    one: dict[str, Any],
    state: dict[str, Any] | None = None,
) -> str:
    return "\n\n" + ledger.projection_for(
        str(one.get("id") or ""), shared_state=state
    )


def _ack_shared(ledger: CollaborationLedger, one: dict[str, Any]) -> None:
    ledger.acknowledge(str(one.get("id") or ""))


def _share_turn(
    ledger: CollaborationLedger,
    contribution: dict[str, Any],
    state: dict[str, Any] | None = None,
) -> None:
    ledger.record_contribution(contribution, state=state)


def _remaining(value: dict[str, Any]) -> list[str]:
    raw = value.get("remaining")
    return [str(one).strip()[:500] for one in raw if str(one).strip()] if isinstance(raw, list) else []


def _schema_problem(value: object, schema: dict[str, Any], path: str = "result") -> str:
    """Validate the small strict JSON-schema subset used by board agents."""

    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            return f"{path} must be an object"
        required = schema.get("required", [])
        missing = [str(one) for one in required if one not in value]
        if missing:
            return f"{path} is missing {', '.join(missing)}"
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = [str(one) for one in value if one not in properties]
            if extra:
                return f"{path} contains unexpected {', '.join(extra)}"
        for name, child in properties.items():
            if name in value:
                problem = _schema_problem(value[name], child, f"{path}.{name}")
                if problem:
                    return problem
        return ""
    if kind == "array":
        if not isinstance(value, list):
            return f"{path} must be an array"
        maximum = schema.get("maxItems")
        if isinstance(maximum, int) and len(value) > maximum:
            return f"{path} has too many items"
        child = schema.get("items", {})
        for index, item in enumerate(value):
            problem = _schema_problem(item, child, f"{path}[{index}]")
            if problem:
                return problem
        return ""
    if kind == "string":
        if not isinstance(value, str):
            return f"{path} must be text"
        maximum = schema.get("maxLength")
        if isinstance(maximum, int) and len(value) > maximum:
            return f"{path} is too long"
        return ""
    if kind == "boolean" and not isinstance(value, bool):
        return f"{path} must be true or false"
    return ""


def _decode(
    answer: dict[str, Any], label: str, response_format: ResponseFormat
) -> dict[str, Any]:
    raw = str(answer.get("text") or "").strip().lstrip("\ufeff")
    # API providers can enforce response_format natively; consumer web chats
    # cannot. ChatGPT and Gemini occasionally wrap an otherwise exact JSON
    # object in a Markdown JSON fence. Accept that presentation wrapper while
    # retaining the same strict schema boundary below. Arbitrary prose around a
    # payload is still rejected rather than silently reinterpreted as control.
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", raw, re.IGNORECASE | re.DOTALL)
    if fenced:
        raw = fenced.group(1).strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HarnessError(f"{label} did not return the structured collaboration result Nexus requested") from exc
    if not isinstance(value, dict):
        raise HarnessError(f"{label} returned the wrong collaboration result shape")
    problem = _schema_problem(value, response_format.schema)
    if problem:
        raise HarnessError(
            f"{label} returned an invalid {response_format.name} result: {problem}"
        )
    return value


def _decode_with_one_web_repair(
    config: LoadedConfig,
    one: dict[str, Any],
    answer: dict[str, Any],
    response_format: ResponseFormat,
    ledger: CollaborationLedger,
    conversation_key: str,
    prefer_existing_conversation: bool,
) -> dict[str, Any]:
    """Strictly decode, giving a stateful consumer web model one format-only correction."""

    label = str(one.get("name") or "The agent")
    try:
        return _decode(answer, label, response_format)
    except HarnessError:
        route = str(one.get("who") or "")
        if not route.startswith("web:"):
            raise
        corrected = chat_lab.ask_once(
            config,
            route,
            _continuation_turn(
                "STRUCTURED FORMAT CORRECTION",
                "Correct your immediately preceding answer into the required JSON schema. "
                "Return only the JSON object, preserve the same substantive answer, escape every "
                "JSON string correctly, replace embedded double quotation marks inside string "
                "values with single quotation marks, and use forward slashes for any Windows paths.",
            ),
            context=(
                "FORMAT CORRECTION ONLY\n"
                "Your immediately preceding provider reply was delivered, but it was not valid "
                "JSON for the required Nexus schema. Do not redo the task or add commentary. "
                "Correct that answer once."
            ),
            response_format=response_format,
            conversation_key=conversation_key,
            prefer_existing_conversation=prefer_existing_conversation,
        )
        _ack_shared(ledger, one)
        return _decode(corrected, label, response_format)


def relay(
    config: LoadedConfig,
    board: dict[str, Any],
    agent_id: str,
    text: str,
    attachments: object = None,
    progress: Progress | None = None,
    live_turn: LiveTurn | None = None,
    peer_id: str = "",
    project_id: str = "",
    filed_as: str = "",
    conversation_key: str = "",
    prefer_existing_conversation: bool = False,
) -> dict[str, Any]:
    """Relay one real lead message to one peer, then return the real reply.

    A directed relay is deliberately sequential. Broadcasting the user's raw
    wording to every provider made peers answer identity questions addressed to
    the lead and made it impossible to tell whether any inter-agent message had
    actually crossed the Nexus boundary.
    """

    lead = _agent(board, agent_id)
    participants = _participants(board, lead, peer_id)
    peers = [one for one in participants if one.get("id") != lead.get("id")]
    named = [
        one for one in peers
        if str(one.get("name") or "").strip()
        and str(one.get("name") or "").casefold() in str(text or "").casefold()
    ]
    if len(named) == 1:
        peer = named[0]
    elif len(peers) == 1:
        peer = peers[0]
    else:
        raise SwarmError("Name exactly one connected agent for this directed relay.")

    ledger = CollaborationLedger(
        config,
        str(lead.get("who") or ""),
        filed_as or str(lead.get("name") or ""),
    ).begin(text, [lead, peer], mode="directed_relay")

    public, provider_files, attachment_text = chat_lab.keep_attachments(
        config, str(lead.get("who") or ""), attachments,
        filed_as or str(lead.get("name") or ""),
    )

    def identity(one: dict[str, Any], paired_with: str) -> str:
        return board_context(
            board, str(one.get("id") or ""), paired_with, project_id,
        )

    _report(
        progress, f"Asking {lead.get('name')} what to relay",
        f"The user's request is addressed to {lead.get('name')}; Nexus has not contacted {peer.get('name')} yet.",
    )
    lead_answer = chat_lab.ask_once(
        config, str(lead.get("who") or ""), text,
        context=(
            identity(lead, str(peer.get("id") or ""))
            + "\n\nDIRECTED RELAY — LEAD TURN\n"
            + f"The quoted user request is addressed to you, {lead.get('name')}, not to {peer.get('name')}. "
              f"Write the exact useful message you want Nexus to relay to {peer.get('name')}. "
              f"Do not answer as {peer.get('name')} and do not claim the relay already happened."
            + ("\n\n" + attachment_text if attachment_text else "")
            + _shared_context(ledger, lead, {"stage": "lead_draft"})
        ),
        provider_attachments=provider_files,
        conversation_key=conversation_key,
        prefer_existing_conversation=prefer_existing_conversation,
    )
    _ack_shared(ledger, lead)
    lead_turn = _contribution(
        lead, lead_answer, "lead_draft", str(lead_answer.get("text") or ""),
        recipient_id=str(peer.get("id") or ""),
        recipient_name=str(peer.get("name") or "The connected agent"),
    )
    _show_turn(live_turn, {"who": "them", **lead_turn})
    _share_turn(ledger, lead_turn, {"stage": "relay_to_peer"})

    _report(
        progress, f"Relaying {lead.get('name')}'s message to {peer.get('name')}",
        "Nexus is sending the lead agent's actual words, not rebroadcasting the user's request.",
    )
    relayed = str(lead_answer.get("text") or "").strip()
    peer_answer = chat_lab.ask_once(
        config, str(peer.get("who") or ""),
        f"Message relayed by Nexus from {lead.get('name')}:\n{relayed}",
        context=(
            identity(peer, str(lead.get("id") or ""))
            + "\n\nDIRECTED RELAY — PEER TURN\n"
            + f"The quoted task above is a real message from {lead.get('name')} to you, {peer.get('name')}. "
              f"Reply to {lead.get('name')}. Do not treat the end user's earlier first-person wording as addressed to you, "
              f"and never impersonate {lead.get('name')}."
            + ("\n\n" + attachment_text if attachment_text else "")
            + _shared_context(ledger, peer, {"stage": "peer_reply"})
        ),
        provider_attachments=provider_files,
        conversation_key=conversation_key,
        prefer_existing_conversation=prefer_existing_conversation,
    )
    _ack_shared(ledger, peer)
    peer_turn = _contribution(
        peer, peer_answer, "agent_reply", str(peer_answer.get("text") or ""),
        recipient_id=str(lead.get("id") or ""),
        recipient_name=str(lead.get("name") or "The lead agent"),
    )
    _show_turn(live_turn, {"who": "them", **peer_turn})
    _share_turn(ledger, peer_turn, {"stage": "final_report"})

    _report(
        progress, f"Waiting for {lead.get('name')} to report the relay",
        f"{lead.get('name')} is receiving {peer.get('name')}'s actual reply and will answer the user as itself.",
    )
    conversation = _actual_conversation([lead_turn, peer_turn])
    began = time.monotonic()
    final = chat_lab.ask_once(
        config, str(lead.get("who") or ""),
        _continuation_turn(
            "FINAL RELAY REPORT",
            f"Report the completed relay to the user and clearly attribute {peer.get('name')}'s actual reply.",
        ),
        context=(
            identity(lead, str(peer.get("id") or ""))
            + "\n\nORIGINAL USER GOAL\n" + text
            + "\n\nDIRECTED RELAY — FINAL USER REPORT\n"
            + "Nexus completed this relay. Here is the exact exchange:\n"
            + conversation
            + f"\n\nAnswer the user as {lead.get('name')}. Clearly attribute {peer.get('name')}'s real reply; "
              f"do not answer as {peer.get('name')} and do not invent any additional relay."
            + _shared_context(ledger, lead, {"stage": "final_report"})
        ),
        provider_attachments=provider_files,
        conversation_key=conversation_key,
        prefer_existing_conversation=prefer_existing_conversation,
    )
    _ack_shared(ledger, lead)
    kept = chat_lab.keep_multiparty_exchange(
        config, str(lead.get("who") or ""), text, str(final.get("text") or ""),
        filed_as=filed_as or str(lead.get("name") or ""),
        lead=lead, participants=[lead, peer],
        contributions=[lead_turn, peer_turn], attachments=public,
        model=final.get("model", ""), milliseconds=int((time.monotonic() - began) * 1000),
    )
    final_turn = _contribution(
        lead, final, "final_answer", str(final.get("text") or ""),
        recipient_name="User",
    )
    _share_turn(ledger, final_turn)
    ledger.finish(
        str(final.get("text") or ""), complete=True,
        stopped_because="relay_complete",
    )
    return {
        **kept,
        "collaboration_ledger": ledger.describe(),
        "collaborated_with": [{
            "id": peer.get("id"), "name": peer.get("name"), "route": peer.get("who"),
        }],
        "relay_complete": True,
    }


def collaborate(
    config: LoadedConfig,
    board: dict[str, Any],
    agent_id: str,
    text: str,
    attachments: object = None,
    progress: Progress | None = None,
    live_turn: LiveTurn | None = None,
    peer_id: str = "",
    project_id: str = "",
    filed_as: str = "",
    conversation_key: str = "",
    prefer_existing_conversation: bool = False,
    round_limit: int | None = None,
    allow_partial_lead_answer: bool = False,
) -> dict[str, Any]:
    round_limit = user_round_limit(round_limit)
    lead = _agent(board, agent_id)
    participants = _participants(board, lead, peer_id)
    if len(participants) < 2:
        raise SwarmError(
            "This agent has no ready connected agent. Draw a green communicates line first."
        )
    ledger = CollaborationLedger(
        config,
        str(lead.get("who") or ""),
        filed_as or str(lead.get("name") or ""),
    ).begin(text, participants, mode="goal_collaboration")
    public, provider_files, attachment_text = chat_lab.keep_attachments(
        config, str(lead.get("who") or ""), attachments,
        filed_as or str(lead.get("name") or "")
    )
    roster = ", ".join(str(one.get("name")) for one in participants)
    routed_roster = ", ".join(
        f"{one.get('name')} ({one.get('who')})" for one in participants
    )
    _report(
        progress, f"Contacting {len(participants)} agents in parallel",
        f"Nexus is relaying the request to {routed_roster}."
    )

    def first_round(one: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        is_lead = one.get("id") == lead.get("id")
        turn_role = (
            f"The quoted user request is addressed to you as the lead agent, {lead.get('name')}. "
            "Give your own draft answer for the peers to review."
            if is_lead else
            f"The quoted user request is addressed to the lead agent, {lead.get('name')}; you are the peer {one.get('name')}. "
            f"Give advice or a proposed reply to {lead.get('name')}. Do not reinterpret first-person or identity questions as being addressed to you."
        )
        context = (
            board_context(
                board, str(one.get("id")),
                str(lead.get("id")) if one.get("id") != lead.get("id") else peer_id,
                project_id,
            )
            + f"\n\nCOLLABORATION ROUND\nThe user explicitly asked this team to confer: {roster}. "
            + turn_role
            + ("\n\n" + attachment_text if attachment_text else "")
            + _shared_context(ledger, one, {"stage": "independent_first_round"})
        )
        try:
            answer = chat_lab.ask_once(
                config, str(one.get("who") or ""), text,
                context=context, provider_attachments=provider_files,
                conversation_key=conversation_key,
                prefer_existing_conversation=prefer_existing_conversation,
            )
            _ack_shared(ledger, one)
            return one, answer
        except cancellation.ChatCancelled:
            raise
        except HarnessError:
            return one, {
                "text": "", "milliseconds": 0, "model": "",
                "_provider_failed": True,
            }

    completed: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    provider_failures: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=len(participants)) as pool:
        futures = [cancellation.submit(pool, first_round, one) for one in participants]
        for future in as_completed(futures):
            one, answer = future.result()
            completed[str(one.get("id"))] = (one, answer)
            if answer.get("_provider_failed"):
                provider_failures.append(one)
                continue
            live = {
                "who": "them",
                "speaker_id": one.get("id"),
                "speaker_name": one.get("name"),
                "speaker_route": one.get("who"),
                "recipient_id": lead.get("id"),
                "recipient_name": lead.get("name"),
                "text": answer.get("text"),
                "milliseconds": answer.get("milliseconds"),
                "model": answer.get("model"),
                "phase": "lead_draft" if one.get("id") == agent_id else "agent_reply",
            }
            _show_turn(live_turn, live)
            _share_turn(ledger, live, {"stage": "team_discussion"})
    if provider_failures:
        failed_names = [str(one.get("name") or "An agent") for one in provider_failures]
        lead_result = completed.get(str(lead.get("id") or ""))
        lead_answer = lead_result[1] if lead_result else {}
        can_keep_lead = bool(
            allow_partial_lead_answer
            and lead_result
            and not lead_answer.get("_provider_failed")
        )
        ledger.record_state("provider_transport_failure", {
            "stage": "independent_first_round",
            "status": "degraded" if can_keep_lead else "paused",
            "failed_agents": [
                {
                    "id": str(one.get("id") or ""),
                    "name": str(one.get("name") or "An agent"),
                    "route": str(one.get("who") or ""),
                    "failure_code": "provider_turn_failed",
                }
                for one in provider_failures
            ],
        })
        remaining = [
            f"Reconnect or reconcile {name}'s provider turn before resuming."
            for name in failed_names
        ]
        if can_keep_lead:
            # A casual Send in an explicitly selected pair asks both agents, but
            # the selected/lead agent's real answer is still useful when a peer
            # has a transient provider failure. The old all-or-nothing path
            # discarded that successful reply and made the chat look completely
            # dead. Preserve the truthful lead answer while recording exactly
            # which peer did not participate; explicit Collaborate actions keep
            # the stricter all-agents-required behavior below.
            successful_peer_contributions = [
                _contribution(
                    one, draft, "agent_reply", str(draft.get("text") or ""),
                    recipient_id=str(lead.get("id") or ""),
                    recipient_name=str(lead.get("name") or "The lead agent"),
                )
                for one, draft in completed.values()
                if one.get("id") != lead.get("id")
                and not draft.get("_provider_failed")
            ]
            kept = chat_lab.keep_multiparty_exchange(
                config,
                str(lead.get("who") or ""),
                text,
                str(lead_answer.get("text") or ""),
                filed_as=filed_as or str(lead.get("name") or ""),
                lead=lead,
                participants=participants,
                contributions=successful_peer_contributions,
                attachments=public,
                model=str(lead_answer.get("model") or ""),
                milliseconds=int(lead_answer.get("milliseconds") or 0),
            )
            final_turn = _contribution(
                lead, lead_answer, "final_answer", str(lead_answer.get("text") or ""),
                recipient_name="User",
            )
            _share_turn(ledger, final_turn)
            partial_note = (
                f"{lead.get('name') or 'The selected agent'} answered. "
                + ", ".join(failed_names)
                + " could not join this turn, so Nexus kept the successful answer instead of discarding it."
            )
            ledger.finish(
                partial_note, complete=True,
                stopped_because="partial_provider_failure",
            )
            return {
                **kept,
                "collaboration_ledger": ledger.describe(),
                "collaborated_with": [
                    {"id": one.get("id"), "name": one.get("name"), "route": one.get("who")}
                    for one, draft in completed.values()
                    if one.get("id") != lead.get("id")
                    and not draft.get("_provider_failed")
                ],
                "provider_failures": [
                    {"id": one.get("id"), "name": one.get("name"), "route": one.get("who")}
                    for one in provider_failures
                ],
                "partial_provider_failure": partial_note,
                "goal_complete": True,
                "discussion_rounds": 0,
                "round_limit": round_limit,
                "stopped_because": "partial_provider_failure",
                "remaining": [],
            }
        report = (
            "Nexus paused this collaboration because "
            + ", ".join(failed_names)
            + " could not complete a provider turn. The failure was not counted as "
              "an agent reply or a discussion round."
        )
        ledger.finish(
            report, complete=False, stopped_because="provider_unavailable",
            remaining=remaining,
        )
        raise SwarmError(report)
    # The screen shows actual completion order. The lead receives stable board
    # order so provider timing does not make otherwise identical runs drift.
    drafts = [completed[str(one.get("id"))] for one in participants]
    contributions = [
        _contribution(
            one, draft,
            "lead_draft" if one.get("id") == agent_id else "agent_reply",
            str(draft.get("text") or ""),
            recipient_id=str(lead.get("id") or ""),
            recipient_name=str(lead.get("name") or "The lead agent"),
        )
        for one, draft in drafts
    ]
    _report(
        progress, "Starting goal-directed team discussion",
        "Each agent will see the real conversation so far and the team will continue until everyone reports the goal complete."
    )
    goal_complete = False
    remaining: list[str] = []
    discussion_rounds = 0
    stopped_because = ""
    progress_guard = _ProgressGuard()
    for round_number in _round_numbers(round_limit):
        discussion_rounds = round_number
        cycle_complete = True
        cycle_remaining: list[str] = []
        cycle_state: list[tuple[str, str]] = []
        _report(
            progress, f"Team discussion round {round_number}",
            "Agents are responding in board order, so every later reply sees every earlier reply."
        )
        for one in participants:
            is_lead = one.get("id") == lead.get("id")
            turn_role = (
                f"You are the lead agent {lead.get('name')}; continue toward a user-facing answer."
                if is_lead else
                f"You are the peer {one.get('name')}. Respond to the lead agent {lead.get('name')}, not directly to the end user. "
                "Do not answer identity or first-person wording as though it were addressed to you."
            )
            context = (
                board_context(
                    board, str(one.get("id")),
                    str(lead.get("id")) if one.get("id") != lead.get("id") else peer_id,
                    project_id,
                )
                + "\n\nGOAL-DIRECTED TEAM CONVERSATION\n"
                + f"Original user goal:\n{text}\n\n"
                + "ACTUAL CONVERSATION SO FAR\n"
                + _actual_conversation(contributions)
                + "\n\nContinue the real conversation. Address the other agents directly when useful. "
                  "Do not claim the goal is complete merely because you gave advice: completion means the user's requested outcome has actually been achieved. "
                  "Set goal_complete false and list concrete remaining work whenever anything is unfinished. "
                  "The remaining list is Nexus's progress ledger: name the current unresolved facts, decisions, or outputs precisely, remove resolved items, "
                  "and change an item when real progress changes its state. Do not disguise an unchanged blocker with new prose."
                  " When a canonical checkpoint really changes, include progress entries with stable IDs, exact states, and concrete evidence; keep the same ID for the same checkpoint."
                + "\n" + turn_role
                + ("\n\n" + attachment_text if attachment_text else "")
                + _shared_context(ledger, one, {
                    "stage": "team_discussion",
                    "round": round_number,
                    "previous_remaining": remaining,
                })
            )
            try:
                failed = False
                answer = chat_lab.ask_once(
                    config, str(one.get("who") or ""),
                    _continuation_turn(
                        f"TEAM DISCUSSION ROUND {round_number}",
                        "Continue the real team conversation from its current point and return the required structured progress state.",
                    ),
                    context=context, provider_attachments=provider_files,
                    response_format=DISCUSSION_FORMAT,
                    conversation_key=conversation_key,
                    prefer_existing_conversation=prefer_existing_conversation,
                )
                _ack_shared(ledger, one)
                value = _decode_with_one_web_repair(
                    config, one, answer, DISCUSSION_FORMAT, ledger,
                    conversation_key, prefer_existing_conversation,
                )
                message = str(value.get("message") or "").strip()
                one_remaining = _remaining(value)
                one_complete = value.get("goal_complete") is True and not one_remaining
            except cancellation.ChatCancelled:
                raise
            except HarnessError as exc:
                failed_name = str(one.get("name") or "An agent")
                report = (
                    f"Nexus paused this collaboration because {failed_name} could not "
                    "complete its provider turn. The failure was not counted as agent "
                    "speech or reasoning progress."
                )
                one_remaining = [
                    f"Reconnect or reconcile {failed_name}'s provider turn before resuming."
                ]
                ledger.record_state("provider_transport_failure", {
                    "stage": "team_discussion",
                    "round": round_number,
                    "status": "paused",
                    "failed_agent": {
                        "id": str(one.get("id") or ""),
                        "name": failed_name,
                        "route": str(one.get("who") or ""),
                    },
                    "failure_code": "provider_turn_failed",
                })
                ledger.finish(
                    report, complete=False, stopped_because="provider_unavailable",
                    remaining=one_remaining,
                )
                raise SwarmError(report) from exc
            contribution = _contribution(
                one, answer, "agent_discussion", message,
                recipient_name="Team deliberation",
            )
            contributions.append(contribution)
            _show_turn(live_turn, {"who": "them", **contribution})
            _share_turn(ledger, contribution, {
                "stage": "team_discussion",
                "round": round_number,
                "speaker_complete": one_complete,
                "speaker_remaining": one_remaining,
            })
            cycle_remaining.extend(one_remaining)
            cycle_complete = cycle_complete and one_complete
            cycle_state.append(_canonical_progress_state(
                str(one.get("id") or ""), one_complete, failed, value
            ))
        remaining = list(dict.fromkeys(cycle_remaining))
        ledger.record_state("discussion_round_state", {
            "stage": "team_discussion",
            "round": round_number,
            "all_agents_complete": cycle_complete,
            "remaining": remaining,
        })
        if cycle_complete:
            goal_complete = True
            stopped_because = "complete"
            break
        if progress_guard.stalled(tuple(cycle_state)):
            remaining.append(
                "Nexus stopped a repeated no-progress cycle: no agent changed completion state, unresolved work, requested files, or provider-failure state."
            )
            stopped_because = "stalled"
            break
    if not goal_complete and not stopped_because:
        remaining.append(
            f"The user-set limit of {round_limit} team discussion round(s) was reached."
        )
        stopped_because = "round_limit"

    began = time.monotonic()
    _report(
        progress, f"Waiting for {lead.get('name')} to report the outcome",
        "The lead agent is preparing a truthful completion report from the full visible discussion."
    )
    final = chat_lab.ask_once(
        config,
        str(lead.get("who") or ""),
        _continuation_turn(
            "FINAL TEAM REPORT",
            "Give the user the current team outcome from the completed conversation and Nexus completion state.",
        ),
        context=(
            board_context(board, agent_id, peer_id, project_id)
            + "\n\nORIGINAL USER GOAL\n" + text
            + "\n\nFULL ACTUAL TEAM CONVERSATION\n"
            + _actual_conversation(contributions)
            + f"\n\nNEXUS COMPLETION STATE: {'complete' if goal_complete else 'incomplete'}"
            + f"\nNEXUS STOP REASON: {stopped_because}"
            + ("\nREMAINING WORK: " + "; ".join(remaining) if remaining else "")
            + "\n\nGive the user a truthful final report. Name disagreements plainly. "
              "If Nexus says incomplete, explicitly say the goal is incomplete and list what remains; do not present discussion or suggested work as completed work."
            + ("\n\n" + attachment_text if attachment_text else "")
            + _shared_context(ledger, lead, {
                "stage": "final_report",
                "goal_complete": goal_complete,
                "stopped_because": stopped_because,
                "remaining": remaining,
            })
        ),
        provider_attachments=provider_files,
        conversation_key=conversation_key,
        prefer_existing_conversation=prefer_existing_conversation,
    )
    _ack_shared(ledger, lead)
    kept = chat_lab.keep_multiparty_exchange(
        config,
        str(lead.get("who") or ""),
        text,
        final["text"],
        filed_as=filed_as or str(lead.get("name") or ""),
        lead=lead,
        participants=participants,
        contributions=contributions,
        attachments=public,
        model=final.get("model", ""),
        milliseconds=int((time.monotonic() - began) * 1000),
    )
    final_turn = _contribution(
        lead, final, "final_answer", str(final.get("text") or ""),
        recipient_name="User",
    )
    _share_turn(ledger, final_turn)
    ledger.finish(
        str(final.get("text") or ""), complete=goal_complete,
        stopped_because=stopped_because, remaining=remaining,
    )
    return {
        **kept,
        "collaboration_ledger": ledger.describe(),
        "collaborated_with": [
            {"id": one.get("id"), "name": one.get("name"), "route": one.get("who")}
            for one in participants if one.get("id") != agent_id
        ],
        "goal_complete": goal_complete,
        "discussion_rounds": discussion_rounds,
        "round_limit": round_limit,
        "stopped_because": stopped_because,
        "remaining": remaining,
    }


def _one_project(
    board: dict[str, Any], lead: dict[str, Any], project_id: str = ""
) -> dict[str, Any]:
    ids = [
        str(line.get("project")) for line in board.get("works_on", [])
        if isinstance(line, dict) and line.get("agent") == lead.get("id")
    ]
    projects = [
        one for one in board.get("projects", [])
        if isinstance(one, dict) and str(one.get("id")) in ids
    ]
    if project_id:
        projects = [one for one in projects if str(one.get("id")) == project_id]
        if not projects:
            raise SwarmError(
                "The active chat project is not connected to this agent any more."
            )
    if not projects:
        raise SwarmError("Connect this agent to a project folder before asking it to change files.")
    if len(projects) != 1:
        raise SwarmError("This agent is connected to more than one project. Leave one project connected for this file task.")
    project = projects[0]
    root = Path(str(project.get("path") or "")).resolve()
    if not root.is_dir():
        raise SwarmError("The connected project folder is not available on this machine.")
    return project


def _project_participants(
    board: dict[str, Any], lead: dict[str, Any], project_id: str,
    peer_id: str = "",
) -> list[dict[str, Any]]:
    assigned = {
        str(line.get("agent")) for line in board.get("works_on", [])
        if isinstance(line, dict) and str(line.get("project")) == project_id
    }
    # A communication line permits a relay.  It does not itself grant the
    # connected agent access to a project tree; the works-on line is that
    # separate authority.
    return [
        one for one in _participants(board, lead, peer_id)
        if str(one.get("id")) in assigned
    ]


def _tree(root: Path) -> str:
    paths: list[str] = []
    skipped = {".git", ".harness", "node_modules", ".venv", "venv", "dist", "build"}
    for folder, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(one for one in directories if one not in skipped)[:80]
        base = Path(folder)
        for name in sorted(files):
            path = base / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                paths.append(path.relative_to(root).as_posix())
            except (OSError, ValueError):
                continue
            if len(paths) >= 600:
                return "\n".join(paths) + "\n[tree truncated]"
    return "\n".join(paths) or "[empty project]"


def _requested_files(root: Path, plans: list[tuple[dict[str, Any], dict[str, Any]]]) -> str:
    wanted: list[str] = []
    for _agent_row, plan in plans:
        raw = plan.get("needs_files", [])
        if isinstance(raw, list):
            wanted.extend(str(one) for one in raw if isinstance(one, str))
    blocks: list[str] = []
    used = 0
    for relative in list(dict.fromkeys(wanted))[:20]:
        try:
            path = confined_path(root, relative)
            if not path.is_file() or path.is_symlink() or path.stat().st_size > 250_000:
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
        except (HarnessError, OSError):
            continue
        if used + len(content) > 180_000:
            break
        blocks.append(f"FILE {relative}\n{content}")
        used += len(content)
    return "\n\n".join(blocks) or "[No existing file content was requested.]"


def _plan_words(value: dict[str, Any]) -> str:
    words = (
        f"Contribution: {value.get('contribution') or '(none)'}\n"
        f"Message to team: {value.get('message_to_lead') or '(none)'}\n"
        "Requested files: "
        + (", ".join(str(path) for path in value.get("needs_files", [])) or "none")
    )
    if "ready_to_execute" in value or "remaining" in value:
        remaining = _remaining(value)
        words += (
            "\nExecution readiness: "
            + ("ready" if value.get("ready_to_execute") is True else "not ready")
            + "\nRemaining planning work: "
            + ("; ".join(remaining) or "none")
        )
    return words


def _validated_changes(root: Path, raw_changes: object) -> list[ChangePlan]:
    if not isinstance(raw_changes, list):
        raise HarnessError("The acting agent returned an invalid file change list")
    changes: list[ChangePlan] = []
    seen: set[str] = set()
    for raw in raw_changes:
        if not isinstance(raw, dict):
            raise HarnessError("A proposed file change is malformed")
        relative = str(raw.get("path") or "").replace("\\", "/").strip()
        if not relative or relative in seen:
            raise HarnessError("The proposed file changes contain a missing or duplicate path")
        path = confined_path(root, relative)
        if path.is_symlink():
            raise HarnessError(f"Refusing to replace a symbolic link: {relative}")
        seen.add(relative)
        content = str(raw.get("content") or "")
        # A provider can ignore the instruction not to return unchanged files.
        # Treat that as no progress rather than creating a misleading backup,
        # transaction id, and execution turn that claims the file changed.
        if file_sha256(path) == sha256_bytes(content.encode("utf-8")):
            continue
        changes.append(ChangePlan(
            path=relative,
            baseline_sha256=file_sha256(path),
            content=content,
            reason=str(raw.get("reason") or "Board work request")[:1000],
        ))
    return changes


def _file_snapshot(root: Path, paths: list[str]) -> str:
    blocks: list[str] = []
    used = 0
    for relative in list(dict.fromkeys(paths))[:30]:
        try:
            path = confined_path(root, relative)
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 500_000:
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
        except (HarnessError, OSError):
            continue
        if used + len(content) > 300_000:
            break
        blocks.append(f"FILE {relative}\n{content}")
        used += len(content)
    return "\n\n".join(blocks) or "[No readable changed or requested files.]"


def work_together(
    config: LoadedConfig,
    board: dict[str, Any],
    agent_id: str,
    text: str,
    attachments: object = None,
    progress: Progress | None = None,
    live_turn: LiveTurn | None = None,
    peer_id: str = "",
    project_id: str = "",
    filed_as: str = "",
    conversation_key: str = "",
    prefer_existing_conversation: bool = False,
    round_limit: int | None = None,
) -> dict[str, Any]:
    round_limit = user_round_limit(round_limit)
    lead = _agent(board, agent_id)
    project = _one_project(board, lead, project_id)
    root = Path(str(project.get("path"))).resolve()
    orphan_recovery = _MutationSaga.recover_orphans(root)
    unresolved_orphans = [
        one for one in orphan_recovery if one.get("status") != "rolled_back"
    ]
    if unresolved_orphans:
        raise SwarmError(
            "Nexus found an interrupted mutation saga that could not be safely compensated: "
            + json.dumps(unresolved_orphans, sort_keys=True)
        )
    participants = _project_participants(
        board, lead, str(project.get("id")), peer_id
    )
    if len(participants) < 2:
        raise SwarmError(
            "No ready connected agent also works on this project. Connect another ready "
            "agent with both a green communication line and a works-on line first."
        )
    ledger = CollaborationLedger(
        config,
        str(lead.get("who") or ""),
        filed_as or str(lead.get("name") or ""),
    ).begin(text, participants, mode="project_work")
    public, provider_files, attachment_text = chat_lab.keep_attachments(
        config, str(lead.get("who") or ""), attachments,
        filed_as or str(lead.get("name") or "")
    )
    roster = ", ".join(str(one.get("name")) for one in participants)
    routed_roster = ", ".join(
        f"{one.get('name')} ({one.get('who')})" for one in participants
    )
    began = time.monotonic()
    _report(
        progress, f"Collecting project plans from {len(participants)} agents",
        f"Nexus is asking {routed_roster} for bounded proposals; no files are being changed yet."
    )
    common = (
        f"EXPLICIT PROJECT-WORK REQUEST\nProject: {project.get('name')}\n"
        "Nexus, not the provider process, owns the project-file transaction. "
        "Paths must be relative to this project. Do not propose .git or .harness files.\n"
        f"Team: {roster}\nORIGINAL USER GOAL\n{text}\nPROJECT TREE\n{_tree(root)}"
        + ("\n\n" + attachment_text if attachment_text else "")
    )

    def context_for(one: dict[str, Any]) -> str:
        paired_with = (
            str(lead.get("id") or "")
            if peer_id and one.get("id") != lead.get("id") else peer_id
        )
        return board_context(
            board, str(one.get("id")), paired_with, str(project.get("id"))
        )

    def plan(one: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            answer = chat_lab.ask_once(
                config,
                str(one.get("who") or ""),
                text,
                context=context_for(one) + "\n\n" + common
                + "\nPlan your contribution and write a message to the lead. Request only existing files you truly need."
                + _shared_context(ledger, one, {"stage": "independent_planning"}),
                provider_attachments=provider_files,
                response_format=PLAN_FORMAT,
                conversation_key=conversation_key,
                prefer_existing_conversation=prefer_existing_conversation,
            )
            _ack_shared(ledger, one)
            decoded = _decode_with_one_web_repair(
                config, one, answer, PLAN_FORMAT, ledger,
                conversation_key, prefer_existing_conversation,
            )
        except cancellation.ChatCancelled:
            raise
        except HarnessError:
            return one, {
                "_provider_failed": True,
                "_milliseconds": 0,
                "_model": "",
            }
        decoded["_milliseconds"] = int(answer.get("milliseconds") or 0)
        decoded["_model"] = str(answer.get("model") or "")
        return one, decoded

    completed: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    provider_failures: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=len(participants)) as pool:
        futures = [cancellation.submit(pool, plan, one) for one in participants]
        for future in as_completed(futures):
            one, value = future.result()
            completed[str(one.get("id"))] = (one, value)
            if value.get("_provider_failed"):
                provider_failures.append(one)
                continue
            live_text = (
                f"Contribution: {value.get('contribution') or '(none)'}\n"
                f"Message to lead: {value.get('message_to_lead') or '(none)'}\n"
                "Requested files: "
                + (", ".join(str(path) for path in value.get("needs_files", [])) or "none")
            )
            live = {
                "who": "them",
                "speaker_id": one.get("id"),
                "speaker_name": one.get("name"),
                "speaker_route": one.get("who"),
                "recipient_id": lead.get("id"),
                "recipient_name": lead.get("name"),
                "text": live_text,
                "milliseconds": value.get("_milliseconds"),
                "model": value.get("_model"),
                "phase": "lead_plan" if one.get("id") == agent_id else "agent_plan",
            }
            _show_turn(live_turn, live)
            _share_turn(ledger, live, {"stage": "plan_review"})
    if provider_failures:
        _pause_provider_failure(
            ledger, provider_failures, "independent_planning"
        )
    plans = [completed[str(one.get("id"))] for one in participants]
    contributions = [
        _contribution(
            one, value,
            "lead_plan" if one.get("id") == agent_id else "agent_plan",
            _plan_words(value),
            recipient_id=str(lead.get("id") or ""),
            recipient_name=str(lead.get("name") or "The lead agent"),
        )
        for one, value in plans
    ]

    latest = {str(one.get("id")): (one, value) for one, value in plans}
    plan_progress_guard = _ProgressGuard()
    plan_rounds = 0
    plan_remaining: list[str] = []
    plan_stopped_because = ""
    for round_number in _round_numbers(round_limit):
        plan_rounds = round_number
        everyone_ready = True
        cycle_remaining: list[str] = []
        cycle_state: list[tuple[str, str]] = []
        _report(
            progress, f"Team plan review round {round_number}",
            "Agents are reviewing one another's real messages in order before any files change."
        )
        for one in participants:
            try:
                failed = False
                answer = chat_lab.ask_once(
                    config, str(one.get("who") or ""),
                    _continuation_turn(
                        f"TEAM PLAN REVIEW ROUND {round_number}",
                        "Review the latest actual team plan, improve only what still needs improvement, and return the required structured readiness state.",
                    ),
                    context=(
                        context_for(one) + "\n\n" + common
                        + "\n\nACTUAL PLAN CONVERSATION SO FAR\n"
                        + _actual_conversation(contributions)
                        + "\n\nReview the team plan and respond to the other agents. Improve your own contribution, "
                          "request any existing files still needed, and set ready_to_execute only when this plan can actually fulfill the user's goal. "
                          "ready_to_execute means no more planning or input is needed before Nexus starts the file transaction; it does not mean the files already exist. "
                          "Execution and post-transaction verification steps belong in the plan and are not remaining planning work. "
                          "When the plan already specifies the requested changes and how to verify them, set ready_to_execute true and remaining to an empty list. "
                          "When a canonical planning checkpoint really changes, include progress entries with stable IDs, exact states, and concrete evidence; keep the same ID for the same checkpoint."
                        + _shared_context(ledger, one, {
                            "stage": "plan_review",
                            "round": round_number,
                            "previous_remaining": plan_remaining,
                        })
                    ),
                    provider_attachments=provider_files,
                    response_format=PLAN_REVIEW_FORMAT,
                    conversation_key=conversation_key,
                    prefer_existing_conversation=prefer_existing_conversation,
                )
                _ack_shared(ledger, one)
                value = _decode_with_one_web_repair(
                    config, one, answer, PLAN_REVIEW_FORMAT, ledger,
                    conversation_key, prefer_existing_conversation,
                )
            except cancellation.ChatCancelled:
                raise
            except HarnessError as exc:
                _pause_provider_failure(
                    ledger,
                    one,
                    "plan_review",
                    checkpoint={"round": round_number},
                    cause=exc,
                )
            value["_milliseconds"] = int(answer.get("milliseconds") or 0)
            value["_model"] = str(answer.get("model") or "")
            latest[str(one.get("id"))] = (one, value)
            one_remaining = _remaining(value)
            one_ready = value.get("ready_to_execute") is True and not one_remaining
            everyone_ready = everyone_ready and one_ready
            cycle_remaining.extend(one_remaining)
            cycle_state.append(_canonical_progress_state(
                str(one.get("id") or ""), one_ready, failed, value,
                value.get("needs_files", []),
            ))
            words = _plan_words(value)
            contribution = _contribution(one, value, "agent_plan_review", words)
            contributions.append(contribution)
            _show_turn(live_turn, {"who": "them", **contribution})
            _share_turn(ledger, contribution, {
                "stage": "plan_review",
                "round": round_number,
                "speaker_ready": one_ready,
                "speaker_remaining": one_remaining,
                "requested_files": value.get("needs_files", []),
            })
        plan_remaining = list(dict.fromkeys(cycle_remaining))
        ledger.record_state("plan_round_state", {
            "stage": "plan_review",
            "round": round_number,
            "all_agents_ready": everyone_ready,
            "remaining": plan_remaining,
        })
        if everyone_ready:
            plan_stopped_because = "complete"
            break
        if plan_progress_guard.stalled(tuple(cycle_state)):
            plan_remaining.append(
                "Nexus stopped a repeated planning cycle because readiness, remaining work, requested files, and provider-failure state did not advance."
            )
            plan_stopped_because = "stalled"
            break
    if not everyone_ready and not plan_stopped_because:
        plan_remaining.append(
            f"The user-set limit of {round_limit} team plan-review round(s) was reached."
        )
        plan_stopped_because = "round_limit"

    plans = [latest[str(one.get("id"))] for one in participants]
    if not everyone_ready:
        plan_remaining = list(dict.fromkeys(
            plan_remaining or ["The team did not produce a valid execution-ready plan."]
        ))
        reply = (
            "Nexus stopped before opening a project-file transaction because every participating agent "
            "did not produce a valid, execution-ready plan.\nRemaining: "
            + "; ".join(plan_remaining)
            + "\n\nNexus applied no project-file changes."
        )
        kept = chat_lab.keep_multiparty_exchange(
            config, str(lead.get("who") or ""), text, reply,
            filed_as=filed_as or str(lead.get("name") or ""),
            lead=lead, participants=participants, contributions=contributions,
            attachments=public, model="",
            milliseconds=int((time.monotonic() - began) * 1000),
        )
        ledger.finish(
            reply, complete=False,
            stopped_because=f"plan_{plan_stopped_because}",
            remaining=plan_remaining,
        )
        return {
            **kept,
            "collaboration_ledger": ledger.describe(),
            "worked_with": [
                {"id": one.get("id"), "name": one.get("name"), "route": one.get("who")}
                for one in participants
            ],
            "project": {"id": project.get("id"), "name": project.get("name"), "path": str(root)},
            "transaction_id": "", "transaction_ids": [], "changed": [],
            "goal_complete": False, "plan_rounds": plan_rounds,
            "work_passes": 0, "round_limit": round_limit,
            "stopped_because": f"plan_{plan_stopped_because}",
            "remaining": plan_remaining,
        }
    team_plans = "\n\n".join(
        f"CURRENT PLAN FROM {one.get('name')} ({one.get('who')}):\n{_plan_words(value)}"
        for one, value in plans
    )
    _report(
        progress, "Reading the requested project files",
        "Nexus is confining requested paths to the connected project before sharing their current contents."
    )
    all_changed: list[str] = []
    transaction_ids: list[str] = []
    mutation_saga = _MutationSaga(root, ledger.session_id)
    work_passes = 0
    goal_complete = False
    remaining = plan_remaining
    feedback = ""
    final_answer: dict[str, Any] = {"model": ""}
    no_change_passes = 0
    work_stopped_because = ""
    for pass_number in _round_numbers(round_limit):
        work_passes = pass_number
        _report(
            progress, f"Project execution pass {pass_number}",
            "Each participating agent will receive its own execution turn in board order."
        )
        pass_changed: list[str] = []
        requested_paths = [
            str(path) for _one, value in plans for path in value.get("needs_files", [])
            if isinstance(path, str)
        ]
        for executor in participants:
            executor_name = str(executor.get("name") or "The acting agent")
            _report(
                progress, f"Execution turn for {executor_name}",
                f"Nexus is asking {executor_name} to perform its own reviewed contribution and any remaining work assigned to it.",
            )
            current_files = _file_snapshot(root, all_changed + requested_paths)
            try:
                execution_answer = chat_lab.ask_once(
                    config,
                    str(executor.get("who") or ""),
                    _continuation_turn(
                        f"EXECUTION PASS {pass_number} — {executor_name}",
                        f"Perform {executor_name}'s currently assigned contribution against the latest real project state and return complete proposed file changes.",
                    ),
                    context=(
                        context_for(executor) + "\n\n" + common
                        + "\n\nEXECUTION TURN — YOU ARE THE ACTING AGENT\n"
                        + f"You are {executor_name}. This is your own execution turn, not a review turn and not a request to wait for another agent. "
                          f"Perform the contribution assigned to {executor_name} in the reviewed plan now. "
                          f"If the conversation or verification feedback says that {executor_name} must do something, that instruction is addressed to you. "
                          "Return the complete file changes needed for your contribution in the changes field; Nexus will apply them through its bounded transaction layer. "
                          "Do not describe yourself as a third party, defer your assigned work back to yourself, or merely announce what you intend to do. "
                          "You may also resolve other remaining work when that is necessary to complete the user's goal. Return no unchanged files."
                        + "\n\nYOUR REVIEWED PLAN\n" + _plan_words(latest[str(executor.get("id"))][1])
                        + "\n\nACTUAL TEAM CONVERSATION\n" + _actual_conversation(contributions)
                        + "\n\nCURRENT TEAM PLANS\n" + team_plans
                        + "\n\nACTUAL PROJECT TREE NOW\n" + _tree(root)
                        + "\n\nACTUAL CHANGED/REQUESTED FILES NOW\n" + current_files
                        + ("\n\nVERIFICATION FEEDBACK FROM THE LAST PASS\n" + feedback if feedback else "")
                        + _shared_context(ledger, executor, {
                            "stage": "execution",
                            "pass": pass_number,
                            "changed": all_changed,
                            "remaining": remaining,
                        })
                    ),
                    provider_attachments=provider_files,
                    response_format=WORK_FORMAT,
                    conversation_key=conversation_key,
                    prefer_existing_conversation=prefer_existing_conversation,
                )
                _ack_shared(ledger, executor)
                execution = _decode_with_one_web_repair(
                    config, executor, execution_answer, WORK_FORMAT, ledger,
                    conversation_key, prefer_existing_conversation,
                )
            except cancellation.ChatCancelled:
                recovery = mutation_saga.compensate("user_cancelled")
                try:
                    ledger.record_state("cancelled", {
                        "stage": "execution", "pass": pass_number,
                        "status": "cancelled", "mutation_recovery": recovery,
                    })
                except HarnessError:
                    pass
                raise
            except HarnessError as exc:
                _pause_provider_failure(
                    ledger,
                    executor,
                    "execution",
                    checkpoint={"pass": pass_number},
                    cause=exc,
                    mutation_root=root,
                    transaction_ids=transaction_ids,
                    mutation_saga=mutation_saga,
                )
            changes = _validated_changes(root, execution.get("changes"))
            executor_changed: list[str] = []
            if changes:
                _report(
                    progress, f"Applying {executor_name}'s proposed changes",
                    "Nexus is checking paths and fresh baselines before opening the atomic transaction."
                )
                try:
                    transaction_id = FileTransaction.new_transaction_id()
                    mutation_saga.prepare(transaction_id)
                    manifest = FileTransaction(
                        root, max_files=12, max_bytes=2_000_000
                    ).apply(changes, transaction_id=transaction_id)
                    mutation_saga.applied(transaction_id)
                except HarnessError as exc:
                    recovery = mutation_saga.compensate("transaction_failed")
                    ledger.record_state("mutation_failed", {
                        "stage": "execution", "pass": pass_number,
                        "status": "paused", "failure": str(exc),
                        "mutation_recovery": recovery,
                    })
                    raise SwarmError(
                        "Nexus stopped because the project transaction conflicted or failed; "
                        f"mutation recovery is {recovery['status']}."
                    ) from exc
                transaction_id = str(manifest.get("transaction_id") or "")
                if transaction_id:
                    transaction_ids.append(transaction_id)
                executor_changed = [
                    str(one.get("path")) for one in manifest.get("changes", [])
                    if isinstance(one, dict)
                ]
                pass_changed.extend(
                    path for path in executor_changed if path not in pass_changed
                )
                all_changed.extend(
                    path for path in executor_changed if path not in all_changed
                )
            execution_words = str(
                execution.get("reply") or "Execution turn finished."
            ).strip()
            execution_words += "\nApplied in this turn: " + (
                ", ".join(executor_changed) or "none"
            )
            execution_turn = _contribution(
                executor, execution_answer,
                (
                    "lead_execution"
                    if executor.get("id") == lead.get("id")
                    else "agent_execution"
                ),
                execution_words, recipient_name="Team verification",
            )
            contributions.append(execution_turn)
            _show_turn(live_turn, {"who": "them", **execution_turn})
            _share_turn(ledger, execution_turn, {
                "stage": "execution",
                "pass": pass_number,
                "applied_in_turn": executor_changed,
                "changed": all_changed,
            })
            final_answer = execution_answer

        current_files = _file_snapshot(root, all_changed + requested_paths)
        _report(
            progress, f"Team verification pass {pass_number}",
            "Every participating agent is checking the actual files now on disk against the original goal."
        )
        pass_complete = True
        pass_remaining: list[str] = []
        verification_feedback: list[str] = []
        for one in participants:
            try:
                answer = chat_lab.ask_once(
                    config, str(one.get("who") or ""),
                    _continuation_turn(
                        f"VERIFICATION PASS {pass_number} — {one.get('name')}",
                        "Verify the latest real on-disk result against outstanding requirements. Silently account for already-settled requirements. The feedback field must omit completed requirements entirely and contain only new verification facts, remaining work, and blockers.",
                    ),
                    context=(
                        context_for(one) + "\n\n" + common
                        + "\n\nACTUAL TEAM CONVERSATION\n" + _actual_conversation(contributions)
                        + "\n\nACTUAL PROJECT TREE NOW\n" + _tree(root)
                        + "\n\nACTUAL CHANGED/REQUESTED FILES NOW\n" + current_files
                        + "\n\nVerify the real on-disk result, not the lead's claims. Set goal_complete true only if the whole user goal is fulfilled. "
                          "Otherwise give specific corrective feedback and list every remaining item."
                        + _shared_context(ledger, one, {
                            "stage": "verification",
                            "pass": pass_number,
                            "changed": all_changed,
                            "previous_remaining": remaining,
                        })
                    ),
                    provider_attachments=provider_files,
                    response_format=WORK_VERIFICATION_FORMAT,
                    conversation_key=conversation_key,
                    prefer_existing_conversation=prefer_existing_conversation,
                )
                _ack_shared(ledger, one)
                value = _decode_with_one_web_repair(
                    config, one, answer, WORK_VERIFICATION_FORMAT, ledger,
                    conversation_key, prefer_existing_conversation,
                )
            except cancellation.ChatCancelled:
                recovery = mutation_saga.compensate("user_cancelled")
                try:
                    ledger.record_state("cancelled", {
                        "stage": "verification", "pass": pass_number,
                        "status": "cancelled", "mutation_recovery": recovery,
                    })
                except HarnessError:
                    pass
                raise
            except HarnessError as exc:
                _pause_provider_failure(
                    ledger,
                    one,
                    "verification",
                    checkpoint={"pass": pass_number},
                    cause=exc,
                    mutation_root=root,
                    transaction_ids=transaction_ids,
                    mutation_saga=mutation_saga,
                )
            one_remaining = _remaining(value)
            one_complete = value.get("goal_complete") is True and not one_remaining
            words = str(value.get("feedback") or "").strip()
            if one_remaining:
                words += "\nRemaining: " + "; ".join(one_remaining)
            turn = _contribution(one, answer, "agent_verification", words)
            contributions.append(turn)
            _show_turn(live_turn, {"who": "them", **turn})
            _share_turn(ledger, turn, {
                "stage": "verification",
                "pass": pass_number,
                "speaker_complete": one_complete,
                "speaker_remaining": one_remaining,
            })
            pass_complete = pass_complete and one_complete
            pass_remaining.extend(one_remaining)
            if not one_complete:
                verification_feedback.append(f"{one.get('name')}: {words}")
        remaining = list(dict.fromkeys(pass_remaining))
        ledger.record_state("verification_pass_state", {
            "stage": "verification",
            "pass": pass_number,
            "all_agents_complete": pass_complete,
            "changed": all_changed,
            "remaining": remaining,
            "verification_basis": "provider_claims_only",
            "deterministically_verified": False,
        })
        if pass_complete:
            goal_complete = True
            work_stopped_because = "complete"
            break
        feedback = "\n\n".join(verification_feedback)
        # Provider prose can paraphrase forever while the project state remains
        # unchanged. One feedback-informed retry is useful; a second complete
        # team pass with no file change proves that execution is not advancing.
        no_change_passes = no_change_passes + 1 if not pass_changed else 0
        if no_change_passes >= 2:
            remaining.append(
                "Two complete team execution passes made no file changes."
            )
            work_stopped_because = "stalled"
            break
    if not goal_complete and not work_stopped_because:
        remaining.append(
            f"The user-set limit of {round_limit} project execution/verification round(s) was reached."
        )
        work_stopped_because = "round_limit"
    mutation_recovery = {"status": "not_needed", "rolled_back_transaction_ids": []}
    if not goal_complete and transaction_ids:
        mutation_recovery = mutation_saga.compensate("incomplete")
        if mutation_recovery["status"] == "rolled_back":
            all_changed = []
        else:
            remaining.append(
                "Automatic rollback stopped at a project-file conflict; inspect the transaction manifests before retrying."
            )
            work_stopped_because = "rollback_conflict"
    elif goal_complete:
        mutation_saga.complete("provider_consensus_unverified")
    else:
        mutation_saga.complete("no_mutations")
    reply = (
        "The connected agents completed their assigned execution turns."
        if goal_complete else
        "The connected agents stopped without completing every assigned execution turn."
    )
    if goal_complete:
        reply += (
            "\n\nProvider review: every participating agent marked the requested goal complete. "
            "Nexus did not run deterministic tests, so this is not independently verified."
        )
    else:
        reply += "\n\nNexus state: the requested goal is still incomplete."
        if remaining:
            reply += "\nRemaining: " + "; ".join(remaining)
    if all_changed:
        reply += "\n\nNexus applied: " + ", ".join(all_changed)
    else:
        reply += "\n\nNexus applied no project-file changes."
    kept = chat_lab.keep_multiparty_exchange(
        config,
        str(lead.get("who") or ""),
        text,
        reply,
        filed_as=filed_as or str(lead.get("name") or ""),
        lead=lead,
        participants=participants,
        contributions=contributions,
        attachments=public,
        model=final_answer.get("model", ""),
        milliseconds=int((time.monotonic() - began) * 1000),
    )
    ledger.finish(
        reply, complete=goal_complete,
        stopped_because=work_stopped_because,
        remaining=remaining,
    )
    return {
        **kept,
        "collaboration_ledger": ledger.describe(),
        "worked_with": [
            {"id": one.get("id"), "name": one.get("name"), "route": one.get("who")}
            for one in participants
        ],
        "project": {"id": project.get("id"), "name": project.get("name"), "path": str(root)},
        "transaction_id": transaction_ids[-1] if transaction_ids else "",
        "transaction_ids": transaction_ids,
        "changed": all_changed,
        "goal_complete": goal_complete,
        "verified": False,
        "verification_status": "provider_consensus_unverified" if goal_complete else "incomplete",
        "mutation_recovery": mutation_recovery,
        "plan_rounds": plan_rounds,
        "work_passes": work_passes,
        "round_limit": round_limit,
        "stopped_because": work_stopped_because,
        "remaining": remaining,
    }
