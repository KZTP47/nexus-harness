"""Recognising a provider that refused one prompt, as opposed to a broken connection.

OpenAI's reasoning models run every prompt through a policy classifier and
answer "Invalid prompt: your prompt was flagged as potentially violating our
usage policy" when it fires. Its known false-positive trigger is text that
reads like the model's own hidden reasoning. A looping agent can produce
exactly that, and Nexus used to echo it back into the next prompt. Anthropic
has a similar "appears to violate our Usage Policy" refusal.

Either way the sign-in, the route and the model all still work. The fix is
a different prompt, not a repaired connection. This module only recognises
the refusal; the goal engine decides how to retry.
"""

from __future__ import annotations

import re
from typing import Any

PROMPT_REFUSAL_VERSION = 1

_PROMPT_REFUSAL = re.compile(
    r"flagged as potentially violating"
    r"|invalid prompt:[^\n]{0,200}usage polic"
    r"|reasoning#advice-on-prompting"
    r"|appears to violate (?:our|the) usage polic"
    r"|violat\w* (?:our|the) usage policy",
    re.IGNORECASE,
)


def prompt_was_refused(text: Any) -> bool:
    """True when a provider refused the prompt itself under its usage policy."""

    return bool(_PROMPT_REFUSAL.search(str(text or "")))
