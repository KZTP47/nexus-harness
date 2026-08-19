"use strict";

// A real end to end check of the desktop app: start it, wait for the window,
// and confirm it shows the control panel with no browser errors. Run it with
// `npm run smoke` from this folder. It needs Playwright, which the project root
// already installs for its browser checks.

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { _electron: electron } = require("playwright");

const PROJECT = process.argv[2] || path.resolve(__dirname, "..");
const TIMEOUT_MS = 90000;

function seedSettings() {
  const base = process.platform === "win32"
    ? path.join(process.env.APPDATA || os.homedir(), "our-harness-desktop")
    : path.join(os.homedir(), ".config", "our-harness-desktop");
  fs.mkdirSync(base, { recursive: true });
  fs.writeFileSync(path.join(base, "settings.json"), JSON.stringify({ lastProject: PROJECT }, null, 2));
  return base;
}

async function main() {
  const problems = [];
  const check = (ok, label) => {
    console.log(`${ok ? "pass" : "FAIL"}  ${label}`);
    if (!ok) problems.push(label);
  };

  seedSettings();
  // Playwright looks for Electron beside itself, and here it lives one folder
  // down, so hand it the path the electron package reports.
  const executablePath = require("electron");
  const app = await electron.launch({ args: [__dirname], executablePath, timeout: TIMEOUT_MS });
  try {
    const page = await app.firstWindow({ timeout: TIMEOUT_MS });
    const consoleErrors = [];
    const pageErrors = [];
    page.on("console", (message) => { if (message.type() === "error") consoleErrors.push(message.text()); });
    page.on("pageerror", (error) => pageErrors.push(String(error && error.message)));

    // If the panel never opens, the reason is on the failure page the app is
    // showing right now. Without this the only thing said was "Timeout 90000ms
    // exceeded", which names nothing and points nowhere - and the reason it
    // was hiding was "No module named our_harness", which anybody could have
    // acted on.
    try {
      await page.waitForFunction(() => location.protocol === "http:", null, { timeout: TIMEOUT_MS });
    } catch (error) {
      const shown = await page.textContent("body").catch(() => "");
      const said = String(shown || "").replace(/\s+/g, " ").trim().slice(0, 800);
      throw new Error(
        `The panel never opened. What the app is showing instead:
${said || "(nothing)"}`
      );
    }
    const address = new URL(page.url());
    check(address.protocol === "http:", "the window loads over http");
    check(["127.0.0.1", "localhost"].includes(address.hostname), "the window stays on this machine");

    await page.waitForSelector("#startView", { timeout: TIMEOUT_MS });
    const heading = await page.textContent("#startTitle");
    check(heading.trim() === "Welcome", "the guided view opens first");

    await page.click('[data-view="checks"]');
    await page.waitForSelector("#checksView:not([hidden])", { timeout: 15000 });
    check(true, "the checks view opens");

    await page.click('[data-view="workflow"]');
    await page.waitForSelector("#workflowView:not([hidden])", { timeout: 15000 });
    const nodeCount = await page.locator("#nodeLayer .graph-node").count();
    check(nodeCount > 0, "the workflow view draws its agents");

    await page.click('[data-view="start"]');
    await page.waitForSelector("#startView:not([hidden])", { timeout: 15000 });

    check(consoleErrors.length === 0, `no browser console errors (${consoleErrors.length})`);
    check(pageErrors.length === 0, `no page script errors (${pageErrors.length})`);
    for (const text of [...consoleErrors, ...pageErrors].slice(0, 5)) console.log(`      ${text}`);
  } finally {
    await app.close();
  }

  if (problems.length) {
    console.error(`\n${problems.length} check(s) failed.`);
    process.exit(1);
  }
  console.log("\nEvery desktop check passed.");
}

main().catch((error) => {
  console.error(`The smoke run itself broke: ${error && error.stack ? error.stack : error}`);
  process.exit(1);
});
