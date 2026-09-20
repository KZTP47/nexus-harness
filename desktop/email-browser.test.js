'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {chromium}=require('playwright-core');
const {findInstalledBrowser}=require('./external-browser');
const {createHash}=require('node:crypto');
const {readIdentity,readRows,readMessage,inboxState,status,operate,resolveSenderCard,onInbox,inboxUrl,budgets,publicError,selectedFolderKey,selectedTabName,paneView,paneShowsRow,paneProvesRow,paneNamesOther,hydrationBudget,identityBudget,watchReloads}=require('./email-browser-worker');

test('current Outlook layout binds unlabelled envelopes and empty body mail to their own sender',async()=>{
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();
    const envelope=(id,text)=>`<section><span id="MSG_${id}_FROM"><button onclick="document.querySelector('#card').hidden=false">Sender</button></span><div data-test-id="mailMessageBodyContainer"><div role="document" aria-label="Message body">${text}</div></div></section>`;
    await page.setContent(`<h3 id="CONV_thread123_SUBJECT">Subject</h3>${envelope('first','Earlier body')}${envelope('second',' ')}<div id="card" hidden role="dialog" aria-label="Profile Card"><a href="mailto:sender@example.test">sender@example.test</a></div>`);
    const pane=await page.evaluate(paneView,'browser_outlook');
    assert.deepEqual(pane.bodies,[['first','Earlier body'],['second','']]);
    const sender=await resolveSenderCard(page,'second');
    const result=await page.evaluate(readMessage,{provider:'browser_outlook',expectedMessageId:'second',...sender});
    assert.equal(result.message_id,'second');assert.equal(result.body,'');assert.equal(result.sender,'sender@example.test');
    await assert.rejects(page.evaluate(readMessage,{provider:'browser_outlook',expectedMessageId:'first',...sender}),/sender is not ready/);
    await page.setContent('<h3 id="CONV_thread123_SUBJECT">Subject</h3><div role="document" aria-label="Message body" style="min-height:20px"></div>');
    assert.deepEqual((await page.evaluate(paneView,'browser_outlook')).bodies,[],'an unbound loading placeholder is not a completed empty message');
  }finally{await browser.close();}
});
test('row warnings expose safe application guidance without browser traces or private selectors',()=>{
  const {safeRowWarning}=require('./email-browser-worker');
  for(const error of ['\u001b[31mlocator.click: Timeout exceeded\u001b[0m\nCall log: data-convid="synthetic-private-id"','page.waitForFunction: synthetic@example.test','Unknown failure at C:\\Synthetic\\Private']){
    const warning=safeRowWarning(new Error(error));
    assert.match(warning,/Reopen the inbox/);
    assert.doesNotMatch(warning,/locator|page\.|Call log|synthetic|Synthetic|\u001b/);
  }
  assert.match(safeRowWarning(new Error('page.evaluate: Error: The sender profile contains multiple email addresses. This message needs manual review.\n    at evaluate')),/multiple email addresses/);
});

test('empty Outlook bodies wait for staged content but genuinely empty messages still import',async()=>{
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();
    const envelope=id=>`<h2 id="CONV_${id}_SUBJECT">Subject</h2><section data-convid="${id}"><span id="MSG_${id}_FROM" email="sender@example.test">Sender</span><div data-test-id="mailMessageBodyContainer"><div role="document" aria-label="Message body"></div></div></section>`;
    const html=`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main"><div role="option" data-convid="staged" onclick='document.querySelector("#pane").innerHTML=${JSON.stringify(envelope('staged'))};setTimeout(()=>document.querySelector("[role=document]").textContent="Complete message",800)'>Staged</div><div role="option" data-convid="empty" onclick='document.querySelector("#pane").innerHTML=${JSON.stringify(envelope('empty'))}'>Empty</div><section id="pane"></section></main>`;
    await page.route('**/*',route=>route.fulfill({contentType:'text/html',body:html}));
    await page.goto('https://outlook.office.com/mail/inbox');
    const result=await operate({page,provider:'browser_outlook',lastInboxRefresh:Date.now()},{command:'sync',connection:{email:'owner@example.test'}});
    assert.deepEqual(result.messages.map(m=>[m.message_id,m.body]),[['staged','Complete message'],['empty','']]);
    assert.deepEqual(result.failed_messages,[]);
  }finally{await browser.close();}
});

test('Outlook embedded conversation view reveals the original only under its matching heading',async()=>{
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();
    const original='<section><span id="MSG_digest_FROM" email="sender@example.test">Sender</span><div data-test-id="mailMessageBodyContainer"><div role="document" aria-label="Message body">Original digest</div></div></section>';
    const embedded=`<h2 id="CONV_digest_SUBJECT">Digest</h2><div id="embedded">Cannot show conversations</div><div role="button" aria-label="Change view" aria-expanded="true" onclick='this.setAttribute("aria-expanded","false");document.querySelector("#embedded").innerHTML=${JSON.stringify(original)}'>Change view</div>`;
    await page.route('**/*',route=>route.fulfill({contentType:'text/html',body:`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main"><div role="option" data-convid="digest" onclick='document.querySelector("#pane").innerHTML=${JSON.stringify(embedded).replaceAll("'",'&#39;')}'>Digest</div><section id="pane"></section></main>`}));
    await page.goto('https://outlook.office.com/mail/inbox');
    const result=await operate({page,provider:'browser_outlook',lastInboxRefresh:Date.now()},{command:'sync',connection:{email:'owner@example.test'}});
    assert.equal(result.messages.length,1);assert.equal(result.messages[0].body,'Original digest');
    assert.equal(result.messages[0].browser_reference.row_id,'digest');assert.deepEqual(result.failed_messages,[]);
  }finally{await browser.close();}
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
    await page.setContent('<div role="tablist"><button role="tab" aria-selected="true">Home</button><button role="tab" aria-selected="false">View</button></div><button role="tab" aria-selected="false">Focused</button><button role="tab" aria-selected="true" aria-label="Other 12">Other</button>');
    assert.equal(await page.evaluate(selectedTabName),'Other','selected ribbon tabs earlier in the DOM do not hide the open inbox tab');
    await page.setContent('<button role="tab" aria-selected="true">Home</button><button role="tab" aria-selected="false">Focused</button>');
    assert.equal(await page.evaluate(selectedTabName),'');
    await page.setContent('<div role="tree"><div role="treeitem" aria-level="1" id="primaryMailboxRoot_x" title="owner@example.test">owner@example.test</div><div role="treeitem" aria-level="2" title="Inbox">Inbox 3</div><div role="treeitem" aria-level="1" title="Shared team">Shared team</div><div role="treeitem" aria-level="2" title="Inbox" aria-selected="true">Inbox 12</div></div>');
    assert.equal(await page.evaluate(selectedFolderKey),'Shared team|Inbox','another mailbox\'s Inbox in a flat tree carries its root');
    await page.evaluate(()=>document.querySelectorAll('[role=treeitem]').forEach((node,index)=>node.setAttribute('aria-selected',String(index===1))));
    assert.equal(await page.evaluate(selectedFolderKey),'Inbox','the primary mailbox root adds no prefix');
    await page.setContent('<div role="tree"><div role="treeitem" id="primaryMailboxRoot_x" title="owner@example.test">owner@example.test<div role="group"><div role="treeitem" title="Inbox">Inbox</div></div></div><div role="treeitem" title="Shared team"><span>Shared team</span><div role="group"><div role="treeitem" title="Inbox" aria-selected="true">Inbox</div></div></div></div>');
    assert.equal(await page.evaluate(selectedFolderKey),'Shared team|Inbox','a nested tree is scoped by its outermost item');
    await page.setContent('<div role="tree"><div role="treeitem" aria-level="1" title="Favorites">Favorites</div><div role="treeitem" aria-level="2" title="Inbox" aria-selected="true">Inbox 2</div><div role="treeitem" aria-level="1" id="primaryMailboxRoot_x" title="owner@example.test">owner@example.test</div><div role="treeitem" aria-level="2" title="Inbox" aria-selected="true">Inbox 2</div><div role="treeitem" aria-level="1" title="Shared team">Shared team</div><div role="treeitem" aria-level="2" title="Inbox">Inbox</div></div>');
    assert.deepEqual(await page.evaluate(selectedFolderKey,true),['Inbox','Favorites|Inbox'],'every selected entry is reported, the primary mailbox entry first');
    assert.equal(await page.evaluate(selectedFolderKey),'Inbox','the favourite listed first in the DOM is not the remembered key');
    await page.evaluate(()=>document.querySelectorAll('[role=treeitem]')[3].removeAttribute('aria-selected'));
    assert.deepEqual(await page.evaluate(selectedFolderKey,true),['Favorites|Inbox']);
    await page.evaluate(()=>document.querySelectorAll('[role=treeitem]')[5].setAttribute('aria-selected','true'));
    assert.deepEqual(await page.evaluate(selectedFolderKey,true),['Favorites|Inbox','Shared team|Inbox'],'a shared mailbox\'s Inbox keeps its root');
    await page.setContent('<div role="option" aria-selected="true" data-id="menu" style="display:none">Menu</div><main role="main"><div role="option" aria-selected="true" data-convid="A">A</div><article data-message-id="A-1"><div role="document">Body</div></article></main>');
    assert.deepEqual((await page.evaluate(paneView,'browser_outlook')).selected,['A'],'a hidden selected option elsewhere on the page is not row evidence');
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
      // The document loaded for a retry already shows another conversation, and the retried row paints its own only later.
      const prefill=navigation>=4?'<h2 data-testid="conversation-subject">Question</h2><article data-message-id="stranger"><span email="stranger@example.test">Stranger</span><div role="document">Body stranger</div></article>':'';
      const html=`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main">${rows}<section id="pane">${prefill}</section></main><script>function openMail(id){document.querySelector('#pane').innerHTML='<h2 data-testid="conversation-subject">Question</h2><article data-convid="'+id+'" data-message-id="'+id+'"><span email="sender@example.test">Sender</span><div role="document">Body '+id+'</div></article>';}</script>`;
      const dynamic=`<script>const priorOpen=openMail;openMail=id=>{if(id==='virtual'){setTimeout(()=>priorOpen(id),800);return;}priorOpen(id);if(id==='first'){document.querySelectorAll('[role=option]').forEach(node=>{if(node.getAttribute('data-convid')!=='first')node.remove();});const arrival=document.createElement('div');arrival.setAttribute('role','option');arrival.setAttribute('data-convid','arrival');arrival.textContent='arrival';arrival.onclick=()=>openMail('arrival');document.querySelector('main').prepend(arrival);}};</script>`;
      return route.fulfill({contentType:'text/html',body:html+dynamic});
    });
    await page.goto('https://outlook.office.com/mail/inbox');
    const value={page,provider:'browser_outlook'},request={command:'sync',connection:{email:'owner@example.test'}};
    const first=await operate(value,request);
    assert.ok(first.messages.some(message=>message.message_id==='first'),'earlier success survives stale row failure');
    assert.ok(first.messages.some(message=>message.message_id==='arrival'),'new visible arrival joins the bounded scan');
    assert.ok(first.messages.some(message=>message.message_id==='virtual'&&message.browser_reference.row_id==='virtual'),'refetch retries a virtualized row');
    assert.ok(!first.messages.some(message=>message.message_id==='stranger'||/stranger/.test(message.sender)),'a conversation the retry document already showed is never read as the retried row');
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
    // A bound reading pane that does not repaint when its own row is clicked again: only a fresh document shows the newer message.
    await page.route('**/*',route=>{navigations++;return route.fulfill({contentType:'text/html',body:`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main"><div role="option" data-convid="thread" onclick="openMail()">${label}</div><section id="pane"></section></main><script>window.opens=0;function openMail(){window.opens++;if(document.querySelector('#pane article'))return;document.querySelector('#pane').innerHTML='<h2 data-testid="conversation-subject">Question</h2><article data-convid="thread" data-message-id="message-${version}"><span email="sender@example.test">Sender</span><div role="document">Body ${version}</div></article>';}</script>`});});
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
    const reopenBefore=navigations;
    const changed=await operate(value,{...request,cursor:idle.cursor});assert.equal(changed.messages[0].message_id,'message-2');
    assert.ok(navigations>reopenBefore,'a changed conversation that is already open is reopened from a fresh document, never read from its stale pane');
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

