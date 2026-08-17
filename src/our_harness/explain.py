"""What a failure means, and what to do about it.

A check that fails says what happened: the exit code, the browser's message,
the missing file. That is the right thing to record and the wrong thing to hand
somebody who has not seen it before. "net::ERR_CONNECTION_REFUSED" is precise
and tells a beginner nothing at all.

This turns what a check saw into a sentence and a short list of what to try.
Every rule here is one somebody has actually needed. Anything it does not
recognise gets an honest answer rather than a guess: what the check said, and
the two or three things worth looking at whatever the reason.

It never guesses at a cause it cannot see, and it never invents a command that
the harness does not have.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Meaning:
    """What went wrong, in words, and what to try."""

    headline: str
    because: str = ""
    try_this: list[str] = field(default_factory=list)
    sure: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "headline": self.headline,
            "because": self.because,
            "try_this": self.try_this,
            "sure": self.sure,
        }


@dataclass(frozen=True)
class Rule:
    """One thing that goes wrong, and what it means."""

    name: str
    looks_like: tuple[str, ...]
    headline: str
    because: str
    try_this: tuple[str, ...]
    # Some words only mean one thing in one kind of check. "2 to look at" is a
    # security scan saying it found something; in a browser check the same
    # words are ordinary English, and reading them as a leaked key sends
    # somebody hunting for a secret that was never there.
    only_for: tuple[str, ...] = ()
    # Every one of these has to be there, not just one of them.
    and_also: tuple[str, ...] = ()


# Every rule is one somebody has really needed. The words on the left are what
# a check, a browser, or a program actually prints.
RULES: tuple[Rule, ...] = (
    Rule(
        name="nothing is listening",
        looks_like=("ERR_CONNECTION_REFUSED", "connection refused", "Failed to fetch",
                    "actively refused it", "Cannot connect to host"),
        headline="Nothing was listening at that address.",
        because="The check asked a server on this machine for a page, and no server answered.",
        try_this=(
            "Start the thing being checked, then run the check again.",
            "Look at the address in the check: a different port is the usual reason.",
            "If it is the harness's own panel, run: harness ui",
        ),
    ),
    Rule(
        name="the browser is missing",
        looks_like=("playwright", "Executable doesn't exist", "browserType.launch",
                    "npx playwright install"),
        headline="The browser this check needs is not installed.",
        because="Browser checks drive a real browser, which is a separate download.",
        try_this=(
            "Run: npm install playwright",
            "Then: npx playwright install chromium",
            "Every other kind of check works without it.",
        ),
    ),
    Rule(
        name="node is missing",
        looks_like=("'node' is not recognized", "node: command not found",
                    "No such file or directory: 'node'"),
        headline="Node.js is not on this machine.",
        because="Browser and screenshot checks are driven by Node.js.",
        try_this=(
            "Install Node.js from nodejs.org, then open a new terminal.",
            "Check it with: node --version",
            "Checks of every other kind need nothing installed.",
        ),
    ),
    Rule(
        name="waiting for something that never appeared",
        looks_like=("Timeout", "timed out", "waiting for selector", "waitForSelector",
                    "exceeded while waiting"),
        headline="The check waited for something that never appeared.",
        because="A step asked for a part of the page, and it was still not there when time ran out.",
        try_this=(
            "Look at the picture of the page in the run folder: it shows what was on screen.",
            "The thing may be named differently now. Use Point at something to pick it again.",
            "If it is only slow, give that step more time with timeout_ms.",
        ),
    ),
    Rule(
        name="the words are not there",
        looks_like=("does not hold the text", "expected to read", "text not found"),
        headline="The page did not say what the check expected.",
        because="The check looked for some words and the page had different ones.",
        try_this=(
            "Read what the page did say, above. Often the wording simply changed.",
            "If the new wording is right, change the check to match it.",
        ),
    ),
    Rule(
        name="a program failed",
        looks_like=("exit code", "exited with", "non-zero"),
        headline="A program the check ran finished badly.",
        because="The check runs a command and expects it to finish cleanly.",
        try_this=(
            "Run that same command in your terminal: it will say more than the check can.",
            "Look at the last few lines above, which are the program's own words.",
        ),
    ),
    Rule(
        name="a file is missing",
        looks_like=("No such file", "does not exist", "FileNotFoundError", "cannot be read"),
        headline="A file the check needs is not there.",
        because="The check names a file, and nothing was at that path.",
        try_this=(
            "Check the path in the check. It is read from the top of your project.",
            "If the file moved, change the check to the new place.",
        ),
    ),
    Rule(
        name="credentials in the code",
        looks_like=("a password or key", "an API key", "a private key",
                    "credentials left", " to look at"),
        and_also=("files",),
        headline="Something that looks like a credential is in your files.",
        because="The security scan reads your own files for keys and passwords left in them.",
        try_this=(
            "Look at what it found. If it is a real key, take it out and change it.",
            "If it is an example, put it beyond the scan or mark it as allowed.",
            "A key belongs in an environment variable, never in a file you commit.",
        ),
    ),
    Rule(
        name="no model connected",
        looks_like=("No model is connected", "provider configuration", "no provider",
                    "provider.api_key_env requires trusted",
                    "provider.command requires trusted",
                    "provider.endpoint requires trusted"),
        headline="The harness has no model it can use yet.",
        because="This step asks a model, and none is set up on this machine.",
        try_this=(
            "Open the first screen and press \"I don't care, just do it for me\".",
            "Or run: harness doctor",
            "A subscription you already pay for will do: no key needed.",
        ),
    ),
    Rule(
        name="a setting nobody has trusted",
        looks_like=("requires trusted", "cannot raise the trusted limit",
                    "from shareable project config"),
        headline="A setting only counts once you say the file is yours.",
        because=(
            "Settings that can start programs, call addresses, or hand over your "
            "environment are only read from your own settings file, and only once "
            "you have said that file is yours."
        ),
        try_this=(
            "The message above names the setting. Move it to .harness/config.local.json.",
            "Then read that file, and run: harness trust",
            "The Settings tab does both of those for you.",
        ),
    ),
    Rule(
        name="the page had an error",
        looks_like=("console error", "page error", "Uncaught", "ReferenceError",
                    "TypeError:"),
        headline="The page itself hit an error while the check watched.",
        because="The check counts errors the browser reports, and there were more than allowed.",
        try_this=(
            "The first error is above. That one is usually the cause of the rest.",
            "This is a real problem in the page, not in the check.",
        ),
    ),
    Rule(
        name="something else already has the port",
        looks_like=("Address already in use", "only one usage of each socket",
                    "EADDRINUSE"),
        headline="Something is already using that port.",
        because="Two things cannot listen at the same address.",
        try_this=(
            "Something is already running there. Use it, or stop it first.",
            "Or start the panel on another port: harness ui --port 8766",
        ),
    ),
    Rule(
        name="a picture changed",
        looks_like=("pixels differ", "looks different", "baseline"),
        headline="The page looks different from the picture that was saved.",
        because="A visual check compares the page with a picture you saved earlier.",
        try_this=(
            "Look at the two pictures in the run folder, side by side.",
            "If the new look is right, press Save screenshots to keep it as the new one.",
        ),
    ),
)

# What is worth looking at whatever went wrong.
WHATEVER_IT_IS = (
    "Read the last few lines above: they are what the check itself saw.",
    "The run folder holds a picture of the page and everything that was printed.",
    "Running the same thing by hand in a terminal usually says more.",
)


def what_it_means(said: str, *, kind: str = "") -> Meaning:
    """Turn what a check saw into a sentence and a few things to try."""

    text = str(said or "").strip()
    if not text:
        return Meaning(
            headline="This did not say what went wrong.",
            because="Nothing was recorded for this failure, which is itself worth reporting.",
            try_this=list(WHATEVER_IT_IS),
            sure=False,
        )
    low = text.lower()
    for rule in RULES:
        if rule.only_for and str(kind or "").lower() not in rule.only_for:
            continue
        if rule.and_also and not all(mark.lower() in low for mark in rule.and_also):
            continue
        if any(mark.lower() in low for mark in rule.looks_like):
            return Meaning(
                headline=rule.headline,
                because=rule.because,
                try_this=list(rule.try_this),
            )
    # Nothing recognised. Say so, rather than making something up: a confident
    # wrong answer sends somebody looking in the wrong place for an hour.
    return Meaning(
        headline="This one is not a failure the harness recognises.",
        because=_first_useful_line(text),
        try_this=list(WHATEVER_IT_IS) + _about_this_kind(kind),
        sure=False,
    )


def _first_useful_line(text: str) -> str:
    """The first line worth showing, without a wall of stack trace."""

    for line in text.splitlines():
        tidy = line.strip()
        if not tidy:
            continue
        if re.match(r"^(File \"|\s*at |Traceback)", tidy):
            continue
        return tidy[:200]
    return text.strip()[:200]


def _about_this_kind(kind: str) -> list[str]:
    extra = {
        "browser": ["A browser check keeps a picture of the page at the moment it stopped."],
        "command": ["Run the command yourself: the check runs it exactly as written."],
        "http": ["Ask the same address in your browser and see what comes back."],
        "secrets": ["The scan lists every file it read, and what it found in each."],
        "visual": ["Both pictures, old and new, are kept in the run folder."],
        "crawl": ["The report lists every page it walked and what each one answered."],
    }
    return extra.get(kind, [])
