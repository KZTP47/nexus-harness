"use strict";

// A focused real-Electron check for the visual automation library.  It uses a
// temporary project and user-data folder, so it cannot alter a person's saved
// automations or last-opened project.

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { _electron: electron } = require("playwright");

const TIMEOUT_MS = 120000;
const ORIGINAL = "Restart automation";

function seedProject(root) {
  const folder = path.join(root, ".harness", "pipelines");
  fs.mkdirSync(folder, { recursive: true });
  fs.writeFileSync(path.join(folder, "restart-automation.json"), JSON.stringify({
    name: ORIGINAL,
    nodes: [{id: "start", kind: "start", label: "Start", settings: {}}],
    edges: [],
  }, null, 2) + "\n");
}

function seedSettings(userData, project) {
  fs.mkdirSync(userData, { recursive: true });
  fs.writeFileSync(path.join(userData, "settings.json"), JSON.stringify({
    lastProject: project,
    lastProjectAt: new Date().toISOString(),
  }, null, 2));
}

async function openPanel(app) {
  const page = await app.firstWindow({timeout: TIMEOUT_MS});
  await page.waitForFunction(() => location.protocol === "http:", null, {timeout: TIMEOUT_MS});
  await page.click('[data-view="pipelines"]');
  await page.waitForFunction(
    (wanted) => document.getElementById("pipelineName").value === wanted
      && [...document.querySelectorAll("#pipelineList .pipeline-saved-one")]
        .some((one) => one.textContent === wanted && one.classList.contains("chosen")),
    ORIGINAL, {timeout: 30000},
  );
  return page;
}

async function main() {
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-automation-smoke-"));
  const project = path.join(temporary, "project");
  const userData = path.join(temporary, "electron-user-data");
  const exported = path.join(temporary, "exported.json");
  const invalid = path.join(temporary, "invalid.json");
  seedProject(project);
  seedSettings(userData, project);
  fs.writeFileSync(invalid, "{ not JSON", "utf8");
  const environment = {
    ...process.env,
    APPDATA: path.join(temporary, "config"),
    XDG_CONFIG_HOME: path.join(temporary, "config"),
  };
  const launch = () => electron.launch({
    executablePath: require("electron"),
    args: [__dirname, `--user-data-dir=${userData}`],
    env: environment,
    timeout: TIMEOUT_MS,
  });
  let app = await launch();
  try {
    const page = await openPanel(app);
    console.log("pass  a saved automation is listed, selected, and opened on startup");

    const exchange = await page.evaluate(async (wanted) => ({
      hasNativeSave: typeof window.harnessDesktop?.saveJsonFile === "function",
      answer: await request(`/api/pipelines/export?name=${encodeURIComponent(wanted)}`),
    }), ORIGINAL);
    if (!exchange.hasNativeSave) throw new Error("the Electron JSON save bridge is missing");
    fs.writeFileSync(exported, JSON.stringify(exchange.answer.document, null, 2) + "\n");
    const envelope = JSON.parse(fs.readFileSync(exported, "utf8"));
    if (envelope.schema !== "nexus-harness.visual-automation"
        || envelope.automation?.name !== ORIGINAL) {
      throw new Error("the downloaded JSON was not the selected saved automation");
    }
    console.log("pass  Electron exposes native JSON saving and exports the selected automation");

    await page.setInputFiles("#pipelineImportFile", exported);
    await page.waitForSelector("#askDialog[open]", {timeout: 15000});
    if (await page.inputValue("#askDialogInput") !== `${ORIGINAL} copy`) {
      throw new Error("a duplicate import did not suggest a clear copy name");
    }
    await page.click("#askDialogOk");
    await page.waitForFunction(
      (wanted) => [...document.querySelectorAll("#pipelineList .pipeline-saved-one")]
        .some((one) => one.textContent === wanted),
      `${ORIGINAL} copy`, {timeout: 30000},
    );
    const selectionAfterImport = await page.evaluate(() => ({
      drawing: document.getElementById("pipelineName").value,
      chosen: document.querySelector("#pipelineList .pipeline-saved-one.chosen")?.textContent || "",
    }));
    if (selectionAfterImport.drawing !== ORIGINAL || selectionAfterImport.chosen !== ORIGINAL) {
      throw new Error(
        `duplicate import unexpectedly replaced the current drawing: ${JSON.stringify(selectionAfterImport)}`
      );
    }
    console.log("pass  importing a duplicate asks for a new name and preserves both");

    const namesBeforeBadImport = await page.locator("#pipelineList .pipeline-saved-one").allTextContents();
    await page.setInputFiles("#pipelineImportFile", invalid);
    await page.waitForFunction(
      () => document.getElementById("pipelineSaid").textContent.includes("not valid JSON"),
      null, {timeout: 15000},
    );
    const namesAfterBadImport = await page.locator("#pipelineList .pipeline-saved-one").allTextContents();
    if (JSON.stringify(namesAfterBadImport) !== JSON.stringify(namesBeforeBadImport)) {
      throw new Error("invalid JSON changed the saved automation list");
    }
    console.log("pass  invalid JSON fails closed without changing the library");

    await app.close();
    app = await launch();
    const reopened = await openPanel(app);
    await reopened.waitForFunction(
      (wanted) => [...document.querySelectorAll("#pipelineList .pipeline-saved-one")]
        .some((one) => one.textContent === wanted),
      `${ORIGINAL} copy`, {timeout: 30000},
    );
    console.log("pass  imported and existing automations remain visible after restart");
  } finally {
    await app.close().catch(() => {});
    try {
      fs.rmSync(temporary, {recursive: true, force: true, maxRetries: 20, retryDelay: 100});
    } catch (error) {
      // A just-closed Windows process may keep its former cwd open briefly.
      // Cleanup trouble must not hide which automation assertion failed.
      console.warn(`warning: temporary smoke folder will be left for Windows cleanup: ${error.message}`);
    }
  }
}

main().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
