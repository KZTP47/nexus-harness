"use strict";

// The only bridge between the pages and the app. It exposes a few named
// actions and nothing else: no file access, no shell, no Node.

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("harnessDesktop", {
  appIconDataUrl: () => ipcRenderer.invoke("harness:appIconDataUrl"),
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
  diagnostics: () => ipcRenderer.invoke("harness:diagnostics"),
  saveDirectGoalOutbox: (record) => ipcRenderer.invoke(
    "harness:saveDirectGoalOutbox", record),
  listDirectGoalOutbox: () => ipcRenderer.invoke("harness:listDirectGoalOutbox"),
  readDirectGoalOutbox: (chatId, requestId, payloadSha256) => ipcRenderer.invoke(
    "harness:readDirectGoalOutbox", String(chatId || ""),
    String(requestId || ""), String(payloadSha256 || "")),
  deleteDirectGoalOutbox: (chatId, requestId, payloadSha256) => ipcRenderer.invoke(
    "harness:deleteDirectGoalOutbox", String(chatId || ""),
    String(requestId || ""), String(payloadSha256 || "")),
  reviewTrust: () => ipcRenderer.invoke("harness:reviewTrust"),
  trustProject: () => ipcRenderer.invoke("harness:trustProject"),
  showProjectFile: (relativePath) => ipcRenderer.invoke(
    "harness:showProjectFile", String(relativePath || "")),
  saveJsonFile: (suggestedName, contents) => ipcRenderer.invoke(
    "harness:saveJsonFile", String(suggestedName || "nexus-export.json"),
    String(contents || "")),
  saveLargeJsonFile: async (suggestedName, contents) => {
    const written = String(contents || "");
    const begun = await ipcRenderer.invoke(
      "harness:beginLargeJsonFile",
      String(suggestedName || "nexus-saved-board.json"),
    );
    if (!begun?.saved) return {saved: false};
    let sequence = 0;
    try {
      // Keep each IPC message far below Chromium's fixed channel maximum.
      // Avoid splitting a UTF-16 surrogate pair between chunks so encoding the
      // original JSON remains byte-for-byte stable.
      for (let start = 0; start < written.length;) {
        let end = Math.min(written.length, start + 1_000_000);
        if (end < written.length) {
          const before = written.charCodeAt(end - 1);
          const after = written.charCodeAt(end);
          if (before >= 0xD800 && before <= 0xDBFF && after >= 0xDC00 && after <= 0xDFFF) {
            end -= 1;
          }
        }
        const accepted = await ipcRenderer.invoke(
          "harness:appendLargeJsonFile", begun.identity, sequence,
          written.slice(start, end),
        );
        sequence = Number(accepted?.sequence);
        start = end;
      }
      return await ipcRenderer.invoke(
        "harness:finishLargeJsonFile", begun.identity, sequence,
      );
    } catch (error) {
      await ipcRenderer.invoke("harness:abortLargeJsonFile", begun.identity).catch(() => {});
      throw error;
    }
  },
  focusHarness: () => ipcRenderer.invoke("harness:focusHarness"),
  setFullScreen: (on) => ipcRenderer.invoke("harness:setFullScreen", Boolean(on)),
  webChatProviders: () => ipcRenderer.invoke("harness:webChatProviders"),
  webChats: () => ipcRenderer.invoke("harness:webChats"),
  desktopSettingsRecoveryStatus: () => ipcRenderer.invoke(
    "harness:desktopSettingsRecoveryStatus"),
  resolveDesktopSettingsRecovery: (action) => ipcRenderer.invoke(
    "harness:resolveDesktopSettingsRecovery", String(action || "")),
  webChatPreferences: () => ipcRenderer.invoke("harness:webChatPreferences"),
  setWebChatBackgroundMode: (enabled) => ipcRenderer.invoke(
    "harness:webChatBackgroundMode", Boolean(enabled)),
  connectWebChat: (provider, connectionId, conversationKey, preferExisting) => ipcRenderer.invoke(
    "harness:webChatConnect", String(provider || ""), String(connectionId || ""),
    String(conversationKey || ""), Boolean(preferExisting)),
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
