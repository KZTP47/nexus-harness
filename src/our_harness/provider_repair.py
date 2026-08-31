"""Provider-neutral recovery plans for agent routes.

The panel must not guess that every provider failure is a login failure.  This
module turns the engine's route-aware, non-billing diagnostics into a small
stable UI contract.  Provider-specific repair details stay here; the browser
only renders the actions it is given.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from . import chat
from .config import LoadedConfig
from .providers.connection import connection_status


DIAGNOSIS_SCHEMA_VERSION = 1


def _action(
    action_id: str,
    label: str,
    note: str,
    *,
    primary: bool = False,
    cost: str = "none",
) -> dict[str, Any]:
    """Build a route-independent action template.

    ``repair_plan`` binds every returned action to the exact route and diagnosis
    fingerprint. The UI may therefore render new action IDs without having to
    infer either provider state or whether clicking them spends a model call.
    """

    return {
        "id": action_id,
        "label": label,
        "note": note,
        "primary": primary,
        "cost": cost,
    }


CHECK = _action(
    "check", "Check again",
    "Runs the provider's free status check. It does not send a model request.",
)
LIVE_TEST = _action(
    "live-test", "Run live test",
    "Uses one model request. It does not read or change project files.",
    primary=True,
    cost="model-request",
)
LOGIN = _action(
    "login", "Open sign-in",
    "Opens the exact configured provider command in its own visible sign-in window.",
    primary=True,
)
WEB_CHAT = _action(
    "web-chat", "Open Web AI chats",
    "Reconnect or sign in to this exact provider-owned web-chat session.",
    primary=True,
)
GOOGLE_PROJECT = _action(
    "google-project", "Set Cloud project",
    "Adds the Google Cloud Project ID required by this Gemini Workspace route. No API key is needed.",
    primary=True,
)
CLAUDE_REPAIR = _action(
    "repair-claude", "Repair Claude access",
    "Updates Claude, signs its command line out, and opens a fresh provider-owned sign-in.",
    primary=True,
)
SETTINGS = _action(
    "settings", "Open route settings",
    "Opens Nexus Settings filtered to this provider route. Credentials are never displayed.",
    primary=True,
)
CHOOSE_ROUTE = _action(
    "choose-route", "Choose another assistant",
    "Opens this agent's assistant selector so the missing route can be replaced.",
    primary=True,
)
INSPECT_PROVIDER_TURN = _action(
    "inspect-provider-turn", "Inspect provider conversation",
    "Opens the exact provider-owned conversation so you can check whether the uncertain turn arrived before retrying.",
    primary=True,
)

_WEB_PROVIDER_IDS = ("chatgpt", "claude", "gemini", "copilot")


def _web_chat_reconnect_action(route: str) -> dict[str, Any]:
    """Bind a reconnect action to the dynamic route it must restore.

    Web-chat routes live in Electron rather than project Settings.  Current
    connection IDs are generated as ``<provider>-<random>``; keep the provider
    hint optional so an older/unknown ID opens the provider chooser instead of
    guessing.  The connection ID itself is always exact.
    """

    connection_id = str(route or "").removeprefix("web:")
    provider = next((
        candidate for candidate in _WEB_PROVIDER_IDS
        if connection_id == candidate or connection_id.startswith(f"{candidate}-")
    ), "")
    return {
        **WEB_CHAT,
        "connection_id": connection_id,
        **({"provider": provider} if provider else {}),
    }


def _actions_for_route(
    route: str, kind: str, actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep dynamic web routes out of the static project-settings repair UI."""

    if kind != "web-chat" and not str(route or "").startswith("web:"):
        return actions
    reconnect = _web_chat_reconnect_action(route)
    fixed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for action in actions:
        candidate = reconnect if action.get("id") in {"settings", "web-chat"} else action
        action_id = str(candidate.get("id") or "")
        if action_id in seen:
            continue
        seen.add(action_id)
        fixed.append(dict(candidate))
    return fixed


