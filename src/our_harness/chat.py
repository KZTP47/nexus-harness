"""Talking to the assistants you have hooked up.

The harness could already give an assistant a job, put one question to one mid
way through a run, and wire several of them together. What it could not do was
the plainest thing of all: let you type something and see what one of them
says.

That is what this is. One box, one assistant, and the conversation kept so you
can carry it on tomorrow. Whatever is set up on this machine can be talked to -
a seat you signed into, a model running here, a route with a key in an
environment variable - and all of them the same way, because they all go
through the same road as everything else in the harness.

Its boundaries, on purpose:

  - Send is conversation. It receives truthful board identity and anything the
    person explicitly attaches, but does not mutate the project.
  - Ask connected agents performs a real harness relay; a green line is never
    presented to the model as if a provider app had already been contacted.
  - Work together is explicit mutation authority. Provider output remains a
    proposal until confined paths and baselines pass the transaction boundary.
  - Not a place for credentials. Everything typed in and everything said back
    has credentials taken out of it before it is written down, the same as
    every other thing the harness keeps.
  - Not unbounded. A message is a message, a conversation is the last few
    dozen turns, and one answer has a time limit.
"""

from __future__ import annotations

import hashlib
import base64
import json
import mimetypes
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from . import cancellation
from .config import LoadedConfig
from .models import HarnessError, ProviderRequest, ResponseFormat
from .providers import ProviderRegistry, create_provider
from .redaction import CredentialRedactor, bounded_redacted_text
from .safety import confined_path

# Where the conversations are kept, so one survives closing the panel.
WHERE_THEY_LIVE = ".harness/chats"
# What happened the last time a route was asked something and would not answer.
# Kept so the board can say so before somebody types, rather than after.
WHERE_THE_NOES_LIVE = ".harness/chats/_last-refusals.json"
# How much of a refusal is kept. Enough to hold what the service said and what
# to do about it - those are the two halves somebody needs and they arrive as
# one sentence each - and short enough to sit under a name on the board.
LONGEST_NO = 800
# How long a refusal is worth mentioning. A service that was down on Friday
# says nothing about Monday, and a note nobody can clear is a note that stops
# being read. Anything getting through clears it long before this.
A_NO_IS_WORTH_MENTIONING_FOR = 24 * 60 * 60
# A long-horizon goal is often a real specification.  This is a disclosed hard
# safety boundary, not a convenient UI size: the browser and server advertise
# the same value and reject an over-limit request instead of silently clipping
# it.  Text files remain the better home for very large reference material.
MOST_LETTERS = 200_000
# How much of a conversation is kept and sent back. Enough to hold a thread of
# thought; few enough that the last turn does not cost the price of all of them.
MOST_KEPT = 40
# What one answer may be, and how long it may take.  No code may slice an
# answer to this size: exceeding it is a visible transport failure so Nexus can
# resume/retry without recording a plausible-looking fragment as complete.
LONGEST_ANSWER = 8_000_000

# One machine-readable policy owns every long-horizon conversation projection,
# from team discussion through final synthesis. The canonical collaboration
# ledger is never clipped; provider prompts get newest complete turns plus a
# deterministic semantic projection of older turns. Keep this here rather than
# in ``swarm_work`` so ``effective_limits`` and the orchestration engine cannot
# drift into two different, partly hidden policies.
LONG_HORIZON_CONTEXT_POLICY: dict[str, Any] = {
    "schema_version": 1,
    "phases": [
        "team_discussion", "planning", "execution", "verification",
        "final_synthesis",
    ],
    "prompt_transcript_characters": 120_000,
    "semantic_summary_characters": 40_000,
    "canonical_history": "append_only_paged_collaboration_ledger",
    "older_turns": "deterministic_semantic_summary",
    "newer_turns": "newest_complete_turns",
    "overflow_policy": "summarize_semantics_without_mid_turn_clipping",
}
# A turn can itself be much larger than a provider's useful context. Canonical
# JSON keeps every turn; provider history is a disclosed projection of newest
# complete turns and never slices the middle out of one.
CHAT_HISTORY_PROMPT_CHARACTERS = int(
    LONG_HORIZON_CONTEXT_POLICY["prompt_transcript_characters"]
)
LONGEST_WAIT_SECONDS = 600.0
# Non-interactive CLI adapters use this as their minimum capture budget.  The
# execution command limit is a different concern and used to truncate provider
# answers even when the response schema explicitly allowed larger file sets.
# Eight million answer characters can occupy almost 96 MB when a provider
# JSON-escapes non-BMP Unicode as surrogate pairs. This is a disclosed hard
# transport boundary, separate from local command/test output.
PROVIDER_TRANSPORT_OUTPUT_BYTES = 100_000_000
# How many can be asked the same thing at once.
MOST_AT_ONCE = 6
MOST_ATTACHMENTS = 6
MOST_ATTACHMENT_BYTES = 4_000_000
MOST_ATTACHMENTS_BYTES = 8_000_000
MOST_ATTACHMENT_TEXT = 1_000_000
# The name the default route is filed under, since it has no name of its own.
THE_USUAL_ONE = "the-usual-one"

# A provider account is not a provider-app conversation. Every adapter below
# either makes a headless command/API request or (for Microsoft 365 Copilot)
# opens a fresh remote conversation for one request. The durable conversation
# is the one Nexus keeps. This metadata is deliberately central rather than
# guessed by the page, so every chat surface tells the same truth.
_CHAT_CONNECTIONS: dict[str, tuple[str, str, str]] = {
    "claude-cli": (
        "Claude Code command line", "Claude Desktop",
        "Nexus sends each turn through Claude Code's non-interactive command line.",
    ),
    "codex-cli": (
        "Codex command line", "Codex desktop app",
        "Nexus runs an ephemeral, headless Codex command for each turn.",
    ),
    "gemini-cli": (
        "Gemini command line", "Gemini or Antigravity",
        "Nexus sends each turn through the Gemini command line.",
    ),
    "copilot-cli": (
        "GitHub Copilot command line", "GitHub Copilot chat",
        "Nexus sends each turn through the GitHub Copilot command line.",
    ),
    "assistant-cli": (
        "Configured assistant command", "",
        "Nexus sends each turn through the configured command.",
    ),
    "m365-copilot": (
        "Microsoft 365 Copilot through Microsoft Graph", "Microsoft 365 Copilot app",
        "Nexus opens a fresh Microsoft Graph Copilot conversation for each request and carries this Nexus history into it.",
    ),
    "openai": (
        "OpenAI API", "ChatGPT",
        "Nexus sends API requests and keeps the conversation here.",
    ),
    "anthropic": (
        "Anthropic API", "Claude",
        "Nexus sends API requests and keeps the conversation here.",
    ),
    "gemini": (
        "Gemini API", "Gemini app",
        "Nexus sends API requests and keeps the conversation here.",
    ),
    "ollama": (
        "Ollama service", "",
        "Nexus sends requests to the configured Ollama service and keeps the conversation here.",
    ),
    "openai-compatible": (
        "OpenAI-compatible API", "",
        "Nexus sends requests to the configured API and keeps the conversation here.",
    ),
    "local": (
        "Local provider", "",
        "Nexus asks the configured local provider and keeps the conversation here.",
    ),
}
# What each one is told about itself. Short on purpose: this is a conversation,
# not a job, and an assistant told it is running a job starts trying to run one.
HOW_TO_ANSWER = (
    "You are an AI agent on a Nexus Harness board, talking to a person working "
    "on a software project. Answer briefly and plainly and say when you do not "
    "know. Nexus may supply board identities, connected-agent replies, attached "
    "file contents, or images below; use that evidence. A green board connection "
    "means Nexus is allowed to relay messages, not that you can control another "
    "provider app yourself. Never claim you contacted another agent unless Nexus "
    "supplies its actual reply. Ordinary chat cannot change project files. "
    "In ordinary chat you cannot read their files except the attachments or "
    "bounded file contents Nexus explicitly supplies. "
    "The separate Work together action may apply explicitly proposed files through "
    "Nexus's bounded transaction layer."
)

# Provider diagnostics sometimes include the identity behind a subscription.
# An email address and the rest of an auth-status line are not needed to fix a
# connection and must not be kept in chat history or painted on the board.
_ACCOUNT_EMAIL = re.compile(
    r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"
)
_AUTH_STATUS_DETAILS = re.compile(
    r"It says of itself:\s*(signed in|not signed in)[^.]*\.", re.IGNORECASE
)
_CLAUDE_SUBSCRIPTION_REFUSALS = (
    "disabled claude subscription access",
    "claude code turned off",
    "subscription_access_disabled",
    "anthropic rejected the command-line subscription request",
)


def _claude_subscription_repair() -> str:
    return (
        "Anthropic rejected the command-line subscription request. This does not "
        "mean the installed Claude app is missing or signed out. Finish anything "
        "open in Claude, then run: claude auth logout, claude update, and claude "
        "auth login; choose Claude account with subscription. If claude -p still "
        "gets a 403, contact Anthropic support. An API key is separately billed "
        "and is never selected automatically."
    )


def _without_personal_account_details(said: str) -> str:
    held = _ACCOUNT_EMAIL.sub("[ACCOUNT EMAIL HIDDEN]", str(said or ""))
    held = _AUTH_STATUS_DETAILS.sub(
        lambda found: f"It says of itself: {found.group(1).lower()}.", held
    )
    # Older builds recorded a definitive and incorrect diagnosis for this 403,
    # sometimes followed by an account identity. Rewrite it while reading as
    # well as while writing so an already-saved refusal cannot keep exposing a
    # person or keep telling them their administrator deliberately disabled it.
    if any(mark in held.lower() for mark in _CLAUDE_SUBSCRIPTION_REFUSALS):
        return _claude_subscription_repair()
    return held


# How many conversations get a lock of their own before they start sharing one.
# Far above the number of assistants anybody has; low enough that a stream of
# made-up names cannot fill this machine's memory with locks.
MOST_LOCKS = 64
_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()


