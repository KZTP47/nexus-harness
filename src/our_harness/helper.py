"""Calling for help part way through a job.

The harness runs a fixed set of jobs: one plans, one writes, one reads the work
back. That covers the shape of the work, and not the moment in the middle of it
where somebody needs one question answered before they can carry on - "is this
function used anywhere else?", "which of these two is the real entry point?".

Until now the only answer to that was to stop and ask a person. This is the
other answer, borrowed from deepseek-harness, where an agent can start a
short-lived helper and get a report back: one question, one answer, on an
assistant you already pay for.

What it is not, on purpose:

  - Not another agent with tools. It cannot read files, run commands, or change
    anything. It is asked something and it answers.
  - Not long-lived. One question, one answer, a short limit, and it is gone.
  - Not free of the rules. It goes through the same provider routes as
    everything else, so a route nobody trusted is a route it cannot use.

Everything it is given, and everything it says, has credentials taken out of it
first, the same as every other thing the harness writes down.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from .config import LoadedConfig
from .models import HarnessError, ProviderRequest
from .providers import ProviderRegistry, create_provider
from .redaction import CredentialRedactor

# A question is a question, not a document. Anything longer belongs in the
# project, with the question pointing at it.
LONGEST_QUESTION = 4000
# And an answer is an answer. This is far above a useful one and far below
# anything that would fill a screen.
LONGEST_ANSWER = 20_000
# How long one helper may take. Long enough for a real answer on a slow
# morning, short enough that a job does not sit waiting on it.
LONGEST_WAIT_SECONDS = 180.0
# What it is told about itself. Short on purpose: a helper that thinks it is
# running the job starts trying to do the job.
HOW_TO_ANSWER = (
    "You are answering one question for somebody in the middle of a piece of "
    "work. Answer it directly and briefly. Say plainly when you do not know. "
    "Do not ask for anything, do not offer to do the work, and do not write "
    "code unless the question asks for code."
)


class HelperError(HarnessError):
    """A question that could not be asked, or an answer that did not come."""


@dataclass
class Answer:
    """What one helper said."""

    question: str
    answer: str
    who: str
    model: str
    milliseconds: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "answer": self.answer,
            "who": self.who,
            "model": self.model,
            "milliseconds": self.milliseconds,
        }


def _in_plain_words(exc: Exception) -> str:
    """The sentence inside what a tool printed, rather than the whole of it.

    A signed-in tool that will not answer usually says why in one sentence, and
    then wraps it in a page of machine-readable detail. Handing the whole thing
    to somebody who asked a question is how a plain "your organisation does not
    have access" becomes something nobody reads.
    """

    from .chat import _in_plain_words as the_same_rule

    # One rule, in one place. This and the conversation both hand a person the
    # reason a tool would not answer, and a page of markup is no better here.
    return the_same_rule(exc)


def who_could_help(config: LoadedConfig) -> list[dict[str, str]]:
    """Every route set up on this machine that could answer a question."""

    routes = config.get("providers", {}) or {}
    found = [
        {
            "route": str(name),
            "model": str(held.get("model") or ""),
            "kind": str(held.get("kind") or ""),
        }
        for name, held in routes.items()
        if isinstance(held, dict)
    ]
    if not found:
        # No named routes: the one the harness uses by default is still an
        # answer, and on most machines it is the only one.
        name = str(config.get("provider.name") or "")
        if name:
            found = [{
                "route": "",
                "model": str(config.get("provider.model") or ""),
                "kind": name,
            }]
    return sorted(found, key=lambda one: one["route"])


def ask_for_help(
    config: LoadedConfig,
    question: str,
    *,
    who: str = "",
    seconds: float = LONGEST_WAIT_SECONDS,
) -> Answer:
    """Ask one assistant one question, and hand back what it said.

    `who` names a provider route. Empty means the one this project uses by
    default, which is what somebody who has set up a single seat wants and has
    not had to think about.
    """

    asked = str(question or "").strip()
    if not asked:
        raise HelperError("Say what to ask. A helper answers a question, and this is empty.")
    if len(asked) > LONGEST_QUESTION:
        raise HelperError(
            f"That question is longer than {LONGEST_QUESTION} letters. A helper answers a "
            "question; anything that long belongs in the project, with the question "
            "pointing at it."
        )
    if any(ord(letter) < 32 and letter not in "\t\n\r" for letter in asked):
        raise HelperError("That question holds a control character.")
    seconds = max(5.0, min(float(seconds or LONGEST_WAIT_SECONDS), LONGEST_WAIT_SECONDS))

    redactor = CredentialRedactor(config)
    registry = ProviderRegistry(config)
    route = str(who or "").strip()
    try:
        # The same road every agent takes to reach an assistant: a named route
        # if one was asked for, otherwise the one this project uses. A route
        # nobody set up is a route this cannot use, which is the point.
        routed = registry.provider_config(route) if route else config
        provider = create_provider(routed)
    except HarnessError as exc:
        raise HelperError(
            redactor.text(
                f"{route or 'The assistant this project uses'} cannot be reached: "
                f"{_in_plain_words(exc)}"
            )
        ) from exc

    model = str(routed.get("provider.model") or "")
    request = ProviderRequest(
        system_prefix=HOW_TO_ANSWER,
        dynamic_context="",
        messages=[{"role": "user", "content": redactor.text(asked)}],
        model=model,
        temperature=0.2,
        max_output_tokens=1024,
        timeout_seconds=seconds,
    )
    started = time.monotonic()
    try:
        answered = provider.complete(request)
    except HarnessError as exc:
        raise HelperError(
            # Redacted, like everything else: a key that is wrong comes back
            # inside the reason it was refused.
            redactor.text(
                f"{route or 'The assistant'} was asked and did not answer: "
                f"{_in_plain_words(exc)}"
            )
        ) from exc
    said = redactor.text(str(getattr(answered, "text", "") or "").strip())
    if not said:
        raise HelperError(
            f"{route or 'The assistant'} answered with nothing at all."
        )
    return Answer(
        question=asked,
        answer=said[:LONGEST_ANSWER],
        who=route or str(config.get("provider.name") or "the usual one"),
        model=model,
        milliseconds=int((time.monotonic() - started) * 1000),
    )
