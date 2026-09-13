'use strict';
const {chromium} = require('playwright-core');
const {findInstalledBrowser} = require('./external-browser');
const readline = require('node:readline');
const {createHash} = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const sessions = new Map();
const operations = new Map();
const REPLY_CONTRACT = 'browser-reply/v1';
const SYNC_CONTRACT = 'browser-sync/v3';
const digest = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');
const contentHash = message => digest([message.sender,message.subject,message.body,message.message_id || '']);
const sourceHash = (provider,row,message) => createHash('sha256').update(message.message_id ? provider+'|'+message.message_id : row.id+'|'+message.sender+'|'+message.subject+'|'+message.body).digest('hex');
const rowSelector = (provider,attr,id) => `${provider==='browser_gmail'?'tr':'[role="option"]'}[${attr}=${JSON.stringify(id)}]`;
const urls = {browser_outlook:'https://outlook.office.com/mail/inbox', browser_gmail:'https://mail.google.com/mail/u/0/#inbox'};
function safeRowWarning(error) {
  const clean=String(error?.message||error||'').replace(/\x1b\[[0-?]*[ -/]*[@-~]/g,'');
  const first=clean.split('\n')[0].replace(/^page\.evaluate: (?:Error: )?/,'').trim();
  const known=new Set([
    'The bound original message is not uniquely ready.',
    'This mailbox layout cannot be read reliably: the latest message sender cannot be identified.',
    'This message exceeds the complete browser import size limit. Read it in your mailbox; no partial draft is generated.',
    'The latest message sender cannot be identified.',
    'The latest message sender profile button could not be opened.',
    'The latest message sender profile card did not open.',
    'The sender profile contains multiple email addresses. This message needs manual review.',
    'No unambiguous sender address was available in the message sender profile.',
    'The selected message did not finish loading.',
    'An inbox conversation moved or is no longer visible; it will be checked again on a later scan.',
    'The inbox conversation is ambiguous.'
  ]);
  const safe=known.has(first)||/^This mailbox layout cannot be read reliably: message (?:body|sender|subject)(?:, (?:body|sender|subject))* is not ready\.$/.test(first);
  return 'A message needs another check: '+(safe?first:'The mailbox page did not finish loading a message. Reopen the inbox and try checking again.');
}

// These selectors deliberately inspect only rendered mailbox UI, never browser
// cookies, application internals, network interception or another user's profile.
function readIdentity(provider) {
  const selectors = provider === 'browser_gmail'
    ? ['a[aria-label*="Google Account"]', 'button[aria-label*="Google Account"]']
    : ['[id^="primaryMailboxRoot_"][role="treeitem"]', '#mectrl_currentAccount_secondary', '#mectrl_currentAccount_primary', '#mectrl_headerPicture', '#mectrl_main_trigger', '[data-testid="account-manager-button"]'];
  for (const selector of selectors) {
    const element = document.querySelector(selector);
    if (!element) continue;
    const text = [element.getAttribute('title'), element.getAttribute('data-folder-name'), element.getAttribute('aria-label'), element.innerText].filter(Boolean).join(' ');
    const email = text.match(/[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i)?.[0];
    if (email) return {email, name: email};
  }
  return {email:'', name:''};
}
function readRows(options) {
  const provider=typeof options==='string'?options:options.provider;
  const selector = provider === 'browser_gmail' ? 'tr[data-legacy-thread-id], tr[data-thread-id]' : '[role="option"][data-convid], [role="option"][data-itemid], [role="option"][data-id]';
  return Array.from(document.querySelectorAll(selector)).filter(e => e.getClientRects().length).slice(0, 50).map(e => {
    const attr = ['data-legacy-thread-id','data-thread-id','data-convid','data-itemid','data-id'].find(a => e.hasAttribute(a));
    return {id:e.getAttribute(attr), attr,...(options.signatures?{summary:[e.innerText,e.getAttribute('aria-label'),...Array.from(e.querySelectorAll('[title],[aria-label],time,[data-message-count]')).map(node=>[node.getAttribute('title'),node.getAttribute('aria-label'),node.getAttribute('datetime'),node.getAttribute('data-message-count')])]}:{})};
  }).filter(e => e.id);
}
function readMessage(options) {
  const provider=typeof options==='string' ? options : options.provider;
  const gmail = provider === 'browser_gmail';
  const bodySelector = gmail ? '.a3s' : '[role="document"], [aria-label="Message body"]';
  const senderSelector = gmail ? '.gD[email]' : '[id^="MSG_"][id$="_FROM"], [data-testid="SenderPersona"] [title*="@"], [email], [smtp]';
  const meaningfulBody=e=>e.innerText?.trim() && getComputedStyle(e).visibility!=='hidden' && Array.from(e.getClientRects()).some(rect=>rect.width>0&&rect.height>0);
  const bodies = Array.from(document.querySelectorAll(bodySelector)).filter(meaningfulBody).filter(body=>{
    const scope=gmail?body.closest('[data-legacy-message-id], [data-message-id]'):body.closest('[aria-label="Email message"]');
    if(!gmail&&scope){
      const copy=scope.cloneNode(true);copy.querySelectorAll(bodySelector).forEach(node=>node.remove());
      if(/This message hasn['’]t been sent\./.test(copy.textContent))return false;
    }
    if(!options.expectedMessageId)return true;
    const idNode=body.closest('[data-legacy-message-id], [data-message-id], [data-item-id]');
    const id=idNode?.getAttribute('data-legacy-message-id')||idNode?.getAttribute('data-message-id')||idNode?.getAttribute('data-item-id')||scope?.querySelector('[id^="MSG_"][id$="_FROM"]')?.id.slice(4,-5);
    return id===options.expectedMessageId;
  });
  if(options.expectedMessageId&&bodies.length!==1)throw new Error('The bound original message is not uniquely ready.');
  const element = bodies.at(-1);
  let envelope=(!gmail && element?.closest('[aria-label="Email message"]')) || element?.parentElement;
  while (envelope && envelope !== document.body && !envelope.querySelector(senderSelector)) envelope=envelope.parentElement;
  // Never associate a sender from another message with the latest body.
  if (!envelope || envelope===document.body || Array.from(envelope.querySelectorAll(bodySelector)).filter(meaningfulBody).length!==1) throw new Error('This mailbox layout cannot be read reliably: the latest message sender cannot be identified.');
  const senderNode=envelope.querySelector(senderSelector);
  const raw=senderNode.getAttribute('email') || senderNode.getAttribute('smtp') || senderNode.getAttribute('title') || senderNode.innerText?.slice(0,2000) || '';
  const parsedSender=raw.match(/[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}/i)?.[0];
  const sender=parsedSender || (options.senderNodeId===senderNode.id && options.senderOverride);
  const body=element.innerText.trim();
  const subjectNode=gmail ? null : envelope.querySelector('[id^="MSG_"][id$="_SUBJECT"]');
  const subjectReference=subjectNode?.getAttribute('aria-labelledby')?.split(/\s+/).map(id=>document.getElementById(id)?.innerText || '').join(' ').trim();
  const conversationSubject=document.querySelector(gmail ? 'h2.hP' : '[id^="CONV_"][id$="_SUBJECT"], [data-testid="conversation-subject"]');
  const subject=subjectReference || subjectNode?.innerText?.trim() || conversationSubject?.innerText?.trim() || '';
  const idNode=element.closest('[data-legacy-message-id], [data-message-id], [data-item-id]');
  const message_id=(idNode && (idNode.getAttribute('data-legacy-message-id') || idNode.getAttribute('data-message-id') || idNode.getAttribute('data-item-id'))) || (!gmail && /^MSG_.+_FROM$/.test(senderNode.id) && senderNode.id.slice(4,-5));
  if (!body || !sender || (!subjectNode&&!conversationSubject)) throw new Error('This mailbox layout cannot be read reliably: message '+[!body && 'body',!sender && 'sender',(!subjectNode&&!conversationSubject) && 'subject'].filter(Boolean).join(', ')+' is not ready.');
  if (body.length>100000 || subject.length>1000) throw new Error('This message exceeds the complete browser import size limit. Read it in your mailbox; no partial draft is generated.');
  return {sender,subject,body,message_id:message_id || ''};
}
async function resolveSenderCard(page,expectedMessageId='') {
  const senderNodeId=await page.evaluate(expected=>Array.from(document.querySelectorAll('[role="document"], [aria-label="Message body"]'))
    .filter(e=>e.innerText?.trim()&&getComputedStyle(e).visibility!=='hidden'&&Array.from(e.getClientRects()).some(rect=>rect.width>0&&rect.height>0))
    .map(e=>e.closest('[aria-label="Email message"]')).filter(scope=>{if(!scope)return false;const copy=scope.cloneNode(true);copy.querySelectorAll('[role="document"], [aria-label="Message body"]').forEach(node=>node.remove());return !/This message hasn['’]t been sent\./.test(copy.textContent);})
    .map(scope=>scope.querySelector('[id^="MSG_"][id$="_FROM"]')?.id).filter(id=>id&&(!expected||id.slice(4,-5)===expected)).at(-1),expectedMessageId);
  if (!senderNodeId) throw new Error('The latest message sender cannot be identified.');
  const sender=page.locator(`[id=${JSON.stringify(senderNodeId)}]`);
  try {
    await sender.locator('[role="button"]').first().click({timeout:5000}).catch(()=>{
      throw new Error('The latest message sender profile button could not be opened.');
    });
    const card=page.locator(':is([role="dialog"][data-log-region="LivePersonaCard"], [role="dialog"][aria-label="Profile Card"]):not([role="document"] *):not([aria-label="Message body"] *)').first();
    // LPC-CARD can be a zero-size custom-element host with visible descendants.
    // Attachment locates the host; visibility is checked on address leaves.
    await card.waitFor({state:'attached',timeout:10000}).catch(()=>{
      throw new Error('The latest message sender profile card did not open.');
    });
    const deadline=Date.now()+8000;
    while (Date.now()<deadline) {
      const addresses=await card.evaluate(element=>{
        const values=Array.from(element.querySelectorAll('span[title], a[href^="mailto:"], span, p, div')).filter(node=>{
          const style=getComputedStyle(node);
          return style.visibility!=='hidden' && style.display!=='none' && Array.from(node.getClientRects()).some(rect=>rect.width>0 && rect.height>0);
        }).flatMap(node=>{
          // The newer Profile Card exposes contact addresses as text leaves.
          // Require a complete address, never scan paragraphs or mail bodies.
          const value=node.getAttribute('href')?.replace(/^mailto:/i,'').split('?')[0] || node.getAttribute('title') || (!node.children.length && node.innerText) || '';
          const email=value.trim();
          return /^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}$/i.test(email) ? [email.toLowerCase()] : [];
        });
        return [...new Set(values)];
      });
      if (addresses.length>1) throw new Error('The sender profile contains multiple email addresses. This message needs manual review.');
      if (addresses.length===1) return {senderNodeId,senderOverride:addresses[0]};
      await new Promise(resolve=>setTimeout(resolve,200));
    }
    throw new Error('No unambiguous sender address was available in the message sender profile.');
  } finally { await page.keyboard.press('Escape').catch(()=>{}); }
}
function inboxState(options) {
  const provider=typeof options==='string' ? options : options.provider;
  const gmail=provider==='browser_gmail';
  const rows=document.querySelectorAll(gmail ? 'tr[data-legacy-thread-id], tr[data-thread-id]' : '[role="option"][data-convid], [role="option"][data-itemid], [role="option"][data-id]');
  if (Array.from(rows).some(e=>e.getClientRects().length)) return 'rows';
  if(!gmail){
    const emptyList=document.querySelector('#MailList #EmptyState_MainMessage');
    if(emptyList&&!emptyList.closest('[role="document"], [aria-label="Message body"], [contenteditable="true"]')&&getComputedStyle(emptyList).visibility!=='hidden'&&[...emptyList.getClientRects()].some(rect=>rect.width>0&&rect.height>0)&&emptyList.innerText.trim()==='Nothing left to read')return 'empty';
  }
  const candidates=document.querySelectorAll(gmail ? '[role="main"] td, [role="main"] [role="status"]' : '[role="main"] [role="status"], [data-testid="empty-folder"], [role="main"] h2');
  const empty=/^(No new mail!|Your inbox is empty\.?|No conversations in Inbox\.?|Nothing in your inbox|You're all caught up!?|All done for the day)$/i;
  return Array.from(candidates).some(e=>e.getClientRects().length && empty.test(e.innerText.trim())) ? 'empty' : (options.wait ? false : 'unsupported');
}
async function waitForInbox(page,provider) {
  await page.waitForFunction(inboxState,{provider,wait:true},{timeout:10000,polling:250});
  return page.evaluate(inboxState,provider);
}
async function session(request) {
  const {connection:c,profile} = request;
  if (!c || !urls[c.provider] || !/^[a-f0-9]{32}$/.test(c.id)) throw new Error('Invalid browser mail connection.');
  if (!profile || !['headed','headless'].includes(c.browser_mode || 'headed')) throw new Error('Invalid browser mail mode or profile.');
  let mode=request.command==='open' ? 'headed' : (c.browser_mode || 'headed');
  let value = sessions.get(c.id);
  if (value && (value.profile!==path.resolve(profile) || value.provider!==c.provider)) throw new Error('The browser profile ownership changed. Close this connection before reconnecting.');
  if (value && value.page.isClosed()) { await value.context.close().catch(()=>{}); sessions.delete(c.id); value=null; }
  if(value && value.mode==='headed' && mode==='headless' && value.explicitSignIn) {
    // A background poll must not close an explicitly opened login/MFA flow.
    // Only the rendered identity on a trusted mailbox origin completes it.
    const current=new URL(value.page.url());
    const hosts=value.provider==='browser_gmail'?['mail.google.com']:['outlook.office.com','outlook.office365.com','outlook.live.com','outlook.cloud.microsoft'];
    const identity=current.protocol==='https:'&&hosts.includes(current.hostname)?await value.page.evaluate(readIdentity,value.provider):{};
    if(!identity.email || (c.email&&identity.email.toLowerCase()!==c.email.toLowerCase()))mode='headed';
    else value.explicitSignIn=false;
  }
  if (value && value.mode!==mode) { if(await hasComposer(value.page))throw new Error('Close or finish the existing reply composer before switching browser mode.');await value.context.close(); sessions.delete(c.id); value=null; }
  if (!value) {
    const browser = findInstalledBrowser(['edge','chrome']);
    if (!browser) throw new Error('Install Microsoft Edge or Google Chrome to connect browser mail.');
    const context = await chromium.launchPersistentContext(profile, {executablePath:browser.executable, chromiumSandbox:true, headless:mode==='headless', viewport:mode==='headless'?{width:1440,height:1000}:null, acceptDownloads:false, timeout:30000});
    const page = context.pages()[0] || await context.newPage();
    page.setDefaultTimeout(8000);
    value = {context,page,provider:c.provider,profile:path.resolve(profile),mode,explicitSignIn:request.command==='open'};
    sessions.set(c.id,value);
    await page.goto(urls[c.provider], {waitUntil:'domcontentloaded',timeout:30000});
  }
  if(request.command==='open')value.explicitSignIn=true;
  return value;
}
async function status(value, connection) {
  const mailboxHosts=value.provider==='browser_gmail' ? ['mail.google.com'] : ['outlook.office.com','outlook.office365.com','outlook.live.com','outlook.cloud.microsoft'];
  const authHosts=value.provider==='browser_gmail' ? ['accounts.google.com'] : ['login.microsoftonline.com','login.live.com','login.microsoft.com'];
  let host = new URL(value.page.url()).hostname;
  if (authHosts.includes(host) && !value.explicitSignIn) {
    // A saved session can briefly cross the provider's sign-in origin before
    // returning to mail. Wait only for known mailbox hosts; never follow a
    // supplied URL or infer authentication from the redirect alone.
    await value.page.waitForURL(url=>url.protocol==='https:' && mailboxHosts.includes(url.hostname),
      {timeout:15000,waitUntil:'domcontentloaded'}).catch(()=>{});
    host=new URL(value.page.url()).hostname;
  }
  const trusted = new URL(value.page.url()).protocol==='https:' && mailboxHosts.includes(host);
  if (trusted) {
    // The mailbox shell hydrates after DOMContentLoaded. Do not mistake that
    // short interval for expired authentication.
    await value.page.waitForFunction(provider => {
      const selectors=provider==='browser_gmail'
        ? 'a[aria-label*="Google Account"], button[aria-label*="Google Account"]'
        : '[id^="primaryMailboxRoot_"][role="treeitem"], #mectrl_currentAccount_secondary, #mectrl_main_trigger';
      return Array.from(document.querySelectorAll(selectors)).some(element => /[^\s@]+@[^\s@]+\.[^\s@]+/.test([
        element.getAttribute('title'),element.getAttribute('data-folder-name'),
        element.getAttribute('aria-label'),element.innerText].filter(Boolean).join(' ')));
    },value.provider,{timeout:10000,polling:200}).catch(()=>{});
  }
  const identity = trusted ? await value.page.evaluate(readIdentity, value.provider) : {email:'',name:''};
  if (identity.email && connection.email && identity.email.toLowerCase() !== connection.email.toLowerCase()) throw new Error('The browser is signed into a different mailbox. Create a new connection.');
  return {...identity,actual_browser_mode:value.mode || 'headed',state:identity.email ? 'connected' : 'sign_in_required',message:identity.email ? 'Browser connected. Keep Nexus running to check new mail.' : 'Sign in in the Nexus mail browser. If already signed in, open the account menu so Nexus can identify your mailbox.'};
}
async function handle(request) {
  if (!['open','status','sync','send'].includes(request.command)) throw new Error('Unknown browser mail command.');
  const key=request.connection?.id;
  const previous=operations.get(key) || Promise.resolve();
  const operation=previous.catch(()=>{}).then(async()=>{
    let value;
    try{value=await session(request);}catch(error){if(request.command==='send')return {status:'not_sent',error:String(error.message||error).split('\n')[0].slice(0,400),submission_id:request.submission_id};throw error;}
    return operate(value,request);
  });
  operations.set(key,operation);
  try { return await operation; } finally { if(operations.get(key)===operation) operations.delete(key); }
}
async function operate(value,request) {
  if (request.command==='send') return sendReviewed(value,request);
  if(request.command==='sync'&&!request._singleTab){
    // Both tabs share one work budget; the bridge waits 150 seconds, including
    // startup and a final in-flight bounded browser operation.
    const scanDeadline=Date.now()+60000;
    const first=await operate(value,{...request,_singleTab:true,_scanDeadline:scanDeadline});
    const firstCursor=JSON.parse(first.cursor||'{}');
    if(!firstCursor.split_inbox)return first;
    if(Date.now()>=scanDeadline-15000)return {...first,warnings:[...first.warnings,'The scan work budget was reached. The other inbox tab will be checked on the next scan.']};
    let second;
    try{second=await operate(value,{...request,_singleTab:true,_scanDeadline:scanDeadline,cursor:first.cursor});}
    catch(error){
      // Recover only provider layout/loading failures, never a changed mailbox,
      // lost authentication, closed browser or unrelated programming exception.
      const identity=await status(value,request.connection);
      if(identity.state!=='connected'||!/Timeout|layout|not ready|needs another check|did not finish|inbox conversation moved/i.test(String(error.message||error)))throw error;
      firstCursor.next_tab=firstCursor.next_tab==='Other'?'Focused':'Other';
      return {...first,cursor:JSON.stringify(firstCursor),warnings:[...first.warnings,'The other inbox tab did not finish loading. Imported messages were preserved; that tab will be retried on the next scan.']};
    }
    return {messages:[...first.messages,...second.messages].filter((message,index,all)=>all.findIndex(other=>other.source_id===message.source_id)===index),cursor:second.cursor,warnings:[...new Set([...first.warnings,...second.warnings])]};
  }
  if (request.command === 'open') await value.page.bringToFront();
  const result = await status(value,request.connection);
  if (request.command !== 'sync') return result;
  if (result.state !== 'connected') throw new Error('Browser sign-in expired or mailbox identity is unavailable. Reconnect the browser.');
  if(await hasComposer(value.page))throw new Error('An existing reply composer is open. Inbox checking will resume after it is finished or closed.');
  // Navigate only the dedicated Nexus page, never an existing user tab.
  const current = new URL(value.page.url());
  const inbox = value.provider === 'browser_gmail' ? current.origin + current.pathname + '#inbox' : current.origin + '/mail/inbox';
  const canonicalInbox=value.provider==='browser_gmail'?current.hash==='#inbox':current.pathname.replace(/\/$/,'')==='/mail/inbox';
  if(!value.lastInboxRefresh||Date.now()-value.lastInboxRefresh>=30000||!canonicalInbox||await value.page.evaluate(inboxState,value.provider)==='unsupported'){
    await value.page.goto(inbox,{waitUntil:'domcontentloaded',timeout:20000});value.lastInboxRefresh=Date.now();value.lastOpenedRow='';
  }
  const inboxStatus = await waitForInbox(value.page,value.provider).catch(()=>{throw new Error('This inbox layout is unsupported or still loading. Reconnect after opening the inbox.');});
  const refreshed = await status(value,request.connection);
  if (refreshed.state !== 'connected') throw new Error('Mailbox sign-in changed while checking mail. Reconnect the browser.');
  let cursor={seen:[],offset:0};
  try { cursor=JSON.parse(request.cursor || '{}'); }  catch { throw new Error('Browser mail cursor is invalid; reconnect the account.'); }
  if (cursor.contract!==SYNC_CONTRACT) cursor={seen:[],offset:0};
  const cacheBinding=digest(['browser-row-cache/v1',value.provider,refreshed.email.toLowerCase()]);
  const cacheValid=cursor.row_cache_contract==='browser-row-cache/v1'&&cursor.row_cache_binding===cacheBinding;
  const rowCache=cacheValid&&cursor.row_cache&&typeof cursor.row_cache==='object'&&!Array.isArray(cursor.row_cache)?cursor.row_cache:{};
  for(const key of Object.keys(rowCache)){const entry=rowCache[key];if(!/^[a-f0-9]{64}$/.test(key)||!entry||!/^[a-f0-9]{64}$/.test(entry.signature)||!Number.isFinite(entry.checked_at)||entry.checked_at>Date.now())delete rowCache[key];}
  if(!cacheValid&&value.lastScanCacheBinding){await value.page.goto(inbox,{waitUntil:'domcontentloaded',timeout:20000});await waitForInbox(value.page,value.provider);value.lastInboxRefresh=Date.now();value.lastOpenedRow='';}
  value.lastScanCacheBinding=cacheBinding;
  const tabName=cursor.next_tab==='Other'?'Other':'Focused';let splitInbox=false;
  const selectInboxTab=async()=>{
    if(value.provider!=='browser_outlook')return;
    const focused=value.page.getByRole('tab',{name:/^Focused(?:\s+\d+)?$/});
    const other=value.page.getByRole('tab',{name:/^Other(?:\s+\d+)?$/});
    if(await focused.count()!==1||await other.count()!==1)return;
    splitInbox=true;
    const target=tabName==='Other'?other:focused;
    if(await target.getAttribute('aria-selected')!=='true'){
      await target.click({timeout:2000});
      await value.page.waitForFunction(name=>[...document.querySelectorAll('[role="tab"]')].some(tab=>(tab.getAttribute('aria-label')||tab.innerText).trim().replace(/\s+\d+$/,'')===name&&tab.getAttribute('aria-selected')==='true'),tabName,{timeout:3000});
      await waitForInbox(value.page,value.provider);
    }
  };
  await selectInboxTab();
  const snapshotRows=async()=> (await value.page.evaluate(readRows,{provider:value.provider,signatures:true})).map(({summary,...row})=>({...row,signature:digest(summary)}));
  const rows = await snapshotRows();
  const nextTab=splitInbox?(tabName==='Focused'?'Other':'Focused'):undefined;
  const cacheFields=()=>({row_cache_contract:'browser-row-cache/v1',row_cache_binding:cacheBinding,row_cache:Object.fromEntries(Object.entries(rowCache).sort((a,b)=>b[1].checked_at-a[1].checked_at).slice(0,200)),split_inbox:splitInbox,next_tab:nextTab});
  if (!rows.length && (inboxStatus==='empty'||await value.page.evaluate(inboxState,value.provider)==='empty')) return {messages:[],cursor:JSON.stringify({...cursor,contract:SYNC_CONTRACT,...cacheFields()}),warnings:[]};
  const seen=cursor.seen || [];
  if (!Array.isArray(seen) || seen.some(id=>typeof id!=='string') || seen.length>5000) throw new Error('Browser mail cursor is invalid.');
  const messages=[];
  const rowWarnings=[];
  let parsedCount=0;
  const started=Date.now();
  const workDeadline=request._scanDeadline||started+60000;
  let checked=0;
  const offset=Number.isInteger(cursor.offset) ? Math.max(0,cursor.offset)%rows.length : 0;
  const rowKey=row=>digest([tabName,row.attr,row.id]);
  const changed=row=>!rowCache[rowKey(row)]||rowCache[rowKey(row)].signature!==row.signature;
  const due=row=>changed(row)||Date.now()-rowCache[rowKey(row)].checked_at>=30000;
  const changedRows=rows.filter(changed);
  const reconciliationRow=[...rows.slice(offset),...rows.slice(0,offset)].find(row=>!changed(row)&&due(row));
  const ordered=[...changedRows,...(reconciliationRow?[reconciliationRow]:[])];
  let nextOffset=offset;
  const attempted=new Set();
  const refreshInbox=async(force=false)=>{
    if(await hasComposer(value.page))throw new Error('An existing reply composer is open. Inbox checking will resume after it is finished or closed.');
    const location=new URL(value.page.url());
    const stillInbox=value.provider==='browser_gmail'?location.hash==='#inbox':location.pathname.replace(/\/$/,'')==='/mail/inbox';
    if(force||!stillInbox||await value.page.evaluate(inboxState,value.provider)==='unsupported'){
      await value.page.goto(inbox,{waitUntil:'domcontentloaded',timeout:15000});value.lastInboxRefresh=Date.now();value.lastOpenedRow='';
    }
    await waitForInbox(value.page,value.provider);
    await selectInboxTab();
    const identity=await status(value,request.connection);
    if(identity.state!=='connected')throw new Error('Mailbox sign-in changed while checking mail. Reconnect the browser.');
  };
  for (const row of ordered) {
    if (checked>=10 || Date.now()>=workDeadline-15000) break;
    attempted.add(row.id);
    if(reconciliationRow&&row.id===reconciliationRow.id)nextOffset=(rows.findIndex(item=>item.id===row.id)+1)%rows.length;
    const warningCount=rowWarnings.length,parsedBefore=parsedCount;
    const selector = rowSelector(value.provider,row.attr,row.id);
    try {
    if(value.lastOpenedRow===row.id)await refreshInbox(true);
    const previous=await value.page.locator(value.provider==='browser_gmail' ? '.a3s' : '[role="document"], [aria-label="Message body"]').allTextContents();
    // Outlook virtualizes and reorders its list while a scan is in progress.
    // Reacquire the exact row with a short bounded retry, never a positional click.
    for(let attempt=0;attempt<2;attempt++){
      try{
        const target=value.page.locator(selector);
        await target.waitFor({state:'visible',timeout:1500});
        if((await target.count())!==1)throw new Error('The inbox conversation is ambiguous.');
        await target.click({timeout:2000});
        value.lastOpenedRow=row.id;
        break;
      }catch(error){
        if(attempt===1)throw new Error('An inbox conversation moved or is no longer visible; it will be checked again on a later scan.');
        await refreshInbox(true);
      }
    }
    await value.page.waitForFunction(({provider,previous,row})=>{
      const selector=provider==='browser_gmail' ? '.a3s' : '[role="document"], [aria-label="Message body"]';
      const bodies=Array.from(document.querySelectorAll(selector)).filter(e=>e.innerText?.trim()&&getComputedStyle(e).visibility!=='hidden'&&Array.from(e.getClientRects()).some(rect=>rect.width>0&&rect.height>0));
      if (!bodies.length) return false;
      const latest=bodies.at(-1);
      if (!latest.innerText.trim()) return false;
      const binding=latest.closest('[data-convid], [data-thread-id], [data-legacy-thread-id]');
      return (binding && [binding.getAttribute('data-convid'),binding.getAttribute('data-thread-id'),binding.getAttribute('data-legacy-thread-id')].includes(row.id)) || JSON.stringify(bodies.map(e=>e.textContent))!==JSON.stringify(previous);
    },{provider:value.provider,previous,row},{timeout:10000,polling:200});
    const visibleIds=await value.page.evaluate(provider=>{
      const gmail=provider==='browser_gmail',selector=gmail?'.a3s':'[role="document"], [aria-label="Message body"]';
      return [...new Set([...document.querySelectorAll(selector)].filter(body=>body.innerText?.trim()&&getComputedStyle(body).visibility!=='hidden'&&[...body.getClientRects()].some(rect=>rect.width>0&&rect.height>0)).flatMap(body=>{
        const scope=body.closest('[aria-label="Email message"]');
        if(!gmail&&scope){const copy=scope.cloneNode(true);copy.querySelectorAll(selector).forEach(node=>node.remove());if(/This message hasn['’]t been sent\./.test(copy.textContent))return [];}
        const node=body.closest('[data-legacy-message-id], [data-message-id], [data-item-id]');
        const id=node?.getAttribute('data-legacy-message-id')||node?.getAttribute('data-message-id')||node?.getAttribute('data-item-id')||scope?.querySelector('[id^="MSG_"][id$="_FROM"]')?.id.slice(4,-5);
        return id?[id]:[];
      }))];
    },value.provider);
    if(visibleIds.length>20)rowWarnings.push('This conversation has more than 20 visible messages; only its latest 20 can be checked in one scan.');
    for(const expectedMessageId of (visibleIds.length?visibleIds.slice(-20):[''])){
    if(Date.now()>=workDeadline-15000){rowWarnings.push('The scan time limit was reached; remaining messages will be checked on a later scan.');break;}
    try{
    // Outlook inserts the body, sender and subject in separate render passes.
    // Wait for the complete bounded extraction after the selected pane changes.
    let message,lastReadError,senderOverride;
    const readStarted=Date.now();
    const readDeadline=readStarted+10000;
    while (Date.now()<readDeadline) {
      try {
        message=await value.page.evaluate(readMessage,{provider:value.provider,expectedMessageId,...senderOverride});
        // An existing empty heading is valid mail, but give staged hydration a
        // short opportunity to populate it before accepting a blank subject.
        if(message.subject===''&&Date.now()-readStarted<600){message=null;await new Promise(resolve=>setTimeout(resolve,150));continue;}
        break;
      }
      catch(error) {
        lastReadError=error;
        if (value.provider==='browser_outlook' && !senderOverride && Date.now()-readStarted>=3000 && /message sender is not ready/.test(error.message)) {
          senderOverride=await resolveSenderCard(value.page,expectedMessageId);
          // Card discovery may outlast the normal body hydration deadline.
          message=await value.page.evaluate(readMessage,{provider:value.provider,expectedMessageId,...senderOverride});
          break;
        } else await new Promise(resolve=>setTimeout(resolve,200));
      }
    }
    if (!message) throw lastReadError || new Error('The selected message did not finish loading.');
    parsedCount++;
    const source_id=sourceHash(value.provider,row,message);
    const browser_reference={contract:REPLY_CONTRACT,provider:value.provider,row_attr:row.attr,row_id:row.id,message_id:message.message_id,source_hash:source_id,content_hash:contentHash(message),...(splitInbox?{inbox_tab:tabName}:{})};
    if (!seen.includes(source_id)) { if(message.sender.toLowerCase()!==refreshed.email.toLowerCase()) messages.push({source_id,...message,browser_reference}); seen.push(source_id); }
    }catch(error){
      const identity=await status(value,request.connection);
      if(identity.state!=='connected')throw new Error('Mailbox sign-in changed while checking mail. Reconnect the browser.');
      rowWarnings.push(safeRowWarning(error));
    }
    }
    } catch(error) {
      // Message layout/virtualization failures are recoverable, but never hide
      // a closed browser, a changed account or a sign-in redirect as bad mail.
      const identity=await status(value,request.connection);
      if(identity.state!=='connected')throw new Error('Mailbox sign-in changed while checking mail. Reconnect the browser.');
      rowWarnings.push(safeRowWarning(error));
    }
    checked++;
    if(rowWarnings.length===warningCount&&parsedCount>parsedBefore)rowCache[rowKey(row)]={signature:row.signature,checked_at:Date.now()};
    else delete rowCache[rowKey(row)];
    try{await refreshInbox();}catch(error){
      const identity=await status(value,request.connection);
      if(identity.state!=='connected'||!parsedCount)throw error;
      rowWarnings.push('Inbox refresh did not finish; imported messages were preserved and remaining conversations will be checked on the next scan.');
      break;
    }
    // Add arrivals/reordered visible rows without retrying the same failed row
    // indefinitely. Unreadable rows are never added to the durable seen set.
    const currentRows=await snapshotRows();
    const additions=currentRows.filter(item=>changed(item)&&!attempted.has(item.id)&&!ordered.some(queued=>queued.id===item.id));
    ordered.splice(ordered.indexOf(row)+1,0,...additions);
  }
  if (!parsedCount && rowWarnings.length) throw new Error(rowWarnings[0]);
  return {messages,cursor:JSON.stringify({contract:SYNC_CONTRACT,seen:seen.slice(-5000),offset:nextOffset,...cacheFields()}),warnings:[...rowWarnings,'Browser mail checks the first 50 visible inbox conversations per tab and reconciles at most one unchanged conversation per tab per scan. Opening mail may mark it read. Website layout changes can interrupt checking.']};
}
const editorSelector = provider => provider==='browser_gmail'
  ? '[contenteditable="true"][role="textbox"][aria-label="Message Body"], .Am.Al.editable[contenteditable="true"][role="textbox"]'
  : '[contenteditable="true"][aria-label="Message body"][role="textbox"], [contenteditable="true"][aria-label="Message body"][role="document"]';
async function visibleElements(locator) { const result=[]; for(const item of await locator.all()) if(await item.isVisible()) result.push(item); return result; }
async function hasComposer(page) {
  return (await visibleElements(page.locator('[contenteditable="true"][role="textbox"], [contenteditable="true"][role="document"], [g_editable="true"]'))).length>0;
}
function acknowledgement() {
  return Array.from(document.querySelectorAll('[role="status"], [role="alert"]'))
    .filter(e=>e.getClientRects().length && !e.closest('[role="document"], .a3s, [contenteditable="true"]'))
    .map(e=>e.innerText.trim()).filter(text=>/^(Message sent\.?|Your message has been sent\.?)$/i.test(text));
}
function readComposer(provider) {
  const visible=e=>e && e.getClientRects().length && getComputedStyle(e).visibility!=='hidden';
  const selector=provider==='browser_gmail'
    ? '[contenteditable="true"][role="textbox"][aria-label="Message Body"], .Am.Al.editable[contenteditable="true"][role="textbox"]'
    : '[contenteditable="true"][aria-label="Message body"][role="textbox"], [contenteditable="true"][aria-label="Message body"][role="document"]';
  const editors=[...document.querySelectorAll(selector)].filter(visible);
  if(editors.length!==1) throw new Error('One unambiguous reply editor is required.');
  const editor=editors[0]; let root=editor.parentElement;
  const isSend=node=>/^(Send|Send \(Ctrl\+Enter\)|Send \(⌘Enter\))$/.test(node.getAttribute('aria-label') || node.innerText.trim());
  while(root && root!==document.body && ![...root.querySelectorAll('button, [role="button"]')].some(node=>visible(node)&&isSend(node)&&!editor.contains(node))) root=root.parentElement;
  if(!root || root===document.body) throw new Error('The reply composer cannot be scoped safely.');
  const send=[...root.querySelectorAll('button, [role="button"]')].filter(node=>visible(node)&&isSend(node)&&!editor.contains(node));
  if(send.length!==1) throw new Error('The reply send control is ambiguous.');
  const extra=root.querySelectorAll('input[name="cc"],input[name="bcc"],input[aria-label="Cc"],input[aria-label="Bcc"],input[aria-label="CC"],input[aria-label="BCC"],[data-recipient-type="cc"],[data-recipient-type="bcc"],[aria-label^="Cc:"],[aria-label^="Bcc:"],[contenteditable="true"][aria-label="Cc"],[contenteditable="true"][aria-label="Bcc"],[contenteditable="true"][aria-label="CC"],[contenteditable="true"][aria-label="BCC"]');
  if([...extra].some(node=>{
    if(node.matches('input'))return !!node.value.trim();
    if(node.matches('[contenteditable="true"]'))return !!node.textContent.trim() || [...node.querySelectorAll('[email],[data-email],[contenteditable="false"][aria-label]')].some(chip=>!!(chip.getAttribute('email')||chip.getAttribute('data-email')||chip.getAttribute('aria-label')||'').trim());
    return !!(node.getAttribute('email')||node.getAttribute('data-email')||node.textContent||node.getAttribute('aria-label')?.replace(/^(Cc|Bcc):\s*/i,'')||'').trim();
  }))throw new Error('CC or BCC recipients require separate review; this reply will not be sent.');
  const recipients=[...root.querySelectorAll('span[email], [data-email], input[name="to"], [aria-label^="To:"], [contenteditable="true"][aria-label="To"] [contenteditable="false"][aria-label]')]
    .filter(node=>(visible(node)||node.matches('input[name="to"]'))&&!editor.contains(node)&&!node.closest('.a3s, [role="document"]:not([contenteditable="true"])'))
    .flatMap(node=>(node.getAttribute('email')||node.getAttribute('data-email')||node.value||node.getAttribute('aria-label')||'').match(/[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}/ig)||[]);
  const unique=[...new Set(recipients.map(s=>s.toLowerCase()))];
  if(unique.length!==1) throw new Error('The reply must show exactly one verifiable recipient.');
  // Outlook's proofing editor renders one DIV per logical line. Chromium's
  // aggregate innerText can count an empty DIV/BR twice; preserve its single
  // logical blank line without collapsing any whitespace in actual text.
  const blocks=[...editor.childNodes];
  const proofLines=provider==='browser_outlook' && blocks.length && blocks.every(node=>node.nodeType===1&&node.matches('div.elementToProof'));
  const body=proofLines?blocks.map(node=>node.childNodes.length===1&&node.firstChild.nodeName==='BR'?'':node.innerText).join('\n'):editor.innerText;
  return {recipient:unique[0],body:body.replace(/\r\n/g,'\n')};
}
function saveSubmission(file,record,exclusive=false) {
  const data=JSON.stringify(record);
  if(exclusive) { const fd=fs.openSync(file,'wx',0o600); try {fs.writeFileSync(fd,data);fs.fsyncSync(fd);} finally{fs.closeSync(fd);} }
  else { const temp=file+'.tmp'; const fd=fs.openSync(temp,'w',0o600); try{fs.writeFileSync(fd,data);fs.fsyncSync(fd);}finally{fs.closeSync(fd);}fs.renameSync(temp,file); }
}
async function sendReviewed(value,request) {
  let dispatched=false,record,file;
  try {
    const incoming=request.incoming,ref=incoming?.browser_reference;
    if(!/^[a-f0-9]{32}$/.test(request.submission_id || '') || typeof request.body!=='string' || !request.body.trim() || request.body.length>200000) throw new Error('A valid approved reply and submission identity are required.');
    if(!value.profile || !request.connection?.email) throw new Error('A verified private mailbox profile is required for sending.');
    if(!ref || ref.contract!==REPLY_CONTRACT || ref.provider!==value.provider || !['data-legacy-thread-id','data-thread-id','data-convid','data-itemid','data-id'].includes(ref.row_attr) || typeof ref.row_id!=='string' || !ref.row_id || ref.row_id.length>2000 || ref.source_hash!==incoming.source_id || !/^[a-f0-9]{64}$/.test(ref.content_hash || '')) throw new Error('This message needs a fresh inbox scan before browser replying.');
    const binding=digest([REPLY_CONTRACT,value.provider,request.connection.email.toLowerCase(),incoming.source_id,ref.content_hash,request.body]);
    const directory=path.join(value.profile,'nexus-reviewed-submissions');
    file=path.join(directory,request.submission_id+'.json');
    if(fs.existsSync(file)) {
      let previous;
      try{previous=JSON.parse(fs.readFileSync(file,'utf8'));}catch{return {status:'unknown',submission_id:request.submission_id,error:'An existing submission receipt cannot be read. Check Sent mail; this approval will not be dispatched again.'};}
      if(previous.contract!==REPLY_CONTRACT || previous.binding!==binding)return {status:'unknown',submission_id:request.submission_id,error:'An existing submission receipt has a different approval binding. Check Sent mail; this approval will not be dispatched again.'};
      return {status:previous.status==='sent'?'sent':'unknown',submission_id:request.submission_id,evidence:previous.status==='sent'?'persisted_ui_acknowledgement':'persisted_dispatch_intent',error:previous.status==='sent'?'':'This reply already has an uncertain submission. Check the mailbox; it will not be sent again.'};
    }
    const identity=await status(value,request.connection);
    if(identity.state!=='connected') throw new Error('Reconnect this mailbox before sending.');
    if(await hasComposer(value.page)) throw new Error('An existing reply composer is open. Finish or close it before sending this reviewed reply.');
    const current=new URL(value.page.url());
    const inbox=value.provider==='browser_gmail'?current.origin+current.pathname+'#inbox':current.origin+'/mail/inbox';
    await value.page.goto(inbox,{waitUntil:'domcontentloaded',timeout:20000});
    await waitForInbox(value.page,value.provider);
    await status(value,request.connection);
    if(value.provider==='browser_outlook'&&ref.inbox_tab){
      if(!['Focused','Other'].includes(ref.inbox_tab))throw new Error('The original inbox tab reference is unsupported.');
      const tab=value.page.getByRole('tab',{name:ref.inbox_tab==='Other'?/^Other(?:\s+\d+)?$/:/^Focused(?:\s+\d+)?$/});
      if(await tab.count()!==1)throw new Error('The original inbox tab is no longer available. Rescan before replying.');
      if(await tab.getAttribute('aria-selected')!=='true')await tab.click({timeout:2000});
      await waitForInbox(value.page,value.provider);
    }
    const row=value.page.locator(rowSelector(value.provider,ref.row_attr,ref.row_id));
    if((await visibleElements(row)).length!==1) throw new Error('The original conversation is no longer uniquely visible. Rescan the inbox before replying.');
    await row.click();
    let original,senderOverride,cardAttempted=false;
    const readStarted=Date.now();let deadline=readStarted+8000;
    const matchesOriginal=candidate=>candidate && sourceHash(value.provider,{id:ref.row_id},candidate)===incoming.source_id && contentHash(candidate)===ref.content_hash && candidate.sender===incoming.sender && candidate.subject===incoming.subject && candidate.body===incoming.body;
    while(Date.now()<deadline) {
      try {
        const candidate=await value.page.evaluate(readMessage,{provider:value.provider,expectedMessageId:ref.message_id,...senderOverride});
        // A clicked thread can leave the previous readable pane mounted while
        // the intended message hydrates. Readability alone is not selection.
        if(matchesOriginal(candidate)){original=candidate;break;}
      } catch(error) {
        if(value.provider==='browser_outlook'&&!cardAttempted&&Date.now()-readStarted>=1000&&/sender is not ready/.test(String(error.message))) {
          cardAttempted=true;
          try{senderOverride=await resolveSenderCard(value.page,ref.message_id);}catch{}
          // Card lookup may outlast initial hydration. Its result must pass the
          // same identity/content checks on the next read; it never bypasses them.
          deadline=Math.max(deadline,Date.now()+3000);
        }
      }
      await new Promise(resolve=>setTimeout(resolve,150));
    }
    if(!matchesOriginal(original)) throw new Error('The original message content changed or a different message is open. Rescan and review before sending.');
    if(await hasComposer(value.page)) throw new Error('An existing reply composer is open. It will not be overwritten.');
    // Locate Reply only in the latest message envelope, outside the mail body.
    const replyHandle=await value.page.evaluateHandle(({provider,expectedMessageId})=>{
      const gmail=provider==='browser_gmail';
      const bodies=[...document.querySelectorAll(gmail?'.a3s':'[role="document"], [aria-label="Message body"]')].filter(e=>e.innerText?.trim()&&getComputedStyle(e).visibility!=='hidden'&&Array.from(e.getClientRects()).some(rect=>rect.width>0&&rect.height>0)).filter(body=>{
        if(!expectedMessageId)return true;
        const node=body.closest('[data-legacy-message-id], [data-message-id], [data-item-id]');
        const id=node?.getAttribute('data-legacy-message-id')||node?.getAttribute('data-message-id')||node?.getAttribute('data-item-id')||body.closest('[aria-label="Email message"]')?.querySelector('[id^="MSG_"][id$="_FROM"]')?.id.slice(4,-5);
        return id===expectedMessageId;
      });
      if(expectedMessageId&&bodies.length!==1)return null;
      const body=bodies.at(-1);let envelope=(!gmail&&body?.closest('[aria-label="Email message"]'))||body?.parentElement;
      const sender=gmail?'.gD[email]':'[id^="MSG_"][id$="_FROM"], [data-testid="SenderPersona"] [title*="@"], [email], [smtp]';
      while(envelope&&envelope!==document.body&&!envelope.querySelector(sender))envelope=envelope.parentElement;
      if(!envelope||envelope===document.body)return null;
      const candidates=[...envelope.querySelectorAll('button,[role="button"],[role="link"]')].filter(e=>e.getClientRects().length&&!body.contains(e)&&(e.getAttribute('aria-label')||e.innerText.trim())==='Reply');
      return candidates.length===1?candidates[0]:null;
    },{provider:value.provider,expectedMessageId:ref.message_id});
    const reply=replyHandle.asElement();if(!reply)throw new Error('A unique Reply control is not available in the original message.');
    await reply.click();
    await value.page.locator(editorSelector(value.provider)).first().waitFor({state:'visible',timeout:8000});
    const editors=await visibleElements(value.page.locator(editorSelector(value.provider)));
    if(editors.length!==1)throw new Error('A unique new reply editor did not open.');
    const recipient=String(incoming.reply_to || incoming.sender).toLowerCase();
    if(!/^[^\s@<>;,]+@[^\s@<>;,]+$/.test(recipient))throw new Error('The approved reply recipient is not a single mailbox.');
    let composer=await value.page.evaluate(readComposer,value.provider);
    if(composer.recipient!==recipient)throw new Error('The browser Reply-To recipient differs from the reviewed recipient. Review this message in the mailbox.');
    // The editor can appear before its quoted thread and framework state are
    // mounted. Require a quiet, unchanged composer before entering approved text.
    let signature='',stableSince=Date.now(),settled=false;
    const settleDeadline=Date.now()+8000;
    while(Date.now()<settleDeadline){
      composer=await value.page.evaluate(readComposer,value.provider);
      if(composer.recipient!==recipient)throw new Error('The browser Reply-To recipient changed while the composer loaded.');
      const next=JSON.stringify([composer,await editors[0].innerHTML()]);
      if(next!==signature){signature=next;stableSince=Date.now();}
      if(Date.now()-stableSince>=1200){settled=true;break;}
      await new Promise(resolve=>setTimeout(resolve,150));
    }
    if(!settled)throw new Error('The reply editor did not finish loading.');
    const lines=request.body.replace(/\r\n/g,'\n').split('\n');
    if(lines.length>2000)throw new Error('The reviewed reply has too many lines for reliable browser entry.');
    await editors[0].fill('');
    if(lines[0])await editors[0].pressSequentially(lines[0]);
    // Soft line breaks avoid Chromium's block-editor normalization adding extra
    // paragraph breaks. Read-back below must still equal the exact approved text.
    for(const line of lines.slice(1)){await editors[0].press('Shift+Enter');if(line)await editors[0].pressSequentially(line);}
    composer=await value.page.evaluate(readComposer,value.provider);
    if(composer.recipient!==recipient || composer.body!==request.body.replace(/\r\n/g,'\n'))throw new Error('The browser reply does not exactly match the reviewed body and recipient.');
    const finalIdentity=await status(value,request.connection);if(finalIdentity.state!=='connected')throw new Error('Mailbox identity changed before sending.');
    composer=await value.page.evaluate(readComposer,value.provider);
    if(composer.recipient!==recipient || composer.body!==request.body.replace(/\r\n/g,'\n'))throw new Error('Reply contents or recipient changed before sending.');
    const before=await value.page.evaluate(acknowledgement);
    // The persisted intent is flushed before dispatch. Once it exists, crashes
    // and timeouts can never cause an automatic second Send click.
    fs.mkdirSync(directory,{recursive:true,mode:0o700});
    record={contract:REPLY_CONTRACT,binding,status:'unknown',created_at:new Date().toISOString()};
    saveSubmission(file,record,true);
    dispatched=true;
    const send=await value.page.evaluateHandle(provider=>{
      const selector=provider==='browser_gmail'?'[contenteditable="true"][role="textbox"][aria-label="Message Body"],.Am.Al.editable[contenteditable="true"][role="textbox"]':'[contenteditable="true"][aria-label="Message body"]';
      const editor=[...document.querySelectorAll(selector)].find(e=>e.getClientRects().length);let root=editor?.parentElement;
      const candidates=node=>[...node.querySelectorAll('button,[role="button"]')].filter(e=>e.getClientRects().length&&!editor.contains(e)&&/^(Send|Send \(Ctrl\+Enter\)|Send \(⌘Enter\))$/.test(e.getAttribute('aria-label')||e.innerText.trim()));
      while(root&&root!==document.body&&!candidates(root).length)root=root.parentElement;
      return root&&root!==document.body&&candidates(root).length===1?candidates(root)[0]:null;
    },value.provider);
    if(!send.asElement())throw new Error('Send control changed after approval.');
    composer=await value.page.evaluate(readComposer,value.provider);
    if(composer.recipient!==recipient || composer.body!==request.body.replace(/\r\n/g,'\n'))throw new Error('Reply contents or recipient changed at dispatch.');
    await send.asElement().click({timeout:8000});
    await value.page.waitForFunction(({before})=>[...document.querySelectorAll('[role="status"],[role="alert"]')].some(e=>e.getClientRects().length&&!e.closest('[role="document"],.a3s,[contenteditable="true"]')&&/^(Message sent\.?|Your message has been sent\.?)$/i.test(e.innerText.trim())&&!before.includes(e.innerText.trim())),{before},{timeout:8000});
    if((await visibleElements(value.page.locator(editorSelector(value.provider)))).length)throw new Error('The reply editor remained open after the send notice.');
    record.status='sent';record.acknowledged_at=new Date().toISOString();saveSubmission(file,record);
    return {status:'sent',submission_id:request.submission_id,evidence:'ui_acknowledgement',message:'The mailbox UI confirmed sending. Recipient delivery is not confirmed.'};
  } catch(error) {
    return {status:dispatched?'unknown':'not_sent',submission_id:request.submission_id,error:dispatched?'The send outcome is uncertain. Check Sent mail; Nexus will not resend this approval automatically.':String(error.message||error).split('\n')[0].slice(0,400)};
  }
}
async function close() { await Promise.allSettled([...sessions.values()].map(s=>s.context.close())); sessions.clear(); }
if (require.main === module) {
  const lines = readline.createInterface({input:process.stdin});
  let chain=Promise.resolve();
  lines.on('line',line=> { chain=chain.then(async()=> {
    try { process.stdout.write(JSON.stringify({result:await handle(JSON.parse(line))})+'\n'); }
    catch(error) { process.stdout.write(JSON.stringify({error:String(error.message || error)})+'\n'); }
  }); });
  // Shutdown must interrupt an in-flight navigation, not leave its browser
  // running until a long mailbox operation finishes.
  lines.on('close',()=>close().finally(()=>process.exit(0)));
  process.on('SIGTERM',()=>close().finally(()=>process.exit(0)));
}
module.exports={readIdentity,readRows,readMessage,inboxState,waitForInbox,handle,close,status,operate,resolveSenderCard,session,sendReviewed,readComposer,safeRowWarning};
