"""Telling somebody when a run finishes.

The timer runs your checks at two in the morning and leaves the report where you
can find it. That is most of the way there, and it stops one step short: you
still have to go and look. This is the last step - a line in the place you
already watch, the moment something needs you.

Everything here needs a key
---------------------------

This is the one part of the harness that cannot work on its own. Slack, Discord,
Telegram, Teams and email all want something you have to go and get: a webhook
address, a bot token, a mail password. There is no way around that and no
pretending otherwise, so the harness says it plainly everywhere it comes up:
which ways are ready, which are waiting on a key, what the key is called, and
where to get one.

The secret is never written down here
-------------------------------------

What is saved is the *name* of an environment variable - `SLACK_WEBHOOK`, say -
and never what is in it. That is the same way the harness already handles the
key for a model, and it is what makes the file safe to commit.

Anything that decides where a secret goes is held the same way. The mail server
is the clearest case: it is not a secret, but whoever sets it decides where your
mail password is sent, so it is read from a variable on this machine and never
from a file that somebody could change in a pull request. What the file does
hold, besides names, is who gets told and which account it comes from - neither
of which can move a secret anywhere.

What is sent
------------

The short version: what ran, whether it passed, and one line of why not.
Everything is cleaned first, because the thing that failed may have printed a
key on its way out, and a chat room is a very public place for that to land.
"""

from __future__ import annotations

import ipaddress
import itertools
import json
import os
import smtplib
import ssl
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Callable
from urllib.parse import urlsplit

from .config import LoadedConfig
from .models import HarnessError

WHERE_THEY_LIVE = "telling"

# The most that goes out in one message. A chat room is not a log file, and a
# whole failing test run pasted into one is read by nobody.
MOST_LETTERS = 3_000
# How long to wait for the other end. Long enough for a slow morning, short
# enough that a timer's run is not held up by somebody else's outage.
LONGEST_WAIT = 20.0
# And the most that comes back. An answer we do not even read should not be
# able to fill this machine's memory.
MOST_TO_READ = 100_000


class TellingError(HarnessError):
    """Something wrong with a way of telling somebody, or with telling them."""


@dataclass
class Way:
    """One way of telling somebody, as it is written down.

    `secret_in` is the name of an environment variable, never the secret. The
    file this is saved in is meant to be committed, and a secret in a committed
    file is a secret you cannot take back.
    """

    name: str
    kind: str
    secret_in: str = ""
    server_in: str = ""
    to: str = ""
    sent_from: str = ""
    turned_on: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "secret_in": self.secret_in,
            "server_in": self.server_in,
            "to": self.to,
            "sent_from": self.sent_from,
            "turned_on": self.turned_on,
        }


@dataclass
class Kind:
    """One kind of place you can be told, and what it needs from you."""

    kind: str
    label: str
    # What the secret is, in the words that place uses for it.
    secret_is: str
    # A name for the variable that is a sensible default.
    usually_called: str
    where_to_get_one: str
    # Whether it needs a mail server, a "to" (a room or a person), and a
    # "sent_from" (the account the secret belongs to).
    needs_a_server: bool = False
    needs_to: bool = False
    needs_sent_from: bool = False
    # What the variable holding the mail server is usually called.
    server_usually_called: str = ""
    # The secret is part of the address this one is asked at, rather than
    # something sent alongside it. True of Telegram and nothing else here.
    secret_is_in_the_address: bool = False


