"use strict";

// A real end to end check of the desktop app: start it, wait for the window,
// and confirm it shows the control panel with no browser errors. Run it with
// `npm run smoke` from this folder. It needs Playwright, which the project root
// already installs for its browser checks.

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { _electron: electron } = require("playwright");

const GIVEN_PROJECT = process.argv[2] || "";
const TIMEOUT_MS = 90000;

function seedSettings(userData, project) {
  fs.mkdirSync(userData, { recursive: true });
  fs.writeFileSync(path.join(userData, "settings.json"), JSON.stringify({
    lastProject: project,
    lastProjectAt: new Date().toISOString(),
  }, null, 2));
  return userData;
}

async function main() {
  const problems = [];
  const check = (ok, label) => {
    console.log(`${ok ? "pass" : "FAIL"}  ${label}`);
    if (!ok) problems.push(label);
  };

  const smokeHome = fs.mkdtempSync(path.join(os.tmpdir(), "harness-desktop-smoke-"));
  const configHome = path.join(smokeHome, "config");
  const userData = path.join(smokeHome, "electron-user-data");
  const firstProject = GIVEN_PROJECT
    ? path.resolve(GIVEN_PROJECT)
    : path.join(smokeHome, "First opened by desktop smoke");
  const otherProject = path.join(smokeHome, "Last opened by desktop smoke");
  if (!GIVEN_PROJECT) fs.mkdirSync(firstProject, { recursive: true });
  fs.mkdirSync(otherProject, { recursive: true });
  seedSettings(userData, firstProject);
  // Both Electron and the Python server use these machine-level configuration
  // roots. Keeping them temporary means a smoke run can never replace the
  // real person's last project or their project list.
  const environment = {
    ...process.env,
    APPDATA: configHome,
    XDG_CONFIG_HOME: configHome,
  };
  // With no override this exercises the source app. Pointing it at the built
  // executable runs the same restart check against what the installer ships.
  const executablePath = process.env.HARNESS_SMOKE_EXE || require("electron");
  const launchArguments = process.env.HARNESS_SMOKE_EXE
    ? [`--user-data-dir=${userData}`]
    : [__dirname, `--user-data-dir=${userData}`];
  let app = await electron.launch({
    args: launchArguments,
    executablePath, env: environment, timeout: TIMEOUT_MS,
  });
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

    // Checks and Workflow are optional navigation now. Turn that preference on
    // in this isolated profile before exercising those two views.
    await page.click('[data-view="settings"]');
    await page.check("#moreOptionsEnabled");
    await page.click('[data-view="checks"]');
    await page.waitForSelector("#checksView:not([hidden])", { timeout: 15000 });
    check(true, "the checks view opens");

    await page.click('[data-view="workflow"]');
    await page.waitForSelector("#workflowView:not([hidden])", { timeout: 15000 });
    const nodeCount = await page.locator("#nodeLayer .graph-node").count();
    check(nodeCount > 0, "the workflow view draws its agents");

    await page.click('[data-view="start"]');
    await page.waitForSelector("#startView:not([hidden])", { timeout: 15000 });

    await page.click("#projectBar");
    await page.fill("#projectAddPath", otherProject);
    await page.click("#projectAdd");
    const row = page.locator("#projectList .project-one", { hasText: otherProject });
    await row.getByRole("button", { name: "Work on this" }).click();
    await page.waitForFunction(
      (wanted) => document.getElementById("projectBarName").textContent.trim() === wanted,
      path.basename(otherProject), { timeout: 30000 }
    );
    check(true, "Work on this changes the current project");

    check(consoleErrors.length === 0, `no browser console errors (${consoleErrors.length})`);
    check(pageErrors.length === 0, `no page script errors (${pageErrors.length})`);
    for (const text of [...consoleErrors, ...pageErrors].slice(0, 5)) console.log(`      ${text}`);
    await app.close();
    app = await electron.launch({
      args: launchArguments,
      executablePath, env: environment, timeout: TIMEOUT_MS,
    });
    const reopened = await app.firstWindow({ timeout: TIMEOUT_MS });
    await reopened.waitForFunction(() => location.protocol === "http:", null, { timeout: TIMEOUT_MS });
    await reopened.waitForFunction(
      (wanted) => document.getElementById("projectBarName").textContent.trim() === wanted,
      path.basename(otherProject), { timeout: 30000 }
    );
    check(true, "reopening the app returns to the project last worked on");
  } finally {
    await app.close().catch(() => {});
    fs.rmSync(smokeHome, { recursive: true, force: true });
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
