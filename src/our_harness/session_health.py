"""Background AI session health: notice, recover, announce, get out of the way.

Every configured command-line AI route (Claude, Codex, Gemini, Copilot...) is
watched here so a lapsed sign-in is handled by Nexus instead of turning into a
stream of unexplained red errors in the Mail and AI Swarm tabs.

Two signals are used, because neither is enough on its own:

* the CLI's own sign-in status command (no model turn, checked periodically).
  ``claude auth status`` still says "logged in" when its OAuth token has
  expired and cannot be refreshed, so this only catches outright sign-outs;
* the outcome of real requests, reported by every provider made through
  ``providers.create_provider``. A failure that reads like a sign-in problem
  opens an incident; any success closes it.

While an incident is open, work that would only fail (automatic mail drafts)
waits instead, the provider's own sign-in window is opened once automatically
(unless the user chose to handle sign-in themselves), and the incident is
re-verified with a tiny request on a back-off. When the route answers again the
refusal notes that mark agents "not ready" are cleared and waiting work
continues on its own. Everything is announced in a feed the UI shows loudly.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .models import HarnessError, ProviderRequest

CONTRACT = "nexus-session-health/v1"
SETTINGS_SCHEMA = 1
# How often a healthy route's sign-in status is looked at. The status command
# is local and cheap, but it still starts a process.
HEALTHY_CHECK_SECONDS = 180.0
# Re-verification of an open incident backs off from this to the maximum.
INCIDENT_CHECK_SECONDS = 20.0
INCIDENT_CHECK_MAX_SECONDS = 300.0
# The sign-in window is opened automatically once per incident, and again only
# if the incident is still open after this long.
AUTO_SIGN_IN_REPEAT_SECONDS = 30 * 60.0
FEED_LIMIT = 50

WATCHED_KINDS = ("claude-cli", "codex-cli", "gemini-cli", "copilot-cli", "assistant-cli")

# Wording that means "this account session cannot be used", as the CLIs print
# it. A plain outage, rate limit or model error must not open a sign-in window.
_SIGN_IN_PROBLEM = re.compile(
    r"failed to authenticate|oauth (?:session|token)|not (?:logged|signed) in"
    r"|(?:log|sign)[ -]?in (?:again|required|expired)|please (?:log|sign)[ -]?in"
    r"|authentication (?:failed|required|expired|error)|(?:session|token|credentials?) (?:has |have )?expired"
    r"|invalid (?:api key|credentials|refresh token)|\bunauthori[sz]ed\b|\b401\b"
    r"|run[: ]+\S*(?:claude|codex|gemini|copilot)\S* (?:auth )?login"
    r"|disabled claude subscription access",
    re.IGNORECASE,
)


def looks_like_sign_in_problem(text: Any) -> bool:
    return bool(_SIGN_IN_PROBLEM.search(str(text or "")))


def session_key(kind: str, command: Any = None) -> str:
    """One key per CLI installation: sign-in belongs to the tool, not the route."""

    program = ""
    if isinstance(command, (list, tuple)) and command:
        program = os.path.basename(str(command[0])).casefold()
        for suffix in (".exe", ".cmd", ".bat", ".ps1"):
            if program.endswith(suffix):
                program = program[: -len(suffix)]
    return f"{kind}:{program or kind}"


# ---- provider outcome reports -------------------------------------------------

_listeners: list[Callable[[str, str, bool, str], None]] = []
_listeners_lock = threading.Lock()


def add_listener(listener: Callable[[str, str, bool, str], None]) -> None:
    with _listeners_lock:
        if listener not in _listeners:
            _listeners.append(listener)


def remove_listener(listener: Callable[[str, str, bool, str], None]) -> None:
    with _listeners_lock:
        if listener in _listeners:
            _listeners.remove(listener)


def report(kind: str, command: Any, ok: bool, message: str = "") -> None:
    """Called for every finished request of a watched provider. Never raises."""

    with _listeners_lock:
        listeners = list(_listeners)
    if not listeners:
        return
    key = session_key(kind, command)
    for listener in listeners:
        try:
            listener(key, kind, ok, str(message or "")[:2000])
        except Exception:
            pass


def observe_provider(provider: Any, kind: str) -> Any:
    """Report the outcome of this provider's requests to the session monitor."""

    if kind not in WATCHED_KINDS:
        return provider
    command = (getattr(provider, "settings", None) or {}).get("command")
    complete = provider.complete

    def observed_complete(request: ProviderRequest, *args: Any, **kwargs: Any):
        try:
            result = complete(request, *args, **kwargs)
        except Exception as exc:
            report(kind, command, False, str(exc))
            raise
        report(kind, command, True)
        return result

    provider.complete = observed_complete
    return provider