THE_KINDS: tuple[Kind, ...] = (
    Kind(
        kind="slack",
        label="Slack",
        secret_is="an incoming webhook address",
        usually_called="SLACK_WEBHOOK",
        where_to_get_one=(
            "In Slack: Settings, then Manage apps, then Incoming Webhooks. Add "
            "one for the channel you want. Slack gives you an address; put that "
            "in the variable, not in any file here."
        ),
    ),
    Kind(
        kind="discord",
        label="Discord",
        secret_is="a webhook address",
        usually_called="DISCORD_WEBHOOK",
        where_to_get_one=(
            "In Discord: right-click the channel, Edit Channel, Integrations, "
            "then New Webhook. Copy the address it gives you."
        ),
    ),
    Kind(
        kind="teams",
        label="Microsoft Teams",
        secret_is="a webhook address",
        usually_called="TEAMS_WEBHOOK",
        where_to_get_one=(
            "In Teams: the channel's menu, then Connectors, then Incoming "
            "Webhook. Your organisation may have this turned off, in which case "
            "ask whoever runs it."
        ),
    ),
    Kind(
        kind="telegram",
        label="Telegram",
        secret_is="a bot token",
        usually_called="TELEGRAM_BOT_TOKEN",
        where_to_get_one=(
            "In Telegram, message @BotFather and ask for a new bot. It gives "
            "you a token. Then message your new bot once, so it is allowed to "
            "message you back, and put the chat number in 'to'."
        ),
        needs_to=True,
        secret_is_in_the_address=True,
    ),
    Kind(
        kind="email",
        label="Email",
        secret_is="the password for the mail account sending it",
        usually_called="MAIL_PASSWORD",
        where_to_get_one=(
            "Whoever runs your mail. If it is Gmail or similar, an ordinary "
            "password will not do: make an app password in your account's "
            "security settings."
        ),
        needs_a_server=True,
        needs_to=True,
        needs_sent_from=True,
        server_usually_called="MAIL_SERVER",
    ),
    Kind(
        kind="webhook",
        label="Anywhere else",
        secret_is="the address to post to",
        usually_called="TELL_SOMEBODY_WEBHOOK",
        where_to_get_one=(
            "Whatever you are sending to. It gets a small piece of JSON: what "
            "ran, whether it passed, and one line of why not."
        ),
    ),
)

BY_KIND = {one.kind: one for one in THE_KINDS}


def kind_or_error(kind: str) -> Kind:
    if kind not in BY_KIND:
        raise TellingError(
            f"There is no way of telling somebody called {kind}. There is: "
            + ", ".join(BY_KIND)
        )
    return BY_KIND[kind]


# --------------------------------------------------------------------------
# What is written down, and what is not.
# --------------------------------------------------------------------------


def read_it(said: Any) -> Way:
    if not isinstance(said, dict):
        raise TellingError("A way of telling somebody is written as an object")
    name = str(said.get("name") or "").strip()
    if not name or len(name) > 64:
        raise TellingError("Give it a name, of 64 letters or fewer")
    kind = kind_or_error(str(said.get("kind") or "").strip())
    secret_in = str(said.get("secret_in") or "").strip() or kind.usually_called
    _check_the_variable_name(secret_in)
    server_in = str(said.get("server_in") or "").strip()
    if kind.needs_a_server:
        server_in = server_in or kind.server_usually_called
        _check_the_variable_name(server_in)
    else:
        # Thrown away rather than kept. Left in place, a box the panel had
        # filled in for a different kind stuck to this one, and a Slack room
        # spent the rest of its life complaining about a mail server.
        server_in = ""
    to = str(said.get("to") or "").strip()
    sent_from = str(said.get("sent_from") or "").strip()
    if kind.needs_to and not to:
        raise TellingError(f"{kind.label} needs somebody to send it to, in 'to'")
    if kind.needs_sent_from and not sent_from:
        raise TellingError(
            f"{kind.label} needs the account it is sent from, in 'sent_from'. "
            "That is the account the password belongs to."
        )
    for field, value in (("to", to), ("sent_from", sent_from)):
        if len(value) > 300:
            raise TellingError(f"{field} is longer than 300 letters")
    return Way(
        name=name,
        kind=kind.kind,
        secret_in=secret_in,
        server_in=server_in,
        to=to,
        sent_from=sent_from,
        turned_on=bool(said.get("turned_on", True)),
    )


def _check_the_variable_name(name: str) -> None:
    """It has to look like a variable name, and not like a secret.

    Somebody pastes the webhook address in here sooner or later, because it is
    the box next to the word "webhook". That address then goes into a file that
    is meant to be committed, and the whole point of holding only the name is
    lost. So anything that is obviously not a variable name is refused, and the
    refusal says what went wrong rather than just "no".
    """

    if not name:
        raise TellingError("Say which environment variable holds the secret")
    if len(name) > 128:
        raise TellingError("That is too long to be the name of a variable")
    if not all(one.isalnum() or one == "_" for one in name):
        # One message, not two. Split in two, a pasted bot token fell into the
        # short one - "letters, numbers and underscores" - which is true and
        # tells somebody nothing about the mistake they actually made.
        raise TellingError(
            "That looks like the secret itself, not the name of the variable "
            "holding it. The name of a variable is letters, numbers and "
            "underscores. Put the secret in one - say SLACK_WEBHOOK - and "
            "write that name here. What is saved here is meant to be safe to "
            "commit, so it never holds the secret."
        )


