"""Owns every v3 team for one Nexus server: create, reopen, talk, answer, close."""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from ..models import HarnessError
from .base import ACCESS_MODES, AgentSession, SeatSpec
from .team import TeamRun

SUPPORTED_KINDS = ("codex-cli", "claude-cli", "gemini-cli", "copilot-cli")
# The route names Nexus's own setup gives each CLI (seats.ROUTE_NAMES, reversed).
ROUTE_KINDS = {"codex": "codex-cli", "claude": "claude-cli", "gemini": "gemini-cli", "copilot": "copilot-cli"}


def _installed(kind: str) -> str:
    from ..providers import subscription_cli
    return subscription_cli.available(kind)
MOST_AGENTS = 6


def make_session(spec: SeatSpec, on_event: Callable[[dict[str, Any]], None],
                 approver: Callable[[dict[str, Any]], str]) -> AgentSession:
    """The real CLI adapters, each started with the user's own signed-in tool."""

    from ..providers import subscription_cli
    from ..providers.codex_cli import _minimal_codex_environment
    from .. import session_health

    executable = subscription_cli.available(spec.kind, list(spec.command) if spec.command else None)
    if not executable:
        raise HarnessError(f"{spec.name}: the {spec.kind} command line is not installed on this computer.")
    env = _minimal_codex_environment(spec.extra_env)
    if spec.mcp and spec.mcp.get("env"):
        env = {**env, **{k: v for k, v in spec.mcp["env"].items() if k == "PYTHONPATH"}}

    def observed(event: dict[str, Any]) -> None:
        # Real outcomes feed the background sign-in watcher, like every other request.
        if event.get("kind") == "turn" and event.get("status") in ("completed", "failed"):
            session_health.report(spec.kind, spec.command, event["status"] == "completed", str(event.get("text") or ""))
        elif event.get("kind") == "session" and event.get("state") == "error":
            session_health.report(spec.kind, spec.command, False, str(event.get("text") or ""))
        on_event(event)

    if spec.kind == "codex-cli":
        from .codex_session import CodexSession
        return CodexSession(spec, observed, executable=executable, env=env, approver=approver)
    if spec.kind == "claude-cli":
        from .claude_session import ClaudeSession
        return ClaudeSession(spec, observed, executable=executable, env=env)
    if spec.kind in ("gemini-cli", "copilot-cli"):
        from .acp_session import AcpSession
        return AcpSession(spec, observed, executable=executable, env=env, approver=approver)
    raise HarnessError(f"{spec.name}: Agent Runtime v3 does not support {spec.kind} yet.")


