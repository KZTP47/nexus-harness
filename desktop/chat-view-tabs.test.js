"use strict";
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {chromium} = require('playwright-core');
const ui = process.env.NEXUS_CHAT_UI_ROOT || path.join(__dirname, '../src/our_harness/ui');
const source = fs.readFileSync(path.join(ui, 'app.js'), 'utf8');
const section = (from, to) => source.slice(source.indexOf(from), source.indexOf(to, source.indexOf(from)));

test('chat tabs isolate settings, bind facilitator choices, retain drafts and expose pending decisions', async () => {
  const runtime = path.join(__dirname,'build-output/win-unpacked/resources/runtime');
  const manifest = JSON.parse(fs.readFileSync(path.join(runtime,'NEXUS_RUNTIME.json'),'utf8'));
  const browser = await chromium.launch({headless:true,executablePath:process.env.NEXUS_TEST_CHROMIUM || path.join(runtime,'playwright',manifest.playwright.chromium_executable)});
  const output = fs.mkdtempSync(path.join(os.tmpdir(),'nexus-chat-tabs-'));
  try {
    const page = await browser.newPage({viewport:{width:1264,height:850}});
    const html = fs.readFileSync(path.join(ui,'index.html'),'utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi,'').replace(/<link\b[^>]*>/gi,'');
    await page.route('http://tabs.test/**', route=>route.fulfill({contentType:'text/html',body:html}));
    await page.goto('http://tabs.test/');
    await page.addStyleTag({content:fs.readFileSync(path.join(ui,'styles.css'),'utf8')});
    await page.addScriptTag({content:`
      const $=id=>document.getElementById(id);
      ${section('function make(tag','function migrateGraph')}
      ${section('function directLongGoalCanonicalValue','async function prepareDirectLongGoalAdmission')}
      ${section('function chatCollaborationPreference','function appendGoalAccessControls')}
      ${section('function chatComposerAccessPreference','function syncChatGoalControls')}
      ${section('function appendFacilitatorRecovery','function goalReviewer')}
      ${section('async function controlChatGoal','async function reviewGoalVerificationCommands')}
      const chatGoalRequests=new Set(); const theBigOne='a';
      function sayInTheChatFor() {}
      window.conversation={id:'first-chat',project:'project-a',pair:['a','b'],binding:{revision:1}};
      window.context={goal:null,problem:''}; window.calls=[];
      function activeConversationFor(){return conversation;}
      function theSwarmAgent(id){return {id,name:id==='a'?'Builder':'Reviewer'};}
      function swarmChatKey(){return conversation.id;}
      function chatLongGoalContext(){return context;}
      function chatGoalBinding(){return {chat_id:conversation.id,project_id:conversation.project,participant_ids:conversation.pair};}
      async function request(url,options){calls.push({url,body:JSON.parse(options.body)});return {goal:{...context.goal,execution_mode:'facilitator',status:'running'}};}
      async function refreshChatGoalAfterAction(_agent,goal){context.goal=goal;render();}
      window.render=()=>syncBigChatTabs('a',context);
      for(let n=$('theBigChat');n;n=n.parentElement)n.hidden=false;
      $('theBigChatSaid').innerHTML='<li class="the-big-chat-turn">The conversation has the full chat pane.</li>';
      render();
      $('theBigChatStop').addEventListener('click',()=>controlChatGoal('a'));
    `});
    const chat=page.getByRole('tab',{name:'CHAT',exact:true});
    const settings=page.getByRole('tab',{name:'collaboration settings',exact:true});
    assert.equal(await chat.getAttribute('aria-selected'),'true');
    await page.locator('#theBigChatSettingsTab').click();
    await page.locator('#theBigChatChatTab').click();
    await page.locator('#theBigChatBox').fill('Keep this unsent draft.');
    for(const width of [1264,760,390]) {
      await page.setViewportSize({width,height:850});
      assert.equal(await page.locator('#theBigChatSettingsPanel').isVisible(),false);
      const bounds=await page.locator('#theBigChatSaid').boundingBox();
      assert.ok(bounds.height>300,JSON.stringify(bounds));
      await page.locator('#theBigChatBox').scrollIntoViewIfNeeded();
      const composer=await page.locator('#theBigChatBox').boundingBox();
      assert.ok(composer.height>=40 && composer.y>=0 && composer.y+composer.height<=850,JSON.stringify(composer));
      await page.screenshot({path:path.join(output,`${width}-chat.png`)});
      await settings.click();
      assert.equal(await page.locator('#theBigChatChatPanel').isVisible(),false);
      assert.equal(await page.getByRole('region',{name:'Facilitator mode',exact:true}).count(),1);
      assert.equal(await page.getByRole('heading',{name:'Facilitator mode',exact:true}).isVisible(),true);
      await page.evaluate(()=>{
        document.querySelector('.the-big-chat-sheet').style.setProperty('--big-chat-destination-height','72px');
        document.getElementById('theBigChatDestinationInfo').innerHTML='<section class="chat-destination">'+Array.from({length:18},(_,i)=>'<p>Provider and saved file information '+i+'</p>').join('')+'</section>';
      });
      const setup=await page.locator('#theBigChatDestination').evaluate(one=>({tag:one.tagName,height:one.clientHeight,scroll:one.scrollHeight,overflow:getComputedStyle(one).overflowY,innerOverflow:getComputedStyle(one.querySelector('.chat-destination')).overflowY}));
      assert.equal(setup.tag,'SECTION');
      assert.ok(setup.height>280 && setup.scroll<=setup.height+1,JSON.stringify(setup));
      assert.equal(setup.overflow,'visible'); assert.equal(setup.innerOverflow,'visible');
      assert.equal(await page.locator('#theBigChatDestinationResize').count(),0);
      await page.locator('#theBigChatSettingsPanel').evaluate(one=>one.scrollTop=0);
      await page.locator('#theBigChatSettingsPanel').getByLabel('Project work mode',{exact:true}).selectOption('facilitator');
      assert.match(await page.locator('#theBigChatSettingsPanel').getByLabel('Project work mode').locator('option:checked').innerText(),/Facilitator mode/);
      assert.equal(await page.evaluate(()=>chatProjectPolicy(conversation,'ask').execution_mode),'facilitator');
      assert.equal(await page.locator('#theBigChatWorkMode .chat-collaboration-direct').isVisible(),false);
      await page.screenshot({path:path.join(output,`${width}-facilitator.png`)});
      await page.locator('#theBigChatSettingsPanel').getByLabel('Project work mode').selectOption('isolated');
      await page.evaluate(()=>{document.getElementById('theBigChatWorkMode').dataset.snapshot='';render();});
      assert.equal(await page.locator('#theBigChatSettingsPanel').getByLabel('Project work mode').inputValue(),'isolated');
      await page.getByLabel('Collaboration mode').selectOption('fixed');
      await page.getByRole('button',{name:'Save collaboration',exact:true}).click();
      await page.screenshot({path:path.join(output,`${width}-settings.png`)});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
      await chat.click();
      assert.equal(await page.locator('#theBigChatBox').inputValue(),'Keep this unsent draft.');
    }
    await chat.focus(); await page.keyboard.press('ArrowRight');
    assert.equal(await settings.getAttribute('aria-selected'),'true');
    await page.keyboard.press('Home');
    assert.equal(await chat.getAttribute('aria-selected'),'true');
    await page.evaluate(()=>{context.goal={goal_id:'saved-goal',execution_workspace:{path:'private'},revision:19,status:'paused',agent_access:{mode:'ask'},pending_interrupts:[{id:'answer'}]};render();});
    // Questions are answered where the user types: the panel moves into CHAT.
    assert.equal(await page.locator('#theBigChatChatNeeded').isVisible(),true);
    assert.equal(await page.locator('#theBigChatSettingsNeeded').isVisible(),false);
    assert.equal(await page.locator('#theBigChatChatPanel #theBigChatTeamGoal').count(),1);
    assert.equal(await chat.getAttribute('aria-selected'),'true','Incoming input must not steal the chat');
    await settings.click();
    assert.match(await page.locator('#theBigChatWorkMode').innerText(),/saved goal uses private working copies/);
    assert.equal(await page.getByRole('button',{name:'Switch to facilitator mode',exact:true}).count(),0);
    await page.evaluate(()=>{context.goal.resume_recovery={items:[{kind:'provider'}],resume_safe:false,message:'Inspect interrupted work'};render();setBigChatTab('chat');document.getElementById('theBigChatStop').disabled=false;});
    await page.locator('#theBigChatStop').click();
    assert.equal(await chat.getAttribute('aria-selected'),'true','Resume must reveal required recovery controls in CHAT');
    assert.equal(await page.locator('#theBigChatChatPanel #theBigChatTeamGoal').count(),1);
    // A recovery that waits for the worker to stop has nothing to press yet.
    await page.evaluate(()=>{context.goal.pending_interrupts=[];context.goal.resume_recovery.needs_user=false;render();});
    assert.equal(await page.locator('#theBigChatChatNeeded').isVisible(),false);
    assert.equal(await page.locator('#theBigChatSettingsPanel #theBigChatTeamGoal').count(),1);
    await page.evaluate(()=>{context.goal.pending_interrupts=[{id:'answer'}];render();});
    assert.equal(await page.evaluate(()=>calls.length),0,'Unsafe Resume must not dispatch work');
    await page.evaluate(()=>delete context.goal.resume_recovery);
    await page.evaluate(()=>{context.goal.pending_interrupts=[];context.goal.revision++;render();});
    await settings.click();
    await page.getByRole('button',{name:'Switch to facilitator mode',exact:true}).click();
    assert.deepEqual(await page.evaluate(()=>calls[0].body),{goal_id:'saved-goal',chat_id:'first-chat',project_id:'project-a',participant_ids:['a','b'],action:'resume',payload:{facilitator_mode:true,expected_revision:20}});
    assert.match(await page.locator('#theBigChatWorkMode').innerText(),/On.*Working in the selected project/);
    assert.equal(await page.getByRole('button',{name:'Switch to facilitator mode',exact:true}).count(),0);
    await page.evaluate(()=>{context.goal.execution_mode='isolated';context.goal.status='running';context.goal.revision++;render();});
    assert.match(await page.locator('#theBigChatWorkMode').innerText(),/Pause this goal from CHAT/);
    assert.equal(await page.getByRole('button',{name:'Switch to facilitator mode',exact:true}).count(),0);
    await page.evaluate(()=>{context.goal.status='paused';context.goal.agent_access.mode='read_only';render();});
    assert.equal(await page.getByRole('button',{name:'Switch to facilitator mode',exact:true}).isDisabled(),true);
    await page.evaluate(()=>{context.problem='Reconnect the saved chat';render();});
    assert.match(await page.locator('#theBigChatWorkMode').innerText(),/Reconnect the saved chat/);
    assert.equal(await page.getByRole('button',{name:'Switch to facilitator mode',exact:true}).count(),0);
    await page.evaluate(()=>{context.problem='';context.goal.agent_access.mode='ask';context.goal.revision++;render();});
    await page.setViewportSize({width:1264,height:850});
    await page.locator('#theBigChatSettingsPanel').evaluate(one=>one.scrollTop=0);
    await page.screenshot({path:path.join(output,'saved-private-facilitator.png')});
    await page.evaluate(()=>{conversation={...conversation,id:'second-chat',project:'project-b'};context.goal=null;render();});
    assert.equal(await chat.getAttribute('aria-selected'),'true');
    await settings.click();
    assert.equal(await page.locator('#theBigChatSettingsPanel').getByLabel('Project work mode').inputValue(),'facilitator');
    await page.evaluate(()=>{conversation={...conversation,id:'first-chat',project:'project-a'};render();});
    await settings.click();
    assert.equal(await page.locator('#theBigChatSettingsPanel').getByLabel('Project work mode').inputValue(),'isolated','Each chat retains its own mode');
    await page.evaluate(()=>{conversation.binding.revision++;render();});
    assert.equal(await page.locator('#theBigChatSettingsPanel').getByLabel('Project work mode').inputValue(),'facilitator','Changed binding does not reuse old settings');
    console.log('Chat tab screenshots:',output);
  } finally {await browser.close();}
});
