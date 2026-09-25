"""Keeps a live team's browser checks off the user's screen when they chose "hidden".

Agents used to check their pages with ``Start-Process index.html``: every check
opened a tab in the user's own browser (and ``Start-Process python ...`` a
console window), interrupting whatever the user was doing. With many teams
running that is unusable. When the user sets a team's browser checks to
hidden, this runs as a Claude Code PreToolUse hook and turns such commands
away with the alternative: the ``check_page`` tool (a hidden browser that
returns the screenshot and the errors) and hidden background processes.

The choice lives in the team's ``settings.json`` and is read on every call,
so switching it in the app takes effect at once. Visible mode allows all.
Other CLIs get the same rule in their instructions.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

BROWSER_MODES = ("hidden", "visible")
DEFAULT_BROWSER = "hidden"

# Something a browser would open: a web address, a local page, a browser.
_PAGE = r"(?:https?://|file:|localhost|127\.0\.0\.1|\.html?\b|\b(?:chrome|msedge|brave|firefox|opera|iexplore)(?:\.exe)?\b)"
_OPENERS = [
    # PowerShell and cmd: Start-Process / start / saps / Invoke-Item / ii / explorer.
    re.compile(r"(?:^|[\s;|&(])(?:start-process|saps|start|invoke-item|ii|explorer(?:\.exe)?)\s+[^;|&\n]*" + _PAGE, re.I),
    # macOS / Linux openers, the .NET call and Python's webbrowser module.
    re.compile(r"(?:^|[\s;|&(])(?:open|xdg-open|gio\s+open|wslview)\s+[^;|&\n]*" + _PAGE, re.I),
    re.compile(r"(?:process\]::start|diagnostics\.process\]::start)\s*\([^)]*" + _PAGE, re.I),
    re.compile(r"\bwebbrowser\.open", re.I),
    re.compile(r"rundll32[^;|&\n]*url\.dll", re.I),
    # A browser automation run asked to show its window.
    re.compile(r"--headed\b|headless\s*[:=]\s*false", re.I),
]
# Start-Process opens a new console window unless told not to.
_START_PROCESS = re.compile(r"(?:^|[\s;|&(])(?:start-process|saps)\b([^;|\n]*)", re.I)
_QUIET = re.compile(r"-windowstyle\s+hidden|-nonewwindow|-wi\w*\s+hidden|-nonew\w*", re.I)
_LOUD = re.compile(r"-windowstyle\s+(?:normal|maximized|minimized)", re.I)

REASON = (
    "The user set this team's browser checks to hidden, so nothing may open windows or browser tabs on "
    "their screen. To see a page, call the nexus check_page tool: it opens the page in a hidden browser "
    "and returns the screenshot, script errors and failed files. To run a server or program in the "
    "background, start it hidden (PowerShell: Start-Process ... -WindowStyle Hidden, or run it as a "
    "background job). The user can switch browser checks to visible in the Live team view."
)


def opens_window(command: str) -> bool:
    """True when a shell command would open a browser tab or a visible window."""
    text = str(command or "")
    if any(pattern.search(text) for pattern in _OPENERS):
        return True
    for match in _START_PROCESS.finditer(text):
        rest = match.group(1)
        if _LOUD.search(rest) or not _QUIET.search(rest):
            return True
    return False


def read_mode(settings: Path | str | None) -> str:
    try:
        value = json.loads(Path(settings).read_text(encoding="utf-8")) if settings else {}
    except (OSError, ValueError, TypeError):
        value = {}
    mode = value.get("browser") if isinstance(value, dict) else None
    return mode if mode in BROWSER_MODES else DEFAULT_BROWSER


def decide(hook_input: dict[str, Any], mode: str) -> dict[str, Any] | None:
    """The hook's answer, or None to let the tool run as usual."""
    if mode != "hidden":
        return None
    tool_input = hook_input.get("tool_input") if isinstance(hook_input.get("tool_input"), dict) else {}
    command = tool_input.get("command")
    if not isinstance(command, str) or not opens_window(command):
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": REASON}}


def hook_settings(settings_path: Path | str, python: str) -> dict[str, Any]:
    """Claude Code ``--settings`` that install this guard for shell tools."""
    exe = str(python).replace("\\", "/")
    target = str(settings_path).replace("\\", "/")
    # This module by its own name; the app's start line is starting.py's to write.
    module = __name__ if __name__ != "__main__" else f"{__package__}.browser_guard"
    command = f'"{exe}" -m {module} --settings "{target}"'
    return {"hooks": {"PreToolUse": [{"matcher": "Bash|PowerShell",
                                      "hooks": [{"type": "command", "command": command, "timeout": 20}]}]}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings", default="")
    options = parser.parse_args(argv)
    try:
        hook_input = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
    except ValueError:
        return 0  # Never break the agent's tool over a malformed hook call.
    if not isinstance(hook_input, dict):
        return 0
    answer = decide(hook_input, read_mode(options.settings))
    if answer is not None:
        sys.stdout.write(json.dumps(answer))
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
