"""Reviewed recovery of saved chats after a CLI update or sign-in repair.

Reconnection grants no execution or retry permission. The saved work stays
paused and unknown provider/file effects retain their existing recovery rules.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
import shutil
from pathlib import Path
from typing import Any

from . import chat, swarm_chats
from .models import HarnessError
from .providers.base import create_provider
from .providers.registry import ProviderRegistry

CONTRACT = "saved-chat-provider-reconnect/v1"
_DIGEST = "effective_dispatch_fingerprint_sha256"
_BASE_FIELDS = (
    "route", "failure_context_version", "route_fingerprint_sha256",
    "transport_contract", "effective_dispatch_version", "effective_dispatch_contract",
)


def compatible(held: dict, current: dict) -> bool:
    """Only the observed executable fingerprint may change, never its contract."""
    return bool(held and current) and all(
        held.get(key) is not None and held.get(key) == current.get(key)
        for key in _BASE_FIELDS
    ) and all(
        re.fullmatch(r"[0-9a-f]{64}", str(value.get(_DIGEST) or "")) is not None
        for value in (held, current)
    )


def _require_available(config, route: str) -> None:
    routed = ProviderRegistry(config).provider_config(route) if config.get("providers") else config
    provider = create_provider(routed)
    command = provider._effective_dispatch_command()
    if not command or not (shutil.which(command[0]) or Path(command[0]).is_file()):
        raise HarnessError("Finish installing and signing in to the provider, then review reconnection again.")


def goal_routes(store, document: dict) -> list[dict]:
    if document.get("status") not in {"paused", "failed"} or store._scheduler_live(document) \
            or any(one.get("state") == "running" for one in document.get("tasks", [])):
        raise HarnessError("Pause the team and wait for its agent workers to stop before reconnecting.")
    if store.collaboration_setup_status(document).get("changed"):
        raise HarnessError("The saved collaboration contract changed; this chat cannot be reconnected.")
    result = []
    for agent in document.get("agents", []):
        held = agent.get("route_binding") or {}
        route = str(agent.get("who") or "")
        _kind, context = chat._route_failure_context(store.config, route)
        current = {"route": route, **context}
        # CLI principal identity includes the executable digest. Prove that
        # current account-slot/config material under the SAVED dispatch digest
        # reconstructs the old principal before accepting its replacement.
        _kind, former = chat._route_failure_context(
            store.config, route, principal_dispatch_fingerprint_override=str(held.get(_DIGEST) or ""))
        if held.get("binding_schema_version") != 3 or not compatible(held, current) or any(
            held.get(key) != former.get(key) for key in (
                "provider_principal_version", "provider_principal_fingerprint_sha256",
                "provider_principal_contract",
            )
        ):
            raise HarnessError("The saved provider route, settings, or contract changed; restore that setup first.")
        if held[_DIGEST] != current[_DIGEST]:
            _require_available(store.config, route)
        result.append({**held, _DIGEST: current[_DIGEST],
                       "provider_principal_fingerprint_sha256": current["provider_principal_fingerprint_sha256"]})
    return result


def _fingerprint(material: dict) -> str:
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _plan(config, board: dict, agent_id: str, chat_id: str, registry: dict, runtime) -> dict:
    raw, agents = swarm_chats._validated_conversation(
        config, registry, board, agent_id, chat_id, require_current_binding=False,
    )
    held = raw.get("binding") or {}
    if held.get("binding_schema_version") != swarm_chats.CHAT_BINDING_SCHEMA_VERSION:
        raise HarnessError("This chat has no verified provider binding to reconnect.")
    candidate = copy.deepcopy(raw)
    changed = []
    for member_id in raw["pair"]:
        old = held["agent_routes"].get(member_id) or {}
        current = swarm_chats._verified_chat_route_projection(
            swarm_chats._route_binding(config, agents[member_id]))
        if old.get("effective_dispatch_strength") != "verified" or not compatible(old, current):
            raise HarnessError("The saved provider route, settings, or contract changed; restore that setup first.")
        if old[_DIGEST] != current[_DIGEST]:
            _require_available(config, current["route"])
            changed.append(str(agents[member_id].get("name") or member_id))
        candidate["binding"]["agent_routes"][member_id] = current
    problem = swarm_chats._binding_problem(config, board, candidate, agents)
    if problem:
        raise HarnessError(problem["message"])

    goals = []
    for listed in runtime.store.active_authority_goals():
        if listed.get("conversation_id") != chat_id:
            continue
        document = runtime.store.get(listed["goal_id"])
        if document.get("project", {}).get("id") != raw.get("project") or sorted(
            document.get("requested_agent_ids") or [one["id"] for one in document["agents"]]
        ) != sorted(raw["pair"]):
            raise HarnessError("The saved goal belongs to a different project or team.")
        runtime._require_goal_authority(document)
        replacements = goal_routes(runtime.store, document)
        goals.append({"goal_id": document["goal_id"], "revision": document["revision"],
                      "before": [one["route_binding"] for one in document["agents"]],
                      "after": replacements})
    if not changed and not any(one["before"] != one["after"] for one in goals):
        raise HarnessError("This saved chat already matches the current provider setup. Use Resume team.")
    material = {"contract": CONTRACT, "workspace_id": raw["workspace_id"],
                "chat_id": chat_id, "pair": raw["pair"], "before": held,
                "after": candidate["binding"], "goals": goals}
    return {"fingerprint": _fingerprint(material), "candidate": candidate, "goals": goals,
            "agents": changed, "contract": CONTRACT, "schema_version": 1}


def reconnect(config, board: dict, agent_id: str, chat_id: str, runtime,
              *, confirmation: object = None) -> dict[str, Any]:
    """Preview, then apply an exact reviewed binding while keeping work paused.

    The caller also holds the conversation-turn lock, blocking ordinary sends.
    Goals commit before chat metadata: interruption can only leave a protected
    chat with paused goals, and a fresh review completes that partial write.
    """
    with runtime.lock, swarm_chats._registry_transaction(config):
        registry = swarm_chats._read(config)
        plan = _plan(config, board, agent_id, chat_id, registry, runtime)
        if confirmation is None:
            names = ", ".join(plan["agents"]) or "the saved goal's providers"
            return {key: plan[key] for key in ("schema_version", "contract", "fingerprint", "agents")} | {
                "message": (
                    f"Reconnect this saved chat to the current installation for {names}? "
                    "Confirm that you signed in to the intended account. The existing transcript "
                    "will be available to that provider when you resume. Saved work, budgets, and "
                    "permissions are kept; interrupted calls still require their recovery checks."
                ),
            }
        if not isinstance(confirmation, dict) or confirmation.get("schema_version") != 1 \
                or confirmation.get("contract") != CONTRACT or confirmation.get("decision") != "reconnect" \
                or not hmac.compare_digest(str(confirmation.get("fingerprint") or ""), plan["fingerprint"]):
            raise HarnessError("This reconnect review changed. Review the current setup again.")
        # External CLI updates are outside our locks. Reobserve the full plan
        # immediately before changing any saved authority.
        checked = _plan(config, board, agent_id, chat_id, registry, runtime)
        if checked["fingerprint"] != plan["fingerprint"]:
            raise HarnessError("The provider changed during reconnection. Review it again.")
        for goal in plan["goals"]:
            runtime.store.reconnect_provider_setup(goal, plan["fingerprint"])
        raw = next(one for one in registry["chats"] if one["id"] == chat_id)
        raw["binding"] = plan["candidate"]["binding"]
        swarm_chats._write(config, registry)
        return {"reconnected": True, "chat_id": chat_id,
                "message": "Saved chat reconnected. Use Resume team to continue; review any interrupted-call recovery shown below."}