# ---- the monitor ---------------------------------------------------------------

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class SessionHealthMonitor:
    """Per-server watcher. Tests construct it with fakes; the app starts it."""

    def __init__(
        self,
        config_source: Callable[[], Any],
        *,
        settings_path: Path | None = None,
        status_check: Callable[[Any, str], dict[str, Any]] | None = None,
        live_check: Callable[[Any, str], None] | None = None,
        open_sign_in: Callable[[Any, str], dict[str, Any]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        dispatch: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._config_source = config_source
        self._settings_path = settings_path
        self._status_check = status_check or _status_check
        self._live_check = live_check or _live_check
        self._open_sign_in = open_sign_in or _open_sign_in
        self._clock = clock
        # Recovery work (resuming goals, retrying drafts) takes other locks. A
        # success is reported on whatever thread made the request, which may
        # hold them, so the work always runs on a thread of its own.
        self._dispatch = dispatch or (lambda work: threading.Thread(
            target=work, name="nexus-session-recovered", daemon=True).start())
        self._lock = threading.RLock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._due: dict[str, float] = {}
        self._backoff: dict[str, float] = {}
        self._feed: list[dict[str, Any]] = []
        self._seq = 0
        self._boot = secrets.token_urlsafe(8)
        self._recovered_callbacks: list[Callable[[list[str]], None]] = []
        # Sessions owned by another part of Nexus (a mailbox sign-in). Their
        # owner detects problems; the monitor only presents them and opens
        # their sign-in through the owner's callback.
        self._openers: dict[str, Callable[[], dict[str, Any]] | None] = {}
        self._checkers: dict[str, Callable[[], None]] = {}
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._settings = self._load_settings()

    # -- settings

    def _load_settings(self) -> dict[str, Any]:
        settings = {"schema_version": SETTINGS_SCHEMA, "auto_sign_in": True}
        try:
            if self._settings_path and self._settings_path.is_file():
                held = json.loads(self._settings_path.read_text(encoding="utf-8"))
                if isinstance(held, dict) and held.get("schema_version") == SETTINGS_SCHEMA:
                    settings["auto_sign_in"] = bool(held.get("auto_sign_in", True))
        except (OSError, ValueError):
            pass
        return settings

    def settings(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._settings)

    def save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if "auto_sign_in" in payload:
                self._settings["auto_sign_in"] = bool(payload["auto_sign_in"])
            settings = dict(self._settings)
        if self._settings_path:
            try:
                self._settings_path.parent.mkdir(parents=True, exist_ok=True)
                part = self._settings_path.with_name(self._settings_path.name + f".{uuid.uuid4().hex}.part")
                part.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
                os.replace(part, self._settings_path)
            except OSError:
                pass
        return settings

    # -- lifecycle

    def on_recovered(self, callback: Callable[[list[str]], None]) -> None:
        self._recovered_callbacks.append(callback)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None or self._stop.is_set():
                return
            add_listener(self.provider_outcome)
            self._thread = threading.Thread(target=self._run, name="nexus-session-health", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        remove_listener(self.provider_outcome)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                pass
            self._wake.wait(1.0)
            self._wake.clear()

    # -- routes

    def _routes(self) -> dict[str, dict[str, Any]]:
        """Configured watched routes, grouped by the CLI session they share."""

        config = self._config_source()
        grouped: dict[str, dict[str, Any]] = {}
        routes = (config.get("providers", {}) if config is not None else {}) or {}
        for name, held in sorted(routes.items()):
            if not isinstance(held, dict):
                continue
            kind = str(held.get("kind") or held.get("name") or "")
            if kind not in WATCHED_KINDS:
                continue
            key = session_key(kind, held.get("command"))
            entry = grouped.setdefault(key, {"kind": kind, "routes": []})
            entry["routes"].append(str(name))
        return grouped

    def _session(self, key: str, kind: str, routes: list[str] | None = None) -> dict[str, Any]:
        session = self._sessions.get(key)
        if session is None:
            session = {"key": key, "kind": kind, "label": _label(kind), "routes": [], "state": "ok",
                       "reason": "", "since": "", "checked_at": "", "sign_in_opened_at": "",
                       "sign_in_note": "", "manual": False, "verifying": False}
            self._sessions[key] = session
        if routes:
            session["routes"] = sorted(set(routes))
        return session

    # -- signals

    def provider_outcome(self, key: str, kind: str, ok: bool, message: str) -> None:
        if ok:
            self._resolve(key, kind, "A request through this sign-in just succeeded.")
        elif looks_like_sign_in_problem(message):
            self._open_incident(key, kind, message)

    def blocked(self, route: str) -> dict[str, Any] | None:
        """The open incident for this route's session, if work should wait."""

        with self._lock:
            for session in self._sessions.values():
                if session["state"] == "signed_out" and route in session["routes"]:
                    return dict(session)
        return None

    def _open_incident(self, key: str, kind: str, reason: str) -> None:
        routes = self._routes().get(key, {}).get("routes", [])
        with self._lock:
            session = self._session(key, kind, routes)
            if session["state"] == "signed_out":
                session["reason"] = _brief(reason) or session["reason"]
                return
            session.update(state="signed_out", reason=_brief(reason), since=_now_iso(),
                           sign_in_opened_at="", sign_in_note="")
            self._backoff[key] = INCIDENT_CHECK_SECONDS
            self._due[key] = self._clock() + INCIDENT_CHECK_SECONDS
            self._announce("signed_out", session,
                           f"{session['label']} needs you to sign in again",
                           "Nexus is holding work that needs it and will continue automatically once it answers.")
            auto = self._settings.get("auto_sign_in", True) and not session["manual"]
        if auto:
            self.open_sign_in(key, automatic=True)
        self._wake.set()

    def _resolve(self, key: str, kind: str, why: str) -> None:
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                self._session(key, kind, self._routes().get(key, {}).get("routes", []))
                return
            session["checked_at"] = _now_iso()
            if session["state"] != "signed_out":
                return
            session.update(state="ok", reason="", since="", sign_in_opened_at="", sign_in_note="", verifying=False)
            self._due[key] = self._clock() + HEALTHY_CHECK_SECONDS
            self._backoff.pop(key, None)
            routes = list(session["routes"])
            self._announce("recovered", session, f"{session['label']} is signed in again",
                           "Waiting work is continuing automatically.")
        callbacks = list(self._recovered_callbacks)

        def recover() -> None:
            for callback in callbacks:
                try:
                    callback(routes)
                except Exception:
                    pass
        self._dispatch(recover)

    # -- sessions owned elsewhere (mailboxes)

    def external_incident(self, key: str, label: str, reason: str,
                          opener: Callable[[], dict[str, Any]] | None = None,
                          checker: Callable[[], None] | None = None) -> None:
        with self._lock:
            self._openers[key] = opener
            if checker is not None:
                self._checkers[key] = checker
            session = self._sessions.get(key)
            if session is None:
                session = self._session(key, "external")
            session["label"] = label
            if session["state"] == "signed_out":
                session["reason"] = _brief(reason) or session["reason"]
                return
            session.update(state="signed_out", reason=_brief(reason), since=_now_iso(),
                           sign_in_opened_at="", sign_in_note="")
            self._announce("signed_out", session, f"{label} needs you to sign in again",
                           "Nexus keeps checking and continues by itself once you are signed in.")
            auto = self._settings.get("auto_sign_in", True) and not session["manual"] and opener is not None
        if auto:
            self.open_sign_in(key, automatic=True)

    def external_resolved(self, key: str) -> None:
        with self._lock:
            session = self._sessions.get(key)
            if session is None or session["state"] != "signed_out":
                return
            session.update(state="ok", reason="", since="", sign_in_opened_at="", sign_in_note="")
            self._announce("recovered", session, f"{session['label']} is connected again",
                           "Checking and drafting continue automatically.")

    def external_forget(self, key: str) -> None:
        with self._lock:
            self._sessions.pop(key, None)
            self._openers.pop(key, None)
            self._checkers.pop(key, None)

    # -- actions the user (or the monitor) can take

    def open_sign_in(self, key: str, *, automatic: bool = False) -> dict[str, Any]:
        config = self._config_source()
        with self._lock:
            session = self._sessions.get(key)
            external = key in self._openers
            if session is None or (not external and not session["routes"]):
                raise HarnessError("Nexus does not know this sign-in. Refresh and try again.")
            route = session["routes"][0] if session["routes"] else ""
            label = session["label"]
            opener = self._openers.get(key)
        if external and opener is None:
            note = f"Open the Mail tab and choose Reconnect mailbox for {label}."
            with self._lock:
                session["sign_in_note"] = note
            return {"opened": False, "note": note}
        try:
            opened = opener() if external else self._open_sign_in(config, route)
        except Exception as exc:
            note = f"Nexus could not open {label}'s sign-in by itself: {_brief(exc)}"
            with self._lock:
                session["sign_in_note"] = note
            return {"opened": False, "note": note}
        note = str(opened.get("note") or "").strip() or (
            f"{label}'s own sign-in window is open. Sign in there; Nexus notices by itself.")
        with self._lock:
            session["sign_in_opened_at"] = _now_iso()
            session["sign_in_opened_clock"] = self._clock()
            session["sign_in_note"] = note
            if automatic:
                self._announce("sign_in_opened", session, f"Nexus opened {label}'s sign-in",
                               "Finish signing in there. Nothing else is needed.")
            # A sign-in usually takes a minute; look again soon after it.
            self._backoff[key] = INCIDENT_CHECK_SECONDS
            self._due[key] = self._clock() + INCIDENT_CHECK_SECONDS
        return {"opened": bool(opened.get("opened", True)), "note": note}

    def check_now(self, key: str = "") -> None:
        with self._lock:
            keys = [key] if key else list(self._sessions)
            for one in keys:
                self._due[one] = 0.0
            checkers = [self._checkers[one] for one in keys if one in self._checkers]
        for checker in checkers:
            try:
                checker()
            except Exception:
                pass
        self._wake.set()

    def manual(self, key: str, manual: bool) -> None:
        """The user takes charge: no automatic sign-in window for this session."""

        with self._lock:
            session = self._sessions.get(key)
            if session is not None:
                session["manual"] = bool(manual)

    # -- periodic work

    def tick(self) -> None:
        now = self._clock()
        routes = self._routes()
        with self._lock:
            for key, entry in routes.items():
                self._session(key, entry["kind"], entry["routes"])
            for key in list(self._sessions):
                if key not in routes and key not in self._openers:
                    self._sessions.pop(key, None)
                    self._due.pop(key, None)
            due = [key for key in self._sessions
                   if key not in self._openers and self._due.get(key, 0.0) <= now]
        for key in due:
            if self._stop.is_set():
                return
            self._check(key)

    def _check(self, key: str) -> None:
        config = self._config_source()
        with self._lock:
            session = self._sessions.get(key)
            if session is None or not session["routes"]:
                return
            route, kind, incident = session["routes"][0], session["kind"], session["state"] == "signed_out"
            # Set before the (slow) check so a failing check is not repeated
            # every second.
            if incident:
                wait = min(INCIDENT_CHECK_MAX_SECONDS, self._backoff.get(key, INCIDENT_CHECK_SECONDS) * 2)
                self._backoff[key] = wait
                self._due[key] = self._clock() + wait
            else:
                self._due[key] = self._clock() + HEALTHY_CHECK_SECONDS
        try:
            status = self._status_check(config, route)
        except Exception:
            status = {"authentication": "unknown"}
        authentication = str(status.get("authentication") or "unknown")
        with self._lock:
            session["checked_at"] = _now_iso()
        if authentication == "signed-out":
            if not incident:
                self._open_incident(key, kind, str(status.get("note") or f"{_label(kind)} says it is not signed in."))
            else:
                self._maybe_reopen_sign_in(key)
            return
        if not incident:
            return
        # The status command can say "signed in" with an expired token, so an
        # incident is only closed by a request that really works.
        with self._lock:
            session["verifying"] = True
        try:
            self._live_check(config, route)
        except Exception as exc:
            with self._lock:
                session["verifying"] = False
                if looks_like_sign_in_problem(exc):
                    session["reason"] = _brief(exc) or session["reason"]
            self._maybe_reopen_sign_in(key)
            return
        self._resolve(key, kind, "A test request succeeded.")

    def _maybe_reopen_sign_in(self, key: str) -> None:
        with self._lock:
            session = self._sessions.get(key)
            if session is None or session["manual"] or not self._settings.get("auto_sign_in", True):
                return
            opened = session.get("sign_in_opened_clock")
            if opened is not None and self._clock() - opened < AUTO_SIGN_IN_REPEAT_SECONDS:
                return
        self.open_sign_in(key, automatic=True)

    # -- presentation

    def _announce(self, kind: str, session: dict[str, Any], title: str, detail: str) -> None:
        self._seq += 1
        self._feed.append({"seq": self._seq, "kind": kind, "session": session["key"], "title": title,
                           "detail": detail, "at": _now_iso(), "raised": self._clock()})
        del self._feed[:-FEED_LIMIT]

    def snapshot(self, after: int | None = None) -> dict[str, Any]:
        with self._lock:
            sessions = []
            for session in self._sessions.values():
                public = {k: v for k, v in session.items() if k != "sign_in_opened_clock"}
                public["auto_sign_in"] = bool(self._settings.get("auto_sign_in", True)) and not session["manual"]
                sessions.append(public)
            now = self._clock()
            feed = [{**{k: v for k, v in item.items() if k != "raised"},
                     "age_seconds": round(max(0.0, now - item["raised"]), 1)}
                    for item in self._feed if after is None or item["seq"] > after]
            return {"contract": CONTRACT, "boot": self._boot, "seq": self._seq,
                    "settings": dict(self._settings), "sessions": sorted(sessions, key=lambda one: one["key"]),
                    "feed": feed}


# ---- real checks -------------------------------------------------------------

def _label(kind: str) -> str:
    return {"claude-cli": "Claude", "codex-cli": "GPT Codex", "gemini-cli": "Gemini",
            "copilot-cli": "GitHub Copilot", "assistant-cli": "The assistant command line"}.get(kind, kind)


def _brief(value: Any) -> str:
    return " ".join(str(value or "").split())[:400]


def _status_check(config: Any, route: str) -> dict[str, Any]:
    from .providers.connection import connection_status

    return connection_status(config, route, timeout_seconds=10.0)


def _open_sign_in(config: Any, route: str) -> dict[str, Any]:
    from .providers.connection import start_interactive_login

    result = start_interactive_login(config, route)
    return {"opened": bool(result.get("opened", True)), "note": str(result.get("note") or "")}


def _live_check(config: Any, route: str) -> None:
    """One tiny request in an empty folder. Raises when the route cannot answer."""

    from .providers import ProviderRegistry, create_provider

    routed = ProviderRegistry(config).provider_config(route)
    with tempfile.TemporaryDirectory(prefix="nexus-session-check-") as folder:
        request = ProviderRequest(
            system_prefix="This is an automatic connection check. Reply with the single word OK.",
            dynamic_context="", messages=[{"role": "user", "content": "Reply with OK."}],
            model=str(routed.get("provider.model") or ""), max_output_tokens=16, timeout_seconds=90,
            conversation_key="session-health:" + uuid.uuid4().hex, working_directory=folder,
        )
        result = create_provider(routed).complete(request)
    if not str(getattr(result, "text", "") or "").strip():
        raise HarnessError("The connection check got no answer.")
