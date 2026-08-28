"""A bounded bridge between provider web pages and Nexus agent turns.

The browser session itself belongs to Electron.  Python owns the agent loop,
so it places a request in this small in-process mailbox and waits for the
desktop window to return the visible assistant reply.  No cookies, passwords,
or arbitrary browser script cross this boundary.
"""

from __future__ import annotations

import json
import hashlib
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import cancellation
from .models import HarnessError, ProviderRequest, ProviderResponse
from .redaction import CredentialRedactor, bounded_redacted_text

WEB_ROUTE = re.compile(r"^web:([a-z0-9][a-z0-9-]{5,63})$")
REQUEST_ID = re.compile(r"^[a-f0-9]{32}$")
CONVERSATION_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
HEARTBEAT_SECONDS = 15.0
WEB_WAIT_SECONDS = 420.0
MAX_WEB_ANSWER_CHARACTERS = 8_000_000


def _safe_conversation_key(value: object) -> str:
    """Return a stable, opaque channel key for every local chat identity.

    Pair registries already use safe ``pair-chat-*`` keys and keep those
    unchanged. Standalone agent names are user-editable, however; rejecting a
    valid board chat because its display identity contains spaces, Unicode or
    is longer than the transport limit makes web agents work only in pairs.
    The Electron bridge needs an identifier, not the display name, so hash any
    non-transport-safe value instead of refusing the chat.
    """

    raw = str(value or "").strip()
    if not raw or CONVERSATION_KEY.fullmatch(raw):
        return raw
    return "conversation-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class WebRequest:
    request_id: str
    route: str
    prompt: str
    made_at: float = field(default_factory=time.time)
    state: str = "queued"
    answer: str = ""
    error: str = ""
    milliseconds: int = 0
    model: str = ""
    attachments: list[dict[str, str]] = field(default_factory=list)
    conversation_key: str = ""
    prefer_existing_conversation: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "route": self.route,
            "prompt": self.prompt,
            "made_at": self.made_at,
            "attachments": list(self.attachments),
            "conversation_key": self.conversation_key,
            "prefer_existing_conversation": self.prefer_existing_conversation,
        }


