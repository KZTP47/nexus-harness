"use strict";

// The desktop window. It starts a local harness server, shows its control
// panel, and stops the server when the app closes. Nothing outside this
// machine is ever loaded.

const electron = require("electron");
const { app, BrowserWindow, Menu, dialog, ipcMain, shell } = electron;
const fs = require("node:fs");
const crypto = require("node:crypto");
const path = require("node:path");

const { HarnessServer, isLoopbackUrl, isOwnPage } = require("./server");
const { attachGuards, onlyOnce, isHarnessVersionMismatch, whyItReallyIs } = require("./guards");
const { WebChatManager } = require("./web-chats");
const { DesktopSettingsStore } = require("./settings-store");
const { DirectGoalOutbox } = require("./direct-goal-outbox");
const { createShutdownCoordinator } = require("./shutdown");

function readBuildInfo() {
  try {
    const value = JSON.parse(fs.readFileSync(path.join(__dirname, "build-info.json"), "utf8"));
    return value && typeof value === "object" ? value : {};
  } catch (_error) { return {}; }
}

const buildInfo = readBuildInfo();
const server = new HarnessServer({
  onExit: (code) => reportServerStopped(code),
  bundledRequired: app.isPackaged,
  environment: {
    ...process.env,
    NEXUS_BUILD_COMMIT: String(buildInfo.commit || "unknown"),
    NEXUS_BUILD_DIRTY: buildInfo.dirty ? "1" : "0",
    NEXUS_BUILD_KIND: String(buildInfo.build_kind || "source development build"),
  },
});
let window = null;
let projectPath = "";
let repairAvailable = false;
let webChatManager = null;
let reviewedTrust = null;
let desktopSettingsStore = null;
let shutdownCoordinator = null;
const pendingJsonExports = new Map();
const ownsApplicationInstance = app.requestSingleInstanceLock();

if (!ownsApplicationInstance) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (!window || window.isDestroyed()) return;
    if (window.isMinimized()) window.restore();
    window.show();
    window.focus();
    window.webContents.focus();
  });
}

function settingsFile() {
  return path.join(app.getPath("userData"), "settings.json");
}

function settingsBackupFile() {
  return path.join(app.getPath("userData"), "settings.last-good.json");
}

function settingsStore() {
  if (!desktopSettingsStore) {
    desktopSettingsStore = new DesktopSettingsStore({
      primaryFile: settingsFile(), backupFile: settingsBackupFile(),
    });
  }
  return desktopSettingsStore;
}

function readSettings() {
  return settingsStore().read();
}

function writeSettings(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Nexus desktop settings must be an object");
  }
  const target = settingsFile();
  try {
    settingsStore().write(value);
    return true;
  } catch (error) {
    if (error?.code === "NEXUS_SETTINGS_UPDATE_REQUIRED") throw error;
    throw new Error(
      `Nexus could not save its desktop settings. Check available disk space and access to ${path.dirname(target)}, then try again. ${error.message || error}`,
    );
  }
}

function directGoalOutbox() {
  if (!projectPath) throw new Error("Open a project before saving a direct goal request.");
  return new DirectGoalOutbox({
    userDataPath: app.getPath("userData"),
    // DirectGoalOutbox resolves this through the filesystem and stores only a
    // non-secret canonical fingerprint.  A renderer can neither choose nor
    // forge the machine-local project binding.
    projectPath,
  });
}

function projectListFile() {
  return path.join(app.getPath("appData"), "our-harness", "projects.json");
}

function existingProject(chosen) {
  if (typeof chosen !== "string" || !chosen.trim()) return "";
  try {
    const resolved = path.resolve(chosen);
    return fs.statSync(resolved).isDirectory() ? resolved : "";
  } catch (_error) {
    return "";
  }
}

function newestProjectFromTheHarnessList() {
  try {
    const value = JSON.parse(fs.readFileSync(projectListFile(), "utf8"));
    const projects = Array.isArray(value && value.projects) ? value.projects : [];
    const newest = projects
      .filter((one) => one && typeof one.path === "string" && one.last_opened)
      .sort((one, other) => String(other.last_opened).localeCompare(String(one.last_opened)))
      .map((one) => ({ path: existingProject(one.path), openedAt: String(one.last_opened) }))
      .find((one) => one.path);
    return newest || null;
  } catch (_error) {
    return null;
  }
}

