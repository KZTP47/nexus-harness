"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {chromium} = require("playwright-core");
const {answerScript} = require("./web-chats");
const vm = require("node:vm");
const runtime = path.join(__dirname, "build-output/win-unpacked/resources/runtime");
const manifestPath = path.join(runtime, "NEXUS_RUNTIME.json");

test("historical action payloads are disclosed in details, never shown as delivery claims", () => {
  const source = fs.readFileSync(path.join(__dirname, "../src/our_harness/ui/app.js"), "utf8");
  const make = (tag, cls, text) => ({tag, text, children: [], append(...children) {this.children.push(...children);}});
  const context = vm.createContext({make});
  vm.runInContext(source.slice(source.indexOf("function appendChatText("), source.indexOf("const chatPhaseNames")), context);
  const raw = 'JSON\n{"action":"complete","changes":[{"content":"unfinished';
  const bubble = make('div');
  context.appendChatText(bubble, raw, {kind: 'long_horizon_agent_event'});
  assert.match(bubble.children[0].text, /not a delivery receipt/);
  assert.equal(bubble.children[1].tag, 'details');
  assert.equal(bubble.children[1].children[1].text, raw);
  const user = make('div');
  context.appendChatText(user, raw, {kind: 'long_horizon_user_event'});
  assert.equal(user.children[0].text, raw);
});

test("Check status uses the bound read-only action without a steering request", async () => {
  const source = fs.readFileSync(path.join(__dirname, "../src/our_harness/ui/app.js"), "utf8");
  const calls = [], messages = [];
  const context = vm.createContext({
    chatLongGoalContext: () => ({goal: {goal_id: "portable-goal"}, problem: ""}),
    swarmChatRuntimeKey: () => "portable-chat",
    chatGoalBinding: () => ({chat_id: "portable-chat", project_id: "arbitrary-project"}),
    request: async (url, options) => { calls.push({url, ...JSON.parse(options.body)});
      return {goal: {status_response: "Provider still waiting; work unchanged."}}; },
    sayInRuntimeChat: (key, text) => messages.push({key, text}),
  });
  vm.runInContext(source.slice(source.indexOf("async function checkChatGoalStatus("), source.indexOf("async function controlChatGoal(")), context);
  await vm.runInContext("checkChatGoalStatus('arbitrary-agent')", context);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].action, "status");
  assert.equal(calls[0].payload.chat_id, "portable-chat");
  assert.equal(messages[0].text, "Provider still waiting; work unchanged.");
});

test("rendered JSON preserves source bytes without accepting snippets from prose", {
  skip: !fs.existsSync(manifestPath) && !process.env.NEXUS_TEST_CHROMIUM,
}, async () => {
  const manifest = fs.existsSync(manifestPath) ? JSON.parse(fs.readFileSync(manifestPath, "utf8")) : {};
  const browser = await chromium.launch({headless: true,
    executablePath: process.env.NEXUS_TEST_CHROMIUM || path.join(runtime, "playwright", manifest.playwright.chromium_executable)});
  try {
    const page = await browser.newPage();
    const provider = {replies: [".reply"], users: [], stop: [], errors: []};
    const began = {beforeCount: 0, beforeLast: "", beforeUserCount: 0, beforeUserLast: ""};
    const raw = JSON.stringify({action: "work", changes: [{path: "arbitrary.html", content: '<html>\n<style>.a { color: red; }</style>\n<div title="one">*_ & <b>two</b></div>\n</html>'}]});
    await page.setContent('<div class="reply">JSON<button>Copy code</button><pre><code></code></pre></div>');
    await page.locator("code").evaluate((one, raw) => {
      // Highlighting can split a JSON string across layout blocks. textContent
      // preserves its exact characters; innerText invents line breaks.
      for (const piece of [raw.slice(0, 30), raw.slice(30)]) {
        const span = document.createElement("span"); span.style.display = "block";
        span.textContent = piece; one.append(span);
      }
    }, raw);
    const result = await page.evaluate(answerScript(provider, began));
    assert.equal(result.answer, raw);
    assert.equal(JSON.parse(result.answer).changes[0].content, JSON.parse(raw).changes[0].content);
    const broken = '{"action":"complete","changes":[{"content":"raw\nnewline"}]}';
    await page.locator("code").evaluate((one, value) => {
      one.textContent = "";
      for (const piece of [value.slice(0, 12), value.slice(12)]) {
        const span = document.createElement("span"); span.style.display = "block";
        span.textContent = piece; one.append(span);
      }
    }, broken);
    assert.equal((await page.evaluate(answerScript(provider, began))).answer, broken);
    await page.locator("code").evaluate((one, value) => {one.textContent = value;}, raw);
    await page.locator(".reply").evaluate(one => one.prepend(document.createTextNode("Example only; do not execute this. ")));
    const prose = await page.evaluate(answerScript(provider, began));
    assert.ok(prose.answer.startsWith("Example only"));
    assert.throws(() => JSON.parse(prose.answer));
    await page.locator(".reply").evaluate(one => one.append(one.querySelector("pre").cloneNode(true)));
    const multiple = await page.evaluate(answerScript(provider, began));
    assert.notEqual(multiple.answer, raw);
  } finally { await browser.close(); }
});
