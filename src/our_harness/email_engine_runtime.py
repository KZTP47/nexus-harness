"""Explicit EmailEngine service ownership; never installs global services.

The caller owns secret storage and supplies configuration. External services are
never started/stopped by Nexus. Local mode owns only its Popen child and requires
an already provisioned durable Redis service (not an arbitrary Redis substitute).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import threading
import time
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .models import HarnessError


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _service_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError()
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError()
    except ValueError as exc:
        raise HarnessError("EmailEngine needs HTTPS, or HTTP on loopback, without URL credentials.") from exc
    return value.rstrip("/")


def redis_preflight(url: str, timeout: float = 3) -> dict:
    """Read-only Redis compatibility probe. Never changes somebody's database."""
    parsed = urlsplit(url)
    if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname or parsed.query or parsed.fragment:
        raise HarnessError("Provide a redis:// or rediss:// URL without query parameters.")
    try:
        database = int(parsed.path.strip("/") or "0")
        if database < 0:
            raise ValueError()
        sock = socket.create_connection((parsed.hostname, parsed.port or 6379), timeout=timeout)
        if parsed.scheme == "rediss":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
        with sock, sock.makefile("rb") as stream:
            def read():
                line = stream.readline(65537)
                if len(line) > 65536 or not line.endswith(b"\r\n"):
                    raise ValueError("Invalid Redis response")
                kind, value = line[:1], line[1:-2]
                if kind == b"-":
                    raise ValueError("Redis rejected probe")
                if kind == b"$":
                    count = int(value)
                    if not 0 <= count <= 1024 * 1024:
                        raise ValueError("Invalid Redis response length")
                    content = stream.read(count + 2)
                    if len(content) != count + 2 or not content.endswith(b"\r\n"):
                        raise ValueError("Incomplete Redis response")
                    return content[:-2].decode("utf-8")
                if kind == b"*":
                    count = int(value)
                    if not 0 <= count <= 100:
                        raise ValueError("Invalid Redis array")
                    return [read() for _ in range(count)]
                if kind in {b"+", b":"}:
                    return value.decode("utf-8")
                raise ValueError("Invalid Redis response")

            def command(*parts):
                encoded = [str(part).encode("utf-8") for part in parts]
                sock.sendall(b"*" + str(len(encoded)).encode() + b"\r\n" + b"".join(
                    b"$" + str(len(part)).encode() + b"\r\n" + part + b"\r\n" for part in encoded))
                return read()

            if parsed.password is not None:
                auth = ["AUTH"]
                if parsed.username:
                    auth.append(unquote(parsed.username))
                command(*auth, unquote(parsed.password))
            command("SELECT", database)
            if command("PING") != "PONG":
                raise ValueError("Redis did not answer PING")
            info = dict(line.split(":", 1) for line in command("INFO").splitlines()
                        if ":" in line and not line.startswith("#"))
            version = tuple(int(part) for part in info.get("redis_version", "0.0").split(".")[:2])
            if version < (6, 2) or info.get("cluster_enabled") == "1":
                raise HarnessError("EmailEngine requires standalone Redis 6.2 or newer.")
            eviction = command("CONFIG", "GET", "maxmemory-policy")
            snapshots = command("CONFIG", "GET", "save")
            appendonly = command("CONFIG", "GET", "appendonly")
            if eviction != ["maxmemory-policy", "noeviction"]:
                raise HarnessError("EmailEngine Redis must use noeviction.")
            if not (len(snapshots) == 2 and snapshots[1]) and appendonly != ["appendonly", "yes"]:
                raise HarnessError("EmailEngine Redis persistence must be enabled.")
            return {"ready": True, "version": info["redis_version"], "persistence": True}
    except HarnessError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise HarnessError("Redis compatibility check failed; verify service, credentials and read-only CONFIG access.") from exc


