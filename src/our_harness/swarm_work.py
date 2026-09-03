"""Explicit, provider-neutral collaboration and project-file work from the board.

Ordinary Send stays a faithful one-agent conversation. Explicit actions request
collaboration or project work, while an unmistakable file-changing request may
enter the same bounded work path only after the user confirms mutation. Provider text is never
treated as a command; file proposals cross the confined, baseline-checked
transaction boundary owned by Nexus.
"""

from __future__ import annotations

import copy
import ast
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import weakref
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from . import chat as chat_lab
from . import cancellation, collaboration_outcomes, swarm_runs, user_questions
from .changes import FileTransaction, atomic_write, file_sha256, sha256_bytes
from .agent_tools import AgentToolSession
from .collaboration_ledger import CollaborationLedger
from .config import LoadedConfig
from .detect import combined_commands, detect_project
from .execution import CommandRunner
from .indexer import WorkspaceIndexer
from .memory import MemoryStore
from .models import (
    ChangePlan, Deadline, DeadlineExpired, HarnessError,
    ProviderOutcomeUnknown, ResponseFormat,
)
from .safety import confined_path
from .redaction import bounded_redacted_text
from .swarm import SwarmError, may_they_talk
from .verification import analyze_verification
from .windows_containment import (
    appcontainer_available, run_appcontainer, verification_runtime_profile,
)
from .verification_python import (
    VerificationPythonUnavailable,
    discover_packaged_runtime,
    snapshot_dependency_paths,
    stage_source_runtime,
)
from .playwright_runtime import (
    discover_bundled_playwright_runtime,
    extract_safe_playwright_scenario,
    normalize_approved_https_base_url,
    run_brokered_playwright_appcontainer,
    run_brokered_playwright_suite,
    run_safe_playwright_scenario,
)


Progress = Callable[[str, str], None]
LiveTurn = Callable[[dict[str, Any]], None]
_active_mutation_sagas: weakref.WeakValueDictionary[str, Any] = weakref.WeakValueDictionary()


class ResumableSwarmError(SwarmError):
    """A paused orchestration failure whose recovery identity must reach clients."""

    def __init__(self, message: str, payload: dict[str, Any]) -> None:
        super().__init__(message)
        self.payload = payload


class StructuredCollaborationError(HarnessError):
    """A provider reply arrived, but its Nexus control payload is invalid."""


class ContextToolBudgetExhausted(DeadlineExpired):
    """Only active context-tool execution time exhausted its user-set budget."""


def _report(progress: Progress | None, stage: str, detail: str = "") -> None:
    if progress:
        progress(stage, detail)


def _show_turn(live_turn: LiveTurn | None, turn: dict[str, Any]) -> None:
    if live_turn:
        live_turn(turn)


def _provider_reason(ledger: CollaborationLedger, cause: Exception | None) -> str:
    if cause is None:
        return ""
    return bounded_redacted_text(
        ledger.redactor, chat_lab._in_plain_words(cause), 65_536
    ).strip()


def _provider_failure(
    ledger: CollaborationLedger,
    one: dict[str, Any],
    cause: Exception,
    **details: Any,
) -> dict[str, Any]:
    """Normalize one adapter failure without letting it erase sibling replies."""

    reason = _provider_reason(ledger, cause)
    unknown = isinstance(cause, ProviderOutcomeUnknown)
    return {
        "id": str(one.get("id") or ""),
        "name": str(one.get("name") or "An agent"),
        "route": str(one.get("who") or ""),
        **({"outcome_unknown": True} if unknown else {}),
        **({"provider_reason": reason} if reason else {}),
        **details,
    }


def _delivery_fields(
    participants: list[dict[str, Any]],
    answered_agent_ids: set[str],
    provider_failures: list[dict[str, Any]],
    *,
    requested_mode: str,
    lead_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    outcome = collaboration_outcomes.build(
        participants,
        answered_agent_ids=answered_agent_ids,
        failures=provider_failures,
        requested_mode=requested_mode,
    )
    fields = collaboration_outcomes.result_fields(outcome)
    fields["collaborated_with"] = [
        {
            "id": one.get("id"),
            "name": one.get("name"),
            "route": one.get("who"),
        }
        for one in participants
        if str(one.get("id") or "") != str(lead_id or "")
        and str(one.get("id") or "") in answered_agent_ids
    ]
    return outcome, fields


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
    safe_reason = _provider_reason(ledger, cause)
    state: dict[str, Any] = {
        "stage": stage,
        "status": "paused",
        "failed_agents": [
            {
                "id": str(one.get("id") or ""),
                "name": str(one.get("name") or "An agent"),
                "route": str(one.get("who") or ""),
                "failure_code": "provider_turn_failed",
                **({"provider_reason": safe_reason} if safe_reason else {}),
            }
            for one in failed_agents
        ],
    }
    state.update(checkpoint or {})
    if safe_reason:
        state["provider_reason"] = safe_reason
    provisional_paths = _transaction_paths(mutation_root, transaction_ids or [])
    if mutation_saga is not None:
        state["mutation_recovery"] = mutation_saga.compensate("provider_failure")
    elif mutation_root is not None and transaction_ids:
        state["mutation_recovery"] = _rollback_transactions(mutation_root, transaction_ids)
    recovery = state.get("mutation_recovery")
    if provisional_paths:
        state["provisional_paths"] = provisional_paths
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
    if safe_reason:
        report += f" Provider reason: {safe_reason}"
    if isinstance(recovery, dict) and recovery.get("status") == "rolled_back":
        report += (
            " Nexus rolled back the provisional project changes from this interrupted run"
            + (": " + ", ".join(provisional_paths) if provisional_paths else "")
            + ". Those provisional changes are not applied."
        )
    elif isinstance(recovery, dict) and recovery.get("status") == "rollback_conflict":
        report += (
            " Nexus could not safely roll back every provisional project change because "
            "the files changed again outside this run; reconcile the recorded mutation conflict before continuing."
        )
    ledger.finish(
        report,
        complete=False,
        stopped_because="provider_unavailable",
        remaining=remaining,
        status="paused_provider",
        state={
            "resume_token": ledger.session_id,
            "checkpoint": dict(checkpoint or {}),
            "write_scope_restricted": bool(
                checkpoint.get("write_scope_restricted", False)
            ) if isinstance(checkpoint, dict) else False,
            **({
                "allowed_write_roots": list(checkpoint.get("allowed_write_roots", []))
            } if isinstance(checkpoint, dict) and isinstance(
                checkpoint.get("allowed_write_roots"), list
            ) else {}),
        },
    )
    payload = {
        "status": "paused_provider",
        "stopped_because": "provider_unavailable",
        "goal_complete": False,
        "verified": False,
        "resume_token": ledger.session_id,
        "questions": remaining,
        "remaining": remaining,
        "checkpoint": dict(checkpoint or {}),
        "allowed_write_roots": list(checkpoint.get("allowed_write_roots", []))
        if isinstance(checkpoint, dict) and isinstance(checkpoint.get("allowed_write_roots"), list)
        else [],
        "write_scope_restricted": bool(
            checkpoint.get("write_scope_restricted", False)
        ) if isinstance(checkpoint, dict) else False,
        "partial_provider_failure": report,
    }
    if cause is not None:
        raise ResumableSwarmError(report, payload) from cause
    raise ResumableSwarmError(report, payload)


def _pause_context_tool_budget(
    ledger: CollaborationLedger,
    budget: dict[str, Any],
    stage: str,
    *,
    checkpoint: dict[str, Any] | None = None,
    cause: Exception | None = None,
    mutation_root: Path | None = None,
    transaction_ids: list[str] | None = None,
    mutation_saga: _MutationSaga | None = None,
) -> None:
    """Pause a resumable run without misreporting tool time as provider failure."""

    state: dict[str, Any] = {
        "stage": stage,
        "status": "paused_tool_budget",
        "context_tool_budget": copy.deepcopy(budget),
    }
    state.update(checkpoint or {})
    provisional_paths = _transaction_paths(mutation_root, transaction_ids or [])
    if mutation_saga is not None:
        state["mutation_recovery"] = mutation_saga.compensate(
            "context_tool_budget_exhausted"
        )
    elif mutation_root is not None and transaction_ids:
        state["mutation_recovery"] = _rollback_transactions(
            mutation_root, transaction_ids
        )
    if provisional_paths:
        state["provisional_paths"] = provisional_paths
    ledger.record_state("context_tool_budget_exhausted", state)
    remaining = [
        "Use Reset tool time and resume on this saved run, or raise the displayed "
        "Context tool execution seconds setting (zero means unlimited), then resume."
    ]
    report = (
        "Nexus paused because the user-configured context-tool execution budget "
        "was exhausted. Provider thinking, network waits, user pauses, and time "
        "while Nexus was closed were not charged. The exact run remains resumable."
    )
    recovery = state.get("mutation_recovery")
    if isinstance(recovery, dict) and recovery.get("status") == "rolled_back":
        report += " Nexus safely rolled back provisional changes from the interrupted pass."
    elif isinstance(recovery, dict) and recovery.get("status") == "rollback_conflict":
        report += (
            " Nexus could not safely roll back every provisional change because a file "
            "changed outside this run; reconcile the recorded conflict before continuing."
        )
    finish_state = {
        "resume_token": ledger.session_id,
        "checkpoint": dict(checkpoint or {}),
        "context_tool_budget": copy.deepcopy(budget),
        "write_scope_restricted": bool(
            checkpoint.get("write_scope_restricted", False)
        ) if isinstance(checkpoint, dict) else False,
        **({
            "allowed_write_roots": list(checkpoint.get("allowed_write_roots", []))
        } if isinstance(checkpoint, dict) and isinstance(
            checkpoint.get("allowed_write_roots"), list
        ) else {}),
    }
    ledger.finish(
        report,
        complete=False,
        stopped_because="context_tool_budget_exhausted",
        remaining=remaining,
        status="paused_tool_budget",
        state=finish_state,
    )
    payload = {
        "status": "paused_tool_budget",
        "stopped_because": "context_tool_budget_exhausted",
        "goal_complete": False,
        "verified": False,
        "resume_token": ledger.session_id,
        "questions": [],
        "remaining": remaining,
        "checkpoint": dict(checkpoint or {}),
        "context_tool_budget": copy.deepcopy(budget),
        "allowed_write_roots": list(checkpoint.get("allowed_write_roots", []))
        if isinstance(checkpoint, dict) and isinstance(
            checkpoint.get("allowed_write_roots"), list
        ) else [],
        "write_scope_restricted": bool(
            checkpoint.get("write_scope_restricted", False)
        ) if isinstance(checkpoint, dict) else False,
    }
    if cause is not None:
        raise ResumableSwarmError(report, payload) from cause
    raise ResumableSwarmError(report, payload)


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
        "effect_paths": {
            "type": "array", "maxItems": 24,
            "items": {"type": "string", "maxLength": 240},
        },
    },
    "required": ["contribution", "message_to_lead", "needs_files"],
    "additionalProperties": False,
})


def _context_tool_call_schema(
    name: str, argument_properties: dict[str, Any], required_arguments: list[str],
) -> dict[str, Any]:
    """Describe one provider-neutral context-tool call without an open object."""

    return {
        "type": "object",
        "properties": {
            "call_id": {"type": "string", "maxLength": 160},
            "name": {"type": "string", "enum": [name]},
            "arguments": {
                "type": "object",
                "properties": argument_properties,
                "required": required_arguments,
                "additionalProperties": False,
            },
        },
        "required": ["call_id", "name", "arguments"],
        "additionalProperties": False,
    }


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
                    "delete": {"type": "boolean"},
                    "reason": {"type": "string", "maxLength": 1000},
                },
                "required": ["path", "reason"],
                "additionalProperties": False,
            },
        },
        "tool_calls": {
            "type": "array", "maxItems": 8,
            "items": {
                "anyOf": [
                    _context_tool_call_schema("list_tree", {
                        "path": {"type": "string"},
                        "max_depth": {"type": "integer", "minimum": 0, "maximum": 8},
                        "max_entries": {"type": "integer", "minimum": 1, "maximum": 500},
                    }, ["path", "max_depth", "max_entries"]),
                    _context_tool_call_schema("read_file", {
                        "path": {"type": "string"},
                        "start_line": {"type": "integer", "minimum": 1},
                        "end_line": {"type": "integer", "minimum": 1},
                        "max_bytes": {"type": "integer", "minimum": 1},
                    }, ["path", "start_line", "end_line", "max_bytes"]),
                    _context_tool_call_schema("search_workspace", {
                        "query": {"type": "string"},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 50},
                    }, ["query", "max_results"]),
                    _context_tool_call_schema(
                        "run_selected_verification", {}, [],
                    ),
                ],
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
        "effect_paths": {
            "type": "array", "maxItems": 24,
            "items": {"type": "string", "maxLength": 240},
        },
        "ready_to_execute": {"type": "boolean"},
        "remaining": {
            "type": "array", "maxItems": 12,
            "items": {"type": "string", "maxLength": 500},
        },
        "questions": {
            **copy.deepcopy(user_questions.QUESTIONS_SCHEMA),
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
    items = values if isinstance(values, list) else [values]
    return frozenset(
        re.sub(r"\s+", " ", str(one or "").strip().casefold())
        for one in items if str(one or "").strip()
    )


def _progress_terms_match(left: frozenset[str], right: frozenset[str]) -> bool:
    return left == right


def _canonical_progress_state(
    agent_id: str,
    complete: bool,
    failed: bool,
    value: dict[str, Any],
    files: object = None,
) -> tuple[
    str, str, frozenset[str], tuple[tuple[str, str], ...], frozenset[str]
]:
    """Return engine-owned canonical state, never a similarity score over prose.

    Provider prose is deliberately excluded.  ``remaining`` is retained only
    so the engine can attest a monotonic reduction, and structured checkpoints
    are retained only as stable ID/state pairs whose transitions the progress
    guard tracks.  Evidence wording itself never buys another round.
    """

    checkpoints: dict[str, str] = {}
    duplicate_checkpoint_ids: set[str] = set()
    raw_progress = value.get("progress", [])
    if isinstance(raw_progress, list):
        for item in raw_progress:
            if not isinstance(item, dict):
                continue
            identifier = re.sub(r"\s+", " ", str(item.get("id") or "").strip().casefold())
            checkpoint_state = re.sub(
                r"\s+", " ", str(item.get("state") or "").strip().casefold()
            )
            evidence = str(item.get("evidence") or "").strip()
            # A checkpoint without all three schema fields is merely another
            # provider claim. Duplicate IDs are ambiguous and therefore do not
            # become engine-tracked progress in this round.
            if not identifier or not checkpoint_state or not evidence:
                continue
            if identifier in duplicate_checkpoint_ids:
                continue
            if identifier in checkpoints:
                checkpoints.pop(identifier, None)
                duplicate_checkpoint_ids.add(identifier)
                continue
            checkpoints[identifier] = checkpoint_state
    state = {
        "complete": bool(complete),
        "failed": bool(failed),
    }
    return (
        agent_id,
        hashlib.sha256(
            json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        frozenset(),
        tuple(sorted(checkpoints.items())),
        _progress_terms(_remaining(value)),
    )


def _progress_states_match(left: tuple[Any, ...], right: tuple[Any, ...]) -> bool:
    if len(left) != len(right):
        return False
    for one, two in zip(left, right):
        if not isinstance(one, tuple) or not isinstance(two, tuple) or one[:2] != two[:2]:
            return False
        one_remaining = one[2] if len(one) > 2 and isinstance(one[2], frozenset) else frozenset()
        two_remaining = two[2] if len(two) > 2 and isinstance(two[2], frozenset) else frozenset()
        if not _progress_terms_match(one_remaining, two_remaining):
            return False
    return True


def _context_result_evidence_digest(
    state: dict[str, Any], relevance_terms: set[str] | None = None,
) -> str:
    """Hash semantic tool evidence while excluding replay/call identities."""

    result = state.get("result", {})
    if not isinstance(result, dict):
        return ""
    content = result.get("content", "")
    if not isinstance(content, str):
        content = json.dumps(
            content, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )
    if relevance_terms:
        haystack = (content + " " + json.dumps(
            state.get("arguments", {}), sort_keys=True, ensure_ascii=False,
        )).casefold()
        if not any(
            term and re.search(rf"(?<![\w]){re.escape(term.casefold())}(?![\w])", haystack)
            for term in relevance_terms
        ):
            return ""
    canonical_result = {
        "tool": state.get("name") or result.get("name"),
        "arguments_sha256": state.get("arguments_sha256"),
        "status": result.get("status"),
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "truncated": result.get("truncated") is True,
    }
    return hashlib.sha256(json.dumps(
        canonical_result, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")).hexdigest()


def _requirement_context_terms(contract: dict[str, Any]) -> set[str]:
    """Return bounded engine-derived terms for unresolved-goal context."""

    terms: set[str] = set()
    for requirement in contract.get("requirements", []):
        if not isinstance(requirement, dict):
            continue
        for field in ("effect_paths", "effect_roots"):
            for value in requirement.get(field, []):
                if not isinstance(value, str):
                    continue
                terms.add(value.casefold())
                terms.add(Path(value).stem.casefold())
        for field in ("acceptance_terms", "artifact_terms"):
            for value in requirement.get(field, []):
                if isinstance(value, str) and len(value) >= 3:
                    terms.add(value.casefold())
        identifier = str(requirement.get("id") or "")
        terms.update(
            token for token in re.findall(r"[^\W_][\w-]*", identifier.casefold())
            if len(token) >= 3 and token not in {"requirement", "artifact", "project", "effect"}
        )
    return {one for one in terms if one}


_CHECKPOINT_STATUS_RANKS = {
    "pending": 0,
    "started": 1,
    "investigating": 2,
    "in progress": 2,
    "working": 2,
    "implemented": 3,
    "ready": 3,
    "tested": 4,
    "validated": 4,
    "verified": 5,
    "complete": 6,
    "completed": 6,
    "done": 6,
}
_CHECKPOINT_TERMINAL_STATES = {"verified", "complete", "completed", "done"}


def _checkpoint_progress_value(state: str) -> tuple[str, int] | None:
    """Recognise deterministic monotonic checkpoint state, not arbitrary prose."""

    held = re.sub(r"[_-]+", " ", str(state or "").strip().casefold())
    held = re.sub(r"\s+", " ", held)
    if held in _CHECKPOINT_STATUS_RANKS:
        return "status", _CHECKPOINT_STATUS_RANKS[held]
    numbered = re.fullmatch(
        r"(?:(?:step|checkpoint|phase)\s*)?(\d+)(?:\s*(?:/|of)\s*(\d+))?",
        held,
    )
    if not numbered:
        return None
    current = int(numbered.group(1))
    total = int(numbered.group(2)) if numbered.group(2) else None
    if current < 0 or (total is not None and (total < 1 or current > total)):
        return None
    return (f"counter/{total}" if total is not None else "counter", current)


def _meaningful_checkpoint_advance(previous: str, current: str) -> bool:
    """True only for a forward engine-recognised or terminal transition."""

    before = _checkpoint_progress_value(previous)
    after = _checkpoint_progress_value(current)
    if after is None:
        return False
    normalized_current = re.sub(
        r"\s+", " ", re.sub(r"[_-]+", " ", current.strip().casefold())
    )
    if before is None:
        return normalized_current in _CHECKPOINT_TERMINAL_STATES
    return before[0] == after[0] and after[1] > before[1]


class _ProgressGuard:
    """Notice stable or oscillating actionable state without policing duration."""

    def __init__(self) -> None:
        self.recent: list[
            tuple[tuple[Any, ...], ...]
        ] = []
        self.identical_run = 0
        self._agents_seen: set[str] = set()
        self._last_remaining: dict[str, frozenset[str]] = {}
        self._tracked_checkpoint_ids: dict[str, set[str]] = {}
        self._checkpoint_last: dict[str, dict[str, str]] = {}
        self._checkpoint_seen_states: dict[str, dict[str, set[str]]] = {}
        self._attested_epochs: dict[str, int] = {}

    def _attest(
        self, state: tuple[tuple[Any, ...], ...],
    ) -> tuple[tuple[Any, ...], ...]:
        """Project provider structure into monotonic engine-observed progress.

        Arbitrary new prose or rotating checkpoint IDs cannot keep a run alive.
        A round advances only when an existing unresolved set shrinks or a
        stable checkpoint ID reaches a state that ID has never visited before.
        Revisiting A/B states remains an oscillation and is stopped.
        """

        attested: list[tuple[Any, ...]] = []
        for item in state:
            if len(item) < 2:
                continue
            agent_id = str(item[0])
            base = item[1]
            remaining = (
                item[4] if len(item) > 4 and isinstance(item[4], frozenset)
                else frozenset()
            )
            checkpoints = dict(
                item[3] if len(item) > 3 and isinstance(item[3], tuple) else ()
            )
            first = agent_id not in self._agents_seen
            self._agents_seen.add(agent_id)
            advanced = False
            previous_remaining = self._last_remaining.get(agent_id, frozenset())
            if not first and previous_remaining and remaining < previous_remaining:
                advanced = True
            self._last_remaining[agent_id] = remaining

            tracked = self._tracked_checkpoint_ids.setdefault(agent_id, set())
            last = self._checkpoint_last.setdefault(agent_id, {})
            seen = self._checkpoint_seen_states.setdefault(agent_id, {})
            if not tracked and checkpoints:
                # Establish stable identities without treating the provider's
                # first claim as completed work.
                tracked.update(checkpoints)
            current_terms: set[str] = set()
            for identifier in sorted(tracked):
                current = checkpoints.get(identifier, "<missing>")
                previous = last.get(identifier)
                visited = seen.setdefault(identifier, set())
                if (
                    previous is not None
                    and current != previous
                    and current != "<missing>"
                    and current not in visited
                    and _meaningful_checkpoint_advance(previous, current)
                ):
                    advanced = True
                if current != "<missing>":
                    visited.add(current)
                last[identifier] = current
            epoch = self._attested_epochs.get(agent_id, 0) + (1 if advanced else 0)
            self._attested_epochs[agent_id] = epoch
            current_terms.add(f"attested-epoch:{epoch}")
            attested.append((agent_id, base, frozenset(current_terms)))
        return tuple(attested)

    def stalled(
        self,
        state: tuple[tuple[Any, ...], ...],
    ) -> bool:
        state = self._attest(state)
        same_as_last = bool(self.recent) and _progress_states_match(self.recent[-1], state)
        self.identical_run = self.identical_run + 1 if same_as_last else 1
        self.recent.append(state)
        if len(self.recent) > 12:
            self.recent.pop(0)
        # Long-horizon work defaults toward continuation. Fourteen identical
        # engine-owned states are required for a stable loop. A/B oscillation
        # needs four complete cycles; shorter patterns remain legitimate
        # legitimate attempt sequence.
        alternating = len(self.recent) >= 8 and all(
            _progress_states_match(self.recent[-8 + index], self.recent[-6 + index])
            for index in range(6)
        ) and not _progress_states_match(self.recent[-1], self.recent[-2])
        return self.identical_run >= 14 or alternating

_DIRECT_COLLABORATION = re.compile(
    r"\b(?:work\s+together|collaborat(?:e|ion|ively)?|ask\s+(?:the\s+)?(?:other|connected)\s+agents?"
    r"|both\s+of\s+you|all\s+of\s+you|team\s+up|peer\s+review|second\s+opinion|compare\s+your\s+answers)\b",
    re.IGNORECASE,
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
    *, participant_ids: list[str] | None = None,
) -> str:
    """Truthful board identity for every normal chat provider.

    ``peer_id`` predates durable saved-chat membership: an empty value means
    every ready peer on a green line. ``participant_ids`` is the stronger
    conversation boundary. When supplied, surrounding board connections are
    never disclosed as members of this exact chat.
    """

    lead = _agent(board, agent_id)
    project_ids = {
        str(line.get("project")) for line in board.get("works_on", [])
        if isinstance(line, dict) and line.get("agent") == agent_id
    }
    projects = [
        one for one in board.get("projects", [])
        if isinstance(one, dict) and str(one.get("id")) in project_ids
    ]
    if participant_ids is None:
        participants = _participants(board, lead, peer_id)
    else:
        allowed = {str(one) for one in participant_ids if str(one)}
        if str(lead.get("id") or "") not in allowed:
            raise SwarmError("This saved chat does not include the selected agent.")
        participants = [lead] if len(allowed) == 1 else [
            one for one in _participants(board, lead, peer_id)
            if str(one.get("id") or "") in allowed
        ]
    peers = [one for one in participants if one is not lead]
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

    # Selecting a pair describes who is available in this conversation, not
    # permission to turn every message into a multi-round team ritual.  Send
    # remains a faithful one-provider relay. The explicit collaboration and
    # project-work actions above are the only automatic expansion points.
    reason = (
        "The request did not explicitly ask Nexus to involve another agent, so Send stayed direct."
    )
    _report(progress, f"Waiting for {lead.get('name')}", reason)
    return {
        "mode": "chat",
        "reason": reason,
    }


def mentions_project_scope(text: str) -> bool:
    """Whether an explicit Work action actually names project/file subject matter."""

    return bool(_PROJECT_SCOPE.search(str(text or "")))


def _contribution(
    one: dict[str, Any], answer: dict[str, Any], phase: str, text: str,
    *, recipient_id: str = "", recipient_name: str = "Team deliberation",
    semantic: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = semantic if isinstance(semantic, dict) else answer
    semantic_state = {
        key: copy.deepcopy(source[key])
        for key in (
            "goal_complete", "ready_to_execute", "remaining", "questions",
            "progress", "needs_files", "effect_paths",
        )
        if key in source
    }
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
        **({"semantic_state": semantic_state} if semantic_state else {}),
    }


def _actual_conversation(contributions: list[dict[str, Any]]) -> str:
    transcript = "\n\n".join(
        f"{one.get('speaker_name') or 'An agent'} ({one.get('speaker_route') or 'unknown route'}):\n"
        f"{one.get('text') or ''}"
        for one in contributions
    )
    # This is used both for the durable report and for later team turns.  A
    # tail slice silently changed what happened in long sessions and made a
    # resumed run reason from an incomplete conversation.  Provider preflight
    # now owns any real transport failure, so preserve the canonical text.
    return transcript


PROMPT_TRANSCRIPT_CHARACTERS = int(
    chat_lab.LONG_HORIZON_CONTEXT_POLICY["prompt_transcript_characters"]
)
PROMPT_SEMANTIC_SUMMARY_CHARACTERS = int(
    chat_lab.LONG_HORIZON_CONTEXT_POLICY["semantic_summary_characters"]
)
_SEMANTIC_HISTORY_MARKER = re.compile(
    r"\b(?:acceptance|blocker|constraint|decision|evidence|fact|must|never|path|"
    r"requirement|remaining|sentinel|test|verif(?:y|ied|ication))\b",
    re.IGNORECASE,
)


def _semantic_history_summary(
    contributions: list[dict[str, Any]], maximum: int,
) -> str:
    """Keep deterministic semantic evidence from turns outside the recent tail.

    This is a projection of quoted, untrusted conversation evidence, never new
    system authority. Structured progress state is preferred. Marker-bearing
    excerpts preserve older requirements, decisions, facts, and paths instead
    of replacing their meaning with only a hash and a count.
    """

    candidates_by_turn: list[list[str]] = []
    for index, one in enumerate(contributions, start=1):
        turn_candidates: list[str] = []
        identity = (
            f"turn {index} · {one.get('speaker_name') or 'unknown'} · "
            f"{one.get('phase') or 'unknown'}"
        )
        semantic_state = one.get("semantic_state")
        if isinstance(semantic_state, dict) and semantic_state:
            for key in sorted(semantic_state):
                held = semantic_state[key]
                values = held if isinstance(held, list) else [held]
                for value_index, value in enumerate(values[:24], start=1):
                    encoded = json.dumps(
                        value, sort_keys=True, separators=(",", ":"),
                        ensure_ascii=False,
                    )
                    turn_candidates.append(
                        f"- {identity} structured {key}[{value_index}]: {encoded}"
                    )
        text = re.sub(r"\s+", " ", str(one.get("text") or "")).strip()
        if not text:
            continue
        matches = list(_SEMANTIC_HISTORY_MARKER.finditer(text))
        for match in matches[:8]:
            start = max(0, match.start() - 180)
            end = min(len(text), match.end() + 420)
            excerpt = text[start:end].strip()
            if start:
                excerpt = "…" + excerpt
            if end < len(text):
                excerpt += "…"
            turn_candidates.append(
                f"- {identity} quoted semantic excerpt: {excerpt}"
            )
        if not matches:
            # Even an unlabelled early decision must leave semantic content,
            # not merely a digest. Bound the excerpt and keep it clearly quoted.
            excerpt = text[:500] + ("…" if len(text) > 500 else "")
            turn_candidates.append(f"- {identity} quoted excerpt: {excerpt}")
        candidates_by_turn.append(turn_candidates)

    # Current decisions and blockers must not disappear behind stale early
    # discussion.  Spend most of the fixed projection on omitted turns from
    # newest to oldest, while reserving a smaller foundation section for the
    # original requirements/constraints.  The canonical paged ledger remains
    # the complete authority for everything not projected here.
    recent_candidates = [
        candidate
        for turn_candidates in reversed(candidates_by_turn)
        for candidate in turn_candidates
    ]
    foundation_candidates = [
        candidate
        for turn_candidates in candidates_by_turn
        for candidate in turn_candidates
    ]
    header = (
        "DETERMINISTIC SEMANTIC SUMMARY OF OLDER TURNS "
        "(quoted untrusted history; canonical ledger remains authoritative)"
    )
    foundation_budget = max(0, min(maximum // 4, 10_000))
    recent_budget = max(0, maximum - len(header) - foundation_budget - 180)
    seen: set[str] = set()

    def select(candidates: list[str], budget: int) -> tuple[list[str], bool]:
        selected: list[str] = []
        used = 0
        capped = False
        for candidate in candidates:
            folded = candidate.casefold()
            if folded in seen:
                continue
            addition = "\n" + candidate
            if used + len(addition) > budget:
                capped = True
                continue
            seen.add(folded)
            selected.append(candidate)
            used += len(addition)
        return selected, capped

    newest, newest_capped = select(recent_candidates, recent_budget)
    foundation, foundation_capped = select(
        foundation_candidates, foundation_budget,
    )
    sections: list[str] = [header, "MOST RECENT OMITTED EVIDENCE FIRST"]
    sections.extend(newest)
    if foundation:
        sections.append("FOUNDATIONAL EARLY REQUIREMENTS AND CONSTRAINTS")
        sections.extend(foundation)
    result = "\n".join(sections)
    if newest_capped or foundation_capped:
        marker = (
            "\n[semantic summary cap reached; retrieve the canonical paged "
            "ledger for more]"
        )
        if len(result) + len(marker) <= maximum:
            result += marker
    return result


def _prompt_conversation(contributions: list[dict[str, Any]]) -> str:
    """Bound provider context without altering the canonical durable transcript.

    Earlier turns remain in the collaboration ledger and its paged projections.
    Prompts receive an engine-owned rolling summary plus as many newest complete
    turns as fit; no turn is silently clipped in the middle.
    """

    full = _actual_conversation(contributions)
    if len(full) <= PROMPT_TRANSCRIPT_CHARACTERS:
        return full
    counts: dict[str, int] = {}
    characters: dict[str, int] = {}
    for one in contributions:
        key = f"{one.get('speaker_name') or 'unknown'} / {one.get('phase') or 'unknown'}"
        counts[key] = counts.get(key, 0) + 1
        characters[key] = characters.get(key, 0) + len(str(one.get("text") or ""))
    summary = [
        "LONG-HORIZON TRANSCRIPT PROJECTION (one policy for discussion, planning, execution, verification, and final synthesis)",
        f"canonical_sha256: {hashlib.sha256(full.encode('utf-8')).hexdigest()}",
        f"total_turns: {len(contributions)}; total_characters: {len(full)}",
        f"prompt_character_limit: {PROMPT_TRANSCRIPT_CHARACTERS}; semantic_summary_reserve: {PROMPT_SEMANTIC_SUMMARY_CHARACTERS}",
    ]
    for key in sorted(counts):
        summary.append(f"- {key}: {counts[key]} turn(s), {characters[key]} character(s)")
    header = "\n".join(summary)
    remaining = (
        PROMPT_TRANSCRIPT_CHARACTERS - len(header)
        - PROMPT_SEMANTIC_SUMMARY_CHARACTERS - 500
    )
    kept: list[str] = []
    used = 0
    for one in reversed(contributions):
        block = (
            f"{one.get('speaker_name') or 'An agent'} ({one.get('speaker_route') or 'unknown route'}):\n"
            f"{one.get('text') or ''}"
        )
        if len(block) > remaining - used:
            break
        kept.append(block)
        used += len(block) + 2
    omitted = max(0, len(contributions) - len(kept))
    older = contributions[:omitted]
    semantic_summary = _semantic_history_summary(
        older, PROMPT_SEMANTIC_SUMMARY_CHARACTERS,
    )
    return (
        header
        + f"\n[older turns semantically summarized: {omitted}; newest complete turns below: {len(kept)}]\n\n"
        + semantic_summary
        + "\n\nNEWEST COMPLETE TURNS\n"
        + "\n\n".join(reversed(kept))
    )


def _prompt_summary_state(contributions: list[dict[str, Any]]) -> dict[str, Any]:
    canonical = _actual_conversation(contributions)
    return {
        "schema_version": 1,
        "turns": len(contributions),
        "canonical_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "canonical_characters": len(canonical),
        "prompt_characters": len(_prompt_conversation(contributions)),
        "history_authority": "paged_collaboration_ledger",
        "context_policy": copy.deepcopy(chat_lab.LONG_HORIZON_CONTEXT_POLICY),
    }


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

    def applied(self, transaction_id: str, manifest_sha256: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256):
            raise HarnessError("Applied transaction manifest digest is invalid")
        entry = next(
            one for one in self.value["transactions"]
            if one["transaction_id"] == transaction_id
        )
        entry["phase"] = "applied"
        entry["manifest_sha256"] = manifest_sha256
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


def _manifest_sha256(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _record_applied_transaction(
    ledger: CollaborationLedger, saga: _MutationSaga,
    transaction_id: str, manifest: dict[str, Any],
) -> str:
    if manifest.get("state") != "applied" or manifest.get("transaction_id") != transaction_id:
        raise HarnessError("Applied transaction evidence does not match its durable manifest")
    digest = _manifest_sha256(manifest)
    saga.applied(transaction_id, digest)
    ledger.record_state("mutation_manifest_applied", {
        "transaction_id": transaction_id,
        "manifest_sha256": digest,
        "saga_id": ledger.session_id,
    })
    return digest


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

    alternatives = schema.get("anyOf")
    if isinstance(alternatives, list):
        if alternatives and any(
            isinstance(alternative, dict)
            and not _schema_problem(value, alternative, path)
            for alternative in alternatives
        ):
            return ""
        return f"{path} does not match any allowed schema"

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
        minimum = schema.get("minLength")
        if isinstance(minimum, int) and len(value) < minimum:
            return f"{path} is too short"
        maximum = schema.get("maxLength")
        if isinstance(maximum, int) and len(value) > maximum:
            return f"{path} is too long"
        allowed = schema.get("enum")
        if isinstance(allowed, list) and value not in allowed:
            return f"{path} is not an allowed value"
        return ""
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return f"{path} must be an integer"
        minimum = schema.get("minimum")
        if isinstance(minimum, int) and value < minimum:
            return f"{path} is too small"
        maximum = schema.get("maximum")
        if isinstance(maximum, int) and value > maximum:
            return f"{path} is too large"
        return ""
    if kind == "boolean" and not isinstance(value, bool):
        return f"{path} must be true or false"
    return ""


def _decode(
    answer: dict[str, Any], label: str, response_format: ResponseFormat
) -> dict[str, Any]:
    raw = str(answer.get("text") or "").strip()
    # Consumer web renderers sometimes prefix the visible answer with a BOM or
    # a private-use formatting glyph (the observed Claude marker is U+E056).
    # Ignore only those leading marker code points; arbitrary prose remains a
    # hard schema failure.
    while raw and (
        raw[0] == "\ufeff"
        or 0xE000 <= ord(raw[0]) <= 0xF8FF
        or 0xF0000 <= ord(raw[0]) <= 0xFFFFD
        or 0x100000 <= ord(raw[0]) <= 0x10FFFD
    ):
        raw = raw[1:].lstrip()
    # API providers can enforce response_format natively; consumer web chats
    # cannot. ChatGPT and Gemini occasionally wrap an otherwise exact JSON
    # object in a Markdown JSON fence. Accept that presentation wrapper while
    # retaining the same strict schema boundary below. Arbitrary prose around a
    # payload is still rejected rather than silently reinterpreted as control.
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", raw, re.IGNORECASE | re.DOTALL)
    if fenced:
        raw = fenced.group(1).strip()
    else:
        # Some provider pages render their language badge as a standalone text
        # line instead of fence chrome. Accept exactly that label followed by
        # an otherwise exact JSON object. Same-line labels, arbitrary prose,
        # and text after the object still fail json.loads below.
        labelled = re.fullmatch(
            r"json[ \t]*\r?\n(?P<body>\{[\s\S]*\})", raw, re.IGNORECASE
        )
        if labelled:
            raw = labelled.group("body").strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StructuredCollaborationError(
            f"{label} did not return the structured collaboration result Nexus requested"
        ) from exc
    if not isinstance(value, dict):
        raise StructuredCollaborationError(
            f"{label} returned the wrong collaboration result shape"
        )
    problem = _schema_problem(value, response_format.schema)
    if problem:
        raise StructuredCollaborationError(
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
                "Return only one fenced ```json code block containing the JSON object, preserve "
                "the same substantive answer, escape every "
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
        return _decode(corrected, label, response_format)


def _natural_language_web_contribution(
    one: dict[str, Any], answer: dict[str, Any],
) -> str:
    """Return only visibly delivered web prose that is safe to keep as speech.

    Invalid JSON remains invalid control data: Nexus must not mine a partial
    object for completion, remaining-work, or peer-message fields. A genuine
    prose reply is still useful team speech, though, so preserve that exact
    provider text after the one strict format correction has failed.
    """

    if not str(one.get("who") or "").startswith("web:"):
        return ""
    raw = str(answer.get("text") or "").strip().lstrip("\ufeff")
    if not raw or not any(character.isalpha() for character in raw):
        return ""
    # JSON-/markup-shaped payloads are control data or provider-page chrome,
    # not natural-language turns. Keep the boundary conservative rather than
    # extracting a plausible-looking message from an invalid object.
    if raw.lstrip().startswith(("{", "[", "```", "<")):
        return ""
    return raw


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
    answered_agent_ids: set[str] = set()
    provider_failures: list[dict[str, Any]] = []

    def identity(one: dict[str, Any], paired_with: str) -> str:
        return board_context(
            board, str(one.get("id") or ""), paired_with, project_id,
        )

    _report(
        progress, f"Asking {lead.get('name')} what to relay",
        f"The user's request is addressed to {lead.get('name')}; Nexus has not contacted {peer.get('name')} yet.",
    )
    try:
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
    except cancellation.ChatCancelled:
        raise
    except Exception as exc:
        failure = _provider_failure(ledger, lead, exc, stage="lead_draft")
        provider_failures.append(failure)
        ledger.record_state("provider_transport_failure", {
            "stage": "lead_draft", "status": "paused",
            "failed_agents": [failure],
        })
        participant_outcome, delivery_fields = _delivery_fields(
            [lead, peer], answered_agent_ids, provider_failures,
            requested_mode="relay", lead_id=str(lead.get("id") or ""),
        )
        note = collaboration_outcomes.notice_text(participant_outcome)
        remaining = [
            f"Reconnect or reconcile {lead.get('name') or 'the lead agent'}'s provider turn before resuming."
        ]
        kept = chat_lab.keep_participant_outcome_exchange(
            config, str(lead.get("who") or ""), text,
            filed_as=filed_as or str(lead.get("name") or ""),
            participant_outcome=participant_outcome, attachments=public,
        )
        ledger.finish(
            note, complete=False, stopped_because="provider_unavailable",
            remaining=remaining,
        )
        return {
            **kept, **delivery_fields,
            "collaboration_ledger": ledger.describe(),
            "provider_failures": provider_failures,
            "partial_provider_failure": note,
            "goal_complete": False,
            "stopped_because": "provider_unavailable",
            "remaining": remaining,
            "relay_complete": False,
        }
    _ack_shared(ledger, lead)
    answered_agent_ids.add(str(lead.get("id") or ""))
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
    try:
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
    except cancellation.ChatCancelled:
        raise
    except Exception as exc:
        failure = _provider_failure(ledger, peer, exc, stage="peer_reply")
        provider_failures.append(failure)
        ledger.record_state("provider_transport_failure", {
            "stage": "peer_reply", "status": "paused",
            "failed_agents": [failure],
        })
        participant_outcome, delivery_fields = _delivery_fields(
            [lead, peer], answered_agent_ids, provider_failures,
            requested_mode="relay", lead_id=str(lead.get("id") or ""),
        )
        note = collaboration_outcomes.notice_text(participant_outcome)
        remaining = [
            f"Reconnect or reconcile {peer.get('name') or 'the peer agent'}'s provider turn before resuming."
        ]
        kept = chat_lab.keep_multiparty_exchange(
            config, str(lead.get("who") or ""), text,
            str(lead_answer.get("text") or ""),
            filed_as=filed_as or str(lead.get("name") or ""),
            lead=lead, final_speaker=lead, participants=[lead, peer],
            contributions=[], attachments=public,
            model=str(lead_answer.get("model") or ""),
            milliseconds=int(lead_answer.get("milliseconds") or 0),
            participant_outcome=participant_outcome,
        )
        _share_turn(ledger, _contribution(
            lead, lead_answer, "final_answer", str(lead_answer.get("text") or ""),
            recipient_name="User",
        ))
        ledger.finish(
            note, complete=False, stopped_because="partial_provider_failure",
            remaining=remaining,
        )
        return {
            **kept, **delivery_fields,
            "collaboration_ledger": ledger.describe(),
            "provider_failures": provider_failures,
            "partial_provider_failure": note,
            "goal_complete": False,
            "stopped_because": "partial_provider_failure",
            "remaining": remaining,
            "relay_complete": False,
        }
    _ack_shared(ledger, peer)
    answered_agent_ids.add(str(peer.get("id") or ""))
    peer_turn = _contribution(
        peer, peer_answer, "agent_reply", str(peer_answer.get("text") or ""),
        recipient_id=str(lead.get("id") or ""),
        recipient_name=str(lead.get("name") or "The lead agent"),
    )
    _show_turn(live_turn, {"who": "them", **peer_turn})
    _share_turn(ledger, peer_turn, {"stage": "final_report"})

    # The relay is already complete after two provider turns. Asking the lead
    # a third time merely to restate the exchange added latency, cost, and a
    # fresh failure point. Nexus can report the two saved, attributed turns
    # deterministically without inventing or paraphrasing agent speech.
    final = {
        "text": (
            f"Relay complete between {lead.get('name')} and {peer.get('name')}.\n\n"
            f"{lead.get('name')} sent:\n{lead_turn['text']}\n\n"
            f"{peer.get('name')} replied:\n{peer_turn['text']}"
        ),
        "model": "nexus/deterministic-relay-receipt", "milliseconds": 0,
    }
    nexus = {"id": "nexus", "name": "Nexus", "who": ""}
    participant_outcome, delivery_fields = _delivery_fields(
        [lead, peer], answered_agent_ids, provider_failures,
        requested_mode="relay", lead_id=str(lead.get("id") or ""),
    )
    kept = chat_lab.keep_multiparty_exchange(
        config, str(lead.get("who") or ""), text, str(final.get("text") or ""),
        filed_as=filed_as or str(lead.get("name") or ""),
        lead=lead, final_speaker=nexus, participants=[lead, peer],
        contributions=[lead_turn, peer_turn], attachments=public,
        model=final.get("model", ""), milliseconds=0,
        participant_outcome=participant_outcome,
    )
    ledger.record_state("deterministic_relay_receipt", {
        "speaker_id": "nexus", "speaker_name": "Nexus",
        "lead_id": str(lead.get("id") or ""),
        "peer_id": str(peer.get("id") or ""),
        "provider_calls": 2,
    })
    ledger.finish(
        str(final.get("text") or ""), complete=True,
        stopped_because="relay_complete",
    )
    return {
        **kept,
        **delivery_fields,
        "collaboration_ledger": ledger.describe(),
        "provider_failures": [],
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
        except cancellation.ChatCancelled:
            raise
        except Exception as exc:
            failure = _provider_failure(ledger, one, exc)
            return one, {
                "text": "", "milliseconds": 0, "model": "",
                "_provider_failed": True,
                "_provider_failure": failure,
            }
        _ack_shared(ledger, one)
        return one, answer

    completed: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    provider_failures: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=len(participants)) as pool:
        futures = [cancellation.submit(pool, first_round, one) for one in participants]
        for future in as_completed(futures):
            one, answer = future.result()
            completed[str(one.get("id"))] = (one, answer)
            if answer.get("_provider_failed"):
                provider_failures.append(dict(answer.get("_provider_failure") or {
                    "id": one.get("id"), "name": one.get("name"),
                    "route": one.get("who"),
                    "provider_reason": "The provider adapter failed before returning an answer.",
                    "outcome_unknown": False,
                }))
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
        successful_results = [
            (one, draft) for one, draft in (
                completed.get(str(candidate.get("id") or ""), (candidate, {}))
                for candidate in participants
            )
            if not draft.get("_provider_failed") and str(draft.get("text") or "").strip()
        ]
        selected_result = (
            lead_result if lead_result and not lead_answer.get("_provider_failed")
            else successful_results[0] if successful_results else None
        )
        responder, responder_answer = selected_result if selected_result else ({}, {})
        can_keep_lead = bool(selected_result)
        answered_ids = {
            str(one.get("id") or "") for one, _draft in successful_results
        }
        participant_outcome, delivery_fields = _delivery_fields(
            participants, answered_ids, provider_failures,
            requested_mode="collaborate", lead_id=str(lead.get("id") or ""),
        )
        ledger.record_state("provider_transport_failure", {
            "stage": "independent_first_round",
            "status": "degraded" if can_keep_lead else "paused",
            "failed_agents": [
                {
                    "id": str(one.get("id") or ""),
                    "name": str(one.get("name") or "An agent"),
                    "route": str(one.get("route") or ""),
                    "outcome_unknown": bool(one.get("outcome_unknown")),
                    "failure_code": "provider_turn_failed",
                    **({"provider_reason": str(one.get("provider_reason") or "")}
                       if one.get("provider_reason") else {}),
                }
                for one in provider_failures
            ],
        })
        remaining = [
            f"Reconnect or reconcile {name}'s provider turn before resuming."
            for name in failed_names
        ]
        if can_keep_lead:
            # A provider failure must not erase truthful work that another agent
            # already completed. This applies to explicit collaboration too:
            # strict all-or-nothing transport turned one unavailable peer into a
            # useless failed chat. Prefer the lead's answer, or the first healthy
            # peer's concrete contribution when the lead itself is unavailable.
            successful_peer_contributions = [
                _contribution(
                    one, draft, "agent_reply", str(draft.get("text") or ""),
                    recipient_id=str(lead.get("id") or ""),
                    recipient_name=str(lead.get("name") or "The lead agent"),
                )
                for one, draft in completed.values()
                if one.get("id") != lead.get("id")
                and one.get("id") != responder.get("id")
                and not draft.get("_provider_failed")
            ]
            kept = chat_lab.keep_multiparty_exchange(
                config,
                str(lead.get("who") or ""),
                text,
                str(responder_answer.get("text") or ""),
                filed_as=filed_as or str(lead.get("name") or ""),
                lead=lead,
                final_speaker=responder,
                participants=participants,
                contributions=successful_peer_contributions,
                attachments=public,
                model=str(responder_answer.get("model") or ""),
                milliseconds=int(responder_answer.get("milliseconds") or 0),
                participant_outcome=participant_outcome,
            )
            final_turn = _contribution(
                responder, responder_answer, "final_answer",
                str(responder_answer.get("text") or ""),
                recipient_name="User",
            )
            _share_turn(ledger, final_turn)
            partial_note = (
                f"{responder.get('name') or 'A connected agent'} answered. "
                + ", ".join(failed_names)
                + " could not join this turn. "
                + collaboration_outcomes.notice_text(participant_outcome)
            )
            partial_reasons = [
                f"{one.get('name')}: {one.get('provider_reason')}"
                for one in provider_failures if one.get("provider_reason")
            ]
            if partial_reasons:
                partial_note += " Provider reason: " + " | ".join(partial_reasons)
            ledger.finish(
                partial_note, complete=False,
                stopped_because="partial_provider_failure",
                remaining=remaining,
            )
            return {
                **kept,
                "collaboration_ledger": ledger.describe(),
                **delivery_fields,
                "provider_failures": provider_failures,
                "partial_provider_failure": partial_note,
                "goal_complete": False,
                "discussion_rounds": 0,
                "round_limit": round_limit,
                "stopped_because": "partial_provider_failure",
                "remaining": remaining,
            }
        report = collaboration_outcomes.notice_text(participant_outcome)
        reasons = [
            f"{one.get('name')}: {one.get('provider_reason')}"
            for one in provider_failures if one.get("provider_reason")
        ]
        if reasons:
            report += " Provider reason: " + " | ".join(reasons)
        kept = chat_lab.keep_participant_outcome_exchange(
            config,
            str(lead.get("who") or ""),
            text,
            filed_as=filed_as or str(lead.get("name") or ""),
            participant_outcome=participant_outcome,
            attachments=public,
        )
        ledger.finish(
            report, complete=False, stopped_because="provider_unavailable",
            remaining=remaining,
        )
        return {
            **kept,
            "collaboration_ledger": ledger.describe(),
            **delivery_fields,
            "provider_failures": provider_failures,
            "partial_provider_failure": report,
            "goal_complete": False,
            "discussion_rounds": 0,
            "round_limit": round_limit,
            "stopped_because": "provider_unavailable",
            "remaining": remaining,
        }
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
    answered_agent_ids = {
        str(one.get("id") or "") for one, _draft in drafts
    }
    _report(
        progress, "Starting goal-directed team discussion",
        "Each agent will see the real conversation so far and the team will continue until everyone reports the goal complete."
    )
    goal_complete = False
    remaining: list[str] = []
    discussion_rounds = 0
    stopped_because = ""
    progress_guard = _ProgressGuard()
    active_participants = list(participants)
    degraded_provider_failures: list[dict[str, Any]] = []
    for round_number in _round_numbers(round_limit):
        discussion_rounds = round_number
        cycle_complete = True
        cycle_remaining: list[str] = []
        cycle_state: list[tuple[Any, ...]] = []
        ledger.record_state("prompt_context_checkpoint", {
            "stage": "discussion", "round": round_number,
            **_prompt_summary_state(contributions),
        })
        _report(
            progress, f"Team discussion round {round_number}",
            "Agents are responding in board order, so every later reply sees every earlier reply."
        )
        for one in list(active_participants):
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
                + _prompt_conversation(contributions)
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
            answer: dict[str, Any] = {}
            failed = False
            degraded_protocol = False
            failure_cause: Exception | None = None
            try:
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
            except cancellation.ChatCancelled:
                raise
            except Exception as exc:
                failure_cause = exc
            else:
                # Ledger integrity is not a provider outcome. A ledger failure
                # here must escape rather than being converted into a repair-
                # provider status for this participant.
                _ack_shared(ledger, one)
                try:
                    value = _decode_with_one_web_repair(
                        config, one, answer, DISCUSSION_FORMAT, ledger,
                        conversation_key, prefer_existing_conversation,
                    )
                    message = str(value.get("message") or "").strip()
                    one_remaining = _remaining(value)
                    one_complete = value.get("goal_complete") is True and not one_remaining
                except cancellation.ChatCancelled:
                    raise
                except Exception as exc:
                    failure_cause = exc
            if failure_cause is not None:
                exc = failure_cause
                failed_name = str(one.get("name") or "An agent")
                safe_reason = _provider_reason(ledger, exc)
                protocol_failure = isinstance(exc, StructuredCollaborationError)
                outcome_unknown = isinstance(exc, ProviderOutcomeUnknown)
                delivered_text = (
                    _natural_language_web_contribution(one, answer)
                    if protocol_failure else ""
                )
                usable_contribution = bool(delivered_text)
                correction_note = (
                    " even after one format correction"
                    if str(one.get("who") or "").startswith("web:") else ""
                )
                report = (
                    f"{failed_name}'s delivered reply did not match the collaboration format"
                    f"{correction_note}."
                    if protocol_failure else
                    f"{failed_name} could not complete this provider turn."
                )
                if safe_reason:
                    report += f" Provider reason: {safe_reason}"
                ledger.record_state(
                    "provider_protocol_failure" if protocol_failure
                    else "provider_transport_failure", {
                    "stage": "team_discussion",
                    "round": round_number,
                    "status": "degraded",
                    "failed_agent": {
                        "id": str(one.get("id") or ""),
                        "name": failed_name,
                        "route": str(one.get("who") or ""),
                    },
                    "failure_code": "invalid_structured_result" if protocol_failure
                    else "provider_turn_failed",
                    "outcome_unknown": outcome_unknown,
                    "usable_contribution": usable_contribution,
                    **({"provider_reason": safe_reason} if safe_reason else {}),
                })
                degraded_provider_failures.append({
                    "id": one.get("id"), "name": failed_name,
                    "route": one.get("who"), "round": round_number,
                    "kind": "protocol" if protocol_failure else "transport",
                    "outcome_unknown": outcome_unknown,
                    "usable_contribution": usable_contribution,
                    **({"provider_reason": safe_reason} if safe_reason else {}),
                })
                if usable_contribution:
                    # The provider genuinely answered, but Nexus cannot trust
                    # the failed schema as machine control state. Keep the
                    # exact prose as team speech, keep the agent reachable,
                    # and conservatively claim neither completion nor progress.
                    failed = True
                    degraded_protocol = True
                    value = {}
                    message = delivered_text
                    one_remaining = []
                    one_complete = False
                    _report(
                        progress, f"Keeping {failed_name}'s delivered reply",
                        report + " Nexus kept the exact natural-language contribution, "
                        "but did not infer completion, remaining work, or peer text from it.",
                    )
                else:
                    active_participants = [
                        candidate for candidate in active_participants
                        if candidate.get("id") != one.get("id")
                    ]
                    _report(
                        progress, f"Continuing without {failed_name}",
                        report + " Nexus preserved the completed team work and kept the healthy agents running.",
                    )
                    continue
            contribution = _contribution(
                one, answer, "agent_discussion", message,
                recipient_name="Team deliberation",
                semantic=value,
            )
            if degraded_protocol:
                contribution["structured_state_unavailable"] = True
            contributions.append(contribution)
            _show_turn(live_turn, {"who": "them", **contribution})
            _share_turn(ledger, contribution, {
                "stage": "team_discussion",
                "round": round_number,
                "speaker_complete": one_complete,
                "speaker_remaining": one_remaining,
                "structured_state_unavailable": degraded_protocol,
            })
            cycle_remaining.extend(one_remaining)
            cycle_complete = cycle_complete and one_complete
            cycle_state.append(_canonical_progress_state(
                str(one.get("id") or ""), one_complete, failed, value
            ))
        if not active_participants:
            remaining.append("No connected agent remains available to continue the team conversation.")
            stopped_because = "provider_unavailable"
            break
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

    def preserved_transcript_report(why: str) -> dict[str, Any]:
        latest = next((
            contribution for contribution in reversed(contributions)
            if str(contribution.get("text") or "").strip()
        ), {})
        source_name = str(latest.get("speaker_name") or "an agent")
        source_text = str(
            latest.get("text") or "The team stopped before a final report was generated."
        )
        return {
            "text": (
                f"Nexus preserved the completed team transcript, but {why}. "
                f"The latest real contribution was from {source_name}:\n\n{source_text}\n\n"
                f"Nexus status: {'complete' if goal_complete else 'incomplete'}"
                + (f". Remaining: {'; '.join(remaining)}" if remaining else ".")
            ),
            "model": "nexus/deterministic-fallback", "milliseconds": 0,
        }

    reporter: dict[str, Any] = {}
    if active_participants:
        reporter = next((
            one for one in active_participants if one.get("id") == lead.get("id")
        ), active_participants[0])
        _report(
            progress, f"Waiting for {reporter.get('name')} to report the outcome",
            "The lead agent is preparing a truthful completion report from the full visible discussion."
        )
        final_speaker = reporter
        try:
            final = chat_lab.ask_once(
                config,
                str(reporter.get("who") or ""),
                _continuation_turn(
                    "FINAL TEAM REPORT",
                    "Give the user the current team outcome from the completed conversation and Nexus completion state.",
                ),
                context=(
                    board_context(board, str(reporter.get("id") or ""), peer_id, project_id)
                    + "\n\nORIGINAL USER GOAL\n" + text
                    + "\n\nFULL ACTUAL TEAM CONVERSATION\n"
                    + _prompt_conversation(contributions)
                    + f"\n\nNEXUS COMPLETION STATE: {'complete' if goal_complete else 'incomplete'}"
                    + f"\nNEXUS STOP REASON: {stopped_because}"
                    + ("\nREMAINING WORK: " + "; ".join(remaining) if remaining else "")
                    + "\n\nGive the user a truthful final report. Name disagreements plainly. "
                      "If Nexus says incomplete, explicitly say the goal is incomplete and list what remains; do not present discussion or suggested work as completed work."
                    + ("\n\n" + attachment_text if attachment_text else "")
                    + _shared_context(ledger, reporter, {
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
        except cancellation.ChatCancelled:
            raise
        except Exception as exc:
            safe_reason = _provider_reason(ledger, exc)
            outcome_unknown = isinstance(exc, ProviderOutcomeUnknown)
            degraded_provider_failures.append({
                "id": reporter.get("id"), "name": reporter.get("name"),
                "route": reporter.get("who"), "kind": "final_report",
                "outcome_unknown": outcome_unknown,
                **({"provider_reason": safe_reason} if safe_reason else {}),
            })
            final = preserved_transcript_report(
                f"{reporter.get('name') or 'the reporter'} could not generate the final synthesis"
            )
            final_speaker = {"id": "nexus", "name": "Nexus", "who": ""}
        else:
            # A collaboration-ledger failure is an engine-integrity failure,
            # not evidence that the provider failed. Keep it outside the
            # provider exception boundary so it remains visible and fail-closed.
            _ack_shared(ledger, reporter)
    else:
        # Every provider already failed or has an uncertain delivery outcome.
        # Do not silently retry one of those turns merely to obtain a final
        # report: preserve the received transcript and make reconciliation a
        # deliberate user action.
        final = preserved_transcript_report(
            "no connected agent remained available to generate the final synthesis"
        )
        final_speaker = {"id": "nexus", "name": "Nexus", "who": ""}
    participant_outcome, delivery_fields = _delivery_fields(
        participants, answered_agent_ids, degraded_provider_failures,
        requested_mode="collaborate", lead_id=str(lead.get("id") or ""),
    )
    delivery_complete = participant_outcome["outcome"] == "complete"
    delivery_note = (
        "" if delivery_complete
        else collaboration_outcomes.notice_text(participant_outcome)
    )
    kept = chat_lab.keep_multiparty_exchange(
        config,
        str(lead.get("who") or ""),
        text,
        final["text"],
        filed_as=filed_as or str(lead.get("name") or ""),
        lead=lead,
        final_speaker=final_speaker,
        participants=participants,
        contributions=contributions,
        attachments=public,
        model=final.get("model", ""),
        milliseconds=int((time.monotonic() - began) * 1000),
        participant_outcome=participant_outcome,
    )
    final_turn = _contribution(
        final_speaker, final, "final_answer", str(final.get("text") or ""),
        recipient_name="User",
    )
    if str(final_speaker.get("id") or "") == "nexus":
        ledger.record_state("deterministic_final_report_fallback", {
            "speaker_id": "nexus", "speaker_name": "Nexus",
            "failed_reporter_id": str(reporter.get("id") or ""),
            "reason": "provider_failure" if reporter else "no_active_participant",
            "goal_complete": goal_complete,
        })
    else:
        _share_turn(ledger, final_turn)
    # Keep a concrete orchestration stop (round limit, stalled, or no provider
    # remaining) while the structured delivery status independently exposes
    # degradation. A delivery failure replaces only a nominal completion.
    delivery_stop = (
        "partial_provider_failure"
        if not delivery_complete and stopped_because in {"", "complete"}
        else stopped_because or "goal_complete"
    )
    ledger.finish(
        delivery_note or str(final.get("text") or ""),
        complete=goal_complete and delivery_complete,
        stopped_because=delivery_stop,
        remaining=remaining,
    )
    return {
        **kept,
        "collaboration_ledger": ledger.describe(),
        **delivery_fields,
        "goal_complete": goal_complete and delivery_complete,
        "discussion_rounds": discussion_rounds,
        "round_limit": round_limit,
        "stopped_because": delivery_stop,
        "remaining": remaining,
        "provider_failures": degraded_provider_failures,
        **({"partial_provider_failure": delivery_note} if delivery_note else {}),
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
    """Return a deterministic, honest project manifest including directories.

    Earlier manifests silently removed the 81st sibling directory and never
    showed empty directories. That made a valid required destination look as
    if it did not exist. The manifest may still be bounded for provider
    context, but traversal is not pruned and the exact omitted counts are
    disclosed.
    """

    directories_found: list[str] = []
    files_found: list[str] = []
    skipped = {".git", ".harness", "node_modules", ".venv", "venv", "dist", "build"}
    for folder, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(one for one in directories if one not in skipped)
        base = Path(folder)
        for name in directories:
            path = base / name
            try:
                if path.is_symlink():
                    continue
                directories_found.append(path.relative_to(root).as_posix() + "/")
            except (OSError, ValueError):
                continue
        for name in sorted(files):
            path = base / name
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                files_found.append(path.relative_to(root).as_posix())
            except (OSError, ValueError):
                continue

    directory_limit = 2_000
    file_limit = 2_000
    shown = [
        "PROJECT MANIFEST (directories end in /)",
        f"[manifest totals: {len(directories_found)} directories, {len(files_found)} files]",
    ]
    omitted_directories = max(0, len(directories_found) - directory_limit)
    omitted_files = max(0, len(files_found) - file_limit)
    if omitted_directories or omitted_files:
        shown.append(
            "[manifest truncated: "
            f"{omitted_directories} directorie(s) and {omitted_files} file(s) omitted; "
            "request exact paths, glob:<pattern>, or dir:<path> for on-demand retrieval]"
        )
    shown.extend(directories_found[:directory_limit])
    shown.extend(files_found[:file_limit])
    return "\n".join(shown) if directories_found or files_found else "[empty project]"


def _safe_query_paths(root: Path, query: str) -> tuple[list[Path], str]:
    """Resolve one explicit provider retrieval query without escaping root."""

    raw = str(query or "").strip()
    if not raw:
        return [], "empty request"
    if raw.startswith("dir:"):
        relative = raw[4:].strip() or "."
        try:
            folder = confined_path(root, relative, allow_missing=False)
        except HarnessError as exc:
            return [], str(exc)
        if not folder.is_dir() or folder.is_symlink():
            return [], f"directory is not readable: {relative}"
        entries = sorted(folder.iterdir(), key=lambda item: item.name.casefold())
        listing = "\n".join(
            item.relative_to(root).as_posix() + ("/" if item.is_dir() else "")
            for item in entries[:500]
            if not item.is_symlink()
        )
        suffix = "" if len(entries) <= 500 else f"\n[dir listing truncated: {len(entries) - 500} omitted]"
        return [], f"DIR {relative}\n{listing or '[empty directory]'}{suffix}"
    if raw.startswith("glob:"):
        pattern = raw[5:].strip().replace("\\", "/")
        if not pattern or Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            return [], f"unsafe glob request rejected: {pattern or '(empty)'}"
        matches: list[Path] = []
        try:
            candidates = root.glob(pattern)
            for candidate in candidates:
                confined_path(root, candidate.relative_to(root), allow_missing=False)
                if candidate.is_file() and not candidate.is_symlink():
                    matches.append(candidate)
                if len(matches) >= 100:
                    break
        except (HarnessError, OSError, ValueError) as exc:
            return [], f"glob request failed: {exc}"
        return sorted(matches), "" if matches else f"glob matched no readable files: {pattern}"
    try:
        return [confined_path(root, raw, allow_missing=False)], ""
    except HarnessError as exc:
        return [], str(exc)


def _requested_files(root: Path, plans: list[tuple[dict[str, Any], dict[str, Any]]]) -> str:
    wanted: list[str] = []
    for _agent_row, plan in plans:
        raw = plan.get("needs_files", [])
        if isinstance(raw, list):
            wanted.extend(str(one) for one in raw if isinstance(one, str))
    blocks: list[str] = []
    diagnostics: list[str] = []
    used = 0
    queries = list(dict.fromkeys(wanted))
    for relative in queries[:60]:
        paths, diagnostic = _safe_query_paths(root, relative)
        if diagnostic.startswith("DIR "):
            blocks.append(diagnostic)
        elif diagnostic:
            diagnostics.append(f"{relative}: {diagnostic}")
        for path in paths:
            try:
                if not path.is_file() or path.is_symlink():
                    diagnostics.append(f"{relative}: not a readable regular file")
                    continue
                size = path.stat().st_size
                if size > 500_000:
                    diagnostics.append(f"{relative}: omitted because it is {size} bytes (per-file limit 500000)")
                    continue
                content = path.read_text(encoding="utf-8", errors="strict")
            except UnicodeDecodeError:
                diagnostics.append(f"{relative}: omitted because it is not valid UTF-8 text")
                continue
            except OSError as exc:
                diagnostics.append(f"{relative}: could not read ({exc})")
                continue
            if used + len(content) > 300_000:
                diagnostics.append(
                    f"{relative}: omitted because the 300000-character retrieval budget was exhausted"
                )
                continue
            actual_relative = path.relative_to(root).as_posix()
            blocks.append(f"FILE {actual_relative}\n{content}")
            used += len(content)
    if len(queries) > 60:
        diagnostics.append(f"{len(queries) - 60} retrieval request(s) omitted after the explicit 60-query limit")
    if diagnostics:
        blocks.append("RETRIEVAL DIAGNOSTICS (nothing was silently omitted)\n" + "\n".join(diagnostics))
    return "\n\n".join(blocks) or "[No existing file content was requested.]"


def _plan_words(value: dict[str, Any]) -> str:
    words = (
        f"Contribution: {value.get('contribution') or '(none)'}\n"
        f"Message to team: {value.get('message_to_lead') or '(none)'}\n"
        "Requested files: "
        + (", ".join(str(path) for path in value.get("needs_files", [])) or "none")
        + "\nExpected project effects: "
        + (", ".join(str(path) for path in value.get("effect_paths", [])) or "not yet path-specific")
    )
    if "ready_to_execute" in value or "remaining" in value:
        remaining = _remaining(value)
        words += (
            "\nExecution readiness: "
            + ("ready" if value.get("ready_to_execute") is True else "not ready")
            + "\nRemaining planning work: "
            + ("; ".join(remaining) or "none")
        )
    questions = value.get("questions")
    if isinstance(questions, list) and questions:
        words += "\nQuestions requiring the user: " + "; ".join(str(one) for one in questions)
    return words


def _normalized_write_authority(
    root: Path, values: object,
) -> tuple[list[str], list[str]]:
    """Return confined roots and every supplied root that failed confinement."""

    roots: list[str] = []
    rejected: list[str] = []
    if not isinstance(values, list):
        return roots, ([] if values is None else [str(values)])
    for value in values:
        raw = str(value or "").strip().strip("`\"'").replace("\\", "/")
        if not raw:
            rejected.append(str(value or ""))
            continue
        raw = re.split(r"\[[^\]]+\]", raw, maxsplit=1)[0].rstrip(" /.")
        try:
            candidate = Path(raw)
            if candidate.is_absolute():
                relative = candidate.resolve().relative_to(root.resolve()).as_posix()
            else:
                relative = confined_path(root, raw).relative_to(root).as_posix()
        except (HarnessError, OSError, ValueError):
            rejected.append(str(value or ""))
            continue
        if relative and relative != "." and relative not in roots:
            roots.append(relative.rstrip("/"))
        elif not relative or relative == ".":
            rejected.append(str(value or ""))
    return roots, rejected


def _normal_write_roots(root: Path, values: object) -> list[str]:
    return _normalized_write_authority(root, values)[0]


def _write_roots_from_goal(root: Path, text: str) -> list[str]:
    """Return exact prompt-authorized destinations, never mere mentioned paths."""

    return _path_authority_from_goal(root, text)["writable"]


def _absolute_prompt_paths(text: str) -> list[tuple[int, str]]:
    """Extract Windows paths with their line, preserving spaces and quoted names."""

    found: list[tuple[int, str]] = []
    for line_number, line in enumerate(str(text or "").splitlines()):
        quoted_spans: list[tuple[int, int]] = []
        for match in re.finditer(r"[\"`]([A-Za-z]:[\\/][^\"`\r\n]+)[\"`]", line):
            found.append((line_number, match.group(1).strip()))
            quoted_spans.append(match.span())
        for match in re.finditer(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]", line):
            if any(start <= match.start() < end for start, end in quoted_spans):
                continue
            raw = line[match.start():].strip().rstrip(" \t`\"').,;:")
            if raw:
                found.append((line_number, raw))
    return found


_WRITE_AUTHORITY = (
    re.compile(r"\b(?:here|there|in this folder)\s*[,;:]?\s+(?:we|you)\s+(?:may|can|will|must)\s+(?:make\s+changes|create|write|document|update)\b", re.I),
    re.compile(r"\b(?:will|must|may|can|should)\s+(?:all\s+)?(?:live|be created|be written|be stored)\s+in\b", re.I),
    re.compile(r"\bcreate\b[\s\S]{0,500}\b(?:in|inside|under)\s*:\s*", re.I),
    re.compile(r"\bcreate\b[^\n]{0,300}\bin\b[^\n]{0,160}\n", re.I),
    re.compile(r"\b(?:write|writable|output|upload)\s+(?:folder|directory|destination|root)\b", re.I),
    re.compile(r"\b(?:document|record)\s+every\s+session\b", re.I),
    re.compile(r"\bupdat(?:e|ing)\s+(?:of\s+)?(?:this|the)\s+vault\b", re.I),
    re.compile(r"\bwhere\s+we\s+will\s+create\b", re.I),
)
_READ_ONLY_AUTHORITY = (
    re.compile(r"\bread[- ]only\b", re.I),
    re.compile(r"\buntouched\s+version\b", re.I),
    re.compile(r"\b(?:may|must)\s+never\s+change\s+anything\s+here\b", re.I),
    re.compile(r"\b(?:do not|never)\s+(?:change|modify|write|touch)\b", re.I),
)
_REFERENCE_AUTHORITY = (
    re.compile(r"\b(?:another\s+separate|other)\s+project\b", re.I),
    re.compile(r"\b(?:use|as)\s+(?:an?\s+)?(?:inspiration|reference|example|baseline\s+to\s+reference)\b", re.I),
)


def _path_authority_from_goal(root: Path, text: str) -> dict[str, list[str]]:
    """Classify path mentions using their surrounding prompt section.

    Permission commonly appears in the paragraphs before or after a standalone
    absolute path. References and immutable snapshots are recorded separately;
    neither becomes writable merely because it appears beneath the project.
    """

    lines = str(text or "").splitlines()
    occurrences = _absolute_prompt_paths(text)
    writable: list[str] = []
    read_only: list[str] = []
    references: list[str] = []
    invalid_writable: list[str] = []
    for index, (line_number, raw) in enumerate(occurrences):
        previous_path_line = occurrences[index - 1][0] if index else -1
        next_path_line = occurrences[index + 1][0] if index + 1 < len(occurrences) else len(lines)
        before = max(previous_path_line + 1, line_number - 5, 0)
        after = min(next_path_line, line_number + 14, len(lines))
        context = "\n".join(lines[before:after]).strip()
        local_context = "\n".join(
            lines[max(previous_path_line + 1, line_number - 3, 0):min(
                next_path_line, line_number + 2, len(lines)
            )]
        ).strip()
        normal = _normal_write_roots(root, [raw])
        relative = normal[0] if normal else ""
        if not relative:
            # An out-of-project example is still useful diagnostic authority,
            # but can never cross the mechanical selected-root confinement.
            if any(pattern.search(local_context) for pattern in _REFERENCE_AUTHORITY):
                references.append(raw)
            elif any(pattern.search(context) for pattern in _WRITE_AUTHORITY):
                invalid_writable.append(raw)
            continue
        is_reference = any(pattern.search(local_context) for pattern in _REFERENCE_AUTHORITY)
        allows_write = any(pattern.search(context) for pattern in _WRITE_AUTHORITY)
        forbids_write = any(pattern.search(local_context) for pattern in _READ_ONLY_AUTHORITY)
        if is_reference and not allows_write:
            references.append(relative)
        elif allows_write:
            writable.append(relative)
        elif forbids_write:
            read_only.append(relative)
    writable = list(dict.fromkeys(writable))
    read_only = list(dict.fromkeys(read_only))
    references = list(dict.fromkeys(references))
    invalid_writable = list(dict.fromkeys(invalid_writable))
    # The current root contract cannot express a writable ancestor with an
    # immutable descendant. Fail closed by dropping such an ancestor rather
    # than silently granting the descendant.
    writable = [
        allowed for allowed in writable
        if not any(
            denied.casefold().startswith(allowed.casefold().rstrip("/") + "/")
            for denied in read_only
        )
    ]
    return {
        "writable": writable,
        "read_only": read_only,
        "references": references,
        "invalid_writable": invalid_writable,
    }


def _path_is_under(relative: str, allowed_roots: list[str]) -> bool:
    normalized = relative.replace("\\", "/").strip("/").casefold()
    return any(
        normalized == root.casefold() or normalized.startswith(root.casefold() + "/")
        for root in allowed_roots
    )


def _paths_overlap(left: str, right: str) -> bool:
    return _path_is_under(left, [right]) or _path_is_under(right, [left])


def _normalized_text_sha256(path: Path) -> str | None:
    try:
        if not path.is_file() or path.is_symlink():
            return None
        text = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError):
        return None
    return sha256_bytes(text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8"))


def _validated_changes(
    root: Path,
    raw_changes: object,
    allowed_write_roots: list[str] | None = None,
    protected_paths: list[str] | None = None,
    exact_write_grants: dict[str, set[str]] | None = None,
) -> list[ChangePlan]:
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
        if protected_paths and _path_is_under(relative, protected_paths):
            raise HarnessError(
                f"Proposed path {relative} is protected by the user's read-only/reference constraint"
            )
        if allowed_write_roots is not None and not _path_is_under(relative, allowed_write_roots):
            raise HarnessError(
                f"Proposed path {relative} is outside the explicit write destinations: "
                + (", ".join(allowed_write_roots) or "(no project paths are writable)")
            )
        capability = "DELETE" if raw.get("delete") is True else (
            "MODIFY" if confined_path(root, relative).exists() else "CREATE"
        )
        # Exact operation grants and explicitly authorized destination roots
        # are additive capabilities.  An exact artifact such as TEST-ci.yml
        # must not turn the other user-authorized output roots into a
        # global deny-list.  With no root grant, exact-only goals remain exact.
        if exact_write_grants is not None:
            allowed = {
                str(one).upper() for one in exact_write_grants.get(relative.casefold(), set())
            }
            allowed_by_root = bool(
                allowed_write_roots and _path_is_under(relative, allowed_write_roots)
            )
            if allowed and capability not in allowed and "CREATE_OR_MODIFY" not in allowed:
                raise HarnessError(
                    f"Proposed {capability.lower()} of {relative} is not authorized by the compiled goal operations"
                )
            if not allowed and not allowed_by_root:
                raise HarnessError(
                    f"Proposed {capability.lower()} of {relative} is not authorized by the compiled goal operations"
                )
        path = confined_path(root, relative)
        if path.is_symlink():
            raise HarnessError(f"Refusing to replace a symbolic link: {relative}")
        seen.add(relative)
        deleting = raw.get("delete") is True
        content = "" if deleting else str(raw.get("content") or "")
        # A provider can ignore the instruction not to return unchanged files.
        # Treat that as no progress rather than creating a misleading backup,
        # transaction id, and execution turn that claims the file changed.
        if deleting and not path.exists():
            continue
        if not deleting and file_sha256(path) == sha256_bytes(content.encode("utf-8")):
            continue
        changes.append(ChangePlan(
            path=relative,
            baseline_sha256=file_sha256(path),
            content=None if deleting else content,
            delete=deleting,
            reason=str(raw.get("reason") or "Board work request")[:1000],
        ))
    return changes


def _with_test_companion_grants(
    root: Path,
    raw_changes: object,
    grants: dict[str, set[str]],
    goal_spec: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, set[str]]:
    """Instantiate only causally related runnable-test companion grants."""

    expanded = {key: set(value) for key, value in grants.items()}
    behavior = any(
        isinstance(one, dict) and one.get("kind") in {"behavior", "behavior_preservation"}
        for one in contract.get("requirements", [])
    )
    if not behavior or not isinstance(raw_changes, list):
        return expanded
    target_stems = {
        Path(str(path)).stem.casefold()
        for path in goal_spec.get("write_policy", {}).get("grants", []) if str(path)
    }
    for raw in raw_changes:
        if not isinstance(raw, dict) or raw.get("delete") is True:
            continue
        relative = str(raw.get("path") or "").replace("\\", "/").strip()
        content = str(raw.get("content") or "")
        filename = Path(relative).name.casefold()
        runnable_shape = bool(
            re.search(r"(?:^|/)(?:tests?|specs?)(?:/|$)", relative, re.I)
            or re.search(r"(?:^test_.*|_test\.|\.test\.|\.spec\.)", filename, re.I)
        ) and Path(relative).suffix.casefold() in {
            ".py", ".js", ".cjs", ".mjs", ".ts", ".tsx", ".go", ".cs", ".java", ".rs",
        }
        references_target = any(
            re.search(rf"(?<![\w]){re.escape(stem)}(?![\w])", content, re.I)
            for stem in target_stems if stem
        )
        if runnable_shape and references_target:
            confined_path(root, relative)
            expanded.setdefault(relative.casefold(), set()).add("CREATE_OR_MODIFY")
    return expanded


def _transaction_paths(root: Path | None, transaction_ids: list[str]) -> list[str]:
    """List paths named by already-applied transactions for truthful recovery UI."""

    if root is None:
        return []
    paths: list[str] = []
    for transaction_id in transaction_ids:
        try:
            manifest_path = confined_path(
                root,
                Path(".harness") / "backups" / transaction_id / "manifest.json",
                allow_missing=True,
                allow_control=True,
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (HarnessError, OSError, json.JSONDecodeError):
            continue
        for change in manifest.get("changes", []):
            if not isinstance(change, dict):
                continue
            relative = str(change.get("path") or "").strip()
            if relative and relative not in paths:
                paths.append(relative)
    return paths


def _file_snapshot(root: Path, paths: list[str]) -> str:
    blocks: list[str] = []
    diagnostics: list[str] = []
    used = 0
    unique = list(dict.fromkeys(paths))
    for relative in unique[:30]:
        try:
            path = confined_path(root, relative)
            if path.is_symlink() or not path.is_file():
                diagnostics.append(f"{relative}: not a readable regular file")
                continue
            size = path.stat().st_size
            if size > 500_000:
                diagnostics.append(f"{relative}: omitted because it is {size} bytes (per-file limit 500000)")
                continue
            content = path.read_text(encoding="utf-8", errors="strict")
        except UnicodeDecodeError:
            diagnostics.append(f"{relative}: omitted because it is not valid UTF-8 text")
            continue
        except (HarnessError, OSError) as exc:
            diagnostics.append(f"{relative}: could not read ({exc})")
            continue
        if used + len(content) > 300_000:
            diagnostics.append(
                f"{relative}: omitted because the 300000-character snapshot budget was exhausted"
            )
            continue
        blocks.append(f"FILE {relative}\n{content}")
        used += len(content)
    if len(unique) > 30:
        diagnostics.append(f"{len(unique) - 30} path(s) omitted after the explicit 30-path snapshot limit")
    if diagnostics:
        blocks.append("SNAPSHOT DIAGNOSTICS (nothing was silently omitted)\n" + "\n".join(diagnostics))
    return "\n\n".join(blocks) or "[No readable changed or requested files.]"


_TEST_GOAL = re.compile(
    r"\b(?:test|tests|testing|e2e|end[- ]to[- ]end|unit|integration|playwright|pytest|vitest)\b",
    re.IGNORECASE,
)
_EMPTY_TEST_OUTPUT = re.compile(
    r"\b(?:no(?: [A-Za-z0-9_-]+)? tests? (?:to run|found|collected)|0 tests? (?:run|passed|collected))\b",
    re.IGNORECASE,
)


def _is_test_goal(goal: str) -> bool:
    return bool(_TEST_GOAL.search(_mask_goal_files(str(goal or ""))))


def _runnable_tests_in_changed_files(root: Path, changed: list[str]) -> dict[str, Any]:
    """Count concrete, non-skipped test declarations in files changed by this run."""

    inspected: list[str] = []
    runnable = 0
    skipped = 0
    levels: dict[str, dict[str, Any]] = {
        "unit": {"runnable": 0, "skipped": 0, "files": []},
        "API": {"runnable": 0, "skipped": 0, "files": []},
        "E2E": {"runnable": 0, "skipped": 0, "files": []},
    }
    for relative in changed:
        normalized = relative.replace("\\", "/")
        if not re.search(
            r"(?:^|/)(?:tests?|specs?)(?:/|$)|(?:^test_.+|_test|(?:test|spec))\.[^.]+$",
            normalized, re.I,
        ):
            continue
        try:
            path = confined_path(root, relative, allow_missing=False)
            if not path.is_file() or path.stat().st_size > 1_000_000:
                continue
            source = path.read_text(encoding="utf-8", errors="strict")
        except (HarnessError, OSError, UnicodeDecodeError):
            continue
        inspected.append(normalized)
        # Remove ordinary line comments before counting. This deliberately
        # does not pretend to be a full parser; deterministic command results
        # remain the authority whenever a runner exists.
        active = "\n".join(
            line for line in source.splitlines()
            if not line.lstrip().startswith(("//", "#"))
        )
        file_skipped = len(re.findall(r"\b(?:test|it|describe)\s*\.\s*skip\s*\(", active))
        python_skipped = len(re.findall(
            r"(?ms)^\s*@(?:unittest\.)?skip(?:If|Unless)?\b[^\n]*\n"
            r"(?:\s*@[^\n]+\n)*\s*(?:async\s+)?def\s+test_[A-Za-z0-9_]+\s*\(",
            active,
        )) + len(re.findall(
            r"(?ms)^\s*@pytest\.mark\.skip\b[^\n]*\n"
            r"(?:\s*@[^\n]+\n)*\s*(?:async\s+)?def\s+test_[A-Za-z0-9_]+\s*\(",
            active,
        ))
        file_skipped += python_skipped
        file_runnable = len(re.findall(r"\b(?:test|it)\s*\(", active))
        file_runnable += len(re.findall(r"(?m)^\s*(?:async\s+)?def\s+test_[A-Za-z0-9_]+\s*\(", active))
        file_runnable += len(re.findall(r"\b#\s*\[test\]", active, re.I))
        file_runnable += len(re.findall(r"(?m)^\s*func\s+Test[A-Za-z0-9_]+\s*\(", active))
        file_runnable += len(re.findall(r"(?m)^\s*@Test\b", active))
        file_runnable += len(re.findall(r"(?m)^\s*\[(?:Test|Fact|Theory)\b", active))
        file_runnable = max(0, file_runnable - python_skipped)
        skipped += file_skipped
        runnable += file_runnable
        path_folded = ("/" + normalized).casefold()
        folded = (path_folded + "\n" + active).casefold()
        if re.search(r"(?:^|[/_.-])e2e(?:[/_.-]|$)", path_folded):
            level = "E2E"
        elif re.search(r"(?:^|[/_.-])api(?:[/_.-]|$)", path_folded):
            level = "API"
        elif re.search(r"(?:^|[/_.-])unit(?:[/_.-]|$)", path_folded):
            level = "unit"
        elif re.search(r"end[- ]to[- ]end|page\.goto\s*\(|cy\.visit\s*\(|@playwright/test", folded):
            level = "E2E"
        elif re.search(r"supertest|\b(?:request|fetch)\s*\(|https?://", folded):
            level = "API"
        else:
            level = "unit"
        levels[level]["files"].append(normalized)
        levels[level]["runnable"] += file_runnable
        levels[level]["skipped"] += file_skipped
    return {
        "runnable": runnable, "skipped": skipped, "files": inspected,
        "levels": levels,
    }


def _test_preflight(root: Path, changed: list[str]) -> dict[str, Any]:
    """Read-only dependency/collection preflight before any discovered runner."""

    all_test_files: list[str] = []
    skipped = {".git", ".harness", "node_modules", ".venv", "venv", "dist", "build"}
    for folder, directories, files in os.walk(root, followlinks=False):
        directories[:] = [one for one in directories if one not in skipped]
        base = Path(folder)
        for name in files:
            path = base / name
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
                continue
            supported = bool(re.search(
                r"\.(?:[cm]?[jt]sx?|py|rs|go|java|cs|rb)$", name, re.I
            ))
            test_location = bool(re.search(r"(?:^|/)(?:tests?|specs?)(?:/|$)", relative, re.I))
            test_name = bool(re.search(
                r"(?:^test_.+|_test|(?:test|tests?|spec))\.(?:[cm]?[jt]sx?|py|rs|go|java|cs|rb)$",
                name, re.I,
            )) or bool(re.search(r"(?:Test|Tests)\.java$", name))
            if path.is_file() and not path.is_symlink() and supported and (test_location or test_name):
                all_test_files.append(relative)
    package_path = root / "package.json"
    missing: list[str] = []
    false_green = ""
    if package_path.is_file():
        try:
            package = json.loads(package_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            package = {}
        dependencies: set[str] = set()
        if isinstance(package, dict):
            for key in ("dependencies", "devDependencies", "peerDependencies"):
                values = package.get(key, {})
                if isinstance(values, dict):
                    dependencies.update(str(name) for name in values)
            script = str(package.get("scripts", {}).get("test", "")) if isinstance(package.get("scripts"), dict) else ""
            if _EMPTY_TEST_OUTPUT.search(script):
                false_green = "The package test script is a known zero-test success stub."
        imports = {
            "@playwright/test": "@playwright/test",
            "from 'vitest'": "vitest",
            'from "vitest"': "vitest",
            "@vue/test-utils": "@vue/test-utils",
        }
        for relative in all_test_files:
            try:
                source = confined_path(root, relative, allow_missing=False).read_text(
                    encoding="utf-8", errors="replace"
                )
            except (HarnessError, OSError):
                continue
            for needle, dependency in imports.items():
                if needle in source and dependency not in dependencies and dependency not in missing:
                    missing.append(dependency)
    return {
        "test_files": all_test_files[:500],
        "test_file_count": len(all_test_files),
        "missing_dependencies": missing,
        "false_green": false_green,
        "authority": "confirmed_project_work_transaction",
    }


def _missing_requested_test_levels(
    goal: str, changed: list[str], root: Path | None = None,
) -> list[str]:
    folded = str(goal or "").casefold()
    requested = _requested_test_levels(goal)
    if root is None:
        return requested
    static = _runnable_tests_in_changed_files(root, changed)
    levels = static.get("levels", {})
    return [
        level for level in requested
        if int(levels.get(level, {}).get("runnable", 0)) <= 0
    ]


def _requested_test_levels(goal: str) -> list[str]:
    folded = str(goal or "").casefold()
    return [
        name for name, wanted in (
            ("E2E", bool(re.search(r"\b(?:e2e|end[- ]to[- ]end)\b", folded))),
            ("API", bool(re.search(r"\bapi\b", folded))),
            ("unit", bool(re.search(r"\bunit\b", folded))),
        ) if wanted
    ]


def _level_probe_command(command: list[str], files: list[str]) -> list[str] | None:
    """Narrow a trusted supported runner to changed files for level proof."""

    if not files:
        return None
    words = [
        Path(part).name.casefold().removesuffix(".exe").removesuffix(".cmd").removesuffix(".bat")
        for part in command
    ]
    if any(words[index:index + 2] == ["-m", "unittest"] for index in range(len(words) - 1)):
        index = next(index for index in range(len(words) - 1) if words[index:index + 2] == ["-m", "unittest"])
        return command[:index + 2] + ["-v", *files]
    if any(words[index:index + 2] == ["-m", "pytest"] for index in range(len(words) - 1)):
        index = next(index for index in range(len(words) - 1) if words[index:index + 2] == ["-m", "pytest"])
        return command[:index + 2] + ["-q", *files]
    if words and words[0].startswith("pytest"):
        return [command[0], "-q", *files]
    joined = " ".join(str(one).replace("\\", "/").casefold() for one in command)
    if "playwright" in joined and "test" in words:
        index = words.index("test")
        return command[:index + 1] + files
    if "vitest" in joined:
        index = next((index for index, word in enumerate(words) if "vitest" in word), len(command) - 1)
        prefix = command[:index + 1]
        return prefix + ([] if "run" in words[index + 1:] else ["run"]) + files
    if "jest" in joined:
        index = next((index for index, word in enumerate(words) if "jest" in word), len(command) - 1)
        return command[:index + 1] + files + ["--runInBand"]
    if words and words[0] == "go" and "test" in words:
        parent = Path(files[0].replace("\\", "/")).parent.as_posix()
        package = "." if parent == "." else "./" + parent
        prefix = command[:words.index("test") + 1]
        return prefix + ["-json", package]
    if words and words[0] in {"cargo", "dotnet", "mvn", "mvnw", "gradle", "gradlew"}:
        # These runners do not all expose a stable file selector. Re-running
        # the exact trusted command still gives a fresh differential witness;
        # a receipt is emitted only if a safe coverage adapter is available.
        return list(command)
    return None


def _semantic_source_terms(source: str) -> set[str]:
    terms: set[str] = set()
    expanded = re.sub(r"[_-]+", " ", source.casefold())
    for raw in re.findall(r"[^\W_][\w]*", expanded, re.UNICODE):
        word = raw
        for suffix in ("ing", "ed", "es", "s"):
            if len(word) > len(suffix) + 3 and word.endswith(suffix):
                word = word[:-len(suffix)]
                break
        word = {
            "crashe": "crash", "retri": "retry", "failur": "failure",
            "requeste": "request", "rejecte": "reject", "preserv": "preserve",
            "addres": "address", "handl": "handle", "chang": "change", "updat": "update",
        }.get(word, word)
        if word:
            terms.add(word)
    return terms


def _acceptance_scenario(requirement: dict[str, Any]) -> dict[str, Any] | None:
    """Compile one bounded behavioral predicate from an engine requirement."""

    terms = {str(one).casefold() for one in requirement.get("acceptance_terms", [])}
    clause = str(requirement.get("acceptance_clause") or "").casefold()
    if not terms:
        return None
    predicate = ""
    stimulus = ""
    if terms & {"reject", "invalid", "malformed", "error", "raise"}:
        predicate = "REJECT"
        stimulus = "empty" if "empty" in terms else "malformed" if "malformed" in terms else "invalid"
    elif "unicode" in terms or re.search(r"round[- ]?trip|preserv", clause):
        predicate = "ROUNDTRIP"
        stimulus = "unicode"
    elif "retry" in terms:
        predicate = "RETRY"
        stimulus = "first_failure"
    elif "timeout" in terms:
        predicate = "TIMEOUT"
        stimulus = "timeout"
    elif terms & {"crash", "deadlock"}:
        predicate = "PREVENT_FAILURE"
        stimulus = next(iter(terms & {"crash", "deadlock"}))
    if not predicate:
        # A vague correction cannot be proved merely because a test repeats
        # words from the prompt. It remains explicitly needs-verification.
        return None
    core = {
        "schema_version": 1,
        "requirement_id": str(requirement.get("id") or ""),
        "predicate": predicate,
        "stimulus_property": stimulus,
        "acceptance_terms": sorted(terms),
    }
    core["scenario_digest"] = hashlib.sha256(json.dumps(
        core, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    return core


_FORBIDDEN_TEST_ORACLE = re.compile(
    r"\b(?:VERSION|__file__|__doc__|read_text|read_bytes|readFileSync|existsSync|"
    r"runpy|pathlib|inspect|getsource|importlib|get_source|sourceFile|readFile|"
    r"exec|eval|compile|grep|sha256|md5|stat\s*\(|mtime)\b|\bopen\s*\(",
    re.I,
)


def _semantic_test_trace(source: str, scenario: dict[str, Any]) -> dict[str, Any] | None:
    """Extract stimulus -> production call -> native oracle provenance.

    This bounded adapter intentionally recognizes only native semantic
    assertion shapes. Source/metadata inspection, dynamic execution, and
    constant/version oracles are rejected rather than guessed.
    """

    active = "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith(("#", "//"))
    )
    if _FORBIDDEN_TEST_ORACLE.search(active):
        return None
    if re.search(r"(?:mock|patch|spyOn)\s*\([^\n]*(?:parse|handle|retry|reject)", active, re.I):
        return None
    browser_scenario = _playwright_static_browser_scenario(active)
    if browser_scenario is not None:
        tests = list(re.finditer(
            r"\b(?:test|it)\s*\(\s*['\"]([^'\"]+)['\"]", active,
        ))
        if not tests:
            return None
        route = str(browser_scenario["route"])
        graph = {
            "oracle": "browser_dom",
            "production_call": "browser_route:" + route,
            "production_component": Path(route).stem or "index",
            "native_test_id": tests[-1].group(1),
            "stimulus": str(scenario.get("stimulus_property") or "browser_action"),
            "browser_scenario": browser_scenario,
            "provenance": "engine_browser_route_dom_observable",
        }
        graph["trace_digest"] = hashlib.sha256(json.dumps(
            graph, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        return graph
    predicate = str(scenario.get("predicate") or "")
    patterns: list[tuple[str, str]] = []
    if predicate == "REJECT":
        patterns = [
            (
                "python_raises",
                r"(?:assertRaises|pytest\.raises)\s*\([^)]*\)\s*:\s*"
                r"(?P<call>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*\((?P<args>[^)]*)\)",
            ),
            (
                "js_throws",
                r"expect\s*\(\s*(?:async\s*)?\(\s*\)\s*=>\s*"
                r"(?P<call>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*\((?P<args>[^)]*)\)\s*\)"
                r"\s*\.\s*(?:rejects\s*\.)?toThrow",
            ),
        ]
    elif predicate == "ROUNDTRIP":
        patterns = [
            (
                "python_equal",
                r"(?:assertEqual|assertEquals)\s*\(\s*(?P<expected>[^,]+),\s*"
                r"(?P<call>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*\((?P<args>[^)]*)\)\s*\)",
            ),
            (
                "js_equal",
                r"expect\s*\(\s*(?P<call>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)"
                r"\s*\((?P<args>[^)]*)\)\s*\)\s*\.\s*to(?:Be|Equal|StrictEqual)\s*\((?P<expected>[^)]*)\)",
            ),
        ]
    else:
        return None
    for oracle, pattern in patterns:
        match = re.search(pattern, active, re.I | re.S)
        if not match:
            continue
        args = match.groupdict().get("args", "")
        stimulus = str(scenario.get("stimulus_property") or "")
        if stimulus == "empty" and not re.search(r"(?:''|\"\")", args):
            continue
        if stimulus == "unicode" and not any(ord(character) > 127 for character in args):
            continue
        if stimulus in {"invalid", "malformed"} and not args.strip():
            continue
        call = match.group("call")
        symbol = call.rsplit(".", 1)[-1]
        module = ""
        imported = re.search(
            rf"\bfrom\s+([\w.]+)\s+import\s+[^\n]*\b{re.escape(symbol)}\b",
            active, re.I,
        )
        if imported:
            module = imported.group(1).split(".")[-1]
        elif "." in call and re.search(
            rf"\bimport\s+{re.escape(call.split('.', 1)[0])}\b", active,
        ):
            module = call.split(".", 1)[0]
        else:
            required = re.search(
                rf"(?:const|let|var)\s*\{{[^}}]*\b{re.escape(symbol)}\b[^}}]*\}}\s*=\s*"
                r"require\s*\(\s*['\"]([^'\"]+)['\"]\s*\)",
                active, re.I,
            ) or re.search(
                rf"\bimport\s*\{{[^}}]*\b{re.escape(symbol)}\b[^}}]*\}}\s*from\s*['\"]([^'\"]+)['\"]",
                active, re.I,
            )
            if required:
                module = Path(required.group(1)).stem
        if not module:
            # A local fake with a goal-shaped name is not a production call.
            continue
        preceding = active[:match.start()]
        python_test = list(re.finditer(r"\bdef\s+(test_[A-Za-z0-9_]+)\s*\(", preceding))
        js_test = list(re.finditer(
            r"\b(?:test|it)\s*\(\s*['\"]([^'\"]+)['\"]", preceding,
        ))
        native_test_id = (
            python_test[-1].group(1) if python_test else
            js_test[-1].group(1) if js_test else ""
        )
        if not native_test_id:
            continue
        graph = {
            "oracle": oracle,
            "production_call": call,
            "production_component": module,
            "native_test_id": native_test_id,
            "stimulus": stimulus,
            "arguments_sha256": hashlib.sha256(args.encode("utf-8")).hexdigest(),
            "provenance": "runtime_call_observable_native_assertion",
        }
        graph["trace_digest"] = hashlib.sha256(json.dumps(
            graph, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        return graph
    return None


def _behavior_test_witnesses(
    root: Path, changed: list[str], requirements: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Assign independent semantic test traces, including pre-existing tests."""

    all_test_files = _test_preflight(root, changed).get("test_files", [])
    candidates: dict[str, dict[str, Any]] = {}
    scenarios = {
        str(requirement.get("id") or ""): _acceptance_scenario(requirement)
        for requirement in requirements
    }
    for relative in all_test_files:
        try:
            path = confined_path(root, relative, allow_missing=False)
            source = path.read_text(encoding="utf-8", errors="strict")
        except (HarnessError, OSError, UnicodeError):
            continue
        candidates[str(relative)] = {
            "path": str(relative), "sha256": file_sha256(path),
            "source": source,
        }
    assigned: dict[str, dict[str, Any]] = {}
    used: set[str] = set()
    for requirement in requirements:
        identifier = str(requirement.get("id") or "")
        scenario = scenarios.get(identifier)
        chosen = None
        if scenario is not None:
            for relative, value in sorted(candidates.items()):
                if relative in used:
                    continue
                trace = _semantic_test_trace(str(value["source"]), scenario)
                if trace is not None:
                    chosen = {key: item for key, item in value.items() if key != "source"}
                    chosen["scenario"] = scenario
                    chosen["semantic_trace"] = trace
                    break
        if chosen is not None:
            assigned[identifier] = chosen
            used.add(str(chosen["path"]))
    return assigned


def _restore_counterfactual(
    root: Path, destination: Path, transaction_ids: list[str], keep_tests: set[str],
) -> dict[str, str | None]:
    """Copy the current project and restore production files to session baseline."""

    shutil.copytree(
        root, destination, dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".git", ".harness"),
    )
    baseline: dict[str, str | None] = {}
    for transaction_id in reversed(transaction_ids):
        manifest = FileTransaction(root).load_manifest(transaction_id)
        records = manifest.get("changes", [])
        if not isinstance(records, list):
            raise HarnessError("Counterfactual transaction manifest has invalid changes")
        backup_root = confined_path(
            root, Path(".harness") / "backups" / transaction_id,
            allow_missing=False, allow_control=True,
        )
        for record in reversed(records):
            if not isinstance(record, dict) or not isinstance(record.get("path"), str):
                raise HarnessError("Counterfactual transaction manifest has an invalid path")
            relative = str(record["path"]).replace("\\", "/")
            if relative in keep_tests:
                continue
            target = confined_path(destination, relative)
            before_hash = record.get("before_sha256")
            baseline[relative] = str(before_hash) if isinstance(before_hash, str) else None
            if before_hash is None:
                target.unlink(missing_ok=True)
                continue
            backup = confined_path(
                backup_root, Path("files") / relative, allow_missing=False,
            )
            content = backup.read_bytes()
            if sha256_bytes(content) != before_hash:
                raise HarnessError("Counterfactual baseline backup digest mismatch: " + relative)
            atomic_write(target, content)
    return baseline


def _copy_verification_snapshot(root: Path, destination: Path) -> None:
    """Create a disposable content copy of the selected project surface."""

    shutil.copytree(
        root, destination, dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".git", ".harness", ".nexus-verification"),
    )
    # This namespace is executable verification infrastructure.  It must be
    # newly owned by Nexus even when copytree writes into an existing target;
    # no file supplied by the selected project may become a guard or runtime.
    _recreate_verification_directory(destination / ".nexus-verification")


def _verification_lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _verification_is_reparse(path: Path) -> bool:
    held = _verification_lstat(path)
    return bool(
        held is not None
        and getattr(held, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _verification_path_exists(path: Path) -> bool:
    return _verification_lstat(path) is not None


def _remove_verification_path(path: Path) -> None:
    """Remove one exact engine-owned path without assuming its current type."""

    held = _verification_lstat(path)
    if held is None:
        return
    attributes = getattr(held, "st_file_attributes", 0)
    if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        if attributes & getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10):
            path.rmdir()
        else:
            path.unlink(missing_ok=True)
    elif stat.S_ISLNK(held.st_mode) or stat.S_ISREG(held.st_mode):
        path.unlink(missing_ok=True)
    elif stat.S_ISDIR(held.st_mode):
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _recreate_verification_directory(path: Path) -> Path:
    if _verification_path_exists(path):
        _remove_verification_path(path)
    path.mkdir(parents=True, exist_ok=False)
    return path


def _restore_verification_escape(
    root: Path, frozen: Path, baseline_manifest: dict[str, str],
) -> bool:
    """Restore the exact frozen file surface after an absolute-path escape."""

    _current_merkle, current_manifest = _project_tree_merkle(root)
    for relative in sorted(
        set(current_manifest) - set(baseline_manifest),
        key=lambda value: (value.count("/"), len(value)), reverse=True,
    ):
        path = confined_path(root, relative, allow_missing=True)
        if path.is_file() or path.is_symlink():
            path.unlink(missing_ok=True)
    for relative, expected in baseline_manifest.items():
        target = confined_path(root, relative, allow_missing=True)
        source = confined_path(frozen, relative, allow_missing=False)
        if not expected.startswith("file:") or source.is_symlink():
            # Reparse/symlink restoration cannot be made race-free by copying;
            # refuse to pretend recovery if such an entry drifted.
            if _project_tree_manifest(root).get(relative) != expected:
                return False
            continue
        if file_sha256(target) != expected.removeprefix("file:"):
            if target.exists() and not target.is_file():
                return False
            atomic_write(target, source.read_bytes())
    restored_merkle, restored_manifest = _project_tree_merkle(root)
    expected_merkle = hashlib.sha256(json.dumps(
        sorted(baseline_manifest.items()), separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    return restored_merkle == expected_merkle and restored_manifest == baseline_manifest


def _snapshot_command(command: list[str], root: Path, snapshot: Path) -> list[str]:
    """Rebase absolute selected-project operands without rewriting toolchains."""

    rebased: list[str] = []
    canonical_root = root.resolve()
    for index, raw in enumerate(command):
        value = str(raw)
        try:
            candidate = Path(value)
            resolved = candidate.resolve() if candidate.is_absolute() else None
            if index > 0 and resolved is not None and (
                resolved == canonical_root or canonical_root in resolved.parents
            ):
                value = str(snapshot / resolved.relative_to(canonical_root))
        except (OSError, ValueError):
            pass
        rebased.append(value)
    return rebased


def _verification_guard_files(snapshot: Path, engine_root: Path) -> tuple[Path, Path]:
    """Install engine-owned language guards outside one disposable snapshot.

    The guards are a launch precondition for the supported language runtimes.
    They deliberately fail closed on process/native escape rather than trying
    to repair the user's machine after an untrusted test has run.
    """

    snapshot = snapshot.resolve()
    guard_root = snapshot / ".nexus-verification"
    if _verification_is_reparse(guard_root) or not guard_root.is_dir():
        _recreate_verification_directory(guard_root)
    # These names are never project data.  Recreate them before every launch
    # so a caller that did not come through the normal copy path still cannot
    # substitute an executable, guard, home, or lock artifact.
    for name in (
        "engine", "sitecustomize.py", "node-guard.cjs", "runtime", "node-workspace",
        "home", "tmp", ".source-python-stage.lock",
    ):
        target = guard_root / name
        if _verification_path_exists(target):
            _remove_verification_path(target)
    for pattern in (".runtime-stage-*", ".runtime-previous-*"):
        for abandoned in guard_root.glob(pattern):
            _remove_verification_path(abandoned)
    engine_root = Path(os.path.abspath(engine_root))
    engine_resolved = engine_root.resolve()
    if (
        _verification_is_reparse(engine_root)
        or engine_resolved == snapshot
        or snapshot in engine_resolved.parents
        or engine_resolved in snapshot.parents
    ):
        raise HarnessError(
            "Verification engine code must use a direct host-owned root outside the writable snapshot"
        )
    _recreate_verification_directory(engine_root)
    python_guard = engine_root / "sitecustomize.py"
    python_source = r'''import builtins,os,sys
_ROOT=os.path.realpath(os.environ['NEXUS_VERIFICATION_ROOT'])
_APPROVED_ENGINE_ROOT=os.path.realpath(os.environ['NEXUS_VERIFICATION_ENGINE_ROOT'])
_EXEC_ROOTS=tuple(os.path.realpath(one) for one in os.environ.get('NEXUS_ALLOWED_EXEC_ROOTS','').split(os.pathsep) if one)
_WRITE_FLAGS=(getattr(os,'O_WRONLY',1)|getattr(os,'O_RDWR',2)|getattr(os,'O_APPEND',8)|getattr(os,'O_CREAT',64)|getattr(os,'O_TRUNC',512))
def _same_component(left,right):
    try:
        if os.path.normcase(os.path.abspath(left))==os.path.normcase(os.path.abspath(right)): return True
        return os.path.samefile(left,right)
    except Exception:
        return False
def _under(path,roots):
    try:
        current=os.path.realpath(os.fspath(path))
    except Exception:
        return False
    while True:
        if any(_same_component(current,root) for root in roots): return True
        parent=os.path.dirname(current)
        if parent==current: return False
        current=parent
_ENGINE_ROOT=os.path.dirname(os.path.realpath(__file__))
_SELF_EXECUTABLE=os.path.realpath(sys.executable)
_SYSTEM_ROOT=os.path.realpath(os.environ.get('SystemRoot','')) if os.name=='nt' else ''
if not _same_component(_ENGINE_ROOT,_APPROVED_ENGINE_ROOT) or _under(_ENGINE_ROOT,(_ROOT,)):
    raise RuntimeError('Nexus verification guard is outside its approved immutable engine root')
if not os.path.isabs(_SELF_EXECUTABLE) or not os.path.isfile(_SELF_EXECUTABLE):
    raise RuntimeError('Nexus verification interpreter identity is invalid')
_SYSTEM_ROOTS=(_SYSTEM_ROOT,) if os.path.isabs(_SYSTEM_ROOT) and os.path.isdir(_SYSTEM_ROOT) and not _under(_SYSTEM_ROOT,(_ROOT,)) else ()
def _inside(value):
    return isinstance(value,int) or _under(value,(_ROOT,))
def _deny_path(value,operation):
    if not isinstance(value,int) and _under(value,(_ENGINE_ROOT,)): raise PermissionError('Nexus verification containment denied '+operation+' in its immutable engine namespace')
    if not _inside(value): raise PermissionError('Nexus verification containment denied '+operation+' outside its disposable snapshot')
def _allow_child(executable):
    try:
        resolved=os.path.realpath(os.fspath(executable or sys.executable))
    except Exception:
        return False
    return _same_component(resolved,_SELF_EXECUTABLE) or _under(resolved,_EXEC_ROOTS)
def _loopback(address):
    if isinstance(address,str): return _inside(address)
    if not isinstance(address,tuple) or not address: return False
    return str(address[0]).casefold() in {'127.0.0.1','::1','localhost'}
def _audit(event,args):
    if event=='open' and args:
        mode=args[1] if len(args)>1 else None; flags=args[2] if len(args)>2 else 0
        writing=(isinstance(mode,str) and any(one in mode for one in 'wax+')) or (isinstance(flags,int) and bool(flags&_WRITE_FLAGS))
        if writing: _deny_path(args[0],'write')
    elif event in {'os.remove','os.rmdir','os.mkdir','os.truncate','os.chmod','os.chown','os.utime'} and args:
        _deny_path(args[0],event)
    elif event in {'os.rename','os.link','os.symlink'} and len(args)>=2:
        _deny_path(args[0],event); _deny_path(args[1],event)
    elif event=='subprocess.Popen':
        if not args or not _allow_child(args[0]):
            raise PermissionError('Nexus verification containment denied unapproved child executable')
    elif event in {'os.system','os.posix_spawn','os.spawn','os.exec','os.fork','pty.spawn'}:
        raise PermissionError('Nexus verification containment denied shell or unbound child execution')
    elif event=='ctypes.dlopen' and args:
        library=args[0]
        bare=isinstance(library,str) and not os.path.dirname(library) and library.casefold() in {'kernel32','kernel32.dll','user32','user32.dll','advapi32','advapi32.dll'}
        if not bare and not _under(library,(*_EXEC_ROOTS,*_SYSTEM_ROOTS)):
            raise PermissionError('Nexus verification containment denied unapproved native library')
    elif event=='import' and len(args)>1 and args[1]:
        origin=str(args[1])
        if origin.casefold().endswith(('.pyd','.dll')) and not _under(origin,_EXEC_ROOTS):
            raise PermissionError('Nexus verification containment denied project native extension')
    elif event in {'socket.connect','socket.bind'}:
        if len(args)<2 or not _loopback(args[1]):
            raise PermissionError('Nexus verification containment denied non-loopback network access')
sys.addaudithook(_audit)
'''
    atomic_write(python_guard, python_source.encode("utf-8"))
    node_guard = engine_root / "node-guard.cjs"
    node_source = r'''const cp=require('node:child_process');const path=require('node:path');
const root=path.resolve(process.env.NEXUS_VERIFICATION_ROOT);const original={};
const allowedBrowser=/^(?:chrome|chromium|msedge|firefox|webkit)(?:\.exe)?$/i;
function guardedEnv(options){const next={...(options||{})};next.env={...process.env,...(next.env||{})};next.env.NODE_OPTIONS=process.env.NODE_OPTIONS||'';next.env.NEXUS_VERIFICATION_ROOT=root;next.env.TEMP=path.join(root,'.nexus-verification','tmp');next.env.TMP=next.env.TEMP;return next;}
function maySpawn(file,args){const base=path.basename(String(file));if(allowedBrowser.test(base))return true;if(/^(?:node|nodejs)(?:\.exe)?$/i.test(base))return Array.isArray(args)&&args.some(v=>/playwright/i.test(String(v)));return false;}
for(const name of ['spawn','spawnSync','execFile','execFileSync']){original[name]=cp[name];cp[name]=function(file,args,options){if(!maySpawn(file,args))throw new Error('Nexus verification containment denied child-process escape');return original[name].call(cp,file,args,guardedEnv(options));};}
for(const name of ['exec','execSync','fork']){cp[name]=function(){throw new Error('Nexus verification containment denied shell/fork escape');};}
'''
    atomic_write(node_guard, node_source.encode("utf-8"))
    (guard_root / "tmp").mkdir()
    (guard_root / "home").mkdir()
    return python_guard, node_guard


def _copy_verified_runtime_file(source: Path, destination: Path) -> None:
    """Copy one engine-selected runtime file and detect source replacement."""

    source = source.resolve()
    if _verification_is_reparse(source) or not source.is_file():
        raise VerificationPythonUnavailable(
            "an engine-selected runtime file is missing or redirected: " + source.name
        )
    before = file_sha256(source)
    shutil.copy2(source, destination)
    if before is None or file_sha256(source) != before or file_sha256(destination) != before:
        raise VerificationPythonUnavailable(
            "an engine-selected runtime file changed while it was being staged: " + source.name
        )


def _stage_packaged_python_runtime(
    bundled: Path,
    runtime_root: Path,
    *,
    snapshot: Path,
    python_guard_parent: Path,
    dependency_paths: tuple[Path, ...],
) -> Path:
    """Recreate a snapshot runtime from the validated private app runtime."""

    try:
        _recreate_verification_directory(runtime_root)
        for runtime_name in (
            "python.exe", "python3.dll", "python311.dll",
            "vcruntime140.dll", "vcruntime140_1.dll",
        ):
            _copy_verified_runtime_file(bundled / runtime_name, runtime_root / runtime_name)
        pth = runtime_root / "python311._pth"
        atomic_write(pth, (
            str((bundled / "python311.zip").resolve()) + "\n"
            + str(bundled.resolve()) + "\n"
            + str((bundled / "Lib" / "site-packages").resolve()) + "\n"
            + str(python_guard_parent.resolve()) + "\n"
            + "".join(str(one.resolve()) + "\n" for one in dependency_paths)
            + str(snapshot.resolve()) + "\nimport site\n"
        ).encode("utf-8"))
        if not (runtime_root / "python.exe").is_file() or not pth.is_file():
            raise VerificationPythonUnavailable(
                "the packaged Python runtime copy is incomplete"
            )
        return runtime_root
    except VerificationPythonUnavailable:
        raise
    except OSError as error:
        raise VerificationPythonUnavailable(
            "the packaged Python runtime could not be staged safely: " + str(error)
        ) from error


def _trusted_host_node(
    requested: str,
    *,
    snapshot: Path,
    denied_root: Path,
) -> Path:
    """Resolve Node from the host toolchain, never from selected-project data."""

    found = shutil.which(requested)
    if not found:
        found = shutil.which(Path(requested).name)
    if not found:
        raise VerificationPythonUnavailable("Node is not installed on this computer")
    requested_path = Path(found).absolute()
    lexical_forbidden = (snapshot.absolute(), denied_root.absolute())
    try:
        if any(
            os.path.normcase(os.path.commonpath([str(root), str(requested_path)]))
            == os.path.normcase(str(root))
            for root in lexical_forbidden
        ):
            raise VerificationPythonUnavailable(
                "the selected Node executable belongs to project-controlled data"
            )
    except ValueError:
        pass
    candidate = Path(found).resolve()
    forbidden = (snapshot.resolve(), denied_root.resolve())
    if (
        _verification_is_reparse(candidate)
        or not candidate.is_file()
        or any(candidate == root or root in candidate.parents for root in forbidden)
    ):
        raise VerificationPythonUnavailable(
            "the selected Node executable belongs to project-controlled data"
        )
    return candidate


def _runtime_is_selected_project_data(runtime_root: Path, project_root: Path) -> bool:
    """Reject runtime roots supplied through or located in the selected project."""

    runtime = runtime_root.resolve()
    project = project_root.resolve()
    engine_location = Path(__file__).resolve().parents[2]
    return bool(
        runtime == project or project in runtime.parents
        or engine_location == project or project in engine_location.parents
    )


def _stage_node_runtime(source: Path, runtime_root: Path) -> Path:
    """Recreate and verify the snapshot Node runtime from an engine-selected file."""

    try:
        _recreate_verification_directory(runtime_root)
        destination = runtime_root / "node.exe"
        _copy_verified_runtime_file(source, destination)
        return destination
    except VerificationPythonUnavailable:
        raise
    except OSError as error:
        raise VerificationPythonUnavailable(
            "the Node containment runtime could not be staged safely: " + str(error)
        ) from error


def _playwright_static_browser_scenario(source: str) -> dict[str, Any] | None:
    """Compile the small, deterministic browser subset Nexus can prove itself.

    Project test prose may ratify selectors and expected UI state, but it is
    never executed as the oracle.  The engine replays only literal navigation,
    fill/click actions, and one native DOM assertion in its own runner.  Tests
    requiring a project-authored server, dynamic code, external URLs, or an
    unsupported matcher remain honestly unverified.
    """

    if re.search(r"\b(?:eval|Function|child_process|exec|spawn|setContent)\b", source):
        return None
    navigation = re.search(
        r"\bpage\s*\.\s*goto\s*\(\s*(['\"])(?P<route>.*?)\1\s*\)", source, re.S,
    )
    if navigation is None:
        return None
    route = navigation.group("route").strip()
    if not route or "://" in route or "\\" in route or ".." in route.split("/"):
        return None
    route = "/" + route.lstrip("/")
    action_pattern = re.compile(
        r"page\s*\.\s*locator\s*\(\s*(['\"])(?P<selector>.*?)\1\s*\)\s*\.\s*"
        r"(?P<action>fill|click)\s*\(\s*(?:(['\"])(?P<value>.*?)\4\s*)?\)",
        re.S,
    )
    actions = [
        {
            "action": match.group("action"),
            "selector": match.group("selector"),
            **({"value": match.group("value")} if match.group("action") == "fill" else {}),
        }
        for match in action_pattern.finditer(source)
    ]
    assertion = re.search(
        r"expect\s*\(\s*page\s*\.\s*locator\s*\(\s*(['\"])(?P<selector>.*?)\1\s*\)\s*\)"
        r"\s*\.\s*toHave(?P<matcher>Text|Value|Attribute)\s*\(\s*"
        r"(['\"])(?P<first>.*?)\4(?:\s*,\s*(['\"])(?P<second>.*?)\6)?\s*\)",
        source, re.S,
    )
    if assertion is None:
        return None
    matcher = assertion.group("matcher").casefold()
    expected: dict[str, Any] = {
        "kind": {"text": "text", "value": "value", "attribute": "attribute"}[matcher],
        "selector": assertion.group("selector"),
    }
    if matcher == "attribute":
        if assertion.group("second") is None:
            return None
        expected.update({"attribute": assertion.group("first"), "value": assertion.group("second")})
    else:
        expected["value"] = assertion.group("first")
    scenario = {
        "schema_version": 1,
        "route": route,
        "actions": actions,
        "expected": expected,
    }
    scenario["scenario_digest"] = hashlib.sha256(json.dumps(
        scenario, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    return scenario


def _approved_playwright_base_url(snapshot: Path, source: str) -> str | None:
    """Read one literal HTTPS baseURL from the selected suite/config only."""

    candidates: list[str] = []
    pattern = re.compile(
        r"\bbaseURL\s*:\s*(['\"])(?P<url>https://[^'\"\s]+)\1", re.I,
    )
    texts = [source]
    for name in (
        "playwright.config.ts", "playwright.config.js", "playwright.config.mts",
        "playwright.config.mjs", "playwright.config.cts", "playwright.config.cjs",
    ):
        path = snapshot / name
        try:
            if path.is_file():
                texts.append(path.read_text(encoding="utf-8", errors="strict"))
        except (OSError, UnicodeError):
            continue
    for text in texts:
        candidates.extend(match.group("url") for match in pattern.finditer(text))
        for match in re.finditer(
            r"\bbaseURL\s*:\s*process\.env\.([A-Za-z_][A-Za-z0-9_]*)", text,
        ):
            value = os.environ.get(match.group(1), "").strip()
            if value:
                candidates.append(value)
    if not candidates:
        return None
    normalized = [normalize_approved_https_base_url(one)[0] for one in candidates]
    if len(set(normalized)) != 1:
        raise ValueError("Playwright exact-origin verification found conflicting literal baseURL values")
    return normalized[0]


def _playwright_base_url_environment(
    snapshot: Path, source: str, approved_base_url: str,
) -> dict[str, str]:
    values: dict[str, str] = {}
    texts = [source]
    for path in snapshot.glob("playwright.config.*"):
        try:
            if path.is_file():
                texts.append(path.read_text(encoding="utf-8", errors="strict"))
        except (OSError, UnicodeError):
            continue
    for text in texts:
        for key in re.findall(
            r"\bbaseURL\s*:\s*process\.env\.([A-Za-z_][A-Za-z0-9_]*)", text,
        ):
            value = os.environ.get(key, "").strip()
            if value and normalize_approved_https_base_url(value)[0] == approved_base_url:
                values[key] = value
    return values


def _playwright_exact_origin_scenario(
    source: str, approved_base_url: str,
) -> dict[str, Any] | None:
    """Lift the already-proven literal subset onto an exact HTTPS origin."""

    common = extract_safe_playwright_scenario(source, approved_base_url)
    if common is not None:
        digest = hashlib.sha256(json.dumps(
            common, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        return {
            "schema_version": 1,
            "route_mode": "REMOTE_TLS_TUNNEL",
            "approved_base_url": approved_base_url,
            "safe_scenario": common,
            "scenario_digest": digest,
        }

    navigation = re.search(
        r"\bpage\s*\.\s*goto\s*\(\s*(['\"])(?P<route>.*?)\1\s*\)", source, re.S,
    )
    if navigation is None:
        return None
    route = navigation.group("route").strip()
    if not route or "\\" in route or ".." in route.split("/"):
        return None
    # Reuse the conservative project-code parser for actions/assertion.  Only
    # its navigation literal is normalized; the real URL is validated below.
    relative_source = (
        source[:navigation.start("route")] + "/" + source[navigation.end("route"):]
    )
    parsed = _playwright_static_browser_scenario(relative_source)
    if parsed is None:
        return None
    steps: list[dict[str, Any]] = [{"op": "goto", "url": route}]
    for action in parsed["actions"]:
        step = {
            "op": action["action"],
            "target": {"kind": "locator", "selector": action["selector"]},
        }
        if action["action"] == "fill":
            step["value"] = action["value"]
        steps.append(step)
    expected = parsed["expected"]
    assertion = {
        "op": "assert", "condition": expected["kind"],
        "target": {"kind": "locator", "selector": expected["selector"]},
        "expected": expected["value"],
    }
    if expected["kind"] == "attribute":
        assertion["name"] = expected["attribute"]
    steps.append(assertion)
    safe = {
        "base_url": approved_base_url,
        "steps": steps,
    }
    digest = hashlib.sha256(json.dumps(
        safe, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    return {
        "schema_version": 1,
        "route_mode": "REMOTE_TLS_TUNNEL",
        "approved_base_url": approved_base_url,
        "safe_scenario": safe,
        "scenario_digest": digest,
    }


def _brokered_playwright_runner_source() -> str:
    return r'''const fs=require('node:fs'),path=require('node:path'),http=require('node:http');
const {chromium}=require(path.join(process.env.NEXUS_BUNDLED_PLAYWRIGHT_ROOT,'node_modules','playwright'));
const scenario=JSON.parse(process.env.NEXUS_E2E_SCENARIO),root=path.resolve(process.env.NEXUS_VERIFICATION_ROOT),out=process.env.NEXUS_E2E_RESULT;
const receipt={passed:false,scenarioDigest:scenario.scenario_digest,route:scenario.route,actions:[],externalWriteDenied:false,serverErrors:[]};
function mime(file){if(file.endsWith('.html'))return'text/html; charset=utf-8';if(file.endsWith('.js'))return'text/javascript; charset=utf-8';if(file.endsWith('.css'))return'text/css; charset=utf-8';if(file.endsWith('.json'))return'application/json';return'application/octet-stream';}
const server=http.createServer((req,res)=>{let file='';try{const pathname=new URL(req.url,'http://127.0.0.1').pathname;const rel=decodeURIComponent(pathname).replace(/^\/+/, '')||'index.html';file=path.resolve(root,rel);const prefix=root.endsWith(path.sep)?root:root+path.sep;if(file!==root&&!file.startsWith(prefix))throw new Error('escape');const data=fs.readFileSync(file);res.writeHead(200,{'content-type':mime(file)});res.end(data);}catch(error){receipt.serverErrors.push({url:String(req.url),file,error:String(error)});res.writeHead(404);res.end('not found');}});
(async()=>{try{try{fs.writeFileSync(process.env.NEXUS_E2E_DENIED,'escape');}catch(error){receipt.externalWriteDenied=['EPERM','EACCES'].includes(error.code);}if(!receipt.externalWriteDenied)throw new Error('external write boundary was not enforced');await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});const address=server.address();const browser=await chromium.connectOverCDP(process.env.NEXUS_CDP_ENDPOINT);const context=browser.contexts()[0];if(!context)throw new Error('brokered browser context missing');const page=await context.newPage();page.setDefaultTimeout(5000);const response=await page.goto(`http://127.0.0.1:${address.port}${scenario.route}`);receipt.navigation={url:page.url(),status:response&&response.status()};for(const action of scenario.actions){const locator=page.locator(action.selector);if(action.action==='fill')await locator.fill(action.value);else if(action.action==='click')await locator.click();else throw new Error('unsupported action');receipt.actions.push({action:action.action,selector:action.selector});}const expected=scenario.expected,locator=page.locator(expected.selector);let observed;if(expected.kind==='text')observed=await locator.textContent();else if(expected.kind==='value')observed=await locator.inputValue();else observed=await locator.getAttribute(expected.attribute);receipt.observed=observed;receipt.observable={kind:expected.kind,selector:expected.selector,attribute:expected.attribute||'',expected:expected.value};receipt.passed=observed===expected.value;await page.close();}catch(error){receipt.error=String(error&&error.stack||error);}finally{if(typeof server.closeAllConnections==='function')server.closeAllConnections();server.close();fs.writeFileSync(out,JSON.stringify(receipt));process.exit(receipt.passed?0:3);}})();
'''


def _run_brokered_playwright_scenario(
    snapshot: Path, scenario: dict[str, Any], *, timeout: float,
    runtime: Any = None,
) -> dict[str, Any]:
    """Run one engine-compiled route/DOM scenario in two contained processes."""

    if scenario.get("route_mode") == "REMOTE_TLS_TUNNEL":
        approved_base_url = str(scenario.get("approved_base_url") or "")
        safe = scenario.get("safe_scenario")
        if not isinstance(safe, dict):
            raise ValueError("Exact-origin Playwright scenario has no validated safe IR")
        result = run_safe_playwright_scenario(
            snapshot, safe, approved_base_url, timeout=timeout, runtime=runtime,
        )
        evidence = result.get("evidence_receipt")
        valid = bool(
            result.get("passed") is True
            and result.get("receipt_attested") is True
            and isinstance(evidence, dict) and evidence.get("passed") is True
            and evidence.get("route_mode") == "REMOTE_TLS_TUNNEL"
            and evidence.get("configured_base_url") == approved_base_url
            and evidence.get("configured_origin") == evidence.get("final_origin")
            and evidence.get("exact_origin_route_attested") is True
            and evidence.get("external_write_denied") is True
            and evidence.get("boundary_inheritance_attested") is True
            and isinstance(evidence.get("tls"), list) and evidence["tls"]
            and isinstance(evidence.get("browser_request_routes"), list)
            and any(
                isinstance(one, dict) and one.get("resource_type") == "document"
                and one.get("exact_origin") is True
                for one in evidence["browser_request_routes"]
            )
            and isinstance(evidence.get("dom_assertions"), list)
            and evidence["dom_assertions"]
            and all(
                isinstance(one, dict) and one.get("passed") is True
                for one in evidence["dom_assertions"]
            )
        )
        return {
            "passed": valid,
            "scenario_digest": scenario.get("scenario_digest"),
            "broker": result.get("broker"),
            "receipt": result.get("receipt"),
            "exact_origin_evidence": evidence,
        }

    control = snapshot / ".nexus-verification"
    control.mkdir(parents=True, exist_ok=True)
    runner = control / "nexus-playwright-direct-probe.cjs"
    result_path = control / ("playwright-result-" + uuid.uuid4().hex + ".json")
    denied_path = snapshot.parent / ("nexus-e2e-denied-" + uuid.uuid4().hex + ".txt")
    atomic_write(runner, _brokered_playwright_runner_source().encode("utf-8"))
    payload = run_brokered_playwright_appcontainer(
        snapshot, runner, timeout=timeout,
        runtime=runtime,
        environment={
            "NEXUS_VERIFICATION_ROOT": str(snapshot.resolve()),
            "NEXUS_E2E_SCENARIO": json.dumps(
                scenario, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ),
            "NEXUS_E2E_RESULT": str(result_path.resolve()),
            "NEXUS_E2E_DENIED": str(denied_path.resolve()),
        },
    )
    try:
        receipt = json.loads(result_path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        receipt = {}
    valid = (
        payload.get("passed") is True
        and isinstance(receipt, dict) and receipt.get("passed") is True
        and receipt.get("externalWriteDenied") is True
        and receipt.get("scenarioDigest") == scenario.get("scenario_digest")
        and not denied_path.exists()
    )
    return {"passed": valid, "broker": payload, "receipt": receipt}


def _playwright_spec_files(snapshot: Path, command: list[str]) -> list[Path]:
    selected: list[Path] = []
    for raw in command[1:]:
        try:
            candidate = Path(str(raw))
            path = candidate if candidate.is_absolute() else snapshot / candidate
            path = path.resolve()
            if path.is_file() and path.is_relative_to(snapshot.resolve()) and re.search(
                r"\.(?:spec|test)\.[cm]?[jt]sx?$", path.name, re.I,
            ):
                selected.append(path)
        except (OSError, ValueError):
            continue
    if not selected:
        selected = sorted(
            path for path in snapshot.rglob("*")
            if path.is_file() and re.search(r"\.(?:spec|test)\.[cm]?[jt]sx?$", path.name, re.I)
            and ".nexus-verification" not in path.parts
        )
    return list(dict.fromkeys(selected))


def _run_brokered_playwright_specs(
    snapshot: Path, command: list[str], *, timeout: float,
    runtime: Any = None,
) -> dict[str, Any]:
    """Replace Playwright's uncontained worker fork with engine-owned probes."""

    files = _playwright_spec_files(snapshot, command)
    sources: list[tuple[Path, str]] = []
    approved_urls: list[str] = []
    exact_environment: dict[str, str] = {}
    for path in files:
        try:
            source = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError):
            continue
        sources.append((path, source))
        approved = _approved_playwright_base_url(snapshot, source)
        if approved is not None:
            approved_urls.append(approved)
            exact_environment.update(
                _playwright_base_url_environment(snapshot, source, approved)
            )
    if sources and len(sources) == len(files) and approved_urls:
        if len(set(approved_urls)) != 1 or len(approved_urls) != len(files):
            raise ValueError("Every selected exact-origin Playwright suite must share one approved baseURL")
        selected = [path.relative_to(snapshot).as_posix() for path, _source in sources]
        observation = run_brokered_playwright_suite(
            snapshot, ["test", *selected], approved_urls[0],
            environment=exact_environment, timeout=timeout, runtime=runtime,
        )
        receipt = {
            "passed": observation.get("passed") is True,
            "test_files": selected,
            "execution_mode": observation.get("execution_mode"),
            "receipt": observation.get("receipt"),
            "exact_origin_evidence": {
                "approved_base_url": observation.get("approved_base_url"),
                "approved_origin": observation.get("approved_origin"),
                "origin_routes": observation.get("broker", {}).get("origin_routes", []),
                "boundary_inheritance_attested": observation.get("broker", {}).get("boundary_inheritance_attested"),
            },
            "broker": observation.get("broker"),
        }
        runner = observation.get("broker", {}).get("runner") or {}
        return {
            "argv": list(command), "cwd": ".",
            "exit_code": 0 if observation.get("passed") else 1,
            "stdout": str(runner.get("stdout", "")),
            "stderr": str(runner.get("stderr", "")),
            "timed_out": bool(runner.get("timed_out", False)),
            "output_truncated": False,
            "brokered_e2e_receipts": [receipt],
            "containment_profile": "windows-appcontainer-job-v1",
            "disposable_snapshot": True,
            "ordinary_suite_executed": True,
        }
    scenarios: list[tuple[Path, dict[str, Any]]] = []
    for path, source in sources:
        scenario = _playwright_static_browser_scenario(source)
        approved_base_url = _approved_playwright_base_url(snapshot, source)
        if approved_base_url is not None:
            scenario = _playwright_exact_origin_scenario(source, approved_base_url)
        if scenario is not None:
            scenarios.append((path, scenario))
    if not scenarios or len(scenarios) != len(files):
        return {
            "argv": list(command), "cwd": ".", "exit_code": -2,
            "stdout": "", "stderr": (
                "Playwright verification needs an engine-provable relative route or exact literal HTTPS baseURL, "
                "literal safe actions, and literal DOM assertions; project-authored worker code was not executed."
            ),
            "timed_out": False, "output_truncated": False,
            "containment_unavailable": True,
        }
    observations = []
    started = time.monotonic()
    for path, scenario in scenarios:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            return {
                "argv": list(command), "cwd": ".", "exit_code": -1,
                "stdout": "", "stderr": "Brokered Playwright verification timed out",
                "timed_out": True, "output_truncated": False,
            }
        observation = _run_brokered_playwright_scenario(
            snapshot, scenario, timeout=remaining, runtime=runtime,
        )
        observation["test_file"] = path.relative_to(snapshot).as_posix()
        observations.append(observation)
        if not observation.get("passed"):
            return {
                "argv": list(command), "cwd": ".", "exit_code": 1,
                "stdout": "", "stderr": "Engine-owned Playwright browser scenario failed",
                "timed_out": False, "output_truncated": False,
                "brokered_e2e_receipts": observations,
                "containment_profile": "windows-appcontainer-job-v1",
            }
    count = len(observations)
    return {
        "argv": list(command), "cwd": ".", "exit_code": 0,
        "stdout": f"Running {count} test{'s' if count != 1 else ''} using engine-owned contained browser probes\n{count} passed (brokered)",
        "stderr": "", "timed_out": False, "output_truncated": False,
        "brokered_e2e_receipts": observations,
        "containment_profile": "windows-appcontainer-job-v1",
        "disposable_snapshot": True,
    }


def _contained_snapshot_command(
    config: LoadedConfig,
    snapshot: Path,
    command: list[str],
    *,
    timeout: float | None = None,
    denied_root: Path | None = None,
) -> dict[str, Any]:
    """Run a command with its trusted runtime outside the writable snapshot."""

    with tempfile.TemporaryDirectory(prefix="nexus-verification-engine-") as temporary:
        engine_root = Path(temporary).resolve()
        selected_root = (denied_root or config.project_root).resolve()
        if (
            _verification_is_reparse(engine_root)
            or engine_root == selected_root
            or selected_root in engine_root.parents
            or engine_root in selected_root.parents
        ):
            return {
                "argv": command, "cwd": ".", "exit_code": -2,
                "stdout": "", "stderr": (
                    "Verification containment infrastructure could not allocate a "
                    "host-owned engine root outside selected-project data. "
                    "Nexus did not run project code."
                ),
                "timed_out": False, "output_truncated": False,
                "containment_unavailable": True,
            }
        return _contained_snapshot_command_with_engine(
            config, snapshot, command, timeout=timeout, denied_root=denied_root,
            engine_root=engine_root,
        )


def _contained_snapshot_command_with_engine(
    config: LoadedConfig,
    snapshot: Path,
    command: list[str],
    *,
    timeout: float | None = None,
    denied_root: Path | None = None,
    engine_root: Path,
) -> dict[str, Any]:
    """Run only commands with a supported fail-closed containment profile."""

    argv = list(command)
    name = Path(argv[0]).name.casefold() if argv else ""
    for executable_suffix in (".exe", ".cmd", ".bat"):
        name = name.removesuffix(executable_suffix)
    requested_pytest = name in {"pytest", "py.test"}
    if requested_pytest:
        # Console-script shims live in a project venv and are executable
        # project data.  Use the engine-owned interpreter with the same module
        # entry point instead; no project interpreter or shim is launched.
        argv = ["python", "-m", "pytest", *argv[1:]]
        name = "python"
    try:
        python_guard, node_guard = _verification_guard_files(snapshot, engine_root)
    except (HarnessError, OSError) as error:
        return {
            "argv": command, "cwd": ".", "exit_code": -2,
            "stdout": "", "stderr": (
                "Verification containment infrastructure could not be prepared: "
                + str(error) + ". Nexus did not run project code."
            ),
            "timed_out": False, "output_truncated": False,
            "containment_unavailable": True,
        }
    environment = {
        "NEXUS_VERIFICATION_ROOT": str(snapshot.resolve()),
        "NEXUS_VERIFICATION_ENGINE_ROOT": str(engine_root.resolve()),
        "HOME": str((snapshot / ".nexus-verification" / "home").resolve()),
        "USERPROFILE": str((snapshot / ".nexus-verification" / "home").resolve()),
        "TEMP": str((snapshot / ".nexus-verification" / "tmp").resolve()),
        "TMP": str((snapshot / ".nexus-verification" / "tmp").resolve()),
    }
    joined_initial = " ".join(str(one).replace("\\", "/").casefold() for one in argv)
    if name in {"node", "nodejs"} and "playwright" in joined_initial:
        CommandRunner(config)._check(command)
        if os.name != "nt" or not appcontainer_available():
            return {
                "argv": command, "cwd": ".", "exit_code": -2,
                "stdout": "", "stderr": "Brokered Playwright AppContainer containment is unavailable",
                "timed_out": False, "output_truncated": False,
                "containment_unavailable": True,
            }
        try:
            brokered_runtime = discover_bundled_playwright_runtime(required=True)
            assert brokered_runtime is not None
            project_root = (denied_root or config.project_root).resolve()
            brokered_root = brokered_runtime.root.resolve()
            if _runtime_is_selected_project_data(brokered_root, project_root):
                raise RuntimeError(
                    "the bundled Playwright runtime belongs to selected-project data"
                )
            payload = _run_brokered_playwright_specs(
                snapshot, command,
                timeout=float(timeout or config.get("execution.timeout_seconds")),
                runtime=brokered_runtime,
            )
        except (HarnessError, OSError, RuntimeError, ValueError) as error:
            return {
                "argv": command, "cwd": ".", "exit_code": -2,
                "stdout": "", "stderr": "Brokered Playwright verification is unavailable: " + str(error),
                "timed_out": False, "output_truncated": False,
                "containment_unavailable": True,
            }
        payload["argv"] = list(command)
        return payload
    if name in {"python", "python3", "py"}:
        python_dependencies = snapshot_dependency_paths(snapshot)
        environment["PYTHONPATH"] = os.pathsep.join([
            str(python_guard.parent.resolve()),
            *(str(one) for one in python_dependencies),
            str(snapshot.resolve()),
        ])
        profile = "python-audit-deny-external-write-v1"
    elif name in {"node", "nodejs"}:
        joined = " ".join(str(one).casefold() for one in argv)
        bundled_playwright = (
            discover_bundled_playwright_runtime()
            if "playwright" in joined else None
        )
        if bundled_playwright is not None:
            project_root = (denied_root or config.project_root).resolve()
            bundled_root = bundled_playwright.root.resolve()
            if _runtime_is_selected_project_data(bundled_root, project_root):
                bundled_playwright = None
        if bundled_playwright is not None:
            node_source = bundled_playwright.node.resolve()
            argv[0] = str(node_source)
            # An approved Playwright command uses the pinned engine-owned CLI;
            # project test/config arguments remain unchanged and are copied in
            # the disposable snapshot.
            for index in range(1, len(argv)):
                candidate = str(argv[index]).replace("\\", "/").casefold()
                if candidate.endswith("/playwright/cli.js") or candidate.endswith("/@playwright/test/cli.js"):
                    argv[index] = str(bundled_playwright.cli)
                    break
            environment.update(bundled_playwright.environment())
            environment["NODE_PATH"] = str(
                (bundled_playwright.root / "node_modules").resolve()
            )
        else:
            try:
                node_source = _trusted_host_node(
                    argv[0], snapshot=snapshot,
                    denied_root=denied_root or config.project_root,
                )
            except VerificationPythonUnavailable as error:
                return {
                    "argv": command, "cwd": ".", "exit_code": -2,
                    "stdout": "", "stderr": (
                        "Node permission-model containment is unavailable: "
                        + str(error) + ". Nexus did not run project code."
                    ),
                    "timed_out": False, "output_truncated": False,
                    "containment_unavailable": True,
                }
            argv[0] = str(node_source)
        try:
            version = subprocess.run(
                [str(node_source), "--version"], check=False, capture_output=True,
                text=True, timeout=5,
            ).stdout.strip()
            major = int(version.lstrip("v").split(".", 1)[0])
        except (OSError, ValueError, subprocess.SubprocessError):
            major = 0
        if major < 20:
            return {
                "argv": command, "cwd": ".", "exit_code": -2,
                "stdout": "", "stderr": "Node permission-model containment is unavailable",
                "timed_out": False, "output_truncated": False,
                "containment_unavailable": True,
            }
        joined = " ".join(str(one).casefold() for one in argv)
        allow_child = "playwright" in joined
        permissions = [
            "--permission", "--allow-fs-read=*",
            # SUBST-backed canonical paths may resolve to either the private
            # drive or its host path.  Let the Node seat-belt permit fs APIs;
            # the AppContainer DACL remains the authoritative write boundary
            # and grants Modify only to this disposable snapshot.
            "--allow-fs-write=*",
        ]
        if allow_child:
            permissions.append("--allow-child-process")
        argv = [argv[0], *permissions, *argv[1:]]
        # Keep the permission model on the executable argv, where the engine
        # can bind and audit it.  Absolute preload-module resolution asks Node
        # to lstat each drive-root ancestor, which a zero-capability
        # AppContainer correctly denies before user code starts.  The OS token
        # is the authoritative boundary; Node's own permission model denies
        # child processes unless the brokered Playwright profile is selected.
        environment.pop("NODE_OPTIONS", None)
        profile = "node-permission-guarded-child-v1"
    else:
        return {
            "argv": command, "cwd": ".", "exit_code": -2,
            "stdout": "", "stderr": (
                "No supported verification containment profile exists for executable: "
                + (argv[0] if argv else "(empty)")
            ),
            "timed_out": False, "output_truncated": False,
            "containment_unavailable": True,
        }
    effective_timeout = float(timeout or config.get("execution.timeout_seconds"))
    if effective_timeout < 0.250:
        # Native containment attestation itself has a non-zero startup cost.
        # A nearly exhausted tool deadline must fail before launching either
        # the canary or project code instead of overrunning the caller's
        # absolute deadline by seconds.
        return {
            "argv": command, "cwd": ".", "exit_code": 124,
            "stdout": "", "stderr": "Verification deadline expired before contained process launch",
            "duration_ms": 0, "timed_out": True,
            "output_truncated": False,
            "containment_profile": "preflight-deadline-fail-closed",
        }
    if os.name == "nt":
        if not appcontainer_available():
            return {
                "argv": command, "cwd": ".", "exit_code": -2,
                "stdout": "", "stderr": "Windows AppContainer containment is unavailable",
                "timed_out": False, "output_truncated": False,
                "containment_unavailable": True,
            }
        # Attest the native boundary while the snapshot is still small.  The
        # AppContainer launcher applies a recursive capability ACL, so staging
        # a full language runtime before this check made the canary needlessly
        # expensive without strengthening the attestation.
        canary = _windows_containment_canary(
            snapshot, environment, denied_root or config.project_root,
        )
        if not canary.get("passed"):
            return {
                "argv": command, "cwd": ".", "exit_code": -2,
                "stdout": "", "stderr": "Windows containment canary failed: " + str(canary.get("reason") or "unknown"),
                "timed_out": False, "output_truncated": False,
                "containment_unavailable": True, "containment_attestation": canary,
            }
        runtime_root = python_guard.parent / "runtime"
        contained_read_roots: tuple[Path, ...] = (python_guard.parent,)
        if name in {"python", "python3", "py"}:
            bundled = discover_packaged_runtime(module_file=Path(__file__))
            project_root = (denied_root or config.project_root).resolve()
            if bundled is not None:
                bundled_root = bundled.resolve()
                if _runtime_is_selected_project_data(bundled_root, project_root):
                    bundled = None
            try:
                if bundled is None:
                    stage_source_runtime(
                        runtime_root,
                        snapshot=snapshot,
                        python_guard_parent=python_guard.parent,
                        dependency_paths=python_dependencies,
                    )
                else:
                    _stage_packaged_python_runtime(
                        bundled, runtime_root, snapshot=snapshot,
                        python_guard_parent=python_guard.parent,
                        dependency_paths=python_dependencies,
                    )
            except (VerificationPythonUnavailable, OSError) as error:
                return {
                    "argv": command, "cwd": ".", "exit_code": -2,
                    "stdout": "", "stderr": (
                        "Lightweight Python containment runtime is unavailable: "
                        + str(error) + ". Nexus did not run project code."
                    ),
                    "timed_out": False, "output_truncated": False,
                    "containment_unavailable": True,
                }
            argv[0] = str(runtime_root / "python.exe")
            if bundled is not None:
                contained_read_roots += (bundled,)
            # Child Python processes and vetted native dependencies may run,
            # but only from engine-owned immutable runtime roots.  They inherit
            # the same AppContainer token and no-breakaway Job.  Project-owned
            # executables remain outside this executable allowlist.
            allowed_roots = [str(runtime_root.resolve())]
            if bundled is not None:
                allowed_roots.extend((
                    str(bundled.resolve()),
                    str((bundled / "Lib" / "site-packages").resolve()),
                ))
            environment["NEXUS_ALLOWED_EXEC_ROOTS"] = os.pathsep.join(allowed_roots)
        else:
            try:
                argv[0] = str(_stage_node_runtime(node_source, runtime_root))
                if bundled_playwright is not None:
                    contained_read_roots += (bundled_playwright.root,)
                node_workspace = snapshot / ".nexus-verification" / "node-workspace"
                _recreate_verification_directory(node_workspace)
                (node_workspace / ".nexus-verification").mkdir()
                for child in snapshot.iterdir():
                    if child.name == ".nexus-verification":
                        continue
                    destination = node_workspace / child.name
                    if child.is_dir() and not child.is_symlink():
                        shutil.copytree(child, destination, symlinks=True)
                    elif child.is_file() and not child.is_symlink():
                        shutil.copy2(child, destination)
            except (VerificationPythonUnavailable, OSError) as error:
                return {
                    "argv": command, "cwd": ".", "exit_code": -2,
                    "stdout": "", "stderr": (
                        "Node containment runtime is unavailable: " + str(error)
                        + ". Nexus did not run project code."
                    ),
                    "timed_out": False, "output_truncated": False,
                    "containment_unavailable": True,
                }
            # Node canonicalizes script/argument paths by walking from the
            # drive root.  A zero-capability AppContainer intentionally cannot
            # enumerate that root, even though it has an ACE on the snapshot.
            # Express snapshot-owned paths relative to the contained cwd so
            # Node never asks for broader ancestor authority.
            for index in range(1, len(argv)):
                value = str(argv[index])
                try:
                    candidate = Path(value)
                    if candidate.is_absolute() and candidate.resolve().is_relative_to(snapshot.resolve()):
                        argv[index] = os.path.relpath(candidate, snapshot)
                except (OSError, ValueError):
                    continue
        CommandRunner(config)._check(command)
        if requested_pytest:
            CommandRunner(config)._check(["python", "-m", "pytest", *command[1:]])
        payload = run_appcontainer(snapshot, argv, {
            **{
                key: os.environ[key] for key in (
                    "SystemRoot", "WINDIR", "COMSPEC", "PATH", "PATHEXT",
                    "SYSTEMDRIVE", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA",
                    "APPDATA", "USERNAME", "ALLUSERSPROFILE", "ProgramData",
                    "PUBLIC", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS",
                    "ProgramFiles", "ProgramFiles(x86)", "CommonProgramFiles",
                ) if key in os.environ
            },
            **environment,
        }, effective_timeout,
            persistent_profile=verification_runtime_profile(),
            read_execute_roots=contained_read_roots,
            transient_read_execute_roots=(python_guard.parent,),
            # PrivateNetworkClientServer permits only contained local-service
            # coordination.  Python's engine guard restricts socket endpoints
            # to loopback; native libraries and child executables are accepted
            # only from immutable engine runtime roots.  No InternetClient
            # capability is given here.
            capability_sids=("S-1-15-3-3",) if (
                name in {"python", "python3", "py"}
                or (name in {"node", "nodejs"} and allow_child)
            ) else (),
            map_authorized_roots=name in {"node", "nodejs"},
            nested_mapped_cwd=name in {"node", "nodejs"},
        )
        payload["containment_attestation"] = canary
        profile = payload.get("containment_profile", "windows-appcontainer-job-v1")
    else:
        # Language audit/permission hooks are useful diagnostics, not a
        # security boundary.  Until the host provides the required namespace /
        # Landlock or signed sandbox helper, do not execute project code.
        return {
            "argv": command, "cwd": ".", "exit_code": -2,
            "stdout": "", "stderr": "OS verification containment is unavailable on this host",
            "timed_out": False, "output_truncated": False,
            "containment_unavailable": True,
        }
    payload["containment_profile"] = profile
    if (
        requested_pytest and payload.get("exit_code") not in {0, None}
        and "no module named pytest" in str(payload.get("stderr") or "").casefold()
    ):
        payload["stderr"] = str(payload.get("stderr") or "") + (
            "\nPytest is not present in the contained project snapshot. Prepare it "
            "beforehand in .venv/Lib/site-packages, venv/Lib/site-packages, "
            "__pypackages__/3.11/lib, or vendor. Nexus does not install packages silently."
        )
    payload["contained_argv"] = argv
    payload["argv"] = list(command)
    return payload


def _containment_owns_runner_availability(command: list[str]) -> bool:
    """Whether the containment layer, not host PATH, selects this runtime.

    Python and pytest are rewritten to an engine-staged interpreter. Node may
    be replaced by the bundled Playwright runtime or resolved and staged by
    the guarded Node profile. Rejecting any of those names with an outer
    ``which`` probe defeats the exact portability boundary that owns them.
    Absolute/package-private runtime paths are classified by basename because
    the containment layer never launches the supplied Python/pytest binary and
    independently validates any selected Node binary before staging it.
    """

    if not command:
        return False
    name = Path(str(command[0])).name.casefold()
    for suffix in (".exe", ".cmd", ".bat"):
        name = name.removesuffix(suffix)
    return name in {
        "python", "python3", "py", "pytest", "py.test", "node", "nodejs",
    }


def _windows_containment_canary(
    snapshot: Path, environment: dict[str, str], denied_root: Path,
) -> dict[str, Any]:
    """Attest native/child/reparse denial before project code is launched."""

    nonce = uuid.uuid4().hex
    local = snapshot / ("canary-local-" + nonce)
    child_local = snapshot / ("canary-child-local-" + nonce)
    sibling = snapshot.parent / ("nexus-denied-sibling-" + nonce)
    original = denied_root / ("nexus-denied-original-" + nonce)
    junction = snapshot / ("nexus-canary-junction-" + nonce)
    via_junction = junction / ("nexus-denied-reparse-" + nonce)
    targets = [sibling, original, via_junction]
    canary_environment = {
        **{
            key: os.environ[key] for key in (
                "SystemRoot", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "SYSTEMDRIVE",
                "LOCALAPPDATA", "APPDATA", "USERNAME", "ALLUSERSPROFILE", "ProgramData",
                "PUBLIC", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS",
                "ProgramFiles", "ProgramFiles(x86)", "CommonProgramFiles",
            ) if key in os.environ
        },
        **environment,
    }
    default_windows = "C:" + os.sep + "Windows"
    executable = str(Path(os.environ.get("SystemRoot", default_windows)) / "System32" / "cmd.exe")
    commands = [f"echo local>{local.name}", f"echo denied>..\\{sibling.name}", f'echo denied>"{original}"']
    commands.append(f"echo denied>{junction.name}\\{via_junction.name}")
    commands.append(f"cmd.exe /c echo childlocal>{child_local.name}")
    commands.append(f"cmd.exe /c echo childdenied>..\\{sibling.name}")
    # One profile/process is enough: cmd continues after each denied redirect,
    # and the final nested cmd proves that child processes inherit the token.
    results = [run_appcontainer(
        snapshot, [executable, "/d", "/s", "/c", " & ".join(commands)],
        canary_environment, 15.0, reparse_probe=(junction, denied_root),
    )]
    def directory_contains(path: Path) -> bool:
        for _attempt in range(5):
            try:
                if any(one.name == path.name for one in path.parent.iterdir()):
                    return True
            except OSError:
                pass
            time.sleep(0.02)
        return False

    escaped = [str(one) for one in targets if directory_contains(one)]
    local_present = directory_contains(local)
    child_local_present = directory_contains(child_local)
    cleanup_removed = list(results[0].get("cleanup_reparse_entries_removed") or [])
    cleanup_error = str(results[0].get("containment_cleanup_error") or "")
    reparse_removed = (
        results[0].get("reparse_created") is True
        and junction.name in cleanup_removed
    )
    # The canary deliberately asks cmd.exe to perform denied redirections.  A
    # non-zero cmd exit is therefore expected evidence of containment, not a
    # failed canary.  Cleanup itself must still be completely successful.
    execution_clean = not cleanup_error
    passed = (
        local_present and child_local_present and not escaped
        and execution_clean and reparse_removed
    )
    local.unlink(missing_ok=True)
    child_local.unlink(missing_ok=True)
    for one in targets:
        if one.exists() and one.is_file():
            one.unlink(missing_ok=True)
    if junction.exists():
        try:
            junction.unlink(missing_ok=True)
        except OSError:
            try:
                junction.rmdir()
            except OSError:
                pass
    return {
        "schema_version": 1,
        "passed": passed,
        "profile": results[0].get("containment_profile"),
        "native_write_denied": not escaped,
        "private_write_allowed": local_present,
        "escaped_paths": escaped,
        "child_inherited_boundary": child_local_present and str(sibling) not in escaped,
        "child_process_started": child_local_present,
        "reparse_checked": reparse_removed,
        "cleanup_reparse_entries_removed": cleanup_removed,
        "containment_cleanup_error": cleanup_error,
        "reason": "" if passed else (
            "native canary escaped, cleanup failed, or could not write its private snapshot: "
            + " | ".join(
                "exit=" + str(one.get("exit_code")) + " "
                + str(one.get("stderr") or one.get("stdout") or "no process output")[:250]
                for one in results
            )
        ),
    }


def _run_disposable_verification_command(
    config: LoadedConfig,
    root: Path,
    command: list[str],
    *,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Run one approved command in a fresh project copy, never in-place."""

    with tempfile.TemporaryDirectory(prefix="nexus-verification-") as temporary:
        frozen = Path(temporary) / "frozen-project"
        snapshot = Path(temporary) / "project"
        baseline_merkle, baseline_manifest = _project_tree_merkle(root)
        _copy_verification_snapshot(root, frozen)
        _copy_verification_snapshot(frozen, snapshot)
        snapshot_config = LoadedConfig(
            copy.deepcopy(config.data), snapshot.resolve(), list(config.sources),
            dict(config.provenance), copy.deepcopy(config.trusted_floor),
        )
        try:
            payload = _contained_snapshot_command(
                snapshot_config, snapshot, _snapshot_command(command, root, snapshot),
                timeout=timeout, denied_root=root,
            )
        except OSError as error:
            # Native containment setup can fail before CreateProcess (for
            # example, when Windows cannot register an AppContainer SID for
            # the current logon token).  This is verification infrastructure,
            # not a missing selected-project runner and not evidence that the
            # applied work needs another provider repair attempt.
            payload = {
                "argv": list(command), "cwd": ".", "exit_code": -2,
                "stdout": "", "stderr": str(error), "duration_ms": 0,
                "timed_out": False, "output_truncated": False,
                "containment_unavailable": True,
                "containment_setup_failed_before_launch": True,
            }
        # Public evidence remains bound to the approved argv, not ephemeral
        # temporary paths that disappear after this call.
        payload["argv"] = list(command)
        payload["cwd"] = "."
        payload["disposable_snapshot"] = True
        after_merkle, _after_manifest = _project_tree_merkle(root)
        if after_merkle != baseline_merkle:
            recovered = _restore_verification_escape(root, frozen, baseline_manifest)
            payload["verification_escape_detected"] = True
            payload["verification_escape_recovered"] = recovered
            if not recovered:
                raise HarnessError(
                    "Selected-project verification escaped its disposable snapshot and exact project recovery failed"
                )
        return payload


def _trace_probe_command(
    command: list[str], files: list[str], cover_dir: Path, root: Path,
) -> list[str] | None:
    words = [
        Path(part).name.casefold().removesuffix(".exe").removesuffix(".cmd").removesuffix(".bat")
        for part in command
    ]
    module = ""
    prefix: list[str] = []
    tail: list[str] = []
    for candidate in ("unittest", "pytest"):
        for index in range(len(words) - 1):
            if words[index:index + 2] == ["-m", candidate]:
                module = candidate
                prefix = command[:index]
                tail = (["-v"] if candidate == "unittest" else ["-q"]) + files
                break
        if module:
            break
    if not module or not prefix:
        joined = " ".join(str(one).replace("\\", "/").casefold() for one in command)
        probe = _level_probe_command(command, files)
        node = shutil.which("node")
        if probe is not None and node and any(name in joined for name in ("playwright", "vitest", "jest")):
            argv = list(probe)
            if Path(argv[0]).name.casefold().removesuffix(".exe") == "node":
                child = argv[1:]
            else:
                cli = ""
                if "playwright" in joined:
                    choices = (
                        root / "node_modules" / "@playwright" / "test" / "cli.js",
                        root / "node_modules" / "playwright" / "cli.js",
                    )
                elif "vitest" in joined:
                    choices = (root / "node_modules" / "vitest" / "vitest.mjs",)
                else:
                    choices = (root / "node_modules" / "jest" / "bin" / "jest.js",)
                cli = next((str(one.resolve()) for one in choices if one.is_file()), "")
                if not cli:
                    return None
                child = [cli, *argv[1:]]
            cover_dir.mkdir(parents=True, exist_ok=True)
            launcher = (
                "const cp=require('node:child_process');"
                "const a=JSON.parse(process.argv[1]);"
                "process.env.NODE_V8_COVERAGE=a.coverage;"
                "const r=cp.spawnSync(process.execPath,a.argv,{cwd:process.cwd(),env:process.env,encoding:'utf8'});"
                "if(r.stdout)process.stdout.write(r.stdout);if(r.stderr)process.stderr.write(r.stderr);"
                "process.exit(r.status===null?1:r.status);"
            )
            return [
                node, "-e", launcher,
                json.dumps({"coverage": str(cover_dir), "argv": child}, separators=(",", ":")),
            ]
        if probe is not None and words and words[0] == "go" and "test" in words:
            cover_dir.mkdir(parents=True, exist_ok=True)
            return [*probe, "-coverprofile=" + str(cover_dir / "go.cover")]
        return None
    # The stdlib ``trace`` writer attempts to reopen frozen/zip-imported
    # bundled-runtime modules and can fail after the selected tests passed.
    # Use the engine-owned runtime tracer instead; it records exact executed
    # project code objects and does not depend on stdlib source files.
    return _python_runtime_trace_command(
        command, files, cover_dir / "callable-events.json", root,
    )


def _coverage_hits_changed(cover_dir: Path, production_paths: list[str]) -> list[str]:
    hits: list[str] = []
    stems = {Path(one).stem.casefold(): one for one in production_paths}
    for report in cover_dir.rglob("*.cover") if cover_dir.exists() else []:
        stem = report.stem.casefold()
        relative = next((path for key, path in stems.items() if key == stem or stem.endswith("." + key)), "")
        if not relative:
            continue
        try:
            content = report.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if re.search(r"(?m)^\s*\d+\s*:", content):
            hits.append(relative)
    for report in cover_dir.glob("*.json") if cover_dir.exists() else []:
        try:
            payload = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("events"), list):
            event_paths = {
                str(event.get("path") or "").replace("\\", "/").casefold()
                for event in payload["events"] if isinstance(event, dict)
                and event.get("event") in {"call", "return", "exception"}
            }
            for relative in production_paths:
                if relative.replace("\\", "/").casefold() in event_paths:
                    hits.append(relative)
            continue
        scripts = payload.get("result", []) if isinstance(payload, dict) else []
        for script in scripts if isinstance(scripts, list) else []:
            if not isinstance(script, dict):
                continue
            url = str(script.get("url") or "").replace("\\", "/").casefold()
            executed = any(
                int(region.get("count", 0)) > 0
                for function in script.get("functions", []) if isinstance(function, dict)
                for region in function.get("ranges", []) if isinstance(region, dict)
            )
            if not executed:
                continue
            for relative in production_paths:
                if url.endswith("/" + relative.replace("\\", "/").casefold()):
                    hits.append(relative)
    go_profile = cover_dir / "go.cover"
    if go_profile.is_file():
        try:
            lines = go_profile.read_text(encoding="utf-8", errors="strict").splitlines()[1:]
        except (OSError, UnicodeError):
            lines = []
        for relative in production_paths:
            folded = relative.replace("\\", "/").casefold()
            if any(
                line.replace("\\", "/").casefold().split(":", 1)[0].endswith(folded)
                and bool(re.search(r"\s[1-9]\d*$", line))
                for line in lines
            ):
                hits.append(relative)
    return list(dict.fromkeys(hits))


def _python_callable_catalog(path: Path) -> dict[str, dict[str, Any]]:
    """Return stable source identities for Python callables in one snapshot."""

    try:
        source = path.read_text(encoding="utf-8", errors="strict")
        tree = ast.parse(source)
    except (OSError, UnicodeError, SyntaxError):
        return {}
    catalog: dict[str, dict[str, Any]] = {}

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.parents: list[str] = []

        def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            qualname = ".".join([*self.parents, node.name])
            segment = ast.get_source_segment(source, node) or ""
            catalog[qualname] = {
                "qualname": qualname,
                "name": node.name,
                "firstlineno": int(node.lineno),
                "end_lineno": int(getattr(node, "end_lineno", node.lineno)),
                "ast_sha256": hashlib.sha256(ast.dump(
                    node, annotate_fields=True, include_attributes=False,
                ).encode("utf-8")).hexdigest(),
                "source_sha256": hashlib.sha256(segment.encode("utf-8")).hexdigest(),
            }
            self.parents.append(node.name)
            self.generic_visit(node)
            self.parents.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._function(node)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.parents.append(node.name)
            self.generic_visit(node)
            self.parents.pop()

    Visitor().visit(tree)
    return catalog


def _python_runtime_trace_command(
    command: list[str], files: list[str], output: Path, root: Path,
) -> list[str] | None:
    """Wrap pytest/unittest with an engine-owned exact code-object tracer."""

    words = [
        Path(part).name.casefold().removesuffix(".exe")
        for part in command
    ]
    module = ""
    executable = ""
    for candidate in ("unittest", "pytest"):
        for index in range(len(words) - 1):
            if words[index:index + 2] == ["-m", candidate]:
                executable = command[0]
                module = candidate
                break
        if module:
            break
    if not module or not executable:
        return None
    probe = _level_probe_command(command, files)
    if probe is None:
        return None
    probe_words = [str(one) for one in probe]
    try:
        marker = next(index for index in range(len(probe_words) - 1)
                      if probe_words[index:index + 2] == ["-m", module])
    except StopIteration:
        return None
    tail = probe_words[marker + 2:]
    output.parent.mkdir(parents=True, exist_ok=True)
    bootstrap = output.parent / "nexus_callable_trace.py"
    script = r'''import hashlib,json,os,runpy,sys
ROOT=os.path.realpath(sys.argv[1]); OUT=sys.argv[2]; MODULE=sys.argv[3]; ARGS=json.loads(sys.argv[4]); EVENTS=[]
def inside(path):
    if not path or str(path).startswith('<'): return False
    try: return os.path.normcase(os.path.commonpath([ROOT,os.path.realpath(path)]))==os.path.normcase(ROOT)
    except Exception: return False
def trace(frame,event,arg):
    code=frame.f_code
    if event in ('call','return','exception') and inside(code.co_filename):
        rel=os.path.relpath(os.path.realpath(code.co_filename),ROOT).replace(chr(92),chr(47))
        raw=code.co_code+repr(code.co_consts).encode('utf-8','replace')
        item={'event':event,'path':rel,'qualname':getattr(code,'co_qualname',code.co_name),'name':code.co_name,'firstlineno':code.co_firstlineno,'code_sha256':hashlib.sha256(raw).hexdigest()}
        if event=='exception' and isinstance(arg,tuple) and arg: item['exception_type']=getattr(arg[0],'__name__',str(arg[0]))
        EVENTS.append(item)
    return trace
status=0
sys.path.insert(0,ROOT)
sys.settrace(trace); sys.argv=[MODULE,*ARGS]
try:
    runpy.run_module(MODULE,run_name='__main__',alter_sys=True)
except SystemExit as exc:
    status=exc.code if isinstance(exc.code,int) else (0 if exc.code is None else 1)
finally:
    sys.settrace(None)
    with open(OUT,'w',encoding='utf-8') as handle: json.dump({'events':EVENTS},handle,sort_keys=True,separators=(',',':'))
raise SystemExit(status)
'''
    atomic_write(bootstrap, script.encode("utf-8"))
    return [
        executable, str(bootstrap), str(root.resolve()), str(output), module,
        json.dumps(tail, separators=(",", ":")),
    ]


def _runtime_callable_identity(
    root: Path,
    counter_root: Path,
    production_paths: list[str],
    semantic_trace: dict[str, Any],
    events: list[dict[str, Any]],
    predicate: str,
    goal: str,
) -> dict[str, Any] | None:
    """Bind the test oracle to an exact production code object, not a module."""

    component = str(semantic_trace.get("production_component") or "").casefold()
    symbol = str(semantic_trace.get("production_call") or "").rsplit(".", 1)[-1]
    relative = next((
        one for one in production_paths
        if Path(one).stem.casefold() == component
    ), "")
    if not relative or not symbol:
        return None
    current_catalog = _python_callable_catalog(confined_path(root, relative, allow_missing=False))
    baseline_catalog = _python_callable_catalog(confined_path(counter_root, relative, allow_missing=True))
    matching = [
        value for value in current_catalog.values()
        if str(value.get("name")) == symbol
    ]
    baseline_public = [
        value for value in baseline_catalog.values()
        if not str(value.get("name") or "").startswith("_")
    ]
    explicitly_named = bool(re.search(rf"\b{re.escape(symbol)}\b", goal, re.I))
    if len(matching) != 1 or (len(baseline_public) > 1 and not explicitly_named):
        return None
    target = matching[0]
    calls = [
        event for event in events
        if isinstance(event, dict)
        and str(event.get("path") or "").casefold() == relative.casefold()
        and str(event.get("qualname") or "").split(".")[-1] == symbol
    ]
    if not any(event.get("event") == "call" for event in calls):
        return None
    required_event = "exception" if predicate == "REJECT" else "return"
    observable = next((event for event in calls if event.get("event") == required_event), None)
    if observable is None:
        return None
    identity = {
        "path": relative,
        "qualname": target["qualname"],
        "firstlineno": target["firstlineno"],
        "ast_sha256": target["ast_sha256"],
        "runtime_code_sha256": observable.get("code_sha256"),
        "observable_event": required_event,
        "exception_type": observable.get("exception_type", ""),
    }
    identity["identity_digest"] = hashlib.sha256(json.dumps(
        identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    return identity


def _v8_runtime_callable_identity(
    cover_dir: Path,
    root: Path,
    production_paths: list[str],
    semantic_trace: dict[str, Any],
) -> dict[str, Any] | None:
    """Resolve one executed JS export/function range from precise V8 data."""

    component = str(semantic_trace.get("production_component") or "").casefold()
    symbol = str(semantic_trace.get("production_call") or "").rsplit(".", 1)[-1]
    relative = next((
        one for one in production_paths if Path(one).stem.casefold() == component
    ), "")
    if not relative or not symbol:
        return None
    try:
        source = confined_path(root, relative, allow_missing=False).read_text(
            encoding="utf-8", errors="strict",
        )
    except (HarnessError, OSError, UnicodeError):
        return None
    definition = re.search(
        rf"(?:exports\s*\.\s*{re.escape(symbol)}|(?:function|class)\s+{re.escape(symbol)}\b|"
        rf"(?:const|let|var)\s+{re.escape(symbol)}\s*=)",
        source, re.I,
    )
    if definition is None:
        return None
    definition_start = definition.start()
    definition_end = source.find(";", definition.end())
    if definition_end < 0:
        definition_end = len(source)
    else:
        definition_end += 1
    for report in cover_dir.glob("*.json") if cover_dir.exists() else []:
        try:
            payload = json.loads(report.read_text(encoding="utf-8", errors="strict"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        scripts = payload.get("result", []) if isinstance(payload, dict) else []
        for script in scripts if isinstance(scripts, list) else []:
            if not isinstance(script, dict):
                continue
            url = str(script.get("url") or "").replace("\\", "/")
            if not url.casefold().endswith("/" + relative.casefold()):
                continue
            for function in script.get("functions", []):
                if not isinstance(function, dict):
                    continue
                name = str(function.get("functionName") or "")
                ranges = [
                    region for region in function.get("ranges", [])
                    if isinstance(region, dict) and int(region.get("count", 0)) > 0
                ]
                if not ranges:
                    continue
                primary = ranges[0]
                start = int(primary.get("startOffset", -1))
                end = int(primary.get("endOffset", -1))
                named = bool(re.search(rf"(?:^|[.$]){re.escape(symbol)}$", name))
                # A module wrapper contains every definition and is therefore
                # not callable identity. The executed function itself must be
                # named or begin inside the exact definition span.
                contained = start >= definition_start and start <= definition_end
                if not named and not contained:
                    continue
                identity = {
                    "path": relative, "qualname": name or symbol,
                    "source_start": definition_start, "source_end": definition_end,
                    "runtime_start": start, "runtime_end": end,
                    "source_sha256": hashlib.sha256(
                        source[definition_start:definition_end].encode("utf-8")
                    ).hexdigest(),
                }
                identity["identity_digest"] = hashlib.sha256(json.dumps(
                    identity, sort_keys=True, separators=(",", ":"),
                ).encode("utf-8")).hexdigest()
                return identity
    return None


def _frozen_acceptance_target(
    root: Path,
    counter_root: Path,
    production_paths: list[str],
    witness: dict[str, Any],
    goal: str,
    changed: list[str],
) -> dict[str, Any] | None:
    """Resolve a target only from the user or a pre-existing regression.

    A newly added project test may corroborate a frozen target, but it cannot
    choose which callable Nexus will invoke to decide that the user's goal is
    complete.
    """

    semantic = witness.get("semantic_trace", {})
    if not isinstance(semantic, dict):
        return None
    component = str(semantic.get("production_component") or "")
    symbol = str(semantic.get("production_call") or "").rsplit(".", 1)[-1]
    browser_scenario = semantic.get("browser_scenario")
    if isinstance(browser_scenario, dict):
        route = str(browser_scenario.get("route") or "")
        relative_route = route.lstrip("/")
        if not relative_route or relative_route not in {
            str(one).replace("\\", "/") for one in production_paths
        }:
            return None
        explicit_route = route.casefold() in goal.casefold() or relative_route.casefold() in goal.casefold()
        pre_existing_regression = str(witness.get("path") or "").casefold() not in {
            str(one).casefold() for one in changed
        }
        if not explicit_route and not pre_existing_regression:
            return None
        try:
            current_hash = file_sha256(confined_path(root, relative_route, allow_missing=False))
            baseline_hash = file_sha256(confined_path(counter_root, relative_route, allow_missing=False))
        except (HarnessError, OSError):
            return None
        target = {
            "path": relative_route,
            "module": "browser-route",
            "qualname": route,
            "baseline_qualname": route,
            "current_ast_sha256": current_hash,
            "baseline_ast_sha256": baseline_hash,
            "route_method": "GET",
            "resolution": "explicit_user_route" if explicit_route else "pre_existing_regression",
        }
        target["target_digest"] = hashlib.sha256(json.dumps(
            target, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        return target
    relative = next((
        one for one in production_paths
        if Path(one).stem.casefold() == component.casefold()
    ), "")
    if not relative or not symbol:
        return None
    explicit = bool(re.search(
        rf"(?<![\w])(?:{re.escape(component)}\s*\.\s*)?{re.escape(symbol)}\b",
        goal, re.I,
    ))
    pre_existing_regression = str(witness.get("path") or "").casefold() not in {
        str(one).casefold() for one in changed
    }
    if not explicit and not pre_existing_regression:
        return None
    path_suffix = Path(relative).suffix.casefold()
    if path_suffix in {".js", ".cjs"}:
        try:
            current_source = confined_path(root, relative, allow_missing=False).read_text(
                encoding="utf-8", errors="strict",
            )
            baseline_source = confined_path(counter_root, relative, allow_missing=False).read_text(
                encoding="utf-8", errors="strict",
            )
        except (HarnessError, OSError, UnicodeError):
            return None
        pattern = re.compile(
            rf"(?:exports\s*\.\s*{re.escape(symbol)}\b|(?:function|class)\s+{re.escape(symbol)}\b|"
            rf"(?:const|let|var)\s+{re.escape(symbol)}\s*=)", re.I,
        )
        if len(pattern.findall(current_source)) != 1 or not pattern.search(baseline_source):
            return None
        target = {
            "path": relative, "module": relative, "qualname": symbol,
            "baseline_qualname": symbol,
            "current_ast_sha256": hashlib.sha256(current_source.encode("utf-8")).hexdigest(),
            "baseline_ast_sha256": hashlib.sha256(baseline_source.encode("utf-8")).hexdigest(),
            "resolution": "explicit_user_symbol" if explicit else "pre_existing_regression",
        }
        target["target_digest"] = hashlib.sha256(json.dumps(
            target, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        return target
    suffix = "." + symbol.casefold()
    current_catalog = _python_callable_catalog(confined_path(root, relative, allow_missing=False))
    baseline_catalog = _python_callable_catalog(confined_path(counter_root, relative, allow_missing=True))
    current = [
        value for key, value in current_catalog.items()
        if key.casefold() == symbol.casefold() or key.casefold().endswith(suffix)
    ]
    baseline = [
        value for key, value in baseline_catalog.items()
        if key.casefold() == symbol.casefold() or key.casefold().endswith(suffix)
    ]
    if len(current) != 1 or len(baseline) != 1:
        return None
    target = {
        "path": relative,
        "module": relative.removesuffix(Path(relative).suffix).replace("/", ".").replace("\\", "."),
        "qualname": current[0]["qualname"],
        "baseline_qualname": baseline[0]["qualname"],
        "current_ast_sha256": current[0]["ast_sha256"],
        "baseline_ast_sha256": baseline[0]["ast_sha256"],
        "resolution": "explicit_user_symbol" if explicit else "pre_existing_regression",
    }
    target["target_digest"] = hashlib.sha256(json.dumps(
        target, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    return target


def _direct_python_probe_command(
    root: Path, target: dict[str, Any], scenario: dict[str, Any], output: Path,
) -> list[str]:
    """Build an engine-owned direct callable probe outside project authorship."""

    script = output.parent / "nexus_direct_acceptance_probe.py"
    source = r'''import asyncio,hashlib,importlib,json,os,sys,traceback
ROOT=os.path.realpath(sys.argv[1]); TARGET=json.loads(sys.argv[2]); SCENARIO=json.loads(sys.argv[3]); OUT=sys.argv[4]
sys.path.insert(0,ROOT); result={'passed':False,'target':TARGET,'scenario':SCENARIO}
try:
    module=importlib.import_module(TARGET['module']); obj=module
    parts=TARGET['qualname'].split('.')
    if len(parts)>1:
        owner=getattr(obj,parts[0]); obj=owner()
        for part in parts[1:]: obj=getattr(obj,part)
    else: obj=getattr(obj,parts[0])
    code=getattr(obj,'__func__',obj).__code__
    result['runtime_identity']={'path':os.path.relpath(os.path.realpath(code.co_filename),ROOT).replace(chr(92),chr(47)),'qualname':getattr(code,'co_qualname',code.co_name),'firstlineno':code.co_firstlineno,'code_sha256':hashlib.sha256(code.co_code+repr(code.co_consts).encode('utf-8','replace')).hexdigest()}
    stimulus='' if SCENARIO['stimulus_property']=='empty' else ('Ångström 東京 🚀' if SCENARIO['stimulus_property']=='unicode' else '{malformed')
    try:
        value=obj(stimulus)
        if hasattr(value,'__await__'): value=asyncio.run(value)
        result['observable']='return'; result['return_repr']=repr(value)[:1000]
        result['passed']=SCENARIO['predicate']=='ROUNDTRIP' and value==stimulus
    except BaseException as exc:
        result['observable']='exception'; result['exception_type']=type(exc).__name__; result['exception_message']=str(exc)[:1000]
        frames=traceback.extract_tb(exc.__traceback__); result['origin_in_target']=any(os.path.realpath(one.filename)==os.path.realpath(code.co_filename) and one.lineno>=code.co_firstlineno for one in frames)
        result['passed']=SCENARIO['predicate']=='REJECT' and result['origin_in_target']
except BaseException as exc:
    result['probe_error']=type(exc).__name__+': '+str(exc)
with open(OUT,'w',encoding='utf-8') as handle: json.dump(result,handle,sort_keys=True,separators=(',',':'),ensure_ascii=False)
raise SystemExit(0 if result.get('passed') else 3)
'''
    atomic_write(script, source.encode("utf-8"))
    return [
        sys.executable, str(script), str(root.resolve()),
        json.dumps(target, separators=(",", ":"), ensure_ascii=False),
        json.dumps(scenario, separators=(",", ":"), ensure_ascii=False),
        str(output),
    ]


def _direct_js_probe_command(
    root: Path, target: dict[str, Any], scenario: dict[str, Any], output: Path,
) -> list[str] | None:
    node = shutil.which("node")
    if not node:
        return None
    # Use an engine-owned eval body instead of a script filename.  Node's main
    # module resolver walks every drive-root ancestor (which the AppContainer
    # deliberately cannot enumerate); eval can operate entirely from the
    # already-authorized contained cwd.
    source = r'''const fs=require('node:fs'),path=require('node:path'),Module=require('node:module');
const target=JSON.parse(process.argv[2]),scenario=JSON.parse(process.argv[3]),out=process.argv[4];
(async()=>{const result={passed:false,target,scenario};try{const filename=String(target.path).replaceAll('\\','/'),loader=new Module(filename);loader.filename=filename;loader.paths=[];loader._compile(fs.readFileSync(filename,'utf8'),filename);const mod=loader.exports;let obj=mod;const parts=target.qualname.split('.');if(parts.length>1){obj=new obj[parts[0]]();for(const p of parts.slice(1))obj=obj[p].bind(obj);}else obj=obj[parts[0]];const stimulus=scenario.stimulus_property==='empty'?'':(scenario.stimulus_property==='unicode'?'Ångström 東京 🚀':'{malformed');try{let value=obj(stimulus);if(value&&typeof value.then==='function')value=await value;result.observable='return';result.returnValue=value;result.passed=scenario.predicate==='ROUNDTRIP'&&value===stimulus;}catch(error){result.observable='exception';result.exceptionType=error&&error.constructor&&error.constructor.name||'Error';result.exceptionMessage=String(error&&error.message||error);result.passed=scenario.predicate==='REJECT';}}catch(error){result.probeError=String(error&&error.stack||error);}fs.writeFileSync(out,JSON.stringify(result));process.exitCode=result.passed?0:3;})();
'''
    return [
        node, "-e", source, "--", str(root.resolve()),
        json.dumps(target, separators=(",", ":"), ensure_ascii=False),
        json.dumps(scenario, separators=(",", ":"), ensure_ascii=False),
        str(output),
    ]


def _run_direct_acceptance_probe(
    config: LoadedConfig,
    current_root: Path,
    counter_root: Path,
    target: dict[str, Any],
    scenario: dict[str, Any],
) -> dict[str, Any] | None:
    """Require two current passes and one same-probe baseline failure."""

    if str(scenario.get("predicate") or "") not in {"REJECT", "ROUNDTRIP"}:
        return None
    suffix = Path(str(target.get("path") or "")).suffix.casefold()
    observations: list[dict[str, Any]] = []
    roots = [current_root, current_root, counter_root]
    for index, snapshot in enumerate(roots):
        output = snapshot / ".nexus-verification" / f"direct-probe-{index}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        snapshot_config = LoadedConfig(
            copy.deepcopy(config.data), snapshot.resolve(), list(config.sources),
            dict(config.provenance), copy.deepcopy(config.trusted_floor),
        )
        if suffix == ".py":
            command = _direct_python_probe_command(snapshot, target, scenario, output)
        elif suffix in {".js", ".cjs"}:
            command = _direct_js_probe_command(snapshot, target, scenario, output)
            if command is None:
                return None
        else:
            return None
        result = _contained_snapshot_command(
            snapshot_config, snapshot, command, denied_root=config.project_root,
        )
        if result.get("containment_unavailable"):
            return None
        # Node executes from a private snapshot-owned workspace below the
        # mapped drive root so its resolver never needs metadata authority on
        # the host drive.  Engine probe artifacts therefore live at the same
        # relative path inside that workspace; resolve that contained copy
        # without broadening any write authority.
        observed_output = output
        if not observed_output.is_file() and suffix in {".js", ".cjs"}:
            try:
                relative_output = output.resolve().relative_to(snapshot.resolve())
                candidate = (
                    snapshot / ".nexus-verification" / "node-workspace"
                    / relative_output
                )
                if candidate.is_file():
                    observed_output = candidate
            except (OSError, ValueError):
                pass
        try:
            payload = json.loads(observed_output.read_text(encoding="utf-8", errors="strict"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        payload["exit_code"] = result.get("exit_code")
        observations.append(payload)
    if not all(one.get("passed") is True for one in observations[:2]):
        return None
    if observations[2].get("passed") is True:
        return None
    evidence = {
        "schema_version": 1,
        "target": copy.deepcopy(target),
        "scenario": copy.deepcopy(scenario),
        "current_observations": observations[:2],
        "baseline_observation": observations[2],
        "adapter": "engine-direct-python-v1" if suffix == ".py" else "engine-direct-node-v1",
    }
    evidence["direct_probe_digest"] = hashlib.sha256(json.dumps(
        evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    return evidence


def _verification_commands(
    config: LoadedConfig,
    root: Path,
    project: dict[str, Any],
) -> tuple[list[list[str]], str]:
    explicit = project.get("test_commands")
    if isinstance(explicit, list) and explicit:
        commands = [list(one) for one in explicit if isinstance(one, list)]
        return commands, "selected_project"
    if config.project_root.resolve() == root.resolve():
        configured = config.get("project.test_commands", [])
        if isinstance(configured, list) and configured:
            return [list(one) for one in configured if isinstance(one, list)], "project_config"
    discovered = combined_commands(detect_project(root), "test")
    return discovered, "discovered"


def _command_approval_digest(
    root: Path,
    commands: list[list[str]],
    *,
    declared_path: str = "",
) -> str:
    """Bind approval to path, argv, and the project files that selected it.

    ``declared_path`` is included for user approvals so moving a board project
    to another path cannot carry authority even when both folders happen to
    contain byte-identical manifests. Internal execution receipts omit it and
    remain bound to the canonical snapshot root.
    """

    evidence: list[tuple[str, str | None]] = []
    for name in (
        "package.json", "pyproject.toml", "pytest.ini", "tox.ini", "go.mod",
        "Cargo.toml", "pom.xml", "gradlew", "gradlew.bat", "Gemfile",
        "CMakeLists.txt",
    ):
        path = root / name
        if path.is_file() and not path.is_symlink():
            evidence.append((name, file_sha256(path)))
    payload: dict[str, Any] = {
        "project_root": os.path.normcase(str(root.resolve())),
        "commands": commands,
        "evidence": evidence,
    }
    if declared_path:
        payload["declared_path"] = os.path.normcase(os.path.abspath(
            os.path.expanduser(str(declared_path))
        ))
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def verification_command_approval(
    config: LoadedConfig,
    project: dict[str, Any],
) -> dict[str, Any]:
    """Describe the exact non-executed test-command approval for one project.

    Discovery reads ordinary project metadata only. It never starts a runner;
    the returned argv is the material the person must review before the exact
    digest may be persisted on the board.
    """

    project_id = str(project.get("id") or "")
    declared_path = str(project.get("path") or "").strip()
    base = {
        "project_id": project_id,
        "project_path": declared_path,
        "canonical_path": "",
        "source": "unavailable",
        "commands": [],
        "approval_digest": "",
        "approved_digest": str(
            project.get("approved_test_command_digest") or ""
        ).lower(),
        "requires_approval": False,
        "approved": False,
        "stale_approval": False,
        "can_approve": False,
        "reason": "",
    }
    if not declared_path:
        return dict(base, reason="This project has no folder path to verify.")
    try:
        root = Path(declared_path).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        return dict(base, reason=f"The project folder cannot be resolved: {exc}")
    base["canonical_path"] = str(root)
    if not root.is_dir():
        return dict(base, reason="The project folder is not available on this machine.")
    try:
        commands, source = _verification_commands(config, root, project)
    except (OSError, HarnessError) as exc:
        return dict(base, reason=f"Test commands could not be discovered safely: {exc}")
    shown = [[str(part) for part in command] for command in commands]
    base["source"] = source
    base["commands"] = shown
    if source != "discovered":
        return dict(
            base,
            reason=(
                "These test commands were configured explicitly; discovered-command "
                "approval is not needed."
            ),
        )
    if not shown:
        return dict(
            base,
            reason="Nexus did not discover a deterministic test command for this project.",
        )
    try:
        digest = _command_approval_digest(
            root, shown, declared_path=declared_path,
        )
    except (OSError, RuntimeError) as exc:
        return dict(
            base,
            requires_approval=True,
            reason=f"The exact command fingerprint could not be read safely: {exc}",
        )
    approved_digest = str(base["approved_digest"] or "")
    approved = approved_digest == digest
    return dict(
        base,
        approval_digest=digest,
        requires_approval=True,
        approved=approved,
        stale_approval=bool(approved_digest and not approved),
        can_approve=True,
        reason=(
            "The exact discovered commands are approved for this project path."
            if approved else
            "Review the exact discovered commands before allowing Nexus to run them."
        ),
    )


_MUTATION_ACTIONS = {
    "fix": "fix", "fixing": "fix", "repair": "repair", "repairing": "repair",
    "ensure": "ensure", "resolve": "resolve", "resolving": "resolve",
    "migrate": "migrate", "migrating": "migrate", "refactor": "refactor",
    "refactoring": "refactor", "update": "update", "updating": "update",
    "modify": "modify", "modifying": "modify", "edit": "edit", "editing": "edit",
    "change": "change", "changing": "change", "write": "write", "writing": "write",
    "implement": "implement", "implementing": "implement", "create": "create",
    "creating": "create", "add": "add", "adding": "add", "remove": "remove",
    "removing": "remove", "delete": "delete", "deleting": "delete",
    "replace": "replace", "replacing": "replace", "rename": "rename",
    "renaming": "rename", "move": "move", "moving": "move", "upgrade": "upgrade",
    "copy": "copy", "copying": "copy",
    "support": "support", "supporting": "support", "handle": "handle", "handling": "handle",
    "prevent": "prevent", "preventing": "prevent", "make": "make", "making": "make",
    "correct": "correct", "correcting": "correct", "address": "address", "addressing": "address",
    "upgrading": "upgrade", "downgrade": "downgrade", "downgrading": "downgrade",
    "build": "build", "building": "build", "generate": "generate",
    "generating": "generate", "apply": "apply", "applying": "apply",
    "rework": "rework", "reworking": "rework", "correct": "correct",
    "correcting": "correct", "address": "address", "addressing": "address",
}
# Nominal action forms are only imperatives inside a positively scoped
# exception ("do not edit, except repairs to parser.py").  Build both number
# forms from semantic noun families so connector handling does not depend on a
# growing list of incident-shaped plural strings.
_MUTATION_ACTION_NOUN_BASES = {
    "fix": "fix", "repair": "repair", "edit": "edit", "change": "change",
    "update": "update", "modification": "modify", "migration": "migrate",
    "refactor": "refactor", "implementation": "implement", "addition": "add",
    "removal": "remove", "deletion": "delete", "replacement": "replace",
    "rename": "rename", "move": "move", "upgrade": "upgrade",
    "downgrade": "downgrade", "correction": "correct", "resolution": "resolve",
    "rework": "rework",
}


def _regular_noun_forms(noun: str) -> tuple[str, str]:
    if noun.endswith(("s", "x", "z", "ch", "sh")):
        return noun, noun + "es"
    if noun.endswith("y") and len(noun) > 1 and noun[-2] not in "aeiou":
        return noun, noun[:-1] + "ies"
    return noun, noun + "s"


_MUTATION_ACTION_NOUNS = {
    form: action
    for noun, action in _MUTATION_ACTION_NOUN_BASES.items()
    for form in _regular_noun_forms(noun)
}
_READ_ONLY_ACTIONS = {
    "review": "review", "reviewing": "review", "explain": "explain",
    "explaining": "explain", "analyze": "analyze", "analyse": "analyze",
    "analyzing": "analyze", "analysing": "analyze", "report": "report",
    "reporting": "report", "inspect": "inspect", "inspecting": "inspect",
    "audit": "audit", "auditing": "audit", "summarize": "summarize",
    "summarise": "summarize", "describe": "describe", "diagnose": "diagnose",
    "investigate": "investigate", "assess": "assess", "compare": "compare",
    "identify": "identify", "list": "list", "show": "show", "read": "read",
    "check": "check", "verify": "verify", "validate": "validate", "status": "status",
    "consider": "consider", "evaluate": "evaluate", "decide": "decide",
    "determine": "determine",
}
_EXCEPTION_CONNECTOR_WORDS = frozenset({
    # Coordinating contrasts, conditional exceptions, and restrictive
    # prepositions all reopen a negative scope for the action that follows.
    "except", "excepting", "excluding", "unless", "but", "yet", "still", "however",
    "nevertheless", "nonetheless", "besides", "bar", "barring",
})
_EXCEPTION_CONNECTOR_PAIRS = {
    "other": frozenset({"than"}),
    "save": frozenset({"for"}),
    "apart": frozenset({"from"}),
    "aside": frozenset({"from"}),
    "outside": frozenset({"of"}),
}
_POSITIVE_CONTRAST_CONNECTORS = frozenset({
    "but", "yet", "still", "however", "nevertheless", "nonetheless",
})
_GLOBAL_READ_ONLY = re.compile(
    r"\bread[- ]only\b"
    r"|\b(?:do not|don't|never)\s+(?:make|apply)\s+(?:any\s+)?(?:changes?|edits?)\b"
    r"|\b(?:do not|don't|never)\s+(?:change|modify|edit|write|touch|alter)\s+"
    r"(?:anything|any\s+files?|the\s+(?:project|repository|code|source))\b"
    r"|\bwithout\s+(?:making|applying)\s+(?:any\s+)?(?:changes?|edits?)\b"
    r"|\bwithout\s+(?:changing|modifying|editing|writing|touching|altering)\s+"
    r"(?:anything|any\s+files?|the\s+(?:project|repository|code|source))\b",
    re.IGNORECASE,
)
_GOAL_FILE = re.compile(
    # Compatibility token for compact paths. Contextual spaced, Unicode, and
    # extensionless names are handled by _goal_named_paths below.
    r"(?<![\w./\\:-])([\w.-]+(?:[\\/][\w.()-]+)*\.[^\W_][\w-]*)"
    r"(?![\w-]|\.[\w])",
    re.IGNORECASE,
)
_CONVENTIONAL_EXTENSIONLESS_FILES = frozenset({
    "makefile", "dockerfile", "jenkinsfile", "vagrantfile", "procfile",
    "gemfile", "rakefile", "brewfile", "license", "readme", "changelog",
    ".gitignore", ".editorconfig", ".env",
})
_PATH_ACTION = re.compile(
    r"\b(?:updat(?:e|ing|ed)|modif(?:y|ying|ied)|edit(?:ing|ed)?|fix(?:ing|ed)?|"
    r"repair(?:ing|ed)?|creat(?:e|ing|ed)|writ(?:e|ing|ten)|generat(?:e|ing|ed)|"
    r"build(?:ing|built)?|document(?:ing|ed)?|produc(?:e|ing|ed)|add(?:ing|ed)?|"
    r"delet(?:e|ing|ed)|renam(?:e|ing|ed)|mov(?:e|ing|ed)|copy(?:ing|ied)?|"
    r"replac(?:e|ing|ed)|resolv(?:e|ing|ed)|refactor(?:ing|ed)?|support(?:ing|ed)?|"
    r"handl(?:e|ing|ed)|prevent(?:ing|ed)?|make|making|correct(?:ing|ed)?|address(?:ing|ed)?|"
    r"use|using|consult(?:ing|ed)?|read(?:ing)?|review(?:ing|ed)?|"
    r"inspect(?:ing|ed)?|preserv(?:e|ing|ed)|keep(?:ing)?|leave|leaving|touch(?:ing|ed)?)\b"
    r"\s+((?:(?!\.\s+[A-Z])[^;\n!?])+)",
    re.IGNORECASE,
)
_PATH_WITH_EXTENSION = re.compile(
    r"[.\w][\w .()-]*(?:[\\/][.\w][\w .()-]*)*(?:\.[^\W_][\w-]*)+",
    re.IGNORECASE,
)


def _normalize_goal_path(raw: str) -> str:
    candidate = str(raw or "").strip().strip("\"'").strip()
    candidate = candidate.rstrip(".,;:!?)]}").lstrip("([{").strip()
    candidate = re.sub(r"^(?:the|a|an)\s+", "", candidate, flags=re.I)
    possessive = re.match(r"^(?:my|our|your)\s+(.+)$", candidate, re.I)
    if possessive and " " not in possessive.group(1):
        candidate = possessive.group(1)
    candidate = candidate.replace("\\", "/")
    if (
        not candidate
        or "://" in candidate
        or candidate.startswith("/")
        or re.match(r"^[A-Za-z]:", candidate)
        or any(part in {"", ".", ".."} for part in candidate.split("/"))
        or any(ord(char) < 32 for char in candidate)
    ):
        return ""
    basename = candidate.rsplit("/", 1)[-1]
    stem, separator, extension = basename.rpartition(".")
    has_extension = bool(stem and separator and re.fullmatch(r"[^\W_][\w-]*", extension, re.UNICODE))
    if not has_extension and basename.casefold() not in _CONVENTIONAL_EXTENSIONLESS_FILES:
        return ""
    return candidate


def _external_goal_path(raw: str) -> bool:
    value = str(raw or "").strip().strip("\"'")
    return bool(
        re.search(r"^[A-Za-z]:[\\/]", value)
        or re.search(r"^[\\/]{2}[^\\/]", value)
        or re.search(r"^[a-z][a-z0-9+.-]*://", value, re.I)
    )


def _unsafe_goal_path(raw: str) -> bool:
    value = str(raw or "").strip().strip("\"'")
    if _external_goal_path(value):
        return False
    return bool(
        re.search(r"(?:^|[\\/])\.\.(?:[\\/]|$)", value)
        or re.search(r"(?<=\S):(?=\S)", value)
        or re.search(r"(?<!:)[\\/]{2,}", value)
    )


def _validate_goal_path_syntax(goal: str) -> None:
    """Reject unsafe path-shaped tokens before semantic authority is compiled.

    Wrapping a path in quotes, backticks, or parentheses must not make it
    disappear and later fall back to a safe-looking basename.  External URLs,
    drive paths, and UNC references remain non-authoritative references rather
    than being reinterpreted as project-relative paths.
    """

    for token in re.findall(r"[^\s\"'`()\[\]{}]+", str(goal or "")):
        candidate = token.rstrip(".,;!?)]}")
        if candidate and _unsafe_goal_path(candidate):
            raise HarnessError(f"Unsafe explicit project path in goal: {candidate[:160]}")


def _goal_named_paths(goal: str) -> list[str]:
    """Extract only explicit project-relative filenames from goal prose.

    Spaced top-level paths need their mutation/create verb for a safe left
    boundary; compact paths may stand alone. Absolute paths and URLs are never
    converted into exact project-effect authority.
    """

    text = str(goal or "")
    _validate_goal_path_syntax(text)
    found: list[str] = []

    def remember(raw: str) -> None:
        normalized = _normalize_goal_path(raw)
        if not normalized:
            return
        key = normalized.casefold()
        for existing in found:
            if key == existing.casefold():
                return
        found.append(normalized)

    for quoted in re.finditer(r"[\"']([^\"'\r\n]+)[\"']", text):
        if _unsafe_goal_path(quoted.group(1)):
            raise HarnessError(f"Unsafe explicit project path in goal: {quoted.group(1)[:160]}")
        if _external_goal_path(quoted.group(1)):
            continue
        remember(quoted.group(1))

    for action in _PATH_ACTION.finditer(text):
        body = action.group(1).strip()
        if _unsafe_goal_path(body):
            raise HarnessError(f"Unsafe explicit project path in goal: {body[:160]}")
        if _external_goal_path(body) or body.startswith(("/", "\\")):
            continue
        raw_action_word = re.match(r"\w+", action.group(0)).group(0).casefold()
        action_word = (
            "replace" if raw_action_word.startswith("replac") else
            "rename" if raw_action_word.startswith("renam") else
            "move" if raw_action_word.startswith("mov") else
            "copy" if raw_action_word.startswith("cop") else raw_action_word
        )
        part_boundary = (
            r"\s+(?:without|using|from|based\s+on|according\s+to|informed\s+by|"
            r"in\s+accordance\s+with|after\s+(?:reviewing|reading|consulting)|"
            r"before\s+(?:updating|modifying|editing|fixing|repairing)|"
            r"while\s+(?:keeping|leaving|preserving)|but\s+(?:do\s+not|don't|never)\s+touch|"
            r"except|excluding)\s+"
            r"|\s+with\s+(?=[^,;.]{0,120}\.[^\W_][\w-]*[^.;]*(?:reference|read[- ]only))"
            r"|\s+(?:to|and)\s+(?=[^,;.]{0,120}(?:\.[^\W_][\w-]*|Makefile|Dockerfile|Jenkinsfile|\.env)\b)"
        )
        if action_word in {"rename", "move"}:
            part_boundary += (
                r"|\s+as\s+(?=[^,;.]{0,120}(?:\.[^\W_][\w-]*|"
                r"Makefile|Dockerfile|Jenkinsfile|\.env)\b)"
            )
        if action_word == "replace":
            # Replacement has two independently authoritative operands.  The
            # ordinary ``with`` rule is intentionally narrower so prose such
            # as "update a file with spaces" remains a single path.
            part_boundary += (
                r"|\s+with\s+(?=[^,;.]{0,120}(?:\.[^\W_][\w-]*|"
                r"Makefile|Dockerfile|Jenkinsfile|\.env)\b)"
            )
            part_boundary += (
                r"|\s+in\s+(?=[^,;.]{0,120}(?:\.[^\W_][\w-]*|"
                r"Makefile|Dockerfile|Jenkinsfile|\.env)\b)"
            )
        parts = re.split(part_boundary, body, flags=re.I)
        for part in parts:
            if action_word == "replace":
                part = re.sub(r"^\s*(?:the\s+)?contents?\s+of\s+", "", part, flags=re.I)
            extended = _PATH_WITH_EXTENSION.search(part)
            if not extended:
                first = re.match(r"(?:the\s+)?([\w.-]+)", part, re.I)
                if first:
                    remember(first.group(1))
                continue
            candidate = extended.group(0)
            # Possessive prose such as "only our own TEST-ci.yml" describes
            # authority; it is not part of the filename.  Keep ordinary
            # spaced names intact, stripping only the explicit ``own`` marker.
            candidate = re.sub(
                r"^.*\b(?:my|our|your)\s+own\s+(?=[^/\\]+\.[^\W_][\w-]*$)",
                "", candidate, flags=re.I,
            )
            # Content verbs and destination prepositions introduce the actual
            # filename in "document findings in notes.md". They must not make
            # the preceding prose part of the path.
            boundaries = list(re.finditer(
                r"\band\s+(?:(?:my|our|your)(?:\s+own)?|the|a|an)\s+"
                r"|\b(?:and\s+)?(?:create|write|document|include|describe|record|produce|"
                r"fix(?:ing)?|repair(?:ing)?|updat(?:e|ing)|edit(?:ing)?|modif(?:y|ying)|chang(?:e|ing)|"
                r"resolve|refactor|support|handle|prevent|make|correct|address|touch|"
                r"use|consult|read|review|preserve|keep|leave|rename|move|copy|replace)\b\s*"
                r"|\b(?:in|to|at|into|onto|named|called)\b\s*",
                candidate, re.I,
            ))
            required_for = re.search(r"\bis\s+required\s+for\s+", candidate, re.I)
            if required_for:
                suffix = candidate[required_for.end():].strip()
                if _normalize_goal_path(suffix):
                    candidate = suffix
            for boundary in reversed(boundaries):
                suffix = candidate[boundary.end():].strip()
                if _normalize_goal_path(suffix):
                    candidate = suffix
                    break
            remember(candidate)

    for compact in _GOAL_FILE.finditer(text):
        # Do not capture a tail of an absolute path or the host portion of a URL.
        prefix = text[max(0, compact.start() - 4):compact.start()]
        if "://" in prefix or (compact.start() and text[compact.start() - 1] in "/\\:"):
            continue
        compact_path = _normalize_goal_path(compact.group(1))
        if compact_path and any(
            existing.casefold().endswith("/" + compact_path.casefold())
            or existing.casefold().endswith(" " + compact_path.casefold())
            or existing.casefold().startswith(compact_path.casefold() + "/")
            for existing in found
        ):
            continue
        remember(compact.group(1))
    conventional = "|".join(
        re.escape(one) for one in sorted(_CONVENTIONAL_EXTENSIONLESS_FILES, key=len, reverse=True)
    )
    for special in re.finditer(
        rf"(?<![\w./\\:-])({conventional})(?![\w.-])", text, re.I,
    ):
        remember(special.group(1))
    return found


def _goal_path_roles(goal: str) -> dict[str, Any]:
    """Classify named files as requested effects or protected references."""

    text = str(goal or "")
    paths = _goal_named_paths(text)
    if _informational_goal(text):
        protected = list(dict.fromkeys(paths))
        return {
            "mentions": [{"path": relative, "role": "protected"} for relative in protected],
            "effects": [],
            "protected": protected,
        }
    roles: list[dict[str, str]] = []
    for relative in paths:
        variants = {relative, relative.replace("/", "\\")}
        mentions: list[str] = []
        for variant in variants:
            for match in re.finditer(re.escape(variant), text, re.I):
                before = text[:match.start()]
                boundaries = [one.end() for one in re.finditer(
                    r";|\n|[!?]\s*|\.\s+(?=[A-Z])", before,
                )]
                sentence_start = boundaries[-1] if boundaries else 0
                sentence = before[sentence_start:]
                after = text[match.end():]
                end_match = re.search(r";|\n|[!?]|\.\s+(?=[A-Z])", after)
                suffix = after[:end_match.start() if end_match else len(after)]
                sentence_full = sentence + relative + suffix
                connectors = []
                for connector in re.finditer(
                    r",|\b(?:and|but|except|excluding|without|using|with|from|"
                    r"based\s+on|according\s+to|informed\s+by|in\s+accordance\s+with|"
                    r"after\s+(?:reviewing|reading|consulting)|while\s+(?:keeping|leaving|preserving))\b",
                    sentence, re.I,
                ):
                    token = connector.group(0).casefold()
                    connectors.append(
                        connector.start()
                        if token not in {",", "and", "but"}
                        else connector.end()
                    )
                local = sentence[connectors[-1] if connectors else 0:]
                folded = local.casefold()
                required_after = bool(re.match(
                    r"\s*(?:(?:is|are|remains?|be)\s+)?(?:required|needed|necessary|mandatory|essential|requested)\b"
                    r"|\s*requires?\s+(?:a\s+)?(?:fix|repair|change|update|modification)\b",
                    suffix, re.I,
                ))
                action_match = re.search(
                    r"\b(updat(?:e|ing|ed)|fix(?:ing|ed)?|modif(?:y|ying|ied)|chang(?:e|ing|ed)|"
                    r"add(?:ing|ed)?|delet(?:e|ing|ed)|renam(?:e|ing|ed)|mov(?:e|ing|ed)|"
                    r"copy(?:ing|ied)?|replac(?:e|ing|ed)|creat(?:e|ing|ed)|writ(?:e|ing|ten)|"
                    r"generat(?:e|ing|ed)|build(?:ing|built)?)\b",
                    sentence_full, re.I,
                )
                action_raw = action_match.group(1).casefold() if action_match else ""
                action = (
                    "update" if action_raw.startswith("updat") else
                    "fix" if action_raw.startswith("fix") else
                    "modify" if action_raw.startswith("modif") else
                    "change" if action_raw.startswith("chang") else
                    "add" if action_raw.startswith("add") else
                    "delete" if action_raw.startswith("delet") else
                    "rename" if action_raw.startswith("renam") else
                    "move" if action_raw.startswith("mov") else
                    "copy" if action_raw.startswith("cop") else
                    "replace" if action_raw.startswith("replac") else
                    "create" if action_raw.startswith("creat") else
                    "write" if action_raw.startswith("writ") else
                    "generate" if action_raw.startswith("generat") else
                    "build" if action_raw.startswith("build") else action_raw
                )
                if required_after and re.search(
                    r"\b(?:however|nevertheless|nonetheless|yet|still|except|but)\b", sentence, re.I,
                ):
                    mentions.append("effect")
                    continue
                if action in {"rename", "move", "replace"}:
                    if action in {"rename", "move"}:
                        mentions.append("effect")
                        continue
                if action == "copy":
                    folded_sentence = sentence_full.casefold()
                    to_position = folded_sentence.find(" to ")
                    from_position = folded_sentence.find(" from ")
                    mention_position = len(sentence)
                    if from_position >= 0:
                        mentions.append("protected" if mention_position > from_position else "effect")
                    else:
                        mentions.append("effect" if to_position >= 0 and mention_position > to_position else "protected")
                    continue
                if action == "replace":
                    folded_sentence = sentence_full.casefold()
                    with_position = folded_sentence.find(" with ")
                    in_position = folded_sentence.find(" in ")
                    mention_position = len(sentence)
                    if in_position >= 0:
                        mentions.append("effect" if mention_position > in_position else "protected")
                    elif with_position >= 0:
                        mentions.append("protected" if mention_position > with_position else "effect")
                    else:
                        mentions.append("effect")
                    continue
                if re.match(
                    r"\s*(?:without|using|with|from|based\s+on|according\s+to|"
                    r"informed\s+by|in\s+accordance\s+with|after\s+(?:reviewing|reading|consulting)|"
                    r"except|excluding|while\s+(?:keeping|leaving|preserving))\b",
                    local, re.I,
                ):
                    mentions.append("protected")
                    continue
                if re.match(r"\s*not\b", local, re.I):
                    mentions.append("protected")
                    continue
                # Bind the path to the nearest governing verb in its clause.
                # This keeps a reference source protected in "use notes.md to
                # update parser.py", while the later mutation verb makes the
                # actual target writable.  It also handles the inverse suffix
                # form "fix parser.py and consult notes.md" without allowing
                # an earlier mutation verb to leak authority across clauses.
                governing = list(re.finditer(
                    r"\b(updat(?:e|ing|ed)|fix(?:ing|ed)?|modif(?:y|ying|ied)|chang(?:e|ing|ed)|"
                    r"add(?:ing|ed)?|delet(?:e|ing|ed)|renam(?:e|ing|ed)|mov(?:e|ing|ed)|"
                    r"copy(?:ing|ied)?|replac(?:e|ing|ed)|creat(?:e|ing|ed)|writ(?:e|ing|ten)|"
                    r"generat(?:e|ing|ed)|build(?:ing|built)?|use|using|consult(?:ing|ed)?|"
                    r"read(?:ing)?|review(?:ing|ed)?|inspect(?:ing|ed)?|preserv(?:e|ing|ed)|"
                    r"keep(?:ing)?|leave|leaving|touch(?:ing|ed)?)\b",
                    sentence, re.I,
                ))
                if governing:
                    nearest_match = governing[-1]
                    nearest_raw = nearest_match.group(1).casefold()
                    nearest = (
                        "update" if nearest_raw.startswith("updat") else
                        "fix" if nearest_raw.startswith("fix") else
                        "modify" if nearest_raw.startswith("modif") else
                        "change" if nearest_raw.startswith("chang") else
                        "add" if nearest_raw.startswith("add") else
                        "delete" if nearest_raw.startswith("delet") else
                        "rename" if nearest_raw.startswith("renam") else
                        "move" if nearest_raw.startswith("mov") else
                        "copy" if nearest_raw.startswith("cop") else
                        "replace" if nearest_raw.startswith("replac") else
                        "create" if nearest_raw.startswith("creat") else
                        "write" if nearest_raw.startswith("writ") else
                        "generate" if nearest_raw.startswith("generat") else
                        "build" if nearest_raw.startswith("build") else
                        "consult" if nearest_raw.startswith("consult") else
                        "read" if nearest_raw.startswith("read") else
                        "review" if nearest_raw.startswith("review") else
                        "inspect" if nearest_raw.startswith("inspect") else
                        "preserve" if nearest_raw.startswith("preserv") else
                        "keep" if nearest_raw.startswith("keep") else
                        "leave" if nearest_raw.startswith("leav") else
                        "touch" if nearest_raw.startswith("touch") else nearest_raw
                    )
                    before_governing = sentence[max(0, nearest_match.start() - 32):nearest_match.start()]
                    if re.search(r"\b(?:do\s+not|don't|never)\s*$", before_governing, re.I):
                        mentions.append("protected")
                        continue
                    if nearest in {"use", "using", "consult", "read", "review", "inspect", "preserve", "keep", "leave", "touch"}:
                        mentions.append("protected")
                        continue
                    if nearest in {
                        "update", "fix", "modify", "change", "add", "delete",
                        "rename", "move", "replace", "create", "write", "generate", "build",
                    }:
                        mentions.append("effect")
                        continue
                if _requested_action_goal(text) and re.match(
                    r"\s*(?:(?:needs?\s+)|(?:(?:should|must)\s+be\s+))?(?:to\s+be\s+)?(?:updated|fixed|modified|changed|"
                    r"edited|repaired|created|written|deleted|renamed|moved|copied|replaced|"
                    r"updating|fixing|modifying|changing|editing|repairing|creating|writing|"
                    r"deleting|renaming|moving|copying|replacing)\b",
                    suffix, re.I,
                ):
                    mentions.append("effect")
                    continue
                preserved = bool(
                    re.search(
                        r"\b(?:read[- ]only|reference|existing|using|use|review|inspect|consult|read|"
                        r"preserve|keep|leave|untouched|unchanged|from|based\s+on|according\s+to|"
                        r"informed\s+by|in\s+accordance\s+with|after\s+(?:reviewing|reading|consulting)|"
                        r"except|excluding)\b",
                        folded,
                    )
                    or re.search(r"\bnot\s*$", folded)
                    or re.match(
                        r"\s*(?:unchanged|untouched|read[- ]only|"
                        r"only\s+as\s+(?:a\s+)?(?:read[- ]only\s+)?reference|"
                        r"as\s+(?:a\s+)?(?:read[- ]only\s+)?reference)\b",
                        suffix, re.I,
                    )
                )
                if preserved:
                    mentions.append("protected")
                    continue
                local_intent = _goal_intent(local)
                if local_intent == "mutation":
                    mentions.append("effect")
                elif local_intent == "read_only":
                    mentions.append("protected")
                else:
                    sentence_intent = _goal_intent(sentence)
                    if sentence_intent == "mutation":
                        mentions.append("effect")
                    else:
                        mentions.append("neutral")
        role = "protected" if "protected" in mentions else (
            "effect" if "effect" in mentions else "protected"
        )
        roles.append({"path": relative, "role": role})
    return {
        "mentions": roles,
        "effects": [one["path"] for one in roles if one["role"] == "effect"],
        "protected": [one["path"] for one in roles if one["role"] == "protected"],
    }


def _fragment_path(fragment: str, *, last: bool = False) -> str:
    """Return one exact operand from an already frame-bounded phrase."""

    bounded = re.sub(
        r"^\s*(?:the\s+)?(?:file\s+)?", "", str(fragment or "").strip(), flags=re.I,
    ).strip(" \t\r\n\"'`()[]{}.,;:!?")
    direct = _normalize_goal_path(bounded)
    if direct:
        return direct
    values = _goal_named_paths(bounded)
    return values[-1 if last else 0] if values else ""


def _fragment_directory(fragment: str) -> str:
    """Return an explicitly written project-relative destination directory."""

    value = str(fragment or "").strip().strip("\"'`()[]{}.,;:!?").replace("\\", "/")
    value = re.sub(r"^(?:the\s+)?(?:folder|directory)\s+", "", value, flags=re.I).strip()
    value = value.rstrip("/")
    if (
        not value or _external_goal_path(value) or _unsafe_goal_path(value)
        or value.startswith("/") or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        return ""
    return value


def _goal_operations(goal: str) -> list[dict[str, Any]]:
    """Compile directional file operations without collapsing operand roles.

    The existing mention scanner remains useful for prose and destination-root
    discovery.  Transfer semantics, however, need frame-local operands: a copy
    source is protected while a move source is deleted, and neither may be
    inferred from a global list of filenames.
    """

    text = str(goal or "")
    operations: list[dict[str, Any]] = []
    occupied: set[tuple[str, ...]] = set()

    def add(kind: str, **operands: str) -> None:
        normalized = {
            key: value.replace("\\", "/").strip(" /")
            for key, value in operands.items() if value
        }
        signature = (kind, *[f"{key}={normalized[key].casefold()}" for key in sorted(normalized)])
        if not normalized or signature in occupied:
            return
        occupied.add(signature)
        identifier = "operation_" + hashlib.sha256(
            "|".join(signature).encode("utf-8")
        ).hexdigest()[:12]
        operations.append({"id": identifier, "kind": kind, **normalized})

    clauses: list[str] = []
    for sentence in re.split(r";|\n|[!?](?=\s|$)|\.\s+(?=[A-Z])", text):
        if not sentence.strip():
            continue
        # A coordinated new verb opens a new frame; a coordinated path noun
        # inherits the current frame and is handled below.
        clauses.extend(
            one.strip().rstrip(".!?") for one in re.split(
                r"\s+(?:and|then|and\s+then)\s+(?=(?:update|fix|repair|modify|edit|change|create|"
                r"delete|remove|move|rename|copy|replace|preserve|keep|leave)\b)",
                sentence, flags=re.I,
            ) if one.strip()
        )
    for clause in clauses:
        scoped_exception = re.match(
            r"\s*(?:no\s+changes?|do\s+not\s+change\s+anything|never\s+change\s+anything)\s+"
            r"(?:except|other\s+than|save\s+for)\s+(.+)$",
            clause, re.I,
        )
        if scoped_exception:
            clause = scoped_exception.group(1).strip()

        # Prefix preservation is an independent constraint followed by a
        # positive operation, not a blanket read-only speech act.
        preserve_prefix = re.match(
            r"\s*(?:preserve|keep|leave|with)\s+(.+?)\s+(?:kept\s+)?"
            r"(?:unchanged|untouched|read[- ]only)\s+(?:while|and|but)\s+(.+)$",
            clause, re.I,
        )
        if preserve_prefix:
            protected = _fragment_path(preserve_prefix.group(1))
            if protected:
                add("preserve", target=protected)
            clause = preserve_prefix.group(2).strip()

        preserve_suffix = re.search(
            r"\s+(?:without\s+(?:changing|modifying|editing|touching)|"
            r"while\s+(?:preserving|keeping|leaving))\s+(.+)$",
            clause, re.I,
        )
        if preserve_suffix:
            for protected in _goal_named_paths(preserve_suffix.group(1)):
                add("preserve", target=protected)
            clause = clause[:preserve_suffix.start()].strip()

        # Coordinated frames inherit the operation, not one enormous path.
        # Examples: ``Move a.md to x/a.md and b.md to x/b.md`` and
        # ``Delete old.md and create dest.md``.
        coordinated_transfer = re.match(r"\s*(move|rename|copy)\s+(.+)$", clause, re.I)
        if coordinated_transfer:
            kind = coordinated_transfer.group(1).casefold()
            body = coordinated_transfer.group(2)
            pair_pattern = re.compile(
                r"(?:^|\s+(?:and|then|and\s+then)\s+|\s*,\s*)"
                r"(.+?)\s+(?:to|into|as)\s+(.+?)"
                r"(?=(?:\s+(?:and|then|and\s+then)\s+|\s*,\s*).+?\s+(?:to|into|as)\s+|$)",
                re.I,
            )
            pairs = list(pair_pattern.finditer(body))
            if len(pairs) > 1:
                for pair in pairs:
                    add(
                        kind,
                        source=_fragment_path(pair.group(1)),
                        destination=_fragment_path(pair.group(2)),
                    )
                continue
            shared_destination = re.match(
                r"(.+?)\s+(?:to|into|as)\s+(.+[/\\])\s*$", body, re.I,
            )
            if shared_destination:
                sources = _goal_named_paths(shared_destination.group(1))
                directory = _fragment_directory(shared_destination.group(2))
                if len(sources) > 1 and directory:
                    for source in sources:
                        add(
                            kind,
                            source=source,
                            destination=f"{directory}/{Path(source).name}",
                        )
                    continue
        delete_create = re.match(
            r"\s*(?:delete|remove)\s+(.+?)\s+(?:and|,?\s*then)\s+create\s+(.+)$",
            clause, re.I,
        )
        if delete_create:
            for relative in _goal_named_paths(delete_create.group(1)):
                add("delete", target=relative)
            for relative in _goal_named_paths(delete_create.group(2)):
                add("create", target=relative)
            continue

        move_from = re.match(
            r"\s*move\s+(?:into|to)\s+(.+?)\s+from\s+(.+)$", clause, re.I,
        )
        if move_from:
            add("move", source=_fragment_path(move_from.group(2)), destination=_fragment_path(move_from.group(1)))
            continue
        transfer = re.match(
            r"\s*(move|rename|copy)\s+(.+?)\s+(?:to|into|as)\s+(.+)$", clause, re.I,
        )
        if transfer:
            add(
                transfer.group(1).casefold(),
                source=_fragment_path(transfer.group(2)),
                destination=_fragment_path(transfer.group(3)),
            )
            continue
        copy_from = re.match(r"\s*copy\s+(.+?)\s+from\s+(.+)$", clause, re.I)
        if copy_from:
            add("copy", source=_fragment_path(copy_from.group(2)), destination=_fragment_path(copy_from.group(1)))
            continue
        replace_with = re.match(
            r"\s*replace\s+(?:the\s+)?(?:contents?\s+of\s+)?(.+?)\s+(?:with|using)\s+(.+)$",
            clause, re.I,
        )
        if replace_with:
            add("replace", target=_fragment_path(replace_with.group(1)), source=_fragment_path(replace_with.group(2)))
            continue
        replace_in = re.match(r"\s*replace\s+(.+?)\s+in\s+(.+)$", clause, re.I)
        if replace_in:
            add("replace", target=_fragment_path(replace_in.group(2)), source=_fragment_path(replace_in.group(1)))
            continue
        ordinary = re.match(
            r"\s*(update|fix|repair|modify|edit|change|create|delete|remove|preserve|keep|leave)\s+(.+)$",
            clause, re.I,
        )
        if ordinary:
            raw_kind = ordinary.group(1).casefold()
            kind = (
                "create" if raw_kind == "create"
                else "delete" if raw_kind in {"delete", "remove"}
                else "preserve" if raw_kind in {"preserve", "keep", "leave"}
                else "modify"
            )
            local_roles = _goal_path_roles(clause)
            operands = (
                local_roles["protected"]
                if kind == "preserve" else local_roles["effects"]
            )
            for relative in operands:
                add(kind, target=relative)

    return operations


def _operation_write_grants(operations: list[dict[str, Any]]) -> dict[str, set[str]]:
    grants: dict[str, set[str]] = {}

    def grant(path: str, capability: str) -> None:
        if path:
            grants.setdefault(path.casefold(), set()).add(capability)

    for operation in operations:
        kind = str(operation.get("kind") or "")
        if kind in {"modify", "replace"}:
            grant(str(operation.get("target") or ""), "MODIFY")
        elif kind == "create":
            grant(str(operation.get("target") or ""), "CREATE_OR_MODIFY")
        elif kind == "delete":
            grant(str(operation.get("target") or ""), "DELETE")
        elif kind in {"move", "rename"}:
            grant(str(operation.get("source") or ""), "DELETE")
            grant(str(operation.get("destination") or ""), "CREATE_OR_MODIFY")
        elif kind == "copy":
            grant(str(operation.get("destination") or ""), "CREATE_OR_MODIFY")
    return grants


def _mask_goal_files(goal: str) -> str:
    masked = str(goal or "")
    for relative in sorted(_goal_named_paths(masked), key=len, reverse=True):
        variants = {relative, relative.replace("/", "\\")}
        for variant in variants:
            masked = re.sub(re.escape(variant), " PROJECT_FILE ", masked, flags=re.I)
    return masked


def _exception_connector(tokens: list[str], index: int) -> str:
    """Return one normalized restrictive/contrast connector at this token."""

    token = tokens[index]
    following = tokens[index + 1] if index + 1 < len(tokens) else ""
    if token in _EXCEPTION_CONNECTOR_WORDS:
        return token
    if following in _EXCEPTION_CONNECTOR_PAIRS.get(token, ()):
        return f"{token} {following}"
    if token == "with":
        # Normalize "with [the/sole] exception of/to" as the same semantic
        # connector without enumerating each surface phrase independently.
        cursor = index + 1
        while cursor < len(tokens) and tokens[cursor] in {"the", "a", "an", "sole", "single"}:
            cursor += 1
        if cursor < len(tokens) and tokens[cursor] == "exception":
            cursor += 1
            if cursor < len(tokens) and tokens[cursor] in {"of", "to", "for"}:
                return "with exception"
    return ""


def _requested_action_goal(goal: str) -> bool:
    """Recognize requests/desiderata separately from capability questions."""

    text = str(goal or "").strip()
    if _pure_prohibition_goal(text):
        return False
    if re.match(r"^should\b.*\?\s*$", text, re.I):
        return False
    if re.match(
        r"^(?:can\s+(?:the|this|a)|does\b|do\s+you\s+think\b|how\s+(?:do|does|should)|should\s+i|"
        r"what\s+would|is\s+it\s+necessary|do\s+we\s+need|(?:please\s+)?tell\s+me\s+(?:if|whether)|"
        r"explain|i\s+want\s+to\s+know|check\s+whether|decide\s+(?:if|whether))\b",
        text, re.I,
    ):
        return False
    base = (
        r"(?:update|fix|repair|modify|edit|change|implement|create|add|delete|"
        r"rename|move|copy|replace|build|generate|write|refactor)"
    )
    gerund = (
        r"(?:updating|fixing|repairing|modifying|editing|changing|implementing|creating|"
        r"adding|deleting|renaming|moving|copying|replacing|building|generating|writing|refactoring)"
    )
    participle = (
        r"(?:updated|fixed|repaired|modified|edited|changed|implemented|created|"
        r"added|deleted|renamed|moved|copied|replaced|built|generated|written|refactored)"
    )
    return bool(
        re.match(
            rf"^(?:can|could|would|will)\s+(?:you|we)\s+(?:(?:please|kindly)\s+)?"
            rf"(?:(?:be\s+able\s+to|help\s+(?:me|us)\s+to?)\s+)?{base}\b",
            text, re.I,
        )
        or re.match(rf"^would\s+you\s+mind\s+{gerund}\b", text, re.I)
        or re.match(rf"^(?:can|could|would)\s+PROJECT_FILE\s+be\s+{participle}\b", _mask_goal_files(text), re.I)
        or re.match(rf"^would\s+it\s+be\s+possible\s+for\s+you\s+to\s+{base}\b", text, re.I)
        or re.match(rf"^could\s+i\s+get\s+PROJECT_FILE\s+{participle}\b", _mask_goal_files(text), re.I)
        or re.match(rf"^i\s+was\s+hoping\s+PROJECT_FILE\s+could\s+be\s+{participle}\b", _mask_goal_files(text), re.I)
        or re.search(rf"\b(?:i\s+(?:need|want|would\s+like)|please\s+have)\b[^;!?]*\b{participle}\b", text, re.I)
        or re.search(rf"\b[^.;!?]+\s+(?:needs?\s+(?:{gerund}|to\s+be\s+{participle})|requires?\s+(?:an?\s+)?(?:update|fix|repair|change|modification)|(?:should|must)\s+be\s+{participle})\b", text, re.I)
        or re.search(rf"\bit\s+would\s+be\s+(?:great|helpful|good)\s+if\s+you\s+(?:{base}|{participle})\b", text, re.I)
        or re.search(rf"\bPROJECT_FILE\s+ought\s+to\s+be\s+{participle}\b", _mask_goal_files(text), re.I)
        or re.search(rf"\bit\s+is\s+requested\s+that\s+PROJECT_FILE\s+(?:be|is)\s+{participle}\b", _mask_goal_files(text), re.I)
        or re.search(rf"\bi\s+would\s+appreciate\s+(?:it\s+if\s+you\s+{base}|(?:you\s+)?{gerund})\b", text, re.I)
        or re.search(rf"\bsee\s+to\s+it\s+that\s+PROJECT_FILE\s+(?:is|gets?)\s+{participle}\b", _mask_goal_files(text), re.I)
        or re.search(rf"\bmake\s+sure\s+.+?\s+(?:is|gets?)\s+{participle}\b", text, re.I)
        or re.search(rf"\bi\s+would\s+appreciate\s+PROJECT_FILE\s+being\s+{participle}\b", _mask_goal_files(text), re.I)
        or re.search(rf"\bmay\s+i\s+have\s+PROJECT_FILE\s+{participle}\b", _mask_goal_files(text), re.I)
        or re.search(rf"\bi\s+request\s+that\s+PROJECT_FILE\s+be\s+{participle}\b", _mask_goal_files(text), re.I)
        or re.search(rf"\bPROJECT_FILE\s+has\s+to\s+be\s+{participle}\b", _mask_goal_files(text), re.I)
        or re.search(r"\bPROJECT_FILE\s+is\s+due\s+for\s+an?\s+(?:update|fix|repair|change)\b", _mask_goal_files(text), re.I)
        or re.search(rf"\bplease\s+arrange\s+for\s+PROJECT_FILE\s+to\s+be\s+{participle}\b", _mask_goal_files(text), re.I)
        or re.search(rf"^(?:please\s+)?(?:have|let)\s+PROJECT_FILE\s+(?:be\s+)?{participle}\b", _mask_goal_files(text), re.I)
        or re.search(r"\ban?\s+(?:update|fix|repair|change)\s+is\s+required\s+for\s+PROJECT_FILE\b", _mask_goal_files(text), re.I)
    )


def _pure_prohibition_goal(goal: str) -> bool:
    """Recognize project-wide negative authority without inventing an action.

    Positive contrast exceptions (``however parser.py must be repaired``)
    remain actionable and are handled by the clause parser.
    """

    text = str(goal or "").strip()
    action = (
        r"(?:update|fix|repair|modify|edit|change|implement|create|add|delete|"
        r"rename|move|copy|replace|build|generate|write|refactor)\w*"
    )
    positive_exception = re.search(
        rf"\b(?:however|nevertheless|nonetheless|yet|still)\b[^.;!?]*"
        rf"(?:{action}|\b(?:repairs?|fix(?:es)?|updates?|changes?)\b[^.;!?]*"
        r"\b(?:required|essential|necessary|needed|mandatory)\b)",
        text, re.I,
    )
    if positive_exception:
        return False
    if re.search(
        r"\bPROJECT_FILE\s+does\s+not\s+need\s+to\s+be\s+"
        r"(?:updated|fixed|repaired|modified|edited|changed)\b",
        _mask_goal_files(text), re.I,
    ):
        return True
    found = False
    for clause in [one.strip() for one in re.split(r"[;.!?]+", text) if one.strip()]:
        has_action = bool(re.search(rf"\b{action}\b", clause, re.I))
        if not has_action:
            continue
        prohibited = bool(
            re.search(
                rf"\b(?:must|should|may|shall|is|are)\s+not\s+"
                rf"(?:be\s+|to\s+(?:be\s+)?)?{action}\b",
                clause, re.I,
            )
            or re.search(
                rf"\bunder\s+no\s+circumstances\b[^.;!?]*\b{action}\b",
                clause, re.I,
            )
        )
        if not prohibited:
            return False
        found = True
    return found


def _informational_goal(goal: str) -> bool:
    text = str(goal or "").strip()
    if _pure_prohibition_goal(text):
        return True
    if _requested_action_goal(text):
        return False
    if re.match(r"^should\b.*\?\s*$", text, re.I):
        return True
    if not re.match(
        r"^(?:can|does|do\s+you\s+think|do\s+we\s+need|how|should(?:\s+i|\s+[^.;!?]+\?)|what\s+would|is\s+it\s+necessary|"
        r"could|would|why|explain|please\s+tell\s+me\s+(?:whether|if)|"
        r"tell\s+me\s+whether|tell\s+me\s+if|describe|i\s+want\s+to\s+know|"
        r"check\s+whether|decide\s+(?:if|whether))\b",
        text, re.I,
    ):
        return False
    # A later, separately punctuated imperative remains actionable.
    return not bool(re.search(
        r"[.;!]\s*(?:please\s+)?(?:fix|repair|update|modify|change|implement|create|"
        r"add|delete|rename|move|copy|replace|build|generate|write)\b",
        text, re.I,
    ))


def _goal_intent(goal: str) -> str:
    return str(_parse_goal_intent(goal)["intent"])


def _parse_goal_intent(goal: str) -> dict[str, Any]:
    """Parse affirmative imperatives, negative constraints, and exceptions.

    Action nouns ("the fix", "whether the repair") are not imperatives. An
    exception reopens a negative constraint, so "do not modify except to fix"
    contains a positive mutation even though the outer clause is prohibitive.
    """

    text = str(goal or "").strip()
    if _pure_prohibition_goal(text):
        return {
            "intent": "read_only", "mutation_actions": [],
            "read_only_actions": ["preserve"],
            "constraints": ["project_wide_prohibition"],
            "exceptions": [], "deliberative_mentions": [],
        }
    masked = _mask_goal_files(text).casefold()
    tokens = re.findall(r"[a-z]+(?:'[a-z]+)?|[;,.!?:]", masked)
    mutation_actions: list[str] = []
    read_only_actions: list[str] = []
    constraints: list[str] = []
    exceptions: list[str] = []
    negative = False
    exception = False
    clause_lead = True
    previous = ""
    action_seen = False
    clause_read_only = False
    deliberative = False
    deliberative_mentions: list[str] = []
    prior_prohibition = False
    exception_bridge = False
    active_connector = ""
    lead_words = {
        "please", "kindly", "can", "could", "would", "will", "you", "i", "we",
        "want", "need", "must", "should", "to", "just", "then", "also", "goal",
        "task",
    }
    noun_markers = {"the", "a", "an", "this", "that", "these", "those", "of", "whether", "which", "whose"}
    punctuation = {";", ".", ",", "!", "?", ":"}
    for index, token in enumerate(tokens):
        if token in punctuation:
            if exception_bridge and token == ",":
                # "However, repairs ..." is one discourse connector even
                # though punctuation separates it from its positive exception.
                clause_lead = True
                previous = token
                continue
            if negative:
                prior_prohibition = True
            negative = False
            exception = False
            exception_bridge = False
            active_connector = ""
            deliberative = False
            clause_read_only = False
            clause_lead = True
            previous = token
            continue
        following = tokens[index + 1] if index + 1 < len(tokens) else ""
        connector = _exception_connector(tokens, index)
        if connector:
            # Restrictive prepositions inherit polarity.  They reopen an
            # existing prohibition ("no edits except repairs"), but introduce
            # an excluded/negative complement in an affirmative review
            # ("review, excluding modifying files").  Coordinating contrasts
            # introduce a new affirmative clause even without an outer
            # prohibition.
            positive_exception = negative or (clause_lead and prior_prohibition)
            if positive_exception:
                negative = False
                exception = True
            elif connector in _POSITIVE_CONTRAST_CONNECTORS:
                # A contrast in ordinary affirmative/review discourse starts
                # a neutral clause. A following explicit verb can still be an
                # imperative, but a noun such as "repairs" is not promoted.
                negative = False
                exception = False
            else:
                negative = True
                exception = False
            exception_bridge = positive_exception
            active_connector = connector
            prior_prohibition = False
            deliberative = False
            clause_lead = True
            exceptions.append(connector)
            previous = token
            continue
        starts_negative = (
            token in {"never", "avoid", "without", "untouched", "unchanged"}
            or (token in {"do", "does"} and following == "not")
            or token in {"don't", "doesn't"}
            or (token == "no" and following in {"change", "changes", "edit", "edits", "modification", "modifications"})
        )
        if starts_negative:
            negative = True
            prior_prohibition = True
            exception = False
            exception_bridge = False
            active_connector = ""
            constraints.append(token)
            clause_lead = False
            previous = token
            continue
        if token == "not" and previous in {"do", "does"}:
            previous = token
            continue
        if token == "whether" or (token == "if" and clause_read_only):
            deliberative = True
            previous = token
            continue
        nominal_action = _MUTATION_ACTION_NOUNS.get(token) if exception and not negative else None
        if nominal_action and active_connector in {"however", "nevertheless", "nonetheless"}:
            obligation = {
                "required", "needed", "necessary", "mandatory", "requested",
                "must", "shall", "essential",
            }
            ahead = {one for one in tokens[index + 1:index + 9] if one not in punctuation}
            if not (ahead & obligation):
                nominal_action = None
        action = _MUTATION_ACTIONS.get(token) or nominal_action or _READ_ONLY_ACTIONS.get(token)
        if action is not None:
            exception_bridge = False
            if token in {"update", "updating"} and following in {"me", "us"} and not negative:
                action_seen = True
                clause_lead = False
                read_only_actions.append("report")
                previous = token
                continue
            noun_reference = previous in noun_markers and not exception and action != "status"
            connected = previous in {"and", "then", "also", "to", "by", "while", "please", "kindly"}
            imperative = not noun_reference and not negative and (
                exception or clause_lead or connected or not action_seen
            )
            if imperative:
                if not exception:
                    prior_prohibition = False
                action_seen = True
                clause_lead = False
                if token in _MUTATION_ACTIONS or nominal_action is not None:
                    if deliberative and not exception:
                        deliberative_mentions.append(action)
                    else:
                        mutation_actions.append(action)
                else:
                    read_only_actions.append(action)
                    clause_read_only = True
            previous = token
            continue
        if token not in lead_words and token not in {"and", "or", "by"}:
            exception_bridge = False
            if not negative:
                prior_prohibition = False
            clause_lead = False
        previous = token
    if _requested_action_goal(text):
        if not mutation_actions:
            mutation_actions.append("requested_outcome")
        intent = "mutation"
    elif _informational_goal(text):
        mutation_actions = []
        intent = "read_only"
        if not read_only_actions:
            read_only_actions.append("explain")
    elif mutation_actions:
        intent = "mutation"
    elif read_only_actions or constraints or _GLOBAL_READ_ONLY.search(text):
        intent = "read_only"
    else:
        intent = "project_work"
    return {
        "intent": intent,
        "mutation_actions": mutation_actions,
        "read_only_actions": read_only_actions,
        "constraints": constraints,
        "exceptions": exceptions,
        "deliberative_mentions": deliberative_mentions,
    }


def _goal_effect_evidence(
    root: Path,
    goal: str,
    changed: list[str],
    required_effect_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Mechanically reject provider consensus that produced no requested effect."""

    intent = _goal_intent(goal)
    effect_required = intent != "read_only"
    normalized_changed = {path.replace("\\", "/").casefold() for path in changed}
    path_roles = _goal_path_roles(goal)
    if intent == "read_only" and changed:
        return {
            "passed": False,
            "effect_required": False,
            "intent": intent,
            "evidence": [],
            "reason": (
                "The goal is informational/read-only, so project-wide zero-write authority applies; "
                "this run reports project-file changes: " + ", ".join(changed)
            ),
        }
    named = list(dict.fromkeys(
        match.replace("\\", "/").strip("/") for match in path_roles["effects"]
    ))
    for raw in required_effect_paths or []:
        relative = str(raw or "").replace("\\", "/").strip().strip("/")
        if not relative:
            continue
        if any(_paths_overlap(relative, protected) for protected in path_roles["protected"]):
            raise HarnessError(
                f"Planned effect path {relative} conflicts with protected/read-only goal path"
            )
        try:
            confined_path(root, relative)
        except HarnessError:
            continue
        if relative not in named:
            named.append(relative)
    evidence: list[dict[str, Any]] = []
    unmet: list[str] = []
    for relative in named:
        try:
            path = confined_path(root, relative, allow_missing=True)
        except HarnessError:
            unmet.append(relative)
            continue
        digest = file_sha256(path)
        changed_here = relative.casefold() in normalized_changed
        evidence.append({
            "requirement": f"project-work effect {relative}",
            "path": relative,
            "sha256": digest,
            "changed_in_session": changed_here,
        })
        if effect_required and not changed_here:
            unmet.append(relative)
        elif not effect_required and digest is None:
            unmet.append(relative)
    if effect_required and not changed:
        return {
            "passed": False, "effect_required": True, "intent": intent, "evidence": evidence,
            "reason": (
                "Project-work completion requires relevant changed state unless the user explicitly marks the goal read-only; "
                "this session produced no project-file effect."
            ),
        }
    if unmet:
        return {
            "passed": False, "effect_required": effect_required, "intent": intent, "evidence": evidence,
            "reason": "Required project effects were not produced in this session: " + ", ".join(unmet),
        }
    return {
        "passed": True, "effect_required": effect_required, "intent": intent, "evidence": evidence,
        "reason": "Required project effect evidence is present." if effect_required else "The explicitly read-only goal required no project mutation.",
    }


def _project_tree_manifest(root: Path) -> dict[str, str]:
    """Content-address the user project surface, excluding engine/Git control state."""

    manifest: dict[str, str] = {}
    skipped = {".git", ".harness"}
    for folder, directories, files in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if name not in skipped]
        base = Path(folder)
        for name in sorted(files):
            path = base / name
            try:
                relative = path.relative_to(root).as_posix()
                if path.is_symlink():
                    manifest[relative] = "symlink:" + os.readlink(path)
                elif path.is_file():
                    digest = file_sha256(path)
                    manifest[relative] = "file:" + str(digest or "missing")
            except (OSError, ValueError, HarnessError):
                # An unreadable/unstable user file makes a zero-write proof
                # impossible rather than being silently omitted.
                manifest[relative if 'relative' in locals() else str(path)] = "unreadable"
    return manifest


def _project_tree_merkle(root: Path) -> tuple[str, dict[str, str]]:
    manifest = _project_tree_manifest(root)
    digest = hashlib.sha256(json.dumps(
        sorted(manifest.items()), separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    return digest, manifest


def _compile_goal_spec(root: Path, goal: str) -> dict[str, Any]:
    """Compile immutable semantic authority before provider planning begins."""

    _validate_goal_path_syntax(goal)
    intent = _goal_intent(goal)
    roles = _goal_path_roles(goal)
    make_sure = re.search(
        r"\bmake\s+sure\s+(.+?)\s+(?:is|gets?)\s+(?:updated|fixed|repaired|"
        r"modified|edited|changed|created|deleted|renamed|moved|copied|replaced)\b",
        goal, re.I,
    )
    if make_sure and _requested_action_goal(goal):
        requested_paths = _goal_named_paths(make_sure.group(1))
        if requested_paths:
            roles = {
                "mentions": [{"path": one, "role": "effect"} for one in requested_paths],
                "effects": requested_paths, "protected": [],
            }
    operations = _goal_operations(goal)
    if _requested_action_goal(goal) and not roles["effects"] and not operations:
        requested_paths = _goal_named_paths(goal)
        if requested_paths:
            roles = {
                "mentions": [{"path": one, "role": "effect"} for one in requested_paths],
                "effects": requested_paths,
                "protected": [],
            }
    claimed = {
        str(operation.get(field) or "").casefold()
        for operation in operations for field in ("target", "source", "destination")
        if operation.get(field)
    }
    leading_action = re.search(
        r"\b(create|generate|build|write|add|delete|remove|update|fix|repair|modify|edit|change)\b",
        _mask_goal_files(goal), re.I,
    )
    raw_action = leading_action.group(1).casefold() if leading_action else "modify"
    ordinary_kind = (
        "create" if raw_action in {"create", "generate", "build", "write", "add"}
        else "delete" if raw_action in {"delete", "remove"}
        else "modify"
    )
    for relative in roles["effects"]:
        folded_relative = relative.casefold()
        if folded_relative not in claimed and not any(
            operand in folded_relative for operand in claimed if operand != folded_relative
        ):
            identifier = "operation_" + hashlib.sha256(
                (ordinary_kind + "|target=" + relative.casefold()).encode("utf-8")
            ).hexdigest()[:12]
            operations.append({"id": identifier, "kind": ordinary_kind, "target": relative})
            claimed.add(relative.casefold())
    for relative in roles["protected"]:
        folded_relative = relative.casefold()
        if folded_relative not in claimed and not any(
            operand in folded_relative for operand in claimed if operand != folded_relative
        ):
            identifier = "operation_" + hashlib.sha256(
                ("preserve|target=" + relative.casefold()).encode("utf-8")
            ).hexdigest()[:12]
            operations.append({"id": identifier, "kind": "preserve", "target": relative})
            claimed.add(relative.casefold())
    for operation in operations:
        baseline: dict[str, str | None] = {}
        baseline_text: dict[str, str | None] = {}
        for field in ("target", "source", "destination"):
            relative = str(operation.get(field) or "")
            if relative:
                baseline_path = confined_path(root, relative)
                baseline[field] = file_sha256(baseline_path)
                baseline_text[field] = _normalized_text_sha256(baseline_path)
        operation["baseline_sha256"] = baseline
        operation["baseline_text_sha256"] = baseline_text
    operation_effects: list[str] = []
    operation_protected: list[str] = []
    for operation in operations:
        kind = str(operation.get("kind") or "")
        if kind in {"modify", "create", "delete", "replace"}:
            operation_effects.append(str(operation.get("target") or ""))
        if kind in {"move", "rename"}:
            operation_effects.extend([
                str(operation.get("source") or ""), str(operation.get("destination") or ""),
            ])
        if kind == "copy":
            operation_effects.append(str(operation.get("destination") or ""))
            operation_protected.append(str(operation.get("source") or ""))
        if kind == "replace":
            operation_protected.append(str(operation.get("source") or ""))
        if kind == "preserve":
            operation_protected.append(str(operation.get("target") or ""))
    operation_effects = [one for one in dict.fromkeys(operation_effects) if one]
    operation_protected = [one for one in dict.fromkeys(operation_protected) if one]
    def explicitly_protected(relative: str) -> bool:
        variants = [re.escape(relative), re.escape(relative.replace("/", "\\"))]
        path_pattern = "(?:" + "|".join(variants) + ")"
        if re.search(
            rf"(?:do\s+not|don't|never|must\s+not|should\s+not|may\s+not)"
            rf"[^,.;!?]{{0,48}}{path_pattern}", goal, re.I,
        ) or re.search(
            rf"(?:preserve|keep|leave)\s+(?:the\s+)?(?:file\s+)?"
            rf"[\"'`(]*{path_pattern}", goal, re.I,
        ):
            return True
        # A suffix such as "parser.py must not be changed" protects that
        # operand.  Stop at coordination/punctuation and, crucially, at a
        # second path mention so "update app.py, preserve README.md unchanged"
        # cannot project README's polarity backwards onto app.py.
        for match in re.finditer(path_pattern, goal, re.I):
            tail = goal[match.end():match.end() + 96]
            tail = re.split(r"[,;.!?]", tail, maxsplit=1)[0]
            status = re.search(
                r"(?:unchanged|untouched|read[- ]only|must\s+not|should\s+not|"
                r"may\s+not|is\s+not\s+to)", tail, re.I,
            )
            if status is None:
                continue
            before_status = tail[:status.start()]
            if not _goal_named_paths(before_status):
                return True
        return False

    explicit_conflict = [
        effect for effect in operation_effects
        if explicitly_protected(effect)
        and not any(
            str(operation.get("kind") or "") in {"copy", "replace"}
            and str(operation.get("source") or "").casefold() == effect.casefold()
            for operation in operations
        )
    ]
    if explicit_conflict:
        raise HarnessError(
            "The goal requires a transfer/change while also prohibiting that same path: "
            + ", ".join(dict.fromkeys(explicit_conflict))
            + ". Clarify which instruction should control."
        )
    if operations:
        operands = [*operation_effects, *operation_protected]
        def independent_mention(candidate: str) -> bool:
            folded = candidate.casefold()
            related = [one for one in operands if one.casefold() in folded or folded in one.casefold()]
            return not related
        effects = list(dict.fromkeys([*operation_effects, *[
            one for one in roles["effects"]
            if one not in operation_protected and independent_mention(one)
        ]]))
        protected = list(dict.fromkeys([*operation_protected, *[
            one for one in roles["protected"]
            if one not in operation_effects and independent_mention(one)
        ]]))
        roles = {
            "mentions": [
                *[{"path": one, "role": "effect"} for one in effects],
                *[{"path": one, "role": "protected"} for one in protected],
            ],
            "effects": effects,
            "protected": protected,
        }
        if effects and not _informational_goal(goal):
            intent = "mutation"
    conflict = [
        effect for effect in roles["effects"]
        if any(_paths_overlap(effect, protected) for protected in roles["protected"])
    ]
    if conflict:
        raise HarnessError(
            "The goal gives the same project path incompatible write and preserve/reference roles: "
            + ", ".join(conflict)
        )
    for relative in [*roles["effects"], *roles["protected"]]:
        confined_path(root, relative)
    core = {
        "schema_version": 1,
        "speech_act": "information_query" if intent == "read_only" else (
            "request_action" if _requested_action_goal(goal) else
            "desired_outcome" if intent == "mutation" else "project_work"
        ),
        "intent": intent,
        "operands": [
            {
                "path": one["path"],
                "role": "PRESERVE_EXACT" if one["role"] == "protected" else "WRITE_MODIFY",
            }
            for one in roles["mentions"]
        ],
        "operations": operations,
        "write_policy": {
            "mode": "DENY_ALL" if intent == "read_only" else "SCOPED",
            "grants": [] if intent == "read_only" else list(roles["effects"]),
            "protected": list(roles["protected"]),
            "exact_capabilities": {
                path: sorted(capabilities)
                for path, capabilities in _operation_write_grants(operations).items()
            } if intent != "read_only" else {},
        },
    }
    digest_core = copy.deepcopy(core)
    for operation in digest_core.get("operations", []):
        if isinstance(operation, dict):
            operation.pop("baseline_sha256", None)
            operation.pop("baseline_text_sha256", None)
    core["spec_digest"] = hashlib.sha256(json.dumps(
        digest_core, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()
    return core


def _artifact_kind(words: str) -> str:
    folded = words.casefold()
    if re.search(r"trace?ability|tracibility|workbook|dashboard", folded):
        return "traceability"
    if "langgraph" in folded:
        return "langgraph"
    if re.search(r"upload|bundle|drag[- ]and[- ]drop", folded):
        return "upload_bundle"
    if re.search(r"obsidian|lasting memory|second brain|2nd brain|memory vault", folded):
        return "durable_memory"
    if re.search(r"\b(?:test|tests|testing|unit|e2e|api)\b", folded):
        return "tests"
    return "project_effect"


def _positive_creation_imperative(text: str, start: int) -> bool:
    if _informational_goal(text):
        return False
    before = text[:start]
    boundaries = [one.end() for one in re.finditer(r"[;.!?\n,]", before)]
    prefix = before[boundaries[-1] if boundaries else 0:].strip().casefold()
    if re.search(r"\b(?:do not|don't|never|avoid|without)\b", prefix):
        return bool(re.search(r"\b(?:except|unless|but|however|nevertheless|nonetheless)\b", prefix))
    if re.search(r"\b(?:can|could|may|might|would)\s*$", prefix):
        return False
    if re.search(r"\b(?:script|tool|system|code|function|class|provider)\b.*\bwill\s*$", prefix):
        return False
    if re.search(
        r"\b(?:add|implement|write|modify|update|build)\b.*\b(?:functionality|command|"
        r"code|logic|feature|support|ability|handler|method|function)\b.*\bto\s*$",
        prefix,
    ):
        return False
    return True


def _singular_artifact_term(term: str) -> str:
    irregular = {
        "inventories": "inventory", "indices": "index", "indexes": "index",
        "summaries": "summary", "policies": "policy",
    }
    folded = term.casefold()
    if folded in irregular:
        return irregular[folded]
    if folded.endswith("ies") and len(folded) > 3:
        return folded[:-3] + "y"
    if folded.endswith("s") and not folded.endswith("ss"):
        return folded[:-1]
    return folded


def _explicit_created_artifacts(goal: str) -> list[dict[str, Any]]:
    """Parse bounded coordinated objects of explicit creation imperatives.

    This is deliberately conservative: long explanatory clauses are not
    guessed into filenames.  A short novel artifact remains independently
    unverified until a changed path semantically matches its noun phrase.
    """

    relational_words = {
        "a", "an", "the", "of", "for", "to", "in", "on", "at", "by",
        "with", "from", "into", "onto", "via", "per", "as",
        "file", "files", "artifact", "artifacts", "new", "all", "our",
        "my", "your", "result", "results",
    }
    artifact_heads = {
        "report", "manifest", "checklist", "document", "guide", "log",
        "inventory", "index", "sbom", "certificate", "plan", "ledger",
        "register", "catalog", "catalogue", "matrix", "manual", "runbook",
        "playbook", "roadmap", "specification", "summary", "policy",
        "procedure",
    }
    content_verbs = {
        "document", "include", "describe", "detail", "summarize", "summarise",
        "record", "list", "cover", "capture", "explain", "present",
    }
    count_words = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        "eleven": 11, "twelve": 12,
    }
    artifacts: list[dict[str, Any]] = []
    masked_goal = _mask_goal_files(str(goal or ""))
    for match in re.finditer(
        r"\b(?:create|generate|build|write|document|produce)\b([^.;!?\n]+)",
        masked_goal, re.I,
    ):
        if not _positive_creation_imperative(masked_goal, match.start()):
            continue
        tail = match.group(1)
        for raw in re.split(r"\s*,\s*|\s+(?:and|as\s+well\s+as)\s+", tail, flags=re.I):
            raw = re.sub(r"^\s*and\s+", "", str(raw), flags=re.I)
            coordinated = re.match(
                r"^\s*(?:then\s+)?(create|generate|build|write|document|produce|include|describe|detail|summari[sz]e|record|list|cover|capture|explain|present)\b\s*(.*)$",
                raw, re.I,
            )
            if coordinated:
                # A new coordinated imperative is a new clause, not another
                # noun in the first verb's object list. If it supplies an
                # exact filename, that exact-path requirement is sufficient;
                # prose such as "document findings in notes.md" must not
                # invent a second artifact called "document findings".
                verb = coordinated.group(1).casefold()
                if "PROJECT_FILE" in raw or verb in content_verbs:
                    continue
                object_phrase = coordinated.group(2)
                # "write instructions" is normally content of the preceding
                # artifact. "write a migration guide" is an explicit second
                # deliverable because it has an artifact-headed noun phrase.
                if verb == "write" and not re.match(r"^\s*(?:a|an)\s+", object_phrase, re.I):
                    continue
                raw = object_phrase
            if "PROJECT_FILE" in raw:
                # Exact named files are already acceptance requirements.  The
                # surrounding words ("my ... file", "a file called ...") are
                # not an additional generic deliverable.
                continue
            phrase = raw.replace("PROJECT_FILE", " ")
            phrase = re.sub(r"\s+", " ", phrase).strip(" :-,()")
            phrase = re.sub(r"^(?:a|an|the|all|new|lasting)\s+", "", phrase, flags=re.I)
            phrase = re.split(
                r"\b(?:that|which|who|whose|where|named|called|including|containing)\b",
                phrase, maxsplit=1, flags=re.I,
            )[0].strip()
            phrase = re.sub(
                r"\s+for\s+(?:the\s+)?(?:project|repository|repo|codebase)\b.*$",
                "", phrase, flags=re.I,
            ).strip()
            if re.match(r"^copies?\s+of\s+(?:the\s+)?files?\b", phrase, re.I):
                # This is the object of a copy/staging operation, not a second
                # artifact identity.  Its destination/bundle requirement is
                # compiled separately from the authorized destination root.
                continue
            if re.match(
                r"^(?:code|functionality|command|logic|feature|support|ability|handler|"
                r"method|function)\s+to\b",
                phrase, re.I,
            ):
                continue
            if re.match(r"^(?:then\s+)?(?:have|ask|let)\b", phrase, re.I):
                continue
            words = re.findall(r"[A-Za-z][A-Za-z0-9_-]*|\d+", phrase)
            if not words or _artifact_kind(phrase) != "project_effect":
                continue
            count = 1
            if words[0].casefold() in count_words:
                count = count_words[words[0].casefold()]
                words = words[1:]
            elif words[0].isdigit():
                count = max(1, min(int(words[0]), 100))
                words = words[1:]
            terms: list[str] = []
            for word in words:
                folded_word = word.casefold()
                if folded_word in relational_words:
                    continue
                singular = _singular_artifact_term(folded_word)
                terms.append(singular if singular in artifact_heads else folded_word)
            generic_file = any(word.casefold() in {"file", "files"} for word in words)
            # Head nouns establish deliverable identity. Modifiers may be long
            # (for example a disaster-recovery readiness checklist document),
            # so there is no silent word-count cliff. Conversely, temporal and
            # procedural prose without an artifact head is not auto-ratified.
            if not any(term in artifact_heads for term in terms) and not generic_file:
                continue
            if generic_file and not terms:
                terms = ["file"]
            if not terms:
                continue
            identifier = "artifact_" + hashlib.sha256(
                " ".join(terms).encode("utf-8")
            ).hexdigest()[:12]
            for ordinal in range(1, count + 1):
                counted_id = identifier if count == 1 else f"{identifier}_{ordinal}"
                if any(one["id"] == counted_id for one in artifacts):
                    continue
                artifacts.append({
                    "id": counted_id,
                    "description": f"Explicit artifact '{phrase}' is created"
                    + (f" ({ordinal} of {count})" if count > 1 else ""),
                    "kind": "generic_artifact",
                    "artifact_terms": terms,
                    "generic_file": generic_file,
                    "requested_count": count,
                    "ordinal": ordinal,
                })
    return artifacts


def _behavior_acceptance_clauses(goal: str) -> list[dict[str, Any]]:
    """Derive atomic runtime outcomes after removing request/file-state scaffolding.

    This is deliberately not gated by a small list of favored implementation
    verbs.  Any actionable clause with a semantic outcome becomes a behavior
    requirement; pure artifact/file-state requests reduce to no outcome terms.
    """

    if _goal_intent(goal) == "read_only":
        return []
    operations = _goal_operations(goal)
    semantic_signal = re.search(
        r"\b(?:so\s+that|ensur\w*|prevent\w*|support\w*|handl\w*|reject\w*|retr\w*|"
        r"timeout\w*|crash\w*|invalid\w*|malform\w*|unicode|round[- ]?trip\w*|"
        r"allow\w*|den(?:y|ied|ies)|return\w*|emit\w*|rais\w*|deadlock\w*|"
        r"fail\w*|error\w*|correct\w*|resolv\w*)\b",
        str(goal), re.I,
    )
    if operations and not semantic_signal:
        # Transfer, preservation, and ordinary file-state frames have exact
        # artifact postconditions. Request/reference scaffolding must never be
        # promoted into an unprovable runtime outcome.
        return []
    masked = _mask_goal_files(str(goal or ""))
    artifacts = _explicit_created_artifacts(goal)
    artifact_terms = {
        str(term).casefold() for item in artifacts
        for term in item.get("artifact_terms", [])
    }
    stop = {
        "a", "an", "the", "this", "that", "these", "those", "please", "kindly",
        "can", "could", "would", "will", "you", "we", "i", "me", "my", "our",
        "need", "needs", "needed", "want", "wants", "wanted", "like", "have", "has",
        "should", "must", "be", "been", "being", "is", "are", "was", "were",
        "project", "repository", "repo", "file", "files", "project_file", "code",
        "functionality", "feature", "logic", "command", "method", "function", "handler",
        "update", "updated", "updating", "fix", "fixed", "fixing", "repair", "repaired",
        "repairing", "modify", "modified", "modifying", "edit", "edited", "editing",
        "change", "changed", "changing", "implement", "implemented", "implementing",
        "create", "created", "creating", "add", "added", "adding", "delete", "deleted",
        "rename", "renamed", "move", "moved", "copy", "copied", "replace", "replaced",
        "build", "built", "generate", "generated", "write", "written", "writing",
        "refactor", "refactored", "refactoring", "ensure", "resolve", "resolved",
        "prevent", "make", "correct", "address", "handle", "support", "handling",
        "bug", "issue", "problem", "behavior", "behaviour", "state", "comments", "comment",
        "documentation", "docs", "test", "tests", "testing",
        "to", "so", "that", "for", "of", "in", "on", "with", "from", "by", "as",
        "without", "while", "and", "or", "then", "also", "it", "if", "mind",
        "consult", "before", "after", "reference", "only", "keep", "kept", "keeping",
        "unchanged", "untouched", "touch", "do", "not", "but", "content", "contents",
        "use", "using", "read", "review", "preserve", "source", "target",
    }

    def normalize(word: str) -> str:
        folded = word.casefold()
        for suffix in ("ing", "ed", "es", "s"):
            if len(folded) > len(suffix) + 3 and folded.endswith(suffix):
                folded = folded[:-len(suffix)]
                break
        return {
            "crashe": "crash", "retri": "retry", "failur": "failure",
            "requeste": "request", "rejecte": "reject", "preserv": "preserve",
            "addres": "address", "handl": "handle", "chang": "change", "updat": "update",
        }.get(folded, folded)

    clauses: list[dict[str, Any]] = []
    seen: set[str] = set()
    normalized_stop = {normalize(one) for one in stop}
    parsed = _parse_goal_intent(goal)
    corrective = bool(set(parsed.get("mutation_actions", [])) & {
        "fix", "repair", "resolve", "refactor",
    })
    if re.match(
        r"^\s*(?:use|consult|read|review|inspect)\b.*\b(?:as\s+(?:a\s+)?reference|"
        r"only\s+as\s+(?:a\s+)?reference|read[- ]only)\b.*\bafter\b",
        str(goal), re.I,
    ):
        corrective = False
    for sentence in re.split(r"[.;!?\n]+", masked):
        sentence = sentence.strip()
        if not sentence:
            continue
        sentence_intent = _goal_intent(sentence)
        if sentence_intent != "mutation" and not _requested_action_goal(sentence):
            continue
        if artifacts and not re.search(
            r"\b(?:so\s+that|ensure|prevent|support|handle|reject|retry|timeout|crash|"
            r"validate|enforce|allow|deny)\b", sentence, re.I,
        ):
            # Creation plus content-population instructions describe the
            # artifact state (for example "have Claude create a file, then
            # Codex populate it"), not an independently runnable behavior.
            continue
        if artifacts and re.match(
            r"^(?:please\s+)?(?:create|generate|build|write|document|produce)\b",
            sentence, re.I,
        ):
            # This clause's acceptance is the independently typed artifact
            # requirement. Content complements/background must not become a
            # second runtime behavior requirement.
            continue
        if _is_test_goal(sentence) and re.match(
            r"^(?:please\s+)?(?:create|generate|build|write|add)\b", sentence, re.I,
        ):
            continue
        purpose = re.search(r"\b(?:so\s+that|so|to)\b\s+(.+)$", sentence, re.I)
        body = purpose.group(1) if purpose else sentence
        atoms = re.split(r"\s+(?:and|as\s+well\s+as)\s+", body, flags=re.I)
        for atom in atoms:
            words = [normalize(one) for one in re.findall(r"[^\W_][\w-]*", atom, re.UNICODE)]
            terms = [one for one in words if one and one not in normalized_stop]
            terms = list(dict.fromkeys(terms))
            if artifact_terms and terms and set(terms).issubset(artifact_terms):
                continue
            if not terms:
                continue
            normalized = " ".join(terms)
            if normalized in seen:
                continue
            seen.add(normalized)
            clauses.append({
                "id": "behavior_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12],
                "description": "Fresh causal tests prove runtime outcome: " + normalized,
                "kind": "behavior",
                "acceptance_terms": terms,
                "acceptance_clause": normalized,
                "proof_mode": "fresh_causal_test",
                "requires_goal_binding": True,
                "goal_sha256": hashlib.sha256(goal.encode("utf-8")).hexdigest(),
            })
    if not clauses and corrective:
        path_terms = [
            normalize(Path(one).stem) for one in _goal_path_roles(goal)["effects"]
            if normalize(Path(one).stem) not in stop
        ]
        terms = list(dict.fromkeys([*path_terms, "regression"]))
        normalized = " ".join(terms)
        clauses.append({
            "id": "behavior_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12],
            "description": "Fresh causal regression tests prove the requested correction",
            "kind": "behavior", "acceptance_terms": terms,
            "acceptance_clause": normalized, "proof_mode": "fresh_causal_test",
            "requires_goal_binding": True,
            "goal_sha256": hashlib.sha256(goal.encode("utf-8")).hexdigest(),
        })
    if re.search(r"\brefactor\b", str(goal), re.I) and re.search(
        r"\bwithout\s+changing\s+(?:behavior|behaviour|api|semantics?)\b", str(goal), re.I,
    ):
        for item in clauses:
            item["kind"] = "behavior_preservation"
            item["proof_mode"] = "fresh_preservation_test"
    return clauses


def _baseline_behavior_candidates(
    root: Path, goal: str, goal_spec: dict[str, Any],
) -> list[dict[str, Any]]:
    """Discover immutable pre-provider callable candidates for a behavior goal.

    Candidate identity comes only from the selected project's current baseline;
    tests or provider proposals created later cannot self-select the target.
    """

    if not _behavior_acceptance_clauses(goal):
        return []
    paths = [
        str(path) for path in goal_spec.get("write_policy", {}).get("grants", [])
        if Path(str(path)).suffix.casefold() in {".py", ".js", ".jsx", ".ts", ".tsx"}
    ]
    candidates: list[dict[str, Any]] = []

    def add(relative: str, qualname: str, start: int, end: int, source: str) -> None:
        if not qualname or qualname.startswith("_"):
            return
        core = {
            "path": relative.replace("\\", "/"),
            "qualname": qualname,
            "start": int(start),
            "end": int(end),
            "baseline_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        }
        core["candidate_id"] = "target_" + hashlib.sha256(json.dumps(
            core, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")).hexdigest()[:16]
        candidates.append(core)

    for relative in dict.fromkeys(paths):
        try:
            path = confined_path(root, relative, allow_missing=False)
            source = path.read_text(encoding="utf-8", errors="strict")
        except (HarnessError, OSError, UnicodeError):
            continue
        suffix = path.suffix.casefold()
        if suffix == ".py":
            try:
                tree = ast.parse(source, filename=relative)
            except SyntaxError:
                continue
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    add(relative, node.name, node.lineno, getattr(node, "end_lineno", node.lineno), source)
                elif isinstance(node, ast.ClassDef):
                    for child in node.body:
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            add(
                                relative, f"{node.name}.{child.name}", child.lineno,
                                getattr(child, "end_lineno", child.lineno), source,
                            )
        else:
            patterns = (
                r"\b(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(",
                r"\b(?:exports\.|module\.exports\.)?([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>",
            )
            for pattern in patterns:
                for match in re.finditer(pattern, source):
                    add(
                        relative, match.group(1),
                        source.count("\n", 0, match.start()) + 1,
                        source.count("\n", 0, match.end()) + 1,
                        source,
                    )
    return list({one["candidate_id"]: one for one in candidates}.values())


def _acceptance_target_decision(
    root: Path,
    goal: str,
    goal_spec: dict[str, Any],
    user_answer: str = "",
) -> dict[str, Any]:
    """Return a frozen target decision or a durable clarification question."""

    if not _behavior_acceptance_clauses(goal) or re.search(r"https?://", goal, re.I):
        return {"status": "not_required", "candidates": []}
    candidates = _baseline_behavior_candidates(root, goal, goal_spec)
    combined = f"{goal}\n{user_answer}".casefold()
    selected = [
        one for one in candidates
        if one["candidate_id"].casefold() in combined
        or bool(re.search(rf"(?<![\w.]){re.escape(str(one['qualname']).casefold())}(?![\w])", combined))
    ]
    answer_selected = bool(user_answer and any(
        one["candidate_id"].casefold() in user_answer.casefold()
        or bool(re.search(
            rf"(?<![\w.]){re.escape(str(one['qualname']).casefold())}(?![\w])",
            user_answer.casefold(),
        ))
        for one in candidates
    ))
    # A path-qualified single public callable is unambiguous without asking.
    if len(candidates) == 1 and not selected:
        selected = candidates[:]
    if len(selected) == 1:
        decision = {
            "status": "ratified" if answer_selected else "unambiguous",
            "target": selected[0],
            "candidates": candidates,
            "answer_sha256": hashlib.sha256(user_answer.encode("utf-8")).hexdigest()
            if answer_selected else "",
        }
        decision["ratification_digest"] = hashlib.sha256(json.dumps(
            decision, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        return decision
    labels = ", ".join(
        f"{one['qualname']} ({one['candidate_id']})" for one in candidates
    ) or "no safely identifiable callable"
    return {
        "status": "needs_clarification",
        "candidates": candidates,
        "question": (
            "Which exact production callable or route should Nexus use for the acceptance probe? "
            f"Baseline candidates: {labels}. Reply with the callable name or candidate ID."
        ),
        "baseline_merkle": _project_tree_merkle(root)[0],
    }


def _derive_requirement_contract(
    root: Path,
    goal: str,
    required_effect_paths: list[str] | None = None,
    *,
    ratified_by: list[str] | None = None,
) -> dict[str, Any]:
    """Build an engine-owned, durable whole-goal acceptance contract.

    Requirements are semantic artifact/behavior classes plus exact plan/path
    effects.  Provider consensus may expand this contract through effect_paths;
    it cannot remove requirements explicitly present in the user's goal.
    """

    requirements: dict[str, dict[str, Any]] = {}
    goal_spec = _compile_goal_spec(root, goal)
    intent = str(goal_spec.get("intent") or _goal_intent(goal))
    path_roles = {
        "mentions": [
            {"path": one["path"], "role": "protected" if one["role"] == "PRESERVE_EXACT" else "effect"}
            for one in goal_spec.get("operands", []) if isinstance(one, dict)
        ],
        "effects": list(goal_spec.get("write_policy", {}).get("grants", [])),
        "protected": list(goal_spec.get("write_policy", {}).get("protected", [])),
    }

    def require(identifier: str, description: str, kind: str) -> dict[str, Any]:
        return requirements.setdefault(identifier, {
            "id": identifier,
            "description": description,
            "kind": kind,
            "effect_roots": [],
            "effect_paths": [],
        })

    if intent != "read_only":
        folded = goal.casefold()
        if _is_test_goal(goal):
            item = require("tests", "Runnable requested tests execute successfully", "tests")
            item["requested_levels"] = _requested_test_levels(goal)
        if re.search(r"trace?ability|tracibility", folded) and re.search(r"workbook|dashboard|html", folded):
            require("traceability", "A traceability workbook/dashboard is created", "traceability")
        if "langgraph" in folded:
            require("langgraph_artifact", "LangGraph implementation artifacts are created", "langgraph")
            if re.search(r"enforc|must be followed|100% followed", folded):
                require("langgraph_enforcement", "LangGraph enforcement behavior is proven", "behavior")
        behavior_clauses = [] if "langgraph" in folded else _behavior_acceptance_clauses(goal)
        if behavior_clauses:
            for clause in behavior_clauses:
                behavior = require(clause["id"], clause["description"], clause["kind"])
                for field in (
                    "acceptance_terms", "acceptance_clause", "proof_mode",
                    "requires_goal_binding", "goal_sha256",
                ):
                    behavior[field] = copy.deepcopy(clause[field])
            require(
                "project_effect",
                "The implementation materially changes selected-project state",
                "project_effect",
            )
        for operation in goal_spec.get("operations", []):
            if not isinstance(operation, dict):
                continue
            if operation.get("kind") in {"modify", "create"}:
                # Exact-path effect evidence already proves these ordinary
                # file-state frames. Transfer/deletion/preservation frames
                # retain additional semantic postconditions below.
                continue
            item = require(
                str(operation.get("id") or "operation_unknown"),
                "The exact file operation reaches its compiled postcondition",
                "operation_postcondition",
            )
            item["operation"] = copy.deepcopy(operation)
        if re.search(r"upload", folded) and re.search(r"bundle|folder|copies|drag\s+and\s+drop|github", folded):
            require("upload_bundle", "A structurally usable upload bundle is created", "upload_bundle")
            if re.search(r"commit\s+(?:title|message)|\.md\s+file", folded):
                require("upload_commit_message", "The upload bundle includes its commit message", "commit_message")
        if re.search(r"obsidian|lasting memory|2nd brain|second brain|memory vault", folded):
            require("durable_memory", "Lasting project memory is updated", "durable_memory")
        for artifact in _explicit_created_artifacts(goal):
            item = require(artifact["id"], artifact["description"], artifact["kind"])
            item["artifact_terms"] = artifact["artifact_terms"]
            item["generic_file"] = artifact["generic_file"]

        authority = _path_authority_from_goal(root, goal)
        for relative in authority["writable"]:
            kind = _artifact_kind(relative)
            identifier = {
                "tests": "tests",
                "traceability": "traceability",
                "langgraph": "langgraph_artifact",
                "upload_bundle": "upload_bundle",
                "durable_memory": "durable_memory",
            }.get(kind, "destination_" + hashlib.sha256(relative.encode("utf-8")).hexdigest()[:12])
            item = require(identifier, f"Required destination {relative} receives its artifact", kind)
            if relative not in item["effect_roots"]:
                item["effect_roots"].append(relative)
            if kind == "upload_bundle" and "upload_commit_message" in requirements:
                commit = requirements["upload_commit_message"]
                if relative not in commit["effect_roots"]:
                    commit["effect_roots"].append(relative)

        for raw in required_effect_paths or []:
            relative = str(raw or "").replace("\\", "/").strip().strip("/")
            if not relative:
                continue
            if any(_paths_overlap(relative, protected) for protected in path_roles["protected"]):
                raise HarnessError(
                    f"Planned effect path {relative} conflicts with protected/read-only goal path"
                )
            try:
                confined_path(root, relative)
            except HarnessError:
                continue
            identifier = "path_" + hashlib.sha256(relative.encode("utf-8")).hexdigest()[:12]
            item = require(identifier, f"Planned project effect {relative} is produced", "exact_path")
            if relative not in item["effect_paths"]:
                item["effect_paths"].append(relative)

        for relative in dict.fromkeys(
            match.replace("\\", "/").strip("/") for match in path_roles["effects"]
        ):
            # A bare explicitly named file inherits the unique authorized
            # destination of its semantic class from the surrounding prompt.
            # This keeps "TEST-ci.yml" in the stated test-output root without
            # granting arbitrary mentioned source/reference paths.
            if "/" not in relative:
                matching_roots = [
                    root_path for root_path in authority["writable"]
                    if _artifact_kind(root_path) == _artifact_kind(relative)
                ]
                if len(matching_roots) == 1:
                    relative = matching_roots[0].rstrip("/") + "/" + relative
            item = require(
                "path_" + hashlib.sha256(relative.encode("utf-8")).hexdigest()[:12],
                f"Explicitly requested file {relative} is changed", "exact_path",
            )
            item["effect_paths"].append(relative)
        if not requirements:
            require("project_effect", "The requested project state materially changes", "project_effect")

    canonical = list(requirements.values())
    return {
        "schema_version": 1,
        "goal_sha256": hashlib.sha256(goal.encode("utf-8")).hexdigest(),
        "intent": intent,
        "requirements": canonical,
        "path_mentions": path_roles["mentions"],
        "protected_paths": path_roles["protected"],
        "goal_spec_digest": goal_spec.get("spec_digest"),
        "operations": copy.deepcopy(goal_spec.get("operations", [])),
        "ratified_by": list(dict.fromkeys(ratified_by or [])),
        "status": "ratified" if ratified_by else "derived",
    }


def _requirement_artifact_evidence(
    root: Path, contract: dict[str, Any], changed: list[str],
) -> dict[str, Any]:
    normalized = list(dict.fromkeys(path.replace("\\", "/") for path in changed))
    protected_paths = [
        str(one).replace("\\", "/") for one in contract.get("protected_paths", [])
        if isinstance(one, str)
    ]
    protected_violations = [
        relative for relative in normalized if _path_is_under(relative, protected_paths)
    ] if protected_paths else []
    static = _runnable_tests_in_changed_files(root, changed)
    evidence: list[dict[str, Any]] = []
    unmet: list[str] = []
    pending_execution: list[str] = []
    for requirement in contract.get("requirements", []):
        if not isinstance(requirement, dict):
            continue
        identifier = str(requirement.get("id") or "unknown")
        kind = str(requirement.get("kind") or "project_effect")
        roots = [str(one) for one in requirement.get("effect_roots", []) if isinstance(one, str)]
        paths = [str(one) for one in requirement.get("effect_paths", []) if isinstance(one, str)]
        candidates = [
            relative for relative in normalized
            if (not roots or _path_is_under(relative, roots))
            and (not paths or any(
                relative.casefold() == path.casefold()
                or relative.casefold().startswith(path.casefold().rstrip("/") + "/")
                for path in paths
            ))
        ]
        if kind == "tests":
            levels = requirement.get("requested_levels", [])
            level_counts = static.get("levels", {})
            scoped_files = [
                one for one in static["files"]
                if (not roots or _path_is_under(one, roots))
                and (not paths or any(
                    one.casefold() == path.casefold()
                    or one.casefold().startswith(path.casefold().rstrip("/") + "/")
                    for path in paths
                ))
            ]
            artifact_ok = bool(scoped_files) and static["runnable"] > 0 and all(
                int(level_counts.get(level, {}).get("runnable", 0)) > 0
                and any(one in scoped_files for one in level_counts.get(level, {}).get("files", []))
                for level in levels
            )
            candidates = scoped_files
            if artifact_ok:
                pending_execution.append(identifier)
        elif kind == "traceability":
            pool = candidates if roots or paths else normalized
            candidates = [one for one in pool if re.search(
                r"trace?ability|tracibility|(?:^|[/_. -])trace(?:[/_. -]|$)|workbook|dashboard", one, re.I
            )]
            artifact_ok = bool(candidates)
        elif kind == "langgraph":
            pool = candidates if roots or paths else normalized
            candidates = [one for one in pool if "langgraph" in one.casefold()]
            artifact_ok = bool(candidates)
        elif kind in {"behavior", "behavior_preservation"}:
            artifact_ok = True
            pending_execution.append(identifier)
        elif kind == "operation_postcondition":
            operation = requirement.get("operation", {})
            operation_kind = str(operation.get("kind") or "")
            baseline = operation.get("baseline_sha256", {}) if isinstance(operation, dict) else {}
            baseline_text = operation.get("baseline_text_sha256", {}) if isinstance(operation, dict) else {}
            target = str(operation.get("target") or "")
            source = str(operation.get("source") or "")
            destination = str(operation.get("destination") or "")
            changed_folded = {one.casefold() for one in normalized}
            current = lambda relative: file_sha256(confined_path(root, relative, allow_missing=True)) if relative else None
            if operation_kind == "preserve":
                artifact_ok = (
                    target.casefold() not in changed_folded
                    and current(target) == baseline.get("target")
                )
                candidates = []
            elif operation_kind == "delete":
                artifact_ok = target.casefold() in changed_folded and current(target) is None
                candidates = [target] if artifact_ok else []
            elif operation_kind in {"move", "rename"}:
                artifact_ok = (
                    source.casefold() in changed_folded
                    and destination.casefold() in changed_folded
                    and current(source) is None
                    and current(destination) is not None
                    and (
                        current(destination) == baseline.get("source")
                        or _normalized_text_sha256(confined_path(root, destination, allow_missing=True))
                        == baseline_text.get("source")
                    )
                )
                candidates = [source, destination] if artifact_ok else []
            elif operation_kind == "copy":
                artifact_ok = (
                    destination.casefold() in changed_folded
                    and current(source) == baseline.get("source")
                    and current(destination) is not None
                    and (
                        current(destination) == baseline.get("source")
                        or _normalized_text_sha256(confined_path(root, destination, allow_missing=True))
                        == baseline_text.get("source")
                    )
                )
                candidates = [destination] if artifact_ok else []
            elif operation_kind == "replace":
                artifact_ok = (
                    target.casefold() in changed_folded
                    and current(target) is not None
                    and current(target) != baseline.get("target")
                    and current(source) == baseline.get("source")
                    and (
                        current(target) == baseline.get("source")
                        or _normalized_text_sha256(
                            confined_path(root, target, allow_missing=True)
                        ) == baseline_text.get("source")
                    )
                )
                candidates = [target] if artifact_ok else []
            else:
                artifact_ok = (
                    target.casefold() in changed_folded
                    and current(target) is not None
                )
                candidates = [target] if artifact_ok else []
        elif kind == "upload_bundle":
            if roots or paths:
                artifact_ok = bool(candidates)
            else:
                candidates = [one for one in normalized if re.search(
                    r"(?:^|[/_. -])(?:upload|bundle)(?:[/_. -]|$)", one, re.I
                )]
                artifact_ok = bool(candidates)
        elif kind == "commit_message":
            pool = candidates if roots or paths else normalized
            candidates = [one for one in pool if one.casefold().endswith(".md") and (
                bool(roots or paths)
                or bool(re.search(r"(?:^|[/_. -])commit(?:[/_. -]|$).*(?:^|[/_. -])message(?:[/_. -]|$)", one, re.I))
                or (
                    bool(re.search(r"(?:^|[/_. -])(?:upload|bundle)(?:[/_. -]|$)", one, re.I))
                    and bool(re.search(r"(?:^|[/_. -])(?:commit|message)(?:[/_. -]|$)", one, re.I))
                )
            )]
            artifact_ok = bool(candidates)
        elif kind == "durable_memory":
            pool = candidates if roots or paths else normalized
            # A filename containing "memory" is not authority to write lasting
            # Obsidian state. Durable memory must be tied to an explicit
            # authorized root/path in the requirement contract.
            candidates = [
                one for one in pool
                if bool(roots or paths) and one.casefold().endswith(".md")
            ]
            artifact_ok = bool(candidates)
        elif kind == "generic_artifact":
            terms = [str(one).casefold() for one in requirement.get("artifact_terms", [])]
            pool = candidates if roots or paths else normalized
            candidates = list(pool) if requirement.get("generic_file") else [
                one for one in pool if all(
                    re.search(rf"(?:^|[/_. -]){re.escape(term)}(?:[/_. -]|$)", one, re.I)
                    for term in terms
                )
            ]
            artifact_ok = bool(candidates)
        else:
            artifact_ok = bool(candidates or normalized) if not paths and not roots else bool(candidates)
        evidence.append({
            "id": identifier, "kind": kind, "artifact_present": artifact_ok,
            "changed_paths": candidates,
            "explicit_effect_paths": paths,
            "execution_pending": identifier in pending_execution,
        })
        if not artifact_ok:
            unmet.append(identifier)
    # Independent deliverables require independent changed artifacts. Solve a
    # small bipartite assignment so an upload commit can coexist with another
    # bundle file, while a single conveniently named file cannot cross-prove a
    # bundle, commit message, memory, report, and manifest simultaneously.
    explicit_path_owners: dict[str, set[str]] = {}
    for item in evidence:
        for path in item.get("explicit_effect_paths", []):
            explicit_path_owners.setdefault(str(path).casefold(), set()).add(str(item["id"]))
    explicitly_shared = {
        path for path, owners in explicit_path_owners.items() if len(owners) > 1
    }
    for item in evidence:
        shared = next((
            str(path) for path in item.get("changed_paths", [])
            if str(path).casefold() in explicitly_shared
            and str(path).casefold() in {
                str(mapped).casefold() for mapped in item.get("explicit_effect_paths", [])
            }
        ), None)
        if shared is not None:
            item["assigned_changed_path"] = shared
            item["explicit_shared_mapping"] = True
    assignable = [
        item for item in evidence
        if item["artifact_present"] and item["changed_paths"]
        and item["kind"] not in {
            "behavior", "behavior_preservation", "project_effect", "exact_path",
            "operation_postcondition",
        }
        and not item.get("explicit_shared_mapping")
    ]
    owner_by_path: dict[str, str] = {}
    assigned_by_id: dict[str, str] = {}

    def assign(item: dict[str, Any], visited: set[str]) -> bool:
        identifier = str(item["id"])
        for relative in item["changed_paths"]:
            key = str(relative).casefold()
            if key in visited:
                continue
            visited.add(key)
            owner = owner_by_path.get(key)
            if owner is None:
                owner_by_path[key] = identifier
                assigned_by_id[identifier] = str(relative)
                return True
            prior = next((one for one in assignable if one["id"] == owner), None)
            if prior is not None and assign(prior, visited):
                owner_by_path[key] = identifier
                assigned_by_id[identifier] = str(relative)
                return True
        return False

    for item in sorted(assignable, key=lambda one: len(one["changed_paths"])):
        if not assign(item, set()):
            item["artifact_present"] = False
            item["independent_artifact_missing"] = True
            if item["id"] not in unmet:
                unmet.append(str(item["id"]))
        else:
            item["assigned_changed_path"] = assigned_by_id[str(item["id"])]
    return {
        "passed": not unmet and not protected_violations,
        "unmet": unmet,
        "protected_violations": protected_violations,
        "pending_execution": pending_execution,
        "evidence": evidence,
    }


def _broker_observation(
    result: dict[str, Any], relative_test: str,
) -> dict[str, Any] | None:
    for item in result.get("brokered_e2e_receipts", []):
        if not isinstance(item, dict) or str(item.get("test_file") or "").casefold() != relative_test.casefold():
            continue
        receipt = item.get("receipt")
        if isinstance(receipt, dict):
            return copy.deepcopy(receipt)
    return None


def _build_brokered_browser_receipts(
    config: LoadedConfig,
    root: Path,
    goal: str,
    requirement_contract: dict[str, Any],
    requirements: dict[str, dict[str, Any]],
    changed: list[str],
    transaction_ids: list[str],
    base_command: list[str],
    command_indexes: dict[str, list[int]],
    results: list[dict[str, Any]],
    positive_indexes: set[int],
    witnesses: dict[str, dict[str, Any]],
    session_id: str,
) -> list[dict[str, Any]]:
    """Seal current/current/baseline route+DOM observations as causal proof."""

    joined = " ".join(str(one).replace("\\", "/").casefold() for one in base_command)
    if "playwright" not in joined:
        return []
    run_nonce = uuid.uuid4().hex
    receipts: list[dict[str, Any]] = []
    test_paths = {str(one["path"]) for one in witnesses.values()}
    production_paths = [
        path.replace("\\", "/") for path in changed
        if path.replace("\\", "/") not in test_paths
    ]
    current_production = {
        relative: file_sha256(confined_path(root, relative, allow_missing=True))
        for relative in production_paths
    }
    current_merkle, _manifest = _project_tree_merkle(root)
    with tempfile.TemporaryDirectory(prefix="nexus-browser-causal-proof-") as temporary:
        current_root = Path(temporary) / "current-project"
        counter_root = Path(temporary) / "baseline-project"
        try:
            _copy_verification_snapshot(root, current_root)
            baseline_production = _restore_counterfactual(
                root, counter_root, transaction_ids, test_paths,
            )
        except (HarnessError, OSError):
            return []
        counter_config = LoadedConfig(
            copy.deepcopy(config.data), counter_root.resolve(), list(config.sources),
            dict(config.provenance), copy.deepcopy(config.trusted_floor),
        )
        for requirement_id, requirement in requirements.items():
            witness = witnesses.get(requirement_id)
            if not isinstance(witness, dict):
                continue
            semantic_trace = witness.get("semantic_trace")
            if not isinstance(semantic_trace, dict) or not isinstance(
                semantic_trace.get("browser_scenario"), dict,
            ):
                continue
            indexes = command_indexes.get(requirement_id, [])
            if len(indexes) < 2 or any(index not in positive_indexes for index in indexes[:2]):
                continue
            relative_test = str(witness.get("path") or "")
            current_observations = [
                _broker_observation(results[index], relative_test) for index in indexes[:2]
            ]
            if any(not isinstance(one, dict) or one.get("passed") is not True for one in current_observations):
                continue
            probe = _level_probe_command(base_command, [relative_test])
            if probe is None:
                continue
            counter_result = _contained_snapshot_command(
                counter_config, counter_root,
                _snapshot_command(probe, root, counter_root), denied_root=root,
            )
            baseline_observation = _broker_observation(counter_result, relative_test)
            if not isinstance(baseline_observation, dict) or baseline_observation.get("passed") is True:
                continue
            frozen_target = _frozen_acceptance_target(
                current_root, counter_root, production_paths, witness, goal, changed,
            )
            if frozen_target is None:
                continue
            scenario = _acceptance_scenario(requirement)
            if scenario is None:
                continue
            direct_probe = {
                "schema_version": 1,
                "target": copy.deepcopy(frozen_target),
                "scenario": copy.deepcopy(scenario),
                "current_observations": current_observations,
                "baseline_observation": baseline_observation,
                "adapter": "engine-direct-playwright-route-dom-v1",
            }
            direct_probe["direct_probe_digest"] = hashlib.sha256(json.dumps(
                direct_probe, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ).encode("utf-8")).hexdigest()
            route_path = str(frozen_target.get("path") or "")
            callable_identity = {
                "path": route_path,
                "method": "GET",
                "route": frozen_target.get("qualname"),
                "observable": copy.deepcopy(current_observations[0].get("observable")),
                "scenario_digest": semantic_trace["browser_scenario"].get("scenario_digest"),
            }
            callable_identity["identity_digest"] = hashlib.sha256(json.dumps(
                callable_identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ).encode("utf-8")).hexdigest()
            adapter_name = "node-playwright-engine-browser-causal-v1"
            executable = Path(base_command[0])
            receipt = {
                "schema_version": 1,
                "adapter": adapter_name,
                "adapter_digest": hashlib.sha256(adapter_name.encode("utf-8")).hexdigest(),
                "session_id": session_id,
                "run_nonce": run_nonce,
                "goal_sha256": requirement_contract.get("goal_sha256"),
                "requirement_id": requirement_id,
                "goal_spec_digest": requirement_contract.get("goal_spec_digest"),
                "acceptance_scenario": copy.deepcopy(scenario),
                "semantic_test_trace": copy.deepcopy(semantic_trace),
                "runtime_callable_identity": callable_identity,
                "frozen_target": frozen_target,
                "direct_acceptance_probe": direct_probe,
                "requirement_digest": hashlib.sha256(json.dumps(
                    requirement, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                ).encode("utf-8")).hexdigest(),
                "root_identity": hashlib.sha256(str(root.resolve()).casefold().encode("utf-8")).hexdigest(),
                "current_tree_merkle": current_merkle,
                "production_content": current_production,
                "baseline_production_content": baseline_production,
                "test_content": {relative_test: file_sha256(confined_path(root, relative_test, allow_missing=False))},
                "selected_test_files": [relative_test],
                "command_digest": hashlib.sha256(json.dumps(
                    probe, separators=(",", ":"), ensure_ascii=False,
                ).encode("utf-8")).hexdigest(),
                "command_approval_digest": _command_approval_digest(root, [probe]),
                "toolchain_digest": file_sha256(executable) if executable.is_file() else hashlib.sha256(
                    str(base_command[0]).encode("utf-8")
                ).hexdigest(),
                "current_result_sha256": [
                    hashlib.sha256((
                        str(results[index].get("stdout") or "") + "\n"
                        + str(results[index].get("stderr") or "")
                    ).encode("utf-8")).hexdigest()
                    for index in indexes[:2]
                ],
                "counterfactual_exit_code": counter_result.get("exit_code"),
                "counterfactual_output_sha256": hashlib.sha256(json.dumps(
                    baseline_observation, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                ).encode("utf-8")).hexdigest(),
                "counterfactual_assertion_failure": True,
                "coverage_hits": [route_path],
                "started_ns": time.time_ns(),
                "finished_ns": time.time_ns(),
            }
            receipt["receipt_digest"] = hashlib.sha256(json.dumps(
                receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ).encode("utf-8")).hexdigest()
            receipts.append(receipt)
    return receipts


def _build_causal_behavior_receipts(
    config: LoadedConfig,
    root: Path,
    goal: str,
    requirement_contract: dict[str, Any],
    changed: list[str],
    transaction_ids: list[str],
    base_command: list[str] | None,
    command_indexes: dict[str, list[int]],
    commands: list[list[str]],
    results: list[dict[str, Any]],
    positive_indexes: set[int],
    witnesses: dict[str, dict[str, Any]],
    session_id: str,
) -> list[dict[str, Any]]:
    """Create engine-observed fresh current/counterfactual evidence receipts."""

    if not transaction_ids or not base_command:
        return []
    requirements = {
        str(one.get("id")): one for one in requirement_contract.get("requirements", [])
        if isinstance(one, dict) and one.get("kind") in {"behavior", "behavior_preservation"}
    }
    test_paths = {str(one["path"]) for one in witnesses.values()}
    production_paths = [
        path.replace("\\", "/") for path in changed
        if path.replace("\\", "/") not in test_paths
    ]
    current_production = {
        relative: file_sha256(confined_path(root, relative, allow_missing=True))
        for relative in production_paths
    }
    if not any(current_production.values()):
        return []
    current_merkle, _current_manifest = _project_tree_merkle(root)
    run_nonce = uuid.uuid4().hex
    receipts = _build_brokered_browser_receipts(
        config, root, goal, requirement_contract, requirements, changed,
        transaction_ids, base_command, command_indexes, results,
        positive_indexes, witnesses, session_id,
    )
    browser_requirement_ids = {
        str(one.get("requirement_id")) for one in receipts
        if one.get("adapter") == "node-playwright-engine-browser-causal-v1"
    }
    with tempfile.TemporaryDirectory(prefix="nexus-causal-proof-") as temporary:
        temporary_root = Path(temporary)
        counter_root = temporary_root / "baseline-project"
        current_root = temporary_root / "current-project"
        try:
            _copy_verification_snapshot(root, current_root)
            baseline_production = _restore_counterfactual(
                root, counter_root, transaction_ids, test_paths,
            )
        except (HarnessError, OSError):
            return []
        if not any(
            baseline_production.get(path) != current_production.get(path)
            for path in production_paths
        ):
            return []
        counter_data = copy.deepcopy(config.data)
        counter_config = LoadedConfig(
            counter_data, counter_root.resolve(), list(config.sources),
            dict(config.provenance), copy.deepcopy(config.trusted_floor),
        )
        current_config = LoadedConfig(
            copy.deepcopy(config.data), current_root.resolve(), list(config.sources),
            dict(config.provenance), copy.deepcopy(config.trusted_floor),
        )
        for requirement_id, requirement in requirements.items():
            if requirement_id in browser_requirement_ids:
                continue
            witness = witnesses.get(requirement_id)
            indexes = command_indexes.get(requirement_id, [])
            if witness is None or len(indexes) < 2 or any(index not in positive_indexes for index in indexes[:2]):
                continue
            selected_files = [str(witness["path"])]
            probe = _level_probe_command(base_command, selected_files)
            if probe is None:
                continue
            started_ns = time.time_ns()
            counter_result = _contained_snapshot_command(
                counter_config, counter_root, _snapshot_command(probe, root, counter_root),
                denied_root=root,
            )
            if counter_result.get("containment_unavailable"):
                continue
            counter_output = str(counter_result.get("stdout") or "") + "\n" + str(counter_result.get("stderr") or "")
            preservation = requirement.get("kind") == "behavior_preservation"
            assertion_failure = bool(re.search(
                r"AssertionError|\bFAIL(?:ED)?\b|\bfailures?=\d+|^FAIL:",
                counter_output, re.I | re.M,
            ))
            setup_failure = bool(re.search(
                r"ERROR collecting|ModuleNotFoundError|ImportError|SyntaxError|"
                r"command not found|is not recognized|no tests? (?:ran|collected)",
                counter_output, re.I,
            ))
            if not preservation and (
                counter_result.get("exit_code") == 0 or not assertion_failure or setup_failure
            ):
                continue
            if preservation and counter_result.get("exit_code") not in {0, None}:
                continue
            cover_dir = current_root / ".nexus-verification" / ("coverage-" + requirement_id)
            trace_command = _trace_probe_command(
                _snapshot_command(base_command, root, current_root),
                selected_files, cover_dir, current_root,
            )
            if trace_command is None:
                continue
            trace_result = _contained_snapshot_command(
                current_config, current_root, trace_command, denied_root=root,
            )
            coverage_hits = _coverage_hits_changed(cover_dir, production_paths)
            if trace_result.get("exit_code") != 0 or not coverage_hits:
                continue
            semantic_trace = witness.get("semantic_trace", {})
            component = str(semantic_trace.get("production_component") or "").casefold()
            if not component or not any(
                Path(relative).stem.casefold() == component for relative in coverage_hits
            ):
                continue
            native_test_id = str(semantic_trace.get("native_test_id") or "")
            if native_test_id and not re.search(
                re.escape(native_test_id).replace(r"\ ", r"[ _-]+"),
                counter_output, re.I,
            ):
                continue
            joined_command = " ".join(
                str(one).replace("\\", "/").casefold() for one in base_command
            )
            framework = next((
                name for name in ("playwright", "vitest", "jest")
                if name in joined_command
            ), "go" if re.search(r"(?:^|[/ ])go(?:\.exe)?\s+test\b", joined_command) else "python")
            callable_identity: dict[str, Any] | None = None
            if framework == "python":
                runtime_output = cover_dir / "callable-events.json"
                runtime_command = _python_runtime_trace_command(
                    _snapshot_command(base_command, root, current_root),
                    selected_files, runtime_output, current_root,
                )
                if runtime_command is None:
                    continue
                runtime_result = _contained_snapshot_command(
                    current_config, current_root, runtime_command, denied_root=root,
                )
                try:
                    runtime_payload = json.loads(runtime_output.read_text(
                        encoding="utf-8", errors="strict",
                    ))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    continue
                events = runtime_payload.get("events", []) if isinstance(runtime_payload, dict) else []
                callable_identity = _runtime_callable_identity(
                    current_root, counter_root, production_paths,
                    semantic_trace, events if isinstance(events, list) else [],
                    str(witness.get("scenario", {}).get("predicate") or ""), goal,
                )
                if runtime_result.get("exit_code") != 0 or callable_identity is None:
                    continue
            elif framework in {"playwright", "vitest", "jest"}:
                callable_identity = _v8_runtime_callable_identity(
                    cover_dir, current_root, production_paths, semantic_trace,
                )
                if callable_identity is None:
                    continue
            else:
                # Coverage without exact callable/observable identity is useful
                # diagnostics but cannot autonomously prove behavior.
                continue
            scenario = witness.get("scenario", {})
            if not isinstance(scenario, dict):
                continue
            frozen_target = _frozen_acceptance_target(
                current_root, counter_root, production_paths, witness, goal, changed,
            )
            if frozen_target is None:
                continue
            direct_probe = _run_direct_acceptance_probe(
                config, current_root, counter_root, frozen_target, scenario,
            )
            if direct_probe is None:
                continue
            test_digest = file_sha256(confined_path(root, str(witness["path"]), allow_missing=False))
            command_digest = hashlib.sha256(json.dumps(
                probe, separators=(",", ":"), ensure_ascii=False,
            ).encode("utf-8")).hexdigest()
            executable = Path(probe[0]) if probe else Path()
            toolchain_digest = file_sha256(executable) if executable.is_file() else hashlib.sha256(
                str(probe[0] if probe else "").encode("utf-8")
            ).hexdigest()
            adapter_name = (
                f"node-v8-{framework}-causal-v1"
                if framework in {"playwright", "vitest", "jest"}
                else "go-cover-causal-v1" if framework == "go"
                else "python-trace-causal-v1"
            )
            receipt = {
                "schema_version": 1,
                "adapter": adapter_name,
                "adapter_digest": hashlib.sha256(adapter_name.encode("utf-8")).hexdigest(),
                "session_id": session_id,
                "run_nonce": run_nonce,
                "goal_sha256": requirement_contract.get("goal_sha256"),
                "requirement_id": requirement_id,
                "goal_spec_digest": requirement_contract.get("goal_spec_digest"),
                "acceptance_scenario": copy.deepcopy(witness.get("scenario")),
                "semantic_test_trace": copy.deepcopy(witness.get("semantic_trace")),
                "runtime_callable_identity": callable_identity,
                "frozen_target": frozen_target,
                "direct_acceptance_probe": direct_probe,
                "requirement_digest": hashlib.sha256(json.dumps(
                    requirement, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                ).encode("utf-8")).hexdigest(),
                "root_identity": hashlib.sha256(str(root.resolve()).casefold().encode("utf-8")).hexdigest(),
                "current_tree_merkle": current_merkle,
                "production_content": current_production,
                "baseline_production_content": baseline_production,
                "test_content": {str(witness["path"]): test_digest},
                "selected_test_files": selected_files,
                "command_digest": command_digest,
                "command_approval_digest": _command_approval_digest(root, [probe]),
                "toolchain_digest": toolchain_digest,
                "current_result_sha256": [
                    hashlib.sha256((
                        str(results[index].get("stdout") or "") + "\n"
                        + str(results[index].get("stderr") or "")
                    ).encode("utf-8")).hexdigest()
                    for index in indexes[:2]
                ],
                "counterfactual_exit_code": counter_result.get("exit_code"),
                "counterfactual_output_sha256": hashlib.sha256(counter_output.encode("utf-8")).hexdigest(),
                "counterfactual_assertion_failure": assertion_failure,
                "coverage_hits": coverage_hits,
                "started_ns": started_ns,
                "finished_ns": time.time_ns(),
            }
            receipt["receipt_digest"] = hashlib.sha256(json.dumps(
                receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            ).encode("utf-8")).hexdigest()
            receipts.append(receipt)
    return receipts


def _executed_requirement_evidence(
    contract: dict[str, Any], commands: list[list[str]], results: list[dict[str, Any]],
    analysis: dict[str, Any], project: dict[str, Any], command_levels: dict[int, str],
    changed: list[str],
    evidence_contracts: list[dict[str, Any]] | None = None,
    causal_receipts: list[dict[str, Any]] | None = None,
    root: Path | None = None,
    verification_session_id: str = "",
) -> dict[str, Any]:
    positive_indexes = {
        int(one["index"]) for one in analysis.get("verification_evidence", [])
        if isinstance(one, dict) and isinstance(one.get("index"), int)
    }
    requested_levels = next((
        list(one.get("requested_levels", [])) for one in contract.get("requirements", [])
        if isinstance(one, dict) and one.get("id") == "tests"
    ), [])
    proven_levels = {level for index, level in command_levels.items() if index in positive_indexes}
    contracts = evidence_contracts if isinstance(evidence_contracts, list) else project.get("test_evidence_contracts", [])
    if isinstance(contracts, list):
        for item in contracts:
            if not isinstance(item, dict) or item.get("command") not in commands:
                continue
            index = commands.index(item["command"])
            if index in positive_indexes:
                proven_levels.update(
                    str(level) for level in item.get("levels", [])
                    if str(level) in {"unit", "API", "E2E"}
                )
    for index in positive_indexes:
        combined = " ".join(commands[index]) + "\n" + str(results[index].get("stdout", "")) + "\n" + str(results[index].get("stderr", ""))
        for level, pattern in (
            ("E2E", r"(?:^|[/_. -])e2e(?:[/_. -]|$)|end[- ]to[- ]end"),
            ("API", r"(?:^|[/_. -])api(?:[/_. -]|$)"),
            ("unit", r"(?:^|[/_. -])unit(?:[/_. -]|$)"),
        ):
            if re.search(pattern, combined, re.I):
                proven_levels.add(level)
    if len(requested_levels) == 1 and positive_indexes:
        proven_levels.add(requested_levels[0])

    def json_field(value: object, dotted: str) -> object:
        current = value
        for part in dotted.split("."):
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return current

    causal_receipts = causal_receipts if isinstance(causal_receipts, list) else []

    def valid_receipt(receipt: object) -> bool:
        if not isinstance(receipt, dict) or receipt.get("schema_version") != 1:
            return False
        claimed = str(receipt.get("receipt_digest") or "")
        unsigned = {key: value for key, value in receipt.items() if key != "receipt_digest"}
        actual = hashlib.sha256(json.dumps(
            unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        if claimed != actual or receipt.get("goal_sha256") != contract.get("goal_sha256"):
            return False
        if root is None:
            return False
        if verification_session_id and receipt.get("session_id") != verification_session_id:
            return False
        requirement = next((
            one for one in contract.get("requirements", [])
            if isinstance(one, dict) and one.get("id") == receipt.get("requirement_id")
        ), None)
        if requirement is None:
            return False
        requirement_digest = hashlib.sha256(json.dumps(
            requirement, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        if receipt.get("requirement_digest") != requirement_digest:
            return False
        if receipt.get("goal_spec_digest") != contract.get("goal_spec_digest"):
            return False
        scenario = _acceptance_scenario(requirement)
        if scenario is None or receipt.get("acceptance_scenario") != scenario:
            return False
        frozen_target = receipt.get("frozen_target")
        direct_probe = receipt.get("direct_acceptance_probe")
        if not isinstance(frozen_target, dict) or not isinstance(direct_probe, dict):
            return False
        unsigned_target = {
            key: value for key, value in frozen_target.items() if key != "target_digest"
        }
        if frozen_target.get("target_digest") != hashlib.sha256(json.dumps(
            unsigned_target, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")).hexdigest():
            return False
        unsigned_probe = {
            key: value for key, value in direct_probe.items() if key != "direct_probe_digest"
        }
        if direct_probe.get("direct_probe_digest") != hashlib.sha256(json.dumps(
            unsigned_probe, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")).hexdigest():
            return False
        current_observations = direct_probe.get("current_observations", [])
        baseline_observation = direct_probe.get("baseline_observation", {})
        if (
            direct_probe.get("target") != frozen_target
            or direct_probe.get("scenario") != scenario
            or not isinstance(current_observations, list)
            or len(current_observations) != 2
            or not all(isinstance(one, dict) and one.get("passed") is True for one in current_observations)
            or not isinstance(baseline_observation, dict)
            or baseline_observation.get("passed") is True
        ):
            return False
        trace = receipt.get("semantic_test_trace")
        if not isinstance(trace, dict) or trace.get("provenance") not in {
            "runtime_call_observable_native_assertion",
            "engine_browser_route_dom_observable",
        }:
            return False
        unsigned_trace = {key: value for key, value in trace.items() if key != "trace_digest"}
        if trace.get("trace_digest") != hashlib.sha256(json.dumps(
            unsigned_trace, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")).hexdigest():
            return False
        runtime_identity = receipt.get("runtime_callable_identity")
        if not isinstance(runtime_identity, dict):
            return False
        unsigned_identity = {
            key: value for key, value in runtime_identity.items()
            if key != "identity_digest"
        }
        if runtime_identity.get("identity_digest") != hashlib.sha256(json.dumps(
            unsigned_identity, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")).hexdigest():
            return False
        identity_path = str(runtime_identity.get("path") or "")
        if not identity_path or identity_path not in receipt.get("production_content", {}):
            return False
        if identity_path.casefold().endswith(".py"):
            catalog = _python_callable_catalog(
                confined_path(root, identity_path, allow_missing=False)
            )
            identity = next((
                value for value in catalog.values()
                if value.get("qualname") == runtime_identity.get("qualname")
            ), None)
            if not isinstance(identity, dict) or (
                identity.get("ast_sha256") != runtime_identity.get("ast_sha256")
                or identity.get("firstlineno") != runtime_identity.get("firstlineno")
            ):
                return False
        expected_root = hashlib.sha256(str(root.resolve()).casefold().encode("utf-8")).hexdigest()
        if receipt.get("root_identity") != expected_root:
            return False
        adapter = str(receipt.get("adapter") or "")
        if receipt.get("adapter_digest") != hashlib.sha256(adapter.encode("utf-8")).hexdigest():
            return False
        matching_indexes = [
            index for index, command in enumerate(commands)
            if hashlib.sha256(json.dumps(
                command, separators=(",", ":"), ensure_ascii=False,
            ).encode("utf-8")).hexdigest() == receipt.get("command_digest")
        ]
        if len(matching_indexes) < 2:
            return False
        observed_outputs = [
            hashlib.sha256((
                str(results[index].get("stdout") or "") + "\n"
                + str(results[index].get("stderr") or "")
            ).encode("utf-8")).hexdigest()
            for index in matching_indexes
        ]
        claimed_outputs = receipt.get("current_result_sha256", [])
        if not isinstance(claimed_outputs, list) or any(
            str(one) not in observed_outputs for one in claimed_outputs
        ) or len(claimed_outputs) != 2:
            return False
        command = commands[matching_indexes[0]]
        if receipt.get("command_approval_digest") != _command_approval_digest(root, [command]):
            return False
        executable = Path(command[0]) if command else Path()
        toolchain_digest = file_sha256(executable) if executable.is_file() else hashlib.sha256(
            str(command[0] if command else "").encode("utf-8")
        ).hexdigest()
        if receipt.get("toolchain_digest") != toolchain_digest:
            return False
        current_merkle, _manifest = _project_tree_merkle(root)
        if receipt.get("current_tree_merkle") != current_merkle:
            return False
        for field in ("production_content", "test_content"):
            values = receipt.get(field, {})
            if not isinstance(values, dict):
                return False
            for relative, expected in values.items():
                try:
                    current = file_sha256(confined_path(root, str(relative), allow_missing=True))
                except (HarnessError, OSError):
                    return False
                if current != expected:
                    return False
        if set(receipt.get("selected_test_files", [])) != set(receipt.get("test_content", {})):
            return False
        if not set(receipt.get("coverage_hits", [])) & set(receipt.get("production_content", {})):
            return False
        return True

    causal_receipts = [one for one in causal_receipts if valid_receipt(one)]
    proven_behaviors: set[str] = {
        str(one.get("requirement_id")) for one in causal_receipts
        if isinstance(one, dict)
        and one.get("schema_version") == 1
        and isinstance(one.get("receipt_digest"), str)
    }
    requirements_by_id = {
        str(one.get("id")): one for one in contract.get("requirements", [])
        if isinstance(one, dict)
    }
    for index in positive_indexes:
        matched = next((
            item for item in contracts
            if isinstance(item, dict) and item.get("command") == commands[index]
        ), None)
        probes = matched.get("requirement_probes", {}) if isinstance(matched, dict) else {}
        if not isinstance(probes, dict) or not probes:
            continue
        try:
            report = json.loads(str(results[index].get("stdout") or ""))
        except (json.JSONDecodeError, UnicodeError):
            continue
        for requirement_id, field in probes.items():
            specification = field if isinstance(field, dict) else {"field": str(field)}
            value = json_field(report, str(specification.get("field") or ""))
            if value is True or (
                isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
            ):
                requirement = requirements_by_id.get(str(requirement_id), {})
                if requirement.get("requires_goal_binding"):
                    # Goal-bound behavior is accepted only through an
                    # engine-observed causal receipt. Project stdout cannot
                    # self-assert hashes/booleans into completion.
                    continue
                proven_behaviors.add(str(requirement_id))
    unmet: list[str] = []
    if requested_levels and any(level not in proven_levels for level in requested_levels):
        unmet.append("tests:" + ",".join(level for level in requested_levels if level not in proven_levels))
    for requirement in contract.get("requirements", []):
        if isinstance(requirement, dict) and requirement.get("kind") in {"behavior", "behavior_preservation"}:
            requirement_id = str(requirement.get("id") or "behavior")
            if requirement_id not in proven_behaviors:
                unmet.append(requirement_id)
    return {
        "passed": not unmet,
        "unmet": unmet,
        "proven_test_levels": sorted(proven_levels),
        "positive_command_indexes": sorted(positive_indexes),
        "behavior_proof": bool(proven_behaviors),
        "proven_behavior_requirements": sorted(proven_behaviors),
        "causal_receipts": causal_receipts,
    }


def _run_selected_project_verification(
    config: LoadedConfig,
    root: Path,
    project: dict[str, Any],
    goal: str,
    changed: list[str],
    progress: Progress | None,
    required_effect_paths: list[str] | None = None,
    deadline: Deadline | None = None,
    requirement_contract: dict[str, Any] | None = None,
    verification_session_id: str = "",
    read_only_baseline_merkle: str = "",
    transaction_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Run deterministic checks in the selected project, never the Harness checkout."""

    commands, source = _verification_commands(config, root, project)
    requirement_contract = requirement_contract or _derive_requirement_contract(
        root, goal, required_effect_paths
    )
    requirement_artifacts = _requirement_artifact_evidence(
        root, requirement_contract, changed
    )
    effect = _goal_effect_evidence(root, goal, changed, required_effect_paths)
    if not effect["passed"]:
        return {
            "status": "failed", "basis": "goal_effect", "commands": [],
            "runnable_tests": {}, "preflight": {}, "goal_effect": effect,
            "requirement_contract": requirement_contract,
            "requirement_evidence": requirement_artifacts,
            "reason": effect["reason"],
        }
    if requirement_contract.get("intent") == "read_only":
        current_merkle, current_manifest = _project_tree_merkle(root)
        if read_only_baseline_merkle and current_merkle != read_only_baseline_merkle:
            return {
                "status": "failed", "basis": "read_only_tree_drift", "commands": [],
                "runnable_tests": {}, "preflight": {}, "goal_effect": effect,
                "requirement_contract": requirement_contract,
                "reason": (
                    "Read-only verification detected selected-project tree drift; no project mutation "
                    "can be accepted for an informational goal."
                ),
                "current_tree_merkle": current_merkle,
                "current_tree_files": len(current_manifest),
            }
        return {
            "status": "passed", "basis": "read_only_zero_write", "commands": [],
            "runnable_tests": {}, "preflight": {}, "goal_effect": effect,
            "requirement_contract": requirement_contract,
            "verification_session_id": verification_session_id,
            "current_tree_merkle": current_merkle,
            "reason": "Read-only completion was proven without executing project code or changing the project tree.",
        }
    artifact_kinds = {
        str(one.get("id")): str(one.get("kind"))
        for one in requirement_contract.get("requirements", []) if isinstance(one, dict)
    }
    if requirement_artifacts.get("protected_violations"):
        return {
            "status": "failed", "basis": "protected_path", "commands": [],
            "runnable_tests": {}, "preflight": {}, "goal_effect": effect,
            "requirement_contract": requirement_contract,
            "requirement_evidence": requirement_artifacts,
            "reason": "Protected/read-only paths were changed: "
            + ", ".join(requirement_artifacts["protected_violations"]),
        }
    hard_artifact_unmet = [
        one for one in requirement_artifacts["unmet"]
        if artifact_kinds.get(one) != "tests"
    ]
    if hard_artifact_unmet:
        return {
            "status": "failed", "basis": "requirement_contract", "commands": [],
            "runnable_tests": {}, "preflight": {}, "goal_effect": effect,
            "requirement_contract": requirement_contract,
            "requirement_evidence": requirement_artifacts,
            "reason": (
                "Whole-goal artifact requirements remain unproven: "
                + ", ".join(hard_artifact_unmet)
            ),
        }
    test_goal = _is_test_goal(goal)
    static = _runnable_tests_in_changed_files(root, changed)
    preflight = _test_preflight(root, changed)
    missing_levels = _missing_requested_test_levels(goal, changed, root)
    preflight["missing_requested_test_levels"] = missing_levels
    if test_goal and missing_levels:
        return {
            "status": "failed", "basis": "test_requirement_coverage", "commands": [],
            "runnable_tests": static, "preflight": preflight,
            "reason": "No changed runnable-test artifact covers required level(s): " + ", ".join(missing_levels),
        }
    if test_goal and preflight["test_file_count"] == 0:
        return {
            "status": "failed", "basis": "test_preflight", "commands": [],
            "runnable_tests": static, "preflight": preflight,
            "reason": "The goal requires tests, but the selected project contains no test files.",
        }
    if test_goal and preflight["missing_dependencies"]:
        return {
            "status": "failed", "basis": "test_preflight", "commands": [],
            "runnable_tests": static, "preflight": preflight,
            "reason": "Test dependencies are missing: " + ", ".join(preflight["missing_dependencies"]),
        }
    if test_goal and preflight["false_green"]:
        return {
            "status": "failed", "basis": "test_preflight", "commands": [],
            "runnable_tests": static, "preflight": preflight,
            "reason": preflight["false_green"],
        }
    if test_goal and effect["effect_required"] and static["runnable"] == 0:
        return {
            "status": "failed", "basis": "static_runnable_test_gate",
            "commands": [], "runnable_tests": static, "preflight": preflight,
            "reason": "The run changed test files but created zero runnable, non-skipped test cases.",
        }
    if not commands:
        return {
            "status": "unavailable", "basis": source, "commands": [],
            "runnable_tests": static, "preflight": preflight,
            "reason": "No deterministic test command is configured or discoverable for the selected project.",
        }
    if source == "discovered":
        try:
            approval_digest = _command_approval_digest(
                root,
                commands,
                declared_path=str(project.get("path") or ""),
            )
        except (OSError, RuntimeError) as exc:
            return {
                "status": "unavailable", "basis": "command_approval_fingerprint_unavailable",
                "commands": [], "proposed_commands": commands,
                "runnable_tests": static, "preflight": preflight, "goal_effect": effect,
                "reason": (
                    "Nexus did not run project code because it could not safely fingerprint "
                    f"the exact discovered test commands: {exc}"
                ),
            }
        approved = str(project.get("approved_test_command_digest") or "")
        if approved != approval_digest:
            return {
                "status": "unavailable", "basis": "discovered_command_approval_required",
                "commands": [], "proposed_commands": commands,
                "approval_digest": approval_digest, "runnable_tests": static,
                "preflight": preflight, "goal_effect": effect,
                "reason": (
                    "Nexus discovered project test commands but did not execute untrusted project code. "
                    "Open the gear on this project, review Project test commands, and approve "
                    "the exact displayed fingerprint; or configure selected-project test commands explicitly."
                ),
            }

    base_commands = list(commands)
    command_levels: dict[int, str] = {}
    behavior_command_indexes: dict[str, list[int]] = {}
    behavior_requirements = [
        one for one in requirement_contract.get("requirements", [])
        if isinstance(one, dict)
        and one.get("kind") in {"behavior", "behavior_preservation"}
        and one.get("acceptance_terms")
    ]
    behavior_witnesses = _behavior_test_witnesses(root, changed, behavior_requirements)
    requested_levels = _requested_test_levels(goal)
    if source != "discovered" and base_commands:
        for level in requested_levels:
            files = list(static.get("levels", {}).get(level, {}).get("files", []))
            probe = _level_probe_command(base_commands[0], files)
            if probe is not None and probe not in commands:
                command_levels[len(commands)] = level
                commands.append(probe)
    if base_commands:
        for requirement in behavior_requirements:
            identifier = str(requirement.get("id") or "")
            witness = behavior_witnesses.get(identifier)
            if witness is None:
                continue
            probe = _level_probe_command(base_commands[0], [str(witness["path"])])
            if probe is None:
                continue
            # Two fresh current-snapshot passes are required. They are kept as
            # distinct observations even though argv is identical.
            behavior_command_indexes[identifier] = [len(commands), len(commands) + 1]
            commands.extend([list(probe), list(probe)])

    command_config = LoadedConfig(
        copy.deepcopy(config.data), root.resolve(), list(config.sources),
        dict(config.provenance), copy.deepcopy(config.trusted_floor),
    )
    verification_origin_merkle, _verification_origin_manifest = _project_tree_merkle(root)
    results: list[dict[str, Any]] = []
    for command in commands:
        executable_text = str(command[0]) if command else ""
        executable_path = Path(executable_text) if executable_text else Path()
        executable_found = _containment_owns_runner_availability(command) or (
            executable_path.is_file()
            if executable_path.is_absolute() or executable_path.parent != Path(".")
            else shutil.which(executable_text) is not None
        )
        if not executable_found:
            return {
                "status": "unavailable", "basis": "missing_runner", "commands": results,
                "runnable_tests": static, "preflight": preflight, "goal_effect": effect,
                "reason": "The selected test runner executable is missing or could not be started: "
                + (executable_text or "(empty command)"),
            }
        _report(
            progress,
            "Running deterministic selected-project verification",
            "Nexus is running: " + " ".join(command),
        )
        try:
            timeout = None
            deadline_limited = False
            if deadline is not None:
                configured_timeout = float(
                    command_config.get("execution.timeout_seconds")
                )
                remaining = deadline.remaining_seconds(
                    "before a selected-project verification command",
                    configured_timeout,
                )
                timeout = remaining
                deadline_limited = (
                    deadline.limits(configured_timeout)
                    if isinstance(deadline, _SwarmToolExecutionBudget)
                    else remaining <= configured_timeout
                )
            payload = _run_disposable_verification_command(
                command_config, root, command, timeout=timeout,
            )
            if payload.get("timed_out") and deadline_limited:
                raise ContextToolBudgetExhausted(
                    "Project context-tool execution budget exhausted during selected-project verification"
                )
            if deadline is not None:
                deadline.check("during selected-project verification")
        except cancellation.ChatCancelled:
            raise
        except DeadlineExpired:
            raise
        except (HarnessError, OSError) as exc:
            payload = {
                "argv": command, "cwd": ".", "exit_code": -1,
                "stdout": "", "stderr": str(exc), "duration_ms": 0,
                "timed_out": False, "output_truncated": False,
            }
        results.append(payload)
        if payload.get("containment_unavailable"):
            return {
                "status": "unavailable", "basis": "verification_containment_unavailable",
                "commands": results, "runnable_tests": static,
                "preflight": preflight, "goal_effect": effect,
                "reason": (
                    "Nexus did not execute the selected-project command because this runtime "
                    "has no supported write-containment profile. The applied work remains "
                    "resumable and needs verification."
                ),
            }
        if payload.get("verification_escape_detected"):
            return {
                "status": "failed", "basis": "verification_escape_detected",
                "commands": results, "runnable_tests": static,
                "preflight": preflight, "goal_effect": effect,
                "reason": (
                    "A selected-project verification process attempted to write to the real project "
                    "outside its disposable snapshot. Nexus restored the frozen project surface and "
                    "refused the evidence."
                ),
            }
        after_command_merkle, _after_command_manifest = _project_tree_merkle(root)
        if after_command_merkle != verification_origin_merkle:
            return {
                "status": "failed", "basis": "verification_escape_detected",
                "commands": results, "runnable_tests": static,
                "preflight": preflight, "goal_effect": effect,
                "reason": (
                    "A selected-project verification process changed the real project outside its "
                    "disposable snapshot; Nexus refused the evidence."
                ),
            }
        combined = str(payload.get("stdout") or "") + "\n" + str(payload.get("stderr") or "")
        if "Nexus verification containment denied" in combined:
            return {
                "status": "failed", "basis": "verification_containment_denied",
                "commands": results, "runnable_tests": static,
                "preflight": preflight, "goal_effect": effect,
                "reason": (
                    "A selected-project command attempted an operation outside its disposable "
                    "verification authority. Nexus denied the operation before accepting evidence."
                ),
            }
        if payload.get("exit_code") == -1 and isinstance(payload.get("stderr"), str):
            return {
                "status": "unavailable", "basis": "missing_runner", "commands": results,
                "runnable_tests": static, "preflight": preflight, "goal_effect": effect,
                "reason": "The selected test runner executable is missing or could not be started: "
                + str(payload.get("stderr")),
            }
        if re.search(
            r"(?:no module named|module not found|cannot find module|command not found|is not recognized)",
            combined, re.IGNORECASE,
        ):
            return {
                "status": "failed", "basis": "missing_test_dependency", "commands": results,
                "runnable_tests": static, "preflight": preflight, "goal_effect": effect,
                "reason": "A selected-project test dependency is missing: " + combined.strip()[:2000],
            }
        passed = payload.get("exit_code") == 0 and not payload.get("timed_out")
        if _EMPTY_TEST_OUTPUT.search(combined):
            passed = False
            payload["false_green_reason"] = "The command reported that zero/no tests ran."
        if not passed:
            return {
                "status": "failed", "basis": source, "commands": results,
                "runnable_tests": static, "preflight": preflight,
                "reason": "A deterministic selected-project test command failed or ran zero tests.",
            }
    contracts = project.get("test_evidence_contracts", [])
    if not isinstance(contracts, list):
        contracts = []
    if not contracts and config.project_root.resolve() == root.resolve():
        configured_contracts = config.get("project.test_evidence_contracts", [])
        if isinstance(configured_contracts, list):
            contracts = configured_contracts
    positive = analyze_verification(
        commands, results, test_indexes=set(range(len(commands))),
        evidence_contracts=contracts,
    )
    if not positive["passed"]:
        return {
            "status": "failed", "basis": "positive_test_evidence",
            "commands": results, "runnable_tests": static,
            "preflight": preflight, "goal_effect": effect,
            "verification_analysis": positive,
            "reason": (
                "Selected-project commands exited successfully but did not provide positive, framework-aware evidence "
                "that one or more tests executed."
            ),
        }
    positive_indexes = {
        int(one["index"]) for one in positive.get("verification_evidence", [])
        if isinstance(one, dict) and isinstance(one.get("index"), int)
    }
    causal_receipts = _build_causal_behavior_receipts(
        command_config, root, goal, requirement_contract, changed,
        list(transaction_ids or []), base_commands[0] if base_commands else None,
        behavior_command_indexes, commands, results, positive_indexes,
        behavior_witnesses, verification_session_id or uuid.uuid4().hex,
    )
    executed_requirements = _executed_requirement_evidence(
        requirement_contract, commands, results, positive, project, command_levels,
        changed,
        contracts,
        causal_receipts,
        root,
        verification_session_id,
    )
    if not executed_requirements["passed"]:
        return {
            "status": "failed", "basis": "requirement_execution_evidence",
            "commands": results, "runnable_tests": static,
            "preflight": preflight, "goal_effect": effect,
            "verification_analysis": positive,
            "requirement_contract": requirement_contract,
            "requirement_evidence": {
                "artifacts": requirement_artifacts,
                "execution": executed_requirements,
            },
            "reason": (
                "Selected-project verification did not independently prove every requested test level or behavior: "
                + ", ".join(executed_requirements["unmet"])
            ),
        }
    return {
        "status": "passed", "basis": source, "commands": results,
        "runnable_tests": static, "preflight": preflight,
        "goal_effect": effect,
        "verification_analysis": positive,
        "requirement_contract": requirement_contract,
        "requirement_evidence": {
            "artifacts": requirement_artifacts,
            "execution": executed_requirements,
        },
        "reason": "All deterministic selected-project test commands passed with goal-effect evidence.",
    }


def _review_correlation(
    plans: list[tuple[dict[str, Any], dict[str, Any]]],
    config: LoadedConfig | None = None,
) -> dict[str, Any]:
    identities: dict[tuple[str, str], list[str]] = {}
    for agent, value in plans:
        route = str(agent.get("who") or "").casefold()
        profile = {}
        if config is not None:
            providers = config.data.get("providers", {})
            if isinstance(providers, dict) and isinstance(providers.get(route), dict):
                profile = providers[route]
        transport = "|".join(str(profile.get(key) or "").casefold() for key in (
            "kind", "model", "command", "endpoint",
        )).strip("|")
        identity = (
            transport or route,
            str(value.get("_model") or profile.get("model") or "").casefold(),
        )
        identities.setdefault(identity, []).append(str(agent.get("name") or "An agent"))
    duplicates = [
        {"route": route, "transport_identity": route, "model": model, "agents": names}
        for (route, model), names in identities.items() if route and len(names) > 1
    ]
    return {
        "independent": not duplicates,
        "duplicates": duplicates,
        "warning": (
            "Reviewer correlation: multiple agent seats use the same provider route/model; "
            "their agreement is not independent evidence."
            if duplicates else "No exact resolved provider transport/model duplicate was detected; this does not prove statistical independence."
        ),
    }


def _project_state_digest(root: Path, changed: list[str]) -> str:
    state: list[tuple[str, str | None]] = []
    for relative in sorted(set(changed)):
        try:
            path = confined_path(root, relative)
            state.append((relative, file_sha256(path)))
        except HarnessError:
            state.append((relative, None))
    return hashlib.sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _delta_path_matches_requirement(
    root: Path, contract: dict[str, Any], relative: str,
) -> bool:
    """Whether one real file delta advances an explicit goal requirement."""

    folded = relative.replace("\\", "/").casefold()
    all_requirements = [
        one for one in contract.get("requirements", []) if isinstance(one, dict)
    ]
    has_specific = any(
        str(one.get("kind") or "project_effect") != "project_effect"
        for one in all_requirements
    )
    for requirement in all_requirements:
        kind = str(requirement.get("kind") or "project_effect")
        if kind in {"behavior", "behavior_preservation"}:
            # Behavior is proved by deterministic execution, never inferred
            # from file churn alone.
            continue
        roots = [str(one) for one in requirement.get("effect_roots", []) if isinstance(one, str)]
        paths = [str(one) for one in requirement.get("effect_paths", []) if isinstance(one, str)]
        scoped = (
            (not roots or _path_is_under(relative, roots))
            and (not paths or any(
                folded == path.replace("\\", "/").casefold()
                or folded.startswith(path.replace("\\", "/").casefold().rstrip("/") + "/")
                for path in paths
            ))
        )
        if (roots or paths) and scoped:
            return True
        if roots or paths:
            continue
        if kind == "project_effect":
            if not has_specific:
                return True
            continue
        if kind == "tests":
            static = _runnable_tests_in_changed_files(root, [relative])
            if int(static.get("runnable", 0)) > 0:
                return True
        elif kind == "traceability" and re.search(
            r"trace?ability|tracibility|(?:^|[/_. -])trace(?:[/_. -]|$)|workbook|dashboard",
            folded, re.I,
        ):
            return True
        elif kind == "langgraph" and "langgraph" in folded:
            return True
        elif kind == "upload_bundle" and re.search(
            r"(?:^|[/_. -])(?:upload|bundle)(?:[/_. -]|$)", folded, re.I,
        ):
            return True
        elif kind == "commit_message" and folded.endswith(".md") and re.search(
            r"(?:^|[/_. -])(?:commit|message)(?:[/_. -]|$)", folded, re.I,
        ):
            return True
        elif kind == "durable_memory" and folded.endswith(".md") and re.search(
            r"(?:^|[/_. -])(?:obsidian|vault|memory)(?:[/_. -]|$)", folded, re.I,
        ):
            return True
        elif kind == "generic_artifact":
            if requirement.get("generic_file"):
                return True
            if all(
                re.search(rf"(?:^|[/_. -]){re.escape(str(term).casefold())}(?:[/_. -]|$)", folded, re.I)
                for term in requirement.get("artifact_terms", [])
            ):
                return True
    return False


def _verified_net_semantic_deltas(
    root: Path, contract: dict[str, Any], records: list[dict[str, Any]],
) -> list[dict[str, str | None]]:
    """Collapse applied manifests to live, relevant, non-reverted deltas."""

    net: dict[str, dict[str, str | None]] = {}
    order: list[str] = []
    digest = re.compile(r"[0-9a-f]{64}")
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("path"), str):
            continue
        relative = str(record["path"]).replace("\\", "/")
        before = record.get("before_sha256")
        after = record.get("after_sha256")
        if before is not None and (not isinstance(before, str) or not digest.fullmatch(before)):
            continue
        if after is not None and (not isinstance(after, str) or not digest.fullmatch(after)):
            continue
        if relative not in net:
            net[relative] = {"path": relative, "before_sha256": before, "after_sha256": after}
            order.append(relative)
        else:
            # A durable manifest chain must join exactly. Broken lineage is not
            # engine-owned semantic progress.
            if net[relative]["after_sha256"] != before:
                net[relative]["invalid"] = "lineage"  # type: ignore[assignment]
            net[relative]["after_sha256"] = after
    verified: list[dict[str, str | None]] = []
    for relative in order:
        item = net[relative]
        if item.get("invalid") or item["before_sha256"] == item["after_sha256"]:
            continue
        try:
            current = file_sha256(confined_path(root, relative, allow_missing=True))
        except (HarnessError, OSError):
            continue
        if current != item["after_sha256"]:
            continue
        if not _delta_path_matches_requirement(root, contract, relative):
            continue
        verified.append({
            "path": relative,
            "before_sha256": item["before_sha256"],
            "after_sha256": item["after_sha256"],
        })
    return verified


class _SwarmToolExecutionBudget:
    """Durable aggregate budget charged only while a context tool is executing.

    It deliberately has no absolute expiry timestamp. Provider thinking,
    network waits between tool calls, user pauses, and process downtime cannot
    spend this budget. A zero configured ceiling means unlimited aggregate tool
    time; each subprocess and MCP call still receives its ordinary local cap.
    """

    def __init__(
        self,
        configured_seconds: float = 0,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if (
            isinstance(configured_seconds, bool)
            or not isinstance(configured_seconds, (int, float))
            or not math.isfinite(float(configured_seconds))
            or float(configured_seconds) < 0
        ):
            raise HarnessError(
                "Project context-tool execution budget must be zero or a positive number"
            )
        self.configured_seconds = float(configured_seconds)
        self.consumed_seconds = 0.0
        self._clock = clock or time.monotonic
        self._active_started: float | None = None

    @property
    def unlimited(self) -> bool:
        return self.configured_seconds == 0

    def _consumed_now(self) -> float:
        active = (
            max(0.0, float(self._clock()) - self._active_started)
            if self._active_started is not None else 0.0
        )
        return self.consumed_seconds + active

    def _remaining(self) -> float:
        if self.unlimited:
            return math.inf
        return max(0.0, self.configured_seconds - self._consumed_now())

    def begin_tool_execution(self) -> None:
        if self._active_started is not None:
            raise HarnessError("Project context-tool execution accounting is already active")
        self.check("before a context tool call")
        self._active_started = float(self._clock())

    def finish_tool_execution(self) -> None:
        if self._active_started is None:
            return
        finished = float(self._clock())
        self.consumed_seconds += max(0.0, finished - self._active_started)
        self._active_started = None

    def check(self, operation: str) -> None:
        if self._remaining() <= 0:
            raise ContextToolBudgetExhausted(
                f"Project context-tool execution budget exhausted {operation}"
            )

    def remaining_seconds(self, operation: str, cap: float | None = None) -> float:
        self.check(operation)
        remaining = self._remaining()
        return remaining if cap is None else min(remaining, float(cap))

    def limits(self, cap: float) -> bool:
        """Whether the aggregate budget, rather than the local cap, is tighter."""

        return not self.unlimited and self._remaining() <= float(cap)

    def shorten(self, seconds: float) -> None:
        """Test/embedding hook: cap future active execution to this much more time."""

        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
            raise HarnessError(
                "Project context-tool execution budget must be greater than zero"
            )
        shortened = self._consumed_now() + float(seconds)
        self.configured_seconds = (
            shortened if self.unlimited else min(self.configured_seconds, shortened)
        )

    def budget_state(self) -> dict[str, Any]:
        consumed = self._consumed_now()
        remaining = None if self.unlimited else max(
            0.0, self.configured_seconds - consumed
        )
        return {
            "schema_version": 2,
            "accounting": "active_context_tool_execution_only",
            "configured_ceiling_seconds": self.configured_seconds,
            "consumed_seconds": consumed,
            "remaining_seconds": remaining,
            "unlimited": self.unlimited,
        }

    def restore_budget_state(self, state: object) -> None:
        if not isinstance(state, dict) or state.get("schema_version") != 2:
            raise HarnessError(
                "Run checkpoint context-tool execution budget has an unsupported schema"
            )
        consumed = state.get("consumed_seconds")
        prior_ceiling = state.get("configured_ceiling_seconds")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0
            for value in (consumed, prior_ceiling)
        ):
            raise HarnessError("Run checkpoint context-tool execution budget is invalid")
        if self._active_started is not None:
            raise HarnessError("Cannot restore context-tool execution budget during a call")
        # The authenticated ledger owns consumed time. The current setting owns
        # the ceiling, so a user can explicitly extend it or switch to zero
        # (unlimited) before resuming without erasing what was already charged.
        self.consumed_seconds = float(consumed)

    def reset_by_user(self) -> None:
        if self._active_started is not None:
            raise HarnessError("Cannot reset context-tool execution budget during a call")
        self.consumed_seconds = 0.0


class _ProjectContextTools:
    """Existing bounded agent-tool runtime adapted to one selected project."""

    def __init__(
        self, config: LoadedConfig, root: Path, ledger: CollaborationLedger,
        project: dict[str, Any], goal: str, changed: list[str], progress: Progress | None,
        required_effect_paths: list[str] | None = None,
        requirement_contract: dict[str, Any] | None = None,
        *,
        reset_execution_budget: bool = False,
    ) -> None:
        data = copy.deepcopy(config.data)
        # Long-horizon exploration gets a useful epoch rather than the generic
        # twelve-call conversational default.  Epochs remain bounded and are
        # renewable only by an engine-owned durable project-state transition.
        configured_calls = int(data.setdefault("workflow", {}).get("max_tool_calls", 48))
        configured_bytes = int(data["workflow"].get("max_tool_total_bytes", 512_000))
        data["workflow"]["max_tool_calls"] = 48 if configured_calls == 12 else configured_calls
        data["workflow"]["max_tool_total_bytes"] = (
            512_000 if configured_bytes == 128_000 else configured_bytes
        )
        data.setdefault("memory", {})["enabled"] = True
        data["memory"]["embedding_provider"] = ""
        data["memory"]["embedding_model"] = ""
        self.config = LoadedConfig(
            data, root.resolve(), list(config.sources), dict(config.provenance),
            copy.deepcopy(config.trusted_floor),
        )
        self.memory = MemoryStore(self.config)
        self.memory.ensure_external_run(
            ledger.session_id, "Nexus project-work context tools", "swarm-tools-v1"
        )
        self.execution_budget = _SwarmToolExecutionBudget(
            float(data["workflow"].get("context_tool_execution_seconds", 0))
        )
        self.ledger = ledger
        self.project = project
        self.goal = goal
        self.changed = changed
        self.progress = progress
        self.required_effect_paths = list(required_effect_paths or [])
        self.requirement_contract = copy.deepcopy(
            requirement_contract or _derive_requirement_contract(
                root, goal, required_effect_paths
            )
        )
        self.indexed = False
        self.epoch = 1
        self.lifetime_calls_before_epoch = 0
        self.lifetime_bytes_before_epoch = 0
        self.renewal_checkpoints: list[str] = []
        self.semantic_path_hashes: dict[str, str | None] = {}
        self.semantic_state_history: list[str] = []
        # Per-epoch call/output bounds remain finite and renew only after a
        # verified semantic project-state change. A second lifetime wall made
        # a genuinely progressing run terminal after enough good work, even
        # across resumes. Zero means no lifetime ceiling; unchanged retries
        # still cannot renew the finite epoch.
        self.absolute_call_limit = 0
        self.absolute_byte_limit = 0
        self.session = AgentToolSession(
            self.config, self.memory, self.execution_budget,
            lambda kind, node, payload: ledger.record_state(
                "context_tool_event", {"kind": kind, "node": node, "payload": payload}
            ),
            run_id=ledger.session_id,
            extra_read_only_tools={
                "run_selected_verification": self._run_selected_verification,
            },
            prepare_tool=self._prepare_tool,
        )
        self.epoch_byte_limit = self.session.total_bytes_limit
        prior_state = next((
            event.get("state", {})
            for event in reversed(ledger._read())
            if event.get("session_id") == ledger.session_id
            and event.get("phase") == "context_tool_budget"
        ), None)
        if isinstance(prior_state, dict) and isinstance(prior_state.get("budget"), dict):
            self.session.restore_budget_state(prior_state["budget"])
            execution_budget = prior_state.get("tool_execution_budget")
            if isinstance(execution_budget, dict):
                self.execution_budget.restore_budget_state(execution_budget)
            # Schema-1 checkpoints stored an absolute Unix expiry. They cannot
            # be converted into active execution time safely and, critically,
            # must not make a run permanently expired merely because the app
            # was closed. They migrate with zero consumed execution seconds.
            epoch_state = prior_state.get("epoch", {})
            if isinstance(epoch_state, dict):
                self.epoch = max(1, int(epoch_state.get("number", 1)))
                self.lifetime_calls_before_epoch = max(0, int(epoch_state.get("prior_calls", 0)))
                self.lifetime_bytes_before_epoch = max(0, int(epoch_state.get("prior_bytes", 0)))
                checkpoints = epoch_state.get("renewal_checkpoints", [])
                if isinstance(checkpoints, list):
                    self.renewal_checkpoints = [
                        str(one) for one in checkpoints
                        if re.fullmatch(r"[0-9a-f]{64}", str(one))
                    ]
                path_hashes = epoch_state.get("semantic_path_hashes", {})
                if isinstance(path_hashes, dict) and all(
                    isinstance(path, str)
                    and (value is None or bool(re.fullmatch(r"[0-9a-f]{64}", str(value))))
                    for path, value in path_hashes.items()
                ):
                    self.semantic_path_hashes = {
                        str(path): None if value is None else str(value)
                        for path, value in path_hashes.items()
                    }
                state_history = epoch_state.get("semantic_state_history", [])
                if isinstance(state_history, list):
                    self.semantic_state_history = [
                        str(one) for one in state_history
                        if re.fullmatch(r"[0-9a-f]{64}", str(one))
                    ]
        if reset_execution_budget:
            self.execution_budget.reset_by_user()
            self.ledger.record_state("context_tool_budget_reset", {
                "status": "reset_by_user",
                "configured_ceiling_seconds": self.execution_budget.configured_seconds,
                "recovery": (
                    "unlimited" if self.execution_budget.unlimited
                    else "a fresh configured execution allowance"
                ),
            })
            self._record_budget()
        _report(
            self.progress, "Project context-tool budget",
            self.disclosure()["summary"],
        )

    def close(self) -> None:
        self.memory.close()

    def _run_selected_verification(self, arguments: object) -> dict[str, Any]:
        if arguments not in ({}, None):
            raise HarnessError("run_selected_verification takes an empty arguments object")
        return _run_selected_project_verification(
            self.config, self.config.project_root, self.project,
            self.goal, self.changed, self.progress, self.required_effect_paths,
            self.execution_budget,
            self.requirement_contract,
            verification_session_id=self.ledger.session_id,
        )

    def _prepare_tool(self, name: str, _arguments: object, deadline: Deadline) -> None:
        if name == "search_workspace" and not self.indexed:
            WorkspaceIndexer(self.config, self.memory).scan(deadline)
            self.indexed = True

    def execute(self, node: str, call: dict[str, Any]) -> dict[str, Any]:
        call_id = str(call.get("call_id") or "")
        name = str(call.get("name") or "")
        arguments = call.get("arguments", {})
        if (
            self.absolute_call_limit > 0
            and self.lifetime_calls_before_epoch + self.session.calls >= self.absolute_call_limit
        ):
            raise HarnessError("Absolute long-horizon context-tool safety ceiling reached")
        # Bound this very result by the lifetime ceiling. AgentToolSession
        # accounts and exposes truncation before it persists/returns a result;
        # an absolute-ceiling truncation is rejected below rather than being
        # silently accepted as useful context.
        if self.absolute_byte_limit > 0:
            absolute_remaining_before = max(
                0,
                self.absolute_byte_limit
                - self.lifetime_bytes_before_epoch
                - self.session.total_bytes,
            )
            absolute_epoch_cap = max(
                0, self.absolute_byte_limit - self.lifetime_bytes_before_epoch
            )
            self.session.total_bytes_limit = min(
                self.epoch_byte_limit, absolute_epoch_cap
            )
            lifetime_limited_this_result = (
                absolute_remaining_before <= self.session.per_call_bytes
            )
        else:
            self.session.total_bytes_limit = self.epoch_byte_limit
            lifetime_limited_this_result = False
        self.execution_budget.begin_tool_execution()
        try:
            result = self.session.execute(node, call_id, name, arguments)
            if lifetime_limited_this_result and result.get("truncated"):
                self.ledger.record_state("context_tool_absolute_limit_rejected", {
                    "call_id": call_id,
                    "name": name,
                    "lifetime_bytes": self.lifetime_bytes_before_epoch + self.session.total_bytes,
                    "absolute_byte_limit": self.absolute_byte_limit,
                    "reason": "The current result exceeded the remaining absolute output budget.",
                })
                raise HarnessError(
                    "Absolute long-horizon context-tool output safety ceiling reached during this result"
                )
        except BaseException as original:
            self.execution_budget.finish_tool_execution()
            try:
                self._record_budget()
            except BaseException as checkpoint_error:
                original.add_note(
                    "Nexus also failed to persist the consumed context-tool budget: "
                    + str(checkpoint_error)
                )
            raise
        self.execution_budget.finish_tool_execution()
        self.ledger.record_state("context_tool_result", {
            "call_id": call_id,
            "name": name,
            "arguments_sha256": hashlib.sha256(
                json.dumps(arguments, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            "result": result,
        })
        self._record_budget()
        return result

    def _record_budget(self) -> None:
        self.ledger.record_state("context_tool_budget", {
            "budget": self.session.budget_state(),
            "tool_execution_budget": self.execution_budget.budget_state(),
            "epoch": {
                "number": self.epoch,
                "prior_calls": self.lifetime_calls_before_epoch,
                "prior_bytes": self.lifetime_bytes_before_epoch,
                "renewal_checkpoints": self.renewal_checkpoints,
                "semantic_path_hashes": self.semantic_path_hashes,
                "semantic_state_history": self.semantic_state_history,
                "renewal_policy": "only_after_durable_semantic_progress",
                "absolute_call_limit": self.absolute_call_limit,
                "absolute_byte_limit": self.absolute_byte_limit,
            },
        })

    @staticmethod
    def _semantic_state_digest(state: dict[str, str | None]) -> str:
        return hashlib.sha256(
            json.dumps(sorted(state.items()), separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _authenticated_manifest_changes(self, transaction_ids: list[str]) -> list[dict[str, Any]]:
        if not transaction_ids or not all(
            isinstance(one, str) and re.fullmatch(r"[0-9]+-[0-9a-f]{10}", one)
            for one in transaction_ids
        ):
            return []
        try:
            saga_path = confined_path(
                self.config.project_root,
                _MutationSaga.FOLDER / f"{self.ledger.session_id}.json",
                allow_control=True,
            )
            saga = json.loads(saga_path.read_text(encoding="utf-8"))
        except (HarnessError, OSError, json.JSONDecodeError):
            return []
        if (
            not isinstance(saga, dict)
            or saga.get("schema_version") != 1
            or saga.get("saga_id") != self.ledger.session_id
        ):
            return []
        entries = {
            str(one.get("transaction_id")): one
            for one in saga.get("transactions", [])
            if isinstance(one, dict) and one.get("phase") == "applied"
        }
        events = {
            str(event.get("state", {}).get("transaction_id")): event.get("state", {})
            for event in self.ledger._read()
            if event.get("phase") == "mutation_manifest_applied"
            and isinstance(event.get("state"), dict)
            and event.get("state", {}).get("saga_id") == self.ledger.session_id
        }
        changes: list[dict[str, Any]] = []
        for transaction_id in dict.fromkeys(transaction_ids):
            entry = entries.get(transaction_id)
            event = events.get(transaction_id)
            if not isinstance(entry, dict) or not isinstance(event, dict):
                return []
            try:
                manifest = FileTransaction(self.config.project_root).load_manifest(transaction_id)
            except HarnessError:
                return []
            digest = _manifest_sha256(manifest)
            if (
                manifest.get("state") != "applied"
                or entry.get("manifest_sha256") != digest
                or event.get("manifest_sha256") != digest
            ):
                return []
            records = manifest.get("changes", [])
            if not isinstance(records, list) or not all(isinstance(one, dict) for one in records):
                return []
            changes.extend(dict(one) for one in records)
        return changes

    def renew_after_progress(self, transaction_ids: list[str]) -> bool:
        manifest_changes = self._authenticated_manifest_changes(transaction_ids)
        deltas = _verified_net_semantic_deltas(
            self.config.project_root, self.requirement_contract, manifest_changes,
        )
        if not deltas:
            return False
        before_state = dict(self.semantic_path_hashes)
        for delta in deltas:
            path = str(delta["path"])
            before = delta.get("before_sha256")
            if path in before_state and before_state[path] != before:
                return False
            before_state.setdefault(path, before)
        before_digest = self._semantic_state_digest(before_state)
        after_state = dict(before_state)
        for delta in deltas:
            after_state[str(delta["path"])] = delta.get("after_sha256")
        after_digest = self._semantic_state_digest(after_state)
        if before_digest == after_digest or after_digest in self.semantic_state_history:
            return False
        checkpoint = hashlib.sha256(
            json.dumps(
                {"before": before_digest, "after": after_digest, "deltas": deltas},
                sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if checkpoint in self.renewal_checkpoints:
            return False
        self.lifetime_calls_before_epoch += self.session.calls
        self.lifetime_bytes_before_epoch += self.session.total_bytes
        if (
            self.absolute_call_limit > 0
            and self.lifetime_calls_before_epoch >= self.absolute_call_limit
        ):
            return False
        self.session.calls = 0
        self.session.total_bytes = 0
        self.epoch += 1
        self.renewal_checkpoints.append(checkpoint)
        if before_digest not in self.semantic_state_history:
            self.semantic_state_history.append(before_digest)
        self.semantic_state_history.append(after_digest)
        self.semantic_path_hashes = after_state
        self._record_budget()
        self.ledger.record_state("context_tool_epoch_renewed", {
            "epoch": self.epoch,
            "checkpoint_sha256": checkpoint,
            "changed": [str(one["path"]) for one in deltas],
            "before_state_sha256": before_digest,
            "after_state_sha256": after_digest,
            "delta_evidence": deltas,
            "renewal_policy": "only_after_durable_semantic_progress",
        })
        _report(self.progress, "Context exploration renewed after progress", self.disclosure()["summary"])
        return True

    def disclosure(self) -> dict[str, Any]:
        remaining = max(0, self.session.max_calls - self.session.calls)
        lifetime = self.lifetime_calls_before_epoch + self.session.calls
        execution = self.execution_budget.budget_state()
        execution_ceiling = float(execution["configured_ceiling_seconds"])
        execution_used = float(execution["consumed_seconds"])
        execution_remaining = execution["remaining_seconds"]
        execution_mode = "unlimited" if execution["unlimited"] else "configured"
        time_words = (
            f"Tool execution time is unlimited in aggregate; {execution_used:.3f} seconds "
            "have been charged so far"
            if execution["unlimited"] else
            f"{float(execution_remaining):.3f} of {execution_ceiling:.3f} configured "
            f"tool-execution seconds remain; {execution_used:.3f} seconds have been charged"
        )
        lifetime_words = (
            f"{lifetime} lifetime calls used with no terminal lifetime ceiling"
            if self.absolute_call_limit == 0 else
            f"{lifetime} of {self.absolute_call_limit} absolute calls used"
        )
        return {
            "epoch": self.epoch,
            "epoch_call_limit": self.session.max_calls,
            "epoch_calls_used": self.session.calls,
            "epoch_calls_remaining": remaining,
            "lifetime_calls_used": lifetime,
            "absolute_call_limit": self.absolute_call_limit,
            "tool_execution_mode": execution_mode,
            "tool_execution_ceiling_seconds": execution_ceiling,
            "tool_execution_consumed_seconds": execution_used,
            "tool_execution_remaining_seconds": execution_remaining,
            "tool_execution_exhausted": (
                not execution["unlimited"] and float(execution_remaining) <= 0
            ),
            "tool_execution_accounting": (
                "Only time inside a Nexus context-tool call is charged. Provider/model "
                "thinking, network waits between calls, user pauses, and process downtime are not."
            ),
            "tool_execution_recovery": (
                "Use the saved run's Reset tool time and resume action, or change Context "
                "tool execution seconds in Settings; zero means unlimited."
            ),
            "renewal_policy": "Renews only after Nexus records durable semantic project progress; restart/resume alone never renews it.",
            "summary": (
                f"Exploration epoch {self.epoch}: {remaining} of {self.session.max_calls} calls remain; "
                f"{lifetime_words}. {time_words}. The call/output epoch renews only after durable semantic "
                "project progress, never merely on restart or resume."
            ),
        }


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
    resume_session_id: str = "",
    user_answers: object = None,
    allowed_write_roots: object = None,
    reset_context_tool_execution_budget: bool = False,
) -> dict[str, Any]:
    # Validate user-controlled prose before opening a ledger, contacting a
    # provider, or inspecting/mutating the selected project.  Resume validates
    # the answer here; its canonical goal is loaded from the trusted ledger.
    if resume_session_id:
        if user_answers is not None:
            user_answers = chat_lab._check_what_was_typed(user_answers)
    else:
        text = chat_lab._check_what_was_typed(text)
    if not isinstance(reset_context_tool_execution_budget, bool):
        raise SwarmError("The context-tool budget reset choice must be true or false.")
    if reset_context_tool_execution_budget and not resume_session_id:
        raise SwarmError("Context-tool time can be reset only for an exact saved run.")
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
        session_id=(resume_session_id or None),
    )
    resumed_answers = ""
    resumed_changed_paths: list[str] = []
    previous_write_roots: list[str] = []
    previous_scope_restricted = False
    previous_status = ""
    if resume_session_id:
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", resume_session_id):
            raise SwarmError("The project-work resume token is invalid.")
        events = [
            one for one in ledger._read()
            if str(one.get("session_id") or "") == resume_session_id
        ]
        goal_event = next((one for one in events if one.get("kind") == "user_goal"), None)
        last_outcome = next((
            one for one in reversed(events) if one.get("kind") == "nexus_outcome"
        ), None)
        if last_outcome is None:
            last_outcome = next((
                one for one in reversed(events)
                if one.get("phase") == "mutation_terminal_checkpoint"
            ), None)
        resumable_statuses = {
            "paused_provider", "paused_for_user", "applied_unverified",
            "needs_verification", "incomplete", "paused_tool_budget",
        }
        previous_status = (
            str(last_outcome.get("state", {}).get("status") or "")
            if isinstance(last_outcome, dict) else ""
        )
        if previous_status == "paused" and isinstance(last_outcome, dict):
            legacy_reason = str(
                last_outcome.get("state", {}).get("stopped_because") or ""
            )
            previous_status = (
                "paused_provider"
                if legacy_reason == "provider_unavailable"
                else "paused_for_user"
                if legacy_reason == "paused_for_user"
                else previous_status
            )
        resumable_statuses.add("paused")  # unknown legacy pause still fails closed below
        if goal_event is None or previous_status not in resumable_statuses:
            raise SwarmError("That project-work session is not resumable.")
        if previous_status == "paused":
            raise SwarmError(
                "That legacy paused session does not say whether a user answer or provider retry is required. Start a new run instead."
            )
        if reset_context_tool_execution_budget and previous_status != "paused_tool_budget":
            raise SwarmError(
                "Context-tool time can be reset only after that exact saved run stopped on its tool-execution budget."
            )
        prior_roots = (
            last_outcome.get("state", {}).get("allowed_write_roots", [])
            if isinstance(last_outcome, dict) else []
        )
        if isinstance(prior_roots, list):
            previous_write_roots, rejected_prior_roots = _normalized_write_authority(
                root, prior_roots
            )
            if rejected_prior_roots:
                raise SwarmError(
                    "The saved project-work write authority is stale or outside the selected project: "
                    + ", ".join(repr(one) for one in rejected_prior_roots)
                )
        prior_state = last_outcome.get("state", {}) if isinstance(last_outcome, dict) else {}
        previous_scope_restricted = bool(
            prior_state.get("write_scope_restricted", bool(previous_write_roots))
        )
        if (
            previous_status == "paused_for_user"
            and (user_answers is None or not str(user_answers).strip())
        ):
            raise SwarmError("Answer the paused questions before resuming project work.")
        text = str(goal_event.get("text") or text)
        resumed_answers = str(user_answers or "").strip()
        prior_verification = next((
            one for one in reversed(events)
            if one.get("phase") == "verification_pass_state"
        ), None)
        if isinstance(prior_verification, dict):
            prior_changed = prior_verification.get("state", {}).get("changed", [])
            if isinstance(prior_changed, list):
                resumed_changed_paths = [
                    str(path) for path in prior_changed if isinstance(path, str)
                ]
        if resumed_answers:
            ledger.append(
                kind="user_answer", phase="user_answer", text=resumed_answers,
                speaker_name="User", recipient_name="Project-work team",
                state={
                    "status": "resumed", "resumed_session_id": resume_session_id,
                    "previous_status": previous_status,
                },
            )
        else:
            provider_retry = previous_status == "paused_provider"
            tool_budget_reset = (
                previous_status == "paused_tool_budget"
                and reset_context_tool_execution_budget
            )
            ledger.append(
                kind="nexus_state",
                phase=(
                    "provider_recovery_resume" if provider_retry
                    else "context_tool_budget_reset_authorized" if tool_budget_reset
                    else "saved_work_resume"
                ),
                text=(
                    "Provider recovery was requested without a user answer."
                    if provider_retry else
                    "The user explicitly reset consumed context-tool execution time and resumed the saved run."
                    if tool_budget_reset else
                    "Saved incomplete or unverified work was resumed without an optional note."
                ),
                state={
                    "status": "resumed", "resumed_session_id": resume_session_id,
                    "previous_status": previous_status,
                    "user_answer_required": False,
                },
            )
    else:
        ledger.begin(text, participants, mode="project_work")
    # The semantic authority contract is compiled before attachments are
    # prepared or any provider is contacted.  Provider plans may narrow or
    # instantiate this policy, but can never turn an information request into
    # write authority or sanitize an unsafe path into a different target.
    goal_spec = _compile_goal_spec(root, text)
    acceptance_target = _acceptance_target_decision(
        root, text, goal_spec, resumed_answers,
    )
    contract_goal = (
        text + "\n\nUSER-RATIFIED ACCEPTANCE TARGET\n" + resumed_answers
        if resumed_answers and acceptance_target.get("status") == "ratified"
        else text
    )
    compiled_exact_grants = {
        str(path).casefold(): {str(one).upper() for one in capabilities}
        for path, capabilities in goal_spec.get("write_policy", {}).get("exact_capabilities", {}).items()
        if isinstance(capabilities, list)
    }
    read_only_run = goal_spec["write_policy"]["mode"] == "DENY_ALL"
    read_only_baseline_merkle = ""
    read_only_baseline_manifest: dict[str, str] = {}
    if read_only_run:
        read_only_baseline_merkle, read_only_baseline_manifest = _project_tree_merkle(root)
    preflight_requirement_contract = _derive_requirement_contract(root, contract_goal)
    ledger.record_state("goal_spec", {
        "stage": "pre_provider_authority",
        "goal_spec": goal_spec,
        "read_only_baseline_merkle": read_only_baseline_merkle,
        "acceptance_target": acceptance_target,
    })
    if acceptance_target.get("status") in {"ratified", "unambiguous"}:
        ledger.record_state("acceptance_target_ratified", acceptance_target)
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
    scope_was_supplied = allowed_write_roots is not None
    requested_write_roots, rejected_write_roots = _normalized_write_authority(
        root, allowed_write_roots
    )
    if rejected_write_roots:
        raise SwarmError(
            "One or more requested write destinations are empty, stale, or outside the selected project: "
            + ", ".join(repr(one) for one in rejected_write_roots)
        )
    if previous_scope_restricted:
        if scope_was_supplied and any(
            not _path_is_under(candidate, previous_write_roots)
            for candidate in requested_write_roots
        ):
            raise SwarmError(
                "A resumed project-work session may retain or narrow its original write destinations; it may not broaden them."
            )
        explicit_write_roots = (
            requested_write_roots if scope_was_supplied else previous_write_roots
        )
        write_scope_restricted = True
    else:
        explicit_write_roots = requested_write_roots
        write_scope_restricted = scope_was_supplied
    if not write_scope_restricted and not resume_session_id:
        derived_authority = _path_authority_from_goal(root, text)
        invalid_derived = derived_authority.get("invalid_writable", [])
        if invalid_derived:
            raise SwarmError(
                "The goal names a write destination outside the selected project; Nexus did not broaden access: "
                + ", ".join(repr(one) for one in invalid_derived)
            )
        explicit_write_roots = list(derived_authority["writable"])
        write_scope_restricted = bool(explicit_write_roots)
    if compiled_exact_grants:
        write_scope_restricted = True
    if read_only_run:
        # Read-only is a project-wide capability, not merely protection for
        # the filenames mentioned in the question.  Even an explicitly
        # supplied UI destination cannot broaden it.
        explicit_write_roots = []
        write_scope_restricted = True
    if (
        write_scope_restricted and not explicit_write_roots
        and _goal_intent(text) != "read_only"
        and (scope_was_supplied or not compiled_exact_grants)
    ):
        raise SwarmError(
            "This project-work request has an explicit empty write scope, so no project path is writable. "
            "Select at least one valid destination or start an explicitly read-only run."
        )
    destination_contract = (
        "\nMECHANICALLY ENFORCED WRITE DESTINATIONS\n"
        + "\n".join(f"- {one}/" for one in explicit_write_roots)
        + "\nNexus will reject proposed paths outside these destinations."
        if write_scope_restricted else ""
    )
    if compiled_exact_grants:
        destination_contract += (
            "\nMECHANICALLY ENFORCED EXACT GOAL OPERANDS\n"
            + "\n".join(
                f"- {path}: {', '.join(sorted(capabilities))}"
                for path, capabilities in compiled_exact_grants.items()
            )
            + "\nProvider plans and executors cannot add sibling or unrelated paths."
        )
    write_authority_state = {
        "allowed_write_roots": explicit_write_roots,
        "write_scope_restricted": write_scope_restricted,
        "exact_write_grants": {
            path: sorted(capabilities) for path, capabilities in compiled_exact_grants.items()
        },
        "goal_spec_digest": goal_spec.get("spec_digest"),
    }
    if acceptance_target.get("status") == "needs_clarification":
        question = str(acceptance_target.get("question") or "Clarify the acceptance target.")
        structured_questions = [user_questions.one("acceptance-target", question)]
        clarification_state = {
            **write_authority_state,
            "questions": structured_questions,
            "resume_token": ledger.session_id,
            "acceptance_target": acceptance_target,
            "baseline_merkle": acceptance_target.get("baseline_merkle"),
        }
        ledger.record_state("acceptance_target_question", clarification_state)
        reply = (
            "Nexus paused before contacting providers or changing files because the behavioral "
            "acceptance target is ambiguous.\nQuestions:\n- " + question
            + "\n\nAnswer this question to resume the same session. Nexus applied no project-file changes."
        )
        kept = chat_lab.keep_multiparty_exchange(
            config, str(lead.get("who") or ""), text, reply,
            filed_as=filed_as or str(lead.get("name") or ""),
            lead=lead, participants=participants, contributions=[], attachments=public,
            model="", milliseconds=int((time.monotonic() - began) * 1000),
        )
        ledger.finish(
            reply, complete=False, stopped_because="paused_for_user",
            remaining=[question], status="paused_for_user", state=clarification_state,
        )
        return {
            **kept,
            "collaboration_ledger": ledger.describe(),
            "project": {"id": project.get("id"), "name": project.get("name"), "path": str(root)},
            "transaction_id": "", "transaction_ids": [], "changed": [],
            "goal_complete": False, "verified": False,
            "verification_status": "paused_for_user",
            "status": "paused_for_user", "stopped_because": "paused_for_user",
            "questions": structured_questions, "resume_token": ledger.session_id,
            "acceptance_target": acceptance_target,
            **write_authority_state,
            "round_limit": round_limit, "plan_rounds": 0,
            "work_passes": 0, "remaining": [question],
        }
    common_without_goal = (
        f"EXPLICIT PROJECT-WORK REQUEST\nProject: {project.get('name')}\n"
        "Nexus, not the provider process, owns the project-file transaction. "
        "Paths must be relative to this project. Do not propose .git or .harness files.\n"
        f"Team: {roster}"
        + ("\n\nUSER ANSWERS THAT RESUMED THIS PAUSED RUN\n" + resumed_answers if resumed_answers else "")
        + destination_contract
        + "\nPROJECT TREE\n" + _tree(root)
        + "\nAgents may request exact relative paths, glob:<pattern>, or dir:<path>. "
          "Nexus reports every retrieval omission explicitly."
        + ("\n\n" + attachment_text if attachment_text else "")
    )
    # The first planning request already carries the exact goal as its user
    # message. Do not duplicate a maximum-size goal in that request. Later
    # continuation turns need it in context because their user message is a
    # short stage instruction and some provider routes are stateless.
    common = common_without_goal + f"\nORIGINAL USER GOAL\n{text}"

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
                context=context_for(one) + "\n\n" + common_without_goal
                + "\nPlan your contribution and write a message to the lead. Request only existing files you truly need. "
                  "List every project-relative file that this contribution must create, modify, or delete in effect_paths; an explicitly read-only goal may leave it empty."
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
        except HarnessError as exc:
            return one, {
                "_provider_failed": True,
                "_milliseconds": 0,
                "_model": "",
                "_provider_reason": _provider_reason(ledger, exc),
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
                provider_failures.append({
                    **one,
                    "_provider_reason": str(value.get("_provider_reason") or ""),
                })
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
        joined_reasons = " | ".join(
            f"{one.get('name')}: {one.get('_provider_reason')}"
            for one in provider_failures if one.get("_provider_reason")
        )
        _pause_provider_failure(
            ledger, provider_failures, "independent_planning",
            checkpoint=dict(write_authority_state),
            cause=SwarmError(joined_reasons) if joined_reasons else None,
        )
    plans = [completed[str(one.get("id"))] for one in participants]
    reviewer_correlation = _review_correlation(plans, config)
    ledger.record_state("reviewer_correlation", reviewer_correlation)
    if not reviewer_correlation["independent"]:
        _report(progress, "Reviewer independence warning", reviewer_correlation["warning"])
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
    paused_questions: list[dict[str, Any]] = []
    for round_number in _round_numbers(round_limit):
        plan_rounds = round_number
        everyone_ready = True
        cycle_remaining: list[str] = []
        cycle_state: list[tuple[Any, ...]] = []
        ledger.record_state("prompt_context_checkpoint", {
            "stage": "plan_review", "round": round_number,
            **_prompt_summary_state(contributions),
        })
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
                        + "\n\nON-DEMAND REQUESTED PROJECT CONTENT\n"
                        + _requested_files(root, list(latest.values()))
                        + "\n\nACTUAL PLAN CONVERSATION SO FAR\n"
                        + _prompt_conversation(contributions)
                        + "\n\nReview the team plan and respond to the other agents. Improve your own contribution, "
                          "request any existing files still needed, and set ready_to_execute only when this plan can actually fulfill the user's goal. "
                          "Carry forward the exact project-relative files that must change in effect_paths; add any missing required effect paths found during review. "
                          "ready_to_execute means no more planning or input is needed before Nexus starts the file transaction; it does not mean the files already exist. "
                          "Execution and post-transaction verification steps belong in the plan and are not remaining planning work. "
                          "When the plan already specifies the requested changes and how to verify them, set ready_to_execute true and remaining to an empty list. "
                          "If and only if an essential user decision cannot be inferred safely, put structured questions in questions, keep ready_to_execute false, and Nexus will pause for an answer. Give two or three clear options when useful, mark at most one recommended option, and allow_other unless a custom value would be invalid. Never hide a user question in remaining. "
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
                    checkpoint={"round": round_number, **write_authority_state},
                    cause=exc,
                )
            value["_milliseconds"] = int(answer.get("milliseconds") or 0)
            value["_model"] = str(answer.get("model") or "")
            latest[str(one.get("id"))] = (one, value)
            one_remaining = _remaining(value)
            one_questions = user_questions.normalize(value.get("questions"))
            paused_questions.extend(one_questions)
            one_ready = value.get("ready_to_execute") is True and not one_remaining and not one_questions
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
            "questions": user_questions.frozen(paused_questions),
        })
        if paused_questions:
            plan_stopped_because = "user_input"
            break
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
    if paused_questions:
        paused_questions = user_questions.normalize(paused_questions)
        question_prompts = user_questions.prompts(paused_questions)
        reply = (
            "Nexus paused before changing files because the team needs a user decision.\nQuestions:\n- "
            + "\n- ".join(question_prompts)
            + "\n\nAnswer these questions to resume this exact project-work session. Nexus applied no project-file changes."
        )
        kept = chat_lab.keep_multiparty_exchange(
            config, str(lead.get("who") or ""), text, reply,
            filed_as=filed_as or str(lead.get("name") or ""),
            lead=lead, participants=participants, contributions=contributions,
            attachments=public, model="",
            milliseconds=int((time.monotonic() - began) * 1000),
        )
        ledger.finish(
            reply, complete=False, stopped_because="paused_for_user",
            remaining=question_prompts, status="paused_for_user",
            state={
                "questions": paused_questions,
                "resume_token": ledger.session_id,
                **write_authority_state,
            },
        )
        return {
            **kept,
            "collaboration_ledger": ledger.describe(),
            "project": {"id": project.get("id"), "name": project.get("name"), "path": str(root)},
            "transaction_id": "", "transaction_ids": [], "changed": [],
            "goal_complete": False, "verified": False,
            "verification_status": "paused_for_user",
            "status": "paused_for_user", "stopped_because": "paused_for_user",
            "questions": paused_questions, "resume_token": ledger.session_id,
            "reviewer_correlation": reviewer_correlation,
            **write_authority_state,
            "round_limit": round_limit, "plan_rounds": plan_rounds,
            "work_passes": 0, "remaining": question_prompts,
        }
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
    required_effect_paths = list(dict.fromkeys(
        str(path).replace("\\", "/").strip()
        for _agent, value in plans
        for path in value.get("effect_paths", [])
        if isinstance(path, str) and path.strip()
    ))
    unauthorized_plan_effects = [
        path for path in required_effect_paths
        if compiled_exact_grants and path.casefold() not in compiled_exact_grants
        and not (
            explicit_write_roots and _path_is_under(path, explicit_write_roots)
        )
        and not (
            _is_test_goal(text)
            and re.search(r"(?:^|/)(?:tests?|specs?)(?:/|$)", path, re.I)
        )
    ]
    if unauthorized_plan_effects:
        ledger.record_state("scope_expansion_rejected", {
            "stage": "pre_mutation_acceptance",
            "requested_paths": unauthorized_plan_effects,
            "goal_spec_digest": goal_spec.get("spec_digest"),
            "status": "requires_user_approval",
        })
        required_effect_paths = [
            path for path in required_effect_paths if path not in unauthorized_plan_effects
        ]
    requirement_contract = _derive_requirement_contract(
        root, contract_goal, [] if read_only_run else required_effect_paths,
        ratified_by=[str(one.get("id") or "") for one, _value in plans],
    )
    if requirement_contract["goal_sha256"] != preflight_requirement_contract["goal_sha256"]:
        raise SwarmError("The pre-provider goal requirement contract changed during planning.")
    prior_contract = next((
        event.get("state", {}).get("contract")
        for event in reversed(ledger._read())
        if event.get("session_id") == ledger.session_id
        and event.get("phase") == "requirement_contract"
        and isinstance(event.get("state", {}).get("contract"), dict)
    ), None)
    if isinstance(prior_contract, dict):
        if (
            prior_contract.get("schema_version") != 1
            or prior_contract.get("goal_sha256") != requirement_contract["goal_sha256"]
        ):
            raise SwarmError("The saved whole-goal requirement contract is invalid for this resume.")
        by_id = {
            str(one.get("id")): copy.deepcopy(one)
            for one in prior_contract.get("requirements", []) if isinstance(one, dict)
        }
        for one in requirement_contract["requirements"]:
            identifier = str(one.get("id"))
            if identifier not in by_id:
                by_id[identifier] = one
                continue
            for field in ("effect_roots", "effect_paths", "requested_levels"):
                combined = [
                    *by_id[identifier].get(field, []), *one.get(field, [])
                ]
                if combined:
                    by_id[identifier][field] = list(dict.fromkeys(combined))
        requirement_contract["requirements"] = list(by_id.values())
        requirement_contract["resumed_from_persisted_contract"] = True
    ledger.record_state("requirement_contract", {
        "stage": "pre_mutation_acceptance",
        "contract": requirement_contract,
        "allowed_write_roots": explicit_write_roots,
        "write_scope_restricted": write_scope_restricted,
    })
    ledger.record_state("goal_effect_contract", {
        "intent": _goal_intent(text),
        "explicit_read_only": _goal_intent(text) == "read_only",
        "required_effect_paths": required_effect_paths,
        "goal_named_paths": list(dict.fromkeys(_goal_named_paths(text))),
        "goal_path_roles": _goal_path_roles(text)["mentions"],
    })
    team_plans = "\n\n".join(
        f"CURRENT PLAN FROM {one.get('name')} ({one.get('who')}):\n{_plan_words(value)}"
        for one, value in plans
    )
    _report(
        progress, "Reading the requested project files",
        "Nexus is confining requested paths to the connected project before sharing their current contents."
    )
    all_changed: list[str] = list(dict.fromkeys(resumed_changed_paths))
    transaction_ids: list[str] = []
    mutation_saga = _MutationSaga(root, ledger.session_id)
    work_passes = 0
    goal_complete = False
    provider_consensus = False
    remaining = plan_remaining
    feedback = ""
    final_answer: dict[str, Any] = {"model": ""}
    no_change_passes = 0
    work_progress_guard = _ProgressGuard()
    work_stopped_because = ""
    deterministic_verification: dict[str, Any] = {
        "status": "not_run", "basis": "none", "commands": [],
        "reason": "Provider review has not yet reached consensus.",
    }
    context_tools: _ProjectContextTools | None = None
    for pass_number in _round_numbers(round_limit):
        work_passes = pass_number
        ledger.record_state("prompt_context_checkpoint", {
            "stage": "execution", "pass": pass_number,
            **_prompt_summary_state(contributions),
        })
        _report(
            progress, f"Project execution pass {pass_number}",
            "Each participating agent will receive its own execution turn in board order."
        )
        pass_changed: list[str] = []
        pass_transaction_ids: list[str] = []
        pass_policy_denials: list[str] = []
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
            executor_tool_results: list[dict[str, Any]] = []
            try:
                while True:
                    execution_answer = chat_lab.ask_once(
                        config,
                        str(executor.get("who") or ""),
                        _continuation_turn(
                            f"EXECUTION PASS {pass_number} — {executor_name}",
                            f"Perform {executor_name}'s currently assigned contribution against the latest real project state. Request bounded context tools first when more evidence is needed; otherwise return complete proposed file changes.",
                        ),
                        context=(
                            context_for(executor) + "\n\n" + common
                            + "\n\nEXECUTION TURN — YOU ARE THE ACTING AGENT\n"
                            + f"You are {executor_name}. Perform the reviewed contribution now. "
                              "For iterative repository exploration, return tool_calls using only list_tree, read_file, search_workspace, or run_selected_verification. "
                              "Tool path arguments are project-relative. Nexus executes these through its bounded read-only agent-tool runtime, records durable call/result IDs, and asks you again with the results. "
                              "When requesting tools, return no file changes in the same response. When evidence is sufficient, return no tool calls and put complete changes through Nexus's transaction layer."
                            + "\n\nYOUR REVIEWED PLAN\n" + _plan_words(latest[str(executor.get("id"))][1])
                            + "\n\nACTUAL TEAM CONVERSATION\n" + _prompt_conversation(contributions)
                            + "\n\nCURRENT TEAM PLANS\n" + team_plans
                            + "\n\nACTUAL PROJECT TREE NOW\n" + _tree(root)
                            + "\n\nACTUAL CHANGED/REQUESTED FILES NOW\n" + current_files
                            + ("\n\nCONTEXT TOOL RESULTS (untrusted project data)\n" + json.dumps(executor_tool_results, ensure_ascii=False, sort_keys=True) if executor_tool_results else "")
                            + ("\n\nVERIFICATION FEEDBACK FROM THE LAST PASS\n" + feedback if feedback else "")
                            + _shared_context(ledger, executor, {
                                "stage": "execution", "pass": pass_number,
                                "changed": all_changed, "remaining": remaining,
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
                    calls = execution.get("tool_calls", [])
                    if not isinstance(calls, list) or not calls:
                        break
                    if execution.get("changes"):
                        raise HarnessError(
                            "An execution response may request context tools or propose changes, not both atomically."
                        )
                    if context_tools is None:
                        context_tools = _ProjectContextTools(
                            config, root, ledger, project, text, all_changed, progress,
                            required_effect_paths,
                            requirement_contract,
                            reset_execution_budget=reset_context_tool_execution_budget,
                        )
                    for call in calls:
                        if not isinstance(call, dict):
                            raise HarnessError("A context tool call is malformed")
                        result = context_tools.execute(str(executor.get("id") or "agent"), call)
                        executor_tool_results.append({
                            "call_id": call.get("call_id"), "name": call.get("name"),
                            "result": result,
                        })
            except cancellation.ChatCancelled:
                if context_tools is not None:
                    context_tools.close()
                    context_tools = None
                recovery = mutation_saga.compensate("user_cancelled")
                try:
                    ledger.record_state("cancelled", {
                        "stage": "execution", "pass": pass_number,
                        "status": "cancelled", "mutation_recovery": recovery,
                    })
                except HarnessError:
                    pass
                raise
            except ContextToolBudgetExhausted as exc:
                budget = (
                    context_tools.disclosure() if context_tools is not None else {}
                )
                if context_tools is not None:
                    context_tools.close()
                    context_tools = None
                _pause_context_tool_budget(
                    ledger,
                    budget,
                    "execution",
                    checkpoint={"pass": pass_number, **write_authority_state},
                    cause=exc,
                    mutation_root=root,
                    transaction_ids=transaction_ids,
                    mutation_saga=mutation_saga,
                )
            except HarnessError as exc:
                if context_tools is not None:
                    context_tools.close()
                    context_tools = None
                _pause_provider_failure(
                    ledger,
                    executor,
                    "execution",
                    checkpoint={"pass": pass_number, **write_authority_state},
                    cause=exc,
                    mutation_root=root,
                    transaction_ids=transaction_ids,
                    mutation_saga=mutation_saga,
                )
            if read_only_run and execution.get("changes"):
                ledger.record_state("read_only_proposal_rejected", {
                    "stage": "execution", "pass": pass_number,
                    "agent_id": str(executor.get("id") or ""),
                    "proposed_paths": [
                        str(one.get("path") or "") for one in execution.get("changes", [])
                        if isinstance(one, dict)
                    ],
                    "write_policy": "DENY_ALL",
                })
                changes = []
            else:
                raw_changes = execution.get("changes")
                pass_grants = _with_test_companion_grants(
                    root, raw_changes, compiled_exact_grants, goal_spec, requirement_contract,
                ) if compiled_exact_grants else None
                try:
                    changes = _validated_changes(
                        root, raw_changes,
                        explicit_write_roots if write_scope_restricted and explicit_write_roots else None,
                        [
                            str(one) for one in requirement_contract.get("protected_paths", [])
                            if isinstance(one, str)
                        ],
                        pass_grants,
                    )
                except HarnessError as exc:
                    ledger.record_state("execution_scope_denied", {
                        "stage": "execution", "pass": pass_number,
                        "agent_id": str(executor.get("id") or ""),
                        "reason": str(exc), "goal_spec_digest": goal_spec.get("spec_digest"),
                        "status": "incomplete",
                    })
                    changes = []
                    pass_policy_denials.append("Nexus write-policy denial: " + str(exc))
            executor_changed: list[str] = []
            if changes:
                _report(
                    progress, f"Applying {executor_name}'s proposed changes",
                    "Nexus is checking paths and fresh baselines before opening the atomic transaction."
                )
                try:
                    with swarm_runs.post_provider_mutation():
                        transaction_id = FileTransaction.new_transaction_id()
                        mutation_saga.prepare(transaction_id)
                        manifest = FileTransaction(
                            root,
                            max_files=12,
                            max_bytes=int(config.get("execution.max_changed_bytes")),
                        ).apply(
                            changes, transaction_id=transaction_id,
                            allowed_exact_capabilities=pass_grants,
                            allowed_write_roots=(
                                explicit_write_roots
                                if write_scope_restricted and explicit_write_roots else None
                            ),
                            protected_paths=[
                                str(one) for one in requirement_contract.get("protected_paths", [])
                                if isinstance(one, str)
                            ],
                        )
                        _record_applied_transaction(
                            ledger, mutation_saga, transaction_id, manifest,
                        )
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
                if transaction_id:
                    pass_transaction_ids.append(transaction_id)
                pass_changed.extend(
                    path for path in executor_changed if path not in pass_changed
                )
                all_changed.extend(
                    path for path in executor_changed if path not in all_changed
                )
            execution_words = str(
                execution.get("reply") or "Execution turn finished."
            ).strip()
            execution_words += "\nStaged provisionally in this run (team verification pending): " + (
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
                semantic=execution,
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
        verification_feedback.extend(pass_policy_denials)
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
                        + "\n\nACTUAL TEAM CONVERSATION\n" + _prompt_conversation(contributions)
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
                    checkpoint={"pass": pass_number, **write_authority_state},
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
            turn = _contribution(
                one, answer, "agent_verification", words, semantic=value,
            )
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
        provider_consensus = pass_complete
        if pass_complete:
            try:
                deterministic_verification = _run_selected_project_verification(
                    config, root, project, contract_goal, all_changed, progress,
                    required_effect_paths,
                    requirement_contract=requirement_contract,
                    verification_session_id=ledger.session_id,
                    read_only_baseline_merkle=read_only_baseline_merkle,
                    transaction_ids=transaction_ids,
                )
            except cancellation.ChatCancelled:
                recovery = mutation_saga.compensate("user_cancelled_during_verification")
                try:
                    ledger.record_state("cancelled", {
                        "stage": "deterministic_verification", "pass": pass_number,
                        "status": "cancelled", "mutation_recovery": recovery,
                    })
                    ledger.finish(
                        "Nexus stopped during deterministic verification and rolled back provisional changes.",
                        complete=False, stopped_because="user_cancelled",
                        remaining=["Resume or start the project work again when ready."],
                        status="paused",
                        state={"resume_token": ledger.session_id},
                    )
                except HarnessError:
                    pass
                raise
            if deterministic_verification["status"] == "passed":
                goal_complete = True
            else:
                pass_complete = False
                verification_problem = str(deterministic_verification.get("reason") or "Deterministic verification did not pass.")
                remaining.append(verification_problem)
                verification_feedback.append("Nexus deterministic verification: " + verification_problem)
        ledger.record_state("verification_pass_state", {
            "stage": "verification",
            "pass": pass_number,
            "all_agents_complete": provider_consensus,
            "changed": all_changed,
            "remaining": remaining,
            "verification_basis": deterministic_verification.get("basis", "provider_claims_only"),
            "deterministically_verified": deterministic_verification.get("status") == "passed",
            "deterministic_verification": deterministic_verification,
        })
        if goal_complete:
            work_stopped_because = "complete"
            break
        if context_tools is not None and pass_transaction_ids:
            context_tools.renew_after_progress(pass_transaction_ids)
        feedback = "\n\n".join(verification_feedback)
        relevant_changed = [
            path for path in all_changed
            if _delta_path_matches_requirement(root, requirement_contract, path)
        ]
        project_state = _project_state_digest(root, relevant_changed)
        verification_state = str(deterministic_verification.get("status") or "not_run")
        transaction_evidence: set[str] = set()
        for transaction_id in transaction_ids:
            try:
                manifest = FileTransaction(root).load_manifest(transaction_id)
            except (HarnessError, OSError):
                continue
            if manifest.get("state") != "applied":
                continue
            records = manifest.get("changes", [])
            if not isinstance(records, list):
                continue
            authenticated = [
                {
                    "path": record.get("path"),
                    "before": record.get("before_sha256"),
                    "after": record.get("after_sha256"),
                    "delete": record.get("delete") is True,
                }
                for record in records
                if isinstance(record, dict)
                and isinstance(record.get("path"), str)
                and _delta_path_matches_requirement(
                    root, requirement_contract, str(record.get("path")),
                )
            ]
            if authenticated:
                transaction_evidence.add(hashlib.sha256(json.dumps(
                    authenticated, sort_keys=True, separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")).hexdigest())
        valid_receipts = sorted({
            str(receipt.get("receipt_digest") or receipt.get("direct_probe_digest") or "")
            for receipt in deterministic_verification.get("causal_receipts", [])
            if isinstance(receipt, dict)
            and str(receipt.get("receipt_digest") or receipt.get("direct_probe_digest") or "")
        })
        progress_key = hashlib.sha256(json.dumps({
            "goal_spec_digest": goal_spec.get("spec_digest"),
            "acceptance_target_ratification": acceptance_target.get("ratification_digest", ""),
            "project_state": project_state,
            "verification_status": verification_state,
            "verification_reason": deterministic_verification.get("reason"),
            "unmet_requirements": deterministic_verification.get(
                "requirement_evidence", {}
            ).get("execution", {}).get("unmet", []),
            # Provider prose, context call IDs/results and keyword-shaped reads
            # are deliberately excluded. Only sealed receipts and authenticated
            # operation deltas can reset long-horizon progress.
            "valid_receipts": valid_receipts,
            "transactions": sorted(transaction_evidence),
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
        deterministic_unmet = deterministic_verification.get("requirement_evidence", {})
        if isinstance(deterministic_unmet, dict):
            deterministic_unmet = deterministic_unmet.get("execution", deterministic_unmet)
        deterministic_unmet = (
            deterministic_unmet.get("unmet", [])
            if isinstance(deterministic_unmet, dict) else []
        )
        if work_progress_guard.stalled(((
            "project", progress_key, _progress_terms(deterministic_unmet),
        ),)):
            remaining.append(
                "Nexus detected a repeated end-of-pass project state with unchanged deterministic verification; the run can be resumed after new evidence or user input."
            )
            work_stopped_because = "stalled"
            break
    if not goal_complete and not work_stopped_because:
        remaining.append(
            f"The user-set limit of {round_limit} project execution/verification round(s) was reached."
        )
        work_stopped_because = "round_limit"
    if read_only_run:
        terminal_merkle, terminal_manifest = _project_tree_merkle(root)
        if transaction_ids or all_changed or terminal_merkle != read_only_baseline_merkle:
            ledger.record_state("read_only_invariant_failed", {
                "status": "incomplete", "transaction_ids": transaction_ids,
                "changed": all_changed, "baseline_merkle": read_only_baseline_merkle,
                "terminal_merkle": terminal_merkle,
                "baseline_files": len(read_only_baseline_manifest),
                "terminal_files": len(terminal_manifest),
            })
            raise SwarmError(
                "Nexus refused to complete the informational run because project-wide zero-write authority was violated."
            )
    mutation_recovery = {"status": "not_needed", "rolled_back_transaction_ids": []}
    applied_unverified = bool(all_changed) and provider_consensus and not goal_complete
    if not goal_complete and transaction_ids and not applied_unverified:
        mutation_recovery = mutation_saga.compensate("incomplete")
        if mutation_recovery["status"] == "rolled_back":
            all_changed = list(dict.fromkeys(resumed_changed_paths))
        else:
            remaining.append(
                "Automatic rollback stopped at a project-file conflict; inspect the transaction manifests before retrying."
            )
            work_stopped_because = "rollback_conflict"
    terminal_status = (
        "complete" if goal_complete else
        "needs_verification" if applied_unverified and deterministic_verification.get("status") == "failed" else
        "applied_unverified" if applied_unverified else "incomplete"
    )
    context_tool_budget = context_tools.disclosure() if context_tools is not None else {
        "epoch": 0,
        "epoch_call_limit": int(config.get("workflow.max_tool_calls")),
        "epoch_calls_used": 0,
        "epoch_calls_remaining": int(config.get("workflow.max_tool_calls")),
        "lifetime_calls_used": 0,
        "absolute_call_limit": 0,
        "tool_execution_mode": (
            "unlimited"
            if int(config.get("workflow.context_tool_execution_seconds", 0)) == 0
            else "configured"
        ),
        "tool_execution_ceiling_seconds": float(
            config.get("workflow.context_tool_execution_seconds", 0)
        ),
        "tool_execution_consumed_seconds": 0.0,
        "tool_execution_remaining_seconds": (
            None
            if int(config.get("workflow.context_tool_execution_seconds", 0)) == 0
            else float(config.get("workflow.context_tool_execution_seconds", 0))
        ),
        "tool_execution_exhausted": False,
        "tool_execution_accounting": (
            "Only time inside a Nexus context-tool call is charged. Provider/model "
            "thinking, network waits between calls, user pauses, and process downtime are not."
        ),
        "tool_execution_recovery": (
            "Use the saved run's Reset tool time and resume action, or change Context "
            "tool execution seconds in Settings; zero means unlimited."
        ),
        "renewal_policy": "Renews only after Nexus records durable semantic project progress; restart/resume alone never renews it.",
        "summary": (
            "No project context tools were requested in this run. Aggregate tool execution "
            + (
                "time is unlimited; individual subprocess timeouts still apply."
                if int(config.get("workflow.context_tool_execution_seconds", 0)) == 0
                else "has the displayed configured ceiling; only active tool time is charged."
            )
        ),
    }
    if context_tools is not None:
        context_tools.close()
        context_tools = None
    # This durable, fsynced ledger checkpoint is written before the mutation
    # saga becomes terminal. A crash after file commit but before transcript
    # rendering therefore still leaves a resumable session with immutable
    # destination authority and the exact changed paths.
    ledger.record_state("mutation_terminal_checkpoint", {
        "stage": "mutation_terminal_checkpoint",
        "status": "needs_verification" if goal_complete else terminal_status,
        "verified_before_checkpoint": goal_complete,
        "changed": all_changed,
        "transaction_ids": transaction_ids,
        **write_authority_state,
        "deterministic_verification": deterministic_verification,
        "context_tool_budget": context_tool_budget,
        "remaining": remaining,
    })
    if goal_complete:
        mutation_saga.complete("deterministically_verified")
    elif applied_unverified:
        mutation_saga.complete(
            "needs_verification"
            if deterministic_verification.get("status") == "failed"
            else "applied_unverified"
        )
    else:
        mutation_saga.complete("no_mutations")
    reply = (
        "The connected agents completed their assigned execution turns and deterministic verification passed."
        if goal_complete else
        "The connected agents stopped without completing every assigned execution turn."
    )
    if goal_complete:
        reply += (
            "\n\nNexus verification: selected-project deterministic checks passed."
        )
    else:
        if applied_unverified:
            reply += (
                "\n\nNexus state: applied_unverified. Provider reviewers agreed, but deterministic "
                "selected-project verification did not pass or was unavailable. Nexus has not claimed completion."
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
        status=terminal_status,
        state={
            "resume_token": "" if goal_complete else ledger.session_id,
            **write_authority_state,
            "changed": all_changed,
            "transaction_ids": transaction_ids,
            "deterministic_verification": deterministic_verification,
            "context_tool_budget": context_tool_budget,
        },
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
        "verified": goal_complete,
        "verification_status": (
            "deterministically_verified" if goal_complete else
            "needs_verification" if applied_unverified and deterministic_verification.get("status") == "failed" else
            "applied_unverified" if applied_unverified else "incomplete"
        ),
        "status": (
            "complete" if goal_complete else
            "needs_verification" if applied_unverified and deterministic_verification.get("status") == "failed" else
            "applied_unverified" if applied_unverified else "incomplete"
        ),
        "deterministic_verification": deterministic_verification,
        "reviewer_correlation": reviewer_correlation,
        "context_tool_budget": context_tool_budget,
        **write_authority_state,
        "resume_token": "" if goal_complete else ledger.session_id,
        "mutation_recovery": mutation_recovery,
        "plan_rounds": plan_rounds,
        "work_passes": work_passes,
        "round_limit": round_limit,
        "stopped_because": work_stopped_because,
        "remaining": remaining,
    }
