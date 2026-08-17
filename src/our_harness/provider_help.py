"""Answer one question in plain words: how do I connect a model?

The control panel shows this on the first screen. It never asks for a key and
never stores one. It only says which ways are ready on this machine, and what
to do about the ones that are not.
"""

from __future__ import annotations

import os
import shutil
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .config import LoadedConfig

READY = "ready"
NEEDS_SETUP = "needs setup"

# Looking for a listening server costs a round trip, and the first screen asks
# for this every time it opens. One second is long enough for a server on this
# machine, and the answer is kept briefly so opening the screen stays instant.
PROBE_TIMEOUT_SECONDS = 1.0
CACHE_SECONDS = 15.0

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _display_endpoint(endpoint: str) -> str:
    """The address without any user name or password someone put in it."""

    try:
        parsed = urllib.parse.urlsplit(endpoint)
    except ValueError:
        return "the configured address"
    if not parsed.hostname:
        return "the configured address"
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}"


@dataclass(frozen=True)
class ProviderOption:
    id: str
    label: str
    summary: str
    state: str
    reason: str
    steps: tuple[str, ...] = ()
    cost: str = ""
    in_use: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "summary": self.summary,
            "state": self.state,
            "reason": self.reason,
            "steps": list(self.steps),
            "cost": self.cost,
            "in_use": self.in_use,
        }


def _reachable(url: str, timeout: float = PROBE_TIMEOUT_SECONDS) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as answer:
            return 200 <= int(answer.status) < 500
    except (urllib.error.HTTPError,):
        # An answer with an error status still proves something is listening.
        return True
    except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError, ValueError):
        return False


def _key_present(name: str) -> bool:
    return bool(name and os.environ.get(name))


def _ollama(config: LoadedConfig, in_use: bool) -> ProviderOption:
    endpoint = str(config.get("provider.endpoint") or "http://127.0.0.1:11434").rstrip("/")
    model = str(config.get("provider.model") or "qwen2.5-coder:7b")
    # Never show the raw address back: someone may have put a user name and
    # password in it, and this text ends up in an HTTP answer and on the page.
    shown = _display_endpoint(endpoint)
    if _reachable(f"{endpoint}/api/tags"):
        return ProviderOption(
            id="ollama",
            label="Ollama on this machine",
            summary="A model that runs on your own computer. Nothing leaves the machine.",
            state=READY,
            reason=f"Ollama answered at {shown}.",
            cost="Free. It uses your own processor and memory.",
            in_use=in_use,
        )
    return ProviderOption(
        id="ollama",
        label="Ollama on this machine",
        summary="A model that runs on your own computer. Nothing leaves the machine.",
        state=NEEDS_SETUP,
        reason=f"Nothing answered at {shown}.",
        steps=(
            "Install Ollama from ollama.com and start it.",
            f"In a terminal, run: ollama pull {model}",
            "Come back here and press Check again.",
        ),
        cost="Free. It uses your own processor and memory.",
        in_use=in_use,
    )


def _hosted(
    identifier: str,
    label: str,
    key_name: str,
    where: str,
    in_use: bool,
) -> ProviderOption:
    summary = "A model run by someone else. Your code is sent to their service."
    if _key_present(key_name):
        return ProviderOption(
            id=identifier,
            label=label,
            summary=summary,
            state=READY,
            reason=f"{key_name} is set in this terminal.",
            cost="You pay their price per use.",
            in_use=in_use,
        )
    return ProviderOption(
        id=identifier,
        label=label,
        summary=summary,
        state=NEEDS_SETUP,
        reason=f"{key_name} is not set.",
        steps=(
            f"Open {where} in your browser and make a key there.",
            f"Set it in your terminal as {key_name}. Never paste a key into this page or into config.json.",
            "Start the harness again from that same terminal.",
        ),
        cost="You pay their price per use.",
        in_use=in_use,
    )


