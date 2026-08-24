"use strict";

const {contextBridge, ipcRenderer} = require("electron");

contextBridge.exposeInMainWorld("nexusWebChatWindow", {
  startNew: () => ipcRenderer.invoke("harness:webChatShellStartNew"),
  useCurrent: () => ipcRenderer.invoke("harness:webChatShellUseCurrent"),
  close: () => ipcRenderer.invoke("harness:webChatShellClose"),
});
