"use strict";

// The only bridge between the pages and the app. It exposes two actions and
// nothing else: no file access, no shell, no Node.

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("harnessDesktop", {
  chooseProject: () => ipcRenderer.invoke("harness:chooseProject"),
  retry: () => ipcRenderer.invoke("harness:retry"),
  showHelp: () => ipcRenderer.invoke("harness:help"),
});