def _signed_in_tool(identifier: str, in_use: bool) -> ProviderOption:
    """An assistant you already pay for, driven through its own command line."""

    from .providers.subscription_cli import available, recipe_for

    recipe = recipe_for(identifier)
    summary = (
        "Uses a seat your organisation already pays for. No key to get, "
        "because the tool is already signed in."
    )
    found = available(identifier)
    if found:
        return ProviderOption(
            id=identifier,
            label=recipe.label,
            summary=summary,
            state=READY,
            reason=f"The {recipe.command[0]} command was found at {found}.",
            steps=(
                "Add it as a provider route in your own local config, then run: harness trust",
            ),
            cost="Covered by your subscription, with that plan's limits.",
            in_use=in_use,
        )
    return ProviderOption(
        id=identifier,
        label=recipe.label,
        summary=summary,
        state=NEEDS_SETUP,
        reason=f"The {recipe.command[0]} command is not on this machine.",
        steps=(
            recipe.install_hint,
            "Add it as a provider route in your own local config, then run: harness trust",
        ),
        cost="Covered by your subscription, with that plan's limits.",
        in_use=in_use,
    )


def _codex_cli(in_use: bool) -> ProviderOption:
    found = shutil.which("codex")
    summary = "Reuses a ChatGPT sign-in you already have, through the codex command."
    if found:
        return ProviderOption(
            id="codex-cli",
            label="Codex command line",
            summary=summary,
            state=READY,
            reason=f"The codex command was found at {found}.",
            steps=("If it asks you to sign in, run: codex login",),
            cost="Covered by your ChatGPT plan, with that plan's limits.",
            in_use=in_use,
        )
    return ProviderOption(
        id="codex-cli",
        label="Codex command line",
        summary=summary,
        state=NEEDS_SETUP,
        reason="The codex command is not on this machine.",
        steps=(
            "Install the Codex command line tool.",
            "Run: codex login",
            "Add a provider route of kind codex-cli to your local config.",
        ),
        cost="Covered by your ChatGPT plan, with that plan's limits.",
        in_use=in_use,
    )


def provider_options(config: LoadedConfig) -> list[ProviderOption]:
    """Every way of connecting a model, with the ready ones first."""

    chosen = str(config.get("provider.name") or "")
    options = [
        _ollama(config, in_use=chosen == "ollama"),
        _signed_in_tool("claude-cli", in_use=chosen == "claude-cli"),
        _signed_in_tool("copilot-cli", in_use=chosen == "copilot-cli"),
        _hosted("anthropic", "Anthropic", "ANTHROPIC_API_KEY", "console.anthropic.com", chosen == "anthropic"),
        _hosted("openai", "OpenAI", "OPENAI_API_KEY", "platform.openai.com", chosen == "openai"),
        _hosted("gemini", "Google Gemini", "GEMINI_API_KEY", "aistudio.google.com", chosen == "gemini"),
        _codex_cli(in_use=False),
    ]
    options.sort(key=lambda item: (item.state != READY, not item.in_use, item.label))
    return options


def setup_advice(
    config: LoadedConfig,
    *,
    refresh: bool = False,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """What the first screen shows under "Connect a model".

    The answer is kept for a few seconds so opening the screen does not wait on
    a network probe every time. Pressing Check again asks for a fresh one.
    """

    # The model belongs in the key. Without it, changing model inside the
    # short remembering window gives back advice naming the old one.
    key = "|".join(
        str(part)
        for part in (
            config.project_root,
            config.get("provider.name"),
            config.get("provider.endpoint"),
            config.get("provider.model"),
            config.get("provider.api_key_env"),
        )
    )
    now = clock()
    if not refresh:
        with _cache_lock:
            found = _cache.get(key)
        if found is not None and now - found[0] < CACHE_SECONDS:
            return found[1]
    answer = _fresh_advice(config)
    with _cache_lock:
        _cache[key] = (now, answer)
        if len(_cache) > 32:
            _cache.pop(next(iter(_cache)))
    return answer


def _fresh_advice(config: LoadedConfig) -> dict[str, Any]:
    options = provider_options(config)
    ready = [item for item in options if item.state == READY]
    chosen = str(config.get("provider.name") or "")
    in_use = next((item for item in options if item.in_use), None)
    if in_use is not None and in_use.state == READY:
        headline = f"{in_use.label} is set up and in use."
    elif ready:
        names = " or ".join(item.label for item in ready[:2])
        headline = (
            f"This project is set to use {chosen or 'no model'}, which is not ready. "
            f"{names} is ready on this machine."
        )
    else:
        headline = "No model is connected yet. Pick one of the ways below."
    return {
        "headline": headline,
        "chosen": chosen,
        "ready_count": len(ready),
        "options": [item.to_dict() for item in options],
        "note": "A key is never typed into this page and never saved in the project.",
    }
