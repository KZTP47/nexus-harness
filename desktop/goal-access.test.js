"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const test = require("node:test");
const {chromium} = require("playwright-core");
const ui = path.resolve(__dirname, "../src/our_harness/ui");
const runtime = path.join(__dirname, "build-output/win-unpacked/resources/runtime");
const source = fs.readFileSync(path.join(ui, "app.js"), "utf8");

test("visible permissions support deny, once, always, mode changes and stale errors in a real browser", {timeout: 45000}, async () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(runtime, "NEXUS_RUNTIME.json"), "utf8"));
  const browser = await chromium.launch({executablePath: process.env.NEXUS_TEST_CHROMIUM || path.join(runtime, "playwright", manifest.playwright.chromium_executable), headless: true});
  const output = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-access-ui-"));
  try {
    const page = await browser.newPage({viewport: {width:1264,height:850}});
    await page.setContent('<main style="max-width:1000px;margin:20px auto;padding:12px"><h1>Nexus Harness · Team chat</h1><section id="panel" class="swarm-chat-team-goal"></section></main>');
    await page.addStyleTag({content: fs.readFileSync(path.join(ui, "styles.css"), "utf8")});
    await page.addScriptTag({content: source.slice(source.indexOf("function appendGoalAccessControls"), source.indexOf("function fillChatGoalPanel")) + `
      function make(tag, cls='', text='') { const n=document.createElement(tag); n.className=cls; n.textContent=text; return n; }
      const goal={goal_id:'exact-goal',revision:3,status:'paused',workspace_path:'portable-workspace',project:{id:'project'},
        tasks:[],agent_access:{mode:'ask'},command_request:{state:'pending'}};
      window.calls=[]; window.fail=false; window.failResume=false; window.commandPause=true;
      async function request(url,options) {
        if (!options) return {goal_id:goal.goal_id,revision:goal.revision,project_path:'Example project',commands:[['npm','run','test']],approval_digest:'a'.repeat(64)};
        const body=JSON.parse(options.body); calls.push({url,body});
        if (window.fail) throw new Error('The command changed; refresh before approving');
        if (window.failResume && url.endsWith('/control')) throw new Error('Permission saved; the project needs attention before Resume.');
        if (url.endsWith('/access')) {
          if (body.mode) {goal.agent_access.mode=body.mode; delete goal.command_request;}
          else goal.command_request={state:body.decision==='deny'?'denied':'approved',resume_after_decision:window.commandPause};
          goal.revision++;
        } else goal.status='running';
        return {goal:{...goal}};
      }
      window.render=()=>{const p=document.getElementById('panel');p.replaceChildren();appendGoalAccessControls(p,goal,async()=>render(),{chat_id:'chat',project_id:'project',participant_ids:['one','two']});};
      window.reset=()=>{goal.status='paused';goal.agent_access.mode='ask';goal.command_request={state:'pending'};goal.pending_interrupts=[];goal.resume_recovery={};window.commandPause=true;window.calls=[];window.fail=false;window.failResume=false;render();};
      window.goal=goal; reset();
    `});
    await page.getByRole("button", {name:"Run once", exact:true}).waitFor();
    assert.match(await page.locator("#panel").innerText(), /npm.*run.*test/s);
    await page.getByRole("button", {name:"Deny", exact:true}).click();
    await page.waitForFunction(() => calls.length === 2);
    assert.equal(await page.evaluate(() => calls[0].body.decision), "deny");
    for (const boundary of ['manual', 'question', 'unknown-effect']) {
      await page.evaluate(boundary => {
        reset();
        if (boundary === 'manual') window.commandPause=false;
        if (boundary === 'question') goal.pending_interrupts=[{id:'real-question'}];
        if (boundary === 'unknown-effect') goal.resume_recovery={items:[{kind:'provider_reply'}],resume_safe:false};
        render();
      }, boundary);
      await page.getByRole("button", {name:"Deny", exact:true}).click();
      await page.getByRole("button", {name:"Review command permissions"}).waitFor();
      assert.equal(await page.evaluate(() => calls.length), 1, boundary);
    }
    await page.evaluate(() => reset());
    await page.getByRole("button", {name:"Run once", exact:true}).click();
    await page.waitForFunction(() => calls.length === 2);
    assert.deepEqual(await page.evaluate(() => calls.map(c=>c.url)), ["/api/long-horizon/access", "/api/long-horizon/control"]);
    assert.equal(await page.evaluate(() => calls[0].body.decision), "once");
    assert.deepEqual(await page.evaluate(() => calls[0].body.participant_ids), ["one", "two"]);
    await page.evaluate(() => reset());
    await page.getByRole("button", {name:"Always allow this command in this chat", exact:true}).click();
    await page.waitForFunction(() => calls.length === 2);
    assert.equal(await page.evaluate(() => calls[0].body.decision), "always");
    await page.evaluate(() => reset());
    await page.getByLabel("Agent access for this chat").selectOption("read_only");
    await page.getByRole("button", {name:"Run once", exact:true}).waitFor();
    assert.equal(await page.getByRole("button", {name:"Run once", exact:true}).isDisabled(), true);
    assert.equal(await page.evaluate(() => calls[0].body.mode), "read_only");
    await page.getByLabel("Agent access for this chat").selectOption("full");
    assert.equal(await page.evaluate(() => calls.at(-1).body.mode), "full");
    await page.evaluate(() => {reset();window.fail=true;});
    await page.getByRole("button", {name:"Run once", exact:true}).click();
    assert.match(await page.getByRole("status").innerText(), /command changed/);
    assert.equal(await page.evaluate(() => calls.length), 1);
    await page.evaluate(() => {reset();window.failResume=true;});
    await page.getByRole("button", {name:"Run once", exact:true}).click();
    await page.waitForFunction(() => calls.length === 2);
    assert.match(await page.locator(".chat-access-status").innerText(), /project needs attention/);
    for (const width of [1264,390]) {
      await page.evaluate(() => reset());
      await page.setViewportSize({width,height:850});
      await page.getByRole("button", {name:"Run once", exact:true}).waitFor();
      const geometry=await page.locator('.chat-command-request').evaluate(n=>({fits:n.scrollWidth<=n.clientWidth+1,doc:document.documentElement.scrollWidth<=innerWidth+1}));
      assert.deepEqual(geometry,{fits:true,doc:true});
      await page.screenshot({path:path.join(output,`permissions-${width}.png`),fullPage:true});
    }
    console.log(`Permission screenshots: ${output}`);
    // The goal composer uses the selected access mode in its saved request,
    // independently of the per-chat controls exercised above.
    await page.setContent(fs.readFileSync(path.join(ui, "index.html"), "utf8")
      .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, ""));
    await page.addScriptTag({content:
      'const $=id=>document.getElementById(id);' +
      source.slice(source.indexOf("function selectedLongGoalAgentIds"), source.indexOf("function saveLongGoalComposerDraft"))});
    await page.evaluate(()=>document.getElementById('longGoalDialog').showModal());
    for (const mode of ['read_only', 'full', 'ask']) {
      await page.locator("#longGoalAccess").selectOption(mode);
      const selected = await page.evaluate(()=>({draft:longGoalComposerDraft(),intent:JSON.parse(longGoalIntent(longGoalComposerDraft()))}));
      assert.equal(selected.draft.access_mode, mode);
      assert.equal(selected.intent.policy.agent_access_mode, mode);
    }
  } finally { await browser.close(); }
});
