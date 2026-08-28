"use strict";

// What the window may hand to the rest of the machine.

const test = require("node:test");
const assert = require("node:assert");

const { isWebAddress, isLoopbackUrl } = require("./server");
const { attachGuards } = require("./guards");

test("only a web address may be handed to the system", () => {
  for (const good of ["http://example.com/a", "https://example.com/a"]) {
    assert.equal(isWebAddress(good), true, good);
  }
  // Handing any of these to the system would start a program or open a share
  // on the user's machine, with nothing asked and nothing shown.
  const shareLink = ["\\", "\\", "elsewhere", "\\", "thing.exe"].join("");
  for (const bad of [
    "file:///settings/list.ini",
    "file://elsewhere/thing.exe",
    shareLink,
    "mailto:someone@example.com",
    "javascript:alert(1)",
    "ms-settings:privacy",
    "not an address at all",
  ]) {
    assert.equal(isWebAddress(bad), false, bad);
  }
});

test("a page on this machine is still recognised", () => {
  assert.equal(isLoopbackUrl("http://127.0.0.1:8765/"), true);
  assert.equal(isLoopbackUrl("http://localhost:8765/"), true);
  assert.equal(isLoopbackUrl("http://example.com/"), false);
});

// A stand-in for the window's page, which records what it was told.
function fakeContents() {
  const handlers = new Map();
  return {
    opened: [],
    blocked: [],
    setWindowOpenHandler(handler) { this.openHandler = handler; },
    on(name, handler) { handlers.set(name, handler); },
    fire(name, url) {
      const event = { prevented: false, preventDefault() { this.prevented = true; } };
      handlers.get(name)(event, url);
      if (event.prevented) this.blocked.push([name, url]);
      return event.prevented;
    },
    has(name) { return handlers.has(name); },
  };
}

test("the window refuses to go anywhere but this machine", () => {
  const contents = fakeContents();
  attachGuards(contents, { allowedTarget: (url) => isLoopbackUrl(url) });
  for (const moment of ["will-navigate", "will-redirect"]) {
    assert.equal(contents.has(moment), true, moment + " is guarded");
    assert.equal(contents.fire(moment, "http://127.0.0.1:8765/x"), false, "own machine is allowed");
    assert.equal(contents.fire(moment, "http://example.com/evil"), true, "elsewhere is refused");
    assert.equal(contents.fire(moment, "file:///settings/list.ini"), true, "a file is refused");
  }
});

test("a new window is never opened, and only a web address is handed on", () => {
  const contents = fakeContents();
  const opened = [];
  attachGuards(contents, { allowedTarget: () => true, openExternally: (url) => opened.push(url) });
  assert.deepEqual(contents.openHandler({ url: "https://example.com/docs" }), { action: "deny" });
  assert.deepEqual(opened, ["https://example.com/docs"]);
  contents.openHandler({ url: "file:///settings/list.ini" });
  contents.openHandler({ url: "mailto:someone@example.com" });
  contents.openHandler({ url: "http://127.0.0.1:8765/" });
  assert.deepEqual(opened, ["https://example.com/docs"], "nothing else reached the system");
});

test("the rules refuse to be attached without an answer to give", () => {
  // Left like this the window would refuse its own pages and look broken.
  assert.throws(() => attachGuards(fakeContents(), { openExternally() {} }), /allowedTarget/);
});

test("the real app hands in its own answer", () => {
  const main = require("node:fs").readFileSync(require("node:path").join(__dirname, "main.js"), "utf8");
  assert.match(main, /attachGuards\(window\.webContents, \{\s*allowedTarget,/,
    "main.js must give attachGuards the rule it uses");
});

// ---------------------------------------------------------------------------
// The app carries its own copy of the harness, so it can be older than the
// settings it is reading. When that happened, the app showed three guesses -
// Python missing, wrong folder, bad download - and every one of them was wrong.
// Somebody spent the evening looking in three wrong places.
// ---------------------------------------------------------------------------

const { onlyOnce, isHarnessVersionMismatch, whyItReallyIs } = require("./guards");

test("an old copy of the harness is named as the reason", () => {
  const said = whyItReallyIs(
    "error: providers.gemini.kind must name a supported provider");
  assert.match(said, /older than your settings/);
  // It says Python is fine, which is the opposite of the guess it replaces.
  assert.match(said, /Nothing is wrong with Python/);
  assert.doesNotMatch(said, /Python 3\.11 or newer is not installed/);
});

test("only version-shaped startup errors offer the automatic repair", () => {
  assert.strictEqual(isHarnessVersionMismatch("error: Unknown config key: persistent_memory"), true);
  assert.strictEqual(isHarnessVersionMismatch("No Python was found on this machine"), false);
  assert.strictEqual(isHarnessVersionMismatch("project has not been told to trust"), false);
});

test("a repairable mismatch explains what the repair button will do", () => {
  const said = whyItReallyIs(
    "error: Unknown config key: persistent_memory", { canRepair: true }
  );
  assert.match(said, /Choose Fix and start/);
  assert.match(said, /use the newer harness code in this project/);
  assert.match(said, /recover automatically on later starts/);
});

test("an installed mismatch tells the truth about its immutable private bundle", () => {
  const said = whyItReallyIs("error: Unknown config key: newer", { installed: true });
  assert.match(said, /will not mix project source/);
  assert.match(said, /newer signed Nexus release/);
  assert.doesNotMatch(said, /python scripts\/harness\.py/);
});

test("an untrusted settings file is named as the reason", () => {
  const said = whyItReallyIs(
    "error: project.test_commands is set in a settings file this machine has "
    + "not been told to trust");
  assert.match(said, /deliberate stop/);
});

test("anything else falls back to the guesses", () => {
  assert.strictEqual(whyItReallyIs("something nobody has seen before"), "");
});

test("the same sentence twelve times is said once", () => {
  const over = Array(12).fill(
    "error: providers.gemini.kind must name a supported provider.").join(" ");
  const said = onlyOnce(over);
  assert.strictEqual(
    said.split("must name a supported provider").length - 1, 1,
    "the page opened with a paragraph of the same words repeating");
});
