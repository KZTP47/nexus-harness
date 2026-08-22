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

What it is not, on purpose:

  - Not an agent. It cannot read your files, run anything, or change anything.
    It is talked to and it answers. Anything that changes your project goes
    through the run, where there is a record of it.
  - Not a place for credentials. Everything typed in and everything said back
    has credentials taken out of it before it is written down, the same as
    every other thing the harness keeps.
  - Not unbounded. A message is a message, a conversation is the last few
    dozen turns, and one answer has a time limit.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .config import LoadedConfig
from .models import HarnessError, ProviderRequest
from .providers import ProviderRegistry, create_provider
from .redaction import CredentialRedactor
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
# One message is a message. Anything longer belongs in the project, with the
# message pointing at it.
MOST_LETTERS = 6000
# How much of a conversation is kept and sent back. Enough to hold a thread of
# thought; few enough that the last turn does not cost the price of all of them.
MOST_KEPT = 40
# What one answer may be, and how long it may take. A signed-in tool starting
# up for the first time is slow once and quick afterwards.
LONGEST_ANSWER = 20_000
LONGEST_WAIT_SECONDS = 180.0
# How many can be asked the same thing at once.
MOST_AT_ONCE = 6
# The name the default route is filed under, since it has no name of its own.
THE_USUAL_ONE = "the-usual-one"
# What each one is told about itself. Short on purpose: this is a conversation,
# not a job, and an assistant told it is running a job starts trying to run one.
HOW_TO_ANSWER = (
    "You are talking to somebody working on a software project. Answer what "
    "they ask, briefly and plainly. Say when you do not know. You cannot read "
    "their files or run anything, so do not offer to; if you need to see "
    "something, ask them to paste it."
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "who": self.who,
            "text": self.text,
            "at": self.at,
            "milliseconds": self.milliseconds,
            "model": self.model,
        }


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
        for route, one in held.items():
            if not isinstance(one, dict) or not isinstance(one.get("why"), str):
                continue
            safe_why = _without_personal_account_details(one["why"])
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
                    "why": _cut_at_a_full_stop(
                        _without_personal_account_details(why)
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
                "Press Connect it and enter the project id. No API key is needed."
                if needs_setup else (_claude_subscription_repair() if claude_needs_attention else "")
            ),
            "connection_state": (
                "needs setup" if needs_setup else (
                    "needs attention" if no else "connected"
                )
            ),
            "retryable": not needs_setup,
            "setup_blocked": False,
            "trouble_last_time": (
                f"The last time this was asked something, it would not answer: {no['why']}"
                if no else ""
            ),
            "when_that_was": no["at"] if no else "",
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
    if not where.is_file():
        return []
    try:
        held = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # A conversation is worth keeping and not worth failing over. One that
        # cannot be read is one that starts again.
        return []
    if not isinstance(held, list):
        return []
    kept: list[Said] = []
    for one in held[-MOST_KEPT:]:
        if not isinstance(one, dict):
            continue
        who = str(one.get("who") or "")
        text = str(one.get("text") or "")
        if who not in ("you", "them") or not text:
            continue
        kept.append(Said(
            who=who,
            text=text[:LONGEST_ANSWER],
            at=str(one.get("at") or ""),
            milliseconds=int(one.get("milliseconds") or 0),
            model=str(one.get("model") or ""),
        ))
    return kept


def _keep_it(
    config: LoadedConfig, route: str, turns: list[Said], filed_as: str = ""
) -> None:
    where = where_it_is_kept(config, route, filed_as)
    where.parent.mkdir(parents=True, exist_ok=True)
    written = json.dumps([one.to_dict() for one in turns[-MOST_KEPT:]], indent=2) + "\n"
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


def start_again(config: LoadedConfig, route: str, filed_as: str = "") -> str:
    """Throw the conversation away and start a fresh one."""

    from .safety import take_the_file_away

    with _the_lock_for(_filed_under(filed_as or route)):
        where = where_it_is_kept(config, route, filed_as)
        if where.is_file():
            take_the_file_away(where, missing_ok=True)
    return "That conversation is gone. Say something and a new one starts."


def _check_what_was_typed(text: str) -> str:
    said = str(text or "").strip()
    if not said:
        raise ChatError("Type something first.")
    if len(said) > MOST_LETTERS:
        raise ChatError(
            f"That is longer than {MOST_LETTERS} letters. Keep the message short "
            "and point at the file, rather than pasting all of it."
        )
    if any(ord(letter) < 32 and letter not in "\t\n\r" for letter in said):
        raise ChatError("That message holds a control character.")
    return said


def say(
    config: LoadedConfig, route: str, text: str, filed_as: str = ""
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

    model = str(routed.get("provider.model") or "")
    # From here to the write is one piece of work: read what was said, add to
    # it, write it back. Two of those at once each write what the other did not
    # know about, and a turn disappears with nobody told.
    with _the_lock_for(_filed_under(filed_as or route)):
        return _ask_and_keep(
            config, route, asked, provider, model, redactor, named, filed_as
        )


def _ask_and_keep(
    config, route, asked, provider, model, redactor, named, filed_as=""
) -> dict[str, Any]:
    so_far = read_it(config, route, filed_as)
    messages = [
        {"role": "user" if one.who == "you" else "assistant", "content": one.text}
        for one in so_far
    ]
    messages.append({"role": "user", "content": redactor.text(asked)})
    # Built here rather than passed in, so everything that goes to an assistant
    # is built in the one place.
    request = ProviderRequest(
        system_prefix=HOW_TO_ANSWER,
        dynamic_context="",
        messages=messages,
        model=model,
        temperature=0.3,
        max_output_tokens=2048,
        timeout_seconds=LONGEST_WAIT_SECONDS,
    )
    started = time.monotonic()
    try:
        answered = provider.complete(request)
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
    back = redactor.text(str(getattr(answered, "text", "") or "").strip())
    if not back:
        raise ChatError(f"{named or 'The assistant'} answered with nothing at all.")
    turns = so_far + [
        Said(who="you", text=redactor.text(asked), at=_now()),
        Said(
            who="them",
            text=back[:LONGEST_ANSWER],
            at=_now(),
            milliseconds=int((time.monotonic() - started) * 1000),
            model=model,
        ),
    ]
    _keep_it(config, route, turns, filed_as)
    return {
        "route": named,
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
        return list(pool.map(one_of_them, ready))


# How much of a reason is worth reading. Four hundred letters was enough while a
# reason was one line out of a screen of noise; it is not enough now that the
# tools which will not answer are told to say what they know about themselves as
# well, and cutting that off in the middle wastes the part that says what to do.
LONGEST_REASON = 900


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
                return f"{before} {inside.strip()}".strip()[:LONGEST_REASON]
    return _without_markup(said)[:LONGEST_REASON]


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
# How much of a page is looked at. Whoever reads an error page already trims it
# to well under this; the cap is here so that stays true if one day they do not.
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
    if len(said) > MOST_TO_READ:
        said = said[:MOST_TO_READ]
        # A tag cut in half is read as words and shows up as `< di`. Only the
        # half tag goes, and only when there is something left without it.
        opened, shut = said.rfind("<"), said.rfind(">")
        if opened > shut and opened > 0:
            said = said[:opened]
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
