from __future__ import annotations

import json
import os
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any

from . import qa as qalab
from .config import LoadedConfig
from .detect import combined_commands, detect_project
from .doctor import run_doctor
from .graphs import migrate_graph, resolve_graph_execution_policy, resolve_workflow_policy, simulate_graph, validate_graph
from .memory import MemoryStore
from .models import HarnessError
from .plugins import check_kinds, load_plugins
from .provider_help import setup_advice
from .providers import ProviderRegistry
from . import workflows as workflow_store
from .workflow import HarnessApplication


def loopback_url(host: str, port: int) -> str:
    authority = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"http://{authority}:{port}"


class EventBus:
    def __init__(self, max_events: int = 5000, max_bytes: int = 4_000_000) -> None:
        if max_events <= 0 or max_bytes <= 0:
            raise ValueError("EventBus limits must be positive")
        self.lock = threading.Lock()
        self.events: list[dict[str, Any]] = []
        self._sizes: list[int] = []
        self._bytes = 0
        self._sequence = 0
        self.max_events = max_events
        self.max_bytes = max_bytes

    @staticmethod
    def _serialized_size(event: dict[str, Any]) -> int:
        return len(json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))

    def add(self, event: dict[str, Any]) -> None:
        with self.lock:
            self._sequence += 1
            stored = {**event, "sequence": self._sequence, "time": time.time()}
            size = self._serialized_size(stored)
            if size > self.max_bytes:
                stored = {
                    "sequence": self._sequence,
                    "time": stored["time"],
                    "kind": "event_omitted",
                    "node": event.get("node", ""),
                    "payload": {"error": "Event exceeded the UI buffer byte limit", "original_bytes": size},
                }
                size = self._serialized_size(stored)
            self.events.append(stored)
            self._sizes.append(size)
            self._bytes += size
            while len(self.events) > self.max_events or self._bytes > self.max_bytes:
                self.events.pop(0)
                self._bytes -= self._sizes.pop(0)

    def after(self, sequence: int) -> list[dict[str, Any]]:
        with self.lock:
            return [event for event in self.events if event["sequence"] > sequence]

    def snapshot_after(self, sequence: int) -> dict[str, Any]:
        with self.lock:
            oldest = self.events[0]["sequence"] if self.events else self._sequence + 1
            events = [event for event in self.events if event["sequence"] > sequence]
            return {
                "events": events,
                "oldest_sequence": oldest,
                "last_sequence": self._sequence,
                "gap": max(0, oldest - max(0, sequence) - 1),
            }


class HarnessHTTPServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], config: LoadedConfig):
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, HarnessHandler)
        self.config = config
        self.token = secrets.token_urlsafe(32)
        self.events = EventBus()
        self.run_lock = threading.Lock()
        self.qa_lock = threading.Lock()
        self.qa_result: dict[str, Any] | None = None
        registry = load_plugins(config)
        self.workflow_policy = resolve_workflow_policy(config, registry.workflow_nodes)
        self.check_kinds = dict(registry.check_kinds)
        self.template = migrate_graph(json.loads(files("our_harness.templates").joinpath("gauntlet.json").read_text(encoding="utf-8")))

    def reserve_run(self) -> bool:
        return self.run_lock.acquire(blocking=False)

    def release_run(self) -> None:
        self.run_lock.release()

    def reserve_qa(self) -> bool:
        return self.qa_lock.acquire(blocking=False)

    def release_qa(self) -> None:
        self.qa_lock.release()