class ChatError(HarnessError):
    """Something that could not be said, or an answer that did not come."""


def _the_lock_for(filed: str) -> threading.Lock:
    """The lock for one conversation.

    Kept here rather than beside the panel's other locks, so that every way of
    reaching a conversation takes it. Saying one thing took a lock and asking
    everyone did not, and asking everyone says things too - so a turn could be
    read, written over, and gone, with nobody told.
    """

    with _locks_lock:
        held = _locks.get(filed)
        if held is None:
            if len(_locks) >= MOST_LOCKS:
                # Past the point of one each, they share. Sharing is slower and
                # still correct; growing for ever is neither.
                held = _locks.setdefault("", threading.Lock())
            else:
                held = threading.Lock()
                _locks[filed] = held
        return held


@dataclass
class Said:
    """One turn: who said it, what they said, and when."""

    who: str  # "you" or "them"
    text: str
    at: str
    milliseconds: int = 0
    model: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    speaker_id: str = ""
    speaker_name: str = ""
    speaker_route: str = ""
    recipient_id: str = ""
    recipient_name: str = ""
    phase: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = {
            "who": self.who,
            "text": self.text,
            "at": self.at,
            "milliseconds": self.milliseconds,
            "model": self.model,
            "attachments": [dict(one) for one in self.attachments],
        }
        for key in (
            "speaker_id", "speaker_name", "speaker_route",
            "recipient_id", "recipient_name", "phase",
        ):
            held = getattr(self, key)
            if held:
                value[key] = held
        return value


def _attachment_folder(config: LoadedConfig, route: str, filed_as: str) -> Path:
    return confined_path(
        config.project_root,
        Path(WHERE_THEY_LIVE) / "attachments" / _filed_under(filed_as or route),
        allow_missing=True,
        allow_control=True,
    )


