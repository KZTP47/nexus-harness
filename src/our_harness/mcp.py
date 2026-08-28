from __future__ import annotations

import codecs
import ctypes
import ipaddress
import json
import os
import queue
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

from .config import LoadedConfig
from .models import HarnessError


# What a program we start is told about this machine. Every one of these is a
# path or a language, and none of them is a secret; the list is the one the
# runner already uses, so a program started here and a job run there find the
# same things.
STARTS_A_PROGRAM = (
    "PATH", "PATHEXT", "SYSTEMDRIVE", "SYSTEMROOT", "WINDIR", "TMP", "TEMP",
    "LANG", "LC_ALL", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOMEDRIVE",
    "HOMEPATH", "HOME",
)

MODERN_PROTOCOL_VERSION = "2026-07-28"
LATEST_LEGACY_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_LEGACY_PROTOCOL_VERSIONS = frozenset(
    {LATEST_LEGACY_PROTOCOL_VERSION, "2025-06-18", "2025-03-26", "2024-11-05", "2024-10-07"}
)
_CLIENT_INFO = {"name": "our-harness", "version": "0.2.0"}


class MCPRemoteError(HarnessError):
    """A well-formed JSON-RPC error returned by an MCP peer."""

    def __init__(self, method: str, error: object, diagnostic: str = "") -> None:
        self.method = method
        self.error = error
        self.code = error.get("code") if isinstance(error, dict) else None
        super().__init__(f"MCP {method} failed: {error}{diagnostic}")


def configured_server(config: LoadedConfig, name: str) -> dict[str, Any]:
    matches = [server for server in config.get("mcp.servers", []) if server.get("name") == name]
    if not matches:
        raise HarnessError(f"Configured MCP server not found: {name}")
    if len(matches) > 1:
        raise HarnessError(f"MCP server name is not unique: {name}")
    return dict(matches[0])


def _validated_http_url(url: object) -> str:
    if not isinstance(url, str):
        raise HarnessError("MCP HTTP URL must use HTTPS or loopback HTTP")
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.fragment or not parsed.hostname:
        raise HarnessError("MCP HTTP URL must not contain credentials or a fragment")
    loopback = parsed.hostname.lower() == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = False
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise HarnessError("MCP HTTP URL must use HTTPS or loopback HTTP")
    return url


class _ValidatedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        # An MCP server is granted authority for one reviewed endpoint. Do not
        # let it redirect the client to another remote origin or a local
        # service. Refusing all redirects also prevents HTTPS downgrades.
        raise HarnessError("MCP HTTP redirects are not accepted")


def _interrupt_response(response: Any) -> None:
    stream = getattr(response, "fp", None)
    raw = getattr(stream, "raw", None)
    connection = getattr(raw, "_sock", None)
    if connection is not None:
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            connection.close()
        except OSError:
            pass
    try:
        response.close()
    except OSError:
        pass


class _SSEJSONDecoder:
    def __init__(self) -> None:
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        self.buffer = ""
        self.data_lines: list[str] = []

    def feed(self, chunk: bytes, *, final: bool = False) -> list[dict[str, Any]]:
        self.buffer += self.decoder.decode(chunk, final=final)
        lines = self.buffer.split("\n")
        if final:
            self.buffer = ""
        else:
            self.buffer = lines.pop()
        values: list[dict[str, Any]] = []
        for raw_line in lines:
            line = raw_line.rstrip("\r")
            if line == "":
                value = self._finish_event()
                if value is not None:
                    values.append(value)
            elif line.startswith("data:"):
                data = line[5:]
                self.data_lines.append(data[1:] if data.startswith(" ") else data)
        if final:
            value = self._finish_event()
            if value is not None:
                values.append(value)
        return values

    def _finish_event(self) -> dict[str, Any] | None:
        if not self.data_lines:
            return None
        raw = "\n".join(self.data_lines)
        self.data_lines.clear()
        if raw == "[DONE]":
            return None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HarnessError("MCP HTTP SSE event was not JSON") from exc
        if not isinstance(value, dict):
            raise HarnessError("MCP HTTP SSE event must be an object")
        return value


