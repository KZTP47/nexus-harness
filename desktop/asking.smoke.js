"use strict";

// Does a button that asks a question work in the app somebody installs?
//
// This one exists because Rename did nothing for months. It asked with
// window.prompt, which Electron does not have, so pressing it opened no box,
// printed no error, and changed nothing. Every browser check passed the whole
// time, because a browser has prompt and the app does not.
//
// A check that runs somewhere more forgiving than where people use it is worse
// than no check at all. So this one runs in the real app: it presses Rename by
// the word on it, types in the box, presses OK, and looks at whether the name
// really changed. Then it puts the name back.
//
// It needs a screen, so a build server cannot run it. Anybody about to send
// this to somebody else can, and should:
//
//     npm run smoke:asking

const path = require("node:path");
const fs = require("node:fs");
const { _electron: electron } = require("playwright");

const TIMEOUT_MS = 120000;
const OUTPUT = path.join(__dirname, "build-output");

function theBuiltApp() {
  for (const name of fs.readdirSync(OUTPUT)) {
    const folder = path.join(OUTPUT, name);
    if (!fs.statSync(folder).isDirectory() || !name.includes("unpacked")) continue;
    const found = fs.readdirSync(folder)
      .find((one) => one.endsWith(".exe") && !one.startsWith("Uninstall"));
    if (found) return path.join(folder, found);
    const app = fs.readdirSync(folder).find((one) => one.endsWith(".app"));
    if (app) return path.join(folder, app);
  }
  throw new Error(`No built app in ${OUTPUT}. Build it first: npm run build`);
}

async function main() {
  const exe = theBuiltApp();
  console.log(`Starting the built app at ${exe}\n`);
  const app = await electron.launch({ executablePath: exe, args: [], timeout: TIMEOUT_MS });
  let wasCalled = "";
  const page = await app.firstWindow({ timeout: TIMEOUT_MS });
  try {
    await page.waitForFunction(
      () => location.protocol === "http:", null, { timeout: 90000 }
    );
    await page.waitForSelector("#projectBar", { timeout: 30000 });
    await page.click("#projectBar");
    await page.waitForSelector("#projectSidebar:not([hidden])", { timeout: 15000 });
    console.log("pass  the list of projects opens");

    wasCalled = (await page.textContent("#projectBarName")).trim();

    // The row for the project this window is showing, and no other. Taking the
    // first row instead renamed whichever project happened to sort first, said
    // "Now called ..." about it, and left this one untouched - which reads
    // exactly like the bug this check is here to catch.
    await page
      .locator("#projectList .project-one.here button", { hasText: "Rename" })
      .first()
      .click();
    await page.waitForSelector("#askDialog[open]", { timeout: 15000 });
    console.log("pass  pressing Rename opens a box to type in");

    await page.fill("#askDialogInput", "Renamed by a check");
    await page.click("#askDialogOk");
    await page.waitForFunction(
      () => document.getElementById("projectBarName").textContent.trim()
        === "Renamed by a check",
      null, { timeout: 20000 }
    );
    console.log("pass  what was typed really became the name");
  } finally {
    // Whatever happened, this project is left called what it was called.
    if (wasCalled) {
      try {
        await page
          .locator("#projectList .project-one.here button", { hasText: "Rename" })
          .first()
          .click();
        await page.waitForSelector("#askDialog[open]", { timeout: 15000 });
        await page.fill("#askDialogInput", wasCalled);
        await page.click("#askDialogOk");
        await page.waitForFunction(
          (name) => document.getElementById("projectBarName").textContent.trim() === name,
          wasCalled, { timeout: 20000 }
        );
        console.log("pass  and the name was put back");
      } catch (error) {
        console.error(`The name could not be put back: ${error.message}`);
      }
    }
    await app.close().catch(() => {});
  }
  console.log("\nAsking a question works in the app somebody installs.");
}

main().catch((error) => {
  console.error(`\n${(error && error.message) || error}`);
  process.exit(1);
});
