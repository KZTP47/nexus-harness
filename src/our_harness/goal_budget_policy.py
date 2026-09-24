"""User-owned cumulative call limits for durable goals.

Agents lead; Nexus only supports. A goal has no cumulative (lifetime) call
ceiling unless the user set one explicitly. This holds for shared and adaptive
team goals alike. Finite per-response tool envelopes belong to their response,
not a goal's lifetime, and are unaffected.

Saved budgets keep their consumed counters. A saved ceiling whose recorded
provenance says it was an engine default (the old adaptive 1,000 provider /
500 context-tool calls) is no longer enforced. Budgets saved before provenance
was recorded keep a finite ceiling only when it differs from those historical
defaults, because such a number was most likely chosen by the user.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .models import HarnessError


SCHEMA_VERSION = 2
# Historical engine defaults. They are recognised in saved state so they can
# be retired; they are never applied to new goals.
LEGACY_DEFAULTS = {"provider_calls": 1_000, "context_tool_calls": 500}
PREVIOUS_CONTRACT = {
    "schema_version": 1,
    "name": "durable-goal-call-limits",
    "shared_default": "unlimited",
    "explicit_zero": "unlimited",
    "explicit_positive": "exact-cumulative-limit-without-clamping",
    "legacy": "preserve-saved-finite-ceilings-and-consumed-counters",
    "accounting": "monotone-consumption-across-response-restart-and-steering",
}
CONTRACT = {
    "schema_version": SCHEMA_VERSION,
    "name": "durable-goal-call-limits",
    "default": "unlimited-unless-user-set",
    "explicit_zero": "unlimited",
    "explicit_positive": "exact-cumulative-limit-without-clamping",
    "recorded_engine_defaults": "not-enforced",
    "legacy": "unrecorded-historical-defaults-not-enforced-other-saved-ceilings-kept",
    "accounting": "monotone-consumption-across-response-restart-and-steering",
}
_SOURCES = {
    SCHEMA_VERSION: {"explicit", "default_unlimited"},
    1: {"explicit", "shared_default_unlimited", "adaptive_default_finite"},
}


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


CONTRACT_FINGERPRINT = _digest(CONTRACT)
PREVIOUS_CONTRACT_FINGERPRINT = _digest(PREVIOUS_CONTRACT)
_FINGERPRINTS = {SCHEMA_VERSION: CONTRACT_FINGERPRINT, 1: PREVIOUS_CONTRACT_FINGERPRINT}


def _nonnegative_integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HarnessError(f"{field} must be a nonnegative integer; zero means no cumulative call limit")
    return value


def create_budget(policy: dict[str, Any] | None = None, *, shared: bool) -> dict[str, Any]:
    """Capture the user's exact limits and their provenance for a new goal.

    Omitted limits are unlimited in every collaboration mode; only a limit the
    user supplied is recorded as ``explicit`` and enforced.
    """
    if policy is not None and not isinstance(policy, dict):
        raise HarnessError("The goal call-limit policy must be an object")
    supplied = policy if policy is not None else {}
    limits: dict[str, int] = {}
    sources: dict[str, str] = {}
    for counter in LEGACY_DEFAULTS:
        key = "max_" + counter
        if key in supplied:
            limits[key] = _nonnegative_integer(supplied[key], key)
            sources[key] = "explicit"
        else:
            limits[key] = 0
            sources[key] = "default_unlimited"
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
    version = record.get("schema_version") if isinstance(record, dict) else None
    if not isinstance(record, dict) or version not in _FINGERPRINTS \
            or record.get("contract_fingerprint_sha256") != _FINGERPRINTS[version] \
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
        if source not in _SOURCES[version]:
            valid = False
        elif source in {"shared_default_unlimited", "default_unlimited"}:
            valid = value == 0 and (source == "default_unlimited" or record["shared"])
        elif source == "adaptive_default_finite":
            valid = not record["shared"] and value == LEGACY_DEFAULTS[key.removeprefix("max_")]
        else:
            valid = True
        if not valid:
            raise HarnessError("The saved goal call-limit policy has invalid limit provenance")
    basis = {key: record[key] for key in (
        "schema_version", "contract_fingerprint_sha256", "shared", "limits", "sources",
    )}
    if record.get("fingerprint_sha256") != _digest(basis):
        raise HarnessError("The saved goal call-limit policy fingerprint changed")
    return record


def limit_source(budget: dict[str, Any], counter: str) -> str:
    """Return ``explicit``, ``saved`` (legacy, provenance unknown) or ``none``."""
    if not isinstance(budget, dict) or counter not in LEGACY_DEFAULTS:
        raise HarnessError("The goal call budget or counter is invalid")
    record = _record(budget)
    key = "max_" + counter
    if record is not None:
        return "explicit" if record["sources"][key] == "explicit" and record["limits"][key] else "none"
    if key not in budget:
        return "none"
    limit = _nonnegative_integer(budget[key], key)
    return "none" if limit == LEGACY_DEFAULTS[counter] else "saved"


def remaining(budget: dict[str, Any], counter: str) -> int | None:
    """Return remaining calls, or None when no user limit applies.

    Reads never migrate or reset counters. A limit the user set is exact.
    Recorded engine defaults and unrecorded historical defaults are not
    enforced. A legacy budget without provenance keeps any other saved
    finite ceiling, including zero, because it may have been the user's.
    """
    if not isinstance(budget, dict) or counter not in LEGACY_DEFAULTS:
        raise HarnessError("The goal call budget or counter is invalid")
    record = _record(budget)
    used = _nonnegative_integer(budget.get(counter, 0), counter)
    key = "max_" + counter
    if record is not None:
        limit = _nonnegative_integer(budget[key], key)
        if record["sources"][key] != "explicit" or limit == 0:
            return None
        return max(0, limit - used)
    if key not in budget:
        # Old context-tool budgets omitted the field and used the historical
        # default, which is no longer enforced. A budget missing its provider
        # ceiling entirely is malformed and grants no dispatch.
        return None if counter == "context_tool_calls" else 0
    limit = _nonnegative_integer(budget[key], key)
    if limit == LEGACY_DEFAULTS[counter]:
        return None
    return max(0, limit - used)


def exhausted(budget: dict[str, Any], counter: str) -> bool:
    return remaining(budget, counter) == 0


def exhausted_message(budget: dict[str, Any], counter: str) -> str:
    """Accurate wording for a reached limit, naming who set it."""
    name = "provider-call" if counter == "provider_calls" else "context-tool call"
    if limit_source(budget, counter) == "explicit":
        return f"The {name} limit you set for this goal was reached. Nothing was discarded."
    return f"The {name} limit saved with this older goal was reached. Nothing was discarded."
