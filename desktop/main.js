"use strict";

// The desktop window. It starts a local harness server, shows its control
// panel, and stops the server when the app closes. Nothing outside this
// machine is ever loaded.

const { app, BrowserWindow, Menu, dialog, ipcMain, shell } = require("electron");
const fs = require("node:fs");
const path = require("node:path");

const { HarnessServer, isLoopbackUrl, isOwnPage } = require("./server");
const { attachGuards, onlyOnce, whyItReallyIs } = require("./guards");

const server = new HarnessServer({ onExit: (code) => reportServerStopped(code) });
let window = null;
let projectPath = "";

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

async function openProject(chosen) {
  projectPath = chosen;
  writeSettings({ ...readSettings(), lastProject: chosen });
  showPage("starting.html", { project: path.basename(chosen) });
  server.stop();
  try {
    const url = await server.start(chosen);
    if (window && !window.isDestroyed()) window.loadURL(url);
  } catch (error) {
    showPage("problem.html", {
      title: "The harness could not start",
      detail: onlyOnce(error.message),
      // What this one really means, when the app can tell. Three guesses that
      // are all wrong send somebody looking in three wrong places.
      because: whyItReallyIs(error.message),
      log: server.recentLog().split("\n").slice(-12).join("\n"),
    });
  }
}


function allowedTarget(candidate) {
  return isLoopbackUrl(candidate) || isOwnPage(candidate, pageUrl(""));
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
    },
  });
  window.once("ready-to-show", () => window.show());
  window.on("closed", () => { window = null; });

  attachGuards(window.webContents, {
    allowedTarget,
    openExternally: (url) => shell.openExternal(url),
  });
  window.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  return window;
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
ipcMain.handle("harness:retry", () => {
  if (projectPath) openProject(projectPath);
  else showPage("welcome.html");
  return projectPath;
});
ipcMain.handle("harness:help", () => {
  showPage("help.html");
  return true;
});

app.whenReady().then(async () => {
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

  const remembered = readSettings().lastProject;
  if (remembered && fs.existsSync(remembered)) {
    openProject(remembered);
  }
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
