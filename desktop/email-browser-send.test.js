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
      const script=`<script>const initialOpenReply=openReply;openReply=()=>{initialOpenReply();const editor=document.querySelector('[contenteditable]');window.entryBeforeReady=false;window.keyCount=0;editor.addEventListener('input',()=>{if(!window.quoteReady)window.entryBeforeReady=true;window.keyCount++;});setTimeout(()=>{editor.innerHTML='<hr><div>Quoted original</div><table><tr><td>From: synthetic sender</td></tr></table>';window.quoteReady=true;},600);};</script>`;
      return route.fulfill({contentType:'text/html',body:f.fixture.html()+script});
    });
    await f.value.page.reload();
    const body='Reviewed greeting.\n\n1. First\n2. Second\nClosing.';
    const result=await worker.operate(f.value,{...f.request,command:'send',incoming:f.incoming,body,submission_id:submissionId});
    assert.equal(result.status,'sent',JSON.stringify(result));
    assert.equal(await f.value.page.evaluate(()=>window.quoteReady&&!window.entryBeforeReady&&window.keyCount>0),true);
    assert.equal(f.fixture.state.lastSent.body,body);assert.equal(f.fixture.state.sendCount,1);
  }finally{await f.close();}
});

test('a new exact Sent Items message confirms sending without a toast while another draft stays open',{timeout:45000},async()=>{
  // An earlier reply gives the conversation a rendered Sent Items baseline.
  const f=await fixtureSession('browser_outlook','headless',{sentFolder:true,acknowledge:false,leaveComposer:true,sendCount:1,lastSent:{body:'An earlier different reply',recipient:'sender@example.test'}});
  try {
    const request={...f.request,command:'send',incoming:f.incoming,body:'Exact approved text\n\n  spaces and <markup>',submission_id:submissionId};
    const result=await worker.operate(f.value,request);
    assert.equal(result.status,'sent',JSON.stringify(result));
    assert.equal(result.evidence,'sent_folder_new_message');
    assert.equal(f.fixture.state.sendCount,2);
    assert.equal(f.value.context.pages().length,1,'verification tab always closes');
    assert.equal((await worker.operate(f.value,request)).status,'sent');
    assert.equal(f.fixture.state.sendCount,2);
  }finally{await f.close();}
});

test('an older identical sent reply never confirms a dropped Send click',{timeout:50000},async()=>{
  const body='Previously sent identical reply';
  const f=await fixtureSession('browser_outlook','headless',{sentFolder:true,acknowledge:false,dropSend:true,sendCount:1,lastSent:{body,recipient:'sender@example.test'}});
  try {
    const result=await worker.operate(f.value,{...f.request,command:'send',incoming:f.incoming,body,submission_id:submissionId});
    assert.equal(result.status,'unknown');assert.equal(f.fixture.state.sendCount,1);
    const receipt=JSON.parse(fs.readFileSync(path.join(f.value.profile,'nexus-reviewed-submissions',submissionId+'.json')));
    assert.equal(receipt.failure_stage,'confirming the sent reply');
    assert.equal(f.value.context.pages().length,1);
  }finally{await f.close();}
});

test('a disabled Send button leaves no dispatch receipt and remains retryable',{timeout:40000},async()=>{
  const f=await fixtureSession('browser_outlook');
  try {
    await f.value.page.evaluate(()=>{const open=window.openReply;window.openReply=()=>{open();document.querySelector('button[aria-label="Send"]').disabled=true;};});
    const request={...f.request,command:'send',incoming:f.incoming,body:'Approved',submission_id:submissionId};
    const result=await worker.operate(f.value,request);
    assert.equal(result.status,'not_sent');assert.match(result.error,/checking the Send button/);
    assert.equal(fs.existsSync(path.join(f.value.profile,'nexus-reviewed-submissions',submissionId+'.json')),false);
    assert.equal(f.fixture.state.sendCount,0);
    await f.value.page.locator('button[aria-label="Send"]').evaluate(e=>e.disabled=false);
    assert.equal((await worker.operate(f.value,request)).status,'sent');assert.equal(f.fixture.state.sendCount,1);
  }finally{await f.close();}
});

