"use strict";

// The desktop window. It starts a local harness server, shows its control
// panel, and stops the server when the app closes. Nothing outside this
// machine is ever loaded.

const { app, BrowserWindow, Menu, dialog, ipcMain, shell } = require("electron");
const fs = require("node:fs");
const path = require("node:path");

const { HarnessServer, isLoopbackUrl } = require("./server");

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
      detail: error.message,
      log: server.recentLog().split("\n").slice(-12).join("\n"),
    });
  }
}

function allowedTarget(candidate) {
  return isLoopbackUrl(candidate) || candidate.startsWith(`file://${path.join(__dirname, "pages").split(path.sep).join("/")}`);
}

function createWindow() {
  window = new BrowserWindow({
    width: 1280,
    height: 840,
    minWidth: 900,
    minHeight: 620,
    backgroundColor: "#071922",
    title: "Our Harness",
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

  // The window only ever shows this machine. Anything else opens in the
  // user's own browser, where they can see the address before they trust it.
  window.webContents.setWindowOpenHandler(({ url }) => {
    if (!isLoopbackUrl(url)) shell.openExternal(url);
    return { action: "deny" };
  });
  window.webContents.on("will-navigate", (event, url) => {
    if (!allowedTarget(url)) event.preventDefault();
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
ipcMain.handle("harness:retry", () => {
  if (projectPath) openProject(projectPath);
  return projectPath;
});

app.whenReady().then(() => {
  buildMenu();
  createWindow();
  const remembered = readSettings().lastProject;
  const startAt = remembered && fs.existsSync(remembered) ? remembered : chooseProject("");
  if (!startAt) {
    showPage("problem.html", {
      title: "No folder chosen",
      detail: "Use Project, then Open another folder, to pick the folder you want to work on.",
      log: "",
    });
    return;
  }
  openProject(startAt);
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
