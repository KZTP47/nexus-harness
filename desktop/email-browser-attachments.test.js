'use strict';
// Pictures and files in browser mail: read with a message, and attached to an
// approved reply before Send. Synthetic mailbox pages only; nothing is sent.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {createHash}=require('node:crypto');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const {chromium}=require('playwright-core');
const {findInstalledBrowser}=require('./external-browser');
const worker=require('./email-browser-worker');
const {createFixture}=require('./email-browser-fixture.cjs');
const connectionId='c'.repeat(32);
const submissionId='d'.repeat(32);
const sha=buffer=>createHash('sha256').update(buffer).digest('hex');
// A 64 x 64 PNG: large enough to count as content, not a tracking pixel.
const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAABRUlEQVR4nNXay3HCQBCE4eYPxKRBaCYCUiMNIsEHV7nAILGP2Xn0RVrtTFd9dx3udx3PV5UNx/P1djmpbJBU2sDvo66Bv7eiBh4PFQ38O5cz8PqploG3XwsZ2LqoYmDnroSB/ev8Bj5OJDfQMpTZQONcWgPtozkNdE0nNNC7kM3AwE4qA2NreQwMbyYxMLOcwcDkfriB+YpYAyYtgQasiqIMGHaFGLCt8zdg3uhsYEWpp4FFvW4G1lX7GFja7mBgabuDAa3PUgNyyToD8soiA3LMCgPyjbkBucfWgCJiaEBBsTKguJgYIgEmhmCApg3xAM0ZUgA0YcgC0KghEUBDhlwA9RvSAdRpyAhQjyEpQM2GvAC1GVID1GDIDtAnQwGAdg01ANo2lAFow1AJoHeGYgC9GOoB9GwoCdCD4fD1Xfjf+9vl9AMF8cDbNuIbfgAAAABJRU5ErkJggg==','base64');
async function session(provider,state) {
  const profile=fs.mkdtempSync(path.join(os.tmpdir(),'nexus-browser-files-'));
  const incoming=fs.mkdtempSync(path.join(os.tmpdir(),'nexus-browser-incoming-'));
  const fixture=createFixture({state:{provider,...state}});
  const context=await chromium.launchPersistentContext(profile,{executablePath:findInstalledBrowser().executable,headless:true,acceptDownloads:true});
  const close=async()=>{await context.close();for(const folder of [profile,incoming])fs.rmSync(folder,{recursive:true,force:true});};
  try {
    await context.route('**/*',fixture.route);
    const page=context.pages()[0];
    await page.goto(provider==='browser_gmail'?'https://mail.google.com/mail/u/0/#inbox':'https://outlook.office.com/mail/inbox');
    await page.locator(provider==='browser_gmail'?'tr[data-legacy-thread-id]':'[role="option"][data-convid]').click({trial:true,timeout:15000});
    const value={context,page,profile,provider,mode:'headless'};
    const request={command:'sync',connection:{id:connectionId,provider,email:'owner@example.test',browser_mode:'headless'}};
    return {value,fixture,request,incoming,close};
  } catch(error) { await close(); throw error; }
}

test('a scan saves a message\'s pictures and files by checksum, beside its unchanged text',{timeout:120000},async()=>{
  for(const provider of ['browser_outlook','browser_gmail']) {
    const files=[{name:'invoice.txt',type:'text/plain',content:'Invoice 42: 100 EUR'},{name:'photo.png',type:'image/png',content:png}];
    const f=await session(provider,{images:[{src:'data:image/png;base64,'+png.toString('base64'),alt:'chart.png',width:64,height:64},
                                             {src:'data:image/png;base64,'+png.toString('base64'),alt:'pixel',width:1,height:1}],files});
    try {
      const plain=(await worker.operate(f.value,f.request)).messages[0];
      assert.equal(plain.attachments,undefined,'no folder given: text only, as before');
      assert.equal(plain.body,'Please reply to this question.');
      await f.value.page.reload();
      const result=await worker.operate(f.value,{...f.request,attachments_dir:f.incoming});
      const message=result.messages[0];
      assert.equal(message.body,plain.body,'pictures never change the imported text');
      assert.equal(message.source_id,plain.source_id,'nor the message identity');
      const byName=Object.fromEntries(message.attachments.map(item=>[item.name,item]));
      assert.equal(byName['chart.png'].inline,true);
      assert.equal(byName['chart.png'].sha256,sha(png));
      assert.ok(!('pixel' in byName),'a tracking pixel is skipped');
      assert.equal(byName['invoice.txt'].sha256,sha(Buffer.from(files[0].content)),provider+': '+JSON.stringify(message.attachments));
      assert.equal(byName['invoice.txt'].inline,false);
      for(const item of message.attachments.filter(one=>one.sha256))
        assert.equal(sha(fs.readFileSync(path.join(f.incoming,item.sha256))),item.sha256);
      // The same picture as a file card is stored once.
      assert.equal(message.attachments.filter(item=>item.sha256===sha(png)).length,1);
    } finally { await f.close(); }
  }
});

