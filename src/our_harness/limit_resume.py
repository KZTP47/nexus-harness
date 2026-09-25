"""Resume goals that paused only because a provider's usage limit was reached.

A subscription limit is not a failure of the work: it resets at a known time.
Nexus reads the reset time from the provider's own message ("resets 5pm",
"try again in 2h 13m", an ISO time, Retry-After seconds) and resumes the goal
once it has passed, exactly as the goal's Resume button would. Without a time
it waits a short, growing interval. Goals the user paused, or paused for any
other reason, are never touched, and repeated limit pauses stop after a few
automatic attempts so the user decides.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

CONTRACT = "nexus-limit-resume/v1"
PROVIDER_PAUSE = "Required provider work failed"
MARGIN_SECONDS = 120
FALLBACK_SECONDS = 30 * 60
MAX_WAIT_SECONDS = 8 * 24 * 3600
MAX_AUTOMATIC_RESUMES = 6

_LIMIT = re.compile(
    r"usage limit|rate[ -]?limit|hit your (?:usage )?limit|limit (?:was |has been )?reached|reached your .*limit"
    r"|too many requests|\b429\b|quota (?:exceeded|exhausted)|insufficient quota|out of credits"
    r"|(?:5-hour|five-hour|weekly|daily) limit|limit (?:will )?resets?",
    re.IGNORECASE,
)
_UNITS = {"d": 86400, "day": 86400, "days": 86400, "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600,
          "hours": 3600, "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60, "s": 1,
          "sec": 1, "secs": 1, "second": 1, "seconds": 1}
_DURATION = re.compile(
    r"(?:try again|retry|resets?|available again|wait)\s+(?:in|after)\s+(?:about\s+|approximately\s+)?"
    r"((?:\d+(?:\.\d+)?\s*(?:days?|d|hours?|hrs?|h|minutes?|mins?|min|m|seconds?|secs?|sec|s)\b[\s,]*(?:and\s+)?)+)",
    re.IGNORECASE,
)
_PART = re.compile(r"(\d+(?:\.\d+)?)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|min|m|seconds?|secs?|sec|s)\b", re.IGNORECASE)
_RETRY_AFTER = re.compile(r"retry[- ]after[:\s]+(\d{1,7})\b", re.IGNORECASE)
_ISO = re.compile(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)")
_CLOCK = re.compile(
    r"resets?\s+(?:at\s+|on\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?:\s*\(([^)]+)\))?",
    re.IGNORECASE,
)


def is_limit(text: str) -> bool:
    return bool(_LIMIT.search(str(text or "")))


def reset_after(text: str, paused_at: float, *, attempt: int = 0) -> float:
    """Epoch seconds when a limit in ``text`` resets (plus a small margin)."""
    words = str(text or "")
    found = _DURATION.search(words)
    if found:
        seconds = sum(float(amount) * _UNITS[unit.lower()] for amount, unit in _PART.findall(found.group(1)))
        if seconds > 0:
            return paused_at + min(seconds, MAX_WAIT_SECONDS) + MARGIN_SECONDS
    found = _RETRY_AFTER.search(words)
    if found:
        return paused_at + min(int(found.group(1)), MAX_WAIT_SECONDS) + MARGIN_SECONDS
    for value in _ISO.findall(words):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "T"))
        except ValueError:
            continue
        if moment.tzinfo is None:
            moment = moment.astimezone()
        at = moment.timestamp()
        if paused_at - 3600 < at < paused_at + MAX_WAIT_SECONDS:
            return max(at, paused_at) + MARGIN_SECONDS
    found = _CLOCK.search(words)
    if found:
        hour, minute, meridiem, zone = int(found.group(1)), int(found.group(2) or 0), found.group(3), found.group(4)
        if meridiem:
            hour = hour % 12 + (12 if meridiem.lower() == "pm" else 0)
        if 0 <= hour < 24 and 0 <= minute < 60:
            tz = None
            if zone:
                try:
                    from zoneinfo import ZoneInfo
                    tz = ZoneInfo(zone.strip())
                except Exception:
                    tz = None
            start = datetime.fromtimestamp(paused_at, tz or timezone.utc).astimezone(tz) if tz else \
                datetime.fromtimestamp(paused_at).astimezone()
            moment = start.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if moment.timestamp() <= paused_at:
                moment += timedelta(days=1)
            return moment.timestamp() + MARGIN_SECONDS
    # No time given: a short wait that grows with each attempt.
    return paused_at + FALLBACK_SECONDS * (2 ** min(attempt, 4))


def plan(goal: dict[str, Any], *, attempt: int = 0) -> dict[str, Any] | None:
    """When Nexus will resume this goal by itself, or None if it will not."""
    if goal.get("status") != "paused":
        return None
    note = str(goal.get("note") or "")
    if not note.startswith(PROVIDER_PAUSE):
        return None
    errors = [note] + [str(one.get("last_error") or "") for one in goal.get("tasks") or [] if isinstance(one, dict)]
    text = next((one for one in errors if is_limit(one)), "")
    if not text:
        return None
    paused_at = float(goal.get("updated_ms") or time.time() * 1000) / 1000
    at = reset_after(" ".join(errors), paused_at, attempt=attempt)
    return {"schema_version": 1, "contract": CONTRACT, "at_ms": int(at * 1000),
            "paused_ms": int(paused_at * 1000), "attempt": attempt,
            "gives_up": attempt >= MAX_AUTOMATIC_RESUMES,
            "reason": "A provider usage limit was reached; the goal resumes after it resets."}
