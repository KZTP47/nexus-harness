'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {chromium}=require('playwright-core');
const {findInstalledBrowser}=require('./external-browser');
const {readIdentity,readRows,readMessage,inboxState,status,operate,resolveSenderCard}=require('./email-browser-worker');
test('row warnings expose safe application guidance without browser traces or private selectors',()=>{
  const {safeRowWarning}=require('./email-browser-worker');
  for(const error of ['\u001b[31mlocator.click: Timeout exceeded\u001b[0m\nCall log: data-convid="synthetic-private-id"','page.waitForFunction: synthetic@example.test','Unknown failure at C:\\Synthetic\\Private']){
    const warning=safeRowWarning(new Error(error));
    assert.match(warning,/Reopen the inbox/);
    assert.doesNotMatch(warning,/locator|page\.|Call log|synthetic|Synthetic|\u001b/);
  }
  assert.match(safeRowWarning(new Error('page.evaluate: Error: The sender profile contains multiple email addresses. This message needs manual review.\n    at evaluate')),/multiple email addresses/);
});
test('real local Chromium DOM fixtures: Gmail and Outlook extraction and unsupported state',async()=>{
  const installed=findInstalledBrowser();
  assert.ok(installed,'Chrome or Edge required for browser mail fixture test');
  const browser=await chromium.launch({executablePath:installed.executable,headless:true});
  try {
    const page=await browser.newPage();
    await page.setContent(`<a aria-label="Google Account: Person (person@example.test)">Account</a><table><tr data-legacy-thread-id="thread1"><td>Mail</td></tr></table><h2 class="hP">Question</h2><article data-legacy-message-id="msg1"><span class="gD" email="sender@example.test"></span><div class="a3s">Please reply</div></article>`);
    assert.equal((await page.evaluate(readIdentity,'browser_gmail')).email,'person@example.test');
    assert.deepEqual(await page.evaluate(readRows,'browser_gmail'),[{id:'thread1',attr:'data-legacy-thread-id'}]);
    assert.equal((await page.evaluate(readMessage,'browser_gmail')).body,'Please reply');
    await page.setContent(`<button id="mectrl_main_trigger" aria-label="person@example.test">Account</button><div role="option" data-convid="conversation1">Mail</div><h2 data-testid="conversation-subject">Question</h2><article data-message-id="msg1"><div data-testid="SenderPersona"><span title="sender@example.test">Sender</span></div><div role="document">Please reply</div></article>`);
    assert.equal((await page.evaluate(readIdentity,'browser_outlook')).email,'person@example.test');
    assert.equal((await page.evaluate(readRows,'browser_outlook'))[0].id,'conversation1');
    assert.equal((await page.evaluate(readMessage,'browser_outlook')).sender,'sender@example.test');
    await page.setContent('<div role="treeitem" id="primaryMailboxRoot_arbitrary" title="other@example.test" data-folder-name="other@example.test">Mailbox</div>');
    assert.equal((await page.evaluate(readIdentity,'browser_outlook')).email,'other@example.test');
    await page.setContent('<h3 id="CONV_arbitrary_SUBJECT" role="heading" aria-level="3">Observed layout</h3><div aria-label="Email message"><span id="MSG_message123_FROM">Example Sender&lt;sender@example.test&gt;</span><div id="MSG_message123_SUBJECT" aria-labelledby="CONV_arbitrary_SUBJECT"></div><div role="document" aria-label="Message body" id="UniqueMessageBody_1">Observed body</div></div>');
    const observed=await page.evaluate(readMessage,'browser_outlook');
    assert.equal(observed.sender,'sender@example.test');
    assert.equal(observed.subject,'Observed layout');
    assert.equal(observed.message_id,'message123');
    await page.setContent('<h2 role="heading" aria-level="2">Navigation pane</h2><div aria-label="Email message"><span id="MSG_nosubject_FROM">sender@example.test</span><div role="document">Actual message body</div></div>');
    await assert.rejects(page.evaluate(readMessage,'browser_outlook'),/subject is not ready/);
    await page.setContent('<p>Sign in</p>');
    assert.equal((await page.evaluate(readIdentity,'browser_outlook')).email,'');
    await assert.rejects(page.evaluate(readMessage,'browser_outlook'),/cannot be read reliably/);
  } finally {await browser.close();}
});
test('latest message attribution, empty inbox, and polling loop with local routed Outlook fixture',async()=>{
  const installed=findInstalledBrowser();
  assert.ok(installed);
  const browser=await chromium.launch({executablePath:installed.executable,headless:true});
  try {
    const page=await browser.newPage();
    const article=(id,sender,body)=>`<article data-message-id="${id}"><div data-testid="SenderPersona"><span title="${sender}">${sender}</span></div><div role="document">${body}</div></article>`;
    await page.setContent(`<h2 data-testid="conversation-subject">Thread</h2>${article('old','first@example.test','Old question')}${article('new','latest@example.test','New question')}`);
    const latest=await page.evaluate(readMessage,'browser_outlook');
    assert.equal(latest.sender,'latest@example.test');
    assert.equal(latest.body,'New question');
    assert.equal(latest.message_id,'new');
    await page.setContent('<main role="main"><h2>Your inbox is empty.</h2></main>');
    assert.equal(await page.evaluate(inboxState,'browser_outlook'),'empty');
    await page.setContent('<main role="main"><h2>Something changed</h2></main>');
    assert.equal(await page.evaluate(inboxState,'browser_outlook'),'unsupported');
    let sender='latest@example.test',messageId='new',empty=false,broken=false;
    await page.route('**/*',route=>{
      const pane=`<h2 data-testid="conversation-subject">Thread</h2>${article('old','first@example.test','Old question')}${article(messageId,sender,'Latest question')}`;
      let row=`<div role="option" data-convid="thread1" onclick='document.querySelector("#pane").innerHTML=${JSON.stringify(pane).replaceAll("'",'&#39;')};const heading=document.querySelector("h2");heading.textContent="";setTimeout(()=>heading.textContent="Thread",300)' >Thread</div><section id="pane"></section>`;
      if (broken) {
        const bad='<h2 data-testid="conversation-subject">Broken message</h2><div aria-label="Email message"><span id="MSG_bad_FROM"><button role="button" onclick="document.querySelector(\'[role=dialog]\').style.display=\'block\'">Display Name</button></span><div role="document">Ambiguous sender</div></div><div role="dialog" data-log-region="LivePersonaCard" style="display:none"><span title="one@example.test">One</span><span title="two@example.test">Two</span></div>';
        row+=`<div role="option" data-convid="broken-thread" onclick='document.querySelector("#pane").innerHTML=${JSON.stringify(bad).replaceAll("'",'&#39;')}'>Broken</div>`;
      }
      return route.fulfill({contentType:'text/html',body:`<button id="mectrl_main_trigger" aria-label="person@example.test">Account</button><main role="main">${empty?'<h2>Your inbox is empty.</h2>':row}</main>`});
    });
    await page.goto('https://outlook.office.com/mail/inbox');
    // Establish a clickable routed inbox before starting the worker's short
    // virtualization budget; browser startup is not part of that contract.
    await page.bringToFront();
    await page.locator('[data-convid="thread1"]').click({trial:true,timeout:15000});
    const value={page,provider:'browser_outlook',lastInboxRefresh:Date.now()};
    const request={command:'sync',connection:{email:'person@example.test'}};
    await assert.rejects(status(value,{email:'foreign@example.test'}),/different mailbox/);
    const first=await operate(value,request);
    assert.equal(first.messages.length,2);
    assert.deepEqual(first.messages.map(message=>message.sender),['first@example.test','latest@example.test']);
    const second=await operate(value,{...request,cursor:first.cursor});
    assert.equal(second.messages.length,0,'same message ID must not redraft');
    sender='person@example.test';messageId='own-reply';value.lastInboxRefresh=0;
    const ownCursor=JSON.parse(second.cursor);for(const entry of Object.values(ownCursor.row_cache||{}))entry.checked_at=0;
    const own=await operate(value,{...request,cursor:JSON.stringify(ownCursor)});
    assert.equal(own.messages.length,0,'own sent reply must not trigger a reply');
    sender='latest@example.test';messageId='incoming-next';value.lastInboxRefresh=0;
    const nextCursor=JSON.parse(own.cursor);for(const entry of Object.values(nextCursor.row_cache||{}))entry.checked_at=0;
    const next=await operate(value,{...request,cursor:JSON.stringify(nextCursor)});
    assert.equal(next.messages.length,1,'new message in same thread must trigger');
    broken=true;messageId='valid-amid-failure';value.lastInboxRefresh=0;
    const partialCursor=JSON.parse(next.cursor);for(const entry of Object.values(partialCursor.row_cache||{}))entry.checked_at=0;
    const partial=await operate(value,{...request,cursor:JSON.stringify(partialCursor)});
    assert.equal(partial.messages.length,1,'one bad sender must not discard another good message');
    assert.ok(partial.warnings.some(w=>w.includes('multiple email addresses')));
    empty=true;value.lastInboxRefresh=0;
    const noMail=await operate(value,{...request,cursor:next.cursor});
    assert.equal(noMail.messages.length,0);
    assert.deepEqual(noMail.warnings,[]);
  } finally {await browser.close();}
});
test('visible received messages before an own reply import independently, survive restart and accept empty subjects',{timeout:30000},async()=>{
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();let extra=false,heading=true;
    await page.route('**/*',route=>{
      const ids=['received-one','received-two',...(extra?['received-three']:[]),'own-reply'];
      const messages=ids.map(id=>`<article data-message-id="${id}" data-convid="thread"><span email="${id==='own-reply'?'owner':'sender'}@example.test">Sender</span><div role="document">Body ${id}</div></article>`).join('');
      const pane=(heading?'<h2 data-testid="conversation-subject"></h2>':'')+messages;
      return route.fulfill({contentType:'text/html',body:`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main"><div role="option" data-convid="thread" onclick='document.querySelector("#pane").innerHTML=${JSON.stringify(pane)}'>Conversation</div><section id="pane"></section></main>`});
    });
    await page.goto('https://outlook.office.com/mail/inbox');
    const request={command:'sync',connection:{email:'owner@example.test'}};
    const first=await operate({page,provider:'browser_outlook'},request);
    assert.deepEqual(first.messages.map(message=>message.message_id),['received-one','received-two']);
    assert.ok(first.messages.every(message=>message.subject===''));
    extra=true;
    const restartCursor=JSON.parse(first.cursor);for(const entry of Object.values(restartCursor.row_cache||{}))entry.checked_at=0;
    const restarted=await operate({page,provider:'browser_outlook'},{...request,cursor:JSON.stringify(restartCursor)});
    assert.deepEqual(restarted.messages.map(message=>message.message_id),['received-three']);
    await page.locator('[role=option]').click();
    await page.locator('h2').evaluate(node=>node.remove());
    await assert.rejects(page.evaluate(readMessage,{provider:'browser_outlook',expectedMessageId:'received-one'}),/subject is not ready/);
  }finally{await browser.close();}
});

