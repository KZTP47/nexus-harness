'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {chromium}=require('playwright-core');
const {findInstalledBrowser}=require('./external-browser');
const worker=require('./email-browser-worker');
const {createFixture}=require('./email-browser-fixture.cjs');
const connectionId='a'.repeat(32);
const submissionId='b'.repeat(32);
const connection=(provider,mode='headless')=>({id:connectionId,provider,email:'owner@example.test',browser_mode:mode});
async function fixtureSession(provider,mode='headless',state={}) {
  const profile=fs.mkdtempSync(path.join(os.tmpdir(),'nexus-browser-send-'));
  const fixture=createFixture({state:{provider,...state}});
  const context=await chromium.launchPersistentContext(profile,{executablePath:findInstalledBrowser().executable,headless:mode==='headless'});
  try {
    await context.route('**/*',fixture.route);
    const page=context.pages()[0];
    await page.goto(provider==='browser_gmail'?'https://mail.google.com/mail/u/0/#inbox':'https://outlook.office.com/mail/inbox');
    // Finish headed-window startup before measuring the worker's bounded clicks.
    await page.bringToFront();
    await page.locator(provider==='browser_gmail'?'tr[data-legacy-thread-id]':'[role="option"][data-convid]').click({trial:true,timeout:15000});
    const value={context,page,profile,provider,mode};
    const request={command:'sync',connection:connection(provider,mode)};
    const incoming=(await worker.operate(value,request)).messages[0];
    return {value,incoming,fixture,request,async close(){await context.close();fs.rmSync(profile,{recursive:true,force:true});}};
  } catch(error) {
    // Setup can fail before the caller receives its close method. Never leave
    // a browser child keeping the test process alive after a reported failure.
    await context.close();
    fs.rmSync(profile,{recursive:true,force:true});
    throw new Error(`${provider}/${mode} fixture setup: ${error.message}`,{cause:error});
  }
}
test('reviewed Outlook and Gmail replies verify original, recipient and exact body in headed and headless Chromium',{timeout:90000},async()=>{
  for(const provider of ['browser_outlook','browser_gmail'])for(const mode of ['headed','headless']) {
    const f=await fixtureSession(provider,mode);
    try {
      assert.equal(f.incoming.browser_reference.contract,'browser-reply/v1');
      const body='Reviewed reply with Unicode åäö.\n\nExactly this text.';
      const send={...f.request,command:'send',incoming:f.incoming,body,submission_id:submissionId};
      const result=await worker.operate(f.value,send);
      assert.equal(result.status,'sent',JSON.stringify({result,composer:result.status==='not_sent'?await f.value.page.evaluate(worker.readComposer,provider):null,html:result.status==='not_sent'?await f.value.page.locator('[contenteditable]').innerHTML():null}));
      assert.equal(result.evidence,'ui_acknowledgement');
      assert.deepEqual(f.fixture.state.lastSent,{body,recipient:'sender@example.test'});
      assert.equal(f.fixture.state.sendCount,1);
      const duplicate=await worker.operate(f.value,send);
      assert.equal(duplicate.status,'sent');
      assert.equal(duplicate.evidence,'persisted_ui_acknowledgement');
      assert.equal(f.fixture.state.sendCount,1);
      const intent=fs.readFileSync(path.join(f.value.profile,'nexus-reviewed-submissions',submissionId+'.json'),'utf8');
      assert.ok(!intent.includes(body)&&!intent.includes('owner@example.test'),'private receipt stores hashes, not message contents');
    }finally{await f.close();}
  }
});
test('source, mailbox, recipient, existing composer and post-fill recipient mismatches never click Send',{timeout:90000},async()=>{
  for(const provider of ['browser_outlook','browser_gmail']) {
    const f=await fixtureSession(provider);
    try {
      const send={...f.request,command:'send',incoming:f.incoming,body:'Approved',submission_id:submissionId};
      assert.equal((await worker.operate(f.value,{...send,connection:{...send.connection,email:'other@example.test'}})).status,'not_sent');
      f.fixture.state.body='Changed original content';
      await f.value.page.reload();
      assert.equal((await worker.operate(f.value,send)).status,'not_sent');
      f.fixture.state.body=f.incoming.body;f.fixture.state.recipient='other@example.test';
      await f.value.page.reload();
      assert.equal((await worker.operate(f.value,send)).status,'not_sent');
      f.fixture.state.recipient='sender@example.test';f.fixture.state.changeRecipientOnInput=true;
      await f.value.page.reload();
      assert.equal((await worker.operate(f.value,send)).status,'not_sent');
      f.fixture.state.changeRecipientOnInput=false;f.fixture.state.hiddenBcc=true;
      await f.value.page.reload();
      assert.match((await worker.operate(f.value,send)).error,/BCC recipients/);
      f.fixture.state.hiddenBcc=false;
      f.fixture.state.changeRecipientOnInput=false;f.fixture.state.existingComposer=true;
      await f.value.page.reload();
      assert.match((await worker.operate(f.value,send)).error,/existing reply composer/);
      assert.equal(f.fixture.state.sendCount,0);
      assert.equal(fs.existsSync(path.join(f.value.profile,'nexus-reviewed-submissions',submissionId+'.json')),false);
    }finally{await f.close();}
  }
});
test('uncertain UI acknowledgement persists dispatch intent and restart cannot resend',{timeout:45000},async()=>{
  const f=await fixtureSession('browser_outlook','headless',{acknowledge:false});
  try {
    const send={...f.request,command:'send',incoming:f.incoming,body:'Reviewed but uncertain',submission_id:submissionId};
    const first=await worker.operate(f.value,send);
    assert.equal(first.status,'unknown');assert.equal(f.fixture.state.sendCount,1);
    await f.value.context.close();
    const restarted=await chromium.launchPersistentContext(f.value.profile,{executablePath:findInstalledBrowser().executable,headless:true});
    await restarted.route('**/*',f.fixture.route);f.value.context=restarted;f.value.page=restarted.pages()[0];
    const retry=await worker.operate(f.value,send);
    assert.equal(retry.status,'unknown');assert.equal(retry.evidence,'persisted_dispatch_intent');
    assert.equal(f.fixture.state.sendCount,1);
    assert.equal((await worker.operate(f.value,{...send,body:'Different body'})).status,'unknown');
    fs.writeFileSync(path.join(f.value.profile,'nexus-reviewed-submissions',submissionId+'.json'),'{truncated');
    assert.equal((await worker.operate(f.value,send)).status,'unknown');
    await restarted.close();
  }finally{await f.close();}
});
test('legacy sync cursor rescans references and oversized originals are never truncated',{timeout:45000},async()=>{
  const f=await fixtureSession('browser_gmail');
  try {
    const legacy=await worker.operate(f.value,{...f.request,cursor:JSON.stringify({seen:[f.incoming.source_id],offset:0})});
    assert.equal(legacy.messages.length,1);assert.ok(legacy.messages[0].browser_reference);
    assert.equal(JSON.parse(legacy.cursor).contract,'browser-sync/v3');
    await f.value.page.setContent('<h2 class="hP">Subject</h2><article data-legacy-message-id="huge"><span class="gD" email="sender@example.test">Sender</span><div class="a3s">'+ 'x'.repeat(100001)+'</div></article>');
    await assert.rejects(f.value.page.evaluate(worker.readMessage,'browser_gmail'),/complete browser import size limit/);
  }finally{await f.close();}
});

