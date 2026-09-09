"""Pair-scoped display preferences and explicit local conversation purging."""
from __future__ import annotations

import json
import shutil

from . import chat, swarm_chats
from .models import HarnessError


def update(config, board, agent_id, chat_id, *, action, name=None, pinned=None, runtime=None, communication_runs=None):
    with swarm_chats._registry_transaction(config):
        registry = swarm_chats._read(config)
        raw = next((item for item in registry["chats"] if item["id"] == chat_id), None)
        if raw is None or agent_id not in raw["pair"] or raw.get("workspace_id") != swarm_chats._board_workspace_id(board):
            raise HarnessError("That chat does not belong to this agent pair.")
        if action == "rename":
            if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80 or any(ord(c) < 32 for c in name):
                raise HarnessError("Use a chat name of 1 to 80 characters on one line.")
            raw["name"] = name.strip()
        elif action == "pin":
            if not isinstance(pinned, bool):
                raise HarnessError("Choose whether to pin this chat.")
            raw["pinned"] = pinned
        elif action == "purge":
            if runtime is not None and not _purge_goals(runtime, chat_id):
                return {"purge_pending": True, "message": "Stopping this chat's work before deletion…"}
            if communication_runs is not None:
                communication_runs.purge_conversation(chat_id)
            route = str(swarm_chats._agents(board).get(raw["pair"][0], {}).get("who") or "")
            chat.remove_conversation(config, route, raw["filed_as"])
            registry["chats"] = [item for item in registry["chats"] if item["id"] != chat_id]
            registry.setdefault("purged_chats", []).append(chat_id)
            for key in ("active", "chosen_active"):
                registry[key] = {k: v for k, v in registry[key].items() if v != chat_id}
        else:
            raise HarnessError("Unknown chat action.")
        raw["updated_at"] = swarm_chats._now()
        swarm_chats._write(config, registry)
        if action == "purge":
            # Registry backups contain names and bindings, not message bodies.
            # Scrub this identity there too, retaining every sibling entry.
            history = swarm_chats._history_where(config)
            for path in history.glob("_board-conversations-*.json") if history.is_dir() else []:
                path = swarm_chats._history_snapshot_where(config, path.name)
                candidate = swarm_chats._registry_candidate(path)
                if candidate is None:
                    continue
                value = candidate["value"]
                value["chats"] = [item for item in value.get("chats", []) if item.get("id") != chat_id]
                value["purged_chats"] = list(set(value.get("purged_chats", [])) | {chat_id})
                for key in ("active", "chosen_active"):
                    value[key] = {k: v for k, v in value.get(key, {}).items() if v != chat_id}
                value["integrity_sha256"] = swarm_chats._registry_integrity(value)
                swarm_chats._atomic_text(config, path, json.dumps(value, indent=2) + "\n")
    return swarm_chats.list_for_agent(config, board, agent_id)


def _purge_goals(runtime, chat_id):
    with runtime.lock:
        return _purge_goals_locked(runtime, chat_id)


def _purge_goals_locked(runtime, chat_id):
    from langgraph.checkpoint.sqlite import SqliteSaver
    from . import long_horizon, goal_workspaces
    store = runtime.store
    with store.lock, store._connect() as db:
        goals = [store._decode(row) for row in db.execute(
            "SELECT * FROM long_goals WHERE request_id LIKE ?", (store.authority_key + ":%",))]
    goals = [goal for goal in goals if goal and goal.get("conversation_id") == chat_id]
    pending = False
    for goal in goals:
        if not store._is_released_terminal(goal):
            runtime.control(goal["goal_id"], "cancel")
            pending = pending or not store._is_released_terminal(store.get(goal["goal_id"]))
    if pending:
        return False
    for original in goals:
        goal = store.get(original["goal_id"])
        if not store._is_released_terminal(goal):
            return False
        # Only the runtime-owned copy is removed. Published project files are
        # outside this root and are never deletion candidates.
        parent = goal_workspaces._direct(store.root / "goal-workspaces")
        folder = goal_workspaces._direct(parent / goal["goal_id"])
        if folder.parent != parent:
            raise HarnessError("The private goal folder changed ownership.")
        if folder.is_dir():
            shutil.rmtree(folder)
        for task in goal.get("tasks", []):
            sessions = {long_horizon._stable_id("lh-tools", goal["goal_id"], task["id"], attempt)
                        for attempt in range(int(task.get("attempts") or 0) + 1)}
            for step in task.get("context_steps", []):
                identity = long_horizon._context_tool_execution(step).get("session_id")
                if identity:
                    sessions.add(identity)
            for identity in sessions:
                chat.remove_conversation(store.config, "", long_horizon._stable_id("lh-context", identity))
        if store.checkpoints.is_file():
            with SqliteSaver.from_conn_string(str(store.checkpoints)) as saver:
                saver.delete_thread(goal["goal_id"])
        with store.lock, store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = store._decode(db.execute("SELECT * FROM long_goals WHERE goal_id=?", (goal["goal_id"],)).fetchone())
            if not current or not store._is_released_terminal(current):
                raise HarnessError("The goal changed while deleting its chat.")
            store._remember_request_tombstone(db, current)
            db.execute("DELETE FROM long_goals WHERE goal_id=?", (goal["goal_id"],))
            db.commit()
    return True
