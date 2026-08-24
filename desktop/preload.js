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
  rememberProject: (projectPath) => ipcRenderer.invoke(
    "harness:rememberProject", String(projectPath || "")),
  retry: () => ipcRenderer.invoke("harness:retry"),
  repairVersionMismatch: () => ipcRenderer.invoke("harness:repairVersionMismatch"),
  showHelp: () => ipcRenderer.invoke("harness:help"),
  showProjectFile: (relativePath) => ipcRenderer.invoke(
    "harness:showProjectFile", String(relativePath || "")),
  setFullScreen: (on) => ipcRenderer.invoke("harness:setFullScreen", Boolean(on)),
  webChatProviders: () => ipcRenderer.invoke("harness:webChatProviders"),
  webChats: () => ipcRenderer.invoke("harness:webChats"),
  connectWebChat: (provider) => ipcRenderer.invoke(
    "harness:webChatConnect", String(provider || "")),
  openWebChatWindow: (id, conversationKey, preferExisting) => ipcRenderer.invoke(
    "harness:webChatOpen", String(id || ""), String(conversationKey || ""),
    Boolean(preferExisting)),
  showWebChat: (id, conversationKey, preferExisting, bounds) => ipcRenderer.invoke(
    "harness:webChatShow", String(id || ""), String(conversationKey || ""),
    Boolean(preferExisting), bounds || {}),
  resizeWebChat: (id, conversationKey, bounds) => ipcRenderer.invoke(
    "harness:webChatResize", String(id || ""), String(conversationKey || ""),
    bounds || {}),
  hideWebChat: () => ipcRenderer.invoke("harness:webChatHide"),
  removeWebChat: (id) => ipcRenderer.invoke(
    "harness:webChatRemove", String(id || "")),
  answerWebChat: (route, prompt, attachments, conversationKey, preferExisting) => ipcRenderer.invoke(
    "harness:webChatAnswer", String(route || ""), String(prompt || ""),
    Array.isArray(attachments) ? attachments : [], String(conversationKey || ""),
    Boolean(preferExisting)),
  stopWebChat: (route, conversationKey) => ipcRenderer.invoke(
    "harness:webChatStop", String(route || ""), String(conversationKey || "")),
  resetWebChat: (route, conversationKey) => ipcRenderer.invoke(
    "harness:webChatReset", String(route || ""), String(conversationKey || "")),
  onWebChatsChanged: (listener) => {
    if (typeof listener !== "function") return;
    ipcRenderer.on(
      "harness:webChatsChanged",
      (_event, chats, selected) => listener(chats, selected || null),
    );
  },
  onFullScreenChanged: (listener) => {
    if (typeof listener !== "function") return;
    ipcRenderer.on("harness:fullScreenChanged", (_event, on) => listener(Boolean(on)));
  },
});
