"""Pointing at something on a page and getting a name for it.

Writing a check means naming the thing you want: a button, a box, a heading.
Newcomers get this wrong more than anything else, because the name they write
matches nothing, or matches nine things and the check quietly watches the wrong
one.

So the harness opens the page, lets a person click what they mean, and offers
names that were tried on the real page first. Only a name that matches exactly
one thing is offered at all.

The order the names are offered in matters. A name written for testing, such as
`data-testid`, survives redesigns. A name built from where the thing sits in the
page breaks the moment anyone moves it. The older tool this replaces put single
class names above test attributes and never counted the matches, so it happily
handed people a name that matched half the page.
"""

from __future__ import annotations

import json
import re
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .config import LoadedConfig
from .execution import CommandRunner
from .models import HarnessError
from .safety import confined_path

# Best first. The whole point of this list is that it is a decision written
# down once, in one place, instead of being spread through the page script.
ORDER = ("test-hook", "id", "role", "name", "placeholder", "text", "class", "path")
KIND_REASON = {
    "test-hook": "An attribute put there for testing. This is the safest name to use.",
    "id": "The thing's own id.",
    "role": "What the thing is, and the words a screen reader would read out.",
    "name": "The name a form field is sent under.",
    "placeholder": "The grey hint text inside the box.",
    "text": "The words on the thing itself.",
    "class": "A style class. It changes when someone restyles the page.",
    "path": "Where the thing sits in the page. It breaks as soon as anything moves.",
}
TEST_ATTRIBUTES = ("data-testid", "data-test-id", "data-test", "data-qa", "data-cy")
MAX_CANDIDATES = 200
MAX_SELECTOR_CHARS = 500


class PickError(HarnessError):
    """A problem with what came back from the page."""


@dataclass(frozen=True)
class Candidate:
    """One possible name for the thing that was clicked."""

    selector: str
    kind: str
    matches: int
    detail: str = ""
    warning: str = ""

    @property
    def reason(self) -> str:
        return KIND_REASON.get(self.kind, "A name for this thing.")

    def to_dict(self) -> dict[str, Any]:
        value = {
            "selector": self.selector,
            "kind": self.kind,
            "matches": self.matches,
            "reason": self.reason,
        }
        if self.detail:
            value["detail"] = self.detail
        if self.warning:
            value["warning"] = self.warning
        return value


# Names that a build tool made up and will make up differently next time.
_LOOKS_MADE_UP = (
    re.compile(r"^:r[0-9a-z]+:$"),                 # React's own generated ids
    re.compile(r"(^|[-_])[0-9a-f]{8,}([-_]|$)", re.IGNORECASE),
    re.compile(r"[0-9]{6,}$"),
    re.compile(r"^(mui|ember|ext|yui|radix|headlessui)[-_]?[0-9]", re.IGNORECASE),
)


def made_up_name(value: str) -> str:
    """A warning when a name looks like something a build tool invented."""

    text = (value or "").strip()
    if not text:
        return ""
    for pattern in _LOOKS_MADE_UP:
        if pattern.search(text):
            return (
                f"The name {text} looks like one the page builds fresh every time. "
                "If the check starts failing for no reason, this is why."
            )
    return ""


def _text(value: object, label: str, limit: int = MAX_SELECTOR_CHARS) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise PickError(f"{label} must be text")
    if len(value) > limit:
        raise PickError(f"{label} must be at most {limit} characters")
    return value


def parse_candidates(value: object) -> tuple[Candidate, ...]:
    """Read what the page sent back, refusing anything odd."""

    if not isinstance(value, list):
        raise PickError("The page did not send back a list of names")
    if len(value) > MAX_CANDIDATES:
        raise PickError(f"The page sent back more than {MAX_CANDIDATES} names")
    built: list[Candidate] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise PickError(f"Name {index + 1} from the page is not an object")
        selector = _text(item.get("selector"), f"Name {index + 1}").strip()
        if not selector:
            continue
        kind = _text(item.get("kind"), f"Name {index + 1} kind", limit=32)
        if kind not in ORDER:
            raise PickError(f"Name {index + 1} has a kind this tool does not know: {kind}")
        matches = item.get("matches")
        if isinstance(matches, bool) or not isinstance(matches, int) or matches < 0:
            raise PickError(f"Name {index + 1} must say how many things it matches")
        built.append(
            Candidate(
                selector=selector,
                kind=kind,
                matches=matches,
                detail=_text(item.get("detail"), f"Name {index + 1} detail", limit=200),
                warning=_text(item.get("warning"), f"Name {index + 1} warning", limit=300),
            )
        )
    return tuple(built)