def _the_mail_server(way: Way) -> tuple[str, int]:
    """Where to send mail through, read from this machine and not from a file.

    Kept in the file, this was the one thing here that was not a secret and
    still decided where a secret went: change the committed file to point at a
    server somebody else runs, and the next run hands them the real mail
    password. It is read from an environment variable for the same reason the
    password is - so that changing a file somebody sends you cannot move it.
    """

    said = os.environ.get(way.server_in, "").strip()
    if not said:
        raise TellingError(
            f"Nothing is in {way.server_in} on this machine, so there is "
            "nowhere to send mail through. Put your mail server there, written "
            "as host:port. It is read from this machine and never from a file, "
            "because whatever holds it decides where your password goes."
        )
    host, _, port = said.partition(":")
    if not host.strip():
        raise TellingError(f"{way.server_in} does not name a mail server")
    if port and (not port.isdigit() or not 1 <= int(port) <= 65535):
        raise TellingError(f"The part of {way.server_in} after the colon is a port")
    return host.strip(), int(port or 587)


def folder(config: LoadedConfig):
    from .safety import confined_path

    return confined_path(
        config.project_root, f".harness/{WHERE_THEY_LIVE}",
        allow_missing=True, allow_control=True,
    )


def _where_it_lives(config: LoadedConfig, name: str):
    from .safety import confined_path

    safe = str(name).strip().replace(" ", "-").lower()
    if not safe or not all(one.isalnum() or one in "-_" for one in safe):
        raise TellingError("A name is letters, numbers, spaces, dashes and underscores")
    return confined_path(
        config.project_root, f".harness/{WHERE_THEY_LIVE}/{safe}.json",
        allow_missing=True, allow_control=True,
    )


def every_one(config: LoadedConfig) -> list[Way]:
    where = folder(config)
    if not where.is_dir():
        return []
    found = []
    for path in sorted(where.glob("*.json")):
        try:
            found.append(read_it(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError, TellingError):
            # One nobody can read is one, not the end of the list.
            continue
    return found


# The most that can be set up here. Every one of these is read whenever
# anything anywhere is cleaned, and a folder somebody can grow without limit is
# a cost on every other part of the harness.
MOST_WAYS = 50


def save(config: LoadedConfig, said: Any) -> Way:
    from .safety import put_this_file_in_place

    way = read_it(said)
    already = {one.name for one in every_one(config)}
    if way.name not in already and len(already) >= MOST_WAYS:
        raise TellingError(
            f"There are already {MOST_WAYS} of these set up, which is more than "
            "anybody reads. Take one off before adding another."
        )
    path = _where_it_lives(config, way.name)
    if path.is_file():
        try:
            already = read_it(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TellingError):
            already = None
        if already is not None and already.name != way.name:
            raise TellingError(
                f"There is already one called {already.name}, kept in "
                f"{path.name}. Pick a name that differs by more than capitals, "
                "spaces and dashes."
            )
    put_this_file_in_place(path, json.dumps(way.to_dict(), indent=2) + "\n")
    return way


def remove(config: LoadedConfig, name: str) -> str:
    from .safety import take_the_file_away

    path = _where_it_lives(config, name)
    if not path.is_file():
        raise TellingError(f"There is nothing set up called {name}.")
    take_the_file_away(path)
    return f"{name} was taken off."


# --------------------------------------------------------------------------
# Whether it can actually be used.
# --------------------------------------------------------------------------


def is_the_key_there(way: Way) -> bool:
    return bool(os.environ.get(way.secret_in, "").strip())


def why_it_cannot_be_used(way: Way) -> str:
    """Why this one will not work right now, in plain words, or nothing."""

    kind = BY_KIND[way.kind]
    if way.server_in and not os.environ.get(way.server_in, "").strip():
        return (
            f"{way.name} needs the mail server it sends through, and nothing "
            f"is in {way.server_in} on this machine. Write it as host:port. It "
            "is read from this machine and never from a file, because whatever "
            "holds it decides where your password goes."
        )
    if not is_the_key_there(way):
        return (
            f"{way.name} needs {kind.secret_is}, and nothing is in "
            f"{way.secret_in} on this machine. This is the one part of the "
            "harness that cannot work without something you go and get. "
            f"{kind.where_to_get_one}"
        )
    return ""


def how_it_stands(config: LoadedConfig) -> list[dict[str, Any]]:
    """Every way set up here, and whether it can be used at this moment."""

    return [
        dict(
            one.to_dict(),
            label=BY_KIND[one.kind].label,
            ready=is_the_key_there(one),
            why_not=why_it_cannot_be_used(one),
        )
        for one in every_one(config)
    ]


# --------------------------------------------------------------------------
# Saying it.
# --------------------------------------------------------------------------


@dataclass
class Said:
    name: str
    sent: bool
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "sent": self.sent, "note": self.note}


