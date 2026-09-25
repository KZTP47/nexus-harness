"""Line-delimited JSON-RPC over a child process's stdio (Codex app-server, ACP).

Plain pipes, never a PTY: Gemini's ACP mode hangs under a TTY and ConPTY has
broken-pipe bugs on Windows. Reading happens on daemon threads; requests wait
on per-id events with a deadline; notifications and server-initiated requests
go to handlers.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections import deque
from typing import Any, Callable

from ..models import HarnessError


class RpcError(HarnessError):
    def __init__(self, message: str, error: Any = None):
        super().__init__(message)
        self.error = error


class JsonRpcProcess:
    def __init__(
        self,
        argv: list[str],
        *,
        cwd: str,
        env: dict[str, str] | None = None,
        on_notification: Callable[[str, dict[str, Any]], None] | None = None,
        on_request: Callable[[str, dict[str, Any]], Any] | None = None,
        jsonrpc_field: bool = True,
        label: str = "agent",
    ) -> None:
        self.label = label
        self.jsonrpc_field = jsonrpc_field
        self.on_notification = on_notification or (lambda method, params: None)
        self.on_request = on_request or (lambda method, params: None)
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            self.process = subprocess.Popen(
                argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, creationflags=flags,
            )
        except OSError as exc:
            raise HarnessError(f"{label} could not start: {exc}") from exc
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._next_id = 0
        self._waiting: dict[int, dict[str, Any]] = {}
        self.stderr_tail: deque[str] = deque(maxlen=40)
        self.closed = threading.Event()
        threading.Thread(target=self._read_stdout, name=f"{label}-rpc-out", daemon=True).start()
        threading.Thread(target=self._read_stderr, name=f"{label}-rpc-err", daemon=True).start()

    # -- plumbing

    def _send(self, message: dict[str, Any]) -> None:
        if self.jsonrpc_field:
            message = {"jsonrpc": "2.0", **message}
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        with self._write_lock:
            if self.process.poll() is not None or self.process.stdin is None:
                raise HarnessError(f"{self.label} has stopped. {self.last_error()}".strip())
            try:
                self.process.stdin.write(data)
                self.process.stdin.flush()
            except OSError as exc:
                raise HarnessError(f"{self.label} stopped accepting input: {exc}") from exc

    def _read_stdout(self) -> None:
        try:
            for raw in self.process.stdout:  # type: ignore[union-attr]
                line = raw.strip()
                if not line or not line.startswith(b"{"):
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                self._dispatch(message)
        finally:
            self.closed.set()
            with self._state_lock:
                for waiter in self._waiting.values():
                    waiter["error"] = RpcError(f"{self.label} stopped. {self.last_error()}".strip())
                    waiter["event"].set()

    def _read_stderr(self) -> None:
        for raw in self.process.stderr:  # type: ignore[union-attr]
            text = raw.decode("utf-8", "replace").rstrip()
            if text:
                self.stderr_tail.append(text[:500])

    def _dispatch(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if method is not None and "id" in message:
            threading.Thread(target=self._answer, args=(message,), daemon=True).start()
            return
        if method is not None:
            try:
                self.on_notification(str(method), message.get("params") or {})
            except Exception:
                pass
            return
        identity = message.get("id")
        with self._state_lock:
            waiter = self._waiting.get(identity) if isinstance(identity, int) else None
        if waiter is None:
            return
        if "error" in message:
            error = message["error"] or {}
            text = str(error.get("message") or error) if isinstance(error, dict) else str(error)
            data = error.get("data") if isinstance(error, dict) else None
            detail = data.get("details") if isinstance(data, dict) else data if isinstance(data, str) else ""
            if detail and str(detail) not in text:
                text = f"{text}: {str(detail)[:800]}"
            waiter["error"] = RpcError(text, error)
        else:
            waiter["result"] = message.get("result")
        waiter["event"].set()

    def _answer(self, message: dict[str, Any]) -> None:
        try:
            result = self.on_request(str(message["method"]), message.get("params") or {})
            self._send({"id": message["id"], "result": result if result is not None else {}})
        except Exception as exc:
            try:
                self._send({"id": message["id"], "error": {"code": -32000, "message": str(exc)[:500]}})
            except Exception:
                pass

    # -- API

    def request(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 120.0) -> Any:
        with self._state_lock:
            self._next_id += 1
            identity = self._next_id
            waiter = {"event": threading.Event()}
            self._waiting[identity] = waiter
        try:
            self._send({"id": identity, "method": method, **({"params": params} if params is not None else {})})
            if not waiter["event"].wait(timeout):
                raise RpcError(f"{self.label} did not answer {method} within {int(timeout)} s")
            if "error" in waiter:
                raise waiter["error"]
            return waiter.get("result")
        finally:
            with self._state_lock:
                self._waiting.pop(identity, None)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"method": method, **({"params": params} if params is not None else {})})

    def alive(self) -> bool:
        return self.process.poll() is None

    def last_error(self) -> str:
        return " ".join(list(self.stderr_tail)[-3:])[:600]

    def close(self, grace: float = 3.0) -> None:
        try:
            if self.process.stdin:
                self.process.stdin.close()
        except OSError:
            pass
        deadline = time.monotonic() + grace
        while self.process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if self.process.poll() is None:
            kill_tree(self.process)


def kill_tree(process: subprocess.Popen) -> None:
    """Stop a CLI and everything it started (Windows: the whole process tree)."""

    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True,
                           timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        process.kill()
    except OSError:
        pass