def rank(candidates: Iterable[Candidate]) -> tuple[Candidate, ...]:
    """Best name first, and only names that match exactly one thing.

    A name that matches nothing is useless. A name that matches several things
    is worse than useless, because a check using it passes or fails on whichever
    one the browser happens to pick first.
    """

    kept = [item for item in candidates if item.matches == 1]
    seen: set[str] = set()
    unique: list[Candidate] = []
    for item in kept:
        if item.selector in seen:
            continue
        seen.add(item.selector)
        unique.append(item)
    return tuple(
        sorted(unique, key=lambda item: (ORDER.index(item.kind), len(item.selector), item.selector))
    )


def rejected(candidates: Iterable[Candidate]) -> tuple[Candidate, ...]:
    """The names that were tried and thrown away, worst first, for explaining."""

    return tuple(
        sorted(
            (item for item in candidates if item.matches != 1),
            key=lambda item: (-item.matches, ORDER.index(item.kind)),
        )
    )


def describe(candidate: Candidate) -> str:
    """One line about a name, in words a person can act on."""

    parts = [candidate.selector, "-", candidate.reason]
    if candidate.detail:
        parts.append(f"({candidate.detail})")
    if candidate.warning:
        parts.append(f"Careful: {candidate.warning}")
    return " ".join(parts)


def starter_step(candidate: Candidate, action: str = "expect_visible") -> dict[str, Any]:
    """A ready-made step for a suite file, so the pick is worth something."""

    if action == "run":
        # A snippet needs no target, so it counts how many things the name
        # matches, which is the useful thing to know about a name.
        return {
            "do": "run",
            "script": f"return document.querySelectorAll({candidate.selector!r}).length",
            "text": "1",
        }
    step: dict[str, Any] = {"do": action, "target": candidate.selector}
    if action == "expect_count":
        step["count"] = 1
    if action == "type":
        step["text"] = "something"
    if action == "expect_text":
        step["text"] = candidate.detail or "some words"
    if action == "press":
        step["key"] = "Enter"
    if action == "choose":
        step["value"] = "an option"
    return step


# ---------------------------------------------------------------------------
# The page script
# ---------------------------------------------------------------------------