test('Sent Items evidence rejects stale dates, wrong recipients, drafts and quoted copies',{timeout:15000},async()=>{
  const f=await fixtureSession('browser_outlook');
  try {
    const body='Exact approved reply';
    await f.value.page.setContent('<article><span id="MSG_new_FROM">Owner</span><div id="MSG_new_TO">To: sender@example.test</div><div id="MSG_new_DATETIME">Today 14:16</div><div role="document"><pre>Exact approved reply</pre><div>Company classification</div></div></article>');
    const options={body,recipient:'sender@example.test',before:{ids:['old'],stamps:['Yesterday']}};
    assert.ok(await f.value.page.evaluate(worker.sentBodyWitness,options));
    for(const changed of [{body:'Different'}, {recipient:'other@example.test'}, {before:{ids:['new'],stamps:[]}}, {before:{ids:[],stamps:['Today 14:16']}}])assert.equal(await f.value.page.evaluate(worker.sentBodyWitness,{...options,...changed}),null);
    await f.value.page.locator('[role="document"]').evaluate(e=>e.insertAdjacentHTML('afterbegin','Unapproved introductory prose'));
    assert.equal(await f.value.page.evaluate(worker.sentBodyWitness,options),null);
    await f.value.page.locator('[role="document"]').evaluate(e=>e.innerHTML='<blockquote><pre>Exact approved reply</pre></blockquote>');
    assert.equal(await f.value.page.evaluate(worker.sentBodyWitness,options),null);
    await f.value.page.locator('[role="document"]').evaluate(e=>e.innerHTML='<pre>Exact approved reply</pre>');
    await f.value.page.locator('article').evaluate(e=>e.insertAdjacentHTML('afterbegin',"<p>This message hasn't been sent.</p>"));
    assert.equal(await f.value.page.evaluate(worker.sentBodyWitness,options),null);
  }finally{await f.close();}
});

test('long replies preserve literal markup and whitespace in a slow proofing editor',{timeout:45000},async()=>{
  for(const provider of ['browser_outlook','browser_gmail']) {
    const f=await fixtureSession(provider);
    try {
      await f.value.context.route('**/*',async route=>{
        if(new URL(route.request().url()).pathname==='/__nexus_test_send__')return f.fixture.route(route);
        const script=`<script>const previousOpenReply=openReply;openReply=()=>{previousOpenReply();window.textInputs=0;const editor=document.querySelector('[contenteditable]');editor.style.whiteSpace='normal';editor.addEventListener('input',()=>{window.textInputs++;editor.querySelectorAll('*').forEach(e=>e.removeAttribute('style'));const until=performance.now()+30;while(performance.now()<until){}});};</script>`;
        return route.fulfill({contentType:'text/html',body:f.fixture.html()+script});
      });
      await f.value.page.reload();
      f.value.page.setDefaultTimeout(8000);
      const body='Reviewed opening.\n\n'+('A long paragraph with Unicode åäö and exact spacing. ').repeat(20)+'\n\n  | (o) (o) |\n  \\_______/\nLiteral <img src=x onerror=alert(1)> & text.';
      const started=Date.now();
      const result=await worker.operate(f.value,{...f.request,command:'send',incoming:f.incoming,body,submission_id:submissionId});
      assert.equal(result.status,'sent',JSON.stringify(result));
      assert.equal(f.fixture.state.lastSent.body,body);
      assert.equal(await f.value.page.evaluate(()=>window.textInputs),1,'one native editing transaction');
      assert.ok(Date.now()-started<15000);
    }finally{await f.close();}
  }
});