test('empty zero-height Outlook body scaffolds do not replace the original or weaken send validation',{timeout:45000},async()=>{
  const f=await fixtureSession('browser_outlook');
  try {
    await f.value.context.route('**/*',async route=>{
      if(new URL(route.request().url()).pathname==='/__nexus_test_send__')return f.fixture.route(route);
      const script=`<script>const originalOpenMessage=openMessage;openMessage=()=>{originalOpenMessage();const scaffold=document.createElement('div');scaffold.setAttribute('role','document');scaffold.setAttribute('aria-label','Message body');scaffold.style.cssText='display:block;visibility:visible;width:675px;height:0px';document.querySelector('#pane article').append(scaffold);document.querySelector('#pane').append(scaffold.cloneNode(true));};</script>`;
      return route.fulfill({contentType:'text/html',body:f.fixture.html()+script});
    });
    await f.value.page.reload();
    const incoming=(await worker.operate(f.value,{...f.request,cursor:''})).messages[0];
    assert.equal(incoming.body,f.incoming.body);
    assert.equal(incoming.source_id,f.incoming.source_id);
    const row=f.value.page.locator('[role="option"][data-convid]');await row.click();
    const geometries=await f.value.page.locator('[role="document"]').evaluateAll(elements=>elements.map(e=>({height:e.getBoundingClientRect().height,rectangles:e.getClientRects().length,text:e.innerText.length})));
    assert.equal(geometries.length,3);assert.ok(geometries[0].height>0);
    assert.ok(geometries.slice(1).every(value=>value.height===0&&value.rectangles===1&&value.text===0));
    assert.equal((await f.value.page.evaluate(worker.readMessage,'browser_outlook')).message_id,f.incoming.message_id);
    const result=await worker.operate(f.value,{...f.request,command:'send',incoming,body:'Reviewed reply with complete source verification.',submission_id:submissionId});
    assert.equal(result.status,'sent',JSON.stringify(result));assert.equal(f.fixture.state.sendCount,1);
    await f.value.page.reload();await row.click();
    await f.value.page.locator('#pane article').evaluate(article=>{const body=document.createElement('div');body.setAttribute('role','document');body.textContent='Second visible message body';article.append(body);});
    await assert.rejects(f.value.page.evaluate(worker.readMessage,'browser_outlook'),/cannot be read reliably/);
  }finally{await f.close();}
});