function projectToOpenAtStartup() {
  const flag = process.argv.indexOf("--project");
  if (flag >= 0 && flag + 1 < process.argv.length) {
    const selected = existingProject(process.argv[flag + 1]);
    if (selected) return selected;
  }
  const settings = readSettings();
  const remembered = existingProject(settings.lastProject);
  const newest = newestProjectFromTheHarnessList();
  // Older versions did not hear about Work on this. The Python side did keep
  // that exact action in its project history. Compare it with the old settings
  // file's write time so a later native folder pick still wins. New settings
  // carry their own timestamp and stay the source of truth for both paths.
  if (!settings.lastProjectAt) {
    let settingsChangedAt = Number.NaN;
    try { settingsChangedAt = fs.statSync(settingsFile()).mtimeMs; } catch (_error) { /* none */ }
    const newestAt = Date.parse(String(newest?.openedAt || ""));
    if (newest && Number.isFinite(newestAt)
        && (!Number.isFinite(settingsChangedAt) || newestAt > settingsChangedAt)) {
      return newest.path;
    }
    return remembered || newest?.path || "";
  }
  const rememberedAt = Date.parse(String(settings.lastProjectAt));
  const newestAt = Date.parse(String(newest?.openedAt || ""));
  if (newest && Number.isFinite(newestAt)
      && (!Number.isFinite(rememberedAt) || newestAt > rememberedAt)) {
    return newest.path;
  }
  return remembered || newest?.path || "";
}

function rememberCurrentProject(chosen) {
  const resolved = existingProject(chosen);
  if (!resolved) return "";
  projectPath = resolved;
  // A prior app may still open the selected project read-only so its recovery
  // banner is reachable, but it must not rewrite a newer desktop-settings
  // envelope merely to remember the current folder.
  if (settingsStore().status().write_blocked) return resolved;
  writeSettings({
    ...readSettings(),
    lastProject: resolved,
    lastProjectAt: new Date().toISOString(),
  });
  return resolved;
}

function pageUrl(name, parameters = {}) {
  const target = new URL(`file://${path.join(__dirname, "pages", name).split(path.sep).join("/")}`);
  for (const [key, value] of Object.entries(parameters)) target.searchParams.set(key, value);
  return target.toString();
}

function showPage(name, parameters) {
  if (window && !window.isDestroyed()) window.loadURL(pageUrl(name, parameters));
}

function reportServerStopped(code) {
  showPage("problem.html", {
    title: "The harness stopped",
    detail: `The local harness server closed on its own (code ${code}).`,
    log: server.recentLog().split("\n").slice(-12).join("\n"),
  });
}

// A folder only counts as a project when we can read it. The harness itself
// decides what to do with the contents.
function chooseProject(startAt) {
  const answer = dialog.showOpenDialogSync(window || undefined, {
    title: "Choose the folder you want to work on",
    defaultPath: startAt || app.getPath("home"),
    properties: ["openDirectory", "createDirectory"],
    buttonLabel: "Open this folder",
  });
  return answer && answer.length ? answer[0] : "";
}

function projectCarriesHarness(chosen) {
  try {
    return fs.existsSync(path.join(chosen, "src", "our_harness", "__init__.py"));
  } catch (error) {
    return false;
  }
}

function runtimeDiagnostics() {
  let port = "not running";
  try { port = String(new URL(server.url).port || "not running"); } catch (_error) { /* no URL */ }
  return {
    version: app.getVersion(),
    commit: `${buildInfo.commit || "unknown"}${buildInfo.dirty ? "+dirty" : ""}`,
    buildKind: String(buildInfo.build_kind || (app.isPackaged ? "packaged build with missing identity" : "source development build")),
    packaged: app.isPackaged,
    installation: app.isPackaged ? "installed desktop release" : "source development build",
    project: projectPath || "No project is open",
    serverUrl: server.url || "The local server is not running",
    port,
    processId: process.pid,
    executable: process.execPath,
    electron: process.versions.electron,
  };
}

function projectKey(chosen) {
  const resolved = path.resolve(chosen);
  return process.platform === "win32" ? resolved.toLowerCase() : resolved;
}

function remembersProjectHarness(chosen) {
  const remembered = readSettings().projectHarnessRepairs;
  if (!Array.isArray(remembered)) return false;
  return remembered.includes(projectKey(chosen));
}

function rememberProjectHarness(chosen) {
  const settings = readSettings();
  const remembered = Array.isArray(settings.projectHarnessRepairs)
    ? settings.projectHarnessRepairs.filter((one) => typeof one === "string")
    : [];
  const key = projectKey(chosen);
  if (!remembered.includes(key)) remembered.push(key);
  // This is a convenience history, not an audit log. Keeping it bounded also
  // prevents a damaged settings file from growing forever through this path.
  writeSettings({ ...settings, projectHarnessRepairs: remembered.slice(-20) });
}

