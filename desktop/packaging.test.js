"use strict";

// The installer ships only the files named in package.json. A file the app
// loads but nobody listed is missing at run time, and the app dies on start
// with no window and no message. That happened once, so it is checked here.

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

// Exported for the matcher tests below.
const PACKAGE = JSON.parse(fs.readFileSync(path.join(__dirname, "package.json"), "utf8"));
const SHIPPED = PACKAGE.build.files;

function localRequires(file) {
  const source = fs.readFileSync(path.join(__dirname, file), "utf8");
  return [...source.matchAll(/require\(["'](\.[^"']+)["']\)/g)].map((match) => match[1]);
}

// electron-builder patterns: "*" stops at a slash, "**" crosses folders, and a
// pattern starting with "!" takes a file back out again. A file counts as
// shipped only when something lets it in and nothing takes it out, so an
// excluded file is never reported as shipped.
function matches(pattern, relative) {
  const expression = pattern
    .split(/(\*\*\/|\*\*|\*|\?)/)
    .map((part) => {
      if (part === "**/") return "(?:.*/)?";
      if (part === "**") return ".*";
      if (part === "*") return "[^/]*";
      if (part === "?") return "[^/]";
      return part.replace(/[.+^${}()|[\]]/g, (found) => "\\" + found);
    })
    .join("");
  return new RegExp(`^${expression}$`).test(relative);
}

function shipped(relative) {
  const allowed = SHIPPED.filter((pattern) => !pattern.startsWith("!"));
  const removed = SHIPPED.filter((pattern) => pattern.startsWith("!")).map((pattern) => pattern.slice(1));
  const letIn = allowed.some(
    (pattern) => matches(pattern, relative) || (pattern.endsWith("/**") && relative.startsWith(pattern.slice(0, -2)))
  );
  const takenOut = removed.some((pattern) => matches(pattern, relative));
  return letIn && !takenOut;
}

test("every local file the app loads is in the installer", () => {
  const entry = ["main.js", "preload.js", "server.js"];
  const missing = [];
  for (const file of entry) {
    for (const name of localRequires(file)) {
      const target = name.endsWith(".js") ? name.slice(2) : `${name.slice(2)}.js`;
      if (!shipped(target)) missing.push(`${file} loads ${target}, which is not shipped`);
    }
  }
  assert.deepStrictEqual(missing, [], missing.join("\n"));
});

test("every page the app opens is in the installer", () => {
  const source = fs.readFileSync(path.join(__dirname, "main.js"), "utf8");
  const pages = [...source.matchAll(/showPage\(\s*["']([^"']+)["']/g)].map((match) => match[1]);
  assert.ok(pages.length >= 3, "main.js should open several pages");
  for (const page of pages) {
    assert.ok(
      fs.existsSync(path.join(__dirname, "pages", page)),
      `main.js opens pages/${page}, which does not exist`
    );
    assert.ok(shipped(`pages/${page}`), `pages/${page} is not shipped`);
  }
});

test("every page loads only scripts and styles that exist", () => {
  const folder = path.join(__dirname, "pages");
  for (const name of fs.readdirSync(folder).filter((item) => item.endsWith(".html"))) {
    const html = fs.readFileSync(path.join(folder, name), "utf8");
    for (const match of html.matchAll(/(?:src|href)="([^"]+)"/g)) {
      const target = match[1];
      if (target.startsWith("http") || target.startsWith("data:")) continue;
      assert.ok(
        fs.existsSync(path.join(folder, target)),
        `pages/${name} asks for ${target}, which does not exist`
      );
    }
  }
});

test("every page uses only actions the bridge really offers", () => {
  const preload = fs.readFileSync(path.join(__dirname, "preload.js"), "utf8");
  const offered = new Set(
    [...preload.matchAll(/^\s{2}([A-Za-z]+):/gm)].map((match) => match[1])
  );
  assert.ok(offered.size > 0, "the preload bridge should offer something");
  const folder = path.join(__dirname, "pages");
  for (const name of fs.readdirSync(folder).filter((item) => item.endsWith(".js"))) {
    const source = fs.readFileSync(path.join(folder, name), "utf8");
    for (const match of source.matchAll(/harnessDesktop\.([A-Za-z]+)/g)) {
      assert.ok(
        offered.has(match[1]),
        `pages/${name} calls harnessDesktop.${match[1]}, which the bridge does not offer`
      );
    }
  }
});

