"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const {WebChatManager} = require("./web-chats");
const {ExternalPageContents} = require("./external-browser");

function fixture(stage, initialUrl = "https://claude.ai/chat/original") {
  let closed = false, url = initialUrl;
  let clicks = 0, insertions = 0, foreignOperations = 0, strings = 0;
  const page = {isClosed: () => closed, url: () => url, close: async () => {closed = true;},
    keyboard: {insertText: async () => {insertions += 1;}},
    mouse: {click: async () => {clicks += 1; if (stage === "created") url = "https://claude.ai/chat/newly-created";}},
    evaluate: async (script, args) => {
      if (typeof script === "function") {
        if (!args?.expected) {
          if (stage === "selection") closed = true;
          if (stage === "url") url = "https://claude.ai/chat/unrelated";
          return true;
        }
        return {fingerprint: "ready", x: 1, y: 1};
      }
      strings += 1;
      if (strings === 1) {
        if (stage === "baseline") closed = true;
        return {needsTrustedInput: true};
      }
      if (strings === 2) return {ok: true, submissionState: stage === "unknown" ? "outcome_unknown" : "acknowledged"};
      if (stage === "created") return {changed: true, answer: "Created conversation answer", stopping: false};
      closed = true;
      return {changed: true, answer: "not attributable after closure"};
    }};
  const other = {isClosed: () => false, url: () => "https://claude.ai/chat/other", title: async () => "Other",
    on: () => {}, evaluate: async () => {foreignOperations += 1;},
    keyboard: {insertText: async () => {foreignOperations += 1;}},
    mouse: {click: async () => {foreignOperations += 1;}}};
  const contents = Object.create(ExternalPageContents.prototype);
  Object.assign(contents, {page, ready: Promise.resolve(page), closed: false, loading: false,
    url, title: "Original", boundPages: new WeakSet(),
    transport: {provider: {label: "Claude"}, currentProviderPage: async () => {foreignOperations += 1; return other;}}});
  contents.focus = () => {};
  contents.keepInBackground = async () => {};
  const manager = new WebChatManager({electron: {}, owner: null,
    readSettings: () => ({}), writeSettings: () => {}, shellPage: "file:///fixture", shellPreload: "none"});
  manager.connections.set("claude-fixture", {id: "claude-fixture", provider: "claude", url, threads: {}});
  const view = {external: true, webContents: contents};
  manager.viewFor = () => {manager.views.set("claude-fixture", view); return view;};
  manager.waitForProviderReady = async () => {};
  manager.attachFiles = async () => {};
  manager.answerPollMs = 1;
  manager.answerSettleMs = 1;
  return {manager, contents, page, other, counts: () => ({clicks, insertions, foreignOperations})};
}

test("generic page drift before activation cannot receive text or Send", async () => {
  const {manager, counts} = fixture("url", "https://claude.ai/new");
  await assert.rejects(manager.ask("claude-fixture", "Private goal", [], "", true), /conversation changed/);
  assert.deepEqual(counts(), {clicks: 0, insertions: 0, foreignOperations: 0});
});

test("generic page may become its newly created conversation after activation", async () => {
  const {manager, counts} = fixture("created", "https://claude.ai/new");
  const answer = await manager.ask("claude-fixture", "Create the conversation", [], "", true);
  assert.equal(answer.answer, "Created conversation answer");
  assert.deepEqual(counts(), {clicks: 1, insertions: 1, foreignOperations: 0});
});

test("deadline at poll entry keeps deterministic accepted and unknown diagnoses", async () => {
  for (const stage of ["reply", "unknown"]) {
    const {manager} = fixture(stage);
    const original = manager.preSubmitOperation.bind(manager);
    manager.preSubmitOperation = (operation, active, deadline, message) => {
      if (active?.phase === "submitted") {
        // Consume the already-created guarded operation just as the real race
        // does; force the boundary that previously lost the relay diagnosis.
        Promise.resolve(operation).catch(() => {});
        const error = new Error("deadline at poll entry");
        error.code = "NEXUS_WEB_CHAT_PRE_SUBMIT_TIMEOUT";
        return Promise.reject(error);
      }
      return original(operation, active, deadline, message);
    };
    await assert.rejects(manager.ask("claude-fixture", "Goal", [], "", true), error =>
      error.deliveryState === (stage === "unknown" ? "unknown" : "accepted")
      && error.failureCode === (stage === "unknown" ? "turn_match_unknown" : "reply_completion_timeout"));
  }
});

for (const stage of ["baseline", "selection", "reply", "url"]) {
  test(`active relay pins page through ${stage} and never adopts another conversation`, async () => {
    const {manager, contents, counts} = fixture(stage);
    await assert.rejects(manager.ask("claude-fixture", "Private original task", [], "", true, Date.now() + 2000),
      error => error.deliveryState === (stage === "reply" ? "accepted" : "unknown"));
    assert.equal(counts().foreignOperations, 0);
    assert.equal(counts().clicks, stage === "reply" ? 1 : 0);
    if (stage !== "reply") assert.equal(counts().insertions, 0);
    assert.equal(contents.turnPagePin, null);
  });
}

test("pin acquisition never adopts; idle recovery and same-page SPA navigation remain available", async () => {
  const {contents, page, other, counts} = fixture("baseline");
  const release = contents.pinTurnPage();
  page.url = () => "https://claude.ai/chat/created-by-current-turn";
  assert.equal(await contents.pageForOperation(), page);
  assert.equal(contents.getURL(), page.url());
  release();
  const releaseNew = contents.pinTurnPage();
  release(); // A stale release must not clear the newer pin.
  assert.ok(contents.turnPagePin);
  releaseNew();
  page.isClosed = () => true;
  assert.throws(() => contents.pinTurnPage(), /page changed/);
  assert.equal(counts().foreignOperations, 0);
  assert.equal(await contents.pageForOperation(), other);
  assert.equal(counts().foreignOperations, 1);
  const recovered = contents.pinTurnPage();
  assert.equal(await contents.pageForOperation(), other);
  recovered();
});
