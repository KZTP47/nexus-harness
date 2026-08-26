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
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import chat as chat_lab
from . import swarm as swarm_lab
from .config import LoadedConfig


WHERE_THEY_LIVE = ".harness/chats/_board-conversations.json"
BACKUP_NAME = "_board-conversations.last-good.json"
HISTORY_FOLDER = "_board-conversation-history"
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


def _read_object(where: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _read(config: LoadedConfig) -> dict[str, Any]:
    where = _where(config)
    value = _read_object(where)
    recovered_from = ""
    if value is None:
        backup = where.with_name(BACKUP_NAME)
        value = _read_object(backup)
        if value is not None:
            recovered_from = backup.name
        else:
            history = where.with_name(HISTORY_FOLDER)
            for candidate in sorted(
                history.glob("_board-conversations-*.json"), reverse=True
            ) if history.is_dir() else []:
                value = _read_object(candidate)
                if value is not None:
                    recovered_from = candidate.relative_to(where.parent).as_posix()
                    break
    if value is None:
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
            "legacy_source": str(raw.get("legacy_source") or "")[:160],
            "archived_at": str(raw.get("archived_at") or "")[:40],
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
        "_needs_rewrite": needs_rewrite or bool(recovered_from),
        "_recovered_from": recovered_from,
    }


def _atomic_text(where: Path, text: str) -> None:
    where.parent.mkdir(parents=True, exist_ok=True)
    beside = where.with_name(
        f"{where.name}.{os.getpid()}-{threading.get_ident()}.part"
    )
    beside.write_text(text, encoding="utf-8")
    os.replace(beside, where)


def _write(config: LoadedConfig, value: dict[str, Any]) -> None:
    where = _where(config)
    where.parent.mkdir(parents=True, exist_ok=True)
    persisted = {
        key: held for key, held in value.items() if not str(key).startswith("_")
    }
    written = json.dumps(persisted, indent=2) + "\n"

    # The registry is the only index that gives opaque transcript files their
    # names and owners. Keep the last known-good complete copy, and keep an
    # append-only snapshot before every structural chat change. A corrupt or
    # accidentally shortened registry must never turn existing transcripts
    # into invisible files on the next write.
    current = _read_object(where)
    if current is not None:
        old_chats = json.dumps(current.get("chats", []), sort_keys=True)
        new_chats = json.dumps(persisted.get("chats", []), sort_keys=True)
        if old_chats != new_chats:
            history = where.with_name(HISTORY_FOLDER)
            history.mkdir(parents=True, exist_ok=True)
            snapshot = history / f"_board-conversations-{time.time_ns()}.json"
            _atomic_text(snapshot, json.dumps(current, indent=2) + "\n")

    _atomic_text(where, written)
    _atomic_text(where.with_name(BACKUP_NAME), written)


def _agents(board: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(one.get("id")): one for one in board.get("agents", [])
        if isinstance(one, dict) and one.get("id")
    }


def _pair(one: str, other: str = "") -> list[str]:
    return sorted([one] if not other or other == one else [one, other])


def _pair_key(pair: list[str]) -> str:
    return "|".join(pair)


def fence_for_board_change(
    config: LoadedConfig, before: dict[str, Any], after: dict[str, Any]
) -> int:
    """Fence saved chats when any authority-bearing board binding changes."""

    def authority(board: dict[str, Any]) -> str:
        agents = sorted(
            (
                str(one.get("id") or ""), str(one.get("name") or ""),
                str(one.get("who") or ""), str(one.get("filed_as") or ""),
            )
            for one in board.get("agents", []) if isinstance(one, dict)
        )
        projects = sorted(
            (str(one.get("id") or ""), str(one.get("path") or ""))
            for one in board.get("projects", []) if isinstance(one, dict)
        )
        payload = {
            "agents": agents,
            "projects": projects,
            "works_on": sorted(
                (str(one.get("agent") or ""), str(one.get("project") or ""))
                for one in board.get("works_on", []) if isinstance(one, dict)
            ),
            "talks_to": sorted(
                tuple(sorted((str(one.get("one") or ""), str(one.get("other") or ""))))
                for one in board.get("talks_to", []) if isinstance(one, dict)
            ),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    if authority(before) == authority(after):
        return 0
    from .collaboration_ledger import fence_ledger

    with _lock:
        registry = _read(config)
        routes = {
            str(one.get("id") or ""): str(one.get("who") or "")
            for one in after.get("agents", []) if isinstance(one, dict)
        }
        old_routes = {
            str(one.get("id") or ""): str(one.get("who") or "")
            for one in before.get("agents", []) if isinstance(one, dict)
        }
        fenced = 0
        for conversation in registry.get("chats", []):
            if not isinstance(conversation, dict):
                continue
            pair = conversation.get("pair", [])
            first = str(pair[0]) if isinstance(pair, list) and pair else ""
            fence_ledger(
                config, routes.get(first, old_routes.get(first, "")),
                str(conversation.get("filed_as") or ""),
            )
            fenced += 1
        return fenced


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
        legacy = str(member.get("filed_as") or "") or swarm_lab.filed_as(
            str(member.get("name") or "")
        )
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
        "legacy_source": "",
        "archived_at": "",
        # One pre-isolation provider URL can be adopted by the first Nexus chat
        # for this pair. Every later chat must open a new remote thread.
        "web_legacy_candidate": not bool(same),
    }
    registry["chats"].append(made)
    return made


def _legacy_names(member: dict[str, Any]) -> list[str]:
    """Stable pre-pair transcript names that can belong to this exact agent."""

    names: list[str] = []
    for candidate in (member.get("filed_as"), member.get("name")):
        value = " ".join(str(candidate or "").split())
        if value and value not in names:
            names.append(value)
    return names


