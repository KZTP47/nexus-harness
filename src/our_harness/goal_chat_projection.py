"""Lossless shared-dialogue projection into an authenticated saved chat.

The goal event journal is bounded operational telemetry. Public messages come
from the separate dialogue archive, including when no renderer was polling.
"""

from __future__ import annotations

import re
import json
import copy
from datetime import datetime, timezone
from typing import Any

from . import chat
from .config import LoadedConfig
from .goal_chat_progress import tool_outcome
from .goal_delivery import report_metadata
from .redaction import CredentialRedactor


def _timestamp(milliseconds: object) -> str:
    try:
        return datetime.fromtimestamp(int(milliseconds or 0) / 1000, timezone.utc).isoformat(
            timespec="milliseconds",
        ).replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError, OSError):
        raise chat.ChatError("Saved team activity has an invalid timestamp") from None


def _later(timestamp: str, earlier: str) -> bool:
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")) > datetime.fromisoformat(
            earlier.replace("Z", "+00:00"),
        )
    except (ValueError, TypeError):
        # Older imported transcript rows can carry display-only timestamps.
        # Their original position is retained rather than guessed.
        return False


def _tool_error(result: dict[str, Any] | None) -> str:
    direct = str((result or {}).get("error") or "")
    if direct:
        return direct
    value = (result or {}).get("result")
    if not isinstance(value, dict) or value.get("status") != "error":
        return ""
    # Agent tools return failed execution as a result envelope rather than
    # throwing. Its public diagnostic lives in content; successful file reads
    # can also contain JSON with an "error" key, so require failure status.
    content = value.get("content")
    if not isinstance(content, str) or not content.strip():
        return str(value.get("error") or "")
    try:
        decoded = json.loads(content)
    except (ValueError, TypeError):
        return content
    if isinstance(decoded, dict) and isinstance(decoded.get("error"), str):
        return decoded["error"]
    return content


def _project_tools(
    saved: list[chat.Said], goal: dict[str, Any], agents: dict[str, Any],
    binding: dict[str, Any], redactor: CredentialRedactor,
) -> list[chat.Said]:
    """Recover tool activity from authenticated steps, even with no UI polling.

    Tool steps belong to the durable goal snapshot, independently of the
    bounded event journal. Only executed tool inputs/outputs are projected;
    provider response envelopes and private reasoning are never inspected.
    A stable per-call identity updates pending activity in place. A newer
    saved goal revision cannot be downgraded by a delayed projection poll.
    """
    output = list(saved)
    for task in goal.get("tasks", []):
        if not isinstance(task, dict):
            continue
        for step in task.get("context_steps", []):
            if not isinstance(step, dict) or not step.get("step_id"):
                continue
            # A task can have been reassigned. Only a frozen step identity
            # proves which agent requested these tools; legacy steps remain
            # truthful team activity without guessing their original owner.
            agent_id = str(step.get("agent_id") or "")
            agent = agents.get(agent_id, {})
            calls = step.get("calls") or []
            if not calls and step.get("requested_files"):
                calls = [{"call_id": "requested-files", "name": "request_file_context",
                          "arguments": {"paths": step["requested_files"]}}]
            for call in calls:
                if not isinstance(call, dict) or not call.get("call_id") or not call.get("name"):
                    continue
                result = next((one for one in step.get("results", [])
                               if isinstance(one, dict) and one.get("call_id") == call["call_id"]), None)
                error = _tool_error(result)
                value = (result or {}).get("result")
                outcome, reason = tool_outcome(str(call["name"]), value, error)
                status = outcome if result is not None else (
                    "finished" if call["name"] == "request_file_context" else
                    "superseded" if step.get("state") == "superseded" else "requested"
                )
                evidence = {
                    "schema_version": 1, "kind": "nexus_tool_activity",
                    "name": str(call["name"]), "status": status,
                    "arguments": call.get("arguments") or {},
                }
                if result is not None:
                    evidence["result"] = value
                if error:
                    evidence["error"] = error
                if reason:
                    evidence["summary"] = reason[:500]
                identity = chat._long_horizon_event_id(
                    "tool-activity-v1", goal["goal_id"], task.get("id"), step["step_id"], call["call_id"],
                )
                previous_index = next((index for index, one in enumerate(output)
                                       if one.correlation.get("event_id") == identity), None)
                revision = int(goal.get("revision") or 0)
                if previous_index is not None and int(output[previous_index].correlation.get("goal_revision") or 0) > revision:
                    continue
                text = json.dumps(redactor.value(evidence), ensure_ascii=False, indent=2)
                at = _timestamp((result or {}).get("at_ms") or (
                    step.get("completed_ms") if result is not None else 0
                ) or step.get("created_ms"))
                if previous_index is not None and output[previous_index].text == text and output[previous_index].at == at:
                    continue
                row = chat.Said(
                    "them", chat._checked_answer(text, "Saved tool activity"),
                    at,
                    speaker_id="nexus", speaker_name="Nexus",
                    phase="agent_tool", correlation={**binding, "event_id": identity,
                        "kind": "long_horizon_tool_activity", "goal_revision": revision,
                        "task_id": str(task.get("id") or "")},
                )
                # The request is made by the agent; the record of its execution
                # is generated by Nexus, never presented as an agent quotation.
                row.speaker_name = str(agent.get("name") or "Team") + " · tool activity"
                if previous_index is not None:
                    output.pop(previous_index)
                # Completed output belongs at its return time, after the tool
                # request/start milestones. Preserve the stable expansion ID.
                insertion = next((index for index, one in enumerate(output)
                                  if one.correlation.get("goal_id") == goal["goal_id"]
                                  and one.at and _later(one.at, row.at)), len(output))
                output.insert(insertion, row)
    return output