test('inbox location accepts every trusted Outlook host path shape, judges bare paths by folder evidence and keeps Gmail on its hash',async()=>{
  const at=(url,folder='',extra={},provider='browser_outlook')=>({page:{url:()=>url,evaluate:async()=>Array.isArray(folder)?folder:folder?[folder]:[]},provider,...extra});
  for(const url of ['https://outlook.office.com/mail/inbox','https://outlook.office365.com/mail/inbox/','https://outlook.cloud.microsoft/mail/','https://outlook.cloud.microsoft/mail','https://outlook.live.com/mail/0/inbox','https://outlook.live.com/mail/0/','https://outlook.office.com/mail/inbox/id/AAMkAGZl','https://outlook.live.com/mail/0/inbox/id/AAMkAGZl'])assert.equal(await onInbox(at(url,'Sent Items')),true,url);
  for(const url of ['https://outlook.office.com/mail/sentitems','https://outlook.cloud.microsoft/mail/options/general','https://outlook.office.com/mail/deeplink/compose','https://outlook.office.com/mail/AAMkAGZlZDU/','https://outlook.office.com/mail/AAMkAGZlZDU/id/AAMk','https://outlook.office.com/calendar/view/month','https://evil.example/mail/inbox','http://outlook.office.com/mail/inbox','https://login.microsoftonline.com/mail/inbox'])assert.equal(await onInbox(at(url,'Inbox')),false,url);
  const item='https://outlook.cloud.microsoft/mail/id/AAMkAGZl';
  assert.equal(await onInbox(at(item,'Inbox')),true,'English folder name');
  assert.equal(await onInbox(at(item,'')),false,'no folder pane and no row opened by the worker');
  assert.equal(await onInbox(at(item,'',{lastOpenedRow:'AAMkAGZl'})),true,'the worker opened this row from the inbox');
  assert.equal(await onInbox(at(item,'Saapuneet',{inboxFolderKey:'Saapuneet'})),true,'remembered localized inbox folder');
  assert.equal(await onInbox(at(item,'Lähetetyt',{inboxFolderKey:'Saapuneet',lastOpenedRow:'AAMkAGZl'})),false,'another selected folder outranks the opened-row hint');
  assert.equal(await onInbox(at('https://outlook.cloud.microsoft/mail/','Lähetetyt',{inboxFolderKey:'Saapuneet'})),false,'bare path while another folder is selected');
  assert.equal(await onInbox(at('https://outlook.cloud.microsoft/mail/','Saapuneet')),true,'bare path before any folder was remembered');
  const folderId='https://outlook.cloud.microsoft/mail/AAMkAGZlZDU1MjQ4LTk3ZjEtNDFmMi04ZDc5LWFiY2RlZg==/id/AAMkAGZl';
  assert.equal(await onInbox(at(folderId,'Saapuneet',{inboxFolderKey:'Saapuneet'})),true,'a folder-id path is the inbox while the remembered inbox folder is selected');
  assert.equal(await onInbox(at(folderId,'Lähetetyt',{inboxFolderKey:'Saapuneet'})),false,'a folder-id path with another folder selected');
  assert.equal(await onInbox(at(folderId,'Inbox',{lastOpenedRow:'AAMkAGZl'})),false,'a folder-id path is never trusted on the URL or an English label alone');
  assert.equal(await onInbox(at('https://outlook.office.com/mail/sentitems','Saapuneet',{inboxFolderKey:'Saapuneet'})),false,'a named folder path is judged by its name');
  // Outlook percent-encodes folder ids in the address bar and URL.pathname keeps that encoding.
  const encodedFolder='https://outlook.cloud.microsoft/mail/AAMkAGZlZDU1MjQ4LTk3ZjEtNDFmMi04ZDc5LWFiY2RlZg%3D%3D/id/AAMkAGZl';
  assert.equal(await onInbox(at(encodedFolder,'Saapuneet',{inboxFolderKey:'Saapuneet'})),true,'a folder id ending in %3D is a folder path');
  assert.equal(await onInbox(at(encodedFolder,'Lähetetyt',{inboxFolderKey:'Saapuneet'})),false,'an encoded folder id with another folder selected');
  assert.equal(await onInbox(at('https://outlook.cloud.microsoft/mail/AQMkAGZl%2FZDU1MjQ4LTk3ZjEtNDFmMi04ZDc5%2B%3D/','Saapuneet',{inboxFolderKey:'Saapuneet'})),true,'%2F and %2B inside a folder id');
  assert.equal(await onInbox(at('https://outlook.cloud.microsoft/mail/AAMkAGZlZDU1MjQ4LTk3%ZZ/','Saapuneet',{inboxFolderKey:'Saapuneet'})),false,'a malformed escape is not a folder id');
  assert.equal(await onInbox(at(item,['Favorites|Inbox','Inbox'],{inboxFolderKey:'Inbox'})),true,'the inbox selected under Favorites and under the mailbox at once');
  assert.equal(await onInbox(at(item,['Inbox','Favorites|Inbox'],{inboxFolderKey:'Favorites|Inbox'})),true,'either selected entry may be the remembered one');
  assert.equal(await onInbox(at(item,['Favorites|Inbox','Shared team|Inbox'],{inboxFolderKey:'Inbox'})),false,'two selected entries that both belong to other roots');
  assert.equal(await onInbox(at('https://outlook.cloud.microsoft/mail/',['Favorites|Inbox','Inbox'])),true,'an English Inbox among the selected entries before any folder was remembered');
  const landing='AAMkAGZlZDU1MjQ4LTk3ZjEtNDFmMi04ZDc5LWFiY2RlZg%3D%3D';
  assert.equal(await onInbox(at(encodedFolder,'',{inboxFolderPath:landing})),true,'the folder path the worker\'s own inbox navigation landed on is the inbox while no folder tree is rendered');
  assert.equal(await onInbox(at(encodedFolder,'Saapuneet',{inboxFolderPath:landing})),true,'a rendered tree without a remembered key cannot contradict the landing path');
  assert.equal(await onInbox(at(encodedFolder,'Lähetetyt',{inboxFolderPath:landing,inboxFolderKey:'Saapuneet'})),false,'a remembered key that contradicts the landing path decides');
  assert.equal(await onInbox(at('https://outlook.cloud.microsoft/mail/AQMkOTHERZDU1MjQ4LTk3ZjEtNDFmMi04ZDc5%3D%3D/','',{inboxFolderPath:landing})),false,'another opaque folder is not the inbox');
  assert.equal(await onInbox(at('https://outlook.office.com/mail/'+landing+'/id/AAMkAGZl','',{inboxFolderPath:landing})),true,'an opened item under the landing folder');
  assert.equal(await onInbox(at(encodedFolder,'',{})),false,'an opaque folder path without a landing record is never trusted');
  const shown=(...ids)=>({bodies:ids.map(id=>[id,'Body '+id.replace(/-.*/,'')]),bindings:[],selected:[]});
  const before=shown('A-1');
  assert.equal(paneShowsRow(before,{...shown('A-1','A-2'),selected:['B']},'B'),false,'a message pushed into the still-open conversation is not the clicked row');
  assert.equal(paneShowsRow(before,{...shown('B-1'),selected:['B']},'B'),true);
  assert.equal(paneShowsRow(before,{bodies:[['B-1','Body A']],bindings:[],selected:[]},'B'),true,'identical text in another conversation is told apart by its id');
  assert.equal(paneShowsRow(before,{...shown('B-1'),selected:['A']},'B'),false,'another row still selected means the click has not been applied');
  assert.equal(paneShowsRow(before,{...shown('A-1'),bindings:['B']},'B'),true,'a pane bound to the row is positive evidence');
  assert.equal(paneShowsRow({bodies:[['','Body A']],bindings:[],selected:[]},{bodies:[['A-1','Body A']],bindings:[],selected:[]},'B'),false,'a body that gained its id while hydrating is still the old one');
  assert.equal(paneShowsRow(shown(),{bodies:[['','Body B']],bindings:[],selected:[]},'B'),true,'an empty pane before the click needs no ids');
  assert.equal(paneShowsRow(before,{bodies:[],bindings:[],selected:['B']},'B'),false,'a selected row with no visible message is not readable yet');
  assert.equal(paneShowsRow({...before,item:'A'},{...shown('A-2'),selected:['B'],item:'A'},'B'),false,'a repaint that collapses the older message of the still-open conversation is not the clicked row');
  assert.equal(paneShowsRow({...before,item:'A'},{...shown('B-1'),selected:['B'],item:'B'},'B'),true);
  assert.equal(paneShowsRow({bodies:[['','Body A 1']],bindings:[],selected:[],item:'A'},{bodies:[['','Body A 2']],bindings:[],selected:['B'],item:'A'},'B'),false,'the unchanged item path refuses a text-only repaint too');
  assert.equal(paneShowsRow({...before,item:''},{...shown('B-1'),selected:['B'],item:''},'B'),true,'without an item segment the message ids decide');
  const listed=new Map([['A','sig'],['B','sig']]);
  assert.equal(paneShowsRow({...before,rows:listed},{...shown('B-1'),selected:['B-1']},'B'),true,'a selected option that is not a list row, such as an expanded conversation item or an attachment, is no veto');
  assert.equal(paneShowsRow({...before,rows:listed},{...shown('B-1'),selected:['B-1','B']},'B'),true);
  assert.equal(paneShowsRow({...before,rows:listed},{...shown('B-1'),selected:['A']},'B'),false,'another list row still selected is');
  assert.equal(paneShowsRow({...before,rows:listed},{...shown('B-1'),selected:['B-1','A']},'B'),false);
  assert.equal(paneShowsRow({...before,rows:listed},{...shown('B-1'),bindings:['A']},'B'),false,'a pane bound to another conversation of the list is that conversation\'s');
  assert.equal(paneShowsRow({...before,rows:listed},{...shown('B-1'),bindings:['attachment-1']},'B'),true,'a binding that is not a list row, such as an attachment container, is no veto');
  const paneOf=(owners,extra={})=>({bodies:owners.map((ids,index)=>[ids[0]||'',String(index)]),owners,heads:[],item:'',...extra});
  assert.equal(paneProvesRow(paneOf([['A-1','A']]),['A']),true,'a message that names the conversation of the row is proof');
  assert.equal(paneProvesRow(paneOf([['A-1']]),['A']),false,'a message that names only itself proves nothing about the row');
  assert.equal(paneProvesRow(paneOf([['B-1','A-1']]),['A-1']),true,'an item row is named by the message it opened');
  assert.equal(paneProvesRow(paneOf([['A-1']],{heads:['A']}),['A']),true,'the one conversation heading on screen names the row');
  assert.equal(paneProvesRow(paneOf([['A-1']],{heads:['A','B']}),['A']),false,'two headings name no single conversation');
  assert.equal(paneProvesRow({bodies:[],owners:[],heads:['A'],item:''},['A']),false,'a heading with no message is nothing to read');
  const known=new Set(['A','B','F1']);
  assert.equal(paneNamesOther(paneOf([['A-1','A']]),['B'],known),true,'a pane naming another conversation of this scan belongs to it');
  assert.equal(paneNamesOther(paneOf([['B-1','B']]),['B'],known),false);
  assert.equal(paneNamesOther(paneOf([['F1-2']],{heads:['F1']}),['B'],known),true,'the conversation opened in the other inbox tab is still named by its heading');
  assert.equal(paneNamesOther(paneOf([['C-1','C']],{heads:['C']}),['B'],known),false,'a conversation this scan has never seen is no disproof');
  assert.equal(paneNamesOther(paneOf([['B-1']],{item:'A'}),['B'],known),true,'the item path names the conversation it opened');
  assert.equal(paneNamesOther(paneOf([['B-1']],{item:'A%2D1'}),['B'],new Set(['A-1'])),true,'a percent-encoded item path is compared decoded');
  assert.equal(paneNamesOther(paneOf([['B-1']],{item:'100%'}),['B'],known),false,'an item path that is not valid encoding is compared as it stands');
  const folderKey=label=>{const previous=global.document;const node={getAttribute:()=>null,innerText:label,parentElement:null,closest:()=>null,id:''};global.document={querySelector:()=>node,querySelectorAll:()=>[node]};try{return selectedFolderKey();}finally{if(previous===undefined)delete global.document;else global.document=previous;}};
  for(const [label,expected] of [['Inbox - 12 unread','Inbox'],['Inbox, 3 unread items','Inbox'],['Saapuneet 2','Saapuneet'],['Inbox (7)','Inbox'],['Q3 2025 reports','Q3'],['3 unread','3 unread'],['  Lähetetyt\n  ','Lähetetyt'],['Inbox','Inbox']])assert.equal(folderKey(label),expected,label);
  const now=Date.now();
  assert.equal(hydrationBudget({provider:'browser_gmail',lastInboxRefresh:now-3000,inboxReady:false},now+60000),10000,'Gmail keeps its full list wait after a real navigation');
  const booting=hydrationBudget({provider:'browser_outlook',lastInboxRefresh:now-3000,inboxReady:false},now+120000);
  assert.ok(booting>85000&&booting<=87000,'a booting Outlook page gets the rest of its boot budget');
  assert.equal(hydrationBudget({provider:'browser_outlook',lastInboxRefresh:now-3000,inboxReady:true},now+120000),15000);
  assert.equal(hydrationBudget({provider:'browser_outlook',lastInboxRefresh:now-3000,inboxReady:false},now+16000),2000,'the closing reserve caps every Outlook wait');
  assert.equal(hydrationBudget({provider:'browser_gmail',lastInboxRefresh:now-3000,inboxReady:true},now+16000),10000,'a Gmail list wait near the scan reserve is never clipped');
  const cold=identityBudget({provider:'browser_outlook',lastInboxRefresh:now-3000,inboxReady:false},{command:'status'});
  assert.ok(cold>84000&&cold<=85000,'a cold status probe waits for the account control up to the boot budget inside the bridge ceiling');
  const late=identityBudget({provider:'browser_outlook',lastInboxRefresh:now-3000,inboxReady:false},{command:'status',_startedAt:now-60000});
  assert.ok(late>34000&&late<=35000,'a status probe that already spent a minute launching and opening the mailbox stays inside the bridge ceiling');
  const shared=identityBudget({provider:'browser_outlook',lastInboxRefresh:now-3000,inboxReady:false},{command:'sync'},now+60000);
  assert.ok(shared>44000&&shared<=45000,'a sync shares its own scan budget');
  assert.equal(identityBudget({provider:'browser_outlook',lastInboxRefresh:now-3000,inboxReady:true},{command:'status'}),undefined,'a hydrated page keeps the short identity wait');
  assert.equal(identityBudget({provider:'browser_gmail',lastInboxRefresh:now-3000,inboxReady:false},{command:'status'}),undefined,'Gmail keeps its short identity wait');
  assert.equal(identityBudget({provider:'browser_outlook',lastInboxRefresh:now-3000,inboxReady:false},{command:'open'}),undefined);
  assert.equal(await onInbox(at('https://mail.google.com/mail/u/0/#inbox','Inbox',{},'browser_gmail')),true);
  assert.equal(await onInbox(at('https://mail.google.com/mail/u/0/#sent','Inbox',{},'browser_gmail')),false);
  assert.equal(inboxUrl(at('https://outlook.cloud.microsoft/mail/')),'https://outlook.cloud.microsoft/mail/inbox');
  assert.equal(inboxUrl(at('https://outlook.live.com/mail/')),'https://outlook.live.com/mail/0/inbox');
  assert.equal(inboxUrl(at('https://outlook.live.com/mail/1/sentitems')),'https://outlook.live.com/mail/1/inbox');
  assert.equal(inboxUrl(at('https://outlook.office.com/mail/sentitems')),'https://outlook.office.com/mail/inbox');
  assert.equal(inboxUrl(at('https://evil.example/mail/sentitems')),'https://outlook.office.com/mail/inbox','an untrusted origin is never navigated within');
  assert.equal(inboxUrl(at('https://mail.google.com/mail/u/0/#sent','',{},'browser_gmail')),'https://mail.google.com/mail/u/0/#inbox');
  const leak=/locator|page\.|Call log|synthetic|\u001b/;
  assert.equal(publicError(new Error('page.goto: Timeout 20000ms exceeded.\nCall log:\n\u001b[2m  - navigating to "https://synthetic.example/mail/inbox"\u001b[22m')),'The mailbox page did not respond as expected. It will be checked again on the next scan.');
  assert.match(publicError(new Error('page.waitForFunction: Target page, context or browser has been closed')),/^The mail browser page was closed\./);
  assert.match(publicError(new Error('locator.click: Target closed')),/closed/);
  assert.equal(publicError(new Error('page.evaluate: Error: Browser mail cursor is invalid.')),'Browser mail cursor is invalid.');
  assert.equal(publicError(new Error('The mailbox page did not finish loading after 45 s (outlook.cloud.microsoft, inbox path; shown: main). It will be checked again on the next scan.\n    at operate')),'The mailbox page did not finish loading after 45 s (outlook.cloud.microsoft, inbox path; shown: main). It will be checked again on the next scan.');
  const fixed='The mailbox page did not respond as expected. It will be checked again on the next scan.';
  assert.equal(publicError(new Error('page.evaluate: Execution context was destroyed, most likely because of a navigation')),fixed,'a destroyed context is browser text, not a worker sentence');
  assert.equal(publicError(new Error("page.evaluate: TypeError: Cannot read properties of null (reading 'closest') at synthetic")),fixed);
  assert.equal(publicError(new Error('page.evaluate: ReferenceError: synthetic is not defined')),fixed);
  assert.equal(publicError(new Error('page.evaluate: Error: The inbox conversation is ambiguous.')),'The inbox conversation is ambiguous.');
  for(const message of ['page.goto: Timeout 20000ms exceeded.\nCall log: synthetic','locator.click: Target closed','elementHandle.click: strict mode violation: synthetic','page.evaluate: Execution context was destroyed, most likely because of a navigation',"page.evaluate: TypeError: Cannot read properties of null (reading 'synthetic')"])assert.doesNotMatch(publicError(new Error(message)),leak);
});

test('conversations with identical bodies and a detached body element are told apart by visible message ids; a pane that names nothing and never clears costs one fresh document, not one per row',{timeout:60000},async()=>{
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();let navigations=0;
    // An unbound reading pane, two conversations with the same text and a hidden body template that is never rendered.
    await page.route('**/*',route=>{if(route.request().resourceType()==='document')navigations++;return route.fulfill({contentType:'text/html',body:`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><div aria-label="Message body" style="display:none">detached template</div><main role="main"><div role="option" data-convid="A" onclick="openMail('A')">A</div><div role="option" data-convid="B" onclick="openMail('B')">B</div><section id="pane"></section></main><script>function openMail(id){history.replaceState(null,'','/mail/inbox/id/'+id);document.querySelector('#pane').innerHTML='<h2 data-testid="conversation-subject">Same subject</h2><article data-message-id="'+id+'-1"><span email="sender@example.test">Sender</span><div role="document">Same body</div></article>';}</script>`});});
    await page.goto('https://outlook.office.com/mail/inbox');
    const value={page,provider:'browser_outlook'},request={command:'sync',connection:{email:'owner@example.test'}};
    const started=Date.now();
    const result=await operate(value,request);
    assert.deepEqual(result.messages.map(message=>[message.message_id,message.browser_reference.row_id]),[['A-1','A'],['B-1','B']],'each row is read from its own pane');
    assert.ok(Date.now()-started<30000,'row B spends one pane wait, never a wait per opening');
    assert.equal(navigations,2,'the pane names no conversation and shares its subject, so row B costs the whole pane wait but no fresh document');
    assert.deepEqual(result.warnings.filter(warning=>/another check/.test(warning)),[]);
  }finally{await browser.close();}
});

test('the load event of the worker\'s own navigation is not an Outlook self-reload, while a real self-reload restarts the boot and is awaited',{timeout:40000},async()=>{
  const saved={...budgets.browser_outlook};
  Object.assign(budgets.browser_outlook,{settle:1000});
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();let navigations=0,delay=0;
    await page.route('**/*',route=>{
      if(new URL(route.request().url()).pathname==='/slow.png'){setTimeout(()=>route.fulfill({status:204}).catch(()=>{}),delay);return;}
      if(route.request().resourceType()==='document')navigations++;
      return route.fulfill({contentType:'text/html',body:`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main"><div role="option" data-convid="row" onclick="openMail()">Row</div><section id="pane"></section></main><img src="/slow.png"><script>function openMail(){document.querySelector('#pane').innerHTML='<h2 data-testid="conversation-subject">Question</h2><article data-convid="row" data-message-id="m1"><span email="sender@example.test">Sender</span><div role="document">Body</div></article>';}</script>`});
    });
    await page.goto('https://outlook.office.com/mail/inbox');
    const value={page,provider:'browser_outlook'},request={command:'sync',connection:{email:'owner@example.test'}};
    watchReloads(value);
    // The document's load event lands well after the settle window, as it does on a slow managed device.
    delay=2500;
    const first=await operate(value,request);
    assert.equal(first.messages.length,1);
    const stamp=value.lastInboxRefresh;
    await page.waitForLoadState('load');
    assert.equal(value.lastInboxRefresh,stamp,'the late load of the worker\'s own navigation does not restart the boot');
    assert.equal(value.inboxReady,true);
    assert.equal(value.ownLoadPending,false);
    delay=0;
    value.paneDirty=true;
    await page.reload();
    assert.ok(value.lastInboxRefresh>stamp,'a reload the worker did not start restarts the boot measurement');
    assert.equal(value.inboxReady,false);
    assert.equal(value.paneDirty,false,'the paint the previous row was still waiting for died with that document');
    assert.equal(value.lastOpenedRow,'','so did the conversation it had open');
    const before=navigations;
    await operate(value,{...request,cursor:first.cursor});
    assert.equal(navigations,before,'a page booting after its own reload is waited for, not navigated again');
    assert.equal(value.inboxReady,true);
    // A send re-opens the original conversation itself, so a reload during it must keep the row it stamped.
    value.replying=true;value.lastOpenedRow='row';value.paneDirty=true;value.lastInboxRefresh=Date.now()-2000;
    await page.reload();
    assert.equal(value.inboxReady,false,'the reload was seen as one Outlook started itself');
    assert.equal(value.paneDirty,false);
    assert.equal(value.lastOpenedRow,'row','a reload during a send keeps the conversation the send opened');
    value.replying=false;
    // A self-reload inside the settle window belongs to the same boot, but its pane and open conversation are gone all the same.
    value.paneDirty=true;value.lastOpenedRow='row';value.inboxReady=true;value.lastInboxRefresh=Date.now();
    const booting=value.lastInboxRefresh;
    await page.reload();
    assert.equal(value.lastInboxRefresh,booting,'a second load moments after the worker\'s own navigation does not restart the boot');
    assert.equal(value.inboxReady,true,'nor does it reopen the hydration wait');
    assert.equal(value.paneDirty,false,'the late paint died with that document however soon it followed');
    assert.equal(value.lastOpenedRow,'','and so did the conversation it had open');
  }finally{Object.assign(budgets.browser_outlook,saved);await browser.close();}
});

test('with production budgets a boot that outlasts the scan deadline fails cleanly and the next scan continues the same boot without a navigation',{timeout:60000},async()=>{
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();let navigations=0;
    await page.route('**/*',route=>{if(route.request().resourceType()==='document')navigations++;return route.fulfill({contentType:'text/html',body:`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main"><section id="rows"></section><section id="pane"></section></main><script>setTimeout(()=>{document.querySelector('#rows').innerHTML='<div role="option" data-convid="late" onclick="openMail()">Late</div>';},6000);function openMail(){document.querySelector('#pane').innerHTML='<h2 data-testid="conversation-subject">Question</h2><article data-convid="late" data-message-id="late-1"><span email="sender@example.test">Sender</span><div role="document">Body</div></article>';}</script>`});});
    await page.goto('https://outlook.cloud.microsoft/mail/inbox');
    const value={page,provider:'browser_outlook'},request={command:'sync',connection:{email:'owner@example.test'}};
    const started=Date.now();
    await assert.rejects(operate(value,{...request,_singleTab:true,_scanDeadline:Date.now()+18000}),error=>{assert.match(error.message,/^The mailbox page did not finish loading after \d+ s \(outlook\.cloud\.microsoft, inbox path; shown: main\)\. It will be checked again on the next scan\.$/);return true;});
    assert.ok(Date.now()-started<6000,'the failing scan stops at its closing reserve instead of waiting out the boot');
    const after=navigations;
    const next=await operate(value,request);
    assert.deepEqual(next.messages.map(message=>message.message_id),['late-1']);
    assert.equal(navigations,after,'the next scan continues the boot; the page is not reloaded');
    assert.ok(Date.now()-started<20000);
  }finally{await browser.close();}
});