def tell_them(
    config: LoadedConfig,
    way: Way,
    heading: str,
    body: str,
    *,
    passed: bool = True,
    post: Callable[..., str] | None = None,
) -> Said:
    """Say one thing, one way.

    Everything is cleaned first. The thing that failed may have printed a key on
    its way out, and a chat room is a very public place for that to land.
    """

    from .redaction import CredentialRedactor

    if not way.turned_on:
        return Said(way.name, False, f"{way.name} is turned off.")
    why_not = why_it_cannot_be_used(way)
    if why_not:
        return Said(way.name, False, why_not)
    # Handed the names of every key set up here, rather than left to find them.
    # Found by looking, the looking can quietly find none - and then the very
    # key this is about goes out in plain text.
    clean = CredentialRedactor(config, also_hide=_every_key_name(config, way))
    heading = clean.text(str(heading))[:200]
    body = clean.text(str(body))[:MOST_LETTERS]
    secret = os.environ[way.secret_in].strip()
    post = post or _post_it

    def send_it() -> None:
        if way.kind == "email":
            _send_an_email(way, secret, heading, body)
        else:
            post(*_what_to_post(way, secret, heading, body, passed))

    try:
        _within(LONGEST_WAIT + A_LITTLE_LONGER, send_it)
    except TooManyStuck:
        return Said(
            way.name, False,
            f"{STUCK_ONES_ALLOWED} messages are already stuck waiting for an "
            "answer, so this one was not started. Something you are telling is "
            "not there. Check the addresses in: harness tell list",
        )
    except TookTooLong:
        return Said(
            way.name, False,
            f"{way.name} did not answer within "
            f"{int(LONGEST_WAIT + A_LITTLE_LONGER)} seconds, "
            "so the harness stopped waiting and carried on.",
        )
    except TellingError:
        raise
    except (OSError, urllib.error.URLError, smtplib.SMTPException, ssl.SSLError) as exc:
        # Never the address and never the secret: this line is written into a
        # run's own record, and for Telegram the secret is part of the address.
        return Said(
            way.name, False,
            f"{way.name} could not be reached ({type(exc).__name__}). The "
            "harness is fine; something between here and there is not.",
        )
    return Said(way.name, True, f"Told {way.name}.")


def _every_key_name(config: LoadedConfig, way: Way) -> set[str]:
    """The variables holding a key for any of this, starting with this one's.

    This one's names first and always: whatever else fails, the key being used
    right now is hidden. The rest are worth having because one failing report
    can quote a different one's key.
    """

    names = {one for one in (way.secret_in, way.server_in) if one}
    try:
        for other in every_one(config):
            names |= {one for one in (other.secret_in, other.server_in) if one}
    except HarnessError:
        pass
    return names


# How many sends may be stuck at once before this stops starting more. A thread
# waiting on a name that never resolves cannot be killed - Python has no safe
# way - so it is left, and left ones are counted. One bad address hit every
# night is one thread a night, for ever, and nothing was watching that.
STUCK_ONES_ALLOWED = 8
# A little longer than the socket is given, so a send that is merely slow gets
# its own answer rather than being cut off by the waiting outside it.
A_LITTLE_LONGER = 5.0
_stuck = itertools.count()
_how_many_stuck = 0
_stuck_lock = threading.Lock()


class TookTooLong(Exception):
    """The far end never answered, and we stopped waiting."""


class TooManyStuck(Exception):
    """Too many sends are already stuck to be worth starting another."""


def _within(seconds: float, do_it: Callable[[], None]) -> None:
    """Do it, and give up waiting after this long, whatever it is stuck on.

    The timeout on a socket does not cover looking a name up, because the name
    is looked up before there is a socket to put a timeout on. A hostname
    pointing at a nameserver that never answers hung the whole nightly run -
    not just the message, the run - and the lock it held meant every firing
    after it stood aside saying the last one was still going. Nobody would have
    found that for a week.

    The thread is left behind rather than killed, because a thread cannot be
    killed safely. It is holding a socket and nothing else, and it goes when the
    command does.
    """

    global _how_many_stuck

    with _stuck_lock:
        if _how_many_stuck >= STUCK_ONES_ALLOWED:
            raise TooManyStuck()
    went_wrong: list[BaseException] = []

    def run_it() -> None:
        global _how_many_stuck
        try:
            do_it()
        except BaseException as exc:  # noqa: BLE001 - handed back to the caller
            went_wrong.append(exc)
        finally:
            # Counted down here rather than by the waiter, so one that comes
            # back late still stops being counted as stuck.
            with _stuck_lock:
                if getattr(run_it, "counted", False):
                    _how_many_stuck -= 1
                    run_it.counted = False  # type: ignore[attr-defined]

    going = threading.Thread(target=run_it, daemon=True)
    going.start()
    going.join(timeout=seconds)
    if going.is_alive():
        with _stuck_lock:
            run_it.counted = True  # type: ignore[attr-defined]
            _how_many_stuck += 1
        raise TookTooLong()
    if went_wrong:
        raise went_wrong[0]