def cursor(config: LoadedConfig, route: str, goal_id: str, *, filed_as: str) -> int:
    exact = chat._exact_long_horizon_identity(goal_id, "goal ID")
    return max((int(one.correlation.get("goal_dialogue_cursor") or 0)
                for one in chat.read_it(config, route, filed_as)
                if one.correlation.get("goal_id") == exact), default=0)


def keep_page(
    config: LoadedConfig, route: str, goal: dict[str, Any], page: dict[str, Any],
    *, filed_as: str, chat_id: str = "", project_id: str = "", lead_id: str = "",
) -> dict[str, Any]:
    """Merge one verified archive page, preserving proven historical ordering.

    Recovered archive rows can precede already projected journal rows. A keyed
    snapshot event records that reconstruction without rewriting earlier audit
    records. The project lock spans read and replacement, including other
    processes, so recovery cannot overwrite a simultaneous chat append.
    """

    goal_id = chat._exact_long_horizon_identity(goal.get("goal_id"), "goal ID")
    request_id = chat._exact_long_horizon_identity(goal.get("request_id"), "request ID")
    conversation_id = chat._exact_long_horizon_identity(
        chat_id or goal.get("conversation_id"), "chat ID",
    )
    project = goal.get("project") if isinstance(goal.get("project"), dict) else {}
    selected_project = chat._exact_long_horizon_identity(project_id or project.get("id"), "project ID")
    selected_lead = chat._exact_long_horizon_identity(lead_id or goal.get("lead_agent_id"), "lead agent ID")
    if page.get("schema_version") != 1 or page.get("goal_id") != goal_id:
        raise chat.ChatError("A dialogue archive page has the wrong durable goal identity")
    raw_messages = page.get("messages")
    if not isinstance(raw_messages, list):
        raise chat.ChatError("A dialogue archive page has an invalid message list")
    agents = {str(one.get("id") or ""): one for one in goal.get("agents", []) if isinstance(one, dict)}
    redactor = CredentialRedactor(config)
    projection_result: dict[str, Any] = {}

    def project(turns: list[chat.Said]) -> list[chat.Said]:
        prompt = chat._long_horizon_prompt_binding(turns, request_id)
        if prompt is None:
            projection_result.update({"binding_missing": True, "projected_dialogue_cursor": 0})
            return turns
        chat._require_long_horizon_binding(
            turns, request_id, conversation_id, selected_project, selected_lead,
            str(prompt.get("intent_sha256") or ""),
        )
        prior = max((int(one.correlation.get("goal_dialogue_cursor") or 0)
                     for one in turns if one.correlation.get("goal_id") == goal_id), default=0)
        prior_events = max((int(one.correlation.get("goal_event_cursor") or 0)
                            for one in turns if one.correlation.get("goal_id") == goal_id), default=0)
        coverage = page.get("coverage") if isinstance(page.get("coverage"), dict) else {}
        # Old shared snapshots counted exactly provider acknowledgements and
        # team steering. Migration freezes that count and its event boundary.
        # Cardinality plus ordinal and exact identity/text can prove a match
        # even after the source event was retired. Matching text alone cannot:
        # an agent may legitimately say the same words on several turns.
        legacy_count = int(coverage.get("legacy_dialogue_sequence") or 0)
        legacy_boundary = int(coverage.get("legacy_event_seq") or 0)
        legacy_turns = [one for one in turns
                        if one.correlation.get("goal_id") == goal_id
                        and one.correlation.get("source_goal_event_id")
                        and one.correlation.get("source_goal_event_type") in {"provider_acknowledged", "goal_steered"}
                        and 0 < int(one.correlation.get("source_goal_event_seq")
                                    or one.correlation.get("goal_event_cursor") or 0) <= legacy_boundary]
        legacy_ids = {one.correlation["source_goal_event_id"] for one in legacy_turns}
        legacy_positions = [int(one.correlation.get("source_goal_event_seq")
                                or one.correlation.get("goal_event_cursor") or 0) for one in legacy_turns]
        legacy_by_ordinal = dict(enumerate(legacy_turns, 1)) if legacy_count \
            and len(legacy_turns) == len(legacy_ids) == len(set(legacy_positions)) == legacy_count \
            and legacy_positions == sorted(legacy_positions) else {}
        saved = copy.deepcopy(turns)
        accepted = prior
        seen_sequences: set[int] = set()
        for message in raw_messages:
            if not isinstance(message, dict) or message.get("goal_id", goal_id) != goal_id:
                raise chat.ChatError("An archived message has the wrong durable goal identity")
            try:
                sequence = int(message.get("sequence") or 0)
                event_sequence = int(message.get("source_goal_event_seq") or 0)
                offset = int(message.get("offset") or 0)
                total = int(message.get("total_characters", len(str(message.get("summary") or ""))))
            except (TypeError, ValueError) as exc:
                raise chat.ChatError("An archived message has invalid sequence or text metadata") from exc
            message_id = str(message.get("id") or "")
            source_id = str(message.get("source_goal_event_id") or "")
            if sequence < 1 or sequence > 9_007_199_254_740_991 or sequence in seen_sequences \
                    or event_sequence < 0 or event_sequence > 9_007_199_254_740_991 \
                    or not message_id or len(message_id) > 160 \
                    or source_id and not re.fullmatch(r"[0-9a-f]{32}", source_id):
                raise chat.ChatError("An archived message has invalid durable identity metadata")
            seen_sequences.add(sequence)
            if sequence <= prior:
                previous = next((one for one in saved
                    if one.correlation.get("goal_id") == goal_id
                    and one.correlation.get("source_dialogue_id") == message_id), None)
                if previous is not None and not message.get("agent_id") and previous.attachments != chat._long_horizon_public_attachments(redactor, message.get("attachments")):
                    raise chat.ChatError("An archived message identity has conflicting saved attachments")
                continue
            words = str(message.get("summary") or "")
            if offset or len(words) != total or message.get("has_more_characters"):
                raise chat.ChatError("Nexus requires the complete archived message before displaying it")
            if not words.strip():
                raise chat.ChatError("An archived public message has no text")
            agent_id = str(message.get("agent_id") or "")
            if agent_id and agent_id not in agents:
                raise chat.ChatError("An archived message names an agent outside this goal")
            agent = agents.get(agent_id, {})
            recipient = message.get("recipient")
            recipient = recipient if isinstance(recipient, dict) else {}
            target_id = str(recipient.get("agent_id") or "")
            target_name = "the team"
            if message.get("action") == "ask_user" or recipient.get("kind") == "user":
                target_id, target_name = "", "You"
            elif recipient.get("kind") == "agent" and target_id in agents and target_id != agent_id:
                target_name = str(recipient.get("name") or agents[target_id].get("name") or target_id)
            elif recipient.get("kind") == "unknown":
                target_id, target_name = "", str(recipient.get("name") or "original recipient (unavailable)")
            else:
                target_id = ""
            correlation = {
                "schema_version": 1,
                "event_id": chat._long_horizon_event_id(
                    "agent-event" if source_id else "dialogue-message", goal_id, source_id or message_id,
                ),
                "kind": "long_horizon_agent_event" if agent_id else "long_horizon_user_event",
                "request_id": request_id, "chat_id": conversation_id,
                "project_id": selected_project, "lead_id": selected_lead,
                "goal_id": goal_id, "task_id": str(message.get("task_id") or ""),
                "source_dialogue_id": message_id, "goal_dialogue_cursor": sequence,
                "source_goal_event_seq": event_sequence,
            }
            if source_id:
                correlation.update({
                    "source_goal_event_id": source_id,
                    "source_goal_event_type": str(message.get("source_goal_event_type")
                                                  or ("provider_acknowledged" if agent_id else "goal_steered")),
                })
            check = message.get("nexus_check")
            if agent_id and isinstance(check, dict) and check.get("verdict"):
                # Nexus's own check of a done claim, shown beside it and never mixed into the agent's words.
                correlation.update({"nexus_check_verdict": str(check.get("verdict") or "")[:1_200],
                                    "nexus_check_page": str(check.get("page") or "")[:300],
                                    "nexus_check_screenshot": str(check.get("screenshot") or "")[:1_000]})
            provider_activity = message.get("provider_activity") if message.get("phase") == "provider_activity" else None
            if agent_id and not provider_activity:
                correlation.update(report_metadata(goal))
            timestamp = _timestamp(message.get("at_ms"))
            row = chat.Said(
                "them" if agent_id else "you", chat._checked_answer(redactor.text(words), "Archived message"),
                timestamp, model="nexus/long-horizon-agent-event-v1" if agent_id else "",
                speaker_id=agent_id or "user", speaker_name=str(agent.get("name") or agent_id) if agent_id else "You",
                speaker_route=str(agent.get("who") or ""), recipient_id=target_id,
                recipient_name=target_name,
                attachments=chat._long_horizon_public_attachments(redactor, message.get("attachments")) if not agent_id else [],
                phase=("agent_progress" if agent_id and str(message.get("phase") or "") != "action"
                       else "agent_discussion" if agent_id else "user_steering"), correlation=correlation,
            )
            if provider_activity:
                from .provider_activity import FINGERPRINT
                if provider_activity.get("contract_fingerprint") != FINGERPRINT or not message.get("activity_id"):
                    raise chat.ChatError("Unsupported archived provider activity")
                row.correlation["kind"] = "long_horizon_provider_activity"
                row.correlation["provider_activity_id"] = message["activity_id"]
                if provider_activity.get("kind") == "reasoning_summary":
                    from .provider_activity import SUMMARY_CONTRACTS
                    if provider_activity.get("summary_contract") not in SUMMARY_CONTRACTS:
                        raise chat.ChatError("Unsupported public reasoning summary")
                    row.phase = "reasoning_summary"
                if provider_activity.get("kind") == "notice":
                    row.speaker_id, row.speaker_name, row.phase = "nexus", "Nexus", "nexus_gap"
                if provider_activity.get("kind") in {"message", "reasoning_summary"} and provider_activity.get("truncated"):
                    row.text += "\n[Provider message truncated at the display limit.]"
                if provider_activity.get("kind") == "tool":
                    evidence = {"schema_version": 1, "kind": "nexus_tool_activity",
                                "origin": "provider", "name": provider_activity.get("name", "provider_tool"),
                                "status": provider_activity.get("status", "requested"),
                                "arguments": provider_activity.get("arguments", {}),
                                "result": provider_activity.get("result", provider_activity.get("details", ""))}
                    if provider_activity.get("truncated"):
                        evidence["summary"] = "This provider event exceeded the display limit; details were truncated."
                    row.text = json.dumps(redactor.value(evidence), ensure_ascii=False)
                    row.phase = "agent_tool"
                    row.speaker_name = str(agent.get("name") or "Team") + " · provider tool"
                    row.correlation.update({"kind": "long_horizon_tool_activity",
                                            "event_id": chat._long_horizon_event_id("provider-tool", goal_id, message["activity_id"])})
                    prior_activity = next((one for one in saved if one.correlation.get("goal_id") == goal_id
                                           and one.correlation.get("provider_activity_id") == message["activity_id"]), None)
                    if prior_activity is not None:
                        if int(prior_activity.correlation.get("goal_dialogue_cursor") or 0) >= sequence:
                            accepted = max(accepted, sequence)
                            continue
                        saved.remove(prior_activity)
            if not source_id:
                proven = legacy_by_ordinal.get(int(message.get("legacy_dialogue_sequence") or 0))
                if proven is not None and proven.text == row.text \
                        and proven.speaker_id == row.speaker_id \
                        and str(proven.correlation.get("task_id") or "") == correlation["task_id"] \
                        and proven.correlation.get("source_goal_event_type") == message.get("source_goal_event_type"):
                    source_id = str(proven.correlation["source_goal_event_id"])
                    row.correlation.update({
                        "event_id": chat._long_horizon_event_id("agent-event", goal_id, source_id),
                        "source_goal_event_id": source_id,
                        "source_goal_event_type": proven.correlation["source_goal_event_type"],
                    })
                    if proven.correlation.get("source_goal_event_seq"):
                        row.correlation["source_goal_event_seq"] = proven.correlation["source_goal_event_seq"]
                else:
                    # The archive proves the original speech, but not which
                    # old bubble (if any) contained it. Present that evidence
                    # as recovered history, never as a new live reply.
                    row.phase = "recovered_history"
                    row.correlation["kind"] = "long_horizon_recovered_dialogue"
            matching = next((index for index, one in enumerate(saved)
                             if one.correlation.get("goal_id") == goal_id and (
                                 one.correlation.get("source_dialogue_id") == message_id
                                 or source_id and one.correlation.get("source_goal_event_id") == source_id
                             )), None)
            if matching is not None:
                previous = saved[matching]
                if previous.correlation.get("source_dialogue_id") == message_id \
                        and previous.text != row.text:
                    raise chat.ChatError("An archived message identity has conflicting saved text")
                if previous.correlation.get("source_dialogue_id") == message_id and previous.attachments != row.attachments:
                    raise chat.ChatError("An archived message identity has conflicting saved attachments")
                if "goal_event_cursor" in previous.correlation:
                    row.correlation["goal_event_cursor"] = previous.correlation["goal_event_cursor"]
                saved[matching] = row
            else:
                insertion = next((index for index, one in enumerate(saved)
                                  if one.correlation.get("goal_id") == goal_id and (
                                      int(one.correlation.get("goal_dialogue_cursor") or 0) > sequence
                                      or event_sequence and int(one.correlation.get("source_goal_event_seq")
                                                               or one.correlation.get("goal_event_cursor") or 0) > event_sequence
                                      or event_sequence and "goal_status_event_cursor" in one.correlation
                                      and int(one.correlation["goal_status_event_cursor"]) >= event_sequence
                                  )), None)
                if insertion is None:
                    insertion = next((index for index, one in enumerate(saved)
                                      if one.correlation.get("goal_id") == goal_id
                                      and one.correlation.get("kind") == "long_horizon_status"
                                      and "goal_status_event_cursor" not in one.correlation
                                      and (not event_sequence or event_sequence <= prior_events)
                                      and one.correlation.get("goal_status") in {
                                          "paused", "waiting_for_user", "failed", "complete", "cancelled",
                                      }), len(saved))
                saved.insert(insertion, row)
            accepted = max(accepted, sequence)
        if coverage.get("status") == "partial_legacy":
            gap_id = chat._long_horizon_event_id("dialogue-legacy-gap", goal_id)
            if not any(one.correlation.get("event_id") == gap_id for one in saved):
                saved.append(chat.Said(
                    "them", "Some older team messages predate the complete conversation archive and "
                    "could not be recovered. All retained messages are shown; Nexus has not invented missing replies.",
                    chat._now(), speaker_id="nexus", speaker_name="Nexus", recipient_name="You", phase="nexus_gap",
                    correlation={"schema_version": 1, "event_id": gap_id,
                                 "kind": "long_horizon_dialogue_gap", "goal_id": goal_id,
                                 "request_id": request_id, "chat_id": conversation_id,
                                 "project_id": selected_project, "lead_id": selected_lead},
                ))
        # Upgrade previously displayed replies even when the archive cursor is
        # already current. Keep the original agent words and identity intact.
        # Completion now does not make an earlier working-copy claim true.
        delivery = report_metadata(goal)
        if delivery:
            for row in saved:
                if row.correlation.get("goal_id") == goal_id and row.correlation.get("kind") in {
                    "long_horizon_agent_event", "long_horizon_recovered_dialogue",
                }:
                    row.correlation.update(delivery)
        projection_result.update({"projected_dialogue_cursor": accepted, "binding_missing": False})
        return _project_tools(saved, goal, agents, {
            "schema_version": 1, "goal_id": goal_id, "request_id": request_id,
            "chat_id": conversation_id, "project_id": selected_project, "lead_id": selected_lead,
        }, redactor)

    with chat._the_lock_for(chat._filed_under(filed_as or route)):
        # First-read migration precedes the final atomic transform.
        chat.read_it(config, route, filed_as)
        chat._keep_it(config, route, [], filed_as, replace_projection=True, transform_projection=project)
    return projection_result


