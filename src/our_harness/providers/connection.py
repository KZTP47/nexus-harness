"""One truthful connection check for every provider route an agent can use.

The board stores route names (``claude``), while provider adapters are selected
by engine kinds (``claude-cli``).  Keeping that resolution here prevents UI
buttons from accidentally handing a route name to a kind-only adapter.  The
result also distinguishes login, configured credentials, local transports, and
web sessions rather than pretending they are the same kind of authentication.
"""

from __future__ import annotations

import os
import shutil
import ssl
import urllib.error
import urllib.request
from typing import Any

from ..config import LoadedConfig
from ..models import HarnessError
from .registry import ProviderRegistry
from . import subscription_cli


def _routed(config: LoadedConfig, route: str) -> tuple[str, LoadedConfig]:
    named = str(route or "").strip()
    if not named:
        raise HarnessError("Choose which agent route to check first.")
    if named == "default":
        return named, config
    try:
        return named, ProviderRegistry(config).provider_config(named)
    except HarnessError:
        raise HarnessError(
            f"There is no configured provider route called {named}. "
            "Choose the assistant again in this agent's settings."
        )


def _with_route(result: dict[str, Any], route: str, kind: str) -> dict[str, Any]:
    return {"route": route, "kind": kind, **result}


def connection_status(
    config: LoadedConfig,
    route: str,
    *,
    web_connection: dict[str, Any] | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Check the transport the named board agent will really use.

    No model prompt is sent. Native provider auth-status commands are
    authoritative where they exist. For transports without one, the answer is
    deliberately ``unknown`` or ``not-required`` rather than inferred from a
    desktop window, an executable, or a credential-shaped file.
    """

    named = str(route or "").strip()
    if named.startswith("web:"):
        connected = web_connection is not None
        provider = str((web_connection or {}).get("provider") or "provider").strip()
        return {
            "route": named,
            "kind": "web-chat",
            "installed": True,
            "authentication": "signed-in" if connected else "signed-out",
            "state": "authenticated" if connected else "needs-login",
            "can_login": True,
            "checked_by": "live-electron-web-chat",
            "note": (
                f"Logged in. Nexus has a live, usable {provider} web-chat session for this exact agent route."
                if connected else
                "Not logged in. This exact web-chat route is not connected to Electron; open Web AI chats and sign in there."
            ),
        }

    named, routed = _routed(config, named)
    settings = routed.data["provider"]
    kind = str(settings.get("name") or "").strip()
    command = settings.get("command")

    if kind in subscription_cli.RECIPES:
        configured = command if isinstance(command, list) and command else None
        status = subscription_cli.connection_status(
            kind,
            timeout_seconds=timeout_seconds,
            use_cache=False,
            probe=True,
            command=configured,
        )
        authentication = str(status.get("authentication") or "unknown")
        label = subscription_cli.recipe_for(kind).label
        if authentication == "signed-in":
            note = (
                f"Logged in. Nexus verified {label}'s own authentication status for route “{named}”. "
                "This is the command-line session this agent actually uses."
            )
        elif authentication == "signed-out":
            note = (
                f"Not logged in. {label}'s own status command says this route needs sign-in."
            )
        elif not status.get("installed"):
            note = f"{label} is not installed at the command configured for route “{named}”."
        else:
            note = (
                f"Nexus found {label} for route “{named}”, but this provider has no safe, "
                "non-billing authentication-status command. No model request was sent, so login remains unconfirmed."
            )
        return _with_route(
            {**status, "checked_by": "native-cli-auth-status", "note": note},
            named,
            kind,
        )

    if kind == "m365-copilot":
        from . import m365_copilot

        problem = m365_copilot.what_is_missing(settings)
        signed_in = not problem
        return _with_route({
            "installed": True,
            "authentication": "signed-in" if signed_in else "signed-out",
            "state": "authenticated" if signed_in else "needs-login",
            "can_login": True,
            "checked_by": "nexus-microsoft-token-state",
            "note": (
                "Logged in. Nexus has a current Microsoft 365 sign-in for this route."
                if signed_in else problem
            ),
        }, named, kind)

    if kind == "local":
        parts = command if isinstance(command, list) else []
        program = shutil.which(parts[0]) if parts else ""
        ready = bool(program)
        return _with_route({
            "installed": ready,
            "authentication": "not-required",
            "state": "ready" if ready else "not-installed",
            "can_login": False,
            "checked_by": "local-command-discovery",
            "note": (
                "No login is required. The configured local model command is installed."
                if ready else "No login is required, but the configured local model command is not installed."
            ),
        }, named, kind)

    if kind == "ollama":
        endpoint = str(settings.get("endpoint") or "").rstrip("/")
        try:
            with urllib.request.urlopen(
                f"{endpoint}/api/tags", timeout=min(timeout_seconds, 2.0)
            ) as response:
                ready = response.status == 200
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, ValueError):
            ready = False
        return _with_route({
            "installed": ready,
            "authentication": "not-required",
            "state": "ready" if ready else "unreachable",
            "can_login": False,
            "checked_by": "ollama-local-health",
            "note": (
                f"The exact Ollama route is reachable at {endpoint}."
                if ready else
                f"The exact Ollama route is not reachable at {endpoint}. Start Ollama there or choose another route."
            ),
        }, named, kind)

    key_name = str(settings.get("api_key_env") or "").strip()
    if key_name:
        present = bool(os.environ.get(key_name))
        return _with_route({
            "installed": True,
            "authentication": "credential-configured" if present else "missing-credential",
            "state": "configured" if present else "needs-credential",
            "can_login": False,
            "checked_by": "credential-presence",
            "note": (
                f"The credential variable {key_name} is configured and present. "
                "Nexus did not send a request or expose its value."
                if present else
                f"The route expects a credential in {key_name}, but that variable is not set on this machine."
            ),
        }, named, kind)

    return _with_route({
        "installed": True,
        "authentication": "not-required",
        "state": "ready",
        "can_login": False,
        "checked_by": "route-configuration",
        "note": (
            "No account login is required for this configured route. "
            "Its availability is checked separately when Nexus contacts the local service."
        ),
    }, named, kind)


def start_interactive_login(config: LoadedConfig, route: str) -> dict[str, Any]:
    """Open the login flow belonging to the exact configured agent route."""

    named, routed = _routed(config, route)
    settings = routed.data["provider"]
    kind = str(settings.get("name") or "").strip()
    if kind not in subscription_cli.RECIPES:
        if kind == "m365-copilot":
            raise HarnessError("Use the Microsoft sign-in controls in Nexus for this route.")
        raise HarnessError(
            f"Route “{named}” does not use an interactive account login. Check its configured service or credential instead."
        )
    command = settings.get("command")
    configured = command if isinstance(command, list) and command else None
    result = subscription_cli.start_interactive_login(kind, command=configured)
    return {"route": named, "kind": kind, **result}
