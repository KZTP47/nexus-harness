"use strict";
const assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path"),os=require("node:os"),test=require("node:test");
const {chromium}=require("playwright-core");
const ui=process.env.NEXUS_CHAT_UI_ROOT||path.resolve(__dirname,"../src/our_harness/ui"),runtime=path.join(__dirname,"build-output/win-unpacked/resources/runtime");
test("prompt library saves, edits, searches, inserts exact text, protects other chats and handles save conflicts",{timeout:30000},async()=>{
  const manifest=JSON.parse(fs.readFileSync(path.join(runtime,"NEXUS_RUNTIME.json"),"utf8"));
  const browser=await chromium.launch({executablePath:process.env.NEXUS_TEST_CHROMIUM||path.join(runtime,"playwright",manifest.playwright.chromium_executable),headless:true});
  const output=fs.mkdtempSync(path.join(os.tmpdir(),"nexus-prompt-library-"));
  try{
    const page=await browser.newPage({viewport:{width:1120,height:820}});
    await page.setContent('<textarea id="composer">Existing draft: </textarea>');
    await page.addStyleTag({content:fs.readFileSync(path.join(ui,"styles.css"),"utf8")});
    const source=fs.readFileSync(path.join(ui,"app.js"),"utf8");
    await page.addScriptTag({content:source.slice(source.indexOf("async function openPromptLibrary"),source.indexOf("function fillChatGoalPanel"))+`
      function make(tag,cls='',text=''){const n=document.createElement(tag);n.className=cls;n.textContent=text;return n;}
      window.rows=[];window.calls=[];window.current=true;window.fail=false;
      async function request(url,options){
        if(!options)return {prompts:rows};
        const p=JSON.parse(options.body);calls.push(p);if(fail)throw new Error('This saved prompt changed in another window.');
        if(p.action==='delete'){rows=rows.filter(r=>r.id!==p.id);return {deleted:p.id};}
        const prompt={id:p.id||String(rows.length+1),title:p.title,body:p.body,revision:(p.revision||0)+1};
        rows=[prompt,...rows.filter(r=>r.id!==prompt.id)];return {prompt};
      }
      window.openLibrary=()=>openPromptLibrary(document.getElementById('composer'),()=>current);
    `});
    await page.evaluate(()=>{const c=document.getElementById('composer');c.selectionStart=c.selectionEnd=c.value.length;openLibrary();});
    const windowBefore=await page.locator('dialog').boundingBox();
    const editorBefore=await page.getByLabel('Prompt text',{exact:true}).boundingBox();
    await page.mouse.move(windowBefore.x+windowBefore.width-4,windowBefore.y+windowBefore.height-4);
    await page.mouse.down();
    await page.mouse.move(windowBefore.x+windowBefore.width+90,windowBefore.y+windowBefore.height+55,{steps:12});
    await page.mouse.up();
    const windowAfter=await page.locator('dialog').boundingBox();
    const editorAfter=await page.getByLabel('Prompt text',{exact:true}).boundingBox();
    assert.ok(windowAfter.width>windowBefore.width+30 && windowAfter.height>windowBefore.height+20,JSON.stringify({windowBefore,windowAfter}));
    assert.ok(editorAfter.width>editorBefore.width && editorAfter.height>editorBefore.height,JSON.stringify({editorBefore,editorAfter}));
    assert.equal(await page.getByLabel('Prompt text',{exact:true}).inputValue(),'Existing draft: ');
    await page.getByLabel('Prompt title',{exact:true}).fill('Review changes');
    const exact='Read the source.\nCheck café and 日本語.\n';
    await page.getByLabel('Prompt text',{exact:true}).fill(exact);
    await page.getByRole('button',{name:'Save prompt',exact:true}).click();
    await page.getByRole('button',{name:'Use in chat',exact:true}).click();
    assert.equal(await page.locator('#composer').inputValue(),'Existing draft: '+exact);
    await page.locator('dialog').waitFor({state:'detached'});
    await page.evaluate(()=>openLibrary());
    await page.getByRole('button',{name:'Review changes',exact:true}).click();
    await page.getByRole('button',{name:'New prompt',exact:true}).click();
    assert.equal(await page.getByLabel('Prompt title',{exact:true}).inputValue(),'');
    assert.equal(await page.getByLabel('Prompt text',{exact:true}).inputValue(),'');
    assert.equal(await page.evaluate(()=>rows.length),1);
    await page.getByRole('button',{name:'Review changes',exact:true}).click();
    await page.getByLabel('Prompt text',{exact:true}).fill(exact+'Run the tests.');
    await page.getByRole('button',{name:'Save prompt',exact:true}).click();
    assert.equal(await page.evaluate(()=>rows[0].revision),2);
    await page.getByLabel('Find a saved prompt').fill('missing');
    assert.match(await page.locator('.prompt-library-list').innerText(),/No matching/);
    await page.getByLabel('Find a saved prompt').fill('日本語');
    assert.equal(await page.getByRole('button',{name:'Review changes',exact:true}).count(),1);
    await page.evaluate(()=>{current=false;});
    const original=await page.locator('#composer').inputValue();
    await page.getByRole('button',{name:'Use in chat',exact:true}).click();
    assert.match(await page.locator('dialog [role=status]').innerText(),/active chat changed/);
    assert.equal(await page.locator('#composer').inputValue(),original);
    await page.evaluate(()=>{current=true;fail=true;});
    await page.getByLabel('Prompt text',{exact:true}).fill('Unsaved conflict draft');
    await page.getByRole('button',{name:'Save prompt',exact:true}).click();
    assert.match(await page.locator('dialog [role=status]').innerText(),/another window/);
    assert.equal(await page.getByLabel('Prompt text',{exact:true}).inputValue(),'Unsaved conflict draft');
    await page.evaluate(()=>{fail=false;});
    await page.getByRole('button',{name:'Save prompt',exact:true}).click();
    await page.getByLabel('Find a saved prompt').fill('');
    for(const [width,height] of [[1120,820],[390,820],[390,420]]){
      await page.setViewportSize({width,height});
      assert.ok(await page.locator('dialog').evaluate(n=>n.scrollWidth<=n.clientWidth+1));
      const bounds=await page.locator('dialog').boundingBox();
      const close=await page.getByRole('button',{name:'Close library',exact:true}).boundingBox();
      const heading=await page.getByRole('heading',{name:'Prompt library',exact:true}).boundingBox();
      if(width<600){
        const listPane=await page.locator('.prompt-library-grid > div:first-child').boundingBox();
        const editPane=await page.locator('.prompt-library-editor').boundingBox();
        assert.ok(editPane.y>=listPane.y+listPane.height,JSON.stringify({listPane,editPane}));
      }
      assert.ok(bounds.x>=0 && bounds.y>=0 && bounds.x+bounds.width<=width+1 && bounds.y+bounds.height<=height+1,JSON.stringify(bounds));
      assert.ok(close.x>heading.x+heading.width && close.y<bounds.y+55 && bounds.x+bounds.width-close.x-close.width<30,JSON.stringify({bounds,heading,close}));
      await page.screenshot({path:path.join(output,'library-'+width+'-'+height+'.png')});
    }
    page.on('dialog',dialog=>dialog.accept());
    await page.getByRole('button',{name:'Delete prompt',exact:true}).click();
    assert.equal(await page.evaluate(()=>rows.length),0);
    await page.getByRole('button',{name:'Close library',exact:true}).click();
    console.log('Prompt library screenshots: '+output);
  }finally{await browser.close();}
});