# ---- Replay receipts -------------------------------------------------------
#
# Listing goals copies each goal's progress into its chat. That copy is
# idempotent, but finding out that there is nothing new meant re-reading and
# re-verifying the whole transcript several times per goal, for every goal,
# on every listing: tens of seconds before a board's chats appeared. A receipt
# records the exact goal snapshot and the exact transcript bytes a completed
# replay left behind. Only the replay is skipped when both are unchanged;
# every read that shows a transcript still verifies it in full, and any
# change to the goal, the transcript, its anchor or this code replays again.

RECEIPT_SCHEMA = 1
RECEIPT_CONTRACT = "goal-chat-projection-receipt/v1"
MOST_RECEIPTS_PER_TRANSCRIPT = 64


def _engine_fingerprint() -> str:
    """The replay code itself: its two modules and the functions that drive it.

    Deliberately not whole large modules, so an unrelated change elsewhere in
    chat.py or server.py does not force every chat to replay its history.
    """

    from pathlib import Path
    import hashlib
    import inspect

    digest = hashlib.sha256(RECEIPT_CONTRACT.encode("utf-8"))
    here = Path(__file__).resolve().parent
    for name in ("goal_chat_projection.py", "goal_chat_progress.py"):
        try:
            digest.update((here / name).read_bytes())
        except OSError:
            digest.update(name.encode("utf-8"))
    from . import server

    for function in (
        chat.long_horizon_event_cursor, chat.keep_long_horizon_events,
        chat.keep_long_horizon_status, chat._keep_it,
        server.HarnessHTTPServer.project_long_horizon_chat_statuses,
        server.HarnessHTTPServer._project_long_horizon_dialogue,
    ):
        try:
            digest.update(inspect.getsource(function).encode("utf-8"))
        except (OSError, TypeError):
            digest.update(getattr(function, "__qualname__", "?").encode("utf-8"))
    return digest.hexdigest()


