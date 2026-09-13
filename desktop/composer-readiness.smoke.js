"use strict";

// Run against the exact rebuilt application, with isolated projects and profiles.
// No provider requests are sent; the production renderer's transport is held
// indefinitely to prove typing has no dependency on request completion.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { _electron: electron } = require("playwright-core");
const {installFixture, exerciseComposer, exerciseCompactComposer} = require("./composer-readiness.test");

async function main() {
  const exe = process.argv[2] || path.join(__dirname, "build-output/win-unpacked/Nexus Harness.exe");
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-composer-readiness-"));
  const environment = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (["SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "COMSPEC", "SYSTEMDRIVE", "OS"].includes(key.toUpperCase())) {
      environment[key] = value;
    }
  }
  for (const key of ["APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME", "TEMP", "TMP"]) {
    environment[key] = path.join(root, key);
    fs.mkdirSync(environment[key], {recursive: true});
  }
  for (const pass of ["startup", "restart", "changed project"]) {
    const project = path.join(root, pass === "changed project" ? "Different project Ω" : "Fresh project Å");
    fs.mkdirSync(project, {recursive: true});
    const app = await electron.launch({executablePath: exe,
      args: [`--user-data-dir=${path.join(root, "Electron")}`, "--project", project],
      env: environment, timeout: 120000});
    try {
      const page = await app.firstWindow({timeout: 120000});
      await page.waitForFunction(() => location.protocol === "http:"
        && typeof directLongGoalRecoveryInventoryReady !== "undefined"
        && directLongGoalRecoveryInventoryReady, null, {timeout: 120000});
      await installFixture(page, false);
      await exerciseComposer(page);
      await exerciseCompactComposer(page);
      await page.screenshot({path: path.join(root, `${pass.replaceAll(" ", "-")}.png`)});
      assert.equal(await page.locator(".swarm-chat-box").first().isEditable(), true);
      console.log(`PASS packaged composer ${pass}`);
    } finally { await app.close(); }
  }
  console.log(`Evidence: ${root}`);
}

main().catch(error => { console.error(error); process.exitCode = 1; });
