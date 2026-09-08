"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {spawnSync} = require("node:child_process");
const test = require("node:test");
const {chromium} = require("playwright-core");
const root = path.resolve(__dirname, "..");
const runtime = path.join(__dirname, "build-output/win-unpacked/resources/runtime");
const manifestPath = path.join(runtime, "NEXUS_RUNTIME.json");
const ui = process.env.NEXUS_PROGRESS_UI || path.join(root, "src/our_harness/ui");

test("live scheduler milestones appear in both chat renderers before answers and survive reopening", {
  timeout: 45000,
  skip: !fs.existsSync(manifestPath) && !process.env.NEXUS_TEST_CHROMIUM,
}, async () => {
  const fixture = spawnSync("python", ["-c",
    "import json; from tests.test_goal_chat_progress import runner_fixture; print(json.dumps(runner_fixture()))"],
    {cwd: root, encoding: "utf8", timeout: 20000, windowsHide: true});
  assert.equal(fixture.status, 0, fixture.stderr);
  const stages = JSON.parse(fixture.stdout);
  const manifest = fs.existsSync(manifestPath) ? JSON.parse(fs.readFileSync(manifestPath, "utf8")) : {};
  const executablePath = process.env.NEXUS_TEST_CHROMIUM || path.join(runtime, "playwright", manifest.playwright.chromium_executable);
  const browser = await chromium.launch({executablePath, headless: true});
  const output = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-visible-progress-"));
  try {
    const page = await browser.newPage({viewport: {width: 1264, height: 900}});
    await page.setContent('<main><h1>Team conversation</h1><p>Recorded requests, tools and replies</p><ul id="compact" class="talk-thread"></ul><h2>Expanded chat</h2><ul id="large" class="the-big-chat-said"></ul></main>');
    await page.addStyleTag({content: fs.readFileSync(path.join(ui, "styles.css"), "utf8")
      + '\nbody{display:block;padding:16px}main{max-width:1020px;margin:auto;min-width:0}#large,#compact{max-height:none;padding:8px}'});
    const source = fs.readFileSync(path.join(ui, "app.js"), "utf8");
    function section(start, end, offset = 0) {
      const begin = source.indexOf(start, offset), finish = source.indexOf(end, begin);
      assert.ok(begin >= 0 && finish > begin, start);
      return source.slice(begin, finish);
    }
    const bigStart = source.indexOf("function renderTheBigChat");
    await page.addScriptTag({content: `
      ${section("function make(tag", "function migrateGraph")}
      ${section("function appendChatText", "const PARTICIPANT_OUTCOME_STATUSES")}
      ${section("function chatTurnSpeaker", "let userQuestionRenderId")}
      ${section("function putTheChatTurnsIn", "function renderTheChatThreadFor")}
      const agent={id:'builder',name:'Ada'}; const theBigOne='builder';
      function isNexusChatTurn(one){return one.speaker_id==='nexus'}
      function agentForChatTurn(){return agent} function theSwarmAgent(){return agent}
      function styleForAgent(){} function normalizedParticipantOutcome(){return null}
      function aChatTurnFace(){return make('span','','◉')} function aFaceFor(){return make('span','','◉')}
      function appendInlineUserQuestions(){} function prettyTime(){return ''}
      window.render = (raw) => {
        putTheChatTurnsIn(document.getElementById('compact'),agent,raw,false);
        const list=document.getElementById('large'); const conversation={};
        const chatTurnsWhileWorking=()=>raw; const keptTranscriptFor=()=>raw;
        ${section("  const turns = [];", "  // What this agent said", bigStart)}
        list.replaceChildren();
        ${section("    for (const one of turns) {", "    list.scrollTop = list.scrollHeight;", bigStart)}
      };
      window.isProgress = normalizedChatProgress;
    `});
    for (const [index, stage] of stages.entries()) {
      await page.evaluate(raw => render(raw), stage);
      const milestones = stage.filter(one => one.phase === "nexus_progress");
      for (const id of ["compact", "large"]) {
        assert.equal(await page.locator(`#${id} .chat-progress-row`).count(), milestones.length);
        assert.deepEqual(await page.locator(`#${id} .chat-progress-text`).allTextContents(), milestones.map(one => one.text));
        assert.ok(await page.locator(`#${id} .chat-progress-row`).first().isVisible());
      }
      if (index === 0) assert.match(await page.locator('#large').innerText(), /Waiting for a reply/);
    }
    const final = stages.at(-1);
    await page.evaluate(() => render([])); // Switch to another saved chat.
    assert.equal(await page.locator('.chat-progress-row').count(), 0);
    await page.evaluate(raw => render(raw), final);
    const failed = page.locator('#large .chat-tool-activity-row[data-tool-status="failed"]');
    await failed.locator('summary').click();
    await page.waitForFunction(() => expandedChatToolActivity.size > 0);
    await page.evaluate(raw => render(raw), final); // Next poll/reopen retains details.
    assert.equal(await failed.locator('.chat-tool-body').isVisible(), true);
    assert.match(await failed.innerText(), /restart check failed/);
    const milestone = final.find(one => one.phase === "nexus_progress");
    for (const forged of [{...milestone,speaker_id:'builder'},
      {...milestone,correlation:{...milestone.correlation,progress_contract:'engine-milestones/v99'}}]) {
      assert.equal(await page.evaluate(one => isProgress(one), forged), null);
    }
    for (const width of [1264,390]) {
      await page.setViewportSize({width,height:1000});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true);
      const rows=await page.locator('.chat-progress-row').evaluateAll(nodes => nodes.map(row=>({
        fits:row.scrollWidth<=row.clientWidth+1,
        readable:parseFloat(getComputedStyle(row.querySelector('p')).fontSize)>=13,
        timestamp:row.querySelector('time').dateTime,
      })));
      assert.ok(rows.every(one=>one.fits&&one.readable&&one.timestamp));
      await page.screenshot({path:path.join(output,`${width}-progress.png`),fullPage:true});
    }
    const malicious = {...milestone,text:'<img src=x onerror="window.injected=true">'};
    await page.evaluate(raw=>render(raw),[malicious]);
    assert.equal(await page.locator('img').count(),0);
    assert.equal(await page.evaluate(()=>Boolean(window.injected)),false);
    console.log('Visible progress screenshots:',output);
  } finally {await browser.close();}
});