class WebChatBroker:
    """Process-local request mailbox, guarded by the harness session token."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._connections: dict[str, dict[str, Any]] = {}
        self._requests: dict[str, WebRequest] = {}
        self._redactor = CredentialRedactor()

    def heartbeat(self, connections: object) -> list[dict[str, Any]]:
        if not isinstance(connections, list):
            raise HarnessError("Web chat connections must be a list")
        now = time.time()
        seen: set[str] = set()
        with self._condition:
            for raw in connections[:32]:
                if not isinstance(raw, dict):
                    continue
                connection_id = str(raw.get("id") or "").strip().lower()
                route = f"web:{connection_id}"
                if not WEB_ROUTE.fullmatch(route):
                    continue
                provider = str(raw.get("provider") or "web").strip()[:40]
                title = " ".join(str(raw.get("title") or "Web chat").split())[:120]
                url = str(raw.get("url") or "")[:1000]
                self._connections[route] = {
                    "id": connection_id,
                    "route": route,
                    "provider": provider,
                    "title": title or "Web chat",
                    "url": url,
                    "seen_at": now,
                }
                seen.add(route)
            # An omitted connection was removed in Electron.  Drop it at once;
            # stale renderer heartbeats are handled by routes() below.
            for route in list(self._connections):
                if route not in seen:
                    self._connections.pop(route, None)
            self._condition.notify_all()
        return self.routes()

    def routes(self) -> list[dict[str, Any]]:
        now = time.time()
        with self._condition:
            held = [dict(one) for one in self._connections.values()
                    if now - float(one.get("seen_at") or 0) <= HEARTBEAT_SECONDS]
        return sorted(held, key=lambda one: (one["provider"], one["title"], one["route"]))

    def decorate_swarm(self, standing: dict[str, Any]) -> dict[str, Any]:
        """Add live web routes to the ordinary machine-provider board view."""

        routes = self.routes()
        by_route = {one["route"]: one for one in routes}
        choices = standing.setdefault("who_can_be_used", [])
        existing = {str(one.get("route") or "") for one in choices if isinstance(one, dict)}
        for one in routes:
            if one["route"] in existing:
                continue
            choices.append({
                "route": one["route"],
                "label": f"{one['provider']} web — {one['title']}",
                "model": "consumer web chat",
                "kind": "web-chat",
                "ready": True,
                "why_not": "",
                "how_to_fix_it": "",
                "connection_state": "connected",
                "retryable": True,
                "can_sign_in": False,
                "setup_blocked": False,
                "trouble_last_time": "",
                "chat_destination": {
                    "kind": "provider-web-chat", "owner_label": "Nexus Harness",
                    "connected": True,
                    "provider_label": f"{one['provider']} web — {one['title']}",
                    "provider_app_linked": True, "route": one["route"],
                    "model": "consumer web chat", "transcript_path": "",
                    "transcript_exists": False,
                    "url": one["url"],
                    "web_chat_id": one["id"],
                    "explanation": (
                        "Nexus relays turns through the logged-in provider page and saves the full multi-agent transcript locally."
                    ),
                },
            })
        for agent in standing.get("board", {}).get("agents", []):
            route = str(agent.get("who") or "")
            if not route.startswith("web:"):
                continue
            found = by_route.get(route)
            agent["assistant_kind"] = "web-chat"
            agent["ready"] = found is not None
            agent["why_not"] = "" if found else (
                "This web chat is not connected to the Electron app. Open Web AI chats and reconnect it."
            )
            agent["chat_destination"] = {
                "kind": "provider-web-chat", "owner_label": "Nexus Harness",
                "connected": found is not None,
                "provider_label": (
                    f"{(found or {}).get('provider', 'Web')} web — {(found or {}).get('title', route)}"
                ),
                "provider_app_linked": found is not None, "route": route,
                "model": "consumer web chat", "transcript_path": "",
                "transcript_exists": False,
                "url": (found or {}).get("url", ""),
                "web_chat_id": (found or {}).get("id", route.removeprefix("web:")),
                "explanation": (
                    "Nexus relays turns through the logged-in provider page and saves the full multi-agent transcript locally."
                    if found else "Reconnect this provider chat from Web AI chats before sending."
                ),
            }
        return standing

    def route(self, route: str) -> dict[str, Any] | None:
        return next((one for one in self.routes() if one["route"] == route), None)

    def provider(self, route: str) -> "WebChatProvider":
        if not WEB_ROUTE.fullmatch(str(route or "")):
            raise HarnessError("That is not a Nexus web-chat route")
        return WebChatProvider(self, route)

    def ask(self, route: str, request: ProviderRequest) -> ProviderResponse:
        connection = self.route(route)
        if connection is None:
            raise HarnessError(
                "That web chat is not connected to the desktop app right now. "
                "Open Web AI chats on the board and reconnect it."
            )
        prompt = _prompt_for(request)
        conversation_key = _safe_conversation_key(request.conversation_key)
        wanted = WebRequest(
            uuid.uuid4().hex, route, prompt,
            attachments=[
                {"name": str(one.get("name") or "")[:180], "path": str(one.get("path") or "")}
                for one in request.attachments if isinstance(one, dict) and one.get("path")
            ],
            conversation_key=conversation_key,
            prefer_existing_conversation=bool(request.prefer_existing_conversation),
        )
        deadline = time.monotonic() + min(
            WEB_WAIT_SECONDS, max(1.0, float(request.timeout_seconds or WEB_WAIT_SECONDS))
        )
        with self._condition:
            self._requests[wanted.request_id] = wanted
            self._condition.notify_all()

        def wake_waiter() -> None:
            with self._condition:
                self._condition.notify_all()

        unregister = cancellation.register(wake_waiter)
        try:
            with self._condition:
                while wanted.state not in {"complete", "error"}:
                    cancellation.checkpoint()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        wanted.state = "error"
                        wanted.error = "The provider web chat did not answer before the Nexus wait ended."
                        break
                    self._condition.wait(min(remaining, 1.0))
                cancellation.checkpoint()
        finally:
            unregister()
            with self._condition:
                self._requests.pop(wanted.request_id, None)
        if wanted.state != "complete":
            raise HarnessError(wanted.error or "The provider web chat did not answer")
        return ProviderResponse(
            text=wanted.answer,
            finish_reason="stop",
            raw={"web_chat": route, "milliseconds": wanted.milliseconds},
        )

    def pending(self) -> list[dict[str, Any]]:
        with self._condition:
            pending = [one for one in self._requests.values() if one.state == "queued"]
            for one in pending:
                one.state = "claimed"
            return [one.to_dict() for one in pending]

    def complete(self, request_id: object, *, answer: object = "", error: object = "",
                 milliseconds: object = 0, model: object = "") -> None:
        wanted_id = str(request_id or "").strip()
        if not REQUEST_ID.fullmatch(wanted_id):
            raise HarnessError("That web-chat request ID is not valid")
        with self._condition:
            wanted = self._requests.get(wanted_id)
            if wanted is None:
                return
            problem = bounded_redacted_text(
                self._redactor, " ".join(str(error or "").split()), 65_536
            )
            text = str(answer or "").strip()
            if len(text) > MAX_WEB_ANSWER_CHARACTERS:
                problem = (
                    f"The provider web chat returned {len(text):,} characters, above "
                    f"the disclosed {MAX_WEB_ANSWER_CHARACTERS:,}-character bridge limit. "
                    "Nexus did not truncate or save a partial answer."
                )
                text = ""
            if problem or not text:
                wanted.state = "error"
                wanted.error = problem or "The provider web chat returned no visible answer."
            else:
                wanted.state = "complete"
                wanted.answer = text
                wanted.milliseconds = max(0, int(milliseconds or 0))
                wanted.model = str(model or "")[:120]
            self._condition.notify_all()


class WebChatProvider:
    """The Provider-shaped end of a connection owned by Electron."""

    def __init__(self, broker: WebChatBroker, route: str):
        self.broker = broker
        self.route = route

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        return self.broker.ask(self.route, request)


def _prompt_for(request: ProviderRequest) -> str:
    latest = ""
    for message in reversed(request.messages):
        if str(message.get("role") or "") == "user":
            latest = str(message.get("content") or "")
            break
    parts = [
        "NEXUS WEB-CHAT TURN",
        "You are participating as an AI agent on a Nexus Harness board. Reply to the task below; do not merely describe how another agent could do it.",
    ]
    if request.system_prefix:
        parts.append(f"Nexus instructions:\n{request.system_prefix}")
    # Consumer web models give the end of a prompt disproportionate weight.
    # Putting the raw user text after the agent identity/phase instructions made
    # a peer treat first-person questions as if the user had addressed that
    # peer directly (for example Gemini answering "are you ChatGPT?").  Keep the
    # user's words quoted as data, then finish with the authoritative role and
    # phase assignment that explains who should answer whom.
    parts.append(f"Quoted user request:\n{latest}")
    if request.dynamic_context:
        parts.append(f"Authoritative role and turn instructions from Nexus:\n{request.dynamic_context}")
    if request.response_format is not None:
        parts.append(
            "Return only JSON matching this schema. Put the entire JSON object inside "
            "one fenced ```json code block. This fence is a transport boundary: it prevents "
            "the provider page's Markdown renderer from consuming literal characters such "
            "as *, _, <, and > inside proposed source files. Do not put any text before or "
            "after the code block:\n"
            + json.dumps(request.response_format.schema, ensure_ascii=False)
        )
    return "\n\n".join(one for one in parts if one).strip()


_active = WebChatBroker()


def active() -> WebChatBroker:
    return _active


def replace_active(broker: WebChatBroker) -> None:
    global _active
    _active = broker
