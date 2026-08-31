"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {execFileSync} = require("node:child_process");
const {_electron: electron, chromium} = require("playwright-core");

function matchingChrome(profile) {
  const quoted = profile.replace(/'/g, "''");
  const command = `$match = Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('chrome.exe','msedge.exe') -and $_.CommandLine -like '*${quoted}*' } | Select-Object -First 1 -ExpandProperty CommandLine; if ($match) { $match }`;
  return execFileSync("powershell.exe", ["-NoProfile", "-Command", command], {encoding: "utf8"}).trim();
}

async function main() {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-claude-browser-smoke-"));
  const configHome = path.join(profile, "config");
  const project = path.join(profile, "project");
  fs.mkdirSync(configHome, {recursive: true});
  fs.mkdirSync(project, {recursive: true});
  fs.writeFileSync(path.join(profile, "settings.json"), JSON.stringify({
    lastProject: project, lastProjectAt: new Date().toISOString(), webChats: [],
  }));
  const packagedExecutable = process.argv[2] ? path.resolve(process.argv[2]) : "";
  const executablePath = packagedExecutable || path.join(
    __dirname, "node_modules", "electron", "dist", "electron.exe");
  const args = packagedExecutable
    ? [`--user-data-dir=${profile}`]
    : [__dirname, `--user-data-dir=${profile}`];
  const app = await electron.launch({
    executablePath, args,
    env: {...process.env, APPDATA: configHome, XDG_CONFIG_HOME: configHome},
    timeout: 120000,
  });
  let controlled = null;
  try {
    const panel = await app.firstWindow({timeout: 120000});
    await panel.waitForFunction(() => location.protocol === "http:", null, {timeout: 90000});
    const shellPromise = app.waitForEvent("window", {timeout: 30000});
    await panel.evaluate(() => window.harnessDesktop.connectWebChat("claude"));
    const shell = await shellPromise;
    await shell.waitForSelector("#external:not([hidden])", {timeout: 30000});

    const browserProfile = path.join(profile, "external-web-chat", "claude");
    let commandLine = "";
    for (let attempt = 0; attempt < 80 && !commandLine; attempt += 1) {
      commandLine = matchingChrome(browserProfile);
      if (!commandLine) await panel.waitForTimeout(250);
    }
    const port = Number(commandLine.match(/--remote-debugging-port=(\d+)/)?.[1]);
    if (!port) throw new Error(`the secure Claude browser did not expose its loopback endpoint: ${commandLine}`);
    controlled = await chromium.connectOverCDP(`http://127.0.0.1:${port}`);
    const context = controlled.contexts()[0];
    let claude = null;
    for (let attempt = 0; attempt < 80 && !claude; attempt += 1) {
      claude = context.pages().find((one) => one.url().includes("claude.ai"));
      if (!claude) await panel.waitForTimeout(250);
    }
    if (!claude) throw new Error("the secure browser did not open Claude");
    await claude.waitForLoadState("domcontentloaded", {timeout: 60000});
    await claude.waitForTimeout(8000);
    const state = await claude.evaluate(() => ({
      url: location.href, title: document.title, webdriver: navigator.webdriver,
      hasEmail: Boolean(document.querySelector("input[type=email]")),
      challenged: /security verification|verifying you are human/i.test(document.body?.innerText || ""),
    }));
    if (state.webdriver) throw new Error("the secure browser exposed an automation identity");
    if (state.challenged) throw new Error(`Claude still challenged the secure browser at ${state.url}`);
    if (!state.hasEmail || !/sign in/i.test(state.title)) {
      throw new Error(`Claude did not reach its sign-in UI: ${JSON.stringify(state)}`);
    }
    console.log("pass  Claude reaches its real sign-in page in a persistent Nexus-owned browser with webdriver disabled");
  } finally {
    await app.close();
    if (controlled?.isConnected()) await controlled.close().catch(() => {});
  }
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
