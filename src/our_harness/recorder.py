"""Clicking through your own app and getting a written check out of it.

The fastest way to a first test is not to write one. It is to do the thing you
already do by hand, once, while the harness watches.

A browser opens. Every click, every box you type in, every choice you make is
written down as a step, using the best name for the thing you touched: the same
order the picker uses, test attributes first and where-it-sits last. Press the
Done button in the bar at the top and the steps come back.

Two rules keep the result honest:

- Nothing is guessed. If a thing has no usable name, the step says so plainly
  rather than inventing one that would break tomorrow.
- What you type is written down as you typed it, except in a password box,
  where the value is replaced with a setting name so a real password never
  ends up in a file.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .config import LoadedConfig
from .execution import CommandRunner
from .models import HarnessError
from .safety import confined_path
from .selectors import TEST_ATTRIBUTES, PickError, check_url

MAX_STEPS = 60
MAX_TEXT = 500
# What a recorded action may be. Anything else from the page is refused.
KNOWN_ACTIONS = ("click", "type", "press", "choose", "expect_text", "expect_visible")


class RecordError(HarnessError):
    """A problem while recording, or with what the page sent back."""


@dataclass(frozen=True)
class Recording:
    """What was done, as steps a check can hold."""

    url: str
    steps: tuple[dict[str, Any], ...]
    skipped: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"url": self.url, "steps": [dict(step) for step in self.steps], "skipped": list(self.skipped)}

    def case(self, case_id: str = "recorded-workflow", title: str = "") -> dict[str, Any]:
        """The whole thing as a check, ready to be written into a suite."""

        return {
            "id": case_id,
            "title": title or "A workflow somebody did by hand",
            "kind": "browser",
            "tags": ["ui", "recorded"],
            "url": self.url,
            "steps": [dict(step) for step in self.steps],
            "expect": {"max_console_errors": 0, "max_page_errors": 0},
        }


def read_actions(value: object, url: str) -> Recording:
    """Turn what the page wrote down into steps, refusing anything odd."""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise RecordError("The page did not send back a list of actions")
    if len(value) > MAX_STEPS * 2:
        raise RecordError(f"More than {MAX_STEPS * 2} actions came back, which is more than one check should hold")
    steps: list[dict[str, Any]] = []
    skipped: list[str] = []
    for number, item in enumerate(value, start=1):
        if not isinstance(item, Mapping):
            raise RecordError(f"Action {number} is not an object")
        action = str(item.get("do") or "")
        if action not in KNOWN_ACTIONS:
            raise RecordError(f"Action {number} is a kind this tool does not know: {action or 'nothing'}")
        target = str(item.get("target") or "").strip()
        if not target:
            # Nothing on the page named it, so there is no honest step to write.
            skipped.append(
                f"Action {number} ({action}) was left out: nothing on the page names that thing on its own. "
                "Ask for a data-testid to be added to it."
            )
            continue
        if len(target) > MAX_TEXT:
            raise RecordError(f"Action {number} has a name longer than {MAX_TEXT} characters")
        step: dict[str, Any] = {"do": action, "target": target}
        note = str(item.get("note") or "").strip()
        if note:
            step["note"] = note[:200]
        for name in ("text", "key", "value"):
            if name in item:
                found = str(item.get(name) or "")
                if len(found) > MAX_TEXT:
                    found = found[:MAX_TEXT]
                step[name] = found
        if action == "expect_text" and not step.get("text"):
            skipped.append(f"Action {number} was left out: there were no words to wait for")
            continue
        steps.append(step)
        if len(steps) >= MAX_STEPS:
            skipped.append(f"Recording stopped at {MAX_STEPS} steps. Split the workflow into shorter checks.")
            break
    if not steps:
        raise RecordError(
            "Nothing was recorded. Click something in the browser window before pressing Done."
        )
    return Recording(url=url, steps=tuple(steps), skipped=tuple(skipped))


def record(
    config: LoadedConfig,
    url: str,
    *,
    viewport: tuple[int, int] = (1280, 800),
    seconds: float | None = None,
    commands: CommandRunner | None = None,
) -> Recording:
    """Open a browser, watch what a person does, and write it down."""

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
        folder = Path(tempfile.mkdtemp(prefix="record-", dir=base))
    except OSError as exc:
        raise RecordError(f"Cannot use the working folder {base}: {exc}") from exc
    script = folder / "recorder.js"
    script.write_text(recorder_script(plan), encoding="utf-8")
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
    marker = "<<<QA_REPORT>>>"
    if marker not in result.stdout:
        detail = (result.stderr or result.stdout).strip()[:600]
        raise RecordError(
            "The browser could not be opened. Node.js and Playwright must be installed: "
            "'npm install playwright' then 'npx playwright install chromium'. "
            + (f"It said: {detail}" if detail else "")
        )
    try:
        report = json.loads(result.stdout.split(marker, 1)[1])
    except json.JSONDecodeError as exc:
        raise RecordError(f"What the browser sent back is not valid JSON: {exc.msg}") from exc
    fatal = str(report.get("fatal") or "")
    if fatal:
        raise RecordError(f"The browser stopped early: {fatal}")
    return read_actions(report.get("actions") or [], url)


# The naming helpers are the picker's, word for word, so a recorded step and a
# picked name are always chosen the same way.
_NAMING = r"""
  function quote(value) {
    return '"' + String(value).replace(/\\/g, '\\\\').replace(/"/g, '\\"') + '"';
  }

  function safeName(value) {
    return window.CSS && CSS.escape ? CSS.escape(value) : value;
  }

  function count(selector) {
    try {
      return document.querySelectorAll(selector).length;
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

  function bestName(thing) {
    // Best first, and only a name that matches exactly one thing. This is the
    // same order the picker offers, so nothing surprising ends up in a check.
    const tries = [];
    for (const attribute of __TEST_ATTRIBUTES__) {
      const value = thing.getAttribute(attribute);
      if (value) tries.push('[' + attribute + '=' + quote(value) + ']');
    }
    if (thing.id) tries.push('#' + safeName(thing.id));
    const tag = thing.tagName.toLowerCase();
    const label = thing.getAttribute('aria-label');
    const role = thing.getAttribute('role');
    if (role && label) tries.push('[role=' + quote(role) + '][aria-label=' + quote(label) + ']');
    else if (label) tries.push('[aria-label=' + quote(label) + ']');
    else if (role) tries.push(tag + '[role=' + quote(role) + ']');
    const named = thing.getAttribute('name');
    if (named) tries.push(tag + '[name=' + quote(named) + ']');
    const hint = thing.getAttribute('placeholder');
    if (hint) tries.push(tag + '[placeholder=' + quote(hint) + ']');
    for (const one of Array.from(thing.classList || []).slice(0, 8)) {
      tries.push(tag + '.' + safeName(one));
    }
    tries.push(pathFor(thing));
    for (const candidate of tries) {
      if (count(candidate) === 1) return candidate;
    }
    return '';
  }
"""

_RECORDER_SCRIPT = r"""
// Written by Our Harness to watch one workflow being done by hand. Deleted after.
const { chromium } = require('playwright');
const plan = __PLAN__;

function inThePage() {
  return new Promise((resolve) => {
    const done = [];
    const wasSecret = new WeakSet();
    const looksSecret = (thing) => {
      if (!thing || thing.tagName !== 'INPUT') return false;
      if (thing.type === 'password' || wasSecret.has(thing)) return true;
      const said = [
        thing.getAttribute('name') || '',
        thing.getAttribute('id') || '',
        thing.getAttribute('autocomplete') || '',
        thing.getAttribute('placeholder') || '',
        thing.getAttribute('aria-label') || '',
      ].join(' ').toLowerCase();
      return /pass(word|wd)?|passphrase|current-password|new-password|otp|one-time/.test(said);
    };
    const bar = document.createElement('div');
    // Along the bottom, not the top: a page's own buttons are usually near the
    // top, and a bar over them would make the very thing you came to record
    // impossible to click.
    bar.style.cssText = [
      'position:fixed', 'right:16px', 'bottom:16px',
      'z-index:2147483647', 'background:#10233a', 'color:#ffffff',
      'padding:8px 14px', 'border-radius:6px', 'font:14px system-ui,sans-serif',
      'box-shadow:0 4px 14px rgba(0,0,0,0.4)', 'display:flex', 'gap:10px', 'align-items:center',
      'max-width:calc(100vw - 32px)', 'flex-wrap:wrap',
    ].join(';');
    const label = document.createElement('span');
    label.textContent = 'Recording. Do the thing you want to check, then press Done.';
    const counter = document.createElement('strong');
    counter.textContent = '0 steps';
    const stop = document.createElement('button');
    stop.textContent = 'Done';
    stop.style.cssText = 'font:14px system-ui,sans-serif;padding:4px 10px;cursor:pointer';
    bar.append(label, counter, stop);
    document.body.appendChild(bar);

    const ours = (thing) => bar.contains(thing);
    const remember = (step) => {
      done.push(step);
      counter.textContent = done.length + (done.length === 1 ? ' step' : ' steps');
    };

    const onClick = (event) => {
      const thing = event.target;
      if (!thing || ours(thing)) return;
      // A tick box, a radio button and a dropdown all report themselves when
      // they change. Writing the click down as well would put the same action
      // in the check twice.
      const tag = thing.tagName;
      if (tag === 'OPTION' || tag === 'SELECT') return;
      if (tag === 'INPUT' && (thing.type === 'checkbox' || thing.type === 'radio')) return;
      remember({ do: 'click', target: bestName(thing), note: 'Press ' + (words(thing) || tag.toLowerCase()) });
    };
    const onChange = (event) => {
      const thing = event.target;
      if (!thing || ours(thing)) return;
      const tag = thing.tagName.toLowerCase();
      if (tag === 'select') {
        remember({ do: 'choose', target: bestName(thing), value: thing.value, note: 'Choose ' + thing.value });
        return;
      }
      if (thing.type === 'checkbox' || thing.type === 'radio') {
        remember({ do: 'click', target: bestName(thing), note: (thing.checked ? 'Tick ' : 'Untick ') + (thing.name || tag) });
        return;
      }
      // A page with a "show password" button turns the box into an ordinary
      // one before you tab away, so asking what kind it is right now is not
      // enough. A box that was ever a password box, or that is named like
      // one, or that the browser fills with a password, counts as secret for
      // as long as the page is open.
      const secret = looksSecret(thing);
      remember({
        do: 'type',
        target: bestName(thing),
        // A real password never goes into a file. The step asks for a saved
        // setting instead, which the person fills in once.
        text: secret ? '${env.PASSWORD}' : String(thing.value || ''),
        note: secret ? 'Type the password from your saved settings' : 'Type in ' + (thing.name || tag),
      });
    };
    const onKey = (event) => {
      if (event.key !== 'Enter' || ours(event.target)) return;
      remember({ do: 'press', target: bestName(event.target), key: 'Enter', note: 'Press Enter' });
    };

    const markSecret = (event) => {
      const thing = event.target;
      if (thing && thing.tagName === 'INPUT' && thing.type === 'password') wasSecret.add(thing);
    };
    document.addEventListener('click', onClick, true);
    document.addEventListener('change', onChange, true);
    document.addEventListener('keydown', onKey, true);
    // Watch for password boxes as they are used, before anything can change
    // what kind of box they say they are.
    for (const moment of ['focusin', 'input', 'keydown', 'mousedown']) {
      document.addEventListener(moment, markSecret, true);
    }
    for (const box of document.querySelectorAll('input[type=password]')) wasSecret.add(box);

    stop.addEventListener('click', (event) => {
      event.preventDefault();
      event.stopPropagation();
      document.removeEventListener('click', onClick, true);
      document.removeEventListener('change', onChange, true);
      document.removeEventListener('keydown', onKey, true);
      bar.remove();
      resolve(done);
    });
  });

__NAMING__
}

(async () => {
  const report = { fatal: '', actions: [] };
  let browser;
  try {
    browser = await chromium.launch({ headless: false });
    const page = await browser.newPage({ viewport: plan.viewport });
    await page.goto(plan.url, { waitUntil: 'load', timeout: plan.timeoutMs });
    report.actions = await page.evaluate(inThePage);
  } catch (error) {
    report.fatal = String((error && error.message) || error).slice(0, 500);
  } finally {
    if (browser) { try { await browser.close(); } catch (error) { /* already gone */ } }
  }
  process.stdout.write('<<<QA_REPORT>>>' + JSON.stringify(report));
})();
"""


def recorder_script(plan: Mapping[str, Any]) -> str:
    """Build the standalone script that watches one workflow being done."""

    return (
        _RECORDER_SCRIPT
        .replace("__PLAN__", json.dumps(dict(plan), sort_keys=True))
        .replace("__NAMING__", _NAMING)
        .replace("__TEST_ATTRIBUTES__", json.dumps(list(TEST_ATTRIBUTES)))
    )
