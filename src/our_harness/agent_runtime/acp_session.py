"""Gemini CLI and GitHub Copilot CLI through the Agent Client Protocol (ACP)."""

from __future__ import annotations

import re
import subprocess
import threading
from typing import Any, Callable

from . import events as ev
from .base import AgentSession, SeatSpec
from .jsonrpc import JsonRpcProcess, RpcError

# Newer CLIs use --acp; Gemini CLI before 0.33 only has --experimental-acp.
ACP_FLAGS = {"gemini-cli": ("--acp", "--experimental-acp"), "copilot-cli": ("--acp",)}
# The account the CLI already signed in with; never an API key Nexus picks.
AUTH_METHODS = ("oauth-personal", "login-with-github", "github", "copilot")


def acp_flag(kind: str, executable: str) -> str:
    """Which ACP switch this installed CLI understands (from its --help)."""

    choices = ACP_FLAGS.get(kind, ("--acp",))
    try:
        # No stdin: Gemini reads piped input, and an inherited open pipe made
        # --help wait until the timeout inside the server.
        done = subprocess.run([executable, "--help"], capture_output=True, timeout=60, stdin=subprocess.DEVNULL,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        text = (done.stdout + done.stderr).decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return choices[-1]  # The oldest spelling is the one every version with ACP still accepts.
    for flag in choices:
        if re.search(r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])", text):
            return flag
    return choices[-1]


class AcpSession(AgentSession):
    can_steer = False  # ACP has no mid-turn input; messages wait for the next turn.

    def __init__(self, spec: SeatSpec, on_event: Callable[[dict[str, Any]], None], *,
                 executable: str, env: dict[str, str] | None = None,
                 approver: Callable[[dict[str, Any]], str] | None = None, flag: str | None = None):
        super().__init__(spec, on_event)
        self.executable = executable
        self.env = env
        self.approver = approver
        self.flag = flag or acp_flag(spec.kind, executable)
        self.rpc: JsonRpcProcess | None = None
        self.acp_session = ""

    def start(self) -> None:
        self.rpc = JsonRpcProcess([self.executable, *list((self.spec.command or [])[1:]), self.flag],
                                  cwd=self.spec.cwd, env=self.env, on_notification=self._notification,
                                  on_request=self._server_request, label=f"{self.spec.name} ({self.spec.kind})")
        init = self.rpc.request("initialize", {"protocolVersion": 1, "clientCapabilities": {
            "fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}}, timeout=90) or {}
        methods = [str(one.get("id")) for one in init.get("authMethods") or [] if isinstance(one, dict)]
        mcp = []
        if self.spec.mcp:
            command = list(self.spec.mcp.get("command") or [])
            mcp = [{"name": "nexus", "command": command[0], "args": command[1:],
                    "env": [{"name": k, "value": v} for k, v in (self.spec.mcp.get("env") or {}).items()]}]
        params = {"cwd": self.spec.cwd, "mcpServers": mcp}
        can_load = bool((init.get("agentCapabilities") or {}).get("loadSession"))
        try:
            opened = self._open(params, can_load)
        except RpcError as exc:
            if "auth" not in str(exc).lower():
                raise
            chosen = next((one for one in AUTH_METHODS if one in methods), methods[0] if methods else "")
            if not chosen:
                raise
            try:
                self.rpc.request("authenticate", {"methodId": chosen}, timeout=120)
            except RpcError as auth_error:
                hint = (" Set the Google Cloud project on this Gemini route (Settings, provider routes) and try again."
                        if "GOOGLE_CLOUD_PROJECT" in str(auth_error) else "")
                raise RpcError(f"{self.spec.name} could not use its saved sign-in: {auth_error}{hint}") from auth_error
            opened = self._open(params, can_load)
        self.acp_session = opened
        self.session_id = opened
        self._set_state("ready")

    def _open(self, params: dict[str, Any], can_load: bool) -> str:
        assert self.rpc is not None
        if self.spec.resume_id and can_load:
            try:
                self.rpc.request("session/load", {**params, "sessionId": self.spec.resume_id}, timeout=90)
                self.resume_outcome = "resumed"
                return self.spec.resume_id
            except RpcError:
                self.resume_outcome = "fresh"
        elif self.spec.resume_id:
            self.resume_outcome = "fresh"
        result = self.rpc.request("session/new", params, timeout=90) or {}
        session = str(result.get("sessionId") or "")
        if not session:
            raise RpcError(f"{self.spec.name} did not open a session.")
        return session

    def close(self) -> None:
        self.state = "closed"
        if self.rpc is not None:
            self.rpc.close()
        self.emit("session", state="closed")

    def interrupt(self) -> None:
        if self.rpc is not None and self.acp_session:
            try:
                self.rpc.notify("session/cancel", {"sessionId": self.acp_session})
            except Exception:
                pass

    def _start_turn(self, text: str) -> None:
        assert self.rpc is not None
        prompt = text if not self.spec.instructions or self.turns_completed else (
            self.spec.instructions + "\n\n" + text)
        self.emit("turn", status="started")

        def run() -> None:
            try:
                result = self.rpc.request("session/prompt", {"sessionId": self.acp_session,
                                                             "prompt": [{"type": "text", "text": prompt}]},
                                          timeout=3600) or {}
                reason = str(result.get("stopReason") or "end_turn")
                self._turn_finished("interrupted" if reason == "cancelled" else "completed")
            except Exception as exc:
                self.error = str(exc)[:500]
                self._turn_finished("failed", text=self.error)
        threading.Thread(target=run, name=f"{self.spec.name}-acp-turn", daemon=True).start()

    def _notification(self, method: str, params: dict[str, Any]) -> None:
        if method != "session/update":
            return
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")
        content = update.get("content") if isinstance(update.get("content"), dict) else {}
        if kind == "agent_message_chunk":
            self.emit("message", id="acp-message", text=str(content.get("text") or ""), delta=True)
        elif kind == "agent_thought_chunk":
            self.emit("thought", id="acp-thought", text=str(content.get("text") or ""), delta=True)
        elif kind in ("tool_call", "tool_call_update"):
            status = {"pending": "running", "in_progress": "running", "completed": "finished",
                      "failed": "failed"}.get(str(update.get("status") or "in_progress"), "running")
            name = str(update.get("kind") or "tool")
            arguments = update.get("rawInput") if isinstance(update.get("rawInput"), dict) else {}
            title = str(update.get("title") or ev.tool_title(name, arguments))
            self.emit("tool", id=str(update.get("toolCallId") or ""), name=name, title=title, status=status,
                      arguments=arguments or None)
        elif kind == "plan":
            self.emit("plan", entries=update.get("entries") or [])

    def _server_request(self, method: str, params: dict[str, Any]) -> Any:
        if method == "session/request_permission":
            options = params.get("options") or []
            tool = params.get("toolCall") or {}
            question = {"method": method, "title": tool.get("title"), "kind": tool.get("kind")}
            if self.spec.access == "full":
                decision = "accept"
            elif self.spec.access == "read_only":
                decision = "decline"
            else:
                decision = self.approver(question) if self.approver else "decline"
            wanted = "allow" if decision in ("accept", "acceptForSession") else "reject"
            option = next((one for one in options if wanted in str(one.get("kind") or "")), None)
            if option is None:
                return {"outcome": {"outcome": "cancelled"}}
            return {"outcome": {"outcome": "selected", "optionId": option["optionId"]}}
        return {}
