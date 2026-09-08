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

test("actual chat activity and decision controls fit wide and narrow windows and preserve answers during polls", {
  skip: !fs.existsSync(manifestPath) && !process.env.NEXUS_TEST_CHROMIUM,
  timeout: 45000,
}, async () => {
  const manifest = fs.existsSync(manifestPath) ? JSON.parse(fs.readFileSync(manifestPath, "utf8")) : {};
  const executablePath = process.env.NEXUS_TEST_CHROMIUM || path.join(runtime, "playwright", manifest.playwright.chromium_executable);
  const browser = await chromium.launch({executablePath, headless: true});
  const output = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-team-feedback-"));
  try {
    const page = await browser.newPage({viewport: {width: 1264, height: 775}});
    await page.setContent(fs.readFileSync(path.join(ui, "index.html"), "utf8")
      .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, "").replace(/<link\b[^>]*>/gi, ""));
    await page.addStyleTag({content: fs.readFileSync(path.join(ui, "styles.css"), "utf8")});
    await page.addScriptTag({content: `
      const $ = id => document.getElementById(id);
      ${section("const BIG_CHAT_LAYOUT_DEFAULTS", "let theBigChatLayout")}
      let theBigChatLayout = {...BIG_CHAT_LAYOUT_DEFAULTS}; let theBigChatResize = null; let theBigOne = 'builder';
      function saveTheBigChatLayout() {}
      ${section("function boundedBigChatSize", "function rememberTheBigChatComposer")}
      ${section("function make(tag", "function migrateGraph")}
      ${section("function chatGoalParticipants", "function rememberChatGoalSnapshot")}
      ${section("function fillChatGoalPanel", "function syncChatGoalControls")}
      ${section("function normalizedUserQuestions", "function frozenWorkRecovery")}
      ${section("function aChatActivityPanel", "async function pollSwarmChatActivity")}
      ${section("function longHorizonStateWords", "function longHorizonAssignmentsForAgent")}
      const conversation = {id:'portable-chat',project:'portable-project',pair:['builder','reviewer']};
      const agents = [{id:'builder',name:'Builder'},{id:'reviewer',name:'Reviewer'}];
      const swarmChats = [{agent:'builder'}];
      const swarmChatActivity = new Map([['chat:portable-chat',{collapsed:true,state:'complete',stage:'Old admission receipt'}]]);
      const chatGoalRequests = new Set();
      let longGoals = [{goal_id:'portable-goal',conversation_id:conversation.id,project:{id:conversation.project},lead_agent_id:'builder',
        requested_agent_ids:conversation.pair,agents,status:'running',revision:4,pending_interrupts:[],tasks:[]}];
      function activeConversationFor() { return conversation; }
      function theSwarmAgent(id) { return agents.find(one => one.id === id); }
      function swarmChatRuntimeKey() { return 'chat:' + conversation.id; }
      function swarmChatKey() { return swarmChatRuntimeKey(); }
      function visibleSwarmChatActivity(one) { return one?.collapsed ? null : one; }
      function theChatCardFor() { return $('compactFixture'); }
      function setWhatCanBePressedInSwarm() {}
      async function openChatGoalDetails() {}
      window.submissions = [];
      async function request(url, options) { window.submissions.push({url,body:JSON.parse(options.body)}); return {goal:longGoals[0]}; }
      async function refreshChatGoalAfterAction() {}
      window.setGoal = (changes) => { Object.assign(longGoals[0],changes); fillChatGoalPanel($('theBigChatTeamGoal'),'builder',chatLongGoalContext('builder')); renderSwarmChatActivity('builder'); };
      window.renderFeedback = () => { fillChatGoalPanel($('theBigChatTeamGoal'),'builder',chatLongGoalContext('builder')); renderSwarmChatActivity('builder'); };
      for (let one=$('theBigChat');one;one=one.parentElement) one.hidden=false;
      $('theBigChatTitle').textContent='Builder ↔ Reviewer — Portable project';
      $('theBigChatScopeHint').textContent='Your messages steer this team’s current goal.';
      $('theBigChatSend').textContent='Send to team';
      $('theBigChatStop').textContent='Pause team';
      for (const selector of ['#theBigChatWork','#theBigChatCollaborate','.the-big-chat-round-policy']) document.querySelector(selector).hidden=true;
      const compact=make('div',''); compact.id='compactFixture'; compact.hidden=true; compact.append(aChatActivityPanel()); document.body.append(compact);
      applyTheBigChatLayout(); renderFeedback();
    `});
    // No messages and no active renderer request are needed: saved state owns
    // the activity display, including after closing/reopening the renderer.
    assert.equal(await page.locator("#theBigChatSaid li").count(), 0);
    await page.locator("#theBigChatActivity").waitFor({state: "visible"});
    assert.equal(await page.locator("#theBigChatActivity .chat-activity-stage").textContent(), "Team is working");
    assert.equal(await page.locator("#compactFixture .chat-activity").evaluate(one => one.hidden), false);
    await page.evaluate(() => setGoal({tasks:[{id:'task-a',assigned_agent_id:'builder',state:'running',provider_effect_state:'dispatched'}]}));
    assert.equal(await page.locator("#theBigChatActivity .chat-activity-stage").textContent(), "Builder is responding");
    await page.evaluate(() => setGoal({tasks:[{id:'task-a',assigned_agent_id:'builder',state:'running',provider_effect_state:'dispatched',
      protocol_recovery:{schema_version:2,state:'dispatched',attempts:1,max_attempts:2,cumulative_attempts:3}}]}));
    assert.match(await page.locator("#theBigChatActivity").innerText(), /Correcting Builder.*response/s);
    const geometry = [];
    for (const viewport of [{width:1264,height:775},{width:760,height:760},{width:390,height:844}]) {
      await page.setViewportSize(viewport);
      await page.evaluate(() => { applyTheBigChatLayout(); renderFeedback(); });
      const activity = await page.evaluate(() => {
        const panel=$('theBigChatActivity'), rect=panel.getBoundingClientRect(), list=$('theBigChatSaid').getBoundingClientRect();
        return {visible:rect.top>=0&&rect.bottom<=innerHeight,belowTranscript:rect.top>=list.bottom-1,
          overflow:panel.scrollWidth>panel.clientWidth+1,documentOverflow:document.documentElement.scrollWidth>innerWidth+1};
      });
      assert.deepEqual(activity,{visible:true,belowTranscript:true,overflow:false,documentOverflow:false});
      await page.screenshot({path:path.join(output,`${viewport.width}-active.png`)});
      await page.evaluate(() => setGoal({status:'waiting_for_user',pending_interrupts:[{id:'decision-a',reason:'Choose how to continue',questions:[{
        id:'approach',prompt:'Which approach should the team use for this project?',allow_other:true,options:[
          {label:'Continue with the current team',description:'Keep both agents and let them finish their shared work.',recommended:true},
          {label:'Review the saved work first',description:'Inspect the existing result before continuing. '+ 'portable-evidence-'.repeat(8)}]}]}]}));
      assert.equal(await page.locator("#theBigChatActivity .chat-activity-stage").textContent(), "Waiting for your answer");
      const options = await page.locator("#theBigChatTeamGoal .agent-question-option").evaluateAll(rows=>rows.map(row=>{
        const box=row.getBoundingClientRect(),radio=row.querySelector('input').getBoundingClientRect(),words=row.querySelector('.agent-question-option-words'),text=words.getBoundingClientRect();
        return {radioWidth:radio.width,radioHeight:radio.height,font:parseFloat(getComputedStyle(words).fontSize),
          centered:Math.abs((text.left+text.right)/2-(box.left+box.right)/2)<2,
          fits:text.left>=box.left&&text.right<=box.right&&row.scrollWidth<=row.clientWidth+1};
      }));
      assert.ok(options.every(one=>one.radioWidth>=12&&one.radioWidth<=20&&one.radioHeight<=20&&one.font>=16&&one.centered&&one.fits),JSON.stringify(options));
      const panel=await page.locator('#theBigChatTeamGoal').evaluate(one=>({overflow:one.scrollWidth>one.clientWidth+1}));
      assert.equal(panel.overflow,false);
      const firstOption = await page.locator('#theBigChatTeamGoal .agent-question-option').first().evaluate(one=>{
        const text=one.querySelector('.agent-question-option-words').getBoundingClientRect(),panel=document.querySelector('#theBigChatTeamGoal').getBoundingClientRect();
        return {visible:text.top>=panel.top&&text.bottom<=panel.bottom,textBottom:text.bottom,panelBottom:panel.bottom};
      });
      assert.equal(firstOption.visible,true,JSON.stringify(firstOption));
      await page.screenshot({path:path.join(output,`${viewport.width}-decision.png`)});
      await page.locator('#theBigChatTeamGoal').evaluate(one=>{one.scrollTop=one.scrollHeight;});
      const answersVisible=await page.locator('#theBigChatTeamGoal .chat-goal-answer').evaluate(one=>{
        const button=one.getBoundingClientRect(),panel=document.querySelector('#theBigChatTeamGoal').getBoundingClientRect();
        return button.top>=panel.top&&button.bottom<=panel.bottom;
      });
      assert.equal(answersVisible,true);
      await page.screenshot({path:path.join(output,`${viewport.width}-decision-answers.png`)});
      geometry.push({viewport,activity,options});
      await page.evaluate(()=>setGoal({status:'running',pending_interrupts:[]}));
    }
    // Selection is a native radio interaction. A repeated status poll must not
    // reset it, and submitting must retain the exact pending decision identity.
    await page.evaluate(()=>setGoal({status:'waiting_for_user',pending_interrupts:[{id:'decision-b',reason:'Choose controls',questions:[{
      id:'controls',prompt:'Keyboard or mouse?',allow_other:false,options:[{label:'Keyboard',description:'Use arrow keys.'},{label:'Mouse',description:'Use clicks.'}]}]}]}));
    await page.locator('#theBigChatTeamGoal').getByText('Keyboard',{exact:true}).click();
    await page.evaluate(()=>renderFeedback());
    assert.equal(await page.locator('#theBigChatTeamGoal input[value="Keyboard"]').isChecked(),true);
    await page.getByRole('button',{name:'Send answers to the team'}).click();
    const submission=await page.evaluate(()=>window.submissions.at(-1));
    assert.equal(submission.url,'/api/long-horizon/answer');
    assert.deepEqual(submission.body.pending_ids,['decision-b']);
    assert.equal(submission.body.answers['decision-b'],'Keyboard or mouse?: Keyboard');
    assert.equal(submission.body.expected_revision,4);
    // The same question control remains usable in the Mission control style.
    await page.evaluate(()=>{
      const field=userQuestionFields({id:'mission',prompt:'Choose a review',multiple:false,allowOther:false,options:[{label:'Review the project',description:'Read the saved files and verify the result.'}]},{},()=>{},'mission');
      field.classList.add('mission-question-set'); field.querySelector('label').classList.add('mission-option'); $('theBigChatTeamGoal').replaceChildren(field);
    });
    assert.equal(await page.locator('.mission-option input').evaluate(one=>one.getBoundingClientRect().width),16);
    const longPause = 'The verifier needs a corrected project snapshot. ' + 'The complete retained diagnostic belongs here. '.repeat(35);
    await page.evaluate(note=>setGoal({status:'paused',pending_interrupts:[],note}),longPause);
    assert.equal(await page.locator('#theBigChatActivity .chat-activity-detail').textContent(),
      'The verifier needs a corrected project snapshot.');
    await page.locator('#theBigChatActivity .chat-activity-explanation summary').click();
    assert.equal(await page.locator('#theBigChatActivity .chat-activity-explanation p').textContent(),longPause.trim());
    await page.evaluate(()=>renderFeedback());
    assert.equal(await page.locator('#theBigChatActivity .chat-activity-explanation').evaluate(one=>one.open),true);
    await page.screenshot({path:path.join(output,'390-expanded-pause.png')});
    await page.evaluate(()=>setGoal({note:'Paused by you.'}));
    assert.equal(await page.locator('#theBigChatActivity .chat-activity-explanation').isVisible(),false);
    await page.evaluate(()=>setGoal({status:'complete',pending_interrupts:[]}));
    assert.equal(await page.locator('#theBigChatActivity').evaluate(one=>one.hidden),true);
    fs.writeFileSync(path.join(output,'geometry.json'),JSON.stringify(geometry,null,2));
    console.log('Team activity and decision screenshots:',output);
  } finally { await browser.close(); }
});