test('interrupted owned composer resumes after restart while external edits and recipients stay protected',{timeout:90000},async()=>{
  for(const tamper of ['none','body','recipient','original','replaced']) {
    const f=await fixtureSession('browser_outlook');
    try {
      await f.value.context.route('**/*',async route=>{
        if(new URL(route.request().url()).pathname==='/__nexus_test_send__')return f.fixture.route(route);
        const script=`<script>const initialOpenReply=openReply,initialOpenMessage=openMessage;openMessage=()=>{initialOpenMessage();if(window.changedOriginal)document.querySelector('#pane [role=document]').textContent='Original changed since failure';};openReply=()=>{initialOpenReply();document.querySelector('#pane').style.display='none';const tabs=document.createElement('div');tabs.setAttribute('role','tablist');for(const editing of [false,true]){const tab=document.createElement('button');tab.setAttribute('role','tab');tab.setAttribute('aria-label',editing?'Editing Re: Question':'Question');tab.textContent=editing?'Editing Re: Question':'Question';tab.setAttribute('aria-selected',String(editing));tab.onclick=()=>{tabs.querySelectorAll('[role=tab]').forEach(t=>t.setAttribute('aria-selected',String(t===tab)));document.querySelector('#pane').style.display=editing?'none':'block';document.querySelector('#compose').style.display=editing?'block':'none';};tabs.append(tab);}document.body.append(tabs);};</script>`;
        return route.fulfill({contentType:'text/html',body:f.fixture.html()+script});
      });
      await f.value.page.reload();
      const body='Reviewed opening.\n\n'+('Long paragraph. ').repeat(40);
      const send={...f.request,command:'send',incoming:f.incoming,body,submission_id:submissionId};
      const evaluate=f.value.page.evaluate.bind(f.value.page);
      f.value.page.evaluate=async(fn,arg)=>{if(fn.name==='insertReviewedBody'){await evaluate(fn,{...arg,body:'Reviewed opening.'});throw new Error('page.evaluate: Editor interrupted during entry.');}return evaluate(fn,arg);};
      const failed=await worker.operate(f.value,send);
      assert.equal(failed.status,'not_sent');assert.match(failed.error,/entering the reviewed reply/);assert.doesNotMatch(failed.error,/next scan/);
      assert.equal(await f.value.page.locator('[role=tablist]').count(),1);
      assert.equal(await f.value.page.locator('#pane').isVisible(),false);
      assert.equal(f.fixture.state.sendCount,0);
      const recovery=path.join(f.value.profile,'nexus-reviewed-composers',submissionId+'.json');
      const saved=fs.readFileSync(recovery,'utf8');
      assert.ok(!saved.includes('Reviewed opening')&&!saved.includes('sender@example.test'));
      assert.equal(fs.existsSync(path.join(f.value.profile,'nexus-reviewed-submissions',submissionId+'.json')),false);
      f.value.page.evaluate=evaluate;
      // Simulate worker restart: only the durable proof and the mailbox's
      // restored composer survive, not any in-memory ownership flag.
      const restarted={context:f.value.context,page:f.value.page,profile:f.value.profile,provider:f.value.provider,mode:f.value.mode};
      if(tamper==='body')await f.value.page.locator('[contenteditable]').fill('User changed this in Outlook');
      if(tamper==='recipient')await f.value.page.locator('#reply-compose>span').evaluate(e=>e.setAttribute('data-email','other@example.test'));
      if(tamper==='original')await f.value.page.evaluate(()=>window.changedOriginal=true);
      if(tamper==='replaced')await f.value.page.locator('[data-nexus-reply-owner]').evaluateAll(es=>es.forEach(e=>e.removeAttribute('data-nexus-reply-owner')));
      const ready=await worker.operate(restarted,{...send,command:'prepare'});
      assert.equal(ready.status,tamper==='none'?'ready':'not_sent',JSON.stringify(ready));
      assert.equal(f.fixture.state.sendCount,0,'preparation never dispatches');
      if(tamper==='none'){
        const result=await worker.operate(restarted,send);
        assert.equal(result.status,'sent',JSON.stringify(result));
        assert.equal(f.fixture.state.lastSent.body,body);assert.equal(f.fixture.state.sendCount,1);
        assert.equal(fs.existsSync(recovery),false);
      }else assert.match(ready.error,tamper==='original'?/original message content changed/:/existing reply composer/);
      if(tamper==='original')assert.equal(await f.value.page.locator('#compose').isVisible(),true,'failed proof restores the interrupted editor');
    }finally{await f.close();}
  }
});

