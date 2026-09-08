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

const guard = String.raw`param([string]$Node,[string]$Script,[string]$Fixture,[string]$Project)
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;
public static class NexusClipboardGuard {
 [DllImport("user32.dll", SetLastError=true)] static extern bool OpenClipboard(IntPtr owner);
 [DllImport("user32.dll", SetLastError=true)] static extern bool CloseClipboard();
 [DllImport("user32.dll", SetLastError=true)] static extern bool EmptyClipboard();
 [DllImport("user32.dll", SetLastError=true)] static extern uint EnumClipboardFormats(uint prior);
 [DllImport("user32.dll", SetLastError=true)] static extern IntPtr GetClipboardData(uint format);
 [DllImport("user32.dll", SetLastError=true)] static extern IntPtr SetClipboardData(uint format,IntPtr data);
 [DllImport("kernel32.dll", SetLastError=true)] static extern UIntPtr GlobalSize(IntPtr memory);
 [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr GlobalLock(IntPtr memory);
 [DllImport("kernel32.dll")] static extern bool GlobalUnlock(IntPtr memory);
 [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr GlobalAlloc(uint flags,UIntPtr bytes);
 [DllImport("kernel32.dll")] static extern IntPtr GlobalFree(IntPtr memory);
 [DllImport("user32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern IntPtr CreateWindowEx(uint ex,string cls,string name,uint style,int x,int y,int w,int h,IntPtr parent,IntPtr menu,IntPtr instance,IntPtr extra);
 [DllImport("user32.dll")] static extern bool DestroyWindow(IntPtr window);
 static IntPtr owner;
 static List<KeyValuePair<uint,byte[]>> saved;
 static List<KeyValuePair<uint,IntPtr>> restore;
 static void Open() { for(int i=0;i<40;i++) { if(OpenClipboard(owner)) return; System.Threading.Thread.Sleep(25); } throw new Exception("Clipboard is in use; no replacement attempted."); }
 static List<KeyValuePair<uint,byte[]>> Read() {
  var values=new List<KeyValuePair<uint,byte[]>>(); uint format=0;
  while((format=EnumClipboardFormats(format))!=0) {
   if(format==2||format==3||format==9||format==14||format>=0x80&&format<=0x8f) throw new Exception("Cannot preserve handle-based clipboard format "+format+" exactly; clipboard test skipped before mutation.");
   IntPtr data=GetClipboardData(format); ulong size=GlobalSize(data).ToUInt64();
   if(data==IntPtr.Zero||size==0||size>67108864) throw new Exception("Cannot preserve clipboard format "+format+" as bounded raw bytes; no mutation attempted.");
   IntPtr pointer=GlobalLock(data); if(pointer==IntPtr.Zero) throw new Win32Exception();
   var bytes=new byte[(int)size]; try { Marshal.Copy(pointer,bytes,0,bytes.Length); } finally {GlobalUnlock(data);}
   values.Add(new KeyValuePair<uint,byte[]>(format,bytes));
  }
  return values;
 }
 public static void Capture() {
  owner=CreateWindowEx(0,"STATIC","Nexus Clipboard Guard",0,0,0,0,0,new IntPtr(-3),IntPtr.Zero,IntPtr.Zero,IntPtr.Zero);
  if(owner==IntPtr.Zero) throw new Win32Exception();
  Open(); try { saved=Read(); } finally { CloseClipboard(); }
  restore=new List<KeyValuePair<uint,IntPtr>>();
  // Allocate the complete restoration before the fixture may alter anything.
  foreach(var entry in saved) {
   IntPtr memory=GlobalAlloc(2,new UIntPtr((uint)entry.Value.Length)); if(memory==IntPtr.Zero) throw new Win32Exception();
   restore.Add(new KeyValuePair<uint,IntPtr>(entry.Key,memory));
   IntPtr pointer=GlobalLock(memory); if(pointer==IntPtr.Zero) throw new Win32Exception();
   try { Marshal.Copy(entry.Value,0,pointer,entry.Value.Length); } finally {GlobalUnlock(memory);}
  }
 }
 public static void Restore() {
  Open(); try {
   if(!EmptyClipboard()) throw new Win32Exception();
   for(int i=0;i<restore.Count;i++) {
    var entry=restore[i]; if(SetClipboardData(entry.Key,entry.Value)==IntPtr.Zero) throw new Win32Exception();
    restore[i]=new KeyValuePair<uint,IntPtr>(entry.Key,IntPtr.Zero);
   }
   var actual=Read(); if(actual.Count!=saved.Count) throw new Exception("Clipboard restoration format count did not match.");
   foreach(var original in saved) {
    byte[] bytes=null; foreach(var item in actual) if(item.Key==original.Key) bytes=item.Value;
    if(bytes==null||bytes.Length!=original.Value.Length) throw new Exception("Clipboard restoration byte count did not match.");
    for(int i=0;i<bytes.Length;i++) if(bytes[i]!=original.Value[i]) throw new Exception("Clipboard restoration bytes did not match.");
   }
  } finally {CloseClipboard();}
 }
 public static void Dispose() { if(restore!=null) foreach(var entry in restore) if(entry.Value!=IntPtr.Zero) GlobalFree(entry.Value); if(owner!=IntPtr.Zero) DestroyWindow(owner); }
}
'@
$captured = $false
try {
 [NexusClipboardGuard]::Capture()
 $captured = $true
 $start = New-Object System.Diagnostics.ProcessStartInfo
 $start.FileName = $Node
 $start.Arguments = ('"{0}" --guarded "{1}"' -f $Script,$Fixture)
 $start.WorkingDirectory = $Project
 $start.UseShellExecute = $false
 $start.CreateNoWindow = $true
 $start.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
 $start.RedirectStandardOutput = $true
 $start.RedirectStandardError = $true
 $process = [System.Diagnostics.Process]::Start($start)
 $stdout = $process.StandardOutput.ReadToEndAsync()
 $stderr = $process.StandardError.ReadToEndAsync()
 if(-not $process.WaitForExit(60000)) {
  # Stop only this owned fixture tree before restoring the shared clipboard.
  $kill = New-Object System.Diagnostics.ProcessStartInfo
  $kill.FileName = Join-Path $env:SystemRoot 'System32\taskkill.exe'
  $kill.Arguments = ('/PID {0} /T /F' -f $process.Id)
  $kill.UseShellExecute = $false
  $kill.CreateNoWindow = $true
  $kill.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
  $terminating = [System.Diagnostics.Process]::Start($kill)
  $null = $terminating.WaitForExit(10000)
  throw 'Clipboard fixture timed out; its process tree was terminated.'
 }
 $process.WaitForExit()
 [Console]::Out.Write($stdout.Result)
 [Console]::Error.Write($stderr.Result)
 if($process.ExitCode -ne 0) { throw ('Clipboard fixture failed with exit ' + $process.ExitCode) }
} finally {
 try { if($captured) { [NexusClipboardGuard]::Restore(); Write-Output 'PASS original clipboard formats restored byte-for-byte' } }
 finally { [NexusClipboardGuard]::Dispose() }
}
`;

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
