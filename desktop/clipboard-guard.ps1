param([string]$Node,[string]$Script,[string]$Fixture,[string]$Project)
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
