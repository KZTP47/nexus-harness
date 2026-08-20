"use strict";

// Does the agent board work in the app somebody installs?
//
// The board's Add an agent and Add a project both ask for one line of text.
// That is the exact thing that was broken for months everywhere else: the
// panel asked with window.prompt, which Electron does not have, so pressing
// the button opened no box, printed no error, and changed nothing - while
// every browser check passed, because a browser has prompt and the app does
// not.
//
// So this one runs in the real app. It opens the board, presses Add an agent,
// types in the box that opens, presses OK, and looks at whether an agent
// really landed on the board. Then it takes it off again and puts the board
// back the way it found it.
//
// It needs a screen, so a build server cannot run it. Anybody about to send
// this to somebody else can, and should:
//
//     npm run smoke:board

const path = require("node:path");
const fs = require("node:fs");
const { _electron: electron } = require("playwright");

const TIMEOUT_MS = 120000;
const OUTPUT = path.join(__dirname, "build-output");
const CALLED = "Added by a smoke check";

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
  const page = await app.firstWindow({ timeout: TIMEOUT_MS });
  let putBack = null;
  try {
    await page.waitForFunction(
      () => location.protocol === "http:", null, { timeout: 90000 }
    );
    await page.click('[data-view="swarm"]', { timeout: 30000 });
    await page.waitForFunction(
      () => !document.getElementById("swarmView").hidden
        && document.getElementById("swarmSaid").textContent.length > 0,
      null, { timeout: 30000 }
    );
    console.log("pass  the board opens");

    // What this machine's board holds, so it can be put back whatever happens.
    putBack = await page.evaluate(async () => (await request("/api/swarm")).board);
    const was = putBack.agents.length;

    await page.click("#swarmAddAgent", { timeout: 20000 });
    await page.waitForSelector("#askDialog[open]", { timeout: 15000 });
    console.log("pass  pressing Add an agent opens a box to type in");

    await page.fill("#askDialogInput", CALLED);
    await page.click("#askDialogOk");
    await page.waitForFunction(
      (wanted) => [...document.querySelectorAll(".swarm-box.agent .swarm-box-name")]
        .some((one) => one.textContent === wanted),
      CALLED, { timeout: 30000 }
    );
    console.log("pass  what was typed really became an agent on the board");

    // And its settings opened on the right, which is what makes the board
    // worth pressing: the agent just added is the one you can change.
    await page.waitForFunction(
      (wanted) => document.getElementById("swarmPanelTitle").textContent === wanted
        && !document.getElementById("swarmAgentPanel").hidden,
      CALLED, { timeout: 20000 }
    );
    console.log("pass  the agent just added is the one whose settings are open");

    await page.click("#swarmAgentRemove", { timeout: 20000 });
    await page.waitForFunction(
      (count) => document.querySelectorAll(".swarm-box.agent").length === count,
      was, { timeout: 30000 }
    );
    console.log("pass  Remove this agent takes it off again");
  } finally {
    if (putBack) {
      try {
        await page.evaluate(async (board) => {
          const now = (await request("/api/swarm")).board.version;
          await request("/api/swarm/save", {
            method: "POST",
            body: JSON.stringify({ board: Object.assign({}, board, { version: now }) }),
          });
        }, putBack);
        console.log("pass  and the board this machine had is back");
      } catch (error) {
        console.error(`The board could not be put back: ${error.message}`);
      }
    }
    await app.close().catch(() => {});
  }
  console.log("\nThe agent board works in the app somebody installs.");
}

main().catch((error) => {
  console.error(`\n${(error && error.message) || error}`);
  process.exit(1);
});
