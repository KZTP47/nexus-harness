"""Microsoft 365 Copilot, reached over the web because there is nothing to run.

Every other assistant the harness drives is a program on the machine. This one
is not, and no amount of installing will make it one: Microsoft 365 Copilot has
no command line and never has had. What it has, since the middle of 2026, is an
ordinary web address you can send a question to, and that is what this uses.

    POST https://graph.microsoft.com/beta/copilot/conversations
    POST https://graph.microsoft.com/beta/copilot/conversations/{id}/chat

The part that matters for anybody with no API keys and no way of getting any:
this cannot be used with a key. Microsoft does not allow it. The only way in is
a person signing in, which is what the harness does - once, with a code you
paste into a browser, the same as signing into any other work tool. After that
it keeps the sign-in and renews it by itself.

Three things have to be true before a single question gets through, and all
three are somebody else's decision rather than yours:

  - The person signing in has a Microsoft 365 Copilot add-on seat. The free
    Copilot Chat that comes with Microsoft 365 is a different thing and is not
    allowed here.
  - An app is registered in your organisation for the harness to sign in as,
    and its number is written into the settings. It takes a few minutes and it
    is free.
  - Somebody who administers the organisation has approved the seven
    permissions below, once, for that app.

None of that can be worked around from here, so the harness says which of the
three is missing rather than a shrug, and says it before somebody types.

It is a preview, on Microsoft's /beta address, and their own note says /beta is
subject to change. It can stop working one morning through nobody's fault. When
it does, this says so plainly instead of blaming the sign-in.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ..models import HarnessError, ProviderRequest, ProviderResponse
from .. import cancellation
from .base import Provider, _interrupt_http_response
from .subscription_cli import UNPRICED

# Where Microsoft is asked for a sign-in and where the questions go.
WHERE_SIGN_IN_HAPPENS = "https://login.microsoftonline.com"
WHERE_THE_QUESTIONS_GO = "https://graph.microsoft.com/beta/copilot"
# Every organisation unless somebody names theirs. "organizations" means work
# and school accounts and not personal ones, which this cannot use anyway.
EVERY_ORGANISATION = "organizations"

# All seven, and it really is all seven: Microsoft's own note says the Chat API
# needs every one of them and refuses without. offline_access is what lets the
# sign-in be renewed instead of asked for again every hour.
WHAT_IT_ASKS_TO_BE_ALLOWED = (
    "https://graph.microsoft.com/Sites.Read.All",
    "https://graph.microsoft.com/Mail.Read",
    "https://graph.microsoft.com/People.Read.All",
    "https://graph.microsoft.com/OnlineMeetingTranscript.Read.All",
    "https://graph.microsoft.com/Chat.Read",
    "https://graph.microsoft.com/ChannelMessage.Read.All",
    "https://graph.microsoft.com/ExternalItem.Read.All",
    "offline_access",
)

# A sign-in is renewed a little before it runs out, rather than after somebody
# has already been told no.
RENEW_IT_THIS_EARLY = 300
# How long to wait for one answer. These take their time - the question goes
# through a search of everything the person can see before anything is written.
LONGEST_WAIT = 180.0
# How long to keep offering the code before giving up on somebody pasting it.
LONGEST_SIGN_IN = 900
# How much longer to wait between asks when Microsoft says to slow down. Five
# seconds is what the rule for this kind of sign-in asks for, and asking again
# at the old pace gets you stopped altogether.
SLOW_DOWN_BY = 5

# What to tell somebody, for each of the three things that can be missing.
HOW_TO_REGISTER_AN_APP = (
    "Nobody has said which registered app the harness signs in as. Somebody "
    "with an Azure portal makes one in a few minutes and it costs nothing: "
    "Microsoft Entra ID, App registrations, New registration, give it a name, "
    "choose Accounts in this organizational directory only, and under "
    "Authentication turn on Allow public client flows. Then put the "
    "Application (client) ID into this route's settings as microsoft_app."
)
WHAT_THE_ADMINISTRATOR_APPROVES = (
    "Microsoft 365 Copilot needs all seven of these permissions before it will "
    "answer anything, and most of them an administrator approves once for the "
    "app: Sites.Read.All, Mail.Read, People.Read.All, "
    "OnlineMeetingTranscript.Read.All, Chat.Read, ChannelMessage.Read.All, "
    "ExternalItem.Read.All. They are all read-only."
)
ABOUT_THE_SEAT = (
    "This needs a Microsoft 365 Copilot add-on seat. The Copilot Chat that "
    "comes with Microsoft 365 is a different product and Microsoft does not "
    "allow it here."
)


class SignInNeeded(HarnessError):
    """Nobody is signed in to Microsoft yet, or the sign-in has run out."""


def _where_the_sign_in_is() -> Path:
    """The file the Microsoft sign-in is kept in.

    Kept with the person and not with the project. A project folder is a thing
    people copy onto shared drives and push to GitHub, and this file is the one
    thing in the harness that is worth stealing.
    """

    base = os.environ.get("NEXUS_HARNESS_HOME")
    if not base:
        base = os.environ.get("LOCALAPPDATA") or ""
        base = str(Path(base) / "NexusHarness") if base else str(Path.home() / ".nexus-harness")
    return Path(base) / "microsoft-sign-in.json"


def read_the_sign_in() -> dict[str, Any]:
    """What is known about the Microsoft sign-in, or nothing."""

    try:
        held = json.loads(_where_the_sign_in_is().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return held if isinstance(held, dict) else {}


def keep_the_sign_in(held: dict[str, Any]) -> None:
    """Write the sign-in down, as much out of anybody else's reach as this can.

    On Windows that means asking for the file to be readable by its owner and
    nobody else, which is a request rather than a promise - a machine somebody
    else administers can still be read by them. It is still worth asking.
    """

    where = _where_the_sign_in_is()
    where.parent.mkdir(parents=True, exist_ok=True)
    beside = where.with_name(f"{where.name}.{os.getpid()}.part")
    # Made with nobody else allowed in, rather than written first and locked
    # down after. Written first, the token sits there readable by anybody on the
    # machine for as long as it takes to get to the next line - which is not
    # long, and is long enough.
    handle = os.open(beside, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as writing:
            writing.write(json.dumps(held, indent=2) + "\n")
    except BaseException:
        beside.unlink(missing_ok=True)
        raise
    _keep_it_to_yourself(beside)
    os.replace(beside, where)


def _keep_it_to_yourself(where: Path) -> None:
    """Ask Windows to let nobody but this account near the file.

    The mode a file is made with is the whole story on Linux and macOS and means
    almost nothing on Windows, where a file takes whatever the folder it lands in
    hands down - and that can be a good deal more than one person. This is the
    only thing here worth stealing, so it is worth asking.

    Asked, not insisted on. Whoever administers the machine can read it whatever
    this does, and if the asking fails the sign-in still has to be written -
    refusing to sign somebody in because their permissions could not be narrowed
    would be worse than the risk, and the folder it sits in is already their own.
    """

    if os.name != "nt":
        return
    who = os.environ.get("USERNAME") or ""
    if not who:
        return
    try:
        subprocess.run(
            ["icacls", str(where), "/inheritance:r", "/grant:r", f"{who}:F"],
            capture_output=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return


def forget_the_sign_in() -> None:
    try:
        _where_the_sign_in_is().unlink()
    except OSError:
        pass


def _ask_microsoft(url: str, form: dict[str, str], timeout: float = 30.0) -> dict[str, Any]:
    """Put a form to Microsoft and read back what it says.

    Sign-in is asked for as a form rather than as JSON, which is the one place
    in the harness that is true, so it does not go through the usual poster.
    A refusal here is expected rather than exceptional - "nobody has pasted the
    code yet" arrives as an error - so the body is read and handed back either
    way, and deciding what it means is left to whoever asked.
    """

    said = urllib.parse.urlencode(form).encode("utf-8")
    asked = urllib.request.Request(
        url,
        data=said,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(asked, timeout=timeout) as answered:  # noqa: S310
            raw = answered.read(1_000_000)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(1_000_000)
        finally:
            exc.close()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HarnessError(f"Microsoft could not be reached: {exc}") from exc
    try:
        held = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HarnessError("Microsoft answered with something that is not JSON") from exc
    if not isinstance(held, dict):
        raise HarnessError("Microsoft answered with something that is not an answer")
    return held


def start_signing_in(app: str, organisation: str = "") -> dict[str, Any]:
    """Ask Microsoft for a code somebody can paste into a browser.

    This is the way in for a tool with no window of its own and no key. The
    person is shown a short code and an address, types the code there, and this
    end waits. Nothing secret passes through the harness at any point.
    """

    if not app:
        raise HarnessError(HOW_TO_REGISTER_AN_APP)
    said = _ask_microsoft(
        f"{WHERE_SIGN_IN_HAPPENS}/{organisation or EVERY_ORGANISATION}/oauth2/v2.0/devicecode",
        {"client_id": app, "scope": " ".join(WHAT_IT_ASKS_TO_BE_ALLOWED)},
    )
    if said.get("error"):
        raise HarnessError(_what_microsoft_meant(said, app))
    for wanted in ("device_code", "user_code", "verification_uri"):
        if not said.get(wanted):
            raise HarnessError("Microsoft did not send back a code to sign in with")
    return {
        "code": str(said["user_code"]),
        "where": str(said["verification_uri"]),
        "waiting_on": str(said["device_code"]),
        "ask_again_after": max(1, int(said.get("interval") or 5)),
        "gives_up_at": time.time() + min(int(said.get("expires_in") or LONGEST_SIGN_IN), LONGEST_SIGN_IN),
    }


def how_the_sign_in_is_going(app: str, waiting_on: str, organisation: str = "") -> dict[str, Any]:
    """Ask once whether the code has been pasted yet.

    Asked once and not in a loop, so the panel stays answering while somebody
    is off in a browser and a person who changes their mind is not waited on
    for a quarter of an hour.
    """

    said = _ask_microsoft(
        f"{WHERE_SIGN_IN_HAPPENS}/{organisation or EVERY_ORGANISATION}/oauth2/v2.0/token",
        {
            "client_id": app,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": waiting_on,
        },
    )
    trouble = str(said.get("error") or "")
    if trouble in ("authorization_pending", "slow_down"):
        # Being asked to slow down and slowing down are two different things.
        # Asked again at the same pace, Microsoft stops answering at all, and
        # somebody watches a code they already pasted never be noticed.
        return {
            "done": False, "waiting": True, "why": "",
            "wait_longer_by": SLOW_DOWN_BY if trouble == "slow_down" else 0,
        }
    if trouble:
        return {"done": False, "waiting": False, "why": _what_microsoft_meant(said, app)}
    keep_the_sign_in(_a_sign_in_from(said, app, organisation))
    return {"done": True, "waiting": False, "why": ""}


def _a_sign_in_from(said: dict[str, Any], app: str, organisation: str) -> dict[str, Any]:
    return {
        "token": str(said.get("access_token") or ""),
        "renew_with": str(said.get("refresh_token") or ""),
        "runs_out_at": time.time() + float(said.get("expires_in") or 3600),
        "app": app,
        "organisation": organisation or EVERY_ORGANISATION,
        "signed_in_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _what_microsoft_meant(said: dict[str, Any], app: str) -> str:
    """One of Microsoft's refusals, in words, with what to do about it.

    Their own description is kept on the end. It is written for somebody who
    already knows the vocabulary, so it goes second, but leaving it out
    entirely means the one person who could look it up cannot.
    """

    trouble = str(said.get("error") or "")
    theirs = " ".join(str(said.get("error_description") or "").split())[:400]
    ours = {
        "expired_token": "Nobody pasted the code in time. Start again and it will make a new one.",
        "authorization_declined": "The sign-in was turned down in the browser.",
        "bad_verification_code": "That code is not one Microsoft is waiting for. Start again.",
        "invalid_client": (
            f"Microsoft does not know an app numbered {app} in this organisation, or it is "
            "not set up to sign people in this way - that is the Allow public client flows "
            f"switch. {HOW_TO_REGISTER_AN_APP}"
        ),
        "invalid_grant": (
            "Microsoft would not accept the sign-in. If it worked before, it has been "
            "signed out somewhere else, and signing in again fixes it."
        ),
        "invalid_scope": (
            f"The app is not allowed to ask for what this needs. {WHAT_THE_ADMINISTRATOR_APPROVES}"
        ),
        "consent_required": WHAT_THE_ADMINISTRATOR_APPROVES,
        "interaction_required": WHAT_THE_ADMINISTRATOR_APPROVES,
        "unauthorized_client": (
            f"That app is not allowed to sign people in this way. {HOW_TO_REGISTER_AN_APP}"
        ),
    }.get(trouble, "")
    if not ours:
        ours = f"Microsoft would not sign this in ({trouble or 'no reason given'})."
    return f"{ours} Microsoft's own words: {theirs}" if theirs else ours


def a_token_to_use(app: str, organisation: str = "") -> str:
    """A sign-in good for right now, renewed if it is about to run out.

    Renewed a few minutes early rather than at the moment it stops working: a
    question can take three minutes to answer, and one that starts on a valid
    sign-in and finishes on a dead one fails halfway through for a reason
    nobody could guess at.
    """

    held = read_the_sign_in()
    if not held.get("token"):
        raise SignInNeeded(
            "Nobody is signed in to Microsoft 365 yet. Open Your team and press "
            "Sign in to Microsoft."
        )
    # An empty one counts as a different one. Read as "no app, nothing to
    # check", a sign-in from before anybody wrote an app down gets used against
    # whatever is written down now, and Microsoft refuses it in a way nothing
    # here could explain.
    if app and str(held.get("app") or "") != app:
        raise SignInNeeded(
            "The Microsoft sign-in on this machine is for a different registered app "
            "than the one in the settings. Sign in again."
        )
    if float(held.get("runs_out_at") or 0) - RENEW_IT_THIS_EARLY > time.time():
        return str(held["token"])
    if not held.get("renew_with"):
        raise SignInNeeded("The Microsoft sign-in has run out. Sign in again.")
    said = _ask_microsoft(
        f"{WHERE_SIGN_IN_HAPPENS}/{held.get('organisation') or organisation or EVERY_ORGANISATION}"
        "/oauth2/v2.0/token",
        {
            "client_id": str(held.get("app") or app),
            "grant_type": "refresh_token",
            "refresh_token": str(held["renew_with"]),
            "scope": " ".join(WHAT_IT_ASKS_TO_BE_ALLOWED),
        },
    )
    if said.get("error") or not said.get("access_token"):
        forget_the_sign_in()
        raise SignInNeeded(
            "The Microsoft sign-in could not be renewed, so it has been forgotten. "
            f"Sign in again. {_what_microsoft_meant(said, str(held.get('app') or app))}"
        )
    renewed = _a_sign_in_from(said, str(held.get("app") or app), str(held.get("organisation") or ""))
    # Microsoft does not always send a new one back. Keeping the old one is the
    # difference between staying signed in and being asked again every hour.
    renewed["renew_with"] = renewed["renew_with"] or str(held["renew_with"])
    renewed["signed_in_at"] = str(held.get("signed_in_at") or renewed["signed_in_at"])
    keep_the_sign_in(renewed)
    return renewed["token"]


# Copilot writes names and files inside tags of its own - <Person>Jo</Person> -
# and marks where it got something with [^1^]. Useful to a page that draws them
# and noise to anybody reading the words, which is all this does with them.
_ITS_OWN_TAGS = re.compile(r"</?(?:Person|File|Event|Email|Meeting|Site|Chat)>")
_ITS_OWN_MARKS = re.compile(r"\[\^\d+\^\]")


def _just_the_words(said: str) -> str:
    held = _ITS_OWN_MARKS.sub("", _ITS_OWN_TAGS.sub("", said))
    return "\n".join(line.rstrip() for line in held.splitlines()).strip()


class M365CopilotProvider(Provider):
    """Microsoft 365 Copilot, asked over the web."""

    def __init__(self, config) -> None:
        super().__init__(config)
        self.app = str(self.settings.get("microsoft_app") or "").strip()
        self.organisation = str(self.settings.get("microsoft_organisation") or "").strip()
        self.where_they_are = str(self.settings.get("time_zone") or "").strip() or "UTC"

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        if any(
            isinstance(one, dict) and str(one.get("type") or "").startswith("image/")
            for one in request.attachments
        ):
            raise HarnessError(
                "Microsoft 365 Copilot chat has no declared screenshot-input contract in Nexus. "
                "Use a vision-capable Claude, Codex, Gemini, OpenAI, Anthropic, or Ollama route."
            )
        started = time.monotonic()
        token = a_token_to_use(self.app, self.organisation)
        held = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        started_talking = self._say(
            f"{WHERE_THE_QUESTIONS_GO}/conversations", {}, held, request.timeout_seconds)
        which = str(started_talking.get("id") or "")
        if not which:
            raise HarnessError("Microsoft 365 Copilot did not open a conversation")
        answered = self._say(
            # Escaped whole, slashes and all. Left to itself this escapes
            # everything except a slash, and a name with slashes in it walks
            # straight out of the part of the address it was meant to fill -
            # carrying the sign-in with it, to whatever it lands on instead.
            f"{WHERE_THE_QUESTIONS_GO}/conversations"
            f"/{urllib.parse.quote(which, safe='')}/chat",
            self._the_question(request),
            held,
            request.timeout_seconds,
        )
        return ProviderResponse(
            text=self._the_answer_in(answered),
            finish_reason="stop",
            raw={
                "tool": "m365-copilot",
                "price_status": UNPRICED,
                "latency_ms": max(0, int((time.monotonic() - started) * 1000)),
                "conversation": which,
            },
        )

    def _the_question(self, request: ProviderRequest) -> dict[str, Any]:
        """One message, with everything said before it as background.

        Microsoft keeps the conversation at their end and the harness keeps it
        at this one. Two copies of the same thing drift apart the first time
        anything is retried or started again, and then nobody can say which is
        the real one - so there is one copy, the harness's, and a fresh
        conversation is opened for each question with what went before handed
        over as background.
        """

        said = [one for one in request.messages if str(one.get("content") or "").strip()]
        if not said:
            raise HarnessError("There is nothing to ask Microsoft 365 Copilot")
        asking = str(said[-1].get("content") or "")
        before = said[:-1]
        held: dict[str, Any] = {
            "message": {"text": asking},
            "locationHint": {"timeZone": self.where_they_are},
        }
        background = []
        if request.system_prefix:
            background.append({"text": request.system_prefix})
        if before:
            background.append({"text": "What was said before this:\n" + "\n".join(
                f"{'They asked' if one.get('role') == 'user' else 'You answered'}: "
                f"{str(one.get('content') or '')}"
                for one in before
            )})
        if background:
            held["additionalContext"] = background
        return held

    def _say(
        self, url: str, body: dict[str, Any], headers: dict[str, str], timeout: float | None
    ) -> dict[str, Any]:
        """Put one thing to Microsoft, and turn a refusal into a sentence.

        The usual poster throws away which number came back, and here the
        number is most of the meaning: turned down and out of seats and signed
        out are three different afternoons.
        """

        asked = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        response_holder: dict[str, Any] = {}
        unregister_cancel = cancellation.register(
            lambda: _interrupt_http_response(response_holder.get("response"))
            if response_holder.get("response") is not None else None
        )
        try:
            cancellation.checkpoint()
            with self._http_opener.open(asked, timeout=min(timeout or LONGEST_WAIT, LONGEST_WAIT)) as answered:
                response_holder["response"] = answered
                cancellation.checkpoint()
                raw = answered.read(20_000_000)
        except urllib.error.HTTPError as exc:
            try:
                body_said = exc.read(16_000).decode("utf-8", errors="replace")
            finally:
                exc.close()
            cancellation.checkpoint()
            raise self._what_that_number_meant(exc.code, body_said) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            cancellation.checkpoint()
            raise HarnessError(
                f"Microsoft 365 Copilot could not be reached: {self._redactor.text(str(exc))}"
            ) from exc
        finally:
            unregister_cancel()
        cancellation.checkpoint()
        try:
            held = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HarnessError("Microsoft 365 Copilot answered with something that is not JSON") from exc
        if not isinstance(held, dict):
            raise HarnessError("Microsoft 365 Copilot answered with something that is not an answer")
        return held

    def _what_that_number_meant(self, number: int, body: str) -> HarnessError:
        theirs = self._redactor.text(" ".join(body.split()))[:400]
        if number == 401:
            return SignInNeeded(
                "Microsoft no longer accepts this sign-in. Open Your team and sign in again."
            )
        if number == 403:
            # The one everybody hits first, and the one where a wrong guess
            # costs the most: three different missing things, and the person
            # who fixes each is a different person.
            return HarnessError(
                "Microsoft allowed the sign-in and would not answer the question. That is "
                f"one of two things, and the message below usually says which. {ABOUT_THE_SEAT} "
                f"Or: {WHAT_THE_ADMINISTRATOR_APPROVES} Microsoft's own words: {theirs}"
            )
        if number == 404:
            return HarnessError(
                "Microsoft has no Copilot chat at that address. This runs on their preview "
                "address, which their own note says can change, so it may have moved or been "
                f"turned off. Microsoft's own words: {theirs}"
            )
        if number == 429:
            return HarnessError(
                "Microsoft is asking for fewer questions for a while. Wait a minute and try "
                f"again. Microsoft's own words: {theirs}"
            )
        if 500 <= number <= 599:
            return HarnessError(
                f"Something went wrong at Microsoft's end ({number}), not here. Microsoft's "
                f"own words: {theirs}"
            )
        return HarnessError(f"Microsoft 365 Copilot said no ({number}): {theirs}")

    def _the_answer_in(self, said: dict[str, Any]) -> str:
        """The answer out of what came back.

        What comes back holds the question as well as the answer - Microsoft
        echoes it - and the answer is the last of them. Read as "the first one"
        this hands somebody their own question back and calls it an answer,
        which reads as the assistant being broken rather than this being wrong.
        """

        messages = said.get("messages")
        if not isinstance(messages, list) or not messages:
            raise HarnessError("Microsoft 365 Copilot answered with nothing")
        for one in reversed(messages):
            if not isinstance(one, dict):
                continue
            text = _just_the_words(self._redactor.text(str(one.get("text") or "")))
            if text:
                return text
        raise HarnessError("Microsoft 365 Copilot answered with nothing")


def what_is_missing(settings: dict[str, Any]) -> str:
    """Why this route cannot be used yet, if it cannot, in one sentence.

    Read before anybody types, so the board can say so rather than letting
    somebody write a message and find out afterwards.
    """

    if not str(settings.get("microsoft_app") or "").strip():
        return HOW_TO_REGISTER_AN_APP
    held = read_the_sign_in()
    if not held.get("token"):
        return (
            "Nobody has signed in to Microsoft on this machine yet. Open Your team and "
            "press Sign in to Microsoft. You will be given a short code to paste into a "
            "browser, once."
        )
    if not held.get("renew_with") and float(held.get("runs_out_at") or 0) <= time.time():
        return "The Microsoft sign-in has run out. Open Your team and sign in again."
    return ""