test('Outlook rendered message IDs can change only with unique content and positive conversation proof',{timeout:90000},async()=>{
  for(const change of ['id','duplicate','content','conversation']) {
    const f=await fixtureSession('browser_outlook');
    try {
      f.fixture.state.messageId='new-rendered-id';
      if(change==='content')f.fixture.state.body='Changed source body';
      if(['duplicate','conversation'].includes(change))await f.value.context.route('**/*',async route=>{
        if(new URL(route.request().url()).pathname==='/__nexus_test_send__')return f.fixture.route(route);
        const script=change==='duplicate'
          ? `<script>const initialOpen=openMessage;openMessage=()=>{initialOpen();const original=document.querySelector('#pane article');const copy=original.cloneNode(true);copy.setAttribute('data-message-id','another-new-id');copy.querySelector('[id$="_FROM"]').id='MSG_another-new-id_FROM';original.after(copy);};</script>`
          : `<script>const initialOpen=openMessage;openMessage=()=>{initialOpen();document.querySelector('#pane article').setAttribute('data-convid','different-conversation');};</script>`;
        return route.fulfill({contentType:'text/html',body:f.fixture.html()+script});
      });
      await f.value.page.reload();
      const result=await worker.operate(f.value,{...f.request,command:'send',incoming:f.incoming,body:'Reviewed reply',submission_id:submissionId});
      assert.equal(result.status,change==='id'?'sent':'not_sent',JSON.stringify({change,result}));
      assert.equal(f.fixture.state.sendCount,change==='id'?1:0);
    }finally{await f.close();}
  }
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

test('send locates a virtualized original outside the current viewport before composing', {timeout:60000}, async()=>{
  for(const provider of ['browser_outlook','browser_gmail']) {
    const f=await fixtureSession(provider);
    try {
      await f.value.page.evaluate(provider=>{
        const row=document.querySelector(provider==='browser_gmail'?'tr[data-legacy-thread-id]':'[role=option][data-convid]');
        const container=document.createElement('div');container.style.cssText='height:120px;overflow-y:auto';
        const spacer=document.createElement('div');spacer.style.height='1200px';
        const table=provider==='browser_gmail'?row.closest('table'):row;
        table.before(container);container.append(spacer,table);
        const attr=provider==='browser_gmail'?'data-legacy-thread-id':'data-convid';
        const original=row.getAttribute(attr);row.setAttribute(attr,'another-thread');
        container.addEventListener('scroll',()=>row.setAttribute(attr,container.scrollTop>800?original:'another-thread'));
      },provider);
      const result=await worker.operate(f.value,{...f.request,command:'send',incoming:f.incoming,body:'Reviewed after locating original.',submission_id:submissionId});
      assert.equal(result.status,'sent',JSON.stringify(result));assert.equal(f.fixture.state.sendCount,1);
      await f.value.page.evaluate(provider=>{
        const row=document.querySelector(provider==='browser_gmail'?'tr[data-legacy-thread-id]':'[role=option][data-convid]');
        row.after(row.cloneNode(true));
      },provider);
      const duplicate=await worker.operate(f.value,{...f.request,command:'send',incoming:f.incoming,body:'Do not send an ambiguous reply.',submission_id:'d'.repeat(32)});
      assert.equal(duplicate.status,'not_sent');assert.match(duplicate.error,/ambiguous/);assert.equal(f.fixture.state.sendCount,1);
    }finally{await f.close();}
  }
});

test('send refreshes a stale identity before dispatch and refuses a different recovered mailbox', {timeout:60000}, async()=>{
  const f=await fixtureSession('browser_gmail');
  const prior=worker.budgets.browser_gmail.identity;worker.budgets.browser_gmail.identity=100;
  try {
    await f.value.page.locator('[aria-label^="Google Account"]').evaluate(e=>e.removeAttribute('aria-label'));
    const request={...f.request,command:'send',incoming:f.incoming,body:'Recovered session reply.',submission_id:submissionId};
    const recovered=await worker.operate(f.value,request);assert.equal(recovered.status,'sent',JSON.stringify(recovered));assert.equal(f.fixture.state.sendCount,1);
    await f.value.page.reload();
    await f.value.page.locator('[aria-label^="Google Account"]').evaluate(e=>e.removeAttribute('aria-label'));
    f.fixture.state.email='different@example.test';
    const result=await worker.operate(f.value,{...request,submission_id:'c'.repeat(32)});
    assert.equal(result.status,'not_sent');assert.match(result.error,/different mailbox/);assert.equal(f.fixture.state.sendCount,1);
  }finally{worker.budgets.browser_gmail.identity=prior;await f.close();}
});

test('Outlook Reply uses the reader scope for unlabelled envelopes and multiple message-local Reply buttons', {timeout:45000}, async()=>{
  const f=await fixtureSession('browser_outlook');
  try {
    await f.value.context.route('**/*',async route=>{
      if(new URL(route.request().url()).pathname==='/__nexus_test_send__')return f.fixture.route(route);
      return route.fulfill({contentType:'text/html',body:f.fixture.html()+`<script>
        const originalOpen=openMessage;openMessage=()=>{originalOpen();const article=document.querySelector('#pane article');article.removeAttribute('aria-label');article.removeAttribute('data-message-id');article.append(article.querySelector('button').cloneNode(true));
          const decoy=document.createElement('button');decoy.setAttribute('aria-label','Reply');decoy.textContent='Reply';decoy.onclick=()=>window.decoyClicked=true;article.querySelector('[role=document]').append(decoy);
        };
      </script>`});
    });
    await f.value.page.reload();
    // The source includes the synthetic body button's visible text too.
    const incoming=(await worker.operate(f.value,{...f.request,cursor:''})).messages[0];
    const result=await worker.operate(f.value,{...f.request,command:'send',incoming,body:'Selected reviewed version.',submission_id:submissionId});
    assert.equal(result.status,'sent',JSON.stringify(result));assert.equal(f.fixture.state.sendCount,1);
    assert.equal(await f.value.page.evaluate(()=>!!window.decoyClicked),false);
  }finally{await f.close();}
});

test('missing Reply refreshes and retries preparation once before sending', {timeout:45000}, async()=>{
  const f=await fixtureSession('browser_outlook');
  let documents=0;
  try {
    await f.value.context.route('**/*',async route=>{
      if(new URL(route.request().url()).pathname==='/__nexus_test_send__')return f.fixture.route(route);
      documents++;
      const script=documents===1?`<script>const oldOpen=openMessage;openMessage=()=>{oldOpen();document.querySelector('#pane button').remove();};</script>`:'';
      return route.fulfill({contentType:'text/html',body:f.fixture.html()+script});
    });
    await f.value.page.reload();
    const result=await worker.operate(f.value,{...f.request,command:'send',incoming:f.incoming,body:'Recovered before sending.',submission_id:submissionId});
    assert.equal(result.status,'sent',JSON.stringify(result));assert.equal(documents,2);assert.equal(f.fixture.state.sendCount,1);
  }finally{await f.close();}
});

test('failed Reply recovery is bounded and changed originals never send', {timeout:60000}, async()=>{
  for(const changed of [false,true]) {
    const f=await fixtureSession('browser_outlook');let documents=0;
    try {
      await f.value.context.route('**/*',async route=>{
        if(new URL(route.request().url()).pathname==='/__nexus_test_send__')return f.fixture.route(route);
        documents++;
        if(changed&&documents>1)f.fixture.state.body='Different incoming message';
        const script=!changed||documents===1?`<script>const oldOpen=openMessage;openMessage=()=>{oldOpen();document.querySelector('#pane button').remove();};</script>`:'';
        return route.fulfill({contentType:'text/html',body:f.fixture.html()+script});
      });
      await f.value.page.reload();
      const result=await worker.operate(f.value,{...f.request,command:'send',incoming:f.incoming,body:'Must not send.',submission_id:submissionId});
      assert.equal(result.status,'not_sent',JSON.stringify(result));assert.equal(documents,2);assert.equal(f.fixture.state.sendCount,0);
      assert.match(result.error,changed?/content changed/:/Reply is not ready/);
    }finally{await f.close();}
  }
});

test('mailbox readiness refreshes the original but never opens a composer or dispatches', {timeout:45000}, async()=>{
  for(const provider of ['browser_outlook','browser_gmail']) {
    const f=await fixtureSession(provider);
    try {
      const request={...f.request,command:'prepare',incoming:f.incoming,body:'Review only',submission_id:submissionId};
      const result=await worker.operate(f.value,request);
      assert.equal(result.status,'ready',JSON.stringify(result));
      assert.equal(f.fixture.state.sendCount,0);
      assert.equal(await f.value.page.locator('[contenteditable=true]').count(),0);
      assert.equal(fs.existsSync(path.join(f.value.profile,'nexus-reviewed-submissions',submissionId+'.json')),false);
      const sent=await worker.operate(f.value,{...request,command:'send',body:'Explicitly approved after readiness.'});
      assert.equal(sent.status,'sent',JSON.stringify(sent));assert.equal(f.fixture.state.sendCount,1);
    }finally{await f.close();}
  }
});

// Serves the fixture with extra page script; Sent Items keeps its own document.
const scripted=(f,extra)=>async route=>{
  const pathname=new URL(route.request().url()).pathname;
  if(pathname.startsWith('/__nexus_test_'))return f.fixture.route(route);
  const sent=f.fixture.state.sentFolder&&pathname==='/mail/sentitems';
  return route.fulfill({contentType:'text/html',body:extra(f.fixture.html(sent),sent)});
};
test('recovery marker stays on the verified editor and an unselected draft tab never receives the reply',{timeout:90000},async()=>{
  for(const flagged of [true,false]) {
    const f=await fixtureSession('browser_outlook');
    try {
      // An unrelated popped-out draft (another conversation, same correspondent) owns the only Editing tab.
      const tabs=flagged?['aria-selected="true"','aria-selected="false"']:['',''];
      await f.value.context.route('**/*',scripted(f,html=>html+`<script>
        const list=document.createElement('div');list.setAttribute('role','tablist');
        list.innerHTML='<button role="tab" aria-label="Inbox" ${tabs[0]}>Inbox</button><button role="tab" aria-label="Editing Re: Other topic" ${tabs[1]}>Editing Re: Other topic</button>';
        document.body.prepend(list);
        const other=document.createElement('section');other.style.display='none';
        other.innerHTML='<span data-email="sender@example.test">sender@example.test</span><div role="textbox" aria-label="Message body" contenteditable="true"></div><button aria-label="Send">Send</button>';
        document.body.append(other);
        other.querySelector('button').onclick=()=>fetch('/__nexus_test_send__',{method:'POST',body:JSON.stringify({body:other.querySelector('[contenteditable]').innerText,recipient:'sender@example.test',conversation:'other'})});
        const [inbox,editing]=list.querySelectorAll('button');
        inbox.onclick=()=>{other.style.display='none';document.querySelector('main').style.display='block';};
        editing.onclick=()=>{other.style.display='block';document.querySelector('main').style.display='none';};
      </script>`));
      await f.value.page.reload();
      const send={...f.request,command:'send',incoming:f.incoming,body:'Approved reply for the Question thread',submission_id:submissionId};
      const evaluate=f.value.page.evaluate.bind(f.value.page);
      f.value.page.evaluate=async(fn,arg)=>{if(fn.name==='insertReviewedBody')throw new Error('page.evaluate: Editor interrupted during entry.');return evaluate(fn,arg);};
      assert.equal((await worker.operate(f.value,send)).status,'not_sent');
      f.value.page.evaluate=evaluate;
      assert.deepEqual(await f.value.page.evaluate(()=>[...document.querySelectorAll('[data-nexus-reply-owner]')].map(e=>e.getAttribute('role'))),['textbox']);
      const result=await worker.operate({context:f.value.context,page:f.value.page,profile:f.value.profile,provider:f.value.provider,mode:f.value.mode},send);
      assert.equal(result.status,'sent',JSON.stringify(result));
      assert.deepEqual(f.fixture.state.lastSent,{body:send.body,recipient:'sender@example.test'},'sent from the owned composer, never the other draft');
      assert.equal(f.fixture.state.sendCount,1);
    }finally{await f.close();}
  }
});
test('an absent or unhydrated Sent Items baseline never confirms a send',{timeout:90000},async()=>{
  const body='Thanks, received.';
  // Slow list: the conversation renders only after the baseline; the Send click is dropped
  // and an older identical reply appears. First reply: no baseline exists at all.
  for(const [state,extra,sends] of [
    [{dropSend:true,sendCount:1,lastSent:{body,recipient:'sender@example.test'}},(html,sent)=>sent?html.replace('refresh();setInterval(refresh,100);','setTimeout(()=>{refresh();setInterval(refresh,100);},2500);'):html,1],
    [{leaveComposer:true},html=>html,1]]) {
    const f=await fixtureSession('browser_outlook','headless',{sentFolder:true,acknowledge:false,...state});
    try {
      await f.value.context.route('**/*',scripted(f,extra));
      await f.value.page.reload();
      const result=await worker.operate(f.value,{...f.request,command:'send',incoming:f.incoming,body,submission_id:submissionId});
      assert.equal(result.status,'unknown',JSON.stringify(result));
      assert.equal(f.fixture.state.sendCount,sends);
      assert.equal(f.value.context.pages().length,1);
    }finally{await f.close();}
  }
});
test('a Send control re-rendered while Sent Items opens or before the receipt never becomes an uncertain send',{timeout:90000},async()=>{
  // Cross-tab re-render as the witness tab loads: the handle is resolved afterwards.
  const f=await fixtureSession('browser_outlook','headless',{sentFolder:true});
  try {
    await f.value.context.route('**/*',scripted(f,(html,sent)=>html+(sent?`<script>new BroadcastChannel('owa').postMessage('opened');</script>`:`<script>new BroadcastChannel('owa').onmessage=()=>{const b=document.querySelector('button[aria-label="Send"]');if(b)b.replaceWith(b.cloneNode(true));};</script>`)));
    await f.value.page.reload();
    const result=await worker.operate(f.value,{...f.request,command:'send',incoming:f.incoming,body:'Approved reply',submission_id:submissionId});
    assert.equal(result.status,'sent',JSON.stringify(result));assert.equal(f.fixture.state.sendCount,1);
  }finally{await f.close();}
  // Re-render after the trial click: the detached handle is caught before the receipt.
  const g=await fixtureSession('browser_outlook');
  try {
    const request={...g.request,command:'send',incoming:g.incoming,body:'Approved reply',submission_id:submissionId};
    const evaluate=g.value.page.evaluate.bind(g.value.page);
    g.value.page.evaluate=async(fn,arg)=>{if(fn.name==='acknowledgement')await evaluate(()=>{const b=document.querySelector('button[aria-label="Send"]');b.replaceWith(b.cloneNode(true));});return evaluate(fn,arg);};
    const result=await worker.operate(g.value,request);
    g.value.page.evaluate=evaluate;
    assert.equal(result.status,'not_sent',JSON.stringify(result));assert.equal(g.fixture.state.sendCount,0);
    assert.equal(fs.existsSync(path.join(g.value.profile,'nexus-reviewed-submissions',submissionId+'.json')),false);
    assert.equal((await worker.operate(g.value,request)).status,'sent');assert.equal(g.fixture.state.sendCount,1);
  }finally{await g.close();}
});
test('an approved reply beginning with blank lines is entered exactly',{timeout:60000},async()=>{
  for(const provider of ['browser_outlook','browser_gmail']) for(const body of ['\n\nReply after two blank lines.','\r\nReply after a Windows blank line.']) {
    const f=await fixtureSession(provider);
    try {
      const result=await worker.operate(f.value,{...f.request,command:'send',incoming:f.incoming,body,submission_id:submissionId});
      assert.equal(result.status,'sent',JSON.stringify(result));
      assert.equal(f.fixture.state.lastSent.body,body.replace(/\r\n/g,'\n'));
    }finally{await f.close();}
  }
});
test('the dispatch deadline counts from the request, not from a late preparation',{timeout:45000},async()=>{
  const f=await fixtureSession('browser_outlook');
  try {
    // 101 s after the request the bridge (150 s) could not await Send confirmation.
    const request={...f.request,command:'send',incoming:f.incoming,body:'Too late to dispatch',submission_id:submissionId};
    const result=await worker.operate(f.value,{...request,_startedAt:Date.now()-101000});
    assert.equal(result.status,'not_sent',JSON.stringify(result));assert.match(result.error,/took too long/);
    assert.equal(f.fixture.state.sendCount,0);
    assert.equal(fs.existsSync(path.join(f.value.profile,'nexus-reviewed-submissions',submissionId+'.json')),false);
  }finally{await f.close();}
});

test('a resumed tabbed composer whose editor re-mounts keeps ownership only through its stamped, selected tab',{timeout:90000},async()=>{
  // remount: re-selecting Editing replaces the editor element. unselected: it also fails to report selection.
  for(const mode of ['remount','unselected']) {
    const f=await fixtureSession('browser_outlook');
    try {
      await f.value.context.route('**/*',scripted(f,html=>html+`<script>const initialOpenReply=openReply;openReply=()=>{initialOpenReply();document.querySelector('#pane').style.display='none';const tabs=document.createElement('div');tabs.setAttribute('role','tablist');for(const editing of [false,true]){const tab=document.createElement('button');tab.setAttribute('role','tab');tab.setAttribute('aria-label',editing?'Editing Re: Question':'Question');tab.textContent=tab.getAttribute('aria-label');tab.setAttribute('aria-selected',String(editing));tab.onclick=()=>{tabs.querySelectorAll('[role=tab]').forEach(t=>t.setAttribute('aria-selected',String(t===tab&&!(editing&&${mode==='unselected'}))));document.querySelector('#pane').style.display=editing?'none':'block';document.querySelector('#compose').style.display=editing?'block':'none';
        if(editing){const old=document.querySelector('#compose [contenteditable]');const fresh=document.createElement('div');for(const a of ['style','role','aria-label','contenteditable'])fresh.setAttribute(a,old.getAttribute(a));fresh.innerHTML=old.innerHTML;old.replaceWith(fresh);}};tabs.append(tab);}document.body.append(tabs);};</script>`));
      await f.value.page.reload();
      const send={...f.request,command:'send',incoming:f.incoming,body:'Reviewed opening.\n\nRest of reply.',submission_id:submissionId};
      const evaluate=f.value.page.evaluate.bind(f.value.page);
      f.value.page.evaluate=async(fn,arg)=>{if(fn.name==='insertReviewedBody'){await evaluate(fn,{...arg,body:'Reviewed opening.'});throw new Error('page.evaluate: Editor interrupted during entry.');}return evaluate(fn,arg);};
      assert.equal((await worker.operate(f.value,send)).status,'not_sent');
      f.value.page.evaluate=evaluate;
      const restarted={context:f.value.context,page:f.value.page,profile:f.value.profile,provider:f.value.provider,mode:f.value.mode};
      const result=await worker.operate(restarted,send);
      if(mode==='remount') {
        assert.equal(result.status,'sent',JSON.stringify(result));
        assert.deepEqual(f.fixture.state.lastSent,{body:send.body,recipient:'sender@example.test'});assert.equal(f.fixture.state.sendCount,1);
      } else {
        assert.equal(result.status,'not_sent',JSON.stringify(result));assert.match(result.error,/changed during recovery/);assert.equal(f.fixture.state.sendCount,0);
      }
    }finally{await f.close();}
  }
});
test('a receipt that fails before it is durable is removed and the approval stays retryable',{timeout:60000},async()=>{
  const f=await fixtureSession('browser_outlook');
  const {openSync,fsyncSync}=fs;let receipt=null,injected=false;
  try {
    // Only the exclusive dispatch receipt fails, once, after its file was created.
    fs.openSync=(file,flags,...rest)=>{const fd=openSync(file,flags,...rest);if(!injected&&flags==='wx'&&String(file).includes('nexus-reviewed-submissions'))receipt=fd;return fd;};
    fs.fsyncSync=fd=>{if(fd===receipt){receipt=null;injected=true;throw Object.assign(new Error('EIO: i/o error, fsync'),{code:'EIO'});}return fsyncSync(fd);};
    const request={...f.request,command:'send',incoming:f.incoming,body:'Approved after a disk hiccup.',submission_id:submissionId};
    const failed=await worker.operate(f.value,request);
    assert.equal(failed.status,'not_sent',JSON.stringify(failed));assert.equal(f.fixture.state.sendCount,0);
    assert.equal(fs.existsSync(path.join(f.value.profile,'nexus-reviewed-submissions',submissionId+'.json')),false);
    const retry=await worker.operate(f.value,request);
    assert.equal(retry.status,'sent',JSON.stringify(retry));assert.equal(f.fixture.state.sendCount,1);
  }finally{fs.openSync=openSync;fs.fsyncSync=fsyncSync;await f.close();}
});
test('a future or non-finite request start never extends the dispatch deadline',{timeout:60000},async()=>{
  for(const start of [Date.now()+3600000,Infinity]) {
    const f=await fixtureSession('browser_outlook');
    const now=Date.now;
    try {
      // Preparation "takes" 101 s: the clock jumps once the approved text is entered.
      const evaluate=f.value.page.evaluate.bind(f.value.page);
      f.value.page.evaluate=async(fn,arg)=>{const result=await evaluate(fn,arg);if(fn.name==='insertReviewedBody'){const base=now();Date.now=()=>base+101000+(now()-base);}return result;};
      const result=await worker.operate(f.value,{...f.request,command:'send',incoming:f.incoming,body:'Too late to dispatch',submission_id:submissionId,_startedAt:start});
      Date.now=now;
      assert.equal(result.status,'not_sent',JSON.stringify({start,result}));assert.match(result.error,/took too long/);
      assert.equal(f.fixture.state.sendCount,0);
    }finally{Date.now=now;await f.close();}
  }
});
