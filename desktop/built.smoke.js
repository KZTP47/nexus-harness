"use strict";

// Does the app somebody installs actually open?
//
// The check beside this one reads what is inside the installer, which is a
// useful thing to know and is not the same question. It said "carries
// everything it needs" while the harness itself was missing, and the app that
// went out opened onto three copies of "No module named our_harness" and
// nothing anybody could act on.
//
// This one starts the built app the way a person does, and waits for the panel.
// It needs a screen, so a build server cannot run it - but anybody about to
// send this to somebody else can, and should.
//
//     npm run smoke:built

const path = require("node:path");
const fs = require("node:fs");
const os = require("node:os");
const { _electron: electron } = require("playwright-core");

const TIMEOUT_MS = 120000;
const OUTPUT = path.join(__dirname, "build-output");
const GIVEN_APP = process.argv[2] || "";

function theBuiltApp() {
  if (GIVEN_APP) {
    const given = path.resolve(GIVEN_APP);
    if (!fs.existsSync(given)) throw new Error(`The app does not exist: ${given}`);
    return given;
  }
  for (const name of fs.readdirSync(OUTPUT)) {
    const folder = path.join(OUTPUT, name);
    if (!fs.statSync(folder).isDirectory() || !name.includes("unpacked")) continue;
    const found = fs.readdirSync(folder).find((one) => one.endsWith(".exe") && !one.startsWith("Uninstall"));
    if (found) return path.join(folder, found);
    const app = fs.readdirSync(folder).find((one) => one.endsWith(".app"));
    if (app) return path.join(folder, app);
  }
  throw new Error(
    `No built app in ${OUTPUT}. Build it first: npm run build`
  );
}

async function reachThePanel(page) {
  const first = await Promise.race([
    page.waitForFunction(() => location.protocol === "http:", null, { timeout: 90000 })
      .then(() => "panel"),
    page.waitForSelector("#repair", { state: "visible", timeout: 90000 })
      .then(() => "repair"),
  ]);
  if (first === "repair") {
    await page.click("#repair");
    await page.waitForFunction(
      () => location.protocol === "http:", null, { timeout: 90000 }
    );
    console.log("pass  the packaged app repaired a newer-project mismatch");
  }
}

async function main() {
  const exe = theBuiltApp();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-clean-profile-"));
  const project = path.resolve(__dirname, "..");
  console.log(`Starting the built app at ${exe}\n`);
  const app = await electron.launch({
    executablePath: exe,
    args: [`--user-data-dir=${profile}`, "--project", project],
    timeout: TIMEOUT_MS,
  });
  try {
    const page = await app.firstWindow({ timeout: TIMEOUT_MS });
    try {
      await reachThePanel(page);
    } catch (error) {
      const shown = await page.textContent("body").catch(() => "");
      throw new Error(
        "The panel never opened. What the app is showing instead:\n"
        + String(shown || "(nothing)").replace(/\s+/g, " ").trim().slice(0, 800)
      );
    }
    const address = new URL(page.url());
    if (!["127.0.0.1", "localhost"].includes(address.hostname)) {
      throw new Error(`The window went somewhere else: ${page.url()}`);
    }
    console.log("pass  the built app opens the panel");
    await page.waitForSelector("#startView", { timeout: 30000 });
    console.log("pass  the panel is really there");
    const runtime = await page.evaluate(async () => {
      const answer = await fetch("/api/bootstrap");
      return (await answer.json()).runtime;
    });
    if (!runtime || path.resolve(runtime.project_root) !== project) {
      throw new Error(`The clean first-run project was not selected: ${JSON.stringify(runtime)}`);
    }
    if (!/[\\/]resources[\\/]runtime[\\/]python\.exe$/i.test(String(runtime.python_executable || ""))) {
      throw new Error(`The packaged app did not use its private Python: ${runtime.python_executable}`);
    }
    if (!String(runtime.python_version || "").startsWith("3.11.")) {
      throw new Error(`The packaged private Python is not supported 3.11: ${runtime.python_version}`);
    }
    if (!/^[0-9a-f]{40}(?:\+dirty)?$/i.test(String(runtime.commit || ""))) {
      throw new Error(`The packaged app did not surface its exact commit identity: ${runtime.commit}`);
    }
    console.log("pass  a fresh profile selects the requested project and uses private Python");
    console.log("\nThe app somebody installs opens.");
  } finally {
    await app.close().catch(() => {});
    fs.rmSync(profile, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(`\n${error && error.message ? error.message : error}`);
  process.exit(1);
});
