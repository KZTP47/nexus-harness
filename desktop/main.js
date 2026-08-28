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

function readSettings() {
  try {
    const value = JSON.parse(fs.readFileSync(settingsFile(), "utf8"));
    return value && typeof value === "object" ? value : {};
  } catch (error) {
    return {};
  }
}

function writeSettings(value) {
  try {
    fs.mkdirSync(path.dirname(settingsFile()), { recursive: true });
    fs.writeFileSync(settingsFile(), JSON.stringify(value, null, 2));
  } catch (error) {
    /* a missing settings file only costs one extra folder pick */
  }
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
  server.stop();
  try {
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
  window.on("closed", () => {
    if (webChatManager) webChatManager.close();
    webChatManager = null;
    window = null;
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
  webChatManager = new WebChatManager({
    electron, owner: window, readSettings, writeSettings,
    shellPage: pageUrl("web-chat.html"),
    shellPreload: path.join(__dirname, "web-chat-shell-preload.js"),
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
ipcMain.handle("harness:setFullScreen", (_event, on) => {
  if (!window || window.isDestroyed()) return false;
  window.setFullScreen(Boolean(on));
  return Boolean(on);
});
ipcMain.handle("harness:webChatProviders", (event) => (
  fromHarnessWindow(event) && webChatManager ? webChatManager.providers() : []
));
ipcMain.handle("harness:focusHarness", (event) => {
  if (!fromHarnessWindow(event) || !window || window.isDestroyed()) return false;
  window.focus();
  window.webContents.focus();
  return true;
});
ipcMain.handle("harness:webChats", (event) => (
  fromHarnessWindow(event) && webChatManager ? webChatManager.list() : []
));
ipcMain.handle("harness:webChatConnect", (event, provider) => {
  if (!fromHarnessWindow(event) || !webChatManager) return false;
  return webChatManager.openSetup(String(provider || ""));
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
  return webChatManager.ask(
    found[1], String(prompt || ""), safeAttachments,
    String(conversationKey || ""), Boolean(preferExisting));
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
  server.stop();
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => server.stop());
process.on("exit", () => server.stop());
