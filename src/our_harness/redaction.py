from __future__ import annotations

import copy
import os
import re
from typing import Any

from .config import LoadedConfig


REDACTED = "[REDACTED]"
# The words that say a name holds a credential. The middle of a name counts,
# so aws_secret_access_key and auth_token are both caught, but a word may not
# run straight on into more lowercase letters: "cookies", "secretive" and
# "tokens" are ordinary words and a count of tokens is a number worth keeping.
# Plurals matter one word at a time. "credentials", "passwords" and "secrets"
# are all real field names, while "cookies" and "tokens" are ordinary words and
# a count of tokens, so only the first group takes an s.
_SECRET_WORDS = (
    r"api[_-]?keys?|access[_-]?token|auth(?:orization)?|bearer|client[_-]?secrets?|"
    r"credentials?|cookie|passwd|passwords?|private[_-]?keys?|secrets?|token"
)
_SENSITIVE_NAME = re.compile(r"(?:" + _SECRET_WORDS + r")(?![a-z])", re.IGNORECASE)
# What may follow the word before the value starts: the rest of the name, an
# optional closing quote for a JSON key, then a colon, an equals sign, or a fat
# arrow. Every piece is bounded, so this is only ever a short look forward.
_VALUE_AFTER_NAME = re.compile(
    r"""
    [A-Za-z0-9_.\-]{0,60}          # the rest of the name, if any
    \s*["']?\s*                    # a JSON key closes its quote first
    (?:=>|[:=])\s*                 # the sign between name and value
    (?:(?:Basic|Bearer|Token|Digest|Negotiate|ApiKey)\s+)?   # a scheme word
    (?:
        # A quoted value is taken whole, escapes and all. Stopping at the
        # first backslash-quote used to cut a secret in half and leave the
        # rest of it in plain sight, which reads as safe and is not.
        # The closing quote only counts when something ends after it, so a
        # loose apostrophe part way through a value cannot end it early.
        "(?P<inside_double>(?:\\.|[^"\\]){0,500})"
        (?=[\s,;)\]}]|$)
      | '(?P<inside_single>(?:\\.|[^'\\]){0,500})'
        (?=[\s,;)\]}]|$)
        # A value that opens with a quote and never closes it, because the text
        # was cut off part way through. Stopping at the next space here used to
        # leave the rest of the secret sitting next to the word REDACTED, so the
        # rest of the line goes with it.
      | (?P<unterminated>["'][^\r\n]{0,500})
        # A value written on the lines below, the way settings files do it:
        #     password: |
        #       the real one
        # The mark is the whole value as far as this is concerned, so the
        # indented lines under it go too. Without this the line above read as
        # hidden while the secret sat underneath it in plain sight.
      | (?P<block>[|>][+-]?\d*[ \t]*\r?\n(?:[ \t]+[^\r\n]*\r?\n?){0,200})
      | (?P<bare>[^\s,;]{1,500})                # or up to the next space
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
# What a bare value may end with and not really be part of it, such as the
# closing brace of the object it sits in.
_TRAILING = "}])\"'"
# A real private key is a few thousand characters between its two markers.
# Without a limit, text holding many "BEGIN" markers and no "END" makes the
# search start again from every marker and read the whole rest of the text each
# time, which turns a large log into minutes of work. The limit keeps every
# real key and gives up quickly on text that only looks like one.
_PRIVATE_KEY_SPAN = 20_000
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.{0,%d}?-----END [^-\r\n]*PRIVATE KEY-----" % _PRIVATE_KEY_SPAN,
    re.IGNORECASE | re.DOTALL,
)
# When a key is opened and never closed, the words between are still worth
# hiding, so the opening marker alone is enough to act on.
_PRIVATE_KEY_START = re.compile(r"-----BEGIN [^-\r\n]*PRIVATE KEY-----", re.IGNORECASE)
_PRIVATE_KEY_END = re.compile(r"-----END [^-\r\n]*PRIVATE KEY-----", re.IGNORECASE)
_BEARER = re.compile(r"(?i)(\bBearer\s+)[A-Za-z0-9._~+/=-]{8,}")
# An address that carries the password inside it, such as
# postgres://user:secret@host. The password is taken out, the rest is kept so
# the line still reads. Every piece is bounded: left open ended, the part
# before "://" would be tried from every character of a long line.
_ADDRESS_WITH_PASSWORD = re.compile(
    r"([a-zA-Z][a-zA-Z0-9+.\-]{0,20}://[^/\s:@]{1,200}:)([^/\s:@]{3,200})@"
)


def _hide_assignments(value: str) -> str:
    """Hide the value of anything whose name says it holds a credential.

    Only the credential words are searched for, which is cheap, and each one is
    followed by a short bounded look forward for a sign and a value. Text with
    none of those words is handed back untouched, which is nearly all text.
    """

    pieces: list[str] = []
    at = 0
    for word in _SENSITIVE_NAME.finditer(value):
        if word.start() < at:
            continue  # inside something already dealt with
        found = _VALUE_AFTER_NAME.match(value, word.end())
        if not found:
            continue
        name = next(
            part
            for part in ("inside_double", "inside_single", "unterminated", "block", "bare")
            if found.group(part) is not None
        )
        start, end = found.span(name)
        if value[start:end].startswith(REDACTED):
            # Already hidden, by an earlier rule or an earlier pass. Hiding it
            # again turns [REDACTED] into [REDACTED]], which reads like a
            # mistake and makes people doubt the rest of the line. Nothing is
            # written down here and the mark is left where it is: the text is
            # handed over untouched by whichever append comes next.
            continue
        if name == "bare":
            # A bare value stops at a space, so it can pick up the bracket that
            # closes whatever it sits in. Those are given back, but nothing
            # else is: a value holding a quote or a brace of its own must be
            # hidden whole, or the rest of it would be left in plain sight.
            while end > start and value[end - 1] in _TRAILING:
                end -= 1
        pieces.append(value[at:start])
        pieces.append(REDACTED)
        at = end
    pieces.append(value[at:])
    return "".join(pieces)


_KNOWN_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{12,}|AKIA[A-Z0-9]{16})(?![A-Za-z0-9])"
)
_JWT = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}(?![A-Za-z0-9_-])")


def hide_private_keys(value: str) -> str:
    """Take out everything between a private key's two markers.

    This walks the text once, looking for the next opening marker and then the
    next closing one after it. A pattern that tries to do the same thing has to
    guess where a key ends, and guesses badly on text holding thousands of
    openings and one ending: it starts again from every opening and reads to
    the end each time. Reading forwards once cannot behave that way, however
    strange the text is.
    """

    if "-----BEGIN" not in value:
        return value
    # Where every marker is, found in one pass each. Walking two lists that are
    # already in order means the work grows with the length of the text and
    # nothing else, however many markers there are.
    openings = [match.span() for match in _PRIVATE_KEY_START.finditer(value)]
    if not openings:
        return value
    closings = [match.span() for match in _PRIVATE_KEY_END.finditer(value)]
    pieces: list[str] = []
    at = 0
    next_closing = 0
    for number, (start, end) in enumerate(openings):
        if start < at:
            continue  # already inside a part that was hidden
        pieces.append(value[at:start])
        pieces.append("[REDACTED PRIVATE KEY]")
        while next_closing < len(closings) and closings[next_closing][0] < end:
            next_closing += 1
        closing = closings[next_closing] if next_closing < len(closings) else None
        following = openings[number + 1][0] if number + 1 < len(openings) else None
        # A key that was opened and never closed still has its contents hidden,
        # up to the next opening marker, so nothing leaks because an ending is
        # missing.
        if closing and (following is None or closing[0] < following):
            at = closing[1]
        elif following is not None:
            at = following
        else:
            at = len(value)
    pieces.append(value[at:])
    return "".join(pieces)


class CredentialRedactor:
    """Remove credential material before network transmission or persistence."""

    def __init__(self, config: LoadedConfig | None = None):
        configured_names = {"HARNESS_API_KEY"}
        if config is not None:
            configured = str(config.get("provider.api_key_env") or "")
            if configured:
                configured_names.add(configured)
            profiles = config.get("providers", {})
            if isinstance(profiles, dict):
                for profile in profiles.values():
                    if not isinstance(profile, dict):
                        continue
                    profile_name = profile.get("api_key_env")
                    if isinstance(profile_name, str) and profile_name:
                        configured_names.add(profile_name)
        configured_secrets = {
            os.environ[name] for name in configured_names if os.environ.get(name, "")
        }
        ambient_secrets = {
            value
            for name, value in os.environ.items()
            if _SENSITIVE_NAME.search(name) and len(value) >= 6
        }
        self._secrets = sorted(
            configured_secrets | ambient_secrets,
            key=len,
            reverse=True,
        )

    def text(self, value: str) -> str:
        output = value
        for secret in self._secrets:
            output = output.replace(secret, REDACTED)
        output = hide_private_keys(output)
        output = _BEARER.sub(r"\1" + REDACTED, output)
        output = _KNOWN_TOKEN.sub(REDACTED, output)
        output = _JWT.sub(REDACTED, output)
        output = _hide_assignments(output)
        output = _ADDRESS_WITH_PASSWORD.sub(lambda match: match.group(1) + REDACTED + "@", output)
        return output

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {
                key: REDACTED if isinstance(key, str) and _SENSITIVE_NAME.search(key) else self.value(child)
                for key, child in value.items()
            }
        if isinstance(value, list):
            return [self.value(child) for child in value]
        if isinstance(value, tuple):
            return tuple(self.value(child) for child in value)
        return copy.deepcopy(value)
