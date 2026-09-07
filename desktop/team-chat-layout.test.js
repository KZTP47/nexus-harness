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

test("actual chat renderers keep both agent replies readable across routine goal transitions", {
  skip: !fs.existsSync(manifestPath) && !process.env.NEXUS_TEST_CHROMIUM,
  timeout: 45000,
}, async () => {
  const manifest = fs.existsSync(manifestPath) ? JSON.parse(fs.readFileSync(manifestPath, "utf8")) : {};
  const executablePath = process.env.NEXUS_TEST_CHROMIUM || path.join(runtime, "playwright", manifest.playwright.chromium_executable);
  const browser = await chromium.launch({executablePath, headless: true});
  const output = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-team-status-layout-"));
  try {
    const page = await browser.newPage({viewport: {width: 1264, height: 775}});
    await page.setContent(fs.readFileSync(path.join(ui, "index.html"), "utf8")
      .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, "").replace(/<link\b[^>]*>/gi, ""));
    await page.addStyleTag({content: fs.readFileSync(path.join(ui, "styles.css"), "utf8")});
    const source = fs.readFileSync(path.join(ui, "app.js"), "utf8");
    const section = (start, end, from = 0) => {
      const begin = source.indexOf(start, from);
      const finish = source.indexOf(end, begin);
      assert.ok(begin >= 0 && finish > begin, start);
      return source.slice(begin, finish);
    };
    const renderStart = source.indexOf("function renderTheBigChat");
    const transform = section("  const turns = [];", "  // What this agent said", renderStart);
    const renderRows = section("    for (const one of turns) {", "    list.scrollTop = list.scrollHeight;", renderStart);
    // Use the production transformation and row renderers. Only network/state
    // lookup and decorative avatar drawing are replaced by this local fixture.
    await page.addScriptTag({content: `
      const $ = id => document.getElementById(id);
      ${section("const BIG_CHAT_LAYOUT_DEFAULTS", "let theBigChatLayout")}
      let theBigChatLayout = {...BIG_CHAT_LAYOUT_DEFAULTS}; let theBigChatResize = null; let theBigOne = 'builder';
      function saveTheBigChatLayout() {}
      ${section("function boundedBigChatSize", "function rememberTheBigChatComposer")}
      ${section("function make(tag", "function migrateGraph")}
      ${section("function isNexusChatTurn", "function aChatTurnFace")}
      ${section("function appendChatText", "const PARTICIPANT_OUTCOME_STATUSES")}
      ${section("function chatTurnSpeaker", "let userQuestionRenderId")}
      ${section("function putTheChatTurnsIn", "function renderTheChatThreadFor")}
      const agent = {id: 'builder', name: 'Team A'};
      function theSwarmAgent() { return agent; }
      function agentForChatTurn() { return agent; }
      function styleForAgent() {}
      function aChatTurnFace(one, speaker, className) { return make('span', 'swarm-agent-face ' + className, 'N'); }
      function aFaceFor() { return make('span', 'swarm-agent-face the-big-chat-face', 'A'); }
      function normalizedParticipantOutcome() { return null; }
      function appendInlineUserQuestions() {}
      function prettyTime(value) { return value + ' ms'; }
      async function openChatGoalDetails(value) { window.openedGoal = value.goal_id; }
      function renderFixtureFull(raw) {
        const list = $('theBigChatSaid');
        const chatTurnsWhileWorking = () => raw;
        const keptTranscriptFor = () => raw;
        ${transform}
        list.replaceChildren();
        ${renderRows}
      }
    `});
    const replies = [
      "TEAM-A-REVIEW: B, I checked your amber page, three-coin win, reset, and API against the goal.",
      "TEAM-B-FINAL: A, our latest files satisfy the steered goal and are ready for deterministic checks.",
    ];
    const statuses = [
      "Durable goal portable was accepted and is queued in Mission control.",
      "Durable goal portable is running. Mission control shows its current task and evidence.",
    ];
    await page.evaluate(({replies, statuses}) => {
      for (let one = document.querySelector("#theBigChat"); one; one = one.parentElement) one.hidden = false;
      $("theBigChatTitle").textContent = "Team A ↔ Team B — Amber arena";
      $("theBigChatScopeHint").textContent = "Your messages steer this team's current goal.";
      $("theBigChatTeamGoal").hidden = true;
      for (const selector of ["#theBigChatWork", "#theBigChatCollaborate", ".the-big-chat-round-policy"]) document.querySelector(selector).hidden = true;
      const reply = (index) => ({who: "them", phase: "agent_discussion", speaker_id: index ? "reviewer" : "builder",
        speaker_name: index ? "Team B" : "Team A", recipient_name: index ? "Team A" : "Team B",
        text: replies[index], speaker_route: "portable-agent", model: "fixture-model", milliseconds: 120});
      const status = (index) => ({who: "them", phase: "long_horizon_status", speaker_id: "nexus",
        speaker_name: "Nexus", recipient_name: "You", text: statuses[index], at: "2026-09-07T12:00:00Z",
        correlation: {schema_version: 1, kind: "long_horizon_status", goal_id: "portable-goal", goal_status: index ? "running" : "queued"}});
      window.fixtureTurns = [reply(0), status(0), status(1), reply(1)];
      renderFixtureFull(window.fixtureTurns);
      applyTheBigChatLayout();
    }, {replies, statuses});
    const measurements = [];
    for (const viewport of [{width: 1264, height: 775}, {width: 1008, height: 655}]) {
      await page.setViewportSize(viewport);
      await page.evaluate(() => { applyTheBigChatLayout(); $("theBigChatSaid").scrollTop = 0; });
      const geometry = await page.evaluate(() => {
        const list = $("theBigChatSaid").getBoundingClientRect();
        return {transcriptHeight: list.height, replies: [...document.querySelectorAll(".the-big-chat-turn.between .chat-prose")].map(one => {
          const rect = one.getBoundingClientRect();
          return {top: rect.top, height: rect.height, visible: Math.max(0, Math.min(rect.bottom, list.bottom, innerHeight) - Math.max(rect.top, list.top, 0))};
        }), statuses: [...document.querySelectorAll(".chat-goal-status-row")].map(one => {
          const rect = one.getBoundingClientRect();
          return {height: rect.height, speaker: one.querySelector(".chat-goal-status-speaker").textContent,
            text: one.querySelector(".chat-prose").textContent,
            visible: rect.top >= list.top && rect.bottom <= list.bottom,
            detailsOpen: one.querySelector("details").open};
        })};
      });
      await page.screenshot({path: path.join(output, `${viewport.width}x${viewport.height}.png`)});
      assert.ok(geometry.transcriptHeight >= Math.min(240, viewport.height * 0.35), JSON.stringify(geometry));
      assert.equal(geometry.replies.length, 2);
      assert.ok(geometry.replies.every(one => one.visible >= Math.min(30, one.height)), JSON.stringify(geometry));
      assert.deepEqual(geometry.statuses.map(one => one.text), statuses);
      assert.ok(geometry.statuses.every(one => one.visible && one.height < 60 && one.speaker === "Nexus → You" && !one.detailsOpen), JSON.stringify(geometry));
      assert.equal(await page.locator(".the-big-chat-turn.between .chat-turn-phase").allTextContents().then(values => values.join("|")), "Team discussion|Team discussion");
      measurements.push({viewport, ...geometry});
      await page.screenshot({path: path.join(output, `${viewport.width}x${viewport.height}.png`)});
    }
    const details = page.locator(".chat-goal-status-row").first().locator("details");
    await details.locator("summary").click();
    assert.match(await details.innerText(), /2026-09-07T12:00:00Z/);
    await details.getByRole("button", {name: "Open goal in Mission control"}).click();
    assert.equal(await page.evaluate(() => window.openedGoal), "portable-goal");
    const retained = await page.evaluate(() => {
      const blocked = {...window.fixtureTurns[1], text: "Checks failed: inspect the test error before resuming.",
        correlation: {...window.fixtureTurns[1].correlation, goal_status: "paused"}};
      const failure = {...blocked, phase: "nexus_error", text: "Provider failed: the saved reply remains available."};
      renderFixtureFull([blocked, failure]);
      const labels = [...document.querySelectorAll("#theBigChatSaid .chat-turn-phase")].map(one => one.textContent);
      const prominent = [...$("theBigChatSaid").children].every(one => !one.classList.contains("chat-goal-status-row"));
      const compact = document.createElement("ol");
      putTheChatTurnsIn(compact, agent, [...window.fixtureTurns, blocked, failure], false);
      return {labels, prominent, compactRoutine: compact.querySelectorAll(".chat-goal-status-row").length,
        compactAgentLabels: [...compact.querySelectorAll(".between .chat-turn-phase")].map(one => one.textContent),
        compactBlockers: [...compact.children].slice(-2).every(one => !one.classList.contains("chat-goal-status-row"))};
    });
    assert.deepEqual(retained, {labels: ["Project goal status", "Nexus failure"], prominent: true,
      compactRoutine: 2, compactAgentLabels: ["Team discussion", "Team discussion"], compactBlockers: true});
    fs.writeFileSync(path.join(output, "layout.json"), JSON.stringify(measurements, null, 2));
    console.log("Goal status layout screenshots:", output);
  } finally {
    await browser.close();
  }
});

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