# This runs inside the page the person is looking at. It draws a box around
# whatever is under the mouse, waits for one click, and works out the possible
# names for that one thing. Every name is tried on the real page before it is
# sent back, which is why each one carries a count.
_PICKER_SCRIPT = r"""
// Written by Our Harness so a person can point at part of a page. Deleted after.
const { chromium } = require('playwright');
const plan = __PLAN__;

function inThePage() {
  return new Promise((resolve) => {
    const marker = document.createElement('div');
    marker.style.cssText = [
      'position:fixed', 'pointer-events:none', 'z-index:2147483646',
      'border:2px solid #1d9bf0', 'background:rgba(29,155,240,0.18)',
      'border-radius:3px', 'display:none',
    ].join(';');
    const hint = document.createElement('div');
    hint.style.cssText = [
      'position:fixed', 'left:50%', 'top:12px', 'transform:translateX(-50%)',
      'z-index:2147483647', 'pointer-events:none', 'background:#10233a',
      'color:#ffffff', 'padding:8px 14px', 'border-radius:6px',
      'font:14px system-ui,sans-serif', 'box-shadow:0 4px 14px rgba(0,0,0,0.4)',
    ].join(';');
    // Written as text, never as page code, so words on the page cannot run.
    hint.textContent = 'Click the thing you want to check. Press Escape to give up.';
    document.body.append(marker, hint);

    const clean = () => {
      marker.remove();
      hint.remove();
      document.removeEventListener('mousemove', onMove, true);
      document.removeEventListener('click', onClick, true);
      document.removeEventListener('keydown', onKey, true);
    };

    let current = null;
    const onMove = (event) => {
      const found = document.elementFromPoint(event.clientX, event.clientY);
      if (!found || found === marker || found === hint) return;
      current = found;
      const box = found.getBoundingClientRect();
      marker.style.display = 'block';
      marker.style.left = box.left + 'px';
      marker.style.top = box.top + 'px';
      marker.style.width = box.width + 'px';
      marker.style.height = box.height + 'px';
    };
    const onKey = (event) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      clean();
      resolve({ gaveUp: true, names: [] });
    };
    const onClick = (event) => {
      event.preventDefault();
      event.stopPropagation();
      const thing = document.elementFromPoint(event.clientX, event.clientY) || current;
      if (!thing) return;
      clean();
      resolve(namesFor(thing));
    };
    document.addEventListener('mousemove', onMove, true);
    document.addEventListener('click', onClick, true);
    document.addEventListener('keydown', onKey, true);
  });

  function quote(value) {
    return '"' + String(value).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';
  }

  function safeName(value) {
    // CSS.escape keeps a class or id with odd characters from becoming code.
    return window.CSS && CSS.escape ? CSS.escape(value) : value;
  }

  function count(selector) {
    try {
      return document.querySelectorAll(selector).length;
    } catch (error) {
      return -1;   // Not a name the browser understands at all.
    }
  }

  function countByWords(tag, wanted) {
    // A name built from the words on a thing cannot be counted by the browser,
    // because "the thing whose words are these" is not plain CSS. So count it
    // here the same way the test runner reads it: same tag, same exact words.
    try {
      return Array.from(document.querySelectorAll(tag))
        .filter((one) => ((one.innerText || one.textContent || '').trim().replace(/\s+/g, ' ')) === wanted)
        .length;
    } catch (error) {
      return -1;
    }
  }

  function words(thing) {
    const found = (thing.innerText || thing.textContent || '').trim().replace(/\s+/g, ' ');
    return found.length > 60 ? '' : found;
  }

  function pathFor(thing) {
    const steps = [];
    let at = thing;
    while (at && at.nodeType === 1 && at !== document.documentElement) {
      let step = at.tagName.toLowerCase();
      if (at.id && count('#' + safeName(at.id)) === 1) {
        steps.unshift('#' + safeName(at.id));
        break;
      }
      const parent = at.parentElement;
      if (parent) {
        const alike = Array.from(parent.children).filter((one) => one.tagName === at.tagName);
        if (alike.length > 1) step += ':nth-of-type(' + (alike.indexOf(at) + 1) + ')';
      }
      steps.unshift(step);
      at = at.parentElement;
    }
    return steps.join(' > ');
  }

  function namesFor(thing) {
    const tag = thing.tagName.toLowerCase();
    const names = [];
    const add = (selector, kind, detail) => {
      if (!selector) return;
      names.push({ selector, kind, detail: detail || '', matches: count(selector) });
    };
    for (const attribute of __TEST_ATTRIBUTES__) {
      const value = thing.getAttribute(attribute);
      if (value) add('[' + attribute + '=' + quote(value) + ']', 'test-hook', attribute);
    }
    if (thing.id) add('#' + safeName(thing.id), 'id', thing.id);
    const label = thing.getAttribute('aria-label');
    const role = thing.getAttribute('role');
    if (role && label) add('[role=' + quote(role) + '][aria-label=' + quote(label) + ']', 'role', label);
    else if (label) add('[aria-label=' + quote(label) + ']', 'role', label);
    else if (role) add(tag + '[role=' + quote(role) + ']', 'role', role);
    const named = thing.getAttribute('name');
    if (named) add(tag + '[name=' + quote(named) + ']', 'name', named);
    const hint = thing.getAttribute('placeholder');
    if (hint) add(tag + '[placeholder=' + quote(hint) + ']', 'placeholder', hint);
    const shown = words(thing);
    if (shown && !thing.children.length) {
      // The test runner understands this. It means "exactly these words".
      names.push({
        selector: tag + ':text-is(' + quote(shown) + ')',
        kind: 'text',
        detail: shown,
        matches: countByWords(tag, shown),
      });
    }
    const classes = Array.from(thing.classList || []);
    for (const one of classes.slice(0, 8)) add(tag + '.' + safeName(one), 'class', one);
    if (classes.length > 1) {
      add(tag + '.' + classes.slice(0, 8).map(safeName).join('.'), 'class', classes.join(' '));
    }
    add(pathFor(thing), 'path', '');
    return {
      gaveUp: false,
      tag,
      text: shown,
      names: names.filter((one) => one.matches >= 0),
    };
  }
}

(async () => {
  const report = { fatal: '', gaveUp: false, tag: '', text: '', names: [] };
  let browser;
  try {
    browser = await chromium.launch({ headless: false });
    const page = await browser.newPage({ viewport: plan.viewport });
    await page.goto(plan.url, { waitUntil: 'load', timeout: plan.timeoutMs });
    const picked = await page.evaluate(inThePage);
    Object.assign(report, picked);
  } catch (error) {
    report.fatal = String((error && error.message) || error).slice(0, 500);
  } finally {
    if (browser) { try { await browser.close(); } catch (error) { /* already gone */ } }
  }
  process.stdout.write('<<<QA_REPORT>>>' + JSON.stringify(report));
})();
"""