test('dynamic inbox rows preserve imports, discover arrivals and retry virtualized conversations',{timeout:45000},async()=>{
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();let navigation=0,restore=false;
    await page.route('**/*',route=>{
      navigation++;
      const ids=restore?['lost','first','arrival','virtual']:navigation<=2?['first','lost','virtual']:navigation<4?['arrival']:['virtual','arrival'];
      // A snapshot row can disappear, while a newly arrived row takes its place.
      const shown=ids;
      const rows=shown.map(id=>`<div role="option" data-convid="${id}" onclick="openMail('${id}')">${id}</div>`).join('');
      const html=`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main">${rows}<section id="pane"></section></main><script>function openMail(id){document.querySelector('#pane').innerHTML='<h2 data-testid="conversation-subject">Question</h2><article data-convid="'+id+'" data-message-id="'+id+'"><span email="sender@example.test">Sender</span><div role="document">Body '+id+'</div></article>';}</script>`;
      const dynamic=`<script>const priorOpen=openMail;openMail=id=>{priorOpen(id);if(id==='first'){document.querySelectorAll('[role=option]').forEach(node=>{if(node.getAttribute('data-convid')!=='first')node.remove();});const arrival=document.createElement('div');arrival.setAttribute('role','option');arrival.setAttribute('data-convid','arrival');arrival.textContent='arrival';arrival.onclick=()=>openMail('arrival');document.querySelector('main').prepend(arrival);}};</script>`;
      return route.fulfill({contentType:'text/html',body:html+dynamic});
    });
    await page.goto('https://outlook.office.com/mail/inbox');
    const value={page,provider:'browser_outlook'},request={command:'sync',connection:{email:'owner@example.test'}};
    const first=await operate(value,request);
    assert.ok(first.messages.some(message=>message.message_id==='first'),'earlier success survives stale row failure');
    assert.ok(first.messages.some(message=>message.message_id==='arrival'),'new visible arrival joins the bounded scan');
    assert.ok(first.messages.some(message=>message.message_id==='virtual'),'refetch retries a virtualized row');
    assert.ok(first.warnings.some(w=>w.includes('no longer visible')));
    assert.ok(first.warnings.every(w=>!/[\u001b]|locator\.|data-convid|Call log/.test(w)));
    restore=true;value.lastInboxRefresh=0;
    const second=await operate(value,{...request,cursor:first.cursor});
    assert.deepEqual(second.messages.map(message=>message.message_id),['lost'],'failed row remains eligible next poll; successful imports deduplicate');
  }finally{await browser.close();}
});