class HarnessHandler(BaseHTTPRequestHandler):
    server: HarnessHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        if args and isinstance(args[0], str) and args[0].startswith("GET /api/events"):
            return
        print(f"ui {self.address_string()} {format % args}")

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        try:
            self._validate_authority()
            self._validate_request_site()
        except HarnessError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return False
        return True

    def _json(self, value: Any, status: int = 200) -> None:
        raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(raw)

    def _static(self, name: str, content_type: str) -> None:
        resource = files("our_harness.ui").joinpath(name)
        try:
            raw = resource.read_bytes()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise HarnessError("Invalid Content-Length") from exc
        if length <= 0 or length > 2_000_000:
            raise HarnessError("Request body must contain 1 to 2000000 bytes")
        try:
            value = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise HarnessError("Request body is not valid JSON") from exc
        if not isinstance(value, dict):
            raise HarnessError("Request JSON root must be an object")
        return value

    @staticmethod
    def _bounded_query_int(query: dict[str, list[str]], name: str, default: int, maximum: int) -> int:
        try:
            value = int(query.get(name, [str(default)])[0])
        except (TypeError, ValueError):
            value = default
        return max(0, min(value, maximum))

    def _catalog(self) -> dict[str, Any]:
        config = self.server.config
        registry = ProviderRegistry(config)
        providers = []
        for profile in registry.profiles():
            capabilities = asdict(registry.capabilities(profile.id))
            models = registry.model_catalog(profile.id)
            project_context_allowed = profile.max_data_class in {"project_private", "restricted"}
            providers.append({
                "route_id": profile.id,
                "kind": profile.name,
                "label": profile.id,
                "credential": {
                    "environment_variable": profile.api_key_env,
                    "present": bool(profile.api_key_env and os.environ.get(profile.api_key_env)),
                },
                "models": [item.model for item in models][:100],
                "model_catalog": [asdict(item) for item in models[:100]],
                "default_model": profile.model,
                "capabilities": capabilities,
                "supports_tools": capabilities["native_tools"],
                "supports_streaming": capabilities["streaming"],
                "graph_routing_allowed": profile.allow_project_graphs and project_context_allowed,
                "configured_graph_routing_allowed": profile.allow_project_graphs,
                "routing_block_reason": (
                    "Route data limit does not permit project context"
                    if profile.allow_project_graphs and not project_context_allowed
                    else "Submitted graphs are not enabled for this route"
                    if not profile.allow_project_graphs else ""
                ),
                "max_concurrency": profile.max_concurrency,
                "max_data_class": profile.max_data_class,
            })
        profile_by_id = {item.id: item for item in registry.profiles()}
        return {
            "schema_version": 1,
            "providers": providers,
            "agents": [
                {
                    "agent_id": agent.id,
                    "role": agent.role,
                    "provider_route": agent.provider_ref,
                    "model": agent.model or profile_by_id[agent.provider_ref].model,
                    "capabilities": sorted(agent.capabilities),
                    "max_data_class": profile_by_id[agent.provider_ref].max_data_class,
                    "graph_routing_allowed": bool(
                        profile_by_id[agent.provider_ref].allow_project_graphs
                        and profile_by_id[agent.provider_ref].max_data_class in {"project_private", "restricted"}
                    ),
                }
                for agent in registry.agents()
            ],
            "capabilities": [
                {"id": "workspace.read", "label": "File read", "allowed": True},
                {"id": "workspace.write", "label": "File write", "allowed": int(config.get("execution.max_changed_files")) > 0},
            ],
        }

    def _qa_suite(self) -> dict[str, Any]:
        try:
            suite = qalab.load_suite(self.server.config, None, self.server.check_kinds)
        except HarnessError as exc:
            return {"present": False, "reason": str(exc), "cases": [], "tags": []}
        return {
            "present": True,
            "reason": "",
            "name": suite.name,
            "tags": list(suite.tags()),
            "cases": [case.to_dict() for case in suite.cases],
        }

    def _checkup(self) -> dict[str, Any]:
        """One plain-language answer to 'is this project ready to use?'."""

        config = self.server.config
        doctor = run_doctor(config)
        detections = [item.to_dict() for item in detect_project(config.project_root)]
        suite = self._qa_suite()
        commands = {
            kind: list(config.get(f"project.{kind}_commands") or [])
            or combined_commands(detect_project(config.project_root), kind)
            for kind in ("test", "lint", "build")
        }
        levels = {str(check.get("name")): str(check.get("level")) for check in doctor["checks"]}
        steps = [
            {
                "id": "provider",
                "title": "Connect a model",
                "done": levels.get("provider") == "ok",
                "detail": "The harness needs one model service it can reach.",
                "action": "harness doctor",
            },
            {
                "id": "stack",
                "title": "Know your project",
                "done": any(item.get("stack") != "unknown" for item in detections),
                "detail": "The harness reads your project files to work out its language and tools.",
                "action": "harness init",
            },
            {
                "id": "commands",
                "title": "Find your test command",
                "done": bool(commands["test"]),
                "detail": "Checks run after every change, so the harness needs a test command.",
                "action": "harness init",
            },
            {
                "id": "suite",
                "title": "Write your checks",
                "done": bool(suite.get("present")),
                "detail": "A check suite says, in plain words, what a working project looks like.",
                "action": "harness qa init",
            },
        ]
        return {
            "project": config.project_root.name,
            "ready": all(step["done"] for step in steps),
            "steps": steps,
            "doctor": doctor,
            "detections": detections,
            "commands": commands,
        }

    def _executable_graph(self, value: object) -> tuple[dict[str, Any], list[dict[str, str]]]:
        source = value if isinstance(value, dict) else {}
        graph = migrate_graph(source)
        issues = [issue.__dict__ for issue in validate_graph(graph)]
        if not issues:
            try:
                resolve_graph_execution_policy(self.server.config, source, self.server.workflow_policy)
            except HarnessError as exc:
                issues.append({"path": "execution", "message": str(exc)})
        return graph, issues

    def _single_header(self, name: str, *, required: bool = False) -> str:
        values = self.headers.get_all(name, [])
        if len(values) > 1 or (required and len(values) != 1):
            qualifier = "exactly one" if required else "at most one"
            raise HarnessError(f"Request must contain {qualifier} {name} header")
        return values[0] if values else ""

    def _validated_host_name(self) -> str:
        authority = self._single_header("Host", required=True)
        if not authority or any(character.isspace() or ord(character) < 32 for character in authority):
            raise HarnessError("Malformed Host authority")
        try:
            parsed = urllib.parse.urlsplit(f"//{authority}")
            port = parsed.port
        except ValueError as exc:
            raise HarnessError("Malformed Host authority") from exc
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.hostname is None
            or parsed.hostname.lower() not in {"127.0.0.1", "localhost", "::1"}
            or (port if port is not None else 80) != self.server.server_port
        ):
            raise HarnessError("Host authority must name this loopback UI server")
        return parsed.hostname.lower()

    def _validate_authority(self) -> None:
        self._validated_host_name()

    def _validate_same_origin_url(self, value: str, name: str, *, origin_header: bool) -> None:
        try:
            parsed = urllib.parse.urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise HarnessError(f"Malformed {name} header") from exc
        request_host = self._validated_host_name()
        if (
            parsed.scheme.lower() != "http"
            or parsed.hostname is None
            or parsed.hostname.lower() != request_host
            or (port if port is not None else 80) != self.server.server_port
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or (origin_header and (parsed.path or parsed.query))
        ):
            raise HarnessError("Cross-origin request rejected")

    def _validate_request_site(self) -> None:
        fetch_site = self._single_header("Sec-Fetch-Site")
        if fetch_site and fetch_site not in {"same-origin", "none"}:
            raise HarnessError("Cross-site request rejected")
        origin = self._single_header("Origin")
        if origin:
            self._validate_same_origin_url(origin, "Origin", origin_header=True)
        referer = self._single_header("Referer")
        if referer:
            self._validate_same_origin_url(referer, "Referer", origin_header=False)

    def _require_token(self) -> None:
        if not secrets.compare_digest(self._single_header("X-Harness-Token"), self.server.token):
            raise HarnessError("Missing or invalid session token")

    def _authorize(self) -> None:
        self._validate_authority()
        self._validate_request_site()
        self._require_token()

    def do_GET(self) -> None:
        try:
            self._validate_authority()
            self._validate_request_site()
        except HarnessError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self._static("index.html", "text/html; charset=utf-8")
        elif parsed.path == "/styles.css":
            self._static("styles.css", "text/css; charset=utf-8")
        elif parsed.path == "/app.js":
            self._static("app.js", "text/javascript; charset=utf-8")
        elif parsed.path == "/api/bootstrap":
            self._json({"token": self.server.token, "template": self.server.template, "project": self.server.config.project_root.name})
        elif parsed.path == "/api/events":
            try:
                self._require_token()
            except HarnessError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            query = urllib.parse.parse_qs(parsed.query)
            try:
                after = int(query.get("after", ["0"])[0])
            except ValueError:
                after = 0
            if query.get("meta", [""])[0] == "1":
                self._json(self.server.events.snapshot_after(after))
            else:
                self._json({"events": self.server.events.after(after)})
        elif parsed.path in {"/api/catalog", "/api/memory", "/api/usage", "/api/prompts"}:
            try:
                self._require_token()
            except HarnessError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            query = urllib.parse.parse_qs(parsed.query)
            if parsed.path == "/api/catalog":
                self._json(self._catalog())
                return
            after = self._bounded_query_int(query, "after", 0, 2_147_483_647)
            limit = self._bounded_query_int(query, "limit", 100, 500) or 1
            with MemoryStore(self.server.config) as memory:
                if parsed.path == "/api/memory":
                    self._json(memory.memory_graph(after, limit, query.get("query", [""])[0][:256], query.get("kind", [""])[0][:32]))
                elif parsed.path == "/api/usage":
                    self._json(memory.usage_records(after, limit, query.get("run_id", [""])[0][:256]))
                else:
                    self._json(memory.prompt_lineage(after, limit, query.get("name", [""])[0][:256]))
        elif parsed.path == "/api/workflows":
            try:
                self._require_token()
            except HarnessError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            query = urllib.parse.parse_qs(parsed.query)
            wanted = query.get("name", [""])[0][:64]
            if wanted:
                found = workflow_store.load(self.server.config, wanted)
                self._json({"workflow": found.to_dict(include_graph=True)})
            else:
                self._json({
                    "workflows": [item.to_dict() for item in workflow_store.listed(self.server.config)]
                })
        elif parsed.path == "/api/timeline":
            try:
                self._require_token()
            except HarnessError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            query = urllib.parse.parse_qs(parsed.query)
            limit = self._bounded_query_int(query, "limit", 10, 100) or 1
            with MemoryStore(self.server.config) as memory:
                self._json({"runs": memory.run_timeline(limit)})
        elif parsed.path == "/api/team":
            try:
                self._require_token()
            except HarnessError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            query = urllib.parse.parse_qs(parsed.query)
            run_id = query.get("run_id", [""])[0][:256]
            limit = self._bounded_query_int(query, "limit", 100, 1000) or 1
            with MemoryStore(self.server.config) as memory:
                self._json({"notes": memory.agent_conversation(run_id, limit)})
        elif parsed.path in {"/api/qa/suite", "/api/qa/history", "/api/qa/result", "/api/checkup"}:
            try:
                self._require_token()
            except HarnessError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            query = urllib.parse.parse_qs(parsed.query)
            if parsed.path == "/api/qa/suite":
                self._json(self._qa_suite())
            elif parsed.path == "/api/qa/history":
                try:
                    suite = qalab.load_suite(self.server.config, None, self.server.check_kinds)
                except HarnessError:
                    suite = None
                self._json({
                    "runs": qalab.load_history(self.server.config)[-25:],
                    "unstable": qalab.flaky_report(self.server.config),
                    "advice": qalab.check_health(self.server.config, suite),
                })
            elif parsed.path == "/api/qa/result":
                self._json({"result": self.server.qa_result, "running": self.server.qa_lock.locked()})
            else:
                refresh = query.get("refresh", [""])[0] == "1"
                self._json({
                    **self._checkup(),
                    "model_setup": setup_advice(self.server.config, refresh=refresh),
                })
        elif parsed.path == "/api/health":
            self._json({"status": "ok"})
        elif parsed.path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        try:
            self._authorize()
            body = self._body()
            if self.path == "/api/validate":
                graph, issues = self._executable_graph(body.get("graph", {}))
                self._json({"valid": not issues, "issues": issues, "graph": graph})
            elif self.path == "/api/simulate":
                result = simulate_graph(body.get("graph", {}), body.get("state", {}))
                self._json(result)
            elif self.path == "/api/run":
                task = body.get("task", "")
                if not isinstance(task, str) or not task.strip():
                    raise HarnessError("Task is required")
                graph = body.get("graph")
                if graph is not None:
                    if not isinstance(graph, dict):
                        raise HarnessError("Run graph must be an object")
                    _migrated, issues = self._executable_graph(graph)
                    if issues:
                        raise HarnessError("Run graph is not executable: " + "; ".join(
                            f"{issue['path']}: {issue['message']}" for issue in issues
                        ))
                if not self.server.reserve_run():
                    self._json({"error": "A workspace run is already active"}, HTTPStatus.CONFLICT)
                    return
                thread = threading.Thread(target=self._run_task, args=(task, bool(body.get("dry_run", False)), graph), daemon=True)
                try:
                    thread.start()
                except Exception:
                    self.server.release_run()
                    raise
                self._json({"accepted": True}, HTTPStatus.ACCEPTED)
            elif self.path == "/api/workflows/save":
                saved = workflow_store.save(
                    self.server.config, body.get("name", ""), body.get("graph", {})
                )
                self._json({"saved": saved.to_dict()})
            elif self.path == "/api/workflows/delete":
                removed = workflow_store.delete(self.server.config, body.get("name", ""))
                self._json({"deleted": removed})
            elif self.path == "/api/workflows/rename":
                renamed = workflow_store.rename(
                    self.server.config, body.get("name", ""), body.get("new_name", "")
                )
                self._json({"saved": renamed.to_dict()})
            elif self.path == "/api/qa/init":
                config = self.server.config
                detections = detect_project(config.project_root)
                commands = {
                    kind: list(config.get(f"project.{kind}_commands") or [])
                    or combined_commands(detections, kind)
                    for kind in ("test", "lint", "build")
                }
                if qalab.suite_path(config).exists() and not body.get("replace"):
                    raise HarnessError("A check suite already exists. Tick replace to write a new one.")
                suite = qalab.starter_suite(commands["test"], commands["lint"], commands["build"])
                qalab.write_suite(config, suite)
                self._json({"created": True, "cases": len(suite.cases)})
            elif self.path == "/api/qa/run":
                tags = body.get("tags") or []
                ids = body.get("cases") or []
                if not isinstance(tags, list) or not isinstance(ids, list):
                    raise HarnessError("Tags and cases must be lists")
                suite = qalab.load_suite(self.server.config, None, self.server.check_kinds)
                selection = qalab.QaRunner(self.server.config, extra_kinds=self.server.check_kinds).select(
                    suite, tags=[str(item) for item in tags], ids=[str(item) for item in ids]
                )
                if not self.server.reserve_qa():
                    self._json({"error": "A check run is already active"}, HTTPStatus.CONFLICT)
                    return
                thread = threading.Thread(
                    target=self._run_checks,
                    args=(suite, [str(item) for item in tags], [str(item) for item in ids]),
                    daemon=True,
                )
                try:
                    thread.start()
                except Exception:
                    self.server.release_qa()
                    raise
                self._json({"accepted": True, "cases": len(selection)}, HTTPStatus.ACCEPTED)
            else:
                self.send_error(404)
        except HarnessError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._json({"error": f"Server error: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _run_checks(self, suite: qalab.QaSuite, tags: list[str], ids: list[str]) -> None:
        events = self.server.events
        events.add({"kind": "qa_started", "node": "checks", "payload": {"suite": suite.name}})
        try:
            result = qalab.QaRunner(self.server.config, extra_kinds=self.server.check_kinds).run(suite, tags=tags, ids=ids)
            qalab.record_history(self.server.config, result)
            self.server.qa_result = result.to_dict()
            events.add({"kind": "qa_result", "node": "checks", "payload": self.server.qa_result})
        except Exception as exc:
            self.server.qa_result = None
            events.add({"kind": "qa_error", "node": "checks", "payload": {"error": str(exc)}})
        finally:
            self.server.release_qa()

    def _run_task(self, task: str, dry_run: bool, graph: dict[str, Any] | None = None) -> None:
        try:
            with HarnessApplication(self.server.config, self.server.events.add) as app:
                result = app.run_task(task, dry_run=dry_run, graph=graph)
            self.server.events.add({"kind": "run_result", "node": "complete", "payload": result})
        except Exception as exc:
            self.server.events.add({"kind": "run_error", "node": "failed", "payload": {"error": str(exc)}})
        finally:
            self.server.release_run()


def serve_ui(
    config: LoadedConfig,
    *,
    host: str | None = None,
    port: int | None = None,
    open_browser: bool | None = None,
) -> None:
    host = str(config.get("ui.host") if host is None else host)
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise HarnessError("The UI may bind only to a loopback address")
    port = int(config.get("ui.port") if port is None else port)
    if not 0 <= port <= 65535:
        raise HarnessError("The UI port must be between 0 and 65535")
    server = HarnessHTTPServer((host, port), config)
    url = loopback_url(host, server.server_port) + "/"
    print(f"Harness UI: {url}")
    # A desktop shell reads this exact line to find the port it was given.
    print(f"harness-ui-ready {json.dumps({'url': url, 'port': server.server_port})}", flush=True)
    print("Press Ctrl+C to stop.")
    if config.get("ui.open_browser") if open_browser is None else open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
