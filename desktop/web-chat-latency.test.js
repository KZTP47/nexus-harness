"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const {createRequire} = require("node:module");
const {PROVIDERS, submissionScript, automationScript} = require("./web-chats");

function acknowledgement(provider, {marker = "NEXUS TRANSPORT TURN unique-turn", user = true,
  rendered = `[${marker}] Collapsed context…`, beforeCount = 0, beforeLast = ""} = {}) {
  let waited = 0;
  const prompt = `[${marker}]\n${"Full project context. ".repeat(100)}\nWho is here?`;
  const visible = {
    getBoundingClientRect: () => ({width: 40, height: 20}),
    getClientRects: () => [1], getAttribute: () => null, hasAttribute: () => false,
  };
  const bubble = {...visible, innerText: rendered};
  const composer = {...visible, innerText: prompt};
  const context = {
    document: {querySelectorAll: (selector) => {
      if (provider.users.includes(selector)) return user ? [bubble] : [];
      if (provider.composer.includes(selector)) return [composer];
      return [];
    }},
    getComputedStyle: () => ({visibility: "visible", display: "block"}),
    HTMLTextAreaElement: class {}, HTMLInputElement: class {},
    setTimeout: (callback, ms) => { waited += ms; callback(); },
  };
  return vm.runInNewContext(submissionScript(provider, prompt, {
    submittedPrompt: prompt, submittedMarker: marker, sendActivated: true,
    beforeCount: 0, beforeLast: "", beforeUserCount: beforeCount,
    beforeUserLast: beforeLast,
  }), context).then((result) => ({result, waited}));
}

for (const provider of [PROVIDERS.chatgpt, PROVIDERS.gemini, PROVIDERS.claude]) {
  test(`${provider.id}: a collapsed marked user turn acknowledges in 100 ms`, async () => {
    const {result, waited} = await acknowledgement(provider);
    assert.equal(result.submissionState, "acknowledged");
    assert.equal(result.needsTrustedEnter, false);
    assert.equal(waited, 100);
  });

  test(`${provider.id}: draft, wrong marker and unchanged old turn cannot acknowledge`, async () => {
    for (const options of [
      {user: false}, {rendered: "NEXUS TRANSPORT TURN other-turn"},
      {beforeCount: 1, beforeLast: "[NEXUS TRANSPORT TURN unique-turn] Collapsed context…"},
    ]) {
      const {result, waited} = await acknowledgement(provider, options);
      assert.equal(result.submissionState, "outcome_unknown");
      assert.equal(result.needsTrustedEnter, false);
      assert.equal(waited, 8000);
    }
  });
}

test("the DOM submission path also acknowledges collapsed markers without accepting drafts", async () => {
  for (const renderUser of [true, false]) {
    let waited = 0;
    let sent = false;
    const provider = PROVIDERS.gemini;
    const marker = "NEXUS TRANSPORT TURN portable-dom-turn";
    const prompt = `[${marker}] ${"Context ".repeat(100)} End of context`;
    const visible = {
      getBoundingClientRect: () => ({width: 40, height: 20}),
      getAttribute: () => null, hasAttribute: () => false,
    };
    const composer = {...visible, innerText: "", focus: () => {}, dispatchEvent: () => {}};
    const send = {...visible, tagName: "BUTTON", matches: () => true,
      click: () => { sent = true; }};
    const context = {
      document: {
        execCommand: () => { composer.innerText = prompt; return true; },
        querySelectorAll: (selector) => {
          if (provider.composer.includes(selector)) return [composer];
          if (provider.send.includes(selector)) return [send];
          if (provider.users.includes(selector) && sent && renderUser) {
            return [{...visible, innerText: `[${marker}] Collapsed…`}];
          }
          return [];
        },
      },
      location: {pathname: "/app/arbitrary"},
      getComputedStyle: () => ({visibility: "visible", display: "block"}),
      HTMLTextAreaElement: class {}, HTMLInputElement: class {}, InputEvent: class {}, Event: class {},
      setTimeout: (callback, ms) => { waited += ms; callback(); },
    };
    const result = await vm.runInNewContext(automationScript(provider, prompt, marker), context);
    assert.equal(result.ok, true);
    assert.equal(result.submissionState, renderUser ? undefined : "outcome_unknown");
    assert.equal(waited, renderUser ? 200 : 8100);
  }
});

