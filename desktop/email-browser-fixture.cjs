'use strict';
// Synthetic provider UI for offline browser-contract tests. Every request is
// fulfilled locally. It never connects a mailbox or submits an actual email.
const fs=require('node:fs');
const escape=value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
function createFixture(options={}) {
  const state={provider:'browser_outlook',email:'owner@example.test',sender:'sender@example.test',recipient:'sender@example.test',subject:'Question',body:'Please reply to this question.',messageId:'message-one',rowId:'thread-one',acknowledge:true,existingComposer:false,changeRecipientOnInput:false,sendCount:0,...options.state};
  const receiptPath=options.receiptPath;
  function html(sentFolder=false) {
    const gmail=state.provider==='browser_gmail';
    // Optional pictures inside the body and file cards beside it (state.images / state.files).
    const pictures=(state.images||[]).map(image=>image.late
      // Like Outlook: a hidden 1 x 1 placeholder, replaced by the real picture a moment after the text paints.
      ?`<img data-imagetype="AttachmentByCid" data-loadstatus="pending" style="display:none" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAEALAAAAAABAAEAAAIBTAA7" data-late="${escape(image.src)}">`
      :`<img src="${escape(image.src)}" alt="${escape(image.alt||'')}" width="${image.width||120}" height="${image.height||90}">`).join('');
    const cards=(state.files||[]).map((file,index)=>gmail
      ?`<span class="aZo" download_url="${escape((file.type||'application/octet-stream')+':'+file.name+':https://mail.google.com/__nexus_test_file__/'+index)}">${escape(file.name)}</span>`
      :`<div role="option" title="${escape(file.name)}">${escape(file.name)}<button aria-label="More actions" onclick="document.getElementById('menu').innerHTML='<div role=&quot;menuitem&quot; onclick=&quot;fetchFile(${index})&quot;>Download</div>'">...</button></div>`).join('');
    const well=cards?(gmail?`<div class="aQH">${cards}</div>`:`<div role="listbox" aria-label="Attachments">${cards}</div><div role="menu" id="menu"></div>`):'';
    const picker=state.attachInput?`<input type="file" multiple onchange="attachFiles(this)"><div id="chips"></div>`:'';
    const identity=gmail?`<button aria-label="Google Account: Owner (${escape(state.email)})">Account</button>`:`<button id="mectrl_main_trigger" aria-label="${escape(state.email)}">Account</button>`;
    const editor=`<section id="reply-compose"><span ${gmail?'email':'data-email'}="${escape(state.recipient)}">${escape(state.recipient)}</span>${state.hiddenBcc?'<input type="hidden" name="bcc" value="hidden@example.test">':''}<div style="white-space:pre-wrap" role="textbox" aria-label="${gmail?'Message Body':'Message body'}" contenteditable="true"></div>${picker}<button aria-label="Send" onclick="sendReply()">Send</button></section>`;
    const message=gmail?`<h2 class="hP">${escape(state.subject)}</h2><article data-legacy-message-id="${escape(state.messageId)}"><span class="gD" email="${escape(state.sender)}">Sender</span><div class="a3s">${escape(state.body)}${pictures}</div>${well}<button aria-label="Reply" onclick="openReply()">Reply</button></article>`:`<h2 data-testid="conversation-subject">${escape(state.subject)}</h2><article aria-label="Email message" data-message-id="${escape(state.messageId)}"><span id="MSG_${escape(state.messageId)}_FROM" email="${escape(state.sender)}">Sender</span><div role="document">${escape(state.body)}${pictures}</div>${well}<button aria-label="Reply" onclick="openReply()">Reply</button></article>`;
    const row=gmail?`<table><tr data-legacy-thread-id="${escape(state.rowId)}" onclick="openMessage()"><td>Question</td></tr></table>`:`<div role="option" data-convid="${escape(state.rowId)}" onclick="openMessage()">Question</div>`;
    if(sentFolder)return `${identity}<div role="treeitem" aria-selected="true">Sent Items</div><main role="main"><div id="rows"></div><div id="pane"></div></main><script>
      let sent;
      function openSent(){if(!sent?.lastSent)return;const p=document.getElementById('pane');p.replaceChildren();const article=document.createElement('article');article.setAttribute('data-convid',${JSON.stringify(state.rowId)});article.innerHTML='<span id="MSG_sent-'+sent.sendCount+'_SUBJECT">Question</span><span id="MSG_sent-'+sent.sendCount+'_FROM" email="${escape(state.email)}">Owner</span><div id="MSG_sent-'+sent.sendCount+'_TO"></div><div id="MSG_sent-'+sent.sendCount+'_DATETIME"></div><div role="document"><pre></pre></div>';article.querySelector('[id$="_TO"]').textContent=sent.lastSent.recipient;article.querySelector('[id$="_DATETIME"]').textContent='Time '+sent.sendCount;article.querySelector('pre').textContent=sent.lastSent.body;p.append(article);}
      async function refresh(){sent=await(await fetch('/__nexus_test_sent__')).json();if(sent.lastSent){document.getElementById('rows').innerHTML=${JSON.stringify(row.replace('openMessage()', 'openSent()'))};if(document.getElementById('pane').children.length)openSent();}}
      refresh();setInterval(refresh,100);
      </script>`;
    return `${identity}${state.sentFolder?'<div role="treeitem" onclick="location.href=\'/mail/sentitems\'">Sent Items</div>':''}<main role="main">${row}<div id="pane"></div><div id="compose"></div><div id="receipt"></div></main><script>
      const message=${JSON.stringify(message)},editor=${JSON.stringify(editor)};
      function openMessage(){setTimeout(()=>{for(const image of document.querySelectorAll('img[data-late]')){image.src=image.getAttribute('data-late');image.removeAttribute('data-loadstatus');image.style.display='';}},${state.lateMs||800});document.querySelector('#pane').innerHTML=message;document.querySelector('#pane article').setAttribute('${gmail?'data-legacy-thread-id':'data-convid'}',${JSON.stringify(state.rowId)});}
      function openReply(){document.querySelector('#compose').innerHTML=editor;${state.changeRecipientOnInput?`document.querySelector('[contenteditable]').addEventListener('input',()=>{document.querySelector('#reply-compose span').setAttribute('${gmail?'email':'data-email'}','changed@example.test');});`:''}}
      // An attachment response navigates into a download; the page itself stays.
      function fetchFile(index){location.href='/__nexus_test_file__/'+index;}
      function attachFiles(input){for(const file of input.files){const chip=document.createElement('span');chip.title=file.name;chip.textContent=file.name+' ('+file.size+' bytes)';document.querySelector('#chips').append(chip);(window.attached=window.attached||[]).push(file.name);}}
      async function sendReply(){${state.dropSend?'return;':''}const body=document.querySelector('[contenteditable]').innerText; await fetch('/__nexus_test_send__',{method:'POST',body:JSON.stringify({body,recipient:document.querySelector('#reply-compose span').getAttribute('${gmail?'email':'data-email'}'),...(window.attached?{files:window.attached}:{})})});${state.leaveComposer?'':'document.querySelector("#compose").replaceChildren();'}${state.acknowledge?`document.querySelector('#receipt').innerHTML='<div role="status">Message sent.</div>';`:''}}
      ${state.existingComposer?'openReply();':''}
      </script>`;
  }
  async function route(requestRoute) {
    const request=requestRoute.request();
    if(new URL(request.url()).pathname==='/__nexus_test_sent__')return requestRoute.fulfill({contentType:'application/json',body:JSON.stringify({sendCount:state.sendCount,lastSent:state.lastSent})});
    const served=new URL(request.url()).pathname.match(/^\/__nexus_test_file__\/(\d+)$/);
    if(served&&state.files?.[Number(served[1])]) {
      const file=state.files[Number(served[1])];
      return requestRoute.fulfill({status:200,contentType:file.type||'application/octet-stream',headers:{'content-disposition':'attachment; filename="'+file.name+'"'},body:Buffer.from(file.content)});
    }
    if(new URL(request.url()).pathname==='/__nexus_test_send__') {
      state.sendCount++;state.lastSent=JSON.parse(request.postData()||'{}');
      if(receiptPath)fs.writeFileSync(receiptPath,JSON.stringify({sendCount:state.sendCount,...state.lastSent}));
      return requestRoute.fulfill({contentType:'application/json',body:'{}'});
    }
    return requestRoute.fulfill({contentType:'text/html',body:html(state.sentFolder&&new URL(request.url()).pathname==='/mail/sentitems')});
  }
  return {state,route,html};
}
module.exports={createFixture};
