"use strict";

// Manual Windows clipboard acceptance. Never include this in node --test:
// it temporarily owns the system clipboard and restores every original format.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const childProcess = require("node:child_process");
const {_electron: electron} = require("playwright-core");
const repo = path.resolve(__dirname, "..");

const guard = fs.readFileSync(path.join(__dirname, "clipboard-guard.ps1"), "utf8");

async function fixture(folder) {
  const appFile = path.join(folder, "app.js");
  fs.writeFileSync(appFile, `const {app,BrowserWindow}=require('electron');
    app.whenReady().then(()=>{const window=new BrowserWindow({show:false,webPreferences:{contextIsolation:true,nodeIntegration:false}});window.loadURL('about:blank');});`);
  const app = await electron.launch({executablePath:path.join(__dirname,"node_modules/electron/dist/electron.exe"),args:[appFile],timeout:15000});
  try {
    const page = await app.firstWindow({timeout:15000});
    const source = fs.readFileSync(path.join(repo,"src/our_harness/ui/app.js"),"utf8");
    const section = (start,end) => {
      const from=source.indexOf(start),to=source.indexOf(end,from);
      assert.ok(from>=0&&to>from,start); return source.slice(from,to);
    };
    await page.setContent('<textarea id="theBigChatBox"></textarea><button id="theBigChatAttach">Attach</button><div id="theBigChatAttachments"></div><div id="compact"><textarea class="swarm-chat-box"></textarea><button class="swarm-chat-attach">Attach</button><div class="chat-attachments"></div></div>');
    await page.addScriptTag({content:`
      const $=id=>document.getElementById(id),theBigOne='clipboard-agent',held={agent:theBigOne};
      const swarmChatAttachments=new Map(),swarmChatAttachmentLoads=new Map();
      const swarmChatKey=()=> 'clipboard-agent:isolated-chat',theChatCardFor=()=> $('compact');
      function setWhatCanBePressedInSwarm() {} function showError(error) {window.fixtureError=error;}
      ${section('function make(tag','function migrateGraph')}
      ${section('function readChatAttachment','function oneSwarmChatCard')}
      const box=document.querySelector('#compact textarea');
      ${source.split('\n').find(line=>line.includes('box.addEventListener("paste", event => pasteChatAttachments'))}
      ${source.split('\n').find(line=>line.includes('$("theBigChatBox").addEventListener("paste"'))}
      window.clipboardEvidence=[];
      document.addEventListener('paste',event=>window.clipboardEvidence.push({trusted:event.isTrusted,
        types:[...event.clipboardData.types],fileCount:event.clipboardData.files.length,
        itemKinds:[...event.clipboardData.items].map(item=>item.kind),prevented:event.defaultPrevented}));
    `});
    const imageClipboard=await app.evaluate(({clipboard,nativeImage})=>{
      const image=nativeImage.createFromBitmap(Buffer.from([255,64,32,255]),{width:1,height:1});
      if(image.isEmpty()) throw new Error('Clipboard fixture image could not be created.');
      clipboard.writeImage(image);
      return {empty:clipboard.readImage().isEmpty(),formats:clipboard.availableFormats()};
    });
    assert.equal(imageClipboard.empty,false,JSON.stringify(imageClipboard));
    await page.locator('#theBigChatBox').focus();
    await page.keyboard.press('Control+V');
    await page.waitForFunction(()=>window.clipboardEvidence.length===1,{},{timeout:5000});
    await page.waitForFunction(()=>swarmChatAttachmentLoads.size===0,{},{timeout:5000});
    let evidence=await page.evaluate(()=>({events:window.clipboardEvidence,attachments:[...(swarmChatAttachments.values())].flat().map(one=>({name:one.name,type:one.type,size:one.size})),error:window.fixtureError}));
    assert.equal(evidence.events[0].trusted,true);
    assert.equal(evidence.attachments.length,1,JSON.stringify(evidence));
    assert.match(evidence.attachments[0].type,/^image\//);
    assert.equal(await page.locator('#theBigChatAttachments img').count(),1);
    console.log('PASS Windows image Control+V becomes one visible attachment:',JSON.stringify(evidence.events[0]));
    const a=path.join(folder,'copied-one.txt'),b=path.join(folder,'copied-two.json');
    fs.writeFileSync(a,'portable copied file'); fs.writeFileSync(b,'{"clipboard":"fixture"}');
    const setFiles=path.join(folder,'set-files.ps1');
    fs.writeFileSync(setFiles,String.raw`param([string]$First,[string]$Second)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Windows.Forms
$files=New-Object System.Collections.Specialized.StringCollection
$null=$files.Add($First)
$null=$files.Add($Second)
[System.Windows.Forms.Clipboard]::SetFileDropList($files)
`);
    childProcess.execFileSync('powershell.exe',['-NoProfile','-NonInteractive','-STA','-File',setFiles,a,b],{windowsHide:true,timeout:10000});
    await page.locator('#compact textarea').focus();
    await page.keyboard.press('Control+V');
    await page.waitForFunction(()=>window.clipboardEvidence.length===2,{},{timeout:5000});
    await page.waitForFunction(()=>swarmChatAttachmentLoads.size===0,{},{timeout:5000});
    evidence=await page.evaluate(()=>({events:window.clipboardEvidence,attachments:[...(swarmChatAttachments.values())].flat().map(one=>({name:one.name,type:one.type,size:one.size})),error:window.fixtureError}));
    assert.equal(evidence.events[1].trusted,true);
    assert.deepEqual(evidence.attachments.slice(1).map(one=>one.name),['copied-one.txt','copied-two.json'],JSON.stringify(evidence));
    assert.equal(await page.locator('#theBigChatAttachments .chat-attachment').count(),3);
    console.log('PASS Windows Explorer file-drop Control+V becomes two visible attachments:',JSON.stringify(evidence.events[1]));
    await app.evaluate(({clipboard})=>clipboard.writeText('plain clipboard text stays text'));
    await page.locator('#theBigChatBox').focus(); await page.keyboard.press('Control+V');
    await page.waitForFunction(()=>document.getElementById('theBigChatBox').value==='plain clipboard text stays text',{},{timeout:5000});
    assert.equal(await page.locator('#theBigChatAttachments .chat-attachment').count(),3);
    console.log('PASS Windows text-only Control+V retains ordinary text editing');
  } finally { await app.close(); }
}

async function main() {
  if(process.platform!=='win32') throw new Error('This manual clipboard acceptance requires Windows.');
  if(process.argv[2]==='--guarded') return fixture(process.argv[3]);
  const folder=fs.mkdtempSync(path.join(os.tmpdir(),'nexus-real-clipboard-'));
  const ownedFolder=fs.realpathSync(folder),temporaryRoot=fs.realpathSync(os.tmpdir());
  if(path.dirname(ownedFolder)!==temporaryRoot||!path.basename(ownedFolder).startsWith('nexus-real-clipboard-')) {
    throw new Error('Clipboard fixture directory is outside its intended temporary root.');
  }
  const script=path.join(folder,'clipboard-guard.ps1'); fs.writeFileSync(script,guard);
  // The original clipboard is held only in the guard's memory, never this folder.
  try {
    const result=childProcess.spawnSync('powershell.exe',['-NoProfile','-NonInteractive','-STA','-File',script,
      '-Node',process.execPath,'-Script',__filename,'-Fixture',folder,'-Project',repo],{cwd:repo,windowsHide:true,encoding:'utf8'});
    process.stdout.write(result.stdout||''); process.stderr.write(result.stderr||'');
    if(result.error) throw result.error;
    if(result.status!==0) throw new Error('Windows clipboard acceptance did not pass; see preserved-clipboard guard result above.');
  } finally {
    if(fs.realpathSync(folder)!==ownedFolder) throw new Error('Clipboard fixture directory identity changed; cleanup refused.');
    fs.rmSync(ownedFolder,{recursive:true,force:true});
  }
}
main().catch(error=>{console.error(error.stack||error);process.exitCode=1;});