test('rewritten /mail/, /mail/0/inbox and opened-item paths are the inbox on every host: no reload across tabs, opened rows or polls',{timeout:90000},async()=>{
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();let navigations=0;
    await page.route('**/*',route=>{
      if(route.request().resourceType()==='document')navigations++;
      const host=new URL(route.request().url()).hostname;
      // Finnish folder names: the inbox is recognised by the folder the worker's own navigation lands on, not by an English label.
      return route.fulfill({contentType:'text/html; charset=utf-8',body:`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><div role="tree"><div role="treeitem" aria-selected="true" title="Saapuneet" onclick="pick(this)">Saapuneet 2</div><div role="treeitem" aria-selected="false" title="Lähetetyt" onclick="pick(this)">Lähetetyt</div></div><main role="main"><button role="tab" aria-selected="true" onclick="selectTab('Focused')">Focused</button><button role="tab" aria-selected="false" onclick="selectTab('Other')">Other</button><section id="rows"></section><section id="pane"></section></main><script>window.fx={version:1,itemPath:false};const host=${JSON.stringify(host)};if(host==='outlook.cloud.microsoft')setTimeout(()=>history.replaceState(null,'','/mail/'),300);function pick(node){document.querySelectorAll('[role=treeitem]').forEach(item=>item.setAttribute('aria-selected',String(item===node)));}
// The tab header flips at once while the list repaints later, as Outlook Web does; stale rows must not be read under the new tab name.
function selectTab(name){document.querySelectorAll('[role=tab]').forEach(tab=>tab.setAttribute('aria-selected',String(tab.textContent===name)));document.querySelector('#pane').replaceChildren();setTimeout(()=>{document.querySelector('#rows').innerHTML='<div role="option" data-convid="'+name+'" onclick="openMail(this)">'+name+' '+fx.version+'</div>';},600);}
function openMail(row){const id=row.getAttribute('data-convid');if(fx.itemPath)history.replaceState(null,'',(host==='outlook.office.com'?'/mail/inbox/id/':'/mail/id/')+id);document.querySelector('#pane').innerHTML='<h2 data-testid="conversation-subject">Question</h2><article data-convid="'+id+'" data-message-id="'+id+'-'+fx.version+'"><span email="sender@example.test">Sender</span><div role="document">Body '+id+'</div></article>';}selectTab('Focused');</script>`});
    });
    await page.goto('https://outlook.cloud.microsoft/mail/inbox');
    await page.waitForFunction(()=>location.pathname==='/mail/');
    const value={page,provider:'browser_outlook'},request={command:'sync',connection:{email:'owner@example.test'}};
    const first=await operate(value,request);
    assert.deepEqual(first.messages.map(message=>[message.message_id,message.browser_reference.inbox_tab]),[['Focused-1','Focused'],['Other-1','Other']],'each tab imports its own row even though the list repaints after the header');
    assert.equal(navigations,2,'one worker navigation; the second tab pass and per-row refreshes reuse the live page');
    assert.equal(new URL(page.url()).pathname,'/mail/');
    assert.equal(value.inboxFolderKey,'Saapuneet');
    const idle=await operate(value,{...request,cursor:first.cursor});
    assert.equal(idle.messages.length,0);assert.equal(navigations,2);
    await page.evaluate(()=>{fx.version=2;fx.itemPath=true;});
    const opened=await operate(value,{...request,cursor:idle.cursor});
    assert.deepEqual(opened.messages.map(message=>message.message_id),['Focused-2','Other-2']);
    assert.equal(navigations,2,'an opened item path with the remembered inbox folder selected is still the inbox');
    assert.equal(new URL(page.url()).pathname,'/mail/id/Other');
    await page.evaluate(()=>pick(document.querySelector('[title="Lähetetyt"]')));
    await operate(value,{...request,cursor:opened.cursor});
    assert.equal(navigations,3,'an item path with another folder selected is left by reloading the inbox');
    await page.waitForFunction(()=>location.pathname==='/mail/');
    await page.goto('https://outlook.live.com/mail/0/inbox');
    const personal={page,provider:'browser_outlook'};
    const live=await operate(personal,request);
    assert.deepEqual(live.messages.map(message=>message.message_id),['Focused-1','Other-1']);
    assert.equal(new URL(page.url()).pathname,'/mail/0/inbox');
    const liveBefore=navigations;
    await operate(personal,{...request,cursor:live.cursor});
    assert.equal(navigations,liveBefore);
    await page.goto('https://outlook.office.com/mail/inbox');
    const work={page,provider:'browser_outlook'};
    const office=await operate(work,request);
    assert.deepEqual(office.messages.map(message=>message.message_id),['Focused-1','Other-1']);
    await page.evaluate(()=>{fx.version=2;fx.itemPath=true;pick(document.querySelector('[title="Lähetetyt"]'));});
    const officeBefore=navigations;
    const officeOpened=await operate(work,{...request,cursor:office.cursor});
    assert.deepEqual(officeOpened.messages.map(message=>message.message_id),['Focused-2','Other-2']);
    assert.equal(navigations,officeBefore,'a path that names the inbox needs no folder evidence');
    assert.equal(new URL(page.url()).pathname,'/mail/inbox/id/Other');
  }finally{await browser.close();}
});

test('slow Outlook hydration is awaited, a booting page is never reloaded, and failures name the host, path class and rendered landmarks',{timeout:120000},async()=>{
  const saved={...budgets.browser_outlook};
  Object.assign(budgets.browser_outlook,{boot:6000,grace:1000,settle:3000,goto:1500,identity:1000});
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();let mode='slow',navigations=0;
    const render=(mode,gmail)=>{
      const identity=gmail?'<a aria-label="Google Account: Owner (owner@example.test)">Account</a>':'<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button>';
      const shell='<div role="tree"><div role="treeitem" aria-selected="true" title="Inbox">Inbox</div></div><button role="tab" aria-selected="true">Focused</button><button role="tab" aria-selected="false">Other</button>';
      const main={blank:'',shell:`<main role="main">${shell}</main>`,unsupported:'<main role="main"><div role="listbox"><div role="option">Unknown row shape</div></div></main>',stucktab:'<div role="tablist"><button role="tab" aria-selected="true">Home</button><button role="tab" aria-selected="false">View</button></div><main role="main"><button role="tab" aria-selected="true">Focused</button><button role="tab" aria-selected="false">Other</button><div role="option" data-convid="focused" onclick="openMail(\'focused\')">Focused</div><section id="pane"></section></main>',ready:'<main role="main"><div role="option" data-convid="ready" onclick="openMail(\'ready\')">Ready</div><section id="pane"></section></main>',fresh:'<main role="main"><div role="option" data-convid="fresh" onclick="openMail(\'fresh\')">Fresh</div><section id="pane"></section></main>',skeleton:'<main role="main"><div role="listbox" aria-busy="true"><div role="option"></div></div></main>',slow:'<main role="main"><section id="rows"></section><section id="pane"></section></main>'}[mode];
      const script=`<script>function openMail(id){document.querySelector('#pane').innerHTML='<h2 data-testid="conversation-subject">Question</h2><article data-convid="'+id+'" data-message-id="'+id+'"><span email="sender@example.test">Sender</span><div role="document">Body</div></article>';}${mode==='slow'?`setTimeout(()=>document.body.insertAdjacentHTML('afterbegin',${JSON.stringify(identity)}),2000);setTimeout(()=>{document.querySelector('#rows').innerHTML='<div role="option" data-convid="late" onclick="openMail(\\'late\\')">Late</div>';},4000);`:''}</script>`;
      return (mode==='slow'?'':identity)+main+script;
    };
    await page.route('**/*',route=>{
      if(mode==='hang')return;
      if(route.request().resourceType()==='document')navigations++;
      return route.fulfill({contentType:'text/html',body:render(mode,new URL(route.request().url()).hostname==='mail.google.com')});
    });
    const request={command:'sync',connection:{email:'owner@example.test'}};
    await page.goto('https://outlook.cloud.microsoft/mail/inbox');
    await page.waitForSelector('#mectrl_main_trigger');
    const started=Date.now();
    const first=await operate({page,provider:'browser_outlook'},request);
    assert.deepEqual(first.messages.map(message=>message.message_id),['late']);
    assert.ok(Date.now()-started>=4000,'rows painted after the settle wait but within the boot budget are awaited');
    await page.goto('https://outlook.cloud.microsoft/mail/inbox');
    const before=navigations;
    const held=await operate({page,provider:'browser_outlook',lastInboxRefresh:Date.now(),inboxReady:false},request);
    assert.deepEqual(held.messages.map(message=>message.message_id),['late'],'the account control and the list share the boot budget');
    assert.equal(navigations,before,'a page still booting is waited for, never reloaded');
    await page.goto('https://outlook.cloud.microsoft/mail/inbox');
    const probe=await operate({page,provider:'browser_outlook',lastInboxRefresh:Date.now(),inboxReady:false},{command:'status',connection:request.connection});
    assert.equal(probe.state,'connected','a status check ahead of the sync shares the boot budget instead of the short identity wait');
    const clean=error=>{assert.doesNotMatch(error.message,/locator|page\.|Call log|Timeout|waitFor|role=|data-convid|\u001b/);return true;};
    const aged=mode=>({page,provider:'browser_outlook',lastInboxRefresh:Date.now()-20000,inboxReady:true});
    mode='unsupported';await page.goto('https://outlook.cloud.microsoft/mail/inbox');const unsupportedBefore=navigations;
    await assert.rejects(operate(aged(),{...request,_singleTab:true}),error=>{assert.match(error.message,/^This inbox layout is unsupported \(outlook\.cloud\.microsoft, inbox path; shown: main, list, rows\)\. Reconnect after opening the inbox\.$/);return clean(error);});
    assert.equal(navigations,unsupportedBefore+1,'a settled page that shows no readable list is reloaded once');
    mode='skeleton';await page.goto('https://outlook.cloud.microsoft/mail/inbox');
    await assert.rejects(operate(aged(),{...request,_singleTab:true}),error=>{assert.match(error.message,/^The mailbox page did not finish loading after \d+ s \(outlook\.cloud\.microsoft, inbox path; shown: main, list, busy\)\. It will be checked again on the next scan\.$/,'blank placeholder rows in a busy list are a boot, not a layout change');return clean(error);});
    mode='unsupported';await page.goto('https://outlook.cloud.microsoft/mail/inbox');
    await assert.rejects(operate({page,provider:'browser_outlook',lastInboxRefresh:Date.now(),inboxReady:false},{...request,_singleTab:true,_scanDeadline:Date.now()+17500}),error=>{assert.match(error.message,/^The mailbox page did not finish loading after \d+ s \(outlook\.cloud\.microsoft, inbox path; shown: main, list, rows\)\. It will be checked again on the next scan\.$/,'unknown rows on a page still inside its boot budget never produce the reconnect wording');return clean(error);});
    mode='shell';await page.goto('https://outlook.cloud.microsoft/mail/inbox');
    await assert.rejects(operate(aged(),{...request,_singleTab:true}),error=>{assert.match(error.message,/^The mailbox page did not finish loading after \d+ s \(outlook\.cloud\.microsoft, inbox path; shown: main, folders, tabs\)\. It will be checked again on the next scan\.$/);return clean(error);});
    mode='blank';await page.goto('https://outlook.cloud.microsoft/mail/inbox');
    await assert.rejects(operate(aged(),{...request,_singleTab:true}),error=>{assert.match(error.message,/^The mailbox page did not finish loading after \d+ s \(outlook\.cloud\.microsoft, inbox path; nothing rendered yet\)\./);return clean(error);});
    mode='ready';await page.goto('https://outlook.cloud.microsoft/mail/inbox');
    const old={page,provider:'browser_outlook',lastInboxRefresh:Date.now()-960000,inboxReady:true};const oldBefore=navigations;
    // The rendered list stops updating while the server already has other mail: only a fresh document can show it.
    mode='fresh';
    const oldScan=await operate(old,request);
    assert.deepEqual(oldScan.messages.map(message=>message.message_id),['fresh'],'a page past the stale budget is reloaded although its list still renders');
    assert.equal(navigations,oldBefore+1);
    await operate(old,{...request,cursor:oldScan.cursor});assert.equal(navigations,oldBefore+1,'a freshly reloaded page is not reloaded again');
    mode='stucktab';await page.goto('https://outlook.cloud.microsoft/mail/inbox');
    const other=JSON.stringify({contract:'browser-sync/v3',seen:[],offset:0,split_inbox:true,next_tab:'Other'});
    const fallback=await operate({page,provider:'browser_outlook',lastInboxRefresh:Date.now(),inboxReady:true},{...request,_singleTab:true,cursor:other});
    assert.deepEqual(fallback.messages.map(message=>message.message_id),['focused'],'a first pass scans the open tab when the other tab will not switch');
    assert.ok(fallback.warnings.some(w=>/^The Other inbox tab did not finish switching; the Focused tab was checked instead\.$/.test(w)&&clean({message:w})));
    await assert.rejects(operate({page,provider:'browser_outlook',lastInboxRefresh:Date.now(),inboxReady:true},{...request,_singleTab:true,_secondPass:true,cursor:other}),error=>{assert.match(error.message,/^The inbox tab did not finish switching\. It will be checked again on the next scan\.$/);return clean(error);});
    mode='hang';
    await assert.rejects(operate({page,provider:'browser_outlook'},{...request,_singleTab:true}),error=>{assert.match(error.message,/^The mailbox page did not finish loading after 2 s \(outlook\.cloud\.microsoft, navigating\)\. It will be checked again on the next scan\.$/);return clean(error);});
    mode='shell';
    const gmail=await browser.newPage();
    await gmail.route('**/*',route=>route.fulfill({contentType:'text/html',body:render('shell',true)}));
    await gmail.goto('https://mail.google.com/mail/u/0/#inbox');
    await assert.rejects(operate({page:gmail,provider:'browser_gmail'},request),{message:'This inbox layout is unsupported or still loading. Reconnect after opening the inbox.'});
  }finally{Object.assign(budgets.browser_outlook,saved);await browser.close();}
});

