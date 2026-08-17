"use strict";

// Checks the built app rather than the source. Run it after `npm run build`.
//
//   npm run build
//   npm run smoke:packaged
//
// It opens the packed archive the installer ships and confirms every file the
// app loads is really inside it. A file that is missing here means the
// installed app dies on start with no window and no message, which is exactly
// the fault this exists to catch.

const fs = require("node:fs");
const path = require("node:path");

const OUTPUT = path.join(__dirname, "build-output");

function unpackedFolder() {
  const candidates = ["win-unpacked", "linux-unpacked", "mac"];
  const found = candidates
    .map((name) => path.join(OUTPUT, name))
    .find((item) => fs.existsSync(item));
  if (!found) {
    throw new Error("No built app was found. Run `npm run build` first, then this again.");
  }
  return found;
}

function archiveContents(folder) {
  // The app folder is named after productName, so read that rather than
  // repeat it here: a rename would otherwise leave this looking for a folder
  // nobody builds any more.
  const productName = require("./package.json").build.productName;
  const archive = [
    path.join(folder, "resources", "app.asar"),
    path.join(folder, `${productName}.app`, "Contents", "Resources", "app.asar"),
  ].find((item) => fs.existsSync(item));
  if (!archive) throw new Error(`No app.asar was found under ${folder}`);
  // Read the archive directly. Shelling out breaks on a path with a space in it.
  const { listPackage } = require("@electron/asar");
  return listPackage(archive)
    .map((line) => String(line).replace(/\\/g, "/").replace(/^\//, ""))
    .filter(Boolean);
}

function localRequires(file) {
  const source = fs.readFileSync(path.join(__dirname, file), "utf8");
  return [...source.matchAll(/require\(["'](\.[^"']+)["']\)/g)]
    .map((match) => match[1].slice(2))
    .map((name) => (name.endsWith(".js") ? name : `${name}.js`));
}

function main() {
  const problems = [];
  const check = (ok, label) => {
    console.log(`${ok ? "pass" : "FAIL"}  ${label}`);
    if (!ok) problems.push(label);
  };

  const folder = unpackedFolder();
  console.log(`Looking inside the build at ${folder}\n`);
  const contents = archiveContents(folder);

  for (const entry of ["main.js", "preload.js", "server.js"]) {
    check(contents.includes(entry), `${entry} is in the installer`);
    for (const needed of localRequires(entry)) {
      check(contents.includes(needed), `${entry} loads ${needed}, and it is in the installer`);
    }
  }

  const pages = contents.filter((item) => item.startsWith("pages/"));
  check(pages.length >= 6, `the pages are in the installer (${pages.length} found)`);

  for (const unwanted of ["server.test.js", "packaging.test.js", "smoke.js", "packaged.smoke.js"]) {
    check(!contents.includes(unwanted), `${unwanted} stayed out of the installer`);
  }

  const installers = fs.readdirSync(OUTPUT).filter((name) => /\.(exe|dmg|AppImage)$/.test(name));
  check(installers.length > 0, `an installer was built (${installers.join(", ") || "none"})`);

  if (problems.length) {
    console.error(`\n${problems.length} check(s) failed.`);
    process.exit(1);
  }
  console.log("\nThe built app carries everything it needs.");
}

try {
  main();
} catch (error) {
  console.error(`The check itself broke: ${error && error.message ? error.message : error}`);
  process.exit(1);
}