test('fast polling skips unchanged rows, prioritizes changes and reconciles identical summaries',{timeout:30000},async t=>{
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();let version=1,label='Preview',navigations=0;
    await page.route('**/*',route=>{navigations++;return route.fulfill({contentType:'text/html',body:`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main"><div role="option" data-convid="thread" onclick="openMail()">${label}</div><section id="pane"></section></main><script>window.opens=0;function openMail(){window.opens++;document.querySelector('#pane').innerHTML='<h2 data-testid="conversation-subject">Question</h2><article data-convid="thread" data-message-id="message-${version}"><span email="sender@example.test">Sender</span><div role="document">Body ${version}</div></article>';}</script>`});});
    await page.goto('https://outlook.office.com/mail/inbox');
    const value={page,provider:'browser_outlook'},request={command:'sync',connection:{email:'owner@example.test'}};
    const first=await operate(value,request);assert.equal(first.messages.length,1);
    const before=navigations,start=Date.now();
    const idle=await operate(value,{...request,cursor:first.cursor});
    const elapsed=Date.now()-start;
    assert.equal(idle.messages.length,0);assert.equal(navigations,before);assert.equal(await page.evaluate(()=>window.opens),1);
    t.diagnostic(`Unchanged retained-inbox poll: ${elapsed} ms, 0 navigations, 0 message opens`);
    assert.ok(elapsed<1500);
    assert.ok(!idle.cursor.includes('Preview')&&!idle.cursor.includes('owner@example.test')&&!idle.cursor.includes('thread'));
    version=2;label='Changed preview';await page.locator('[role=option]').evaluate((node,text)=>node.textContent=text,label);
    const changed=await operate(value,{...request,cursor:idle.cursor});assert.equal(changed.messages[0].message_id,'message-2');
    version=3;
    const same=await operate(value,{...request,cursor:changed.cursor});assert.equal(same.messages.length,0);
    const aged=JSON.parse(same.cursor);for(const entry of Object.values(aged.row_cache))entry.checked_at=0;
    const reconcile=await operate(value,{...request,cursor:JSON.stringify(aged)});assert.equal(reconcile.messages[0].message_id,'message-3');
    const restartBefore=navigations;
    const restart=await operate({page,provider:'browser_outlook'},{...request,cursor:reconcile.cursor});assert.equal(restart.messages.length,0);assert.ok(navigations>restartBefore);
    const invalid=JSON.parse(restart.cursor);invalid.row_cache_contract='obsolete';version=4;
    const migrated=await operate(value,{...request,cursor:JSON.stringify(invalid)});assert.equal(migrated.messages[0].message_id,'message-4');
    await page.goto('https://outlook.office.com/mail/sentitems');
    const outsideBefore=navigations;await operate(value,{...request,cursor:migrated.cursor});assert.ok(navigations>outsideBefore);assert.equal(new URL(page.url()).pathname,'/mail/inbox');
    const exhausted=await operate(value,{...request,_singleTab:true,_scanDeadline:Date.now()+1000,cursor:''});
    assert.equal(exhausted.messages.length,0);assert.equal(await page.evaluate(()=>window.opens),0);
    assert.equal(Object.keys(JSON.parse(exhausted.cursor).row_cache).length,0,'shared deadline leaves unvisited rows eligible instead of caching them');
  }finally{await browser.close();}
});