def picker_script(plan: Mapping[str, Any]) -> str:
    """Build the standalone script that opens the page and waits for one click."""

    return (
        _PICKER_SCRIPT
        .replace("__PLAN__", json.dumps(dict(plan), sort_keys=True))
        .replace("__TEST_ATTRIBUTES__", json.dumps(list(TEST_ATTRIBUTES)))
    )


# ---------------------------------------------------------------------------
# Opening the page and waiting for the click
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Pick:
    """What came back from one visit to the page."""

    gave_up: bool
    tag: str
    text: str
    offered: tuple[Candidate, ...]
    thrown_away: tuple[Candidate, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "gave_up": self.gave_up,
            "tag": self.tag,
            "text": self.text,
            "offered": [item.to_dict() for item in self.offered],
            "thrown_away": [item.to_dict() for item in self.thrown_away],
        }


def check_url(config: LoadedConfig, url: str) -> str:
    """The same host rule the checks use, so picking cannot reach further."""

    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise PickError("The address must start with http:// or https://")
    if parsed.username or parsed.password:
        raise PickError("The address must not carry a user name or password")
    host = (parsed.hostname or "").lower()
    allowed = [str(item).lower() for item in config.get("qa.allow_hosts", ["127.0.0.1", "localhost", "::1"])]
    if host not in allowed:
        raise PickError(
            f"This project may not open {host or 'that address'}. "
            f"Allowed: {', '.join(allowed)}. Change qa.allow_hosts to add one."
        )
    return url


def read_report(text: str) -> Pick:
    """Turn what the page sent back into names, best first."""

    marker = "<<<QA_REPORT>>>"
    if marker not in text:
        raise PickError("The browser did not send anything back")
    try:
        report = json.loads(text.split(marker, 1)[1])
    except json.JSONDecodeError as exc:
        raise PickError(f"What the browser sent back is not valid JSON: {exc.msg}") from exc
    if not isinstance(report, Mapping):
        raise PickError("What the browser sent back is not an answer this tool understands")
    fatal = str(report.get("fatal") or "")
    if fatal:
        raise PickError(f"The browser stopped early: {fatal}")
    found = parse_candidates(report.get("names") or [])
    warned = tuple(
        item
        if item.warning or item.kind != "id"
        else Candidate(item.selector, item.kind, item.matches, item.detail, made_up_name(item.detail))
        for item in found
    )
    return Pick(
        gave_up=bool(report.get("gaveUp")),
        tag=str(report.get("tag") or "")[:40],
        text=str(report.get("text") or "")[:200],
        offered=rank(warned),
        thrown_away=rejected(warned),
    )


def pick(
    config: LoadedConfig,
    url: str,
    *,
    viewport: tuple[int, int] = (1280, 800),
    seconds: float | None = None,
    commands: CommandRunner | None = None,
) -> Pick:
    """Open a real browser window, wait for one click, and report the names."""

    check_url(config, url)
    runner = commands or CommandRunner(config)
    limit = float(config.get("execution.timeout_seconds", 180))
    wait = min(limit, float(seconds or limit))
    plan = {
        "url": url,
        "viewport": {"width": int(viewport[0]), "height": int(viewport[1])},
        "timeoutMs": int(min(wait, 60) * 1000),
    }
    base = confined_path(config.project_root, ".harness/qa/tmp", allow_missing=True, allow_control=True)
    try:
        base.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix="pick-", dir=base))
    except OSError as exc:
        raise PickError(f"Cannot use the working folder {base}: {exc}") from exc
    script = folder / "picker.js"
    script.write_text(picker_script(plan), encoding="utf-8")
    try:
        result = runner.run(
            ["node", script.relative_to(config.project_root).as_posix()], cwd=".", timeout=wait
        )
    finally:
        script.unlink(missing_ok=True)
        try:
            folder.rmdir()
        except OSError:
            pass
    if "<<<QA_REPORT>>>" not in result.stdout:
        detail = (result.stderr or result.stdout).strip()[:600]
        raise PickError(
            "The browser could not be opened. Node.js and Playwright must be installed: "
            "'npm install playwright' then 'npx playwright install chromium'. "
            + (f"It said: {detail}" if detail else "")
        )
    return read_report(result.stdout)
