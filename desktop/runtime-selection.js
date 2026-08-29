"use strict";

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

function sha256(filename) {
  return crypto.createHash("sha256").update(fs.readFileSync(filename)).digest("hex");
}

function comparable(filename) {
  const normalized = path.normalize(filename);
  return process.platform === "win32" ? normalized.toLowerCase() : normalized;
}

function isDirectReparsePoint(filename) {
  const lexical = path.resolve(filename);
  const metadata = fs.lstatSync(lexical);
  const actual = fs.realpathSync.native(lexical);
  const expected = path.join(fs.realpathSync.native(path.dirname(lexical)), path.basename(lexical));
  return metadata.isSymbolicLink() || comparable(actual) !== comparable(expected);
}

function runtimeTreeSha256(runtime) {
  const root = path.resolve(runtime);
  const rootReal = fs.realpathSync.native(root);
  if (isDirectReparsePoint(root)) {
    throw new Error("The selected private runtime is a reparse point");
  }
  const entries = [];
  function visit(folder) {
    for (const name of fs.readdirSync(folder)) {
      const entry = path.join(folder, name);
      const relative = path.relative(root, entry).split(path.sep).join("/");
      const metadata = fs.lstatSync(entry);
      const real = fs.realpathSync.native(entry);
      const confined = real.toLowerCase() === rootReal.toLowerCase()
        || real.toLowerCase().startsWith(rootReal.toLowerCase() + path.sep.toLowerCase());
      if (metadata.isSymbolicLink() || !confined) {
        throw new Error(`The selected private runtime contains a reparse point: ${relative}`);
      }
      if (metadata.isDirectory()) {
        entries.push({ type: "D", relative, entry, size: 0 });
        visit(entry);
      } else if (metadata.isFile()) {
        entries.push({ type: "F", relative, entry, size: metadata.size });
      } else {
        throw new Error(`The selected private runtime contains an unsupported entry: ${relative}`);
      }
    }
  }
  visit(root);
  entries.sort((left, right) => Buffer.from(left.relative).compare(Buffer.from(right.relative)));
  const held = crypto.createHash("sha256");
  for (const entry of entries) {
    held.update(entry.type + "\0" + entry.relative + "\0");
    if (entry.type === "F") {
      held.update(String(entry.size) + "\0");
      held.update(fs.readFileSync(entry.entry));
    }
  }
  return held.digest("hex");
}

function resolveSelectedRuntime(desktop = __dirname) {
  const root = path.resolve(desktop);
  const selector = path.join(root, ".runtime-selection.json");
  let payload;
  try {
    payload = JSON.parse(fs.readFileSync(selector, "utf8"));
  } catch (error) {
    throw new Error(`The verified private-runtime selection is missing or unreadable: ${selector}`, {
      cause: error,
    });
  }
  if (payload?.schema_version !== 1) {
    throw new Error("The private-runtime selection schema is unsupported");
  }
  const relative = String(payload.runtime_path || "").replace(/\\/g, "/");
  if (relative !== "runtime" && !/^\.runtime-published\/[0-9a-f]{64}$/.test(relative)) {
    throw new Error("The private-runtime selection path is outside the owned runtime roots");
  }
  const selected = path.resolve(root, ...relative.split("/"));
  if (selected === root || !selected.startsWith(root + path.sep)) {
    throw new Error("The private-runtime selection escapes the desktop directory");
  }
  if (relative.startsWith(".runtime-published/")
      && isDirectReparsePoint(path.join(root, ".runtime-published"))) {
    throw new Error("The private-runtime publication root is a reparse point");
  }
  const manifest = path.join(selected, "NEXUS_RUNTIME.json");
  if (!fs.statSync(selected, { throwIfNoEntry: false })?.isDirectory()
      || !/^[0-9a-f]{64}$/.test(String(payload.manifest_sha256 || ""))
      || !fs.statSync(manifest, { throwIfNoEntry: false })?.isFile()
      || sha256(manifest) !== payload.manifest_sha256) {
    throw new Error("The selected private-runtime manifest does not match its selection");
  }
  const metadata = JSON.parse(fs.readFileSync(manifest, "utf8"));
  const repository = path.resolve(root, "..");
  const requirementsLock = path.join(repository, "requirements-runtime.lock");
  const playwrightLock = path.join(repository, "runtime-playwright.lock.json");
  const requirementsSha256 = sha256(requirementsLock);
  const playwrightLockSha256 = sha256(playwrightLock);
  if (!/^3\.11\.\d+$/.test(String(payload.python || ""))
      || !/^[0-9a-f]{64}$/.test(String(payload.python_sha256 || ""))
      || payload.requirements_sha256 !== requirementsSha256
      || payload.playwright_lock_sha256 !== playwrightLockSha256
      || metadata.python !== payload.python
      || metadata.python_sha256 !== payload.python_sha256
      || metadata.requirements_sha256 !== requirementsSha256
      || metadata.playwright?.lock_sha256 !== playwrightLockSha256) {
    throw new Error("The selected private runtime does not match the current dependency locks");
  }
  if (relative.startsWith(".runtime-published/")) {
    const inputIdentity = crypto.createHash("sha256").update(JSON.stringify({
      playwright_lock_sha256: playwrightLockSha256,
      python: payload.python,
      python_sha256: payload.python_sha256,
      requirements_sha256: requirementsSha256,
      schema_version: 1,
    })).digest("hex");
    if (path.basename(selected) !== inputIdentity) {
      throw new Error("The selected private-runtime path does not match its locked input identity");
    }
  }
  if (!/^[0-9a-f]{64}$/.test(String(payload.tree_sha256 || ""))) {
    throw new Error("The private-runtime selection tree identity is invalid");
  }
  if (runtimeTreeSha256(selected) !== payload.tree_sha256) {
    throw new Error("The selected private-runtime tree does not match its selection");
  }
  return selected;
}

function withSelectedRuntime(configured, desktop = __dirname) {
  const selectedRuntime = resolveSelectedRuntime(desktop);
  return {
    ...configured,
    extraResources: configured.extraResources.map((entry) => (
      entry && entry.to === "runtime" ? { ...entry, from: selectedRuntime } : entry
    )),
  };
}

module.exports = { resolveSelectedRuntime, runtimeTreeSha256, withSelectedRuntime };