async function openProject(chosen, options = {}) {
  const remembered = rememberCurrentProject(chosen);
  if (!remembered) {
    showPage("problem.html", {
      title: "That project could not be opened",
      detail: "The project folder is missing or is not a folder any more.",
    });
    return;
  }
  chosen = remembered;
  reviewedTrust = null;
  repairAvailable = false;
  showPage("starting.html", { project: path.basename(chosen) });
  try {
    const stopped = await server.stop();
    if (!stopped) {
      throw new Error(
        "The previous local Nexus server did not close in time. Wait a moment, then open the project again."
      );
    }
    const url = await server.start(chosen, options);
    if (window && !window.isDestroyed()) window.loadURL(url);
  } catch (error) {
    const canRepair = !app.isPackaged && isHarnessVersionMismatch(error.message) && projectCarriesHarness(chosen);
    // Once somebody has chosen this repair for this project, future launches
    // recover without stopping at the error page. The bundled copy still gets
    // the first attempt, so an updated installer naturally takes over again.
    if (canRepair && !options.preferProjectHarness && remembersProjectHarness(chosen)) {
      return openProject(chosen, { preferProjectHarness: true });
    }
    repairAvailable = canRepair && !options.preferProjectHarness;
    showPage("problem.html", {
      title: "The harness could not start",
      detail: onlyOnce(error.message),
      // What this one really means, when the app can tell. Three guesses that
      // are all wrong send somebody looking in three wrong places.
      because: whyItReallyIs(error.message, { canRepair: repairAvailable, installed: app.isPackaged }),
      repair: repairAvailable ? "1" : "",
      trust: /has not been told to trust|requires trusted|not trusted yet/i.test(error.message) ? "1" : "",
      log: server.recentLog().split("\n").slice(-12).join("\n"),
    });
  }
}


function allowedTarget(candidate) {
  return isLoopbackUrl(candidate) || isOwnPage(candidate, pageUrl(""));
}

function projectFileToShow(root, asked) {
  if (!root || typeof asked !== "string" || !asked.trim() || path.isAbsolute(asked)) return "";
  const project = path.resolve(root);
  const target = path.resolve(project, asked);
  const within = path.relative(project, target);
  if (!within || within.startsWith(`..${path.sep}`) || path.isAbsolute(within)) return "";
  try {
    return fs.statSync(target).isFile() ? target : "";
  } catch (_error) {
    return "";
  }
}

function createWindow() {
  window = new BrowserWindow({
    width: 1280,
    height: 840,
    minWidth: 900,
    minHeight: 620,
    backgroundColor: "#071922",
    title: "Nexus Harness",
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webviewTag: false,
      spellcheck: false,
      // This window is the authenticated web-chat courier as well as the UI.
      // Chromium otherwise throttles its heartbeat and request listener while
      // Nexus is minimised or covered by another app.
      backgroundThrottling: false,
    },
  });
  window.once("ready-to-show", () => window.show());
  const harnessRendererId = window.webContents.id;
  const abandonRendererExports = () => {
    for (const [identity, held] of pendingJsonExports.entries()) {
      if (held.sender === harnessRendererId) {
        try { closeLargeJsonExport(identity); } catch (_error) { /* target stayed untouched */ }
      }
    }
  };
  window.webContents.on("render-process-gone", abandonRendererExports);
  window.webContents.on("did-start-navigation", (_event, _url, _inPlace, mainFrame) => {
    if (mainFrame) abandonRendererExports();
  });
  const createdWindow = window;
  window.on("closed", () => {
    abandonRendererExports();
    if (window === createdWindow) window = null;
  });
  window.on("enter-full-screen", () => {
    if (window && !window.isDestroyed()) {
      window.webContents.send("harness:fullScreenChanged", true);
    }
  });
  window.on("leave-full-screen", () => {
    if (window && !window.isDestroyed()) {
      window.webContents.send("harness:fullScreenChanged", false);
    }
  });

  attachGuards(window.webContents, {
    allowedTarget,
    openExternally: (url) => shell.openExternal(url),
  });
  window.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  const manager = new WebChatManager({
    electron, owner: window, readSettings, writeSettings,
    shellPage: pageUrl("web-chat.html"),
    shellPreload: path.join(__dirname, "web-chat-shell-preload.js"),
  });
  webChatManager = manager;
  const coordinator = createShutdownCoordinator({
    closeWebChats: async () => {
      // Stop accepting renderer work before native child views and controlled
      // browser processes begin their ordered teardown.
      if (webChatManager === manager) webChatManager = null;
      await manager.close();
    },
    stopServer: () => server.stop(),
    quit: () => app.quit(),
    closeWindow: () => {
      if (!createdWindow.isDestroyed()) createdWindow.close();
    },
  });
  shutdownCoordinator = coordinator;
  createdWindow.on("close", (event) => {
    if (coordinator.isReady()) return;
    event.preventDefault();
    abandonRendererExports();
    for (const identity of [...pendingJsonExports.keys()]) {
      try { closeLargeJsonExport(identity); } catch (_error) { /* target stayed untouched */ }
    }
    void coordinator.request(process.platform === "darwin" ? "close" : "quit");
  });
  return window;
}

