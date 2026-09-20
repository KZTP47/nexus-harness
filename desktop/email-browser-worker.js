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
const PARSER_CONTRACT = 'browser-message-dom/v5';
const digest = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');
const contentHash = message => digest([message.sender,message.subject,message.body,message.message_id || '']);
const sourceHash = (provider,row,message) => createHash('sha256').update(message.message_id ? provider+'|'+message.message_id : row.id+'|'+message.sender+'|'+message.subject+'|'+message.body).digest('hex');
const rowSelector = (provider,attr,id) => `${provider==='browser_gmail'?'tr':'[role="option"]'}[${attr}=${JSON.stringify(id)}]`;
const urls = {browser_outlook:'https://outlook.office.com/mail/inbox', browser_gmail:'https://mail.google.com/mail/u/0/#inbox'};
const mailboxHosts = provider => provider==='browser_gmail' ? ['mail.google.com'] : ['outlook.office.com','outlook.office365.com','outlook.live.com','outlook.cloud.microsoft'];
const trustedOrigin = (provider,url) => url.protocol==='https:' && mailboxHosts(provider).includes(url.hostname);
// Gmail paints its list within seconds and is reloaded often. Outlook Web boots slowly on managed
// devices and pushes list updates itself, so it is reloaded rarely and given longer to hydrate.
const budgets = {
  browser_gmail:{stale:30000,boot:10000,grace:0,settle:10000,goto:20000,identity:10000,signin:15000,pane:10000},
  browser_outlook:{stale:900000,boot:90000,grace:3000,settle:15000,goto:20000,identity:10000,signin:15000,pane:10000}
};
const policy = provider => budgets[provider] || budgets.browser_outlook;
const RESERVE_MS = 15000;
const RECOVERY_MAX = 2;
const NEXT_SCAN = 'It will be checked again on the next scan.';
// Walking the list costs budget the conversations themselves need, so collection
// is capped well before the work deadline and resumes on the next scan.
const ROW_LIMIT = 200;
// A conversation the mailbox never paints must not be retried ahead of new
// mail on every scan, so each failure pushes its next attempt further out.
const FAILURE_COOLDOWN_MS = 300000;
const FAILURE_COOLDOWN_MAX = 3600000;
const ROW_STEPS = 40;
const COLLECT_MS = 8000;
const SCROLL_SETTLE_MS = 150;
const REVEAL_MS = 6000;
// outlook.cloud.microsoft rewrites /mail/inbox to /mail/, outlook.live.com uses /mail/0/inbox and an opened conversation appends /id/<item>.
const INBOX_PATH = /^\/mail(?:\/\d+)?\/inbox(?:\/id\/[^/]+)?\/?$/i;
const BARE_PATH = /^\/mail(?:\/\d+)?(?:\/id\/[^/]+)?\/?$/i;
// Some tenants address the folder by an opaque, percent-encoded id instead of a name; only the remembered inbox folder makes such a path the inbox.
const FOLDER_PATH = /^\/mail\/(?:[A-Za-z0-9_=-]|%[0-9A-Fa-f]{2}){20,}(?:\/id\/[^/]+)?\/?$/;
const folderSegment = pathname => pathname.match(/^\/mail\/([^/]+)/)?.[1]||'';
const pathClass = pathname => INBOX_PATH.test(pathname)||BARE_PATH.test(pathname) ? 'inbox path' : FOLDER_PATH.test(pathname) ? 'folder path' : 'not an inbox path';
const isTimeout = error => error?.name==='TimeoutError';
const sleep = ms => new Promise(resolve=>setTimeout(resolve,ms));
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
// The bridge stores this text verbatim; browser call logs never leave the worker.
function publicError(error) {
  const line=String(error?.message||error||'').replace(/\x1b\[[0-?]*[ -/]*[@-~]/g,'').split('\n')[0].trim();
  // Only an Error thrown by the worker's own page function is unwrapped; a destroyed context or a script fault stays browser text.
  const thrown=line.match(/^page\.evaluate: Error: (.*)$/)?.[1].trim();
  const first=thrown??line;
  if (/has been closed|Target closed|Target page, context or browser/i.test(first)) return 'The mail browser page was closed. Reconnect the browser.';
  if (/^(?:page|locator|frame|elementHandle|jsHandle|browserContext|browser|keyboard|mouse)\.\w+:/.test(first)||/^[A-Z]\w*Error:/.test(first)||/Execution context was destroyed|Cannot read propert|is not defined|is not a function/.test(first)) return 'The mailbox page did not respond as expected. '+NEXT_SCAN;
  return first.slice(0,1000);
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
  return Array.from(document.querySelectorAll(selector)).filter(e => e.getClientRects().length).map(e => {
    const attr = ['data-legacy-thread-id','data-thread-id','data-convid','data-itemid','data-id'].find(a => e.hasAttribute(a));
    return {id:e.getAttribute(attr), attr,...(options.signatures?{ids:['data-legacy-thread-id','data-thread-id','data-convid','data-itemid','data-id'].map(a=>e.getAttribute(a)).filter(Boolean),summary:[e.innerText,...Array.from(e.querySelectorAll('time,[data-message-count]')).map(node=>[node.getAttribute('datetime'),node.getAttribute('data-message-count')])]}:{})};
  }).filter(e => e.id);
}
// Outlook and Gmail both virtualise their list, so a conversation below the
// viewport has no element at all until its list is scrolled to it.
function scrollList(options) {
  const provider=typeof options==='string'?options:options.provider;
  const selector = provider === 'browser_gmail' ? 'tr[data-legacy-thread-id], tr[data-thread-id]' : '[role="option"][data-convid], [role="option"][data-itemid], [role="option"][data-id]';
  const row=document.querySelector(selector);
  if (!row) return {moved:false,atEnd:true,top:0};
  let node=row.parentElement;
  while (node && node!==document.body && !(node.scrollHeight>node.clientHeight+8 && /auto|scroll/.test(getComputedStyle(node).overflowY))) node=node.parentElement;
  if (!node || node===document.body) return {moved:false,atEnd:true,top:0};
  const before=node.scrollTop;
  // Step by less than a screen: a step larger than the viewport scrolls whole
  // conversations past unseen, because the list only renders its current window.
  node.scrollTop = typeof options.top==='number' ? options.top : before+Math.max(40,Math.round(node.clientHeight*0.8));
  return {moved:node.scrollTop!==before, atEnd:node.scrollTop+node.clientHeight>=node.scrollHeight-2, top:node.scrollTop};
}
function readMessage(options) {
  const provider=typeof options==='string' ? options : options.provider;
  const gmail = provider === 'browser_gmail';
  const bodySelector = gmail ? '.a3s' : '[role="document"], [aria-label="Message body"]';
  const senderSelector = gmail ? '.gD[email]' : '[id^="MSG_"][id$="_FROM"], [data-testid="SenderPersona"] [title*="@"], [email], [smtp]';
  const meaningfulBody=e=>(e.innerText?.trim() || (!gmail && e.matches('[aria-label="Message body"]') && e.closest('[data-test-id="mailMessageBodyContainer"]'))) && getComputedStyle(e).visibility!=='hidden' && Array.from(e.getClientRects()).some(rect=>rect.width>0&&(rect.height>0||(e.matches('[aria-label="Message body"]')&&e.closest('[data-test-id="mailMessageBodyContainer"]'))));
  const scopeOf=body=>{
    let scope=body.closest(gmail?'[data-legacy-message-id], [data-message-id]':'[aria-label="Email message"]') || body.parentElement;
    while(scope&&scope!==document.body&&!scope.querySelector(senderSelector))scope=scope.parentElement;
    return scope&&scope!==document.body&&Array.from(scope.querySelectorAll(bodySelector)).filter(meaningfulBody).length===1?scope:null;
  };
  const bodies = Array.from(document.querySelectorAll(bodySelector)).filter(meaningfulBody).filter(body=>{
    const scope=scopeOf(body);
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
  const envelope=element&&scopeOf(element);
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
  if (!sender || (!subjectNode&&!conversationSubject)) throw new Error('This mailbox layout cannot be read reliably: message '+[!sender && 'sender',(!subjectNode&&!conversationSubject) && 'subject'].filter(Boolean).join(', ')+' is not ready.');
  if (body.length>100000 || subject.length>1000) throw new Error('This message exceeds the complete browser import size limit. Read it in your mailbox; no partial draft is generated.');
  // Only a machine-readable stamp is trusted; a localised "2 h" would sort worse than no date at all.
  const stamp=Array.from(envelope.querySelectorAll('time[datetime]')).map(node=>node.getAttribute('datetime'))
    .find(value=>value&&Number.isFinite(Date.parse(value)))||'';
  const received_at=stamp?new Date(Date.parse(stamp)).toISOString():'';
  return {sender,subject,body,received_at,message_id:message_id || ''};
}
async function resolveSenderCard(page,expectedMessageId='') {
  const senderNodeId=await page.evaluate(expected=>Array.from(document.querySelectorAll('[role="document"], [aria-label="Message body"]'))
    .filter(e=>getComputedStyle(e).visibility!=='hidden'&&Array.from(e.getClientRects()).some(rect=>rect.width>0&&(rect.height>0||(e.matches('[aria-label="Message body"]')&&e.closest('[data-test-id="mailMessageBodyContainer"]')))))
    .map(e=>{
      let scope=e.closest('[aria-label="Email message"]')||e.parentElement;
      while(scope&&scope!==document.body&&!scope.querySelector('[id^="MSG_"][id$="_FROM"]'))scope=scope.parentElement;
      return scope&&scope!==document.body&&scope.querySelectorAll('[role="document"], [aria-label="Message body"]').length===1?scope:null;
    }).filter(scope=>{if(!scope)return false;const copy=scope.cloneNode(true);copy.querySelectorAll('[role="document"], [aria-label="Message body"]').forEach(node=>node.remove());return !/This message hasn['’]t been sent\./.test(copy.textContent);})
    .map(scope=>scope.querySelector('[id^="MSG_"][id$="_FROM"]')?.id).filter(id=>id&&(!expected||id.slice(4,-5)===expected)).at(-1),expectedMessageId);
  if (!senderNodeId) throw new Error('The latest message sender cannot be identified.');
  const sender=page.locator(`[id=${JSON.stringify(senderNodeId)}]:visible`).first();
  try {
    await sender.getByRole('button').first().click({timeout:5000}).catch(()=>{
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
  if (options.hosts && (location.protocol!=='https:' || !options.hosts.includes(location.hostname))) return 'elsewhere';
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
// The selected folder is compared by its own label, so it works in any display language; a shared mailbox has its own "Inbox", so a folder outside the primary mailbox root carries that root's name.
function selectedFolderKey(all) {
  const nodes=Array.from(document.querySelectorAll('[role="treeitem"][aria-selected="true"], [role="treeitem"][aria-current="page"]'));
  const name=item=>{
    const label=(item.getAttribute('data-folder-id')||item.getAttribute('data-folder-name')||item.getAttribute('title')||item.getAttribute('aria-label')||(item.innerText||'').split('\n')[0]||'').replace(/\s+/g,' ').trim();
    // An unread count and its phrasing ("Inbox - 12 unread") come and go; the name before it is the stable part.
    return label.replace(/(?:^|[\s,.:;()\-–])+\d[\s\S]*$/,'').trim()||label;
  };
  const keyOf=node=>{
    let root=node,parent;
    while ((parent=root.parentElement?.closest('[role="treeitem"]'))) root=parent;
    if (root===node) {
      // A flat tree marks depth with aria-level; the nearest earlier item at a lower level owns this one.
      const items=Array.from((node.closest('[role="tree"]')||document).querySelectorAll('[role="treeitem"]'));
      let level=Number(node.getAttribute('aria-level'))||0;
      for (let index=items.indexOf(node)-1;index>=0&&level>1;index--) { const above=Number(items[index].getAttribute('aria-level'))||0; if (above&&above<level) { root=items[index]; level=above; } }
    }
    return (root===node||/^primaryMailboxRoot_/.test(root.id) ? '' : name(root)+'|')+name(node);
  };
  // Favorites lists the inbox a second time under its own root; the primary mailbox's entry is the one remembered.
  const keys=[...new Set(nodes.map(keyOf))].sort((a,b)=>a.includes('|')-b.includes('|'));
  return all ? keys : keys[0]||'';
}
function listSignature(provider) {
  const selector=provider==='browser_gmail' ? 'tr[data-legacy-thread-id], tr[data-thread-id]' : '[role="option"][data-convid], [role="option"][data-itemid], [role="option"][data-id]';
  return Array.from(document.querySelectorAll(selector)).filter(e=>e.getClientRects().length).slice(0,50)
    .map(e=>['data-convid','data-itemid','data-id','data-legacy-thread-id','data-thread-id'].map(attr=>e.getAttribute(attr)).find(Boolean)).join('|');
}
// The ribbon (Home, View) also uses selected tabs and precedes the inbox tabs in the DOM.
function selectedTabName() {
  for (const tab of document.querySelectorAll('[role="tab"][aria-selected="true"]')) {
    const name=(tab.getAttribute('aria-label')||tab.innerText||'').trim().replace(/\s+\d+$/,'');
    if (['Focused','Other'].includes(name)) return name;
  }
  return '';
}
// Visible message bodies with their ids: two conversations with the same text still differ, and a detached body never counts.
function paneView(provider) {
  const gmail=provider==='browser_gmail';
  const bodySelector=gmail?'.a3s':'[role="document"], [aria-label="Message body"]';
  const bodies=Array.from(document.querySelectorAll(bodySelector)).filter(e=>(e.innerText?.trim()||(!gmail&&e.matches('[aria-label="Message body"]')&&e.closest('[data-test-id="mailMessageBodyContainer"]')))&&getComputedStyle(e).visibility!=='hidden'&&Array.from(e.getClientRects()).some(rect=>rect.width>0&&(rect.height>0||(e.matches('[aria-label="Message body"]')&&e.closest('[data-test-id="mailMessageBodyContainer"]')))));
  const scopeOf=body=>{let scope=body.closest('[aria-label="Email message"]')||body.parentElement;while(scope&&scope!==document.body&&!scope.querySelector('[id^="MSG_"][id$="_FROM"]'))scope=scope.parentElement;return scope&&scope!==document.body&&Array.from(scope.querySelectorAll(bodySelector)).filter(e=>bodies.includes(e)).length===1?scope:null;};
  const idOf=body=>{const node=body.closest('[data-legacy-message-id], [data-message-id], [data-item-id]');return node?.getAttribute('data-legacy-message-id')||node?.getAttribute('data-message-id')||node?.getAttribute('data-item-id')||scopeOf(body)?.querySelector('[id^="MSG_"][id$="_FROM"]')?.id.slice(4,-5)||'';};
  const binding=bodies.at(-1)?.closest('[data-convid], [data-thread-id], [data-legacy-thread-id]');
  // data-id is generic enough for an unrelated wrapper to carry it, so it is never pane-side identity.
  const attrs=gmail ? ['data-legacy-thread-id','data-thread-id','data-thread-perm-id','data-convid'] : ['data-convid','data-itemid','data-thread-id'];
  // Outlook names the conversation and the message in element ids as well as attributes: CONV_<id>_SUBJECT, MSG_<id>_FROM.
  const keyed=node=>/^(?:CONV|MSG)_(.+)_(?:SUBJECT|FROM)$/.exec(node?.id||'')?.[1]||'';
  const owners=body=>{
    const scope=gmail?body.closest('[data-legacy-message-id], [data-message-id]'):scopeOf(body);
    return [...new Set([idOf(body),keyed(scope?.querySelector('[id^="MSG_"][id$="_FROM"]')),...attrs.map(attr=>body.closest('['+attr+']')?.getAttribute(attr))].filter(Boolean))];
  };
  const selected=gmail ? [] : Array.from(document.querySelectorAll('[role="option"][aria-selected="true"]')).filter(row=>row.getClientRects().length).flatMap(row=>['data-convid','data-itemid','data-id'].map(attr=>row.getAttribute(attr)).filter(Boolean));
  const item=gmail ? '' : location.pathname.match(/\/id\/([^/]+)/)?.[1]||'';
  // A list row can carry a conversation heading of its own, so only headings outside the message list describe the open conversation.
  const headings=Array.from(document.querySelectorAll(gmail ? 'h2.hP' : '[id^="CONV_"][id$="_SUBJECT"], [data-testid="conversation-subject"]')).filter(e=>e.getClientRects().length&&!e.closest('[role="option"]'));
  return {owners:bodies.map(owners),heads:[...new Set(headings.map(keyed).filter(Boolean))],subject:headings.map(e=>e.innerText.trim()).find(Boolean)||'',bodies:bodies.map(body=>[idOf(body),body.innerText.trim()]),bindings:binding ? ['data-convid','data-thread-id','data-legacy-thread-id'].map(attr=>binding.getAttribute(attr)).filter(Boolean) : [],selected,item};
}
// Outlook Web names a conversation in element ids by the tail of the id its list row carries, so pane-side identity can be a suffix of a row id.
const SHORT_ID=8;
const tailOf=(id,candidate)=>candidate.length>id.length&&candidate.endsWith(id);
const knows=(id,candidates)=>candidates.some(candidate=>candidate===id||(id.length>=SHORT_ID&&tailOf(id,candidate)));
// A short form identifies a conversation only while it matches this row and no other conversation the scan knows.
const identifies=(id,ids,known)=>!!id&&knows(id,ids)&&(ids.includes(id)||![...known||[]].some(other=>!ids.includes(other)&&knows(id,[other])));
// Positive proof: a rendered message names one of the clicked row's own ids, or the single conversation heading does.
const paneProvesRow=(view,ids,known)=>!!view.bodies.length&&(view.owners.some(owners=>owners.some(id=>identifies(id,ids,known)))||(view.heads.length===1&&identifies(view.heads[0],ids,known)));
// An item id reaches the path percent-encoded, and a tenant may leave a stray percent sign in one.
const decoded=value=>{try{return decodeURIComponent(value);}catch{return value;}};
// A pane that names a conversation known to this scan but not this row is that conversation's, however its rows are marked.
const paneNamesOther=(view,ids,known)=>[...view.owners.flat(),...view.heads,view.item&&decoded(view.item)].some(id=>id&&!!known&&!identifies(id,ids,known)&&knows(id,[...known]));
// Outlook also marks expanded conversation items and attachments as selected options; only another list row still selected means the click was not applied.
const otherSelected=(before,view,rowId)=>view.selected.some(id=>id!==rowId&&(!before.rows||before.rows.has(id)));
// A conversation switch leaves none of the previously visible messages on screen; a repaint of the still-open conversation keeps them.
function paneShowsRow(before,view,rowId) {
  if (!view.bodies.length) return false;
  if (view.bindings.includes(rowId)) return true;
  // A pane bound to another conversation of this list is that conversation's, however its rows are marked.
  if (view.bindings.some(id=>!before.rows||before.rows.has(id))) return false;
  if (otherSelected(before,view,rowId)) return false;
  // Outlook moves the /id/ segment when another conversation opens; a reply pushed into the open one leaves it, however its older messages collapse.
  if (before.item&&view.item===before.item) return false;
  const ids=new Set(before.bodies.map(([id])=>id).filter(Boolean)),texts=new Set(before.bodies.map(([,text])=>text));
  // Ids decide when both sides carry them; text alone must decide when either side lacks them, since a body can gain its id while it hydrates.
  return !view.bodies.some(([id,text])=>id&&ids.size ? ids.has(id) : texts.has(text));
}
// Rendered landmarks only: they separate a page that is still booting from a list layout the worker cannot read.
function inboxSignals() {
  // Loading placeholders are blank option rows; only rows that carry text count as a rendered list.
  const visible=(selector,withText)=>Array.from(document.querySelectorAll(selector)).some(e=>e.getClientRects().length&&(!withText||e.innerText?.trim()||e.getAttribute('aria-label')?.trim()));
  const seen=[['main','[role="main"]'],['folders','[role="treeitem"]'],['tabs','[role="tab"]'],['list','[role="main"] [role="listbox"], #MailList'],['rows','[role="main"] [role="option"]',true],['busy','[role="main"] [role="progressbar"], [role="main"] [aria-busy="true"]']]
    .filter(([,selector,withText])=>visible(selector,withText)).map(([name])=>name);
  return document.visibilityState==='hidden' ? [...seen,'hidden'] : seen;
}
async function waitForInbox(page,provider,timeout) {
  const deadline=Date.now()+timeout;
  for (;;) {
    const state=await page.waitForFunction(inboxState,{provider,wait:true,hosts:mailboxHosts(provider)},{timeout:Math.max(1,deadline-Date.now()),polling:250}).then(handle=>handle.jsonValue());
    if (state!=='elsewhere') return state;
    // A booting page can pass through the provider's sign-in origin; only staying there ends the wait, and the caller's identity check decides what it means.
    if (!await page.waitForURL(url=>trustedOrigin(provider,url),{timeout:Math.max(1,Math.min(policy(provider).signin,deadline-Date.now()))}).then(()=>true,()=>false)) return 'elsewhere';
  }
}
async function inboxFailure(value,elapsed,follow) {
  if (value.provider==='browser_gmail') return new Error('This inbox layout is unsupported or still loading. Reconnect after opening the inbox.');
  const current=new URL(value.page.url());
  const signals=await value.page.evaluate(inboxSignals).catch(()=>[]);
  const shown=signals.filter(name=>name!=='hidden');
  const where=`${trustedOrigin(value.provider,current)?current.hostname:'untrusted host'}, ${pathClass(current.pathname)}; ${shown.length?'shown: '+shown.join(', '):'nothing rendered yet'}`;
  // Unknown rows, or a list that stays unreadable scan after scan, mean a layout change only once the page is past its boot and idle.
  const settled=!booting(value)&&!signals.includes('busy');
  if (settled&&(signals.includes('rows')||(signals.includes('list')&&value.inboxFailures>=3))) return new Error(`This inbox layout is unsupported (${where}). Reconnect after opening the inbox.`);
  if (signals.includes('hidden')&&value.mode==='headed') return new Error(`The Nexus mail window is hidden or minimised, so the mailbox did not finish loading (${where}). Restore it or switch this connection to headless mode.`);
  return new Error(`The mailbox page did not finish loading after ${Math.round(elapsed/1000)} s (${where}). ${follow}`);
}
// The folder the worker's own inbox navigation lands on is remembered, so hosts that drop the folder from the URL can still be checked.
async function rememberFolder(value) {
  if (value.provider!=='browser_outlook'||!value.folderPending||value.lastOpenedRow||Date.now()-value.lastInboxRefresh>=policy(value.provider).boot) return;
  const current=new URL(value.page.url());
  // A tenant that addresses the inbox by an opaque id can render its folder tree late; the landing path stands in until the tree confirms the folder.
  if (trustedOrigin(value.provider,current)&&FOLDER_PATH.test(current.pathname)) value.inboxFolderPath=folderSegment(current.pathname);
  const key=await value.page.evaluate(selectedFolderKey).catch(()=>'');
  if (key) { value.inboxFolderKey=key; value.folderPending=false; }
}
async function awaitInbox(value,timeout,follow=NEXT_SCAN) {
  const started=Date.now();
  let state;
  try { state=await waitForInbox(value.page,value.provider,timeout); }
  catch(error) { if (!isTimeout(error)) throw error; value.inboxFailures=(value.inboxFailures||0)+1; throw await inboxFailure(value,Date.now()-started,follow); }
  if (state==='elsewhere') throw await inboxFailure(value,Date.now()-started,follow);
  value.inboxReady=true; value.inboxFailures=0; await rememberFolder(value); return state;
}
async function reloadInbox(value,timeout,follow=NEXT_SCAN) {
  const target=inboxUrl(value);
  // Stamped before navigating: a slow navigation is still in flight when the next scan starts and must be awaited, not restarted.
  value.lastInboxRefresh=Date.now();value.lastOpenedRow='';value.paneDirty=false;value.inboxReady=false;value.folderPending=true;value.ownLoadPending=true;
  try { await value.page.goto(target,{waitUntil:'domcontentloaded',timeout}); }
  catch(error) { if (!isTimeout(error)) throw error; throw new Error(`The mailbox page did not finish loading after ${Math.round(timeout/1000)} s (${new URL(target).hostname}, navigating). ${follow}`); }
}
async function probeInbox(value,grace) {
  if (!grace) return value.page.evaluate(inboxState,value.provider);
  return waitForInbox(value.page,value.provider,grace).catch(error=>{ if (!isTimeout(error)) throw error; return 'unsupported'; });
}
const sinceReload = value => value.lastInboxRefresh ? Date.now()-value.lastInboxRefresh : Infinity;
const booting = value => value.provider==='browser_outlook' && sinceReload(value)<policy(value.provider).boot && !value.inboxReady;
// A page still inside its boot budget is waited for, never reloaded; an Outlook wait always leaves the scan its closing reserve, Gmail keeps its short fixed list wait.
function hydrationBudget(value,deadline) {
  const {boot,settle}=policy(value.provider);
  if (value.provider==='browser_gmail') return settle;
  const since=sinceReload(value);
  return Math.max(2000,Math.min(booting(value) ? boot-since : settle,deadline-RESERVE_MS-Date.now()));
}
// The account control hydrates with the rest of a booting shell: a sync shares its scan budget, a cold status probe may use the boot budget within the bridge's 150 s ceiling.
function identityBudget(value,request,workDeadline) {
  if (request.command==='open'||!booting(value)) return undefined;
  return hydrationBudget(value,request.command==='sync' ? workDeadline : Math.min(Date.now()+100000,(request._startedAt||Date.now())+110000));
}
// Outlook Web reloads itself for token renewal; a scan measures the boot from that navigation, not from the worker's earlier one.
function watchReloads(value) {
  value.page.on('load',()=>{
    if (value.ownLoadPending) { value.ownLoadPending=false; return; }
    if (value.provider!=='browser_outlook'||!trustedOrigin(value.provider,new URL(value.page.url()))) return;
    // The late paint and the open conversation died with that document however soon the reload followed the worker's own navigation.
    value.paneDirty=false; if (!value.replying) value.lastOpenedRow='';
    // A second load moments after the worker's own navigation belongs to the same boot, so the boot clock is left alone.
    if (sinceReload(value)>policy(value.provider).settle) { value.lastInboxRefresh=Date.now(); value.inboxReady=false; }
  });
}
function inboxUrl(value) {
  const current=new URL(value.page.url());
  if (!trustedOrigin(value.provider,current)) return urls[value.provider];
  if (value.provider==='browser_gmail') return current.origin+current.pathname+'#inbox';
  const index=current.pathname.match(/^\/mail\/(\d+)(?:\/|$)/)?.[1] || (current.hostname==='outlook.live.com' ? '0' : '');
  return current.origin+'/mail/'+(index ? index+'/' : '')+'inbox';
}
async function onInbox(value) {
  const current=new URL(value.page.url());
  if (!trustedOrigin(value.provider,current)) return false;
  if (value.provider==='browser_gmail') return current.hash==='#inbox';
  if (INBOX_PATH.test(current.pathname)) return true;
  if (!BARE_PATH.test(current.pathname)&&!FOLDER_PATH.test(current.pathname)) return false;
  const keys=await value.page.evaluate(selectedFolderKey,true).catch(()=>[]);
  if (keys.length&&value.inboxFolderKey) return keys.includes(value.inboxFolderKey);
  if (FOLDER_PATH.test(current.pathname)) return !!value.inboxFolderPath&&folderSegment(current.pathname)===value.inboxFolderPath;
  if (keys.some(key=>/^Inbox$/i.test(key))) return true;
  // Without folder evidence a bare folder path is the inbox the worker navigated to; an item path only if the worker opened that row.
  return !/\/id\//i.test(current.pathname) || !!value.lastOpenedRow;
}
async function ensureInbox(value,options) {
  const {stale,grace,goto}=policy(value.provider);
  const follow=options.follow||NEXT_SCAN;
  const hold=booting(value);
  let located=await onInbox(value);
  if (hold&&!located) {
    // A booting page can pass through sign-in or shell paths; only a trusted inbox URL ends the wait.
    await value.page.waitForURL(url=>trustedOrigin(value.provider,url)&&(value.provider==='browser_gmail'?url.hash==='#inbox':INBOX_PATH.test(url.pathname)||BARE_PATH.test(url.pathname)),{timeout:hydrationBudget(value,options.deadline)}).catch(()=>{});
    located=await onInbox(value);
  }
  // A rendered list can stop updating (expired session dialog, dead push channel); only a fresh document proves it is live, judged once per scan.
  const aged=!options.inLoop&&sinceReload(value)>=stale;
  if (options.force||!value.lastInboxRefresh||!located||aged||(!hold&&await probeInbox(value,grace)==='unsupported')) await reloadInbox(value,goto,follow);
  return awaitInbox(value,hydrationBudget(value,options.deadline),follow);
}
async function selectTab(value,target,name,attempts=2) {
  const {page,provider}=value;
  const before=await page.evaluate(listSignature,provider);
  // A freshly hydrated tab strip can ignore a click while the list header re-renders.
  for (let attempt=0;attempt<attempts;attempt++) {
    try {
      await target.click({timeout:8000});
      await page.waitForFunction(name=>[...document.querySelectorAll('[role="tab"]')].some(tab=>(tab.getAttribute('aria-label')||tab.innerText).trim().replace(/\s+\d+$/,'')===name&&tab.getAttribute('aria-selected')==='true'),name,{timeout:3000});
      break;
    } catch(error) {
      if (!isTimeout(error)&&!/strict mode violation/.test(error?.message||'')) throw error;
      if (attempt===attempts-1) throw new Error('The inbox tab did not finish switching.');
    }
  }
  // The list repaints after the tab flips; rows still showing the previous tab must not be read under the new name.
  const deadline=Date.now()+policy(provider).settle;
  let last=before;
  while (Date.now()<deadline) {
    const now=await page.evaluate(listSignature,provider);
    if ((now!==before||!now)&&now===last) return true;
    last=now; await sleep(250);
  }
  return false;
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
    const identity=current.protocol==='https:'&&mailboxHosts(value.provider).includes(current.hostname)?await value.page.evaluate(readIdentity,value.provider):{};
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
    watchReloads(value);
    // The first Outlook scan continues this boot instead of restarting it.
    if (c.provider==='browser_outlook') { value.lastInboxRefresh=Date.now(); value.folderPending=true; }
    value.ownLoadPending=true;
    try { await page.goto(urls[c.provider], {waitUntil:'domcontentloaded',timeout:30000}); }
    catch(error) { if (!isTimeout(error)) throw error; throw new Error(`The mailbox page did not finish loading after 30 s (${new URL(urls[c.provider]).hostname}, opening the mailbox). ${NEXT_SCAN}`); }
  }
  if(request.command==='open')value.explicitSignIn=true;
  return value;
}
async function status(value, connection, identityTimeout=policy(value.provider).identity) {
  const hosts=mailboxHosts(value.provider);
  const authHosts=value.provider==='browser_gmail' ? ['accounts.google.com'] : ['login.microsoftonline.com','login.live.com','login.microsoft.com'];
  let host = new URL(value.page.url()).hostname;
  if (authHosts.includes(host) && !value.explicitSignIn) {
    // A saved session can briefly cross the provider's sign-in origin before
    // returning to mail. Wait only for known mailbox hosts; never follow a
    // supplied URL or infer authentication from the redirect alone.
    await value.page.waitForURL(url=>url.protocol==='https:' && hosts.includes(url.hostname),
      {timeout:policy(value.provider).signin,waitUntil:'domcontentloaded'}).catch(()=>{});
    host=new URL(value.page.url()).hostname;
  }
  const trusted = new URL(value.page.url()).protocol==='https:' && hosts.includes(host);
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
    },value.provider,{timeout:identityTimeout,polling:200}).catch(()=>{});
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
    const started=Date.now();
    let value;
    try{value=await session(request);}catch(error){if(request.command==='send')return {status:'not_sent',error:publicError(error).slice(0,400),submission_id:request.submission_id};throw error;}
    return operate(value,{...request,_startedAt:started});
  });
  operations.set(key,operation);
  try { return await operation; } finally { if(operations.get(key)===operation) operations.delete(key); }
}
async function operate(value,request) {
  // A send stamps the conversation it re-opens itself; an Outlook self-reload during it must not erase that row.
  if (request.command==='send') { value.replying=true; try { return await sendReviewed(value,request); } finally { value.replying=false; } }
  if(request.command==='sync'&&!request._singleTab){
    // Both tabs share one work budget; the bridge waits 150 seconds from the request,
    // including browser startup and a final in-flight bounded browser operation.
    const scanDeadline=Math.min(Date.now()+60000,(request._startedAt||Date.now())+110000);
    // Recover only provider layout/loading failures, never a changed mailbox,
    // lost authentication, closed browser or unrelated programming exception.
    const recoverableTab=async error=>(await status(value,request.connection)).state==='connected'
      &&/Timeout|layout|not ready|needs another check|did not finish|inbox conversation moved/i.test(String(error.message||error));
    let first;
    try{first=await operate(value,{...request,_singleTab:true,_scanDeadline:scanDeadline});}
    catch(error){
      // A tab that cannot load must not strand the other one for the whole scan.
      const held=JSON.parse(request.cursor||'{}');
      if(!held.split_inbox||!await recoverableTab(error))throw error;
      const fallback={...held,next_tab:held.next_tab==='Other'?'Focused':'Other'};
      const only=await operate(value,{...request,_singleTab:true,_scanDeadline:scanDeadline,cursor:JSON.stringify(fallback)});
      // Its own cursor already names the failed tab, which therefore leads the next scan.
      return {...only,warnings:[...only.warnings,'The '+(fallback.next_tab==='Other'?'Focused':'Other')+' inbox tab did not finish loading, so the other tab was checked instead. '+NEXT_SCAN]};
    }
    const firstCursor=JSON.parse(first.cursor||'{}');
    if(!firstCursor.split_inbox)return first;
    if(Date.now()>=scanDeadline-RESERVE_MS)return {...first,warnings:[...first.warnings,'The scan work budget was reached. The other inbox tab will be checked on the next scan.']};
    let second;
    try{second=await operate(value,{...request,_singleTab:true,_secondPass:true,_scanDeadline:scanDeadline,cursor:first.cursor});}
    catch(error){
      if(!await recoverableTab(error))throw error;
      // The cursor already names the tab that just failed, so it leads the next scan
      // with the full work budget instead of being demoted to second place again.
      return {...first,warnings:[...first.warnings,'The '+(firstCursor.next_tab==='Other'?'Other':'Focused')+' inbox tab did not finish loading. Imported messages were preserved; that tab is checked first on the next scan.']};
    }
    return {messages:[...first.messages,...second.messages].filter((message,index,all)=>all.findIndex(other=>other.source_id===message.source_id)===index),
      failed_messages:[...first.failed_messages||[],...second.failed_messages||[]].filter((failure,index,all)=>all.findIndex(other=>other.source_id===failure.source_id)===index),
      resolved_failures:[...new Set([...first.resolved_failures||[],...second.resolved_failures||[]])],
      has_more:Boolean(first.has_more||second.has_more),
      cursor:second.cursor,warnings:[...new Set([...first.warnings,...second.warnings])]};
  }
  if (request.command === 'open') await value.page.bringToFront();
  const workDeadline=request._scanDeadline||Date.now()+60000;
  const result = await status(value,request.connection,identityBudget(value,request,workDeadline));
  if (request.command !== 'sync') return result;
  if (result.state !== 'connected') throw new Error('Browser sign-in expired or mailbox identity is unavailable. Reconnect the browser.');
  if(await hasComposer(value.page))throw new Error('An existing reply composer is open. Inbox checking will resume after it is finished or closed.');
  // Navigate only the dedicated Nexus page, never an existing user tab.
  let inboxStatus;
  try { inboxStatus = await ensureInbox(value,{deadline:workDeadline}); }
  catch(error) {
    // A fresh document that left the mailbox origin is judged by the rendered identity, never by the redirect itself.
    if (!trustedOrigin(value.provider,new URL(value.page.url()))&&(await status(value,request.connection)).state!=='connected') throw new Error('Mailbox sign-in changed while checking mail. Reconnect the browser.');
    throw error;
  }
  const refreshed = await status(value,request.connection);
  if (refreshed.state !== 'connected') throw new Error('Mailbox sign-in changed while checking mail. Reconnect the browser.');
  let cursor={seen:[],offset:0};
  try { cursor=JSON.parse(request.cursor || '{}'); }  catch { throw new Error('Browser mail cursor is invalid; reconnect the account.'); }
  if (cursor.contract!==SYNC_CONTRACT) cursor={seen:[],offset:0};
  const cacheBinding=digest(['browser-row-cache/v1',value.provider,refreshed.email.toLowerCase()]);
  const cacheValid=cursor.row_cache_contract==='browser-row-cache/v1'&&cursor.row_cache_binding===cacheBinding;
  const rowCache=cacheValid&&cursor.row_cache&&typeof cursor.row_cache==='object'&&!Array.isArray(cursor.row_cache)?cursor.row_cache:{};
  for(const key of Object.keys(rowCache)){const entry=rowCache[key];if(!/^[a-f0-9]{64}$/.test(key)||!entry||!/^[a-f0-9]{64}$/.test(entry.signature)||!Number.isFinite(entry.checked_at)||entry.checked_at>Date.now())delete rowCache[key];}
  const rowFailures=cacheValid&&cursor.parser_contract===PARSER_CONTRACT&&cursor.row_failures&&typeof cursor.row_failures==='object'&&!Array.isArray(cursor.row_failures)?{...cursor.row_failures}:{};
  for(const key of Object.keys(rowFailures)){const entry=rowFailures[key];if(!/^[a-f0-9]{64}$/.test(key)||!entry||!Number.isInteger(entry.count)||entry.count<1||!Number.isFinite(entry.at)||entry.at>Date.now())delete rowFailures[key];}
  if(!cacheValid&&value.lastScanCacheBinding)await ensureInbox(value,{force:true,deadline:workDeadline});
  value.lastScanCacheBinding=cacheBinding;
  let tabName=cursor.next_tab==='Other'?'Other':'Focused';let splitInbox=false;
  const notes=[];
  const selectInboxTab=async()=>{
    if(value.provider!=='browser_outlook')return;
    const focused=value.page.getByRole('tab',{name:/^Focused(?:\s+\d+)?$/});
    const other=value.page.getByRole('tab',{name:/^Other(?:\s+\d+)?$/});
    if(await focused.count()!==1||await other.count()!==1)return;
    splitInbox=true;
    const target=tabName==='Other'?other:focused;
    if(await target.getAttribute('aria-selected')==='true')return;
    let settled;
    try{settled=await selectTab(value,target,tabName);}
    catch(error){
      // A first pass keeps its budget useful by scanning the tab that is open; the second pass reports the failure instead.
      const open=request._secondPass||!/did not finish switching/.test(error.message)?'':await value.page.evaluate(selectedTabName);
      if(!open)throw new Error(`${error.message} ${NEXT_SCAN}`);
      notes.push(`The ${tabName} inbox tab did not finish switching; the ${open} tab was checked instead.`);
      tabName=open;
      return;
    }
    if(!settled){tabSettled=false;notes.push(`The ${tabName} inbox tab list did not visibly refresh after switching; its conversations are rechecked on the next scan.`);}
    await awaitInbox(value,hydrationBudget(value,workDeadline));
  };
  let tabSettled=true;
  await selectInboxTab();
  const snapshotRows=async()=> (await value.page.evaluate(readRows,{provider:value.provider,signatures:true})).map(({summary,...row})=>({...row,signature:digest(summary)}));
  // One viewport is a few conversations, so the list is walked to its end to find
  // the mail below it; a scan that never scrolled could only ever see the top.
  const scrollTo=async top=>{
    const position=await value.page.evaluate(scrollList,{provider:value.provider,top});
    if(position.moved)await value.page.waitForTimeout(SCROLL_SETTLE_MS);
    return position;
  };
  const scrollTop=async()=>{ try { await scrollTo(0); } catch {} };
  // Pagination belongs to the mailbox-bound cursor, not the browser process.
  // Keep unattempted rows until they are drained; a budget limit is not EOF.
  const progress=cacheValid&&cursor.walk_contract==='browser-walk/v1'&&cursor.walks&&typeof cursor.walks==='object'?{...cursor.walks}:{};
  const held=progress[tabName];
  const messageOffsets=cacheValid&&cursor.message_offsets&&typeof cursor.message_offsets==='object'?{...cursor.message_offsets}:{};
  for(const key of Object.keys(messageOffsets))if(!/^[a-f0-9]{64}$/.test(key)||!Number.isSafeInteger(messageOffsets[key])||messageOffsets[key]<0)delete messageOffsets[key];
  const validPosition=n=>Number.isFinite(n)&&n>=0&&n<=1e9;
  const validRow=row=>row&&typeof row.id==='string'&&row.id.length>0&&row.id.length<=2000&&['data-legacy-thread-id','data-thread-id','data-convid','data-itemid','data-id'].includes(row.attr)&&/^[a-f0-9]{64}$/.test(row.signature)&&validPosition(row.top);
  const walk=held&&validPosition(held.top)&&Array.isArray(held.pending)&&held.pending.length<=ROW_LIMIT&&held.pending.every(validRow)?held:{top:0,pending:[],capped:true};
  const collectRows=async()=>{
    const found=new Map();
    const budget=Math.min(workDeadline-RESERVE_MS,Date.now()+COLLECT_MS);
    let top=walk.top,after='';
    const add=async(resume='')=>{
      let overflow=false;
      const snapshot=await snapshotRows();
      const start=resume?snapshot.findIndex(row=>row.id===resume)+1:0;
      for (const row of snapshot.slice(start)) {
        if(found.has(row.id))continue;
        if(found.size>=ROW_LIMIT){overflow=true;continue;}
        found.set(row.id,{...row,top});
        after=row.id;
      }
      return overflow;
    };
    if(walk.pending.length){
      // Refresh the newest viewport without losing the older queued work.
      for(const row of walk.pending)found.set(row.id,row);
      await scrollTop();
      const fresh=await snapshotRows();
      for(const row of (fresh.length<=ROW_LIMIT?fresh:fresh.slice(0,10))){
        if(found.has(row.id))found.set(row.id,{...row,top:0});
        else if(found.size<ROW_LIMIT)found.set(row.id,{...row,top:0});
      }
      return {rows:[...found.values()],capped:walk.capped,top,after:walk.after||''};
    }
    top=(await scrollTo(top)).top;
    let overflow=await add(typeof walk.after==='string'?walk.after:'');
    // Only seeing the end of the list proves there is nothing further down; every
    // other way out of this loop leaves conversations that this scan never saw.
    let reachedEnd=false,scrolled=false;
    for (let step=0;step<ROW_STEPS;step++) {
      if (overflow || found.size>=ROW_LIMIT || Date.now()>=budget) break;
      let moved;
      try { moved=await value.page.evaluate(scrollList,{provider:value.provider}); } catch { break; }
      // A list that cannot scroll is already showing all of itself.
      if (!moved.moved) { reachedEnd=true; break; }
      scrolled=true;
      top=moved.top;
      await value.page.waitForTimeout(SCROLL_SETTLE_MS);
      overflow=await add();
      if (moved.atEnd&&!overflow) { reachedEnd=true; break; }
    }
    // Returning to the top costs a round trip, so it is spent only if the list moved.
    if (scrolled||top) await scrollTop();
    return {rows:[...found.values()],capped:!reachedEnd,top:reachedEnd?0:top,after:!reachedEnd&&(overflow||found.size>=ROW_LIMIT)?after:''};
  };
  const collected = await collectRows();
  const rows = collected.rows;
  const nextTab=splitInbox?(tabName==='Focused'?'Other':'Focused'):undefined;
  const cacheFields=()=>({parser_contract:PARSER_CONTRACT,message_offsets:messageOffsets,walk_contract:'browser-walk/v1',walks:progress,row_cache_contract:'browser-row-cache/v1',row_cache_binding:cacheBinding,row_cache:Object.fromEntries(Object.entries(rowCache).sort((a,b)=>b[1].checked_at-a[1].checked_at).slice(0,200)),row_failures:Object.fromEntries(Object.entries(rowFailures).sort((a,b)=>b[1].at-a[1].at).slice(0,200)),split_inbox:splitInbox,next_tab:nextTab});
  if (!rows.length && (inboxStatus==='empty'||await value.page.evaluate(inboxState,value.provider)==='empty')) return {messages:[],failed_messages:[],has_more:false,cursor:JSON.stringify({...cursor,contract:SYNC_CONTRACT,...cacheFields()}),warnings:notes};
  const seen=cursor.seen || [];
  if (!Array.isArray(seen) || seen.some(id=>typeof id!=='string') || seen.length>5000) throw new Error('Browser mail cursor is invalid.');
  const messages=[];
  const rowWarnings=[];
  let parsedCount=0,deadPane=false,recoveries=0,refusals=0;
  const outlook=value.provider!=='browser_gmail';
  const {goto,pane}=policy(value.provider);
  // A fresh document is the last resort for a pane that proves nothing: it is spent only while the scan can still absorb the navigation and another pane wait.
  const canRecover=()=>outlook&&recoveries<RECOVERY_MAX&&workDeadline-Date.now()>=goto+pane+RESERVE_MS;
  // The recovery's own wait is clamped by the hydration budget, so spending it needs only the navigation and the closing reserve.
  const recoverable=()=>outlook&&recoveries<RECOVERY_MAX&&workDeadline-Date.now()>=goto+RESERVE_MS;
  const refuse=()=>{refusals++;return new Error('The selected message did not finish loading.');};
  let checked=0;
  const failures=new Map();
  // A conversation read successfully clears the failure a previous scan recorded
  // for it. The studio cannot pair them itself: it stores failures under this row
  // key, while an imported message carries its own content hash instead.
  const resolved=new Set();
  const offset=rows.length&&Number.isInteger(cursor.offset) ? Math.max(0,cursor.offset)%rows.length : 0;
  const rowKey=row=>digest([tabName,row.attr,row.id]);
  const changed=row=>!rowCache[rowKey(row)]||rowCache[rowKey(row)].signature!==row.signature;
  const due=row=>changed(row)||Date.now()-rowCache[rowKey(row)].checked_at>=30000;
  const changedRows=rows.filter(changed);
  // A conversation that keeps failing must not hold the rest of the inbox behind
  // it. Rows with no recorded failure are read first, every row still gets one
  // immediate retry, and a row that fails again waits out a growing cooldown.
  const cooling=row=>{const past=rowFailures[rowKey(row)];return!!past&&past.count>1&&Date.now()-past.at<Math.min(FAILURE_COOLDOWN_MS*2**Math.min(past.count-2,4),FAILURE_COOLDOWN_MAX);};
  const failedBefore=changedRows.filter(row=>rowFailures[rowKey(row)]);
  const deferred=failedBefore.filter(cooling);
  const reconciliationRow=[...rows.slice(offset),...rows.slice(0,offset)].find(row=>!changed(row)&&due(row));
  const ordered=[...changedRows.filter(row=>!rowFailures[rowKey(row)]),...failedBefore.filter(row=>!cooling(row)),...(reconciliationRow?[reconciliationRow]:[])];
  if(deferred.length)notes.push(deferred.length+' conversation'+(deferred.length===1?' that':'s that')+' failed earlier '+(deferred.length===1?'is':'are')+' waiting before another attempt, so new mail is checked first.');
  // Rotate every scan, so a scan that ends before its reconciliation row still
  // starts the next one further down instead of re-offering the same row forever.
  let nextOffset=rows.length?(offset+1)%rows.length:0;
  const noteFailure=(row,error)=>{
    const warning=safeRowWarning(error);
    rowWarnings.push(warning);
    failures.set(rowKey(row),{source_id:rowKey(row),error:warning});
    rowFailures[rowKey(row)]={count:(rowFailures[rowKey(row)]?.count||0)+1,at:Date.now()};
  };
  const attempted=new Set();
  const incomplete=new Set();
  const revealRow=async (selector,row)=>{
    const budget=Math.min(workDeadline-RESERVE_MS,Date.now()+REVEAL_MS);
    // A saved scroll hint locates deep virtualized rows after reload/restart.
    // Only the exact row selector is ever clicked; positions are never identity.
    if(validPosition(row.top)){
      await scrollTo(row.top);
      if(await value.page.locator(selector).count())return true;
    }
    await scrollTop();
    for (let step=0;step<ROW_STEPS&&Date.now()<budget;step++) {
      if (await value.page.locator(selector).count()) return true;
      let moved;
      try { moved=await value.page.evaluate(scrollList,{provider:value.provider}); } catch { break; }
      if (!moved.moved) break;
      await value.page.waitForTimeout(SCROLL_SETTLE_MS);
      if (moved.atEnd) break;
    }
    return Boolean(await value.page.locator(selector).count());
  };
  const refreshInbox=async(force=false)=>{
    if(await hasComposer(value.page))throw new Error('An existing reply composer is open. Inbox checking will resume after it is finished or closed.');
    await ensureInbox(value,{force,deadline:workDeadline,inLoop:true});
    await selectInboxTab();
    const identity=await status(value,request.connection);
    if(identity.state!=='connected')throw new Error('Mailbox sign-in changed while checking mail. Reconnect the browser.');
  };
  for (const row of ordered) {
    if (checked>=10 || Date.now()>=workDeadline-RESERVE_MS) break;
    // A conversation that paints after its own wait must never be read under the next row; only a fresh document discards it, and a boot this scan cannot absorb is left to the next one.
    if (value.paneDirty&&!canRecover()) { notes.push('A conversation did not finish loading, so the remaining conversations will be checked on the next scan.'); break; }
    attempted.add(row.id);
    if(reconciliationRow&&row.id===reconciliationRow.id)nextOffset=(rows.findIndex(item=>item.id===row.id)+1)%rows.length;
    const warningCount=rowWarnings.length,parsedBefore=parsedCount;
    const selector = rowSelector(value.provider,row.attr,row.id);
    try {
    // Reopening the conversation that is already open may leave a stale pane; a fresh document is the only proof of new content.
    // Discarding a late paint costs the same document a proof failure would, so it is charged to the same budget.
    if(value.paneDirty)recoveries++;
    if(value.lastOpenedRow===row.id||value.paneDirty)await refreshInbox(true);
    let previous,view,proof='',virgin=false,observable=false,watch=new Set();
    const ids=row.ids?.length?row.ids:[row.id];
    for (let opening=0;;opening++) {
    // Outlook virtualizes and reorders its list while a scan is in progress.
    // Reacquire the exact row with a short bounded retry, never a positional click.
    for(let attempt=0;attempt<2;attempt++){
      try{
        const target=value.page.locator(selector);
        if(!await target.count())await revealRow(selector,row);
        await target.waitFor({state:'visible',timeout:1500});
        if((await target.count())!==1)throw new Error('The inbox conversation is ambiguous.');
        // Snapshotted after any retry reload, so whatever a fresh document shows before the click is never taken for this row.
        const listed=new Map((await snapshotRows()).map(item=>[item.id,item.signature]));
        previous={...await value.page.evaluate(paneView,value.provider),rows:listed,known:new Set(listed.keys())};
        // A conversation open before the click keeps its identity even when its own row is in no list this scan reads, as on a Focused/Other inbox.
        for (const id of [value.lastOpenedRow,...previous.selected,...previous.bindings,...previous.owners.flat()]) if (id&&!ids.includes(id)) previous.known.add(id);
        // Only a conversation on screen before the click can repaint into this pane, and mail arriving in it moves its own row.
        watch=new Set([value.lastOpenedRow,...previous.selected,...previous.bindings].filter(id=>id&&!ids.includes(id)));
        // A pane that was empty, held no selection and named no item can only paint the conversation clicked now.
        virgin=!previous.bodies.length&&!previous.selected.length&&!previous.item&&!value.paneDirty;
        // A repaint can only be told from this row's own paint while every conversation that could own it has a row this scan watches.
        observable=virgin||(watch.size>0&&[...watch].every(id=>listed.has(id)));
        await target.click({timeout:2000});
        value.lastOpenedRow=row.id;
        break;
      }catch(error){
        if(attempt===1)throw new Error('An inbox conversation moved or is no longer visible; it will be checked again on a later scan.');
        // A row gathered further down the list is unmounted until the list scrolls
        // back to it; that costs nothing here, and only then is a document spent.
        if(!await revealRow(selector,row))await refreshInbox(true);
      }
    }
    // Only proof opens a read: the pane names this row, nothing else could paint into it, or its content replaced the conversation that was open.
    // The fresh document this row may need is funded before its wait, so the wait cannot spend what the recovery needs.
    const funded=canRecover();
    const clicked=Date.now();
    let baseline=previous.rows,sticky=false,waited=false,standardViewRequested=false;
    const prove=()=>{
      if (!view.bodies.length) return false;
      if (paneProvesRow(view,ids,previous.known)) { proof='named'; return true; }
      // Gmail keeps the check it has today, since no extra wait or reload may be spent there.
      if (!outlook) { if (!paneShowsRow(previous,view,row.id)) return false; proof='shown'; return true; }
      if (paneNamesOther(view,ids,previous.known)) return false;
      if (virgin) { proof='shown'; return true; }
      if (!paneShowsRow(previous,view,row.id)) return false;
      // A heading other than the one the pane carried before the click cannot be that conversation's repaint.
      if (previous.subject&&view.subject&&view.subject!==previous.subject) { proof='shown'; return true; }
      // Nothing names this paint, so it is only taken once the whole pane budget passed with no mail reaching a conversation that was on screen.
      if (!waited||sticky||!observable) return false;
      proof='waited'; return true;
    };
    // Mail arriving in a watched conversation repaints it under a heading this pane cannot be told apart from; the row it moves is the only warning.
    const watchRows=async()=>{
      if (!outlook||!watch.size) return;
      const current=new Map((await snapshotRows()).map(item=>[item.id,item.signature]));
      // A virtualized list unmounts the row instead of updating it, which hides the push; both count as a move.
      sticky||=[...watch].some(id=>baseline.has(id)&&current.get(id)!==baseline.get(id));
      baseline=current;
    };
    for (const paneDeadline=Date.now()+pane;;) {
      view=await value.page.evaluate(paneView,value.provider);
      if (prove()) break;
      // Outlook can replace a Viva/actionable email with an embedded app that
      // renders no mail body. Its own Change view toggle exposes the original.
      // Only use the provider control when the heading identifies this row;
      // controls inside correspondence and another conversation are excluded.
      if(outlook&&!standardViewRequested&&!view.bodies.length&&view.heads.length===1&&identifies(view.heads[0],ids,previous.known)){
        const toggle=value.page.locator('[role="button"][aria-label="Change view"][aria-expanded="true"]:not([role="document"] *):not([aria-label="Message body"] *)');
        if(await toggle.count()===1&&await toggle.isVisible()){
          standardViewRequested=true;
          await toggle.click({timeout:2000});
          continue;
        }
      }
      // The row snapshot costs a round trip, so it is taken only while an unproven pane still has to be told from a repaint.
      await watchRows();
      if (Date.now()>=paneDeadline) { waited=true; prove(); break; }
      // The pane is polled closely right after the click: the gap before it paints is what proves the paint is this row's.
      await sleep(outlook&&Date.now()-clicked<1500 ? 75 : 200);
    }
    if (proof) break;
    // A pane that painted nothing at all needs time, not a fresh document.
    const stuck=view.bodies.length||otherSelected(previous,view,row.id);
    if (opening||!stuck||!funded||!recoverable()) { deadPane||=!view.bodies.length&&Date.now()<workDeadline-RESERVE_MS; value.paneDirty=true; throw refuse(); }
    recoveries++;
    await refreshInbox(true);
    }
    // A pane can hold another conversation's messages too: only those naming this row, or naming no other conversation this scan knows, are read.
    // A message already on screen before the click is read only from a pane that names this row.
    const ownBody=id=>{
      const index=view.bodies.findIndex(([bodyId])=>bodyId===id);
      if (index<0) return false;
      if (view.owners[index].some(own=>identifies(own,ids,previous.known))) return true;
      return !view.owners[index].some(own=>previous.rows.has(own))&&!previous.bodies.some(([bodyId])=>bodyId===id);
    };
    const shown=(await value.page.evaluate(provider=>{
      const gmail=provider==='browser_gmail',selector=gmail?'.a3s':'[role="document"], [aria-label="Message body"]';
      return [...new Set([...document.querySelectorAll(selector)].filter(body=>(body.innerText?.trim()||(!gmail&&body.matches('[aria-label="Message body"]')&&body.closest('[data-test-id="mailMessageBodyContainer"]')))&&getComputedStyle(body).visibility!=='hidden'&&[...body.getClientRects()].some(rect=>rect.width>0&&(rect.height>0||(body.matches('[aria-label="Message body"]')&&body.closest('[data-test-id="mailMessageBodyContainer"]'))))).flatMap(body=>{
        let scope=body.closest('[aria-label="Email message"]')||body.parentElement;
        while(scope&&scope!==document.body&&!scope.querySelector('[id^="MSG_"][id$="_FROM"]'))scope=scope.parentElement;
        if(scope===document.body||scope?.querySelectorAll(selector).length!==1)scope=null;
        if(!gmail&&scope){const copy=scope.cloneNode(true);copy.querySelectorAll(selector).forEach(node=>node.remove());if(/This message hasn['’]t been sent\./.test(copy.textContent))return [];}
        const node=body.closest('[data-legacy-message-id], [data-message-id], [data-item-id]');
        const id=node?.getAttribute('data-legacy-message-id')||node?.getAttribute('data-message-id')||node?.getAttribute('data-item-id')||scope?.querySelector('[id^="MSG_"][id$="_FROM"]')?.id.slice(4,-5);
        return id?[id]:[];
      }))];
    },value.provider));
    const visibleIds=shown.filter(ownBody);
    // A message that painted after the proof was taken is judged by the next scan instead, so this row stays unchecked.
    if(shown.some(id=>!view.bodies.some(([bodyId])=>bodyId===id)))noteFailure(row,refuse());
    // A pane whose identified messages all belong to another conversation proves nothing about this row.
    if(!visibleIds.length&&view.bodies.some(([id])=>id))throw refuse();
    const messageStart=(messageOffsets[rowKey(row)]||0)%Math.max(1,visibleIds.length);
    const messageBatch=visibleIds.length?visibleIds.slice(messageStart,messageStart+20):[''];
    let messageCount=0;
    for(const expectedMessageId of messageBatch){
    if(Date.now()>=workDeadline-RESERVE_MS){rowWarnings.push('The scan time limit was reached; remaining messages will be checked on a later scan.');break;}
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
        // An actual empty message is valid, but Outlook can mount its body
        // before hydrating its contents. Do not freeze that initial scaffold
        // as the immutable original while it is still painting.
        if(message.body===''&&Date.now()-readStarted<1500){message=null;await sleep(150);continue;}
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
    // The pane can repaint while a message is read, so the proof is taken again over what was actually read.
    if (outlook) {
      const after=await value.page.evaluate(paneView,value.provider);
      const held=(proof==='named' ? paneProvesRow(after,ids,previous.known) : !paneNamesOther(after,ids,previous.known)&&!otherSelected(previous,after,row.id))
        // A tenant that renders no message envelope leaves the pane body without an id, so its own text is what identifies it.
        &&after.bodies.some(([id,text])=>message.message_id&&id ? id===message.message_id : text===message.body);
      if (!held) { value.paneDirty=true; noteFailure(row,refuse()); break; }
    }
    parsedCount++;
    const source_id=sourceHash(value.provider,row,message);
    const browser_reference={contract:REPLY_CONTRACT,provider:value.provider,row_attr:row.attr,row_id:row.id,message_id:message.message_id,source_hash:source_id,content_hash:contentHash(message),...(splitInbox?{inbox_tab:tabName}:{})};
    if (!seen.includes(source_id)) { if(message.sender.toLowerCase()!==refreshed.email.toLowerCase()) messages.push({source_id,...message,browser_reference}); seen.push(source_id); }
    }catch(error){
      const identity=await status(value,request.connection);
      if(identity.state!=='connected')throw new Error('Mailbox sign-in changed while checking mail. Reconnect the browser.');
      noteFailure(row,error);
    }
    messageCount++;
    }
    if(messageStart+messageCount<visibleIds.length){
      messageOffsets[rowKey(row)]=messageStart+messageCount;
      incomplete.add(row.id);
      rowWarnings.push('This conversation has more messages to import; checking continues on the next scan.');
    }else delete messageOffsets[rowKey(row)];
    } catch(error) {
      // Message layout/virtualization failures are recoverable, but never hide
      // a closed browser, a changed account or a sign-in redirect as bad mail.
      const identity=await status(value,request.connection);
      if(identity.state!=='connected')throw new Error('Mailbox sign-in changed while checking mail. Reconnect the browser.');
      noteFailure(row,error);
    }
    checked++;
    if(tabSettled&&rowWarnings.length===warningCount&&parsedCount>parsedBefore){rowCache[rowKey(row)]={signature:row.signature,checked_at:Date.now()};failures.delete(rowKey(row));delete rowFailures[rowKey(row)];resolved.add(rowKey(row));}
    else delete rowCache[rowKey(row)];
    if(Date.now()>=workDeadline-RESERVE_MS)break;
    try{await refreshInbox();}catch(error){
      const identity=await status(value,request.connection);
      if(identity.state!=='connected'||!parsedCount)throw error;
      rowWarnings.push('Inbox refresh did not finish; imported messages were preserved and remaining conversations will be checked on the next scan.');
      break;
    }
    // Add arrivals/reordered visible rows without retrying the same failed row
    // indefinitely. Unreadable rows are never added to the durable seen set.
    const currentRows=await snapshotRows();
    const arrivals=currentRows.length<=ROW_LIMIT?currentRows:currentRows.slice(0,10);
    const additions=arrivals.filter(item=>changed(item)&&!cooling(item)&&!attempted.has(item.id)&&!ordered.some(queued=>queued.id===item.id));
    ordered.splice(ordered.indexOf(row)+1,0,...additions);
  }
  // A list whose conversations never paint at all may sit on an expired session; only a fresh document can show its sign-in redirect.
  // A scan whose rows only refused a pane they could not prove keeps its cursor instead, so the next scan starts no worse than this one.
  if (!parsedCount && rowWarnings.length && (deadPane||refusals<rowWarnings.length)) {
    if (deadPane) { value.inboxReady=false; value.lastInboxRefresh=0; }
    // Preserve traversal progress when a whole batch fails ahead of older mail.
    if(!collected.capped&&!walk.pending.length&&checked>=ordered.length)throw new Error(rowWarnings[0]);
  }
  if (checked<ordered.length && Date.now()>=workDeadline-RESERVE_MS) notes.push('The scan time limit was reached; remaining conversations will be checked on the next scan.');
  // Work this scan could not finish is reported so the poller drains the backlog
  // instead of waiting a whole interval for each further batch.
  const has_more=checked<ordered.length||collected.capped||incomplete.size>0;
  progress[tabName]={top:collected.top,after:collected.after,capped:collected.capped,pending:ordered.filter(row=>!attempted.has(row.id)||incomplete.has(row.id)).slice(0,ROW_LIMIT).map(row=>({...row,top:validPosition(row.top)?row.top:0}))};
  return {messages,failed_messages:[...failures.values()],resolved_failures:[...resolved],has_more,
    cursor:JSON.stringify({contract:SYNC_CONTRACT,seen:seen.slice(-5000),offset:nextOffset,...cacheFields()}),
    warnings:[...rowWarnings,...notes,'Browser mail walks up to '+ROW_LIMIT+' inbox conversations per tab per scan and reconciles at most one unchanged conversation per tab per scan. Opening mail may mark it read. Website layout changes can interrupt checking.']};
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
    // The bridge waits 150 seconds from the request; the inbox wait leaves room for reading, composing and the send acknowledgement.
    const rescan='Rescan the inbox before replying.';
    await ensureInbox(value,{deadline:(request._startedAt||Date.now())+70000,follow:rescan});
    await status(value,request.connection);
    if(value.provider==='browser_outlook'&&ref.inbox_tab){
      if(!['Focused','Other'].includes(ref.inbox_tab))throw new Error('The original inbox tab reference is unsupported.');
      const tab=value.page.getByRole('tab',{name:ref.inbox_tab==='Other'?/^Other(?:\s+\d+)?$/:/^Focused(?:\s+\d+)?$/});
      if(await tab.count()!==1)throw new Error('The original inbox tab is no longer available. Rescan before replying.');
      if(await tab.getAttribute('aria-selected')!=='true'){
        try{if(!await selectTab(value,tab,ref.inbox_tab,1))throw new Error('The original inbox tab list did not refresh.');}
        catch(error){throw new Error(`${error.message} ${rescan}`);}
      }
      await awaitInbox(value,policy(value.provider).settle,rescan);
    }
    const row=value.page.locator(rowSelector(value.provider,ref.row_attr,ref.row_id));
    if((await visibleElements(row)).length!==1) throw new Error('The original conversation is no longer uniquely visible. Rescan the inbox before replying.');
    await row.click();
    value.lastOpenedRow=ref.row_id;
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
    // Timer polling like every other wait here; a hidden headed window may not deliver animation frames.
    await value.page.waitForFunction(({before})=>[...document.querySelectorAll('[role="status"],[role="alert"]')].some(e=>e.getClientRects().length&&!e.closest('[role="document"],.a3s,[contenteditable="true"]')&&/^(Message sent\.?|Your message has been sent\.?)$/i.test(e.innerText.trim())&&!before.includes(e.innerText.trim())),{before},{timeout:8000,polling:200});
    if((await visibleElements(value.page.locator(editorSelector(value.provider)))).length)throw new Error('The reply editor remained open after the send notice.');
    record.status='sent';record.acknowledged_at=new Date().toISOString();saveSubmission(file,record);
    return {status:'sent',submission_id:request.submission_id,evidence:'ui_acknowledgement',message:'The mailbox UI confirmed sending. Recipient delivery is not confirmed.'};
  } catch(error) {
    return {status:dispatched?'unknown':'not_sent',submission_id:request.submission_id,error:dispatched?'The send outcome is uncertain. Check Sent mail; Nexus will not resend this approval automatically.':publicError(error).slice(0,400)};
  }
}
async function close() { await Promise.allSettled([...sessions.values()].map(s=>s.context.close())); sessions.clear(); }
if (require.main === module) {
  const lines = readline.createInterface({input:process.stdin});
  let chain=Promise.resolve();
  lines.on('line',line=> { chain=chain.then(async()=> {
    try { process.stdout.write(JSON.stringify({result:await handle(JSON.parse(line))})+'\n'); }
    catch(error) { process.stdout.write(JSON.stringify({error:publicError(error)})+'\n'); }
  }); });
  // Shutdown must interrupt an in-flight navigation, not leave its browser
  // running until a long mailbox operation finishes.
  lines.on('close',()=>close().finally(()=>process.exit(0)));
  process.on('SIGTERM',()=>close().finally(()=>process.exit(0)));
}
module.exports={readIdentity,readRows,scrollList,readMessage,inboxState,inboxSignals,selectedFolderKey,selectedTabName,paneView,paneShowsRow,paneProvesRow,paneNamesOther,waitForInbox,onInbox,inboxUrl,budgets,hydrationBudget,identityBudget,watchReloads,handle,close,status,operate,resolveSenderCard,session,sendReviewed,readComposer,safeRowWarning,publicError};