test('Outlook editable To chips are scoped and populated Cc or Bcc containers block sending',{timeout:45000},async()=>{
  const f=await fixtureSession('browser_outlook');
  let extra='',wrong=false;
  try {
    await f.value.context.route('**/*',async route=>{
      if(new URL(route.request().url()).pathname==='/__nexus_test_send__')return f.fixture.route(route);
      const script=`<script>const previousOpenReply=openReply;openReply=()=>{previousOpenReply();const old=document.querySelector('#reply-compose>span');const container=document.createElement('div');container.setAttribute('contenteditable','true');container.setAttribute('aria-label','To');const chip=document.createElement('span');chip.setAttribute('contenteditable','false');chip.setAttribute('aria-label',${JSON.stringify(wrong?'Wrong person wrong@example.test':'Sender sender@example.test')});chip.textContent='Sender';container.append(chip);old.replaceWith(container);${extra?`const other=container.cloneNode(true);other.setAttribute('aria-label',${JSON.stringify(extra)});other.style.display='none';document.querySelector('#reply-compose').append(other);`:''}};
      const withChips=openReply;openReply=()=>{withChips();const empty=document.createElement('div');empty.setAttribute('contenteditable','true');empty.setAttribute('aria-label','Cc');empty.style.display='none';document.querySelector('#reply-compose').append(empty);};
      sendReply=async()=>{const body=document.querySelector('[role=textbox][contenteditable]').innerText;await fetch('/__nexus_test_send__',{method:'POST',body:JSON.stringify({body,recipient:'sender@example.test'})});document.querySelector('#compose').replaceChildren();document.querySelector('#receipt').innerHTML='<div role="status">Message sent.</div>';};</script>`;
      return route.fulfill({contentType:'text/html',body:f.fixture.html()+script});
    });
    const request={...f.request,command:'send',incoming:f.incoming,body:'Explicitly reviewed reply.',submission_id:submissionId};
    for(const channel of ['Cc','Bcc']) {
      extra=channel;await f.value.page.reload();
      const rejected=await worker.operate(f.value,request);
      assert.equal(rejected.status,'not_sent');assert.match(rejected.error,/CC or BCC/);
      assert.equal(f.fixture.state.sendCount,0);
    }
    extra='';wrong=true;await f.value.page.reload();
    const mismatch=await worker.operate(f.value,request);
    assert.equal(mismatch.status,'not_sent');assert.match(mismatch.error,/recipient differs/);
    wrong=false;await f.value.page.reload();
    const sent=await worker.operate(f.value,request);
    assert.equal(sent.status,'sent',JSON.stringify(sent));assert.equal(f.fixture.state.sendCount,1);
    assert.equal(f.fixture.state.lastSent.body,request.body);
  }finally{await f.close();}
});