test('row budget leaves unvisited rows eligible and new arrivals precede reconciliation',{timeout:15000},async()=>{
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();
    await page.route('**/*',route=>route.fulfill({contentType:'text/html',body:`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main">${Array.from({length:12},(_,index)=>'<div role="option" data-convid="row-'+index+'" onclick="openMail(this)">Preview '+index+'</div>').join('')}<section id="pane"></section></main><script>window.opened=[];function openMail(row){const id=row.getAttribute('data-convid');window.opened.push(id);document.querySelector('#pane').innerHTML='<h2 data-testid="conversation-subject">Question</h2><article data-convid="'+id+'" data-message-id="'+id+'"><span email="sender@example.test">Sender</span><div role="document">Body '+id+'</div></article>';}</script>`}));
    await page.goto('https://outlook.office.com/mail/inbox');
    const value={page,provider:'browser_outlook'},request={command:'sync',connection:{email:'owner@example.test'}};
    const first=await operate(value,request);assert.equal(first.messages.length,10);assert.equal(Object.keys(JSON.parse(first.cursor).row_cache).length,10);
    const aged=JSON.parse(first.cursor);for(const entry of Object.values(aged.row_cache))entry.checked_at=0;
    await page.evaluate(()=>{window.opened=[];const row=document.querySelector('[role=option]').cloneNode(true);row.setAttribute('data-convid','arrival');row.textContent='New arrival';document.querySelector('main').append(row);});
    const second=await operate(value,{...request,cursor:JSON.stringify(aged)});
    assert.deepEqual(second.messages.map(message=>message.message_id),['row-10','row-11','arrival']);
    assert.deepEqual((await page.evaluate(()=>window.opened)).slice(0,3),['row-10','row-11','arrival']);
    assert.equal((await page.evaluate(()=>window.opened)).length,4,'only one aged known row follows new arrivals');
    let cursor=second.cursor;const reconciled=[];
    for(let pass=0;pass<12;pass++){
      const due=JSON.parse(cursor);for(const entry of Object.values(due.row_cache))entry.checked_at=0;
      await page.evaluate(()=>{window.opened=[];});
      const result=await operate(value,{...request,cursor:JSON.stringify(due)});cursor=result.cursor;
      const opened=await page.evaluate(()=>window.opened);
      assert.equal(opened.length,1,'each all-aged scan opens just one unchanged conversation');
      reconciled.push(opened[0]);assert.equal(result.messages.length,0);
    }
    assert.equal(new Set(reconciled).size,12,'reconciliation offset rotates without repeating one row');
  }finally{await browser.close();}
});