test('an approved reply gets its files through the composer\'s file picker before Send; a missing file stops before Send',{timeout:120000},async()=>{
  const folder=fs.mkdtempSync(path.join(os.tmpdir(),'nexus-reply-files-'));
  const report=path.join(folder,'report.txt');fs.writeFileSync(report,'Quarterly report');
  try {
    for(const provider of ['browser_outlook','browser_gmail']) {
      const f=await session(provider,{attachInput:true});
      try {
        const incoming=(await worker.operate(f.value,f.request)).messages[0];
        const body='Here is the report.';
        const attachments=[{path:report,name:'report.txt',type:'text/plain',sha256:sha(fs.readFileSync(report))}];
        const missing=await worker.operate(f.value,{...f.request,command:'send',incoming,body,submission_id:submissionId,
          attachments:[{...attachments[0],path:path.join(folder,'gone.txt'),name:'gone.txt'}]});
        assert.equal(missing.status,'not_sent');
        assert.match(missing.error,/missing/);
        assert.equal(f.fixture.state.sendCount,0);
        const sent=await worker.operate(f.value,{...f.request,command:'send',incoming,body,submission_id:submissionId,attachments});
        assert.equal(sent.status,'sent',JSON.stringify(sent));
        assert.deepEqual(f.fixture.state.lastSent,{body,recipient:'sender@example.test',files:['report.txt']});
        assert.equal(f.fixture.state.sendCount,1);
        // The files are part of the approval binding: the same id with other files is not resent.
        const other=await worker.operate(f.value,{...f.request,command:'send',incoming,body,submission_id:submissionId});
        assert.equal(other.status,'unknown');
        assert.equal(f.fixture.state.sendCount,1);
      } finally { await f.close(); }
    }
  } finally { fs.rmSync(folder,{recursive:true,force:true}); }
});

test('a composer that shows no way to attach files stops before Send',{timeout:90000},async()=>{
  const folder=fs.mkdtempSync(path.join(os.tmpdir(),'nexus-reply-files-'));
  const report=path.join(folder,'report.txt');fs.writeFileSync(report,'Quarterly report');
  const f=await session('browser_outlook',{});
  try {
    const incoming=(await worker.operate(f.value,f.request)).messages[0];
    const result=await worker.operate(f.value,{...f.request,command:'send',incoming,body:'Attached.',submission_id:submissionId,
      attachments:[{path:report,name:'report.txt',type:'text/plain',sha256:sha(fs.readFileSync(report))}]});
    assert.equal(result.status,'not_sent');
    assert.match(result.error,/attach/i);
    assert.equal(f.fixture.state.sendCount,0);
  } finally { await f.close(); fs.rmSync(folder,{recursive:true,force:true}); }
});

test('a picture the mailbox loads after the text is waited for, and stored mail can have its files read again',{timeout:120000},async()=>{
  const f=await session('browser_outlook',{images:[{src:'data:image/png;base64,'+png.toString('base64'),late:true}]});
  try {
    const message=(await worker.operate(f.value,{...f.request,attachments_dir:f.incoming})).messages[0];
    assert.deepEqual(message.attachments.map(item=>[item.sha256,item.inline]),[[sha(png),true]],'not skipped as a 1 x 1 pixel');
    fs.rmSync(path.join(f.incoming,sha(png)));
    // A message stored before its files were read: reopened by its reference and proved unchanged.
    await f.value.page.reload();
    const again=await worker.operate(f.value,{...f.request,command:'attachments',incoming:message,attachments_dir:f.incoming});
    assert.equal(again.status,'read');
    assert.deepEqual(again.attachments.map(item=>item.sha256),[sha(png)]);
    assert.ok(fs.existsSync(path.join(f.incoming,sha(png))));
    await assert.rejects(worker.operate(f.value,{...f.request,command:'attachments',incoming:{...message,body:'Different text'},attachments_dir:f.incoming}),/unchanged/);
  } finally { await f.close(); }
});
