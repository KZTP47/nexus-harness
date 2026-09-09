const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const destination=path.join(os.tmpdir(),'Nexus portable destination');
const deliveredFile=path.join(destination,'game','start.html');
const vm=require('node:vm');
const test=require('node:test');
const {chromium}=require('playwright-core');
const source=fs.readFileSync(path.join(__dirname,'../src/our_harness/ui/app.js'),'utf8');
const section=(start,end)=>source.slice(source.indexOf(start),source.indexOf(end,source.indexOf(start)));

test('desktop OPEN reveals files and opens directories without executing files or accepting external frames',async()=>{
  const main=fs.readFileSync(path.join(__dirname,'main.js'),'utf8');
  const code=main.slice(main.indexOf('ipcMain.handle("harness:openLocation"'),main.indexOf('ipcMain.handle("harness:saveJsonFile"'));
  let handle,kind='directory'; const opened=[],shown=[];
  vm.runInNewContext(code,{ipcMain:{handle:(name,callback)=>handle=callback},fromHarnessWindow:e=>e.trusted,path,
    fs:{realpathSync:{native:value=>value},statSync:()=>({isDirectory:()=>kind==='directory',isFile:()=>kind==='file'})},
    shell:{openPath:async value=>{opened.push(value);return '';},showItemInFolder:value=>shown.push(value)}});
  const root=path.resolve('portable destination');
  await handle({trusted:true},root);
  kind='file'; await handle({trusted:true},path.join(root,'script.exe'));
  assert.deepEqual(opened,[root]); assert.deepEqual(shown,[path.join(root,'script.exe')]);
  await assert.rejects(handle({trusted:false},root));
  await assert.rejects(handle({trusted:true},'https://example.test'));
});

test('actual menu dispatch, rename dialog, cancel, and OPEN buttons work with portable paths',async()=>{
  const root=path.join(__dirname,'build-output/win-unpacked/resources/runtime');
  const manifest=JSON.parse(fs.readFileSync(path.join(root,'NEXUS_RUNTIME.json'),'utf8'));
  const browser=await chromium.launch({headless:true,executablePath:path.join(root,'playwright',manifest.playwright.chromium_executable)});
  try {
    const page=await browser.newPage({viewport:{width:760,height:775}});
    await page.setContent('<button id="chat">Custom chat</button><main id="content"></main>');
    await page.addStyleTag({content:fs.readFileSync(path.join(__dirname,'../src/our_harness/ui/styles.css'),'utf8')});
    await page.addScriptTag({content:`
      function make(tag,cls='',text=''){const el=document.createElement(tag);el.className=cls;el.textContent=text;return el;}
      window.calls=[]; window.harnessDesktop={openLocation:async location=>calls.push(['open',location])};
      async function archiveConversationFor(a,c){calls.push(['archive',a,c]);}
      async function restoreConversationFor(a,c){calls.push(['restore',a,c]);}
      ${section('function requestChatName','async function manageConversationFor')}
      async function manageConversationFor(a,c,action){calls.push([action,a,c.id]);}
      ${section('let closeChatContextMenu','async function restoreConversationFor')}
      ${section('function chatDeliveryNotice','function normalizedLongHorizonCorrelation')}
      window.conversation={id:'chat-portable',name:'My game',pinned:false};
      const button=document.querySelector('#chat');
      button.addEventListener('contextmenu',e=>showChatContextMenu(e,'builder',conversation,button));
      appendChatDeliveryNotice(document.querySelector('#content'),{correlation:{schema_version:1,kind:'long_horizon_agent_event',delivery_contract:'selected-project-delivery/v1',delivery_state:'working_copy_report',delivery_project:${JSON.stringify(destination)}}});
    `});
    await page.getByRole('button',{name:'OPEN',exact:true}).click();
    assert.deepEqual(await page.evaluate(()=>calls),[['open',destination]]);
    await page.addScriptTag({content:`
      function appendLongHorizonGoalLink(){}
      function aChatTurnFace(){return make('span','');}
      ${section('function appendChatText','const chatPhaseNames')}
      ${section('function aChatGoalCompletionRow','// Only the engine')}
      document.querySelector('#content').append(aChatGoalCompletionRow(${JSON.stringify('Verified complete.\nDelivery checked in: '+destination+'\n'+deliveredFile)},'saved time',{goalId:'goal',deliveryLocations:${JSON.stringify([destination,deliveredFile])}},'fixture-completion'));
    `});
    const receipt=page.locator('.fixture-completion');
    assert.equal(await receipt.locator('details').evaluate(el=>el.open),true);
    await receipt.getByRole('button',{name:'OPEN',exact:true}).nth(1).click();
    assert.deepEqual(await page.evaluate(()=>calls.at(-1)),['open',deliveredFile]);
    for (const [label,action] of [['Archive','archive'],['Delete/Purge','purge'],['Rename','rename'],['Pin','pin']]) {
      await page.locator('#chat').click({button:'right'});
      await page.getByRole('menuitem',{name:label,exact:true}).click();
      assert.deepEqual(await page.evaluate(()=>calls.at(-1)),[action,'builder','chat-portable']);
      assert.equal(await page.getByRole('menu').count(),0);
    }
    await page.evaluate(()=>conversation.pinned=true);
    await page.locator('#chat').click({button:'right'});
    await page.getByRole('menuitem',{name:'Unpin',exact:true}).click();
    assert.deepEqual(await page.evaluate(()=>calls.at(-1)),['pin','builder','chat-portable']);
    const count=await page.evaluate(()=>calls.length);
    await page.locator('#chat').click({button:'right'}); await page.getByRole('menuitem',{name:'Cancel',exact:true}).click();
    await page.locator('#chat').click({button:'right'}); await page.keyboard.press('Escape');
    assert.equal(await page.evaluate(()=>calls.length),count);
    await page.evaluate(()=>{window.renamed=requestChatName('Old name');});
    await page.getByLabel('Chat name').fill('New custom name');
    await page.getByRole('button',{name:'Rename',exact:true}).click();
    assert.equal(await page.evaluate(()=>window.renamed),'New custom name');
    await page.setViewportSize({width:390,height:700});
    await page.locator('#chat').click({button:'right'});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    await page.screenshot({path:path.join(__dirname,'../.harness/runtime/chat-qol-menu.png')});
  } finally {await browser.close();}
});
