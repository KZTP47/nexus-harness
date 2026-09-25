"""One event vocabulary for every agent session, shaped like ACP ``session/update``.

Every adapter turns its CLI's native stream into these plain dicts:

``session``   state changes: starting, ready, resumed, fresh, closed, error
``turn``      status: started | completed | failed | interrupted
``message``   the agent's visible text; ``delta`` True for a streamed chunk
``thought``   public/summarized reasoning; ``delta`` as above
``tool``      status: requested | running | finished | failed, with a title
``plan``      the agent's current plan/todo list
``usage``     token usage when the CLI reports it
``permission`` a question for the user (approve a command/edit)
``notice``    Nexus-side information (delivery, lease, retry)

Every event carries ``agent`` (seat id), ``session`` (occupying session id when
known), ``turn`` (turn id when known), ``id`` (item id for updates) and ``at``.
"""

from __future__ import annotations

import re
import time
from typing import Any

KINDS = frozenset({"session", "turn", "message", "thought", "tool", "plan", "usage", "permission", "notice"})
TEXT_LIMIT = 20_000


def event(kind: str, **fields: Any) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"unknown agent event kind: {kind}")
    value: dict[str, Any] = {"kind": kind, "at": time.time()}
    for key, item in fields.items():
        if item is None:
            continue
        if isinstance(item, str) and len(item) > TEXT_LIMIT:
            item = item[:TEXT_LIMIT] + "…"
        value[key] = item
    return value


_SHELL_WRAPPER = re.compile(
    r'^\s*"?[^"\s]*(?:powershell|pwsh|cmd|bash|sh)(?:\.exe)?"?\s+(?:-NoProfile\s+)?(?:-Command|-c|/c|/d /s /c)\s+',
    re.IGNORECASE)


def readable_command(command: str) -> str:
    """The command the agent meant, without the shell wrapper the CLI adds."""

    inner = _SHELL_WRAPPER.sub("", str(command or ""), count=1).strip()
    if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "'\"":
        inner = inner[1:-1]
    return inner or str(command or "")


def tool_name(name: str) -> str:
    """``mcp__nexus__send_message`` -> ``send_message``."""

    value = str(name or "tool")
    if value.startswith("mcp__"):
        value = value.split("__")[-1]
    return value


def tool_title(name: str, arguments: Any) -> str:
    """A short human title for a tool call: what it runs, edits or reads."""

    args = arguments if isinstance(arguments, dict) else {}
    name = tool_name(name)
    command = args.get("command")
    if isinstance(command, list):
        command = " ".join(str(one) for one in command)
    if command:
        return readable_command(str(command))[:300]
    changes = args.get("changes")
    if isinstance(changes, list) and changes:
        names = [str((one or {}).get("path") or "").replace("\\", "/").rsplit("/", 1)[-1] for one in changes]
        return "Edit " + ", ".join(one for one in names if one)[:280]
    for key in ("file_path", "path", "pattern", "query", "url", "description"):
        if args.get(key):
            return f"{name}: {str(args[key])[:280]}"
    return str(name or "tool")[:120]