def _copy_legacy_attachments(
    config: LoadedConfig, route: str, source: str, destination: str
) -> None:
    source_folder = chat_lab._attachment_folder(config, route, source)
    if not source_folder.is_dir() or source_folder.is_symlink():
        return
    destination_folder = chat_lab._attachment_folder(config, route, destination)
    destination_folder.mkdir(parents=True, exist_ok=True)
    for item in source_folder.iterdir():
        if not item.is_file() or item.is_symlink():
            continue
        target = destination_folder / item.name
        if not target.exists():
            shutil.copy2(item, target)


def _recover_direct_legacy_chats(
    config: LoadedConfig, registry: dict[str, Any], board: dict[str, Any],
    agent_id: str, agents: dict[str, dict[str, Any]],
) -> bool:
    """Index old agent-owned chats without pretending they belonged to a pair.

    Before pair chats existed, one stable board-agent name owned one transcript.
    Those files were deliberately left untouched by the pair migration, but no
    UI indexed them afterwards. Copy each one into a canonical single-agent
    conversation and retain the source as immutable recovery evidence.
    """

    member = agents.get(agent_id) or {}
    route = str(member.get("who") or "")
    changed = False
    for legacy_name in _legacy_names(member):
        source = chat_lab.where_it_is_kept(config, route, legacy_name)
        if not source.is_file():
            continue
        source_key = source.name
        if any(
            one.get("legacy_source") == source_key and one.get("pair") == [agent_id]
            for one in registry["chats"]
        ):
            continue
        turns = chat_lab.read_it(config, route, legacy_name)
        if not turns:
            continue
        made = _new_chat(registry, board, [agent_id])
        made["name"] = "Recovered older chat"
        made["project"] = ""
        made["legacy_recovered"] = True
        made["legacy_source"] = source_key
        made["web_legacy_candidate"] = False
        made["created_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(source.stat().st_mtime)
        )
        made["updated_at"] = _now()
        chat_lab.merge_transcript(config, route, made["filed_as"], turns)
        _copy_legacy_attachments(config, route, legacy_name, made["filed_as"])
        key = _pair_key([agent_id])
        if key not in registry["known_pairs"]:
            registry["known_pairs"].append(key)
        changed = True
    return changed


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
        recovered_from = str(registry.get("_recovered_from") or "")
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

        if _recover_direct_legacy_chats(
            config, registry, board, agent_id, agents
        ):
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
        usable_ids = {one["id"] for one in visible if not one.get("archived_at")}
        chosen = str(registry["chosen_active"].get(agent_id) or "")
        if chosen not in usable_ids:
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
                if not one.get("archived_at") and not one.get("legacy_source")
                and _shared_projects(board, one["pair"])
            ), next((
                one for one in visible if not one.get("archived_at")
            ), None))
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
            "registry_recovered_from": recovered_from,
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
        if raw.get("archived_at"):
            raise swarm_lab.SwarmError("Restore this archived chat before using it.")
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
        if str(raw.get("project") or "") != project_id:
            from .collaboration_ledger import fence_ledger

            current = _agents(board).get(agent_id) or {}
            fence_ledger(
                config, str(current.get("who") or ""), str(raw.get("filed_as") or "")
            )
        raw["project"] = project_id
        raw["updated_at"] = _now()
        registry["active"][agent_id] = chat_id
        registry["chosen_active"][agent_id] = chat_id
        _write(config, registry)
    return list_for_agent(config, board, agent_id)


def delete(
    config: LoadedConfig, board: dict[str, Any], agent_id: str, chat_id: str
) -> dict[str, Any]:
    """Archive one chat without removing any transcript or attachment."""

    with _lock:
        registry = _read(config)
        agents = _agents(board)
        raw = next((one for one in registry["chats"] if one["id"] == chat_id), None)
        if raw is None or agent_id not in raw["pair"]:
            raise swarm_lab.SwarmError("That chat does not belong to this agent pair.")
        if any(one not in agents for one in raw["pair"]):
            raise swarm_lab.SwarmError("One of this chat's agents is no longer on the board.")
        raw["archived_at"] = _now()
        raw["updated_at"] = raw["archived_at"]
        # Archiving is an authority revocation. Fence the exact transcript
        # before changing discoverability so late provider replies cannot
        # append to an archived conversation.
        from .collaboration_ledger import fence_ledger

        lead = agents.get(raw["pair"][0]) or {}
        fence_ledger(
            config, str(lead.get("who") or ""), str(raw.get("filed_as") or "")
        )
        for key, active in list(registry["active"].items()):
            if active == chat_id:
                registry["active"].pop(key, None)
        for key, chosen in list(registry["chosen_active"].items()):
            if chosen == chat_id:
                registry["chosen_active"].pop(key, None)
        _write(config, registry)
    return list_for_agent(config, board, agent_id)


def restore(
    config: LoadedConfig, board: dict[str, Any], agent_id: str, chat_id: str
) -> dict[str, Any]:
    """Restore one archived chat and make it active again."""

    if not _CHAT_ID.fullmatch(str(chat_id or "")):
        raise swarm_lab.SwarmError("Choose an archived chat first.")
    with _lock:
        registry = _read(config)
        agents = _agents(board)
        raw = next((one for one in registry["chats"] if one["id"] == chat_id), None)
        if raw is None or agent_id not in raw["pair"]:
            raise swarm_lab.SwarmError("That chat does not belong to this agent pair.")
        if any(one not in agents for one in raw["pair"]):
            raise swarm_lab.SwarmError("One of this chat's agents is no longer on the board.")
        raw["archived_at"] = ""
        raw["updated_at"] = _now()
        registry["active"][agent_id] = chat_id
        registry["chosen_active"][agent_id] = chat_id
        _write(config, registry)
    return list_for_agent(config, board, agent_id)
