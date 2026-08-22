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
const { _electron: electron } = require("playwright");

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

async function main() {
  const exe = theBuiltApp();
  console.log(`Starting the built app at ${exe}\n`);
  const app = await electron.launch({ executablePath: exe, args: [], timeout: TIMEOUT_MS });
  try {
    const page = await app.firstWindow({ timeout: TIMEOUT_MS });
    try {
      await page.waitForFunction(
        () => location.protocol === "http:", null, { timeout: 90000 }
      );
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
    console.log("\nThe app somebody installs opens.");
  } finally {
    await app.close().catch(() => {});
  }
}

main().catch((error) => {
  console.error(`\n${error && error.message ? error.message : error}`);
  process.exit(1);
});
