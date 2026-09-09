"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const {chromium} = require("playwright-core");
const ui = path.resolve(__dirname, "../src/our_harness/ui");
const runtime = path.join(__dirname, "build-output/win-unpacked/resources/runtime");

test("collaboration UI persists role selection and separate real-project permission", {timeout:45000}, async () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(runtime, "NEXUS_RUNTIME.json"), "utf8"));
  const browser = await chromium.launch({executablePath: process.env.NEXUS_TEST_CHROMIUM || path.join(runtime, "playwright", manifest.playwright.chromium_executable), headless:true});
  try {
    const page = await browser.newPage({viewport:{width:390,height:850}});
    await page.setContent('<main id="panel"></main>');
    await page.addStyleTag({content:fs.readFileSync(path.join(ui,"styles.css"),"utf8")});
    const source = fs.readFileSync(path.join(ui,"app.js"),"utf8");
    await page.addScriptTag({content:source.slice(source.indexOf("function chatCollaborationPreference"),source.indexOf("function appendGoalAccessControls")) + `
      function make(tag, cls='', text=''){const el=document.createElement(tag);el.className=cls;el.textContent=text;return el;}
      window.saved=[];
      appendCollaborationControls(document.getElementById('panel'), {}, [{id:'alpha',name:'Agent A'},{id:'beta',name:'Agent B'}],true, async settings=>saved.push(settings));
    `});
    await page.getByLabel('Collaboration mode').selectOption('fixed');
    await page.getByLabel('Writer',{exact:true}).selectOption('beta');
    assert.equal(await page.getByRole('button',{name:'Save collaboration'}).isDisabled(),true);
    await page.getByLabel('Reviewer',{exact:true}).selectOption('alpha');
    await page.getByLabel('Allow direct editing of the real project').check();
    await page.getByRole('button',{name:'Save collaboration'}).click();
    await page.getByText('Collaboration saved.',{exact:true}).waitFor();
    assert.deepEqual(await page.evaluate(()=>saved[0]),{mode:'fixed',writer_id:'beta',reviewer_id:'alpha',allow_direct_real_edits:true});
    await page.getByLabel('Collaboration mode').selectOption('flexible');
    assert.equal(await page.getByLabel('Writer',{exact:true}).isVisible(),false);
    assert.ok(await page.locator('main').evaluate(el=>el.scrollWidth<=390));
  } finally {await browser.close();}
});

test("workspace viewer switches between real project and both agents and pages file content", {timeout:45000}, async () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(runtime, "NEXUS_RUNTIME.json"), "utf8"));
  const browser = await chromium.launch({executablePath: process.env.NEXUS_TEST_CHROMIUM || path.join(runtime, "playwright", manifest.playwright.chromium_executable), headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1200,height:850}});
    await page.setContent('<main><h1>Nexus Harness</h1><button id="open">View project and agent copies</button></main>');
    await page.addStyleTag({content:fs.readFileSync(path.join(ui,"styles.css"),"utf8")});
    const source = fs.readFileSync(path.join(ui,"app.js"),"utf8");
    await page.addScriptTag({content:source.slice(source.indexOf("async function openGoalWorkspaces"), source.indexOf("function appendGoalAccessControls")) + `
      function make(tag, cls='', text=''){const el=document.createElement(tag);el.className=cls;el.textContent=text;return el;}
      window.calls=[];
      async function request(url) {
        const p=new URL(url,'http://localhost').searchParams;calls.push(Object.fromEntries(p));
        const id=p.get('workspace_id'), path=p.get('path');
        if (path==='missing.txt') throw new Error('File was removed while the agent worked');
        if (path==='app.txt') return {kind:'file',root:id,path,content:id+' '+(p.get('cursor')?'second page':'first page'),truncated:!p.get('cursor'),next_cursor:'next'};
        return {kind:'directory',root:id,entries:[{name:'app.txt',path:'app.txt',directory:false},{name:'missing.txt',path:'missing.txt',directory:false}]};
      }
      document.getElementById('open').onclick=()=>openGoalWorkspaces({goal_id:'goal-only',workspaces:[{id:'real',label:'Real project'},{id:'agent-a',label:"Agent A's copy"},{id:'agent-b',label:"Agent B's copy"}]});
    `});
    await page.getByRole('button',{name:'View project and agent copies'}).click();
    for (const [label,id] of [['Real project','real'],["Agent A's copy",'agent-a'],["Agent B's copy",'agent-b']]) {
      await page.getByRole('button',{name:label,exact:true}).click();
      await page.getByRole('button',{name:'app.txt',exact:true}).click();
      await page.waitForFunction(id=>document.querySelector('pre')?.textContent===id+' first page',id);
      await page.getByRole('button',{name:'Next page'}).click();
      await page.waitForFunction(id=>document.querySelector('pre')?.textContent===id+' second page',id);
      await page.getByRole('button',{name:'Back to folder'}).click();
    }
    await page.getByRole('button',{name:'missing.txt',exact:true}).click();
    await page.getByText('File was removed while the agent worked',{exact:true}).waitFor();
    assert.ok((await page.evaluate(()=>calls)).every(one=>one.goal_id==='goal-only'));
    await page.setViewportSize({width:390,height:850});
    await page.getByRole('button',{name:'Real project',exact:true}).click();
    assert.ok(await page.locator('dialog').evaluate(el=>el.getBoundingClientRect().width<=390));
    await page.getByRole('button',{name:'Close',exact:true}).click();
    await page.locator('dialog').waitFor({state:'detached'});
    assert.equal(await page.locator('dialog').count(),0);
  } finally {await browser.close();}
});