test('second tab layout failure preserves first imports but identity changes remain fatal',{timeout:45000},async()=>{
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();let broken=true,foreign=false;
    await page.route('**/*',route=>route.fulfill({contentType:'text/html',body:`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main"><button role="tab" aria-selected="true" onclick="selectTab('Focused')">Focused</button><button role="tab" aria-selected="false" onclick="selectTab('Other')">Other</button><section id="rows"></section><section id="pane"></section></main><script>function selectTab(name){document.querySelectorAll('[role=tab]').forEach(tab=>tab.setAttribute('aria-selected',String(tab.textContent===name)));document.querySelector('#pane').replaceChildren();if(name==='Other'&&${foreign})document.querySelector('#mectrl_main_trigger').setAttribute('aria-label','other@example.test');document.querySelector('#rows').innerHTML=name==='Other'&&${broken}?'<p>Unsupported loading layout</p>':'<div role="option" data-convid="'+name+'" onclick="openMail(this)">'+name+'</div>';};function openMail(row){const id=row.getAttribute('data-convid');document.querySelector('#pane').innerHTML='<h2 data-testid="conversation-subject">Question</h2><article data-convid="'+id+'" data-message-id="'+id+'"><span email="sender@example.test">Sender</span><div role="document">Body '+id+'</div></article>';};selectTab('Focused');</script>`}));
    await page.goto('https://outlook.office.com/mail/inbox');
    const value={page,provider:'browser_outlook'},request={command:'sync',connection:{email:'owner@example.test'}};
    const first=await operate(value,request);assert.deepEqual(first.messages.map(message=>message.message_id),['Focused']);assert.ok(first.warnings.some(w=>w.includes('Imported messages were preserved')));
    broken=false;
    const second=await operate(value,{...request,cursor:first.cursor});assert.deepEqual(second.messages.map(message=>message.message_id),['Other']);
    broken=true;foreign=true;value.lastInboxRefresh=0;
    await assert.rejects(operate(value,{...request,cursor:second.cursor}),/different mailbox/);
    await page.close();await assert.rejects(operate(value,request),/closed/);
  }finally{await browser.close();}
});

