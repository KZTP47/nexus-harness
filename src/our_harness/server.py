from __future__ import annotations

import json
import hashlib
import hmac
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
import webbrowser
from contextlib import contextmanager
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, TYPE_CHECKING

from . import bundle
from . import cancellation
from . import comparison
from . import coverage
from . import handover
from . import autosetup
from . import explain as explainer
from . import pipeline_starters
from . import pipelines as pipeline_lab
from . import pipeline_runs as pipeline_runtime
from . import chat as chat_lab
from . import swarm as swarm_lab
from . import swarm_chats
from . import swarm_work
from . import swarm_runs
from . import swarm_goal_queue
from . import web_chats
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
from .config import LoadedConfig, load_config
from .detect import combined_commands, detect_project
from .doctor import run_doctor
from .graphs import migrate_graph, resolve_graph_execution_policy, resolve_workflow_policy, simulate_graph, validate_graph
from .memory import MemoryStore
from .models import HarnessError, ProviderOutcomeUnknown
from .runtime_integrity import atomic_text, compare as compare_runtime_mac, mac

if TYPE_CHECKING:
    from . import long_horizon


DIRECT_LONG_HORIZON_ADMISSION_SCHEMA_VERSION = 1
DIRECT_LONG_HORIZON_ADMISSION_MAX_BYTES = 12_000_000
DIRECT_LONG_HORIZON_ADMISSION_MAX_PENDING = 20
DIRECT_LONG_HORIZON_ADMISSION_MAX_TOMBSTONES = 2048
DIRECT_LONG_HORIZON_ADMISSION_BINDING_SCHEMA_VERSION = 1
DIRECT_LONG_HORIZON_RECEIPT_SCHEMA_VERSION = 1
DIRECT_LONG_HORIZON_TERMINAL_RECEIPT_SCHEMA_VERSION = 1


def _long_horizon_module():
    """Load the optional goal engine only when its endpoints or fences are used."""
    from . import long_horizon
    return long_horizon
from .plugins import check_kinds, load_plugins
from .redaction import CredentialRedactor
from .provider_help import setup_advice
from . import provider_repair
from .providers import ProviderRegistry
from .providers.connection import connection_status
from . import workflows as workflow_store
from .workflow import HarnessApplication
from . import __version__


def loopback_url(host: str, port: int) -> str:
    authority = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"http://{authority}:{port}"


def harness_commit_identity() -> str:
    embedded = str(os.environ.get("NEXUS_BUILD_COMMIT") or "").strip()
    if embedded and embedded != "unknown":
        return embedded + ("+dirty" if os.environ.get("NEXUS_BUILD_DIRTY") == "1" else "")
    root = Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=2, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    commit = result.stdout.strip()
    return commit if re.fullmatch(r"[0-9a-fA-F]{40}", commit) else "unknown"


def effective_route_readiness(config: LoadedConfig) -> list[dict[str, Any]]:
    """Check every model route the configured workflow can assign work to."""

    registry = ProviderRegistry(config)
    required_routes = ["default"]
    required_routes.extend(agent.provider_ref for agent in registry.agents())
    readiness: list[dict[str, Any]] = []
    for route in dict.fromkeys(required_routes):
        try:
            status = connection_status(config, route, timeout_seconds=2.0)
        except HarnessError as exc:
            status = {
                "route": route, "kind": "unknown", "installed": False,
                "state": "invalid", "authentication": "unknown",
                "checked_by": "effective-route-resolution", "note": str(exc),
            }
        unknown_but_probeable = (
            bool(status.get("installed"))
            and str(status.get("kind")) in {"gemini-cli", "copilot-cli"}
            and str(status.get("authentication")) == "unknown"
        )
        if unknown_but_probeable:
            status["state"] = "first-request-required"
            status["ready_for_first_request"] = True
            status["note"] = (
                str(status.get("note") or "").rstrip()
                + " Nexus cannot verify this CLI without a model call. The first run is clearly treated as a live readiness request; "
                  "if the provider refuses it, no success is claimed and its exact sign-in error is shown."
            ).strip()
        isolated_codex = (
            bool(status.get("installed"))
            and str(status.get("kind")) == "codex-cli"
            and str(status.get("state")) == "isolated-ready"
        )
        if isolated_codex:
            status["ready_for_first_request"] = True
            status["note"] = (
                str(status.get("note") or "").rstrip()
                + " Nexus will verify ChatGPT authentication through the first isolated request; "
                  "the incompatible user configuration is not loaded for agent turns."
            ).strip()
        status["ready"] = unknown_but_probeable or isolated_codex or (bool(status.get("installed")) and str(status.get("state")) in {
            "authenticated", "configured", "ready",
        })
        readiness.append(status)
    return readiness


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
        with self.lock:
            # Config reload replaces the credential redactor under this same
            # lock.  Cleaning inside the lock prevents an event from reading
            # the old redactor just before a newly configured secret becomes
            # effective and then publishing with the new runtime revision.
            cleaned = {
                key: (self.redactor.value(value) if key == "payload" else value)
                for key, value in event.items()
            }
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

    def replace_redactor(self, redactor: CredentialRedactor) -> None:
        """Atomically change the policy used by every later UI event."""

        with self.lock:
            self.redactor = redactor

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

_CHAT_ACTIVITY_ID = re.compile(r"^[A-Za-z0-9_-]{8,80}$")


