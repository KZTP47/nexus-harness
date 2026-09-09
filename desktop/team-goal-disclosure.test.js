"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const test = require("node:test");
const {chromium} = require("playwright-core");
const ui = path.join(__dirname, "../src/our_harness/ui");
const source = fs.readFileSync(path.join(ui, "app.js"), "utf8");
const runtime = path.join(__dirname, "build-output/win-unpacked/resources/runtime");

test("team panel remembers collapse, expands for fresh input, preserves drafts and stops pulsing after resolution", {timeout:30000}, async () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(runtime, "NEXUS_RUNTIME.json"), "utf8"));
  const browser = await chromium.launch({executablePath:process.env.NEXUS_TEST_CHROMIUM || path.join(runtime,"playwright",manifest.playwright.chromium_executable),headless:true});
  const output = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-team-disclosure-"));
  try {
    const page = await browser.newPage({viewport:{width:1100,height:760}});
    await page.route("http://nexus.test/**", route => route.fulfill({contentType:"text/html",body:'<main class="the-big-chat-transcript"><section id="panel" class="chat-team-goal"></section></main><input id="composer" aria-label="Message">'}));
    await page.goto("http://nexus.test/");
    await page.addStyleTag({content:fs.readFileSync(path.join(ui,"styles.css"),"utf8")});
    const script = source.slice(source.indexOf("function fillChatGoalPanel"),source.indexOf("function syncChatGoalControls"));
    await page.addScriptTag({content:script + `
      function make(tag,cls='',text=''){const n=document.createElement(tag);n.className=cls;n.textContent=text;return n;}
      window.chat='first';window.goal={goal_id:'goal-one',project:{id:'portable-project'},status:'running',revision:1};
      const swarmChatKey=()=>window.chat, longHorizonStateWords=value=>value;
      const chatGoalBinding=()=>({}),openChatGoalDetails=()=>{},addGoalReconsideration=()=>{};
      const normalizedUserQuestions=items=>items||[];
      const userQuestionFields=q=>{const n=make('input');n.setAttribute('aria-label',q.prompt);return n;};
      const goalAnswerAudience=panel=>{const n=make('select');panel.append(n);return n;};
      const appendGoalAccessControls=panel=>panel.append(make('button','','Review command permissions'));
      window.render=()=>fillChatGoalPanel(document.getElementById('panel'),'agent',{goal:window.goal,problem:''});
      window.change=values=>{Object.assign(window.goal,values);render();};render();
    `});
    const details = page.locator("#panel details");
    const summary = page.locator("#panel summary");
    assert.equal(await details.evaluate(n=>n.open),false);
    await page.screenshot({path:path.join(output,'collapsed.png')});
    await summary.focus(); await page.keyboard.press("Enter");
    assert.equal(await details.evaluate(n=>n.open),true);
    await page.evaluate(()=>change({status:'paused',revision:2}));
    assert.equal(await details.evaluate(n=>n.open),true,"manual choice survives status updates");
    assert.equal(await summary.evaluate(n=>n.classList.contains('needs-user-input')),false,"ordinary pause is not a question");
    await summary.click();
    await page.evaluate(()=>{window.chat='second';window.goal={goal_id:'goal-two',project:{id:'portable-project'},status:'running',revision:1};render();});
    assert.equal(await details.evaluate(n=>n.open),false,"other chats start collapsed");
    await summary.click();
    await page.evaluate(()=>{window.chat='first';window.goal={goal_id:'goal-one',project:{id:'portable-project'},status:'running',revision:3};render();});
    assert.equal(await details.evaluate(n=>n.open),false,"first chat retains its own preference");
    await page.locator('#composer').focus();
    await page.evaluate(()=>change({status:'waiting_for_user',pending_interrupts:[{id:'question-1',questions:[{id:'color',prompt:'Choose a color'}]}]}));
    assert.equal(await details.evaluate(n=>n.open),true);
    assert.equal(await summary.innerText(),"Working together · waiting_for_userInput needed");
    assert.equal(await summary.evaluate(n=>getComputedStyle(n).animationName),"chat-input-glow");
    assert.equal(await page.evaluate(()=>document.activeElement.id),'composer',"attention must not steal typing focus");
    await page.getByLabel('Choose a color').fill('Keep this draft');
    await summary.click();
    await page.evaluate(()=>change({revision:4}));
    assert.equal(await details.evaluate(n=>n.open),false,"polling does not reopen an acknowledged request");
    assert.equal(await page.getByLabel('Choose a color').inputValue(),'Keep this draft');
    await page.evaluate(()=>change({pending_interrupts:[{id:'question-2',questions:[{id:'name',prompt:'Choose a name'}]}]}));
    assert.equal(await details.evaluate(n=>n.open),true,"a new request reopens the panel");
    await page.screenshot({path:path.join(output,'input-needed.png')});
    await page.emulateMedia({reducedMotion:'reduce'});
    assert.equal(await summary.evaluate(n=>getComputedStyle(n).animationName),'none');
    assert.equal(await summary.locator('.chat-team-input-needed').innerText(),'Input needed');
    await page.evaluate(()=>change({status:'running',pending_interrupts:[]}));
    assert.equal(await details.evaluate(n=>n.open),false,"answer restores the saved collapsed preference");
    assert.equal(await summary.locator('.chat-team-input-needed').count(),0);
    await page.evaluate(()=>change({status:'paused',command_request:{state:'pending',approval_digest:'new-command'}}));
    assert.equal(await details.evaluate(n=>n.open),true,"command approval needs attention too");
    await page.evaluate(()=>change({status:'complete'}));
    assert.equal(await summary.locator('.chat-team-input-needed').count(),0,"terminal history does not pulse");
    await page.setViewportSize({width:390,height:700});
    await page.evaluate(()=>change({status:'paused',command_request:null,resume_recovery:{items:[{id:'interrupted'}]}}));
    assert.equal(await details.evaluate(n=>n.open),true,"recovery input auto-expands");
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:path.join(output,'mobile-recovery.png')});
    // Recreate the renderer: persisted manual choices survive, pending input
    // still takes precedence on a fresh mount.
    await page.evaluate(()=>{document.querySelector('#panel').remove();const p=make('section','chat-team-goal');p.id='panel';document.querySelector('main').append(p);render();});
    assert.equal(await details.evaluate(n=>n.open),true);
    await page.evaluate(()=>change({status:'running',resume_recovery:null}));
    assert.equal(await details.evaluate(n=>n.open),false);
    console.log('Team panel screenshots: '+output);
  } finally {await browser.close();}
});
