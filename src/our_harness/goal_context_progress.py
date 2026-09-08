"""Recognize repeated tool-only continuations without capping exploration.

The owning authenticated goal snapshot persists this small record. A scope,
lease, call ID or restart is not evidence of progress. New arguments, observed
contents, a teammate/user message, or a changed project/route contract is.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Callable

from .models import HarnessError


SCHEMA_VERSION = 1
MAX_IDENTICAL_REPEATS = 4
CONTRACT = {
    "schema_version": SCHEMA_VERSION,
    "comparison": "tool-name-arguments-semantic-observation/v1",
    "continuity": "authenticated-step-identity-not-session-or-lease/v1",
    "freshness": "project-objective-route-and-other-participant-messages/v1",
    "conversation_reads": "other-participant-content-without-own-request-echo/v1",
    "max_identical_repeats": MAX_IDENTICAL_REPEATS,
}
PAUSE_REASON = (
    "The same context-tool request returned the same result repeatedly. "
    "No new project evidence or teammate message arrived. "
    "Resume with a different question, file range, or next action to continue."
)


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def contract_fingerprint() -> str:
    return _digest(CONTRACT)


def _visible_other_messages(messages: object, speaker_id: str) -> list[dict[str, Any]]:
    return [
        {key: message[key] for key in (
            "id", "sequence", "agent_id", "summary", "objective_epoch",
            "offset", "next_offset", "has_more_characters", "total_characters",
        ) if key in message}
        for message in messages if isinstance(message, dict)
        and str(message.get("agent_id") or "") != speaker_id
        and message.get("visibility") != "operator_only"
        and (message.get("visibility") != "agent_only"
             or (message.get("recipient") or {}).get("agent_id") == speaker_id)
    ] if isinstance(messages, list) else []


def _observation(name: str, value: object, speaker_id: str,
                 normalize: Callable[..., object]) -> object:
    result = normalize(value, verification=name == "run_selected_verification")
    if name != "read_shared_conversation" or not isinstance(result, dict):
        return result
    # Each request publishes its own summary. Reading that same archive must
    # not manufacture progress merely because our requests made it longer.
    # Explicit argument cursors still distinguish real pagination requests.
    return {
        "messages": _visible_other_messages(result.get("messages"), speaker_id),
        "coverage": result.get("coverage"),
        "error": result.get("error"),
    }


def observe(
    previous: object, step: dict[str, Any], *, binding: dict[str, Any],
    speaker_id: str, messages: object, normalize: Callable[..., object],
) -> dict[str, Any]:
    """Return the next durable record for one fully receipted context step.

    ``binding`` must contain goal/task identity and the non-secret current
    project/objective/provider fingerprints, excluding session IDs and leases.
    The caller supplies the engine's semantic result normalizer. Invoke after
    a step is complete, and reset the record after an accepted non-tool action.
    Older engine contracts automatically start a fresh comparison episode.
    """
    if not isinstance(step, dict) or step.get("state") != "complete":
        return copy.deepcopy(previous) if isinstance(previous, dict) else {}
    calls = step.get("calls") or []
    if not calls:
        return copy.deepcopy(previous) if isinstance(previous, dict) else {}
    step_id = str(step.get("step_id") or "")
    if not step_id:
        raise HarnessError("A completed context progress step needs its durable identity")
    call_ids = [str(call.get("call_id") or "") for call in calls if isinstance(call, dict)]
    results = step.get("results") or []
    result_ids = [str(result.get("call_id") or "") for result in results if isinstance(result, dict)]
    if len(call_ids) != len(calls) or len(set(call_ids)) != len(call_ids) or not all(call_ids) \
            or len(result_ids) != len(results) or len(set(result_ids)) != len(result_ids) \
            or set(result_ids) != set(call_ids):
        raise HarnessError("Context progress requires one durable result for every distinct tool call")
    by_call = {result["call_id"]: result for result in results}
    observations = []
    for call in calls:
        name = str(call.get("name") or "")
        result = by_call[call["call_id"]]
        if str(result.get("name") or "") != name:
            raise HarnessError("A context progress result does not match its requested tool")
        observations.append({
            "name": name, "arguments": call.get("arguments") or {},
            "result": _observation(name, result.get("result"), speaker_id, normalize),
            "error": result.get("error") or "",
        })
    observation_digest = _digest(observations)
    binding_digest = _digest({
        "context": binding, "speaker_id": speaker_id,
        "other_messages": _visible_other_messages(messages, speaker_id),
    })
    fingerprint = contract_fingerprint()
    held = previous if isinstance(previous, dict) else {}
    compatible = held.get("schema_version") == SCHEMA_VERSION \
        and held.get("contract_fingerprint_sha256") == fingerprint
    if compatible:
        if type(held.get("identical_repeats")) is not int \
                or not 0 <= held["identical_repeats"] <= MAX_IDENTICAL_REPEATS \
                or not str(held.get("last_step_id") or "") \
                or any(not re.fullmatch(r"[0-9a-f]{64}", str(held.get(key) or "")) for key in (
                    "binding_sha256", "observation_sha256",
                )) \
                or held.get("state") not in {"tracking", "paused"}:
            raise HarnessError("The saved context progress record is malformed")
        if held.get("binding_sha256") == binding_digest and held.get("last_step_id") == step_id:
            if held.get("observation_sha256") != observation_digest:
                raise HarnessError("A completed context step changed after its progress receipt")
            return copy.deepcopy(held)
    same = compatible and held.get("binding_sha256") == binding_digest \
        and held.get("observation_sha256") == observation_digest
    repeats = min(MAX_IDENTICAL_REPEATS, int(held.get("identical_repeats") or 0) + 1) if same else 0
    paused = repeats >= MAX_IDENTICAL_REPEATS
    return {
        "schema_version": SCHEMA_VERSION, "contract_fingerprint_sha256": fingerprint,
        "binding_sha256": binding_digest, "observation_sha256": observation_digest,
        "last_step_id": step_id, "identical_repeats": repeats,
        "state": "paused" if paused else "tracking",
        "reason": PAUSE_REASON if paused else "",
        "tool_names": [str(call.get("name") or "") for call in calls],
    }