test('row budget leaves unvisited rows eligible and new arrivals precede reconciliation',{timeout:60000},async()=>{
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

test('second tab layout failure preserves first imports but identity changes remain fatal',{timeout:90000},async()=>{
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

test('a message pushed into the still-open conversation is never read as the clicked row, whether or not its older messages collapse; a pane that stays on it gets one fresh document and is not cached',{timeout:90000},async()=>{
  const saved={...budgets.browser_outlook};
  Object.assign(budgets.browser_outlook,{pane:4000,grace:500,settle:2000});
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();let navigations=0,stuck=false,collapse=false,labels={A:'A 1',B:'B 1'};
    await page.route('**/*',route=>{
      if(route.request().resourceType()==='document')navigations++;
      // office.com item paths with a reading pane that is not bound to its row, as on Outlook Web.
      return route.fulfill({contentType:'text/html',body:`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main"><div role="option" data-convid="A" aria-selected="false" onclick="openMail('A')">${labels.A}</div><div role="option" data-convid="B" aria-selected="false" onclick="openMail('B')">${labels.B}</div><section id="pane"></section></main><script>
window.stuck=${stuck};window.collapse=${collapse};
// A reply already pushed into conversation A is still there after a fresh document, and its row still previews it. Each pass of the loop keeps its own record.
const store='replies-${collapse}';let replies=Number(sessionStorage.getItem(store)||1);
if(replies>1)document.querySelector('[data-convid="A"]').textContent='A '+replies;
const article=(conv,n)=>'<article data-message-id="'+conv+'-'+n+'"><span email="sender'+n+'@example.test">Sender</span><div role="document">Body '+conv+' '+n+'</div></article>';
// A collapsed conversation renders only its newest message, so nothing shown before a push stays on screen.
function show(conv,count){history.replaceState(null,'','/mail/inbox/id/'+conv);const numbers=Array.from({length:count},(_,i)=>i+1);document.querySelector('#pane').innerHTML='<h2 data-testid="conversation-subject">Thread '+conv+'</h2>'+(window.collapse?numbers.slice(-1):numbers).map(n=>article(conv,n)).join('');}
function openMail(id){
  document.querySelectorAll('[role=option]').forEach(row=>row.setAttribute('aria-selected',String(row.getAttribute('data-convid')===id)));
  if(id==='A'){show('A',replies);return;}
  // While B loads, a reply is pushed into the open conversation A and A's row preview changes.
  setTimeout(()=>{if(document.querySelector('#pane h2')?.textContent==='Thread A'){replies=2;sessionStorage.setItem(store,'2');show('A',2);document.querySelector('[data-convid="A"]').textContent='A 2';}},150);
  if(!window.stuck)setTimeout(()=>show('B',1),2500);
}
</script>`});
    });
    const request={command:'sync',connection:{email:'owner@example.test'}};
    let value,second;
    for(collapse of [false,true]){
      await page.goto('https://outlook.office.com/mail/inbox');
      value={page,provider:'browser_outlook'};
      const before=navigations;
      const first=await operate(value,request);
      assert.deepEqual(first.messages.map(message=>[message.message_id,message.browser_reference.row_id]),[['A-1','A'],['B-1','B']],`the repaint of A while B loads is not read under row B (collapse ${collapse})`);
      assert.deepEqual(first.warnings.filter(warning=>/another check/.test(warning)),[]);
      assert.equal(navigations,before+1,`the repaint is disproved by its own item path, so row B needs no fresh document (collapse ${collapse})`);
      second=await operate(value,{...request,cursor:first.cursor});
      assert.deepEqual(second.messages.map(message=>[message.message_id,message.browser_reference.row_id]),[['A-2','A']],`the pushed reply is imported under its own row on the next scan (collapse ${collapse})`);
      assert.equal(navigations,before+1,`the pushed reply is read from the live page too (collapse ${collapse})`);
    }
    // B never paints while the push collapses A to its new reply: the only visible message must not be read under B.
    stuck=true;labels={A:'A 3',B:'B 2'};
    await page.evaluate(labels=>{window.stuck=true;for(const [id,text] of Object.entries(labels))document.querySelector(`[data-convid="${id}"]`).textContent=text;},labels);
    const before=navigations;
    const third=await operate(value,{...request,cursor:second.cursor});
    assert.deepEqual(third.messages,[]);
    assert.ok(third.warnings.includes('A message needs another check: The selected message did not finish loading.'));
    assert.equal(navigations,before+2,'reopening the open conversation and a pane that keeps showing the earlier conversation each get one fresh document');
    const key=id=>createHash('sha256').update(JSON.stringify(['Focused','data-convid',id])).digest('hex');
    const cache=JSON.parse(third.cursor).row_cache;
    assert.ok(key('A') in cache);
    assert.ok(!(key('B') in cache),'a row whose own message never showed is not cached as checked');
  }finally{Object.assign(budgets.browser_outlook,saved);await browser.close();}
});

test('a rendered list whose conversations never open is reloaded on the next scan, so an expired session surfaces as a sign-in failure',{timeout:60000},async()=>{
  const saved={...budgets.browser_outlook};
  Object.assign(budgets.browser_outlook,{pane:1000,signin:1000,settle:2000,grace:500});
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();let dead=false,documents=0;
    await page.route('**/*',route=>{
      const url=new URL(route.request().url());
      if(url.hostname==='login.microsoftonline.com')return route.fulfill({contentType:'text/html',body:'<form><input name="loginfmt"><p>Sign in</p></form>'});
      if(route.request().resourceType()==='document'){documents++;if(dead)return route.fulfill({status:302,headers:{location:'https://login.microsoftonline.com/common/oauth2/authorize'}});}
      return route.fulfill({contentType:'text/html',body:`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main"><div role="option" data-convid="row" aria-selected="false" onclick="this.setAttribute('aria-selected','true')">Row</div><section id="pane"></section></main>`});
    });
    await page.goto('https://outlook.office.com/mail/inbox');
    const value={page,provider:'browser_outlook',lastInboxRefresh:Date.now(),inboxReady:true},request={command:'sync',connection:{email:'owner@example.test'}};
    // The session behind the list expired: the account control stays in the DOM, but no conversation opens any more.
    await assert.rejects(operate(value,request),{message:'A message needs another check: The selected message did not finish loading.'});
    assert.equal(documents,1,'the first scan works on the retained page');
    assert.equal(value.inboxReady,false);
    dead=true;
    await assert.rejects(operate(value,request),{message:'Mailbox sign-in changed while checking mail. Reconnect the browser.'});
    assert.equal(documents,2,'the next scan loads a fresh document and the rendered identity, not the redirect, decides');
    assert.equal(new URL(page.url()).hostname,'login.microsoftonline.com');
  }finally{Object.assign(budgets.browser_outlook,saved);await browser.close();}
});

test('a Gmail list wait started near the scan reserve keeps its full length',{timeout:30000},async()=>{
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();
    // Opening a thread moves the hash off #inbox and the list stays hidden for a while after the worker returns to it.
    await page.route('**/*',route=>route.fulfill({contentType:'text/html',body:`<a aria-label="Google Account: Owner (owner@example.test)">Account</a><div role="main"><table id="list"><tr data-legacy-thread-id="thread" onclick="openMail()"><td>Mail</td></tr></table><section id="pane"></section></div><script>function openMail(){location.hash='#inbox/thread';document.querySelector('#pane').innerHTML='<h2 class="hP">Question</h2><article data-legacy-message-id="m1"><span class="gD" email="sender@example.test"></span><div class="a3s">Please reply</div></article>';const list=document.querySelector('#list');list.style.display='none';setTimeout(()=>{list.style.display='';},4000);}</script>`}));
    await page.goto('https://mail.google.com/mail/u/0/#inbox');
    const value={page,provider:'browser_gmail'},request={command:'sync',connection:{email:'owner@example.test'}};
    const result=await operate(value,{...request,_singleTab:true,_scanDeadline:Date.now()+19000});
    assert.deepEqual(result.messages.map(message=>message.message_id),['m1']);
    assert.deepEqual(result.warnings.filter(warning=>/Inbox refresh did not finish/.test(warning)),[],'a list that returns within the 10 s Gmail wait is awaited although the scan reserve is near');
    assert.equal(new URL(page.url()).hash,'#inbox');
  }finally{await browser.close();}
});

test('a scan that runs out of time after its boot keeps the rendered page; only a pane that never opens within its own wait forces a fresh document',{timeout:60000},async()=>{
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();let documents=0,lateRows=false,paneDelay=0;const labels={a:'1',b:'1'};
    await page.route('**/*',route=>{
      if(route.request().resourceType()==='document')documents++;
      return route.fulfill({contentType:'text/html',body:`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main"><section id="rows"></section><section id="pane"></section></main><script>
window.paneDelay=${paneDelay};window.version=1;
setTimeout(()=>{document.querySelector('#rows').innerHTML='<div role="option" data-convid="a" onclick="openMail(this)">A ${labels.a}</div><div role="option" data-convid="b" onclick="openMail(this)">B ${labels.b}</div>';},${lateRows?2500:0});
function openMail(row){const id=row.getAttribute('data-convid');setTimeout(()=>{document.querySelector('#pane').innerHTML='<h2 data-testid="conversation-subject">Question</h2><article data-convid="'+id+'" data-message-id="'+id+'-'+window.version+'"><span email="sender@example.test">Sender</span><div role="document">Body '+id+'</div></article>';},window.paneDelay);}
</script>`});
    });
    await page.goto('https://outlook.office.com/mail/inbox');
    const value={page,provider:'browser_outlook'},request={command:'sync',connection:{email:'owner@example.test'}};
    const warm=await operate(value,request);
    assert.deepEqual(warm.messages.map(message=>message.message_id),['a-1','b-1']);
    // The boot of a fresh document eats the working budget and the first pane paints just after the closing reserve.
    lateRows=true;paneDelay=4000;labels.a='2';value.lastInboxRefresh=0;
    await assert.rejects(operate(value,{...request,_singleTab:true,_scanDeadline:Date.now()+21000,cursor:warm.cursor}),{message:'The scan time limit was reached; remaining messages will be checked on a later scan.'});
    assert.ok(value.lastInboxRefresh>0,'a scan clipped by its deadline keeps the page it booted');
    assert.equal(value.inboxReady,true);
    const after=documents;
    // Row a is unchanged again and row b changed, so the next scan opens b on the retained page.
    await page.evaluate(()=>{window.paneDelay=0;window.version=2;document.querySelector('[data-convid="a"]').textContent='A 1';document.querySelector('[data-convid="b"]').textContent='B 2';});
    const next=await operate(value,{...request,cursor:warm.cursor});
    assert.deepEqual(next.messages.map(message=>message.message_id),['b-2']);
    assert.equal(documents,after,'the next scan reads the retained list without another document');
  }finally{await browser.close();}
});

test('a Gmail inbox refresh between rows never reloads on the stale budget',{timeout:30000},async()=>{
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();
    // A reading-pane layout keeps the hash on #inbox while the thread paints late.
    await page.route('**/*',route=>route.fulfill({contentType:'text/html',body:`<a aria-label="Google Account: Owner (owner@example.test)">Account</a><div role="main"><table><tr data-legacy-thread-id="thread" onclick="openMail()"><td>Mail</td></tr></table><section id="pane"></section></div><script>function openMail(){setTimeout(()=>{document.querySelector('#pane').innerHTML='<h2 class="hP">Question</h2><article data-legacy-message-id="m1"><span class="gD" email="sender@example.test"></span><div class="a3s">Please reply</div></article>';},4000);}</script>`}));
    await page.goto('https://mail.google.com/mail/u/0/#inbox');
    const stamp=Date.now()-27000;
    const value={page,provider:'browser_gmail',lastInboxRefresh:stamp,inboxReady:true},request={command:'sync',connection:{email:'owner@example.test'}};
    const result=await operate(value,request);
    assert.deepEqual(result.messages.map(message=>message.message_id),['m1']);
    assert.equal(value.lastInboxRefresh,stamp,'staleness is judged once at the start of a scan, as before');
  }finally{await browser.close();}
});

test('another mailbox\'s Inbox selected in the folder pane is not the inbox: the scan reloads the primary inbox instead of reading its rows',{timeout:30000},async()=>{
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();let navigations=0;
    await page.route('**/*',route=>{
      if(route.request().resourceType()==='document')navigations++;
      return route.fulfill({contentType:'text/html',body:`<div role="tree"><div role="treeitem" aria-level="1" id="primaryMailboxRoot_x" title="owner@example.test">owner@example.test</div><div role="treeitem" aria-level="2" title="Inbox" aria-selected="true" onclick="pick(this,'inbox')">Inbox</div><div role="treeitem" aria-level="1" title="Shared team">Shared team</div><div role="treeitem" aria-level="2" title="Inbox" aria-selected="false" onclick="pick(this,'AAMkAGZlZDU1MjQ4LTk3ZjEtNDFmMi04ZDc5LWFiY2RlZg%3D%3D')">Inbox</div></div><main role="main"><section id="rows"></section><section id="pane"></section></main><script>
window.mailbox='own';
function pick(node,folder){document.querySelectorAll('[role=treeitem]').forEach(item=>item.setAttribute('aria-selected',String(item===node)));window.mailbox=folder==='inbox'?'own':'shared';history.replaceState(null,'','/mail/'+folder+'/');render();}
function render(){document.querySelector('#rows').innerHTML='<div role="option" data-convid="'+window.mailbox+'" onclick="openMail(this)">'+window.mailbox+'</div>';}
function openMail(row){const id=row.getAttribute('data-convid');document.querySelector('#pane').innerHTML='<h2 data-testid="conversation-subject">Question</h2><article data-convid="'+id+'" data-message-id="'+id+'-1"><span email="sender@example.test">Sender</span><div role="document">Body '+id+'</div></article>';}
render();
</script>`});
    });
    await page.goto('https://outlook.office.com/mail/inbox');
    const value={page,provider:'browser_outlook'},request={command:'sync',connection:{email:'owner@example.test'}};
    const first=await operate(value,request);
    assert.deepEqual(first.messages.map(message=>message.message_id),['own-1']);
    assert.equal(value.inboxFolderKey,'Inbox');
    // The shared mailbox's Inbox is selected in the mail window and its folder id replaces the inbox path.
    await page.evaluate(()=>document.querySelectorAll('[role=treeitem]')[3].click());
    assert.equal(await page.evaluate(selectedFolderKey),'Shared team|Inbox');
    const before=navigations;
    const second=await operate(value,{...request,cursor:first.cursor});
    assert.equal(navigations,before+1,'a folder path whose selected Inbox belongs to another mailbox is left by reloading the primary inbox');
    assert.equal(new URL(page.url()).pathname,'/mail/inbox');
    assert.deepEqual(second.messages,[],'no row of the other mailbox is read');
  }finally{await browser.close();}
});

test('a repaint of the still-open conversation is never read as the clicked row when the item path moves at click time: ids, text only, two repaints, no item path, and outlook.cloud.microsoft',{timeout:180000},async()=>{
  const saved={...budgets.browser_outlook};
  Object.assign(budgets.browser_outlook,{pane:4000,grace:500,settle:2000});
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();let navigations=0,shape;
    const labels={A:'A 1',B:'B 1'};
    await page.route('**/*',route=>{
      if(route.request().resourceType()==='document')navigations++;
      const host=new URL(route.request().url()).hostname;
      return route.fulfill({contentType:'text/html',body:`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main"><div role="option" data-convid="A" aria-selected="false" onclick="openMail('A')">${labels.A}</div><div role="option" data-convid="B" aria-selected="false" onclick="openMail('B')">${labels.B}</div><section id="pane"></section></main><script>
const shape=${JSON.stringify(shape)};
// A reply already pushed into conversation A is still there after a fresh document, and its row still previews it. Each shape keeps its own record.
const store='replies-'+shape.name;let replies=Number(sessionStorage.getItem(store)||1);
if(replies>1)document.querySelector('[data-convid="A"]').textContent='A '+replies;
if(${JSON.stringify(host)}==='outlook.cloud.microsoft')history.replaceState(null,'','/mail/');
const article=(conv,n)=>'<article'+(shape.ids?' data-message-id="'+conv+'-'+n+'"':'')+'><span email="sender'+n+'@example.test">Sender</span><div role="document">Body '+conv+' '+n+'</div></article>';
// The router moves the /id/ segment when a row is clicked, before any pane paints; a collapsed conversation shows only its newest message.
function setUrl(conv){if(shape.noItem)return;const base=location.pathname.split('/id/')[0];history.replaceState(null,'',(base.endsWith('/')?base.slice(0,-1):base)+'/id/'+conv);}
function show(conv,count){document.querySelector('#pane').innerHTML='<h2 data-testid="conversation-subject">Thread '+conv+'</h2>'+article(conv,count);}
// Outlook renders the pushed reply into the list row and into the reading pane from one update.
function push(count){
  if(document.querySelector('#pane h2')?.textContent!=='Thread A')return;
  replies=count;sessionStorage.setItem(store,String(count));
  document.querySelector('[data-convid="A"]').textContent='A '+count;show('A',count);
}
function openMail(id){
  document.querySelectorAll('[role=option]').forEach(row=>row.setAttribute('aria-selected',String(row.getAttribute('data-convid')===id)));
  setUrl(id);
  if(id==='A'){show('A',replies);return;}
  setTimeout(()=>push(2),150);
  if(shape.repaints>1)setTimeout(()=>push(3),1000);
  setTimeout(()=>show('B',1),2500);
}
</script>`});
    });
    const request={command:'sync',connection:{email:'owner@example.test'}};
    const key=id=>createHash('sha256').update(JSON.stringify(['Focused','data-convid',id])).digest('hex');
    const shapes=[{name:'ids',ids:true,repaints:1},{name:'text only',ids:false,repaints:1},{name:'two repaints',ids:true,repaints:2},{name:'no item path',ids:true,repaints:1,noItem:true},{name:'cloud ids',ids:true,repaints:1,host:'outlook.cloud.microsoft'},{name:'cloud text only',ids:false,repaints:2,host:'outlook.cloud.microsoft'}];
    for(shape of shapes){
      labels.A='A 1';labels.B='B 1';
      await page.goto(`https://${shape.host||'outlook.office.com'}/mail/inbox`);
      const value={page,provider:'browser_outlook'};
      const before=navigations;
      const first=await operate(value,request);
      assert.deepEqual(first.messages.map(message=>[message.body,message.browser_reference.row_id]),[['Body A 1','A'],['Body B 1','B']],`the repaint of A while B loads is not read under row B (${shape.name})`);
      assert.deepEqual(first.warnings.filter(warning=>/another check/.test(warning)),[],shape.name);
      assert.equal(navigations,before+1,`the repaint is waited out on the live page, never discarded by a fresh document (${shape.name})`);
      const cache=JSON.parse(first.cursor).row_cache;
      assert.ok(key('A') in cache&&key('B') in cache,`both rows are cached once their own messages were read (${shape.name})`);
      const second=await operate(value,{...request,cursor:first.cursor});
      assert.deepEqual(second.messages.map(message=>[message.body,message.browser_reference.row_id]),[[`Body A ${1+shape.repaints}`,'A']],`the pushed reply is imported under its own row on the next scan (${shape.name})`);
      assert.equal(navigations,before+1,`the pushed reply is read from the live page on the next scan too (${shape.name})`);
      if(shape.host)assert.match(new URL(page.url()).pathname,/^\/mail\/(?:id\/A)?$/);
    }
  }finally{Object.assign(budgets.browser_outlook,saved);await browser.close();}
});

test('a conversation that paints after its own wait is discarded by a fresh document before the next row opens, or left with the remaining rows for the next scan when the boot would not fit',{timeout:120000},async()=>{
  const saved={...budgets.browser_outlook};
  Object.assign(budgets.browser_outlook,{pane:4000,grace:500,settle:2000});
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();let documents=0,rows=['B','C'];
    await page.route('**/*',route=>{
      if(route.request().resourceType()==='document')documents++;
      return route.fulfill({contentType:'text/html',body:`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main">${rows.map(id=>`<div role="option" data-convid="${id}" aria-selected="false" onclick="openMail('${id}')">${id} 1</div>`).join('')}<section id="pane"></section></main><script>
window.opened=[];
const article=(conv,n)=>'<article data-message-id="'+conv+'-'+n+'"><span email="sender'+n+'@example.test">Sender</span><div role="document">Body '+conv+' '+n+'</div></article>';
function show(conv){document.querySelector('#pane').innerHTML='<h2 data-testid="conversation-subject">Thread '+conv+'</h2>'+article(conv,1);}
// B paints only after the worker's pane wait has expired, while the next row is already open.
function openMail(id){
  window.opened.push(id);
  document.querySelectorAll('[role=option]').forEach(row=>row.setAttribute('aria-selected',String(row.getAttribute('data-convid')===id)));
  history.replaceState(null,'','/mail/inbox/id/'+id);
  document.querySelector('#pane').innerHTML='';
  setTimeout(()=>show(id),{A:200,B:5500,C:3000}[id]);
}
</script>`});
    });
    const request={command:'sync',connection:{email:'owner@example.test'}};
    const key=id=>createHash('sha256').update(JSON.stringify(['Focused','data-convid',id])).digest('hex');
    const stale='A message needs another check: The selected message did not finish loading.';
    await page.goto('https://outlook.office.com/mail/inbox');
    const value={page,provider:'browser_outlook'};
    const first=await operate(value,request);
    assert.deepEqual(first.messages.map(message=>[message.message_id,message.browser_reference.row_id]),[['C-1','C']],'the late paint of B is never read under row C');
    assert.ok(first.warnings.includes(stale));
    assert.equal(documents,3,'the fresh document that discards the late paint is the only extra navigation');
    let cache=JSON.parse(first.cursor).row_cache;
    assert.ok(!(key('B') in cache),'a row whose own message never showed is not cached');
    assert.ok(key('C') in cache);
    await assert.rejects(operate(value,{...request,cursor:first.cursor}),{message:stale},'the next scan retries B on the retained page and fails it cleanly');
    assert.equal(documents,3);
    // A shorter scan cannot absorb a boot after B fails: the remaining row waits for the next scan instead of reading B's late paint.
    rows=['A','B','C'];
    await page.goto('https://outlook.office.com/mail/inbox');
    const clippedValue={page,provider:'browser_outlook'};
    const before=documents;
    const clipped=await operate(clippedValue,{...request,_singleTab:true,_scanDeadline:Date.now()+40000});
    assert.deepEqual(clipped.messages.map(message=>[message.message_id,message.browser_reference.row_id]),[['A-1','A']]);
    assert.ok(clipped.warnings.includes('A conversation did not finish loading, so the remaining conversations will be checked on the next scan.'));
    assert.ok(clipped.warnings.every(warning=>!/[\u001b]|locator\.|data-convid|Call log/.test(warning)));
    assert.deepEqual(await page.evaluate(()=>window.opened),['A','B'],'row C is not opened on a document that may still paint B');
    cache=JSON.parse(clipped.cursor).row_cache;
    assert.ok(key('A') in cache&&!(key('B') in cache)&&!(key('C') in cache));
    assert.equal(documents,before+1,'the clipped scan keeps its page');
    const later=await operate(clippedValue,{...request,cursor:clipped.cursor});
    assert.deepEqual(later.messages.map(message=>[message.message_id,message.browser_reference.row_id]),[['C-1','C']],'the next scan discards the late paint with a fresh document before opening the remaining rows');
    assert.deepEqual((await page.evaluate(()=>window.opened)).slice(-2),['C','B'],'the row that never failed is read before the one that failed last scan');
    assert.equal(documents,before+2,'one fresh document discards the late paint, and the failed row goes last so no second recovery is needed');
  }finally{Object.assign(budgets.browser_outlook,saved);await browser.close();}
});

