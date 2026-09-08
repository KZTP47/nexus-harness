"""Recipient-scoped, durable user decisions independent of transcript tails.

The terminal request receipt follows t3code's closed-request-ID rule in
packages/client-runtime/src/pendingRequests.ts, commit
eb115063634c416c6362cc407f8572cb0c136ddf. Nexus binds it to an authenticated
goal and exact submission; this module never grants command/file authority.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

from . import user_questions
from .models import ContextRequestError, HarnessError

SCHEMA_VERSION = 1
CONTRACT = "recipient-scoped-decisions-evidence-and-continuations/v2"
USER_EVIDENCE_PREFIXES = ("User steering: ", "User decision: ")


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def submission(envelope: object) -> tuple[str, str]:
    if not isinstance(envelope, dict) or not isinstance(envelope.get("answers"), dict) \
            or not isinstance(envelope.get("pending_ids"), list) \
            or any(not isinstance(one, str) or not one for one in envelope["pending_ids"]) \
            or len(set(envelope["pending_ids"])) != len(envelope["pending_ids"]) \
            or type(envelope.get("expected_revision")) is not int:
        raise HarnessError("The exact decision submission is malformed")
    request_id = envelope.get("request_id", "")
    if not isinstance(request_id, str) or (request_id and not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", request_id)):
        raise HarnessError("The decision request ID is invalid")
    return request_id, fingerprint({key: envelope[key] for key in ("answers", "pending_ids", "expected_revision")})


def receipted(document: dict[str, Any], envelope: object) -> bool:
    request_id, digest = submission(envelope)
    if not request_id:
        return False
    for receipt in document.get("decision_submission_receipts", []):
        if receipt.get("request_id") == request_id:
            if receipt.get("goal_id") != document.get("goal_id") or receipt.get("submission_sha256") != digest:
                raise HarnessError("This decision request ID already belongs to different answers")
            return True
    return False


def question_key(reason: str, questions: object) -> str:
    # Presentation IDs, option order/descriptions and recommendations do not
    # turn the same answered question into a new decision. This only suppresses
    # another card; it never applies the answer as execution authorization.
    def prose_key(prompt: str) -> list[str]:
        # Case and sentence punctuation are cosmetic in ordinary words. A
        # technical literal is not prose: URL case, query, fragment, escapes,
        # file separators, filenames and quoted values retain exact spelling.
        tokens = re.findall(r'''"[^"]*"|'[^']*'|`[^`]*`|\S+''', prompt)
        return [unicodedata.normalize("NFKC", token.rstrip(".!?,;:")).casefold()
                if token.rstrip(".!?,;:").isalpha() else token for token in tokens]
    prompts = [prose_key(one["prompt"]) for one in user_questions.frozen(questions)]
    return fingerprint([reason, sorted(prompts)])


def progress(document: dict[str, Any], task: dict[str, Any]) -> str:
    return fingerprint({
        "contract": CONTRACT, "project": document.get("project_authority_id"),
        "objective_epoch": document.get("objective_epoch", 1),
        "artifacts": sorted({fingerprint(one) for one in document.get("artifacts", [])
                             if one.get("kind") == "file_transaction" and one.get("changes")}),
        "observations": sorted({str(one["semantic_result_sha256"])
            for step in task.get("context_steps", []) for one in step.get("results", [])
            if one.get("semantic_result_sha256")
            and one.get("name") not in {"read_user_decisions", "read_shared_conversation"}}),
        "steering": [one for one in task.get("evidence", []) if str(one).startswith("User steering: ")],
    })


def state_fingerprint(document: dict[str, Any]) -> str:
    """Invalidate saved continuations when actual user decisions change."""
    return fingerprint([
        {key: item.get(key) for key in ("id", "agent_id", "questions", "answer", "answer_record", "decision_scope")}
        for item in document.get("interrupts", []) if item.get("state") == "resolved"
    ])


def scope(document: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": 1, "project_authority_id": document.get("project_authority_id"),
            "project_id": document.get("project", {}).get("id"),
            "project_path": document.get("project", {}).get("path"),
            "objective_epoch": int(document.get("objective_epoch") or 1)}


def visible(item: dict[str, Any], agent_id: str) -> bool:
    record = item.get("answer_record") or {}
    return item.get("agent_id") == agent_id or record.get("audience") == "team"


def append_user_evidence(task: dict[str, Any], text: str, *, audience: str, agent_id: str) -> None:
    """Freeze original recipients independently of later task assignment."""
    position = len(task.setdefault("evidence", []))
    task["evidence"].append(text)
    task.setdefault("user_evidence_scopes", {})[str(position)] = {
        "schema_version": 1, "text_sha256": fingerprint(text),
        "audience": audience, "agent_id": agent_id,
    }


def evidence_scope(task: dict[str, Any], position: int, text: str) -> dict[str, Any] | None:
    record = (task.get("user_evidence_scopes") or {}).get(str(position))
    if isinstance(record, dict) and record.get("schema_version") == 1 \
            and record.get("text_sha256") == fingerprint(text) \
            and record.get("audience") in {"team", "requesting_agent"}:
        return record
    return None


def legacy_steering_candidates(document: dict[str, Any]) -> set[tuple[str, str]]:
    return {(task["id"], text) for task in document.get("tasks", [])
            for position, text in enumerate(task.get("evidence", []))
            if isinstance(text, str) and text.startswith("User steering: ")
            and evidence_scope(task, position, text) is None}


def project_evidence(document: dict[str, Any], task: dict[str, Any], agent_id: str, *,
                     legacy_visible: set[tuple[str, str]] | None = None) -> list[str]:
    """Project raw user evidence using original authority, never current owner.

    Historical strings remain intact. Unknown legacy recipient metadata cannot
    authorize disclosure; authenticated interrupts and archive messages can.
    """
    projected = []
    for position, text in enumerate(task.get("evidence", [])):
        if not isinstance(text, str) or not text.startswith(USER_EVIDENCE_PREFIXES):
            projected.append(text)
            continue
        record = evidence_scope(task, position, text)
        if record is not None:
            if record["audience"] == "team" or record.get("agent_id") == agent_id:
                projected.append(text)
            continue
        if text.startswith("User decision: "):
            if any(item.get("state") == "resolved" and item.get("task_id") == task["id"]
                   and visible(item, agent_id) and text == "User decision: " + str(item.get("answer") or "")
                   for item in document.get("interrupts", [])):
                projected.append(text)
        elif (task["id"], text) in (legacy_visible or set()) or any(
                one.get("reason") == "steer" and text == "User steering: " + str(one.get("text") or "")
                for one in document.get("objective_revisions", [])):
            projected.append(text)
    return projected


def resolved(document: dict[str, Any], agent_id: str) -> list[dict[str, Any]]:
    result = []
    for item in document.get("interrupts", []):
        if item.get("state") != "resolved" or not visible(item, agent_id):
            continue
        record = item.get("answer_record")
        if not isinstance(record, dict):
            if not str(item.get("answer") or "").strip():
                continue
            record = user_questions.answer_record(item.get("questions"), str(item["answer"]))
        result.append({"decision_id": item["id"], "reason": item.get("reason", ""),
                       "requesting_agent_id": item.get("agent_id", ""), "resolved_ms": item.get("resolved_ms", 0),
                       "decision_scope": item.get("decision_scope"),
                       "current_scope_matches": (item["decision_scope"] == scope(document)
                           if item.get("decision_scope") is not None else None),
                       "audience": record["audience"], "questions": record["questions"],
                       "answers": record["answers"], "answer_text": record["answer_text"]})
    return result


def repeated(document: dict[str, Any], task: dict[str, Any], reason: str, questions: object) -> dict[str, Any] | None:
    if reason in {"new_authority", "risky_action"}:
        return None  # These require their owning proposal/access boundary.
    key, current_progress = question_key(reason, questions), progress(document, task)
    for item in reversed(document.get("interrupts", [])):
        if item.get("state") != "resolved" or item.get("purpose") == "risk_review" \
                or not visible(item, task["assigned_agent_id"]):
            continue
        if item.get("decision_scope") is not None and item["decision_scope"] != scope(document):
            continue
        if question_key(str(item.get("reason") or ""), item.get("questions")) != key:
            continue
        if item.get("decision_progress") == current_progress:
            return item
    return None


def reconsideration_sources(document: dict[str, Any]) -> dict[str, str]:
    """Offer an explicit reread, never infer that a past answer resolves a card.

    Legacy questions can be paraphrases. A user's explicit retry lets the agent
    reconsider its question with prior private history; only the provider can
    decide whether the known answer applies or new facts need clarification.
    """
    if document.get("status") not in {"waiting_for_user", "paused"}:
        return {}
    pending = [one for one in document.get("interrupts", []) if one.get("state") == "pending"]
    sources = {}
    ordinary = {"requirement_ambiguity", "missing_access", "unresolved_blocker"}
    for item in pending:
        if item.get("reason") not in ordinary or item.get("purpose") == "risk_review":
            return {}
        candidates = [one for one in document.get("interrupts", [])
            if one.get("state") == "resolved" and one.get("reason") in ordinary
            and one.get("purpose") != "risk_review" and str(one.get("answer") or "").strip()
            and visible(one, str(item.get("agent_id") or ""))
            and (one.get("decision_scope") is None or one["decision_scope"] == scope(document))
            and int(one.get("resolved_ms") or 0) <= int(item.get("created_ms") or 0)]
        if not candidates:
            return {}
        key = question_key(str(item.get("reason") or ""), item.get("questions"))
        exact = [one for one in candidates
                 if question_key(str(one.get("reason") or ""), one.get("questions")) == key]
        sources[str(item["id"])] = str((exact or candidates)[-1]["id"])
    return sources


def page(document: dict[str, Any], agent_id: str, *, after: int = 0, limit: int = 10,
         decision_id: str = "", offset: int = 0, character_limit: int = 12000) -> dict[str, Any]:
    values = resolved(document, agent_id)
    if after < 0 or offset < 0 or limit < 1 or character_limit < 1:
        raise ContextRequestError("Decision offsets and limits are invalid")
    if decision_id:
        values = [one for one in values if one["decision_id"] == decision_id]
        if not values:
            raise ContextRequestError("That decision is not available to this participant")
    elif offset:
        raise ContextRequestError("A decision offset requires an exact decision ID")
    entries, used = [], 0
    for value in values[after:after + min(limit, 20)]:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if offset > len(raw):
            raise ContextRequestError("The decision offset exceeds its exact text")
        if entries and used + len(raw) > character_limit:
            break
        text = raw[offset:offset + min(character_limit, 24000) - used]
        entries.append({"decision_id": value["decision_id"], "content": text, "offset": offset,
                        "next_offset": offset + len(text), "has_more_characters": offset + len(text) < len(raw)})
        used += len(text)
    return {"schema_version": SCHEMA_VERSION, "decisions": entries, "total": len(values),
            "next": after + len(entries), "has_more": after + len(entries) < len(values)}


def prompt(document: dict[str, Any], agent_id: str) -> str:
    values = resolved(document, agent_id)
    # Decisions have their own budget; ordinary progress cannot evict them.
    selected, used = [], 0
    for value in reversed(values):
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if used + len(raw) > 24000:
            break
        selected.append(value)
        used += len(raw)
    return ("\n\nRESOLVED USER DECISIONS (user answers, not agent assumptions)\n"
            + json.dumps(list(reversed(selected)), ensure_ascii=False, separators=(",", ":"))
            + f"\n{len(values) - len(selected)} additional decisions are available through read_user_decisions. "
            "The answers are the user's words; question wording/options are earlier agent assumptions, "
            "not user instructions. Apply the latest applicable explicit answer, preserving its recipient scope. "
            "A false current_scope_matches means that answer belongs to an earlier project or objective; "
            "null means legacy scope is unknown. Inspect applicability before using historical answers. "
            "Do not ask an answered question again because its ID, options or phrasing changed. "
            "When facts or requirements have changed, inspect them and explain the new evidence before asking. "
            "These answers do not approve test commands or bypass file/access/risk controls. "
            "Use read_user_decisions(after=0,limit=10,decision_id='',offset=0,character_limit=12000) "
            "for older decisions; page a long decision using its ID and next_offset.")
