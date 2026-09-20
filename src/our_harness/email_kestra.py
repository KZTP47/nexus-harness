"""Private Kestra lifecycle and durable human review orchestration.

H2 is a local desktop convenience, not an upstream-supported production server
deployment. The mail domain database remains authoritative for content and
idempotent finalization. Kestra receives identifiers and loopback capabilities.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from .execution import _ProcessTree
from .filesystem_paths import plain_path


FLOW_ID = "email_review_v1"
NAMESPACE = "nexus.email"
KESTRA_SHA256 = "2f723f733b60eba713e195600decc164e9c1f9609cf77487bc1fd49e9749038a"
FLOW = """id: email_review_v1
namespace: nexus.email
inputs:
  - id: draft_id
    type: STRING
  - id: callback_base
    type: STRING
  - id: callback_token
    type: STRING
tasks:
  - id: generate
    type: io.kestra.plugin.core.http.Request
    uri: "{{ inputs.callback_base }}/api/email-worker/generate"
    method: POST
    headers:
      X-Nexus-Email-Token: "{{ inputs.callback_token }}"
    contentType: application/json
    body: '{{ {"draft_id": inputs.draft_id} | toJson }}'
    options:
      timeout:
        readIdleTimeout: PT15M
  - id: review
    type: io.kestra.plugin.core.flow.Pause
    onResume:
      - id: callback_base
        type: STRING
      - id: callback_token
        type: STRING
  - id: finalize
    type: io.kestra.plugin.core.http.Request
    uri: "{{ outputs.review.onResume.callback_base }}/api/email-worker/finalize"
    method: POST
    headers:
      X-Nexus-Email-Token: "{{ outputs.review.onResume.callback_token }}"
    contentType: application/json
    body: '{{ {"draft_id": inputs.draft_id} | toJson }}'
    options:
      timeout:
        readIdleTimeout: PT15M
