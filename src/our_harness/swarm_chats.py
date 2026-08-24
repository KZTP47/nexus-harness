"""Durable, pair-scoped conversations for the board of agents.

The ordinary chat transcript store deliberately knows only a provider route
and a safe file name.  This module adds the missing board-level identity: one
conversation belongs to one canonical pair of agents and selects one project
that both agents work on.  Every server action resolves that metadata again,
so a stale or hand-edited browser request cannot silently change the pair or
the folder that receives file changes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import chat as chat_lab
from . import swarm as swarm_lab
from .config import LoadedConfig


WHERE_THEY_LIVE = ".harness/chats/_board-conversations.json"
MOST_CHATS = 240
MOST_PER_PAIR = 40
_CHAT_ID = re.compile(r"^chat-[0-9a-f]{16}$")
_lock = threading.Lock()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _where(config: LoadedConfig) -> Path:
    from .safety import confined_path

    return confined_path(
        config.project_root, WHERE_THEY_LIVE,
        allow_missing=True, allow_control=True,
    )


def _empty() -> dict[str, Any]:
    return {
        "schema_version": 5,
        "chats": [],
        "active": {},
        "chosen_active": {},
        "known_pairs": [],
    }


def _read(config: LoadedConfig) -> dict[str, Any]:
    where = _where(config)
    try:
        value = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty()
    if not isinstance(value, dict):
        return _empty()
    chats = []
    seen_ids: set[str] = set()
    needs_rewrite = value.get("schema_version") != 5
    for raw in value.get("chats", []) if isinstance(value.get("chats"), list) else []:
        if not isinstance(raw, dict) or not _CHAT_ID.fullmatch(str(raw.get("id") or "")):
            continue
        pair = raw.get("pair")
        if (
            not isinstance(pair, list) or len(pair) not in (1, 2)
            or any(not str(one or "").strip() for one in pair)
        ):
            continue
        canonical = sorted(dict.fromkeys(str(one)[:120] for one in pair))
        if len(canonical) != len(pair):
            continue
        chat_id = str(raw["id"])
        if chat_id in seen_ids:
            needs_rewrite = True
            continue
        seen_ids.add(chat_id)
        canonical_filed_as = _filed_as(canonical, chat_id)
        if str(raw.get("filed_as") or "") != canonical_filed_as:
            needs_rewrite = True
        chats.append({
            "id": chat_id,
            "pair": canonical,
            "name": str(raw.get("name") or "Chat")[:80],
            "project": str(raw.get("project") or "")[:120],
            # The transcript key is an ownership capability, not mutable
            # registry data. Older registries could point a new pair chat at
            # an agent-name transcript. That transcript had no pair identity
            # and could contain work from a completely different pair. Always
            # derive the key from the canonical pair and chat id so malformed,
            # stale, and schema-v2 registries repair themselves on read.
            "filed_as": canonical_filed_as,
            "created_at": str(raw.get("created_at") or ""),
            "updated_at": str(raw.get("updated_at") or ""),
            "legacy_recovered": bool(raw.get("legacy_recovered")),
            "web_legacy_candidate": (
                raw.get("web_legacy_candidate")
                if isinstance(raw.get("web_legacy_candidate"), bool) else None
            ),
        })
    active = value.get("active") if isinstance(value.get("active"), dict) else {}
    chosen_active = (
        value.get("chosen_active")
        if isinstance(value.get("chosen_active"), dict) else {}
    )
    known = value.get("known_pairs") if isinstance(value.get("known_pairs"), list) else []
    first_by_pair: set[str] = set()
    for one in chats:
        key = _pair_key(one["pair"])
        inferred = key not in first_by_pair
        first_by_pair.add(key)
        if one["web_legacy_candidate"] is None:
            one["web_legacy_candidate"] = inferred
            needs_rewrite = True
    return {
        "schema_version": 5,
        "chats": chats[-MOST_CHATS:],
        "active": {str(key)[:120]: str(item) for key, item in active.items()},
        "chosen_active": {
            str(key)[:120]: str(item) for key, item in chosen_active.items()
        },
        "known_pairs": [str(one)[:260] for one in known if isinstance(one, str)],
        "_needs_rewrite": needs_rewrite,
    }


def _write(config: LoadedConfig, value: dict[str, Any]) -> None:
    where = _where(config)
    where.parent.mkdir(parents=True, exist_ok=True)
    beside = where.with_name(
        f"{where.name}.{os.getpid()}-{threading.get_ident()}.part"
    )
    persisted = {
        key: held for key, held in value.items() if not str(key).startswith("_")
    }
    beside.write_text(json.dumps(persisted, indent=2) + "\n", encoding="utf-8")
    os.replace(beside, where)


def _agents(board: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(one.get("id")): one for one in board.get("agents", [])
        if isinstance(one, dict) and one.get("id")
    }


def _pair(one: str, other: str = "") -> list[str]:
    return sorted([one] if not other or other == one else [one, other])


def _pair_key(pair: list[str]) -> str:
    return "|".join(pair)


def _connected_pairs(board: dict[str, Any], agent_id: str) -> list[list[str]]:
    agents = _agents(board)
    if agent_id not in agents:
        raise swarm_lab.SwarmError("That agent is not on the board any more. Refresh the board.")
    pairs = []
    for other in agents:
        if other != agent_id and swarm_lab.may_they_talk(board, agent_id, other):
            pairs.append(_pair(agent_id, other))
    # A single-agent chat remains usable on a board with no green line. As soon
    # as a peer is connected, the real pair workspaces replace this fallback.
    return pairs or [[agent_id]]


def _shared_projects(board: dict[str, Any], pair: list[str]) -> list[dict[str, Any]]:
    assigned = {
        agent: {
            str(line.get("project")) for line in board.get("works_on", [])
            if isinstance(line, dict) and str(line.get("agent")) == agent
        }
        for agent in pair
    }
    shared = set.intersection(*(assigned[agent] for agent in pair)) if pair else set()
    return [
        one for one in board.get("projects", [])
        if isinstance(one, dict) and str(one.get("id")) in shared
    ]


def _filed_as(pair: list[str], chat_id: str) -> str:
    marked = hashlib.sha256(("|".join(pair) + "|" + chat_id).encode("utf-8")).hexdigest()[:20]
    return f"pair-chat-{marked}"


def _turn_ids(value: str) -> set[str]:
    return {
        one.strip() for one in str(value or "").split(",") if one.strip()
    }


def _owned_blocks(turns: list[chat_lab.Said], pair: list[str]) -> list[chat_lab.Said]:
    """Return only complete legacy blocks that prove this exact pair owns them."""

    target = set(pair)
    recovered: list[chat_lab.Said] = []
    block: list[chat_lab.Said] = []

    def keep_if_owned() -> None:
        if not block or block[0].who != "you":
            return
        # The user turn written by pair-aware builds names every intended
        # recipient. This is the ownership boundary. Speaker/recipient IDs on
        # all remaining turns must stay inside it as a second fail-closed check.
        prompt_recipients = _turn_ids(block[0].recipient_id)
        mentioned: set[str] = set()
        for turn in block:
            mentioned.update(_turn_ids(turn.speaker_id))
            mentioned.update(_turn_ids(turn.recipient_id))
        if prompt_recipients == target and mentioned and mentioned <= target:
            recovered.extend(block)

    for turn in turns:
        if turn.who == "you":
            keep_if_owned()
            block = [turn]
        elif block:
            block.append(turn)
    keep_if_owned()
    return recovered


def _recover_legacy_history(
    config: LoadedConfig, raw: dict[str, Any], agents: dict[str, dict[str, Any]]
) -> int:
    recovered: list[chat_lab.Said] = []
    for member_id in raw["pair"]:
        member = agents.get(member_id) or {}
        legacy = swarm_lab.filed_as(str(member.get("name") or ""))
        if not legacy:
            continue
        turns = chat_lab.read_it(
            config, str(member.get("who") or ""), legacy
        )
        recovered.extend(_owned_blocks(turns, raw["pair"]))
    lead = agents.get(raw["pair"][0]) or {}
    return chat_lab.merge_transcript(
        config, str(lead.get("who") or ""), raw["filed_as"], recovered
    )


def _new_chat(
    registry: dict[str, Any], board: dict[str, Any], pair: list[str],
) -> dict[str, Any]:
    same = [one for one in registry["chats"] if one["pair"] == pair]
    if len(same) >= MOST_PER_PAIR or len(registry["chats"]) >= MOST_CHATS:
        raise swarm_lab.SwarmError("This pair already has the maximum number of saved chats.")
    chat_id = f"chat-{uuid.uuid4().hex[:16]}"
    projects = _shared_projects(board, pair)
    now = _now()
    made = {
        "id": chat_id,
        "pair": pair,
        "name": f"Chat {len(same) + 1}",
        "project": str(projects[0].get("id")) if projects else "",
        "filed_as": _filed_as(pair, chat_id),
        "created_at": now,
        "updated_at": now,
        # Legacy history belongs to Chat 1 only. A second chat is a deliberate
        # fresh workspace and must not repeat recovered history.
        "legacy_recovered": bool(same),
        # One pre-isolation provider URL can be adopted by the first Nexus chat
        # for this pair. Every later chat must open a new remote thread.
        "web_legacy_candidate": not bool(same),
    }
    registry["chats"].append(made)
    return made


def _present(
    config: LoadedConfig, board: dict[str, Any], agent_id: str,
    raw: dict[str, Any], agents: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    pair_agents = [agents[one] for one in raw["pair"] if one in agents]
    projects = _shared_projects(board, raw["pair"])
    project_ids = {str(one.get("id")) for one in projects}
    project_id = raw["project"] if raw["project"] in project_ids else ""
    current = agents[agent_id]
    destination = chat_lab.chat_destination(
        config, str(current.get("who") or ""), raw["filed_as"],
        conversation_key=raw["filed_as"],
        prefer_existing_conversation=bool(raw.get("web_legacy_candidate")),
    )
    return {
        **raw,
        "project": project_id,
        "pair_agents": [{
            "id": str(one.get("id") or ""),
            "name": str(one.get("name") or "An agent"),
            "who": str(one.get("who") or ""),
            "ready": bool(one.get("ready")),
        } for one in pair_agents],
        "projects": [{
            "id": str(one.get("id") or ""),
            "name": str(one.get("name") or Path(str(one.get("path") or "")).name),
            "path": str(one.get("path") or ""),
            "is_there": bool(one.get("is_there", Path(str(one.get("path") or "")).is_dir())),
        } for one in projects],
        "connected": len(raw["pair"]) == 1 or (
            len(raw["pair"]) == 2
            and swarm_lab.may_they_talk(board, raw["pair"][0], raw["pair"][1])
        ),
        "destination": destination,
    }


def list_for_agent(
    config: LoadedConfig, board: dict[str, Any], agent_id: str
) -> dict[str, Any]:
    """List chats containing this agent, creating one initial chat per new pair."""

    with _lock:
        registry = _read(config)
        changed = bool(registry.pop("_needs_rewrite", False))
        agents = _agents(board)
        pairs = _connected_pairs(board, agent_id)
        for pair in pairs:
            key = _pair_key(pair)
            if key in registry["known_pairs"]:
                continue
            # Pair chats start in pair-owned storage. A pre-multi-chat file is
            # intentionally left intact as legacy history: it has no reliable
            # pair identity, so adopting it would relabel unknown speakers as
            # members of whichever pair happened to be listed first.
            made = _new_chat(registry, board, pair)
            registry["known_pairs"].append(key)
            changed = True

        visible = [
            one for one in registry["chats"]
            if agent_id in one["pair"] and all(member in agents for member in one["pair"])
        ]
        first_for_pair = {}
        for one in registry["chats"]:
            first_for_pair.setdefault(_pair_key(one["pair"]), one["id"])
        for one in visible:
            if one.get("legacy_recovered"):
                continue
            if first_for_pair.get(_pair_key(one["pair"])) == one["id"]:
                _recover_legacy_history(config, one, agents)
            one["legacy_recovered"] = True
            one["updated_at"] = _now()
            changed = True
        visible_ids = {one["id"] for one in visible}
        chosen = str(registry["chosen_active"].get(agent_id) or "")
        if chosen not in visible_ids:
            if chosen:
                registry["chosen_active"].pop(agent_id, None)
                changed = True
            chosen = ""

        active = str(registry["active"].get(agent_id) or "")
        if chosen:
            preferred = chosen
        else:
            # A board can connect one agent to several peers. Board order is
            # not a useful default: the first pair may have no folder in
            # common while a later pair is ready for project work. Until the
            # user explicitly picks a pair, prefer a conversation that has a
            # real shared project and therefore can do the work advertised by
            # the chat controls.
            preferred_chat = next((
                one for one in visible
                if _shared_projects(board, one["pair"])
            ), visible[0] if visible else None)
            preferred = preferred_chat["id"] if preferred_chat else ""
        if active != preferred:
            active = preferred
            registry["active"][agent_id] = active
            changed = True

        # If a selected project stopped being shared, clear it now. This makes
        # the UI honest and makes file work fail closed until another is picked.
        for one in visible:
            valid = {str(project.get("id")) for project in _shared_projects(board, one["pair"])}
            if one["project"] and one["project"] not in valid:
                one["project"] = ""
                one["updated_at"] = _now()
                changed = True
        if changed:
            _write(config, registry)
        return {
            "agent": agent_id,
            "active": active,
            "chats": [_present(config, board, agent_id, one, agents) for one in visible],
        }


def resolve(
    config: LoadedConfig, board: dict[str, Any], agent_id: str, chat_id: str
) -> dict[str, Any]:
    """Resolve and validate one conversation for one of its pair members."""

    if not _CHAT_ID.fullmatch(str(chat_id or "")):
        raise swarm_lab.SwarmError("Choose a saved chat first.")
    with _lock:
        registry = _read(config)
        raw = next((one for one in registry["chats"] if one["id"] == chat_id), None)
        agents = _agents(board)
        if raw is None or agent_id not in raw["pair"]:
            raise swarm_lab.SwarmError("That chat does not belong to this agent pair.")
        if any(one not in agents for one in raw["pair"]):
            raise swarm_lab.SwarmError("One of this chat's agents is no longer on the board.")
        if len(raw["pair"]) == 2 and not swarm_lab.may_they_talk(
            board, raw["pair"][0], raw["pair"][1]
        ):
            raise swarm_lab.SwarmError("Reconnect these two agents before using this chat.")
        presented = _present(config, board, agent_id, raw, agents)
        presented["peer"] = next((one for one in raw["pair"] if one != agent_id), "")
        return presented


def create(
    config: LoadedConfig, board: dict[str, Any], agent_id: str, peer_id: str
) -> dict[str, Any]:
    pair = _pair(agent_id, peer_id)
    allowed = _connected_pairs(board, agent_id)
    if pair not in allowed:
        raise swarm_lab.SwarmError("These two agents need a green communication line first.")
    with _lock:
        registry = _read(config)
        made = _new_chat(registry, board, pair)
        key = _pair_key(pair)
        if key not in registry["known_pairs"]:
            registry["known_pairs"].append(key)
        registry["active"][agent_id] = made["id"]
        registry["chosen_active"][agent_id] = made["id"]
        _write(config, registry)
    return list_for_agent(config, board, agent_id)


def activate(
    config: LoadedConfig, board: dict[str, Any], agent_id: str, chat_id: str
) -> dict[str, Any]:
    resolve(config, board, agent_id, chat_id)
    with _lock:
        registry = _read(config)
        registry["active"][agent_id] = chat_id
        registry["chosen_active"][agent_id] = chat_id
        _write(config, registry)
    return list_for_agent(config, board, agent_id)


def select_project(
    config: LoadedConfig, board: dict[str, Any], agent_id: str,
    chat_id: str, project_id: str,
) -> dict[str, Any]:
    resolved = resolve(config, board, agent_id, chat_id)
    valid = {str(one.get("id")) for one in resolved["projects"]}
    if project_id and project_id not in valid:
        raise swarm_lab.SwarmError(
            "Both agents must work on the selected project before this chat can write to it."
        )
    with _lock:
        registry = _read(config)
        raw = next(one for one in registry["chats"] if one["id"] == chat_id)
        raw["project"] = project_id
        raw["updated_at"] = _now()
        registry["active"][agent_id] = chat_id
        registry["chosen_active"][agent_id] = chat_id
        _write(config, registry)
    return list_for_agent(config, board, agent_id)


def delete(
    config: LoadedConfig, board: dict[str, Any], agent_id: str, chat_id: str
) -> dict[str, Any]:
    resolved = resolve(config, board, agent_id, chat_id)
    current = _agents(board)[agent_id]
    with _lock:
        registry = _read(config)
        registry["chats"] = [one for one in registry["chats"] if one["id"] != chat_id]
        for key, active in list(registry["active"].items()):
            if active == chat_id:
                registry["active"].pop(key, None)
        for key, chosen in list(registry["chosen_active"].items()):
            if chosen == chat_id:
                registry["chosen_active"].pop(key, None)
        _write(config, registry)
    chat_lab.remove_conversation(
        config, str(current.get("who") or ""), str(resolved["filed_as"])
    )
    return list_for_agent(config, board, agent_id)
