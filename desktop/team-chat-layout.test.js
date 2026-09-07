"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const {chromium} = require("playwright-core");

const root = path.resolve(__dirname, "..");
const ui = path.join(root, "src/our_harness/ui");
const runtime = path.join(__dirname, "build-output/win-unpacked/resources/runtime");
const manifestPath = path.join(runtime, "NEXUS_RUNTIME.json");

test("full chat prioritizes visible replies, collapses setup, and retains keyboard resizing at desktop and narrow widths", {
  skip: !fs.existsSync(manifestPath) && !process.env.NEXUS_TEST_CHROMIUM,
  timeout: 45000,
}, async () => {
  const manifest = fs.existsSync(manifestPath) ? JSON.parse(fs.readFileSync(manifestPath, "utf8")) : {};
  const executablePath = process.env.NEXUS_TEST_CHROMIUM || path.join(runtime, "playwright", manifest.playwright.chromium_executable);
  const browser = await chromium.launch({executablePath, headless: true});
  const output = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-team-layout-"));
  try {
    const page = await browser.newPage({viewport: {width: 1264, height: 775}});
    const html = fs.readFileSync(path.join(ui, "index.html"), "utf8")
      .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, "").replace(/<link\b[^>]*>/gi, "");
    await page.setContent(html);
    await page.addStyleTag({content: fs.readFileSync(path.join(ui, "styles.css"), "utf8")});
    const source = fs.readFileSync(path.join(ui, "app.js"), "utf8");
    const defaults = source.slice(source.indexOf("const BIG_CHAT_LAYOUT_DEFAULTS"), source.indexOf("let theBigChatLayout"));
    const sizing = source.slice(source.indexOf("function boundedBigChatSize"), source.indexOf("function rememberTheBigChatComposer"));
    await page.addScriptTag({content: `const $ = id => document.getElementById(id); ${defaults}
      let theBigChatLayout = {...BIG_CHAT_LAYOUT_DEFAULTS}; let theBigChatResize = null; let theBigOne = 'builder';
      function saveTheBigChatLayout() {} ${sizing}`});
    await page.evaluate(() => {
      for (let one = document.querySelector("#theBigChat"); one; one = one.parentElement) one.hidden = false;
      document.querySelector("#theBigChatTitle").textContent = "Builder ↔ Reviewer — Amber arena";
      document.querySelector("#theBigChatScopeHint").textContent = "Your messages steer this team's current goal.";
      document.querySelector("#theBigChatSend").textContent = "Send to team";
      document.querySelector("#theBigChatStop").textContent = "Pause team";
      document.querySelector("#theBigChatStop").disabled = false;
      for (const selector of ["#theBigChatWork", "#theBigChatCollaborate", ".the-big-chat-round-policy"]) document.querySelector(selector).hidden = true;
      const panel = document.querySelector("#theBigChatTeamGoal");
      panel.hidden = false;
      panel.innerHTML = '<strong>Working together · running</strong><p class="hint">Send a message below to steer both agents.</p><button class="chat-goal-details">Advanced goal details</button>';
      const list = document.querySelector("#theBigChatSaid");
      for (const [name, words] of [
        ["You", "Create a 3D arena game with keyboard controls and a restart button."],
        ["Builder → Reviewer", "I have added movement, three collectible coins, and the restart control. Please inspect the collision handling and verify that the win state resets correctly."],
        ["Reviewer → Builder", "I checked the movement and reset logic. Reset clears both the score and the win state. I added checks for all three coins and the restart behavior, and the tests pass."],
      ]) {
        const row = document.createElement("li");
        row.className = "the-big-chat-turn " + (name === "You" ? "from-you" : "from-them between");
        row.innerHTML = '<div class="the-big-chat-face" aria-hidden="true">' + name[0] + '</div><div class="the-big-chat-what"><div class="the-big-chat-turn-head"><span class="the-big-chat-who"></span></div><p class="chat-prose"></p></div>';
        row.querySelector(".the-big-chat-who").textContent = name;
        row.querySelector(".chat-prose").textContent = words;
        list.append(row);
      }
      applyTheBigChatLayout();
      list.scrollTop = list.scrollHeight;
    });
    const measure = () => page.evaluate(() => {
      const list = document.querySelector("#theBigChatSaid").getBoundingClientRect();
      const box = document.querySelector("#theBigChatBox").getBoundingClientRect();
      const close = document.querySelector("#theBigChatShut").getBoundingClientRect();
      const header = document.querySelector(".the-big-chat-top").getBoundingClientRect();
      const replies = [...document.querySelectorAll(".the-big-chat-turn.between .chat-prose")].map((one) => {
        const rect = one.getBoundingClientRect();
        return Math.max(0, Math.min(rect.bottom, list.bottom, innerHeight) - Math.max(rect.top, list.top, 0));
      });
      return {transcriptHeight: list.height, transcriptWidth: list.width, replies,
        setupOpen: document.querySelector("#theBigChatDestination").open,
        historyVisible: getComputedStyle(document.querySelector(".the-big-chat-conversations")).display !== "none",
        limitsOpen: document.querySelector("#theBigChatLimits").open,
        composerVisible: box.top >= 0 && box.bottom <= innerHeight,
        closeVisible: close.left >= 0 && close.right <= innerWidth && close.top >= header.top && close.bottom <= Math.min(header.bottom, innerHeight),
        horizontalOverflow: document.documentElement.scrollWidth > innerWidth + 1};
    });
    const desktop = await measure();
    assert.ok(desktop.transcriptHeight >= 300, JSON.stringify(desktop));
    assert.ok(desktop.transcriptWidth >= 1000, JSON.stringify(desktop));
    assert.ok(desktop.replies.every((height) => height >= 30), JSON.stringify(desktop));
    assert.equal(desktop.setupOpen, false);
    assert.equal(desktop.historyVisible, false);
    assert.equal(desktop.limitsOpen, false);
    assert.equal(desktop.composerVisible, true);
    assert.equal(desktop.closeVisible, true);
    await page.screenshot({path: path.join(output, "desktop.png")});
    const resized = await page.evaluate(() => {
      const original = document.querySelector(".the-big-chat-bottom").getBoundingClientRect().height;
      resizeTheBigChatWithKeys("composer", {key: "ArrowUp", shiftKey: false, preventDefault() {}});
      const changed = document.querySelector(".the-big-chat-bottom").getBoundingClientRect().height;
      resizeTheBigChatWithKeys("composer", {key: "Home", preventDefault() {}});
      return {original, changed, reset: document.querySelector(".the-big-chat-bottom").getBoundingClientRect().height};
    });
    assert.ok(resized.changed > resized.original, JSON.stringify(resized));
    assert.equal(resized.reset, resized.original);
    await page.setViewportSize({width: 390, height: 844});
    await page.evaluate(() => applyTheBigChatLayout());
    const narrow = await measure();
    assert.ok(narrow.transcriptHeight >= 250, JSON.stringify(narrow));
    assert.equal(narrow.composerVisible, true);
    assert.equal(narrow.closeVisible, true);
    assert.equal(narrow.horizontalOverflow, false);
    await page.screenshot({path: path.join(output, "narrow.png")});
    fs.writeFileSync(path.join(output, "layout.json"), JSON.stringify({desktop, narrow, resized}, null, 2));
    console.log("Chat layout screenshots:", output);
  } finally {
    await browser.close();
  }
});