test('an expanded conversation whose child item is the selected option does not veto its own pane: every row is read from its own pane and the scan loads one document',{timeout:60000},async()=>{
  const saved={...budgets.browser_outlook};
  Object.assign(budgets.browser_outlook,{pane:2000,grace:500,settle:2000});
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();const opened=[];let documents=0;
    await page.route('**/*',route=>{
      const url=new URL(route.request().url());
      if(url.pathname.startsWith('/opened/')){opened.push(url.pathname.slice(8));return route.fulfill({status:204});}
      if(route.request().resourceType()==='document')documents++;
      // Outlook expands the clicked conversation in the list and marks the opened item, not the header, as the selected option.
      return route.fulfill({contentType:'text/html',body:`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main"><div role="listbox" id="list"><div role="option" data-convid="A" aria-selected="false" onclick="openMail('A')">A 2</div><div role="option" data-convid="B" aria-selected="false" onclick="openMail('B')">B 2</div></div><section id="pane"></section></main><script>
function openMail(id){
  fetch('/opened/'+id).catch(()=>{});
  const conversation=id.split('-')[0],message=id.includes('-')?id:conversation+'-2';
  document.querySelectorAll('#list [data-itemid]').forEach(node=>node.remove());
  const header=document.querySelector('[data-convid="'+conversation+'"]');
  for(const number of [2,1]){
    const item=document.createElement('div');
    item.setAttribute('role','option');item.setAttribute('data-itemid',conversation+'-'+number);
    item.setAttribute('aria-selected',String(conversation+'-'+number===message));
    item.textContent=conversation+' item '+number;item.onclick=()=>openMail(conversation+'-'+number);
    header.insertAdjacentElement('afterend',item);
  }
  document.querySelector('#pane').innerHTML='<h2 data-testid="conversation-subject">Thread '+conversation+'</h2><article data-message-id="'+message+'"><span email="sender@example.test">Sender</span><div role="document">Body '+message+'</div></article>';
}
</script>`});
    });
    await page.goto('https://outlook.office.com/mail/inbox');
    const value={page,provider:'browser_outlook'},request={command:'sync',connection:{email:'owner@example.test'}};
    const before=documents;
    const result=await operate(value,request);
    assert.deepEqual(result.messages.map(message=>[message.message_id,message.browser_reference.row_id]),[['A-2','A'],['A-1','A-1'],['B-2','B'],['B-1','B-1']],'every row is read from its own pane although only a child item is selected');
    assert.deepEqual(result.warnings.filter(warning=>!/Opening mail may mark it read/.test(warning)),[],'no row needed another check');
    assert.equal(documents,before+1,'neither the selected child item nor a pane that names no conversation costs a fresh document');
    assert.deepEqual(opened,['A','A-1','A-2','B','B-1','B-2'],'each row is opened exactly once');
    const key=(id,attr='data-convid')=>createHash('sha256').update(JSON.stringify(['Focused',attr,id])).digest('hex');
    const cache=JSON.parse(result.cursor).row_cache;
    assert.ok(key('A') in cache&&key('B') in cache&&key('A-2','data-itemid') in cache,'an item whose message was already imported under its conversation is still cached from its own pane');
  }finally{Object.assign(budgets.browser_outlook,saved);await browser.close();}
});

test('the inbox listed under Favorites and under the mailbox is remembered by its mailbox entry, so scans marking either or both entries selected reuse the live page',{timeout:60000},async()=>{
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();let navigations=0;
    await page.route('**/*',route=>{
      if(route.request().resourceType()==='document')navigations++;
      return route.fulfill({contentType:'text/html',body:`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><div role="tree"><div role="treeitem" aria-level="1" title="Favorites">Favorites</div><div role="treeitem" aria-level="2" title="Inbox" aria-selected="true">Inbox 2</div><div role="treeitem" aria-level="1" id="primaryMailboxRoot_x" title="owner@example.test">owner@example.test</div><div role="treeitem" aria-level="2" title="Inbox" aria-selected="true">Inbox 2</div><div role="treeitem" aria-level="2" title="Sent Items">Sent Items</div></div><main role="main"><div role="option" data-convid="row" onclick="openMail(this)">Row 1</div><section id="pane"></section></main><script>
history.replaceState(null,'','/mail/');
function openMail(row){const id=row.getAttribute('data-convid');history.replaceState(null,'','/mail/id/'+id);document.querySelector('#pane').innerHTML='<h2 data-testid="conversation-subject">Question</h2><article data-message-id="'+id+'-1"><span email="sender@example.test">Sender</span><div role="document">Body</div></article>';}
</script>`});
    });
    await page.goto('https://outlook.cloud.microsoft/mail/inbox');
    const value={page,provider:'browser_outlook'},request={command:'sync',connection:{email:'owner@example.test'}};
    const first=await operate(value,request);
    assert.deepEqual(first.messages.map(message=>message.message_id),['row-1']);
    assert.equal(navigations,2);
    assert.equal(value.inboxFolderKey,'Inbox','the primary mailbox entry is remembered although Favorites lists the inbox first');
    const select=indexes=>page.evaluate(indexes=>document.querySelectorAll('[role=treeitem]').forEach((node,index)=>node.setAttribute('aria-selected',String(indexes.includes(index)))),indexes);
    await select([3]);
    await operate(value,{...request,cursor:first.cursor});
    assert.equal(navigations,2,'only the mailbox entry selected');
    await select([1,3]);
    await operate(value,{...request,cursor:first.cursor});
    assert.equal(navigations,2,'both entries selected on an opened item path');
    await select([4]);
    await operate(value,{...request,cursor:first.cursor});
    assert.equal(navigations,3,'another folder selected is still left by reloading the inbox');
  }finally{await browser.close();}
});

test('a tenant that addresses the inbox by an opaque folder id and renders no folder tree is checked on the live page: the landing path of the worker\'s own navigation is the inbox',{timeout:60000},async()=>{
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();let navigations=0;
    const folder='AAMkAGZlZDU1MjQ4LTk3ZjEtNDFmMi04ZDc5LWFiY2RlZg%3D%3D';
    await page.route('**/*',route=>{
      if(route.request().resourceType()==='document')navigations++;
      return route.fulfill({contentType:'text/html',body:`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main"><div role="option" data-convid="row" onclick="openMail(this)">Row 1</div><section id="pane"></section></main><script>
const folder=${JSON.stringify(folder)};
if(location.pathname==='/mail/inbox')history.replaceState(null,'','/mail/'+folder+'/');
function openMail(row){const id=row.getAttribute('data-convid');history.replaceState(null,'','/mail/'+folder+'/id/'+id);document.querySelector('#pane').innerHTML='<h2 data-testid="conversation-subject">Question</h2><article data-message-id="'+id+'-1"><span email="sender@example.test">Sender</span><div role="document">Body</div></article>';}
</script>`});
    });
    await page.goto('https://outlook.cloud.microsoft/mail/inbox');
    const value={page,provider:'browser_outlook'},request={command:'sync',connection:{email:'owner@example.test'}};
    const first=await operate(value,request);
    assert.deepEqual(first.messages.map(message=>message.message_id),['row-1']);
    assert.equal(navigations,2,'the worker\'s first navigation lands on the opaque folder and the opened row reuses the page');
    assert.equal(value.inboxFolderPath,folder);
    assert.equal(value.inboxFolderKey,undefined);
    assert.equal(new URL(page.url()).pathname,`/mail/${folder}/id/row`);
    await operate(value,{...request,cursor:first.cursor});
    assert.equal(navigations,2,'the next scan reuses the live page');
    await page.evaluate(()=>history.replaceState(null,'','/mail/AQMkOTHERZDU1MjQ4LTk3ZjEtNDFmMi04ZDc5%3D%3D/'));
    await operate(value,{...request,cursor:first.cursor});
    assert.equal(navigations,3,'another opaque folder is left by reloading the inbox');
    assert.equal(new URL(page.url()).pathname,`/mail/${folder}/`);
  }finally{await browser.close();}
});

test('a repaint of the conversation the click scrolled out of a virtualized list is never read as the clicked row',{timeout:90000},async()=>{
  const saved={...budgets.browser_outlook};
  Object.assign(budgets.browser_outlook,{pane:4000,grace:500,settle:2000});
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();let navigations=0;
    // Outlook mounts only the rows around the scroll position, so opening a row further down unmounts the conversation that is still open.
    await page.route('**/*',route=>{
      if(route.request().resourceType()==='document')navigations++;
      return route.fulfill({contentType:'text/html',body:`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main"><div id="list" style="height:400px;overflow-y:auto"><div id="top"></div><div id="mount"></div><div id="bottom"></div></div><section id="pane"></section></main><script>
const HEIGHT=80,VIEW=400,BUFFER=5,items=[{row:'A'},...Array.from({length:9},(_,index)=>({filler:index})),{row:'B'},...Array.from({length:4},(_,index)=>({filler:10+index}))];
const nodes=new Map(),mount=document.querySelector('#mount'),list=document.querySelector('#list');
let replies=Number(sessionStorage.getItem('replies')||1),selected='';
// Both the pushed reply and the record of where it landed outlive a fresh document, as they would on the server.
window.pushedWhileMounted=JSON.parse(sessionStorage.getItem('pushedWhileMounted')||'null');
function build(index){
  const item=items[index],node=document.createElement('div');
  node.style.height=HEIGHT+'px';
  if(!item.row){node.textContent='filler '+item.filler;return node;}
  node.setAttribute('role','option');node.setAttribute('data-convid',item.row);node.setAttribute('aria-selected',String(selected===item.row));
  node.textContent=item.row+' '+(item.row==='A'?replies:1);node.onclick=()=>openMail(item.row);
  return node;
}
function render(){
  const first=Math.max(0,Math.floor(list.scrollTop/HEIGHT)-BUFFER),last=Math.min(items.length-1,Math.ceil((list.scrollTop+VIEW)/HEIGHT)+BUFFER);
  for(const [index,node] of [...nodes])if(index<first||index>last){node.remove();nodes.delete(index);}
  for(let index=first;index<=last;index++){
    if(nodes.has(index))continue;
    const node=build(index);nodes.set(index,node);
    const after=[...nodes.keys()].filter(key=>key>index).sort((a,b)=>a-b)[0];
    mount.insertBefore(node,after===undefined?null:nodes.get(after));
  }
  document.querySelector('#top').style.height=(first*HEIGHT)+'px';
  document.querySelector('#bottom').style.height=((items.length-1-last)*HEIGHT)+'px';
}
list.addEventListener('scroll',render);
const rowNode=id=>[...nodes.values()].find(node=>node.getAttribute('data-convid')===id);
function show(conversation,count){document.querySelector('#pane').innerHTML='<h2 data-testid="conversation-subject">Thread '+conversation+'</h2><article data-message-id="'+conversation+'-'+count+'"><span email="sender'+count+'@example.test">Sender</span><div role="document">Body '+conversation+' '+count+'</div></article>';}
function openMail(id){
  selected=id;
  for(const node of nodes.values())if(node.hasAttribute('role'))node.setAttribute('aria-selected',String(node.getAttribute('data-convid')===id));
  const base=location.pathname.split('/id/')[0];history.replaceState(null,'',(base.endsWith('/')?base.slice(0,-1):base)+'/id/'+id);
  if(id==='A'){show('A',replies);return;}
  // A reply lands in the still-open conversation A while B loads; its row cannot show that, because the click scrolled the row away.
  setTimeout(()=>{
    if(document.querySelector('#pane h2')?.textContent!=='Thread A')return;
    replies=2;sessionStorage.setItem('replies','2');show('A',2);
    const node=rowNode('A');window.pushedWhileMounted=Boolean(node);sessionStorage.setItem('pushedWhileMounted',JSON.stringify(window.pushedWhileMounted));
    if(node)node.textContent='A 2';
  },150);
  setTimeout(()=>show('B',1),2500);
}
render();
</script>`});
    });
    const request={command:'sync',connection:{email:'owner@example.test'}};
    const key=id=>createHash('sha256').update(JSON.stringify(['Focused','data-convid',id])).digest('hex');
    for(const host of ['outlook.office.com','outlook.cloud.microsoft']){
      await page.goto(`https://${host}/mail/inbox`);
      const value={page,provider:'browser_outlook'},before=navigations;
      const result=await operate(value,request);
      assert.equal(await page.evaluate(()=>window.pushedWhileMounted),false,`opening row B scrolled row A out of the mounted list, so its preview could not change (${host})`);
      assert.deepEqual(result.messages.map(message=>[message.message_id,message.browser_reference.row_id]),[['A-1','A'],['B-1','B']],`the repaint of the unmounted conversation is not read under row B (${host})`);
      assert.deepEqual(result.warnings.filter(warning=>/another check/.test(warning)),[],host);
      assert.equal(navigations,before+1,`the unmounted conversation is disproved without a fresh document (${host})`);
      const cache=JSON.parse(result.cursor).row_cache;
      assert.ok(key('A') in cache&&key('B') in cache,`both rows are cached from their own messages (${host})`);
    }
  }finally{Object.assign(budgets.browser_outlook,saved);await browser.close();}
});

test('a conversation receiving mail while another row is opening neither delays that row nor reloads the inbox, on Outlook and on Gmail',{timeout:90000},async()=>{
  const saved={...budgets.browser_outlook};
  Object.assign(budgets.browser_outlook,{pane:4000,grace:500,settle:2000});
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();let navigations=0,churn='none';
    // The clicked conversation paints at the very moment an unrelated conversation receives a new message.
    await page.route('**/*',route=>{
      if(route.request().resourceType()==='document')navigations++;
      const gmail=new URL(route.request().url()).hostname==='mail.google.com';
      const rows=['A','B','C'];
      const outlook=`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main">${rows.map(id=>`<div role="option" data-convid="${id}" aria-selected="false" onclick="openMail('${id}')">${id} 1</div>`).join('')}<section id="pane"></section></main>`;
      const gmailList=`<a aria-label="Google Account: Owner (owner@example.test)">Account</a><div role="main"><table>${rows.map(id=>`<tr data-legacy-thread-id="${id}" onclick="openMail('${id}')"><td>${id} 1</td></tr>`).join('')}</table><section id="pane"></section></div>`;
      return route.fulfill({contentType:'text/html',body:`${gmail?gmailList:outlook}<script>
const gmail=${gmail},churn=${JSON.stringify(churn)};
const pane=id=>gmail
  ? '<h2 class="hP">Thread '+id+'</h2><article data-legacy-message-id="'+id+'-1"><span class="gD" email="sender@example.test"></span><div class="a3s">Body '+id+' 1</div></article>'
  : '<h2 data-testid="conversation-subject">Thread '+id+'</h2><article data-message-id="'+id+'-1"><span email="sender@example.test">Sender</span><div role="document">Body '+id+' 1</div></article>';
const preview=id=>gmail?document.querySelector('[data-legacy-thread-id="'+id+'"] td'):document.querySelector('[data-convid="'+id+'"]');
let opens=0;
function openMail(id){
  if(!gmail){
    document.querySelectorAll('[role=option]').forEach(row=>row.setAttribute('aria-selected',String(row.getAttribute('data-convid')===id)));
    const base=location.pathname.split('/id/')[0];history.replaceState(null,'',(base.endsWith('/')?base.slice(0,-1):base)+'/id/'+id);
  }
  document.querySelector('#pane').innerHTML='';
  const noisy=id==='B'&&(churn==='always'||(churn==='once'&&++opens===1));
  setTimeout(()=>{document.querySelector('#pane').innerHTML=pane(id);if(noisy)preview('C').textContent='C 2';},400);
}
</script>`});
    });
    const request={command:'sync',connection:{email:'owner@example.test'}};
    const key=id=>createHash('sha256').update(JSON.stringify(['Focused','data-convid',id])).digest('hex');
    for(churn of ['none','once','always']){
      await page.goto('https://outlook.cloud.microsoft/mail/inbox');
      const value={page,provider:'browser_outlook'},before=navigations;
      const result=await operate(value,request);
      assert.deepEqual(result.messages.map(message=>[message.message_id,message.browser_reference.row_id]),[['A-1','A'],['B-1','B'],['C-1','C']],`every conversation is imported under its own row (Outlook, churn ${churn})`);
      assert.deepEqual(result.warnings.filter(warning=>!/Opening mail may mark it read/.test(warning)),[],`churn ${churn}`);
      assert.equal(navigations,before+1,`one worker navigation; new mail elsewhere does not reload the inbox (churn ${churn})`);
      const cache=JSON.parse(result.cursor).row_cache;
      assert.ok(['A','B','C'].every(id=>key(id) in cache),`every row is cached from its own message (churn ${churn})`);
    }
    for(churn of ['none','once','always']){
      await page.goto('https://mail.google.com/mail/u/0/#inbox');
      const value={page,provider:'browser_gmail',lastInboxRefresh:Date.now(),inboxReady:true},before=navigations;
      const result=await operate(value,request);
      assert.deepEqual(result.messages.map(message=>[message.message_id,message.browser_reference.row_id]),[['A-1','A'],['B-1','B'],['C-1','C']],`every thread is imported under its own row (Gmail, churn ${churn})`);
      assert.deepEqual(result.warnings.filter(warning=>!/Opening mail may mark it read/.test(warning)),[],`Gmail churn ${churn}`);
      assert.equal(navigations,before,`Gmail reads its threads without a reload (churn ${churn})`);
    }
  }finally{Object.assign(budgets.browser_outlook,saved);await browser.close();}
});

