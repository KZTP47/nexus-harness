"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {_electron: electron} = require("playwright-core");

async function main() {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-web-chat-smoke-"));
  const configHome = path.join(profile, "config");
  const smokeProject = path.join(profile, "project");
  fs.mkdirSync(configHome, {recursive: true});
  fs.mkdirSync(smokeProject, {recursive: true});
  const selected = {
    id: "chatgpt-smoke-selected", provider: "chatgpt",
    title: "Smoke selected web chat", url: "https://chatgpt.com/c/smoke",
  };
  let page = null;
  let putBack = null;
  fs.writeFileSync(path.join(profile, "settings.json"), JSON.stringify({
    lastProject: smokeProject, lastProjectAt: new Date().toISOString(),
    webChats: [selected],
  }));
  const packagedExecutable = process.argv[2] ? path.resolve(process.argv[2]) : "";
  const executablePath = packagedExecutable || path.join(__dirname, "node_modules", "electron", "dist",
    process.platform === "win32" ? "electron.exe" : "electron");
  const args = packagedExecutable ? [`--user-data-dir=${profile}`] : [__dirname,
    `--user-data-dir=${profile}`];
  const environment = {...process.env, APPDATA: configHome, XDG_CONFIG_HOME: configHome};
  const app = await electron.launch({executablePath, args, env: environment, timeout: 120000});
  try {
    page = await app.firstWindow({timeout: 120000});
    try {
      await page.waitForFunction(() => location.protocol === "http:", null, {timeout: 90000});
    } catch (error) {
      const shown = await page.textContent("body").catch(() => "");
      throw new Error(`The panel did not open: ${String(shown || "").replace(/\s+/g, " ").trim().slice(0, 800)}`, {cause: error});
    }
    await page.click('[data-view="swarm"]');
    await page.waitForSelector("#swarmWebChats", {state: "visible"});
    await page.click("#swarmFullScreen");
    await page.waitForFunction(() => document.getElementById("swarmStage").classList.contains("is-fullscreen"));
    await page.click("#swarmWebChats");
    await page.waitForSelector("#webChatDialog[open]", {state: "visible"});
    const labels = await page.locator("#webChatProviders .web-chat-provider strong").allTextContents();
    for (const expected of ["ChatGPT", "Claude", "Gemini", "Microsoft Copilot"]) {
      if (!labels.includes(expected)) throw new Error(`missing provider: ${expected}`);
    }
    const shellPromise = app.waitForEvent("window", {timeout: 30000});
    await page.locator("#webChatProviders .web-chat-provider")
      .filter({hasText: "ChatGPT"}).getByRole("button").click();
    const shell = await shellPromise;
    await shell.waitForSelector("#title", {state: "visible"});
    if (!await shell.locator("#title").textContent().then((one) => one.includes("ChatGPT"))) {
      throw new Error("the headered provider window did not identify its provider");
    }
    await shell.click("#close");

    await page.locator("#webChatProviders .web-chat-provider")
      .filter({hasText: "Claude"}).getByRole("button").click();
    let claudeShell = null;
    for (let attempt = 0; attempt < 120 && !claudeShell; attempt += 1) {
      claudeShell = app.windows().find((one) => (
        one !== page && !one.isClosed() && one.url().includes("provider=Claude")));
      if (!claudeShell) await page.waitForTimeout(250);
    }
    if (!claudeShell) throw new Error("the Claude authorization window did not open");
    await claudeShell.waitForSelector("#external:not([hidden])", {timeout: 30000});
    if (!await claudeShell.locator("#external").textContent().then(
      (one) => one.includes("Chrome or Edge window"))) {
      throw new Error("the Claude authorization window did not explain its secure browser transport");
    }
    await app.evaluate(({BrowserWindow}) => {
      const opened = BrowserWindow.getAllWindows().find(
        (one) => one.getTitle() !== "Nexus Harness");
      if (opened && !opened.isDestroyed()) opened.close();
    });
    console.log("pass  Claude authorization selects the secure external-browser transport");

    const connected = await page.locator("#webChatConnections").textContent();
    if (!connected.includes("Smoke selected web chat")) throw new Error("the saved web chat was not listed");
    console.log("pass  Web AI chats opens inside real Electron full screen and opens a sandboxed headered provider window");
    await page.click("#webChatDialogClose");
    await page.waitForSelector("#webChatDialog", {state: "hidden"});

    // Provider pages cannot be automated without a real account.  Emit the
    // exact IPC message useCurrent sends after its separately unit-tested URL
    // and session checks, then assert the installed renderer makes the board
    // change the user can see.
    putBack = await page.evaluate(async () => (await request("/api/swarm")).board);
    const nativeWindow = await app.browserWindow(page);
    await nativeWindow.evaluate((held, chat) => {
      held.webContents.send("harness:webChatsChanged", [chat], chat);
    }, selected);
    await page.waitForFunction(
      () => [...document.querySelectorAll("#swarmCanvas .swarm-box.agent")]
        .some((box) => box.textContent.includes("Smoke selected web chat")),
      null, {timeout: 30000},
    );
    await nativeWindow.evaluate((held, chat) => {
      held.webContents.send("harness:webChatsChanged", [chat], chat);
    }, selected);
    await page.waitForTimeout(1000);
    const boxes = await page.locator("#swarmCanvas .swarm-box.agent")
      .filter({hasText: "Smoke selected web chat"}).count();
    if (boxes !== 1) throw new Error(`selecting one web chat made ${boxes} board boxes`);
    console.log("pass  using a web chat immediately adds one visible, duplicate-safe board box");

    const connectedAgent = await page.evaluate(async (route) => {
      const standing = await request("/api/swarm");
      return standing.board.agents.find((one) => one.who === route);
    }, `web:${selected.id}`);
    if (!connectedAgent?.ready) throw new Error("the selected web-chat route never became ready");
    if (connectedAgent.chat_destination?.provider_label?.includes("Missing route")) {
      throw new Error("a live web chat was still presented as a missing static provider route");
    }
    console.log("pass  the listener registers a saved web chat as a live destination");

    // The main Nexus window is the courier. Its listener must keep running
    // while somebody works in another app, not expire 15 seconds after Nexus
    // is minimised. Query the Python process from Node so restoring/focusing
    // the renderer cannot accidentally rescue the test before it observes it.
    const session = await page.evaluate(() => ({origin: location.origin, token}));
    await nativeWindow.evaluate((held) => held.minimize());
    await new Promise((resolve) => setTimeout(resolve, 17000));
    const whileMinimised = await fetch(`${session.origin}/api/swarm`, {
      headers: {"X-Harness-Token": session.token},
    }).then((response) => response.json());
    const sleepingAgent = whileMinimised.board.agents.find(
      (one) => one.who === `web:${selected.id}`);
    if (!sleepingAgent?.ready) throw new Error("the web-chat listener expired while Nexus was minimised");
    await nativeWindow.evaluate((held) => held.restore());
    console.log("pass  the web-chat listener stays alive while the Electron window is minimised");

    const agent = page.locator("#swarmCanvas .swarm-box.agent")
      .filter({hasText: "Smoke selected web chat"});
    await agent.getByRole("button", {name: "settings for Smoke selected web chat"}).click();
    await page.waitForFunction(() => document.querySelectorAll(
      "#swarmAgentWho optgroup[label='Connect a web AI chat'] option").length === 4);
    const setupOptions = await page.locator(
      "#swarmAgentWho optgroup[label='Connect a web AI chat'] option").allTextContents();
    for (const expected of ["ChatGPT", "Claude", "Gemini", "Microsoft Copilot"]) {
      if (!setupOptions.some((one) => one.includes(expected))) {
        throw new Error(`missing agent-selector setup option: ${expected}`);
      }
    }
    console.log("pass  every web provider is directly available from an agent's assistant selector");
    await agent.getByRole("button", {name: "chat with Smoke selected web chat"}).click();
    const chatCard = page.locator(".swarm-chat-card")
      .filter({hasText: "Smoke selected web chat"});
    const fullWeb = chatCard.getByRole("button", {name: "View full web AI chat"});
    await fullWeb.waitFor({state: "visible"});
    await fullWeb.click();
    await page.waitForFunction(() => document.getElementById("webChatDialog")
      .classList.contains("is-chat-viewing"));
    if (await page.locator("#webChatViewer").isHidden()) {
      throw new Error("the full in-app web AI chat viewer stayed hidden");
    }
    console.log("pass  an agent chat has a direct full web-AI chat button that opens inside Electron");
  } finally {
    if (page && putBack) {
      await page.evaluate(async (board) => request("/api/swarm/save", {
        method: "POST", body: JSON.stringify({board}),
      }), putBack).catch(() => {});
    }
    await app.close();
    // The bundled Python server can release its project working directory a
    // fraction after Electron closes on Windows. Retry that transient lock so
    // cleanup cannot hide the actual smoke result.
    try {
      fs.rmSync(profile, {recursive: true, force: true, maxRetries: 10, retryDelay: 250});
    } catch (error) {
      console.warn(`warning  temporary smoke profile is still locked: ${error.message}`);
    }
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
