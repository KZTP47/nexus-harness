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

test("interrupted chat shows actionable recovery, preserves approvals and rejects stale choices", {timeout:45000}, async () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(runtime, "NEXUS_RUNTIME.json"), "utf8"));
  const browser = await chromium.launch({executablePath:path.join(runtime, "playwright", manifest.playwright.chromium_executable),headless:true});
  const output=fs.mkdtempSync(path.join(os.tmpdir(),"nexus-recovery-ui-"));
  try {
    const page=await browser.newPage({viewport:{width:1264,height:850}});
    await page.setContent('<main style="max-width:1000px;margin:20px auto"><h1>Team chat</h1><section id="panel" class="swarm-chat-team-goal"></section></main>');
    await page.addStyleTag({content:fs.readFileSync(path.join(ui,"styles.css"),"utf8")});
    await page.addScriptTag({content:source.slice(source.indexOf("function appendGoalAccessControls"),source.indexOf("function fillChatGoalPanel"))+`
      function make(tag,cls='',text=''){const n=document.createElement(tag);n.className=cls;n.textContent=text;return n;}
      window.calls=[];window.fail=false;
      window.inspected=[];async function openChatGoalDetails(goal){inspected.push(goal.goal_id);}
      const goal={goal_id:'saved-goal',revision:7,status:'paused',tasks:[],agent_access:{mode:'full'},command_request:{state:'approved'}};
      async function request(url,options){
        if(!options){calls.push({url});return {goal_id:goal.goal_id,revision:goal.revision,project_path:'Portable project',commands:[['npm','run','test']],approval_digest:'a'.repeat(64)};}
        const body=JSON.parse(options.body);calls.push({url,body});
        if(window.fail)throw new Error('The interrupted call changed. Refresh this chat before choosing recovery.');
        if(url.endsWith('/access'))goal.revision++;
        else {goal.status='running';delete goal.resume_recovery;}
        return {goal:{...goal}};
      }
      window.render=()=>{const p=document.getElementById('panel');p.replaceChildren();appendGoalAccessControls(p,goal,async()=>render(),{chat_id:'exact-chat',project_id:'project',participant_ids:['a','b']});};
      window.reset=(safe=false)=>{calls=[];fail=false;goal.status='paused';goal.resume_recovery={schema_version:1,fingerprint:'exact-fingerprint',can_retry:true,resume_safe:safe,message:safe?'Resume team will request a fresh read-only reply.':'Review recovery below; command permission is a separate setting.',items:[{agent_name:'Example agent',reason:'The agent call was interrupted before its reply was saved.'}]};render();};reset();
    `});
    const retry=page.getByRole("button",{name:"Retry interrupted agent call",exact:true});
    assert.equal(await retry.isDisabled(),true);
    assert.deepEqual(await page.evaluate(()=>calls),[]); // no irrelevant approval request
    assert.match(await page.locator('#panel').innerText(),/interrupted/);
    await page.getByRole('checkbox').check();
    await retry.click();
    assert.equal(await page.evaluate(()=>calls[0].body.goal_id),'saved-goal');
    assert.equal(await page.evaluate(()=>calls[0].body.chat_id),'exact-chat');
    assert.deepEqual(await page.evaluate(()=>calls[0].body.payload),{expected_revision:7,recovery:{schema_version:1,fingerprint:'exact-fingerprint',decision:'retry_provider'}});
    assert.equal(await page.getByLabel('Agent access for this chat').inputValue(),'full');
    await page.evaluate(()=>reset(true));
    await page.getByRole('button',{name:'Resume interrupted turn',exact:true}).click();
    assert.equal(await page.evaluate(()=>calls[0].body.payload.recovery),undefined);
    await page.evaluate(()=>{reset();fail=true;});
    await page.getByRole('checkbox').check();await retry.click();
    assert.match(await page.locator('.chat-goal-recovery [role=status]').innerText(),/call changed/);
    assert.equal(await page.locator('.chat-goal-recovery').isVisible(),true);
    await page.evaluate(()=>reset());
    await page.getByRole("button",{name:"Review command permissions"}).click();
    await page.getByRole('button',{name:'Always allow this command in this chat',exact:true}).click();
    assert.equal(await page.evaluate(()=>calls.filter(c=>c.url.endsWith('/control')).length),0);
    assert.equal(await retry.isVisible(),true);
    await page.evaluate(()=>{reset();goal.resume_recovery.can_retry=false;render();});
    await page.getByRole("button",{name:"Inspect saved work",exact:true}).click();
    assert.deepEqual(await page.evaluate(()=>inspected),['saved-goal']);
    assert.equal(await page.evaluate(()=>calls.filter(c=>c.url.endsWith('/control')).length),0);
    for(const width of [1264,390]){
      await page.evaluate(()=>reset());await page.setViewportSize({width,height:850});
      assert.equal(await page.locator('.chat-goal-recovery').evaluate(n=>n.scrollWidth<=n.clientWidth+1),true);
      await page.screenshot({path:path.join(output,'recovery-'+width+'.png'),fullPage:true});
    }
    console.log('Recovery screenshots: '+output);
  }finally{await browser.close();}
});