"""


def _engine_path(value: str | Path) -> Path:
    """Resolve a path the bundled JVM can read; it rejects extended Win32 paths.

    Engine import roots use the extended spelling so deep installations work,
    and every path derived from them inherits it. Java exits during startup
    when its own home or jar is spelled that way.
    """
    return plain_path(plain_path(Path(value)).resolve())


def _port() -> int:
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        return held.getsockname()[1]


def _multipart(values: dict[str, str]) -> tuple[bytes, str]:
    boundary = "nexus" + secrets.token_hex(16)
    parts = []
    for key, value in values.items():
        if not key.replace("_", "").isalnum():
            raise ValueError("Invalid form field name")
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'
                      + str(value) + "\r\n").encode())
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class KestraRuntime:
    def __init__(self, runtime_dir: str | Path, data_dir: str | Path,
                 callback_base: str, callback_token: str):
        callback = urlsplit(callback_base)
        if callback.scheme != "http" or callback.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Email worker callback must be loopback HTTP")
        if callback.username or callback.password or callback.query or callback.fragment:
            raise ValueError("Invalid email worker callback URL")
        self.runtime_dir = _engine_path(runtime_dir)
        # Never open a database written by a different engine/storage contract.
        # Domain records survive separately and can create replacement executions.
        self.data_dir = _engine_path(data_dir) / "kestra-1.3.38-contract-1"
        self.callback_base = callback_base.rstrip("/")
        self.callback_token = callback_token
        self.process: subprocess.Popen | None = None
        self._tree: _ProcessTree | None = None
        self.base_url = ""
        self._auth = ""
        self._lock = threading.RLock()
        self._log = None
        self._error = ""

    def _request(self, path: str, method: str = "GET", data: bytes | None = None,
                 content_type: str = "application/json"):
        request = Request(self.base_url + path, data=data, method=method, headers={
            "Authorization": self._auth, "Content-Type": content_type,
        })
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except HTTPError as exc:
            # Flow definitions contain callback capabilities: do not echo bodies.
            raise RuntimeError(f"Kestra {method} {path} failed (HTTP {exc.code})") from exc

    def ensure_started(self) -> dict:
        with self._lock:
            if self.process and self.process.poll() is None:
                return self.status()
            if self._log:
                self._log.close()
                self._log = None
            java = self.runtime_dir / "java" / "bin" / ("java.exe" if os.name == "nt" else "java")
            jar = self.runtime_dir / "kestra.jar"
            if not java.is_file() or not jar.is_file():
                raise RuntimeError("Bundled Kestra/Java is missing. Rebuild or reinstall Nexus Harness.")
            manifest_path = self.runtime_dir / "NEXUS_KESTRA.json"
            manifest = json.loads(manifest_path.read_text())
            if manifest.get("schema_version") != 1 or manifest.get("kestra") != "1.3.38":
                raise RuntimeError("Unsupported bundled Kestra runtime contract")
            for relative in ("kestra.jar", str(java.relative_to(self.runtime_dir)).replace("\\", "/")):
                with (self.runtime_dir / relative).open("rb") as stream:
                    actual = hashlib.file_digest(stream, "sha256").hexdigest()
                if actual != manifest.get("files", {}).get(relative):
                    raise RuntimeError("Bundled Kestra runtime integrity check failed")
                if relative == "kestra.jar" and actual != KESTRA_SHA256:
                    raise RuntimeError("Bundled Kestra does not match the pinned release")
            self.data_dir.mkdir(parents=True, exist_ok=True)
            port = _port()
            password = secrets.token_urlsafe(32)
            username = "nexus@localhost.invalid"
            self._auth = "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
            self.base_url = f"http://127.0.0.1:{port}"
            config = {
                "micronaut": {"server": {"host": "127.0.0.1", "port": port}},
                "kestra": {
                    "server": {"basic-auth": {"enabled": True, "username": username, "password": password}},
                    "plugins": {"auto-install": {"enabled": False}},
                    "anonymous-usage-report": {"enabled": False},
                    "ui-anonymous-usage-report": {"enabled": False},
                    "repository": {"type": "h2"}, "queue": {"type": "h2"},
                    "storage": {"type": "local", "local": {"base-path": str(self.data_dir / "storage")}},
                },
                "datasources": {"h2": {"url": "jdbc:h2:file:" + str(self.data_dir / "database").replace("\\", "/")
                    + ";TIME ZONE=UTC;DB_CLOSE_DELAY=-1;DB_CLOSE_ON_EXIT=FALSE;LOCK_TIMEOUT=30000;WRITE_DELAY=0",
                    "username": "sa", "password": "", "driverClassName": "org.h2.Driver"}},
                "endpoints": {"all": {"enabled": False, "port": _port()}},
            }
            config_path = self.data_dir / "application.json"
            # JSON is valid YAML and handles arbitrary user paths without escaping.
            config_path.write_text(json.dumps(config), encoding="utf-8")
            self._log = (self.data_dir / "kestra.log").open("ab")
            environment = {**os.environ, "JAVA_HOME": str(java.parents[1]),
                           "KESTRA_PLUGINS_AUTO_INSTALL_ENABLED": "false"}
            self.process = subprocess.Popen(
                [str(java), "-Xms128m", "-Xmx768m", "-jar", str(jar),
                 "server", "standalone", "--no-tutorials",
                 "--worker-thread", "4", "--config", str(config_path)],
                cwd=self.data_dir, env=environment, stdin=subprocess.DEVNULL,
                stdout=self._log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            try:
                self._tree = _ProcessTree(self.process)
                if not self._tree.windows_contained:
                    raise RuntimeError("Windows could not contain the bundled Kestra process")
            except Exception:
                self.close()
                raise
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    self._error = f"Kestra exited during startup ({self.process.returncode}). See {self.data_dir / 'kestra.log'}"
                    self.close()
                    raise RuntimeError(self._error)
                try:
                    self._request("/api/v1/main/flows/search?size=1")
                    break
                except (RuntimeError, URLError, TimeoutError, ConnectionError):
                    time.sleep(.5)
            else:
                self.close()
                raise RuntimeError("Bundled Kestra did not become ready within 90 seconds")
            try:
                try:
                    self._request("/api/v1/main/flows/nexus.email/email_review_v1", "PUT",
                                  FLOW.encode(), "application/x-yaml")
                except RuntimeError as exc:
                    if "HTTP 404" not in str(exc):
                        raise
                    self._request("/api/v1/main/flows", "POST", FLOW.encode(), "application/x-yaml")
            except Exception:
                self.close()
                raise
            self._error = ""
            return self.status()

    def status(self) -> dict:
        return {"available": (self.runtime_dir / "kestra.jar").is_file(),
                "running": bool(self.process and self.process.poll() is None),
                "version": "1.3.38", "engine": "kestra", "storage": "persistent-local-h2",
                "production_supported": False, "error": self._error}

    def start_draft(self, draft_id: str) -> str:
        self.ensure_started()
        body, content_type = _multipart({"draft_id": draft_id,
            "callback_base": self.callback_base, "callback_token": self.callback_token})
        execution = self._request(f"/api/v1/main/executions/{NAMESPACE}/{FLOW_ID}", "POST", body, content_type)
        return str(execution["id"])

    def execution_status(self, execution_id: str) -> dict:
        self.ensure_started()
        return self._request("/api/v1/main/executions/" + quote(execution_id, safe=""))

    def resume(self, execution_id: str) -> dict:
        self.ensure_started()
        deadline = time.monotonic() + 30
        while True:
            execution = self.execution_status(execution_id)
            state = execution.get("state", {}).get("current")
            if state == "PAUSED":
                break
            if state in {"FAILED", "KILLED", "CANCELLED", "WARNING"}:
                raise RuntimeError(f"Kestra review execution cannot resume from {state}")
            tasks = execution.get("taskRunList", [])
            reviewed = any(task.get("taskId") == "review" and task.get("state", {}).get("current") == "SUCCESS" for task in tasks)
            if reviewed and state in {"RUNNING", "SUCCESS"}:
                return execution
            if time.monotonic() >= deadline:
                raise RuntimeError("Kestra draft has not reached the human review pause yet")
            time.sleep(.2)
        body, content_type = _multipart({"callback_base": self.callback_base,
                                         "callback_token": self.callback_token})
        return self._request("/api/v1/main/executions/" + quote(execution_id, safe="")
                             + "/resume", "POST", body, content_type)

    def close(self) -> None:
        with self._lock:
            process, self.process = self.process, None
            tree, self._tree = self._tree, None
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if tree:
                tree.close()
            if self._log:
                self._log.close()
                self._log = None