_ENGINE_HELD: list[str] = []


def _engine() -> str:
    if not _ENGINE_HELD:
        _ENGINE_HELD.append(_engine_fingerprint())
    return _ENGINE_HELD[0]


def _goal_fingerprint(goal: dict[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(json.dumps(
        goal, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()


def _transcript_state(config: LoadedConfig, route: str, filed_as: str):
    """(receipt path, digest of transcript and anchor bytes) or None."""

    import hashlib
    from .runtime_integrity import runtime_root

    events = chat.where_it_is_kept(config, route, filed_as).with_suffix(".events.jsonl")
    try:
        body = events.read_bytes()
    except OSError:
        return None
    anchor = chat._transcript_anchor_path(events)
    try:
        anchored = anchor.read_bytes()
    except OSError:
        anchored = b""
    identity = hashlib.sha256(str(events.resolve()).encode("utf-8")).hexdigest()
    digest = hashlib.sha256()
    for part in (identity.encode("utf-8"), body, b"\0", anchored):
        digest.update(part)
    return runtime_root() / "goal-projection-receipts" / f"{identity}.json", digest.hexdigest()


def _read_receipt(where) -> dict[str, Any]:
    try:
        held = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(held, dict) or held.get("schema_version") != RECEIPT_SCHEMA \
            or held.get("contract") != RECEIPT_CONTRACT or held.get("engine") != _engine() \
            or not isinstance(held.get("goals"), dict):
        return {}
    return held["goals"]


def already_projected(config: LoadedConfig, route: str, goal: dict[str, Any], *, filed_as: str) -> bool:
    """Whether this exact goal snapshot was already replayed into these exact bytes."""

    state = _transcript_state(config, route, filed_as)
    if state is None:
        return False
    where, digest = state
    seen = _read_receipt(where).get(str(goal.get("goal_id") or ""))
    return isinstance(seen, dict) and seen.get("goal") == _goal_fingerprint(goal) \
        and seen.get("transcript") == digest


def remember_projected(config: LoadedConfig, route: str, goals: list[dict[str, Any]], *, filed_as: str) -> None:
    """Record that every goal given is fully replayed into the transcript as it is now."""

    from .runtime_integrity import atomic_text

    state = _transcript_state(config, route, filed_as)
    if state is None or not goals:
        return
    where, digest = state
    with chat._the_lock_for(chat._filed_under(filed_as or route)):
        kept = {key: value for key, value in _read_receipt(where).items() if isinstance(value, dict)}
        for goal in goals:
            goal_id = str(goal.get("goal_id") or "")
            if goal_id:
                kept.pop(goal_id, None)
                kept[goal_id] = {"goal": _goal_fingerprint(goal), "transcript": digest}
        while len(kept) > MOST_RECEIPTS_PER_TRANSCRIPT:
            kept.pop(next(iter(kept)))
        try:
            atomic_text(where, json.dumps({
                "schema_version": RECEIPT_SCHEMA, "contract": RECEIPT_CONTRACT,
                "engine": _engine(), "goals": kept,
            }, sort_keys=True) + "\n")
        except OSError:
            # A receipt only saves work. Without it the next listing replays.
            return
