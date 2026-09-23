"use strict";

// The corner notification page may only receive notices, say how tall it is,
// and say which notice was clicked. Nothing else crosses this bridge.

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("nexusToast", {
  onShow: (listener) => {
    if (typeof listener !== "function") return;
    ipcRenderer.on("mail-toast:show", (_event, notice) => listener(notice || {}));
  },
  resize: (height) => ipcRenderer.send("mail-toast:resize", Number(height) || 0),
  activate: (id) => ipcRenderer.send("mail-toast:activate", String(id || "")),
});