class EmailEngineRuntime:
    SCHEMA = 1

    def __init__(self, root, config=None):
        self.root = Path(root).resolve() / "email-engine-runtime"
        self.config = dict(config or {})
        self._process = None
        self._tree = None
        self._lease = None
        self._lock = threading.RLock()
        self._stopped = threading.Event()
        self._supervisor = None
        self._url = ""
        self._message = ""
        self._restarts = 0
        public_contract = {key: self.config.get(key) for key in ("mode", "url", "command", "port")}
        redis = urlsplit(str(self.config.get("redis_url", "")))
        public_contract["redis"] = [redis.scheme, redis.hostname, redis.port, redis.path]
        public_contract.update(schema=self.SCHEMA, root=str(self.root))
        self.fingerprint = hashlib.sha256(json.dumps(public_contract, sort_keys=True).encode()).hexdigest()

    @property
    def mode(self):
        return self.config.get("mode", "external")

    def _health(self):
        if not self._url:
            return False
        try:
            # Health carries no API token; readiness does not imply mailbox auth.
            opener = build_opener(ProxyHandler({}), _NoRedirect())
            with opener.open(Request(self._url + "/health"), timeout=2) as response:
                body = response.read(65537)
                return len(body) <= 65536 and json.loads(body).get("success") is True
        except (OSError, ValueError, AttributeError):
            return False

    def status(self):
        with self._lock:
            if self.mode == "external" and self.config.get("url"):
                try:
                    self._url = _service_url(str(self.config["url"]))
                except HarnessError as exc:
                    self._message = str(exc)
            ready = self._health() and (self.mode == "external" or
                    (self._process is not None and self._process.poll() is None))
            configured = bool(self.config.get("url") if self.mode == "external" else self.config.get("command"))
            return {"configured": configured, "ready": ready, "mode": self.mode,
                    "state": "ready" if ready else "unavailable" if configured else "not_configured",
                    "url": self._url, "fingerprint": self.fingerprint,
                    "message": self._message or ("EmailEngine reachable; mailbox authorization must be checked separately."
                        if ready else "Configure an EmailEngine service or supply a local executable and durable Redis."),
                    "api_auth_checked": False, "entitlement_checked": False,
                    "managed": self._process is not None, "restarts": self._restarts}

    def _acquire(self):
        self.root.mkdir(parents=True, exist_ok=True)
        lease = (self.root / "owner.lock").open("a+b")
        try:
            lease.seek(0)
            if os.name == "nt":
                import msvcrt
                if lease.read(1) == b"":
                    lease.write(b"0")
                    lease.flush()
                lease.seek(0)
                msvcrt.locking(lease.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            lease.close()
            raise HarnessError("This project's EmailEngine runtime already has an owner.") from exc
        self._lease = lease

    def _launch(self):
        command = self.config.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
            raise HarnessError("Local EmailEngine requires an explicit executable command list.")
        if not Path(command[0]).is_file():
            raise HarnessError("Configured EmailEngine executable is missing.")
        if len(str(self.config.get("secret", ""))) < 32:
            raise HarnessError("Local EmailEngine requires a persistent secret of at least 32 characters.")
        redis_preflight(str(self.config.get("redis_url", "")))
        port = int(self.config.get("port") or 0)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", port))
            port = reservation.getsockname()[1]
        self._url = f"http://127.0.0.1:{port}"
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("EENGINE_") and key not in {"REDIS_URL", "PORT", "NODE_OPTIONS", "NODE_CONFIG_PATH"}}
        environment.update(EENGINE_HOST="127.0.0.1", EENGINE_PORT=str(port),
                           EENGINE_REDIS=str(self.config["redis_url"]),
                           EENGINE_REDIS_PREFIX="{nexus-" + self.fingerprint[:24] + "}",
                           EENGINE_SECRET=str(self.config["secret"]), EENGINE_WORKERS="1",
                           EENGINE_LOG_LEVEL="error", EENGINE_LOG_RAW="false",
                           EENGINE_UPDATE_CHECK_DISABLED="true")
        if self._tree is not None:
            self._tree.close()
            self._tree = None
        options = dict(cwd=self.root, env=environment, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.name == "nt":
            # Reuse the engine's suspended launch + kill-on-close job. Parent
            # crashes must not leave a second queue owner alive after restart.
            from .execution import _start_windows_contained_process
            flags = subprocess.CREATE_NO_WINDOW | 0x00000004  # CREATE_SUSPENDED
            self._process, self._tree = _start_windows_contained_process(
                lambda: subprocess.Popen(command, **options, creationflags=flags | 0x01000000),
                lambda: subprocess.Popen(command, **options, creationflags=flags),
                label="EmailEngine")
        else:
            self._process = subprocess.Popen(command, **options)
        # Non-secret receipt, never a PID-based authority to terminate a process.
        receipt = {"schema": self.SCHEMA, "fingerprint": self.fingerprint, "url": self._url}
        (self.root / "runtime.json").write_text(json.dumps(receipt), encoding="utf-8")

    def start(self, timeout=20):
        with self._lock:
            if self.mode == "external":
                self._url = _service_url(str(self.config.get("url", "")))
                return self.status()
            if self.mode != "local":
                raise HarnessError("Unknown EmailEngine runtime mode.")
            if self._process is not None:
                return self.status()
            self._acquire()
            try:
                self._launch()
                deadline = time.monotonic() + max(0.1, min(float(timeout), 120))
                while not self._health():
                    if self._process.poll() is not None:
                        raise HarnessError("EmailEngine exited before becoming healthy; check Redis and runtime entitlement.")
                    if time.monotonic() >= deadline:
                        raise HarnessError("EmailEngine did not become healthy before startup timeout.")
                    time.sleep(0.1)
                self._message = ""
                self._stopped.clear()
                self._supervisor = threading.Thread(target=self._watch, daemon=True, name="emailengine-supervisor")
                self._supervisor.start()
                return self.status()
            except Exception:
                self._stop_owned()
                raise

    def _watch(self):
        while not self._stopped.wait(2):
            with self._lock:
                if self._process is None or self._process.poll() is None:
                    continue
                if self._restarts >= 3:
                    self._message = "EmailEngine repeatedly exited. Restart it after correcting the service configuration."
                    return
                self._restarts += 1
                try:
                    self._launch()
                except Exception:
                    self._message = "EmailEngine restart failed; verify its executable and durable Redis service."

    def ensure_running(self):
        return self.start()

    def _stop_owned(self):
        if self._tree is not None:
            self._tree.close()
            self._tree = None
        if self._process is not None:
            if self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=5)
            self._process = None
        if self._lease is not None:
            self._lease.close()
            self._lease = None
        if self.mode == "local":
            self._url = ""

    def close(self):
        self._stopped.set()
        if self._supervisor and self._supervisor is not threading.current_thread():
            self._supervisor.join(timeout=5)
        with self._lock:
            self._stop_owned()