class ChatActivities:
    """Small process-local progress feed for long board-chat requests."""

    def __init__(self, limit: int = 128):
        self.limit = limit
        self.lock = threading.Lock()
        self.records: dict[str, dict[str, Any]] = {}

    def _id(self, activity_id: object) -> str:
        value = str(activity_id or "").strip()
        if value and not _CHAT_ACTIVITY_ID.fullmatch(value):
            raise HarnessError("That chat activity ID is not valid")
        return value

    def update(
        self, activity_id: object, stage: str, detail: str = "", *, state: str = "working"
    ) -> None:
        wanted = self._id(activity_id)
        if not wanted:
            return
        now = time.time()
        with self.lock:
            previous = self.records.get(wanted, {})
            began = previous.get("started_at", now)
            self.records[wanted] = {
                "activity": wanted,
                "state": state,
                "stage": str(stage)[:180],
                "detail": str(detail)[:500],
                "started_at": began,
                "updated_at": now,
                "turns": list(previous.get("turns", [])),
            }
            while len(self.records) > self.limit:
                self.records.pop(next(iter(self.records)))

    def add_turn(self, activity_id: object, turn: object) -> None:
        wanted = self._id(activity_id)
        if not wanted or not isinstance(turn, dict):
            return
        kept = {
            key: str(turn.get(key) or "")[:longest]
            for key, longest in {
                "who": 12, "speaker_id": 100, "speaker_name": 100,
                "speaker_route": 100, "recipient_id": 100,
                "recipient_name": 100, "text": 20000, "model": 200,
                "phase": 40,
            }.items()
        }
        kept["milliseconds"] = max(0, int(turn.get("milliseconds") or 0))
        now = time.time()
        with self.lock:
            previous = self.records.get(wanted, {})
            previous["activity"] = wanted
            previous.setdefault("state", "working")
            previous.setdefault("stage", "Receiving agent replies")
            previous.setdefault("detail", "Completed agent turns are appearing in the chat.")
            previous.setdefault("started_at", now)
            previous["updated_at"] = now
            previous["turns"] = [*previous.get("turns", []), kept][-16:]
            self.records[wanted] = previous

    def read(self, activity_id: object) -> dict[str, Any]:
        wanted = self._id(activity_id)
        if not wanted:
            raise HarnessError("A chat activity ID is required")
        with self.lock:
            record = self.records.get(wanted)
            if record:
                return dict(record)
        return {
            "activity": wanted,
            "state": "waiting",
            "stage": "Starting the request",
            "detail": "Nexus is preparing the agent connection.",
            "turns": [],
        }


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
        self._swarm_runs: swarm_runs.SwarmRunStore | None = None
        self._swarm_communication_runs: swarm_runs.SwarmRunStore | None = None
        self._swarm_runner: swarm_lab.Running | None = None
        self._swarm_goal_queue: swarm_goal_queue.SwarmGoalQueueStore | None = None
        self._long_horizon: long_horizon.LongHorizonRuntime | None = None
        self.authority_lock = threading.Lock()
        # Provider discovery is an in-memory acceleration only.  Bind it to an
        # exact runtime-config revision so a slow refresh from the old project
        # or old provider map can never publish after a settings reload.
        self._config_revision = 1
        self._swarm_known_routes: list[dict[str, Any]] | None = None
        self._swarm_known_routes_revision = 0
        self.chat_activities = ChatActivities()
        self.chat_cancellations = cancellation.ChatCancellationRegistry()
        self.web_chats = web_chats.WebChatBroker()
        # Board runners and direct chat use the provider-neutral chat module,
        # which may execute on background threads. Give all of those paths the
        # same Electron mailbox owned by this server.
        web_chats.replace_active(self.web_chats)
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
        # "I don't care, just do it for me" runs one job at a time, in the
        # background, because fetching a model can take a long while and the
        # page has to keep saying what is happening.
        self.setup_runner = autosetup.Runner()
        # One pipeline run at a time. A pipeline starts real suites and real
        # commands, and two at once would fight over the same project.
        self.pipeline_lock = threading.Lock()
        # Serializes project switching with the short admission window before
        # a command owns its durable run/provider lease.
        self.project_admission_lock = threading.Lock()
        # Created only when automation functionality is first used. Merely
        # opening an unrelated chat must not write a project authority
        # descriptor into that project.
        self._pipeline_store: pipeline_runtime.PipelineRunStore | None = None
        self.pipeline_active_run_id = ""
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

    @property
    def swarm_known_routes(self) -> list[dict[str, Any]] | None:
        """Provider readiness cached only for the current config revision.

        The property remains assignable for the small number of tests and
        internal callers that seed a known route set.  Such an assignment is
        explicitly bound to the revision current at that instant.
        """

        with self.authority_lock:
            if self._swarm_known_routes_revision != self._config_revision:
                return None
            return self._swarm_known_routes

    @swarm_known_routes.setter
    def swarm_known_routes(self, routes: list[dict[str, Any]] | None) -> None:
        with self.authority_lock:
            self._swarm_known_routes = routes
            self._swarm_known_routes_revision = (
                self._config_revision if routes is not None else 0
            )

    @staticmethod
    def _long_horizon_has_live_effect(
        runtime: long_horizon.LongHorizonRuntime | None,
    ) -> bool:
        return bool(runtime is not None and any(
            worker.is_alive() for worker in runtime.workers.values()
        ))

    def require_config_reload_boundary(self) -> None:
        """Refuse a config swap in the middle of a long-horizon effect.

        Long-horizon workers intentionally keep one LoadedConfig for an
        entire provider/apply boundary.  A paused or waiting goal has no live
        worker and can be reopened on the new revision; a live worker must
        finish reaching that durable boundary first.
        """

        with self.authority_lock:
            if self._long_horizon_has_live_effect(self._long_horizon):
                raise HarnessError(
                    "An AI goal is in the middle of a provider or apply step. "
                    "Wait for that step to finish or pause, then change the settings."
                )

    def reload_config(
        self, project_root: Path | None = None, *, reset_project_state: bool = False,
    ) -> LoadedConfig:
        """Load and atomically publish one complete runtime config revision.

        Configuration is more than ``self.config``: plugin check kinds,
        workflow policy, credential redaction, long-horizon dispatch and the
        provider-readiness cache are all derived from it.  Preparing every
        dependency before taking the authority lock means a bad new config is
        never partially published.  The commit below then makes those pieces
        advance together.
        """

        if project_root is None:
            with self.authority_lock:
                wanted = self.config.project_root
        else:
            wanted = Path(project_root)
        config = load_config(wanted)
        registry = load_plugins(config)
        workflow_policy = resolve_workflow_policy(config, registry.workflow_nodes)
        check_kinds_for_config = dict(registry.check_kinds)
        event_redactor = CredentialRedactor(config)

        with self.authority_lock:
            old_long_horizon = self._long_horizon
            live_long_horizon = self._long_horizon_has_live_effect(old_long_horizon)
            if live_long_horizon:
                raise HarnessError(
                    "An AI goal is in the middle of a provider or apply step. "
                    "Wait for that step to finish or pause, then change the settings."
                )
            if reset_project_state and old_long_horizon is not None:
                active_goals = old_long_horizon.store.active_authority_goals()
                if active_goals:
                    raise HarnessError(
                        "Long-horizon project work is unfinished. Complete or cancel it "
                        "before moving projects."
                    )
            if old_long_horizon is not None:
                # No worker can be using this checkpointer now. Paused and
                # waiting goals remain durable; the next access reconstructs
                # the runtime with the new config and recovers them there.
                old_long_horizon.close()

            if reset_project_state:
                self._pipeline_store = None
                self._swarm_runs = None
                self._swarm_communication_runs = None
                self._swarm_runner = None
                self._swarm_goal_queue = None
                self.pipeline_active_run_id = ""
                self.qa_result = None
                self.pipeline_run = None
                self.seats_before = None
                self.seats_were_set_up = False
            else:
                # These stores are project-authority caches, so they can stay
                # open for a same-project settings change. Their only
                # config-derived state is the policy used to redact future
                # records; refresh it without replacing a durable coordinator.
                if self._pipeline_store is not None:
                    self._pipeline_store.config = config
                    self._pipeline_store.redactor = CredentialRedactor(config)
                if self._swarm_runs is not None:
                    self._swarm_runs.redactor = CredentialRedactor(config)
                if self._swarm_communication_runs is not None:
                    self._swarm_communication_runs.redactor = CredentialRedactor(config)

            self._long_horizon = None
            self.workflow_policy = workflow_policy
            self.check_kinds = check_kinds_for_config
            # Replace the redactor before publishing config. EventBus uses its
            # own lock, so no event can be cleaned across this hand-off.
            self.events.replace_redactor(event_redactor)
            self.config = config
            self._config_revision += 1
            self._swarm_known_routes = None
            self._swarm_known_routes_revision = 0
        return config

    @property
    def pipeline_store(self) -> pipeline_runtime.PipelineRunStore:
        with self.authority_lock:
            held = self._pipeline_store
            if held is None:
                held = pipeline_runtime.PipelineRunStore(self.config)
                self._pipeline_store = held
            return held

    @property
    def swarm_runs(self) -> swarm_runs.SwarmRunStore:
        with self.authority_lock:
            held = self._swarm_runs
            if held is None:
                held = swarm_runs.SwarmRunStore(self.config)
                self._swarm_runs = held
            return held

    @property
    def swarm_communication_runs(self) -> swarm_runs.SwarmRunStore:
        """Durable provider/chat effects that carry no project-work authority."""

        with self.authority_lock:
            held = self._swarm_communication_runs
            if held is None:
                held = swarm_runs.SwarmRunStore.for_communication(self.config)
                self._swarm_communication_runs = held
            return held

    def find_swarm_run(
        self, identity: str,
    ) -> tuple[swarm_runs.SwarmRunStore, dict[str, Any]]:
        """Resolve an exact run across execution and communication journals.

        Ordinary chat can remain available when project execution authority is
        paused, so Stop and activity reads cannot assume the execution store.
        Exact run IDs always outrank request-ID aliases, even when an alias in
        the other journal is active. Only then prefer the one active alias and
        fail closed on a genuinely ambiguous reused request identity.
        """

        wanted = str(identity or "").strip()
        if not wanted:
            raise HarnessError("A Swarm run identity is required")
        communication = self.swarm_communication_runs
        with self.authority_lock:
            execution = self._swarm_runs
        execution_error: HarnessError | None = None
        if execution is None:
            # A Stop/activity request may arrive through a second Nexus
            # process which never created this run locally. Open the execution
            # journal on demand so project-work chats remain addressable across
            # processes just like communication-only chats. If execution
            # authority cannot be opened, an exact communication match may
            # still be read or stopped safely.
            try:
                execution = self.swarm_runs
            except HarnessError as exc:
                execution_error = exc
        stores = [one for one in (execution, communication) if one is not None]
        matches: list[tuple[swarm_runs.SwarmRunStore, dict[str, Any]]] = []
        for store in stores:
            try:
                matches.append((store, store.get(wanted)))
            except HarnessError as exc:
                if str(exc) != "That Swarm run does not exist":
                    raise
        exact = [
            one for one in matches
            if str(one[1].get("run_id") or "") == wanted
        ]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise HarnessError(
                "That exact run_id belongs to more than one durable Swarm run journal."
            )
        if execution_error is not None:
            # Without the execution journal we cannot prove that a matching
            # communication request-ID alias does not shadow an exact project
            # run ID. Exact communication run IDs returned above remain safe;
            # every alias lookup fails closed until both journals are readable.
            raise execution_error
        active = [
            one for one in matches
            if str(one[1].get("status") or "") in {"accepted", "running", "stopping"}
        ]
        if len(active) == 1:
            return active[0]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise HarnessError(
                "That request identity belongs to more than one durable Swarm run; use its exact run_id."
            )
        raise HarnessError("That Swarm run does not exist")

    @property
    def swarm_runner(self) -> swarm_lab.Running:
        with self.authority_lock:
            held = self._swarm_runner
            if held is None:
                # Avoid recursively taking authority_lock via the property.
                store = self._swarm_runs
                if store is None:
                    store = swarm_runs.SwarmRunStore(self.config)
                    self._swarm_runs = store
                held = swarm_lab.Running(store)
                self._swarm_runner = held
            return held

    @property
    def swarm_goal_queue(self) -> swarm_goal_queue.SwarmGoalQueueStore:
        with self.authority_lock:
            held = self._swarm_goal_queue
            if held is None:
                held = swarm_goal_queue.SwarmGoalQueueStore(self.config)
                self._swarm_goal_queue = held
            return held

    @property
    def long_horizon(self) -> long_horizon.LongHorizonRuntime:
        with self.authority_lock:
            held = self._long_horizon
            if held is None:
                held = _long_horizon_module().LongHorizonRuntime(
                    self.config, external_project_conflicts=self.legacy_project_conflicts,
                )
                held.recover_all()
                self._long_horizon = held
            return held

    @staticmethod
    def _long_horizon_chat(
        config: LoadedConfig, board: dict[str, Any], goal: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Resolve a goal's immutable origin chat without redirecting history."""

        chat_id = str(goal.get("conversation_id") or "")
        if not chat_id:
            return None
        candidates = list(dict.fromkeys([
            str(goal.get("lead_agent_id") or ""),
            *[
                str(one or "") for one in (goal.get("requested_agent_ids") or [])
                if str(one or "")
            ],
            *[
                str(one.get("id") or "") for one in (goal.get("agents") or [])
                if isinstance(one, dict) and str(one.get("id") or "")
            ],
        ]))
        for agent_id in candidates:
            if not agent_id:
                continue
            try:
                return swarm_chats.resolve(
                    config, board, agent_id, chat_id, allow_binding_drift=True,
                )
            except HarnessError:
                continue
        # Legacy and board-wide goals intentionally have no resolvable saved
        # pair chat. Their Mission-control history remains authoritative.
        return None

    def project_long_horizon_chat_statuses(
        self, goals: list[dict[str, Any]],
    ) -> None:
        """Idempotently copy canonical goal transitions into origin chats."""

        chat_goals = [
            goal for goal in goals
            if isinstance(goal, dict) and str(goal.get("conversation_id") or "")
        ]
        if not chat_goals:
            return
        with self.swarm_lock:
            standing = self.swarm_standing()
            board = standing["board"]
            for goal in chat_goals:
                chat_id = str(goal.get("conversation_id") or "")
                try:
                    with self.swarm_communication_runs.conversation_turn(
                        f"long-goal-projection-{uuid.uuid4().hex}", chat_id,
                        timeout=0.0,
                    ):
                        conversation = self._long_horizon_chat(
                            self.config, board, goal,
                        )
                        if conversation is None:
                            continue
                        chat_lab.keep_long_horizon_status(
                            self.config,
                            str(conversation.get("transcript_route") or ""),
                            goal,
                            filed_as=str(conversation.get("filed_as") or ""),
                            chat_id=chat_id,
                            project_id=str(
                                (goal.get("project") or {}).get("id") or ""
                            ),
                            lead_id=str(goal.get("lead_agent_id") or ""),
                        )
                except HarnessError as exc:
                    # A normal chat turn owns this exact transcript briefly.
                    # Its next goal poll will reconcile the status; unrelated
                    # registry/integrity errors remain visible to the caller.
                    if "already working on another request" in str(exc):
                        continue
                    raise

    @staticmethod
    def _canonical_direct_long_horizon_payload(
        supplied: dict[str, Any],
    ) -> dict[str, Any]:
        """Freeze the one exact bounded payload saved and later admitted."""

        request_id = str(supplied.get("request_id") or "").strip()
        if not request_id or len(request_id) > 160:
            raise HarnessError("A stable request ID is required for direct project work")
        has_objectives = "objectives" in supplied
        has_text = supplied.get("text") is not None
        if has_objectives and not isinstance(supplied.get("objectives"), list):
            raise HarnessError(
                "Direct project objectives must be a JSON list of goal text."
            )
        objectives = (
            [str(one) for one in supplied.get("objectives", [])]
            if has_objectives else [str(supplied.get("text")) if has_text else ""]
        )
        prompt_text = "\n\n".join(objectives)
        if has_text and has_objectives \
                and str(supplied.get("text")) != prompt_text:
            raise HarnessError(
                "Direct project text and objectives disagree. Send one or send both "
                "with text exactly equal to the objectives joined by a blank line."
            )
        raw_attachments = supplied.get("attachments")
        if raw_attachments is None:
            raw_attachments = []
        if not isinstance(raw_attachments, list) or len(raw_attachments) > 6 \
                or any(not isinstance(one, dict) for one in raw_attachments):
            raise HarnessError("Direct project work accepts at most 6 attachment records")
        raw_criteria = supplied.get("success_criteria")
        if raw_criteria is not None and not isinstance(raw_criteria, list):
            raise HarnessError("Direct project success criteria must be a JSON list")
        raw_policy = supplied.get("policy")
        if raw_policy is not None and not isinstance(raw_policy, dict):
            raise HarnessError("Direct project policy must be a JSON object")
        payload = {
            "schema_version": DIRECT_LONG_HORIZON_ADMISSION_SCHEMA_VERSION,
            "request_id": request_id,
            "project_id": str(supplied.get("project_id") or ""),
            "lead_id": str(supplied.get("lead_id") or ""),
            "chat_id": str(supplied.get("chat_id") or ""),
            "text": prompt_text,
            "objectives": objectives,
            "success_criteria": (
                [str(one) for one in raw_criteria]
                if isinstance(raw_criteria, list) else None
            ),
            "policy": dict(raw_policy) if isinstance(raw_policy, dict) else None,
            "attachments": [dict(one) for one in raw_attachments],
        }
        try:
            frozen = json.loads(json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ))
        except (TypeError, ValueError) as exc:
            raise HarnessError(
                "Direct project admission inputs must be JSON-compatible"
            ) from exc
        encoded = json.dumps(
            frozen, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > DIRECT_LONG_HORIZON_ADMISSION_MAX_BYTES:
            raise HarnessError(
                "The exact direct project admission payload is too large to save safely"
            )
        return frozen

    def _direct_long_horizon_context(
        self, standing: dict[str, Any], payload: dict[str, Any],
    ) -> dict[str, Any]:
        project_id = str(payload.get("project_id") or "")
        project_root = self._board_project_path(standing["board"], project_id)
        lead_id = str(payload.get("lead_id") or "")
        chat_id = str(payload.get("chat_id") or "")
        if not chat_id:
            raise HarnessError(
                "Direct project work requires an exact saved chat identity. Use "
                "board-goal work for an explicitly project-wide agent pool."
            )
        conversation = swarm_chats.resolve(
            self.config, standing["board"], lead_id, chat_id,
        )
        if str(conversation.get("project") or "") != project_id:
            raise HarnessError(
                "The selected chat and long-horizon target project no longer match."
            )
        participant_ids = [
            str(one.get("id") or "")
            for one in conversation.get("pair_agents", [])
            if isinstance(one, dict) and str(one.get("id") or "")
        ]
        return {
            "project_id": project_id,
            "project_root": project_root,
            "lead_id": lead_id,
            "chat_id": chat_id,
            "conversation": conversation,
            "participant_ids": participant_ids,
            "transcript_route": str(conversation.get("transcript_route") or ""),
            "filed_as": str(conversation.get("filed_as") or ""),
        }

    @contextmanager
    def _direct_admission_turn(
        self, request_id: str, chat_id: str, *, timeout: float = 30.0,
    ):
        """Serialize one direct request identity and its exact chat cross-process."""

        request = str(request_id or "").strip()
        chat = str(chat_id or "").strip()
        request_key = hashlib.sha256(request.encode("utf-8")).hexdigest()
        request_owner = "long-goal-request-" + request_key
        chat_owner = "long-goal-chat-" + hashlib.sha256(
            f"{chat}\0{request}".encode("utf-8")
        ).hexdigest()
        with self.swarm_communication_runs.conversation_turn(
            request_owner, "direct-long-horizon-request:" + request_key,
            timeout=timeout,
        ):
            with self.swarm_communication_runs.conversation_turn(
                chat_owner, chat, timeout=timeout,
            ):
                yield

    def _preflight_direct_long_horizon(
        self, standing: dict[str, Any], payload: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Compute the exact goal binding without constructing a scheduler runtime."""

        store = _long_horizon_module().GoalStore(
            self.config, migrate_execution_metadata=False,
        )
        return store.preflight_runtime_admission(
            standing["board"], context["project_id"],
            list(payload["objectives"]), str(payload["request_id"]),
            lead_id=context["lead_id"],
            success_criteria=payload.get("success_criteria"),
            policy=payload.get("policy"), attachments=payload.get("attachments"),
            participant_ids=context["participant_ids"],
            conversation_id=context["chat_id"],
        )

    def _direct_admission_folder(self) -> tuple[Path, str]:
        store = self.swarm_communication_runs
        authority = str(store.chat_scope)
        parent = (store.root / "direct-long-horizon-admissions").resolve()
        folder = (parent / authority).resolve()
        if parent not in folder.parents:
            raise HarnessError("Direct project admission storage escaped its authority")
        folder.mkdir(parents=True, exist_ok=True)
        return folder, authority

    def _direct_admission_path(self, request_id: str) -> Path:
        folder, _authority = self._direct_admission_folder()
        return folder / (
            hashlib.sha256(str(request_id).encode("utf-8")).hexdigest() + ".json"
        )

    @staticmethod
    def _direct_admission_unsigned(record: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in record.items() if key != "integrity_mac"}

    @staticmethod
    def _direct_admission_binding_digest(record: dict[str, Any]) -> str:
        binding = record.get("admission_binding")
        if binding in (None, {}):
            return ""
        if not isinstance(binding, dict) or binding.get(
            "schema_version"
        ) != DIRECT_LONG_HORIZON_ADMISSION_BINDING_SCHEMA_VERSION:
            raise HarnessError(
                "A saved direct project admission has an unsupported goal binding"
            )
        digest = str(binding.get("admission_digest") or "").lower()
        if len(digest) != 64 or any(one not in "0123456789abcdef" for one in digest):
            raise HarnessError(
                "A saved direct project admission has an invalid goal binding"
            )
        return digest

    @staticmethod
    def _direct_terminal_client_state(record: dict[str, Any]) -> str:
        """Classify an authenticated terminal row without guessing its age.

        Version 0.2.1 terminal rows predate client acknowledgement and contain
        none of these three fields. They must be treated as unconsumed after an
        upgrade: a lost response is indistinguishable from a response the old
        renderer received. Every partially upgraded or malformed combination
        fails closed instead of silently authorizing another request.
        """

        if str(record.get("state") or "") not in {"discarded", "reconciled"}:
            raise HarnessError("Choose one terminal direct project request")
        has_schema = "terminal_receipt_schema_version" in record
        has_consumed = "client_consumed" in record
        has_consumed_ms = "client_consumed_ms" in record
        historical_keys = {
            "schema_version", "state", "request_id", "chat_id", "project_id",
            "lead_id", "payload_sha256", "intent_sha256", "execution_contract",
            "created_ms", "retired_ms", "goal_id", "integrity_mac",
        }
        if historical_keys - set(record):
            raise HarnessError(
                "A retired direct project request is missing exact historical fields"
            )
        allowed_keys = historical_keys | {"admission_binding"}
        if not has_schema and not has_consumed and not has_consumed_ms:
            if set(record) - allowed_keys:
                raise HarnessError(
                    "A retired direct project request does not match the exact legacy schema"
                )
            return "legacy_unconsumed"
        allowed_keys |= {
            "terminal_receipt_schema_version", "client_consumed",
            "client_consumed_ms",
        }
        if set(record) - allowed_keys:
            raise HarnessError(
                "A retired direct project request has unsupported terminal fields"
            )
        terminal_schema_version = record.get("terminal_receipt_schema_version")
        if type(terminal_schema_version) is not int \
                or terminal_schema_version \
                != DIRECT_LONG_HORIZON_TERMINAL_RECEIPT_SCHEMA_VERSION:
            raise HarnessError(
                "A retired direct project request has an unsupported terminal schema"
            )
        if type(record.get("client_consumed")) is not bool:
            raise HarnessError(
                "A retired direct project request has an invalid client acknowledgement"
            )
        if record["client_consumed"] is False:
            if has_consumed_ms:
                raise HarnessError(
                    "An unconsumed direct project request has a consumption timestamp"
                )
            return "unconsumed"
        consumed_ms = record.get("client_consumed_ms")
        if type(consumed_ms) is not int or consumed_ms <= 0:
            raise HarnessError(
                "A consumed direct project request has no valid consumption timestamp"
            )
        return "consumed"

    def _validate_direct_admission_contract(
        self, record: dict[str, Any], *, project_id: str, chat_id: str,
        lead_id: str,
    ) -> None:
        contract = record.get("execution_contract")
        if not isinstance(contract, dict) \
                or type(contract.get("schema_version")) is not int \
                or contract.get("schema_version") \
                != DIRECT_LONG_HORIZON_ADMISSION_SCHEMA_VERSION:
            raise HarnessError(
                "A saved direct project admission has an unsupported execution contract"
            )
        kind = str(contract.get("kind") or "")
        if kind not in {
            "direct_long_horizon_admission", "desktop_direct_long_horizon_outbox",
        }:
            raise HarnessError(
                "A saved direct project admission has an unsupported execution owner"
            )
        _folder, authority = self._direct_admission_folder()
        if str(contract.get("chat_scope") or "") != authority or any(
            str(contract.get(field) or "") != expected
            for field, expected in (
                ("project_id", project_id), ("chat_id", chat_id),
                ("lead_id", lead_id),
            )
        ):
            raise HarnessError(
                "A saved direct project admission disagrees with its execution contract"
            )
        if kind == "direct_long_horizon_admission":
            project_root_fingerprint = str(
                contract.get("project_root_fingerprint_sha256") or ""
            )
            if len(project_root_fingerprint) != 64 or any(
                one not in "0123456789abcdef" for one in project_root_fingerprint
            ):
                raise HarnessError(
                    "A saved direct project admission has no exact project-root fingerprint"
                )
        unsigned_contract = {
            key: value for key, value in contract.items()
            if key != "fingerprint_sha256"
        }
        expected_fingerprint = hashlib.sha256(json.dumps(
            unsigned_contract, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        if not hmac.compare_digest(
            str(contract.get("fingerprint_sha256") or ""), expected_fingerprint,
        ):
            raise HarnessError(
                "A saved direct project admission execution contract changed"
            )

    def _direct_admission_receipt(self, record: dict[str, Any]) -> dict[str, Any]:
        """Return a causal receipt only from an authenticated backend record."""

        unsigned = self._direct_admission_unsigned(record)
        if not compare_runtime_mac(
            "direct-long-horizon-admission-v1", unsigned,
            record.get("integrity_mac"),
        ):
            raise HarnessError(
                "A direct project admission receipt failed integrity verification"
            )
        state = str(record.get("state") or "pending")
        if state == "pending":
            payload = self._canonical_direct_long_horizon_payload(
                record.get("payload") if isinstance(record.get("payload"), dict) else {},
            )
            canonical = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
            if not hmac.compare_digest(
                hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                str(record.get("payload_sha256") or ""),
            ):
                raise HarnessError("A direct project admission receipt payload changed")
            request_id = str(payload.get("request_id") or "")
            chat_id = str(payload.get("chat_id") or "")
            project_id = str(payload.get("project_id") or "")
            lead_id = str(payload.get("lead_id") or "")
            intent_sha256 = chat_lab.long_horizon_intent_sha256(
                chat_id, project_id, lead_id, str(payload.get("text") or ""),
                payload.get("attachments"),
            )
        elif state in {"discarded", "reconciled"}:
            request_id = str(record.get("request_id") or "")
            chat_id = str(record.get("chat_id") or "")
            project_id = str(record.get("project_id") or "")
            lead_id = str(record.get("lead_id") or "")
            intent_sha256 = str(record.get("intent_sha256") or "").lower()
        else:
            raise HarnessError("A direct project admission receipt has an unsupported state")
        if not all((request_id, chat_id, project_id, lead_id)):
            raise HarnessError("A direct project admission receipt is incomplete")
        if str(record.get("request_id") or "") != request_id:
            raise HarnessError("A direct project admission receipt changed request identity")
        contract = record.get("execution_contract") \
            if isinstance(record.get("execution_contract"), dict) else {}
        if any(
            str(contract.get(key) or "") != expected
            for key, expected in (
                ("project_id", project_id), ("chat_id", chat_id), ("lead_id", lead_id),
            )
        ):
            raise HarnessError(
                "A direct project admission receipt disagrees with its execution contract"
            )
        if len(intent_sha256) != 64 or any(
            one not in "0123456789abcdef" for one in intent_sha256
        ):
            raise HarnessError("A direct project admission receipt has an invalid intent digest")
        return {
            "schema_version": DIRECT_LONG_HORIZON_RECEIPT_SCHEMA_VERSION,
            "request_id": request_id,
            "chat_id": chat_id,
            "project_id": project_id,
            "lead_id": lead_id,
            "intent_sha256": intent_sha256,
        }

    @staticmethod
    def _direct_discard_response(
        receipt: dict[str, Any], *, discarded: bool, reconciled: bool,
        safe_to_delete: bool, transcript_noted: bool = False,
        goal: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        for value in (discarded, reconciled, safe_to_delete, transcript_noted):
            if type(value) is not bool:
                raise HarnessError("Direct project discard receipt flags must be booleans")
        response = {
            **receipt,
            "discarded": discarded,
            "reconciled": reconciled,
            "safe_to_delete": safe_to_delete,
            "transcript_noted": transcript_noted,
        }
        if goal is not None:
            response["goal"] = goal
        return response

    @staticmethod
    def _goal_for_direct_receipt(
        goal: dict[str, Any], receipt: dict[str, Any], *,
        admission_digest: str = "", transcript_proven: bool = False,
    ) -> dict[str, Any]:
        """Require the returned canonical goal to name the receipt's exact cause."""

        if not isinstance(goal, dict):
            raise HarnessError("Direct project admission returned no canonical goal")
        canonical = dict(goal)
        if canonical.get("request_tombstone") is True:
            tombstone_version = canonical.get("request_tombstone_schema_version")
            if tombstone_version != _long_horizon_module().REQUEST_TOMBSTONE_SCHEMA_VERSION:
                raise HarnessError(
                    "The admitted goal request tombstone has an unsupported schema"
                )
            # Renderer goal receipts require a bounded positive schema marker.
            # Normalize only this response copy; the authenticated compact
            # tombstone remains stored in its native request schema.
            canonical.setdefault("schema_version", int(tombstone_version))
        if str(canonical.get("request_id") or "") != str(receipt["request_id"]):
            raise HarnessError("The admitted goal has a different request identity")
        if str(canonical.get("conversation_id") or "") != str(receipt["chat_id"]):
            raise HarnessError("The admitted goal has a different saved chat identity")
        if str(canonical.get("lead_agent_id") or "") != str(receipt["lead_id"]):
            raise HarnessError("The admitted goal has a different lead-agent identity")
        project = canonical.get("project") if isinstance(canonical.get("project"), dict) else {}
        stored_project_id = str(project.get("id") or "")
        if stored_project_id != str(receipt["project_id"]):
            if stored_project_id:
                raise HarnessError("The admitted goal has a different project identity")
            stored_digest = str(canonical.get("admission_digest") or "").lower()
            digest_proven = bool(admission_digest) and hmac.compare_digest(
                str(admission_digest).lower(), stored_digest,
            )
            if canonical.get("request_tombstone") is not True \
                    or not (digest_proven or transcript_proven):
                raise HarnessError("The admitted goal has a different project identity")
            # Older compact request tombstones did not retain the project id.
            # Exact full-digest or transcript proof safely restores it only in
            # this response; no persistent history is rewritten.
            canonical["project"] = {"id": str(receipt["project_id"])}
        return canonical

    def _read_direct_admission_path(self, path: Path) -> dict[str, Any]:
        if path.is_symlink() or not path.is_file():
            raise HarnessError("A saved direct project admission is not a regular file")
        if path.stat().st_size > DIRECT_LONG_HORIZON_ADMISSION_MAX_BYTES + 16_384:
            raise HarnessError("A saved direct project admission is oversized")
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HarnessError("A saved direct project admission is unreadable") from exc
        if not isinstance(record, dict) \
                or type(record.get("schema_version")) is not int \
                or record.get("schema_version") \
                != DIRECT_LONG_HORIZON_ADMISSION_SCHEMA_VERSION:
            raise HarnessError("A saved direct project admission has an unsupported schema")
        unsigned = self._direct_admission_unsigned(record)
        if not compare_runtime_mac(
            "direct-long-horizon-admission-v1", unsigned,
            record.get("integrity_mac"),
        ):
            raise HarnessError("A saved direct project admission failed integrity verification")
        self._direct_admission_binding_digest(record)
        request_id = str(record.get("request_id") or "")
        if not request_id or len(request_id) > 160:
            raise HarnessError("A saved direct project admission has an invalid request identity")
        expected_name = hashlib.sha256(request_id.encode("utf-8")).hexdigest() + ".json"
        if path.name != expected_name:
            raise HarnessError(
                "A saved direct project admission filename changed request identity"
            )
        state = str(record.get("state") or "pending")
        if state in {"discarded", "reconciled"}:
            chat_id = str(record.get("chat_id") or "")
            project_id = str(record.get("project_id") or "")
            lead_id = str(record.get("lead_id") or "")
            payload_sha256 = str(record.get("payload_sha256") or "")
            intent_sha256 = str(record.get("intent_sha256") or "")
            if not all((chat_id, project_id, lead_id)):
                raise HarnessError("A retired direct project request is incomplete")
            for label, digest in (
                ("payload", payload_sha256), ("intent", intent_sha256),
            ):
                if len(digest) != 64 or any(
                    one not in "0123456789abcdef" for one in digest
                ):
                    raise HarnessError(
                        f"A retired direct project request has an invalid {label} digest"
                    )
            for field in ("created_ms", "retired_ms"):
                timestamp = record.get(field)
                if type(timestamp) is not int or timestamp <= 0:
                    raise HarnessError(
                        "A retired direct project request has an invalid timestamp"
                    )
            goal_id = str(record.get("goal_id") or "")
            if (state == "reconciled" and not goal_id) \
                    or (state == "discarded" and goal_id):
                raise HarnessError(
                    "A retired direct project request has an inconsistent goal outcome"
                )
            self._validate_direct_admission_contract(
                record, project_id=project_id, chat_id=chat_id, lead_id=lead_id,
            )
            self._direct_terminal_client_state(record)
            record["state"] = state
            return record
        if state != "pending":
            raise HarnessError("A saved direct project admission has an unsupported state")
        payload = self._canonical_direct_long_horizon_payload(
            record.get("payload") if isinstance(record.get("payload"), dict) else {},
        )
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        if hashlib.sha256(canonical.encode("utf-8")).hexdigest() \
                != str(record.get("payload_sha256") or ""):
            raise HarnessError("A saved direct project admission payload changed")
        if str(payload.get("request_id") or "") != request_id:
            raise HarnessError("A saved direct project admission changed request identity")
        for field in ("created_ms", "updated_ms"):
            timestamp = record.get(field)
            if type(timestamp) is not int or timestamp <= 0:
                raise HarnessError(
                    "A saved direct project admission has an invalid timestamp"
                )
        self._validate_direct_admission_contract(
            record,
            project_id=str(payload.get("project_id") or ""),
            chat_id=str(payload.get("chat_id") or ""),
            lead_id=str(payload.get("lead_id") or ""),
        )
        record["payload"] = payload
        record["state"] = "pending"
        return record

    def _save_direct_admission(
        self, payload: dict[str, Any], context: dict[str, Any],
        admission_digest: str,
    ) -> dict[str, Any]:
        from .safety import ProjectTransactionLock

        path = self._direct_admission_path(str(payload["request_id"]))
        folder, authority = self._direct_admission_folder()
        contract = {
            "schema_version": DIRECT_LONG_HORIZON_ADMISSION_SCHEMA_VERSION,
            "kind": "direct_long_horizon_admission",
            "chat_scope": authority,
            "project_id": str(context["project_id"]),
            "chat_id": str(context["chat_id"]),
            "lead_id": str(context["lead_id"]),
            "project_root_fingerprint_sha256": hashlib.sha256(
                os.path.normcase(str(Path(context["project_root"]).resolve())).encode("utf-8")
            ).hexdigest(),
        }
        contract["fingerprint_sha256"] = hashlib.sha256(json.dumps(
            contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        payload_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        bound_digest = str(admission_digest or "").lower()
        if len(bound_digest) != 64 or any(
            one not in "0123456789abcdef" for one in bound_digest
        ):
            raise HarnessError(
                "Direct project admission requires an exact goal admission binding"
            )
        admission_binding = {
            "schema_version": DIRECT_LONG_HORIZON_ADMISSION_BINDING_SCHEMA_VERSION,
            "admission_digest": bound_digest,
        }
        with ProjectTransactionLock(self.config.project_root).held(30.0):
            if path.exists():
                existing = self._read_direct_admission_path(path)
                if str(existing.get("state") or "pending") != "pending":
                    raise HarnessError(
                        "That direct project request identity was explicitly retired. "
                        "Start new work with a new request identity."
                    )
                if str(existing.get("payload_sha256") or "") != payload_sha256 \
                        or existing.get("execution_contract") != contract:
                    raise HarnessError(
                        "That direct project request identity is already bound to a "
                        "different exact payload or execution contract."
                    )
                existing_digest = self._direct_admission_binding_digest(existing)
                if existing_digest and not hmac.compare_digest(
                    existing_digest, bound_digest,
                ):
                    raise HarnessError(
                        "That saved direct project request is bound to a different "
                        "full goal admission contract."
                    )
                if not existing_digest:
                    now = int(time.time() * 1000)
                    upgraded_unsigned = {
                        **self._direct_admission_unsigned(existing),
                        "admission_binding": admission_binding,
                        "updated_ms": now,
                    }
                    existing = {
                        **upgraded_unsigned,
                        "integrity_mac": mac(
                            "direct-long-horizon-admission-v1", upgraded_unsigned,
                        ),
                    }
                    atomic_text(path, json.dumps(
                        existing, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"),
                    ) + "\n")
                return existing
            records = [one for one in folder.iterdir() if one.suffix == ".json"]
            pending: list[Path] = []
            tombstones = 0
            for held_path in records:
                held = self._read_direct_admission_path(held_path)
                if str(held.get("state") or "pending") != "pending":
                    tombstones += 1
                    if self._direct_terminal_client_state(held) != "consumed" \
                            and str(held.get("chat_id") or "") == str(
                                payload["chat_id"]
                            ):
                        raise HarnessError(
                            "This exact chat has an unconsumed terminal project "
                            "request. A client must acknowledge that outcome before "
                            "starting another one."
                        )
                    continue
                pending.append(held_path)
                held_payload = held.get("payload") \
                    if isinstance(held.get("payload"), dict) else {}
                if str(held_payload.get("chat_id") or "") == str(payload["chat_id"]):
                    raise HarnessError(
                        "This exact chat already has a saved direct project request. "
                        "Reconcile it before starting another one."
                    )
            if tombstones + len(pending) + 1 > DIRECT_LONG_HORIZON_ADMISSION_MAX_TOMBSTONES:
                raise HarnessError(
                    "The bounded retired-request guard cannot reserve safe replay "
                    "tombstones for another direct project request."
                )
            if len(pending) >= DIRECT_LONG_HORIZON_ADMISSION_MAX_PENDING:
                raise HarnessError(
                    "Reconcile an earlier saved direct project request before starting "
                    "another one."
                )
            now = int(time.time() * 1000)
            unsigned = {
                "schema_version": DIRECT_LONG_HORIZON_ADMISSION_SCHEMA_VERSION,
                "state": "pending",
                "request_id": str(payload["request_id"]),
                "payload": payload,
                "payload_sha256": payload_sha256,
                "admission_binding": admission_binding,
                "execution_contract": contract,
                "created_ms": now,
                "updated_ms": now,
            }
            record = {
                **unsigned,
                "integrity_mac": mac("direct-long-horizon-admission-v1", unsigned),
            }
            atomic_text(path, json.dumps(
                record, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ) + "\n")
            return record

    def _load_direct_admission(
        self, request_id: str, chat_id: str = "",
    ) -> dict[str, Any]:
        from .safety import ProjectTransactionLock

        request = str(request_id or "").strip()
        if not request or len(request) > 160:
            raise HarnessError("Choose an exact saved direct project request")
        path = self._direct_admission_path(request)
        with ProjectTransactionLock(self.config.project_root).held(30.0):
            if not path.exists():
                raise HarnessError(
                    "That saved direct project admission no longer needs recovery. "
                    "Refresh Mission control and the chat transcript."
                )
            record = self._read_direct_admission_path(path)
            if str(record.get("state") or "pending") != "pending":
                raise HarnessError(
                    "That direct project request identity was explicitly retired. "
                    "Start new work with a new request identity."
                )
        if str(record.get("request_id") or "") != request:
            raise HarnessError("The saved direct project request identity does not match")
        payload = record["payload"]
        if chat_id and str(payload.get("chat_id") or "") != str(chat_id):
            raise HarnessError("That saved direct project request belongs to another chat")
        return record

    def _remove_direct_admission(self, request_id: str, payload_sha256: str) -> None:
        from .safety import ProjectTransactionLock

        path = self._direct_admission_path(request_id)
        with ProjectTransactionLock(self.config.project_root).held(30.0):
            if not path.exists():
                return
            record = self._read_direct_admission_path(path)
            if str(record.get("payload_sha256") or "") != str(payload_sha256):
                raise HarnessError(
                    "The saved direct project admission changed before reconciliation"
                )
            path.unlink()

    def _retire_direct_admission(
        self, record: dict[str, Any], state: str, *, goal_id: str = "",
    ) -> None:
        """Replace exact payload bytes with an authenticated replay tombstone."""

        from .safety import ProjectTransactionLock

        if state not in {"discarded", "reconciled"}:
            raise HarnessError("Choose a supported direct admission retirement state")
        request_id = str(record.get("request_id") or "")
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        payload_sha256 = str(record.get("payload_sha256") or "")
        admission_digest = self._direct_admission_binding_digest(record)
        path = self._direct_admission_path(request_id)
        folder, _authority = self._direct_admission_folder()
        with ProjectTransactionLock(self.config.project_root).held(30.0):
            current = self._read_direct_admission_path(path)
            current_state = str(current.get("state") or "pending")
            if current_state in {"discarded", "reconciled"}:
                return
            if str(current.get("payload_sha256") or "") != payload_sha256:
                raise HarnessError(
                    "The saved direct project admission changed before retirement"
                )
            current_digest = self._direct_admission_binding_digest(current)
            if current_digest != admission_digest:
                raise HarnessError(
                    "The saved direct project admission binding changed before retirement"
                )
            tombstones = sum(
                1 for candidate in folder.iterdir()
                if candidate.suffix == ".json"
                and str(self._read_direct_admission_path(candidate).get("state") or "pending")
                in {"discarded", "reconciled"}
            )
            if tombstones >= DIRECT_LONG_HORIZON_ADMISSION_MAX_TOMBSTONES:
                raise HarnessError(
                    "The bounded retired-request guard is full. The exact pending "
                    "request was left recoverable instead of deleting replay protection."
                )
            now = int(time.time() * 1000)
            unsigned = {
                "schema_version": DIRECT_LONG_HORIZON_ADMISSION_SCHEMA_VERSION,
                "state": state,
                "request_id": request_id,
                "chat_id": str(payload.get("chat_id") or ""),
                "project_id": str(payload.get("project_id") or ""),
                "lead_id": str(payload.get("lead_id") or ""),
                "payload_sha256": payload_sha256,
                "intent_sha256": chat_lab.long_horizon_intent_sha256(
                    str(payload.get("chat_id") or ""),
                    str(payload.get("project_id") or ""),
                    str(payload.get("lead_id") or ""),
                    str(payload.get("text") or ""), payload.get("attachments"),
                ),
                "execution_contract": dict(record.get("execution_contract") or {}),
                "created_ms": int(record.get("created_ms") or now),
                "retired_ms": now,
                "goal_id": str(goal_id or ""),
                "terminal_receipt_schema_version": (
                    DIRECT_LONG_HORIZON_TERMINAL_RECEIPT_SCHEMA_VERSION
                ),
                # This terminal replay fence remains discoverable until a
                # client first verifies the exact outcome and clears its own
                # browser marker or desktop outbox. Consumption later hides,
                # but never deletes, the fence.
                "client_consumed": False,
            }
            if admission_digest:
                unsigned["admission_binding"] = {
                    "schema_version": DIRECT_LONG_HORIZON_ADMISSION_BINDING_SCHEMA_VERSION,
                    "admission_digest": admission_digest,
                }
            retired = {
                **unsigned,
                "integrity_mac": mac("direct-long-horizon-admission-v1", unsigned),
            }
            atomic_text(path, json.dumps(
                retired, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ) + "\n")

    def _retire_missing_direct_admission(
        self, *, request_id: str, chat_id: str, project_id: str,
        lead_id: str, intent_sha256: str, state: str = "discarded",
        goal_id: str = "",
    ) -> None:
        """Fence a desktop-outbox request after absence of both journal and goal."""

        from .safety import ProjectTransactionLock

        if state not in {"discarded", "reconciled"}:
            raise HarnessError("Choose a supported direct admission retirement state")
        digest = str(intent_sha256 or "").lower()
        if len(digest) != 64 or any(one not in "0123456789abcdef" for one in digest):
            raise HarnessError(
                "The exact desktop outbox digest is required before a local-only "
                "goal request can be discarded."
            )
        path = self._direct_admission_path(request_id)
        folder, authority = self._direct_admission_folder()
        with ProjectTransactionLock(self.config.project_root).held(30.0):
            if path.exists():
                existing = self._read_direct_admission_path(path)
                if str(existing.get("state") or "pending") in {"discarded", "reconciled"}:
                    return
                raise HarnessError(
                    "A backend admission appeared before local-only discard completed"
                )
            tombstones = sum(
                1 for candidate in folder.iterdir()
                if candidate.suffix == ".json"
                and str(self._read_direct_admission_path(candidate).get("state") or "pending")
                in {"discarded", "reconciled"}
            )
            if tombstones >= DIRECT_LONG_HORIZON_ADMISSION_MAX_TOMBSTONES:
                raise HarnessError(
                    "The bounded retired-request guard is full. The desktop outbox "
                    "was kept instead of deleting replay protection."
                )
            now = int(time.time() * 1000)
            contract = {
                "schema_version": DIRECT_LONG_HORIZON_ADMISSION_SCHEMA_VERSION,
                "kind": "desktop_direct_long_horizon_outbox",
                "chat_scope": authority,
                "project_id": str(project_id or ""),
                "chat_id": str(chat_id or ""),
                "lead_id": str(lead_id or ""),
            }
            contract["fingerprint_sha256"] = hashlib.sha256(json.dumps(
                contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            unsigned = {
                "schema_version": DIRECT_LONG_HORIZON_ADMISSION_SCHEMA_VERSION,
                "state": state,
                "request_id": request_id,
                "chat_id": chat_id,
                "project_id": str(project_id or ""),
                "lead_id": str(lead_id or ""),
                "payload_sha256": digest,
                "intent_sha256": digest,
                "execution_contract": contract,
                "created_ms": now,
                "retired_ms": now,
                "goal_id": str(goal_id or ""),
                "terminal_receipt_schema_version": (
                    DIRECT_LONG_HORIZON_TERMINAL_RECEIPT_SCHEMA_VERSION
                ),
                "client_consumed": False,
            }
            retired = {
                **unsigned,
                "integrity_mac": mac("direct-long-horizon-admission-v1", unsigned),
            }
            atomic_text(path, json.dumps(
                retired, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ) + "\n")

    def _verify_direct_goal_intent_binding(
        self, goal: dict[str, Any], request_id: str, intent_sha256: str,
    ) -> dict[str, Any]:
        """Prove an existing goal came from the exact desktop outbox payload."""

        expected = str(intent_sha256 or "").lower()
        if len(expected) != 64 or any(one not in "0123456789abcdef" for one in expected):
            raise HarnessError(
                "The exact desktop outbox digest is required to reconcile an existing goal"
            )
        standing = self.swarm_standing()
        conversation = self._long_horizon_chat(
            self.config, standing["board"], goal,
        )
        if conversation is None:
            raise HarnessError(
                "The existing goal's exact saved chat binding could not be verified"
            )
        found = [
            one for one in chat_lab.read_it(
                self.config, str(conversation.get("transcript_route") or ""),
                str(conversation.get("filed_as") or ""),
            )
            if isinstance(getattr(one, "correlation", None), dict)
            and str(one.correlation.get("kind") or "") == "long_horizon_prompt"
            and str(one.correlation.get("request_id") or "") == request_id
            and str(one.correlation.get("chat_id") or "")
            == str(goal.get("conversation_id") or "")
        ]
        if not found or any(
            str(one.correlation.get("intent_sha256") or "") != expected
            for one in found
        ):
            raise HarnessError(
                "The existing goal is bound to a different exact saved prompt digest"
            )
        identities = {
            (
                str(one.correlation.get("request_id") or ""),
                str(one.correlation.get("chat_id") or ""),
                str(one.correlation.get("project_id") or ""),
                str(one.correlation.get("lead_id") or ""),
                str(one.correlation.get("intent_sha256") or "").lower(),
            )
            for one in found
        }
        if len(identities) != 1:
            raise HarnessError(
                "The existing goal has conflicting saved prompt receipt identities"
            )
        saved_request, saved_chat, saved_project, saved_lead, saved_intent = \
            next(iter(identities))
        if not all((saved_request, saved_chat, saved_project, saved_lead)):
            raise HarnessError("The existing goal's saved prompt receipt is incomplete")
        return {
            "schema_version": DIRECT_LONG_HORIZON_RECEIPT_SCHEMA_VERSION,
            "request_id": saved_request,
            "chat_id": saved_chat,
            "project_id": saved_project,
            "lead_id": saved_lead,
            "intent_sha256": saved_intent,
        }

    def _note_discarded_direct_admission(
        self, record: dict[str, Any], request_id: str,
    ) -> bool:
        payload = record.get("payload") \
            if isinstance(record.get("payload"), dict) else {}
        try:
            standing = self.swarm_standing()
            context = self._direct_long_horizon_context(standing, payload)
            intent_sha256 = chat_lab.long_horizon_intent_sha256(
                context["chat_id"], context["project_id"], context["lead_id"],
                str(payload["text"]), payload.get("attachments"),
            )
            chat_lab.keep_long_horizon_error(
                self.config, context["transcript_route"],
                "This saved project goal request was explicitly discarded before "
                "goal admission. This discard did not start provider work.",
                filed_as=context["filed_as"], request_id=request_id,
                chat_id=context["chat_id"], project_id=context["project_id"],
                lead_id=context["lead_id"], intent_sha256=intent_sha256,
            )
            return True
        except HarnessError:
            # The board/chat may have been removed while the authenticated
            # journal remained. Discard never invents a replacement identity.
            return False

    def direct_admission_goal(self, supplied: dict[str, Any]) -> dict[str, Any]:
        """Look up one exact direct request without bounded-history loss.

        This is the read-only authority used after a reconciliation response
        may have been lost. ``get_by_request`` includes compact request
        tombstones, unlike the bounded active/recent goal inventory.
        """

        receipt = {
            "schema_version": DIRECT_LONG_HORIZON_RECEIPT_SCHEMA_VERSION,
            "request_id": str(supplied.get("request_id") or "").strip(),
            "chat_id": str(supplied.get("chat_id") or "").strip(),
            "project_id": str(supplied.get("project_id") or "").strip(),
            "lead_id": str(supplied.get("lead_id") or "").strip(),
            "intent_sha256": str(supplied.get("intent_sha256") or "").strip().lower(),
        }
        if not all(receipt.values()) or len(receipt["request_id"]) > 160 \
                or len(receipt["chat_id"]) > 256 \
                or len(receipt["project_id"]) > 512 \
                or len(receipt["lead_id"]) > 256:
            raise HarnessError("Choose one exact direct project request identity")
        if len(receipt["intent_sha256"]) != 64 or any(
            one not in "0123456789abcdef" for one in receipt["intent_sha256"]
        ):
            raise HarnessError("Choose the exact direct project request intent digest")
        with self.project_admission_lock, self.swarm_lock:
            with self._direct_admission_turn(
                receipt["request_id"], receipt["chat_id"], timeout=30.0,
            ):
                existing = self.long_horizon.store.get_by_request(
                    receipt["request_id"],
                )
                if existing is None:
                    # Validate that the claimed live board/chat identity is
                    # itself exact before returning authoritative absence.
                    self._direct_long_horizon_context(self.swarm_standing(), {
                        "project_id": receipt["project_id"],
                        "lead_id": receipt["lead_id"],
                        "chat_id": receipt["chat_id"],
                    })
                    return {**receipt, "found": False}

                path = self._direct_admission_path(receipt["request_id"])
                binding_digest = ""
                transcript_proven = False
                if path.exists():
                    record = self._read_direct_admission_path(path)
                    saved_receipt = self._direct_admission_receipt(record)
                    for field in (
                        "request_id", "chat_id", "project_id", "lead_id",
                        "intent_sha256",
                    ):
                        if not hmac.compare_digest(
                            str(saved_receipt[field]), str(receipt[field]),
                        ):
                            raise HarnessError(
                                "The exact direct request lookup is bound to a "
                                f"different {field.replace('_', ' ')}"
                            )
                    binding_digest = self._direct_admission_binding_digest(record)
                    goal_digest = str(existing.get("admission_digest") or "").lower()
                    if binding_digest:
                        if len(goal_digest) != 64 or any(
                            one not in "0123456789abcdef" for one in goal_digest
                        ) or not hmac.compare_digest(binding_digest, goal_digest):
                            raise HarnessError(
                                "The exact direct request lookup found a different "
                                "full goal admission binding"
                            )
                    else:
                        saved_receipt = self._verify_direct_goal_intent_binding(
                            existing, receipt["request_id"], receipt["intent_sha256"],
                        )
                        transcript_proven = True
                else:
                    saved_receipt = self._verify_direct_goal_intent_binding(
                        existing, receipt["request_id"], receipt["intent_sha256"],
                    )
                    transcript_proven = True
                public_goal = self._goal_for_direct_receipt(
                    existing, saved_receipt,
                    admission_digest=binding_digest,
                    transcript_proven=transcript_proven,
                )
        return {**saved_receipt, "found": True, "goal": public_goal}

    def discard_direct_admission(
        self, supplied: dict[str, Any],
    ) -> dict[str, Any]:
        """Discard one exact unadmitted journal, never an already-created goal.

        The request id is a provider-dispatch idempotency key.  A renderer can
        lose the response after ``start`` has durably created a goal, so a
        discard must first prove that this authority has no goal for that key.
        If it does, only an exact full binding may reconcile and return that
        canonical goal. A provably different pending journal is retired as an
        unadmitted attempt without changing or claiming the older goal.
        """

        request_id = str(supplied.get("request_id") or "").strip()
        exact_chat_id = str(supplied.get("chat_id") or "").strip()
        if not request_id or len(request_id) > 160 or not exact_chat_id:
            raise HarnessError("Choose one exact saved direct project request and chat")
        payload_sha256 = str(supplied.get("payload_sha256") or "").strip()
        outbox_intent_sha256 = str(supplied.get("intent_sha256") or "").strip()
        project_id = str(supplied.get("project_id") or "")
        lead_id = str(supplied.get("lead_id") or "")
        with self.project_admission_lock, self.swarm_lock:
            with self._direct_admission_turn(
                request_id, exact_chat_id, timeout=30.0,
            ):
                path = self._direct_admission_path(request_id)
                record: dict[str, Any] | None = None
                held_record: dict[str, Any] | None = None
                terminal_state = ""
                if path.exists():
                    # Integrity failures are not absence. Fail closed so a
                    # corrupt exact journal cannot be bypassed by deleting a
                    # similarly named desktop record.
                    held = self._read_direct_admission_path(path)
                    held_record = held
                    terminal_state = str(held.get("state") or "pending")
                    held_chat_id = str(
                        (held.get("payload") or {}).get("chat_id")
                        if isinstance(held.get("payload"), dict)
                        else held.get("chat_id") or ""
                    )
                    if held_chat_id != exact_chat_id:
                        raise HarnessError(
                            "That saved direct project request belongs to another chat"
                        )
                    if terminal_state in {"discarded", "reconciled"} \
                            and outbox_intent_sha256 \
                            and str(held.get("intent_sha256") or "") \
                            != outbox_intent_sha256:
                        raise HarnessError(
                            "That retired request identity is bound to a different "
                            "exact desktop outbox digest"
                        )
                    if terminal_state == "pending":
                        record = held
                        payload = held["payload"]
                        if str(payload.get("chat_id") or "") != exact_chat_id:
                            raise HarnessError(
                                "That saved direct project request belongs to another chat"
                            )
                        if payload_sha256 and payload_sha256 != str(
                            held.get("payload_sha256") or ""
                        ):
                            raise HarnessError(
                                "The saved direct project admission changed before it "
                                "could be discarded"
                            )
                    elif terminal_state not in {"discarded", "reconciled"}:
                        raise HarnessError(
                            "The saved direct project request has an unsupported state"
                        )

                existing = self.long_horizon.store.get_by_request(request_id)
                if existing is not None:
                    expected_intent = outbox_intent_sha256
                    receipt: dict[str, Any]
                    binding_digest = ""
                    transcript_proven = False
                    if record is not None:
                        receipt = self._direct_admission_receipt(record)
                        record_payload = record["payload"]
                        recorded_intent = chat_lab.long_horizon_intent_sha256(
                            str(record_payload.get("chat_id") or ""),
                            str(record_payload.get("project_id") or ""),
                            str(record_payload.get("lead_id") or ""),
                            str(record_payload.get("text") or ""),
                            record_payload.get("attachments"),
                        )
                        if expected_intent and expected_intent != recorded_intent:
                            raise HarnessError(
                                "The desktop outbox and backend journal contain different "
                                "exact prompt digests"
                            )
                        expected_intent = recorded_intent

                        journal_digest = self._direct_admission_binding_digest(record)
                        goal_digest = str(existing.get("admission_digest") or "").lower()
                        if not journal_digest:
                            raise HarnessError(
                                "The saved direct project request predates full goal "
                                "binding and cannot be safely reconciled or discarded."
                            )
                        if len(goal_digest) != 64 or any(
                            one not in "0123456789abcdef" for one in goal_digest
                        ):
                            raise HarnessError(
                                "The existing goal has no verifiable full admission binding."
                            )
                        if not hmac.compare_digest(journal_digest, goal_digest):
                            transcript_noted = self._note_discarded_direct_admission(
                                record, request_id,
                            )
                            self._retire_direct_admission(record, "discarded")
                            return self._direct_discard_response(
                                receipt, discarded=True, reconciled=False,
                                safe_to_delete=True,
                                transcript_noted=transcript_noted,
                            )
                        if str(existing.get("conversation_id") or "") != exact_chat_id:
                            raise HarnessError(
                                "The exact full admission binding disagrees with the "
                                "existing goal's saved chat identity."
                            )
                        binding_digest = journal_digest
                    else:
                        terminal_digest = self._direct_admission_binding_digest(
                            held_record or {},
                        )
                        goal_digest = str(existing.get("admission_digest") or "").lower()
                        if terminal_digest:
                            if len(goal_digest) != 64:
                                raise HarnessError(
                                    "The existing goal has no verifiable full admission binding."
                                )
                            if not hmac.compare_digest(terminal_digest, goal_digest):
                                if terminal_state == "discarded":
                                    return self._direct_discard_response(
                                        self._direct_admission_receipt(held_record or {}),
                                        discarded=True, reconciled=False,
                                        safe_to_delete=True,
                                    )
                                raise HarnessError(
                                    "The retired direct request and existing goal have "
                                    "different full admission bindings."
                                )
                            receipt = self._direct_admission_receipt(held_record or {})
                            binding_digest = terminal_digest
                        else:
                            # A local-only desktop record carries no full backend
                            # payload. Keep the narrow legacy recovery path: the
                            # exact prompt digest must be proven by the canonical
                            # saved transcript before the old goal can be claimed.
                            receipt = self._verify_direct_goal_intent_binding(
                                existing, request_id, expected_intent,
                            )
                            transcript_proven = True
                        if str(existing.get("conversation_id") or "") != exact_chat_id:
                            raise HarnessError(
                                "That request identity already belongs to a goal from another chat"
                            )
                    public_goal = self._goal_for_direct_receipt(
                        existing, receipt, admission_digest=binding_digest,
                        transcript_proven=transcript_proven,
                    )
                    if record is not None:
                        self._retire_direct_admission(
                            record, "reconciled",
                            goal_id=str(existing.get("goal_id") or ""),
                        )
                    if record is None and not terminal_state:
                        self._retire_missing_direct_admission(
                            request_id=str(receipt["request_id"]),
                            chat_id=str(receipt["chat_id"]),
                            project_id=str(receipt["project_id"]),
                            lead_id=str(receipt["lead_id"]),
                            intent_sha256=str(receipt["intent_sha256"]),
                            state="reconciled",
                            goal_id=str(existing.get("goal_id") or ""),
                        )
                    return self._direct_discard_response(
                        receipt, discarded=False, reconciled=True,
                        safe_to_delete=False, goal=public_goal,
                    )

                if terminal_state in {"discarded", "reconciled"}:
                    return self._direct_discard_response(
                        self._direct_admission_receipt(held_record or {}),
                        discarded=terminal_state == "discarded",
                        reconciled=False, safe_to_delete=True,
                    )

                if record is None:
                    standing = self.swarm_standing()
                    context = self._direct_long_horizon_context(standing, {
                        "project_id": project_id, "lead_id": lead_id,
                        "chat_id": exact_chat_id,
                    })
                    self._retire_missing_direct_admission(
                        request_id=request_id, chat_id=exact_chat_id,
                        project_id=context["project_id"], lead_id=context["lead_id"],
                        intent_sha256=outbox_intent_sha256,
                    )
                    retired = self._read_direct_admission_path(path)
                    return self._direct_discard_response(
                        self._direct_admission_receipt(retired),
                        discarded=True, reconciled=False, safe_to_delete=True,
                    )

                receipt = self._direct_admission_receipt(record)
                transcript_noted = self._note_discarded_direct_admission(
                    record, request_id,
                )
                self._retire_direct_admission(record, "discarded")
        return self._direct_discard_response(
            receipt, discarded=True, reconciled=False, safe_to_delete=True,
            transcript_noted=transcript_noted,
        )

    def acknowledge_direct_admission(
        self, supplied: dict[str, Any],
    ) -> dict[str, Any]:
        """Consume one exact terminal receipt without removing its replay fence."""

        expected = {
            "schema_version": DIRECT_LONG_HORIZON_RECEIPT_SCHEMA_VERSION,
            "request_id": str(supplied.get("request_id") or "").strip(),
            "chat_id": str(supplied.get("chat_id") or "").strip(),
            "project_id": str(supplied.get("project_id") or "").strip(),
            "lead_id": str(supplied.get("lead_id") or "").strip(),
            "intent_sha256": str(
                supplied.get("intent_sha256") or ""
            ).strip().lower(),
        }
        terminal_state = str(supplied.get("terminal_state") or "").strip()
        exact_goal_id = str(supplied.get("goal_id") or "").strip()
        if not all(expected.values()) or len(expected["request_id"]) > 160 \
                or len(expected["chat_id"]) > 256 \
                or len(expected["project_id"]) > 512 \
                or len(expected["lead_id"]) > 256 \
                or terminal_state not in {"discarded", "reconciled"}:
            raise HarnessError("Choose one exact terminal direct project receipt")
        if len(expected["intent_sha256"]) != 64 or any(
            one not in "0123456789abcdef"
            for one in expected["intent_sha256"]
        ):
            raise HarnessError("Choose the exact terminal request intent digest")
        if terminal_state == "reconciled" and not exact_goal_id:
            raise HarnessError("Choose the exact reconciled goal identity")
        if terminal_state == "discarded" and exact_goal_id:
            raise HarnessError("A discarded terminal request cannot name a goal")

        from .safety import ProjectTransactionLock

        with self.project_admission_lock, self.swarm_lock:
            with self._direct_admission_turn(
                expected["request_id"], expected["chat_id"], timeout=30.0,
            ):
                path = self._direct_admission_path(expected["request_id"])
                if not path.exists():
                    raise HarnessError("That terminal direct request receipt is unavailable")
                with ProjectTransactionLock(self.config.project_root).held(30.0):
                    record = self._read_direct_admission_path(path)
                    state = str(record.get("state") or "pending")
                    if state != terminal_state:
                        raise HarnessError(
                            "The terminal direct request has a different exact outcome"
                        )
                    receipt = self._direct_admission_receipt(record)
                    for field in (
                        "request_id", "chat_id", "project_id", "lead_id",
                        "intent_sha256",
                    ):
                        if not hmac.compare_digest(
                            str(receipt[field]), str(expected[field]),
                        ):
                            raise HarnessError(
                                "The terminal direct request acknowledgement is bound "
                                f"to a different {field.replace('_', ' ')}"
                            )
                    recorded_goal_id = str(record.get("goal_id") or "")
                    if not hmac.compare_digest(recorded_goal_id, exact_goal_id):
                        raise HarnessError(
                            "The terminal direct request has a different exact goal"
                        )
                    public_goal = None
                    if state == "reconciled":
                        existing = self.long_horizon.store.get_by_request(
                            expected["request_id"],
                        )
                        if existing is None:
                            raise HarnessError(
                                "The reconciled terminal request has no canonical goal"
                            )
                        binding_digest = self._direct_admission_binding_digest(record)
                        public_goal = self._goal_for_direct_receipt(
                            existing, receipt, admission_digest=binding_digest,
                            transcript_proven=not bool(binding_digest),
                        )
                        if str(public_goal.get("goal_id") or "") != exact_goal_id:
                            raise HarnessError(
                                "The reconciled terminal receipt changed canonical goal"
                            )
                    terminal_client_state = self._direct_terminal_client_state(record)
                    if terminal_client_state != "consumed":
                        unsigned = self._direct_admission_unsigned(record)
                        unsigned["terminal_receipt_schema_version"] = (
                            DIRECT_LONG_HORIZON_TERMINAL_RECEIPT_SCHEMA_VERSION
                        )
                        unsigned["client_consumed"] = True
                        unsigned["client_consumed_ms"] = int(time.time() * 1000)
                        consumed = {
                            **unsigned,
                            "integrity_mac": mac(
                                "direct-long-horizon-admission-v1", unsigned,
                            ),
                        }
                        atomic_text(path, json.dumps(
                            consumed, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"),
                        ) + "\n")
        response = {
            **receipt,
            "terminal_state": terminal_state,
            "goal_id": exact_goal_id,
            "client_consumed": True,
        }
        if public_goal is not None:
            response["goal"] = public_goal
        return response

    def _public_direct_admission(self, record: dict[str, Any]) -> dict[str, Any]:
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        text = str(payload.get("text") or "")
        safe_preview = CredentialRedactor(self.config).text(text)[:500]
        return {
            "schema_version": DIRECT_LONG_HORIZON_ADMISSION_SCHEMA_VERSION,
            "request_id": str(record.get("request_id") or ""),
            "chat_id": str(payload.get("chat_id") or ""),
            "project_id": str(payload.get("project_id") or ""),
            "lead_id": str(payload.get("lead_id") or ""),
            "intent_sha256": chat_lab.long_horizon_intent_sha256(
                str(payload.get("chat_id") or ""),
                str(payload.get("project_id") or ""),
                str(payload.get("lead_id") or ""), text,
                payload.get("attachments"),
            ),
            "text_preview": safe_preview,
            "text_characters": len(text),
            "attachment_count": len(payload.get("attachments") or []),
            "payload_sha256": str(record.get("payload_sha256") or ""),
            "created_ms": int(record.get("created_ms") or 0),
            "execution_contract": dict(record.get("execution_contract") or {}),
        }

    def _public_terminal_direct_admission(
        self, record: dict[str, Any],
    ) -> dict[str, Any]:
        receipt = self._direct_admission_receipt(record)
        state = str(record.get("state") or "")
        client_state = self._direct_terminal_client_state(record)
        if state not in {"discarded", "reconciled"} \
                or client_state == "consumed":
            raise HarnessError("Choose one unconsumed terminal direct request")
        return {
            **receipt,
            "terminal_receipt_schema_version": (
                DIRECT_LONG_HORIZON_TERMINAL_RECEIPT_SCHEMA_VERSION
            ),
            "terminal_state": state,
            "goal_id": str(record.get("goal_id") or ""),
            "client_consumed": False,
            "legacy_terminal": client_state == "legacy_unconsumed",
            "payload_sha256": str(record.get("payload_sha256") or ""),
            "text_preview": "",
            "text_characters": 0,
            "attachment_count": 0,
            "created_ms": int(record.get("created_ms") or 0),
            "execution_contract": dict(record.get("execution_contract") or {}),
        }

    def direct_admission_inventory(self) -> list[dict[str, Any]]:
        from .safety import ProjectTransactionLock

        folder, _authority = self._direct_admission_folder()
        with ProjectTransactionLock(self.config.project_root).held(30.0):
            records = [
                self._read_direct_admission_path(path)
                for path in folder.iterdir() if path.suffix == ".json"
            ]
        records = [
            one for one in records
            if str(one.get("state") or "pending") == "pending"
        ]
        records.sort(key=lambda one: int(one.get("created_ms") or 0))
        return [self._public_direct_admission(one) for one in records]

    @classmethod
    def _unconsumed_terminal_direct_admissions(
        cls, records: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Return one newest unconsumed row per chat.

        Old releases could create several authenticated terminal rows for one
        chat because they had no client-acknowledgement fence. Present them in
        reverse retirement order, one at a time, so a surviving desktop outbox
        or browser marker is most likely to meet its exact backend row first.
        The renderer's one-recovery-per-chat contract stays exact while every
        legacy outcome still requires acknowledgement.
        """

        selected: list[dict[str, Any]] = []
        seen_chats: set[str] = set()
        for record in sorted(
            records, key=lambda one: (
                -int(one.get("retired_ms") or 0),
                -int(one.get("created_ms") or 0),
                str(one.get("request_id") or ""),
            ),
        ):
            if str(record.get("state") or "pending") \
                    not in {"discarded", "reconciled"} \
                    or cls._direct_terminal_client_state(record) == "consumed":
                continue
            chat_id = str(record.get("chat_id") or "")
            if chat_id in seen_chats:
                continue
            seen_chats.add(chat_id)
            selected.append(record)
        return selected

    def terminal_direct_admission_inventory(self) -> list[dict[str, Any]]:
        """Return current and authenticated legacy outcomes needing acknowledgement."""

        from .safety import ProjectTransactionLock

        folder, _authority = self._direct_admission_folder()
        with ProjectTransactionLock(self.config.project_root).held(30.0):
            records = [
                self._read_direct_admission_path(path)
                for path in folder.iterdir() if path.suffix == ".json"
            ]
        records = self._unconsumed_terminal_direct_admissions(records)
        return [self._public_terminal_direct_admission(one) for one in records]

    def direct_admission_recovery_inventory(self) -> dict[str, list[dict[str, Any]]]:
        """Snapshot pending and unconsumed terminal phases under one file lock."""

        from .safety import ProjectTransactionLock

        folder, _authority = self._direct_admission_folder()
        with ProjectTransactionLock(self.config.project_root).held(30.0):
            records = [
                self._read_direct_admission_path(path)
                for path in folder.iterdir() if path.suffix == ".json"
            ]
        records.sort(key=lambda one: int(one.get("created_ms") or 0))
        pending = [
            self._public_direct_admission(one) for one in records
            if str(one.get("state") or "pending") == "pending"
        ]
        pending_chat_ids = {
            str(one.get("chat_id") or "") for one in pending
        }
        terminal = [
            self._public_terminal_direct_admission(one)
            for one in self._unconsumed_terminal_direct_admissions(records)
            if str(one.get("chat_id") or "") not in pending_chat_ids
        ]
        return {"pending": pending, "terminal": terminal}

    def prepare_direct_long_horizon(
        self, supplied: dict[str, Any],
    ) -> dict[str, Any]:
        payload = self._canonical_direct_long_horizon_payload(supplied)
        with self.project_admission_lock, self.swarm_lock:
            standing = self.swarm_standing()
            context = self._direct_long_horizon_context(standing, payload)
            intent_sha256 = chat_lab.long_horizon_intent_sha256(
                context["chat_id"], context["project_id"], context["lead_id"],
                str(payload["text"]), payload.get("attachments"),
            )
            with self._direct_admission_turn(
                str(payload["request_id"]), context["chat_id"], timeout=30.0,
            ):
                preflight = self._preflight_direct_long_horizon(
                    standing, payload, context,
                )
                record = self._save_direct_admission(
                    payload, context, str(preflight["admission_digest"]),
                )
                chat_lab.keep_long_horizon_prompt(
                    self.config, context["transcript_route"], str(payload["text"]),
                    filed_as=context["filed_as"], request_id=str(payload["request_id"]),
                    chat_id=context["chat_id"], project_id=context["project_id"],
                    lead_id=context["lead_id"], intent_sha256=intent_sha256,
                    attachments=payload.get("attachments"),
                )
        return self._public_direct_admission(record)

    def admit_direct_long_horizon(
        self, supplied: dict[str, Any], *, from_pending: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if from_pending:
            initial = self._load_direct_admission(
                str(supplied.get("request_id") or ""),
                str(supplied.get("chat_id") or ""),
            )
            payload = initial["payload"]
        else:
            payload = self._canonical_direct_long_horizon_payload(supplied)
        with self.project_admission_lock, self.swarm_lock:
            standing = self.swarm_standing()
            context = self._direct_long_horizon_context(standing, payload)
            with self._direct_admission_turn(
                str(payload["request_id"]), context["chat_id"], timeout=30.0,
            ):
                if from_pending:
                    # Re-read only after the cross-process request+chat leases. A
                    # concurrent explicit discard may have replaced the
                    # payload with a replay tombstone; never recreate it from
                    # the stale pre-lease copy.
                    record = self._load_direct_admission(
                        str(payload["request_id"]), str(payload["chat_id"]),
                    )
                    payload = record["payload"]
                    standing = self.swarm_standing()
                    context = self._direct_long_horizon_context(standing, payload)
                intent_sha256 = chat_lab.long_horizon_intent_sha256(
                    context["chat_id"], context["project_id"], context["lead_id"],
                    str(payload["text"]), payload.get("attachments"),
                )
                preflight = self._preflight_direct_long_horizon(
                    standing, payload, context,
                )
                record = self._save_direct_admission(
                    payload, context, str(preflight["admission_digest"]),
                )
                chat_lab.keep_long_horizon_prompt(
                    self.config, context["transcript_route"], str(payload["text"]),
                    filed_as=context["filed_as"], request_id=str(payload["request_id"]),
                    chat_id=context["chat_id"], project_id=context["project_id"],
                    lead_id=context["lead_id"], intent_sha256=intent_sha256,
                    attachments=payload.get("attachments"),
                )
                try:
                    self.long_horizon.store.validate_create(
                        standing["board"], context["project_id"],
                        list(payload["objectives"]), str(payload["request_id"]),
                        success_criteria=payload.get("success_criteria"),
                        policy=payload.get("policy"),
                        participant_ids=context["participant_ids"],
                    )
                    admitted_authority = self.require_project_execution_authority(
                        context["project_root"]
                    )
                    goal = self.long_horizon.start(
                        standing["board"], context["project_id"],
                        list(payload["objectives"]), str(payload["request_id"]),
                        lead_id=context["lead_id"],
                        success_criteria=payload.get("success_criteria"),
                        policy=payload.get("policy"),
                        attachments=payload.get("attachments"),
                        participant_ids=context["participant_ids"],
                        conversation_id=context["chat_id"],
                        expected_project_authority_id=str(
                            admitted_authority.get("project_authority_id") or ""
                        ),
                    )
                except Exception as exc:
                    try:
                        chat_lab.keep_long_horizon_error(
                            self.config, context["transcript_route"], str(exc),
                            filed_as=context["filed_as"],
                            request_id=str(payload["request_id"]),
                            chat_id=context["chat_id"],
                            project_id=context["project_id"],
                            lead_id=context["lead_id"], intent_sha256=intent_sha256,
                        )
                    except Exception:
                        pass
                    raise
                receipt = self._direct_admission_receipt(record)
                goal = self._goal_for_direct_receipt(
                    goal, receipt,
                    admission_digest=self._direct_admission_binding_digest(record),
                )
                chat_lab.keep_long_horizon_status(
                    self.config, context["transcript_route"], goal,
                    filed_as=context["filed_as"], chat_id=context["chat_id"],
                    project_id=context["project_id"], lead_id=context["lead_id"],
                    intent_sha256=intent_sha256,
                )
                # Starting the goal is not proof that the renderer received
                # this receipt. Keep the authenticated admission journal
                # pending until the renderer verifies this exact start and
                # explicitly reconciles it through discard_direct_admission.
                # A response lost after this point can then be recovered from
                # another browser origin without redispatching the goal.
        return goal, receipt

    @staticmethod
    def _project_paths_overlap(one: Path, other: Path) -> bool:
        left = one.resolve()
        right = other.resolve()
        return left == right or left in right.parents or right in left.parents

    @staticmethod
    def _board_project_path(board: dict[str, Any], project_id: str) -> Path:
        project = next((
            one for one in board.get("projects", []) if isinstance(one, dict)
            and str(one.get("id") or "") == str(project_id or "")
        ), None)
        if project is None or project.get("is_there") is not True:
            raise HarnessError("Choose an available project before starting project work")
        return Path(str(project.get("path") or "")).resolve(strict=True)

    def legacy_project_conflicts(self, root: Path) -> list[str]:
        """Read shared legacy ownership before long-horizon admission."""
        conflicts: list[str] = []
        for active in self.swarm_runs.active_runs():
            snapshot = active.get("snapshot") if isinstance(active.get("snapshot"), dict) else {}
            if str(snapshot.get("kind") or "") == "board_order":
                for project in (snapshot.get("board") or {}).get("projects", []):
                    if not isinstance(project, dict) or not str(project.get("path") or ""):
                        continue
                    legacy_root = Path(str(project["path"])).resolve(strict=True)
                    if self._project_paths_overlap(root, legacy_root):
                        conflicts.append(f"legacy-board-run:{active.get('run_id')}")
                continue
            selected_mode = str(snapshot.get("selected_mode") or snapshot.get("requested_mode") or "")
            conversation = snapshot.get("conversation") if isinstance(snapshot.get("conversation"), dict) else {}
            project_id = str(snapshot.get("project_id") or conversation.get("project") or "")
            if selected_mode in {"work", "auto"} and project_id:
                legacy_root = self._board_project_path(snapshot.get("board") or {}, project_id)
                if self._project_paths_overlap(root, legacy_root):
                    conflicts.append(f"legacy-run:{active.get('run_id')}")
        for path in self.swarm_goal_queue.active_project_paths():
            legacy_root = Path(path).resolve(strict=True)
            if self._project_paths_overlap(root, legacy_root):
                conflicts.append("legacy-goal-queue:" + str(legacy_root))
        current_root = self.config.project_root.resolve()
        if self._project_paths_overlap(root, current_root):
            if self.run_lock.locked():
                conflicts.append("workspace-run:" + str(current_root))
            if self.pipeline_running or self.pipeline_lock.locked():
                conflicts.append("pipeline-run:" + str(current_root))
        return conflicts

    def require_no_long_horizon_path(self, root: Path) -> Path:
        root = root.resolve(strict=True)
        if self.long_horizon.store.active_overlapping_project(root):
            raise HarnessError(
                "Long-horizon goal work already owns this project. Cancel or finish it before starting other project work."
            )
        return root

    def require_no_long_horizon_owner(
        self, board: dict[str, Any], project_id: str, *, agent_id: str = "",
    ) -> Path:
        """Fence legacy work against active long-horizon ownership."""
        if not project_id and agent_id:
            assigned = list(dict.fromkeys(
                str(one.get("project") or "")
                for one in board.get("works_on", []) if isinstance(one, dict)
                and str(one.get("agent") or "") == agent_id
                and str(one.get("project") or "")
            ))
            if len(assigned) == 1:
                project_id = assigned[0]
        root = self._board_project_path(board, project_id)
        return self.require_no_long_horizon_path(root)

    def server_close(self) -> None:
        held = self._long_horizon
        if held is not None:
            held.close()
        super().server_close()

    def swarm_goal_queue_status(self) -> dict[str, Any] | None:
        """Reconcile an interrupted HTTP response before exposing its cursor."""

        return self.swarm_goal_queue.reconcile(
            self.swarm_runs.get_by_request_any_authority
        )

    def project_authority_status(self, project_root: Path | None = None) -> dict[str, Any]:
        target = (project_root or self.config.project_root)
        try:
            return pipeline_runtime.inspect_project_authority(target)
        except HarnessError as exc:
            return {
                "can_run": False, "reason": str(exc), "reason_code": "unsafe_or_malformed",
                "repairable": False, "fingerprint": "",
            }

    def require_project_execution_authority(
        self, project_root: Path | None = None,
    ) -> dict[str, Any]:
        target = (project_root or self.config.project_root).resolve(strict=True)
        authority = self.project_authority_status(target)
        if not authority.get("can_run"):
            raise HarnessError(str(authority.get("reason") or "Project execution is paused."))
        # An accepted mutation boundary binds a previously unseen folder to a
        # stable local authority.  Later stages compare this exact ID so a
        # copied descriptor or target substitution cannot inherit permission.
        return {
            **authority,
            "project_authority_id": pipeline_runtime.project_identity(target),
            "project_root": str(target),
        }

    def swarm_change_pause_reason(self) -> str:
        held = self._swarm_runner
        if held is not None:
            reason = held.why_it_cannot_be_changed()
            if reason:
                return reason
        return swarm_runs.global_board_change_pause_reason(self.config)

    def decorate_swarm_authority(self, standing: dict[str, Any]) -> dict[str, Any]:
        authority = self.project_authority_status()
        standing["cannot_run"] = str(authority.get("reason") or "")
        standing["authority"] = authority
        project_authorities: dict[str, Any] = {}
        for project in standing.get("board", {}).get("projects", []):
            if not isinstance(project, dict) or not str(project.get("id") or ""):
                continue
            path = str(project.get("path") or "").strip()
            project_authorities[str(project["id"])] = (
                self.project_authority_status(Path(path)) if path else {
                    "can_run": False,
                    "reason": "The board project has no local folder path.",
                    "reason_code": "missing",
                    "repairable": False,
                    "fingerprint": "",
                }
            )
        standing["project_authorities"] = project_authorities
        standing["cannot_be_changed"] = self.swarm_change_pause_reason()
        return standing

    @staticmethod
    def _pipeline_result(run: Any) -> dict[str, Any]:
        if callable(getattr(run, "to_dict", None)):
            return run.to_dict()
        return {
            "passed": bool(getattr(run, "passed", False)),
            "outcome": "passed" if getattr(run, "passed", False) else "failed",
            "said": str(getattr(run, "said", "")),
            "nodes": [],
        }

    def run_manual_timer(self, name: str, request_id: str = "") -> dict[str, Any]:
        """Run one timer against one immutable project binding."""

        with self.project_admission_lock:
            config = self.config
            root = config.project_root.resolve(strict=True)
            self.require_project_execution_authority(root)
            self.require_no_long_horizon_path(root)
            if not self.pipeline_lock.acquire(blocking=False):
                raise HarnessError("A pipeline is running already. Wait for it, or stop it.")
        run_id = ""
        attempt_id = ""
        try:
            store = self.pipeline_store
            kinds = dict(self.check_kinds)
            with self.pipelines_lock:
                one = timer_lab.load(config, name)
                held = pipeline_lab.load(config, one.automation)
                frozen = pipeline_lab.freeze_definition(config, held)
            accepted, created = store.accept(
                frozen, source=f"timer-manual:{one.name}", request_id=request_id,
            )
            run_id = accepted["run_id"]
            attempt_id = accepted["attempt_id"]
            if not created:
                prior = accepted.get("result")
                if isinstance(prior, dict):
                    return {
                        "run_id": run_id, "replayed": True,
                        "passed": bool(prior.get("passed")),
                        "said": str(prior.get("said") or ""),
                    }
                raise HarnessError(
                    f"Automation run {run_id} is already {accepted['state']}; it was not duplicated."
                )
            self.pipeline_running = True
            self.pipeline_active_run_id = run_id
            store.start(run_id, attempt_id)
            try:
                run = pipeline_lab.run_it(
                    config, held, check_kinds=kinds,
                    stopping=lambda: store.should_stop(run_id),
                    run_id=run_id, frozen=frozen, decision_nonce=attempt_id,
                )
                run_result = self._pipeline_result(run)
                finished = store.finish(run_id, attempt_id, run_result)
            except BaseException as exc:
                store.fail(
                    run_id, attempt_id,
                    f"The manual timer run stopped before completion: {exc}",
                )
                raise
            result = finished.get("result") or run_result
            said = timer_lab.in_safe_words(config, str(result.get("said") or ""))
            with self.pipelines_lock:
                timer_lab.write_down_a_run(
                    config, one, said, bool(result.get("passed")),
                    by_hand=True, run_id=run_id,
                )
            return {"run_id": run_id, "passed": bool(result.get("passed")), "said": said}
        finally:
            self.pipeline_running = False
            if self.pipeline_active_run_id == run_id:
                self.pipeline_active_run_id = ""
            self.pipeline_lock.release()

    def refresh_swarm_provider_status(self) -> bool:
        """Refresh slow machine/provider status without owning the board lock.

        Callers deliberately invoke this before taking ``swarm_lock``. A
        project/configuration switch racing the probe makes its result stale
        instead of letting old-project routes become authoritative for the new
        project.
        """

        with self.authority_lock:
            config = self.config
            config_revision = self._config_revision
        discovered = [
            dict(one) for one in swarm_lab.discover_who_can_be_used(config)
            if not str(one.get("route") or "").startswith("web:")
        ]
        with self.authority_lock:
            if self._config_revision != config_revision or self.config is not config:
                return False
            self._swarm_known_routes = discovered
            self._swarm_known_routes_revision = config_revision
        return True

    def swarm_standing(self) -> dict[str, Any]:
        # Loading the durable board is local and quick. Discovering every AI
        # tool installed on the machine is not: it starts several CLIs and can
        # take seconds. This method is deliberately cache/local-state only, so
        # it remains safe to call while the topology lock is held. Callers that
        # want fresh machine status must invoke refresh_swarm_provider_status
        # before taking that lock.
        provider_status_stale = False
        with self.authority_lock:
            config = self.config
            config_revision = self._config_revision
            known_routes = (
                self._swarm_known_routes
                if self._swarm_known_routes_revision == config_revision else None
            )
        if known_routes is None:
            known_routes = chat_lab.already_set_up(config)
            provider_status_stale = True
        standing = swarm_lab.how_it_stands(
            config, known_routes=known_routes or []
        )
        with self.authority_lock:
            if self._config_revision != config_revision or self.config is not config:
                provider_status_stale = True
        standing["provider_status_stale"] = provider_status_stale
        self.web_chats.decorate_swarm(standing)
        standing["verification_command_approvals"] = [
            swarm_work.verification_command_approval(config, project)
            for project in standing.get("board", {}).get("projects", [])
            if isinstance(project, dict)
        ]
        return standing

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

        wanted = Path(where).expanduser()
        try:
            wanted = wanted.resolve()
        except OSError as exc:
            raise HarnessError(f"That path cannot be read: {exc}") from exc
        if not wanted.is_dir():
            raise HarnessError(
                f"There is no folder at {wanted}. Pick the folder your project is in."
            )
        if not self.project_admission_lock.acquire(blocking=False):
            raise HarnessError(
                "A swarm board or chat command is being accepted, or another project "
                "command is contacting a provider. Wait for it before moving projects."
            )
        if self.pipeline_running:
            self.project_admission_lock.release()
            raise HarnessError(
                "An automation is running. Wait for it to finish, or stop it, "
                "before moving to another project."
            )
        if not self.pipeline_lock.acquire(blocking=False):
            self.project_admission_lock.release()
            raise HarnessError(
                "An automation is being accepted or is running. Wait for it before moving projects."
            )
        if not self.run_lock.acquire(blocking=False):
            self.pipeline_lock.release()
            self.project_admission_lock.release()
            raise HarnessError(
                "A run is going. Wait for it to finish before moving to "
                "another project."
            )
        if not self.swarm_lock.acquire(blocking=False):
            self.run_lock.release()
            self.pipeline_lock.release()
            self.project_admission_lock.release()
            raise HarnessError(
                "A Swarm board or chat command is being accepted. Wait for it before moving projects."
            )
        try:
            # Switching away from a copied/unregistered project is harmless
            # and must not instantiate its execution authority merely to ask
            # whether this process already owns a run.
            if not self.qa_lock.acquire(blocking=False):
                raise HarnessError(
                    "The checks are running. Wait for them to finish before "
                    "moving to another project."
                )
            try:
                with self.authority_lock:
                    active_swarm = (
                        self._swarm_runs.active() if self._swarm_runs is not None else None
                    )
                    active_communication = (
                        self._swarm_communication_runs.active()
                        if self._swarm_communication_runs is not None else None
                    )
                    if active_swarm is not None or active_communication is not None:
                        raise HarnessError(
                            "A Swarm board or chat command is active. Wait for it to finish, "
                            "or stop that exact run, before moving to another project."
                        )
                    if self._swarm_goal_queue is not None \
                            and self._swarm_goal_queue.active_project_paths():
                        raise HarnessError(
                            "Legacy project work is queued or running. Finish or stop that queue before moving projects."
                        )
                self.reload_config(wanted, reset_project_state=True)
            finally:
                self.qa_lock.release()
        finally:
            self.swarm_lock.release()
            self.run_lock.release()
            self.pipeline_lock.release()
            self.project_admission_lock.release()
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

    def handle_one_request(self) -> None:
        """Never let a QA capability leak to the next keep-alive request."""

        swarm_lab._set_board_qa_request_capability("")  # noqa: SLF001
        try:
            super().handle_one_request()
        finally:
            swarm_lab._set_board_qa_request_capability("")  # noqa: SLF001

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

    def _chat_attachment(self, agent_id: str, attachment_id: str) -> None:
        with self.server.swarm_lock:
            board = swarm_lab.load()
        one = swarm_lab.the_agent(board, agent_id)
        path, metadata = chat_lab.attachment_path(
            self.server.config, one.who,
            one.filed_as_name or swarm_lab.filed_as(one.name), attachment_id
        )
        raw = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", str(metadata.get("type") or "application/octet-stream"))
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "private, no-store")
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
        maximum = (
            swarm_lab.MAX_SAVED_BOARD_DOCUMENT_BYTES
            if self.path.startswith("/api/swarm/") else 12_000_000
        )
        if length <= 0 or length > maximum:
            self.close_connection = True
            raise HarnessError(
                f"Request body must contain 1 to {maximum} bytes. Nexus did not "
                "truncate or partially apply it."
            )
        try:
            value = json.loads(self.rfile.read(length))
        except UnicodeDecodeError as exc:
            raise HarnessError(
                "Request body is not valid UTF-8. Nexus did not decode replacement characters."
            ) from exc
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

    def _checkup(self, model_setup: dict[str, Any] | None = None) -> dict[str, Any]:
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
        # The legacy workflow uses the default route. Explicit trusted agents
        # are a deliberate workflow choice, so every one of their effective
        # routes must be ready; an unrelated healthy provider must never turn
        # this step green.
        route_readiness = effective_route_readiness(config)
        provider_ready = bool(route_readiness) and all(item["ready"] for item in route_readiness)
        first_request_routes = [item["route"] for item in route_readiness if item.get("ready_for_first_request")]
        route_problem = "; ".join(
            f"{item['route']}: {item.get('note') or item.get('state')}"
            for item in route_readiness if not item["ready"]
        )
        steps = [
            {
                "id": "provider",
                "title": "Connect a model",
                "done": provider_ready,
                "detail": (
                    (("Ready to make a clearly labelled first live readiness request through: "
                      + ", ".join(first_request_routes) + ". No success will be claimed if it refuses.")
                     if first_request_routes else
                     "Every route used by the selected workflow and its trusted agents is ready.")
                    if provider_ready else
                    "Every effective route must be ready, not merely one unrelated provider. " + route_problem
                ),
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
            "required_routes": route_readiness,
            "bootstrap_ready": provider_ready,
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
        board_capability = self._single_header(qalab.BOARD_QA_CAPABILITY_HEADER)
        if board_capability:
            if not qalab.board_qa_capability_is_active(
                board_capability, swarm_lab.where_it_lives(),
            ):
                raise HarnessError("Missing or invalid board QA transaction capability")
            swarm_lab._set_board_qa_request_capability(board_capability)  # noqa: SLF001

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
                    "runtime": {
                        "version": __version__,
                        "commit": harness_commit_identity(),
                        "build_kind": str(os.environ.get("NEXUS_BUILD_KIND") or "source/runtime identity unavailable"),
                        "project_root": str(self.server.config.project_root),
                        "port": self.server.server_port,
                        "process_id": os.getpid(),
                        "python_executable": sys.executable,
                        "python_version": sys.version.split()[0],
                    },
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
            elif parsed.path == "/api/swarm/recoveries":
                self._require_token()
                self._json(self.server.swarm_runs.recoverable_work())
            elif parsed.path == "/api/long-horizon/goals":
                self._require_token()
                goals = self.server.long_horizon.store.list(100)
                self.server.project_long_horizon_chat_statuses(goals)
                self._json({"goals": goals})
            elif parsed.path == "/api/long-horizon/pending-admissions":
                self._require_token()
                self._json(self.server.direct_admission_recovery_inventory())
            elif parsed.path == "/api/long-horizon/goal":
                self._require_token()
                query = urllib.parse.parse_qs(parsed.query)
                goal_id = str(query.get("id", [""])[0])
                store = self.server.long_horizon.store
                goal = store.public(store.get(goal_id))
                self.server.project_long_horizon_chat_statuses([goal])
                self._json({"goal": goal})
            elif parsed.path == "/api/long-horizon/events":
                self._require_token()
                query = urllib.parse.parse_qs(parsed.query)
                goal_id = str(query.get("id", [""])[0])
                try:
                    after = int(query.get("after", ["0"])[0])
                except ValueError:
                    after = 0
                self._json(self.server.long_horizon.store.events(goal_id, after))
            elif parsed.path == "/api/swarm/activity":
                self._require_token()
                query = urllib.parse.parse_qs(parsed.query)
                identity = query.get("run_id", query.get("activity", [""]))[0]
                try:
                    store, _run = self.server.find_swarm_run(identity)
                    projection = store.projection(
                        identity, int(query.get("after", ["0"])[0])
                    )
                    stage = "Starting the request"
                    detail = "Nexus is preparing the agent connection."
                    turns = []
                    for event in projection["events"]:
                        payload = event.get("payload")
                        if event.get("kind") == "progress" and isinstance(payload, dict):
                            stage = str(payload.get("stage") or stage)
                            detail = str(payload.get("detail") or detail)
                        elif event.get("kind") == "agent_turn" and isinstance(payload, dict):
                            turns.append(payload)
                    state = {
                        "accepted": "waiting", "running": "working",
                        "stopping": "stopping", "complete": "complete",
                        "stopped": "stopped",
                    }.get(str(projection.get("status") or ""), "error")
                    projection.update({
                        "activity": identity, "state": state,
                        "stage": stage, "detail": detail, "turns": turns,
                    })
                    self._json(projection)
                except (HarnessError, ValueError):
                    self._json(self.server.chat_activities.read(identity))
            elif parsed.path == "/api/web-chats/pending":
                self._require_token()
                self._json({"requests": self.server.web_chats.pending()})
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
            elif parsed.path == "/api/pipeline-runs/by-request":
                self._require_token()
                query = urllib.parse.parse_qs(parsed.query)
                request_id = (query.get("request_id", [""])[0] or "").strip()
                if not request_id:
                    raise HarnessError("Name the automation request ID to look up.")
                self._json(self.server.pipeline_store.by_request(request_id))
            elif parsed.path.startswith("/api/pipeline-runs/"):
                self._require_token()
                parts = [one for one in parsed.path.split("/") if one]
                if len(parts) not in (3, 4) or parts[:2] != ["api", "pipeline-runs"]:
                    raise HarnessError("That automation-run route does not exist.")
                run_id = parts[2]
                if len(parts) == 4:
                    if parts[3] != "events":
                        raise HarnessError("That automation-run route does not exist.")
                    query = urllib.parse.parse_qs(parsed.query)
                    after = int((query.get("after", ["0"])[0] or "0"))
                    self._json({
                        "run_id": run_id,
                        "events": self.server.pipeline_store.events(run_id, after),
                    })
                else:
                    self._json(self.server.pipeline_store.get(run_id))
            elif parsed.path == "/api/pipelines":
                self._require_token()
                config = self.server.config
                query = urllib.parse.parse_qs(parsed.query)
                wanted = (query.get("name", [""])[0] or "").strip()
                recover_missing = (
                    (query.get("recover_missing", [""])[0] or "").strip() == "1"
                )
                # Reading takes the lock as well. A read that does not wait for
                # a write is a read that can see half of one.
                with self.server.pipelines_lock:
                    saved_now, saved_problems = pipeline_lab.saved_inventory(config)
                    selected_name = wanted
                    if not selected_name or (
                        recover_missing and selected_name not in saved_now
                    ):
                        selected_name = saved_now[0] if saved_now else ""
                    kept_now = (
                        pipeline_lab.older_ones(config, selected_name)
                        if selected_name else []
                    )
                    on_screen = (
                        pipeline_lab.load(config, selected_name)
                        if selected_name
                        else pipeline_lab.a_starting_pipeline()
                    )
                authority = self.server.project_authority_status()
                cannot_run = str(authority.get("reason") or "")
                try:
                    if cannot_run:
                        raise HarnessError(cannot_run)
                    store = self.server.pipeline_store
                    active_run = store.active()
                    latest_run = store.latest()
                    authority_id = store.authority_id
                except HarnessError as exc:
                    # Saved definitions are ordinary project JSON and remain
                    # useful for review, repair, import, and export even when a
                    # copied authority descriptor correctly pauses execution.
                    # Do not turn that execution fence into apparent data loss.
                    active_run = None
                    latest_run = None
                    authority_id = ""
                    cannot_run = str(exc)
                if active_run is not None:
                    active_run["project_authority_id"] = authority_id
                if latest_run is not None:
                    latest_run["project_authority_id"] = authority_id
                answer: dict[str, Any] = {
                    "project_authority_id": authority_id,
                    "cannot_run": cannot_run,
                    "authority": authority,
                    "saved": saved_now,
                    "saved_problems": saved_problems,
                    "selected_name": selected_name,
                    "kinds": [kind.to_dict() for kind in pipeline_lab.KINDS.values()],
                    "running": bool(active_run),
                    "active_run": active_run,
                    # A completed run must remain reconcilable after an
                    # ambiguous POST response: the client persists request_id
                    # and maps it here to the one authoritative run_id.
                    "latest_run": latest_run,
                    "waiting_at": (active_run or {}).get("waiting_at", ""),
                    "last_run": (
                        (latest_run or {}).get("result") or self.server.pipeline_run
                    ),
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
            elif parsed.path == "/api/pipelines/export":
                self._require_token()
                query = urllib.parse.parse_qs(parsed.query)
                wanted = (query.get("name", [""])[0] or "").strip()
                if not wanted:
                    raise HarnessError("Choose a saved automation to export.")
                with self.server.pipelines_lock:
                    document = pipeline_lab.export_document(self.server.config, wanted)
                    filename = pipeline_lab.file_for(self.server.config, wanted).name
                self._json({"filename": filename, "document": document})
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
            elif parsed.path == "/api/pipelines/agent-contract":
                self._require_token()
                query = urllib.parse.parse_qs(parsed.query)
                wanted = (query.get("name", [""])[0] or "").strip()
                if not wanted:
                    raise HarnessError("Name the saved automation the desktop agent must run.")
                with self.server.pipelines_lock:
                    drawn = pipeline_lab.load(self.server.config, wanted)
                self._json({
                    "protocol": "nexus-harness.pipeline-run.v1",
                    "purpose": "Run exactly one saved visual test automation.",
                    "automation": {"name": drawn["name"], "definition": drawn},
                    "execution": {
                        "method": "POST", "path": "/api/pipelines/agent-run",
                        "body": {"automation": drawn["name"], "from_here": "", "only": ""},
                        "rules": [
                            "Use the exact automation name returned above.",
                            "Do not substitute another automation or infer a step.",
                            "POST /api/pipelines/agent-run returns accepted=true and an immutable run_id.",
                            "Poll GET /api/pipeline-runs/{run_id}; never follow global last-run state.",
                            "Read only that run's state/result and optional /events?after= cursor.",
                            "On rejection, stop and report the error; do not guess a payload."
                        ]
                    },
                    "standalone": True,
                    "swarm_interop": "The AI Agent Swarm orchestrator may call this contract, but it is not required and has no shared UI state."
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
            elif parsed.path == "/api/swarm/export-kept":
                self._require_token()
                query = urllib.parse.parse_qs(parsed.query)
                wanted = str(query.get("name", [""])[0] or "").strip()
                with self.server.swarm_lock:
                    document = swarm_lab.export_kept_board(wanted)
                self._json({
                    "document": document,
                    "filename": f"{swarm_lab._filed_under(wanted).rsplit('.', 1)[0]}.json",
                })
            elif parsed.path == "/api/swarm":
                self._require_token()
                query = urllib.parse.parse_qs(parsed.query)
                refresh_providers = str(
                    query.get("refresh_providers", ["true"])[0]
                ).lower() not in {"0", "false", "no"}
                # Provider discovery can launch several installed CLIs. Do it
                # before taking the topology lock so a board already painted
                # in the UI remains editable while that decoration is slow.
                if refresh_providers:
                    self.server.refresh_swarm_provider_status()
                with self.server.swarm_lock:
                    said = self.server.swarm_standing()
                    config = self.server.config
                    said["what_is_not_ready"] = swarm_lab.what_is_not_ready(config, said)
                try:
                    self.server.decorate_swarm_authority(said)
                except HarnessError as exc:
                    # Failure to verify the global board lease is a mutation
                    # fence, not permission to hide the user's board data.
                    said["cannot_be_changed"] = str(exc)
                    authority = self.server.project_authority_status()
                    said["cannot_run"] = str(authority.get("reason") or "")
                    said["authority"] = authority
                self._json(said)
            elif parsed.path == "/api/swarm/chats":
                self._require_token()
                query = urllib.parse.parse_qs(parsed.query)
                agent_id = query.get("agent", [""])[0]
                with self.server.swarm_lock:
                    # Listing saved conversations is a local registry read. It
                    # must not re-run every provider CLI/version/readiness
                    # probe: the board has already cached those results, and
                    # chat switching asks for this list immediately before or
                    # after reading the selected transcript. A fresh scan here
                    # made one click wait for several unrelated assistants.
                    standing = self.server.swarm_standing()
                self._json(swarm_chats.list_for_agent(
                    self.server.config, standing["board"], agent_id
                ))
            elif parsed.path == "/api/swarm/said":
                # One agent's own conversation. Its name decides which file is
                # read, so two agents both using Claude do not read each
                # other's half of it.
                self._require_token()
                query = urllib.parse.parse_qs(parsed.query)
                with self.server.swarm_lock:
                    # Transcript identity comes from the saved board and pair
                    # registry. Provider discovery cannot change which file a
                    # chat owns, so reuse the cached route status just as a
                    # live chat turn does below.
                    standing = self.server.swarm_standing()
                    board = swarm_lab.read_it(standing["board"])
                agent_id = query.get("agent", [""])[0]
                one = swarm_lab.the_agent(board, agent_id)
                chat_id = query.get("chat", [""])[0]
                conversation = swarm_chats.resolve(
                    self.server.config, standing["board"], agent_id, chat_id,
                    allow_binding_drift=True,
                ) if chat_id else None
                filed_as = (
                    str(conversation["filed_as"]) if conversation
                    else one.filed_as_name or swarm_lab.filed_as(one.name)
                )
                self._json({
                    "agent": one.to_dict(),
                    "said": [
                        held.to_dict() for held in chat_lab.read_it(
                            self.server.config,
                            str(conversation.get("transcript_route") or one.who)
                            if conversation else one.who,
                            filed_as,
                        )
                        if held.phase != "long_horizon_checkpoint"
                    ],
                    "most_letters": chat_lab.MOST_LETTERS,
                    "limits": chat_lab.effective_limits(
                        self.server.config, str(one.who or "")
                    ),
                    "conversation": conversation,
                })
            elif parsed.path == "/api/swarm/how-it-is-going":
                self._require_token()
                query = urllib.parse.parse_qs(parsed.query)
                try:
                    after = int(query.get("after", ["0"])[0])
                except ValueError:
                    after = 0
                run_id = query.get("run_id", [""])[0]
                runner = self.server._swarm_runner
                doing = (
                    runner.how_it_is_going(run_id, after)
                    if runner is not None
                    else self.server.swarm_runner.how_it_is_going(run_id, after)
                    if run_id
                    else None
                )
                self._json({"doing": doing})
            elif parsed.path == "/api/swarm/goal-queue":
                self._require_token()
                self._json({"queue": self.server.swarm_goal_queue_status()})
            elif parsed.path == "/api/swarm/event-payload":
                self._require_token()
                query = urllib.parse.parse_qs(parsed.query)
                run_id = str(query.get("run_id", [""])[0] or "").strip()
                if not run_id:
                    raise HarnessError("An exact durable Swarm run ID is required.")
                try:
                    seq = int(query.get("seq", [""])[0])
                    offset = int(query.get("offset", ["0"])[0])
                except (TypeError, ValueError) as exc:
                    raise HarnessError(
                        "The exact Swarm event sequence and byte offset must be whole numbers."
                    ) from exc
                self._json(self.server.swarm_runs.event_payload(run_id, seq, offset))
            elif parsed.path == "/api/swarm/attachment":
                self._require_token()
                query = urllib.parse.parse_qs(parsed.query)
                self._chat_attachment(
                    query.get("agent", [""])[0], query.get("id", [""])[0]
                )
            elif parsed.path == "/api/swarm/what-they-said":
                # Asked for on its own rather than sent with every "how is it
                # going": these are whole answers, and a page watching a run
                # asks how it is going every second and a half.
                self._require_token()
                runner = self.server._swarm_runner
                self._json(
                    runner.what_they_said() if runner is not None
                    else swarm_lab.Running().what_they_said()
                )
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
                authority = self.server.project_authority_status()
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
                    "cannot_run": str(authority.get("reason") or ""),
                    "authority": authority,
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
                authority = self.server.project_authority_status()
                self._json({
                    "who": everyone,
                    "open": wanted,
                    "said": [
                        one.to_dict()
                        for one in chat_lab.read_it(self.server.config, wanted)
                    ] if ready else [],
                    "most_letters": chat_lab.MOST_LETTERS,
                    "most_kept": chat_lab.MOST_KEPT,
                    "limits": chat_lab.effective_limits(
                        self.server.config, wanted
                    ),
                    "cannot_run": str(authority.get("reason") or ""),
                    "authority": authority,
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
                    authority = self.server.project_authority_status()
                    self._json({
                        **self._qa_suite(),
                        "cannot_run": str(authority.get("reason") or ""),
                        "authority": authority,
                    })
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
                    advice = setup_advice(self.server.config, refresh=refresh)
                    authority = self.server.project_authority_status()
                    self._json({
                        **self._checkup(advice), "model_setup": advice,
                        "cannot_run": str(authority.get("reason") or ""),
                        "authority": authority,
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
            if self.path == "/api/web-chats/heartbeat":
                routes = self.server.web_chats.heartbeat(body.get("connections"))
                self._json({"routes": routes})
            elif self.path == "/api/web-chats/complete":
                receipt = self.server.web_chats.complete(
                    body.get("request_id"),
                    answer=body.get("answer"), error=body.get("error"),
                    milliseconds=body.get("milliseconds"), model=body.get("model"),
                    delivery_state=body.get("delivery_state"),
                    failure_code=body.get("failure_code"),
                    diagnostics=body.get("diagnostics"),
                )
                accepted = receipt is True
                self._json({
                    # Exact acknowledgement is a delivery boundary. Legacy or
                    # injected brokers that return None have not proven it.
                    "accepted": accepted,
                    "receipt_state": (
                        "recorded" if accepted else "expired_or_unknown"
                    ),
                })
            elif self.path == "/api/validate":
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
                bootstrap_tests = body.get("bootstrap_tests", False)
                if not isinstance(bootstrap_tests, bool):
                    raise HarnessError("bootstrap_tests must be true or false")
                if bootstrap_tests:
                    task = task.rstrip() + (
                        "\n\nNEXUS BOOTSTRAP MODE (explicitly selected by the user): "
                        "this project does not yet have dependable automated tests. "
                        "First inspect its stack and create the smallest maintainable runnable test infrastructure, "
                        "including a real test command and focused acceptance tests for this goal. "
                        "Then run those tests and the project's other applicable checks. "
                        "Bootstrap mode never waives final verification and is not permission to claim success without evidence."
                    )
                graph = body.get("graph")
                if graph is not None:
                    if not isinstance(graph, dict):
                        raise HarnessError("Run graph must be an object")
                    _migrated, issues = self._executable_graph(graph)
                    if issues:
                        raise HarnessError("Run graph is not executable: " + "; ".join(
                            f"{issue['path']}: {issue['message']}" for issue in issues
                        ))
                with self.server.project_admission_lock:
                    config = self.server.config
                    project_root = config.project_root.resolve(strict=True)
                    self.server.require_project_execution_authority(project_root)
                    self.server.require_no_long_horizon_path(project_root)
                    if not self.server.reserve_run():
                        self._json({"error": "A workspace run is already active"}, HTTPStatus.CONFLICT)
                        return
                thread = threading.Thread(
                    target=self._run_task,
                    args=(task, bool(body.get("dry_run", False)), graph, bootstrap_tests, config),
                    daemon=True,
                )
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
                self.server.require_project_execution_authority()
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
                self.server.require_project_execution_authority()
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
                        # Validation belongs in vault.write_one.  Slicing here
                        # used to report success after silently discarding the
                        # tail of a long note.
                        body=str(body.get("body") or ""),
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
                    with self.server.swarm_lock:
                        self.server.require_config_reload_boundary()
                        done = settings_lab.change(
                            self._settings_now(), str(body.get("key") or ""),
                            body.get("value")
                        )
                        # An existing untrusted local file deliberately stays
                        # ineffective until the person approves the exact file
                        # they were shown. The trust endpoint reloads it after
                        # that explicit choice.
                        if not done.needs_trusting:
                            self.server.reload_config()
                self._json(done.to_dict())
            elif self.path == "/api/settings/reset":
                with self.server.seats_lock:
                    with self.server.swarm_lock:
                        self.server.require_config_reload_boundary()
                        done = settings_lab.reset(
                            self._settings_now(), str(body.get("key") or "")
                        )
                        self.server.reload_config()
                self._json(done.to_dict())
            elif self.path == "/api/pipelines/starter":
                self._json({"pipeline": pipeline_starters.build(str(body.get("key") or ""))})
            elif self.path == "/api/explain":
                # What a failure means, in words. Nothing is looked up
                # anywhere: it reads what the check already said.
                self._json(explainer.what_it_means(
                    # The request-body boundary is already explicit. Do not
                    # silently discard the tail here: a recognisable failure
                    # marker may occur after a long build log.
                    str(body.get("said") or ""), kind=str(body.get("kind") or "")
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
            elif self.path == "/api/projects/use-as-new-local":
                if body.get("confirmation") != "USE THIS FOLDER AS A NEW LOCAL PROJECT":
                    raise HarnessError(
                        "Confirm ‘Use this folder as a new local project’ before rotating authority."
                    )
                if not self.server.pipeline_lock.acquire(blocking=False):
                    raise HarnessError("An automation is active; wait for it before repairing authority.")
                if not self.server.run_lock.acquire(blocking=False):
                    self.server.pipeline_lock.release()
                    raise HarnessError("Project work is active; wait for it before repairing authority.")
                if not self.server.swarm_lock.acquire(blocking=False):
                    self.server.run_lock.release()
                    self.server.pipeline_lock.release()
                    raise HarnessError("Swarm work is being accepted; wait before repairing authority.")
                try:
                    if self.server.pipeline_running:
                        raise HarnessError(
                            "An automation is active; wait for it before repairing authority."
                        )
                    if self.server._swarm_runner is not None and self.server._swarm_runner.busy:
                        raise HarnessError(
                            "The board is active; stop it before repairing authority."
                        )
                    if self.server._pipeline_store is not None \
                            and self.server._pipeline_store.active() is not None:
                        raise HarnessError(
                            "An automation run is active; stop it before repairing authority."
                        )
                    if self.server._swarm_runs is not None \
                            and self.server._swarm_runs.active() is not None:
                        raise HarnessError(
                            "Swarm or chat work is active; stop it before repairing authority."
                        )
                    if self.server.swarm_change_pause_reason():
                        raise HarnessError(
                            "A board run is active; stop it before repairing authority."
                        )
                    with self.server.authority_lock:
                        authority_id = pipeline_runtime.repair_project_authority(
                            self.server.config.project_root,
                            str(body.get("fingerprint") or ""),
                        )
                        # Discard only cached execution objects, and only after
                        # the descriptor and user-local registration both land.
                        self.server._pipeline_store = None
                        self.server._swarm_runs = None
                        self.server._swarm_runner = None
                    authority = self.server.project_authority_status()
                    self._json({
                        "repaired": True,
                        "project_authority_id": authority_id,
                        "authority": authority,
                        "note": "This folder is now a new local project. Project work is enabled.",
                    })
                finally:
                    self.server.swarm_lock.release()
                    self.server.run_lock.release()
                    self.server.pipeline_lock.release()
            elif self.path == "/api/swarm/verification-approval":
                # This is deliberately separate from the general board-save
                # endpoint. A visible approval press names the exact project,
                # path, board version, and freshly discovered command digest;
                # a portable/imported JSON field is never sufficient by itself.
                with self.server.swarm_lock:
                    stopping = self.server.swarm_change_pause_reason()
                    if stopping:
                        raise swarm_lab.SwarmError(stopping)
                    project_id = str(body.get("project_id") or "").strip()
                    project_path = str(body.get("project_path") or "")
                    approved_value = body.get("approved")
                    board_version = body.get("board_version")
                    if (
                        not project_id
                        or not project_path
                        or not isinstance(approved_value, bool)
                        or isinstance(board_version, bool)
                        or not isinstance(board_version, int)
                    ):
                        raise swarm_lab.SwarmError(
                            "Choose one current board project and whether to approve or revoke its exact test commands."
                        )
                    board = swarm_lab.load()
                    if board.version != board_version:
                        raise swarm_lab.SwarmError(
                            "The board changed before this command decision landed. Review the current project commands again."
                        )
                    project = next(
                        (one for one in board.projects if one.id == project_id), None
                    )
                    if project is None or project.path != project_path:
                        raise swarm_lab.SwarmError(
                            "That project or its folder path changed. Review the current project commands again."
                        )
                    if approved_value:
                        proposal = swarm_work.verification_command_approval(
                            self.server.config, project.to_dict()
                        )
                        supplied = str(body.get("approval_digest") or "").lower()
                        current = str(proposal.get("approval_digest") or "")
                        if (
                            not proposal.get("requires_approval")
                            or not proposal.get("can_approve")
                            or not supplied
                            or not secrets.compare_digest(supplied, current)
                        ):
                            raise swarm_lab.SwarmError(
                                "The project path or discovered test commands changed. Nothing was approved; refresh and review the exact commands again."
                            )
                        project.approved_test_command_digest = current
                        note = (
                            "Approved only the exact test commands shown for "
                            f"{project_path}. A path or command change revokes this automatically."
                        )
                    else:
                        project.approved_test_command_digest = ""
                        note = (
                            f"Revoked discovered test-command approval for {project_path}. "
                            "Nexus will not run those commands until they are reviewed and approved again."
                        )
                    swarm_lab.save(
                        board.to_dict(),
                        self.server.config,
                        allow_command_approval_changes=True,
                    )
                    said = self.server.swarm_standing()
                    said["what_is_not_ready"] = swarm_lab.what_is_not_ready(
                        self.server.config, said
                    )
                    said["verification_command_approval_note"] = note
                    self.server.decorate_swarm_authority(said)
                self._json(said)
            elif self.path == "/api/swarm/save":
                # The whole board at once. It is one small picture, and saving
                # it whole means the panel can never leave a line pointing at a
                # box that half of a save had already taken away.
                # Both under the one lock. Asked outside it, a run could start
                # in the gap between "is anything going?" and the write, and
                # then be working from a board that had changed underneath it.
                with self.server.swarm_lock:
                    stopping = self.server.swarm_change_pause_reason()
                    if stopping:
                        raise swarm_lab.SwarmError(stopping)
                    swarm_lab.save(body.get("board"), self.server.config)
                    # Saving settings must finish at disk speed. Provider
                    # discovery is refreshed by board reads and Look again;
                    # reusing that status here avoids multi-second CLI probes
                    # between every keystroke and its autosave acknowledgement.
                    said = self.server.swarm_standing()
                    said["what_is_not_ready"] = swarm_lab.what_is_not_ready(
                        self.server.config, said
                    )
                    self.server.decorate_swarm_authority(said)
                self._json(said)
            elif self.path in {
                "/api/swarm/chats/create",
                "/api/swarm/chats/activate",
                "/api/swarm/chats/project",
                "/api/swarm/chats/delete",
                "/api/swarm/chats/restore",
                "/api/swarm/collaboration/reset",
            }:
                agent_id = str(body.get("agent") or "")
                with self.server.swarm_lock:
                    # These mutations only change local pair-chat metadata.
                    # Provider readiness is refreshed by /api/swarm and Look
                    # again; probing every installed CLI while this lock is
                    # held turned an atomic file update into a multi-second UI
                    # stall and then the transcript read repeated that stall.
                    standing = self.server.swarm_standing()
                if self.path == "/api/swarm/chats/create":
                    said = swarm_chats.create(
                        self.server.config, standing["board"], agent_id,
                        str(body.get("peer") or ""),
                        scope=str(body.get("scope") or ""),
                    )
                elif self.path == "/api/swarm/chats/activate":
                    said = swarm_chats.activate(
                        self.server.config, standing["board"], agent_id,
                        str(body.get("chat") or ""),
                    )
                elif self.path == "/api/swarm/chats/project":
                    chat_id = str(body.get("chat") or "")
                    with self.server.swarm_communication_runs.conversation_turn(
                        f"chat-project-{uuid.uuid4().hex}", chat_id, timeout=0.0,
                    ):
                        said = swarm_chats.select_project(
                            self.server.config, standing["board"], agent_id,
                            chat_id, str(body.get("project") or ""),
                        )
                elif self.path == "/api/swarm/chats/delete":
                    chat_id = str(body.get("chat") or "")
                    with self.server.swarm_communication_runs.conversation_turn(
                        f"chat-archive-{uuid.uuid4().hex}", chat_id, timeout=0.0,
                    ):
                        said = swarm_chats.delete(
                            self.server.config, standing["board"], agent_id,
                            chat_id,
                        )
                elif self.path == "/api/swarm/collaboration/reset":
                    chat_id = str(body.get("chat") or "")
                    with self.server.swarm_communication_runs.conversation_turn(
                        f"collaboration-reset-{uuid.uuid4().hex}",
                        chat_id, timeout=0.0,
                    ):
                        conversation = swarm_chats.resolve(
                            self.server.config, standing["board"], agent_id,
                            chat_id, allow_binding_drift=True,
                        )
                        from .collaboration_ledger import (
                            collaboration_problem, remove_ledger,
                        )

                        route = str(conversation.get("transcript_route") or "")
                        filed_as = str(conversation.get("filed_as") or "")
                        problem = collaboration_problem(
                            self.server.config, route, filed_as,
                        )
                        if not problem:
                            raise swarm_lab.SwarmError(
                                "This chat's collaboration record passes its integrity checks. "
                                "Nothing was reset."
                            )
                        remove_ledger(self.server.config, route, filed_as)
                        said = swarm_chats.list_for_agent(
                            self.server.config, standing["board"], agent_id,
                        )
                        said["collaboration_reset"] = {
                            "schema_version": 1,
                            "chat_id": chat_id,
                            "transcript_preserved": True,
                            "attachments_preserved": True,
                            "provider_conversation_preserved": True,
                            "automatic_resend": False,
                        }
                        said["note"] = (
                            "The untrusted collaboration record was reset. The saved "
                            "conversation and attachments were kept, and Nexus did not "
                            "resend any prompt."
                        )
                else:
                    said = swarm_chats.restore(
                        self.server.config, standing["board"], agent_id,
                        str(body.get("chat") or ""),
                    )
                self._json(said)
            elif self.path == "/api/swarm/goal-queue/start":
                request_id = str(body.get("request_id") or uuid.uuid4().hex)
                with self.server.project_admission_lock, self.server.swarm_lock:
                    standing = self.server.swarm_standing()
                    for project in standing["board"].get("projects", []):
                        if isinstance(project, dict) and project.get("is_there") is True \
                                and any(isinstance(one, str) and one.strip()
                                        for one in project.get("tasks", [])):
                            self.server.require_no_long_horizon_owner(
                                standing["board"], str(project.get("id") or ""),
                            )
                    queue = self.server.swarm_goal_queue.start(
                        standing["board"], request_id
                    )
                self._json({"queue": queue})
            elif self.path == "/api/long-horizon/start-board":
                request_id = str(body.get("request_id") or uuid.uuid4().hex)
                with self.server.project_admission_lock, self.server.swarm_lock:
                    standing = self.server.swarm_standing()
                    if "goal" in body:
                        spec = body.get("goal")
                        if not isinstance(spec, dict) or spec.get("schema_version") != 1:
                            raise HarnessError(
                                "The board goal must use the supported goal schema version"
                            )
                        project_id = str(spec.get("project_id") or "")
                        project_root = self.server._board_project_path(
                            standing["board"], project_id
                        )
                        objectives = (
                            [str(one) for one in spec.get("objectives", [])]
                            if isinstance(spec.get("objectives"), list) else []
                        )
                        success_criteria = (
                            [str(one) for one in spec.get("success_criteria", [])]
                            if isinstance(spec.get("success_criteria"), list) else None
                        )
                        collaboration_mode = str(
                            spec.get("collaboration_mode") or "adaptive"
                        )
                        if collaboration_mode not in {"adaptive", "every"}:
                            raise HarnessError(
                                "Choose either adaptive team work or a required contribution from every selected agent"
                            )
                        if not isinstance(spec.get("participant_ids"), list):
                            raise HarnessError("Choose the agents authorized for this goal")
                        participant_ids = list(dict.fromkeys(
                            str(one) for one in spec.get("participant_ids", []) if str(one)
                        ))
                        if not participant_ids:
                            raise HarnessError("Choose at least one ready agent")
                        require_all_participants = collaboration_mode == "every"
                        policy = spec.get("policy") \
                            if isinstance(spec.get("policy"), dict) else None
                        lead_id = str(spec.get("lead_id") or "")
                        self.server.long_horizon.store.validate_create(
                            standing["board"], project_id, objectives, request_id,
                            success_criteria=success_criteria, policy=policy,
                            participant_ids=participant_ids,
                            require_all_participants=require_all_participants,
                        )
                        admitted_authority = self.server.require_project_execution_authority(
                            project_root
                        )
                        goal = self.server.long_horizon.start(
                            standing["board"], project_id, objectives, request_id,
                            lead_id=lead_id, success_criteria=success_criteria,
                            policy=policy, participant_ids=participant_ids,
                            require_all_participants=require_all_participants,
                            expected_project_authority_id=str(
                                admitted_authority.get("project_authority_id") or ""
                            ),
                        )
                        goals = [goal]
                    else:
                        # Compatibility for saved boards created before the inline goal
                        # composer. Their project task lists remain a supported batch-start
                        # input, while new UI starts are intent-bound to one explicit spec.
                        selected = [
                            one for one in standing["board"].get("projects", [])
                            if isinstance(one, dict) and one.get("is_there") is True
                            and any(isinstance(task, str) and task.strip()
                                    for task in one.get("tasks", []))
                        ]
                        for project in selected:
                            self.server.long_horizon.store.validate_create(
                                standing["board"], str(project.get("id") or ""),
                                [str(task) for task in project.get("tasks", [])
                                 if isinstance(task, str) and task.strip()],
                                f"{request_id}:{project.get('id')}",
                            )
                        for project in selected:
                            self.server.require_project_execution_authority(
                                Path(str(project.get("path") or ""))
                            )
                        goals = self.server.long_horizon.start_board(
                            standing["board"], request_id
                        )
                self._json({"goals": goals, "engine": "long_horizon"}, HTTPStatus.ACCEPTED)
            elif self.path == "/api/long-horizon/prepare-admission":
                prepared = dict(body)
                prepared["request_id"] = str(
                    body.get("request_id") or uuid.uuid4().hex
                )
                pending = self.server.prepare_direct_long_horizon(prepared)
                self._json({"pending": pending})
            elif self.path == "/api/long-horizon/admission-goal":
                self._json(self.server.direct_admission_goal(body))
            elif self.path == "/api/long-horizon/discard-admission":
                discarded = self.server.discard_direct_admission(body)
                self._json(discarded)
            elif self.path == "/api/long-horizon/acknowledge-admission":
                acknowledged = self.server.acknowledge_direct_admission(body)
                self._json(acknowledged)
            elif self.path == "/api/long-horizon/start":
                prepared = dict(body)
                prepared["request_id"] = str(
                    body.get("request_id") or uuid.uuid4().hex
                )
                goal, receipt = self.server.admit_direct_long_horizon(
                    prepared, from_pending=body.get("from_pending") is True,
                )
                self._json({
                    **receipt, "goal": goal, "engine": "long_horizon",
                }, HTTPStatus.ACCEPTED)
            elif self.path == "/api/long-horizon/control":
                goal_id = str(body.get("goal_id") or "")
                action = str(body.get("action") or "")
                if action not in {"pause", "cancel"}:
                    held_goal = self.server.long_horizon.store.get(goal_id)
                    self.server.require_project_execution_authority(
                        Path(str(held_goal.get("project", {}).get("path") or ""))
                    )
                payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
                with self.server.project_admission_lock, self.server.swarm_lock:
                    runtime = self.server.long_horizon
                    if action == "resume":
                        goal = runtime.resume(goal_id)
                    elif action == "fork":
                        goal = runtime.fork(goal_id, str(body.get("request_id") or uuid.uuid4().hex))
                    else:
                        goal = runtime.control(goal_id, action, payload)
                self._json({"goal": goal})
            elif self.path == "/api/long-horizon/answer":
                goal_id = str(body.get("goal_id") or "")
                held_goal = self.server.long_horizon.store.get(goal_id)
                self.server.require_project_execution_authority(
                    Path(str(held_goal.get("project", {}).get("path") or ""))
                )
                answers = body.get("answers") if isinstance(body.get("answers"), dict) else {}
                with self.server.project_admission_lock, self.server.swarm_lock:
                    goal = self.server.long_horizon.resume(goal_id, {
                        "answers": answers,
                        "expected_revision": int(body.get("expected_revision") or 0),
                        "pending_ids": body.get("pending_ids") if isinstance(body.get("pending_ids"), list) else [],
                    })
                self._json({"goal": goal})
            elif self.path == "/api/swarm/goal-queue/cancel":
                queue = self.server.swarm_goal_queue.cancel(
                    str(body.get("queue_id") or "")
                )
                self._json({"queue": queue})
            elif self.path == "/api/swarm/say":
                # No board lock while it waits for an answer: an assistant can
                # take a minute, and holding the lock that long would freeze
                # every other window looking at the same board. The
                # conversation has its own lock, which is the one that matters.
                activity_id = str(body.get("activity") or "")
                request_id = str(body.get("request_id") or activity_id or uuid.uuid4().hex)
                activity = self.server.chat_activities
                cancellations = self.server.chat_cancellations
                config = None
                run_store = None
                activity.update(
                    activity_id, "Reading your request",
                    "Nexus is checking the selected agent and board connections."
                )
                run_id = ""
                one = None
                filed_as = ""
                def progress(stage, detail=""):
                    activity.update(activity_id, stage, detail)
                    if run_id and run_store is not None:
                        run_store.event(run_id, "progress", {
                            "stage": str(stage)[:180], "detail": str(detail)[:500],
                        })

                def live_turn(turn):
                    activity.add_turn(activity_id, turn)
                    if run_id and run_store is not None:
                        run_store.event(run_id, "agent_turn", turn)
                agent_id = str(body.get("agent") or "")
                chat_id = str(body.get("chat") or "")
                chat_key = chat_id or agent_id
                cancel_token = None
                cancel_scope = None
                run_scope = None
                chat_scope = None
                stop_watch_done = threading.Event()
                stop_watch_thread = None
                chat_owned = False
                answer_saved = False
                requested_mode = str(body.get("mode") or "auto")
                mode = requested_mode
                routing = {
                    "requested": requested_mode,
                    "selected": requested_mode,
                    "reason": "The user chose this action explicitly.",
                }
                conversation = None
                board_payload = {}
                peer_id = ""
                project_id = ""
                objective_text = ""
                work_text = None
                work_answers = body.get("user_answers")
                goal_queue_id = str(body.get("goal_queue_id") or "")
                goal_item_id = str(body.get("goal_item_id") or "")
                goal_queue_claimed = False
                replay_response = None
                replay_status = HTTPStatus.OK
                response_to_deliver = None
                response_delivery_status = HTTPStatus.OK

                def release_chat_ownership() -> None:
                    """End the durable turn before any terminal response I/O.

                    A client can finish reading a small loopback response before
                    ``BaseHTTPRequestHandler`` returns from ``_json``. Keeping
                    the whole-chat lease until the outer ``finally`` therefore
                    creates a real window where the next user turn is rejected
                    as though the completed request were still running.
                    """

                    nonlocal stop_watch_thread, run_scope
                    nonlocal cancel_token, cancel_scope, chat_scope
                    stop_watch_done.set()
                    cleanup_error: BaseException | None = None

                    watcher = stop_watch_thread
                    stop_watch_thread = None
                    if watcher is not None:
                        try:
                            # ``Thread.start`` itself can fail. Joining that
                            # never-started object raises RuntimeError and used
                            # to abort cleanup before the cancellation/context
                            # and whole-chat lease were released.
                            if watcher.ident is not None:
                                watcher.join(0.25)
                        except BaseException as exc:
                            cleanup_error = cleanup_error or exc

                    bound_run = run_scope
                    run_scope = None
                    if bound_run is not None:
                        try:
                            bound_run.__exit__(None, None, None)
                        except BaseException as exc:
                            cleanup_error = cleanup_error or exc

                    token = cancel_token
                    cancel_token = None
                    if token is not None:
                        try:
                            cancellations.finish(chat_key, token)
                        except BaseException as exc:
                            cleanup_error = cleanup_error or exc

                    cancellation_scope = cancel_scope
                    cancel_scope = None
                    if cancellation_scope is not None:
                        try:
                            cancellation_scope.__exit__(None, None, None)
                        except BaseException as exc:
                            cleanup_error = cleanup_error or exc

                    owned_chat = chat_scope
                    chat_scope = None
                    if owned_chat is not None:
                        try:
                            owned_chat.__exit__(None, None, None)
                        except BaseException as exc:
                            cleanup_error = cleanup_error or exc

                    if cleanup_error is not None:
                        raise cleanup_error

                try:
                    # Validate every message before accepting a durable run or
                    # taking a conversation lease. Reusing this checked value
                    # also keeps auto-routed file work from becoming the
                    # literal objective "None".
                    objective_text = chat_lab._check_what_was_typed(body.get("text"))
                    if (
                        requested_mode == "work"
                        and body.get("resume_session_id")
                        and work_answers is not None
                    ):
                        work_answers = chat_lab._check_what_was_typed(work_answers)
                    with self.server.project_admission_lock, self.server.swarm_lock:
                        # Own the exact logical chat before resolving mutable
                        # pair metadata. Archive, project rebind, and reset use
                        # this same authority-neutral lease, so none can slip
                        # between resolution and the eventual transcript write
                        # in this or another Nexus process.
                        candidate_chat_scope = (
                            self.server.swarm_communication_runs.conversation_turn(
                                f"chat-admission-{uuid.uuid4().hex}",
                                chat_key, timeout=0.0,
                            )
                        )
                        candidate_chat_scope.__enter__()
                        chat_scope = candidate_chat_scope
                        # Acceptance and project switching share this lock.  A
                        # request that is accepted captures all project-scoped
                        # collaborators exactly once; later progress,
                        # checkpoints, provider calls and finish never consult
                        # mutable server project properties.
                        config = self.server.config
                        # A chat turn needs the saved board topology, not a fresh
                        # probe of every installed provider.  Probing here can
                        # take seconds (or wait on a CLI), and because the board
                        # lock is held it serialises otherwise independent chats.
                        # It does still need the cached readiness decoration.
                        # The raw persisted board intentionally contains no
                        # ephemeral `ready` fields; passing that raw shape to
                        # pair selection made every explicitly selected peer
                        # look disconnected even when /api/swarm showed both
                        # agents green. Live web routes are re-applied here too.
                        standing = self.server.swarm_standing()
                        board_payload = standing["board"]
                        board = swarm_lab.read_it(board_payload)
                        one = swarm_lab.the_agent(board, agent_id)
                        conversation = swarm_chats.resolve(
                            config, board_payload, agent_id, chat_id
                        ) if chat_id else None
                        peer_id = str(conversation.get("peer") or "") if conversation else ""
                        project_id = str(conversation.get("project") or "") if conversation else ""
                        filed_as = (
                            str(conversation.get("filed_as") or "") if conversation
                            else one.filed_as_name or swarm_lab.filed_as(one.name)
                        )
                        conversation_key = (
                            str(conversation.get("web_conversation_key") or filed_as)
                            if conversation else filed_as
                        )
                        saved_participant_ids = [
                            str(member_id) for member_id in conversation.get("pair", [])
                            if str(member_id)
                        ] if conversation else []
                        single_agent_conversation = (
                            bool(conversation)
                            and saved_participant_ids == [agent_id]
                        )
                        if single_agent_conversation:
                            if requested_mode == "auto":
                                # An empty peer means "all connected peers" to
                                # the older board workflow helpers. A saved
                                # singleton means the opposite: this exact
                                # transcript belongs only to its one agent.
                                # Constrain auto before it can expand the
                                # participant set from the surrounding board.
                                mode = "chat"
                                routing = {
                                    "requested": "auto",
                                    "selected": "chat",
                                    "reason": (
                                        "This saved chat belongs to one agent, "
                                        "so Nexus kept the request direct."
                                    ),
                                }
                            elif requested_mode != "chat":
                                raise swarm_lab.SwarmError(
                                    "This saved chat belongs to one agent. Use its "
                                    "direct Ask button; connected-agent collaboration "
                                    "and project work are unavailable in this chat."
                                )
                        if (
                            requested_mode == "work"
                            and not goal_queue_id
                            and str(body.get("resume_session_id") or "")
                        ):
                            # A resume token is already explicit project-work
                            # intent even when the saved objective is phrased
                            # without words such as "file" or "project".
                            resume_root = self.server._board_project_path(
                                board_payload, project_id
                            )
                            self.server.require_project_execution_authority(resume_root)
                            work_text = objective_text
                            active_queue = self.server.swarm_goal_queue_status()
                            queued = (active_queue or {}).get("current") or {}
                            if (
                                (active_queue or {}).get("status") == "paused"
                                and str(queued.get("resume_token") or "")
                                == str(body.get("resume_session_id") or "")
                                and str(queued.get("objective") or "") == work_text
                                and str(queued.get("lead_id") or "") == agent_id
                                and str(queued.get("peer_id") or "") == peer_id
                                and str(queued.get("project_id") or "") == project_id
                            ):
                                goal_queue_id = str(active_queue["queue_id"])
                                goal_item_id = str(queued["id"])
                                body["board_goal"] = True
                        allow_partial_lead_answer = False
                        if mode == "auto":
                            decision = swarm_work.automatic_mode(
                                config, board_payload, agent_id,
                                objective_text, progress=progress,
                                peer_id=peer_id, project_id=project_id,
                            )
                            mode = decision["mode"]
                            routing = {
                                "requested": "auto",
                                "selected": mode,
                                "reason": decision["reason"],
                            }
                        elif (
                            mode == "work"
                            and body.get("board_goal") is not True
                            and not swarm_work.mentions_project_scope(objective_text)
                        ):
                            # A work-button click is not authority to reinterpret
                            # a plain identity/message question as file mutation.
                            decision = swarm_work.automatic_mode(
                                config, board_payload, agent_id,
                                objective_text, progress=progress,
                                peer_id=peer_id, project_id=project_id,
                            )
                            mode = decision["mode"]
                            routing = {
                                "requested": "work",
                                "selected": mode,
                                "reason": (
                                    "No project or file subject was present, so Nexus did not open a file transaction. "
                                    + decision["reason"]
                                ),
                            }
                        if (
                            mode == "work"
                            and requested_mode == "auto"
                            and body.get("allow_project_changes") is not True
                        ):
                            raise swarm_lab.SwarmError(
                                "This message asks the team to change project files, but that change was not confirmed. Send it again and approve the project-file confirmation."
                            )
                        if mode == "work":
                            # Project execution authority fences only actions
                            # that can run commands or change files. Ordinary
                            # chat keeps its own durable path-scoped journal.
                            project_root = self.server.require_no_long_horizon_owner(
                                board_payload, project_id, agent_id=agent_id,
                            )
                            target_authority = self.server.require_project_execution_authority(
                                project_root
                            )
                            run_store = self.server.swarm_runs
                            work_text = objective_text
                        else:
                            authority = self.server.project_authority_status()
                            run_store = (
                                self.server.swarm_runs
                                if authority.get("can_run")
                                else self.server.swarm_communication_runs
                            )
                        if goal_queue_id or goal_item_id:
                            if (
                                requested_mode != "work"
                                or body.get("board_goal") is not True
                                or not goal_queue_id
                                or not goal_item_id
                            ):
                                raise swarm_lab.SwarmError(
                                    "A durable board-goal identity is valid only for explicit board project work."
                                )
                            self.server.swarm_goal_queue.claim(
                                goal_queue_id,
                                goal_item_id,
                                objective=str(work_text),
                                agent_id=agent_id,
                                peer_id=peer_id,
                                project_id=project_id,
                                conversation_id=str(conversation.get("id") or "")
                                if conversation else "",
                                request_id=request_id,
                            )
                            goal_queue_claimed = True
                        objective = CredentialRedactor(config).text(
                            objective_text
                        )
                        snapshot = {
                            "schema_version": 1,
                            "project_root": str(config.project_root.resolve()),
                            "board": board_payload,
                            "conversation": conversation,
                            "agent_id": agent_id,
                            "chat_key": chat_key,
                            "filed_as": filed_as,
                            "requested_mode": str(body.get("mode") or "auto"),
                            "selected_mode": mode,
                            "project_id": project_id,
                            "target_project_authority_id": (
                                str(target_authority.get("project_authority_id") or "")
                                if mode == "work" else ""
                            ),
                            "objective": objective,
                            "objective_generation": hashlib.sha256(
                                f"{chat_key}\0{request_id}\0{objective}".encode("utf-8")
                            ).hexdigest(),
                        }
                        accepted, created = run_store.accept(request_id, snapshot)
                        run_id = str(accepted["run_id"])
                        if not created:
                            if accepted.get("result") is not None:
                                if goal_queue_claimed:
                                    self.server.swarm_goal_queue.record_result(
                                        goal_queue_id, goal_item_id,
                                        accepted["result"],
                                        run_id=str(accepted.get("run_id") or ""),
                                    )
                                replay_response = accepted["result"]
                            else:
                                replay_response = {
                                    "run_id": run_id, "request_id": request_id,
                                    "state": accepted["status"], "idempotent": True,
                                    "note": "This exact request is already recorded; Nexus did not dispatch it again.",
                                }
                                replay_status = HTTPStatus.ACCEPTED
                        else:
                            run_store.start(run_id)
                            chat_owned = True
                    if replay_response is not None:
                        response_to_deliver = replay_response
                        response_delivery_status = replay_status
                        return
                    cancel_token = cancellations.begin(chat_key, run_id)
                    cancel_scope = cancellation.use(cancel_token)
                    cancel_scope.__enter__()

                    # The watcher can be inside a slow cross-process journal
                    # read after the request has completed and the bounded
                    # cleanup join has elapsed. Capture its collaborators by
                    # value: cleanup may retire the handler's registry/context
                    # references, but the late watcher must still be able to
                    # cancel its own immutable token safely.
                    watch_run_store = run_store
                    watch_run_id = run_id
                    watch_cancel_token = cancel_token

                    def watch_durable_stop() -> None:
                        # Exact Stop may be pressed in another Nexus process.
                        # The signed journal is the cross-process signal; the
                        # local token fans it into provider/subprocess
                        # cancellation without touching any sibling chat.
                        while not stop_watch_done.is_set():
                            try:
                                if watch_run_store.should_stop(watch_run_id):
                                    watch_cancel_token.cancel()
                                    return
                            except Exception:
                                # Losing trustworthy stop state is a reason to
                                # stop this effect, never to keep mutating. Try
                                # to persist that decision first so every
                                # process sees it and the transcript fence can
                                # serialize against it. The local token remains
                                # the fail-closed fallback if persistence is
                                # itself unavailable.
                                try:
                                    watch_run_store.request_stop(watch_run_id)
                                except Exception:
                                    pass
                                watch_cancel_token.cancel()
                                return
                            stop_watch_done.wait(0.075)

                    stop_watch_thread = threading.Thread(
                        target=watch_durable_stop,
                        name=f"nexus-chat-stop-{run_id[:8]}", daemon=True,
                    )
                    stop_watch_thread.start()
                    run_scope = swarm_runs.bind(run_store, run_id)
                    run_scope.__enter__()
                    if not one.who:
                        raise swarm_lab.SwarmError(
                            f"{one.name} has no assistant chosen yet. Open its "
                            "settings and pick which one it uses."
                        )
                    round_limit = swarm_work.user_round_limit(
                        body.get("round_limit")
                    )
                    if mode == "relay":
                        answer = swarm_work.relay(
                            config, board_payload, agent_id,
                            objective_text, body.get("attachments"),
                            progress=progress, live_turn=live_turn,
                            peer_id=peer_id, project_id=project_id,
                            filed_as=filed_as,
                            conversation_key=conversation_key,
                            prefer_existing_conversation=bool(
                                conversation.get("web_legacy_candidate")
                            ) if conversation else True,
                        )
                    elif mode == "collaborate":
                        answer = swarm_work.collaborate(
                            config, board_payload, agent_id,
                            objective_text, body.get("attachments"),
                            progress=progress, live_turn=live_turn,
                            peer_id=peer_id, project_id=project_id,
                            filed_as=filed_as,
                            conversation_key=conversation_key,
                            prefer_existing_conversation=bool(
                                conversation.get("web_legacy_candidate")
                            ) if conversation else True,
                            round_limit=round_limit,
                            allow_partial_lead_answer=allow_partial_lead_answer,
                        )
                    elif mode == "work":
                        if conversation and not project_id:
                            raise swarm_lab.SwarmError(
                                "Select the project this chat should write to first."
                            )
                        answer = swarm_work.work_together(
                            config, board_payload, agent_id,
                            str(work_text), body.get("attachments"),
                            progress=progress, live_turn=live_turn,
                            peer_id=peer_id, project_id=project_id,
                            filed_as=filed_as,
                            conversation_key=conversation_key,
                            prefer_existing_conversation=bool(
                                conversation.get("web_legacy_candidate")
                            ) if conversation else True,
                            round_limit=round_limit,
                            resume_session_id=str(body.get("resume_session_id") or ""),
                            user_answers=work_answers,
                            allowed_write_roots=body.get("allowed_write_roots"),
                            reset_context_tool_execution_budget=(
                                body.get("reset_context_tool_execution_budget") is True
                            ),
                        )
                    elif mode == "chat":
                        progress(
                            f"Waiting for {one.name}",
                            f"Nexus sent the request through the {one.who} route."
                        )
                        answer = chat_lab.say(
                            config,
                            one.who,
                            objective_text,
                            filed_as=filed_as,
                            context=swarm_work.board_context(
                                board_payload, agent_id, peer_id, project_id,
                                participant_ids=(
                                    saved_participant_ids if conversation else None
                                ),
                            ),
                            attachments=body.get("attachments"),
                            speaker=one.to_dict() if conversation else None,
                            # This explicit action is intentionally isolated:
                            # the selected agent is the only recipient, even
                            # when the transcript belongs to a connected pair.
                            recipients=[one.to_dict()] if conversation else None,
                            conversation_key=conversation_key,
                            prefer_existing_conversation=bool(
                                conversation.get("web_legacy_candidate")
                            ) if conversation else True,
                        )
                    else:
                        raise swarm_lab.SwarmError("That chat action is not recognised.")
                    # Every mode handler returns only after its transcript write
                    # succeeds. From this point onward an exception belongs to
                    # the activity/run-journal finalisation path; it must not
                    # append a false "no answer was saved" turn or duplicate the
                    # user's prompt.
                    answer_saved = True
                    participant_outcome = answer.get("participant_outcome")
                    outcome_kind = str(
                        participant_outcome.get("outcome") or ""
                    ) if isinstance(participant_outcome, dict) else ""
                    if outcome_kind == "partial":
                        terminal_stage = "Some agents need attention"
                        terminal_detail = (
                            "Nexus saved every available answer and the exact "
                            "participant status in the conversation."
                        )
                    elif outcome_kind == "none":
                        terminal_stage = "No agent answered"
                        terminal_detail = (
                            "Nexus saved the recoverable participant status. "
                            "No uncertain provider turn was resent."
                        )
                    else:
                        terminal_stage = "Answer received"
                        terminal_detail = (
                            "Nexus saved the conversation and is updating the chat."
                        )
                    activity.update(
                        activity_id, terminal_stage, terminal_detail,
                        state="complete",
                    )
                    run_store.event(run_id, "progress", {
                        "stage": terminal_stage,
                        "detail": terminal_detail,
                    })
                    response = dict(
                        answer, agent=one.to_dict(), routing=routing,
                        conversation=conversation, run_id=run_id,
                        request_id=request_id,
                    )
                    run_store.checkpoint(
                        run_id, "response_ready", response
                    )
                    run_store.finish(run_id, response)
                    if goal_queue_claimed:
                        self.server.swarm_goal_queue.record_result(
                            goal_queue_id, goal_item_id, response, run_id=run_id
                        )
                    response_to_deliver = response
                except Exception as exc:
                    if isinstance(exc, swarm_work.ResumableSwarmError):
                        response = dict(
                            exc.payload,
                            agent=one.to_dict() if one is not None else {},
                            routing=routing,
                            conversation=conversation,
                            run_id=run_id,
                            request_id=request_id,
                        )
                        answer_saved = True
                        try:
                            cancellation.checkpoint()
                            if run_id and run_store.should_stop(run_id):
                                raise cancellation.ChatCancelled(
                                    cancellation.STOPPED_MESSAGE
                                )
                            activity.update(
                                activity_id, "Paused for recovery", str(exc),
                                state="complete",
                            )
                            if run_id:
                                run_store.checkpoint(run_id, "paused", response)
                                run_store.finish(run_id, response)
                        except Exception as finalization_error:
                            # Stop can win between producing a resumable payload
                            # and committing its terminal projection. Feed that
                            # losing transition through the common classifier
                            # below so the durable run cannot remain `stopping`.
                            exc = finalization_error
                        else:
                            if goal_queue_claimed:
                                self.server.swarm_goal_queue.record_result(
                                    goal_queue_id, goal_item_id, response,
                                    run_id=run_id,
                                )
                            response_to_deliver = response
                            return
                    stopped = isinstance(exc, cancellation.ChatCancelled) or bool(
                        cancel_token is not None and cancel_token.cancelled
                    )
                    if run_id and not stopped:
                        try:
                            # Stop can win before the local cancellation token
                            # exists (accepted -> running), or at a durable
                            # post-provider mutation fence. Classify those
                            # races from the signed run state instead of the
                            # incidental HarnessError raised by the losing
                            # transition.
                            stopped = run_store.should_stop(run_id)
                        except Exception:
                            # The original exception still fails this request
                            # closed. Do not invent a user cancellation when
                            # durable state itself cannot be verified.
                            pass
                    failure_message = (
                        cancellation.STOPPED_MESSAGE if stopped else str(exc)
                    )
                    activity.update(
                        activity_id, "Stopped" if stopped else "Request stopped",
                        failure_message if isinstance(exc, HarnessError)
                        else "Nexus could not finish this request. The chat will show the safe error details.",
                        state="stopped" if stopped else "error",
                    )
                    if run_id:
                        # If the provider effect is already acknowledged, this
                        # is a known local protocol/validation failure—not an
                        # unknown provider outcome. The run store records that
                        # receipt atomically while genuine uncertain deliveries
                        # remain fail-closed.
                        run_store.fail(
                            run_id, failure_message, stopped=stopped,
                            acknowledged_outcome=not isinstance(
                                exc, ProviderOutcomeUnknown
                            ),
                        )
                    if goal_queue_claimed:
                        self.server.swarm_goal_queue.record_failure(
                            goal_queue_id, goal_item_id, failure_message
                        )
                    if (
                        chat_owned and not answer_saved
                        and config is not None and one is not None
                    ):
                        failed_run = run_store.get(run_id) if run_id else {}
                        try:
                            chat_lab.keep_failed_exchange(
                                config,
                                str(one.who or ""),
                                str(body.get("text") or ""),
                                failure_message,
                                filed_as=filed_as,
                                attachments=body.get("attachments")
                                if isinstance(body.get("attachments"), list) else [],
                                contributions=list(
                                    activity.read(activity_id).get("turns") or []
                                ) if activity_id else [],
                                run_id=run_id,
                                state=str(failed_run.get("status") or (
                                    "stopped" if stopped else "failed"
                                )),
                            )
                        except Exception:
                            # The original failure remains authoritative. A
                            # secondary transcript-write problem must not hide
                            # it or turn an uncertain delivery into a resend.
                            pass
                    if stopped and not isinstance(exc, cancellation.ChatCancelled):
                        raise cancellation.ChatCancelled(
                            cancellation.STOPPED_MESSAGE
                        ) from exc
                    raise
                finally:
                    release_chat_ownership()
                    if response_to_deliver is not None:
                        # Socket delivery is deliberately outside the execution
                        # classifier above. A page closing after a durable
                        # result/replay must not turn an existing running or
                        # completed run, activity, or goal into a failure.
                        self._json(response_to_deliver, response_delivery_status)
            elif self.path == "/api/team/connect":
                # One press to make an assistant usable. Somebody had Claude
                # installed and signed in, an agent set to use it, and the board
                # still said not ready - because nothing in the settings pointed
                # at it by name, and the only way to say so was a settings file
                # or a terminal. They had to ask somebody else for help with
                # their own machine.
                wanted = str(body.get("kind") or "").strip()
                if wanted not in seat_setup.KNOWN_SEATS:
                    raise HarnessError(
                        f"{wanted or 'that'} is not an assistant this can set up on its own. "
                        f"It knows: {', '.join(seat_setup.KNOWN_SEATS)}."
                    )
                # Route writes already have their own narrow, atomic lock. Do
                # not queue this behind full seat discovery: version probes can
                # be slow or stuck in a provider tool, and this button's job is
                # to write one route immediately.
                with self.server.swarm_lock:
                    self.server.require_config_reload_boundary()
                    settings = self._settings_now()
                    name = seat_setup.ROUTE_NAMES.get(wanted, wanted)
                    # One route added, and nothing else touched. Setting a seat up
                    # the usual way also picks it as the assistant used by default,
                    # which is right for somebody choosing their first one and
                    # wrong here: connecting Gemini so one agent can use it should
                    # not quietly move everything else onto Gemini.
                    route = seat_setup.routes_for(settings, [wanted])[name]
                    # Google will not answer a work account until it is told which
                    # Cloud project to bill. Taken here, at the moment of connecting,
                    # rather than leaving somebody to find out from a refusal and
                    # then go looking for the setting.
                    project = str(body.get("google_project") or "").strip()
                    if project and wanted == "gemini-cli":
                        route["google_project"] = project
                    done = seat_setup.write_one_route(settings, name, route)
                    # Reload the complete server revision, not only its config
                    # field. If an existing untrusted file needs a deliberate
                    # approval, that approval endpoint performs this reload.
                    if not done.needs_your_say:
                        self.server.reload_config()
                from .providers.subscription_cli import connection_status

                # Login probing is the separate explicit "check" action. A
                # connect response must not wait ten seconds—or forever on a
                # broken third-party CLI—after the route is already written.
                connection = connection_status(wanted, probe=False)
                self._json({
                    "route": name,
                    "trusted": done.trusted,
                    "note": done.note,
                    "needs_your_say": done.needs_your_say,
                    **connection,
                })
            elif self.path == "/api/team/login":
                # This is intentionally a separate, explicit press from adding
                # a route. Connecting settings must never pop up an account
                # window by surprise. The provider CLI owns the window and the
                # credentials; the harness captures neither.
                wanted = str(body.get("route") or body.get("kind") or "").strip()
                if wanted in seat_setup.KNOWN_SEATS:
                    # Backwards compatibility for the pre-board seat picker,
                    # which can offer an installed CLI before a route exists.
                    from .providers.subscription_cli import start_interactive_login

                    self._json(start_interactive_login(wanted))
                else:
                    from .providers.connection import start_interactive_login

                    self._json(start_interactive_login(self.server.config, wanted))
            elif self.path == "/api/team/check-login":
                wanted = str(body.get("route") or body.get("kind") or "").strip()
                if wanted in seat_setup.KNOWN_SEATS:
                    # Kept for callers of the older kind-oriented endpoint.
                    from .providers.subscription_cli import connection_status

                    self._json(connection_status(wanted, use_cache=False, probe=True))
                else:
                    from .providers.connection import connection_status

                    web_connection = (
                        self.server.web_chats.route(wanted)
                        if wanted.startswith("web:") else None
                    )
                    self._json(connection_status(
                        self.server.config,
                        wanted,
                        web_connection=web_connection,
                    ))
            elif self.path == "/api/team/repair-plan":
                # Diagnosis is deliberately separate from verification. This
                # route may run a provider-owned status command, but it never
                # sends a model prompt or spends a model request.
                wanted = str(body.get("route") or "").strip()
                web_connection = (
                    self.server.web_chats.route(wanted)
                    if wanted.startswith("web:") else None
                )
                self._json(provider_repair.repair_plan(
                    self.server.config,
                    wanted,
                    web_connection=web_connection,
                ))
            elif self.path == "/api/team/test-route":
                # A live request is the only general proof that authentication,
                # routing and the selected model all work together. It happens
                # only after an explicit press, in an empty temporary folder,
                # and it owns a cancellable key per exact route.
                wanted = str(body.get("route") or "").strip()
                web_connection = (
                    self.server.web_chats.route(wanted)
                    if wanted.startswith("web:") else None
                )
                before = provider_repair.repair_plan(
                    self.server.config,
                    wanted,
                    web_connection=web_connection,
                )
                allowed = {
                    str(action.get("id") or "")
                    for action in (before.get("repair") or {}).get("actions", [])
                    if isinstance(action, dict)
                }
                if "live-test" not in allowed:
                    raise HarnessError(
                        "Finish the repair step shown for this route before running a live test."
                    )
                chat_key = f"connection-test:{wanted}"
                cancel_token = self.server.chat_cancellations.begin(chat_key)
                try:
                    with tempfile.TemporaryDirectory(prefix="nexus-connection-test-") as empty:
                        with cancellation.use(cancel_token):
                            answered = chat_lab.ask_once(
                                self.server.config,
                                wanted,
                                "This is a Nexus Harness connection test. Do not use tools, "
                                "inspect files, or change anything. Reply with the single word READY.",
                                context=(
                                    "CONNECTION TEST ONLY. The working directory is an empty temporary "
                                    "folder. Do not inspect, create, edit, or execute files."
                                ),
                                conversation_key=f"connection-test-{uuid.uuid4().hex}",
                                prefer_existing_conversation=False,
                                working_directory=empty,
                            )
                    # Re-run the free diagnosis after success, then make the
                    # live proof explicit without returning model text.
                    web_connection = (
                        self.server.web_chats.route(wanted)
                        if wanted.startswith("web:") else None
                    )
                    checked = provider_repair.repair_plan(
                        self.server.config,
                        wanted,
                        web_connection=web_connection,
                    )
                    self._json({
                        "route": wanted,
                        "answered": True,
                        "milliseconds": int(answered.get("milliseconds") or 0),
                        "plan": provider_repair.verified_plan(
                            checked, int(answered.get("milliseconds") or 0)
                        ),
                    })
                finally:
                    self.server.chat_cancellations.finish(chat_key, cancel_token)
            elif self.path == "/api/team/stop-route-test":
                wanted = str(body.get("route") or "").strip()
                stopped, _activity = self.server.chat_cancellations.stop(
                    f"connection-test:{wanted}"
                )
                self._json({
                    "stopped": stopped,
                    "note": "Stopping the live test." if stopped else "This route has no live test running.",
                })
            elif self.path == "/api/team/set-google-project":
                # Update the exact named Gemini route, not the default Gemini
                # route. People can have work and personal routes side by side.
                wanted = str(body.get("route") or "").strip()
                project = str(body.get("google_project") or "").strip()
                if not project:
                    raise HarnessError("Enter the Google Cloud Project ID first.")
                with self.server.swarm_lock:
                    self.server.require_config_reload_boundary()
                    routes = self.server.config.data.get("providers") or {}
                    existing = routes.get(wanted) if isinstance(routes, dict) else None
                    if not isinstance(existing, dict) or str(
                        existing.get("kind") or existing.get("name") or ""
                    ) != "gemini-cli":
                        raise HarnessError("That exact route is not a configured Gemini route.")
                    updated = dict(existing)
                    updated["google_project"] = project
                    done = seat_setup.write_one_route(
                        self.server.config, wanted, updated
                    )
                    if not done.needs_your_say:
                        self.server.reload_config()
                self._json({
                    "route": wanted,
                    "saved": True,
                    "note": done.note,
                    "needs_your_say": done.needs_your_say,
                })
            elif self.path == "/api/team/repair-claude":
                # A separate explicit press, with a confirmation in the panel.
                # The visible provider terminal owns the update and OAuth flow;
                # this server captures no output and no account information.
                from .providers.subscription_cli import start_claude_repair

                wanted = str(body.get("route") or "").strip()
                fingerprint = str(body.get("diagnosis_fingerprint") or "").strip()
                with self.server.swarm_lock:
                    # Repair logs the provider CLI out, so a stale button must
                    # never target whichever Claude command happens to be first
                    # on PATH. Recompute the free diagnosis and require the
                    # exact action/fingerprint the panel was shown.
                    plan = provider_repair.repair_plan(self.server.config, wanted)
                    repair = plan.get("repair") if isinstance(plan, dict) else {}
                    actions = repair.get("actions") if isinstance(repair, dict) else []
                    offered = next((
                        one for one in actions
                        if isinstance(one, dict)
                        and one.get("id") == "repair-claude"
                        and one.get("route") == wanted
                    ), None)
                    current_fingerprint = str(
                        repair.get("diagnosis_fingerprint") or ""
                    ) if isinstance(repair, dict) else ""
                    if (
                        offered is None or not fingerprint
                        or fingerprint != current_fingerprint
                        or fingerprint != str(offered.get("diagnosis_fingerprint") or "")
                    ):
                        raise HarnessError(
                            "This repair diagnosis changed. Press Repair connection and check the route again before signing it out."
                        )
                    routed = (
                        self.server.config if wanted == "default" else
                        ProviderRegistry(self.server.config).provider_config(wanted)
                    )
                    if str(routed.get("provider.name") or "") != "claude-cli":
                        raise HarnessError("That exact route is not a configured Claude route.")
                    result = start_claude_repair(
                        command=list(routed.get("provider.command") or []) or None
                    )
                self._json({**result, "route": wanted})
            elif self.path == "/api/swarm/the-page":
                # The page every agent on one project writes to. Read through
                # the same door the run uses, so what the panel shows is what
                # the agents saw.
                from . import pages as pages_lab

                folder = str(body.get("folder") or "").strip()
                if not folder:
                    raise HarnessError("Which project folder's page? None was named.")
                try:
                    before = int(body.get("before") or 0)
                    page_limit = int(body.get("limit") or 20)
                except (TypeError, ValueError) as exc:
                    raise HarnessError("The shared-page window is invalid.") from exc
                held = pages_lab.page_window(
                    self.server.config,
                    folder,
                    str(body.get("name") or ""),
                    before=before,
                    limit=page_limit,
                )
                self._json(held)
            elif self.path == "/api/swarm/page-part":
                from . import pages as pages_lab

                folder = str(body.get("folder") or "").strip()
                if not folder:
                    raise HarnessError("Which project folder's page? None was named.")
                try:
                    number = int(body.get("number") or 0)
                except (TypeError, ValueError) as exc:
                    raise HarnessError("The shared-page part number is invalid.") from exc
                self._json(pages_lab.page_part(
                    self.server.config, folder, number,
                    str(body.get("name") or ""),
                ))
            elif self.path == "/api/swarm/where-it-stands":
                from . import pages as pages_lab

                self._json(pages_lab.where_it_stands(
                    self.server.config,
                    str(body.get("folder") or "").strip(),
                    str(body.get("text") or ""),
                    str(body.get("instead_of") or ""),
                    str(body.get("name") or ""),
                ))
            elif self.path == "/api/swarm/add-to-the-page":
                # The person is a writer on the page too. Filed as "you", so
                # anybody reading it later can tell which parts were theirs.
                from . import pages as pages_lab

                self._json(pages_lab.add_to_the_page(
                    self.server.config,
                    str(body.get("folder") or "").strip(),
                    who=str(body.get("who") or "You"),
                    author_id="person",
                    text=str(body.get("text") or ""),
                    what_they_were_doing="typed in by the person",
                    after=int(body.get("after") or 0),
                    name=str(body.get("name") or ""),
                ))
            elif self.path == "/api/swarm/put-the-page-away":
                from . import pages as pages_lab

                self._json(pages_lab.put_the_page_away(
                    self.server.config,
                    str(body.get("folder") or "").strip(),
                    str(body.get("name") or ""),
                ))
            elif self.path == "/api/swarm/import-kept":
                raw = body.get("json")
                document = body.get("document")
                if raw is None and isinstance(document, dict):
                    raw = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
                if not isinstance(raw, str):
                    raise HarnessError("Choose a JSON board file to import.")
                try:
                    raw_bytes = raw.encode("utf-8", errors="strict")
                except UnicodeEncodeError as exc:
                    raise HarnessError(
                        "That saved-board file is not valid UTF-8 text. Nothing was imported."
                    ) from exc
                if len(raw_bytes) > swarm_lab.MAX_SAVED_BOARD_DOCUMENT_BYTES:
                    raise HarnessError(
                        f"A saved-board JSON file may be at most "
                        f"{swarm_lab.MAX_SAVED_BOARD_DOCUMENT_BYTES:,} UTF-8 bytes. "
                        "Nothing was imported."
                    )
                try:
                    document = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise HarnessError(
                        "That file is not valid JSON. Nothing was imported."
                    ) from exc
                except RecursionError as exc:
                    raise HarnessError(
                        "That saved-board file is nested too deeply to read. Nothing was imported."
                    ) from exc
                with self.server.swarm_lock:
                    saved = swarm_lab.import_kept_board(
                        document, str(body.get("name") or "")
                    )
                    saved["kept"], saved["kept_problems"] = (
                        swarm_lab.kept_board_inventory()
                    )
                self._json(saved)
            elif self.path == "/api/swarm/keep":
                # Saving the board as it stands, under a name. One board came
                # back on its own and only ever one, so a second arrangement
                # meant taking the first apart and building it again from
                # memory on Monday.
                with self.server.swarm_lock:
                    said = swarm_lab.keep_this_board(
                        str(body.get("name") or ""), self.server.config)
                    said["kept"], said["kept_problems"] = (
                        swarm_lab.kept_board_inventory()
                    )
                self._json(said)
            elif self.path == "/api/swarm/open-kept":
                with self.server.swarm_lock:
                    swarm_lab.open_this_board(
                        str(body.get("name") or ""), self.server.config)
                    # Opening a local snapshot must finish at local-disk speed.
                    # Provider discovery starts installed CLIs and can take
                    # longer than the HTTP request deadline, leaving the user
                    # with a board that opened on disk while the button looked
                    # broken. Return configured/cached routes now; the renderer
                    # performs the full discovery as a background refresh.
                    said = self.server.swarm_standing()
                    said["what_is_not_ready"] = swarm_lab.what_is_not_ready(
                        self.server.config, said)
                    said["kept"], said["kept_problems"] = (
                        swarm_lab.kept_board_inventory()
                    )
                    self.server.decorate_swarm_authority(said)
                self._json(said)
            elif self.path == "/api/swarm/forget-kept":
                with self.server.swarm_lock:
                    swarm_lab.forget_this_board(
                        str(body.get("name") or ""), self.server.config)
                    kept, problems = swarm_lab.kept_board_inventory()
                    self._json({"kept": kept, "kept_problems": problems})
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
                    standing = self.server.swarm_standing()
                    board = swarm_lab.read_it(standing["board"])
                agent_id = str(body.get("agent") or "")
                one = swarm_lab.the_agent(board, agent_id)
                chat_id = str(body.get("chat") or "")
                chat_key = chat_id or agent_id
                with self.server.swarm_communication_runs.conversation_turn(
                    f"chat-reset-{uuid.uuid4().hex}", chat_key, timeout=0.0,
                ):
                    conversation = swarm_chats.resolve(
                        self.server.config, standing["board"], agent_id, chat_id
                    ) if chat_id else None
                    destination = (
                        conversation.get("destination", {}) if conversation else {}
                    )
                    old_web_key = str(
                        conversation.get("web_conversation_key")
                        or conversation.get("filed_as") or ""
                    ) if conversation else ""
                    web_routes = list(dict.fromkeys(
                        str(member.get("who") or "")
                        for member in (conversation or {}).get("pair_agents", [])
                        if isinstance(member, dict)
                        and str(member.get("who") or "").startswith("web:")
                    ))[:2]
                    note = chat_lab.start_again(
                        self.server.config, one.who,
                        str(conversation["filed_as"]) if conversation
                        else one.filed_as_name or swarm_lab.filed_as(one.name)
                    )
                    if conversation and web_routes:
                        conversation = swarm_chats.restart_provider_conversation(
                            self.server.config, standing["board"], agent_id, chat_id,
                        )
                        destination = conversation.get("destination", {})
                    web_chat_resets = [
                        {
                            "route": web_route,
                            "previous_web_conversation_key": old_web_key,
                        }
                        for web_route in web_routes
                        if old_web_key
                    ]
                    primary_web_id = (
                        web_routes[0].removeprefix("web:") if web_routes else
                        str(destination.get("web_chat_id") or "")
                    )
                    self._json({
                        "note": note,
                        "said": [],
                        "conversation": conversation,
                        "web_chat_id": primary_web_id,
                        "web_conversation_key": str(
                            (conversation or {}).get("web_conversation_key")
                            or destination.get("web_conversation_key") or ""
                        ),
                        "previous_web_conversation_key": (
                            old_web_key if web_routes else ""
                        ),
                        "web_chat_resets": web_chat_resets,
                    })
            elif self.path == "/api/swarm/start":
                # The lock is held only while the board is read and the run is
                # marked as going, which is all start does - the asking happens
                # on a thread of its own afterwards. Held for that much, no save
                # can slip in between the board being read and the run owning
                # it; held for the whole run, every window looking at the board
                # would freeze for as long as the assistants took.
                # Readiness discovery is slow machine I/O, not part of the
                # atomic board snapshot/start boundary.
                self.server.refresh_swarm_provider_status()
                with self.server.project_admission_lock, self.server.swarm_lock:
                    request_id = str(body.get("request_id") or uuid.uuid4().hex)
                    standing = self.server.swarm_standing()
                    for project in standing.get("board", {}).get("projects", []):
                        if isinstance(project, dict) and project.get("is_there") is True \
                                and str(project.get("path") or ""):
                            self.server.require_no_long_horizon_path(
                                Path(str(project["path"]))
                            )
                    doing = self.server.swarm_runner.start(
                        self.server.config, standing, request_id)
                self._json({
                    "doing": doing,
                    "run_id": str(doing.get("run_id") or ""),
                    "request_id": str(doing.get("request_id") or request_id),
                })
            elif self.path == "/api/swarm/stop-chat":
                agent_id = str(body.get("agent") or "")
                if not agent_id:
                    raise HarnessError("A chat agent ID is required")
                activity_id = str(body.get("activity") or "")
                requested_run = str(body.get("run_id") or activity_id)
                chat_id = str(body.get("chat") or "")
                active_identity = activity_id
                try:
                    run_store, found_run = self.server.find_swarm_run(requested_run)
                    snapshot = found_run.get("snapshot")
                    snapshot = snapshot if isinstance(snapshot, dict) else {}
                    requested_chat_key = chat_id or agent_id
                    if str(snapshot.get("chat_key") or "") != requested_chat_key:
                        raise HarnessError(
                            "That exact Swarm run belongs to a different chat; nothing was stopped."
                        )
                    durable = run_store.request_stop(str(found_run.get("run_id") or requested_run))
                    active_identity = str(durable.get("run_id") or requested_run)
                except HarnessError:
                    if str(body.get("run_id") or "").strip():
                        # A caller that supplied an exact durable identity does
                        # not get to fall back to a process-local activity. A
                        # stale run ID paired with the current activity ID must
                        # never stop the newer run.
                        raise
                    durable = None
                durable_status = str((durable or {}).get("status") or "")
                if durable is None or durable_status == "stopping":
                    locally_stopped, active_activity = self.server.chat_cancellations.stop(
                        chat_id or agent_id, active_identity
                    )
                else:
                    # A terminal durable run is authoritative. In particular,
                    # never cancel a residual local token after the run already
                    # completed or failed; that token may be between response
                    # finalisation and cleanup.
                    locally_stopped = False
                    active_activity = ""
                stopped = locally_stopped or durable_status in {"stopping", "stopped"}
                if (locally_stopped or durable_status == "stopping") and active_activity:
                    self.server.chat_activities.update(
                        active_activity, "Stopping", "Nexus is interrupting this chat request.",
                        state="stopping",
                    )
                note = (
                    "Stopping this chat." if durable_status == "stopping" or locally_stopped
                    else "This chat is already stopped." if durable_status == "stopped"
                    else "This chat is not waiting for an answer."
                )
                self._json({
                    "stopped": stopped,
                    "activity": active_activity,
                    "run_id": active_identity if durable is not None else "",
                    "note": note,
                })
            elif self.path == "/api/swarm/stop":
                run_id = str(body.get("run_id") or "").strip()
                note = self.server.swarm_runner.stop(run_id)
                self._json({
                    "note": note,
                    "run_id": run_id,
                    "doing": self.server.swarm_runner.how_it_is_going(run_id),
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
                timer_value = body.get("timer")
                if not isinstance(timer_value, dict) or timer_value.get("turned_on") is not False:
                    self.server.require_project_execution_authority()
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
                turning_on = bool(body.get("turned_on"))
                if turning_on:
                    self.server.require_project_execution_authority()
                # Only the on-off switch, flipped where the timer is kept.
                # Sending the whole timer back from a panel that had been open
                # a while put back the old time and the old automation with it.
                with self.server.pipelines_lock:
                    one = timer_lab.load(
                        self.server.config, str(body.get("name") or "")
                    )
                    one.turned_on = turning_on
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
                self.server.require_project_execution_authority()
                self._json(self.server.run_manual_timer(
                    str(body.get("name") or ""),
                    str(body.get("request_id") or ""),
                ))
            elif self.path == "/api/chat/say":
                # The conversation has its own provider lock; admission also
                # pins the current project until this provider turn finishes.
                # This endpoint cannot run project commands or mutate project
                # files, so copied-project execution authority is irrelevant.
                who = str(body.get("who") or "")
                chat_key = f"talk:{who}"
                # Claim the individual turn first so a duplicate request is
                # refused immediately instead of queueing behind project
                # admission and silently becoming a second provider turn.
                cancel_token = self.server.chat_cancellations.begin(chat_key)
                try:
                    with self.server.project_admission_lock:
                        config = self.server.config
                        with cancellation.use(cancel_token):
                            answer = chat_lab.say(
                                config, who, str(body.get("text") or ""),
                            )
                finally:
                    self.server.chat_cancellations.finish(chat_key, cancel_token)
                # Provider completion is the project boundary.  Socket writes
                # can outlive the request observed by the client, especially
                # on a loaded runner, but they no longer read project state.
                # Do not make unrelated project moves wait for response I/O.
                self._json(answer)
            elif self.path == "/api/chat/ask-everyone":
                # Every one of them, at the same time. Six one after another is
                # six waits.
                chat_key = "talk:everyone"
                cancel_token = self.server.chat_cancellations.begin(chat_key)
                try:
                    with self.server.project_admission_lock:
                        config = self.server.config
                        with cancellation.use(cancel_token):
                            answers = chat_lab.ask_everyone(
                                config, str(body.get("text") or "")
                            )
                finally:
                    self.server.chat_cancellations.finish(chat_key, cancel_token)
                self._json({"answers": answers})
            elif self.path == "/api/chat/stop":
                who = str(body.get("who") or "")
                chat_key = "talk:everyone" if body.get("everyone") is True else f"talk:{who}"
                stopped, _activity = self.server.chat_cancellations.stop(chat_key)
                self._json({
                    "stopped": stopped,
                    "note": "Stopping this chat." if stopped else "This chat is not waiting for an answer.",
                })
            elif self.path == "/api/chat/start-again":
                route = str(body.get("who") or "")
                destination = chat_lab.chat_destination(self.server.config, route)
                self._json({
                    "note": chat_lab.start_again(
                        self.server.config, route
                    ),
                    "said": [],
                    "web_chat_id": str(destination.get("web_chat_id") or ""),
                    "web_conversation_key": str(
                        destination.get("web_conversation_key") or ""
                    ),
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
            elif self.path == "/api/pipelines/create":
                with self.server.pipelines_lock:
                    created = pipeline_lab.create_blank(
                        self.server.config, str(body.get("name") or "")
                    )
                    saved, saved_problems = pipeline_lab.saved_inventory(self.server.config)
                    self._json({
                        "pipeline": created,
                        "saved": saved,
                        "saved_problems": saved_problems,
                    })
            elif self.path == "/api/pipelines/import":
                with self.server.pipelines_lock:
                    written = body.get("json")
                    if written is None and isinstance(body.get("document"), dict):
                        written = json.dumps(
                            body["document"], ensure_ascii=False, separators=(",", ":")
                        )
                    imported = pipeline_lab.import_document(
                        self.server.config,
                        written,
                        name=str(body.get("name") or ""),
                    )
                    saved, saved_problems = pipeline_lab.saved_inventory(self.server.config)
                    self._json({
                        "pipeline": imported,
                        "saved": saved,
                        "saved_problems": saved_problems,
                        "note": f"Imported and saved {imported['name']}.",
                    })
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
                    note = pipeline_lab.remove(
                        self.server.config, str(body.get("name") or "")
                    )
                    saved, saved_problems = pipeline_lab.saved_inventory(self.server.config)
                    self._json({
                        "note": note,
                        "saved": saved,
                        "saved_problems": saved_problems,
                    })
            elif self.path == "/api/pipelines/check":
                # Says whether a drawing would run, without running any of it.
                self._json({"pipeline": pipeline_lab.read_it(body.get("pipeline"))})
            elif self.path == "/api/pipelines/stop":
                run_id = str(body.get("run_id") or "").strip()
                if not run_id:
                    active = self.server.pipeline_store.active()
                    if active is None:
                        # Compatibility for an idle old panel: no run can be
                        # affected, and the next accepted run clears this flag.
                        self.server.pipeline_stop = True
                        self._json({"note": "There is no active automation to stop."})
                        return
                    raise HarnessError("Stop must name the exact active automation run_id.")
                stopped = self.server.pipeline_store.request_stop(run_id)
                if run_id == self.server.pipeline_active_run_id:
                    self.server.pipeline_stop = True
                self._json({
                    "run_id": run_id,
                    "state": stopped["state"],
                    "note": "Stop was accepted for this exact automation run.",
                })
            elif self.path == "/api/pipelines/answer":
                # Somebody answering a step that stopped to ask. Nothing else
                # reads these, and they are cleared at the start of every run.
                step = str(body.get("step") or "")
                run_id = str(body.get("run_id") or "").strip()
                if not run_id:
                    raise HarnessError("An answer must name the exact automation run_id.")
                if not step:
                    raise HarnessError("Say which step is being answered.")
                decided = self.server.pipeline_store.decide(
                    run_id, step, bool(body.get("carry_on"))
                )
                if run_id == self.server.pipeline_active_run_id:
                    self.server.pipeline_answers[step] = decided["carry_on"]
                self._json({
                    "run_id": run_id,
                    "step": step,
                    "carry_on": decided["carry_on"],
                    "note": (
                        "Carrying on." if decided["carry_on"]
                        else "Stopping there. Nothing after it will run."
                    ),
                })
            elif self.path in ("/api/pipelines/run", "/api/pipelines/agent-run"):
                local_lock_held = False
                replay_response: dict[str, Any] | None = None
                try:
                    with self.server.project_admission_lock:
                        config = self.server.config
                        project_root = config.project_root.resolve(strict=True)
                        self.server.require_project_execution_authority(project_root)
                        self.server.require_no_long_horizon_path(project_root)
                        if self.path == "/api/pipelines/agent-run":
                            automation = str(body.get("automation") or "").strip()
                            if not automation or any(
                                body.get(key) for key in ("from_here", "only")
                            ):
                                raise HarnessError(
                                    "Agent runs require exactly one saved automation name."
                                )
                            with self.server.pipelines_lock:
                                drawn = pipeline_lab.load(config, automation)
                        else:
                            drawn = pipeline_lab.read_it(body.get("pipeline"))
                        # Resolve every nested definition before acceptance. The
                        # thread receives this immutable snapshot, never a later
                        # edit of a saved child automation.
                        frozen = pipeline_lab.freeze_definition(config, drawn)
                        store = self.server.pipeline_store
                        accepted, created = store.accept(
                            frozen,
                            source=(
                                "desktop-agent"
                                if self.path == "/api/pipelines/agent-run" else "panel"
                            ),
                            request_id=str(body.get("request_id") or ""),
                        )
                        if not created:
                            # Acceptance is complete now. Do not keep the
                            # project-admission lease while writing bytes to a
                            # client: the client can finish reading before the
                            # handler unwinds the surrounding context manager,
                            # which made a move immediately after a replay
                            # response fail intermittently on slower machines.
                            replay_response = {
                                "accepted": True,
                                "replayed": True,
                                "run_id": accepted["run_id"],
                                "name": accepted["name"],
                            }
                        else:
                            if not self.server.pipeline_lock.acquire(blocking=False):
                                store.fail(
                                    accepted["run_id"],
                                    accepted["attempt_id"],
                                    "A local automation worker is running without a matching coordinator lease.",
                                )
                                raise HarnessError(
                                    "A pipeline is running already. Wait for it, or press Stop."
                                )
                            local_lock_held = True
                            kinds = dict(self.server.check_kinds)
                except Exception:
                    if local_lock_held:
                        self.server.pipeline_lock.release()
                    raise
                if replay_response is not None:
                    self._json(replay_response, HTTPStatus.ACCEPTED)
                    return
                run_id = accepted["run_id"]
                attempt_id = accepted["attempt_id"]
                self.server.pipeline_active_run_id = run_id
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
                server = self.server

                def go() -> None:
                    try:
                        store.start(run_id, attempt_id)
                        events.add({
                            "kind": "pipeline_started", "node": "pipeline", "run_id": run_id,
                            "payload": {"name": drawn["name"], "run_id": run_id},
                        })

                        def tell(event: dict[str, Any]) -> None:
                            event = {**event, "run_id": run_id}
                            payload = event.get("payload")
                            if isinstance(payload, dict):
                                event["payload"] = {**payload, "run_id": run_id}
                            store.append_event(run_id, attempt_id, event)
                            events.add(event)

                        def waiting_on(step: str):
                            answer = store.decision(run_id, step)
                            waiting = step if answer is None else ""
                            server.pipeline_waiting_at = waiting
                            current = store.get(run_id)
                            if current.get("waiting_at") != waiting and current["state"] != "stopping":
                                store.set_waiting(run_id, attempt_id, waiting)
                            return answer

                        run = pipeline_lab.run_it(
                            config, drawn, tell=tell, check_kinds=kinds,
                            stopping=lambda: store.should_stop(run_id),
                            from_here=from_here, only=only, answers=answers,
                            waiting_on=waiting_on,
                            run_id=run_id, frozen=frozen, decision_nonce=attempt_id,
                        )
                        finished = store.finish(run_id, attempt_id, run.to_dict())
                        server.pipeline_run = finished.get("result") or run.to_dict()
                        events.add({
                            "kind": "pipeline_finished", "node": "pipeline", "run_id": run_id,
                            "payload": server.pipeline_run,
                        })
                    except BaseException as exc:  # noqa: BLE001 - thread death must terminalize
                        # Nothing ran: a run that gets this far has fallen over
                        # before the first step, so there are no steps to
                        # report. Saying that plainly beats an empty list that
                        # reads as "nothing went wrong".
                        failed_result = {
                            "name": drawn.get("name", ""), "nodes": [], "passed": False,
                            "run_id": run_id, "outcome": "failed",
                            "said": (
                                f"The run stopped before any step ran, so nothing was "
                                f"checked: {exc}"
                            ),
                            "milliseconds": 0,
                        }
                        try:
                            finished = store.fail(
                                run_id, attempt_id, failed_result["said"]
                            )
                            server.pipeline_run = finished.get("result") or failed_result
                        except Exception:
                            server.pipeline_run = failed_result
                        events.add({
                            "kind": "pipeline_finished", "node": "pipeline", "run_id": run_id,
                            "payload": server.pipeline_run,
                        })
                    finally:
                        # However it ends, the next press has to work.
                        server.pipeline_running = False
                        server.pipeline_waiting_at = ""
                        if server.pipeline_active_run_id == run_id:
                            server.pipeline_active_run_id = ""
                        server.pipeline_lock.release()

                try:
                    threading.Thread(target=go, name="pipeline", daemon=True).start()
                except Exception:
                    self.server.pipeline_running = False
                    try:
                        store.fail(
                            run_id, attempt_id, "The automation worker could not be started."
                        )
                    except Exception:
                        pass
                    self.server.pipeline_active_run_id = ""
                    self.server.pipeline_lock.release()
                    raise
                self._json({
                    "accepted": True,
                    "run_id": run_id,
                    "name": drawn["name"],
                    "definition_digest": accepted["definition_digest"],
                }, HTTPStatus.ACCEPTED)
            elif self.path == "/api/settings/trust-anyway":
                # Deliberate, and only ever from a press. The panel shows the
                # whole file and what in it carries risk before offering this.
                with self.server.seats_lock:
                    with self.server.swarm_lock:
                        self.server.require_config_reload_boundary()
                        note = seat_setup.trust_it_anyway(
                            self._settings_now(), str(body.get("seen") or "")
                        )
                        self.server.reload_config()
                    self._json({"trusted": True, "note": note})
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
                self.server.require_project_execution_authority()
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
                self.server.require_project_execution_authority()
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
                self.server.require_project_execution_authority()
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
                self.server.require_project_execution_authority()
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

    def _run_task(
        self,
        task: str,
        dry_run: bool,
        graph: dict[str, Any] | None = None,
        bootstrap_tests: bool = False,
        accepted_config: LoadedConfig | None = None,
    ) -> None:
        try:
            config = accepted_config or self.server.config
            with HarnessApplication(config, self.server.events.add) as app:
                if bootstrap_tests:
                    self.server.events.add({
                        "kind": "bootstrap_milestone", "node": "verification",
                        "payload": {
                            "state": "required",
                            "summary": "Before completion, Nexus must create runnable tests and execute non-empty verification evidence.",
                        },
                    })
                result = app.run_task(task, dry_run=dry_run, graph=graph)
                if bootstrap_tests and not dry_run:
                    proof = app.test(check_kinds=("test",))
                    if not proof.get("passed"):
                        reasons = "; ".join(
                            str(item.get("reason")) for item in proof.get("verification_problems", [])
                        ) or "no runnable test command was created"
                        raise HarnessError(
                            "Bootstrap milestone was not met: Nexus may not claim verified completion until "
                            f"new test infrastructure runs real tests ({reasons})."
                        )
                    result["bootstrap_verification"] = proof
                    self.server.events.add({
                        "kind": "bootstrap_milestone", "node": "verification",
                        "payload": {
                            "state": "passed",
                            "summary": "Created test infrastructure produced non-empty passing verification evidence.",
                            "commands": proof.get("commands", []),
                        },
                    })
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
