"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");
// Initialize the library's entry point before its mutually importing packagers.
require("app-builder-lib");
const { WinPackager } = require("app-builder-lib/out/winPackager");
const { withPreservedKestraJava } = require("./kestra-signing.cjs");

function fixture(callback) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "nexus-signing-"));
  try {
    fs.mkdirSync(path.join(root, "kestra-runtime"));
    fs.writeFileSync(path.join(root, "kestra-runtime", "NEXUS_KESTRA.json"), JSON.stringify({
      schema_version: 1, files: { "java/bin/java.exe": "a".repeat(64), "java/bin/keytool.exe": "b".repeat(64) },
    }));
    callback(root);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
}

test("builder preserves only manifest-owned Java EXEs while Nexus and NSIS remain signable", () => fixture(root => {
  const config = withPreservedKestraJava({ win: { target: "nsis" } }, root);
  const shouldSign = file => WinPackager.prototype.shouldSignFile.call({ platformSpecificBuildOptions: config.win }, file);
  for (const java of ["C:\\arbitrary source\\kestra-runtime\\java\\bin\\java.exe", "F:/installed/resources/kestra-runtime/java/bin/keytool.exe"]) {
    assert.equal(shouldSign(java), false, java);
  }
  for (const file of ["Nexus Harness.exe", "Nexus-Harness-Setup-9.8.7.exe", "__uninstaller-nsis-nexus.exe", "C:\\resources\\runtime\\python.exe", "C:\\unrelated\\java.exe", "C:\\kestra-runtime\\java\\bin\\unknown.exe"]) {
    assert.equal(shouldSign(file), true, file);
  }
  assert.equal(config.win.signExecutable, undefined);
  assert.equal(config.win.signAndEditExecutable, undefined);
}));

test("positive signing overrides cannot silently defeat the Java integrity boundary", () => fixture(root => {
  assert.throws(() => withPreservedKestraJava({ win: { signExts: [".exe"] } }, root), /would modify/);
  assert.deepEqual(withPreservedKestraJava({ win: { signExts: [".dll"] } }, root).win.signExts.slice(0, 1), [".dll"]);
}));
