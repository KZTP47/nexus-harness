'use strict';
// Synthetic provider UI for offline browser-contract tests. Every request is
// fulfilled locally. It never connects a mailbox or submits an actual email.
const fs=require('node:fs');
const escape=value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
function createFixture(options={}) {
  const state={provider:'browser_outlook',email:'owner@example.test',sender:'sender@example.test',recipient:'sender@example.test',subject:'Question',body:'Please reply to this question.',messageId:'message-one',rowId:'thread-one',acknowledge:true,existingComposer:false,changeRecipientOnInput:false,sendCount:0,...options.state};
  const receiptPath=options.receiptPath;
  function html() {
    const gmail=state.provider==='browser_gmail';
    const identity=gmail?`<button aria-label="Google Account: Owner (${escape(state.email)})">Account</button>`:`<button id="mectrl_main_trigger" aria-label="${escape(state.email)}">Account</button>`;
    const editor=`<section id="reply-compose"><span ${gmail?'email':'data-email'}="${escape(state.recipient)}">${escape(state.recipient)}</span>${state.hiddenBcc?'<input type="hidden" name="bcc" value="hidden@example.test">':''}<div style="white-space:pre-wrap" role="textbox" aria-label="${gmail?'Message Body':'Message body'}" contenteditable="true"></div><button aria-label="Send" onclick="sendReply()">Send</button></section>`;
    const message=gmail?`<h2 class="hP">${escape(state.subject)}</h2><article data-legacy-message-id="${escape(state.messageId)}"><span class="gD" email="${escape(state.sender)}">Sender</span><div class="a3s">${escape(state.body)}</div><button aria-label="Reply" onclick="openReply()">Reply</button></article>`:`<h2 data-testid="conversation-subject">${escape(state.subject)}</h2><article aria-label="Email message" data-message-id="${escape(state.messageId)}"><span id="MSG_${escape(state.messageId)}_FROM" email="${escape(state.sender)}">Sender</span><div role="document">${escape(state.body)}</div><button aria-label="Reply" onclick="openReply()">Reply</button></article>`;
    const row=gmail?`<table><tr data-legacy-thread-id="${escape(state.rowId)}" onclick="openMessage()"><td>Question</td></tr></table>`:`<div role="option" data-convid="${escape(state.rowId)}" onclick="openMessage()">Question</div>`;
    return `${identity}<main role="main">${row}<div id="pane"></div><div id="compose"></div><div id="receipt"></div></main><script>
      const message=${JSON.stringify(message)},editor=${JSON.stringify(editor)};
      function openMessage(){document.querySelector('#pane').innerHTML=message;document.querySelector('#pane article').setAttribute('${gmail?'data-legacy-thread-id':'data-convid'}',${JSON.stringify(state.rowId)});}
      function openReply(){document.querySelector('#compose').innerHTML=editor;${state.changeRecipientOnInput?`document.querySelector('[contenteditable]').addEventListener('input',()=>{document.querySelector('#reply-compose span').setAttribute('${gmail?'email':'data-email'}','changed@example.test');});`:''}}
      async function sendReply(){const body=document.querySelector('[contenteditable]').innerText; await fetch('/__nexus_test_send__',{method:'POST',body:JSON.stringify({body,recipient:document.querySelector('#reply-compose span').getAttribute('${gmail?'email':'data-email'}')})});document.querySelector('#compose').replaceChildren();${state.acknowledge?`document.querySelector('#receipt').innerHTML='<div role="status">Message sent.</div>';`:''}}
      ${state.existingComposer?'openReply();':''}
      </script>`;
  }
  async function route(requestRoute) {
    const request=requestRoute.request();
    if(new URL(request.url()).pathname==='/__nexus_test_send__') {
      state.sendCount++;state.lastSent=JSON.parse(request.postData()||'{}');
      if(receiptPath)fs.writeFileSync(receiptPath,JSON.stringify({sendCount:state.sendCount,...state.lastSent}));
      return requestRoute.fulfill({contentType:'application/json',body:'{}'});
    }
    return requestRoute.fulfill({contentType:'text/html',body:html()});
  }
  return {state,route,html};
}
module.exports={createFixture};
