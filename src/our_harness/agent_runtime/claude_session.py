"""Claude Code as one persistent ``claude -p`` process with stream-json in and out.

Each user or teammate message is one JSON line on stdin. A message written
while a turn is running is read by Claude at its next step (between tool
calls), so teammate messages reach a busy Claude immediately.
"""

from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
import tempfile
import threading
from collections import deque
from pathlib import Path
from typing import Any, Callable

from . import events as ev
from .base import AgentSession, SeatSpec
from .browser_guard import hook_settings
from .jsonrpc import kill_tree

PERMISSIONS = {
    # Plan mode is Claude Code's own read-only mode.
    "read_only": ["--permission-mode", "plan", "--allowedTools", "mcp__nexus"],
    # Every tool that needs permission is asked through the Nexus MCP tool;
    # Nexus's own team tools never need asking.
    "ask": ["--permission-mode", "default", "--permission-prompt-tool", "mcp__nexus__approve_action",
            "--allowedTools", "mcp__nexus"],
    # Claude Code on Windows runs shell commands through its PowerShell tool.
    "full": ["--permission-mode", "acceptEdits", "--allowedTools", "Bash,PowerShell,WebFetch,WebSearch,mcp__nexus"],
}


class ClaudeSession(AgentSession):
    can_steer = True

    def __init__(self, spec: SeatSpec, on_event: Callable[[dict[str, Any]], None], *,
                 executable: str, env: dict[str, str] | None = None):
        super().__init__(spec, on_event)
        self.executable = executable
        self.env = env
        self.process: subprocess.Popen | None = None
        self._write = threading.Lock()
        self._turns = itertools.count(1)
        self._message_id = ""
        self._tools: dict[str, dict[str, Any]] = {}
        self.stderr_tail: deque[str] = deque(maxlen=30)
        self._started = threading.Event()
        self._files = ""

    def argv(self) -> list[str]:
        args = [self.executable, *list((self.spec.command or [])[1:]), "-p",
                "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--include-partial-messages"]
        if self.spec.model:
            args += ["--model", self.spec.model]
        args += PERMISSIONS.get(self.spec.access, PERMISSIONS["ask"])
        # Text and JSON go in files, never on the command line: an npm install
        # starts Claude through claude.cmd, and cmd.exe re-reads the arguments,
        # so a double quote in the rules silently broke every argument after it
        # (the nexus tools and the browser guard were simply missing).
        if self.spec.instructions:
            args += ["--append-system-prompt-file", self._file("rules.md", self.spec.instructions)]
        if self.spec.mcp:
            command = list(self.spec.mcp.get("command") or [])
            server = {"type": "stdio", "command": command[0], "args": command[1:],
                      **({"env": dict(self.spec.mcp["env"])} if self.spec.mcp.get("env") else {})}
            args += ["--mcp-config", self._file("mcp.json", json.dumps({"mcpServers": {"nexus": server}}))]
        for folder in self.spec.extra_dirs:
            args += ["--add-dir", folder]
        if self.spec.browser_settings:
            # Keeps pages and windows off the user's screen while browser checks are hidden.
            hooks = hook_settings(self.spec.browser_settings, sys.executable)
            args += ["--settings", self._file("claude-settings.json", json.dumps(hooks))]
        if self.spec.resume_id:
            args += ["--resume", self.spec.resume_id]
        return args

    def _file(self, name: str, text: str) -> str:
        if not self._files:
            base = Path(self.spec.state_dir) if self.spec.state_dir else Path(tempfile.mkdtemp(prefix="nexus-claude-"))
            base.mkdir(parents=True, exist_ok=True)
            self._files = str(base)
        path = Path(self._files) / name
        path.write_text(text, encoding="utf-8")
        return str(path)

    def start(self) -> None:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self.process = subprocess.Popen(self.argv(), cwd=self.spec.cwd, env=self.env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags)
        threading.Thread(target=self._read, name=f"{self.spec.name}-claude-out", daemon=True).start()
        threading.Thread(target=self._read_errors, name=f"{self.spec.name}-claude-err", daemon=True).start()
        # Claude prints its init line only when the first message arrives, so
        # the session is usable immediately; resume is confirmed on that line.
        if self.spec.resume_id:
            self.session_id = self.spec.resume_id
        self._set_state("ready")

    def close(self) -> None:
        self.state = "closed"
        if self.process is not None:
            try:
                if self.process.stdin:
                    self.process.stdin.close()
            except OSError:
                pass
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                kill_tree(self.process)
        self.emit("session", state="closed")

    def interrupt(self) -> None:
        # Claude's stream input has a control_request interrupt.
        self._line({"type": "control_request", "request_id": f"interrupt-{next(self._turns)}",
                    "request": {"subtype": "interrupt"}})

    # -- turns

    def _line(self, value: dict[str, Any]) -> None:
        data = (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write:
            if self.process is None or self.process.poll() is not None or self.process.stdin is None:
                raise RuntimeError(f"{self.spec.name}'s Claude session stopped. {self.last_error()}".strip())
            self.process.stdin.write(data)
            self.process.stdin.flush()

    def _user(self, text: str) -> None:
        self._line({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}})

    def _start_turn(self, text: str) -> None:
        self.turn_id = f"turn-{next(self._turns)}"
        self.emit("turn", status="started")
        self._user(text)

    def _steer(self, text: str) -> bool:
        try:
            self._user(text)
            return True
        except (OSError, RuntimeError):
            return False

    # -- stream mapping

    def _read_errors(self) -> None:
        assert self.process is not None
        for raw in self.process.stderr:  # type: ignore[union-attr]
            text = raw.decode("utf-8", "replace").strip()
            if text:
                self.stderr_tail.append(text[:500])

    def last_error(self) -> str:
        return " ".join(list(self.stderr_tail)[-3:])[:600]

    def _read(self) -> None:
        assert self.process is not None
        try:
            for raw in self.process.stdout:  # type: ignore[union-attr]
                line = raw.strip()
                if not line.startswith(b"{"):
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    try:
                        self._message(message)
                    except Exception:
                        pass
        finally:
            if self.state != "closed":
                self.error = self.last_error() or "Claude's session ended."
                self._set_state("error", text=self.error)
                if self.turn_active.is_set():
                    self._turn_finished("failed", text=self.error)

    def _message(self, message: dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "system" and message.get("subtype") == "init":
            session = str(message.get("session_id") or "")
            if self.spec.resume_id and not self.resume_outcome:
                self.resume_outcome = "resumed" if session == self.spec.resume_id else "fresh"
                self.emit("session", state="ready", resume=self.resume_outcome)
            if session:
                self.session_id = session
            if not self.turn_active.is_set():
                # A message written mid-turn that Claude answers as its own turn.
                self.turn_active.set()
                self.turn_id = f"turn-{next(self._turns)}"
                self.emit("turn", status="started")
            return
        if kind == "stream_event":
            event = message.get("event") or {}
            if event.get("type") == "message_start":
                self._message_id = str((event.get("message") or {}).get("id") or "")
            elif event.get("type") == "content_block_delta":
                delta = event.get("delta") or {}
                identity = f"{self._message_id}-{event.get('index', 0)}"
                if delta.get("type") == "text_delta":
                    self.emit("message", id=identity, text=str(delta.get("text") or ""), delta=True)
                elif delta.get("type") == "thinking_delta":
                    self.emit("thought", id=identity, text=str(delta.get("thinking") or ""), delta=True)
            return
        if kind == "assistant":
            body = message.get("message") or {}
            identity = str(body.get("id") or self._message_id)
            for index, block in enumerate(body.get("content") or []):
                if not isinstance(block, dict):
                    continue
                kind_of = block.get("type")
                if kind_of == "text" and str(block.get("text") or "").strip():
                    self.emit("message", id=f"{identity}-final-{index}", text=str(block["text"]), delta=False)
                elif kind_of == "thinking" and str(block.get("thinking") or "").strip():
                    self.emit("thought", id=f"{identity}-final-{index}", text=str(block["thinking"]), delta=False)
                elif kind_of == "tool_use":
                    tool_id = str(block.get("id") or "")
                    name = str(block.get("name") or "tool")
                    arguments = block.get("input") if isinstance(block.get("input"), dict) else {}
                    self._tools[tool_id] = {"name": name, "arguments": arguments}
                    self.emit("tool", id=tool_id, name=name, title=ev.tool_title(name, arguments),
                              status="running", arguments=arguments)
            return
        if kind == "user":
            for block in (message.get("message") or {}).get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                tool_id = str(block.get("tool_use_id") or "")
                known = self._tools.pop(tool_id, {"name": "tool", "arguments": {}})
                content = block.get("content")
                if isinstance(content, list):
                    content = "\n".join(str(one.get("text") or "") for one in content
                                        if isinstance(one, dict) and one.get("type") == "text")
                self.emit("tool", id=tool_id, name=known["name"], title=ev.tool_title(known["name"], known["arguments"]),
                          status="failed" if block.get("is_error") else "finished",
                          arguments=known["arguments"], output=str(content or "")[:4000])
            return
        if kind == "result":
            usage = message.get("usage")
            if usage:
                self.emit("usage", usage=usage, cost=message.get("total_cost_usd"))
            failed = bool(message.get("is_error"))
            text = str(message.get("result") or "") if failed else None
            if self.turn_active.is_set():
                self._turn_finished("failed" if failed else "completed", text=text)