def keep_attachments(
    config: LoadedConfig, route: str, supplied: object, filed_as: str = ""
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    """Validate, persist and prepare user-selected files for one provider turn."""

    if supplied in (None, []):
        return [], [], ""
    if not isinstance(supplied, list) or len(supplied) > MOST_ATTACHMENTS:
        raise ChatError(f"Attach at most {MOST_ATTACHMENTS} files at once.")
    total = 0
    kept: list[dict[str, Any]] = []
    provider_files: list[dict[str, Any]] = []
    text_blocks: list[str] = []
    folder = _attachment_folder(config, route, filed_as)
    folder.mkdir(parents=True, exist_ok=True)
    for position, raw in enumerate(supplied):
        if not isinstance(raw, dict):
            raise ChatError("An attachment is malformed.")
        name = Path(str(raw.get("name") or "")).name.strip()
        if not name or len(name) > 180:
            raise ChatError("Every attachment needs a short file name.")
        mime = str(raw.get("type") or mimetypes.guess_type(name)[0] or "application/octet-stream")
        encoded = str(raw.get("data") or "")
        if encoded.startswith("data:"):
            _, mark, encoded = encoded.partition(",")
            if not mark:
                raise ChatError(f"{name} has an invalid data URL.")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise ChatError(f"{name} is not a valid attachment.") from exc
        if not content or len(content) > MOST_ATTACHMENT_BYTES:
            raise ChatError(
                f"{name} is empty or larger than {MOST_ATTACHMENT_BYTES // 1_000_000} MB."
            )
        total += len(content)
        if total > MOST_ATTACHMENTS_BYTES:
            raise ChatError("The attachments together are larger than 8 MB.")
        textual = (
            mime.startswith("text/")
            or mime in {"application/json", "application/xml", "application/javascript"}
            or Path(name).suffix.lower() in {
                ".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".html", ".md",
                ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".csv",
            }
        )
        if textual:
            try:
                decoded = content.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise ChatError(
                    f"{name} is labelled as text but is not valid UTF-8 at byte "
                    f"{exc.start}. Nexus did not replace or discard any bytes. "
                    "Save it as UTF-8 or attach it as a binary reference."
                ) from exc
        else:
            decoded = ""
        if len(decoded) > MOST_ATTACHMENT_TEXT:
            raise ChatError(
                f"{name} contains more than {MOST_ATTACHMENT_TEXT:,} text characters. "
                "Nexus did not clip it. Split it into smaller files so every character "
                "can be supplied to the assistant."
            )
        attachment_id = uuid.uuid4().hex
        suffix = Path(name).suffix[:16]
        stored = folder / f"{attachment_id}{suffix}"
        beside = folder / f".{attachment_id}.part"
        descriptor = os.open(beside, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(beside, stored)
        public = {
            "id": attachment_id,
            "name": name,
            "type": mime,
            "size": len(content),
            "image": mime.startswith("image/"),
        }
        kept.append(public)
        provider_files.append({
            **public,
            "data": base64.b64encode(content).decode("ascii"),
            "path": str(stored),
        })
        if textual:
            text_blocks.append(f"ATTACHED TEXT FILE {position + 1}: {name}\n{decoded}")
    return kept, provider_files, "\n\n".join(text_blocks)


def attachment_path(
    config: LoadedConfig, route: str, filed_as: str, attachment_id: str
) -> tuple[Path, dict[str, Any]]:
    """Resolve only an attachment that is actually recorded in this chat."""

    if not re.fullmatch(r"[0-9a-f]{32}", str(attachment_id or "")):
        raise ChatError("That attachment id is invalid.")
    metadata = None
    for turn in read_it(config, route, filed_as):
        for one in turn.attachments:
            if one.get("id") == attachment_id:
                metadata = one
                break
    if metadata is None:
        raise ChatError("That attachment is not in this conversation.")
    folder = _attachment_folder(config, route, filed_as)
    matches = list(folder.glob(f"{attachment_id}.*"))
    if len(matches) != 1 or not matches[0].is_file():
        raise ChatError("That attachment is no longer available.")
    return matches[0], dict(metadata)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _filed_under(route: str) -> str:
    """The file name for one conversation.

    A route is a name somebody typed into a settings file, so it is checked
    rather than trusted. Nothing here may reach outside the chats folder.
    """

    said = str(route or "").strip() or THE_USUAL_ONE
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}", said):
        raise ChatError(
            f"{said!r} is not a name this can keep a conversation under. Names "
            "hold letters, numbers, spaces, dots, dashes and underscores."
        )
    tidy = said.replace(" ", "-").lower()
    if tidy == said and not (route and tidy == THE_USUAL_ONE):
        return tidy
    # A file name on Windows does not care about capitals, so "MyBot" and
    # "mybot" - two routes the settings treat as two - would share one file and
    # one conversation. A few letters of the exact name keep them apart, on any
    # machine, and only the names that need it carry them.
    marked = hashlib.sha256(said.encode("utf-8")).hexdigest()[:8]
    return f"{tidy}-{marked}"


def where_it_is_kept(config: LoadedConfig, route: str, filed_as: str = "") -> Path:
    """The file one conversation is kept in.

    `filed_as` is for when one assistant holds more than one conversation - two
    agents on a board both using Claude, say. Without it they would share one
    file and each would read the other's half of it.
    """

    return confined_path(
        config.project_root,
        f"{WHERE_THEY_LIVE}/{_filed_under(filed_as or route)}.json",
        allow_missing=True,
        allow_control=True,
    )


def chat_destination(
    config: LoadedConfig,
    route: str,
    filed_as: str = "",
    *,
    conversation_key: str = "",
    prefer_existing_conversation: bool = True,
) -> dict[str, Any]:
    """Say exactly where a chat lives and how its answers are obtained."""

    named = str(route or "").strip()

    def shared_files(where: Path) -> dict[str, Any]:
        from .collaboration_ledger import ledger_paths

        ledger = ledger_paths(config, named, filed_as)
        return {
            "transcript_path": where.relative_to(config.project_root).as_posix(),
            "transcript_exists": where.is_file(),
            "collaboration_path": ledger.markdown.relative_to(
                config.project_root
            ).as_posix(),
            "collaboration_exists": ledger.markdown.is_file(),
        }

    if named.startswith("web:"):
        # Web-chat routes are deliberately not part of ProviderRegistry: the
        # authenticated browser belongs to Electron and announces itself to
        # the in-process broker with a heartbeat.  Treating one like a static
        # provider route made a live Gemini/ChatGPT/Claude chat say "Missing
        # route" in its own header even while that same broker could answer it.
        from . import web_chats

        where = where_it_is_kept(config, named, filed_as)
        found = web_chats.active().route(named)
        connection_id = named.removeprefix("web:")
        provider = str((found or {}).get("provider") or "web").strip()
        title = str((found or {}).get("title") or connection_id).strip()
        provider_name = {
            "chatgpt": "ChatGPT",
            "claude": "Claude",
            "gemini": "Gemini",
            "copilot": "Microsoft Copilot",
        }.get(provider.lower(), provider.replace("-", " ").title() or "Web AI")
        connected = found is not None
        return {
            "kind": "provider-web-chat",
            "owner": "nexus",
            "owner_label": "Nexus Harness",
            "connected": connected,
            "provider_kind": "web-chat",
            "provider_label": (
                f"{provider_name} web — {title}"
                if connected else f"Disconnected web chat “{connection_id}”"
            ),
            "provider_app_name": provider_name if connected else "",
            "provider_app_linked": connected,
            "route": named,
            "model": "consumer web chat",
            **shared_files(where),
            "url": str((found or {}).get("url") or ""),
            "web_chat_id": str((found or {}).get("id") or connection_id),
            "web_conversation_key": str(
                conversation_key or _filed_under(filed_as or named)
            ),
            "web_prefer_existing_conversation": bool(prefer_existing_conversation),
            "explanation": (
                "Nexus relays turns through the logged-in provider page and "
                "saves the full multi-agent transcript locally."
                if connected else
                "Reconnect this provider chat from Web AI chats before sending. "
                "Its saved Nexus transcript is still available."
            ),
        }
    uses_project_default = not named and not bool(config.get("providers", {}))
    if not named and not uses_project_default:
        return {
            "owner": "nexus",
            "owner_label": "Nexus Harness",
            "connected": False,
            "provider_kind": "",
            "provider_label": "No assistant chosen",
            "provider_app_name": "",
            "provider_app_linked": False,
            "route": "",
            "model": "",
            "transcript_path": "",
            "transcript_exists": False,
            "collaboration_path": "",
            "collaboration_exists": False,
            "explanation": "Choose an assistant before this Nexus chat can send a message.",
        }
    try:
        routed = ProviderRegistry(config).provider_config(
            "default" if uses_project_default else named
        )
    except HarnessError:
        where = where_it_is_kept(config, named, filed_as)
        return {
            "owner": "nexus",
            "owner_label": "Nexus Harness",
            "connected": False,
            "provider_kind": "",
            "provider_label": f"Missing route “{named}”",
            "provider_app_name": "",
            "provider_app_linked": False,
            "route": named,
            "model": "",
            **shared_files(where),
            "explanation": "This Nexus chat is saved here, but its provider route is no longer configured.",
        }
    kind = str(routed.get("provider.name") or "")
    model = str(routed.get("provider.model") or "")
    provider_label, provider_app, explanation = _CHAT_CONNECTIONS.get(
        kind,
        (kind or "Configured provider", "", "Nexus asks the configured provider and keeps the conversation here."),
    )
    where = where_it_is_kept(config, named, filed_as)
    not_linked = (
        f"It is not linked to a chat in {provider_app}; that app will not contain these messages."
        if provider_app
        else "This provider exposes no separate app chat for Nexus to open."
    )
    return {
        "owner": "nexus",
        "owner_label": "Nexus Harness",
        "connected": True,
        "provider_kind": kind,
        "provider_label": provider_label,
        "provider_app_name": provider_app,
        "provider_app_linked": False,
        "route": named or "project default",
        "model": model,
        **shared_files(where),
        "explanation": f"{explanation} {not_linked}",
    }


def _cut_at_a_full_stop(said: str) -> str:
    """As much of a refusal as fits, ending where a sentence ends.

    Cut by counting letters alone, this landed in the middle of the sentence
    that says what to do about it - so the half somebody could act on was the
    half thrown away, and what was left stopped mid-thought like the app had
    crashed writing it.
    """

    held = " ".join(str(said or "").split())
    if len(held) <= LONGEST_NO:
        return held
    room = LONGEST_NO - 3
    ended = -1
    for mark in (". ", "? ", "! "):
        at = room
        while True:
            at = held.rfind(mark, 0, at)
            if at < 0:
                break
            # A full stop after one or two letters is "Mr." or somebody's
            # initial, not the end of anything. Stopping there leaves a line
            # that reads like a whole sentence with the useful half gone.
            before = held[:at].rsplit(" ", 1)[-1].strip(".")
            if len(before) > 2:
                ended = max(ended, at)
                break
            at = max(at - 1, 0)
            if at == 0:
                break
    if ended > room // 3:
        return held[:ended + 1] + "..."
    # No sentence ended anywhere in reach, so this is one long sentence.
    return held[:room] + "..."


# One at a time while the refusals file is changed. It is read, changed and
# written back, which is three things, and two routes failing in the same moment
# each wrote back what the other had not seen. One of the two notes then never
# existed - and the one that goes missing is the one nobody knows to look for.
_while_writing_the_noes = threading.RLock()


def _where_the_noes_are(config: LoadedConfig) -> Path:
    return confined_path(
        config.project_root, WHERE_THE_NOES_LIVE, allow_missing=True, allow_control=True)


def what_would_not_answer(config: LoadedConfig) -> dict[str, dict[str, Any]]:
    """What each route said the last time it would not answer, if it still counts.

    A note that cannot be read is worth nothing and a note that cannot be got
    rid of is worth less, so anything old enough to be about a different day is
    dropped on the way out.
    """

    where = _where_the_noes_are(config)
    with _while_writing_the_noes:
        try:
            held = json.loads(where.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(held, dict):
            return {}
        now = time.time()
        kept: dict[str, dict[str, Any]] = {}
        cleaned = dict(held)
        changed = False
        redactor = CredentialRedactor(config)
        for route, one in held.items():
            if not isinstance(one, dict) or not isinstance(one.get("why"), str):
                continue
            safe_why = bounded_redacted_text(
                redactor, _without_personal_account_details(one["why"]), 65_536
            )
            if safe_why != one["why"]:
                cleaned[route] = dict(one, why=safe_why)
                changed = True
            when = one.get("when")
            if not isinstance(when, (int, float)) or now - when > A_NO_IS_WORTH_MENTIONING_FOR:
                continue
            kept[str(route)] = {
                "why": safe_why,
                "when": float(when),
                "at": str(one.get("at") or ""),
            }
        # Earlier builds persisted the whole auth-status line. Clean it from
        # the existing file as soon as it is read, not only from the screen.
        if changed:
            try:
                beside = where.with_name(
                    f"{where.name}.{os.getpid()}-{threading.get_ident()}.part"
                )
                beside.write_text(json.dumps(cleaned, indent=2) + "\n", encoding="utf-8")
                os.replace(beside, where)
            except OSError:
                pass
        return kept


def _write_down_that_it_would_not(config: LoadedConfig, route: str, why: str) -> None:
    """Remember a refusal, or forget one, without ever failing over it.

    This is bookkeeping around somebody's message. If it cannot be written the
    message still went and the answer still came back, and throwing here would
    turn a working chat into a broken one over a note.
    """

    try:
        # One at a time, and inside the guard. Working out where the file goes
        # can throw as easily as writing it, and neither is worth turning
        # somebody's working chat into a broken one.
        with _while_writing_the_noes:
            where = _where_the_noes_are(config)
            held = what_would_not_answer(config)
            if why:
                held[route] = {
                    "why": bounded_redacted_text(
                        CredentialRedactor(config),
                        _without_personal_account_details(why),
                        65_536,
                    ),
                    "when": time.time(),
                    "at": _now(),
                }
            elif route not in held:
                return
            else:
                held.pop(route, None)
            where.parent.mkdir(parents=True, exist_ok=True)
            # Written beside and moved into place, like the conversations
            # themselves, so a panel reading this never catches it half written.
            beside = where.with_name(
                f"{where.name}.{os.getpid()}-{threading.get_ident()}.part")
            beside.write_text(json.dumps(held, indent=2) + "\n", encoding="utf-8")
            os.replace(beside, where)
    except (OSError, ValueError):
        return


def already_set_up(config: LoadedConfig) -> list[dict[str, Any]]:
    """Everyone that can be talked to right now, read from the settings.

    Kept apart from the list the panel shows because that one also looks over
    the machine for tools nobody has wired up yet, and looking means running
    each tool to ask its version. That is worth a second when somebody opens a
    tab, and not worth it before every message.
    """

    found: list[dict[str, Any]] = []
    routes = config.get("providers", {}) or {}
    # What was turned down last time somebody asked. Ready used to mean only
    # "there is a route written down for it", which is not what the word says:
    # somebody read every agent as ready, typed a message, and found out then.
    turned_down = what_would_not_answer(config)
    for name, held in sorted(routes.items()):
        if not isinstance(held, dict):
            continue
        no = turned_down.get(str(name))
        kind = str(held.get("kind") or held.get("name") or "")
        try:
            from .providers.subscription_cli import recipe_for

            can_sign_in = recipe_for(kind).interactive_login_arguments is not None
        except HarnessError:
            can_sign_in = False
        # A transient refusal remains retryable. This one is different: Gemini
        # says it cannot make any request until a route setting is supplied.
        # Calling that route ready leaves the input enabled but provides no
        # place to enter the value that every retry will keep asking for.
        needs_setup = bool(
            kind == "gemini-cli"
            and no
            and any(mark in no["why"].lower() for mark in (
                "google_cloud_project", "google cloud project", "google_project"
            ))
        )
        claude_needs_attention = bool(
            kind == "claude-cli"
            and no
            and any(mark in no["why"].lower() for mark in _CLAUDE_SUBSCRIPTION_REFUSALS)
        )
        found.append({
            "route": str(name),
            "label": str(name),
            "model": str(held.get("model") or ""),
            "kind": kind,
            # Ready normally means what it always meant: there is a route here
            # and something may be sent to it. Three other things read this word as
            # "may this be used at all" - a run picking who to set going,
            # asking everyone at once, and which conversation the panel opens -
            # so hanging last Tuesday's bad minute on it stopped all three,
            # quietly, for a day. The note itself said "send something and it
            # will try again", and then nothing would send anything. A warning
            # belongs beside the word and not inside it. The exception above is
            # not a past outage: it is a required route setting, so no retry can
            # start until the setting is supplied.
            # A provider refusal is a connection-health warning, not a permanent
            # kill switch. Keeping this retryable lets a refreshed OAuth session
            # recover without deleting and recreating the assistant.
            "ready": not needs_setup,
            "why_not": (
                "Gemini needs the Google Cloud project id for this Workspace account."
                if needs_setup else ""
            ),
            "how_to_fix_it": (
                "Press Set Cloud project on the agent card (or Connect it in the "
                "readiness list) and enter the Project ID. No API key is needed."
                if needs_setup else (_claude_subscription_repair() if claude_needs_attention else "")
            ),
            "connection_state": (
                "needs setup" if needs_setup else (
                    "needs attention" if no else "connected"
                )
            ),
            "retryable": not needs_setup,
            "can_sign_in": can_sign_in,
            "setup_blocked": False,
            "trouble_last_time": (
                (
                    "Gemini is installed and Nexus reached its command line, but "
                    "this Workspace account needs a Google Cloud Project ID."
                    if needs_setup else
                    f"The last time this was asked something, it would not answer: {no['why']}"
                )
                if no else ""
            ),
            "when_that_was": no["at"] if no else "",
            "chat_destination": chat_destination(config, str(name)),
        })
    if not found:
        # No named routes: the one this project uses is still somebody, and on
        # a machine with one seat it is the only one.
        kind = str(config.get("provider.name") or "")
        if kind:
            found.append({
                "route": "",
                "label": "The one this project uses",
                "model": str(config.get("provider.model") or ""),
                "kind": kind,
                "ready": True,
                "why_not": "",
                "how_to_fix_it": "",
                "chat_destination": chat_destination(config, ""),
            })
    return found


def who_can_talk(config: LoadedConfig) -> list[dict[str, Any]]:
    """Everyone you could type something to, and everyone you nearly can.

    The ones already set up come first and can be talked to now. The ones that
    are on the machine but have no route yet are listed too, greyed, with what
    to do about it - because "there is nobody here" is a worse answer than
    "here is who you could have in one press".
    """

    from . import team as team_lab

    found = already_set_up(config)
    known = {one["kind"] for one in found} | {one["route"] for one in found}
    try:
        here = team_lab.who_is_here(config)
    except HarnessError:
        here = {"members": []}
    for member in here.get("members", []):
        route = str(member.get("route") or "")
        if route in known or member.get("kind") in known:
            continue
        found.append({
            "route": route,
            "label": str(member.get("label") or route),
            "model": str(member.get("version") or ""),
            "kind": str(member.get("kind") or ""),
            "ready": False,
            "why_not": (
                str(member.get("why_not") or "")
                or "It is on this machine but nothing points at it yet."
            ),
            "how_to_fix_it": (
                str(member.get("install_hint") or "")
                or "Open Your team and press Set them up, then come back."
            ),
        })
    return found


def read_it(config: LoadedConfig, route: str, filed_as: str = "") -> list[Said]:
    """The conversation with one of them, oldest first."""

    where = where_it_is_kept(config, route, filed_as)
    events = where.with_suffix(".events.jsonl")
    from .safety import ProjectTransactionLock

    # JSONL and its external anchor are one authority. A reader must not see
    # the append after it lands but before the matching atomic anchor replace,
    # or the reverse. The same cross-process project lock used by _keep_it
    # covers the whole read, verification and legacy migration.
    with ProjectTransactionLock(config.project_root).held(30.0):
        if events.is_file():
            return _read_transcript_events(events)
        if not where.is_file():
            return []
        try:
            held = json.loads(where.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ChatError(
                f"The saved conversation at {where} cannot be read. Nexus preserved "
                "it and did not pretend the chat was empty."
            ) from exc
        if not isinstance(held, list):
            raise ChatError(
                f"The saved conversation at {where} has an invalid legacy format. "
                "Nexus preserved it and did not migrate a partial chat."
            )
        kept = _said_from_dicts(held)
        # First read migrates the legacy snapshot while still holding the same
        # authority as an append.
        if not events.exists():
            _append_transcript_event(events, [], "snapshot", kept)
        else:
            kept = _read_transcript_events(events)
        return kept


def _said_from_dicts(held: object) -> list[Said]:
    if not isinstance(held, list):
        raise ChatError(
            "A saved conversation event is not a turn list. Nexus did not skip it."
        )
    kept: list[Said] = []
    for index, one in enumerate(held):
        if not isinstance(one, dict):
            raise ChatError(
                f"Saved conversation turn {index + 1} is malformed. Nexus did not "
                "drop it or migrate the remaining turns."
            )
        who = str(one.get("who") or "")
        text = str(one.get("text") or "")
        if who not in ("you", "them") or not text:
            raise ChatError(
                f"Saved conversation turn {index + 1} has no valid speaker/text. "
                "Nexus did not drop it or migrate a partial chat."
            )
        attachments = one.get("attachments", [])
        if not isinstance(attachments, list) or any(
            not isinstance(item, dict) for item in attachments
        ):
            raise ChatError(
                f"Saved conversation turn {index + 1} has malformed attachments. "
                "Nexus did not drop them."
            )
        try:
            milliseconds = int(one.get("milliseconds") or 0)
        except (TypeError, ValueError) as exc:
            raise ChatError(
                f"Saved conversation turn {index + 1} has invalid timing metadata. "
                "Nexus did not skip it."
            ) from exc
        kept.append(Said(
            who=who, text=text, at=str(one.get("at") or ""),
            milliseconds=milliseconds, model=str(one.get("model") or ""),
            attachments=[dict(item) for item in attachments],
            speaker_id=str(one.get("speaker_id") or "")[:120],
            speaker_name=str(one.get("speaker_name") or "")[:240],
            speaker_route=str(one.get("speaker_route") or "")[:120],
            recipient_id=str(one.get("recipient_id") or "")[:120],
            recipient_name=str(one.get("recipient_name") or "")[:500],
            phase=str(one.get("phase") or "")[:80],
        ))
    return kept


def _transcript_event_hash(event: dict[str, Any]) -> str:
    unsigned = {
        key: value for key, value in event.items()
        if key not in {"hash", "integrity_mac"}
    }
    return hashlib.sha256(json.dumps(
        unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def _transcript_anchor_path(path: Path) -> Path:
    from .runtime_integrity import runtime_root

    identity = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()
    return runtime_root() / "transcript-anchors" / f"{identity}.json"


def _transcript_event_integrity(event: dict[str, Any]) -> str:
    from .runtime_integrity import mac

    unsigned = {key: value for key, value in event.items() if key != "integrity_mac"}
    return mac("conversation-transcript-event-v1", unsigned)


def _write_transcript_anchor(path: Path, records: list[dict[str, Any]]) -> None:
    from .runtime_integrity import atomic_text, mac

    value = {
        "schema_version": 1,
        "transcript": hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest(),
        "count": len(records),
        "head": str(records[-1].get("integrity_mac") or "") if records else "",
    }
    held = dict(value, integrity_mac=mac("conversation-transcript-anchor-v1", value))
    atomic_text(
        _transcript_anchor_path(path),
        json.dumps(held, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
    )


def _transcript_integrity_failure(path: Path, reason: str) -> None:
    from .runtime_integrity import quarantine_marker

    quarantine_marker(f"conversation-transcript:{path.resolve()}", path, reason)
    raise ChatError(
        "The append-only conversation record failed keyed integrity; Nexus "
        "quarantined it and preserved the evidence without rewriting it."
    )


def _read_transcript_event_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    previous = ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ChatError(
            f"The append-only conversation record at {path} cannot be read. "
            "Nexus did not pretend it was empty."
        ) from exc
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            break
        if (
            not isinstance(event, dict)
            or event.get("schema_version") != 1
            or event.get("seq") != len(records) + 1
            or event.get("previous_hash") != previous
            or event.get("hash") != _transcript_event_hash(event)
        ):
            break
        records.append(event)
        previous = str(event["hash"])
    if len(records) != len([line for line in lines if line.strip()]):
        raise ChatError("The append-only conversation record is damaged; Nexus refused to hide or extend its suffix.")
    anchor_path = _transcript_anchor_path(path)
    if anchor_path.exists():
        from .runtime_integrity import compare

        try:
            anchor = json.loads(anchor_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _transcript_integrity_failure(path, "The external transcript anchor is unreadable.")
        value = {key: anchor.get(key) for key in ("schema_version", "transcript", "count", "head")}
        expected_identity = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()
        if (
            value["schema_version"] != 1
            or value["transcript"] != expected_identity
            or not isinstance(value["count"], int)
            or value["count"] < 0
            or value["count"] > len(records)
            or not compare("conversation-transcript-anchor-v1", value, anchor.get("integrity_mac"))
        ):
            _transcript_integrity_failure(path, "The transcript no longer matches its external anchor.")
        for event in records:
            if event.get("integrity_mac") != _transcript_event_integrity(event):
                _transcript_integrity_failure(path, f"Transcript event {event.get('seq')} was rewritten.")
        anchored_count = int(value["count"])
        anchored_head = (
            str(records[anchored_count - 1].get("integrity_mac") or "")
            if anchored_count else ""
        )
        if value["head"] != anchored_head:
            _transcript_integrity_failure(
                path, "The transcript prefix no longer matches its external anchor."
            )
        if anchored_count < len(records):
            # A valid keyed suffix with the old prefix anchor is the one safe
            # crash state: append+fsync succeeded and the separate anchor move
            # did not. Advance only after every suffix event and chain link has
            # verified; rollback/rewrite still fails above.
            _write_transcript_anchor(path, records)
    elif records:
        # One-time migration of a valid legacy public hash chain. Once the
        # external anchor exists, missing keyed fields are corruption and this
        # migration can never be used to bless a later rewrite.
        have = [bool(one.get("integrity_mac")) for one in records]
        if any(have) and not all(have):
            _transcript_integrity_failure(path, "The transcript has a partial keyed chain.")
        if all(have):
            for event in records:
                if event.get("integrity_mac") != _transcript_event_integrity(event):
                    _transcript_integrity_failure(path, "A keyed transcript event is invalid.")
        else:
            for event in records:
                event["integrity_mac"] = _transcript_event_integrity(event)
            from .runtime_integrity import atomic_text

            atomic_text(path, "".join(
                json.dumps(one, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                for one in records
            ))
        _write_transcript_anchor(path, records)
    return records


def _read_transcript_events(path: Path) -> list[Said]:
    projection: list[Said] = []
    for event in _read_transcript_event_records(path):
        payload = event.get("turns")
        turns = _said_from_dicts(payload)
        if event.get("kind") == "snapshot":
            projection = turns
        elif event.get("kind") == "append":
            projection.extend(turns)
    return projection


def _append_transcript_event(
    path: Path, records: list[dict[str, Any]], kind: str, turns: list[Said]
) -> None:
    event: dict[str, Any] = {
        "schema_version": 1,
        "seq": len(records) + 1,
        "at": _now(),
        "kind": kind,
        "turns": [one.to_dict() for one in turns],
        "previous_hash": str(records[-1]["hash"]) if records else "",
    }
    event["hash"] = _transcript_event_hash(event)
    event["integrity_mac"] = _transcript_event_integrity(event)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    _write_transcript_anchor(path, [*records, event])


def _keep_it(
    config: LoadedConfig, route: str, turns: list[Said], filed_as: str = "",
    *, replace_projection: bool = False,
) -> None:
    where = where_it_is_kept(config, route, filed_as)
    where.parent.mkdir(parents=True, exist_ok=True)
    event_path = where.with_suffix(".events.jsonl")
    from .safety import ProjectTransactionLock

    with ProjectTransactionLock(config.project_root).held(30.0):
        records = _read_transcript_event_records(event_path) if event_path.exists() else []
        existing = _read_transcript_events(event_path) if records else []
        existing_dicts = [one.to_dict() for one in existing]
        requested = [one.to_dict() for one in turns]
        if requested[:len(existing_dicts)] == existing_dicts:
            delta = turns[len(existing_dicts):]
            if delta:
                _append_transcript_event(event_path, records, "append", delta)
        elif requested != existing_dicts and not replace_projection:
            common = 0
            for before, after in zip(existing_dicts, requested):
                if before != after:
                    break
                common += 1
            delta = turns[common:]
            if delta:
                _append_transcript_event(event_path, records, "append", delta)
            turns = [*existing, *delta]
            requested = [one.to_dict() for one in turns]
        elif requested != existing_dicts:
            # Historical migration can insert proven older blocks. Preserve
            # every prior event and append a replacement projection rather
            # than rewriting canonical history.
            _append_transcript_event(event_path, records, "snapshot", turns)
        written = json.dumps(requested, indent=2) + "\n"
    # Written beside and moved into place, so a panel reading it never sees
    # half a conversation.
        beside = where.with_name(f"{where.name}.{os.getpid()}-{threading.get_ident()}.part")
        beside.write_text(written, encoding="utf-8")
        for wait in (0.02, 0.05, 0.1, 0.2, 0.4):
            try:
                os.replace(beside, where)
                return
            except PermissionError:
                time.sleep(wait)
        os.replace(beside, where)


def merge_transcript(
    config: LoadedConfig, route: str, filed_as: str, recovered: list[Said]
) -> int:
    """Merge proven historical turns into one transcript without duplicates.

    Pair-chat migration uses this only after it has established ownership from
    explicit agent IDs. The original legacy file is never changed. Sorting by
    the stored ISO timestamp restores old blocks ahead of newer pair-native
    turns, while Python's stable sort preserves each block's internal order.
    """

    if not recovered:
        return 0
    with _the_lock_for(_filed_under(filed_as or route)):
        existing = read_it(config, route, filed_as)
        seen = {
            json.dumps(one.to_dict(), ensure_ascii=False, sort_keys=True)
            for one in existing
        }
        added: list[Said] = []
        for one in recovered:
            marked = json.dumps(one.to_dict(), ensure_ascii=False, sort_keys=True)
            if marked in seen:
                continue
            seen.add(marked)
            added.append(one)
        if not added:
            return 0
        combined = added + existing
        combined.sort(key=lambda one: str(one.at or ""))
        _keep_it(
            config, route, combined, filed_as, replace_projection=True
        )
        return len(added)


def start_again(config: LoadedConfig, route: str, filed_as: str = "") -> str:
    """Throw the conversation away and start a fresh one."""

    from .safety import take_the_file_away

    with _the_lock_for(_filed_under(filed_as or route)):
        where = where_it_is_kept(config, route, filed_as)
        from .safety import ProjectTransactionLock

        with ProjectTransactionLock(config.project_root).held(30.0):
            for path in (where, where.with_suffix(".events.jsonl")):
                if path.is_file():
                    take_the_file_away(path, missing_ok=True)
        from .collaboration_ledger import remove_ledger

        remove_ledger(config, route, filed_as)
    return "That conversation is gone. Say something and a new one starts."


def remove_conversation(config: LoadedConfig, route: str, filed_as: str = "") -> None:
    """Remove one exact transcript and only the attachments filed with it."""

    from .safety import take_the_file_away

    filed = _filed_under(filed_as or route)
    with _the_lock_for(filed):
        where = where_it_is_kept(config, route, filed_as)
        from .safety import ProjectTransactionLock

        with ProjectTransactionLock(config.project_root).held(30.0):
            for path in (where, where.with_suffix(".events.jsonl")):
                if path.is_file():
                    take_the_file_away(path, missing_ok=True)
        from .collaboration_ledger import remove_ledger

        remove_ledger(config, route, filed_as)
        folder = _attachment_folder(config, route, filed_as)
        if folder.is_dir() and not folder.is_symlink():
            # Attachment folders are flat and contain only Nexus-generated
            # names. Remove each exact file before the directory, rather than
            # recursively deleting a path assembled from user input.
            for child in folder.iterdir():
                if child.is_file() and not child.is_symlink():
                    take_the_file_away(child, missing_ok=True)
            try:
                folder.rmdir()
            except OSError:
                pass


def _check_what_was_typed(text: str) -> str:
    said = str(text or "")
    if not said.strip():
        raise ChatError("Type something first.")
    if len(said) > MOST_LETTERS:
        raise ChatError(
            f"That message is {len(said):,} characters; the effective limit is "
            f"{MOST_LETTERS:,}. Nexus did not truncate it. Attach or point at a file, "
            "or split the request into explicit consecutive parts."
        )
    if any(ord(letter) < 32 and letter not in "\t\n\r" for letter in said):
        raise ChatError("That message holds a control character.")
    return said


def effective_limits(
    config: LoadedConfig | None = None, route: str = ""
) -> dict[str, Any]:
    """The actual chat/transport budget shown by every composer.

    These are deliberately machine-readable so the UI never has to remember a
    second copy of a backend limit.  Route/model context windows can be smaller;
    a provider rejection is preserved verbatim (after credential redaction)
    rather than guessed or hidden here.
    """

    routed = config
    is_web = route.startswith("web:")
    if config is not None and route and not route.startswith("web:"):
        try:
            routed = ProviderRegistry(config).provider_config(route)
        except HarnessError:
            routed = config
    configured_output = 0
    configured_timeout = LONGEST_WAIT_SECONDS
    if config is not None:
        try:
            configured_output = int(config.get("execution.max_output_bytes"))
            configured_timeout = min(
                LONGEST_WAIT_SECONDS, float(routed.get("provider.timeout_seconds"))
            )
        except (TypeError, ValueError):
            pass
    if is_web:
        from .web_chats import WEB_WAIT_SECONDS

        configured_timeout = WEB_WAIT_SECONDS
    provider_kind = "web-chat" if is_web else str(
        routed.get("provider.name") if routed is not None else ""
    )
    token_limited_kinds = {
        "openai", "openai-compatible", "anthropic", "gemini", "ollama", "local",
    }
    cli_kinds = {
        "codex-cli", "claude-cli", "copilot-cli", "assistant-cli", "gemini-cli",
    }
    token_control = (
        "provider_page_uncontrolled" if is_web
        else "nexus_requested_maximum" if provider_kind in token_limited_kinds
        else "provider_cli_uncontrolled"
    )
    if is_web:
        provider_capture_bytes: int | None = None
        provider_capture_policy = "provider_page_answer_bridge"
        structured_capture_policy = "provider_page_answer_bridge"
    elif provider_kind in cli_kinds:
        provider_capture_bytes = max(2_000_000, configured_output)
        provider_capture_policy = "cli_plain_response_fixed"
        structured_capture_policy = "schema_derived"
    else:
        provider_capture_bytes = max(
            PROVIDER_TRANSPORT_OUTPUT_BYTES, configured_output
        )
        provider_capture_policy = "provider_http_or_process_transport"
        structured_capture_policy = "same_fixed_transport"
    return {
        "input_characters": MOST_LETTERS,
        "answer_characters": LONGEST_ANSWER,
        "provider_kind": provider_kind,
        "provider_capture_bytes": provider_capture_bytes,
        "provider_capture_policy": provider_capture_policy,
        "structured_capture_policy": structured_capture_policy,
        "turn_timeout_seconds": configured_timeout,
        "configured_provider_output_tokens": (
            None if token_control != "nexus_requested_maximum" else (
                int(routed.get("provider.max_output_tokens")) if routed is not None else 65_536
            )
        ),
        "output_token_control": token_control,
        "history_turns": MOST_KEPT,
        "history_prompt_characters": CHAT_HISTORY_PROMPT_CHARACTERS,
        "history_overflow_policy": "newest_complete_turns_with_canonical_reference",
        "attachments": {
            "count": MOST_ATTACHMENTS,
            "each_bytes": MOST_ATTACHMENT_BYTES,
            "total_bytes": MOST_ATTACHMENTS_BYTES,
            "text_characters_each": MOST_ATTACHMENT_TEXT,
        },
        "long_horizon_context": {
            **LONG_HORIZON_CONTEXT_POLICY,
            "phases": list(LONG_HORIZON_CONTEXT_POLICY["phases"]),
            "note": (
                "Team discussion, planning, execution, verification, and final "
                "synthesis use this same policy. "
                "Older requirements, decisions, facts, blockers, paths, and structured "
                "checkpoints are retained in a deterministic semantic summary; the full "
                "canonical history remains in the paged collaboration ledger."
            ),
        },
        "overflow_policy": "reject_without_truncation",
        "note": (
            "Nexus rejects oversized user-submitted prompt and answer payloads instead "
            "of silently truncating them. Long-horizon conversation history uses the "
            "disclosed semantic projection above while the full canonical history remains "
            "in the paged collaboration ledger. A provider can have a smaller model-specific "
            "context window; its redacted reason will be shown if so."
        ),
    }


_ONE_OUTER_FENCE = re.compile(
    r"\A\s*```(?:json|JSON)?[ \t]*\r?\n(?P<body>[\s\S]*?)\r?\n```\s*\Z"
)


def _structured_value(text: str) -> Any:
    """Decode a native JSON answer or one *outer* web-style JSON fence.

    Matching the whole response matters: taking the first fence corrupts JSON
    whenever a file body inside the result itself contains Markdown fences.
    """

    held = str(text or "").strip()
    fenced = _ONE_OUTER_FENCE.fullmatch(held)
    if fenced:
        held = fenced.group("body").strip()
    return json.loads(held)


def _contract_failure(text: str, response_format: ResponseFormat) -> str:
    from . import contracts

    try:
        value = _structured_value(text)
    except json.JSONDecodeError as exc:
        return f"response is not valid JSON ({exc.msg} at character {exc.pos})"
    failures = contracts.problems(value, response_format.schema)
    return "; ".join(failures[:8])


def _complete_with_one_schema_repair(
    provider, request: ProviderRequest, redactor: CredentialRedactor
):  # type: ignore[no-untyped-def]
    """Complete once, with exactly one correction for a malformed contract.

    This is intentionally below orchestration so every stateless CLI/API route
    gets the same bounded recovery. Consumer web chats keep their existing
    delivery-aware repair path because resending there can duplicate an
    uncertain browser side effect.
    """

    response = provider.complete(request)
    if request.response_format is None:
        return response
    failure = _contract_failure(response.text, request.response_format)
    if not failure:
        return response
    if not bool(getattr(provider, "structured_retry_is_safe", False)):
        raise ChatError(
            f"The assistant returned malformed {request.response_format.name} JSON. "
            "Nexus did not retry because this provider call is not proven side-effect-free: "
            f"{failure}"
        )
    # The malformed response is provider output and may echo credentials from
    # context.  Redact before it enters the retry prompt; never put the raw
    # payload in a user-facing exception, transcript, or collaboration ledger.
    rejected = redactor.text(str(response.text or ""))
    excerpt_limit = 16_000
    if len(rejected) > excerpt_limit:
        rejected_excerpt = (
            rejected[:8_000]
            + "\n...[middle omitted from repair prompt; original was not altered]...\n"
            + rejected[-8_000:]
        )
    else:
        rejected_excerpt = rejected
    correction = (
        "STRUCTURED FORMAT CORRECTION (one and only retry)\n"
        f"Your previous answer did not match {request.response_format.name}: {failure}.\n"
        "Return the complete answer again as one JSON value matching the supplied "
        "schema. Do not add prose or an outer wrapper.\n\n"
        f"PREVIOUS ANSWER (sha256 {hashlib.sha256(rejected.encode('utf-8')).hexdigest()})\n"
        + rejected_excerpt
    )
    repaired = provider.complete(replace(
        request,
        messages=[*request.messages, {"role": "assistant", "content": rejected_excerpt},
                  {"role": "user", "content": correction}],
        prefer_existing_conversation=False,
    ))
    second_failure = _contract_failure(repaired.text, request.response_format)
    if second_failure:
        raise ChatError(
            f"The assistant returned malformed {request.response_format.name} JSON twice. "
            f"Nexus kept the real cause and stopped instead of applying a partial result: "
            f"{second_failure}"
        )
    return repaired


def _checked_answer(text: str, who: str = "The assistant") -> str:
    held = str(text or "")
    if not held.strip():
        raise ChatError(f"{who} answered with nothing at all.")
    if len(held) > LONGEST_ANSWER:
        raise ChatError(
            f"{who} returned {len(held):,} characters, above Nexus's disclosed "
            f"{LONGEST_ANSWER:,}-character answer limit. Nexus did not save or "
            "truncate the answer; resume with a smaller/file-backed result."
        )
    return held


def say(
    config: LoadedConfig,
    route: str,
    text: str,
    filed_as: str = "",
    *,
    context: str = "",
    attachments: object = None,
    speaker: dict[str, Any] | None = None,
    recipients: list[dict[str, Any]] | None = None,
    conversation_key: str = "",
    prefer_existing_conversation: bool = True,
) -> dict[str, Any]:
    """Say one thing to one of them, and keep what comes back.

    The conversation so far goes with it, so this is a conversation and not a
    row of unrelated questions.

    `filed_as` keeps this conversation apart from any other going through the
    same assistant. The route still decides who is reached.
    """

    asked = _check_what_was_typed(text)
    redactor = CredentialRedactor(config)
    registry = ProviderRegistry(config)
    named = str(route or "").strip()
    try:
        if named.startswith("web:"):
            from . import web_chats

            provider = web_chats.active().provider(named)
            routed = config
        else:
            routed = registry.provider_config(named) if named else config
            provider = create_provider(routed)
    except HarnessError as exc:
        # Redacted, like everything else. A key typed into a settings file comes
        # back inside "incorrect API key provided: ..." when it is wrong, and
        # that sentence is put on the screen.
        raise ChatError(
            _without_personal_account_details(redactor.text(
                f"{named or 'The assistant this project uses'} cannot be reached: "
                f"{_in_plain_words(exc)}"
            ))
        ) from exc

    model = (
        f"{(web_chats.active().route(named) or {}).get('provider', 'web')} web chat"
        if named.startswith("web:") else str(routed.get("provider.model") or "")
    )
    # From here to the write is one piece of work: read what was said, add to
    # it, write it back. Two of those at once each write what the other did not
    # know about, and a turn disappears with nobody told.
    with _the_lock_for(_filed_under(filed_as or route)):
        kept_files, provider_files, attachment_text = keep_attachments(
            config, route, attachments, filed_as
        )
        dynamic = "\n\n".join(
            one for one in (str(context or "").strip(), attachment_text) if one
        )
        return _ask_and_keep(
            config,
            route,
            asked,
            provider,
            model,
            redactor,
            named,
            filed_as,
            dynamic_context=dynamic,
            kept_attachments=kept_files,
            provider_attachments=provider_files,
            speaker=speaker,
            recipients=recipients,
            conversation_key=conversation_key,
            prefer_existing_conversation=prefer_existing_conversation,
            max_output_tokens=int(routed.get("provider.max_output_tokens") or 65_536),
        )


def _project_chat_history(
    eligible: list[Any], *, speaker: Any, filed_as: str, route: str
) -> list[dict[str, str]]:
    """Newest complete canonical turns within the disclosed character budget."""

    candidates = eligible[-MOST_KEPT:]
    selected: list[Any] = []
    used = 0
    for one in reversed(candidates):
        text = (
            f"{one.speaker_name}: {one.text}"
            if speaker and one.who == "them" and one.speaker_name else one.text
        )
        needed = len(text)
        if needed > CHAT_HISTORY_PROMPT_CHARACTERS or used + needed > CHAT_HISTORY_PROMPT_CHARACTERS:
            continue
        selected.append((one, text))
        used += needed
    selected.reverse()
    selected_ids = {id(one) for one, _text in selected}
    omitted = [one for one in eligible if id(one) not in selected_ids]
    messages: list[dict[str, str]] = []
    if omitted:
        omitted_characters = sum(len(str(one.text or "")) for one in omitted)
        canonical = f"{WHERE_THEY_LIVE}/{_filed_under(filed_as or route)}.json"
        messages.append({
            "role": "user",
            "content": (
                "NEXUS CHAT-HISTORY PROJECTION — canonical conversation was not "
                f"changed. {len(omitted)} complete earlier turn(s), "
                f"{omitted_characters:,} characters, are omitted from this provider "
                f"request only. Full Nexus history: {canonical}. No turn was sliced."
            ),
        })
    messages.extend({
        "role": "user" if one.who == "you" else "assistant",
        "content": text,
    } for one, text in selected)
    return messages


def _ask_and_keep(
    config,
    route,
    asked,
    provider,
    model,
    redactor,
    named,
    filed_as="",
    *,
    dynamic_context="",
    kept_attachments=None,
    provider_attachments=None,
    speaker=None,
    recipients=None,
    conversation_key="",
    prefer_existing_conversation=False,
    max_output_tokens=65_536,
) -> dict[str, Any]:
    so_far = read_it(config, route, filed_as)
    eligible = [
        one for one in so_far
        if one.phase not in {
            "agent_reply", "lead_draft", "agent_plan", "lead_plan",
            "agent_discussion", "agent_plan_review", "lead_execution", "agent_execution",
            "agent_verification",
        }
    ]
    messages = _project_chat_history(
        eligible, speaker=speaker, filed_as=filed_as, route=route
    )
    messages.append({"role": "user", "content": redactor.text(asked)})
    # Built here rather than passed in, so everything that goes to an assistant
    # is built in the one place.
    request = ProviderRequest(
        system_prefix=HOW_TO_ANSWER,
        dynamic_context=str(dynamic_context or ""),
        messages=messages,
        model=model,
        temperature=0.3,
        max_output_tokens=max(1, int(max_output_tokens)),
        timeout_seconds=LONGEST_WAIT_SECONDS,
        attachments=list(provider_attachments or []),
        conversation_key=str(
            conversation_key or _filed_under(filed_as or route)
        ),
        prefer_existing_conversation=bool(prefer_existing_conversation),
    )
    started = time.monotonic()
    try:
        from .swarm_runs import provider_effect

        effect_digest = hashlib.sha256(json.dumps({
            "route": route, "conversation_key": request.conversation_key,
            "messages": request.messages, "response_format": bool(request.response_format),
        }, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        with provider_effect(config, route, request.conversation_key, effect_digest):
            answered = _complete_with_one_schema_repair(provider, request, redactor)
    except cancellation.ChatCancelled:
        # Stop is control flow, not a provider refusal.  Turning it into a
        # ChatError makes multi-round collaboration treat the user's stop as a
        # recoverable failed turn and immediately ask the agents again.
        raise
    except HarnessError as exc:  # noqa: PERF203 - one shape of failure, one sentence
        # Remembered against the route, so the board can say this before the
        # next person types rather than after.
        safe_reason = _without_personal_account_details(
            redactor.text(_in_plain_words(exc))
        )
        _write_down_that_it_would_not(config, route, safe_reason)
        raise ChatError(
            _without_personal_account_details(redactor.text(
                f"{named or 'The assistant'} was asked and did not answer: "
                f"{_in_plain_words(exc)}"
            ))
        ) from exc
    # It answered, so whatever it said last time is over.
    _write_down_that_it_would_not(config, route, "")
    back = _checked_answer(
        redactor.text(str(getattr(answered, "text", "") or "")),
        named or "The assistant",
    )
    turns = so_far + [
        Said(
            who="you",
            text=redactor.text(asked),
            at=_now(),
            attachments=list(kept_attachments or []),
            speaker_name="You" if speaker else "",
            recipient_id=",".join(
                str(one.get("id") or "") for one in (recipients or [])
            ),
            recipient_name=", ".join(
                str(one.get("name") or "An agent") for one in (recipients or [])
            )[:500],
            phase="user_prompt" if speaker else "",
        ),
        Said(
            who="them",
            text=back,
            at=_now(),
            milliseconds=int((time.monotonic() - started) * 1000),
            model=model,
            speaker_id=str((speaker or {}).get("id") or "")[:120],
            speaker_name=str((speaker or {}).get("name") or "")[:240],
            speaker_route=str((speaker or {}).get("who") or route)[:120]
            if speaker else "",
            recipient_name="You" if speaker else "",
            phase="final_answer" if speaker else "",
        ),
    ]
    _keep_it(config, route, turns, filed_as)
    return {
        "route": named,
        "said": [one.to_dict() for one in turns[-MOST_KEPT:]],
        "answer": turns[-1].to_dict(),
    }


def ask_once(
    config: LoadedConfig,
    route: str,
    text: str,
    *,
    context: str = "",
    provider_attachments: list[dict[str, Any]] | None = None,
    response_format: ResponseFormat | None = None,
    conversation_key: str = "",
    prefer_existing_conversation: bool = False,
) -> dict[str, Any]:
    """Ask without touching a transcript, for a bounded collaboration round."""

    asked = _check_what_was_typed(text)
    redactor = CredentialRedactor(config)
    named = str(route or "").strip()
    try:
        if named.startswith("web:"):
            from . import web_chats

            routed = config
            provider = web_chats.active().provider(named)
        else:
            routed = ProviderRegistry(config).provider_config(named) if named else config
            provider = create_provider(routed)
        request = ProviderRequest(
            system_prefix=HOW_TO_ANSWER,
            dynamic_context=str(context or ""),
            messages=[{"role": "user", "content": redactor.text(asked)}],
            model=str(routed.get("provider.model") or ""),
            temperature=0.2,
            max_output_tokens=max(1, int(routed.get("provider.max_output_tokens") or 65_536)),
            timeout_seconds=LONGEST_WAIT_SECONDS,
            response_format=response_format,
            attachments=list(provider_attachments or []),
            conversation_key=str(conversation_key or _filed_under(named)),
            prefer_existing_conversation=bool(prefer_existing_conversation),
        )
        started = time.monotonic()
        from .swarm_runs import provider_effect

        effect_digest = hashlib.sha256(json.dumps({
            "route": named, "conversation_key": request.conversation_key,
            "messages": request.messages, "response_format": bool(response_format),
        }, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        with provider_effect(config, named, request.conversation_key, effect_digest):
            response = (
                provider.complete(request)
                if named.startswith("web:")
                else _complete_with_one_schema_repair(provider, request, redactor)
            )
    except cancellation.ChatCancelled:
        # Collaboration catches this exact type and aborts the whole request.
        # Do not flatten it into an ordinary per-agent failure that the next
        # discussion round will retry.
        raise
    except HarnessError as exc:
        raise ChatError(
            _without_personal_account_details(
                redactor.text(f"{named or 'The assistant'} was asked and did not answer: {_in_plain_words(exc)}")
            )
        ) from exc
    answer = _checked_answer(
        redactor.text(str(response.text or "")), named or "The assistant"
    )
    return {
        "text": answer,
        "milliseconds": int((time.monotonic() - started) * 1000),
        "model": (
            f"{(web_chats.active().route(named) or {}).get('provider', 'web')} web chat"
            if named.startswith("web:") else str(routed.get("provider.model") or "")
        ),
    }


def keep_exchange(
    config: LoadedConfig,
    route: str,
    text: str,
    answer: str,
    *,
    filed_as: str = "",
    attachments: list[dict[str, Any]] | None = None,
    model: str = "",
    milliseconds: int = 0,
) -> dict[str, Any]:
    """Record a harness-orchestrated exchange after its side effects succeed."""

    redactor = CredentialRedactor(config)
    with _the_lock_for(_filed_under(filed_as or route)):
        turns = read_it(config, route, filed_as) + [
            Said("you", redactor.text(_check_what_was_typed(text)), _now(), attachments=list(attachments or [])),
            Said("them", _checked_answer(redactor.text(answer)), _now(), milliseconds, model),
        ]
        _keep_it(config, route, turns, filed_as)
    return {
        "route": str(route or "").strip(),
        "said": [one.to_dict() for one in turns[-MOST_KEPT:]],
        "answer": turns[-1].to_dict(),
    }


def keep_failed_exchange(
    config: LoadedConfig,
    route: str,
    text: str,
    error: str,
    *,
    filed_as: str = "",
    attachments: list[dict[str, Any]] | None = None,
    contributions: list[dict[str, Any]] | None = None,
    run_id: str = "",
    state: str = "failed",
) -> dict[str, Any]:
    """Keep the truth when an accepted chat turn saves no provider answer.

    Provider delivery can be uncertain, especially for consumer web pages. A
    failed turn previously existed only in the transient progress panel and an
    internal run journal, so reopening the chat made it look as though Nexus had
    ignored the user. Record the user's accepted turn and a clearly attributed
    Nexus outcome without pretending that an AI answered or automatically
    resending an ambiguous side effect.
    """

    redactor = CredentialRedactor(config)
    safe_text = redactor.text(_check_what_was_typed(text))
    safe_error = bounded_redacted_text(
        redactor, error or "Nexus did not receive an answer", 65_536
    ).strip()
    safe_state = re.sub(r"[^a-z0-9_-]", "", str(state or "failed").lower())[:40] or "failed"
    run_note = f" Run {str(run_id)[:64]}." if run_id else ""
    if safe_state in {"delivery_unknown", "outcome_unknown"}:
        explanation = (
            "Nexus could not prove whether the provider accepted or completed this turn, "
            "so it did not resend it and risk duplicate work. No AI answer was saved."
        )
    elif safe_state == "stopped":
        explanation = "This turn was stopped before an AI answer was saved."
    else:
        explanation = "This turn ended before an AI answer was saved."
    reason = safe_error if safe_error.endswith((".", "!", "?")) else safe_error + "."
    outcome = f"{explanation}\n\nReason: {reason}{run_note}"
    with _the_lock_for(_filed_under(filed_as or route)):
        turns = read_it(config, route, filed_as)
        now = _now()
        turns.append(Said(
            "you", safe_text, now, attachments=list(attachments or []),
            speaker_name="You", phase="user_prompt",
        ))
        for contribution in list(contributions or []):
            if not isinstance(contribution, dict) or not str(
                contribution.get("text") or ""
            ).strip():
                continue
            turns.append(Said(
                "them",
                _checked_answer(redactor.text(str(contribution.get("text") or ""))),
                now,
                max(0, int(contribution.get("milliseconds") or 0)),
                str(contribution.get("model") or "")[:200],
                speaker_id=str(contribution.get("speaker_id") or "")[:100],
                speaker_name=str(contribution.get("speaker_name") or "An agent")[:100],
                speaker_route=str(contribution.get("speaker_route") or "")[:100],
                recipient_id=str(contribution.get("recipient_id") or "")[:100],
                recipient_name=str(contribution.get("recipient_name") or "")[:100],
                phase=str(contribution.get("phase") or "agent_reply")[:40],
            ))
        turns.append(Said(
            "them", _checked_answer(outcome, "Nexus"), now,
            model=f"nexus/{safe_state}", speaker_id="nexus",
            speaker_name="Nexus", recipient_name="You", phase="nexus_error",
        ))
        _keep_it(config, route, turns, filed_as)
    return {
        "route": str(route or "").strip(),
        "said": [one.to_dict() for one in turns[-MOST_KEPT:]],
        "answer": turns[-1].to_dict(),
    }


def keep_multiparty_exchange(
    config: LoadedConfig,
    route: str,
    text: str,
    answer: str,
    *,
    filed_as: str,
    lead: dict[str, Any],
    participants: list[dict[str, Any]],
    contributions: list[dict[str, Any]],
    attachments: list[dict[str, Any]] | None = None,
    model: str = "",
    milliseconds: int = 0,
) -> dict[str, Any]:
    """Keep every real turn from a bounded fan-out/fan-in exchange.

    Provider calls remain transient, but their exact redacted replies are part
    of the conversation the user asked to see.  The legacy ``who`` field stays
    intact for compatibility; the explicit speaker/recipient/phase fields make
    a multi-party transcript unambiguous without pretending there were rounds
    that the orchestrator did not actually run.
    """

    redactor = CredentialRedactor(config)
    lead_id = str(lead.get("id") or "")
    lead_name = str(lead.get("name") or "The lead agent")
    team_name = ", ".join(str(one.get("name") or "an agent") for one in participants)
    team_ids = ",".join(str(one.get("id") or "") for one in participants)
    with _the_lock_for(_filed_under(filed_as or route)):
        turns = read_it(config, route, filed_as)
        now = _now()
        turns.append(Said(
            "you", redactor.text(_check_what_was_typed(text)), now,
            attachments=list(attachments or []),
            speaker_name="You", recipient_id=team_ids,
            recipient_name=team_name, phase="user_prompt",
        ))
        for contribution in contributions:
            speaker_id = str(contribution.get("speaker_id") or "")
            is_lead = speaker_id == lead_id
            recipient_id = str(contribution.get("recipient_id") or (
                lead_id if not is_lead else team_ids
            ))
            recipient_name = str(contribution.get("recipient_name") or (
                lead_name if not is_lead else "Team deliberation"
            ))[:240]
            turns.append(Said(
                "them",
                _checked_answer(redactor.text(str(contribution.get("text") or ""))),
                now,
                int(contribution.get("milliseconds") or 0),
                str(contribution.get("model") or ""),
                speaker_id=speaker_id,
                speaker_name=str(contribution.get("speaker_name") or "An agent")[:240],
                speaker_route=str(contribution.get("speaker_route") or "")[:120],
                recipient_id=recipient_id,
                recipient_name=recipient_name,
                phase=str(contribution.get("phase") or (
                    "lead_draft" if is_lead else "agent_reply"
                ))[:80],
            ))
        turns.append(Said(
            "them", _checked_answer(redactor.text(answer)), now,
            milliseconds, model,
            speaker_id=lead_id, speaker_name=lead_name,
            speaker_route=str(lead.get("who") or "")[:120],
            recipient_name="You", phase="final_answer",
        ))
        _keep_it(config, route, turns, filed_as)
    return {
        "route": str(route or "").strip(),
        "said": [one.to_dict() for one in turns[-MOST_KEPT:]],
        "answer": turns[-1].to_dict(),
    }


def ask_everyone(config: LoadedConfig, text: str) -> list[dict[str, Any]]:
    """Put the same thing to every assistant that is ready, all at once.

    This is the thing two subscriptions are actually for: the same question, two
    answers, side by side. They are asked at the same time, because asking six
    of them one after another is six waits.
    """

    asked = _check_what_was_typed(text)
    # From the settings, not from a fresh look over the machine: that look runs
    # every assistant's own tool, and asking six of them should not wait for it.
    ready = [one for one in already_set_up(config) if one["ready"]][:MOST_AT_ONCE]
    if not ready:
        raise ChatError(
            "Nobody is set up to answer yet. Open Your team and press Set them up."
        )

    def one_of_them(who: dict[str, Any]) -> dict[str, Any]:
        try:
            got = say(config, who["route"], asked)
            return {
                "route": who["route"],
                "label": who["label"],
                "answer": got["answer"]["text"],
                "milliseconds": got["answer"]["milliseconds"],
                "went_wrong": "",
            }
        except cancellation.ChatCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - one route may not fell the rest
            # One that will not answer must not stop the others being read, and
            # that has to hold for every way of not answering - not only the one
            # the harness has a name for. Anything else coming out of here ended
            # the whole round: every other assistant had already answered, or
            # was about to, and nobody saw any of it.
            return {
                "route": who["route"],
                "label": who["label"],
                "answer": "",
                "milliseconds": 0,
                "went_wrong": _in_plain_words(exc) if isinstance(exc, HarnessError) else (
                    f"{who['label']} stopped in a way nobody expected: "
                    f"{type(exc).__name__}"
                ),
            }

    with ThreadPoolExecutor(max_workers=min(len(ready), MOST_AT_ONCE)) as pool:
        return [
            future.result()
            for future in [cancellation.submit(pool, one_of_them, one) for one in ready]
        ]


# How much of a reason is worth reading. Four hundred letters was enough while a
# reason was one line out of a screen of noise; it is not enough now that the
# tools which will not answer are told to say what they know about themselves as
# well, and cutting that off in the middle wastes the part that says what to do.
def _in_plain_words(exc: Exception) -> str:
    """The sentence inside what a tool printed, rather than the whole of it.

    A tool that will not answer says why in one line and then wraps it in a
    screen of detail - machine-readable if it is a program, a whole web page if
    something in between answered instead. Either way, one line is what is worth
    reading, and the rest is what nobody reads.
    """

    said = str(exc)
    held, start = _the_answer_tacked_on_the_end(said)
    if held is not None:
        for key in ("result", "error", "message", "detail"):
            inside = held.get(key)
            if isinstance(inside, str) and inside.strip():
                # What came before the JSON goes through the same rule as
                # everything else. A gateway can answer with a page and the
                # upstream's own JSON one after the other, and this branch
                # used to hand the page back with its tags on.
                before = _without_markup(said[:start]).strip()
                return f"{before} {inside.strip()}".strip()
    return _without_markup(said)


# How many braces are tried before giving up looking for the JSON.
_HOW_MANY_BRACES_TRIED = 10


def _the_answer_tacked_on_the_end(said: str) -> tuple[dict[str, Any] | None, int]:
    """The JSON a tool put on the end, and where it starts.

    Looked for from the right. From the left, the first brace on an error page
    belongs to its own stylesheet - `body{background:#fff}` - the JSON after it
    never parses, and the whole thing is handed back with the braces showing.
    """

    at = len(said)
    for _try in range(_HOW_MANY_BRACES_TRIED):
        at = said.rfind("{", 0, at)
        if at == -1:
            return None, 0
        try:
            held = json.loads(said[at:])
        except json.JSONDecodeError:
            continue
        if isinstance(held, dict):
            return held, at
    return None, 0


# A whole web page: the first tag in the words opens a document. The tag has to
# end right there - `<html>` or `<html lang="en">` - because `<html-status>` is
# somebody's own tag and `<html:body>` is a namespace, and neither is a page.
_OPENS_A_DOCUMENT = re.compile(
    r"^[^<]{0,200}<\s*(?:!doctype\s+html\b|html\s*[>\s])", re.IGNORECASE
)
# Historical diagnostic threshold retained for compatibility with callers and
# tests. Pages are now parsed in full; any durable cause cap is applied only
# after credential redaction by ``bounded_redacted_text``.
MOST_TO_READ = 20_000
# What a page says in a tag that is worth nothing to a person.
_NOT_WORTH_READING = frozenset({"viewport", "generator", "referrer", "theme-color"})


class _ReadingAPage(HTMLParser):
    """Every word a page says, in order, with the markup left behind.

    Four goes at doing this with patterns each got a real page wrong in a new
    way: an apostrophe inside a quoted value ended the value early, a `>` inside
    one ended the tag early, and the word `content` inside somebody else's value
    was read as the message. All of that is what a parser is for, and there has
    been one in the standard library the whole time.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.words: list[str] = []
        # Words held back because they might be a stylesheet or a script. They
        # are only thrown away once that really closes: a page can show the
        # word "<script>" as text - a gateway echoing back what was sent, for
        # one - and then nothing closes it, and everything after it is words
        # somebody needs. Dropping them left a page saying half of what it
        # said, with no sign of the rest.
        self._might_not_be_words: list[str] = []
        self._inside = ""
        # What the parser never handed over, for the caller to read again.
        self.left_over = ""

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style"):
            if not self._inside:
                self._inside = tag
            return
        if tag != "meta":
            return
        held = {name: (value or "") for name, value in attrs}
        if "charset" in held or held.get("name", "").lower() in _NOT_WORTH_READING:
            return
        # A page that says why it is down often says it here and nowhere else.
        said = held.get("content", "").strip()
        if said:
            self._where_words_go().append(said)

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag not in ("script", "style"):
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if self._inside and tag == self._inside:
            # It really did close, so it really was a script or a stylesheet.
            self._inside = ""
            self._might_not_be_words.clear()

    def handle_data(self, data: str) -> None:
        if data.strip():
            self._where_words_go().append(data.strip())

    def _where_words_go(self) -> list[str]:
        return self._might_not_be_words if self._inside else self.words

    def close(self) -> None:
        # What the parser has not taken yet, kept before closing takes it away.
        # On some versions of Python an element that never closes swallows the
        # rest of the page and hands none of it over; the rest is still sitting
        # here, and it is words somebody needs.
        waiting = getattr(self, "rawdata", "") or ""
        super().close()
        if self._inside:
            # Nothing ever closed it, so it was never one. Whatever was held
            # back is words, and they carry on from where the rest left off.
            self.words.extend(self._might_not_be_words)
            if not self._might_not_be_words:
                self.left_over = waiting
            self._might_not_be_words.clear()
            self._inside = ""


def _without_markup(said: str) -> str:
    """A whole web page as one line, or the words exactly as they came.

    Three goes at "which parts of this are markup?" all went the same way.
    Ordinary error text is full of angle brackets - "expected List<Item>",
    "bash: <stdin>:", "git diff <head>..<branch>", "expected </div> after
    <div>" - and cutting them out hands somebody a sentence that reads
    perfectly and has had the useful part taken out of it, with nothing to say
    so. Lifting out a heading was worse again: "Message: Unsupported method" is
    the sentence worth reading on an error page, and the heading above it says
    "Error response".

    So there is one rule now, and it is about the whole thing rather than the
    parts. If the words begin a web page, every word in that page is kept, in
    order, with the tags and the stylesheets taken out. If they do not, they
    are handed back exactly as they came, tags and all. Untidy beats untrue.
    """

    # Whether this is a page at all is asked first, and nothing is done to
    # words that are not one. Asked the other way round, a long message with a
    # single stray `<` in it - "queue depth < 5 required", and then a thousand
    # lines of detail - had everything after that mark thrown away, on a rule
    # about half tags that had no business being applied to it.
    if not _OPENS_A_DOCUMENT.match(said):
        return said
    words = _the_words_in(said)
    if words is None:
        return said
    plain = re.sub(r"\s+", " ", " ".join(words)).strip()
    return plain or said


# How many times the rest of a page is picked up again after an element that
# never closed. Two is one more than any real page needs.
_HOW_MANY_PICK_UPS = 2


def _the_words_in(said: str, pick_ups: int = 0) -> list[str] | None:
    """Every word a page says, or nothing if it cannot be read at all."""

    reading = _ReadingAPage()
    try:
        reading.feed(said)
        reading.close()
    except Exception:  # noqa: BLE001 - a page it cannot read is words as they came
        return None
    words = list(reading.words)
    if reading.left_over and pick_ups < _HOW_MANY_PICK_UPS:
        # The rest of the page, read again from outside the element that never
        # closed. Without this it is simply gone, and the page reads as if it
        # said half of what it said.
        more = _the_words_in(reading.left_over, pick_ups + 1)
        if more:
            words.extend(more)
    return words