test('a repaint whose own row moves only while the message is read is never read under the next row, which is opened again from a fresh document in the same scan',{timeout:90000},async()=>{
  const saved={...budgets.browser_outlook};
  Object.assign(budgets.browser_outlook,{pane:4000,grace:500,settle:2000});
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();let navigations=0;
    // The reading pane repaints the open conversation first, its list row follows much later, and the sender of the repainted message hydrates later still.
    await page.route('**/*',route=>{
      if(route.request().resourceType()==='document')navigations++;
      return route.fulfill({contentType:'text/html',body:`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main"><div role="option" data-convid="A" aria-selected="false" onclick="openMail('A')">A 1</div><div role="option" data-convid="B" aria-selected="false" onclick="openMail('B')">B 1</div><section id="pane"></section></main><script>
const heading=conversation=>'<h2 data-testid="conversation-subject">Thread '+conversation+'</h2>';
function openMail(id){
  document.querySelectorAll('[role=option]').forEach(row=>row.setAttribute('aria-selected',String(row.getAttribute('data-convid')===id)));
  history.replaceState(null,'','/mail/inbox/id/'+id);
  if(id==='A'){document.querySelector('#pane').innerHTML=heading('A')+'<article data-message-id="A-1"><span email="sender1@example.test">Sender</span><div role="document">Body A 1</div></article>';return;}
  if(document.querySelector('#pane h2')?.textContent==='Thread A'&&!sessionStorage.getItem('repainted')){
    sessionStorage.setItem('repainted','1');
    setTimeout(()=>{document.querySelector('#pane').innerHTML=heading('A')+'<article data-message-id="A-2"><div role="document">Body A 2</div></article>';},200);
    setTimeout(()=>{document.querySelector('[data-convid="A"]').textContent='A 2';},1600);
    setTimeout(()=>{document.querySelector('#pane article').insertAdjacentHTML('afterbegin','<span email="sender2@example.test">Sender</span>');},2200);
    return;
  }
  setTimeout(()=>{document.querySelector('#pane').innerHTML=heading('B')+'<article data-message-id="B-1"><span email="sender1@example.test">Sender</span><div role="document">Body B 1</div></article>';},2500);
}
</script>`});
    });
    await page.goto('https://outlook.office.com/mail/inbox');
    const value={page,provider:'browser_outlook'},request={command:'sync',connection:{email:'owner@example.test'}};
    const key=id=>createHash('sha256').update(JSON.stringify(['Focused','data-convid',id])).digest('hex');
    const before=navigations;
    const first=await operate(value,request);
    assert.deepEqual(first.messages.map(message=>[message.message_id,message.browser_reference.row_id]),[['A-1','A'],['B-1','B']],'the repaint of the still-open conversation is never imported under row B');
    assert.deepEqual(first.warnings.filter(warning=>/another check/.test(warning)),[],'the row the repaint blocked is read on the fresh document instead of failing');
    assert.equal(navigations,before+2,'one fresh document discards the repaint, and row B is opened again on it');
    const cache=JSON.parse(first.cursor).row_cache;
    assert.ok(key('A') in cache&&key('B') in cache,'both rows are cached from their own messages');
    const second=await operate(value,{...request,cursor:first.cursor});
    assert.deepEqual(second.messages,[],'the fresh document left no unread conversation behind');
    assert.equal(navigations,before+2,'the next scan needs no document of its own');
  }finally{Object.assign(budgets.browser_outlook,saved);await browser.close();}
});
test('three conversations sharing one subject are each read from the pane that names it, in one navigation, and the read marker they gain leaves them cached',{timeout:90000},async()=>{
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();let navigations=0;
    // Outlook names the open conversation in its heading's element id, keeps the previous one on screen until the next paints,
    // and drops the "Unread, " prefix from the clicked row's aria-label a moment later, inside the next row's pane wait.
    await page.route('**/*',route=>{
      if(route.request().resourceType()==='document')navigations++;
      return route.fulfill({contentType:'text/html',body:`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main">${['A','B','C'].map(id=>`<div role="option" data-convid="${id}" aria-selected="false" aria-label="Unread, Weekly report, Sender ${id}" onclick="openMail('${id}')"><span>Weekly report</span></div>`).join('')}<section id="pane"></section></main><script>
function openMail(id){
  document.querySelectorAll('[role=option]').forEach(row=>row.setAttribute('aria-selected',String(row.getAttribute('data-convid')===id)));
  const base=location.pathname.split('/id/')[0];history.replaceState(null,'',(base.endsWith('/')?base.slice(0,-1):base)+'/id/'+id);
  setTimeout(()=>{document.querySelector('#pane').innerHTML='<h2 id="CONV_'+id+'_SUBJECT">Weekly report</h2><div aria-label="Email message" data-message-id="'+id+'-1"><span id="MSG_'+id+'-1_FROM" email="sender'+id+'@example.test">Sender</span><div id="MSG_'+id+'-1_SUBJECT">Weekly report</div><div role="document">Body '+id+' 1</div></div>';},400);
  setTimeout(()=>{const row=document.querySelector('[data-convid="'+id+'"]');row.setAttribute('aria-label',row.getAttribute('aria-label').replace('Unread, ',''));},600);
}
</script>`});
    });
    const request={command:'sync',connection:{email:'owner@example.test'}};
    const key=id=>createHash('sha256').update(JSON.stringify(['Focused','data-convid',id])).digest('hex');
    for(const host of ['outlook.office.com','outlook.cloud.microsoft']){
      await page.goto(`https://${host}/mail/inbox`);
      const value={page,provider:'browser_outlook'},before=navigations;
      const first=await operate(value,request);
      assert.deepEqual(first.messages.map(message=>[message.message_id,message.browser_reference.row_id]),[['A-1','A'],['B-1','B'],['C-1','C']],`a shared subject never moves a message to another row (${host})`);
      assert.deepEqual(first.warnings.filter(warning=>/another check/.test(warning)),[],host);
      assert.equal(navigations,before+1,`one navigation for the scan although every conversation has the same subject (${host})`);
      let cache=JSON.parse(first.cursor).row_cache;
      assert.ok(['A','B','C'].every(id=>key(id) in cache),`every row is cached from its own message (${host})`);
      const second=await operate(value,{...request,cursor:first.cursor});
      assert.deepEqual(second.messages,[],`the read marker each row gained is no new message (${host})`);
      assert.equal(navigations,before+1,`the next scan needs no navigation (${host})`);
      const after=JSON.parse(second.cursor).row_cache;
      assert.ok(['A','B','C'].every(id=>key(id) in after),`losing the unread prefix does not make a row due again (${host})`);
      assert.deepEqual(['A','B','C'].map(id=>after[key(id)].signature),['A','B','C'].map(id=>cache[key(id)].signature),`the read marker a row gains does not change the signature the cache compares (${host})`);
    }
  }finally{await browser.close();}
});

test('a reading pane with no conversation heading is judged by the message ids it names: a repaint of another item is never read under the clicked one',{timeout:120000},async()=>{
  const saved={...budgets.browser_outlook};
  Object.assign(budgets.browser_outlook,{pane:6000,grace:500,settle:2000});
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();let navigations=0;
    // Conversation view off: each row is one item and the pane renders a single message with no conversation-level heading,
    // so its only identity is the item id and the MSG_<id>_FROM envelope. While B-1 loads, the pane repaints A's newer item.
    await page.route('**/*',route=>{
      if(route.request().resourceType()==='document')navigations++;
      const host=new URL(route.request().url()).hostname;
      return route.fulfill({contentType:'text/html',body:`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main"><div role="option" data-itemid="A-1" aria-selected="false" onclick="openMail('A-1')">Invoice from A</div><div role="option" data-itemid="B-1" aria-selected="false" onclick="openMail('B-1')">Invoice from B</div><section id="pane"></section></main><script>
if(${JSON.stringify(host)}==='outlook.cloud.microsoft'&&location.pathname==='/mail/inbox')history.replaceState(null,'','/mail/');
const message=item=>'<div aria-label="Email message" data-item-id="'+item+'"><span id="MSG_'+item+'_FROM" email="sender@example.test">Sender</span><div id="MSG_'+item+'_SUBJECT">Invoice</div><div role="document">Body '+item+'</div></div>';
const shown=()=>document.querySelector('#pane [data-item-id]')?.getAttribute('data-item-id')||'';
function openMail(item){
  document.querySelectorAll('[role=option]').forEach(row=>row.setAttribute('aria-selected',String(row.getAttribute('data-itemid')===item)));
  const base=location.pathname.split('/id/')[0];history.replaceState(null,'',(base.endsWith('/')?base.slice(0,-1):base)+'/id/'+item);
  if(item==='A-1'){document.querySelector('#pane').innerHTML=message('A-1');return;}
  setTimeout(()=>{if(shown()==='A-1')document.querySelector('#pane').innerHTML=message('A-2');},200);
  setTimeout(()=>{document.querySelector('#pane').innerHTML=message(item);},2500);
}
</script>`});
    });
    const request={command:'sync',connection:{email:'owner@example.test'}};
    const key=id=>createHash('sha256').update(JSON.stringify(['Focused','data-itemid',id])).digest('hex');
    for(const host of ['outlook.office.com','outlook.cloud.microsoft']){
      await page.goto(`https://${host}/mail/inbox`);
      const value={page,provider:'browser_outlook'},before=navigations;
      const result=await operate(value,request);
      assert.deepEqual(result.messages.map(message=>[message.message_id,message.browser_reference.row_id]),[['A-1','A-1'],['B-1','B-1']],`a pane without a conversation heading still reads every row from its own message (${host})`);
      assert.deepEqual(result.warnings.filter(warning=>/another check/.test(warning)),[],host);
      assert.equal(navigations,before+1,`waiting out the repaint costs no fresh document (${host})`);
      const cache=JSON.parse(result.cursor).row_cache;
      assert.ok(key('A-1') in cache&&key('B-1') in cache,`both item rows are cached from their own messages (${host})`);
    }
  }finally{Object.assign(budgets.browser_outlook,saved);await browser.close();}
});

test('the conversation opened in the Focused pass is still in the pane during the Other pass and is never read as an Other row',{timeout:120000},async()=>{
  const saved={...budgets.browser_outlook};
  Object.assign(budgets.browser_outlook,{pane:6000,grace:500,settle:3000});
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();let navigations=0;
    // Both passes share one document, so the Focused conversation is still in the reading pane while the Other tab is scanned;
    // it even receives a second message there, and its own row is in neither pass's list.
    await page.route('**/*',route=>{
      if(route.request().resourceType()==='document')navigations++;
      const host=new URL(route.request().url()).hostname;
      return route.fulfill({contentType:'text/html',body:`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main"><button role="tab" aria-selected="true" onclick="pick(this)">Focused</button><button role="tab" aria-selected="false" onclick="pick(this)">Other</button><div role="listbox" id="list"></div><section id="pane"></section></main><script>
if(${JSON.stringify(host)}==='outlook.cloud.microsoft'&&location.pathname==='/mail/inbox')history.replaceState(null,'','/mail/');
const lists={Focused:['F1'],Other:['B','C']};
const message=(conversation,n)=>'<h2 id="CONV_'+conversation+'_SUBJECT">Thread '+conversation+'</h2><div aria-label="Email message" data-message-id="'+conversation+'-'+n+'"><span id="MSG_'+conversation+'-'+n+'_FROM" email="sender@example.test">Sender</span><div id="MSG_'+conversation+'-'+n+'_SUBJECT">Thread '+conversation+'</div><div role="document">Body '+conversation+' '+n+'</div></div>';
const open=()=>document.querySelector('#pane h2')?.id.slice(5,-8)||'';
function render(name){document.querySelector('#list').innerHTML=lists[name].map(id=>'<div role="option" data-convid="'+id+'" aria-selected="false" onclick="openMail(\\''+id+'\\')">'+id+' 1</div>').join('');}
function pick(node){document.querySelectorAll('[role=tab]').forEach(tab=>tab.setAttribute('aria-selected',String(tab===node)));render(node.textContent);}
function openMail(id){
  document.querySelectorAll('[role=option]').forEach(row=>row.setAttribute('aria-selected',String(row.getAttribute('data-convid')===id)));
  const base=location.pathname.split('/id/')[0];history.replaceState(null,'',(base.endsWith('/')?base.slice(0,-1):base)+'/id/'+id);
  if(id==='F1'){document.querySelector('#pane').innerHTML=message('F1',1);return;}
  // A reply lands in the Focused conversation while the first Other row loads; that conversation has no row on this tab.
  setTimeout(()=>{if(open()==='F1')document.querySelector('#pane').innerHTML=message('F1',2);},200);
  setTimeout(()=>{document.querySelector('#pane').innerHTML=message(id,1);},2500);
}
render('Focused');
</script>`});
    });
    const request={command:'sync',connection:{email:'owner@example.test'}};
    const key=(tab,id)=>createHash('sha256').update(JSON.stringify([tab,'data-convid',id])).digest('hex');
    for(const host of ['outlook.office.com','outlook.cloud.microsoft']){
      await page.goto(`https://${host}/mail/inbox`);
      const value={page,provider:'browser_outlook'},before=navigations;
      const result=await operate(value,request);
      assert.deepEqual(result.messages.map(message=>[message.message_id,message.browser_reference.row_id]),[['F1-1','F1'],['B-1','B'],['C-1','C']],`the second message of the Focused conversation is never imported under an Other row (${host})`);
      assert.deepEqual(result.warnings.filter(warning=>/another check/.test(warning)),[],host);
      assert.equal(navigations,before+1,`one navigation covers both inbox tabs (${host})`);
      const cursor=JSON.parse(result.cursor);
      assert.equal(cursor.split_inbox,true);
      assert.ok(key('Focused','F1') in cursor.row_cache&&key('Other','B') in cursor.row_cache&&key('Other','C') in cursor.row_cache,`every row is cached under its own tab (${host})`);
    }
  }finally{Object.assign(budgets.browser_outlook,saved);await browser.close();}
});

test('Gmail threads sharing a subject are read with no extra navigation and no wait of their own',{timeout:60000},async()=>{
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();let navigations=0;
    // Three threads with one subject and an unbound reading pane: the shape that must cost Gmail nothing beyond the paint it waits for.
    await page.route('**/*',route=>{
      if(route.request().resourceType()==='document')navigations++;
      return route.fulfill({contentType:'text/html',body:`<a aria-label="Google Account: Owner (owner@example.test)">Account</a><div role="main"><table>${['A','B','C'].map(id=>`<tr data-legacy-thread-id="${id}" onclick="openMail('${id}')"><td>Re: Invoice</td></tr>`).join('')}</table><section id="pane"></section></div><script>
window.clicked={};window.painted={};
function openMail(id){
  window.clicked[id]=Date.now();
  document.querySelector('#pane').innerHTML='';
  setTimeout(()=>{document.querySelector('#pane').innerHTML='<h2 class="hP">Re: Invoice</h2><article data-legacy-message-id="'+id+'-1"><span class="gD" email="sender@example.test">Sender</span><div class="a3s">Body '+id+'</div></article>';window.painted[id]=Date.now();},200);
}
</script>`});
    });
    await page.goto('https://mail.google.com/mail/u/0/#inbox');
    const value={page,provider:'browser_gmail',lastInboxRefresh:Date.now(),inboxReady:true},request={command:'sync',connection:{email:'owner@example.test'}};
    const before=navigations;
    const result=await operate(value,request);
    assert.deepEqual(result.messages.map(message=>[message.message_id,message.browser_reference.row_id]),[['A-1','A'],['B-1','B'],['C-1','C']],'a shared subject never moves a Gmail message to another row');
    assert.deepEqual(result.warnings.filter(warning=>/another check/.test(warning)),[]);
    assert.equal(navigations,before,'Gmail reads its threads without a navigation');
    const {clicked,painted}=await page.evaluate(()=>({clicked:window.clicked,painted:window.painted}));
    for(const [current,next] of [['A','B'],['B','C']])assert.ok(clicked[next]-painted[current]<500,`no confirmation window is spent on the shared subject (${current} to ${next}: ${clicked[next]-painted[current]} ms)`);
  }finally{await browser.close();}
});

test('a conversation that receives mail in the gap while the pane is torn down is never read under the row the click opened',{timeout:120000},async()=>{
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();let navigations=0,order;
    // Outlook empties the pane at click time and moves the item path with it, so the clicked row owns both. The conversation
    // open until that click then receives a reply and repaints into the gap, before the clicked one paints at all.
    await page.route('**/*',route=>{
      if(route.request().resourceType()==='document')navigations++;
      return route.fulfill({contentType:'text/html',body:`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main"><div role="option" data-convid="A" aria-selected="false" onclick="openMail('A')">A 1</div><div role="option" data-convid="B" aria-selected="false" onclick="openMail('B')">B 1</div><section id="pane"></section></main><script>
const paneFirst=${JSON.stringify(order)}==='pane repaints first';
if(location.hostname==='outlook.cloud.microsoft'&&location.pathname==='/mail/inbox')history.replaceState(null,'','/mail/');
const article=(conv,n)=>'<article data-message-id="'+conv+'-'+n+'"><span email="sender'+conv+n+'@example.test">Sender</span><div role="document">Body '+conv+' '+n+'</div></article>';
function setUrl(conv){const base=location.pathname.split('/id/')[0];history.replaceState(null,'',(base.endsWith('/')?base.slice(0,-1):base)+'/id/'+conv);}
function show(conv,n){document.querySelector('#pane').innerHTML='<h2 data-testid="conversation-subject">Thread '+conv+'</h2>'+article(conv,n);}
function openMail(id){
  document.querySelectorAll('[role=option]').forEach(row=>row.setAttribute('aria-selected',String(row.getAttribute('data-convid')===id)));
  setUrl(id);
  document.querySelector('#pane').innerHTML='';
  if(id==='A'){setTimeout(()=>show('A',1),300);return;}
  // The reply reaches the pane and the row it belongs to in either order, and the clicked conversation paints long after both.
  setTimeout(()=>show('A',2),paneFirst?250:900);
  setTimeout(()=>{document.querySelector('[data-convid="A"]').textContent='A 2';},paneFirst?900:250);
  setTimeout(()=>show('B',1),2500);
}
</script>`});
    });
    const request={command:'sync',connection:{email:'owner@example.test'}};
    const key=id=>createHash('sha256').update(JSON.stringify(['Focused','data-convid',id])).digest('hex');
    for(order of ['pane repaints first','row repaints first'])for(const host of ['outlook.office.com','outlook.cloud.microsoft']){
      await page.goto(`https://${host}/mail/inbox`);
      const value={page,provider:'browser_outlook'},before=navigations;
      const result=await operate(value,request);
      assert.deepEqual(result.messages.map(message=>[message.message_id,message.browser_reference.row_id,message.sender]),[['A-1','A','senderA1@example.test'],['B-1','B','senderB1@example.test']],`the reply that painted into the torn-down pane is not row B's message (${order}, ${host})`);
      assert.deepEqual(result.warnings.filter(warning=>/another check/.test(warning)),[],`${order}, ${host}`);
      assert.equal(navigations,before+1,`waiting out the repaint costs no fresh document (${order}, ${host})`);
      const cache=JSON.parse(result.cursor).row_cache;
      assert.ok(key('A') in cache&&key('B') in cache,`both rows are cached from their own messages (${order}, ${host})`);
    }
  }finally{await browser.close();}
});

