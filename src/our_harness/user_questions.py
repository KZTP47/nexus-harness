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
