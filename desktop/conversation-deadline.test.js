"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const {WebChatManager, PROVIDERS, WebChatTurnError} = require("./web-chats");

function fixture(t, options = {}) {
  let now = 1000000;
  let polls = 0;
  let stops = 0;
  t.mock.method(Date, "now", () => now);
  const contents = {
    isDestroyed: () => false,
    getURL: () => PROVIDERS.gemini.home,
    executeJavaScript: async (script) => {
      if (script.includes("const prompt =")) return {ok: true, beforeCount: 0, beforeLast: ""};
      if (script.includes("const began =")) {
        polls += 1;
        now += polls <= 3 ? 60000 : 2000;
        return {answer: polls <= 3 ? "Still reasoning" : "Finished answer", changed: true,
          stopping: polls <= 3, markerFound: true};
      }
      stops += 1;
      return true;
    },
  };
  const manager = new WebChatManager({electron: {}, owner: null,
    readSettings: () => ({}), writeSettings: () => {},
    shellPage: "file:///web-chat.html", shellPreload: "web-chat-shell-preload.js",
    answerPollMs: 1, ...options});
  manager.connections.set("gemini-example", {id: "gemini-example", provider: "gemini",
    title: "Gemini", url: PROVIDERS.gemini.home});
  manager.viewFor = () => ({webContents: contents});
  manager.waitForLoad = async () => {};
  manager.attachFiles = async () => {};
  manager.rememberConnectionPage = () => false;
  manager.showCreatedConversationInOpenShells = () => {};
  return {manager, counts: () => ({polls, stops}), now: () => now};
}

test("a healthy web reply continues beyond the former 165-second cutoff", async (t) => {
  const {manager, counts} = fixture(t);
  const result = await manager.ask("gemini-example", "Think carefully");
  assert.equal(result.answer, "Finished answer");
  assert.ok(result.milliseconds > 165000);
  assert.equal(counts().stops, 0);
});

test("the bridge deadline survives queuing and still rejects incomplete text", async (t) => {
  const {manager, counts, now} = fixture(t);
  await assert.rejects(manager.ask("gemini-example", "Think carefully", [], "", false, now() + 125000),
    (error) => error instanceof WebChatTurnError && error.failureCode === "reply_completion_timeout");
  assert.ok(counts().stops >= 1);
});

test("an explicitly shorter answer limit remains effective", async (t) => {
  const {manager, counts} = fixture(t, {answerDeadlineMs: 120000});
  await assert.rejects(manager.ask("gemini-example", "Think carefully"),
    (error) => error instanceof WebChatTurnError && error.failureCode === "reply_completion_timeout");
  assert.ok(counts().stops >= 1);
});