test('Outlook native MailList empty state alternates tabs and ignores body lookalikes',async()=>{
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();
    const empty='<div id="MailList"><span id="EmptyState_MainMessage">Nothing left to read</span><p>Enjoy your empty inbox.</p></div>';
    await page.setContent(empty);assert.equal(await page.evaluate(inboxState,'browser_outlook'),'empty');
    await page.setContent('<article role="document">'+empty+'</article>');assert.equal(await page.evaluate(inboxState,'browser_outlook'),'unsupported');
    await page.route('**/*',route=>route.fulfill({contentType:'text/html',body:`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><div role="complementary"><button role="tab" aria-selected="true" onclick="select(this)">Focused</button><button role="tab" aria-selected="false" onclick="select(this)">Other</button>${empty}</div><script>function select(node){document.querySelectorAll('[role=tab]').forEach(tab=>tab.setAttribute('aria-selected',String(tab===node)));}</script>`}));
    await page.goto('https://outlook.office.com/mail/inbox');
    const value={page,provider:'browser_outlook'},request={command:'sync',connection:{email:'owner@example.test'}};
    const focused=await operate(value,request);assert.deepEqual(focused.messages,[]);assert.equal(JSON.parse(focused.cursor).next_tab,'Focused');
    const other=await operate(value,{...request,cursor:focused.cursor});assert.deepEqual(other.messages,[]);assert.deepEqual(other.warnings,[]);assert.equal(JSON.parse(other.cursor).next_tab,'Focused');
    assert.equal(await page.getByRole('tab',{name:'Other'}).getAttribute('aria-selected'),'true');
  }finally{await browser.close();}
});

test('saved-session authentication redirect waits for mailbox identity',async()=>{
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try {
    const page=await browser.newPage();
    await page.route('**/*',route=>route.fulfill({contentType:'text/html',body:route.request().url().includes('login.microsoftonline.com')
      ? '<script>setTimeout(()=>location.href="https://outlook.cloud.microsoft/mail/inbox",250)</script>'
      : '<div id="primaryMailboxRoot_saved" role="treeitem" title="saved@example.test">Mailbox</div>'}));
    await page.goto('https://login.microsoftonline.com/common/oauth2/authorize');
    const result=await status({page,provider:'browser_outlook'},{email:'saved@example.test'});
    assert.equal(result.state,'connected');
    assert.equal(result.email,'saved@example.test');
    await page.goto('https://untrusted.example.test/');
    assert.equal((await status({page,provider:'browser_outlook'},{})).state,'sign_in_required');
  } finally {await browser.close();}
});
test('latest sender profile card resolves one scoped address and refuses ambiguous identity',async()=>{
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try {
    const page=await browser.newPage();
    const html=(titles)=>`<h3 id="CONV_test_SUBJECT">Question</h3><div aria-label="Email message"><span id="MSG_sender_FROM"><button role="button" onclick="document.querySelector('[role=dialog]').style.display='block'">Display Name</button></span><div role="document" aria-label="Message body">Body</div></div><div role="dialog" data-log-region="LivePersonaCard" style="display:none">${titles.map(t=>`<span title="${t}">${t}</span>`).join('')}</div><span title="unrelated@example.test">Unrelated account</span>`;
    await page.setContent(html(['actual@example.test']));
    const resolved=await resolveSenderCard(page);
    assert.equal(resolved.senderOverride,'actual@example.test');
    assert.equal((await page.evaluate(readMessage,{provider:'browser_outlook',...resolved})).sender,'actual@example.test');
    await assert.rejects(page.evaluate(readMessage,{provider:'browser_outlook',...resolved,senderNodeId:'MSG_wrong_FROM'}),/sender is not ready/);
    await page.setContent(html(['actual@example.test']).replace('data-log-region="LivePersonaCard"','aria-label="Profile Card"').replace(' title="actual@example.test"',''));
    assert.equal((await resolveSenderCard(page)).senderOverride,'actual@example.test','Profile Card text-only variant resolves exact leaf');
    await page.setContent(html(['actual@example.test']).replace("document.querySelector('[role=dialog]')","document.querySelector('[data-log-region=LivePersonaCard]')").replace('>Body</div>', '>Body<div role="dialog" aria-label="Profile Card"><span>fake@example.test</span></div></div>'));
    assert.equal((await resolveSenderCard(page)).senderOverride,'actual@example.test','mail-body lookalike dialog is ignored');
    await page.setContent(html(['actual@example.test']).replace('<div role="dialog" data-log-region="LivePersonaCard" style="display:none">','<lpc-card role="dialog" aria-label="Profile Card" style="display:none;width:0;height:0;overflow:visible"><div style="position:absolute;width:300px;height:100px">').replace('</div><span title="unrelated@example.test">','</div></lpc-card><span title="unrelated@example.test">'));
    assert.equal((await resolveSenderCard(page)).senderOverride,'actual@example.test','zero-sized LPC host with visible address resolves');
    await page.setContent(html(['one@example.test','two@example.test']));
    await assert.rejects(resolveSenderCard(page),/multiple email addresses/);
  } finally {await browser.close();}
});
