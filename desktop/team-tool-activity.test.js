"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
const {chromium} = require("playwright-core");
const ui = path.resolve(__dirname, "../src/our_harness/ui");
const runtime = path.join(__dirname, "build-output/win-unpacked/resources/runtime");
const manifestPath = path.join(runtime, "NEXUS_RUNTIME.json");
const source = fs.readFileSync(path.join(ui, "app.js"), "utf8");
function section(start, end) {
  const from = source.indexOf(start), to = source.indexOf(end, from);
  assert.ok(from >= 0 && to > from, start);
  return source.slice(from, to);
}

test("public updates and recorded tools render inline with safe expandable output in compact and large chats", {
  skip: !fs.existsSync(manifestPath) && !process.env.NEXUS_TEST_CHROMIUM,
  timeout: 30000,
}, async () => {
  const manifest = fs.existsSync(manifestPath) ? JSON.parse(fs.readFileSync(manifestPath, "utf8")) : {};
  const executablePath = process.env.NEXUS_TEST_CHROMIUM || path.join(runtime, "playwright", manifest.playwright.chromium_executable);
  const browser = await chromium.launch({executablePath, headless: true});
  const output = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-tool-timeline-"));
  try {
    const page = await browser.newPage({viewport: {width: 1264, height: 840}});
    await page.setContent('<main><h1>Builder ↔ Reviewer</h1><p>Shared progress and tool activity</p><ul id="compact" class="talk-thread"></ul><h2>Expanded conversation</h2><ul id="large" class="the-big-chat-said"></ul></main>');
    await page.addStyleTag({content: fs.readFileSync(path.join(ui, "styles.css"), "utf8")
      + '\nbody{display:block;padding:18px}main{max-width:1020px;margin:auto;min-width:0}#large{padding:8px;max-height:none}#compact{max-height:none}'});
    await page.addScriptTag({content: `
      ${section("function make(tag", "function migrateGraph")}
      ${section("function appendChatText", "const PARTICIPANT_OUTCOME_STATUSES")}
      ${section("function chatTurnSpeaker", "let userQuestionRenderId")}
      ${section("function putTheChatTurnsIn", "function renderTheChatThreadFor")}
      function isNexusChatTurn(one) { return one.speaker_id === 'nexus'; }
      function normalizedParticipantOutcome() { return null; }
      function agentForChatTurn() { return {name:'Builder'}; }
      function styleForAgent() {}
      function aChatTurnFace() { return make('span','','◉'); }
      function appendInlineUserQuestions() {}
      function prettyTime() { return ''; }
      const publicUpdate = {who:'them',speaker_id:'builder',speaker_name:'Builder',recipient_name:'Reviewer',
        phase:'agent_progress',text:'I checked the file and found the existing keyboard controls. I am running the project tests now.'};
      const payload = {schema_version:1,kind:'nexus_tool_activity',name:'run_selected_verification',status:'finished',
        arguments:{path:'arbitrary-user/project/verify.js'},result:{passed:true,stdout:'3 tests passed. <img src=x onerror="window.injected=true">'}};
      const tool = {who:'them',speaker_id:'nexus',speaker_name:'Builder · tool activity',phase:'agent_tool',at:'2026-09-08T08:30:14.123Z',
        text:JSON.stringify(payload),correlation:{schema_version:1,kind:'long_horizon_tool_activity',event_id:'portable-tool'}};
      const ordinary = {...publicUpdate,phase:'agent_discussion',text:JSON.stringify(payload)};
      window.renderCompact = () => putTheChatTurnsIn(document.getElementById('compact'),{name:'Builder'},[publicUpdate,tool],false);
      window.renderLarge = () => {
        const list=document.getElementById('large');list.replaceChildren();
        const speech=make('li','the-big-chat-turn from-them');
        const bubble=make('div','the-big-chat-what');appendChatText(bubble,'The project tests passed. Reviewer, please check the result.');speech.append(bubble);
        list.append(aChatToolActivityRow(tool.speaker_name,normalizedChatToolActivity(tool),tool.at,'the-big-chat-turn'),speech);
      };
      window.checkOrdinary = () => normalizedChatToolActivity(ordinary);
      window.checkUnsupported = () => normalizedChatToolActivity({...tool,correlation:{...tool.correlation,schema_version:2}});
      window.checkMalformed = () => normalizedChatToolActivity({...tool,text:'not valid JSON'});
      window.renderFailed = () => {
        const failed={...payload,eventId:'failed-portable-tool',status:'failed',name:'read_file',
          arguments:{end_line:1,path:'context-fixture.txt',start_line:2},
          error:'read_file end_line must be at least start_line',
          result:{status:'error',content:'{"error":"read_file end_line must be at least start_line"}'}};
        document.getElementById('compact').replaceChildren(aChatToolActivityRow(tool.speaker_name,failed,tool.at,'talk-turn'));
      };
      renderCompact();renderLarge();
    `});
    assert.equal(await page.evaluate(() => checkOrdinary()), null);
    assert.equal(await page.evaluate(() => checkUnsupported()), null);
    assert.equal(await page.evaluate(() => checkMalformed()), null);
    assert.equal(await page.locator('#compact .phase-agent_progress').textContent(), 'Progress update');
    assert.equal(await page.locator('#compact .chat-tool-activity').count(), 1);
    assert.equal(await page.locator('#large .chat-tool-activity').count(), 1);
    assert.equal(await page.locator('#compact .chat-tool-body').isVisible(), false);
    assert.match(await page.locator('#compact .chat-tool-heading').innerText(), /Builder.*Run verification.*Finished/s);
    await page.locator('#compact .chat-tool-heading').click();
    await page.waitForFunction(() => expandedChatToolActivity.has('portable-tool'));
    assert.match(await page.locator('#compact .chat-tool-output').last().innerText(), /3 tests passed/);
    assert.equal(await page.locator('#compact img').count(), 0);
    assert.equal(await page.evaluate(() => Boolean(window.injected)), false);
    // Rerendering on a poll, and switching to the large chat, both retain the
    // selected expansion because it belongs to the exact saved activity ID.
    await page.evaluate(() => { renderCompact();renderLarge(); });
    assert.equal(await page.locator('#compact .chat-tool-body').isVisible(), true);
    assert.equal(await page.locator('#large .chat-tool-body').isVisible(), true);
    for (const width of [1264, 390]) {
      await page.setViewportSize({width,height:1000});
      const fit=await page.locator('.chat-tool-activity-row').evaluateAll(rows=>rows.map(row=>({
        fits:row.scrollWidth<=row.clientWidth+1,
        readable:parseFloat(getComputedStyle(row.querySelector('.chat-tool-name')).fontSize)>=13,
        details:row.querySelector('details').open,
      })));
      assert.ok(fit.every(one=>one.fits&&one.readable&&one.details),JSON.stringify(fit));
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth<=innerWidth+1), true);
      await page.screenshot({path:path.join(output,`${width}-tool-timeline.png`),fullPage:true});
    }
    await page.evaluate(()=>renderFailed());
    await page.locator('#compact .chat-tool-heading').click();
    const failedDetails=await page.locator('#compact .chat-tool-body').innerText();
    assert.match(failedDetails,/Input.*"end_line": 1.*"start_line": 2.*Error\s+read_file end_line must be at least start_line.*Output/s);
    assert.match(await page.locator('#compact .chat-tool-output').last().innerText(),/"status": "error"/);
    console.log('Tool timeline screenshots:',output);
  } finally { await browser.close(); }
});