function fromHarnessWindow(event) {
  return Boolean(window && !window.isDestroyed() && event.sender === window.webContents);
}

function buildMenu() {
  const template = [
    {
      label: "Project",
      submenu: [
        {
          label: "Open another folder",
          accelerator: "CmdOrCtrl+O",
          click: () => { const chosen = chooseProject(projectPath); if (chosen) openProject(chosen); },
        },
        {
          label: "Start again",
          accelerator: "CmdOrCtrl+R",
          click: () => { if (projectPath) openProject(projectPath); },
        },
        { type: "separator" },
        { role: "quit", label: "Quit" },
      ],
    },
    {
      label: "View",
      submenu: [
        { role: "resetZoom", label: "Normal size" },
        { role: "zoomIn", label: "Bigger text" },
        { role: "zoomOut", label: "Smaller text" },
        { type: "separator" },
        { role: "togglefullscreen", label: "Full screen" },
        { role: "toggleDevTools", label: "Developer tools" },
      ],
    },
    {
      label: "Help",
      submenu: [
        {
          label: "What is this?",
          click: () => showPage("help.html"),
        },
        {
          label: "Welcome screen",
          click: () => showPage("welcome.html"),
        },
        {
          label: "About and diagnostics",
          click: () => showPage("about.html"),
        },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

ipcMain.handle("harness:chooseProject", () => {
  const chosen = chooseProject(projectPath);
  if (chosen) openProject(chosen);
  return chosen;
});
ipcMain.handle("harness:pickAFolder", () => {
  // The folder, and nothing else. The one above opens what it picks, which is
  // right for the Project menu and wrong for a list somebody is adding to
  // while working on something else.
  return chooseProject(projectPath);
});
ipcMain.handle("harness:rememberProject", (_event, chosen) => {
  // Work on this changes the already-running Python server. Keep Electron in
  // step without starting a second server or asking for the folder again.
  return rememberCurrentProject(String(chosen || ""));
});
ipcMain.handle("harness:retry", () => {
  if (projectPath) openProject(projectPath);
  else showPage("welcome.html");
  return projectPath;
});
ipcMain.handle("harness:repairVersionMismatch", () => {
  if (!repairAvailable || !projectPath) return false;
  // Consume the offer before starting. If the newer project code also fails,
  // openProject will decide from that fresh error whether repair still applies.
  repairAvailable = false;
  rememberProjectHarness(projectPath);
  openProject(projectPath, { preferProjectHarness: true });
  return true;
});
ipcMain.handle("harness:help", () => {
  showPage("help.html");
  return true;
});
ipcMain.handle("harness:diagnostics", (event) => (
  fromHarnessWindow(event) ? runtimeDiagnostics() : null
));
ipcMain.handle("harness:saveDirectGoalOutbox", (event, record) => {
  if (!fromHarnessWindow(event)) {
    throw new Error("Only the Nexus Harness window may save a direct goal request.");
  }
  return directGoalOutbox().save(record);
});
ipcMain.handle("harness:listDirectGoalOutbox", (event) => {
  if (!fromHarnessWindow(event) || !projectPath) return [];
  return directGoalOutbox().list();
});
ipcMain.handle("harness:readDirectGoalOutbox", (event, chatId, requestId, digest) => {
  if (!fromHarnessWindow(event)) {
    throw new Error("Only the Nexus Harness window may continue a saved direct goal request.");
  }
  return directGoalOutbox().read(
    String(chatId || ""), String(requestId || ""), String(digest || ""),
  );
});
ipcMain.handle("harness:deleteDirectGoalOutbox", (event, chatId, requestId, digest) => {
  if (!fromHarnessWindow(event)) {
    throw new Error("Only the Nexus Harness window may discard a saved direct goal request.");
  }
  return directGoalOutbox().delete(
    String(chatId || ""), String(requestId || ""), String(digest || ""),
  );
});
ipcMain.handle("harness:reviewTrust", (event) => {
  if (!fromHarnessWindow(event) || !projectPath) return null;
  const local = path.join(projectPath, ".harness", "config.local.json");
  const shared = path.join(projectPath, ".harness", "config.json");
  const selected = fs.realpathSync.native(fs.existsSync(local) ? local : shared);
  if (fs.statSync(selected).size > 1024 * 1024) throw new Error("The settings file is too large to review safely.");
  const contents = fs.readFileSync(selected, "utf8");
  const value = JSON.parse(contents);
  const consequences = [];
  if (value.providers || value.provider) consequences.push("start the provider programs or contact the model endpoints named here");
  if (value.project?.test_commands) consequences.push("run the project test commands named here");
  if (value.mcp?.servers) consequences.push("start the MCP servers named here");
  if (value.plugins?.paths) consequences.push("load executable plugins named here");
  reviewedTrust = {
    path: selected,
    sha256: crypto.createHash("sha256").update(contents, "utf8").digest("hex"),
  };
  return {
    path: selected,
    contents,
    consequences: consequences.length ? consequences : ["apply the non-executable project settings shown here"],
  };
});
ipcMain.handle("harness:trustProject", async (event) => {
  if (!fromHarnessWindow(event) || !projectPath) throw new Error("No project is open.");
  if (!reviewedTrust) throw new Error("Review the exact settings file before trusting it.");
  const current = fs.readFileSync(reviewedTrust.path, "utf8");
  const digest = crypto.createHash("sha256").update(current, "utf8").digest("hex");
  if (digest !== reviewedTrust.sha256) {
    reviewedTrust = null;
    throw new Error("The settings file changed after review. Press Try again and review the new exact contents.");
  }
  const reviewed = reviewedTrust;
  const result = await server.trustProject(projectPath, {
    reviewedConfig: fs.realpathSync.native(reviewed.path),
    expectedSha256: reviewed.sha256,
  });
  reviewedTrust = null;
  openProject(projectPath);
  return result;
});
ipcMain.handle("harness:showProjectFile", (_event, relativePath) => {
  const target = projectFileToShow(projectPath, relativePath);
  if (!target) return false;
  shell.showItemInFolder(target);
  return true;
});
ipcMain.handle("harness:saveJsonFile", (event, suggestedName, contents) => {
  if (!fromHarnessWindow(event)) throw new Error("Only the Nexus Harness window may save an export.");
  const written = String(contents || "");
  if (!written || Buffer.byteLength(written, "utf8") > 12_000_000) {
    throw new Error("A JSON export must contain 1 to 12000000 UTF-8 bytes.");
  }
  let safe = path.basename(String(suggestedName || "visual-automation.json"))
    .replace(/[^A-Za-z0-9._ -]/g, "-");
  if (!safe.toLowerCase().endsWith(".json")) safe += ".json";
  const chosen = dialog.showSaveDialogSync(window || undefined, {
    title: "Export Nexus JSON",
    defaultPath: path.join(app.getPath("downloads"), safe),
    buttonLabel: "Export JSON",
    filters: [{name: "JSON files", extensions: ["json"]}],
    properties: ["showOverwriteConfirmation", "createDirectory"],
  });
  if (!chosen) return {saved: false};
  const beside = `${chosen}.${process.pid}-${Date.now()}.part`;
  try {
    fs.writeFileSync(beside, written, {encoding: "utf8"});
    fs.renameSync(beside, chosen);
  } finally {
    try { fs.unlinkSync(beside); } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
  }
  return {saved: true, filename: path.basename(chosen)};
});

function closeLargeJsonExport(identity, removeTemporary = true) {
  const held = pendingJsonExports.get(identity);
  if (!held) return;
  pendingJsonExports.delete(identity);
  try { fs.closeSync(held.file); } catch (_error) { /* already closed */ }
  if (removeTemporary) {
    try { fs.unlinkSync(held.beside); } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
  }
}

ipcMain.handle("harness:beginLargeJsonFile", (event, suggestedName) => {
  if (!fromHarnessWindow(event)) throw new Error("Only the Nexus Harness window may save an export.");
  let safe = path.basename(String(suggestedName || "nexus-saved-board.json"))
    .replace(/[^A-Za-z0-9._ -]/g, "-");
  if (!safe.toLowerCase().endsWith(".json")) safe += ".json";
  const chosen = dialog.showSaveDialogSync(window || undefined, {
    title: "Export Nexus board JSON",
    defaultPath: path.join(app.getPath("downloads"), safe),
    buttonLabel: "Export JSON",
    filters: [{name: "JSON files", extensions: ["json"]}],
    properties: ["showOverwriteConfirmation", "createDirectory"],
  });
  if (!chosen) return {saved: false};
  const identity = crypto.randomUUID();
  const beside = `${chosen}.${process.pid}-${identity}.part`;
  const file = fs.openSync(beside, "wx");
  pendingJsonExports.set(identity, {
    sender: event.sender.id, chosen, beside, file, bytes: 0, sequence: 0,
  });
  return {saved: true, identity};
});

ipcMain.handle("harness:appendLargeJsonFile", (event, identity, sequence, chunk) => {
  const held = pendingJsonExports.get(String(identity || ""));
  if (!fromHarnessWindow(event) || !held || held.sender !== event.sender.id) {
    throw new Error("That board export is no longer active.");
  }
  try {
    if (sequence !== held.sequence) throw new Error("Board export chunks arrived out of order.");
    const bytes = Buffer.from(String(chunk || ""), "utf8");
    if (!bytes.length || bytes.length > 8_000_000) {
      throw new Error("A board export chunk must contain 1 to 8000000 UTF-8 bytes.");
    }
    if (held.bytes + bytes.length > 768_000_000) {
      throw new Error("A board JSON export may be at most 768000000 UTF-8 bytes.");
    }
    let written = 0;
    while (written < bytes.length) {
      const count = fs.writeSync(
        held.file, bytes, written, bytes.length - written,
      );
      if (!Number.isInteger(count) || count <= 0) {
        throw new Error("The board export stopped before the complete chunk was written.");
      }
      written += count;
    }
    held.bytes += bytes.length;
    held.sequence += 1;
    return {sequence: held.sequence, bytes: held.bytes};
  } catch (error) {
    closeLargeJsonExport(String(identity || ""));
    throw error;
  }
});

ipcMain.handle("harness:finishLargeJsonFile", (event, identity, sequence) => {
  const key = String(identity || "");
  const held = pendingJsonExports.get(key);
  if (!fromHarnessWindow(event) || !held || held.sender !== event.sender.id) {
    throw new Error("That board export is no longer active.");
  }
  try {
    if (sequence !== held.sequence || !held.bytes) {
      throw new Error("The board export is incomplete; the destination was not replaced.");
    }
    fs.fsyncSync(held.file);
    fs.closeSync(held.file);
    fs.renameSync(held.beside, held.chosen);
    pendingJsonExports.delete(key);
    return {saved: true, filename: path.basename(held.chosen), bytes: held.bytes};
  } catch (error) {
    closeLargeJsonExport(key);
    throw error;
  }
});

ipcMain.handle("harness:abortLargeJsonFile", (event, identity) => {
  const key = String(identity || "");
  const held = pendingJsonExports.get(key);
  if (!fromHarnessWindow(event) || !held || held.sender !== event.sender.id) return false;
  closeLargeJsonExport(key);
  return true;
});
ipcMain.handle("harness:setFullScreen", (event, on) => {
  if (!fromHarnessWindow(event) || !window || window.isDestroyed()) return false;
  const target = window;
  const wanted = Boolean(on);
  if (target.isFullScreen() === wanted) return wanted;
  // BrowserWindow#setFullScreen only starts the native transition. Resolve the
  // IPC request after Electron confirms it so renderer-side focus restoration
  // cannot race the OS taking focus during that transition.
  return new Promise((resolve, reject) => {
    const changedEvent = wanted ? "enter-full-screen" : "leave-full-screen";
    let timer = null;
    const cleanup = () => {
      if (timer) clearTimeout(timer);
      target.removeListener(changedEvent, changed);
      target.removeListener("closed", closed);
    };
    const finish = (value) => {
      cleanup();
      resolve(Boolean(value));
    };
    const changed = () => finish(wanted);
    const closed = () => finish(false);
    target.once(changedEvent, changed);
    target.once("closed", closed);
    timer = setTimeout(() => finish(
      !target.isDestroyed() && target.isFullScreen(),
    ), 5_000);
    try {
      target.setFullScreen(wanted);
    } catch (error) {
      cleanup();
      reject(error);
    }
  });
});
ipcMain.handle("harness:webChatProviders", (event) => (
  fromHarnessWindow(event) && webChatManager ? webChatManager.providers() : []
));
ipcMain.handle("harness:appIconDataUrl", (event) => {
  if (!fromHarnessWindow(event)) return "";
  try {
    return `data:image/x-icon;base64,${fs.readFileSync(
      path.join(__dirname, "nexus-harness.ico")).toString("base64")}`;
  } catch (_error) {
    return "";
  }
});
ipcMain.handle("harness:focusHarness", (event) => {
  if (!fromHarnessWindow(event) || !window || window.isDestroyed()) return false;
  window.focus();
  window.webContents.focus();
  return true;
});
ipcMain.handle("harness:desktopSettingsRecoveryStatus", (event) => (
  fromHarnessWindow(event) ? settingsStore().status() : {
    state: "unavailable", resolution_required: false,
    requires_web_chat_resolution: false, recovered_web_chat_count: 0,
  }
));
ipcMain.handle("harness:resolveDesktopSettingsRecovery", (event, action) => {
  if (!fromHarnessWindow(event)) throw new Error("Desktop settings recovery is not available");
  const choice = String(action || "");
  if (!new Set(["restore", "discard_web_chats"]).has(choice)) {
    throw new Error("Choose whether to restore or discard the recovered web chats");
  }
  let routesBefore = new Set();
  if (webChatManager) {
    try {
      routesBefore = new Set(webChatManager.list().map((one) => String(one.id || "")));
    } catch (_error) { /* only used for truthful restored-route count */ }
  }
  let outcome;
  try {
    outcome = settingsStore().resolve(choice);
  } catch (error) {
    if (error?.code === "NEXUS_SETTINGS_UPDATE_REQUIRED") throw error;
    throw new Error(
      `Nexus could not save its desktop settings. Check available disk space and access to ${path.dirname(settingsFile())}, then try again. ${error.message || error}`,
    );
  }
  try {
    const connections = webChatManager ? webChatManager.reloadFromSettings() : [];
    const restoredConnectionCount = choice === "restore" && outcome.changed && webChatManager
      ? connections.filter((one) => !routesBefore.has(String(one.id || ""))).length
      : 0;
    return {
      ...outcome, connections,
      restored_connection_count: restoredConnectionCount,
    };
  } catch (error) {
    // The durable choice already committed. Do not turn a subsequent in-memory
    // route refresh failure into the false renderer claim that nothing changed.
    // A retry/restart can reconcile the manager from the now-authoritative files.
    return {
      ...outcome,
      connections: webChatManager ? webChatManager.list() : [],
      reload_error: String(error?.message || error),
    };
  }
});
ipcMain.handle("harness:webChats", (event) => (
  fromHarnessWindow(event) && webChatManager ? webChatManager.list() : []
));
ipcMain.handle("harness:webChatPreferences", (event) => (
  fromHarnessWindow(event) && webChatManager
    ? webChatManager.preferences() : {backgroundMode: false}
));
ipcMain.handle("harness:webChatBackgroundMode", (event, enabled) => {
  if (!fromHarnessWindow(event) || !webChatManager) return {backgroundMode: false};
  return webChatManager.setBackgroundMode(Boolean(enabled));
});
ipcMain.handle("harness:webChatConnect", (
  event, provider, connectionId, conversationKey, preferExisting
) => {
  if (!fromHarnessWindow(event) || !webChatManager) return false;
  const exactId = String(connectionId || "").toLowerCase();
  if (exactId && !/^[a-z0-9][a-z0-9-]{5,63}$/.test(exactId)) {
    throw new Error("That web-chat connection ID is not valid");
  }
  return webChatManager.openSetup(
    String(provider || ""), exactId, String(conversationKey || ""),
    Boolean(preferExisting));
});
ipcMain.handle("harness:webChatOpen", (event, id, conversationKey, preferExisting) => {
  if (!fromHarnessWindow(event) || !webChatManager) return false;
  return webChatManager.openHeadered(
    String(id || ""), String(conversationKey || ""), Boolean(preferExisting));
});
ipcMain.handle("harness:webChatShow", (event, id, conversationKey, preferExisting, bounds) => {
  if (!fromHarnessWindow(event) || !webChatManager) return false;
  return webChatManager.showEmbedded(
    String(id || ""), String(conversationKey || ""), Boolean(preferExisting), bounds);
});
ipcMain.handle("harness:webChatResize", (event, id, conversationKey, bounds) => {
  if (!fromHarnessWindow(event) || !webChatManager) return false;
  return webChatManager.resizeEmbedded(String(id || ""), String(conversationKey || ""), bounds);
});
ipcMain.handle("harness:webChatHide", (event) => {
  if (!fromHarnessWindow(event) || !webChatManager) return false;
  return webChatManager.hideEmbedded();
});
ipcMain.handle("harness:webChatRemove", (event, id) => {
  if (!fromHarnessWindow(event) || !webChatManager) return false;
  return webChatManager.remove(String(id || ""));
});
ipcMain.handle("harness:webChatAnswer", async (
  event, route, prompt, attachments, conversationKey, preferExisting
) => {
  if (!fromHarnessWindow(event) || !webChatManager) throw new Error("Web chats are not available");
  const found = /^web:([a-z0-9][a-z0-9-]{5,63})$/.exec(String(route || ""));
  if (!found) throw new Error("That web-chat route is not valid");
  const root = path.resolve(projectPath || ".");
  const safeAttachments = (Array.isArray(attachments) ? attachments : []).slice(0, 6)
    .map((one) => ({
      name: String(one?.name || "").slice(0, 180),
      path: path.resolve(String(one?.path || "")),
    }))
    .filter((one) => {
      const relative = path.relative(root, one.path);
      const parts = relative.split(path.sep);
      return relative && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative)
        && parts[0] === ".harness" && parts[1] === "chats" && parts[2] === "attachments";
    });
  try {
    return await webChatManager.ask(
      found[1], String(prompt || ""), safeAttachments,
      String(conversationKey || ""), Boolean(preferExisting));
  } catch (error) {
    // Electron does not preserve custom Error properties across invoke().
    // Return the bounded transport receipt explicitly so Python can tell a
    // known provider failure from a genuinely ambiguous delivery.
    return {
      answer: "", model: "", error: String(error?.message || error),
      delivery_state: String(error?.deliveryState || "unknown"),
      failure_code: String(error?.failureCode || "web_chat_failure"),
      diagnostics: error?.diagnostics && typeof error.diagnostics === "object"
        ? error.diagnostics : {},
    };
  }
});
ipcMain.handle("harness:webChatStop", async (event, route, conversationKey) => {
  if (!fromHarnessWindow(event) || !webChatManager) return false;
  const found = /^web:([a-z0-9][a-z0-9-]{5,63})$/.exec(String(route || ""));
  return found ? webChatManager.stop(found[1], String(conversationKey || "")) : false;
});
ipcMain.handle("harness:webChatReset", async (event, route, conversationKey) => {
  if (!fromHarnessWindow(event) || !webChatManager) return false;
  const found = /^web:([a-z0-9][a-z0-9-]{5,63})$/.exec(String(route || ""));
  return found
    ? webChatManager.resetThread(found[1], String(conversationKey || "")) : false;
});
ipcMain.handle("harness:webChatShellStartNew", (event) => (
  webChatManager ? webChatManager.startNew(event.sender) : false
));
ipcMain.handle("harness:webChatShellUseCurrent", (event) => {
  if (!webChatManager) throw new Error("The Nexus window is closed");
  return webChatManager.useCurrent(event.sender);
});
ipcMain.handle("harness:webChatShellClose", (event) => {
  const held = webChatManager?.shellFor(event.sender);
  if (!held) return false;
  held.shell.close();
  return true;
});

if (ownsApplicationInstance) app.whenReady().then(async () => {
  buildMenu();
  createWindow();
  // Show the window before anything else. A folder picker on top of a blank
  // screen tells a first-time user nothing, and a dialog opened before the
  // window is on screen blocks with nothing to look at.
  showPage("welcome.html");
  await new Promise((resolve) => {
    if (!window || window.isVisible()) {
      resolve();
      return;
    }
    // Resolve on whichever comes first. Waiting only for ready-to-show would
    // hang for good if the user closed the window before it finished loading.
    const done = () => {
      clearTimeout(timer);
      resolve();
    };
    const timer = setTimeout(done, 10000);
    window.once("ready-to-show", done);
    window.once("closed", done);
  });
  if (!window || window.isDestroyed()) return;

  const remembered = projectToOpenAtStartup();
  if (remembered) openProject(remembered);
});

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) {
    createWindow();
    if (projectPath) openProject(projectPath);
  }
});

app.on("window-all-closed", () => {
  if (process.platform === "darwin") return;
  if (shutdownCoordinator && !shutdownCoordinator.isReady()) {
    void shutdownCoordinator.request("quit");
    return;
  }
  app.quit();
});

app.on("before-quit", (event) => {
  for (const identity of [...pendingJsonExports.keys()]) {
    try { closeLargeJsonExport(identity); } catch (_error) { /* target was never replaced */ }
  }
  if (!shutdownCoordinator || shutdownCoordinator.isReady()) return;
  event.preventDefault();
  void shutdownCoordinator.request("quit");
});