def _normalise_failure(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()[:2_000]


def classify_prior_failure(value: Any) -> dict[str, Any]:
    """Return a stable, provider-neutral diagnosis for saved failure evidence.

    This intentionally classifies only evidence already saved by a real request;
    it never probes or calls a model. Specific categories run before broad ones
    so, for example, HTTP 429 is not mistaken for generic account capacity and a
    model-plan mismatch is not mistaken for authentication trouble.
    """

    summary = _normalise_failure(value)
    text = summary.casefold()
    category = "unknown"

    if any(marker in text for marker in (
        "outcome is unknown", "outcome unknown", "unknown outcome",
        "unreconciled provider turn", "may have submitted", "may have been sent",
        "cannot prove whether", "cannot tell whether the provider",
        "uncertain prior delivery", "will not resend", "did not resend",
    )):
        category = "outcome-unknown"
    elif (
        re.search(r"\b(?:http\s*)?429\b", text)
        or any(marker in text for marker in (
            "rate limit", "rate-limit", "too many requests", "retry-after",
            "requests per minute", "tokens per minute", "slow down",
        ))
    ):
        category = "rate-limit"
    elif any(marker in text for marker in (
        "quota exhausted", "quota exceeded", "insufficient quota",
        "resource exhausted", "usage limit", "credit balance", "out of credits",
        "billing account", "provider capacity", "service capacity", "overloaded",
        "no available seat", "subscription access",
    )):
        category = "capacity"
    elif any(marker in text for marker in (
        "timed out", "timeout", "time-out", "deadline exceeded", "deadline expired",
        "ran past its time limit", "took too long",
    )):
        category = "timeout"
    elif any(marker in text for marker in (
        "unknown model", "model not found", "model is not available",
        "model isn't available", "model does not exist", "unsupported model",
        "model is unsupported", "selected model", "model catalog",
        "does not support reasoning", "model access",
    )):
        category = "model"
    elif any(marker in text for marker in (
        "error loading configuration", "invalid configuration", "configuration error",
        "could not read its configuration", "couldn't read its configuration",
        "config.toml", "config.json", "unknown option", "unknown argument",
        "unknown flag", "unknown variant", "invalid setting", "invalid argument",
        "unsupported option", "malformed configuration",
    )):
        category = "config"
    elif (
        re.search(r"\b(?:http\s*)?401\b", text)
        or any(marker in text for marker in (
            "authentication required", "authentication failed", "not authenticated",
            "not signed in", "not logged in", "sign-in required", "signin required",
            "login required", "unauthorized", "invalid api key", "expired api key",
            "revoked api key", "invalid access token", "expired access token",
            "missing credential", "credential is missing", "api key is not set",
            "api key environment variable", "token is not set",
        ))
    ):
        category = "auth"
    elif any(marker in text for marker in (
        "connection refused", "connection reset", "connection aborted",
        "network is unreachable", "host is unreachable", "service unreachable",
        "name resolution", "dns lookup", "dns error", "socket error",
        "tls error", "ssl error", "certificate verify", "proxy error",
        "failed to connect", "could not connect", "couldn't connect",
    )):
        category = "network"
    elif (
        re.search(r"\b(?:http\s*)?(?:404|405|406|415|422)\b", text)
        or any(marker in text for marker in (
            "invalid json", "malformed json", "protocol error", "schema mismatch",
            "response schema", "missing response field", "missing completion event",
            "stream ended", "unexpected redirect", "unsupported content type",
            "invalid response format", "could not parse the response",
        ))
    ):
        category = "protocol"

    retryable: bool | None = {
        "auth": False,
        "config": False,
        "model": False,
        "capacity": False,
        "rate-limit": True,
        "network": True,
        "timeout": True,
        "protocol": False,
        "outcome-unknown": False,
        "unknown": None,
    }[category]
    return {
        "schema_version": DIAGNOSIS_SCHEMA_VERSION,
        "source": "prior-provider-failure",
        "category": category,
        "retryable": retryable,
        "summary": summary,
    }


def _status_diagnosis(status: dict[str, Any]) -> dict[str, Any]:
    state = str(status.get("state") or "unknown")
    authentication = str(status.get("authentication") or "unknown")
    if state == "configuration-error" or state in {"not-installed", "route-missing"}:
        category = "config"
    elif state == "needs-login" or authentication == "signed-out":
        category = "auth"
    elif state == "needs-credential" or authentication == "missing-credential":
        category = "auth"
    elif state == "unreachable":
        category = "network"
    else:
        category = "none"
    return {
        "schema_version": DIAGNOSIS_SCHEMA_VERSION,
        "source": "non-billing-status",
        "category": category,
        "retryable": category == "network" if category != "none" else None,
        "summary": _normalise_failure(status.get("note")),
    }


def _diagnosis_fingerprint(
    route: str,
    status: dict[str, Any],
    diagnosis: dict[str, Any],
) -> str:
    material = {
        "schema_version": DIAGNOSIS_SCHEMA_VERSION,
        "route": str(route or ""),
        "kind": str(status.get("kind") or ""),
        "state": str(status.get("state") or "unknown"),
        "authentication": str(status.get("authentication") or "unknown"),
        "diagnosis": {
            "source": diagnosis.get("source"),
            "category": diagnosis.get("category"),
            "summary": diagnosis.get("summary"),
        },
    }
    encoded = json.dumps(
        material, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _finish_plan(
    status: dict[str, Any],
    repair: dict[str, Any],
    diagnosis: dict[str, Any],
) -> dict[str, Any]:
    route = str(status.get("route") or "")
    fingerprint = _diagnosis_fingerprint(route, status, diagnosis)
    actions = [
        {
            **dict(action),
            "route": route,
            "diagnosis_fingerprint": fingerprint,
        }
        for action in _actions_for_route(
            route,
            str(status.get("kind") or ""),
            [dict(one) for one in repair.get("actions", []) if isinstance(one, dict)],
        )
    ]
    return {
        **status,
        "repair": {
            **repair,
            "diagnosis": diagnosis,
            "diagnosis_fingerprint": fingerprint,
            "actions": actions,
        },
    }


def _route_exists(config: LoadedConfig, route: str) -> bool:
    named = str(route or "").strip()
    if named == "default" or named.startswith("web:"):
        return True
    routes = config.get("providers", {})
    return bool(named and isinstance(routes, dict) and named in routes)


def _route_setup(config: LoadedConfig, route: str) -> dict[str, Any]:
    setup = next(
        (one for one in chat.already_set_up(config)
         if str(one.get("route") or "") == str(route or "")),
        {},
    )
    if setup:
        return setup
    # Electron web routes are live session resources and therefore are not in
    # the static configured-route list above. Their real request failures are
    # still saved by the same transport layer. Without joining that evidence
    # here, an unreconciled visible provider reply was diagnosed merely as
    # "connected" and the safe inspect-before-retry recovery never appeared.
    failure = chat.what_would_not_answer(config).get(str(route or ""), {})
    why = str(failure.get("why") or "").strip() if isinstance(failure, dict) else ""
    if why:
        return {
            "ready": True,
            "kind": "web-chat" if str(route or "").startswith("web:") else "",
            "trouble_last_time": why,
        }
    return {}


def repair_plan(
    config: LoadedConfig,
    route: str,
    *,
    web_connection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Diagnose one exact route without making a billable model request."""

    named = str(route or "").strip()
    if not _route_exists(config, named):
        status = {
            "route": named,
            "kind": "unknown",
            "installed": False,
            "authentication": "unknown",
            "state": "route-missing",
            "can_login": False,
            "checked_by": "route-configuration",
            "note": (
                f"There is no configured provider route called {named}."
                if named else
                "No provider route was selected."
            ),
        }
        diagnosis = _status_diagnosis(status)
        return _finish_plan(status, {
            "state": "route-missing",
            "tone": "blocked",
            "title": "This agent's provider route no longer exists",
            "summary": str(status["note"]),
            "steps": [
                "Choose another configured assistant for this agent, or recreate the missing route.",
                "Press Check again after the agent points to an existing route.",
            ],
            "actions": [CHOOSE_ROUTE, SETTINGS, CHECK],
            "diagnosis_costs_model_request": False,
        }, diagnosis)

    status = connection_status(config, route, web_connection=web_connection)
    setup = _route_setup(config, route)
    kind = str(status.get("kind") or setup.get("kind") or "")
    state = str(status.get("state") or "unknown")
    authentication = str(status.get("authentication") or "unknown")
    recent_failure = str(setup.get("trouble_last_time") or "").strip()
    diagnosis = (
        classify_prior_failure(recent_failure)
        if recent_failure else
        _status_diagnosis(status)
    )
    blocked = bool(setup.get("why_not") or not setup.get("ready", True))

    plan_state = "needs-verification"
    tone = "attention"
    title = "Ready for a live check"
    summary = str(status.get("note") or "Nexus found the route, but has not verified a model answer.")
    steps = [
        "Review the free route and session check below.",
        "Run one live test when you are ready to verify an actual answer.",
    ]
    actions: list[dict[str, Any]] = [LIVE_TEST, CHECK]

    if kind == "gemini-cli" and blocked:
        plan_state, tone = "needs-cloud-project", "blocked"
        title = "Gemini needs a Cloud project"
        summary = str(setup.get("why_not") or status.get("note") or "").strip()
        steps = [
            "Enter the Google Cloud Project ID used by this Workspace account.",
            "Run the live test to prove that this exact Gemini route can answer.",
        ]
        actions = [GOOGLE_PROJECT, CHECK]
    elif state == "configuration-error":
        plan_state, tone = "configuration-error", "blocked"
        title = "The provider configuration must be repaired"
        summary = str(status.get("note") or "The provider could not read its configuration.")
        steps = [
            "Open this route in Nexus Settings and correct the reported value.",
            "Press Check again; signing in cannot repair an invalid configuration.",
        ]
        actions = [SETTINGS, CHECK]
    elif not bool(status.get("installed", True)) or state == "not-installed":
        plan_state, tone = "not-installed", "blocked"
        title = "The configured provider command was not found"
        summary = str(status.get("note") or "Install the provider command or select a route that exists on this computer.")
        steps = [
            "Install the provider command, or update this route to the installed command.",
            "Press Check again after the command is available.",
        ]
        actions = [SETTINGS, CHECK]
    elif state == "needs-credential" or authentication == "missing-credential":
        plan_state, tone = "needs-credential", "blocked"
        title = "This route is missing its credential"
        summary = str(status.get("note") or "The configured credential environment variable is not present.")
        steps = [
            "Set the named environment variable outside Nexus; do not paste a secret into this panel.",
            "Restart Nexus so the provider process receives it, then press Check again.",
        ]
        actions = [SETTINGS, CHECK]
    elif state == "unreachable":
        plan_state, tone = "service-unreachable", "blocked"
        title = "The configured service is not reachable"
        summary = str(status.get("note") or "Start the local service or correct its endpoint.")
        steps = [
            "Start the configured local service or correct its endpoint in route settings.",
            "Press Check again after the service is listening.",
        ]
        actions = [SETTINGS, CHECK]
    elif kind == "web-chat" and (
        state == "needs-login" or authentication == "signed-out"
    ):
        # The current live Electron state is stronger than a stale failure
        # note.  A dynamic web route does not exist in project Settings, and a
        # model test cannot repair an absent browser connection.  Recreate the
        # exact connection ID first; once it is live, Check again can safely
        # classify any still-relevant provider-turn evidence.
        diagnosis = _status_diagnosis(status)
        plan_state, tone = "needs-login", "blocked"
        title = "Reconnect this web chat"
        summary = str(status.get("note") or "This web chat is not connected to Electron.")
        steps = [
            "Open the provider-owned web session for this exact route.",
            "Choose the intended conversation and press Use this chat in Nexus.",
            "Come back and press Check again.",
        ]
        actions = [WEB_CHAT, CHECK]
    elif recent_failure:
        summary = recent_failure
        category = str(diagnosis.get("category") or "unknown")
        plan_state, tone = "previous-request-failed", "blocked"
        title = "The last real request failed"
        if category == "auth":
            plan_state = "needs-login"
            title = "The provider session needs attention"
            steps = [
                "Repair or reopen this exact provider-owned session.",
                "Press Check again, then run one live test to prove a real answer.",
            ]
            if kind == "claude-cli":
                actions = [CLAUDE_REPAIR, LIVE_TEST, CHECK]
            elif kind == "web-chat":
                actions = [WEB_CHAT, LIVE_TEST, CHECK]
            elif bool(status.get("can_login")):
                actions = [LOGIN, LIVE_TEST, CHECK]
            else:
                actions = [SETTINGS, CHECK]
        elif category == "config":
            plan_state = "configuration-error"
            title = "The provider configuration must be repaired"
            steps = [
                "Correct the reported provider setting for this exact route.",
                "Press Check again; signing in cannot repair invalid configuration.",
            ]
            actions = [SETTINGS, CHECK]
        elif category == "model":
            plan_state = "model-unavailable"
            title = "The selected model is not usable on this route"
            steps = [
                "Choose a model supported by this account and provider route.",
                "Press Check again before spending another model request.",
            ]
            actions = [SETTINGS, CHECK]
        elif category == "capacity":
            plan_state = "capacity-unavailable"
            title = "The provider account has no available capacity"
            steps = [
                "Review this provider account's quota, credits, billing, or seat availability.",
                "Press Check again after capacity is restored.",
            ]
            actions = [SETTINGS, CHECK]
        elif category == "rate-limit":
            plan_state = "rate-limited"
            title = "The provider asked Nexus to slow down"
            steps = [
                "Wait for the provider's rate-limit window to reset.",
                "Press Check again before deliberately running another model request.",
            ]
            actions = [CHECK]
        elif category == "network":
            plan_state = "service-unreachable"
            title = "The provider could not be reached"
            steps = [
                "Check the configured endpoint, network, proxy, and certificate path.",
                "Press Check again after connectivity is restored.",
            ]
            actions = [SETTINGS, CHECK]
        elif category == "timeout":
            plan_state = "provider-timeout"
            title = "The provider request exceeded its time limit"
            steps = [
                "Check provider availability and the route's timeout setting.",
                "Press Check again before deliberately running another model request.",
            ]
            actions = [SETTINGS, CHECK]
        elif category == "protocol":
            plan_state = "protocol-error"
            title = "The provider returned an incompatible response"
            steps = [
                "Check the route's endpoint, API mode, adapter, and provider version.",
                "Press Check again after correcting the compatibility mismatch.",
            ]
            actions = [SETTINGS, CHECK]
        elif category == "outcome-unknown":
            plan_state = "outcome-unknown"
            title = "Nexus cannot safely tell whether the provider acted"
            steps = [
                "Inspect the provider-owned conversation before retrying this request.",
                "Retry only after confirming that doing so cannot duplicate the work.",
            ]
            actions = (
                [INSPECT_PROVIDER_TURN, CHECK]
                if kind == "web-chat" else
                [CHECK]
            )
        else:
            steps = [
                "Review the saved failure and this exact route's settings.",
                "Run one deliberate live test when it is safe to retry.",
            ]
            actions = [LIVE_TEST, SETTINGS, CHECK]
    elif state == "needs-login" or authentication == "signed-out":
        diagnosis = _status_diagnosis(status)
        plan_state, tone = "needs-login", "blocked"
        title = "Sign-in is required"
        summary = str(status.get("note") or "This provider route is signed out.")
        steps = [
            "Open the provider-owned sign-in and finish it there.",
            "Come back and press Check again.",
            "Run the live test to verify a real answer.",
        ]
        actions = ([WEB_CHAT] if kind == "web-chat" else [LOGIN]) + [CHECK]
    elif state in {"authenticated", "configured", "ready"}:
        plan_state, tone = "ready", "ready"
        title = "Connection looks ready"
        summary = str(status.get("note") or "The route's free readiness checks passed.")
        steps = [
            "The non-billing checks passed.",
            "Optionally run one live test to verify that the model itself answers.",
        ]
        actions = [LIVE_TEST, CHECK]
    elif state == "isolated-ready":
        title = "Protected agent mode is ready"
        summary = str(status.get("note") or "Nexus can isolate agent turns from the incompatible user configuration.")
        steps = [
            "Nexus will bypass the incompatible user configuration for agent turns.",
            "Run one live test to verify the account and exact isolated command path.",
        ]

    return _finish_plan(status, {
        "state": plan_state,
        "tone": tone,
        "title": title,
        "summary": summary,
        "steps": steps,
        "actions": actions,
        "diagnosis_costs_model_request": False,
    }, diagnosis)


def verified_plan(plan: dict[str, Any], milliseconds: int) -> dict[str, Any]:
    """Return the same contract after a real provider answer proves recovery."""

    verified = {key: value for key, value in plan.items() if key != "repair"}
    repair = dict(plan.get("repair") or {})
    diagnosis = {
        "schema_version": DIAGNOSIS_SCHEMA_VERSION,
        "source": "live-model-answer",
        "category": "none",
        "retryable": None,
        "summary": "A live model answer verified this route.",
    }
    repair.update({
        "state": "verified",
        "tone": "ready",
        "title": "Connection verified",
        "summary": f"This exact route answered a live Nexus test in {max(0, int(milliseconds)) / 1000:.1f} seconds.",
        "steps": [
            "The route, provider session, and model answer path all worked.",
            "This agent is reachable again.",
        ],
        "actions": [LIVE_TEST, CHECK],
        "verified_with_model_request": True,
    })
    return _finish_plan(verified, repair, diagnosis)
