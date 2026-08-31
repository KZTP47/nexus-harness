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
from .models import HarnessError, ProviderOutcomeUnknown, ProviderRequest, ProviderResponse
from .redaction import CredentialRedactor, bounded_redacted_text

WEB_ROUTE = re.compile(r"^web:([a-z0-9][a-z0-9-]{5,63})$")
REQUEST_ID = re.compile(r"^[a-f0-9]{32}$")
CONVERSATION_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
HEARTBEAT_SECONDS = 15.0
WEB_WAIT_SECONDS = 420.0
# Waiting for the one physical browser/account slot is a distinct phase from
# waiting for the provider to answer.  Keep that admission phase bounded too,
# but never spend the provider's service budget before Electron claims the
# turn and can actually submit it.
WEB_QUEUE_WAIT_SECONDS = WEB_WAIT_SECONDS
# If Python's service budget ends before Electron reports its exact terminal
# result, the provider/account may still be generating.  Fence that physical
# session long enough for Electron's bounded relay to finish, then recover
# automatically even if its late receipt is lost forever.
WEB_UNCERTAIN_RESOURCE_SECONDS = WEB_WAIT_SECONDS
MAX_WEB_ANSWER_CHARACTERS = 8_000_000
MAX_WEB_CONNECTIONS = 256
WEB_RECEIPT_SECONDS = 15 * 60.0
MAX_WEB_RECEIPTS = 2_048


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
    delivery_state: str = ""
    failure_code: str = ""
    diagnostics: dict[str, Any] = field(default_factory=dict)
    attachments: list[dict[str, str]] = field(default_factory=list)
    conversation_key: str = ""
    prefer_existing_conversation: bool = False
    # Capture the physical browser/account resource at admission time.  Live
    # heartbeats are mutable: a user can remove or relabel a route while its
    # turn is still running.  Re-deriving this value from `_connections`
    # would then make the claimed turn appear to release its provider slot
    # and could submit a second prompt through the same consumer session.
    physical_resource_id: str = ""
    service_timeout_seconds: float = WEB_WAIT_SECONDS
    queued_at: float = field(default_factory=time.monotonic)
    queue_deadline: float = 0.0
    claimed_at: float = 0.0
    completion_deadline: float = 0.0
    queue_blocked_by_resource: bool = False
    queue_blocked_by_uncertain_resource: bool = False

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

    def __init__(
        self, *, queue_wait_seconds: float = WEB_QUEUE_WAIT_SECONDS,
        uncertain_resource_seconds: float = WEB_UNCERTAIN_RESOURCE_SECONDS,
    ) -> None:
        self._condition = threading.Condition()
        self._connections: dict[str, dict[str, Any]] = {}
        self._requests: dict[str, WebRequest] = {}
        # A completion can reach Python even when the HTTP response carrying
        # its acknowledgement is lost.  Keep a small, expiring idempotency
        # window so the renderer can retry the receipt without ever sending
        # the prompt to the provider a second time.
        self._receipts: dict[str, float] = {}
        # request_id -> (immutable physical browser/account resource, monotonic expiry)
        self._uncertain_requests: dict[str, tuple[str, float]] = {}
        self._redactor = CredentialRedactor()
        try:
            wanted_queue_wait = float(queue_wait_seconds)
        except (TypeError, ValueError):
            wanted_queue_wait = WEB_QUEUE_WAIT_SECONDS
        self._queue_wait_seconds = min(
            WEB_QUEUE_WAIT_SECONDS, max(0.01, wanted_queue_wait)
        )
        try:
            wanted_uncertain_wait = float(uncertain_resource_seconds)
        except (TypeError, ValueError):
            wanted_uncertain_wait = WEB_UNCERTAIN_RESOURCE_SECONDS
        self._uncertain_resource_seconds = min(
            WEB_UNCERTAIN_RESOURCE_SECONDS, max(0.01, wanted_uncertain_wait)
        )

    def heartbeat(self, connections: object) -> list[dict[str, Any]]:
        if not isinstance(connections, list):
            raise HarnessError("Web chat connections must be a list")
        if len(connections) > MAX_WEB_CONNECTIONS:
            raise HarnessError(
                f"Nexus can keep at most {MAX_WEB_CONNECTIONS} web-chat connections "
                "reachable at once. Remove an unused connection in Web AI chats and try again."
            )
        now = time.time()
        seen: set[str] = set()
        with self._condition:
            for raw in connections:
                if not isinstance(raw, dict):
                    continue
                connection_id = str(raw.get("id") or "").strip().lower()
                route = f"web:{connection_id}"
                if not WEB_ROUTE.fullmatch(route):
                    continue
                provider = str(raw.get("provider") or "web").strip()[:40]
                provider_key = re.sub(r"[^a-z0-9]+", "-", provider.lower()).strip("-") or "web"
                title = " ".join(str(raw.get("title") or "Web chat").split())[:120]
                url = str(raw.get("url") or "")[:1000]
                self._connections[route] = {
                    "id": connection_id,
                    "route": route,
                    "provider": provider,
                    "title": title or "Web chat",
                    "url": url,
                    # Electron deliberately shares one persistent browser
                    # profile/account per provider.  Tell the engine the
                    # physical capacity truth so two apparent conversations
                    # cannot race through the same consumer session.
                    "physical_resource_id": f"web-session:{provider_key}",
                    "physical_resource_capacity": 1,
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
                "physical_resource_id": one["physical_resource_id"],
                "physical_resource_capacity": one["physical_resource_capacity"],
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
            agent["physical_resource_id"] = (found or {}).get("physical_resource_id", "")
            agent["physical_resource_capacity"] = (found or {}).get(
                "physical_resource_capacity", 0
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
        service_timeout = min(
            WEB_WAIT_SECONDS, max(1.0, float(request.timeout_seconds or WEB_WAIT_SECONDS))
        )
        queued_at = time.monotonic()
        wanted = WebRequest(
            uuid.uuid4().hex, route, prompt,
            attachments=[
                {"name": str(one.get("name") or "")[:180], "path": str(one.get("path") or "")}
                for one in request.attachments if isinstance(one, dict) and one.get("path")
            ],
            conversation_key=conversation_key,
            prefer_existing_conversation=bool(request.prefer_existing_conversation),
            physical_resource_id=str(connection.get("physical_resource_id") or route),
            service_timeout_seconds=service_timeout,
            queued_at=queued_at,
            queue_deadline=queued_at + min(self._queue_wait_seconds, service_timeout),
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
                    deadline = (
                        wanted.completion_deadline
                        if wanted.state == "claimed" else wanted.queue_deadline
                    )
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        now = time.monotonic()
                        if wanted.state == "claimed":
                            wanted.state = "error"
                            wanted.error = (
                                "The desktop relay claimed this turn but did not return a completion "
                                "receipt before the provider service wait ended."
                            )
                            wanted.delivery_state = "unknown"
                            wanted.failure_code = "relay_completion_missing"
                            wanted.diagnostics = {
                                "relay_claimed": True,
                                "failure_stage": "provider_completion",
                                "resource_fence_seconds": self._uncertain_resource_seconds,
                            }
                            # Returning an unknown outcome does not prove that
                            # the physical browser session stopped. Keep its
                            # immutable slot occupied until Electron's exact
                            # late terminal receipt arrives or this bounded
                            # safety grace expires.
                            self._fence_uncertain_locked(wanted, now)
                        else:
                            self._expire_queued_locked(wanted, now)
                        break
                    self._condition.wait(min(remaining, 1.0))
                cancellation.checkpoint()
        except cancellation.ChatCancelled:
            with self._condition:
                if wanted.state == "claimed":
                    # Stopping Python does not itself prove that Electron
                    # reached or stopped the provider page. Keep the immutable
                    # physical slot fenced until the desktop relay returns its
                    # exact terminal receipt (including a confirmed Stop), or
                    # until the same bounded safety grace expires.
                    # Close the completion boundary before releasing the
                    # condition lock. A racing Electron receipt must observe
                    # cancellation, release the fence, and return False; it
                    # must never rewrite the stopped Python turn as accepted.
                    wanted.state = "cancelled"
                    self._fence_uncertain_locked(wanted, time.monotonic())
            raise
        finally:
            unregister()
            with self._condition:
                self._requests.pop(wanted.request_id, None)
        if wanted.state != "complete":
            message = wanted.error or "The provider web chat did not answer"
            diagnostic_values = dict(wanted.diagnostics)
            if wanted.failure_code:
                diagnostic_values.setdefault("failure_code", wanted.failure_code)
            if diagnostic_values:
                safe = ", ".join(
                    f"{key}={value}" for key, value in diagnostic_values.items()
                    if isinstance(value, (str, int, float, bool))
                )
                if safe:
                    message += f" [relay diagnostic: {safe[:1_000]}]"
            if wanted.delivery_state == "unknown":
                raise ProviderOutcomeUnknown(message)
            raise HarnessError(message)
        return ProviderResponse(
            text=wanted.answer,
            finish_reason="stop",
            raw={"web_chat": route, "milliseconds": wanted.milliseconds},
        )

    def pending(self) -> list[dict[str, Any]]:
        with self._condition:
            now = time.monotonic()
            self._prune_uncertain_locked(now)
            uncertain_resources = {
                resource for resource, _expires_at in self._uncertain_requests.values()
            }
            busy_resources = {
                one.physical_resource_id or one.route
                for one in self._requests.values()
                if one.state == "claimed"
            } | uncertain_resources
            pending: list[WebRequest] = []
            expired = False
            for one in sorted(self._requests.values(), key=lambda held: held.made_at):
                if one.state != "queued":
                    continue
                if now >= one.queue_deadline:
                    self._expire_queued_locked(one, now)
                    expired = True
                    continue
                resource = one.physical_resource_id or one.route
                if resource in busy_resources:
                    one.queue_blocked_by_resource = True
                    if resource in uncertain_resources:
                        one.queue_blocked_by_uncertain_resource = True
                    continue
                one.state = "claimed"
                one.claimed_at = now
                one.completion_deadline = now + one.service_timeout_seconds
                busy_resources.add(resource)
                pending.append(one)
            if expired or pending:
                self._condition.notify_all()
            return [one.to_dict() for one in pending]

    @staticmethod
    def _expire_queued_locked(wanted: WebRequest, now: float) -> None:
        """Close one never-submitted turn with an exact admission diagnosis."""

        waited = max(0.0, now - wanted.queued_at)
        wanted.state = "error"
        wanted.delivery_state = "not_accepted"
        if wanted.queue_blocked_by_uncertain_resource:
            wanted.error = (
                "An earlier turn on this provider ended with an unknown outcome or an unconfirmed "
                "Stop, so Nexus kept the shared browser session fenced while Electron finished "
                "reconciling it. "
                "This queued turn was not submitted. Inspect the affected provider chat, or retry "
                "after the earlier relay finishes."
            )
            wanted.failure_code = "relay_uncertain_resource_timeout"
            failure_stage = "physical_resource_reconciliation"
        elif wanted.queue_blocked_by_resource:
            wanted.error = (
                "Another turn kept this provider's shared signed-in browser session busy until "
                "the bounded queue-admission wait ended. This turn was not submitted."
            )
            wanted.failure_code = "relay_queue_admission_timeout"
            failure_stage = "physical_resource_queue"
        else:
            wanted.error = (
                "The desktop relay did not claim this turn before the bounded queue-admission "
                "wait ended. This turn was not submitted."
            )
            wanted.failure_code = "relay_not_claimed"
            failure_stage = "desktop_bridge_admission"
        wanted.diagnostics = {
            "relay_claimed": False,
            "failure_stage": failure_stage,
            "queue_wait_seconds": round(waited, 3),
        }

    def _prune_uncertain_locked(self, now: float) -> None:
        for request_id, (_resource, expires_at) in list(self._uncertain_requests.items()):
            if expires_at <= now:
                self._uncertain_requests.pop(request_id, None)

    def _fence_uncertain_locked(self, wanted: WebRequest, now: float) -> None:
        resource = wanted.physical_resource_id or wanted.route
        self._uncertain_requests[wanted.request_id] = (
            resource, now + self._uncertain_resource_seconds,
        )

    def _release_uncertain_locked(self, request_id: str) -> bool:
        released = self._uncertain_requests.pop(request_id, None) is not None
        if released:
            self._condition.notify_all()
        return released

    def _prune_receipts(self, now: float) -> None:
        expired_before = now - WEB_RECEIPT_SECONDS
        for request_id, received_at in list(self._receipts.items()):
            if received_at < expired_before:
                self._receipts.pop(request_id, None)
        while len(self._receipts) > MAX_WEB_RECEIPTS:
            self._receipts.pop(next(iter(self._receipts)))

    def complete(self, request_id: object, *, answer: object = "", error: object = "",
                 milliseconds: object = 0, model: object = "",
                 delivery_state: object = "", failure_code: object = "",
                 diagnostics: object = None) -> bool:
        wanted_id = str(request_id or "").strip()
        if not REQUEST_ID.fullmatch(wanted_id):
            raise HarnessError("That web-chat request ID is not valid")
        with self._condition:
            now = time.time()
            self._prune_receipts(now)
            self._prune_uncertain_locked(time.monotonic())
            wanted = self._requests.get(wanted_id)
            if wanted is None:
                if self._release_uncertain_locked(wanted_id):
                    # The provider attempt is terminal now, so its physical
                    # slot is safe to reuse. The already-returned Python
                    # outcome remains unknown and cannot be rewritten.
                    return False
                return wanted_id in self._receipts
            if wanted_id in self._receipts:
                return True
            # A request whose waiter already timed out/cancelled is a closed
            # delivery boundary.  A late renderer must not turn that durable
            # unknown/failure into a success merely by racing the waiter's
            # final removal from the in-memory table.
            if wanted.state != "claimed":
                self._release_uncertain_locked(wanted_id)
                return False
            was_claimed = wanted.state == "claimed"
            problem = bounded_redacted_text(
                self._redactor, " ".join(str(error or "").split()), 65_536
            )
            state = str(delivery_state or "").strip().lower()
            wanted.delivery_state = state if state in {"accepted", "not_accepted", "unknown"} else (
                "unknown" if was_claimed else "not_accepted"
            )
            wanted.failure_code = re.sub(
                r"[^a-z0-9_-]", "", str(failure_code or "").strip().lower()
            )[:80]
            wanted.diagnostics = {
                str(key)[:80]: self._redactor.text(str(value))[:240]
                for key, value in (diagnostics.items() if isinstance(diagnostics, dict) else [])
                if isinstance(value, (str, int, float, bool))
            }
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
            self._receipts[wanted_id] = now
            self._prune_receipts(now)
            self._condition.notify_all()
            return True


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