test('a repaint of a conversation sharing the clicked row\'s subject is never read under it: the row fails cleanly and is reopened from a fresh document',{timeout:120000},async()=>{
  const saved={...budgets.browser_outlook};
  Object.assign(budgets.browser_outlook,{pane:3000,grace:500,settle:2000});
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();let navigations=0,order;
    // Every conversation carries the same subject and the pane names none of them, so the heading cannot tell a repaint from
    // the clicked conversation's own paint. Only the row the reply moves can, and it moves before or after the repaint.
    await page.route('**/*',route=>{
      if(route.request().resourceType()==='document')navigations++;
      return route.fulfill({contentType:'text/html',body:`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main"><div role="option" data-convid="A" aria-selected="false" onclick="openMail('A')">A 1</div><div role="option" data-convid="B" aria-selected="false" onclick="openMail('B')">B 1</div><section id="pane"></section></main><script>
const paneFirst=${JSON.stringify(order)}==='pane repaints first';
const store='replies-'+${JSON.stringify(order)};let replies=Number(sessionStorage.getItem(store)||1);
if(replies>1)document.querySelector('[data-convid="A"]').textContent='A '+replies;
const article=(conv,n)=>'<article data-message-id="'+conv+'-'+n+'"><span email="sender'+conv+n+'@example.test">Sender</span><div role="document">Body '+conv+' '+n+'</div></article>';
function show(conv,n){document.querySelector('#pane').innerHTML='<h2 data-testid="conversation-subject">Weekly report</h2>'+article(conv,n);}
function openMail(id){
  document.querySelectorAll('[role=option]').forEach(row=>row.setAttribute('aria-selected',String(row.getAttribute('data-convid')===id)));
  const base=location.pathname.split('/id/')[0];history.replaceState(null,'',(base.endsWith('/')?base.slice(0,-1):base)+'/id/'+id);
  if(id==='A'){show('A',replies);return;}
  // Conversation A is still open, so the reply repaints it; row B never paints on this document at all.
  if(document.querySelector('#pane h2')){
    replies=2;sessionStorage.setItem(store,'2');
    setTimeout(()=>show('A',2),paneFirst?150:1200);
    setTimeout(()=>{document.querySelector('[data-convid="A"]').textContent='A 2';},paneFirst?1200:150);
    return;
  }
  show('B',1);
}
</script>`});
    });
    const request={command:'sync',connection:{email:'owner@example.test'}};
    const key=id=>createHash('sha256').update(JSON.stringify(['Focused','data-convid',id])).digest('hex');
    for(order of ['pane repaints first','row repaints first']){
      await page.goto('https://outlook.office.com/mail/inbox');
      const value={page,provider:'browser_outlook'},before=navigations;
      const result=await operate(value,request);
      assert.deepEqual(result.messages.map(message=>[message.body,message.browser_reference.row_id]),[['Body A 1','A'],['Body B 1','B']],`the repaint sharing the subject is never row B's message (${order})`);
      assert.equal(navigations,before+2,`row B is proven on a fresh document, once (${order})`);
      const cache=JSON.parse(result.cursor).row_cache;
      assert.ok(key('A') in cache&&key('B') in cache,`both rows are cached from their own messages (${order})`);
    }
  }finally{Object.assign(budgets.browser_outlook,saved);await browser.close();}
});

test('six ordinary conversations whose reading pane names none of them are all read in one navigation',{timeout:90000},async()=>{
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();let navigations=0;
    // No conversation id anywhere in the pane and no id a row shares with its message: the shape the worker may not answer
    // with a fresh document per row, because nothing about this mailbox is unhealthy.
    await page.route('**/*',route=>{
      if(route.request().resourceType()==='document')navigations++;
      return route.fulfill({contentType:'text/html',body:`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main">${['A','B','C','D','E','F'].map(id=>`<div role="option" data-convid="${id}" aria-selected="false" onclick="openMail('${id}')">Report ${id}</div>`).join('')}<section id="pane"></section></main><script>
function openMail(id){
  document.querySelectorAll('[role=option]').forEach(row=>row.setAttribute('aria-selected',String(row.getAttribute('data-convid')===id)));
  const base=location.pathname.split('/id/')[0];history.replaceState(null,'',(base.endsWith('/')?base.slice(0,-1):base)+'/id/'+id);
  document.querySelector('#pane').innerHTML='<h2 data-testid="conversation-subject">Report '+id+'</h2><article data-message-id="'+id+'-1"><span email="sender'+id+'@example.test">Sender</span><div role="document">Body '+id+'</div></article>';
}
</script>`});
    });
    const request={command:'sync',connection:{email:'owner@example.test'}};
    const key=id=>createHash('sha256').update(JSON.stringify(['Focused','data-convid',id])).digest('hex');
    for(const host of ['outlook.office.com','outlook.cloud.microsoft']){
      await page.goto(`https://${host}/mail/inbox`);
      const value={page,provider:'browser_outlook'},before=navigations;
      const result=await operate(value,request);
      assert.deepEqual(result.messages.map(message=>[message.message_id,message.browser_reference.row_id]),['A','B','C','D','E','F'].map(id=>[`${id}-1`,id]),`every row is read from its own pane in one scan (${host})`);
      assert.deepEqual(result.warnings.filter(warning=>/another check|did not finish/.test(warning)),[],host);
      assert.equal(navigations,before+1,`an unnamed pane is no reason to reload a healthy mailbox (${host})`);
      const cache=JSON.parse(result.cursor).row_cache;
      assert.ok(['A','B','C','D','E','F'].every(id=>key(id) in cache),`every row is cached (${host})`);
    }
  }finally{await browser.close();}
});

test('a conversation heading rendered inside a list row is not the reading pane\'s, so the pane its own heading names is still read',{timeout:60000},async()=>{
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();let navigations=0;
    // Each list row carries a CONV_<id>_SUBJECT of its own. Counting those as reading-pane headings leaves no single heading
    // to name the open conversation and makes every other row's id disprove it, so the mailbox stays unreadable.
    await page.route('**/*',route=>{
      if(route.request().resourceType()==='document')navigations++;
      return route.fulfill({contentType:'text/html',body:`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main">${['A','B','C'].map(id=>`<div role="option" data-convid="${id}" aria-selected="false" onclick="openMail('${id}')"><span id="CONV_${id}_SUBJECT">Weekly report</span></div>`).join('')}<section id="pane"></section></main><script>
function openMail(id){
  document.querySelectorAll('[role=option]').forEach(row=>row.setAttribute('aria-selected',String(row.getAttribute('data-convid')===id)));
  document.querySelector('#pane').innerHTML='<h2 id="CONV_'+id+'_SUBJECT">Weekly report</h2><div aria-label="Email message" data-message-id="'+id+'-1"><span email="sender'+id+'@example.test">Sender</span><div role="document">Body '+id+'</div></div>';
}
</script>`});
    });
    await page.goto('https://outlook.cloud.microsoft/mail/inbox');
    const value={page,provider:'browser_outlook'},request={command:'sync',connection:{email:'owner@example.test'}};
    const before=navigations;
    const result=await operate(value,request);
    assert.deepEqual(result.messages.map(message=>[message.message_id,message.browser_reference.row_id]),[['A-1','A'],['B-1','B'],['C-1','C']],'the heading inside the pane names the conversation, the ones inside the rows do not');
    assert.deepEqual(result.warnings.filter(warning=>/another check|did not finish/.test(warning)),[]);
    assert.equal(navigations,before+1,'headings in the list are no reason to reload');
  }finally{await browser.close();}
});

test('a pane that paints another listed conversation is never read under the clicked row, however different its heading',{timeout:60000},async()=>{
  const saved={...budgets.browser_outlook};
  Object.assign(budgets.browser_outlook,{pane:6000,grace:500,settle:2000});
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();let navigations=0;
    // Outlook puts a third conversation of the same list into the pane while the clicked one loads, so its heading differs
    // from the one the pane carried at click time as well as from the clicked row's.
    await page.route('**/*',route=>{
      if(route.request().resourceType()==='document')navigations++;
      return route.fulfill({contentType:'text/html',body:`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main">${['A','B','U'].map(id=>`<div role="option" data-convid="${id}" aria-selected="false" onclick="openMail('${id}')">${id} 1</div>`).join('')}<section id="pane"></section></main><script>
const message=conversation=>'<h2 id="CONV_'+conversation+'_SUBJECT">Thread '+conversation+'</h2><div aria-label="Email message" data-message-id="'+conversation+'-1"><span id="MSG_'+conversation+'-1_FROM" email="sender@example.test">Sender</span><div id="MSG_'+conversation+'-1_SUBJECT">Thread '+conversation+'</div><div role="document">Body '+conversation+' 1</div></div>';
function openMail(id){
  document.querySelectorAll('[role=option]').forEach(row=>row.setAttribute('aria-selected',String(row.getAttribute('data-convid')===id)));
  if(id!=='B'){document.querySelector('#pane').innerHTML=message(id);return;}
  setTimeout(()=>{document.querySelector('#pane').innerHTML=message('U');},200);
  setTimeout(()=>{document.querySelector('#pane').innerHTML=message('B');},2500);
}
</script>`});
    });
    await page.goto('https://outlook.office.com/mail/inbox');
    const value={page,provider:'browser_outlook'},request={command:'sync',connection:{email:'owner@example.test'}};
    const before=navigations;
    const result=await operate(value,request);
    assert.deepEqual(result.messages.map(message=>[message.message_id,message.browser_reference.row_id]),[['A-1','A'],['B-1','B'],['U-1','U']],'conversation U is read under its own row, never under B');
    assert.deepEqual(result.warnings.filter(warning=>/another check/.test(warning)),[]);
    assert.equal(navigations,before+1,'waiting for the clicked conversation costs no fresh document');
  }finally{Object.assign(budgets.browser_outlook,saved);await browser.close();}
});

test('a message that paints after the proof was taken is not imported under it and leaves its row unchecked',{timeout:60000},async()=>{
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();
    // The pane gains a second message in the round trip between the proof and the read, so the read returns a message the
    // proof never covered.
    await page.route('**/*',route=>route.fulfill({contentType:'text/html',body:`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main"><div role="option" data-convid="A" aria-selected="false" onclick="openMail('A')">A 1</div><div role="option" data-convid="B" aria-selected="false" onclick="openMail('B')">B 1</div><section id="pane"></section></main><script>
const all=document.querySelectorAll.bind(document);let armed=false;
// Reopening row B takes a fresh document, and a message that already arrived is still there on it.
const arrival=()=>sessionStorage.getItem('arrived')==='1';
const article=(conversation,n)=>'<article data-convid="'+conversation+'" data-message-id="'+conversation+'-'+n+'"><span email="sender'+n+'@example.test">Sender</span><div role="document">Body '+conversation+' '+n+'</div></article>';
// The body query the worker runs to read the pane is the moment B's second message arrives, one round trip after the proof.
document.querySelectorAll=selector=>{
  const nodes=all(selector);
  if(armed&&String(selector).includes('[aria-label="Message body"]')){armed=false;setTimeout(()=>{sessionStorage.setItem('arrived','1');document.querySelector('#pane').insertAdjacentHTML('beforeend',article('B',2));},0);}
  return nodes;
};
function openMail(id){
  all('[role=option]').forEach(row=>row.setAttribute('aria-selected',String(row.getAttribute('data-convid')===id)));
  document.querySelector('#pane').innerHTML='<h2 data-testid="conversation-subject">Thread '+id+'</h2>'+article(id,1)+(id==='B'&&arrival()?article('B',2):'');
  armed=id==='B'&&!arrival();
}
</script>`}));
    await page.goto('https://outlook.office.com/mail/inbox');
    const value={page,provider:'browser_outlook'},request={command:'sync',connection:{email:'owner@example.test'}};
    const first=await operate(value,request);
    assert.deepEqual(first.messages.map(message=>[message.message_id,message.browser_reference.row_id]),[['A-1','A'],['B-1','B']],'the message the proof covered is still read');
    assert.deepEqual(first.warnings.filter(warning=>/another check/.test(warning)),['A message needs another check: The selected message did not finish loading.'],'the message that arrived after it is reported, not imported');
    const key=id=>createHash('sha256').update(JSON.stringify(['Focused','data-convid',id])).digest('hex');
    const cache=JSON.parse(first.cursor).row_cache;
    assert.ok(key('A') in cache,'the row whose pane was read in full is cached');
    assert.ok(!(key('B') in cache),'the row whose pane gained a message is not, so the next scan reads it');
    const second=await operate(value,{...request,cursor:first.cursor});
    assert.deepEqual(second.messages.map(message=>message.message_id),['B-2'],'the message that arrived late is imported on the next scan');
  }finally{await browser.close();}
});

test('a scan whose only row refuses a pane it cannot prove keeps its cursor instead of reporting a mailbox failure',{timeout:60000},async()=>{
  const saved={...budgets.browser_outlook};
  Object.assign(budgets.browser_outlook,{pane:1500,grace:500,settle:2000});
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();
    // The pane already holds another conversation and the click never changes it, so the one row of this scan refuses
    // cleanly while the page stays perfectly alive.
    await page.route('**/*',route=>route.fulfill({contentType:'text/html',body:`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main"><div role="option" data-convid="B" aria-selected="false" onclick="openMail('B')">B 1</div><section id="pane"><h2 id="CONV_A_SUBJECT">Thread A</h2><div aria-label="Email message" data-convid="A" data-message-id="A-1"><span id="MSG_A-1_FROM" email="sender@example.test">Sender</span><div id="MSG_A-1_SUBJECT">Thread A</div><div role="document">Body A 1</div></div></section></main><script>
function openMail(id){document.querySelectorAll('[role=option]').forEach(row=>row.setAttribute('aria-selected',String(row.getAttribute('data-convid')===id)));}
</script>`}));
    await page.goto('https://outlook.office.com/mail/inbox');
    const value={page,provider:'browser_outlook'},request={command:'sync',connection:{email:'owner@example.test'}};
    const result=await operate(value,request);
    assert.deepEqual(result.messages,[],'the conversation still in the pane is not read under row B');
    assert.deepEqual(result.warnings.filter(warning=>/another check/.test(warning)),['A message needs another check: The selected message did not finish loading.']);
    const cursor=JSON.parse(result.cursor);
    assert.equal(cursor.contract,'browser-sync/v3','a scan of clean refusals still returns its cursor');
    assert.deepEqual(cursor.row_cache,{},'the row it could not prove is not cached');
  }finally{Object.assign(budgets.browser_outlook,saved);await browser.close();}
});

test('every fresh document the row loop spends is charged to one budget, so a list of late panes stops instead of reloading per row',{timeout:90000},async()=>{
  const saved={...budgets.browser_outlook};
  Object.assign(budgets.browser_outlook,{pane:1000,grace:500,settle:2000,boot:4000});
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();let documents=0;
    // Every row but the first paints after its own pane wait, so each one leaves a late paint the next row has to discard.
    await page.route('**/*',route=>{
      if(route.request().resourceType()==='document')documents++;
      return route.fulfill({contentType:'text/html',body:`<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main">${['A','B','C','D','E','F'].map(id=>`<div role="option" data-convid="${id}" aria-selected="false" onclick="openMail('${id}')">${id} 1</div>`).join('')}<section id="pane"></section></main><script>
function openMail(id){
  document.querySelectorAll('[role=option]').forEach(row=>row.setAttribute('aria-selected',String(row.getAttribute('data-convid')===id)));
  document.querySelector('#pane').innerHTML='';
  const paint=()=>{document.querySelector('#pane').innerHTML='<h2 id="CONV_'+id+'_SUBJECT">Thread '+id+'</h2><div aria-label="Email message" data-convid="'+id+'" data-message-id="'+id+'-1"><span id="MSG_'+id+'-1_FROM" email="sender@example.test">Sender</span><div id="MSG_'+id+'-1_SUBJECT">Thread '+id+'</div><div role="document">Body '+id+'</div></div>';};
  if(id==='A')paint();else setTimeout(paint,2000);
}
</script>`});
    });
    await page.goto('https://outlook.office.com/mail/inbox');
    const value={page,provider:'browser_outlook'},request={command:'sync',connection:{email:'owner@example.test'}};
    const before=documents;
    const result=await operate(value,request);
    assert.deepEqual(result.messages.map(message=>message.message_id),['A-1'],'only the row whose pane painted inside its own wait is read');
    assert.ok(result.warnings.some(warning=>/will be checked on the next scan/.test(warning)),'the scan stops instead of reloading for every remaining row');
    assert.equal(documents,before+3,'the boot and at most two discarding documents; never one per row');
  }finally{Object.assign(budgets.browser_outlook,saved);await browser.close();}
});

