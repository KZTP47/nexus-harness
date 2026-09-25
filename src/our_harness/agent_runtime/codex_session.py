"""Codex as one persistent ``codex app-server`` thread (JSON-RPC over stdio)."""

from __future__ import annotations

from typing import Any, Callable

from . import events as ev
from .base import AgentSession, SeatSpec
from .jsonrpc import JsonRpcProcess, RpcError

CLIENT = {"name": "nexus-harness", "title": "Nexus Harness", "version": "3.0"}

SANDBOX = {"read_only": "read-only", "ask": "workspace-write", "full": "danger-full-access"}
# Codex's native Windows sandbox is experimental (best-effort on Windows 10):
# measured here, "workspace-write" + "on-request" let a write outside the
# project through without asking. "untrusted" asks before every command that
# is not a known-safe read, so Ask really asks and Read only really declines.
APPROVAL = {"read_only": "untrusted", "ask": "untrusted", "full": "never"}


class CodexSession(AgentSession):
    can_steer = True

    def __init__(self, spec: SeatSpec, on_event: Callable[[dict[str, Any]], None], *,
                 executable: str, env: dict[str, str] | None = None,
                 approver: Callable[[dict[str, Any]], str] | None = None):
        super().__init__(spec, on_event)
        self.executable = executable
        self.env = env
        self.approver = approver
        self.rpc: JsonRpcProcess | None = None
        self.thread_id = ""
        self._items: dict[str, dict[str, Any]] = {}

    # -- lifecycle

    def start(self) -> None:
        self.rpc = JsonRpcProcess(
            [self.executable, *self.spec_args(), "app-server"], cwd=self.spec.cwd, env=self.env,
            on_notification=self._notification, on_request=self._server_request,
            jsonrpc_field=False, label=f"{self.spec.name} (Codex)",
        )
        self.rpc.request("initialize", {"clientInfo": CLIENT}, timeout=60)
        self.rpc.notify("initialized")
        settings = {
            "cwd": self.spec.cwd,
            "sandbox": SANDBOX.get(self.spec.access, "workspace-write"),
            "approvalPolicy": APPROVAL.get(self.spec.access, "on-request"),
            **({"model": self.spec.model} if self.spec.model else {}),
            **({"developerInstructions": self.spec.instructions} if self.spec.instructions else {}),
            **({"config": self._config()} if self.spec.mcp else {}),
        }
        if self.spec.resume_id:
            try:
                result = self.rpc.request("thread/resume", {"threadId": self.spec.resume_id, **settings,
                                                            "excludeTurns": True}, timeout=90)
                resumed = ((result or {}).get("thread") or {}).get("id")
                if resumed == self.spec.resume_id:
                    self.thread_id = resumed
                    self.resume_outcome = "resumed"
            except RpcError:
                self.resume_outcome = "fresh"
        if not self.thread_id:
            result = self.rpc.request("thread/start", settings, timeout=90)
            self.thread_id = ((result or {}).get("thread") or {}).get("id") or ""
            if self.spec.resume_id and not self.resume_outcome:
                self.resume_outcome = "fresh"
        if not self.thread_id:
            raise RpcError("Codex did not open a conversation thread.")
        self.session_id = self.thread_id
        self._set_state("ready")

    def spec_args(self) -> list[str]:
        return list((self.spec.command or [])[1:])

    def _config(self) -> dict[str, Any]:
        command = list(self.spec.mcp.get("command") or [])
        server = {"command": command[0], "args": command[1:]}
        if self.spec.mcp.get("env"):
            server["env"] = dict(self.spec.mcp["env"])
        return {"mcp_servers": {"nexus": server}}

    def close(self) -> None:
        self.state = "closed"
        if self.rpc is not None:
            self.rpc.close()
        self.emit("session", state="closed")

    # -- turns

    def _start_turn(self, text: str) -> None:
        assert self.rpc is not None
        result = self.rpc.request("turn/start", {"threadId": self.thread_id,
                                                 "input": [{"type": "text", "text": text}]}, timeout=60)
        turn = (result or {}).get("turn") or {}
        if turn.get("id"):
            self.turn_id = str(turn["id"])

    def _steer(self, text: str) -> bool:
        if self.rpc is None or not self.turn_id:
            return False
        try:
            self.rpc.request("turn/steer", {"threadId": self.thread_id, "expectedTurnId": self.turn_id,
                                            "input": [{"type": "text", "text": text}]}, timeout=30)
            return True
        except RpcError:
            return False

    def interrupt(self) -> None:
        if self.rpc is not None and self.turn_id:
            try:
                self.rpc.request("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id}, timeout=15)
            except RpcError:
                pass

    # -- stream mapping

    def _notification(self, method: str, params: dict[str, Any]) -> None:
        if params.get("threadId") and self.thread_id and params["threadId"] != self.thread_id:
            return
        if method == "turn/started":
            self.turn_id = str((params.get("turn") or {}).get("id") or self.turn_id)
            self.emit("turn", status="started")
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            status = {"completed": "completed", "interrupted": "interrupted", "failed": "failed"}.get(
                str(turn.get("status") or "completed"), "completed")
            error = (turn.get("error") or {}).get("message") if isinstance(turn.get("error"), dict) else None
            self._turn_finished(status, text=error)
        elif method == "item/agentMessage/delta":
            self.emit("message", id=params.get("itemId"), text=str(params.get("delta") or ""), delta=True)
        elif method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
            self.emit("thought", id=params.get("itemId"), text=str(params.get("delta") or ""), delta=True)
        elif method in ("item/started", "item/completed"):
            self._item(params.get("item") or {}, done=method == "item/completed")
        elif method == "turn/plan/updated":
            self.emit("plan", entries=params.get("plan") or [], text=params.get("explanation"))
        elif method == "thread/tokenUsage/updated":
            self.emit("usage", usage=params.get("tokenUsage"))
        elif method == "error":
            error = params.get("error") or {}
            text = error.get("message") if isinstance(error, dict) else str(error)
            self.emit("notice", level="error", text=f"Codex: {text}", retrying=bool(params.get("willRetry")))

    def _item(self, item: dict[str, Any], *, done: bool) -> None:
        kind = item.get("type")
        identity = str(item.get("id") or "")
        if kind == "agentMessage":
            if done:
                self.emit("message", id=identity, text=str(item.get("text") or ""), delta=False)
            return
        if kind == "reasoning":
            if done:
                summary = item.get("summary")
                text = "\n".join(str(one) for one in summary) if isinstance(summary, list) else str(summary or "")
                if text.strip():
                    self.emit("thought", id=identity, text=text, delta=False)
            return
        if kind == "plan" and done:
            self.emit("plan", id=identity, text=str(item.get("text") or ""))
            return
        if kind not in ("commandExecution", "fileChange", "mcpToolCall", "webSearch", "dynamicToolCall",
                        "collabAgentToolCall"):
            return
        name = {"commandExecution": "command", "fileChange": "edit", "webSearch": "web_search"}.get(
            kind, str(item.get("tool") or kind))
        arguments: dict[str, Any] = {}
        if kind == "commandExecution":
            arguments = {"command": item.get("command")}
        elif kind == "fileChange":
            arguments = {"changes": item.get("changes") or []}
        elif kind == "webSearch":
            arguments = {"query": item.get("query")}
        else:
            arguments = {"server": item.get("server"), "arguments": item.get("arguments")}
        failed = done and (item.get("status") in ("failed", "declined") or bool(item.get("error"))
                           or (isinstance(item.get("exitCode"), int) and item["exitCode"] != 0))
        output = item.get("aggregatedOutput") if kind == "commandExecution" else item.get("result") or item.get("error")
        self.emit("tool", id=identity, name=name, title=ev.tool_title(name, arguments),
                  status=("failed" if failed else "finished") if done else "running",
                  arguments=arguments, output=str(output)[:4000] if output not in (None, "") else None,
                  exit_code=item.get("exitCode"))

    def _server_request(self, method: str, params: dict[str, Any]) -> Any:
        if method == "mcpServer/elicitation/request":
            meta = params.get("_meta") or {}
            if meta.get("codex_approval_kind") == "mcp_tool_call":
                # Nexus's own team tools are always allowed; other servers' tools
                # follow the access mode like commands do.
                if params.get("serverName") == "nexus" or self.spec.access == "full":
                    return {"action": "accept", "content": {}, "_meta": {"persist": "session"}}
                if self.spec.access == "read_only":
                    return {"action": "decline"}
                decision = self._ask({"method": method, "server": params.get("serverName"),
                                      "message": params.get("message"), "tool": meta.get("tool_params")})
                return {"action": "accept" if decision in ("accept", "acceptForSession") else "decline",
                        **({"content": {}} if decision in ("accept", "acceptForSession") else {})}
            return {"action": "decline"}
        if "requestApproval" in method or method in ("execCommandApproval", "applyPatchApproval"):
            if self.spec.access == "read_only":
                return {"decision": "decline"}
            question = {"method": method, "command": params.get("command"), "reason": params.get("reason"),
                        "cwd": params.get("cwd"), "changes": params.get("changes") or params.get("fileChanges")}
            return {"decision": self._ask(question)}
        if method == "item/tool/requestUserInput":
            return {"answers": {}}
        return {}

    def _ask(self, question: dict[str, Any]) -> str:
        return self.approver(question) if self.approver else "decline"
