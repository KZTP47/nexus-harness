"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const {
  resolveSelectedRuntime,
  runtimeTreeSha256,
  withSelectedRuntime,
} = require("./runtime-selection");

function sha256(filename) {
  return crypto.createHash("sha256").update(fs.readFileSync(filename)).digest("hex");
}

function fixture() {
  const repository = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-runtime-selection-"));
  const desktop = path.join(repository, "desktop");
  fs.mkdirSync(desktop);
  const requirements = path.join(repository, "requirements-runtime.lock");
  const playwright = path.join(repository, "runtime-playwright.lock.json");
  fs.writeFileSync(requirements, "exact requirements\n");
  fs.writeFileSync(playwright, "{\"schema_version\":1}\n");
  const python = "3.11.9";
  const pythonSha256 = "b".repeat(64);
  const requirementsSha256 = sha256(requirements);
  const playwrightLockSha256 = sha256(playwright);
  const identity = crypto.createHash("sha256").update(JSON.stringify({
    playwright_lock_sha256: playwrightLockSha256,
    python,
    python_sha256: pythonSha256,
    requirements_sha256: requirementsSha256,
    schema_version: 1,
  })).digest("hex");
  const selected = path.join(desktop, ".runtime-published", identity);
  fs.mkdirSync(selected, { recursive: true });
  const manifest = path.join(selected, "NEXUS_RUNTIME.json");
  fs.writeFileSync(manifest, JSON.stringify({
    python,
    python_sha256: pythonSha256,
    requirements_sha256: requirementsSha256,
    playwright: { lock_sha256: playwrightLockSha256 },
  }));
  fs.writeFileSync(path.join(selected, "fresh-sentinel.txt"), "fresh candidate");
  const old = path.join(desktop, "runtime");
  fs.mkdirSync(old);
  fs.writeFileSync(path.join(old, "stale-sentinel.txt"), "stale runtime");
  fs.writeFileSync(path.join(desktop, ".runtime-selection.json"), JSON.stringify({
    schema_version: 1,
    runtime_path: `.runtime-published/${identity}`,
    manifest_sha256: sha256(manifest),
    tree_sha256: runtimeTreeSha256(selected),
    python,
    python_sha256: pythonSha256,
    requirements_sha256: requirementsSha256,
    playwright_lock_sha256: playwrightLockSha256,
  }));
  return { repository, desktop, selected, old };
}

test("builder resources use the fully verified selected candidate and not stale runtime", () => {
  const held = fixture();
  try {
    const config = withSelectedRuntime({
      extraResources: [{ from: "runtime", to: "runtime" }, { from: "../src", to: "harness/src" }],
    }, held.desktop);
    const source = config.extraResources.find((entry) => entry.to === "runtime").from;
    assert.equal(source, held.selected);
    assert.equal(fs.readFileSync(path.join(source, "fresh-sentinel.txt"), "utf8"), "fresh candidate");
    assert.equal(fs.existsSync(path.join(source, "stale-sentinel.txt")), false);
  } finally {
    fs.rmSync(held.repository, { recursive: true, force: true });
  }
});

test("non-manifest tree tampering is rejected immediately before builder configuration", () => {
  const held = fixture();
  try {
    fs.writeFileSync(path.join(held.selected, "fresh-sentinel.txt"), "tampered");
    assert.throws(() => resolveSelectedRuntime(held.desktop), /tree does not match/i);
  } finally {
    fs.rmSync(held.repository, { recursive: true, force: true });
  }
});

test("selector traversal is rejected", () => {
  const held = fixture();
  try {
    fs.writeFileSync(path.join(held.desktop, ".runtime-selection.json"), JSON.stringify({
      schema_version: 1,
      runtime_path: "../outside",
      manifest_sha256: "0".repeat(64),
      tree_sha256: "0".repeat(64),
    }));
    assert.throws(() => resolveSelectedRuntime(held.desktop), /outside the owned runtime roots/i);
  } finally {
    fs.rmSync(held.repository, { recursive: true, force: true });
  }
});

test("published runtime junctions are rejected", (context) => {
  const held = fixture();
  try {
    fs.rmSync(path.join(held.desktop, ".runtime-published"), { recursive: true, force: true });
    const outside = path.join(held.repository, "outside");
    fs.mkdirSync(outside);
    try {
      fs.symlinkSync(outside, path.join(held.desktop, ".runtime-published"), "junction");
    } catch (error) {
      if (["EPERM", "EACCES"].includes(error.code)) return context.skip("junction creation denied");
      throw error;
    }
    assert.throws(() => resolveSelectedRuntime(held.desktop), /manifest|reparse|escapes/i);
  } finally {
    fs.rmSync(held.repository, { recursive: true, force: true });
  }
});
