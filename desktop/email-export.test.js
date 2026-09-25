"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

test("native email save enforces origin, bounds, cancellation and safe extension", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "email-save-"));
  const source = fs.readFileSync(path.join(__dirname, "main.js"), "utf8");
  const block = source.slice(source.indexOf('ipcMain.handle("harness:saveEmailFile"'), source.indexOf("function closeLargeJsonExport"));
  let handler, choice, options;
  vm.runInNewContext(block, {
    ipcMain: {handle(_name, callback) { handler = callback; }},
    fromHarnessWindow: event => event.trusted,
    fs, path, Buffer, process, window: {},
    app: {getPath: () => root},
    dialog: {showSaveDialogSync(_window, value) { options = value; return choice; }},
  });
  assert.throws(() => handler({}, "reply.eml", "hello"), /Only the Nexus/);
  assert.throws(() => handler({trusted: true}, "reply.eml", ""), /bytes/);
  assert.throws(() => handler({trusted: true}, "reply.eml", "a".repeat(30_000_001)), /bytes/);
  assert.equal(handler({trusted: true}, "reply.eml", "hello").saved, false);
  assert.deepEqual(fs.readdirSync(root), []);
  choice = path.join(root, "selected.exe");
  const result = handler({trusted: true}, "../../untrusted.exe", "To: test@example.test\n\nWarm wishes");
  assert.equal(result.path, choice + ".eml");
  assert.equal(fs.existsSync(choice), false);
  assert.match(options.defaultPath, /untrusted\.exe\.eml$/);
  assert.match(fs.readFileSync(result.path, "utf8"), /Warm wishes/);
  assert.ok(!fs.readdirSync(root).some(name => name.endsWith(".part")));
});