class MCPClient:
    """Bounded JSON-RPC client for configured MCP servers."""

    def __init__(self, server: dict[str, Any], timeout: int | float = 60, max_response_bytes: int = 1_000_000):
        self.server = server
        self.timeout = max(0.001, float(timeout))
        self.max_response_bytes = max_response_bytes
        self.sequence = 0
        self.process: subprocess.Popen[str] | None = None
        self.lock = threading.Lock()
        self.stderr_lock = threading.Lock()
        self.stderr_limit = max(1, min(max_response_bytes, 65_536))
        self.stderr_buffer = bytearray()
        self.stderr_truncated = False
        self.stderr_thread: threading.Thread | None = None
        self.stdio_reader_thread: threading.Thread | None = None
        self.stdio_writer_thread: threading.Thread | None = None
        self.protocol_version: str | None = None
        self.protocol_era: str | None = None
        self.discovery: dict[str, Any] | None = None
        self.session_id: str | None = None
        self.cache_hints: dict[str, dict[str, object]] = {}
        self.notifications: list[dict[str, Any]] = []
        self._notification_bytes = 0
        self._http_opener = urllib.request.build_opener(_ValidatedRedirectHandler())
        self._windows_job: int | None = None

    def connect(self) -> dict[str, Any]:
        deadline_at = time.monotonic() + self.timeout
        self.cache_hints.clear()
        mode = self.server.get("protocol_mode", "legacy")
        if mode not in ("legacy", "auto", "modern"):
            raise HarnessError("MCP protocol_mode must be legacy, auto, or modern")
        try:
            if mode == "legacy":
                return self._connect_legacy(deadline_at)
            if self.server.get("transport", "stdio") == "stdio":
                discovery = self._probe_stdio(mode, deadline_at)
                if discovery is None:
                    return self._connect_legacy(deadline_at)
                self._start_stdio()
            else:
                self.protocol_era = "modern"
                self.protocol_version = MODERN_PROTOCOL_VERSION
                try:
                    discovery = self._request("server/discover", {}, deadline_at)
                except MCPRemoteError as exc:
                    if mode == "auto" and exc.code == -32601:
                        self.protocol_era = None
                        self.protocol_version = None
                        return self._connect_legacy(deadline_at)
                    raise
            self._accept_discovery(discovery)
            return discovery
        except Exception:
            self.close()
            raise

    def _start_stdio(self, *, discard_stderr: bool = False) -> None:
        command = self.server.get("command")
        args = self.server.get("args", [])
        if not isinstance(command, str) or not command:
            raise HarnessError("MCP stdio server needs a command")
        popen_options: dict[str, Any] = {}
        if os.name == "nt":
            popen_options["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        else:
            popen_options["start_new_session"] = True
        self.process = subprocess.Popen(
            [command, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL if discard_stderr else subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            # The same names a job we run gets, and for the same reason: these
            # are paths, not secrets, and a program that cannot find the home
            # folder cannot start at all. Without them our own server fell over
            # before it said a word, and all our client saw was a closed pipe.
            env={name: os.environ[name] for name in STARTS_A_PROGRAM if name in os.environ},
            **popen_options,
        )
        if os.name == "nt":
            self._windows_job = self._create_windows_job(self.process)
        self.stderr_buffer.clear()
        self.stderr_truncated = False
        if self.process.stderr is not None:
            self.stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
            self.stderr_thread.start()

    def _connect_legacy(self, deadline_at: float) -> dict[str, Any]:
        self.protocol_era = "legacy"
        self.protocol_version = None
        self.session_id = None
        if self.server.get("transport", "stdio") == "stdio" and self.process is None:
            self._start_stdio()
        result = self._request(
            "initialize",
            {
                "protocolVersion": LATEST_LEGACY_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": dict(_CLIENT_INFO),
            },
            deadline_at,
        )
        negotiated = result.get("protocolVersion")
        if not isinstance(negotiated, str) or negotiated not in SUPPORTED_LEGACY_PROTOCOL_VERSIONS:
            raise HarnessError(f"MCP server negotiated an unsupported protocol version: {negotiated!r}")
        self.protocol_version = negotiated
        self._notify("notifications/initialized", {}, deadline_at)
        return result

    def _probe_stdio(self, mode: str, deadline_at: float) -> dict[str, Any] | None:
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            raise HarnessError("MCP request timed out")
        probe_timeout = remaining if mode == "modern" else max(0.001, remaining * 0.35)
        probe = MCPClient(self.server, timeout=probe_timeout, max_response_bytes=self.max_response_bytes)
        probe.protocol_era = "modern"
        probe.protocol_version = MODERN_PROTOCOL_VERSION
        try:
            probe._start_stdio(discard_stderr=True)
            return probe._request("server/discover", {}, min(deadline_at, time.monotonic() + probe_timeout))
        except MCPRemoteError as exc:
            if mode == "auto" and exc.code == -32601:
                return None
            raise
        except HarnessError as exc:
            legacy_signal = any(text in str(exc).lower() for text in ("timed out", "closed its output", "not running", "write failed"))
            if mode == "auto" and legacy_signal:
                return None
            raise
        finally:
            probe.close()

    def _accept_discovery(self, result: dict[str, Any]) -> None:
        versions = result.get("supportedVersions")
        capabilities = result.get("capabilities")
        if (
            not isinstance(versions, list)
            or any(not isinstance(version, str) or not version for version in versions)
            or MODERN_PROTOCOL_VERSION not in versions
            or not isinstance(capabilities, dict)
        ):
            raise HarnessError("MCP server/discover did not advertise a supported modern protocol")
        self.protocol_era = "modern"
        self.protocol_version = MODERN_PROTOCOL_VERSION
        self.session_id = None
        self._cache_hint("server/discover", result)
        self.discovery = json.loads(json.dumps(result))

    def close(self) -> None:
        process = self.process
        if process:
            self._terminate_process_tree(process)
        if self.stderr_thread and self.stderr_thread is not threading.current_thread():
            self.stderr_thread.join(timeout=min(0.05, self.timeout))
        if process and process.stdin:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process and process.stdout and not (self.stdio_reader_thread and self.stdio_reader_thread.is_alive()):
            try:
                process.stdout.close()
            except OSError:
                pass
        if process and process.stderr and not (self.stderr_thread and self.stderr_thread.is_alive()):
            try:
                process.stderr.close()
            except OSError:
                pass
        self.process = None
        self.stderr_thread = None
        if not (self.stdio_reader_thread and self.stdio_reader_thread.is_alive()):
            self.stdio_reader_thread = None
        if self.stdio_writer_thread and self.stdio_writer_thread is not threading.current_thread():
            self.stdio_writer_thread.join(timeout=0.25)
        if not (self.stdio_writer_thread and self.stdio_writer_thread.is_alive()):
            self.stdio_writer_thread = None
        self.protocol_version = None
        self.protocol_era = None
        self.discovery = None
        self.session_id = None

    @staticmethod
    def _create_windows_job(process: subprocess.Popen[str]) -> int | None:
        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        limits.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        configured = kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
        process_handle = ctypes.c_void_p(int(getattr(process, "_handle")))
        assigned = configured and kernel32.AssignProcessToJobObject(job, process_handle)
        if not assigned:
            kernel32.CloseHandle(job)
            return None
        return int(job)

    def _terminate_process_tree(self, process: subprocess.Popen[str]) -> None:
        closed_job = False
        if os.name == "nt" and self._windows_job is not None:
            try:
                ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(ctypes.c_void_p(self._windows_job))
                closed_job = True
            except (OSError, ValueError):
                pass
            self._windows_job = None
        if os.name == "nt" and not closed_job:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=0.25,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        elif os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=0.05)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def __enter__(self) -> "MCPClient":
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._notify(method, params, time.monotonic() + self.timeout)

    def _notify(self, method: str, params: dict[str, Any], deadline_at: float) -> None:
        message = {"jsonrpc": "2.0", "method": method, "params": params}
        if self.server.get("transport", "stdio") == "stdio":
            self._write_stdio(message, deadline_at)
        else:
            self._post_http(message, allow_empty=True, deadline_at=deadline_at)

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._request(method, params, time.monotonic() + self.timeout)

    def _request(self, method: str, params: dict[str, Any], deadline_at: float) -> dict[str, Any]:
        self.sequence += 1
        request_id = self.sequence
        wire_params = json.loads(json.dumps(params))
        if self.protocol_era == "modern":
            existing_meta = wire_params.get("_meta")
            meta = dict(existing_meta) if isinstance(existing_meta, dict) else {}
            meta.update(
                {
                    "io.modelcontextprotocol/protocolVersion": MODERN_PROTOCOL_VERSION,
                    "io.modelcontextprotocol/clientInfo": dict(_CLIENT_INFO),
                    "io.modelcontextprotocol/clientCapabilities": {},
                }
            )
            wire_params["_meta"] = meta
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": wire_params}
        if self.server.get("transport", "stdio") == "stdio":
            self._write_stdio(message, deadline_at)
            response = self._read_stdio(request_id, deadline_at)
        else:
            response = self._post_http(message, deadline_at=deadline_at)
        response_id = response.get("id")
        if response.get("jsonrpc") != "2.0" or type(response_id) is not int or response_id != request_id:
            raise HarnessError(f"MCP {method} returned a mismatched JSON-RPC response")
        if "error" in response:
            error = response["error"]
            if not isinstance(error, dict) or type(error.get("code")) is not int or not isinstance(error.get("message"), str):
                raise HarnessError(f"MCP {method} returned a malformed JSON-RPC error")
            raise MCPRemoteError(method, error, self._stderr_diagnostic())
        result = response.get("result", {})
        if not isinstance(result, dict):
            raise HarnessError(f"MCP {method} result must be an object")
        if self.protocol_era == "modern" and not isinstance(result.get("resultType"), str):
            raise HarnessError(f"MCP {method} modern result must include resultType")
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        deadline_at = time.monotonic() + self.timeout
        self.cache_hints.pop("tools/list", None)
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        total_bytes = 0
        pages = 0
        while True:
            pages += 1
            if pages > 10_000:
                raise HarnessError("MCP tools/list returned too many pages")
            params = {"cursor": cursor} if cursor is not None else {}
            result = self._request("tools/list", params, deadline_at)
            self._cache_hint("tools/list", result)
            page = result.get("tools", [])
            if not isinstance(page, list) or any(not isinstance(tool, dict) for tool in page):
                raise HarnessError("MCP tools/list tools must be an array of objects")
            total_bytes += len(json.dumps(page, separators=(",", ":")).encode("utf-8"))
            if total_bytes > self.max_response_bytes:
                raise HarnessError("MCP tools/list pages exceeded the aggregate response limit")
            tools.extend(page)
            if len(tools) > 100_000:
                raise HarnessError("MCP tools/list returned too many tools")
            next_cursor = result.get("nextCursor")
            if next_cursor is None:
                return tools
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                raise HarnessError("MCP tools/list returned an invalid or repeated nextCursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def _cache_hint(self, method: str, result: dict[str, Any]) -> None:
        if self.protocol_era != "modern":
            return
        ttl = result.get("ttlMs")
        scope = result.get("cacheScope")
        if type(ttl) is not int or ttl < 0:
            ttl = 0
        if scope not in ("private", "public"):
            scope = "private"
        current = self.cache_hints.get(method)
        if current is not None:
            ttl = min(int(current["ttlMs"]), ttl)
            if current["cacheScope"] == "private":
                scope = "private"
        self.cache_hints[method] = {"ttlMs": ttl, "cacheScope": scope}

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        allowed = set(self.server.get("allowed_tools", []))
        if allowed and name not in allowed:
            raise HarnessError(f"MCP tool is not allowed for this server: {name}")
        return self.request("tools/call", {"name": name, "arguments": arguments})

    def _write_stdio(self, message: dict[str, Any], deadline_at: float) -> None:
        process = self.process
        if not process or not process.stdin or process.poll() is not None:
            raise HarnessError(f"MCP stdio server is not running{self._stderr_diagnostic()}")
        try:
            payload = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise HarnessError(f"MCP stdio request was not JSON serializable: {exc}") from exc
        if len(payload) > self.max_response_bytes:
            raise HarnessError("MCP stdio request exceeded its byte limit")
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            raise HarnessError("MCP stdio write timed out")
        descriptor = process.stdin.fileno()
        completed: queue.Queue[BaseException | None] = queue.Queue(maxsize=1)

        def write_payload() -> None:
            outcome: BaseException | None = None
            try:
                with self.lock:
                    view = memoryview(payload)
                    while view:
                        written = os.write(descriptor, view)
                        if written <= 0:
                            raise OSError("stdio write returned no progress")
                        view = view[written:]
            except BaseException as exc:
                outcome = exc
            try:
                completed.put_nowait(outcome)
            except queue.Full:
                pass

        writer = threading.Thread(target=write_payload, name="harness-mcp-stdio-writer", daemon=True)
        self.stdio_writer_thread = writer
        writer.start()
        try:
            while True:
                remaining = deadline_at - time.monotonic()
                if remaining <= 0:
                    diagnostic = self._stderr_diagnostic()
                    self._terminate_process_tree(process)
                    writer.join(timeout=0.25)
                    if writer.is_alive():
                        try:
                            os.close(descriptor)
                        except OSError:
                            pass
                        writer.join(timeout=0.75)
                    elif process.stdin:
                        try:
                            process.stdin.close()
                        except OSError:
                            pass
                    if writer.is_alive():
                        raise HarnessError(f"MCP stdio writer did not stop after timeout{diagnostic}")
                    raise HarnessError(f"MCP stdio write timed out{diagnostic}")
                try:
                    outcome = completed.get(timeout=min(0.02, remaining))
                except queue.Empty:
                    continue
                writer.join(timeout=0)
                if outcome is not None:
                    raise HarnessError(f"MCP stdio write failed: {outcome}{self._stderr_diagnostic()}") from outcome
                return
        finally:
            if not writer.is_alive() and self.stdio_writer_thread is writer:
                self.stdio_writer_thread = None

    def _read_stdio(self, request_id: int, deadline_at: float) -> dict[str, Any]:
        if not self.process or not self.process.stdout:
            raise HarnessError(f"MCP stdio server is not running{self._stderr_diagnostic()}")

        def read_matching() -> dict[str, Any]:
            consumed = 0
            decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
            text_buffer = ""

            def accept_line(line: str) -> dict[str, Any] | None:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    return None
                if not isinstance(value, dict):
                    raise HarnessError(f"MCP stdio response must be an object{self._stderr_diagnostic()}")
                if self._dispatch_server_message(value, deadline_at, "stdio"):
                    return None
                if value.get("id") == request_id:
                    return value
                return None

            descriptor = self.process.stdout.fileno()
            while True:
                read_size = min(4096, self.max_response_bytes - consumed + 1)
                if read_size <= 0:
                    raise HarnessError(f"MCP response exceeded its limit{self._stderr_diagnostic()}")
                chunk = os.read(descriptor, read_size)
                if not chunk:
                    text_buffer += decoder.decode(b"", final=True)
                    if text_buffer:
                        matched = accept_line(text_buffer.rstrip("\r"))
                        if matched is not None:
                            return matched
                    raise HarnessError(f"MCP server closed its output{self._stderr_diagnostic()}")
                consumed += len(chunk)
                if consumed > self.max_response_bytes:
                    raise HarnessError(f"MCP response exceeded its limit{self._stderr_diagnostic()}")
                text_buffer += decoder.decode(chunk)
                lines = text_buffer.split("\n")
                text_buffer = lines.pop()
                for line in lines:
                    matched = accept_line(line.rstrip("\r"))
                    if matched is not None:
                        return matched

        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            raise HarnessError("MCP request timed out")
        received: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)

        def run_reader() -> None:
            try:
                received.put(("result", read_matching()))
            except Exception as exc:
                try:
                    received.put(("error", exc), timeout=0.01)
                except queue.Full:
                    pass

        reader = threading.Thread(target=run_reader, name="harness-mcp-stdio-reader", daemon=True)
        self.stdio_reader_thread = reader
        reader.start()
        try:
            while True:
                remaining = deadline_at - time.monotonic()
                if remaining <= 0:
                    diagnostic = self._stderr_diagnostic()
                    self.close()
                    reader.join(timeout=0.05)
                    raise HarnessError(f"MCP request timed out{diagnostic}")
                try:
                    kind, value = received.get(timeout=min(0.05, remaining))
                except queue.Empty:
                    continue
                if kind == "result" and isinstance(value, dict):
                    return value
                if kind == "error" and isinstance(value, Exception):
                    self.close()
                    reader.join(timeout=0.05)
                    raise value
                raise HarnessError("MCP stdio reader returned an invalid result")
        finally:
            if not reader.is_alive():
                reader.join(timeout=0)
                if self.stdio_reader_thread is reader:
                    self.stdio_reader_thread = None

    def drain_notifications(self) -> list[dict[str, Any]]:
        with self.lock:
            notifications = self.notifications
            self.notifications = []
            self._notification_bytes = 0
        return notifications

    def _dispatch_server_message(self, message: dict[str, Any], deadline_at: float, transport: str) -> bool:
        if "method" not in message:
            return False
        method = message.get("method")
        if message.get("jsonrpc") != "2.0" or not isinstance(method, str) or not method:
            raise HarnessError("MCP server request or notification is malformed")
        if "id" not in message:
            notification_bytes = len(json.dumps(message, separators=(",", ":")).encode("utf-8"))
            with self.lock:
                while self.notifications and (
                    len(self.notifications) >= 256 or self._notification_bytes + notification_bytes > self.max_response_bytes
                ):
                    removed = self.notifications.pop(0)
                    self._notification_bytes -= len(
                        json.dumps(removed, separators=(",", ":")).encode("utf-8")
                    )
                self.notifications.append(json.loads(json.dumps(message)))
                self._notification_bytes += notification_bytes
            return True
        server_id = message.get("id")
        if isinstance(server_id, bool) or not isinstance(server_id, (int, str)):
            raise HarnessError("MCP server request ID must be a string or integer")
        if method == "ping":
            reply = {"jsonrpc": "2.0", "id": server_id, "result": {}}
        else:
            reply = {
                "jsonrpc": "2.0",
                "id": server_id,
                "error": {"code": -32601, "message": "Method not found"},
            }
        if transport == "stdio":
            self._write_stdio(reply, deadline_at)
        else:
            self._post_http(reply, allow_empty=True, deadline_at=deadline_at)
        return True

    def _drain_stderr(self) -> None:
        process = self.process
        if not process or not process.stderr:
            return
        while True:
            try:
                chunk = os.read(process.stderr.fileno(), 4096)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            with self.stderr_lock:
                remaining = self.stderr_limit - len(self.stderr_buffer)
                if remaining > 0:
                    self.stderr_buffer.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self.stderr_truncated = True

    def _stderr_diagnostic(self) -> str:
        with self.stderr_lock:
            raw = bytes(self.stderr_buffer)
            truncated = self.stderr_truncated
        if not raw:
            return ""
        text = raw.decode("utf-8", errors="replace").strip()
        suffix = " [truncated]" if truncated else ""
        return f"; server stderr{suffix}: {text}"

    def _http_headers(self, message: dict[str, Any]) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        if self.protocol_era == "modern":
            method = message.get("method")
            params = message.get("params")
            name = ""
            if isinstance(params, dict):
                for key in ("name", "uri"):
                    candidate = params.get(key)
                    if isinstance(candidate, str):
                        name = candidate
                        break
            if isinstance(method, str):
                headers["Mcp-Method"] = method
                headers["Mcp-Name"] = name
        elif self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def _post_http(
        self,
        message: dict[str, Any],
        allow_empty: bool = False,
        deadline_at: float | None = None,
    ) -> dict[str, Any]:
        url = _validated_http_url(self.server.get("url"))
        deadline_at = deadline_at if deadline_at is not None else time.monotonic() + self.timeout
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            raise HarnessError("MCP HTTP request timed out")
        request = urllib.request.Request(
            url,
            data=json.dumps(message, separators=(",", ":")).encode(),
            headers=self._http_headers(message),
            method="POST",
        )
        stopped = threading.Event()
        received: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=8)
        response_lock = threading.Lock()
        response_holder: dict[str, Any] = {}

        def offer(kind: str, value: object) -> None:
            while not stopped.is_set():
                try:
                    received.put((kind, value), timeout=0.05)
                    return
                except queue.Full:
                    continue

        def read_response() -> None:
            response: Any = None
            try:
                response = self._http_opener.open(request, timeout=max(0.001, remaining))
                final_url = getattr(response, "geturl", lambda: url)()
                _validated_http_url(final_url)
                headers = response.headers
                with response_lock:
                    response_holder["response"] = response
                offer("headers", headers)
                while not stopped.is_set():
                    chunk = response.read(4096)
                    if not chunk:
                        offer("eof", None)
                        return
                    offer("chunk", chunk)
            except urllib.error.HTTPError as exc:
                if self.protocol_era == "modern" and exc.code == 400:
                    if exc.fp is None:
                        offer("error", exc)
                        return
                    response = exc
                    with response_lock:
                        response_holder["response"] = response
                    offer("headers", response.headers)
                    while not stopped.is_set():
                        chunk = response.read(4096)
                        if not chunk:
                            offer("eof", None)
                            return
                        offer("chunk", chunk)
                else:
                    offer("error", exc)
            except Exception as exc:
                offer("error", exc)

        reader = threading.Thread(target=read_response, name="harness-mcp-http-reader", daemon=True)
        reader.start()
        consumed = 0
        raw_parts: list[bytes] = []
        content_type = ""
        sse = _SSEJSONDecoder()
        expected_id = message.get("id")
        try:
            while True:
                remaining_now = deadline_at - time.monotonic()
                if remaining_now <= 0:
                    raise HarnessError("MCP HTTP request timed out at its wall-clock deadline")
                try:
                    kind, value = received.get(timeout=min(0.05, remaining_now))
                except queue.Empty:
                    continue
                if kind == "headers":
                    headers = value
                    content_type = str(headers.get("Content-Type", ""))
                    session_id = headers.get("Mcp-Session-Id") if self.protocol_era != "modern" else None
                    if session_id is not None:
                        if not isinstance(session_id, str) or not session_id or any(ord(char) < 0x21 or ord(char) > 0x7E for char in session_id):
                            raise HarnessError("MCP server returned an invalid session ID")
                        if message.get("method") == "initialize" and self.session_id is None:
                            self.session_id = session_id
                        elif self.session_id != session_id:
                            raise HarnessError("MCP server changed its session ID after initialization")
                    continue
                if kind == "chunk":
                    if not isinstance(value, bytes):
                        raise HarnessError("MCP HTTP reader returned non-byte data")
                    consumed += len(value)
                    if consumed > self.max_response_bytes:
                        raise HarnessError("MCP HTTP response exceeded its limit")
                    if "text/event-stream" in content_type:
                        for candidate in sse.feed(value):
                            if self._dispatch_server_message(candidate, deadline_at, "http"):
                                continue
                            candidate_id = candidate.get("id")
                            if expected_id is None or (type(candidate_id) is int and candidate_id == expected_id):
                                return candidate
                    else:
                        raw_parts.append(value)
                    continue
                if kind == "error":
                    if isinstance(value, urllib.error.HTTPError):
                        status = value.code
                        value.close()
                        raise HarnessError(f"MCP HTTP request failed with status {status}") from value
                    if isinstance(value, Exception):
                        raise HarnessError(f"MCP HTTP request failed: {value}") from value
                    raise HarnessError("MCP HTTP reader failed")
                if kind != "eof":
                    raise HarnessError("MCP HTTP reader returned an unknown event")
                if "text/event-stream" in content_type:
                    candidates = sse.feed(b"", final=True)
                    for candidate in candidates:
                        if self._dispatch_server_message(candidate, deadline_at, "http"):
                            continue
                        candidate_id = candidate.get("id")
                        if expected_id is None or (type(candidate_id) is int and candidate_id == expected_id):
                            return candidate
                    if allow_empty:
                        return {}
                    raise HarnessError("MCP HTTP SSE stream ended without a matching response")
                raw = b"".join(raw_parts)
                if not raw.strip() and allow_empty:
                    return {}
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise HarnessError("MCP HTTP response was not JSON") from exc
                if not isinstance(parsed, dict):
                    raise HarnessError("MCP HTTP response must be an object")
                return parsed
        except (UnicodeDecodeError, OSError) as exc:
            raise HarnessError(f"MCP HTTP response failed: {exc}") from exc
        finally:
            stopped.set()
            with response_lock:
                response = response_holder.get("response")
            if response is not None:
                _interrupt_response(response)
            reader.join(timeout=0.25)