def _what_to_post(
    way: Way, secret: str, heading: str, body: str, passed: bool
) -> tuple[str, dict[str, Any]]:
    """The address to post to, and what to post, for one kind of place."""

    if way.kind == "slack":
        return _an_address(secret), {"text": f"{heading}\n{body}"}
    if way.kind == "discord":
        return _an_address(secret), {"content": f"**{heading}**\n{body}"[:1900]}
    if way.kind == "teams":
        return _an_address(secret), {"title": heading, "text": body}
    if way.kind == "telegram":
        # The one place where the secret is part of the address. That is how
        # Telegram is built, and it is why nothing here ever writes an address
        # into a message, a log or a run's record.
        return (
            f"https://api.telegram.org/bot{secret}/sendMessage",
            {"chat_id": way.to, "text": f"{heading}\n{body}"},
        )
    return _an_address(secret), {
        "heading": heading,
        "body": body,
        "passed": passed,
        "from": "harness",
    }


def _an_address(said: str) -> str:
    """A web address we are willing to post to.

    The same rule the rest of the harness already uses for talking to anything:
    https, or http only to this very machine. A webhook address that arrives
    over plain http from somewhere else is somebody reading your run reports.
    """

    parsed = urlsplit(said)
    if parsed.username or parsed.password or not parsed.hostname:
        raise TellingError(
            "That address has a name and password in it, which this will not "
            "send to. Use an ordinary webhook address."
        )
    here = parsed.hostname.lower() == "localhost"
    if not here:
        try:
            here = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            here = False
    if parsed.scheme != "https" and not (parsed.scheme == "http" and here):
        raise TellingError(
            "That address is not https. Anything but this machine has to be "
            "https, or the report can be read on the way."
        )
    return said


def _post_it(address: str, body: dict[str, Any]) -> str:
    said = json.dumps(body).encode("utf-8")
    ask = urllib.request.Request(
        address,
        data=said,
        headers={"Content-Type": "application/json", "User-Agent": "harness"},
        method="POST",
    )
    # No redirects followed. A webhook that answers with "go over there" is
    # either broken or somebody moving your report somewhere you did not agree
    # to, and neither is worth guessing about.
    opener = urllib.request.build_opener(_NoGoingElsewhere)
    with opener.open(ask, timeout=LONGEST_WAIT) as answer:
        return answer.read(MOST_TO_READ).decode("utf-8", errors="replace")


class _NoGoingElsewhere(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise TellingError(
            "That address answered by pointing somewhere else, which this will "
            "not follow. Use the address it really lives at."
        )


def _send_an_email(way: Way, secret: str, heading: str, body: str) -> None:
    host, port = _the_mail_server(way)
    written = EmailMessage()
    # A heading is one line. A newline in one is how somebody adds a header of
    # their own to your message, and the report it comes from is not always
    # written by somebody you know.
    written["Subject"] = _one_line(heading)
    written["From"] = _one_line(way.sent_from)
    written["To"] = _one_line(way.to)
    written.set_content(body)
    with smtplib.SMTP(host, port, timeout=LONGEST_WAIT) as post:
        # No password without this. A server that will not do it is a server
        # that would take the password in the clear, and that is worse than not
        # sending the message.
        post.starttls(context=ssl.create_default_context())
        post.login(way.sent_from, secret)
        post.send_message(written)


def _one_line(said: str) -> str:
    return " ".join(str(said).split())


def tell_everybody(
    config: LoadedConfig,
    heading: str,
    body: str,
    *,
    passed: bool = True,
    only_when_it_fails: bool = False,
    post: Callable[..., str] | None = None,
) -> list[Said]:
    """Say one thing every way that is set up and can be used."""

    if only_when_it_fails and passed:
        return []
    return [
        tell_them(config, one, heading, body, passed=passed, post=post)
        for one in every_one(config)
        if one.turned_on
    ]