class RuntimeManager:
    def __init__(self, *, root: Path, board: Callable[[], dict[str, Any]], config: Callable[[], Any],
                 project_root: Callable[[], Path], session_factory=make_session,
                 installed: Callable[[str], str] = _installed):
        self.root = Path(root)
        self.board = board
        self.config = config
        self.project_root = project_root
        self.session_factory = session_factory
        self.installed = installed
        self.teams: dict[str, TeamRun] = {}
        self._lock = threading.RLock()

    # -- agents available to a team

    def available_agents(self) -> list[dict[str, Any]]:
        routes = (self.config().get("providers") or {}) if self.config() is not None else {}
        found = []
        present: dict[str, bool] = {}
        for agent in (self.board() or {}).get("agents", []) or []:
            if not isinstance(agent, dict):
                continue
            route = str(agent.get("who") or agent.get("route") or "")
            settings = routes.get(route) if isinstance(routes.get(route), dict) else {}
            kind = str(settings.get("kind") or settings.get("name") or "")
            if not settings and route in ROUTE_KINDS:
                # The board is shared by every project; this one has not set the
                # route up. The installed, signed-in CLI is all a live session needs.
                wanted = ROUTE_KINDS[route]
                if wanted not in present:
                    present[wanted] = bool(self.installed(wanted))
                kind = wanted if present[wanted] else ""
            found.append({"id": str(agent.get("id") or ""), "name": str(agent.get("name") or route), "route": route,
                          "kind": kind, "model": str(settings.get("model") or ""),
                          "supported": kind in SUPPORTED_KINDS})
        return [one for one in found if one["id"]]

    def _agent(self, agent_id: str) -> dict[str, Any]:
        routes = self.config().get("providers") or {}
        for one in self.available_agents():
            if one["id"] == agent_id:
                if not one["supported"]:
                    raise HarnessError(f"{one['name']} uses {one['kind'] or 'an unknown connection'}, which Agent "
                                       "Runtime v3 does not drive yet. Pick a Claude, Codex, Gemini or Copilot agent.")
                settings = routes.get(one["route"]) or {}
                env = {}
                if one["kind"] == "gemini-cli" and settings.get("google_project"):
                    env["GOOGLE_CLOUD_PROJECT"] = str(settings["google_project"])
                command = settings.get("command")
                return {**one, "command": list(command) if isinstance(command, list) and command else None, "env": env}
        raise HarnessError(f"There is no agent {agent_id!r} on the board.")

    # -- teams

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        ids = [str(one) for one in payload.get("agents") or [] if str(one).strip()]
        ids = list(dict.fromkeys(ids))
        if not ids:
            raise HarnessError("Choose at least one agent.")
        if len(ids) > MOST_AGENTS:
            raise HarnessError(f"A live team can have up to {MOST_AGENTS} agents.")
        goal = str(payload.get("goal") or "").strip()
        if not goal:
            raise HarnessError("Describe what the team should do.")
        access = str(payload.get("access") or "full")
        if access not in ACCESS_MODES:
            raise HarnessError("Choose Read only, Ask before commands or Full project access.")
        mode = str(payload.get("mode") or "shared")
        if mode not in ("shared", "worktrees"):
            raise HarnessError("Choose the shared folder or separate git worktrees.")
        project = Path(str(payload.get("project") or self.project_root())).expanduser()
        if not project.is_dir():
            raise HarnessError(f"The project folder {project} does not exist.")
        agents = [self._agent(one) for one in ids]
        lead = str(payload.get("lead") or ids[0])
        if lead not in ids:
            lead = ids[0]
        team_id = uuid.uuid4().hex
        name = str(payload.get("name") or "").strip()[:80] or (goal.splitlines()[0][:60])
        team = TeamRun(team_id=team_id, name=name, goal=goal, project=str(project), agents=agents, lead=lead,
                       access=access, mode=mode, root=self.root / team_id, session_factory=self.session_factory)
        with self._lock:
            self.teams[team_id] = team

        def begin() -> None:
            team.start()
            team.kickoff(goal)
        threading.Thread(target=begin, name=f"v3-team-{team_id[:8]}", daemon=True).start()
        return team.snapshot()

    def saved(self) -> list[dict[str, Any]]:
        found = []
        if self.root.is_dir():
            for record in sorted(self.root.glob("*/team.json"), key=lambda one: one.stat().st_mtime, reverse=True)[:50]:
                try:
                    value = json.loads(record.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if value.get("schema_version") != 1:
                    continue
                live = self.teams.get(value.get("team_id"))
                found.append({"team_id": value["team_id"], "name": value.get("name", ""), "goal": value.get("goal", ""),
                              "project": value.get("project", ""), "state": live.state if live else "saved",
                              "agents": [one.get("name") for one in value.get("agents", [])], "created": value.get("created")})
        return found

    def reopen(self, team_id: str) -> dict[str, Any]:
        with self._lock:
            live = self.teams.get(team_id)
            if live is not None and live.state != "closed":
                return live.snapshot()
        record = self.root / team_id / "team.json"
        try:
            value = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise HarnessError("That team is not saved on this computer.") from None
        agents = []
        for held in value.get("agents", []):
            fresh = self._agent(held["id"])
            agents.append({**fresh, "resume_id": held.get("resume_id", ""), "branch": held.get("branch")})
        team = TeamRun(team_id=team_id, name=value.get("name", ""), goal=value.get("goal", ""),
                       project=value["project"], agents=agents, lead=value.get("lead") or agents[0]["id"],
                       access=value.get("access", "full"), mode=value.get("mode", "shared"),
                       root=self.root / team_id, session_factory=self.session_factory)
        with self._lock:
            self.teams[team_id] = team
        threading.Thread(target=team.start, name=f"v3-reopen-{team_id[:8]}", daemon=True).start()
        return team.snapshot()

    def team(self, team_id: str) -> TeamRun:
        live = self.teams.get(str(team_id or ""))
        if live is None:
            raise HarnessError("That team is not running. Reopen it first.")
        return live

    def close(self, team_id: str) -> None:
        self.team(team_id).close()

    def close_all(self) -> None:
        for team in list(self.teams.values()):
            try:
                team.close()
            except Exception:
                pass
