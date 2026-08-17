"""Ready-made checks, screen sizes, and made-up data.

Most people do not get stuck on the idea of a test. They get stuck on the empty
file. So this holds a small shelf of checks that already work, in the words a
person would use to ask for them: "a page opens without errors", "a login form
works", "an answer keeps its shape".

Each one is a real case, ready to run, with a note saying what to change. Pick
one, change the address, and you have a test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .models import HarnessError


class StarterError(HarnessError):
    """A problem picking or filling in a ready-made check."""


# Common screen sizes, so nobody has to remember numbers. A check can name one
# instead of writing width and height.
SCREENS: dict[str, tuple[int, int]] = {
    "phone": (390, 844),
    "small-phone": (360, 640),
    "tablet": (820, 1180),
    "laptop": (1280, 800),
    "desktop": (1600, 900),
    "wide": (1920, 1080),
}


@dataclass(frozen=True)
class Starter:
    """One ready-made check, with words explaining when to use it."""

    key: str
    title: str
    what_it_does: str
    change_this: str
    needs: str
    case: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "what_it_does": self.what_it_does,
            "change_this": self.change_this,
            "needs": self.needs,
            "case": dict(self.case),
        }


STARTERS: tuple[Starter, ...] = (
    Starter(
        key="page-opens",
        title="A page opens with no errors",
        what_it_does=(
            "Opens the page in a real browser and fails if the browser reports an error, "
            "if a file it asked for did not arrive, or if the page cannot be read by "
            "somebody using a screen reader."
        ),
        change_this="Change the address to your own page.",
        needs="Node.js with Playwright",
        case={
            "id": "page-opens",
            "title": "The home page opens with no errors",
            "kind": "browser",
            "tags": ["ui"],
            "url": "http://127.0.0.1:8765/",
            "check_accessibility": True,
            "expect": {
                "max_console_errors": 0,
                "max_page_errors": 0,
                "max_failed_requests": 0,
                "max_accessibility_problems": 0,
            },
        },
    ),
    Starter(
        key="sign-in",
        title="Signing in works",
        what_it_does="Types a name and a password, presses the button, and waits for the page to say you are in.",
        change_this="Change the address, the three names in the steps, and the words you expect to see.",
        needs="Node.js with Playwright",
        case={
            "id": "sign-in-works",
            "title": "A person can sign in",
            "kind": "browser",
            "tags": ["ui", "sign-in"],
            "url": "http://127.0.0.1:8765/",
            "steps": [
                {"do": "type", "target": "#username", "text": "${env.USER_NAME}", "note": "Type the name"},
                {"do": "type", "target": "#password", "text": "${env.PASSWORD}", "note": "Type the password"},
                {"do": "click", "target": "button[type=submit]", "note": "Press sign in"},
                {"do": "expect_text", "target": "body", "text": "Welcome", "note": "The page says you are in"},
            ],
            "expect": {"max_page_errors": 0},
        },
    ),
    Starter(
        key="form-refuses-empty",
        title="A form refuses to be sent empty",
        what_it_does="Presses the button with nothing filled in and expects the page to say what is missing.",
        change_this="Change the address, the button, and the words the page should show.",
        needs="Node.js with Playwright",
        case={
            "id": "form-refuses-empty",
            "title": "The form says what is missing",
            "kind": "browser",
            "tags": ["ui", "forms"],
            "url": "http://127.0.0.1:8765/",
            "steps": [
                {"do": "click", "target": "button[type=submit]", "note": "Press send with nothing filled in"},
                {"do": "expect_visible", "target": "[role=alert]", "note": "A message appears"},
            ],
            "expect": {"max_page_errors": 0},
        },
    ),
    Starter(
        key="page-is-quick",
        title="A page loads quickly enough",
        what_it_does="Measures how long the page takes to load, when it first shows anything, and how much it pulls down.",
        change_this="Change the address and the limits to what your project promises.",
        needs="Node.js with Playwright",
        case={
            "id": "page-is-quick",
            "title": "The home page loads quickly enough",
            "kind": "browser",
            "tags": ["ui", "speed"],
            "url": "http://127.0.0.1:8765/",
            "expect": {"max_load_ms": 3000, "max_first_paint_ms": 1500, "max_requests": 50},
        },
    ),
    Starter(
        key="looks-the-same",
        title="A page still looks the same",
        what_it_does="Takes a picture of the page and compares it with the one you kept, marking anything that moved.",
        change_this="Change the address, then run: harness qa baseline",
        needs="Node.js with Playwright",
        case={
            "id": "looks-the-same",
            "title": "The home page still looks the same",
            "kind": "visual",
            "tags": ["ui", "looks"],
            "url": "http://127.0.0.1:8765/",
            "viewport": {"width": 1280, "height": 800},
            "expect": {"max_changed_percent": 0.5},
        },
    ),
    Starter(
        key="answer-keeps-its-shape",
        title="An answer keeps its shape",
        what_it_does="Asks a server for something and checks the answer holds the fields it promised, of the right kinds.",
        change_this="Change the address and the shape under contract.",
        needs="Nothing extra",
        case={
            "id": "answer-keeps-its-shape",
            "title": "The list of things comes back in the promised shape",
            "kind": "http",
            "tags": ["api"],
            "url": "http://127.0.0.1:8765/api/health",
            "expect": {
                "status": 200,
                "contract": {
                    "type": "object",
                    "required": ["status"],
                    "properties": {"status": {"type": "string"}},
                },
            },
        },
    ),
    Starter(
        key="api-needs-a-key",
        title="A private address refuses a stranger",
        what_it_does="Asks without a key and expects to be turned away, so a private thing does not become public by accident.",
        change_this="Change the address and the status you expect.",
        needs="Nothing extra",
        case={
            "id": "api-needs-a-key",
            "title": "The private address refuses a request with no key",
            "kind": "http",
            "tags": ["api", "safety"],
            "url": "http://127.0.0.1:8765/api/events",
            "expect": {"status": 400},
        },
    ),
    Starter(
        key="many-logins",
        title="The same check over a table of examples",
        what_it_does="Runs one check once for every row of a table, so twenty examples cost one written check.",
        change_this="Change the rows, and use ${row.name} anywhere in the check.",
        needs="Nothing extra",
        case={
            "id": "answers-for-each",
            "title": "Every example answers",
            "kind": "http",
            "tags": ["api"],
            "url": "http://127.0.0.1:8765/api/${row.path}",
            "rows": [{"path": "health"}, {"path": "health?again=1"}],
            "expect": {"status": 200},
        },
    ),
    Starter(
        key="tests-pass",
        title="The project's own tests pass",
        what_it_does="Runs the command you already use for tests and fails if it does.",
        change_this="Change the command to yours.",
        needs="Nothing extra",
        case={
            "id": "project-tests",
            "title": "The project tests pass",
            "kind": "command",
            "tags": ["tests"],
            "command": ["python", "-m", "unittest", "discover", "-q"],
            "expect": {"exit_code": 0},
        },
    ),
    Starter(
        key="no-keys-in-the-code",
        title="No credentials in the code",
        what_it_does="Reads your files and fails if anything looks like a key, a token, or a password left in the code.",
        change_this="Change the paths to the folders you write.",
        needs="Nothing extra",
        case={
            "id": "no-keys-in-the-code",
            "title": "No credentials are written into the code",
            "kind": "secrets",
            "tags": ["safety"],
            "paths": ["src/**/*", "*.md"],
            "expect": {"max_findings": 0},
        },
    ),
    Starter(
        key="every-page",
        title="Every page of the site opens",
        what_it_does="Follows the links from one page and opens each one, reporting any that answer with an error.",
        change_this="Change the address and how far to follow links.",
        needs="Node.js with Playwright",
        case={
            "id": "every-page-opens",
            "title": "Every page opens with no errors",
            "kind": "crawl",
            "tags": ["ui"],
            "url": "http://127.0.0.1:8765/",
            "max_pages": 20,
            "expect": {"max_broken_pages": 0, "max_console_errors": 0},
        },
    ),
    Starter(
        key="works-on-a-phone",
        title="The page works at phone size",
        what_it_does=(
            "Opens the page in a window the size of a phone and checks it still works there, "
            "which is where most people will see it."
        ),
        change_this=(
            "Change the address. For a tablet use 820 by 1180, for a laptop 1280 by 800, "
            "and copy the check once for each size you care about."
        ),
        needs="Node.js with Playwright",
        case={
            "id": "works-on-a-phone",
            "title": "The page works at phone size",
            "kind": "browser",
            "tags": ["ui"],
            "url": "http://127.0.0.1:8765/",
            "viewport": {"width": 390, "height": 844},
            "check_accessibility": True,
            "expect": {"max_console_errors": 0, "max_page_errors": 0},
        },
    ),
)

BY_KEY: dict[str, Starter] = {item.key: item for item in STARTERS}


def listed() -> list[dict[str, Any]]:
    """Every ready-made check, for showing in a list."""

    return [item.to_dict() for item in STARTERS]


def screen(name: str) -> tuple[int, int]:
    """The width and height of a named screen size."""

    key = str(name or "").strip().lower()
    if key not in SCREENS:
        raise StarterError(
            f"There is no screen size called {name}. Known ones: {', '.join(SCREENS)}"
        )
    return SCREENS[key]


_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _joined(given: str, example: str) -> str:
    """The address to use, keeping the part of the example that still matters.

    Somebody typing "http://127.0.0.1:8080/" means "my site is here". If the
    ready-made check looks at a particular page, such as /api/health, throwing
    that path away would leave a check that asks the wrong thing. So a bare
    address keeps the example's path, and an address with its own path wins.
    """

    import urllib.parse

    chosen = urllib.parse.urlsplit(given)
    if chosen.path not in ("", "/") or chosen.query:
        return given
    from_example = urllib.parse.urlsplit(example)
    if from_example.path in ("", "/") and not from_example.query:
        return given
    return urllib.parse.urlunsplit(
        (chosen.scheme, chosen.netloc, from_example.path, from_example.query, "")
    )


def build(key: str, *, url: str = "", case_id: str = "") -> dict[str, Any]:
    """One ready-made check, with the address and the name you want."""

    if key not in BY_KEY:
        raise StarterError(
            f"There is no ready-made check called {key}. Known ones: {', '.join(BY_KEY)}"
        )
    case = dict(BY_KEY[key].case)
    if url:
        if not url.startswith(("http://", "https://")):
            raise StarterError("The address must start with http:// or https://")
        case["url"] = _joined(url, str(case.get("url") or ""))
    if case_id:
        clean = case_id.strip().lower()
        if not _SAFE_ID.fullmatch(clean):
            raise StarterError(
                "A check name may hold lowercase letters, digits, dash and underscore, "
                "and must start with a letter or digit"
            )
        case["id"] = clean
    return case


# ---------------------------------------------------------------------------
# Made-up data for tables
# ---------------------------------------------------------------------------

_FIRST_NAMES = (
    "ada", "bo", "cai", "dara", "eli", "fen", "gita", "hal", "ines", "jo",
    "kai", "lena", "mo", "nia", "omar", "pia", "quinn", "rui", "sam", "tess",
)
_LAST_NAMES = (
    "archer", "bell", "chen", "diaz", "evans", "faber", "gomez", "hill",
    "imani", "jones", "kaur", "lopez", "mensah", "novak", "olsen", "park",
)
_WORDS = (
    "quick", "quiet", "bright", "steady", "clear", "plain", "warm", "sharp",
    "small", "wide", "early", "late", "green", "blue", "grey", "gold",
)
MAX_MADE_UP_ROWS = 500


def made_up_rows(count: int, columns: Iterable[str], seed: int = 1) -> list[dict[str, str]]:
    """A table of made-up values, the same every time for the same seed.

    Test data has to be repeatable, or a check that fails cannot be looked at
    again. Nothing here is random at run time: the same seed always gives the
    same table.

    Known column names get sensible values: name, first_name, last_name, email,
    password, phone, city, number, word, id, date. Anything else gets the
    column name and the row number.
    """

    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= MAX_MADE_UP_ROWS:
        raise StarterError(f"Ask for 1 to {MAX_MADE_UP_ROWS} rows")
    wanted = [str(name).strip() for name in columns if str(name).strip()]
    if not wanted:
        raise StarterError("Name at least one column")
    repeated = sorted({name for name in wanted if wanted.count(name) > 1})
    if repeated:
        # Asking for the same column twice would quietly give one column back.
        raise StarterError(f"The column {repeated[0]} was asked for more than once")
    if len(wanted) > 30:
        raise StarterError("A made-up table may hold at most 30 columns")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise StarterError("The seed must be a whole number")
    rows: list[dict[str, str]] = []
    for number in range(count):
        step = (seed * 7919 + number * 104729) % 1_000_003
        first = _FIRST_NAMES[step % len(_FIRST_NAMES)]
        last = _LAST_NAMES[(step // 7) % len(_LAST_NAMES)]
        word = _WORDS[(step // 13) % len(_WORDS)]
        row: dict[str, str] = {}
        for name in wanted:
            plain = name.strip().lower().replace(" ", "_")
            if plain in ("name", "full_name"):
                row[name] = f"{first.title()} {last.title()}"
            elif plain in ("first_name", "given_name", "firstname"):
                row[name] = first.title()
            elif plain in ("last_name", "family_name", "surname", "lastname"):
                row[name] = last.title()
            elif plain in ("email", "e_mail", "mail"):
                row[name] = f"{first}.{last}{number}@example.com"
            elif plain in ("password", "pass", "secret"):
                # It says "example" on purpose. Made-up data must never be
                # mistaken for a real credential, by a person or by the scan
                # that looks for credentials left in the code.
                row[name] = f"example-password-{word}-{number}"
            elif plain in ("phone", "telephone", "mobile"):
                row[name] = f"+1-555-{1000 + (step % 9000)}"
            elif plain in ("city", "town"):
                row[name] = f"{word.title()} Town"
            elif plain in ("number", "count", "quantity", "amount"):
                row[name] = str(step % 1000)
            elif plain in ("word", "title", "label"):
                row[name] = f"{word} {last}"
            elif plain in ("id", "identifier", "code"):
                row[name] = f"{plain}-{number + 1:04d}"
            elif plain in ("date", "day"):
                row[name] = f"2026-{1 + (step % 12):02d}-{1 + (step % 28):02d}"
            else:
                row[name] = f"{plain}-{number + 1}"
        rows.append(row)
    return rows
