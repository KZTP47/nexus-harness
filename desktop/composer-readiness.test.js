"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const {chromium} = require("playwright-core");

const ui = path.join(__dirname, "../src/our_harness/ui");
const runtime = path.join(__dirname, "build-output/win-unpacked/resources/runtime");

// The complete production renderer runs here. Only transport is controlled:
// unresolved requests cannot accidentally make the input test pass by finishing.
async function installFixture(page, wire = true) {
  await page.evaluate(wire => {
    window.fixtureRequests = [];
    window.fixtureErrors = [];
    request = (url, options = {}) => new Promise((resolve, reject) => {
      fixtureRequests.push({url, options, resolve, reject});
    });
    const agents = ["writer-å", "reviewer-z"].map((id, index) => ({
      id, name: id, who: `portable-route-${index}`, ready: true,
      at: {x: index * 250, y: 40}, icon: "robot", colour: "#18aabb",
    }));
    swarmSaid = {board: {version: 1, agents, projects: [], works_on: [],
      talks_to: [{one: agents[0].id, other: agents[1].id}]}};
    swarmBoardHydrated = true;
    swarmChats = [];
    longGoals = []; longGoal = null;
    directLongGoalRecoveryInventoryReady = true;
    if (wire) { wireUpTheTray(); wireUpTheSwarmBoard(); }
    document.querySelectorAll(".view").forEach(one => { one.hidden = true; });
    $("swarmView").hidden = false;
    renderSwarmBoard();
    window.fixtureAgent = agents[0].id;
    window.fixtureChat = id => ({id, name: id, pair: agents.map(one => one.id),
      pair_agents: agents, project: "portable-project", scope: "pair"});
    window.resolveFixture = (needle, active, failed = false) => {
      const pending = fixtureRequests.find(one => !one.done && one.url.includes(needle));
      if (!pending) throw new Error(`No pending request: ${needle}`);
      pending.done = true;
      if (failed) pending.reject(new Error("Simulated disk unavailable"));
      else pending.resolve(needle.includes("/said") ? {said: []}
        : {active, chats: ["saved-a", "new-b", "new-c"].map(fixtureChat)});
    };
  }, wire);
}

async function exerciseComposer(page) {
  const errors = [];
  page.on("pageerror", error => errors.push(String(error)));
  await page.evaluate(() => { openTheBigChat(fixtureAgent); setWhatCanBePressedInSwarm(); });
  const box = page.locator("#theBigChatBox");
  const typeImmediately = async text => {
    const start = performance.now();
    await box.click({timeout: 1000});
    await page.keyboard.type(text);
    const elapsed = performance.now() - start;
    assert.ok(elapsed < 1000, "first keystrokes must not await chat loading");
    console.log(`typing before response: ${Math.round(elapsed)} ms`);
    assert.equal(await box.inputValue(), text);
  };
  await typeImmediately("Startup draft — α");
  assert.equal(await page.locator("#theBigChatSend").isDisabled(), true);
  // Exercise the real keyboard handler while the identity is still unknown.
  await page.keyboard.press("Control+Enter");
  assert.equal(await page.evaluate(() => fixtureRequests.some(one => /\/say|\/prepare|\/start$/.test(one.url))), false);
  await page.evaluate(() => resolveFixture("/chats?", "saved-a"));
  await page.waitForFunction(() => activeConversationFor(fixtureAgent)?.id === "saved-a");
  assert.equal(await box.inputValue(), "Startup draft — α");
  assert.equal(await box.evaluate(el => document.activeElement === el), true);

  await page.locator("#theBigChatHistoryToggle").click();
  await page.getByRole("button", {name: "+ New chat for this pair", exact: true}).click({timeout: 1000});
  await typeImmediately("New chat draft — β");
  assert.equal(await page.locator("#theBigChatSend").isDisabled(), true);
  await page.evaluate(() => { renderSwarmBoard(); renderTheBigChat(); });
  assert.equal(await box.inputValue(), "New chat draft — β");
  await page.evaluate(() => resolveFixture("/create", "new-b"));
  await page.waitForFunction(() => activeConversationFor(fixtureAgent)?.id === "new-b");
  assert.equal(await box.inputValue(), "New chat draft — β");
  assert.equal(await box.evaluate(el => document.activeElement === el), true);
  await page.locator('.the-big-chat-conversation-pick[data-chat-id="saved-a"]').click({timeout: 1000});
  assert.equal(await box.inputValue(), "Startup draft — α");
  await page.evaluate(() => resolveFixture("/activate", "saved-a"));
  await page.waitForFunction(() => !swarmConversationSwitching.has(fixtureAgent));

  await page.getByRole("button", {name: "+ New chat for this pair", exact: true}).click({timeout: 1000});
  await typeImmediately("Keep after creation fails");
  await page.evaluate(() => resolveFixture("/create", "", true));
  await page.waitForFunction(() => !swarmConversationSwitching.has(fixtureAgent));
  assert.equal(await box.inputValue(), "Keep after creation fails");
  assert.equal(await box.isEditable(), true);
  assert.equal(await page.locator("#theBigChatSend").isDisabled(), true);
  await page.keyboard.press("Control+Enter");
  assert.equal(await page.evaluate(() => fixtureRequests.some(one => /\/say|\/prepare|\/start$/.test(one.url))), false);
  // Explicitly leave a failed draft, then retry it: neither saved nor new text is lost.
  await page.locator('.the-big-chat-conversation-pick[data-chat-id="saved-a"]').click({timeout: 1000});
  assert.equal(await box.inputValue(), "Startup draft — α");
  await page.evaluate(() => resolveFixture("/activate", "saved-a"));
  await page.waitForFunction(() => !swarmConversationSwitching.has(fixtureAgent));
  await page.getByRole("button", {name: "+ New chat for this pair", exact: true}).click({timeout: 1000});
  assert.equal(await box.inputValue(), "Keep after creation fails");
  await page.evaluate(() => resolveFixture("/create", "new-c"));
  await page.waitForFunction(() => activeConversationFor(fixtureAgent)?.id === "new-c");
  assert.equal(await box.inputValue(), "Keep after creation fails");
  // Late history and redraws cannot take focus or reset the draft/caret.
  await page.evaluate(() => {
    const box = $("theBigChatBox"); box.setSelectionRange(3, 8);
    for (const pending of fixtureRequests.filter(one => !one.done && one.url.includes("/said"))) {
      pending.done = true; pending.resolve({said: []});
    }
  });
  await page.evaluate(() => renderTheBigChat());
  assert.deepEqual(await box.evaluate(el => [el.value, el.selectionStart, el.selectionEnd]),
    ["Keep after creation fails", 3, 8]);
  assert.deepEqual(errors, []);
}

