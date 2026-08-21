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

from . import bundle
from . import comparison
from . import coverage
from . import handover
from . import autosetup
from . import explain as explainer
from . import pipeline_starters
from . import pipelines as pipeline_lab
from . import chat as chat_lab
from . import swarm as swarm_lab
from . import projects as projects_lab
from . import tell_somebody as telling_lab
from . import timer as timer_lab
from . import navigate as navigate_lab
from . import plain_graph
from . import qa as qalab
from . import recorder
from . import starters
from . import seats as seat_setup
from . import settings as settings_lab
from . import team as team_lab
from . import vault as vault_lab
from . import selectors
from . import share
from .config import LoadedConfig
from .detect import combined_commands, detect_project
from .doctor import run_doctor
from .graphs import migrate_graph, resolve_graph_execution_policy, resolve_workflow_policy, simulate_graph, validate_graph
from .memory import MemoryStore
from .models import HarnessError
from .plugins import check_kinds, load_plugins
from .redaction import CredentialRedactor
from .provider_help import setup_advice
from .providers import ProviderRegistry
from . import workflows as workflow_store
from .workflow import HarnessApplication


def loopback_url(host: str, port: int) -> str:
    authority = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"http://{authority}:{port}"


class EventBus:
    """What the panel is told, as it happens.

    Everything here goes over the wire and onto a screen the moment it is
    added, and it stays in the page afterwards. That is a live broadcast, not a
    private note, and it is often the first place a check's output exists at
    all: it arrives while the run is going, before anything is written to the
    run folder. So credentials come out of every event as it is put in, at this
    one door, rather than at each of the places that add one.
    """

    def __init__(
        self,
        max_events: int = 5000,
        max_bytes: int = 4_000_000,
        redactor: CredentialRedactor | None = None,
    ) -> None:
        if max_events <= 0 or max_bytes <= 0:
            raise ValueError("EventBus limits must be positive")
        self.lock = threading.Lock()
        self.events: list[dict[str, Any]] = []
        self._sizes: list[int] = []
        self._bytes = 0
        self._sequence = 0
        self.max_events = max_events
        self.max_bytes = max_bytes
        # A bus made without one still hides credentials. Forgetting must not
        # be the thing that turns this off.
        self.redactor = redactor or CredentialRedactor(None)

    @staticmethod
    def _serialized_size(event: dict[str, Any]) -> int:
        return len(json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))

    def add(self, event: dict[str, Any]) -> None:
        cleaned = {
            key: (self.redactor.value(value) if key == "payload" else value)
            for key, value in event.items()
        }
        with self.lock:
            self._sequence += 1
            stored = {**cleaned, "sequence": self._sequence, "time": time.time()}
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



def _a_name_for_a_local_route(server: str, model: str) -> str:
    """A short route name for a local model, from the model's own name.

    Model names carry slashes, colons and version tags - "qwen2.5-coder:7b" and
    worse - and a route name has to be something a person can type and a file
    can hold. The tag goes, the punctuation goes, and what is left is the part
    anybody would have called it anyway.
    """

    plain = model.split("/")[-1].split(":")[0]
    plain = "".join(one if one.isalnum() else "-" for one in plain).strip("-")
    while "--" in plain:
        plain = plain.replace("--", "-")
    return (plain or server)[:48].lower()

class HarnessHTTPServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], config: LoadedConfig):
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, HarnessHandler)
        self.config = config
        self.token = secrets.token_urlsafe(32)
        # A fresh mark for every start. A page left open from a previous run can
        # see that this is a different one and start listening again from the
        # beginning, instead of waiting forever for numbers that will never come.
        self.started_id = secrets.token_urlsafe(8)
        self.events = EventBus(redactor=CredentialRedactor(config))
        self.run_lock = threading.Lock()
        self.qa_lock = threading.Lock()
        # Changing the suite is read, change, write. Two of those at once
        # would each write what the other did not know about, and one
        # change would quietly disappear while both said they worked.
        self.suite_lock = threading.Lock()
        # The notes have a lock of their own. Changing the suite has nothing to
        # do with them, and reading them has to wait for a write to finish: a
        # change of title writes the new file and then takes the old one away,
        # so a read caught in the middle of that would see the same note twice.
        self.vault_lock = threading.Lock()
        self.qa_result: dict[str, Any] | None = None
        # Seat setup has a lock of its own. It asks each assistant its version,
        # and a tool waiting on a sign-in can sit there for the best part of a
        # minute. Sharing the suite lock would stop every other change to the
        # checks for that whole time, for a job that has nothing to do with them.
        self.seats_lock = threading.Lock()
        # Pipelines have their own lock, and reads take it as well as writes.
        # Saving one writes the file and then writes its old versions, and a
        # panel refreshing in the middle of that read a pipeline whose history
        # did not match it - or, on Windows, a file being moved into place.
        self.pipelines_lock = threading.Lock()
        # The board of agents has its own lock. Saving it is read, change,
        # write, and two windows open on the same board would otherwise each
        # write a whole board built from what it read before the other wrote.
        self.swarm_lock = threading.Lock()
        # The Microsoft sign-in somebody is part way through, if anybody is.
        # One at a time, because there is one machine and one browser code.
        self.microsoft_lock = threading.Lock()
        self.microsoft_sign_in: dict[str, str] = {}
        # Setting the board going is one run at a time, in the background,
        # because every turn is a real assistant being asked a real question and
        # a page that says nothing for two minutes is a page nobody trusts.
        self.swarm_runner = swarm_lab.Running()
        # "I don't care, just do it for me" runs one job at a time, in the
        # background, because fetching a model can take a long while and the
        # page has to keep saying what is happening.
        self.setup_runner = autosetup.Runner()
        # One pipeline run at a time. A pipeline starts real suites and real
        # commands, and two at once would fight over the same project.
        self.pipeline_lock = threading.Lock()
        self.pipeline_run: dict[str, Any] | None = None
        self.pipeline_running = False
        self.pipeline_stop = False
        # What somebody has said about a step that stopped to ask. One answer
        # per step: True to carry on, False to stop there.
        self.pipeline_answers: dict[str, bool] = {}
        # The step a run is waiting at, so the page can show the question and
        # the two buttons without asking every second what is going on.
        self.pipeline_waiting_at = ""
        # What the settings file held before the last seat setup, kept so
        # that setup can be undone. Only ever written back by this process.
        self.seats_before: seat_setup.Before | None = None
        self.seats_were_set_up = False
        registry = load_plugins(config)
        self.workflow_policy = resolve_workflow_policy(config, registry.workflow_nodes)
        self.check_kinds = dict(registry.check_kinds)
        self.template = migrate_graph(json.loads(files("our_harness.templates").joinpath("gauntlet.json").read_text(encoding="utf-8")))

    def move_to(self, where: str) -> dict[str, Any]:
        """Show a different project, without stopping and starting again.

        Everything the panel reads comes from `self.config` at the moment it is
        asked, so most of this is one assignment. The rest is what was worked
        out from the old project when this started: which plugins it has, what
        its workflow is allowed to do, and which secrets to keep out of the
        news. All three belong to a project and all three have to move with it.

        A run in flight stops this. Halfway through, the run would be reading
        one project's settings and writing into another's.
        """

        from .config import load_config

        wanted = Path(where).expanduser()
        try:
            wanted = wanted.resolve()
        except OSError as exc:
            raise HarnessError(f"That path cannot be read: {exc}") from exc
        if not wanted.is_dir():
            raise HarnessError(
                f"There is no folder at {wanted}. Pick the folder your project is in."
            )
        if self.pipeline_running:
            raise HarnessError(
                "An automation is running. Wait for it to finish, or stop it, "
                "before moving to another project."
            )
        if not self.run_lock.acquire(blocking=False):
            raise HarnessError(
                "A run is going. Wait for it to finish before moving to "
                "another project."
            )
        try:
            if not self.qa_lock.acquire(blocking=False):
                raise HarnessError(
                    "The checks are running. Wait for them to finish before "
                    "moving to another project."
                )
            try:
                config = load_config(wanted)
                registry = load_plugins(config)
                self.config = config
                self.workflow_policy = resolve_workflow_policy(
                    config, registry.workflow_nodes
                )
                self.check_kinds = dict(registry.check_kinds)
                # What to keep out of the news is worked out from the project's
                # own settings, so it moves with the project too.
                self.events.redactor = CredentialRedactor(config)
                self.qa_result = None
                self.pipeline_run = None
                self.seats_before = None
                self.seats_were_set_up = False
            finally:
                self.qa_lock.release()
        finally:
            self.run_lock.release()
        projects_lab.opened(wanted)
        return projects_lab.where_we_are(self.config)

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
    # A caller that says it is sending a megabyte and then sends nothing used
    # to hold a thread here for as long as it liked. One thread per connection,
    # and no way to get them back, is how a panel stops answering the person
    # who actually opened it.
    timeout = 30

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
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'")
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
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(raw)

    def _picture(self, wanted: str) -> None:
        """Hand back one picture a check kept, and nothing else.

        Only a .png inside this project's own run folder can be reached. The
        name is checked against that folder rather than trusted, so a made-up
        path cannot read anything else on the machine.
        """

        from .safety import confined_path

        base = str(self.server.config.get("qa.artifacts_dir", ".harness/qa/runs")).strip("/")
        if not wanted.lower().endswith(".png") or "\\" in wanted:
            self._json({"error": "Only a picture from a run can be shown"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            path = confined_path(
                self.server.config.project_root, f"{base}/{wanted}",
                allow_missing=True, allow_control=True,
            )
        except HarnessError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if not path.is_file():
            self._json({"error": "There is no picture there"}, HTTPStatus.NOT_FOUND)
            return
        raw = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            # The rest of what was sent is still on the wire and cannot be
            # trusted to be the right length, so this connection ends here.
            self.close_connection = True
            raise HarnessError("Invalid Content-Length") from exc
        if length <= 0 or length > 2_000_000:
            self.close_connection = True
            raise HarnessError("Request body must contain 1 to 2000000 bytes")
        try:
            value = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise HarnessError("Request body is not valid JSON") from exc
        except RecursionError as exc:
            # JSON nested thousands of levels deep. This is a bad request, not
            # a broken server.
            raise HarnessError("Request body is nested far too deeply to read") from exc
        if not isinstance(value, dict):
            raise HarnessError("Request JSON root must be an object")
        return value

    @staticmethod
    def _whole_number(value: object, name: str, smallest: int, largest: int) -> int:
        """A number from a request, or a sentence saying what was wrong with it."""

        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise HarnessError(f"{name} must be a whole number")
        try:
            found = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            # A number too large to write down, such as JSON's 1e999, is not a
            # whole number either, and saying so is the same plain answer as
            # for any other value this cannot use.
            raise HarnessError(f"{name} must be a whole number") from exc
        if not smallest <= found <= largest:
            raise HarnessError(f"{name} must be from {smallest} to {largest}")
        return found

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
            # The same lock every change to the suite takes. Reading while one
            # is being written is how the panel came to say "no checks yet"
            # about a project full of them.
            with self.server.suite_lock:
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

    def _settings_now(self):
        """The settings as they are on disk this second.

        The panel reads the settings once when it starts. Seat setup changes
        that same file while the panel is running, so answering from the copy
        loaded at start would tell somebody a route they have just written is
        not there yet.
        """

        settings, _trouble = seat_setup.settings_to_work_from(self.server.config.project_root)
        return settings

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

    def _came_from_our_own_page(self) -> bool:
        """True when a browser says this request came from the panel itself.

        Browsers always tell a server where a request came from, with
        Sec-Fetch-Site or an Origin or Referer line, and they will not let a
        page lie about it. That is what this stops: a web page open in another
        tab cannot ask for the key, because it cannot claim to be this panel.

        It is worth being plain about what it does not stop. Another program
        already running as you on this machine can send those lines itself,
        because nothing is checking them but this. Loopback HTTP has no way to
        tell one local program from another, so this is a fence against pages,
        not against programs. Anything already running as you can read your
        files anyway.
        """

        fetch_site = self._single_header("Sec-Fetch-Site")
        if fetch_site:
            return fetch_site == "same-origin"
        for name in ("Origin", "Referer"):
            value = self._single_header(name)
            if not value:
                continue
            try:
                self._validate_same_origin_url(value, name, origin_header=name == "Origin")
            except HarnessError:
                return False
            return True
        return False

    def _something_nobody_expected(self, exc: Exception) -> str:
        """What to put on the screen when something went wrong that nothing expected.

        Not the message. Everything the harness raises on purpose is a plain
        sentence that has been through the redactor; anything reaching here has
        been through nothing, and the one that started this was a name and
        password written into an address, coming back inside "nonnumeric port:
        'supersecretpassword@gateway.example'". A redactor takes out the shapes
        it knows, and a password somebody chose is not one of them.

        So the screen gets the kind of failure and where to read the rest. The
        rest goes to the window the panel was started from, which is this
        person's own machine and nobody else's.
        """

        from .redaction import CredentialRedactor

        try:
            detail = CredentialRedactor(self.server.config).text(str(exc))
        except Exception:  # noqa: BLE001 - saying less beats saying a key
            detail = "(it could not be written down safely)"
        self.log_message("unexpected %s: %s", type(exc).__name__, detail)
        return (
            f"Something went wrong that the panel did not expect ({type(exc).__name__}). "
            "The rest is in the window the panel was started from."
        )

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
        # Reading answers the same way as changing does: a plain
        # sentence the panel can show, never a dropped connection.
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path in {"/", "/index.html"}:
                self._static("index.html", "text/html; charset=utf-8")
            elif parsed.path == "/styles.css":
                self._static("styles.css", "text/css; charset=utf-8")
            elif parsed.path == "/app.js":
                self._static("app.js", "text/javascript; charset=utf-8")
            elif parsed.path == "/api/bootstrap":
                # This is the one call that hands out the session key, so it must
                # come from the panel's own page. A browser always says where a
                # request came from; a script run by something else on this machine
                # does not, and is turned away.
                if not self._came_from_our_own_page():
                    self._json(
                        {
                            "error": (
                                "This call must come from the control panel page itself. "
                                "Open the address in a browser instead."
                            )
                        },
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
                self._json({
                    "token": self.server.token,
                    "template": self.server.template,
                    "project": self.server.config.project_root.name,
                    # Changes every time the harness starts, so a page left open
                    # can tell that it is now talking to a different run.
                    "started_id": self.server.started_id,
                })
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
                    # The mark goes with every answer, so a page that was open
                    # across a restart notices at the next question it asks.
                    self._json({**self.server.events.snapshot_after(after), "started_id": self.server.started_id})
                else:
                    self._json({"events": self.server.events.after(after), "started_id": self.server.started_id})
            elif parsed.path == "/api/qa/picture":
                try:
                    self._require_token()
                except HarnessError as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                query = urllib.parse.parse_qs(parsed.query)
                wanted = (query.get("path", [""])[0] or "").strip()
                self._picture(wanted)
            elif parsed.path == "/api/qa/changed":
                try:
                    self._require_token()
                except HarnessError as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                try:
                    before, after = comparison.last_two(self.server.config)
                except HarnessError as exc:
                    self._json({"nothing_to_compare": str(exc)})
                    return
                self._json(
                    comparison.compare(
                        before, after, redactor=CredentialRedactor(self.server.config)
                    ).to_dict()
                )
            elif parsed.path == "/api/vault":
                self._require_token()
                config = self.server.config
                query = urllib.parse.parse_qs(parsed.query)
                # Reading takes the same lock as writing. Everything below is
                # several passes over the same folder, and a write landing
                # between two of them would answer with a vault that never
                # existed - a note in the picture and not in the list, or the
                # same note twice while a change of title is halfway done.
                with self.server.vault_lock:
                    answer = vault_lab.everything(config)
                    # A vault too large to hand over whole is sifted where the
                    # notes are, rather than sending every one to be sifted here.
                    looking = (query.get("q", [""])[0] or "").strip()
                    if looking:
                        matched = {note.name for note in vault_lab.search(config, looking)}
                        answer["notes"] = [
                            note for note in answer["notes"] if note["name"] in matched
                        ]
                        answer["links"] = [
                            link for link in answer["links"]
                            if link["from"] in matched and link["to"] in matched
                        ]
                        answer["searched_for"] = looking
                    wanted = (query.get("name", [""])[0] or "").strip()
                    if wanted:
                        try:
                            answer["open"] = vault_lab.neighbours(config, wanted)
                        except HarnessError:
                            # The note was removed, here or in an editor. Losing
                            # the whole view over one missing note would be a
                            # poor trade.
                            answer["open"] = None
                            answer["gone"] = wanted
                    answer["going_stale"] = [
                        note.to_dict() for note in vault_lab.going_stale(config)
                    ]
                    answer["lately"] = [note.to_dict() for note in vault_lab.lately(config, 14)]
                self._json(answer)
            elif parsed.path == "/api/settings":
                self._require_token()
                # Read fresh: a setting changed a moment ago has to show as
                # changed, and the panel's own copy was loaded at start.
                now = self._settings_now()
                self._json({
                    "settings": [item.to_dict() for item in settings_lab.everything(now)],
                    "groups": settings_lab.groups(),
                    "files": {
                        "shared": settings_lab.SHAREABLE,
                        "yours": settings_lab.YOURS,
                    },
                })
            elif parsed.path == "/api/pipelines":
                self._require_token()
                config = self.server.config
                query = urllib.parse.parse_qs(parsed.query)
                wanted = (query.get("name", [""])[0] or "").strip()
                # Reading takes the lock as well. A read that does not wait for
                # a write is a read that can see half of one.
                with self.server.pipelines_lock:
                    saved_now = pipeline_lab.saved_ones(config)
                    kept_now = pipeline_lab.older_ones(config, wanted) if wanted else []
                    on_screen = (
                        pipeline_lab.load(config, wanted)
                        if wanted
                        else pipeline_lab.a_starting_pipeline()
                    )
                answer: dict[str, Any] = {
                    "saved": saved_now,
                    "kinds": [kind.to_dict() for kind in pipeline_lab.KINDS.values()],
                    "running": self.server.pipeline_running,
                    "waiting_at": self.server.pipeline_waiting_at,
                    "last_run": self.server.pipeline_run,
                }
                answer["starters"] = pipeline_starters.listed()
                # The words for the two choices every step has: when it runs,
                # and how it waits before trying again. Kept beside the kinds so
                # the page and the engine cannot disagree about what exists.
                answer["when_it_runs"] = [
                    {"when": key, "label": label, "means": means}
                    for key, label, means in pipeline_lab.WHEN_IT_RUNS
                ]
                answer["waits"] = [
                    {"wait": key, "label": label, "means": means}
                    for key, label, means in pipeline_lab.WAITS
                ]
                # What this pipeline looked like before the last few saves, so
                # a person can put one back rather than redrawing it.
                answer["older_ones"] = (
                    [
                        {key: value for key, value in one.items() if key != "pipeline"}
                        for one in kept_now
                    ]
                    if wanted else []
                )
                answer["pipeline"] = on_screen
                self._json(answer)
            elif parsed.path == "/api/pipelines/why-not-alone":
                # Why this automation should not be left to run itself, if it
                # should not. Asked before it goes on a timer, not after.
                self._require_token()
                query = urllib.parse.parse_qs(parsed.query)
                self._json({
                    "why_not": timer_lab.what_stops_it_running_alone(
                        self.server.config, query.get("name", [""])[0]
                    )
                })
            elif parsed.path == "/api/projects":
                self._require_token()
                config = self.server.config
                self._json({
                    "here": projects_lab.where_we_are(config),
                    "projects": [
                        one.to_dict()
                        for one in projects_lab.every_one(config.project_root)
                    ],
                    "sidebar": projects_lab.how_it_looks(),
                    "how_it_can_look": list(projects_lab.HOW_IT_CAN_LOOK),
                })
            elif parsed.path == "/api/swarm":
                self._require_token()
                config = self.server.config
                with self.server.swarm_lock:
                    said = swarm_lab.how_it_stands(config)
                    said["what_is_not_ready"] = swarm_lab.what_is_not_ready(config, said)
                said["cannot_be_changed"] = (
                    self.server.swarm_runner.why_it_cannot_be_changed()
                )
                self._json(said)
            elif parsed.path == "/api/swarm/said":
                # One agent's own conversation. Its name decides which file is
                # read, so two agents both using Claude do not read each
                # other's half of it.
                self._require_token()
                query = urllib.parse.parse_qs(parsed.query)
                with self.server.swarm_lock:
                    board = swarm_lab.load()
                one = swarm_lab.the_agent(board, query.get("agent", [""])[0])
                self._json({
                    "agent": one.to_dict(),
                    "said": [
                        held.to_dict() for held in chat_lab.read_it(
                            self.server.config, one.who, swarm_lab.filed_as(one.name)
                        )
                    ],
                    "most_letters": chat_lab.MOST_LETTERS,
                })
            elif parsed.path == "/api/swarm/how-it-is-going":
                self._require_token()
                self._json({"doing": self.server.swarm_runner.how_it_is_going()})
            elif parsed.path == "/api/swarm/what-they-said":
                # Asked for on its own rather than sent with every "how is it
                # going": these are whole answers, and a page watching a run
                # asks how it is going every second and a half.
                self._require_token()
                self._json(self.server.swarm_runner.what_they_said())
            elif parsed.path == "/api/telling":
                self._require_token()
                config = self.server.config
                self._json({
                    "ways": telling_lab.how_it_stands(config),
                    "kinds": [
                        {
                            "kind": one.kind,
                            "label": one.label,
                            "secret_is": one.secret_is,
                            "usually_called": one.usually_called,
                            "where_to_get_one": one.where_to_get_one,
                            "needs_a_server": one.needs_a_server,
                            "server_usually_called": one.server_usually_called,
                            "needs_to": one.needs_to,
                            "needs_sent_from": one.needs_sent_from,
                        }
                        for one in telling_lab.THE_KINDS
                    ],
                })
            elif parsed.path == "/api/timers":
                self._require_token()
                import datetime as when_lab

                config = self.server.config
                now = when_lab.datetime.now()
                with self.server.pipelines_lock:
                    found = timer_lab.every_one(config)
                    saved = pipeline_lab.saved_ones(config)
                self._json({
                    "timers": [
                        dict(
                            one.to_dict(),
                            in_plain_words=timer_lab.in_plain_words(one),
                            next_run=timer_lab.when_it_runs_next(one, now).strftime(
                                "%A %d %B at %H:%M"
                            ),
                        )
                        for one in found
                    ],
                    "automations": saved,
                    "how_often": [
                        {"how_often": key, "label": label, "means": means}
                        for key, label, means in timer_lab.HOW_OFTEN
                    ],
                    "days": list(timer_lab.DAYS),
                    "how_to_ask_this_machine": timer_lab.how_to_ask_this_machine(config),
                    "could_not_be_read": timer_lab.what_could_not_be_read(config),
                })
            elif parsed.path == "/api/chat":
                self._require_token()
                query = urllib.parse.parse_qs(parsed.query)
                wanted = query.get("who", [""])[0]
                everyone = chat_lab.who_can_talk(self.server.config)
                # Which one is open. Whatever was asked for if it can answer,
                # otherwise the first one that can.
                ready = [one for one in everyone if one["ready"]]
                if not any(one["route"] == wanted for one in ready):
                    wanted = ready[0]["route"] if ready else ""
                self._json({
                    "who": everyone,
                    "open": wanted,
                    "said": [
                        one.to_dict()
                        for one in chat_lab.read_it(self.server.config, wanted)
                    ] if ready else [],
                    "most_letters": chat_lab.MOST_LETTERS,
                    "most_kept": chat_lab.MOST_KEPT,
                })
            elif parsed.path == "/api/look-up":
                self._require_token()
                # Only which servers are here. Asking one a question is a POST,
                # because it starts a program.
                self._json({
                    "servers": navigate_lab.what_is_on_this_machine(),
                    "asking": [
                        {"asking": "where-is-it", "label": "Where is it?"},
                        {"asking": "what-uses-it", "label": "What uses it?"},
                        {"asking": "what-is-it", "label": "What is it?"},
                    ],
                })
            elif parsed.path == "/api/who-is-on-it":
                self._require_token()
                query = urllib.parse.parse_qs(parsed.query)
                wanted = (query.get("name", [""])[0] or "").strip()
                # Looking for assistants runs their command line tools, and
                # setting seats up writes the routes those tools need. One at a
                # time, so neither reads the other half done.
                with self.server.seats_lock:
                    answer = team_lab.everything(self._settings_now())
                if wanted:
                    try:
                        answer["open"] = team_lab.load_team(self.server.config, wanted)
                    except HarnessError:
                        # Removed here, or in an editor, since the page last
                        # looked. Losing the whole view over it would be a poor
                        # trade.
                        answer["open"] = None
                        answer["gone"] = wanted
                self._json(answer)
            elif parsed.path == "/api/setup/do-it":
                self._require_token()
                self._json({
                    "job": self.server.setup_runner.latest(),
                    "busy": self.server.setup_runner.busy,
                    "can_do": sorted(autosetup.PLANS),
                })
            elif parsed.path == "/api/seats":
                self._require_token()
                found = seat_setup.look(self._settings_now()).to_dict()
                # Setting seats up changes one file, so only one setup can be
                # outstanding at a time. Saying so lets anything else wait its
                # turn instead of writing over it.
                found["setup_outstanding"] = self.server.seats_were_set_up
                self._json(found)
            elif parsed.path == "/api/qa/starters":
                try:
                    self._require_token()
                except HarnessError as exc:
                    self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                    return
                self._json({"starters": starters.listed(), "screens": dict(starters.SCREENS)})
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
        except HarnessError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except ConnectionError:
            # The page went to another view and closed the connection while
            # this was still answering. There is nobody left to tell, and
            # trying anyway wrote a 500 into a log and into the console of
            # whatever opened the next page.
            self.close_connection = True
        except Exception as exc:
            # Whatever went wrong that nothing expected, said without whatever
            # was in it. Anything that gets this far has not been through the
            # redactor on its own way, and an address with a password in it is
            # exactly the sort of thing that turns up in one of these.
            self._json(
                {"error": self._something_nobody_expected(exc)},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def do_POST(self) -> None:
        try:
            self._authorize()
            body = self._body()
            if self.path == "/api/validate":
                graph, issues = self._executable_graph(body.get("graph", {}))
                self._json({"valid": not issues, "issues": issues, "graph": graph})
            elif self.path == "/api/simulate":
                state = body.get("state", {})
                if not isinstance(state, dict):
                    raise HarnessError("state must be an object of names and values")
                result = simulate_graph(body.get("graph", {}), state)
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
                with self.server.suite_lock:
                    qalab.write_suite(config, suite)
                self._json({"created": True, "cases": len(suite.cases)})
            elif self.path == "/api/bundle":
                parts = body.get("parts") or []
                if not isinstance(parts, list):
                    raise HarnessError("Parts must be a list")
                built = bundle.build(
                    self.server.config,
                    parts=[str(item) for item in parts],
                    runs=self._whole_number(body.get("runs", bundle.DEFAULT_RUNS), "runs", 0, 100),
                )
                self._json({
                    "path": str(built.path),
                    "files": len(built.files),
                    "left_out": list(built.left_out),
                    "parts": list(built.parts),
                })
            elif self.path == "/api/qa/add":
              with self.server.suite_lock:
                  case = starters.build(
                      str(body.get("starter") or ""),
                      url=str(body.get("url") or ""),
                      case_id=str(body.get("name") or ""),
                  )
                  # A suite that is there and cannot be read is not an empty
                  # suite. Treating it as one and then writing would replace
                  # somebody's checks with this single new one.
                  suite = coverage.read_suite(self.server.config, None, self.server.check_kinds)
                  cases = [item.to_dict() for item in suite.cases]
                  name = suite.name
                  if any(item["id"] == case["id"] for item in cases):
                      raise HarnessError(
                          f"This suite already holds a check called {case['id']}. Give the new one another name."
                      )
                  cases.append(case)
                  written = qalab.parse_suite(
                      {"schema_version": 1, "name": name, "cases": cases},
                      extra_kinds=self.server.check_kinds,
                  )
                  qalab.write_suite(self.server.config, written)
                  self._json({"added": case["id"], "cases": len(cases)})
            elif self.path == "/api/qa/explain":
                report = self.server.qa_result or handover.read_run(self.server.config)
                case, evidence = handover.failure_from_run(report, str(body.get("case") or ""))
                if body.get("question_only"):
                    self._json({
                        "case_id": case.get("id"),
                        "question": handover.failure_question(case, evidence),
                    })
                    return
                answer = handover.explain_failure(self.server.config, case, evidence)
                self._json(answer.to_dict())
            elif self.path == "/api/qa/remove":
              with self.server.suite_lock:
                  wanted = str(body.get("case") or "")
                  suite = qalab.load_suite(self.server.config, None, self.server.check_kinds)
                  keeping = [item.to_dict() for item in suite.cases if item.id != wanted]
                  if len(keeping) == len(suite.cases):
                      raise HarnessError(f"There is no check called {wanted}")
                  written = qalab.parse_suite(
                      {"schema_version": 1, "name": suite.name, "cases": keeping},
                      extra_kinds=self.server.check_kinds,
                  )
                  qalab.write_suite(self.server.config, written)
                  self._json({"removed": wanted, "cases": len(keeping)})
            elif self.path == "/api/qa/record":
                url = str(body.get("url") or "")
                selectors.check_url(self.server.config, url)
                if not self.server.reserve_qa():
                    self._json({"error": "A check run is already active"}, HTTPStatus.CONFLICT)
                    return
                thread = threading.Thread(target=self._run_record, args=(url,), daemon=True)
                try:
                    thread.start()
                except Exception:
                    self.server.release_qa()
                    raise
                self._json({"accepted": True}, HTTPStatus.ACCEPTED)
            elif self.path == "/api/seats/setup":
              with self.server.seats_lock:
                  asked = body.get("kinds") or []
                  if not isinstance(asked, list):
                      raise HarnessError("Choose which assistants to set up")
                  if self.server.seats_were_set_up:
                      raise HarnessError(
                          "A setup here has not been put back yet. Press 'Put my settings "
                          "back' first, or set them up again once that is done."
                      )
                  done = seat_setup.set_up(
                      self._settings_now(),
                      [str(item) for item in asked][:8],
                      trust=body.get("trust") is not False,
                  )
                  self.server.seats_before = done.previous
                  self.server.seats_were_set_up = True
                  self._json(done.to_dict())
            elif self.path == "/api/seats/undo":
              with self.server.seats_lock:
                  if not self.server.seats_were_set_up:
                      raise HarnessError(
                          "Nothing was set up here to undo. This only puts back what "
                          "this panel changed while it has been running."
                      )
                  said = seat_setup.put_it_back(self._settings_now(), self.server.seats_before)
                  self.server.seats_were_set_up = False
                  self.server.seats_before = None
                  self._json({"note": said})
            elif self.path == "/api/vault/start":
                # Writing the first two notes is a thing somebody asks for, not
                # something that happens because they opened a tab. Looking at
                # a view must never leave files behind.
                with self.server.vault_lock:
                    made = vault_lab.start_it_off(self.server.config)
                    self._json({
                        "made": [note.name for note in made],
                        "note": (
                            f"{len(made)} note(s) written to start you off."
                            if made
                            else "There are notes here already, so nothing was written."
                        ),
                    })
            elif self.path == "/api/vault/write":
                with self.server.vault_lock:
                    note = vault_lab.Note(
                        name="",
                        title=str(body.get("title") or ""),
                        kind=str(body.get("kind") or "about-this-project"),
                        body=str(body.get("body") or "")[:vault_lab.MOST_LETTERS],
                        tags=[str(tag) for tag in (body.get("tags") or [])][:12],
                        sure=float(body.get("sure") or 0.5),
                        learned=str(body.get("learned") or ""),
                        came_from=str(body.get("came_from") or "you"),
                        uses=int(body.get("uses") or 0),
                        worked=int(body.get("worked") or 0),
                    )
                    # Which note this is, when it is one that already exists.
                    # Without it a change of title leaves the old file behind.
                    was = str(body.get("was") or "")
                    self._json({
                        "note": vault_lab.write_one(
                            self.server.config, note, was=was
                        ).to_dict()
                    })
            elif self.path == "/api/vault/remove":
                with self.server.vault_lock:
                    self._json({
                        "note": vault_lab.remove(self.server.config, str(body.get("name") or ""))
                    })
            elif self.path == "/api/vault/used":
                with self.server.vault_lock:
                    note = vault_lab.used(
                        self.server.config,
                        str(body.get("name") or ""),
                        went_well=bool(body.get("went_well")),
                    )
                    self._json({"note": note.to_dict()})
            elif self.path == "/api/vault/learn":
                with self.server.vault_lock:
                    self._json(vault_lab.learn_from_memory(self.server.config))
            elif self.path == "/api/settings/change":
                with self.server.seats_lock:
                    done = settings_lab.change(
                        self._settings_now(), str(body.get("key") or ""), body.get("value")
                    )
                self._json(done.to_dict())
            elif self.path == "/api/settings/reset":
                with self.server.seats_lock:
                    done = settings_lab.reset(
                        self._settings_now(), str(body.get("key") or "")
                    )
                self._json(done.to_dict())
            elif self.path == "/api/pipelines/starter":
                self._json({"pipeline": pipeline_starters.build(str(body.get("key") or ""))})
            elif self.path == "/api/explain":
                # What a failure means, in words. Nothing is looked up
                # anywhere: it reads what the check already said.
                self._json(explainer.what_it_means(
                    str(body.get("said") or "")[:20000], kind=str(body.get("kind") or "")
                ).to_dict())
            elif self.path == "/api/projects/add":
                one = projects_lab.add(str(body.get("path") or ""))
                self._json({"project": one.to_dict()})
            elif self.path == "/api/projects/rename":
                # A name is about the project, so it is written inside it and
                # travels with it. Somebody else who clones this gets the name.
                name = projects_lab.rename(
                    str(body.get("path") or self.server.config.project_root),
                    str(body.get("name") or ""),
                )
                self._json({"name": name})
            elif self.path == "/api/projects/forget":
                self._json({"note": projects_lab.forget(str(body.get("path") or ""))})
            elif self.path == "/api/projects/open":
                here = self.server.move_to(str(body.get("path") or ""))
                self._json({
                    "here": here,
                    "note": (
                        f"Now showing {here['name']}. The page reloads, because "
                        "everything on it belongs to the project it came from."
                    ),
                })
            elif self.path == "/api/projects/sidebar":
                self._json({
                    "sidebar": projects_lab.make_it_look(str(body.get("how") or ""))
                })
            elif self.path == "/api/swarm/save":
                # The whole board at once. It is one small picture, and saving
                # it whole means the panel can never leave a line pointing at a
                # box that half of a save had already taken away.
                # Both under the one lock. Asked outside it, a run could start
                # in the gap between "is anything going?" and the write, and
                # then be working from a board that had changed underneath it.
                with self.server.swarm_lock:
                    stopping = self.server.swarm_runner.why_it_cannot_be_changed()
                    if stopping:
                        raise swarm_lab.SwarmError(stopping)
                    swarm_lab.save(body.get("board"))
                    said = swarm_lab.how_it_stands(self.server.config)
                    said["what_is_not_ready"] = swarm_lab.what_is_not_ready(
                        self.server.config, said
                    )
                self._json(said)
            elif self.path == "/api/swarm/say":
                # No board lock while it waits for an answer: an assistant can
                # take a minute, and holding the lock that long would freeze
                # every other window looking at the same board. The
                # conversation has its own lock, which is the one that matters.
                with self.server.swarm_lock:
                    board = swarm_lab.load()
                one = swarm_lab.the_agent(board, str(body.get("agent") or ""))
                if not one.who:
                    raise swarm_lab.SwarmError(
                        f"{one.name} has no assistant chosen yet. Open its "
                        "settings and pick which one it uses."
                    )
                self._json(dict(chat_lab.say(
                    self.server.config,
                    one.who,
                    str(body.get("text") or ""),
                    filed_as=swarm_lab.filed_as(one.name),
                ), agent=one.to_dict()))
            elif self.path == "/api/swarm/keep":
                # Saving the board as it stands, under a name. One board came
                # back on its own and only ever one, so a second arrangement
                # meant taking the first apart and building it again from
                # memory on Monday.
                with self.server.swarm_lock:
                    said = swarm_lab.keep_this_board(str(body.get("name") or ""))
                    said["kept"] = swarm_lab.every_kept_board()
                self._json(said)
            elif self.path == "/api/swarm/open-kept":
                with self.server.swarm_lock:
                    swarm_lab.open_this_board(str(body.get("name") or ""))
                    said = swarm_lab.how_it_stands(self.server.config)
                    said["what_is_not_ready"] = swarm_lab.what_is_not_ready(
                        self.server.config, said)
                    said["kept"] = swarm_lab.every_kept_board()
                self._json(said)
            elif self.path == "/api/swarm/forget-kept":
                with self.server.swarm_lock:
                    swarm_lab.forget_this_board(str(body.get("name") or ""))
                    self._json({"kept": swarm_lab.every_kept_board()})
            elif self.path == "/api/local-models/use":
                # One press to use a model already running here. No key, no
                # seat, nobody to ask - which is the whole point of running one
                # on your own machine, and it should not cost a trip into a
                # settings file to find that out.
                from . import local_models as local_lab

                wanted = str(body.get("server") or "").strip()
                model = str(body.get("model") or "").strip()
                found = [one for one in local_lab.look() if one.id == wanted]
                if not found:
                    raise HarnessError(
                        f"There is nothing called {wanted or 'that'} running on this machine.")
                try:
                    route = local_lab.a_route_for(found[0], model)
                except ValueError as exc:
                    raise HarnessError(str(exc)) from exc
                name = _a_name_for_a_local_route(found[0].id, model)
                seat_setup.write_one_route(self._settings_now(), name, route)
                self._json({"route": name, "using": route})
            elif self.path == "/api/microsoft/sign-in":
                # Microsoft 365 Copilot has no command line, so signing in
                # cannot be handed off to one. It is a code somebody pastes
                # into a browser: this asks for the code, and the next one asks
                # whether it has been pasted yet. Nothing secret goes through
                # the panel at any point.
                from .providers import m365_copilot as microsoft

                held = microsoft.start_signing_in(
                    str(body.get("app") or "").strip(),
                    str(body.get("organisation") or "").strip(),
                )
                # Each attempt carries a number of its own. Two windows open on
                # the same panel both pressed this, and the second quietly took
                # the first one's place - leaving the first showing a code that
                # would never be noticed however carefully somebody typed it.
                with self.server.microsoft_lock:
                    self.server.microsoft_sign_in = {
                        "attempt": secrets.token_urlsafe(12),
                        # Microsoft's handle for this attempt. Worth nothing to
                        # anybody without the code, and still not something to
                        # put on a screen.
                        "waiting_on": held.pop("waiting_on"),
                        "app": str(body.get("app") or "").strip(),
                        "organisation": str(body.get("organisation") or "").strip(),
                    }
                    held["attempt"] = self.server.microsoft_sign_in["attempt"]
                self._json(held)
            elif self.path == "/api/microsoft/sign-in/how-it-is-going":
                from .providers import m365_copilot as microsoft

                with self.server.microsoft_lock:
                    held = dict(getattr(self.server, "microsoft_sign_in", None) or {})
                if not held.get("waiting_on"):
                    raise HarnessError(
                        "Nothing is waiting on a code. Press Sign in to Microsoft first.")
                asked_about = str(body.get("attempt") or "")
                if asked_about and asked_about != held["attempt"]:
                    raise HarnessError(
                        "That sign-in was replaced by a newer one, probably in another "
                        "window. Press Sign in to Microsoft again to get a fresh code.")
                said = microsoft.how_the_sign_in_is_going(
                    held.get("app", ""), held["waiting_on"], held.get("organisation", ""))
                if said["done"] or not said["waiting"]:
                    # Over either way, so the handle goes - but only if it is
                    # still this attempt's. A newer one may have started while
                    # Microsoft was being asked, and throwing that away would
                    # break the window that is now the live one.
                    with self.server.microsoft_lock:
                        now = getattr(self.server, "microsoft_sign_in", None) or {}
                        if now.get("attempt") == held["attempt"]:
                            self.server.microsoft_sign_in = {}
                self._json(said)
            elif self.path == "/api/microsoft/sign-out":
                from .providers import m365_copilot as microsoft

                microsoft.forget_the_sign_in()
                with self.server.microsoft_lock:
                    self.server.microsoft_sign_in = {}
                self._json({"signed_out": True})
            elif self.path == "/api/swarm/start-again":
                with self.server.swarm_lock:
                    board = swarm_lab.load()
                one = swarm_lab.the_agent(board, str(body.get("agent") or ""))
                self._json({
                    "note": chat_lab.start_again(
                        self.server.config, one.who, swarm_lab.filed_as(one.name)
                    ),
                    "said": [],
                })
            elif self.path == "/api/swarm/start":
                # The lock is held only while the board is read and the run is
                # marked as going, which is all start does - the asking happens
                # on a thread of its own afterwards. Held for that much, no save
                # can slip in between the board being read and the run owning
                # it; held for the whole run, every window looking at the board
                # would freeze for as long as the assistants took.
                with self.server.swarm_lock:
                    doing = self.server.swarm_runner.start(self.server.config)
                self._json({"doing": doing})
            elif self.path == "/api/swarm/stop":
                self._json({
                    "note": self.server.swarm_runner.stop(),
                    "doing": self.server.swarm_runner.how_it_is_going(),
                })
            elif self.path == "/api/telling/save":
                # The same lock its neighbour takes. Looking to see whether a
                # name is taken and then writing it is two steps, and two people
                # pressing the button at the same instant both got past the
                # first one.
                with self.server.pipelines_lock:
                    saved = telling_lab.save(self.server.config, body.get("way"))
                self._json({
                    "way": saved.to_dict(),
                    "why_not": telling_lab.why_it_cannot_be_used(saved),
                })
            elif self.path == "/api/telling/remove":
                with self.server.pipelines_lock:
                    note = telling_lab.remove(
                        self.server.config, str(body.get("name") or "")
                    )
                self._json({"note": note})
            elif self.path == "/api/telling/try":
                name = str(body.get("name") or "")
                found = [
                    one for one in telling_lab.every_one(self.server.config)
                    if one.name == name
                ]
                if not found:
                    raise HarnessError(f"There is nothing set up called {name}.")
                said = telling_lab.tell_them(
                    self.server.config, found[0],
                    "A message from the harness",
                    "This is what a message from your harness looks like. "
                    "Nothing has gone wrong; somebody pressed a button.",
                )
                self._json(said.to_dict())
            elif self.path == "/api/timers/save":
                with self.server.pipelines_lock:
                    # Saving does the refusing. Held in the panel's own code
                    # alone, anything talking to the harness directly got none
                    # of it.
                    saved = timer_lab.save(
                        self.server.config,
                        body.get("timer"),
                        they_meant_it=bool(body.get("anyway")),
                    )
                    # Looked at once now, so a timer added at noon does not set
                    # the night's job off the moment it is saved.
                    timer_lab.looked_just_now(self.server.config)
                self._json({
                    "timer": saved.to_dict(),
                    "in_plain_words": timer_lab.in_plain_words(saved),
                    "why_not": timer_lab.what_stops_it_running_alone(
                        self.server.config, saved.automation
                    ),
                })
            elif self.path == "/api/timers/remove":
                with self.server.pipelines_lock:
                    self._json({
                        "note": timer_lab.remove(
                            self.server.config, str(body.get("name") or "")
                        )
                    })
            elif self.path == "/api/timers/turn":
                # Only the on-off switch, flipped where the timer is kept.
                # Sending the whole timer back from a panel that had been open
                # a while put back the old time and the old automation with it.
                with self.server.pipelines_lock:
                    one = timer_lab.load(
                        self.server.config, str(body.get("name") or "")
                    )
                    one.turned_on = bool(body.get("turned_on"))
                    saved = timer_lab.save(
                        self.server.config,
                        one.to_dict(),
                        they_meant_it=bool(body.get("anyway")),
                    )
                self._json({
                    "timer": saved.to_dict(),
                    "note": f"{saved.name} is turned {'on' if saved.turned_on else 'off'}.",
                })
            elif self.path == "/api/timers/run-now":
                # What the machine's scheduler would do, done here so somebody
                # can see it work rather than wait until two in the morning.
                name = str(body.get("name") or "")
                with self.server.pipelines_lock:
                    one = timer_lab.load(self.server.config, name)
                    held = pipeline_lab.load(self.server.config, one.automation)
                # The run itself is outside the lock: it can take the best part
                # of an hour, and the panel must not be dead for all of it.
                run = pipeline_lab.run_it(
                    self.server.config, held, check_kinds=self.server.check_kinds,
                )
                # Written down like any other run. Without this the line saying
                # what it last did never changed, however many times somebody
                # pressed the button.
                # Cleaned before it goes anywhere. Cleaned only on its way to
                # the file, the same words still came back to the browser and
                # onto the screen.
                said = timer_lab.in_safe_words(self.server.config, run.said)
                with self.server.pipelines_lock:
                    timer_lab.write_down_a_run(
                        self.server.config, one, said, run.passed, by_hand=True
                    )
                self._json({"passed": run.passed, "said": said})
            elif self.path == "/api/chat/say":
                # No lock here: the conversation has one of its own, taken by
                # every way of reaching it, including asking everyone at once.
                self._json(chat_lab.say(
                    self.server.config,
                    str(body.get("who") or ""),
                    str(body.get("text") or ""),
                ))
            elif self.path == "/api/chat/ask-everyone":
                # Every one of them, at the same time. Six one after another is
                # six waits.
                self._json({
                    "answers": chat_lab.ask_everyone(
                        self.server.config, str(body.get("text") or "")
                    )
                })
            elif self.path == "/api/chat/start-again":
                self._json({
                    "note": chat_lab.start_again(
                        self.server.config, str(body.get("who") or "")
                    ),
                    "said": [],
                })
            elif self.path == "/api/look-up":
                self._json(navigate_lab.look_it_up(
                    self.server.config,
                    asking=str(body.get("asking") or ""),
                    path=str(body.get("path") or ""),
                    line=int(body.get("line") or 0),
                    column=int(body.get("column") or 0),
                    name=str(body.get("name") or ""),
                ).to_dict())
            elif self.path == "/api/who-is-on-it/save":
                # Saving a team looks at the machine, which runs each
                # assistant's own tool. That belongs to the seats lock: holding
                # the suite's lock through a minute of waiting would stop every
                # change to the checks for a job that has nothing to do with
                # them.
                with self.server.seats_lock:
                    self._json({
                        "team": team_lab.save_team(
                            self.server.config,
                            str(body.get("name") or ""),
                            body.get("team"),
                            was=str(body.get("was") or ""),
                        ),
                        "teams": team_lab.teams(self.server.config),
                    })
            elif self.path == "/api/who-is-on-it/remove":
                with self.server.seats_lock:
                    self._json({
                        "note": team_lab.remove_team(self.server.config, str(body.get("name") or "")),
                        "teams": team_lab.teams(self.server.config),
                    })
            elif self.path == "/api/who-is-on-it/add-a-model":
                # Writing a route into somebody's own settings. It takes the
                # seats lock because that is the lock the settings file has.
                with self.server.seats_lock:
                    done = team_lab.add_its_own_way_in(self._settings_now(), body.get("model"))
                if done.get("needs_your_say"):
                    # The same choice the seat setup puts in front of somebody,
                    # so the panel can show the one window it already has.
                    self.server.seats_were_set_up = True
                self._json(done)
            elif self.path == "/api/who-is-on-it/check":
                # Says what is wrong with a drawing without saving any of it.
                with self.server.seats_lock:
                    problems = team_lab.check_it(self._settings_now(), body.get("team"))
                self._json({
                    "problems": problems,
                    "plain": team_lab.in_plain_words(body.get("team")),
                })
            elif self.path == "/api/pipelines/save":
                with self.server.pipelines_lock:
                    self._json({"pipeline": pipeline_lab.save(self.server.config, body.get("pipeline"))})
            elif self.path == "/api/pipelines/put-one-back":
                # Bringing an old version back is itself a save, so what is on
                # disk now goes on the pile too. Nothing is ever lost by this.
                with self.server.pipelines_lock:
                    name = str(body.get("name") or "")
                    which = int(body.get("which") or 0)
                    kept = pipeline_lab.older_ones(self.server.config, name)
                    if not 0 <= which < len(kept):
                        raise HarnessError(
                            f"There is no earlier version {which + 1} of {name}."
                        )
                    put_back = pipeline_lab.save(self.server.config, kept[which]["pipeline"])
                    self._json({
                        "pipeline": put_back,
                        "older_ones": [
                            {key: value for key, value in one.items() if key != "pipeline"}
                            for one in pipeline_lab.older_ones(self.server.config, name)
                        ],
                        "note": (
                            f"Put back the version from {kept[which]['saved_at']}. "
                            "What was there a moment ago is on the pile too."
                        ),
                    })
            elif self.path == "/api/pipelines/delete":
                with self.server.pipelines_lock:
                    self._json({
                        "note": pipeline_lab.remove(self.server.config, str(body.get("name") or "")),
                        "saved": pipeline_lab.saved_ones(self.server.config),
                    })
            elif self.path == "/api/pipelines/check":
                # Says whether a drawing would run, without running any of it.
                self._json({"pipeline": pipeline_lab.read_it(body.get("pipeline"))})
            elif self.path == "/api/pipelines/stop":
                self.server.pipeline_stop = True
                self._json({"note": "The run will stop after the step it is on."})
            elif self.path == "/api/pipelines/answer":
                # Somebody answering a step that stopped to ask. Nothing else
                # reads these, and they are cleared at the start of every run.
                step = str(body.get("step") or "")
                if not step:
                    raise HarnessError("Say which step is being answered.")
                self.server.pipeline_answers[step] = bool(body.get("carry_on"))
                self._json({
                    "step": step,
                    "carry_on": self.server.pipeline_answers[step],
                    "note": (
                        "Carrying on." if self.server.pipeline_answers[step]
                        else "Stopping there. Nothing after it will run."
                    ),
                })
            elif self.path == "/api/pipelines/run":
                drawn = pipeline_lab.read_it(body.get("pipeline"))
                if not self.server.pipeline_lock.acquire(blocking=False):
                    raise HarnessError(
                        "A pipeline is running already. Wait for it, or press Stop."
                    )
                self.server.pipeline_running = True
                self.server.pipeline_stop = False
                self.server.pipeline_answers = {}
                self.server.pipeline_waiting_at = ""
                # Three ways to run less than the whole thing: carry on from a
                # step, run one step on its own, and fill in what a step said
                # it would ask about.
                from_here = str(body.get("from_here") or "")
                only = str(body.get("only") or "")
                answers = body.get("answers")
                answers = answers if isinstance(answers, dict) else {}
                events = self.server.events
                config = self.server.config
                kinds = self.server.check_kinds
                server = self.server

                def go() -> None:
                    try:
                        events.add({"kind": "pipeline_started", "node": "pipeline",
                                    "payload": {"name": drawn["name"]}})
                        def waiting_on(step: str):
                            server.pipeline_waiting_at = (
                                step if step not in server.pipeline_answers else ""
                            )
                            return server.pipeline_answers.get(step)

                        run = pipeline_lab.run_it(
                            config, drawn, tell=events.add, check_kinds=kinds,
                            stopping=lambda: server.pipeline_stop,
                            from_here=from_here, only=only, answers=answers,
                            waiting_on=waiting_on,
                        )
                        server.pipeline_run = run.to_dict()
                        events.add({"kind": "pipeline_finished", "node": "pipeline",
                                    "payload": run.to_dict()})
                    except Exception as exc:  # noqa: BLE001 - it has to end, whatever happens
                        # Nothing ran: a run that gets this far has fallen over
                        # before the first step, so there are no steps to
                        # report. Saying that plainly beats an empty list that
                        # reads as "nothing went wrong".
                        server.pipeline_run = {
                            "name": drawn.get("name", ""), "nodes": [], "passed": False,
                            "said": (
                                f"The run stopped before any step ran, so nothing was "
                                f"checked: {exc}"
                            ),
                            "milliseconds": 0,
                        }
                        events.add({"kind": "pipeline_finished", "node": "pipeline",
                                    "payload": server.pipeline_run})
                    finally:
                        # However it ends, the next press has to work.
                        server.pipeline_running = False
                        server.pipeline_waiting_at = ""
                        server.pipeline_lock.release()

                try:
                    threading.Thread(target=go, name="pipeline", daemon=True).start()
                except Exception:
                    self.server.pipeline_running = False
                    self.server.pipeline_lock.release()
                    raise
                self._json({"accepted": True, "name": drawn["name"]}, HTTPStatus.ACCEPTED)
            elif self.path == "/api/settings/trust-anyway":
                # Deliberate, and only ever from a press. The panel shows the
                # whole file and what in it carries risk before offering this.
                with self.server.seats_lock:
                    self._json({
                        "trusted": True,
                        "note": seat_setup.trust_it_anyway(
                            self._settings_now(), str(body.get("seen") or "")
                        ),
                    })
            elif self.path == "/api/setup/do-it":
                # One name, chosen from a list this module holds. Nothing from
                # the request reaches a command line.
                option = str(body.get("option") or "")
                self._json(self.server.setup_runner.start(self._settings_now(), option))
            elif self.path == "/api/how-it-works":
                # The picture on the first screen is drawn from the workflow
                # that will really run, so it cannot say one thing while the
                # harness does another. The page sends the workflow it has on
                # screen; with nothing to send, the shipped one is described.
                asked = body.get("graph")
                self._json(
                    plain_graph.in_plain_words(
                        asked if isinstance(asked, dict) else self.server.template
                    )
                )
            elif self.path == "/api/seats/share-the-work":
                graph = body.get("graph")
                routes = body.get("routes") or []
                if not isinstance(routes, list):
                    raise HarnessError("Routes must be a list")
                self._json({
                    "graph": seat_setup.share_the_work(
                        graph if isinstance(graph, dict) else self.server.template,
                        [str(item) for item in routes][:8],
                    )
                })
            elif self.path == "/api/qa/share":
              with self.server.suite_lock:
                  path, page = share.write(
                      self.server.config,
                      str(body.get("run") or ""),
                      with_pictures=body.get("pictures") is not False,
                  )
                  answer = page.to_dict()
                  answer["path"] = path.relative_to(self.server.config.project_root).as_posix()
                  self._json(answer)
            elif self.path == "/api/qa/coverage":
                url = str(body.get("url") or "")
                selectors.check_url(self.server.config, url)
                asked_pages = body.get("max_pages")
                pages = (
                    coverage.DEFAULT_MAX_PAGES
                    if asked_pages in (None, "")
                    else self._whole_number(asked_pages, "The number of pages", 1, 500)
                )
                if not self.server.reserve_qa():
                    self._json({"error": "A check run is already active"}, HTTPStatus.CONFLICT)
                    return
                thread = threading.Thread(
                    target=self._run_coverage, args=(url, pages), daemon=True
                )
                try:
                    thread.start()
                except Exception:
                    self.server.release_qa()
                    raise
                self._json({"accepted": True}, HTTPStatus.ACCEPTED)
            elif self.path == "/api/qa/coverage/add":
              with self.server.suite_lock:
                  asked = body.get("addresses") or []
                  if not isinstance(asked, list):
                      raise HarnessError("Addresses must be a list")
                  wanted = [str(item) for item in asked][:50]
                  for address in wanted:
                      selectors.check_url(self.server.config, address)
                  added = coverage.add_missing(
                      self.server.config, wanted, extra_kinds=self.server.check_kinds
                  )
                  self._json({"added": added})
            elif self.path == "/api/qa/pick":
                url = str(body.get("url") or "")
                selectors.check_url(self.server.config, url)
                if not self.server.reserve_qa():
                    self._json({"error": "A check run is already active"}, HTTPStatus.CONFLICT)
                    return
                thread = threading.Thread(target=self._run_pick, args=(url,), daemon=True)
                try:
                    thread.start()
                except Exception:
                    self.server.release_qa()
                    raise
                self._json({"accepted": True}, HTTPStatus.ACCEPTED)
            elif self.path == "/api/qa/baseline":
                suite = qalab.load_suite(self.server.config, None, self.server.check_kinds)
                shots = [case.id for case in suite.cases if case.kind == "visual"]
                asked = body.get("cases") or []
                if not isinstance(asked, list):
                    raise HarnessError("Cases must be a list")
                if asked:
                    unknown = sorted({str(item) for item in asked} - set(shots))
                    if unknown:
                        raise HarnessError(f"{unknown[0]} is not a screenshot check in this suite")
                    shots = [case_id for case_id in shots if case_id in {str(item) for item in asked}]
                if not shots:
                    raise HarnessError(
                        "This suite has no screenshot checks yet. Add one with the visual kind first."
                    )
                if not self.server.reserve_qa():
                    self._json({"error": "A check run is already active"}, HTTPStatus.CONFLICT)
                    return
                thread = threading.Thread(
                    target=self._run_checks,
                    args=(suite, [], shots, True),
                    daemon=True,
                )
                try:
                    thread.start()
                except Exception:
                    self.server.release_qa()
                    raise
                self._json({"accepted": True, "cases": len(shots)}, HTTPStatus.ACCEPTED)
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
        except ConnectionError:
            # The page went to another view and closed the connection while
            # this was still answering. There is nobody left to tell, and
            # trying anyway wrote a 500 into a log and into the console of
            # whatever opened the next page.
            self.close_connection = True
        except Exception as exc:
            # Whatever went wrong that nothing expected, said without whatever
            # was in it. Anything that gets this far has not been through the
            # redactor on its own way, and an address with a password in it is
            # exactly the sort of thing that turns up in one of these.
            self._json(
                {"error": self._something_nobody_expected(exc)},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def _run_record(self, url: str) -> None:
        events = self.server.events
        events.add({"kind": "record_started", "node": "checks", "payload": {"url": url}})
        try:
            taken = recorder.record(self.server.config, url)
            case = taken.case()
            # Reading the suite, adding to it and writing it back is one piece
            # of work. Another change arriving in the middle would be lost.
            with self.server.suite_lock:
                suite = qalab.load_suite(self.server.config, None, self.server.check_kinds)
                cases = [item.to_dict() for item in suite.cases]
                # A second recording must not quietly replace the first.
                names = {item["id"] for item in cases}
                case["id"] = case["id"] if case["id"] not in names else f"{case['id']}-{len(names) + 1}"
                cases.append(case)
                written = qalab.parse_suite(
                    {"schema_version": 1, "name": suite.name, "cases": cases},
                    extra_kinds=self.server.check_kinds,
                )
                qalab.write_suite(self.server.config, written)
            events.add({
                "kind": "record_result",
                "node": "checks",
                "payload": {"added": case["id"], "steps": len(taken.steps), "left_out": list(taken.skipped)},
            })
        except Exception as exc:
            events.add({"kind": "record_error", "node": "checks", "payload": {"error": str(exc)}})
        finally:
            self.server.release_qa()

    def _run_coverage(self, url: str, max_pages: int) -> None:
        events = self.server.events
        events.add({"kind": "coverage_started", "node": "checks", "payload": {"url": url}})
        try:
            found = coverage.look(
                self.server.config,
                url,
                max_pages=max_pages,
                extra_kinds=self.server.check_kinds,
            )
            events.add({"kind": "coverage_result", "node": "checks", "payload": found.to_dict()})
        except Exception as exc:
            events.add({"kind": "coverage_error", "node": "checks", "payload": {"error": str(exc)}})
        finally:
            self.server.release_qa()

    def _run_pick(self, url: str) -> None:
        events = self.server.events
        events.add({"kind": "pick_started", "node": "checks", "payload": {"url": url}})
        try:
            picked = selectors.pick(self.server.config, url)
            events.add({"kind": "pick_result", "node": "checks", "payload": picked.to_dict()})
        except Exception as exc:
            events.add({"kind": "pick_error", "node": "checks", "payload": {"error": str(exc)}})
        finally:
            self.server.release_qa()

    def _run_checks(
        self,
        suite: qalab.QaSuite,
        tags: list[str],
        ids: list[str],
        update_baselines: bool = False,
    ) -> None:
        events = self.server.events
        events.add({"kind": "qa_started", "node": "checks", "payload": {"suite": suite.name}})
        try:
            result = qalab.QaRunner(
                self.server.config,
                extra_kinds=self.server.check_kinds,
                update_baselines=update_baselines,
            ).run(suite, tags=tags, ids=ids, write_artifacts=not update_baselines)
            if not update_baselines:
                # Saving pictures is not a test result, so it stays out of the
                # history the flaky-check advice is worked out from.
                qalab.record_history(self.server.config, result)
            # The kept copy is the one the panel asks for later, so it is the
            # cleaned one. The run folder still holds the whole thing.
            self.server.qa_result = events.redactor.value(result.to_dict())
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
