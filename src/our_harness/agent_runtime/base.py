"""The shape every persistent agent session shares."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import events as ev

# Access modes, the same words the rest of Nexus uses.
ACCESS_MODES = ("read_only", "ask", "full")


@dataclass
class SeatSpec:
    """Everything needed to start (or resume) one agent's session.

    ``seat`` is the stable address (``agent@team``); the occupying CLI session
    can change (restart, resume) while the seat stays the same.
    """

    seat: str
    agent_id: str
    name: str
    kind: str                      # codex-cli | claude-cli | gemini-cli | copilot-cli
    command: list[str] | None
    model: str
    cwd: str
    access: str = "full"
    instructions: str = ""
    mcp: dict[str, Any] | None = None       # {"command": [...], "env": {...}}
    resume_id: str = ""
    extra_env: dict[str, str] = field(default_factory=dict)
    extra_dirs: list[str] = field(default_factory=list)   # more folders the agent may read (attachments)
    browser_settings: str = ""     # the team's settings.json; Claude's browser guard reads it
    state_dir: str = ""            # where the adapter may keep files it hands its CLI


class AgentSession:
    """One live CLI session. Subclasses implement the transport.

    ``on_event`` receives every normalized event. ``send`` starts a turn (or,
    when a turn is running, adds the message to it if the CLI can take input
    mid-turn, else queues it for the next turn). ``busy`` tells whether a turn
    is running.
    """

    can_steer = False

    def __init__(self, spec: SeatSpec, on_event: Callable[[dict[str, Any]], None]):
        self.spec = spec
        self._emit_to = on_event
        self.session_id = ""
        self.state = "starting"
        self.resume_outcome = ""        # resumed | fresh | failed (after a resume attempt)
        self.error = ""
        self.lock = threading.RLock()
        self.turn_active = threading.Event()
        self.turn_id = ""
        self.last_activity = time.time()
        self.pending: list[str] = []
        self.turns_completed = 0

    # -- helpers for subclasses

    def emit(self, kind: str, **fields: Any) -> None:
        self.last_activity = time.time()
        try:
            self._emit_to(ev.event(kind, agent=self.spec.agent_id, seat=self.spec.seat,
                                   session=self.session_id or None, turn=self.turn_id or None, **fields))
        except Exception:
            pass

    def _set_state(self, state: str, **fields: Any) -> None:
        self.state = state
        self.emit("session", state=state, resume=self.resume_outcome or None, **fields)

    def _turn_finished(self, status: str, **fields: Any) -> None:
        with self.lock:
            self.turn_active.clear()
            self.turns_completed += 1
            follow = self.pending.pop(0) if self.pending else None
        self.emit("turn", status=status, **fields)
        self.turn_id = ""
        if follow is not None and self.state not in ("closed", "error"):
            self.send(follow)

    # -- API

    @property
    def busy(self) -> bool:
        return self.turn_active.is_set()

    def start(self) -> None:
        raise NotImplementedError

    def send(self, text: str) -> str:
        """Deliver user or teammate text. Returns 'started', 'steered' or 'queued'."""
        with self.lock:
            if self.state in ("closed", "error"):
                raise RuntimeError(f"{self.spec.name}'s session is {self.state}. {self.error}".strip())
            if self.turn_active.is_set():
                if self.can_steer and self._steer(text):
                    return "steered"
                self.pending.append(text)
                return "queued"
            self.turn_active.set()
        try:
            self._start_turn(text)
        except Exception as exc:
            self.turn_active.clear()
            self.error = str(exc)[:500]
            self.emit("turn", status="failed", text=self.error)
            raise
        return "started"

    def interrupt(self) -> None:
        pass

    def close(self) -> None:
        raise NotImplementedError

    def wait_idle(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.turn_active.is_set() and not self.pending:
                return True
            time.sleep(0.05)
        return False

    # -- transport hooks

    def _start_turn(self, text: str) -> None:
        raise NotImplementedError

    def _steer(self, text: str) -> bool:
        return False

    def snapshot(self) -> dict[str, Any]:
        return {"seat": self.spec.seat, "agent_id": self.spec.agent_id, "name": self.spec.name,
                "kind": self.spec.kind, "model": self.spec.model, "state": self.state,
                "busy": self.busy, "session_id": self.session_id, "resume": self.resume_outcome,
                "error": self.error, "queued": len(self.pending), "turns": self.turns_completed,
                "last_activity": self.last_activity, "cwd": self.spec.cwd, "access": self.spec.access}