async function exerciseCompactComposer(page) {
  await page.evaluate(() => { minimiseTheBigChat(false); });
  const box = page.locator(".swarm-chat-box").first();
  await page.evaluate(() => { void createConversationFor(fixtureAgent, "reviewer-z"); });
  await box.click({timeout: 1000});
  await page.keyboard.type("Compact pending draft");
  assert.equal(await box.inputValue(), "Compact pending draft");
  assert.equal(await page.locator(".swarm-chat-send").first().isDisabled(), true);
  await page.evaluate(() => renderSwarmBoard());
  assert.equal(await box.inputValue(), "Compact pending draft");
  assert.equal(await box.evaluate(el => document.activeElement === el), true);
  await page.evaluate(() => resolveFixture("/create", "new-b"));
  await page.waitForFunction(() => !swarmConversationSwitching.has(fixtureAgent));
  assert.equal(await box.inputValue(), "Compact pending draft");
  await page.evaluate(() => { void activateConversationFor(fixtureAgent, "saved-a"); });
  assert.equal(await box.inputValue(), "");
  await page.evaluate(() => resolveFixture("/activate", "saved-a"));
  await page.waitForFunction(() => !swarmConversationSwitching.has(fixtureAgent));
  await page.evaluate(() => { void activateConversationFor(fixtureAgent, "new-b"); });
  assert.equal(await box.inputValue(), "Compact pending draft");
  // A backend that never responds still cannot freeze local typing.
  await box.click({timeout: 1000});
  await page.keyboard.press("End");
  await page.keyboard.type(" continues");
  assert.equal(await box.inputValue(), "Compact pending draft continues");
}

if (require.main === module || process.env.NODE_TEST_CONTEXT) {
  test("full renderer accepts real keystrokes before startup/new-chat requests resolve and preserves exact drafts", {timeout: 45000}, async () => {
    const manifest = JSON.parse(fs.readFileSync(path.join(runtime, "NEXUS_RUNTIME.json"), "utf8"));
    const browser = await chromium.launch({headless: true, executablePath: process.env.NEXUS_TEST_CHROMIUM
      || path.join(runtime, "playwright", manifest.playwright.chromium_executable)});
    try {
      const page = await browser.newPage({viewport: {width: 1280, height: 800}});
      await page.route("https://nexus.test/**", route => route.fulfill({contentType: "text/html",
        body: fs.readFileSync(path.join(ui, "index.html"), "utf8")
          .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, "").replace(/<link\b[^>]*>/gi, "")}));
      await page.goto("https://nexus.test");
      await page.addStyleTag({content: fs.readFileSync(path.join(ui, "styles.css"), "utf8")});
      let source = fs.readFileSync(path.join(ui, "app.js"), "utf8").replace(/\bboot\(\);\s*$/, "");
      if (process.env.NEXUS_COMPOSER_NEGATIVE_CONTROL === "1") {
        source = source.replace('$("theBigChatBox").disabled = !chatAgent;',
          '$("theBigChatBox").disabled = !chatAgent || identityChanging;');
      }
      await page.addScriptTag({content: source});
      await installFixture(page);
      await exerciseComposer(page);
      await exerciseCompactComposer(page);
    } finally { await browser.close(); }
  });
}

module.exports = {installFixture, exerciseComposer, exerciseCompactComposer};
