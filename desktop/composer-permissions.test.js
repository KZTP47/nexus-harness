const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const {chromium} = require('playwright-core');
const ui = path.join(__dirname, '../src/our_harness/ui');
const source = fs.readFileSync(path.join(ui, 'app.js'), 'utf8');
const section = (start,end) => source.slice(source.indexOf(start),source.indexOf(end,source.indexOf(start)));

test('composer permissions persist per binding, reach request identity and reuse exact active-goal controls', async () => {
  const runtime = path.join(__dirname,'build-output/win-unpacked/resources/runtime');
  const manifest = JSON.parse(fs.readFileSync(path.join(runtime,'NEXUS_RUNTIME.json'),'utf8'));
  const browser = await chromium.launch({headless:true,executablePath:path.join(runtime,'playwright',manifest.playwright.chromium_executable)});
  try {
    const page = await browser.newPage({viewport:{width:1000,height:720}});
    await page.route('https://nexus.test/**', route => route.fulfill({contentType:'text/html',body:'<main style="padding:20px"><h1>Team chat</h1><div style="position:fixed;bottom:30px;left:20px;right:20px"><textarea aria-label="Message"></textarea><div class="button-row"><button>Send to team</button><button>Pause team</button><div id="permissions" class="chat-composer-permissions"></div></div></div></main>'}));
    const setup = async () => {
      await page.goto('https://nexus.test/');
      await page.addStyleTag({content:fs.readFileSync(path.join(ui,'styles.css'),'utf8')});
      await page.addScriptTag({content:`
        function make(tag,cls='',text=''){const n=document.createElement(tag);n.className=cls;n.textContent=text;return n;}
        window.conversation={id:'portable-chat',project:'portable-project',pair:['one','two'],binding:{route:'provider-a'}};
        window.context={goal:null,problem:''};window.calls=[];
        const activeConversationFor=()=>conversation,swarmChatKey=()=>conversation.id,chatLongGoalContext=()=>context;
        const chatGoalBinding=()=>({chat_id:conversation.id,project_id:conversation.project,participant_ids:conversation.pair});
        async function refreshChatGoalAfterAction(a,goal){context.goal=goal;render();}
        async function request(url,options){
          if(!options)return {goal_id:context.goal.goal_id,revision:context.goal.revision,commands:[]};
          const body=JSON.parse(options.body);calls.push({url,body});
          if(window.fail)throw Error('Stale permissions; refresh this goal');
          return {goal:{...context.goal,revision:context.goal.revision+1,agent_access:{mode:body.mode}}};
        }
        ${section('function directLongGoalCanonicalValue','async function prepareDirectLongGoalAdmission')}
        ${section('function appendGoalAccessControls','function fillChatGoalPanel')}
        ${section('function chatComposerAccessPreference','function syncChatGoalControls')}
        window.render=()=>fillChatComposerPermissions(document.getElementById('permissions'),'one',context);render();
      `});
    };
    await setup();
    const toggle=page.locator('.chat-composer-permissions-toggle');
    await toggle.click();
    const initial=await page.evaluate(()=>directLongGoalIntent(conversation,'one','Build a game',[]));
    await page.getByLabel('Agent access for this chat').selectOption('full');
    assert.match(await toggle.innerText(),/Full project access/);
    const changed=await page.evaluate(()=>directLongGoalIntent(conversation,'one','Build a game',[]));
    assert.notEqual(initial,changed,'access belongs to immutable request identity');
    await page.keyboard.press('Escape');
    await page.waitForFunction(()=>document.querySelector('.chat-composer-permissions-toggle').getAttribute('aria-expanded')==='false');
    assert.equal(await toggle.getAttribute('aria-expanded'),'false');
    await setup();
    assert.match(await toggle.innerText(),/Full project access/,'survives reload');
    await page.evaluate(()=>{conversation.id='other-chat';render();});
    assert.match(await toggle.innerText(),/Ask before commands/);
    await page.evaluate(()=>{conversation.id='portable-chat';render();});
    assert.match(await toggle.innerText(),/Full project access/);
    await page.evaluate(()=>{conversation.binding.route='changed-provider';render();});
    assert.match(await toggle.innerText(),/Ask before commands/,'changed provider binding resets preference');
    await page.evaluate(()=>{context.goal={goal_id:'exact-goal',revision:4,status:'paused',tasks:[],agent_access:{mode:'read_only'}};render();});
    await toggle.click();
    await page.getByLabel('Agent access for this chat').selectOption('full');
    await page.waitForFunction(()=>context.goal.agent_access.mode==='full');
    assert.deepEqual(await page.evaluate(()=>calls[0].body),{goal_id:'exact-goal',chat_id:'portable-chat',project_id:'portable-project',participant_ids:['one','two'],expected_revision:4,mode:'full'});
    await page.evaluate(()=>{window.fail=true;});
    await page.getByLabel('Agent access for this chat').selectOption('ask');
    await page.getByRole('status').filter({hasText:'Stale permissions'}).waitFor();
    assert.match(await toggle.innerText(),/Full project access/,'failed save does not claim changed mode');
    await page.evaluate(()=>{window.fail=false;context.goal.status='running';render();});
    assert.equal(await page.getByLabel('Agent access for this chat').isDisabled(),true);
    assert.match(await page.getByRole('dialog').innerText(),/Pause the team/);
    await page.setViewportSize({width:390,height:700});
    await page.evaluate(()=>{context.goal.status='paused';context.goal.command_request={state:'pending'};render();});
    assert.match(await toggle.innerText(),/Input needed/);
    const bounds=await page.getByRole('dialog').boundingBox();
    assert.ok(bounds.x>=0&&bounds.x+bounds.width<=390&&bounds.y>=0);
    await page.screenshot({path:path.join(__dirname,'../.harness/runtime/composer-permissions.png')});
    await page.getByRole('heading', {name:'Team chat'}).click();
    await page.waitForFunction(()=>document.querySelector('.chat-composer-permissions-toggle').getAttribute('aria-expanded')==='false');
    assert.equal(await toggle.getAttribute('aria-expanded'),'false','outside click dismisses');
  } finally {await browser.close();}
});