test('send waits for the exact original when a clicked conversation temporarily retains readable stale mail',{timeout:45000},async()=>{
  for(const provider of ['browser_outlook','browser_gmail']) {
    const f=await fixtureSession(provider);
    try {
      await f.value.context.route('**/*',async route=>{
        if(new URL(route.request().url()).pathname==='/__nexus_test_send__')return f.fixture.route(route);
        const gmail=provider==='browser_gmail';
        const script=`<script>const settledOpenMessage=openMessage;openMessage=()=>{settledOpenMessage();document.querySelector('#pane ${gmail?'.a3s':'[role=document]'}').textContent='Previous readable email body';document.querySelector('#pane article').setAttribute('${gmail?'data-legacy-message-id':'data-message-id'}','previous-message');document.querySelector('#pane ${gmail?'.gD':'[email]'}').setAttribute('email','previous@example.test');document.querySelector('#pane h2').textContent='Previous subject';window.stalePaneShown=true;setTimeout(()=>{settledOpenMessage();window.expectedPaneReady=true;},450);};</script>`;
        return route.fulfill({contentType:'text/html',body:f.fixture.html()+script});
      });
      await f.value.page.reload();
      const result=await worker.operate(f.value,{...f.request,command:'send',incoming:f.incoming,body:'Approved after exact original appears.',submission_id:submissionId});
      assert.equal(result.status,'sent',JSON.stringify(result));
      assert.equal(await f.value.page.evaluate(()=>window.stalePaneShown&&window.expectedPaneReady),true);
      assert.equal(f.fixture.state.sendCount,1);
    }finally{await f.close();}
  }
});
test('saved Outlook draft after an original is excluded from sync and cannot supply its Reply control',{timeout:30000},async()=>{
  const f=await fixtureSession('browser_outlook');
  try{
    await f.value.context.route('**/*',async route=>{
      if(new URL(route.request().url()).pathname==='/__nexus_test_send__')return f.fixture.route(route);
      const script=`<script>const originalOpenMessage=openMessage;openMessage=()=>{originalOpenMessage();const original=document.querySelector('#pane article');original.removeAttribute('data-message-id');const draft=original.cloneNode(true);draft.querySelector('[id$="_FROM"]').id='MSG_saved-draft_FROM';draft.querySelector('[id$="_FROM"]').removeAttribute('email');draft.querySelector('[role=document]').textContent='Unsent draft text';draft.insertAdjacentHTML('afterbegin',"<div>This message hasn't been sent.</div>");draft.querySelector('[aria-label=Reply]').onclick=()=>{window.wrongReply=true;};original.after(draft);};</script>`;
      return route.fulfill({contentType:'text/html',body:f.fixture.html()+script});
    });
    await f.value.page.reload();
    const scanned=await worker.operate(f.value,f.request);
    assert.equal(scanned.messages.length,1);assert.equal(scanned.messages[0].source_id,f.incoming.source_id);
    assert.equal(scanned.messages[0].body,f.incoming.body);
    const result=await worker.operate(f.value,{...f.request,command:'send',incoming:f.incoming,body:'Exact reviewed reply.',submission_id:submissionId});
    assert.equal(result.status,'sent',JSON.stringify(result));assert.equal(f.fixture.state.sendCount,1);
    assert.equal(await f.value.page.evaluate(()=>!!window.wrongReply),false);
  }finally{await f.close();}
});

test('Focused and Other polling alternates and an Other reply reopens its original tab',{timeout:30000},async()=>{
  const f=await fixtureSession('browser_outlook');
  try{
    await f.value.context.route('**/*',async route=>{
      if(new URL(route.request().url()).pathname==='/__nexus_test_send__')return f.fixture.route(route);
      const script=`<script>const inboxRow=document.querySelector('[role=option]');const slot=document.createElement('section');inboxRow.before(slot);slot.append(inboxRow);const savedRow=slot.innerHTML;const tabs=document.createElement('div');tabs.innerHTML='<button role="tab" aria-selected="true">Focused</button><button role="tab" aria-selected="false">Other</button>';slot.before(tabs);const activate=name=>{tabs.querySelectorAll('button').forEach(t=>t.setAttribute('aria-selected',String(t.textContent===name)));slot.innerHTML=name==='Other'?savedRow:'<h2>Your inbox is empty.</h2>';};tabs.querySelectorAll('button').forEach(t=>t.onclick=()=>activate(t.textContent));activate('Focused');</script>`;
      return route.fulfill({contentType:'text/html',body:f.fixture.html()+script});
    });
    await f.value.page.reload();
    const other=await worker.operate(f.value,f.request);
    assert.equal(other.messages.length,1);assert.equal(other.messages[0].browser_reference.inbox_tab,'Other');
    const result=await worker.operate(f.value,{...f.request,command:'send',incoming:other.messages[0],body:'Reviewed Other reply.',submission_id:submissionId});
    assert.equal(result.status,'sent',JSON.stringify(result));assert.equal(f.fixture.state.sendCount,1);
  }finally{await f.close();}
});