test('a tenant that names conversations by the tail of the row id and renders no message envelope is read: the heading proves the row and the pane body is identified by its own text',{timeout:60000},async()=>{
  const conversation='AAQkADY0ZmYxZDFkLTRjYzItNGIwZC04YTY1LWQ3YzI4YjMzMDU0YwAQAFlSQLLe6L5KhmM7fdIw8N8=';
  const other='AAQkADY0ZmYxZDFkLTRjYzItNGIwZC04YTY1LWQ3YzI4YjMzMDU0YwAQAKZtRmC3n0hLsDIP4OUHtcA=';
  const tail=conversation.slice(-11);
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();let navigations=0;
    await page.route('**/*',route=>{
      if(route.request().resourceType()==='document')navigations++;
      // Outlook Web on outlook.cloud.microsoft keys the pane heading by the tail of the row id and renders no message envelope,
      // so the pane body carries no message id of its own.
      return route.fulfill({contentType:'text/html',body:`<div id="mectrl_currentAccount_secondary">owner@example.test</div><div id="rows"></div><div id="pane"></div><script>
const rows=${JSON.stringify([conversation,other])};
function render(){document.querySelector('#rows').innerHTML=rows.map(id=>'<div role="option" data-convid="'+id+'" aria-selected="false" onclick="openMail(this)">Ticket '+id.slice(-4)+'</div>').join('');}
function openMail(row){
  const id=row.getAttribute('data-convid');
  document.querySelectorAll('[role=option]').forEach(node=>node.setAttribute('aria-selected',String(node===row)));
  history.replaceState(null,'','/mail/id/AAkALgAAAAAAHYQDEapmEc2byACqAB'+id.slice(-6));
  document.querySelector('#pane').innerHTML='<h2 id="CONV_'+id.slice(-11)+'_SUBJECT">Ticket '+id.slice(-4)+'</h2>'+
    '<div class="msg"><span id="MSG_'+id.slice(-4)+'-1_FROM" email="sender@example.test">Sender</span><div role="document">Body '+id.slice(-4)+'</div></div>';
}
history.replaceState(null,'','/mail/');render();
</script>`});
    });
    await page.goto('https://outlook.cloud.microsoft/mail/inbox');
    const value={page,provider:'browser_outlook'},request={command:'sync',connection:{email:'owner@example.test'}};
    const first=await operate(value,request);
    assert.deepEqual(first.messages.map(message=>message.body),['Body '+conversation.slice(-4),'Body '+other.slice(-4)],'both conversations are read although the pane names them only by their tail');
    assert.deepEqual(first.messages.map(message=>message.browser_reference.row_id),[conversation,other],'each message is filed under its own row');
    assert.equal(Object.keys(JSON.parse(first.cursor).row_cache).length,2,'both rows are cached');
    const before=navigations;
    await operate(value,{...request,cursor:first.cursor});
    assert.equal(navigations,before,'a pane named by a tail needs no fresh document');
  }finally{await browser.close();}
});

test('a conversation tail shared with another conversation the scan knows proves nothing, and a tail too short to identify anything is never proof',()=>{
  const conversation='AAQkADY0ZmYxZDFkLTRjYzItNGIwZC04YTY1LWQ3YzI4YjMzMDU0YwAQAFlSQLLe6L5KhmM7fdIw8N8=';
  const tail=conversation.slice(-11);
  const view={bodies:[['','Body']],owners:[[]],heads:[tail],bindings:[],selected:[],item:'',subject:'Ticket'};
  assert.equal(paneProvesRow(view,[conversation],new Set([conversation,'AAQkUnrelatedConversationId='])),true,'a tail matching this row and no other identifies it');
  assert.equal(paneProvesRow(view,[conversation],new Set([conversation,'AAQkADY0OtherRootButSame'+tail])),false,'a tail shared with another conversation identifies neither');
  assert.equal(paneProvesRow({...view,heads:[tail.slice(-4)]},[conversation],new Set([conversation])),false,'a tail too short to be distinctive is never proof');
  assert.equal(paneProvesRow({...view,bodies:[]},[conversation],new Set([conversation])),false,'a pane with no rendered body proves nothing');
  // The same relation decides the other way round, so a tail cannot prove this row while naming another conversation.
  assert.equal(paneNamesOther(view,['AAQkADY0SomeOtherRowEntirely='],new Set([conversation])),true,'a tail of a conversation the scan knows names that conversation');
  assert.equal(paneNamesOther(view,[conversation],new Set([conversation])),false,'the clicked row\'s own tail names no other conversation');
});

test('a failing inbox tab leads the next scan instead of being demoted again',async()=>{
  const installed=findInstalledBrowser();
  assert.ok(installed,'Chrome or Edge required for browser mail fixture test');
  const browser=await chromium.launch({executablePath:installed.executable,headless:true});
  try {
    const page=await browser.newPage();
    const pane=id=>`<div data-convid="${id}"><h2 data-testid="conversation-subject">Thread</h2><article data-message-id="m-${id}"><div data-testid="SenderPersona"><span title="${id}@example.test">s</span></div><div role="document">Body ${id}</div></article></div>`;
    // The Other tab never reports itself selected, so switching to it always fails.
    await page.route('**/*',route=>route.fulfill({contentType:'text/html',body:
      '<button id="mectrl_main_trigger" aria-label="person@example.test">Account</button><main role="main">'
      +'<div role="tablist"><button role="tab" aria-selected="true">Focused</button><button role="tab" aria-selected="false">Other</button></div>'
      +`<div role="option" data-convid="thread1" onclick='document.querySelector("#pane").innerHTML=${JSON.stringify(pane('thread1')).replaceAll("'",'&#39;')}'>Thread</div>`
      +'<section id="pane"></section></main>'}));
    await page.goto('https://outlook.office.com/mail/inbox');
    await page.bringToFront();
    await page.locator('[data-convid="thread1"]').click({trial:true,timeout:15000});
    const value={page,provider:'browser_outlook',lastInboxRefresh:Date.now()};
    const result=await operate(value,{command:'sync',connection:{email:'person@example.test'}});
    const cursor=JSON.parse(result.cursor);
    assert.equal(cursor.split_inbox,true,'the fixture must present a split inbox');
    assert.equal(cursor.next_tab,'Other','the tab that failed must lead the next scan, not the tab that already succeeded');
    assert.ok(result.warnings.some(w=>/Other inbox tab did not finish loading/.test(w)&&/checked first on the next scan/.test(w)),
      'the warning must say the failed tab is checked first next time');
    assert.equal(result.messages.length,1,'the working tab must still import its mail');
  } finally {await browser.close();}
});
test('the inbox scan scrolls the virtualised list and reads conversations below the first viewport',async()=>{
  const installed=findInstalledBrowser();
  assert.ok(installed,'Chrome or Edge required for browser mail fixture test');
  const browser=await chromium.launch({executablePath:installed.executable,headless:true});
  try {
    const page=await browser.newPage();
    // Mirrors Outlook: rows outside the scrolled window are removed from the DOM entirely.
    const script=`
      const TOTAL=6,H=30,VIEW=2,list=document.getElementById('list'),spacer=document.getElementById('spacer');
      function render(){
        const start=Math.max(0,Math.floor(list.scrollTop/H)),end=Math.min(TOTAL,start+VIEW);
        spacer.querySelectorAll('[role=option]').forEach(node=>node.remove());
        for(let i=start;i<end;i++){
          const row=document.createElement('div');
          row.setAttribute('role','option');row.setAttribute('data-convid','thread'+i);
          row.style.cssText='position:absolute;left:0;right:0;height:'+H+'px;top:'+(i*H)+'px';
          row.textContent='Thread '+i;
          row.addEventListener('click',()=>{document.getElementById('pane').innerHTML=
            '<div data-convid="thread'+i+'"><h2 data-testid="conversation-subject">Thread '+i+'</h2>'+
            '<article data-message-id="m'+i+'"><div data-testid="SenderPersona"><span title="s'+i+'@example.test">s</span></div>'+
            '<time datetime="2026-09-17T0'+(i%10)+':00:00Z">when</time><div role="document">Body '+i+'</div></article></div>';});
          spacer.appendChild(row);
        }
      }
      list.addEventListener('scroll',render);render();`;
    await page.route('**/*',route=>route.fulfill({contentType:'text/html',body:
      '<button id="mectrl_main_trigger" aria-label="person@example.test">Account</button><main role="main">'
      +'<div id="list" style="height:60px;overflow-y:auto"><div id="spacer" style="height:180px;position:relative"></div></div>'
      +'<section id="pane"></section></main><script>'+script+'</'+'script>'}));
    await page.goto('https://outlook.office.com/mail/inbox');
    await page.bringToFront();
    await page.locator('[data-convid="thread0"]').click({trial:true,timeout:15000});
    // Control: without scrolling the list only ever exposes one window of rows.
    assert.equal((await page.evaluate(readRows,{provider:'browser_outlook'})).length,2,'the fixture must virtualise');
    const value={page,provider:'browser_outlook',lastInboxRefresh:Date.now()};
    const result=await operate(value,{command:'sync',connection:{email:'person@example.test'}});
    const senders=result.messages.map(message=>message.sender);
    assert.ok(senders.length>2,`the scan must read past the first window, got ${senders.length}`);
    assert.ok(senders.some(sender=>Number(/^s(\d+)@/.exec(sender)[1])>=3),'a conversation below the first window must be imported');
    assert.equal(result.has_more,senders.length<6,`remaining backlog must match what was left over: has_more=${result.has_more} read=${senders.length} warnings=${JSON.stringify(result.warnings)}`);
    assert.ok(result.messages.every(message=>/^2026-09-17T/.test(message.received_at)),'each message must carry the date its pane showed');
  } finally {await browser.close();}
});
test('the reconciliation offset advances on every scan instead of freezing on one row',async()=>{
  const installed=findInstalledBrowser();
  assert.ok(installed,'Chrome or Edge required for browser mail fixture test');
  const browser=await chromium.launch({executablePath:installed.executable,headless:true});
  try {
    const page=await browser.newPage();
    const pane=i=>`<div data-convid="thread${i}"><h2 data-testid="conversation-subject">Thread ${i}</h2><article data-message-id="m${i}"><div data-testid="SenderPersona"><span title="s${i}@example.test">s</span></div><div role="document">Body ${i}</div></article></div>`;
    const row=i=>`<div role="option" data-convid="thread${i}" onclick='document.querySelector("#pane").innerHTML=${JSON.stringify(pane(i)).replaceAll("'",'&#39;')}'>Thread ${i}</div>`;
    await page.route('**/*',route=>route.fulfill({contentType:'text/html',body:
      '<button id="mectrl_main_trigger" aria-label="person@example.test">Account</button><main role="main">'
      +row(0)+row(1)+row(2)+'<section id="pane"></section></main>'}));
    await page.goto('https://outlook.office.com/mail/inbox');
    await page.bringToFront();
    await page.locator('[data-convid="thread0"]').click({trial:true,timeout:15000});
    const value={page,provider:'browser_outlook',lastInboxRefresh:Date.now()};
    const request={command:'sync',connection:{email:'person@example.test'}};
    const first=await operate(value,request);
    const second=await operate(value,{...request,cursor:first.cursor});
    const offsets=[JSON.parse(first.cursor).offset,JSON.parse(second.cursor).offset];
    assert.deepEqual(offsets,[1,2],`the rotation must move every scan, saw ${offsets.join(',')}`);
  } finally {await browser.close();}
});

test('a conversation that keeps failing stops leading every scan, so new mail still arrives',async()=>{
  const installed=findInstalledBrowser();
  assert.ok(installed,'Chrome or Edge required for browser mail fixture test');
  const browser=await chromium.launch({executablePath:installed.executable,headless:true});
  try {
    const page=await browser.newPage();
    let arrivals=['first'];
    const pane=id=>`<div data-convid="${id}"><h2 data-testid="conversation-subject">Thread ${id}</h2><article data-message-id="m${id}"><div data-testid="SenderPersona"><span title="${id}@example.test">s</span></div><div role="document">Body ${id}</div></article></div>`;
    // A broken conversation opens a pane that never paints a message, which is how
    // an unreadable row behaves in a real mailbox.
    const row=(id,broken)=>`<div role="option" data-convid="${id}" onclick='window.opened.push("${id}");document.querySelector("#pane").innerHTML=${JSON.stringify(broken?'':pane(id)).replaceAll("'",'&#39;')}'>Thread ${id}</div>`;
    await page.route('**/*',route=>route.fulfill({contentType:'text/html',body:
      '<script>window.opened=[];</script><button id="mectrl_main_trigger" aria-label="person@example.test">Account</button><main role="main">'
      +row('broken',true)+arrivals.map(id=>row(id,false)).join('')+'<section id="pane"></section></main>'}));
    const request={command:'sync',connection:{email:'person@example.test'}};
    const scan=async cursor=>{
      await page.goto('https://outlook.office.com/mail/inbox');
      await page.bringToFront();
      await page.locator('[data-convid="broken"]').click({trial:true,timeout:15000});
      return operate({page,provider:'browser_outlook',lastInboxRefresh:Date.now()},{...request,...(cursor?{cursor}:{})});
    };
    const first=await scan();
    assert.deepEqual(first.messages.map(message=>message.browser_reference.row_id),['first'],'the readable conversation is imported despite the broken one');
    assert.deepEqual(Object.values(JSON.parse(first.cursor).row_failures).map(entry=>entry.count),[1],'the broken row records one failure');
    // A first failure is retried at once, as an ordinary slow conversation deserves.
    arrivals.push('second');
    const second=await scan(first.cursor);
    assert.deepEqual(second.messages.map(message=>message.browser_reference.row_id),['second']);
    assert.deepEqual(Object.values(JSON.parse(second.cursor).row_failures).map(entry=>entry.count),[2]);
    assert.ok((await page.evaluate(()=>window.opened)).includes('broken'),'the first failure is retried on the next scan');
    // A second failure buys the broken conversation a cooldown, and mail keeps arriving.
    arrivals.push('third');
    const third=await scan(second.cursor);
    assert.deepEqual(third.messages.map(message=>message.browser_reference.row_id),['third'],'new mail is imported while the broken conversation waits');
    assert.ok(third.warnings.some(warning=>/waiting before another attempt/.test(warning)),'the deferred conversation is reported');
    assert.ok(!(await page.evaluate(()=>window.opened)).includes('broken'),'the cooling row is not opened again this scan');
    assert.deepEqual(Object.values(JSON.parse(third.cursor).row_failures).map(entry=>entry.count),[2],'waiting is not counted as another failure');
  } finally {await browser.close();}
});


for (const total of [200,215]) test('mailbox cursor drains '+total+' mounted rows across browser restarts', {timeout:180000}, async()=>{
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try {
    const page=await browser.newPage();
    await page.route('**/*',route=>route.fulfill({contentType:'text/html',body:
      '<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main">'+
      Array.from({length:total},(_,i)=>`<div role="option" data-convid="thread${i}" onclick="openMail(${i})">Thread ${i}</div>`).join('')+
      '<section id="pane"></section></main><script>function openMail(i){document.querySelector("#pane").innerHTML=`<article data-convid="thread${i}" data-message-id="m${i}"><h2 data-testid="conversation-subject">Subject ${i}</h2><span email="sender@example.test">Sender</span><div role="document">Body ${i}</div></article>`;}</script>'})) ;
    let cursor,hasMore=true;const imported=new Set();
    for(let scan=0;scan<30&&hasMore;scan++){
      await page.goto('https://outlook.office.com/mail/inbox');
      const result=await operate({page,provider:'browser_outlook',lastInboxRefresh:Date.now()}, {command:'sync',connection:{email:'owner@example.test'},cursor});
      for(const message of result.messages)imported.add(message.message_id);
      cursor=result.cursor;hasMore=result.has_more;
    }
    assert.equal(imported.size,total,'every row, including rows beyond both original caps, must import');
    assert.equal(hasMore,false,'reaching the actual list end completes the backlog');
    assert.equal(JSON.parse(cursor).walks.Focused.pending.length,0);
  } finally {await browser.close();}
});

test('virtualized mailbox cursor resumes beyond the scroll budget and survives restarts', {timeout:240000}, async()=>{
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try {
    const page=await browser.newPage();
    await page.route('**/*',route=>route.fulfill({contentType:'text/html',body:`
      <button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main">
      <div id="list" style="height:60px;overflow-y:auto"><div id="spacer" style="height:2700px;position:relative"></div></div><section id="pane"></section></main>
      <script>
      const list=document.querySelector('#list'),spacer=document.querySelector('#spacer');
      function render(){
        const start=Math.floor(list.scrollTop/30);spacer.replaceChildren();
        for(let i=start;i<Math.min(90,start+3);i++){
          const row=document.createElement('div');row.setAttribute('role','option');row.setAttribute('data-convid','thread'+i);
          row.style.cssText='position:absolute;left:0;right:0;height:30px;top:'+i*30+'px';row.textContent='Thread '+i;
          row.onclick=()=>{document.querySelector('#pane').innerHTML='<article data-convid="thread'+i+'" data-message-id="m'+i+'"><h2 data-testid="conversation-subject">Subject '+i+'</h2><span email="sender@example.test">Sender</span><div role="document">Body '+i+'</div></article>';};
          spacer.append(row);
        }
      }
      list.addEventListener('scroll',render);render();
      </script>`}));
    let cursor,hasMore=true;const imported=new Set();let resumed=false;
    for(let scan=0;scan<18&&hasMore;scan++){
      await page.goto('https://outlook.office.com/mail/inbox');
      const result=await operate({page,provider:'browser_outlook',lastInboxRefresh:Date.now()}, {command:'sync',connection:{email:'owner@example.test'},cursor});
      for(const message of result.messages)imported.add(message.message_id);
      cursor=result.cursor;hasMore=result.has_more;
      resumed ||= JSON.parse(cursor).walks.Focused.top>0;
    }
    assert.ok(resumed,'the fixture must exceed one bounded collection');
    assert.equal(imported.size,90,'older rows must remain reachable after repeated restart');
    assert.equal(hasMore,false);
  }finally{await browser.close();}
});


test('conversation cursor imports every visible message beyond a 20-message batch', {timeout:60000}, async()=>{
  const browser=await chromium.launch({executablePath:findInstalledBrowser().executable,headless:true});
  try{
    const page=await browser.newPage();
    const pane='<h2 data-testid="conversation-subject">Long conversation</h2>'+Array.from({length:23},(_,i)=>`<article data-convid="thread" data-message-id="m${i}"><span email="sender@example.test">Sender</span><div role="document">Body ${i}</div></article>`).join('');
    await page.route('**/*',route=>route.fulfill({contentType:'text/html',body:'<button id="mectrl_main_trigger" aria-label="owner@example.test">Account</button><main role="main"><div role="option" data-convid="thread" onclick=\'document.querySelector("#pane").innerHTML='+JSON.stringify(pane)+'\'>Thread</div><section id="pane"></section></main>'}));
    const imported=new Set();let cursor,hasMore=true;
    for(let scan=0;scan<4&&hasMore;scan++){
      await page.goto('https://outlook.office.com/mail/inbox');
      const result=await operate({page,provider:'browser_outlook',lastInboxRefresh:Date.now()},{command:'sync',connection:{email:'owner@example.test'},cursor});
      result.messages.forEach(message=>imported.add(message.message_id));cursor=result.cursor;hasMore=result.has_more;
      if(scan===0)assert.equal(hasMore,true);
    }
    assert.equal(imported.size,23);assert.equal(hasMore,false);
    assert.deepEqual(JSON.parse(cursor).message_offsets,{});
  }finally{await browser.close();}
});
