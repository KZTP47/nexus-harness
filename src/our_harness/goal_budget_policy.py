"""User-owned cumulative call limits for durable collaborative goals.

Finite per-response tool envelopes belong to their response, not a goal's
lifetime. New shared goals have no cumulative call ceiling unless requested.
Old budgets retain their saved ceilings because they did not record whether
those numbers were defaults or an explicit user choice.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .models import HarnessError


SCHEMA_VERSION = 1
LEGACY_DEFAULTS = {"provider_calls": 1_000, "context_tool_calls": 500}
CONTRACT = {
    "schema_version": SCHEMA_VERSION,
    "name": "durable-goal-call-limits",
    "shared_default": "unlimited",
    "explicit_zero": "unlimited",
    "explicit_positive": "exact-cumulative-limit-without-clamping",
    "legacy": "preserve-saved-finite-ceilings-and-consumed-counters",
    "accounting": "monotone-consumption-across-response-restart-and-steering",
}


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


CONTRACT_FINGERPRINT = _digest(CONTRACT)


def _nonnegative_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HarnessError(f"{field} must be a nonnegative integer; zero means no cumulative call limit")
    return value


def create_budget(policy: dict[str, Any] | None = None, *, shared: bool) -> dict[str, Any]:
    """Capture exact limits and their provenance when admitting a new goal."""
    if policy is not None and not isinstance(policy, dict):
        raise HarnessError("The goal call-limit policy must be an object")
    supplied = policy if policy is not None else {}
    limits: dict[str, int] = {}
    sources: dict[str, str] = {}
    for counter, default in LEGACY_DEFAULTS.items():
        key = "max_" + counter
        if key in supplied:
            limits[key] = _nonnegative_integer(supplied[key], key)
            sources[key] = "explicit"
        else:
            limits[key] = 0 if shared else default
            sources[key] = "shared_default_unlimited" if shared else "adaptive_default_finite"
    basis = {
        "schema_version": SCHEMA_VERSION,
        "contract_fingerprint_sha256": CONTRACT_FINGERPRINT,
        "shared": bool(shared), "limits": limits, "sources": sources,
    }
    return {
        **{counter: 0 for counter in LEGACY_DEFAULTS}, **limits,
        "call_limit_policy": {**basis, "fingerprint_sha256": _digest(basis)},
    }


def _record(budget: dict[str, Any]) -> dict[str, Any] | None:
    if "call_limit_policy" not in budget:
        return None
    record = budget["call_limit_policy"]
    if not isinstance(record, dict) or record.get("schema_version") != SCHEMA_VERSION \
            or record.get("contract_fingerprint_sha256") != CONTRACT_FINGERPRINT \
            or not isinstance(record.get("shared"), bool) \
            or not isinstance(record.get("limits"), dict) \
            or not isinstance(record.get("sources"), dict):
        raise HarnessError("The saved goal call-limit policy has an unsupported contract")
    keys = {"max_" + counter for counter in LEGACY_DEFAULTS}
    if set(record["limits"]) != keys or set(record["sources"]) != keys:
        raise HarnessError("The saved goal call-limit policy has incomplete limit provenance")
    for key in keys:
        value = _nonnegative_integer(record["limits"][key], key)
        if key not in budget or _nonnegative_integer(budget[key], key) != value:
            raise HarnessError("The saved goal call-limit policy disagrees with its budget")
        source = record["sources"][key]
        if source == "shared_default_unlimited":
            valid = record["shared"] and value == 0
        elif source == "adaptive_default_finite":
            valid = not record["shared"] and value == LEGACY_DEFAULTS[key.removeprefix("max_")]
        else:
            valid = source == "explicit"
        if not valid:
            raise HarnessError("The saved goal call-limit policy has invalid limit provenance")
    basis = {key: record[key] for key in (
        "schema_version", "contract_fingerprint_sha256", "shared", "limits", "sources",
    )}
    if record.get("fingerprint_sha256") != _digest(basis):
        raise HarnessError("The saved goal call-limit policy fingerprint changed")
    return record


def remaining(budget: dict[str, Any], counter: str) -> int | None:
    """Return remaining calls, or None for a versioned unlimited allowance.

    Reads never migrate or reset counters. Legacy zero remains exhausted; it
    cannot become fresh unlimited authority just because software changed.
    Older context-tool budgets omitted their fields and used the 500-call
    default at dispatch, which is preserved here.
    """
    if not isinstance(budget, dict) or counter not in LEGACY_DEFAULTS:
        raise HarnessError("The goal call budget or counter is invalid")
    record = _record(budget)
    used = _nonnegative_integer(budget.get(counter, 0), counter)
    fallback = LEGACY_DEFAULTS[counter] if counter == "context_tool_calls" else 0
    limit = _nonnegative_integer(budget.get("max_" + counter, fallback), "max_" + counter)
    if record is not None and limit == 0:
        return None
    return max(0, limit - used)


def exhausted(budget: dict[str, Any], counter: str) -> bool:
    return remaining(budget, counter) == 0
