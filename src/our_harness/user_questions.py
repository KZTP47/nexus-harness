"""Structured, durable questions an agent can hand back to a person.

The provider-facing protocol is deliberately tiny and provider-neutral.  A
normal assistant answer remains normal prose.  Only an assistant that truly
needs a decision appends one ``nexus-user-input`` JSON fence.  Nexus removes
the transport fence, saves the questions as transcript metadata, and renders
ordinary controls beside the message.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

from .models import HarnessError


MAX_QUESTIONS = 6
MAX_OPTIONS = 8
_QUESTION_FENCE = re.compile(
    r"(?:^|\n)```nexus-user-input\s*\r?\n(?P<payload>\{[\s\S]*?\})\s*\r?\n```\s*$",
    re.IGNORECASE,
)

OPTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "maxLength": 160},
        "description": {"type": "string", "maxLength": 500},
        "recommended": {"type": "boolean"},
    },
    "required": ["label", "description", "recommended"],
    "additionalProperties": False,
}

QUESTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "maxLength": 120},
        "prompt": {"type": "string", "maxLength": 500},
        "options": {
            "type": "array",
            "maxItems": MAX_OPTIONS,
            "items": OPTION_SCHEMA,
        },
        "multiple": {"type": "boolean"},
        "allow_other": {"type": "boolean"},
    },
    "required": ["id", "prompt", "options", "multiple", "allow_other"],
    "additionalProperties": False,
}

QUESTIONS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "maxItems": MAX_QUESTIONS,
    "items": QUESTION_SCHEMA,
}


def _identifier(value: object, fallback: str) -> str:
    held = re.sub(r"[^A-Za-z0-9_-]", "-", str(value or "").strip())[:120]
    return held.strip("-") or fallback


def normalize(value: object) -> list[dict[str, Any]]:
    """Return a bounded canonical question list, accepting legacy strings."""

    if not isinstance(value, list):
        return []
    found: list[dict[str, Any]] = []
    used: set[str] = set()
    for position, raw in enumerate(value[:MAX_QUESTIONS], start=1):
        if isinstance(raw, str):
            prompt = raw.strip()[:500]
            if not prompt:
                continue
            raw = {
                "id": f"question-{position}", "prompt": prompt,
                "options": [], "multiple": False, "allow_other": True,
            }
        if not isinstance(raw, dict):
            continue
        prompt = str(raw.get("prompt") or "").strip()[:500]
        if not prompt:
            continue
        question_id = _identifier(raw.get("id"), f"question-{position}")
        if question_id.casefold() in used:
            question_id = f"{question_id}-{position}"
        used.add(question_id.casefold())
        options: list[dict[str, Any]] = []
        recommendation_kept = False
        for option in list(raw.get("options") or [])[:MAX_OPTIONS]:
            if not isinstance(option, dict):
                continue
            label = str(option.get("label") or "").strip()[:160]
            if not label:
                continue
            recommended = option.get("recommended") is True and not recommendation_kept
            recommendation_kept = recommendation_kept or recommended
            options.append({
                "label": label,
                "description": str(option.get("description") or "").strip()[:500],
                "recommended": recommended,
            })
        found.append({
            "id": question_id,
            "prompt": prompt,
            "options": options,
            "multiple": bool(raw.get("multiple")) and len(options) > 1,
            "allow_other": raw.get("allow_other") is not False or not options,
        })
    return found


def one(question_id: str, prompt: str) -> dict[str, Any]:
    """Build one free-text question in the canonical shape."""

    return normalize([{
        "id": question_id,
        "prompt": prompt,
        "options": [],
        "multiple": False,
        "allow_other": True,
    }])[0]


def prompts(value: object) -> list[str]:
    return [str(question["prompt"]) for question in normalize(value)]


def provider_instruction() -> str:
    """Protocol shown only to a directly addressed board agent."""

    example = {
        "questions": [{
            "id": "target-platform",
            "prompt": "Which platform should this target?",
            "options": [{
                "label": "Windows 11",
                "description": "Use the current supported desktop target.",
                "recommended": True,
            }],
            "multiple": False,
            "allow_other": True,
        }]
    }
    return (
        "NEXUS USER-INPUT CAPABILITY\n"
        "Answer normally whenever you can make safe, reversible progress. If an essential "
        "user decision is genuinely required, ask it in your prose and append exactly one "
        "fenced nexus-user-input JSON object at the very end of the response. Give two or "
        "three mutually exclusive options when useful, mark at most one recommended option, "
        "and allow a custom answer unless that would be invalid. Do not use this protocol for "
        "rhetorical questions or optional preferences. Schema example:\n"
        "```nexus-user-input\n"
        + json.dumps(example, ensure_ascii=False, separators=(",", ":"))
        + "\n```"
    )


def extract(text: object) -> tuple[str, list[dict[str, Any]]]:
    """Remove one valid terminal question envelope from assistant prose."""

    source = str(text or "")
    match = _QUESTION_FENCE.search(source)
    if match is None:
        return source, []
    try:
        payload = json.loads(match.group("payload"))
    except json.JSONDecodeError:
        return source, []
    questions = normalize(payload.get("questions") if isinstance(payload, dict) else None)
    if not questions:
        return source, []
    visible = source[:match.start()].rstrip()
    if not visible:
        visible = "I need your answer before I can continue."
    return visible, questions


def frozen(value: object) -> list[dict[str, Any]]:
    """Return an isolated JSON-safe copy for transcripts and run journals."""

    return copy.deepcopy(normalize(value))


def answer_record(questions: object, value: object) -> dict[str, Any]:
    """Keep user text separate from agent-authored framing and choice labels.

    Adapted from t3code apps/web/src/pendingUserInput.ts at eb115063634c416c6362cc407f8572cb0c136ddf:
    exact question keys, selected options and custom answers remain distinct.
    Nexus additionally validates the saved question and retains legacy input.
    """
    saved = frozen(questions)
    if isinstance(value, str):
        text = value.strip()
        if not text or len(value) > 20_000:
            raise HarnessError("A decision answer needs 1 to 20,000 characters. No answers were saved or truncated.")
        # Old Nexus cards prefixed a single answer with the exact saved prompt.
        # Preserve original bytes separately; do not infer or split other prose.
        prefix = str(saved[0]["prompt"]) + ": " if len(saved) == 1 else ""
        body = text[len(prefix):] if prefix and text.startswith(prefix) else text
        if not body.strip():
            raise HarnessError("Answer every saved question before continuing")
        return {"schema_version": 1, "audience": "requesting_agent", "raw_answer": value,
                "questions": saved, "answers": [{"question_id": saved[0]["id"] if len(saved) == 1 else "",
                    "selected_options": [], "text": body}], "answer_text": body, "legacy": True}
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int or value.get("schema_version") != 1 \
            or set(value) != {"schema_version", "audience", "questions"} \
            or value.get("audience") not in {"team", "requesting_agent"}:
        raise HarnessError("The decision answer format or audience is invalid")
    entries = value.get("questions")
    if not isinstance(entries, list) or not saved or len(entries) != len(saved):
        raise HarnessError("Answer every exact saved question once")
    expected = {one["id"]: one for one in saved}
    result, seen, characters = [], set(), 0
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"question_id", "selected_options", "text"}:
            raise HarnessError("Each answer needs its exact question ID, selected options and raw text")
        identity = entry.get("question_id")
        if not isinstance(identity, str) or identity not in expected or identity in seen:
            raise HarnessError("The decision contains an unknown or duplicate question ID")
        seen.add(identity)
        question = expected[identity]
        options, text = entry.get("selected_options"), entry.get("text")
        if not isinstance(options, list) or any(not isinstance(one, str) for one in options) \
                or len(set(options)) != len(options) or not isinstance(text, str):
            raise HarnessError("Selected options and custom answer text are malformed")
        labels = {one["label"] for one in question["options"]}
        if any(one not in labels for one in options) or (len(options) > 1 and not question["multiple"]):
            raise HarnessError("Choose only saved options allowed by this question")
        if text.strip() and not question["allow_other"]:
            raise HarnessError("This question does not allow a custom answer")
        if not text.strip() and not options:
            raise HarnessError("Answer every saved question before continuing")
        characters += len(text) + sum(len(one) for one in options)
        result.append({"question_id": identity, "selected_options": list(options), "text": text})
    if characters > 20_000:
        raise HarnessError("A decision answer supports at most 20,000 characters. No answers were saved or truncated.")
    return {"schema_version": 1, "audience": value["audience"], "raw_answer": copy.deepcopy(value),
            "questions": saved, "answers": result,
            "answer_text": "\n".join(one["text"].strip() or ", ".join(one["selected_options"]) for one in result),
            "legacy": False}