async function timedReply(stateAt = () => ({answer: "Finished", stopping: false})) {
  let now = 0;
  const filename = require.resolve("./web-chats");
  const context = {
    require: createRequire(filename), module: {exports: {}},
    Date: {now: () => now},
    setTimeout: (callback, ms) => { now += ms; callback(); return 1; },
  };
  vm.runInNewContext(fs.readFileSync(filename, "utf8"), context);
  const {WebChatManager} = context.module.exports;
  const manager = new WebChatManager({
    electron: {}, owner: null, readSettings: () => ({}), writeSettings: () => {},
  });
  manager.connections.set("portable-route", {provider: "chatgpt"});
  manager.viewFor = () => ({webContents: {
    focus: () => {},
    executeJavaScript: async (script) => {
      if (script.includes("const prompt =")) {
        now += 400;
        return {ok: true, submissionState: "acknowledged"};
      }
      return {changed: true, ...stateAt(now - 1600)};
    },
  }});
  manager.waitForLoad = async () => { now += 1200; };
  manager.preflightConnectionPage = () => "";
  manager.attachFiles = async () => {};
  manager.preSubmitOperation = async (operation) => operation;
  manager.rememberConnectionPage = () => {};
  manager.showCreatedConversationInOpenShells = () => {};
  return manager.askNow("portable-route", "Who is here?", [], "arbitrary-chat");
}

test("default reply capture takes 2050 ms with the full 1800 ms stability window", async () => {
  const result = await timedReply();
  assert.equal(result.answer, "Finished");
  assert.equal(result.milliseconds, 2050); // Previous 900 ms polling took 2700 ms.
  assert.equal(result.diagnostics.capture_tail_ms, 1800);
  assert.equal(result.diagnostics.first_reply_ms, 250);
  assert.equal(result.diagnostics.prepare_ms, 1200);
  assert.equal(result.diagnostics.submit_ms, 400);
  assert.equal(result.diagnostics.browser_total_ms, 3650);
});

test("unchanging partial text waits for Stop to clear, however long generation pauses", async () => {
  const result = await timedReply((elapsed) => ({
    answer: elapsed < 4000 ? "Partial" : "Finished", stopping: elapsed < 7000,
  }));
  assert.equal(result.answer, "Finished");
  assert.ok(result.milliseconds >= 7000);
});

test("a vanished and remounted reply must establish stability again", async () => {
  const result = await timedReply((elapsed) => ({
    answer: "Finished", changed: elapsed !== 1750, stopping: false,
  }));
  assert.equal(result.milliseconds, 3800);
  assert.equal(result.diagnostics.capture_tail_ms, 1800);
});

test("phase details reject unknown versions and do not render arbitrary metadata", () => {
  const source = fs.readFileSync(require.resolve("../src/our_harness/ui/app.js"), "utf8");
  const start = source.indexOf("function relayTimingDetails(");
  const end = source.indexOf("\n}", source.indexOf("function prettyTime(", start)) + 2;
  const context = {};
  vm.runInNewContext(source.slice(start, end), context);
  assert.equal(context.relayTimingDetails({timing_version: 2, queue_ms: 1200}), "");
  assert.equal(context.relayTimingDetails({timing_version: 1, queue_ms: 0,
    submit_ms: 100, first_reply_ms: 2000, token: "secret", prepare_ms: "bad"}),
  "Queue: 0 ms | Send acknowledgement: 100 ms | First visible reply: 2.0 seconds");
});