test("the version-mismatch page cannot ship without its repair button wiring", () => {
  const html = fs.readFileSync(path.join(__dirname, "pages", "problem.html"), "utf8");
  const page = fs.readFileSync(path.join(__dirname, "pages", "problem.js"), "utf8");
  const preload = fs.readFileSync(path.join(__dirname, "preload.js"), "utf8");
  const main = fs.readFileSync(path.join(__dirname, "main.js"), "utf8");
  assert.match(html, /id=["']repair["'][^>]*>Fix and start</);
  assert.match(page, /query\.get\(["']repair["']\)/);
  assert.match(page, /harnessDesktop\.repairVersionMismatch\(\)/);
  assert.match(preload, /repairVersionMismatch.*harness:repairVersionMismatch/);
  assert.match(main, /ipcMain\.handle\(["']harness:repairVersionMismatch["']/);
});

test("untrusted machine-local settings have native exact review and trust wiring", () => {
  const html = fs.readFileSync(path.join(__dirname, "pages", "problem.html"), "utf8");
  const page = fs.readFileSync(path.join(__dirname, "pages", "problem.js"), "utf8");
  const preload = fs.readFileSync(path.join(__dirname, "preload.js"), "utf8");
  const main = fs.readFileSync(path.join(__dirname, "main.js"), "utf8");
  assert.match(html, /id=["']trustContents["']/);
  assert.match(html, /Trust this exact file and start/);
  assert.match(page, /harnessDesktop\.reviewTrust\(\)/);
  assert.match(page, /harnessDesktop\.trustProject\(\)/);
  assert.match(preload, /reviewTrust.*harness:reviewTrust/);
  assert.match(preload, /trustProject.*harness:trustProject/);
  assert.match(main, /config\.local\.json/);
  assert.match(main, /consequences/);
});

test("the private Python runtime and harness source are packaged together", () => {
  const resources = PACKAGE.build.extraResources;
  assert.ok(resources.some((item) => item.from === "runtime" && item.to === "runtime"));
  assert.ok(resources.some((item) => item.from === "../src" && item.to === "harness/src"));
  assert.ok(shipped("build-info.json"), "the exact commit/build label must ship with the app");
  assert.match(PACKAGE.scripts.prebuild, /prepare_build_info\.py/);
  assert.match(PACKAGE.scripts.prebuild, /prepare_windows_runtime\.py/);
  assert.match(PACKAGE.scripts.prebuild, /smoke_bundled_playwright\.py/,
    "a release must prove its bundled browser in AppContainer before packaging");
});

test("the packaged Electron app carries the visual automation exchange UI", () => {
  const html = fs.readFileSync(
    path.join(__dirname, "..", "src", "our_harness", "ui", "index.html"), "utf8"
  );
  const script = fs.readFileSync(
    path.join(__dirname, "..", "src", "our_harness", "ui", "app.js"), "utf8"
  );
  for (const id of ["pipelineList", "pipelineImport", "pipelineExport", "pipelineImportFile"]) {
    assert.match(html, new RegExp(`id=["']${id}["']`));
  }
  assert.match(script, /\/api\/pipelines\/import/);
  assert.match(script, /\/api\/pipelines\/export\?name=/);
  const preload = fs.readFileSync(path.join(__dirname, "preload.js"), "utf8");
  const main = fs.readFileSync(path.join(__dirname, "main.js"), "utf8");
  assert.match(preload, /saveJsonFile:/);
  assert.match(preload, /harness:saveJsonFile/);
  assert.match(main, /ipcMain\.handle\("harness:saveJsonFile"/);
  assert.match(main, /12_000_000/);
  assert.match(main, /fs\.renameSync\(beside, chosen\)/);
  assert.ok(
    PACKAGE.build.extraResources.some((item) => item.from === "../src" && item.to === "harness/src"),
    "the Electron package must carry the Python-served UI source",
  );
});

test("the private runtime lock includes exact Node, Playwright, and Chromium identities", () => {
  const locked = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "runtime-playwright.lock.json"), "utf8"));
  assert.strictEqual(locked.schema_version, 1);
  assert.match(locked.node.version, /^\d+\.\d+\.\d+$/);
  assert.match(locked.node.sha256, /^[0-9a-f]{64}$/);
  assert.strictEqual(locked.chromium.name, "chromium-headless-shell");
  assert.match(locked.chromium.sha256, /^[0-9a-f]{64}$/);
  assert.deepStrictEqual(new Set(locked.packages.map((one) => one.version)), new Set(["1.62.1"]));
  assert.deepStrictEqual(
    new Set(locked.packages.map((one) => one.name)),
    new Set(["playwright", "playwright-core", "@playwright/test"])
  );
  for (const dependency of locked.packages) assert.match(dependency.integrity, /^sha512-/);
  assert.strictEqual(PACKAGE.dependencies["playwright-core"], "1.62.1",
    "desktop Playwright code must not float independently of the bundled runtime");
});

test("test and smoke files stay out of the installer", () => {
  for (const name of ["server.test.js", "packaging.test.js", "smoke.js", "packaged.smoke.js",
                      "automations.smoke.js"]) {
    assert.ok(!shipped(name), `${name} should not be shipped`);
  }
});

test("a file taken out by an exclusion is never called shipped", () => {
  // A test file inside pages matches "pages/**" and is then taken back out by
  // "!**/*.test.js". Reading only the first of those would call it shipped,
  // which is how a file the app loads goes missing from the installer.
  assert.ok(!shipped("pages/welcome.test.js"), "an excluded page test must not count as shipped");
  assert.ok(shipped("pages/welcome.js"), "a real page script must count as shipped");
  assert.ok(shipped("pages/welcome.html"), "a real page must count as shipped");
});

test("the pattern matcher treats stars the way the builder does", () => {
  assert.ok(matches("pages/*.js", "pages/welcome.js"));
  assert.ok(!matches("pages/*.js", "pages/deep/welcome.js"), "one star must not cross a folder");
  assert.ok(matches("pages/**", "pages/deep/welcome.js"), "two stars must cross folders");
  assert.ok(matches("**/*.test.js", "server.test.js"), "a leading two stars must also match no folder");
  assert.ok(matches("**/*.test.js", "pages/deep/a.test.js"));
  assert.ok(!matches("**/*.test.js", "server.js"));
});

test("the desktop app owns one server process and exposes exact diagnostics", () => {
  const main = fs.readFileSync(path.join(__dirname, "main.js"), "utf8");
  const preload = fs.readFileSync(path.join(__dirname, "preload.js"), "utf8");
  assert.match(main, /requestSingleInstanceLock\(\)/);
  assert.match(main, /["']second-instance["']/);
  assert.match(main, /harness:diagnostics/);
  assert.match(main, /project:\s*projectPath/);
  assert.match(main, /serverUrl:\s*server\.url/);
  assert.match(preload, /diagnostics.*harness:diagnostics/);
});

test("the release is a versioned per-user NSIS installer", () => {
  assert.strictEqual(PACKAGE.build.win.target, "nsis");
  assert.match(PACKAGE.build.win.artifactName, /\$\{version\}/);
  assert.match(PACKAGE.build.win.artifactName, /UNSIGNED-DEV/,
    "an unsigned local build must not look like a signed public release");
  assert.strictEqual(PACKAGE.build.nsis.perMachine, false);
  assert.strictEqual(PACKAGE.build.nsis.createDesktopShortcut, true);
  assert.strictEqual(PACKAGE.build.nsis.createStartMenuShortcut, true);
});
