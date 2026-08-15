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

test("test and smoke files stay out of the installer", () => {
  for (const name of ["server.test.js", "packaging.test.js", "smoke.js", "packaged.smoke.js"]) {
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