test('Outlook proofing blocks preserve exact logical blank lines and spaces',async()=>{
  const f=await fixtureSession('browser_outlook');
  try{
    await f.value.page.evaluate(()=>{openReply();const editor=document.querySelector('[contenteditable]');editor.innerHTML='<div class="elementToProof">Greeting  with spaces</div><div class="elementToProof"><br></div><div class="elementToProof">1. First</div><div class="elementToProof">2. Next</div>';});
    assert.equal((await f.value.page.evaluate(worker.readComposer,'browser_outlook')).body,'Greeting  with spaces\n\n1. First\n2. Next');
    await f.value.page.evaluate(()=>document.querySelector('[contenteditable]').insertAdjacentHTML('beforeend','<div class="elementToProof"><br></div>'));
    assert.equal((await f.value.page.evaluate(worker.readComposer,'browser_outlook')).body,'Greeting  with spaces\n\n1. First\n2. Next\n');
  }finally{await f.close();}
});

test('late quoted-thread hydration settles before keyboard entry of the approved reply',{timeout:30000},async()=>{
  const f=await fixtureSession('browser_outlook');
  try{
    await f.value.context.route('**/*',async route=>{
      if(new URL(route.request().url()).pathname==='/__nexus_test_send__')return f.fixture.route(route);
      const script=`<script>const initialOpenReply=openReply;openReply=()=>{initialOpenReply();const editor=document.querySelector('[contenteditable]');window.entryBeforeReady=false;window.keyCount=0;editor.addEventListener('input',()=>{if(!window.quoteReady)window.entryBeforeReady=true;});editor.addEventListener('keydown',()=>window.keyCount++);setTimeout(()=>{editor.innerHTML='<hr><div>Quoted original</div><table><tr><td>From: synthetic sender</td></tr></table>';window.quoteReady=true;},600);};</script>`;
      return route.fulfill({contentType:'text/html',body:f.fixture.html()+script});
    });
    await f.value.page.reload();
    const body='Reviewed greeting.\n\n1. First\n2. Second\nClosing.';
    const result=await worker.operate(f.value,{...f.request,command:'send',incoming:f.incoming,body,submission_id:submissionId});
    assert.equal(result.status,'sent',JSON.stringify(result));
    assert.equal(await f.value.page.evaluate(()=>window.quoteReady&&!window.entryBeforeReady&&window.keyCount>20),true);
    assert.equal(f.fixture.state.lastSent.body,body);assert.equal(f.fixture.state.sendCount,1);
  }finally{await f.close();}
});

test('mode changes reuse private profile, open forces visible, and operations serialize before relaunch',{timeout:90000},async()=>{
  const profile=fs.mkdtempSync(path.join(os.tmpdir(),'nexus-browser-modes-'));
  const fixture=createFixture();const original=chromium.launchPersistentContext.bind(chromium);const modes=[];const contexts=[];
  chromium.launchPersistentContext=async(directory,options)=>{
    const context=await original(directory,options);modes.push(options.headless);contexts.push(context);
    await context.route('**/*',fixture.route);return context;
  };
  try {
    const request={profile,connection:connection('browser_outlook'),command:'status'};
    assert.equal((await worker.handle(request)).actual_browser_mode,'headless');
    const first=await worker.handle({...request,command:'sync'});
    const send=worker.handle({...request,command:'send',incoming:first.messages[0],body:'Queued operation test',submission_id:submissionId});
    const opened=worker.handle({...request,command:'open'});
    assert.equal((await send).status,'sent');
    assert.equal((await opened).actual_browser_mode,'headed');
    assert.equal(contexts[0].pages().length,0,'mode change closes previous context after sending');
    assert.equal((await worker.handle(request)).actual_browser_mode,'headless');
    assert.deepEqual(modes,[true,false,true]);
    assert.equal(fixture.state.sendCount,1);
    assert.equal((await worker.handle({...request,command:'send',incoming:first.messages[0],body:'Queued operation test',submission_id:submissionId})).evidence,'persisted_ui_acknowledgement');
    fixture.state.existingComposer=true;
    await contexts.at(-1).pages()[0].reload();
    await assert.rejects(worker.handle({...request,command:'open'}),/existing reply composer/);
    await assert.rejects(worker.handle({...request,command:'sync'}),/existing reply composer/);
    assert.equal(modes.length,3,'open composer prevents mode relaunch');
    assert.equal((await worker.handle({...request,connection:{...request.connection,browser_mode:'invalid'},command:'send',incoming:first.messages[0],body:'body',submission_id:'c'.repeat(32)})).status,'not_sent');
    fixture.state.existingComposer=false;
    await contexts.at(-1).close();
    assert.equal((await worker.handle({...request,command:'open'})).actual_browser_mode,'headed');
    await contexts.at(-1).close();
    assert.equal((await worker.handle(request)).actual_browser_mode,'headless','closing visible sign-in allows background mode to resume');
    await worker.close();
  }finally{chromium.launchPersistentContext=original;await worker.close();fs.rmSync(profile,{recursive:true,force:true});}
});

