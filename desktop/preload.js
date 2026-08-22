"use strict";

// The only bridge between the pages and the app. It exposes a few named
// actions and nothing else: no file access, no shell, no Node.

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("harnessDesktop", {
  chooseProject: () => ipcRenderer.invoke("harness:chooseProject"),
  // Only says which folder was picked. Opening it is a separate decision, made
  // by whoever asked - the list in the panel adds it without leaving the
  // project somebody is in the middle of.
  pickAFolder: () => ipcRenderer.invoke("harness:pickAFolder"),
  retry: () => ipcRenderer.invoke("harness:retry"),
  repairVersionMismatch: () => ipcRenderer.invoke("harness:repairVersionMismatch"),
  showHelp: () => ipcRenderer.invoke("harness:help"),
});
