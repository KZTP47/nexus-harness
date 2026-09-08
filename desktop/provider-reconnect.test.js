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

test("saved chat reconnection reviews exact setup, refreshes bindings and leaves resume separate", {timeout:45000}, async () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(runtime, "NEXUS_RUNTIME.json"), "utf8"));
  const browser = await chromium.launch({executablePath:process.env.NEXUS_TEST_CHROMIUM || path.join(runtime, "playwright", manifest.playwright.chromium_executable),headless:true});
  const output = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-reconnect-ui-"));
  try {
    const page = await browser.newPage({viewport:{width:1264,height:850}});
    await page.setContent('<main style="max-width:1000px;margin:20px auto;padding:12px"><h1>Nexus Harness · Saved team chat</h1><section id="panel" class="swarm-chat-team-goal"></section><p id="status" role="status"></p></main>');
    await page.addStyleTag({content:fs.readFileSync(path.join(ui,"styles.css"),"utf8")});
    await page.addScriptTag({content:
      source.slice(source.indexOf("function appendProviderReconnectControl"), source.indexOf("async function activateConversationFor")) +
      source.slice(source.indexOf("function fillChatGoalPanel"), source.indexOf("function syncChatGoalControls")) + `
      function make(tag,cls='',text=''){const n=document.createElement(tag);n.className=cls;n.textContent=text;return n;}
      const swarmConversationSwitching=new Set();const theBigOne='';
      const swarmChatIsHydrating=()=>false;const swarmChatKey=id=>id;
      const setWhatCanBePressedInSwarm=()=>{};
      window.calls=[];window.fail=false;window.refreshed=[];
      const conversation={id:'exact-chat',binding_problem:{can_review_reconnect:true}};
      const review={schema_version:1,contract:'saved-chat-provider-reconnect/v1',fingerprint:'exact-fingerprint',
        message:'Reconnect this saved chat to the currently installed provider? Confirm that you signed in to the intended account. The existing transcript will be available to that provider when you resume. Saved work, budgets, and permissions are kept; interrupted calls still require their recovery checks.'};
      async function request(url,options){
        const body=JSON.parse(options.body);calls.push({url,body});
        if(body.confirmation){if(fail)throw Error('This reconnect review changed. Review the current setup again.');
          return {reconnected:true,message:'Saved chat reconnected. Use Resume team to continue.'};}
        return review;
      }
      async function loadConversationsFor(id){if(swarmConversationSwitching.has(id))throw Error('refresh is still blocked');refreshed.push('chats');}
      async function refreshLongGoals(){refreshed.push('goals');}
      async function refreshTheChatFor(id){refreshed.push('transcript');}
      function sayInBigChatConversationFor(id,message){document.getElementById('status').textContent=message;}
      window.render=()=>{const p=document.getElementById('panel');delete p.dataset.snapshot;
        fillChatGoalPanel(p,'original-agent',{goal:{goal_id:'saved-goal',revision:5,status:'paused'},
          problem:'This chat is paused because the provider executable changed. After signing in or updating the provider, review reconnection to continue this saved chat.',
          reconnectChat:conversation});};render();
    `});
    const reconnect = page.getByRole("button", {name:"Reconnect saved chat",exact:true});
    assert.equal(await reconnect.isVisible(),true);
    assert.deepEqual(await page.evaluate(()=>calls),[]);
    page.once("dialog", dialog=>dialog.dismiss());
    await reconnect.click();
    assert.equal(await page.evaluate(()=>calls.length),1);
    assert.deepEqual(await page.evaluate(()=>refreshed),[]);
    page.once("dialog", dialog=>dialog.accept());
    await reconnect.click();
    await page.getByRole('status').filter({hasText:'Use Resume team'}).waitFor();
    const sent = await page.evaluate(()=>calls.at(-1));
    assert.deepEqual(sent.body,{agent:'original-agent',chat:'exact-chat',confirmation:{schema_version:1,
      contract:'saved-chat-provider-reconnect/v1',fingerprint:'exact-fingerprint',decision:'reconnect'}});
    assert.deepEqual(await page.evaluate(()=>refreshed),['chats','goals','transcript']);
    assert.equal(await page.evaluate(()=>calls.some(c=>c.url.includes('control'))),false);
    await page.evaluate(()=>{fail=true;render();});
    page.once("dialog", dialog=>dialog.accept());
    await reconnect.click();
    await page.getByRole('status').filter({hasText:'review changed'}).waitFor();
    for (const width of [1264,390]) {
      await page.setViewportSize({width,height:850});
      assert.equal(await page.locator('#panel').evaluate(n=>n.scrollWidth<=n.clientWidth+1),true);
      await page.screenshot({path:path.join(output,'reconnect-'+width+'.png'),fullPage:true});
    }
    console.log('Reconnect screenshots: '+output);
  } finally {await browser.close();}
});