test('explicit sign-in survives background headless preference until mailbox authentication completes',{timeout:90000},async()=>{
  const profile=fs.mkdtempSync(path.join(os.tmpdir(),'nexus-browser-signin-'));
  const fixture=createFixture();const original=chromium.launchPersistentContext.bind(chromium);const modes=[];const contexts=[];
  let authenticated=false;
  chromium.launchPersistentContext=async(directory,options)=>{
    const context=await original(directory,options);modes.push(options.headless);contexts.push(context);
    await context.route('**/*',route=>{
      if(authenticated)return fixture.route(route);
      return route.fulfill({contentType:'text/html',body:'<main><h1>Complete sign-in</h1><input aria-label="Verification code"></main>'});
    });
    return context;
  };
  try {
    const request={profile,connection:connection('browser_outlook'),command:'status'};
    const opened=await worker.session({...request,command:'open'});
    await opened.page.goto('https://login.microsoftonline.com/common/mfa');
    await opened.page.getByLabel('Verification code').fill('unfinished');
    const background=await worker.session(request);
    assert.equal(background===opened,true,'a pending explicit sign-in must not be replaced by headless polling');
    assert.equal((await worker.handle(request)).state,'sign_in_required');
    await assert.rejects(worker.handle({...request,command:'sync'}),/sign-in expired|identity is unavailable/);
    assert.equal(await opened.page.getByLabel('Verification code').inputValue(),'unfinished');
    assert.deepEqual(modes,[false]);
    authenticated=true;
    await opened.page.goto('https://outlook.office.com/mail/inbox');
    const connected=await worker.handle(request);
    assert.equal(connected.state,'connected');
    assert.equal(connected.actual_browser_mode,'headless');
    assert.deepEqual(modes,[false,true]);
    authenticated=false;
    await contexts.at(-1).pages()[0].goto('https://login.microsoftonline.com/common/mfa');
    const hiddenFailure=await worker.session(request);
    assert.equal(hiddenFailure.mode,'headless');
    assert.deepEqual(modes,[false,true],'authentication failure must not open a visible window automatically');
  }finally{chromium.launchPersistentContext=original;await worker.close();fs.rmSync(profile,{recursive:true,force:true});}
});

test('an Outlook self-reload during a send keeps the conversation the send opened, and clears it again afterwards',{timeout:45000},async()=>{
  const f=await fixtureSession('browser_outlook');
  try{
    worker.watchReloads(f.value);
    let duringSend;
    const sent=f.fixture.route;
    // The load event is raised directly because a real reload would abort the send this scoping exists for.
    f.value.context.route('**/*',async route=>{
      if(new URL(route.request().url()).pathname==='/__nexus_test_send__'){
        f.value.paneDirty=true;f.value.lastOpenedRow='thread-one';f.value.lastInboxRefresh=Date.now()-60000;f.value.ownLoadPending=false;
        f.value.page.emit('load');
        duringSend={replying:f.value.replying,lastOpenedRow:f.value.lastOpenedRow,paneDirty:f.value.paneDirty};
      }
      return sent(route);
    });
    const result=await worker.operate(f.value,{...f.request,command:'send',incoming:f.incoming,body:'Reviewed reply.',submission_id:submissionId});
    assert.equal(result.status,'sent');
    assert.deepEqual(duringSend,{replying:true,lastOpenedRow:'thread-one',paneDirty:false},'a reload mid-send discards the pane but keeps the row the send stamped');
    f.value.paneDirty=true;f.value.lastOpenedRow='thread-one';f.value.lastInboxRefresh=Date.now()-60000;f.value.ownLoadPending=false;
    f.value.page.emit('load');
    assert.equal(f.value.lastOpenedRow,'','outside a send the reload clears the open conversation as well');
  }finally{await f.close();}
});
